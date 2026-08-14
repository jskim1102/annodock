from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest
import torch

from app.models import TrainingRun
from app.worker import failure


pytestmark = pytest.mark.asyncio


def _run(out_dir: Path) -> TrainingRun:
    return TrainingRun(
        owner_id=1,
        dataset_id=None,
        dataset_name=f"test-failure-{uuid4().hex}",
        weights="yolo26n.pt",
        epochs=1,
        imgsz=640,
        batch=32,
        split_mode="2way",
        ratios={"train": 0.8, "valid": 0.2},
        seed=1,
        state="running",
        started_at=datetime.now(timezone.utc),
        out_dir=str(out_dir),
    )


@pytest.mark.parametrize(
    ("error", "stderr", "exit_code"),
    [
        (torch.cuda.OutOfMemoryError("allocation failed"), "", None),
        (None, "CUDA error: CUBLAS_STATUS_ALLOC_FAILED", None),
        (None, "", 137),
    ],
)
async def test_failure_classifies_all_three_memory_signals(
    tmp_path: Path,
    error: BaseException | None,
    stderr: str,
    exit_code: int | None,
) -> None:
    report = failure.classify_failure(
        error=error,
        stderr=stderr,
        exit_code=exit_code,
        out_dir=tmp_path,
    )

    assert report.is_oom is True
    assert "메모리" in report.reason
    if exit_code is not None:
        assert "137" in report.reason


async def test_failure_filters_dataloader_noise_and_reads_effective_batch(
    tmp_path: Path,
) -> None:
    checkpoint = tmp_path / "workdir" / "train" / "weights" / "last.pt"
    checkpoint.parent.mkdir(parents=True)
    torch.save({"train_args": {"batch": 16}}, checkpoint)
    noisy_tail = "\n".join(
        [
            "RuntimeError: DataLoader worker (pid 7) is killed by signal: Terminated.",
            "ConnectionResetError: [Errno 104] Connection reset by peer",
            "CUDA error: CUBLAS_STATUS_ALLOC_FAILED",
        ]
    )

    report = failure.classify_failure(
        stderr=noisy_tail,
        out_dir=tmp_path,
    )

    assert report.effective_batch == 16
    assert "실제 batch: 16" in report.reason
    assert "CUBLAS_STATUS_ALLOC_FAILED" in report.reason
    assert "DataLoader worker" not in report.reason
    assert "ConnectionResetError" not in report.reason


async def test_failure_persistence_is_idempotent_and_preserves_first_reason(
    app,
    tmp_path: Path,
) -> None:
    async with app.state.session_factory() as session:
        run = _run(tmp_path)
        session.add(run)
        await session.commit()
        run_id = run.id

    dsn = app.state.settings.database_url.replace("+asyncpg", "", 1)
    first = failure.FailureReport(
        reason="첫 실패 사유",
        is_oom=False,
        effective_batch=None,
        exit_code=None,
    )
    second = failure.FailureReport(
        reason="나중 실패 사유",
        is_oom=False,
        effective_batch=None,
        exit_code=None,
    )

    changed_first = await asyncio.to_thread(
        failure.persist_worker_failure,
        run_id,
        1,
        dsn,
        first,
    )
    changed_second = await asyncio.to_thread(
        failure.persist_worker_failure,
        run_id,
        1,
        dsn,
        second,
    )

    assert changed_first is True
    assert changed_second is False
    async with app.state.session_factory() as session:
        persisted = await session.get(TrainingRun, run_id)
        assert persisted is not None
        assert persisted.state == "failed"
        assert persisted.finished_at is not None
        assert persisted.error == "첫 실패 사유"
