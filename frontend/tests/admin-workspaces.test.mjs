import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import ts from "typescript";

const moduleUrl = source => `data:text/javascript;base64,${Buffer.from(source).toString("base64")}`;
const GiB = 1024 ** 3;
const workspaceRows = [
  { id: 82, kind: "team", name: "검수 팀", owner_user_id: 2, member_count: 3,
    bytes_used: 3 * GiB, limit_bytes: 5 * GiB, quota_override: true },
  { id: 71, kind: "personal", name: "Alice 개인", owner_user_id: 1, member_count: 1,
    bytes_used: GiB, limit_bytes: 2 * GiB, quota_override: false },
];
const userRows = [
  { id: 1, email: "alice@example.test", username: "alice", created_at: "2026-10-01T00:00:00Z",
    login_methods: ["password"], bytes_used: GiB, limit_bytes: 2 * GiB,
    workspaces: [{ ...workspaceRows[1], role: "owner" }, { ...workspaceRows[0], role: "editor" }] },
  { id: 2, email: "bob@example.test", username: "bob", created_at: "2026-10-02T00:00:00Z",
    login_methods: [], bytes_used: 0, limit_bytes: 2 * GiB,
    workspaces: [{ ...workspaceRows[0], role: "owner" }] },
];
const overview = { user_count: 2, storage_total_bytes: 4 * GiB };
const ownedTeams = [{ id: 82, name: "검수 팀" }, { id: 93, name: "분석 팀" }];
function elements(node) {
  if (Array.isArray(node)) return node.flatMap(elements);
  return !node || typeof node !== "object" ? [] : [node, ...elements(node.props?.children)];
}
function textOf(node) {
  if (Array.isArray(node)) return node.map(textOf).join("");
  if (typeof node === "string" || typeof node === "number") return String(node);
  return node && typeof node === "object" ? textOf(node.props?.children) : "";
}
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

