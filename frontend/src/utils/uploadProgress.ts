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
  windowStartedCompleted: number;
  windowStartedAtMs: number;
  smoothedRatePerSecond: number | null;
  stabilizedRemainingSeconds: number | null;
  estimateUpdatedAtMs: number;
  estimateCompleted: number;
}

export interface ProgressEstimate {
  state: ProgressEstimateState;
  remainingSeconds: number | null;
}

export interface UploadJobProgressSample {
  state: string;
  image_total: number;
  image_processed: number;
}

const RATE_WINDOW_MS = 5_000;
const RATE_SMOOTHING_WEIGHT = 0.2;
const ESTIMATE_UPDATE_INTERVAL_MS = 1_000;
const ETA_CORRECTION_WEIGHT = 0.2;
const MAX_CORRECTION_SECONDS_PER_SECOND = 2;

function normalizedSample(sample: ProgressSample) {
  const total = Math.max(0, sample.total);
  return {
    ...sample,
    total,
    completed: Math.min(total, Math.max(0, sample.completed)),
  };
}

function initialEstimate(sample: ProgressSample): ProgressEstimate {
  const isComplete = sample.total > 0 && sample.completed >= sample.total;
  return {
    state: {
      key: sample.key,
      total: sample.total,
      lastCompleted: sample.completed,
      lastSampleAtMs: sample.atMs,
      lastProgressAtMs: sample.atMs,
      windowStartedCompleted: sample.completed,
      windowStartedAtMs: sample.atMs,
      smoothedRatePerSecond: null,
      stabilizedRemainingSeconds: isComplete ? 0 : null,
      estimateUpdatedAtMs: sample.atMs,
      estimateCompleted: sample.completed,
    },
    remainingSeconds: isComplete ? 0 : null,
  };
}

function displayedRemaining(seconds: number | null): number | null {
  return seconds === null ? null : Math.max(1, Math.ceil(seconds));
}

export function uploadJobProcessingRatio(
  job: UploadJobProgressSample,
): number {
  if (job.state === "done") return 1;
  if (job.image_total <= 0) return 0;
  return Math.min(1, Math.max(0, job.image_processed / job.image_total));
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
        stabilizedRemainingSeconds: 0,
        estimateUpdatedAtMs: sample.atMs,
        estimateCompleted: sample.completed,
      },
      remainingSeconds: 0,
    };
  }

  const completedDelta = sample.completed - previous.lastCompleted;
  const windowDelta = sample.completed - previous.windowStartedCompleted;
  const windowElapsedMs = sample.atMs - previous.windowStartedAtMs;
  let smoothedRate = previous.smoothedRatePerSecond;
  let lastProgressAtMs = previous.lastProgressAtMs;
  let windowStartedCompleted = previous.windowStartedCompleted;
  let windowStartedAtMs = previous.windowStartedAtMs;
  if (completedDelta > 0) {
    lastProgressAtMs = sample.atMs;
  }
  if (windowDelta > 0 && windowElapsedMs >= RATE_WINDOW_MS) {
    const currentRate = windowDelta / (windowElapsedMs / 1000);
    smoothedRate = smoothedRate === null
      ? currentRate
      : smoothedRate * (1 - RATE_SMOOTHING_WEIGHT)
        + currentRate * RATE_SMOOTHING_WEIGHT;
    windowStartedCompleted = sample.completed;
    windowStartedAtMs = sample.atMs;
  }

  const state: ProgressEstimateState = {
    key: sample.key,
    total: sample.total,
    lastCompleted: sample.completed,
    lastSampleAtMs: sample.atMs,
    lastProgressAtMs,
    windowStartedCompleted,
    windowStartedAtMs,
    smoothedRatePerSecond: smoothedRate,
    stabilizedRemainingSeconds: previous.stabilizedRemainingSeconds,
    estimateUpdatedAtMs: previous.estimateUpdatedAtMs,
    estimateCompleted: previous.estimateCompleted,
  };
  if (smoothedRate === null || smoothedRate <= 0) {
    return {
      state,
      remainingSeconds: displayedRemaining(state.stabilizedRemainingSeconds),
    };
  }

  const estimateElapsedMs = sample.atMs - previous.estimateUpdatedAtMs;
  if (
    previous.stabilizedRemainingSeconds !== null
    && estimateElapsedMs < ESTIMATE_UPDATE_INTERVAL_MS
  ) {
    return {
      state,
      remainingSeconds: displayedRemaining(previous.stabilizedRemainingSeconds),
    };
  }

  const rawRemainingSeconds = (sample.total - sample.completed) / smoothedRate;
  let stabilizedRemainingSeconds = rawRemainingSeconds;
  if (previous.stabilizedRemainingSeconds !== null) {
    const madeProgress = sample.completed > previous.estimateCompleted;
    if (!madeProgress) {
      // A quiet server poll is not evidence that the job sped up or finished.
      // Keep the last useful answer instead of flickering back to "calculating".
      stabilizedRemainingSeconds = previous.stabilizedRemainingSeconds;
    } else {
      const elapsedSeconds = estimateElapsedMs / 1000;
      const countdown = Math.max(
        1,
        previous.stabilizedRemainingSeconds - elapsedSeconds,
      );
      const correction = (
        rawRemainingSeconds - countdown
      ) * ETA_CORRECTION_WEIGHT;
      const correctionLimit = (
        elapsedSeconds * MAX_CORRECTION_SECONDS_PER_SECOND
      );
      stabilizedRemainingSeconds = Math.max(
        1,
        countdown + Math.max(
          -correctionLimit,
          Math.min(correctionLimit, correction),
        ),
      );
    }
  }

  state.stabilizedRemainingSeconds = stabilizedRemainingSeconds;
  state.estimateUpdatedAtMs = sample.atMs;
  state.estimateCompleted = sample.completed;
  return {
    state,
    remainingSeconds: displayedRemaining(stabilizedRemainingSeconds),
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
