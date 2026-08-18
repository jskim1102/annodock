from __future__ import annotations

import asyncio
import io
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from asyncpg.exceptions import DeadlockDetectedError
from fastapi import HTTPException
from PIL import Image as PillowImage
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from app import db as db_service
from app.routers import datasets as datasets_router
from app.routers import uploads as uploads_router
from app.services import ingest as ingest_service
from app.services import cleanup as cleanup_service
from app.models import Dataset, Image, UploadJob, UploadSession, UserStorage
from app.services.collect import CollectedFile
from app.services.cleanup import sweep_upload_storage
from app.services.ingest import ingest_collected, run_upload_batch_job
from app.services.quota import get_bytes_used
from app.services.storage import contained_storage_path
from app.services.uploads import abort_upload, store_chunk_stream, upload_directory


pytestmark = pytest.mark.asyncio


async def _dataset(client: httpx.AsyncClient) -> int:
    response = await client.post(
        "/api/datasets",
        json={"name": f"test-upload-gc-{uuid4().hex}"},
    )
    assert response.status_code == 201
    return response.json()["id"]


async def _upload(client: httpx.AsyncClient, dataset_id: int) -> int:
    response = await client.post(
        f"/api/datasets/{dataset_id}/uploads",
        json={
            "filename": "empty.zip",
            "size": 0,
            "chunk_size": 4,
            "kind": "zip",
        },
    )
    assert response.status_code == 201
    return response.json()["upload_id"]


def _jpeg_bytes() -> bytes:
    output = io.BytesIO()
    PillowImage.new("RGB", (32, 24), (40, 80, 120)).save(output, "JPEG")
    return output.getvalue()


async def _completed_image_upload(
    client: httpx.AsyncClient,
    dataset_id: int,
) -> tuple[int, int]:
    content = _jpeg_bytes()
    created = await client.post(
        f"/api/datasets/{dataset_id}/uploads",
        json={
            "filename": "images/train/source.jpg",
            "size": len(content),
            "chunk_size": len(content),
            "kind": "file",
        },
    )
    assert created.status_code == 201
    upload_id = created.json()["upload_id"]
    sent = await client.put(
        f"/api/uploads/{upload_id}/chunks/0",
        content=content,
    )
    assert sent.status_code == 204
    completed = await client.post(f"/api/uploads/{upload_id}/complete")
    assert completed.status_code == 202
    return upload_id, completed.json()["job_id"]


class _LockUnavailable(RuntimeError):
    sqlstate = "55P03"


def _lock_unavailable_error() -> OperationalError:
    return OperationalError("SELECT 1", {}, _LockUnavailable())


def _set_tree_mtime(path: Path, value: datetime) -> None:
    timestamp = value.timestamp()
    for candidate in sorted(path.rglob("*"), reverse=True):
        os.utime(candidate, (timestamp, timestamp), follow_symlinks=False)
    os.utime(path, (timestamp, timestamp), follow_symlinks=False)


def _caused_by(error: BaseException, error_type: type[BaseException]) -> bool:
    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        if isinstance(current, error_type):
            return True
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return False


async def test_gc_removes_orphans_and_stale_open_but_preserves_recent_upload(
    client: httpx.AsyncClient,
    app,
) -> None:
    now = datetime.now(timezone.utc)
    dataset_id = await _dataset(client)
    recent_id = await _upload(client, dataset_id)
    stale_id = await _upload(client, dataset_id)
    stale_path = upload_directory(app.state.settings, stale_id)
    _set_tree_mtime(stale_path, now - timedelta(hours=25))
    orphan = app.state.settings.storage_dir / "uploads" / "orphaned-directory"
    orphan.mkdir(parents=True)
    (orphan / "payload").write_bytes(b"untracked")
    async with app.state.session_factory() as session:
        session.add(UserStorage(owner_id=1, bytes_used=777))
        await session.commit()

    result = await sweep_upload_storage(
        app.state.session_factory,
        storage_dir=app.state.settings.storage_dir,
        ttl_hours=24,
        resolution_ttl_days=7,
        now=now,
    )

    assert result.orphan_directories == 1
    assert result.expired_sessions == 1
    assert not orphan.exists()
    assert not stale_path.exists()
    assert upload_directory(app.state.settings, recent_id).is_dir()
    async with app.state.session_factory() as session:
        stale = await session.get(UploadSession, stale_id)
        recent = await session.get(UploadSession, recent_id)
        assert stale is not None and stale.state == "aborted"
        assert recent is not None and recent.state == "open"
        assert await get_bytes_used(session, 1) == 777


