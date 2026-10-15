import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import ts from "typescript";

const moduleUrl = (source) => `data:text/javascript;base64,${Buffer.from(source).toString("base64")}`;
const settle = () => new Promise((resolve) => setImmediate(resolve));
const storeDependencies = moduleUrl(`
  export const useSyncExternalStore = () => {};
  export const requestJson = async () => ({}), invalidateStorageQuotaCache = () => {};
  export const navigateWithinApp = () => {}, clearAuthenticatedResourceCache = () => {};
  export const readStoredString = () => null, writeStoredString = () => {};
`);
const storeSource = await readFile(new URL("../src/store/workspace.ts", import.meta.url), "utf8");
const storeModule = ts.transpileModule(storeSource, { compilerOptions: {
  module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022,
} }).outputText.replace(/from "[^"]+"/g, `from "${storeDependencies}"`);
const labels = (await import(moduleUrl(storeModule))).WORKSPACE_ROLE_LABELS;
const personal = { id: 1, kind: "personal", name: "개인", role: "owner" };
const team = { id: 7, kind: "team", name: "비전팀", role: "editor" };
const preview = { workspace_id: 7, workspace_name: "비전팀", role: "editor", expires_at: "2026-10-20T00:00:00Z", already_member: false };

class ApiError extends Error {
  constructor(status, message, detail) { super(message); this.status = status; this.detail = detail; }
}

function elements(tree, predicate) {
  if (!tree || typeof tree !== "object") return [];
  return [predicate(tree) ? tree : null, ...[tree.props?.children].flat(Infinity).flatMap((child) => elements(child, predicate))].filter(Boolean);
}
function text(tree) {
  if (typeof tree === "string" || typeof tree === "number") return String(tree);
  if (!tree || typeof tree !== "object") return "";
  return [tree?.props?.children].flat(Infinity).map(text).join("");
}

async function harness(t, componentName, overrides = {}) {
  const calls = [];
  const state = {
    calls,
    snapshot: { items: [personal, team], active: team },
    createWorkspace: async (name) => { calls.push(["create", name]); return team; },
    getInvitePreview: async (token) => { calls.push(["preview", token]); return preview; },
    acceptInvite: async (token) => { calls.push(["accept", token]); return { workspace_id: 7, role: "editor", already_member: false }; },
    reloadWorkspaces: async () => { calls.push(["reload"]); },
    switchWorkspace: (...args) => calls.push(["switch", ...args]),
    navigateWithinApp: (path) => calls.push(["navigate", path]),
    ...overrides,
  };
  const originalDocument = globalThis.document;
  const originalWindow = globalThis.window;
  const listeners = new Map();
  const browser = {
    activeElement: { focus() {} },
    addEventListener(name, callback) { if (!listeners.has(name)) listeners.set(name, new Set()); listeners.get(name).add(callback); },
    removeEventListener(name, callback) { listeners.get(name)?.delete(callback); },
  };
  globalThis.document = browser;
  globalThis.window = browser;
  const slots = [];
  let cursor = 0;
  let effects = [];
  const hooks = {
    useState(initial) {
      const i = cursor++;
      if (!(i in slots)) slots[i] = typeof initial === "function" ? initial() : initial;
      return [slots[i], (value) => { slots[i] = typeof value === "function" ? value(slots[i]) : value; }];
    },
    useRef(current) { const i = cursor++; return slots[i] ??= { current }; },
    useEffect(callback, deps) {
      const i = cursor++;
      if (!slots[i] || deps?.some((value, n) => value !== slots[i].deps[n])) {
        effects.push(() => { slots[i]?.cleanup?.(); slots[i] = { deps, cleanup: callback() }; });
      }
    },
    useId: () => "test-workspace",
  };
  const key = `__workspaceUi${Math.random().toString(16).slice(2)}`;
  globalThis[key] = { state, hooks, labels, ApiError };
  const stub = moduleUrl(`
    const { state, hooks, labels, ApiError } = globalThis[${JSON.stringify(key)}];
    export const { useState, useRef, useEffect, useId } = hooks;
    export { ApiError };
    export const jsx = (type, props) => ({type, props}), jsxs = jsx, Fragment = "fragment";
    export const WORKSPACE_ROLE_LABELS = labels;
    export const useWorkspace = () => state.snapshot, useAuthSession = () => ({user:{email:"kim@example.com"}});
    export const createWorkspace = (...args) => state.createWorkspace(...args);
    export const getInvitePreview = (...args) => state.getInvitePreview(...args);
    export const acceptInvite = (...args) => state.acceptInvite(...args);
    export const reloadWorkspaces = (...args) => state.reloadWorkspaces(...args);
    export const switchWorkspace = (...args) => state.switchWorkspace(...args);
    export const navigateWithinApp = (...args) => state.navigateWithinApp(...args);
    export const appHref = (path) => path;
    export const Brand = () => null, Icon = () => null, NewWorkspaceDialog = () => null;
  `);
  const folder = componentName === "InviteAcceptPage" ? "pages" : "components";
  const source = await readFile(new URL(`../src/${folder}/${componentName}.tsx`, import.meta.url), "utf8");
  const compiled = ts.transpileModule(source, { compilerOptions: {
    module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX,
  } }).outputText.replace(/from "[^"]+"/g, `from "${stub}"`);
  const Component = (await import(moduleUrl(compiled)))[componentName];
  const unmount = () => { slots.forEach((slot) => slot?.cleanup?.()); };
  t.after(() => { unmount(); globalThis.document = originalDocument; globalThis.window = originalWindow; delete globalThis[key]; });
  return {
    calls, state, unmount,
    render(props = {}) { cursor = 0; const tree = Component(props); const pending = effects; effects = []; pending.forEach((effect) => effect()); return tree; },
  };
}

