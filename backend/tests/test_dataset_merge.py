from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import delete, event, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Annotation,
    Dataset,
    DatasetClass,
    DatasetMergeSource,
    Image,
    ProjectClass,
    RunImage,
    TrainingRun,
    UploadJob,
    WorkspaceStorage,
)
from app.services import dataset_merge as dataset_merge_service
from app.services import jobs as jobs_service
from app.services import training
from app.services.quota import increase_bytes_used
from app.services.storage import (
    contained_storage_path,
    stage_deletions_async as real_stage_deletions_async,
    storage_relative_path,
)
from tests.factories import image_with_media


pytestmark = pytest.mark.asyncio


async def create_ready_dataset(
    client: httpx.AsyncClient,
    app,
    *,
    project_id: int,
    suffix: str,
    classes: dict[int, str],
    annotation_class_id: int,
    content: bytes,
    count: int = 1,
) -> int:
    created = await client.post(
        "/api/datasets",
        json={
            "name": f"test-merge-{suffix}-{uuid4().hex}",
            "project_id": project_id,
        },
    )
    assert created.status_code == 201
    dataset_id = created.json()["id"]

    async with app.state.session_factory() as session:
        dataset = await session.get(Dataset, dataset_id)
        assert dataset is not None
        root = contained_storage_path(
            app.state.settings.storage_dir,
            dataset.storage_path,
        ) / "fixture"
        root.mkdir(parents=True)
        await session.execute(
            delete(DatasetClass).where(DatasetClass.dataset_id == dataset_id)
        )
        physical_bytes = 0
        for index in range(count):
            stem = "same" if count == 1 else f"same-{index}"
            original = root / f"{stem}.jpg"
            thumbnail = root / f"{stem}-thumb.jpg"
            original.write_bytes(content + f"-{index}".encode())
            thumbnail.write_bytes(b"thumb-" + content + f"-{index}".encode())
            original_bytes = original.stat().st_size
            thumb_bytes = thumbnail.stat().st_size
            physical_bytes += original_bytes + thumb_bytes
            image = image_with_media(
                workspace_id=dataset.workspace_id,
                dataset_id=dataset_id,
                stem=stem,
                filename=f"{stem}.jpg",
                rel_path=f"images/train/{stem}.jpg",
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
                original_bytes=original_bytes,
                display_bytes=0,
                thumb_bytes=thumb_bytes,
                box_count=1,
                is_modified=True,
            )
            image.annotations.append(
                Annotation(
                    class_id=annotation_class_id,
                    cx=0.5,
                    cy=0.5,
                    w=0.2,
                    h=0.2,
                )
            )
            session.add(image)
        dataset.status = "ready"
        dataset.image_count = count
        dataset.annotation_count = count
        dataset.class_count = len(classes)
        session.add_all(
            [
                DatasetClass(
                    dataset_id=dataset_id,
                    class_id=class_id,
                    name=name,
                )
                for class_id, name in classes.items()
            ]
        )
        await increase_bytes_used(session, dataset.workspace_id, physical_bytes)
        await session.commit()
    return dataset_id


def host_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(training, "is_container_environment", lambda: False)
    monkeypatch.setattr(training.torch.cuda, "is_available", lambda: True)
    monkeypatch.setattr(training, "_weight_is_available", lambda _name: True)
    monkeypatch.setattr(
        training.torch.cuda,
        "mem_get_info",
        lambda: (24 * 1024**3, 24 * 1024**3),
    )
    monkeypatch.setattr(
        training,
        "spawn_worker",
        lambda _run_id, _owner_id, _out_dir, _database_url: (
            training.SpawnedWorker(
                pid=4242,
                pid_started_at="123456",
                boot_id="test-boot-id",
            )
        ),
    )


def training_body() -> dict[str, object]:
    return {
        "weights": "yolo26n.pt",
        "epochs": 3,
        "imgsz": 640,
        "batch": -1,
    }


async def test_merge_creates_trainable_copy_and_groups_originals(
    client: httpx.AsyncClient,
    app,
) -> None:
    project = await client.post(
        "/api/projects",
        json={
            "name": f"test-merge-project-{uuid4().hex}",
            "classes": [],
        },
    )
    assert project.status_code == 201
    project_id = project.json()["id"]
    first_id = await create_ready_dataset(
        client,
        app,
        project_id=project_id,
        suffix="first",
        classes={0: "forklift", 1: "person"},
        annotation_class_id=0,
        content=b"first-image",
    )
    second_id = await create_ready_dataset(
        client,
        app,
        project_id=project_id,
        suffix="second",
        classes={0: "person", 1: "forklift"},
        annotation_class_id=0,
        content=b"second-image",
    )
    merged_name = f"test-merge-result-{uuid4().hex}"

    response = await client.post(
        "/api/datasets/merge",
        json={
            "name": merged_name,
            "dataset_ids": [first_id, second_id],
            "purpose": "train",
        },
    )

    assert response.status_code == 201
    merged = response.json()
    merged_id = merged["id"]
    assert merged["name"] == merged_name
    assert merged["status"] == "ready"
    assert merged["image_count"] == 2
    assert merged["annotation_count"] == 2
    assert merged["class_count"] == 2
    assert merged["merge_purpose"] == "train"
    assert [source["id"] for source in merged["source_datasets"]] == [
        first_id,
        second_id,
    ]

    classes = await client.get(f"/api/datasets/{merged_id}/classes")
    async with app.state.session_factory() as session:
        catalog_version = await session.scalar(select(Dataset.class_catalog_version).where(
            Dataset.id == merged_id,
        ))
    assert classes.json() == {
        "catalog_version": catalog_version,
        "classes": [
            {"class_id": 0, "name": "forklift"},
            {"class_id": 1, "name": "person"},
        ]
    }

    async with app.state.session_factory() as session:
        merged_images = (
            await session.scalars(
                select(Image)
                .where(Image.dataset_id == merged_id)
                .order_by(Image.stem)
            )
        ).all()
        assert {
            image.training_source_key for image in merged_images
        } == {f"dataset:{first_id}", f"dataset:{second_id}"}
        assert len(
            {image.training_group_key for image in merged_images}
        ) == 2
        annotations = (
            await session.execute(
                select(Image.stem, Annotation.class_id)
                .join(Annotation, Annotation.image_id == Image.id)
                .where(Image.dataset_id == merged_id)
            )
        ).all()
        usage = await session.get(WorkspaceStorage, 1)

    assert [image.stem for image in merged_images] == ["same", "same (1)"]
    assert {image.filename for image in merged_images} == {
        "same.jpg",
        "same (1).jpg",
    }
    assert all(not Path(image.file_path).is_absolute() for image in merged_images)
    assert all(not Path(image.thumb_path).is_absolute() for image in merged_images)
    assert all(
        contained_storage_path(
            app.state.settings.storage_dir,
            image.file_path,
        ).is_file()
        for image in merged_images
    )
    assert dict(annotations) == {"same": 0, "same (1)": 1}
    merged_bytes = sum(
        image.original_bytes + image.display_bytes + image.thumb_bytes
        for image in merged_images
    )
    assert merged_bytes > 0
    assert usage is not None and usage.bytes_used == merged_bytes

    listing = await client.get("/api/datasets?offset=0&limit=200")
    assert listing.status_code == 200
    assert listing.json()["total"] == 1
    assert [item["id"] for item in listing.json()["items"]] == [merged_id]
    assert [
        source["id"]
        for source in listing.json()["items"][0]["source_datasets"]
    ] == [first_id, second_id]
    assert (await client.get(f"/api/datasets/{first_id}")).status_code == 200
    assert (await client.get(f"/api/datasets/{second_id}")).status_code == 200

    project_statements: list[str] = []

    def capture_project_statements(
        _connection,
        _cursor,
        statement: str,
        _parameters,
        _context,
        _executemany,
    ) -> None:
        project_statements.append(statement.lower())

    event.listen(
        app.state.engine.sync_engine,
        "before_cursor_execute",
        capture_project_statements,
    )
    try:
        projects = await client.get("/api/projects")
    finally:
        event.remove(
            app.state.engine.sync_engine,
            "before_cursor_execute",
            capture_project_statements,
        )
    assert projects.status_code == 200
    assert sum(
        "dataset_merge_sources.position" in statement
        for statement in project_statements
    ) == 1
    project_row = next(
        item for item in projects.json()["items"] if item["id"] == project_id
    )
    assert [item["id"] for item in project_row["datasets"]] == [merged_id]
    project_sources = project_row["datasets"][0]["source_datasets"]
    assert [source["id"] for source in project_sources] == [
        first_id,
        second_id,
    ]
    assert [source["project_id"] for source in project_sources] == [
        project_id,
        project_id,
    ]
    assert [source["image_count"] for source in project_sources] == [1, 1]
    assert [source["annotation_count"] for source in project_sources] == [1, 1]
    assert [source["class_count"] for source in project_sources] == [2, 2]
    assert [source["status"] for source in project_sources] == ["ready", "ready"]
    assert [source["is_merged"] for source in project_sources] == [False, False]
    assert [source["active_job"] for source in project_sources] == [None, None]
    assert all(
        source["name"].startswith("test-merge-")
        for source in project_sources
    )
    assert all(source["created_at"] for source in project_sources)

    deleted = await client.delete(f"/api/datasets/{merged_id}")
    assert deleted.status_code == 204
    async with app.state.session_factory() as session:
        usage = await session.get(WorkspaceStorage, 1)
        assert usage is not None and usage.bytes_used == merged_bytes
        remaining_ids = (await session.scalars(select(Dataset.id))).all()
        assert set(remaining_ids) == {first_id, second_id}
    # The selected reference is removed; its source datasets remain intact and
    # become visible when merge membership cascades away.
    after_listing = await client.get("/api/datasets?offset=0&limit=200")
    assert after_listing.json()["total"] == 2
    assert {item["id"] for item in after_listing.json()["items"]} == {
        first_id,
        second_id,
    }


