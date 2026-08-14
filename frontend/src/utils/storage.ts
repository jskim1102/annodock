const STORAGE_PREFIX = "deeplabel:";

function namespacedKey(suffix: string) {
  if (!suffix || suffix.startsWith(STORAGE_PREFIX)) {
    throw new Error("storage keys must be non-empty, unprefixed suffixes");
  }
  return `${STORAGE_PREFIX}${suffix}`;
}

export function readStoredString(suffix: string): string | null {
  try {
    return window.localStorage.getItem(namespacedKey(suffix));
  } catch {
    return null;
  }
}

export function writeStoredString(suffix: string, value: string): boolean {
  try {
    window.localStorage.setItem(namespacedKey(suffix), value);
    return true;
  } catch {
    return false;
  }
}

export function removeStoredValue(suffix: string): void {
  try {
    window.localStorage.removeItem(namespacedKey(suffix));
  } catch {
    // Storage is an optional enhancement; callers can continue without it.
  }
}

export function readStoredJson(suffix: string): unknown | null {
  const stored = readStoredString(suffix);
  if (stored === null) return null;
  try {
    return JSON.parse(stored) as unknown;
  } catch {
    return null;
  }
}

export function writeStoredJson(suffix: string, value: unknown): boolean {
  try {
    return writeStoredString(suffix, JSON.stringify(value));
  } catch {
    return false;
  }
}
