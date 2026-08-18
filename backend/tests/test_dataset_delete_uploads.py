from __future__ import annotations

import asyncio
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import app.db as db_service
import app.routers.datasets as datasets_router
from app.models import (
    Dataset,
    Image,
    Project,
    TrainingRun,
    UploadSession,
    UserStorage,
)
from app.services import cleanup
from app.services.quota import get_bytes_used
from app.services.storage import (
    contained_storage_path,
    finalize_staged_deletion,
    restore_staged_deletion,
    stage_deletions,
    stage_deletions_async as real_stage_deletions_async,
    stage_dataset_deletion as real_stage_dataset_deletion,
    storage_relative_path,
)
from app.services.uploads import upload_directory


pytestmark = pytest.mark.asyncio


async def test_staged_deletions_share_one_request_scope(
    app,
) -> None:
    storage_dir = app.state.settings.storage_dir
    first = contained_storage_path(storage_dir, "datasets/request-scope-first")
    second = contained_storage_path(storage_dir, "uploads/request-scope-second")
    first.mkdir(parents=True)
    second.mkdir(parents=True)
    (first / "first.bin").write_bytes(b"first")
    (second / "second.bin").write_bytes(b"second")

    staged = stage_deletions(storage_dir, [first, second])

    assert len(staged) == 2
    first_stage, second_stage = staged
    assert first_stage is not None
    assert second_stage is not None
    assert first_stage.quarantine == second_stage.quarantine
    assert first_stage.payload != second_stage.payload

    finalize_staged_deletion(first_stage)
    assert not first_stage.payload.exists()
    assert second_stage.payload.exists()
    assert second_stage.quarantine.exists()

    assert restore_staged_deletion(second_stage) is True
    assert (second / "second.bin").read_bytes() == b"second"
    assert not second_stage.quarantine.exists()


async def _project(client: httpx.AsyncClient) -> int:
    response = await client.post(
        "/api/projects",
        json={"name": f"test-delete-uploads-project-{uuid4().hex}"},
    )
    assert response.status_code == 201
    return response.json()["id"]


async def _dataset(client: httpx.AsyncClient, project_id: int) -> int:
    response = await client.post(
        "/api/datasets",
        json={
            "name": f"test-delete-uploads-dataset-{uuid4().hex}",
            "project_id": project_id,
        },
    )
    assert response.status_code == 201
    return response.json()["id"]


async def _upload(client: httpx.AsyncClient, dataset_id: int) -> int:
    response = await client.post(
        f"/api/datasets/{dataset_id}/uploads",
        json={
            "filename": "payload.bin",
            "size": 0,
            "chunk_size": 4,
            "kind": "file",
        },
    )
    assert response.status_code == 201
    return response.json()["upload_id"]


async def test_dataset_delete_reclaims_all_upload_directories_without_quota_change(
    client: httpx.AsyncClient,
    app,
) -> None:
    project_id = await _project(client)
    dataset_id = await _dataset(client, project_id)
    upload_ids = [await _upload(client, dataset_id) for _ in range(2)]
    async with app.state.session_factory() as session:
        session.add(UserStorage(owner_id=1, bytes_used=456))
        await session.commit()

    assert (await client.delete(f"/api/datasets/{dataset_id}")).status_code == 204
    assert all(
        not upload_directory(app.state.settings, upload_id).exists()
        for upload_id in upload_ids
    )
    async with app.state.session_factory() as session:
        assert await get_bytes_used(session, 1) == 456


async def test_project_delete_reclaims_descendant_upload_directories(
    client: httpx.AsyncClient,
    app,
) -> None:
    project_id = await _project(client)
    dataset_ids = [await _dataset(client, project_id) for _ in range(2)]
    upload_ids = [await _upload(client, dataset_id) for dataset_id in dataset_ids]

    response = await client.delete(f"/api/projects/{project_id}?confirm=true")

    assert response.status_code == 204
    assert all(
        not upload_directory(app.state.settings, upload_id).exists()
        for upload_id in upload_ids
    )