// Exercise the real page, local dialogs, effects, and formatting modules.
// Only API responses and the enclosing shell are replaced at the boundary.
async function harness(t, options = {}) {
  const key = `__adminWorkspaces${Math.random().toString(16).slice(2)}`;
  class ApiError extends Error {
    constructor(status, message, detail) { super(message); this.status = status; this.detail = detail; }
  }
  const context = { ApiError, calls: [], updatesAfterUnmount: 0, users: structuredClone(options.users ?? userRows),
    workspaces: structuredClone(options.workspaces ?? workspaceRows) };
  context.call = async (name, args) => {
    context.calls.push([name, ...args]);
    if (options.api?.[name]) return options.api[name](...args, context);
    if (name === "probeAdminOverview" || name === "getAdminOverview") return overview;
    if (name === "getAdminUsers") return { users: structuredClone(context.users) };
    if (name === "getAdminWorkspaces") return { workspaces: structuredClone(context.workspaces) };
    if (name === "invalidateStorageQuotaCache") return;
    if (name === "updateAdminWorkspaceQuota") {
      const [id, limit] = args;
      for (const row of [...context.workspaces, ...context.users.flatMap(user => user.workspaces)]) {
        if (row.id === id) { row.limit_bytes = limit; row.quota_override = true; }
      }
      return { workspace_id: id, limit_bytes: limit };
    }
    throw new Error(`unexpected API call ${name}`);
  };
  const previousWindow = globalThis.window, previousDocument = globalThis.document;
  const listeners = new Map();
  const browser = {
    location: { origin: "https://annodock.test" }, setTimeout, clearTimeout,
    addEventListener(name, callback) {
      if (!listeners.has(name)) listeners.set(name, new Set());
      listeners.get(name).add(callback);
    },
    removeEventListener(name, callback) { listeners.get(name)?.delete(callback); },
  };
  globalThis.window = browser; globalThis.document = browser;
  const frames = new Map(), effects = [];
  let current, cursor = 0, disposed = false;
  const changed = (old, next) => !old || !next || old.length !== next.length
    || next.some((value, index) => !Object.is(value, old[index]));
  const hooks = {
    useState(initial) {
      const frame = current, index = cursor++;
      if (!(index in frame.slots)) frame.slots[index] = typeof initial === "function" ? initial() : initial;
      return [frame.slots[index], value => {
        if (frame.unmounted) { context.updatesAfterUnmount++; return; }
        frame.slots[index] = typeof value === "function" ? value(frame.slots[index]) : value;
      }];
    },
    useRef(value) { return current.slots[cursor++] ??= { current: value }; },
    useEffect(callback, deps) {
      const frame = current, index = cursor++;
      if (changed(frame.slots[index]?.deps, deps)) effects.push(() => {
        frame.cleanups.get(index)?.();
        frame.slots[index] = { deps }; frame.cleanups.set(index, callback());
      });
    },
    useMemo(factory, deps) {
      const index = cursor++;
      if (changed(current.slots[index]?.deps, deps)) current.slots[index] = { value: factory(), deps };
      return current.slots[index].value;
    },
    useCallback(callback, deps) { return hooks.useMemo(() => callback, deps); },
    useId() { return `admin-${cursor++}`; },
  };
  context.hooks = hooks; globalThis[key] = context;
  const cleanup = frame => { frame.unmounted = true; frame.cleanups.forEach(fn => fn?.()); };
  const unmount = () => { disposed = true; frames.forEach(cleanup); frames.clear(); };
  t.after(() => {
    unmount(); globalThis.window = previousWindow; globalThis.document = previousDocument;
    delete globalThis[key];
  });
  const bridge = `const c=globalThis[${JSON.stringify(key)}];`;
  const shared = moduleUrl(`${bridge}
    export const {useState,useRef,useEffect,useMemo,useCallback,useId}=c.hooks;
    export const jsx=(type,props,key)=>({type,props,key}),jsxs=jsx,Fragment="fragment";
    export const WORKSPACE_ROLE_LABELS={owner:"소유자",admin:"관리자",editor:"라벨러",viewer:"보기 전용"};
  `);
  const cache = new Map();
  async function compile(file) {
    if (cache.has(file.href)) return cache.get(file.href);
    const source = await readFile(file, "utf8");
    let javascript = ts.transpileModule(source, { compilerOptions: {
      module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX,
    } }).outputText;
    for (const match of [...javascript.matchAll(/import\s+([^;]+?)\s+from\s+"([^"]+)";/g)]) {
      const [statement, bindings, dependency] = match;
      const names = bindings.replace(/[{}]/g, "").split(",").map(name => name.trim()).filter(Boolean);
      let dependencyUrl;
      if (["react", "react/jsx-runtime", "../store/workspace"].includes(dependency)) dependencyUrl = shared;
      else if (dependency === "../api/client" || dependency === "../utils/adminAccess") {
        dependencyUrl = moduleUrl(`${bridge}\n${names.map(name => name === "ApiError"
          ? "export const ApiError=c.ApiError;"
          : `export const ${name}=(...args)=>c.call(${JSON.stringify(name)},args);`).join("\n")}`);
      } else if (dependency.endsWith("/AppShell")) {
        dependencyUrl = moduleUrl("export const AppShell=props=>({type:'app-shell',props});");
      } else if (dependency.endsWith("/Icon")) dependencyUrl = moduleUrl("export const Icon=()=>null;");
      else {
        assert.ok(dependency.startsWith("."), `unmapped dependency ${dependency}`);
        dependencyUrl = await compile(new URL(`${dependency}.ts`, file));
      }
      javascript = javascript.replace(statement, statement.replace(`"${dependency}"`, `"${dependencyUrl}"`));
    }
    const url = moduleUrl(javascript); cache.set(file.href, url); return url;
  }
  const { AdminPage } = await import(await compile(new URL("../src/pages/AdminPage.tsx", import.meta.url)));
  let seen;
  const evaluate = (component, props, path) => {
    if (!frames.has(path)) frames.set(path, { slots: [], cleanups: new Map(), unmounted: false });
    seen.add(path); current = frames.get(path); cursor = 0;
    return expand(component(props), path);
  };
  const expand = (node, path) => {
    if (Array.isArray(node)) return node.map((child, index) => expand(child, `${path}/${child?.key ?? index}`));
    if (!node || typeof node !== "object") return node;
    if (typeof node.type === "function") return evaluate(node.type, node.props, `${path}:${node.type.name}:${node.key ?? ""}`);
    return { ...node, props: { ...node.props, children: expand(node.props?.children, `${path}/children`) } };
  };
  const render = () => {
    if (disposed) return null;
    seen = new Set();
    const tree = evaluate(AdminPage, {}, "root");
    for (const [path, frame] of frames) if (!seen.has(path)) { cleanup(frame); frames.delete(path); }
    effects.splice(0).forEach(effect => effect());
    return tree;
  };
  const flush = async () => { for (let turn = 0; turn < 4; turn++) { render(); await new Promise(resolve => setImmediate(resolve)); } };
  render();
  return {
    context, ApiError, render, flush, unmount, text: () => textOf(render()),
    find: predicate => elements(render()).find(predicate),
    button(label, within = render()) {
      const button = elements(within).find(node => node.type === "button" && textOf(node).trim() === label);
      assert.ok(button, `button '${label}' exists`); return button;
    },
  };
}

