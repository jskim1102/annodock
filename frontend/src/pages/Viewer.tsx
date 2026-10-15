import { type MouseEvent, useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  ApiError, assignImage, claimDatasetImage, completeImage, uncompleteImage, getImage,
  getDataset,
  getDatasetClasses,
  getProject,
  getDatasetImages,
  getImageAnnotations,
  imageResourceUrl,
  renameDatasetClass,
  updateProjectClass,
  saveImageAnnotations,
  type AnnotationResponse,
  type BoxDto,
  type ClassRow,
  type ClassCatalog,
  type DatasetDetail,
  type ImageRow,
} from "../api/client";
import { getWorkspaceMembers, type WorkspaceMemberRow } from "../api/workspaces";
import { useAuthSession } from "../store/auth";
import { WorkspaceDialog } from "../components/InviteLinkDialog";
import { Brand } from "../components/AppShell";
import {
  prefetchAuthenticatedResource,
  useAuthenticatedObjectUrl,
} from "../components/AuthenticatedImage";
import { BboxCanvas, type EditorClass } from "../components/BboxCanvas";
import { ThemeToggle } from "../components/ThemeToggle";
import { ThumbStrip } from "../components/ThumbStrip";
import { type EditorBox, useAnnotationEdit } from "../hooks/useAnnotationEdit";
import { appHref } from "../navigation";
import { CLASS_COLOR_PRESETS } from "../utils/classColors";
import * as adminAccess from "../utils/adminAccess";

interface ViewerPageProps {
  datasetId: number;
}

type ActiveImage = ImageRow;

interface AnnotationFlight {
  controller: AbortController;
  generation: number;
  catalogVersion: number;
  promise: Promise<AnnotationResponse>;
}

const PREFETCH_RADIUS = 3;

function scheduleIdleWork(work: () => void): () => void {
  if (typeof window.requestIdleCallback === "function") {
    const request = window.requestIdleCallback(work, { timeout: 500 });
    return () => window.cancelIdleCallback(request);
  }
  const request = window.setTimeout(work, 50);
  return () => window.clearTimeout(request);
}

function clamp(value: number, minimum: number, maximum: number) {
  return Math.min(maximum, Math.max(minimum, value));
}

function fromDto(box: BoxDto, width: number, height: number): EditorBox {
  return {
    id: box.id,
    classId: box.class_id,
    x: (box.cx - box.w / 2) * width,
    y: (box.cy - box.h / 2) * height,
    width: box.w * width,
    height: box.h * height,
  };
}

function toDto(box: EditorBox, width: number, height: number): Omit<BoxDto, "id"> {
  const left = clamp(Math.min(box.x, box.x + box.width), 0, width);
  const top = clamp(Math.min(box.y, box.y + box.height), 0, height);
  const right = clamp(Math.max(box.x, box.x + box.width), 0, width);
  const bottom = clamp(Math.max(box.y, box.y + box.height), 0, height);
  return {
    class_id: box.classId,
    cx: (left + right) / (2 * width),
    cy: (top + bottom) / (2 * height),
    w: (right - left) / width,
    h: (bottom - top) / height,
  };
}

const CLASS_MAPPING_ERROR = "클래스 목록이 바뀌어 내 편집을 옮길 수 없어요. 서버 것으로 불러와 주세요.";

function hasClassCatalog(value: { classes?: ClassRow[]; catalog_version?: number } | undefined): value is ClassCatalog {
  return !!value && Array.isArray(value.classes)
    && Number.isInteger(value.catalog_version) && value.catalog_version! >= 0;
}

function isTypingTarget(target: EventTarget | null) {
  return target instanceof HTMLElement && (
    target.isContentEditable
    || target.tagName === "INPUT"
    || target.tagName === "TEXTAREA"
    || target.tagName === "SELECT"
  );
}

