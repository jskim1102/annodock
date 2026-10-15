import { useEffect, useRef, useState, type KeyboardEvent } from "react";

import { switchWorkspace, useWorkspace, WORKSPACE_ROLE_LABELS } from "../store/workspace";
import { Icon } from "./Icon";
import { NewWorkspaceDialog } from "./NewWorkspaceDialog";

export function WorkspaceSwitcher() {
  const { items, active } = useWorkspace();
  const [open, setOpen] = useState(false);
  const [creating, setCreating] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const selected = menuRef.current?.querySelector<HTMLButtonElement>('[aria-current="true"]');
    selected?.focus();
    const outside = (event: PointerEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) setOpen(false);
    };
    const escape = (event: globalThis.KeyboardEvent) => {
      if (event.key === "Escape") {
        setOpen(false);
        triggerRef.current?.focus();
      }
    };
    document.addEventListener("pointerdown", outside);
    document.addEventListener("keydown", escape);
    return () => {
      document.removeEventListener("pointerdown", outside);
      document.removeEventListener("keydown", escape);
    };
  }, [open]);

  const moveFocus = (event: KeyboardEvent<HTMLDivElement>) => {
    const options = Array.from(menuRef.current?.querySelectorAll<HTMLButtonElement>('[role="menuitem"]') ?? []);
    const index = options.indexOf(document.activeElement as HTMLButtonElement);
    let next: number;
    if (event.key === "ArrowDown") next = (index + 1) % options.length;
    else if (event.key === "ArrowUp") next = (index - 1 + options.length) % options.length;
    else if (event.key === "Home") next = 0;
    else if (event.key === "End") next = options.length - 1;
    else {
      if (event.key === "Tab") setOpen(false);
      return;
    }
    event.preventDefault();
    options[next]?.focus();
  };

  return (
    <div className="ws-switcher-wrap" ref={rootRef}>
      <button
        className="ws-switcher" type="button" ref={triggerRef}
        aria-haspopup="menu" aria-expanded={open} aria-label="워크스페이스 선택"
        disabled={!active} onClick={() => setOpen((value) => !value)}
        onKeyDown={(event) => {
          if (event.key === "ArrowDown" || event.key === "ArrowUp") {
            event.preventDefault();
            setOpen(true);
          }
        }}
      >
        <span className="ws-avatar" aria-hidden="true">{active?.name.slice(0, 1) ?? "—"}</span>
        <span className="ws-label">
          <span className="ws-name">{active?.name ?? "워크스페이스"}</span>
          <span className="ws-role">{active ? WORKSPACE_ROLE_LABELS[active.role] : "불러오는 중"}</span>
        </span>
        <span className="ws-caret"><Icon name="chevron-down" size={14} /></span>
      </button>
      {open ? (
        <div className="ws-menu" role="menu" aria-label="워크스페이스" ref={menuRef} onKeyDown={moveFocus}>
          <div className="ws-menu-label">워크스페이스</div>
          {items.map((item) => (
            <button
              className="ws-menu-item" type="button" role="menuitem" key={item.id}
              aria-current={item.id === active?.id ? "true" : undefined}
              onClick={() => { setOpen(false); switchWorkspace(item.id); triggerRef.current?.focus(); }}
            >
              <span className="ws-avatar" aria-hidden="true">{item.name.slice(0, 1)}</span>
              <span className="ws-label">
                <span className="ws-name">{item.name}</span>
                <span className="ws-role">{item.kind === "personal" ? "나만 사용" : WORKSPACE_ROLE_LABELS[item.role]}</span>
              </span>
              {item.id === active?.id ? <span className="ws-check" aria-hidden="true">✓</span> : null}
            </button>
          ))}
          <div className="ws-menu-foot">
            <button className="btn btn-ghost" type="button" role="menuitem" onClick={() => { setOpen(false); setCreating(true); }}>
              <Icon name="plus" size={14} /> 새 팀 만들기
            </button>
          </div>
        </div>
      ) : null}
      {creating ? <NewWorkspaceDialog onClose={() => { setCreating(false); triggerRef.current?.focus(); }} /> : null}
    </div>
  );
}
