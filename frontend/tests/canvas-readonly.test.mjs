import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import ts from "typescript";

const moduleUrl = (source) => `data:text/javascript;base64,${Buffer.from(source).toString("base64")}`;
const settle = () => new Promise((resolve) => setImmediate(resolve));
const BOX = { id: 1, classId: 0, x: 20, y: 20, width: 30, height: 30 };
const CLASSES = [
  { id: 0, name: "person", count: 1, color: "#EF4444" },
  { id: 1, name: "vehicle", count: 0, color: "#22C55E" },
];
function textOf(node) {
  if (typeof node === "string" || typeof node === "number") return String(node);
  if (!node || typeof node !== "object") return "";
  return [node.props?.children].flat(Infinity).map(textOf).join("");
}
function elements(node) {
  if (!node || typeof node !== "object") return [];
  return [node, ...[node.props?.children].flat(Infinity).flatMap(elements)];
}

async function harness(t, overrides = {}) {
  const globals = new Map(["window", "document", "Node", "Element", "HTMLElement", "ResizeObserver"]
    .map((name) => [name, Object.getOwnPropertyDescriptor(globalThis, name)]));
  class HostNode {
    parentElement = null;
    contains(node) {
      for (let current = node; current; current = current.parentElement) if (current === this) return true;
      return false;
    }
  }
  class HostElement extends HostNode {
    tagName = "DIV";
    props = {};
    isContentEditable = false;
    captures = new Set();
    listeners = new Map();
    closest(selectors) {
      const matches = (node, selector) => selector.startsWith(".")
        ? (node.props.className ?? "").split(" ").includes(selector.slice(1))
        : node.tagName === selector.toUpperCase();
      for (let node = this; node; node = node.parentElement) {
        if (selectors.split(",").some((selector) => matches(node, selector.trim()))) return node;
      }
      return null;
    }
    getBoundingClientRect() {
      const frame = this.props.className === "image-frame";
      return { left: 0, top: 0, width: frame ? 100 : 132, height: frame ? 100 : 132 };
    }
    setPointerCapture(id) { this.captures.add(id); }
    hasPointerCapture(id) { return this.captures.has(id); }
    releasePointerCapture(id) { this.captures.delete(id); }
    addEventListener(type, callback) {
      if (!this.listeners.has(type)) this.listeners.set(type, new Set());
      this.listeners.get(type).add(callback);
    }
    removeEventListener(type, callback) { this.listeners.get(type)?.delete(callback); }
    focus() { globalThis.document.activeElement = this; }
  }
  const windowHost = new HostElement();
  Object.assign(globalThis, {
    window: windowHost, document: { activeElement: null },
    Node: HostNode, Element: HostElement, HTMLElement: HostElement,
    ResizeObserver: class { observe() {} disconnect() {} },
  });
  const slots = [], pendingEffects = [];
  const cleanups = new Map();
  let cursor = 0, dirty = false;
  const sameDependencies = (before, after) => before && after
    && before.length === after.length && after.every((value, index) => Object.is(before[index], value));
  const react = {
    useState(initial) {
      const index = cursor++;
      if (!(index in slots)) slots[index] = typeof initial === "function" ? initial() : initial;
      return [slots[index], (value) => {
        const next = typeof value === "function" ? value(slots[index]) : value;
        if (!Object.is(next, slots[index])) { slots[index] = next; dirty = true; }
      }];
    },
    useRef(initial) {
      const index = cursor++;
      if (!(index in slots)) slots[index] = { current: initial };
      return slots[index];
    },
    useCallback(callback, dependencies) {
      const index = cursor++;
      if (!sameDependencies(slots[index]?.dependencies, dependencies)) slots[index] = { callback, dependencies };
      return slots[index].callback;
    },
    useEffect(callback, dependencies) {
      const index = cursor++;
      if (!sameDependencies(slots[index], dependencies)) {
        slots[index] = dependencies;
        pendingEffects.push(() => { cleanups.get(index)?.(); cleanups.set(index, callback()); });
      }
    },
  };
  const key = `__canvasReadOnly${Math.random().toString(16).slice(2)}`;
  globalThis[key] = react;
  t.after(() => {
    cleanups.forEach((cleanup) => cleanup?.());
    for (const [name, descriptor] of globals) {
      if (descriptor) Object.defineProperty(globalThis, name, descriptor);
      else delete globalThis[name];
    }
    delete globalThis[key];
  });
  const reactModule = moduleUrl(`export const {useState,useRef,useCallback,useEffect} = globalThis[${JSON.stringify(key)}];`);
  const jsxModule = moduleUrl(`
    export const jsx = (type, props) => ({type,props}), jsxs = jsx, Fragment = "fragment";
    export const Icon = () => null, ClassColorPicker = () => null;
  `);
  const source = await readFile(new URL("../src/components/BboxCanvas.tsx", import.meta.url), "utf8");
  const compiled = ts.transpileModule(source, { compilerOptions: {
    target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext, jsx: ts.JsxEmit.ReactJSX,
  } }).outputText.replace(/from "([^"]+)"/g, (_match, path) => {
    assert.ok(["react", "react/jsx-runtime", "./Icon", "./ClassColorPicker"].includes(path), `unmapped dependency ${path}`);
    return `from "${path === "react" ? reactModule : jsxModule}"`;
  });
  const { BboxCanvas } = await import(moduleUrl(compiled));
  const calls = { changes: [], undos: 0, retries: 0, renames: [], colors: [], navigations: [] };
  const props = {
    imageSrc: "fixture.jpg", imageWidth: 100, imageHeight: 100,
    boxes: [{ ...BOX }], classes: CLASSES.map((row) => ({ ...row })), catalogVersion: 5, saveState: "saved", undoDepth: 3,
    onChange(boxes) { calls.changes.push(structuredClone(boxes)); props.boxes = boxes; },
    onUndo() { calls.undos++; }, onRetrySave() { calls.retries++; },
    onRenameClass: async (...args) => { calls.renames.push(args); },
    onChangeClassColor: (...args) => { calls.colors.push(args); },
    onNavigate: (...args) => { calls.navigations.push(args); }, ...overrides,
  };
  const hosts = new Map();
  const attachRefs = (element, path = "root", parent = null) => {
    if (!element || typeof element !== "object") return;
    let host = parent;
    if (typeof element.type === "string") {
      if (!hosts.has(path)) hosts.set(path, new HostElement());
      host = hosts.get(path);
      host.parentElement = parent; host.tagName = element.type.toUpperCase(); host.props = element.props;
      element.host = host;
      if (element.props.ref) element.props.ref.current = host;
    }
    [element.props.children].flat(Infinity).forEach((child, index) => attachRefs(child, `${path}.${index}`, host));
  };
  let tree;
  const render = () => {
    for (let pass = 0; pass < 20; pass++) {
      dirty = false; cursor = 0;
      tree = BboxCanvas(props); attachRefs(tree);
      pendingEffects.splice(0).forEach((effect) => effect());
      if (!dirty) return tree;
    }
    throw new Error("canvas did not settle after effects");
  };
  const find = (predicate) => elements(render()).find(predicate);
  const labeled = (label) => find((node) => node.props["aria-label"] === label);
  const button = (label) => find((node) => node.type === "button" && textOf(node) === label);
  const event = (target, overrides = {}) => ({ target, currentTarget: target, pointerId: 7, button: 0,
    clientX: 25, clientY: 25, prevented: false, stopped: false,
    preventDefault() { this.prevented = true; }, stopPropagation() { this.stopped = true; }, ...overrides });
  const click = (node) => {
    assert.ok(node, "click target exists");
    // Native disabled buttons never dispatch click events.
    if (!node.props.disabled) node.props.onClick?.(event(node.host));
    render();
  };
  const pointer = (node, kind, overrides = {}) => {
    assert.ok(node, "pointer target exists");
    node.props[`onPointer${kind}`]?.(event(node.host, overrides)); render();
  };
  const keydown = (key, code = key) => {
    const target = labeled("라벨 편집 캔버스").host;
    const value = event(target, { key, code, ctrlKey: false, metaKey: false, altKey: false });
    [...(windowHost.listeners.get("keydown") ?? [])].forEach((listener) => listener(value)); render();
  };
  render();
  return { calls, props, render, find, labeled, button, click, pointer, keydown,
    setProps(next) { Object.assign(props, next); render(); },
    stage: () => labeled("라벨 편집 캔버스"),
    frame: () => find((node) => node.props.className === "image-frame"),
    box: () => labeled("person 바운딩 박스"),
  };
}

