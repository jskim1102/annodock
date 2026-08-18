import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const runsPage = await readFile(
  new URL("../src/pages/RunsPage.tsx", import.meta.url),
  "utf8",
);
const projectsPage = await readFile(
  new URL("../src/pages/ProjectsPage.tsx", import.meta.url),
  "utf8",
);

function sourceBetween(start, end) {
  const startIndex = runsPage.indexOf(start);
  const endIndex = runsPage.indexOf(end, startIndex);
  assert.notEqual(startIndex, -1, `missing source boundary: ${start}`);
  assert.notEqual(endIndex, -1, `missing source boundary: ${end}`);
  return runsPage.slice(startIndex, endIndex);
}

test("run polling retries 2.5 seconds after a transient failure", () => {
  const pollingEffect = sourceBetween(
    "  useEffect(() => {\n    let active = true;",
    "\n\n  const runningCount",
  );

  assert.match(
    pollingEffect,
    /const scheduleRefresh = \(\) => \{[\s\S]*?if \(!active\) return;[\s\S]*?window\.setTimeout\(\(\) => void refresh\(\), 2500\)/,
  );
  assert.match(
    pollingEffect,
    /catch \(reason: unknown\) \{[\s\S]*?if \(!active\) return;[\s\S]*?setError\([\s\S]*?scheduleRefresh\(\);[\s\S]*?\}/,
  );
  assert.match(pollingEffect, /active = false;[\s\S]*?window\.clearTimeout\(timer\)/);
});

test("project polling retries after failure and preserves successful thumbnails", () => {
  const startIndex = projectsPage.indexOf("  useEffect(() => {\n    let active = true;");
  const endIndex = projectsPage.indexOf("\n\n  useEffect(() => {\n    if (!createdProject)", startIndex);
  const pollingEffect = projectsPage.slice(startIndex, endIndex);

  assert.notEqual(startIndex, -1);
  assert.notEqual(endIndex, -1);
  assert.match(
    pollingEffect,
    /const scheduleRefresh = \(\) => \{[\s\S]*?window\.setTimeout\(\(\) => void refresh\(\), 2000\)/,
  );
  assert.match(
    pollingEffect,
    /catch \(reason: unknown\) \{[\s\S]*?setError\([\s\S]*?scheduleRefresh\(\);/,
  );
  assert.match(
    pollingEffect,
    /setThumbs\(\(current\) => \{[\s\S]*?const next = new Map\([\s\S]*?next\.set\(entry\[0\], entry\[1\]\)[\s\S]*?return next/,
  );
  assert.match(pollingEffect, /active = false;[\s\S]*?window\.clearTimeout\(timer\)/);
});

test("run cleanup and deletion keep canonical refresh but suppress state after unmount", () => {
  assert.match(
    runsPage,
    /const mountedRef = useRef\(true\);[\s\S]*?useEffect\(\(\) => \{[\s\S]*?mountedRef\.current = true;[\s\S]*?mountedRef\.current = false;[\s\S]*?\}, \[\]\);/,
  );

  const cleanupHandler = sourceBetween(
    "  const cleanupSelected = async () => {",
    "\n\n  const deleteSelectedRuns",
  );
  assert.match(cleanupHandler, /finally \{[\s\S]*?await getRuns\(\)/);
  assert.match(cleanupHandler, /if \(mountedRef\.current\) \{[\s\S]*?setNotice\(/);
  assert.match(cleanupHandler, /if \(mountedRef\.current\) \{[\s\S]*?setRuns\(response\.items\)/);
  assert.match(cleanupHandler, /if \(mountedRef\.current\) \{[\s\S]*?setSelected\([\s\S]*?setCleaning\(false\)/);

  const deleteHandler = sourceBetween(
    "  const deleteSelectedRuns = async () => {",
    "\n\n  return (",
  );
  assert.match(deleteHandler, /finally \{[\s\S]*?await getRuns\(\)/);
  assert.match(deleteHandler, /if \(mountedRef\.current\) \{[\s\S]*?setNotice\(/);
  assert.match(deleteHandler, /if \(mountedRef\.current\) \{[\s\S]*?setRuns\(response\.items\)/);
  assert.match(deleteHandler, /if \(mountedRef\.current\) \{[\s\S]*?setSelected\([\s\S]*?setDeleting\(false\)/);
});
