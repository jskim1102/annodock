import {
  type FormEvent,
  type PointerEvent as ReactPointerEvent,
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";

import type { AnnotationSaveState, EditorBox } from "../hooks/useAnnotationEdit";
import { ClassColorPicker } from "./ClassColorPicker";
import { Icon } from "./Icon";

export interface EditorClass {
  id: number;
  name: string;
  count: number;
  color: string;
}

interface BboxCanvasProps {
  imageSrc: string;
  imageWidth: number;
  imageHeight: number;
  boxes: EditorBox[];
  classes: EditorClass[];
  saveState: AnnotationSaveState;
  undoDepth: number;
  onChange: (boxes: EditorBox[]) => void;
  onUndo: () => void;
  onRetrySave: () => void;
  onNavigate: (offset: -1 | 1) => void;
  onRenameClass: (classId: number, name: string) => Promise<void>;
  onChangeClassColor: (classId: number, color: string) => void;
}

type Tool = "select" | "draw";
type HandleName = "nw" | "n" | "ne" | "e" | "se" | "s" | "sw" | "w";

type DragState =
  | {
      kind: "move";
      pointerId: number;
      startX: number;
      startY: number;
      box: EditorBox;
      boxes: EditorBox[];
    }
  | {
      kind: "resize";
      pointerId: number;
      startX: number;
      startY: number;
      handle: HandleName;
      box: EditorBox;
      boxes: EditorBox[];
    }
  | {
      kind: "draw";
      pointerId: number;
      startX: number;
      startY: number;
      classId: number;
      boxes: EditorBox[];
    }
  | {
      kind: "pan";
      pointerId: number;
      startClientX: number;
      startClientY: number;
      originX: number;
      originY: number;
    };

interface DraftBox {
  x: number;
  y: number;
  width: number;
  height: number;
}

const FIT_ZOOM = 148;
const MIN_BOX_SIZE = 4;
const HANDLES: readonly HandleName[] = ["nw", "n", "ne", "e", "se", "s", "sw", "w"];

function clamp(value: number, minimum: number, maximum: number) {
  return Math.min(maximum, Math.max(minimum, value));
}

function isTypingTarget(target: EventTarget | null) {
  return target instanceof HTMLElement && (
    target.isContentEditable
    || target.tagName === "INPUT"
    || target.tagName === "TEXTAREA"
    || target.tagName === "SELECT"
  );
}

function movedBox(
  box: EditorBox,
  deltaX: number,
  deltaY: number,
  imageWidth: number,
  imageHeight: number,
): EditorBox {
  return {
    ...box,
    x: clamp(box.x + deltaX, 0, imageWidth - box.width),
    y: clamp(box.y + deltaY, 0, imageHeight - box.height),
  };
}

function resizedBox(
  box: EditorBox,
  handle: HandleName,
  deltaX: number,
  deltaY: number,
  imageWidth: number,
  imageHeight: number,
  minimumBoxSize: number,
): EditorBox {
  let left = box.x;
  let top = box.y;
  let right = box.x + box.width;
  let bottom = box.y + box.height;

  if (handle.includes("w")) left = clamp(box.x + deltaX, 0, right - minimumBoxSize);
  if (handle.includes("e")) right = clamp(box.x + box.width + deltaX, left + minimumBoxSize, imageWidth);
  if (handle.includes("n")) top = clamp(box.y + deltaY, 0, bottom - minimumBoxSize);
  if (handle.includes("s")) bottom = clamp(box.y + box.height + deltaY, top + minimumBoxSize, imageHeight);

  return { ...box, x: left, y: top, width: right - left, height: bottom - top };
}

function boxesChanged(before: EditorBox[], after: EditorBox[]) {
  if (before.length !== after.length) return true;
  return before.some((box, index) => {
    const next = after[index];
    return !next
      || box.id !== next.id
      || box.classId !== next.classId
      || box.x !== next.x
      || box.y !== next.y
      || box.width !== next.width
      || box.height !== next.height;
  });
}

export function BboxCanvas({
  imageSrc,
  imageWidth,
  imageHeight,
  boxes,
  classes,
  saveState,
  undoDepth,
  onChange,
  onUndo,
  onRetrySave,
  onNavigate,
  onRenameClass,
  onChangeClassColor,
}: BboxCanvasProps) {
  const stageRef = useRef<HTMLElement>(null);
  const frameRef = useRef<HTMLDivElement>(null);
  const dragRef = useRef<DragState | null>(null);
  const previewRef = useRef<EditorBox[] | null>(null);
  const draftRef = useRef<DraftBox | null>(null);
  const nextBoxIdRef = useRef(Math.max(0, ...boxes.map((box) => box.id)) + 1);
  const spaceDownRef = useRef(false);
  const [tool, setTool] = useState<Tool>("select");
  const [selectedId, setSelectedId] = useState(0);
  const [selectedClass, setSelectedClass] = useState(boxes[0]?.classId ?? classes[0]?.id ?? 0);
  const [previewBoxes, setPreviewBoxes] = useState<EditorBox[] | null>(null);
  const [draft, setDraft] = useState<DraftBox | null>(null);
  const [zoom, setZoom] = useState(FIT_ZOOM);
  const [pan, setPan] = useState({ x: 0, y: 0 });
  const [spaceDown, setSpaceDown] = useState(false);
  const [isPanning, setIsPanning] = useState(false);
  const [rename, setRename] = useState<{ id: number; value: string } | null>(null);
  const [renameBusy, setRenameBusy] = useState(false);
  const [renameError, setRenameError] = useState("");
  const [colorClassId, setColorClassId] = useState<number | null>(null);
  const [fitScale, setFitScale] = useState(1);

  const renderedBoxes = previewBoxes ?? boxes;
  const visualScale = fitScale * (zoom / FIT_ZOOM);
  const minimumBoxSize = MIN_BOX_SIZE / Math.max(visualScale, 0.001);

  useEffect(() => {
    const stage = stageRef.current;
    if (!stage) return;
    const updateFit = () => {
      const bounds = stage.getBoundingClientRect();
      const next = Math.min(
        Math.max(1, bounds.width - 32) / imageWidth,
        Math.max(1, bounds.height - 32) / imageHeight,
      );
      setFitScale(Math.max(0.001, next));
    };
    updateFit();
    const observer = new ResizeObserver(updateFit);
    observer.observe(stage);
    setZoom(FIT_ZOOM);
    setPan({ x: 0, y: 0 });
    return () => observer.disconnect();
  }, [imageHeight, imageWidth]);

  useEffect(() => {
    nextBoxIdRef.current = Math.max(nextBoxIdRef.current, Math.max(0, ...boxes.map((box) => box.id)) + 1);
    const selected = boxes.find((box) => box.id === selectedId);
    if (!selected) {
      if (selectedId !== 0) setSelectedId(0);
    } else if (selected.classId !== selectedClass) {
      setSelectedClass(selected.classId);
    }
  }, [boxes, selectedClass, selectedId]);

  const pointInImage = useCallback((clientX: number, clientY: number) => {
    const bounds = frameRef.current?.getBoundingClientRect();
    if (!bounds || bounds.width === 0 || bounds.height === 0) return { x: 0, y: 0 };
    return {
      x: clamp((clientX - bounds.left) * imageWidth / bounds.width, 0, imageWidth),
      y: clamp((clientY - bounds.top) * imageHeight / bounds.height, 0, imageHeight),
    };
  }, [imageHeight, imageWidth]);

  const capturePointer = (pointerId: number) => {
    try {
      stageRef.current?.setPointerCapture(pointerId);
    } catch {
      // A pointer can end before capture when the window loses focus.
    }
  };

  const releasePointer = (pointerId: number) => {
    try {
      if (stageRef.current?.hasPointerCapture(pointerId)) stageRef.current.releasePointerCapture(pointerId);
    } catch {
      // The browser may already have released capture.
    }
  };

  const beginPan = (event: ReactPointerEvent) => {
    dragRef.current = {
      kind: "pan",
      pointerId: event.pointerId,
      startClientX: event.clientX,
      startClientY: event.clientY,
      originX: pan.x,
      originY: pan.y,
    };
    setIsPanning(true);
    capturePointer(event.pointerId);
  };

  const beginBoxDrag = (event: ReactPointerEvent, box: EditorBox) => {
    if (event.button !== 0 || tool !== "select") return;
    event.preventDefault();
    event.stopPropagation();
    if (spaceDownRef.current) {
      beginPan(event);
      return;
    }
    const point = pointInImage(event.clientX, event.clientY);
    setSelectedId(box.id);
    setSelectedClass(box.classId);
    dragRef.current = {
      kind: "move",
      pointerId: event.pointerId,
      startX: point.x,
      startY: point.y,
      box,
      boxes,
    };
    previewRef.current = boxes;
    setPreviewBoxes(boxes);
    capturePointer(event.pointerId);
  };

  const beginResize = (event: ReactPointerEvent, box: EditorBox, handle: HandleName) => {
    if (event.button !== 0 || tool !== "select") return;
    event.preventDefault();
    event.stopPropagation();
    if (spaceDownRef.current) {
      beginPan(event);
      return;
    }
    const point = pointInImage(event.clientX, event.clientY);
    setSelectedId(box.id);
    setSelectedClass(box.classId);
    dragRef.current = {
      kind: "resize",
      pointerId: event.pointerId,
      startX: point.x,
      startY: point.y,
      handle,
      box,
      boxes,
    };
    previewRef.current = boxes;
    setPreviewBoxes(boxes);
    capturePointer(event.pointerId);
  };

  const onStagePointerDown = (event: ReactPointerEvent<HTMLElement>) => {
    if (event.button !== 0) return;
    if (spaceDownRef.current) {
      event.preventDefault();
      beginPan(event);
      return;
    }
    // 버튼 등 UI 요소 위에서는 팬/선택해제를 시작하지 않는다 —
    // 스테이지가 포인터를 캡처하면 버튼 click 이 죽는다.
    if (event.target instanceof Element && event.target.closest("button, a, input, select, textarea")) {
      return;
    }
    // 경계상자 밖(배경) 클릭 = 선택 해제
    const onBackground = !(event.target instanceof Element) || !event.target.closest(".editor-box");
    if (onBackground) {
      setSelectedId(0);
    }
    // 마우스(select) 모드에서 배경 드래그 = 팬
    if (tool === "select" && onBackground) {
      event.preventDefault();
      beginPan(event);
      return;
    }
    if (tool !== "draw" || !(event.target instanceof Node) || !frameRef.current?.contains(event.target)) return;
    event.preventDefault();
    const point = pointInImage(event.clientX, event.clientY);
    const nextDraft = { x: point.x, y: point.y, width: 0, height: 0 };
    draftRef.current = nextDraft;
    setDraft(nextDraft);
    dragRef.current = {
      kind: "draw",
      pointerId: event.pointerId,
      startX: point.x,
      startY: point.y,
      classId: selectedClass,
      boxes,
    };
    capturePointer(event.pointerId);
  };

  const onStagePointerMove = (event: ReactPointerEvent<HTMLElement>) => {
    const drag = dragRef.current;
    if (!drag || drag.pointerId !== event.pointerId) return;

    if (drag.kind === "pan") {
      setPan({
        x: drag.originX + event.clientX - drag.startClientX,
        y: drag.originY + event.clientY - drag.startClientY,
      });
      return;
    }

    const point = pointInImage(event.clientX, event.clientY);
    if (drag.kind === "draw") {
      const nextDraft = {
        x: Math.min(drag.startX, point.x),
        y: Math.min(drag.startY, point.y),
        width: Math.abs(point.x - drag.startX),
        height: Math.abs(point.y - drag.startY),
      };
      draftRef.current = nextDraft;
      setDraft(nextDraft);
      return;
    }

    const deltaX = point.x - drag.startX;
    const deltaY = point.y - drag.startY;
    const nextBox = drag.kind === "move"
      ? movedBox(drag.box, deltaX, deltaY, imageWidth, imageHeight)
      : resizedBox(
          drag.box,
          drag.handle,
          deltaX,
          deltaY,
          imageWidth,
          imageHeight,
          minimumBoxSize,
        );
    const next = drag.boxes.map((box) => box.id === drag.box.id ? nextBox : box);
    previewRef.current = next;
    setPreviewBoxes(next);
  };

  const finishPointer = (commit: boolean) => {
    const drag = dragRef.current;
    if (!drag) return;
    releasePointer(drag.pointerId);
    dragRef.current = null;

    if (drag.kind === "pan") {
      if (!commit) setPan({ x: drag.originX, y: drag.originY });
      setIsPanning(false);
      return;
    }

    if (drag.kind === "draw") {
      const nextDraft = draftRef.current;
      draftRef.current = null;
      setDraft(null);
      if (
        commit
        && nextDraft
        && nextDraft.width * visualScale >= MIN_BOX_SIZE
        && nextDraft.height * visualScale >= MIN_BOX_SIZE
      ) {
        const newBox: EditorBox = {
          id: nextBoxIdRef.current++,
          classId: drag.classId,
          ...nextDraft,
        };
        onChange([...drag.boxes, newBox]);
        setSelectedId(newBox.id);
        setTool("select");
      }
      return;
    }

    const next = previewRef.current;
    previewRef.current = null;
    setPreviewBoxes(null);
    if (commit && next && boxesChanged(drag.boxes, next)) onChange(next);
  };

  const deleteSelected = useCallback(() => {
    if (!selectedId) return;
    const next = boxes.filter((box) => box.id !== selectedId);
    if (next.length === boxes.length) return;
    onChange(next);
    setSelectedId(0);
  }, [boxes, onChange, selectedId]);

  const chooseClass = useCallback((classId: number) => {
    setSelectedClass(classId);
    const selected = boxes.find((box) => box.id === selectedId);
    if (selected && selected.classId !== classId) {
      onChange(boxes.map((box) => box.id === selectedId ? { ...box, classId } : box));
    }
  }, [boxes, onChange, selectedId]);

  const resetView = useCallback(() => {
    setZoom(FIT_ZOOM);
    setPan({ x: 0, y: 0 });
  }, []);

  const applyZoom = useCallback((nextValue: number, clientX?: number, clientY?: number) => {
    const next = clamp(nextValue, 50, 320);
    if (next === zoom) return;
    if (clientX !== undefined && clientY !== undefined) {
      const bounds = stageRef.current?.getBoundingClientRect();
      if (bounds) {
        const ratio = next / zoom;
        const anchorX = clientX - (bounds.left + bounds.width / 2 + pan.x);
        const anchorY = clientY - (bounds.top + bounds.height / 2 + pan.y);
        setPan({
          x: pan.x - anchorX * (ratio - 1),
          y: pan.y - anchorY * (ratio - 1),
        });
      }
    }
    setZoom(next);
  }, [pan, zoom]);

  useEffect(() => {
    const stage = stageRef.current;
    if (!stage) return;
    const onWheel = (event: WheelEvent) => {
      if (Math.abs(event.deltaY) < Math.abs(event.deltaX)) return;
      event.preventDefault();
      applyZoom(zoom + (event.deltaY < 0 ? 8 : -8), event.clientX, event.clientY);
    };
    stage.addEventListener("wheel", onWheel, { passive: false });
    return () => stage.removeEventListener("wheel", onWheel);
  }, [applyZoom, onNavigate, zoom]);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (isTypingTarget(event.target)) return;
      if (event.code === "Space") {
        event.preventDefault();
        if (!spaceDownRef.current) {
          spaceDownRef.current = true;
          setSpaceDown(true);
        }
        return;
      }
      if (event.ctrlKey || event.metaKey || event.altKey) return;
      if (event.key === "Delete" || event.key === "Backspace") {
        event.preventDefault();
        deleteSelected();
      } else if (event.key === "Tab" && boxes.length > 0) {
        event.preventDefault();
        const index = boxes.findIndex((box) => box.id === selectedId);
        const next = boxes[(index + 1) % boxes.length];
        setSelectedId(next.id);
        setSelectedClass(next.classId);
      } else if (/^[0-9]$/.test(event.key)) {
        const classId = Number(event.key);
        if (classes.some((item) => item.id === classId)) chooseClass(classId);
      } else if (event.code === "KeyN") {
        setTool("draw");
      } else if (event.key === "Escape") {
        setTool("select");
      }
    };
    const onKeyUp = (event: KeyboardEvent) => {
      if (event.code === "Space") {
        spaceDownRef.current = false;
        setSpaceDown(false);
      }
    };
    const onBlur = () => {
      spaceDownRef.current = false;
      setSpaceDown(false);
    };
    window.addEventListener("keydown", onKeyDown);
    window.addEventListener("keyup", onKeyUp);
    window.addEventListener("blur", onBlur);
    return () => {
      window.removeEventListener("keydown", onKeyDown);
      window.removeEventListener("keyup", onKeyUp);
      window.removeEventListener("blur", onBlur);
    };
  }, [boxes, chooseClass, classes, deleteSelected, resetView, selectedId]);

  const submitRename = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!rename || renameBusy) return;
    const value = rename.value.trim();
    if (!value) {
      setRenameError("클래스 이름을 입력하세요.");
      return;
    }
    setRenameBusy(true);
    setRenameError("");
    try {
      await onRenameClass(rename.id, value);
      setRename(null);
    } catch {
      setRenameError("이름을 저장하지 못했습니다.");
    } finally {
      setRenameBusy(false);
    }
  };

  const saveLabel = saveState === "saved" ? "저장됨" : saveState === "saving" ? "저장 중" : "저장 실패";

  return (
    <div className="viewer-body">
      <aside className="tool-rail" aria-label="라벨링 도구">
        <button className="rail-button" type="button" aria-label="선택·팬" title="선택·팬" aria-pressed={tool === "select"} onClick={() => setTool("select")}><Icon name="mouse" size={17} /></button>
        <button className="rail-button" type="button" aria-label="박스 생성" title="박스 생성" aria-pressed={tool === "draw"} onClick={() => setTool("draw")}><Icon name="box" size={17} /></button>
      </aside>

      <section
        ref={stageRef}
        className={`viewer-stage${tool === "draw" ? " is-drawing" : ""}${spaceDown ? " is-pan-ready" : ""}${isPanning ? " is-panning" : ""}`}
        aria-label="라벨 편집 캔버스"
        tabIndex={0}
        onPointerDown={onStagePointerDown}
        onPointerMove={onStagePointerMove}
        onPointerUp={() => finishPointer(true)}
        onPointerCancel={() => finishPointer(false)}
      >
        <div
          ref={frameRef}
          className="image-frame"
          style={{
            width: imageWidth,
            height: imageHeight,
            transform: `translate3d(${pan.x}px, ${pan.y}px, 0) scale(${visualScale})`,
          }}
        >
          <img src={imageSrc} alt="도로 장면 라벨링 이미지" draggable="false" />
          {renderedBoxes.map((box) => {
            const editorClass = classes.find((item) => item.id === box.classId) ?? classes[0];
            const selected = box.id === selectedId;
            return (
              <button
                className={`editor-box${selected ? " is-selected" : ""}`}
                type="button"
                key={box.id}
                aria-label={`${editorClass?.name ?? "미지정"} 바운딩 박스`}
                style={{ left: box.x, top: box.y, width: box.width, height: box.height, color: editorClass?.color }}
                onPointerDown={(event) => beginBoxDrag(event, box)}
              >
                <span className="box-label" style={{ background: editorClass?.color }}>{editorClass?.name ?? "미지정"}{box.score ? ` ${box.score}` : ""}</span>
                {selected ? HANDLES.map((handle) => (
                  <i
                    className={`resize-handle handle-${handle}`}
                    key={handle}
                    aria-hidden="true"
                    onPointerDown={(event) => beginResize(event, box, handle)}
                  />
                )) : null}
              </button>
            );
          })}
          {draft ? (
            <span
              className="editor-box draft-box"
              style={{ ...draft, color: classes.find((item) => item.id === selectedClass)?.color }}
            />
          ) : null}
        </div>
        <div className="zoom-pill">
          <button type="button" aria-label="축소" onClick={() => applyZoom(zoom - 8)}>−</button>
          <span className="mono">{zoom}%</span>
          <button type="button" aria-label="확대" onClick={() => applyZoom(zoom + 8)}>＋</button>
          <span className="zoom-divider" />
          <button type="button" onClick={resetView}>화면 초기화</button>
        </div>
      </section>

      <aside className="inspector">
        <section>
          <h2>클래스</h2>
          <div className="inspector-class-list">
            {classes.map((item) => rename?.id === item.id ? (
              <form className="inspector-class is-renaming" key={item.id} onSubmit={(event) => void submitRename(event)}>
                <span className="class-index" style={{ background: item.color }}>{item.id}</span>
                <input
                  autoFocus
                  value={rename.value}
                  aria-label={`${item.name} 클래스 이름`}
                  onChange={(event) => setRename({ id: item.id, value: event.target.value })}
                  onKeyDown={(event) => {
                    if (event.key === "Escape") {
                      event.preventDefault();
                      setRename(null);
                      setRenameError("");
                    }
                  }}
                />
                <button type="submit" aria-label="이름 저장" disabled={renameBusy}><Icon name="check" size={13} /></button>
                <button type="button" aria-label="이름 변경 취소" onClick={() => { setRename(null); setRenameError(""); }}><Icon name="x" size={13} /></button>
              </form>
            ) : (
              <div
                className="inspector-class"
                aria-pressed={selectedClass === item.id}
                role="button"
                tabIndex={0}
                key={item.id}
                onClick={() => chooseClass(item.id)}
                onKeyDown={(event) => {
                  if (event.key === "Enter" || event.key === " ") {
                    event.preventDefault();
                    chooseClass(item.id);
                  }
                }}
              >
                <button
                  className="class-index class-color-trigger"
                  type="button"
                  aria-label={`${item.name} 색상 변경`}
                  style={{ background: item.color }}
                  onClick={(event) => {
                    event.stopPropagation();
                    setColorClassId((current) => current === item.id ? null : item.id);
                  }}
                >{item.id}</button>
                <span>{item.name}</span>
                <button
                  className="class-rename-trigger"
                  type="button"
                  aria-label={`${item.name} 이름 변경`}
                  onClick={(event) => {
                    event.stopPropagation();
                    setRename({ id: item.id, value: item.name });
                    setRenameError("");
                  }}
                >✎</button>
                <span className="mono">{item.count}</span>
                {colorClassId === item.id ? (
                  <ClassColorPicker
                    className={item.name}
                    color={item.color}
                    top={36}
                    onChange={(color) => onChangeClassColor(item.id, color)}
                    onClose={() => setColorClassId(null)}
                  />
                ) : null}
              </div>
            ))}
          </div>
          {renameError ? <p className="class-rename-error" role="alert">{renameError}</p> : null}
        </section>
        <section className="shortcut-section">
          <h2>단축키</h2>
          <dl className="shortcut-list">
            <div><dt>팬</dt><dd><kbd>Space</kbd></dd></div>
            <div><dt>줌</dt><dd>휠</dd></div>
            <div><dt>박스 생성</dt><dd><kbd>N</kbd></dd></div>
            <div><dt>이전·다음 이미지</dt><dd><kbd>←</kbd><kbd>→</kbd></dd></div>
            <div><dt>다음 박스</dt><dd><kbd>Tab</kbd></dd></div>
            <div><dt>클래스</dt><dd><kbd>0–9</kbd></dd></div>
            <div><dt>삭제</dt><dd><kbd>Del</kbd></dd></div>
            <div><dt>실행취소 50단계</dt><dd><kbd>Ctrl</kbd>+<kbd>Z</kbd></dd></div>
          </dl>
        </section>
        <footer className="inspector-footer">
          <button
            className={`${saveState === "saved" ? "save-ok" : "save-pending"}${saveState === "error" ? " save-error" : ""}`}
            type="button"
            disabled={saveState !== "error"}
            onClick={onRetrySave}
          ><span />{saveLabel}</button>
          <button className="btn btn-ghost btn-sm" type="button" disabled={undoDepth === 0} onClick={onUndo}><Icon name="undo" size={13} />실행취소</button>
        </footer>
      </aside>
    </div>
  );
}
