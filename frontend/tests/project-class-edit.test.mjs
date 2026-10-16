import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import ts from "typescript";

const moduleUrl = source => `data:text/javascript;base64,${Buffer.from(source).toString("base64")}`;
const originalClasses = [
  { class_id: 0, name: "person", color: "#ef4444" },
  { class_id: 1, name: "vehicle", color: "#3b82f6" },
];
const revision = "2026-10-15T08:00:00Z";
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

// Export the existing local dialog only in the compiled test module. Its
// handlers and the real color picker run unchanged; API calls form the seam.
async function harness(t, { classes = originalClasses, save } = {}) {
  const key = `__projectClassEdit${Math.random().toString(16).slice(2)}`;
  const context = { calls: [], closed: 0, saved: 0 };
  context.call = async (name, args) => {
    assert.equal(name, "updateProjectClasses", "editing uses the atomic catalog endpoint");
    context.calls.push(structuredClone(args));
    return save ? save(...args) : { classes: [], catalog_versions: [] };
  };
  const project = { id: 31, name: "도로 프로젝트", updated_at: revision, classes: structuredClone(classes) };
  const previousWindow = globalThis.window, previousDocument = globalThis.document;
  const listeners = new Map();
  const browser = {
    addEventListener(name, listener) {
      if (!listeners.has(name)) listeners.set(name, new Set());
      listeners.get(name).add(listener);
    },
    removeEventListener(name, listener) { listeners.get(name)?.delete(listener); },
  };
  globalThis.window = browser; globalThis.document = browser;
  const frames = new Map(), effects = [];
  let current, cursor = 0;
  const changed = (before, next) => !before || !next || before.length !== next.length
    || next.some((value, index) => !Object.is(value, before[index]));
  const hooks = {
    useState(initial) {
      const frame = current, index = cursor++;
      if (!(index in frame.slots)) frame.slots[index] = typeof initial === "function" ? initial() : initial;
      return [frame.slots[index], value => {
        frame.slots[index] = typeof value === "function" ? value(frame.slots[index]) : value;
      }];
    },
    useRef(value) { return current.slots[cursor++] ??= { current: value }; },
    useEffect(callback, deps) {
      const frame = current, index = cursor++;
      if (changed(frame.slots[index]?.deps, deps)) effects.push(() => {
        frame.cleanups.get(index)?.(); frame.slots[index] = { deps };
        frame.cleanups.set(index, callback());
      });
    },
    useMemo(factory, deps) {
      const index = cursor++;
      if (changed(current.slots[index]?.deps, deps)) current.slots[index] = { value: factory(), deps };
      return current.slots[index].value;
    },
    useCallback(callback, deps) { return hooks.useMemo(() => callback, deps); },
  };
  context.hooks = hooks; globalThis[key] = context;
  t.after(() => {
    frames.forEach(frame => frame.cleanups.forEach(cleanup => cleanup?.()));
    globalThis.window = previousWindow; globalThis.document = previousDocument;
    delete globalThis[key];
  });
  const shared = moduleUrl(`
    const c=globalThis[${JSON.stringify(key)}];
    export const {useState,useRef,useEffect,useMemo,useCallback}=c.hooks;
    export const jsx=(type,props,key)=>({type,props,key}),jsxs=jsx,Fragment="fragment";
    export const useWorkspace=()=>({active:{id:71,role:"owner"},items:[]});
  `);
  const cache = new Map();
  async function compile(file) {
    if (cache.has(file.href)) return cache.get(file.href);
    let source = await readFile(file, "utf8");
    if (file.pathname.endsWith("/ProjectsPage.tsx")) source += "\nexport { EditProjectClassesDialog };\n";
    let javascript = ts.transpileModule(source, { compilerOptions: {
      module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX,
    } }).outputText;
    for (const match of [...javascript.matchAll(/import\s+([^;]+?)\s+from\s+"([^"]+)";/g)]) {
      const [statement, bindings, dependency] = match;
      const names = bindings.replace(/[{}]/g, "").split(",").map(name => name.trim()).filter(Boolean);
      let url;
      if (["react", "react/jsx-runtime", "../store/workspace"].includes(dependency)) url = shared;
      else if (dependency.includes("/api/")) url = moduleUrl(`
        const c=globalThis[${JSON.stringify(key)}];
        ${names.map(name => name === "ApiError" ? "export class ApiError extends Error {}"
          : `export const ${name}=(...args)=>c.call(${JSON.stringify(name)},args);`).join("\n")}
      `);
      else if (dependency.endsWith("/ClassColorPicker")) url = await compile(new URL(`${dependency}.tsx`, file));
      else if (dependency.includes("/components/")) url = moduleUrl(names.map(name =>
        `export const ${name}=()=>null;`).join("\n"));
      else if (dependency === "../navigation") url = moduleUrl("export const appHref=path=>path,navigate=()=>{};");
      else {
        assert.ok(dependency.startsWith("."), `unmapped dependency ${dependency}`);
        url = await compile(new URL(`${dependency}.ts`, file));
      }
      javascript = javascript.replace(statement, statement.replace(`"${dependency}"`, `"${url}"`));
    }
    const url = moduleUrl(javascript); cache.set(file.href, url); return url;
  }
  const { EditProjectClassesDialog } = await import(await compile(new URL("../src/pages/ProjectsPage.tsx", import.meta.url)));
  let seen;
  const expand = (node, path) => {
    if (Array.isArray(node)) return node.map((child, index) => expand(child, `${path}/${child?.key ?? index}`));
    if (!node || typeof node !== "object") return node;
    if (typeof node.type === "function") return evaluate(node.type, node.props, `${path}:${node.type.name}:${node.key ?? ""}`);
    return { ...node, props: { ...node.props, children: expand(node.props?.children, `${path}/children`) } };
  };
  const evaluate = (component, props, path) => {
    if (!frames.has(path)) frames.set(path, { slots: [], cleanups: new Map() });
    seen.add(path); current = frames.get(path); cursor = 0;
    return expand(component(props), path);
  };
  const render = () => {
    seen = new Set();
    const tree = evaluate(EditProjectClassesDialog, {
      project, onClose: () => { context.closed++; }, onSaved: () => { context.saved++; },
    }, "root");
    for (const [path, frame] of frames) if (!seen.has(path)) {
      frame.cleanups.forEach(cleanup => cleanup?.()); frames.delete(path);
    }
    effects.splice(0).forEach(effect => effect()); return tree;
  };
  const find = predicate => elements(render()).find(predicate);
  const button = label => {
    const result = find(node => node.type === "button" && textOf(node).trim() === label);
    assert.ok(result, `button '${label}' exists`); return result;
  };
  const inputs = () => elements(render()).filter(node => node.type === "input" && /클래스 이름$/.test(node.props?.["aria-label"]));
  const flush = async () => { for (let i = 0; i < 3; i++) { render(); await new Promise(resolve => setImmediate(resolve)); } };
  render();
  return {
    context, project, render, find, button, inputs, flush,
    add() { const control = button("클래스 추가"); assert.equal(Boolean(control.props.disabled), false); control.props.onClick(); render(); },
    name(index, value) { const input = inputs()[index]; assert.ok(input); input.props.onChange({ target: { value } }); render(); },
    color(index, hex) {
      const row = elements(render()).filter(node => String(node.props?.className).split(" ").includes("class-editor-row"))[index];
      assert.ok(row);
      const swatch = elements(row).find(node => node.type === "button" && node.props["aria-haspopup"] === "dialog");
      assert.ok(swatch); swatch.props.onClick(); render();
      const preset = find(node => node.type === "button" && node.props["aria-label"]?.includes(`${hex.toUpperCase()} 색상`));
      assert.ok(preset, "the real color picker offers the requested preset");
      preset.props.onClick(); button("완료").props.onClick(); render();
    },
    key(key) { for (const handler of listeners.get("keydown") ?? []) handler({ key }); },
    text() { return textOf(render()); },
  };
}

