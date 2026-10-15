import { useEffect, useRef, useState } from "react";

import { ApiError } from "../api/client";
import { changeWorkspaceMemberRole, deleteWorkspace, getWorkspaceInvites, getWorkspaceMembers,
  leaveWorkspace, removeWorkspaceMember, renameWorkspace, revokeWorkspaceInvite,
  transferWorkspaceOwner, workspaceErrorCode, workspaceErrorMessage,
  type InvitableRole, type WorkspaceInviteRow, type WorkspaceMemberRow, type WorkspaceNotEmpty } from "../api/workspaces";
import { AppShell } from "../components/AppShell";
import { InviteLinkDialog, WorkspaceDialog } from "../components/InviteLinkDialog";
import { NewWorkspaceDialog } from "../components/NewWorkspaceDialog";
import { getWorkspaceSnapshot, reloadWorkspaces, switchWorkspace, useWorkspace, WORKSPACE_ROLE_LABELS } from "../store/workspace";

type Confirmation = { kind: "remove" | "transfer"; member: WorkspaceMemberRow } | { kind: "leave" | "delete" };
const memberName = (member: WorkspaceMemberRow) => member.username || member.email || "이름 없음";
const date = (value: string) => new Date(value).toLocaleDateString("ko-KR");
function remaining(value: string) {
  const expiry = new Date(value), today = new Date();
  expiry.setHours(0, 0, 0, 0); today.setHours(0, 0, 0, 0);
  const days = Math.round((expiry.getTime() - today.getTime()) / 86_400_000);
  return days <= 0 ? "오늘" : days === 1 ? "내일" : `${days}일 남음`;
}

