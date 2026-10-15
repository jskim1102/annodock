import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import ts from "typescript";

const moduleUrl = (source) => `data:text/javascript;base64,${Buffer.from(source).toString("base64")}`;
const settle = () => new Promise((resolve) => setImmediate(resolve));

async function compile(path, dependencies) {
  const source = await readFile(new URL(path, import.meta.url), "utf8");
  return moduleUrl(ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX },
  }).outputText.replace(/from "([^"]+)"/g, (_match, name) => `from "${dependencies[name]}"`));
}

test("workspace helpers bind path IDs and send the documented mutation bodies", async (t) => {
  const key = `__membersApi${Math.random().toString(16).slice(2)}`;
  const calls = [];
  globalThis[key] = calls;
  t.after(() => { delete globalThis[key]; });
  const client = moduleUrl(`
    export class ApiError extends Error {}
    export const requestJson = async (path, init) => { globalThis[${JSON.stringify(key)}].push({path,init}); return {}; };
  `);
  const api = await import(await compile("../src/api/workspaces.ts", { "./client": client }));
  await api.createWorkspace("Vision");
  await api.getInvitePreview("abc/def");
  await api.acceptInvite("abc/def");
  await api.changeWorkspaceMemberRole(71, 5, "editor");
  await api.removeWorkspaceMember(71, 5);
  await api.transferWorkspaceOwner(71, 5);
  await api.renameWorkspace(71, "Renamed");
  await api.leaveWorkspace(71);
  await api.deleteWorkspace(71);
  await api.createWorkspaceInvite(71, { role: "editor", expires_in_days: 7, max_uses: null });
  await api.revokeWorkspaceInvite(71, 9);
  assert.deepEqual(calls.map(({ path, init }) => [path, init?.method ?? "GET", init?.body ? JSON.parse(init.body) : null]), [
    ["/api/workspaces", "POST", { name: "Vision" }],
    ["/api/invites/abc%2Fdef", "GET", null], ["/api/invites/abc%2Fdef/accept", "POST", null],
    ["/api/workspaces/71/members/5", "PATCH", { role: "editor" }],
    ["/api/workspaces/71/members/5", "DELETE", null],
    ["/api/workspaces/71/owner", "POST", { user_id: 5 }],
    ["/api/workspaces/71", "PATCH", { name: "Renamed" }],
    ["/api/workspaces/71/members/me", "DELETE", null], ["/api/workspaces/71", "DELETE", null],
    ["/api/workspaces/71/invites", "POST", { role: "editor", expires_in_days: 7, max_uses: null }],
    ["/api/workspaces/71/invites/9", "DELETE", null],
  ]);
  assert.ok(calls.every(({ init }) => init.workspaceId === undefined), "apiFetch captures the active header; management scope remains the path ID");
});

const storeDependencies = moduleUrl(`
  export const useSyncExternalStore = () => {};
  export const requestJson = async () => ({}), invalidateStorageQuotaCache = () => {};
  export const navigateWithinApp = () => {}, clearAuthenticatedResourceCache = () => {};
  export const readStoredString = () => null, writeStoredString = () => {};
`);
const ROLES = (await import(await compile("../src/store/workspace.ts", {
  react: storeDependencies, "../api/client": storeDependencies, "../navigation": storeDependencies,
  "../utils/authenticatedResourceCache": storeDependencies, "../utils/storage": storeDependencies,
}))).WORKSPACE_ROLE_LABELS;
// Historical viewer memberships stay in the fixture to preserve permission checks.
const MEMBER_ROLES = ["owner", "admin", "editor", "viewer"];
const members = MEMBER_ROLES.map((role, index) => ({
  user_id: index + 1, username: role, email: `${role}@example.test`, role, joined_at: "2026-10-01T00:00:00Z",
}));

function elements(node) {
  if (!node || typeof node !== "object") return [];
  return [node, ...[node.props?.children].flat(Infinity).flatMap(elements)];
}
function textOf(node) {
  if (typeof node === "string" || typeof node === "number") return String(node);
  if (!node || typeof node !== "object") return "";
  return [node.props?.children].flat(Infinity).map(textOf).join("");
}

