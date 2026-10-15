import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import ts from "typescript";


function moduleUrl(source) {
  return `data:text/javascript;base64,${Buffer.from(source).toString("base64")}`;
}


async function loadUploadApi(overrides = {}) {
  const source = await readFile(
    new URL("../src/api/upload.ts", import.meta.url),
    "utf8",
  );
  const harnessKey = `__annodockManifestHarness${Math.random().toString(16).slice(2)}`;
  const storage = overrides.storage ?? new Map();
  const calls = [];
  const harness = {
    apiFetch: overrides.apiFetch ?? (async () => ({})),
    requestJson: async (path, options) => {
      calls.push({ path, options });
      return overrides.requestJson?.(path, options);
    },
    responseOrThrow: overrides.responseOrThrow ?? (async (response) => response),
    readStoredJson: (key) => structuredClone(storage.get(key) ?? null),
    writeStoredJson: (key, value) => {
      storage.set(key, structuredClone(value));
      return true;
    },
    removeStoredValue: (key) => storage.delete(key),
  };
  globalThis[harnessKey] = harness;
  if (globalThis.window === undefined) globalThis.window = { setTimeout };
  const clientModule = moduleUrl(`
    const harness = globalThis[${JSON.stringify(harnessKey)}];
    export const captureWorkspaceId = () => 1;
    export const apiFetch = (...args) => harness.apiFetch(...args);
    export const requestJson = (...args) => harness.requestJson(...args);
    export const responseOrThrow = (...args) => harness.responseOrThrow(...args);
  `);
  const storageModule = moduleUrl(`
    const harness = globalThis[${JSON.stringify(harnessKey)}];
    export const readStoredJson = (...args) => harness.readStoredJson(...args);
    export const writeStoredJson = (...args) => harness.writeStoredJson(...args);
    export const removeStoredValue = (...args) => harness.removeStoredValue(...args);
  `);
  const javascript = ts.transpileModule(source, {
    compilerOptions: {
      module: ts.ModuleKind.ESNext,
      target: ts.ScriptTarget.ES2022,
    },
  }).outputText
    .replace('from "./client"', `from "${clientModule}"`)
    .replace('from "../store/workspace"', `from "${moduleUrl("export const getWorkspaceKind = () => 'personal';")}"`)
    .replace('from "../utils/storage"', `from "${storageModule}"`);
  const api = await import(`${moduleUrl(javascript)}#${Math.random()}`);
  return { api, calls, storage };
}

async function loadUploadGrouping() {
  const source = await readFile(
    new URL("../src/utils/uploadGrouping.ts", import.meta.url),
    "utf8",
  );
  return import(moduleUrl(ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
  }).outputText));
}


function item(index, kind = "image", size = 1) {
  const content = new Blob([new Uint8Array(size)]);
  return {
    relPath: `${kind === "label" ? "labels" : "images"}/sample-${index}.${kind === "label" ? "txt" : "jpg"}`,
    kind,
    file: {
      name: `sample-${index}`,
      size,
      lastModified: 1_000 + index,
      slice: (start, end) => content.slice(start, end),
    },
  };
}


function metadataOnlyItem(index) {
  return {
    relPath: `images/frame-${String(index).padStart(6, "0")}.jpg`,
    kind: "image",
    file: {
      name: `frame-${index}.jpg`,
      size: 0,
      lastModified: index,
    },
  };
}


test("a large selection stores one small manifest and creates idempotent session pages", async () => {
  let nextUploadId = 50_000;
  const createBodies = [];
  let batchId;
  const { api, storage } = await loadUploadApi({
    requestJson: async (path, options) => {
      if (options?.method === "PUT" && path.includes("/upload-batches/")) {
        batchId = path.split("/").at(-1);
        return { batch_id: batchId, state: "open", job_id: null };
      }
      if (path.endsWith("/uploads/batch")) {
        const body = JSON.parse(options.body);
        createBodies.push(body);
        return {
          uploads: body.files.map((file) => ({
            upload_id: nextUploadId++,
            chunk_size: file.chunk_size,
            received: [],
            size: file.size,
            state: "open",
          })),
        };
      }
      throw new Error(`unexpected request: ${path}`);
    },
  });
  const files = Array.from({ length: 2_005 }, (_, index) => item(index));

  const manifest = await api.beginUploadBatch(51, files);
  const prepared = await api.prepareUploadBatch(51, files, manifest, () => {});

  assert.equal(manifest.batchId, batchId);
  assert.deepEqual(createBodies.map((body) => body.files.length), [1_000, 1_000, 5]);
  assert.ok(createBodies.every((body) => body.batch_id === batchId));
  assert.equal(prepared.uploads.length, 2_005);
  const stored = storage.get("upload:51:resume");
  assert.equal(stored.batchId, batchId);
  assert.equal(Object.hasOwn(stored, "uploads"), false);
  assert.ok(JSON.stringify(stored).length < 512);
});


