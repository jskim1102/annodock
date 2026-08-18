from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from app.models import TrainingRun
from app.services import cancel, training
from app.services.cancel import cancel_training_run
from app.services.quota import get_bytes_used, path_tree_bytes
from app.services.storage import storage_relative_path
from app.services.training import mark_training_failed
from app.worker import failure
from app.worker.failure import FailureReport, persist_worker_failure


pytestmark = pytest.mark.asyncio


async def _run(
    app,
    *,
    state: str = "running",
    confirmed_pid: bool = True,
) -> tuple[int, Path]:
    storage_dir = app.state.settings.storage_dir
    async with app.state.session_factory() as session:
        run = TrainingRun(
            owner_id=1,
            dataset_id=None,
            dataset_name=f"test-workdir-cleanup-{uuid4().hex}",
            weights="yolo26n.pt",
            epochs=1,
            imgsz=64,
            batch=1,
            split_mode="2way",
            ratios={"train": 0.8, "valid": 0.2},
            seed=1,
            state=state,
            pid=999_999_999 if confirmed_pid else None,
            pid_started_at="1" if confirmed_pid else None,
            boot_id="test-boot" if confirmed_pid else None,
            started_at=datetime.now(timezone.utc),
            out_dir="pending",
        )
        session.add(run)
        await session.flush()
        out_dir = storage_dir / "training-runs" / str(run.id)
        run.out_dir = storage_relative_path(storage_dir, out_dir)
        await session.commit()
        run_id = run.id
    workdir = out_dir / "workdir" / "train"
    artifacts = out_dir / "artifacts"
    (workdir / "weights").mkdir(parents=True)
    artifacts.mkdir(parents=True)
    (workdir / "weights" / "best.pt").write_bytes(b"best-weights")
    (workdir / "weights" / "last.pt").write_bytes(b"partial-weights")
    (workdir / "results.csv").write_bytes(b"epoch,metric\n")
    (artifacts / "log").write_bytes(b"partial-log")
    return run_id, out_dir


async def _assert_terminal_cleanup(app, run_id: int, out_dir: Path, state: str) -> None:
    assert not (out_dir / "workdir").exists()
    assert (out_dir / "artifacts" / "last.pt").is_file()
    async with app.state.session_factory() as session:
        run = await session.get(TrainingRun, run_id)
        assert run is not None and run.state == state
        artifact_bytes = sum(
            path.stat().st_size for path in (out_dir / "artifacts").iterdir()
        )
        assert run.artifact_bytes == artifact_bytes
        assert await get_bytes_used(session, 1) == artifact_bytes


def _partially_collect_then_fail(out_dir: str | Path) -> int:
    root = Path(out_dir)
    source = root / "workdir" / "train" / "weights" / "last.pt"
    target = root / "artifacts" / "last.pt"
    os.replace(source, target)
    raise OSError("injected artifact collection failure")


async def _assert_preserved_after_collection_failure(
    app,
    run_id: int,
    out_dir: Path,
    state: str,
) -> None:
    assert (out_dir / "workdir").is_dir()
    assert (out_dir / "workdir" / "train" / "weights" / "best.pt").is_file()
    artifact_bytes = path_tree_bytes(out_dir / "artifacts")
    async with app.state.session_factory() as session:
        run = await session.get(TrainingRun, run_id)
        assert run is not None and run.state == state
        assert run.artifact_bytes == artifact_bytes
        assert await get_bytes_used(session, 1) == artifact_bytes


async def test_async_failure_reclaims_workdir_after_collecting_artifacts(app) -> None:
    run_id, out_dir = await _run(app)
    async with app.state.session_factory() as session:
        assert await mark_training_failed(
            session,
            run_id,
            "test failure",
            owner_id=1,
            storage_dir=app.state.settings.storage_dir,
        )
    await _assert_terminal_cleanup(app, run_id, out_dir, "failed")


async def test_worker_failure_reclaims_workdir_after_database_commit(app) -> None:
    run_id, out_dir = await _run(app)
    changed = await asyncio.to_thread(
        persist_worker_failure,
        run_id,
        1,
        app.state.settings.database_url.replace("+asyncpg", "", 1),
        FailureReport(
            reason="worker failure",
            is_oom=False,
            effective_batch=None,
            exit_code=1,
        ),
        out_dir=out_dir,
        storage_dir=app.state.settings.storage_dir,
    )
    assert changed is True
    await _assert_terminal_cleanup(app, run_id, out_dir, "failed")


