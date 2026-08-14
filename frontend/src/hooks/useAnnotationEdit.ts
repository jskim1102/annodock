import { useCallback, useEffect, useRef, useState } from "react";

export interface EditorBox {
  id: number;
  classId: number;
  score?: string;
  x: number;
  y: number;
  width: number;
  height: number;
}

export type AnnotationSaveState = "saving" | "saved" | "error";

interface UseAnnotationEdit {
  boxes: EditorBox[];
  applyChange: (next: EditorBox[]) => void;
  undo: () => void;
  undoDepth: number;
  saveState: AnnotationSaveState;
  retry: () => void;
  flush: () => Promise<boolean>;
  reset: (next: EditorBox[]) => void;
}

const UNDO_LIMIT = 50;

function isTypingTarget(target: EventTarget | null) {
  return target instanceof HTMLElement && (
    target.isContentEditable
    || target.tagName === "INPUT"
    || target.tagName === "TEXTAREA"
    || target.tagName === "SELECT"
  );
}

export function useAnnotationEdit(
  initialBoxes: EditorBox[],
  save: (boxes: EditorBox[]) => Promise<void>,
): UseAnnotationEdit {
  const [boxes, setBoxes] = useState(initialBoxes);
  const [undoStack, setUndoStack] = useState<EditorBox[][]>([]);
  const [saveState, setSaveState] = useState<AnnotationSaveState>("saved");
  const boxesRef = useRef(initialBoxes);
  const undoStackRef = useRef<EditorBox[][]>([]);
  const saveRef = useRef(save);
  const generationRef = useRef(0);
  const pendingRef = useRef(false);
  const inFlightRef = useRef<Promise<boolean> | null>(null);

  useEffect(() => {
    saveRef.current = save;
  }, [save]);

  const flush = useCallback(async (): Promise<boolean> => {
    while (true) {
      const existing = inFlightRef.current;
      if (existing) {
        const succeeded = await existing;
        if (!succeeded) return false;
        continue;
      }

      if (!pendingRef.current) return true;

      pendingRef.current = false;
      const generation = generationRef.current;
      const payload = boxesRef.current;
      setSaveState("saving");

      const operation = (async () => {
        try {
          await saveRef.current(payload);
          if (generation === generationRef.current) setSaveState("saved");
          return true;
        } catch {
          if (generation === generationRef.current) {
            pendingRef.current = true;
            setSaveState("error");
          }
          return false;
        }
      })();

      inFlightRef.current = operation;
      const succeeded = await operation;
      if (inFlightRef.current === operation) inFlightRef.current = null;
      if (!succeeded) return false;
    }
  }, []);

  const scheduleSave = useCallback(() => {
    pendingRef.current = true;
    setSaveState("saving");
    void flush();
  }, [flush]);

  const applyChange = useCallback((next: EditorBox[]) => {
    if (next === boxesRef.current) return;
    const nextStack = [...undoStackRef.current, boxesRef.current].slice(-UNDO_LIMIT);
    undoStackRef.current = nextStack;
    setUndoStack(nextStack);
    boxesRef.current = next;
    setBoxes(next);
    scheduleSave();
  }, [scheduleSave]);

  const undo = useCallback(() => {
    const previous = undoStackRef.current.at(-1);
    if (!previous) return;
    const nextStack = undoStackRef.current.slice(0, -1);
    undoStackRef.current = nextStack;
    setUndoStack(nextStack);
    boxesRef.current = previous;
    setBoxes(previous);
    scheduleSave();
  }, [scheduleSave]);

  const reset = useCallback((next: EditorBox[]) => {
    generationRef.current += 1;
    pendingRef.current = false;
    boxesRef.current = next;
    undoStackRef.current = [];
    setBoxes(next);
    setUndoStack([]);
    setSaveState("saved");
  }, []);

  const retry = useCallback(() => {
    pendingRef.current = true;
    void flush();
  }, [flush]);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (
        (event.ctrlKey || event.metaKey)
        && event.key.toLowerCase() === "z"
        && !event.shiftKey
        && !isTypingTarget(event.target)
      ) {
        event.preventDefault();
        undo();
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [undo]);

  return {
    boxes,
    applyChange,
    undo,
    undoDepth: undoStack.length,
    saveState,
    retry,
    flush,
    reset,
  };
}
