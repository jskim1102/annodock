import { useEffect, useRef, useState, type FormEvent } from "react";

import { ApiError } from "../api/client";
import { createWorkspace } from "../api/workspaces";
import { reloadWorkspaces, switchWorkspace } from "../store/workspace";
import { Icon } from "./Icon";

export function NewWorkspaceDialog({ onClose }: { onClose: () => void }) {
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const nameRef = useRef<HTMLInputElement>(null);
  const dialogRef = useRef<HTMLElement>(null);
  const pending = useRef(false);
  const mounted = useRef(true);
  const createdId = useRef<number | null>(null);

  useEffect(() => {
    mounted.current = true;
    const previous = document.activeElement as HTMLElement | null;
    nameRef.current?.focus();
    return () => { mounted.current = false; previous?.focus(); };
  }, []);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !pending.current) onClose();
      if (event.key !== "Tab") return;
      const controls = Array.from(dialogRef.current?.querySelectorAll<HTMLElement>('button:not(:disabled), input:not(:disabled), [tabindex="0"]') ?? []);
      const first = controls[0];
      const last = controls.at(-1);
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault(); last?.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault(); first?.focus();
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (pending.current || !name.trim()) return;
    pending.current = true;
    setBusy(true);
    setError(null);
    try {
      // A list-refresh failure must not create a second team when retried.
      if (createdId.current === null) createdId.current = (await createWorkspace(name.trim())).id;
      if (!mounted.current) return;
      await reloadWorkspaces();
      if (!mounted.current) return;
      switchWorkspace(createdId.current);
      onClose();
    } catch (failure: unknown) {
      if (!mounted.current) return;
      const detail = failure instanceof ApiError ? failure.detail as { code?: string; limit?: number } | undefined : undefined;
      setError(detail?.code === "team_limit_reached" && typeof detail.limit === "number"
        ? `팀은 최대 ${detail.limit}개까지 만들 수 있어요`
        : failure instanceof Error ? failure.message : "팀을 만들지 못했어요. 다시 시도해 주세요.");
    } finally {
      pending.current = false;
      if (mounted.current) setBusy(false);
    }
  };

  return (
    <div className="dialog-backdrop" role="presentation" onMouseDown={(event) => {
      if (event.target === event.currentTarget && !pending.current) onClose();
    }}>
      <section className="dialog new-workspace-dialog" role="dialog" aria-modal="true" aria-labelledby="new-workspace-title" ref={dialogRef}>
        <button className="btn btn-ghost btn-sm dialog-close" type="button" aria-label="닫기" disabled={busy} onClick={onClose}>
          <Icon name="x" size={16} />
        </button>
        <h2 className="dialog-title" id="new-workspace-title">새 팀 만들기</h2>
        <form onSubmit={submit} aria-busy={busy}>
          <div className="field">
            <label htmlFor="workspace-name">팀 이름</label>
            <input className="input" id="workspace-name" ref={nameRef} value={name} maxLength={255}
              disabled={busy || createdId.current !== null} placeholder="팀 이름 입력"
              onChange={(event) => setName(event.target.value)} />
          </div>
          {error ? <div className="banner banner-danger" role="alert">{error}</div> : null}
          <div className="dialog-actions">
            <button className="btn btn-secondary" type="button" disabled={busy} onClick={onClose}>취소</button>
            <button className="btn btn-primary" type="submit" disabled={busy || !name.trim()}>
              {busy ? "만드는 중…" : createdId.current !== null ? "다시 시도" : "만들기"}
            </button>
          </div>
        </form>
      </section>
    </div>
  );
}
