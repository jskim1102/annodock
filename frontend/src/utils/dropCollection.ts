import type { CollectedFile } from "../api/upload";
import {
  droppedDirectoryName,
  groupInputFiles,
  type UploadSourceDraft,
} from "./uploadGrouping";

const IMAGE_EXTENSIONS = new Set([
  "avif", "bmp", "dng", "heic", "heif", "jp2", "jpeg", "jpeg2000",
  "jpg", "mpo", "png", "tif", "tiff", "webp",
]);

// Directory APIs do not expose the total number of descendants up front. Use
// a bounded discovery estimate so a large directory visibly advances while
// the browser is still returning entry batches; exact weighted completion
// takes over as soon as those entries are traversed.
const TREE_DISCOVERY_HINT_SCALE = 5_000;
const TREE_DISCOVERY_HINT_MAX = 95;
const HANDLE_DISCOVERY_BATCH_SIZE = 128;

interface LegacyEntry {
  isFile: boolean;
  isDirectory: boolean;
  name?: string;
  fullPath: string;
  file?: (
    resolve: (file: File) => void,
    reject?: (error: DOMException) => void,
  ) => void;
  createReader?: () => {
    readEntries: (
      resolve: (entries: LegacyEntry[]) => void,
      reject?: (error: DOMException) => void,
    ) => void;
  };
}

interface BrowserFileHandle {
  kind: "file";
  name: string;
  getFile: () => Promise<File>;
}

interface BrowserDirectoryHandle {
  kind: "directory";
  name: string;
  values?: () => AsyncIterable<BrowserFileSystemHandle>;
  entries?: () => AsyncIterable<[string, BrowserFileSystemHandle]>;
}

type BrowserFileSystemHandle = BrowserFileHandle | BrowserDirectoryHandle;

interface DropItem {
  kind?: string;
  getAsFile?: () => File | null;
  getAsEntry?: () => LegacyEntry | null;
  webkitGetAsEntry?: () => LegacyEntry | null;
  getAsFileSystemHandle?: () => Promise<BrowserFileSystemHandle | null>;
}

interface DropData {
  items: ArrayLike<DropItem>;
  files: ArrayLike<File>;
}

interface PendingFile {
  relPath: string;
  read: () => Promise<File>;
}

interface PreparedDropItem {
  sourceName: string | null;
  files: PendingFile[];
  unsupportedDirectory: boolean;
}

export interface DropCollectionProgress {
  treePercentage: number;
  filePercentage: number;
  filesProcessed: number;
  filesTotal: number;
  current: string;
}

type DropCollectionProgressCallback = (progress: DropCollectionProgress) => void;

interface CollectionProgressController {
  describeTree: (current: string) => void;
  discoverTreeEntries: (count: number, current: string) => Promise<void>;
  completeTreeWeight: (weight: number, current: string) => Promise<void>;
  completeTree: (filesTotal: number) => void;
  completeFile: (current: string) => Promise<void>;
}

function yieldForProgressPaint(): Promise<void> {
  return new Promise((resolve) => {
    if (typeof requestAnimationFrame === "function") {
      requestAnimationFrame(() => resolve());
      return;
    }
    setTimeout(resolve, 0);
  });
}