test("workspace display labels retain historical viewer alongside owner, admin, and labeler", () => {
  assert.deepEqual(labels, { owner: "소유자", admin: "관리자", editor: "라벨러", viewer: "보기 전용" });
});

test("switcher shows personal first, marks active scope and switches selected workspace", async (t) => {
  const h = await harness(t, "WorkspaceSwitcher");
  let tree = h.render();
  const trigger = elements(tree, (item) => item.props?.["aria-haspopup"] === "menu")[0];
  assert.match(text(trigger), /비전팀라벨러/);
  trigger.props.onClick();
  tree = h.render();
  const choices = elements(tree, (item) => item.props?.className?.includes("ws-menu-item"));
  assert.match(text(choices[0]), /개인나만 사용/);
  assert.equal(choices[1].props["aria-current"], "true");
  choices[0].props.onClick();
  assert.deepEqual(h.calls, [["switch", 1]]);
  assert.equal(elements(h.render(), (item) => item.props?.role === "menu").length, 0);
});

test("team creation trims name, waits for refreshed membership, then switches and closes", async (t) => {
  let release;
  const wait = new Promise((resolve) => { release = resolve; });
  const h = await harness(t, "NewWorkspaceDialog", { reloadWorkspaces: () => wait });
  const props = { onClose: () => h.calls.push(["close"]) };
  let tree = h.render(props);
  assert.equal(elements(tree, (item) => item.props?.type === "submit")[0].props.disabled, true);
  elements(tree, (item) => item.type === "input")[0].props.onChange({ target: { value: "  비전팀  " } });
  tree = h.render(props);
  const submit = elements(tree, (item) => item.type === "form")[0].props.onSubmit({ preventDefault() {} });
  await settle();
  assert.deepEqual(h.calls, [["create", "비전팀"]]);
  release();
  await submit;
  assert.deepEqual(h.calls, [["create", "비전팀"], ["switch", 7], ["close"]]);
});

