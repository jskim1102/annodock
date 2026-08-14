from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.models import RunMetric, TrainingRun
from app.worker import callbacks, failure, train_worker


pytestmark = pytest.mark.asyncio

OWNER_ID = 501
OTHER_OWNER_ID = 502


def make_run(*, owner_id: int, out_dir: str, state: str = "running") -> TrainingRun:
    return TrainingRun(
        owner_id=owner_id,
        dataset_id=None,
        dataset_name=f"test-worker-owner-{uuid4().hex}",
        weights="yolo26n.pt",
        epochs=2,
        imgsz=640,
        batch=4,
        split_mode="2way",
        ratios={"train": 0.8, "valid": 0.2},
        seed=7,
        state=state,
        started_at=datetime.now(timezone.utc),
        out_dir=out_dir,
    )


def metric_trainer() -> SimpleNamespace:
    return SimpleNamespace(
        epoch=0,
        tloss=[0.1, 0.2, 0.3],
        metrics={
            "val/box_loss": 0.4,
            "metrics/mAP50(B)": 0.8,
            "metrics/mAP50-95(B)": 0.6,
        },
        lr={"lr/pg0": 0.01},
        label_loss_items=lambda _loss: {
            "train/box_loss": 0.1,
            "train/cls_loss": 0.2,
            "train/dfl_loss": 0.3,
        },
    )


async def test_worker_reads_metrics_and_completion_are_owner_scoped(
    app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage_dir = app.state.settings.storage_dir
    async with app.state.session_factory() as session:
        run = make_run(owner_id=OWNER_ID, out_dir="training-runs/pending")
        session.add(run)
        await session.flush()
        run_id = run.id
        run.out_dir = f"training-runs/{run_id}"
        await session.commit()

    out_dir = storage_dir / "training-runs" / str(run_id)
    workdir = out_dir / "workdir" / "train"
    weights_dir = workdir / "weights"
    artifacts_dir = out_dir / "artifacts"
    weights_dir.mkdir(parents=True)
    artifacts_dir.mkdir(parents=True)
    best = weights_dir / "best.pt"
    last = weights_dir / "last.pt"
    results = workdir / "results.csv"
    best.write_bytes(b"best")
    last.write_bytes(b"last")
    results.write_text("epoch,metric\n1,0.5\n", encoding="utf-8")
    trainer = SimpleNamespace(best=best, last=last, csv=results)
    dsn = app.state.settings.database_url.replace("+asyncpg", "", 1)
    monkeypatch.setattr(train_worker, "get_settings", lambda: app.state.settings)

    with pytest.raises(LookupError):
        await asyncio.to_thread(
            train_worker.load_run_config,
            run_id,
            OTHER_OWNER_ID,
            dsn,
        )
    config = await asyncio.to_thread(
        train_worker.load_run_config,
        run_id,
        OWNER_ID,
        dsn,
    )
    assert config.run_id == run_id
    assert config.owner_id == OWNER_ID

    await asyncio.to_thread(
        callbacks.make_epoch_callback(run_id, OTHER_OWNER_ID, dsn),
        metric_trainer(),
    )
    async with app.state.session_factory() as session:
        metric_count = await session.scalar(
            select(func.count(RunMetric.id)).where(RunMetric.run_id == run_id)
        )
    assert metric_count == 0

    await asyncio.to_thread(
        callbacks.make_epoch_callback(run_id, OWNER_ID, dsn),
        metric_trainer(),
    )
    async with app.state.session_factory() as session:
        metrics = (
            await session.scalars(
                select(RunMetric).where(RunMetric.run_id == run_id)
            )
        ).all()
    assert len(metrics) == 1
    assert metrics[0].epoch == 1

    changed = await asyncio.to_thread(
        train_worker.complete_run,
        run_id,
        OTHER_OWNER_ID,
        dsn,
        out_dir,
        trainer,
        storage_dir=storage_dir,
    )
    assert changed is False
    async with app.state.session_factory() as session:
        persisted = await session.get(TrainingRun, run_id)
        assert persisted is not None
        assert persisted.state == "running"
        assert persisted.finished_at is None

    changed = await asyncio.to_thread(
        train_worker.complete_run,
        run_id,
        OWNER_ID,
        dsn,
        out_dir,
        trainer,
        storage_dir=storage_dir,
    )
    assert changed is True
    async with app.state.session_factory() as session:
        persisted = await session.get(TrainingRun, run_id)
        assert persisted is not None
        assert persisted.state == "done"


async def test_worker_failure_update_is_owner_scoped(app) -> None:
    async with app.state.session_factory() as session:
        run = make_run(owner_id=OWNER_ID, out_dir="training-runs/failure")
        session.add(run)
        await session.commit()
        run_id = run.id

    dsn = app.state.settings.database_url.replace("+asyncpg", "", 1)
    report = failure.FailureReport(
        reason="소유권 경계 테스트 실패",
        is_oom=False,
        effective_batch=None,
        exit_code=1,
    )

    changed = await asyncio.to_thread(
        failure.persist_worker_failure,
        run_id,
        OTHER_OWNER_ID,
        dsn,
        report,
    )
    assert changed is False
    async with app.state.session_factory() as session:
        persisted = await session.get(TrainingRun, run_id)
        assert persisted is not None
        assert persisted.state == "running"
        assert persisted.error is None
        assert persisted.finished_at is None

    changed = await asyncio.to_thread(
        failure.persist_worker_failure,
        run_id,
        OWNER_ID,
        dsn,
        report,
    )
    assert changed is True
    async with app.state.session_factory() as session:
        persisted = await session.get(TrainingRun, run_id)
        assert persisted is not None
        assert persisted.state == "failed"
        assert persisted.error == report.reason
        assert persisted.finished_at is not None
