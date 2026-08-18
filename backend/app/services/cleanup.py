"""Staged cleanup for persisted training-run directories."""

from __future__ import annotations

import asyncio
import os
import shutil
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db import set_local_lock_timeout
from app.models import Dataset, TrainingRun, UploadJob, UploadSession
from app.services.quota import decrease_bytes_used, path_tree_bytes
from app.services.storage import (
    StagedDeletion,
    StorageBoundaryError,
    contained_storage_path,
    finalize_staged_deletions,
    restore_staged_deletions,
    stage_deletions_async,
    stage_dataset_deletion,
    storage_root,
)


TERMINAL_RUN_STATES = frozenset({"done", "failed", "canceled"})
ACTIVE_UPLOAD_JOB_STATES = frozenset(
    {"queued", "running", "awaiting_class_resolution"}
)
PENDING_DELETION_MIN_AGE_SECONDS = 60.0


@dataclass(frozen=True)
class RetentionResult:
    removed_runs: int = 0
    removed_bytes: int = 0
    finalized_pending: int = 0


@dataclass(frozen=True)
class UploadGcResult:
    reclaimed_directories: int = 0
    orphan_directories: int = 0
    expired_sessions: int = 0
    failed_jobs: int = 0


def _upload_root(root: Path) -> Path:
    uploads = storage_root(root) / "uploads"
    if uploads.exists() and (uploads.is_symlink() or not uploads.is_dir()):
        raise StorageBoundaryError("uploads must be a real directory")
    uploads.mkdir(parents=True, exist_ok=True)
    return uploads


def _latest_tree_mtime(path: Path) -> float:
    """Return the newest lstat mtime without following directory symlinks."""
    latest = path.lstat().st_mtime
    for directory, names, files in os.walk(path, followlinks=False):
        root = Path(directory)
        for name in (*names, *files):
            candidate = root / name
            try:
                latest = max(latest, candidate.lstat().st_mtime)
            except FileNotFoundError:
                continue
    return latest


async def _stage_upload_paths(
    storage_dir: Path,
    paths: list[Path],
) -> list[StagedDeletion | None]:
    return await stage_deletions_async(storage_dir, paths)


async def _fail_processing_dataset(
    session_factory: async_sessionmaker[AsyncSession],
    dataset_id: int,
) -> None:
    """Project upload failure without extending the upload-lock transaction."""
    async with session_factory() as session:
        await set_local_lock_timeout(session)
        active_job_exists = (
            select(UploadJob.id)
            .where(
                UploadJob.dataset_id == dataset_id,
                UploadJob.state.in_(ACTIVE_UPLOAD_JOB_STATES),
            )
            .exists()
        )
        await session.execute(
            update(Dataset)
            .where(
                Dataset.id == dataset_id,
                Dataset.status == "processing",
                ~active_job_exists,
            )
            .values(status="failed")
        )
        await session.commit()


