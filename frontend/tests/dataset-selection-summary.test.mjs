import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import ts from "typescript";

async function loadDatasetSelectionSummary() {
  const source = await readFile(
    new URL("../src/utils/datasetSelectionSummary.ts", import.meta.url),
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

test("selection totals stay hidden until a dataset is selected", async () => {
  const { getDatasetSelectionSummary } = await loadDatasetSelectionSummary();

  assert.equal(getDatasetSelectionSummary([], 8), null);
});

test("selection totals sum images without double-counting the shared class catalog", async () => {
  const { getDatasetSelectionSummary } = await loadDatasetSelectionSummary();
  const selectedDatasets = [
    { image_count: 91, class_count: 2 },
    { image_count: 75, class_count: 2 },
  ];

  assert.deepEqual(getDatasetSelectionSummary(selectedDatasets, 2), {
    datasetCount: 2,
    imageCount: 166,
    classCount: 2,
  });
});

test("merged source datasets join the same selectable row collection", async () => {
  const { getProjectSelectionRows } = await loadDatasetSelectionSummary();
  const rows = getProjectSelectionRows([
    {
      id: 10,
      image_count: 30,
      source_datasets: [
        { id: 11, image_count: 12 },
        { id: 12, image_count: 18 },
      ],
    },
    { id: 20, image_count: 7, source_datasets: [] },
  ]);

  assert.deepEqual(rows.map((row) => row.id), [10, 11, 12, 20]);
  assert.deepEqual(rows[1].source_datasets, []);
  assert.deepEqual(rows[2].source_datasets, []);
});

test("selecting a merged dataset or one of its sources clears the other side", async () => {
  const { toggleDatasetSelection } = await loadDatasetSelectionSummary();

  assert.deepEqual(
    [...toggleDatasetSelection(new Set([11, 12]), 10, [11, 12])],
    [10],
  );
  assert.deepEqual(
    [...toggleDatasetSelection(new Set([10]), 11, [10])],
    [11],
  );
  assert.deepEqual(
    [...toggleDatasetSelection(new Set([11]), 11, [10])],
    [],
  );
});
