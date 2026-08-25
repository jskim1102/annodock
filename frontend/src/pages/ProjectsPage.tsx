import { Fragment, useEffect, useMemo, useRef, useState } from "react";

import {
  ApiError,
  createProject,
  deleteDataset,
  deleteProject,
  extractDatasetClasses,
  extendMergedDataset,
  getDatasetImages,
  getProjects,
  getProjectClassImageCounts,
  imageResourceUrl,
  invalidateStorageQuotaCache,
  mergeDatasets,
  renameDataset,
  renameProject,
  updateProjectClass,
  type DatasetMergeOverlap,
  type ProjectDeleteConfirmation,
  type ProjectClassImageCount,
  type ProjectDatasetRow,
  type ProjectDatasetSourceRow,
  type ProjectRow,
} from "../api/client";
import { AppShell } from "../components/AppShell";
import { AuthenticatedImage } from "../components/AuthenticatedImage";
import { Icon } from "../components/Icon";
import { ClassColorPicker } from "../components/ClassColorPicker";
import { NewProjectDialog } from "../components/NewProjectDialog";
import { SelectMenu } from "../components/SelectMenu";
import { appHref, navigate } from "../navigation";
import {
  getDatasetSelectionSummary,
  getProjectSelectionRows,
  toggleDatasetSelection,
} from "../utils/datasetSelectionSummary";
import { getProjectSummary } from "../utils/projectSummary";
import { sortProjects, type ProjectSortOrder } from "../utils/projectSort";
import { formatBytes } from "../utils/formatBytes";

function progressPercentage(row: ProjectDatasetSourceRow) {
  if (row.active_job) {
    return row.active_job.total > 0
      ? Math.round(row.active_job.processed / row.active_job.total * 100)
      : 0;
  }
  if (row.status === "ready") {
    // ready 이후 진행률 = 라벨링 현황 (라벨 있는 이미지 / 전체 이미지)
    return row.image_count > 0
      ? Math.round(row.labeled_image_count / row.image_count * 100)
      : 0;
  }
  return 0;
}

