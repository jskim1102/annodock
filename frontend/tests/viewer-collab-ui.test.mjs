import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import ts from "typescript";

const moduleUrl = (source) => `data:text/javascript;base64,${Buffer.from(source).toString("base64")}`;
const settle = () => new Promise((resolve) => setImmediate(resolve));
const initialBox = { id: 5, class_id: 0, cx: 0.5, cy: 0.5, w: 0.2, h: 0.25 };
const editedBoxes = [{ id: 5, classId: 0, x: 10, y: 8, width: 20, height: 16 }];
const latestBoxes = [...editedBoxes, { id: 6, classId: 0, x: 50, y: 24, width: 10, height: 8 }];

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

function elements(tree, predicate) {
  if (Array.isArray(tree)) return tree.flatMap((item) => elements(item, predicate));
  if (!tree || typeof tree !== "object") return [];
  return [...(predicate(tree) ? [tree] : []), ...elements(tree.props?.children, predicate)];
}

function content(tree) {
  if (Array.isArray(tree)) return tree.map(content).join("");
  if (typeof tree === "string" || typeof tree === "number") return String(tree);
  return tree && typeof tree === "object" ? content(tree.props?.children) : "";
}

async function harness(t, {
  role = "editor", image = {}, classes = [{ class_id: 0, name: "person", color: "#123456" }], catalogVersion = 5, beforeMount,
} = {}) {
  const h = {
    user: { id: 3, username: "작업자" }, workspaceId: 11, datasetId: 10, calls: [], handlers: {},
    catalog: structuredClone(classes), catalogVersion,
    dataset: { id: 10, project_id: 1, workspace_id: 11, my_role: role, name: "이미지 모음",
      image_count: 2, splits: [{ split: "train", image_count: 1 }, { split: "valid", image_count: 1 }] },
    rows: new Map([
      [101, { id: 101, stem: "first", filename: "first.jpg", split: "train", width: 100, height: 80,
        box_count: 1, is_modified: false, version: 3, label_status: "unlabeled",
        assignee_id: null, labeled_by: null, labeled_at: null, ...image }],
      [102, { id: 102, stem: "second", filename: "second.jpg", split: "valid", width: 100, height: 80,
        box_count: 0, is_modified: false, version: 1, label_status: "unlabeled",
        assignee_id: null, labeled_by: null, labeled_at: null }],
    ]),
    boxes: new Map([[101, [initialBox]], [102, []]]),
  };
  h.annotation = (id) => {
    const row = h.rows.get(id);
    const response = { image_id: id, width: row.width, height: row.height, version: row.version,
      label_status: row.label_status, assignee_id: row.assignee_id, boxes: structuredClone(h.boxes.get(id)), catalog_version: h.catalogVersion,
      classes: h.catalog.map(({ class_id, name }) => ({ class_id, name })).sort((a, b) => a.class_id - b.class_id) };
    if (h.omitClasses) delete response.classes;
    if (h.omitCatalogVersion) delete response.catalog_version;
    return response;
  };
  h.patchImage = (id, changes, boxes) => {
    h.rows.set(id, { ...h.rows.get(id), ...changes });
    if (boxes !== undefined) h.boxes.set(id, boxes);
    return structuredClone(h.rows.get(id));
  };
  const defaults = {
    getDataset: async () => structuredClone(h.dataset),
    getDatasetClasses: async () => ({ catalog_version: h.catalogVersion, classes: h.catalog.map(({ class_id, name }) => ({ class_id, name })) }),
    getProject: async () => ({ name: "Project", classes: structuredClone(h.catalog) }),
    getWorkspaceMembers: async () => ({ items: [
      { user_id: 3, username: "작업자", email: null, role: "editor" },
      { user_id: 4, username: "동료", email: null, role: "editor" },
    ] }),
    getDatasetImages: async (_id, from, limit, split) => {
      const rows = [...h.rows.values()].filter((row) => split === null || row.split === split);
      return { total: rows.length, items: structuredClone(rows.slice(from, from + limit)) };
    },
    getImageAnnotations: async (id) => h.annotation(id),
    getImage: async (id) => structuredClone(h.rows.get(id)),
    saveImageAnnotations: async (id, boxes, _workspaceId, expectedVersion) => {
      const row = h.rows.get(id);
      h.patchImage(id, { version: expectedVersion + 1, box_count: boxes.length, is_modified: true,
        label_status: row.label_status === "unlabeled" ? "in_progress" : row.label_status,
        assignee_id: row.label_status === "unlabeled" ? h.user.id : row.assignee_id },
      boxes.map((box, index) => ({ ...box, id: 900 + index })));
      return { ...h.annotation(id), is_modified: true };
    },
    claimDatasetImage: async () => ({ image: null }),
    completeImage: async (id) => h.patchImage(id, { version: h.rows.get(id).version + 1,
      label_status: "labeled", assignee_id: null, labeled_by: h.user.id, labeled_at: "2026-10-14T03:00:00Z" }),
    uncompleteImage: async (id) => h.patchImage(id, { version: h.rows.get(id).version + 1,
      label_status: "in_progress", assignee_id: h.user.id, labeled_by: null, labeled_at: null }),
    assignImage: async (id, assignee_id) => h.patchImage(id, { assignee_id }),
    renameDatasetClass: async (_datasetId, classId, name) => {
      h.catalog = h.catalog.map((row) => row.class_id === classId ? { ...row, name } : row);
      h.catalogVersion += 1;
      return { class_id: classId, name, catalog_version: h.catalogVersion,
        classes: h.catalog.map(({ class_id, name: label }) => ({ class_id, name: label })) };
    },
    updateProjectClass: async () => {},
  };
  h.defaults = defaults;
  h.invoke = async (name, args) => {
    h.calls.push({ name, args });
    return (h.handlers[name] ?? defaults[name])(...args);
  };
  const originalWindow = globalThis.window;
  const originalHTMLElement = globalThis.HTMLElement;
  const listeners = new Map();
  const idle = new Map();
  let idleId = 0;
  globalThis.HTMLElement = class HTMLElement {};
  globalThis.window = {
    addEventListener(name, callback) {
      if (!listeners.has(name)) listeners.set(name, new Set());
      listeners.get(name).add(callback);
    },
    removeEventListener(name, callback) { listeners.get(name)?.delete(callback); },
    requestIdleCallback(callback) { idle.set(++idleId, callback); return idleId; },
    cancelIdleCallback(id) { idle.delete(id); },
    location: { assign() {} },
  };
  const slots = [];
  const effects = [];
  const cleanups = new Map();
  let index = 0;
  let mounted = false;
  const changed = (previous, next) => !previous || !next || next.some((value, offset) => value !== previous[offset]);
  const memo = (create, deps) => {
    const slot = index++;
    if (!mounted || changed(slots[slot]?.deps, deps)) slots[slot] = { deps, value: create() };
    return slots[slot].value;
  };
  h.react = {
    useState(initial) {
      const slot = index++;
      if (!mounted) slots[slot] = typeof initial === "function" ? initial() : initial;
      return [slots[slot], (value) => { slots[slot] = typeof value === "function" ? value(slots[slot]) : value; }];
    },
    useRef(current) { const slot = index++; if (!mounted) slots[slot] = { current }; return slots[slot]; },
    useCallback: (fn, deps) => memo(() => fn, deps),
    useMemo: memo,
    useEffect(fn, deps) {
      const slot = index++;
      if (!mounted || changed(slots[slot], deps)) effects.push(() => {
        cleanups.get(slot)?.(); cleanups.set(slot, fn()); slots[slot] = deps;
      });
    },
  };
  const key = `__viewerCollabUI${Math.random().toString(16).slice(2)}`;
  globalThis[key] = h;
  t.after(() => {
    cleanups.forEach((cleanup) => cleanup?.());
    globalThis.window = originalWindow;
    if (originalHTMLElement === undefined) delete globalThis.HTMLElement;
    else globalThis.HTMLElement = originalHTMLElement;
    delete globalThis[key];
  });
  const reactModule = moduleUrl(`export const {useState,useRef,useCallback,useMemo,useEffect} = globalThis[${JSON.stringify(key)}].react;`);
  const apiModule = moduleUrl(`
    const h = globalThis[${JSON.stringify(key)}];
    export class ApiError extends Error { constructor(status,message,detail){ super(message); this.status=status; this.detail=detail; } }
    export class AuthApiError extends ApiError {}
    export const captureWorkspaceId = () => h.workspaceId;
    export const useAuthSession = () => ({ user: h.user });
    export const imageResourceUrl = (id) => "/api/images/"+id+"/file";
    ${Object.keys(defaults).map((name) => `export const ${name} = (...args) => h.invoke(${JSON.stringify(name)}, args);`).join("\n")}
  `);
  h.ApiError = (await import(apiModule)).ApiError;
  const compile = async (path, dependencies) => {
    const source = await readFile(new URL(path, import.meta.url), "utf8");
    return moduleUrl(ts.transpileModule(source, { compilerOptions: {
      module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX,
    } }).outputText.replace(/from "([^"]+)"/g, (_match, name) => `from "${dependencies(name)}"`));
  };
  const actualHookModule = await compile("../src/hooks/useAnnotationEdit.ts", (name) => name === "react" ? reactModule : apiModule);
  const hookModule = moduleUrl(`
    import { useAnnotationEdit as actualHook } from ${JSON.stringify(actualHookModule)};
    export function useAnnotationEdit(...args) {
      const edit = actualHook(...args);
      globalThis[${JSON.stringify(key)}].edit = edit;
      return edit;
    }
  `);
  const components = moduleUrl(`
    export const jsx = (type,props) => ({type,props}), jsxs = jsx, Fragment = "fragment";
    export function BboxCanvas() {} export function ThumbStrip() {} export function WorkspaceDialog() {}
    export function Brand() {} export function ThemeToggle() {}
    export const useAuthenticatedObjectUrl = (path) => ({ url: path ? "blob:"+path : null });
    export const prefetchAuthenticatedResource = async () => {};
    export const adminHomePath = async () => "/projects";
    export const appHref = (path) => path, CLASS_COLOR_PRESETS = [{value:"#123456"}];
  `);
  const viewerModule = await compile("../src/pages/Viewer.tsx", (name) => {
    if (name === "react") return reactModule;
    if (name === "../hooks/useAnnotationEdit") return hookModule;
    if (name.startsWith("../api/") || name === "../store/auth") return apiModule;
    return components;
  });
  const { ViewerPage } = await import(viewerModule);
  h.render = () => {
    index = 0;
    const tree = ViewerPage({ datasetId: h.datasetId });
    mounted = true;
    effects.splice(0).forEach((effect) => effect());
    return tree;
  };
  h.drain = async () => {
    for (let round = 0; round < 8; round += 1) { h.render(); await settle(); }
    return h.render();
  };
  h.canvas = () => elements(h.render(), (node) => node.type?.name === "BboxCanvas")[0];
  h.strip = () => elements(h.render(), (node) => node.type?.name === "ThumbStrip")[0];
  h.dialog = () => elements(h.render(), (node) => node.type?.name === "WorkspaceDialog")[0];
  h.button = (label) => elements(h.render(), (node) => node.type === "button" && content(node) === label)[0];
  h.press = async (label) => {
    const button = h.button(label);
    assert.ok(button, `button ${label} exists`);
    assert.notEqual(button.props.disabled, true, `button ${label} is enabled`);
    button.props.onClick();
    return h.drain();
  };
  h.requests = (name) => h.calls.filter((call) => call.name === name);
  h.keyboardUndo = () => {
    const event = { ctrlKey: true, key: "z", target: null, preventDefault() {} };
    listeners.get("keydown")?.forEach((listener) => listener(event));
  };
  h.runIdle = async () => {
    const callbacks = [...idle.values()];
    assert.ok(callbacks.length, "Viewer scheduled annotation prefetch");
    idle.clear();
    callbacks.forEach((callback) => callback());
    await h.drain();
  };
  beforeMount?.(h);
  await h.drain();
  assert.ok(h.canvas(), "fixture loads the actual Viewer and real annotation hook");
  return h;
}

