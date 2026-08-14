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

test("project datasets expose source metadata required by merged rows", () => {
  assert.ok(
    /export interface ProjectDatasetRow extends DatasetListItem/.test(client)
      || /export interface ProjectDatasetRow[\s\S]*?source_datasets:\s*(?:DatasetListItem|ProjectDatasetSourceRow)\[\]/.test(client),
    "ProjectDatasetRow must expose merged source datasets",
  );
});

test("only merged datasets with sources render an accessible disclosure", () => {
  assert.match(
    projectsPage,
    /dataset\.is_merged\s*&&\s*dataset\.source_datasets\.length\s*>\s*0/,
  );
  assert.match(projectsPage, /className="merged-(?:source|dataset)-toggle"/);
  assert.match(projectsPage, /aria-expanded=\{[^}]+\}/);
  assert.match(
    projectsPage,
    /const\s+mergeSourcesId\s*=\s*`merged-[^`]*\$\{dataset\.id\}`[\s\S]*aria-controls=\{mergeSourcesId\}/,
  );
  assert.match(
    projectsPage,
    /<Icon name=\{[^?]+\?\s*"chevron-down"\s*:\s*"chevron-right"\}/,
  );
});

test("expanded sources render in the row immediately after their merged dataset", () => {
  assert.match(
    projectsPage,
    /<tr className=\{`dataset-row[\s\S]*?<\/tr>[\s\S]*?merged-source-row/,
  );
  assert.match(projectsPage, /className="merged-source-row"/);
  assert.match(projectsPage, /id=\{mergeSourcesId\}/);
  assert.match(projectsPage, /<td colSpan=\{5\}>/);
  assert.match(projectsPage, /dataset\.source_datasets\.map\(\(source\)/);
});

test("source details use a semantic nested table with selection, dataset, image, and label columns", () => {
  assert.match(projectsPage, /merged-dataset-badge[^>]*>병합</);
  assert.match(projectsPage, /<table[\s\S]*?className="merged-source-table"/);
  assert.match(projectsPage, /<caption className="sr-only">[^<]*원본 데이터셋[^<]*<\/caption>/);
  assert.doesNotMatch(projectsPage, /merged-source-table[\s\S]{0,400}?<thead>/);
  assert.match(
    projectsPage,
    /className="merged-source-name"[\s\S]*?<strong>\{source\.name\}<\/strong>[\s\S]*?이미지 \{source\.image_count\.toLocaleString\(\)\} · 라벨 \{source\.annotation_count\.toLocaleString\(\)\}/,
  );
  assert.match(projectsPage, /<tbody>[\s\S]*dataset\.source_datasets\.map\(\(source\)[\s\S]*<tr key=\{source\.id\}>/);
  assert.match(projectsPage, /source\.name/);
  assert.match(projectsPage, /source\.image_count\.toLocaleString\(\)/);
  assert.match(projectsPage, /source\.annotation_count\.toLocaleString\(\)/);
  assert.match(
    projectsPage,
    /appHref\(`\/datasets\/\$\{source\.id\}\/viewer`\)/,
  );
  assert.match(
    projectsPage,
    /sourceSelectionBlocked[\s\S]*disabled=\{source\.status !== "ready" \|\| sourceSelectionBlocked\}[\s\S]*aria-checked=\{selected\.has\(source\.id\)\}[\s\S]*onClick=\{\(\) => onSelect\(source\.id, \[dataset\.id\]\)\}/,
  );
  assert.doesNotMatch(projectsPage, /병합 시점[\s\S]*(?:스냅샷|복사본)/);
  assert.doesNotMatch(projectsPage, /원본 변경[\s\S]*자동 반영되지 않/);
  assert.doesNotMatch(projectsPage, /원본과 별도로 저장 공간/);
  assert.doesNotMatch(projectsPage, /className="merged-source-(?:heading|notice|panel)"/);
  assert.doesNotMatch(projectsPage, /<ul className="merged-source-list">/);
  assert.doesNotMatch(projectsPage, /dataset\.source_datasets\.map\(\(source\)[\s\S]*?<li key=\{source\.id\}>/);
});

test("merged datasets can be extended or consolidated from the merge dialog", () => {
  assert.match(projectsPage, /datasets\.filter\(\(dataset\) => dataset\.is_merged\)/);
  assert.match(projectsPage, /extendMergedDataset/);
  assert.doesNotMatch(
    projectsPage,
    /병합 데이터셋은 다른 데이터셋과 다시 합칠 수 없습니다/,
  );
});

test("merged source details avoid decorative timelines and dots", () => {
  const combined = projectsPage + appCss;
  assert.doesNotMatch(combined, /merged-source-(?:timeline|marker|dot)/);
  assert.doesNotMatch(combined, /source-dataset-(?:timeline|marker|dot)/);
  assert.doesNotMatch(appCss, /\.merged-source-list/);
  assert.match(appCss, /\.merged-source-table\s*\{[^}]*width:\s*100%;/);
  assert.doesNotMatch(appCss, /\.merged-source-(?:heading|notice|panel)/);
  assert.doesNotMatch(appCss, /\.merged-source-table th/);
  assert.doesNotMatch(
    projectsPage,
    /merged-source-table[\s\S]*?<Icon name="folder"/,
  );
  const mergedStyles = appCss.slice(
    appCss.indexOf(".merged-dataset-toggle"),
    appCss.indexOf(".dataset-thumb"),
  );
  assert.doesNotMatch(mergedStyles, /#[0-9a-f]{3,8}|rgba?\(/i);
});

test("merged source checkboxes start at the parent thumbnail start line", () => {
  assert.match(
    appCss,
    /\.merged-source-select-column\s*\{[^}]*width:\s*calc\(64px \+ var\(--space-5\) \+ 28px \+ var\(--space-7\) \+ 14px \+ var\(--space-6\)\);/,
  );
  assert.match(
    appCss,
    /\.merged-source-table td\.merged-source-select-cell\s*\{[^}]*padding:\s*0 0 0 calc\(64px \+ var\(--space-5\) \+ 28px \+ var\(--space-7\)\);\s*text-align:\s*left !important;/,
  );
  assert.doesNotMatch(
    appCss,
    /\.merged-source-table th:nth-child\(2\),\s*\.merged-source-table td:nth-child\(2\)\s*\{\s*padding-left/,
  );
});

test("merged parent and source selections expose mutual-exclusion reasons", () => {
  assert.match(
    projectsPage,
    /const selectedSourceIds = dataset\.source_datasets[\s\S]*const parentSelectionBlocked = selectedSourceIds\.length > 0/,
  );
  assert.match(
    projectsPage,
    /원본 데이터셋이 선택되어 병합 데이터셋을 선택할 수 없습니다\./,
  );
  assert.match(
    projectsPage,
    /병합 데이터셋이 선택되어 원본 데이터셋을 선택할 수 없습니다\./,
  );
  assert.match(projectsPage, /aria-describedby=\{parentSelectionBlocked/);
  assert.match(projectsPage, /aria-describedby=\{sourceSelectionBlocked/);
  assert.match(projectsPage, /title=\{parentSelectionBlocked/);
  assert.match(projectsPage, /title=\{sourceSelectionBlocked/);
});
