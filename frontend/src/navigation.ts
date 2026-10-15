export function appHref(path: string): string {
  const target = new URL(path, window.location.origin);
  const authPath = /^\/(?:login|signup|password-reset|reset|auth)(?:\/|$)/.test(target.pathname);
  const workspace = new URLSearchParams(window.location.search).get("ws");
  if (!authPath && workspace !== null && !target.searchParams.has("ws")) {
    target.searchParams.set("ws", workspace);
  }
  return `${target.pathname}${target.search}${target.hash}`;
}

const NAVIGATION_EVENT = "annodock:navigate";

export function getLocationSnapshot(): string {
  return `${window.location.pathname}${window.location.search}${window.location.hash}`;
}

export function subscribeNavigation(listener: () => void): () => void {
  window.addEventListener("popstate", listener);
  window.addEventListener(NAVIGATION_EVENT, listener);
  return () => {
    window.removeEventListener("popstate", listener);
    window.removeEventListener(NAVIGATION_EVENT, listener);
  };
}

export function navigateWithinApp(path: string): void {
  window.history.pushState(null, "", appHref(path));
  window.dispatchEvent(new Event(NAVIGATION_EVENT));
}

export function navigate(path: string): void {
  window.location.assign(appHref(path));
}
