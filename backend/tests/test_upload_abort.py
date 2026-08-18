from __future__ import annotations

from uuid import uuid4

import httpx
import pytest

from app.models import UploadSession, UserStorage
from app.services.quota import get_bytes_used
from app.services.uploads import upload_directory


pytestmark = pytest.mark.asyncio


async def _dataset(client: httpx.AsyncClient) -> int:
    response = await client.post(
        "/api/datasets",
        json={"name": f"test-upload-abort-{uuid4().hex}"},
    )
    assert response.status_code == 201
    return response.json()["id"]


async def _upload(client: httpx.AsyncClient, dataset_id: int) -> int:
    response = await client.post(
        f"/api/datasets/{dataset_id}/uploads",
        json={
            "filename": "sample.bin",
            "size": 4,
            "chunk_size": 4,
            "kind": "file",
        },
    )
    assert response.status_code == 201
    upload_id = response.json()["upload_id"]
    assert (
        await client.put(f"/api/uploads/{upload_id}/chunks/0", content=b"data")
    ).status_code == 204
    return upload_id


async def test_abort_reclaims_files_rejects_resume_and_keeps_quota(
    client: httpx.AsyncClient,
    app,
) -> None:
    dataset_id = await _dataset(client)
    upload_id = await _upload(client, dataset_id)
    path = upload_directory(app.state.settings, upload_id)
    async with app.state.session_factory() as session:
        session.add(UserStorage(owner_id=1, bytes_used=321))
        await session.commit()

    response = await client.delete(f"/api/uploads/{upload_id}")

    assert response.status_code == 204
    assert not path.exists()
    assert (await client.delete(f"/api/uploads/{upload_id}")).status_code == 204
    assert (
        await client.put(f"/api/uploads/{upload_id}/chunks/0", content=b"data")
    ).status_code == 409
    assert (await client.post(f"/api/uploads/{upload_id}/complete")).status_code == 409
    async with app.state.session_factory() as session:
        upload = await session.get(UploadSession, upload_id)
        assert upload is not None and upload.state == "aborted"
        assert await get_bytes_used(session, 1) == 321


async def test_abort_is_owner_scoped_and_rejects_consumed_upload(
    client: httpx.AsyncClient,
    app,
    auth_headers,
) -> None:
    dataset_id = await _dataset(client)
    upload_id = await _upload(client, dataset_id)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
        headers=auth_headers(2),
    ) as foreign_client:
        assert (
            await foreign_client.delete(f"/api/uploads/{upload_id}")
        ).status_code == 404
    completed = await client.post(f"/api/uploads/{upload_id}/complete")
    assert completed.status_code == 202
    assert (await client.delete(f"/api/uploads/{upload_id}")).status_code == 409

