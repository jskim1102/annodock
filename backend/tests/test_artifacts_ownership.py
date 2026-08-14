from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from app.models import Dataset, Project, RunMetric, TrainingRun
from app.services.storage import storage_relative_path


pytestmark = pytest.mark.asyncio


async def _persist_run(
    app,
    *,
    owner_id: int,
    state: str = "done",
) -> tuple[int, int, Path]:
    async with app.state.session_factory() as session:
        project = Project(
            owner_id=owner_id,
            name=f"test-artifact-project-{uuid4().hex}",
        )
        session.add(project)
        await session.flush()
        dataset = Dataset(
            owner_id=owner_id,
            project_id=project.id,
            name=f"test-artifact-dataset-{uuid4().hex}",
            status="ready",
            storage_path="datasets/pending",
        )
        session.add(dataset)
        await session.flush()

        run_directory = (
            app.state.settings.storage_dir / "training-runs" / uuid4().hex
        )
        artifacts = run_directory / "artifacts"
        artifacts.mkdir(parents=True)
        (artifacts / "best.pt").write_bytes(b"owner-scoped-best")
        (artifacts / "last.pt").write_bytes(b"owner-scoped-last")
        (artifacts / "results.csv").write_text("epoch,loss\n1,0.5\n")
        (artifacts / "log").write_text("private training log\n")

        run = TrainingRun(
            owner_id=owner_id,
            dataset_id=dataset.id,
            dataset_name=f"test-artifact-run-{uuid4().hex}",
            weights="yolo26n.pt",
            epochs=1,
            imgsz=640,
            batch=4,
            split_mode="2way",
            ratios={"train": 0.8, "valid": 0.2},
            seed=1,
            state=state,
            started_at=datetime.now(timezone.utc),
            finished_at=(datetime.now(timezone.utc) if state == "done" else None),
            out_dir=storage_relative_path(
                app.state.settings.storage_dir,
                run_directory,
            ),
        )
        session.add(run)
        await session.flush()
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
        await session.commit()
        return run.id, dataset.id, run_directory


async def test_run_surfaces_and_artifacts_are_hidden_from_other_owner(
    client,
    app,
    auth_headers: Callable[[int], dict[str, str]],
) -> None:
    owner_id = 101
    other_id = 202
    run_id, dataset_id, _run_directory = await _persist_run(
        app,
        owner_id=owner_id,
    )
    owner = auth_headers(owner_id)
    other = auth_headers(other_id)

    owner_listing = await client.get("/api/runs", headers=owner)
    other_listing = await client.get("/api/runs", headers=other)
    assert owner_listing.status_code == 200
    assert run_id in {item["id"] for item in owner_listing.json()["items"]}
    assert other_listing.status_code == 200
    assert run_id not in {item["id"] for item in other_listing.json()["items"]}

    foreign_paths = (
        f"/api/runs/{run_id}",
        f"/api/runs/{run_id}/metrics",
        f"/api/runs/{run_id}/log",
        f"/api/runs/{run_id}/inference-images",
        f"/api/runs/{run_id}/artifacts/best.pt",
    )
    for path in foreign_paths:
        response = await client.get(path, headers=other)
        assert response.status_code == 404, path
    assert (
        await client.delete(
            f"/api/runs/{run_id}/artifacts",
            headers=other,
        )
    ).status_code == 404

    foreign_dataset_filter = await client.get(
        f"/api/runs?dataset_id={dataset_id}",
        headers=other,
    )
    assert foreign_dataset_filter.status_code == 404

    artifact = await client.get(
        f"/api/runs/{run_id}/artifacts/best.pt",
        headers=owner,
    )
    assert artifact.status_code == 200
    assert artifact.content == b"owner-scoped-best"


async def test_foreign_cancel_is_404_and_does_not_mutate_run(
    client,
    app,
    auth_headers: Callable[[int], dict[str, str]],
) -> None:
    owner_id = 303
    other_id = 404
    run_id, _dataset_id, _run_directory = await _persist_run(
        app,
        owner_id=owner_id,
        state="running",
    )

    denied = await client.post(
        f"/api/runs/{run_id}/cancel",
        headers=auth_headers(other_id),
    )
    assert denied.status_code == 404
    async with app.state.session_factory() as session:
        persisted = await session.get(TrainingRun, run_id)
        assert persisted is not None
        assert persisted.state == "running"

    canceled = await client.post(
        f"/api/runs/{run_id}/cancel",
        headers=auth_headers(owner_id),
    )
    assert canceled.status_code == 202
    assert canceled.json() == {"run_id": run_id, "state": "canceled"}