for (const split of [null, "train"]) {
  test(`claim null preserves the current image and offers retry with split ${split}`, async (t) => {
    const h = await harness(t);
    if (split) {
      const chip = elements(h.render(), (node) => node.type === "button" && content(node).startsWith(split))[0];
      chip.props.onClick(); await h.drain();
    }
    const before = h.canvas().props;
    await h.press("다음 이미지 →");
    assert.deepEqual(h.requests("claimDatasetImage").map(({ args }) => args), [[10, split, 11]]);
    assert.match(content(h.render()), /지금 가져올 이미지가 없어요/);
    assert.equal(h.canvas().props.imageSrc, before.imageSrc);
    assert.deepEqual(h.canvas().props.boxes, before.boxes);
    assert.equal(h.strip().props.currentIndex, 0);
    assert.equal(content(h.canvas().props.statusBadge), "미배정");
    await h.press("다시 시도");
    assert.deepEqual(h.requests("claimDatasetImage").map(({ args }) => args), [[10, split, 11], [10, split, 11]]);
    assert.equal(h.requests("completeImage").length, 0);
  });
}

test("claiming the current position refreshes assignment and the next save version", async (t) => {
  const h = await harness(t);
  h.handlers.claimDatasetImage = async () => {
    h.patchImage(101, { version: 4, label_status: "in_progress", assignee_id: 3 });
    return { image: { id: 101, stem: "first", split: "train", version: 4,
      label_status: "in_progress", assignee_id: 3, labeled_by: null, labeled_at: null }, position: 0 };
  };
  await h.press("다음 이미지 →");
  assert.equal(h.strip().props.currentIndex, 0);
  assert.match(content(h.canvas().props.statusBadge), /나 · 작업 중/);
  assert.equal(h.strip().props.statusLabel(h.strip().props.items.get(0)), "나");
  assert.deepEqual(h.requests("getImageAnnotations").at(-1).args, [101, undefined, 11]);
  h.canvas().props.onChange(editedBoxes);
  await h.drain();
  assert.equal(h.requests("saveImageAnnotations")[0].args[3], 4);
});

