import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import ts from "typescript";

const moduleUrl = (source) => `data:text/javascript;base64,${Buffer.from(source).toString("base64")}`;
const settle = () => new Promise((resolve) => setImmediate(resolve));

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

async function harness(t, save) {
  const originalWindow = globalThis.window;
  const slots = [];
  const effects = [];
  const cleanups = new Map();
  const listeners = new Map();
  let index = 0;
  let mounted = false;
  let currentSave = save;
  const key = `__annotationScope${Math.random().toString(16).slice(2)}`;
  const context = { workspaceId: 11 };
  globalThis[key] = context;
  globalThis.window = {
    addEventListener(type, fn) {
      if (!listeners.has(type)) listeners.set(type, new Set());
      listeners.get(type).add(fn);
    },
    removeEventListener(type, fn) { listeners.get(type)?.delete(fn); },
  };
  context.react = {
    useState(initial) {
      const slot = index++;
      if (!mounted) slots[slot] = typeof initial === "function" ? initial() : initial;
      return [slots[slot], (value) => { slots[slot] = typeof value === "function" ? value(slots[slot]) : value; }];
    },
    useRef(current) {
      const slot = index++;
      if (!mounted) slots[slot] = { current };
      return slots[slot];
    },
    useCallback(fn) { return fn; },
    useEffect(fn, deps) {
      const slot = index++;
      if (!mounted || deps.some((value, offset) => value !== slots[slot]?.[offset])) {
        effects.push(() => {
          cleanups.get(slot)?.();
          cleanups.set(slot, fn());
          slots[slot] = deps;
        });
      }
    },
  };
  const reactModule = moduleUrl(`
    export const { useState, useRef, useCallback, useEffect } = globalThis[${JSON.stringify(key)}].react;
  `);
  const apiModule = moduleUrl(`
    export class ApiError extends Error { constructor(status, message, detail) { super(message); this.status = status; this.detail = detail; } }
    export class AuthApiError extends ApiError {}
    export const captureWorkspaceId = () => globalThis[${JSON.stringify(key)}].workspaceId;
  `);
  const source = await readFile(new URL("../src/hooks/useAnnotationEdit.ts", import.meta.url), "utf8");
  const compiled = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
  }).outputText.replace(/from "([^"]+)"/g, (_match, dependency) => `from "${dependency === "react" ? reactModule : apiModule}"`);
  const { useAnnotationEdit } = await import(moduleUrl(compiled));
  const render = (nextSave = currentSave) => {
    currentSave = nextSave;
    index = 0;
    const edit = useAnnotationEdit([], currentSave);
    mounted = true;
    effects.splice(0).forEach((effect) => effect());
    return edit;
  };
  const unmount = () => { cleanups.forEach((cleanup) => cleanup?.()); cleanups.clear(); };
  t.after(() => {
    unmount();
    globalThis.window = originalWindow;
    delete globalThis[key];
  });
  render();
  return {
    render,
    unmount,
    switchWorkspace(workspaceId, nextSave) { context.workspaceId = workspaceId; return render(nextSave); },
    beforeUnload() {
      const event = { prevented: false, preventDefault() { this.prevented = true; } };
      listeners.get("beforeunload")?.forEach((fn) => fn(event));
      return event.prevented;
    },
  };
}

test("edits queued during a save keep their original callback and workspace after a switch", async (t) => {
  const first = deferred();
  const calls = [];
  const saveA = async (boxes, workspaceId) => {
    calls.push({ callback: "A", boxes, workspaceId });
    if (calls.length === 1) await first.promise;
  };
  const saveB = async (boxes, workspaceId) => { calls.push({ callback: "B", boxes, workspaceId }); };
  const state = await harness(t, saveA);
  state.render().applyChange([{ id: 1 }]);
  state.render().applyChange([{ id: 2 }]);
  state.switchWorkspace(22, saveB);
  first.resolve();
  await settle();
  assert.deepEqual(calls, [
    { callback: "A", boxes: [{ id: 1 }], workspaceId: 11 },
    { callback: "A", boxes: [{ id: 2 }], workspaceId: 11 },
  ]);
  assert.equal(state.beforeUnload(), false);
});

