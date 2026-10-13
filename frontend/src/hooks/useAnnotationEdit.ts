import { useCallback, useEffect, useRef, useState } from "react";

import { AuthApiError } from "../api/auth";
import { ApiError } from "../api/client";

export interface EditorBox {
  id: number;
  classId: number;
  score?: string;
  x: number;
  y: number;
  width: number;
  height: number;
}

export type AnnotationSaveState = "saving" | "saved" | "error" | "rejected";

interface UseAnnotationEdit {
  boxes: EditorBox[];
  applyChange: (next: EditorBox[]) => void;
  undo: () => void;
  undoDepth: number;
  saveState: AnnotationSaveState;
  saveError: string | null;
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
  const [saveError, setSaveError] = useState<string | null>(null);
  const boxesRef = useRef(initialBoxes);
  const undoStackRef = useRef<EditorBox[][]>([]);
  const saveRef = useRef(save);
  const generationRef = useRef(0);
  const pendingRef = useRef(false);
  const inFlightRef = useRef<Promise<boolean> | null>(null);

  useEffect(() => {
    saveRef.current = save;
  }, [save]);

  useEffect(() => {
    const onBeforeUnload = (event: BeforeUnloadEvent) => {
      if (!pendingRef.current && inFlightRef.current === null) return;
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", onBeforeUnload);
    return () => window.removeEventListener("beforeunload", onBeforeUnload);
  }, []);

  const flush = useCallback(async (): Promise<boolean> => {
    while (true) {
      const existing = inFlightRef.current;
      if (existing) {
        const canContinue = await existing;
        if (!canContinue) return false;
        continue;
      }

      if (!pendingRef.current) return true;

      pendingRef.current = false;
      const generation = generationRef.current;
      const payload = boxesRef.current;
      setSaveState("saving");
      setSaveError(null);

      const operation = (async () => {
        try {
          await saveRef.current(payload);
          if (generation === generationRef.current) setSaveState("saved");
          return true;
        } catch (reason: unknown) {
          const rejected = (reason instanceof ApiError || reason instanceof AuthApiError)
            && reason.status >= 400 && reason.status < 500;
          if (generation === generationRef.current) {
            // A rejected payload cannot be saved by retrying. Leave any newer
            // queued edit intact, but do not requeue this rejected snapshot.
            if (!rejected) pendingRef.current = true;
            setSaveState(rejected ? "rejected" : "error");
            setSaveError(reason instanceof Error ? reason.message : "저장하지 못했습니다.");
          }
          // flush controls navigation: a 4xx releases it without claiming the
          // edit was saved; network/5xx errors keep unsaved edits protected.
          return rejected;
        }
      })();

      inFlightRef.current = operation;
      const canContinue = await operation;
      if (inFlightRef.current === operation) inFlightRef.current = null;
      if (!canContinue) return false;
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
    setSaveError(null);
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
    saveError,
    retry,
    flush,
    reset,
  };
}
