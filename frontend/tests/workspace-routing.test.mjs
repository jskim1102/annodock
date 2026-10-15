import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import ts from "typescript";

const url = (source) => `data:text/javascript;base64,${Buffer.from(source).toString("base64")}`;
const settle = () => new Promise((resolve) => setImmediate(resolve));
const personal = { id: 101, kind: "personal", name: "개인", role: "owner" };
const team = { id: 202, kind: "team", name: "팀", role: "editor" };
const user = { id: 1, email: "user@example.com", username: "tester", identities: [] };

function elements(tree, predicate) {
  if (!tree || typeof tree !== "object") return [];
  return [predicate(tree) ? tree : null, ...[tree.props?.children].flat(Infinity).flatMap((child) => elements(child, predicate))].filter(Boolean);
}
function text(tree) {
  if (typeof tree === "string" || typeof tree === "number") return String(tree);
  return !tree || typeof tree !== "object" ? "" : [tree.props?.children].flat(Infinity).map(text).join("");
}

async function harness(t, { path = "/projects", authUser = user, bootstrap, hydrate } = {}) {
  const h = {
    session: { accessToken: "access", refreshToken: "refresh", user: authUser },
    workspace: { userId: 1, active: personal, items: [personal, team], status: "ready", notFound: false, error: null, message: null },
    bootstrapCalls: [], hydrateCalls: 0, mounted: [], unmounted: [], redirects: [],
  };
  h.bootstrap = bootstrap ?? (async () => {});
  h.hydrate = hydrate ?? (async () => user);
  const originalWindow = globalThis.window;
  const location = new URL(path, "https://annodock.example");
  const listeners = new Map();
  globalThis.window = {
    location: {
      pathname: location.pathname, search: location.search, hash: location.hash,
      origin: location.origin,
      replace: (next) => h.redirects.push(next), assign: (next) => h.redirects.push(next),
    },
    addEventListener(name, callback) { if (!listeners.has(name)) listeners.set(name, new Set()); listeners.get(name).add(callback); },
    removeEventListener(name, callback) { listeners.get(name)?.delete(callback); },
  };
  const frames = new Map();
  let activeFrame;
  let hookIndex = 0;
  let effects = [];
  let visited;
  const hooks = {
    useState(initial) {
      const frame = activeFrame;
      const i = hookIndex++;
      if (!(i in frame.slots)) frame.slots[i] = typeof initial === "function" ? initial() : initial;
      return [frame.slots[i], (value) => { frame.slots[i] = typeof value === "function" ? value(frame.slots[i]) : value; }];
    },
    useRef(current) { const frame = activeFrame; const i = hookIndex++; return frame.slots[i] ??= { current }; },
    useEffect(callback, deps) {
      const frame = activeFrame;
      const i = hookIndex++;
      if (!frame.slots[i] || deps?.some((value, n) => value !== frame.slots[i].deps[n])) {
        effects.push(() => { frame.slots[i]?.cleanup?.(); frame.slots[i] = { deps, cleanup: callback() }; });
      }
    },
    useSyncExternalStore: (_subscribe, getSnapshot) => getSnapshot(),
  };
  const key = `__workspaceRouting${Math.random().toString(16).slice(2)}`;
  globalThis[key] = { h, hooks };
  const pageNames = ["LoginPage", "OAuthCallbackPage", "PasswordResetPage", "SignupPage", "ProjectsPage", "RunDetailPage", "RunsPage", "TrainPage", "UploadPage", "ViewerPage", "InviteAcceptPage", "MembersPage", "AdminPage"];
  const stub = url(`
    const { h, hooks } = globalThis[${JSON.stringify(key)}];
    export const {useEffect,useState,useRef,useSyncExternalStore} = hooks;
    export const jsx = (type,props,key) => ({type,props,key}), jsxs = jsx, Fragment = "fragment";
    export const ThemeProvider = ({children}) => children;
    export const useAdminNavigation = () => false;
    export const useAuthSession = () => h.session, getAuthSnapshot = () => h.session;
    export const setAuthSession = (tokens,user) => {h.session = {accessToken:tokens.access_token,refreshToken:tokens.refresh_token,user};};
    export const clearAuthSession = () => {h.session = {accessToken:null,refreshToken:null,user:null};};
    export class AuthApiError extends Error {constructor(status,message){super(message);this.status=status;}}
    export const refreshAuthSession = async () => ({access_token:h.session.accessToken,refresh_token:h.session.refreshToken});
    export const getCurrentUser = async (...args) => {h.hydrateCalls++; return h.hydrate(...args);};
    export const useWorkspace = () => h.workspace, getWorkspaceSnapshot = () => h.workspace;
    export const initializeWorkspaces = async (...args) => {h.bootstrapCalls.push(args); await h.bootstrap(h,...args);};
    export const resetWorkspaces = () => {}, dismissWorkspaceMessage = () => {};
    export const getLocationSnapshot = () => window.location.pathname+window.location.search+window.location.hash;
    export const subscribeNavigation = () => () => {};
    export const appHref = (path) => {const target = new URL(path,window.location.origin);const ws=new URLSearchParams(window.location.search).get("ws");if(ws&&!target.searchParams.has("ws"))target.searchParams.set("ws",ws);return target.pathname+target.search;};
    ${pageNames.map((name) => `export function ${name}(props){hooks.useEffect(()=>{h.mounted.push("${name}");return()=>h.unmounted.push("${name}");},[]);return jsx("page",{...props,name:"${name}"});}`).join("\n")}
  `);
  const source = await readFile(new URL("../src/App.tsx", import.meta.url), "utf8");
  const compiled = ts.transpileModule(source, { compilerOptions: {
    module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX,
  } }).outputText.replace(/from "[^"]+"/g, `from "${stub}"`);
  const { App } = await import(url(compiled));
  function renderElement(element, parent = "root") {
    if (Array.isArray(element)) return element.map((child, i) => renderElement(child, `${parent}:${i}`));
    if (!element || typeof element !== "object") return element;
    if (typeof element.type === "function") {
      const id = `${parent}:${element.type.name}:${element.key ?? "default"}`;
      if (!frames.has(id)) frames.set(id, { slots: [] });
      visited.add(id);
      activeFrame = frames.get(id); hookIndex = 0;
      return renderElement(element.type(element.props ?? {}), id);
    }
    return { ...element, props: { ...element.props, children: renderElement(element.props?.children, `${parent}:${element.type}`) } };
  }
  const cleanup = (frame) => frame.slots.forEach((slot) => slot?.cleanup?.());
  t.after(() => { frames.forEach(cleanup); globalThis.window = originalWindow; delete globalThis[key]; });
  h.render = () => {
    visited = new Set(); effects = [];
    const tree = renderElement({ type: App, props: {} });
    for (const [id, frame] of frames) if (!visited.has(id)) { cleanup(frame); frames.delete(id); }
    effects.forEach((effect) => effect());
    return tree;
  };
  h.navigate = (next) => {
    const location = new URL(next, window.location.origin);
    Object.assign(window.location, { pathname: location.pathname, search: location.search, hash: location.hash });
  };
  return h;
}

