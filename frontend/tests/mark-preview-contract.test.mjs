import assert from "node:assert/strict";
import { access } from "node:fs/promises";
import test from "node:test";

test("the mark comparison preview is preserved outside Vite public assets", async () => {
  await assert.rejects(access(new URL("../public/_mark-variants.html", import.meta.url)));
  await access(new URL("../design-previews/_mark-variants.html", import.meta.url));
});
