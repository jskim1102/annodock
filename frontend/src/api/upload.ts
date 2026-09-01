import {
  apiFetch,
  type DatasetRow,
  type Job,
  requestJson,
  responseOrThrow,
} from "./client";
import {
  readStoredJson,
  removeStoredValue,
  writeStoredJson,
} from "../utils/storage";

export interface CollectedFile {
  relPath: string;
  file: File;
  kind: "image" | "label" | "classfile" | "zip" | "other";
}

interface UploadSession {
  upload_id: number;
  chunk_size: number;
  received: number[];
  size: number;
  state: "open" | "complete" | "aborted";
}

interface UploadBatchResponse {
  batch_id: string;
  state: "open" | "sealed";
  job_id: number | null;
}

interface ResumeRecord {
  uploadId: number;
  chunkSize: number;
  jobId?: number;
  size?: number;
  lastModified?: number;
}

interface DatasetResumeRecord {
  uploads: Record<string, ResumeRecord>;
}

interface UploadManifestResumeRecord {
  batchId: string;
  fingerprint: string;
  fileCount: number;
  totalSize: number;
  expectedExtractedSize: number;
  largestFileSize: number;
}

interface UploadTargetResumeRecord {
  datasetId: number;
  projectId: number;
  datasetName: string;
  fingerprint: string;
}

export interface PreparedUploadOperation {
  datasetId: number;
  batchId: string;
  knownJobId: number | null;
  resumeKey: string;
}

export interface PreparedUpload {
  item: CollectedFile;
  resumeKey: string;
  session: UploadSession;
  jobId?: number;
}

export interface TransferredUploadBatch {
  openUploads: PreparedUpload[];
  knownJobId: number | null;
}

export interface PreparedUploadBatch {
  datasetId: number;
  uploads: PreparedUpload[];
  totalBytes: number;
  resumeKey: string;
  resumeRecord: DatasetResumeRecord;
  operation?: PreparedUploadOperation;
}

export interface UploadProgress {
  uploadedBytes: number;
  totalBytes: number;
  uploadedImages: number;
  totalImages: number;
  currentPath: string;
}

export interface UploadPreparationProgress {
  preparedFiles: number;
  totalFiles: number;
}

export class UploadError extends Error {
  constructor(
    public readonly status: number,
    message: string,
    public readonly requiredBytes?: number,
    public readonly availableBytes?: number,
  ) {
    super(message);
    this.name = "UploadError";
  }
}

const DEFAULT_CHUNK_SIZE = 4 * 1024 * 1024;
const MAX_CHUNK_ATTEMPTS = 3;
const MAX_SESSION_BATCH_SIZE = 1_000;
const MAX_CHUNKS_PER_TRANSFER_BATCH = 128;
const MAX_TRANSFER_BATCH_BYTES = 7 * 1024 * 1024;
const MAX_CONCURRENT_TRANSFERS = 8;
const MAX_CONCURRENT_RESUME_CHECKS = 8;
const manifestMemory = new Map<number, UploadManifestResumeRecord>();
const activeOperations = new Map<number, PreparedUploadOperation>();

function delay(milliseconds: number) {
  return new Promise<void>((resolve) => window.setTimeout(resolve, milliseconds));
}

function fingerprint(datasetId: number, item: CollectedFile): string {
  const value = `${datasetId}:${item.relPath}:${item.file.size}:${item.file.lastModified}`;
  let hash = 2166136261;
  for (let index = 0; index < value.length; index += 1) {
    hash ^= value.charCodeAt(index);
    hash = Math.imul(hash, 16777619);
  }
  return `upload:${(hash >>> 0).toString(16)}`;
}

function datasetResumeKey(datasetId: number): string {
  return `upload:${datasetId}:resume`;
}

function uniqueManifestFiles(
  collected: readonly CollectedFile[],
): CollectedFile[] {
  const unique = new Map<string, CollectedFile>();
  collected.forEach((item) => {
    if (item.kind === "other") return;
    const existing = unique.get(item.relPath);
    if (!existing) {
      unique.set(item.relPath, item);
      return;
    }
    if (
      existing.kind !== item.kind
      || existing.file.size !== item.file.size
      || existing.file.lastModified !== item.file.lastModified
    ) {
      throw new UploadError(
        409,
        `${item.relPath}: 같은 경로에 서로 다른 파일이 선택되었습니다.`,
      );
    }
  });
  return [...unique.values()];
}