@pytest.mark.parametrize(
    "active_state", ["queued", "running", "awaiting_class_resolution"]
)
async def test_large_merge_is_durable_and_completes_through_a_polled_job(
    client: httpx.AsyncClient,
    app,
    monkeypatch: pytest.MonkeyPatch,
    active_state: str,
) -> None:
    project = await client.post(
        "/api/projects",
        json={"name": f"test-merge-queued-{uuid4().hex}", "classes": []},
    )
    assert project.status_code == 201
    project_id = project.json()["id"]
    source_ids = [
        await create_ready_dataset(
            client,
            app,
            project_id=project_id,
            suffix=f"queued-{index}",
            classes={0: "forklift"},
            annotation_class_id=0,
            content=f"queued-{index}".encode(),
        )
        for index in range(2)
    ]
    monkeypatch.setattr(
        dataset_merge_service,
        "DATASET_MERGE_INLINE_WORK_LIMIT",
        0,
    )

    queued_name = f"test-merge-queued-result-{uuid4().hex}"
    queued = await client.post(
        "/api/datasets/merge",
        json={
            "name": queued_name,
            "dataset_ids": source_ids,
        },
    )

    assert queued.status_code == 202
    queued_row = queued.json()
    assert queued_row["status"] == "pending"
    assert queued_row["image_count"] == 0
    assert queued_row["merge_purpose"] == "merge"
    assert queued_row["active_job"]["state"] == "queued"
    assert [row["id"] for row in queued_row["source_datasets"]] == source_ids
    job_id = queued_row["active_job"]["job_id"]

    async with app.state.session_factory() as session:
        active_job = await session.get(UploadJob, job_id)
        assert active_job is not None
        active_job.state = active_state
        await session.commit()

    replayed = await client.post(
        "/api/datasets/merge",
        json={"name": queued_name, "dataset_ids": source_ids},
    )
    assert replayed.status_code == 200
    assert replayed.json()["id"] == queued_row["id"]
    assert replayed.json()["active_job"]["job_id"] == job_id
    assert replayed.json()["active_job"]["state"] == active_state

    async with app.state.session_factory() as session:
        interrupted_job = await session.get(UploadJob, job_id)
        assert interrupted_job is not None
        assert interrupted_job.upload_ids == []
        interrupted_job.state = "running"
        interrupted_job.phase = "merging"
        await session.commit()
    redispatched: list[int] = []
    monkeypatch.setattr(
        jobs_service,
        "enqueue_dataset_merge_job",
        lambda _app, recovered_job_id: redispatched.append(recovered_job_id),
    )
    recovered = await jobs_service.recover_upload_jobs(app)
    assert recovered == [job_id]
    assert redispatched == [job_id]

    await dataset_merge_service.run_dataset_merge_job(
        app.state.settings,
        app.state.session_factory,
        job_id,
    )

    completed = await client.get(f"/api/jobs/{job_id}")
    assert completed.status_code == 200
    completed_job = completed.json()
    assert completed_job["state"] == "done"
    assert completed_job["phase"] == "done"
    assert completed_job["image_processed"] == 2
    assert completed_job["datasets"] == [
        {
            "id": queued_row["id"],
            "name": queued_row["name"],
            "status": "ready",
            "image_count": 2,
            "annotation_count": 2,
            "class_count": 1,
        }
    ]