function dateLabel(value: string) {
  return new Intl.DateTimeFormat("ko-KR", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(new Date(value));
}

async function loadThumbnailEntries(datasets: ProjectDatasetRow[]) {
  return Promise.all(datasets.map(async (dataset) => {
    if (dataset.status !== "ready" || dataset.image_count === 0) return null;
    try {
      const images = await getDatasetImages(dataset.id, 0, 1);
      return images.items[0]
        ? [dataset.id, imageResourceUrl(images.items[0].id, "thumb")] as const
        : null;
    } catch {
      return null;
    }
  }));
}

function deleteConfirmationFrom(error: unknown): ProjectDeleteConfirmation | null {
  if (!(error instanceof ApiError) || error.status !== 409) return null;
  const detail = error.detail;
  if (!detail || typeof detail !== "object") return null;

  const candidate = detail as Partial<ProjectDeleteConfirmation>;
  if (
    candidate.code !== "project-delete-confirmation-required"
    || candidate.requires_confirmation !== true
    || typeof candidate.warning !== "string"
    || !Array.isArray(candidate.datasets)
    || !candidate.datasets.every((dataset) => (
      dataset
      && typeof dataset === "object"
      && typeof dataset.id === "number"
      && typeof dataset.name === "string"
    ))
  ) {
    return null;
  }
  return candidate as ProjectDeleteConfirmation;
}

function mergeConflictFrom(error: unknown): DatasetMergeOverlap | null {
  if (!(error instanceof ApiError) || error.status !== 409) return null;
  const detail = error.detail;
  if (!detail || typeof detail !== "object") return null;

  const candidate = detail as Partial<DatasetMergeOverlap>;
  const mergedDataset = candidate.merged_dataset;
  if (
    candidate.code !== "dataset_merge_source_overlap"
    || !mergedDataset
    || typeof mergedDataset !== "object"
    || typeof mergedDataset.id !== "number"
    || !Number.isSafeInteger(mergedDataset.id)
    || mergedDataset.id <= 0
    || typeof mergedDataset.name !== "string"
    || !Array.isArray(mergedDataset.source_dataset_ids)
    || !mergedDataset.source_dataset_ids.every((id) => (
      typeof id === "number" && Number.isSafeInteger(id) && id > 0
    ))
  ) {
    return null;
  }
  return candidate as DatasetMergeOverlap;
}

type MergeActionPurpose = "train" | "merge";

interface MergeDialogTarget {
  project: ProjectRow;
  datasets: ProjectDatasetRow[];
  purpose: MergeActionPurpose;
}

interface ClassExtractionDialogTarget {
  project: ProjectRow;
  datasets: ProjectDatasetRow[];
}

interface DeleteDialogState extends ProjectDeleteConfirmation {
  project: ProjectRow;
}

export function ProjectsPage({ initialDialogOpen = false }: { initialDialogOpen?: boolean }) {
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [expanded, setExpanded] = useState<Set<number>>(new Set());
  const [search, setSearch] = useState("");
  const [filterOn, setFilterOn] = useState(false);
  const [sortOrder, setSortOrder] = useState<ProjectSortOrder>("recent");
  const [dialogOpen, setDialogOpen] = useState(initialDialogOpen);
  const [createdProject, setCreatedProject] = useState<string | null>(null);
  const [projects, setProjects] = useState<ProjectRow[]>([]);
  const [thumbs, setThumbs] = useState<Map<number, string>>(new Map());
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [loadAttempt, setLoadAttempt] = useState(0);
  const [mergeTarget, setMergeTarget] = useState<MergeDialogTarget | null>(null);
  const [mergeBusy, setMergeBusy] = useState(false);
  const [mergeError, setMergeError] = useState<string | null>(null);
  const [mergeConflict, setMergeConflict] = useState<DatasetMergeOverlap | null>(null);
  const [classExtractionTarget, setClassExtractionTarget] = useState<ClassExtractionDialogTarget | null>(null);
  const [classExtractionBusy, setClassExtractionBusy] = useState(false);
  const [classExtractionError, setClassExtractionError] = useState<string | null>(null);
  const [renameTarget, setRenameTarget] = useState<ProjectRow | null>(null);
  const [classEditTarget, setClassEditTarget] = useState<ProjectRow | null>(null);
  const [deleteConfirmation, setDeleteConfirmation] = useState<DeleteDialogState | null>(null);
  const [projectMutationError, setProjectMutationError] = useState<string | null>(null);
  const [mutatingProjectId, setMutatingProjectId] = useState<number | null>(null);
  const [deleteDatasetTarget, setDeleteDatasetTarget] = useState<ProjectDatasetRow | null>(null);
  const [renameDatasetTarget, setRenameDatasetTarget] = useState<ProjectDatasetRow | null>(null);
  const [renamingDatasetId, setRenamingDatasetId] = useState<number | null>(null);
  const [datasetMutationError, setDatasetMutationError] = useState<string | null>(null);
  const [deletingDatasetId, setDeletingDatasetId] = useState<number | null>(null);

  useEffect(() => {
    let active = true;
    let timer: number | undefined;
    const scheduleRefresh = () => {
      if (!active) return;
      if (timer !== undefined) window.clearTimeout(timer);
      timer = window.setTimeout(() => void refresh(), 2000);
    };
    const refresh = async () => {
      try {
        const response = await getProjects();
        if (!active) return;
        setProjects(response.items);
        setError(null);
        const datasets = response.items.flatMap((project) => project.datasets);
        const thumbnailEntries = await loadThumbnailEntries(datasets);
        if (active) {
          const liveDatasetIds = new Set(datasets.map((dataset) => dataset.id));
          setThumbs((current) => {
            const next = new Map(
              [...current].filter(([datasetId]) => liveDatasetIds.has(datasetId)),
            );
            thumbnailEntries.forEach((entry) => {
              if (entry !== null) next.set(entry[0], entry[1]);
            });
            return next;
          });
        }
        if (datasets.some((dataset) => dataset.active_job !== null)) {
          scheduleRefresh();
        }
      } catch (reason: unknown) {
        if (active) {
          setError(reason instanceof Error ? reason.message : "프로젝트 목록을 불러오지 못했습니다.");
          scheduleRefresh();
        }
      } finally {
        if (active) setLoading(false);
      }
    };
    void refresh();
    return () => {
      active = false;
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [loadAttempt]);

  useEffect(() => {
    if (!createdProject) return;
    const timer = window.setTimeout(() => setCreatedProject(null), 4000);
    return () => window.clearTimeout(timer);
  }, [createdProject]);

  const retryProjectLoad = () => {
    setError(null);
    setLoading(true);
    setLoadAttempt((current) => current + 1);
  };

  const visibleProjects = useMemo(() => {
    const needle = search.trim().toLowerCase();
    const filtered = projects.filter((project) => {
      const matchesName = needle.length === 0
        || project.name.toLowerCase().includes(needle)
        || project.datasets.some((dataset) => dataset.name.toLowerCase().includes(needle));
      if (!matchesName) return false;
      return !filterOn || project.datasets.some((dataset) => dataset.status === "ready");
    });
    return sortProjects(filtered, sortOrder);
  }, [filterOn, projects, search, sortOrder]);

  const allDatasets = projects.flatMap((project) => project.datasets);
  const totalImages = projects.reduce((sum, project) => sum + project.image_count, 0);
  const totalAnnotations = projects.reduce((sum, project) => sum + project.annotation_count, 0);
  const summary = getProjectSummary(projects);

  const updateSelected = (id: number, mutuallyExclusiveIds: readonly number[] = []) => {
    setSelected((current) => toggleDatasetSelection(current, id, mutuallyExclusiveIds));
  };

  const toggleProject = (projectId: number) => {
    setExpanded((current) => {
      if (current.has(projectId)) return new Set();
      return new Set([projectId]);
    });
  };

  const syncProjectsAfterDatasetMutation = async () => {
    const response = await getProjects();
    const datasets = response.items.flatMap((project) => project.datasets);
    const liveDatasetIds = new Set(
      response.items.flatMap((project) => (
        getProjectSelectionRows(project.datasets).map((dataset) => dataset.id)
      )),
    );
    const thumbnailEntries = await loadThumbnailEntries(datasets);
    setProjects(response.items);
    setSelected((current) => new Set(
      [...current].filter((datasetId) => liveDatasetIds.has(datasetId)),
    ));
    setThumbs(new Map(
      thumbnailEntries.filter((entry): entry is readonly [number, string] => entry !== null),
    ));
  };

  const openMergeDialog = (
    project: ProjectRow,
    datasets: ProjectDatasetRow[],
    purpose: MergeActionPurpose,
  ) => {
    if (datasets.length <= 1) return;
    setMergeError(null);
    setMergeConflict(null);
    setMergeTarget({ project, datasets, purpose });
  };

  const openClassExtractionDialog = (
    project: ProjectRow,
    datasets: ProjectDatasetRow[],
  ) => {
    if (datasets.length === 0 || project.classes.length === 0) return;
    setClassExtractionError(null);
    setClassExtractionTarget({ project, datasets });
  };

  const continueMergedAction = async (
    datasetId: number,
    purpose: MergeActionPurpose,
  ) => {
    try {
      await syncProjectsAfterDatasetMutation();
    } catch {
      setError("병합은 완료되었지만 프로젝트 목록을 새로고침하지 못했습니다.");
    }
    if (purpose === "train") navigate(`/datasets/${datasetId}/train`);
  };

  const submitMerge = async (name: string, targetDatasetId: number | null) => {
    const target = mergeTarget;
    if (!target || mergeBusy) return;
    setMergeBusy(true);
    setMergeError(null);
    setMergeConflict(null);
    try {
      const mergedDatasets = target.datasets.filter((dataset) => dataset.is_merged);
      const targetDataset = targetDatasetId === null
        ? null
        : mergedDatasets.find((dataset) => dataset.id === targetDatasetId) ?? null;
      const merged = targetDataset
        ? await extendMergedDataset(targetDataset.id, {
            dataset_ids: target.datasets
              .filter((dataset) => dataset.id !== targetDataset.id)
              .map((dataset) => dataset.id),
          })
        : await mergeDatasets({
            name,
            dataset_ids: target.datasets.map((dataset) => dataset.id),
          });
      invalidateStorageQuotaCache();
      await continueMergedAction(merged.id, target.purpose);
      setMergeTarget(null);
    } catch (reason: unknown) {
      const conflict = mergeConflictFrom(reason);
      if (conflict) {
        setMergeConflict(conflict);
      } else {
        setMergeError(
          reason instanceof Error ? reason.message : "데이터셋을 병합하지 못했습니다.",
        );
      }
    } finally {
      setMergeBusy(false);
    }
  };

  const useExistingMergedDataset = async () => {
    const target = mergeTarget;
    const conflict = mergeConflict;
    if (!target || !conflict || mergeBusy) return;
    setMergeBusy(true);
    setMergeError(null);
    try {
      await continueMergedAction(conflict.merged_dataset.id, target.purpose);
      setMergeTarget(null);
      setMergeConflict(null);
    } catch (reason: unknown) {
      setMergeError(
        reason instanceof Error ? reason.message : "기존 병합 데이터셋으로 진행하지 못했습니다.",
      );
    } finally {
      setMergeBusy(false);
    }
  };

  const submitClassExtraction = async (name: string, classIds: number[]) => {
    const target = classExtractionTarget;
    if (!target || classExtractionBusy || classIds.length === 0) return;
    setClassExtractionBusy(true);
    setClassExtractionError(null);
    try {
      await extractDatasetClasses({
        name,
        dataset_ids: [...new Set(target.datasets.map((dataset) => dataset.id))],
        class_ids: [...new Set(classIds)],
      });
      invalidateStorageQuotaCache();
      try {
        await syncProjectsAfterDatasetMutation();
      } catch {
        setError("분리는 완료되었지만 프로젝트 목록을 새로고침하지 못했습니다.");
      }
      setClassExtractionTarget(null);
    } catch (reason: unknown) {
      setClassExtractionError(
        reason instanceof Error ? reason.message : "데이터셋을 분리하지 못했습니다.",
      );
    } finally {
      setClassExtractionBusy(false);
    }
  };

  const removeProjectFromView = (project: ProjectRow) => {
    const datasetIds = new Set(
      getProjectSelectionRows(project.datasets).map((dataset) => dataset.id),
    );
    setProjects((current) => current.filter((candidate) => candidate.id !== project.id));
    setExpanded((current) => {
      const next = new Set(current);
      next.delete(project.id);
      return next;
    });
    setSelected((current) => new Set(
      [...current].filter((datasetId) => !datasetIds.has(datasetId)),
    ));
    setThumbs((current) => new Map(
      [...current].filter(([datasetId]) => !datasetIds.has(datasetId)),
    ));
  };

  const submitRename = async (project: ProjectRow, name: string) => {
    if (mutatingProjectId !== null) return;
    setMutatingProjectId(project.id);
    setProjectMutationError(null);
    try {
      const renamed = await renameProject(project.id, name);
      setProjects((current) => current.map((candidate) => (
        candidate.id === renamed.id ? { ...candidate, name: renamed.name } : candidate
      )));
      setRenameTarget(null);
    } catch (reason: unknown) {
      setProjectMutationError(
        reason instanceof Error ? reason.message : "프로젝트 이름을 변경하지 못했습니다.",
      );
    } finally {
      setMutatingProjectId(null);
    }
  };

  const requestProjectDelete = async (project: ProjectRow, confirmed = false) => {
    if (mutatingProjectId !== null) return;
    setMutatingProjectId(project.id);
    setProjectMutationError(null);
    setError(null);
    try {
      await deleteProject(project.id, confirmed);
      invalidateStorageQuotaCache();
      removeProjectFromView(project);
      setDeleteConfirmation(null);
    } catch (reason: unknown) {
      const confirmation = confirmed ? null : deleteConfirmationFrom(reason);
      if (confirmation) {
        setDeleteConfirmation({ project, ...confirmation });
        return;
      }
      const message = reason instanceof Error
        ? reason.message
        : "프로젝트를 삭제하지 못했습니다.";
      if (deleteConfirmation || confirmed) setProjectMutationError(message);
      else setError(message);
    } finally {
      setMutatingProjectId(null);
    }
  };

  const requestDatasetDelete = async (dataset: ProjectDatasetRow) => {
    if (deletingDatasetId !== null) return;
    setDeletingDatasetId(dataset.id);
    setDatasetMutationError(null);
    setError(null);
    try {
      await deleteDataset(dataset.id);
      invalidateStorageQuotaCache();

      setSelected((current) => {
        const next = new Set(current);
        next.delete(dataset.id);
        return next;
      });
      setThumbs((current) => {
        const next = new Map(current);
        next.delete(dataset.id);
        return next;
      });
      setDeleteDatasetTarget(null);

      try {
        const response = await getProjects();
        const datasets = response.items.flatMap((project) => project.datasets);
        const liveDatasetIds = new Set(
          response.items.flatMap((project) => (
            getProjectSelectionRows(project.datasets).map((candidate) => candidate.id)
          )),
        );
        setProjects(response.items);
        setSelected((current) => new Set(
          [...current].filter((datasetId) => liveDatasetIds.has(datasetId)),
        ));

        const thumbnailEntries = await loadThumbnailEntries(datasets);
        setThumbs((current) => {
          const next = new Map(
            [...current].filter(([datasetId]) => liveDatasetIds.has(datasetId)),
          );
          thumbnailEntries.forEach((entry) => {
            if (entry) next.set(entry[0], entry[1]);
          });
          return next;
        });
      } catch {
        setProjects((current) => current.map((project) => {
          if (project.id !== dataset.project_id) return project;
          return {
            ...project,
            datasets: project.datasets.filter((candidate) => candidate.id !== dataset.id),
            dataset_count: Math.max(0, project.dataset_count - 1),
            image_count: Math.max(0, project.image_count - dataset.image_count),
            annotation_count: Math.max(0, project.annotation_count - dataset.annotation_count),
          };
        }));
        setError("데이터셋은 삭제되었지만 프로젝트 목록을 새로고침하지 못했습니다.");
      }
    } catch (reason: unknown) {
      setDatasetMutationError(
        reason instanceof Error ? reason.message : "데이터셋을 삭제하지 못했습니다.",
      );
    } finally {
      setDeletingDatasetId(null);
    }
  };

  const requestDatasetRename = async (dataset: ProjectDatasetRow, name: string) => {
    if (renamingDatasetId !== null) return;
    setRenamingDatasetId(dataset.id);
    setDatasetMutationError(null);
    try {
      await renameDataset(dataset.id, name);
      setRenameDatasetTarget(null);
      try {
        const response = await getProjects();
        setProjects(response.items);
      } catch {
        setProjects((current) => current.map((project) => ({
          ...project,
          datasets: project.datasets.map((candidate) => (
            candidate.id === dataset.id
              ? { ...candidate, name }
              : {
                ...candidate,
                source_datasets: candidate.source_datasets.map((source) => (
                  source.id === dataset.id ? { ...source, name } : source
                )),
              }
          )),
        })));
      }
    } catch (reason: unknown) {
      setDatasetMutationError(
        reason instanceof Error ? reason.message : "데이터셋 이름을 변경하지 못했습니다.",
      );
    } finally {
      setRenamingDatasetId(null);
    }
  };

  const anyDialogOpen = dialogOpen
    || mergeTarget !== null
    || classExtractionTarget !== null
    || renameTarget !== null
    || deleteConfirmation !== null
    || deleteDatasetTarget !== null
    || renameDatasetTarget !== null
    || classEditTarget !== null;

  return (
    <>
      <div aria-hidden={anyDialogOpen || undefined} className={anyDialogOpen ? "modal-background" : undefined}>
        <AppShell active="projects" breadcrumb="프로젝트">
          <div className="page-heading-row">
            <div>
              <h1>프로젝트</h1>
              <p>데이터셋 {allDatasets.length.toLocaleString()} · 이미지 {totalImages.toLocaleString()} · 라벨 {totalAnnotations.toLocaleString()}</p>
            </div>
            <button className="btn btn-primary" type="button" onClick={() => setDialogOpen(true)}>
              <Icon name="plus" size={15} /> 새 프로젝트
            </button>
          </div>

          {createdProject ? <div className="created-toast" role="status"><span className="tag tag-ok"><span className="dot" />생성됨</span>{createdProject}</div> : null}
          {error && projects.length > 0 ? <div className="created-toast" role="alert">{error}</div> : null}

          <section className="project-stats" aria-label="프로젝트 요약">
            <div className="card stat-card"><Icon name="folder" /><span>전체</span><strong className="mono">{summary.total}</strong></div>
            <div className="card stat-card"><Icon name="user" /><span>진행 중</span><strong className="mono">{summary.inProgress}</strong></div>
            <div className="card stat-card"><Icon name="users" /><span>완료</span><strong className="mono">{summary.completed}</strong></div>
            <div className="card stat-card"><Icon name="archive" /><span>보관함</span><strong className="mono">{summary.archived}</strong></div>
          </section>

          {loading ? (
            <section className="card project-loading-state" role="status">
              프로젝트를 불러오는 중…
            </section>
          ) : error && projects.length === 0 ? (
            <section className="card project-load-error-state" role="alert">
              <Icon name="warning" size={24} />
              <strong>프로젝트 목록을 불러오지 못했습니다.</strong>
              <span>{error}</span>
              <button className="btn btn-secondary" type="button" onClick={retryProjectLoad}>
                다시 시도
              </button>
            </section>
          ) : projects.length === 0 ? (
            <section className="card project-empty-state">
              <Icon name="folder" size={24} />
              <strong>프로젝트가 없습니다.</strong>
              <span>새 프로젝트를 만들어 시작하세요.</span>
            </section>
          ) : (
            <section className="card project-list-card" aria-labelledby="project-list-title">
              <h2 className="sr-only" id="project-list-title">프로젝트 목록</h2>
              <div className="project-toolbar">
                <label className="search-field"><Icon name="search" size={15} /><span className="sr-only">프로젝트 또는 데이터셋 이름 검색</span><input value={search} placeholder="이름 검색" onChange={(event) => setSearch(event.target.value)} /></label>
                <button className={`btn btn-secondary btn-sm${filterOn ? " is-active" : ""}`} type="button" aria-pressed={filterOn} onClick={() => setFilterOn((current) => !current)}><Icon name="filter" size={14} /> 필터</button>
                <SelectMenu className="sort-select" value={sortOrder} onChange={(value) => setSortOrder(value as ProjectSortOrder)} ariaLabel="정렬" options={[{ value: "recent", label: "최근 생성순" }, { value: "name", label: "이름순" }]} />
              </div>
              <div className="project-card-list">
                {visibleProjects.map((project) => {
                  const isExpanded = expanded.has(project.id);
                  const selectedRows = getProjectSelectionRows(project.datasets)
                    .filter((dataset) => selected.has(dataset.id) && dataset.status === "ready");
                  return (
                    <ProjectRows
                      key={project.id}
                      project={project}
                      expanded={isExpanded}
                      selected={selected}
                      selectedRows={selectedRows}
                      thumbs={thumbs}
                      busy={mutatingProjectId === project.id}
                      onToggle={() => toggleProject(project.id)}
                      onSelect={updateSelected}
                      onSelectAll={(datasetIds, shouldSelect) => {
                        setSelected((current) => {
                          const next = new Set(current);
                          datasetIds.forEach((datasetId) => {
                            if (shouldSelect) next.add(datasetId);
                            else next.delete(datasetId);
                          });
                          return next;
                        });
                      }}
                      onMergeAction={(purpose) => openMergeDialog(project, selectedRows, purpose)}
                      onExtractAction={() => openClassExtractionDialog(project, selectedRows)}
                      onRename={() => {
                        setProjectMutationError(null);
                        setRenameTarget(project);
                      }}
                      onEditClasses={() => {
                        setProjectMutationError(null);
                        setClassEditTarget(project);
                      }}
                      onDelete={() => void requestProjectDelete(project)}
                      deletingDatasetId={deletingDatasetId}
                      onDeleteDataset={(dataset) => {
                        setDatasetMutationError(null);
                        setDeleteDatasetTarget(dataset);
                      }}
                      onRenameDataset={(dataset) => {
                        setDatasetMutationError(null);
                        setRenameDatasetTarget(dataset);
                      }}
                    />
                  );
                })}
                {visibleProjects.length === 0 ? (
                  <div className="project-search-empty">검색 결과가 없습니다.</div>
                ) : null}
              </div>
              <span className="sr-only" role="status">총 {visibleProjects.length}개 프로젝트</span>
            </section>
          )}
        </AppShell>
      </div>

      {dialogOpen ? <NewProjectDialog projects={projects} onClose={() => setDialogOpen(false)} onCreate={(projectInput) => {
        void createProject(projectInput).then((project) => {
          setProjects((current) => [project, ...current]);
          setExpanded(new Set([project.id]));
          setCreatedProject(project.name);
          setDialogOpen(false);
        }).catch((reason: unknown) => setError(reason instanceof Error ? reason.message : "생성하지 못했습니다."));
      }} /> : null}
      {mergeTarget ? (
        <MergeDatasetsDialog
          target={mergeTarget}
          busy={mergeBusy}
          error={mergeError}
          conflict={mergeConflict}
          onClose={() => {
            if (mergeBusy) return;
            setMergeTarget(null);
            setMergeConflict(null);
            setMergeError(null);
          }}
          onSubmit={(name, targetDatasetId) => void submitMerge(name, targetDatasetId)}
          onUseExisting={() => void useExistingMergedDataset()}
        />
      ) : null}
      {classExtractionTarget ? (
        <ClassExtractionDialog
          target={classExtractionTarget}
          busy={classExtractionBusy}
          error={classExtractionError}
          onClose={() => {
            if (classExtractionBusy) return;
            setClassExtractionTarget(null);
            setClassExtractionError(null);
          }}
          onSubmit={(name, classIds) => void submitClassExtraction(name, classIds)}
        />
      ) : null}
      {classEditTarget ? (
        <EditProjectClassesDialog
          project={classEditTarget}
          onClose={() => setClassEditTarget(null)}
          onSaved={() => {
            setClassEditTarget(null);
            void (async () => {
              try {
                const response = await getProjects();
                setProjects(response.items);
              } catch {
                setError("클래스는 저장되었지만 프로젝트 목록을 새로고침하지 못했습니다.");
              }
            })();
          }}
        />
      ) : null}
      {renameTarget ? (
        <RenameProjectDialog
          project={renameTarget}
          busy={mutatingProjectId === renameTarget.id}
          error={projectMutationError}
          onClose={() => {
            if (mutatingProjectId === null) setRenameTarget(null);
          }}
          onRename={(name) => void submitRename(renameTarget, name)}
        />
      ) : null}
      {deleteConfirmation ? (
        <DeleteProjectDialog
          deleteConfirmation={deleteConfirmation}
          busy={mutatingProjectId === deleteConfirmation.project.id}
          error={projectMutationError}
          onClose={() => {
            if (mutatingProjectId === null) setDeleteConfirmation(null);
          }}
          onConfirm={() => void requestProjectDelete(deleteConfirmation.project, true)}
        />
      ) : null}
      {renameDatasetTarget ? (
        <RenameDatasetDialog
          dataset={renameDatasetTarget}
          busy={renamingDatasetId === renameDatasetTarget.id}
          error={datasetMutationError}
          onClose={() => {
            if (renamingDatasetId === null) setRenameDatasetTarget(null);
          }}
          onRename={(name) => void requestDatasetRename(renameDatasetTarget, name)}
        />
      ) : null}
      {deleteDatasetTarget ? (
        <DeleteDatasetDialog
          dataset={deleteDatasetTarget}
          busy={deletingDatasetId === deleteDatasetTarget.id}
          error={datasetMutationError}
          onClose={() => {
            if (deletingDatasetId === null) setDeleteDatasetTarget(null);
          }}
          onConfirm={() => void requestDatasetDelete(deleteDatasetTarget)}
        />
      ) : null}
    </>
  );
}

interface ProjectRowsProps {
  project: ProjectRow;
  expanded: boolean;
  selected: Set<number>;
  selectedRows: ProjectDatasetRow[];
  thumbs: Map<number, string>;
  busy: boolean;
  onToggle: () => void;
  onSelect: (datasetId: number, mutuallyExclusiveIds?: readonly number[]) => void;
  onSelectAll: (datasetIds: number[], shouldSelect: boolean) => void;
  onMergeAction: (purpose: MergeActionPurpose) => void;
  onExtractAction: () => void;
  onRename: () => void;
  onEditClasses: () => void;
  onDelete: () => void;
  deletingDatasetId: number | null;
  onDeleteDataset: (dataset: ProjectDatasetRow) => void;
  onRenameDataset: (dataset: ProjectDatasetRow) => void;
}

interface ClassImageCountResult {
  datasetKey: string;
  items: ProjectClassImageCount[];
  error: string | null;
}

function ProjectRows({
  project,
  expanded: isExpanded,
  selected,
  selectedRows,
  thumbs,
  busy,
  onToggle,
  onSelect,
  onSelectAll,
  onMergeAction,
  onExtractAction,
  onRename,
  onEditClasses,
  onDelete,
  deletingDatasetId,
  onDeleteDataset,
  onRenameDataset,
}: ProjectRowsProps) {
  const [expandedMergedDatasets, setExpandedMergedDatasets] = useState<Set<number>>(new Set());
  const selectionSummary = getDatasetSelectionSummary(selectedRows, project.class_count);
  const selectedDatasetKey = selectedRows.map((dataset) => dataset.id).join(",");
  const [classImageCountResult, setClassImageCountResult] = useState<ClassImageCountResult>({
    datasetKey: "",
    items: [],
    error: null,
  });
  const classImageCountsReady = classImageCountResult.datasetKey === selectedDatasetKey;
  const classImageCounts = classImageCountsReady ? classImageCountResult.items : [];
  const classImageCountsError = classImageCountsReady ? classImageCountResult.error : null;
  const classImageCountsStatus = !classImageCountsReady
    ? "클래스 이미지 집계 중"
    : classImageCountsError
      ? classImageCountsError
      : classImageCounts.length > 0
        ? `클래스 이미지: ${classImageCounts.map((item) => `${item.name} ${item.image_count}장`).join(", ")}`
        : "등록된 클래스가 없습니다.";

  useEffect(() => {
    let active = true;
    if (!isExpanded || selectedDatasetKey.length === 0) {
      setClassImageCountResult({ datasetKey: "", items: [], error: null });
      return () => {
        active = false;
      };
    }

    const datasetIds = selectedDatasetKey.split(",").map(Number);
    void getProjectClassImageCounts(project.id, datasetIds)
      .then((response) => {
        if (active) {
          setClassImageCountResult({
            datasetKey: selectedDatasetKey,
            items: response.items,
            error: null,
          });
        }
      })
      .catch(() => {
        if (active) {
          setClassImageCountResult({
            datasetKey: selectedDatasetKey,
            items: [],
            error: "클래스 이미지 수를 불러오지 못했습니다.",
          });
        }
      });

    return () => {
      active = false;
    };
  }, [isExpanded, project.id, selectedDatasetKey]);

  const readyDatasetIds = project.datasets
    .filter((dataset) => (
      dataset.status === "ready"
      && !dataset.source_datasets.some((source) => selected.has(source.id))
    ))
    .map((dataset) => dataset.id);
  const selectedReadyCount = readyDatasetIds.filter((datasetId) => selected.has(datasetId)).length;
  const allReadySelected = readyDatasetIds.length > 0 && selectedReadyCount === readyDatasetIds.length;
  const someReadySelected = selectedReadyCount > 0 && !allReadySelected;

  return (
    <article
      className={`project-card${isExpanded ? " is-expanded" : ""}`}
      aria-labelledby={`project-title-${project.id}`}
    >
      <header className="project-card-header">
        <div className="project-title-lockup">
          <button
            className="project-expand"
            type="button"
            aria-expanded={isExpanded}
            aria-controls={`project-details-${project.id}`}
            aria-label={`${project.name} ${isExpanded ? "접기" : "펼치기"}`}
            onClick={onToggle}
          >
            <Icon name={isExpanded ? "chevron-down" : "chevron-right"} size={16} />
          </button>
          <span className="project-folder-tile" aria-hidden="true">
            <Icon className="project-folder-icon" name="folder-solid" size={30} />
          </span>
          <div className="project-card-identity">
            <h3 className="project-card-title" id={`project-title-${project.id}`}>{project.name}</h3>
            <p className="project-card-meta">
              <span>데이터셋 {project.dataset_count.toLocaleString()}</span>
              <span aria-hidden="true">·</span>
              <span>이미지 {project.image_count.toLocaleString()}</span>
              <span aria-hidden="true">·</span>
              <span>클래스 {project.class_count.toLocaleString()}</span>
              <span aria-hidden="true">·</span>
              <span>생성 <time dateTime={project.created_at}>{dateLabel(project.created_at)}</time></span>
            </p>
          </div>
        </div>
        {isExpanded ? (
          <span className="dataset-actions">
            {selectedRows.length === 0 ? (
              <button className="btn btn-primary btn-sm" type="button" disabled><Icon name="cpu" size={13} />AI 학습</button>
            ) : selectedRows.length === 1 ? (
              <a className="btn btn-primary btn-sm" href={appHref(`/datasets/${selectedRows[0].id}/train`)}><Icon name="cpu" size={13} />AI 학습</a>
            ) : (
              <button className="btn btn-primary btn-sm" type="button" onClick={() => onMergeAction("train")}><Icon name="cpu" size={13} />AI 학습</button>
            )}
            <button
              className="btn btn-secondary btn-sm"
              type="button"
              disabled={selectedRows.length < 2}
              onClick={() => onMergeAction("merge")}
            ><Icon name="layers" size={13} />병합</button>
            <button
              className="btn btn-secondary btn-sm"
              type="button"
              disabled={selectedRows.length === 0 || project.classes.length === 0}
              onClick={onExtractAction}
            ><Icon name="filter" size={13} />분리</button>
            <a className="btn btn-secondary btn-sm" href={appHref(`/upload?project_id=${project.id}`)}><Icon name="plus" size={13} />데이터셋</a>
          </span>
        ) : null}
        <ProjectRowMenu
          projectId={project.id}
          projectName={project.name}
          busy={busy}
          onRename={onRename}
          onEditClasses={onEditClasses}
          onDelete={onDelete}
        />
      </header>
      {isExpanded ? (
        <div
          id={`project-details-${project.id}`}
          className="project-card-body"
          aria-labelledby={`project-title-${project.id}`}
        >
          <div className="project-chip-strip">
            {selectionSummary ? (
              <>
                <span className="sr-only" role="status">
                  선택 합계: 데이터셋 {selectionSummary.datasetCount}개, 이미지 {selectionSummary.imageCount}개, 클래스 {selectionSummary.classCount}개. {classImageCountsStatus}
                </span>
                <div className="project-class-image-content">
                  <div className="project-class-image-list" aria-label="선택한 데이터셋의 클래스 이미지 수 및 전체 이미지 수">
                    {classImageCountsError ? (
                      <span className="project-class-image-message is-error">{classImageCountsError}</span>
                    ) : !classImageCountsReady ? (
                      <span className="project-class-image-message">집계 중…</span>
                    ) : classImageCounts.length > 0 ? (
                      classImageCounts.map((item) => (
                        <span className="class-image-count" key={item.class_id}>
                          <i aria-hidden="true" style={{ background: item.color }} />
                          <span>{item.name}</span>
                          <strong className="mono">{item.image_count.toLocaleString()}장</strong>
                        </span>
                      ))
                    ) : (
                      <span className="project-class-image-message">등록된 클래스가 없습니다.</span>
                    )}
                    <span className="class-image-count is-total">
                      <span>전체 이미지</span>
                      <strong className="mono">{selectionSummary.imageCount.toLocaleString()}장</strong>
                    </span>
                  </div>
                </div>
              </>
            ) : null}
          </div>
          {project.datasets.length === 0 ? (
            <div className="project-empty-child">데이터셋이 없습니다. 위의 ‘데이터셋’ 버튼으로 첫 데이터셋을 추가하세요.</div>
          ) : (
            <table className="project-dataset-table">
              <caption className="sr-only">{project.name} 데이터셋 목록</caption>
              <colgroup>
                <col className="dataset-select-column" />
                <col />
                <col className="dataset-storage-column" />
                <col className="dataset-progress-column" />
                <col className="dataset-menu-column" />
              </colgroup>
              <thead>
                <tr>
                  <th className="dataset-select-cell" scope="col">
                    <button
                      className={`checkbox${allReadySelected ? " is-on" : ""}`}
                      type="button"
                      role="checkbox"
                      disabled={readyDatasetIds.length === 0}
                      aria-checked={someReadySelected ? "mixed" : allReadySelected}
                      aria-label={`${project.name} 데이터셋 전체 선택`}
                      onClick={() => onSelectAll(readyDatasetIds, !allReadySelected)}
                    >
                      {allReadySelected ? <Icon name="check" size={10} /> : null}
                    </button>
                  </th>
                  <th scope="col">데이터셋</th>
                  <th scope="col">용량</th>
                  <th scope="col">진행률</th>
                  <th scope="col"><span className="sr-only">작업</span></th>
                </tr>
              </thead>
              <tbody>
                {project.datasets.map((dataset) => {
                  const progress = progressPercentage(dataset);
                  const ready = dataset.status === "ready";
                  const hasMergeSources = dataset.is_merged && dataset.source_datasets.length > 0;
                  const selectedSourceIds = dataset.source_datasets
                    .filter((source) => selected.has(source.id))
                    .map((source) => source.id);
                  const parentSelectionBlocked = selectedSourceIds.length > 0;
                  const parentSelectionReasonId = `merged-dataset-selection-reason-${dataset.id}`;
                  const mergeSourcesExpanded = hasMergeSources && expandedMergedDatasets.has(dataset.id);
                  const mergeSourcesId = `merged-dataset-sources-${dataset.id}`;
                  return (
                    <Fragment key={dataset.id}>
                      <tr className={`dataset-row${dataset.is_merged ? " is-merged" : ""}`}>
                        <td className="dataset-select-cell">
                          <span
                            className="dataset-selection-control"
                            title={parentSelectionBlocked ? "원본 데이터셋이 선택되어 병합 데이터셋을 선택할 수 없습니다." : undefined}
                          >
                            <button
                              className={`checkbox${selected.has(dataset.id) ? " is-on" : ""}`}
                              type="button"
                              role="checkbox"
                              disabled={!ready || parentSelectionBlocked}
                              aria-checked={selected.has(dataset.id)}
                              aria-describedby={parentSelectionBlocked ? parentSelectionReasonId : undefined}
                              aria-label={`${dataset.name} 선택`}
                              onClick={() => onSelect(
                                dataset.id,
                                dataset.source_datasets.map((source) => source.id),
                              )}
                            >
                              {selected.has(dataset.id) ? <Icon name="check" size={10} /> : null}
                            </button>
                          </span>
                          {parentSelectionBlocked ? (
                            <span className="sr-only" id={parentSelectionReasonId}>
                              원본 데이터셋이 선택되어 병합 데이터셋을 선택할 수 없습니다.
                            </span>
                          ) : null}
                        </td>
                        <td>
                          <div className="dataset-identity">
                            {hasMergeSources ? (
                              <button
                                className="merged-dataset-toggle"
                                id={`merged-dataset-toggle-${dataset.id}`}
                                type="button"
                                aria-expanded={mergeSourcesExpanded}
                                aria-controls={mergeSourcesId}
                                aria-label={`${dataset.name} 원본 데이터셋 ${mergeSourcesExpanded ? "접기" : "펼치기"}`}
                                onClick={() => setExpandedMergedDatasets((current) => {
                                  const next = new Set(current);
                                  if (next.has(dataset.id)) next.delete(dataset.id);
                                  else next.add(dataset.id);
                                  return next;
                                })}
                              >
                                <Icon name={mergeSourcesExpanded ? "chevron-down" : "chevron-right"} size={16} />
                              </button>
                            ) : <span className="merged-dataset-toggle-spacer" aria-hidden="true">−</span>}
                            {ready ? <a className="dataset-thumb" href={appHref(`/datasets/${dataset.id}/viewer`)}>{thumbs.get(dataset.id) ? <AuthenticatedImage resourcePath={thumbs.get(dataset.id)} alt="" /> : null}</a> : <span className="dataset-thumb" />}
                            {ready ? <a className="dataset-name" href={appHref(`/datasets/${dataset.id}/viewer`)}><strong>{dataset.name}{dataset.is_merged ? <span className="merged-dataset-badge">병합</span> : null}</strong><span>이미지 {dataset.image_count.toLocaleString()} · 라벨 {dataset.annotation_count.toLocaleString()}</span></a> : <span className="dataset-name"><strong>{dataset.name}{dataset.is_merged ? <span className="merged-dataset-badge">병합</span> : null}</strong><span>이미지 {dataset.image_count.toLocaleString()} · 라벨 {dataset.annotation_count.toLocaleString()}</span></span>}
                          </div>
                        </td>
                        <td>
                          <span className="dataset-storage-usage">
                            <span><small>참조</small><strong className="mono">{formatBytes(dataset.storage_bytes)}</strong></span>
                            <span><small>실제 점유</small><strong className="mono">{formatBytes(dataset.physical_storage_bytes)}</strong></span>
                          </span>
                        </td>
                        <td><span className="dataset-progress"><span className="dataset-progress-track"><span className="bar"><i style={{ width: `${progress}%` }} /></span><span className="dataset-progress-fraction mono">{dataset.labeled_image_count.toLocaleString()}/{dataset.image_count.toLocaleString()}</span></span><span className="mono">{progress}%</span></span></td>
                        <td className="dataset-menu-cell">
                          <DatasetRowMenu
                            datasetId={dataset.id}
                            datasetName={dataset.name}
                            busy={deletingDatasetId === dataset.id}
                            onRename={() => onRenameDataset(dataset)}
                            onDelete={() => onDeleteDataset(dataset)}
                          />
                        </td>
                      </tr>
                      {mergeSourcesExpanded ? (
                        <tr className="merged-source-row">
                          <td colSpan={5}>
                            <table
                              className="merged-source-table"
                              id={mergeSourcesId}
                              aria-labelledby={`merged-dataset-toggle-${dataset.id}`}
                            >
                              <caption className="sr-only">{dataset.name} 원본 데이터셋 목록</caption>
                              <colgroup>
                                <col className="merged-source-select-column" />
                                <col />
                                <col className="dataset-storage-column" />
                                <col className="dataset-progress-column" />
                                <col className="dataset-menu-column" />
                              </colgroup>
                              <tbody>
                                {dataset.source_datasets.map((source) => {
                                  const sourceSelectionBlocked = selected.has(dataset.id);
                                  const sourceSelectionReasonId = `source-dataset-selection-reason-${source.id}`;
                                  return (
                                  <tr key={source.id}>
                                    <td className="merged-source-select-cell">
                                      <span
                                        className="dataset-selection-control"
                                        title={sourceSelectionBlocked ? "병합 데이터셋이 선택되어 원본 데이터셋을 선택할 수 없습니다." : undefined}
                                      >
                                        <button
                                          className={`checkbox${selected.has(source.id) ? " is-on" : ""}`}
                                          type="button"
                                          role="checkbox"
                                          disabled={source.status !== "ready" || sourceSelectionBlocked}
                                          aria-checked={selected.has(source.id)}
                                          aria-describedby={sourceSelectionBlocked ? sourceSelectionReasonId : undefined}
                                          aria-label={`${source.name} 선택`}
                                          onClick={() => onSelect(source.id, [dataset.id])}
                                        >
                                          {selected.has(source.id) ? <Icon name="check" size={10} /> : null}
                                        </button>
                                      </span>
                                      {sourceSelectionBlocked ? (
                                        <span className="sr-only" id={sourceSelectionReasonId}>
                                          병합 데이터셋이 선택되어 원본 데이터셋을 선택할 수 없습니다.
                                        </span>
                                      ) : null}
                                    </td>
                                    <td>
                                      <a className="merged-source-name" href={appHref(`/datasets/${source.id}/viewer`)}>
                                        <strong>{source.name}</strong>
                                        <span>이미지 {source.image_count.toLocaleString()} · 라벨 {source.annotation_count.toLocaleString()}</span>
                                      </a>
                                    </td>
                                    <td>
                                      <span className="dataset-storage-usage">
                                        <span><small>참조</small><strong className="mono">{formatBytes(source.storage_bytes)}</strong></span>
                                        <span><small>실제 점유</small><strong className="mono">{formatBytes(source.physical_storage_bytes)}</strong></span>
                                      </span>
                                    </td>
                                    <td>
                                      {(() => {
                                        const sourceProgress = progressPercentage(source);
                                        return (
                                          <span className="dataset-progress"><span className="dataset-progress-track"><span className="bar"><i style={{ width: `${sourceProgress}%` }} /></span><span className="dataset-progress-fraction mono">{source.labeled_image_count.toLocaleString()}/{source.image_count.toLocaleString()}</span></span><span className="mono">{sourceProgress}%</span></span>
                                        );
                                      })()}
                                    </td>
                                    <td className="dataset-menu-cell">
                                      <DatasetRowMenu
                                        datasetId={source.id}
                                        datasetName={source.name}
                                        busy={deletingDatasetId === source.id}
                                        onRename={() => onRenameDataset({ ...source, source_datasets: [] })}
                                        onDelete={() => onDeleteDataset({ ...source, source_datasets: [] })}
                                      />
                                    </td>
                                  </tr>
                                  );
                                })}
                              </tbody>
                            </table>
                          </td>
                        </tr>
                      ) : null}
                    </Fragment>
                  );
                })}
              </tbody>
            </table>
          )}
        </div>
      ) : null}
    </article>
  );
}

interface ProjectRowMenuProps {
  projectId: number;
  projectName: string;
  busy: boolean;
  onRename: () => void;
  onEditClasses: () => void;
  onDelete: () => void;
}

function ProjectRowMenu({
  projectId,
  projectName,
  busy,
  onRename,
  onEditClasses,
  onDelete,
}: ProjectRowMenuProps) {
  const [open, setOpen] = useState(false);
  const menuRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const firstItemRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (!open) return;
    firstItemRef.current?.focus();
    const closeOnOutsidePointer = (event: PointerEvent) => {
      if (!menuRef.current?.contains(event.target as Node)) setOpen(false);
    };
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      setOpen(false);
      triggerRef.current?.focus();
    };
    document.addEventListener("pointerdown", closeOnOutsidePointer);
    document.addEventListener("keydown", closeOnEscape);
    return () => {
      document.removeEventListener("pointerdown", closeOnOutsidePointer);
      document.removeEventListener("keydown", closeOnEscape);
    };
  }, [open]);

  return (
    <div className="project-row-menu" ref={menuRef}>
      <button
        className="btn btn-ghost btn-sm icon-button"
        type="button"
        aria-label={`${projectName} 메뉴`}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls={`project-row-menu-${projectId}`}
        disabled={busy}
        ref={triggerRef}
        onClick={() => setOpen((current) => !current)}
      >
        <Icon name="more" size={14} />
      </button>
      {open ? (
        <div
          className="project-row-menu-popover"
          id={`project-row-menu-${projectId}`}
          role="menu"
          aria-label={`${projectName} 프로젝트 작업`}
        >
          <button
            className="project-row-menu-item"
            type="button"
            role="menuitem"
            ref={firstItemRef}
            onClick={() => {
              setOpen(false);
              onRename();
            }}
          >이름 변경</button>
          <button
            className="project-row-menu-item"
            type="button"
            role="menuitem"
            onClick={() => {
              setOpen(false);
              onEditClasses();
            }}
          >클래스 수정</button>
          <button
            className="project-row-menu-item is-danger"
            type="button"
            role="menuitem"
            onClick={() => {
              setOpen(false);
              onDelete();
            }}
          >삭제</button>
        </div>
      ) : null}
    </div>
  );
}

