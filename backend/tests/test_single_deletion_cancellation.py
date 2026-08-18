from __future__ import annotations

import asyncio
import threading
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from app.models import Dataset, Project, TrainingRun, UploadSession
from app.services import storage as storage_service
from app.services.storage import storage_relative_path
from app.services.uploads import upload_directory


pytestmark = pytest.mark.asyncio


async def _persist_terminal_run(app) -> tuple[int, Path]:
    storage_dir = app.state.settings.storage_dir
    async with app.state.session_factory() as session:
        project = Project(
            owner_id=1,
            name=f"test-single-delete-project-{uuid4().hex}",
        )
        session.add(project)
        await session.flush()
        dataset = Dataset(
            owner_id=1,
            project_id=project.id,
            name=f"test-single-delete-dataset-{uuid4().hex}",
            status="ready",
            storage_path="datasets/pending",
        )
        session.add(dataset)
        await session.flush()
        run = TrainingRun(
            owner_id=1,
            dataset_id=dataset.id,
            dataset_name=dataset.name,
            weights="yolo26n.pt",
            epochs=1,
            imgsz=640,
            batch=4,
            split_mode="2way",
            ratios={"train": 0.8, "valid": 0.2},
            seed=1,
            state="done",
            started_at=datetime.now(timezone.utc),
            finished_at=datetime.now(timezone.utc),
            out_dir="pending",
        )
        session.add(run)
        await session.flush()

        run_root = storage_dir / "training-runs" / str(run.id)
        artifacts = run_root / "artifacts"
        workdir = run_root / "workdir"
        artifacts.mkdir(parents=True)
        workdir.mkdir()
        (artifacts / "best.pt").write_bytes(b"best")
        (workdir / "snapshot.txt").write_bytes(b"snapshot")
        run.out_dir = storage_relative_path(storage_dir, run_root)
        run.artifact_bytes = 4
        await session.commit()
        return run.id, run_root


def _block_stage_after_move(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[threading.Event, threading.Event]:
    moved = threading.Event()
    release = threading.Event()
    original_stage = storage_service.stage_deletions

    def blocking_stage(root, stored_paths):
        staged = original_stage(root, stored_paths)
        moved.set()
        release.wait()
        return staged

    monkeypatch.setattr(storage_service, "stage_deletions", blocking_stage)
    return moved, release


@pytest.mark.parametrize("artifacts_only", [False, True])
async def test_run_delete_cancellation_during_staging_restores_files(
    client: httpx.AsyncClient,
    app,
    monkeypatch: pytest.MonkeyPatch,
    artifacts_only: bool,
) -> None:
    run_id, run_root = await _persist_terminal_run(app)
    target = run_root / "artifacts" if artifacts_only else run_root
    marker = target / ("best.pt" if artifacts_only else "workdir/snapshot.txt")
    expected = marker.read_bytes()
    moved, release = _block_stage_after_move(monkeypatch)
    url = (
        f"/api/runs/{run_id}/artifacts"
        if artifacts_only
        else f"/api/runs/{run_id}?confirm=true"
    )
    request = asyncio.create_task(client.delete(url))

    try:
        assert await asyncio.wait_for(
            asyncio.to_thread(moved.wait),
            timeout=1,
        )
        assert not target.exists()
        request.cancel()
        await asyncio.sleep(0)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await request
    finally:
        release.set()
        if not request.done():
            request.cancel()

    assert marker.read_bytes() == expected
    pending_root = app.state.settings.storage_dir / ".delete-pending"
    assert not pending_root.exists() or not any(pending_root.iterdir())
    async with app.state.session_factory() as session:
        run = await session.get(TrainingRun, run_id)
        assert run is not None
        assert run.artifacts_deleted_at is None


async def test_upload_abort_cancellation_during_staging_restores_files(
    client: httpx.AsyncClient,
    app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dataset = await client.post(
        "/api/datasets",
        json={"name": f"test-single-delete-upload-{uuid4().hex}"},
    )
    assert dataset.status_code == 201
    created = await client.post(
        f"/api/datasets/{dataset.json()['id']}/uploads",
        json={
            "filename": "cancel.bin",
            "size": 4,
            "chunk_size": 4,
            "kind": "file",
        },
    )
    assert created.status_code == 201
    upload_id = created.json()["upload_id"]
    upload_root = upload_directory(app.state.settings, upload_id)
    marker = upload_root / "upload-marker.bin"
    marker.write_bytes(b"upload")
    moved, release = _block_stage_after_move(monkeypatch)
    request = asyncio.create_task(client.delete(f"/api/uploads/{upload_id}"))

    try:
        assert await asyncio.wait_for(
            asyncio.to_thread(moved.wait),
            timeout=1,
        )
        assert not upload_root.exists()
        request.cancel()
        await asyncio.sleep(0)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await request
    finally:
        release.set()
        if not request.done():
            request.cancel()

    assert marker.read_bytes() == b"upload"
    pending_root = app.state.settings.storage_dir / ".delete-pending"
    assert not pending_root.exists() or not any(pending_root.iterdir())
    async with app.state.session_factory() as session:
        upload = await session.get(UploadSession, upload_id)
        assert upload is not None
        assert upload.state == "open"
