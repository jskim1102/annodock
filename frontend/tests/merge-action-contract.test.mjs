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

test("the API client merges every selected source dataset", () => {
  assert.match(client, /export interface DatasetMergeInput/);
  assert.match(client, /dataset_ids:\s*number\[\]/);
  assert.match(client, /export function mergeDatasets/);
  assert.match(
    client,
    /requestJson<DatasetListItem>\("\/api\/datasets\/merge",\s*jsonInit\("POST", input\)\)/,
  );
  assert.match(client, /export function extendMergedDataset/);
  assert.match(
    client,
    /`\/api\/datasets\/\$\{mergedDatasetId\}\/merge-sources`/,
  );
});

test("project actions keep the zero, one, and many selection paths explicit", () => {
  assert.match(projectsPage, /selectedRows\.length === 0/);
  assert.match(projectsPage, /selectedRows\.length === 1/);
  assert.match(projectsPage, /selectedRows\.length < 2/);
  assert.match(
    projectsPage,
    /appHref\(`\/datasets\/\$\{selectedRows\[0\]\.id\}\/train`\)/,
  );
  assert.match(projectsPage, /onMergeAction\("train"\)/);
  assert.match(projectsPage, /onMergeAction\("merge"\)/);
  assert.doesNotMatch(projectsPage, /onMergeAction\("export"\)/);
  assert.doesNotMatch(projectsPage, /for \(const dataset of datasets\)/);
});

test("multi-selection opens an accessible merge-name dialog with every source id", () => {
  assert.match(projectsPage, /function MergeDatasetsDialog/);
  assert.match(projectsPage, /role="dialog"/);
  assert.match(projectsPage, /aria-modal="true"/);
  assert.match(projectsPage, /htmlFor="merge-dataset-name"/);
  assert.match(projectsPage, /id="merge-dataset-name"/);
  assert.match(projectsPage, /dataset_ids:\s*target\.datasets\.map\(\(dataset\) => dataset\.id\)/);
  assert.match(
    projectsPage,
    /disabled=\{busy \|\| \(extendsExisting \? targetDatasetId === null : !normalizedName\)\}/,
  );
  assert.match(appCss, /\.merge-dataset-list/);
});

test("one selected merged dataset becomes the fixed append target", () => {
  assert.match(projectsPage, /const mergedDatasets = target\.datasets\.filter\(\(dataset\) => dataset\.is_merged\)/);
  assert.match(projectsPage, /mergedDatasets\.length === 1/);
  assert.match(projectsPage, /target\.datasets[\s\S]*?\.filter\(\(dataset\) => dataset\.id !== targetDataset\.id\)/);
  assert.match(projectsPage, /await extendMergedDataset\(targetDataset\.id, \{/);
  assert.match(
    projectsPage,
    /const continueMergedAction[\s\S]*?await syncProjectsAfterDatasetMutation\(\);[\s\S]*?if \(purpose === "train"\) navigate/,
  );
  assert.match(projectsPage, /<strong>\{targetDataset\.name\}<\/strong>에 추가/);
  assert.doesNotMatch(projectsPage, /병합 데이터셋은 다른 데이터셋과 다시 합칠 수 없습니다/);
});

test("multiple merged datasets require an explicit target radio choice", () => {
  assert.match(projectsPage, /mergedDatasets\.length === 1[\s\S]*?병합 데이터셋 통합/);
  assert.match(projectsPage, /type="radio"/);
  assert.match(projectsPage, /name="merge-target-dataset"/);
  assert.match(projectsPage, /checked=\{targetDatasetId === dataset\.id\}/);
  assert.match(projectsPage, /병합 결과를 유지할 대상/);
  assert.match(projectsPage, /나머지 병합 데이터셋은 대상에 통합된 뒤 삭제됩니다/);
});

test("a merged result drives one train route or a plain merge", () => {
  assert.match(projectsPage, /await mergeDatasets/);
  assert.match(projectsPage, /continueMergedAction\(merged\.id, target\.purpose\)/);
  assert.match(projectsPage, /navigate\(`\/datasets\/\$\{datasetId\}\/train`\)/);
  assert.doesNotMatch(projectsPage, /startDatasetExport/);
});

test("partial overlap is explicit and recoverable", () => {
  assert.match(projectsPage, /mergeConflictFrom/);
  assert.match(projectsPage, /기존 병합으로 진행/);
  assert.match(projectsPage, /dataset_merge_source_overlap/);
  assert.match(projectsPage, /merged_dataset/);
  assert.match(projectsPage, /source_dataset_ids/);
  assert.match(projectsPage, /role="alert"/);
  assert.match(projectsPage, /finally\s*\{\s*setMergeBusy\(false\)/);
});
