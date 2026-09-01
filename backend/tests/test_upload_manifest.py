from __future__ import annotations

import asyncio
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import func, select

from app.models import UploadJob, UploadSession
from app.services import uploads as uploads_service


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
