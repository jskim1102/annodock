"""Periodic reconciliation of active training runs and detached workers."""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import TrainingRun
from app.services.cancel import is_cancel_in_progress
from app.services.cleanup import (
    finalize_pending_deletions,
    retain_run_artifacts,
)
from app.services.proc_identity import ProcessIdentity, read_process_identity
from app.services.training import mark_training_failed
from app.services.storage import contained_storage_path
from app.worker.failure import FailureReport, classify_failure


REAPER_INTERVAL_SECONDS = 5.0
SPAWN_IDENTITY_GRACE_SECONDS = 15.0


@dataclass(frozen=True)
class ReaperResult:
    preserved: int = 0
    failed: int = 0
    pending_identity: int = 0


def read_child_exit_code(pid: int) -> int | None:
    """Return shell-style exit code when this backend can reap the worker."""
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


def _dead_process_report(
    run: TrainingRun,
    identity: ProcessIdentity | None,
    exit_code: int | None,
    storage_dir: Path | None,
) -> FailureReport:
    out_dir = (
        contained_storage_path(storage_dir, run.out_dir)
        if storage_dir is not None
        else Path(run.out_dir)
    )
    report = classify_failure(
        exit_code=exit_code,
        out_dir=out_dir,
    )
    if identity is not None and identity.boot_id != run.boot_id:
        return replace(
            report,
            reason="호스트 재부팅으로 기존 학습 워커를 재인식할 수 없습니다.\n"
            + report.reason,
        )
    if identity is not None and identity.started_at != run.pid_started_at:
        return replace(
            report,
            reason="PID가 다른 프로세스에 재사용되어 학습 워커를 재인식할 수 없습니다.\n"
            + report.reason,
        )
    return report


async def reconcile_training_runs(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    storage_dir: Path | None = None,
    spawn_grace_seconds: float = SPAWN_IDENTITY_GRACE_SECONDS,
    now: datetime | None = None,
) -> ReaperResult:
    """Reconcile every active row once; safe to repeat or run concurrently."""
    observed_at = now or datetime.now(timezone.utc)
    async with session_factory() as session:
        runs = (
            await session.scalars(
                select(TrainingRun).where(
                    TrainingRun.state.in_(("running", "canceling"))
                )
            )
        ).all()

    preserved = failed = pending_identity = 0
    for run in runs:
        if is_cancel_in_progress(run.id):
            preserved += 1
            continue
        if run.pid is None:
            started_at = run.started_at
            age_seconds = (
                (observed_at - started_at).total_seconds()
                if started_at is not None
                else spawn_grace_seconds + 1
            )
            if age_seconds <= spawn_grace_seconds:
                pending_identity += 1
                continue
            reason = (
                "학습 워커 PID가 제한 시간 안에 기록되지 않아 실패로 처리했습니다."
            )
            async with session_factory() as session:
                changed = await mark_training_failed(
                    session,
                    run.id,
                    reason,
                    active_states=("running", "canceling"),
                    storage_dir=storage_dir,
                )
            failed += int(changed)
            continue

        identity = read_process_identity(run.pid)
        if (
            identity is not None
            and identity.started_at == run.pid_started_at
            and identity.boot_id == run.boot_id
        ):
            preserved += 1
            continue

        exit_code = read_child_exit_code(run.pid) if identity is None else None
        report = await asyncio.to_thread(
            _dead_process_report,
            run,
            identity,
            exit_code,
            storage_dir,
        )
        async with session_factory() as session:
            changed = await mark_training_failed(
                session,
                run.id,
                report.reason,
                active_states=("running", "canceling"),
                storage_dir=storage_dir,
            )
        failed += int(changed)
    return ReaperResult(
        preserved=preserved,
        failed=failed,
        pending_identity=pending_identity,
    )


async def run_reaper_loop(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    storage_dir: Path,
    interval_seconds: float = REAPER_INTERVAL_SECONDS,
    spawn_grace_seconds: float = SPAWN_IDENTITY_GRACE_SECONDS,
    keep_count: int = 10,
    keep_days: int = 30,
) -> None:
    """Run reconciliation without blocking the FastAPI event loop."""
    while True:
        try:
            await reconcile_training_runs(
                session_factory,
                storage_dir=storage_dir,
                spawn_grace_seconds=spawn_grace_seconds,
            )
            await retain_run_artifacts(
                session_factory,
                storage_dir=storage_dir,
                keep_count=keep_count,
                keep_days=keep_days,
            )
            await asyncio.to_thread(
                finalize_pending_deletions,
                storage_dir,
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            print(f"training reaper iteration failed: {error}", flush=True)
        await asyncio.sleep(interval_seconds)