function createProgressController(
  onProgress?: DropCollectionProgressCallback,
): CollectionProgressController {
  let snapshot: DropCollectionProgress = {
    treePercentage: 0,
    filePercentage: 0,
    filesProcessed: 0,
    filesTotal: 0,
    current: "폴더 트리 검색 시작",
  };
  let completedTreeWeight = 0;
  let discoveredTreeEntries = 0;
  let lastTreePaint = 0;
  let lastFilePaint = 0;

  const emit = (next: Partial<DropCollectionProgress>) => {
    snapshot = { ...snapshot, ...next };
    onProgress?.({ ...snapshot });
  };

  onProgress?.({ ...snapshot });

  return {
    describeTree(current) {
      emit({ current });
    },
    async discoverTreeEntries(count, current) {
      if (count <= 0) return;
      discoveredTreeEntries += count;
      const discoveryHint = Math.max(1, Math.floor(
        TREE_DISCOVERY_HINT_MAX
          * discoveredTreeEntries
          / (discoveredTreeEntries + TREE_DISCOVERY_HINT_SCALE),
      ));
      const treePercentage = Math.min(99, Math.max(
        snapshot.treePercentage,
        Math.floor(completedTreeWeight),
        discoveryHint,
      ));
      emit({ treePercentage, current });
      if (
        treePercentage > 0
        && (lastTreePaint === 0 || treePercentage >= lastTreePaint + 5)
      ) {
        lastTreePaint = treePercentage;
        await yieldForProgressPaint();
      }
    },
    async completeTreeWeight(weight, current) {
      completedTreeWeight = Math.min(100, completedTreeWeight + weight);
      const treePercentage = Math.min(99, Math.floor(completedTreeWeight));
      if (treePercentage > snapshot.treePercentage) {
        emit({ treePercentage, current });
      }
      if (treePercentage >= lastTreePaint + 5) {
        lastTreePaint = treePercentage;
        await yieldForProgressPaint();
      }
    },
    completeTree(filesTotal) {
      emit({
        treePercentage: 100,
        filePercentage: 0,
        filesProcessed: 0,
        filesTotal,
        current: `${filesTotal.toLocaleString()}개 파일 읽기 시작`,
      });
    },
    async completeFile(current) {
      const filesProcessed = snapshot.filesProcessed + 1;
      const filePercentage = snapshot.filesTotal > 0
        ? Math.min(100, Math.floor(filesProcessed / snapshot.filesTotal * 100))
        : 100;
      if (filePercentage > snapshot.filePercentage || filesProcessed === snapshot.filesTotal) {
        emit({ filesProcessed, filePercentage, current });
      } else {
        snapshot = { ...snapshot, filesProcessed, current };
      }
      if (filePercentage >= lastFilePaint + 5) {
        lastFilePaint = filePercentage;
        await yieldForProgressPaint();
      }
    },
  };
}

function classify(file: File): CollectedFile["kind"] {
  const lower = file.name.toLowerCase();
  const extension = lower.includes(".") ? lower.slice(lower.lastIndexOf(".") + 1) : "";
  if (IMAGE_EXTENSIONS.has(extension) || file.type.startsWith("image/")) return "image";
  if (lower === "classes.txt" || extension === "yaml" || extension === "yml") return "classfile";
  if (extension === "txt") return "label";
  if (extension === "zip") return "zip";
  return "other";
}

export function toCollectedFile(file: File, relPath = file.name): CollectedFile {
  return {
    file,
    relPath: relPath.replace(/^\/+/, "") || file.name,
    kind: classify(file),
  };
}

function fileFromLegacyEntry(entry: LegacyEntry): Promise<File> {
  return new Promise((resolve, reject) => {
    if (!entry.file) {
      reject(new Error(`파일을 읽을 수 없습니다: ${entry.fullPath}`));
      return;
    }
    entry.file(
      resolve,
      () => reject(new Error(`파일을 읽을 수 없습니다: ${entry.fullPath}`)),
    );
  });
}

async function listLegacyFiles(
  entry: LegacyEntry,
  treeWeight: number,
  progress: CollectionProgressController,
): Promise<LegacyEntry[]> {
  if (entry.isFile) {
    await progress.completeTreeWeight(treeWeight, entry.fullPath);
    return [entry];
  }
  if (!entry.isDirectory || !entry.createReader) {
    await progress.completeTreeWeight(treeWeight, entry.fullPath);
    return [];
  }

  progress.describeTree(`${entry.fullPath} 트리 검색 중`);
  const reader = entry.createReader();
  const entries: LegacyEntry[] = [];
  while (true) {
    const batch = await new Promise<LegacyEntry[]>((resolve, reject) => {
      reader.readEntries(
        resolve,
        () => reject(new Error(`폴더를 읽을 수 없습니다: ${entry.fullPath}`)),
      );
    });
    if (batch.length === 0) break;
    entries.push(...batch);
    await progress.discoverTreeEntries(
      batch.length,
      `${entry.fullPath} · ${entries.length.toLocaleString()}개 항목 발견`,
    );
  }
  if (entries.length === 0) {
    await progress.completeTreeWeight(treeWeight, entry.fullPath);
    return [];
  }

  const files: LegacyEntry[] = [];
  const childWeight = treeWeight / entries.length;
  for (const child of entries) {
    files.push(...await listLegacyFiles(child, childWeight, progress));
  }
  return files;
}

