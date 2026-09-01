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
  const harnessKey = `__annodockUploadHarness${Math.random().toString(16).slice(2)}`;
  const storage = overrides.storage ?? new Map();
  const calls = [];
  const writes = [];
  const removals = [];
  const harness = {
    apiFetch: overrides.apiFetch ?? (async () => ({})),
    requestJson: async (path, options) => {
      calls.push({ path, options });
      return overrides.requestJson?.(path, options);
    },
    responseOrThrow: overrides.responseOrThrow ?? (async (response) => response),
    readStoredJson: (key) => structuredClone(storage.get(key) ?? null),
    writeStoredJson: (key, value) => {
      writes.push({ key, value: structuredClone(value) });
      storage.set(key, structuredClone(value));
      return true;
    },
    removeStoredValue: (key) => {
      removals.push(key);
      storage.delete(key);
    },
  };
  globalThis[harnessKey] = harness;
  if (globalThis.window === undefined) {
    globalThis.window = { setTimeout };
  }
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
  return { api, calls, writes, removals, storage };
}

function item(index, kind = "image", size = 1) {
  const content = new Blob([new Uint8Array(size)]);
  return {
    relPath: `images/sample-${index}.jpg`,
    kind,
    file: {
      name: `sample-${index}.jpg`,
      size,
      lastModified: 1_000 + index,
      slice: (start, end) => content.slice(start, end),
    },
  };
}

function legacyFingerprint(datasetId, uploadItem) {
  const value = `${datasetId}:${uploadItem.relPath}:${uploadItem.file.size}:${uploadItem.file.lastModified}`;
  let hash = 2166136261;
  for (let index = 0; index < value.length; index += 1) {
    hash ^= value.charCodeAt(index);
    hash = Math.imul(hash, 16777619);
  }
  return `upload:${(hash >>> 0).toString(16)}`;
}

test("20,005 files prepare in ordered batches of at most 1000", async () => {
  let nextUploadId = 10_000;
  const createBodies = [];
  const { api, storage } = await loadUploadApi({
    requestJson: async (path, options) => {
      if (path.endsWith("/upload-batches/preflight")) return undefined;
      if (path.endsWith("/uploads/batch")) {
        const body = JSON.parse(options.body);
        createBodies.push(body);
        return {
          uploads: body.files.map(() => ({
            upload_id: nextUploadId++,
            chunk_size: 4 * 1024 * 1024,
            received: [],
          })),
        };
      }
      throw new Error(`unexpected request: ${path}`);
    },
  });
  const files = Array.from({ length: 20_005 }, (_, index) => item(index));
  const preparation = [];

  const batch = await api.prepareUploadBatch(41, files, (progress) => {
    preparation.push(progress);
  });

  assert.deepEqual(
    createBodies.map((body) => body.files.length),
    [...Array(20).fill(1_000), 5],
  );
  assert.deepEqual(
    batch.uploads.map((upload) => upload.item.relPath),
    files.map((file) => file.relPath),
  );
  assert.deepEqual(
    preparation.map((progress) => progress.preparedFiles),
    [
      0,
      ...Array.from({ length: 20 }, (_, index) => (index + 1) * 1_000),
      20_005,
    ],
  );
  assert.ok(preparation.every((progress) => progress.totalFiles === 20_005));
  assert.deepEqual([...storage.keys()], ["upload:41:resume"]);
  assert.equal(Object.keys(storage.get("upload:41:resume").uploads).length, 20_005);
});