test("completion sends no POST after failed flush and refreshes version after a successful flush", async (t) => {
  const h = await harness(t);
  h.handlers.saveImageAnnotations = async () => { throw new TypeError("Failed to fetch"); };
  h.canvas().props.onChange(editedBoxes);
  await h.drain();
  h.canvas().props.completionAction.props.onClick();
  await h.drain();
  assert.equal(h.requests("completeImage").length, 0);
  assert.deepEqual(h.canvas().props.boxes, editedBoxes);
  assert.equal(h.canvas().props.saveState, "error");
  delete h.handlers.saveImageAnnotations;
  h.canvas().props.completionAction.props.onClick();
  await h.drain();
  assert.deepEqual(h.requests("completeImage").map(({ args }) => args), [[101, 11]]);
  assert.equal(h.rows.get(101).version, 5, "save increments 3→4; completion increments 4→5");
  assert.match(content(h.canvas().props.statusBadge), /✓ 완료/);
  assert.equal(content(h.canvas().props.completionAction), "완료 취소");
  h.canvas().props.onChange(latestBoxes);
  await h.drain();
  assert.equal(h.requests("saveImageAnnotations").at(-1).args[3], 5);
});

for (const scenario of [
  { name: "viewer role", role: "viewer", image: {}, message: null, claim: false },
  { name: "another editor assignment", role: "editor", image: { label_status: "in_progress", assignee_id: 4 }, message: /동료 님이 작업 중인 이미지예요/, claim: true },
]) {
  test(`${scenario.name} makes canvas read-only and disables annotation mutations`, async (t) => {
    const h = await harness(t, scenario);
    assert.equal(h.canvas().props.readOnly, true);
    assert.equal(h.canvas().props.canManageClasses, false);
    assert.equal(h.canvas().props.completionAction, null);
    assert.equal(Boolean(h.button("다음 이미지 →")), scenario.claim);
    if (scenario.message) assert.match(content(h.render()), scenario.message);
    else assert.doesNotMatch(content(h.render()), /보기 전용|역할이라 수정/);
    h.canvas().props.onChange(editedBoxes);
    h.canvas().props.onUndo();
    await h.drain();
    assert.equal(h.requests("saveImageAnnotations").length, 0);
  });
}

test("an admin can edit another member's assignment", async (t) => {
  const h = await harness(t, { role: "admin", image: { label_status: "in_progress", assignee_id: 4 } });
  assert.equal(h.canvas().props.readOnly, false);
  assert.equal(h.canvas().props.canManageClasses, true);
  h.canvas().props.onChange(editedBoxes);
  await h.drain();
  assert.equal(h.requests("saveImageAnnotations").length, 1);
});

test("assignment conflict changes the current canvas to read-only and stops queued saves", async (t) => {
  const h = await harness(t);
  const save = deferred();
  h.handlers.saveImageAnnotations = async () => save.promise;
  h.canvas().props.onChange(editedBoxes);
  h.canvas().props.onChange(latestBoxes);
  save.reject(new h.ApiError(409, "conflict", { code: "image_assigned_to_other", assignee_id: 4 }));
  await h.drain();
  assert.equal(h.canvas().props.readOnly, true);
  assert.equal(h.canvas().props.completionAction, null);
  assert.match(content(h.render()), /동료 님이 작업 중인 이미지예요/);
  h.canvas().props.onChange(editedBoxes);
  h.canvas().props.onUndo();
  await h.drain();
  assert.equal(h.requests("saveImageAnnotations").length, 1);
});

for (const choice of ["서버 것으로", "내 것 유지"]) {
  test(`version conflict bypasses cache in the original workspace and resolves with ${choice}`, async (t) => {
    const h = await harness(t, { image: { label_status: "in_progress", assignee_id: 3 } });
    const first = deferred();
    h.handlers.saveImageAnnotations = (...args) => h.requests("saveImageAnnotations").length === 1
      ? first.promise : h.defaults.saveImageAnnotations(...args);
    h.canvas().props.onChange(editedBoxes);
    h.canvas().props.onChange(latestBoxes);
    h.workspaceId = 22;
    h.patchImage(101, { version: 8 }, [initialBox, { ...initialBox, id: 92 }]);
    first.reject(new h.ApiError(409, "conflict", {
      code: "version_conflict", current: { ...h.annotation(101), version: 4, boxes: [] },
    }));
    await h.drain();
    assert.equal(h.dialog().props.title, "그사이 다른 저장이 있었어요");
    assert.match(content(h.dialog()), /내 편집 박스 2개/);
    assert.match(content(h.dialog()), /서버 박스 2개/, "comparison uses the fresh GET instead of cached or 409 boxes");
    assert.equal(h.canvas().props.readOnly, true);
    assert.equal(h.requests("saveImageAnnotations").length, 1);
    assert.deepEqual(h.requests("getImageAnnotations").at(-1).args, [101, undefined, 11]);

    h.patchImage(101, { version: 9 }, [{ ...initialBox, id: 99 }]);
    await h.press(choice);
    assert.equal(h.dialog(), undefined);
    assert.equal(h.requests("getImageAnnotations").length, 3, "resolution fetches again after initial read and conflict refresh");
    assert.deepEqual(h.requests("getImageAnnotations").at(-1).args, [101, undefined, 11]);
    assert.deepEqual(h.requests("getImage").at(-1).args, [101, 11]);
    if (choice === "내 것 유지") {
      assert.deepEqual(h.requests("saveImageAnnotations").at(-1).args, [101, [
        { class_id: 0, cx: 0.2, cy: 0.2, w: 0.2, h: 0.2 },
        { class_id: 0, cx: 0.55, cy: 0.35, w: 0.1, h: 0.1 },
      ], 11, 9, 5]);
      assert.deepEqual(h.canvas().props.boxes, latestBoxes);
    } else {
      assert.equal(h.requests("saveImageAnnotations").length, 1);
      assert.deepEqual(h.canvas().props.boxes, [{ id: 99, classId: 0, x: 40, y: 30, width: 20, height: 20 }]);
      h.workspaceId = 11;
      h.canvas().props.onChange(editedBoxes);
      await h.drain();
      assert.equal(h.requests("saveImageAnnotations").at(-1).args[3], 9);
    }
  });
}

const originalCatalog = [
  { class_id: 0, name: "cat", color: "#AA1122" },
  { class_id: 1, name: "dog", color: "#3344BB" },
];
const swappedCatalog = [{ class_id: 0, name: "dog" }, { class_id: 1, name: "cat" }];
const addedCatalog = [...originalCatalog, { class_id: 2, name: "bird" }];
const mappingMessage = "클래스 목록이 바뀌어 내 편집을 옮길 수 없어요. 서버 것으로 불러와 주세요.";
const catAndDogEdits = [editedBoxes[0], { ...latestBoxes[1], classId: 1 }];
const classNames = (catalog) => catalog.map(({ class_id, name }) => ({ class_id, name }));

