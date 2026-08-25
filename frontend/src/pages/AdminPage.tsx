import { useEffect, useRef, useState } from "react";

import {
  ApiError,
  deleteAdminUser,
  getAdminOverview,
  getAdminUsers,
  invalidateStorageQuotaCache,
  type AdminOverview,
  type AdminUserRow,
  updateAdminUserQuota,
} from "../api/client";
import { AppShell } from "../components/AppShell";
import { Icon } from "../components/Icon";
import { probeAdminOverview } from "../utils/adminAccess";
import { formatBytes } from "../utils/formatBytes";
import { quotaBytesFromGiB, quotaGiBFromBytes } from "../utils/quotaLimit";

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

interface AdminUserDeleteConfirmation {
  code: "admin-user-delete-confirmation-required";
  requires_confirmation: true;
  warning: string;
  email: string | null;
  username: string | null;
  project_count: number;
  dataset_count: number;
  bytes_used: number;
}

interface PendingAdminUserDeletion {
  user: AdminUserRow;
  detail: AdminUserDeleteConfirmation;
}

function userDeleteConfirmationFrom(
  error: unknown,
): AdminUserDeleteConfirmation | null {
  if (!(error instanceof ApiError) || error.status !== 409) return null;
  const detail = error.detail;
  if (!detail || typeof detail !== "object") return null;
  const candidate = detail as Partial<AdminUserDeleteConfirmation>;
  if (
    candidate.code !== "admin-user-delete-confirmation-required"
    || candidate.requires_confirmation !== true
    || typeof candidate.warning !== "string"
    || typeof candidate.project_count !== "number"
    || typeof candidate.dataset_count !== "number"
    || typeof candidate.bytes_used !== "number"
  ) {
    return null;
  }
  return candidate as AdminUserDeleteConfirmation;
}

