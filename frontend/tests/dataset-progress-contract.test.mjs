import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import ts from "typescript";

const moduleUrl = (source) => `data:text/javascript;base64,${Buffer.from(source).toString("base64")}`;
const settle = () => new Promise((resolve) => setImmediate(resolve));
const roles = { owner: "소유자", admin: "관리자", editor: "라벨러" };
const members = [
  { user_id: 11, username: "민수", role: "owner" },
  { user_id: 12, username: "지민", role: "admin" },
  { user_id: 13, username: "서연", role: "editor" },
  { user_id: 14, username: "가람", role: "viewer" },
].map((row) => ({ ...row, email: `${row.user_id}@example.test`, joined_at: "2026-10-01T00:00:00Z" }));
const initialProgress = {
  total: 20, unlabeled: 7, in_progress: 8, labeled: 5, imported_labeled: 2,
  members: [{ user_id: 11, in_progress: 5, labeled: 2 }, { user_id: 12, in_progress: 3, labeled: 1 }],
};

function elements(node) {
  if (!node || typeof node !== "object") return [];
  return [node, ...[node.props?.children].flat(Infinity).flatMap(elements)];
}
function textOf(node) {
  if (typeof node === "string" || typeof node === "number") return String(node);
  if (!node || typeof node !== "object") return "";
  return [node.props?.children].flat(Infinity).map(textOf).join("");
}
const hasClass = (node, name) => node.props?.className?.split(/\s+/).includes(name);
function renderChildren(node) {
  if (Array.isArray(node)) return node.map(renderChildren);
  if (!node || typeof node !== "object") return node;
  if (typeof node.type === "function") return renderChildren(node.type(node.props));
  return { ...node, props: { ...node.props, children: renderChildren(node.props?.children) } };
}

async function harness(t, { canManage = true, progress = initialProgress } = {}) {
  const key = `__datasetProgress${Math.random().toString(16).slice(2)}`;
  const state = {
    calls: [], changed: 0, closed: 0, progress: structuredClone(progress),
    getDatasetProgress: async () => structuredClone(state.progress),
    getWorkspaceMembers: async () => ({ items: structuredClone(members) }),
    distributeDatasetAssignments: async () => ({ assigned: {} }),
    reclaimDatasetAssignments: async () => ({ reclaimed: 0 }),
  };
  const originalWindow = globalThis.window, originalDocument = globalThis.document;
  const originalHTMLElement = globalThis.HTMLElement;
  class Element { isConnected = true; focus() {} }
  globalThis.HTMLElement = Element;
  const listeners = new Map();
  const browser = {
    activeElement: new Element(), setTimeout, clearTimeout,
    addEventListener(name, callback) {
      if (!listeners.has(name)) listeners.set(name, new Set());
      listeners.get(name).add(callback);
    },
    removeEventListener(name, callback) { listeners.get(name)?.delete(callback); },
  };
  globalThis.window = browser;
  globalThis.document = browser;
  const slots = [], effects = [], cleanups = new Map();
  let cursor = 0;
  const changedDeps = (previous, next) => !previous || !next || previous.length !== next.length
    || next.some((value, index) => !Object.is(value, previous[index]));
  const hooks = {
    useState(initial) {
      const index = cursor++;
      if (!(index in slots)) slots[index] = typeof initial === "function" ? initial() : initial;
      return [slots[index], (value) => { slots[index] = typeof value === "function" ? value(slots[index]) : value; }];
    },
    useRef(current) { const index = cursor++; return slots[index] ??= { current }; },
    useEffect(callback, deps) {
      const index = cursor++;
      if (changedDeps(slots[index]?.deps, deps)) effects.push(() => {
        cleanups.get(index)?.(); slots[index] = { deps }; cleanups.set(index, callback());
      });
    },
    useMemo(factory, deps) {
      const index = cursor++;
      if (changedDeps(slots[index]?.deps, deps)) slots[index] = { value: factory(), deps };
      return slots[index].value;
    },
    useCallback(callback, deps) { return hooks.useMemo(() => callback, deps); },
    useId() { return `progress-${cursor++}`; },
  };
  globalThis[key] = { state, hooks };
  const unmount = () => {
    cleanups.forEach((cleanup) => cleanup?.());
    cleanups.clear();
  };
  t.after(() => {
    unmount(); globalThis.window = originalWindow; globalThis.document = originalDocument;
    globalThis.HTMLElement = originalHTMLElement;
    delete globalThis[key];
  });
  const stub = moduleUrl(`
    const {state,hooks} = globalThis[${JSON.stringify(key)}];
    export const {useState,useRef,useEffect,useMemo,useCallback,useId} = hooks;
    export const jsx = (type,props) => ({type,props}), jsxs = jsx, Fragment = 'fragment';
    export class ApiError extends Error { constructor(status,message,detail) { super(message); this.status=status; this.detail=detail; } }
    export const WORKSPACE_ROLE_LABELS = ${JSON.stringify(roles)};
    export const Icon = () => null, WorkspaceDialog = ({children}) => jsx('dialog',{children});
    const call = (name,args) => { state.calls.push([name,...args]); return state[name](...args); };
    export const getDatasetProgress = (...args) => call('getDatasetProgress',args);
    export const getWorkspaceMembers = (...args) => call('getWorkspaceMembers',args);
    export const distributeDatasetAssignments = (...args) => call('distributeDatasetAssignments',args);
    export const reclaimDatasetAssignments = (...args) => call('reclaimDatasetAssignments',args);
  `);
  const dependencies = {
    react: stub, "react/jsx-runtime": stub, "../api/client": stub, "../api/workspaces": stub,
    "../store/workspace": stub, "./Icon": stub, "./InviteLinkDialog": stub,
  };
  const source = await readFile(new URL("../src/components/DatasetProgressPanel.tsx", import.meta.url), "utf8");
  const compiled = ts.transpileModule(source, { compilerOptions: {
    module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX,
  } }).outputText.replace(/from "([^"]+)"/g, (_match, name) => {
    assert.ok(dependencies[name], `unmapped panel dependency: ${name}`);
    return `from "${dependencies[name]}"`;
  });
  const { DatasetProgressPanel } = await import(moduleUrl(compiled));
  const props = { dataset: { id: 91, name: "도로 표지판" }, workspaceId: 71, canManage,
    onClose: () => { state.closed++; }, onChanged: () => { state.changed++; } };
  const render = () => {
    cursor = 0;
    const tree = DatasetProgressPanel(props);
    effects.splice(0).forEach((effect) => effect());
    return renderChildren(tree);
  };
  const flush = async () => {
    for (let turn = 0; turn < 3; turn++) { render(); await settle(); }
  };
  const button = (name) => elements(render()).find((node) => node.type === "button" && textOf(node).trim() === name);
  const checkbox = (name) => {
    const label = elements(render()).find((node) => node.type === "label" && textOf(node).includes(name));
    return elements(label).find((node) => node.type === "input" && node.props.type === "checkbox");
  };
  render(); await settle();
  return { state, props, render, button, checkbox, unmount, flush,
    text: () => textOf(render()),
    row: (name) => elements(render()).find((node) => node.type === "tr" && textOf(node).includes(name)),
    escape() { listeners.get("keydown")?.forEach((callback) => callback({ key: "Escape", preventDefault() {} })); },
  };
}

