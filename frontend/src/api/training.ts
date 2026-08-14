import { apiFetch, requestJson, responseOrThrow, type PageResponse } from "./client";

export type RunState = "queued" | "running" | "canceling" | "done" | "failed" | "canceled";
export type ArtifactName = "best.pt" | "last.pt" | "results.csv";

export interface ModelPreset {
  name: string;
  type: "preset";
  size_mb: number | null;
}

export type TrainingOptimizer =
  | "Adam"
  | "Adamax"
  | "AdamW"
  | "NAdam"
  | "RAdam"
  | "RMSProp"
  | "SGD"
  | "MuSGD"
  | "auto";

export interface TrainingArguments {
  exclude_unlabeled_images: boolean;
  include_unlabeled_images_in_test: boolean;
  device: 0;
  optimizer: TrainingOptimizer;
  lr0: number;
  lrf: number;
  warmup_epochs: number;
  cos_lr: boolean;
  patience: number;
  augment: boolean;
  mosaic: number;
  mixup: number;
  copy_paste: number;
  close_mosaic: number;
  hsv_h: number;
  hsv_s: number;
  hsv_v: number;
  fliplr: number;
  scale: number;
  translate: number;
  workers: number;
  cache: "none" | "ram" | "disk";
  amp: boolean;
  compile: boolean;
  deterministic: boolean;
  save_period: number;
  multi_scale: number;
}

export interface TrainingRecommendation {
  policy_version: string;
  total_images: number;
  labeled_images: number;
  unlabeled_images: number;
  train_images: number;
  total_instances: number;
  instances_per_image: number;
  small_object_ratio: number;
  epochs: number;
  imgsz: number;
  batch: number;
  optimizer: "auto";
  lr0: number;
  warmup_epochs: number;
  patience: number;
  mosaic: number;
  mixup: number;
  scale: number;
  amp: boolean;
  close_mosaic: number;
  copy_paste: 0;
  compile: boolean;
  effective_max_imgsz: number;
  reasons: string[];
}

export interface StartTrainingBody extends TrainingArguments {
  weights: string;
  epochs: number;
  imgsz: number;
  batch: number;
  split_mode: "2way" | "3way";
  ratios: Record<string, number>;
  seed?: number;
}

export interface StartTrainingResponse {
  run_id: number;
  warnings: string[];
}

export interface RunSummary {
  id: number;
  dataset_id: number | null;
  dataset_name: string;
  weights: string;
  state: RunState;
  epochs: number;
  epoch: number;
  started_at: string | null;
  finished_at: string | null;
  artifacts_deleted_at: string | null;
}

export interface RunDetail extends RunSummary {
  imgsz: number;
  batch: number;
  split_mode: "2way" | "3way";
  ratios: Record<string, number>;
  seed: number;
  training_args: TrainingArguments;
  error: string | null;
  image_counts: { train: number; valid: number; test: number };
}

export interface RunMetric {
  epoch: number;
  box_loss: number | null;
  cls_loss: number | null;
  dfl_loss: number | null;
  map50: number | null;
  map5095: number | null;
  lr: Record<string, number> | null;
}

export interface InferenceImageRow {
  id: number;
  image_id: number;
  filename: string;
}

export interface InferenceImagePage {
  split: "valid" | "test";
  items: InferenceImageRow[];
  next_cursor: number | null;
  total: number | null;
}

function jsonPost(body?: unknown): RequestInit {
  return {
    method: "POST",
    ...(body === undefined
      ? {}
      : {
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        }),
  };
}

export const getModels = () => requestJson<ModelPreset[]>("/api/models");

export function getTrainingRecommendation(
  datasetId: number,
  options: {
    weights: string;
    imgsz: number;
    multiScale: number;
    trainRatio: number;
    excludeUnlabeledImages: boolean;
    includeUnlabeledImagesInTest: boolean;
  },
) {
  const query = new URLSearchParams({
    weights: options.weights,
    imgsz: String(options.imgsz),
    multi_scale: String(options.multiScale),
    train_ratio: String(options.trainRatio),
    exclude_unlabeled_images: String(options.excludeUnlabeledImages),
    include_unlabeled_images_in_test: String(options.includeUnlabeledImagesInTest),
  });
  return requestJson<TrainingRecommendation>(
    `/api/datasets/${datasetId}/training-recommendation?${query}`,
  );
}

export const startTraining = (datasetId: number, body: StartTrainingBody) =>
  requestJson<StartTrainingResponse>(
    `/api/datasets/${datasetId}/train`,
    jsonPost(body),
  );

export function getRuns(datasetId?: number): Promise<PageResponse<RunSummary>> {
  const query = new URLSearchParams({ offset: "0", limit: "200" });
  if (datasetId !== undefined) query.set("dataset_id", String(datasetId));
  return requestJson(`/api/runs?${query}`);
}

export const getRun = (runId: number) =>
  requestJson<RunDetail>(`/api/runs/${runId}`);

export const getRunMetrics = (runId: number) =>
  requestJson<RunMetric[]>(`/api/runs/${runId}/metrics`);

export async function getRunLog(runId: number): Promise<string> {
  const response = await responseOrThrow(
    await apiFetch(`/api/runs/${runId}/log?tail=200`),
  );
  return response.text();
}

export const cancelRun = (runId: number) =>
  requestJson<{ run_id: number; state: "canceled" }>(
    `/api/runs/${runId}/cancel`,
    jsonPost(),
  );

export async function deleteRunArtifacts(runId: number): Promise<void> {
  await responseOrThrow(await apiFetch(`/api/runs/${runId}/artifacts`, {
    method: "DELETE",
  }));
}

export async function deleteRun(runId: number): Promise<void> {
  await responseOrThrow(await apiFetch(`/api/runs/${runId}?confirm=true`, {
    method: "DELETE",
  }));
}

export function artifactUrl(runId: number, name: ArtifactName) {
  return `/api/runs/${runId}/artifacts/${name}`;
}

export function getRunInferenceImages(
  runId: number,
  cursor: number | null = null,
  limit = 12,
): Promise<InferenceImagePage> {
  const query = new URLSearchParams({ limit: String(limit) });
  if (cursor !== null) query.set("cursor", String(cursor));
  return requestJson(`/api/runs/${runId}/inference-images?${query}`);
}

export async function inferRunImage(
  runId: number,
  runImageId: number,
): Promise<Blob> {
  const response = await responseOrThrow(await apiFetch(
    `/api/runs/${runId}/inference-images/${runImageId}`,
    { method: "POST" },
  ));
  return response.blob();
}
