import {
  clearAuthSession,
  getAuthSnapshot,
  setAuthSession,
  setAuthTokens,
  syncAuthSessionFromStorage,
  type AuthTokens,
  type AuthUser,
} from "../store/auth";

export type OAuthProvider = "naver" | "kakao" | "google";

export interface SignupResponse {
  id: number;
  email: string;
  username: string;
}

export class AuthApiError extends Error {
  constructor(
    public readonly status: number,
    message: string,
    public readonly detail?: unknown,
  ) {
    super(message);
    this.name = "AuthApiError";
  }
}

export class AuthRefreshTimeoutError extends Error {
  constructor() {
    super(
      "인증 갱신 제한 시간을 초과했습니다. 다른 Annodock 탭을 닫고 이어 올리기를 다시 시도해 주세요.",
    );
    this.name = "AuthRefreshTimeoutError";
  }
}

async function authError(response: Response): Promise<AuthApiError> {
  let message = `인증 요청 실패 (${response.status})`;
  let detail: unknown;
  try {
    const body = await response.json() as { detail?: unknown };
    detail = body.detail;
    if (typeof detail === "string") message = detail;
    else if (Array.isArray(detail)) message = "입력값을 확인해 주세요.";
  } catch {
    // Keep the status fallback for empty/non-JSON responses.
  }
  return new AuthApiError(response.status, message, detail);
}

async function authRequest(
  path: string,
  init: RequestInit = {},
  expectedStatus?: number,
): Promise<Response> {
  const response = await fetch(path, {
    ...init,
    credentials: "include",
    cache: "no-store",
  });
  if (!response.ok) throw await authError(response);
  if (expectedStatus !== undefined && response.status !== expectedStatus) {
    throw new AuthApiError(
      response.status,
      `인증 서버 응답 코드가 올바르지 않습니다. (예상 ${expectedStatus})`,
    );
  }
  return response;
}

async function authJson<T>(
  path: string,
  init: RequestInit,
  expectedStatus = 200,
): Promise<T> {
  const response = await authRequest(path, init, expectedStatus);
  return await response.json() as T;
}

function jsonPost(body: unknown): RequestInit {
  return {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  };
}

function assertTokenPair(value: AuthTokens): AuthTokens {
  if (typeof value.access_token !== "string"
    || value.access_token.length === 0
    || typeof value.refresh_token !== "string"
    || value.refresh_token.length === 0) {
    throw new Error("인증 서버가 올바른 토큰쌍을 반환하지 않았습니다.");
  }
  return value;
}

export function signup(
  username: string,
  email: string,
  password: string,
): Promise<SignupResponse> {
  return authJson("/auth/signup", jsonPost({ username, email, password }), 201);
}

export async function login(
  identifier: string,
  password: string,
): Promise<AuthTokens> {
  return assertTokenPair(await authJson<AuthTokens>(
    "/auth/login",
    jsonPost({ identifier, password }),
  ));
}

export async function refreshTokens(signal?: AbortSignal): Promise<AuthTokens> {
  return assertTokenPair(await authJson<AuthTokens>(
    "/auth/token/refresh",
    { method: "POST", signal },
  ));
}

export async function getCurrentUser(accessToken: string): Promise<AuthUser> {
  const response = await authRequest("/auth/me", {
    headers: { Authorization: `Bearer ${accessToken}` },
  }, 200);
  return await response.json() as AuthUser;
}

export async function establishAuthSession(tokens: AuthTokens): Promise<AuthUser> {
  const validTokens = assertTokenPair(tokens);
  const user = await getCurrentUser(validTokens.access_token);
  setAuthSession(validTokens, user);
  return user;
}

let refreshFlight: Promise<string> | null = null;
const AUTH_REFRESH_LOCK = "annodock:auth-refresh";
const AUTH_REFRESH_DEADLINE_MS = 15_000;

function withRefreshLock<T>(
  signal: AbortSignal,
  callback: () => Promise<T>,
): Promise<T> {
  if (typeof navigator === "undefined" || !navigator.locks) return callback();
  return navigator.locks.request(
    AUTH_REFRESH_LOCK,
    { mode: "exclusive", signal },
    () => {
      if (signal.aborted) throw signal.reason;
      return callback();
    },
  );
}

