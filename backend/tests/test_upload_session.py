from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import func, select

from app.models import UploadJob, UploadSession
from app.services import uploads as uploads_service


pytestmark = pytest.mark.asyncio


class CountingStream(httpx.AsyncByteStream):
    def __init__(self, chunks: int, chunk: bytes) -> None:
        self.chunks = chunks
        self.chunk = chunk
        self.yielded = 0

    async def __aiter__(self):
        for _ in range(self.chunks):
            self.yielded += 1
            yield self.chunk


async def create_dataset(client: httpx.AsyncClient) -> int:
    response = await client.post(
        "/api/datasets",
        json={"name": f"test-upload-{uuid4().hex}"},
    )
    assert response.status_code == 201
    return response.json()["id"]


async def upload_file(
    client: httpx.AsyncClient,
    dataset_id: int,
    filename: str,
    content: bytes,
) -> int:
    created = await client.post(
        f"/api/datasets/{dataset_id}/uploads",
        json={
            "filename": filename,
            "size": len(content),
            "chunk_size": max(1, len(content)),
            "kind": "file",
        },
    )
    assert created.status_code == 201
    upload_id = created.json()["upload_id"]
    if content:
        assert (
            await client.put(
                f"/api/uploads/{upload_id}/chunks/0",
                content=content,
            )
        ).status_code == 204
    return upload_id


