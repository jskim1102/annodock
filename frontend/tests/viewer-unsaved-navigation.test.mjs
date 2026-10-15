import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import ts from "typescript";

const moduleUrl = (source) => `data:text/javascript;base64,${Buffer.from(source).toString("base64")}`;
const settle = () => new Promise((resolve) => setImmediate(resolve));

async function apiModules() {
  const storeModule = moduleUrl(`
    export const clearAuthSession = () => {}, getAuthSnapshot = () => ({}),
      setAuthSession = () => {}, setAuthTokens = () => {}, syncAuthSessionFromStorage = () => {};
  `);
  const compile = async (path, dependencies) => {
    const source = await readFile(new URL(path, import.meta.url), "utf8");
    return moduleUrl(ts.transpileModule(source, {
      compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
    }).outputText.replace(/from "([^"]+)"/g, (_match, name) => `from "${dependencies[name]}"`));
  };
  const authModule = await compile("../src/api/auth.ts", { "../store/auth": storeModule });
  const workspaceModule = moduleUrl(`export const getActiveWorkspaceId = () => null;`);
  const clientModule = await compile("../src/api/client.ts", {
    "./auth": authModule, "../store/auth": storeModule, "../store/workspace": workspaceModule,
  });
  return { authModule, clientModule, ...await import(clientModule), ...await import(authModule) };
}

const api = await apiModules();

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

async function harness(t, save) {
  const originalWindow = globalThis.window;
  const listeners = new Map();
  const destinations = [];
  globalThis.window = {
    addEventListener(type, callback) {
      if (!listeners.has(type)) listeners.set(type, new Set());
      listeners.get(type).add(callback);
    },
    removeEventListener(type, callback) { listeners.get(type)?.delete(callback); },
    location: { assign: (path) => destinations.push(path) },
  };
  const effects = [];
  const cleanups = [];
  const slots = [];
  let slotIndex = 0;
  let renderingHook = false;
  let mounted = false;
  const key = `__viewerNavigation${Math.random().toString(16).slice(2)}`;
  const react = {
    useState: (value) => {
      const initial = () => typeof value === "function" ? value() : value;
      if (!renderingHook) return [initial(), () => {}];
      const index = slotIndex++;
      if (!mounted) slots[index] = initial();
      return [slots[index], (next) => {
        slots[index] = typeof next === "function" ? next(slots[index]) : next;
      }];
    },
    useRef: (current) => {
      if (!renderingHook) return { current };
      const index = slotIndex++;
      if (!mounted) slots[index] = { current };
      return slots[index];
    },
    useCallback: (callback) => callback,
    useMemo: (callback) => callback(),
    useEffect: (callback) => { if (renderingHook && !mounted) effects.push(callback); },
  };
  globalThis[key] = { react };
  t.after(() => {
    cleanups.forEach((cleanup) => cleanup?.());
    globalThis.window = originalWindow;
    delete globalThis[key];
  });
  const reactModule = moduleUrl(`
    const hooks = globalThis[${JSON.stringify(key)}].react;
    export const { useState, useRef, useCallback, useMemo, useEffect } = hooks;
  `);
  const source = await readFile(new URL("../src/hooks/useAnnotationEdit.ts", import.meta.url), "utf8");
  const compiled = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
  }).outputText
    .replace('from "react"', `from "${reactModule}"`)
    .replace('from "../api/client"', `from "${api.clientModule}"`)
    .replace('from "../api/auth"', `from "${api.authModule}"`);
  const { useAnnotationEdit } = await import(moduleUrl(compiled));
  const getEdit = () => {
    slotIndex = 0;
    renderingHook = true;
    const edit = useAnnotationEdit([], save);
    renderingHook = false;
    mounted = true;
    return edit;
  };
  getEdit();
  effects.splice(0).forEach((effect) => cleanups.push(effect()));

  const beforeUnload = () => {
    const event = { prevented: false, preventDefault() { this.prevented = true; } };
    listeners.get("beforeunload")?.forEach((callback) => callback(event));
    return event.prevented;
  };

  const viewerSource = await readFile(new URL("../src/pages/Viewer.tsx", import.meta.url), "utf8");
  // Render the real component's element tree with inert effects. Its logo click
  // callback still uses the real save hook and all asynchronous save behavior.
  globalThis[key].getEdit = getEdit;
  const componentModule = moduleUrl(`
    const h = globalThis[${JSON.stringify(key)}];
    export { ApiError } from "${api.clientModule}";
    export const useAuthSession = () => ({ user: { id: 1 } });
    export const getWorkspaceMembers = async () => ({ items: [] });
    export const WorkspaceDialog = () => {};
    export const assignImage = () => {}, claimDatasetImage = () => {}, completeImage = () => {},
      uncompleteImage = () => {}, getImage = () => {};
    export const useAnnotationEdit = () => h.getEdit();
    export const jsx = (type, props) => ({ type, props });
    export const jsxs = jsx;
    export const useAuthenticatedObjectUrl = () => ({ url: null });
    export const appHref = (path) => path;
    export const CLASS_COLOR_PRESETS = [];
    export const getDataset = () => {}, getDatasetClasses = () => {},
      getProject = () => {}, getDatasetImages = () => {}, getImageAnnotations = () => {},
      imageResourceUrl = () => {}, renameDatasetClass = () => {}, updateProjectClass = () => {},
      saveImageAnnotations = () => {}, prefetchAuthenticatedResource = () => {},
      Brand = () => {}, BboxCanvas = () => {}, ThemeToggle = () => {}, ThumbStrip = () => {},
      ClassColorPicker = () => {}, Icon = () => {};
  `);
  const viewerCompiled = ts.transpileModule(viewerSource, {
    compilerOptions: {
      module: ts.ModuleKind.ESNext,
      target: ts.ScriptTarget.ES2022,
      jsx: ts.JsxEmit.ReactJSX,
    },
  }).outputText.replace(/from "([^"]+)"/g, (_match, path) => (
    `from "${path === "react" ? reactModule : componentModule}"`
  ));
  const { ViewerPage } = await import(moduleUrl(viewerCompiled));
  const renderViewer = () => ViewerPage({ datasetId: 1 });
  function findElement(element, predicate) {
    if (!element || typeof element !== "object") return null;
    if (predicate(element)) return element;
    const children = [element.props?.children].flat(Infinity);
    return children.map((child) => findElement(child, predicate)).find(Boolean) ?? null;
  }
  const logo = findElement(renderViewer(), (element) => element.props?.className === "viewer-brand");
  assert.ok(logo);
  const clickLogo = (overrides = {}) => {
    const event = {
      button: 0,
      prevented: false,
      preventDefault() { this.prevented = true; },
      ...overrides,
    };
    logo.props.onClick(event);
    return event;
  };
  const canvasSource = await readFile(new URL("../src/components/BboxCanvas.tsx", import.meta.url), "utf8");
  const canvasCompiled = ts.transpileModule(canvasSource, {
    compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX },
  }).outputText.replace(/from "([^"]+)"/g, (_match, path) => (
    `from "${path === "react" ? reactModule : componentModule}"`
  ));
  const { BboxCanvas } = await import(moduleUrl(canvasCompiled));
  const saveControl = () => findElement(BboxCanvas({
    imageSrc: "test.jpg", imageWidth: 100, imageHeight: 100, boxes: [], classes: [],
    saveState: getEdit().saveState, undoDepth: 0, onRetrySave: getEdit().retry,
  }), (element) => element.props?.className?.startsWith("save-"));
  return {
    get edit() { return getEdit(); }, beforeUnload, clickLogo, destinations, saveControl,
    alert: () => findElement(renderViewer(), (element) => element.props?.role === "alert"),
  };
}

