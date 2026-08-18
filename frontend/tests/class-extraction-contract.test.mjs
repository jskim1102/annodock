import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const projectsPage = await readFile(
  new URL("../src/pages/ProjectsPage.tsx", import.meta.url),
  "utf8",
);
const client = await readFile(
  new URL("../src/api/client.ts", import.meta.url),
  "utf8",
);
const appCss = await readFile(
  new URL("../src/styles/app.css", import.meta.url),
  "utf8",
);

test("the API client submits source dataset and project class ids", () => {
  assert.match(client, /export interface DatasetClassExtractionInput/);
  assert.match(
    client,
    /DatasetClassExtractionInput[\s\S]*?name:\s*string;[\s\S]*?dataset_ids:\s*number\[\];[\s\S]*?class_ids:\s*number\[\];/,
  );
  assert.match(client, /export function extractDatasetClasses/);
  assert.match(
    client,
    /requestJson<DatasetRow>\("\/api\/datasets\/extract",\s*jsonInit\("POST", input\)\)/,
  );
});

test("the project header exposes extraction beside merge for selected ready rows", () => {
  assert.match(
    projectsPage,
    /const selectedRows = getProjectSelectionRows\(project\.datasets\)[\s\S]*?dataset\.status === "ready"/,
  );
  assert.match(
    projectsPage,
    /dataset-actions[\s\S]*?AI 학습[\s\S]*?병합[\s\S]*?분리[\s\S]*?데이터셋/,
  );
  assert.match(
    projectsPage,
    /disabled=\{selectedRows\.length === 0 \|\| project\.classes\.length === 0\}[\s\S]*?onExtractAction/,
  );
  assert.match(
    projectsPage,
    /onExtractAction=\{\(\) => openClassExtractionDialog\(project, selectedRows\)\}/,
  );
});

test("the extraction dialog explains the copy and requires a name and class choice", () => {
  assert.match(projectsPage, /function ClassExtractionDialog/);
  assert.match(projectsPage, /className="dialog project-action-dialog class-extraction-dialog"/);
  assert.match(projectsPage, /role="dialog"/);
  assert.match(projectsPage, /aria-modal="true"/);
  assert.match(projectsPage, /aria-labelledby="class-extraction-title"/);
  assert.match(projectsPage, /aria-describedby="class-extraction-description"/);
  assert.match(projectsPage, /원본 데이터셋은 변경되지 않습니다/);
  assert.match(projectsPage, /선택한 클래스의 라벨이 하나도 없는 이미지는 제외/);
  assert.match(
    projectsPage,
    /포함된 이미지에서도 선택하지 않은 클래스의 라벨은 제외/,
  );
  assert.match(projectsPage, /aria-label="분리 원본 데이터셋"/);
  assert.match(projectsPage, /htmlFor="class-extraction-name"/);
  assert.match(projectsPage, /id="class-extraction-name"/);
  assert.match(projectsPage, /maxLength=\{255\}/);
  assert.match(
    projectsPage,
    /<fieldset className="class-extraction-fieldset" disabled=\{busy\}>/,
  );
  assert.match(projectsPage, /<legend>분리할 클래스<\/legend>/);
  assert.match(projectsPage, /target\.project\.classes\.map/);
  assert.match(projectsPage, /type="checkbox"/);
  assert.match(projectsPage, /checked=\{selectedClassIds\.has\(projectClass\.class_id\)\}/);
  assert.match(
    projectsPage,
    /getProjectClassImageCounts\(target\.project\.id, sourceDatasetIds\)/,
  );
  assert.match(
    projectsPage,
    /이미지[\s\S]*?classImageCountById\.get\(projectClass\.class_id\)[\s\S]*?장/,
  );
  assert.match(projectsPage, /className="class-extraction-class-image-count mono"/);
  assert.match(
    projectsPage,
    /disabled=\{busy \|\| !normalizedName \|\| selectedClassIds\.size === 0\}/,
  );
  assert.match(appCss, /\.class-extraction-class-list/);
  assert.match(appCss, /\.class-extraction-class-image-count/);
});

test("extraction is busy-safe, recoverable, and refreshes canonical projects", () => {
  assert.match(
    projectsPage,
    /await extractDatasetClasses\(\{[\s\S]*?name,[\s\S]*?dataset_ids:[\s\S]*?class_ids:/,
  );
  assert.match(
    projectsPage,
    /await syncProjectsAfterDatasetMutation\(\)[\s\S]*?setClassExtractionTarget\(null\)/,
  );
  assert.match(
    projectsPage,
    /const syncProjectsAfterDatasetMutation = async \(\) => \{[\s\S]*?await getProjects\(\)[\s\S]*?loadThumbnailEntries\(datasets\)[\s\S]*?setProjects\(response\.items\)/,
  );
  assert.match(projectsPage, /분리는 완료되었지만 프로젝트 목록을 새로고침하지 못했습니다/);
  assert.match(projectsPage, /finally\s*\{\s*setClassExtractionBusy\(false\)/);
  assert.match(projectsPage, /event\.key === "Escape" && !busy/);
  assert.match(projectsPage, /event\.target === event\.currentTarget && !busy/);
  assert.match(projectsPage, /error \? <div className="error-text project-dialog-error" role="alert"/);
  assert.match(projectsPage, /busy \? "분리 중…" : "분리"/);
});
