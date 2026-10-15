import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import ts from "typescript";

const moduleUrl = (source) => `data:text/javascript;base64,${Buffer.from(source).toString("base64")}`;
const settle = () => new Promise((resolve) => setImmediate(resolve));
const sourceWorkspace = { id: 71, name: "원본 팀", kind: "team", role: "owner" };
const targetWorkspace = { id: 82, name: "받는 팀", kind: "team", role: "admin" };
const personalWorkspace = { id: 93, name: "나의 공간", kind: "personal", role: "owner" };
const restrictedWorkspaces = [
  { id: 104, name: "라벨러 팀", kind: "team", role: "editor" },
  { id: 115, name: "보기 팀", kind: "team", role: "viewer" },
];
const project = {
  id: 31, name: "도로 프로젝트", archived: false, dataset_count: 2,
  image_count: 0, annotation_count: 0, class_count: 0, classes: [],
  created_at: "2026-10-01T00:00:00Z", updated_at: "2026-10-01T00:00:00Z",
  datasets: [
    { id: 91, name: "정면", status: "ready", storage_bytes: 1024, image_count: 0,
      annotation_count: 0, class_count: 0, classes: [], is_merged: false,
      active_job: null, source_datasets: [], created_at: "2026-10-01T00:00:00Z" },
    { id: 92, name: "후면", status: "ready", storage_bytes: 2048, image_count: 0,
      annotation_count: 0, class_count: 0, classes: [], is_merged: false,
      active_job: null, source_datasets: [], created_at: "2026-10-01T00:00:00Z" },
  ],
};
const result = { project_id: 31, workspace_id: 82, moved_bytes: 2500 };

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

