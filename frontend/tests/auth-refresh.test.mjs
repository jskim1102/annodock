import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import ts from "typescript";

function moduleUrl(source) {
  return `data:text/javascript;base64,${Buffer.from(source).toString("base64")}`;
}

function transpile(source) {
  return ts.transpileModule(source, {
    compilerOptions: {
      module: ts.ModuleKind.ESNext,
      target: ts.ScriptTarget.ES2022,
    },
  }).outputText;
}

function installBrowserGlobals(t, { fetchImpl, locks, setTimeoutImpl } = {}) {
  const fetchDescriptor = Object.getOwnPropertyDescriptor(globalThis, "fetch");
  const navigatorDescriptor = Object.getOwnPropertyDescriptor(globalThis, "navigator");
  const windowDescriptor = Object.getOwnPropertyDescriptor(globalThis, "window");
  Object.defineProperty(globalThis, "fetch", {
    configurable: true,
    writable: true,
    value: fetchImpl,
  });
  Object.defineProperty(globalThis, "navigator", {
    configurable: true,
    writable: true,
    value: locks ? { locks } : {},
  });
  Object.defineProperty(globalThis, "window", {
    configurable: true,
    writable: true,
    value: {
      location: { origin: "https://app.annodock.com" },
      setTimeout: setTimeoutImpl ?? setTimeout,
      clearTimeout,
    },
  });
  t.after(() => {
    if (fetchDescriptor) Object.defineProperty(globalThis, "fetch", fetchDescriptor);
    else delete globalThis.fetch;
    if (navigatorDescriptor) Object.defineProperty(globalThis, "navigator", navigatorDescriptor);
    else delete globalThis.navigator;
    if (windowDescriptor) Object.defineProperty(globalThis, "window", windowDescriptor);
    else delete globalThis.window;
  });
}

async function loadAuthApi(harness) {
  const source = await readFile(
    new URL("../src/api/auth.ts", import.meta.url),
    "utf8",
  );
  const harnessKey = `__annodockAuthHarness${Math.random().toString(16).slice(2)}`;
  globalThis[harnessKey] = harness;
  const storeModule = moduleUrl(`
    const harness = globalThis[${JSON.stringify(harnessKey)}];
    export const clearAuthSession = () => {
      harness.clearCalls += 1;
      harness.snapshot = { accessToken: null, refreshToken: null, user: null };
    };
    export const getAuthSnapshot = () => harness.snapshot;
    export const setAuthSession = (tokens, user) => {
      harness.snapshot = {
        accessToken: tokens.access_token,
        refreshToken: tokens.refresh_token,
        user,
      };
    };
    export const setAuthTokens = (tokens) => {
      harness.snapshot = {
        ...harness.snapshot,
        accessToken: tokens.access_token,
        refreshToken: tokens.refresh_token,
      };
      if (harness.persistTokens) {
        harness.storedSnapshot = { ...harness.snapshot };
      }
      harness.setTokenCalls += 1;
    };
    export const syncAuthSessionFromStorage = () => {
      if (harness.storedSnapshot) harness.snapshot = { ...harness.storedSnapshot };
      return harness.snapshot;
    };
  `);
  const javascript = transpile(source)
    .replace('from "../store/auth"', `from "${storeModule}"`);
  return import(`${moduleUrl(javascript)}#${Math.random()}`);
}

async function loadApiClient(harness) {
  const source = await readFile(
    new URL("../src/api/client.ts", import.meta.url),
    "utf8",
  );
  const harnessKey = `__annodockClientHarness${Math.random().toString(16).slice(2)}`;
  globalThis[harnessKey] = harness;
  const authModule = moduleUrl(`
    const harness = globalThis[${JSON.stringify(harnessKey)}];
    export const refreshAuthSession = (...args) => harness.refreshAuthSession(...args);
  `);
  const storeModule = moduleUrl(`
    const harness = globalThis[${JSON.stringify(harnessKey)}];
    export const clearAuthSession = () => { harness.clearCalls += 1; };
    export const getAuthSnapshot = () => harness.snapshot;
  `);
  const javascript = transpile(source)
    .replace('from "./auth"', `from "${authModule}"`)
    .replace('from "../store/auth"', `from "${storeModule}"`);
  return import(`${moduleUrl(javascript)}#${Math.random()}`);
}

