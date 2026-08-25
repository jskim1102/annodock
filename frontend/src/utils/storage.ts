const STORAGE_PREFIX = "annodock:";
const LEGACY_STORAGE_PREFIX = "deeplabel:";

function migrateLegacyStorage(): void {
  if (typeof window === "undefined") return;

  try {
    const storage = window.localStorage;
    const legacyKeys: string[] = [];
    for (let index = 0; index < storage.length; index += 1) {
      const key = storage.key(index);
      if (key?.startsWith(LEGACY_STORAGE_PREFIX)) legacyKeys.push(key);
    }

    legacyKeys.forEach((legacyKey) => {
      const suffix = legacyKey.slice(LEGACY_STORAGE_PREFIX.length);
      if (!suffix) return;
      const canonicalKey = `${STORAGE_PREFIX}${suffix}`;

      try {
        const canonicalValue = storage.getItem(canonicalKey);
        if (canonicalValue === null) {
          const legacyValue = storage.getItem(legacyKey);
          if (legacyValue === null) return;
          storage.setItem(canonicalKey, legacyValue);
          if (storage.getItem(canonicalKey) !== legacyValue) return;
        }
        storage.removeItem(legacyKey);
      } catch {
        // Keep the legacy value so a later app boot can retry safely.
      }
    });
  } catch {
    // Storage may be unavailable (for example, in a restricted browser mode).
  }
}

migrateLegacyStorage();

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
