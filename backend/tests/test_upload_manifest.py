from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import func, select

from app.models import UploadBatch, UploadJob, UploadSession
from app.services import uploads as uploads_service
from app.services.cleanup import sweep_upload_storage


pytestmark = pytest.mark.asyncio


async def create_dataset(client: httpx.AsyncClient) -> int:
    response = await client.post(
        "/api/datasets",
        json={"name": f"test-upload-manifest-{uuid4().hex}"},
    )
    assert response.status_code == 201
    return response.json()["id"]


def manifest_body(*, file_count: int = 2, total_size: int = 0) -> dict[str, int]:
    return {
        "total_size": total_size,
        "largest_file_size": total_size,
        "file_count": file_count,
        "expected_extracted_size": total_size,
    }


def session_body(batch_id: str, names: list[str]) -> dict[str, object]:
    return {
        "batch_id": batch_id,
        "files": [
            {
                "filename": name,
                "size": 0,
                "chunk_size": 1,
                "kind": "file",
            }
            for name in names
        ],
    }


async def test_manifest_session_creation_and_completion_are_idempotent(
    client: httpx.AsyncClient,
    app,
) -> None:
    dataset_id = await create_dataset(client)
    batch_id = str(uuid4())
    manifest_url = f"/api/datasets/{dataset_id}/upload-batches/{batch_id}"

    started = await client.put(manifest_url, json=manifest_body())
    replayed_start = await client.put(manifest_url, json=manifest_body())

    assert started.status_code == replayed_start.status_code == 200
    assert started.json() == replayed_start.json() == {
        "batch_id": batch_id,
        "state": "open",
        "job_id": None,
    }

    create_url = f"/api/datasets/{dataset_id}/uploads/batch"
    body = session_body(batch_id, ["images/a.jpg", "labels/a.txt"])
    created, replayed_create = await asyncio.gather(
        client.post(create_url, json=body),
        client.post(create_url, json=body),
    )

    assert created.status_code == replayed_create.status_code == 201
    assert created.json() == replayed_create.json()
    upload_ids = [item["upload_id"] for item in created.json()["uploads"]]
    assert len(upload_ids) == 2
    assert all(item["state"] == "open" for item in created.json()["uploads"])

    complete_url = f"{manifest_url}/complete"
    completed, replayed_complete = await asyncio.gather(
        client.post(complete_url),
        client.post(complete_url),
    )

    assert completed.status_code == replayed_complete.status_code == 202
    assert completed.json() == replayed_complete.json()
    job_id = completed.json()["job_id"]
    async with app.state.session_factory() as session:
        job_count = await session.scalar(
            select(func.count(UploadJob.id)).where(
                UploadJob.dataset_id == dataset_id
            )
        )
        rows = list(
            (
                await session.scalars(
                    select(UploadSession)
                    .where(UploadSession.id.in_(upload_ids))
                    .order_by(UploadSession.id)
                )
            ).all()
        )
        job = await session.get(UploadJob, job_id)
    assert job_count == 1
    assert job is not None and job.upload_ids == upload_ids
    assert [row.state for row in rows] == ["complete", "complete"]


async def test_manifest_completion_rejects_partial_session_creation_atomically(
    client: httpx.AsyncClient,
    app,
) -> None:
    dataset_id = await create_dataset(client)
    batch_id = str(uuid4())
    manifest_url = f"/api/datasets/{dataset_id}/upload-batches/{batch_id}"
    assert (
        await client.put(manifest_url, json=manifest_body(file_count=2))
    ).status_code == 200
    created = await client.post(
        f"/api/datasets/{dataset_id}/uploads/batch",
        json=session_body(batch_id, ["images/only-one.jpg"]),
    )
    assert created.status_code == 201
    upload_id = created.json()["uploads"][0]["upload_id"]

    completed = await client.post(f"{manifest_url}/complete")

    assert completed.status_code == 409
    assert "2" in str(completed.json()["detail"])
    async with app.state.session_factory() as session:
        job_count = await session.scalar(
            select(func.count(UploadJob.id)).where(
                UploadJob.dataset_id == dataset_id
            )
        )
        upload = await session.get(UploadSession, upload_id)
    assert job_count == 0
    assert upload is not None and upload.state == "open"


