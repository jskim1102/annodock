import { useEffect, useRef, useState } from "react";

import { ApiError, getStorageQuota, invalidateStorageQuotaCache, transferProject,
  type ProjectRow, type ProjectTransferResult, type StorageQuota } from "../api/client";
import { useAuthSession } from "../store/auth";
import { useWorkspace, WORKSPACE_ROLE_LABELS, type WorkspaceRow } from "../store/workspace";
import { formatBytes } from "../utils/formatBytes";
import { WorkspaceDialog } from "./InviteLinkDialog";

function transferError(reason: unknown, targetName: string): { message: string; names: string[] } {
  const detail = reason instanceof ApiError && reason.detail && typeof reason.detail === "object"
    ? reason.detail as Record<string, unknown> : {};
  const messages: Record<string, string> = {
    transfer_shared_media: "옮길 수 없어요 — 다른 데이터셋과 같은 이미지를 함께 쓰고 있어요.",
    transfer_upload_active: "업로드 중인 데이터셋이 있어요. 끝난 뒤 다시 시도하세요.",
    transfer_run_active: "학습이 진행 중이에요. 끝나거나 취소한 뒤 다시 시도하세요.",
    transfer_name_conflict: `${targetName}에 같은 이름이 있어요. 이름을 바꾼 뒤 다시 시도하세요.`,
    transfer_same_workspace: "같은 워크스페이스로는 옮길 수 없어요.",
    transfer_quota_exceeded: `${targetName} 용량이 부족해요.`,
  };
  const names = detail.code === "transfer_shared_media" && Array.isArray(detail.datasets)
    ? detail.datasets.flatMap((row: unknown) => row && typeof row === "object" && "name" in row
      && typeof row.name === "string" ? [row.name] : [])
    : Array.isArray(detail.names) ? detail.names.filter((name): name is string => typeof name === "string") : [];
  return {
    message: (typeof detail.code === "string" && messages[detail.code])
      || (reason instanceof Error ? reason.message : "프로젝트를 옮기지 못했어요. 다시 시도해 주세요."),
    names,
  };
}

export function ProjectTransferDialog({ project, onClose, onTransferred }: {
  project: ProjectRow;
  onClose: () => void;
  onTransferred: (result: ProjectTransferResult, target: WorkspaceRow) => void;
}) {
  const { active, items } = useWorkspace();
  const session = useAuthSession();
  const [source] = useState(active);
  const targets = items.filter((item) => item.id !== source?.id && ["owner", "admin"].includes(item.role));
  const [targetId, setTargetId] = useState(targets[0]?.id ?? 0);
  const target = targets.find((item) => item.id === targetId);
  const [quota, setQuota] = useState<{ workspaceId: number; value: StorageQuota } | null>(null);
  const [quotaError, setQuotaError] = useState<string | null>(null);
  const [quotaAttempt, setQuotaAttempt] = useState(0);
  const [error, setError] = useState<ReturnType<typeof transferError> | null>(null);
  const [busy, setBusy] = useState(false);
  const busyRef = useRef(false);
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; };
  }, []);
  useEffect(() => {
    let current = true;
    setQuota(null); setQuotaError(null); setError(null);
    if (targetId && session?.accessToken) {
      void getStorageQuota(session.accessToken, targetId).then((value) => {
        if (current) setQuota({ workspaceId: targetId, value });
      }).catch(() => {
        if (current) setQuotaError("대상 워크스페이스의 용량을 불러오지 못했어요.");
      });
    }
    return () => { current = false; };
  }, [targetId, session?.accessToken, quotaAttempt]);

  const datasets = new Map(project.datasets.flatMap((row) => [row, ...row.source_datasets])
    .map((row) => [row.id, row]));
  const estimatedBytes = [...datasets.values()].reduce((sum, row) => sum + row.storage_bytes, 0);
  const targetQuota = quota?.workspaceId === targetId ? quota.value : null;
  const canSubmit = !!source && source.id === active?.id && active.role === "owner" && !!target && !!targetQuota;
  const close = () => { if (!busyRef.current) onClose(); };
  const submit = async () => {
    if (busyRef.current || !canSubmit || !source || !target) return;
    busyRef.current = true; setBusy(true); setError(null);
    try {
      const result = await transferProject(project.id, target.id, source.id);
      invalidateStorageQuotaCache();
      if (mounted.current) onTransferred(result, target);
    } catch (reason: unknown) {
      if (mounted.current) setError(transferError(reason, target.name));
    } finally {
      busyRef.current = false;
      if (mounted.current) setBusy(false);
    }
  };

  return <WorkspaceDialog title="프로젝트 옮기기" busy={busy} onClose={close}>
    <form onSubmit={(event) => { event.preventDefault(); void submit(); }}>
      <p>{project.name} · {source?.name}</p>
      <label className="field">대상 워크스페이스
        <select className="select" value={targetId} disabled={busy || targets.length === 0}
          onChange={(event) => setTargetId(Number(event.target.value))}>
          {targets.map((item) => <option key={item.id} value={item.id}>
            {item.name} · {WORKSPACE_ROLE_LABELS[item.role]}
          </option>)}
        </select>
      </label>
      {!targets.length ? <p className="hint">옮길 수 있는 팀이 없어요</p> : null}
      <p>이동 용량: 약 {formatBytes(estimatedBytes)}</p>
      <p>대상 잔여 용량: {targetQuota
        ? formatBytes(Math.max(0, targetQuota.limit_bytes - targetQuota.used_bytes)) : "확인 중…"}</p>
      {quotaError ? <div className="banner-danger" role="alert">{quotaError}
        <button className="btn btn-secondary btn-sm" type="button" onClick={() => setQuotaAttempt((value) => value + 1)}>다시 시도</button>
      </div> : null}
      <p className="hint">복사가 아니라 이동이에요 — {source?.name}에서는 사라져요.</p>
      <p className="hint">옮긴 뒤에는 {target?.name ?? "대상 워크스페이스"} 멤버가 역할에 따라 보고 고칠 수 있어요.</p>
      {error ? <div className="banner-danger" role="alert">{error.message}
        {error.names.length ? <ul>{error.names.map((name, index) => <li key={`${index}:${name}`}>{name}</li>)}</ul> : null}
      </div> : null}
      <div className="dialog-actions">
        <button type="button" className="btn btn-secondary" disabled={busy} onClick={close}>취소</button>
        <button type="submit" className="btn btn-primary" disabled={busy || !canSubmit}>{busy ? "옮기는 중…" : "옮기기"}</button>
      </div>
    </form>
  </WorkspaceDialog>;
}
