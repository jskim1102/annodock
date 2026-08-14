import { type FormEvent, type ReactNode, useEffect, useState } from "react";

import {
  AuthApiError,
  establishAuthSession,
  exchangeOAuthCodeOnce,
  login,
  oauthAuthorizeUrl,
  requestPasswordReset,
  resetPassword,
  signup,
  type OAuthProvider,
} from "../api/auth";
import { Brand } from "../components/AppShell";
import { appHref, navigate } from "../navigation";
import { useTheme } from "../theme";

const providers = [
  { id: "naver", label: "Naver", supported: true },
  { id: "kakao", label: "Kakao", supported: true },
  { id: "google", label: "Google", supported: true },
  { id: "github", label: "GitHub", supported: false },
] as const;

function ProviderIcon({ provider }: { provider: typeof providers[number]["id"] }) {
  if (provider === "naver") {
    return <svg width="14" height="14" viewBox="0 0 24 24" aria-hidden="true"><path fill="#03c75a" d="M4 4h5.4l5.2 7.6V4H20v16h-5.4l-5.2-7.6V20H4z" /></svg>;
  }
  if (provider === "kakao") {
    return <svg width="16" height="16" viewBox="0 0 24 24" aria-hidden="true"><rect x="1" y="1" width="22" height="22" rx="5" fill="#fee500" /><path fill="#191919" d="M12 5.8c-3.87 0-7 2.45-7 5.47 0 1.96 1.31 3.68 3.29 4.65l-.84 3.06c-.07.27.21.48.44.33l3.67-2.42c.15.01.29.02.44.02 3.87 0 7-2.45 7-5.64 0-3.02-3.13-5.47-7-5.47Z" /></svg>;
  }
  if (provider === "google") {
    return <svg width="15" height="15" viewBox="0 0 24 24" aria-hidden="true"><path fill="#4285f4" d="M21.35 12.2c0-.66-.06-1.3-.17-1.9H12v3.6h5.24a4.48 4.48 0 0 1-1.94 2.94v2.44h3.14c1.84-1.7 2.91-4.2 2.91-7.08Z" /><path fill="#34a853" d="M12 21.5c2.62 0 4.83-.87 6.44-2.35l-3.14-2.44c-.87.58-1.98.93-3.3.93-2.54 0-4.7-1.72-5.46-4.02H3.28v2.52A9.72 9.72 0 0 0 12 21.5Z" /><path fill="#fbbc05" d="M6.54 13.62a5.84 5.84 0 0 1 0-3.73V7.37H3.28a9.73 9.73 0 0 0 0 8.77l3.26-2.52Z" /><path fill="#ea4335" d="M12 6.36c1.43 0 2.71.49 3.72 1.45l2.79-2.79C16.83 3.44 14.62 2.5 12 2.5a9.72 9.72 0 0 0-8.72 5.37l3.26 2.52C7.3 8.09 9.46 6.36 12 6.36Z" /></svg>;
  }
  return <svg width="15" height="15" viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" d="M12 .5C5.65.5.5 5.65.5 12c0 5.08 3.29 9.39 7.86 10.91.58.11.79-.25.79-.56v-2.17c-3.2.7-3.87-1.37-3.87-1.37-.52-1.33-1.28-1.69-1.28-1.69-1.04-.71.08-.7.08-.7 1.15.08 1.76 1.18 1.76 1.18 1.03 1.76 2.7 1.25 3.36.96.1-.75.4-1.25.72-1.54-2.55-.29-5.23-1.28-5.23-5.68 0-1.26.45-2.29 1.18-3.1-.12-.29-.51-1.46.11-3.05 0 0 .96-.31 3.15 1.18a10.9 10.9 0 0 1 5.74 0c2.19-1.49 3.15-1.18 3.15-1.18.62 1.59.23 2.76.11 3.05.73.81 1.18 1.84 1.18 3.1 0 4.41-2.69 5.38-5.25 5.66.41.35.77 1.05.77 2.12v3.14c0 .31.21.68.8.56A11.52 11.52 0 0 0 23.5 12C23.5 5.65 18.35.5 12 .5Z" /></svg>;
}

function formValue(form: HTMLFormElement, name: string): string {
  return String(new FormData(form).get(name) ?? "");
}

function authErrorMessage(error: unknown, fallback: string): string {
  if (error instanceof AuthApiError && error.status === 422) {
    return "입력값을 확인해 주세요.";
  }
  return error instanceof Error && error.message ? error.message : fallback;
}

function pathAfterLogin(): string {
  const candidate = new URLSearchParams(window.location.search).get("next");
  if (!candidate || !candidate.startsWith("/") || candidate.startsWith("//")) {
    return "/projects";
  }
  const target = new URL(candidate, window.location.origin);
  return target.origin === window.location.origin
    ? `${target.pathname}${target.search}${target.hash}`
    : "/projects";
}