test("a failed edit retries its captured callback and scope after the active workspace changes", async (t) => {
  const calls = [];
  let failing = true;
  const saveA = async (boxes, workspaceId) => {
    calls.push({ callback: "A", boxes, workspaceId });
    if (failing) throw new Error("connection failed");
  };
  const saveB = async (boxes, workspaceId) => { calls.push({ callback: "B", boxes, workspaceId }); };
  const state = await harness(t, saveA);
  state.render().applyChange([{ id: 1 }]);
  await settle();
  assert.equal(state.beforeUnload(), true);
  state.switchWorkspace(22, saveB);
  failing = false;
  state.render().retry();
  await settle();
  assert.deepEqual(calls, [
    { callback: "A", boxes: [{ id: 1 }], workspaceId: 11 },
    { callback: "A", boxes: [{ id: 1 }], workspaceId: 11 },
  ]);
  assert.equal(state.render().saveState, "saved");
  assert.equal(state.beforeUnload(), false);
});

test("newer queued edits survive an older failure and preserve the callback captured while editing", async (t) => {
  const first = deferred();
  const calls = [];
  const saveA = async (boxes, workspaceId) => {
    calls.push({ callback: "A", boxes, workspaceId });
    if (calls.length === 1) await first.promise;
  };
  const saveB = async (boxes, workspaceId) => { calls.push({ callback: "B", boxes, workspaceId }); };
  const state = await harness(t, saveA);
  state.render().applyChange([{ id: 1 }]);
  state.render().applyChange([{ id: 2 }]);
  state.switchWorkspace(22, saveB);
  first.reject(new Error("server failed"));
  await settle();
  state.render().retry();
  await settle();
  assert.deepEqual(calls, [
    { callback: "A", boxes: [{ id: 1 }], workspaceId: 11 },
    { callback: "A", boxes: [{ id: 2 }], workspaceId: 11 },
  ]);
});

test("unmounting the old viewer does not move its pending save into the newly selected workspace", async (t) => {
  const first = deferred();
  const calls = [];
  const state = await harness(t, async (boxes, workspaceId) => {
    calls.push({ boxes, workspaceId });
    if (calls.length === 1) await first.promise;
  });
  state.render().applyChange([{ id: 1 }]);
  state.render().applyChange([{ id: 2 }]);
  state.switchWorkspace(22);
  state.unmount();
  first.resolve();
  await settle();
  assert.deepEqual(calls, [
    { boxes: [{ id: 1 }], workspaceId: 11 },
    { boxes: [{ id: 2 }], workspaceId: 11 },
  ]);
});

test("reset does not revive an old failed edit or reuse its scope for a new image", async (t) => {
  const first = deferred();
  const calls = [];
  const state = await harness(t, async (boxes, workspaceId) => {
    calls.push({ callback: "A", boxes, workspaceId });
    await first.promise;
  });
  state.render().applyChange([{ id: 1 }]);
  state.switchWorkspace(22, async (boxes, workspaceId) => { calls.push({ callback: "B", boxes, workspaceId }); });
  state.render().reset([]);
  first.reject(new Error("old scope failed"));
  await settle();
  assert.equal(state.render().saveState, "saved");
  state.render().applyChange([{ id: 2 }]);
  await settle();
  assert.deepEqual(calls, [
    { callback: "A", boxes: [{ id: 1 }], workspaceId: 11 },
    { callback: "B", boxes: [{ id: 2 }], workspaceId: 22 },
  ]);
});

test("an explicit headerless personal scope remains null through a queued save", async (t) => {
  const first = deferred();
  const calls = [];
  const save = async (boxes, workspaceId) => {
    calls.push({ boxes, workspaceId });
    if (calls.length === 1) await first.promise;
  };
  const state = await harness(t, save);
  state.switchWorkspace(null);
  state.render().applyChange([{ id: 1 }]);
  state.render().applyChange([{ id: 2 }]);
  state.switchWorkspace(22);
  first.resolve();
  await settle();
  assert.deepEqual(calls.map(({ workspaceId }) => workspaceId), [null, null]);
});

