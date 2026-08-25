from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from PIL import Image as PillowImage
from sqlalchemy import select

from app.models import Dataset, Image, UploadJob
from app.services.collect import CollectedFile
from app.services.dataset_partition import (
    balanced_image_partition_sizes,
    dataset_partition_name,
)
from app.services.ingest import ingest_collected
from app.services.storage import contained_storage_path


def _make_jpeg(path: Path, color: tuple[int, int, int]) -> None:
    PillowImage.new("RGB", (32, 24), color).save(path, "JPEG")


def _collected_image(path: Path, index: int) -> CollectedFile:
    return CollectedFile(
        rel_path=f"images/frame-{index:02d}.jpg",
        abs_path=path,
        kind="image",
        split=None,
    )


def _collected_label(path: Path, index: int) -> CollectedFile:
    return CollectedFile(
        rel_path=f"labels/frame-{index:02d}.txt",
        abs_path=path,
        kind="label",
        split=None,
    )


async def _draft_dataset_and_job(
    client: httpx.AsyncClient,
    app,
    name: str,
) -> tuple[int, int]:
    project = await client.post(
        "/api/projects",
        json={"name": f"test-partition-project-{uuid4().hex}", "classes": []},
    )
    assert project.status_code == 201
    dataset = await client.post(
        "/api/datasets",
        json={
            "name": name,
            "project_id": project.json()["id"],
            "upload_draft": True,
        },
    )
    assert dataset.status_code == 201
    dataset_id = dataset.json()["id"]
    async with app.state.session_factory() as session:
        job = UploadJob(
            dataset_id=dataset_id,
            kind="folder",
            state="queued",
            phase="uploading",
            total=0,
            processed=0,
            failed=0,
        )
        session.add(job)
        await session.commit()
        await session.refresh(job)
        return dataset_id, job.id


def test_balanced_partition_contract() -> None:
    assert balanced_image_partition_sizes(0, 5_000) == ()
    assert balanced_image_partition_sizes(5_000, 5_000) == (5_000,)
    assert balanced_image_partition_sizes(5_001, 5_000) == (2_501, 2_500)
    assert balanced_image_partition_sizes(20_000, 5_000) == (5_000,) * 4
    assert balanced_image_partition_sizes(20_001, 5_000) == (
        4_001,
        4_000,
        4_000,
        4_000,
        4_000,
    )


def test_partition_names_keep_the_suffix_inside_the_name_limit() -> None:
    assert dataset_partition_name("sample", 1) == "sample_(1)"
    assert dataset_partition_name("sample", 12) == "sample_(12)"
    assert len(dataset_partition_name("가" * 255, 12)) == 255


@pytest.mark.asyncio
async def test_ingest_distributes_images_and_labels_into_balanced_parts(
    client: httpx.AsyncClient,
    app,
    tmp_path: Path,
) -> None:
    base_name = f"test-partition-{uuid4().hex}"
    dataset_id, job_id = await _draft_dataset_and_job(
        client,
        app,
        base_name,
    )
    items: list[CollectedFile] = []
    for index in range(4):
        path = tmp_path / f"frame-{index:02d}.jpg"
        _make_jpeg(path, (index * 40, 80, 120))
        items.append(_collected_image(path, index))
        label_path = tmp_path / f"frame-{index:02d}.txt"
        label_path.write_text("0 0.5 0.5 0.5 0.5\n", encoding="utf-8")
        items.append(_collected_label(label_path, index))
    classes_path = tmp_path / "classes.txt"
    classes_path.write_text("object\n", encoding="utf-8")
    items.append(
        CollectedFile(
            rel_path="classes.txt",
            abs_path=classes_path,
            kind="classfile",
            split=None,
        )
    )

    settings = app.state.settings.model_copy(
        update={"dataset_max_images": 3, "ingest_batch_size": 2},
    )
    await ingest_collected(
        settings,
        app.state.session_factory,
        job_id,
        items,
    )

    async with app.state.session_factory() as session:
        first = await session.get(Dataset, dataset_id)
        assert first is not None
        assert first.upload_group_id is not None
        parts = list(
            (
                await session.scalars(
                    select(Dataset)
                    .where(Dataset.upload_group_id == first.upload_group_id)
                    .order_by(Dataset.upload_part_index)
                )
            ).all()
        )
        assert [part.name for part in parts] == [
            f"{base_name}_(1)",
            f"{base_name}_(2)",
        ]
        assert [part.image_count for part in parts] == [2, 2]
        assert [part.annotation_count for part in parts] == [2, 2]
        assert [part.class_count for part in parts] == [1, 1]
        assert [part.upload_part_index for part in parts] == [1, 2]
        assert [part.upload_part_count for part in parts] == [2, 2]
        assert all(part.status == "ready" for part in parts)
        assert all(not part.is_placeholder for part in parts)

        for part in parts:
            storage = contained_storage_path(settings.storage_dir, part.storage_path)
            images = list(
                (
                    await session.scalars(
                        select(Image).where(Image.dataset_id == part.id)
                    )
                ).all()
            )
            assert len(images) == 2
            assert all(image.box_count == 1 for image in images)
            for image in images:
                stored = contained_storage_path(settings.storage_dir, image.file_path)
                assert stored.is_relative_to(storage)
                assert stored.is_file()

    job_response = await client.get(f"/api/jobs/{job_id}")
    assert job_response.status_code == 200
    assert [row["name"] for row in job_response.json()["datasets"]] == [
        f"{base_name}_(1)",
        f"{base_name}_(2)",
    ]
    assert [row["image_count"] for row in job_response.json()["datasets"]] == [2, 2]


