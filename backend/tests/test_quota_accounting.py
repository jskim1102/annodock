from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from PIL import Image as PillowImage
from sqlalchemy import delete, select

from app.models import (
    Dataset,
    Image,
    TrainingRun,
    UploadJob,
    UserStorage,
)
from app.services.collect import CollectedFile
from app.services.ingest import ingest_collected
from app.services.quota import get_bytes_used
from app.services.storage import (
    contained_storage_path,
    restore_staged_deletion,
    stage_dataset_deletion,
    storage_relative_path,
)
from app.worker import train_worker


pytestmark = pytest.mark.asyncio


async def _reset_usage(app, owner_id: int = 1) -> None:
    async with app.state.session_factory() as session:
        await session.execute(
            delete(UserStorage).where(UserStorage.owner_id == owner_id)
        )
        await session.commit()


async def _dataset_and_job(client, app) -> tuple[int, int]:
    response = await client.post(
        "/api/datasets",
        json={"name": f"test-quota-accounting-{uuid4().hex}"},
    )
    assert response.status_code == 201
    dataset_id = response.json()["id"]
    async with app.state.session_factory() as session:
        job = UploadJob(
            dataset_id=dataset_id,
            kind="file",
            state="queued",
            phase="uploading",
            total=0,
            processed=0,
            failed=0,
        )
        session.add(job)
        await session.commit()
        return dataset_id, job.id


async def test_ingest_and_dataset_delete_keep_exact_counter(
    client,
    app,
    tmp_path: Path,
) -> None:
    await _reset_usage(app)
    dataset_id, job_id = await _dataset_and_job(client, app)
    source = tmp_path / "quota-source.jpg"
    PillowImage.new("RGB", (96, 48), (20, 80, 140)).save(source, "JPEG")

    await ingest_collected(
        app.state.settings,
        app.state.session_factory,
        job_id,
        [
            CollectedFile(
                rel_path="images/train/quota-source.jpg",
                abs_path=source,
                kind="image",
                split="train",
            )
        ],
    )

    async with app.state.session_factory() as session:
        dataset = await session.get(Dataset, dataset_id)
        image = await session.scalar(
            select(Image).where(Image.dataset_id == dataset_id)
        )
        assert dataset is not None
        assert image is not None
        physical_bytes = sum(
            contained_storage_path(app.state.settings.storage_dir, path).stat().st_size
            for path in (image.file_path, image.thumb_path)
        )
        assert image.display_path is None
        assert image.original_bytes + image.display_bytes + image.thumb_bytes == physical_bytes
        assert await get_bytes_used(session, 1) == physical_bytes

        staged = stage_dataset_deletion(
            app.state.settings.storage_dir,
            dataset.storage_path,
        )
        assert staged is not None
        assert staged.quarantine.parent.name == ".delete-pending"
        assert await get_bytes_used(session, 1) == physical_bytes
        restore_staged_deletion(staged)

    deleted = await client.delete(f"/api/datasets/{dataset_id}")
    assert deleted.status_code == 204
    async with app.state.session_factory() as session:
        assert await get_bytes_used(session, 1) == 0


