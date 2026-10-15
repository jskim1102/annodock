import { useEffect, useId, useRef, useState, type PropsWithChildren } from "react";

import { createWorkspaceInvite, workspaceErrorMessage, type InviteCreated, type InvitableRole,
  type WorkspaceInviteRow } from "../api/workspaces";
import { WORKSPACE_ROLE_LABELS } from "../store/workspace";

export function WorkspaceDialog({ title, busy = false, onClose, children }: PropsWithChildren<{
  title: string; busy?: boolean; onClose: () => void;
}>) {
  const id = useId();
  const dialogRef = useRef<HTMLElement>(null);
  useEffect(() => {
    const previous = document.activeElement;
    dialogRef.current?.querySelector<HTMLElement>("button, input, select")?.focus();
    return () => { if (previous instanceof HTMLElement && previous.isConnected) previous.focus(); };
  }, []);
  useEffect(() => {
    const keydown = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !busy) { event.preventDefault(); onClose(); }
      if (event.key !== "Tab") return;
      const controls = dialogRef.current?.querySelectorAll<HTMLElement>(
        'button:not(:disabled), input:not(:disabled), select:not(:disabled), [tabindex="0"]',
      );
      if (!controls?.length) return;
      const first = controls[0], last = controls[controls.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    };
    window.addEventListener("keydown", keydown);
    return () => window.removeEventListener("keydown", keydown);
  }, [busy, onClose]);
  return <div className="dialog-backdrop" role="presentation" onMouseDown={(event) => {
    if (event.target === event.currentTarget && !busy) onClose();
  }}>
    <section className="dialog project-action-dialog invite-link-dialog" ref={dialogRef}
      role="dialog" aria-modal="true" aria-labelledby={id}>
      <h2 className="dialog-title" id={id}>{title}</h2>
      {children}
    </section>
  </div>;
}

export function InviteLinkDialog({ workspaceId, onClose, onCreated }: {
  workspaceId: number; onClose: () => void; onCreated: (invite: WorkspaceInviteRow) => void;
}) {
  const [role, setRole] = useState<InvitableRole>("editor");
  const [days, setDays] = useState(7);
  const [limited, setLimited] = useState(false);
  const [maxUses, setMaxUses] = useState("10");
  const [created, setCreated] = useState<InviteCreated | null>(null);
  const [busy, setBusy] = useState(false);
  const busyRef = useRef(false);
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const validLimit = /^\d+$/.test(maxUses) && Number(maxUses) >= 1 && Number(maxUses) <= 2_147_483_647;
  const create = async () => {
    if (busyRef.current || (limited && !validLimit)) return;
    busyRef.current = true; setBusy(true); setError(null);
    try {
      const response = await createWorkspaceInvite(workspaceId, {
        role, expires_in_days: days, max_uses: limited ? Number(maxUses) : null,
      });
      setCreated(response);
      // Only public metadata leaves this dialog. The raw URL disappears on close.
      onCreated(response.invite);
    } catch (reason: unknown) { setError(workspaceErrorMessage(reason)); }
    finally { busyRef.current = false; setBusy(false); }
  };
  const link = created ? `${window.location.origin}${created.accept_url}` : "";
  const copy = async () => {
    try { await navigator.clipboard.writeText(link); setCopied(true); setError(null); }
    catch { setError("복사하지 못했어요. 링크를 선택해 직접 복사해 주세요."); }
  };
  return <WorkspaceDialog title={created ? "링크가 만들어졌어요" : "초대 링크 만들기"} busy={busy} onClose={onClose}>
    {created ? <>
      <div className="invite-copy-row">
        <input className="input" aria-label="초대 링크" value={link} readOnly onFocus={(event) => event.target.select()} />
        <button type="button" className="btn btn-primary" onClick={() => void copy()}>복사</button>
      </div>
      {copied ? <p className="toast" role="status">복사했어요</p> : null}
      <p className="hint">이 창을 닫으면 링크를 다시 볼 수 없어요. 다시 필요하면 철회하고 새로 만드세요.</p>
      <p className="hint">{WORKSPACE_ROLE_LABELS[created.invite.role]} · {new Date(created.invite.expires_at).toLocaleDateString("ko-KR")} 만료 · {created.invite.max_uses === null ? "제한 없음" : `${created.invite.max_uses}회`}</p>
      <div className="dialog-actions"><button type="button" className="btn btn-secondary" onClick={onClose}>닫기</button></div>
    </> : <form onSubmit={(event) => { event.preventDefault(); void create(); }}>
      <div className="field"><span>역할</span><div className="seg" role="group" aria-label="초대 역할">
        {(["admin", "editor"] as const).map((value) => <button key={value} type="button"
          className={`seg-opt${role === value ? " is-active" : ""}`} aria-pressed={role === value}
          disabled={busy} onClick={() => setRole(value)}>{WORKSPACE_ROLE_LABELS[value]}</button>)}
      </div></div>
      <label className="field">만료<select className="select" value={days} disabled={busy}
        onChange={(event) => setDays(Number(event.target.value))}>
        {[1, 3, 7, 14, 30].map((value) => <option key={value} value={value}>{value}일</option>)}
      </select></label>
      <div className="invite-limit-row">
        <label><input className="checkbox" type="checkbox" checked={limited} disabled={busy}
          onChange={(event) => setLimited(event.target.checked)} /> 사용 횟수 제한</label>
        {limited ? <label><input className="input" type="number" min={1} max={2_147_483_647}
          aria-label="최대 사용 횟수" value={maxUses} disabled={busy}
          onChange={(event) => setMaxUses(event.target.value)} /> 회</label> : null}
      </div>
      <div className="dialog-actions">
        <button type="button" className="btn btn-secondary" disabled={busy} onClick={onClose}>취소</button>
        <button type="submit" className="btn btn-primary" disabled={busy || (limited && !validLimit)}>{busy ? "만드는 중…" : "만들기"}</button>
      </div>
    </form>}
    {error ? <p className="banner-danger" role="alert">{error}</p> : null}
  </WorkspaceDialog>;
}
