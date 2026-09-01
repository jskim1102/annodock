from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import func, select

from app.models import UploadJob, UploadSession
from app.routers import uploads as uploads_router
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


async def test_batch_upload_session_creation_preserves_order_and_checks_totals_once(
    client: httpx.AsyncClient,
    app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dataset_id = await create_dataset(client)
    capacity_calls: list[dict[str, int]] = []
    quota_calls: list[tuple[int, int]] = []

    def observe_capacity(_request, **values: int) -> None:
        capacity_calls.append(values)

    async def observe_quota(_session, _request, owner_id: int, required: int) -> None:
        quota_calls.append((owner_id, required))

    monkeypatch.setattr(uploads_router, "_check_capacity", observe_capacity)
    monkeypatch.setattr(uploads_router, "_check_user_quota", observe_quota)
    response = await client.post(
        f"/api/datasets/{dataset_id}/uploads/batch",
        json={
            "files": [
                {
                    "filename": "images/first.jpg",
                    "size": 3,
                    "chunk_size": 2,
                    "kind": "file",
                    "file_count": 1,
                },
                {
                    "filename": "archives/second.zip",
                    "size": 5,
                    "chunk_size": 4,
                    "kind": "zip",
                    "file_count": 4,
                    "expected_extracted_size": 12,
                },
            ]
        },
    )

    assert response.status_code == 201, response.text
    created = response.json()["uploads"]
    assert [item["chunk_size"] for item in created] == [2, 4]
    assert [item["received"] for item in created] == [[], []]
    assert capacity_calls == [{"size": 8, "file_count": 5, "expected_extracted_size": 15}]
    assert quota_calls == [(1, 15)]
    async with app.state.session_factory() as session:
        rows = (
            await session.scalars(
                select(UploadSession)
                .where(UploadSession.id.in_([item["upload_id"] for item in created]))
                .order_by(UploadSession.id)
            )
        ).all()
    assert [row.filename for row in rows] == [
        "images/first.jpg",
        "archives/second.zip",
    ]
    assert all(
        (
            Path(app.state.settings.storage_dir)
            / "uploads"
            / str(item["upload_id"])
        ).is_dir()
        for item in created
    )


async def test_batch_upload_session_creation_rejects_invalid_batch_lengths(
    client: httpx.AsyncClient,
) -> None:
    dataset_id = await create_dataset(client)
    file_body = {
        "filename": "sample.jpg",
        "size": 1,
        "chunk_size": 1,
        "kind": "file",
    }

    empty = await client.post(
        f"/api/datasets/{dataset_id}/uploads/batch",
        json={"files": []},
    )
    maximum = await client.post(
        f"/api/datasets/{dataset_id}/uploads/batch",
        json={"files": [file_body] * 1_000},
    )
    oversized = await client.post(
        f"/api/datasets/{dataset_id}/uploads/batch",
        json={"files": [file_body] * 1_001},
    )

    assert empty.status_code == 422
    assert maximum.status_code == 201
    assert len(maximum.json()["uploads"]) == 1_000
    assert oversized.status_code == 422


async def test_batch_upload_session_creation_hides_foreign_dataset_and_requires_auth(
    client: httpx.AsyncClient,
    auth_headers,
) -> None:
    dataset_id = await create_dataset(client)
    body = {
        "files": [{
            "filename": "sample.jpg",
            "size": 1,
            "chunk_size": 1,
            "kind": "file",
        }]
    }

    unauthenticated = await client.post(
        f"/api/datasets/{dataset_id}/uploads/batch",
        headers={"Authorization": ""},
        json=body,
    )
    foreign = await client.post(
        f"/api/datasets/{dataset_id}/uploads/batch",
        headers=auth_headers(2),
        json=body,
    )

    assert unauthenticated.status_code == 401
    assert foreign.status_code == 404


async def test_chunk_batch_upload_is_bounded_resumable_and_idempotent(
    client: httpx.AsyncClient,
    app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dataset_id = await create_dataset(client)
    created = await client.post(
        f"/api/datasets/{dataset_id}/uploads/batch",
        json={
            "files": [
                {
                    "filename": "images/a.jpg",
                    "size": 4,
                    "chunk_size": 4,
                    "kind": "file",
                },
                {
                    "filename": "labels/a.txt",
                    "size": 2,
                    "chunk_size": 2,
                    "kind": "file",
                },
            ]
        },
    )
    upload_ids = [item["upload_id"] for item in created.json()["uploads"]]
    metadata = {
        "chunks": [
            {"upload_id": upload_ids[0], "chunk_number": 0, "size": 4},
            {"upload_id": upload_ids[1], "chunk_number": 0, "size": 2},
        ]
    }
    commit_calls = 0
    original_commit = uploads_service.AsyncSession.commit

    async def count_commit(session, *args, **kwargs):
        nonlocal commit_calls
        commit_calls += 1
        return await original_commit(session, *args, **kwargs)

    monkeypatch.setattr(uploads_service.AsyncSession, "commit", count_commit)

    async def send_batch():
        return await client.post(
            f"/api/datasets/{dataset_id}/uploads/chunks/batch",
            data={"metadata": json.dumps(metadata)},
            files=[
                ("chunks", ("first.part", b"abcd", "application/octet-stream")),
                ("chunks", ("second.part", b"xy", "application/octet-stream")),
            ],
        )

    assert (await send_batch()).status_code == 204
    # A lost success response can replay the entire request safely.
    assert (await send_batch()).status_code == 204
    conflicting = await client.post(
        f"/api/datasets/{dataset_id}/uploads/chunks/batch",
        data={"metadata": json.dumps(metadata)},
        files=[
            ("chunks", ("first.part", b"abce", "application/octet-stream")),
            ("chunks", ("second.part", b"xy", "application/octet-stream")),
        ],
    )

    assert conflicting.status_code == 409
    assert commit_calls == 2
    async with app.state.session_factory() as session:
        uploads = [await session.get(UploadSession, upload_id) for upload_id in upload_ids]
        assert all(upload is not None for upload in uploads)
        assert [upload.received_chunks for upload in uploads] == [[0], [0]]
    assert (
        Path(app.state.settings.storage_dir)
        / "uploads"
        / str(upload_ids[0])
        / "chunks"
        / "0.part"
    ).read_bytes() == b"abcd"
    assert (
        Path(app.state.settings.storage_dir)
        / "uploads"
        / str(upload_ids[1])
        / "chunks"
        / "0.part"
    ).read_bytes() == b"xy"


async def test_chunk_batch_rejects_invalid_part_before_publishing_any_chunk(
    client: httpx.AsyncClient,
    app,
) -> None:
    dataset_id = await create_dataset(client)
    created = await client.post(
        f"/api/datasets/{dataset_id}/uploads/batch",
        json={
            "files": [
                {
                    "filename": filename,
                    "size": 4,
                    "chunk_size": 4,
                    "kind": "file",
                }
                for filename in ("a.jpg", "b.jpg")
            ]
        },
    )
    upload_ids = [item["upload_id"] for item in created.json()["uploads"]]
    response = await client.post(
        f"/api/datasets/{dataset_id}/uploads/chunks/batch",
        data={
            "metadata": json.dumps({
                "chunks": [
                    {"upload_id": upload_ids[0], "chunk_number": 0, "size": 4},
                    {"upload_id": upload_ids[1], "chunk_number": 0, "size": 4},
                ]
            })
        },
        files=[
            ("chunks", ("first.part", b"abcd", "application/octet-stream")),
            ("chunks", ("short.part", b"bad", "application/octet-stream")),
        ],
    )

    assert response.status_code == 422
    async with app.state.session_factory() as session:
        uploads = [await session.get(UploadSession, upload_id) for upload_id in upload_ids]
        assert [upload.received_chunks for upload in uploads] == [[], []]
    assert all(
        not (
            Path(app.state.settings.storage_dir)
            / "uploads"
            / str(upload_id)
            / "chunks"
            / "0.part"
        ).exists()
        for upload_id in upload_ids
    )


async def test_chunk_batch_hides_foreign_sessions_and_rejects_oversized_body(
    client: httpx.AsyncClient,
    auth_headers,
) -> None:
    dataset_id = await create_dataset(client)
    foreign = await client.post(
        f"/api/datasets/{dataset_id}/uploads/chunks/batch",
        headers=auth_headers(2),
    )
    unauthenticated = await client.post(
        f"/api/datasets/{dataset_id}/uploads/chunks/batch",
        headers={"Authorization": ""},
    )
    oversized_stream = CountingStream(chunks=10_000, chunk=b"x" * 1024)
    oversized = await client.post(
        f"/api/datasets/{dataset_id}/uploads/chunks/batch",
        content=oversized_stream,
        headers={
            "Content-Length": str(9 * 1024**2),
            "Content-Type": "multipart/form-data; boundary=annodock",
        },
    )

    assert foreign.status_code == 404
    assert unauthenticated.status_code == 401
    assert oversized.status_code == 413
    assert oversized_stream.yielded == 0


@pytest.mark.parametrize(
    ("settings_update", "batch_files", "single_file"),
    [
        (
            {"max_file_count": 1},
            [
                {"filename": "a.jpg", "size": 1, "chunk_size": 1, "kind": "file"},
                {"filename": "b.jpg", "size": 1, "chunk_size": 1, "kind": "file"},
            ],
            {
                "filename": "bundle.zip",
                "size": 2,
                "chunk_size": 1,
                "kind": "zip",
                "file_count": 2,
            },
        ),
        (
            {"quota_bytes_per_user": 1},
            [
                {"filename": "a.jpg", "size": 1, "chunk_size": 1, "kind": "file"},
                {"filename": "b.jpg", "size": 1, "chunk_size": 1, "kind": "file"},
            ],
            {
                "filename": "bundle.zip",
                "size": 2,
                "chunk_size": 1,
                "kind": "zip",
            },
        ),
    ],
)
async def test_batch_upload_session_rejection_matches_single_and_creates_nothing(
    client: httpx.AsyncClient,
    app,
    settings_update: dict[str, int],
    batch_files: list[dict[str, object]],
    single_file: dict[str, object],
) -> None:
    app.state.settings = app.state.settings.model_copy(update=settings_update)
    dataset_id = await create_dataset(client)

    single = await client.post(
        f"/api/datasets/{dataset_id}/uploads",
        json=single_file,
    )
    batch = await client.post(
        f"/api/datasets/{dataset_id}/uploads/batch",
        json={"files": batch_files},
    )

    assert batch.status_code == single.status_code == 413
    assert batch.json() == single.json()
    async with app.state.session_factory() as session:
        count = await session.scalar(
            select(func.count(UploadSession.id)).where(
                UploadSession.dataset_id == dataset_id
            )
        )
    assert count == 0
    uploads_root = Path(app.state.settings.storage_dir) / "uploads"
    assert not uploads_root.exists() or list(uploads_root.iterdir()) == []


async def test_batch_upload_session_directory_failure_aborts_every_created_row(
    client: httpx.AsyncClient,
    app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dataset_id = await create_dataset(client)
    directory_calls = 0

    class ControlledDirectory:
        def __init__(self, path: Path, fail: bool) -> None:
            self.path = path
            self.fail = fail

        def mkdir(self, *, parents: bool, exist_ok: bool) -> None:
            if self.fail:
                raise OSError("simulated batch mkdir failure")
            self.path.mkdir(parents=parents, exist_ok=exist_ok)

    def controlled_directory(settings, upload_id: int) -> ControlledDirectory:
        nonlocal directory_calls
        directory_calls += 1
        return ControlledDirectory(
            Path(settings.storage_dir) / "uploads" / str(upload_id),
            fail=directory_calls == 2,
        )

    monkeypatch.setattr(uploads_router, "upload_directory", controlled_directory)
    with pytest.raises(OSError, match="simulated batch mkdir failure"):
        await client.post(
            f"/api/datasets/{dataset_id}/uploads/batch",
            json={
                "files": [
                    {"filename": "a.jpg", "size": 1, "chunk_size": 1, "kind": "file"},
                    {"filename": "b.jpg", "size": 1, "chunk_size": 1, "kind": "file"},
                    {"filename": "c.jpg", "size": 1, "chunk_size": 1, "kind": "file"},
                ]
            },
        )

    async with app.state.session_factory() as session:
        rows = (
            await session.scalars(
                select(UploadSession)
                .where(UploadSession.dataset_id == dataset_id)
                .order_by(UploadSession.id)
            )
        ).all()
    assert len(rows) == 3
    assert [row.state for row in rows] == ["aborted", "aborted", "aborted"]


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


async def test_batch_completion_survives_asyncpg_parameter_limit(
    client: httpx.AsyncClient,
    app,
) -> None:
    # asyncpg caps a statement at 32767 bind parameters; the complete path
    # must therefore never expand upload ids into per-id parameters.
    session_count = 33_000
    dataset_id = await create_dataset(client)
    async with app.state.session_factory() as session:
        session.add_all(
            UploadSession(
                dataset_id=dataset_id,
                filename=f"images/{index}.jpg",
                size=0,
                chunk_size=1,
                received_chunks=[],
                kind="file",
                state="open",
            )
            for index in range(session_count)
        )
        await session.commit()
        upload_ids = list(
            (
                await session.scalars(
                    select(UploadSession.id).where(
                        UploadSession.dataset_id == dataset_id
                    )
                )
            ).all()
        )
    assert len(upload_ids) == session_count

    completed = await client.post(
        f"/api/datasets/{dataset_id}/upload-batches/complete",
        json={"upload_ids": upload_ids},
    )

    assert completed.status_code == 202
    job_id = completed.json()["job_id"]
    async with app.state.session_factory() as session:
        job = await session.get(UploadJob, job_id)
        assert job is not None
        assert sorted(job.upload_ids) == sorted(upload_ids)
        open_left = await session.scalar(
            select(func.count(UploadSession.id)).where(
                UploadSession.dataset_id == dataset_id,
                UploadSession.state != "complete",
            )
        )
        assert open_left == 0


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