test("transfer groups tiny files into bounded batches with at most eight requests", async () => {
  let nextUploadId = 20_000;
  let activeTransfers = 0;
  let maximumTransfers = 0;
  let completedUploadIds = [];
  const transferBatches = [];
  const { api } = await loadUploadApi({
    requestJson: async (path, options) => {
      if (path.endsWith("/upload-batches/preflight")) return undefined;
      if (path.endsWith("/uploads/batch")) {
        const body = JSON.parse(options.body);
        return {
          uploads: body.files.map(() => ({
            upload_id: nextUploadId++,
            chunk_size: 4 * 1024 * 1024,
            received: [],
          })),
        };
      }
      if (path.endsWith("/upload-batches/complete")) {
        completedUploadIds = JSON.parse(options.body).upload_ids;
        return { job_id: 77 };
      }
      throw new Error(`unexpected request: ${path}`);
    },
    apiFetch: async (path, options) => {
      activeTransfers += 1;
      maximumTransfers = Math.max(maximumTransfers, activeTransfers);
      assert.equal(path, "/api/datasets/42/uploads/chunks/batch");
      assert.equal(options.method, "POST");
      const metadata = JSON.parse(options.body.get("metadata"));
      const parts = options.body.getAll("chunks");
      transferBatches.push({ metadata, parts });
      await new Promise((resolve) => (
        setTimeout(resolve, metadata.chunks[0].upload_id % 3 + 1)
      ));
      activeTransfers -= 1;
      return {};
    },
  });
  const files = Array.from({ length: 1_025 }, (_, index) => item(index));
  const batch = await api.prepareUploadBatch(42, files, () => {});
  const progress = [];

  const transferred = await api.transferUploadBatch(batch, (update) => {
    progress.push(update);
  });

  assert.equal(transferred.knownJobId, null);
  assert.equal(maximumTransfers, 8);
  assert.deepEqual(completedUploadIds, []);
  assert.deepEqual(
    transferBatches.map(({ metadata }) => metadata.chunks.length),
    [...Array(8).fill(128), 1],
  );
  assert.ok(transferBatches.every(({ metadata, parts }) => (
    metadata.chunks.length === parts.length
    && metadata.chunks.every((chunk) => chunk.size === 1)
  )));

  const jobId = await api.completeUploadBatches(batch, transferred.openUploads);

  assert.equal(jobId, 77);
  assert.deepEqual(
    completedUploadIds,
    Array.from({ length: 1_025 }, (_, index) => 20_000 + index),
  );
  assert.equal(progress.at(-1).uploadedBytes, 1_025);
  assert.equal(progress.at(-1).totalBytes, 1_025);
  assert.equal(progress.at(-1).uploadedImages, 1_025);
  assert.equal(progress.at(-1).totalImages, 1_025);
  assert.ok(progress.every((update, index) => (
    index === 0 || update.uploadedBytes >= progress[index - 1].uploadedBytes
  )));
});

test("retrying the same prepared batch sends only chunks not confirmed before the pause", async () => {
  let attempt = 1;
  let requestIndex = 0;
  const retryChunkCounts = [];
  const { api } = await loadUploadApi({
    apiFetch: async (_path, options) => {
      const metadata = JSON.parse(options.body.get("metadata"));
      if (attempt === 1) {
        const currentRequest = requestIndex;
        requestIndex += 1;
        if (currentRequest === 0) {
          await new Promise((resolve) => setTimeout(resolve, 10));
          return {};
        }
        const error = new Error("upload paused");
        error.status = 400;
        throw error;
      }
      retryChunkCounts.push(metadata.chunks.length);
      return {};
    },
  });
  const files = Array.from({ length: 129 }, (_, index) => item(index));
  const batch = {
    datasetId: 46,
    uploads: files.map((uploadItem, index) => ({
      item: uploadItem,
      resumeKey: "upload:46:resume",
      session: {
        upload_id: 40_000 + index,
        chunk_size: 4 * 1024 * 1024,
        received: [],
        size: 1,
        state: "open",
      },
    })),
    totalBytes: files.length,
    resumeKey: "upload:46:resume",
    resumeRecord: { uploads: {} },
  };

  await assert.rejects(
    api.transferUploadBatch(batch, () => {}),
    /upload paused/,
  );

  assert.ok(batch.uploads.slice(0, 128).every((upload) => (
    upload.session.received.includes(0)
  )));
  assert.deepEqual(batch.uploads[128].session.received, []);

  attempt = 2;
  const retryProgress = [];
  await api.transferUploadBatch(batch, (progress) => retryProgress.push(progress));

  assert.equal(retryProgress[0].uploadedBytes, 128);
  assert.equal(retryProgress[0].uploadedImages, 128);
  assert.deepEqual(retryChunkCounts, [1]);
  assert.equal(retryProgress.at(-1).uploadedBytes, 129);
});