export function ViewerPage({ datasetId }: ViewerPageProps) {
  const { user } = useAuthSession();
  const annotationCacheRef = useRef(new Map<number, AnnotationResponse>());
  const annotationFlightsRef = useRef(new Map<number, AnnotationFlight>());
  const annotationGenerationRef = useRef(0);
  const appliedCatalogVersionRef = useRef(-1);
  const datasetContextRef = useRef(0);
  const requestedRangesRef = useRef(new Set<string>());
  const rangeGenerationRef = useRef(0);
  const navigationRef = useRef(0);
  const [dataset, setDataset] = useState<DatasetDetail | null>(null);
  const [classRows, setClassRows] = useState<ClassRow[]>([]);
  const classRowsRef = useRef(classRows);
  classRowsRef.current = classRows;
  const [projectName, setProjectName] = useState<string | null>(null);
  const [colors, setColors] = useState<Record<string, string>>({});
  const [items, setItems] = useState<Map<number, ImageRow>>(new Map());
  const [total, setTotal] = useState(0);
  const [split, setSplit] = useState<string | null>(null);
  const [imageIndex, setImageIndex] = useState(0);
  const [loadedImage, setLoadedImage] = useState<ActiveImage | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [homePath, setHomePath] = useState("/projects");

  useEffect(() => {
    let active = true;
    void adminAccess.adminHomePath().then((path) => {
      if (active) setHomePath(path);
    });
    return () => { active = false; };
  }, []);

  const [members, setMembers] = useState<WorkspaceMemberRow[]>([]);
  const [forcedReadOnly, setForcedReadOnly] = useState(false);
  const [busy, setBusy] = useState(false);
  const busyRef = useRef(false);
  const [renameReloadPending, setRenameReloadPending] = useState(false);
  const renameReloadRef = useRef<(() => Promise<void>) | null>(null);
  const [claimEmpty, setClaimEmpty] = useState(false);
  const [changingAssignee, setChangingAssignee] = useState(false);
  const canManage = dataset?.my_role === "admin" || dataset?.my_role === "owner";
  const canEdit = canManage || dataset?.my_role === "editor";
  const readOnly = forcedReadOnly || !canEdit || (loadedImage?.label_status === "in_progress"
    && loadedImage.assignee_id !== user?.id && !canManage);
  const memberName = (id: number | null | undefined) => members.find((member) => member.user_id === id)?.username
    || members.find((member) => member.user_id === id)?.email || "";
  const updateImage = useCallback((id: number, update: Partial<ImageRow>) => {
    setItems((current) => new Map([...current].map(([index, image]) =>
      [index, image.id === id ? { ...image, ...update } : image])));
    setLoadedImage((current) => current?.id === id ? { ...current, ...update } : current);
  }, []);

  const applyClassCatalog = useCallback((snapshot: ClassCatalog) => {
    if (!hasClassCatalog(snapshot) || snapshot.catalog_version <= appliedCatalogVersionRef.current) return;
    appliedCatalogVersionRef.current = snapshot.catalog_version;
    const next = snapshot.classes;
    setColors((current) => Object.fromEntries(next.map((row, index) => [String(row.class_id),
      current[row.class_id] ?? CLASS_COLOR_PRESETS[index % CLASS_COLOR_PRESETS.length].value])));
    classRowsRef.current = next;
    setClassRows(next);
    for (const [id, cached] of annotationCacheRef.current) {
      if (cached.catalog_version < snapshot.catalog_version) annotationCacheRef.current.delete(id);
    }
    // An older in-flight read may return pre-change IDs. Its identity check
    // prevents a late response from repopulating the cache after invalidation.
    for (const [id, flight] of annotationFlightsRef.current) {
      if (flight.catalogVersion < snapshot.catalog_version) {
        flight.controller.abort();
        annotationFlightsRef.current.delete(id);
      }
    }
  }, []);

  const resetAnnotationRequests = useCallback(() => {
    annotationGenerationRef.current += 1;
    annotationFlightsRef.current.forEach(({ controller }) => controller.abort());
    annotationFlightsRef.current.clear();
    annotationCacheRef.current.clear();
  }, []);

  const invalidateImageAnnotations = useCallback((imageId: number) => {
    annotationFlightsRef.current.get(imageId)?.controller.abort();
    annotationFlightsRef.current.delete(imageId);
    annotationCacheRef.current.delete(imageId);
  }, []);

  const loadImageAnnotations = useCallback((imageId: number): Promise<AnnotationResponse> => {
    const cached = annotationCacheRef.current.get(imageId);
    if (cached && cached.catalog_version >= appliedCatalogVersionRef.current) return Promise.resolve(cached);
    annotationCacheRef.current.delete(imageId);

    const existing = annotationFlightsRef.current.get(imageId);
    if (existing) return existing.promise;

    const controller = new AbortController();
    const generation = annotationGenerationRef.current;
    const flight: AnnotationFlight = {
      controller,
      generation,
      catalogVersion: appliedCatalogVersionRef.current,
      promise: Promise.resolve(null as never),
    };
    flight.promise = getImageAnnotations(imageId, controller.signal)
      .then((response) => {
        if (generation !== annotationGenerationRef.current || annotationFlightsRef.current.get(imageId) !== flight) {
          throw new DOMException("어노테이션 요청이 취소되었습니다.", "AbortError");
        }
        annotationCacheRef.current.set(imageId, response);
        return response;
      })
      .finally(() => {
        if (annotationFlightsRef.current.get(imageId) === flight) {
          annotationFlightsRef.current.delete(imageId);
        }
      });
    annotationFlightsRef.current.set(imageId, flight);
    return flight.promise;
  }, []);

  const saveAnnotations = useCallback(async (boxes: EditorBox[], workspaceId: number | null, expectedVersion: number, expectedCatalogVersion: number) => {
    const active = loadedImage;
    if (!active) return;
    const generation = annotationGenerationRef.current;
    try {
      const response = await saveImageAnnotations(active.id,
        boxes.map((box) => toDto(box, active.width, active.height)), workspaceId, expectedVersion, expectedCatalogVersion);
      if (generation === annotationGenerationRef.current) {
        invalidateImageAnnotations(active.id);
        if (response.catalog_version >= appliedCatalogVersionRef.current) {
          annotationCacheRef.current.set(active.id, { ...response, width: active.width, height: active.height });
        }
        updateImage(active.id, { version: response.version, label_status: response.label_status,
          assignee_id: response.assignee_id, box_count: response.boxes.length, is_modified: response.is_modified });
      }
      return response;
    } catch (reason) {
      if (reason instanceof ApiError && reason.status === 409) {
        const detail = reason.detail as {code?: string; assignee_id?: number;
          current?: Pick<AnnotationResponse, "version" | "boxes" | "classes" | "catalog_version" | "label_status" | "assignee_id">};
        if (detail?.code === "image_assigned_to_other" && generation === annotationGenerationRef.current) {
          updateImage(active.id, { label_status: "in_progress", assignee_id: detail.assignee_id });
        }
        if (detail?.code === "version_conflict" || detail?.code === "class_catalog_changed") {
          // This read must bypass prefetch/cache and preserve the write's workspace.
          invalidateImageAnnotations(active.id);
          let current = detail.current;
          if (hasClassCatalog(current)) {
            try {
              current = await getImageAnnotations(active.id, undefined, workspaceId);
            } catch {
              // Keep the original conflict if the fresh read fails.
            }
          }
          if (current) throw new ApiError(409, reason.message, { code: detail.code, current });
        }
      }
      throw reason;
    }
  }, [invalidateImageAnnotations, loadedImage, updateImage]);

  const annotation = useAnnotationEdit([], saveAnnotations, {
    readOnly, editBlocked: busy || renameReloadPending, onAssignmentConflict: () => setForcedReadOnly(true),
  });
  const { flush, reset, setCatalogVersion } = annotation;

  useEffect(() => {
    let active = true;
    resetAnnotationRequests();
    datasetContextRef.current += 1;
    renameReloadRef.current = null;
    setRenameReloadPending(false);
    busyRef.current = false;
    setBusy(false);
    appliedCatalogVersionRef.current = -1;
    classRowsRef.current = [];
    setDataset(null);
    setMembers([]);
    setForcedReadOnly(false);
    setClaimEmpty(false);
    setClassRows([]);
    setItems(new Map());
    setTotal(0);
    setSplit(null);
    setImageIndex(0);
    setLoadedImage(null);
    setColors({});
    setError(null);
    rangeGenerationRef.current += 1;
    requestedRangesRef.current.clear();
    reset([]);
    void Promise.all([getDataset(datasetId), getDatasetClasses(datasetId)])
      .then(([detail, classes]) => {
        if (!active) return;
        setDataset(detail);
        void getWorkspaceMembers(detail.workspace_id).then(({ items: rows }) => {
          if (active) setMembers(rows);
        }).catch(() => {});
        applyClassCatalog(classes);
        setTotal(detail.image_count);
        setError(null);
        // 클래스 대표색 정본 = 프로젝트 카탈로그
        void getProject(detail.project_id).then((project) => {
          if (!active) return;
          setProjectName(project.name);
          setColors(Object.fromEntries(
            classRowsRef.current.map((item, index) => [String(item.class_id),
              project.classes.find((row) => row.class_id === item.class_id)?.color
                ?? CLASS_COLOR_PRESETS[index % CLASS_COLOR_PRESETS.length].value]),
          ));
        }).catch(() => {});
      })
      .catch((reason: unknown) => {
        if (active) setError(reason instanceof Error ? reason.message : "데이터셋을 불러오지 못했습니다.");
      });
    return () => {
      active = false;
      datasetContextRef.current += 1;
      resetAnnotationRequests();
    };
  }, [applyClassCatalog, datasetId, reset, resetAnnotationRequests]);

  const requestRange = useCallback((from: number, to: number) => {
    if (to <= from) return;
    const key = `${split ?? "all"}:${from}:${to}`;
    if (requestedRangesRef.current.has(key)) return;
    requestedRangesRef.current.add(key);
    const generation = rangeGenerationRef.current;
    void getDatasetImages(datasetId, from, to - from, split)
      .then((page) => {
        if (generation !== rangeGenerationRef.current) return;
        setTotal(page.total);
        setItems((current) => {
          const next = new Map(current);
          page.items.forEach((item, offset) => next.set(from + offset, item));
          return next;
        });
      })
      .catch((reason: unknown) => {
        if (generation !== rangeGenerationRef.current) return;
        requestedRangesRef.current.delete(key);
        setError(reason instanceof Error ? reason.message : "이미지 목록을 불러오지 못했습니다.");
      });
  }, [datasetId, split]);

  useEffect(() => {
    if (!dataset || total === 0) return;
    requestRange(Math.max(0, imageIndex - 20), Math.min(total, imageIndex + 21));
  }, [dataset, imageIndex, requestRange, total]);

  const currentImage = items.get(imageIndex);
  const currentImageId = currentImage?.id;
  const currentImageResource = useAuthenticatedObjectUrl(
    currentImageId === undefined ? null : imageResourceUrl(currentImageId),
  );

  useEffect(() => {
    if (currentImageId === undefined) return;
    let active = true;
    const request = navigationRef.current;
    setLoadedImage(null);
    void loadImageAnnotations(currentImageId)
      .then((response) => {
        if (!active || request !== navigationRef.current) return;
        const image = { ...items.get(imageIndex)!, ...response, id: currentImageId };
        applyClassCatalog(response);
        reset(response.boxes.map((box) => fromDto(box, response.width, response.height)), response.version, appliedCatalogVersionRef.current);
        setForcedReadOnly(false);
        setLoadedImage(image);
        setError(null);
      })
      .catch((reason: unknown) => {
        if (active) setError(reason instanceof Error ? reason.message : "어노테이션을 불러오지 못했습니다.");
      });
    return () => { active = false; };
  }, [applyClassCatalog, currentImageId, loadImageAnnotations, reset]);

  useEffect(() => {
    if (loadedImage?.id !== currentImageId || !currentImageResource.url) return;
    return scheduleIdleWork(() => {
      for (let offset = -PREFETCH_RADIUS; offset <= PREFETCH_RADIUS; offset += 1) {
        if (offset === 0) continue;
        const image = items.get(imageIndex + offset);
        if (!image) continue;
        void prefetchAuthenticatedResource(imageResourceUrl(image.id))
          .catch(() => undefined);
        void loadImageAnnotations(image.id)
          .catch(() => undefined);
      }
    });
  }, [
    currentImageId,
    currentImageResource.url,
    imageIndex,
    items,
    loadImageAnnotations,
    loadedImage?.id,
  ]);

  const navigate = useCallback(async (target: number) => {
    if (busyRef.current || total === 0) return;
    const next = clamp(target, 0, total - 1);
    if (next === imageIndex) return;
    const request = navigationRef.current + 1;
    navigationRef.current = request;
    if (!await flush() || request !== navigationRef.current) return;
    setLoadedImage(null);
    reset([]);
    setImageIndex(next);
  }, [flush, imageIndex, reset, total]);

  const navigateOffset = useCallback((offset: -1 | 1) => {
    void navigate(imageIndex + offset);
  }, [imageIndex, navigate]);

  const leaveViewer = async (event: MouseEvent<HTMLAnchorElement>) => {
    if (event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    if (!busyRef.current && await flush()) window.location.assign(appHref(homePath));
  };

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (isTypingTarget(event.target) || event.ctrlKey || event.metaKey || event.altKey) return;
      if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
        event.preventDefault();
        void navigate(imageIndex + (event.key === "ArrowRight" ? 1 : -1));
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [imageIndex, navigate]);

  const changeSplit = async (nextSplit: string | null) => {
    if (busyRef.current || !dataset || nextSplit === split || !await flush()) return;
    navigationRef.current += 1;
    setLoadedImage(null);
    reset([]);
    rangeGenerationRef.current += 1;
    requestedRangesRef.current.clear();
    setItems(new Map());
    setSplit(nextSplit);
    setImageIndex(0);
    setTotal(nextSplit === null
      ? dataset.image_count
      : dataset.splits.find((item) => item.split === nextSplit)?.image_count ?? 0);
  };

  const retryRenameReload = useCallback(async () => {
    const reload = renameReloadRef.current;
    if (!reload || busy) return;
    const context = datasetContextRef.current;
    busyRef.current = true;
    setBusy(true);
    try {
      await reload();
    } catch {
      // The reload retains its recovery action and reports the failure inline.
    } finally {
      if (context === datasetContextRef.current) {
        busyRef.current = renameReloadRef.current !== null;
        setBusy(false);
      }
    }
  }, [busy]);

  const renameClass = useCallback(async (classId: number, name: string) => {
    const failure = "저장하지 못해 이름을 바꾸지 않았어요";
    if (!canManage || !dataset || !loadedImage || busyRef.current) throw new Error(failure);
    const context = datasetContextRef.current;
    const image = loadedImage;
    const workspaceId = dataset.workspace_id;
    const expectedCatalogVersion = annotation.catalogVersion;
    const prepareReload = (failureMessage: string, successMessage: string | null = null) => {
      const reload = async () => {
        if (context !== datasetContextRef.current) return;
        try {
          invalidateImageAnnotations(image.id);
          const response = await getImageAnnotations(image.id, undefined, workspaceId);
          if (context !== datasetContextRef.current) return;
          applyClassCatalog(response);
          annotationCacheRef.current.set(image.id, response);
          reset(response.boxes.map((box) => fromDto(box, response.width, response.height)),
            response.version, appliedCatalogVersionRef.current);
          updateImage(image.id, { ...response, box_count: response.boxes.length });
          renameReloadRef.current = null;
          setRenameReloadPending(false);
          setError(successMessage);
        } catch {
          if (context !== datasetContextRef.current) return;
          setError(failureMessage);
          throw new Error(failureMessage);
        }
      };
      renameReloadRef.current = reload;
      setRenameReloadPending(true);
      return reload;
    };
    busyRef.current = true;
    setBusy(true);
    setError(null);
    try {
      const saved = await flush({ requireSaved: true });
      if (context !== datasetContextRef.current) return;
      if (!saved) throw new Error(failure);
      let renamed: ClassCatalog;
      try {
        renamed = await renameDatasetClass(datasetId, classId, name, expectedCatalogVersion, workspaceId);
      } catch (reason) {
        if (context !== datasetContextRef.current) return;
        if (reason instanceof ApiError && reason.status === 409
          && (reason.detail as { code?: string } | undefined)?.code === "class_catalog_changed") {
          const message = "클래스 목록이 바뀌어 새로 불러왔어요. 이름 변경을 다시 열어 주세요.";
          await prepareReload("클래스 목록을 새로 불러오지 못했어요. 다시 시도해 주세요.", message)();
          if (context !== datasetContextRef.current) return;
          throw new Error(message);
        }
        throw reason;
      }
      if (context !== datasetContextRef.current) return;
      const reload = prepareReload("이름은 바꿨지만 새로 불러오지 못했어요. 다시 시도해 주세요.");
      applyClassCatalog(renamed);
      setCatalogVersion(appliedCatalogVersionRef.current);
      // Editing stays blocked until boxes and their catalog share a fresh snapshot.
      await reload();
    } finally {
      if (context === datasetContextRef.current) {
        busyRef.current = renameReloadRef.current !== null;
        setBusy(false);
      }
    }
  }, [annotation.catalogVersion, applyClassCatalog, canManage, dataset, datasetId,
    flush, invalidateImageAnnotations, loadedImage, reset, setCatalogVersion, updateImage]);

  const changeClassColor = useCallback((classId: number, color: string) => {
    if (!canManage) return;
    setColors((current) => ({ ...current, [classId]: color }));
    const projectId = dataset?.project_id;
    if (projectId === undefined) return;
    void updateProjectClass(projectId, classId, { color }).catch(() => {
      setError("클래스 색을 저장하지 못했습니다.");
    });
  }, [canManage, dataset?.project_id]);

  const classes = useMemo<EditorClass[]>(() => classRows.map((row, index) => ({
    id: row.class_id,
    name: row.name,
    color: colors[row.class_id]
      ?? CLASS_COLOR_PRESETS[index % CLASS_COLOR_PRESETS.length].value,
    count: annotation.boxes.filter((box) => box.classId === row.class_id).length,
  })), [annotation.boxes, classRows, colors]);

  const runImageAction = async (operation: (workspaceId: number) => Promise<void>) => {
    if (!dataset || busyRef.current) return;
    busyRef.current = true; setBusy(true); setError(null);
    try { if (await flush()) await operation(dataset.workspace_id); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "요청하지 못했어요"); }
    finally { busyRef.current = false; setBusy(false); }
  };
  const refreshImage = async (image: ImageRow, workspaceId: number, position = imageIndex) => {
    invalidateImageAnnotations(image.id);
    const response = await getImageAnnotations(image.id, undefined, workspaceId);
    navigationRef.current += 1;
    annotationCacheRef.current.set(image.id, response);
    setItems((current) => new Map(current).set(position, { ...image, ...response }));
    setImageIndex(position);
    applyClassCatalog(response);
    reset(response.boxes.map((box) => fromDto(box, response.width, response.height)), response.version, appliedCatalogVersionRef.current);
    setLoadedImage({ ...image, ...response });
    setForcedReadOnly(false);
  };
  const claimNext = () => runImageAction(async (workspaceId) => {
    if (!canEdit) return;
    const response = await claimDatasetImage(datasetId, split, workspaceId);
    setClaimEmpty(!response.image);
    if (response.image && response.position !== undefined) await refreshImage(response.image, workspaceId, response.position);
  });
  const complete = (undo: boolean) => runImageAction(async (workspaceId) => {
    if (!loadedImage || !canEdit) return;
    const image = await (undo ? uncompleteImage : completeImage)(loadedImage.id, workspaceId);
    await refreshImage(image, workspaceId);
  });
  const changeAssignee = (assignee: number | null) => runImageAction(async (workspaceId) => {
    if (!loadedImage || !canManage) return;
    const image = await assignImage(loadedImage.id, assignee, workspaceId);
    await refreshImage(image, workspaceId);
    setChangingAssignee(false);
  });
  const localClassesMatch = annotation.conflict?.code === "version_conflict"
    && hasClassCatalog(annotation.conflict.current)
    && annotation.conflict.current.catalog_version === annotation.catalogVersion;
  const resolveConflict = async (choice: "server" | "local") => {
    if (!loadedImage || !dataset || busyRef.current) return;
    if (choice === "local" && !localClassesMatch) { setError(CLASS_MAPPING_ERROR); return; }
    const context = datasetContextRef.current;
    busyRef.current = true; setBusy(true); setError(null);
    try {
      invalidateImageAnnotations(loadedImage.id);
      const response = await getImageAnnotations(loadedImage.id, undefined, dataset.workspace_id);
      const metadata = await getImage(loadedImage.id, dataset.workspace_id);
      if (context !== datasetContextRef.current) return;
      if (!hasClassCatalog(response)) { setError(CLASS_MAPPING_ERROR); return; }
      const server = { version: response.version, catalog_version: response.catalog_version,
        boxes: response.boxes.map((box) => fromDto(box, response.width, response.height)) };
      if (choice === "local" && response.label_status === "in_progress" && response.assignee_id !== user?.id && !canManage) {
        setForcedReadOnly(true);
        setError("다른 사람이 작업 중이에요. 서버 것으로 불러와 주세요.");
        updateImage(loadedImage.id, metadata);
        return;
      }
      if (choice === "local" && response.catalog_version !== annotation.catalogVersion) {
        setError(CLASS_MAPPING_ERROR);
        return;
      }
      const adopted = await annotation.resolveConflict(choice, server);
      if (!adopted || context !== datasetContextRef.current) return;
      // Adoption and catalog application belong to the same dataset context;
      // cache invalidation must never skip the latter after the hook resets.
      applyClassCatalog(response);
      if (choice === "server") {
        annotationCacheRef.current.set(loadedImage.id, response);
        updateImage(loadedImage.id, { ...metadata, ...response });
      }
    } catch (reason) { setError(reason instanceof Error ? reason.message : "최신 라벨을 불러오지 못했어요"); }
    finally { busyRef.current = false; setBusy(false); }
  };
  const status = loadedImage?.label_status;
  const completedName = memberName(loadedImage?.labeled_by);
  const completedDate = loadedImage?.labeled_at ? new Date(loadedImage.labeled_at).toLocaleDateString("ko-KR", { month: "numeric", day: "numeric" }) : "";
  const statusText = status === "labeled" ? (loadedImage?.labeled_by == null ? "✓ 가져온 라벨" : `✓ 완료${completedName ? ` · ${completedName}` : ""}${completedDate ? ` · ${completedDate}` : ""}`)
    : status === "in_progress" ? `● ${loadedImage?.assignee_id === user?.id ? "나" : memberName(loadedImage?.assignee_id) || "다른 작업자"} · 작업 중` : "미배정";
  const canComplete = !readOnly && (status === "unlabeled" || (status === "in_progress" && loadedImage?.assignee_id === user?.id));
  const canUncomplete = status === "labeled" && canEdit && (canManage || loadedImage?.labeled_by === user?.id);
  const imageInspector = <section><h2>이 이미지</h2><p>{statusText}</p>
    {status === "in_progress" ? <p>담당 · {memberName(loadedImage?.assignee_id) || "이름 없음"}</p> : null}
    {loadedImage?.labeled_at ? <p>완료 {completedName} · {new Date(loadedImage.labeled_at).toLocaleString("ko-KR")}</p> : null}
    {canManage && status !== "labeled" ? <button className="btn btn-ghost btn-sm" disabled={busy} onClick={() => setChangingAssignee(!changingAssignee)}>바꾸기 ⌄</button> : null}
    {canManage && changingAssignee && status !== "labeled" ? <select className="select" aria-label="이미지 담당 변경" disabled={busy} value={loadedImage?.assignee_id ?? ""} onChange={(event) => void changeAssignee(event.target.value ? Number(event.target.value) : null)}>
      <option value="">회수</option>{members.filter((member) => ["owner", "admin", "editor"].includes(member.role)).map((member) => <option key={member.user_id} value={member.user_id}>{memberName(member.user_id)}</option>)}</select> : null}
  </section>;

  return (
    <main className="viewer-screen" data-screen-label="07 라벨링 에디터">
      <header className="viewer-header">
        <a className="viewer-brand" href={appHref(homePath)} onClick={(event) => void leaveViewer(event)}><Brand /></a>
        <div className="breadcrumbs"><span>{projectName ?? ""}</span><span>/</span><strong>{dataset?.name ?? `데이터셋 ${datasetId}`}</strong></div>
        <div className="viewer-filters">
          <button className="chip" type="button" aria-pressed={split === null} onClick={() => void changeSplit(null)}>전체 <b>{dataset?.image_count ?? 0}</b></button>
          {dataset?.splits.map((item) => <button className="chip" type="button" key={item.split} aria-pressed={split === item.split} onClick={() => void changeSplit(item.split)}>{item.split} <b>{item.image_count}</b></button>)}
        </div>
        <ThemeToggle />
        {canEdit ? <button className="btn btn-primary" disabled={busy} onClick={() => void claimNext()}>다음 이미지 →</button> : null}
      </header>

      {claimEmpty ? <div className="banner-info" role="status">지금 가져올 이미지가 없어요 <button className="btn btn-ghost" disabled={busy} onClick={() => void claimNext()}>다시 시도</button></div> : null}
      {loadedImage && canEdit && readOnly ? <div className="banner-info">{`${memberName(loadedImage.assignee_id) || "다른 작업자"} 님이 작업 중인 이미지예요 — 보기만 할 수 있어요`}</div> : null}
      {currentImage && loadedImage?.id === currentImage.id && currentImageResource.url ? (
        <BboxCanvas
          readOnly={readOnly || busy || renameReloadPending || !!annotation.conflict}
          catalogVersion={appliedCatalogVersionRef.current}
          canManageClasses={canManage}
          statusBadge={<span className={`tag ${status === "labeled" ? "tag-ok" : status === "in_progress" ? (loadedImage.assignee_id === user?.id ? "tag-accent" : "tag-warn") : "tag-neutral"}`} title={loadedImage.labeled_at ? new Date(loadedImage.labeled_at).toLocaleString("ko-KR") : undefined}>{statusText}</span>}
          imageInspector={imageInspector}
          completionAction={canComplete ? <button className="btn btn-primary" disabled={busy} onClick={() => void complete(false)}>완료 ✓</button> : canUncomplete ? <button className="btn btn-secondary" disabled={busy} onClick={() => void complete(true)}>완료 취소</button> : null}
          imageSrc={currentImageResource.url}
          imageWidth={loadedImage.width}
          imageHeight={loadedImage.height}
          boxes={annotation.boxes}
          classes={classes}
          saveState={annotation.saveState}
          undoDepth={annotation.undoDepth}
          onChange={annotation.applyChange}
          onUndo={annotation.undo}
          onRetrySave={annotation.retry}
          onNavigate={navigateOffset}
          onRenameClass={renameClass}
          onChangeClassColor={changeClassColor}
        />
      ) : (
        <div className="viewer-body"><section className="viewer-stage">{error ?? currentImageResource.error ?? (total === 0 && dataset ? "이미지가 없습니다." : "이미지를 불러오는 중…")}</section></div>
      )}

      {error && currentImage ? <div className="viewer-inline-error" role="alert">{error}
        {renameReloadPending ? <button className="btn btn-ghost" disabled={busy} onClick={() => void retryRenameReload()}>다시 불러오기</button> : null}
      </div> : null}
      {annotation.saveError ? <div className="viewer-inline-error" role="alert">{annotation.saveError}</div> : null}
      <ThumbStrip
        total={total}
        currentIndex={imageIndex}
        items={items}
        onSelect={(index) => void navigate(index)}
        onNavigate={navigateOffset}
        onRangeRequest={requestRange}
        statusLabel={(image) => image.label_status === "labeled" ? "✓" : image.label_status === "in_progress" ? (image.assignee_id === user?.id ? "나" : memberName(image.assignee_id) || "작업 중") : "—"}
      />
      {annotation.conflict ? <WorkspaceDialog title="그사이 다른 저장이 있었어요" busy={busy} onClose={() => {}}>
        <p>같은 이미지를 다른 탭이나 다른 사람이 먼저 저장했어요. 어느 쪽을 남길까요?</p>
        <div className="compare"><p>내 편집 박스 {annotation.boxes.length}개</p><p>서버 박스 {annotation.conflict.current.boxes.length}개</p></div>
        {!localClassesMatch ? <p role="alert">{CLASS_MAPPING_ERROR}</p> : error ? <p role="alert">{error}</p> : null}
        <div className="dialog-actions"><button className="btn btn-secondary" disabled={busy} onClick={() => void resolveConflict("server")}>서버 것으로</button>
          <button className="btn btn-primary" disabled={busy || readOnly || !localClassesMatch || error === CLASS_MAPPING_ERROR} onClick={() => void resolveConflict("local")}>내 것 유지</button></div>
      </WorkspaceDialog> : null}
    </main>
  );
}
