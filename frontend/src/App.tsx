import { useEffect } from "react";

import { LoginPage, OAuthCallbackPage, PasswordResetPage, SignupPage } from "./pages/AuthPages";
import { ProjectsPage } from "./pages/ProjectsPage";
import { RunDetailPage } from "./pages/RunDetailPage";
import { RunsPage } from "./pages/RunsPage";
import { TrainPage } from "./pages/TrainPage";
import { UploadPage } from "./pages/UploadPage";
import { ViewerPage } from "./pages/Viewer";
import { appHref } from "./navigation";
import { useAuthSession } from "./store/auth";
import { ThemeProvider } from "./theme";

function Screen() {
  const path = window.location.pathname.replace(/\/+$/, "") || "/";
  const session = useAuthSession();
  const authenticated = Boolean(session.accessToken && session.refreshToken);
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

  if (!authenticated && !publicPath) return null;

  if (path === "/") return authenticated ? <ProjectsPage /> : <LoginPage />;
  if (path === "/login") return <LoginPage />;
  if (path === "/signup") return <SignupPage />;
  if (passwordResetPath) return <PasswordResetPage />;
  if (path === "/auth/callback") return <OAuthCallbackPage />;
  if (path === "/projects" || path === "/datasets") return <ProjectsPage />;
  if (path === "/projects/new") return <ProjectsPage initialDialogOpen />;
  if (path === "/upload") return <UploadPage />;
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

  return (
    <main className="not-found">
      <h1>페이지를 찾을 수 없습니다</h1>
      <a href="/projects">프로젝트로 돌아가기</a>
    </main>
  );
}

export function App() {
  return <ThemeProvider><Screen /></ThemeProvider>;
}