function rowContaining(h, name) {
  const row = h.find(node => node.type === "tr" && textOf(node).includes(name));
  assert.ok(row, `row '${name}' exists`); return row;
}
async function showWorkspaces(h) { h.button("워크스페이스").props.onClick(); await h.flush(); }
function selectUser(h, email) {
  const checkbox = h.find(node => node.props?.["aria-label"] === `${email} 선택`);
  assert.ok(checkbox); checkbox.props.onClick(); h.render();
}
function submitQuota(h, value) {
  const dialog = h.find(node => node.props?.role === "dialog");
  assert.ok(dialog, "quota dialog exists");
  const input = elements(dialog).find(node => node.type === "input" && node.props.type === "number");
  assert.ok(input); input.props.onChange({ target: { value } });
  const form = h.find(node => node.type === "form");
  assert.ok(form); form.props.onSubmit({ preventDefault() {} });
}
function deletionConfirmation() {
  return { code: "admin-user-delete-confirmation-required", requires_confirmation: true,
    warning: "이 작업은 되돌릴 수 없습니다.", email: "alice@example.test", username: "alice",
    project_count: 1, dataset_count: 2, bytes_used: GiB };
}

test("pending and denied admin probes keep workspace management hidden", async t => {
  const pending = deferred();
  const h = await harness(t, { api: { probeAdminOverview: () => pending.promise } });
  assert.equal(h.render(), null);
  assert.deepEqual(h.context.calls.map(([name]) => name), ["probeAdminOverview"]);
  pending.resolve(null); await h.flush();
  assert.match(h.text(), /페이지를 찾을 수 없습니다/);
  assert.equal(h.find(node => node.type === "app-shell"), undefined);
  assert.equal(h.context.calls.some(([name]) => name === "getAdminWorkspaces"), false);
});

test("a server 404 keeps admin user and workspace tables hidden", async t => {
  const h = await harness(t, { api: { getAdminUsers: c => { throw new c.ApiError(404, "Not Found"); } } });
  await h.flush();
  assert.match(h.text(), /페이지를 찾을 수 없습니다/);
  assert.equal(h.find(node => node.type === "table"), undefined);
  assert.equal(h.find(node => node.type === "app-shell"), undefined);
});

test("directory 503 shows its error without invented usage values", async t => {
  const h = await harness(t, { api: { getAdminUsers: c => { throw new c.ApiError(503, "인증 디렉터리를 불러올 수 없습니다."); } } });
  await h.flush();
  assert.match(h.text(), /인증 디렉터리를 불러올 수 없습니다/);
  assert.equal(h.find(node => node.type === "table"), undefined);
  assert.equal(h.find(node => node.type === "app-shell")?.props.breadcrumb, "사이트 관리");
});

test("unknown load failures use the approved site-management wording", async t => {
  const h = await harness(t, { api: { getAdminUsers: () => { throw { unavailable: true }; } } });
  await h.flush();
  assert.match(h.text(), /사이트 관리 정보를 불러오지 못했습니다\./);
  assert.equal(h.find(node => node.type === "app-shell")?.props.breadcrumb, "사이트 관리");
});

test("user mode preserves personal usage and expands each membership with role and capacity", async t => {
  const h = await harness(t); await h.flush();
  assert.equal(h.find(node => node.type === "app-shell")?.props.breadcrumb, "사이트 관리");
  const seg = h.find(node => String(node.props?.className).split(" ").includes("seg"));
  assert.ok(seg, "approved segmented view control exists");
  h.button("사용자", seg); h.button("워크스페이스", seg);
  const row = rowContaining(h, "alice@example.test");
  assert.match(textOf(row), /1\.0 GB/);
  assert.doesNotMatch(textOf(row), /4\.0 GB/, "membership totals must not replace personal usage");
  const toggle = elements(row).find(node => node.type === "button" && node.props["aria-expanded"] !== undefined);
  assert.ok(toggle, "user exposes an accessible membership expander");
  assert.equal(toggle.props["aria-expanded"], false);
  toggle.props.onClick(); h.render();
  const subrows = elements(h.render()).filter(node => String(node.props?.className).split(" ").includes("subrow"));
  assert.ok(subrows.length > 0);
  assert.match(textOf(subrows), /Alice 개인.*개인.*소유자.*1\.0 GB.*2\.0 GB/);
  assert.match(textOf(subrows), /검수 팀.*팀.*라벨러.*3\.0 GB.*5\.0 GB/);
  const opened = elements(rowContaining(h, "alice@example.test")).find(node => node.type === "button" && node.props["aria-expanded"] === true);
  assert.ok(opened); opened.props.onClick(); h.render();
  assert.equal(h.find(node => String(node.props?.className).split(" ").includes("subrow")), undefined);
});

