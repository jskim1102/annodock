import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import ts from "typescript";

const moduleUrl = (source) => `data:text/javascript;base64,${Buffer.from(source).toString("base64")}`;
const adminOverview = { user_count: 2, storage_total_bytes: 0 };
const anonymous = { accessToken: null, refreshToken: null, user: null };
const account = (id) => ({
  accessToken: `access-${id}`,
  refreshToken: `refresh-${id}`,
  user: { id, email: null, username: `user-${id}`, identities: [] },
});

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

// Exercise the production TSX and effects, including dependency cleanup and
// rerenders, without adding a DOM or React testing dependency to the project.
async function harness(t, options = {}) {
  const previousWindow = globalThis.window;
  const previousDocument = globalThis.document;
  const previousFormData = globalThis.FormData;
  const location = new URL(options.path ?? "/", "https://annodock.example");
  const destinations = [];
  globalThis.window = {
    location: {
      origin: location.origin,
      pathname: location.pathname,
      search: location.search,
      hash: location.hash,
      replace: (path) => destinations.push(path),
      assign: (path) => destinations.push(path),
    },
    addEventListener() {}, removeEventListener() {}, setTimeout, clearTimeout,
  };
  globalThis.document = { addEventListener() {}, removeEventListener() {} };
  globalThis.FormData = class {
    constructor(values) { this.values = values; }
    get(name) { return this.values[name] ?? null; }
  };

  const instances = new Map();
  let current;
  let effects = [];
  let dirty = false;
  let disposed = false;
  let updatesAfterUnmount = 0;
  let tree;
  const equalDeps = (left, right) => left !== undefined && right !== undefined
    && left.length === right.length && left.every((value, index) => Object.is(value, right[index]));
  const slot = (initialize) => {
    const index = current.cursor++;
    if (!(index in current.slots)) current.slots[index] = initialize();
    return current.slots[index];
  };
  const hooks = {
    useState(initial) {
      const instance = current;
      const state = slot(() => ({ value: typeof initial === "function" ? initial() : initial }));
      return [state.value, (update) => {
        if (instance.unmounted) { updatesAfterUnmount += 1; return; }
        const value = typeof update === "function" ? update(state.value) : update;
        if (!Object.is(value, state.value)) { state.value = value; dirty = true; }
      }];
    },
    useSyncExternalStore: (_subscribe, getSnapshot) => getSnapshot(),
    useRef: (value) => slot(() => ({ current: value })),
    useMemo(callback, dependencies) {
      const state = slot(() => ({}));
      if (!equalDeps(state.dependencies, dependencies)) {
        state.value = callback();
        state.dependencies = dependencies;
      }
      return state.value;
    },
    useCallback: (callback, dependencies) => hooks.useMemo(() => callback, dependencies),
    useEffect(callback, dependencies) {
      const state = slot(() => ({}));
      if (equalDeps(state.dependencies, dependencies)) return;
      state.dependencies = dependencies;
      effects.push(() => { state.cleanup?.(); state.cleanup = callback(); });
    },
  };
  const cleanup = (instance) => {
    instance.unmounted = true;
    instance.slots.forEach((state) => state.cleanup?.());
  };
  function expand(element, path, seen) {
    if (Array.isArray(element)) return element.map((child, index) => expand(child, `${path}.${index}`, seen));
    if (!element || typeof element !== "object") return element;
    if (typeof element.type === "function") {
      let instance = instances.get(path);
      if (instance?.type !== element.type) {
        if (instance) cleanup(instance);
        instance = { type: element.type, slots: [], cursor: 0, unmounted: false };
        instances.set(path, instance);
      }
      seen.add(path);
      instance.cursor = 0;
      const previous = current;
      current = instance;
      const result = element.type(element.props);
      current = previous;
      return expand(result, `${path}.rendered`, seen);
    }
    return { ...element, props: { ...element.props, children: expand(element.props?.children, `${path}.children`, seen) } };
  }

  const key = `__adminNavigation${Math.random().toString(16).slice(2)}`;
  const state = {
    hooks,
    session: options.session ?? account(1),
    authCalls: [],
    flushCalls: 0,
    exchange: options.exchange ?? (() => undefined),
    probeCalls: [],
    probe: options.probe ?? (() => {
      if (options.failure === "network") throw new TypeError("network unavailable");
      if (typeof options.failure === "number") throw new state.ApiError(options.failure, "probe failed");
      return options.overview ?? null;
    }),
  };
  state.annotation = {
    boxes: [], saveState: "saved", saveError: null, undoDepth: 0,
    reset() {}, applyChange() {}, undo() {}, retry() {}, resolveConflict() {}, setCatalogVersion() {},
    flush: () => { state.flushCalls += 1; return Promise.resolve(options.flush?.() ?? true); },
  };
  globalThis[key] = state;
  const dependency = moduleUrl(`
    const h = globalThis[${JSON.stringify(key)}];
    export const { useState, useRef, useMemo, useCallback, useEffect, useSyncExternalStore } = h.hooks;
    export const jsx = (type, props) => ({ type, props }), jsxs = jsx;
    export const Fragment = "fragment";
    export const useAuthSession = () => h.session;
    export const getAuthSnapshot = () => h.session;
    export const setAuthSession = (tokens, user) => {
      h.session = { accessToken: tokens.access_token, refreshToken: tokens.refresh_token, user };
    };
    export const getCurrentUser = async () => h.session.user;
    export const refreshAuthSession = async () => h.session;
    export const getWorkspaceSnapshot = () => ({
      userId: h.session.user?.id ?? null, status: "ready", notFound: false,
      active: { id: 1, kind: "personal", my_role: "owner" }, message: null,
    });
    export const useWorkspace = getWorkspaceSnapshot;
    export const initializeWorkspaces = async () => getWorkspaceSnapshot();
    export const resetWorkspaces = () => {}, dismissWorkspaceMessage = () => {};
    export const getLocationSnapshot = () => window.location.pathname + window.location.search + window.location.hash;
    export const subscribeNavigation = () => () => {};
    export const appHref = (path) => path;
    export const navigate = (path) => window.location.assign(path);
    export const useTheme = () => ({ theme: "light" });
    export class AuthApiError extends Error {
      constructor(status, message) { super(message); this.status = status; }
    }
    export const login = async (identifier, password) => {
      h.authCalls.push(["login", identifier, password]);
      return { access_token: "login-access", refresh_token: "login-refresh" };
    };
    export const establishAuthSession = async (tokens) => {
      h.authCalls.push(["establish", tokens]);
      h.session = { ...h.session, accessToken: tokens.access_token, refreshToken: tokens.refresh_token };
    };
    export const exchangeOAuthCodeOnce = async (code) => {
      h.authCalls.push(["exchange", code]);
      await h.exchange();
    };
    export const oauthAuthorizeUrl = () => "/oauth", requestPasswordReset = async () => {},
      resetPassword = async () => {}, signup = async () => {};
    export class ApiError extends Error {
      constructor(status, message) { super(message); this.status = status; }
    }
    h.ApiError = ApiError;
    export const getAdminOverview = () => {
      const session = h.session;
      h.probeCalls.push(session);
      return Promise.resolve().then(() => h.probe(session));
    };
    export const getStorageQuota = async () => ({ used_bytes: 0, limit_bytes: 1024 });
    export const resetStorageQuotaCache = () => {};
    export const STORAGE_QUOTA_INVALIDATED_EVENT = "storage-quota-invalidated";
    export const logoutCurrentSession = async () => {};
    export const getAccountPresentation = () => ({ label: "test user", initials: "TU" });
    export const formatBytes = (value) => String(value);
    export const Icon = () => null, ThemeToggle = () => null, WorkspaceSwitcher = () => null;
    export const useAnnotationEdit = () => h.annotation;
    export const useAuthenticatedObjectUrl = () => ({ url: null });
    export const getDataset = async () => ({ id: 1, name: "empty", image_count: 0, splits: [], project_id: 1, workspace_id: 1, my_role: "owner" });
    export const getWorkspaceMembers = async () => ({ items: [] });
    export const assignImage = async () => {}, claimDatasetImage = async () => {},
      completeImage = async () => {}, uncompleteImage = async () => {}, getImage = async () => {};
    export const WorkspaceDialog = () => null;
    export const getDatasetClasses = async () => ({ classes: [] });
    export const getProject = async () => ({ name: "project", classes: [] });
    export const getDatasetImages = async () => ({ items: [], total: 0 });
    export const getImageAnnotations = async () => ({ boxes: [] });
    export const imageResourceUrl = () => "", renameDatasetClass = async () => {},
      updateProjectClass = async () => {}, saveImageAnnotations = async () => {},
      prefetchAuthenticatedResource = async () => {}, CLASS_COLOR_PRESETS = [];
    export const BboxCanvas = () => null, ThumbStrip = () => null;
    export const ThemeProvider = ({ children }) => children;
    const page = (name) => () => jsx("page", { name });
    export const LoginPage = page("LoginPage"), OAuthCallbackPage = page("OAuthCallbackPage"),
      PasswordResetPage = page("PasswordResetPage"), SignupPage = page("SignupPage"),
      ProjectsPage = page("ProjectsPage"), RunDetailPage = page("RunDetailPage"),
      RunsPage = page("RunsPage"), TrainPage = page("TrainPage"), UploadPage = page("UploadPage"),
      ViewerPage = page("ViewerPage"), AdminPage = page("AdminPage"),
      InviteAcceptPage = page("InviteAcceptPage"), MembersPage = page("MembersPage");
  `);
  const compile = async (path, adminModule = dependency, appShellModule = dependency) => {
    const source = await readFile(new URL(path, import.meta.url), "utf8");
    return moduleUrl(ts.transpileModule(source, {
      compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX },
    }).outputText.replace(/from "([^"]+)"/g, (_match, name) => (
      `from "${name.endsWith("/adminAccess") ? adminModule
        : name.endsWith("/AppShell") ? appShellModule : dependency}"`
    )));
  };
  const adminModule = await compile("../src/utils/adminAccess.ts");
  const appShellModule = await compile("../src/components/AppShell.tsx", adminModule);
  const { AppShell } = await import(appShellModule);
  const { App } = await import(await compile("../src/App.tsx", adminModule, appShellModule));
  let component = options.shell ? AppShell : App;
  let props = options.shell ? { active: "projects", children: "content" } : {};
  if (options.screen === "login" || options.screen === "oauth") {
    const auth = await import(await compile("../src/pages/AuthPages.tsx", adminModule, appShellModule));
    component = options.screen === "login" ? auth.LoginPage : auth.OAuthCallbackPage;
  } else if (options.screen === "viewer") {
    component = (await import(await compile("../src/pages/Viewer.tsx", adminModule, appShellModule))).ViewerPage;
    props = { datasetId: 1 };
  }
  const render = () => {
    if (disposed) return tree;
    let passes = 0;
    do {
      assert.ok(passes++ < 20, "component effects must settle");
      dirty = false;
      effects = [];
      const seen = new Set();
      tree = expand({ type: component, props }, "root", seen);
      for (const [path, instance] of instances) {
        if (!seen.has(path)) { cleanup(instance); instances.delete(path); }
      }
      effects.forEach((effect) => effect());
    } while (dirty);
    return tree;
  };
  const unmount = () => {
    if (disposed) return;
    disposed = true;
    instances.forEach(cleanup);
  };
  t.after(() => {
    unmount();
    globalThis.window = previousWindow;
    globalThis.document = previousDocument;
    globalThis.FormData = previousFormData;
    delete globalThis[key];
  });
  render();
  return {
    destinations, probeCalls: state.probeCalls, authCalls: state.authCalls, render, unmount,
    get flushCalls() { return state.flushCalls; },
    get tree() { return tree; },
    get updatesAfterUnmount() { return updatesAfterUnmount; },
    setSession(session) { state.session = session; render(); },
    async settle() { await new Promise((resolve) => setImmediate(resolve)); render(); },
  };
}

