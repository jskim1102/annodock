import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const sourcePath = new URL("../src/components/AppShell.tsx", import.meta.url);
const source = await readFile(sourcePath, "utf8");

test("the account trigger toggles a menu instead of logging out", () => {
  const classIndex = source.indexOf('className="user-chip"');
  const triggerStart = source.lastIndexOf("<button", classIndex);
  const triggerEnd = source.indexOf("</button>", classIndex);
  const trigger = source.slice(triggerStart, triggerEnd + "</button>".length);

  assert.ok(classIndex >= 0 && triggerStart >= 0 && triggerEnd >= 0, "the account trigger must exist");
  assert.match(trigger, /aria-haspopup="menu"/);
  assert.match(trigger, /aria-expanded=\{accountMenuOpen\}/);
  assert.match(trigger, /setAccountMenuOpen/);
  assert.doesNotMatch(trigger, /handleLogout/);
});

test("logout is exposed only as an explicit menu item", () => {
  assert.match(source, /role="menu"/);
  const logoutItem = source.match(
    /<button[\s\S]*?role="menuitem"[\s\S]*?<\/button>/,
  )?.[0];

  assert.ok(logoutItem, "the logout menu item must exist");
  assert.match(logoutItem, /handleLogout/);
  assert.match(logoutItem, /로그아웃/);
});