test("readonly disables both labeling tools and undo even when undo history exists", async (t) => {
  const h = await harness(t, { readOnly: true, canManageClasses: false });
  for (const control of [h.labeled("선택·팬"), h.labeled("박스 생성"), h.button("실행취소")]) {
    assert.equal(control.props.disabled, true); h.click(control);
  }
  assert.equal(h.calls.undos, 0);
  assert.equal(h.calls.changes.length, 0);
  assert.equal(h.labeled("확대").props.disabled, undefined, "viewing controls remain available");
});

for (const message of [
  "저장하지 못해 이름을 바꾸지 않았어요",
]) {
  test(`rename failure keeps the actual input open and displays the supplied message: ${message}`, async (t) => {
    const requests = [];
    let failing = true;
    const h = await harness(t, { canManageClasses: true, onRenameClass: async (...args) => {
      requests.push(args);
      if (failing) throw new Error(message);
    } });
    h.click(h.labeled("person 이름 변경"));
    const input = h.labeled("person 클래스 이름");
    assert.ok(input);
    input.props.onChange({ target: { value: "renamed person" } });
    h.render();
    h.find((node) => node.type === "form" && node.props.className.includes("is-renaming")).props.onSubmit({ preventDefault() {} });
    await settle(); h.render();
    assert.equal(h.labeled("person 클래스 이름").props.value, "renamed person");
    assert.equal(textOf(h.find((node) => node.props.className === "class-rename-error")), message);
    assert.equal(h.labeled("이름 저장").props.disabled, false);
    assert.deepEqual(requests, [[0, "renamed person"]]);
    failing = false;
    h.find((node) => node.type === "form" && node.props.className.includes("is-renaming")).props.onSubmit({ preventDefault() {} });
    await settle(); h.render();
    assert.equal(h.labeled("person 클래스 이름"), undefined);
    assert.equal(h.find((node) => node.props.className === "class-rename-error"), undefined);
    assert.deepEqual(requests, [[0, "renamed person"], [0, "renamed person"]]);
  });
}