function elements(tree, predicate) {
  if (Array.isArray(tree)) return tree.flatMap((child) => elements(child, predicate));
  if (!tree || typeof tree !== "object") return [];
  return [...(predicate(tree) ? [tree] : []), ...elements(tree.props?.children, predicate)];
}
const sidebarLinks = (h) => elements(h.tree, (node) => node.props?.className === "sidebar-link").map((node) => node.props.href);
const brandHref = (h) => elements(h.tree, (node) => node.props?.className === "sidebar-brand")[0]?.props.href;
const pageName = (h) => elements(h.tree, (node) => node.type === "page")[0]?.props.name;

test("the sidebar keeps existing links while pending, then shows only admin and its home link", async (t) => {
  const verdict = deferred();
  const h = await harness(t, { shell: true, probe: () => verdict.promise });
  assert.deepEqual(sidebarLinks(h), ["/projects", "/runs", "/members"]);
  assert.equal(brandHref(h), "/projects");
  verdict.resolve(adminOverview);
  await h.settle();
  assert.deepEqual(sidebarLinks(h), ["/admin"]);
  assert.equal(brandHref(h), "/admin");
});

test("an authenticated admin root redirects only after the server verdict, without hiding the pending page", async (t) => {
  const verdict = deferred();
  const h = await harness(t, { probe: () => verdict.promise });
  await h.settle(); // Workspace bootstrap completes independently of the pending admin probe.
  assert.equal(pageName(h), "ProjectsPage");
  assert.deepEqual(h.destinations, []);
  verdict.resolve(adminOverview);
  await h.settle();
  assert.deepEqual(h.destinations, ["/admin"]);
});

