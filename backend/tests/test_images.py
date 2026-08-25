from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select

from app.models import Dataset, Image
from app.services.storage import contained_storage_path
from tests.factories import image_with_media


pytestmark = pytest.mark.asyncio


async def create_dataset(
    client: httpx.AsyncClient,
    app,
) -> tuple[int, Path]:
    response = await client.post(
        "/api/datasets",
        json={"name": f"test-images-{uuid4().hex}"},
    )
    assert response.status_code == 201
    dataset_id = response.json()["id"]
    async with app.state.session_factory() as session:
        dataset = await session.get(Dataset, dataset_id)
        assert dataset is not None
        return dataset_id, contained_storage_path(
            app.state.settings.storage_dir,
            dataset.storage_path,
        )


async def add_image(
    app,
    dataset_id: int,
    storage_path: Path,
    *,
    stem: str,
    filename: str,
    split: str | None,
    box_count: int = 0,
    is_modified: bool = False,
    original: bytes = b"original",
    display: bytes | None = None,
) -> int:
    image_dir = storage_path / "api-test" / f"{split or 'flat'}-{stem}"
    image_dir.mkdir(parents=True, exist_ok=True)
    original_path = image_dir / filename
    thumb_path = image_dir / "thumb.jpg"
    original_path.write_bytes(original)
    thumb_path.write_bytes(b"thumbnail")
    display_path: Path | None = None
    if display is not None:
        display_path = image_dir / "display.jpg"
        display_path.write_bytes(display)

    async with app.state.session_factory() as session:
        dataset = await session.get(Dataset, dataset_id)
        assert dataset is not None
        image = image_with_media(
            owner_id=dataset.owner_id,
            dataset_id=dataset_id,
            stem=stem,
            filename=filename,
            rel_path=f"images/{split or 'flat'}/{filename}",
            split=split,
            width=640,
            height=480,
            file_path=str(original_path),
            display_path=str(display_path) if display_path else None,
            thumb_path=str(thumb_path),
            box_count=box_count,
            is_modified=is_modified,
        )
        session.add(image)
        await session.commit()
        await session.refresh(image)
        return image.id


async def test_image_list_is_stable_paginated_and_split_filtered(
    client: httpx.AsyncClient,
    app,
) -> None:
    dataset_id, storage_path = await create_dataset(client, app)
    await add_image(
        app,
        dataset_id,
        storage_path,
        stem="b",
        filename="b.jpg",
        split="train",
        box_count=2,
    )
    train_a = await add_image(
        app,
        dataset_id,
        storage_path,
        stem="a",
        filename="a-train.jpg",
        split="train",
        box_count=1,
        is_modified=True,
    )
    val_a = await add_image(
        app,
        dataset_id,
        storage_path,
        stem="a",
        filename="a-val.jpg",
        split="val",
        box_count=3,
    )

    first_page = await client.get(
        f"/api/datasets/{dataset_id}/images?offset=0&limit=2"
    )
    last_page = await client.get(
        f"/api/datasets/{dataset_id}/images?offset=2&limit=2"
    )
    train_only = await client.get(
        f"/api/datasets/{dataset_id}/images"
        "?offset=0&limit=200&split=train"
    )

    assert first_page.status_code == 200
    assert first_page.json()["total"] == 3
    assert [
        (item["stem"], item["split"])
        for item in first_page.json()["items"]
    ] == [("a", "train"), ("a", "val")]
    assert first_page.json()["items"][0] == {
        "id": train_a,
        "stem": "a",
        "filename": "a-train.jpg",
        "split": "train",
        "width": 640,
        "height": 480,
        "box_count": 1,
        "is_modified": True,
    }
    assert last_page.status_code == 200
    assert [
        (item["stem"], item["split"])
        for item in last_page.json()["items"]
    ] == [("b", "train")]
    assert train_only.status_code == 200
    assert train_only.json()["total"] == 2
    assert [item["id"] for item in train_only.json()["items"]] == [
        train_a,
        next(
            item["id"]
            for item in last_page.json()["items"]
            if item["stem"] == "b"
        ),
    ]
    assert val_a not in {
        item["id"] for item in train_only.json()["items"]
    }

    detail = await client.get(f"/api/datasets/{dataset_id}")
    assert detail.json()["splits"] == [
        {"split": "train", "image_count": 2},
        {"split": "val", "image_count": 1},
    ]


async def test_file_prefers_display_derivative_and_thumb_is_cacheable(
    client: httpx.AsyncClient,
    app,
) -> None:
    dataset_id, storage_path = await create_dataset(client, app)
    image_id = await add_image(
        app,
        dataset_id,
        storage_path,
        stem="derived",
        filename="derived.heic",
        split=None,
        original=b"heic-original",
        display=b"jpeg-display",
    )

    displayed = await client.get(f"/api/images/{image_id}/file")
    thumbnail = await client.get(f"/api/images/{image_id}/thumb")

    assert displayed.status_code == 200
    assert displayed.content == b"jpeg-display"
    assert displayed.headers["content-type"].startswith("image/jpeg")
    assert thumbnail.status_code == 200
    assert thumbnail.content == b"thumbnail"
    assert thumbnail.headers["content-type"].startswith("image/jpeg")
    assert thumbnail.headers["cache-control"] == (
        "public, max-age=31536000, immutable"
    )


async def test_web_safe_original_is_streamed_with_its_content_type(
    client: httpx.AsyncClient,
    app,
) -> None:
    dataset_id, storage_path = await create_dataset(client, app)
    image_id = await add_image(
        app,
        dataset_id,
        storage_path,
        stem="original",
        filename="original.png",
        split=None,
        original=b"png-original",
    )

    response = await client.get(f"/api/images/{image_id}/file")

    assert response.status_code == 200
    assert response.content == b"png-original"
    assert response.headers["content-type"].startswith("image/png")


async def test_missing_images_and_paths_outside_storage_return_404(
    client: httpx.AsyncClient,
    app,
    tmp_path: Path,
) -> None:
    assert (await client.get("/api/images/999999999/file")).status_code == 404
    assert (await client.get("/api/images/999999999/thumb")).status_code == 404
    assert (
        await client.get("/api/datasets/999999999/images")
    ).status_code == 404

    dataset_id, storage_path = await create_dataset(client, app)
    image_id = await add_image(
        app,
        dataset_id,
        storage_path,
        stem="escaped",
        filename="escaped.jpg",
        split=None,
    )
    outside = tmp_path / "outside-secret.jpg"
    outside.write_bytes(b"outside")
    async with app.state.session_factory() as session:
        image = await session.scalar(select(Image).where(Image.id == image_id))
        assert image is not None
        image.file_path = str(outside)
        image.thumb_path = str(outside)
        await session.commit()

    assert (await client.get(f"/api/images/{image_id}/file")).status_code == 404
    assert (await client.get(f"/api/images/{image_id}/thumb")).status_code == 404
