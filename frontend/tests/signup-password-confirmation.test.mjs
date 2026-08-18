import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const sourcePath = new URL("../src/pages/AuthPages.tsx", import.meta.url);
const source = await readFile(sourcePath, "utf8");
const signupPageStart = source.indexOf("export function SignupPage()");
const signupPageEnd = source.indexOf("export function PasswordResetPage()", signupPageStart);
const signupPage = source.slice(signupPageStart, signupPageEnd);

test("signup renders an accessible password confirmation field", () => {
  assert.match(signupPage, /<label htmlFor="signup-password-confirm">비밀번호 확인<\/label>/);
  assert.match(
    signupPage,
    /id="signup-password-confirm"[\s\S]*?name="confirmation"[\s\S]*?type="password"[\s\S]*?autoComplete="new-password"[\s\S]*?minLength=\{8\}[\s\S]*?required/,
  );
});

test("signup rejects mismatched passwords before starting or calling the API", () => {
  assert.match(
    signupPage,
    /const confirmation = formValue\(event\.currentTarget, "confirmation"\);/,
  );
  assert.match(
    signupPage,
    /if \(password !== confirmation\) \{\s*setError\("비밀번호가 서로 일치하지 않습니다\."\);\s*return;\s*\}\s*setSubmitting\(true\);/,
  );
  assert.match(signupPage, /await signup\(username, email, password\);/);
  assert.doesNotMatch(signupPage, /signup\(username, email, password, confirmation\)/);
});