for (const [name, options] of [
  ["a non-admin null verdict", { overview: null }],
  ["a non-admin 404", { failure: 404 }],
  ["a network failure", { failure: "network" }],
  ["a server 503", { failure: 503 }],
]) {
  test(`${name} preserves the normal sidebar and brand`, async (t) => {
    const h = await harness(t, { shell: true, ...options });
    await h.settle();
    assert.deepEqual(sidebarLinks(h), ["/projects", "/runs", "/members"]);
    assert.equal(brandHref(h), "/projects");
  });
  test(`${name} keeps the authenticated root on projects`, async (t) => {
    const h = await harness(t, options);
    await h.settle();
    assert.equal(pageName(h), "ProjectsPage");
    assert.deepEqual(h.destinations, []);
  });
}

test("the unauthenticated root stays on login without an admin probe", async (t) => {
  const h = await harness(t, { session: anonymous, overview: adminOverview });
  await h.settle();
  assert.equal(pageName(h), "LoginPage");
  assert.deepEqual(h.destinations, []);
  assert.equal(h.probeCalls.length, 0);
});

test("an unauthenticated shell never adopts an admin menu", async (t) => {
  const h = await harness(t, { shell: true, session: anonymous, overview: adminOverview });
  await h.settle();
  assert.deepEqual(sidebarLinks(h), ["/projects", "/runs", "/members"]);
  assert.equal(brandHref(h), "/projects");
});