async function catalogConflict(t, {
  catalog = originalCatalog, catalogVersion = 5, localBoxes = catAndDogEdits,
  code = "version_conflict", omitClasses = false, omitCatalogVersion = false, refreshFailure = false,
} = {}) {
  const h = await harness(t, {
    image: { label_status: "in_progress", assignee_id: 3 }, classes: originalCatalog,
  });
  const first = deferred();
  h.handlers.saveImageAnnotations = (...args) => h.requests("saveImageAnnotations").length === 1
    ? first.promise : h.defaults.saveImageAnnotations(...args);
  h.canvas().props.onChange(editedBoxes);
  h.canvas().props.onChange(localBoxes);
  h.catalog = structuredClone(catalog);
  h.catalogVersion = catalogVersion;
  h.patchImage(101, { version: 8 }, catalog.map(({ class_id }, index) => ({ ...initialBox, id: 91 + index, class_id })));
  h.omitClasses = omitClasses;
  h.omitCatalogVersion = omitCatalogVersion;
  if (refreshFailure) h.handlers.getImageAnnotations = async () => { throw new TypeError("annotation refresh unavailable"); };
  first.reject(new h.ApiError(409, "annotation conflict", { code, current: h.annotation(101) }));
  await h.drain();
  assert.ok(h.dialog());
  assert.equal(h.requests("saveImageAnnotations").length, 1);
  return h;
}

function canvasCatalog(h) {
  return h.canvas().props.classes.map(({ id, name, color }) => ({ class_id: id, name, color }));
}

function assertSaveVersions(h, imageVersion, catalogVersion) {
  assert.deepEqual(h.requests("saveImageAnnotations").at(-1).args.slice(2), [11, imageVersion, catalogVersion]);
}

async function selectImage(h, index) {
  h.strip().props.onSelect(index);
  await h.drain();
}

test("entry catalog version zero is applied and preserved in the first annotation save", async (t) => {
  const h = await harness(t, { classes: originalCatalog, catalogVersion: 0 });
  assert.deepEqual(canvasCatalog(h), originalCatalog);
  h.canvas().props.onChange(editedBoxes);
  await h.drain();
  assertSaveVersions(h, 3, 0);
});

test("a newer initial image snapshot replaces the entry catalog before the first save", async (t) => {
  const project = deferred();
  const h = await harness(t, {
    classes: originalCatalog, image: { label_status: "in_progress", assignee_id: 3 },
    beforeMount(current) {
      current.handlers.getProject = () => project.promise;
      current.handlers.getImageAnnotations = async (id) => ({ ...current.annotation(id),
        version: 8, catalog_version: 6, classes: structuredClone(swappedCatalog),
        boxes: [{ ...initialBox, class_id: 1 }],
      });
    },
  });
  assert.deepEqual(classNames(canvasCatalog(h)), swappedCatalog);
  assert.deepEqual(h.canvas().props.boxes.map(({ classId }) => classId), [1]);
  project.resolve({ name: "Project", classes: structuredClone(originalCatalog) });
  await h.drain();
  assert.deepEqual(canvasCatalog(h).map(({ color }) => color), originalCatalog.map(({ color }) => color));
  h.canvas().props.onChange(h.canvas().props.boxes.map((box) => ({ ...box, x: box.x + 1 })));
  await h.drain();
  assert.equal(h.requests("saveImageAnnotations").at(-1).args[1][0].class_id, 1);
  assertSaveVersions(h, 8, 6);
});

test("the first uncached image after a reorder displays and saves the new IDs and catalog version", async (t) => {
  const h = await harness(t, { classes: originalCatalog });
  h.catalog = structuredClone(swappedCatalog);
  h.catalogVersion = 6;
  h.patchImage(102, { version: 8, box_count: 1 }, [{ ...initialBox, id: 202, class_id: 1 }]);
  await selectImage(h, 1);
  assert.deepEqual(classNames(canvasCatalog(h)), swappedCatalog);
  assert.deepEqual(h.canvas().props.classes.map(({ name, count }) => [name, count]), [["dog", 0], ["cat", 1]]);
  h.canvas().props.onChange(h.canvas().props.boxes.map((box) => ({ ...box, x: box.x + 1 })));
  await h.drain();
  const saved = h.requests("saveImageAnnotations").at(-1).args;
  assert.equal(saved[0], 102);
  assert.equal(saved[1][0].class_id, 1);
  assertSaveVersions(h, 8, 6);
});

test("a newly added class on image entry is usable and allows a later same-catalog conflict retry", async (t) => {
  const h = await harness(t, { classes: originalCatalog });
  h.catalog = structuredClone(addedCatalog);
  h.catalogVersion = 6;
  h.patchImage(102, { version: 8, box_count: 1 }, [{ ...initialBox, id: 202, class_id: 2 }]);
  await selectImage(h, 1);
  assert.deepEqual(classNames(canvasCatalog(h)), classNames(addedCatalog));
  assert.equal(h.canvas().props.classes.find(({ id }) => id === 2).count, 1);
  const next = h.canvas().props.boxes.map((box) => ({ ...box, x: box.x + 1 }));
  h.handlers.saveImageAnnotations = async (...args) => {
    if (h.requests("saveImageAnnotations").length === 1) {
      h.patchImage(102, { version: 9 });
      throw new h.ApiError(409, "another image save", { code: "version_conflict", current: h.annotation(102) });
    }
    return h.defaults.saveImageAnnotations(...args);
  };
  h.canvas().props.onChange(next);
  await h.drain();
  assert.notEqual(h.button("내 것 유지").props.disabled, true);
  await h.press("내 것 유지");
  assert.equal(h.dialog(), undefined);
  assert.deepEqual(h.canvas().props.boxes, next);
  assert.equal(h.requests("saveImageAnnotations").at(-1).args[1][0].class_id, 2);
  assertSaveVersions(h, 9, 6);
});

for (const action of ["claim", "complete", "assignment"]) {
  test(`${action} refresh applies a newer image catalog before the next edit`, async (t) => {
    const h = await harness(t, {
      role: action === "assignment" ? "admin" : "editor", classes: originalCatalog,
      image: { label_status: "in_progress", assignee_id: 3 },
    });
    const change = () => {
      h.catalog = structuredClone(swappedCatalog);
      h.catalogVersion = 6;
      return h.patchImage(101, { version: 8 }, [{ ...initialBox, class_id: 1 }]);
    };
    if (action === "claim") {
      h.handlers.claimDatasetImage = async () => ({ image: change(), position: 0 });
      await h.press("다음 이미지 →");
    } else if (action === "complete") {
      h.handlers.completeImage = async () => {
        change();
        return h.patchImage(101, { label_status: "labeled", assignee_id: null, labeled_by: 3 });
      };
      h.canvas().props.completionAction.props.onClick();
      await h.drain();
    } else {
      h.handlers.assignImage = async () => {
        change();
        return h.patchImage(101, { assignee_id: 4 });
      };
      const toggle = elements(h.canvas().props.imageInspector, (node) => node.type === "button" && content(node) === "바꾸기 ⌄")[0];
      assert.ok(toggle);
      toggle.props.onClick();
      await h.drain();
      const select = elements(h.canvas().props.imageInspector, (node) => node.type === "select" && node.props["aria-label"] === "이미지 담당 변경")[0];
      assert.ok(select);
      select.props.onChange({ target: { value: "4" } });
      await h.drain();
    }
    assert.deepEqual(classNames(canvasCatalog(h)), swappedCatalog);
    assert.deepEqual(h.canvas().props.boxes.map(({ classId }) => classId), [1]);
    h.canvas().props.onChange(h.canvas().props.boxes.map((box) => ({ ...box, x: box.x + 1 })));
    await h.drain();
    assert.equal(h.requests("saveImageAnnotations").at(-1).args[1][0].class_id, 1);
    assertSaveVersions(h, 8, 6);
  });
}