async def test_cancel_reclaims_workdir_after_collecting_artifacts(app) -> None:
    run_id, out_dir = await _run(app)
    result = await cancel_training_run(
        app.state.session_factory,
        run_id,
        1,
        storage_dir=app.state.settings.storage_dir,
    )
    assert result.state == "canceled"
    await _assert_terminal_cleanup(app, run_id, out_dir, "canceled")


async def test_async_failure_preserves_workdir_when_artifact_collection_fails(
    app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id, out_dir = await _run(app)
    monkeypatch.setattr(
        training,
        "collect_run_artifacts",
        _partially_collect_then_fail,
    )

    async with app.state.session_factory() as session:
        assert await training.mark_training_failed(
            session,
            run_id,
            "test failure",
            owner_id=1,
            storage_dir=app.state.settings.storage_dir,
        )

    await _assert_preserved_after_collection_failure(
        app,
        run_id,
        out_dir,
        "failed",
    )


async def test_worker_failure_preserves_workdir_when_artifact_collection_fails(
    app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id, out_dir = await _run(app)
    monkeypatch.setattr(
        failure,
        "collect_run_artifacts",
        _partially_collect_then_fail,
    )

    changed = await asyncio.to_thread(
        failure.persist_worker_failure,
        run_id,
        1,
        app.state.settings.database_url.replace("+asyncpg", "", 1),
        FailureReport(
            reason="worker failure",
            is_oom=False,
            effective_batch=None,
            exit_code=1,
        ),
        out_dir=out_dir,
        storage_dir=app.state.settings.storage_dir,
    )

    assert changed is True
    await _assert_preserved_after_collection_failure(
        app,
        run_id,
        out_dir,
        "failed",
    )


async def test_worker_failure_records_terminal_state_when_workdir_stage_fails(
    app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id, out_dir = await _run(app)

    def fail_stage(_storage_dir: Path, _out_dir: str | Path) -> None:
        raise OSError("injected workdir staging failure")

    monkeypatch.setattr(failure, "stage_run_workdir", fail_stage)

    changed = await asyncio.to_thread(
        failure.persist_worker_failure,
        run_id,
        1,
        app.state.settings.database_url.replace("+asyncpg", "", 1),
        FailureReport(
            reason="worker failure",
            is_oom=False,
            effective_batch=None,
            exit_code=1,
        ),
        out_dir=out_dir,
        storage_dir=app.state.settings.storage_dir,
    )

    assert changed is True
    assert (out_dir / "workdir").is_dir()
    async with app.state.session_factory() as session:
        run = await session.get(TrainingRun, run_id)
        assert run is not None and run.state == "failed"


async def test_cancel_preserves_workdir_when_artifact_collection_fails(
    app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id, out_dir = await _run(app)
    monkeypatch.setattr(
        cancel,
        "collect_run_artifacts",
        _partially_collect_then_fail,
    )

    result = await cancel.cancel_training_run(
        app.state.session_factory,
        run_id,
        1,
        storage_dir=app.state.settings.storage_dir,
    )

    assert result.state == "canceled"
    await _assert_preserved_after_collection_failure(
        app,
        run_id,
        out_dir,
        "canceled",
    )


async def test_cancel_records_terminal_state_when_run_path_resolution_fails(
    app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id, out_dir = await _run(app)

    def fail_path(_storage_dir: Path, _out_dir: str | Path) -> Path:
        raise OSError("injected path resolution failure")

    monkeypatch.setattr(cancel, "contained_storage_path", fail_path)

    result = await cancel.cancel_training_run(
        app.state.session_factory,
        run_id,
        1,
        storage_dir=app.state.settings.storage_dir,
    )

    assert result.state == "canceled"
    assert (out_dir / "workdir").is_dir()
    async with app.state.session_factory() as session:
        run = await session.get(TrainingRun, run_id)
        assert run is not None and run.state == "canceled"


async def test_async_failure_with_unconfirmed_pid_does_not_stage_workdir(app) -> None:
    run_id, out_dir = await _run(app, confirmed_pid=False)

    async with app.state.session_factory() as session:
        assert await mark_training_failed(
            session,
            run_id,
            "PID was not confirmed",
            owner_id=1,
            storage_dir=app.state.settings.storage_dir,
        )

    assert (out_dir / "workdir").is_dir()
    assert (out_dir / "workdir" / "train" / "weights" / "best.pt").is_file()
    async with app.state.session_factory() as session:
        run = await session.get(TrainingRun, run_id)
        assert run is not None and run.state == "failed"