async def test_dataset_delete_restores_dataset_and_upload_on_commit_failure(
    client: httpx.AsyncClient,
    app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id = await _project(client)
    dataset_id = await _dataset(client, project_id)
    upload_id = await _upload(client, dataset_id)
    async with app.state.session_factory() as session:
        dataset = await session.get(Dataset, dataset_id)
        assert dataset is not None
        dataset_root = contained_storage_path(
            app.state.settings.storage_dir,
            dataset.storage_path,
        )
    upload_root = upload_directory(app.state.settings, upload_id)
    original_commit = AsyncSession.commit

    async def fail_commit(_session: AsyncSession) -> None:
        raise RuntimeError("forced delete commit failure")

    monkeypatch.setattr(AsyncSession, "commit", fail_commit)
    with pytest.raises(RuntimeError, match="forced delete commit failure"):
        await client.delete(f"/api/datasets/{dataset_id}")
    monkeypatch.setattr(AsyncSession, "commit", original_commit)

    assert dataset_root.is_dir()
    assert upload_root.is_dir()
    async with app.state.session_factory() as session:
        assert await session.get(Dataset, dataset_id) is not None


async def test_project_delete_restores_dataset_and_upload_on_commit_failure(
    client: httpx.AsyncClient,
    app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id = await _project(client)
    dataset_id = await _dataset(client, project_id)
    upload_id = await _upload(client, dataset_id)
    async with app.state.session_factory() as session:
        dataset = await session.get(Dataset, dataset_id)
        assert dataset is not None
        dataset_root = contained_storage_path(
            app.state.settings.storage_dir,
            dataset.storage_path,
        )
    upload_root = upload_directory(app.state.settings, upload_id)
    original_commit = AsyncSession.commit

    async def fail_commit(_session: AsyncSession) -> None:
        raise RuntimeError("forced project delete commit failure")

    monkeypatch.setattr(AsyncSession, "commit", fail_commit)
    with pytest.raises(RuntimeError, match="forced project delete commit failure"):
        await client.delete(f"/api/projects/{project_id}?confirm=true")
    monkeypatch.setattr(AsyncSession, "commit", original_commit)

    assert dataset_root.is_dir()
    assert upload_root.is_dir()
    async with app.state.session_factory() as session:
        assert await session.get(Project, project_id) is not None
        assert await session.get(Dataset, dataset_id) is not None


@pytest.mark.parametrize("delete_project", [False, True])
async def test_delete_cancellation_restores_request_scoped_quarantine(
    client: httpx.AsyncClient,
    app,
    monkeypatch: pytest.MonkeyPatch,
    delete_project: bool,
) -> None:
    project_id = await _project(client)
    dataset_id = await _dataset(client, project_id)
    upload_id = await _upload(client, dataset_id)
    async with app.state.session_factory() as session:
        dataset = await session.get(Dataset, dataset_id)
        assert dataset is not None
        dataset_root = contained_storage_path(
            app.state.settings.storage_dir,
            dataset.storage_path,
        )
    dataset_marker = dataset_root / "dataset-marker.bin"
    upload_root = upload_directory(app.state.settings, upload_id)
    upload_marker = upload_root / "upload-marker.bin"
    dataset_marker.write_bytes(b"dataset")
    upload_marker.write_bytes(b"upload")

    commit_reached = asyncio.Event()
    original_commit = AsyncSession.commit

    async def flush_then_block(session: AsyncSession) -> None:
        await session.flush()
        commit_reached.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(AsyncSession, "commit", flush_then_block)
    url = (
        f"/api/projects/{project_id}?confirm=true"
        if delete_project
        else f"/api/datasets/{dataset_id}"
    )
    task = asyncio.create_task(client.delete(url))
    try:
        await asyncio.wait_for(commit_reached.wait(), timeout=1)
        assert not dataset_root.exists()
        assert not upload_root.exists()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        monkeypatch.setattr(AsyncSession, "commit", original_commit)
        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    assert dataset_marker.read_bytes() == b"dataset"
    assert upload_marker.read_bytes() == b"upload"
    pending_root = app.state.settings.storage_dir / ".delete-pending"
    assert not pending_root.exists() or not any(pending_root.iterdir())
    async with app.state.session_factory() as session:
        assert await session.get(Project, project_id) is not None
        assert await session.get(Dataset, dataset_id) is not None
        assert await session.get(UploadSession, upload_id) is not None


@pytest.mark.parametrize("delete_project", [False, True])
async def test_delete_restores_every_staged_path_when_rollback_fails(
    client: httpx.AsyncClient,
    app,
    monkeypatch: pytest.MonkeyPatch,
    delete_project: bool,
) -> None:
    project_id = await _project(client)
    dataset_id = await _dataset(client, project_id)
    upload_id = await _upload(client, dataset_id)
    storage_dir = app.state.settings.storage_dir
    run_id: int | None = None
    run_marker = None
    async with app.state.session_factory() as session:
        dataset = await session.get(Dataset, dataset_id)
        assert dataset is not None
        dataset_root = contained_storage_path(
            storage_dir,
            dataset.storage_path,
        )
        if delete_project:
            run_root = (
                storage_dir
                / "training-runs"
                / f"test-delete-rollback-run-{uuid4().hex}"
            )
            run_root.mkdir(parents=True)
            run_marker = run_root / "run-marker.bin"
            run_marker.write_bytes(b"run")
            run = TrainingRun(
                owner_id=1,
                dataset_id=dataset_id,
                dataset_name=dataset.name,
                weights="yolo26n.pt",
                epochs=1,
                imgsz=640,
                batch=1,
                split_mode="2way",
                ratios={"train": 0.8, "valid": 0.2},
                seed=1,
                state="done",
                out_dir=storage_relative_path(storage_dir, run_root),
                artifact_bytes=run_marker.stat().st_size,
            )
            session.add(run)
            await session.commit()
            run_id = run.id

    dataset_marker = dataset_root / "dataset-marker.bin"
    upload_root = upload_directory(app.state.settings, upload_id)
    upload_marker = upload_root / "upload-marker.bin"
    dataset_marker.write_bytes(b"dataset")
    upload_marker.write_bytes(b"upload")

    original_commit = AsyncSession.commit
    original_rollback = AsyncSession.rollback
    rollback_calls = 0

    async def flush_then_fail_commit(session: AsyncSession) -> None:
        await session.flush()
        raise RuntimeError("forced delete commit failure")

    async def fail_rollback(_session: AsyncSession) -> None:
        nonlocal rollback_calls
        rollback_calls += 1
        raise RuntimeError("forced delete rollback failure")

    monkeypatch.setattr(AsyncSession, "commit", flush_then_fail_commit)
    monkeypatch.setattr(AsyncSession, "rollback", fail_rollback)
    url = (
        f"/api/projects/{project_id}?confirm=true"
        if delete_project
        else f"/api/datasets/{dataset_id}"
    )
    try:
        with pytest.raises(RuntimeError):
            await client.delete(url)
    finally:
        monkeypatch.setattr(AsyncSession, "commit", original_commit)
        monkeypatch.setattr(AsyncSession, "rollback", original_rollback)

    assert rollback_calls == 1
    assert dataset_marker.read_bytes() == b"dataset"
    assert upload_marker.read_bytes() == b"upload"
    if run_marker is not None:
        assert run_marker.read_bytes() == b"run"
    pending_root = storage_dir / ".delete-pending"
    assert not pending_root.exists() or not any(pending_root.iterdir())
    async with app.state.session_factory() as session:
        assert await session.get(Project, project_id) is not None
        assert await session.get(Dataset, dataset_id) is not None
        assert await session.get(UploadSession, upload_id) is not None
        if run_id is not None:
            assert await session.get(TrainingRun, run_id) is not None


async def test_dataset_delete_returns_retryable_503_when_upload_is_locked(
    client: httpx.AsyncClient,
    app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id = await _project(client)
    dataset_id = await _dataset(client, project_id)
    upload_id = await _upload(client, dataset_id)
    upload_root = upload_directory(app.state.settings, upload_id)
    monkeypatch.setattr(db_service, "SHORT_LOCK_TIMEOUT_MS", 50)

    async with app.state.session_factory() as blocker:
        locked = await blocker.scalar(
            select(UploadSession)
            .where(UploadSession.id == upload_id)
            .with_for_update()
        )
        assert locked is not None
        response = await asyncio.wait_for(
            client.delete(f"/api/datasets/{dataset_id}"),
            timeout=1,
        )
        await blocker.rollback()

    assert response.status_code == 503
    assert response.headers["retry-after"] == "1"
    assert response.json()["detail"]["retryable"] is True
    assert upload_root.is_dir()
    async with app.state.session_factory() as session:
        assert await session.get(Dataset, dataset_id) is not None
        assert await session.get(UploadSession, upload_id) is not None


async def test_project_delete_returns_retryable_503_when_upload_is_locked(
    client: httpx.AsyncClient,
    app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id = await _project(client)
    dataset_id = await _dataset(client, project_id)
    upload_id = await _upload(client, dataset_id)
    upload_root = upload_directory(app.state.settings, upload_id)
    monkeypatch.setattr(db_service, "SHORT_LOCK_TIMEOUT_MS", 50)

    async with app.state.session_factory() as blocker:
        locked = await blocker.scalar(
            select(UploadSession)
            .where(UploadSession.id == upload_id)
            .with_for_update()
        )
        assert locked is not None
        response = await asyncio.wait_for(
            client.delete(f"/api/projects/{project_id}?confirm=true"),
            timeout=1,
        )
        await blocker.rollback()

    assert response.status_code == 503
    assert response.headers["retry-after"] == "1"
    assert response.json()["detail"]["retryable"] is True
    assert upload_root.is_dir()
    async with app.state.session_factory() as session:
        assert await session.get(Project, project_id) is not None
        assert await session.get(Dataset, dataset_id) is not None
        assert await session.get(UploadSession, upload_id) is not None


async def test_dataset_delete_reaper_tick_cannot_remove_inflight_quarantine(
    client: httpx.AsyncClient,
    app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id = await _project(client)
    dataset_id = await _dataset(client, project_id)
    upload_id = await _upload(client, dataset_id)
    storage_dir = app.state.settings.storage_dir
    original_bytes = b"original-image"
    thumb_bytes = b"thumbnail"

    async with app.state.session_factory() as session:
        dataset = await session.get(Dataset, dataset_id)
        assert dataset is not None
        dataset.status = "ready"
        dataset.image_count = 1
        dataset_root = contained_storage_path(
            storage_dir,
            dataset.storage_path,
        )
        original_path = dataset_root / "images" / "sample.jpg"
        thumb_path = dataset_root / "thumbs" / "sample.jpg"
        original_path.parent.mkdir(parents=True, exist_ok=True)
        thumb_path.parent.mkdir(parents=True, exist_ok=True)
        original_path.write_bytes(original_bytes)
        thumb_path.write_bytes(thumb_bytes)
        upload_root = upload_directory(app.state.settings, upload_id)
        upload_marker = upload_root / "upload-marker.bin"
        upload_marker.write_bytes(b"upload")
        image = Image(
            dataset_id=dataset_id,
            stem="sample",
            filename="sample.jpg",
            rel_path="sample.jpg",
            split="train",
            width=1,
            height=1,
            file_path=storage_relative_path(storage_dir, original_path),
            display_path=None,
            thumb_path=storage_relative_path(storage_dir, thumb_path),
            original_bytes=len(original_bytes),
            display_bytes=0,
            thumb_bytes=len(thumb_bytes),
            box_count=0,
            has_label_source=False,
        )
        session.add(image)
        usage = await session.get(UserStorage, 1)
        accounted_bytes = len(original_bytes) + len(thumb_bytes)
        if usage is None:
            session.add(UserStorage(owner_id=1, bytes_used=accounted_bytes))
        else:
            usage.bytes_used = accounted_bytes
        await session.commit()
        image_id = image.id

    reaper_observations: list[tuple[int, bool]] = []

    async def stage_then_force_reaper(root, stored_paths):
        staged = await real_stage_deletions_async(root, stored_paths)
        scope = next(item.quarantine for item in staged if item is not None)
        result = cleanup.finalize_pending_deletions(root)
        reaper_observations.append(
            (result.finalized_pending, scope.exists())
        )
        return staged

    monkeypatch.setattr(
        datasets_router,
        "stage_deletions_async",
        stage_then_force_reaper,
    )
    monkeypatch.setattr(db_service, "SHORT_LOCK_TIMEOUT_MS", 50)

    async with app.state.session_factory() as blocker:
        locked = await blocker.scalar(
            select(Image).where(Image.id == image_id).with_for_update()
        )
        assert locked is not None
        response = await asyncio.wait_for(
            client.delete(f"/api/datasets/{dataset_id}"),
            timeout=1,
        )
        await blocker.rollback()

    assert response.status_code == 503
    assert response.headers["retry-after"] == "1"
    assert reaper_observations == [(0, True)]
    assert original_path.read_bytes() == original_bytes
    assert thumb_path.read_bytes() == thumb_bytes
    assert upload_marker.read_bytes() == b"upload"
    async with app.state.session_factory() as session:
        assert await session.get(Dataset, dataset_id) is not None
        assert await session.get(Image, image_id) is not None
        assert await get_bytes_used(session, 1) == accounted_bytes

    monkeypatch.setattr(
        datasets_router,
        "stage_deletions_async",
        real_stage_deletions_async,
    )
    retry = await client.delete(f"/api/datasets/{dataset_id}")
    assert retry.status_code == 204
    assert not dataset_root.exists()
    async with app.state.session_factory() as session:
        assert await session.get(Dataset, dataset_id) is None
        assert await get_bytes_used(session, 1) == 0