async def test_gc_finishes_during_an_active_chunk_and_preserves_the_upload(
    client: httpx.AsyncClient,
    app,
) -> None:
    now = datetime.now(timezone.utc)
    dataset_id = await _dataset(client)
    response = await client.post(
        f"/api/datasets/{dataset_id}/uploads",
        json={
            "filename": "active.bin",
            "size": 4,
            "chunk_size": 4,
            "kind": "file",
        },
    )
    assert response.status_code == 201
    upload_id = response.json()["upload_id"]
    path = upload_directory(app.state.settings, upload_id)
    _set_tree_mtime(path, now - timedelta(hours=25))
    stream_started = asyncio.Event()
    release_stream = asyncio.Event()

    async def delayed_stream():
        stream_started.set()
        await release_stream.wait()
        yield b"data"

    async def write_chunk() -> None:
        async with app.state.session_factory() as session:
            await store_chunk_stream(
                session,
                app.state.settings,
                upload_id,
                0,
                delayed_stream(),
                4,
            )

    writer = asyncio.create_task(write_chunk())
    await asyncio.wait_for(stream_started.wait(), timeout=1)
    sweeper = asyncio.create_task(
        sweep_upload_storage(
            app.state.session_factory,
            storage_dir=app.state.settings.storage_dir,
            ttl_hours=24,
            resolution_ttl_days=7,
            now=now,
        )
    )
    try:
        result = await asyncio.wait_for(asyncio.shield(sweeper), timeout=0.5)
        assert not writer.done()
        assert result.expired_sessions == 0
        assert path.is_dir()
        async with app.state.session_factory() as session:
            upload = await session.get(UploadSession, upload_id)
            assert upload is not None and upload.state == "open"
            assert upload.received_chunks == []
    finally:
        release_stream.set()
        await writer
        if not sweeper.done():
            await sweeper

    assert result.expired_sessions == 0
    assert path.is_dir()
    async with app.state.session_factory() as session:
        upload = await session.get(UploadSession, upload_id)
        assert upload is not None and upload.state == "open"
        assert upload.received_chunks == [0]


async def test_abort_finishes_during_an_active_chunk_and_writer_fails_closed(
    client: httpx.AsyncClient,
    app,
) -> None:
    dataset_id = await _dataset(client)
    response = await client.post(
        f"/api/datasets/{dataset_id}/uploads",
        json={
            "filename": "abort-active.bin",
            "size": 4,
            "chunk_size": 4,
            "kind": "file",
        },
    )
    assert response.status_code == 201
    upload_id = response.json()["upload_id"]
    path = upload_directory(app.state.settings, upload_id)
    stream_started = asyncio.Event()
    release_stream = asyncio.Event()

    async def delayed_stream():
        stream_started.set()
        await release_stream.wait()
        yield b"data"

    async def write_chunk() -> None:
        async with app.state.session_factory() as session:
            await store_chunk_stream(
                session,
                app.state.settings,
                upload_id,
                0,
                delayed_stream(),
                4,
            )

    writer = asyncio.create_task(write_chunk())
    await asyncio.wait_for(stream_started.wait(), timeout=1)
    async with app.state.session_factory() as session:
        await asyncio.wait_for(
            abort_upload(session, app.state.settings, upload_id),
            timeout=0.5,
        )
    assert not path.exists()
    release_stream.set()
    with pytest.raises(HTTPException) as error:
        await writer
    assert error.value.status_code == 409
    async with app.state.session_factory() as session:
        upload = await session.get(UploadSession, upload_id)
        assert upload is not None and upload.state == "aborted"