async def test_merge_uses_complete_project_catalog_and_name_based_annotation_ids(
    client: httpx.AsyncClient,
    app,
) -> None:
    project = await client.post(
        "/api/projects",
        json={
            "name": f"test-merge-catalog-{uuid4().hex}",
            "classes": [
                {"name": "person", "color": "#EF4444"},
                {"name": "forklift", "color": "#F59E0B"},
                {"name": "helmet", "color": "#22C55E"},
            ],
        },
    )
    assert project.status_code == 201
    project_id = project.json()["id"]
    forklift_id = await create_ready_dataset(
        client,
        app,
        project_id=project_id,
        suffix="catalog-forklift",
        classes={1: "forklift"},
        annotation_class_id=1,
        content=b"forklift-image",
    )
    person_id = await create_ready_dataset(
        client,
        app,
        project_id=project_id,
        suffix="catalog-person",
        classes={0: "person"},
        annotation_class_id=0,
        content=b"person-image",
    )

    merged = await client.post(
        "/api/datasets/merge",
        json={
            "name": f"test-merge-catalog-result-{uuid4().hex}",
            "dataset_ids": [forklift_id, person_id],
        },
    )

    assert merged.status_code == 201
    merged_id = merged.json()["id"]
    classes = await client.get(f"/api/datasets/{merged_id}/classes")
    async with app.state.session_factory() as session:
        catalog_version = await session.scalar(select(Dataset.class_catalog_version).where(
            Dataset.id == merged_id,
        ))
    assert classes.json() == {
        "catalog_version": catalog_version,
        "classes": [
            {"class_id": 0, "name": "person"},
            {"class_id": 1, "name": "forklift"},
            {"class_id": 2, "name": "helmet"},
        ]
    }
    async with app.state.session_factory() as session:
        project_classes = (
            await session.execute(
                select(ProjectClass.class_id, ProjectClass.name)
                .where(ProjectClass.project_id == project_id)
                .order_by(ProjectClass.class_id)
            )
        ).all()
        merged_classes = (
            await session.execute(
                select(DatasetClass.class_id, DatasetClass.name)
                .where(DatasetClass.dataset_id == merged_id)
                .order_by(DatasetClass.class_id)
            )
        ).all()
        annotations = (
            await session.execute(
                select(Image.stem, Annotation.class_id)
                .join(Annotation, Annotation.image_id == Image.id)
                .where(Image.dataset_id == merged_id)
            )
        ).all()
    assert merged_classes == project_classes
    assert dict(annotations) == {"same": 1, "same (1)": 0}

    renamed = await client.patch(
        f"/api/datasets/{merged_id}/classes/1",
        json={"name": "lift"},
    )
    assert renamed.status_code == 200
    renamed_classes = await client.get(f"/api/datasets/{merged_id}/classes")
    async with app.state.session_factory() as session:
        catalog_version = await session.scalar(select(Dataset.class_catalog_version).where(
            Dataset.id == merged_id,
        ))
    assert renamed_classes.json() == {
        "catalog_version": catalog_version,
        "classes": [
            {"class_id": 0, "name": "person"},
            {"class_id": 1, "name": "lift"},
            {"class_id": 2, "name": "helmet"},
        ]
    }


async def test_merge_registers_missing_and_orphan_class_names_deterministically(
    client: httpx.AsyncClient,
    app,
) -> None:
    project = await client.post(
        "/api/projects",
        json={
            "name": f"test-merge-orphan-{uuid4().hex}",
            "classes": [{"name": "person", "color": "#EF4444"}],
        },
    )
    project_id = project.json()["id"]
    forklift_id = await create_ready_dataset(
        client,
        app,
        project_id=project_id,
        suffix="missing-name",
        classes={7: "forklift"},
        annotation_class_id=7,
        content=b"forklift-image",
    )
    orphan_id = await create_ready_dataset(
        client,
        app,
        project_id=project_id,
        suffix="orphan-id",
        classes={},
        annotation_class_id=9,
        content=b"orphan-image",
    )

    response = await client.post(
        "/api/datasets/merge",
        json={
            "name": f"test-merge-orphan-result-{uuid4().hex}",
            "dataset_ids": [forklift_id, orphan_id],
        },
    )

    assert response.status_code == 201
    merged_id = response.json()["id"]
    async with app.state.session_factory() as session:
        project_classes = (
            await session.execute(
                select(
                    ProjectClass.class_id,
                    ProjectClass.name,
                    ProjectClass.color,
                )
                .where(ProjectClass.project_id == project_id)
                .order_by(ProjectClass.class_id)
            )
        ).all()
        merged_classes = (
            await session.execute(
                select(DatasetClass.class_id, DatasetClass.name)
                .where(DatasetClass.dataset_id == merged_id)
                .order_by(DatasetClass.class_id)
            )
        ).all()
        annotation_ids = dict(
            (
                await session.execute(
                    select(Image.stem, Annotation.class_id)
                    .join(Annotation, Annotation.image_id == Image.id)
                    .where(Image.dataset_id == merged_id)
                )
            ).all()
        )
    assert project_classes == [
        (0, "person", "#EF4444"),
        (1, "class_9", "#F59E0B"),
        (2, "forklift", "#22C55E"),
    ]
    assert merged_classes == [
        (0, "person"),
        (1, "class_9"),
        (2, "forklift"),
    ]
    assert annotation_ids == {"same": 2, "same (1)": 1}


async def test_merge_reuses_exact_source_set_without_new_rows_storage_or_quota(
    client: httpx.AsyncClient,
    app,
) -> None:
    project = await client.post(
        "/api/projects",
        json={"name": f"test-merge-reuse-{uuid4().hex}", "classes": []},
    )
    project_id = project.json()["id"]
    first_id = await create_ready_dataset(
        client,
        app,
        project_id=project_id,
        suffix="reuse-first",
        classes={0: "person"},
        annotation_class_id=0,
        content=b"first",
    )
    second_id = await create_ready_dataset(
        client,
        app,
        project_id=project_id,
        suffix="reuse-second",
        classes={0: "person"},
        annotation_class_id=0,
        content=b"second",
    )
    first = await client.post(
        "/api/datasets/merge",
        json={
            "name": f"test-merge-reuse-result-{uuid4().hex}",
            "dataset_ids": [first_id, second_id],
        },
    )
    assert first.status_code == 201
    merged_id = first.json()["id"]
    assert first.json()["merge_purpose"] == "merge"

    async with app.state.session_factory() as session:
        dataset_count_before = await session.scalar(
            select(func.count(Dataset.id))
        )
        membership_count_before = await session.scalar(
            select(func.count()).select_from(DatasetMergeSource)
        )
        usage_before = await session.get(WorkspaceStorage, 1)
        assert usage_before is not None
        bytes_before = usage_before.bytes_used
    dataset_root = app.state.settings.storage_dir / "datasets"
    directories_before = {path.name for path in dataset_root.iterdir()}

    reused = await client.post(
        "/api/datasets/merge",
        json={
            "name": f"test-merge-reuse-ignored-{uuid4().hex}",
            "dataset_ids": [second_id, first_id],
            "purpose": "train",
        },
    )

    assert reused.status_code == 200
    assert reused.json()["id"] == merged_id
    assert reused.json()["merge_purpose"] == "train"
    assert [row["id"] for row in reused.json()["source_datasets"]] == [
        first_id,
        second_id,
    ]
    async with app.state.session_factory() as session:
        assert await session.scalar(select(func.count(Dataset.id))) == (
            dataset_count_before
        )
        assert await session.scalar(
            select(func.count()).select_from(DatasetMergeSource)
        ) == membership_count_before
        usage_after = await session.get(WorkspaceStorage, 1)
        assert usage_after is not None and usage_after.bytes_used == bytes_before
    assert {path.name for path in dataset_root.iterdir()} == directories_before

    ordinary_reuse = await client.post(
        "/api/datasets/merge",
        json={
            "name": f"test-merge-reuse-still-training-{uuid4().hex}",
            "dataset_ids": [first_id, second_id],
            "purpose": "merge",
        },
    )
    assert ordinary_reuse.status_code == 200
    assert ordinary_reuse.json()["merge_purpose"] == "train"


async def _create_merge_reuse_case(
    client: httpx.AsyncClient,
    app,
    *,
    source_count: int = 2,
) -> tuple[list[int], int]:
    project = await client.post(
        "/api/projects",
        json={"name": f"test-merge-reuse-case-{uuid4().hex}", "classes": []},
    )
    assert project.status_code == 201
    source_ids = [
        await create_ready_dataset(
            client,
            app,
            project_id=project.json()["id"],
            suffix=f"reuse-case-{index}",
            classes={0: "person"},
            annotation_class_id=0,
            content=f"source-{index}".encode(),
        )
        for index in range(source_count)
    ]
    merged = await client.post(
        "/api/datasets/merge",
        json={
            "name": f"test-merge-reuse-case-result-{uuid4().hex}",
            "dataset_ids": source_ids,
        },
    )
    assert merged.status_code == 201
    return source_ids, merged.json()["id"]