function actionErrorMessage(error: unknown, fallback: string): string {
  if (error instanceof ApiError) {
    const detail = error.detail;
    if (detail && typeof detail === "object" && "message" in detail) {
      const message = (detail as { message?: unknown }).message;
      if (typeof message === "string") return message;
    }
    if (typeof detail === "string") return detail;
  }
  if (error instanceof Error) return error.message;
  return fallback;
}

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
  const [selectedUserIds, setSelectedUserIds] = useState<Set<number>>(
    new Set(),
  );
  const [confirmations, setConfirmations] = useState<
    PendingAdminUserDeletion[] | null
  >(null);
  const [deleting, setDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const [quotaTarget, setQuotaTarget] = useState<AdminUserRow | null>(null);
  const [updatingQuota, setUpdatingQuota] = useState(false);
  const [quotaError, setQuotaError] = useState<string | null>(null);

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

  const refresh = async () => {
    const [overview, { users }] = await Promise.all([
      getAdminOverview(),
      getAdminUsers(),
    ]);
    setState({ phase: "ready", overview, users });
    const availableUserIds = new Set(users.map((user) => user.id));
    setSelectedUserIds((current) => new Set(
      [...current].filter((userId) => availableUserIds.has(userId)),
    ));
  };

  const toggleUserSelected = (userId: number) => {
    setSelectedUserIds((current) => {
      const next = new Set(current);
      if (next.has(userId)) next.delete(userId);
      else next.add(userId);
      return next;
    });
  };

  const saveUserQuota = async (limitBytes: number) => {
    if (quotaTarget === null || updatingQuota || deleting) return;
    setUpdatingQuota(true);
    setQuotaError(null);
    try {
      await updateAdminUserQuota(quotaTarget.id, limitBytes);
      invalidateStorageQuotaCache();
      await refresh();
      setQuotaTarget(null);
    } catch (error) {
      setQuotaError(actionErrorMessage(
        error,
        "할당 용량을 변경하지 못했습니다.",
      ));
    } finally {
      setUpdatingQuota(false);
    }
  };

  const prepareSelectedUsersDelete = async (targets: AdminUserRow[]) => {
    if (targets.length === 0 || deleting) return;
    setDeleteError(null);
    setDeleting(true);
    try {
      const pending: PendingAdminUserDeletion[] = [];
      for (const user of targets) {
        try {
          await deleteAdminUser(user.id);
          throw new Error("삭제 확인 정보를 불러오지 못했습니다.");
        } catch (error) {
          const detail = userDeleteConfirmationFrom(error);
          if (!detail) throw error;
          pending.push({ user, detail });
        }
      }
      setConfirmations(pending);
    } catch (error) {
      setDeleteError(actionErrorMessage(
        error,
        "사용자를 삭제하지 못했습니다.",
      ));
    } finally {
      setDeleting(false);
    }
  };

  const deleteSelectedUsers = async () => {
    if (confirmations === null || confirmations.length === 0 || deleting) return;
    const targets = [...confirmations];
    const completedIds = new Set<number>();
    let deletedCount = 0;
    setDeleting(true);
    setDeleteError(null);
    try {
      for (const target of targets) {
        await deleteAdminUser(target.user.id, true);
        completedIds.add(target.user.id);
        deletedCount += 1;
      }
    } catch (error) {
      const message = actionErrorMessage(
        error,
        "사용자를 삭제하지 못했습니다.",
      );
      setDeleteError(deletedCount > 0
        ? `${deletedCount}명은 삭제했지만 나머지는 처리하지 못했습니다. ${message}`
        : message);
    } finally {
      try {
        await refresh();
      } catch (error) {
        setDeleteError((current) => current ?? actionErrorMessage(
          error,
          "사용자 목록을 새로고침하지 못했습니다.",
        ));
      }
      setSelectedUserIds((current) => {
        const next = new Set(current);
        for (const id of completedIds) next.delete(id);
        return next;
      });
      const remaining = targets.filter(
        (target) => !completedIds.has(target.user.id),
      );
      setConfirmations(remaining.length > 0 ? remaining : null);
      setDeleting(false);
    }
  };

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
  const selectedUsers = users.filter((user) => selectedUserIds.has(user.id));
  const allUsersSelected = users.length > 0
    && selectedUsers.length === users.length;
  const someUsersSelected = selectedUsers.length > 0 && !allUsersSelected;
  const operationBusy = deleting || updatingQuota;
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

        <section className="card admin-users-card" aria-label="전체 사용자">
          <header className="admin-users-header">
            <h2 className="card-title">전체 사용자 (용량 내림차순)</h2>
            <button
              className="btn btn-danger btn-sm"
              type="button"
              aria-label="선택한 사용자 삭제"
              disabled={selectedUsers.length === 0 || operationBusy}
              onClick={() => void prepareSelectedUsersDelete(selectedUsers)}
            >
              <Icon name="trash" size={14} />
              {deleting && confirmations === null ? "확인 중…" : "삭제"}
            </button>
          </header>
          {deleteError && !confirmations ? (
            <p className="error-text" role="alert">{deleteError}</p>
          ) : null}
          <table className="table admin-users-table">
            <colgroup>
              <col className="admin-user-select-column" />
              <col />
              <col />
              <col />
              <col />
              <col />
            </colgroup>
            <thead>
              <tr>
                <th className="admin-user-select-cell" scope="col">
                  <button
                    className={`checkbox${allUsersSelected ? " is-on" : ""}`}
                    type="button"
                    role="checkbox"
                    disabled={users.length === 0 || operationBusy}
                    aria-checked={someUsersSelected ? "mixed" : allUsersSelected}
                    aria-label="전체 사용자 선택"
                    onClick={() => setSelectedUserIds(
                      allUsersSelected
                        ? new Set()
                        : new Set(users.map((user) => user.id)),
                    )}
                  >
                    {allUsersSelected ? <Icon name="check" size={10} /> : null}
                  </button>
                </th>
                <th>이메일</th>
                <th>로그인 방식</th>
                <th>가입일</th>
                <th>사용 용량</th>
                <th>할당 용량</th>
              </tr>
            </thead>
            <tbody>
              {users.map((user) => (
                <tr key={user.id} aria-selected={selectedUserIds.has(user.id)}>
                  <td className="admin-user-select-cell">
                    <button
                      className={`checkbox${selectedUserIds.has(user.id) ? " is-on" : ""}`}
                      type="button"
                      role="checkbox"
                      disabled={operationBusy}
                      aria-checked={selectedUserIds.has(user.id)}
                      aria-label={`${displayName(user)} 선택`}
                      onClick={() => toggleUserSelected(user.id)}
                    >
                      {selectedUserIds.has(user.id)
                        ? <Icon name="check" size={10} />
                        : null}
                    </button>
                  </td>
                  <td>{displayName(user)}</td>
                  <td>{user.login_methods.length ? user.login_methods.join(" · ") : "—"}</td>
                  <td className="mono">{formatDate(user.created_at)}</td>
                  <td className="mono">{formatBytes(user.bytes_used)}</td>
                  <td>
                    <div className="admin-user-quota">
                      <span className="mono">{formatBytes(user.limit_bytes)}</span>
                      <button
                        className="btn btn-primary btn-sm"
                        type="button"
                        disabled={operationBusy}
                        aria-label={`${displayName(user)} 할당 용량 변경`}
                        onClick={() => {
                          setQuotaError(null);
                          setQuotaTarget(user);
                        }}
                      >변경</button>
                    </div>
                  </td>
                </tr>
              ))}
              {users.length === 0 ? (
                <tr><td colSpan={6}>등록된 사용자가 없습니다.</td></tr>
              ) : null}
            </tbody>
          </table>
        </section>
      </div>

      {confirmations ? (
        <DeleteUsersDialog
          targets={confirmations}
          busy={deleting}
          error={deleteError}
          onClose={() => {
            if (!deleting) {
              setConfirmations(null);
              setDeleteError(null);
            }
          }}
          onConfirm={() => void deleteSelectedUsers()}
        />
      ) : null}

      {quotaTarget ? (
        <QuotaLimitDialog
          key={quotaTarget.id}
          user={quotaTarget}
          busy={updatingQuota}
          error={quotaError}
          onClose={() => {
            if (!updatingQuota) {
              setQuotaTarget(null);
              setQuotaError(null);
            }
          }}
          onConfirm={(limitBytes) => void saveUserQuota(limitBytes)}
        />
      ) : null}
    </AppShell>
  );
}

