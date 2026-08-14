from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from app import main
from app.models import TrainingRun
from app.services import reaper
from app.services.proc_identity import (
    ProcessIdentity,
    parse_proc_stat,
    read_process_identity,
)


pytestmark = pytest.mark.asyncio


def _run(
    out_dir: Path,
    *,
    pid: int | None,
    pid_started_at: str | None,
    boot_id: str | None,
    started_at: datetime | None = None,
    state: str = "running",
) -> TrainingRun:
    return TrainingRun(
        owner_id=1,
        dataset_id=None,
        dataset_name=f"test-reaper-{uuid4().hex}",
        weights="yolo26n.pt",
        epochs=1,
        imgsz=640,
        batch=4,
        split_mode="2way",
        ratios={"train": 0.8, "valid": 0.2},
        seed=1,
        state=state,
        pid=pid,
        pid_started_at=pid_started_at,
        boot_id=boot_id,
        started_at=started_at or datetime.now(timezone.utc),
        out_dir=str(out_dir),
    )


async def _persist(app, run: TrainingRun) -> int:
    async with app.state.session_factory() as session:
        session.add(run)
        await session.commit()
        return run.id


async def test_reaper_preserves_live_matching_process(app, tmp_path: Path) -> None:
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        start_new_session=True,
    )
    try:
        identity = read_process_identity(process.pid)
        assert identity is not None
        run_id = await _persist(
            app,
            _run(
                tmp_path,
                pid=process.pid,
                pid_started_at=identity.started_at,
                boot_id=identity.boot_id,
            ),
        )

        result = await reaper.reconcile_training_runs(app.state.session_factory)

        assert result.preserved == 1
        assert result.failed == 0
        async with app.state.session_factory() as session:
            persisted = await session.get(TrainingRun, run_id)
            assert persisted is not None
            assert persisted.state == "running"
    finally:
        process.kill()
        process.wait(timeout=2)


async def test_reaper_fails_dead_process_with_exit_137_reason(
    app,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = await _persist(
        app,
        _run(tmp_path, pid=999_999_999, pid_started_at="1", boot_id="boot"),
    )
    monkeypatch.setattr(reaper, "read_process_identity", lambda _pid: None)
    monkeypatch.setattr(reaper, "read_child_exit_code", lambda _pid: 137)

    result = await reaper.reconcile_training_runs(app.state.session_factory)
    repeated = await reaper.reconcile_training_runs(app.state.session_factory)

    assert result.failed == 1
    assert repeated.failed == 0
    async with app.state.session_factory() as session:
        persisted = await session.get(TrainingRun, run_id)
        assert persisted is not None
        assert persisted.state == "failed"
        assert "137" in (persisted.error or "")


async def test_reaper_fails_boot_mismatch_without_touching_process(
    app,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = await _persist(
        app,
        _run(tmp_path, pid=123, pid_started_at="22", boot_id="old-boot"),
    )
    monkeypatch.setattr(
        reaper,
        "read_process_identity",
        lambda pid: ProcessIdentity(
            pid=pid,
            state="S",
            started_at="22",
            boot_id="new-boot",
        ),
    )

    result = await reaper.reconcile_training_runs(app.state.session_factory)

    assert result.failed == 1
    async with app.state.session_factory() as session:
        persisted = await session.get(TrainingRun, run_id)
        assert persisted is not None
        assert persisted.state == "failed"
        assert "재부팅" in (persisted.error or "")


async def test_reaper_pid_null_uses_spawn_grace(app, tmp_path: Path) -> None:
    old_run_id = await _persist(
        app,
        _run(
            tmp_path,
            pid=None,
            pid_started_at=None,
            boot_id=None,
            started_at=datetime.now(timezone.utc) - timedelta(seconds=30),
        ),
    )

    result = await reaper.reconcile_training_runs(
        app.state.session_factory,
        spawn_grace_seconds=15,
    )

    assert result.failed == 1
    async with app.state.session_factory() as session:
        persisted = await session.get(TrainingRun, old_run_id)
        assert persisted is not None
        assert persisted.state == "failed"
        assert "PID" in (persisted.error or "")


async def test_reaper_treats_zombie_as_dead(app, tmp_path: Path) -> None:
    process = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
    raw = ""
    for _ in range(100):
        raw = Path(f"/proc/{process.pid}/stat").read_text()
        state, started_at = parse_proc_stat(raw)
        if state == "Z":
            break
        await asyncio.sleep(0.01)
    assert parse_proc_stat(raw)[0] == "Z"
    boot_id = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    run_id = await _persist(
        app,
        _run(
            tmp_path,
            pid=process.pid,
            pid_started_at=started_at,
            boot_id=boot_id,
        ),
    )

    result = await reaper.reconcile_training_runs(app.state.session_factory)

    assert result.failed == 1
    assert read_process_identity(process.pid) is None
    async with app.state.session_factory() as session:
        persisted = await session.get(TrainingRun, run_id)
        assert persisted is not None
        assert persisted.state == "failed"


async def test_reaper_lifespan_starts_and_stops_periodic_task(
    test_settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = asyncio.Event()

    async def fake_loop(
        _session_factory,
        *,
        storage_dir: Path,
        keep_count: int,
        keep_days: int,
    ) -> None:
        assert storage_dir == test_settings.storage_dir
        assert keep_count == test_settings.run_artifact_keep_count
        assert keep_days == test_settings.run_artifact_keep_days
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(main, "run_reaper_loop", fake_loop)
    application = main.create_app(test_settings, auto_start_jobs=True)
    async with application.router.lifespan_context(application):
        await asyncio.wait_for(started.wait(), timeout=1)
        task = application.state.reaper_task
        assert task.done() is False
    assert task.done() is True
    await application.state.engine.dispose()
