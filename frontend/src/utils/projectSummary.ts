import type { ProjectRow } from "../api/client";

export interface ProjectSummary {
  total: number;
  inProgress: number;
  completed: number;
  archived: number;
}

type SummaryRow = Pick<ProjectRow, "archived" | "datasets">;

export function getProjectSummary(rows: readonly SummaryRow[]): ProjectSummary {
  return {
    total: rows.length,
    inProgress: rows.filter((row) => !row.archived && row.datasets.some((dataset) =>
      dataset.active_job !== null
      || dataset.status === "pending"
      || dataset.status === "processing"
    )).length,
    completed: rows.filter((row) =>
      !row.archived
      && row.datasets.length > 0
      && row.datasets.every((dataset) => dataset.status === "ready")
    ).length,
    archived: rows.filter((row) => row.archived).length,
  };
}
