from __future__ import annotations

import re
from collections import namedtuple
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import func, select

from app.models import (
    Annotation,
    Dataset,
    DatasetClass,
    Image,
    TrainingRun,
    UploadJob,
    UserStorage,
)
from app.services import training
from app.services.storage import contained_storage_path, storage_relative_path


pytestmark = pytest.mark.asyncio

QUOTA_BYTES = 1_000
EXHAUSTED_OWNER = 51_002
ALLOWED_OWNER = 51_003


def _quota_settings(app, *, limit: int = QUOTA_BYTES) -> None:
    app.state.settings = app.state.settings.model_copy(
        update={"quota_bytes_per_user": limit}
    )


async def _set_usage(app, owner_id: int, bytes_used: int) -> None:
    async with app.state.session_factory() as session:
        storage = await session.get(UserStorage, owner_id)
        if storage is None:
            session.add(UserStorage(owner_id=owner_id, bytes_used=bytes_used))
        else:
            storage.bytes_used = bytes_used
        await session.commit()


async def _create_dataset(
    client: httpx.AsyncClient,
    app,
    auth_headers,
    owner_id: int,
    *,
    ready_images: int = 0,
) -> int:
    response = await client.post(
        "/api/datasets",
        headers=auth_headers(owner_id),
        json={"name": f"test-quota-reject-{owner_id}-{uuid4().hex}"},
    )
    assert response.status_code == 201, response.text
    dataset_id = int(response.json()["id"])
    if ready_images == 0:
        return dataset_id

    async with app.state.session_factory() as session:
        dataset = await session.get(Dataset, dataset_id)
        assert dataset is not None
        dataset_root = contained_storage_path(
            app.state.settings.storage_dir,
            dataset.storage_path,
        )
        dataset_root.mkdir(parents=True, exist_ok=True)
        session.add(
            DatasetClass(dataset_id=dataset_id, class_id=0, name="object")
        )
        for index in range(ready_images):
            source = dataset_root / f"quota-{index}.jpg"
            source.write_bytes(f"quota-image-{index}".encode())
            image = Image(
                dataset_id=dataset_id,
                stem=f"quota-{index}",
                filename=source.name,
                rel_path=f"images/{source.name}",
                split=None,
                width=32,
                height=24,
                file_path=storage_relative_path(
                    app.state.settings.storage_dir,
                    source,
                ),
                display_path=None,
                thumb_path=storage_relative_path(
                    app.state.settings.storage_dir,
                    dataset_root / f"quota-{index}.thumb.jpg",
                ),
                original_bytes=source.stat().st_size,
                box_count=1,
            )
            session.add(image)
            await session.flush()
            session.add(
                Annotation(
                    image_id=image.id,
                    class_id=0,
                    cx=0.5,
                    cy=0.5,
                    w=0.25,
                    h=0.25,
                )
            )
        dataset.status = "ready"
        dataset.image_count = ready_images
        dataset.annotation_count = ready_images
        dataset.class_count = 1
        await session.commit()
    return dataset_id


def _assert_quota_rejection(response: httpx.Response) -> None:
    assert response.status_code == 413, response.text
    detail = response.json()["detail"]
    assert isinstance(detail, str)
    assert "잔여" in detail
    assert "필요" in detail
    assert re.search(r"잔여(?:\s*용량)?[^\d]*0(?:\s*(?:B|바이트))?", detail)
    assert re.search(
        r"필요(?:\s*용량)?[^\d]*[1-9][\d,]*(?:\s*(?:B|바이트))?",
        detail,
    )


def _ample_disk(monkeypatch: pytest.MonkeyPatch) -> None:
    DiskUsage = namedtuple("usage", "total used free")
    monkeypatch.setattr(
        "app.services.validate.shutil.disk_usage",
        lambda _path: DiskUsage(total=10**15, used=0, free=10**15),
    )


def _host_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(training, "is_container_environment", lambda: False)
    monkeypatch.setattr(training.torch.cuda, "is_available", lambda: True)
    monkeypatch.setattr(
        training.torch.cuda,
        "mem_get_info",
        lambda: (24 * 1024**3, 24 * 1024**3),
    )
    monkeypatch.setattr(
        training,
        "spawn_worker",
        lambda _run_id, _owner_id, _out_dir, _database_url: training.SpawnedWorker(
            pid=4242,
            pid_started_at="123456",
            boot_id="test-boot-id",
        ),
    )