async def test_chunk_upload_is_resumable_idempotent_and_completes_to_a_job(
    client: httpx.AsyncClient,
    app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dataset_id = await create_dataset(client)
    created = await client.post(
        f"/api/datasets/{dataset_id}/uploads",
        json={
            "filename": "images/train/sample.jpg",
            "size": 12,
            "chunk_size": 4,
            "kind": "file",
        },
    )

    assert created.status_code == 201
    body = created.json()
    assert body["chunk_size"] == 4
    assert body["received"] == []
    upload_id = body["upload_id"]

    assert (
        await client.put(
            f"/api/uploads/{upload_id}/chunks/0",
            content=b"abcd",
            headers={"Content-Type": "application/octet-stream"},
        )
    ).status_code == 204
    assert (
        await client.put(
            f"/api/uploads/{upload_id}/chunks/2",
            content=b"ijkl",
            headers={"Content-Type": "application/octet-stream"},
        )
    ).status_code == 204
    # Replaying an identical chunk is a successful no-op.
    assert (
        await client.put(
            f"/api/uploads/{upload_id}/chunks/0",
            content=b"abcd",
            headers={"Content-Type": "application/octet-stream"},
        )
    ).status_code == 204

    resumed = await client.get(f"/api/uploads/{upload_id}")
    assert resumed.status_code == 200
    assert resumed.json() == {
        "upload_id": upload_id,
        "chunk_size": 4,
        "received": [0, 2],
        "size": 12,
        "state": "open",
    }
    assert (
        await client.post(f"/api/uploads/{upload_id}/complete")
    ).status_code == 409

    assert (
        await client.put(
            f"/api/uploads/{upload_id}/chunks/1",
            content=b"efgh",
            headers={"Content-Type": "application/octet-stream"},
        )
    ).status_code == 204
    assembly_calls = 0
    original_assemble = uploads_service._assemble_chunks

    def observe_assembly(*args, **kwargs):
        nonlocal assembly_calls
        assembly_calls += 1
        return original_assemble(*args, **kwargs)

    monkeypatch.setattr(uploads_service, "_assemble_chunks", observe_assembly)
    completed = await client.post(f"/api/uploads/{upload_id}/complete")
    assert completed.status_code == 202
    assert assembly_calls == 0
    job_id = completed.json()["job_id"]
    assert (
        await client.get(f"/api/uploads/{upload_id}")
    ).json()["state"] == "complete"

    async with app.state.session_factory() as session:
        upload = await session.get(UploadSession, upload_id)
        job = await session.get(UploadJob, job_id)
        assert upload is not None
        assert job is not None
        assert job.state == "queued"
        assert job.phase == "uploading"
        assembled = (
            Path(app.state.settings.storage_dir)
            / "uploads"
            / str(upload_id)
            / "source"
        )
        assert not assembled.exists()
        assert sorted(
            path.read_bytes()
            for path in (assembled.parent / "chunks").glob("*.part")
        ) == [b"abcd", b"efgh", b"ijkl"]

    published = original_assemble(app.state.settings, upload)
    assert published.read_bytes() == b"abcdefghijkl"
    assert not (published.parent / "chunks").exists()


async def test_chunk_validation_and_aborted_sessions_fail_closed(
    client: httpx.AsyncClient,
    app,
) -> None:
    dataset_id = await create_dataset(client)
    created = await client.post(
        f"/api/datasets/{dataset_id}/uploads",
        json={
            "filename": "sample.png",
            "size": 5,
            "chunk_size": 4,
            "kind": "file",
        },
    )
    upload_id = created.json()["upload_id"]

    assert (
        await client.put(
            f"/api/uploads/{upload_id}/chunks/0",
            content=b"bad",
        )
    ).status_code == 422
    assert (
        await client.put(
            f"/api/uploads/{upload_id}/chunks/2",
            content=b"x",
        )
    ).status_code == 416

    async with app.state.session_factory() as session:
        upload = await session.get(UploadSession, upload_id)
        assert upload is not None
        upload.state = "aborted"
        await session.commit()

    rejected = await client.put(
        f"/api/uploads/{upload_id}/chunks/0",
        content=b"abcd",
    )
    assert rejected.status_code == 409
    assert "처음부터" in rejected.json()["detail"]


async def test_upload_session_rejects_missing_dataset(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post(
        "/api/datasets/999999999/uploads",
        json={
            "filename": "sample.jpg",
            "size": 4,
            "chunk_size": 4,
            "kind": "file",
        },
    )
    assert response.status_code == 404


async def test_batch_completion_creates_one_job_for_all_file_sessions(
    client: httpx.AsyncClient,
    app,
) -> None:
    dataset_id = await create_dataset(client)
    upload_ids = [
        await upload_file(
            client,
            dataset_id,
            "images/train/a.jpg",
            b"\xff\xd8\xffimage-a",
        ),
        await upload_file(
            client,
            dataset_id,
            "labels/train/a.txt",
            b"0 0.5 0.5 0.2 0.2\n",
        ),
    ]

    completed = await client.post(
        f"/api/datasets/{dataset_id}/upload-batches/complete",
        json={"upload_ids": upload_ids},
    )

    assert completed.status_code == 202
    job_id = completed.json()["job_id"]
    async with app.state.session_factory() as session:
        jobs = await session.scalar(
            select(func.count(UploadJob.id)).where(
                UploadJob.dataset_id == dataset_id
            )
        )
        uploads = (
            await session.scalars(
                select(UploadSession)
                .where(UploadSession.id.in_(upload_ids))
                .order_by(UploadSession.id)
            )
        ).all()
        assert jobs == 1
        assert all(upload.state == "complete" for upload in uploads)
        assert (await session.get(UploadJob, job_id)) is not None


async def test_oversized_chunk_is_rejected_before_declared_body_is_read(
    client: httpx.AsyncClient,
    app,
) -> None:
    dataset_id = await create_dataset(client)
    upload_id = await upload_file(client, dataset_id, "empty.txt", b"")
    async with app.state.session_factory() as session:
        upload = await session.get(UploadSession, upload_id)
        assert upload is not None
        upload.size = 4
        upload.chunk_size = 4
        await session.commit()
    stream = CountingStream(chunks=32_768, chunk=b"x" * 1024)

    response = await client.put(
        f"/api/uploads/{upload_id}/chunks/0",
        content=stream,
        headers={"Content-Length": str(32 * 1024**2)},
    )

    assert response.status_code == 413
    assert stream.yielded == 0
    assert (await client.get("/api/health")).status_code == 200


async def test_chunked_body_stops_streaming_as_soon_as_limit_is_exceeded(
    client: httpx.AsyncClient,
    app,
) -> None:
    dataset_id = await create_dataset(client)
    created = await client.post(
        f"/api/datasets/{dataset_id}/uploads",
        json={
            "filename": "sample.txt",
            "size": 4,
            "chunk_size": 4,
            "kind": "file",
        },
    )
    upload_id = created.json()["upload_id"]
    stream = CountingStream(chunks=10_000, chunk=b"xxxx")

    response = await client.put(
        f"/api/uploads/{upload_id}/chunks/0",
        content=stream,
    )

    assert response.status_code == 413
    assert stream.yielded <= 2
    async with app.state.session_factory() as session:
        upload = await session.get(UploadSession, upload_id)
        assert upload is not None
        assert upload.received_chunks == []
    assert not (
        Path(app.state.settings.storage_dir)
        / "uploads"
        / str(upload_id)
        / "chunks"
        / "0.part"
    ).exists()
    chunks = (
        Path(app.state.settings.storage_dir)
        / "uploads"
        / str(upload_id)
        / "chunks"
    )
    assert list(chunks.glob(".*.tmp")) == []
    assert (await client.get("/api/health")).status_code == 200
