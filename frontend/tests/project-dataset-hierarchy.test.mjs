import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

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
const client = await readFile(
  new URL("../src/api/client.ts", import.meta.url),
  "utf8",
);
const uploadApi = await readFile(
  new URL("../src/api/upload.ts", import.meta.url),
  "utf8",
);
const appCss = await readFile(
  new URL("../src/styles/app.css", import.meta.url),
  "utf8",
);

test("the project page renders parent projects with selectable child datasets", () => {
  assert.match(projectsPage, /getProjects/);
  assert.match(projectsPage, /createProject/);
  assert.match(projectsPage, /className="project-card-list"/);
  assert.match(projectsPage, /project-card/);
  assert.match(projectsPage, /className="project-card-header"/);
  assert.match(projectsPage, /className="project-card-body"/);
  assert.match(projectsPage, /className="project-dataset-table"/);
  assert.match(projectsPage, /project\.datasets/);
  assert.match(projectsPage, /project_id/);
  assert.doesNotMatch(projectsPage, /createDataset\(/);
  assert.doesNotMatch(projectsPage, /개 선택됨/);
});

test("only one project remains expanded at a time", () => {
  assert.match(
    projectsPage,
    /if \(current\.has\(projectId\)\) return new Set\(\);[\s\S]*?return new Set\(\[projectId\]\);/,
  );
  assert.match(projectsPage, /setExpanded\(new Set\(\[project\.id\]\)\)/);
});

test("project card headers present real aggregates as metadata", () => {
  assert.match(projectsPage, /className="project-card-meta"/);
  for (const label of ["데이터셋", "이미지", "클래스", "수정"]) {
    assert.match(projectsPage, new RegExp(label));
  }
  assert.match(projectsPage, /project\.dataset_count\.toLocaleString\(\)/);
  assert.match(projectsPage, /project\.image_count\.toLocaleString\(\)/);
  assert.match(projectsPage, /project\.class_count\.toLocaleString\(\)/);
  assert.match(projectsPage, /dateLabel\(project\.created_at\)/);
});

test("project cards use a folder mark and regular-weight name", () => {
  assert.match(projectsPage, /<Icon className="project-folder-icon" name="folder-solid" size=\{30\}/);
  assert.match(
    appCss,
    /\.project-folder-icon\s*\{[^}]*color:\s*var\(--color-accent\)/,
  );
  assert.match(
    appCss,
    /\.project-card-title\s*\{[^}]*font-size:\s*var\(--text-lg\);[^}]*font-weight:\s*400;/,
  );
});

test("the selected project header keeps actions and a conditional selection summary", () => {
  assert.match(projectsPage, /getDatasetSelectionSummary\(selectedRows, project\.class_count\)/);
  assert.match(
    projectsPage,
    /project-card-header[\s\S]*AI 학습[\s\S]*병합[\s\S]*데이터셋/,
  );
  assert.match(
    projectsPage,
    /class-image-count is-total[\s\S]*전체 이미지[\s\S]*selectionSummary\.imageCount\.toLocaleString\(\)/,
  );
  assert.doesNotMatch(projectsPage, /project-selection-summary/);
  assert.doesNotMatch(projectsPage, /project-selection-metric/);
});