test("protected workspace pages mount only after initial membership bootstrap resolves", async (t) => {
  let resolve;
  const h = await harness(t, { path: "/datasets/42/viewer", bootstrap: () => new Promise((yes) => { resolve = yes; }) });
  assert.match(text(h.render()), /워크스페이스를 불러오는 중/);
  assert.deepEqual(h.mounted, []);
  assert.deepEqual(h.bootstrapCalls, [[1, null]]);
  resolve(); await settle(); h.render();
  assert.deepEqual(h.mounted, ["ViewerPage"]);
});

test("nonmember workspace deep link renders 404 and removes its query from the return link", async (t) => {
  const h = await harness(t, { path: "/projects?ws=999", bootstrap: async (state) => { state.workspace.notFound = true; } });
  h.render(); await settle(); const tree = h.render();
  assert.match(text(tree), /페이지를 찾을 수 없습니다/);
  assert.equal(elements(tree, (item) => item.type === "a")[0].props.href, "/projects");
  assert.deepEqual(h.mounted, []);
  assert.equal(h.workspace.active.id, 101);
});

test("workspace changes replace the scoped page and internal navigation lands on projects", async (t) => {
  const h = await harness(t, { path: "/datasets/42/viewer" });
  h.render(); await settle(); h.render();
  h.workspace = { ...h.workspace, active: team };
  h.render();
  assert.deepEqual(h.unmounted, ["ViewerPage"], "workspace key remounts even the same viewer URL");
  assert.deepEqual(h.mounted, ["ViewerPage", "ViewerPage"]);
  h.navigate("/projects?ws=202");
  h.render(); await settle(); h.render();
  assert.deepEqual(h.unmounted, ["ViewerPage", "ViewerPage"]);
  assert.equal(h.mounted.at(-1), "ProjectsPage");
  assert.deepEqual(h.redirects, [], "switch does not reload the document");
});

test("membership list refresh loading and failure keep the current page and its dialogs mounted", async (t) => {
  const h = await harness(t);
  h.render(); await settle(); h.render();
  h.workspace = { ...h.workspace, status: "loading" };
  h.render();
  h.workspace = { ...h.workspace, status: "error", error: "목록 갱신 실패" };
  const tree = h.render();
  assert.equal(elements(tree, (item) => item.props?.name === "ProjectsPage").length, 1);
  assert.deepEqual(h.mounted, ["ProjectsPage"]);
  assert.deepEqual(h.unmounted, [], "refresh error must not discard dialog creation state");
});

test("initial workspace bootstrap failure has a retry gate before any protected page mounts", async (t) => {
  let failed = false;
  const h = await harness(t, { bootstrap: async (state) => {
    if (!failed) { failed = true; state.workspace = { ...state.workspace, status: "error", error: "초기 목록 실패" }; }
    else state.workspace = { ...state.workspace, status: "ready", error: null };
  } });
  h.render(); await settle(); let tree = h.render();
  assert.match(text(tree), /초기 목록 실패/);
  assert.deepEqual(h.mounted, []);
  elements(tree, (item) => item.type === "button" && text(item) === "다시 시도")[0].props.onClick();
  h.render(); await settle(); tree = h.render();
  assert.equal(h.bootstrapCalls.length, 2);
  assert.equal(elements(tree, (item) => item.props?.name === "ProjectsPage").length, 1);
});

test("restored token session hydrates its missing user before workspace bootstrap", async (t) => {
  let resolve;
  const h = await harness(t, { authUser: null, hydrate: () => new Promise((yes) => { resolve = yes; }) });
  h.render();
  assert.equal(h.hydrateCalls, 1);
  assert.deepEqual(h.bootstrapCalls, []);
  assert.deepEqual(h.mounted, []);
  resolve(user); await settle(); h.render(); await settle(); h.render();
  assert.deepEqual(h.bootstrapCalls, [[1, null]]);
  assert.deepEqual(h.mounted, ["ProjectsPage"]);
});