interface DatasetRowMenuProps {
  onRename: () => void;
  datasetId: number;
  datasetName: string;
  busy: boolean;
  onDelete: () => void;
}

function DatasetRowMenu({
  datasetId,
  datasetName,
  busy,
  onRename,
  onDelete,
}: DatasetRowMenuProps) {
  const [open, setOpen] = useState(false);
  const menuRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const deleteItemRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (!open) return;
    deleteItemRef.current?.focus();
    const closeOnOutsidePointer = (event: PointerEvent) => {
      if (!menuRef.current?.contains(event.target as Node)) setOpen(false);
    };
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      setOpen(false);
      triggerRef.current?.focus();
    };
    document.addEventListener("pointerdown", closeOnOutsidePointer);
    document.addEventListener("keydown", closeOnEscape);
    return () => {
      document.removeEventListener("pointerdown", closeOnOutsidePointer);
      document.removeEventListener("keydown", closeOnEscape);
    };
  }, [open]);

  return (
    <div className="project-row-menu dataset-row-menu" ref={menuRef}>
      <button
        className="btn btn-ghost btn-sm icon-button"
        type="button"
        aria-label={`${datasetName} 메뉴`}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls={`dataset-row-menu-${datasetId}`}
        disabled={busy}
        ref={triggerRef}
        onClick={() => setOpen((current) => !current)}
      >
        <Icon name="more" size={14} />
      </button>
      {open ? (
        <div
          className="project-row-menu-popover"
          id={`dataset-row-menu-${datasetId}`}
          role="menu"
          aria-label={`${datasetName} 데이터셋 작업`}
        >
          <button
            className="project-row-menu-item"
            type="button"
            role="menuitem"
            onClick={() => {
              setOpen(false);
              onRename();
            }}
          >이름 변경</button>
          <button
            className="project-row-menu-item is-danger"
            type="button"
            role="menuitem"
            ref={deleteItemRef}
            onClick={() => {
              setOpen(false);
              onDelete();
            }}
          >삭제</button>
        </div>
      ) : null}
    </div>
  );
}

