import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import ts from "typescript";

async function loadTypeScriptModule(relativePath) {
  const sourcePath = new URL(relativePath, import.meta.url);
  const source = await readFile(sourcePath, "utf8");
  const javascript = ts.transpileModule(source, {
    compilerOptions: {
      module: ts.ModuleKind.ESNext,
      target: ts.ScriptTarget.ES2022,
    },
  }).outputText;

  return import(`data:text/javascript;base64,${Buffer.from(javascript).toString("base64")}`);
}

test("HSV conversion round-trips every class preset", async () => {
  const { hexToHsv, hsvToHex } = await loadTypeScriptModule("../src/utils/colorMath.ts");
  const presets = [
    "#ef4444",
    "#f59e0b",
    "#22c55e",
    "#3b82f6",
    "#8b5cf6",
    "#ec4899",
    "#06b6d4",
    "#84cc16",
  ];

  for (const preset of presets) {
    assert.equal(hsvToHex(hexToHsv(preset)), preset);
  }
});

test("wheel coordinates resolve hue and saturation predictably", async () => {
  const { pointToHueSaturation } = await loadTypeScriptModule("../src/utils/colorMath.ts");

  assert.deepEqual(pointToHueSaturation(100, 100, 200, 200), {
    hue: 0,
    saturation: 0,
  });
  assert.deepEqual(pointToHueSaturation(200, 100, 200, 200), {
    hue: 0,
    saturation: 1,
  });
  assert.deepEqual(pointToHueSaturation(100, 200, 200, 200), {
    hue: 90,
    saturation: 1,
  });
});

test("the picker exposes a free-color wheel, brightness slider, and presets", async () => {
  const pickerSource = await readFile(
    new URL("../src/components/ClassColorPicker.tsx", import.meta.url),
    "utf8",
  );
  const dialogSource = await readFile(
    new URL("../src/components/NewProjectDialog.tsx", import.meta.url),
    "utf8",
  );

  assert.match(pickerSource, /class-color-wheel/);
  assert.match(pickerSource, /type="range"/);
  assert.match(pickerSource, /CLASS_COLOR_PRESETS\.map/);
  assert.match(dialogSource, /<ClassColorPicker/);
  assert.doesNotMatch(dialogSource, /colorIndex:\s*\(candidate\.colorIndex \+ 1\)/);
});
