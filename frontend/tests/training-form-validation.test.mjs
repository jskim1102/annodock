import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import ts from "typescript";

async function loadTrainingFormValidation() {
  const source = await readFile(
    new URL("../src/utils/trainingFormValidation.ts", import.meta.url),
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

const validValues = {
  trainRatio: "70",
  validRatio: "20",
  testRatio: "10",
  epochs: "200",
  imgsz: "640",
  batch: "32",
  lr0: "0.01",
  lrf: "0.01",
  warmupEpochs: "3",
  patience: "30",
  mosaic: "1",
  mixup: "0",
  hsvH: "0.015",
  hsvS: "0.7",
  hsvV: "0.4",
  fliplr: "0.5",
  scale: "0.5",
  translate: "0.1",
  workers: "8",
  savePeriod: "25",
  multiScale: "0",
  seed: "",
};

test("valid training numeric values pass, including optional seed and -1 sentinels", async () => {
  const { validateTrainingFormNumericValues } = await loadTrainingFormValidation();

  assert.deepEqual(validateTrainingFormNumericValues(validValues), {});
  assert.deepEqual(validateTrainingFormNumericValues({
    ...validValues,
    batch: "-1",
    savePeriod: "-1",
    seed: "42",
  }), {});
});

test("required numeric fields reject blanks before Number can coerce them to zero", async () => {
  const { TRAINING_NUMERIC_FIELDS, validateTrainingFormNumericValues } = await loadTrainingFormValidation();

  for (const field of TRAINING_NUMERIC_FIELDS) {
    if (field === "seed") continue;
    const errors = validateTrainingFormNumericValues({ ...validValues, [field]: "   " });
    assert.ok(errors[field], field);
  }
  assert.equal(validateTrainingFormNumericValues({ ...validValues, seed: "   " }).seed, undefined);
});

test("integer-only and sentinel fields enforce the backend contract", async () => {
  const { validateTrainingFormNumericValues } = await loadTrainingFormValidation();

  for (const [field, value] of [
    ["epochs", "0"],
    ["epochs", "1.5"],
    ["imgsz", "0"],
    ["imgsz", "640.5"],
    ["batch", "0"],
    ["batch", "-2"],
    ["batch", "1.5"],
    ["patience", "-1"],
    ["patience", "1.5"],
    ["workers", "-1"],
    ["workers", "129"],
    ["workers", "1.5"],
    ["savePeriod", "0"],
    ["savePeriod", "-2"],
    ["savePeriod", "1.5"],
    ["seed", "1.5"],
  ]) {
    const errors = validateTrainingFormNumericValues({ ...validValues, [field]: value });
    assert.ok(errors[field], `${field}=${value}`);
  }
});

test("bounded decimal parameters reject non-numbers and out-of-range values", async () => {
  const { validateTrainingFormNumericValues } = await loadTrainingFormValidation();

  for (const [field, value] of [
    ["trainRatio", "-1"],
    ["validRatio", "101"],
    ["testRatio", "not-a-number"],
    ["lr0", "0"],
    ["lr0", "1.1"],
    ["lrf", "-0.1"],
    ["lrf", "1.1"],
    ["warmupEpochs", "-0.1"],
    ["mosaic", "1.1"],
    ["mixup", "-0.1"],
    ["hsvH", "1.1"],
    ["hsvS", "-0.1"],
    ["hsvV", "Infinity"],
    ["fliplr", "not-a-number"],
    ["scale", "1.1"],
    ["translate", "-0.1"],
    ["multiScale", "1.1"],
  ]) {
    const errors = validateTrainingFormNumericValues({ ...validValues, [field]: value });
    assert.ok(errors[field], `${field}=${value}`);
  }
});

test("TrainPage validates and renders field errors before starting the API request", async () => {
  const trainPage = await readFile(
    new URL("../src/pages/TrainPage.tsx", import.meta.url),
    "utf8",
  );

  assert.match(trainPage, /<form noValidate/);
  assert.match(trainPage, /const numericErrors = validateTrainingFormNumericValues\(/);
  assert.match(trainPage, /setFieldErrors\(numericErrors\)/);
  assert.ok(
    trainPage.indexOf("const numericErrors = validateTrainingFormNumericValues(")
      < trainPage.indexOf("await startTraining"),
  );
  assert.match(trainPage, /aria-invalid/);
  assert.match(trainPage, /fieldErrors\[field\]/);

  for (const field of Object.keys(validValues).filter((field) => !field.endsWith("Ratio"))) {
    assert.match(trainPage, new RegExp(`numericInputProps\\("${field}"\\)`), field);
  }
  for (const [key, field] of [
    ["train", "trainRatio"],
    ["valid", "validRatio"],
    ["test", "testRatio"],
  ]) {
    assert.match(trainPage, new RegExp(`${key}: "${field}"`), field);
  }
});
