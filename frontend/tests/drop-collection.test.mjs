import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import ts from "typescript";

async function loadDropCollection() {
  const groupingSource = await readFile(
    new URL("../src/utils/uploadGrouping.ts", import.meta.url),
    "utf8",
  );
  const groupingJavascript = ts.transpileModule(groupingSource, {
    compilerOptions: {
      module: ts.ModuleKind.ESNext,
      target: ts.ScriptTarget.ES2022,
    },
  }).outputText;
  const groupingUrl = `data:text/javascript;base64,${Buffer.from(groupingJavascript).toString("base64")}`;
  const source = await readFile(
    new URL("../src/utils/dropCollection.ts", import.meta.url),
    "utf8",
  );
  const javascript = ts.transpileModule(source, {
    compilerOptions: {
      module: ts.ModuleKind.ESNext,
      target: ts.ScriptTarget.ES2022,
    },
  }).outputText.replace(
    'from "./uploadGrouping"',
    `from "${groupingUrl}"`,
  );

  return import(`data:text/javascript;base64,${Buffer.from(javascript).toString("base64")}`);
}

function fakeFile(name, type) {
  return {
    name,
    type,
    size: 11,
    lastModified: 1,
  };
}

function legacyFile(name, fullPath, type) {
  return {
    isFile: true,
    isDirectory: false,
    name,
    fullPath,
    file(resolve) {
      resolve(fakeFile(name, type));
    },
  };
}

function legacyDirectory(name, fullPath, children) {
  return {
    isFile: false,
    isDirectory: true,
    name,
    fullPath,
    createReader() {
      let finished = false;
      return {
        readEntries(resolve) {
          if (finished) resolve([]);
          else {
            finished = true;
            resolve(children);
          }
        },
      };
    },
  };
}

function modernFile(name, type) {
  return {
    kind: "file",
    name,
    async getFile() {
      return fakeFile(name, type);
    },
  };
}

function modernDirectory(name, children) {
  return {
    kind: "directory",
    name,
    async *values() {
      yield* children;
    },
  };
}

test("folder drops work when the browser exposes the unprefixed getAsEntry API", async () => {
  const { collectDroppedSources } = await loadDropCollection();
  const images = legacyDirectory("images", "/images", [
    legacyDirectory("train", "/images/train", [
      legacyFile("a.jpg", "/images/train/a.jpg", "image/jpeg"),
    ]),
  ]);
  const labels = legacyDirectory("labels", "/labels", [
    legacyDirectory("train", "/labels/train", [
      legacyFile("a.txt", "/labels/train/a.txt", "text/plain"),
    ]),
  ]);

  const sources = await collectDroppedSources({
    items: [
      { getAsEntry: () => images, getAsFile: () => null },
      { getAsEntry: () => labels, getAsFile: () => null },
    ],
    files: [],
  });

  assert.deepEqual(sources.map((source) => source.name), ["images", "labels"]);
  assert.deepEqual(
    sources.flatMap((source) => source.files.map((file) => file.relPath)),
    ["images/train/a.jpg", "labels/train/a.txt"],
  );
});

test("a partial getAsEntry implementation falls back to webkitGetAsEntry", async () => {
  const { collectDroppedSources } = await loadDropCollection();
  const images = legacyDirectory("images", "/images", [
    legacyFile("a.jpg", "/images/a.jpg", "image/jpeg"),
  ]);

  const sources = await collectDroppedSources({
    items: [{
      getAsEntry: () => null,
      webkitGetAsEntry: () => images,
      getAsFile: () => null,
    }],
    files: [],
  });

  assert.equal(sources.length, 1);
  assert.equal(sources[0].name, "images");
  assert.equal(sources[0].files[0].relPath, "images/a.jpg");
});

test("folder drops work through File System Access handles", async () => {
  const { collectDroppedSources } = await loadDropCollection();
  const images = modernDirectory("images", [
    modernDirectory("val", [modernFile("b.png", "image/png")]),
  ]);
  const labels = modernDirectory("labels", [
    modernDirectory("val", [modernFile("b.txt", "text/plain")]),
  ]);

  const sources = await collectDroppedSources({
    items: [
      { getAsFileSystemHandle: () => Promise.resolve(images), getAsFile: () => null },
      { getAsFileSystemHandle: () => Promise.resolve(labels), getAsFile: () => null },
    ],
    files: [],
  });

  assert.deepEqual(sources.map((source) => source.name), ["images", "labels"]);
  assert.deepEqual(
    sources.flatMap((source) => source.files.map((file) => file.relPath)),
    ["images/val/b.png", "labels/val/b.txt"],
  );
});

