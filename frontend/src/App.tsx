import { useEffect, useState, useSyncExternalStore } from "react";

import { useAdminNavigation } from "./components/AppShell";
import { LoginPage, OAuthCallbackPage, PasswordResetPage, SignupPage } from "./pages/AuthPages";
import { ProjectsPage } from "./pages/ProjectsPage";
import { RunDetailPage } from "./pages/RunDetailPage";
import { RunsPage } from "./pages/RunsPage";
import { TrainPage } from "./pages/TrainPage";
import { UploadPage } from "./pages/UploadPage";
import { ViewerPage } from "./pages/Viewer";
import { InviteAcceptPage } from "./pages/InviteAcceptPage";
import { MembersPage } from "./pages/MembersPage";
import { appHref, getLocationSnapshot, subscribeNavigation } from "./navigation";
import { AdminPage } from "./pages/AdminPage";
import { AuthApiError, getCurrentUser, refreshAuthSession } from "./api/auth";
import { getAuthSnapshot, setAuthSession, useAuthSession } from "./store/auth";
import { dismissWorkspaceMessage, getWorkspaceSnapshot, initializeWorkspaces, resetWorkspaces, useWorkspace } from "./store/workspace";
import { ThemeProvider } from "./theme";

function Screen() {
  useSyncExternalStore(subscribeNavigation, getLocationSnapshot, getLocationSnapshot);
  const [roleMessage, setRoleMessage] = useState<string | null>(null);
  const [accountError, setAccountError] = useState<string | null>(null);
  const [accountRetry, setAccountRetry] = useState(0);
  const path = window.location.pathname.replace(/\/+$/, "") || "/";
  const session = useAuthSession();
  const authenticated = Boolean(session.accessToken && session.refreshToken);
  const adminVisible = useAdminNavigation(path === "/");
  // auth-service currently sends reset emails to /reset?token=... while the
  // public product route is /password-reset. Accept both without exposing a
  // second navigation link or changing the read-only auth module.
  const passwordResetPath = path === "/password-reset" || path === "/reset";
  const publicPath = path === "/"
    || path === "/login"
    || path === "/signup"
    || passwordResetPath
    || path === "/auth/callback";

  useEffect(() => {
    if (authenticated || publicPath) return;
    const next = `${window.location.pathname}${window.location.search}${window.location.hash}`;
    window.location.replace(appHref(`/login?next=${encodeURIComponent(next)}`));
  }, [authenticated, publicPath]);

  useEffect(() => {
    if (!authenticated) resetWorkspaces();
  }, [authenticated]);
  useEffect(() => {
    if (!authenticated || session.user || !session.accessToken) return;
    let alive = true;
    const token = session.accessToken;
    setAccountError(null);
    void (async () => {
      try {
        const user = await getCurrentUser(token);
        const current = getAuthSnapshot();
        if (alive && current.accessToken === token && current.refreshToken) {
          setAuthSession({ access_token: token, refresh_token: current.refreshToken }, user);
        }
      } catch (error: unknown) {
        if (!alive) return;
        if (error instanceof AuthApiError && error.status === 401) {
          try {
            await refreshAuthSession(token);
            // The token change starts a fresh account lookup in this effect.
            return;
          } catch (refreshError: unknown) {
            if (alive) setAccountError(refreshError instanceof Error ? refreshError.message : "계정 정보를 불러오지 못했어요.");
          }
        } else {
          setAccountError(error instanceof Error ? error.message : "계정 정보를 불러오지 못했어요.");
        }
      }
    })();
    return () => { alive = false; };
  }, [authenticated, session.user, session.accessToken, accountRetry]);
  useEffect(() => {
    const onRoleError = (event: Event) => setRoleMessage((event as CustomEvent<string>).detail);
    window.addEventListener("annodock:role-error", onRoleError);
    return () => window.removeEventListener("annodock:role-error", onRoleError);
  }, []);

  useEffect(() => {
    if (path === "/" && authenticated && adminVisible) {
      window.location.replace(appHref("/admin"));
    }
  }, [path, authenticated, adminVisible]);

  if (!authenticated && !publicPath) return null;

  if (path === "/" && !authenticated) return <LoginPage />;
  if (path === "/login") return <LoginPage />;
  if (path === "/signup") return <SignupPage />;
  if (passwordResetPath) return <PasswordResetPage />;
  if (path === "/auth/callback") return <OAuthCallbackPage />;

  if (!session.user) return <main className="not-found">{accountError
    ? <><p role="alert">{accountError}</p><button className="btn btn-secondary" onClick={() => setAccountRetry(value => value + 1)}>다시 시도</button></>
    : <p role="status">계정 정보를 불러오는 중…</p>}</main>;
  const requestedId = new URLSearchParams(window.location.search).get("ws");
  return <>
    <WorkspaceScreen key={`${session.user.id}:${requestedId ?? "default"}`} userId={session.user.id} requestedId={requestedId} path={path} />
    {roleMessage && <div className="toast workspace-toast" role="alert">{roleMessage}<button className="toast-action" onClick={() => setRoleMessage(null)}>닫기</button></div>}
  </>;
}

