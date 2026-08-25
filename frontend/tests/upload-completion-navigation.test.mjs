import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const uploadPage = await readFile(
  new URL("../src/pages/UploadPage.tsx", import.meta.url),
  "utf8",
);

const appCss = await readFile(
  new URL("../src/styles/app.css", import.meta.url),
  "utf8",
);

test("project and labeling destinations stay below the issue results", () => {
  assert.match(uploadPage, /setDone\(true\);/);
  assert.match(
    uploadPage,
    /const labelingDataset = finalDataset;/,
  );
  assert.match(
    uploadPage,
    /<div className="issue-list">[\s\S]*<div className="upload-result-actions"/,
  );
  assert.match(uploadPage, /href=\{appHref\("\/projects"\)\}>프로젝트<\/a>/);
  assert.match(
    uploadPage,
    /href=\{appHref\(`\/datasets\/\$\{labelingDataset\.id\}\/viewer`\)\}/,
  );
  assert.match(uploadPage, />라벨링<\/a>/);
  assert.doesNotMatch(uploadPage, /upload-complete-dialog/);
  assert.doesNotMatch(uploadPage, /completeProjectLinkRef/);
  assert.doesNotMatch(uploadPage, /dialog-backdrop/);
  assert.doesNotMatch(uploadPage, /어떤 페이지로 이동하시겠습니까\?/);
});

test("destination buttons enable only after the whole upload completes", () => {
  assert.match(
    uploadPage,
    /const percentage = done[\s\S]*?\? 100[\s\S]*?: Math\.min\(99\.9, measuredPercentage\)/,
  );
  assert.match(
    uploadPage,
    /const canNavigateAfterUpload = done && percentage === 100 && labelingDataset !== null;/,
  );
  assert.match(
    uploadPage,
    /canNavigateAfterUpload \? \([\s\S]*href=\{appHref\("\/projects"\)\}[\s\S]*labelingDataset\.id[\s\S]*\) : \([\s\S]*<button className="btn btn-secondary" type="button" disabled>프로젝트<\/button>[\s\S]*<button className="btn btn-primary" type="button" disabled>라벨링<\/button>/,
  );
  assert.match(appCss, /\.upload-result-actions\s*\{[\s\S]*grid-template-columns:\s*repeat\(2, minmax\(0, 1fr\)\)/);
});

test("the drop zone shows live collection and upload progress", () => {
  assert.match(uploadPage, /collectDroppedSources\(event\.dataTransfer, setCollectionProgress\)/);
  assert.match(uploadPage, /collectionProgress\.treePercentage/);
  assert.match(uploadPage, /collectionProgress\.filePercentage/);
  assert.match(uploadPage, />폴더 트리 검색<\/span>/);
  assert.match(uploadPage, />실제 파일 읽기<\/span>/);
  assert.match(uploadPage, /className="drop-live-progress"/);
  assert.match(uploadPage, /role="progressbar"/);
  assert.match(uploadPage, /aria-valuenow=\{collectionProgress\.treePercentage\}/);
  assert.match(uploadPage, /aria-valuenow=\{collectionProgress\.filePercentage\}/);
  assert.ok(uploadPage.includes(
    'style={{ width: `${collectionProgress.treePercentage}%` }}',
  ));
  assert.ok(uploadPage.includes(
    'style={{ width: `${collectionProgress.filePercentage}%` }}',
  ));
  assert.match(uploadPage, /\{collecting \? \(/);
  assert.match(uploadPage, /\) : busy \|\| done \? \(/);
  assert.match(appCss, /\.drop-live-progress \.bar/);
  assert.doesNotMatch(appCss, /\.drop-live-progress \.bar\.is-indeterminate > i/);
  assert.doesNotMatch(uploadPage, />확인 중<\/strong>/);
});