interface QuotaLimitDialogProps {
  user: AdminUserRow;
  busy: boolean;
  error: string | null;
  onClose: () => void;
  onConfirm: (limitBytes: number) => void;
}

function QuotaLimitDialog({
  user,
  busy,
  error,
  onClose,
  onConfirm,
}: QuotaLimitDialogProps) {
  const [value, setValue] = useState(() => quotaGiBFromBytes(user.limit_bytes));
  const inputRef = useRef<HTMLInputElement>(null);
  const limitBytes = quotaBytesFromGiB(value);
  const belowCurrentUsage = limitBytes !== null && limitBytes < user.bytes_used;

  useEffect(() => {
    inputRef.current?.focus();
    inputRef.current?.select();
  }, []);

  useEffect(() => {
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !busy) onClose();
    };
    window.addEventListener("keydown", closeOnEscape);
    return () => window.removeEventListener("keydown", closeOnEscape);
  }, [busy, onClose]);

  return (
    <div
      className="dialog-backdrop"
      role="presentation"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget && !busy) onClose();
      }}
    >
      <section
        className="dialog project-action-dialog quota-limit-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="quota-limit-title"
        aria-describedby="quota-limit-description"
      >
        <h2 className="dialog-title" id="quota-limit-title">할당 용량 변경</h2>
        <p id="quota-limit-description">
          <strong>{displayName(user)}</strong> 계정에 사용할 수 있는 최대 저장
          용량을 설정합니다.
        </p>
        <form
          onSubmit={(event) => {
            event.preventDefault();
            if (limitBytes !== null && !busy) onConfirm(limitBytes);
          }}
        >
          <div className="field">
            <label htmlFor="admin-user-quota-limit">할당 용량 (GB)</label>
            <div className="quota-limit-input">
              <input
                className={`input${limitBytes === null ? " is-error" : ""}`}
                id="admin-user-quota-limit"
                ref={inputRef}
                type="number"
                min="0.1"
                step="0.1"
                required
                value={value}
                disabled={busy}
                aria-invalid={limitBytes === null}
                onChange={(event) => setValue(event.target.value)}
              />
              <span aria-hidden="true">GB</span>
            </div>
            {limitBytes === null ? (
              <div className="error-text" role="alert">
                0보다 큰 안전한 숫자를 입력해 주세요.
              </div>
            ) : null}
          </div>
          <dl className="quota-limit-summary">
            <div><dt>현재 사용량</dt><dd>{formatBytes(user.bytes_used)}</dd></div>
            <div><dt>현재 할당량</dt><dd>{formatBytes(user.limit_bytes)}</dd></div>
          </dl>
          <p className="hint">
            사용량보다 작게 설정해도 기존 데이터는 유지되며, 용량을 확보할
            때까지 새 업로드와 학습이 제한됩니다.
          </p>
          {belowCurrentUsage ? (
            <div className="quota-limit-warning" role="alert">
              새 할당량이 현재 사용량보다 작습니다. 저장 후 이 계정은 즉시
              용량 초과 상태가 됩니다.
            </div>
          ) : null}
          {error ? (
            <div className="error-text project-dialog-error" role="alert">
              {error}
            </div>
          ) : null}
          <div className="dialog-actions">
            <button
              className="btn btn-secondary"
              type="button"
              disabled={busy}
              onClick={onClose}
            >취소</button>
            <button
              className="btn btn-primary"
              type="submit"
              disabled={busy || limitBytes === null}
            >{busy ? "저장 중…" : "변경 저장"}</button>
          </div>
        </form>
      </section>
    </div>
  );
}

