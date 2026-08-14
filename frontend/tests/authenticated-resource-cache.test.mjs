import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import ts from "typescript";

async function loadResourceCache() {
  const source = await readFile(
    new URL("../src/utils/authenticatedResourceCache.ts", import.meta.url),
    "utf8",
  );
  const javascript = ts.transpileModule(source, {
    compilerOptions: {
      module: ts.ModuleKind.ESNext,
      target: ts.ScriptTarget.ES2022,
    },
  }).outputText;
  return import(`data:text/javascript;base64,${Buffer.from(javascript).toString("base64")}#${Math.random()}`);
}

function stubObjectUrls() {
  const created = [];
  const revoked = [];
  const originalCreate = URL.createObjectURL;
  const originalRevoke = URL.revokeObjectURL;
  URL.createObjectURL = () => {
    const url = `blob:test-${created.length}`;
    created.push(url);
    return url;
  };
  URL.revokeObjectURL = (url) => revoked.push(url);
  return {
    created,
    revoked,
    restore() {
      URL.createObjectURL = originalCreate;
      URL.revokeObjectURL = originalRevoke;
    },
  };
}

test("authenticated resource cache coalesces concurrent fetches and reuses one blob URL", async () => {
  const urls = stubObjectUrls();
  try {
    const cache = await loadResourceCache();
    let calls = 0;
    const loader = async () => {
      calls += 1;
      await Promise.resolve();
      return new Blob(["image"]);
    };

    const [first, second] = await Promise.all([
      cache.acquireAuthenticatedResource("/api/images/1/file", loader),
      cache.acquireAuthenticatedResource("/api/images/1/file", loader),
    ]);

    assert.equal(calls, 1);
    assert.equal(first.url, second.url);
    assert.equal(
      cache.peekAuthenticatedResource("/api/images/1/file"),
      first.url,
    );
    first.release();
    second.release();
  } finally {
    urls.restore();
  }
});

test("LRU keeps at most 40 unretained resources and revokes evicted URLs", async () => {
  const urls = stubObjectUrls();
  try {
    const cache = await loadResourceCache();
    const loader = async (path) => new Blob([path]);

    for (let index = 0; index <= cache.AUTHENTICATED_RESOURCE_CACHE_LIMIT; index += 1) {
      await cache.prefetchAuthenticatedResource(`/api/images/${index}/file`, loader);
    }

    assert.equal(cache.AUTHENTICATED_RESOURCE_CACHE_LIMIT, 40);
    assert.equal(cache.peekAuthenticatedResource("/api/images/0/file"), null);
    assert.notEqual(cache.peekAuthenticatedResource("/api/images/1/file"), null);
    assert.deepEqual(urls.revoked, ["blob:test-0"]);
  } finally {
    urls.restore();
  }
});

test("retained resources survive pressure and clear aborts flights and revokes every URL", async () => {
  const urls = stubObjectUrls();
  try {
    const cache = await loadResourceCache();
    const loader = async (path) => new Blob([path]);
    const current = await cache.acquireAuthenticatedResource("/api/images/current/file", loader);

    for (let index = 0; index < cache.AUTHENTICATED_RESOURCE_CACHE_LIMIT; index += 1) {
      await cache.prefetchAuthenticatedResource(`/api/images/${index}/thumb`, loader);
    }
    assert.equal(
      cache.peekAuthenticatedResource("/api/images/current/file"),
      current.url,
    );

    let aborted = false;
    const slow = cache.prefetchAuthenticatedResource(
      "/api/images/slow/file",
      (_path, signal) => new Promise((_resolve, reject) => {
        signal.addEventListener("abort", () => {
          aborted = true;
          reject(new DOMException("aborted", "AbortError"));
        }, { once: true });
      }),
    );
    cache.clearAuthenticatedResourceCache();
    await assert.rejects(slow, (error) => error instanceof DOMException && error.name === "AbortError");

    assert.equal(aborted, true);
    assert.equal(cache.peekAuthenticatedResource("/api/images/current/file"), null);
    assert.ok(urls.revoked.includes(current.url));
    current.release();
  } finally {
    urls.restore();
  }
});