async function directoryHandleChildren(
  handle: BrowserDirectoryHandle,
  progress: CollectionProgressController,
  relPath: string,
): Promise<BrowserFileSystemHandle[]> {
  if (handle.values) {
    const children: BrowserFileSystemHandle[] = [];
    let reported = 0;
    for await (const child of handle.values()) {
      children.push(child);
      if (children.length - reported >= HANDLE_DISCOVERY_BATCH_SIZE) {
        await progress.discoverTreeEntries(
          children.length - reported,
          `${relPath} · ${children.length.toLocaleString()}개 항목 발견`,
        );
        reported = children.length;
      }
    }
    if (children.length > reported) {
      await progress.discoverTreeEntries(
        children.length - reported,
        `${relPath} · ${children.length.toLocaleString()}개 항목 발견`,
      );
    }
    return children;
  }
  if (handle.entries) {
    const children: BrowserFileSystemHandle[] = [];
    let reported = 0;
    for await (const [, child] of handle.entries()) {
      children.push(child);
      if (children.length - reported >= HANDLE_DISCOVERY_BATCH_SIZE) {
        await progress.discoverTreeEntries(
          children.length - reported,
          `${relPath} · ${children.length.toLocaleString()}개 항목 발견`,
        );
        reported = children.length;
      }
    }
    if (children.length > reported) {
      await progress.discoverTreeEntries(
        children.length - reported,
        `${relPath} · ${children.length.toLocaleString()}개 항목 발견`,
      );
    }
    return children;
  }
  throw new Error(`폴더를 읽을 수 없습니다: ${handle.name}`);
}

interface PendingHandleFile {
  handle: BrowserFileHandle;
  relPath: string;
}

async function listHandleFiles(
  handle: BrowserFileSystemHandle,
  treeWeight: number,
  progress: CollectionProgressController,
  relPath = handle.name,
): Promise<PendingHandleFile[]> {
  if (handle.kind === "file") {
    await progress.completeTreeWeight(treeWeight, relPath);
    return [{ handle, relPath }];
  }
  progress.describeTree(`${relPath} 트리 검색 중`);
  const children = await directoryHandleChildren(handle, progress, relPath);
  if (children.length === 0) {
    await progress.completeTreeWeight(treeWeight, relPath);
    return [];
  }

  const files: PendingHandleFile[] = [];
  const childWeight = treeWeight / children.length;
  for (const child of children) {
    files.push(...await listHandleFiles(
      child,
      childWeight,
      progress,
      `${relPath}/${child.name}`,
    ));
  }
  return files;
}

async function prepareItem(
  item: DropItem,
  treeWeight: number,
  progress: CollectionProgressController,
): Promise<PreparedDropItem> {
  const entryReaders = [item.getAsEntry, item.webkitGetAsEntry].filter(
    (reader, index, readers): reader is NonNullable<typeof reader> => (
      reader !== undefined && readers.indexOf(reader) === index
    ),
  );
  for (const getEntry of entryReaders) {
    let entry: LegacyEntry | null = null;
    try {
      entry = getEntry.call(item);
    } catch {
      continue;
    }
    if (entry?.isDirectory) {
      const entries = await listLegacyFiles(entry, treeWeight, progress);
      return {
        sourceName: droppedDirectoryName(entry.name, entry.fullPath),
        files: entries.map((fileEntry) => ({
          relPath: fileEntry.fullPath,
          read: () => fileFromLegacyEntry(fileEntry),
        })),
        unsupportedDirectory: false,
      };
    }
    if (entry?.isFile) {
      await progress.completeTreeWeight(treeWeight, entry.fullPath);
      return {
        sourceName: null,
        files: [{
          relPath: entry.fullPath,
          read: () => fileFromLegacyEntry(entry),
        }],
        unsupportedDirectory: false,
      };
    }
  }

  if (item.getAsFileSystemHandle) {
    // Invoke this while the drop event is still active. Some browsers reject
    // delayed calls after the data-transfer store leaves read mode.
    let handle: BrowserFileSystemHandle | null = null;
    try {
      handle = await item.getAsFileSystemHandle.call(item);
    } catch {
      // A browser may expose the experimental method outside a context where
      // it is usable. Continue to the plain File fallback in that case.
    }
    if (handle?.kind === "directory") {
      const handles = await listHandleFiles(handle, treeWeight, progress);
      return {
        sourceName: droppedDirectoryName(handle.name, handle.name),
        files: handles.map((pending) => ({
          relPath: pending.relPath,
          read: () => pending.handle.getFile(),
        })),
        unsupportedDirectory: false,
      };
    }
    if (handle?.kind === "file") {
      await progress.completeTreeWeight(treeWeight, handle.name);
      return {
        sourceName: null,
        files: [{ relPath: handle.name, read: () => handle.getFile() }],
        unsupportedDirectory: false,
      };
    }
  }

  const file = item.getAsFile?.call(item) ?? null;
  if (file) {
    await progress.completeTreeWeight(treeWeight, file.name);
    return {
      sourceName: null,
      files: [{ relPath: file.name, read: async () => file }],
      unsupportedDirectory: false,
    };
  }
  return {
    sourceName: null,
    files: [],
    unsupportedDirectory: item.kind !== "string",
  };
}

