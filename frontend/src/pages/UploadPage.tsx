import { useEffect, useMemo, useRef, useState } from "react";

import {
  ApiError,
  getAllIssues,
  getDataset,
  getProject,
  invalidateStorageQuotaCache,
  extendMergedDataset,
  mergeDatasets,
  resolveJobClassConflicts,
  type ClassResolutionChoice,
  type ClassResolutionPlan,
  type IssueKind,
  type Job,
  type JobDataset,
  type ProjectRow,
} from "../api/client";
import {
  type CollectedFile,
  type PreparedUploadBatch,
  type PreparedUploadOperation,
  beginUploadBatch,
  clearUploadDatasetTarget,
  clearUploadBatchResume,
  completeUploadBatch,
  createDatasetForUpload,
  pollUploadJob,
  prepareUploadBatch,
  rememberUploadDatasetTarget,
  resumeUploadDatasetTarget,
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
  collectDroppedSources,
  toCollectedFile,
} from "../utils/dropCollection";
import {
  formatRemainingTime,
  updateProgressEstimate,
  type ProgressEstimateState,
} from "../utils/uploadProgress";
import {
  coalesceDroppedSources,
  createUploadPlan,
  datasetNameAfterSourceChange,
  datasetNameWithSuffix,
  groupInputFiles,
  uploadPartitionPreview,
  type UploadSource,
  type UploadSourceDraft,
} from "../utils/uploadGrouping";

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

function uploadableBytes(files: readonly CollectedFile[]) {
  return files.reduce(
    (total, file) => total + (file.kind === "other" ? 0 : file.file.size),
    0,
  );
}

interface CompletedDataset {
  id: number;
  name: string;
}

interface UnitTransferState {
  operation: PreparedUploadOperation;
  knownJobId: number | null;
  pendingBatch?: {
    index: number;
    batch: PreparedUploadBatch;
  };
}

type LiveProgressStage = "idle" | "transferring" | "processing" | "finishing" | "done";

interface LiveProgress {
  stage: LiveProgressStage;
  imageProcessed: number;
  imageTotal: number;
  etaSeconds: number | null;
}

const EMPTY_LIVE_PROGRESS: LiveProgress = {
  stage: "idle",
  imageProcessed: 0,
  imageTotal: 0,
  etaSeconds: null,
};

const JOB_PHASE_LABELS: Record<string, string> = {
  queued: "서버 접수 대기",
  assembling: "업로드 파일 결합",
  uploading: "업로드 확인",
  collecting: "파일 목록 정리",
  parsing: "라벨 분석",
  storing: "이미지 처리 준비",
  deriving: "이미지 처리",
  thumbnailing: "마무리",
  done: "처리 완료",
};

