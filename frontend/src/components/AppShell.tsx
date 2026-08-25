import {
  useEffect,
  useRef,
  useState,
  type PropsWithChildren,
  type ReactNode,
} from "react";

import { logoutCurrentSession } from "../api/auth";
import {
  getStorageQuota,
  resetStorageQuotaCache,
  STORAGE_QUOTA_INVALIDATED_EVENT,
  type StorageQuota,
} from "../api/client";
import { appHref } from "../navigation";
import { useAuthSession } from "../store/auth";
import { getAccountPresentation } from "../utils/accountPresentation";
import { probeAdminOverview, resetAdminProbe } from "../utils/adminAccess";
import { formatBytes } from "../utils/formatBytes";
import { Icon } from "./Icon";
import { ThemeToggle } from "./ThemeToggle";

interface AppShellProps extends PropsWithChildren {
  active: "projects" | "runs" | "admin";
  breadcrumb?: ReactNode;
  mainClassName?: string;
}

const STORAGE_QUOTA_RETRY_MS = 3_000;

export function Brand({ inverse = false }: { inverse?: boolean }) {
  return (
    <span className={`brand${inverse ? " brand-inverse" : ""}`}>
      <img className="brand-mark brand-mark-light" src="/assets/annodock-mark-on-light.svg" alt="" />
      <img className="brand-mark brand-mark-dark" src="/assets/annodock-mark-on-dark.svg" alt="" />
      <span className="brand-word">
        Anno<span>dock</span>
      </span>
    </span>
  );
}

export function StorageMeter({
  compact = false,
  quota = null,
}: {
  compact?: boolean;
  quota?: StorageQuota | null;
}) {
  const usageLabel = quota === null
    ? null
    : `${formatBytes(quota.used_bytes)} / ${formatBytes(quota.limit_bytes)}`;
  const ariaLabel = usageLabel === null
    ? "스토리지 사용량 정보 없음"
    : `스토리지 사용 중 ${usageLabel}`;

  if (compact) {
    return (
      <span className="storage-pill" aria-label={ariaLabel}>
        <span>스토리지</span>
        <span className="storage-pill-values">
          <span className="mono">{usageLabel ?? "—"}</span>
        </span>
      </span>
    );
  }

  return (
    <div className="sidebar-storage">
      <div className="sidebar-storage-label">
        <strong>스토리지</strong>
        <span className="mono">{usageLabel ?? "—"}</span>
      </div>
    </div>
  );
}