async function harness(t, { page = false, role = "owner", items, quota, transfer } = {}) {
  const key = `__projectTransfer${Math.random().toString(16).slice(2)}`;
  const active = { ...sourceWorkspace, role };
  const context = {
    workspace: { active, items: items ?? [active, targetWorkspace, personalWorkspace, ...restrictedWorkspaces] },
    session: { accessToken: "transfer-token", refreshToken: "refresh", user: { id: 1 } },
    calls: [], closed: 0, transferred: [], navigated: [],
  };
  class ApiError extends Error {
    constructor(status, message, detail) { super(message); this.status = status; this.detail = detail; }
  }
  context.ApiError = ApiError;
  context.call = async (name, args) => {
    context.calls.push([name, ...args]);
    if (name === "getProjects") return { items: [structuredClone(project)] };
    if (name === "getDatasetImages") return { items: [] };
    if (name === "getProjectClassImageCounts") return { items: [] };
    if (name === "getStorageQuota") return quota
      ? quota(...args) : { used_bytes: 4096, referenced_bytes: 0, limit_bytes: 12288 };
    if (name === "transferProject") return transfer ? transfer(...args) : result;
    if (name === "invalidateStorageQuotaCache") return;
    throw new Error(`unexpected API call ${name}`);
  };
  const originalWindow = globalThis.window, originalDocument = globalThis.document;
  const originalHTMLElement = globalThis.HTMLElement;
  const listeners = new Map();
  const browser = {
    activeElement: null,
    location: { pathname: "/projects", search: "?ws=71", hash: "", origin: "https://annodock.test" },
    setTimeout: () => 1, clearTimeout() {}, dispatchEvent() {},
    addEventListener(name, callback) {
      if (!listeners.has(name)) listeners.set(name, new Set());
      listeners.get(name).add(callback);
    },
    removeEventListener(name, callback) { listeners.get(name)?.delete(callback); },
  };
  globalThis.window = browser; globalThis.document = browser;
  globalThis.HTMLElement = class HTMLElement {};
  const frames = new Map(), effects = [];
  let current, cursor = 0;
  const changed = (old, next) => !old || !next || old.length !== next.length
    || next.some((value, index) => !Object.is(value, old[index]));
  const hooks = {
    useState(initial) {
      const frame = current, index = cursor++;
      if (!(index in frame.slots)) frame.slots[index] = typeof initial === "function" ? initial() : initial;
      return [frame.slots[index], (value) => {
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
    useId() { return `transfer-${cursor++}`; },
  };
  context.hooks = hooks;
  globalThis[key] = context;
  t.after(() => {
    frames.forEach((frame) => frame.cleanups.forEach((cleanup) => cleanup?.()));
    globalThis.window = originalWindow; globalThis.document = originalDocument;
    if (originalHTMLElement === undefined) delete globalThis.HTMLElement;
    else globalThis.HTMLElement = originalHTMLElement;
    delete globalThis[key];
  });
  const shared = moduleUrl(`
    const c=globalThis[${JSON.stringify(key)}];
    export const {useState,useRef,useEffect,useMemo,useCallback,useId}=c.hooks;
    export const jsx=(type,props,key)=>({type,props,key}),jsxs=jsx,Fragment="fragment";
    export const useWorkspace=()=>c.workspace,getWorkspaceSnapshot=()=>c.workspace;
    export const useAuthSession=()=>c.session,getAuthSnapshot=()=>c.session;
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
      let dependencyUrl;
      const names = bindings.replace(/[{}]/g, "").split(",").map((name) => name.trim()).filter(Boolean);
      if (["react", "react/jsx-runtime", "../store/workspace", "../store/auth"].includes(dependency)) dependencyUrl = shared;
      else if (dependency.includes("/api/")) dependencyUrl = moduleUrl(`
        const c=globalThis[${JSON.stringify(key)}];
        ${names.map((name) => name === "ApiError" ? "export const ApiError=c.ApiError;"
          : `export const ${name}=(...args)=>c.call(${JSON.stringify(name)},args);`).join("\n")}
      `);
      else if (dependency.includes("/components/") || dependency === "./Icon") dependencyUrl = moduleUrl(
        names.map((name) => name === "Icon" ? "export const Icon=()=>null;"
          : `export const ${name}=(props)=>({type:${JSON.stringify(name)},props});`).join("\n"),
      );
      else if (dependency === "./InviteLinkDialog") {
        dependencyUrl = await compile(new URL("./InviteLinkDialog.tsx", file));
      } else {
        assert.ok(dependency.startsWith("."), `unmapped dependency ${dependency}`);
        dependencyUrl = await compile(new URL(`${dependency}.ts`, file));
      }
      javascript = javascript.replace(statement, statement.replace(`"${dependency}"`, `"${dependencyUrl}"`));
    }
    const url = moduleUrl(javascript); cache.set(file.href, url); return url;
  }
  const loaded = await import(await compile(new URL(page
    ? "../src/pages/ProjectsPage.tsx" : "../src/components/ProjectTransferDialog.tsx", import.meta.url)));
  const props = page ? {} : {
    project, onClose: () => { context.closed++; },
    onTransferred: (...args) => context.transferred.push(args),
  };
  const evaluate = (component, componentProps, path) => {
    if (!frames.has(path)) frames.set(path, { slots: [], cleanups: new Map() });
    current = frames.get(path); cursor = 0;
    return expand(component(componentProps), path);
  };
  const expand = (node, path) => {
    if (Array.isArray(node)) return node.map((child, index) => expand(child, `${path}/${child?.key ?? index}`));
    if (!node || typeof node !== "object") return node;
    if (typeof node.type === "function") return evaluate(node.type, node.props, `${path}:${node.type.name}`);
    return { ...node, props: { ...node.props, children: expand(node.props?.children, `${path}/children`) } };
  };
  const render = () => {
    const tree = evaluate(loaded[page ? "ProjectsPage" : "ProjectTransferDialog"], props, "root");
    effects.splice(0).forEach((effect) => effect());
    return tree;
  };
  const flush = async () => { for (let turn = 0; turn < 4; turn++) { render(); await settle(); } };
  await flush();
  return {
    context, ApiError, render, flush, text: () => textOf(render()),
    find: (predicate) => elements(render()).find(predicate),
    button: (label) => elements(render()).find((node) => node.type === "button" && textOf(node).trim() === label),
    select(id) {
      const control = elements(render()).find((node) => node.type === "select");
      assert.ok(control, "target workspace select exists");
      control.props.onChange({ target: { value: String(id) } });
    },
    submit() {
      const form = elements(render()).find((node) => node.type === "form");
      assert.ok(form, "dialog exposes its transfer form");
      return form.props.onSubmit({ preventDefault() {} });
    },
    key(key) { for (const callback of listeners.get("keydown") ?? []) callback({ key, preventDefault() {} }); },
  };
}

test("transfer dialog filters destinations by membership and shows scoped quota and estimated bytes", async (t) => {
  const view = await harness(t);
  const options = elements(view.render()).filter((node) => node.type === "option" && Number(node.props.value) > 0);
  assert.deepEqual(options.map((node) => Number(node.props.value)).sort((a, b) => a - b), [82, 93]);
  assert.match(textOf(options.find((node) => Number(node.props.value) === 82)), /받는 팀.*관리자/);
  assert.match(textOf(options.find((node) => Number(node.props.value) === 93)), /나의 공간.*소유자/);
  assert.match(view.text(), /원본 팀/);
  assert.match(view.text(), /약\s*3(?:\.0)?\s*KB/);
  assert.match(view.text(), /8(?:\.0)?\s*KB/);
  assert.match(view.text(), /복사가 아니라 이동이에요.*원본 팀.*사라져요/);
  assert.match(view.text(), /받는 팀.*멤버가 역할에 따라 보고 고칠 수 있어요/);
  assert.deepEqual(view.context.calls.filter(([name]) => name === "getStorageQuota"), [["getStorageQuota", "transfer-token", 82]]);
  assert.equal(Boolean(view.button("옮기기")?.props.disabled), false);
});

test("a late quota response cannot replace the selected destination's remaining capacity", async (t) => {
  const first = deferred(), second = deferred();
  const view = await harness(t, { quota: (_token, id) => id === 82 ? first.promise : second.promise });
  view.select(93); await view.flush();
  second.resolve({ used_bytes: 1024, referenced_bytes: 0, limit_bytes: 5120 }); await view.flush();
  assert.match(view.text(), /4(?:\.0)?\s*KB/);
  first.resolve({ used_bytes: 0, referenced_bytes: 0, limit_bytes: 1048576 }); await view.flush();
  assert.match(view.text(), /4(?:\.0)?\s*KB/);
  assert.doesNotMatch(view.text(), /1(?:\.0)?\s*MB/);
  assert.deepEqual(view.context.calls.filter(([name]) => name === "getStorageQuota").map((call) => call[2]), [82, 93]);
});

test("busy transfer is single-submit, ignores Escape, and reports the server's exact result", async (t) => {
  const pending = deferred();
  const view = await harness(t, { transfer: () => pending.promise });
  const first = view.submit();
  const second = view.submit();
  view.key("Escape");
  assert.equal(view.context.closed, 0);
  assert.deepEqual(view.context.calls.filter(([name]) => name === "transferProject"), [["transferProject", 31, 82, 71]]);
  assert.equal(view.button("취소").props.disabled, true);
  assert.equal(view.find((node) => node.type === "select").props.disabled, true);
  pending.resolve(result);
  await Promise.all([first, second]); await view.flush();
  assert.deepEqual(view.context.transferred, [[result, targetWorkspace]]);
});

const failures = [
  [409, "transfer_shared_media", "옮길 수 없어요 — 다른 데이터셋과 같은 이미지를 함께 쓰고 있어요.", { datasets: [{ id: 201, name: "공유 정면" }, { id: 202, name: "공유 후면" }] }, ["공유 정면", "공유 후면"]],
  [409, "transfer_upload_active", "업로드 중인 데이터셋이 있어요. 끝난 뒤 다시 시도하세요.", { names: ["업로드 정면", "업로드 후면"] }, ["업로드 정면", "업로드 후면"]],
  [409, "transfer_run_active", "학습이 진행 중이에요. 끝나거나 취소한 뒤 다시 시도하세요.", { names: ["학습 정면", "학습 후면"] }, ["학습 정면", "학습 후면"]],
  [409, "transfer_name_conflict", "받는 팀에 같은 이름이 있어요. 이름을 바꾼 뒤 다시 시도하세요.", { names: ["프로젝트 중복", "데이터셋 중복"] }, ["프로젝트 중복", "데이터셋 중복"]],
  [409, "transfer_same_workspace", "같은 워크스페이스로는 옮길 수 없어요.", {}, []],
  [413, "transfer_quota_exceeded", "받는 팀 용량이 부족해요.", { required_bytes: 2500, remaining_bytes: 1 }, []],
];
for (const [status, code, message, detail, names] of failures) {
  test(`${code} preserves the dialog and displays every affected name`, async (t) => {
    let failure;
    const view = await harness(t, { transfer: async () => { throw failure; } });
    failure = new view.ApiError(status, "server transfer refusal", { code, ...detail });
    await view.submit(); await view.flush();
    const banner = view.find((node) => node.props?.role === "alert" && node.props.className?.includes("banner-danger"));
    assert.ok(banner, "refusal appears in the danger banner");
    assert.ok(textOf(banner).includes(message), textOf(banner));
    for (const name of names) assert.ok(textOf(banner).includes(name), `missing affected name ${name}`);
    if (names.length) {
      const rows = elements(banner).filter((node) => ["li", "p"].includes(node.type));
      for (const name of names) assert.ok(rows.some((row) => textOf(row) === name), `${name} has its own row`);
    }
    assert.equal(view.context.closed, 0);
    assert.deepEqual(view.context.transferred, []);
    assert.equal(Boolean(view.button("옮기기").props.disabled), false);
    view.key("Escape");
    assert.equal(view.context.closed, 1);
  });
}

for (const role of ["owner", "admin", "editor", "viewer"]) {
  test(`ProjectsPage ${role} exposes project transfer only to the workspace owner`, async (t) => {
    const view = await harness(t, { page: true, role });
    const menu = view.find((node) => node.props?.["aria-label"] === "도로 프로젝트 메뉴");
    menu?.props.onClick();
    const transfer = view.button("프로젝트 옮기기");
    assert.equal(Boolean(transfer), role === "owner");
    if (transfer) {
      transfer.props.onClick();
      const dialog = view.find((node) => node.type === "ProjectTransferDialog");
      assert.equal(dialog.props.project.id, 31);
    }
  });
}

test("owner transfer menu is disabled with an explanation when no eligible destination exists", async (t) => {
  const view = await harness(t, { page: true, items: [sourceWorkspace, ...restrictedWorkspaces] });
  view.find((node) => node.props?.["aria-label"] === "도로 프로젝트 메뉴").props.onClick();
  const transfer = view.button("프로젝트 옮기기");
  assert.ok(transfer);
  assert.equal(transfer.props.disabled, true);
  assert.equal(transfer.props.title, "옮길 수 있는 팀이 없어요");
});

test("successful transfer removes the source project and links directly to the destination workspace", async (t) => {
  const view = await harness(t, { page: true });
  view.find((node) => node.props?.["aria-label"] === "도로 프로젝트 메뉴").props.onClick();
  view.button("프로젝트 옮기기").props.onClick();
  const dialog = view.find((node) => node.type === "ProjectTransferDialog");
  await dialog.props.onTransferred(result, targetWorkspace); await view.flush();
  assert.equal(view.find((node) => node.type === "ProjectTransferDialog"), undefined);
  assert.equal(view.find((node) => node.props?.["aria-label"] === "도로 프로젝트 메뉴"), undefined);
  assert.match(view.text(), /받는 팀으로 옮겼어요/);
  const link = view.find((node) => node.type === "a" && textOf(node).includes("받는 팀에서 보기"));
  assert.ok(link);
  const href = new URL(link.props.href, "https://annodock.test");
  assert.equal(href.pathname, "/projects");
  assert.equal(href.searchParams.get("ws"), "82");
});

test("transfer client pins the source header while target quota uses its own workspace and cache entry", async (t) => {
  const originalWindow = globalThis.window, originalFetch = globalThis.fetch;
  const calls = [];
  globalThis.window = { location: { origin: "https://annodock.test" }, setTimeout, clearTimeout };
  globalThis.fetch = async (path, init) => {
    calls.push({ path, init });
    return Response.json(path === "/api/storage"
      ? { used_bytes: 10, referenced_bytes: 0, limit_bytes: 100 } : result);
  };
  t.after(() => { globalThis.window = originalWindow; globalThis.fetch = originalFetch; });
  const dependencies = moduleUrl(`
    export const getAuthSnapshot=()=>({accessToken:"transfer-token",refreshToken:"refresh"});
    export const getActiveWorkspaceId=()=>999;
    export const refreshAuthSession=async()=>{throw new Error("unexpected refresh");};
  `);
  const source = await readFile(new URL("../src/api/client.ts", import.meta.url), "utf8");
  const javascript = ts.transpileModule(source, { compilerOptions: {
    module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022,
  } }).outputText.replace(/from "[^"]+"/g, `from "${dependencies}"`);
  const api = await import(`${moduleUrl(javascript)}#${Math.random()}`);
  assert.deepEqual(await api.transferProject(31, 82, 71), result);
  await api.getStorageQuota("transfer-token", 82);
  await api.getStorageQuota("transfer-token", 82);
  await api.getStorageQuota("transfer-token", 93);
  assert.deepEqual(calls.map(({ path, init }) => [path, init.method ?? "GET", init.headers.get("X-Workspace-Id")]), [
    ["/api/projects/31/transfer", "POST", "71"],
    ["/api/storage", "GET", "82"],
    ["/api/storage", "GET", "93"],
  ]);
  assert.deepEqual(JSON.parse(calls[0].init.body), { target_workspace_id: 82 });
});
