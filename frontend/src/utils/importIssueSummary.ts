import type { IssueKind, IssueRow } from "../api/client";

export interface ScopedIssueRow extends IssueRow {
  summaryScope?: string;
}

export interface ImportIssueSummary {
  counts: Map<IssueKind, number>;
  total: number;
}

export interface ImportIssueDetailGroup {
  key: string;
  summaryScope?: string;
  path: string;
  details: string[];
}

export function groupImportIssueDetails(
  issues: readonly ScopedIssueRow[],
  kind: IssueKind,
): ImportIssueDetailGroup[] {
  const grouped = new Map<
    string,
    { group: ImportIssueDetailGroup; details: Set<string> }
  >();

  issues.forEach((issue) => {
    if (issue.kind !== kind) return;
    const key = `${issue.summaryScope ?? ""}\u0000${issue.path}`;
    let entry = grouped.get(key);
    if (entry === undefined) {
      entry = {
        group: {
          key,
          summaryScope: issue.summaryScope,
          path: issue.path,
          details: [],
        },
        details: new Set<string>(),
      };
      grouped.set(key, entry);
    }
    if (!entry.details.has(issue.detail)) {
      entry.details.add(issue.detail);
      entry.group.details.push(issue.detail);
    }
  });

  return [...grouped.values()].map((entry) => entry.group);
}

export function getImportIssueSummary(
  issues: readonly ScopedIssueRow[],
): ImportIssueSummary {
  const counts = new Map<IssueKind, number>();
  const brokenLabelPaths = new Set<string>();

  issues.forEach((issue) => {
    if (issue.kind === "broken_label") {
      brokenLabelPaths.add(`${issue.summaryScope ?? ""}\u0000${issue.path}`);
      return;
    }
    counts.set(issue.kind, (counts.get(issue.kind) ?? 0) + 1);
  });

  if (brokenLabelPaths.size > 0) {
    counts.set("broken_label", brokenLabelPaths.size);
  }

  return {
    counts,
    total: [...counts.values()].reduce((sum, count) => sum + count, 0),
  };
}
