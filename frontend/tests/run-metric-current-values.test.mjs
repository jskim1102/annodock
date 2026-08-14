import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import ts from "typescript";

async function loadRunMetricChart() {
  const source = await readFile(
    new URL("../src/utils/runMetricChart.ts", import.meta.url),
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

const runDetailPage = await readFile(
  new URL("../src/pages/RunDetailPage.tsx", import.meta.url),
  "utf8",
);
const appCss = await readFile(
  new URL("../src/styles/app.css", import.meta.url),
  "utf8",
);

test("current values use the last finite metric and a stable four-decimal format", async () => {
  const { formatMetricValue, latestMetricValue } = await loadRunMetricChart();
  const metrics = [
    { box_loss: 0.81234 },
    { box_loss: null },
    { box_loss: Number.NaN },
  ];

  assert.equal(latestMetricValue(metrics, "box_loss"), 0.81234);
  assert.equal(latestMetricValue(metrics, "cls_loss"), null);
  assert.equal(formatMetricValue(0.81234), "0.8123");
  assert.equal(formatMetricValue(0), "0.0000");
});

test("nearby current values are separated without leaving the chart", async () => {
  const { positionMetricValueLabels } = await loadRunMetricChart();
  const positioned = positionMetricValueLabels([
    { key: "box", y: 50 },
    { key: "cls", y: 52 },
    { key: "dfl", y: 54 },
  ], 10, 110, 12);
  const sorted = positioned.toSorted((left, right) => left.labelY - right.labelY);

  assert.ok(sorted[0].labelY >= 10);
  assert.ok(sorted.at(-1).labelY <= 110);
  assert.ok(sorted[1].labelY - sorted[0].labelY >= 12);
  assert.ok(sorted[2].labelY - sorted[1].labelY >= 12);
});

test("metric cards expose a title, explanation, legend, and latest values", () => {
  assert.match(runDetailPage, /Training loss by epoch/);
  assert.match(runDetailPage, /Validation mAP by epoch/);
  assert.match(runDetailPage, /className="chart-latest"/);
  assert.match(runDetailPage, />Latest</);
  assert.match(runDetailPage, /className="chart-latest-value"/);
  assert.match(runDetailPage, /formatMetricValue\(currentValue\)/);
});

test("each metric line renders axes, its latest point, and a right-edge value badge", () => {
  assert.match(runDetailPage, /const CHART_PLOT_LEFT = 38;/);
  assert.match(runDetailPage, /const CHART_PLOT_RIGHT = 374;/);
  assert.match(runDetailPage, /const CHART_VIEW_WIDTH = 440;/);
  assert.match(runDetailPage, /viewBox="0 0 440 190"/);
  assert.match(runDetailPage, /className="chart-grid-line"/);
  assert.match(runDetailPage, /className="chart-axis-label"/);
  assert.match(runDetailPage, /className="chart-current-point"/);
  assert.match(runDetailPage, /className="chart-current-badge"/);
  assert.match(runDetailPage, /className="chart-current-value"/);
  assert.match(runDetailPage, /fill=\{line\.color\}/);
  assert.match(runDetailPage, /formatMetricValue\(current\.value\)/);
});

test("chart hierarchy and current value badges remain visually distinct", () => {
  const cardRule = appCss.match(/\.chart-card\s*\{(?<body>[\s\S]*?)\}/)?.groups?.body ?? "";
  const legendRule = appCss.match(/\.chart-legend\s*\{(?<body>[\s\S]*?)\}/)?.groups?.body ?? "";
  const latestRule = appCss.match(/\.chart-latest-values\s*\{(?<body>[\s\S]*?)\}/)?.groups?.body ?? "";
  const gridRule = appCss.match(/\.metric-chart \.chart-grid-line\s*\{(?<body>[\s\S]*?)\}/)?.groups?.body ?? "";
  const badgeRule = appCss.match(/\.chart-current-badge\s*\{(?<body>[\s\S]*?)\}/)?.groups?.body ?? "";
  const rule = appCss.match(/\.chart-current-value\s*\{(?<body>[\s\S]*?)\}/)?.groups?.body ?? "";

  assert.match(cardRule, /padding:\s*var\(--space-7\)/);
  assert.match(legendRule, /border:\s*1px solid var\(--color-divider\)/);
  assert.match(latestRule, /display:\s*flex/);
  assert.match(gridRule, /stroke-dasharray:/);
  assert.match(badgeRule, /fill:\s*var\(--color-bg\)/);
  assert.match(rule, /font-size:\s*7px/);
  assert.match(rule, /font-weight:\s*600/);
});
