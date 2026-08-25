import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import ts from "typescript";

const source = await readFile(
  new URL("../src/utils/storage.ts", import.meta.url),
  "utf8",
);
const javascript = ts.transpileModule(source, {
  compilerOptions: {
    module: ts.ModuleKind.ESNext,
    target: ts.ScriptTarget.ES2022,
  },
}).outputText;

class MemoryStorage {
  constructor(entries = [], failSetKeys = []) {
    this.values = new Map(entries);
    this.failSetKeys = new Set(failSetKeys);
  }

  get length() {
    return this.values.size;
  }

  key(index) {
    return [...this.values.keys()][index] ?? null;
  }

  getItem(key) {
    return this.values.get(key) ?? null;
  }

  setItem(key, value) {
    if (this.failSetKeys.has(key)) throw new Error("quota exceeded");
    this.values.set(key, String(value));
  }

  removeItem(key) {
    this.values.delete(key);
  }
}

async function runStorageModule(storage) {
  const previousWindow = globalThis.window;
  globalThis.window = { localStorage: storage };
  try {
    await import(
      `data:text/javascript;base64,${Buffer.from(javascript).toString("base64")}#${Math.random()}`
    );
  } finally {
    if (previousWindow === undefined) delete globalThis.window;
    else globalThis.window = previousWindow;
  }
}

test("legacy auth, theme, and upload resume values migrate once to annodock keys", async () => {
  const storage = new MemoryStorage([
    ["deeplabel:auth:session", '{"accessToken":"token"}'],
    ["deeplabel:theme", "dark"],
    ["deeplabel:upload:resume", '{"uploadId":"resume"}'],
    ["unrelated", "keep"],
  ]);

  await runStorageModule(storage);

  assert.equal(storage.getItem("annodock:auth:session"), '{"accessToken":"token"}');
  assert.equal(storage.getItem("annodock:theme"), "dark");
  assert.equal(storage.getItem("annodock:upload:resume"), '{"uploadId":"resume"}');
  assert.equal(storage.getItem("deeplabel:auth:session"), null);
  assert.equal(storage.getItem("deeplabel:theme"), null);
  assert.equal(storage.getItem("deeplabel:upload:resume"), null);
  assert.equal(storage.getItem("unrelated"), "keep");
});

test("an existing annodock value wins over its legacy counterpart", async () => {
  const storage = new MemoryStorage([
    ["deeplabel:theme", "dark"],
    ["annodock:theme", "light"],
  ]);

  await runStorageModule(storage);

  assert.equal(storage.getItem("annodock:theme"), "light");
  assert.equal(storage.getItem("deeplabel:theme"), null);
});

test("a failed migration write preserves the legacy value for a later retry", async () => {
  const storage = new MemoryStorage(
    [["deeplabel:auth:session", '{"accessToken":"token"}']],
    ["annodock:auth:session"],
  );

  await runStorageModule(storage);

  assert.equal(storage.getItem("annodock:auth:session"), null);
  assert.equal(storage.getItem("deeplabel:auth:session"), '{"accessToken":"token"}');
});
