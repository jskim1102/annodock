import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import ts from "typescript";

const moduleUrl = (source) => `data:text/javascript;base64,${Buffer.from(source).toString("base64")}`;
const tick = () => new Promise((resolve) => setImmediate(resolve));
const json = (body, status = 200) => new Response(JSON.stringify(body), {
  status, headers: { "Content-Type": "application/json" },
});
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

// Run the production client and upload module together. Only browser/network,
// auth identity, workspace selection and persistence are replaced at boundaries.
async function harness(t, handle) {
  const originals = Object.fromEntries(["window", "fetch"].map((key) => [key, globalThis[key]]));
  const h = {
    workspaceId: 11,
    kinds: new Map([[11, "personal"], [22, "team"]]),
    snapshot: { accessToken: "old-token", refreshToken: "refresh-token", user: { id: 1 } },
    storage: new Map(), calls: [], events: [],
    refresh: async () => "new-token",
  };
  globalThis.window = {
    location: { origin: "https://annodock.test", pathname: "/upload", search: "?ws=11" },
    setTimeout, clearTimeout,
    dispatchEvent: (event) => { h.events.push(event.type); return true; },
  };
  globalThis.fetch = async (path, init) => {
    const call = { path, init, headers: new Headers(init.headers) };
    h.calls.push(call);
    return handle?.(call, h) ?? new Response(null, { status: 204 });
  };
  const key = `__workspaceRequest${Math.random().toString(16).slice(2)}`;
  globalThis[key] = h;
  t.after(() => {
    Object.assign(globalThis, originals);
    delete globalThis[key];
  });
  const auth = moduleUrl(`
    const h = globalThis[${JSON.stringify(key)}];
    export const refreshAuthSession = (...args) => h.refresh(...args);
  `);
  const identity = moduleUrl(`
    const h = globalThis[${JSON.stringify(key)}];
    export const getAuthSnapshot = () => h.snapshot;
    export const clearAuthSession = () => {};
  `);
  const workspace = moduleUrl(`
    const h = globalThis[${JSON.stringify(key)}];
    export const getActiveWorkspaceId = () => h.workspaceId;
    export const getWorkspaceKind = (id) => h.kinds.get(id) ?? null;
  `);
  const storage = moduleUrl(`
    const h = globalThis[${JSON.stringify(key)}];
    export const readStoredJson = (key) => structuredClone(h.storage.get(key) ?? null);
    export const writeStoredJson = (key, value) => {
      h.storage.set(key, structuredClone(value)); return true;
    };
    export const removeStoredValue = (key) => h.storage.delete(key);
  `);
  const compile = async (path, dependencies) => {
    const source = await readFile(new URL(path, import.meta.url), "utf8");
    return moduleUrl(ts.transpileModule(source, {
      compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
    }).outputText.replace(/from "([^"]+)"/g, (_match, dependency) => {
      assert.ok(dependencies[dependency], `unmapped dependency ${dependency}`);
      return `from "${dependencies[dependency]}"`;
    }));
  };
  const clientModule = await compile("../src/api/client.ts", {
    "./auth": auth, "../store/auth": identity, "../store/workspace": workspace,
  });
  h.client = await import(clientModule);
  h.upload = await import(await compile("../src/api/upload.ts", {
    "./client": clientModule, "../utils/storage": storage, "../store/workspace": workspace,
  }));
  return h;
}

function file(size = 2) {
  const content = new Blob([new Uint8Array(size)]);
  return { relPath: "images/a.jpg", kind: "image", file: {
    name: "a.jpg", size, lastModified: 123, slice: (start, end) => content.slice(start, end),
  } };
}

for (const mode of ["refresh", "already rotated"]) {
  test(`a 401 retry keeps the first workspace when the selection changes (${mode})`, async (t) => {
    const first = deferred(), refreshed = deferred();
    const h = await harness(t, (_call, state) => state.calls.length === 1
      ? first.promise : new Response(null, { status: 204 }));
    let refreshCalls = 0;
    h.refresh = () => { refreshCalls += 1; return refreshed.promise; };
    const request = h.client.apiFetch("/api/projects", { headers: { "X-Test": "preserved" } });
    h.workspaceId = 22;
    if (mode === "already rotated") h.snapshot.accessToken = "new-token";
    first.resolve(new Response(null, { status: 401 }));
    await tick();
    refreshed.resolve("new-token");
    assert.equal((await request).status, 204);
    assert.deepEqual(h.calls.map((call) => call.headers.get("X-Workspace-Id")), ["11", "11"]);
    assert.deepEqual(h.calls.map((call) => call.headers.get("Authorization")), ["Bearer old-token", "Bearer new-token"]);
    assert.ok(h.calls.every((call) => call.headers.get("X-Test") === "preserved"));
    assert.equal(refreshCalls, mode === "refresh" ? 1 : 0);
  });
}

