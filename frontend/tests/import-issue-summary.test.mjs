import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import ts from "typescript";

async function loadImportIssueSummary() {
  const source = await readFile(
    new URL("../src/utils/importIssueSummary.ts", import.meta.url),
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

const appCss = await readFile(
  new URL("../src/styles/app.css", import.meta.url),
  "utf8",
);

test("broken label issues count distinct files instead of invalid lines", async () => {
  const { getImportIssueSummary } = await loadImportIssueSummary();
  const summary = getImportIssueSummary([
    { kind: "broken_label", path: "labels/a.txt", detail: "line 1" },
    { kind: "broken_label", path: "labels/a.txt", detail: "line 2" },
    { kind: "broken_label", path: "labels/b.txt", detail: "line 4" },
    { kind: "broken_image", path: "images/broken.jpg", detail: "cannot decode" },
    { kind: "rejected_file", path: "notes.md", detail: "unsupported" },
    { kind: "rejected_file", path: "archive.exe", detail: "unsupported" },
  ]);

  assert.equal(summary.counts.get("broken_label"), 2);
  assert.equal(summary.counts.get("broken_image"), 1);
  assert.equal(summary.counts.get("rejected_file"), 2);
  assert.equal(summary.total, 5);
});

test("broken label paths are distinct per created dataset", async () => {
  const { getImportIssueSummary } = await loadImportIssueSummary();
  const summary = getImportIssueSummary([
    { kind: "broken_label", path: "labels/a.txt", detail: "line 1", summaryScope: "11" },
    { kind: "broken_label", path: "labels/a.txt", detail: "line 2", summaryScope: "11" },
    { kind: "broken_label", path: "labels/a.txt", detail: "line 1", summaryScope: "12" },
  ]);

  assert.equal(summary.counts.get("broken_label"), 2);
  assert.equal(summary.total, 2);
});

test("issue details group every reason by kind, dataset, and file path", async () => {
  const { groupImportIssueDetails } = await loadImportIssueSummary();
  const groups = groupImportIssueDetails([
    { kind: "broken_label", path: "labels/a.txt", detail: "line 1", summaryScope: "11" },
    { kind: "broken_label", path: "labels/a.txt", detail: "line 2", summaryScope: "11" },
    { kind: "broken_label", path: "labels/a.txt", detail: "line 1", summaryScope: "11" },
    { kind: "broken_label", path: "labels/a.txt", detail: "line 4", summaryScope: "12" },
    { kind: "broken_image", path: "labels/a.txt", detail: "cannot decode", summaryScope: "11" },
  ], "broken_label");

  assert.deepEqual(
    groups.map(({ summaryScope, path, details }) => ({ summaryScope, path, details })),
    [
      {
        summaryScope: "11",
        path: "labels/a.txt",
        details: ["line 1", "line 2"],
      },
      {
        summaryScope: "12",
        path: "labels/a.txt",
        details: ["line 4"],
      },
    ],
  );
});

test("issue rows expand accessible file details below the selected category", () => {
  const issueSection = uploadPage.slice(
    uploadPage.indexOf('<div className="issue-list">'),
    uploadPage.indexOf('<div className="upload-result-actions"'),
  );

  assert.match(uploadPage, /groupImportIssueDetails/);
  assert.match(uploadPage, /const \[expandedIssueKind, setExpandedIssueKind\] = useState<IssueKind \| null>\(null\)/);
  assert.match(issueSection, /const detailGroups = groupImportIssueDetails\(issues, kind\)/);
  assert.match(issueSection, /aria-expanded=\{expanded\}/);
  assert.match(issueSection, /aria-controls=\{detailsId\}/);
  assert.match(issueSection, /disabled=\{detailGroups\.length === 0\}/);
  assert.match(issueSection, /setExpandedIssueKind\(expanded \? null : kind\)/);
  assert.match(issueSection, /id=\{detailsId\}/);
  assert.match(issueSection, /role="region"/);
  assert.match(issueSection, /\{group\.path\}/);
  assert.match(issueSection, /\{detail\}/);
  assert.match(issueSection, /name=\{expanded \? "chevron-down" : "chevron-right"\}/);
  assert.doesNotMatch(issueSection, /title=\{issues\.find/);
  assert.match(appCss, /\.issue-details\s*\{[^}]*max-height:[^}]*overflow-y:\s*auto/);
  assert.match(appCss, /\.issue-detail-path\s*\{[^}]*overflow-wrap:\s*anywhere/);
});

test("the upload result labels the grouped count as broken label files", () => {
  assert.match(uploadPage, /image_without_label: "라벨 없는 이미지 파일"/);
  assert.match(uploadPage, /empty_label: "빈 라벨 파일"/);
  assert.match(uploadPage, /label_without_image: "이미지 없는 라벨 파일"/);
  assert.match(uploadPage, /broken_image: "깨진 이미지 파일"/);
  assert.match(uploadPage, /broken_label: "깨진 라벨 파일"/);
  assert.match(uploadPage, /duplicate_skipped: "중복 이미지"/);
  assert.match(uploadPage, /ignored_file: "사용하지 않은 파일"/);
  assert.match(uploadPage, /class_conflict: "클래스 오류"/);
  assert.match(uploadPage, /getImportIssueSummary\(issues\)/);
  assert.match(uploadPage, /issueSummary\.total\.toLocaleString\(\)/);
  assert.doesNotMatch(uploadPage, /깨진 라벨 줄/);
  assert.doesNotMatch(uploadPage, /중복 스킵/);
  assert.doesNotMatch(uploadPage, /대상 아닌 파일/);
  assert.doesNotMatch(uploadPage, /클래스 이름 충돌/);
  assert.ok(
    uploadPage.indexOf('label_without_image: "이미지 없는 라벨 파일"')
      < uploadPage.indexOf('broken_image: "깨진 이미지 파일"'),
  );
});

test("renamed image collisions are counted one row per image", async () => {
  const { getImportIssueSummary } = await loadImportIssueSummary();
  const summary = getImportIssueSummary([
    {
      kind: "duplicate_skipped",
      path: "camera_b/train/0001.png",
      detail: "stored as camera_b/train/0001 (1).png",
    },
    {
      kind: "duplicate_skipped",
      path: "camera_c/train/0001.jpg",
      detail: "stored as camera_c/train/0001 (2).jpg",
    },
  ]);

  assert.equal(summary.counts.get("duplicate_skipped"), 2);
  assert.equal(summary.total, 2);
});
