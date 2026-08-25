import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import ts from "typescript";

async function loadUploadGrouping() {
  const source = await readFile(
    new URL("../src/utils/uploadGrouping.ts", import.meta.url),
    "utf8",
  );
  const javascript = ts.transpileModule(source, {
    compilerOptions: {
      module: ts.ModuleKind.ESNext,
      target: ts.ScriptTarget.ES2022,
    },
  }).outputText;

  return import(`data:text/javascript;base64,${Buffer.from(javascript).toString("base64")}`);
}

test("image partitions are balanced without exceeding 5,000 images", async () => {
  const {
    MAX_DATASET_IMAGES,
    balancedImagePartitionSizes,
  } = await loadUploadGrouping();

  assert.equal(MAX_DATASET_IMAGES, 5_000);
  assert.deepEqual(balancedImagePartitionSizes(0), []);
  assert.deepEqual(balancedImagePartitionSizes(5_000), [5_000]);
  assert.deepEqual(balancedImagePartitionSizes(5_001), [2_501, 2_500]);
  assert.deepEqual(
    balancedImagePartitionSizes(20_000),
    [5_000, 5_000, 5_000, 5_000],
  );
  assert.deepEqual(
    balancedImagePartitionSizes(20_001),
    [4_001, 4_000, 4_000, 4_000, 4_000],
  );
});

test("automatic partition names use the requested underscore suffix", async () => {
  const {
    datasetPartitionName,
    uploadPartitionPreview,
  } = await loadUploadGrouping();

  assert.equal(datasetPartitionName("sample", 1), "sample_(1)");
  assert.equal(datasetPartitionName("sample", 12), "sample_(12)");
  assert.equal(datasetPartitionName("가".repeat(255), 12).length, 255);

  assert.deepEqual(uploadPartitionPreview("sample", 5_001), {
    imageCount: 5_001,
    partCount: 2,
    sizes: [2_501, 2_500],
    names: ["sample_(1)", "sample_(2)"],
  });
  assert.equal(uploadPartitionPreview("sample", 5_000), null);
  assert.equal(uploadPartitionPreview("", 5_001), null);
});

test("the upload screen previews server-side ZIP splitting and known image parts", async () => {
  const uploadPage = await readFile(
    new URL("../src/pages/UploadPage.tsx", import.meta.url),
    "utf8",
  );

  assert.match(uploadPage, /uploadPartitionPreview\(/);
  assert.match(uploadPage, /5,000장 초과 시 자동 분할/);
  assert.match(uploadPage, /개 데이터셋으로 자동 분할/);
  assert.match(uploadPage, /terminal\.datasets/);
});
