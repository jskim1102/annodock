import { ApiError, requestJson, type ApiRequestInit } from "./client";
import type { WorkspaceRole, WorkspaceRow } from "../store/workspace";

export type InvitableRole = Extract<WorkspaceRole, "admin" | "editor">;
export interface WorkspaceMemberRow {
  user_id: number;
  email: string | null;
  username: string | null;
  role: WorkspaceRole;
  joined_at: string;
}
export interface WorkspaceInviteRow {
  id: number;
  role: InvitableRole;
  expires_at: string;
  max_uses: number | null;
  uses: number;
  created_by: number;
  created_at: string;
}
export interface InviteCreateInput {
  role: InvitableRole;
  expires_in_days: number;
  max_uses: number | null;
}
export interface InviteCreated { invite: WorkspaceInviteRow; accept_url: string }
export interface InvitePreview {
  workspace_id: number;
  workspace_name: string;
  role: WorkspaceRole;
  expires_at: string;
  already_member: boolean;
}
export interface InviteAccepted { workspace_id: number; role: WorkspaceRole; already_member: boolean }
export interface WorkspaceNotEmpty {
  projects?: Array<{ id: number; name: string }>;
  runs?: Array<{ id: number; dataset_name: string | null }>;
}

function scopedInit(method = "GET", body?: unknown): ApiRequestInit {
  // Management uses the path's scope; apiFetch still attaches the selected
  // workspace header consistently with the rest of the API.
  return { method, ...(body === undefined ? {} : {
    headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  }) };
}
export function createWorkspace(name: string): Promise<WorkspaceRow> {
  return requestJson("/api/workspaces", scopedInit("POST", { name }));
}
export function getInvitePreview(token: string): Promise<InvitePreview> {
  return requestJson(`/api/invites/${encodeURIComponent(token)}`, scopedInit());
}
export function acceptInvite(token: string): Promise<InviteAccepted> {
  return requestJson(`/api/invites/${encodeURIComponent(token)}/accept`, scopedInit("POST"));
}
export function getWorkspaceMembers(id: number): Promise<{ items: WorkspaceMemberRow[] }> {
  return requestJson(`/api/workspaces/${id}/members`, scopedInit());
}
export function getWorkspaceInvites(id: number): Promise<WorkspaceInviteRow[]> {
  return requestJson(`/api/workspaces/${id}/invites`, scopedInit());
}
export function createWorkspaceInvite(id: number, input: InviteCreateInput): Promise<InviteCreated> {
  return requestJson(`/api/workspaces/${id}/invites`, scopedInit("POST", input));
}
export function revokeWorkspaceInvite(id: number, inviteId: number): Promise<void> {
  return requestJson(`/api/workspaces/${id}/invites/${inviteId}`, scopedInit("DELETE"));
}
export function changeWorkspaceMemberRole(id: number, userId: number, role: InvitableRole): Promise<{ user_id: number; role: WorkspaceRole }> {
  return requestJson(`/api/workspaces/${id}/members/${userId}`, scopedInit("PATCH", { role }));
}
export function removeWorkspaceMember(id: number, userId: number): Promise<void> {
  return requestJson(`/api/workspaces/${id}/members/${userId}`, scopedInit("DELETE"));
}
export function leaveWorkspace(id: number): Promise<void> {
  return requestJson(`/api/workspaces/${id}/members/me`, scopedInit("DELETE"));
}
export function renameWorkspace(id: number, name: string): Promise<WorkspaceRow> {
  return requestJson(`/api/workspaces/${id}`, scopedInit("PATCH", { name }));
}
export function transferWorkspaceOwner(id: number, userId: number): Promise<WorkspaceRow> {
  return requestJson(`/api/workspaces/${id}/owner`, scopedInit("POST", { user_id: userId }));
}
export function deleteWorkspace(id: number): Promise<void> {
  return requestJson(`/api/workspaces/${id}`, scopedInit("DELETE"));
}
export function workspaceErrorCode(error: unknown): string | null {
  if (!(error instanceof ApiError) || !error.detail || typeof error.detail !== "object") return null;
  return "code" in error.detail && typeof error.detail.code === "string" ? error.detail.code : null;
}
export function workspaceErrorMessage(error: unknown): string {
  switch (workspaceErrorCode(error)) {
    case "owner_role_fixed": return "소유자 역할은 소유권 넘기기로만 바꿀 수 있어요";
    case "owner_must_transfer": return "소유자는 먼저 소유권을 다른 멤버에게 넘겨야 해요";
    case "personal_workspace_fixed": return "개인 워크스페이스에서는 할 수 없어요";
    case "workspace_not_empty": return "프로젝트나 학습 기록이 남아 있어 삭제할 수 없어요";
    default: return error instanceof Error ? error.message : "요청을 처리하지 못했어요. 다시 시도해 주세요.";
  }
}
