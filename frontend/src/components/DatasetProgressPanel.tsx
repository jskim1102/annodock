import { useEffect, useRef, useState } from "react";

import { getDatasetProgress, distributeDatasetAssignments, reclaimDatasetAssignments, type DatasetProgress } from "../api/client";
import { getWorkspaceMembers, type WorkspaceMemberRow } from "../api/workspaces";
import { WorkspaceDialog } from "./InviteLinkDialog";

export function DatasetProgressBar({ progress }: { progress: DatasetProgress }) {
  const percent = (value: number) => progress.total ? Math.round(value / progress.total * 100) : 0;
  return <div className="bar-stack" role="img" aria-label={`완료 ${percent(progress.labeled)}%, 작업 중 ${percent(progress.in_progress)}%, 남음 ${percent(progress.unlabeled)}%`}>
    <span className="is-done" style={{ width: `${progress.total ? progress.labeled / progress.total * 100 : 0}%` }} />
    <span className="is-active" style={{ width: `${progress.total ? progress.in_progress / progress.total * 100 : 0}%` }} />
  </div>;
}

export function DatasetProgressCell({ dataset, workspaceId, revision, onOpen }: {
  dataset: {id: number; name: string}; workspaceId: number; revision: number; onOpen: () => void;
}) {
  const [progress, setProgress] = useState<DatasetProgress | null>(null);
  useEffect(() => {
    let active = true;
    setProgress(null);
    void getDatasetProgress(dataset.id, workspaceId).then((value) => { if (active) setProgress(value); }).catch(() => {});
    return () => { active = false; };
  }, [dataset.id, workspaceId, revision]);
  const summary = progress ? `${progress.labeled}/${progress.total} · ${progress.total ? Math.round(progress.labeled / progress.total * 100) : 0}%` : "—";
  return <div className="dataset-progress-cell">
    <div className="dataset-progress-summary">
      {progress ? <DatasetProgressBar progress={progress} /> : null}
      <span className="dataset-progress-count mono" title={summary}>{summary}</span>
    </div>
    <button className="btn btn-ghost btn-sm dataset-progress-open" onClick={onOpen} aria-label={`${dataset.name} 진행률`}>진행률 ›</button>
  </div>;
}

