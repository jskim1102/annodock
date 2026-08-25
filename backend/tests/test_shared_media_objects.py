from __future__ import annotations

import errno
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import func, select

from app.models import (
    Annotation,
    Dataset,
    Image,
    MediaObject,
    UserStorage,
)
from app.services import dataset_merge as dataset_merge_service
from app.services.quota import increase_bytes_used
from app.services.storage import contained_storage_path, storage_relative_path


pytestmark = pytest.mark.asyncio


async def _create_shared_source(
    client: httpx.AsyncClient,
    app,
    *,
    project_id: int,
    suffix: str,
    content: bytes,
) -> tuple[int, int, int]:
    response = await client.post(
        "/api/datasets",
        json={
            "name": f"test-shared-source-{suffix}-{uuid4().hex}",
            "project_id": project_id,
        },
    )
    assert response.status_code == 201, response.text
    dataset_id = int(response.json()["id"])

    async with app.state.session_factory() as session:
        dataset = await session.get(Dataset, dataset_id)
        assert dataset is not None
        root = contained_storage_path(
            app.state.settings.storage_dir,
            dataset.storage_path,
        ) / "fixture"
        root.mkdir(parents=True, exist_ok=True)
        original = root / f"{suffix}.jpg"
        thumbnail = root / f"{suffix}-thumb.jpg"
        original.write_bytes(content)
        thumbnail.write_bytes(b"thumb-" + content)
        physical_bytes = original.stat().st_size + thumbnail.stat().st_size

        media_object = MediaObject(
            owner_id=1,
            created_by_dataset_id=dataset_id,
            original_bytes=original.stat().st_size,
            display_bytes=0,
            thumb_bytes=thumbnail.stat().st_size,
        )
        image = Image(
            dataset_id=dataset_id,
            media_object=media_object,
            stem=suffix,
            filename=original.name,
            rel_path=f"images/train/{original.name}",
            split="train",
            width=64,
            height=32,
            file_path=storage_relative_path(
                app.state.settings.storage_dir,
                original,
            ),
            display_path=None,
            thumb_path=storage_relative_path(
                app.state.settings.storage_dir,
                thumbnail,
            ),
            original_bytes=original.stat().st_size,
            display_bytes=0,
            thumb_bytes=thumbnail.stat().st_size,
            box_count=1,
        )
        image.annotations.append(
            Annotation(
                class_id=0,
                cx=0.5,
                cy=0.5,
                w=0.25,
                h=0.25,
            )
        )
        session.add(image)
        dataset.status = "ready"
        dataset.image_count = 1
        dataset.annotation_count = 1
        dataset.class_count = 1
        await increase_bytes_used(session, 1, physical_bytes)
        await session.commit()
        return dataset_id, media_object.id, physical_bytes


async def _project(client: httpx.AsyncClient) -> int:
    response = await client.post(
        "/api/projects",
        json={
            "name": f"test-shared-project-{uuid4().hex}",
            "classes": [{"name": "person", "color": "#112233"}],
        },
    )
    assert response.status_code == 201, response.text
    return int(response.json()["id"])


