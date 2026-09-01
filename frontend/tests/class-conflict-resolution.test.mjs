import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import ts from "typescript";

async function loadClassResolutionPreferences() {
  const source = await readFile(
    new URL("../src/utils/classResolutionPreferences.ts", import.meta.url),
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
const uploadApi = await readFile(
  new URL("../src/api/upload.ts", import.meta.url),
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

async function readResolutionPanel() {
  return readFile(
    new URL("../src/components/ClassConflictResolutionPanel.tsx", import.meta.url),
    "utf8",
  );
}

test("class conflicts pause the same upload job before dataset writes", () => {
  assert.match(client, /export type JobState =[\s\S]*"awaiting_class_resolution"/);
  assert.match(client, /export interface ClassResolutionPlan/);
  assert.match(client, /export function resolveJobClassConflicts/);
  assert.match(client, /`\/api\/jobs\/\$\{jobId\}\/class-resolution`/);
  assert.match(uploadApi, /job\.state === "awaiting_class_resolution"/);
  assert.match(uploadPage, /setPendingClassResolution\(terminal\.class_resolution\)/);
  assert.match(uploadPage, /terminal\.state === "awaiting_class_resolution"/);
  assert.match(uploadPage, /<ClassConflictResolutionPanel/);
  assert.match(uploadPage, /await resolveJobClassConflicts/);
  assert.match(uploadPage, /void startUpload\(\)/);

  const pauseIndex = uploadPage.indexOf(
    'while (terminal.state === "awaiting_class_resolution")',
  );
  const completionIndex = uploadPage.indexOf("completedWork += unitBytes;");
  assert.ok(pauseIndex >= 0 && pauseIndex < completionIndex);
  assert.match(uploadPage.slice(pauseIndex, completionIndex), /return;/);
  assert.doesNotMatch(uploadPage.slice(pauseIndex, completionIndex), /setDone\(true\)/);
  assert.match(
    uploadPage,
    /const canNavigateAfterUpload = done && percentage === 100 && labelingDataset !== null/,
  );
});

test("one choice is reused only for the same class-name mismatch", async () => {
  const {
    rememberClassResolutions,
    resolutionsFromPreferences,
  } = await loadClassResolutionPreferences();
  const firstPlan = {
    revision: "first",
    conflicts: [{
      key: "class:1",
      class_id: 1,
      source_path: "first/data.yaml",
      project_name: "forklift",
      uploaded_name: "orklift",
    }],
  };
  const repeatedPlan = {
    revision: "second",
    conflicts: [{
      ...firstPlan.conflicts[0],
      source_path: "second/data.yaml",
    }],
  };
  const differentPlan = {
    revision: "third",
    conflicts: [{
      ...firstPlan.conflicts[0],
      source_path: "third/data.yaml",
      uploaded_name: "lift",
    }],
  };
  const preferences = rememberClassResolutions({}, firstPlan, [
    { key: "class:1", action: "use_project" },
  ]);

  for (let index = 0; index < 12; index += 1) {
    assert.deepEqual(resolutionsFromPreferences(repeatedPlan, preferences), [
      { key: "class:1", action: "use_project" },
    ]);
  }
  assert.equal(resolutionsFromPreferences(differentPlan, preferences), null);
});

test("a plan waits for confirmation when even one conflict has no remembered choice", async () => {
  const {
    rememberClassResolutions,
    resolutionsFromPreferences,
  } = await loadClassResolutionPreferences();
  const firstPlan = {
    revision: "first",
    conflicts: [{
      key: "class:1",
      class_id: 1,
      source_path: "data.yaml",
      project_name: "forklift",
      uploaded_name: "orklift",
    }],
  };
  const mixedPlan = {
    revision: "mixed",
    conflicts: [
      firstPlan.conflicts[0],
      {
        key: "class:2",
        class_id: 2,
        source_path: "data.yaml",
        project_name: "person",
        uploaded_name: "worker",
      },
    ],
  };
  const preferences = rememberClassResolutions({}, firstPlan, [
    { key: "class:1", action: "use_upload" },
  ]);

  assert.equal(resolutionsFromPreferences(mixedPlan, preferences), null);
});

test("the upload loop automatically submits fully remembered conflicts", () => {
  assert.match(uploadPage, /const classResolutionPreferencesRef = useRef/);
  assert.match(uploadPage, /resolutionsFromPreferences\([\s\S]*terminal\.class_resolution/);
  assert.match(uploadPage, /while \(terminal\.state === "awaiting_class_resolution"\)/);
  assert.match(uploadPage, /await resolveJobClassConflicts\(activeJobId/);
  assert.match(uploadPage, /자동 적용/);
});

test("the inline resolver offers the two canonical naming choices", async () => {
  const panel = await readResolutionPanel();

  assert.match(panel, /클래스 명칭 확인이 필요합니다/);
  assert.match(panel, /<fieldset/);
  assert.match(panel, /<legend/);
  assert.match(panel, /type="radio"/);
  assert.match(panel, /value="use_project"/);
  assert.match(panel, /initialChoices/);
  assert.match(panel, /업로드 클래스명 수정/);
  assert.match(panel, /프로젝트 클래스명을 기준으로 이 데이터셋에만 적용/);
  assert.match(panel, /value="use_upload"/);
  assert.match(panel, /프로젝트 클래스명 수정/);
  assert.match(panel, /기존 데이터셋.*에도 적용/);
  assert.match(panel, /선택한 이름으로 업로드 계속/);
  assert.match(panel, /같은 클래스명 차이에는 선택이 자동으로 적용/);
  assert.match(panel, /aria-live="polite"/);
  assert.match(panel, /role="alert"/);
  assert.match(panel, /\.focus\(\)/);
  assert.doesNotMatch(panel, /role="dialog"|aria-modal/);
});

test("class resolution UI follows the existing upload option-card tokens", () => {
  assert.match(appCss, /\.class-resolution-panel\s*\{/);
  assert.match(appCss, /\.class-resolution-option\s*\{/);
  assert.match(appCss, /\.class-resolution-option:has\(input:checked\)/);
  assert.match(appCss, /\.class-conflict-comparison\s*\{/);
  assert.match(appCss, /var\(--color-accent-weak\)/);
});