async def sweep_upload_storage(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    storage_dir: Path,
    ttl_hours: int = 24,
    resolution_ttl_days: int = 7,
    now: datetime | None = None,
) -> UploadGcResult:
    """Reclaim orphaned, terminal, or inactive upload directories."""
    if ttl_hours <= 0 or resolution_ttl_days <= 0:
        raise ValueError("upload GC TTL values must be positive")
    observed_at = now or datetime.now(timezone.utc)
    upload_root = await asyncio.to_thread(_upload_root, storage_dir)
    directories = tuple(
        path
        for path in upload_root.iterdir()
        if path.is_dir() and not path.is_symlink()
    )
    processed_ids: set[int] = set()
    reclaimed = orphaned = expired = failed_jobs = 0

    for directory in directories:
        try:
            upload_id = int(directory.name)
        except ValueError:
            upload_id = -1
        if upload_id <= 0:
            staged = await _stage_upload_paths(storage_dir, [directory])
            await asyncio.to_thread(finalize_staged_deletions, staged)
            reclaimed += int(staged[0] is not None)
            orphaned += int(staged[0] is not None)
            continue
        if upload_id in processed_ids:
            continue

        dataset_id_to_fail: int | None = None
        async with session_factory() as session:
            await set_local_lock_timeout(session)
            upload = await session.scalar(
                select(UploadSession).where(UploadSession.id == upload_id)
            )
            if upload is None:
                staged = await _stage_upload_paths(storage_dir, [directory])
                await asyncio.to_thread(finalize_staged_deletions, staged)
                reclaimed += int(staged[0] is not None)
                orphaned += int(staged[0] is not None)
                continue

            snapshot_jobs = list(
                (
                    await session.scalars(
                        select(UploadJob)
                        .where(UploadJob.dataset_id == upload.dataset_id)
                        .order_by(UploadJob.id.desc())
                    )
                ).all()
            )
            snapshot_job = next(
                (
                    candidate
                    for candidate in snapshot_jobs
                    if upload_id in (candidate.upload_ids or [])
                ),
                None,
            )
            group_ids = sorted(
                {
                    item
                    for item in (
                        (snapshot_job.upload_ids or [])
                        if snapshot_job
                        else [upload_id]
                    )
                    if isinstance(item, int) and item > 0
                }
            )
            uploads = list(
                (
                    await session.scalars(
                        select(UploadSession)
                        .where(UploadSession.id.in_(group_ids))
                        .order_by(UploadSession.id)
                        .with_for_update()
                        .execution_options(populate_existing=True)
                    )
                ).all()
            )
            locked_ids = [item.id for item in uploads]
            locked_upload = next(
                (item for item in uploads if item.id == upload_id),
                None,
            )
            if (
                locked_ids != group_ids
                or locked_upload is None
                or any(
                    item.dataset_id != locked_upload.dataset_id
                    for item in uploads
                )
            ):
                await session.rollback()
                continue

            dataset_jobs = list(
                (
                    await session.scalars(
                        select(UploadJob)
                        .where(
                            UploadJob.dataset_id == locked_upload.dataset_id
                        )
                        .order_by(UploadJob.id.desc())
                        .with_for_update()
                        .execution_options(populate_existing=True)
                    )
                ).all()
            )
            job = next(
                (
                    candidate
                    for candidate in dataset_jobs
                    if upload_id in (candidate.upload_ids or [])
                ),
                None,
            )
            locked_group_ids = sorted(
                {
                    item
                    for item in (
                        (job.upload_ids or []) if job else [upload_id]
                    )
                    if isinstance(item, int) and item > 0
                }
            )
            if (
                locked_group_ids != group_ids
                or (
                    snapshot_job is not None
                    and (job is None or job.id != snapshot_job.id)
                )
            ):
                await session.rollback()
                continue

            group_paths = [upload_root / str(item.id) for item in uploads]
            existing_paths = [path for path in group_paths if path.is_dir()]
            terminal = (
                job is not None and job.state in {"done", "failed"}
            ) or (job is None and locked_upload.state == "aborted")
            awaiting_resolution = (
                job is not None and job.state == "awaiting_class_resolution"
            )
            ttl = timedelta(
                days=resolution_ttl_days
                if awaiting_resolution
                else 0,
                hours=0 if awaiting_resolution else ttl_hours,
            )
            mtimes = [
                await asyncio.to_thread(_latest_tree_mtime, path)
                for path in existing_paths
            ]
            latest_mtime = max(mtimes, default=observed_at.timestamp())
            stale = latest_mtime < (observed_at - ttl).timestamp()
            if not terminal and not stale:
                processed_ids.update(group_ids)
                continue

            staged = await _stage_upload_paths(storage_dir, existing_paths)
            try:
                if not terminal:
                    expired_upload_count = len(uploads)
                    for item in uploads:
                        item.state = "aborted"
                    if job is not None and job.state not in {"done", "failed"}:
                        job.state = "failed"
                        job.phase = "failed"
                        job.failed += expired_upload_count
                        has_active_sibling = any(
                            candidate.id != job.id
                            and candidate.state in ACTIVE_UPLOAD_JOB_STATES
                            for candidate in dataset_jobs
                        )
                        if not has_active_sibling:
                            dataset_id_to_fail = job.dataset_id
                        failed_jobs += 1
                    expired += expired_upload_count
                await session.commit()
            except BaseException as error:
                restore_staged_deletions(reversed(staged))
                try:
                    await session.rollback()
                except BaseException as rollback_error:
                    error.add_note(
                        "upload sweep rollback also failed: "
                        f"{type(rollback_error).__name__}"
                    )
                raise

        if dataset_id_to_fail is not None:
            await _fail_processing_dataset(
                session_factory,
                dataset_id_to_fail,
            )
        await asyncio.to_thread(finalize_staged_deletions, staged)
        reclaimed += sum(item is not None for item in staged)
        processed_ids.update(group_ids)

    return UploadGcResult(
        reclaimed_directories=reclaimed,
        orphan_directories=orphaned,
        expired_sessions=expired,
        failed_jobs=failed_jobs,
    )


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


