import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import ts from "typescript";

async function loadUploadGrouping() {
  const source = await readFile(
    new URL("../src/utils/uploadGrouping.ts", import.meta.url),
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
const uploadGrouping = await readFile(
  new URL("../src/utils/uploadGrouping.ts", import.meta.url),
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

function item(name, kind = "image") {
  return {
    relPath: name,
    kind,
    file: { name, size: 1, lastModified: 1 },
  };
}

test("one upload source creates one dataset with the entered name", async () => {
  const { createUploadPlan } = await loadUploadGrouping();
  const sources = [
    { key: "folder:1", name: "train", kind: "folder", files: [item("train/a.jpg")] },
  ];

  const plan = createUploadPlan(sources, "release", []);

  assert.equal(plan.length, 1);
  assert.equal(plan[0].key, "folder:1");
  assert.equal(plan[0].name, "release");
  assert.deepEqual(
    plan[0].batches.map((batch) => batch.map((file) => file.relPath)),
    [["train/a.jpg"]],
  );
});

test("multiple upload sources create uniquely named source datasets for automatic merge", async () => {
  const { createUploadPlan } = await loadUploadGrouping();
  const sources = [
    { key: "folder:1", name: "train", kind: "folder", files: [item("train/a.jpg")] },
    { key: "folder:2", name: "train", kind: "folder", files: [item("train/b.jpg")] },
    { key: "zip:3", name: "labels", kind: "zip", files: [item("labels.zip", "zip")] },
    { key: "folder:4", name: "notes", kind: "folder", files: [item("notes/readme.md", "other")] },
  ];

  const plan = createUploadPlan(sources, "train", []);

  assert.deepEqual(plan.map((unit) => unit.key), ["folder:1", "folder:2", "zip:3"]);
  assert.deepEqual(plan.map((unit) => unit.name), ["train (2)", "train (3)", "labels"]);
  assert.deepEqual(
    plan.map((unit) => unit.batches.map((batch) => batch.map((file) => file.relPath))),
    [[["train/a.jpg"]], [["train/b.jpg"]], [["labels.zip"]]],
  );
});

test("folder roots and zip files remain separate upload sources", async () => {
  const { groupInputFiles } = await loadUploadGrouping();
  const folderSources = groupInputFiles([
    item("train/images/a.jpg"),
    item("train/labels/a.txt", "label"),
    item("valid/images/b.jpg"),
  ], "folder");
  const fileSources = groupInputFiles([
    item("release-one.zip", "zip"),
    item("release-two.zip", "zip"),
    item("notes.md", "other"),
    item("loose.jpg"),
    item("loose.txt", "label"),
  ], "files");

  assert.deepEqual(folderSources.map((source) => source.name), ["train", "valid"]);
  assert.deepEqual(folderSources.map((source) => source.kind), ["folder", "folder"]);
  assert.deepEqual(fileSources.map((source) => source.name), ["release-one", "release-two", "loose"]);
  assert.deepEqual(fileSources.map((source) => source.kind), ["zip", "zip", "files"]);
  assert.deepEqual(
    fileSources.at(-1).files.map((file) => file.relPath),
    ["notes.md", "loose.jpg", "loose.txt"],
  );
});

test("dataset name is suggested only when exactly one upload source is selected", async () => {
  const { suggestedDatasetName } = await loadUploadGrouping();
  const folder = {
    key: "folder:1",
    name: "train",
    kind: "folder",
    files: [item("train/a.jpg")],
  };
  const zip = {
    key: "zip:2",
    name: "release",
    kind: "zip",
    files: [item("release.zip", "zip")],
  };
  const ignored = {
    key: "folder:3",
    name: "notes",
    kind: "folder",
    files: [item("notes/readme.md", "other")],
  };

  assert.equal(suggestedDatasetName([folder]), "train");
  assert.equal(suggestedDatasetName([zip]), "release");
  assert.equal(suggestedDatasetName([folder, zip]), "");
  assert.equal(suggestedDatasetName([folder, ignored]), "train");
  assert.equal(suggestedDatasetName([ignored]), "");
});

test("generated dataset names stay within the backend limit when suffixes are added", async () => {
  const { createUploadPlan, datasetNameWithSuffix } = await loadUploadGrouping();
  const longName = "가".repeat(255);
  const plan = createUploadPlan(
    [
      { key: "folder:1", name: longName, kind: "folder", files: [item("a.jpg")] },
      { key: "folder:2", name: longName, kind: "folder", files: [item("b.jpg")] },
    ],
    longName,
    [longName],
  );

  assert.equal(plan[0].name.length, 255);
  assert.equal(plan[1].name.length, 255);
  assert.match(plan[0].name, / \(2\)$/);
  assert.match(plan[1].name, / \(3\)$/);
  assert.equal(datasetNameWithSuffix(longName, 12).length, 255);
  assert.match(datasetNameWithSuffix(longName, 12), / \(12\)$/);

  const single = createUploadPlan(
    [{ key: "folder:1", name: "source", kind: "folder", files: [item("a.jpg")] }],
    `${longName}overflow`,
    [],
  );
  assert.equal(single[0].name.length, 255);
});

test("the upload page chooses grouping automatically and uploads each source sequentially", () => {
  assert.doesNotMatch(uploadGrouping, /export type UploadMode/);
  assert.doesNotMatch(uploadPage, /role="radiogroup" aria-label="다중 항목 업로드 방식"/);
  assert.doesNotMatch(uploadPage, /하나의 데이터셋으로 업로드/);
  assert.doesNotMatch(uploadPage, /각각 다른 데이터셋으로 업로드/);
  assert.doesNotMatch(uploadPage, /uploadMode/);
  assert.match(uploadPage, /setDatasetName\(suggestedDatasetName\(sources\)\)/);
  assert.match(uploadPage, /value=\{datasetName\}/);
  assert.match(uploadPage, /maxLength=\{255\}/);
  assert.match(uploadPage, /const datasetNameMissing = uploadableSourceCount > 0 && !datasetName\.trim\(\)/);
  assert.match(uploadPage, /className=\{`input\$\{datasetNameMissing \? " is-error" : ""\}`\}/);
  assert.match(uploadPage, /aria-invalid=\{datasetNameMissing\}/);
  assert.match(uploadPage, /aria-describedby="dataset-name-hint"/);
  assert.match(uploadPage, /datasetNameMissing \? "error-text" : "hint"/);
  assert.match(uploadPage, /병합 데이터셋 이름을 입력해야 업로드를 시작할 수 있습니다\./);
  assert.match(uploadPage, /개 항목을 각각 업로드한 뒤 이 이름으로 자동 병합합니다/);
  assert.match(uploadPage, /for \(const unit of uploadPlan\)/);
  assert.match(uploadPage, /createDatasetForUpload\(candidateName, project\.id\)/);
  assert.match(uploadPage, /reason instanceof ApiError && reason\.status === 409/);
  assert.match(uploadPage, /prepareUploadBatch\(targetId, batchFiles\)/);
  assert.match(uploadPage, /const targetIds = \{ \.\.\.uploadTargets \}/);
  assert.match(uploadPage, /let targetId = targetIds\[unit\.key\]/);
  assert.match(uploadPage, /setUploadTargets\(\{ \.\.\.targetIds \}\)/);
  assert.match(uploadPage, /const \[completedBatchCounts, setCompletedBatchCounts\]/);
  assert.match(uploadPage, /const batchStartIndex = completedBatchCounts\[unit\.key\] \?\? 0/);
  assert.match(uploadPage, /unit\.batches\.slice\(batchStartIndex\)/);
  assert.match(uploadPage, /setCompletedBatchCounts/);
  assert.match(uploadPage, /const completedUploadItemCount = uploadPlan\.reduce/);
  assert.match(uploadPage, /\(completedBatchCounts\[unit\.key\] \?\? 0\) >= unit\.batches\.length/);
  assert.match(uploadPage, /\{completedUploadItemCount\}\/\{uploadPlan\.length\}개 완료/);
  assert.match(uploadPage, /clearUploadBatchResume\(batch\)/);
  assert.match(uploadApi, /export function clearUploadBatchResume/);
  assert.match(uploadApi, /removeStoredValue\(upload\.resumeKey\)/);
  assert.match(uploadPage, /selectionLockedRef\.current = true/);
  assert.match(uploadPage, /if \(selectionLockedRef\.current\) return/);
  assert.match(uploadPage, /!busy && !hasUploadTargets && project !== null/);
  assert.match(uploadPage, /disabled=\{busy \|\| hasUploadTargets\}/);
  assert.match(uploadPage, /개 데이터셋은 완료되었습니다\./);
  assert.match(uploadPage, /개 항목은 이미 반영되었습니다\./);
  assert.match(uploadPage, /summaryScope: String\(targetId\)/);
  assert.match(uploadPage, /detail\.status !== "ready"/);
  assert.match(uploadPage, /uploadPlan\.length > 200/);
  assert.match(uploadPage, /const merged = await mergeDatasets\(\{/);
  assert.match(uploadPage, /dataset_ids: completed\.map\(\(dataset\) => dataset\.id\)/);
  assert.match(uploadPage, /setFinalDataset\(\{ id: merged\.id, name: merged\.name \}\)/);
  assert.match(uploadPage, /setStats\(\{[\s\S]*images: merged\.image_count,[\s\S]*annotations: merged\.annotation_count,[\s\S]*classes: merged\.class_count/);
  assert.doesNotMatch(appCss, /\.upload-mode-options/);
  assert.doesNotMatch(appCss, /\.upload-mode-option:has\(input:checked\)/);
});

test("uploads can append to one of the project's ready merged datasets", () => {
  assert.match(apiClient, /export function extendMergedDataset/);
  assert.match(uploadPage, /project\?\.datasets\.filter\([\s\S]*?dataset\.is_merged[\s\S]*?dataset\.status === "ready"/);
  assert.match(uploadPage, /const \[mergeIntoDatasetId, setMergeIntoDatasetId\] = useState<number \| null>\(null\)/);
  assert.match(uploadPage, /<fieldset[\s\S]*?className="upload-merge-target-fieldset"/);
  assert.match(uploadPage, /name="upload-merge-target"/);
  assert.match(uploadPage, /새 데이터셋으로 추가/);
  assert.match(uploadPage, /mergedUploadTargets\.map\(\(dataset\) =>/);
  assert.match(uploadPage, /checked=\{mergeIntoDatasetId === dataset\.id\}/);
  assert.match(uploadPage, /disabled=\{busy \|\| hasUploadTargets \|\| pendingClassResolution !== null\}/);
  assert.match(uploadPage, /const selectedMergedDataset = mergedUploadTargets\.find/);
  assert.match(uploadPage, /const merged = await extendMergedDataset\(selectedMergedDataset\.id, \{/);
  assert.match(uploadPage, /dataset_ids: completed\.map\(\(dataset\) => dataset\.id\)/);
  assert.match(uploadPage, /setFinalDataset\(\{ id: merged\.id, name: merged\.name \}\)/);
  assert.match(uploadPage, /기존 병합 데이터셋에 포함 완료/);
  assert.match(uploadPage, /\$\{selectedMergedDataset\.name\}에 포함합니다\./);
  assert.match(appCss, /\.upload-merge-target-fieldset\s*\{/);
  assert.match(appCss, /\.upload-merge-target-option:has\(input:checked\)/);
});

test("upload-created datasets remain hidden drafts until ingest succeeds", () => {
  assert.match(
    uploadApi,
    /body: JSON\.stringify\(\{ name, project_id: projectId, upload_draft: true \}\)/,
  );
});
