import { useEffect, useState } from "react";

import {
  ApiError,
  getAdminUsers,
  type AdminOverview,
  type AdminUserRow,
} from "../api/client";
import { AppShell } from "../components/AppShell";
import { probeAdminOverview } from "../utils/adminAccess";
import { formatBytes } from "../utils/formatBytes";

function formatDate(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleString("ko-KR", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function displayName(user: AdminUserRow): string {
  return user.email ?? user.username ?? `사용자 ${user.id}`;
}

function NotFoundScreen() {
  // Mirrors the router fallback so a non-admin visit to /admin is
  // indistinguishable from a URL that does not exist.
  return (
    <main className="not-found">
      <h1>페이지를 찾을 수 없습니다</h1>
      <a href="/projects">프로젝트로 돌아가기</a>
    </main>
  );
}

type AdminState =
  | { phase: "loading" }
  | { phase: "denied" }
  | { phase: "error"; message: string }
  | { phase: "ready"; overview: AdminOverview; users: AdminUserRow[] };

const STACK_STYLE = {
  display: "flex",
  flexDirection: "column",
  gap: "var(--space-6)",
} as const;

const STAT_ROW_STYLE = {
  display: "flex",
  gap: "var(--space-4)",
  flexWrap: "wrap",
} as const;

const STAT_CARD_STYLE = { flex: "1 1 220px" } as const;

export function AdminPage() {
  const [state, setState] = useState<AdminState>({ phase: "loading" });

  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const overview = await probeAdminOverview();
        if (!alive) return;
        if (overview === null) {
          setState({ phase: "denied" });
          return;
        }
        const { users } = await getAdminUsers();
        if (!alive) return;
        setState({ phase: "ready", overview, users });
      } catch (error) {
        if (!alive) return;
        if (error instanceof ApiError && error.status === 404) {
          setState({ phase: "denied" });
          return;
        }
        const message = error instanceof Error
          ? error.message
          : "관리자 정보를 불러오지 못했습니다.";
        setState({ phase: "error", message });
      }
    })();
    return () => {
      alive = false;
    };
  }, []);

  if (state.phase === "denied") return <NotFoundScreen />;
  // While the server verdict is pending, render nothing: a non-admin visit
  // must be indistinguishable from a nonexistent route, and flashing the
  // admin shell before the 404 would give the page away.
  if (state.phase === "loading") return null;
  if (state.phase === "error") {
    return (
      <AppShell active="admin" breadcrumb="관리자">
        <p className="card-meta" role="alert">{state.message}</p>
      </AppShell>
    );
  }

  const { overview, users } = state;
  return (
    <AppShell active="admin" breadcrumb="관리자">
      <div style={STACK_STYLE}>
        <section style={STAT_ROW_STYLE} aria-label="서비스 현황">
          <div className="card" style={STAT_CARD_STYLE}>
            <div className="card-meta">전체 가입자</div>
            <div className="card-title mono">
              {overview.user_count.toLocaleString()}명
            </div>
          </div>
          <div className="card" style={STAT_CARD_STYLE}>
            <div className="card-meta">저장 용량 합계</div>
            <div className="card-title mono">
              {formatBytes(overview.storage_total_bytes)}
            </div>
          </div>
        </section>

        <section className="card" aria-label="전체 사용자">
          <h2 className="card-title">전체 사용자 (용량 내림차순)</h2>
          <table className="table">
            <thead>
              <tr>
                <th>이메일</th>
                <th>로그인 방식</th>
                <th>가입일</th>
                <th>사용 용량</th>
              </tr>
            </thead>
            <tbody>
              {users.map((user) => (
                <tr key={user.id}>
                  <td>{displayName(user)}</td>
                  <td>{user.login_methods.length ? user.login_methods.join(" · ") : "—"}</td>
                  <td className="mono">{formatDate(user.created_at)}</td>
                  <td className="mono">{formatBytes(user.bytes_used)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      </div>
    </AppShell>
  );
}
