from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from app.models import Dataset, DatasetClass, Image, UploadJob
from app.services.storage import contained_storage_path


pytestmark = pytest.mark.asyncio


def dataset_name(suffix: str) -> str:
    return f"test-{suffix}-{uuid4().hex}"


async def test_dataset_crud_uses_precomputed_counts_and_removes_storage(
    client: httpx.AsyncClient,
    app,
) -> None:
    original_name = dataset_name("crud")
    created = await client.post("/api/datasets", json={"name": original_name})

    assert created.status_code == 201
    created_body = created.json()
    assert created_body["name"] == original_name
    assert created_body["status"] == "pending"
    dataset_id = created_body["id"]

    async with app.state.session_factory() as session:
        dataset_row = await session.get(Dataset, dataset_id)
        assert dataset_row is not None
        storage_path = contained_storage_path(
            app.state.settings.storage_dir,
            dataset_row.storage_path,
        )
        assert storage_path.is_dir()
        dataset_row.image_count = 2
        dataset_row.annotation_count = 7
        dataset_row.class_count = 3
        session.add_all(
            [
                DatasetClass(
                    dataset_id=dataset_id,
                    class_id=1,
                    name="vehicle",
                ),
                DatasetClass(
                    dataset_id=dataset_id,
                    class_id=0,
                    name="person",
                ),
                Image(
                    dataset_id=dataset_id,
                    stem="train-a",
                    filename="train-a.jpg",
                    rel_path="images/train/train-a.jpg",
                    split="train",
                    width=10,
                    height=10,
                    file_path=str(storage_path / "train-a.jpg"),
                    display_path=None,
                    thumb_path=str(storage_path / "train-a-thumb.jpg"),
                    box_count=4,
                ),
                Image(
                    dataset_id=dataset_id,
                    stem="val-a",
                    filename="val-a.jpg",
                    rel_path="images/val/val-a.jpg",
                    split="val",
                    width=10,
                    height=10,
                    file_path=str(storage_path / "val-a.jpg"),
                    display_path=None,
                    thumb_path=str(storage_path / "val-a-thumb.jpg"),
                    box_count=3,
                ),
            ]
        )
        await session.commit()

    listing = await client.get("/api/datasets?offset=0&limit=50")
    assert listing.status_code == 200
    listed = next(
        row for row in listing.json()["items"] if row["id"] == dataset_id
    )
    assert listed["image_count"] == 2
    assert listed["annotation_count"] == 7
    assert listed["class_count"] == 3

    detail = await client.get(f"/api/datasets/{dataset_id}")
    assert detail.status_code == 200
    assert detail.json()["splits"] == [
        {"split": "train", "image_count": 1},
        {"split": "val", "image_count": 1},
    ]
    classes = await client.get(f"/api/datasets/{dataset_id}/classes")
    assert classes.status_code == 200
    assert classes.json() == {
        "classes": [
            {"class_id": 0, "name": "person"},
            {"class_id": 1, "name": "vehicle"},
        ]
    }

    renamed = dataset_name("renamed")
    patched = await client.patch(
        f"/api/datasets/{dataset_id}",
        json={"name": renamed},
    )
    assert patched.status_code == 200
    assert patched.json() == {"id": dataset_id, "name": renamed}

    deleted = await client.delete(f"/api/datasets/{dataset_id}")
    assert deleted.status_code == 204
    assert not storage_path.exists()
    assert (await client.get(f"/api/datasets/{dataset_id}")).status_code == 404


async def test_duplicate_names_return_409_and_missing_ids_return_404(
    client: httpx.AsyncClient,
) -> None:
    name = dataset_name("duplicate")
    first = await client.post("/api/datasets", json={"name": name})
    second = await client.post("/api/datasets", json={"name": name})

    assert first.status_code == 201
    assert second.status_code == 409
    assert second.json()["detail"] == "이미 있는 이름입니다."
    assert (await client.get("/api/datasets/999999999")).status_code == 404
    assert (
        await client.patch(
            "/api/datasets/999999999",
            json={"name": dataset_name("missing")},
        )
    ).status_code == 404
    assert (await client.delete("/api/datasets/999999999")).status_code == 404

    await client.delete(f"/api/datasets/{first.json()['id']}")


async def test_dataset_name_rejects_blank_values(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post("/api/datasets", json={"name": "   "})
    assert response.status_code == 422


async def test_dataset_name_rejects_null_bytes(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post(
        "/api/datasets",
        json={"name": "bad\x00name"},
    )
    assert response.status_code == 422


async def test_dataset_list_returns_latest_active_job_or_null(
    client: httpx.AsyncClient,
    app,
) -> None:
    active_response = await client.post(
        "/api/datasets",
        json={"name": dataset_name("active-job")},
    )
    inactive_response = await client.post(
        "/api/datasets",
        json={"name": dataset_name("no-active-job")},
    )
    active_id = active_response.json()["id"]
    inactive_id = inactive_response.json()["id"]
    async with app.state.session_factory() as session:
        active_dataset = await session.get(Dataset, active_id)
        assert active_dataset is not None
        active_dataset.status = "ready"
        jobs = [
            UploadJob(
                dataset_id=active_id,
                kind="file",
                state="queued",
                phase="uploading",
                total=10,
                processed=0,
                failed=0,
            ),
            UploadJob(
                dataset_id=active_id,
                kind="file",
                state="running",
                phase="deriving",
                total=10,
                processed=6,
                failed=1,
            ),
            UploadJob(
                dataset_id=active_id,
                kind="file",
                state="done",
                phase="done",
                total=10,
                processed=10,
                failed=1,
            ),
        ]
        session.add_all(jobs)
        await session.commit()
        latest_active_id = jobs[1].id

    listing = await client.get("/api/datasets?offset=0&limit=200")

    assert listing.status_code == 200
    rows = {row["id"]: row for row in listing.json()["items"]}
    assert rows[active_id]["status"] == "ready"
    assert rows[active_id]["active_job"] == {
        "job_id": latest_active_id,
        "state": "running",
        "phase": "deriving",
        "total": 10,
        "processed": 6,
        "failed": 1,
    }
    assert rows[inactive_id]["active_job"] is None
