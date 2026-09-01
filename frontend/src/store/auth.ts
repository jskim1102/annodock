import { useSyncExternalStore } from "react";

import {
  readStoredJson,
  removeStoredValue,
  writeStoredJson,
} from "../utils/storage";
import { clearAuthenticatedResourceCache } from "../utils/authenticatedResourceCache";

export interface AuthTokens {
  access_token: string;
  refresh_token: string;
}

export interface AuthUser {
  id: number;
  email: string | null;
  username: string | null;
  identities: string[];
}

export interface AuthSnapshot {
  accessToken: string | null;
  refreshToken: string | null;
  user: AuthUser | null;
}

const SESSION_KEY = "auth:session";
const EMPTY_SNAPSHOT: AuthSnapshot = {
  accessToken: null,
  refreshToken: null,
  user: null,
};

function isAuthUser(value: unknown): value is AuthUser {
  if (!value || typeof value !== "object") return false;
  const candidate = value as Partial<AuthUser>;
  return typeof candidate.id === "number"
    && (typeof candidate.email === "string" || candidate.email === null)
    && (typeof candidate.username === "string" || candidate.username === null)
    && Array.isArray(candidate.identities)
    && candidate.identities.every((identity) => typeof identity === "string");
}

function loadSnapshot(): AuthSnapshot {
  const stored = readStoredJson(SESSION_KEY);
  if (!stored || typeof stored !== "object") return EMPTY_SNAPSHOT;
  const candidate = stored as Partial<AuthSnapshot>;
  if (typeof candidate.accessToken !== "string"
    || candidate.accessToken.length === 0
    || typeof candidate.refreshToken !== "string"
    || candidate.refreshToken.length === 0) {
    return EMPTY_SNAPSHOT;
  }
  return {
    accessToken: candidate.accessToken,
    refreshToken: candidate.refreshToken,
    user: isAuthUser(candidate.user) ? candidate.user : null,
  };
}

let snapshot = loadSnapshot();
const listeners = new Set<() => void>();

function publish(next: AuthSnapshot) {
  snapshot = next;
  listeners.forEach((listener) => listener());
}

function sameUser(left: AuthUser | null, right: AuthUser | null): boolean {
  if (left === right) return true;
  if (left === null || right === null) return false;
  return left.id === right.id
    && left.email === right.email
    && left.username === right.username
    && left.identities.length === right.identities.length
    && left.identities.every((identity, index) => identity === right.identities[index]);
}

function sameSnapshot(left: AuthSnapshot, right: AuthSnapshot): boolean {
  return left.accessToken === right.accessToken
    && left.refreshToken === right.refreshToken
    && sameUser(left.user, right.user);
}

function persist(next: AuthSnapshot) {
  // One JSON record keeps a rotated access/refresh pair from being restored
  // half-updated after a reload.
  if (!writeStoredJson(SESSION_KEY, next)) {
    // Never leave an older refresh token restorable after the server has rotated
    // it. The current tab can continue with the in-memory session.
    removeStoredValue(SESSION_KEY);
  }
  publish(next);
}

export function getAuthSnapshot(): AuthSnapshot {
  return snapshot;
}

export function syncAuthSessionFromStorage(
  preserveCurrentWhenEmpty = false,
): AuthSnapshot {
  const stored = loadSnapshot();
  if (
    preserveCurrentWhenEmpty
    && stored.accessToken === null
    && stored.refreshToken === null
    && snapshot.accessToken !== null
    && snapshot.refreshToken !== null
  ) return snapshot;
  if (sameSnapshot(snapshot, stored)) return snapshot;
  if (snapshot.user?.id !== stored.user?.id || stored.accessToken === null) {
    clearAuthenticatedResourceCache();
  }
  publish(stored);
  return snapshot;
}

export function setAuthTokens(tokens: AuthTokens): void {
  if (!tokens.access_token || !tokens.refresh_token) {
    throw new Error("인증 서버가 올바른 토큰쌍을 반환하지 않았습니다.");
  }
  persist({
    accessToken: tokens.access_token,
    refreshToken: tokens.refresh_token,
    user: snapshot.user,
  });
}

export function setAuthSession(tokens: AuthTokens, user: AuthUser): void {
  if (!tokens.access_token || !tokens.refresh_token) {
    throw new Error("인증 서버가 올바른 토큰쌍을 반환하지 않았습니다.");
  }
  if (snapshot.user?.id !== undefined && snapshot.user.id !== user.id) {
    clearAuthenticatedResourceCache();
  }
  persist({
    accessToken: tokens.access_token,
    refreshToken: tokens.refresh_token,
    user,
  });
}

export function clearAuthSession(): void {
  removeStoredValue(SESSION_KEY);
  clearAuthenticatedResourceCache();
  publish(EMPTY_SNAPSHOT);
}

export function subscribeAuth(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function useAuthSession(): AuthSnapshot {
  return useSyncExternalStore(subscribeAuth, getAuthSnapshot, getAuthSnapshot);
}

if (typeof window !== "undefined") {
  window.addEventListener("storage", () => {
    syncAuthSessionFromStorage();
  });
}
