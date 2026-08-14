import type { ReactNode, SVGProps } from "react";

export type IconName =
  | "box"
  | "archive"
  | "broom"
  | "check"
  | "chevron-down"
  | "chevron-left"
  | "chevron-right"
  | "chevron-up"
  | "cpu"
  | "download"
  | "expand"
  | "filter"
  | "folder"
  | "folder-solid"
  | "folder-up"
  | "grid"
  | "layers"
  | "list"
  | "more"
  | "moon"
  | "mouse"
  | "plus"
  | "refresh"
  | "search"
  | "sun"
  | "trash"
  | "undo"
  | "user"
  | "users"
  | "upload"
  | "warning"
  | "x"
  | "zoom-in"
  | "zoom-out";

interface IconProps extends SVGProps<SVGSVGElement> {
  name: IconName;
  size?: number;
}

export function Icon({ name, size = 16, ...props }: IconProps) {
  const common = {
    width: size,
    height: size,
    viewBox: "0 0 24 24",
    fill: "none",
    stroke: "currentColor",
    strokeWidth: 1.75,
    strokeLinecap: "round" as const,
    strokeLinejoin: "round" as const,
    "aria-hidden": true,
    ...props,
  };

  const paths: Record<IconName, ReactNode> = {
    archive: <><rect x="3" y="4" width="18" height="5" rx="1" /><path d="M5 9v11h14V9M10 13h4" /></>,
    box: <><rect x="4" y="4" width="16" height="16" rx="1" /><path d="M9 4v2M15 4v2M9 18v2M15 18v2M4 9h2M4 15h2M18 9h2M18 15h2" /></>,
    broom: <><path d="m14 4 6 6" /><path d="m12.5 5.5-8 8 6 6 8-8" /><path d="m4.5 13.5-1 6 6-1" /></>,
    check: <path d="m5 12 4 4L19 6" />,
    "chevron-down": <path d="m7 10 5 5 5-5" />,
    "chevron-left": <path d="m15 18-6-6 6-6" />,
    "chevron-right": <path d="m9 18 6-6-6-6" />,
    "chevron-up": <path d="m7 14 5-5 5 5" />,
    cpu: <><rect x="4" y="4" width="16" height="16" rx="2" /><rect x="9" y="9" width="6" height="6" /><path d="M9 1v3M15 1v3M9 20v3M15 20v3M1 9h3M1 15h3M20 9h3M20 15h3" /></>,
    download: <><path d="M12 3v12" /><path d="m7 10 5 5 5-5" /><path d="M5 21h14" /></>,
    expand: <><path d="M8 3H3v5M16 3h5v5M8 21H3v-5M16 21h5v-5" /></>,
    filter: <path d="M4 5h16l-6 7v5l-4 2v-7Z" />,
    folder: <path d="M20 20a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9L9.6 3.9A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2Z" />,
    "folder-solid": <><path d="M3 7a3 3 0 0 1 3-3h4.7l2.2 2.3H18a3 3 0 0 1 3 3v1.2H3Z" fill="currentColor" fillOpacity=".35" stroke="none" /><path d="M3 10a3 3 0 0 1 3-3h12a3 3 0 0 1 3 3v7a3 3 0 0 1-3 3H6a3 3 0 0 1-3-3Z" fill="currentColor" stroke="none" /></>,
    layers: <><path d="m12 2 8.5 4.5L12 11 3.5 6.5Z" /><path d="m3.5 12 8.5 4.5L20.5 12" /><path d="m3.5 17.5 8.5 4.5 8.5-4.5" /></>,
    "folder-up": <><path d="M3 19V6a2 2 0 0 1 2-2h5l2 3h7a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2Z" /><path d="M12 17v-6m-3 3 3-3 3 3" /></>,
    grid: <><rect x="3" y="3" width="7" height="7" /><rect x="14" y="3" width="7" height="7" /><rect x="3" y="14" width="7" height="7" /><rect x="14" y="14" width="7" height="7" /></>,
    list: <><path d="M8 6h13M8 12h13M8 18h13" /><path d="M3 6h.01M3 12h.01M3 18h.01" /></>,
    more: <><circle cx="5" cy="12" r="1" fill="currentColor" stroke="none" /><circle cx="12" cy="12" r="1" fill="currentColor" stroke="none" /><circle cx="19" cy="12" r="1" fill="currentColor" stroke="none" /></>,
    moon: <path d="M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9Z" />,
    mouse: <><rect x="5" y="3" width="14" height="18" rx="7" /><path d="M12 3v6" /></>,
    plus: <path d="M12 5v14M5 12h14" />,
    refresh: <><path d="M20 7v5h-5" /><path d="M4 17v-5h5" /><path d="M6.1 8a7 7 0 0 1 11.8-2L20 8M4 16l2.1 2a7 7 0 0 0 11.8-2" /></>,
    search: <><circle cx="11" cy="11" r="7" /><path d="m20 20-4-4" /></>,
    sun: <><circle cx="12" cy="12" r="4" /><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M4.93 19.07l1.41-1.41M17.66 6.34l1.41-1.41" /></>,
    trash: <><path d="M4 7h16M10 11v6M14 11v6M6 7l1 14h10l1-14M9 7V4h6v3" /></>,
    undo: <><path d="M9 7 4 12l5 5" /><path d="M20 17a8 8 0 0 0-16-5" /></>,
    user: <><circle cx="12" cy="8" r="4" /><path d="M4 21a8 8 0 0 1 16 0" /></>,
    users: <><circle cx="9" cy="8" r="3" /><path d="M3 20a6 6 0 0 1 12 0M16 5a3 3 0 0 1 0 6M17 14a5 5 0 0 1 4 5" /></>,
    upload: <><path d="M12 16V4" /><path d="m7 9 5-5 5 5" /><path d="M5 20h14" /></>,
    warning: <><path d="M10.3 3.8 2 18a2 2 0 0 0 1.7 3h16.6a2 2 0 0 0 1.7-3L13.7 3.8a2 2 0 0 0-3.4 0Z" /><path d="M12 9v4M12 17h.01" /></>,
    x: <path d="m6 6 12 12M18 6 6 18" />,
    "zoom-in": <><circle cx="11" cy="11" r="7" /><path d="M11 8v6M8 11h6m6 9-4-4" /></>,
    "zoom-out": <><circle cx="11" cy="11" r="7" /><path d="M8 11h6m6 9-4-4" /></>,
  };

  return <svg {...common}>{paths[name]}</svg>;
}