async def test_gc_counts_each_expired_upload_and_preserves_pending_dataset(
    client: httpx.AsyncClient,
    app,
) -> None:
    now = datetime.now(timezone.utc)
    dataset_id = await _dataset(client)
    upload_ids = [await _upload(client, dataset_id) for _ in range(3)]
    completed = await client.post(
        f"/api/datasets/{dataset_id}/upload-batches/complete",
        json={"upload_ids": upload_ids},
    )
    assert completed.status_code == 202
    job_id = completed.json()["job_id"]
    async with app.state.session_factory() as session:
        dataset = await session.get(Dataset, dataset_id)
        job = await session.get(UploadJob, job_id)
        assert dataset is not None and job is not None
        dataset.status = "pending"
        job.failed = 2
        await session.commit()
    for upload_id in upload_ids:
        _set_tree_mtime(
            upload_directory(app.state.settings, upload_id),
            now - timedelta(hours=25),
        )

    result = await sweep_upload_storage(
        app.state.session_factory,
        storage_dir=app.state.settings.storage_dir,
        ttl_hours=24,
        resolution_ttl_days=7,
        now=now,
    )

    assert result.expired_sessions == 3
    assert result.failed_jobs == 1
    async with app.state.session_factory() as session:
        dataset = await session.get(Dataset, dataset_id)
        job = await session.get(UploadJob, job_id)
        uploads = [
            await session.get(UploadSession, upload_id)
            for upload_id in upload_ids
        ]
        assert dataset is not None and dataset.status == "pending"
        assert job is not None
        assert (job.state, job.phase, job.failed) == ("failed", "failed", 5)
        assert all(
            upload is not None and upload.state == "aborted"
            for upload in uploads
        )


