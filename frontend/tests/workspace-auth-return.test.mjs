import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import ts from "typescript";

const moduleUrl = (source) => `data:text/javascript;base64,${Buffer.from(source).toString("base64")}`;
const settle = () => new Promise((resolve) => setImmediate(resolve));
const response = (body, status = 200) => new Response(JSON.stringify(body), {
  status, headers: { "Content-Type": "application/json" },
});
const invite = "/invites/team-token?ws=22&from=email#accept";

function find(element, predicate) {
  if (!element || typeof element !== "object") return null;
  if (predicate(element)) return element;
  return [element.props?.children].flat(Infinity)
    .map((child) => find(child, predicate)).find(Boolean) ?? null;
}

// Render the real page handlers and run the real auth/navigation modules;
// only React/browser/admin-probe boundaries and the HTTP transport are simulated.
async function harness(t, path, handle) {
  const originals = Object.fromEntries(["window", "fetch", "FormData"].map((key) => [key, globalThis[key]]));
  const h = { destinations: [], requests: [], session: new Map(), established: [], storageReads: [] };
  h.visit = (destination) => {
    const url = new URL(destination, "https://annodock.test");
    globalThis.window.location = {
      origin: url.origin, pathname: url.pathname, search: url.search, hash: url.hash,
      assign: (next) => h.destinations.push(next),
    };
  };
  globalThis.window = {
    sessionStorage: {
      getItem: (key) => {
        const value = h.session.get(key) ?? null;
        h.storageReads.push({ key, value });
        return value;
      },
      setItem: (key, value) => h.session.set(key, String(value)),
      removeItem: (key) => h.session.delete(key),
    },
    setTimeout, clearTimeout,
  };
  h.visit(path);
  globalThis.FormData = class {
    constructor(values) { this.values = values; }
    get(name) { return this.values[name] ?? null; }
  };
  globalThis.fetch = async (url, init) => {
    h.requests.push({ url, init });
    if (handle) {
      const handled = handle(url, init);
      if (handled !== undefined) return handled;
    }
    if (url === "/auth/signup") return response({ id: 5 }, 201);
    if (url === "/auth/me") return response({ id: 5, email: "member@example.test" });
    if (url === "/auth/login" || url === "/auth/oauth/exchange") {
      return response({ access_token: "access", refresh_token: "refresh" });
    }
    throw new Error(`unexpected auth request ${url}`);
  };
  let slots = [], cursor = 0, effects = [], cleanups = [], mounted = false;
  h.react = {
    useState(initial) {
      const index = cursor++;
      if (!mounted) slots[index] = typeof initial === "function" ? initial() : initial;
      return [slots[index], (next) => { slots[index] = typeof next === "function" ? next(slots[index]) : next; }];
    },
    useEffect(effect) { if (!mounted) effects.push(effect); },
  };
  const key = `__authReturn${Math.random().toString(16).slice(2)}`;
  globalThis[key] = h;
  h.unmount = () => { cleanups.splice(0).forEach((cleanup) => cleanup?.()); };
  t.after(() => {
    h.unmount();
    Object.assign(globalThis, originals);
    delete globalThis[key];
  });
  const boundaries = moduleUrl(`
    const h = globalThis[${JSON.stringify(key)}];
    export const { useState, useEffect } = h.react;
    export const jsx = (type, props) => ({ type, props }), jsxs = jsx;
    export const useTheme = () => ({ theme: 'light' }), Brand = () => null;
    export const adminHomePath = async () => "/projects", resetAdminProbe = () => {};
    export const clearAuthSession = () => {}, getAuthSnapshot = () => ({}),
      setAuthTokens = () => {}, syncAuthSessionFromStorage = () => ({}),
      setAuthSession = (...args) => h.established.push(args);
  `);
  const compile = async (sourcePath, dependencies) => moduleUrl(ts.transpileModule(
    await readFile(new URL(sourcePath, import.meta.url), "utf8"),
    { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX } },
  ).outputText.replace(/from "([^"]+)"/g, (_match, name) => {
    assert.ok(dependencies[name], `unmapped dependency: ${name}`);
    return `from "${dependencies[name]}"`;
  }));
  const authModule = await compile("../src/api/auth.ts", { "../store/auth": boundaries });
  const navigationModule = await compile("../src/navigation.ts", {});
  const pages = await import(await compile("../src/pages/AuthPages.tsx", {
    "react": boundaries, "react/jsx-runtime": boundaries, "../api/auth": authModule,
    "../components/AppShell": boundaries, "../navigation": navigationModule, "../theme": boundaries,
    "../utils/adminAccess": boundaries,
  }));
  let page;
  h.render = () => { cursor = 0; const tree = page(); mounted = true; return tree; };
  h.mount = (name, { strictMode = false } = {}) => {
    h.unmount(); slots = []; effects = []; mounted = false; page = pages[name];
    if (strictMode) {
      // React 18 development renders a new component twice before commit,
      // discarding the first render's hooks, then replays its committed effects.
      h.render(); slots = []; effects = []; mounted = false;
    }
    const tree = h.render();
    const committedEffects = effects.splice(0);
    cleanups.push(...committedEffects.map((effect) => effect()));
    if (strictMode) {
      h.unmount();
      cleanups.push(...committedEffects.map((effect) => effect()));
    }
    return tree;
  };
  h.submit = async (tree, values) => {
    find(tree, (element) => element.type === "form").props.onSubmit({
      preventDefault() {}, currentTarget: values,
    });
    await settle();
  };
  h.oauth = (tree) => find(tree, (element) => element.type === "button"
    && element.props.className?.includes("oauth-button") && !element.props.disabled).props.onClick();
  return h;
}