async def test_manifest_rejects_changed_totals_and_session_overflow(
    client: httpx.AsyncClient,
    app,
) -> None:
    dataset_id = await create_dataset(client)
    batch_id = str(uuid4())
    manifest_url = f"/api/datasets/{dataset_id}/upload-batches/{batch_id}"
    assert (
        await client.put(
            manifest_url,
            json=manifest_body(file_count=1, total_size=0),
        )
    ).status_code == 200

    changed = await client.put(
        manifest_url,
        json=manifest_body(file_count=2, total_size=0),
    )
    overflow = await client.post(
        f"/api/datasets/{dataset_id}/uploads/batch",
        json=session_body(batch_id, ["images/a.jpg", "images/b.jpg"]),
    )

    assert changed.status_code == 409
    assert overflow.status_code == 409
    async with app.state.session_factory() as session:
        session_count = await session.scalar(
            select(func.count(UploadSession.id)).where(
                UploadSession.dataset_id == dataset_id
            )
        )
    assert session_count == 0


async def test_manifest_accepts_one_million_files_without_an_id_payload(
    client: httpx.AsyncClient,
) -> None:
    dataset_id = await create_dataset(client)
    batch_id = str(uuid4())

    started = await client.put(
        f"/api/datasets/{dataset_id}/upload-batches/{batch_id}",
        json=manifest_body(file_count=1_000_000, total_size=0),
    )

    assert started.status_code == 200, started.text
    assert started.json() == {
        "batch_id": batch_id,
        "state": "open",
        "job_id": None,
    }


async def test_manifest_routes_hide_foreign_batches(
    client: httpx.AsyncClient,
    auth_headers,
) -> None:
    dataset_id = await create_dataset(client)
    batch_id = str(uuid4())
    manifest_url = f"/api/datasets/{dataset_id}/upload-batches/{batch_id}"
    assert (
        await client.put(manifest_url, json=manifest_body(file_count=1))
    ).status_code == 200
    foreign_headers = auth_headers(2)

    replay = await client.put(
        manifest_url,
        headers=foreign_headers,
        json=manifest_body(file_count=1),
    )
    create = await client.post(
        f"/api/datasets/{dataset_id}/uploads/batch",
        headers=foreign_headers,
        json=session_body(batch_id, ["images/a.jpg"]),
    )
    complete = await client.post(
        f"{manifest_url}/complete",
        headers=foreign_headers,
    )

    assert replay.status_code == create.status_code == complete.status_code == 404


async def test_zero_byte_assembly_repairs_a_missing_session_directory(app) -> None:
    upload = UploadSession(
        id=987_654_321,
        dataset_id=1,
        filename="classes.txt",
        size=0,
        chunk_size=1,
        received_chunks=[],
        kind="file",
        state="complete",
    )
    directory = uploads_service.upload_directory(app.state.settings, upload.id)
    assert not directory.exists()

    assembled = uploads_service._assemble_chunks(app.state.settings, upload)

    assert assembled.read_bytes() == b""