test("adding a class preserves existing IDs and saves its trimmed name and selected color", async t => {
  const h = await harness(t);
  assert.equal(h.button("저장").props.disabled, true);
  h.add(); assert.equal(h.inputs().length, 3);
  assert.equal(h.button("저장").props.disabled, true, "an unfinished new class cannot be saved");
  h.name(2, "  bicycle  "); h.color(2, "#22c55e");
  assert.equal(h.button("저장").props.disabled, false);
  h.button("저장").props.onClick(); await h.flush();
  assert.deepEqual(h.context.calls, [[31, revision, [
    ...originalClasses, { class_id: null, name: "bicycle", color: "#22c55e" },
  ]]]);
  assert.equal(h.context.saved, 1);
  assert.deepEqual(h.project.classes, originalClasses, "draft edits must not mutate the loaded catalog");
});

test("an empty catalog can save its first class", async t => {
  const h = await harness(t, { classes: [] });
  assert.equal(h.inputs().length, 0);
  h.add(); h.name(0, "person"); h.color(0, "#8b5cf6");
  h.button("저장").props.onClick(); await h.flush();
  assert.deepEqual(h.context.calls, [[31, revision, [{ class_id: null, name: "person", color: "#8b5cf6" }]]]);
  assert.equal(h.context.saved, 1);
});

test("multiple additions keep independent name, color, and null server identities", async t => {
  const h = await harness(t);
  h.add(); h.add(); h.name(2, "bicycle"); h.name(3, "traffic light");
  h.color(2, "#22c55e"); h.color(3, "#ec4899");
  assert.deepEqual(h.inputs().map(input => input.props.value), ["person", "vehicle", "bicycle", "traffic light"]);
  h.button("저장").props.onClick(); await h.flush();
  assert.deepEqual(h.context.calls[0][2], [...originalClasses,
    { class_id: null, name: "bicycle", color: "#22c55e" },
    { class_id: null, name: "traffic light", color: "#ec4899" },
  ]);
});

for (const cancel of ["취소", "Escape"]) {
  test(`${cancel} abandons appended and renamed drafts without any API write`, async t => {
    const h = await harness(t);
    h.add(); h.name(2, "bicycle"); h.name(0, "pedestrian");
    if (cancel === "Escape") h.key("Escape"); else h.button(cancel).props.onClick();
    await h.flush();
    assert.equal(h.context.closed, 1); assert.equal(h.context.saved, 0);
    assert.deepEqual(h.context.calls, []);
    assert.deepEqual(h.project.classes, originalClasses);
  });
}