async function renderHarness(t, { role = "owner", kind = "team", component = "MembersPage", respond } = {}) {
  const key = `__membersUI${Math.random().toString(16).slice(2)}`;
  const personal = { id: 8, name: "개인", kind: "personal", role: "owner" };
  const active = kind === "personal" ? personal : { id: 71, name: "Vision", kind, role };
  const context = {
    requests: [], reloads: 0, switched: [], copied: [], closed: 0, created: [],
    workspace: { active, items: [personal, ...(kind === "team" ? [active] : [])], userId: MEMBER_ROLES.indexOf(role) + 1 },
    members: structuredClone(members), invites: [{ id: 9, role: "editor", expires_at: "2099-10-20T00:00:00Z",
      max_uses: null, uses: 1, created_by: 2, created_at: "2026-10-01T00:00:00Z" }],
  };
  const originalWindow = globalThis.window;
  const originalNavigator = Object.getOwnPropertyDescriptor(globalThis, "navigator");
  globalThis.window = { location: { origin: "https://annodock.test" }, addEventListener() {}, removeEventListener() {} };
  Object.defineProperty(globalThis, "navigator", { configurable: true, value: { clipboard: { writeText: async (value) => context.copied.push(value) } } });
  globalThis[key] = context;
  const slots = [];
  const effects = [];
  const cleanups = new Map();
  let index = 0;
  let mounted = false;
  context.react = {
    useState(initial) {
      const slot = index++;
      if (!mounted) slots[slot] = typeof initial === "function" ? initial() : initial;
      return [slots[slot], (value) => { slots[slot] = typeof value === "function" ? value(slots[slot]) : value; }];
    },
    useRef(current) { const slot = index++; if (!mounted) slots[slot] = { current }; return slots[slot]; },
    useEffect(fn, deps) {
      const slot = index++;
      if (!mounted || deps.some((value, offset) => value !== slots[slot]?.[offset])) effects.push(() => {
        cleanups.get(slot)?.(); cleanups.set(slot, fn()); slots[slot] = deps;
      });
    },
    useId() { return `fixture-${index++}`; },
  };
  context.respond = async (path, init) => {
    context.requests.push({ path, init });
    if (respond) return respond(path, init, context);
    if (path.endsWith("/members") && init.method === "GET") return { items: context.members };
    if (path.endsWith("/invites") && init.method === "GET") return context.invites;
    if (path.endsWith("/invites") && init.method === "POST") return {
      invite: { ...context.invites[0], ...JSON.parse(init.body), id: 10 }, accept_url: "/invites/transient-secret",
    };
    if (init.method === "PATCH" && path.includes("/members/")) return { user_id: Number(path.split("/").at(-1)), ...JSON.parse(init.body) };
    return {};
  };
  const clientModule = moduleUrl(`
    export class ApiError extends Error { constructor(status, message, detail) { super(message); this.status=status; this.detail=detail; } }
    export const requestJson = (path, init) => globalThis[${JSON.stringify(key)}].respond(path, init);
  `);
  const apiModule = await compile("../src/api/workspaces.ts", { "./client": clientModule });
  const stubModule = moduleUrl(`
    const c = globalThis[${JSON.stringify(key)}];
    export const {useState,useRef,useEffect,useId} = c.react;
    export const useWorkspace = () => c.workspace, getWorkspaceSnapshot = () => c.workspace;
    export const reloadWorkspaces = async () => { c.reloads++; if(c.onReload) c.onReload(); };
    export const switchWorkspace = (id) => c.switched.push(id);
    export const WORKSPACE_ROLE_LABELS = ${JSON.stringify(ROLES)};
    export const jsx = (type, props) => ({type,props}), jsxs = jsx, Fragment = "fragment";
    export const AppShell = () => {}, Icon = () => {}, NewWorkspaceDialog = () => {};
  `);
  const dependencies = { react: stubModule, "react/jsx-runtime": stubModule,
    "../api/client": clientModule, "../api/workspaces": apiModule,
    "../store/workspace": stubModule, "../components/AppShell": stubModule,
    "../components/Icon": stubModule, "./Icon": stubModule,
    "../components/NewWorkspaceDialog": stubModule };
  const dialogModule = await compile("../src/components/InviteLinkDialog.tsx", dependencies);
  dependencies["../components/InviteLinkDialog"] = dialogModule;
  const loaded = await import(component === "InviteLinkDialog" ? dialogModule : await compile("../src/pages/MembersPage.tsx", dependencies));
  const props = { workspaceId: 71, onClose: () => { context.closed++; }, onCreated: (invite) => context.created.push(invite) };
  const render = () => {
    index = 0;
    const tree = loaded[component](props);
    mounted = true;
    effects.splice(0).forEach((effect) => effect());
    return tree;
  };
  t.after(() => {
    cleanups.forEach((cleanup) => cleanup?.());
    globalThis.window = originalWindow;
    if (originalNavigator) Object.defineProperty(globalThis, "navigator", originalNavigator);
    else delete globalThis.navigator;
    delete globalThis[key];
  });
  render();
  await settle();
  return { context, render, api: await import(clientModule),
    find(predicate) { return elements(render()).find(predicate); },
    button(label) { return elements(render()).find((node) => node.type === "button" && textOf(node) === label); },
    text() { return textOf(render()); },
  };
}

