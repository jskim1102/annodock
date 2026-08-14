import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const trainPage = await readFile(
  new URL("../src/pages/TrainPage.tsx", import.meta.url),
  "utf8",
);
const appCss = await readFile(
  new URL("../src/styles/app.css", import.meta.url),
  "utf8",
);

test("training summary loads class image counts for its dataset", () => {
  assert.match(trainPage, /getProjectClassImageCounts/);
  assert.match(trainPage, /getProjectClassImageCounts\(detail\.project_id, \[datasetId\]\)/);
  assert.match(trainPage, /type ProjectClassImageCount/);
});

test("training summary presents class and total image counts as one stat panel", () => {
  const summaryStart = trainPage.indexOf('className="card training-summary-card"');
  const summaryEnd = trainPage.indexOf('className="training-actions"', summaryStart);
  const summaryMarkup = trainPage.slice(summaryStart, summaryEnd);

  assert.match(summaryMarkup, /className="training-class-image-list"/);
  assert.match(summaryMarkup, /className="training-class-stat"/);
  assert.match(summaryMarkup, /className="training-class-stat-heading"[\s\S]*<i[\s\S]*item\.name/);
  assert.match(summaryMarkup, /className="training-class-stat-value mono"/);
  assert.match(summaryMarkup, /item\.image_count\.toLocaleString\(\).*장/);
  assert.match(summaryMarkup, /className="training-class-stat is-total"/);
  assert.match(summaryMarkup, />전체 이미지</);
  assert.match(summaryMarkup, /selectedTotal\.toLocaleString\(\).*장/);
  assert.doesNotMatch(summaryMarkup, /합계 이미지[\s\S]*클래스/);
});

test("training image stats use equal centered columns with clear dividers", () => {
  const cardRule = appCss.match(/\.training-summary-card\s*\{([^}]*)\}/)?.[1] ?? "";
  const listRule = appCss.match(/\.training-class-image-list\s*\{([^}]*)\}/)?.[1] ?? "";
  const statRule = appCss.match(/\.training-class-stat\s*\{([^}]*)\}/)?.[1] ?? "";
  const dividedRule = appCss.match(/\.training-class-stat \+ \.training-class-stat\s*\{([^}]*)\}/)?.[1] ?? "";
  const headingRule = appCss.match(/\.training-class-stat-heading\s*\{([^}]*)\}/)?.[1] ?? "";
  const dotRule = appCss.match(/\.training-class-stat-heading i\s*\{([^}]*)\}/)?.[1] ?? "";
  const valueRule = appCss.match(/\.training-class-stat-value\s*\{([^}]*)\}/)?.[1] ?? "";
  const titleRule = appCss.match(/\.training-summary-card \.card-title\s*\{([^}]*)\}/)?.[1] ?? "";
  const splitTitleRule = appCss.match(/\.training-summary-card \.summary-kicker\s*\{([^}]*)\}/)?.[1] ?? "";

  assert.match(cardRule, /width:\s*400px/);
  assert.match(listRule, /display:\s*grid/);
  assert.match(listRule, /grid-auto-flow:\s*column/);
  assert.match(listRule, /grid-auto-columns:\s*minmax\(96px, 1fr\)/);
  assert.match(listRule, /overflow-x:\s*auto/);
  assert.match(listRule, /border:\s*1px solid var\(--color-divider-strong\)/);
  assert.match(statRule, /display:\s*grid/);
  assert.match(statRule, /min-height:\s*72px/);
  assert.match(statRule, /padding:\s*var\(--space-4\) var\(--space-3\)/);
  assert.match(statRule, /place-items:\s*center/);
  assert.match(dividedRule, /border-left:\s*1px solid var\(--color-divider\)/);
  assert.match(headingRule, /display:\s*inline-flex/);
  assert.match(headingRule, /align-items:\s*center/);
  assert.match(dotRule, /width:\s*8px/);
  assert.match(dotRule, /height:\s*8px/);
  assert.match(headingRule, /font-size:\s*var\(--text-xs\)/);
  assert.match(valueRule, /font-size:\s*var\(--text-lg\)/);
  assert.match(titleRule, /font-size:\s*var\(--text-xl\)/);
  assert.match(splitTitleRule, /font-size:\s*var\(--text-lg\)/);
  assert.match(splitTitleRule, /text-transform:\s*none/);
});