test("transfer batches stay below the seven MiB payload envelope", async () => {
  const payloadSizes = [];
  const { api } = await loadUploadApi({
    apiFetch: async (_path, options) => {
      const metadata = JSON.parse(options.body.get("metadata"));
      payloadSizes.push(metadata.chunks.reduce(
        (sum, chunk) => sum + chunk.size,
        0,
      ));
      return {};
    },
  });
  const chunkSize = 3 * 1024 * 1024;
  const files = Array.from(
    { length: 3 },
    (_, index) => item(index, "image", chunkSize),
  );
  const batch = {
    datasetId: 45,
    uploads: files.map((uploadItem, index) => ({
      item: uploadItem,
      resumeKey: "upload:45:resume",
      session: {
        upload_id: 30_000 + index,
        chunk_size: chunkSize,
        received: [],
        size: chunkSize,
        state: "open",
      },
    })),
    totalBytes: chunkSize * files.length,
    resumeKey: "upload:45:resume",
    resumeRecord: { uploads: {} },
  };

  await api.transferUploadBatch(batch, () => {});

  assert.deepEqual(payloadSizes, [6 * 1024 * 1024, 3 * 1024 * 1024]);
  assert.ok(payloadSizes.every((size) => size <= 7 * 1024 * 1024));
});

test("legacy per-file resume data migrates to one dataset-scoped record", async () => {
  const uploadItem = item(1);
  const oldKey = legacyFingerprint(43, uploadItem);
  const storage = new Map([[oldKey, { uploadId: 88, chunkSize: 1024 }]]);
  let batchCreateCalls = 0;
  const { api, removals } = await loadUploadApi({
    storage,
    requestJson: async (path) => {
      if (path.endsWith("/upload-batches/preflight")) return undefined;
      if (path === "/api/uploads/88") {
        return {
          upload_id: 88,
          chunk_size: 1024,
          received: [],
          size: 1,
          state: "open",
        };
      }
      if (path.endsWith("/uploads/batch")) {
        batchCreateCalls += 1;
        return { uploads: [] };
      }
      throw new Error(`unexpected request: ${path}`);
    },
  });

  const batch = await api.prepareUploadBatch(43, [uploadItem], () => {});

  assert.equal(batchCreateCalls, 0);
  assert.equal(storage.has(oldKey), false);
  assert.ok(removals.includes(oldKey));
  assert.deepEqual([...storage.keys()], ["upload:43:resume"]);
  assert.equal(storage.get("upload:43:resume").uploads[uploadItem.relPath].uploadId, 88);
  api.clearUploadBatchResume(batch);
  assert.equal(storage.size, 0);
});

test("ZIP session metadata keeps extraction estimates in the batch API", async () => {
  const zipItem = item(3, "zip", 25);
  zipItem.relPath = "release.zip";
  let createBody;
  const { api } = await loadUploadApi({
    requestJson: async (path, options) => {
      if (path.endsWith("/upload-batches/preflight")) return undefined;
      if (path.endsWith("/uploads/batch")) {
        createBody = JSON.parse(options.body);
        return {
          uploads: [{ upload_id: 99, chunk_size: 1024, received: [] }],
        };
      }
      throw new Error(`unexpected request: ${path}`);
    },
  });

  await api.prepareUploadBatch(44, [zipItem], () => {});

  assert.equal(createBody.files[0].kind, "zip");
  assert.equal(createBody.files[0].file_count, 1);
  assert.equal(createBody.files[0].expected_extracted_size, 100);
});

test("the upload page renders preparation counts while sessions are created", async () => {
  const uploadPage = await readFile(
    new URL("../src/pages/UploadPage.tsx", import.meta.url),
    "utf8",
  );

  assert.match(uploadPage, /prepareUploadBatch\(targetId, batchFiles, \(\{/);
  assert.match(uploadPage, /preparedFiles/);
  assert.match(uploadPage, /totalFiles/);
  assert.match(uploadPage, /준비 중/);
  assert.match(uploadPage, /pendingBatch\?:/);
  assert.match(uploadPage, /pendingBatch\?\.batch/);
  assert.match(uploadPage, /transferState\.pendingBatch = \{ index: batchIndex, batch \}/);
  assert.match(uploadPage, /transferState\.pendingBatch = undefined/);
});