function selectionFingerprint(files: readonly CollectedFile[]): string {
  let first = 2166136261;
  let second = 2246822519;
  const ordered = [...files].sort((left, right) => (
    left.relPath.localeCompare(right.relPath)
  ));
  ordered.forEach((item) => {
    const descriptor = `${item.relPath}\0${item.kind}\0${item.file.size}\0${item.file.lastModified}\n`;
    for (let index = 0; index < descriptor.length; index += 1) {
      const code = descriptor.charCodeAt(index);
      first = Math.imul(first ^ code, 16777619);
      second = Math.imul(second ^ code, 3266489917);
    }
  });
  return `${(first >>> 0).toString(16).padStart(8, "0")}${(second >>> 0).toString(16).padStart(8, "0")}`;
}

function uploadTargetResumeKey(
  projectId: number,
  collected: readonly CollectedFile[],
) {
  const fingerprint = selectionFingerprint(uniqueManifestFiles(collected));
  return {
    fingerprint,
    storageKey: `upload-target:${projectId}:${fingerprint}`,
  };
}

function parseUploadTargetResume(value: unknown): UploadTargetResumeRecord | null {
  if (!value || typeof value !== "object") return null;
  const record = value as Partial<UploadTargetResumeRecord>;
  if (
    !Number.isInteger(record.datasetId)
    || !Number.isInteger(record.projectId)
    || typeof record.datasetName !== "string"
    || typeof record.fingerprint !== "string"
  ) return null;
  return record as UploadTargetResumeRecord;
}

export function resumeUploadDatasetTarget(
  projectId: number,
  datasetName: string,
  collected: readonly CollectedFile[],
): number | null {
  const { fingerprint, storageKey } = uploadTargetResumeKey(projectId, collected);
  const record = parseUploadTargetResume(readStoredJson(storageKey));
  if (
    record === null
    || record.projectId !== projectId
    || record.datasetName !== datasetName.trim()
    || record.fingerprint !== fingerprint
  ) return null;
  return record.datasetId;
}

export function rememberUploadDatasetTarget(
  projectId: number,
  datasetName: string,
  datasetId: number,
  collected: readonly CollectedFile[],
): void {
  const { fingerprint, storageKey } = uploadTargetResumeKey(projectId, collected);
  writeStoredJson(storageKey, {
    datasetId,
    projectId,
    datasetName: datasetName.trim(),
    fingerprint,
  } satisfies UploadTargetResumeRecord);
}

export function clearUploadDatasetTarget(
  projectId: number,
  datasetName: string,
  datasetId: number,
  collected: readonly CollectedFile[],
): void {
  const { storageKey } = uploadTargetResumeKey(projectId, collected);
  const record = parseUploadTargetResume(readStoredJson(storageKey));
  if (
    record?.datasetId === datasetId
    && record.datasetName === datasetName.trim()
  ) removeStoredValue(storageKey);
}

function parseManifestResume(value: unknown): UploadManifestResumeRecord | null {
  if (!value || typeof value !== "object") return null;
  const record = value as Partial<UploadManifestResumeRecord>;
  if (
    typeof record.batchId !== "string"
    || typeof record.fingerprint !== "string"
    || typeof record.fileCount !== "number"
    || typeof record.totalSize !== "number"
    || typeof record.expectedExtractedSize !== "number"
    || typeof record.largestFileSize !== "number"
  ) return null;
  return record as UploadManifestResumeRecord;
}

function sameManifestSelection(
  left: UploadManifestResumeRecord,
  right: Omit<UploadManifestResumeRecord, "batchId">,
) {
  return left.fingerprint === right.fingerprint
    && left.fileCount === right.fileCount
    && left.totalSize === right.totalSize
    && left.expectedExtractedSize === right.expectedExtractedSize
    && left.largestFileSize === right.largestFileSize;
}

