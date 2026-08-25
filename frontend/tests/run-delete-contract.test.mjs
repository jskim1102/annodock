import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const trainingApi = await readFile(
  new URL("../src/api/training.ts", import.meta.url),
  "utf8",
);
const runsPage = await readFile(
  new URL("../src/pages/RunsPage.tsx", import.meta.url),
  "utf8",
);

test("run deletion uses the explicitly confirmed endpoint", () => {
  assert.match(
    trainingApi,
    /export async function deleteRun\(runId: number\): Promise<void>[\s\S]*?`\/api\/runs\/\$\{runId\}\?confirm=true`[\s\S]*?method: "DELETE"/,
  );
});

test("run list only offers record deletion for terminal selections", () => {
  assert.match(runsPage, /const hasActiveSelection = selectedRuns\.some\(\(run\) => ACTIVE_STATES\.has\(run\.state\)\)/);
  assert.match(runsPage, /disabled=\{selectedRuns\.length === 0 \|\| hasActiveSelection \|\| cleaning \|\| deleting\}/);
  assert.match(runsPage, /선택한 run 삭제/);
});

test("run deletion requires an accessible irreversible confirmation", () => {
  assert.match(runsPage, /function DeleteRunsDialog/);
  assert.match(runsPage, /role="dialog"/);
  assert.match(runsPage, /aria-modal="true"/);
  assert.match(runsPage, /run 기록과 산출물이 모두 삭제됩니다/);
  assert.match(runsPage, /원본 데이터셋은 유지됩니다/);
  assert.match(runsPage, /이 작업은 되돌릴 수 없습니다/);
});

test("bulk run deletion refreshes canonical state and preserves failed selections", () => {
  const handler = runsPage.match(
    /const deleteSelectedRuns = async \(\) => \{(?<body>[\s\S]*?)\n  \};/,
  )?.groups?.body ?? "";

  assert.match(handler, /await deleteRun\(run\.id\)/);
  assert.match(handler, /completedIds\.add\(run\.id\)/);
  assert.match(handler, /finally \{[\s\S]*?await getRuns\(\)/);
  assert.match(
    handler,
    /finally \{[\s\S]*?if \(completedIds\.size > 0\) invalidateStorageQuotaCache\(\)/,
  );
  assert.match(handler, /for \(const id of completedIds\) next\.delete\(id\)/);
});
