import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const baseCss = await readFile(
  new URL("../src/styles/base.css", import.meta.url),
  "utf8",
);
const checkboxSources = await Promise.all([
  readFile(new URL("../src/pages/ProjectsPage.tsx", import.meta.url), "utf8"),
  readFile(new URL("../src/pages/RunsPage.tsx", import.meta.url), "utf8"),
  readFile(new URL("../src/pages/TrainPage.tsx", import.meta.url), "utf8"),
]);

test("checked boxes center a vector check icon on both axes", () => {
  const checkboxRule = baseCss.match(/\.checkbox\s*\{(?<body>[\s\S]*?)\}/)?.groups?.body ?? "";

  assert.match(checkboxRule, /align-items:\s*center/);
  assert.match(checkboxRule, /justify-content:\s*center/);
  assert.match(baseCss, /\.checkbox svg\s*\{[\s\S]*?stroke-width:\s*2\.5;[\s\S]*?\}/);
  assert.doesNotMatch(baseCss, /\.checkbox\.is-on::after/);

  for (const source of checkboxSources) {
    assert.match(source, /<Icon name="check" size=\{10\} \/>/);
  }
});