async def test_completed_run_counts_artifacts_but_not_original_hardlinks(
    client,
    app,
) -> None:
    await _reset_usage(app)
    dataset_id, _ = await _dataset_and_job(client, app)
    storage_dir = app.state.settings.storage_dir

    async with app.state.session_factory() as session:
        dataset = await session.get(Dataset, dataset_id)
        assert dataset is not None
        dataset_root = contained_storage_path(storage_dir, dataset.storage_path)
        original = dataset_root / "original" / "sample.jpg"
        original.parent.mkdir(parents=True, exist_ok=True)
        original.write_bytes(b"original-image-bytes")
        image = Image(
            dataset_id=dataset_id,
            stem="sample",
            filename="sample.jpg",
            rel_path="images/sample.jpg",
            split="train",
            width=10,
            height=10,
            file_path=storage_relative_path(storage_dir, original),
            display_path=None,
            thumb_path=storage_relative_path(storage_dir, original),
            original_bytes=original.stat().st_size,
            display_bytes=0,
            thumb_bytes=0,
            box_count=0,
            is_modified=False,
        )
        session.add(image)
        run = TrainingRun(
            owner_id=1,
            dataset_id=dataset_id,
            dataset_name=dataset.name,
            weights="yolo26n.pt",
            epochs=1,
            imgsz=64,
            batch=1,
            split_mode="2way",
            ratios={"train": 0.8, "valid": 0.2},
            seed=7,
            state="running",
            out_dir="pending",
        )
        session.add(run)
        await session.flush()
        out_dir = storage_dir / "training-runs" / str(run.id)
        run.out_dir = storage_relative_path(storage_dir, out_dir)
        session.add(UserStorage(owner_id=1, bytes_used=original.stat().st_size))
        await session.commit()
        run_id = run.id
        original_bytes = original.stat().st_size

    weights_dir = out_dir / "workdir" / "train" / "weights"
    images_dir = out_dir / "workdir" / "images" / "train"
    artifacts_dir = out_dir / "artifacts"
    weights_dir.mkdir(parents=True)
    images_dir.mkdir(parents=True)
    artifacts_dir.mkdir(parents=True)
    os.link(original, images_dir / "sample.jpg")
    best = weights_dir / "best.pt"
    last = weights_dir / "last.pt"
    results = out_dir / "workdir" / "train" / "results.csv"
    best.write_bytes(b"best-weights")
    last.write_bytes(b"last-weights")
    results.write_bytes(b"epoch,metric\n1,0.5\n")

    assert train_worker.complete_run(
        run_id,
        1,
        app.state.settings.database_url.replace("+asyncpg", "", 1),
        out_dir,
        SimpleNamespace(best=best, last=last, csv=results),
        storage_dir=storage_dir,
    )

    artifact_bytes = sum(path.stat().st_size for path in artifacts_dir.iterdir())
    async with app.state.session_factory() as session:
        assert await get_bytes_used(session, 1) == original_bytes + artifact_bytes

    deleted = await client.delete(f"/api/runs/{run_id}/artifacts")
    assert deleted.status_code == 204
    async with app.state.session_factory() as session:
        assert await get_bytes_used(session, 1) == original_bytes


async def test_canceled_run_collects_and_accounts_partial_artifacts(
    client,
    app,
) -> None:
    await _reset_usage(app)
    storage_dir = app.state.settings.storage_dir
    async with app.state.session_factory() as session:
        run = TrainingRun(
            owner_id=1,
            dataset_id=None,
            dataset_name=f"test-quota-cancel-{uuid4().hex}",
            weights="yolo26n.pt",
            epochs=2,
            imgsz=64,
            batch=1,
            split_mode="2way",
            ratios={"train": 0.8, "valid": 0.2},
            seed=3,
            state="running",
            pid=None,
            started_at=datetime.now(timezone.utc),
            out_dir="pending",
        )
        session.add(run)
        await session.flush()
        out_dir = storage_dir / "training-runs" / str(run.id)
        run.out_dir = storage_relative_path(storage_dir, out_dir)
        await session.commit()
        run_id = run.id

    artifacts = out_dir / "artifacts"
    weights = out_dir / "workdir" / "train" / "weights"
    artifacts.mkdir(parents=True)
    weights.mkdir(parents=True)
    (artifacts / "log").write_bytes(b"partial-log")
    (weights / "last.pt").write_bytes(b"partial-last-weights")

    canceled = await client.post(f"/api/runs/{run_id}/cancel")
    assert canceled.status_code == 202
    expected_bytes = sum(path.stat().st_size for path in artifacts.iterdir())
    async with app.state.session_factory() as session:
        run = await session.get(TrainingRun, run_id)
        assert run is not None
        assert run.state == "canceled"
        assert run.artifact_bytes == expected_bytes
        assert await get_bytes_used(session, 1) == expected_bytes

    assert not (weights / "last.pt").exists()
    assert (artifacts / "last.pt").is_file()
    assert (await client.delete(f"/api/runs/{run_id}/artifacts")).status_code == 204
    async with app.state.session_factory() as session:
        assert await get_bytes_used(session, 1) == 0
