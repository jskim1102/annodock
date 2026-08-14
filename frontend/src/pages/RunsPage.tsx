import { Fragment, useEffect, useMemo, useRef, useState } from "react";

import {
  deleteRun,
  deleteRunArtifacts,
  getRuns,
  type RunState,
  type RunSummary,
} from "../api/training";
import { AppShell } from "../components/AppShell";
import { Icon } from "../components/Icon";
import { appHref } from "../navigation";

const ACTIVE_STATES = new Set<RunState>(["queued", "running", "canceling"]);

function RunStateTag({ state }: { state: RunState }) {
  if (state === "failed") return <span className="tag tag-danger"><span className="dot" />실패</span>;
  if (state === "canceled") return <span className="tag tag-neutral">취소됨</span>;
  if (state === "done") return <span className="tag tag-ok"><span className="dot" />완료</span>;
  if (state === "canceling") return <span className="tag tag-warn"><span className="dot" />취소 중</span>;
  return <span className="tag tag-accent"><span className="dot dot-pulse" />{state === "queued" ? "대기" : "실행 중"}</span>;
}

function duration(run: RunSummary) {
  if (!run.started_at) return "—";
  const end = run.finished_at ? new Date(run.finished_at).getTime() : Date.now();
  const minutes = Math.max(0, Math.round((end - new Date(run.started_at).getTime()) / 60000));
  return minutes >= 60 ? `${Math.floor(minutes / 60)}h ${minutes % 60}m` : `${minutes}m`;
}

interface DeleteRunsDialogProps {
  runs: RunSummary[];
  busy: boolean;
  error: string | null;
  onClose: () => void;
  onConfirm: () => void;
}

function DeleteRunsDialog({
  runs,
  busy,
  error,
  onClose,
  onConfirm,
}: DeleteRunsDialogProps) {
  const cancelRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    cancelRef.current?.focus();
  }, []);

  useEffect(() => {
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !busy) onClose();
    };
    window.addEventListener("keydown", closeOnEscape);
    return () => window.removeEventListener("keydown", closeOnEscape);
  }, [busy, onClose]);

  return (
    <div
      className="dialog-backdrop"
      role="presentation"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget && !busy) onClose();
      }}
    >
      <section
        className="dialog project-action-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="delete-runs-title"
        aria-describedby="delete-runs-warning"
      >
        <button
          className="btn btn-ghost btn-sm dialog-close"
          type="button"
          aria-label="닫기"
          disabled={busy}
          onClick={onClose}
        ><Icon name="x" size={16} /></button>
        <h2 className="dialog-title" id="delete-runs-title">run 삭제</h2>
        <div className="project-delete-warning" id="delete-runs-warning">
          <Icon name="warning" size={18} />
          <div>
            <strong>이 작업은 되돌릴 수 없습니다.</strong>
            <p>선택한 run 기록과 산출물이 모두 삭제됩니다.</p>
          </div>
        </div>
        <p className="project-delete-copy">
          원본 데이터셋은 유지됩니다. 삭제 후 학습 설정, 지표, 로그와 모델 파일은 다시 확인할 수 없습니다.
        </p>
        <div className="project-delete-list" aria-label="삭제 대상 run">
          <strong>삭제 대상 run {runs.length}개</strong>
          <ul>
            {runs.map((run) => (
              <li key={run.id}>run {run.id} · {run.dataset_name}</li>
            ))}
          </ul>
        </div>
        {error ? <div className="error-text project-dialog-error" role="alert">{error}</div> : null}
        <div className="dialog-actions">
          <button className="btn btn-secondary" type="button" disabled={busy} ref={cancelRef} onClick={onClose}>취소</button>
          <button className="btn btn-danger" type="button" disabled={busy} onClick={onConfirm}>
            {busy ? "삭제 중…" : "run 삭제"}
          </button>
        </div>
      </section>
    </div>
  );
}