for (const [path, expectedPage] of [["/projects", "ProjectsPage"], ["/runs", "RunsPage"]]) {
  test(`a direct admin visit to ${path} stays on its requested route`, async (t) => {
    const h = await harness(t, { path, overview: adminOverview });
    await h.settle(); // Wait for the workspace bootstrap before checking its scoped route.
    assert.equal(pageName(h), expectedPage);
    await h.settle();
    assert.equal(pageName(h), expectedPage);
    assert.deepEqual(h.destinations, []);
  });
}

test("a late admin root response cannot redirect after unmount", async (t) => {
  const verdict = deferred();
  const h = await harness(t, { probe: () => verdict.promise });
  await h.settle();
  h.unmount();
  verdict.resolve(adminOverview);
  await h.settle();
  assert.deepEqual(h.destinations, []);
  assert.equal(h.updatesAfterUnmount, 0);
});

test("a late admin shell response cannot update an unmounted page", async (t) => {
  const verdict = deferred();
  const h = await harness(t, { shell: true, probe: () => verdict.promise });
  await h.settle();
  h.unmount();
  verdict.resolve(adminOverview);
  await h.settle();
  assert.equal(h.updatesAfterUnmount, 0);
});

test("a previous account's pending admin verdict cannot redirect the new account", async (t) => {
  const previous = deferred();
  const h = await harness(t, {
    probe: (session) => session.user.id === 1 ? previous.promise : null,
  });
  await h.settle();
  h.setSession(account(2));
  await h.settle();
  previous.resolve(adminOverview);
  await h.settle();
  assert.equal(pageName(h), "ProjectsPage");
  assert.deepEqual(h.destinations, []);
});

test("switching accounts clears the admin sidebar while the new verdict is pending", async (t) => {
  const next = deferred();
  const h = await harness(t, {
    shell: true,
    probe: (session) => session.user.id === 1 ? adminOverview : next.promise,
  });
  await h.settle();
  assert.deepEqual(sidebarLinks(h), ["/admin"]);
  h.setSession(account(2));
  assert.deepEqual(sidebarLinks(h), ["/projects", "/runs", "/members"]);
  assert.equal(brandHref(h), "/projects");
  next.resolve(null);
  await h.settle();
  assert.deepEqual(sidebarLinks(h), ["/projects", "/runs", "/members"]);
});

