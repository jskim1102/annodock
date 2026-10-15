import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import ts from "typescript";

const moduleUrl = (source) => `data:text/javascript;base64,${Buffer.from(source).toString("base64")}`;
const settle = () => new Promise((resolve) => setImmediate(resolve));
const roles = ["owner", "admin", "editor", "viewer"];
const dataset = {
  id: 91, project_id: 31, name: "합친 데이터", status: "ready", is_merged: true,
  image_count: 10, labeled_image_count: 8, annotation_count: 8, class_count: 1,
  storage_bytes: 100, active_job: null, source_datasets: [],
  classes: [{ class_id: 0, name: "표지판", color: "#abcdef" }],
  created_at: "2026-10-01T00:00:00Z", split_counts: { train: 7, valid: 2, test: 1 },
};
const project = {
  id: 31, name: "도로 프로젝트", archived: false, dataset_count: 1,
  image_count: 10, annotation_count: 8, class_count: 1, storage_bytes: 100,
  created_at: "2026-10-01T00:00:00Z", classes: dataset.classes, datasets: [dataset],
};

function elements(node) {
  if (Array.isArray(node)) return node.flatMap(elements);
  if (!node || typeof node !== "object") return [];
  return [node, ...elements(node.props?.children)];
}
function textOf(node) {
  if (Array.isArray(node)) return node.map(textOf).join("");
  if (typeof node === "string" || typeof node === "number") return String(node);
  return node && typeof node === "object" ? textOf(node.props?.children) : "";
}

async function harness(t, page, role, props = {}) {
  const key = `__roleControls${Math.random().toString(16).slice(2)}`;
  const active = { id: 71, kind: "team", role };
  const context = {
    workspace: { active, items: [active] }, calls: [], navigated: [],
  };
  const originalWindow = globalThis.window, originalDocument = globalThis.document;
  const listeners = new Map();
  const browser = {
    location: { search: "?project_id=31" },
    setTimeout: () => 1, clearTimeout() {},
    addEventListener(name, listener) {
      if (!listeners.has(name)) listeners.set(name, new Set());
      listeners.get(name).add(listener);
    },
    removeEventListener(name, listener) { listeners.get(name)?.delete(listener); },
  };
  globalThis.window = browser; globalThis.document = browser;
  const frames = new Map(), pendingEffects = [];
  let current, cursor = 0;
  const changedDeps = (previous, next) => !previous || !next || previous.length !== next.length
    || next.some((value, index) => !Object.is(value, previous[index]));
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
      if (changedDeps(frame.slots[index]?.deps, deps)) pendingEffects.push(() => {
        frame.cleanups.get(index)?.(); frame.slots[index] = { deps };
        frame.cleanups.set(index, callback());
      });
    },
    useMemo(factory, deps) {
      const index = cursor++;
      if (changedDeps(current.slots[index]?.deps, deps)) current.slots[index] = { value: factory(), deps };
      return current.slots[index].value;
    },
    useCallback(callback, deps) { return hooks.useMemo(() => callback, deps); },
  };
  context.hooks = hooks;
  globalThis[key] = context;
  t.after(() => {
    frames.forEach((frame) => frame.cleanups.forEach((cleanup) => cleanup?.()));
    globalThis.window = originalWindow; globalThis.document = originalDocument;
    delete globalThis[key];
  });
  const react = moduleUrl(`
    const c = globalThis[${JSON.stringify(key)}];
    export const {useState,useRef,useEffect,useMemo,useCallback} = c.hooks;
    export const jsx = (type,props,key) => ({type,props,key}), jsxs = jsx, Fragment = 'fragment';
    export const useWorkspace = () => c.workspace;
  `);
  const compileCache = new Map();
  async function compile(file) {
    if (compileCache.has(file.href)) return compileCache.get(file.href);
    const source = await readFile(file, "utf8");
    let compiled = ts.transpileModule(source, { compilerOptions: {
      module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX,
    } }).outputText;
    for (const match of [...compiled.matchAll(/import\s+([^;]+?)\s+from\s+"([^"]+)";/g)]) {
      const [statement, bindings, dependency] = match;
      let url;
      if (["react", "react/jsx-runtime", "../store/workspace"].includes(dependency)) url = react;
      else if (dependency.includes("/api/")) {
        const names = bindings.replace(/[{}]/g, "").split(",").map((name) => name.trim()).filter(Boolean);
        url = moduleUrl(`const c = globalThis[${JSON.stringify(key)}];\n${names.map((name) => {
          if (name === "ApiError") return "export class ApiError extends Error {}";
          if (name === "captureWorkspaceId") return "export const captureWorkspaceId = () => c.workspace.active.id;";
          return `export const ${name} = (...args) => c.call(${JSON.stringify(name)}, args);`;
        }).join("\n")}`);
      } else if (dependency.includes("/components/")) {
        const names = bindings.replace(/[{}]/g, "").split(",").map((name) => name.trim()).filter(Boolean);
        url = moduleUrl(names.map((name) => name === "Icon" ? "export const Icon = () => null;"
          : `export const ${name} = (props) => ({type:${JSON.stringify(name)},props});`).join("\n"));
      } else if (dependency === "../navigation") {
        url = moduleUrl(`const c=globalThis[${JSON.stringify(key)}]; export const appHref=(path)=>path; export const navigate=(path)=>c.navigated.push(path);`);
      } else {
        assert.ok(dependency.startsWith("."), `unexpected dependency ${dependency}`);
        url = await compile(new URL(`${dependency}.ts`, file));
      }
      compiled = compiled.replace(statement, statement.replace(`"${dependency}"`, `"${url}"`));
    }
    const url = moduleUrl(compiled); compileCache.set(file.href, url); return url;
  }
  const defaultsModule = await compile(new URL("../src/utils/trainingArguments.ts", import.meta.url));
  const { RTX_3090_TRAINING_DEFAULTS: defaults } = await import(defaultsModule);
  context.call = async (name, args) => {
    context.calls.push([name, ...args]);
    if (name === "getProjects") return { items: [structuredClone(project)] };
    if (name === "getProject") return structuredClone(project);
    if (name === "getDataset") return structuredClone(dataset);
    if (name === "getDatasetImages") return { items: [], next_cursor: null };
    if (name === "getProjectClassImageCounts") return { items: [] };
    if (name === "getModels") return [{ name: "yolo26s.pt", type: "preset", size_mb: 20 }];
    if (name === "getTrainingRecommendation") return {
      ...defaults, total_images: 10, labeled_images: 8, unlabeled_images: 2, train_images: 7,
      small_object_ratio: 0, effective_max_imgsz: 640, reasons: [],
    };
    if (name === "startTraining") return { run_id: 501, warnings: [] };
    throw new Error(`unexpected API call ${name}`);
  };
  const loaded = await import(await compile(new URL(`../src/pages/${page}.tsx`, import.meta.url)));
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
    const tree = evaluate(loaded[page], props, "root");
    pendingEffects.splice(0).forEach((effect) => effect());
    return tree;
  };
  const flush = async () => { for (let turn = 0; turn < 5; turn++) { render(); await settle(); } };
  await flush();
  return {
    context, render, flush, text: () => textOf(render()),
    find: (predicate) => elements(render()).find(predicate),
    button: (name) => elements(render()).find((node) => node.type === "button" && textOf(node).trim() === name),
  };
}