for (const role of ["viewer", "editor"]) {
  test(`${role} sees server progress and imported labels without assignment controls`, async (t) => {
    const view = await harness(t, { canManage: false });
    assert.ok(view.state.calls.some((call) => JSON.stringify(call) === JSON.stringify(["getDatasetProgress", 91, 71])));
    assert.ok(view.state.calls.some((call) => JSON.stringify(call) === JSON.stringify(["getWorkspaceMembers", 71])));
    assert.match(view.text(), /도로 표지판/);
    const stats = elements(view.render()).find((node) => hasClass(node, "stat-row"));
    assert.ok(stats);
    for (const pattern of [/전체\s*20/, /완료\s*5/, /작업 중\s*8/, /남음\s*7/, /가져온 라벨\s*2\s*포함/]) {
      assert.match(textOf(stats), pattern);
    }
    assert.ok(view.row("민수"));
    assert.match(textOf(view.row("민수")), /민수.*5.*2/);
    assert.match(textOf(view.row("지민")), /지민.*3.*1/);
    const imported = view.row("가져온 라벨");
    assert.ok(imported);
    assert.match(textOf(imported), /가져온 라벨.*—.*2/);
    assert.equal(elements(imported).filter((node) => node.type === "button").length, 0);
    assert.equal(view.button("배정하기"), undefined);
    assert.equal(view.button("회수"), undefined);
    assert.equal(elements(view.render()).filter((node) => node.props?.type === "checkbox").length, 0);
  });
}

test("managers select only editor-or-higher members and preview the ceiling per person", async (t) => {
  const view = await harness(t);
  assert.match(view.text(), /남은\s*7장을 나눠 맡기기/);
  assert.equal(view.button("배정하기").props.disabled, true);
  assert.doesNotMatch(view.text(), /1인당 약/);
  for (const name of ["민수", "지민", "서연"]) {
    assert.ok(view.checkbox(name));
    assert.equal(Boolean(view.checkbox(name).props.disabled), false);
    assert.equal(Boolean(view.checkbox(name).props.checked), false);
  }
  const hiddenViewer = view.checkbox("가람");
  assert.ok(!hiddenViewer || hiddenViewer.props.disabled, "historical viewers must not be selectable for assignments");
  assert.doesNotMatch(view.text(), /보기 전용|편집자/);
  view.checkbox("민수").props.onChange({ target: { checked: true } });
  view.checkbox("서연").props.onChange({ target: { checked: true } });
  assert.match(view.text(), /선택\s*2명.*1인당 약\s*4장/);
  assert.equal(Boolean(view.button("배정하기").props.disabled), false);
  view.checkbox("민수").props.onChange({ target: { checked: false } });
  view.checkbox("서연").props.onChange({ target: { checked: false } });
  assert.equal(view.button("배정하기").props.disabled, true);
  assert.doesNotMatch(view.text(), /1인당 약/);
  assert.equal(view.state.calls.filter(([name]) => name === "distributeDatasetAssignments").length, 0);
});

