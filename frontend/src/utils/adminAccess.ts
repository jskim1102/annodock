import { ApiError, getAdminOverview } from "../api/client";
import type { AdminOverview } from "../api/client";

// The server response is the only source of truth for admin access; nothing
// is persisted in browser storage, so a stale grant can never outlive its
// revocation beyond the current page load.
let probe: Promise<AdminOverview | null> | null = null;

export function probeAdminOverview(): Promise<AdminOverview | null> {
  if (!probe) {
    probe = getAdminOverview().catch((error: unknown) => {
      probe = null;
      if (error instanceof ApiError && error.status === 404) {
        // Cache the negative answer for this page load.
        probe = Promise.resolve(null);
        return null;
      }
      throw error;
    });
  }
  return probe;
}

export function resetAdminProbe(): void {
  probe = null;
}