for (const choice of ["내 것 유지", "서버 것으로"]) {
  test(`same catalog version permits ${choice} and preserves its undo behavior`, async (t) => {
    const h = await catalogConflict(t);
    assert.equal(h.canvas().props.undoDepth, 2);
    await h.press(choice);
    assert.equal(h.dialog(), undefined);
    assert.deepEqual(canvasCatalog(h), originalCatalog);
    if (choice === "내 것 유지") {
      assert.deepEqual(h.canvas().props.boxes, catAndDogEdits);
      assert.equal(h.canvas().props.undoDepth, 2);
      assert.deepEqual(h.requests("saveImageAnnotations").at(-1).args[1].map(({ class_id }) => class_id), [0, 1]);
      assertSaveVersions(h, 8, 5);
      h.canvas().props.onUndo();
      await h.drain();
      assert.deepEqual(h.canvas().props.boxes, editedBoxes);
      assertSaveVersions(h, 9, 5);
    } else {
      assert.equal(h.canvas().props.undoDepth, 0);
      assert.deepEqual(h.canvas().props.boxes.map(({ id, classId }) => [id, classId]), [[91, 0], [92, 1]]);
      assert.equal(h.requests("saveImageAnnotations").length, 1);
    }
  });
}

for (const scenario of [
  { name: "name swap", catalog: swappedCatalog },
  { name: "rename", catalog: [{ class_id: 0, name: "kitty" }, originalCatalog[1]] },
  { name: "reorder", catalog: [{ class_id: 2, name: "cat" }, { class_id: 3, name: "dog" }] },
  { name: "merge", catalog: [originalCatalog[0]] },
  { name: "deletion", catalog: [originalCatalog[1]] },
  { name: "addition", catalog: addedCatalog },
  { name: "unused class rename", catalog: [originalCatalog[0], { ...originalCatalog[1], name: "wolf" }], localBoxes: latestBoxes },
  { name: "identical names and IDs with a newer catalog version", catalog: originalCatalog },
]) {
  test(`a different catalog version blocks local resolution after ${scenario.name}`, async (t) => {
    const h = await catalogConflict(t, { ...scenario, catalogVersion: 6 });
    assert.equal(h.button("내 것 유지").props.disabled, true);
    assert.ok(content(h.dialog()).includes(mappingMessage));
    h.button("내 것 유지").props.onClick();
    await h.drain();
    assert.equal(h.requests("saveImageAnnotations").length, 1);
    assert.deepEqual(h.canvas().props.boxes, scenario.localBoxes ?? catAndDogEdits);
    assert.deepEqual(canvasCatalog(h), originalCatalog);
    await h.press("서버 것으로");
    assert.equal(h.dialog(), undefined);
    assert.deepEqual(classNames(canvasCatalog(h)), classNames(scenario.catalog));
    assert.equal(h.canvas().props.undoDepth, 0);
    const next = h.canvas().props.boxes.map((box) => ({ ...box, x: box.x + 1 }));
    h.canvas().props.onChange(next);
    await h.drain();
    assertSaveVersions(h, 8, 6);
  });
}

test("class_catalog_changed blocks local resolution even when its numeric version matches", async (t) => {
  const h = await catalogConflict(t, { code: "class_catalog_changed" });
  assert.equal(h.button("내 것 유지").props.disabled, true);
  assert.ok(content(h.dialog()).includes(mappingMessage));
  h.button("내 것 유지").props.onClick();
  await h.drain();
  assert.equal(h.requests("saveImageAnnotations").length, 1);
  await h.press("서버 것으로");
  assert.equal(h.dialog(), undefined);
  assert.equal(h.canvas().props.undoDepth, 0);
  assert.deepEqual(canvasCatalog(h), originalCatalog);
});

for (const missing of ["classes", "catalog_version", "both"]) {
  test(`a conflict missing ${missing} blocks local writes without a dataset-class fallback`, async (t) => {
    const h = await catalogConflict(t, {
      omitClasses: missing !== "catalog_version", omitCatalogVersion: missing !== "classes",
    });
    assert.equal(h.button("내 것 유지").props.disabled, true);
    assert.ok(content(h.dialog()).includes(mappingMessage));
    assert.equal(h.requests("getDatasetClasses").length, 1, "only the initial dataset catalog is requested");
    h.button("내 것 유지").props.onClick();
    await h.drain();
    assert.equal(h.requests("saveImageAnnotations").length, 1);
    assert.equal(h.requests("getDatasetClasses").length, 1);
    assert.deepEqual(h.canvas().props.boxes, catAndDogEdits);
    assert.deepEqual(canvasCatalog(h), originalCatalog);
  });
}

test("a newer catalog appearing during resolution blocks a previously enabled local action", async (t) => {
  const h = await catalogConflict(t);
  assert.notEqual(h.button("내 것 유지").props.disabled, true);
  h.catalog = structuredClone(addedCatalog);
  h.catalogVersion = 6;
  h.patchImage(101, { version: 11 });
  await h.press("내 것 유지");
  assert.ok(h.dialog());
  assert.ok(content(h.render()).includes(mappingMessage));
  assert.equal(h.requests("saveImageAnnotations").length, 1);
  assert.deepEqual(h.canvas().props.boxes, catAndDogEdits);
  assert.deepEqual(canvasCatalog(h), originalCatalog);
});

test("a 422 retry is not adopted and preserves the conflict, local boxes, and unapplied catalog", async (t) => {
  const h = await catalogConflict(t);
  h.handlers.saveImageAnnotations = async () => { throw new h.ApiError(422, "invalid annotation", { code: "invalid_class" }); };
  await h.press("내 것 유지");
  assert.ok(h.dialog());
  assert.equal(h.canvas().props.saveState, "rejected");
  assert.equal(h.canvas().props.readOnly, true);
  assert.deepEqual(h.canvas().props.boxes, catAndDogEdits);
  assert.deepEqual(canvasCatalog(h), originalCatalog);
  h.canvas().props.onRetrySave();
  h.canvas().props.onChange(latestBoxes);
  await h.drain();
  assert.equal(h.requests("saveImageAnnotations").length, 2);
  assert.deepEqual(h.canvas().props.boxes, catAndDogEdits);
  h.catalog = structuredClone(swappedCatalog);
  h.catalogVersion = 6;
  h.patchImage(101, { version: 9 }, [{ ...initialBox, class_id: 1 }]);
  await h.press("서버 것으로");
  assert.equal(h.dialog(), undefined);
  assert.deepEqual(classNames(canvasCatalog(h)), swappedCatalog);
  assert.equal(h.canvas().props.undoDepth, 0);
});

