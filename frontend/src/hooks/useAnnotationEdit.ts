import { useCallback, useEffect, useRef, useState } from "react";

import { AuthApiError } from "../api/auth";
import { ApiError, captureWorkspaceId, type ClassRow } from "../api/client";

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

type SaveAnnotations = (boxes: EditorBox[], workspaceId: number | null, expectedVersion: number, expectedCatalogVersion: number) => Promise<{ version: number } | void>;

interface VersionConflict {
  code: "version_conflict" | "class_catalog_changed";
  current: { version: number; boxes: unknown[]; classes?: ClassRow[]; catalog_version?: number; label_status?: string; assignee_id?: number | null };
}
interface EditOptions { readOnly?: boolean; editBlocked?: boolean; onAssignmentConflict?: () => void }


interface PendingEdit {
  boxes: EditorBox[];
  save: SaveAnnotations;
  workspaceId: number | null;
  generation: number;
  version: { current: number };
  catalogVersion: number;
}

interface UseAnnotationEdit {
  boxes: EditorBox[];
  applyChange: (next: EditorBox[]) => void;
  undo: () => void;
  undoDepth: number;
  saveState: AnnotationSaveState;
  saveError: string | null;
  retry: () => void;
  flush: (options?: { requireSaved?: boolean }) => Promise<boolean>;
  reset: (next: EditorBox[], version?: number, catalogVersion?: number) => void;
  catalogVersion: number;
  setCatalogVersion: (version: number) => void;
  conflict: VersionConflict | null;
  resolveConflict: (choice: "server" | "local", server: {boxes: EditorBox[]; version: number; catalog_version: number}) => Promise<boolean>;
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
  save: SaveAnnotations,
  options: EditOptions = {},
): UseAnnotationEdit {
  const [boxes, setBoxes] = useState(initialBoxes);
  const [undoStack, setUndoStack] = useState<EditorBox[][]>([]);
  const [saveState, setSaveState] = useState<AnnotationSaveState>("saved");
  const [saveError, setSaveError] = useState<string | null>(null);
  const [conflict, setConflict] = useState<VersionConflict | null>(null);
  const conflictRef = useRef<VersionConflict | null>(null);
  const blockedRef = useRef(false);
  const versionRef = useRef({ current: 0 });
  const [catalogVersion, updateCatalogVersion] = useState(0);
  const catalogVersionRef = useRef(0);
  const rejectedRef = useRef(false);
  const setCatalogVersion = useCallback((version: number) => {
    catalogVersionRef.current = version;
    updateCatalogVersion(version);
  }, []);
  const optionsRef = useRef(options);
  optionsRef.current = options;
  const boxesRef = useRef(initialBoxes);
  const undoStackRef = useRef<EditorBox[][]>([]);
  const saveRef = useRef(save);
  const generationRef = useRef(0);
  const pendingRef = useRef<PendingEdit | null>(null);
  const inFlightRef = useRef<Promise<boolean> | null>(null);

  useEffect(() => {
    saveRef.current = save;
  }, [save]);

  useEffect(() => {
    const onBeforeUnload = (event: BeforeUnloadEvent) => {
      if (!pendingRef.current && !conflictRef.current && inFlightRef.current === null) return;
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", onBeforeUnload);
    return () => window.removeEventListener("beforeunload", onBeforeUnload);
  }, []);

  const flush = useCallback(async (options: { requireSaved?: boolean } = {}): Promise<boolean> => {
    while (true) {
      if (conflictRef.current) return false;
      if (blockedRef.current) return !options.requireSaved;
      const existing = inFlightRef.current;
      if (existing) {
        const canContinue = await existing;
        if (!canContinue) return false;
        continue;
      }

      const edit = pendingRef.current;
      if (!edit) return !options.requireSaved || !rejectedRef.current;
      if (edit.generation === generationRef.current && optionsRef.current.readOnly) {
        pendingRef.current = null;
        blockedRef.current = true;
        setSaveState("rejected");
        setSaveError(null);
        return false;
      }

      pendingRef.current = null;
      const generation = edit.generation;
      if (generation === generationRef.current) rejectedRef.current = false;
      setSaveState("saving");
      setSaveError(null);

      const operation = (async () => {
        try {
          const result = await edit.save(edit.boxes, edit.workspaceId, edit.version.current, edit.catalogVersion);
          if (result) edit.version.current = result.version;
          if (generation === generationRef.current) setSaveState("saved");
          return true;
        } catch (reason: unknown) {
          const detail = reason instanceof ApiError && reason.status === 409
            ? reason.detail as { code?: string; current?: VersionConflict["current"] } | undefined : undefined;
          if (generation === generationRef.current && (detail?.code === "version_conflict" || detail?.code === "class_catalog_changed") && detail.current) {
            if (!pendingRef.current) pendingRef.current = edit;
            conflictRef.current = { code: detail.code, current: detail.current };
            setConflict(conflictRef.current);
            setSaveState("error");
            setSaveError(null);
            return false;
          }
          if (generation === generationRef.current && detail?.code === "image_assigned_to_other") {
            blockedRef.current = true;
            pendingRef.current = null;
            setSaveState("rejected");
            setSaveError("다른 사람이 작업 중인 이미지예요 — 보기만 할 수 있어요");
            optionsRef.current.onAssignmentConflict?.();
            return false;
          }
          const rejected = (reason instanceof ApiError || reason instanceof AuthApiError)
            && reason.status >= 400 && reason.status < 500;
          if (generation === generationRef.current) {
            rejectedRef.current = rejected;
            // A rejected payload cannot be saved by retrying. Leave any newer
            // queued edit intact, but do not requeue this rejected snapshot.
            if (!rejected && pendingRef.current === null) pendingRef.current = edit;
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
    // A queued edit can outlive its viewer after workspace navigation. Keep
    // its target callback and workspace together through saves and retries.
    pendingRef.current = {
      boxes: boxesRef.current,
      save: saveRef.current,
      workspaceId: captureWorkspaceId(),
      generation: generationRef.current,
      version: versionRef.current,
      catalogVersion: catalogVersionRef.current,
    };
    setSaveState("saving");
    void flush();
  }, [flush]);

  const applyChange = useCallback((next: EditorBox[]) => {
    if (optionsRef.current.readOnly || optionsRef.current.editBlocked || blockedRef.current || conflictRef.current || next === boxesRef.current) return;
    const nextStack = [...undoStackRef.current, boxesRef.current].slice(-UNDO_LIMIT);
    undoStackRef.current = nextStack;
    setUndoStack(nextStack);
    boxesRef.current = next;
    setBoxes(next);
    scheduleSave();
  }, [scheduleSave]);

  const undo = useCallback(() => {
    if (optionsRef.current.readOnly || optionsRef.current.editBlocked || blockedRef.current || conflictRef.current) return;
    const previous = undoStackRef.current.at(-1);
    if (!previous) return;
    const nextStack = undoStackRef.current.slice(0, -1);
    undoStackRef.current = nextStack;
    setUndoStack(nextStack);
    boxesRef.current = previous;
    setBoxes(previous);
    scheduleSave();
  }, [scheduleSave]);

  const reset = useCallback((next: EditorBox[], version = 0, nextCatalogVersion = 0) => {
    setCatalogVersion(nextCatalogVersion);
    rejectedRef.current = false;
    versionRef.current = { current: version };
    conflictRef.current = null;
    blockedRef.current = false;
    setConflict(null);
    generationRef.current += 1;
    pendingRef.current = null;
    boxesRef.current = next;
    undoStackRef.current = [];
    setBoxes(next);
    setUndoStack([]);
    setSaveState("saved");
    setSaveError(null);
  }, [setCatalogVersion]);

  const resolveConflict = useCallback(async (choice: "server" | "local", server: {boxes: EditorBox[]; version: number; catalog_version: number}) => {
    const unresolved = conflictRef.current;
    if (!unresolved) return false;
    if (choice === "server") {
      if (!Number.isInteger(server.catalog_version) || server.catalog_version < 0) return false;
      reset(server.boxes, server.version, server.catalog_version);
      return true;
    }
    if (optionsRef.current.readOnly || blockedRef.current
      || unresolved.code !== "version_conflict"
      || !Array.isArray(unresolved.current.classes)
      || unresolved.current.catalog_version !== catalogVersionRef.current
      || server.catalog_version !== catalogVersionRef.current) return false;
    versionRef.current.current = server.version;
    conflictRef.current = null;
    setConflict(null);
    // Keep the newest local snapshot, including edits made while the failed
    // request was in flight. Its captured image/workspace callback stays fixed.
    if (pendingRef.current) pendingRef.current.boxes = boxesRef.current;
    const pending = pendingRef.current;
    const generation = generationRef.current;
    const flushed = await flush();
    const saved = flushed && !rejectedRef.current;
    if (!saved && rejectedRef.current && generation === generationRef.current
      && !conflictRef.current && !blockedRef.current) {
      // The caller has not adopted the new catalog. Keep editing paused until
      // another explicit choice can adopt both the boxes and catalog together.
      if (!pendingRef.current) pendingRef.current = pending;
      conflictRef.current = unresolved;
      setConflict(unresolved);
    }
    return saved;
  }, [flush, reset]);

  const retry = useCallback(() => {
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
    catalogVersion,
    setCatalogVersion,
    applyChange,
    undo,
    undoDepth: undoStack.length,
    saveState,
    saveError,
    retry,
    flush,
    reset,
    conflict,
    resolveConflict,
  };
}