async def test_expired_manifest_rejects_old_batch_and_accepts_fresh_batch(
    client: httpx.AsyncClient,
    app,
) -> None:
    dataset_id = await create_dataset(client)
    batch_id = str(uuid4())
    manifest_url = f"/api/datasets/{dataset_id}/upload-batches/{batch_id}"
    page_url = f"/api/datasets/{dataset_id}/uploads/batch"
    assert (
        await client.put(manifest_url, json=manifest_body(file_count=1))
    ).status_code == 200
    created = await client.post(
        page_url,
        json=session_body(batch_id, ["images/a.jpg"]),
    )
    assert created.status_code == 201
    upload_id = created.json()["uploads"][0]["upload_id"]
    directory = uploads_service.upload_directory(app.state.settings, upload_id)
    now = datetime.now(timezone.utc)
    stale_timestamp = (now - timedelta(hours=25)).timestamp()
    os.utime(directory, (stale_timestamp, stale_timestamp))

    result = await sweep_upload_storage(
        app.state.session_factory,
        storage_dir=app.state.settings.storage_dir,
        now=now,
    )

    assert result.expired_sessions == 1
    assert not directory.exists()
    async with app.state.session_factory() as session:
        batch = await session.get(UploadBatch, UUID(batch_id))
        upload = await session.get(UploadSession, upload_id)
        assert batch is not None and batch.state == "aborted"
        assert upload is not None and upload.state == "aborted"

    replay = await client.put(manifest_url, json=manifest_body(file_count=1))
    assert replay.status_code == 409
    assert replay.json()["detail"] == {
        "code": "upload_batch_aborted",
        "message": "이전 업로드 기록이 만료되었습니다. 새로 시작합니다.",
    }

    fresh_batch_id = str(uuid4())
    fresh_manifest_url = (
        f"/api/datasets/{dataset_id}/upload-batches/{fresh_batch_id}"
    )
    restarted = await client.put(
        fresh_manifest_url,
        json=manifest_body(file_count=1),
    )
    fresh_page = await client.post(
        page_url,
        json=session_body(fresh_batch_id, ["images/a.jpg"]),
    )
    assert restarted.status_code == 200
    assert restarted.json() == {
        "batch_id": fresh_batch_id,
        "state": "open",
        "job_id": None,
    }
    assert fresh_page.status_code == 201
    fresh_upload = fresh_page.json()["uploads"][0]
    assert fresh_upload["upload_id"] != upload_id
    assert fresh_upload["state"] == "open"
    assert uploads_service.upload_directory(
        app.state.settings,
        fresh_upload["upload_id"],
    ).is_dir()
    async with app.state.session_factory() as session:
        assert await session.scalar(
            select(func.count(UploadSession.id)).where(
                UploadSession.dataset_id == dataset_id
            )
        ) == 2


async def test_aborted_manifest_session_returns_restart_code(
    client: httpx.AsyncClient,
    app,
) -> None:
    dataset_id = await create_dataset(client)
    batch_id = str(uuid4())
    manifest_url = f"/api/datasets/{dataset_id}/upload-batches/{batch_id}"
    assert (
        await client.put(manifest_url, json=manifest_body(file_count=1))
    ).status_code == 200
    page_url = f"/api/datasets/{dataset_id}/uploads/batch"
    body = session_body(batch_id, ["images/a.jpg"])
    created = await client.post(page_url, json=body)
    assert created.status_code == 201
    upload_id = created.json()["uploads"][0]["upload_id"]
    assert (await client.delete(f"/api/uploads/{upload_id}")).status_code == 204

    replay = await client.post(page_url, json=body)

    assert replay.status_code == 409
    detail = replay.json()["detail"]
    assert detail["code"] == "upload_session_aborted"
    assert isinstance(detail["message"], str) and detail["message"]
    async with app.state.session_factory() as session:
        batch = await session.get(UploadBatch, UUID(batch_id))
        upload = await session.get(UploadSession, upload_id)
        assert batch is not None and batch.state == "open"
        assert upload is not None and upload.state == "aborted"
        assert await session.scalar(
            select(func.count(UploadSession.id)).where(
                UploadSession.upload_batch_id == UUID(batch_id)
            )
        ) == 1