function parseResume(parsed: unknown): ResumeRecord | null {
  if (!parsed || typeof parsed !== "object") return null;
  const record = parsed as Partial<ResumeRecord>;
  if (typeof record.uploadId !== "number") return null;
  return {
    uploadId: record.uploadId,
    chunkSize: typeof record.chunkSize === "number"
      ? record.chunkSize
      : DEFAULT_CHUNK_SIZE,
    jobId: typeof record.jobId === "number" ? record.jobId : undefined,
    size: typeof record.size === "number" ? record.size : undefined,
    lastModified: typeof record.lastModified === "number"
      ? record.lastModified
      : undefined,
  };
}

function readLegacyResume(key: string): ResumeRecord | null {
  return parseResume(readStoredJson(key));
}

function readDatasetResume(key: string): DatasetResumeRecord {
  const parsed = readStoredJson(key);
  const stored = parsed && typeof parsed === "object"
    ? (parsed as { uploads?: unknown }).uploads
    : null;
  const uploads: Record<string, ResumeRecord> = Object.create(null);
  if (!stored || typeof stored !== "object" || Array.isArray(stored)) {
    return { uploads };
  }
  Object.entries(stored).forEach(([path, value]) => {
    const resume = parseResume(value);
    if (resume) uploads[path] = resume;
  });
  return { uploads };
}

function resumeMatchesItem(
  resume: ResumeRecord | undefined,
  item: CollectedFile,
): resume is ResumeRecord {
  return Boolean(
    resume
    && resume.size === item.file.size
    && resume.lastModified === item.file.lastModified,
  );
}

function resumeForItem(
  item: CollectedFile,
  uploadId: number,
  chunkSize: number,
  jobId?: number,
): ResumeRecord {
  return {
    uploadId,
    chunkSize,
    jobId,
    size: item.file.size,
    lastModified: item.file.lastModified,
  };
}

async function runConcurrent<T>(
  values: readonly T[],
  limit: number,
  callback: (value: T, index: number) => Promise<void>,
): Promise<void> {
  let nextIndex = 0;
  let failed = false;
  let failure: unknown;
  const worker = async () => {
    while (!failed) {
      const index = nextIndex;
      nextIndex += 1;
      if (index >= values.length) return;
      try {
        await callback(values[index], index);
      } catch (error) {
        failed = true;
        failure = error;
      }
    }
  };
  await Promise.all(
    Array.from(
      { length: Math.min(limit, values.length) },
      () => worker(),
    ),
  );
  if (failed) throw failure;
}

async function runConcurrentIterable<T>(
  values: Iterable<T>,
  limit: number,
  callback: (value: T, index: number) => Promise<void>,
): Promise<void> {
  const iterator = values[Symbol.iterator]();
  let nextIndex = 0;
  let failed = false;
  let failure: unknown;
  const worker = async () => {
    while (!failed) {
      const next = iterator.next();
      if (next.done) return;
      const index = nextIndex;
      nextIndex += 1;
      try {
        await callback(next.value, index);
      } catch (error) {
        failed = true;
        failure = error;
      }
    }
  };
  await Promise.all(
    Array.from({ length: limit }, () => worker()),
  );
  if (failed) throw failure;
}

function expectedExtractedSize(item: CollectedFile) {
  return item.kind === "zip" ? item.file.size * 4 : item.file.size;
}

