import { useEffect, useMemo, useRef, useState } from "react";

import { imageResourceUrl, invalidateStorageQuotaCache } from "../api/client";
import {
  cancelRun,
  deleteRunArtifacts,
  getRun,
  getRunInferenceImages,
  getRunLog,
  getRunMetrics,
  inferRunImage,
  type InferenceImagePage,
  type RunDetail,
  type RunMetric,
  type RunState,
} from "../api/training";
import { AppShell, BreadcrumbLink } from "../components/AppShell";
import { AuthenticatedImage } from "../components/AuthenticatedImage";
import { Icon } from "../components/Icon";
import { SelectMenu } from "../components/SelectMenu";
import { downloadArtifact } from "../utils/download";
import {
  formatMetricValue,
  latestMetricValue,
  positionMetricValueLabels,
} from "../utils/runMetricChart";

const ACTIVE_STATES = new Set<RunState>(["queued", "running", "canceling"]);
const INFERENCE_PAGE_SIZE = 16;
const CHART_PLOT_LEFT = 38;
const CHART_PLOT_RIGHT = 374;
const CHART_VIEW_WIDTH = 440;
const CHART_VALUE_X = 414;
const CHART_VALUE_CONNECT_X = 388;
const CHART_VALUE_GAP = 11;
const CHART_TOP = 12;
const CHART_BOTTOM = 150;

type MetricKey = "box_loss" | "cls_loss" | "dfl_loss" | "map50" | "map5095";

interface MetricSeries {
  key: MetricKey;
  label: string;
  color: string;
}

const LOSS_SERIES: MetricSeries[] = [
  { key: "box_loss", label: "box", color: "var(--color-class-3)" },
  { key: "cls_loss", label: "cls", color: "var(--color-class-1)" },
  { key: "dfl_loss", label: "dfl", color: "var(--color-class-5)" },
];

const MAP_SERIES: MetricSeries[] = [
  { key: "map50", label: "mAP50", color: "var(--color-class-2)" },
  { key: "map5095", label: "mAP50-95", color: "var(--color-class-6)" },
];

function errorMessage(reason: unknown, fallback: string) {
  return reason instanceof Error ? reason.message : fallback;
}

function statusTag(state: RunState | null) {
  if (state === null) return <span className="tag tag-neutral">불러오는 중</span>;
  if (state === "failed") return <span className="tag tag-danger"><span className="dot" />실패</span>;
  if (state === "canceled") return <span className="tag tag-neutral">취소됨</span>;
  if (state === "done") return <span className="tag tag-ok"><span className="dot" />완료</span>;
  if (state === "canceling") return <span className="tag tag-warn"><span className="dot" />취소 중</span>;
  return <span className="tag tag-accent"><span className="dot dot-pulse" />{state === "queued" ? "대기" : "실행중"}</span>;
}

function formatDuration(milliseconds: number) {
  const minutes = Math.max(0, Math.round(milliseconds / 60_000));
  if (minutes < 60) return `${minutes}m`;
  return `${Math.floor(minutes / 60)}h ${minutes % 60}m`;
}

function timingText(run: RunDetail | null) {
  if (!run?.started_at) return run?.state === "queued" ? "시작 대기 중" : "소요 —";
  const end = run.finished_at ? new Date(run.finished_at).getTime() : Date.now();
  const elapsed = Math.max(0, end - new Date(run.started_at).getTime());
  if (!ACTIVE_STATES.has(run.state)) return `소요 ${formatDuration(elapsed)}`;
  if (run.epoch <= 0 || run.epochs <= 0) return `경과 ${formatDuration(elapsed)} · 예상 계산 중`;
  const estimatedTotal = elapsed * run.epochs / Math.min(run.epoch, run.epochs);
  return `경과 ${formatDuration(elapsed)} · 예상 ${formatDuration(estimatedTotal)}`;
}