test("Viewer's captured save callback keeps the original image, workspace, and catalog version", async (t) => {
  const originalWindow = globalThis.window;
  globalThis.window = { addEventListener() {}, removeEventListener() {} };
  const key = `__viewerSaveScope${Math.random().toString(16).slice(2)}`;
  const context = { calls: [], save: null };
  globalThis[key] = context;
  const slots = [];
  const effects = [];
  const cleanups = new Map();
  t.after(() => {
    cleanups.forEach((cleanup) => cleanup?.());
    globalThis.window = originalWindow;
    delete globalThis[key];
  });
  let index = 0;
  let mounted = false;
  const changed = (previous, deps) => !previous || deps.some((value, offset) => value !== previous[offset]);
  const memo = (create, deps) => {
    const slot = index++;
    if (!mounted || changed(slots[slot]?.deps, deps)) slots[slot] = { deps, value: create() };
    return slots[slot].value;
  };
  context.react = {
    useState(initial) {
      const slot = index++;
      if (!mounted) slots[slot] = typeof initial === "function" ? initial() : initial;
      return [slots[slot], (value) => { slots[slot] = typeof value === "function" ? value(slots[slot]) : value; }];
    },
    useRef(current) {
      const slot = index++;
      if (!mounted) slots[slot] = { current };
      return slots[slot];
    },
    useCallback(fn, deps) { return memo(() => fn, deps); },
    useMemo(create, deps) { return memo(create, deps); },
    useEffect(fn, deps) {
      const slot = index++;
      if (!mounted || changed(slots[slot], deps)) effects.push(() => {
        cleanups.get(slot)?.();
        cleanups.set(slot, fn());
        slots[slot] = deps;
      });
    },
  };
  const reactModule = moduleUrl(`export const {useState,useRef,useCallback,useMemo,useEffect} = globalThis[${JSON.stringify(key)}].react;`);
  const componentModule = moduleUrl(`
    const context = globalThis[${JSON.stringify(key)}];
    const flush = async () => true, reset = () => {};
    export const useAnnotationEdit = (_boxes, save) => {
      context.save = save;
      return { boxes: [], flush, reset, undoDepth: 0, saveState: "saved", saveError: null };
    };
    export class ApiError extends Error { constructor(status, message, detail) { super(message); this.status = status; this.detail = detail; } }
    export const useAuthSession = () => ({ user: { id: 1 } });
    export const getWorkspaceMembers = async () => ({ items: [] });
    export const WorkspaceDialog = () => {};
    export const assignImage = () => {}, claimDatasetImage = () => {}, completeImage = () => {},
      uncompleteImage = () => {}, getImage = () => {};
    export const getDataset = async (id) => ({ id, project_id: 1, workspace_id: id + 1,
      my_role: "owner", image_count: 1, splits: [] });
    export const getDatasetClasses = async () => ({ classes: [], catalog_version: 9 });
    export const getProject = async () => ({ name: "fixture", classes: [] });
    export const getDatasetImages = async (id) => ({ total: 1, items: [{ id: id * 10 + 1,
      width: 100, height: 80, version: 7, label_status: "labeled", assignee_id: null,
      labeled_by: null, labeled_at: null }] });
    export const getImageAnnotations = async (id) => ({ image_id: id, width: 100, height: 80,
      boxes: [], version: 7, catalog_version: 9, classes: [], label_status: "labeled", assignee_id: null });
    export const saveImageAnnotations = async (...args) => { context.calls.push(args); return {
      image_id: args[0], boxes: [], is_modified: true, version: args[3] + 1,
      catalog_version: args[4], classes: [], label_status: "labeled", assignee_id: null }; };
    export const imageResourceUrl = (id) => "/api/images/" + id + "/file";
    export const useAuthenticatedObjectUrl = () => ({ url: null });
    export const appHref = (path) => path;
    export const adminHomePath = async () => "/projects";
    export const jsx = (type, props) => ({ type, props }), jsxs = jsx;
    export const CLASS_COLOR_PRESETS = [];
    export const renameDatasetClass = () => {}, updateProjectClass = () => {},
      prefetchAuthenticatedResource = () => {}, Brand = () => {}, BboxCanvas = () => {},
      ThemeToggle = () => {}, ThumbStrip = () => {};
  `);
  const source = await readFile(new URL("../src/pages/Viewer.tsx", import.meta.url), "utf8");
  const compiled = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX },
  }).outputText.replace(/from "([^"]+)"/g, (_match, dependency) => `from "${dependency === "react" ? reactModule : componentModule}"`);
  const { ViewerPage } = await import(moduleUrl(compiled));
  const load = async (datasetId) => {
    for (let render = 0; render < 6; render += 1) {
      index = 0;
      ViewerPage({ datasetId });
      mounted = true;
      effects.splice(0).forEach((effect) => effect());
      await settle();
    }
  };
  await load(10);
  const originalSave = context.save;
  await load(20);
  await originalSave([{ id: 1, classId: 3, x: 10, y: 8, width: 20, height: 16 }], 11, 7, 9);
  await context.save([], 22, 20, 15);
  assert.deepEqual(context.calls, [
    [101, [{ class_id: 3, cx: .2, cy: .2, w: .2, h: .2 }], 11, 7, 9],
    [201, [], 22, 20, 15],
  ]);
});