export async function collectDroppedSources(
  dataTransfer: DropData,
  onProgress?: DropCollectionProgressCallback,
): Promise<UploadSourceDraft[]> {
  // Snapshot both lists synchronously. Browsers protect the drag data store
  // again once the drop callback returns.
  const items = Array.from(dataTransfer.items);
  const fallbackFiles = Array.from(dataTransfer.files);
  const progress = createProgressController(onProgress);
  const rootWeight = items.length > 0 ? 100 / items.length : 0;
  let groups = await Promise.all(items.map((item) => (
    prepareItem(item, rootWeight, progress)
  )));
  const unsupportedDirectory = groups.some((group) => group.unsupportedDirectory);

  if (unsupportedDirectory) {
    throw new Error(
      "이 브라우저에서 드롭한 폴더를 읽을 수 없습니다. 폴더 선택 버튼을 사용해 주세요.",
    );
  }

  if (groups.every((group) => group.files.length === 0) && fallbackFiles.length > 0) {
    const fallbackWeight = 100 / fallbackFiles.length;
    for (const file of fallbackFiles) {
      await progress.completeTreeWeight(fallbackWeight, file.name);
    }
    groups = [{
      sourceName: null,
      files: fallbackFiles.map((file) => ({
        relPath: file.name,
        read: async () => file,
      })),
      unsupportedDirectory: false,
    }];
  }

  const total = groups.reduce((count, group) => count + group.files.length, 0);
  if (total === 0) {
    throw new Error("드롭한 파일이나 폴더를 읽을 수 없습니다.");
  }
  progress.completeTree(total);

  const collectedGroups: Array<{ sourceName: string | null; files: CollectedFile[] }> = groups.map((group) => ({
    sourceName: group.sourceName,
    files: new Array<CollectedFile>(group.files.length),
  }));
  const jobs = groups.flatMap((group, groupIndex) => (
    group.files.map((pending, fileIndex) => ({ pending, groupIndex, fileIndex }))
  ));
  let nextJobIndex = 0;
  const workerCount = Math.min(16, jobs.length);
  await Promise.all(Array.from({ length: workerCount }, async () => {
    while (nextJobIndex < jobs.length) {
      const job = jobs[nextJobIndex];
      nextJobIndex += 1;
      const file = toCollectedFile(await job.pending.read(), job.pending.relPath);
      collectedGroups[job.groupIndex].files[job.fileIndex] = file;
      await progress.completeFile(file.relPath);
    }
  }));

  const folderSources = collectedGroups.flatMap((group) => (
    group.sourceName === null
      ? []
      : [{ name: group.sourceName, kind: "folder" as const, files: group.files }]
  ));
  const looseFiles = collectedGroups.flatMap((group) => (
    group.sourceName === null ? group.files : []
  ));

  return [...folderSources, ...groupInputFiles(looseFiles, "files")];
}