async def test_source_deletion_detaches_merge_without_changing_snapshot(
    client: httpx.AsyncClient,
    app,
) -> None:
    source_ids, merged_id = await _create_merge_reuse_case(
        client, app, source_count=3,
    )
    snapshot_query = (
        select(
            Image.id, Image.file_path, Image.thumb_path, Image.training_source_key,
        )
        .where(Image.dataset_id == merged_id)
        .order_by(Image.id)
    )
    async with app.state.session_factory() as session:
        snapshot_before = (await session.execute(snapshot_query)).all()
        usage = await session.get(WorkspaceStorage, 1)
        assert usage is not None
        bytes_before = usage.bytes_used
        old_dataset = await session.get(Dataset, merged_id)
        assert old_dataset is not None
        storage_path_before = old_dataset.storage_path
    files_before = {
        path: contained_storage_path(
            app.state.settings.storage_dir, path,
        ).read_bytes()
        for _image_id, file_path, thumb_path, _source_key in snapshot_before
        for path in (file_path, thumb_path)
    }
    assert len(snapshot_before) == 3

    deleted = await client.delete(f"/api/datasets/{source_ids[2]}")

    assert deleted.status_code == 204
    async with app.state.session_factory() as session:
        assert await session.get(Dataset, source_ids[2]) is None
        assert await session.scalar(
            select(func.count()).select_from(DatasetMergeSource).where(
                DatasetMergeSource.merged_dataset_id == merged_id,
            )
        ) == 0
        old_dataset = await session.get(Dataset, merged_id)
        assert old_dataset is not None
        assert old_dataset.status == "ready"
        assert old_dataset.image_count == 3
        assert old_dataset.storage_path == storage_path_before
        assert (await session.execute(snapshot_query)).all() == snapshot_before
        usage = await session.get(WorkspaceStorage, 1)
        # The snapshot still references all three immutable media bundles.
        assert usage is not None and usage.bytes_used == bytes_before
    for path, content in files_before.items():
        assert contained_storage_path(
            app.state.settings.storage_dir, path,
        ).read_bytes() == content

    new_merge = await client.post(
        "/api/datasets/merge",
        json={
            "name": f"test-merge-after-source-delete-{uuid4().hex}",
            "dataset_ids": source_ids[:2],
        },
    )

    assert new_merge.status_code == 201
    assert new_merge.json()["id"] != merged_id
    assert new_merge.json()["image_count"] == 2
    assert [
        source["id"] for source in new_merge.json()["source_datasets"]
    ] == source_ids[:2]
    async with app.state.session_factory() as session:
        assert (await session.execute(snapshot_query)).all() == snapshot_before
        old_dataset = await session.get(Dataset, merged_id)
        assert old_dataset is not None and old_dataset.image_count == 3
        assert await session.scalar(
            select(func.count()).select_from(DatasetMergeSource).where(
                DatasetMergeSource.merged_dataset_id == merged_id,
            )
        ) == 0
    for path, content in files_before.items():
        assert contained_storage_path(
            app.state.settings.storage_dir, path,
        ).read_bytes() == content


@pytest.mark.parametrize(
    ("dataset_status", "job_state"),
    [
        ("failed", None),
        ("failed", "queued"),
        ("pending", None),
        ("pending", "done"),
        ("pending", "failed"),
    ],
)
async def test_merge_rejects_unusable_exact_match_without_side_effects(
    client: httpx.AsyncClient,
    app,
    dataset_status: str,
    job_state: str | None,
) -> None:
    source_ids, merged_id = await _create_merge_reuse_case(client, app)
    async with app.state.session_factory() as session:
        merged = await session.get(Dataset, merged_id)
        assert merged is not None
        merged.status = dataset_status
        if job_state is not None:
            session.add(UploadJob(
                dataset_id=merged_id,
                kind="dataset_merge",
                state=job_state,
                upload_ids=[],
            ))
        await session.commit()
        dataset_count_before = await session.scalar(
            select(func.count(Dataset.id))
        )
        membership_count_before = await session.scalar(
            select(func.count()).select_from(DatasetMergeSource)
        )
        usage = await session.get(WorkspaceStorage, 1)
        assert usage is not None
        bytes_before = usage.bytes_used
    dataset_root = app.state.settings.storage_dir / "datasets"
    files_before = {
        path.relative_to(dataset_root): path.read_bytes()
        for path in dataset_root.rglob("*")
        if path.is_file()
    }
    directories_before = {path.name for path in dataset_root.iterdir()}

    rejected = await client.post(
        "/api/datasets/merge",
        json={
            "name": f"test-merge-unusable-retry-{uuid4().hex}",
            "dataset_ids": list(reversed(source_ids)),
            "purpose": "train",
        },
    )

    assert rejected.status_code == 409
    assert rejected.json()["detail"] == (
        "실패한 병합 작업은 삭제한 뒤 다시 시도해 주세요."
    )
    async with app.state.session_factory() as session:
        assert await session.scalar(select(func.count(Dataset.id))) == (
            dataset_count_before
        )
        assert await session.scalar(
            select(func.count()).select_from(DatasetMergeSource)
        ) == membership_count_before
        merged = await session.get(Dataset, merged_id)
        assert merged is not None and merged.status == dataset_status
        assert merged.merge_purpose == "merge"
        usage = await session.get(WorkspaceStorage, 1)
        assert usage is not None and usage.bytes_used == bytes_before
    assert {path.name for path in dataset_root.iterdir()} == directories_before
    assert {
        path.relative_to(dataset_root): path.read_bytes()
        for path in dataset_root.rglob("*")
        if path.is_file()
    } == files_before