export async function beginUploadBatch(
  datasetId: number,
  collected: readonly CollectedFile[],
): Promise<PreparedUploadOperation> {
  const accepted = uniqueManifestFiles(collected);
  if (accepted.length === 0) {
    throw new UploadError(422, "업로드할 파일이 없습니다.");
  }
  const resumeKey = datasetResumeKey(datasetId);
  const selection = {
    fingerprint: selectionFingerprint(accepted),
    fileCount: accepted.length,
    totalSize: accepted.reduce((sum, item) => sum + item.file.size, 0),
    expectedExtractedSize: accepted.reduce(
      (sum, item) => sum + expectedExtractedSize(item),
      0,
    ),
    largestFileSize: accepted.reduce(
      (largest, item) => Math.max(largest, item.file.size),
      0,
    ),
  };
  const inMemory = manifestMemory.get(datasetId);
  const stored = inMemory ?? parseManifestResume(readStoredJson(resumeKey));
  const resume = stored && sameManifestSelection(stored, selection)
    ? stored
    : {
        batchId: globalThis.crypto.randomUUID(),
        ...selection,
      };
  // Persist before the request. If the server commits and the response is
  // lost, the next attempt replays the same manifest id instead of creating
  // an unrelated logical upload.
  manifestMemory.set(datasetId, resume);
  writeStoredJson(resumeKey, resume);
  const response = await requestJson<UploadBatchResponse>(
    `/api/datasets/${datasetId}/upload-batches/${resume.batchId}`,
    {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        total_size: selection.totalSize,
        largest_file_size: selection.largestFileSize,
        file_count: selection.fileCount,
        expected_extracted_size: selection.expectedExtractedSize,
      }),
    },
  );
  if (response.batch_id !== resume.batchId) {
    throw new UploadError(502, "업로드 배치 식별자가 서버 응답과 다릅니다.");
  }
  const operation = {
    datasetId,
    batchId: response.batch_id,
    knownJobId: response.job_id,
    resumeKey,
  };
  activeOperations.set(datasetId, operation);
  return operation;
}

async function preflight(datasetId: number, files: readonly CollectedFile[]) {
  const totalSize = files.reduce((sum, item) => sum + item.file.size, 0);
  const largestFileSize = files.reduce(
    (largest, item) => Math.max(largest, item.file.size),
    0,
  );
  await requestJson<void>(
    `/api/datasets/${datasetId}/upload-batches/preflight`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        total_size: totalSize,
        largest_file_size: largestFileSize,
        file_count: files.length,
        expected_extracted_size: files.reduce(
          (sum, item) => sum + expectedExtractedSize(item),
          0,
        ),
      }),
    },
  );
}

interface UploadCreated {
  upload_id: number;
  chunk_size: number;
  received: number[];
  size?: number;
  state?: "open" | "complete" | "aborted";
}

async function createUploadBatch(
  datasetId: number,
  items: readonly CollectedFile[],
  batchId?: string,
) {
  return requestJson<{ uploads: UploadCreated[] }>(
    `/api/datasets/${datasetId}/uploads/batch`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        ...(batchId ? { batch_id: batchId } : {}),
        files: items.map((item) => ({
          filename: item.relPath,
          size: item.file.size,
          chunk_size: DEFAULT_CHUNK_SIZE,
          kind: item.kind === "zip" ? "zip" : "file",
          file_count: 1,
          expected_extracted_size: expectedExtractedSize(item),
        })),
      }),
    },
  );
}

async function putChunk(uploadId: number, index: number, chunk: Blob) {
  let lastError: unknown;
  for (let attempt = 1; attempt <= MAX_CHUNK_ATTEMPTS; attempt += 1) {
    try {
      await responseOrThrow(await apiFetch(
        `/api/uploads/${uploadId}/chunks/${index}`,
        { method: "PUT", body: chunk },
      ));
      return;
    } catch (error) {
      lastError = error;
      const status = error && typeof error === "object" && "status" in error
        ? Number(error.status)
        : 0;
      if (attempt === MAX_CHUNK_ATTEMPTS || (status > 0 && status < 500)) {
        throw error;
      }
      await delay(250 * 2 ** (attempt - 1));
    }
  }
  throw lastError;
}

interface PendingChunk {
  upload: PreparedUpload;
  chunkNumber: number;
  start: number;
  end: number;
  size: number;
}