interface MergeDatasetsDialogProps {
  target: MergeDialogTarget;
  busy: boolean;
  error: string | null;
  conflict: DatasetMergeOverlap | null;
  onClose: () => void;
  onSubmit: (name: string, targetDatasetId: number | null) => void;
  onUseExisting: () => void;
}

function MergeDatasetsDialog({
  target,
  busy,
  error,
  conflict,
  onClose,
  onSubmit,
  onUseExisting,
}: MergeDatasetsDialogProps) {
  const [name, setName] = useState("");
  const mergedDatasets = target.datasets.filter((dataset) => dataset.is_merged);
  const [targetDatasetId, setTargetDatasetId] = useState<number | null>(
    mergedDatasets[0]?.id ?? null,
  );
  const inputRef = useRef<HTMLInputElement>(null);
  const purposeLabel = target.purpose === "train" ? "학습" : "병합";
  const isMergeOnly = target.purpose === "merge";
  const normalizedName = name.trim();
  const targetDataset = mergedDatasets.find((dataset) => dataset.id === targetDatasetId) ?? null;
  const extendsExisting = mergedDatasets.length > 0;

  useEffect(() => {
    if (!extendsExisting) inputRef.current?.focus();
  }, [extendsExisting]);

  useEffect(() => {
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !busy) onClose();
    };
    window.addEventListener("keydown", closeOnEscape);
    return () => window.removeEventListener("keydown", closeOnEscape);
  }, [busy, onClose]);

  const datasetNameById = new Map(
    target.project.datasets.map((dataset) => [dataset.id, dataset.name]),
  );

  return (
    <div
      className="dialog-backdrop"
      role="presentation"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget && !busy) onClose();
      }}
    >
      <section
        className="dialog project-action-dialog merge-datasets-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="merge-datasets-title"
        aria-describedby="merge-datasets-description"
      >
        <button
          className="btn btn-ghost btn-sm dialog-close"
          type="button"
          aria-label="닫기"
          disabled={busy}
          onClick={onClose}
        ><Icon name="x" size={16} /></button>
        <h2 className="dialog-title" id="merge-datasets-title">
          {mergedDatasets.length === 0
            ? isMergeOnly ? "데이터셋 병합" : `${purposeLabel}용 데이터셋 병합`
            : mergedDatasets.length === 1
              ? "병합 데이터셋에 추가"
              : "병합 데이터셋 통합"}
        </h2>
        <p className="merge-dataset-copy" id="merge-datasets-description">
          {mergedDatasets.length === 0
            ? <>선택한 데이터셋 {target.datasets.length.toLocaleString()}개를 {isMergeOnly ? "하나로 합칩니다." : `하나로 합친 뒤 ${purposeLabel}합니다.`} 원본 데이터셋은 변경되지 않습니다.</>
            : <>선택한 데이터셋을 기존 병합본에 추가합니다. 대상 병합본의 현재 이미지와 수정 라벨은 그대로 유지됩니다.</>}
        </p>
        <ul className="merge-dataset-list" aria-label="병합할 데이터셋">
          {target.datasets.map((dataset) => (
            <li key={dataset.id}>
              <span>{dataset.name}</span>
              <span className="mono">이미지 {dataset.image_count.toLocaleString()}개</span>
            </li>
          ))}
        </ul>
        <form
          onSubmit={(event) => {
            event.preventDefault();
            if (!conflict && (extendsExisting ? targetDatasetId !== null : Boolean(normalizedName))) {
              onSubmit(normalizedName, targetDatasetId);
            }
          }}
        >
          {mergedDatasets.length === 0 ? (
            <div className="field merge-name-field">
              <label htmlFor="merge-dataset-name">병합 데이터셋 이름</label>
              <input
                className={`input${error ? " is-error" : ""}`}
                id="merge-dataset-name"
                ref={inputRef}
                value={name}
                maxLength={255}
                placeholder="병합 데이터셋 이름 입력"
                disabled={busy || conflict !== null}
                onChange={(event) => setName(event.target.value)}
              />
            </div>
          ) : mergedDatasets.length === 1 && targetDataset ? (
            <p className="merge-target-copy"><strong>{targetDataset.name}</strong>에 추가합니다.</p>
          ) : (
            <fieldset className="merge-target-fieldset">
              <legend>병합 결과를 유지할 대상</legend>
              <div className="merge-target-options">
                {mergedDatasets.map((dataset) => (
                  <label className="merge-target-option" key={dataset.id}>
                    <input
                      type="radio"
                      name="merge-target-dataset"
                      value={dataset.id}
                      checked={targetDatasetId === dataset.id}
                      disabled={busy}
                      onChange={() => setTargetDatasetId(dataset.id)}
                    />
                    <span><strong>{dataset.name}</strong><small>이미지 {dataset.image_count.toLocaleString()}개</small></span>
                  </label>
                ))}
              </div>
              <p className="merge-target-warning">나머지 병합 데이터셋은 대상에 통합된 뒤 삭제됩니다.</p>
            </fieldset>
          )}

          {conflict ? (
            <div className="merge-conflict-notice" role="alert">
              <strong>선택한 데이터셋 일부가 이미 다른 병합에 포함되어 있습니다.</strong>
              <p>
                기존 병합 <strong>{conflict.merged_dataset.name}</strong>으로 진행하거나 취소해
                선택을 다시 확인해 주세요.
              </p>
              <dl>
                <div><dt>기존 병합 ID</dt><dd className="mono">#{conflict.merged_dataset.id}</dd></div>
                <div>
                  <dt>원본 데이터셋</dt>
                  <dd>{conflict.merged_dataset.source_dataset_ids.map((id) => (
                    datasetNameById.get(id) ?? `데이터셋 #${id}`
                  )).join(", ")}</dd>
                </div>
              </dl>
            </div>
          ) : null}
          {error ? <div className="error-text project-dialog-error" role="alert">{error}</div> : null}

          <div className="dialog-actions">
            <button className="btn btn-secondary" type="button" disabled={busy} onClick={onClose}>취소</button>
            {conflict ? (
              <button className="btn btn-primary" type="button" disabled={busy} onClick={onUseExisting}>
                {busy ? "진행 중…" : "기존 병합으로 진행"}
              </button>
            ) : (
              <button className="btn btn-primary" type="submit" disabled={busy || (extendsExisting ? targetDatasetId === null : !normalizedName)}>
                {busy
                  ? "병합 중…"
                  : extendsExisting
                    ? target.purpose === "train" ? "통합 후 학습" : "추가 및 통합"
                    : isMergeOnly ? "병합" : `병합 후 ${purposeLabel}`}
              </button>
            )}
          </div>
        </form>
      </section>
    </div>
  );
}

