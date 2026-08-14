import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const source = await readFile(
  new URL("../src/components/AppShell.tsx", import.meta.url),
  "utf8",
);

test("storage meters never present invented usage numbers", () => {
  assert.doesNotMatch(source, /12\.4|100 GiB/);
  assert.equal(
    source.match(/>—</g)?.length,
    2,
    "compact and full meters must both state that usage is unavailable",
  );
});