function* pendingChunkBatches(
  uploads: readonly PreparedUpload[],
): Generator<PendingChunk[]> {
  let chunks: PendingChunk[] = [];
  let payloadBytes = 0;
  for (const upload of uploads) {
    const received = new Set(upload.session.received);
    const chunkCount = Math.ceil(
      upload.item.file.size / upload.session.chunk_size,
    );
    for (let chunkNumber = 0; chunkNumber < chunkCount; chunkNumber += 1) {
      if (received.has(chunkNumber)) continue;
      const start = chunkNumber * upload.session.chunk_size;
      const end = Math.min(
        upload.item.file.size,
        start + upload.session.chunk_size,
      );
      const size = end - start;
      if (
        chunks.length > 0
        && (
          chunks.length >= MAX_CHUNKS_PER_TRANSFER_BATCH
          || payloadBytes + size > MAX_TRANSFER_BATCH_BYTES
        )
      ) {
        yield chunks;
        chunks = [];
        payloadBytes = 0;
      }
      chunks.push({ upload, chunkNumber, start, end, size });
      payloadBytes += size;
      if (
        chunks.length >= MAX_CHUNKS_PER_TRANSFER_BATCH
        || payloadBytes >= MAX_TRANSFER_BATCH_BYTES
      ) {
        yield chunks;
        chunks = [];
        payloadBytes = 0;
      }
    }
  }
  if (chunks.length > 0) yield chunks;
}

async function putChunkBatch(
  datasetId: number,
  chunks: readonly PendingChunk[],
) {
  // Older resumable sessions can have a chunk size above the new envelope.
  // Keep their established single-chunk transport instead of invalidating the
  // durable resume checkpoint.
  if (chunks.length === 1 && chunks[0].size > MAX_TRANSFER_BATCH_BYTES) {
    const chunk = chunks[0];
    await putChunk(
      chunk.upload.session.upload_id,
      chunk.chunkNumber,
      chunk.upload.item.file.slice(chunk.start, chunk.end),
    );
    return;
  }

  let lastError: unknown;
  for (let attempt = 1; attempt <= MAX_CHUNK_ATTEMPTS; attempt += 1) {
    const body = new FormData();
    body.append("metadata", JSON.stringify({
      chunks: chunks.map((chunk) => ({
        upload_id: chunk.upload.session.upload_id,
        chunk_number: chunk.chunkNumber,
        size: chunk.size,
      })),
    }));
    chunks.forEach((chunk) => {
      body.append(
        "chunks",
        chunk.upload.item.file.slice(chunk.start, chunk.end),
        `${chunk.upload.session.upload_id}-${chunk.chunkNumber}.part`,
      );
    });
    try {
      await responseOrThrow(await apiFetch(
        `/api/datasets/${datasetId}/uploads/chunks/batch`,
        { method: "POST", body },
      ));
      return;
    } catch (error) {
      lastError = error;
      const status = error && typeof error === "object" && "status" in error
        ? Number(error.status)
        : 0;
      if (attempt === MAX_CHUNK_ATTEMPTS || (status > 0 && status < 500)) {
        throw error;
      }
      await delay(250 * 2 ** (attempt - 1));
    }
  }
  throw lastError;
}

export function createDatasetForUpload(
  name: string,
  projectId: number,
): Promise<DatasetRow> {
  return requestJson("/api/datasets", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, project_id: projectId, upload_draft: true }),
  });
}

async function prepareManifestUploadBatch(
  datasetId: number,
  collected: readonly CollectedFile[],
  operation: PreparedUploadOperation,
  onPreparationProgress?: (progress: UploadPreparationProgress) => void,
): Promise<PreparedUploadBatch> {
  if (operation.datasetId !== datasetId) {
    throw new UploadError(409, "업로드 배치의 데이터셋이 일치하지 않습니다.");
  }
  const accepted = collected.filter((item) => item.kind !== "other");
  if (accepted.length === 0) throw new UploadError(422, "업로드할 파일이 없습니다.");
  onPreparationProgress?.({ preparedFiles: 0, totalFiles: accepted.length });
  const uploads: PreparedUpload[] = [];
  for (let offset = 0; offset < accepted.length; offset += MAX_SESSION_BATCH_SIZE) {
    const items = accepted.slice(offset, offset + MAX_SESSION_BATCH_SIZE);
    const created = await createUploadBatch(datasetId, items, operation.batchId);
    if (created.uploads.length !== items.length) {
      throw new UploadError(502, "업로드 준비 응답의 파일 수가 올바르지 않습니다.");
    }
    created.uploads.forEach((upload, index) => {
      const item = items[index];
      if (upload.size !== undefined && upload.size !== item.file.size) {
        throw new UploadError(409, `${item.relPath}: 업로드 파일 크기가 변경되었습니다.`);
      }
      uploads.push({
        item,
        resumeKey: operation.resumeKey,
        session: {
          upload_id: upload.upload_id,
          chunk_size: upload.chunk_size,
          received: upload.received,
          size: upload.size ?? item.file.size,
          state: upload.state ?? "open",
        },
      });
    });
    onPreparationProgress?.({
      preparedFiles: Math.min(offset + items.length, accepted.length),
      totalFiles: accepted.length,
    });
  }
  return {
    datasetId,
    uploads,
    totalBytes: accepted.reduce((sum, item) => sum + item.file.size, 0),
    resumeKey: operation.resumeKey,
    resumeRecord: { uploads: Object.create(null) },
    operation,
  };
}

