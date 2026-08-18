from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import delete, event, func, select

from app.models import (
    Annotation,
    Dataset,
    DatasetClass,
    DatasetMergeSource,
    Image,
    ProjectClass,
    RunImage,
    TrainingRun,
    UserStorage,
)
from app.services import dataset_merge as dataset_merge_service
from app.services import training
from app.services.storage import (
    contained_storage_path,
    stage_deletions_async as real_stage_deletions_async,
    storage_relative_path,
)


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
        for index in range(count):
            stem = "same" if count == 1 else f"same-{index}"
            original = root / f"{stem}.jpg"
            thumbnail = root / f"{stem}-thumb.jpg"
            original.write_bytes(content + f"-{index}".encode())
            thumbnail.write_bytes(b"thumb-" + content + f"-{index}".encode())
            image = Image(
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
    assert [source["id"] for source in merged["source_datasets"]] == [
        first_id,
        second_id,
    ]

    classes = await client.get(f"/api/datasets/{merged_id}/classes")
    assert classes.json() == {
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
        annotations = (
            await session.execute(
                select(Image.stem, Annotation.class_id)
                .join(Annotation, Annotation.image_id == Image.id)
                .where(Image.dataset_id == merged_id)
            )
        ).all()
        usage = await session.get(UserStorage, 1)

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
        usage = await session.get(UserStorage, 1)
        assert usage is not None and usage.bytes_used == 0
        remaining_ids = (await session.scalars(select(Dataset.id))).all()
        assert remaining_ids == []
    # 병합 데이터셋 삭제는 숨겨진 원본까지 함께 지운다 — 복원되지 않는다.
    after_listing = await client.get("/api/datasets?offset=0&limit=200")
    assert after_listing.json()["total"] == 0
    assert after_listing.json()["items"] == []


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
    assert classes.json() == {
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
    assert renamed_classes.json() == {
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

    async with app.state.session_factory() as session:
        dataset_count_before = await session.scalar(
            select(func.count(Dataset.id))
        )
        membership_count_before = await session.scalar(
            select(func.count()).select_from(DatasetMergeSource)
        )
        usage_before = await session.get(UserStorage, 1)
        assert usage_before is not None
        bytes_before = usage_before.bytes_used
    dataset_root = app.state.settings.storage_dir / "datasets"
    directories_before = {path.name for path in dataset_root.iterdir()}

    reused = await client.post(
        "/api/datasets/merge",
        json={
            "name": f"test-merge-reuse-ignored-{uuid4().hex}",
            "dataset_ids": [second_id, first_id],
        },
    )

    assert reused.status_code == 200
    assert reused.json()["id"] == merged_id
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
        usage_after = await session.get(UserStorage, 1)
        assert usage_after is not None and usage_after.bytes_used == bytes_before
    assert {path.name for path in dataset_root.iterdir()} == directories_before


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
        usage = await session.get(UserStorage, 1)
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
        usage_after = await session.get(UserStorage, 1)
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
        run_image_count = await session.scalar(
            select(func.count(RunImage.id)).where(RunImage.run_id == run.id)
        )
    assert run_image_count == merged.json()["image_count"]


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

    assert too_few.status_code == 422
    assert duplicated.status_code == 422


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
        usage_before = await session.get(UserStorage, 1)
        assert usage_before is not None
        bytes_before = usage_before.bytes_used

    extended = await client.post(
        f"/api/datasets/{target_id}/merge-sources",
        json={"dataset_ids": [source_ids[2]]},
    )

    assert extended.status_code == 200
    result = extended.json()
    assert result["id"] == target_id
    assert result["image_count"] == 3
    assert result["annotation_count"] == 3
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
        appended_bytes = (
            appended.original_bytes + appended.display_bytes + appended.thumb_bytes
        )
        usage_after = await session.get(UserStorage, 1)
        assert usage_after is not None
        assert usage_after.bytes_used == bytes_before + appended_bytes

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
        usage_before = await session.get(UserStorage, 1)
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
        usage_after = await session.get(UserStorage, 1)
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
                    owner_id=1,
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
                owner_id=1,
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