async def test_merge_reuses_media_objects_and_releases_only_the_last_reference(
    client: httpx.AsyncClient,
    app,
) -> None:
    project_id = await _project(client)
    first_id, first_object_id, first_bytes = await _create_shared_source(
        client,
        app,
        project_id=project_id,
        suffix="first",
        content=b"first-shared-image",
    )
    second_id, second_object_id, second_bytes = await _create_shared_source(
        client,
        app,
        project_id=project_id,
        suffix="second",
        content=b"second-shared-image",
    )
    physical_bytes = first_bytes + second_bytes

    merged_response = await client.post(
        "/api/datasets/merge",
        json={
            "name": f"test-shared-merged-{uuid4().hex}",
            "dataset_ids": [first_id, second_id],
        },
    )
    assert merged_response.status_code == 201, merged_response.text
    merged_id = int(merged_response.json()["id"])

    async with app.state.session_factory() as session:
        source_images = list(
            (
                await session.scalars(
                    select(Image)
                    .where(Image.dataset_id.in_([first_id, second_id]))
                    .order_by(Image.dataset_id)
                )
            ).all()
        )
        merged_images = list(
            (
                await session.scalars(
                    select(Image)
                    .where(Image.dataset_id == merged_id)
                    .order_by(Image.stem)
                )
            ).all()
        )
        usage = await session.get(UserStorage, 1)

    assert [image.media_object_id for image in source_images] == [
        first_object_id,
        second_object_id,
    ]
    assert [image.media_object_id for image in merged_images] == [
        first_object_id,
        second_object_id,
    ]
    assert usage is not None and usage.bytes_used == physical_bytes
    for source, merged in zip(source_images, merged_images, strict=True):
        source_path = contained_storage_path(
            app.state.settings.storage_dir,
            source.file_path,
        )
        merged_path = contained_storage_path(
            app.state.settings.storage_dir,
            merged.file_path,
        )
        assert source_path.stat().st_ino == merged_path.stat().st_ino

    storage = await client.get("/api/storage")
    assert storage.status_code == 200, storage.text
    assert storage.json()["used_bytes"] == physical_bytes
    assert storage.json()["referenced_bytes"] == physical_bytes * 2

    projects = await client.get(f"/api/projects/{project_id}")
    assert projects.status_code == 200, projects.text
    merged_row = projects.json()["datasets"][0]
    assert merged_row["id"] == merged_id
    assert merged_row["storage_bytes"] == physical_bytes
    assert merged_row["physical_storage_bytes"] == 0
    assert [row["physical_storage_bytes"] for row in merged_row["source_datasets"]] == [
        first_bytes,
        second_bytes,
    ]

    # Annotation rows remain dataset-local even though both image rows share
    # the same immutable media object.
    edited = await client.put(
        f"/api/images/{merged_images[0].id}/annotations",
        json={
            "boxes": [
                {
                    "class_id": 0,
                    "cx": 0.25,
                    "cy": 0.25,
                    "w": 0.1,
                    "h": 0.1,
                }
            ]
        },
    )
    assert edited.status_code == 200, edited.text
    source_annotations = await client.get(
        f"/api/images/{source_images[0].id}/annotations"
    )
    assert source_annotations.status_code == 200
    assert source_annotations.json()["boxes"][0]["cx"] == 0.5

    # Removing the creator dataset only removes one directory entry.  The
    # merged reference keeps the inode, media row, and quota charge alive.
    deleted_first = await client.delete(f"/api/datasets/{first_id}")
    assert deleted_first.status_code == 204
    surviving_file = await client.get(f"/api/images/{merged_images[0].id}/file")
    assert surviving_file.status_code == 200
    async with app.state.session_factory() as session:
        first_object = await session.get(MediaObject, first_object_id)
        usage = await session.get(UserStorage, 1)
        assert first_object is not None
        assert first_object.created_by_dataset_id == merged_id
        assert usage is not None and usage.bytes_used == physical_bytes

    # Deleting the merged dataset releases only the object whose final
    # reference disappeared; the second original remains usable.
    deleted_merged = await client.delete(f"/api/datasets/{merged_id}")
    assert deleted_merged.status_code == 204
    async with app.state.session_factory() as session:
        assert await session.get(MediaObject, first_object_id) is None
        assert await session.get(MediaObject, second_object_id) is not None
        usage = await session.get(UserStorage, 1)
        assert usage is not None and usage.bytes_used == second_bytes
    second_detail = await client.get(f"/api/datasets/{second_id}")
    assert second_detail.status_code == 200

    deleted_second = await client.delete(f"/api/datasets/{second_id}")
    assert deleted_second.status_code == 204
    async with app.state.session_factory() as session:
        assert await session.scalar(select(func.count(MediaObject.id))) == 0
        usage = await session.get(UserStorage, 1)
        assert usage is not None and usage.bytes_used == 0


async def test_cross_device_fallback_creates_and_accounts_a_distinct_object(
    client: httpx.AsyncClient,
    app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id = await _project(client)
    source_id, source_object_id, source_bytes = await _create_shared_source(
        client,
        app,
        project_id=project_id,
        suffix="fallback",
        content=b"fallback-image",
    )
    mate_id, _mate_object_id, mate_bytes = await _create_shared_source(
        client,
        app,
        project_id=project_id,
        suffix="fallback-mate",
        content=b"fallback-mate-image",
    )

    def fail_link(_source: Path, _target: Path) -> None:
        raise OSError(errno.EXDEV, "cross-device link")

    monkeypatch.setattr(dataset_merge_service.os, "link", fail_link)
    response = await client.post(
        "/api/datasets/merge",
        json={
            "name": f"test-shared-copy-{uuid4().hex}",
            "dataset_ids": [source_id, mate_id],
        },
    )
    assert response.status_code == 201, response.text
    merged_id = int(response.json()["id"])

    async with app.state.session_factory() as session:
        copied = await session.scalar(
            select(Image).where(
                Image.dataset_id == merged_id,
                Image.stem == "fallback",
            )
        )
        source = await session.scalar(
            select(Image).where(Image.dataset_id == source_id)
        )
        usage = await session.get(UserStorage, 1)
        assert copied is not None and source is not None
        assert copied.media_object_id != source_object_id
        assert copied.media_object_id != source.media_object_id
        assert usage is not None
        assert usage.bytes_used == (source_bytes + mate_bytes) * 2
        copied_path = contained_storage_path(
            app.state.settings.storage_dir,
            copied.file_path,
        )
        source_path = contained_storage_path(
            app.state.settings.storage_dir,
            source.file_path,
        )
        assert copied_path.stat().st_ino != source_path.stat().st_ino
