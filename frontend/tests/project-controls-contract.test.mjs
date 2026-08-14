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

test("project row menu exposes rename and delete controls", () => {
  assert.match(projectsPage, /role="menu"/);
  assert.match(projectsPage, />이름 변경</);
  assert.match(projectsPage, />삭제</);
  assert.match(projectsPage, /renameProject/);
  assert.match(projectsPage, /deleteProject/);
});

test("project API client follows the PATCH and confirmed DELETE contract", () => {
  assert.match(client, /export function renameProject/);
  assert.match(client, /jsonInit\("PATCH", \{ name \}\)/);
  assert.match(client, /export function deleteProject/);
  assert.match(client, /confirm=true/);
});

test("dataset rows expose a confirmed delete flow backed by the dataset DELETE API", () => {
  assert.match(client, /export function deleteDataset\(datasetId: number\): Promise<void>/);
  assert.match(client, /`\/api\/datasets\/\$\{datasetId\}`[\s\S]*jsonInit\("DELETE"\)/);
  assert.match(projectsPage, /function DatasetRowMenu/);
  assert.match(projectsPage, /aria-haspopup="menu"/);
  assert.match(projectsPage, /aria-controls=\{`dataset-row-menu-\$\{datasetId\}`\}/);
  assert.match(projectsPage, /function DeleteDatasetDialog/);
  assert.match(
    projectsPage,
    /await deleteDataset\(dataset\.id\)[\s\S]*?const response = await getProjects\(\)/,
  );
  assert.match(projectsPage, /next\.delete\(dataset\.id\)/);
  assert.match(projectsPage, /const liveDatasetIds = new Set/);
  assert.match(projectsPage, /setProjects\(response\.items\)/);
  assert.match(projectsPage, /liveDatasetIds\.has\(datasetId\)/);
  assert.match(projectsPage, /데이터셋 삭제/);
  assert.match(projectsPage, /이 작업은 되돌릴 수 없습니다/);
  assert.match(projectsPage, /aria-describedby="delete-dataset-warning"/);
  assert.match(projectsPage, /error \? <div className="error-text project-dialog-error" role="alert"/);
  assert.match(projectsPage, /완료된 학습 기록과 산출물은 유지/);
});

test("non-empty project deletion requires a destructive confirmation dialog", () => {
  assert.match(projectsPage, /requires_confirmation/);
  assert.match(projectsPage, /되돌릴 수 없습니다/);
  assert.match(projectsPage, /deleteConfirmation\.datasets\.map/);
  assert.match(projectsPage, /aria-modal="true"/);
});
