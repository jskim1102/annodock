"""Filesystem operations constrained to the configured storage root."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4


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


def stage_dataset_deletion(
    root: Path,
    stored_path: str | Path,
) -> StagedDeletion | None:
    target = contained_storage_path(root, stored_path)
    if not target.exists():
        return None
    quarantine_root = storage_root(root) / ".delete-pending"
    quarantine_root.mkdir(parents=True, exist_ok=True)
    quarantine = quarantine_root / f"{target.name}-{uuid4().hex}"
    os.replace(target, quarantine)
    return StagedDeletion(original=target, quarantine=quarantine)


def restore_staged_deletion(staged: StagedDeletion | None) -> None:
    if staged is None or not staged.quarantine.exists():
        return
    staged.original.parent.mkdir(parents=True, exist_ok=True)
    os.replace(staged.quarantine, staged.original)


def finalize_staged_deletion(staged: StagedDeletion | None) -> None:
    if staged is not None and staged.quarantine.exists():
        shutil.rmtree(staged.quarantine)