test("workspace mode lists owners by email, physical usage, member count, and override marker", async t => {
  const h = await harness(t); await h.flush(); await showWorkspaces(h);
  const team = rowContaining(h, "검수 팀"), personal = rowContaining(h, "Alice 개인");
  assert.match(textOf(team), /검수 팀.*팀.*bob@example\.test.*3.*3\.0 GB.*5\.0 GB\s*\*/);
  assert.match(textOf(personal), /Alice 개인.*개인.*alice@example\.test.*1.*1\.0 GB.*2\.0 GB/);
  assert.doesNotMatch(textOf(personal), /\*/);
  const rows = elements(h.render()).filter(node => node.type === "tr" && /검수 팀|Alice 개인/.test(textOf(node)));
  assert.deepEqual(rows.map(row => textOf(row).includes("검수 팀") ? 82 : 71), [82, 71]);
  assert.ok(h.context.calls.some(([name]) => name === "getAdminWorkspaces"));
});

test("missing workspace owner and capacity values render em dashes", async t => {
  const h = await harness(t, { workspaces: [{ id: 95, kind: "team", name: "정보 없는 팀",
    owner_user_id: 999, member_count: null, bytes_used: null, limit_bytes: null, quota_override: false }] });
  await h.flush(); await showWorkspaces(h);
  const row = rowContaining(h, "정보 없는 팀");
  const cells = elements(row).filter(node => node.type === "td").map(textOf);
  assert.ok(cells.filter(value => value === "—").length >= 3, "unknown owner/usage/limit remain unknown");
  assert.doesNotMatch(textOf(row), /NaN|undefined|null|0 B/);
});

test("workspace quota dialog writes workspace ID and refreshes its canonical override", async t => {
  const h = await harness(t); await h.flush(); await showWorkspaces(h);
  h.button("변경", rowContaining(h, "검수 팀")).props.onClick(); h.render();
  const dialog = h.find(node => node.props?.role === "dialog");
  assert.match(textOf(dialog), /할당 용량 변경.*검수 팀/);
  assert.match(textOf(dialog), /현재 사용량.*3\.0 GB/);
  submitQuota(h, "7.5"); await h.flush();
  assert.deepEqual(h.context.calls.filter(([name]) => name === "updateAdminWorkspaceQuota"), [["updateAdminWorkspaceQuota", 82, 7.5 * GiB]]);
  assert.equal(h.context.calls.some(([name]) => name === "updateAdminUserQuota"), false);
  assert.equal(h.find(node => node.props?.role === "dialog"), undefined);
  assert.match(textOf(rowContaining(h, "검수 팀")), /7\.5 GB\s*\*/);
  assert.ok(h.context.calls.some(([name]) => name === "invalidateStorageQuotaCache"));
});

test("workspace quota failure keeps the draft and allows a retry", async t => {
  let attempts = 0;
  const h = await harness(t, { api: { updateAdminWorkspaceQuota: (id, limit, c) => {
    if (++attempts === 1) throw new c.ApiError(503, "한도 변경을 처리하지 못했습니다.");
    c.workspaces.find(row => row.id === id).limit_bytes = limit;
    return { workspace_id: id, limit_bytes: limit };
  } } });
  await h.flush(); await showWorkspaces(h);
  h.button("변경", rowContaining(h, "검수 팀")).props.onClick(); h.render();
  submitQuota(h, "8"); await h.flush();
  assert.match(h.text(), /한도 변경을 처리하지 못했습니다/);
  assert.equal(h.find(node => node.type === "input" && node.props.type === "number")?.props.value, "8");
  submitQuota(h, "8"); await h.flush();
  assert.equal(attempts, 2);
  assert.equal(h.find(node => node.props?.role === "dialog"), undefined);
});