export function LoginPage() {
  const { theme } = useTheme();
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const registered = new URLSearchParams(window.location.search).has("registered");

  const submitLogin = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const identifier = formValue(event.currentTarget, "identifier").trim();
    const password = formValue(event.currentTarget, "password");
    setSubmitting(true);
    setError(null);
    try {
      const tokens = await login(identifier, password);
      await establishAuthSession(tokens);
      navigate(pathAfterLogin());
    } catch (reason: unknown) {
      setError(reason instanceof AuthApiError && reason.status === 401
        ? "아이디 또는 비밀번호가 올바르지 않습니다."
        : authErrorMessage(reason, "로그인하지 못했습니다."));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <main className="login-screen" data-screen-label="01 로그인">
      <section className="login-hero" aria-label="Annodock 소개">
        <img
          src={theme === "dark" ? "/assets/login-hero-dark.png" : "/assets/login-hero-bright.png"}
          alt="Annodock — 데이터 레이어 비주얼"
        />
        <div className="login-hero-fade" />
        <div className="login-hero-copy">
          <Brand inverse />
          <p>이미지 라벨링부터 모델 학습까지, 한 곳에서</p>
        </div>
      </section>

      <section className="auth-pane">
        <div className="auth-form-wrap">
          <Brand />
          <h1>계정에 로그인하세요</h1>

          <div className="oauth-stack">
            {providers.map((provider) => (
              <button
                className="btn btn-secondary oauth-button"
                type="button"
                key={provider.id}
                aria-disabled={!provider.supported}
                disabled={!provider.supported}
                title={provider.supported ? undefined : "아직 지원하지 않습니다"}
                onClick={() => {
                  if (provider.supported) {
                    window.location.assign(oauthAuthorizeUrl(provider.id as OAuthProvider));
                  }
                }}
              >
                <ProviderIcon provider={provider.id} />
                {provider.label} 로 계속하기
              </button>
            ))}
          </div>

          <div className="or-divider"><span>or</span></div>

          <form onSubmit={(event) => void submitLogin(event)}>
            <div className="field">
              <label htmlFor="login-id">아이디 또는 이메일</label>
              <input
                className="input"
                id="login-id"
                name="identifier"
                placeholder="you@example.com"
                autoComplete="username"
                required
                disabled={submitting}
              />
            </div>
            <div className="field">
              <label htmlFor="login-password">비밀번호</label>
              <input
                className="input"
                id="login-password"
                name="password"
                type="password"
                autoComplete="current-password"
                required
                disabled={submitting}
              />
            </div>
            <button className="btn btn-primary auth-submit" type="submit" disabled={submitting}>{submitting ? "로그인 중…" : "로그인"}</button>
          </form>

          {registered ? <div className="banner-info auth-banner" role="status">회원가입이 완료되었습니다. 로그인해 주세요.</div> : null}
          {error ? <div className="banner-danger auth-banner" role="alert">{error}</div> : null}

          <div className="auth-links">
            <span>계정이 없으신가요? <a href={appHref("/signup")}>회원가입</a></span>
            <a href={appHref("/password-reset")}>비밀번호를 잊으셨나요?</a>
          </div>
        </div>
      </section>
    </main>
  );
}

function AuthCard({ children }: { children: ReactNode }) {
  return (
    <main className="auth-card-screen">
      <section className="card auth-card">{children}</section>
    </main>
  );
}

export function SignupPage() {
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const submitSignup = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const username = formValue(event.currentTarget, "username").trim();
    const email = formValue(event.currentTarget, "email").trim();
    const password = formValue(event.currentTarget, "password");
    setSubmitting(true);
    setError(null);
    try {
      await signup(username, email, password);
      navigate("/login?registered=1");
    } catch (reason: unknown) {
      setError(reason instanceof AuthApiError && reason.status === 409
        ? "이미 사용 중인 username 또는 email입니다."
        : authErrorMessage(reason, "회원가입하지 못했습니다."));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <AuthCard>
      <Brand />
      <h1>회원가입</h1>
      <form onSubmit={(event) => void submitSignup(event)}>
        <div className="field">
          <label htmlFor="signup-username">username</label>
          <input
            className="input"
            id="signup-username"
            name="username"
            placeholder="영문 소문자 · 숫자 · 하이픈"
            autoComplete="username"
            required
            disabled={submitting}
          />
        </div>
        <div className="field">
          <label htmlFor="signup-email">email</label>
          <input
            className="input"
            id="signup-email"
            name="email"
            type="email"
            placeholder="you@example.com"
            autoComplete="email"
            required
            disabled={submitting}
          />
        </div>
        <div className="field">
          <label htmlFor="signup-password">비밀번호</label>
          <input
            className="input"
            id="signup-password"
            name="password"
            type="password"
            placeholder="••••••••"
            autoComplete="new-password"
            minLength={8}
            required
            disabled={submitting}
          />
          <div className="hint">8자 이상 · 영문/숫자 포함</div>
        </div>
        <button className="btn btn-primary auth-submit" type="submit" disabled={submitting}>{submitting ? "가입 중…" : "회원가입"}</button>
      </form>
      {error ? <div className="banner-danger auth-banner" role="alert">{error}</div> : null}
      <p className="auth-card-link">이미 계정이 있으신가요? <a href={appHref("/login")}>로그인</a></p>
    </AuthCard>
  );
}

