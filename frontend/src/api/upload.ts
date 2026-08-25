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

interface ResumeRecord {
  uploadId: number;
  chunkSize: number;
  jobId?: number;
}

interface PreparedUpload {
  item: CollectedFile;
  resumeKey: string;
  session: UploadSession;
  jobId?: number;
}

export interface PreparedUploadBatch {
  datasetId: number;
  uploads: PreparedUpload[];
  totalBytes: number;
}

export interface UploadProgress {
  uploadedBytes: number;
  totalBytes: number;
  uploadedImages: number;
  totalImages: number;
  currentPath: string;
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

function readResume(key: string): ResumeRecord | null {
  const parsed = readStoredJson(key);
  if (!parsed || typeof parsed !== "object") return null;
  const record = parsed as Partial<ResumeRecord>;
  if (typeof record.uploadId !== "number") return null;
  return {
    uploadId: record.uploadId,
    chunkSize: typeof record.chunkSize === "number"
      ? record.chunkSize
      : DEFAULT_CHUNK_SIZE,
    jobId: typeof record.jobId === "number" ? record.jobId : undefined,
  };
}

function expectedExtractedSize(item: CollectedFile) {
  return item.kind === "zip" ? item.file.size * 4 : item.file.size;
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

async function createUpload(datasetId: number, item: CollectedFile) {
  return requestJson<{ upload_id: number; chunk_size: number; received: number[] }>(
    `/api/datasets/${datasetId}/uploads`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        filename: item.relPath,
        size: item.file.size,
        chunk_size: DEFAULT_CHUNK_SIZE,
        kind: item.kind === "zip" ? "zip" : "file",
        file_count: 1,
        expected_extracted_size: expectedExtractedSize(item),
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

export async function prepareUploadBatch(
  datasetId: number,
  collected: readonly CollectedFile[],
): Promise<PreparedUploadBatch> {
  const accepted = collected.filter((item) => item.kind !== "other");
  if (accepted.length === 0) throw new UploadError(422, "업로드할 파일이 없습니다.");
  await preflight(datasetId, accepted);
  const uploads: PreparedUpload[] = [];

  for (const item of accepted) {
    const resumeKey = fingerprint(datasetId, item);
    const resume = readResume(resumeKey);
    let session: UploadSession | null = null;
    if (resume) {
      try {
        session = await requestJson<UploadSession>(
          `/api/uploads/${resume.uploadId}`,
        );
        if (session.size !== item.file.size) session = null;
      } catch (error) {
        const status = error && typeof error === "object" && "status" in error
          ? Number(error.status)
          : 0;
        if (status !== 404) throw error;
      }
      if (!session) removeStoredValue(resumeKey);
    }
    if (session?.state === "aborted") {
      removeStoredValue(resumeKey);
      throw new UploadError(409, `${item.relPath}: 중단된 업로드입니다.`);
    }
    if (!session) {
      const created = await createUpload(datasetId, item);
      session = {
        ...created,
        size: item.file.size,
        state: "open",
      };
      writeStoredJson(resumeKey, {
        uploadId: created.upload_id,
        chunkSize: created.chunk_size,
      } satisfies ResumeRecord);
    }
    uploads.push({ item, resumeKey, session, jobId: resume?.jobId });
  }

  return {
    datasetId,
    uploads,
    totalBytes: accepted.reduce((sum, item) => sum + item.file.size, 0),
  };
}

export async function transferUploadBatch(
  batch: PreparedUploadBatch,
  onProgress: (progress: UploadProgress) => void,
): Promise<number | null> {
  const uploadIds: number[] = [];
  let completedBytes = 0;
  let completedImages = 0;
  const totalImages = batch.uploads.reduce(
    (count, upload) => count + (upload.item.kind === "image" ? 1 : 0),
    0,
  );
  for (const upload of batch.uploads) {
    if (upload.session.state === "complete") {
      completedBytes += upload.item.file.size;
      if (upload.item.kind === "image") completedImages += 1;
      onProgress({
        uploadedBytes: completedBytes,
        totalBytes: batch.totalBytes,
        uploadedImages: completedImages,
        totalImages,
        currentPath: upload.item.relPath,
      });
      continue;
    }
    const received = new Set(upload.session.received);
    const chunkCount = Math.ceil(upload.item.file.size / upload.session.chunk_size);
    for (let index = 0; index < chunkCount; index += 1) {
      const start = index * upload.session.chunk_size;
      const end = Math.min(upload.item.file.size, start + upload.session.chunk_size);
      if (!received.has(index)) {
        await putChunk(
          upload.session.upload_id,
          index,
          upload.item.file.slice(start, end),
        );
      }
      onProgress({
        uploadedBytes: completedBytes + end,
        totalBytes: batch.totalBytes,
        uploadedImages: completedImages + (
          upload.item.kind === "image" && end === upload.item.file.size ? 1 : 0
        ),
        totalImages,
        currentPath: upload.item.relPath,
      });
    }
    completedBytes += upload.item.file.size;
    if (upload.item.kind === "image") completedImages += 1;
    onProgress({
      uploadedBytes: completedBytes,
      totalBytes: batch.totalBytes,
      uploadedImages: completedImages,
      totalImages,
      currentPath: upload.item.relPath,
    });
    uploadIds.push(upload.session.upload_id);
  }
  if (uploadIds.length === 0) {
    return batch.uploads.find((item) => item.jobId !== undefined)?.jobId ?? null;
  }
  const created = await requestJson<{ job_id: number }>(
    `/api/datasets/${batch.datasetId}/upload-batches/complete`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ upload_ids: uploadIds }),
    },
  );
  for (const upload of batch.uploads) {
    if (uploadIds.includes(upload.session.upload_id)) {
      writeStoredJson(upload.resumeKey, {
        uploadId: upload.session.upload_id,
        chunkSize: upload.session.chunk_size,
        jobId: created.job_id,
      } satisfies ResumeRecord);
    }
  }
  return created.job_id;
}

export function clearUploadBatchResume(batch: PreparedUploadBatch) {
  batch.uploads.forEach((upload) => removeStoredValue(upload.resumeKey));
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