export function UploadPage() {
  const requestedProjectId = useMemo(() => {
    const raw = new URLSearchParams(window.location.search).get("project_id");
    const parsed = raw === null ? Number.NaN : Number(raw);
    return Number.isInteger(parsed) && parsed > 0 ? parsed : null;
  }, []);
  const fileRef = useRef<HTMLInputElement>(null);
  const folderRef = useRef<HTMLInputElement>(null);
  const sourceSequence = useRef(0);
  const progressEstimateRef = useRef<ProgressEstimateState | null>(null);
  // 유닛(=데이터셋)당 영속 매니페스트를 유지해 재시도도 같은 작업으로 수렴시킨다.
  const unitTransfersRef = useRef<Record<string, UnitTransferState>>({});
  const selectionLockedRef = useRef(false);
  const datasetNameEditedRef = useRef(false);
  const classResolutionPreferencesRef = useRef<ClassResolutionPreferences>({});
  const [dragging, setDragging] = useState(false);
  const [trayOpen, setTrayOpen] = useState(false);
  const [datasetName, setDatasetName] = useState("");
  const [mergeIntoDatasetId, setMergeIntoDatasetId] = useState<number | null>(null);
  const [sources, setSources] = useState<UploadSource[]>([]);
  const [uploadTargets, setUploadTargets] = useState<Record<string, number>>({});
  const [uploadDatasetResults, setUploadDatasetResults] = useState<Record<string, JobDataset[]>>({});
  const [completedBatchCounts, setCompletedBatchCounts] = useState<Record<string, number>>({});
  const [completedDatasets, setCompletedDatasets] = useState<CompletedDataset[]>([]);
  const [finalDataset, setFinalDataset] = useState<CompletedDataset | null>(null);
  const [issues, setIssues] = useState<ScopedIssueRow[]>([]);
  const [expandedIssueKind, setExpandedIssueKind] = useState<IssueKind | null>(null);
  const [stats, setStats] = useState({ images: 0, annotations: 0, classes: 0 });
  const [progress, setProgress] = useState({ processed: 0, total: 0, current: "" });
  const [liveProgress, setLiveProgress] = useState<LiveProgress>(EMPTY_LIVE_PROGRESS);
  const [collectionProgress, setCollectionProgress] = useState({
    treePercentage: 0,
    filePercentage: 0,
    filesProcessed: 0,
    filesTotal: 0,
    current: "폴더 트리 검색 시작",
  });
  const [collecting, setCollecting] = useState(false);
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
  const uploadableSourceCount = useMemo(
    () => sources.filter((source) =>
      source.files.some((file) => file.kind !== "other"),
    ).length,
    [sources],
  );
  const selectedImageCount = useMemo(
    () => files.filter((file) => file.kind === "image").length,
    [files],
  );
  const partitionPreview = useMemo(
    () => uploadableSourceCount === 1
      ? uploadPartitionPreview(datasetName, selectedImageCount)
      : null,
    [datasetName, selectedImageCount, uploadableSourceCount],
  );
  const completedUploadItemCount = uploadPlan.reduce(
    (count, unit) => count + (
      (completedBatchCounts[unit.key] ?? 0) >= unit.batches.length ? 1 : 0
    ),
    0,
  );
  const datasetNameMissing = uploadableSourceCount > 0 && !datasetName.trim();
  const hasUploadTargets = Object.keys(uploadTargets).length > 0;

  useEffect(() => {
    if (selectionLockedRef.current) return;
    setDatasetName((current) => datasetNameAfterSourceChange(
      current,
      sources,
      datasetNameEditedRef.current,
    ));
  }, [sources]);

  const addSources = (drafts: UploadSourceDraft[]) => {
    if (selectionLockedRef.current) return;
    const nonempty = coalesceDroppedSources(drafts).filter((draft) => draft.files.length > 0);
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
    setUploadDatasetResults({});
    setTrayOpen(true);
    setDone(false);
    setFinalDataset(null);
    progressEstimateRef.current = null;
    setLiveProgress(EMPTY_LIVE_PROGRESS);
    setError(null);
  };

  const collectInput = (list: FileList | null, folder: boolean) => {
    if (!list) return;
    const collectedFiles = Array.from(list).map((file) => toCollectedFile(
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
    if (uploadPlan.length === 0) {
      setError(sources.length > 0
        ? "업로드할 수 있는 파일이 없습니다."
        : "업로드할 파일이나 폴더를 먼저 선택하세요.");
      return;
    }
    if (!datasetName.trim()) {
      setError("데이터셋 이름을 입력하세요.");
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
    const isResumingPreparedBatch = Object.values(unitTransfersRef.current).some(
      (state) => state.pendingBatch !== undefined,
    );
    setBusy(true);
    setDone(false);
    setError(null);
    setClassResolutionError(null);
    setIssues([]);
    setExpandedIssueKind(null);
    setStats({ images: 0, annotations: 0, classes: 0 });
    setCompletedDatasets([]);
    setFinalDataset(null);
    progressEstimateRef.current = null;
    if (!isResumingPreparedBatch) setLiveProgress(EMPTY_LIVE_PROGRESS);

    const targetIds = { ...uploadTargets };
    const datasetResultsByUnit = { ...uploadDatasetResults };
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
    const totalWork = totalBytes * 3;
    let completedWork = uploadPlan.reduce((unitTotal, unit) => {
      const completedCount = Math.min(
        completedBatchCounts[unit.key] ?? 0,
        unit.batches.length,
      );
      const transferredBytes = unit.batches.slice(0, completedCount).reduce(
        (batchTotal, batchFiles) => batchTotal + uploadableBytes(batchFiles),
        0,
      );
      // 전송(준비+업로드)까지 끝난 배치는 2/3, 서버 처리까지 끝난 유닛은 3/3.
      const processedBytes = datasetResultsByUnit[unit.key] !== undefined
        ? transferredBytes
        : 0;
      return unitTotal + transferredBytes * 2 + processedBytes;
    }, 0);
    let completedBatches = uploadPlan.reduce(
      (total, unit) => total + Math.min(
        completedBatchCounts[unit.key] ?? 0,
        unit.batches.length,
      ),
      0,
    );
    if (!isResumingPreparedBatch) {
      setProgress({ processed: completedWork, total: totalWork, current: "" });
    }

    try {
      for (const unit of uploadPlan) {
        let unitDatasetResults = datasetResultsByUnit[unit.key] ?? null;
        const unitFiles = unit.batches.flat();
        let targetId = targetIds[unit.key];
        let targetWasResumed = false;
        const createTarget = async () => {
          let createdTargetId: number | undefined;
          let candidateName = unit.name;
          let suffixIndex = 2;
          let duplicateAttempts = 0;
          while (createdTargetId === undefined) {
            try {
              const created = await createDatasetForUpload(candidateName, project.id);
              createdTargetId = created.id;
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
          rememberUploadDatasetTarget(
            project.id,
            unit.name,
            createdTargetId,
            unitFiles,
          );
          targetIds[unit.key] = createdTargetId;
          setUploadTargets({ ...targetIds });
          return createdTargetId;
        };
        if (targetId === undefined) {
          const resumedTargetId = resumeUploadDatasetTarget(
            project.id,
            unit.name,
            unitFiles,
          );
          if (resumedTargetId !== null) {
            targetId = resumedTargetId;
            targetWasResumed = true;
            targetIds[unit.key] = targetId;
            setUploadTargets({ ...targetIds });
          } else {
            targetId = await createTarget();
          }
        }

        const unitBytes = unit.batches.reduce(
          (total, batchFiles) => total + uploadableBytes(batchFiles),
          0,
        );
        let transferState = unitTransfersRef.current[unit.key];
        if (unitDatasetResults === null) {
          if (transferState === undefined) {
            let operation: PreparedUploadOperation;
            try {
              operation = await beginUploadBatch(targetId, unitFiles);
            } catch (reason: unknown) {
              if (!(
                targetWasResumed
                && reason instanceof ApiError
                && reason.status === 404
              )) throw reason;
              clearUploadDatasetTarget(
                project.id,
                unit.name,
                targetId,
                unitFiles,
              );
              targetId = await createTarget();
              operation = await beginUploadBatch(targetId, unitFiles);
            }
            transferState = {
              operation,
              knownJobId: operation.knownJobId,
            };
            unitTransfersRef.current[unit.key] = transferState;
          }
          const batchStartIndex = batchCounts[unit.key] ?? 0;
          const pendingBatches = transferState.knownJobId === null
            ? unit.batches.slice(batchStartIndex)
            : [];
          for (const [pendingIndex, batchFiles] of pendingBatches.entries()) {
            const batchIndex = batchStartIndex + pendingIndex;
            const batchStart = completedWork;
            const batchBytes = uploadableBytes(batchFiles);
            const pendingBatch = transferState.pendingBatch;
            if (pendingBatch && pendingBatch.index !== batchIndex) {
              throw new Error(`${unit.name}: 이어 올릴 전송 배치 순서가 일치하지 않습니다.`);
            }
            let batch = pendingBatch?.batch;
            if (batch === undefined) {
              batch = await prepareUploadBatch(targetId, batchFiles, ({
                preparedFiles,
                totalFiles,
              }) => {
                setProgress({
                  processed: batchStart + batchBytes * preparedFiles / totalFiles,
                  total: totalWork,
                  current: `${unit.name} · ${preparedFiles.toLocaleString()} / ${totalFiles.toLocaleString()} 준비 중`,
                });
              });
              transferState.pendingBatch = { index: batchIndex, batch };
            }
            const transferStart = batchStart + batch.totalBytes;
            const transferProgressKey = `transfer:${unit.key}:${batchCounts[unit.key] ?? 0}`;
            const transferred = await transferUploadBatch(batch, ({
              uploadedBytes,
              uploadedImages,
              totalImages,
              currentPath,
            }) => {
              const estimate = updateProgressEstimate(progressEstimateRef.current, {
                key: transferProgressKey,
                completed: uploadedBytes,
                total: batch.totalBytes,
                atMs: performance.now(),
              });
              progressEstimateRef.current = estimate.state;
              setProgress({
                processed: transferStart + uploadedBytes,
                total: totalWork,
                current: `${unit.name} · ${currentPath}`,
              });
              setLiveProgress({
                stage: "transferring",
                imageProcessed: uploadedImages,
                imageTotal: totalImages,
                etaSeconds: estimate.remainingSeconds,
              });
            });
            if (
              transferState.knownJobId === null
              && transferred.knownJobId !== null
            ) transferState.knownJobId = transferred.knownJobId;
            transferState.pendingBatch = undefined;
            completedWork += batch.totalBytes * 2;
            completedBatches += 1;
            batchCounts[unit.key] = (batchCounts[unit.key] ?? 0) + 1;
            setCompletedBatchCounts({ ...batchCounts });
            setProgress({
              processed: completedWork,
              total: totalWork,
              current: `${unit.name} · 전송 완료`,
            });
          }
        }

        if (unitDatasetResults === null) {
          if (transferState === undefined) {
            throw new Error(`${unit.name}: 업로드 배치를 복구하지 못했습니다.`);
          }
          let jobId = transferState.knownJobId;
          if (jobId === null) {
            // 완료 본문에 수십만 개 세션 ID를 싣지 않는다. 서버가 영속
            // 매니페스트를 원자적으로 봉인하고 재호출에도 같은 잡을 준다.
            jobId = await completeUploadBatch(transferState.operation);
            transferState.knownJobId = jobId;
          }
          if (jobId !== null) {
            const activeJobId = jobId;
            const unitProcessStart = completedWork;
            const updateJobProgress = (job: Job) => {
              const ratio = job.image_total > 0
                ? Math.min(1, job.image_processed / job.image_total)
                : job.total > 0
                  ? Math.min(1, job.processed / job.total)
                  : 0;
              const processingImages = job.phase === "storing" || job.phase === "deriving";
              let etaSeconds: number | null = null;
              if (processingImages && job.image_total > 0) {
                const estimate = updateProgressEstimate(progressEstimateRef.current, {
                  key: `processing:${activeJobId}`,
                  completed: job.image_processed,
                  total: job.image_total,
                  atMs: performance.now(),
                });
                progressEstimateRef.current = estimate.state;
                etaSeconds = estimate.remainingSeconds;
              } else {
                progressEstimateRef.current = null;
              }
              setLiveProgress({
                stage: processingImages ? "processing" : "finishing",
                imageProcessed: job.image_processed,
                imageTotal: job.image_total,
                etaSeconds,
              });
              setProgress({
                processed: unitProcessStart + unitBytes * ratio,
                total: totalWork,
                current: `${unit.name} · ${JOB_PHASE_LABELS[job.phase] ?? job.phase}`,
              });
            };
            let terminal = await pollUploadJob(activeJobId, updateJobProgress);
            if (terminal.state === "failed") {
              delete unitTransfersRef.current[unit.key];
              batchCounts[unit.key] = 0;
              setCompletedBatchCounts({ ...batchCounts });
              clearUploadBatchResume(transferState.operation);
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
                setPendingClassResolutionJobId(activeJobId);
                setProgress((current) => ({
                  ...current,
                  current: `${unit.name} · 클래스 명칭 확인 필요`,
                }));
                progressEstimateRef.current = null;
                setLiveProgress((current) => ({
                  ...current,
                  stage: "finishing",
                  etaSeconds: null,
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
              await resolveJobClassConflicts(activeJobId, {
                revision: terminal.class_resolution.revision,
                resolutions: remembered,
              });
              terminal = await pollUploadJob(activeJobId, updateJobProgress);
            }
            if (terminal.state === "failed") {
              delete unitTransfersRef.current[unit.key];
              batchCounts[unit.key] = 0;
              setCompletedBatchCounts({ ...batchCounts });
              clearUploadBatchResume(transferState.operation);
              throw new Error(`${unit.name}: 서버 처리 중 오류가 발생했습니다.`);
            }
            unitDatasetResults = terminal.datasets;
            datasetResultsByUnit[unit.key] = terminal.datasets;
            setUploadDatasetResults({ ...datasetResultsByUnit });
          }
          completedWork += unitBytes;
          setProgress({
            processed: completedWork,
            total: totalWork,
            current: `${unit.name} · 처리 완료`,
          });
        }

        const datasetResults = unitDatasetResults ?? [{
          id: targetId,
          name: unit.name,
          status: "pending" as const,
          image_count: 0,
          annotation_count: 0,
          class_count: 0,
        }];
        const [details, issueRows] = await Promise.all([
          Promise.all(datasetResults.map((result) => getDataset(result.id))),
          getAllIssues(targetId),
        ]);
        if (details.some((detail) => detail.status !== "ready")) {
          throw new Error(`${unit.name}: 사용할 수 있는 데이터셋으로 처리되지 않았습니다.`);
        }
        collectedStats.images += details.reduce(
          (total, detail) => total + detail.image_count,
          0,
        );
        collectedStats.annotations += details.reduce(
          (total, detail) => total + detail.annotation_count,
          0,
        );
        collectedStats.classes += details.reduce(
          (total, detail) => total + detail.class_count,
          0,
        );
        collectedIssues.push(...issueRows.map((issue) => ({
          ...issue,
          summaryScope: String(targetId),
        })));
        completed.push(...details.map((detail) => ({
          id: detail.id,
          name: detail.name,
        })));
        setStats({ ...collectedStats });
        setIssues([...collectedIssues]);
        setCompletedDatasets([...completed]);
      }

      if (selectedMergedDataset !== null) {
        progressEstimateRef.current = null;
        setLiveProgress((current) => ({
          ...current,
          stage: "finishing",
          etaSeconds: null,
        }));
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
        progressEstimateRef.current = null;
        setLiveProgress((current) => ({
          ...current,
          stage: "finishing",
          etaSeconds: null,
        }));
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
      uploadPlan.forEach((unit) => {
        const transferState = unitTransfersRef.current[unit.key];
        if (transferState) clearUploadBatchResume(transferState.operation);
        const targetId = targetIds[unit.key];
        if (targetId !== undefined) {
          clearUploadDatasetTarget(
            project.id,
            unit.name,
            targetId,
            unit.batches.flat(),
          );
        }
        delete unitTransfersRef.current[unit.key];
      });
      setLiveProgress((current) => ({
        ...current,
        stage: "done",
        etaSeconds: 0,
      }));
      setDone(true);
    } catch (reason: unknown) {
      progressEstimateRef.current = null;
      setLiveProgress((current) => ({
        ...current,
        stage: "idle",
        etaSeconds: null,
      }));
      setProgress((current) => ({
        ...current,
        current: "업로드 일시 중지 · 이어 올리기 가능",
      }));
      const message = reason instanceof Error ? reason.message : "업로드에 실패했습니다.";
      const partial = completed.length > 0
        ? `${completed.length}개 데이터셋은 완료되었습니다. `
        : completedBatches > 0
          ? `${completedBatches}개 전송 배치는 서버에 보존되었습니다. 재시도하면 이어서 진행합니다. `
          : "";
      setError(`${partial}${message}`);
    } finally {
      invalidateStorageQuotaCache();
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
      progressEstimateRef.current = null;
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

  const measuredPercentage = progress.total > 0
    ? Math.min(100, progress.processed / progress.total * 100)
    : 0;
  const percentage = done
    ? 100
    : Math.min(99.9, measuredPercentage);
  const percentageLabel = Math.floor(percentage * 10) / 10;
  const activeProgressLabel = progress.current || (done ? "처리 완료" : "업로드 준비 중…");
  const imageProgressLabel = liveProgress.imageTotal > 0 && liveProgress.stage !== "done"
    ? `이미지 ${liveProgress.imageTotal.toLocaleString()}장 중 ${liveProgress.imageProcessed.toLocaleString()}장 ${liveProgress.stage === "transferring" ? "전송" : "처리"}`
    : liveProgress.stage === "processing"
      ? "서버에서 이미지 수 확인 중…"
      : null;
  const etaLabel = (
    liveProgress.stage === "transferring" || liveProgress.stage === "processing"
  )
    ? `예상 남은 시간 ${formatRemainingTime(liveProgress.etaSeconds)}`
    : null;
  const labelingDataset = finalDataset;
  const canNavigateAfterUpload = done && percentage === 100 && labelingDataset !== null;

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
                onChange={(event) => {
                  datasetNameEditedRef.current = true;
                  setDatasetName(event.target.value);
                  setError((current) => (
                    current === "데이터셋 이름을 입력하세요." ? null : current
                  ));
                }}
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
              <span className="hint upload-partition-hint">
                데이터셋당 최대 5,000장 · 5,000장 초과 시 자동 분할
                {partitionPreview
                  ? ` · ${partitionPreview.imageCount.toLocaleString()}장을 ${partitionPreview.partCount.toLocaleString()}개 데이터셋으로 자동 분할 (${partitionPreview.sizes.map((size, index) => `${partitionPreview.names[index]} ${size.toLocaleString()}장`).join(" · ")})`
                  : " · ZIP은 서버에서 이미지 수를 확인한 뒤 분할합니다."}
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
            className={`drop-zone${dragging ? " is-dragging" : ""}${collecting ? " is-collecting" : ""}${busy || done ? " is-progressing" : ""}`}
            onDragEnter={(event) => { event.preventDefault(); setDragging(true); }}
            onDragOver={(event) => event.preventDefault()}
            onDragLeave={(event) => {
              if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setDragging(false);
            }}
            onDrop={(event) => {
              event.preventDefault();
              setDragging(false);
              if (busy || collecting || hasUploadTargets) return;
              if (project === null) {
                setError(projectLoading
                  ? "프로젝트를 불러오는 중입니다. 잠시 후 다시 놓아 주세요."
                  : "데이터셋을 추가할 프로젝트가 필요합니다.");
                return;
              }
              setCollecting(true);
              setDone(false);
              setFinalDataset(null);
              setCollectionProgress({
                treePercentage: 0,
                filePercentage: 0,
                filesProcessed: 0,
                filesTotal: 0,
                current: "폴더 트리 검색 시작",
              });
              setError(null);
              void collectDroppedSources(event.dataTransfer, setCollectionProgress)
                .then(addSources)
                .catch((reason: unknown) => {
                  setError(reason instanceof Error ? reason.message : "파일을 읽지 못했습니다.");
                })
                .finally(() => setCollecting(false));
            }}
          >
            <Icon name="upload" size={28} />
            <strong>파일 · 폴더 · zip 을 끌어다 놓으세요</strong>
            {collecting ? (
              <div className="drop-live-progress">
                <div className="drop-progress-stage">
                  <div className="drop-live-progress-copy">
                    <span>폴더 트리 검색</span>
                    <strong className="mono">{collectionProgress.treePercentage}%</strong>
                  </div>
                  <span
                    className="bar"
                    role="progressbar"
                    aria-label="폴더 트리 검색 진행률"
                    aria-valuemin={0}
                    aria-valuemax={100}
                    aria-valuenow={collectionProgress.treePercentage}
                  >
                    <i style={{ width: `${collectionProgress.treePercentage}%` }} />
                  </span>
                </div>
                <div className="drop-progress-stage">
                  <div className="drop-live-progress-copy">
                    <span>실제 파일 읽기</span>
                    <strong className="mono">{collectionProgress.filePercentage}%</strong>
                  </div>
                  <span
                    className="bar"
                    role="progressbar"
                    aria-label="실제 파일 읽기 진행률"
                    aria-valuemin={0}
                    aria-valuemax={100}
                    aria-valuenow={collectionProgress.filePercentage}
                  >
                    <i style={{ width: `${collectionProgress.filePercentage}%` }} />
                  </span>
                </div>
                <span className="drop-live-progress-current">
                  {collectionProgress.current}
                  {collectionProgress.filesTotal > 0
                    ? ` · ${collectionProgress.filesProcessed.toLocaleString()}/${collectionProgress.filesTotal.toLocaleString()}개`
                    : ""}
                </span>
              </div>
            ) : busy || done ? (
              <div className="drop-live-progress">
                <div className="drop-live-progress-copy">
                  <span>{activeProgressLabel}</span>
                  <strong className="mono">{percentageLabel}%</strong>
                </div>
                <span
                  className="bar"
                  role="progressbar"
                  aria-label="업로드 진행률"
                  aria-valuemin={0}
                  aria-valuemax={100}
                  aria-valuenow={percentage}
                >
                  <i style={{ width: `${percentage}%` }} />
                </span>
                {imageProgressLabel || etaLabel ? (
                  <div className="upload-progress-detail" aria-live="polite">
                    <span>{imageProgressLabel}</span>
                    <strong>{etaLabel}</strong>
                  </div>
                ) : null}
              </div>
            ) : (
              <span>
                YOLO / COCO / VOC 자동 감지 · {uploadableSourceCount.toLocaleString()}개 항목 · {files.length.toLocaleString()}개 파일 선택됨
              </span>
            )}
            <div className="drop-actions">
              <button className="btn btn-secondary" type="button" disabled={busy || collecting || hasUploadTargets || project === null} onClick={() => fileRef.current?.click()}>
                <Icon name="upload" size={14} /> 파일 선택
              </button>
              <button className="btn btn-secondary" type="button" disabled={busy || collecting || hasUploadTargets || project === null} onClick={() => folderRef.current?.click()}>
                <Icon name="folder-up" size={14} /> 폴더 선택
              </button>
              <button
                className="btn btn-primary"
                type="button"
                disabled={
                  project === null
                  || busy
                  || collecting
                  || pendingClassResolution !== null
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
            <input ref={fileRef} className="sr-only" type="file" multiple disabled={busy || collecting || hasUploadTargets} accept="image/*,.zip,.txt,.json,.xml,.yaml,.yml" onChange={(event) => { collectInput(event.target.files, false); event.target.value = ""; }} />
            <input ref={folderRef} className="sr-only" type="file" multiple disabled={busy || collecting || hasUploadTargets} onChange={(event) => { collectInput(event.target.files, true); event.target.value = ""; }} />
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
              <div className="tray-total"><span className="bar"><i style={{ width: `${percentage}%` }} /></span><span className="mono">{percentageLabel}%</span></div>
              {imageProgressLabel || etaLabel ? (
                <div className="tray-progress-detail" aria-live="polite">
                  <span>{imageProgressLabel}</span>
                  <strong>{etaLabel}</strong>
                </div>
              ) : null}
              <div className="tray-files">
                <div>
                  <span>{progress.current || files[0]?.relPath || "파일을 선택하세요"}</span>
                  <span className={pendingClassResolution ? "tag tag-warn" : done ? "tag tag-ok" : "tray-file-progress"}>
                    {pendingClassResolution ? "확인 필요" : done ? "완료" : `${percentageLabel}%`}
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