interface ClassExtractionDialogProps {
  target: ClassExtractionDialogTarget;
  busy: boolean;
  error: string | null;
  onClose: () => void;
  onSubmit: (name: string, classIds: number[]) => void;
}

function ClassExtractionDialog({
  target,
  busy,
  error,
  onClose,
  onSubmit,
}: ClassExtractionDialogProps) {
  const [name, setName] = useState("");
  const [selectedClassIds, setSelectedClassIds] = useState<Set<number>>(new Set());
  const [classImageCounts, setClassImageCounts] = useState<ProjectClassImageCount[] | null>(null);
  const [classImageCountError, setClassImageCountError] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const normalizedName = name.trim();
  const sourceDatasetKey = target.datasets.map((dataset) => dataset.id).join(",");
  const classImageCountById = new Map(
    (classImageCounts ?? []).map((item) => [item.class_id, item.image_count]),
  );

  useEffect(() => {
    inputRef.current?.focus();
  }, []);

  useEffect(() => {
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !busy) onClose();
    };
    window.addEventListener("keydown", closeOnEscape);
    return () => window.removeEventListener("keydown", closeOnEscape);
  }, [busy, onClose]);

  useEffect(() => {
    let active = true;
    const sourceDatasetIds = sourceDatasetKey.split(",").map(Number);
    setClassImageCounts(null);
    setClassImageCountError(null);
    void getProjectClassImageCounts(target.project.id, sourceDatasetIds)
      .then((response) => {
        if (active) setClassImageCounts(response.items);
      })
      .catch(() => {
        if (active) {
          setClassImageCounts([]);
          setClassImageCountError("이미지 수를 불러오지 못했습니다.");
        }
      });
    return () => {
      active = false;
    };
  }, [sourceDatasetKey, target.project.id]);

  const toggleClass = (classId: number) => {
    setSelectedClassIds((current) => {
      const next = new Set(current);
      if (next.has(classId)) next.delete(classId);
      else next.add(classId);
      return next;
    });
  };

  const selectedClassIdsInProjectOrder = target.project.classes
    .filter((projectClass) => selectedClassIds.has(projectClass.class_id))
    .map((projectClass) => projectClass.class_id);

  return (
    <div
      className="dialog-backdrop"
      role="presentation"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget && !busy) onClose();
      }}
    >
      <section
        className="dialog project-action-dialog class-extraction-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="class-extraction-title"
        aria-describedby="class-extraction-description"
      >
        <button
          className="btn btn-ghost btn-sm dialog-close"
          type="button"
          aria-label="닫기"
          disabled={busy}
          onClick={onClose}
        ><Icon name="x" size={16} /></button>
        <h2 className="dialog-title" id="class-extraction-title">클래스 분리</h2>
        <p className="merge-dataset-copy" id="class-extraction-description">
          선택한 데이터셋을 원본으로 사용해 고른 클래스의 이미지와 라벨로 새 데이터셋을 만듭니다. 선택한 클래스의 라벨이 하나도 없는 이미지는 제외되며, 포함된 이미지에서도 선택하지 않은 클래스의 라벨은 제외됩니다. 원본 데이터셋은 변경되지 않습니다.
        </p>
        <ul className="class-extraction-source-list" aria-label="분리 원본 데이터셋">
          {target.datasets.map((dataset) => (
            <li key={dataset.id}>
              <span>{dataset.name}</span>
              <span className="mono">이미지 {dataset.image_count.toLocaleString()}개</span>
            </li>
          ))}
        </ul>
        <form
          onSubmit={(event) => {
            event.preventDefault();
            if (normalizedName && selectedClassIdsInProjectOrder.length > 0) {
              onSubmit(normalizedName, selectedClassIdsInProjectOrder);
            }
          }}
        >
          <div className="field class-extraction-name-field">
            <label htmlFor="class-extraction-name">새 데이터셋 이름</label>
            <input
              className={`input${error ? " is-error" : ""}`}
              id="class-extraction-name"
              ref={inputRef}
              value={name}
              maxLength={255}
              placeholder="새 데이터셋 이름 입력"
              disabled={busy}
              onChange={(event) => setName(event.target.value)}
            />
          </div>
          <fieldset className="class-extraction-fieldset" disabled={busy}>
            <legend>분리할 클래스</legend>
            <div className="class-extraction-class-list">
              {target.project.classes.map((projectClass) => (
                <label className="class-input-option class-extraction-class-option" key={projectClass.class_id}>
                  <input
                    type="checkbox"
                    name="class-extraction-classes"
                    value={projectClass.class_id}
                    checked={selectedClassIds.has(projectClass.class_id)}
                    onChange={() => toggleClass(projectClass.class_id)}
                  />
                  <i aria-hidden="true" style={{ background: projectClass.color }} />
                  <span className="class-extraction-class-name">{projectClass.name}</span>
                  <span className="class-extraction-class-image-count mono">
                    {classImageCounts === null
                      ? "집계 중…"
                      : classImageCountError
                        ? "이미지 —"
                        : `이미지 ${(classImageCountById.get(projectClass.class_id) ?? 0).toLocaleString()}장`}
                  </span>
                </label>
              ))}
            </div>
            <span className="class-extraction-count" role="status">
              선택한 클래스 {selectedClassIds.size.toLocaleString()}개
            </span>
            {classImageCountError ? (
              <span className="class-extraction-count is-error" role="status">
                {classImageCountError}
              </span>
            ) : null}
          </fieldset>
          {error ? <div className="error-text project-dialog-error" role="alert">{error}</div> : null}
          <div className="dialog-actions">
            <button className="btn btn-secondary" type="button" disabled={busy} onClick={onClose}>취소</button>
            <button
              className="btn btn-primary"
              type="submit"
              disabled={busy || !normalizedName || selectedClassIds.size === 0}
            >{busy ? "분리 중…" : "분리"}</button>
          </div>
        </form>
      </section>
    </div>
  );
}