test("207,458 files begin with one constant-size request and resume record", async () => {
  let manifestBody;
  const { api, calls, storage } = await loadUploadApi({
    requestJson: async (path, options) => {
      manifestBody = JSON.parse(options.body);
      return {
        batch_id: path.split("/").at(-1),
        state: "open",
        job_id: null,
      };
    },
  });
  const files = Array.from(
    { length: 207_458 },
    (_, index) => metadataOnlyItem(index),
  );

  await api.beginUploadBatch(53, files);

  assert.equal(calls.length, 1);
  assert.equal(manifestBody.file_count, 207_458);
  assert.equal(manifestBody.total_size, 0);
  assert.ok(JSON.stringify(storage.get("upload:53:resume")).length < 512);
});


test("manifest replay recovers the durable job without sending upload ids", async () => {
  const files = [item(1), item(1, "label")];
  let putCount = 0;
  const { api, calls, storage } = await loadUploadApi({
    requestJson: async (path, options) => {
      if (options?.method === "PUT") {
        putCount += 1;
        return {
          batch_id: path.split("/").at(-1),
          state: putCount === 1 ? "open" : "sealed",
          job_id: putCount === 1 ? null : 71,
        };
      }
      if (options?.method === "POST" && path.endsWith("/complete")) {
        return { job_id: 71 };
      }
      throw new Error(`unexpected request: ${path}`);
    },
  });

  const first = await api.beginUploadBatch(52, files);
  const jobId = await api.completeUploadBatch(first);
  const resumed = await api.beginUploadBatch(52, files);

  assert.equal(jobId, 71);
  assert.equal(resumed.batchId, first.batchId);
  assert.equal(resumed.knownJobId, 71);
  assert.equal(storage.get("upload:52:resume").batchId, first.batchId);
  const completeCall = calls.find((call) => call.path.endsWith("/complete"));
  assert.equal(completeCall.options.method, "POST");
  assert.equal(completeCall.options.body, undefined);
  assert.equal(completeCall.path, `/api/datasets/52/upload-batches/${first.batchId}/complete`);
});


test("reselecting the same files after a reload recovers the draft dataset", async () => {
  const files = [item(1), item(1, "label")];
  const { api, storage } = await loadUploadApi();

  api.rememberUploadDatasetTarget(9, "dataset", 123, files);

  assert.equal(api.resumeUploadDatasetTarget(9, "dataset", files), 123);
  assert.equal(api.resumeUploadDatasetTarget(9, "renamed", files), null);
  assert.equal(api.resumeUploadDatasetTarget(10, "dataset", files), null);
  assert.equal(storage.size, 1);
  api.clearUploadDatasetTarget(9, "dataset", 123, files);
  assert.equal(storage.size, 0);
});

