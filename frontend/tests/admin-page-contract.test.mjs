import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const adminPage = await readFile(
  new URL("../src/pages/AdminPage.tsx", import.meta.url),
  "utf8",
);
const adminAccess = await readFile(
  new URL("../src/utils/adminAccess.ts", import.meta.url),
  "utf8",
);
const appShell = await readFile(
  new URL("../src/components/AppShell.tsx", import.meta.url),
  "utf8",
);
const app = await readFile(new URL("../src/App.tsx", import.meta.url), "utf8");
const client = await readFile(
  new URL("../src/api/client.ts", import.meta.url),
  "utf8",
);

test("admin API client exposes overview and users", () => {
  assert.match(client, /export function getAdminOverview\(\)/);
  assert.match(client, /export function getAdminUsers\(\)/);
  assert.match(client, /\/api\/admin\/overview/);
  assert.match(client, /\/api\/admin\/users/);
});

test("the /admin route renders AdminPage", () => {
  assert.match(app, /path === "\/admin"/);
  assert.match(app, /<AdminPage \/>/);
});

test("a non-admin visit falls back to the not-found screen", () => {
  assert.match(adminPage, /phase === "denied"/);
  assert.match(adminPage, /페이지를 찾을 수 없습니다/);
  // The 404 from the server is what flips the page into denied state.
  assert.match(adminPage, /error\.status === 404/);
});

test("nothing renders while the server verdict is pending", () => {
  // Flashing the admin shell before the 404 would reveal the page to
  // non-admins; loading must render null, not AppShell.
  assert.match(adminPage, /phase === "loading"\) return null/);
});

test("logout clears the cached admin probe", () => {
  assert.match(appShell, /resetAdminProbe\(\)/);
});

test("admin access is never persisted client-side", () => {
  assert.doesNotMatch(adminAccess, /localStorage|sessionStorage/);
  assert.match(
    adminAccess,
    /server response is the only source of truth/i,
  );
});

test("the sidebar link is hidden until the server confirms the grant", () => {
  assert.match(appShell, /adminVisible && \(/);
  assert.match(appShell, /probeAdminOverview\(\)/);
  assert.match(appShell, /useState\(false\)/);
});

test("the single table shows every user with login methods", () => {
  assert.match(adminPage, /전체 사용자/);
  assert.doesNotMatch(adminPage, /최근 가입/);
  assert.match(adminPage, /로그인 방식/);
  assert.match(adminPage, /user\.login_methods/);
  assert.match(client, /login_methods:\s*string\[\]/);
  assert.match(adminPage, /formatBytes\(user\.bytes_used\)/);
  assert.match(adminPage, /from "\.\.\/utils\/formatBytes"/);
  assert.match(adminPage, /formatDate\(user\.created_at\)/);
});

test("formatBytes renders human units", async () => {
  const { default: ts } = await import("typescript");
  const source = await readFile(
    new URL("../src/utils/formatBytes.ts", import.meta.url),
    "utf8",
  );
  const javascript = ts.transpileModule(source, {
    compilerOptions: {
      module: ts.ModuleKind.ESNext,
      target: ts.ScriptTarget.ES2022,
    },
  }).outputText;
  const { formatBytes } = await import(
    `data:text/javascript;base64,${Buffer.from(javascript).toString("base64")}`
  );
  assert.equal(formatBytes(0), "0 B");
  assert.equal(formatBytes(512), "512 B");
  assert.equal(formatBytes(1536), "1.5 KB");
  assert.equal(formatBytes(1024 ** 3), "1.0 GB");
});
