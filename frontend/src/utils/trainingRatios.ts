export type TrainingSplitMode = "2way" | "3way";

export type RecommendedRatios = {
  train: number;
  valid: number;
  test: number;
};

export function getRecommendedRatios(
  train: number,
  valid: number,
  mode: TrainingSplitMode,
): RecommendedRatios | null {
  if (!Number.isFinite(train) || train < 0 || train > 100) return null;

  const remaining = 100 - train;
  if (mode === "2way") return { train, valid: remaining, test: 0 };

  if (!Number.isFinite(valid) || valid < 0 || valid > remaining) return null;
  return { train, valid, test: remaining - valid };
}