for (const value of ["", "   ", " person "]) {
  test(`an appended ${JSON.stringify(value)} name cannot reach the save API`, async t => {
    const h = await harness(t);
    h.add(); h.name(2, value);
    assert.equal(h.button("저장").props.disabled, true);
    h.button("저장").props.onClick(); await h.flush();
    assert.deepEqual(h.context.calls, []); assert.equal(h.context.saved, 0);
  });
}

test("duplicate names between newly added rows are rejected after trimming", async t => {
  const h = await harness(t, { classes: [] });
  h.add(); h.add(); h.name(0, "bicycle"); h.name(1, " bicycle ");
  assert.equal(h.button("저장").props.disabled, true);
  h.button("저장").props.onClick(); await h.flush();
  assert.deepEqual(h.context.calls, []);
});

test("existing class renames cannot introduce duplicate trimmed names", async t => {
  const h = await harness(t);
  h.name(1, " person ");
  assert.equal(h.button("저장").props.disabled, true);
  h.button("저장").props.onClick(); await h.flush();
  assert.deepEqual(h.context.calls, []);
});

test("existing keyboard reorder still sends original IDs in the new order", async t => {
  const h = await harness(t);
  const handle = h.find(node => node.type === "button" && node.props["aria-label"]?.startsWith("vehicle 순서 이동"));
  assert.ok(handle); let prevented = false;
  handle.props.onKeyDown({ key: "ArrowUp", preventDefault() { prevented = true; } }); h.render();
  assert.equal(prevented, true);
  assert.deepEqual(h.inputs().map(input => input.props.value), ["vehicle", "person"]);
  h.button("저장").props.onClick(); await h.flush();
  assert.deepEqual(h.context.calls, [[31, revision, [originalClasses[1], originalClasses[0]]]]);
});

test("a failed catalog save preserves the new class for retry", async t => {
  let attempts = 0;
  const h = await harness(t, { save: () => {
    if (++attempts === 1) throw new Error("클래스를 저장하지 못했습니다.");
    return { classes: [], catalog_versions: [] };
  } });
  h.add(); h.name(2, "bicycle"); h.color(2, "#22c55e");
  h.button("저장").props.onClick(); await h.flush();
  assert.match(h.text(), /클래스를 저장하지 못했습니다/);
  assert.equal(h.context.saved, 0);
  assert.equal(h.inputs()[2].props.value, "bicycle");
  h.button("저장").props.onClick(); await h.flush();
  assert.equal(h.context.calls.length, 2);
  assert.deepEqual(h.context.calls[1], h.context.calls[0]);
  assert.equal(h.context.saved, 1);
});

test("a pending save blocks further additions and duplicate saves", async t => {
  const pending = deferred();
  const h = await harness(t, { save: () => pending.promise });
  h.add(); h.name(2, "bicycle"); h.button("저장").props.onClick(); h.render();
  assert.equal(h.button("클래스 추가").props.disabled, true);
  assert.equal(h.button("취소").props.disabled, true);
  assert.equal(h.button("저장 중…").props.disabled, true);
  h.button("저장 중…").props.onClick(); await h.flush();
  assert.equal(h.context.calls.length, 1);
  pending.resolve({ classes: [], catalog_versions: [] }); await h.flush();
  assert.equal(h.context.saved, 1);
});

test("the catalog API sends a new null class ID with the existing revision and IDs", async t => {
  const previousWindow = globalThis.window, previousFetch = globalThis.fetch;
  const calls = [];
  globalThis.window = { location: { origin: "https://annodock.test" }, setTimeout, clearTimeout };
  globalThis.fetch = async (path, init) => {
    calls.push({ path, init }); return Response.json({ classes: [], catalog_versions: [] });
  };
  t.after(() => { globalThis.window = previousWindow; globalThis.fetch = previousFetch; });
  const dependencies = moduleUrl(`
    export const getAuthSnapshot=()=>({accessToken:"classes-token",refreshToken:"refresh"});
    export const getActiveWorkspaceId=()=>71;
    export const refreshAuthSession=async()=>{throw new Error("unexpected refresh");};
  `);
  const source = await readFile(new URL("../src/api/client.ts", import.meta.url), "utf8");
  const javascript = ts.transpileModule(source, { compilerOptions: {
    module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022,
  } }).outputText.replace(/from "[^"]+"/g, `from "${dependencies}"`);
  const api = await import(`${moduleUrl(javascript)}#${Math.random()}`);
  const classes = [...originalClasses, { class_id: null, name: "bicycle", color: "#22c55e" }];
  await api.updateProjectClasses(31, revision, classes);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].path, "/api/projects/31/classes");
  assert.equal(calls[0].init.method, "PUT");
  assert.equal(calls[0].init.headers.get("X-Workspace-Id"), "71");
  assert.deepEqual(JSON.parse(calls[0].init.body), { expected_updated_at: revision, classes });
});
