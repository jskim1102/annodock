import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
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
  type DatasetDetail,
  type ImageRow,
} from "../api/client";
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

interface ViewerPageProps {
  datasetId: number;
}

interface ActiveImage {
  id: number;
  width: number;
  height: number;
}

interface AnnotationFlight {
  controller: AbortController;
  generation: number;
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

function isTypingTarget(target: EventTarget | null) {
  return target instanceof HTMLElement && (
    target.isContentEditable
    || target.tagName === "INPUT"
    || target.tagName === "TEXTAREA"
    || target.tagName === "SELECT"
  );
}

export function ViewerPage({ datasetId }: ViewerPageProps) {
  const activeImageRef = useRef<ActiveImage | null>(null);
  const annotationCacheRef = useRef(new Map<number, AnnotationResponse>());
  const annotationFlightsRef = useRef(new Map<number, AnnotationFlight>());
  const annotationGenerationRef = useRef(0);
  const requestedRangesRef = useRef(new Set<string>());
  const rangeGenerationRef = useRef(0);
  const navigationRef = useRef(0);
  const [dataset, setDataset] = useState<DatasetDetail | null>(null);
  const [classRows, setClassRows] = useState<ClassRow[]>([]);
  const [projectName, setProjectName] = useState<string | null>(null);
  const [colors, setColors] = useState<Record<string, string>>({});
  const [items, setItems] = useState<Map<number, ImageRow>>(new Map());
  const [total, setTotal] = useState(0);
  const [split, setSplit] = useState<string | null>(null);
  const [imageIndex, setImageIndex] = useState(0);
  const [loadedImage, setLoadedImage] = useState<ActiveImage | null>(null);
  const [error, setError] = useState<string | null>(null);

  const resetAnnotationRequests = useCallback(() => {
    annotationGenerationRef.current += 1;
    annotationFlightsRef.current.forEach(({ controller }) => controller.abort());
    annotationFlightsRef.current.clear();
    annotationCacheRef.current.clear();
  }, []);

  const loadImageAnnotations = useCallback((imageId: number): Promise<AnnotationResponse> => {
    const cached = annotationCacheRef.current.get(imageId);
    if (cached) return Promise.resolve(cached);

    const existing = annotationFlightsRef.current.get(imageId);
    if (existing) return existing.promise;

    const controller = new AbortController();
    const generation = annotationGenerationRef.current;
    const flight: AnnotationFlight = {
      controller,
      generation,
      promise: Promise.resolve(null as never),
    };
    flight.promise = getImageAnnotations(imageId, controller.signal)
      .then((response) => {
        if (generation !== annotationGenerationRef.current) {
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

  const saveAnnotations = useCallback(async (boxes: EditorBox[]) => {
    const active = activeImageRef.current;
    if (!active) return;
    const response = await saveImageAnnotations(
      active.id,
      boxes.map((box) => toDto(box, active.width, active.height)),
    );
    annotationCacheRef.current.set(active.id, {
      image_id: active.id,
      width: active.width,
      height: active.height,
      boxes: response.boxes,
    });
    setItems((current) => {
      const next = new Map(current);
      for (const [index, image] of next) {
        if (image.id === active.id) {
          next.set(index, {
            ...image,
            box_count: response.boxes.length,
            is_modified: response.is_modified,
          });
          break;
        }
      }
      return next;
    });
  }, []);

  const annotation = useAnnotationEdit([], saveAnnotations);
  const { flush, reset } = annotation;

  useEffect(() => {
    let active = true;
    resetAnnotationRequests();
    setDataset(null);
    setClassRows([]);
    setItems(new Map());
    setTotal(0);
    setSplit(null);
    setImageIndex(0);
    setLoadedImage(null);
    setColors({});
    setError(null);
    activeImageRef.current = null;
    rangeGenerationRef.current += 1;
    requestedRangesRef.current.clear();
    reset([]);
    void Promise.all([getDataset(datasetId), getDatasetClasses(datasetId)])
      .then(([detail, classes]) => {
        if (!active) return;
        setDataset(detail);
        setClassRows(classes.classes);
        setTotal(detail.image_count);
        setError(null);
        // 클래스 대표색 정본 = 프로젝트 카탈로그
        void getProject(detail.project_id).then((project) => {
          if (!active) return;
          setProjectName(project.name);
          setColors(Object.fromEntries(
            project.classes.map((item) => [String(item.class_id), item.color]),
          ));
        }).catch(() => {});
      })
      .catch((reason: unknown) => {
        if (active) setError(reason instanceof Error ? reason.message : "데이터셋을 불러오지 못했습니다.");
      });
    return () => {
      active = false;
      resetAnnotationRequests();
    };
  }, [datasetId, reset, resetAnnotationRequests]);

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
        const image = { id: currentImageId, width: response.width, height: response.height };
        activeImageRef.current = image;
        reset(response.boxes.map((box) => fromDto(box, response.width, response.height)));
        setLoadedImage(image);
        setError(null);
      })
      .catch((reason: unknown) => {
        if (active) setError(reason instanceof Error ? reason.message : "어노테이션을 불러오지 못했습니다.");
      });
    return () => { active = false; };
  }, [currentImageId, loadImageAnnotations, reset]);

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
    if (total === 0) return;
    const next = clamp(target, 0, total - 1);
    if (next === imageIndex) return;
    const request = navigationRef.current + 1;
    navigationRef.current = request;
    if (!await flush() || request !== navigationRef.current) return;
    activeImageRef.current = null;
    setLoadedImage(null);
    reset([]);
    setImageIndex(next);
  }, [flush, imageIndex, reset, total]);

