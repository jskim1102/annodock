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
    /const canNavigateAfterUpload = done && labelingDataset !== null;/,
  );
  assert.match(
    uploadPage,
    /canNavigateAfterUpload \? \([\s\S]*href=\{appHref\("\/projects"\)\}[\s\S]*labelingDataset\.id[\s\S]*\) : \([\s\S]*<button className="btn btn-secondary" type="button" disabled>프로젝트<\/button>[\s\S]*<button className="btn btn-primary" type="button" disabled>라벨링<\/button>/,
  );
  assert.match(appCss, /\.upload-result-actions\s*\{[\s\S]*grid-template-columns:\s*repeat\(2, minmax\(0, 1fr\)\)/);
});