function WorkspaceScreen({ userId, requestedId, path }: { userId: number; requestedId: string | null; path: string }) {
  const workspace = useWorkspace();
  const [resolved, setResolved] = useState(false);
  const [bootError, setBootError] = useState(false);
  const [retry, setRetry] = useState(0);
  useEffect(() => {
    let alive = true;
    setResolved(false);
    setBootError(false);
    void initializeWorkspaces(userId, requestedId).then(() => {
      if (alive) {
        setBootError(getWorkspaceSnapshot().status === "error");
        setResolved(true);
      }
    });
    return () => { alive = false; };
  }, [userId, requestedId, retry]);
  if (!resolved || workspace.userId !== userId) {
    return <main className="not-found" role="status">워크스페이스를 불러오는 중…</main>;
  }
  if (workspace.notFound) return <NotFound clearWorkspaceQuery />;
  if (bootError) return <main className="not-found"><p role="alert">{workspace.error}</p><button className="btn btn-secondary" onClick={() => setRetry(value => value + 1)}>다시 시도</button></main>;
  return <>
    <ScopedRoutes key={workspace.active?.id} path={path} />
    {workspace.message && <div className="toast workspace-toast" role="status">{workspace.message}<button className="toast-action" onClick={dismissWorkspaceMessage}>닫기</button></div>}
  </>;
}

function ScopedRoutes({ path }: { path: string }) {
  if (path === "/" || path === "/projects" || path === "/datasets") return <ProjectsPage />;
  if (path === "/projects/new") return <ProjectsPage initialDialogOpen />;
  if (path === "/upload") return <UploadPage />;
  if (path === "/admin") return <AdminPage />;
  if (path === "/members") return <MembersPage />;
  const inviteMatch = path.match(/^\/invites\/([^/]+)$/);
  if (inviteMatch) return <InviteAcceptPage token={inviteMatch[1]} />;
  const datasetMatch = path.match(/^\/datasets\/(\d+)\/(viewer|train)$/);
  if (datasetMatch) {
    const datasetId = Number(datasetMatch[1]);
    if (Number.isSafeInteger(datasetId) && datasetId > 0) {
      return datasetMatch[2] === "viewer"
        ? <ViewerPage datasetId={datasetId} />
        : <TrainPage datasetId={datasetId} />;
    }
  }
  if (path === "/runs") return <RunsPage />;

  const runMatch = path.match(/^\/runs\/(\d+)$/);
  if (runMatch) return <RunDetailPage runId={Number(runMatch[1])} />;

  return <NotFound />;
}

function NotFound({ clearWorkspaceQuery = false }: { clearWorkspaceQuery?: boolean }) {
  return (
    <main className="not-found">
      <h1>페이지를 찾을 수 없습니다</h1>
      <a href={clearWorkspaceQuery ? "/projects" : appHref("/projects")}>프로젝트로 돌아가기</a>
    </main>
  );
}

export function App() {
  return <ThemeProvider><Screen /></ThemeProvider>;
}
