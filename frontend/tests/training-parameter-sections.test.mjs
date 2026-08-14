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

test("performance and operation parameters follow the basic parameter hierarchy", () => {
  assert.match(trainPage, /성능 파라미터/);
  assert.match(trainPage, /운영 파라미터/);
  assert.match(trainPage, /<section className="training-parameter-section">[\s\S]*<h2 className="training-section-label training-parameter-title">성능 파라미터<\/h2>[\s\S]*<div className="training-parameter-panel"/);
  assert.match(trainPage, /<section className="training-parameter-section">[\s\S]*<h2 className="training-section-label training-parameter-title">운영 파라미터<\/h2>[\s\S]*<div className="training-parameter-panel"/);
  assert.doesNotMatch(trainPage, /performanceOpen|operationsOpen|training-parameter-toggle|aria-expanded|aria-controls="(?:performance|operation)-parameters"/);
  assert.doesNotMatch(trainPage, /training-parameter-(?:heading|copy)/);
});

test("RTX 3090 recommendations prefill both parameter sections", () => {
  assert.match(trainPage, /RTX_3090_TRAINING_DEFAULTS/);
  assert.match(trainPage, /setCompile\] = useState<boolean>\(RTX_3090_TRAINING_DEFAULTS\.compile\)/);
  assert.match(trainPage, /setDeterministic\] = useState<boolean>\(RTX_3090_TRAINING_DEFAULTS\.deterministic\)/);
  assert.match(trainPage, /setMultiScale\] = useState\(String\(RTX_3090_TRAINING_DEFAULTS\.multi_scale\)\)/);
});

test("the legacy floating advanced popover is removed", () => {
  assert.doesNotMatch(trainPage, /advanced-popover/);
  assert.doesNotMatch(trainPage, />고급 </);
});

test("boolean options render as lightweight borderless checkbox rows", () => {
  const rowRule = appCss.match(/\.training-option-list label\s*\{([^}]*)\}/)?.[1] ?? "";
  assert.match(rowRule, /padding:\s*var\(--space-2\) 0/);
  assert.doesNotMatch(rowRule, /border:/);
  assert.doesNotMatch(rowRule, /border-radius:/);
  assert.doesNotMatch(rowRule, /min-height:/);
});

test("parameter groups match the borderless basic parameter section", () => {
  const sectionRule = appCss.match(/\.training-parameter-section\s*\{([^}]*)\}/)?.[1] ?? "";
  const panelRule = appCss.match(/\.training-parameter-panel\s*\{([^}]*)\}/)?.[1] ?? "";

  assert.notEqual(sectionRule, "");
  assert.notEqual(panelRule, "");
  assert.doesNotMatch(sectionRule, /border:/);
  assert.doesNotMatch(sectionRule, /border-radius:/);
  assert.doesNotMatch(panelRule, /border-top:/);
  assert.match(panelRule, /padding:\s*0/);
  assert.match(panelRule, /background:\s*transparent/);
});

test("performance and operation groups stack like the basic parameter group", () => {
  const sectionsRule = appCss.match(/\.training-parameter-sections\s*\{([^}]*)\}/)?.[1] ?? "";

  assert.match(sectionsRule, /grid-template-columns:\s*minmax\(0,\s*1fr\)/);
  assert.doesNotMatch(appCss, /\.training-parameter-(?:switch|toggle)/);
  assert.doesNotMatch(appCss, /\.training-parameter-(?:heading|copy)/);
});

test("all parameter rows use the same vertical rhythm as basic parameters", () => {
  const panelFieldRule = appCss.match(/\.training-parameter-panel \.field\s*\{([^}]*)\}/)?.[1] ?? "";

  assert.match(panelFieldRule, /margin-bottom:\s*var\(--space-5\)/);
  assert.doesNotMatch(panelFieldRule, /margin-bottom:\s*0/);
});
