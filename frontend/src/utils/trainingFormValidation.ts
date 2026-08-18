export const TRAINING_NUMERIC_FIELDS = [
  "trainRatio",
  "validRatio",
  "testRatio",
  "epochs",
  "imgsz",
  "batch",
  "lr0",
  "lrf",
  "warmupEpochs",
  "patience",
  "mosaic",
  "mixup",
  "hsvH",
  "hsvS",
  "hsvV",
  "fliplr",
  "scale",
  "translate",
  "workers",
  "savePeriod",
  "multiScale",
  "seed",
] as const;

export type TrainingNumericField = typeof TRAINING_NUMERIC_FIELDS[number];

export type TrainingFormNumericValues = Record<TrainingNumericField, string>;

export type TrainingFormNumericErrors = Partial<Record<TrainingNumericField, string>>;

interface NumericRule {
  label: string;
  optional?: boolean;
  integer?: boolean;
  min?: number;
  minExclusive?: number;
  max?: number;
  allowNegativeOne?: boolean;
}

const RULES: Record<TrainingNumericField, NumericRule> = {
  trainRatio: { label: "train 비율", min: 0, max: 100 },
  validRatio: { label: "valid 비율", min: 0, max: 100 },
  testRatio: { label: "test 비율", min: 0, max: 100 },
  epochs: { label: "epochs", integer: true, min: 1 },
  imgsz: { label: "imgsz", integer: true, min: 1 },
  batch: { label: "batch", integer: true, min: 1, allowNegativeOne: true },
  lr0: { label: "lr0", minExclusive: 0, max: 1 },
  lrf: { label: "lrf", min: 0, max: 1 },
  warmupEpochs: { label: "warmup epochs", min: 0 },
  patience: { label: "patience", integer: true, min: 0 },
  mosaic: { label: "mosaic", min: 0, max: 1 },
  mixup: { label: "mixup", min: 0, max: 1 },
  hsvH: { label: "hsv h", min: 0, max: 1 },
  hsvS: { label: "hsv s", min: 0, max: 1 },
  hsvV: { label: "hsv v", min: 0, max: 1 },
  fliplr: { label: "fliplr", min: 0, max: 1 },
  scale: { label: "scale", min: 0, max: 1 },
  translate: { label: "translate", min: 0, max: 1 },
  workers: { label: "workers", integer: true, min: 0, max: 128 },
  savePeriod: { label: "save period", integer: true, min: 1, allowNegativeOne: true },
  multiScale: { label: "multi scale", min: 0, max: 1 },
  seed: { label: "seed", optional: true, integer: true },
};

function rangeMessage(rule: NumericRule): string {
  if (rule.allowNegativeOne) {
    return `${rule.label}는 -1 또는 ${rule.min} 이상의 정수여야 합니다.`;
  }
  if (rule.minExclusive !== undefined && rule.max !== undefined) {
    return `${rule.label}는 ${rule.minExclusive}보다 크고 ${rule.max} 이하여야 합니다.`;
  }
  if (rule.min !== undefined && rule.max !== undefined) {
    return `${rule.label}는 ${rule.min} 이상 ${rule.max} 이하여야 합니다.`;
  }
  if (rule.min !== undefined) {
    return `${rule.label}는 ${rule.min} 이상이어야 합니다.`;
  }
  if (rule.max !== undefined) {
    return `${rule.label}는 ${rule.max} 이하여야 합니다.`;
  }
  return `${rule.label} 값을 확인하세요.`;
}

export function validateTrainingFormNumericValues(
  values: TrainingFormNumericValues,
): TrainingFormNumericErrors {
  const errors: TrainingFormNumericErrors = {};

  for (const field of TRAINING_NUMERIC_FIELDS) {
    const rule = RULES[field];
    const rawValue = values[field].trim();
    if (rawValue === "") {
      if (!rule.optional) errors[field] = `${rule.label} 값을 입력하세요.`;
      continue;
    }

    const value = Number(rawValue);
    if (!Number.isFinite(value)) {
      errors[field] = `${rule.label}에 유효한 숫자를 입력하세요.`;
      continue;
    }
    if (rule.integer && !Number.isInteger(value)) {
      errors[field] = `${rule.label}는 정수여야 합니다.`;
      continue;
    }
    if (rule.allowNegativeOne && value === -1) continue;
    if (
      (rule.minExclusive !== undefined && value <= rule.minExclusive)
      || (rule.min !== undefined && value < rule.min)
      || (rule.max !== undefined && value > rule.max)
    ) {
      errors[field] = rangeMessage(rule);
    }
  }

  return errors;
}

export function hasTrainingFormNumericErrors(
  errors: TrainingFormNumericErrors,
): boolean {
  return Object.keys(errors).length > 0;
}