export function DatasetProgressPanel({ dataset, workspaceId, canManage, onClose, onChanged }: {
  dataset: {id: number; name: string}; workspaceId: number; canManage: boolean;
  onClose: () => void; onChanged?: () => void;
}) {
  const [progress, setProgress] = useState<DatasetProgress | null>(null);
  const [members, setMembers] = useState<WorkspaceMemberRow[]>([]);
  const [selected, setSelected] = useState<number[]>([]);
  const [revision, setRevision] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [toast, setToast] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const busyRef = useRef(false);
  const [reclaimUser, setReclaimUser] = useState<number | null>(null);
  const panelRef = useRef<HTMLElement>(null);
  const memberName = (id: number) => members.find((member) => member.user_id === id)?.username
    || members.find((member) => member.user_id === id)?.email || "이름 없음";
  useEffect(() => {
    const previous = document.activeElement;
    panelRef.current?.focus();
    return () => { if (previous instanceof HTMLElement && previous.isConnected) previous.focus(); };
  }, []);
  useEffect(() => {
    const keydown = (event: KeyboardEvent) => {
      if (event.key === "Escape" && reclaimUser === null && !busyRef.current) { event.preventDefault(); onClose(); }
    };
    window.addEventListener("keydown", keydown);
    return () => window.removeEventListener("keydown", keydown);
  }, [onClose, reclaimUser]);
  useEffect(() => {
    let active = true;
    setProgress(null); setError(null);
    void Promise.all([getDatasetProgress(dataset.id, workspaceId), getWorkspaceMembers(workspaceId)])
      .then(([value, response]) => {
        if (!active) return;
        setProgress(value); setMembers(response.items);
        setSelected((ids) => ids.filter((id) => response.items.some((member) => member.user_id === id && ["owner", "admin", "editor"].includes(member.role))));
      }).catch((reason: unknown) => { if (active) setError(reason instanceof Error ? reason.message : "진행률을 불러오지 못했어요"); });
    return () => { active = false; };
  }, [dataset.id, workspaceId, revision]);
  useEffect(() => { setSelected([]); setReclaimUser(null); setToast(null); }, [dataset.id, workspaceId, canManage]);
  const mutate = async (operation: () => Promise<void>) => {
    if (!canManage || busyRef.current) return;
    busyRef.current = true; setBusy(true); setError(null); setToast(null);
    try { await operation(); setRevision((value) => value + 1); onChanged?.(); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "요청하지 못했어요"); }
    finally { busyRef.current = false; setBusy(false); }
  };
  const distribute = () => {
    if (!selected.length) return;
    void mutate(async () => {
      const response = await distributeDatasetAssignments(dataset.id, selected, workspaceId);
      setToast(`${Object.entries(response.assigned).map(([id, count]) => `${memberName(Number(id))} ${count}장`).join(" · ")} 배정했어요`);
      setSelected([]);
    });
  };
  return <aside className="dataset-progress-panel" role="dialog" aria-modal="false" aria-label={`${dataset.name} 진행률`} tabIndex={-1} ref={panelRef}>
    <header><h2>{dataset.name} 진행률</h2><button className="btn btn-ghost" aria-label="진행률 닫기" disabled={busy} onClick={onClose}>✕</button></header>
    {error ? <div className="banner-danger" role="alert">{error}<button className="btn btn-ghost" disabled={busy} onClick={() => setRevision((value) => value + 1)}>다시 시도</button></div> : null}
    {toast ? <div className="toast" role="status">{toast}</div> : null}
    {progress ? <>
      <div className="stat-row">
        <div><span className="label">전체</span><strong className="value">{progress.total}</strong></div>
        <div><span className="label">완료</span><strong className="value">{progress.labeled}</strong><span className="sub">가져온 라벨 {progress.imported_labeled} 포함</span></div>
        <div><span className="label">작업 중</span><strong className="value">{progress.in_progress}</strong></div>
        <div><span className="label">남음</span><strong className="value">{progress.unlabeled}</strong></div>
      </div>
      <DatasetProgressBar progress={progress} />
      <div className="bar-legend"><span className="is-done">완료</span><span className="is-active">작업 중</span><span>남음</span></div>
      <table className="table"><thead><tr><th>멤버</th><th>작업 중</th><th>완료</th>{canManage ? <th>관리</th> : null}</tr></thead><tbody>
        {progress.members.map((member) => <tr key={member.user_id}><td>{memberName(member.user_id)}</td><td>{member.in_progress}</td><td>{member.labeled}</td>
          {canManage ? <td><button className="btn btn-ghost btn-sm" disabled={busy || member.in_progress === 0} onClick={() => setReclaimUser(member.user_id)}>회수</button></td> : null}</tr>)}
        <tr><td>가져온 라벨</td><td>—</td><td>{progress.imported_labeled}</td>{canManage ? <td /> : null}</tr>
      </tbody></table>
      {canManage ? <section><h3>남은 {progress.unlabeled}장을 나눠 맡기기</h3>
        <div className="assignment-members">{members.filter((member) => ["owner", "admin", "editor"].includes(member.role)).map((member) => <label key={member.user_id} className="assignment-member">
          <input type="checkbox" checked={selected.includes(member.user_id)} disabled={busy} onChange={(event) => setSelected((ids) => event.target.checked ? [...ids.filter((id) => id !== member.user_id), member.user_id] : ids.filter((id) => id !== member.user_id))} />
          <span>{memberName(member.user_id)}</span></label>)}</div>
        {selected.length ? <p>선택 {selected.length}명 → 1인당 약 {Math.ceil(progress.unlabeled / selected.length)}장</p> : null}
        <button className="btn btn-primary" disabled={busy || selected.length === 0} onClick={distribute}>배정하기</button>
      </section> : null}
    </> : !error ? <p role="status">진행률을 불러오는 중…</p> : null}
    {canManage && reclaimUser !== null ? <WorkspaceDialog title="배정을 회수할까요?" busy={busy} onClose={() => setReclaimUser(null)}>
      <p>{memberName(reclaimUser)} 님의 작업 중인 이미지를 미배정으로 돌립니다.</p>
      <div className="dialog-actions"><button className="btn btn-secondary" disabled={busy} onClick={() => setReclaimUser(null)}>취소</button>
        <button className="btn btn-primary" disabled={busy} onClick={() => void mutate(async () => {
          const response = await reclaimDatasetAssignments(dataset.id, reclaimUser, workspaceId);
          setReclaimUser(null); setToast(`${response.reclaimed}장을 회수했어요`);
        })}>확인</button></div>
    </WorkspaceDialog> : null}
  </aside>;
}
