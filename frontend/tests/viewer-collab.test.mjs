import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import ts from "typescript";

const moduleUrl = (source) => `data:text/javascript;base64,${Buffer.from(source).toString("base64")}`;
const settle = () => new Promise((resolve) => setImmediate(resolve));
const box = (id, x = id) => ({ id, classId: 0, x, y: 5, width: 10, height: 10 });
const initialBoxes = [box(1)];
const firstEdit = [box(1, 15)];
const latestEdit = [box(1, 25), box(2)];
const serverBoxes = [box(91, 40)];
const currentServer = {
  version: 12,
  catalog_version: 4,
  boxes: [{ id: 91, class_id: 0, cx: 0.45, cy: 0.1, w: 0.1, h: 0.1 }],
  label_status: "in_progress",
  assignee_id: 3,
  classes: [{ class_id: 0, name: "person" }],
};

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

async function harness(t, save, options = {}) {
  // Same hook-slot/effect model as workspace-annotation-scope.test.mjs,
  // with mutable collaboration options and structured ApiError details.
  const originalWindow = globalThis.window;
  const originalHTMLElement = globalThis.HTMLElement;
  const slots = [];
  const effects = [];
  const cleanups = new Map();
  const listeners = new Map();
  let index = 0;
  let mounted = false;
  let currentSave = save;
  let currentOptions = options;
  const key = `__viewerCollab${Math.random().toString(16).slice(2)}`;
  const context = { workspaceId: 11 };
  globalThis[key] = context;
  globalThis.HTMLElement = class HTMLElement {};
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
      if (!mounted || !deps || deps.some((value, offset) => value !== slots[slot]?.[offset])) {
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
    export class ApiError extends Error {
      constructor(status, message, detail) { super(message); this.status = status; this.detail = detail; }
    }
    export class AuthApiError extends ApiError {}
    export const captureWorkspaceId = () => globalThis[${JSON.stringify(key)}].workspaceId;
  `);
  const { ApiError } = await import(apiModule);
  const source = await readFile(new URL("../src/hooks/useAnnotationEdit.ts", import.meta.url), "utf8");
  const compiled = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
  }).outputText.replace(/from "([^"]+)"/g, (_match, dependency) => `from "${dependency === "react" ? reactModule : apiModule}"`);
  const { useAnnotationEdit } = await import(moduleUrl(compiled));
  const render = () => {
    index = 0;
    const edit = useAnnotationEdit([], currentSave, currentOptions);
    mounted = true;
    effects.splice(0).forEach((effect) => effect());
    return edit;
  };
  t.after(() => {
    cleanups.forEach((cleanup) => cleanup?.());
    globalThis.window = originalWindow;
    if (originalHTMLElement === undefined) delete globalThis.HTMLElement;
    else globalThis.HTMLElement = originalHTMLElement;
    delete globalThis[key];
  });
  render();
  return {
    ApiError,
    render,
    setOptions(next) { currentOptions = next; return render(); },
    switchWorkspace(workspaceId, nextSave = currentSave) {
      context.workspaceId = workspaceId;
      currentSave = nextSave;
      return render();
    },
    beforeUnload() {
      const event = { prevented: false, preventDefault() { this.prevented = true; } };
      listeners.get("beforeunload")?.forEach((fn) => fn(event));
      return event.prevented;
    },
    keyboardUndo() {
      const event = { ctrlKey: true, key: "z", target: null, preventDefault() {} };
      listeners.get("keydown")?.forEach((fn) => fn(event));
    },
  };
}

async function conflictedHook(t, afterConflict = async () => ({ version: 16 }), detail = {
  code: "version_conflict", current: currentServer,
}) {
  const first = deferred();
  const calls = [];
  const h = await harness(t, async (boxes, workspaceId, expectedVersion, expectedCatalogVersion) => {
    calls.push({ boxes, workspaceId, expectedVersion, expectedCatalogVersion });
    return calls.length === 1 ? first.promise : afterConflict();
  });
  h.render().reset(initialBoxes, 7, 4);
  h.render().applyChange(firstEdit);
  h.render().applyChange(latestEdit);
  first.reject(new h.ApiError(409, "요청 실패 (409)", detail));
  await settle();
  return { h, calls };
}

test("queued annotation edits use each preceding save response version when dequeued", async (t) => {
  const first = deferred();
  const second = deferred();
  const calls = [];
  const h = await harness(t, (boxes, workspaceId, expectedVersion, expectedCatalogVersion) => {
    calls.push({ boxes, workspaceId, expectedVersion, expectedCatalogVersion });
    return calls.length === 1 ? first.promise : second.promise;
  });
  h.render().reset(initialBoxes, 7, 4);
  h.render().applyChange(firstEdit);
  h.render().applyChange([box(1, 20)]);
  h.render().applyChange(latestEdit);
  assert.deepEqual(calls, [{ boxes: firstEdit, workspaceId: 11, expectedVersion: 7, expectedCatalogVersion: 4 }]);
  first.resolve({ version: 8 });
  await settle();
  assert.deepEqual(calls, [
    { boxes: firstEdit, workspaceId: 11, expectedVersion: 7, expectedCatalogVersion: 4 },
    { boxes: latestEdit, workspaceId: 11, expectedVersion: 8, expectedCatalogVersion: 4 },
  ]);
  second.resolve({ version: 9 });
  await settle();
  assert.equal(await h.render().flush(), true);
  assert.equal(h.render().saveState, "saved");
  assert.deepEqual(h.render().boxes, latestEdit);
  assert.equal(h.render().catalogVersion, 4);
});

test("explicit catalog metadata changes preserve boxes while an authoritative reset replaces the snapshot", async (t) => {
  const calls = [];
  const h = await harness(t, async (boxes, workspaceId, expectedVersion, expectedCatalogVersion) => {
    calls.push({ boxes, workspaceId, expectedVersion, expectedCatalogVersion });
    return { version: expectedVersion + 1 };
  });
  h.render().reset(initialBoxes, 7, 4);
  h.render().applyChange(firstEdit);
  assert.equal(await h.render().flush(), true);
  assert.equal(h.render().catalogVersion, 4);
  h.render().setCatalogVersion(6);
  assert.equal(h.render().catalogVersion, 6);
  assert.deepEqual(h.render().boxes, firstEdit);
  assert.equal(h.render().undoDepth, 1);
  assert.equal(calls.length, 1, "updating catalog metadata does not save unchanged boxes");
  h.render().undo();
  await settle();
  assert.deepEqual(calls.at(-1), {
    boxes: initialBoxes, workspaceId: 11, expectedVersion: 8, expectedCatalogVersion: 6,
  });
  h.render().reset(serverBoxes);
  assert.equal(h.render().catalogVersion, 0, "reset's optional catalog version defaults to zero");
  assert.equal(h.render().undoDepth, 0);
});

test("a queued edit retains its callback workspace and captured catalog after the active context changes", async (t) => {
  const first = deferred();
  const calls = [], wrongScopeCalls = [];
  const h = await harness(t, async (boxes, workspaceId, expectedVersion, expectedCatalogVersion) => {
    calls.push({ boxes, workspaceId, expectedVersion, expectedCatalogVersion });
    return calls.length === 1 ? first.promise : { version: 9, catalog_version: 4 };
  });
  h.render().reset(initialBoxes, 7, 4);
  h.render().applyChange(firstEdit);
  h.render().applyChange(latestEdit);
  h.switchWorkspace(22, async (...args) => { wrongScopeCalls.push(args); return { version: 99 }; });
  h.render().setCatalogVersion(9);
  first.resolve({ version: 8, catalog_version: 4 });
  await settle();
  assert.deepEqual(calls, [
    { boxes: firstEdit, workspaceId: 11, expectedVersion: 7, expectedCatalogVersion: 4 },
    { boxes: latestEdit, workspaceId: 11, expectedVersion: 8, expectedCatalogVersion: 4 },
  ]);
  assert.deepEqual(wrongScopeCalls, []);
  assert.equal(h.render().catalogVersion, 9, "old save completion cannot roll back active catalog metadata");
  assert.equal(await h.render().flush(), true);
});

test("version conflict pauses flush and retains raw server details and the latest queued edit", async (t) => {
  const { h, calls } = await conflictedHook(t);
  assert.equal(calls.length, 1, "a 409 must not drain the newer queued edit automatically");
  assert.deepEqual(h.render().conflict.current, currentServer);
  assert.equal(h.render().conflict.code, "version_conflict");
  assert.deepEqual(h.render().boxes, latestEdit);
  assert.notEqual(h.render().saveState, "rejected");
  assert.equal(await h.render().flush(), false);
  assert.equal(h.beforeUnload(), true);
  h.render().retry();
  await settle();
  assert.equal(calls.length, 1, "retry waits for an explicit conflict choice");
  assert.equal(await h.render().flush(), false);
});

for (const code of ["version_conflict", "class_catalog_changed"]) {
test(`server resolution of ${code} discards local edits and adopts both server versions`, async (t) => {
  const { h, calls } = await conflictedHook(t, undefined, {
    code, current: { ...currentServer, catalog_version: 9 },
  });
  assert.equal(h.render().undoDepth, 2);
  assert.equal(typeof h.render().resolveConflict, "function");
  assert.equal(await h.render().resolveConflict("server", { boxes: serverBoxes, version: 15, catalog_version: 9 }), true);
  assert.deepEqual(h.render().boxes, serverBoxes);
  assert.equal(h.render().catalogVersion, 9);
  assert.equal(h.render().conflict, null);
  assert.equal(h.render().undoDepth, 0);
  assert.equal(calls.length, 1, "accepting server state must not issue a PUT");
  assert.equal(await h.render().flush(), true);
  assert.equal(h.beforeUnload(), false);
  const editedServer = [box(91, 55)];
  h.render().applyChange(editedServer);
  await settle();
  assert.deepEqual(calls.at(-1), { boxes: editedServer, workspaceId: 11, expectedVersion: 15, expectedCatalogVersion: 9 });
});
}

for (const [name, code, current, fetchedCatalog] of [
  ["class_catalog_changed even with equal versions", "class_catalog_changed", currentServer, 4],
  ["class_catalog_changed with a newer catalog", "class_catalog_changed", { ...currentServer, catalog_version: 5 }, 5],
  ["missing current classes", "version_conflict", { ...currentServer, classes: undefined }, 4],
  ["missing current catalog version", "version_conflict", { ...currentServer, catalog_version: undefined }, 4],
  ["newer current catalog", "version_conflict", { ...currentServer, catalog_version: 5 }, 5],
  ["older current catalog", "version_conflict", { ...currentServer, catalog_version: 3 }, 3],
  ["fetched catalog differs from local and conflict", "version_conflict", currentServer, 5],
  ["missing fetched catalog", "version_conflict", currentServer, undefined],
]) {
  test(`local resolution rejects ${name} without changing boxes history or catalog`, async (t) => {
    const { h, calls } = await conflictedHook(t, undefined, { code, current });
    const resolved = await h.render().resolveConflict("local", {
      boxes: serverBoxes, version: 15, catalog_version: fetchedCatalog,
    });
    assert.equal(resolved, false);
    assert.equal(calls.length, 1, "a rejected catalog choice must not send a replacement");
    assert.deepEqual(h.render().boxes, latestEdit);
    assert.equal(h.render().undoDepth, 2, "invalid catalog choices preserve undo");
    assert.equal(h.render().catalogVersion, 4);
    assert.deepEqual(h.render().conflict, { code, current });
    assert.equal(await h.render().flush(), false);
    assert.equal(h.beforeUnload(), true);
    h.render().retry();
    await settle();
    assert.equal(calls.length, 1);
  });
}

test("local resolution resends the latest local boxes using fetched version and captured workspace", async (t) => {
  const replacement = deferred();
  const { h, calls } = await conflictedHook(t, () => replacement.promise);
  const wrongScopeCalls = [];
  h.switchWorkspace(22, async (...args) => { wrongScopeCalls.push(args); return { version: 99 }; });
  assert.equal(typeof h.render().resolveConflict, "function");
  const resolved = h.render().resolveConflict("local", { boxes: serverBoxes, version: 15, catalog_version: 4 });
  await settle();
  assert.deepEqual(calls, [
    { boxes: firstEdit, workspaceId: 11, expectedVersion: 7, expectedCatalogVersion: 4 },
    { boxes: latestEdit, workspaceId: 11, expectedVersion: 15, expectedCatalogVersion: 4 },
  ]);
  assert.equal(h.beforeUnload(), true);
  replacement.resolve({ version: 16 });
  assert.equal(await resolved, true);
  assert.deepEqual(wrongScopeCalls, []);
  assert.deepEqual(h.render().boxes, latestEdit);
  assert.equal(h.render().conflict, null);
  assert.equal(h.beforeUnload(), false);
});

test("another same-catalog conflict preserves local IDs and undo for the next choice", async (t) => {
  const replacement = deferred();
  let retries = 0;
  const { h, calls } = await conflictedHook(t, () => ++retries === 1 ? replacement.promise : { version: 19 });
  const resolved = h.render().resolveConflict("local", { boxes: serverBoxes, version: 15, catalog_version: 4 });
  const newerServer = { ...currentServer, version: 18, boxes: [] };
  replacement.reject(new h.ApiError(409, "another save", { code: "version_conflict", current: newerServer }));
  assert.equal(await resolved, false);
  assert.deepEqual(h.render().boxes, latestEdit);
  assert.equal(h.render().undoDepth, 2);
  assert.deepEqual(h.render().conflict.current, newerServer);
  assert.equal(await h.render().flush(), false);
  assert.equal(h.beforeUnload(), true);
  assert.equal(await h.render().resolveConflict("local", { boxes: serverBoxes, version: 18, catalog_version: 4 }), true);
  assert.deepEqual(calls.slice(1), [
    { boxes: latestEdit, workspaceId: 11, expectedVersion: 15, expectedCatalogVersion: 4 },
    { boxes: latestEdit, workspaceId: 11, expectedVersion: 18, expectedCatalogVersion: 4 },
  ]);
});

test("local resolution preserves working undo without a separate clear-undo argument", async (t) => {
  const { h, calls } = await conflictedHook(t);
  assert.equal(await h.render().resolveConflict("local", { boxes: serverBoxes, version: 15, catalog_version: 4 }), true);
  assert.deepEqual(h.render().boxes, latestEdit);
  assert.equal(h.render().undoDepth, 2);
  h.render().undo();
  await settle();
  assert.deepEqual(h.render().boxes, firstEdit);
  assert.equal(h.render().undoDepth, 1);
  assert.deepEqual(calls.slice(1), [
    { boxes: latestEdit, workspaceId: 11, expectedVersion: 15, expectedCatalogVersion: 4 },
    { boxes: firstEdit, workspaceId: 11, expectedVersion: 16, expectedCatalogVersion: 4 },
  ]);
});

test("network failure after same-catalog local resolution allows ordinary retry", async (t) => {
  const replacement = deferred();
  let retries = 0;
  const { h, calls } = await conflictedHook(t, () => ++retries === 1 ? replacement.promise : { version: 16 });
  const resolved = h.render().resolveConflict("local", { boxes: serverBoxes, version: 15, catalog_version: 4 });
  replacement.reject(new TypeError("Failed to fetch"));
  assert.equal(await resolved, false);
  assert.deepEqual(h.render().boxes, latestEdit);
  assert.equal(h.render().undoDepth, 2);
  assert.equal(h.render().saveState, "error");
  assert.equal(h.render().saveError, "Failed to fetch");
  assert.equal(h.beforeUnload(), true);
  assert.equal(h.render().conflict, null);
  h.render().retry();
  await settle();
  assert.deepEqual(calls.slice(1), [
    { boxes: latestEdit, workspaceId: 11, expectedVersion: 15, expectedCatalogVersion: 4 },
    { boxes: latestEdit, workspaceId: 11, expectedVersion: 15, expectedCatalogVersion: 4 },
  ]);
  assert.equal(h.render().saveState, "saved");
  assert.equal(h.beforeUnload(), false);
});

test("read-only local resolution does not clear undo or replace boxes before accepting a choice", async (t) => {
  const { h, calls } = await conflictedHook(t);
  h.setOptions({ readOnly: true });
  assert.equal(await h.render().resolveConflict("local", { boxes: serverBoxes, version: 15, catalog_version: 4 }), false);
  assert.deepEqual(h.render().boxes, latestEdit);
  assert.equal(h.render().undoDepth, 2);
  assert.deepEqual(h.render().conflict.current, currentServer);
  assert.equal(calls.length, 1);
  h.setOptions({ readOnly: false });
  assert.equal(await h.render().resolveConflict("local", { boxes: serverBoxes, version: 15, catalog_version: 4 }), true);
  assert.equal(h.render().undoDepth, 2);
  assert.deepEqual(calls.at(-1), { boxes: latestEdit, workspaceId: 11, expectedVersion: 15, expectedCatalogVersion: 4 });
});

for (const choice of ["local", "server"]) {
  test(`${choice} resolution without an active conflict leaves saved boxes and undo unchanged`, async (t) => {
    const calls = [];
    const h = await harness(t, async (boxes, workspaceId, expectedVersion, expectedCatalogVersion) => {
      calls.push({ boxes, workspaceId, expectedVersion, expectedCatalogVersion });
      return { version: expectedVersion + 1 };
    });
    h.render().reset(initialBoxes, 7, 4);
    h.render().applyChange(firstEdit);
    await settle();
    assert.equal(await h.render().resolveConflict(choice, { boxes: serverBoxes, version: 15, catalog_version: 4 }), false);
    assert.deepEqual(h.render().boxes, firstEdit);
    assert.equal(h.render().undoDepth, 1);
    assert.equal(h.render().conflict, null);
    assert.equal(calls.length, 1);
    h.render().undo();
    await settle();
    assert.deepEqual(calls.at(-1), { boxes: initialBoxes, workspaceId: 11, expectedVersion: 8, expectedCatalogVersion: 4 });
  });
}

test("a second version conflict while keeping local edits preserves them for another choice", async (t) => {
  const replacement = deferred();
  const { h, calls } = await conflictedHook(t, () => replacement.promise);
  assert.equal(typeof h.render().resolveConflict, "function");
  const resolved = h.render().resolveConflict("local", { boxes: serverBoxes, version: 15, catalog_version: 4 });
  const newerServer = { ...currentServer, version: 18, boxes: [] };
  replacement.reject(new h.ApiError(409, "요청 실패 (409)", {
    code: "version_conflict", current: newerServer,
  }));
  assert.equal(await resolved, false);
  assert.equal(calls.length, 2);
  assert.deepEqual(h.render().boxes, latestEdit);
  assert.equal(h.render().undoDepth, 2, "an unchanged catalog preserves local undo through another conflict");
  assert.deepEqual(h.render().conflict.current, newerServer);
  assert.equal(await h.render().flush(), false);
  assert.equal(h.beforeUnload(), true);
});

test("read-only hook ignores apply and undo without issuing saves", async (t) => {
  const calls = [];
  const h = await harness(t, async (...args) => { calls.push(args); return { version: 8 }; }, { readOnly: true });
  h.render().reset(initialBoxes, 7, 4);
  h.render().applyChange(latestEdit);
  h.render().undo();
  h.keyboardUndo();
  await settle();
  assert.deepEqual(calls, []);
  assert.deepEqual(h.render().boxes, initialBoxes);
  assert.equal(h.render().undoDepth, 0);
  assert.equal(h.beforeUnload(), false);
});

test("switching to read-only blocks an existing undo history and keyboard undo", async (t) => {
  const calls = [];
  const h = await harness(t, async (...args) => { calls.push(args); return { version: 8 }; });
  h.render().reset(initialBoxes, 7, 4);
  h.render().applyChange(firstEdit);
  await settle();
  assert.equal(h.render().undoDepth, 1);
  h.setOptions({ readOnly: true });
  h.render().undo();
  h.keyboardUndo();
  h.render().applyChange(latestEdit);
  await settle();
  assert.equal(calls.length, 1);
  assert.deepEqual(h.render().boxes, firstEdit);
});

test("temporary edit blocking prevents undo and apply without dropping already queued saves", async (t) => {
  const first = deferred();
  const calls = [];
  const h = await harness(t, async (boxes, workspaceId, expectedVersion, expectedCatalogVersion) => {
    calls.push({ boxes, workspaceId, expectedVersion, expectedCatalogVersion });
    return calls.length === 1 ? first.promise : { version: expectedVersion + 1 };
  });
  h.render().reset(initialBoxes, 7, 4);
  h.render().applyChange(firstEdit);
  h.render().applyChange(latestEdit);
  h.setOptions({ editBlocked: true });
  h.keyboardUndo();
  h.render().undo();
  h.render().applyChange([box(77)]);
  assert.deepEqual(h.render().boxes, latestEdit);
  assert.equal(h.render().undoDepth, 2);
  first.resolve({ version: 8 });
  await settle();
  assert.equal(await h.render().flush({ requireSaved: true }), true);
  assert.deepEqual(calls, [
    { boxes: firstEdit, workspaceId: 11, expectedVersion: 7, expectedCatalogVersion: 4 },
    { boxes: latestEdit, workspaceId: 11, expectedVersion: 8, expectedCatalogVersion: 4 },
  ], "busy blocks new edits while allowing the preexisting queue to finish");
  h.setOptions({ editBlocked: false });
  h.keyboardUndo();
  await settle();
  assert.deepEqual(h.render().boxes, firstEdit);
  assert.deepEqual(calls.at(-1), { boxes: firstEdit, workspaceId: 11, expectedVersion: 9, expectedCatalogVersion: 4 });
});

test("switching to read-only during a save prevents the queued replacement from being sent", async (t) => {
  const first = deferred();
  const calls = [];
  const h = await harness(t, async (boxes, workspaceId, expectedVersion, expectedCatalogVersion) => {
    calls.push({ boxes, workspaceId, expectedVersion, expectedCatalogVersion });
    return calls.length === 1 ? first.promise : { version: 9 };
  });
  h.render().reset(initialBoxes, 7, 4);
  h.render().applyChange(firstEdit);
  h.render().applyChange(latestEdit);
  h.setOptions({ readOnly: true });
  first.resolve({ version: 8 });
  await settle();
  assert.deepEqual(calls, [{ boxes: firstEdit, workspaceId: 11, expectedVersion: 7, expectedCatalogVersion: 4 }]);
  assert.deepEqual(h.render().boxes, latestEdit);
  assert.equal(h.render().saveState, "rejected", "the unsent replacement must not appear saved");
  h.render().retry();
  h.render().undo();
  h.render().applyChange(serverBoxes);
  await h.render().flush();
  await settle();
  assert.equal(calls.length, 1, "no queued save or subsequent edit can bypass the new read-only state");
});

test("late success from an old image cannot overwrite the new image or catalog version", async (t) => {
  const first = deferred();
  const calls = [];
  const h = await harness(t, async (boxes, workspaceId, expectedVersion, expectedCatalogVersion) => {
    calls.push({ image: "old", boxes, workspaceId, expectedVersion, expectedCatalogVersion });
    return first.promise;
  });
  h.render().reset(initialBoxes, 7, 4);
  h.render().applyChange(firstEdit);
  h.switchWorkspace(22, async (boxes, workspaceId, expectedVersion, expectedCatalogVersion) => {
    calls.push({ image: "new", boxes, workspaceId, expectedVersion, expectedCatalogVersion });
    return { version: 31 };
  });
  h.render().reset(serverBoxes, 30, 9);
  h.render().applyChange(latestEdit);
  first.resolve({ version: 8, catalog_version: 4 });
  await settle();
  assert.deepEqual(calls, [
    { image: "old", boxes: firstEdit, workspaceId: 11, expectedVersion: 7, expectedCatalogVersion: 4 },
    { image: "new", boxes: latestEdit, workspaceId: 22, expectedVersion: 30, expectedCatalogVersion: 9 },
  ]);
  assert.equal(h.render().conflict, null);
  assert.deepEqual(h.render().boxes, latestEdit);
  assert.equal(h.render().saveState, "saved");
  assert.equal(h.render().catalogVersion, 9);
});

for (const code of ["version_conflict", "class_catalog_changed", "image_assigned_to_other"]) {
  test(`late ${code} from an old image cannot block the new generation`, async (t) => {
    const first = deferred();
    const calls = [];
    let assignmentConflicts = 0;
    const h = await harness(t, async () => first.promise, {
      onAssignmentConflict: () => { assignmentConflicts += 1; },
    });
    h.render().reset(initialBoxes, 7, 4);
    h.render().applyChange(firstEdit);
    h.switchWorkspace(22, async (boxes, workspaceId, expectedVersion, expectedCatalogVersion) => {
      calls.push({ boxes, workspaceId, expectedVersion, expectedCatalogVersion });
      return { version: 31 };
    });
    h.render().reset(serverBoxes, 30, 9);
    first.reject(new h.ApiError(409, "old image conflict", {
      code, current: currentServer, assignee_id: 999,
    }));
    await settle();
    assert.equal(h.render().conflict, null);
    assert.equal(assignmentConflicts, 0);
    h.render().applyChange(latestEdit);
    await settle();
    assert.deepEqual(calls, [{ boxes: latestEdit, workspaceId: 22, expectedVersion: 30, expectedCatalogVersion: 9 }]);
    assert.equal(h.render().saveState, "saved");
  });
}

test("assignment conflict drops queued saves and blocks further mutation until reset", async (t) => {
  const first = deferred();
  const calls = [];
  let assignmentConflicts = 0;
  const h = await harness(t, async (boxes, workspaceId, expectedVersion, expectedCatalogVersion) => {
    calls.push({ boxes, workspaceId, expectedVersion, expectedCatalogVersion });
    return calls.length === 1 ? first.promise : { version: expectedVersion + 1 };
  }, { onAssignmentConflict: () => { assignmentConflicts += 1; } });
  h.render().reset(initialBoxes, 7, 4);
  h.render().applyChange(firstEdit);
  h.render().applyChange(latestEdit);
  first.reject(new h.ApiError(409, "another member is assigned", {
    code: "image_assigned_to_other", assignee_id: 999,
  }));
  await settle();
  assert.equal(assignmentConflicts, 1);
  assert.equal(calls.length, 1);
  const blockedBoxes = h.render().boxes;
  const blockedUndoDepth = h.render().undoDepth;
  assert.equal(await h.render().resolveConflict("local", { boxes: serverBoxes, version: 15, catalog_version: 4 }), false);
  assert.deepEqual(h.render().boxes, blockedBoxes);
  assert.equal(h.render().undoDepth, blockedUndoDepth);
  h.render().applyChange([box(77)]);
  h.render().undo();
  h.keyboardUndo();
  h.render().retry();
  await h.render().flush();
  await settle();
  assert.equal(calls.length, 1);
  assert.deepEqual(h.render().boxes, blockedBoxes);
  h.render().reset(serverBoxes, 30, 9);
  h.render().applyChange(latestEdit);
  await settle();
  assert.equal(calls.length, 2, "loading another image clears the assignment block");
  assert.deepEqual(calls.at(-1), { boxes: latestEdit, workspaceId: 11, expectedVersion: 30, expectedCatalogVersion: 9 });
});

test("network failure retries the latest pending edit without advancing its version", async (t) => {
  const first = deferred();
  const calls = [];
  const h = await harness(t, async (boxes, workspaceId, expectedVersion, expectedCatalogVersion) => {
    calls.push({ boxes, workspaceId, expectedVersion, expectedCatalogVersion });
    return calls.length === 1 ? first.promise : { version: 8 };
  });
  h.render().reset(initialBoxes, 7, 4);
  h.render().applyChange(firstEdit);
  h.render().applyChange(latestEdit);
  first.reject(new TypeError("Failed to fetch"));
  await settle();
  assert.equal(h.render().saveState, "error");
  assert.equal(h.beforeUnload(), true);
  h.render().retry();
  await settle();
  assert.deepEqual(calls, [
    { boxes: firstEdit, workspaceId: 11, expectedVersion: 7, expectedCatalogVersion: 4 },
    { boxes: latestEdit, workspaceId: 11, expectedVersion: 7, expectedCatalogVersion: 4 },
  ]);
  assert.equal(h.render().saveState, "saved");
});

for (const status of [403, 422]) {
    test(`local resolution rejected with ${status} restores its conflict and pending edit without adopting server state`, async (t) => {
      const replacement = deferred();
      let attempts = 0;
      const { h, calls } = await conflictedHook(t, () => ++attempts === 1 ? replacement.promise : { version: 16 });
      const resolved = h.render().resolveConflict("local", {
        boxes: serverBoxes, version: 15, catalog_version: 4,
      });
      replacement.reject(new h.ApiError(status, "저장할 수 없음", { code: "save_rejected" }));
      assert.equal(await resolved, false, "navigation permission from flush is not successful conflict adoption");
      assert.equal(h.render().saveState, "rejected");
      assert.equal(h.render().saveError, "저장할 수 없음");
      assert.deepEqual(h.render().conflict, { code: "version_conflict", current: currentServer });
      assert.deepEqual(h.render().boxes, latestEdit);
      assert.equal(h.render().catalogVersion, 4);
      assert.equal(h.render().undoDepth, 2);
      assert.equal(h.beforeUnload(), true);
      assert.equal(await h.render().flush(), false);
      h.render().retry();
      h.render().applyChange([box(77)]);
      h.render().undo();
      h.keyboardUndo();
      await settle();
      assert.equal(calls.length, 2, "rejected resolution waits for another explicit choice");
      assert.deepEqual(h.render().boxes, latestEdit);
      assert.equal(await h.render().resolveConflict("local", {
        boxes: serverBoxes, version: 15, catalog_version: 4,
      }), true);
      assert.deepEqual(calls.slice(1), [
        { boxes: latestEdit, workspaceId: 11, expectedVersion: 15, expectedCatalogVersion: 4 },
        { boxes: latestEdit, workspaceId: 11, expectedVersion: 15, expectedCatalogVersion: 4 },
      ], "the explicit retry must retain the original latest local snapshot");
      assert.equal(h.render().saveState, "saved");
      assert.equal(h.render().conflict, null);
      assert.equal(h.beforeUnload(), false);
    });
}

test("ordinary validation rejection remains terminal and does not advance version", async (t) => {
  const calls = [];
  const h = await harness(t, async (boxes, workspaceId, expectedVersion, expectedCatalogVersion) => {
    calls.push({ boxes, workspaceId, expectedVersion, expectedCatalogVersion });
    if (calls.length === 1) throw new h.ApiError(422, "없는 클래스", { code: "unknown_annotation_class" });
    return { version: 8 };
  });
  h.render().reset(initialBoxes, 7, 4);
  h.render().applyChange(firstEdit);
  await settle();
  assert.equal(h.render().saveState, "rejected");
  assert.equal(h.render().saveError, "없는 클래스");
  assert.equal(h.render().conflict, null);
  assert.equal(await h.render().flush(), true);
  assert.equal(h.beforeUnload(), false);
  h.render().applyChange(latestEdit);
  await settle();
  assert.deepEqual(calls.at(-1), { boxes: latestEdit, workspaceId: 11, expectedVersion: 7, expectedCatalogVersion: 4 });
});
