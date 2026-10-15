import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import ts from "typescript";

const url = (source) => `data:text/javascript;base64,${Buffer.from(source).toString("base64")}`;
const compile = (source) => ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
}).outputText;
const personal = { id: 101, kind: "personal", name: "개인", role: "owner" };
const team = { id: 202, kind: "team", name: "팀 A", role: "editor" };

async function harness(t, initial = {}) {
  const h = { values: new Map(), items: [personal, team], clears: 0, quota: 0,
    paths: [], calls: 0, ...initial };
  const key = `__workspace${Math.random()}`;
  globalThis[key] = h;
  t.after(() => delete globalThis[key]);
  const bridge = `const h = globalThis[${JSON.stringify(key)}];`;
  const cacheUrl = url(compile(await readFile(new URL("../src/utils/authenticatedResourceCache.ts", import.meta.url), "utf8")) + `\n// ${key}`);
  const cache = await import(cacheUrl);
  const deps = {
    react: url("export const useSyncExternalStore = (_s, get) => get();"),
    "../api/client": url(`${bridge}
      export const requestJson = async () => { h.calls++; return {items: h.load ? await h.load() : h.items}; };
      export const invalidateStorageQuotaCache = () => h.quota++;
    `),
    "../utils/storage": url(`${bridge}
      export const readStoredString = key => h.values.get(key) ?? null;
      export const writeStoredString = (key, value) => { h.values.set(key, value); return true; };
    `),
    "../utils/authenticatedResourceCache": url(`${bridge}
      import { clearAuthenticatedResourceCache as clear } from "${cacheUrl}";
      export const clearAuthenticatedResourceCache = () => { h.clears++; clear(); };
    `),
    "../navigation": url(`${bridge} export const navigateWithinApp = path => h.paths.push(path);`),
  };
  const source = await readFile(new URL("../src/store/workspace.ts", import.meta.url), "utf8");
  const mod = await import(url(compile(source).replace(/from "([^"]+)"/g,
    (_all, name) => `from "${deps[name]}"`)));
  return { h, cache, ...mod };
}

test("boot restores only a current membership and keeps storage per user", async (t) => {
  const w = await harness(t, { values: new Map([["workspace:7", "202"], ["workspace:8", "999"]]) });
  await w.initializeWorkspaces(7);
  assert.equal(w.getActiveWorkspaceId(), 202);
  await w.initializeWorkspaces(8);
  assert.equal(w.getActiveWorkspaceId(), 101);
  assert.equal(w.h.values.get("workspace:7"), "202");
  assert.equal(w.h.values.get("workspace:8"), "101");
});

test("switch clears blobs and quota and navigates within document to unmount Viewer", async (t) => {
  const w = await harness(t);
  await w.initializeWorkspaces(7);
  const before = [w.h.clears, w.h.quota];
  let loads = 0;
  const loader = async () => { loads++; return new Blob(["image"]); };
  const lease = await w.cache.acquireAuthenticatedResource("/api/images/5/file", loader);
  assert.equal(w.cache.peekAuthenticatedResource("/api/images/5/file"), lease.url);
  w.switchWorkspace(202);
  assert.equal(w.cache.peekAuthenticatedResource("/api/images/5/file"), null);
  const next = await w.cache.acquireAuthenticatedResource("/api/images/5/file", loader);
  assert.notEqual(next.url, lease.url);
  assert.equal(loads, 2);
  lease.release();
  next.release();
  w.cache.clearAuthenticatedResourceCache();
  assert.equal(w.getActiveWorkspaceId(), 202);
  assert.equal(w.getWorkspaceKind(202), "team");
  assert.equal(w.getWorkspaceKind(101), "personal");
  assert.deepEqual([w.h.clears, w.h.quota], before.map(n => n + 1));
  assert.deepEqual(w.h.paths, ["/projects?ws=202"]);
  assert.equal(w.getWorkspaceSnapshot().message, "워크스페이스 팀 A 로 전환됨");
  assert.equal(w.h.values.get("workspace:7"), "202");
});

test("deep link revalidates membership, preserves its route, and rejects outsiders without switching", async (t) => {
  const w = await harness(t);
  await w.initializeWorkspaces(7);
  const count = w.h.calls;
  await w.initializeWorkspaces(7, "202");
  assert.equal(w.h.calls, count + 1);
  assert.equal(w.getActiveWorkspaceId(), 202);
  assert.deepEqual(w.h.paths, []);
  assert.equal(w.getWorkspaceSnapshot().message, "워크스페이스 팀 A 로 전환됨");
  w.h.items = [personal];
  await w.initializeWorkspaces(7, "303");
  assert.equal(w.getWorkspaceSnapshot().notFound, true);
  assert.equal(w.getActiveWorkspaceId(), 202);
  assert.equal(w.h.values.get("workspace:7"), "202");
});

test("a full reload with the already stored workspace query does not announce a switch", async (t) => {
  const values = new Map([["workspace:7", "202"]]);
  const first = await harness(t, { values });
  await first.initializeWorkspaces(7, "202");
  assert.equal(first.getActiveWorkspaceId(), 202);
  assert.equal(first.getWorkspaceSnapshot().message, null);

  // A new module instance has no in-memory active workspace, like a full load.
  const reloaded = await harness(t, { values });
  await reloaded.initializeWorkspaces(7, "202");
  assert.equal(reloaded.getActiveWorkspaceId(), 202);
  assert.equal(reloaded.getWorkspaceSnapshot().message, null);
});

