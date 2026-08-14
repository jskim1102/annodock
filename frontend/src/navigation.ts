export function appHref(path: string): string {
  const target = new URL(path, window.location.origin);
  return `${target.pathname}${target.search}`;
}

export function navigate(path: string): void {
  window.location.assign(appHref(path));
}