test("valid tokens can confirm admin navigation without profile metadata", async (t) => {
  const h = await harness(t, {
    shell: true, session: { ...account(1), user: null }, overview: adminOverview,
  });
  await h.settle();
  assert.deepEqual(sidebarLinks(h), ["/admin"]);
  assert.equal(brandHref(h), "/admin");
});

test("a refreshed session rechecks the server verdict instead of keeping the old grant", async (t) => {
  const h = await harness(t, {
    shell: true,
    probe: (session) => session.accessToken === "refreshed-access" ? null : adminOverview,
  });
  await h.settle();
  assert.deepEqual(sidebarLinks(h), ["/admin"]);
  h.setSession({ ...account(1), accessToken: "refreshed-access", refreshToken: "refreshed-refresh" });
  assert.deepEqual(sidebarLinks(h), ["/projects", "/runs", "/members"]);
  await h.settle();
  assert.deepEqual(sidebarLinks(h), ["/projects", "/runs", "/members"]);
  assert.equal(h.probeCalls.length, 2);
});

function submitLogin(h) {
  const form = elements(h.tree, (node) => node.type === "form")[0];
  assert.ok(form, "render the real login form");
  form.props.onSubmit({
    preventDefault() {},
    currentTarget: { identifier: " alice ", password: "test-password" },
  });
}

async function authHarness(t, screen, options = {}) {
  const path = screen === "login" ? "/login" : "/auth/callback?code=one-time-code";
  const h = await harness(t, { screen, path, ...options });
  if (screen === "login") submitLogin(h);
  return h;
}

for (const screen of ["login", "oauth"]) {
  for (const [verdict, options, destination] of [
    ["admin", { overview: adminOverview }, "/admin"],
    ["ordinary", { overview: null }, "/projects"],
    ["404", { failure: 404 }, "/projects"],
    ["network error", { failure: "network" }, "/projects"],
    ["503", { failure: 503 }, "/projects"],
  ]) {
    test(`${screen} success lands on ${destination} after the ${verdict} probe`, async (t) => {
      const h = await authHarness(t, screen, options);
      await h.settle();
      assert.deepEqual(h.destinations, [destination]);
      assert.equal(h.probeCalls.length, 1, "perform one server check before landing");
      if (screen === "login") {
        assert.deepEqual(h.authCalls[0], ["login", "alice", "test-password"]);
        assert.equal(h.authCalls[1][0], "establish");
        assert.equal(h.probeCalls[0].accessToken, "login-access", "probe the newly established session");
      } else {
        assert.deepEqual(h.authCalls, [["exchange", "one-time-code"]]);
      }
    });
  }

  for (const next of ["/projects", "/datasets/42?split=train#image-2"]) {
    test(`${screen} preserves the admin's explicit next=${next}`, async (t) => {
      const path = `${screen === "login" ? "/login?" : "/auth/callback?code=one-time-code&"}next=${encodeURIComponent(next)}`;
      const h = await authHarness(t, screen, { path, overview: adminOverview });
      await h.settle();
      assert.deepEqual(h.destinations, [next]);
    });
  }

  test(`${screen} preserves an ordinary user's explicit local destination`, async (t) => {
    const path = `${screen === "login" ? "/login?" : "/auth/callback?code=one-time-code&"}next=%2Fruns%2F42`;
    const h = await authHarness(t, screen, { path, overview: null });
    await h.settle();
    assert.deepEqual(h.destinations, ["/runs/42"]);
  });

  for (const next of ["https://other.example/projects", "//other.example/projects", "/\\other.example/projects"]) {
    test(`${screen} rejects unsafe next=${next} and uses the admin landing`, async (t) => {
      const path = `${screen === "login" ? "/login?" : "/auth/callback?code=one-time-code&"}next=${encodeURIComponent(next)}`;
      const h = await authHarness(t, screen, { path, overview: adminOverview });
      await h.settle();
      assert.deepEqual(h.destinations, ["/admin"]);
    });
  }

  test(`${screen} waits for the admin verdict before navigating`, async (t) => {
    const verdict = deferred();
    const h = await authHarness(t, screen, { probe: () => verdict.promise });
    await h.settle();
    assert.deepEqual(h.destinations, []);
    assert.equal(h.probeCalls.length, 1);
    verdict.resolve(adminOverview);
    await h.settle();
    assert.deepEqual(h.destinations, ["/admin"]);
  });
}

