import { type DragEvent, useEffect, useMemo, useRef, useState } from "react";

import {
  ApiError,
  getAllIssues,
  getDataset,
  getProject,
  extendMergedDataset,
  mergeDatasets,
  resolveJobClassConflicts,
  type ClassResolutionChoice,
  type ClassResolutionPlan,
  type IssueKind,
  type ProjectRow,
} from "../api/client";
import {
  type CollectedFile,
  clearUploadBatchResume,
  createDatasetForUpload,
  pollUploadJob,
  prepareUploadBatch,
  transferUploadBatch,
} from "../api/upload";
import { AppShell, BreadcrumbLink } from "../components/AppShell";
import { ClassConflictResolutionPanel } from "../components/ClassConflictResolutionPanel";
import { Icon } from "../components/Icon";
import { appHref } from "../navigation";
import {
  choicesWithRememberedPreferences,
  rememberClassResolutions,
  resolutionsFromPreferences,
  type ClassResolutionPreferences,
} from "../utils/classResolutionPreferences";
import {
  getImportIssueSummary,
  groupImportIssueDetails,
  type ScopedIssueRow,
} from "../utils/importIssueSummary";
import {
  createUploadPlan,
  datasetNameWithSuffix,
  groupInputFiles,
  suggestedDatasetName,
  type UploadSource,
  type UploadSourceDraft,
} from "../utils/uploadGrouping";

const IMAGE_EXTENSIONS = new Set([
  "avif", "bmp", "dng", "heic", "heif", "jp2", "jpeg", "jpeg2000",
  "jpg", "mpo", "png", "tif", "tiff", "webp",
]);

const ISSUE_LABELS: Record<IssueKind, string> = {
  image_without_label: "라벨 없는 이미지 파일",
  empty_label: "빈 라벨 파일",
  label_without_image: "이미지 없는 라벨 파일",
  broken_image: "깨진 이미지 파일",
  broken_label: "깨진 라벨 파일",
  duplicate_skipped: "중복 이미지",
  ignored_file: "사용하지 않은 파일",
  class_conflict: "클래스 오류",
  rejected_file: "거부된 파일",
};

const ISSUE_ORDER = Object.keys(ISSUE_LABELS) as IssueKind[];

interface FileEntry {
  isFile: boolean;
  isDirectory: boolean;
  fullPath: string;
  file?: (callback: (file: File) => void) => void;
  createReader?: () => {
    readEntries: (callback: (entries: FileEntry[]) => void) => void;
  };
}

function classify(file: File): CollectedFile["kind"] {
  const lower = file.name.toLowerCase();
  const extension = lower.includes(".") ? lower.slice(lower.lastIndexOf(".") + 1) : "";
  if (IMAGE_EXTENSIONS.has(extension) || file.type.startsWith("image/")) return "image";
  if (lower === "classes.txt" || extension === "yaml" || extension === "yml") return "classfile";
  if (extension === "txt") return "label";
  if (extension === "zip") return "zip";
  return "other";
}

function collected(file: File, relPath = file.name): CollectedFile {
  return {
    file,
    relPath: relPath.replace(/^\/+/, "") || file.name,
    kind: classify(file),
  };
}

function uploadableBytes(files: readonly CollectedFile[]) {
  return files.reduce(
    (total, file) => total + (file.kind === "other" ? 0 : file.file.size),
    0,
  );
}

function fileFromEntry(entry: FileEntry): Promise<CollectedFile> {
  return new Promise((resolve, reject) => {
    if (!entry.file) {
      reject(new Error(`파일을 읽을 수 없습니다: ${entry.fullPath}`));
      return;
    }
    entry.file((file) => resolve(collected(file, entry.fullPath)));
  });
}

async function walkEntry(entry: FileEntry): Promise<CollectedFile[]> {
  if (entry.isFile) return [await fileFromEntry(entry)];
  if (!entry.isDirectory || !entry.createReader) return [];
  const reader = entry.createReader();
  const entries: FileEntry[] = [];
  while (true) {
    const batch = await new Promise<FileEntry[]>((resolve) => reader.readEntries(resolve));
    if (batch.length === 0) break;
    entries.push(...batch);
  }
  return (await Promise.all(entries.map(walkEntry))).flat();
}

