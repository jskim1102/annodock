from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import func, select

from app.models import Annotation, Dataset, DatasetClass, Image
from app.services.storage import contained_storage_path
from tests.factories import image_with_media


pytestmark = pytest.mark.asyncio


async def create_image_with_annotation(
    client: httpx.AsyncClient,
    app,
    tmp_path: Path,
) -> tuple[int, int, Path]:
    response = await client.post(
        "/api/datasets",
        json={"name": f"test-annotations-{uuid4().hex}"},
    )
    assert response.status_code == 201
    dataset_id = response.json()["id"]
    original_label = tmp_path / "original.txt"
    original_label.write_text(
        "0 0.500000 0.500000 0.200000 0.200000\n",
        encoding="utf-8",
    )

    async with app.state.session_factory() as session:
        dataset = await session.get(Dataset, dataset_id)
        assert dataset is not None
        storage = contained_storage_path(
            app.state.settings.storage_dir,
            dataset.storage_path,
        )
        image_path = storage / "original.jpg"
        thumb_path = storage / "thumb.jpg"
        image_path.write_bytes(b"image")
        thumb_path.write_bytes(b"thumb")
        image = image_with_media(
            owner_id=dataset.owner_id,
            dataset_id=dataset_id,
            stem="sample",
            filename="sample.jpg",
            rel_path="images/sample.jpg",
            split=None,
            width=800,
            height=600,
            file_path=str(image_path),
            display_path=None,
            thumb_path=str(thumb_path),
            box_count=1,
            is_modified=False,
        )
        session.add(image)
        await session.flush()
        session.add(
            Annotation(
                image_id=image.id,
                class_id=0,
                cx=0.5,
                cy=0.5,
                w=0.2,
                h=0.2,
            )
        )
        dataset.image_count = 1
        dataset.annotation_count = 1
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
            ]
        )
        dataset.class_count = 2
        await session.commit()
        return dataset_id, image.id, original_label


async def test_get_and_put_annotations_replace_boxes_and_update_counts(
    client: httpx.AsyncClient,
    app,
    tmp_path: Path,
) -> None:
    dataset_id, image_id, original_label = (
        await create_image_with_annotation(client, app, tmp_path)
    )
    original_text = original_label.read_text(encoding="utf-8")
    original_mtime = original_label.stat().st_mtime_ns

    fetched = await client.get(f"/api/images/{image_id}/annotations")
    replaced = await client.put(
        f"/api/images/{image_id}/annotations",
        json={
            "boxes": [
                {
                    "class_id": 1,
                    "cx": 0.25,
                    "cy": 0.3,
                    "w": 0.1,
                    "h": 0.2,
                },
                {
                    "class_id": 0,
                    "cx": 0.75,
                    "cy": 0.7,
                    "w": 0.2,
                    "h": 0.1,
                },
            ]
        },
    )

    assert fetched.status_code == 200
    assert fetched.json()["image_id"] == image_id
    assert (fetched.json()["width"], fetched.json()["height"]) == (800, 600)
    assert len(fetched.json()["boxes"]) == 1
    assert replaced.status_code == 200
    assert replaced.json()["image_id"] == image_id
    assert replaced.json()["is_modified"] is True
    assert [
        box["class_id"] for box in replaced.json()["boxes"]
    ] == [1, 0]
    assert all(box["id"] > 0 for box in replaced.json()["boxes"])

    async with app.state.session_factory() as session:
        image = await session.get(Image, image_id)
        dataset = await session.get(Dataset, dataset_id)
        rows = (
            await session.scalars(
                select(Annotation)
                .where(Annotation.image_id == image_id)
                .order_by(Annotation.id)
            )
        ).all()
        assert image is not None
        assert dataset is not None
        assert (image.box_count, image.is_modified) == (2, True)
        assert dataset.annotation_count == 2
        assert [row.class_id for row in rows] == [1, 0]

    assert original_label.read_text(encoding="utf-8") == original_text
    assert original_label.stat().st_mtime_ns == original_mtime

    cleared = await client.put(
        f"/api/images/{image_id}/annotations",
        json={"boxes": []},
    )
    assert cleared.status_code == 200
    assert cleared.json()["boxes"] == []
    async with app.state.session_factory() as session:
        image = await session.get(Image, image_id)
        dataset = await session.get(Dataset, dataset_id)
        count = await session.scalar(
            select(func.count(Annotation.id)).where(
                Annotation.image_id == image_id
            )
        )
        assert image is not None
        assert dataset is not None
        assert image.box_count == 0
        assert dataset.annotation_count == 0
        assert count == 0


@pytest.mark.parametrize(
    "box",
    [
        {"class_id": -1, "cx": 0.5, "cy": 0.5, "w": 0.2, "h": 0.2},
        {"class_id": 0, "cx": -0.1, "cy": 0.5, "w": 0.2, "h": 0.2},
        {"class_id": 0, "cx": 0.5, "cy": 1.1, "w": 0.2, "h": 0.2},
        {"class_id": 0, "cx": 0.5, "cy": 0.5, "w": -0.1, "h": 0.2},
        {"class_id": 0, "cx": 0.5, "cy": 0.5, "w": 0.2, "h": 1.1},
    ],
)
async def test_put_rejects_invalid_box_values_without_mutation(
    client: httpx.AsyncClient,
    app,
    tmp_path: Path,
    box: dict[str, float | int],
) -> None:
    dataset_id, image_id, _original_label = (
        await create_image_with_annotation(client, app, tmp_path)
    )

    response = await client.put(
        f"/api/images/{image_id}/annotations",
        json={"boxes": [box]},
    )

    assert response.status_code == 422
    async with app.state.session_factory() as session:
        image = await session.get(Image, image_id)
        dataset = await session.get(Dataset, dataset_id)
        annotation = await session.scalar(
            select(Annotation).where(Annotation.image_id == image_id)
        )
        assert image is not None
        assert dataset is not None
        assert image.box_count == 1
        assert image.is_modified is False
        assert dataset.annotation_count == 1
        assert annotation is not None
        assert annotation.class_id == 0


async def test_classes_are_sorted_and_missing_images_return_404(
    client: httpx.AsyncClient,
    app,
    tmp_path: Path,
) -> None:
    dataset_id, _image_id, _original_label = (
        await create_image_with_annotation(client, app, tmp_path)
    )

    classes = await client.get(f"/api/datasets/{dataset_id}/classes")

    assert classes.status_code == 200
    assert classes.json() == {
        "classes": [
            {"class_id": 0, "name": "person"},
            {"class_id": 1, "name": "vehicle"},
        ]
    }
    assert (
        await client.get("/api/images/999999999/annotations")
    ).status_code == 404
    assert (
        await client.put(
            "/api/images/999999999/annotations",
            json={"boxes": []},
        )
    ).status_code == 404