interface RenameProjectDialogProps {
  project: ProjectRow;
  busy: boolean;
  error: string | null;
  onClose: () => void;
  onRename: (name: string) => void;
}

interface EditProjectClassesDialogProps {
  project: ProjectRow;
  onClose: () => void;
  onSaved: () => void;
}

function EditProjectClassesDialog({
  project,
  onClose,
  onSaved,
}: EditProjectClassesDialogProps) {
  const [rows, setRows] = useState(
    project.classes.map((item) => ({ ...item })),
  );
  const [colorClassId, setColorClassId] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !busy) onClose();
    };
    window.addEventListener("keydown", closeOnEscape);
    return () => window.removeEventListener("keydown", closeOnEscape);
  }, [busy, onClose]);

  const findOriginal = (classId: number) =>
    project.classes.find((item) => item.class_id === classId);
  const changedRows = rows.filter((row) => {
    const original = findOriginal(row.class_id);
    if (!original) return false;
    return original.name !== row.name.trim() || original.color !== row.color;
  });
  const hasBlankName = rows.some((row) => row.name.trim().length === 0);

  const save = async () => {
    if (busy || changedRows.length === 0 || hasBlankName) return;
    setBusy(true);
    setError(null);
    try {
      for (const row of changedRows) {
        const original = findOriginal(row.class_id);
        await updateProjectClass(project.id, row.class_id, {
          ...(original && original.name !== row.name.trim() ? { name: row.name.trim() } : {}),
          ...(original && original.color !== row.color ? { color: row.color } : {}),
        });
      }
      onSaved();
    } catch (reason: unknown) {
      setError(reason instanceof Error ? reason.message : "클래스를 저장하지 못했습니다.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div
      className="dialog-backdrop"
      role="presentation"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget && !busy) onClose();
      }}
    >
      <section
        className="dialog project-action-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="edit-project-classes-title"
      >
        <button
          className="btn btn-ghost btn-sm dialog-close"
          type="button"
          aria-label="닫기"
          disabled={busy}
          onClick={onClose}
        ><Icon name="x" size={16} /></button>
        <h2 className="dialog-title" id="edit-project-classes-title">클래스 수정</h2>
        <p className="project-delete-copy">
          이름을 바꾸면 프로젝트의 모든 데이터셋에 함께 적용됩니다.
        </p>
        <div className="class-editor-list">
          {rows.map((row) => (
            <div className="class-editor-row" key={row.class_id}>
              <div className="class-color-cell">
                <button
                  className="class-swatch"
                  style={{ background: row.color }}
                  type="button"
                  aria-label={`${row.name || `${row.class_id}번`} 클래스 색 변경`}
                  aria-haspopup="dialog"
                  aria-expanded={colorClassId === row.class_id}
                  disabled={busy}
                  onClick={() => setColorClassId((current) => current === row.class_id ? null : row.class_id)}
                />
                {colorClassId === row.class_id ? (
                  <ClassColorPicker
                    className={row.name || `${row.class_id}번 클래스`}
                    color={row.color}
                    top={34}
                    placement="start"
                    onChange={(color) =>
                      setRows((current) =>
                        current.map((candidate) =>
                          candidate.class_id === row.class_id ? { ...candidate, color } : candidate,
                        ),
                      )
                    }
                    onClose={() => setColorClassId(null)}
                  />
                ) : null}
              </div>
              <input
                className="input"
                value={row.name}
                maxLength={255}
                disabled={busy}
                aria-label={`${row.class_id}번 클래스 이름`}
                onChange={(event) =>
                  setRows((current) =>
                    current.map((candidate) =>
                      candidate.class_id === row.class_id
                        ? { ...candidate, name: event.target.value }
                        : candidate,
                    ),
                  )
                }
              />
            </div>
          ))}
        </div>
        {error ? <div className="error-text project-dialog-error" role="alert">{error}</div> : null}
        <div className="dialog-actions">
          <button className="btn btn-secondary" type="button" disabled={busy} onClick={onClose}>취소</button>
          <button
            className="btn btn-primary"
            type="button"
            disabled={busy || changedRows.length === 0 || hasBlankName}
            onClick={() => void save()}
          >{busy ? "저장 중…" : "저장"}</button>
        </div>
      </section>
    </div>
  );
}