for (const role of MEMBER_ROLES) {
  test(`members controls follow the ${role} workspace role`, async (t) => {
    const view = await renderHarness(t, { role });
    const manage = ["owner", "admin"].includes(role);
    assert.equal(Boolean(view.button("＋ 초대 링크 만들기")), manage);
    assert.equal(Boolean(view.button("변경")), manage);
    assert.equal(Boolean(view.button("넘기기")), role === "owner");
    assert.equal(Boolean(view.button("삭제")), role === "owner");
    assert.equal(Boolean(view.find((node) => node.type === "select" && node.props["aria-label"] === "viewer 역할")), manage);
    assert.equal(Boolean(view.find((node) => node.type === "select" && node.props["aria-label"] === "owner 역할")), false);
    assert.ok(view.text().includes("owner@example.test"));
    assert.equal(view.context.requests.filter(({path}) => path.endsWith("/invites")).length, manage ? 1 : 0);
  });
}

for (const role of ["editor", "viewer"]) {
  test(`${role} sees the historical viewer member's read-only role label`, async (t) => {
    const view = await renderHarness(t, { role });
    const row = view.find((node) => node.type === "tr" && textOf(node).includes("viewer@example.test"));
    assert.ok(row);
    const cells = elements(row).filter((node) => node.type === "td");
    assert.equal(textOf(cells[1]), "보기 전용");
    assert.equal(elements(row).some((node) => node.type === "select"), false);
    assert.equal(view.context.requests.some(({ init }) => init.method === "PATCH"), false);
  });
}

test("personal workspace shows the one-person empty state and performs no member requests", async (t) => {
  const view = await renderHarness(t, { kind: "personal" });
  assert.match(view.text(), /나만 쓰는 공간 — 팀을 만들면 함께 작업할 수 있어요/);
  assert.ok(view.button("＋ 새 팀"));
  assert.equal(view.context.requests.length, 0);
});

test("admin role changes update the row and reload role controls", async (t) => {
  const view = await renderHarness(t, { role: "admin" });
  const select = view.find((node) => node.type === "select" && node.props["aria-label"] === "viewer 역할");
  select.props.onChange({ target: { value: "editor" } });
  await settle();
  const mutation = view.context.requests.find(({init}) => init.method === "PATCH");
  assert.equal(mutation.path, "/api/workspaces/71/members/4");
  assert.deepEqual(JSON.parse(mutation.init.body), { role: "editor" });
  assert.equal(view.find((node) => node.type === "select" && node.props["aria-label"] === "viewer 역할").props.value, "editor");
  assert.equal(view.context.reloads, 1);
});

test("directory 503 is shown where the member table would be with retry", async (t) => {
  const view = await renderHarness(t, { role: "editor", respond: async () => {
    const error = new Error("directory down"); error.status = 503; throw error;
  } });
  assert.match(view.text(), /멤버 정보를 불러오지 못했어요 — 잠시 뒤 다시 시도/);
  assert.ok(view.button("다시 시도"));
});

test("invite creation keeps its raw URL only in the dialog and copies it once requested", async (t) => {
  const view = await renderHarness(t, { component: "InviteLinkDialog" });
  const form = view.find((node) => node.type === "form");
  form.props.onSubmit({ preventDefault() {} });
  await settle();
  const mutation = view.context.requests.find(({init}) => init.method === "POST");
  assert.deepEqual(JSON.parse(mutation.init.body), { role: "editor", expires_in_days: 7, max_uses: null });
  const input = view.find((node) => node.type === "input" && node.props.readOnly);
  assert.equal(input.props.value, "https://annodock.test/invites/transient-secret");
  view.button("복사").props.onClick();
  await settle();
  assert.deepEqual(view.context.copied, [input.props.value]);
  assert.match(view.text(), /복사했어요/);
  assert.equal(view.find((node) => node.props?.role === "status" && textOf(node) === "복사했어요").props.className, "toast");
  assert.match(view.text(), /이 창을 닫으면 링크를 다시 볼 수 없어요/);
  assert.equal(view.context.created.length, 1);
  assert.equal(JSON.stringify(view.context.created).includes("transient-secret"), false);
  view.button("닫기").props.onClick();
  assert.equal(view.context.closed, 1);
});

