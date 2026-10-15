from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Dataset, RunImage, TrainingRun, UploadBatch, UploadSession
from app.services.cleanup import sweep_upload_storage
from app.services.storage import contained_storage_path
from app.services.uploads import upload_directory
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
        workspace_id=1,
        created_by=1,
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


async def _cleanup_dataset(client: httpx.AsyncClient) -> int:
    created = await client.post(
        "/api/datasets",
        json={"name": _run_name("upload-batch")},
    )
    assert created.status_code == 201
    return created.json()["id"]


async def _manifest_uploads(
    client: httpx.AsyncClient,
    dataset_id: int,
    filenames: tuple[str, ...] = ("images/a.jpg",),
) -> tuple[UUID, list[int]]:
    batch_id = uuid4()
    started = await client.put(
        f"/api/datasets/{dataset_id}/upload-batches/{batch_id}",
        json={
            "total_size": 0,
            "largest_file_size": 0,
            "file_count": len(filenames),
            "expected_extracted_size": 0,
        },
    )
    assert started.status_code == 200
    created = await client.post(
        f"/api/datasets/{dataset_id}/uploads/batch",
        json={
            "batch_id": str(batch_id),
            "files": [
                {"filename": name, "size": 0, "chunk_size": 1, "kind": "file"}
                for name in filenames
            ],
        },
    )
    assert created.status_code == 201
    return batch_id, [item["upload_id"] for item in created.json()["uploads"]]


def _expire_upload_directory(path: Path, now: datetime) -> None:
    timestamp = (now - timedelta(hours=25)).timestamp()
    for candidate in [path, *path.rglob("*")]:
        os.utime(candidate, (timestamp, timestamp), follow_symlinks=False)


async def test_upload_cleanup_aborts_only_batches_with_expired_sessions(
    client: httpx.AsyncClient,
    app,
) -> None:
    dataset_id = await _cleanup_dataset(client)
    stale_batch, stale_ids = await _manifest_uploads(
        client,
        dataset_id,
        ("images/a.jpg", "images/b.jpg"),
    )
    fresh_batch, fresh_ids = await _manifest_uploads(client, dataset_id)
    unrelated_dataset_id = await _cleanup_dataset(client)
    unrelated_batch, unrelated_ids = await _manifest_uploads(
        client,
        unrelated_dataset_id,
    )
    empty_batch = uuid4()
    assert (
        await client.put(
            f"/api/datasets/{dataset_id}/upload-batches/{empty_batch}",
            json={
                "total_size": 0,
                "largest_file_size": 0,
                "file_count": 1,
                "expected_extracted_size": 0,
            },
        )
    ).status_code == 200
    now = datetime.now(timezone.utc)
    for upload_id in stale_ids:
        _expire_upload_directory(upload_directory(app.state.settings, upload_id), now)

    result = await sweep_upload_storage(
        app.state.session_factory,
        storage_dir=app.state.settings.storage_dir,
        now=now,
    )

    assert result.expired_sessions == 2
    assert result.reclaimed_directories == 2
    async with app.state.session_factory() as session:
        batch_states = dict(
            (
                await session.execute(
                    select(UploadBatch.id, UploadBatch.state).where(
                        UploadBatch.id.in_(
                            [stale_batch, fresh_batch, unrelated_batch, empty_batch]
                        )
                    )
                )
            ).all()
        )
        assert batch_states == {
            stale_batch: "aborted",
            fresh_batch: "open",
            unrelated_batch: "open",
            empty_batch: "open",
        }
        for upload_id in [*stale_ids, *fresh_ids, *unrelated_ids]:
            upload = await session.get(UploadSession, upload_id)
            assert upload is not None
            assert upload.state == ("aborted" if upload_id in stale_ids else "open")
            assert upload_directory(app.state.settings, upload_id).is_dir() == (
                upload_id not in stale_ids
            )


@pytest.mark.parametrize("batch_state", ["sealed", "aborted"])
async def test_upload_cleanup_preserves_nonopen_batch_state(
    client: httpx.AsyncClient,
    app,
    batch_state: str,
) -> None:
    dataset_id = await _cleanup_dataset(client)
    batch_id, upload_ids = await _manifest_uploads(client, dataset_id)
    async with app.state.session_factory() as session:
        batch = await session.get(UploadBatch, batch_id)
        assert batch is not None
        batch.state = batch_state
        await session.commit()
    now = datetime.now(timezone.utc)
    directory = upload_directory(app.state.settings, upload_ids[0])
    _expire_upload_directory(directory, now)

    result = await sweep_upload_storage(
        app.state.session_factory,
        storage_dir=app.state.settings.storage_dir,
        now=now,
    )

    assert result.expired_sessions == 1
    assert not directory.exists()
    async with app.state.session_factory() as session:
        batch = await session.get(UploadBatch, batch_id)
        upload = await session.get(UploadSession, upload_ids[0])
        assert batch is not None and batch.state == batch_state
        assert upload is not None and upload.state == "aborted"


async def test_upload_cleanup_rolls_back_batch_session_and_files_on_commit_failure(
    client: httpx.AsyncClient,
    app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dataset_id = await _cleanup_dataset(client)
    batch_id, upload_ids = await _manifest_uploads(client, dataset_id)
    upload_id = upload_ids[0]
    directory = upload_directory(app.state.settings, upload_id)
    chunk = directory / "unfinished.part"
    chunk.write_bytes(b"partial-upload-content")
    now = datetime.now(timezone.utc)
    _expire_upload_directory(directory, now)

    async def fail_commit(session: AsyncSession) -> None:
        # Force both writes to the database, then observe them from another
        # transaction to catch an accidental earlier commit of either row.
        await session.flush()
        assert await session.scalar(
            select(UploadBatch.state).where(UploadBatch.id == batch_id)
        ) == "aborted"
        assert await session.scalar(
            select(UploadSession.state).where(UploadSession.id == upload_id)
        ) == "aborted"
        async with app.state.session_factory() as observer:
            assert await observer.scalar(
                select(UploadBatch.state).where(UploadBatch.id == batch_id)
            ) == "open"
            assert await observer.scalar(
                select(UploadSession.state).where(UploadSession.id == upload_id)
            ) == "open"
        assert not directory.exists()
        raise RuntimeError("forced manifest cleanup commit failure")

    with monkeypatch.context() as patch:
        patch.setattr(AsyncSession, "commit", fail_commit)
        with pytest.raises(RuntimeError, match="forced manifest cleanup commit failure"):
            await sweep_upload_storage(
                app.state.session_factory,
                storage_dir=app.state.settings.storage_dir,
                now=now,
            )

    assert chunk.read_bytes() == b"partial-upload-content"
    async with app.state.session_factory() as session:
        batch = await session.get(UploadBatch, batch_id)
        upload = await session.get(UploadSession, upload_id)
        assert batch is not None and batch.state == "open"
        assert upload is not None and upload.state == "open"