async function prepareLegacyUploadBatch(
  datasetId: number,
  collected: readonly CollectedFile[],
  onPreparationProgress?: (progress: UploadPreparationProgress) => void,
): Promise<PreparedUploadBatch> {
  const accepted = collected.filter((item) => item.kind !== "other");
  if (accepted.length === 0) throw new UploadError(422, "업로드할 파일이 없습니다.");
  onPreparationProgress?.({ preparedFiles: 0, totalFiles: accepted.length });
  await preflight(datasetId, accepted);
  const resumeKey = datasetResumeKey(datasetId);
  const resumeRecord = readDatasetResume(resumeKey);
  const prepared: Array<PreparedUpload | undefined> = new Array(accepted.length);
  let preparedFiles = 0;
  let resumeRecordDirty = false;
  const legacyKeysReadyToRemove = new Set<string>();
  const candidates = accepted.map((item, index) => {
    const legacyKey = fingerprint(datasetId, item);
    const storedResume = resumeRecord.uploads[item.relPath];
    let resume: ResumeRecord | null = null;
    let fromLegacy = false;
    if (resumeMatchesItem(storedResume, item)) {
      resume = storedResume;
    } else {
      if (storedResume) {
        delete resumeRecord.uploads[item.relPath];
        resumeRecordDirty = true;
      }
      resume = readLegacyResume(legacyKey);
      fromLegacy = resume !== null;
    }
    return { index, item, legacyKey, resume, fromLegacy };
  });
  const reportPrepared = (count = 1) => {
    preparedFiles += count;
    onPreparationProgress?.({
      preparedFiles,
      totalFiles: accepted.length,
    });
  };
  const persistResumeRecord = () => {
    if (!writeStoredJson(resumeKey, resumeRecord)) return;
    legacyKeysReadyToRemove.forEach((key) => removeStoredValue(key));
    legacyKeysReadyToRemove.clear();
    resumeRecordDirty = false;
  };

  try {
    await runConcurrent(
      candidates.filter((candidate) => candidate.resume !== null),
      MAX_CONCURRENT_RESUME_CHECKS,
      async (candidate) => {
        const resume = candidate.resume;
        if (!resume) return;
        let session: UploadSession | null = null;
        try {
          session = await requestJson<UploadSession>(
            `/api/uploads/${resume.uploadId}`,
          );
          if (session.size !== candidate.item.file.size) session = null;
        } catch (error) {
          const status = error && typeof error === "object" && "status" in error
            ? Number(error.status)
            : 0;
          if (status !== 404) throw error;
        }
        if (session?.state === "aborted") {
          delete resumeRecord.uploads[candidate.item.relPath];
          legacyKeysReadyToRemove.add(candidate.legacyKey);
          resumeRecordDirty = true;
          throw new UploadError(
            409,
            `${candidate.item.relPath}: 중단된 업로드입니다.`,
          );
        }
        if (!session) {
          delete resumeRecord.uploads[candidate.item.relPath];
          resumeRecordDirty = true;
          return;
        }
        if (
          candidate.fromLegacy
          || resume.chunkSize !== session.chunk_size
        ) resumeRecordDirty = true;
        resumeRecord.uploads[candidate.item.relPath] = resumeForItem(
          candidate.item,
          session.upload_id,
          session.chunk_size,
          resume.jobId,
        );
        legacyKeysReadyToRemove.add(candidate.legacyKey);
        prepared[candidate.index] = {
          item: candidate.item,
          resumeKey,
          session,
          jobId: resume.jobId,
        };
        reportPrepared();
      },
    );
  } catch (error) {
    if (resumeRecordDirty || legacyKeysReadyToRemove.size > 0) {
      persistResumeRecord();
    }
    throw error;
  }
  if (resumeRecordDirty || legacyKeysReadyToRemove.size > 0) {
    persistResumeRecord();
  }

  const missing = candidates.filter((candidate) => !prepared[candidate.index]);
  for (let offset = 0; offset < missing.length; offset += MAX_SESSION_BATCH_SIZE) {
    const sessionBatch = missing.slice(offset, offset + MAX_SESSION_BATCH_SIZE);
    const created = await createUploadBatch(
      datasetId,
      sessionBatch.map((candidate) => candidate.item),
    );
    if (created.uploads.length !== sessionBatch.length) {
      throw new UploadError(502, "업로드 준비 응답의 파일 수가 올바르지 않습니다.");
    }
    created.uploads.forEach((upload, index) => {
      const candidate = sessionBatch[index];
      const session: UploadSession = {
        ...upload,
        size: candidate.item.file.size,
        state: "open",
      };
      resumeRecord.uploads[candidate.item.relPath] = resumeForItem(
        candidate.item,
        upload.upload_id,
        upload.chunk_size,
      );
      legacyKeysReadyToRemove.add(candidate.legacyKey);
      prepared[candidate.index] = {
        item: candidate.item,
        resumeKey,
        session,
      };
    });
    resumeRecordDirty = true;
    persistResumeRecord();
    reportPrepared(sessionBatch.length);
  }

  const uploads = prepared.map((upload) => {
    if (!upload) {
      throw new UploadError(502, "일부 파일의 업로드 세션을 준비하지 못했습니다.");
    }
    return upload;
  });

  return {
    datasetId,
    uploads,
    totalBytes: accepted.reduce((sum, item) => sum + item.file.size, 0),
    resumeKey,
    resumeRecord,
  };
}