const intercept = (view, predicate, action) => {
  const original = view.context.respond;
  view.context.respond = async (path, init) => {
    if (!predicate(path, init)) return original(path, init);
    view.context.requests.push({ path, init });
    return action(path, init);
  };
};

function openMemberRemoval(view, name = "viewer") {
  view.find((node) => node.type === "button" && node.props["aria-label"] === `${name} 메뉴`).props.onClick();
  view.button("내보내기").props.onClick();
}

test("removing a member requires confirmation and explains image unassignment", async (t) => {
  const view = await renderHarness(t, { role: "admin" });
  openMemberRemoval(view);
  assert.match(view.text(), /viewer 님을 내보낼까요\? 작업 중이던 이미지는 다시 미배정이 돼요/);
  assert.equal(view.context.requests.filter(({init}) => init.method === "DELETE").length, 0);
  view.button("취소").props.onClick();
  assert.equal(view.context.requests.filter(({init}) => init.method === "DELETE").length, 0);
  openMemberRemoval(view);
  view.button("확인").props.onClick();
  await settle();
  assert.ok(view.context.requests.some(({path, init}) => path === "/api/workspaces/71/members/4" && init.method === "DELETE"));
  assert.doesNotMatch(view.text(), /viewer@example.test/);
  assert.deepEqual(view.context.switched, []);
});

test("nonowner leave and owner deletion reload then return to the personal workspace", async (t) => {
  for (const [role, label, path] of [["editor", "탈퇴", "/api/workspaces/71/members/me"], ["owner", "삭제", "/api/workspaces/71"]]) {
    await t.test(label, async (t) => {
      const view = await renderHarness(t, { role });
      view.button(label).props.onClick();
      view.button("확인").props.onClick();
      await settle();
      assert.ok(view.context.requests.some((request) => request.path === path && request.init.method === "DELETE"));
      assert.equal(view.context.reloads, 1);
      assert.deepEqual(view.context.switched, [8]);
    });
  }
});

test("ownership transfer retains the team and removes former owner controls", async (t) => {
  const view = await renderHarness(t);
  view.find((node) => node.type === "select" && node.props.id === "workspace-next-owner").props.onChange({ target: { value: "3" } });
  view.button("넘기기").props.onClick();
  assert.match(view.text(), /editor 님에게 소유권을 넘길까요\? 나는 관리자가 돼요/);
  view.context.onReload = () => {
    view.context.workspace.active = { ...view.context.workspace.active, role: "admin" };
    view.context.members = view.context.members.map((member) => ({ ...member,
      role: member.user_id === 1 ? "admin" : member.user_id === 3 ? "owner" : member.role }));
  };
  view.button("확인").props.onClick();
  await settle();
  view.render(); await settle();
  const mutation = view.context.requests.find(({path}) => path.endsWith("/owner"));
  assert.deepEqual(JSON.parse(mutation.init.body), { user_id: 3 });
  assert.equal(view.context.reloads, 1);
  assert.deepEqual(view.context.switched, []);
  assert.equal(view.button("삭제"), undefined);
  assert.equal(view.button("넘기기"), undefined);
  assert.ok(view.button("＋ 초대 링크 만들기"));
});

test("self demotion reloads permissions and hides management controls", async (t) => {
  const view = await renderHarness(t, { role: "admin" });
  view.context.onReload = () => { view.context.workspace.active = { ...view.context.workspace.active, role: "editor" }; };
  view.find((node) => node.type === "select" && node.props["aria-label"] === "admin 역할").props.onChange({ target: { value: "editor" } });
  await settle(); view.render(); await settle();
  assert.equal(view.button("＋ 초대 링크 만들기"), undefined);
  assert.equal(view.button("변경"), undefined);
  assert.equal(view.find((node) => node.type === "select" && node.props["aria-label"] === "viewer 역할"), undefined);
});

