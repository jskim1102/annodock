import { useSyncExternalStore } from "react";

import { invalidateStorageQuotaCache, requestJson } from "../api/client";
import { navigateWithinApp } from "../navigation";
import { clearAuthenticatedResourceCache } from "../utils/authenticatedResourceCache";
import { readStoredString, writeStoredString } from "../utils/storage";

export type WorkspaceRole = "viewer" | "editor" | "admin" | "owner";
export interface WorkspaceRow {
  id: number;
  kind: "personal" | "team";
  name: string;
  role: WorkspaceRole;
}
export const WORKSPACE_ROLE_LABELS: Partial<Record<WorkspaceRole, string>> = {
  viewer: "보기 전용", editor: "라벨러", admin: "관리자", owner: "소유자",
};

interface WorkspaceSnapshot {
  userId: number | null;
  items: WorkspaceRow[];
  active: WorkspaceRow | null;
  status: "idle" | "loading" | "ready" | "error";
  notFound: boolean;
  message: string | null;
  error: string | null;
}
const EMPTY: WorkspaceSnapshot = {
  userId: null, items: [], active: null, status: "idle", notFound: false,
  message: null, error: null,
};
let snapshot = EMPTY;
let generation = 0;
const listeners = new Set<() => void>();

function publish(next: WorkspaceSnapshot) {
  const changed = snapshot.active?.id !== next.active?.id || snapshot.userId !== next.userId;
  snapshot = next;
  if (changed) {
    clearAuthenticatedResourceCache();
    invalidateStorageQuotaCache();
  }
  listeners.forEach((listener) => listener());
}

export function getWorkspaceSnapshot(): WorkspaceSnapshot { return snapshot; }
export function getActiveWorkspaceId(): number | null { return snapshot.active?.id ?? null; }
export function getWorkspaceKind(id: number | null): WorkspaceRow["kind"] | null {
  return snapshot.items.find((item) => item.id === id)?.kind ?? null;
}
export function subscribeWorkspace(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}
export function useWorkspace(): WorkspaceSnapshot {
  return useSyncExternalStore(subscribeWorkspace, getWorkspaceSnapshot, getWorkspaceSnapshot);
}
export function resetWorkspaces(): void {
  generation++;
  publish(EMPTY);
}
export function dismissWorkspaceMessage(): void { publish({ ...snapshot, message: null }); }

function persist(active: WorkspaceRow, userId: number) {
  writeStoredString(`workspace:${userId}`, String(active.id));
}
function workspaceId(value: string): number | null {
  if (!/^[1-9]\d*$/.test(value)) return null;
  const id = Number(value);
  return Number.isSafeInteger(id) && id <= 2_147_483_647 ? id : null;
}

/** Revalidate deep links against the server before mounting a scoped page. */
export async function initializeWorkspaces(userId: number, requestedId?: string | null): Promise<void> {
  const version = ++generation;
  const sameUser = snapshot.userId === userId;
  publish({ ...(sameUser ? snapshot : EMPTY), userId, status: "loading", notFound: false, error: null });
  try {
    const { items } = await requestJson<{ items: WorkspaceRow[] }>("/api/workspaces");
    if (version !== generation) return;
    const requested = requestedId === undefined || requestedId === null ? undefined : workspaceId(requestedId);
    const selected = requested === undefined ? undefined : items.find((item) => item.id === requested);
    if (requested !== undefined && !selected) {
      // Do not persist or switch to a scope named by an unauthorized link.
      publish({ ...snapshot, items, status: "ready", notFound: true });
      return;
    }
    const stored = readStoredString(`workspace:${userId}`);
    const active = selected
      ?? items.find((item) => item.id === snapshot.active?.id)
      ?? items.find((item) => String(item.id) === stored)
      ?? items.find((item) => item.kind === "personal");
    if (!active) throw new Error("워크스페이스를 불러오지 못했어요. 다시 시도해 주세요.");
    persist(active, userId);
    publish({ ...snapshot, items, active, status: "ready", notFound: false,
      message: selected && String(selected.id) !== stored
        ? `워크스페이스 ${selected.name} 로 전환됨` : snapshot.message });
  } catch (error: unknown) {
    if (version !== generation) return;
    publish({ ...snapshot, status: "error", error: error instanceof Error ? error.message : "워크스페이스를 불러오지 못했어요." });
  }
}

export async function reloadWorkspaces(): Promise<void> {
  if (snapshot.userId !== null) await initializeWorkspaces(snapshot.userId);
  if (snapshot.status === "error") throw new Error(snapshot.error ?? "워크스페이스를 불러오지 못했어요.");
}

/** Same-document navigation lets captured uploads/saves finish in their original scope. */
export function switchWorkspace(id: number, message?: string): void {
  const active = snapshot.items.find((item) => item.id === id);
  if (!active || snapshot.userId === null) return;
  generation++;
  persist(active, snapshot.userId);
  publish({ ...snapshot, active, status: "ready", notFound: false, error: null,
    message: message ?? `워크스페이스 ${active.name} 로 전환됨` });
  navigateWithinApp(`/projects?ws=${active.id}`);
}
