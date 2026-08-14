import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import ts from "typescript";

const sourcePath = new URL("../src/utils/accountPresentation.ts", import.meta.url);

async function loadAccountPresentation() {
  const source = await readFile(sourcePath, "utf8");
  const javascript = ts.transpileModule(source, {
    compilerOptions: {
      module: ts.ModuleKind.ESNext,
      target: ts.ScriptTarget.ES2022,
    },
  }).outputText;
  const moduleUrl = `data:text/javascript;base64,${Buffer.from(javascript).toString("base64")}`;

  return import(moduleUrl);
}

test("OAuth accounts use one provider-neutral ID and avatar rule", async () => {
  const { getAccountPresentation } = await loadAccountPresentation();

  assert.deepEqual(
    getAccountPresentation({
      id: 1,
      email: "naver_member@example.com",
      username: " naver_member ",
      identities: ["local", "naver"],
    }),
    { label: "naver_member", initials: "NM" },
  );
  assert.deepEqual(
    getAccountPresentation({
      id: 2,
      email: null,
      username: null,
      identities: ["kakao"],
    }),
    { label: "user-2", initials: "U2" },
  );
  assert.deepEqual(
    getAccountPresentation({
      id: 3,
      email: "sample.user@example.com",
      username: null,
      identities: ["google"],
    }),
    { label: "sample.user", initials: "SU" },
  );
  assert.deepEqual(
    getAccountPresentation({
      id: 4,
      email: "fallback@example.com",
      username: " login@example.com ",
      identities: ["local"],
    }),
    { label: "login", initials: "LO" },
  );
  assert.deepEqual(getAccountPresentation(null), {
    label: "annodock",
    initials: "AD",
  });
});