function lossAxisMax(maxValue: number) {
  if (maxValue <= 0) return 1;
  const padded = maxValue * 1.05;
  const magnitude = 10 ** Math.floor(Math.log10(padded));
  const normalized = padded / magnitude;
  const rounded = normalized <= 1 ? 1 : normalized <= 2 ? 2 : normalized <= 4 ? 4 : normalized <= 5 ? 5 : 10;
  return rounded * magnitude;
}

function chartModel(metrics: RunMetric[], series: MetricSeries[], type: "loss" | "map", totalEpochs: number) {
  const values = metrics.flatMap((metric) => series.flatMap(({ key }) => {
    const value = metric[key];
    return value !== null && Number.isFinite(value) ? [value] : [];
  }));
  const lastEpoch = metrics.at(-1)?.epoch ?? 0;
  const xMax = Math.max(1, totalEpochs, lastEpoch);
  const maxValue = values.length > 0 ? Math.max(...values) : 0;
  const yMax = type === "map" ? Math.max(1, maxValue) : lossAxisMax(maxValue);
  const chartWidth = CHART_PLOT_RIGHT - CHART_PLOT_LEFT;
  const chartHeight = CHART_BOTTOM - CHART_TOP;

  const lines = series.map((entry) => {
    const points = metrics.flatMap((metric) => {
      const value = metric[entry.key];
      if (value === null || !Number.isFinite(value)) return [];
      const x = CHART_PLOT_LEFT + metric.epoch / xMax * chartWidth;
      const y = CHART_BOTTOM - value / yMax * chartHeight;
      return [{ x, y }];
    });
    return { ...entry, points, currentValue: latestMetricValue(metrics, entry.key) };
  });

  return { lines, xMax, yMax };
}

function ChartCard({ type, metrics, totalEpochs }: { type: "loss" | "map"; metrics: RunMetric[]; totalEpochs: number }) {
  const loss = type === "loss";
  const series = loss ? LOSS_SERIES : MAP_SERIES;
  const { lines, xMax, yMax } = useMemo(
    () => chartModel(metrics, series, type, totalEpochs),
    [metrics, series, type, totalEpochs],
  );
  const hasPoints = lines.some((line) => line.points.length > 0);
  const yTicks = Array.from({ length: 5 }, (_, index) => ({
    value: yMax * (4 - index) / 4,
    y: CHART_TOP + index * (CHART_BOTTOM - CHART_TOP) / 4,
  }));
  const xTicks = Array.from(new Set(Array.from(
    { length: 6 },
    (_, index) => Math.round(xMax * index / 5),
  )));
  const currentValuePositions = useMemo(() => positionMetricValueLabels(
    lines.flatMap((line) => {
      const current = line.points.at(-1);
      return current && line.currentValue !== null ? [{ key: line.key, y: current.y }] : [];
    }),
    CHART_TOP,
    CHART_BOTTOM,
    CHART_VALUE_GAP,
  ), [lines]);

  return (
    <section className="card chart-card">
      <header className="chart-card-header">
        <div className="chart-card-heading">
          <h2>{loss ? "loss" : "mAP (valid)"}</h2>
          <p>{loss ? "Training loss by epoch" : "Validation mAP by epoch"}</p>
        </div>
        <div className="chart-legend">
          {series.map(({ label, color }) => <span key={label}><i style={{ background: color }} />{label}</span>)}
        </div>
      </header>
      <div className="chart-latest">
        <span>Latest</span>
        <div className="chart-latest-values">
          {series.map(({ key, label, color }) => {
            const currentValue = latestMetricValue(metrics, key);
            return (
              <span className="chart-latest-value" key={key}>
                <i style={{ background: color }} />
                <span>{label}</span>
                <strong className="mono">{currentValue === null ? "—" : formatMetricValue(currentValue)}</strong>
              </span>
            );
          })}
        </div>
      </div>
      <svg className="metric-chart" viewBox="0 0 440 190" role="img" aria-label={loss ? "실제 loss 차트" : "실제 mAP 차트"}>
        {yTicks.map(({ value, y }) => (
          <g key={`y-${value}`}>
            <line className="chart-grid-line" x1={CHART_PLOT_LEFT} x2={CHART_PLOT_RIGHT} y1={y} y2={y} />
            <text className="chart-axis-label" x={CHART_PLOT_LEFT - 8} y={y} textAnchor="end" dominantBaseline="middle">
              {value.toFixed(loss ? 1 : 2)}
            </text>
          </g>
        ))}
        {xTicks.map((value) => {
          const x = CHART_PLOT_LEFT + value / xMax * (CHART_PLOT_RIGHT - CHART_PLOT_LEFT);
          return <text className="chart-axis-label" x={x} y={CHART_BOTTOM + 18} textAnchor="middle" key={`x-${value}`}>{value}</text>;
        })}
        <text className="chart-axis-title" x={(CHART_PLOT_LEFT + CHART_PLOT_RIGHT) / 2} y="187" textAnchor="middle">epoch</text>
        {lines.map((line) => line.points.length > 1
          ? <polyline key={line.key} points={line.points.map(({ x, y }) => `${x},${y}`).join(" ")} stroke={line.color} />
          : line.points.length === 1
            ? <circle key={line.key} cx={line.points[0].x} cy={line.points[0].y} r="3" fill={line.color} />
            : null)}
        {lines.map((line) => {
          const point = line.points.at(-1);
          const labelY = currentValuePositions.find((position) => position.key === line.key)?.labelY;
          const current = point && labelY !== undefined && line.currentValue !== null
            ? { ...point, labelY, value: line.currentValue }
            : null;
          return current ? (
            <g key={`${line.key}-current`}>
              <circle className="chart-current-point" cx={current.x} cy={current.y} r="3.5" fill={line.color} />
              <path className="chart-current-connector" d={`M${current.x + 3} ${current.y}L${CHART_VALUE_CONNECT_X} ${current.labelY}`} stroke={line.color} />
              <rect className="chart-current-badge" x={CHART_VALUE_CONNECT_X} y={current.labelY - 6} width="50" height="12" rx="2" stroke={line.color} />
              <text className="chart-current-value" x={CHART_VALUE_X} y={current.labelY} textAnchor="middle" dominantBaseline="middle" fill={line.color}>
                {formatMetricValue(current.value)}
              </text>
            </g>
          ) : null;
        })}
        {!hasPoints ? <text x={CHART_VIEW_WIDTH / 2} y="90" textAnchor="middle" fill="var(--color-muted)" fontSize="11">측정값 없음</text> : null}
      </svg>
    </section>
  );
}