test("own rename flushes queued edits, sends its local catalog version, and resets from fresh server annotations", async (t) => {
  const h = await harness(t, { role: "admin", classes: originalCatalog });
  const first = deferred();
  h.handlers.saveImageAnnotations = (...args) => h.requests("saveImageAnnotations").length === 1
    ? first.promise : h.defaults.saveImageAnnotations(...args);
  h.canvas().props.onChange(editedBoxes);
  h.canvas().props.onChange(latestBoxes);
  const renaming = h.canvas().props.onRenameClass(0, "kitty");
  await h.drain();
  assert.equal(h.requests("renameDatasetClass").length, 0, "rename cannot overtake the pending annotation save");
  assert.deepEqual(classNames(canvasCatalog(h)), classNames(originalCatalog));
  first.resolve(await h.defaults.saveImageAnnotations(...h.requests("saveImageAnnotations")[0].args));
  await h.drain();
  await renaming;
  await h.drain();
  assert.deepEqual(h.calls.filter(({ name }) => ["saveImageAnnotations", "renameDatasetClass"].includes(name)).map(({ name }) => name),
    ["saveImageAnnotations", "saveImageAnnotations", "renameDatasetClass"]);
  assert.deepEqual(h.requests("saveImageAnnotations").map(({ args }) => args[4]), [5, 5]);
  assert.deepEqual(h.requests("renameDatasetClass")[0].args, [10, 0, "kitty", 5, 11]);
  assert.equal(h.requests("getImageAnnotations").length, 2, "rename must bypass the annotation cache");
  assert.deepEqual(h.requests("getImageAnnotations").at(-1).args, [101, undefined, 11]);
  assert.deepEqual(h.canvas().props.boxes, latestBoxes.map((box, index) => ({ ...box, id: 900 + index })));
  assert.equal(h.canvas().props.undoDepth, 0);
  assert.deepEqual(canvasCatalog(h), [{ ...originalCatalog[0], name: "kitty" }, originalCatalog[1]]);
  h.canvas().props.onChange(h.canvas().props.boxes.map((box) => ({ ...box, x: box.x + 1 })));
  await h.drain();
  assertSaveVersions(h, 5, 6);
});

test("own rename rejects without a PATCH when flushing local edits fails with 503", async (t) => {
  const h = await harness(t, { role: "admin", classes: originalCatalog });
  h.handlers.saveImageAnnotations = async () => { throw new h.ApiError(503, "save unavailable", {}); };
  h.canvas().props.onChange(latestBoxes);
  await h.drain();
  await assert.rejects(h.canvas().props.onRenameClass(0, "kitty"), { message: "저장하지 못해 이름을 바꾸지 않았어요" });
  await h.drain();
  assert.equal(h.requests("renameDatasetClass").length, 0);
  assert.deepEqual(h.canvas().props.boxes, latestBoxes);
  assert.deepEqual(canvasCatalog(h), originalCatalog);
});

for (const status of [403, 422]) {
  test(`own rename cannot discard local edits after an annotation save is rejected with ${status}`, async (t) => {
    const h = await harness(t, { role: "admin", classes: originalCatalog });
    h.handlers.saveImageAnnotations = async () => { throw new h.ApiError(status, "save rejected", { code: "save_rejected" }); };
    h.canvas().props.onChange(latestBoxes);
    await h.drain();
    assert.equal(h.canvas().props.saveState, "rejected");
    assert.deepEqual(h.canvas().props.boxes, latestBoxes);
    const readsBeforeRename = h.requests("getImageAnnotations").length;
    await assert.rejects(h.canvas().props.onRenameClass(0, "kitty"), { message: "저장하지 못해 이름을 바꾸지 않았어요" });
    await h.drain();
    assert.equal(h.requests("renameDatasetClass").length, 0, "navigation-permitted rejection is not a successful save before rename");
    assert.equal(h.requests("getImageAnnotations").length, readsBeforeRename, "rename cannot replace unsaved boxes with server annotations");
    assert.deepEqual(h.canvas().props.boxes, latestBoxes);
    assert.deepEqual(canvasCatalog(h), originalCatalog);
    assert.equal(h.canvas().props.saveState, "rejected");
  });
}

test("a remote reorder rejects the rename with its captured catalog version and reloads authoritative IDs", async (t) => {
  const h = await harness(t, { role: "admin", classes: originalCatalog });
  h.handlers.renameDatasetClass = async () => {
    h.catalog = structuredClone(swappedCatalog); h.catalogVersion = 7;
    h.patchImage(101, { version: 9 }, [{ ...initialBox, id: 99, class_id: 1 }]);
    throw new h.ApiError(409, "catalog changed", { code: "class_catalog_changed" });
  };
  await assert.rejects(h.canvas().props.onRenameClass(0, "kitty"), {
    message: "클래스 목록이 바뀌어 새로 불러왔어요. 이름 변경을 다시 열어 주세요.",
  });
  await h.drain();
  assert.match(content(h.render()), /클래스 목록이 바뀌어 새로 불러왔어요\. 이름 변경을 다시 열어 주세요\./);
  assert.deepEqual(h.requests("renameDatasetClass")[0].args, [10, 0, "kitty", 5, 11]);
  assert.deepEqual(h.requests("getImageAnnotations").at(-1).args, [101, undefined, 11]);
  assert.deepEqual(classNames(canvasCatalog(h)), swappedCatalog);
  assert.deepEqual(h.canvas().props.boxes.map(({ id, classId }) => [id, classId]), [[99, 1]]);
  assert.equal(h.requests("saveImageAnnotations").length, 0, "the pre-rename class 0 boxes are never rewritten");
  h.handlers.saveImageAnnotations = async () => {
    h.catalogVersion = 8;
    throw new h.ApiError(409, "another catalog change", { code: "version_conflict", current: h.annotation(101) });
  };
  h.canvas().props.onChange(h.canvas().props.boxes.map((box) => ({ ...box, x: box.x + 1 })));
  await h.drain();
  assert.equal(h.requests("saveImageAnnotations")[0].args[1][0].class_id, 1);
  assertSaveVersions(h, 9, 7);
  assert.equal(h.button("내 것 유지").props.disabled, true);
  h.button("내 것 유지").props.onClick();
  await h.drain();
  assert.equal(h.requests("saveImageAnnotations").length, 1);
});

