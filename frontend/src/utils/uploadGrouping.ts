import type { CollectedFile } from "../api/upload";

export type UploadSourceKind = "files" | "folder" | "zip";
export type UploadSelectionKind = "files" | "folder";

export interface UploadSourceDraft {
  name: string;
  kind: UploadSourceKind;
  files: CollectedFile[];
}

export interface UploadSource {
  key: string;
  name: string;
  kind: UploadSourceKind;
  files: CollectedFile[];
}

export interface UploadUnit {
  key: string;
  baseName: string;
  name: string;
  batches: CollectedFile[][];
}

const MAX_DATASET_NAME_LENGTH = 255;
const MAX_UPLOAD_BATCH_FILES = 200_000;
export const MAX_DATASET_IMAGES = 5_000;
const SPLIT_NAMES = new Map([
  ["train", "train"],
  ["val", "val"],
  ["valid", "val"],
  ["validation", "val"],
  ["test", "test"],
]);

function isUploadableSource(source: Pick<UploadSource, "files">) {
  return source.files.some((file) => file.kind !== "other");
}

function normalizedName(value: string) {
  return value.trim().slice(0, MAX_DATASET_NAME_LENGTH) || "dataset";
}

export function balancedImagePartitionSizes(
  imageCount: number,
  maxImages = MAX_DATASET_IMAGES,
) {
  if (!Number.isInteger(imageCount) || imageCount < 0) {
    throw new RangeError("imageCount must be a nonnegative integer");
  }
  if (!Number.isInteger(maxImages) || maxImages <= 0) {
    throw new RangeError("maxImages must be a positive integer");
  }
  if (imageCount === 0) return [];

  const partCount = Math.ceil(imageCount / maxImages);
  const baseSize = Math.floor(imageCount / partCount);
  const largerPartCount = imageCount % partCount;
  return Array.from(
    { length: partCount },
    (_, index) => baseSize + (index < largerPartCount ? 1 : 0),
  );
}

export function datasetPartitionName(base: string, partIndex: number) {
  if (!Number.isInteger(partIndex) || partIndex < 1) {
    throw new RangeError("partIndex must be a positive integer");
  }
  const suffix = `_(${partIndex})`;
  const normalized = normalizedName(base);
  return `${normalized.slice(0, MAX_DATASET_NAME_LENGTH - suffix.length)}${suffix}`;
}

export function uploadPartitionPreview(base: string, imageCount: number) {
  if (!base.trim() || imageCount <= MAX_DATASET_IMAGES) return null;
  const sizes = balancedImagePartitionSizes(imageCount);
  return {
    imageCount,
    partCount: sizes.length,
    sizes,
    names: sizes.map((_size, index) => datasetPartitionName(base, index + 1)),
  };
}

export function droppedDirectoryName(
  name: string | undefined,
  fullPath: string,
) {
  const explicitName = name?.trim();
  if (explicitName) return normalizedName(explicitName);
  const fallbackName = fullPath.replaceAll("\\", "/").split("/").filter(Boolean).at(-1);
  return normalizedName(fallbackName ?? "");
}

function fileStem(path: string) {
  const basename = path.replaceAll("\\", "/").split("/").filter(Boolean).at(-1) ?? "";
  return normalizedName(basename.replace(/\.[^.]+$/, ""));
}

function uploadPairKey(file: CollectedFile, index: number) {
  if (file.kind !== "image" && file.kind !== "label") {
    return `single:${index}`;
  }
  const segments = file.relPath.replaceAll("\\", "/").split("/").filter(Boolean);
  const basename = segments.at(-1) ?? "";
  const stem = basename.replace(/\.[^.]+$/, "");
  const split = segments
    .map((segment) => SPLIT_NAMES.get(segment.toLowerCase()))
    .find((candidate) => candidate !== undefined) ?? "";
  return `pair:${split}\0${stem}`;
}

export function batchUploadFiles(
  files: readonly CollectedFile[],
  maxBatchFiles = MAX_UPLOAD_BATCH_FILES,
): CollectedFile[][] {
  if (!Number.isInteger(maxBatchFiles) || maxBatchFiles <= 0) {
    throw new RangeError("maxBatchFiles must be a positive integer");
  }
  const accepted = files.filter((file) => file.kind !== "other");
  if (accepted.length === 0) return [];
  if (accepted.length <= maxBatchFiles) return [accepted];

  const metadata = accepted.filter((file) => file.kind === "classfile");
  if (metadata.length >= maxBatchFiles) {
    throw new RangeError("class metadata leaves no room for upload files");
  }
  const groups = new Map<string, CollectedFile[]>();
  accepted.forEach((file, index) => {
    if (file.kind === "classfile") return;
    const key = uploadPairKey(file, index);
    const group = groups.get(key);
    if (group) group.push(file);
    else groups.set(key, [file]);
  });

  const payloadCapacity = maxBatchFiles - metadata.length;
  const batches: CollectedFile[][] = [];
  let payload: CollectedFile[] = [];
  const flush = () => {
    if (payload.length === 0) return;
    batches.push([...metadata, ...payload]);
    payload = [];
  };

  for (const group of groups.values()) {
    if (group.length > payloadCapacity) {
      flush();
      for (let offset = 0; offset < group.length; offset += payloadCapacity) {
        batches.push([
          ...metadata,
          ...group.slice(offset, offset + payloadCapacity),
        ]);
      }
      continue;
    }
    if (payload.length + group.length > payloadCapacity) flush();
    payload.push(...group);
  }
  flush();
  return batches;
}