async def test_gc_and_dataset_delete_do_not_deadlock(
    client: httpx.AsyncClient,
    app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(timezone.utc)
    dataset_id = await _dataset(client)
    upload_id = await _upload(client, dataset_id)
    completed = await client.post(f"/api/uploads/{upload_id}/complete")
    assert completed.status_code == 202
    _set_tree_mtime(
        upload_directory(app.state.settings, upload_id),
        now - timedelta(hours=25),
    )
    delete_has_dataset_lock = asyncio.Event()
    allow_delete_to_continue = asyncio.Event()
    sweeper_has_upload_lock = asyncio.Event()
    allow_sweeper_to_continue = asyncio.Event()
    original_accounted_bytes = datasets_router.dataset_accounted_bytes
    original_stage_upload_paths = cleanup_service._stage_upload_paths

    async def pause_delete_after_dataset_lock(
        session: AsyncSession,
        target_dataset_id: int,
    ) -> int:
        delete_has_dataset_lock.set()
        await allow_delete_to_continue.wait()
        return await original_accounted_bytes(session, target_dataset_id)

    async def pause_sweeper_after_upload_locks(
        storage_dir: Path,
        paths: list[Path],
    ):
        sweeper_has_upload_lock.set()
        await allow_sweeper_to_continue.wait()
        return await original_stage_upload_paths(storage_dir, paths)

    monkeypatch.setattr(
        datasets_router,
        "dataset_accounted_bytes",
        pause_delete_after_dataset_lock,
    )
    monkeypatch.setattr(
        cleanup_service,
        "_stage_upload_paths",
        pause_sweeper_after_upload_locks,
    )

    delete_task = asyncio.create_task(client.delete(f"/api/datasets/{dataset_id}"))
    await asyncio.wait_for(delete_has_dataset_lock.wait(), timeout=1)
    sweeper_task = asyncio.create_task(
        sweep_upload_storage(
            app.state.session_factory,
            storage_dir=app.state.settings.storage_dir,
            ttl_hours=24,
            resolution_ttl_days=7,
            now=now,
        )
    )
    await asyncio.wait_for(sweeper_has_upload_lock.wait(), timeout=1)
    allow_delete_to_continue.set()
    await asyncio.sleep(0.05)
    allow_sweeper_to_continue.set()
    results = await asyncio.wait_for(
        asyncio.gather(delete_task, sweeper_task, return_exceptions=True),
        timeout=5,
    )

    errors = [result for result in results if isinstance(result, BaseException)]
    assert not any(_caused_by(error, DeadlockDetectedError) for error in errors)
    assert errors == []
    delete_response, sweep_result = results
    assert isinstance(delete_response, httpx.Response)
    assert delete_response.status_code == 204
    assert sweep_result.failed_jobs == 1


async def test_gc_high_id_first_and_dataset_delete_do_not_deadlock(
    client: httpx.AsyncClient,
    app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(timezone.utc)
    dataset_id = await _dataset(client)
    upload_ids = [await _upload(client, dataset_id) for _ in range(2)]
    completed = await client.post(
        f"/api/datasets/{dataset_id}/upload-batches/complete",
        json={"upload_ids": upload_ids},
    )
    assert completed.status_code == 202
    for upload_id in upload_ids:
        _set_tree_mtime(
            upload_directory(app.state.settings, upload_id),
            now - timedelta(hours=25),
        )

    upload_root = app.state.settings.storage_dir / "uploads"
    high_id = max(upload_ids)
    high_lookup_complete = asyncio.Event()
    allow_sweeper_to_continue = asyncio.Event()
    delete_has_dataset_lock = asyncio.Event()
    allow_delete_to_continue = asyncio.Event()
    original_iterdir = Path.iterdir
    original_scalar = AsyncSession.scalar
    original_accounted_bytes = datasets_router.dataset_accounted_bytes
    sweeper_task: asyncio.Task | None = None

    def high_id_first(path: Path):
        entries = list(original_iterdir(path))
        if path == upload_root:
            entries.sort(
                key=lambda entry: (
                    int(entry.name) if entry.name.isdigit() else -1
                ),
                reverse=True,
            )
        return iter(entries)

    async def pause_after_high_lookup(
        session: AsyncSession,
        statement,
        *args,
        **kwargs,
    ):
        value = await original_scalar(session, statement, *args, **kwargs)
        if (
            asyncio.current_task() is sweeper_task
            and isinstance(value, UploadSession)
            and value.id == high_id
            and not high_lookup_complete.is_set()
        ):
            high_lookup_complete.set()
            await allow_sweeper_to_continue.wait()
        return value

    async def pause_delete_after_dataset_lock(
        session: AsyncSession,
        target_dataset_id: int,
    ) -> int:
        delete_has_dataset_lock.set()
        await allow_delete_to_continue.wait()
        return await original_accounted_bytes(session, target_dataset_id)

    monkeypatch.setattr(Path, "iterdir", high_id_first)
    monkeypatch.setattr(AsyncSession, "scalar", pause_after_high_lookup)
    monkeypatch.setattr(
        datasets_router,
        "dataset_accounted_bytes",
        pause_delete_after_dataset_lock,
    )

    sweeper_task = asyncio.create_task(
        sweep_upload_storage(
            app.state.session_factory,
            storage_dir=app.state.settings.storage_dir,
            ttl_hours=24,
            resolution_ttl_days=7,
            now=now,
        )
    )
    await asyncio.wait_for(high_lookup_complete.wait(), timeout=1)
    delete_task = asyncio.create_task(client.delete(f"/api/datasets/{dataset_id}"))
    await asyncio.wait_for(delete_has_dataset_lock.wait(), timeout=1)
    allow_delete_to_continue.set()
    await asyncio.sleep(0.05)
    allow_sweeper_to_continue.set()
    results = await asyncio.wait_for(
        asyncio.gather(delete_task, sweeper_task, return_exceptions=True),
        timeout=6,
    )

    errors = [result for result in results if isinstance(result, BaseException)]
    assert not any(_caused_by(error, DeadlockDetectedError) for error in errors)
    assert errors == []
    delete_response = results[0]
    assert isinstance(delete_response, httpx.Response)
    assert delete_response.status_code == 204


async def test_gc_does_not_fail_dataset_with_active_sibling_job(
    client: httpx.AsyncClient,
    app,
) -> None:
    now = datetime.now(timezone.utc)
    dataset_id = await _dataset(client)
    upload_id = await _upload(client, dataset_id)
    completed = await client.post(f"/api/uploads/{upload_id}/complete")
    assert completed.status_code == 202
    expired_job_id = completed.json()["job_id"]
    async with app.state.session_factory() as session:
        dataset = await session.get(Dataset, dataset_id)
        assert dataset is not None
        dataset.status = "processing"
        sibling = UploadJob(
            dataset_id=dataset_id,
            kind="file",
            state="queued",
            phase="queued",
            total=0,
            processed=0,
            failed=0,
            upload_ids=[],
        )
        session.add(sibling)
        await session.commit()
        sibling_job_id = sibling.id
    _set_tree_mtime(
        upload_directory(app.state.settings, upload_id),
        now - timedelta(hours=25),
    )

    result = await sweep_upload_storage(
        app.state.session_factory,
        storage_dir=app.state.settings.storage_dir,
        ttl_hours=24,
        resolution_ttl_days=7,
        now=now,
    )

    assert result.failed_jobs == 1
    async with app.state.session_factory() as session:
        dataset = await session.get(Dataset, dataset_id)
        expired_job = await session.get(UploadJob, expired_job_id)
        sibling = await session.get(UploadJob, sibling_job_id)
        assert dataset is not None and dataset.status == "processing"
        assert expired_job is not None and expired_job.state == "failed"
        assert sibling is not None and sibling.state == "queued"


async def test_database_engine_leaves_lock_timeout_disabled(app) -> None:
    async with app.state.session_factory() as session:
        lock_timeout = await session.scalar(text("SHOW lock_timeout"))
    assert lock_timeout == "0"


async def test_gc_uses_local_lock_timeout_and_preserves_locked_upload(
    client: httpx.AsyncClient,
    app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(timezone.utc)
    dataset_id = await _dataset(client)
    upload_id = await _upload(client, dataset_id)
    path = upload_directory(app.state.settings, upload_id)
    _set_tree_mtime(path, now - timedelta(hours=25))
    monkeypatch.setattr(db_service, "SHORT_LOCK_TIMEOUT_MS", 50)

    async with app.state.session_factory() as blocker:
        locked = await blocker.scalar(
            select(UploadSession)
            .where(UploadSession.id == upload_id)
            .with_for_update()
        )
        assert locked is not None
        with pytest.raises(Exception) as caught:
            await asyncio.wait_for(
                sweep_upload_storage(
                    app.state.session_factory,
                    storage_dir=app.state.settings.storage_dir,
                    ttl_hours=24,
                    resolution_ttl_days=7,
                    now=now,
                ),
                timeout=1,
            )
        assert db_service.is_lock_not_available(caught.value)
        await blocker.rollback()

    assert path.is_dir()
    async with app.state.session_factory() as session:
        upload = await session.get(UploadSession, upload_id)
        lock_timeout = await session.scalar(text("SHOW lock_timeout"))
        assert upload is not None and upload.state == "open"
        assert lock_timeout == "0"


async def test_upload_delete_does_not_lock_joined_dataset(
    client: httpx.AsyncClient,
    app,
) -> None:
    dataset_id = await _dataset(client)
    upload_id = await _upload(client, dataset_id)
    async with app.state.session_factory() as blocker:
        dataset = await blocker.scalar(
            select(Dataset).where(Dataset.id == dataset_id).with_for_update()
        )
        assert dataset is not None
        response = await asyncio.wait_for(
            client.delete(f"/api/uploads/{upload_id}"),
            timeout=0.5,
        )
        assert response.status_code == 204
        await blocker.rollback()


async def test_lock_unavailable_http_error_is_retryable(
    client: httpx.AsyncClient,
    app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dataset_id = await _dataset(client)
    upload_id = await _upload(client, dataset_id)

    async def raise_lock_unavailable(*_args, **_kwargs) -> None:
        raise _lock_unavailable_error()

    monkeypatch.setattr(uploads_router, "abort_upload", raise_lock_unavailable)
    response = await client.delete(f"/api/uploads/{upload_id}")

    assert response.status_code == 503
    assert response.headers["retry-after"] == "1"
    assert response.json()["detail"] == {
        "code": "database_busy",
        "message": "다른 작업이 처리 중입니다. 잠시 후 다시 시도하세요.",
        "retryable": True,
    }


async def test_dataset_lock_does_not_fail_ingest_or_delete_upload_sources(
    client: httpx.AsyncClient,
    app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dataset_id = await _dataset(client)
    upload_id, job_id = await _completed_image_upload(client, dataset_id)
    path = upload_directory(app.state.settings, upload_id)
    fail_calls: list[int] = []
    original_fail_job = ingest_service.fail_job

    async def observe_fail_job(session_factory, target_job_id, *args, **kwargs):
        fail_calls.append(target_job_id)
        return await original_fail_job(
            session_factory,
            target_job_id,
            *args,
            **kwargs,
        )

    monkeypatch.setattr(ingest_service, "fail_job", observe_fail_job)
    async with app.state.session_factory() as blocker:
        dataset = await blocker.scalar(
            select(Dataset).where(Dataset.id == dataset_id).with_for_update()
        )
        assert dataset is not None
        worker = asyncio.create_task(
            run_upload_batch_job(
                app.state.settings,
                app.state.session_factory,
                job_id,
                [upload_id],
            )
        )
        await asyncio.sleep(6.1)
        assert not worker.done()
        assert fail_calls == []
        assert path.is_dir()
        async with app.state.session_factory() as session:
            job = await session.get(UploadJob, job_id)
            assert job is not None and job.state != "failed"
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker
        await blocker.rollback()

    assert path.is_dir()


async def test_ingest_lock_timeout_does_not_mark_job_failed(
    client: httpx.AsyncClient,
    app,
    tmp_path: Path,
) -> None:
    dataset_id = await _dataset(client)
    image_path = tmp_path / "lock-timeout.jpg"
    image_path.write_bytes(_jpeg_bytes())
    async with app.state.session_factory() as session:
        job = UploadJob(
            dataset_id=dataset_id,
            kind="file",
            state="queued",
            phase="queued",
            total=0,
            processed=0,
            failed=0,
            upload_ids=[],
        )
        session.add(job)
        await session.commit()
        job_id = job.id

    def raise_before_commit() -> None:
        raise _lock_unavailable_error()

    with pytest.raises(OperationalError):
        await ingest_collected(
            app.state.settings,
            app.state.session_factory,
            job_id,
            [
                CollectedFile(
                    rel_path="images/train/lock-timeout.jpg",
                    abs_path=image_path,
                    kind="image",
                    split="train",
                )
            ],
            before_commit=raise_before_commit,
        )

    async with app.state.session_factory() as session:
        dataset = await session.get(Dataset, dataset_id)
        job = await session.get(UploadJob, job_id)
        assert dataset is not None and dataset.status != "failed"
        assert job is not None and job.state != "failed"


async def test_ingest_cancellation_after_server_commit_preserves_final_batch(
    client: httpx.AsyncClient,
    app,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dataset_id = await _dataset(client)
    _upload_id, job_id = await _completed_image_upload(client, dataset_id)
    image_path = tmp_path / "committed-before-cancel.jpg"
    image_path.write_bytes(_jpeg_bytes())
    original_commit = AsyncSession.commit
    server_commit_applied = False

    async def commit_then_cancel(session: AsyncSession) -> None:
        nonlocal server_commit_applied
        await original_commit(session)
        server_commit_applied = True
        raise asyncio.CancelledError

    def cancel_after_commit_starts() -> None:
        monkeypatch.setattr(AsyncSession, "commit", commit_then_cancel)

    try:
        with pytest.raises(asyncio.CancelledError):
            await ingest_collected(
                app.state.settings,
                app.state.session_factory,
                job_id,
                [
                    CollectedFile(
                        rel_path="images/train/committed-before-cancel.jpg",
                        abs_path=image_path,
                        kind="image",
                        split="train",
                    )
                ],
                before_commit=cancel_after_commit_starts,
            )
    finally:
        monkeypatch.setattr(AsyncSession, "commit", original_commit)

    assert server_commit_applied
    async with app.state.session_factory() as session:
        dataset = await session.get(Dataset, dataset_id)
        images = (
            await session.scalars(
                select(Image).where(Image.dataset_id == dataset_id)
            )
        ).all()

    assert dataset is not None
    assert len(images) == 1
    final_batch = (
        contained_storage_path(
            app.state.settings.storage_dir,
            dataset.storage_path,
        )
        / "batches"
        / str(job_id)
    )
    assert final_batch.is_dir()
    for stored_path in (
        images[0].file_path,
        images[0].display_path,
        images[0].thumb_path,
    ):
        if stored_path is not None:
            assert contained_storage_path(
                app.state.settings.storage_dir,
                stored_path,
            ).is_file()


async def test_upload_worker_preserves_sources_on_lock_timeout(
    client: httpx.AsyncClient,
    app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dataset_id = await _dataset(client)
    upload_id, job_id = await _completed_image_upload(client, dataset_id)
    path = upload_directory(app.state.settings, upload_id)

    async def raise_lock_timeout(*_args, **_kwargs) -> None:
        raise _lock_unavailable_error()

    monkeypatch.setattr(
        ingest_service,
        "ingest_collected",
        raise_lock_timeout,
    )

    with pytest.raises(OperationalError):
        await run_upload_batch_job(
            app.state.settings,
            app.state.session_factory,
            job_id,
            [upload_id],
        )

    assert path.is_dir()
    async with app.state.session_factory() as session:
        job = await session.get(UploadJob, job_id)
        assert job is not None and job.state != "failed"


async def test_gc_uses_longer_class_resolution_ttl_and_fails_expired_job(
    client: httpx.AsyncClient,
    app,
) -> None:
    now = datetime.now(timezone.utc)
    dataset_id = await _dataset(client)
    upload_id = await _upload(client, dataset_id)
    completed = await client.post(f"/api/uploads/{upload_id}/complete")
    assert completed.status_code == 202
    job_id = completed.json()["job_id"]
    path = upload_directory(app.state.settings, upload_id)
    async with app.state.session_factory() as session:
        job = await session.get(UploadJob, job_id)
        assert job is not None
        job.state = "awaiting_class_resolution"
        job.phase = "awaiting_class_resolution"
        await session.commit()
    _set_tree_mtime(path, now - timedelta(days=2))

    preserved = await sweep_upload_storage(
        app.state.session_factory,
        storage_dir=app.state.settings.storage_dir,
        ttl_hours=24,
        resolution_ttl_days=7,
        now=now,
    )
    assert preserved.reclaimed_directories == 0
    assert path.is_dir()

    _set_tree_mtime(path, now - timedelta(days=8))
    reclaimed = await sweep_upload_storage(
        app.state.session_factory,
        storage_dir=app.state.settings.storage_dir,
        ttl_hours=24,
        resolution_ttl_days=7,
        now=now,
    )
    assert reclaimed.failed_jobs == 1
    assert not path.exists()
    async with app.state.session_factory() as session:
        upload = await session.get(UploadSession, upload_id)
        job = await session.get(UploadJob, job_id)
        dataset = await session.get(Dataset, dataset_id)
        assert upload is not None and upload.state == "aborted"
        assert job is not None and (job.state, job.phase) == ("failed", "failed")
        assert dataset is not None and dataset.status == "failed"


async def test_gc_reclaims_terminal_job_upload_immediately(
    client: httpx.AsyncClient,
    app,
) -> None:
    dataset_id = await _dataset(client)
    upload_id = await _upload(client, dataset_id)
    completed = await client.post(f"/api/uploads/{upload_id}/complete")
    job_id = completed.json()["job_id"]
    async with app.state.session_factory() as session:
        job = await session.get(UploadJob, job_id)
        assert job is not None
        job.state = "done"
        job.phase = "done"
        await session.commit()

    result = await sweep_upload_storage(
        app.state.session_factory,
        storage_dir=app.state.settings.storage_dir,
    )

    assert result.reclaimed_directories == 1
    assert not upload_directory(app.state.settings, upload_id).exists()


async def test_gc_reclaims_aborted_session_upload_immediately(
    client: httpx.AsyncClient,
    app,
) -> None:
    dataset_id = await _dataset(client)
    upload_id = await _upload(client, dataset_id)
    async with app.state.session_factory() as session:
        upload = await session.get(UploadSession, upload_id)
        assert upload is not None
        upload.state = "aborted"
        await session.commit()

    result = await sweep_upload_storage(
        app.state.session_factory,
        storage_dir=app.state.settings.storage_dir,
    )

    assert result.reclaimed_directories == 1
    assert not upload_directory(app.state.settings, upload_id).exists()


async def test_gc_restores_expired_upload_when_database_commit_fails(
    client: httpx.AsyncClient,
    app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(timezone.utc)
    dataset_id = await _dataset(client)
    upload_id = await _upload(client, dataset_id)
    path = upload_directory(app.state.settings, upload_id)
    _set_tree_mtime(path, now - timedelta(hours=25))
    original_commit = AsyncSession.commit

    async def fail_commit(_session: AsyncSession) -> None:
        raise RuntimeError("forced upload GC commit failure")

    monkeypatch.setattr(AsyncSession, "commit", fail_commit)
    with pytest.raises(RuntimeError, match="forced upload GC commit failure"):
        await sweep_upload_storage(
            app.state.session_factory,
            storage_dir=app.state.settings.storage_dir,
            ttl_hours=24,
            resolution_ttl_days=7,
            now=now,
        )
    monkeypatch.setattr(AsyncSession, "commit", original_commit)

    assert path.is_dir()
    async with app.state.session_factory() as session:
        upload = await session.get(UploadSession, upload_id)
        assert upload is not None and upload.state == "open"
