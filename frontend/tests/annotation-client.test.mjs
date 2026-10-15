import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import ts from "typescript";

const moduleUrl = (source) => `data:text/javascript;base64,${Buffer.from(source).toString("base64")}`;
const json = (body, status = 200) => new Response(JSON.stringify(body), {
  status, headers: { "Content-Type": "application/json" },
});
const boxes = [{ class_id: 0, cx: 0.5, cy: 0.5, w: 0.2, h: 0.3 }];
const image = { id: 101, stem: "image", split: "train", version: 8,
  label_status: "in_progress", assignee_id: 3, labeled_by: null, labeled_at: null };
const classes = [{ class_id: 0, name: "cat" }];
const annotations = { image_id: 101, width: 100, height: 80, version: 8,
  label_status: "in_progress", assignee_id: 3, boxes: [{ id: 91, ...boxes[0] }], catalog_version: 12, classes };

function deferred() {
  let resolve;
  const promise = new Promise((yes) => { resolve = yes; });
  return { promise, resolve };
}

async function harness(t, respond = () => json(annotations)) {
  // Exercise the real helper → requestJson → apiFetch → fetch chain.
  // Only the HTTP/browser boundary and auth/workspace stores are substituted.
  const originals = { window: globalThis.window, fetch: globalThis.fetch };
  const h = {
    workspaceId: 22, calls: [], refreshCalls: [],
    snapshot: { accessToken: "access-old", refreshToken: "refresh-token", user: { id: 3 } },
  };
  h.refresh = async (staleAccessToken) => {
    h.refreshCalls.push(staleAccessToken);
    return "access-new"; // refreshAuthSession returns the access-token string.
  };
  globalThis.window = {
    location: { origin: "https://annodock.test", pathname: "/datasets/10/viewer", search: "?ws=22" },
    setTimeout, clearTimeout, dispatchEvent: () => true,
  };
  globalThis.fetch = async (path, init) => {
    const call = { path, init, headers: new Headers(init.headers),
      body: init.body === undefined ? undefined : JSON.parse(init.body) };
    h.calls.push(call);
    return respond(call, h);
  };
  const key = `__annotationClient${Math.random().toString(16).slice(2)}`;
  globalThis[key] = h;
  t.after(() => { Object.assign(globalThis, originals); delete globalThis[key]; });
  const bridge = `const h = globalThis[${JSON.stringify(key)}];`;
  const dependencies = {
    "./auth": moduleUrl(`${bridge} export const refreshAuthSession = (...args) => h.refresh(...args);`),
    "../store/auth": moduleUrl(`${bridge} export const getAuthSnapshot = () => h.snapshot;`),
    "../store/workspace": moduleUrl(`${bridge} export const getActiveWorkspaceId = () => h.workspaceId;`),
  };
  const source = await readFile(new URL("../src/api/client.ts", import.meta.url), "utf8");
  const compiled = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
  }).outputText.replace(/from "([^"]+)"/g, (_match, name) => {
    assert.ok(dependencies[name], `unmapped dependency ${name}`);
    return `from "${dependencies[name]}"`;
  });
  h.client = await import(moduleUrl(compiled));
  return h;
}

function assertRequest(call, { path, method, body, workspaceId = 11, accessToken = "access-old" }) {
  assert.equal(call.path, path);
  assert.equal(call.init.method ?? "GET", method);
  assert.deepEqual(call.body, body);
  assert.equal(call.headers.get("X-Workspace-Id"), workspaceId === null ? null : String(workspaceId));
  assert.equal(call.headers.get("Authorization"), `Bearer ${accessToken}`);
  assert.equal(call.init.credentials, "include");
  assert.equal(call.init.cache, "no-store");
  assert.equal(Object.hasOwn(call.init, "workspaceId"), false, "internal scope is encoded only as a header");
  if (body !== undefined) assert.equal(call.headers.get("Content-Type"), "application/json");
}

