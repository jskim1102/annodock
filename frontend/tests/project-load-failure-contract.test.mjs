import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const projectsPage = await readFile(
  new URL("../src/pages/ProjectsPage.tsx", import.meta.url),
  "utf8",
);

test("project load failure is distinct from the legitimate empty state and can retry", () => {
  assert.match(projectsPage, /const \[loadAttempt, setLoadAttempt\] = useState\(0\)/);
  assert.match(projectsPage, /const retryProjectLoad = \(\) => \{/);
  assert.match(projectsPage, /setLoadAttempt\(\(current\) => current \+ 1\)/);
  assert.match(projectsPage, /className="card project-load-error-state"/);
  assert.match(projectsPage, /프로젝트 목록을 불러오지 못했습니다/);
  assert.match(projectsPage, /onClick=\{retryProjectLoad\}[\s\S]*?다시 시도/);

  const loadingBranch = projectsPage.indexOf("{loading ? (");
  const failureBranch = projectsPage.indexOf("project-load-error-state", loadingBranch);
  const emptyBranch = projectsPage.indexOf("project-empty-state", loadingBranch);
  assert.ok(loadingBranch >= 0 && failureBranch > loadingBranch && emptyBranch > failureBranch);
});

test("project-created notice expires and clears its timer on replacement or unmount", () => {
  assert.match(
    projectsPage,
    /if \(!createdProject\) return;[\s\S]*?window\.setTimeout\(\(\) => setCreatedProject\(null\), 4000\)[\s\S]*?window\.clearTimeout\(timer\)/,
  );
});

test("dead grid controls are removed while the table-oriented project hierarchy remains", () => {
  assert.doesNotMatch(projectsPage, /setView|data-view=|보기 방식|그리드 보기/);
  assert.match(projectsPage, /className="project-card-list"/);
  assert.match(projectsPage, /<table className="project-dataset-table">/);
});