for (const role of roles) {
  const manage = role === "owner" || role === "admin";
  const upload = role !== "viewer";
  test(`ProjectsPage ${role} controls project creation, row actions, upload and training`, async (t) => {
    const view = await harness(t, "ProjectsPage", role);
    assert.match(view.text(), /도로 프로젝트/);
    assert.equal(Boolean(view.button("새 프로젝트")), manage);
    const menu = view.find((node) => node.props?.["aria-label"] === "도로 프로젝트 메뉴");
    assert.equal(Boolean(menu), manage);
    view.find((node) => node.props?.["aria-label"] === "도로 프로젝트 펼치기").props.onClick();
    await view.flush();
    assert.equal(Boolean(view.find((node) => node.type === "a" && node.props.href === "/upload?project_id=31")), upload);
    for (const label of ["AI 학습", "병합", "분리"]) assert.equal(Boolean(view.button(label)), manage);
    if (manage) {
      menu.props.onClick();
      for (const label of ["이름 변경", "클래스 수정", "삭제"]) {
        assert.equal(view.button(label)?.props.role, "menuitem");
      }
      view.button("새 프로젝트").props.onClick();
      assert.ok(view.find((node) => node.type === "NewProjectDialog"));
    } else {
      assert.equal(view.find((node) => node.props?.role === "menuitem"), undefined);
      assert.equal(view.find((node) => node.type === "NewProjectDialog"), undefined);
    }
    assert.equal(view.context.calls.some(([name]) => /^(create|delete|rename|merge|start)/.test(name)), false);
  });

  test(`TrainPage direct access by ${role} requires admin before a training mutation`, async (t) => {
    const view = await harness(t, "TrainPage", role, { datasetId: 91 });
    const form = view.find((node) => node.type === "form");
    assert.equal(Boolean(form), manage);
    assert.equal(Boolean(view.button("학습 시작")), manage);
    if (manage) {
      assert.equal(Boolean(view.button("학습 시작").props.disabled), false);
      await form.props.onSubmit({ preventDefault() {} });
      await view.flush();
      const requests = view.context.calls.filter(([name]) => name === "startTraining");
      assert.equal(requests.length, 1);
      assert.equal(requests[0][1], 91);
      assert.equal(requests[0][2].weights, "yolo26s.pt");
      assert.deepEqual(view.context.navigated, ["/runs/501"]);
    } else {
      assert.match(view.text(), /관리자 이상만 학습을 시작할 수 있어요/);
      assert.equal(view.context.calls.filter(([name]) => name === "startTraining").length, 0);
      assert.deepEqual(view.context.navigated, []);
    }
  });

  test(`UploadPage direct access by ${role} separates upload from merge management`, async (t) => {
    const view = await harness(t, "UploadPage", role);
    assert.equal(Boolean(view.find((node) => node.type === "input" && node.props.type === "file")), upload);
    assert.equal(Boolean(view.button("업로드 시작")), upload);
    const mergeOption = view.find((node) => node.type === "input" && node.props.name === "upload-merge-target");
    assert.equal(Boolean(mergeOption), manage);
    if (upload) {
      assert.ok(view.find((node) => node.type === "input" && node.props.readOnly && node.props.value === "도로 프로젝트"));
      view.button("업로드 시작").props.onClick();
      assert.match(view.text(), /업로드할 파일이나 폴더를 먼저 선택하세요/);
      if (manage) {
        const target = view.find((node) => node.type === "input" && node.props.name === "upload-merge-target" && node.props.value === 91);
        target.props.onChange();
        assert.equal(view.find((node) => node.type === "input" && node.props.value === 91).props.checked, true);
      } else assert.doesNotMatch(view.text(), /기존 병합 데이터셋에 포함/);
    } else {
      assert.match(view.text(), /라벨러 이상만 업로드할 수 있어요/);
    }
    assert.equal(view.context.calls.some(([name]) => /^(create|begin|prepare|transfer|complete|merge|extend)/.test(name)), false);
  });
}