export function datasetNameWithSuffix(base: string, index: number) {
  const suffix = ` (${index})`;
  const normalized = normalizedName(base);
  return `${normalized.slice(0, MAX_DATASET_NAME_LENGTH - suffix.length)}${suffix}`;
}

export function suggestedDatasetName(sources: readonly UploadSource[]) {
  const uploadableSources = sources.filter(isUploadableSource);
  if (uploadableSources.length !== 1) return "";
  return uploadableSources[0].name.trim().slice(0, MAX_DATASET_NAME_LENGTH);
}

export function datasetNameAfterSourceChange(
  currentName: string,
  sources: readonly UploadSource[],
  userEdited: boolean,
) {
  return userEdited ? currentName : suggestedDatasetName(sources);
}

function complementaryFolderIndexes(
  sources: readonly UploadSourceDraft[],
  name: "images" | "labels",
  fileKind: CollectedFile["kind"],
) {
  return sources.flatMap((source, index) => (
    source.kind === "folder"
    && source.name.trim().toLowerCase() === name
    && source.files.some((file) => file.kind === fileKind)
      ? [index]
      : []
  ));
}

function isDatasetMetadataSource(source: UploadSourceDraft) {
  return source.kind === "files"
    && source.files.some((file) => file.kind === "classfile")
    && source.files.every((file) => (
      file.kind === "classfile" || file.kind === "other"
    ));
}

export function coalesceDroppedSources(
  sources: readonly UploadSourceDraft[],
): UploadSourceDraft[] {
  const imageIndexes = complementaryFolderIndexes(sources, "images", "image");
  const labelIndexes = complementaryFolderIndexes(sources, "labels", "label");
  if (imageIndexes.length !== 1 || labelIndexes.length !== 1) return [...sources];

  const joinedIndexes = new Set([
    imageIndexes[0],
    labelIndexes[0],
    ...sources.flatMap((source, index) => (
      isDatasetMetadataSource(source) ? [index] : []
    )),
  ]);
  const insertionIndex = Math.min(...joinedIndexes);
  const joinedFiles = sources.flatMap((source, index) => (
    joinedIndexes.has(index) ? source.files : []
  ));
  const result: UploadSourceDraft[] = [];

  sources.forEach((source, index) => {
    if (index === insertionIndex) {
      result.push({ name: "dataset", kind: "folder", files: joinedFiles });
    }
    if (!joinedIndexes.has(index)) result.push(source);
  });
  return result;
}

export function groupInputFiles(
  files: readonly CollectedFile[],
  selectionKind: UploadSelectionKind,
): UploadSourceDraft[] {
  if (selectionKind === "folder") {
    const groups = new Map<string, UploadSourceDraft>();
    files.forEach((file) => {
      const path = file.relPath.replaceAll("\\", "/").replace(/^\/+/, "");
      const segments = path.split("/").filter(Boolean);
      const root = segments.length > 1 ? segments[0] : "";
      const key = root || "__selected_folder__";
      const current = groups.get(key);
      if (current) {
        current.files.push(file);
      } else {
        groups.set(key, {
          name: normalizedName(root || fileStem(path)),
          kind: "folder",
          files: [file],
        });
      }
    });
    return [...groups.values()];
  }

  const sources: UploadSourceDraft[] = [];
  let looseSource: UploadSourceDraft | null = null;
  for (const file of files) {
    if (file.kind === "zip") {
      sources.push({ name: fileStem(file.relPath), kind: "zip", files: [file] });
      continue;
    }
    if (looseSource === null) {
      looseSource = {
        name: fileStem(file.relPath),
        kind: "files",
        files: [],
      };
      sources.push(looseSource);
    }
    looseSource.files.push(file);
  }
  if (looseSource !== null) {
    const namingFile = looseSource.files.find((file) => file.kind !== "other")
      ?? looseSource.files[0];
    if (namingFile) looseSource.name = fileStem(namingFile.relPath);
  }
  return sources;
}

function availableName(base: string, reserved: Set<string>) {
  const normalized = normalizedName(base);
  if (!reserved.has(normalized)) {
    reserved.add(normalized);
    return normalized;
  }

  let index = 2;
  while (true) {
    const candidate = datasetNameWithSuffix(normalized, index);
    if (!reserved.has(candidate)) {
      reserved.add(candidate);
      return candidate;
    }
    index += 1;
  }
}

export function createUploadPlan(
  sources: readonly UploadSource[],
  finalName: string,
  existingNames: readonly string[],
): UploadUnit[] {
  const uploadableSources = sources.filter(isUploadableSource);
  if (uploadableSources.length === 0) return [];
  if (uploadableSources.length === 1) {
    const [source] = uploadableSources;
    return [{
      key: source.key,
      baseName: normalizedName(finalName),
      name: normalizedName(finalName),
      batches: batchUploadFiles(source.files),
    }];
  }

  // The final name is reserved for the merged dataset. Source datasets use
  // distinct names so the owner-wide unique-name constraint cannot collide.
  const reserved = new Set([...existingNames, normalizedName(finalName)]);
  return uploadableSources.map((source) => ({
    key: source.key,
    baseName: normalizedName(source.name),
    name: availableName(source.name, reserved),
    batches: batchUploadFiles(source.files),
  }));
}
