"""Filesystem operations constrained to the configured storage root."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4


logger = logging.getLogger(__name__)


class StorageBoundaryError(ValueError):
    """Raised when persisted paths point outside the configured storage root."""


def storage_root(path: Path) -> Path:
    root = path.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def dataset_storage_path(root: Path, dataset_id: int) -> Path:
    return storage_root(root) / "datasets" / str(dataset_id)


def create_dataset_storage(root: Path, dataset_id: int) -> Path:
    target = dataset_storage_path(root, dataset_id)
    target.mkdir(parents=True, exist_ok=False)
    return target


def contained_storage_path(root: Path, candidate: str | Path) -> Path:
    resolved_root = storage_root(root)
    raw_candidate = Path(candidate).expanduser()
    if not str(candidate) or "\x00" in str(candidate):
        raise StorageBoundaryError("storage path is empty or invalid")
    resolved_candidate = (
        raw_candidate.resolve()
        if raw_candidate.is_absolute()
        else (resolved_root / raw_candidate).resolve()
    )
    if (
        resolved_candidate == resolved_root
        or resolved_root not in resolved_candidate.parents
    ):
        raise StorageBoundaryError("storage path is outside STORAGE_DIR")
    return resolved_candidate


def storage_relative_path(root: Path, candidate: str | Path) -> str:
    """Return a root-contained runtime path in its persisted POSIX form."""
    resolved_root = storage_root(root)
    resolved_candidate = contained_storage_path(resolved_root, candidate)
    return resolved_candidate.relative_to(resolved_root).as_posix()


@dataclass(frozen=True)
class StagedDeletion:
    original: Path
    quarantine: Path
    payload: Path


def _remove_empty_quarantine(quarantine: Path) -> None:
    try:
        quarantine.rmdir()
    except FileNotFoundError:
        return
    except OSError:
        # A request-scoped quarantine remains while sibling payloads exist.
        return


def stage_deletions(
    root: Path,
    stored_paths: Iterable[str | Path],
) -> list[StagedDeletion | None]:
    """Move all existing paths into one fresh request-scoped quarantine."""

    targets = [contained_storage_path(root, path) for path in stored_paths]
    if len(set(targets)) != len(targets):
        raise ValueError("duplicate deletion target")
    existing = [(index, target) for index, target in enumerate(targets) if target.exists()]
    if not existing:
        return [None] * len(targets)

    quarantine_root = storage_root(root) / ".delete-pending"
    quarantine_root.mkdir(parents=True, exist_ok=True)
    quarantine = quarantine_root / uuid4().hex
    quarantine.mkdir()
    result: list[StagedDeletion | None] = [None] * len(targets)
    staged: list[StagedDeletion | None] = []
    try:
        for index, target in existing:
            payload = quarantine / f"{index:06d}-{target.name}-{uuid4().hex}"
            os.replace(target, payload)
            os.utime(quarantine, None)
            item = StagedDeletion(
                original=target,
                quarantine=quarantine,
                payload=payload,
            )
            result[index] = item
            staged.append(item)
    except BaseException:
        restore_staged_deletions(reversed(staged))
        _remove_empty_quarantine(quarantine)
        raise
    return result


def stage_dataset_deletion(
    root: Path,
    stored_path: str | Path,
) -> StagedDeletion | None:
    return stage_deletions(root, [stored_path])[0]


async def stage_deletions_async(
    root: Path,
    stored_paths: Iterable[str | Path],
) -> list[StagedDeletion | None]:
    """Stage a request scope without losing moved paths to task cancellation."""

    paths = tuple(stored_paths)
    task = asyncio.create_task(asyncio.to_thread(stage_deletions, root, paths))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        # A task can receive another cancellation while it waits for the
        # staging thread.  Do not let that second delivery discard the only
        # handle capable of restoring paths which the thread already moved.
        current = asyncio.current_task()
        while not task.done():
            if current is not None:
                while current.cancelling():
                    current.uncancel()
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
        if not task.cancelled():
            try:
                staged = task.result()
            except BaseException:
                pass
            else:
                restore_staged_deletions(reversed(staged))
        raise


async def stage_dataset_deletion_async(
    root: Path,
    stored_path: str | Path,
) -> StagedDeletion | None:
    """Cancellation-safe async wrapper for a single deletion target."""

    return (await stage_deletions_async(root, [stored_path]))[0]


def restore_staged_deletion(staged: StagedDeletion | None) -> bool:
    if staged is None:
        return True
    if not staged.quarantine.exists() or not staged.payload.exists():
        logger.error(
            "cannot restore staged deletion because quarantine is missing: "
            "original=%s quarantine=%s payload=%s",
            staged.original,
            staged.quarantine,
            staged.payload,
        )
        return False
    staged.original.parent.mkdir(parents=True, exist_ok=True)
    os.replace(staged.payload, staged.original)
    _remove_empty_quarantine(staged.quarantine)
    return True


def restore_staged_deletions(
    staged_deletions: Iterable[StagedDeletion | None],
) -> bool:
    restored = True
    for staged in staged_deletions:
        restored = restore_staged_deletion(staged) and restored
    return restored


def finalize_staged_deletion(staged: StagedDeletion | None) -> None:
    if staged is None:
        return
    if staged.payload.is_symlink() or staged.payload.is_file():
        staged.payload.unlink(missing_ok=True)
    elif staged.payload.is_dir():
        shutil.rmtree(staged.payload)
    _remove_empty_quarantine(staged.quarantine)


def finalize_staged_deletions(
    staged_deletions: Iterable[StagedDeletion | None],
) -> None:
    for staged in staged_deletions:
        finalize_staged_deletion(staged)
