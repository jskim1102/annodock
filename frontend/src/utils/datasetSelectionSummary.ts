import type { ProjectDatasetRow } from "../api/client";

export interface DatasetSelectionSummary {
  datasetCount: number;
  imageCount: number;
  classCount: number;
}

type SelectionRow = Pick<ProjectDatasetRow, "image_count">;

export function getProjectSelectionRows(
  rows: readonly ProjectDatasetRow[],
): ProjectDatasetRow[] {
  return rows.flatMap((row) => [
    row,
    ...row.source_datasets.map((source) => ({
      ...source,
      source_datasets: [],
    })),
  ]);
}

export function toggleDatasetSelection(
  current: ReadonlySet<number>,
  datasetId: number,
  mutuallyExclusiveIds: readonly number[] = [],
): Set<number> {
  const next = new Set(current);
  if (next.has(datasetId)) {
    next.delete(datasetId);
    return next;
  }

  mutuallyExclusiveIds.forEach((id) => next.delete(id));
  next.add(datasetId);
  return next;
}

export function getDatasetSelectionSummary(
  rows: readonly SelectionRow[],
  projectClassCount: number,
): DatasetSelectionSummary | null {
  if (rows.length === 0) return null;

  return {
    datasetCount: rows.length,
    imageCount: rows.reduce((total, row) => total + row.image_count, 0),
    classCount: projectClassCount,
  };
}