test("team limit response keeps dialog open and displays returned limit", async (t) => {
  const h = await harness(t, "NewWorkspaceDialog", { createWorkspace: async () => { throw new ApiError(409, "limit", { code: "team_limit_reached", limit: 4 }); } });
  const props = { onClose: () => h.calls.push(["close"]) };
  let tree = h.render(props);
  elements(tree, (item) => item.type === "input")[0].props.onChange({ target: { value: "팀" } });
  tree = h.render(props);
  await elements(tree, (item) => item.type === "form")[0].props.onSubmit({ preventDefault() {} });
  assert.match(text(h.render(props)), /팀은 최대 4개까지 만들 수 있어요/);
  assert.deepEqual(h.calls, []);
});

test("retrying membership refresh after team creation does not create another team", async (t) => {
  let failed = false;
  const h = await harness(t, "NewWorkspaceDialog", { reloadWorkspaces: async () => {
    if (!failed) { failed = true; throw new Error("목록을 새로고침하지 못했어요."); }
  } });
  const props = { onClose: () => h.calls.push(["close"]) };
  let tree = h.render(props);
  elements(tree, (item) => item.type === "input")[0].props.onChange({ target: { value: "비전팀" } });
  tree = h.render(props);
  await elements(tree, (item) => item.type === "form")[0].props.onSubmit({ preventDefault() {} });
  tree = h.render(props);
  assert.match(text(tree), /목록을 새로고침하지 못했어요/);
  await elements(tree, (item) => item.type === "form")[0].props.onSubmit({ preventDefault() {} });
  assert.deepEqual(h.calls, [["create", "비전팀"], ["switch", 7], ["close"]]);
});

test("invite preview needs confirmation and later leaves the active scope unchanged", async (t) => {
  const h = await harness(t, "InviteAcceptPage");
  h.render({ token: "secret" });
  await settle();
  const tree = h.render({ token: "secret" });
  assert.match(text(tree), /비전팀 워크스페이스에 들어가시겠어요\?/);
  assert.match(text(tree), /라벨러 역할로 초대받았어요/);
  assert.match(text(tree), /kim@example.com 으로 로그인됨/);
  elements(tree, (item) => item.type === "button" && text(item) === "나중에")[0].props.onClick();
  assert.deepEqual(h.calls, [["preview", "secret"], ["navigate", "/projects"]]);
});

test("existing membership opens the team without consuming invitation again", async (t) => {
  const h = await harness(t, "InviteAcceptPage", { getInvitePreview: async () => ({ ...preview, already_member: true }) });
  h.render({ token: "secret" });
  await settle();
  assert.deepEqual(h.calls, [["reload"], ["switch", 7, "'비전팀'으로 이동했어요"]]);
});

test("accept waits for membership refresh and switches only after the user confirms", async (t) => {
  const h = await harness(t, "InviteAcceptPage");
  h.render({ token: "secret" });
  await settle();
  const tree = h.render({ token: "secret" });
  await elements(tree, (item) => item.type === "button" && text(item) === "들어가기")[0].props.onClick();
  assert.deepEqual(h.calls, [["preview", "secret"], ["accept", "secret"], ["reload"], ["switch", 7]]);
});

for (const stage of ["preview", "accept"]) {
  test(`invite ${stage} authentication failure returns to login with this invitation`, async (t) => {
    const reject = async () => { throw new ApiError(401, "unauthenticated"); };
    const h = await harness(t, "InviteAcceptPage", stage === "preview"
      ? { getInvitePreview: reject } : { acceptInvite: reject });
    h.render({ token: "secret" });
    await settle();
    if (stage === "accept") {
      const tree = h.render({ token: "secret" });
      await elements(tree, (item) => item.type === "button" && text(item) === "들어가기")[0].props.onClick();
    }
    assert.deepEqual(h.calls.at(-1), ["navigate", "/login?next=%2Finvites%2Fsecret"]);
  });
}