test("selected datasets expose an on-demand per-class image breakdown", () => {
  assert.match(client, /export interface ProjectClassImageCount/);
  assert.match(client, /getProjectClassImageCounts/);
  assert.match(client, /query\.append\("dataset_ids", String\(datasetId\)\)/);
  assert.match(projectsPage, /getProjectClassImageCounts\(project\.id, datasetIds\)/);
  assert.match(
    projectsPage,
    /project-chip-strip[\s\S]*\{selectionSummary \? \([\s\S]*project-class-image-content/,
  );
  assert.doesNotMatch(projectsPage, /project-class-summary-row/);
  assert.doesNotMatch(projectsPage, /클래스별 이미지/);
  assert.match(projectsPage, /class-image-count/);
  assert.doesNotMatch(projectsPage, /className="project-class-image-title"/);
  assert.doesNotMatch(appCss, /\.project-class-image-title/);
  assert.match(
    projectsPage,
    /<span className="sr-only" role="status">[\s\S]*\{classImageCountsStatus\}/,
  );
  assert.doesNotMatch(projectsPage, /project-class-image-summary" aria-live/);
  assert.match(appCss, /\.project-class-image-list/);
});

test("dataset rows do not repeat project class counts or expose a second training action", () => {
  assert.doesNotMatch(projectsPage, /className="dataset-owner/);
  assert.doesNotMatch(projectsPage, /dataset\.name\} 학습/);
  assert.doesNotMatch(projectsPage, /dataset\.name\} 처리 중/);
  assert.doesNotMatch(appCss, /\.dataset-owner/);
});

test("dataset rows use semantic cells inside the nested full-width table", () => {
  const datasetRowCellRule = appCss.match(/\.dataset-row\s*>\s*td\s*\{([^}]*)\}/)?.[1] ?? "";
  const datasetTableRule = appCss.match(/\.project-dataset-table\s*\{([^}]*)\}/)?.[1] ?? "";
  const datasetProgressRule = appCss.match(/\.dataset-progress\s*\{([^}]*)\}/)?.[1] ?? "";

  assert.match(
    projectsPage,
    /<table className="project-dataset-table">[\s\S]*?<tr className=\{`dataset-row[\s\S]*?<td/,
  );
  assert.doesNotMatch(projectsPage, /<td colSpan=\{6\}>\s*<div className="dataset-row-content">/);
  assert.doesNotMatch(datasetRowCellRule, /display:\s*flex;/);
  assert.match(datasetTableRule, /width:\s*100%;/);
  assert.match(datasetProgressRule, /gap:\s*var\(--space-5\);/);
  assert.match(appCss, /\.dataset-row-menu\s*\{[^}]*margin-left:\s*auto;/);
});

test("project cards keep a relaxed row rhythm and approved expanded treatment", () => {
  const toolbarRule = appCss.match(/\.project-toolbar\s*\{([^}]*)\}/)?.[1] ?? "";
  const cardHeaderRule = appCss.match(/\.project-card-header\s*\{([^}]*)\}/)?.[1] ?? "";
  const datasetRowRule = appCss.match(/\.dataset-row\s*>\s*td\s*\{([^}]*)\}/)?.[1] ?? "";

  assert.match(toolbarRule, /padding:\s*var\(--space-6\);/);
  assert.match(cardHeaderRule, /min-height:/);
  assert.match(datasetRowRule, /height:\s*84px;/);
  assert.doesNotMatch(appCss, /\.project-work-list::before/);
  assert.doesNotMatch(appCss, /\.dataset-timeline-marker/);
  assert.doesNotMatch(projectsPage, /dataset-timeline-marker/);
  assert.match(appCss, /\.project-card\.is-expanded\s*\{[^}]*box-shadow:\s*inset 4px 0 0/);
});

test("project toolbar keeps the sort menu fixed-width beside intrinsic controls", () => {
  const scopedSortRule = appCss.match(/\.project-toolbar\s+\.sort-select\s*\{([^}]*)\}/)?.[1] ?? "";

  assert.match(scopedSortRule, /width:\s*142px;/);
  assert.match(scopedSortRule, /flex:\s*0 0 142px;/);
  assert.match(scopedSortRule, /margin-left:\s*auto;/);
});

test("new project creation sends class names and colors", () => {
  assert.match(newProjectDialog, /onCreate:\s*\(project:/);
  assert.match(newProjectDialog, /classes[\s\S]*color/);
  assert.match(newProjectDialog, /item\.name\.trim\(\)/);
});

test("new projects can reuse only the class catalog behind an existing dataset", () => {
  assert.match(newProjectDialog, /projects:\s*ProjectRow\[\]/);
  assert.equal((newProjectDialog.match(/type="radio"/g) ?? []).length, 2);
  assert.match(newProjectDialog, /신규 데이터셋 · 클래스 직접 입력/);
  assert.match(newProjectDialog, /기존 데이터셋 · 기존 클래스 활용/);
  assert.match(newProjectDialog, /project\.datasets\.map/);
  assert.match(newProjectDialog, /sourceProject\.classes\.map/);
  assert.match(newProjectDialog, /데이터셋은 이동하거나 복제하지 않습니다/);
  assert.match(projectsPage, /projects=\{projects\}/);
});

test("a project can be created before any initial class or dataset exists", () => {
  assert.match(newProjectDialog, /const canCreate = Boolean\(name\.trim\(\)\);/);
  assert.doesNotMatch(newProjectDialog, /canReuseClasses/);
  assert.match(newProjectDialog, /classInputMode === "existing"[\s\S]*selectedSource[\s\S]*:\s*\[\]/);
  assert.match(newProjectDialog, /첫 데이터셋 업로드 시 클래스가 설정됩니다/);
});

test("dataset upload requires and submits the fixed project context", () => {
  assert.match(uploadPage, /URLSearchParams/);
  assert.match(uploadPage, /project_id/);
  assert.match(uploadPage, /getProject/);
  assert.match(uploadPage, /createDatasetForUpload\(candidateName,\s*project\.id\)/);
  assert.match(uploadApi, /projectId:\s*number/);
  assert.match(uploadApi, /project_id:\s*projectId/);
  assert.match(client, /export function getProjects/);
  assert.match(client, /export function createProject/);
});
