export const AUTHENTICATED_RESOURCE_CACHE_LIMIT = 40;

export type AuthenticatedResourceLoader = (
  resourcePath: string,
  signal: AbortSignal,
) => Promise<Blob>;

export interface AuthenticatedResourceLease {
  url: string;
  release: () => void;
}

interface CacheEntry {
  url: string;
  retainCount: number;
}

interface InFlightResource {
  controller: AbortController;
  generation: number;
  retainCount: number;
  promise: Promise<CacheEntry>;
}

const cache = new Map<string, CacheEntry>();
const inFlight = new Map<string, InFlightResource>();
let generation = 0;

function abortError(): DOMException {
  return new DOMException("인증 리소스 요청이 취소되었습니다.", "AbortError");
}

function touch(resourcePath: string, entry: CacheEntry): void {
  cache.delete(resourcePath);
  cache.set(resourcePath, entry);
}

function trimCache(): void {
  while (cache.size > AUTHENTICATED_RESOURCE_CACHE_LIMIT) {
    const victim = [...cache].find(([, entry]) => entry.retainCount === 0);
    if (!victim) return;
    const [resourcePath, entry] = victim;
    cache.delete(resourcePath);
    URL.revokeObjectURL(entry.url);
  }
}

function startFlight(
  resourcePath: string,
  loader: AuthenticatedResourceLoader,
): InFlightResource {
  const controller = new AbortController();
  const flightGeneration = generation;
  const flight: InFlightResource = {
    controller,
    generation: flightGeneration,
    retainCount: 0,
    promise: loader(resourcePath, controller.signal)
      .then((blob) => {
        if (generation !== flightGeneration || inFlight.get(resourcePath) !== flight) {
          throw abortError();
        }
        const entry: CacheEntry = {
          url: URL.createObjectURL(blob),
          retainCount: flight.retainCount,
        };
        cache.set(resourcePath, entry);
        trimCache();
        return entry;
      })
      .finally(() => {
        if (inFlight.get(resourcePath) === flight) inFlight.delete(resourcePath);
      }),
  };
  inFlight.set(resourcePath, flight);
  return flight;
}

function resourceFlight(
  resourcePath: string,
  loader: AuthenticatedResourceLoader,
): InFlightResource {
  return inFlight.get(resourcePath) ?? startFlight(resourcePath, loader);
}

export function peekAuthenticatedResource(resourcePath: string): string | null {
  const entry = cache.get(resourcePath);
  if (!entry) return null;
  touch(resourcePath, entry);
  return entry.url;
}

export async function acquireAuthenticatedResource(
  resourcePath: string,
  loader: AuthenticatedResourceLoader,
): Promise<AuthenticatedResourceLease> {
  let entry = cache.get(resourcePath);
  if (entry) {
    entry.retainCount += 1;
    touch(resourcePath, entry);
  } else {
    const flight = resourceFlight(resourcePath, loader);
    flight.retainCount += 1;
    entry = await flight.promise;
  }

  let released = false;
  return {
    url: entry.url,
    release: () => {
      if (released) return;
      released = true;
      const current = cache.get(resourcePath);
      if (current !== entry) return;
      current.retainCount = Math.max(0, current.retainCount - 1);
      trimCache();
    },
  };
}

export async function prefetchAuthenticatedResource(
  resourcePath: string,
  loader: AuthenticatedResourceLoader,
): Promise<void> {
  const entry = cache.get(resourcePath);
  if (entry) {
    touch(resourcePath, entry);
    return;
  }
  await resourceFlight(resourcePath, loader).promise;
}

export function clearAuthenticatedResourceCache(): void {
  generation += 1;
  inFlight.forEach((flight) => flight.controller.abort());
  inFlight.clear();
  cache.forEach((entry) => URL.revokeObjectURL(entry.url));
  cache.clear();
}