export function AppShell({
  active,
  breadcrumb,
  mainClassName,
  children,
}: AppShellProps) {
  const session = useAuthSession();
  const [loggingOut, setLoggingOut] = useState(false);
  // Server response is the source of truth for admin access; nothing is
  // persisted client-side, so the link simply stays hidden until the probe
  // confirms the grant for this page load.
  const [adminVisible, setAdminVisible] = useState(false);
  const [storageQuota, setStorageQuota] = useState<StorageQuota | null>(null);
  const storageTokenKey = session.accessToken && session.refreshToken
    ? session.accessToken
    : null;

  useEffect(() => {
    setStorageQuota(null);
    let alive = true;
    let requestVersion = 0;
    let retryTimer: number | null = null;
    const clearStorageQuotaRetry = () => {
      if (retryTimer === null) return;
      window.clearTimeout(retryTimer);
      retryTimer = null;
    };
    const scheduleStorageQuotaRetry = () => {
      clearStorageQuotaRetry();
      if (!alive || storageTokenKey === null) return;
      retryTimer = window.setTimeout(loadStorageQuota, STORAGE_QUOTA_RETRY_MS);
    };
    const loadStorageQuota = () => {
      clearStorageQuotaRetry();
      const version = requestVersion += 1;
      if (storageTokenKey === null) return;
      getStorageQuota(storageTokenKey)
        .then((quota) => {
          if (alive && version === requestVersion) setStorageQuota(quota);
        })
        .catch(() => {
          if (alive && version === requestVersion) scheduleStorageQuotaRetry();
        });
    };
    loadStorageQuota();
    window.addEventListener(STORAGE_QUOTA_INVALIDATED_EVENT, loadStorageQuota);
    return () => {
      alive = false;
      clearStorageQuotaRetry();
      window.removeEventListener(STORAGE_QUOTA_INVALIDATED_EVENT, loadStorageQuota);
    };
  }, [storageTokenKey]);

  useEffect(() => {
    let alive = true;
    probeAdminOverview()
      .then((overview) => {
        if (alive) setAdminVisible(overview !== null);
      })
      .catch(() => {
        // Network or auth hiccups keep the link hidden; the probe result is
        // advisory here — the /admin route re-checks with the server anyway.
      });
    return () => {
      alive = false;
    };
  }, []);
  const [accountMenuOpen, setAccountMenuOpen] = useState(false);
  const accountMenuRef = useRef<HTMLDivElement>(null);
  const accountTriggerRef = useRef<HTMLButtonElement>(null);
  const logoutItemRef = useRef<HTMLButtonElement>(null);
  const { label: accountLabel, initials } = getAccountPresentation(session.user);

  useEffect(() => {
    if (!accountMenuOpen) return;

    logoutItemRef.current?.focus();
    const closeOnOutsidePointer = (event: PointerEvent) => {
      if (!accountMenuRef.current?.contains(event.target as Node)) {
        setAccountMenuOpen(false);
      }
    };
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      setAccountMenuOpen(false);
      accountTriggerRef.current?.focus();
    };

    document.addEventListener("pointerdown", closeOnOutsidePointer);
    document.addEventListener("keydown", closeOnEscape);
    return () => {
      document.removeEventListener("pointerdown", closeOnOutsidePointer);
      document.removeEventListener("keydown", closeOnEscape);
    };
  }, [accountMenuOpen]);

  const handleLogout = async () => {
    resetAdminProbe();
    resetStorageQuotaCache();
    if (loggingOut) return;
    setAccountMenuOpen(false);
    setLoggingOut(true);
    try {
      await logoutCurrentSession();
    } finally {
      window.location.replace(appHref("/login"));
    }
  };

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <a className="sidebar-brand" href={appHref("/projects")} aria-label="Annodock 홈">
          <Brand inverse />
        </a>
        <div className="sidebar-kicker">워크스페이스</div>
        <nav className="sidebar-nav" aria-label="워크스페이스">
          <a
            className="sidebar-link"
            href={appHref("/projects")}
            aria-current={active === "projects" ? "page" : undefined}
          >
            <Icon name="folder" size={15} />
            프로젝트
          </a>
          <a
            className="sidebar-link"
            href={appHref("/runs")}
            aria-current={active === "runs" ? "page" : undefined}
          >
            <Icon name="cpu" size={15} />
            AI 학습
          </a>
          {adminVisible && (
            <a
              className="sidebar-link"
              href={appHref("/admin")}
              aria-current={active === "admin" ? "page" : undefined}
            >
              <Icon name="users" size={15} />
              관리자
            </a>
          )}
        </nav>
        <StorageMeter quota={storageQuota} />
      </aside>

      <div className="shell-content">
        <header className="shell-header">
          <div className="breadcrumbs">{breadcrumb ?? "워크스페이스"}</div>
          <div className="header-actions">
            <ThemeToggle />
            <StorageMeter compact quota={storageQuota} />
            <div className="account-menu" ref={accountMenuRef}>
              <button
                className="user-chip"
                type="button"
                aria-label={`${accountLabel} 계정 메뉴`}
                aria-haspopup="menu"
                aria-expanded={accountMenuOpen}
                aria-controls="account-menu-popover"
                disabled={loggingOut}
                ref={accountTriggerRef}
                onClick={() => setAccountMenuOpen((open) => !open)}
              >
                <span className="avatar">{initials}</span>
                <span>{loggingOut ? "로그아웃 중…" : accountLabel}</span>
                <span className={`account-menu-chevron${accountMenuOpen ? " is-open" : ""}`}>
                  <Icon name="chevron-down" size={13} />
                </span>
              </button>
              {accountMenuOpen ? (
                <div
                  className="account-menu-popover"
                  id="account-menu-popover"
                  role="menu"
                  aria-label={`${accountLabel} 계정 메뉴`}
                >
                  <button
                    className="account-menu-item"
                    type="button"
                    role="menuitem"
                    disabled={loggingOut}
                    ref={logoutItemRef}
                    onClick={() => void handleLogout()}
                  >
                    {loggingOut ? "로그아웃 중…" : "로그아웃"}
                  </button>
                </div>
              ) : null}
            </div>
          </div>
        </header>
        <main className={mainClassName ? `shell-main ${mainClassName}` : "shell-main"}>
          {children}
        </main>
      </div>
    </div>
  );
}

export function BreadcrumbLink({ href, children }: PropsWithChildren<{ href: string }>) {
  return <a href={appHref(href)}>{children}</a>;
}
