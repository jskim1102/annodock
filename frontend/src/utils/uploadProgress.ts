export interface ProgressSample {
  key: string;
  completed: number;
  total: number;
  atMs: number;
}

export interface ProgressEstimateState {
  key: string;
  total: number;
  lastCompleted: number;
  lastSampleAtMs: number;
  lastProgressAtMs: number;
  smoothedRatePerSecond: number | null;
}

export interface ProgressEstimate {
  state: ProgressEstimateState;
  remainingSeconds: number | null;
}

const RATE_SMOOTHING_WEIGHT = 0.35;
const STALE_RATE_AFTER_MS = 15_000;

function normalizedSample(sample: ProgressSample) {
  const total = Math.max(0, sample.total);
  return {
    ...sample,
    total,
    completed: Math.min(total, Math.max(0, sample.completed)),
  };
}

function initialEstimate(sample: ProgressSample): ProgressEstimate {
  return {
    state: {
      key: sample.key,
      total: sample.total,
      lastCompleted: sample.completed,
      lastSampleAtMs: sample.atMs,
      lastProgressAtMs: sample.atMs,
      smoothedRatePerSecond: null,
    },
    remainingSeconds: sample.total > 0 && sample.completed >= sample.total
      ? 0
      : null,
  };
}

export function updateProgressEstimate(
  previous: ProgressEstimateState | null,
  rawSample: ProgressSample,
): ProgressEstimate {
  const sample = normalizedSample(rawSample);
  if (
    previous === null
    || previous.key !== sample.key
    || previous.total !== sample.total
    || sample.completed < previous.lastCompleted
    || sample.atMs < previous.lastSampleAtMs
  ) {
    return initialEstimate(sample);
  }

  if (sample.completed >= sample.total && sample.total > 0) {
    return {
      state: {
        ...previous,
        lastCompleted: sample.completed,
        lastSampleAtMs: sample.atMs,
        lastProgressAtMs: sample.atMs,
      },
      remainingSeconds: 0,
    };
  }

  const completedDelta = sample.completed - previous.lastCompleted;
  const elapsedSeconds = (sample.atMs - previous.lastSampleAtMs) / 1000;
  let smoothedRate = previous.smoothedRatePerSecond;
  let lastProgressAtMs = previous.lastProgressAtMs;
  let lastSampleAtMs = previous.lastSampleAtMs;
  if (completedDelta > 0 && elapsedSeconds > 0) {
    const currentRate = completedDelta / elapsedSeconds;
    smoothedRate = smoothedRate === null
      ? currentRate
      : smoothedRate * (1 - RATE_SMOOTHING_WEIGHT)
        + currentRate * RATE_SMOOTHING_WEIGHT;
    lastProgressAtMs = sample.atMs;
    lastSampleAtMs = sample.atMs;
  }

  const state: ProgressEstimateState = {
    key: sample.key,
    total: sample.total,
    lastCompleted: sample.completed,
    lastSampleAtMs,
    lastProgressAtMs,
    smoothedRatePerSecond: smoothedRate,
  };
  if (
    smoothedRate === null
    || smoothedRate <= 0
    || sample.atMs - lastProgressAtMs >= STALE_RATE_AFTER_MS
  ) {
    return { state, remainingSeconds: null };
  }
  return {
    state,
    remainingSeconds: Math.max(
      1,
      Math.ceil((sample.total - sample.completed) / smoothedRate),
    ),
  };
}

export function formatRemainingTime(seconds: number | null): string {
  if (seconds === null || !Number.isFinite(seconds)) return "계산 중…";
  const safeSeconds = Math.max(0, Math.ceil(seconds));
  if (safeSeconds === 0) return "곧 완료";
  if (safeSeconds < 60) return `약 ${safeSeconds}초`;
  if (safeSeconds < 3600) {
    const minutes = Math.floor(safeSeconds / 60);
    const remainder = safeSeconds % 60;
    return remainder > 0
      ? `약 ${minutes}분 ${remainder}초`
      : `약 ${minutes}분`;
  }
  const hours = Math.floor(safeSeconds / 3600);
  const minutes = Math.floor((safeSeconds % 3600) / 60);
  return minutes > 0
    ? `약 ${hours}시간 ${minutes}분`
    : `약 ${hours}시간`;
}