test("a successful rename uses the following GET snapshot rather than trusting the PATCH catalog version", async (t) => {
  const h = await harness(t, { role: "admin", classes: originalCatalog });
  h.handlers.renameDatasetClass = async (...args) => {
    const response = await h.defaults.renameDatasetClass(...args);
    h.catalog = [{ class_id: 0, name: "dog" }, { class_id: 1, name: "kitty" }];
    h.catalogVersion = 7;
    h.patchImage(101, { version: 9 }, [{ ...initialBox, id: 99, class_id: 1 }]);
    return response; // PATCH version 6 was followed by another writer's reorder.
  };
  await h.canvas().props.onRenameClass(0, "kitty");
  await h.drain();
  assert.equal(h.requests("getImageAnnotations").length, 2);
  assert.deepEqual(classNames(canvasCatalog(h)), h.catalog);
  assert.deepEqual(h.canvas().props.boxes.map(({ id, classId }) => [id, classId]), [[99, 1]]);
  h.canvas().props.onChange(h.canvas().props.boxes.map((box) => ({ ...box, x: box.x + 1 })));
  await h.drain();
  assert.equal(h.requests("saveImageAnnotations")[0].args[1][0].class_id, 1);
  assertSaveVersions(h, 9, 7);
});

test("rename during another image action rejects instead of silently succeeding", async (t) => {
  const h = await harness(t, { role: "admin", classes: originalCatalog });
  const pending = deferred();
  h.handlers.claimDatasetImage = () => pending.promise;
  await h.press("다음 이미지 →");
  await assert.rejects(h.canvas().props.onRenameClass(0, "kitty"), { message: "저장하지 못해 이름을 바꾸지 않았어요" });
  assert.equal(h.requests("renameDatasetClass").length, 0);
  pending.resolve({ image: null });
  await h.drain();
});

for (const waitingAt of ["PATCH", "reload"]) {
  test(`rename waiting at ${waitingAt} blocks keyboard undo and new edits while preserving the saved boxes`, async (t) => {
    const h = await harness(t, { role: "admin", classes: originalCatalog });
    h.canvas().props.onChange(latestBoxes);
    await h.drain();
    const saved = structuredClone(h.canvas().props.boxes);
    const pending = deferred();
    if (waitingAt === "PATCH") h.handlers.renameDatasetClass = () => pending.promise;
    else h.handlers.getImageAnnotations = () => pending.promise;
    const renaming = h.canvas().props.onRenameClass(0, "kitty");
    await h.drain();
    assert.equal(h.canvas().props.readOnly, true);
    h.keyboardUndo();
    h.canvas().props.onUndo();
    h.canvas().props.onChange(editedBoxes);
    await h.drain();
    assert.equal(h.requests("saveImageAnnotations").length, 1, "Ctrl+Z and canvas callbacks cannot create writes while rename is busy");
    assert.deepEqual(h.canvas().props.boxes, saved);
    if (waitingAt === "PATCH") pending.resolve(await h.defaults.renameDatasetClass(10, 0, "kitty"));
    else pending.resolve(h.annotation(101));
    await renaming;
    await h.drain();
    assert.equal(h.canvas().props.readOnly, false);
    assert.deepEqual(h.canvas().props.boxes.map(({ id, ...box }) => box), saved.map(({ id, ...box }) => box));
  });
}

test("successful PATCH followed by failed reload inherits its catalog and retries only GET while edits stay blocked", async (t) => {
  const h = await harness(t, { role: "admin", classes: originalCatalog });
  h.canvas().props.onChange(latestBoxes);
  await h.drain();
  const before = structuredClone(h.canvas().props.boxes);
  const reload = deferred();
  h.handlers.getImageAnnotations = () => reload.promise;
  const renaming = Promise.resolve(h.canvas().props.onRenameClass(0, "kitty")).catch(() => {});
  await h.drain();
  const renamed = [{ ...originalCatalog[0], name: "kitty" }, originalCatalog[1]];
  assert.equal(h.requests("renameDatasetClass").length, 1);
  assert.deepEqual(canvasCatalog(h), renamed, "a committed PATCH updates the catalog before its reload finishes");
  assert.equal(h.edit.catalogVersion, 6, "the annotation hook inherits the committed catalog while editing remains blocked");
  assert.equal(h.canvas().props.catalogVersion, 6, "the Canvas receives the catalog generation used to close old rename drafts");
  reload.reject(new TypeError("reload unavailable"));
  await renaming;
  await h.drain();
  const message = "이름은 바꿨지만 새로 불러오지 못했어요. 다시 시도해 주세요.";
  assert.ok(content(h.render()).includes(message));
  assert.equal(content(h.render()).includes("저장하지 못해 이름을 바꾸지 않았어요"), false);
  assert.equal(h.canvas().props.readOnly, true);
  assert.deepEqual(h.canvas().props.boxes, before);
  h.keyboardUndo(); h.canvas().props.onUndo(); h.canvas().props.onChange(editedBoxes);
  await h.drain();
  assert.equal(h.requests("saveImageAnnotations").length, 1);
  assert.deepEqual(h.canvas().props.boxes, before);

  h.handlers.getImageAnnotations = async () => { throw new TypeError("reload still unavailable"); };
  await h.press("다시 불러오기");
  assert.equal(h.requests("renameDatasetClass").length, 1);
  assert.equal(h.canvas().props.readOnly, true);
  assert.ok(content(h.render()).includes(message));
  delete h.handlers.getImageAnnotations;
  h.patchImage(101, { version: 10 });
  await h.press("다시 불러오기");
  assert.equal(h.requests("renameDatasetClass").length, 1, "reload retry must never repeat the committed rename");
  assert.deepEqual(h.requests("getImageAnnotations").at(-1).args, [101, undefined, 11]);
  assert.equal(Boolean(h.button("다시 불러오기")), false);
  assert.equal(h.canvas().props.readOnly, false);
  assert.equal(h.edit.catalogVersion, 6);
  assert.deepEqual(canvasCatalog(h), renamed);
  h.handlers.saveImageAnnotations = async (...args) => {
    assert.equal(args[4], h.catalogVersion, "the first post-recovery save must not send the old catalog version");
    return h.defaults.saveImageAnnotations(...args);
  };
  h.canvas().props.onChange(h.canvas().props.boxes.map((box) => ({ ...box, x: box.x + 1 })));
  await h.drain();
  assertSaveVersions(h, 10, 6);
  assert.equal(h.dialog(), undefined);
});

