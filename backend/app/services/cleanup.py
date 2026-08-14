"""Staged cleanup for persisted training-run directories."""

from __future__ import annotations

import asyncio
import shutil
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import TrainingRun
from app.services.quota import decrease_bytes_used, path_tree_bytes
from app.services.storage import (
    StagedDeletion,
    StorageBoundaryError,
    contained_storage_path,
    finalize_staged_deletion,
    restore_staged_deletion,
    stage_dataset_deletion,
    storage_root,
)


TERMINAL_RUN_STATES = frozenset({"done", "failed", "canceled"})


@dataclass(frozen=True)
class RetentionResult:
    removed_runs: int = 0
    removed_bytes: int = 0
    finalized_pending: int = 0


def _contained_run_root(
    root: Path,
    out_dir: str | Path,
) -> Path:
    resolved_root = storage_root(root)
    run_root = contained_storage_path(resolved_root, out_dir)
    if run_root.parent != resolved_root / "training-runs":
        raise StorageBoundaryError(
            "training run path is outside STORAGE_DIR/training-runs"
        )
    return run_root


def _stage_run_subdirectory(
    root: Path,
    out_dir: str | Path,
    name: str,
) -> StagedDeletion | None:
    """Quarantine one direct run child after validating the storage boundary."""
    resolved_root = storage_root(root)
    run_root = _contained_run_root(resolved_root, out_dir)
    return stage_dataset_deletion(resolved_root, run_root / name)


def stage_run_workdir(
    root: Path,
    out_dir: str | Path,
) -> StagedDeletion | None:
    return _stage_run_subdirectory(root, out_dir, "workdir")


def stage_run_artifacts(
    root: Path,
    out_dir: str | Path,
) -> StagedDeletion | None:
    return _stage_run_subdirectory(root, out_dir, "artifacts")


def stage_training_run_deletion(
    root: Path,
    out_dir: str | Path,
) -> StagedDeletion | None:
    """Quarantine a complete run only when it is a direct training-runs child."""
    resolved_root = storage_root(root)
    run_root = _contained_run_root(resolved_root, out_dir)
    return stage_dataset_deletion(resolved_root, run_root)


def finalize_pending_deletions(root: Path) -> RetentionResult:
    """Finish only entries already quarantined under STORAGE_DIR."""

    pending_root = storage_root(root) / ".delete-pending"
    if not pending_root.exists():
        return RetentionResult()
    if pending_root.is_symlink() or not pending_root.is_dir():
        raise StorageBoundaryError(".delete-pending must be a real directory")

    finalized = 0
    for candidate in tuple(pending_root.iterdir()):
        if candidate.parent != pending_root:
            continue
        if candidate.is_symlink() or candidate.is_file():
            candidate.unlink(missing_ok=True)
        elif candidate.is_dir():
            shutil.rmtree(candidate)
        else:
            continue
        finalized += 1
    return RetentionResult(finalized_pending=finalized)


def _retention_candidates(
    runs: list[TrainingRun],
    *,
    keep_count: int,
    keep_days: int,
    now: datetime,
) -> list[int]:
    by_owner: dict[int, list[TrainingRun]] = defaultdict(list)
    for run in runs:
        by_owner[run.owner_id].append(run)

    cutoff = now - timedelta(days=keep_days)
    candidates: list[int] = []
    for owner_runs in by_owner.values():
        owner_runs.sort(
            key=lambda run: (
                run.finished_at
                or run.created_at
                or datetime.min.replace(tzinfo=timezone.utc),
                run.id,
            ),
            reverse=True,
        )
        for index, run in enumerate(owner_runs):
            beyond_count = index >= keep_count
            expired = run.finished_at is not None and run.finished_at < cutoff
            if beyond_count or expired:
                candidates.append(run.id)
    return candidates


async def retain_run_artifacts(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    storage_dir: Path,
    keep_count: int,
    keep_days: int,
    now: datetime | None = None,
) -> RetentionResult:
    """Delete terminal artifacts beyond either per-user retention limit."""

    if keep_count < 0 or keep_days < 0:
        raise ValueError("retention limits must be non-negative")
    observed_at = now or datetime.now(timezone.utc)
    async with session_factory() as session:
        runs = list(
            (
                await session.scalars(
                    select(TrainingRun).where(
                        TrainingRun.state.in_(TERMINAL_RUN_STATES),
                        TrainingRun.artifacts_deleted_at.is_(None),
                    )
                )
            ).all()
        )
    candidate_ids = _retention_candidates(
        runs,
        keep_count=keep_count,
        keep_days=keep_days,
        now=observed_at,
    )

    removed_runs = 0
    removed_bytes = 0
    for run_id in candidate_ids:
        async with session_factory() as session:
            run = await session.scalar(
                select(TrainingRun)
                .where(
                    TrainingRun.id == run_id,
                    TrainingRun.state.in_(TERMINAL_RUN_STATES),
                    TrainingRun.artifacts_deleted_at.is_(None),
                )
                .with_for_update()
            )
            if run is None:
                continue
            artifact_bytes = run.artifact_bytes
            if artifact_bytes is None:
                try:
                    run_root = contained_storage_path(storage_dir, run.out_dir)
                except StorageBoundaryError:
                    artifact_bytes = 0
                else:
                    artifact_bytes = await asyncio.to_thread(
                        path_tree_bytes,
                        run_root / "artifacts",
                    )
            staged = await asyncio.to_thread(
                stage_run_artifacts,
                storage_dir,
                run.out_dir,
            )
            run.artifacts_deleted_at = observed_at
            run.artifact_bytes = 0
            owner_id = run.owner_id
            try:
                await session.commit()
            except Exception:
                await session.rollback()
                await asyncio.to_thread(restore_staged_deletion, staged)
                raise

        await asyncio.to_thread(finalize_staged_deletion, staged)
        async with session_factory() as session:
            await decrease_bytes_used(session, owner_id, artifact_bytes)
            await session.commit()
        removed_runs += 1
        removed_bytes += artifact_bytes

    return RetentionResult(
        removed_runs=removed_runs,
        removed_bytes=removed_bytes,
    )