@pytest.mark.parametrize("state", ["done", "complete"])
async def test_closed_manifest_session_preserves_string_conflict(
    client: httpx.AsyncClient,
    app,
    state: str,
) -> None:
    dataset_id = await create_dataset(client)
    batch_id = str(uuid4())
    assert (
        await client.put(
            f"/api/datasets/{dataset_id}/upload-batches/{batch_id}",
            json=manifest_body(file_count=1),
        )
    ).status_code == 200
    page_url = f"/api/datasets/{dataset_id}/uploads/batch"
    body = session_body(batch_id, ["images/a.jpg"])
    created = await client.post(page_url, json=body)
    assert created.status_code == 201
    upload_id = created.json()["uploads"][0]["upload_id"]
    async with app.state.session_factory() as session:
        upload = await session.get(UploadSession, upload_id)
        assert upload is not None
        upload.state = state
        await session.commit()

    replay = await client.post(page_url, json=body)

    assert replay.status_code == 409
    assert replay.json()["detail"] == "images/a.jpg: 이미 닫힌 업로드 세션입니다."
    async with app.state.session_factory() as session:
        upload = await session.get(UploadSession, upload_id)
        assert upload is not None and upload.state == state


async def test_sealed_manifest_put_restores_job_and_preserves_closed_write_guards(
    client: httpx.AsyncClient,
) -> None:
    dataset_id = await create_dataset(client)
    batch_id = str(uuid4())
    manifest_url = f"/api/datasets/{dataset_id}/upload-batches/{batch_id}"
    assert (
        await client.put(manifest_url, json=manifest_body(file_count=1))
    ).status_code == 200
    assert (
        await client.post(
            f"/api/datasets/{dataset_id}/uploads/batch",
            json=session_body(batch_id, ["images/a.jpg"]),
        )
    ).status_code == 201
    completed = await client.post(f"{manifest_url}/complete")
    assert completed.status_code == 202

    replay = await client.put(manifest_url, json=manifest_body(file_count=1))

    assert replay.status_code == 200
    assert replay.json() == {
        "batch_id": batch_id,
        "state": "sealed",
        "job_id": completed.json()["job_id"],
    }
    page = await client.post(
        f"/api/datasets/{dataset_id}/uploads/batch",
        json=session_body(batch_id, ["images/a.jpg"]),
    )
    assert page.status_code == 409
    assert page.json()["detail"] == "이미 봉인된 업로드 배치입니다."
    replayed_complete = await client.post(f"{manifest_url}/complete")
    assert replayed_complete.status_code == 202
    assert replayed_complete.json() == completed.json()


@pytest.mark.parametrize("job_state", ["running", "awaiting_class_resolution"])
async def test_sealed_manifest_replay_restores_active_job_without_new_rows(
    client: httpx.AsyncClient,
    app,
    job_state: str,
) -> None:
    dataset_id = await create_dataset(client)
    batch_id = str(uuid4())
    manifest_url = f"/api/datasets/{dataset_id}/upload-batches/{batch_id}"
    original_manifest = manifest_body(file_count=1)
    assert (
        await client.put(manifest_url, json=original_manifest)
    ).status_code == 200
    created = await client.post(
        f"/api/datasets/{dataset_id}/uploads/batch",
        json=session_body(batch_id, ["images/a.jpg"]),
    )
    assert created.status_code == 201
    upload_id = created.json()["uploads"][0]["upload_id"]
    completed = await client.post(f"{manifest_url}/complete")
    assert completed.status_code == 202
    job_id = completed.json()["job_id"]
    async with app.state.session_factory() as session:
        job = await session.get(UploadJob, job_id)
        assert job is not None
        job.state = job_state
        await session.commit()

    replay = await client.put(manifest_url, json=original_manifest)
    assert replay.status_code == 200
    assert replay.json() == {
        "batch_id": batch_id,
        "state": "sealed",
        "job_id": job_id,
    }
    changed_totals = await client.put(
        manifest_url,
        json=manifest_body(file_count=2, total_size=8),
    )
    assert changed_totals.status_code == 409
    assert changed_totals.json()["detail"] == (
        "같은 업로드 배치의 파일 수 또는 용량이 변경되었습니다."
    )

    async with app.state.session_factory() as session:
        jobs = list((await session.scalars(
            select(UploadJob).where(UploadJob.dataset_id == dataset_id)
        )).all())
        uploads = list((await session.scalars(
            select(UploadSession).where(UploadSession.dataset_id == dataset_id)
        )).all())
        assert [job.id for job in jobs] == [job_id]
        assert jobs[0].state == job_state
        assert jobs[0].upload_ids == [upload_id]
        assert [upload.id for upload in uploads] == [upload_id]
        assert uploads[0].state == "complete"
        batch = await session.get(UploadBatch, UUID(batch_id))
        assert batch is not None and batch.state == "sealed"
        assert batch.expected_file_count == 1
        assert batch.expected_total_size == batch.expected_extracted_size == 0
        assert batch.largest_file_size == 0