@pytest.mark.asyncio
async def test_partitioned_ingest_resumes_from_a_committed_checkpoint(
    client: httpx.AsyncClient,
    app,
    tmp_path: Path,
) -> None:
    base_name = f"test-partition-resume-{uuid4().hex}"
    dataset_id, job_id = await _draft_dataset_and_job(client, app, base_name)
    items: list[CollectedFile] = []
    for index in range(4):
        path = tmp_path / f"resume-{index:02d}.jpg"
        _make_jpeg(path, (index * 30, 40, 100))
        items.append(_collected_image(path, index))

    settings = app.state.settings.model_copy(
        update={"dataset_max_images": 3, "ingest_batch_size": 1},
    )
    commit_count = 0

    def interrupt_second_checkpoint() -> None:
        nonlocal commit_count
        commit_count += 1
        if commit_count == 2:
            raise RuntimeError("checkpoint interruption")

    with pytest.raises(RuntimeError, match="checkpoint interruption"):
        await ingest_collected(
            settings,
            app.state.session_factory,
            job_id,
            items,
            before_commit=interrupt_second_checkpoint,
        )

    async with app.state.session_factory() as session:
        interrupted_job = await session.get(UploadJob, job_id)
        first = await session.get(Dataset, dataset_id)
        assert interrupted_job is not None
        assert interrupted_job.ingest_cursor == 1
        assert first is not None and first.upload_group_id is not None

    await ingest_collected(
        settings,
        app.state.session_factory,
        job_id,
        items,
    )

    async with app.state.session_factory() as session:
        first = await session.get(Dataset, dataset_id)
        assert first is not None
        parts = list(
            (
                await session.scalars(
                    select(Dataset)
                    .where(Dataset.upload_group_id == first.upload_group_id)
                    .order_by(Dataset.upload_part_index)
                )
            ).all()
        )
        assert [part.image_count for part in parts] == [2, 2]
        assert all(part.status == "ready" for part in parts)
        finished_job = await session.get(UploadJob, job_id)
        assert finished_job is not None
        assert finished_job.state == "done"
        assert finished_job.ingest_cursor == 4


@pytest.mark.asyncio
async def test_existing_dataset_cannot_exceed_the_server_image_limit(
    client: httpx.AsyncClient,
    app,
    tmp_path: Path,
) -> None:
    base_name = f"test-partition-limit-{uuid4().hex}"
    dataset_id, first_job_id = await _draft_dataset_and_job(
        client,
        app,
        base_name,
    )
    settings = app.state.settings.model_copy(
        update={"dataset_max_images": 3, "ingest_batch_size": 2},
    )
    initial_items: list[CollectedFile] = []
    for index in range(3):
        path = tmp_path / f"initial-{index:02d}.jpg"
        _make_jpeg(path, (20, index * 40, 120))
        initial_items.append(_collected_image(path, index))
    await ingest_collected(
        settings,
        app.state.session_factory,
        first_job_id,
        initial_items,
    )

    async with app.state.session_factory() as session:
        next_job = UploadJob(
            dataset_id=dataset_id,
            kind="folder",
            state="queued",
            phase="uploading",
            total=0,
            processed=0,
            failed=0,
        )
        session.add(next_job)
        await session.commit()
        await session.refresh(next_job)
        next_job_id = next_job.id

    extra_path = tmp_path / "extra.jpg"
    _make_jpeg(extra_path, (200, 80, 40))
    with pytest.raises(RuntimeError, match="maximum of 3 images"):
        await ingest_collected(
            settings,
            app.state.session_factory,
            next_job_id,
            [_collected_image(extra_path, 99)],
        )

    async with app.state.session_factory() as session:
        dataset = await session.get(Dataset, dataset_id)
        failed_job = await session.get(UploadJob, next_job_id)
        assert dataset is not None
        assert dataset.image_count == 3
        assert dataset.status == "ready"
        assert failed_job is not None and failed_job.state == "failed"
