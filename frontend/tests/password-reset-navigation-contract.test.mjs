import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const sourcePath = new URL("../src/pages/AuthPages.tsx", import.meta.url);
const source = await readFile(sourcePath, "utf8");
const resetPageStart = source.indexOf("export function PasswordResetPage()");
const resetPageEnd = source.indexOf("export function OAuthCallbackPage()", resetPageStart);
const resetPage = source.slice(resetPageStart, resetPageEnd);

test("a successful password reset returns to the login page", () => {
  assert.match(
    resetPage,
    /await resetPassword\(token, password\);\s*navigate\("\/login"\);/,
  );
});

test("the password reset page does not expose a manual login return link", () => {
  assert.doesNotMatch(resetPage, /로그인으로 돌아가기/);
});
