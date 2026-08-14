import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import ts from "typescript";

async function loadProjectSummary() {
  const source = await readFile(
    new URL("../src/utils/projectSummary.ts", import.meta.url),
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

test("project summary counts real lifecycle states", async () => {
  const { getProjectSummary } = await loadProjectSummary();
  const rows = [
    { archived: false, datasets: [] },
    { archived: false, datasets: [{ status: "pending", active_job: null }] },
    { archived: false, datasets: [{ status: "ready", active_job: null }] },
    { archived: true, datasets: [{ status: "ready", active_job: null }] },
    { archived: false, datasets: [{ status: "failed", active_job: null }] },
  ];

  assert.deepEqual(getProjectSummary(rows), {
    total: 5,
    inProgress: 1,
    completed: 1,
    archived: 1,
  });
});