test("plain files fall back to dataTransfer.files when items are unavailable", async () => {
  const { collectDroppedSources } = await loadDropCollection();

  const sources = await collectDroppedSources({
    items: [],
    files: [fakeFile("loose.jpg", "image/jpeg"), fakeFile("loose.txt", "text/plain")],
  });

  assert.equal(sources.length, 1);
  assert.deepEqual(
    sources[0].files.map((file) => [file.relPath, file.kind]),
    [["loose.jpg", "image"], ["loose.txt", "label"]],
  );
});

test("unsupported directory drops report an error instead of silently returning zero", async () => {
  const { collectDroppedSources } = await loadDropCollection();

  await assert.rejects(
    collectDroppedSources({
      items: [{ kind: "file", getAsFile: () => null }],
      files: [],
    }),
    /폴더를 읽을 수 없습니다/,
  );
});

test("folder collection reports tree search and file reads as separate 0-to-100 stages", async () => {
  const { collectDroppedSources } = await loadDropCollection();
  const progress = [];
  const images = legacyDirectory("images", "/images", [
    legacyFile("a.jpg", "/images/a.jpg", "image/jpeg"),
    legacyFile("b.jpg", "/images/b.jpg", "image/jpeg"),
  ]);

  await collectDroppedSources({
    items: [{ webkitGetAsEntry: () => images, getAsFile: () => null }],
    files: [],
  }, (update) => progress.push(update));

  assert.deepEqual(
    [progress[0].treePercentage, progress[0].filePercentage],
    [0, 0],
  );
  assert.match(progress[0].current, /트리/);
  assert.ok(progress.some((update) => (
    update.treePercentage > 0 && update.treePercentage < 100
  )));
  const treeComplete = progress.findIndex((update) => (
    update.treePercentage === 100 && update.filePercentage === 0
  ));
  assert.notEqual(treeComplete, -1);
  assert.deepEqual(
    progress
      .slice(treeComplete)
      .filter((update) => update.filesTotal === 2)
      .map((update) => update.filesProcessed),
    [0, 1, 2],
  );
  assert.deepEqual(
    [progress.at(-1).treePercentage, progress.at(-1).filePercentage],
    [100, 100],
  );
  assert.equal(progress.at(-1).current, "images/b.jpg");
});

test("large legacy directory enumeration advances above zero before the final batch", async () => {
  const { collectDroppedSources } = await loadDropCollection();
  const progress = [];
  let readCount = 0;
  let releaseFinalBatch;
  let markFinalBatchRequested;
  const finalBatchGate = new Promise((resolve) => {
    releaseFinalBatch = resolve;
  });
  const finalBatchRequested = new Promise((resolve) => {
    markFinalBatchRequested = resolve;
  });
  const images = {
    isFile: false,
    isDirectory: true,
    name: "images",
    fullPath: "/images",
    createReader() {
      return {
        readEntries(resolve) {
          readCount += 1;
          if (readCount === 1) {
            resolve(Array.from({ length: 100 }, (_, index) => (
              legacyFile(
                `${index}.jpg`,
                `/images/${index}.jpg`,
                "image/jpeg",
              )
            )));
            return;
          }
          markFinalBatchRequested();
          void finalBatchGate.then(() => resolve([]));
        },
      };
    },
  };

  const collection = collectDroppedSources({
    items: [{ webkitGetAsEntry: () => images, getAsFile: () => null }],
    files: [],
  }, (update) => progress.push(update));

  await finalBatchRequested;
  const percentageWhileEnumerationIsOpen = progress.at(-1).treePercentage;
  releaseFinalBatch();
  await collection;

  assert.ok(
    percentageWhileEnumerationIsOpen > 0,
    "tree progress must advance as each directory batch is discovered",
  );
  assert.equal(progress.at(-1).treePercentage, 100);
  assert.equal(progress.at(-1).filePercentage, 100);
});