function RenameProjectDialog({
  project,
  busy,
  error,
  onClose,
  onRename,
}: RenameProjectDialogProps) {
  const [name, setName] = useState(project.name);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    inputRef.current?.focus();
    inputRef.current?.select();
  }, []);

  useEffect(() => {
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !busy) onClose();
    };
    window.addEventListener("keydown", closeOnEscape);
    return () => window.removeEventListener("keydown", closeOnEscape);
  }, [busy, onClose]);

  const normalizedName = name.trim();
  return (
    <div
      className="dialog-backdrop"
      role="presentation"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget && !busy) onClose();
      }}
    >
      <section
        className="dialog project-action-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="rename-project-title"
      >
        <button
          className="btn btn-ghost btn-sm dialog-close"
          type="button"
          aria-label="닫기"
          disabled={busy}
          onClick={onClose}
        ><Icon name="x" size={16} /></button>
        <h2 className="dialog-title" id="rename-project-title">프로젝트 이름 변경</h2>
        <form
          onSubmit={(event) => {
            event.preventDefault();
            if (normalizedName && normalizedName !== project.name) onRename(normalizedName);
          }}
        >
          <div className="field">
            <label htmlFor="rename-project-name">프로젝트명</label>
            <input
              className={`input${error ? " is-error" : ""}`}
              id="rename-project-name"
              ref={inputRef}
              value={name}
              maxLength={255}
              disabled={busy}
              onChange={(event) => setName(event.target.value)}
            />
            {error ? <div className="error-text" role="alert">{error}</div> : null}
          </div>
          <div className="dialog-actions">
            <button className="btn btn-secondary" type="button" disabled={busy} onClick={onClose}>취소</button>
            <button className="btn btn-primary" type="submit" disabled={busy || !normalizedName || normalizedName === project.name}>
              {busy ? "변경 중…" : "변경"}
            </button>
          </div>
        </form>
      </section>
    </div>
  );
}