export function MembersPage() {
  const { active, userId } = useWorkspace();
  const [members, setMembers] = useState<WorkspaceMemberRow[]>([]);
  const [invites, setInvites] = useState<WorkspaceInviteRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [memberError, setMemberError] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notFound, setNotFound] = useState(false);
  const [version, setVersion] = useState(0);
  const [busy, setBusy] = useState(false);
  const busyRef = useRef(false);
  const [inviteOpen, setInviteOpen] = useState(false);
  const [newTeamOpen, setNewTeamOpen] = useState(false);
  const [menuUser, setMenuUser] = useState<number | null>(null);
  const [confirmation, setConfirmation] = useState<Confirmation | null>(null);
  const [blockedResources, setBlockedResources] = useState<WorkspaceNotEmpty | null>(null);
  const [editingName, setEditingName] = useState(false);
  const [name, setName] = useState("");
  const [nextOwner, setNextOwner] = useState("");
  const canManage = active?.role === "owner" || active?.role === "admin";
  const isOwner = active?.role === "owner";

  useEffect(() => {
    let alive = true;
    setMembers([]); setInvites([]); setMemberError(null); setError(null); setNotFound(false);
    setMenuUser(null); setConfirmation(null); setBlockedResources(null); setNextOwner("");
    if (!active || active.kind === "personal") { setLoading(false); return; }
    setLoading(true);
    void getWorkspaceMembers(active.id).then(({ items }) => {
      if (alive) setMembers(items);
    }).catch((reason: unknown) => {
      if (!alive) return;
      if (reason instanceof ApiError && reason.status === 404) setNotFound(true);
      else setMemberError("멤버 정보를 불러오지 못했어요 — 잠시 뒤 다시 시도");
    }).finally(() => { if (alive) setLoading(false); });
    if (canManage) void getWorkspaceInvites(active.id).then((rows) => {
      if (alive) setInvites(rows);
    }).catch((reason: unknown) => { if (alive) setError(workspaceErrorMessage(reason)); });
    return () => { alive = false; };
  }, [active?.id, active?.kind, canManage, version]);

  const run = async (operation: () => Promise<void>) => {
    if (busyRef.current) return;
    busyRef.current = true; setBusy(true); setError(null); setBlockedResources(null);
    try { await operation(); }
    catch (reason: unknown) {
      setError(workspaceErrorMessage(reason));
      if (reason instanceof ApiError && reason.status === 404) setNotFound(true);
      if (workspaceErrorCode(reason) === "workspace_not_empty" && reason instanceof ApiError) {
        setBlockedResources(reason.detail as WorkspaceNotEmpty);
      }
    } finally { busyRef.current = false; setBusy(false); }
  };
  const personal = async () => {
    await reloadWorkspaces();
    const target = getWorkspaceSnapshot().items.find((workspace) => workspace.kind === "personal");
    if (target) switchWorkspace(target.id);
  };
  const confirm = () => {
    if (!active || !confirmation) return;
    const workspaceId = active.id, action = confirmation;
    void run(async () => {
      if (action.kind === "remove") {
        await removeWorkspaceMember(workspaceId, action.member.user_id);
        setMembers((rows) => rows.filter((row) => row.user_id !== action.member.user_id));
      } else if (action.kind === "transfer") {
        await transferWorkspaceOwner(workspaceId, action.member.user_id);
        setConfirmation(null);
        await reloadWorkspaces();
        setVersion((value) => value + 1);
      } else {
        if (action.kind === "leave") await leaveWorkspace(workspaceId);
        else await deleteWorkspace(workspaceId);
        setConfirmation(null);
        await personal();
      }
      setConfirmation(null);
    });
  };
  const openConfirmation = (value: Confirmation) => {
    setMenuUser(null); setError(null); setBlockedResources(null); setConfirmation(value);
  };

  if (notFound) return <main className="empty"><h1>페이지를 찾을 수 없습니다</h1><p>접근할 수 없는 워크스페이스입니다.</p></main>;
  return <AppShell active="members" breadcrumb="멤버" mainClassName="members-page">
    <div className="members-heading page-heading-row"><h1>멤버</h1>
      <p>{active?.name ?? ""}{active?.kind === "team" && !loading && !memberError ? ` · ${members.length}명` : ""}</p></div>
    {!active ? <p role="status">워크스페이스를 불러오는 중…</p> : active.kind === "personal" ? <div className="empty">
      <p>나만 쓰는 공간 — 팀을 만들면 함께 작업할 수 있어요</p>
      <button type="button" className="btn btn-primary" onClick={() => setNewTeamOpen(true)}>＋ 새 팀</button>
    </div> : <>
      {error && !confirmation ? <p className="banner-danger" role="alert">{error}</p> : null}
      <section className="members-section" aria-label="멤버 목록">
        {loading ? <p role="status">멤버를 불러오는 중…</p> : memberError ? <div className="empty" role="alert">
          <p>{memberError}</p><button type="button" className="btn btn-secondary" onClick={() => setVersion((value) => value + 1)}>다시 시도</button>
        </div> : <div className="members-table-wrap"><table className="table members-table"><thead><tr>
          <th>멤버</th><th>역할</th><th>참여일</th><th><span className="sr-only">멤버 관리</span></th>
        </tr></thead><tbody>{members.map((member) => <tr key={member.user_id}>
          <td><div className="member-identity"><span className="member-avatar" aria-hidden="true">{memberName(member).slice(0, 1).toUpperCase()}</span>
            <span><strong>{memberName(member)}</strong><span className="member-email">{member.email ?? "—"}</span></span>
            {member.user_id === userId ? <span className="tag tag-accent">나</span> : null}</div></td>
          <td>{canManage && member.role !== "owner" ? <select className="select" aria-label={`${memberName(member)} 역할`}
            ref={(select) => {
              if (select && !["admin", "editor"].includes(member.role)) select.selectedIndex = -1;
            }} value={member.role} disabled={busy} onChange={(event) => {
              const role = event.target.value as InvitableRole;
              void run(async () => {
                const updated = await changeWorkspaceMemberRole(active.id, member.user_id, role);
                setMembers((rows) => rows.map((row) => row.user_id === updated.user_id ? { ...row, role: updated.role } : row));
                await reloadWorkspaces();
              });
            }}>{(["admin", "editor"] as const).map((role) => <option key={role} value={role}>{WORKSPACE_ROLE_LABELS[role]}</option>)}</select>
            : WORKSPACE_ROLE_LABELS[member.role]}</td>
          <td className="mono">{date(member.joined_at)}</td>
          <td className="member-actions">{member.user_id === userId && member.role !== "owner" ? <button type="button" className="btn btn-secondary btn-sm"
            disabled={busy} onClick={() => openConfirmation({ kind: "leave" })}>탈퇴</button>
            : canManage && member.role !== "owner" && member.user_id !== userId ? <>
              <button type="button" className="btn btn-ghost btn-sm" aria-label={`${memberName(member)} 메뉴`}
                aria-expanded={menuUser === member.user_id} aria-haspopup="menu" disabled={busy}
                onClick={() => setMenuUser((current) => current === member.user_id ? null : member.user_id)}>⋯</button>
              {menuUser === member.user_id ? <div className="member-menu" role="menu">
                <button type="button" role="menuitem" className="btn btn-ghost btn-sm" onClick={() => openConfirmation({ kind: "remove", member })}>내보내기</button>
              </div> : null}
            </> : "—"}</td>
        </tr>)}</tbody></table></div>}
        {isOwner ? <p className="hint">소유자는 먼저 소유권을 다른 멤버에게 넘겨야 해요</p> : null}
      </section>
      {canManage ? <>
        <section className="members-section" aria-label="초대 링크">
          <div className="members-heading"><h2>초대 링크</h2><button type="button" className="btn btn-secondary" onClick={() => setInviteOpen(true)}>＋ 초대 링크 만들기</button></div>
          <div className="members-table-wrap"><table className="table members-table"><thead><tr>
            <th>역할</th><th>만료</th><th>사용</th><th>만든 사람</th><th><span className="sr-only">링크 관리</span></th>
          </tr></thead><tbody>{invites.map((invite) => <tr key={invite.id}>
            <td>{WORKSPACE_ROLE_LABELS[invite.role]}</td><td>{date(invite.expires_at)} <span className="hint">({remaining(invite.expires_at)})</span></td>
            <td className="mono">{invite.uses} / {invite.max_uses ?? "제한 없음"}</td>
            <td>{members.find((member) => member.user_id === invite.created_by)?.username ?? "—"}</td>
            <td><button type="button" className="btn btn-secondary btn-sm" disabled={busy} onClick={() => void run(async () => {
              try { await revokeWorkspaceInvite(active.id, invite.id); }
              catch (reason: unknown) { if (!(reason instanceof ApiError && reason.status === 404)) throw reason; }
              setInvites((rows) => rows.filter((row) => row.id !== invite.id));
            })}>철회</button></td>
          </tr>)}</tbody></table></div>
          {invites.length === 0 ? <p className="empty">활성 초대 링크가 없어요</p> : null}
          <p className="hint">쓸 수 있는 링크만 보여요. 링크 주소는 만들 때 한 번만 볼 수 있어요.</p>
        </section>
        <section className="card members-management" aria-label="팀 관리"><h2>팀 관리</h2>
          <div className="management-row"><span>팀 이름</span>{editingName ? <form onSubmit={(event) => {
            event.preventDefault(); if (!name.trim() || name.trim().length > 255) return;
            void run(async () => { await renameWorkspace(active.id, name.trim()); setEditingName(false); await reloadWorkspaces(); });
          }}><input className="input" aria-label="팀 이름" value={name} maxLength={255} disabled={busy} onChange={(event) => setName(event.target.value)} />
            <button type="submit" className="btn btn-primary btn-sm" disabled={busy || !name.trim()}>저장</button>
            <button type="button" className="btn btn-secondary btn-sm" disabled={busy} onClick={() => setEditingName(false)}>취소</button></form>
            : <><strong>{active.name}</strong><button type="button" className="btn btn-secondary btn-sm" disabled={busy} onClick={() => { setName(active.name); setEditingName(true); }}>변경</button></>}</div>
          {isOwner ? <>
            <div className="management-row"><label htmlFor="workspace-next-owner">소유권 위임</label><select id="workspace-next-owner" className="select"
              value={nextOwner} disabled={busy} onChange={(event) => setNextOwner(event.target.value)}><option value="">멤버 선택</option>
              {members.filter((member) => member.role !== "owner").map((member) => <option key={member.user_id} value={member.user_id}>{memberName(member)}</option>)}</select>
              <button type="button" className="btn btn-secondary btn-sm" disabled={busy || !nextOwner} onClick={() => {
                const member = members.find((row) => row.user_id === Number(nextOwner));
                if (member) openConfirmation({ kind: "transfer", member });
              }}>넘기기</button></div>
            <div className="management-row"><span>팀 삭제</span><button type="button" className="btn btn-danger btn-sm" disabled={busy}
              onClick={() => openConfirmation({ kind: "delete" })}>삭제</button></div>
          </> : null}
        </section>
      </> : null}
    </>}
    {newTeamOpen ? <NewWorkspaceDialog onClose={() => setNewTeamOpen(false)} /> : null}
    {inviteOpen && active && canManage ? <InviteLinkDialog workspaceId={active.id} onClose={() => setInviteOpen(false)}
      onCreated={(invite) => setInvites((rows) => [invite, ...rows])} /> : null}
    {confirmation ? <WorkspaceDialog title={confirmation.kind === "remove" ? "멤버 내보내기" : confirmation.kind === "transfer" ? "소유권 위임" : confirmation.kind === "leave" ? "팀 탈퇴" : "팀 삭제"}
      busy={busy} onClose={() => setConfirmation(null)}>
      <p>{confirmation.kind === "remove" ? `${memberName(confirmation.member)} 님을 내보낼까요? 작업 중이던 이미지는 다시 미배정이 돼요.`
        : confirmation.kind === "transfer" ? `${memberName(confirmation.member)} 님에게 소유권을 넘길까요? 나는 관리자가 돼요.`
          : confirmation.kind === "leave" ? "이 팀에서 탈퇴할까요? 다시 들어오려면 초대 링크가 필요해요." : `${active?.name ?? "팀"}을 삭제할까요? 이 작업은 되돌릴 수 없어요.`}</p>
      {error ? <p className="banner-danger" role="alert">{error}</p> : null}
      {blockedResources ? <ul>
        {blockedResources.projects?.map((project) => <li key={`project-${project.id}`}>{project.name}</li>)}
        {blockedResources.runs?.map((run) => <li key={`run-${run.id}`}>학습 #{run.id} · {run.dataset_name ?? "이름 없음"}</li>)}
      </ul> : null}
      <div className="dialog-actions"><button type="button" className="btn btn-secondary" disabled={busy} onClick={() => setConfirmation(null)}>취소</button>
        <button type="button" className="btn btn-danger" disabled={busy} onClick={confirm}>{busy ? "처리 중…" : "확인"}</button></div>
    </WorkspaceDialog> : null}
  </AppShell>;
}
