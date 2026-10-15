import { useEffect, useRef, useState } from "react";

import { ApiError } from "../api/client";
import { acceptInvite, getInvitePreview, type InvitePreview } from "../api/workspaces";
import { Brand } from "../components/AppShell";
import { navigateWithinApp } from "../navigation";
import { useAuthSession } from "../store/auth";
import { reloadWorkspaces, switchWorkspace, WORKSPACE_ROLE_LABELS } from "../store/workspace";

function inviteError(error: unknown): string {
  if (error instanceof ApiError) {
    const code = (error.detail as { code?: string } | undefined)?.code;
    if (error.status === 404) return "사용할 수 없는 링크예요. 주소를 확인하거나 새 링크를 요청하세요.";
    if (error.status === 410 && code === "invite_expired") return "링크가 만료됐어요. 초대한 분께 새 링크를 요청하세요.";
    if (error.status === 410 && code === "invite_exhausted") return "이 링크는 사용 횟수를 다 썼어요. 초대한 분께 새 링크를 요청하세요.";
    if (error.status === 410 && code === "invite_revoked") return "이 링크는 더 이상 쓸 수 없어요. 초대한 분께 새 링크를 요청하세요.";
  }
  return error instanceof Error ? error.message : "초대 정보를 불러오지 못했어요. 잠시 뒤 다시 시도해 주세요.";
}

export function InviteAcceptPage({ token }: { token: string }) {
  const { user } = useAuthSession();
  const [preview, setPreview] = useState<InvitePreview | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const generation = useRef(0);
  const accepting = useRef(false);

  const showError = (failure: unknown) => {
    if (failure instanceof ApiError && failure.status === 401) {
      navigateWithinApp(`/login?next=${encodeURIComponent(`/invites/${token}`)}`);
      return;
    }
    setError(inviteError(failure));
  };

  useEffect(() => {
    const version = ++generation.current;
    setPreview(null);
    setError(null);
    setBusy(false);
    void getInvitePreview(token).then(async (value) => {
      if (version !== generation.current) return;
      if (value.already_member) {
        await reloadWorkspaces();
        if (version === generation.current) switchWorkspace(value.workspace_id, `'${value.workspace_name}'으로 이동했어요`);
      } else {
        setPreview(value);
      }
    }).catch((failure: unknown) => {
      if (version === generation.current) showError(failure);
    });
    return () => { generation.current++; };
  }, [token]);

  const accept = async () => {
    if (!preview || accepting.current) return;
    const version = generation.current;
    accepting.current = true;
    setBusy(true);
    setError(null);
    try {
      const result = await acceptInvite(token);
      if (version !== generation.current) return;
      await reloadWorkspaces();
      if (version === generation.current) switchWorkspace(result.workspace_id);
    } catch (failure: unknown) {
      if (version === generation.current) showError(failure);
    } finally {
      accepting.current = false;
      if (version === generation.current) setBusy(false);
    }
  };

  return (
    <main className="auth-card-screen">
      <section className="auth-card invite-accept-card" aria-busy={busy}>
        <Brand />
        {error ? (
          <>
            <p className="banner banner-danger" role="alert">{error}</p>
            <button className="btn btn-secondary auth-submit" type="button" onClick={() => navigateWithinApp("/projects")}>프로젝트로 가기</button>
          </>
        ) : preview ? (
          <>
            <span className="ws-avatar invite-avatar" aria-hidden="true">{preview.workspace_name.slice(0, 1)}</span>
            <h2>{preview.workspace_name}</h2>
            <p>{WORKSPACE_ROLE_LABELS[preview.role]} 역할로 초대받았어요</p>
            <p className="invite-expiry">링크 만료 {new Date(preview.expires_at).toLocaleDateString("ko-KR")}</p>
            <hr />
            <p className="invite-question">{preview.workspace_name} 워크스페이스에 들어가시겠어요?</p>
            <button className="btn btn-primary auth-submit" type="button" disabled={busy} onClick={accept}>{busy ? "들어가는 중…" : "들어가기"}</button>
            <button className="btn btn-ghost auth-submit" type="button" disabled={busy} onClick={() => navigateWithinApp("/projects")}>나중에</button>
            <p className="auth-card-link">{user?.email ?? user?.username ?? "현재 계정"} 으로 로그인됨</p>
          </>
        ) : <p role="status">초대 정보를 불러오는 중…</p>}
      </section>
    </main>
  );
}