interface DeleteProjectDialogProps {
  deleteConfirmation: DeleteDialogState;
  busy: boolean;
  error: string | null;
  onClose: () => void;
  onConfirm: () => void;
}

function DeleteProjectDialog({
  deleteConfirmation,
  busy,
  error,
  onClose,
  onConfirm,
}: DeleteProjectDialogProps) {
  const cancelRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    cancelRef.current?.focus();
  }, []);

  useEffect(() => {
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !busy) onClose();
    };
    window.addEventListener("keydown", closeOnEscape);
    return () => window.removeEventListener("keydown", closeOnEscape);
  }, [busy, onClose]);

  return (
    <div
      className="dialog-backdrop"
      role="presentation"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget && !busy) onClose();
      }}
    >
      <section
        className="dialog project-action-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="delete-project-title"
        aria-describedby="delete-project-warning"
      >
        <button
          className="btn btn-ghost btn-sm dialog-close"
          type="button"
          aria-label="닫기"
          disabled={busy}
          onClick={onClose}
        ><Icon name="x" size={16} /></button>
        <h2 className="dialog-title" id="delete-project-title">프로젝트 삭제</h2>
        <div className="project-delete-warning" id="delete-project-warning">
          <Icon name="warning" size={18} />
          <div>
            <strong>이 작업은 되돌릴 수 없습니다.</strong>
            <p>{deleteConfirmation.warning}</p>
          </div>
        </div>
        <p className="project-delete-copy">
          <strong>{deleteConfirmation.project.name}</strong> 프로젝트와 다음 데이터셋의 원본·라벨·학습 산출물이 모두 삭제됩니다.
        </p>
        <div className="project-delete-list" aria-label="삭제 대상 데이터셋">
          <strong>삭제 대상 데이터셋 {deleteConfirmation.datasets.length}개</strong>
          <ul>
            {deleteConfirmation.datasets.map((dataset) => (
              <li key={dataset.id}>{dataset.name}</li>
            ))}
          </ul>
        </div>
        {error ? <div className="error-text project-dialog-error" role="alert">{error}</div> : null}
        <div className="dialog-actions">
          <button className="btn btn-secondary" type="button" disabled={busy} ref={cancelRef} onClick={onClose}>취소</button>
          <button className="btn btn-danger" type="button" disabled={busy} onClick={onConfirm}>
            {busy ? "삭제 중…" : "프로젝트 삭제"}
          </button>
        </div>
      </section>
    </div>
  );
}

interface RenameDatasetDialogProps {
  dataset: ProjectDatasetRow;
  busy: boolean;
  error: string | null;
  onClose: () => void;
  onRename: (name: string) => void;
}

function RenameDatasetDialog({
  dataset,
  busy,
  error,
  onClose,
  onRename,
}: RenameDatasetDialogProps) {
  const [name, setName] = useState(dataset.name);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    inputRef.current?.focus();
    inputRef.current?.select();
  }, []);

  useEffect(() => {
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !busy) onClose();
    };
    window.addEventListener("keydown", closeOnEscape);
    return () => window.removeEventListener("keydown", closeOnEscape);
  }, [busy, onClose]);

  const normalizedName = name.trim();
  return (
    <div
      className="dialog-backdrop"
      role="presentation"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget && !busy) onClose();
      }}
    >
      <section
        className="dialog project-action-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="rename-dataset-title"
      >
        <button
          className="btn btn-ghost btn-sm dialog-close"
          type="button"
          aria-label="닫기"
          disabled={busy}
          onClick={onClose}
        ><Icon name="x" size={16} /></button>
        <h2 className="dialog-title" id="rename-dataset-title">데이터셋 이름 변경</h2>
        <form
          onSubmit={(event) => {
            event.preventDefault();
            if (normalizedName && normalizedName !== dataset.name) onRename(normalizedName);
          }}
        >
          <div className="field">
            <label htmlFor="rename-dataset-name">데이터셋명</label>
            <input
              className={`input${error ? " is-error" : ""}`}
              id="rename-dataset-name"
              ref={inputRef}
              value={name}
              maxLength={255}
              disabled={busy}
              onChange={(event) => setName(event.target.value)}
            />
            {error ? <div className="error-text" role="alert">{error}</div> : null}
          </div>
          <div className="dialog-actions">
            <button className="btn btn-secondary" type="button" disabled={busy} onClick={onClose}>취소</button>
            <button className="btn btn-primary" type="submit" disabled={busy || !normalizedName || normalizedName === dataset.name}>
              {busy ? "변경 중…" : "변경"}
            </button>
          </div>
        </form>
      </section>
    </div>
  );
}

interface DeleteDatasetDialogProps {
  dataset: ProjectDatasetRow;
  busy: boolean;
  error: string | null;
  onClose: () => void;
  onConfirm: () => void;
}

function DeleteDatasetDialog({
  dataset,
  busy,
  error,
  onClose,
  onConfirm,
}: DeleteDatasetDialogProps) {
  const cancelRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    cancelRef.current?.focus();
  }, []);

  useEffect(() => {
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !busy) onClose();
    };
    window.addEventListener("keydown", closeOnEscape);
    return () => window.removeEventListener("keydown", closeOnEscape);
  }, [busy, onClose]);

  return (
    <div
      className="dialog-backdrop"
      role="presentation"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget && !busy) onClose();
      }}
    >
      <section
        className="dialog project-action-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="delete-dataset-title"
        aria-describedby="delete-dataset-warning"
      >
        <button
          className="btn btn-ghost btn-sm dialog-close"
          type="button"
          aria-label="닫기"
          disabled={busy}
          onClick={onClose}
        ><Icon name="x" size={16} /></button>
        <h2 className="dialog-title" id="delete-dataset-title">데이터셋 삭제</h2>
        <div className="project-delete-warning" id="delete-dataset-warning">
          <Icon name="warning" size={18} />
          <div>
            <strong>이 작업은 되돌릴 수 없습니다.</strong>
            <p>원본 이미지·라벨 파일이 삭제됩니다.</p>
          </div>
        </div>
        <p className="project-delete-copy">
          <strong>{dataset.name}</strong> 데이터셋을 삭제하시겠습니까?
        </p>
        {dataset.is_merged && dataset.source_datasets.length > 0 ? (
          <p className="project-delete-copy">
            병합에 포함된 원본 데이터셋 {dataset.source_datasets.length}개도 함께 삭제됩니다.
          </p>
        ) : null}
        <p className="project-delete-copy">
          완료된 학습 기록과 산출물은 유지되지만, 이 데이터셋과의 연결은 해제됩니다.
        </p>
        {error ? <div className="error-text project-dialog-error" role="alert">{error}</div> : null}
        <div className="dialog-actions">
          <button className="btn btn-secondary" type="button" disabled={busy} ref={cancelRef} onClick={onClose}>취소</button>
          <button className="btn btn-danger" type="button" disabled={busy} onClick={onConfirm}>
            {busy ? "삭제 중…" : "데이터셋 삭제"}
          </button>
        </div>
      </section>
    </div>
  );
}
