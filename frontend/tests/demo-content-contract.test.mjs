import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const appShell = await readFile(
  new URL("../src/components/AppShell.tsx", import.meta.url),
  "utf8",
);
const projectsPage = await readFile(
  new URL("../src/pages/ProjectsPage.tsx", import.meta.url),
  "utf8",
);
const newProjectDialog = await readFile(
  new URL("../src/components/NewProjectDialog.tsx", import.meta.url),
  "utf8",
);
const uploadPage = await readFile(
  new URL("../src/pages/UploadPage.tsx", import.meta.url),
  "utf8",
);

test("the sidebar has no fixed demo counters", () => {
  assert.doesNotMatch(appShell, /sidebar-count/);
  assert.doesNotMatch(appShell, /sidebar-running/);
  assert.doesNotMatch(appShell, />34</);
});

test("the project dashboard uses lifecycle summaries without a hard-coded sample project", () => {
  for (const label of ["전체", "진행 중", "완료", "보관함"]) {
    assert.match(projectsPage, new RegExp(`<span>${label}</span>`));
  }
  assert.match(projectsPage, /project-card-list/);
  assert.doesNotMatch(projectsPage, /<strong>Annodock<\/strong>/);
});

test("an empty project collection hides list controls and selection actions", () => {
  assert.match(
    projectsPage,
    /\{loading \? \([\s\S]*?project-loading-state[\s\S]*?\) : projects\.length === 0 \? \([\s\S]*?project-empty-state[\s\S]*?\) : \(\s*<section className="card project-list-card"/,
  );
  assert.match(projectsPage, /프로젝트가 없습니다\./);
  assert.match(projectsPage, /검색 결과가 없습니다\./);
  assert.doesNotMatch(projectsPage, /표시할 데이터셋이 없습니다\./);
});

test("creation forms do not prefill sample projects, datasets, or classes", () => {
  assert.doesNotMatch(newProjectDialog, /name: "(?:person|car|traffic light)"/);
  assert.doesNotMatch(newProjectDialog, /placeholder="예:/);
  assert.doesNotMatch(uploadPage, /value="Annodock"/);
  assert.doesNotMatch(uploadPage, /placeholder="예:/);
});
