from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.models import (
    Dataset,
    Project,
    RunImage,
    RunMetric,
    TrainingRun,
    UserStorage,
)
from app.services.storage import storage_relative_path


pytestmark = pytest.mark.asyncio


async def _persist_run(
    app,
    *,
    owner_id: int,
    state: str = "done",
) -> tuple[int, int, Path, int]:
    storage_dir = app.state.settings.storage_dir
    async with app.state.session_factory() as session:
        project = Project(
            owner_id=owner_id,
            name=f"test-run-delete-project-{uuid4().hex}",
        )
        session.add(project)
        await session.flush()
        dataset = Dataset(
            owner_id=owner_id,
            project_id=project.id,
            name=f"test-run-delete-dataset-{uuid4().hex}",
            status="ready",
            storage_path="datasets/pending",
        )
        session.add(dataset)
        await session.flush()
        run = TrainingRun(
            owner_id=owner_id,
            dataset_id=dataset.id,
            dataset_name=dataset.name,
            weights="yolo26n.pt",
            epochs=1,
            imgsz=640,
            batch=4,
            split_mode="2way",
            ratios={"train": 0.8, "valid": 0.2},
            seed=1,
            state=state,
            started_at=datetime.now(timezone.utc),
            finished_at=(
                datetime.now(timezone.utc)
                if state in {"done", "failed", "canceled"}
                else None
            ),
            out_dir="pending",
        )
        session.add(run)
        await session.flush()

        run_directory = storage_dir / "training-runs" / str(run.id)
        artifacts = run_directory / "artifacts"
        workdir = run_directory / "workdir"
        artifacts.mkdir(parents=True)
        workdir.mkdir()
        (artifacts / "best.pt").write_bytes(b"run-delete-artifact")
        (workdir / "snapshot.txt").write_text("run snapshot\n")
        artifact_bytes = (artifacts / "best.pt").stat().st_size
        run.out_dir = storage_relative_path(storage_dir, run_directory)
        run.artifact_bytes = artifact_bytes

        session.add(
            RunImage(
                run_id=run.id,
                image_id=None,
                split="train",
                stem="sample",
                filename="sample.jpg",
                rel_path="sample.jpg",
            )
        )
        session.add(
            RunMetric(
                run_id=run.id,
                epoch=1,
                box_loss=0.5,
                cls_loss=0.4,
                dfl_loss=0.3,
                map50=0.6,
                map5095=0.45,
                lr={"lr0": 0.01},
            )
        )
        session.add(UserStorage(owner_id=owner_id, bytes_used=artifact_bytes))
        await session.commit()
        return run.id, dataset.id, run_directory, artifact_bytes


async def test_terminal_run_delete_requires_confirmation_then_removes_run_only(
    client,
    app,
) -> None:
    run_id, dataset_id, run_directory, artifact_bytes = await _persist_run(
        app,
        owner_id=1,
    )

    confirmation = await client.delete(f"/api/runs/{run_id}")
    assert confirmation.status_code == 409
    detail = confirmation.json()["detail"]
    assert detail == {
        "code": "run-delete-confirmation-required",
        "requires_confirmation": True,
        "warning": "run 기록과 산출물이 삭제되며 되돌릴 수 없습니다.",
        "run": {"id": run_id, "dataset_name": detail["run"]["dataset_name"]},
    }
    assert run_directory.exists()

    deleted = await client.delete(f"/api/runs/{run_id}?confirm=true")
    assert deleted.status_code == 204
    assert not run_directory.exists()

    async with app.state.session_factory() as session:
        assert await session.get(TrainingRun, run_id) is None
        assert await session.get(Dataset, dataset_id) is not None
        image_count = await session.scalar(
            select(func.count())
            .select_from(RunImage)
            .where(RunImage.run_id == run_id)
        )
        metric_count = await session.scalar(
            select(func.count())
            .select_from(RunMetric)
            .where(RunMetric.run_id == run_id)
        )
        assert image_count == 0
        assert metric_count == 0
        usage = await session.get(UserStorage, 1)
        assert usage is not None
        assert usage.bytes_used == 0
    assert artifact_bytes > 0


@pytest.mark.parametrize("state", ["queued", "running", "canceling"])
async def test_active_run_cannot_be_deleted(
    client,
    app,
    state: str,
) -> None:
    run_id, dataset_id, run_directory, artifact_bytes = await _persist_run(
        app,
        owner_id=1,
        state=state,
    )

    response = await client.delete(f"/api/runs/{run_id}?confirm=true")
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "run-active"
    assert run_directory.exists()

    async with app.state.session_factory() as session:
        assert await session.get(TrainingRun, run_id) is not None
        assert await session.get(Dataset, dataset_id) is not None
        usage = await session.get(UserStorage, 1)
        assert usage is not None
        assert usage.bytes_used == artifact_bytes


async def test_foreign_owner_cannot_delete_run(
    client,
    app,
    auth_headers,
) -> None:
    run_id, _dataset_id, run_directory, _artifact_bytes = await _persist_run(
        app,
        owner_id=101,
    )

    response = await client.delete(
        f"/api/runs/{run_id}?confirm=true",
        headers=auth_headers(202),
    )
    assert response.status_code == 404
    assert run_directory.exists()
