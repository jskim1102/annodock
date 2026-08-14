import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const selectMenu = await readFile(
  new URL("../src/components/SelectMenu.tsx", import.meta.url),
  "utf8",
).catch(() => "");
const appCss = await readFile(
  new URL("../src/styles/app.css", import.meta.url),
  "utf8",
);
const componentSources = await Promise.all([
  "../src/pages/TrainPage.tsx",
  "../src/pages/ProjectsPage.tsx",
  "../src/pages/RunDetailPage.tsx",
  "../src/components/NewProjectDialog.tsx",
].map((path) => readFile(new URL(path, import.meta.url), "utf8")));

test("all dropdown selections use the shared custom menu instead of native select popovers", () => {
  assert.ok(selectMenu, "SelectMenu.tsx must exist");
  for (const source of componentSources) {
    assert.doesNotMatch(source, /<select\b/);
  }
  assert.equal(
    componentSources.reduce((count, source) => count + (source.match(/<SelectMenu\b/g)?.length ?? 0), 0),
    6,
  );
});

test("the shared selection trigger and options expose an accessible listbox contract", () => {
  assert.match(selectMenu, /aria-haspopup="listbox"/);
  assert.match(selectMenu, /aria-expanded=\{open\}/);
  assert.match(selectMenu, /aria-controls=\{listboxId\}/);
  assert.match(selectMenu, /role="listbox"/);
  assert.match(selectMenu, /role="option"/);
  assert.match(selectMenu, /aria-selected=\{option\.value === selectedValue\}/);
  assert.match(selectMenu, /event\.key === "Escape"/);
  assert.match(selectMenu, /event\.key === "ArrowDown"/);
  assert.match(selectMenu, /event\.key === "ArrowUp"/);
  assert.match(selectMenu, /pointerdown/);
});

test("the custom popover follows the existing row-menu visual language", () => {
  const popoverRule = appCss.match(/\.select-menu-popover\s*\{([^}]*)\}/)?.[1] ?? "";
  const optionRule = appCss.match(/\.select-menu-option\s*\{([^}]*)\}/)?.[1] ?? "";

  assert.match(popoverRule, /background:\s*var\(--color-bg\)/);
  assert.match(popoverRule, /border:\s*1px solid var\(--color-divider\)/);
  assert.match(popoverRule, /border-radius:\s*var\(--radius-md\)/);
  assert.match(popoverRule, /box-shadow:\s*var\(--shadow-md\)/);
  assert.match(optionRule, /background:\s*transparent/);
  assert.match(optionRule, /text-align:\s*left/);
});