for (const order of ["catalog before error", "error before catalog"]) {
  test(`reloaded catalog closes an old-ID rename draft with ${order} and prevents old-ID retry`, async (t) => {
    const requests = [];
    let reject;
    const pending = new Promise((_resolve, fail) => { reject = fail; });
    const h = await harness(t, { canManageClasses: true, onRenameClass: (...args) => {
      requests.push(args); return pending;
    } });
    h.click(h.labeled("person 이름 변경"));
    h.labeled("person 클래스 이름").props.onChange({ target: { value: "renamed person" } });
    h.render();
    h.find((node) => node.type === "form" && node.props.className.includes("is-renaming")).props.onSubmit({ preventDefault() {} });
    const reload = () => h.setProps({ catalogVersion: 6, classes: [
      { ...CLASSES[0], name: "vehicle" }, { ...CLASSES[1], name: "person" },
    ] });
    if (order === "catalog before error") reload();
    reject(new Error("클래스 목록이 바뀌어 새로 불러왔어요. 이름 변경을 다시 열어 주세요."));
    await settle(); h.render();
    if (order === "error before catalog") reload();
    assert.equal(Boolean(h.find((node) => node.type === "form" && node.props.className.includes("is-renaming"))), false);
    assert.equal(Boolean(h.labeled("이름 저장")), false);
    h.keydown("Enter");
    await settle(); h.render();
    assert.deepEqual(requests, [[0, "renamed person"]], "the discarded draft cannot rename vehicle at the former person ID");
    h.click(h.labeled("person 이름 변경"));
    assert.equal(h.labeled("person 클래스 이름").props.value, "person", "reopening starts from the new catalog rather than the discarded draft");
  });
}

