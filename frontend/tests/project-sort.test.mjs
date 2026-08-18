import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import ts from "typescript";

async function loadProjectSort() {
  const source = await readFile(
    new URL("../src/utils/projectSort.ts", import.meta.url),
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

const projectsPage = await readFile(
  new URL("../src/pages/ProjectsPage.tsx", import.meta.url),
  "utf8",
);

const projects = [
  { id: 3, name: "하역", created_at: "2026-08-02T00:00:00Z" },
  { id: 1, name: "가공 10", created_at: "2026-08-03T00:00:00Z" },
  { id: 2, name: "가공 2", created_at: "2026-08-03T00:00:00Z" },
];

test("recent project sorting is descending and deterministic", async () => {
  const { sortProjects } = await loadProjectSort();
  const original = [...projects];

  assert.deepEqual(sortProjects(projects, "recent").map((project) => project.id), [2, 1, 3]);
  assert.deepEqual(projects, original);
});

test("name sorting uses Korean-aware natural ascending order", async () => {
  const { sortProjects } = await loadProjectSort();

  assert.deepEqual(sortProjects(projects, "name").map((project) => project.id), [2, 1, 3]);
});

test("the project sort menu is controlled and orders the filtered rows", () => {
  assert.match(projectsPage, /const \[sortOrder, setSortOrder\] = useState<ProjectSortOrder>\("recent"\)/);
  assert.match(projectsPage, /return sortProjects\(filtered, sortOrder\)/);
  assert.match(
    projectsPage,
    /<SelectMenu className="sort-select" value=\{sortOrder\} onChange=\{\(value\) => setSortOrder\(value as ProjectSortOrder\)\}/,
  );
  assert.doesNotMatch(projectsPage, /className="sort-select" defaultValue=/);
});
