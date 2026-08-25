import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import ts from "typescript";

async function loadUploadProgress() {
  const source = await readFile(
    new URL("../src/utils/uploadProgress.ts", import.meta.url),
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

const uploadPage = await readFile(
  new URL("../src/pages/UploadPage.tsx", import.meta.url),
  "utf8",
);
const uploadApi = await readFile(
  new URL("../src/api/upload.ts", import.meta.url),
  "utf8",
);
const apiClient = await readFile(
  new URL("../src/api/client.ts", import.meta.url),
  "utf8",
);
const appCss = await readFile(
  new URL("../src/styles/app.css", import.meta.url),
  "utf8",
);

test("ETA uses a smoothed rate and resets between upload phases", async () => {
  const { updateProgressEstimate } = await loadUploadProgress();

  const first = updateProgressEstimate(null, {
    key: "transfer:1",
    completed: 0,
    total: 100,
    atMs: 0,
  });
  assert.equal(first.remainingSeconds, null);

  const steady = updateProgressEstimate(first.state, {
    key: "transfer:1",
    completed: 10,
    total: 100,
    atMs: 1000,
  });
  assert.equal(steady.remainingSeconds, 9);

  const faster = updateProgressEstimate(steady.state, {
    key: "transfer:1",
    completed: 30,
    total: 100,
    atMs: 2000,
  });
  assert.ok(faster.remainingSeconds >= 5 && faster.remainingSeconds <= 7);

  const nextPhase = updateProgressEstimate(faster.state, {
    key: "processing:9",
    completed: 1,
    total: 100,
    atMs: 2100,
  });
  assert.equal(nextPhase.remainingSeconds, null);

  const paused = updateProgressEstimate(steady.state, {
    key: "transfer:1",
    completed: 10,
    total: 100,
    atMs: 11_000,
  });
  const resumed = updateProgressEstimate(paused.state, {
    key: "transfer:1",
    completed: 20,
    total: 100,
    atMs: 12_000,
  });
  assert.ok(
    resumed.remainingSeconds > 9,
    "a long stall must not be mistaken for a one-second transfer burst",
  );
});

test("remaining time is formatted as readable Korean duration", async () => {
  const { formatRemainingTime } = await loadUploadProgress();

  assert.equal(formatRemainingTime(null), "계산 중…");
  assert.equal(formatRemainingTime(12), "약 12초");
  assert.equal(formatRemainingTime(75), "약 1분 15초");
  assert.equal(formatRemainingTime(3720), "약 1시간 2분");
  assert.equal(formatRemainingTime(0), "곧 완료");
});

test("upload and ingestion expose image counts at sub-second polling cadence", () => {
  assert.match(apiClient, /image_total:\s*number/);
  assert.match(apiClient, /image_processed:\s*number/);
  assert.match(uploadApi, /uploadedImages:\s*number/);
  assert.match(uploadApi, /totalImages:\s*number/);
  assert.match(uploadApi, /await delay\(500\)/);
  assert.match(uploadPage, /이미지 \$\{liveProgress\.imageTotal\.toLocaleString\(\)\}장 중/);
  assert.match(uploadPage, /liveProgress\.imageProcessed\.toLocaleString\(\)/);
  assert.match(uploadPage, /예상 남은 시간/);
  assert.match(uploadPage, /formatRemainingTime\(liveProgress\.etaSeconds\)/);
  assert.match(appCss, /\.upload-progress-detail/);
  assert.match(appCss, /transition:\s*width 500ms linear/);
});
