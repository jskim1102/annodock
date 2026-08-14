import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import ts from "typescript";

async function loadTrainingRatios() {
  const source = await readFile(
    new URL("../src/utils/trainingRatios.ts", import.meta.url),
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

const trainPage = await readFile(
  new URL("../src/pages/TrainPage.tsx", import.meta.url),
  "utf8",
);
const appCss = await readFile(
  new URL("../src/styles/app.css", import.meta.url),
  "utf8",
);

test("training setup removes explanatory copy that does not help the decision", () => {
  assert.doesNotMatch(trainPage, /서버가 데이터셋과 run ID로 확정/);
  assert.doesNotMatch(trainPage, /allowlist 5종 · 커스텀 \.pt 업로드 없음/);
  assert.doesNotMatch(trainPage, /2-way 선택 시 test 숨김 · 합 100 검증/);
});

test("split recommendations keep entered train and valid values before completing 100", async () => {
  const { getRecommendedRatios } = await loadTrainingRatios();

  assert.deepEqual(getRecommendedRatios(65, 20, "2way"), { train: 65, valid: 35, test: 0 });
  assert.deepEqual(getRecommendedRatios(65, 20, "3way"), { train: 65, valid: 20, test: 15 });
  assert.deepEqual(getRecommendedRatios(70, 20, "3way"), { train: 70, valid: 20, test: 10 });
  assert.equal(getRecommendedRatios(-1, 20, "3way"), null);
  assert.equal(getRecommendedRatios(65, 36, "3way"), null);
});

test("split inputs show train-anchored recommended values and a live path to 100", () => {
  assert.match(trainPage, /getRecommendedRatios\(ratios\.train, ratios\.valid, splitMode\)/);
  assert.match(trainPage, /recommendedRatios\.train[\s\S]*recommendedRatios\.valid[\s\S]*recommendedRatios\.test/);
  assert.match(trainPage, /const ratioGap = 100 - ratioTotal/);
  assert.match(trainPage, /ratioGap > 0[\s\S]*\$\{ratioGap\} 더 필요[\s\S]*Math\.abs\(ratioGap\)[\s\S]*줄이기/);
  assert.match(trainPage, /className=\{`split-ratio-guidance\$\{ratioGap === 0 \? "" : " is-warning"\}`\}/);
  assert.match(trainPage, /권장값 · \{recommendedRatioText\}/);
  assert.match(trainPage, /\{ratioStatusText\}/);
});

test("split recommendation stays compact and visually secondary", () => {
  const rule = appCss.match(/\.split-ratio-guidance\s*\{([^}]*)\}/)?.[1] ?? "";
  assert.match(rule, /display:\s*flex/);
  assert.match(rule, /justify-content:\s*space-between/);
  assert.match(rule, /color:\s*var\(--color-muted\)/);
  assert.match(appCss, /\.split-ratio-guidance\.is-warning[\s\S]*color:\s*var\(--color-danger\)/);
});
