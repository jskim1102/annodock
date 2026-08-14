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

function isUploadableSource(source: Pick<UploadSource, "files">) {
  return source.files.some((file) => file.kind !== "other");
}

function normalizedName(value: string) {
  return value.trim().slice(0, MAX_DATASET_NAME_LENGTH) || "dataset";
}

function fileStem(path: string) {
  const basename = path.replaceAll("\\", "/").split("/").filter(Boolean).at(-1) ?? "";
  return normalizedName(basename.replace(/\.[^.]+$/, ""));
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
      batches: [source.files],
    }];
  }

  // The final name is reserved for the merged dataset. Source datasets use
  // distinct names so the owner-wide unique-name constraint cannot collide.
  const reserved = new Set([...existingNames, normalizedName(finalName)]);
  return uploadableSources.map((source) => ({
    key: source.key,
    baseName: normalizedName(source.name),
    name: availableName(source.name, reserved),
    batches: [source.files],
  }));
}