  const navigateOffset = useCallback((offset: -1 | 1) => {
    void navigate(imageIndex + offset);
  }, [imageIndex, navigate]);

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
    if (!dataset || nextSplit === split || !await flush()) return;
    navigationRef.current += 1;
    activeImageRef.current = null;
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

  const renameClass = useCallback(async (classId: number, name: string) => {
    const renamed = await renameDatasetClass(datasetId, classId, name);
    setClassRows((current) => current.map((item) => item.class_id === classId ? renamed : item));
  }, [datasetId]);

  const changeClassColor = useCallback((classId: number, color: string) => {
    setColors((current) => ({ ...current, [classId]: color }));
    const projectId = dataset?.project_id;
    if (projectId === undefined) return;
    void updateProjectClass(projectId, classId, { color }).catch(() => {
      setError("클래스 색을 저장하지 못했습니다.");
    });
  }, [dataset?.project_id]);

  const classes = useMemo<EditorClass[]>(() => classRows.map((row, index) => ({
    id: row.class_id,
    name: row.name,
    color: colors[row.class_id]
      ?? CLASS_COLOR_PRESETS[index % CLASS_COLOR_PRESETS.length].value,
    count: annotation.boxes.filter((box) => box.classId === row.class_id).length,
  })), [annotation.boxes, classRows, colors]);

  return (
    <main className="viewer-screen" data-screen-label="07 라벨링 에디터">
      <header className="viewer-header">
        <a className="viewer-brand" href={appHref("/projects")}><Brand /></a>
        <div className="breadcrumbs"><span>{projectName ?? ""}</span><span>/</span><strong>{dataset?.name ?? `데이터셋 ${datasetId}`}</strong></div>
        <div className="viewer-filters">
          <button className="chip" type="button" aria-pressed={split === null} onClick={() => void changeSplit(null)}>전체 <b>{dataset?.image_count ?? 0}</b></button>
          {dataset?.splits.map((item) => <button className="chip" type="button" key={item.split} aria-pressed={split === item.split} onClick={() => void changeSplit(item.split)}>{item.split} <b>{item.image_count}</b></button>)}
        </div>
        <ThemeToggle />
      </header>

      {currentImage && loadedImage?.id === currentImage.id && currentImageResource.url ? (
        <BboxCanvas
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

      {error && currentImage ? <div className="viewer-inline-error" role="alert">{error}</div> : null}
      <ThumbStrip
        total={total}
        currentIndex={imageIndex}
        items={items}
        onSelect={(index) => void navigate(index)}
        onNavigate={navigateOffset}
        onRangeRequest={requestRange}
      />
    </main>
  );
}