interface DeleteUsersDialogProps {
  targets: PendingAdminUserDeletion[];
  busy: boolean;
  error: string | null;
  onClose: () => void;
  onConfirm: () => void;
}

function DeleteUsersDialog({
  targets,
  busy,
  error,
  onClose,
  onConfirm,
}: DeleteUsersDialogProps) {
  const cancelRef = useRef<HTMLButtonElement>(null);
  const summary = targets.reduce(
    (total, target) => ({
      projectCount: total.projectCount + target.detail.project_count,
      datasetCount: total.datasetCount + target.detail.dataset_count,
      bytesUsed: total.bytesUsed + target.detail.bytes_used,
    }),
    { projectCount: 0, datasetCount: 0, bytesUsed: 0 },
  );

  useEffect(() => {
    cancelRef.current?.focus();
  }, []);

  useEffect(() => {
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !busy) onClose();
    };
    window.addEventListener("keydown", closeOnEscape);
    return () => window.removeEventListener("keydown", closeOnEscape);
  }, [busy, onClose]);

  return (
    <div
      className="dialog-backdrop"
      role="presentation"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget && !busy) onClose();
      }}
    >
      <section
        className="dialog project-action-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="delete-user-title"
        aria-describedby="delete-user-warning"
      >
        <h2 className="dialog-title" id="delete-user-title">사용자 삭제</h2>
        <div className="project-delete-warning" id="delete-user-warning">
          <strong>이 작업은 되돌릴 수 없습니다.</strong>
        </div>
        <p className="project-delete-copy">
          선택한 {targets.length.toLocaleString()}개 계정과 각 계정이 소유한
          데이터가 모두 삭제됩니다. 삭제 후 같은 이메일로 다시 가입할 수 있습니다.
        </p>
        <div className="project-delete-list" aria-label="삭제 대상 사용자">
          <strong>삭제 대상 사용자 {targets.length.toLocaleString()}명</strong>
          <ul>
            {targets.map((target) => (
              <li key={target.user.id}>{displayName(target.user)}</li>
            ))}
          </ul>
        </div>
        <div className="project-delete-list" aria-label="삭제 요약">
          <ul>
            <li>프로젝트 {summary.projectCount.toLocaleString()}개</li>
            <li>데이터셋 {summary.datasetCount.toLocaleString()}개</li>
            <li>저장 용량 {formatBytes(summary.bytesUsed)}</li>
          </ul>
        </div>
        {error ? (
          <div className="error-text project-dialog-error" role="alert">
            {error}
          </div>
        ) : null}
        <div className="dialog-actions">
          <button
            className="btn btn-secondary"
            type="button"
            disabled={busy}
            ref={cancelRef}
            onClick={onClose}
          >
            취소
          </button>
          <button
            className="btn btn-danger"
            type="button"
            disabled={busy}
            onClick={onConfirm}
          >
            {busy ? "삭제 중..." : `${targets.length.toLocaleString()}명 삭제`}
          </button>
        </div>
      </section>
    </div>
  );
}