for (const [expectedVersion, expectedCatalogVersion] of [[0, 0], [7, 12]]) {
  test(`annotation PUT includes image version ${expectedVersion}, catalog version ${expectedCatalogVersion}, and the explicit workspace`, async (t) => {
    const response = { ...annotations, version: expectedVersion + 1, catalog_version: expectedCatalogVersion, is_modified: true };
    const h = await harness(t, () => json(response));
    assert.deepEqual(await h.client.saveImageAnnotations(101, boxes, 11, expectedVersion, expectedCatalogVersion), response);
    assert.equal(h.calls.length, 1);
    assertRequest(h.calls[0], { path: "/api/images/101/annotations", method: "PUT",
      body: { boxes, expected_version: expectedVersion, expected_catalog_version: expectedCatalogVersion } });
  });
}

test("annotation GET performs fresh requests in the supplied workspace after active scope changes", async (t) => {
  const h = await harness(t, (_call, state) => json({ ...annotations, version: state.calls.length }));
  const controller = new AbortController();
  const first = await h.client.getImageAnnotations(101, controller.signal, 11);
  h.workspaceId = 33;
  const second = await h.client.getImageAnnotations(101, undefined, 11);
  assert.deepEqual([first.version, second.version], [1, 2]);
  assert.deepEqual([first.catalog_version, second.catalog_version], [12, 12]);
  assert.deepEqual(first.classes, classes);
  assert.deepEqual(second.classes, classes);
  assert.equal(h.calls.length, 2);
  for (const call of h.calls) {
    assertRequest(call, { path: "/api/images/101/annotations", method: "GET" });
    assert.ok(call.init.signal instanceof AbortSignal);
  }
});

test("an explicit headerless scope is preserved by annotation GET and PUT", async (t) => {
  const h = await harness(t);
  await h.client.getImageAnnotations(101, undefined, null);
  await h.client.saveImageAnnotations(101, [], null, 0, 0);
  assertRequest(h.calls[0], { path: "/api/images/101/annotations", method: "GET", workspaceId: null });
  assertRequest(h.calls[1], { path: "/api/images/101/annotations", method: "PUT",
    body: { boxes: [], expected_version: 0, expected_catalog_version: 0 }, workspaceId: null });
});

test("dataset classes preserve their catalog version in the explicit workspace", async (t) => {
  const response = { classes, catalog_version: 12 };
  const h = await harness(t, () => json(response));
  assert.deepEqual(await h.client.getDatasetClasses(10, 11), response);
  assertRequest(h.calls[0], { path: "/api/datasets/10/classes", method: "GET" });
});

test("class rename returns the complete new catalog and version alongside the renamed row", async (t) => {
  const response = { class_id: 0, name: "kitty", catalog_version: 13, classes: [{ class_id: 0, name: "kitty" }] };
  const h = await harness(t, () => json(response));
  assert.deepEqual(await h.client.renameDatasetClass(10, 0, "kitty"), response);
  assertRequest(h.calls[0], { path: "/api/datasets/10/classes/0", method: "PATCH", body: { name: "kitty" }, workspaceId: 22 });
});

for (const expectedCatalogVersion of [0, 12]) {
  test(`class rename sends expected catalog version ${expectedCatalogVersion} in its original workspace`, async (t) => {
    const response = { class_id: 0, name: "kitty", catalog_version: expectedCatalogVersion + 1,
      classes: [{ class_id: 0, name: "kitty" }] };
    const h = await harness(t, () => json(response));
    assert.deepEqual(await h.client.renameDatasetClass(10, 0, "kitty", expectedCatalogVersion, 11), response);
    assertRequest(h.calls[0], { path: "/api/datasets/10/classes/0", method: "PATCH",
      body: { name: "kitty", expected_catalog_version: expectedCatalogVersion } });
  });
}