for (const confirmed of [false, true]) {
  test(`team owner deletion refusal lists required transfers ${confirmed ? "after confirmation" : "during preflight"}`, async t => {
    const h = await harness(t, { api: { deleteAdminUser: (_id, confirmation, c) => {
      const context = c ?? confirmation;
      const isConfirmed = c ? confirmation : false;
      if (confirmed && !isConfirmed) throw new context.ApiError(409, "confirmation", deletionConfirmation());
      throw new context.ApiError(409, "team owner", { code: "admin-user-team-owner", workspaces: ownedTeams });
    } } });
    await h.flush(); selectUser(h, "alice@example.test");
    h.find(node => node.props?.["aria-label"] === "선택한 사용자 삭제").props.onClick(); await h.flush();
    if (confirmed) { h.button("1명 삭제").props.onClick(); await h.flush(); }
    const dialog = h.find(node => node.props?.role === "dialog" && /삭제할 수 없어요/.test(textOf(node)));
    assert.ok(dialog, "team ownership refusal is an accessible dialog");
    assert.match(textOf(dialog), /alice@example\.test.*아래 팀의 소유자예요.*소유권을 다른 멤버에게 넘겨야 삭제할 수 있어요/);
    assert.match(textOf(dialog), /검수 팀/); assert.match(textOf(dialog), /분석 팀/);
    assert.equal(h.context.calls.filter(([name]) => name === "deleteAdminUser").length, confirmed ? 2 : 1);
    h.button("확인", dialog).props.onClick(); await h.flush();
    assert.equal(h.find(node => node.props?.role === "dialog" && /삭제할 수 없어요/.test(textOf(node))), undefined);
    assert.equal(h.find(node => node.props?.["aria-label"] === "alice@example.test 선택")?.props["aria-checked"], true);
  });
}

test("late admin data cannot expose content after the page unmounts", async t => {
  const pending = deferred();
  const h = await harness(t, { api: { getAdminUsers: () => pending.promise } });
  await h.flush(); h.unmount(); pending.resolve({ users: userRows }); await h.flush();
  assert.equal(h.render(), null);
  assert.equal(h.context.updatesAfterUnmount, 0, "the page must discard late data before updating state");
});

test("admin workspace client uses global authenticated endpoints and preserves refusal details", async t => {
  const oldWindow = globalThis.window, oldFetch = globalThis.fetch;
  const calls = [];
  globalThis.window = { location: { origin: "https://annodock.test" }, activeWorkspaceId: 999, setTimeout, clearTimeout };
  globalThis.fetch = async (path, init) => {
    calls.push({ path, init });
    if (init.method === "DELETE") return Response.json({ detail: {
      code: "admin-user-team-owner", workspaces: ownedTeams,
    } }, { status: 409 });
    return Response.json(init.method === "PATCH"
      ? { workspace_id: 82, limit_bytes: 7 * GiB } : { workspaces: workspaceRows });
  };
  t.after(() => { globalThis.window = oldWindow; globalThis.fetch = oldFetch; });
  const bridge = moduleUrl(`
    export const getAuthSnapshot=()=>({accessToken:"admin-token",refreshToken:"refresh"});
    export const getActiveWorkspaceId=()=>window.activeWorkspaceId;
    export const refreshAuthSession=async()=>{throw new Error("unexpected refresh");};
  `);
  const source = await readFile(new URL("../src/api/client.ts", import.meta.url), "utf8");
  const javascript = ts.transpileModule(source, { compilerOptions: {
    module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022,
  } }).outputText.replace(/from "[^"]+"/g, `from "${bridge}"`);
  const api = await import(`${moduleUrl(javascript)}#${Math.random()}`);
  assert.equal(typeof api.getAdminWorkspaces, "function");
  assert.equal(typeof api.updateAdminWorkspaceQuota, "function");
  assert.deepEqual(await api.getAdminWorkspaces(), { workspaces: workspaceRows });
  window.activeWorkspaceId = 555;
  assert.deepEqual(await api.updateAdminWorkspaceQuota(82, 7 * GiB), { workspace_id: 82, limit_bytes: 7 * GiB });
  window.activeWorkspaceId = 444;
  await assert.rejects(api.deleteAdminUser(2), error => {
    assert.ok(error instanceof api.ApiError); assert.equal(error.status, 409);
    assert.equal(error.detail.code, "admin-user-team-owner");
    assert.deepEqual(error.detail.workspaces, ownedTeams); return true;
  });
  assert.deepEqual(calls.map(({ path, init }) => [path, init.method ?? "GET"]), [
    ["/api/admin/workspaces", "GET"], ["/api/admin/workspaces/82/quota", "PATCH"], ["/api/admin/users/2", "DELETE"],
  ]);
  assert.deepEqual(JSON.parse(calls[1].init.body), { limit_bytes: 7 * GiB });
  // The shared /api contract attaches the current workspace to every request;
  // the global admin operation still addresses workspace 82 through its URL.
  assert.deepEqual(calls.map(({ init }) => init.headers.get("X-Workspace-Id")), ["999", "555", "444"]);
  for (const { init } of calls) {
    assert.equal(init.headers.get("Authorization"), "Bearer admin-token");
  }
});
