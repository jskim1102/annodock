from __future__ import annotations

from collections import namedtuple
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from app.services.validate import RejectedFile, validate_image_file


pytestmark = pytest.mark.asyncio


async def create_dataset(client: httpx.AsyncClient) -> int:
    response = await client.post(
        "/api/datasets",
        json={"name": f"test-validate-{uuid4().hex}"},
    )
    assert response.status_code == 201
    return response.json()["id"]


async def test_extension_and_magic_bytes_must_both_match(
    tmp_path: Path,
) -> None:
    allowed = ("jpg", "jpeg", "png")
    jpeg = tmp_path / "valid.jpg"
    jpeg.write_bytes(b"\xff\xd8\xff\xe0" + b"jpeg-data")
    disguised = tmp_path / "disguised.jpg"
    disguised.write_bytes(b"\x89PNG\r\n\x1a\n" + b"png-data")
    disallowed = tmp_path / "script.exe"
    disallowed.write_bytes(b"MZ")

    assert validate_image_file(jpeg, jpeg.name, allowed) == "jpg"
    with pytest.raises(RejectedFile, match="signature"):
        validate_image_file(disguised, disguised.name, allowed)
    with pytest.raises(RejectedFile, match="extension"):
        validate_image_file(disallowed, disallowed.name, allowed)


async def test_extension_comes_from_logical_path_not_assembled_file(
    tmp_path: Path,
) -> None:
    assembled = tmp_path / "source"
    assembled.write_bytes(b"\xff\xd8\xff\xe0" + b"jpeg-data")

    assert (
        validate_image_file(
            assembled,
            "images/train/example.jpg",
            ("jpg", "png"),
        )
        == "jpg"
    )


async def test_session_creation_enforces_size_and_file_count_limits(
    client: httpx.AsyncClient,
    app,
) -> None:
    dataset_id = await create_dataset(client)
    too_large = await client.post(
        f"/api/datasets/{dataset_id}/uploads",
        json={
            "filename": "huge.zip",
            "size": app.state.settings.max_zip_bytes + 1,
            "chunk_size": 1024,
            "kind": "zip",
        },
    )
    too_many = await client.post(
        f"/api/datasets/{dataset_id}/uploads",
        json={
            "filename": "folder",
            "size": 1024,
            "chunk_size": 1024,
            "kind": "folder",
            "file_count": app.state.settings.max_file_count + 1,
        },
    )

    assert too_large.status_code == 413
    assert too_many.status_code == 413


async def test_session_creation_rejects_insufficient_disk_before_writing(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dataset_id = await create_dataset(client)
    DiskUsage = namedtuple("usage", "total used free")
    monkeypatch.setattr(
        "app.services.validate.shutil.disk_usage",
        lambda _path: DiskUsage(total=10_000, used=9_000, free=1_000),
    )

    response = await client.post(
        f"/api/datasets/{dataset_id}/uploads",
        json={
            "filename": "dataset.zip",
            "size": 500,
            "chunk_size": 100,
            "kind": "zip",
            "expected_extracted_size": 1_000,
        },
    )

    assert response.status_code == 507
    detail = response.json()["detail"]
    assert detail["required_bytes"] == 1_200
    assert detail["available_bytes"] == 1_000


async def test_batch_preflight_checks_total_size_and_file_count(
    client: httpx.AsyncClient,
    app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dataset_id = await create_dataset(client)
    DiskUsage = namedtuple("usage", "total used free")
    monkeypatch.setattr(
        "app.services.validate.shutil.disk_usage",
        lambda _path: DiskUsage(total=10_000, used=9_000, free=1_000),
    )

    insufficient = await client.post(
        f"/api/datasets/{dataset_id}/upload-batches/preflight",
        json={
            "total_size": 900,
            "largest_file_size": 500,
            "file_count": 2,
            "expected_extracted_size": 1_000,
        },
    )
    too_many = await client.post(
        f"/api/datasets/{dataset_id}/upload-batches/preflight",
        json={
            "total_size": 1,
            "largest_file_size": 1,
            "file_count": app.state.settings.max_file_count + 1,
            "expected_extracted_size": 1,
        },
    )
    total_size_only = await client.post(
        f"/api/datasets/{dataset_id}/upload-batches/preflight",
        json={
            "total_size": 900,
            "largest_file_size": 500,
            "file_count": 2,
            "expected_extracted_size": 0,
        },
    )

    assert insufficient.status_code == 507
    assert insufficient.json()["detail"] == {
        "message": "디스크 여유가 부족합니다.",
        "required_bytes": 1_200,
        "available_bytes": 1_000,
    }
    assert too_many.status_code == 413
    assert total_size_only.status_code == 507
    assert total_size_only.json()["detail"]["required_bytes"] == 1_080


async def test_zero_expected_extracted_size_is_not_replaced_by_upload_size(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dataset_id = await create_dataset(client)
    DiskUsage = namedtuple("usage", "total used free")
    monkeypatch.setattr(
        "app.services.validate.shutil.disk_usage",
        lambda _path: DiskUsage(total=1, used=1, free=0),
    )

    response = await client.post(
        f"/api/datasets/{dataset_id}/uploads",
        json={
            "filename": "empty.zip",
            "size": 1,
            "chunk_size": 1,
            "kind": "zip",
            "expected_extracted_size": 0,
        },
    )

    assert response.status_code == 201


@pytest.mark.parametrize(
    "filename",
    ["x\x00.jpg", "a\x00b.jpg", "ok\x00.txt"],
)
async def test_upload_filename_rejects_null_bytes_with_422(
    client: httpx.AsyncClient,
    filename: str,
) -> None:
    dataset_id = await create_dataset(client)

    response = await client.post(
        f"/api/datasets/{dataset_id}/uploads",
        json={
            "filename": filename,
            "size": 1,
            "chunk_size": 1,
            "kind": "file",
        },
    )

    assert response.status_code == 422
    assert (await client.get("/api/health")).status_code == 200
