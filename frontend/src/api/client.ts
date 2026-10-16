import { refreshAuthSession } from "./auth";
import { getAuthSnapshot } from "../store/auth";
import { getActiveWorkspaceId } from "../store/workspace";

export interface ApiRequestInit extends RequestInit {
  workspaceId?: number | null;
}

export function captureWorkspaceId(): number | null {
  return getActiveWorkspaceId();
}

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
  merge_purpose: "merge" | "train" | null;
}

export interface DatasetListItem extends DatasetRow {
  active_job: Job | null;
  source_datasets: DatasetListItem[];
}

export interface DatasetMergeInput {
  name: string;
  dataset_ids: number[];
  purpose: "merge" | "train";
}

export interface DatasetClassExtractionInput {
  name: string;
  dataset_ids: number[];
  class_ids: number[];
}

export interface DatasetMergeSourcesInput {
  dataset_ids: number[];
  purpose: "merge" | "train";
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
  workspace_id: number;
  my_role: "viewer" | "editor" | "admin" | "owner";
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

export interface ProjectMergeClassSource {
  project_id: number;
  class_id: number;
}

export interface ProjectMergeClass {
  name: string;
  color: string;
  sources: ProjectMergeClassSource[];
}

export interface ProjectMergeInput {
  name: string;
  projects: Array<{
    project_id: number;
    expected_updated_at: string;
  }>;
  classes: ProjectMergeClass[];
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

export interface AnnotationState {
  version: number;
  label_status: "unlabeled" | "in_progress" | "labeled";
  assignee_id: number | null;
}

export interface ImageRow extends AnnotationState {
  labeled_by: number | null;
  labeled_at: string | null;
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

export interface DatasetCatalogVersion {
  dataset_id: number;
  catalog_version: number;
}

export interface ClassCatalog {
  classes: ClassRow[];
  catalog_version: number;
}

export interface AnnotationResponse extends AnnotationState, ClassCatalog {
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
  | "class_conflict"
  | "upload_lock_retry";

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

export class ApiRequestTimeoutError extends ApiError {
  constructor(message = "서버 응답 제한 시간을 초과했습니다. 작업 상태를 확인한 뒤 다시 시도해 주세요.") {
    super(
      0,
      message,
    );
    this.name = "ApiRequestTimeoutError";
  }
}

async function errorFromResponse(response: Response): Promise<ApiError> {
  let message = `요청 실패 (${response.status})`;
  let detail: unknown;
  try {
    const body = await response.json() as { detail?: unknown };
    detail = body.detail;
    if (response.status === 403 && detail && typeof detail === "object"
      && "code" in detail && detail.code === "insufficient_role") {
      const roles: Record<string, string> = { owner: "소유자", admin: "관리자", editor: "라벨러" };
      const { required, actual } = detail as { required?: string; actual?: string };
      const currentRole = roles[actual ?? ""];
      message = `이 작업은 ${roles[required ?? ""] ?? "권한이 있는 사용자"} 이상만 할 수 있어요${currentRole ? ` (지금: ${currentRole})` : ""}`;
      window.dispatchEvent(new CustomEvent("annodock:role-error", { detail: message }));
    } else if (typeof detail === "string") {
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

function authorizedInit(
  init: RequestInit | undefined,
  accessToken: string | null,
  signal: AbortSignal,
): RequestInit {
  const headers = new Headers(init?.headers);
  if (accessToken) headers.set("Authorization", `Bearer ${accessToken}`);
  return {
    ...init,
    credentials: "include" as const,
    // Resource URLs are owner-scoped; do not let a browser reuse one user's
    // authenticated response after a different user logs in on the same origin.
    cache: init?.cache ?? "no-store",
    headers,
    signal,
  };
}

const API_REQUEST_DEADLINE_MS = 120_000;

function abortReason(signal: AbortSignal): unknown {
  return signal.reason ?? new DOMException("요청이 취소되었습니다.", "AbortError");
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

  const callerSignal = init?.signal;
  if (callerSignal?.aborted) throw abortReason(callerSignal);

  const controller = new AbortController();
  const timeoutError = new ApiRequestTimeoutError();
  let timedOut = false;
  let timeoutId: number | undefined;
  let removeCallerAbort: (() => void) | undefined;

  const timeout = new Promise<never>((_resolve, reject) => {
    timeoutId = window.setTimeout(() => {
      timedOut = true;
      controller.abort(timeoutError);
      reject(timeoutError);
    }, API_REQUEST_DEADLINE_MS);
  });
  const callerAbort = callerSignal
    ? new Promise<never>((_resolve, reject) => {
        const onAbort = () => {
          const reason = abortReason(callerSignal);
          controller.abort(reason);
          reject(reason);
        };
        callerSignal.addEventListener("abort", onAbort, { once: true });
        removeCallerAbort = () => callerSignal.removeEventListener("abort", onAbort);
      })
    : null;

  try {
    const request = fetch(path, authorizedInit(init, accessToken, controller.signal));
    return await Promise.race(
      callerAbort ? [request, timeout, callerAbort] : [request, timeout],
    );
  } catch (error: unknown) {
    if (timedOut) throw timeoutError;
    throw error;
  } finally {
    if (timeoutId !== undefined) window.clearTimeout(timeoutId);
    removeCallerAbort?.();
  }
}

async function retryApiOnce(
  path: string,
  init: RequestInit | undefined,
  accessToken: string,
): Promise<Response> {
  return fetchApiOnce(path, init, accessToken);
}

export async function apiFetch(
  path: string,
  init?: ApiRequestInit,
): Promise<Response> {
  const { workspaceId = captureWorkspaceId(), ...requestInit } = init ?? {};
  const headers = new Headers(requestInit.headers);
  if (init?.workspaceId !== undefined || !headers.has("X-Workspace-Id")) {
    if (workspaceId === null) headers.delete("X-Workspace-Id");
    else headers.set("X-Workspace-Id", String(workspaceId));
  }
  // Every retry uses this immutable request scope, even if the user switches
  // workspaces while token refresh or another request is still in flight.
  const scopedInit = { ...requestInit, headers };
  const tokenUsed = getAuthSnapshot().accessToken;
  const response = await fetchApiOnce(path, scopedInit, tokenUsed);
  if (response.status !== 401) return response;

  const current = getAuthSnapshot();
  if (current.accessToken && current.accessToken !== tokenUsed) {
    // Another request already rotated the pair while this request was in flight.
    return retryApiOnce(path, scopedInit, current.accessToken);
  }
  // The refresh credential lives in an httpOnly cookie, so recovery remains
  // possible even if this tab's JavaScript/localStorage copy disappeared.
  // Refresh errors deliberately propagate: callers can pause and preserve a
  // resumable operation instead of displaying a frozen progress percentage.
  const refreshedAccess = await refreshAuthSession(tokenUsed);
  // The original request is retried exactly once. A second 401 is returned to
  // the caller rather than entering another refresh loop.
  return retryApiOnce(path, scopedInit, refreshedAccess);
}

export async function requestJson<T>(
  path: string,
  init?: ApiRequestInit,
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

export interface ProjectTransferResult {
  project_id: number;
  workspace_id: number;
  moved_bytes: number;
}

export function transferProject(
  projectId: number, targetWorkspaceId: number, sourceWorkspaceId: number,
): Promise<ProjectTransferResult> {
  return requestJson(`/api/projects/${projectId}/transfer`, {
    ...jsonInit("POST", { target_workspace_id: targetWorkspaceId }),
    workspaceId: sourceWorkspaceId,
  });
}

export function mergeProjects(input: ProjectMergeInput): Promise<ProjectRow> {
  return requestJson<ProjectRow>("/api/projects/merge", jsonInit("POST", input));
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
): Promise<ProjectClassRow & { catalog_versions: DatasetCatalogVersion[] }> {
  return requestJson<ProjectClassRow & { catalog_versions: DatasetCatalogVersion[] }>(
    `/api/projects/${projectId}/classes/${classId}`,
    jsonInit("PATCH", input),
  );
}

export function updateProjectClasses(
  projectId: number,
  expectedUpdatedAt: string,
  classes: readonly (Omit<ProjectClassRow, "class_id"> & { class_id: number | null })[],
): Promise<{ classes: ProjectClassRow[]; catalog_versions: DatasetCatalogVersion[] }> {
  return requestJson<{ classes: ProjectClassRow[]; catalog_versions: DatasetCatalogVersion[] }>(
    `/api/projects/${projectId}/classes`,
    jsonInit("PUT", { expected_updated_at: expectedUpdatedAt, classes }),
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

function waitForNextJobPoll(): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, 500));
}

async function waitForMergedDataset(
  queued: DatasetListItem,
  workspaceId: number | null,
): Promise<DatasetListItem> {
  const jobId = queued.active_job?.job_id;
  if (jobId === undefined) return queued;

  while (true) {
    const job = await getJob(jobId, workspaceId);
    if (job.state === "failed") {
      let detail = "데이터셋 병합 작업에 실패했습니다.";
      try {
        const issues = await getAllIssues(queued.id, workspaceId);
        detail = issues[0]?.detail || detail;
      } catch {
        // Preserve the terminal job failure when issue details are unavailable.
      }
      throw new ApiError(500, detail);
    }
    if (job.state === "done") {
      const completed = job.datasets.find((dataset) => dataset.id === queued.id);
      if (!completed || completed.status !== "ready") {
        throw new ApiError(500, "병합 결과 데이터셋을 확인할 수 없습니다.");
      }
      return {
        ...queued,
        name: completed.name,
        status: completed.status,
        image_count: completed.image_count,
        annotation_count: completed.annotation_count,
        class_count: completed.class_count,
        active_job: null,
      };
    }
    await waitForNextJobPoll();
  }
}

export async function mergeDatasets(
  input: DatasetMergeInput,
  workspaceId: number | null = captureWorkspaceId(),
): Promise<DatasetListItem> {
  const queued = await requestJson<DatasetListItem>(
    "/api/datasets/merge",
    { ...jsonInit("POST", input), workspaceId },
  );
  return waitForMergedDataset(queued, workspaceId);
}

export function extractDatasetClasses(
  input: DatasetClassExtractionInput,
): Promise<DatasetRow> {
  return requestJson<DatasetRow>("/api/datasets/extract", jsonInit("POST", input));
}

export function extendMergedDataset(
  mergedDatasetId: number,
  input: DatasetMergeSourcesInput,
  workspaceId?: number | null,
): Promise<DatasetListItem> {
  return requestJson<DatasetListItem>(
    `/api/datasets/${mergedDatasetId}/merge-sources`,
    { ...jsonInit("POST", input), workspaceId },
  );
}

export function getDataset(datasetId: number, workspaceId?: number | null): Promise<DatasetDetail> {
  return requestJson(`/api/datasets/${datasetId}`, { workspaceId });
}

export function getDatasetClasses(
  datasetId: number,
  workspaceId?: number | null,
): Promise<ClassCatalog> {
  return requestJson(`/api/datasets/${datasetId}/classes`, { workspaceId });
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
  workspaceId?: number | null,
): Promise<AnnotationResponse> {
  return requestJson(
    `/api/images/${imageId}/annotations`,
    { signal, workspaceId },
  );
}

export function saveImageAnnotations(
  imageId: number,
  boxes: Array<Omit<BoxDto, "id">>,
  workspaceId: number | null | undefined,
  expectedVersion: number,
  expectedCatalogVersion: number,
): Promise<AnnotationState & ClassCatalog & { image_id: number; boxes: BoxDto[]; is_modified: boolean }> {
  return requestJson(
    `/api/images/${imageId}/annotations`,
    { ...jsonInit("PUT", { boxes, expected_version: expectedVersion, expected_catalog_version: expectedCatalogVersion }), workspaceId },
  );
}

export function renameDatasetClass(
  datasetId: number,
  classId: number,
  name: string,
  expectedCatalogVersion?: number,
  workspaceId?: number | null,
): Promise<ClassRow & ClassCatalog> {
  return requestJson(
    `/api/datasets/${datasetId}/classes/${classId}`,
    { ...jsonInit("PATCH", { name, expected_catalog_version: expectedCatalogVersion }), workspaceId },
  );
}

export function imageResourceUrl(imageId: number, kind: "file" | "thumb" = "file") {
  return `/api/images/${imageId}/${kind}`;
}

export function getJob(jobId: number, workspaceId?: number | null): Promise<Job> {
  return requestJson(`/api/jobs/${jobId}`, { workspaceId });
}

export function resolveJobClassConflicts(
  jobId: number,
  resolution: ClassResolutionRequest,
  workspaceId?: number | null,
): Promise<{ job_id: number }> {
  return requestJson(
    `/api/jobs/${jobId}/class-resolution`,
    { ...jsonInit("POST", resolution), workspaceId },
  );
}

export async function getAllIssues(datasetId: number, workspaceId: number | null = captureWorkspaceId()): Promise<IssueRow[]> {
  const items: IssueRow[] = [];
  let total = 1;
  while (items.length < total) {
    const page = await requestJson<PageResponse<IssueRow>>(
      `/api/datasets/${datasetId}/issues?offset=${items.length}&limit=1000`,
      { workspaceId },
    );
    total = page.total;
    items.push(...page.items);
    if (page.items.length === 0) break;
  }
  return items;
}

export async function downloadResponse(
  path: string,
  init?: ApiRequestInit,
): Promise<Blob> {
  const response = await responseOrThrow(await apiFetch(path, init));
  return response.blob();
}

export interface AdminWorkspaceUsage {
  id: number;
  kind: "personal" | "team";
  name: string;
  bytes_used: number;
  limit_bytes: number;
  quota_override: boolean;
}

export interface AdminUserWorkspaceRow extends AdminWorkspaceUsage {
  role: "owner" | "admin" | "editor" | "viewer";
}

export interface AdminWorkspaceRow extends AdminWorkspaceUsage {
  owner_user_id: number | null;
  member_count: number;
}

export interface AdminUserRow {
  id: number;
  email: string | null;
  username: string | null;
  created_at: string;
  login_methods: string[];
  bytes_used: number;
  limit_bytes: number;
  workspaces: AdminUserWorkspaceRow[];
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
  workspaceId: number | null;
  request: Promise<StorageQuota>;
} | null = null;

export const STORAGE_QUOTA_INVALIDATED_EVENT = "annodock:storage-quota-invalidated";

export function getStorageQuota(
  tokenKey: string, workspaceId: number | null = captureWorkspaceId(),
): Promise<StorageQuota> {
  if (storageQuotaCache?.tokenKey === tokenKey && storageQuotaCache.workspaceId === workspaceId) {
    return storageQuotaCache.request;
  }
  const request = requestJson<StorageQuota>("/api/storage", { workspaceId });
  storageQuotaCache = { tokenKey, workspaceId, request };
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

export function getAdminWorkspaces(): Promise<{ workspaces: AdminWorkspaceRow[] }> {
  return requestJson("/api/admin/workspaces");
}

export function updateAdminWorkspaceQuota(
  workspaceId: number, limitBytes: number,
): Promise<{ workspace_id: number; limit_bytes: number }> {
  return requestJson(`/api/admin/workspaces/${workspaceId}/quota`, jsonInit("PATCH", { limit_bytes: limitBytes }));
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


export function getImage(imageId: number, workspaceId?: number | null): Promise<ImageRow> {
  return requestJson(`/api/images/${imageId}`, { workspaceId });
}
export function claimDatasetImage(datasetId: number, split: string | null, workspaceId: number): Promise<{image: ImageRow | null; position?: number}> {
  return requestJson(`/api/datasets/${datasetId}/claim`, { ...jsonInit("POST", { split }), workspaceId });
}
export function completeImage(imageId: number, workspaceId: number): Promise<ImageRow> {
  return requestJson(`/api/images/${imageId}/complete`, { method: "POST", workspaceId });
}
export function uncompleteImage(imageId: number, workspaceId: number): Promise<ImageRow> {
  return requestJson(`/api/images/${imageId}/uncomplete`, { method: "POST", workspaceId });
}
export function assignImage(imageId: number, assigneeId: number | null, workspaceId: number): Promise<ImageRow> {
  return requestJson(`/api/images/${imageId}/assignment`, { ...jsonInit("PATCH", { assignee_id: assigneeId }), workspaceId });
}
export interface DatasetProgress {
  total: number;
  unlabeled: number;
  in_progress: number;
  labeled: number;
  imported_labeled: number;
  members: Array<{user_id: number; in_progress: number; labeled: number}>;
}
export function getDatasetProgress(id: number, workspaceId: number): Promise<DatasetProgress> {
  return requestJson(`/api/datasets/${id}/progress`, { workspaceId });
}
export function distributeDatasetAssignments(id: number, userIds: number[], workspaceId: number): Promise<{assigned: Record<string, number>}> {
  return requestJson(`/api/datasets/${id}/assignments/distribute`, { ...jsonInit("POST", { user_ids: userIds }), workspaceId });
}
export function reclaimDatasetAssignments(id: number, userId: number, workspaceId: number): Promise<{reclaimed: number}> {
  return requestJson(`/api/datasets/${id}/assignments/reclaim`, { ...jsonInit("POST", { user_id: userId }), workspaceId });
}
