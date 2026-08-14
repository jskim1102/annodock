import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import ts from "typescript";

async function loadTrainingArguments() {
  const source = await readFile(
    new URL("../src/utils/trainingArguments.ts", import.meta.url),
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
const trainingApi = await readFile(
  new URL("../src/api/training.ts", import.meta.url),
  "utf8",
);
const appCss = await readFile(
  new URL("../src/styles/app.css", import.meta.url),
  "utf8",
);

test("RTX 3090 defaults cover the complete supported training recipe", async () => {
  const { RTX_3090_TRAINING_DEFAULTS } = await loadTrainingArguments();

  assert.deepEqual(RTX_3090_TRAINING_DEFAULTS, {
    epochs: 200,
    imgsz: 640,
    batch: 32,
    device: 0,
    optimizer: "auto",
    lr0: 0.01,
    lrf: 0.01,
    warmup_epochs: 3,
    cos_lr: true,
    patience: 30,
    augment: true,
    mosaic: 1,
    mixup: 0,
    copy_paste: 0,
    close_mosaic: 10,
    hsv_h: 0.015,
    hsv_s: 0.7,
    hsv_v: 0.4,
    fliplr: 0.5,
    scale: 0.5,
    translate: 0.1,
    workers: 8,
    cache: "ram",
    amp: true,
    compile: true,
    deterministic: false,
    save_period: 25,
    multi_scale: 0,
    exclude_unlabeled_images: false,
    include_unlabeled_images_in_test: false,
  });
});

test("the API request type carries every persisted worker argument", () => {
  for (const field of [
    "device", "optimizer", "lr0", "lrf", "warmup_epochs", "cos_lr",
    "patience", "augment", "mosaic", "mixup", "copy_paste", "close_mosaic", "hsv_h",
    "hsv_s", "hsv_v", "fliplr", "scale", "translate", "workers",
    "cache", "amp", "compile", "deterministic", "save_period", "multi_scale",
    "exclude_unlabeled_images", "include_unlabeled_images_in_test",
  ]) {
    assert.match(trainingApi, new RegExp(`\\b${field}\\??:`), field);
  }
});

test("the training form submits every editable and fixed detection parameter to the API", () => {
  assert.match(trainPage, /RTX_3090_TRAINING_DEFAULTS/);
  assert.match(trainPage, /RTX 3090 추천값 적용/);
  for (const binding of [
    "device: 0", "optimizer", "lr0: Number\\(lr0\\)", "lrf: Number\\(lrf\\)",
    "warmup_epochs: Number\\(warmupEpochs\\)", "cos_lr: cosLr", "patience: Number\\(patience\\)",
    "augment", "mosaic: Number\\(mosaic\\)", "mixup: Number\\(mixup\\)",
    "copy_paste: 0", "close_mosaic: RTX_3090_TRAINING_DEFAULTS.close_mosaic",
    "hsv_h: Number\\(hsvH\\)",
    "hsv_s: Number\\(hsvS\\)", "hsv_v: Number\\(hsvV\\)",
    "fliplr: Number\\(fliplr\\)", "scale: Number\\(scale\\)",
    "translate: Number\\(translate\\)", "workers: Number\\(workers\\)",
    "cache", "amp", "compile", "deterministic", "save_period: Number\\(savePeriod\\)",
    "multi_scale: Number\\(multiScale\\)",
    "exclude_unlabeled_images: excludeUnlabeledImages",
    "include_unlabeled_images_in_test: includeUnlabeledImagesInTest",
  ]) {
    assert.match(trainPage, new RegExp(binding), binding);
  }
  assert.match(trainPage, /compile && Number\(multiScale\) > 0/);
});

test("the training form can exclude unlabeled images from recommendations and splits", () => {
  assert.match(trainPage, /useState<boolean>\(RTX_3090_TRAINING_DEFAULTS\.exclude_unlabeled_images\)/);
  assert.match(trainPage, /id="exclude-unlabeled-images"/);
  assert.match(trainPage, /checked=\{excludeUnlabeledImages\}/);
  assert.match(trainPage, />라벨 없는 이미지 제외</);
  assert.match(trainPage, /excludeUnlabeledImages\s*\? recommendation\?\.labeled_images/);
  assert.match(trainPage, /excludeUnlabeledImages\s*&&\s*!includeUnlabeledImagesInTest\s*\? eligibleImages\s*:\s*totalImages/);
  assert.match(trainingApi, /excludeUnlabeledImages: boolean/);
  assert.match(trainingApi, /exclude_unlabeled_images: String\(options\.excludeUnlabeledImages\)/);
});

test("three-way mode can reserve missing-label images for test only", () => {
  assert.match(trainPage, /useState<boolean>\(RTX_3090_TRAINING_DEFAULTS\.include_unlabeled_images_in_test\)/);
  assert.match(trainPage, /id="include-unlabeled-images-in-test"/);
  assert.match(trainPage, /checked=\{includeUnlabeledImagesInTest\}/);
  assert.match(trainPage, /disabled=\{splitMode !== "3way" \|\| !excludeUnlabeledImages\}/);
  assert.match(trainPage, />라벨 없는 이미지 test에 포함</);
  assert.match(trainPage, /setIncludeUnlabeledImagesInTest\(false\)/);
  assert.doesNotMatch(trainPage, /if \(checked\) setExcludeUnlabeledImages\(false\)/);
  assert.match(trainPage, /excludeUnlabeledImages\s*&&\s*!includeUnlabeledImagesInTest\s*\? eligibleImages\s*:\s*totalImages/);
  assert.match(trainingApi, /includeUnlabeledImagesInTest: boolean/);
  assert.match(trainingApi, /include_unlabeled_images_in_test: String\(options\.includeUnlabeledImagesInTest\)/);
});

test("unlabeled image policies render as large stateful option cards without redundant status copy", () => {
  assert.match(trainPage, /className=\{`training-data-option\$\{excludeUnlabeledImages \? " is-selected" : ""\}`\}/);
  assert.match(trainPage, /training-data-option-title/);
  assert.match(trainPage, />3-way 전용</);
  assert.doesNotMatch(trainPage, /라벨 없는 이미지 처리/);
  assert.doesNotMatch(trainPage, />선택됨</);
  assert.match(appCss, /\.training-data-option\s*\{[\s\S]*?border:/);
  assert.match(appCss, /\.training-data-option\s*\{[\s\S]*?background:\s*var\(--color-bg\)/);
  assert.match(appCss, /\.training-data-option\.is-selected\s*\{[\s\S]*?background:\s*color-mix\(in srgb, var\(--color-accent\) 5%, var\(--color-bg\)\)/);
  const selectedRule = appCss.match(/\.training-data-option\.is-selected\s*\{([^}]*)\}/)?.[1] ?? "";
  assert.doesNotMatch(selectedRule, /border-color|box-shadow/);
  assert.match(appCss, /\.training-data-options \.training-data-checkbox\s*\{[\s\S]*?width:\s*18px;[\s\S]*?height:\s*18px/);
  assert.match(trainPage, /라벨 소스와 bbox가 없는 이미지를 제외하고 나머지만 분할합니다\./);
  assert.match(trainPage, /제외된 라벨 없는 이미지를 test에만 추가합니다\./);
  assert.match(appCss, /\.training-data-options \.training-data-option\s*\{[\s\S]*?min-height:\s*68px/);
});

test("primary training fields are ordered run, split, Model", () => {
  const runIndex = trainPage.indexOf('htmlFor="run-name">run</label>');
  const splitIndex = trainPage.indexOf('>분할</span>');
  const modelIndex = trainPage.indexOf('htmlFor="preset">Model</label>');

  assert.ok(runIndex >= 0);
  assert.ok(splitIndex > runIndex);
  assert.ok(modelIndex > splitIndex);
});

test("optimization and augmentation groups expose the missing controls", () => {
  assert.match(trainPage, />최적화 파라미터</);
  assert.match(trainPage, />증강 파라미터</);
  for (const id of [
    "optimizer", "lr0", "lrf", "warmup-epochs", "patience", "mosaic",
    "mixup", "hsv-h", "hsv-s", "hsv-v", "fliplr",
    "scale", "translate", "multi-scale",
  ]) {
    assert.match(trainPage, new RegExp(`id="${id}"`), id);
  }
  assert.doesNotMatch(trainPage, /id="copy-paste"/);
});

test("RTX 3090 recommendation is fetched, applied, and multi-scale disables compile", () => {
  assert.match(trainingApi, /interface TrainingRecommendation/);
  assert.match(trainingApi, /getTrainingRecommendation/);
  assert.match(trainingApi, /training-recommendation\?\$\{query\}/);
  assert.match(trainPage, /getTrainingRecommendation/);
  for (const setter of [
    "setEpochs", "setImgsz", "setBatch", "setOptimizer", "setLr0",
    "setWarmupEpochs", "setPatience", "setMosaic", "setMixup", "setScale",
    "setAmp", "setCompile",
  ]) {
    assert.match(trainPage, new RegExp(`${setter}\\(`), setter);
  }
  assert.match(trainPage, /if \(Number\(value\) > 0\) setCompile\(false\)/);
});