const authLink = (tree, pathname) => find(tree, (element) => element.type === "a"
  && new URL(element.props.href, "https://annodock.test").pathname === pathname)?.props.href;
const nextOf = (path) => new URL(path, "https://annodock.test").searchParams.get("next");

test("an invitation survives login, signup, registered login and the real login submit", async (t) => {
  const h = await harness(t, `/login?next=${encodeURIComponent(invite)}`);
  const signupLink = authLink(h.mount("LoginPage"), "/signup");
  assert.equal(nextOf(signupLink), invite);
  h.visit(signupLink);
  const signup = h.mount("SignupPage");
  assert.equal(nextOf(authLink(signup, "/login")), invite);
  await h.submit(signup, { username: "member", email: "member@example.test", password: "pass1234", confirmation: "pass1234" });
  const registered = h.destinations.at(-1);
  assert.equal(new URL(registered, "https://annodock.test").searchParams.get("registered"), "1");
  assert.equal(nextOf(registered), invite);
  h.visit(registered);
  await h.submit(h.mount("LoginPage"), { identifier: "member", password: "pass1234" });
  assert.equal(h.destinations.at(-1), invite);
  assert.equal(h.established.length, 1);
  assert.ok(h.requests.every(({ init }) => !new Headers(init.headers).has("X-Workspace-Id")));
});

test("OAuth started from an invitation returns there after a callback with only a code", async (t) => {
  const h = await harness(t, `/login?next=${encodeURIComponent(invite)}`);
  h.oauth(h.mount("LoginPage"));
  assert.equal(h.destinations.at(-1), "/auth/oauth/naver/authorize");
  h.visit("/auth/callback?code=single-use-code");
  h.mount("OAuthCallbackPage");
  await settle();
  assert.equal(h.destinations.at(-1), invite);
  assert.equal(h.session.size, 0, "a successful callback consumes its tab-local return path");
  assert.ok(h.requests.every(({ init }) => !new Headers(init.headers).has("X-Workspace-Id")));
});

test("an explicit callback next takes precedence over an earlier OAuth return path", async (t) => {
  const h = await harness(t, "/login?next=%2Fprojects");
  h.oauth(h.mount("LoginPage"));
  h.visit(`/auth/callback?code=callback-next&next=${encodeURIComponent(invite)}`);
  h.mount("OAuthCallbackPage");
  await settle();
  assert.equal(h.destinations.at(-1), invite);
  assert.equal(h.session.size, 0);
});

test("a failed OAuth exchange preserves the invitation in the login return link", async (t) => {
  const h = await harness(t, `/login?next=${encodeURIComponent(invite)}`,
    (url) => url === "/auth/oauth/exchange" ? response({ detail: "expired code" }, 400) : undefined);
  h.oauth(h.mount("LoginPage"));
  h.visit("/auth/callback?code=expired");
  h.mount("OAuthCallbackPage");
  await settle();
  assert.equal(nextOf(authLink(h.render(), "/login")), invite);
  assert.equal(h.destinations.length, 1, "failed exchange must not navigate");
  assert.equal(h.session.has("annodock:oauth-return"), false, "failed exchange must not retain the invitation token in session storage");
});

test("OAuth callback clears its stored return path before the exchange resolves", async (t) => {
  let finish;
  const pending = new Promise((resolve) => { finish = resolve; });
  const h = await harness(t, "/auth/callback?code=still-exchanging",
    (url) => url === "/auth/oauth/exchange" ? pending : undefined);
  h.session.set("annodock:oauth-return", invite);
  h.mount("OAuthCallbackPage");
  try {
    assert.equal(h.session.has("annodock:oauth-return"), false);
    assert.deepEqual(h.destinations, []);
  } finally {
    finish(response({ access_token: "access", refresh_token: "refresh" }));
    await settle();
  }
  assert.equal(h.destinations.at(-1), invite, "return path survives in component state after the key is cleared");
});