test("browser exit is protected during saves and after failure, then released after retry", async (t) => {
  const first = deferred();
  const retry = deferred();
  const saves = [first, retry];
  const state = await harness(t, () => saves.shift().promise);
  assert.equal(state.beforeUnload(), false);

  state.edit.applyChange([{ id: 1, classId: 0, x: 1, y: 1, width: 10, height: 10 }]);
  assert.equal(state.beforeUnload(), true, "protect edits before React can rerender");
  first.reject(new Error("server failed"));
  await settle();
  assert.equal(state.beforeUnload(), true);

  state.edit.retry();
  retry.resolve();
  await settle();
  assert.equal(state.beforeUnload(), false);
});

test("the viewer logo stays on the page if saving fails and navigates after a successful retry", async (t) => {
  let failing = true;
  const payloads = [];
  const state = await harness(t, async (boxes) => {
    payloads.push(boxes);
    if (failing) throw new Error("server failed");
  });
  const boxes = [{ id: 1, classId: 0, x: 1, y: 1, width: 10, height: 10 }];
  state.edit.applyChange(boxes);
  await settle();

  assert.equal(state.clickLogo().prevented, true);
  await settle();
  assert.deepEqual(state.destinations, []);
  assert.equal(state.beforeUnload(), true);

  failing = false;
  state.clickLogo();
  await settle();
  assert.deepEqual(state.destinations, ["/projects"]);
  assert.equal(state.beforeUnload(), false);
  assert.deepEqual(payloads.at(-1), boxes);
});