async def test_source_delete_commit_failure_preserves_merge_membership_and_files(
    client: httpx.AsyncClient,
    app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_ids, merged_id = await _create_merge_reuse_case(
        client, app, source_count=3,
    )
    async with app.state.session_factory() as session:
        usage = await session.get(WorkspaceStorage, 1)
        assert usage is not None
        bytes_before = usage.bytes_used
    dataset_root = app.state.settings.storage_dir / "datasets"
    files_before = {
        path.relative_to(dataset_root): path.read_bytes()
        for path in dataset_root.rglob("*")
        if path.is_file()
    }

    async def fail_commit(_session: AsyncSession) -> None:
        raise RuntimeError("forced source delete commit failure")

    with monkeypatch.context() as patch:
        patch.setattr(AsyncSession, "commit", fail_commit)
        with pytest.raises(
            RuntimeError, match="forced source delete commit failure",
        ):
            await client.delete(f"/api/datasets/{source_ids[2]}")

    async with app.state.session_factory() as session:
        assert await session.get(Dataset, source_ids[2]) is not None
        old_dataset = await session.get(Dataset, merged_id)
        assert old_dataset is not None and old_dataset.image_count == 3
        assert list(
            await session.scalars(
                select(DatasetMergeSource.source_dataset_id)
                .where(DatasetMergeSource.merged_dataset_id == merged_id)
                .order_by(DatasetMergeSource.position)
            )
        ) == source_ids
        usage = await session.get(WorkspaceStorage, 1)
        assert usage is not None and usage.bytes_used == bytes_before
    assert {
        path.relative_to(dataset_root): path.read_bytes()
        for path in dataset_root.rglob("*")
        if path.is_file()
    } == files_before


async def test_merge_partial_overlap_returns_structured_conflict_without_side_effects(
    client: httpx.AsyncClient,
    app,
) -> None:
    project = await client.post(
        "/api/projects",
        json={"name": f"test-merge-overlap-{uuid4().hex}", "classes": []},
    )
    project_id = project.json()["id"]
    source_ids = [
        await create_ready_dataset(
            client,
            app,
            project_id=project_id,
            suffix=f"overlap-{index}",
            classes={0: "person"},
            annotation_class_id=0,
            content=f"source-{index}".encode(),
        )
        for index in range(3)
    ]
    first = await client.post(
        "/api/datasets/merge",
        json={
            "name": f"test-merge-overlap-result-{uuid4().hex}",
            "dataset_ids": source_ids[:2],
        },
    )
    assert first.status_code == 201
    existing = first.json()
    async with app.state.session_factory() as session:
        dataset_count_before = await session.scalar(select(func.count(Dataset.id)))
        usage = await session.get(WorkspaceStorage, 1)
        assert usage is not None
        bytes_before = usage.bytes_used
    dataset_root = app.state.settings.storage_dir / "datasets"
    directories_before = {path.name for path in dataset_root.iterdir()}

    overlap = await client.post(
        "/api/datasets/merge",
        json={
            "name": f"test-merge-overlap-rejected-{uuid4().hex}",
            "dataset_ids": source_ids[1:],
        },
    )

    assert overlap.status_code == 409
    assert overlap.json()["detail"] == {
        "code": "dataset_merge_source_overlap",
        "merged_dataset": {
            "id": existing["id"],
            "name": existing["name"],
            "source_dataset_ids": source_ids[:2],
        },
    }
    async with app.state.session_factory() as session:
        assert await session.scalar(select(func.count(Dataset.id))) == (
            dataset_count_before
        )
        usage_after = await session.get(WorkspaceStorage, 1)
        assert usage_after is not None and usage_after.bytes_used == bytes_before
    assert {path.name for path in dataset_root.iterdir()} == directories_before


async def test_merge_reuse_does_not_bypass_owner_scoped_404(
    client: httpx.AsyncClient,
    app,
    auth_headers,
) -> None:
    project = await client.post(
        "/api/projects",
        json={"name": f"test-merge-owner-{uuid4().hex}", "classes": []},
    )
    project_id = project.json()["id"]
    source_ids = [
        await create_ready_dataset(
            client,
            app,
            project_id=project_id,
            suffix=f"owner-{index}",
            classes={0: "person"},
            annotation_class_id=0,
            content=f"owner-{index}".encode(),
        )
        for index in range(2)
    ]
    first = await client.post(
        "/api/datasets/merge",
        json={
            "name": f"test-merge-owner-result-{uuid4().hex}",
            "dataset_ids": source_ids,
        },
    )
    assert first.status_code == 201

    hidden = await client.post(
        "/api/datasets/merge",
        headers=auth_headers(2),
        json={
            "name": f"test-merge-owner-hidden-{uuid4().hex}",
            "dataset_ids": list(reversed(source_ids)),
        },
    )

    assert hidden.status_code == 404
    assert hidden.json()["detail"] == "선택한 데이터셋을 찾을 수 없습니다."


async def test_merged_dataset_id_trains_all_selected_images(
    client: httpx.AsyncClient,
    app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host_ready(monkeypatch)
    project = await client.post(
        "/api/projects",
        json={
            "name": f"test-merge-train-{uuid4().hex}",
            "classes": [
                {"name": "person", "color": "#EF4444"},
                {"name": "forklift", "color": "#F59E0B"},
            ],
        },
    )
    project_id = project.json()["id"]
    first_id = await create_ready_dataset(
        client,
        app,
        project_id=project_id,
        suffix="train-person",
        classes={0: "person"},
        annotation_class_id=0,
        content=b"person",
        count=5,
    )
    second_id = await create_ready_dataset(
        client,
        app,
        project_id=project_id,
        suffix="train-forklift",
        classes={1: "forklift"},
        annotation_class_id=1,
        content=b"forklift",
        count=5,
    )
    merged = await client.post(
        "/api/datasets/merge",
        json={
            "name": f"test-merge-train-result-{uuid4().hex}",
            "dataset_ids": [first_id, second_id],
        },
    )
    assert merged.status_code == 201
    assert merged.json()["image_count"] == 10
    merged_id = merged.json()["id"]

    submitted = await client.post(
        f"/api/datasets/{merged_id}/train",
        json=training_body(),
    )

    assert submitted.status_code == 201
    async with app.state.session_factory() as session:
        run = await session.get(TrainingRun, submitted.json()["run_id"])
        assert run is not None and run.dataset_id == merged_id
        trained_dataset = await session.get(Dataset, merged_id)
        assert trained_dataset is not None
        assert trained_dataset.merge_purpose == "train"
        run_image_count = await session.scalar(
            select(func.count(RunImage.id)).where(RunImage.run_id == run.id)
        )
        source_split_counts = {
            (source_key, split): count
            for source_key, split, count in (
                await session.execute(
                    select(
                        Image.training_source_key,
                        RunImage.split,
                        func.count(RunImage.id),
                    )
                    .join(Image, Image.id == RunImage.image_id)
                    .where(RunImage.run_id == run.id)
                    .group_by(Image.training_source_key, RunImage.split)
                )
            ).all()
        }
    assert run_image_count == merged.json()["image_count"]
    assert source_split_counts == {
        (f"dataset:{first_id}", "train"): 4,
        (f"dataset:{first_id}", "valid"): 1,
        (f"dataset:{second_id}", "train"): 4,
        (f"dataset:{second_id}", "valid"): 1,
    }


async def test_merge_rejects_sources_from_different_projects(
    client: httpx.AsyncClient,
    app,
) -> None:
    first_project = await client.post(
        "/api/projects",
        json={"name": f"test-merge-project-a-{uuid4().hex}", "classes": []},
    )
    second_project = await client.post(
        "/api/projects",
        json={"name": f"test-merge-project-b-{uuid4().hex}", "classes": []},
    )
    assert first_project.status_code == 201
    assert second_project.status_code == 201

    first = await client.post(
        "/api/datasets",
        json={
            "name": f"test-merge-cross-project-a-{uuid4().hex}",
            "project_id": first_project.json()["id"],
        },
    )
    second = await client.post(
        "/api/datasets",
        json={
            "name": f"test-merge-cross-project-b-{uuid4().hex}",
            "project_id": second_project.json()["id"],
        },
    )
    assert first.status_code == 201
    assert second.status_code == 201

    async with app.state.session_factory() as session:
        for dataset_id in (first.json()["id"], second.json()["id"]):
            dataset = await session.get(Dataset, dataset_id)
            assert dataset is not None
            dataset.status = "ready"
        await session.commit()

    merged_name = f"test-merge-cross-project-result-{uuid4().hex}"
    response = await client.post(
        "/api/datasets/merge",
        json={
            "name": merged_name,
            "dataset_ids": [first.json()["id"], second.json()["id"]],
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"] == (
        "같은 프로젝트의 데이터셋만 합칠 수 있습니다."
    )
    listing = await client.get("/api/datasets?offset=0&limit=200")
    assert all(item["name"] != merged_name for item in listing.json()["items"])


async def test_merge_rejects_invalid_source_sets(
    client: httpx.AsyncClient,
) -> None:
    created = await client.post(
        "/api/datasets",
        json={"name": f"test-merge-pending-{uuid4().hex}"},
    )
    pending_id = created.json()["id"]

    too_few = await client.post(
        "/api/datasets/merge",
        json={"name": f"test-merge-few-{uuid4().hex}", "dataset_ids": [pending_id]},
    )
    duplicated = await client.post(
        "/api/datasets/merge",
        json={
            "name": f"test-merge-duplicate-{uuid4().hex}",
            "dataset_ids": [pending_id, pending_id],
        },
    )
    invalid_purpose = await client.post(
        "/api/datasets/merge",
        json={
            "name": f"test-merge-invalid-purpose-{uuid4().hex}",
            "dataset_ids": [pending_id, pending_id + 1],
            "purpose": "export",
        },
    )

    assert too_few.status_code == 422
    assert duplicated.status_code == 422
    assert invalid_purpose.status_code == 422


async def test_extend_merged_dataset_appends_sources_without_mutating_existing_snapshot(
    client: httpx.AsyncClient,
    app,
) -> None:
    project = await client.post(
        "/api/projects",
        json={"name": f"test-merge-extend-{uuid4().hex}", "classes": []},
    )
    project_id = project.json()["id"]
    source_ids = [
        await create_ready_dataset(
            client,
            app,
            project_id=project_id,
            suffix=f"extend-{index}",
            classes={0: "person"},
            annotation_class_id=0,
            content=f"extend-{index}".encode(),
        )
        for index in range(3)
    ]
    created = await client.post(
        "/api/datasets/merge",
        json={
            "name": f"test-merge-extend-target-{uuid4().hex}",
            "dataset_ids": source_ids[:2],
        },
    )
    assert created.status_code == 201
    target_id = created.json()["id"]

    async with app.state.session_factory() as session:
        existing_image = await session.scalar(
            select(Image)
            .where(Image.dataset_id == target_id)
            .order_by(Image.id)
        )
        assert existing_image is not None
        existing_annotation = await session.scalar(
            select(Annotation).where(Annotation.image_id == existing_image.id)
        )
        assert existing_annotation is not None
        existing_annotation.cx = 0.8125
        existing_image.is_modified = True
        await session.commit()
        existing_snapshot = (
            existing_image.id,
            existing_image.file_path,
            existing_annotation.id,
            existing_annotation.cx,
        )
        usage_before = await session.get(WorkspaceStorage, 1)
        assert usage_before is not None
        bytes_before = usage_before.bytes_used

    extended = await client.post(
        f"/api/datasets/{target_id}/merge-sources",
        json={"dataset_ids": [source_ids[2]], "purpose": "train"},
    )

    assert extended.status_code == 200
    result = extended.json()
    assert result["id"] == target_id
    assert result["image_count"] == 3
    assert result["annotation_count"] == 3
    assert result["merge_purpose"] == "train"
    assert [source["id"] for source in result["source_datasets"]] == source_ids
    async with app.state.session_factory() as session:
        unchanged_image = await session.get(Image, existing_snapshot[0])
        unchanged_annotation = await session.get(Annotation, existing_snapshot[2])
        assert unchanged_image is not None
        assert unchanged_annotation is not None
        assert unchanged_image.file_path == existing_snapshot[1]
        assert unchanged_image.is_modified is True
        assert unchanged_annotation.cx == existing_snapshot[3]
        appended = await session.scalar(
            select(Image)
            .where(Image.dataset_id == target_id, Image.id != existing_snapshot[0])
            .order_by(Image.id.desc())
        )
        assert appended is not None
        assert appended.original_bytes + appended.thumb_bytes > 0
        usage_after = await session.get(WorkspaceStorage, 1)
        assert usage_after is not None
        assert usage_after.bytes_used == bytes_before

    listing = await client.get("/api/datasets?offset=0&limit=200")
    assert [item["id"] for item in listing.json()["items"]] == [target_id]


async def test_extend_merged_dataset_consolidates_merged_snapshots_and_preserves_edits(
    client: httpx.AsyncClient,
    app,
) -> None:
    project = await client.post(
        "/api/projects",
        json={"name": f"test-merge-consolidate-{uuid4().hex}", "classes": []},
    )
    project_id = project.json()["id"]
    source_ids = [
        await create_ready_dataset(
            client,
            app,
            project_id=project_id,
            suffix=f"consolidate-{index}",
            classes={0: "person"},
            annotation_class_id=0,
            content=f"consolidate-{index}".encode(),
        )
        for index in range(4)
    ]
    target_response = await client.post(
        "/api/datasets/merge",
        json={
            "name": f"test-merge-consolidate-target-{uuid4().hex}",
            "dataset_ids": source_ids[:2],
        },
    )
    loser_response = await client.post(
        "/api/datasets/merge",
        json={
            "name": f"test-merge-consolidate-loser-{uuid4().hex}",
            "dataset_ids": source_ids[2:],
        },
    )
    assert target_response.status_code == 201
    assert loser_response.status_code == 201
    target_id = target_response.json()["id"]
    loser_id = loser_response.json()["id"]

    async with app.state.session_factory() as session:
        loser_image = await session.scalar(
            select(Image)
            .where(Image.dataset_id == loser_id)
            .order_by(Image.id)
        )
        assert loser_image is not None
        loser_annotation = await session.scalar(
            select(Annotation).where(Annotation.image_id == loser_image.id)
        )
        assert loser_annotation is not None
        loser_annotation.cx = 0.9375
        loser_annotation.cy = 0.1875
        loser_image.is_modified = True
        loser_dataset = await session.get(Dataset, loser_id)
        assert loser_dataset is not None
        loser_storage = contained_storage_path(
            app.state.settings.storage_dir,
            loser_dataset.storage_path,
        )
        await session.commit()
        usage_before = await session.get(WorkspaceStorage, 1)
        assert usage_before is not None
        bytes_before = usage_before.bytes_used

    consolidated = await client.post(
        f"/api/datasets/{target_id}/merge-sources",
        json={"dataset_ids": [loser_id]},
    )

    assert consolidated.status_code == 200
    result = consolidated.json()
    assert result["id"] == target_id
    assert result["image_count"] == 4
    assert result["annotation_count"] == 4
    assert [source["id"] for source in result["source_datasets"]] == source_ids
    assert (await client.get(f"/api/datasets/{loser_id}")).status_code == 404
    assert not loser_storage.exists()
    async with app.state.session_factory() as session:
        assert await session.get(Dataset, loser_id) is None
        memberships = (
            await session.execute(
                select(
                    DatasetMergeSource.source_dataset_id,
                    DatasetMergeSource.position,
                )
                .where(DatasetMergeSource.merged_dataset_id == target_id)
                .order_by(DatasetMergeSource.position)
            )
        ).all()
        assert memberships == list(zip(source_ids, range(4), strict=True))
        copied_edits = (
            await session.execute(
                select(Image.is_modified, Annotation.cx, Annotation.cy)
                .join(Annotation, Annotation.image_id == Image.id)
                .where(
                    Image.dataset_id == target_id,
                    Annotation.cx == 0.9375,
                )
            )
        ).one()
        assert copied_edits == (True, 0.9375, 0.1875)
        usage_after = await session.get(WorkspaceStorage, 1)
        assert usage_after is not None
        assert usage_after.bytes_used == bytes_before


async def _merged_consolidation_fixture(
    client: httpx.AsyncClient,
    app,
    *,
    suffix: str,
) -> tuple[int, list[int]]:
    project = await client.post(
        "/api/projects",
        json={"name": f"test-merge-{suffix}-{uuid4().hex}", "classes": []},
    )
    assert project.status_code == 201
    project_id = project.json()["id"]
    source_ids = [
        await create_ready_dataset(
            client,
            app,
            project_id=project_id,
            suffix=f"{suffix}-source-{index}",
            classes={0: "person"},
            annotation_class_id=0,
            content=f"{suffix}-{index}".encode(),
        )
        for index in range(6)
    ]
    merged_ids: list[int] = []
    for role, start in (("target", 0), ("loser-a", 2), ("loser-b", 4)):
        response = await client.post(
            "/api/datasets/merge",
            json={
                "name": f"test-merge-{suffix}-{role}-{uuid4().hex}",
                "dataset_ids": source_ids[start : start + 2],
            },
        )
        assert response.status_code == 201
        merged_ids.append(response.json()["id"])
    return merged_ids[0], merged_ids[1:]


async def test_extend_merged_dataset_stages_all_losing_merges_in_one_request_scope(
    client: httpx.AsyncClient,
    app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target_id, losing_ids = await _merged_consolidation_fixture(
        client,
        app,
        suffix="bulk-stage",
    )
    async with app.state.session_factory() as session:
        losing_rows = [
            await session.get(Dataset, dataset_id) for dataset_id in losing_ids
        ]
        assert all(row is not None for row in losing_rows)
        losing_roots = {
            contained_storage_path(app.state.settings.storage_dir, row.storage_path)
            for row in losing_rows
            if row is not None
        }

    observations: list[tuple[set[Path], set[Path]]] = []

    async def observe_request_scope(root: Path, stored_paths) -> list:
        paths = tuple(stored_paths)
        staged = await real_stage_deletions_async(root, paths)
        observations.append(
            (
                {contained_storage_path(root, path) for path in paths},
                {
                    item.quarantine
                    for item in staged
                    if item is not None
                },
            )
        )
        return staged

    monkeypatch.setattr(
        dataset_merge_service,
        "stage_deletions_async",
        observe_request_scope,
    )

    response = await client.post(
        f"/api/datasets/{target_id}/merge-sources",
        json={"dataset_ids": losing_ids},
    )

    assert response.status_code == 200
    assert len(observations) == 1
    observed_roots, quarantine_scopes = observations[0]
    assert observed_roots == losing_roots
    assert len(quarantine_scopes) == 1


async def test_extend_merged_dataset_cancellation_restores_files_before_failed_rollback(
    client: httpx.AsyncClient,
    app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target_id, losing_ids = await _merged_consolidation_fixture(
        client,
        app,
        suffix="cancel-restore",
    )
    async with app.state.session_factory() as session:
        target = await session.get(Dataset, target_id)
        losing_rows = [
            await session.get(Dataset, dataset_id) for dataset_id in losing_ids
        ]
        assert target is not None
        assert all(row is not None for row in losing_rows)
        target_root = contained_storage_path(
            app.state.settings.storage_dir,
            target.storage_path,
        )
        losing_roots = [
            contained_storage_path(app.state.settings.storage_dir, row.storage_path)
            for row in losing_rows
            if row is not None
        ]
    target_files_before = {
        path.relative_to(target_root)
        for path in target_root.rglob("*")
        if path.is_file()
    }
    markers = []
    for index, root in enumerate(losing_roots):
        marker = root / f"rollback-marker-{index}.bin"
        marker.write_bytes(f"loser-{index}".encode())
        markers.append(marker)

    rollback_attempted = False
    async with app.state.session_factory() as session:
        async def cancel_commit() -> None:
            await session.flush()
            raise asyncio.CancelledError

        async def fail_rollback() -> None:
            nonlocal rollback_attempted
            rollback_attempted = True
            raise RuntimeError("forced merge rollback failure")

        with monkeypatch.context() as patch:
            patch.setattr(session, "commit", cancel_commit)
            patch.setattr(session, "rollback", fail_rollback)
            with pytest.raises(asyncio.CancelledError):
                await dataset_merge_service.extend_merged_dataset(
                    app.state.settings,
                    session,
                    merged_dataset_id=target_id,
                    dataset_ids=losing_ids,
                    workspace_id=1,
                )
        await session.rollback()

    assert rollback_attempted is True
    assert {
        path.relative_to(target_root)
        for path in target_root.rglob("*")
        if path.is_file()
    } == target_files_before
    assert [marker.read_bytes() for marker in markers] == [b"loser-0", b"loser-1"]
    pending_root = app.state.settings.storage_dir / ".delete-pending"
    assert not pending_root.exists() or not any(pending_root.iterdir())
    async with app.state.session_factory() as session:
        target = await session.get(Dataset, target_id)
        assert target is not None
        assert target.image_count == 2
        losing_rows = [
            await session.get(Dataset, dataset_id) for dataset_id in losing_ids
        ]
        assert all(row is not None for row in losing_rows)


async def test_extend_merged_dataset_rejects_losing_merge_with_active_training(
    client: httpx.AsyncClient,
    app,
) -> None:
    project = await client.post(
        "/api/projects",
        json={"name": f"test-merge-active-run-{uuid4().hex}", "classes": []},
    )
    project_id = project.json()["id"]
    source_ids = [
        await create_ready_dataset(
            client,
            app,
            project_id=project_id,
            suffix=f"active-run-{index}",
            classes={0: "person"},
            annotation_class_id=0,
            content=f"active-run-{index}".encode(),
        )
        for index in range(4)
    ]
    target = await client.post(
        "/api/datasets/merge",
        json={
            "name": f"test-merge-active-target-{uuid4().hex}",
            "dataset_ids": source_ids[:2],
        },
    )
    losing = await client.post(
        "/api/datasets/merge",
        json={
            "name": f"test-merge-active-loser-{uuid4().hex}",
            "dataset_ids": source_ids[2:],
        },
    )
    assert target.status_code == 201
    assert losing.status_code == 201
    target_id = target.json()["id"]
    losing_id = losing.json()["id"]

    async with app.state.session_factory() as session:
        session.add(
            TrainingRun(
                workspace_id=1,
                created_by=1,
                dataset_id=losing_id,
                dataset_name=losing.json()["name"],
                weights="yolo26n.pt",
                epochs=1,
                imgsz=64,
                batch=1,
                split_mode="2way",
                ratios={"train": 0.8, "valid": 0.2},
                seed=7,
                state="running",
                out_dir=storage_relative_path(
                    app.state.settings.storage_dir,
                    app.state.settings.storage_dir
                    / "training-runs"
                    / f"test-merge-active-{uuid4().hex}",
                ),
            )
        )
        await session.commit()

    response = await client.post(
        f"/api/datasets/{target_id}/merge-sources",
        json={"dataset_ids": [losing_id]},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == (
        "진행 중인 학습이 참조하는 병합 데이터셋은 통합할 수 없습니다."
    )
    async with app.state.session_factory() as session:
        assert await session.get(Dataset, losing_id) is not None
        target_memberships = await session.scalar(
            select(func.count(DatasetMergeSource.source_dataset_id)).where(
                DatasetMergeSource.merged_dataset_id == target_id
            )
        )
        losing_memberships = await session.scalar(
            select(func.count(DatasetMergeSource.source_dataset_id)).where(
                DatasetMergeSource.merged_dataset_id == losing_id
            )
        )
    assert target_memberships == 2
    assert losing_memberships == 2


async def test_extend_merged_dataset_rejects_invalid_target_and_cross_project_source(
    client: httpx.AsyncClient,
    app,
    auth_headers,
) -> None:
    first_project = await client.post(
        "/api/projects",
        json={"name": f"test-merge-extend-project-a-{uuid4().hex}", "classes": []},
    )
    second_project = await client.post(
        "/api/projects",
        json={"name": f"test-merge-extend-project-b-{uuid4().hex}", "classes": []},
    )
    first_id = await create_ready_dataset(
        client,
        app,
        project_id=first_project.json()["id"],
        suffix="invalid-target",
        classes={0: "person"},
        annotation_class_id=0,
        content=b"first",
    )
    cross_project_id = await create_ready_dataset(
        client,
        app,
        project_id=second_project.json()["id"],
        suffix="cross-project",
        classes={0: "person"},
        annotation_class_id=0,
        content=b"second",
    )
    mate_id = await create_ready_dataset(
        client,
        app,
        project_id=first_project.json()["id"],
        suffix="target-mate",
        classes={0: "person"},
        annotation_class_id=0,
        content=b"mate",
    )
    merged = await client.post(
        "/api/datasets/merge",
        json={
            "name": f"test-merge-extend-valid-target-{uuid4().hex}",
            "dataset_ids": [first_id, mate_id],
        },
    )
    assert merged.status_code == 201
    merged_id = merged.json()["id"]

    normal_target = await client.post(
        f"/api/datasets/{first_id}/merge-sources",
        json={"dataset_ids": [cross_project_id]},
    )
    self_source = await client.post(
        f"/api/datasets/{first_id}/merge-sources",
        json={"dataset_ids": [first_id]},
    )
    foreign_owner = await client.post(
        f"/api/datasets/{merged_id}/merge-sources",
        headers=auth_headers(2),
        json={"dataset_ids": [cross_project_id]},
    )
    cross_project = await client.post(
        f"/api/datasets/{merged_id}/merge-sources",
        json={"dataset_ids": [cross_project_id]},
    )

    assert normal_target.status_code == 409
    assert normal_target.json()["detail"] == "병합 데이터셋만 확장할 수 있습니다."
    assert self_source.status_code == 422
    assert foreign_owner.status_code == 404
    assert foreign_owner.json()["detail"] == "선택한 데이터셋을 찾을 수 없습니다."
    assert cross_project.status_code == 409
    assert cross_project.json()["detail"] == "같은 프로젝트의 데이터셋만 합칠 수 있습니다."


@pytest.mark.parametrize("operation", ["merge", "extract"])
async def test_new_snapshot_labels_preserve_content_without_copying_assignment_or_actor_history(
    client: httpx.AsyncClient, app, operation: str,
) -> None:
    project = await client.post("/api/projects", json={
        "name": f"test-merge-label-state-{uuid4().hex}",
        "classes": [{"name": "person", "color": "#EF4444"}],
    })
    assert project.status_code == 201, project.text
    project_id = project.json()["id"]
    source_ids = [
        await create_ready_dataset(
            client, app, project_id=project_id, suffix=f"label-state-{index}",
            classes={0: "person"}, annotation_class_id=0,
            content=f"label-state-{index}".encode(), count=5 if index == 0 else 1,
        ) for index in range(2)
    ]
    timestamp = datetime(2026, 10, 14, tzinfo=timezone.utc)
    # Positive boxes, explicit empty completion, missing labels, empty active
    # work, and an imported empty label exercise distinct snapshot semantics.
    cases = [
        (False, 1, "in_progress", True),
        (False, 0, "labeled", True),
        (False, 0, "unlabeled", False),
        (False, 0, "in_progress", True),
        (True, 0, "labeled", False),
    ]
    def snapshot(image: Image) -> tuple:
        return (image.has_label_source, image.box_count, image.is_modified,
                image.label_status, image.assignee_id, image.assigned_at,
                image.labeled_by, image.labeled_at, image.version)

    async with app.state.session_factory() as session:
        first_images = list(await session.scalars(select(Image).where(
            Image.dataset_id == source_ids[0]).order_by(Image.id)))
        assert len(first_images) == len(cases)
        for image, (has_source, count, status, modified) in zip(first_images, cases, strict=True):
            image.has_label_source = has_source
            image.box_count = count
            image.is_modified = modified
            image.label_status = status
            image.assignee_id = 1 if status == "in_progress" else None
            image.assigned_at = timestamp if status == "in_progress" else None
            image.labeled_by = 1 if status == "labeled" and modified else None
            image.labeled_at = timestamp if image.labeled_by is not None else None
            image.version = 7 if modified else 0
            if count == 0:
                await session.execute(delete(Annotation).where(Annotation.image_id == image.id))
        first_dataset = await session.get(Dataset, source_ids[0])
        first_dataset.annotation_count = sum(case[1] for case in cases)
        second_image = await session.scalar(select(Image).where(Image.dataset_id == source_ids[1]))
        assert second_image is not None
        second_image.label_status = "labeled"
        second_image.labeled_by = 1
        second_image.labeled_at = timestamp
        second_image.version = 9
        await session.commit()
        sources = [*first_images, second_image]
        before = {image.id: snapshot(image) for image in sources}
        expected_labels = ("labeled", "labeled", "unlabeled", "unlabeled", "labeled", "labeled")
        expected = {
            image.stem: (status, image.box_count, image.has_label_source, image.is_modified)
            for image, status in zip(sources, expected_labels, strict=True)
            if operation == "merge" or image.box_count > 0
        }

    body = {"name": f"test-merge-label-snapshot-{operation}-{uuid4().hex}", "dataset_ids": source_ids}
    if operation == "extract":
        body["class_ids"] = [0]
    response = await client.post(f"/api/datasets/{operation}", json=body)
    assert response.status_code == 201, response.text
    target_id = response.json()["id"]
    async with app.state.session_factory() as session:
        copies = list(await session.scalars(select(Image).where(Image.dataset_id == target_id)))
        assert {image.stem for image in copies} == set(expected)
        for image in copies:
            assert (image.label_status, image.box_count, image.has_label_source,
                    image.is_modified) == expected[image.stem]
            assert (image.assignee_id, image.assigned_at, image.labeled_by,
                    image.labeled_at, image.version) == (None, None, None, None, 0)
        unchanged_sources = list(await session.scalars(select(Image).where(Image.id.in_(before))))
        assert {image.id: snapshot(image) for image in unchanged_sources} == before
        assert await session.scalar(select(func.count()).select_from(Annotation).join(
            Image, Annotation.image_id == Image.id).where(Image.dataset_id == target_id)) == 2