export function PasswordResetPage() {
  const [sent, setSent] = useState(false);
  const [requesting, setRequesting] = useState(false);
  const [resetting, setResetting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const initialToken = new URLSearchParams(window.location.search).get("token") ?? "";

  const submitRequest = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const identifier = formValue(event.currentTarget, "identifier").trim();
    setRequesting(true);
    setError(null);
    try {
      await requestPasswordReset(identifier);
      // Keep the same UI response regardless of whether an account matched.
      setSent(true);
    } catch (reason: unknown) {
      setError(authErrorMessage(reason, "재설정 요청을 보내지 못했습니다."));
    } finally {
      setRequesting(false);
    }
  };

  const submitReset = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const token = formValue(event.currentTarget, "token").trim();
    const password = formValue(event.currentTarget, "new-password");
    const confirmation = formValue(event.currentTarget, "confirmation");
    if (password !== confirmation) {
      setError("새 비밀번호가 서로 일치하지 않습니다.");
      return;
    }
    setResetting(true);
    setError(null);
    try {
      await resetPassword(token, password);
      navigate("/login");
    } catch (reason: unknown) {
      setError(authErrorMessage(reason, "비밀번호를 변경하지 못했습니다."));
    } finally {
      setResetting(false);
    }
  };

  return (
    <AuthCard>
      <Brand />
      <h1>비밀번호 재설정</h1>
      <div className="auth-step-label">1단계 — 요청</div>
      <form onSubmit={(event) => void submitRequest(event)}>
        <div className="field">
          <label htmlFor="reset-email">email</label>
          <input
            className="input"
            id="reset-email"
            name="identifier"
            type="email"
            placeholder="you@example.com"
            autoComplete="email"
            required
            disabled={requesting}
          />
        </div>
        <button className="btn btn-primary auth-submit" type="submit" disabled={requesting}>{requesting ? "보내는 중…" : "재설정 링크 보내기"}</button>
      </form>
      {sent ? <div className="banner-info auth-banner" role="status">메일을 보냈습니다. 받은편지함을 확인하세요.</div> : null}

      <div className="auth-dashed-divider" />
      <div className="auth-step-label">2단계 — 확정 · 메일 링크로 진입</div>
      <form onSubmit={(event) => void submitReset(event)}>
        <div className="field">
          <label htmlFor="reset-token">토큰</label>
          <input className="input" id="reset-token" name="token" placeholder="메일의 재설정 토큰" defaultValue={initialToken} required disabled={resetting} />
        </div>
        <div className="field">
          <label htmlFor="reset-password">새 비밀번호</label>
          <input className="input" id="reset-password" name="new-password" type="password" placeholder="••••••••" autoComplete="new-password" minLength={8} required disabled={resetting} />
        </div>
        <div className="field">
          <label htmlFor="reset-confirm">새 비밀번호 확인</label>
          <input className="input" id="reset-confirm" name="confirmation" type="password" placeholder="••••••••" autoComplete="new-password" minLength={8} required disabled={resetting} />
        </div>
        <button className="btn btn-primary auth-submit" type="submit" disabled={resetting}>{resetting ? "변경 중…" : "변경"}</button>
      </form>
      {error ? <div className="banner-danger auth-banner" role="alert">{error}</div> : null}
    </AuthCard>
  );
}

export function OAuthCallbackPage() {
  const [error, setError] = useState<string | null>(null);
  const code = new URLSearchParams(window.location.search).get("code");

  useEffect(() => {
    if (!code) {
      setError("OAuth 인증 코드가 없습니다. 다시 로그인해 주세요.");
      return;
    }
    let active = true;
    void exchangeOAuthCodeOnce(code)
      .then(() => {
        if (active) navigate("/projects");
      })
      .catch((reason: unknown) => {
        if (active) setError(authErrorMessage(reason, "OAuth 로그인을 완료하지 못했습니다."));
      });
    return () => { active = false; };
  }, [code]);

  return (
    <AuthCard>
      <Brand />
      <h1>소셜 로그인</h1>
      {error
        ? <div className="banner-danger auth-banner" role="alert">{error}</div>
        : <div className="banner-info auth-banner" role="status">로그인을 완료하는 중입니다…</div>}
      <p className="auth-card-link"><a href={appHref("/login")}>로그인으로 돌아가기</a></p>
    </AuthCard>
  );
}
