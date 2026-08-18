import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const projectsPage = await readFile(
  new URL("../src/pages/ProjectsPage.tsx", import.meta.url),
  "utf8",
);
const newProjectDialog = await readFile(
  new URL("../src/components/NewProjectDialog.tsx", import.meta.url),
  "utf8",
);

function functionBody(source, name, nextName) {
  const start = source.indexOf(`function ${name}`);
  const end = nextName === null ? source.length : source.indexOf(`function ${nextName}`, start + 1);
  assert.notEqual(start, -1, `${name} must exist`);
  assert.notEqual(end, -1, `${nextName ?? "end of file"} must follow ${name}`);
  return source.slice(start, end);
}

test("project and dataset dialogs focus once without weakening Escape handling", () => {
  const cases = [
    ["RenameProjectDialog", "DeleteProjectDialog", /inputRef\.current\?\.focus\(\);[\s\S]*?inputRef\.current\?\.select\(\);[\s\S]*?\}, \[\]\);/],
    ["DeleteProjectDialog", "RenameDatasetDialog", /cancelRef\.current\?\.focus\(\);[\s\S]*?\}, \[\]\);/],
    ["RenameDatasetDialog", "DeleteDatasetDialog", /inputRef\.current\?\.focus\(\);[\s\S]*?inputRef\.current\?\.select\(\);[\s\S]*?\}, \[\]\);/],
    ["DeleteDatasetDialog", null, /cancelRef\.current\?\.focus\(\);[\s\S]*?\}, \[\]\);/],
  ];

  for (const [name, nextName, focusPattern] of cases) {
    const body = functionBody(projectsPage, name, nextName);
    assert.match(body, focusPattern, `${name} initial focus must be mount-only`);
    assert.match(body, /window\.addEventListener\("keydown", closeOnEscape\)[\s\S]*?\}, \[busy, onClose\]\);/);
  }
});

test("new-project focus is mount-only and Escape remains subscribed to onClose", () => {
  assert.match(newProjectDialog, /nameRef\.current\?\.focus\(\);\s*\}, \[\]\);/);
  assert.match(
    newProjectDialog,
    /window\.addEventListener\("keydown", onKeyDown\)[\s\S]*?\}, \[onClose\]\);/,
  );
});