test("successful catalog reload also closes a pending rename draft", async (t) => {
  let finish;
  const pending = new Promise((resolve) => { finish = resolve; });
  const h = await harness(t, { canManageClasses: true, onRenameClass: () => pending });
  h.click(h.labeled("person 이름 변경"));
  h.labeled("person 클래스 이름").props.onChange({ target: { value: "renamed person" } });
  h.render();
  h.find((node) => node.type === "form" && node.props.className.includes("is-renaming")).props.onSubmit({ preventDefault() {} });
  h.setProps({ catalogVersion: 6, classes: [{ ...CLASSES[0], name: "renamed person" }, CLASSES[1]] });
  assert.equal(Boolean(h.labeled("이름 저장")), false);
  finish(); await settle(); h.render();
  assert.equal(Boolean(h.labeled("이름 저장")), false);
});

test("readonly blocks pointer edits, selected-box deletion, and class clicks or shortcuts", async (t) => {
  const h = await harness(t, { readOnly: true, canManageClasses: false });
  h.pointer(h.box(), "Down");
  h.pointer(h.stage(), "Move", { clientX: 45, clientY: 45 }); h.pointer(h.stage(), "Up");
  h.keydown("Tab");
  assert.match(h.box().props.className, /is-selected/, "a box is selected before mutation attempts");
  h.keydown("Delete"); h.keydown("Backspace"); h.keydown("1", "Digit1");
  h.click(h.find((node) => node.props.className === "inspector-class" && textOf(node).includes("vehicle")));
  h.keydown("n", "KeyN");
  h.pointer(h.stage(), "Down", { target: h.frame().host, clientX: 60, clientY: 60 });
  h.pointer(h.stage(), "Move", { clientX: 90, clientY: 90 }); h.pointer(h.stage(), "Up");
  assert.deepEqual(h.calls.changes, []);
  assert.equal(h.find((node) => node.props.className?.startsWith("resize-handle")), undefined);
  assert.deepEqual(h.props.boxes, [BOX]);
});

test("writable canvas gesture harness creates a real box and dispatches undo", async (t) => {
  const h = await harness(t);
  h.click(h.labeled("박스 생성"));
  h.pointer(h.stage(), "Down", { target: h.frame().host, clientX: 60, clientY: 60 });
  h.pointer(h.stage(), "Move", { clientX: 90, clientY: 90 });
  assert.ok(h.find((node) => node.props.className === "editor-box draft-box"));
  h.pointer(h.stage(), "Up");
  assert.equal(h.calls.changes.length, 1);
  assert.deepEqual(h.calls.changes[0][1], { id: 2, classId: 0, x: 60, y: 60, width: 30, height: 30 });
  h.click(h.button("실행취소")); assert.equal(h.calls.undos, 1);
});

test("readonly background pan keeps its position on pointerup without changing boxes", async (t) => {
  const h = await harness(t, { readOnly: true, canManageClasses: false });
  const initial = h.frame().props.style.transform;
  h.pointer(h.stage(), "Down", { target: h.frame().host, clientX: 60, clientY: 60 });
  assert.equal(h.stage().host.hasPointerCapture(7), true);
  h.pointer(h.stage(), "Move", { clientX: 90, clientY: 80 });
  const moved = h.frame().props.style.transform;
  assert.notEqual(moved, initial);
  assert.match(moved, /^translate3d\(30px, 20px, 0\)/);
  h.pointer(h.stage(), "Up");
  assert.equal(h.frame().props.style.transform, moved, "view-only movement remains committed");
  assert.equal(h.stage().host.hasPointerCapture(7), false);
  assert.doesNotMatch(h.stage().props.className, /is-panning/);
  assert.deepEqual(h.calls.changes, []);
  assert.deepEqual(h.props.boxes, [BOX]);
});