async function loadAuthStore(harness) {
  const source = await readFile(
    new URL("../src/store/auth.ts", import.meta.url),
    "utf8",
  );
  const harnessKey = `__annodockStoreHarness${Math.random().toString(16).slice(2)}`;
  globalThis[harnessKey] = harness;
  const reactModule = moduleUrl(`
    export const useSyncExternalStore = () => {
      throw new Error("hook is not used by this test");
    };
  `);
  const storageModule = moduleUrl(`
    const harness = globalThis[${JSON.stringify(harnessKey)}];
    export const readStoredJson = () => structuredClone(harness.stored);
    export const writeStoredJson = (_key, value) => {
      harness.stored = structuredClone(value);
      return true;
    };
    export const removeStoredValue = () => { harness.stored = null; };
  `);
  const cacheModule = moduleUrl(`
    const harness = globalThis[${JSON.stringify(harnessKey)}];
    export const clearAuthenticatedResourceCache = () => {
      harness.cacheClearCalls += 1;
    };
  `);
  const javascript = transpile(source)
    .replace('from "react"', `from "${reactModule}"`)
    .replace('from "../utils/storage"', `from "${storageModule}"`)
    .replace(
      'from "../utils/authenticatedResourceCache"',
      `from "${cacheModule}"`,
    );
  return import(`${moduleUrl(javascript)}#${Math.random()}`);
}