test("an explicit operation workspace overrides a later active selection", async (t) => {
  const h = await harness(t);
  h.workspaceId = 22;
  await h.client.apiFetch("/api/projects", { workspaceId: 11 });
  await h.client.apiFetch("/api/workspaces", { workspaceId: null });
  assert.deepEqual(h.calls.map((call) => call.headers.get("X-Workspace-Id")), ["11", null]);
  assert.ok(h.calls.every((call) => !Object.hasOwn(call.init, "workspaceId")));
});

test("quota cache isolates the same token between workspaces and ignores a stale response", async (t) => {
  const old = deferred();
  const h = await harness(t, (call) => call.headers.get("X-Workspace-Id") === "11"
    ? old.promise : json({ used_bytes: 220, referenced_bytes: 220, limit_bytes: 1000 }));
  const first = h.client.getStorageQuota("same-token");
  assert.equal(h.client.getStorageQuota("same-token"), first);
  h.workspaceId = 22;
  const second = h.client.getStorageQuota("same-token");
  // Always resolve the old request, including on the unmodified implementation.
  old.resolve(json({ used_bytes: 110, referenced_bytes: 110, limit_bytes: 1000 }));
  assert.notEqual(first, second);
  assert.equal((await second).used_bytes, 220);
  assert.equal((await first).used_bytes, 110);
  assert.equal(h.client.getStorageQuota("same-token"), second);
  assert.deepEqual(h.calls.map((call) => call.headers.get("X-Workspace-Id")), ["11", "22"]);
  h.client.invalidateStorageQuotaCache();
  assert.deepEqual(h.events, [h.client.STORAGE_QUOTA_INVALIDATED_EVENT]);
  assert.notEqual(h.client.getStorageQuota("same-token"), second);
});

test("manifest upload creation, sessions, chunks, completion and polling retain their original scope", async (t) => {
  const h = await harness(t, (call) => {
    if (call.path === "/api/datasets") return json({ id: 51, name: "dataset" }, 201);
    if (call.init.method === "PUT" && call.path.includes("/upload-batches/")) {
      return json({ batch_id: call.path.split("/").at(-1), state: "open", job_id: null });
    }
    if (call.path.endsWith("/uploads/batch")) return json({ uploads: [{
      upload_id: 501, chunk_size: 2, received: [], size: 2, state: "open",
    }] });
    if (call.path.endsWith("/chunks/batch")) return new Response(null, { status: 204 });
    if (call.path.endsWith("/complete")) return json({ job_id: 71 });
    if (call.path === "/api/jobs/71") return json({ id: 71, state: "done", datasets: [] });
    throw new Error(`unexpected request ${call.path}`);
  });
  const files = [file()];
  const captured = 11;
  h.workspaceId = 22;
  const dataset = await h.upload.createDatasetForUpload("dataset", 9, captured);
  const operation = await h.upload.beginUploadBatch(dataset.id, files, captured);
  const batch = await h.upload.prepareUploadBatch(dataset.id, files, operation);
  await h.upload.transferUploadBatch(batch, () => {});
  const jobId = await h.upload.completeUploadBatch(operation);
  await h.upload.pollUploadJob(jobId, () => {}, captured);
  assert.equal(operation.workspaceId, captured);
  assert.equal(batch.workspaceId, captured);
  assert.equal(h.calls.length, 6);
  assert.ok(h.calls.every((call) => call.headers.get("X-Workspace-Id") === "11"));
});

test("legacy preparation captures before preflight and keeps that scope through completion", async (t) => {
  const h = await harness(t, (call, state) => {
    if (call.path.endsWith("/preflight")) {
      state.workspaceId = 22;
      return new Response(null, { status: 204 });
    }
    if (call.path.endsWith("/uploads/batch")) return json({ uploads: [{
      upload_id: 502, chunk_size: 2, received: [], size: 2, state: "open",
    }] });
    if (call.path.endsWith("/chunks/batch")) return new Response(null, { status: 204 });
    if (call.path.endsWith("/complete")) return json({ job_id: 72 });
    throw new Error(`unexpected request ${call.path}`);
  });
  const batch = await h.upload.prepareUploadBatch(52, [file()]);
  const transferred = await h.upload.transferUploadBatch(batch, () => {});
  await h.upload.completeUploadBatches(batch, transferred.openUploads);
  assert.equal(batch.workspaceId, 11);
  assert.equal(h.calls.length, 4);
  assert.ok(h.calls.every((call) => call.headers.get("X-Workspace-Id") === "11"));
});