test("multi-source resume survives a completed source changing the generated name", async () => {
  const grouping = await loadUploadGrouping();
  const sources = ["A", "B"].map((name, index) => ({
    key: `folder:${index}`,
    kind: "folder",
    name,
    files: [item(index)],
  }));
  const original = grouping.createUploadPlan(sources, "combined", []);
  const first = await loadUploadApi();
  first.api.rememberUploadDatasetTarget(9, original[0].name, 123, sources[0].files);

  // Only storage survives the reload. The completed A now appears in the
  // canonical project list, so the same selected source is proposed as A (2).
  const reloaded = await loadUploadApi({ storage: first.storage });
  const resumed = grouping.createUploadPlan(sources, "combined", ["A"]);
  assert.equal(resumed[0].name, "A (2)");
  assert.equal(reloaded.api.resumeUploadDatasetTarget(9, null, sources[0].files), 123);
  assert.equal(reloaded.api.resumeUploadDatasetTarget(10, null, sources[0].files), null);
  assert.equal(reloaded.api.resumeUploadDatasetTarget(9, null, [item(99)]), null);

  reloaded.api.clearUploadDatasetTarget(9, null, 999, sources[0].files);
  assert.equal(reloaded.api.resumeUploadDatasetTarget(9, null, sources[0].files), 123);
  reloaded.api.clearUploadDatasetTarget(9, null, 123, sources[0].files);
  assert.equal(reloaded.api.resumeUploadDatasetTarget(9, null, sources[0].files), null);
});

test("selecting the same ZIP twice never assigns one resumed dataset to both merge sources", async () => {
  const { createUploadPlan } = await loadUploadGrouping();
  const files = [item(1, "zip")];
  const plan = createUploadPlan([
    { key: "zip:1", name: "archive", kind: "zip", files },
    { key: "zip:2", name: "archive", kind: "zip", files },
  ], "combined", []);
  let nextId = 124;
  const { api, calls } = await loadUploadApi({
    requestJson: async (path, options) => {
      assert.equal(path, "/api/datasets");
      return { id: nextId++, name: JSON.parse(options.body).name };
    },
  });
  // A prior attempt already saved a checkpoint for the identical selection.
  api.rememberUploadDatasetTarget(9, "archive", 123, files);
  const targets = {};
  for (const unit of plan) {
    const resumed = api.resumeUploadDatasetTarget(9, null, unit.batches.flat(), Object.values(targets));
    const targetId = resumed ?? (await api.createDatasetForUpload(unit.name, 9)).id;
    api.rememberUploadDatasetTarget(9, unit.name, targetId, unit.batches.flat());
    targets[unit.key] = targetId;
  }

  assert.deepEqual(plan.map((unit) => unit.name), ["archive", "archive (2)"]);
  assert.deepEqual(Object.values(targets), [123, 124]);
  assert.equal(calls.length, 1, "the second distinct source creates its own target");
});

test("selecting the same files repeatedly produces one manifest and one session per path", async () => {
  let declaredCount;
  const sessionPaths = [];
  const { api } = await loadUploadApi({
    requestJson: async (path, options) => {
      const body = JSON.parse(options.body);
      if (options.method === "PUT") {
        declaredCount = body.file_count;
        return { batch_id: path.split("/").at(-1), state: "open", job_id: null };
      }
      const paths = body.files.map((file) => file.filename);
      assert.equal(new Set(paths).size, paths.length, "the server rejects repeated paths with 422");
      sessionPaths.push(...paths);
      return {
        uploads: body.files.map((file, index) => ({
          upload_id: sessionPaths.length - paths.length + index + 1,
          chunk_size: file.chunk_size,
          received: [],
          size: file.size,
          state: "open",
        })),
      };
    },
  });
  const unique = Array.from({ length: 1_001 }, (_, index) => item(index));
  const selected = [...unique, ...unique];
  const manifest = await api.beginUploadBatch(51, selected);
  const prepared = await api.prepareUploadBatch(51, selected, manifest);

  assert.equal(declaredCount, unique.length);
  assert.equal(prepared.uploads.length, unique.length);
  assert.equal(prepared.totalBytes, unique.length);
  assert.deepEqual(sessionPaths, unique.map((file) => file.relPath));
});

test("different files sharing a path remain a validation error before session creation", async () => {
  const { api, calls } = await loadUploadApi();
  const first = item(1);
  const changed = item(1, "image", 2);
  await assert.rejects(api.beginUploadBatch(51, [first, changed]), /같은 경로/);
  assert.equal(calls.length, 0);
});