test("the viewer logo waits for in-flight and queued edits before leaving", async (t) => {
  const first = deferred();
  const second = deferred();
  const saves = [first, second];
  const payloads = [];
  const state = await harness(t, (boxes) => {
    payloads.push(boxes);
    return saves.shift().promise;
  });
  state.edit.applyChange([{ id: 1 }]);
  state.clickLogo();
  state.edit.applyChange([{ id: 2 }]);
  first.resolve();
  await settle();
  assert.deepEqual(state.destinations, []);
  assert.equal(state.beforeUnload(), true);
  assert.deepEqual(payloads, [[{ id: 1 }], [{ id: 2 }]]);

  second.resolve();
  await settle();
  assert.deepEqual(state.destinations, ["/projects"]);
  assert.equal(state.beforeUnload(), false);
});

test("opening the viewer logo in another tab keeps the existing page intact", async (t) => {
  const state = await harness(t, async () => {});
  assert.equal(state.clickLogo({ ctrlKey: true }).prevented, false);
  await settle();
  assert.deepEqual(state.destinations, []);
});

test("a rejected annotation displays the server reason and releases navigation without retrying", async (t) => {
  const message = "데이터셋에 없는 클래스는 저장할 수 없습니다.";
  let calls = 0;
  const state = await harness(t, async () => {
    calls += 1;
    await api.responseOrThrow(new Response(JSON.stringify({
      detail: { code: "unknown_annotation_class", message, class_ids: [999] },
    }), { status: 422 }));
  });
  state.edit.applyChange([{ id: 1, classId: 999 }]);
  await settle();
  assert.equal(state.edit.saveState, "rejected");
  assert.equal(state.edit.saveError, message);
  assert.equal(state.alert()?.props.children, message);
  assert.equal(state.saveControl().props.disabled, true, "rejected saves cannot be retried");
  assert.equal(state.beforeUnload(), false);
  assert.equal(await state.edit.flush(), true);
  state.clickLogo();
  await settle();
  assert.deepEqual(state.destinations, ["/projects"]);
  assert.equal(calls, 1);
  state.edit.reset([]);
  assert.equal(state.edit.saveError, null);
  assert.equal(state.edit.saveState, "saved");
});

test("a logo click waiting for a 4xx response is released when the save is rejected", async (t) => {
  const response = deferred();
  const state = await harness(t, () => response.promise);
  state.edit.applyChange([{ id: 1 }]);
  state.clickLogo();
  assert.equal(state.beforeUnload(), true);
  response.reject(new api.ApiError(403, "이 데이터를 수정할 수 없습니다."));
  await settle();
  assert.deepEqual(state.destinations, ["/projects"]);
  assert.equal(state.beforeUnload(), false);
});

for (const [name, failure] of [
  ["network", new TypeError("Failed to fetch")],
  ["5xx", new api.ApiError(503, "잠시 후 다시 시도해 주세요.")],
]) {
  test(`${name} failures keep edits protected and permit a successful retry`, async (t) => {
    let failing = true;
    const state = await harness(t, async () => { if (failing) throw failure; });
    state.edit.applyChange([{ id: 1 }]);
    await settle();
    assert.equal(state.edit.saveState, "error");
    assert.equal(state.edit.saveError, failure.message);
    assert.equal(state.saveControl().props.disabled, false);
    assert.equal(state.beforeUnload(), true);
    state.clickLogo();
    await settle();
    assert.deepEqual(state.destinations, []);
    failing = false;
    state.edit.retry();
    await settle();
    assert.equal(state.edit.saveState, "saved");
    assert.equal(state.edit.saveError, null);
    assert.equal(state.beforeUnload(), false);
  });
}

test("a 4xx response preserves newer queued edits until their save finishes", async (t) => {
  const first = deferred();
  const second = deferred();
  const saves = [first, second];
  const payloads = [];
  const state = await harness(t, (boxes) => { payloads.push(boxes); return saves.shift().promise; });
  state.edit.applyChange([{ id: 1, classId: 999 }]);
  state.edit.applyChange([{ id: 1, classId: 0 }]);
  first.reject(new api.ApiError(422, "클래스가 없습니다."));
  await settle();
  assert.deepEqual(payloads, [[{ id: 1, classId: 999 }], [{ id: 1, classId: 0 }]]);
  assert.equal(state.beforeUnload(), true);
  second.resolve();
  await settle();
  assert.equal(state.edit.saveState, "saved");
  assert.equal(state.edit.saveError, null);
  assert.equal(state.beforeUnload(), false);
});

test("an authentication 4xx also releases rejected edits", async (t) => {
  const state = await harness(t, async () => { throw new api.AuthApiError(401, "다시 로그인해 주세요."); });
  state.edit.applyChange([{ id: 1 }]);
  await settle();
  assert.equal(state.edit.saveState, "rejected");
  assert.equal(state.edit.saveError, "다시 로그인해 주세요.");
  assert.equal(state.beforeUnload(), false);
  assert.equal(await state.edit.flush(), true);
});