test("nonempty team deletion stays in the confirmation and displays blocking project names", async (t) => {
  const view = await renderHarness(t);
  intercept(view, (path, init) => path === "/api/workspaces/71" && init.method === "DELETE", () => {
    throw new view.api.ApiError(409, "not empty", { code: "workspace_not_empty", projects: [{ id: 4, name: "Road signs" }, { id: 5, name: "Factory" }] });
  });
  view.button("삭제").props.onClick(); view.button("확인").props.onClick();
  await settle();
  assert.match(view.text(), /프로젝트나 학습 기록이 남아 있어 삭제할 수 없어요/);
  assert.match(view.text(), /Road signs/);
  assert.match(view.text(), /Factory/);
  assert.ok(view.button("확인"));
  assert.deepEqual(view.context.switched, []);
  assert.equal(view.context.reloads, 0);
});

for (const [label, projects, runs, expectedItems] of [
  ["retained training runs only", [], [{ id: 42, dataset_name: "Archived road signs" }], ["학습 #42 · Archived road signs"]],
  ["both projects and retained training runs", [{ id: 4, name: "Factory" }], [{ id: 42, dataset_name: "Archived road signs" }], ["Factory", "학습 #42 · Archived road signs"]],
  ["a retained training run without a stored name", [], [{ id: 43, dataset_name: null }], null],
]) {
  test(`nonempty team deletion displays ${label} and keeps the confirmation open`, async (t) => {
    const view = await renderHarness(t);
    intercept(view, (path, init) => path === "/api/workspaces/71" && init.method === "DELETE", () => {
      throw new view.api.ApiError(409, "not empty", { code: "workspace_not_empty", projects, runs });
    });
    view.button("삭제").props.onClick();
    view.button("확인").props.onClick();
    await settle();
    assert.equal(textOf(view.find((node) => node.props?.role === "alert")), "프로젝트나 학습 기록이 남아 있어 삭제할 수 없어요");
    const items = elements(view.render()).filter((node) => node.type === "li").map(textOf);
    if (expectedItems) assert.deepEqual(items, expectedItems);
    else {
      assert.equal(items.length, 1);
      assert.match(items[0], /^학습 #43 · \S+/u, "missing dataset names still show a readable fallback after the run ID");
      assert.doesNotMatch(items[0], /\b(?:null|undefined)\b/u);
    }
    assert.ok(view.button("확인"));
    assert.equal(view.button("확인").props.disabled, false);
    assert.equal(view.context.requests.filter(({ init }) => init.method === "DELETE").length, 1);
    assert.deepEqual(view.context.switched, []);
    assert.equal(view.context.reloads, 0);
  });
}

test("missing membership on a mutation falls back to the existing 404 page", async (t) => {
  const view = await renderHarness(t, { role: "admin" });
  intercept(view, (path, init) => path.endsWith("/members/4") && init.method === "DELETE", () => {
    throw new view.api.ApiError(404, "missing");
  });
  openMemberRemoval(view); view.button("확인").props.onClick();
  await settle();
  assert.match(view.text(), /페이지를 찾을 수 없습니다/);
  assert.doesNotMatch(view.text(), /owner@example.test/);
});

test("revoking an already missing invite removes its row without navigating to 404", async (t) => {
  const view = await renderHarness(t);
  intercept(view, (path, init) => path.endsWith("/invites/9") && init.method === "DELETE", () => {
    throw new view.api.ApiError(404, "missing");
  });
  view.button("철회").props.onClick(); await settle();
  assert.equal(view.button("철회"), undefined);
  assert.match(view.text(), /활성 초대 링크가 없어요/);
  assert.doesNotMatch(view.text(), /페이지를 찾을 수 없습니다/);
});

test("renaming trims the name, blocks whitespace, and refreshes the workspace", async (t) => {
  const view = await renderHarness(t, { role: "admin" });
  view.button("변경").props.onClick();
  const input = () => view.find((node) => node.type === "input" && node.props["aria-label"] === "팀 이름");
  input().props.onChange({ target: { value: "   " } });
  assert.equal(view.button("저장").props.disabled, true);
  input().props.onChange({ target: { value: "  New team  " } });
  view.find((node) => node.type === "form").props.onSubmit({ preventDefault() {} });
  await settle();
  const mutation = view.context.requests.find(({init}) => init.method === "PATCH");
  assert.deepEqual(JSON.parse(mutation.init.body), { name: "New team" });
  assert.equal(view.context.reloads, 1);
});

test("limited invite validates counts and submits selected role and expiration", async (t) => {
  const view = await renderHarness(t, { component: "InviteLinkDialog" });
  view.button("관리자").props.onClick();
  view.find((node) => node.type === "select").props.onChange({ target: { value: "14" } });
  view.find((node) => node.props?.type === "checkbox").props.onChange({ target: { checked: true } });
  const count = () => view.find((node) => node.props?.["aria-label"] === "최대 사용 횟수");
  for (const value of ["0", "-1", "1.5", "2147483648", ""]) {
    count().props.onChange({ target: { value } });
    assert.equal(view.button("만들기").props.disabled, true);
    view.find((node) => node.type === "form").props.onSubmit({ preventDefault() {} });
  }
  assert.equal(view.context.requests.length, 0);
  count().props.onChange({ target: { value: "3" } });
  view.find((node) => node.type === "form").props.onSubmit({ preventDefault() {} });
  await settle();
  assert.deepEqual(JSON.parse(view.context.requests[0].init.body), { role: "admin", expires_in_days: 14, max_uses: 3 });
});

test("invite expiry uses calendar days for today, tomorrow, and seven days remaining", async (t) => {
  const NativeDate = Date;
  const now = new NativeDate(2026, 9, 13, 9, 0, 0);
  globalThis.Date = class extends NativeDate {
    constructor(...args) { super(...(args.length ? args : [now.getTime()])); }
    static now() { return now.getTime(); }
  };
  t.after(() => { globalThis.Date = NativeDate; });
  const view = await renderHarness(t, { respond: (path, init, context) => {
    if (path.endsWith("/members")) return { items: context.members };
    return [0, 1, 7].map((days, index) => ({ ...context.invites[0], id: index + 1,
      expires_at: new NativeDate(2026, 9, 13 + days, 23, 0, 0).toISOString() }));
  } });
  assert.match(view.text(), /\(오늘\)/);
  assert.match(view.text(), /\(내일\)/);
  assert.match(view.text(), /\(7일 남음\)/);
});


test("invite role choices are exactly admin and labeler with labeler selected by default", async (t) => {
  const view = await renderHarness(t, { component: "InviteLinkDialog" });
  const group = () => view.find((node) => node.props?.["aria-label"] === "초대 역할");
  const options = () => elements(group()).filter((node) => node.type === "button");
  assert.deepEqual(options().map((node) => [textOf(node), node.props["aria-pressed"]]), [
    ["관리자", false], ["라벨러", true],
  ]);
  assert.doesNotMatch(view.text(), /편집자|보기 전용/);
  view.button("관리자").props.onClick();
  assert.deepEqual(options().map((node) => node.props["aria-pressed"]), [true, false]);
  view.button("라벨러").props.onClick();
  view.find((node) => node.type === "form").props.onSubmit({ preventDefault() {} });
  await settle();
  assert.equal(JSON.parse(view.context.requests[0].init.body).role, "editor");
});

for (const actor of ["owner", "admin"]) {
  test(`${actor} member role selectors expose exactly admin and labeler`, async (t) => {
    const view = await renderHarness(t, { role: actor });
    const selectors = elements(view.render()).filter((node) => node.type === "select"
      && node.props?.["aria-label"]?.endsWith(" 역할"));
    assert.equal(selectors.length, 3, "each nonowner member remains manageable, including historical viewer");
    for (const selector of selectors) {
      assert.deepEqual(elements(selector).filter((node) => node.type === "option")
        .map((node) => [node.props.value, textOf(node)]), [["admin", "관리자"], ["editor", "라벨러"]]);
    }
    assert.doesNotMatch(view.text(), /편집자|보기 전용/);
  });
}

test("historical viewer role stays unselected while admin and labeler keep their displayed selection", async (t) => {
  const view = await renderHarness(t, { role: "admin" });
  for (const [role, initialIndex, expectedIndex] of [
    ["viewer", 0, -1], ["admin", 0, 0], ["editor", 1, 1],
  ]) {
    const selector = view.find((node) => node.type === "select"
      && node.props["aria-label"] === `${role} 역할`);
    assert.ok(selector, `${role} member must retain the role control`);
    assert.equal(selector.props.value, role);
    assert.deepEqual(elements(selector).filter((node) => node.type === "option")
      .map((node) => node.props.value), ["admin", "editor"]);
    const selectElement = { selectedIndex: initialIndex };
    selector.props.ref(selectElement);
    assert.equal(selectElement.selectedIndex, expectedIndex,
      "an unlisted historical role must not visually inherit the first admin option");
  }
  assert.equal(view.context.requests.filter(({ init }) => init.method === "PATCH").length, 0);
});
