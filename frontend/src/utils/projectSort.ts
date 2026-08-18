export type ProjectSortOrder = "recent" | "name";

interface SortableProject {
  id: number;
  name: string;
  created_at: string;
}

const koreanNameCollator = new Intl.Collator("ko", {
  numeric: true,
  sensitivity: "base",
});

function compareRecent(left: SortableProject, right: SortableProject) {
  const timestampDifference = Date.parse(right.created_at) - Date.parse(left.created_at);
  if (Number.isFinite(timestampDifference) && timestampDifference !== 0) {
    return timestampDifference;
  }
  return right.id - left.id;
}

export function sortProjects<T extends SortableProject>(
  projects: readonly T[],
  order: ProjectSortOrder,
) {
  return [...projects].sort((left, right) => {
    if (order === "recent") return compareRecent(left, right);
    return koreanNameCollator.compare(left.name, right.name)
      || compareRecent(left, right);
  });
}