export function prepareUploadBatch(
  datasetId: number,
  collected: readonly CollectedFile[],
  onPreparationProgress?: (progress: UploadPreparationProgress) => void,
): Promise<PreparedUploadBatch>;
export function prepareUploadBatch(
  datasetId: number,
  collected: readonly CollectedFile[],
  operation: PreparedUploadOperation,
  onPreparationProgress?: (progress: UploadPreparationProgress) => void,
): Promise<PreparedUploadBatch>;
export function prepareUploadBatch(
  datasetId: number,
  collected: readonly CollectedFile[],
  operationOrProgress?: PreparedUploadOperation | (
    (progress: UploadPreparationProgress) => void
  ),
  onPreparationProgress?: (progress: UploadPreparationProgress) => void,
): Promise<PreparedUploadBatch> {
  const explicitOperation = typeof operationOrProgress === "object"
    ? operationOrProgress
    : undefined;
  const progressCallback = typeof operationOrProgress === "function"
    ? operationOrProgress
    : onPreparationProgress;
  const operation = explicitOperation ?? activeOperations.get(datasetId);
  if (operation) {
    return prepareManifestUploadBatch(
      datasetId,
      collected,
      operation,
      progressCallback,
    );
  }
  return prepareLegacyUploadBatch(
    datasetId,
    collected,
    progressCallback,
  );
}