test("the upload page begins and seals one manifest per logical upload unit", async () => {
  const uploadPage = await readFile(
    new URL("../src/pages/UploadPage.tsx", import.meta.url),
    "utf8",
  );

  assert.match(uploadPage, /beginUploadBatch\(\s*targetId,/);
  assert.match(uploadPage, /completeUploadBatch\(/);
  assert.match(uploadPage, /resumeUploadDatasetTarget\(/);
  assert.match(uploadPage, /rememberUploadDatasetTarget\(/);
  assert.match(uploadPage, /clearUploadDatasetTarget\(/);
  assert.doesNotMatch(uploadPage, /openUploads\.push/);
  assert.doesNotMatch(uploadPage, /completeUploadBatches\(/);
});


function uploadFailure(status, code, message = code ?? "upload failed") {
  return Object.assign(new Error(message), {
    status,
    detail: code === undefined ? message : { code, message },
  });
}


test("an expired manifest retries once with a fresh id and preserves its draft target", async () => {
  const batchIds = [];
  const { api, storage } = await loadUploadApi({
    requestJson: async (path) => {
      const batchId = path.split("/").at(-1);
      batchIds.push(batchId);
      if (batchIds.length === 1) throw uploadFailure(409, "upload_batch_aborted");
      return { batch_id: batchId, state: "open", job_id: null };
    },
  });
  const files = [item(1)];
  api.rememberUploadDatasetTarget(9, "dataset", 51, files);
  let restarted = 0;

  const operation = await api.withUploadBatchRestart(
    () => 51,
    () => api.beginUploadBatch(51, files),
    () => {
      restarted += 1;
      assert.equal(storage.has("upload:51:resume"), false);
      assert.equal(api.resumeUploadDatasetTarget(9, "dataset", files), 51);
    },
  );

  assert.equal(restarted, 1);
  assert.equal(batchIds.length, 2);
  assert.notEqual(batchIds[0], batchIds[1]);
  assert.equal(operation.batchId, batchIds[1]);
  assert.equal(storage.get("upload:51:resume").batchId, batchIds[1]);
  assert.equal(api.resumeUploadDatasetTarget(9, "dataset", files), 51);
});


test("an expired session page restarts preparation under the new manifest", async () => {
  const batchIds = [];
  const sessionIds = [];
  const { api, storage } = await loadUploadApi({
    requestJson: async (path, options) => {
      if (options.method === "PUT") {
        const batchId = path.split("/").at(-1);
        batchIds.push(batchId);
        return { batch_id: batchId, state: "open", job_id: null };
      }
      const body = JSON.parse(options.body);
      sessionIds.push(body.batch_id);
      if (sessionIds.length === 1) throw uploadFailure(409, "upload_session_aborted");
      return { uploads: body.files.map((file, index) => ({
        upload_id: index + 100,
        chunk_size: file.chunk_size,
        received: [],
        size: file.size,
        state: "open",
      })) };
    },
  });
  const files = [item(1), item(2)];

  const prepared = await api.withUploadBatchRestart(() => 51, async () => {
    await api.beginUploadBatch(51, files);
    return api.prepareUploadBatch(51, files);
  }, () => {});

  assert.equal(batchIds.length, 2);
  assert.notEqual(batchIds[0], batchIds[1]);
  assert.deepEqual(sessionIds, batchIds);
  assert.equal(prepared.operation.batchId, batchIds[1]);
  assert.equal(storage.get("upload:51:resume").batchId, batchIds[1]);
});


for (const secondStage of ["manifest", "session"]) {
  test(`a second aborted ${secondStage} propagates its original error without another retry`, async () => {
    let manifestAttempts = 0;
    let sessionAttempts = 0;
    let restarts = 0;
    const secondFailure = uploadFailure(409, `upload_${secondStage === "manifest" ? "batch" : "session"}_aborted`, "second failure");
    const { api } = await loadUploadApi({
      requestJson: async (path, options) => {
        if (options.method === "PUT") {
          manifestAttempts += 1;
          if (manifestAttempts === 1) throw uploadFailure(409, "upload_batch_aborted");
          if (secondStage === "manifest") throw secondFailure;
          return { batch_id: path.split("/").at(-1), state: "open", job_id: null };
        }
        sessionAttempts += 1;
        throw secondFailure;
      },
    });
    const files = [item(1)];

    await assert.rejects(api.withUploadBatchRestart(() => 51, async () => {
      await api.beginUploadBatch(51, files);
      return api.prepareUploadBatch(51, files);
    }, () => { restarts += 1; }), (error) => error === secondFailure);

    assert.equal(manifestAttempts, 2);
    assert.equal(sessionAttempts, secondStage === "session" ? 1 : 0);
    assert.equal(restarts, 1);
  });
}


test("unrelated errors preserve the same manifest and never trigger expiration recovery", async () => {
  for (const failure of [
    uploadFailure(409, undefined, "이미 닫힌 업로드 세션입니다"),
    uploadFailure(409, "other_conflict"),
    uploadFailure(422, "upload_batch_aborted"),
    uploadFailure(503, "upload_session_aborted"),
    new TypeError("Failed to fetch"),
  ]) {
    let restarts = 0;
    const { api, calls, storage } = await loadUploadApi({
      requestJson: async () => { throw failure; },
    });
    await assert.rejects(api.withUploadBatchRestart(
      () => 51,
      () => api.beginUploadBatch(51, [item(1)]),
      () => { restarts += 1; },
    ), (error) => error === failure);
    assert.equal(calls.length, 1);
    assert.equal(restarts, 0);
    assert.equal(storage.get("upload:51:resume").batchId, calls[0].path.split("/").at(-1));
  }
});


test("a network failure during the fresh attempt keeps its new durable manifest", async () => {
  const batchIds = [];
  const networkFailure = new TypeError("Failed to fetch");
  let failNetwork = true;
  const { api, storage } = await loadUploadApi({
    requestJson: async (path) => {
      batchIds.push(path.split("/").at(-1));
      if (batchIds.length === 1) throw uploadFailure(409, "upload_batch_aborted");
      if (failNetwork) throw networkFailure;
      return { batch_id: batchIds.at(-1), state: "open", job_id: null };
    },
  });
  const files = [item(1)];
  await assert.rejects(api.withUploadBatchRestart(
    () => 51,
    () => api.beginUploadBatch(51, files),
    () => {},
  ), (error) => error === networkFailure);
  assert.notEqual(batchIds[0], batchIds[1]);
  assert.equal(storage.get("upload:51:resume").batchId, batchIds[1]);

  failNetwork = false;
  const recovered = await api.beginUploadBatch(51, files);
  assert.equal(recovered.batchId, batchIds[1]);
});


test("resume guidance is limited to transient upload failures", async () => {
  const { api } = await loadUploadApi();
  for (const failure of [new TypeError("Failed to fetch"), ...[0, 408, 429, 500, 503].map((status) => uploadFailure(status))]) {
    assert.equal(api.isResumableUploadError(failure), true);
  }
  for (const failure of [new Error("invalid data"), ...[400, 401, 403, 404, 409, 413, 422, 507].map((status) => uploadFailure(status))]) {
    assert.equal(api.isResumableUploadError(failure), false);
  }
});


async function loadUploadHandler(api, unit) {
  const source = await readFile(new URL("../src/pages/UploadPage.tsx", import.meta.url), "utf8");
  const permissionsStart = source.indexOf("const canManage =");
  const permissionsEnd = source.indexOf("const requestedProjectId =", permissionsStart);
  const start = source.indexOf("const startUpload = async () => {");
  const end = source.indexOf("const continueUploadAfterClassResolution", start);
  assert.ok(permissionsStart > 0 && permissionsEnd > permissionsStart);
  assert.ok(start > 0 && end > start);
  const states = {
    progress: { processed: 0, total: 0, current: "" },
    liveProgress: {},
    error: null,
    completedBatchCounts: {},
  };
  const progressLabels = [];
  const batchCounts = [];
  const harness = {
    ...api,
    active: { id: 1, kind: "personal", role: "owner" },
    captureWorkspaceId: () => 1,
    project: { id: 9, datasets: [] },
    uploadPlan: [unit],
    sources: [],
    datasetName: unit.name,
    mergeIntoDatasetId: null,
    selectedMergedDataset: null,
    uploadTargets: { [unit.key]: 51 },
    uploadDatasetResults: {},
    completedBatchCounts: {},
    selectionLockedRef: { current: false },
    uploadWorkspaceRef: { current: undefined },
    unitTransfersRef: { current: {} },
    progressEstimateRef: { current: null },
    classResolutionPreferencesRef: { current: {} },
    EMPTY_LIVE_PROGRESS: {},
    JOB_PHASE_LABELS: { done: "처리 완료" },
    uploadableBytes: (files) => files.reduce((sum, file) => sum + file.file.size, 0),
    updateProgressEstimate: () => ({ state: {}, remainingSeconds: null }),
    uploadJobProcessingRatio: () => 1,
    ApiError: class extends Error {},
    getDataset: async (id) => ({ id, name: unit.name, status: "ready", image_count: 1, annotation_count: 0, class_count: 0 }),
    getAllIssues: async () => [],
    invalidateStorageQuotaCache: () => {},
  };
  for (const [, setter] of source.matchAll(/\b(set[A-Z]\w*)\(/g)) {
    const key = setter[3].toLowerCase() + setter.slice(4);
    harness[setter] = (value) => {
      states[key] = typeof value === "function" ? value(states[key]) : value;
      if (Object.hasOwn(harness, key)) harness[key] = states[key];
      if (key === "progress") progressLabels.push(states[key].current);
      if (key === "completedBatchCounts") batchCounts.push({ ...states[key] });
    };
  }
  const handlerSource = `export function bind(harness) {
    const { ${Object.keys(harness).join(", ")} } = harness;
    ${source.slice(permissionsStart, permissionsEnd)}
    ${source.slice(start, end)}
    return startUpload;
  }`;
  const javascript = ts.transpileModule(handlerSource, {
    compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
  }).outputText;
  const { bind } = await import(moduleUrl(javascript));
  return {
    run: () => bind(harness)(),
    states,
    progressLabels,
    batchCounts,
    transferStates: harness.unitTransfersRef,
  };
}


test("the page restarts all unit files after a later transfer batch expires", async () => {
  const files = Array.from({ length: 1_002 }, (_, index) => item(index));
  const unit = { key: "unit", name: "dataset", batches: [files.slice(0, 1_001), files.slice(1_001)] };
  const manifests = [];
  const sessions = new Map();
  let firstBatchPages = 0;
  let completedId;
  const { api } = await loadUploadApi({
    requestJson: async (path, options) => {
      if (options?.method === "PUT") {
        const batchId = path.split("/").at(-1);
        manifests.push({ batchId, declared: JSON.parse(options.body).file_count });
        sessions.set(batchId, []);
        return { batch_id: batchId, state: "open", job_id: null };
      }
      if (path.endsWith("/uploads/batch")) {
        const body = JSON.parse(options.body);
        if (body.batch_id === manifests[0].batchId && ++firstBatchPages === 3) {
          throw uploadFailure(409, "upload_session_aborted");
        }
        const paths = sessions.get(body.batch_id);
        const base = paths.length;
        paths.push(...body.files.map((file) => file.filename));
        return { uploads: body.files.map((file, index) => ({
          upload_id: base + index + manifests.length * 10_000,
          chunk_size: file.chunk_size,
          received: [],
          size: file.size,
          state: "open",
        })) };
      }
      if (path.endsWith("/complete")) {
        completedId = path.split("/").at(-2);
        assert.equal(sessions.get(completedId).length, files.length);
        return { job_id: 71 };
      }
      if (path === "/api/jobs/71") return {
        job_id: 71,
        state: "done",
        phase: "done",
        datasets: [{ id: 51, name: "dataset", status: "ready" }],
      };
      throw new Error(`unexpected request: ${path}`);
    },
  });
  const handler = await loadUploadHandler(api, unit);
  await handler.run();

  assert.equal(handler.states.error, null);
  assert.equal(handler.states.done, true);
  assert.equal(manifests.length, 2);
  assert.notEqual(manifests[0].batchId, manifests[1].batchId);
  assert.deepEqual(manifests.map((manifest) => manifest.declared), [1_002, 1_002]);
  assert.deepEqual(sessions.get(completedId), files.map((file) => file.relPath));
  assert.ok(handler.progressLabels.includes("이전 업로드 기록이 만료되어 새로 시작합니다"));
  assert.deepEqual(handler.batchCounts.map((counts) => counts.unit), [1, 0, 1, 2]);
  assert.equal(handler.states.progress.processed, files.length * 3);
});


test("the page only promises resumability for a transient failure", async () => {
  for (const [failure, expected] of [
    [new TypeError("Failed to fetch"), "업로드 일시 중지 · 이어 올리기 가능"],
    [uploadFailure(409, "upload_batch_aborted"), "업로드 중단 · 오류를 확인해 주세요"],
    [uploadFailure(422), "업로드 중단 · 오류를 확인해 주세요"],
  ]) {
    const { api } = await loadUploadApi({ requestJson: async () => { throw failure; } });
    const handler = await loadUploadHandler(api, { key: "unit", name: "dataset", batches: [[item(1)]] });
    await handler.run();
    assert.equal(handler.states.progress.current, expected);
    assert.equal(handler.states.error, failure.message);
    assert.equal(handler.states.liveProgress.stage, "idle");
  }
});


async function interruptedUpload(stage, { repeatExpired = false } = {}) {
  const files = [item(1, "image", stage === "single_chunk" ? 8 * 1024 * 1024 : 1)];
  const unit = { key: "unit", name: "dataset", batches: [files] };
  const calls = [];
  const batchIds = [];
  const sessionBatches = new Map();
  const restartErrors = [];
  const targetAtManifest = [];
  const pauseFailure = uploadFailure(408, undefined, "temporary interruption");
  const expiredFailure = uploadFailure(409, stage.includes("chunk") ? "upload_session_aborted" : "upload_batch_aborted");
  const secondFailure = uploadFailure(409, stage.includes("chunk") ? "upload_session_aborted" : "upload_batch_aborted", "second expired attempt");
  let mode = "pause";
  let pollFailure = null;
  let nextUploadId = 100;
  const record = (requestStage, batchId) => {
    calls.push({ stage: requestStage, batchId });
    if (requestStage !== stage) return;
    if (mode === "pause") throw pauseFailure;
    if (mode === "expire" && batchId === batchIds[0]) throw expiredFailure;
    if (mode === "expire" && repeatExpired) throw secondFailure;
  };
  const { api, storage } = await loadUploadApi({
    requestJson: async (path, options) => {
      if (options?.method === "PUT") {
        const batchId = path.split("/").at(-1);
        record("manifest", batchId);
        batchIds.push(batchId);
        targetAtManifest.push(api.resumeUploadDatasetTarget(9, "dataset", files));
        return { batch_id: batchId, state: "open", job_id: null };
      }
      if (path.endsWith("/uploads/batch")) {
        const body = JSON.parse(options.body);
        record("page", body.batch_id);
        return { uploads: body.files.map((file) => {
          const uploadId = nextUploadId++;
          sessionBatches.set(uploadId, body.batch_id);
          return {
            upload_id: uploadId,
            // Exercise the existing oversized single-chunk compatibility path.
            chunk_size: stage === "single_chunk" && body.batch_id === batchIds[0]
              ? 8 * 1024 * 1024 : file.chunk_size,
            received: [],
            size: file.size,
            state: "open",
          };
        }) };
      }
      if (path.endsWith("/complete")) {
        record("complete", path.split("/").at(-2));
        return { job_id: 71 };
      }
      if (path === "/api/jobs/71") {
        record("poll", batchIds.at(-1));
        if (pollFailure !== null) throw pollFailure;
        return {
          job_id: 71,
          state: "done",
          phase: "done",
          datasets: [{ id: 51, name: "dataset", status: "ready" }],
        };
      }
      throw new Error(`unexpected request: ${path}`);
    },
    apiFetch: async (path, options) => {
      const single = /^\/api\/uploads\/(\d+)\/chunks\/\d+$/.exec(path);
      const uploadId = single === null
        ? JSON.parse(options.body.get("metadata")).chunks[0].upload_id
        : Number(single[1]);
      record(single === null ? "batch_chunk" : "single_chunk", sessionBatches.get(uploadId));
      return {};
    },
  });
  api.rememberUploadDatasetTarget(9, unit.name, 51, files);
  const handler = await loadUploadHandler({
    ...api,
    withUploadBatchRestart: async (...args) => {
      try {
        return await api.withUploadBatchRestart(...args);
      } catch (error) {
        restartErrors.push(error);
        throw error;
      }
    },
  }, unit);
  return {
    handler, calls, batchIds, storage, restartErrors, targetAtManifest,
    pauseFailure, expiredFailure, secondFailure, files,
    expire: () => { mode = "expire"; },
    resumeNormally: () => { mode = "normal"; },
    failPolling: (error) => { pollFailure = error; },
  };
}


for (const stage of ["single_chunk", "batch_chunk", "page", "complete"]) {
  test(`same-tab retry recovers an expired ${stage} without an initial manifest PUT`, async () => {
    const fixture = await interruptedUpload(stage);
    await fixture.handler.run();
    assert.match(fixture.handler.states.error, /temporary interruption/);
    assert.equal(fixture.handler.states.canResume, true);
    assert.equal(fixture.batchIds.length, 1);
    assert.ok(fixture.handler.transferStates.current.unit);
    const resumeStart = fixture.calls.length;

    fixture.expire();
    await fixture.handler.run();

    assert.equal(fixture.calls[resumeStart].stage, stage, "the cached transfer resumes directly at the interrupted stage");
    assert.equal(fixture.handler.states.error, null);
    assert.equal(fixture.handler.states.done, true);
    assert.equal(fixture.batchIds.length, 2);
    assert.notEqual(fixture.batchIds[0], fixture.batchIds[1]);
    assert.deepEqual(fixture.targetAtManifest, [51, 51]);
    assert.equal(fixture.handler.progressLabels.filter((label) => label === "이전 업로드 기록이 만료되어 새로 시작합니다").length, 1);
    assert.equal(fixture.handler.states.progress.processed, fixture.files[0].file.size * 3);
    assert.deepEqual(fixture.calls.at(-1), { stage: "poll", batchId: fixture.batchIds[1] });
  });
}


for (const stage of ["batch_chunk", "complete"]) {
  test(`same-tab ${stage} expiration only restarts once and preserves the second error`, async () => {
    const fixture = await interruptedUpload(stage, { repeatExpired: true });
    await fixture.handler.run();
    fixture.expire();
    await fixture.handler.run();

    assert.equal(fixture.batchIds.length, 2);
    assert.notEqual(fixture.batchIds[0], fixture.batchIds[1]);
    assert.equal(fixture.restartErrors.at(-1), fixture.secondFailure);
    assert.equal(fixture.handler.states.error, fixture.secondFailure.message);
    assert.equal(fixture.handler.states.done, false);
    assert.equal(fixture.handler.states.canResume, false);
    assert.equal(fixture.storage.get("upload:51:resume").batchId, fixture.batchIds[1]);
    assert.deepEqual(fixture.targetAtManifest, [51, 51]);
    assert.equal(fixture.handler.progressLabels.filter((label) => label === "이전 업로드 기록이 만료되어 새로 시작합니다").length, 1);
  });
}


test("polling remains outside expiration recovery and reuses the known completed job", async () => {
  const fixture = await interruptedUpload("complete");
  fixture.resumeNormally();
  fixture.failPolling(fixture.expiredFailure);
  await fixture.handler.run();

  assert.equal(fixture.handler.states.error, fixture.expiredFailure.message);
  assert.equal(fixture.batchIds.length, 1);
  assert.equal(fixture.handler.transferStates.current.unit.knownJobId, 71);
  assert.equal(fixture.handler.progressLabels.includes("이전 업로드 기록이 만료되어 새로 시작합니다"), false);
  const resumeStart = fixture.calls.length;

  fixture.failPolling(null);
  await fixture.handler.run();

  assert.equal(fixture.handler.states.error, null);
  assert.equal(fixture.handler.states.done, true);
  assert.deepEqual(fixture.calls.slice(resumeStart).map((call) => call.stage), ["poll"]);
  assert.equal(fixture.calls.filter((call) => call.stage === "complete").length, 1);
  assert.equal(fixture.batchIds.length, 1);
});
