from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.models import Dataset, RunImage, TrainingRun
from app.services.storage import contained_storage_path
from app.worker import train_worker


pytestmark = pytest.mark.asyncio


def _run_name(suffix: str) -> str:
    return f"test-cleanup-{suffix}-{uuid4().hex}"


def _run(
    out_dir: Path,
    *,
    state: str,
    dataset_id: int | None = None,
) -> TrainingRun:
    return TrainingRun(
        owner_id=1,
        dataset_id=dataset_id,
        dataset_name=_run_name("run"),
        weights="yolo26n.pt",
        epochs=2,
        imgsz=640,
        batch=4,
        split_mode="2way",
        ratios={"train": 0.8, "valid": 0.2},
        seed=11,
        state=state,
        started_at=datetime.now(timezone.utc),
        finished_at=(
            datetime.now(timezone.utc)
            if state in {"done", "failed", "canceled"}
            else None
        ),
        out_dir=str(out_dir),
    )


async def test_done_transition_removes_only_workdir_and_preserves_run_images(
    app,
) -> None:
    storage_dir = app.state.settings.storage_dir
    async with app.state.session_factory() as session:
        run = _run(storage_dir / "training-runs" / "pending", state="running")
        session.add(run)
        await session.flush()
        out_dir = storage_dir / "training-runs" / str(run.id)
        run.out_dir = str(out_dir)
        session.add(
            RunImage(
                run_id=run.id,
                image_id=None,
                split="valid",
                stem="sample",
                filename="sample.jpg",
                rel_path="incoming/sample.jpg",
            )
        )
        await session.commit()
        run_id = run.id

    source = out_dir / "workdir" / "train"
    weights = source / "weights"
    artifacts = out_dir / "artifacts"
    weights.mkdir(parents=True)
    artifacts.mkdir(parents=True)
    best = weights / "best.pt"
    last = weights / "last.pt"
    csv = source / "results.csv"
    best.write_bytes(b"best")
    last.write_bytes(b"last")
    csv.write_text("epoch,metric\n1,0.5\n", encoding="utf-8")

    updated = train_worker.complete_run(
        run_id,
        1,
        app.state.settings.database_url.replace("+asyncpg", "", 1),
        out_dir,
        SimpleNamespace(best=best, last=last, csv=csv),
        storage_dir=storage_dir,
    )

    assert updated is True
    assert not (out_dir / "workdir").exists()
    assert sorted(path.name for path in artifacts.iterdir()) == [
        "best.pt",
        "last.pt",
        "results.csv",
    ]
    async with app.state.session_factory() as session:
        persisted = await session.get(TrainingRun, run_id)
        image_count = await session.scalar(
            select(func.count(RunImage.id)).where(RunImage.run_id == run_id)
        )
        assert persisted is not None
        assert persisted.state == "done"
        assert persisted.finished_at is not None
        assert image_count == 1


async def test_artifact_delete_removes_directory_but_preserves_run_row(
    client,
    app,
) -> None:
    storage_dir = app.state.settings.storage_dir
    async with app.state.session_factory() as session:
        run = _run(storage_dir / "training-runs" / "pending", state="failed")
        session.add(run)
        await session.flush()
        out_dir = storage_dir / "training-runs" / str(run.id)
        run.out_dir = str(out_dir)
        await session.commit()
        run_id = run.id

    artifacts = out_dir / "artifacts"
    workdir = out_dir / "workdir"
    artifacts.mkdir(parents=True)
    workdir.mkdir()
    (artifacts / "log").write_text("failure", encoding="utf-8")
    (artifacts / "best.pt").write_bytes(b"best")

    response = await client.delete(f"/api/runs/{run_id}/artifacts")

    assert response.status_code == 204
    assert not artifacts.exists()
    assert workdir.is_dir()
    async with app.state.session_factory() as session:
        persisted = await session.get(TrainingRun, run_id)
        assert persisted is not None
        assert persisted.state == "failed"
        assert persisted.artifacts_deleted_at is not None

    assert (await client.get(f"/api/runs/{run_id}")).status_code == 200
    assert (await client.get(f"/api/runs/{run_id}/log")).text == ""
    assert (
        await client.get(f"/api/runs/{run_id}/artifacts/best.pt")
    ).status_code == 410
    assert (await client.delete(f"/api/runs/{run_id}/artifacts")).status_code == 204


async def test_dataset_delete_restricts_active_run_but_allows_inactive_run(
    client,
    app,
) -> None:
    created = await client.post(
        "/api/datasets",
        json={"name": _run_name("dataset")},
    )
    assert created.status_code == 201
    dataset_id = created.json()["id"]

    async with app.state.session_factory() as session:
        dataset = await session.get(Dataset, dataset_id)
        assert dataset is not None
        dataset_path = contained_storage_path(
            app.state.settings.storage_dir,
            dataset.storage_path,
        )
        run = _run(
            app.state.settings.storage_dir / "training-runs" / "active",
            state="running",
            dataset_id=dataset_id,
        )
        session.add(run)
        await session.commit()
        run_id = run.id

    restricted = await client.delete(f"/api/datasets/{dataset_id}")

    assert restricted.status_code == 409
    assert "학습" in restricted.json()["detail"]
    assert dataset_path.is_dir()

    async with app.state.session_factory() as session:
        persisted = await session.get(TrainingRun, run_id)
        assert persisted is not None
        persisted.state = "done"
        persisted.finished_at = datetime.now(timezone.utc)
        await session.commit()

    deleted = await client.delete(f"/api/datasets/{dataset_id}")

    assert deleted.status_code == 204
    assert not dataset_path.exists()
    async with app.state.session_factory() as session:
        persisted = await session.get(TrainingRun, run_id)
        assert persisted is not None
        assert persisted.dataset_id is None
