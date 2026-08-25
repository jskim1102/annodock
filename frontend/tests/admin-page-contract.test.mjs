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
const quotaLimit = await readFile(
  new URL("../src/utils/quotaLimit.ts", import.meta.url),
  "utf8",
);
const appStyles = await readFile(
  new URL("../src/styles/app.css", import.meta.url),
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
  assert.match(client, /limit_bytes:\s*number/);
  assert.match(adminPage, /formatBytes\(user\.bytes_used\)/);
  assert.match(adminPage, /할당 용량/);
  assert.match(adminPage, /formatBytes\(user\.limit_bytes\)/);
  assert.match(adminPage, /from "\.\.\/utils\/formatBytes"/);
  assert.match(adminPage, /formatDate\(user\.created_at\)/);
});

test("admin quota updates use a dedicated PATCH endpoint", () => {
  assert.match(client, /export function updateAdminUserQuota\(/);
  assert.match(client, /\/api\/admin\/users\/\$\{userId\}\/quota/);
  assert.match(client, /jsonInit\("PATCH", \{ limit_bytes: limitBytes \}\)/);
});

test("each user quota can be changed from an accessible dialog", () => {
  assert.match(adminPage, /aria-label=\{`\$\{displayName\(user\)\} 할당 용량 변경`\}/);
  assert.match(adminPage, /className="btn btn-primary btn-sm"/);
  assert.match(
    appStyles,
    /\.admin-user-quota\s*\{[^}]*justify-content:\s*space-between;[^}]*width:\s*100%;/s,
  );
  assert.match(adminPage, /할당 용량 변경/);
  assert.match(adminPage, /quotaBytesFromGiB/);
  assert.match(adminPage, /await updateAdminUserQuota/);
  assert.match(adminPage, /현재 사용량/);
  assert.match(adminPage, /기존 데이터는 유지/);
});

test("quota GiB conversion rejects invalid and unsafe values", async () => {
  const { default: ts } = await import("typescript");
  const javascript = ts.transpileModule(quotaLimit, {
    compilerOptions: {
      module: ts.ModuleKind.ESNext,
      target: ts.ScriptTarget.ES2022,
    },
  }).outputText;
  const { quotaBytesFromGiB, quotaGiBFromBytes } = await import(
    `data:text/javascript;base64,${Buffer.from(javascript).toString("base64")}`
  );
  assert.equal(quotaBytesFromGiB("5"), 5 * 1024 ** 3);
  assert.equal(quotaBytesFromGiB("0.5"), 512 * 1024 ** 2);
  assert.equal(quotaBytesFromGiB("0"), null);
  assert.equal(quotaBytesFromGiB("0.05"), null);
  assert.equal(quotaBytesFromGiB("invalid"), null);
  assert.equal(quotaBytesFromGiB("9007199254740991"), null);
  assert.equal(quotaGiBFromBytes(5 * 1024 ** 3), "5");
  assert.equal(quotaGiBFromBytes(1.5 * 1024 ** 3), "1.5");
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

test("admin API client exposes user deletion with confirm phase", () => {
  assert.match(client, /export function deleteAdminUser\(/);
  assert.match(client, /\/api\/admin\/users\/\$\{userId\}/);
  assert.match(client, /confirm=true/);
});

test("user deletion is selected by row checkboxes and started from the card header", () => {
  assert.match(adminPage, /selectedUserIds/);
  assert.match(adminPage, /aria-label="전체 사용자 선택"/);
  assert.match(adminPage, /aria-checked=\{someUsersSelected \? "mixed" : allUsersSelected\}/);
  assert.match(adminPage, /aria-label=\{`\$\{displayName\(user\)\} 선택`\}/);
  assert.match(adminPage, /className="admin-users-header"/);
  assert.match(adminPage, /aria-label="선택한 사용자 삭제"/);
  assert.doesNotMatch(adminPage, /aria-label=\{`\$\{displayName\(user\)\} 사용자 삭제`\}/);
});

test("deletion is two-phase: server confirmation then confirmed call", () => {
  assert.match(adminPage, /admin-user-delete-confirmation-required/);
  assert.match(adminPage, /requires_confirmation/);
  assert.match(adminPage, /이 작업은 되돌릴 수 없습니다/);
});

test("the confirm dialog summarizes every selected target and its data footprint", () => {
  assert.match(adminPage, /삭제 대상 사용자/);
  assert.match(adminPage, /targets\.map/);
  assert.match(adminPage, /project_count/);
  assert.match(adminPage, /dataset_count/);
  assert.match(adminPage, /formatBytes\(summary\.bytesUsed\)/);
});

test("bulk deletion refreshes canonical data and preserves failed selections", () => {
  assert.match(adminPage, /const deleteSelectedUsers = async/);
  assert.match(adminPage, /for \(const target of targets\)/);
  assert.match(adminPage, /await deleteAdminUser\(target\.user\.id, true\)/);
  assert.match(adminPage, /completedIds\.add\(target\.user\.id\)/);
  assert.match(adminPage, /await refresh\(\)/);
  assert.match(adminPage, /for \(const id of completedIds\) next\.delete\(id\)/);
});