for (const query of ["", "?error=access_denied"]) {
  test(`OAuth callback without a code clears the return key${query ? " after provider cancellation" : ""}`, async (t) => {
    const h = await harness(t, `/auth/callback${query}`);
    h.session.set("annodock:oauth-return", invite);
    h.mount("OAuthCallbackPage");
    assert.equal(h.session.has("annodock:oauth-return"), false);
    assert.equal(nextOf(authLink(h.render(), "/login")), invite);
    assert.deepEqual(h.requests, [], "missing codes never start an OAuth exchange");
    assert.deepEqual(h.destinations, []);
  });
}

test("re-entering login removes stale OAuth state but a new provider click saves the current next", async (t) => {
  const h = await harness(t, `/login?next=${encodeURIComponent(invite)}`);
  h.oauth(h.mount("LoginPage"));
  assert.equal(h.session.get("annodock:oauth-return"), invite);
  h.visit("/login?next=%2Fmembers");
  const login = h.mount("LoginPage", { strictMode: true });
  assert.equal(h.session.has("annodock:oauth-return"), false);
  assert.equal(nextOf(authLink(login, "/signup")), "/members");
  h.oauth(login);
  assert.equal(h.session.get("annodock:oauth-return"), "/members");
});

test("StrictMode callback render and effect replay preserve the invitation and exchange only once", async (t) => {
  let finish;
  const pending = new Promise((resolve) => { finish = resolve; });
  const h = await harness(t, "/auth/callback?code=strict-mode-return",
    (url) => url === "/auth/oauth/exchange" ? pending : undefined);
  h.session.set("annodock:oauth-return", invite);
  h.mount("OAuthCallbackPage", { strictMode: true });
  try {
    assert.deepEqual(h.storageReads.filter(({ key }) => key === "annodock:oauth-return").map(({ value }) => value), [invite, invite]);
    assert.equal(h.session.has("annodock:oauth-return"), false);
  } finally {
    finish(response({ access_token: "access", refresh_token: "refresh" }));
    await settle();
  }
  assert.equal(h.requests.filter(({ url }) => url === "/auth/oauth/exchange").length, 1);
  assert.deepEqual(h.destinations, [invite], "only the active replay may navigate");
});

for (const unsafe of ["https://outside.test/steal", "//outside.test/steal", "/\\outside.test/steal"]) {
  test(`login, signup and OAuth reject an external return path: ${unsafe}`, async (t) => {
    const h = await harness(t, `/login?next=${encodeURIComponent(unsafe)}`);
    const login = h.mount("LoginPage");
    const signupLink = authLink(login, "/signup");
    assert.equal(nextOf(signupLink), "/projects");
    await h.submit(login, { identifier: "member", password: "pass1234" });
    assert.equal(h.destinations.at(-1), "/projects");
    h.visit(`/auth/callback?code=untrusted-next&next=${encodeURIComponent(unsafe)}`);
    h.mount("OAuthCallbackPage");
    await settle();
    assert.equal(h.destinations.at(-1), "/projects");
  });
}

test("OAuth remains usable when session storage is unavailable", async (t) => {
  const h = await harness(t, `/login?next=${encodeURIComponent(invite)}`);
  Object.defineProperty(window, "sessionStorage", { get() { throw new Error("storage disabled"); } });
  assert.doesNotThrow(() => h.oauth(h.mount("LoginPage")));
  assert.equal(h.destinations.at(-1), "/auth/oauth/naver/authorize");
  h.visit("/auth/callback?code=storage-disabled");
  h.mount("OAuthCallbackPage");
  await settle();
  assert.equal(h.destinations.at(-1), "/projects");
});

test("an OAuth response arriving after unmount cannot navigate", async (t) => {
  let finish;
  const pending = new Promise((resolve) => { finish = resolve; });
  const h = await harness(t, `/auth/callback?code=late&next=${encodeURIComponent(invite)}`,
    (url) => url === "/auth/oauth/exchange" ? pending : undefined);
  h.mount("OAuthCallbackPage");
  h.unmount();
  finish(response({ access_token: "access", refresh_token: "refresh" }));
  await settle();
  assert.deepEqual(h.destinations, []);
});

test("ordinary login and signup links retain their existing default paths", async (t) => {
  const h = await harness(t, "/login");
  assert.equal(authLink(h.mount("LoginPage"), "/signup"), "/signup");
  h.visit("/signup");
  const signup = h.mount("SignupPage");
  assert.equal(authLink(signup, "/login"), "/login");
  await h.submit(signup, { username: "member", email: "member@example.test", password: "pass1234", confirmation: "pass1234" });
  assert.equal(h.destinations.at(-1), "/login?registered=1");
});
