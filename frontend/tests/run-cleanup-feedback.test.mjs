import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const runsPage = await readFile(
  new URL("../src/pages/RunsPage.tsx", import.meta.url),
  "utf8",
);
const runDetailPage = await readFile(
  new URL("../src/pages/RunDetailPage.tsx", import.meta.url),
  "utf8",
);

test("run-list artifact cleanup remains visible after the request succeeds", () => {
  assert.match(runsPage, /const \[notice, setNotice\] = useState<string \| null>\(null\)/);
  assert.match(
    runsPage,
    /setNotice\(`\$\{cleanedCount\}개 run의 산출물을 정리했습니다\.`\)/,
  );
  assert.match(
    runsPage,
    /notice \? <div className="run-notice" role="status">\{notice\}<\/div>/,
  );
  assert.match(
    runsPage,
    /run\.artifacts_deleted_at !== null[\s\S]*?산출물 정리됨/,
  );
});

test("bulk cleanup refreshes canonical run state even after a partial failure", () => {
  const cleanupHandler = runsPage.match(
    /const cleanupSelected = async \(\) => \{(?<body>[\s\S]*?)\n  \};/,
  )?.groups?.body ?? "";

  assert.match(cleanupHandler, /let cleanedCount = 0/);
  assert.match(cleanupHandler, /finally \{[\s\S]*?await getRuns\(\)/);
  assert.match(cleanupHandler, /completedIds\.add\(run\.id\)/);
  assert.match(
    cleanupHandler,
    /finally \{[\s\S]*?if \(completedIds\.size > 0\) invalidateStorageQuotaCache\(\)/,
  );
  assert.match(runsPage, /cleaning \? "정리 중…" : "선택한 산출물 정리"/);
});

test("single run artifact cleanup invalidates quota only after success", () => {
  const cleanupHandler = runDetailPage.match(
    /const handleCleanup = async \(\) => \{(?<body>[\s\S]*?)\n  \};/,
  )?.groups?.body ?? "";

  assert.match(runDetailPage, /invalidateStorageQuotaCache/);
  assert.match(
    cleanupHandler,
    /await deleteRunArtifacts\(runId\);\s*invalidateStorageQuotaCache\(\);/,
  );
});