function tokenResponse(accessToken = "access-new", refreshToken = "refresh-new") {
  return new Response(JSON.stringify({
    access_token: accessToken,
    refresh_token: refreshToken,
  }), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

test("refresh uses the shared httpOnly cookie and coalesces concurrent callers", async (t) => {
  const requests = [];
  installBrowserGlobals(t, {
    fetchImpl: async (path, init) => {
      requests.push({ path, init });
      await Promise.resolve();
      return tokenResponse();
    },
    locks: {
      request: async (_name, _options, callback) => callback(),
    },
  });
  const harness = {
    snapshot: { accessToken: "access-old", refreshToken: "refresh-old", user: null },
    storedSnapshot: null,
    clearCalls: 0,
    setTokenCalls: 0,
  };
  const auth = await loadAuthApi(harness);

  const [first, second] = await Promise.all([
    auth.refreshAuthSession("access-old"),
    auth.refreshAuthSession("access-old"),
  ]);

  assert.equal(first, "access-new");
  assert.equal(second, "access-new");
  assert.equal(requests.length, 1);
  assert.equal(requests[0].path, "/auth/token/refresh");
  assert.equal(requests[0].init.method, "POST");
  assert.equal(requests[0].init.body, undefined);
  assert.equal(harness.setTokenCalls, 1);
  assert.equal(harness.clearCalls, 0);
});

test("the browser refresh lock prevents two tabs from replaying one rotating cookie", async (t) => {
  let lockTail = Promise.resolve();
  let refreshRequests = 0;
  const locks = {
    request: (_name, _options, callback) => {
      const run = lockTail.then(callback);
      lockTail = run.catch(() => {});
      return run;
    },
  };
  installBrowserGlobals(t, {
    fetchImpl: async () => {
      refreshRequests += 1;
      await new Promise((resolve) => setTimeout(resolve, 5));
      return tokenResponse();
    },
    locks,
  });
  const oldSnapshot = {
    accessToken: "access-old",
    refreshToken: "refresh-old",
    user: { id: 129, email: null, username: null, identities: [] },
  };
  const harness = {
    snapshot: { ...oldSnapshot },
    storedSnapshot: { ...oldSnapshot },
    persistTokens: true,
    clearCalls: 0,
    setTokenCalls: 0,
  };
  const [firstTab, secondTab] = await Promise.all([
    loadAuthApi(harness),
    loadAuthApi(harness),
  ]);

  const [first, second] = await Promise.all([
    firstTab.refreshAuthSession("access-old"),
    secondTab.refreshAuthSession("access-old"),
  ]);

  assert.deepEqual([first, second], ["access-new", "access-new"]);
  assert.equal(refreshRequests, 1);
  assert.equal(harness.setTokenCalls, 1);
  assert.equal(harness.clearCalls, 0);
});

test("a refresh lock that is never granted fails within a bounded deadline", async (t) => {
  let refreshRequests = 0;
  installBrowserGlobals(t, {
    fetchImpl: async () => {
      refreshRequests += 1;
      return tokenResponse();
    },
    locks: {
      request: () => new Promise(() => {}),
    },
    setTimeoutImpl: (callback) => setTimeout(callback, 0),
  });
  const harness = {
    snapshot: { accessToken: "access-old", refreshToken: "refresh-old", user: null },
    storedSnapshot: null,
    clearCalls: 0,
    setTokenCalls: 0,
  };
  const auth = await loadAuthApi(harness);

  const outcome = await Promise.race([
    auth.refreshAuthSession("access-old").then(
      () => ({ state: "resolved" }),
      (error) => ({ state: "rejected", error }),
    ),
    new Promise((resolve) => setTimeout(
      () => resolve({ state: "still-pending" }),
      25,
    )),
  ]);

  assert.equal(outcome.state, "rejected");
  assert.match(outcome.error.message, /인증 갱신.*시간/);
  assert.equal(refreshRequests, 0);
  assert.equal(harness.clearCalls, 0);
});

test("a refresh fetch that never answers is aborted and surfaces a resumable error", async (t) => {
  let refreshSignal;
  installBrowserGlobals(t, {
    fetchImpl: async (_path, init) => {
      refreshSignal = init.signal;
      return new Promise(() => {});
    },
    locks: {
      request: async (_name, optionsOrCallback, callback) => (
        callback ?? optionsOrCallback
      )(),
    },
    setTimeoutImpl: (callback) => setTimeout(callback, 0),
  });
  const harness = {
    snapshot: { accessToken: "access-old", refreshToken: "refresh-old", user: null },
    storedSnapshot: null,
    clearCalls: 0,
    setTokenCalls: 0,
  };
  const auth = await loadAuthApi(harness);

  const outcome = await Promise.race([
    auth.refreshAuthSession("access-old").then(
      () => ({ state: "resolved" }),
      (error) => ({ state: "rejected", error }),
    ),
    new Promise((resolve) => setTimeout(
      () => resolve({ state: "still-pending" }),
      25,
    )),
  ]);

  assert.equal(outcome.state, "rejected");
  assert.match(outcome.error.message, /인증 갱신.*시간/);
  assert.equal(refreshSignal.aborted, true);
  assert.equal(harness.clearCalls, 0);
  assert.equal(harness.snapshot.refreshToken, "refresh-old");
});

test("a timed-out refresh flight is cleared so the next attempt can recover", async (t) => {
  let lockCalls = 0;
  let refreshRequests = 0;
  installBrowserGlobals(t, {
    fetchImpl: async () => {
      refreshRequests += 1;
      return tokenResponse();
    },
    locks: {
      request: (_name, _options, callback) => {
        lockCalls += 1;
        if (lockCalls === 1) return new Promise(() => {});
        return callback();
      },
    },
    setTimeoutImpl: (callback) => setTimeout(callback, 0),
  });
  const harness = {
    snapshot: { accessToken: "access-old", refreshToken: "refresh-old", user: null },
    storedSnapshot: null,
    clearCalls: 0,
    setTokenCalls: 0,
  };
  const auth = await loadAuthApi(harness);

  await assert.rejects(
    auth.refreshAuthSession("access-old"),
    (error) => error?.name === "AuthRefreshTimeoutError",
  );
  const recovered = await auth.refreshAuthSession("access-old");

  assert.equal(recovered, "access-new");
  assert.equal(lockCalls, 2);
  assert.equal(refreshRequests, 1);
  assert.equal(harness.clearCalls, 0);
});

test("four consecutive token expiries each refresh and release the flight", async (t) => {
  let refreshRequests = 0;
  installBrowserGlobals(t, {
    fetchImpl: async () => {
      refreshRequests += 1;
      return tokenResponse(
        `access-${refreshRequests}`,
        `refresh-${refreshRequests}`,
      );
    },
    locks: {
      request: async (_name, _options, callback) => callback(),
    },
  });
  const harness = {
    snapshot: { accessToken: "access-0", refreshToken: "refresh-0", user: null },
    storedSnapshot: { accessToken: "access-0", refreshToken: "refresh-0", user: null },
    persistTokens: true,
    clearCalls: 0,
    setTokenCalls: 0,
  };
  const auth = await loadAuthApi(harness);

  for (let cycle = 1; cycle <= 4; cycle += 1) {
    const staleAccessToken = harness.snapshot.accessToken;
    assert.equal(
      await auth.refreshAuthSession(staleAccessToken),
      `access-${cycle}`,
    );
  }

  assert.equal(refreshRequests, 4);
  assert.equal(harness.setTokenCalls, 4);
  assert.equal(harness.clearCalls, 0);
});

test("a transient refresh failure preserves the resumable session and surfaces the error", async (t) => {
  installBrowserGlobals(t, {
    fetchImpl: async () => new Response("temporary outage", { status: 503 }),
  });
  const harness = {
    snapshot: { accessToken: "access-old", refreshToken: "refresh-old", user: null },
    storedSnapshot: null,
    clearCalls: 0,
    setTokenCalls: 0,
  };
  const auth = await loadAuthApi(harness);

  await assert.rejects(
    auth.refreshAuthSession("access-old"),
    (error) => error?.status === 503,
  );
  assert.equal(harness.clearCalls, 0);
  assert.equal(harness.snapshot.refreshToken, "refresh-old");
});

test("an invalid refresh cookie clears the expired session", async (t) => {
  installBrowserGlobals(t, {
    fetchImpl: async () => new Response(
      JSON.stringify({ detail: "invalid refresh token" }),
      { status: 401, headers: { "Content-Type": "application/json" } },
    ),
  });
  const harness = {
    snapshot: { accessToken: "access-old", refreshToken: "refresh-old", user: null },
    storedSnapshot: null,
    clearCalls: 0,
    setTokenCalls: 0,
  };
  const auth = await loadAuthApi(harness);

  await assert.rejects(auth.refreshAuthSession("access-old"));
  assert.equal(harness.clearCalls, 1);
  assert.equal(harness.snapshot.accessToken, null);
});

test("apiFetch refreshes from the cookie even when the JavaScript refresh copy is missing", async (t) => {
  const authorization = [];
  const responses = [new Response(null, { status: 401 }), new Response(null, { status: 204 })];
  installBrowserGlobals(t, {
    fetchImpl: async (_path, init) => {
      authorization.push(new Headers(init.headers).get("Authorization"));
      return responses.shift();
    },
  });
  const harness = {
    snapshot: { accessToken: "access-old", refreshToken: null, user: null },
    clearCalls: 0,
    refreshCalls: 0,
    refreshAuthSession: async (staleAccessToken) => {
      harness.refreshCalls += 1;
      assert.equal(staleAccessToken, "access-old");
      harness.snapshot = { accessToken: "access-new", refreshToken: "refresh-new", user: null };
      return "access-new";
    },
  };
  const client = await loadApiClient(harness);

  const response = await client.apiFetch("/api/datasets/299/uploads/chunks/batch", {
    method: "POST",
    body: new FormData(),
  });

  assert.equal(response.status, 204);
  assert.equal(harness.refreshCalls, 1);
  assert.deepEqual(authorization, ["Bearer access-old", "Bearer access-new"]);
  assert.equal(harness.clearCalls, 0);
});

test("apiFetch does not hide a refresh failure behind the original 401", async (t) => {
  installBrowserGlobals(t, {
    fetchImpl: async () => new Response(null, { status: 401 }),
  });
  const refreshFailure = Object.assign(new Error("인증 서버 연결 실패"), { status: 503 });
  const harness = {
    snapshot: { accessToken: "access-old", refreshToken: "refresh-old", user: null },
    clearCalls: 0,
    refreshAuthSession: async () => {
      throw refreshFailure;
    },
  };
  const client = await loadApiClient(harness);

  await assert.rejects(
    client.apiFetch("/api/datasets/299/uploads/chunks/batch", { method: "POST" }),
    (error) => error === refreshFailure,
  );
  assert.equal(harness.clearCalls, 0);
});

test("apiFetch aborts an API request that never answers", async (t) => {
  let requestSignal;
  installBrowserGlobals(t, {
    fetchImpl: async (_path, init) => {
      requestSignal = init.signal;
      return new Promise(() => {});
    },
    setTimeoutImpl: (callback) => setTimeout(callback, 0),
  });
  const harness = {
    snapshot: { accessToken: "access-old", refreshToken: "refresh-old", user: null },
    clearCalls: 0,
    refreshCalls: 0,
    refreshAuthSession: async () => {
      harness.refreshCalls += 1;
      return "access-new";
    },
  };
  const client = await loadApiClient(harness);

  const outcome = await Promise.race([
    client.apiFetch("/api/datasets/299/uploads/chunks/batch", { method: "POST" }).then(
      () => ({ state: "resolved" }),
      (error) => ({ state: "rejected", error }),
    ),
    new Promise((resolve) => setTimeout(
      () => resolve({ state: "still-pending" }),
      25,
    )),
  ]);

  assert.equal(outcome.state, "rejected");
  assert.match(outcome.error.message, /서버 응답.*시간/);
  assert.equal(requestSignal.aborted, true);
  assert.equal(harness.refreshCalls, 0);
  assert.equal(harness.clearCalls, 0);
});

test("apiFetch preserves an explicit caller cancellation", async (t) => {
  let requestSignal;
  installBrowserGlobals(t, {
    fetchImpl: async (_path, init) => {
      requestSignal = init.signal;
      return new Promise(() => {});
    },
  });
  const harness = {
    snapshot: { accessToken: "access-old", refreshToken: "refresh-old", user: null },
    clearCalls: 0,
    refreshCalls: 0,
    refreshAuthSession: async () => {
      harness.refreshCalls += 1;
      return "access-new";
    },
  };
  const client = await loadApiClient(harness);
  const controller = new AbortController();
  const cancellation = new DOMException("사용자가 요청을 취소했습니다.", "AbortError");

  const request = client.apiFetch("/api/datasets/299/uploads/chunks/batch", {
    method: "POST",
    signal: controller.signal,
  });
  controller.abort(cancellation);

  await assert.rejects(request, (error) => error === cancellation);
  assert.equal(requestSignal.aborted, true);
  assert.equal(requestSignal.reason, cancellation);
  assert.equal(harness.refreshCalls, 0);
  assert.equal(harness.clearCalls, 0);
});

test("a storage event adopts a token pair rotated by another tab", async (t) => {
  const storageListeners = [];
  installBrowserGlobals(t, { fetchImpl: async () => new Response(null, { status: 500 }) });
  globalThis.window.addEventListener = (type, listener) => {
    if (type === "storage") storageListeners.push(listener);
  };
  const harness = {
    stored: {
      accessToken: "access-old",
      refreshToken: "refresh-old",
      user: { id: 129, email: null, username: null, identities: [] },
    },
    cacheClearCalls: 0,
  };
  const store = await loadAuthStore(harness);
  assert.equal(store.getAuthSnapshot().accessToken, "access-old");

  harness.stored = {
    ...harness.stored,
    accessToken: "access-new",
    refreshToken: "refresh-new",
  };
  assert.equal(storageListeners.length, 1);
  storageListeners[0](new Event("storage"));

  assert.equal(store.getAuthSnapshot().accessToken, "access-new");
  assert.equal(store.getAuthSnapshot().refreshToken, "refresh-new");
});

test("an interrupted upload is labeled as paused instead of keeping an active ETA", async () => {
  const source = await readFile(
    new URL("../src/pages/UploadPage.tsx", import.meta.url),
    "utf8",
  );
  const messageStart = source.indexOf(
    'const message = reason instanceof Error ? reason.message : "업로드에 실패했습니다.";',
  );
  const catchStart = source.lastIndexOf("} catch (reason: unknown) {", messageStart);
  const finallyStart = source.indexOf("} finally {", catchStart);
  const failurePath = source.slice(catchStart, finallyStart);

  assert.match(
    failurePath,
    /setLiveProgress\(\(current\) => \(\{[\s\S]*stage: "idle",[\s\S]*etaSeconds: null/,
  );
  assert.match(failurePath, /current: "업로드 일시 중지 · 이어 올리기 가능"/);
  assert.ok(
    failurePath.indexOf("setLiveProgress") < failurePath.indexOf("setError"),
    "the active progress state must stop before the failure is surfaced",
  );
});