@pytest.mark.parametrize("operation", ["page", "complete"])
async def test_expired_batch_returns_restart_code_for_page_and_completion(
    client: httpx.AsyncClient,
    app,
    operation: str,
) -> None:
    dataset_id = await create_dataset(client)
    batch_id = str(uuid4())
    manifest_url = f"/api/datasets/{dataset_id}/upload-batches/{batch_id}"
    page_url = f"/api/datasets/{dataset_id}/uploads/batch"
    body = session_body(batch_id, ["images/a.jpg"])
    assert (
        await client.put(manifest_url, json=manifest_body(file_count=1))
    ).status_code == 200
    created = await client.post(page_url, json=body)
    assert created.status_code == 201
    upload_id = created.json()["uploads"][0]["upload_id"]
    directory = uploads_service.upload_directory(app.state.settings, upload_id)
    now = datetime.now(timezone.utc)
    stale_timestamp = (now - timedelta(hours=25)).timestamp()
    os.utime(directory, (stale_timestamp, stale_timestamp))
    result = await sweep_upload_storage(
        app.state.session_factory,
        storage_dir=app.state.settings.storage_dir,
        now=now,
    )
    assert result.expired_sessions == 1

    if operation == "page":
        rejected = await client.post(page_url, json=body)
    else:
        rejected = await client.post(f"{manifest_url}/complete")

    assert rejected.status_code == 409
    assert rejected.json()["detail"] == {
        "code": "upload_batch_aborted",
        "message": "이전 업로드 기록이 만료되었습니다. 새로 시작합니다.",
    }
    assert not directory.exists()
    async with app.state.session_factory() as session:
        batch = await session.get(UploadBatch, UUID(batch_id))
        upload = await session.get(UploadSession, upload_id)
        assert batch is not None and batch.state == "aborted"
        assert upload is not None and upload.state == "aborted"
        assert await session.scalar(
            select(func.count(UploadJob.id)).where(UploadJob.dataset_id == dataset_id)
        ) == 0
        assert await session.scalar(
            select(func.count(UploadSession.id)).where(
                UploadSession.dataset_id == dataset_id
            )
        ) == 1


async def test_sealed_manifest_without_job_preserves_string_complete_conflict(
    client: httpx.AsyncClient,
    app,
) -> None:
    dataset_id = await create_dataset(client)
    batch_id = uuid4()
    manifest_url = f"/api/datasets/{dataset_id}/upload-batches/{batch_id}"
    assert (
        await client.put(manifest_url, json=manifest_body(file_count=1))
    ).status_code == 200
    async with app.state.session_factory() as session:
        batch = await session.get(UploadBatch, batch_id)
        assert batch is not None
        batch.state = "sealed"
        await session.commit()

    rejected = await client.post(f"{manifest_url}/complete")

    assert rejected.status_code == 409
    assert rejected.json()["detail"] == "이미 봉인된 업로드 배치입니다."
    async with app.state.session_factory() as session:
        assert await session.scalar(
            select(func.count(UploadJob.id)).where(UploadJob.dataset_id == dataset_id)
        ) == 0