test("readonly background pan cancellation restores its starting position without changing boxes", async (t) => {
  const h = await harness(t, { readOnly: true, canManageClasses: false });
  const initial = h.frame().props.style.transform;
  h.pointer(h.stage(), "Down", { target: h.frame().host, clientX: 60, clientY: 60 });
  h.pointer(h.stage(), "Move", { clientX: 90, clientY: 80 });
  assert.notEqual(h.frame().props.style.transform, initial);
  assert.equal(h.stage().host.hasPointerCapture(7), true);
  h.pointer(h.stage(), "Cancel");
  assert.equal(h.frame().props.style.transform, initial);
  assert.equal(h.stage().host.hasPointerCapture(7), false);
  assert.doesNotMatch(h.stage().props.className, /is-panning/);
  assert.deepEqual(h.calls.changes, []);
  assert.deepEqual(h.props.boxes, [BOX]);
});

for (const kind of ["move", "resize", "draw"]) {
  test(`switching to readonly during ${kind} discards the pending edit on pointerup`, async (t) => {
    const h = await harness(t);
    if (kind === "draw") {
      h.click(h.labeled("박스 생성"));
      h.pointer(h.stage(), "Down", { target: h.frame().host, clientX: 60, clientY: 60 });
      h.pointer(h.stage(), "Move", { clientX: 90, clientY: 90 });
      assert.ok(h.find((node) => node.props.className === "editor-box draft-box"));
    } else if (kind === "resize") {
      h.keydown("Tab");
      h.pointer(h.find((node) => node.props.className === "resize-handle handle-se"), "Down", { clientX: 50, clientY: 50 });
      h.pointer(h.stage(), "Move", { clientX: 65, clientY: 65 });
      assert.equal(h.box().props.style.width, 45);
    } else {
      h.pointer(h.box(), "Down"); h.pointer(h.stage(), "Move", { clientX: 40, clientY: 40 });
      assert.equal(h.box().props.style.left, 35);
    }
    assert.equal(h.stage().host.hasPointerCapture(7), true);
    h.setProps({ readOnly: true }); h.pointer(h.stage(), "Up");
    assert.deepEqual(h.calls.changes, []);
    assert.deepEqual(h.props.boxes, [BOX]);
    assert.equal(h.stage().host.hasPointerCapture(7), false);
    assert.equal(h.find((node) => node.props.className === "editor-box draft-box"), undefined);
    assert.equal(h.box().props.style.left, BOX.x);
    assert.equal(h.box().props.style.width, BOX.width);
  });
}

test("editor can relabel a box while class catalog rename and color controls stay unavailable", async (t) => {
  const h = await harness(t, { canManageClasses: false });
  for (const name of ["person", "vehicle"]) {
    const rename = h.labeled(`${name} 이름 변경`);
    assert.ok(!rename || rename.props.disabled, "catalog rename is hidden or disabled");
    if (rename) h.click(rename);
    const color = h.labeled(`${name} 색상 변경`);
    assert.ok(!color || color.props.disabled, "catalog color is hidden or disabled");
    if (color) h.click(color);
  }
  assert.equal(h.find((node) => node.type === "form"), undefined);
  assert.equal(h.find((node) => node.type?.name === "ClassColorPicker"), undefined);
  assert.deepEqual(h.calls.renames, []); assert.deepEqual(h.calls.colors, []);
  h.keydown("Tab");
  h.click(h.find((node) => node.props.className === "inspector-class" && textOf(node).includes("vehicle")));
  assert.equal(h.calls.changes.length, 1); assert.equal(h.calls.changes[0][0].classId, 1);
});

test("rejected save cannot retry while transient error exposes an actionable retry", async (t) => {
  const h = await harness(t, { saveState: "rejected" });
  const rejected = h.button("저장할 수 없음");
  assert.ok(rejected); assert.equal(rejected.props.disabled, true); h.click(rejected);
  assert.equal(h.calls.retries, 0);
  h.setProps({ saveState: "error" });
  const retry = h.button("재시도");
  assert.ok(retry); assert.equal(retry.props.disabled, false); h.click(retry);
  assert.equal(h.calls.retries, 1);
});