interface ParsedLogLine {
  key: string;
  timestamp: string;
  text: string;
  isError: boolean;
}

function parseLog(log: string): ParsedLogLine[] {
  return log.split(/\r?\n/).flatMap((line, index) => {
    if (line.length === 0) return [];
    const timestamp = line.match(/^\[?(\d{2}:\d{2}:\d{2})\]?\s*(.*)$/);
    const text = timestamp?.[2] ?? line;
    return [{
      key: `${index}-${line}`,
      timestamp: timestamp?.[1] ?? "",
      text,
      isError: /warning|error|traceback|fail/i.test(text),
    }];
  });
}

export function RunDetailPage({ runId }: { runId: number }) {
  const [run, setRun] = useState<RunDetail | null>(null);
  const [metrics, setMetrics] = useState<RunMetric[]>([]);
  const [log, setLog] = useState("");
  const [loadError, setLoadError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [follow, setFollow] = useState(true);
  const [refreshVersion, setRefreshVersion] = useState(0);
  const [canceling, setCanceling] = useState(false);
  const [downloading, setDownloading] = useState(false);
  const [cleaning, setCleaning] = useState(false);
  const consoleRef = useRef<HTMLDivElement>(null);
  const observedRunStateRef = useRef<RunState | null>(null);

  const [inferencePage, setInferencePage] = useState<InferenceImagePage | null>(null);
  const [inferenceTotal, setInferenceTotal] = useState<number | null>(null);
  const [inferenceCursors, setInferenceCursors] = useState<Array<number | null>>([null]);
  const [inferencePageIndex, setInferencePageIndex] = useState(0);
  const [selectedImage, setSelectedImage] = useState(0);
  const [inferenceLoading, setInferenceLoading] = useState(false);
  const [inferenceError, setInferenceError] = useState<string | null>(null);
  const [inferenceImageUrl, setInferenceImageUrl] = useState<string | null>(null);
  const [predictionLoading, setPredictionLoading] = useState(false);
  const [inferenceZoom, setInferenceZoom] = useState(100);
  const pendingInferenceSelectionRef = useRef<"first" | "last">("first");

  useEffect(() => {
    setRun(null);
    setMetrics([]);
    setLog("");
    setLoadError(null);
    setActionError(null);
    setNotice(null);
    observedRunStateRef.current = null;
  }, [runId]);

  useEffect(() => {
    let active = true;
    let timer: number | undefined;
    let lastKnownActive = false;

    const refresh = async () => {
      const [runResult, metricsResult, logResult] = await Promise.allSettled([
        getRun(runId),
        getRunMetrics(runId),
        getRunLog(runId),
      ]);
      if (!active) return;

      const errors: string[] = [];
      if (runResult.status === "fulfilled") {
        const previousState = observedRunStateRef.current;
        const nextState = runResult.value.state;
        if (
          previousState !== null
          && ACTIVE_STATES.has(previousState)
          && !ACTIVE_STATES.has(nextState)
        ) {
          invalidateStorageQuotaCache();
        }
        observedRunStateRef.current = nextState;
        setRun(runResult.value);
        lastKnownActive = ACTIVE_STATES.has(nextState);
      } else {
        errors.push(errorMessage(runResult.reason, "run 정보를 불러오지 못했습니다."));
      }
      if (metricsResult.status === "fulfilled") {
        setMetrics(metricsResult.value);
      } else {
        errors.push(errorMessage(metricsResult.reason, "학습 metrics를 불러오지 못했습니다."));
      }
      if (logResult.status === "fulfilled") {
        setLog(logResult.value);
      } else {
        errors.push(errorMessage(logResult.reason, "학습 로그를 불러오지 못했습니다."));
      }
      setLoadError(errors[0] ?? null);

      if (lastKnownActive) {
        timer = window.setTimeout(() => void refresh(), 2500);
      }
    };

    void refresh();
    return () => {
      active = false;
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [refreshVersion, runId]);

  const parsedLogs = useMemo(() => parseLog(log), [log]);
  const visibleLogs = useMemo(() => {
    const query = search.trim().toLowerCase();
    return query.length === 0
      ? parsedLogs
      : parsedLogs.filter((row) => `${row.timestamp} ${row.text}`.toLowerCase().includes(query));
  }, [parsedLogs, search]);

  useEffect(() => {
    if (!follow || !consoleRef.current) return;
    consoleRef.current.scrollTop = consoleRef.current.scrollHeight;
  }, [follow, visibleLogs]);

  const canInfer = run?.id === runId
    && run.state === "done"
    && run.artifacts_deleted_at === null;

  useEffect(() => {
    setInferencePage(null);
    setInferenceTotal(null);
    setInferenceCursors([null]);
    setInferencePageIndex(0);
    setSelectedImage(0);
    setInferenceError(null);
    setInferenceZoom(100);
    pendingInferenceSelectionRef.current = "first";
  }, [canInfer, runId]);

  const inferenceCursor = inferenceCursors[inferencePageIndex] ?? null;

  useEffect(() => {
    if (!canInfer) return;
    let active = true;
    setInferenceLoading(true);
    setInferenceError(null);
    setInferencePage(null);
    void getRunInferenceImages(runId, inferenceCursor, INFERENCE_PAGE_SIZE)
      .then((page) => {
        if (!active) return;
        setInferencePage(page);
        if (page.total !== null) setInferenceTotal(page.total);
        setSelectedImage(
          pendingInferenceSelectionRef.current === "last"
            ? Math.max(0, page.items.length - 1)
            : 0,
        );
        pendingInferenceSelectionRef.current = "first";
      })
      .catch((reason: unknown) => {
        if (active) setInferenceError(errorMessage(reason, "추론 이미지 목록을 불러오지 못했습니다."));
      })
      .finally(() => {
        if (active) setInferenceLoading(false);
      });
    return () => { active = false; };
  }, [canInfer, inferenceCursor, runId]);

  const selectedInferenceImage = inferencePage?.items[selectedImage];

  useEffect(() => {
    setInferenceZoom(100);
  }, [selectedInferenceImage?.id]);

  useEffect(() => {
    if (!canInfer || !selectedInferenceImage) {
      setInferenceImageUrl(null);
      setPredictionLoading(false);
      return;
    }
    let active = true;
    let objectUrl: string | null = null;
    setPredictionLoading(true);
    setInferenceImageUrl(null);
    setInferenceError(null);
    void inferRunImage(runId, selectedInferenceImage.id)
      .then((blob) => {
        objectUrl = URL.createObjectURL(blob);
        if (!active) {
          URL.revokeObjectURL(objectUrl);
          objectUrl = null;
          return;
        }
        setInferenceImageUrl(objectUrl);
      })
      .catch((reason: unknown) => {
        if (active) setInferenceError(errorMessage(reason, "이미지 추론에 실패했습니다."));
      })
      .finally(() => {
        if (active) setPredictionLoading(false);
      });
    return () => {
      active = false;
      if (objectUrl !== null) URL.revokeObjectURL(objectUrl);
    };
  }, [canInfer, runId, selectedInferenceImage]);

  const progress = run && run.epochs > 0
    ? Math.min(100, Math.max(0, Math.round(run.epoch / run.epochs * 100)))
    : 0;
  const canCancel = run !== null && (run.state === "queued" || run.state === "running");
  const canDownload = run?.state === "done" && run.artifacts_deleted_at === null;
  const canClean = run !== null
    && !ACTIVE_STATES.has(run.state)
    && run.artifacts_deleted_at === null;

  const handleCancel = async () => {
    if (!canCancel || canceling) return;
    setCanceling(true);
    setActionError(null);
    setNotice(null);
    setRun((current) => current === null ? null : { ...current, state: "canceling" });
    try {
      const result = await cancelRun(runId);
      setRun((current) => current === null ? null : { ...current, state: result.state });
      setNotice("학습을 취소했습니다.");
      setRefreshVersion((value) => value + 1);
    } catch (reason: unknown) {
      setActionError(errorMessage(reason, "학습을 취소하지 못했습니다."));
    } finally {
      setCanceling(false);
    }
  };

  const handleDownload = async () => {
    if (!canDownload || downloading) return;
    setDownloading(true);
    setActionError(null);
    setNotice(null);
    try {
      await downloadArtifact(runId, "best.pt");
      setNotice("best.pt 다운로드를 시작했습니다.");
    } catch (reason: unknown) {
      setActionError(errorMessage(reason, "best.pt를 다운로드하지 못했습니다."));
    } finally {
      setDownloading(false);
    }
  };

  const handleCleanup = async () => {
    if (!canClean || cleaning) return;
    setCleaning(true);
    setActionError(null);
    setNotice(null);
    try {
      await deleteRunArtifacts(runId);
      invalidateStorageQuotaCache();
      setRun((current) => current === null ? null : {
        ...current,
        artifacts_deleted_at: new Date().toISOString(),
      });
      setNotice("산출물을 정리했습니다.");
      setRefreshVersion((value) => value + 1);
    } catch (reason: unknown) {
      setActionError(errorMessage(reason, "산출물을 정리하지 못했습니다."));
    } finally {
      setCleaning(false);
    }
  };

  const previousInferencePage = () => {
    if (inferenceLoading || inferencePageIndex === 0) return;
    pendingInferenceSelectionRef.current = "last";
    setInferencePageIndex((value) => value - 1);
  };

  const nextInferencePage = () => {
    if (inferenceLoading || inferencePage?.next_cursor === null || inferencePage?.next_cursor === undefined) return;
    const nextCursor = inferencePage.next_cursor;
    pendingInferenceSelectionRef.current = "first";
    setInferenceCursors((current) => [
      ...current.slice(0, inferencePageIndex + 1),
      nextCursor,
    ]);
    setInferencePageIndex((value) => value + 1);
  };

  const pageStart = inferencePage && inferencePage.items.length > 0
    ? inferencePageIndex * INFERENCE_PAGE_SIZE + 1
    : 0;
  const pageEnd = inferencePage ? pageStart + inferencePage.items.length - 1 : 0;
  const selectedPosition = pageStart + selectedImage;
  const hasPreviousInference = selectedImage > 0 || inferencePageIndex > 0;
  const hasNextInference = inferencePage !== null && (
    selectedImage < inferencePage.items.length - 1
    || inferencePage.next_cursor !== null
  );
  const inferenceProgress = inferenceTotal !== null && inferenceTotal > 1
    ? Math.max(0, Math.min(100, (selectedPosition - 1) / (inferenceTotal - 1) * 100))
    : 0;

  const navigateInferenceImage = (offset: -1 | 1) => {
    if (!inferencePage || inferenceLoading || predictionLoading) return;
    if (offset === -1) {
      if (selectedImage > 0) {
        setSelectedImage((value) => value - 1);
      } else {
        previousInferencePage();
      }
      return;
    }
    if (selectedImage < inferencePage.items.length - 1) {
      setSelectedImage((value) => value + 1);
    } else {
      nextInferencePage();
    }
  };

  return (
    <AppShell
      active="runs"
      breadcrumb={<><BreadcrumbLink href="/runs">AI 학습</BreadcrumbLink><span>/</span><strong className="mono">run {runId}</strong></>}
    >
      <div className="run-title-row">
        <h1 className="mono">run {runId}</h1>
        {statusTag(run?.state ?? null)}
      </div>

      {loadError ? <div className="run-notice" role="alert">{loadError}</div> : null}
      {actionError ? <div className="run-notice" role="alert">{actionError}</div> : null}
      {run?.error ? <div className="run-notice" role="alert">{run.error}</div> : null}
      {notice ? <div className="run-notice" role="status">{notice}</div> : null}

      <section className="card run-progress-card">
        <div className="run-progress-copy">
          <strong className="mono"><b>{run?.epoch ?? 0}</b> / {run?.epochs ?? 0} epoch</strong>
          <div className="run-progress-line"><span className="bar"><i style={{ width: `${progress}%` }} /></span><span className="mono">{progress}%</span></div>
          <span>{timingText(run)}</span>
        </div>
        <div className="run-detail-actions">
          <button className="btn btn-secondary" type="button" disabled={!canCancel || canceling} onClick={() => void handleCancel()}>{canceling ? "취소 중…" : "취소"}</button>
          <button className="btn btn-primary" type="button" disabled={!canDownload || downloading} onClick={() => void handleDownload()}><Icon name="download" size={14} />{downloading ? "다운로드 중…" : "best.pt 다운로드"}</button>
          <button className="btn btn-ghost" type="button" disabled={!canClean || cleaning} onClick={() => void handleCleanup()}><Icon name="broom" size={14} />{run?.artifacts_deleted_at ? "정리됨" : cleaning ? "정리 중…" : "산출물 정리"}</button>
        </div>
      </section>

      <div className="chart-grid-layout">
        <ChartCard type="loss" metrics={metrics} totalEpochs={run?.epochs ?? 0} />
        <ChartCard type="map" metrics={metrics} totalEpochs={run?.epochs ?? 0} />
      </div>

      <section className="card log-card">
        <header className="log-toolbar">
          <h2>로그 <span>(마지막 200줄)</span></h2>
          <SelectMenu className="log-tail-select" ariaLabel="로그 범위" value="200" options={[{ value: "200", label: "마지막 200줄" }]} disabled />
          <label className="log-search"><Icon name="search" size={14} /><span className="sr-only">로그 검색</span><input className="input" placeholder="로그 검색" value={search} onChange={(event) => setSearch(event.target.value)} /></label>
          <button className="chip" type="button" aria-pressed={follow} onClick={() => setFollow((current) => !current)}><span className="follow-dot" />따라가기</button>
        </header>
        <div className="console" aria-live="polite" ref={consoleRef}>
          {visibleLogs.map((row) => <div className={row.isError ? "line-error" : undefined} key={row.key}><span className="ts">{row.timestamp}</span>{row.text}</div>)}
          {visibleLogs.length === 0 ? <div>{search.trim() ? "검색 결과가 없습니다." : "로그가 없습니다."}</div> : null}
        </div>
      </section>

      {canInfer ? <section className="inference-section" aria-labelledby="inference-title">
        <header className="inference-heading">
          <div><h2 id="inference-title">best.pt 추론</h2><p>{inferencePage?.split ?? (run?.split_mode === "3way" ? "test" : "valid")} 분할 이미지를 선택하면 실제 bbox 추론 결과를 표시합니다</p></div>
          <div className="inference-selection-meta"><span className="mono">{selectedPosition || 0} / {inferenceTotal ?? "—"}</span>{selectedInferenceImage ? <span>{selectedInferenceImage.filename}</span> : null}</div>
        </header>
        {inferenceError ? <div className="run-notice" role="alert">{inferenceError}</div> : null}
        <article className="card inference-viewer" aria-busy={inferenceLoading || predictionLoading}>
          <div className="inference-stage" aria-busy={predictionLoading}>
            <button className="inference-arrow left" type="button" aria-label="이전 이미지" disabled={!hasPreviousInference || inferenceLoading || predictionLoading} onClick={() => navigateInferenceImage(-1)}><Icon name="chevron-left" size={18} /></button>
            <div className="inference-image-viewport">
              {inferenceImageUrl && selectedInferenceImage ? <img className="inference-result-image" src={inferenceImageUrl} alt={`${selectedInferenceImage.filename} best.pt 추론 결과`} style={{ transform: `scale(${inferenceZoom / 100})` }} /> : <span>{predictionLoading || inferenceLoading ? "추론 중…" : "추론 결과를 불러오지 못했습니다."}</span>}
            </div>
            <button className="inference-arrow right" type="button" aria-label="다음 이미지" disabled={!hasNextInference || inferenceLoading || predictionLoading} onClick={() => navigateInferenceImage(1)}><Icon name="chevron-right" size={18} /></button>
            <div className="zoom-pill inference-zoom-pill">
              <button type="button" aria-label="축소" disabled={inferenceZoom <= 50} onClick={() => setInferenceZoom((value) => Math.max(50, value - 10))}>−</button>
              <span className="mono">{inferenceZoom}%</span>
              <button type="button" aria-label="확대" disabled={inferenceZoom >= 200} onClick={() => setInferenceZoom((value) => Math.min(200, value + 10))}>＋</button>
              <span className="zoom-divider" />
              <button type="button" onClick={() => setInferenceZoom(100)}>화면 초기화</button>
            </div>
          </div>
          <div className="inference-timeline">
            <div className="inference-scrub" role="progressbar" aria-label="추론 이미지 위치" aria-valuemin={1} aria-valuemax={(inferenceTotal ?? pageEnd) || 1} aria-valuenow={selectedPosition || 1}>
              <i style={{ width: `${inferenceProgress}%` }} />
              <b style={{ left: `${inferenceProgress}%` }} />
            </div>
            <div className="inference-thumb-strip">
              {inferencePage?.items.map((item, index) => (
                <button
                  type="button"
                  className={`inference-thumb${selectedImage === index ? " is-selected" : ""}`}
                  key={item.id}
                  aria-label={`${item.filename} 추론`}
                  aria-current={selectedImage === index ? "true" : undefined}
                  onClick={() => setSelectedImage(index)}
                ><AuthenticatedImage resourcePath={imageResourceUrl(item.image_id, "thumb")} alt="" /></button>
              ))}
            </div>
          </div>
        </article>
      </section> : null}
    </AppShell>
  );
}