export function RunsPage() {
  const [expandedId, setExpandedId] = useState<number | null>(null);
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [chip, setChip] = useState<"running" | "ended">("running");
  const [runs, setRuns] = useState<RunSummary[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [cleaning, setCleaning] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [deleteTargets, setDeleteTargets] = useState<RunSummary[] | null>(null);
  const [deleteError, setDeleteError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    let timer: number | undefined;
    const refresh = async () => {
      try {
        const response = await getRuns();
        if (!active) return;
        setRuns(response.items);
        setError(null);
        if (response.items.some((run) => ACTIVE_STATES.has(run.state))) {
          timer = window.setTimeout(() => void refresh(), 2500);
        }
      } catch (reason: unknown) {
        if (active) setError(reason instanceof Error ? reason.message : "run 목록을 불러오지 못했습니다.");
      }
    };
    void refresh();
    return () => {
      active = false;
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, []);

  const runningCount = runs.filter((run) => ACTIVE_STATES.has(run.state)).length;
  const endedCount = runs.length - runningCount;
  const visibleRuns = useMemo(() => runs.filter((run) => chip === "running"
    ? ACTIVE_STATES.has(run.state)
    : !ACTIVE_STATES.has(run.state)), [chip, runs]);

  const toggleSelected = (id: number) => {
    setSelected((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
  };

  const selectedRuns = runs.filter((run) => selected.has(run.id));
  const hasActiveSelection = selectedRuns.some((run) => ACTIVE_STATES.has(run.state));
  const cleanable = selectedRuns.filter((run) => !ACTIVE_STATES.has(run.state) && run.artifacts_deleted_at === null);

  const cleanupSelected = async () => {
    if (cleanable.length === 0 || cleaning || deleting) return;
    const targets = [...cleanable];
    const completedIds = new Set<number>();
    let cleanedCount = 0;
    setCleaning(true);
    setError(null);
    setNotice(null);
    try {
      for (const run of targets) {
        await deleteRunArtifacts(run.id);
        completedIds.add(run.id);
        cleanedCount += 1;
      }
      setNotice(`${cleanedCount}개 run의 산출물을 정리했습니다.`);
    } catch (reason: unknown) {
      const message = reason instanceof Error
        ? reason.message
        : "산출물을 정리하지 못했습니다.";
      setError(cleanedCount > 0
        ? `${cleanedCount}개 run의 산출물은 정리했지만 나머지는 처리하지 못했습니다. ${message}`
        : message);
    } finally {
      try {
        const response = await getRuns();
        setRuns(response.items);
      } catch (reason: unknown) {
        setError((current) => current ?? (reason instanceof Error
          ? reason.message
          : "정리 결과를 다시 불러오지 못했습니다."));
      }
      setSelected((current) => {
        if (completedIds.size === targets.length) return new Set();
        const next = new Set(current);
        for (const id of completedIds) next.delete(id);
        return next;
      });
      setCleaning(false);
    }
  };

  const deleteSelectedRuns = async () => {
    if (deleteTargets === null || deleteTargets.length === 0 || deleting) return;
    const targets = [...deleteTargets];
    const completedIds = new Set<number>();
    let deletedCount = 0;
    setDeleting(true);
    setDeleteError(null);
    setError(null);
    setNotice(null);
    try {
      for (const run of targets) {
        await deleteRun(run.id);
        completedIds.add(run.id);
        deletedCount += 1;
      }
      setNotice(`${deletedCount}개 run을 삭제했습니다.`);
    } catch (reason: unknown) {
      const message = reason instanceof Error
        ? reason.message
        : "run을 삭제하지 못했습니다.";
      setDeleteError(deletedCount > 0
        ? `${deletedCount}개 run은 삭제했지만 나머지는 처리하지 못했습니다. ${message}`
        : message);
    } finally {
      try {
        const response = await getRuns();
        setRuns(response.items);
      } catch (reason: unknown) {
        setError(reason instanceof Error
          ? reason.message
          : "삭제 결과를 다시 불러오지 못했습니다.");
      }
      setSelected((current) => {
        const next = new Set(current);
        for (const id of completedIds) next.delete(id);
        return next;
      });
      const remaining = targets.filter((run) => !completedIds.has(run.id));
      setDeleteTargets(remaining.length > 0 ? remaining : null);
      setDeleting(false);
    }
  };

  return (
    <AppShell active="runs" breadcrumb="AI 학습">
      <div className="page-heading-row runs-heading">
        <div>
          <h1>AI 학습</h1>
          <div className="run-filter-chips">
            <button className="chip" type="button" aria-pressed={chip === "running"} onClick={() => setChip("running")}>실행중 <b>{runningCount}</b></button>
            <button className="chip" type="button" aria-pressed={chip === "ended"} onClick={() => setChip("ended")}>종료 <b>{endedCount}</b></button>
          </div>
        </div>
        <div className="run-detail-actions">
          <button className="btn btn-danger btn-sm" type="button" disabled={cleanable.length === 0 || cleaning || deleting} onClick={() => void cleanupSelected()}><Icon name="trash" size={14} />{cleaning ? "정리 중…" : "선택한 산출물 정리"}</button>
          <button
            className="btn btn-danger btn-sm"
            type="button"
            disabled={selectedRuns.length === 0 || hasActiveSelection || cleaning || deleting}
            title={hasActiveSelection ? "대기 중이거나 실행 중인 run은 삭제할 수 없습니다." : undefined}
            onClick={() => {
              setDeleteError(null);
              setDeleteTargets([...selectedRuns]);
            }}
          ><Icon name="trash" size={14} />선택한 run 삭제</button>
        </div>
      </div>
      {error ? <p className="training-error" role="alert">{error}</p> : null}
      {notice ? <div className="run-notice" role="status">{notice}</div> : null}

      <section className="card runs-card">
        <table className="table runs-table">
          <thead><tr><th><span className="sr-only">선택</span></th><th>run</th><th>데이터셋</th><th>preset</th><th>진행</th><th className="num">소요</th></tr></thead>
          <tbody>
            {visibleRuns.map((run) => {
              const expanded = expandedId === run.id;
              const progress = run.epochs > 0 ? Math.min(100, Math.round(run.epoch / run.epochs * 100)) : 0;
              return <Fragment key={run.id}>
                <tr className={ACTIVE_STATES.has(run.state) ? "run-main-row" : undefined}>
                  <td><button className={`checkbox${selected.has(run.id) ? " is-on" : ""}`} role="checkbox" aria-checked={selected.has(run.id)} type="button" aria-label={`run ${run.id} 선택`} onClick={() => toggleSelected(run.id)}>{selected.has(run.id) ? <Icon name="check" size={10} /> : null}</button></td>
                  <td><button className="run-expand" type="button" aria-label={expanded ? "run 접기" : "run 펼치기"} aria-expanded={expanded} onClick={() => setExpandedId(expanded ? null : run.id)}><Icon name={expanded ? "chevron-down" : "chevron-right"} size={13} /></button>{ACTIVE_STATES.has(run.state) ? <span className="running-dot" /> : null}<a className="mono run-link" href={appHref(`/runs/${run.id}`)}>run {run.id}</a></td>
                  <td>{run.dataset_name}</td><td className="mono">{run.weights.replace(".pt", "")}</td>
                  <td>{ACTIVE_STATES.has(run.state) ? <span className="run-progress"><span className="bar"><i style={{ width: `${progress}%` }} /></span><span className="mono">{run.epoch} / {run.epochs}</span></span> : <span className="run-state-tags"><RunStateTag state={run.state} />{run.artifacts_deleted_at !== null ? <span className="tag tag-neutral">산출물 정리됨</span> : null}</span>}</td>
                  <td className="num">{duration(run)}</td>
                </tr>
                {expanded ? <tr className="subrow"><td /><td colSpan={5}><span className="run-dataset-indent">{run.dataset_name} <span>— dataset {run.dataset_id ?? "삭제됨"} · {run.weights} · {run.state}</span></span></td></tr> : null}
              </Fragment>;
            })}
            {visibleRuns.length === 0 ? <tr><td colSpan={6}>표시할 run이 없습니다.</td></tr> : null}
          </tbody>
        </table>
        <div className="table-footer"><span className="mono">총 {visibleRuns.length}개</span></div>
      </section>
      {deleteTargets ? (
        <DeleteRunsDialog
          runs={deleteTargets}
          busy={deleting}
          error={deleteError}
          onClose={() => {
            if (!deleting) {
              setDeleteTargets(null);
              setDeleteError(null);
            }
          }}
          onConfirm={() => void deleteSelectedRuns()}
        />
      ) : null}
    </AppShell>
  );
}