test("an OAuth exchange completing after unmount cannot probe or navigate", async (t) => {
  const exchange = deferred();
  const h = await authHarness(t, "oauth", { exchange: () => exchange.promise, overview: adminOverview });
  h.unmount();
  exchange.resolve();
  await h.settle();
  assert.deepEqual(h.destinations, []);
  assert.equal(h.probeCalls.length, 0);
  assert.equal(h.updatesAfterUnmount, 0);
});

test("an OAuth admin verdict completing after unmount cannot navigate", async (t) => {
  const verdict = deferred();
  const h = await authHarness(t, "oauth", { probe: () => verdict.promise });
  await h.settle();
  assert.equal(h.probeCalls.length, 1);
  h.unmount();
  verdict.resolve(adminOverview);
  await h.settle();
  assert.deepEqual(h.destinations, []);
  assert.equal(h.updatesAfterUnmount, 0);
});

const viewerBrand = (h) => elements(h.tree, (node) => node.props?.className === "viewer-brand")[0];
function clickViewerBrand(h, overrides = {}) {
  const logo = viewerBrand(h);
  assert.ok(logo, "render the real viewer logo");
  const event = {
    button: 0, prevented: false,
    preventDefault() { this.prevented = true; },
    ...overrides,
  };
  logo.props.onClick(event);
  return event;
}

for (const [verdict, options, destination] of [
  ["admin", { overview: adminOverview }, "/admin"],
  ["ordinary", { overview: null }, "/projects"],
  ["404", { failure: 404 }, "/projects"],
  ["network error", { failure: "network" }, "/projects"],
  ["503", { failure: 503 }, "/projects"],
]) {
  test(`the viewer logo uses ${destination} for a ${verdict} verdict and flushes before leaving`, async (t) => {
    const h = await harness(t, { screen: "viewer", ...options });
    assert.equal(viewerBrand(h).props.href, "/projects", "preserve the existing target before confirmation");
    await h.settle();
    assert.equal(viewerBrand(h).props.href, destination);
    assert.equal(clickViewerBrand(h).prevented, true);
    await h.settle();
    assert.equal(h.flushCalls, 1);
    assert.deepEqual(h.destinations, [destination]);
  });
}

test("the admin viewer logo stays on the editor if flushing fails", async (t) => {
  const h = await harness(t, { screen: "viewer", overview: adminOverview, flush: () => false });
  await h.settle();
  clickViewerBrand(h);
  await h.settle();
  assert.equal(h.flushCalls, 1);
  assert.deepEqual(h.destinations, []);
});

test("the admin viewer logo waits for a pending flush before navigating to admin", async (t) => {
  const save = deferred();
  const h = await harness(t, { screen: "viewer", overview: adminOverview, flush: () => save.promise });
  await h.settle();
  clickViewerBrand(h);
  await h.settle();
  assert.equal(h.flushCalls, 1);
  assert.deepEqual(h.destinations, []);
  save.resolve(true);
  await h.settle();
  assert.deepEqual(h.destinations, ["/admin"]);
});

test("modified clicks on the admin viewer logo retain native navigation without flushing", async (t) => {
  const h = await harness(t, { screen: "viewer", overview: adminOverview });
  await h.settle();
  assert.equal(viewerBrand(h).props.href, "/admin");
  for (const event of [{ ctrlKey: true }, { metaKey: true }, { shiftKey: true }, { altKey: true }, { button: 1 }]) {
    assert.equal(clickViewerBrand(h, event).prevented, false);
  }
  await h.settle();
  assert.equal(h.flushCalls, 0);
  assert.deepEqual(h.destinations, []);
});

test("a late viewer admin verdict cannot update an unmounted editor", async (t) => {
  const verdict = deferred();
  const h = await harness(t, { screen: "viewer", probe: () => verdict.promise });
  await h.settle();
  h.unmount();
  verdict.resolve(adminOverview);
  await h.settle();
  assert.equal(h.updatesAfterUnmount, 0);
  assert.deepEqual(h.destinations, []);
});
