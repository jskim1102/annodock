import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const client = await readFile(
  new URL("../src/api/client.ts", import.meta.url),
  "utf8",
);
const shell = await readFile(
  new URL("../src/components/AppShell.tsx", import.meta.url),
  "utf8",
);
const projects = await readFile(
  new URL("../src/pages/ProjectsPage.tsx", import.meta.url),
  "utf8",
);

test("storage contracts expose referenced and physical byte counts separately", () => {
  assert.match(client, /physical_storage_bytes:\s*number/);
  assert.match(client, /referenced_bytes:\s*number/);
});

test("dataset rows label referenced capacity and actual occupancy", () => {
  assert.match(projects, /참조/);
  assert.match(projects, /실제 점유/);
  assert.match(projects, /formatBytes\(dataset\.storage_bytes\)/);
  assert.match(projects, /formatBytes\(dataset\.physical_storage_bytes\)/);
  assert.match(projects, /formatBytes\(source\.storage_bytes\)/);
  assert.match(projects, /formatBytes\(source\.physical_storage_bytes\)/);
});

test("the account meter shows only physical usage against its quota", () => {
  const meterStart = shell.indexOf("export function StorageMeter");
  const meterEnd = shell.indexOf("export function AppShell", meterStart);
  const meter = shell.slice(meterStart, meterEnd);

  assert.match(meter, /formatBytes\(quota\.used_bytes\)/);
  assert.match(meter, /formatBytes\(quota\.limit_bytes\)/);
  assert.match(
    meter,
    /`\$\{formatBytes\(quota\.used_bytes\)\} \/ \$\{formatBytes\(quota\.limit_bytes\)\}`/,
  );
  assert.doesNotMatch(meter, /referencedLabel|quota\.referenced_bytes|참조/);
});
