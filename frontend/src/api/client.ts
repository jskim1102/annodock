import { refreshAuthSession } from "./auth";
import { clearAuthSession, getAuthSnapshot } from "../store/auth";

export type DatasetStatus = "pending" | "processing" | "ready" | "failed" | "archived";
export type JobState =
  | "queued"
  | "running"
  | "awaiting_class_resolution"
  | "done"
  | "failed";

export type ClassResolutionAction = "use_project" | "use_upload";

export interface ClassNameConflict {
  key: string;
  class_id: number;
  source_path: string;
  project_name: string;
  uploaded_name: string;
}

export interface ClassResolutionPlan {
  revision: string;
  conflicts: ClassNameConflict[];
}

export interface ClassResolutionChoice {
  key: string;
  action: ClassResolutionAction;
}

export interface ClassResolutionRequest {
  revision: string;
  resolutions: ClassResolutionChoice[];
}

export interface PageResponse<T> {
  items: T[];
  total: number;
}

export interface Job {
  job_id: number;
  state: JobState;
  total: number;
  processed: number;
  failed: number;
  image_total: number;
  image_processed: number;
  phase: string;
  class_resolution?: ClassResolutionPlan | null;
  datasets: JobDataset[];
}

export interface JobDataset {
  id: number;
  name: string;
  status: DatasetStatus;
  image_count: number;
  annotation_count: number;
  class_count: number;
  part_index?: number;
  part_count?: number;
}

export interface DatasetRow {
  id: number;
  project_id: number;
  name: string;
  status: DatasetStatus;
  image_count: number;
  annotation_count: number;
  class_count: number;
  created_at: string;
  is_merged: boolean;
}

export interface DatasetListItem extends DatasetRow {
  active_job: Job | null;
  source_datasets: DatasetListItem[];
}

export interface DatasetMergeInput {
  name: string;
  dataset_ids: number[];
}

export interface DatasetClassExtractionInput {
  name: string;
  dataset_ids: number[];
  class_ids: number[];
}

export interface DatasetMergeSourcesInput {
  dataset_ids: number[];
}

export interface DatasetMergeOverlap {
  code: "dataset_merge_source_overlap";
  merged_dataset: {
    id: number;
    name: string;
    source_dataset_ids: number[];
  };
}

export interface DatasetDetail extends DatasetRow {
  splits: { split: string; image_count: number }[];
}

export interface ProjectClassRow {
  class_id: number;
  name: string;
  color: string;
}

export interface ProjectClassImageCount extends ProjectClassRow {
  image_count: number;
}

export interface ProjectDatasetSourceRow extends DatasetRow {
  labeled_image_count: number;
  storage_bytes: number;
  physical_storage_bytes: number;
  active_job: Job | null;
}

export interface ProjectDatasetRow extends ProjectDatasetSourceRow {
  source_datasets: ProjectDatasetSourceRow[];
}

export interface ProjectRow {
  id: number;
  name: string;
  created_at: string;
  updated_at: string;
  archived: boolean;
  dataset_count: number;
  image_count: number;
  annotation_count: number;
  class_count: number;
  classes: ProjectClassRow[];
  datasets: ProjectDatasetRow[];
}

export interface ProjectCreateInput {
  name: string;
  classes: Array<{ name: string; color: string }>;
}

export interface ProjectRenameResponse {
  id: number;
  name: string;
}

export interface ProjectDeleteDataset {
  id: number;
  name: string;
}

export interface ProjectDeleteConfirmation {
  code: "project-delete-confirmation-required";
  requires_confirmation: true;
  warning: string;
  datasets: ProjectDeleteDataset[];
}

export interface ImageRow {
  id: number;
  stem: string;
  filename: string;
  split: string | null;
  width: number;
  height: number;
  box_count: number;
  is_modified: boolean;
}

export interface BoxDto {
  id: number;
  class_id: number;
  cx: number;
  cy: number;
  w: number;
  h: number;
}

export interface AnnotationResponse {
  image_id: number;
  width: number;
  height: number;
  boxes: BoxDto[];
}

export interface ClassRow {
  class_id: number;
  name: string;
}

export type IssueKind =
  | "image_without_label"
  | "empty_label"
  | "label_without_image"
  | "broken_image"
  | "broken_label"
  | "rejected_file"
  | "duplicate_skipped"
  | "ignored_file"
  | "class_conflict";

export interface IssueRow {
  kind: IssueKind;
  path: string;
  detail: string;
}

