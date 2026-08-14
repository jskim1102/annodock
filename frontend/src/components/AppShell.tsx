import {
  useEffect,
  useRef,
  useState,
  type PropsWithChildren,
  type ReactNode,
} from "react";

import { logoutCurrentSession } from "../api/auth";
import { appHref } from "../navigation";
import { useAuthSession } from "../store/auth";
import { getAccountPresentation } from "../utils/accountPresentation";
import { Icon } from "./Icon";
import { ThemeToggle } from "./ThemeToggle";

interface AppShellProps extends PropsWithChildren {
  active: "projects" | "runs";
  breadcrumb?: ReactNode;
  mainClassName?: string;
}

export function Brand({ inverse = false }: { inverse?: boolean }) {
  return (
    <span className={`brand${inverse ? " brand-inverse" : ""}`}>
      Anno<span>dock</span>
    </span>
  );
}

export function StorageMeter({ compact = false }: { compact?: boolean }) {
  if (compact) {
    return (
      <span className="storage-pill" aria-label="스토리지 사용량 정보 없음">
        <span>스토리지</span>
        <span className="mono">—</span>
      </span>
    );
  }

  return (
    <div className="sidebar-storage">
      <div className="sidebar-storage-label">
        <span>스토리지</span>
        <span className="mono">—</span>
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
        </nav>
        <StorageMeter />
      </aside>

      <div className="shell-content">
        <header className="shell-header">
          <div className="breadcrumbs">{breadcrumb ?? "워크스페이스"}</div>
          <div className="header-actions">
            <ThemeToggle />
            <StorageMeter compact />
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