def _training_body() -> dict[str, int | str]:
    return {
        "weights": "yolo26n.pt",
        "epochs": 3,
        "imgsz": 640,
        "batch": -1,
    }


async def test_upload_preflight_rejects_exhausted_user_quota(
    client: httpx.AsyncClient,
    app,
    auth_headers,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _quota_settings(app)
    _ample_disk(monkeypatch)
    await _set_usage(app, EXHAUSTED_OWNER, QUOTA_BYTES)
    dataset_id = await _create_dataset(
        client, app, auth_headers, EXHAUSTED_OWNER
    )

    response = await client.post(
        f"/api/datasets/{dataset_id}/upload-batches/preflight",
        headers=auth_headers(EXHAUSTED_OWNER),
        json={
            "total_size": 200,
            "largest_file_size": 100,
            "file_count": 2,
            "expected_extracted_size": 250,
        },
    )

    _assert_quota_rejection(response)


async def test_upload_preflight_allows_capacity_and_retains_disk_507(
    client: httpx.AsyncClient,
    app,
    auth_headers,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _quota_settings(app)
    await _set_usage(app, ALLOWED_OWNER, 0)
    dataset_id = await _create_dataset(client, app, auth_headers, ALLOWED_OWNER)
    _ample_disk(monkeypatch)

    allowed = await client.post(
        f"/api/datasets/{dataset_id}/upload-batches/preflight",
        headers=auth_headers(ALLOWED_OWNER),
        json={
            "total_size": 200,
            "largest_file_size": 100,
            "file_count": 2,
            "expected_extracted_size": 250,
        },
    )
    assert allowed.status_code == 204, allowed.text

    DiskUsage = namedtuple("usage", "total used free")
    monkeypatch.setattr(
        "app.services.validate.shutil.disk_usage",
        lambda _path: DiskUsage(total=10_000, used=9_900, free=100),
    )
    insufficient_disk = await client.post(
        f"/api/datasets/{dataset_id}/upload-batches/preflight",
        headers=auth_headers(ALLOWED_OWNER),
        json={
            "total_size": 200,
            "largest_file_size": 100,
            "file_count": 2,
            "expected_extracted_size": 250,
        },
    )

    assert insufficient_disk.status_code == 507, insufficient_disk.text
    assert insufficient_disk.json()["detail"] == {
        "message": "디스크 여유가 부족합니다.",
        "required_bytes": 300,
        "available_bytes": 100,
    }


async def test_training_quota_guard_follows_container_guard_and_precedes_cuda(
    client: httpx.AsyncClient,
    app,
    auth_headers,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _quota_settings(app)
    await _set_usage(app, EXHAUSTED_OWNER, QUOTA_BYTES)
    dataset_id = await _create_dataset(
        client,
        app,
        auth_headers,
        EXHAUSTED_OWNER,
        ready_images=10,
    )
    monkeypatch.setattr(training.torch.cuda, "is_available", lambda: False)

    monkeypatch.setattr(training, "is_container_environment", lambda: True)
    container = await client.post(
        f"/api/datasets/{dataset_id}/train",
        headers=auth_headers(EXHAUSTED_OWNER),
        json=_training_body(),
    )
    assert container.status_code == 503, container.text
    assert container.json()["detail"] == training.CONTAINER_DETAIL

    monkeypatch.setattr(training, "is_container_environment", lambda: False)
    exhausted = await client.post(
        f"/api/datasets/{dataset_id}/train",
        headers=auth_headers(EXHAUSTED_OWNER),
        json=_training_body(),
    )

    _assert_quota_rejection(exhausted)
    async with app.state.session_factory() as session:
        run_count = await session.scalar(
            select(func.count(TrainingRun.id)).where(
                TrainingRun.dataset_id == dataset_id
            )
        )
    assert run_count == 0


async def test_training_submission_allows_user_with_remaining_quota(
    client: httpx.AsyncClient,
    app,
    auth_headers,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _quota_settings(app, limit=10**12)
    await _set_usage(app, ALLOWED_OWNER, 0)
    dataset_id = await _create_dataset(
        client,
        app,
        auth_headers,
        ALLOWED_OWNER,
        ready_images=10,
    )
    _host_ready(monkeypatch)

    response = await client.post(
        f"/api/datasets/{dataset_id}/train",
        headers=auth_headers(ALLOWED_OWNER),
        json=_training_body(),
    )

    assert response.status_code == 201, response.text
    assert isinstance(response.json()["run_id"], int)