export class ApiError extends Error {
  constructor(
    public readonly status: number,
    message: string,
    public readonly detail?: unknown,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

async function errorFromResponse(response: Response): Promise<ApiError> {
  let message = `요청 실패 (${response.status})`;
  let detail: unknown;
  try {
    const body = await response.json() as { detail?: unknown };
    detail = body.detail;
    if (typeof detail === "string") {
      message = detail;
    } else if (detail && typeof detail === "object" && "message" in detail) {
      const candidate = (detail as { message?: unknown }).message;
      if (typeof candidate === "string") message = candidate;
    } else if (Array.isArray(detail)) {
      message = "입력값을 확인해 주세요.";
    }
  } catch {
    // Non-JSON responses keep the status-based fallback.
  }
  return new ApiError(response.status, message, detail);
}

export async function responseOrThrow(response: Response): Promise<Response> {
  if (!response.ok) throw await errorFromResponse(response);
  return response;
}

function authorizedInit(init: RequestInit | undefined, accessToken: string | null) {
  const headers = new Headers(init?.headers);
  if (accessToken) headers.set("Authorization", `Bearer ${accessToken}`);
  return {
    ...init,
    credentials: "include" as const,
    // Resource URLs are owner-scoped; do not let a browser reuse one user's
    // authenticated response after a different user logs in on the same origin.
    cache: init?.cache ?? "no-store",
    headers,
  };
}

async function fetchApiOnce(
  path: string,
  init: RequestInit | undefined,
  accessToken: string | null,
): Promise<Response> {
  const url = new URL(path, window.location.origin);
  if (url.origin !== window.location.origin || !url.pathname.startsWith("/api/")) {
    throw new Error("apiFetch는 동일 오리진 /api 경로만 호출할 수 있습니다.");
  }
  return fetch(path, authorizedInit(init, accessToken));
}

async function retryApiOnce(
  path: string,
  init: RequestInit | undefined,
  accessToken: string,
): Promise<Response> {
  const retried = await fetchApiOnce(path, init, accessToken);
  if (retried.status === 401) clearAuthSession();
  return retried;
}

export async function apiFetch(
  path: string,
  init?: RequestInit,
): Promise<Response> {
  const tokenUsed = getAuthSnapshot().accessToken;
  const response = await fetchApiOnce(path, init, tokenUsed);
  if (response.status !== 401) return response;

  const current = getAuthSnapshot();
  if (current.accessToken && current.accessToken !== tokenUsed) {
    // Another request already rotated the pair while this request was in flight.
    return retryApiOnce(path, init, current.accessToken);
  }
  if (!current.refreshToken) {
    clearAuthSession();
    return response;
  }

  try {
    const refreshedAccess = await refreshAuthSession();
    // The original request is retried exactly once. A second 401 is returned to
    // the caller rather than entering another refresh loop.
    return retryApiOnce(path, init, refreshedAccess);
  } catch {
    return response;
  }
}

export async function requestJson<T>(
  path: string,
  init?: RequestInit,
): Promise<T> {
  const response = await responseOrThrow(await apiFetch(path, init));
  if (response.status === 204) return undefined as T;
  return await response.json() as T;
}

function jsonInit(method: string, body?: unknown): RequestInit {
  return {
    method,
    ...(body === undefined
      ? {}
      : {
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        }),
  };
}

export function getDatasets(): Promise<PageResponse<DatasetListItem>> {
  return requestJson("/api/datasets?offset=0&limit=200");
}

export function getProjects(): Promise<PageResponse<ProjectRow>> {
  return requestJson("/api/projects");
}

export function getProject(projectId: number): Promise<ProjectRow> {
  return requestJson(`/api/projects/${projectId}`);
}

export function getProjectClassImageCounts(
  projectId: number,
  datasetIds: readonly number[],
): Promise<{ items: ProjectClassImageCount[] }> {
  const query = new URLSearchParams();
  datasetIds.forEach((datasetId) => {
    query.append("dataset_ids", String(datasetId));
  });
  return requestJson(`/api/projects/${projectId}/class-image-counts?${query}`);
}

export function createProject(project: ProjectCreateInput): Promise<ProjectRow> {
  return requestJson("/api/projects", jsonInit("POST", project));
}

export function renameProject(
  projectId: number,
  name: string,
): Promise<ProjectRenameResponse> {
  return requestJson(
    `/api/projects/${projectId}`,
    jsonInit("PATCH", { name }),
  );
}

export function deleteProject(projectId: number, confirm = false): Promise<void> {
  const suffix = confirm ? "?confirm=true" : "";
  return requestJson(`/api/projects/${projectId}${suffix}`, jsonInit("DELETE"));
}

export function deleteDataset(datasetId: number): Promise<void> {
  return requestJson(`/api/datasets/${datasetId}`, jsonInit("DELETE"));
}

export function updateProjectClass(
  projectId: number,
  classId: number,
  input: { name?: string; color?: string },
): Promise<ProjectClassRow> {
  return requestJson<ProjectClassRow>(
    `/api/projects/${projectId}/classes/${classId}`,
    jsonInit("PATCH", input),
  );
}

export function renameDataset(
  datasetId: number,
  name: string,
): Promise<{ id: number; name: string }> {
  return requestJson<{ id: number; name: string }>(
    `/api/datasets/${datasetId}`,
    jsonInit("PATCH", { name }),
  );
}

export function mergeDatasets(input: DatasetMergeInput): Promise<DatasetListItem> {
  return requestJson<DatasetListItem>("/api/datasets/merge", jsonInit("POST", input));
}

export function extractDatasetClasses(
  input: DatasetClassExtractionInput,
): Promise<DatasetRow> {
  return requestJson<DatasetRow>("/api/datasets/extract", jsonInit("POST", input));
}

export function extendMergedDataset(
  mergedDatasetId: number,
  input: DatasetMergeSourcesInput,
): Promise<DatasetListItem> {
  return requestJson<DatasetListItem>(
    `/api/datasets/${mergedDatasetId}/merge-sources`,
    jsonInit("POST", input),
  );
}

export function getDataset(datasetId: number): Promise<DatasetDetail> {
  return requestJson(`/api/datasets/${datasetId}`);
}

export function getDatasetClasses(
  datasetId: number,
): Promise<{ classes: ClassRow[] }> {
  return requestJson(`/api/datasets/${datasetId}/classes`);
}

export function getDatasetImages(
  datasetId: number,
  offset: number,
  limit: number,
  split: string | null = null,
): Promise<PageResponse<ImageRow>> {
  const query = new URLSearchParams({
    offset: String(offset),
    limit: String(limit),
  });
  if (split !== null) query.set("split", split);
  return requestJson(`/api/datasets/${datasetId}/images?${query}`);
}

export function getImageAnnotations(
  imageId: number,
  signal?: AbortSignal,
): Promise<AnnotationResponse> {
  return requestJson(
    `/api/images/${imageId}/annotations`,
    signal ? { signal } : undefined,
  );
}

export function saveImageAnnotations(
  imageId: number,
  boxes: Array<Omit<BoxDto, "id">>,
): Promise<{ image_id: number; boxes: BoxDto[]; is_modified: boolean }> {
  return requestJson(
    `/api/images/${imageId}/annotations`,
    jsonInit("PUT", { boxes }),
  );
}

export function renameDatasetClass(
  datasetId: number,
  classId: number,
  name: string,
): Promise<ClassRow> {
  return requestJson(
    `/api/datasets/${datasetId}/classes/${classId}`,
    jsonInit("PATCH", { name }),
  );
}

export function imageResourceUrl(imageId: number, kind: "file" | "thumb" = "file") {
  return `/api/images/${imageId}/${kind}`;
}

export function getJob(jobId: number): Promise<Job> {
  return requestJson(`/api/jobs/${jobId}`);
}

export function resolveJobClassConflicts(
  jobId: number,
  resolution: ClassResolutionRequest,
): Promise<{ job_id: number }> {
  return requestJson(
    `/api/jobs/${jobId}/class-resolution`,
    jsonInit("POST", resolution),
  );
}

export async function getAllIssues(datasetId: number): Promise<IssueRow[]> {
  const items: IssueRow[] = [];
  let total = 1;
  while (items.length < total) {
    const page = await requestJson<PageResponse<IssueRow>>(
      `/api/datasets/${datasetId}/issues?offset=${items.length}&limit=1000`,
    );
    total = page.total;
    items.push(...page.items);
    if (page.items.length === 0) break;
  }
  return items;
}

export async function downloadResponse(
  path: string,
  init?: RequestInit,
): Promise<Blob> {
  const response = await responseOrThrow(await apiFetch(path, init));
  return response.blob();
}

export interface AdminUserRow {
  id: number;
  email: string | null;
  username: string | null;
  created_at: string;
  login_methods: string[];
  bytes_used: number;
  limit_bytes: number;
}

export interface AdminQuotaUpdateResponse {
  user_id: number;
  limit_bytes: number;
}

export interface AdminOverview {
  user_count: number;
  storage_total_bytes: number;
}

export interface StorageQuota {
  used_bytes: number;
  referenced_bytes: number;
  limit_bytes: number;
}

let storageQuotaCache: {
  tokenKey: string;
  request: Promise<StorageQuota>;
} | null = null;

export const STORAGE_QUOTA_INVALIDATED_EVENT = "annodock:storage-quota-invalidated";

export function getStorageQuota(tokenKey: string): Promise<StorageQuota> {
  if (storageQuotaCache?.tokenKey === tokenKey) {
    return storageQuotaCache.request;
  }
  const request = requestJson<StorageQuota>("/api/storage");
  storageQuotaCache = { tokenKey, request };
  void request.catch(() => {
    if (storageQuotaCache?.request === request) storageQuotaCache = null;
  });
  return request;
}

export function resetStorageQuotaCache(): void {
  storageQuotaCache = null;
}

export function invalidateStorageQuotaCache(): void {
  resetStorageQuotaCache();
  window.dispatchEvent(new Event(STORAGE_QUOTA_INVALIDATED_EVENT));
}

export function getAdminOverview(): Promise<AdminOverview> {
  return requestJson("/api/admin/overview");
}

export function getAdminUsers(): Promise<{ users: AdminUserRow[] }> {
  return requestJson("/api/admin/users");
}

export function updateAdminUserQuota(
  userId: number,
  limitBytes: number,
): Promise<AdminQuotaUpdateResponse> {
  return requestJson(
    `/api/admin/users/${userId}/quota`,
    jsonInit("PATCH", { limit_bytes: limitBytes }),
  );
}

export function deleteAdminUser(userId: number, confirm = false): Promise<void> {
  const suffix = confirm ? "?confirm=true" : "";
  return requestJson(`/api/admin/users/${userId}${suffix}`, jsonInit("DELETE"));
}
