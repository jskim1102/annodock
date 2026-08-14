"""Atomic training cancellation with process-group identity protection."""

from __future__ import annotations

import asyncio
import os
import signal
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import TrainingRun
from app.services.proc_identity import process_identity_matches
from app.services.quota import increase_bytes_used
from app.services.rundir import collect_run_artifacts
from app.services.storage import StorageBoundaryError, contained_storage_path


CANCEL_TERM_GRACE_SECONDS = 5.0
CANCEL_KILL_WAIT_SECONDS = 2.0
PROCESS_POLL_INTERVAL_SECONDS = 0.05
_LOCAL_CANCELS_IN_PROGRESS: set[int] = set()


class TrainingRunNotFoundError(LookupError):
    pass


class TrainingRunNotCancelableError(RuntimeError):
    pass


class ProcessGroupTerminationError(RuntimeError):
    pass


@dataclass(frozen=True)
class CancelResult:
    run_id: int
    state: str


def is_cancel_in_progress(run_id: int) -> bool:
    """Tell the local reaper that the request path owns this transition."""
    return run_id in _LOCAL_CANCELS_IN_PROGRESS


def process_group_exists(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def reap_child_process(pid: int) -> int | None:
    """Reap a worker leader when this backend is still its parent."""
    try:
        reaped_pid, status = os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        return None
    if reaped_pid == 0:
        return None
    if os.WIFSIGNALED(status):
        return 128 + os.WTERMSIG(status)
    if os.WIFEXITED(status):
        return os.WEXITSTATUS(status)
    return None


async def _wait_for_group_exit(
    pgid: int,
    leader_pid: int,
    timeout_seconds: float,
    poll_interval_seconds: float,
) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds
    while True:
        reap_child_process(leader_pid)
        if not process_group_exists(pgid):
            return True
        if loop.time() >= deadline:
            return False
        await asyncio.sleep(poll_interval_seconds)


async def _finish_canceled(
    session_factory: async_sessionmaker[AsyncSession],
    run_id: int,
    owner_id: int,
    *,
    storage_dir: Path | None = None,
) -> CancelResult:
    async with session_factory() as session:
        run = await session.scalar(
            select(TrainingRun)
            .where(
                TrainingRun.id == run_id,
                TrainingRun.owner_id == owner_id,
                TrainingRun.state == "canceling",
            )
            .with_for_update()
        )
        if run is None:
            raise ProcessGroupTerminationError(
                f"training run {run_id} left canceling before terminal write"
            )
        artifact_bytes = 0
        if storage_dir is not None:
            try:
                out_dir = contained_storage_path(storage_dir, run.out_dir)
                artifact_bytes = await asyncio.to_thread(
                    collect_run_artifacts,
                    out_dir,
                )
            except (OSError, StorageBoundaryError, ValueError):
                artifact_bytes = 0
        run.state = "canceled"
        if run.finished_at is None:
            run.finished_at = datetime.now(timezone.utc)
        run.artifact_bytes = artifact_bytes
        await increase_bytes_used(session, owner_id, artifact_bytes)
        await session.commit()
    return CancelResult(run_id=run_id, state="canceled")


async def _claim_cancel(
    session_factory: async_sessionmaker[AsyncSession],
    run_id: int,
    owner_id: int,
):
    async with session_factory() as session:
        result = await session.execute(
            update(TrainingRun)
            .where(
                TrainingRun.id == run_id,
                TrainingRun.owner_id == owner_id,
                TrainingRun.state.in_(("queued", "running")),
            )
            .values(state="canceling")
            .returning(
                TrainingRun.pid,
                TrainingRun.pid_started_at,
                TrainingRun.boot_id,
            )
        )
        row = result.one_or_none()
        if row is None:
            exists = await session.scalar(
                select(TrainingRun.id).where(
                    TrainingRun.id == run_id,
                    TrainingRun.owner_id == owner_id,
                )
            )
            await session.rollback()
            if exists is None:
                raise TrainingRunNotFoundError(run_id)
            raise TrainingRunNotCancelableError(run_id)
        await session.commit()
        return row


async def cancel_training_run(
    session_factory: async_sessionmaker[AsyncSession],
    run_id: int,
    owner_id: int,
    *,
    term_grace_seconds: float = CANCEL_TERM_GRACE_SECONDS,
    kill_wait_seconds: float = CANCEL_KILL_WAIT_SECONDS,
    poll_interval_seconds: float = PROCESS_POLL_INTERVAL_SECONDS,
    storage_dir: Path | None = None,
) -> CancelResult:
    """Cancel one active run, writing terminal state only after group death."""
    _LOCAL_CANCELS_IN_PROGRESS.add(run_id)
    try:
        claimed = await _claim_cancel(session_factory, run_id, owner_id)
        pid, pid_started_at, boot_id = claimed
        if pid is None:
            return await _finish_canceled(
                session_factory,
                run_id,
                owner_id,
                storage_dir=storage_dir,
            )

        if not process_identity_matches(pid, pid_started_at, boot_id):
            return await _finish_canceled(
                session_factory,
                run_id,
                owner_id,
                storage_dir=storage_dir,
            )
        try:
            pgid = os.getpgid(pid)
        except ProcessLookupError:
            return await _finish_canceled(
                session_factory,
                run_id,
                owner_id,
                storage_dir=storage_dir,
            )
        if pgid != pid:
            # start_new_session=True guarantees leader PID == PGID. A live
            # mismatch cannot be declared terminal and must not be signaled.
            raise ProcessGroupTerminationError(
                f"training PID {pid} is not its process-group leader"
            )

        try:
            os.killpg(pgid, signal.SIGTERM)
        except ProcessLookupError:
            return await _finish_canceled(
                session_factory,
                run_id,
                owner_id,
                storage_dir=storage_dir,
            )
        if await _wait_for_group_exit(
            pgid,
            pid,
            term_grace_seconds,
            poll_interval_seconds,
        ):
            return await _finish_canceled(
                session_factory,
                run_id,
                owner_id,
                storage_dir=storage_dir,
            )

        # Recheck the persisted leader before escalation. If TERM already
        # reaped the leader but its original group still contains children,
        # that group cannot be reassigned while it exists.
        leader_still_matches = process_identity_matches(
            pid,
            pid_started_at,
            boot_id,
        )
        if not leader_still_matches and not process_group_exists(pgid):
            return await _finish_canceled(
                session_factory,
                run_id,
                owner_id,
                storage_dir=storage_dir,
            )
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            return await _finish_canceled(
                session_factory,
                run_id,
                owner_id,
                storage_dir=storage_dir,
            )
        if not await _wait_for_group_exit(
            pgid,
            pid,
            kill_wait_seconds,
            poll_interval_seconds,
        ):
            raise ProcessGroupTerminationError(
                f"training process group {pgid} did not exit after SIGKILL"
            )
        return await _finish_canceled(
            session_factory,
            run_id,
            owner_id,
            storage_dir=storage_dir,
        )
    finally:
        _LOCAL_CANCELS_IN_PROGRESS.discard(run_id)