export async function transferUploadBatch(
  batch: PreparedUploadBatch,
  onProgress: (progress: UploadProgress) => void,
): Promise<TransferredUploadBatch> {
  let uploadedBytes = 0;
  let uploadedImages = 0;
  const totalImages = batch.uploads.reduce(
    (count, upload) => count + (upload.item.kind === "image" ? 1 : 0),
    0,
  );
  const openUploads: PreparedUpload[] = [];
  const remainingChunks = new Map<number, number>();
  const reportProgress = (currentPath: string) => {
    onProgress({
      uploadedBytes,
      totalBytes: batch.totalBytes,
      uploadedImages,
      totalImages,
      currentPath,
    });
  };
  batch.uploads.forEach((upload) => {
    if (upload.session.state === "complete") {
      uploadedBytes += upload.item.file.size;
      if (upload.item.kind === "image") uploadedImages += 1;
      return;
    }
    const received = new Set(upload.session.received);
    const chunkCount = Math.ceil(upload.item.file.size / upload.session.chunk_size);
    let receivedChunkCount = 0;
    received.forEach((index) => {
      if (index < 0 || index >= chunkCount) return;
      const start = index * upload.session.chunk_size;
      const end = Math.min(upload.item.file.size, start + upload.session.chunk_size);
      uploadedBytes += end - start;
      receivedChunkCount += 1;
    });
    const missingChunks = chunkCount - receivedChunkCount;
    remainingChunks.set(upload.session.upload_id, missingChunks);
    if (missingChunks === 0 && upload.item.kind === "image") {
      uploadedImages += 1;
    }
    openUploads.push(upload);
  });
  if (batch.uploads.length > 0) {
    reportProgress(batch.uploads[0].item.relPath);
  }

  await runConcurrentIterable(
    pendingChunkBatches(openUploads),
    MAX_CONCURRENT_TRANSFERS,
    async (chunks) => {
      await putChunkBatch(batch.datasetId, chunks);
      chunks.forEach((chunk) => {
        const received = chunk.upload.session.received;
        if (!received.includes(chunk.chunkNumber)) {
          // Keep the live prepared batch as the retry checkpoint. A later
          // failure must not make the next click resend chunks that this tab
          // already received a success response for.
          received.push(chunk.chunkNumber);
        }
        uploadedBytes += chunk.size;
        const uploadId = chunk.upload.session.upload_id;
        const remaining = Math.max(
          0,
          (remainingChunks.get(uploadId) ?? 0) - 1,
        );
        remainingChunks.set(uploadId, remaining);
        if (remaining === 0 && chunk.upload.item.kind === "image") {
          uploadedImages += 1;
        }
      });
      reportProgress(chunks[chunks.length - 1].upload.item.relPath);
    },
  );
  return {
    openUploads,
    knownJobId: batch.uploads.find((item) => item.jobId !== undefined)?.jobId ?? null,
  };
}

export async function completeUploadBatches(
  batch: PreparedUploadBatch,
  openUploads: readonly PreparedUpload[],
): Promise<number> {
  const created = await requestJson<{ job_id: number }>(
    `/api/datasets/${batch.datasetId}/upload-batches/complete`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        upload_ids: openUploads.map((upload) => upload.session.upload_id),
      }),
    },
  );
  openUploads.forEach((upload) => {
    batch.resumeRecord.uploads[upload.item.relPath] = resumeForItem(
      upload.item,
      upload.session.upload_id,
      upload.session.chunk_size,
      created.job_id,
    );
  });
  writeStoredJson(batch.resumeKey, batch.resumeRecord);
  return created.job_id;
}

export async function completeUploadBatch(
  operation: PreparedUploadOperation,
): Promise<number> {
  const created = await requestJson<{ job_id: number }>(
    `/api/datasets/${operation.datasetId}/upload-batches/${operation.batchId}/complete`,
    { method: "POST" },
  );
  operation.knownJobId = created.job_id;
  return created.job_id;
}

export function clearUploadBatchResume(
  batch: PreparedUploadBatch | PreparedUploadOperation,
) {
  if ("batchId" in batch) {
    removeStoredValue(batch.resumeKey);
    manifestMemory.delete(batch.datasetId);
    activeOperations.delete(batch.datasetId);
    return;
  }
  const upload = batch.uploads[0];
  if (upload) removeStoredValue(upload.resumeKey);
  else removeStoredValue(batch.resumeKey);
  batch.uploads.forEach((item) => {
    removeStoredValue(fingerprint(batch.datasetId, item.item));
  });
}

export async function pollUploadJob(
  jobId: number,
  onJob: (job: Job) => void,
): Promise<Job> {
  while (true) {
    const job = await requestJson<Job>(`/api/jobs/${jobId}`);
    onJob(job);
    if (
      job.state === "awaiting_class_resolution"
      || job.state === "done"
      || job.state === "failed"
    ) return job;
    await delay(500);
  }
}
