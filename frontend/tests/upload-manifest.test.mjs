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