async function withRefreshDeadline<T>(
  callback: (signal: AbortSignal) => Promise<T>,
): Promise<T> {
  const controller = new AbortController();
  const timeoutError = new AuthRefreshTimeoutError();
  let timedOut = false;
  let timeoutId: number | undefined;
  const timeout = new Promise<never>((_resolve, reject) => {
    timeoutId = window.setTimeout(() => {
      timedOut = true;
      controller.abort(timeoutError);
      reject(timeoutError);
    }, AUTH_REFRESH_DEADLINE_MS);
  });
  try {
    return await Promise.race([callback(controller.signal), timeout]);
  } catch (error: unknown) {
    if (timedOut) throw timeoutError;
    throw error;
  } finally {
    if (timeoutId !== undefined) window.clearTimeout(timeoutId);
  }
}

function isTerminalRefreshError(error: unknown): boolean {
  return error instanceof AuthApiError
    && [400, 401, 403, 422].includes(error.status);
}

export function refreshAuthSession(staleAccessToken?: string | null): Promise<string> {
  if (refreshFlight) return refreshFlight;
  const observedAccessToken = staleAccessToken === undefined
    ? getAuthSnapshot().accessToken
    : staleAccessToken;

  refreshFlight = withRefreshDeadline((signal) => (
    withRefreshLock(signal, async () => {
      const synchronized = syncAuthSessionFromStorage(true);
      if (
        synchronized.accessToken
        && synchronized.accessToken !== observedAccessToken
      ) return synchronized.accessToken;

      try {
        // The auth service owns refresh rotation in an httpOnly cookie. Keeping
        // that cookie as the one presented credential prevents stale tabs from
        // replaying an older JavaScript copy and burning the rotation chain.
        const tokens = await refreshTokens(signal);
        setAuthTokens(tokens);
        return tokens.access_token;
      } catch (error: unknown) {
        // A different tab may have completed rotation while this request was in
        // flight. Adopt that pair before treating the refresh as a real failure.
        const latest = syncAuthSessionFromStorage(true);
        if (latest.accessToken && latest.accessToken !== observedAccessToken) {
          return latest.accessToken;
        }
        // Network/5xx failures are not proof that the refresh credential is bad.
        // Preserve the durable upload session so the same operation can resume.
        if (isTerminalRefreshError(error)) clearAuthSession();
        throw error;
      }
    })
  ))
    .catch((error: unknown) => {
      if (error instanceof AuthRefreshTimeoutError) {
        // The lock holder may have rotated the token immediately before our
        // deadline. Prefer that durable result over pausing a healthy upload.
        const latest = syncAuthSessionFromStorage(true);
        if (latest.accessToken && latest.accessToken !== observedAccessToken) {
          return latest.accessToken;
        }
      }
      throw error;
    })
    .finally(() => {
      refreshFlight = null;
    });
  return refreshFlight;
}

export async function logoutCurrentSession(): Promise<void> {
  const refreshToken = getAuthSnapshot().refreshToken;
  try {
    await authRequest("/auth/logout", jsonPost({ refresh_token: refreshToken }), 204);
  } finally {
    clearAuthSession();
  }
}

export async function requestPasswordReset(identifier: string): Promise<void> {
  await authRequest(
    "/auth/password/reset-request",
    jsonPost({ identifier }),
    202,
  );
}

export async function resetPassword(
  token: string,
  newPassword: string,
): Promise<void> {
  await authRequest(
    "/auth/password/reset",
    jsonPost({ token, new_password: newPassword }),
    204,
  );
}

export function oauthAuthorizeUrl(provider: OAuthProvider): string {
  return `/auth/oauth/${provider}/authorize`;
}

const oauthExchangeFlights = new Map<string, Promise<AuthUser>>();

export function exchangeOAuthCodeOnce(code: string): Promise<AuthUser> {
  const existing = oauthExchangeFlights.get(code);
  if (existing) return existing;
  const flight = authJson<AuthTokens>(
    "/auth/oauth/exchange",
    jsonPost({ code }),
  ).then(assertTokenPair).then(establishAuthSession);
  // Keep the promise for this page lifetime: React StrictMode remounts effects in
  // development, while an OAuth code is intentionally consumable only once.
  oauthExchangeFlights.set(code, flight);
  return flight;
}