test("upload continuation APIs keep the supplied workspace for class resolution, issues and merge polling", async (t) => {
  const h = await harness(t, (call) => {
    if (call.path.endsWith("/class-resolution")) return json({ job_id: 70 });
    if (call.path === "/api/datasets/51") return json({ id: 51, status: "ready" });
    if (call.path.includes("/issues?")) return json({ items: [], total: 0 });
    if (call.path === "/api/datasets/merge") return json({ id: 53, active_job: { job_id: 73 } });
    if (call.path === "/api/jobs/73") return json({ state: "done", datasets: [{ id: 53, status: "ready" }] });
    if (call.path.endsWith("/merge-sources")) return json({ id: 53, status: "ready" });
    throw new Error(`unexpected request ${call.path}`);
  });
  h.workspaceId = 22;
  await h.client.resolveJobClassConflicts(70, { revision: "r", resolutions: [] }, 11);
  await h.client.getDataset(51, 11);
  await h.client.getAllIssues(51, 11);
  await h.client.mergeDatasets({ name: "merged", dataset_ids: [51, 52] }, 11);
  await h.client.extendMergedDataset(53, { dataset_ids: [54] }, 11);
  assert.equal(h.calls.length, 6);
  assert.ok(h.calls.every((call) => call.headers.get("X-Workspace-Id") === "11"));
});

test("target resume and removal are isolated by workspace even after selection changes", async (t) => {
  const h = await harness(t);
  const files = [file()];
  h.upload.rememberUploadDatasetTarget(9, "dataset", 51, files, 11);
  h.workspaceId = 22;
  assert.equal(h.upload.resumeUploadDatasetTarget(9, "dataset", files), null);
  h.upload.rememberUploadDatasetTarget(9, "dataset", 52, files, 22);
  assert.equal(h.upload.resumeUploadDatasetTarget(9, "dataset", files, [], 11), 51);
  assert.equal(h.upload.resumeUploadDatasetTarget(9, "dataset", files), 52);
  assert.ok([...h.storage.keys()].some((key) => key.startsWith("upload-target:11:9:")));
  assert.ok([...h.storage.keys()].some((key) => key.startsWith("upload-target:22:9:")));
  h.upload.clearUploadDatasetTarget(9, "dataset", 51, files, 11);
  assert.equal(h.upload.resumeUploadDatasetTarget(9, "dataset", files, [], 11), null);
  assert.equal(h.upload.resumeUploadDatasetTarget(9, "dataset", files), 52);
});

for (const workspaceId of [11, 22]) {
  test(`legacy target resume ${workspaceId === 11 ? "migrates only in personal" : "is ignored in a team"}`, async (t) => {
    const h = await harness(t);
    const files = [file()];
    h.upload.rememberUploadDatasetTarget(9, "dataset", 51, files, 11);
    const record = [...h.storage.values()][0];
    h.storage.clear();
    const legacyKey = `upload-target:9:${record.fingerprint}`;
    const scopedKey = `upload-target:${workspaceId}:9:${record.fingerprint}`;
    h.storage.set(legacyKey, record);
    h.workspaceId = workspaceId;
    assert.equal(h.upload.resumeUploadDatasetTarget(9, "dataset", files), workspaceId === 11 ? 51 : null);
    if (workspaceId === 11) {
      assert.deepEqual(h.storage.get(scopedKey), record);
      assert.equal(h.storage.has(legacyKey), false);
    } else {
      assert.equal(h.storage.has(scopedKey), false);
      assert.equal(h.storage.has(legacyKey), true);
    }
  });
}

test("insufficient_role responses explain required and current roles", async (t) => {
  const h = await harness(t, () => json({ detail: {
    code: "insufficient_role", required: "admin", actual: "editor",
  } }, 403));
  await assert.rejects(h.client.requestJson("/api/projects", { method: "POST" }), (error) => (
    error.status === 403 && error.message === "이 작업은 관리자 이상만 할 수 있어요 (지금: 라벨러)"
  ));
  assert.deepEqual(h.events, ["annodock:role-error"]);
});

test("hidden viewer denial preserves the security response without displaying a viewer role label", async (t) => {
  const detail = { code: "insufficient_role", required: "editor", actual: "viewer" };
  const h = await harness(t, () => json({ detail }, 403));
  await assert.rejects(h.client.requestJson("/api/datasets", { method: "POST" }), (error) => {
    assert.equal(error.status, 403);
    assert.deepEqual(error.detail, detail);
    assert.match(error.message, /라벨러 이상만 할 수 있어요/);
    assert.doesNotMatch(error.message, /viewer|보기 전용|편집자/);
    return true;
  });
  assert.deepEqual(h.events, ["annodock:role-error"]);
  assert.equal(h.calls.length, 1, "a role denial must not retry the mutation");
});