test("distribution shows actual server counts and refreshes the original dataset and workspace", async (t) => {
  const view = await harness(t);
  let finish;
  const pending = new Promise((resolve) => { finish = resolve; });
  view.state.distributeDatasetAssignments = () => pending;
  view.checkbox("민수").props.onChange({ target: { checked: true } });
  view.checkbox("서연").props.onChange({ target: { checked: true } });
  assert.match(view.text(), /1인당 약\s*4장/);
  view.button("배정하기").props.onClick();
  await settle();
  assert.deepEqual(view.state.calls.filter(([name]) => name === "distributeDatasetAssignments"), [
    ["distributeDatasetAssignments", 91, [11, 13], 71],
  ]);
  assert.equal(view.button("배정하기").props.disabled, true);
  assert.equal(view.state.changed, 0);
  view.state.progress = { ...initialProgress, unlabeled: 3, in_progress: 12,
    members: [{ user_id: 11, in_progress: 6, labeled: 2 }, { user_id: 12, in_progress: 3, labeled: 1 },
      { user_id: 13, in_progress: 3, labeled: 0 }] };
  finish({ assigned: { 11: 1, 13: 3 } });
  await view.flush();
  assert.match(view.text(), /민수\s*1장.*서연\s*3장.*배정했어요/);
  assert.match(view.text(), /남은\s*3장을 나눠 맡기기/);
  assert.equal(view.state.changed, 1);
  assert.equal(view.state.calls.filter(([name]) => name === "getDatasetProgress").length, 2);
  assert.ok(view.state.calls.filter(([name]) => name === "getDatasetProgress")
    .every((call) => call[1] === 91 && call[2] === 71));
});

test("reclaim requires confirmation, supports cancellation, and refreshes member counts", async (t) => {
  const view = await harness(t);
  const reclaim = () => elements(view.row("민수")).find((node) => node.type === "button" && textOf(node) === "회수");
  reclaim().props.onClick();
  assert.ok(view.button("취소"));
  assert.ok(view.button("확인"));
  assert.equal(view.state.calls.filter(([name]) => name === "reclaimDatasetAssignments").length, 0);
  view.button("취소").props.onClick();
  assert.equal(view.button("확인"), undefined);
  assert.equal(view.state.calls.filter(([name]) => name === "reclaimDatasetAssignments").length, 0);
  view.state.reclaimDatasetAssignments = async () => {
    view.state.progress = { ...initialProgress, unlabeled: 12, in_progress: 3,
      members: [{ user_id: 11, in_progress: 0, labeled: 2 }, { user_id: 12, in_progress: 3, labeled: 1 }] };
    return { reclaimed: 5 };
  };
  reclaim().props.onClick();
  view.button("확인").props.onClick();
  await view.flush();
  assert.deepEqual(view.state.calls.filter(([name]) => name === "reclaimDatasetAssignments"), [
    ["reclaimDatasetAssignments", 91, 11, 71],
  ]);
  assert.match(textOf(view.row("민수")), /민수.*0.*2/);
  assert.match(view.text(), /남은\s*12장을 나눠 맡기기/);
  assert.equal(view.state.changed, 1);
  assert.equal(view.state.calls.filter(([name]) => name === "getDatasetProgress").length, 2);
});

test("zero totals render finite zero proportions and keep the empty selection disabled", async (t) => {
  const view = await harness(t, { progress: { total: 0, unlabeled: 0, in_progress: 0,
    labeled: 0, imported_labeled: 0, members: [] } });
  const bar = elements(view.render()).find((node) => hasClass(node, "bar-stack"));
  assert.ok(bar);
  assert.ok(bar.props["aria-label"]);
  assert.doesNotMatch(String(bar.props["aria-label"]), /NaN|Infinity/);
  const segments = elements(bar).filter((node) => hasClass(node, "is-done") || hasClass(node, "is-active"));
  assert.equal(segments.length, 2);
  for (const segment of segments) assert.match(String(segment.props.style.width), /^0(?:\.0+)?%?$/);
  assert.match(view.text(), /남은\s*0장을 나눠 맡기기/);
  assert.equal(view.button("배정하기").props.disabled, true);
  assert.doesNotMatch(view.text(), /1인당 약|NaN|Infinity/);
});

test("close control and Escape close the panel and unmount removes the listener", async (t) => {
  const view = await harness(t);
  const close = elements(view.render()).find((node) => node.type === "button"
    && /닫기/.test(node.props["aria-label"] ?? textOf(node)));
  assert.ok(close);
  close.props.onClick();
  assert.equal(view.state.closed, 1);
  view.escape();
  assert.equal(view.state.closed, 2);
  view.unmount();
  view.escape();
  assert.equal(view.state.closed, 2);
});