test("double acceptance clicks send one request and show a newly exhausted link", async (t) => {
  let reject;
  let acceptCount = 0;
  const h = await harness(t, "InviteAcceptPage", { acceptInvite: () => {
    acceptCount++;
    return new Promise((_resolve, no) => { reject = no; });
  } });
  h.render({ token: "secret" });
  await settle();
  const tree = h.render({ token: "secret" });
  const click = elements(tree, (item) => item.type === "button" && text(item) === "들어가기")[0].props.onClick;
  const pending = click();
  await click();
  assert.equal(acceptCount, 1);
  reject(new ApiError(410, "failure", { code: "invite_exhausted" }));
  await pending;
  assert.match(text(h.render({ token: "secret" })), /이 링크는 사용 횟수를 다 썼어요/);
  assert.deepEqual(h.calls, [["preview", "secret"]]);
});

for (const [status, code, message] of [
  [404, null, "사용할 수 없는 링크예요. 주소를 확인하거나 새 링크를 요청하세요."],
  [410, "invite_expired", "링크가 만료됐어요. 초대한 분께 새 링크를 요청하세요."],
  [410, "invite_exhausted", "이 링크는 사용 횟수를 다 썼어요. 초대한 분께 새 링크를 요청하세요."],
  [410, "invite_revoked", "이 링크는 더 이상 쓸 수 없어요. 초대한 분께 새 링크를 요청하세요."],
]) {
  test(`invite ${code ?? status} is displayed with a projects escape`, async (t) => {
    const h = await harness(t, "InviteAcceptPage", { getInvitePreview: async () => { throw new ApiError(status, "failure", { code }); } });
    h.render({ token: "secret" });
    await settle();
    const tree = h.render({ token: "secret" });
    assert.equal(text(elements(tree, (item) => item.props?.role === "alert")[0]), message);
    elements(tree, (item) => item.type === "button" && text(item) === "프로젝트로 가기")[0].props.onClick();
    assert.deepEqual(h.calls, [["navigate", "/projects"]]);
  });
}

for (const [code, message] of [
  ["invite_revoked", "이 링크는 더 이상 쓸 수 없어요. 초대한 분께 새 링크를 요청하세요."],
  ["invite_expired", "링크가 만료됐어요. 초대한 분께 새 링크를 요청하세요."],
  ["invite_exhausted", "이 링크는 사용 횟수를 다 썼어요. 초대한 분께 새 링크를 요청하세요."],
]) {
  test(`invite accept ${code} displays its exact explanation without entering the workspace`, async (t) => {
    const h = await harness(t, "InviteAcceptPage", {
      acceptInvite: async () => { throw new ApiError(410, "요청 실패 (410)", { code }); },
    });
    h.render({ token: "secret" });
    await settle();
    const confirmation = h.render({ token: "secret" });
    await elements(confirmation, (item) => item.type === "button" && text(item) === "들어가기")[0].props.onClick();
    const tree = h.render({ token: "secret" });
    assert.equal(text(elements(tree, (item) => item.props?.role === "alert")[0]), message);
    assert.equal(elements(tree, (item) => item.type === "button" && text(item) === "들어가기").length, 0);
    assert.deepEqual(h.calls, [["preview", "secret"]], "failed acceptance must not refresh or switch the workspace");
    elements(tree, (item) => item.type === "button" && text(item) === "프로젝트로 가기")[0].props.onClick();
    assert.deepEqual(h.calls.at(-1), ["navigate", "/projects"]);
  });
}

test("late invite preview after unmount never changes workspace", async (t) => {
  let resolve;
  const h = await harness(t, "InviteAcceptPage", { getInvitePreview: () => new Promise((yes) => { resolve = yes; }) });
  h.render({ token: "secret" });
  h.unmount();
  resolve({ ...preview, already_member: true });
  await settle();
  assert.deepEqual(h.calls, []);
});
