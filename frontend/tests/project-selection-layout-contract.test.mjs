import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const projectsPage = await readFile(
  new URL("../src/pages/ProjectsPage.tsx", import.meta.url),
  "utf8",
);
const appCss = await readFile(
  new URL("../src/styles/app.css", import.meta.url),
  "utf8",
);

test("project actions stay in the card header and selection chips stay in the card body", () => {
  const headerStart = projectsPage.indexOf('<header className="project-card-header">');
  const headerEnd = projectsPage.indexOf("</header>", headerStart);
  const bodyStart = projectsPage.indexOf('className="project-card-body"', headerEnd);
  const tableStart = projectsPage.indexOf('<table className="project-dataset-table">', bodyStart);
  const headerMarkup = projectsPage.slice(headerStart, headerEnd);
  const chipMarkup = projectsPage.slice(bodyStart, tableStart);

  assert.notEqual(headerStart, -1);
  assert.notEqual(headerEnd, -1);
  assert.notEqual(bodyStart, -1);
  assert.notEqual(tableStart, -1);
  assert.match(headerMarkup, /dataset-actions[\s\S]*AI 학습[\s\S]*병합[\s\S]*데이터셋/);
  assert.match(chipMarkup, /project-chip-strip[\s\S]*class-image-count is-total/);
  assert.doesNotMatch(chipMarkup, /dataset-actions/);
  assert.doesNotMatch(projectsPage, /project-class-summary-row/);
  assert.doesNotMatch(projectsPage, /project-selection-summary/);
  assert.match(
    appCss,
    /\.project-card-header\s*\{[^}]*(?:display:\s*flex|display:\s*grid);/,
  );
  assert.match(appCss, /\.project-card-header\s+\.dataset-actions\s*\{[^}]*margin-left:\s*auto;/);
});

test("the chip strip presents the selected image total without a section title", () => {
  const summaryStart = projectsPage.indexOf('className="project-chip-strip"');
  const summaryEnd = projectsPage.indexOf('<table className="project-dataset-table">', summaryStart);
  const summaryMarkup = projectsPage.slice(summaryStart, summaryEnd);

  assert.notEqual(summaryStart, -1);
  assert.notEqual(summaryEnd, -1);
  assert.match(summaryMarkup, /className="class-image-count is-total"/);
  assert.match(summaryMarkup, />전체 이미지</);
  assert.match(summaryMarkup, /selectionSummary\.imageCount\.toLocaleString\(\)/);
  assert.doesNotMatch(summaryMarkup, /클래스별 이미지/);
  assert.doesNotMatch(summaryMarkup, /className="project-selection-metric"/);
  assert.doesNotMatch(appCss, /\.project-selection-summary/);
  assert.doesNotMatch(appCss, /\.project-selection-metric/);
});