test("a dismissed switch notice stays dismissed across same-workspace menu navigation", async (t) => {
  const w = await harness(t);
  await w.initializeWorkspaces(7);
  w.switchWorkspace(202);
  assert.equal(w.getWorkspaceSnapshot().message, "워크스페이스 팀 A 로 전환됨");
  w.dismissWorkspaceMessage();
  await w.initializeWorkspaces(7, "202");
  assert.equal(w.getWorkspaceSnapshot().message, null);
  await w.initializeWorkspaces(7, "202");
  assert.equal(w.getWorkspaceSnapshot().message, null);
});

test("a different workspace query announces once and persists its selection", async (t) => {
  const w = await harness(t, { values: new Map([["workspace:7", "101"]]) });
  await w.initializeWorkspaces(7, "202");
  assert.equal(w.getActiveWorkspaceId(), 202);
  assert.equal(w.getWorkspaceSnapshot().message, "워크스페이스 팀 A 로 전환됨");
  assert.equal(w.h.values.get("workspace:7"), "202");
  w.dismissWorkspaceMessage();
  await w.initializeWorkspaces(7, "202");
  assert.equal(w.getWorkspaceSnapshot().message, null);
  assert.deepEqual(w.h.paths, [], "deep-link initialization preserves its destination route");
});

test("same-workspace initialization preserves explicit switch and invitation notices", async (t) => {
  const w = await harness(t);
  await w.initializeWorkspaces(7);
  w.switchWorkspace(202);
  await w.initializeWorkspaces(7, "202");
  assert.equal(w.getWorkspaceSnapshot().message, "워크스페이스 팀 A 로 전환됨");
  const custom = "'팀 A'으로 이동했어요";
  w.switchWorkspace(202, custom);
  await w.initializeWorkspaces(7, "202");
  assert.equal(w.getWorkspaceSnapshot().message, custom);
  await w.reloadWorkspaces();
  assert.equal(w.getWorkspaceSnapshot().message, custom);
});

test("a nonmember query remains 404 without changing the stored selection or announcing", async (t) => {
  const w = await harness(t, { values: new Map([["workspace:7", "202"]]) });
  await w.initializeWorkspaces(7);
  await w.initializeWorkspaces(7, "999");
  assert.equal(w.getWorkspaceSnapshot().notFound, true);
  assert.equal(w.getWorkspaceSnapshot().message, null);
  assert.equal(w.getActiveWorkspaceId(), 202);
  assert.equal(w.h.values.get("workspace:7"), "202");
});

test("malformed ws parameters cannot select a membership", async (t) => {
  for (const query of ["", "0", "-1", "202oops", "1.5", "99999999999999999999999"]) {
    const w = await harness(t);
    await w.initializeWorkspaces(7, query);
    assert.equal(w.getWorkspaceSnapshot().notFound, true, query);
    assert.equal(w.getActiveWorkspaceId(), null);
  }
});

test("a late response from the previous account cannot overwrite the next account", async (t) => {
  let release;
  const w = await harness(t, { load: () => new Promise(resolve => { release = resolve; }) });
  const old = w.initializeWorkspaces(7);
  w.h.load = () => Promise.resolve([{ ...personal, id: 108 }]);
  await w.initializeWorkspaces(8);
  release([personal, team]);
  await old;
  assert.equal(w.getWorkspaceSnapshot().userId, 8);
  assert.equal(w.getActiveWorkspaceId(), 108);
});

test("reload falls back to personal after leaving or losing membership", async (t) => {
  const w = await harness(t);
  await w.initializeWorkspaces(7);
  w.switchWorkspace(202);
  w.h.items = [personal];
  await w.reloadWorkspaces();
  assert.equal(w.getActiveWorkspaceId(), 101);
  assert.equal(w.h.values.get("workspace:7"), "101");
});

test("logout invalidates scope and prevents an in-flight boot restoring it", async (t) => {
  let release;
  const w = await harness(t, { load: () => new Promise(resolve => { release = resolve; }) });
  const old = w.initializeWorkspaces(7);
  w.resetWorkspaces();
  release([personal, team]);
  await old;
  assert.equal(w.getActiveWorkspaceId(), null);
  assert.equal(w.getWorkspaceSnapshot().userId, null);
});

test("app links preserve ws and hash; workspace navigation never reloads the document", async (t) => {
  const original = globalThis.window;
  const target = new EventTarget();
  const location = new URL("https://annodock.test/datasets/5/viewer?ws=202");
  const assigned = [];
  globalThis.window = Object.assign(target, {
    location: Object.assign(location, { assign: path => assigned.push(path) }),
    history: { pushState: (_data, _title, path) => { location.href = new URL(path, location).href; } },
  });
  t.after(() => { globalThis.window = original; });
  const source = await readFile(new URL("../src/navigation.ts", import.meta.url), "utf8");
  const nav = await import(`${url(compile(source))}#${Math.random()}`);
  assert.equal(nav.appHref("/runs/4?tab=metrics#last"), "/runs/4?tab=metrics&ws=202#last");
  assert.equal(nav.appHref("/projects?ws=101"), "/projects?ws=101");
  assert.equal(nav.appHref("/login?next=%2Fmembers"), "/login?next=%2Fmembers");
  let updates = 0;
  const unsubscribe = nav.subscribeNavigation(() => updates++);
  nav.navigateWithinApp("/projects?ws=101");
  assert.equal(nav.getLocationSnapshot(), "/projects?ws=101");
  assert.equal(updates, 1);
  assert.deepEqual(assigned, []);
  unsubscribe();
});
