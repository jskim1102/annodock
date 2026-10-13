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