def contained_training_run_path(
    root: Path,
    out_dir: str | Path,
) -> Path:
    """Return one validated direct training-run path for coordinated cleanup."""

    return _contained_run_root(root, out_dir)


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


async def _stage_run_subdirectory_async(
    root: Path,
    out_dir: str | Path,
    name: str,
) -> StagedDeletion | None:
    """Cancellation-safe async quarantine for one validated run child."""

    resolved_root = storage_root(root)
    run_root = _contained_run_root(resolved_root, out_dir)
    return (await stage_deletions_async(resolved_root, [run_root / name]))[0]


async def stage_run_workdir_async(
    root: Path,
    out_dir: str | Path,
) -> StagedDeletion | None:
    return await _stage_run_subdirectory_async(root, out_dir, "workdir")


def stage_run_artifacts(
    root: Path,
    out_dir: str | Path,
) -> StagedDeletion | None:
    return _stage_run_subdirectory(root, out_dir, "artifacts")


async def stage_run_artifacts_async(
    root: Path,
    out_dir: str | Path,
) -> StagedDeletion | None:
    return await _stage_run_subdirectory_async(root, out_dir, "artifacts")


def stage_training_run_deletion(
    root: Path,
    out_dir: str | Path,
) -> StagedDeletion | None:
    """Quarantine a complete run only when it is a direct training-runs child."""
    resolved_root = storage_root(root)
    run_root = _contained_run_root(resolved_root, out_dir)
    return stage_dataset_deletion(resolved_root, run_root)


async def stage_training_run_deletion_async(
    root: Path,
    out_dir: str | Path,
) -> StagedDeletion | None:
    """Cancellation-safe quarantine of one direct training-run directory."""

    resolved_root = storage_root(root)
    run_root = _contained_run_root(resolved_root, out_dir)
    return (await stage_deletions_async(resolved_root, [run_root]))[0]


def finalize_pending_deletions(root: Path) -> RetentionResult:
    """Finish aged quarantine scopes without racing active requests."""

    pending_root = storage_root(root) / ".delete-pending"
    if not pending_root.exists():
        return RetentionResult()
    if pending_root.is_symlink() or not pending_root.is_dir():
        raise StorageBoundaryError(".delete-pending must be a real directory")

    finalized = 0
    observed_at = time.time()
    for candidate in tuple(pending_root.iterdir()):
        if candidate.parent != pending_root:
            continue
        try:
            age_seconds = observed_at - candidate.lstat().st_mtime
        except FileNotFoundError:
            continue
        if age_seconds < PENDING_DELETION_MIN_AGE_SECONDS:
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
            staged = await stage_run_artifacts_async(
                storage_dir,
                run.out_dir,
            )
            run.artifacts_deleted_at = observed_at
            run.artifact_bytes = 0
            owner_id = run.owner_id
            try:
                await session.commit()
            except BaseException as error:
                restore_staged_deletions([staged])
                try:
                    await session.rollback()
                except BaseException as rollback_error:
                    error.add_note(
                        "retention rollback also failed: "
                        f"{type(rollback_error).__name__}"
                    )
                raise

        await asyncio.to_thread(finalize_staged_deletions, [staged])
        async with session_factory() as session:
            await decrease_bytes_used(session, owner_id, artifact_bytes)
            await session.commit()
        removed_runs += 1
        removed_bytes += artifact_bytes

    return RetentionResult(
        removed_runs=removed_runs,
        removed_bytes=removed_bytes,
    )