function entryName(entry: FileEntry) {
  return entry.fullPath.replaceAll("\\", "/").split("/").filter(Boolean)[0] || "dataset";
}

async function collectDrop(event: DragEvent<HTMLDivElement>): Promise<UploadSourceDraft[]> {
  const groups = await Promise.all(Array.from(event.dataTransfer.items).map(async (item) => {
    const entry = (item as DataTransferItem & {
      webkitGetAsEntry?: () => FileEntry | null;
    }).webkitGetAsEntry?.();
    if (entry?.isDirectory) {
      return {
        source: {
          name: entryName(entry),
          kind: "folder" as const,
          files: await walkEntry(entry),
        },
        files: [] as CollectedFile[],
      };
    }
    if (entry?.isFile) {
      return { source: null, files: [await fileFromEntry(entry)] };
    }
    const file = item.getAsFile();
    return { source: null, files: file ? [collected(file)] : [] };
  }));
  return [
    ...groups.flatMap((group) => group.source ? [group.source] : []),
    ...groupInputFiles(groups.flatMap((group) => group.files), "files"),
  ];
}

interface CompletedDataset {
  id: number;
  name: string;
}

export function UploadPage() {
  const requestedProjectId = useMemo(() => {
    const raw = new URLSearchParams(window.location.search).get("project_id");
    const parsed = raw === null ? Number.NaN : Number(raw);
    return Number.isInteger(parsed) && parsed > 0 ? parsed : null;
  }, []);
  const fileRef = useRef<HTMLInputElement>(null);
  const folderRef = useRef<HTMLInputElement>(null);
  const sourceSequence = useRef(0);
  const selectionLockedRef = useRef(false);
  const classResolutionPreferencesRef = useRef<ClassResolutionPreferences>({});
  const [dragging, setDragging] = useState(false);
  const [trayOpen, setTrayOpen] = useState(false);
  const [datasetName, setDatasetName] = useState("");
  const [mergeIntoDatasetId, setMergeIntoDatasetId] = useState<number | null>(null);
  const [sources, setSources] = useState<UploadSource[]>([]);
  const [uploadTargets, setUploadTargets] = useState<Record<string, number>>({});
  const [completedBatchCounts, setCompletedBatchCounts] = useState<Record<string, number>>({});
  const [completedDatasets, setCompletedDatasets] = useState<CompletedDataset[]>([]);
  const [finalDataset, setFinalDataset] = useState<CompletedDataset | null>(null);
  const [issues, setIssues] = useState<ScopedIssueRow[]>([]);
  const [expandedIssueKind, setExpandedIssueKind] = useState<IssueKind | null>(null);
  const [stats, setStats] = useState({ images: 0, annotations: 0, classes: 0 });
  const [progress, setProgress] = useState({ processed: 0, total: 0, current: "" });
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [pendingClassResolution, setPendingClassResolution] = useState<ClassResolutionPlan | null>(null);
  const [pendingClassResolutionJobId, setPendingClassResolutionJobId] = useState<number | null>(null);
  const [classResolutionBusy, setClassResolutionBusy] = useState(false);
  const [classResolutionError, setClassResolutionError] = useState<string | null>(null);
  const [project, setProject] = useState<ProjectRow | null>(null);
  const [projectLoading, setProjectLoading] = useState(true);

  useEffect(() => {
    folderRef.current?.setAttribute("webkitdirectory", "");
  }, []);

  useEffect(() => {
    let active = true;
    if (requestedProjectId === null) {
      setProjectLoading(false);
      setError("프로젝트 목록에서 ‘데이터셋’ 버튼을 눌러 진입하세요.");
      return () => { active = false; };
    }
    void getProject(requestedProjectId)
      .then((row) => {
        if (!active) return;
        setProject(row);
        setError(null);
      })
      .catch((reason: unknown) => {
        if (!active) return;
        setError(reason instanceof Error ? reason.message : "프로젝트를 불러오지 못했습니다.");
      })
      .finally(() => {
        if (active) setProjectLoading(false);
      });
    return () => { active = false; };
  }, [requestedProjectId]);

  const files = useMemo(
    () => sources.flatMap((source) => source.files),
    [sources],
  );
  const mergedUploadTargets = useMemo(
    () => project?.datasets.filter((dataset) => (
      dataset.is_merged && dataset.status === "ready"
    )) ?? [],
    [project],
  );
  const selectedMergedDataset = mergedUploadTargets.find(
    (dataset) => dataset.id === mergeIntoDatasetId,
  ) ?? null;
  const uploadPlan = useMemo(
    () => createUploadPlan(
      sources,
      datasetName,
      project?.datasets.map((dataset) => dataset.name) ?? [],
    ),
    [datasetName, project, sources],
  );
  const completedUploadItemCount = uploadPlan.reduce(
    (count, unit) => count + (
      (completedBatchCounts[unit.key] ?? 0) >= unit.batches.length ? 1 : 0
    ),
    0,
  );
  const uploadableSourceCount = useMemo(
    () => sources.filter((source) =>
      source.files.some((file) => file.kind !== "other"),
    ).length,
    [sources],
  );
  const datasetNameMissing = uploadableSourceCount > 0 && !datasetName.trim();
  const hasUploadTargets = Object.keys(uploadTargets).length > 0;

  useEffect(() => {
    if (selectionLockedRef.current) return;
    setDatasetName(suggestedDatasetName(sources));
  }, [sources]);

  const addSources = (drafts: UploadSourceDraft[]) => {
    if (selectionLockedRef.current) return;
    const nonempty = drafts.filter((draft) => draft.files.length > 0);
    if (nonempty.length === 0) return;
    const nextSources = nonempty.map((draft) => ({
      ...draft,
      key: `${draft.kind}:${sourceSequence.current += 1}`,
    }));
    setSources((current) => {
      const merged = [...current];
      nextSources.forEach((source) => {
        if (source.kind !== "files") {
          merged.push(source);
          return;
        }
        const looseIndex = merged.findIndex((item) => item.kind === "files");
        if (looseIndex === -1) {
          merged.push(source);
          return;
        }
        const loose = merged[looseIndex];
        merged[looseIndex] = {
          ...loose,
          files: [...loose.files, ...source.files],
        };
      });
      return merged;
    });
    setTrayOpen(true);
    setDone(false);
    setFinalDataset(null);
    setError(null);
  };

  const collectInput = (list: FileList | null, folder: boolean) => {
    if (!list) return;
    const collectedFiles = Array.from(list).map((file) => collected(
      file,
      folder && file.webkitRelativePath ? file.webkitRelativePath : file.name,
    ));
    addSources(groupInputFiles(collectedFiles, folder ? "folder" : "files"));
  };

  const issueSummary = useMemo(() => getImportIssueSummary(issues), [issues]);
  const completedDatasetNames = useMemo(
    () => new Map(completedDatasets.map((dataset) => [String(dataset.id), dataset.name])),
    [completedDatasets],
  );

  const startUpload = async () => {
    if (project === null) {
      setError("데이터셋을 추가할 프로젝트가 필요합니다.");
      return;
    }
    if (!datasetName.trim()) {
      setError("데이터셋 이름을 입력하세요.");
      return;
    }
    if (uploadPlan.length === 0) {
      setError("업로드할 수 있는 파일이 없습니다.");
      return;
    }
    if (uploadPlan.length > 200) {
      setError("한 번에 자동 병합할 수 있는 폴더·ZIP은 최대 200개입니다.");
      return;
    }
    if (mergeIntoDatasetId !== null && selectedMergedDataset === null) {
      setError("선택한 병합 데이터셋을 사용할 수 없습니다. 페이지를 새로고침해 주세요.");
      return;
    }
    selectionLockedRef.current = true;
    setBusy(true);
    setDone(false);
    setError(null);
    setClassResolutionError(null);
    setIssues([]);
    setExpandedIssueKind(null);
    setStats({ images: 0, annotations: 0, classes: 0 });
    setCompletedDatasets([]);
    setFinalDataset(null);

    const targetIds = { ...uploadTargets };
    const batchCounts = { ...completedBatchCounts };
    const reservedNames = new Set([
      ...project.datasets.map((dataset) => dataset.name),
      ...uploadPlan.map((unit) => unit.name),
    ]);
    const completed: CompletedDataset[] = [];
    const collectedIssues: ScopedIssueRow[] = [];
    const collectedStats = { images: 0, annotations: 0, classes: 0 };
    const totalBytes = uploadPlan.reduce(
      (unitTotal, unit) => unitTotal + unit.batches.reduce(
        (batchTotal, batchFiles) => batchTotal + uploadableBytes(batchFiles),
        0,
      ),
      0,
    );
    const totalWork = totalBytes * 2;
    let completedWork = uploadPlan.reduce((unitTotal, unit) => {
      const completedCount = Math.min(
        completedBatchCounts[unit.key] ?? 0,
        unit.batches.length,
      );
      return unitTotal + unit.batches.slice(0, completedCount).reduce(
        (batchTotal, batchFiles) => batchTotal + uploadableBytes(batchFiles) * 2,
        0,
      );
    }, 0);
    let completedBatches = uploadPlan.reduce(
      (total, unit) => total + Math.min(
        completedBatchCounts[unit.key] ?? 0,
        unit.batches.length,
      ),
      0,
    );
    setProgress({ processed: completedWork, total: totalWork, current: "" });

    try {
      for (const unit of uploadPlan) {
        let targetId = targetIds[unit.key];
        if (targetId === undefined) {
          let candidateName = unit.name;
          let suffixIndex = 2;
          let duplicateAttempts = 0;
          while (targetId === undefined) {
            try {
              const created = await createDatasetForUpload(candidateName, project.id);
              targetId = created.id;
              reservedNames.add(created.name);
            } catch (reason: unknown) {
              if (!(reason instanceof ApiError && reason.status === 409)) throw reason;
              duplicateAttempts += 1;
              if (duplicateAttempts >= 100) {
                throw new Error(`${unit.name}: 중복되지 않는 데이터셋 이름을 만들지 못했습니다.`);
              }
              do {
                candidateName = datasetNameWithSuffix(unit.baseName, suffixIndex);
                suffixIndex += 1;
              } while (reservedNames.has(candidateName));
              reservedNames.add(candidateName);
            }
          }
          targetIds[unit.key] = targetId;
          setUploadTargets({ ...targetIds });
        }

        const batchStartIndex = completedBatchCounts[unit.key] ?? 0;
        for (const batchFiles of unit.batches.slice(batchStartIndex)) {
          const batch = await prepareUploadBatch(targetId, batchFiles);
          const batchStart = completedWork;
          const jobId = await transferUploadBatch(batch, ({ uploadedBytes, currentPath }) => {
            setProgress({
              processed: batchStart + uploadedBytes,
              total: totalWork,
              current: `${unit.name} · ${currentPath}`,
            });
          });
          if (jobId !== null) {
            const updateJobProgress = (job: { total: number; processed: number; phase: string }) => {
              const ratio = job.total > 0
                ? Math.min(1, job.processed / job.total)
                : 0;
              setProgress({
                processed: batchStart + batch.totalBytes + batch.totalBytes * ratio,
                total: totalWork,
                current: `${unit.name} · ${job.phase}`,
              });
            };
            let terminal = await pollUploadJob(jobId, updateJobProgress);
            if (terminal.state === "failed") {
              clearUploadBatchResume(batch);
              throw new Error(`${unit.name}: 서버 처리 중 오류가 발생했습니다.`);
            }
            const autoResolvedRevisions = new Set<string>();
            while (terminal.state === "awaiting_class_resolution") {
              if (!terminal.class_resolution) {
                throw new Error(`${unit.name}: 클래스 확인 정보를 불러오지 못했습니다.`);
              }
              const remembered = resolutionsFromPreferences(
                terminal.class_resolution,
                classResolutionPreferencesRef.current,
              );
              if (remembered === null) {
                setPendingClassResolution(terminal.class_resolution);
                setPendingClassResolutionJobId(jobId);
                setProgress((current) => ({
                  ...current,
                  current: `${unit.name} · 클래스 명칭 확인 필요`,
                }));
                return;
              }
              if (autoResolvedRevisions.has(terminal.class_resolution.revision)) {
                throw new Error(`${unit.name}: 클래스 명칭 자동 적용을 완료하지 못했습니다.`);
              }
              autoResolvedRevisions.add(terminal.class_resolution.revision);
              setProgress((current) => ({
                ...current,
                current: `${unit.name} · 이전 클래스 선택 자동 적용`,
              }));
              await resolveJobClassConflicts(jobId, {
                revision: terminal.class_resolution.revision,
                resolutions: remembered,
              });
              terminal = await pollUploadJob(jobId, updateJobProgress);
            }
            if (terminal.state === "failed") {
              clearUploadBatchResume(batch);
              throw new Error(`${unit.name}: 서버 처리 중 오류가 발생했습니다.`);
            }
          }
          completedWork += batch.totalBytes * 2;
          completedBatches += 1;
          batchCounts[unit.key] = (batchCounts[unit.key] ?? 0) + 1;
          setCompletedBatchCounts({ ...batchCounts });
          setProgress({
            processed: completedWork,
            total: totalWork,
            current: `${unit.name} · 처리 완료`,
          });
        }

        const [detail, issueRows] = await Promise.all([
          getDataset(targetId),
          getAllIssues(targetId),
        ]);
        if (detail.status !== "ready") {
          throw new Error(`${unit.name}: 사용할 수 있는 데이터셋으로 처리되지 않았습니다.`);
        }
        collectedStats.images += detail.image_count;
        collectedStats.annotations += detail.annotation_count;
        collectedStats.classes += detail.class_count;
        collectedIssues.push(...issueRows.map((issue) => ({
          ...issue,
          summaryScope: String(targetId),
        })));
        completed.push({ id: targetId, name: detail.name });
        setStats({ ...collectedStats });
        setIssues([...collectedIssues]);
        setCompletedDatasets([...completed]);
      }

      if (selectedMergedDataset !== null) {
        setProgress({
          processed: totalWork,
          total: totalWork,
          current: `${selectedMergedDataset.name}에 포함 중`,
        });
        const merged = await extendMergedDataset(selectedMergedDataset.id, {
          dataset_ids: completed.map((dataset) => dataset.id),
        });
        setFinalDataset({ id: merged.id, name: merged.name });
        setStats({
          images: merged.image_count,
          annotations: merged.annotation_count,
          classes: merged.class_count,
        });
        setProgress({
          processed: totalWork,
          total: totalWork,
          current: "기존 병합 데이터셋에 포함 완료",
        });
      } else if (uploadPlan.length > 1) {
        setProgress({
          processed: totalWork,
          total: totalWork,
          current: `${uploadPlan.length.toLocaleString()}개 데이터셋 병합 중`,
        });
        const merged = await mergeDatasets({
          name: datasetName.trim(),
          dataset_ids: completed.map((dataset) => dataset.id),
        });
        setFinalDataset({ id: merged.id, name: merged.name });
        setStats({
          images: merged.image_count,
          annotations: merged.annotation_count,
          classes: merged.class_count,
        });
        setProgress({ processed: totalWork, total: totalWork, current: "자동 병합 완료" });
      } else {
        setFinalDataset(completed[0] ?? null);
        setProgress({ processed: totalWork, total: totalWork, current: "처리 완료" });
      }
      setDone(true);
    } catch (reason: unknown) {
      const message = reason instanceof Error ? reason.message : "업로드에 실패했습니다.";
      const partial = completed.length > 0
        ? `${completed.length}개 데이터셋은 완료되었습니다. `
        : completedBatches > 0
          ? `${completedBatches}개 항목은 이미 반영되었습니다. `
          : "";
      setError(`${partial}${message}`);
    } finally {
      if (Object.keys(targetIds).length === 0) selectionLockedRef.current = false;
      setBusy(false);
    }
  };

  const continueUploadAfterClassResolution = async (
    resolutions: ClassResolutionChoice[],
  ) => {
    if (pendingClassResolution === null || pendingClassResolutionJobId === null) return;
    setClassResolutionBusy(true);
    setClassResolutionError(null);
    try {
      await resolveJobClassConflicts(pendingClassResolutionJobId, {
        revision: pendingClassResolution.revision,
        resolutions,
      });
      classResolutionPreferencesRef.current = rememberClassResolutions(
        classResolutionPreferencesRef.current,
        pendingClassResolution,
        resolutions,
      );
      setPendingClassResolution(null);
      setPendingClassResolutionJobId(null);
      setProgress((current) => ({
        ...current,
        current: "클래스 명칭 적용 완료 · 처리 재개",
      }));
      void startUpload();
    } catch (reason: unknown) {
      setClassResolutionError(
        reason instanceof Error
          ? reason.message
          : "클래스 명칭을 적용하지 못했습니다.",
      );
    } finally {
      setClassResolutionBusy(false);
    }
  };

  const percentage = progress.total > 0
    ? Math.min(100, Math.round(progress.processed / progress.total * 100))
    : done ? 100 : 0;
  const labelingDataset = finalDataset;
  const canNavigateAfterUpload = done && labelingDataset !== null;

  return (
    <>
      <AppShell
        active="projects"
        breadcrumb={
          <>
            <BreadcrumbLink href="/projects">프로젝트</BreadcrumbLink><span>/</span>
            {project ? <><strong>{project.name}</strong><span>/</span></> : null}
            <strong>데이터셋 추가</strong>
          </>
        }
      >
        <h1 className="page-title">데이터셋 추가</h1>
        <section className="card upload-source-card">
          <div className="two-field-grid">
            <div className="field">
              <label htmlFor="project-name">프로젝트</label>
              <input
                className="input"
                id="project-name"
                value={projectLoading ? "불러오는 중…" : project?.name ?? ""}
                disabled
                readOnly
              />
              <span className="hint">현재 프로젝트 · 변경할 수 없습니다.</span>
            </div>
            <div className="field">
              <label htmlFor="dataset-name">데이터셋 이름</label>
              <input
                className={`input${datasetNameMissing ? " is-error" : ""}`}
                id="dataset-name"
                value={datasetName}
                maxLength={255}
                aria-invalid={datasetNameMissing}
                aria-describedby="dataset-name-hint"
                disabled={
                  project === null
                  || busy
                  || pendingClassResolution !== null
                  || (hasUploadTargets && uploadableSourceCount <= 1)
                }
                placeholder={
                  selectedMergedDataset !== null
                    ? "업로드 데이터셋 이름 입력"
                    : uploadableSourceCount > 1
                      ? "병합 데이터셋 이름 입력"
                      : "데이터셋 이름 입력"
                }
                onChange={(event) => setDatasetName(event.target.value)}
              />
              <span
                className={datasetNameMissing ? "error-text" : "hint"}
                id="dataset-name-hint"
              >
                {datasetNameMissing
                  ? uploadableSourceCount > 1
                    ? "병합 데이터셋 이름을 입력해야 업로드를 시작할 수 있습니다."
                    : "데이터셋 이름을 입력해야 업로드를 시작할 수 있습니다."
                  : uploadableSourceCount > 200
                  ? "한 번에 자동 병합할 수 있는 항목은 최대 200개입니다."
                  : selectedMergedDataset !== null
                    ? uploadableSourceCount > 1
                      ? `${uploadableSourceCount.toLocaleString()}개 항목을 각각 업로드한 뒤 ${selectedMergedDataset.name}에 포함합니다.`
                      : `업로드한 데이터셋을 ${selectedMergedDataset.name}에 포함합니다.`
                  : uploadableSourceCount > 1
                    ? `${uploadableSourceCount.toLocaleString()}개 항목을 각각 업로드한 뒤 이 이름으로 자동 병합합니다.`
                    : "선택한 항목을 이 데이터셋으로 업로드합니다."}
              </span>
            </div>
          </div>
          {mergedUploadTargets.length > 0 ? (
            <fieldset
              className="upload-merge-target-fieldset"
              aria-describedby="upload-merge-target-description"
            >
              <legend>기존 병합 데이터셋에 포함</legend>
              <p className="upload-merge-target-copy" id="upload-merge-target-description">
                {mergedUploadTargets.length === 1
                  ? "업로드한 데이터를 기존 병합 데이터셋에 포함할까요?"
                  : `준비된 병합 데이터셋 ${mergedUploadTargets.length.toLocaleString()}개 중 포함할 대상을 선택하세요.`}
              </p>
              <div className="upload-merge-target-options">
                <label className="upload-merge-target-option">
                  <input
                    type="radio"
                    name="upload-merge-target"
                    checked={mergeIntoDatasetId === null}
                    disabled={busy || hasUploadTargets || pendingClassResolution !== null}
                    onChange={() => setMergeIntoDatasetId(null)}
                  />
                  <span>
                    <strong>새 데이터셋으로 추가</strong>
                    <small>기존 병합 데이터셋은 변경하지 않습니다.</small>
                  </span>
                </label>
                {mergedUploadTargets.map((dataset) => (
                  <label className="upload-merge-target-option" key={dataset.id}>
                    <input
                      type="radio"
                      name="upload-merge-target"
                      value={dataset.id}
                      checked={mergeIntoDatasetId === dataset.id}
                      disabled={busy || hasUploadTargets || pendingClassResolution !== null}
                      onChange={() => setMergeIntoDatasetId(dataset.id)}
                    />
                    <span>
                      <strong>{dataset.name}</strong>
                      <small>
                        원본 {dataset.source_datasets.length.toLocaleString()}개 · 이미지 {dataset.image_count.toLocaleString()}개
                      </small>
                    </span>
                  </label>
                ))}
              </div>
            </fieldset>
          ) : null}
          <div
            className={`drop-zone${dragging ? " is-dragging" : ""}`}
            onDragEnter={(event) => { event.preventDefault(); setDragging(true); }}
            onDragOver={(event) => event.preventDefault()}
            onDragLeave={(event) => {
              if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setDragging(false);
            }}
            onDrop={(event) => {
              event.preventDefault();
              setDragging(false);
              if (!busy && !hasUploadTargets && project !== null) void collectDrop(event).then(addSources).catch((reason: unknown) => {
                setError(reason instanceof Error ? reason.message : "파일을 읽지 못했습니다.");
              });
            }}
          >
            <Icon name="upload" size={28} />
            <strong>파일 · 폴더 · zip 을 끌어다 놓으세요</strong>
            <span>
              YOLO / COCO / VOC 자동 감지 · {uploadableSourceCount.toLocaleString()}개 항목 · {files.length.toLocaleString()}개 파일 선택됨
            </span>
            <div className="drop-actions">
              <button className="btn btn-secondary" type="button" disabled={busy || hasUploadTargets || project === null} onClick={() => fileRef.current?.click()}>
                <Icon name="upload" size={14} /> 파일 선택
              </button>
              <button className="btn btn-secondary" type="button" disabled={busy || hasUploadTargets || project === null} onClick={() => folderRef.current?.click()}>
                <Icon name="folder-up" size={14} /> 폴더 선택
              </button>
              <button
                className="btn btn-primary"
                type="button"
                disabled={
                  project === null
                  || busy
                  || pendingClassResolution !== null
                  || !datasetName.trim()
                  || uploadPlan.length === 0
                  || uploadPlan.length > 200
                }
                onClick={() => void startUpload()}
              >
                {pendingClassResolution
                  ? "클래스 확인 필요"
                  : busy
                    ? "처리 중…"
                    : hasUploadTargets
                      ? "이어 올리기"
                      : "업로드 시작"}
              </button>
            </div>
            <input ref={fileRef} className="sr-only" type="file" multiple disabled={busy || hasUploadTargets} accept="image/*,.zip,.txt,.json,.xml,.yaml,.yml" onChange={(event) => { collectInput(event.target.files, false); event.target.value = ""; }} />
            <input ref={folderRef} className="sr-only" type="file" multiple disabled={busy || hasUploadTargets} onChange={(event) => { collectInput(event.target.files, true); event.target.value = ""; }} />
          </div>
          {error ? <p className="class-rename-error" role="alert">{error}</p> : null}
        </section>

        <section className="card upload-result-card" aria-labelledby="upload-result-title">
          <h2 className="sr-only" id="upload-result-title">업로드 검사 결과</h2>
          <div className="upload-stats">
            <div><span>유효 이미지</span><strong className="mono">{stats.images.toLocaleString()}</strong></div>
            <div><span>라벨</span><strong className="mono">{stats.annotations.toLocaleString()}</strong></div>
            <div><span>확인 필요</span><strong className="mono issue-total">{issueSummary.total.toLocaleString()}</strong></div>
          </div>
          {pendingClassResolution ? (
            <ClassConflictResolutionPanel
              plan={pendingClassResolution}
              initialChoices={choicesWithRememberedPreferences(
                pendingClassResolution,
                classResolutionPreferencesRef.current,
              )}
              affectedDatasetCount={project?.dataset_count ?? 0}
              busy={classResolutionBusy}
              error={classResolutionError}
              onSubmit={(resolutions) => void continueUploadAfterClassResolution(resolutions)}
            />
          ) : null}
          <div className="issue-list">
            {ISSUE_ORDER.map((kind) => {
              const detailGroups = groupImportIssueDetails(issues, kind);
              const expanded = expandedIssueKind === kind && detailGroups.length > 0;
              const triggerId = `upload-issue-trigger-${kind}`;
              const detailsId = `upload-issue-details-${kind}`;
              return (
                <div className={`issue-group${expanded ? " is-expanded" : ""}`} key={kind}>
                  <button
                    className="issue-row"
                    type="button"
                    id={triggerId}
                    aria-expanded={expanded}
                    aria-controls={detailsId}
                    disabled={detailGroups.length === 0}
                    onClick={() => setExpandedIssueKind(expanded ? null : kind)}
                  >
                    <span>{ISSUE_LABELS[kind]}</span>
                    <span className="mono">{issueSummary.counts.get(kind) ?? 0}</span>
                    <Icon name={expanded ? "chevron-down" : "chevron-right"} size={13} />
                  </button>
                  <div
                    className="issue-details"
                    id={detailsId}
                    role="region"
                    aria-labelledby={triggerId}
                    hidden={!expanded}
                  >
                    {detailGroups.map((group) => {
                      const scopedDatasetName = group.summaryScope
                        ? completedDatasetNames.get(group.summaryScope)
                        : undefined;
                      return (
                        <div className="issue-detail-group" key={group.key}>
                          <div className="issue-detail-heading">
                            {completedDatasets.length > 1 && scopedDatasetName ? (
                              <span className="tag tag-neutral">데이터셋 {scopedDatasetName}</span>
                            ) : null}
                            <span className="issue-detail-caption">파일 경로</span>
                            <span className="issue-detail-path mono">{group.path}</span>
                          </div>
                          <ul className="issue-detail-reasons">
                            {group.details.map((detail) => (
                              <li key={`${group.key}\u0000${detail}`}>{detail}</li>
                            ))}
                          </ul>
                        </div>
                      );
                    })}
                  </div>
                </div>
              );
            })}
          </div>
          <div className="upload-result-actions" aria-label="업로드 완료 후 이동">
            {canNavigateAfterUpload ? (
              <>
                <a className="btn btn-secondary" href={appHref("/projects")}>프로젝트</a>
                <a className="btn btn-primary" href={appHref(`/datasets/${labelingDataset.id}/viewer`)}>라벨링</a>
              </>
            ) : (
              <>
                <button className="btn btn-secondary" type="button" disabled>프로젝트</button>
                <button className="btn btn-primary" type="button" disabled>라벨링</button>
              </>
            )}
          </div>
        </section>

        <aside className={`card upload-tray${trayOpen ? "" : " is-collapsed"}`} aria-label="업로드 진행률">
          <button className="upload-tray-head" type="button" aria-expanded={trayOpen} onClick={() => setTrayOpen((current) => !current)}>
            <span><strong>{pendingClassResolution ? "클래스 확인 필요" : done ? "업로드 완료" : busy ? "업로드 중" : "업로드 준비"}</strong> <span className="mono">{completedUploadItemCount}/{uploadPlan.length}개 완료</span></span>
            <Icon name={trayOpen ? "chevron-down" : "chevron-up"} size={15} />
          </button>
          {trayOpen ? (
            <>
              <div className="tray-total"><span className="bar"><i style={{ width: `${percentage}%` }} /></span><span className="mono">{percentage}%</span></div>
              <div className="tray-files">
                <div>
                  <span>{progress.current || files[0]?.relPath || "파일을 선택하세요"}</span>
                  <span className={pendingClassResolution ? "tag tag-warn" : done ? "tag tag-ok" : "tray-file-progress"}>
                    {pendingClassResolution ? "확인 필요" : done ? "완료" : `${percentage}%`}
                  </span>
                </div>
              </div>
            </>
          ) : null}
        </aside>
      </AppShell>
    </>
  );
}
