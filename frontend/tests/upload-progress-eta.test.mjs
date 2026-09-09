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

test("ETA waits for a representative window and keeps its last estimate through stalls", async () => {
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
  assert.equal(steady.remainingSeconds, null);

  const stillWarming = updateProgressEstimate(steady.state, {
    key: "transfer:1",
    completed: 49,
    total: 100,
    atMs: 4999,
  });
  assert.equal(stillWarming.remainingSeconds, null);

  const stable = updateProgressEstimate(stillWarming.state, {
    key: "transfer:1",
    completed: 50,
    total: 100,
    atMs: 5000,
  });
  assert.equal(stable.remainingSeconds, 5);

  const nextPhase = updateProgressEstimate(stable.state, {
    key: "processing:9",
    completed: 1,
    total: 100,
    atMs: 5100,
  });
  assert.equal(nextPhase.remainingSeconds, null);

  const paused = updateProgressEstimate(stable.state, {
    key: "transfer:1",
    completed: 50,
    total: 100,
    atMs: 20_000,
  });
  assert.equal(paused.remainingSeconds, stable.remainingSeconds);
  const resumed = updateProgressEstimate(paused.state, {
    key: "transfer:1",
    completed: 60,
    total: 100,
    atMs: 21_000,
  });
  assert.ok(
    resumed.remainingSeconds > 4,
    "a long stall must not be mistaken for a one-second transfer burst",
  );
});

test("one overall ETA survives several short ZIP phases without reporting completion early", async () => {
  const { updateProgressEstimate } = await loadUploadProgress();
  const samples = [
    [0, 0],
    [1_000, 50],
    [2_000, 100],
    [3_000, 150],
    [4_000, 200],
    [5_000, 250],
    // The first ZIP ends at 300 work units. The same aggregate key and total
    // continue through the next ZIP instead of restarting a phase-local ETA.
    [6_000, 300],
    [7_000, 350],
    [8_000, 400],
    [9_000, 450],
    [10_000, 500],
    [11_000, 550],
    [12_000, 600],
    [13_000, 650],
    [14_000, 700],
    [15_000, 750],
    [16_000, 800],
    [17_000, 850],
  ];
  let state = null;
  const observed = [];

  for (const [atMs, completed] of samples) {
    const estimate = updateProgressEstimate(state, {
      key: "overall-upload",
      completed,
      total: 900,
      atMs,
    });
    state = estimate.state;
    if (atMs >= 5_000) observed.push(estimate.remainingSeconds);
  }

  assert.ok(observed.every((remaining) => remaining !== null && remaining > 0));
  const done = updateProgressEstimate(state, {
    key: "overall-upload",
    completed: 900,
    total: 900,
    atMs: 18_000,
  });
  assert.equal(done.remainingSeconds, 0);
});

test("server processing progress ignores phase-local counters that reset between backend phases", async () => {
  const { uploadJobProcessingRatio } = await loadUploadProgress();

  assert.equal(uploadJobProcessingRatio({
    state: "running",
    total: 1,
    processed: 1,
    image_total: 0,
    image_processed: 0,
  }), 0);
  assert.equal(uploadJobProcessingRatio({
    state: "running",
    total: 200,
    processed: 80,
    image_total: 100,
    image_processed: 40,
  }), 0.4);
  assert.equal(uploadJobProcessingRatio({
    state: "done",
    total: 1,
    processed: 1,
    image_total: 0,
    image_processed: 0,
  }), 1);
});

test("ETA corrections are gradual when work changes from a fast phase to a slow phase", async () => {
  const { updateProgressEstimate } = await loadUploadProgress();
  let estimate = updateProgressEstimate(null, {
    key: "overall-upload",
    completed: 0,
    total: 3_000,
    atMs: 0,
  });
  estimate = updateProgressEstimate(estimate.state, {
    key: "overall-upload",
    completed: 500,
    total: 3_000,
    atMs: 5_000,
  });
  const fastPhaseEta = estimate.remainingSeconds;
  assert.equal(fastPhaseEta, 25);

  estimate = updateProgressEstimate(estimate.state, {
    key: "overall-upload",
    completed: 510,
    total: 3_000,
    atMs: 10_000,
  });
  assert.ok(estimate.remainingSeconds !== null);
  assert.ok(
    estimate.remainingSeconds <= fastPhaseEta + 5,
    "a phase boundary must not make the displayed ETA jump abruptly",
  );

  const stalled = updateProgressEstimate(estimate.state, {
    key: "overall-upload",
    completed: 510,
    total: 3_000,
    atMs: 30_000,
  });
  assert.equal(stalled.remainingSeconds, estimate.remainingSeconds);
});

test("ETA ignores alternating tiny labels and image completion bursts", async () => {
  const { updateProgressEstimate } = await loadUploadProgress();
  const total = 113_945_839_254;
  const sizes = [1_014_945, 37, 1_011_727, 37, 971_341, 37, 1_035_064, 37];
  let estimate = updateProgressEstimate(null, {
    key: "transfer:large",
    completed: 0,
    total,
    atMs: 0,
  });
  let completed = 0;
  let atMs = 0;
  const observed = [];

  for (let index = 0; index < 400; index += 1) {
    completed += sizes[index % sizes.length];
    // A large image often finishes in the same concurrent burst, while the
    // following tiny label is the next isolated completion event.
    atMs += index % 2 === 0 ? 8 : 92;
    estimate = updateProgressEstimate(estimate.state, {
      key: "transfer:large",
      completed,
      total,
      atMs,
    });
    if (estimate.remainingSeconds !== null) {
      observed.push(estimate.remainingSeconds);
    }
  }

  assert.ok(observed.length > 0);
  assert.ok(
    Math.max(...observed) / Math.min(...observed) < 1.03,
    "per-file completion order must not make a steady upload ETA jump by hours",
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

test("the upload page feeds one aggregate ETA across preparation, transfer, and server jobs", () => {
  assert.match(uploadPage, /const updateOverallEstimate = \(processedWork: number\) =>/);
  assert.match(uploadPage, /key: "overall-upload"/);
  assert.match(uploadPage, /updateOverallEstimate\(preparationWork\)/);
  assert.match(uploadPage, /updateOverallEstimate\(transferStart \+ uploadedBytes\)/);
  assert.match(
    uploadPage,
    /updateOverallEstimate\(\s*unitProcessStart \+ unitBytes \* ratio,?\s*\)/,
  );
  assert.doesNotMatch(uploadPage, /transferProgressKey/);
  assert.doesNotMatch(uploadPage, /key: `processing:\$\{activeJobId\}`/);
  assert.doesNotMatch(uploadPage, /job\.processed \/ job\.total/);
});