for (const waitingAt of ["flush", "PATCH", "reload"]) {
  test(`switching datasets while rename waits at ${waitingAt} finishes quietly without touching the new image`, async (t) => {
    const h = await harness(t, { role: "admin", classes: originalCatalog });
    const pending = deferred();
    const oldSnapshot = h.annotation(101);
    if (waitingAt === "flush") {
      h.handlers.saveImageAnnotations = () => pending.promise;
      h.canvas().props.onChange(editedBoxes);
    } else if (waitingAt === "PATCH") h.handlers.renameDatasetClass = () => pending.promise;
    else h.handlers.getImageAnnotations = (id) => id === 101 ? pending.promise : h.defaults.getImageAnnotations(id);
    const result = Promise.resolve(h.canvas().props.onRenameClass(0, "kitty"))
      .then(() => ({ ok: true }), (error) => ({ error: error.message }));
    await h.drain();
    h.datasetId = 20;
    h.dataset = { ...h.dataset, id: 20, project_id: 2, workspace_id: 22, image_count: 1 };
    h.catalog = [{ class_id: 0, name: "bird", color: "#778899" }]; h.catalogVersion = 2;
    h.rows = new Map([[201, { ...h.rows.get(101), id: 201, version: 30 }]]);
    h.boxes = new Map([[201, [{ ...initialBox, id: 2010 }]]]);
    await h.drain();
    pending.resolve(waitingAt === "flush" ? { ...oldSnapshot, version: 4, is_modified: true } : {
      ...(waitingAt === "reload" ? oldSnapshot : { class_id: 0, name: "kitty" }),
      catalog_version: 6, classes: [{ class_id: 0, name: "kitty" }, originalCatalog[1]],
    });
    const outcome = await result;
    await h.drain();
    assert.deepEqual(outcome, { ok: true });
    assert.equal(h.requests("renameDatasetClass").length, waitingAt === "flush" ? 0 : 1);
    assert.deepEqual(classNames(canvasCatalog(h)), [{ class_id: 0, name: "bird" }]);
    assert.deepEqual(h.canvas().props.boxes.map(({ id }) => id), [2010]);
    assert.equal(h.requests("getImageAnnotations").filter(({ args }) => args[0] === 101).length, waitingAt === "reload" ? 2 : 1,
      "the abandoned rename cannot reload its old image into the new dataset");
  });
}

for (const cache of ["visited neighbor", "prefetched neighbor", "successful save"]) {
  test(`own rename refreshes older ${cache} annotations without restoring old names or losing edits`, async (t) => {
    const h = await harness(t, { role: "admin", classes: originalCatalog });
    if (cache === "visited neighbor") {
      await selectImage(h, 1); await selectImage(h, 0);
    } else if (cache === "prefetched neighbor") {
      await h.runIdle();
    } else {
      h.canvas().props.onChange(latestBoxes); await h.drain();
    }
    const before = structuredClone(h.canvas().props.boxes);
    await h.canvas().props.onRenameClass(0, "kitty");
    await h.drain();
    const renamed = [{ ...originalCatalog[0], name: "kitty" }, originalCatalog[1]];
    assert.deepEqual(h.canvas().props.boxes.map(({ id, ...box }) => box), before.map(({ id, ...box }) => box));
    assert.deepEqual(canvasCatalog(h), renamed);
    await selectImage(h, 1);
    assert.deepEqual(canvasCatalog(h), renamed);
    await selectImage(h, 0);
    assert.deepEqual(canvasCatalog(h), renamed);
    if (cache !== "successful save") assert.equal(h.requests("getImageAnnotations").filter(({ args }) => args[0] === 102).length, 2,
      "the older neighbor annotation version is reloaded after rename");
    assert.deepEqual(h.canvas().props.boxes.map(({ classId }) => classId), before.map(({ classId }) => classId));
    h.canvas().props.onChange(h.canvas().props.boxes.map((box) => ({ ...box, x: box.x + 1 })));
    await h.drain();
    assert.equal(h.requests("saveImageAnnotations").at(-1).args[4], 6);
  });
}

test("prefetch of a newer catalog waits until that image is displayed before changing current labels", async (t) => {
  const h = await harness(t, { classes: originalCatalog });
  h.catalog = structuredClone(addedCatalog);
  h.catalogVersion = 6;
  h.patchImage(102, { version: 8, box_count: 1 }, [{ ...initialBox, id: 202, class_id: 2 }]);
  await h.runIdle();
  assert.deepEqual(canvasCatalog(h), originalCatalog);
  assert.equal(h.requests("getImageAnnotations").filter(({ args }) => args[0] === 102).length, 1);
  await selectImage(h, 1);
  assert.deepEqual(classNames(canvasCatalog(h)), classNames(addedCatalog));
  assert.equal(h.requests("getImageAnnotations").filter(({ args }) => args[0] === 102).length, 1, "the current-version prefetched snapshot is reusable");
  h.canvas().props.onChange(h.canvas().props.boxes.map((box) => ({ ...box, x: box.x + 1 })));
  await h.drain();
  assertSaveVersions(h, 8, 6);
});

for (const prefetch of ["completed", "still in flight"]) {
  test(`server adoption applies its catalog despite invalidating ${prefetch} older neighbor requests`, async (t) => {
    const h = await harness(t, {
      classes: originalCatalog, image: { label_status: "in_progress", assignee_id: 3 },
      beforeMount(current) {
        current.patchImage(102, { box_count: 1, label_status: "in_progress", assignee_id: 3 }, [{ ...initialBox, id: 202, class_id: 0 }]);
      },
    });
    const oldNeighbor = h.annotation(102);
    const flight = deferred();
    if (prefetch === "still in flight") h.handlers.getImageAnnotations = (id) => id === 102
      ? flight.promise : h.defaults.getImageAnnotations(id);
    await h.runIdle();
    const first = deferred();
    h.handlers.saveImageAnnotations = (...args) => h.requests("saveImageAnnotations").length === 1
      ? first.promise : h.defaults.saveImageAnnotations(...args);
    h.canvas().props.onChange(editedBoxes);
    h.catalog = structuredClone(swappedCatalog); h.catalogVersion = 6;
    h.patchImage(101, { version: 8 }, [{ ...initialBox, id: 105, class_id: 1 }]);
    h.patchImage(102, { version: 8 }, [{ ...initialBox, id: 202, class_id: 1 }]);
    first.reject(new h.ApiError(409, "class reorder", { code: "class_catalog_changed", current: h.annotation(101) }));
    await h.drain();
    await h.press("서버 것으로");
    assert.equal(h.dialog(), undefined);
    assert.deepEqual(classNames(canvasCatalog(h)), swappedCatalog);
    if (prefetch === "still in flight") {
      flight.resolve(oldNeighbor); await h.drain(); delete h.handlers.getImageAnnotations;
      assert.deepEqual(classNames(canvasCatalog(h)), swappedCatalog, "a late lower-version response cannot undo adoption");
    }
    await selectImage(h, 1);
    assert.equal(h.requests("getImageAnnotations").filter(({ args }) => args[0] === 102).length, 2);
    assert.deepEqual(h.canvas().props.boxes.map(({ id, classId }) => [id, classId]), [[202, 1]]);
    h.canvas().props.onChange(h.canvas().props.boxes.map((box) => ({ ...box, x: box.x + 1 })));
    await h.drain();
    assertSaveVersions(h, 8, 6);
  });
}

test("a failed fresh conflict GET retains the versioned original snapshot for safe resolution", async (t) => {
  const h = await catalogConflict(t, { refreshFailure: true });
  assert.notEqual(h.button("내 것 유지").props.disabled, true);
  await h.press("내 것 유지");
  assert.ok(h.dialog());
  assert.equal(h.requests("saveImageAnnotations").length, 1);
  assert.deepEqual(h.canvas().props.boxes, catAndDogEdits);
  delete h.handlers.getImageAnnotations;
  h.patchImage(101, { version: 9 });
  await h.press("내 것 유지");
  assert.equal(h.dialog(), undefined);
  assertSaveVersions(h, 9, 5);
});