test("a rename catalog conflict preserves its structured response and does not automatically retry PATCH", async (t) => {
  const detail = { code: "class_catalog_changed", current: { ...annotations, catalog_version: 13 } };
  const h = await harness(t, () => json({ detail }, 409));
  await assert.rejects(h.client.renameDatasetClass(10, 0, "kitty", 12, 11), (error) => {
    assert.equal(error.status, 409);
    assert.deepEqual(error.detail, detail);
    return true;
  });
  assert.equal(h.calls.length, 1);
  assertRequest(h.calls[0], { path: "/api/datasets/10/classes/0", method: "PATCH",
    body: { name: "kitty", expected_catalog_version: 12 } });
});

for (const operation of [
  { name: "claim all splits", call: (api) => api.claimDatasetImage(10, null, 11),
    path: "/api/datasets/10/claim", method: "POST", body: { split: null }, response: { image: null } },
  { name: "claim current split", call: (api) => api.claimDatasetImage(10, "train", 11),
    path: "/api/datasets/10/claim", method: "POST", body: { split: "train" }, response: { image, position: 0 } },
  { name: "complete", call: (api) => api.completeImage(101, 11),
    path: "/api/images/101/complete", method: "POST", response: image },
  { name: "uncomplete", call: (api) => api.uncompleteImage(101, 11),
    path: "/api/images/101/uncomplete", method: "POST", response: image },
  { name: "assign", call: (api) => api.assignImage(101, 4, 11),
    path: "/api/images/101/assignment", method: "PATCH", body: { assignee_id: 4 }, response: image },
  { name: "reclaim", call: (api) => api.assignImage(101, null, 11),
    path: "/api/images/101/assignment", method: "PATCH", body: { assignee_id: null }, response: image },
  { name: "refresh image metadata", call: (api) => api.getImage(101, 11),
    path: "/api/images/101", method: "GET", response: image },
]) {
  test(`${operation.name} uses the documented HTTP path, body and operation workspace`, async (t) => {
    const h = await harness(t, () => json(operation.response));
    assert.deepEqual(await operation.call(h.client), operation.response);
    assert.equal(h.calls.length, 1);
    assertRequest(h.calls[0], operation);
  });
}

test("a 401 annotation retry retains both versions, boxes and workspace while refreshing auth", async (t) => {
  const first = deferred();
  const response = { ...annotations, version: 1, is_modified: true };
  const h = await harness(t, (_call, state) => state.calls.length === 1 ? first.promise : json(response));
  const request = h.client.saveImageAnnotations(101, boxes, 11, 0, 12);
  h.workspaceId = 33;
  first.resolve(json({ detail: "token expired" }, 401));
  assert.deepEqual(await request, response);
  assert.deepEqual(h.refreshCalls, ["access-old"]);
  assert.equal(h.calls.length, 2);
  for (const [index, call] of h.calls.entries()) assertRequest(call, {
    path: "/api/images/101/annotations", method: "PUT", body: { boxes, expected_version: 0, expected_catalog_version: 12 },
    accessToken: index === 0 ? "access-old" : "access-new",
  });
});

for (const detail of [
  { code: "version_conflict", current: { ...annotations, version: 9 } },
  { code: "class_catalog_changed", current: { ...annotations, catalog_version: 13, classes: [{ class_id: 0, name: "kitty" }] } },
  { code: "image_assigned_to_other", assignee_id: 4 },
]) {
  test(`${detail.code} preserves the structured HTTP error for the annotation hook`, async (t) => {
    const h = await harness(t, () => json({ detail }, 409));
    await assert.rejects(h.client.saveImageAnnotations(101, boxes, 11, 7, 12), (error) => {
      assert.ok(error instanceof h.client.ApiError);
      assert.equal(error.status, 409);
      assert.deepEqual(error.detail, detail);
      return true;
    });
    assert.equal(h.calls.length, 1, "a collaboration conflict must not trigger an HTTP retry");
  });
}
