from __future__ import annotations

import errno
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
import yaml
from sqlalchemy import func, select

from app.models import (
    Annotation,
    Dataset,
    DatasetClass,
    DatasetMergeSource,
    Image,
    RunImage,
    TrainingRun,
    UserStorage,
)
from app.services import dataset_merge as dataset_merge_service
from app.services import training
from app.services.quota import increase_bytes_used
from app.services.storage import contained_storage_path, storage_relative_path
from tests.factories import image_with_media


pytestmark = pytest.mark.asyncio


def _host_ready(monkeypatch: pytest.MonkeyPatch) -> None:
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


async def _create_project(
    client: httpx.AsyncClient,
    *,
    owner_headers: dict[str, str] | None = None,
) -> int:
    response = await client.post(
        "/api/projects",
        headers=owner_headers,
        json={
            "name": f"test-extract-project-{uuid4().hex}",
            "classes": [
                {"name": "person", "color": "#112233"},
                {"name": "vehicle", "color": "#445566"},
                {"name": "helmet", "color": "#778899"},
            ],
        },
    )
    assert response.status_code == 201, response.text
    return int(response.json()["id"])


async def _create_dataset(
    client: httpx.AsyncClient,
    app,
    *,
    project_id: int,
    images: list[tuple[str, list[int], bytes]],
    status: str = "ready",
    owner_headers: dict[str, str] | None = None,
) -> int:
    response = await client.post(
        "/api/datasets",
        headers=owner_headers,
        json={
            "name": f"test-extract-source-{uuid4().hex}",
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
        annotation_count = 0
        physical_bytes = 0
        for stem, class_ids, content in images:
            original = root / f"{stem}.jpg"
            thumbnail = root / f"{stem}-thumb.jpg"
            original.write_bytes(content)
            thumbnail.write_bytes(b"thumb-" + content)
            physical_bytes += original.stat().st_size + thumbnail.stat().st_size
            image = image_with_media(
                owner_id=dataset.owner_id,
                dataset_id=dataset_id,
                stem=stem,
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
                thumb_bytes=thumbnail.stat().st_size,
                box_count=len(class_ids),
                is_modified=True,
            )
            image.annotations.extend(
                [
                    Annotation(
                        class_id=class_id,
                        cx=0.5,
                        cy=0.5,
                        w=0.25,
                        h=0.25,
                    )
                    for class_id in class_ids
                ]
            )
            session.add(image)
            annotation_count += len(class_ids)
        dataset.status = status
        dataset.image_count = len(images)
        dataset.annotation_count = annotation_count
        dataset.class_count = 3
        await increase_bytes_used(session, dataset.owner_id, physical_bytes)
        await session.commit()
    return dataset_id


async def test_extract_creates_visible_independent_filtered_snapshot(
    client: httpx.AsyncClient,
    app,
) -> None:
    project_id = await _create_project(client)
    first_id = await _create_dataset(
        client,
        app,
        project_id=project_id,
        images=[
            ("same", [0, 2], b"first-selected"),
            ("person-only", [0], b"first-unselected"),
        ],
    )
    second_id = await _create_dataset(
        client,
        app,
        project_id=project_id,
        images=[("same", [1, 2, 2], b"second-selected")],
    )
    async with app.state.session_factory() as session:
        source_usage = await session.get(UserStorage, 1)
        assert source_usage is not None
        source_physical_bytes = source_usage.bytes_used
    result_name = f"test-extract-result-{uuid4().hex}"

    response = await client.post(
        "/api/datasets/extract",
        json={
            "name": result_name,
            "dataset_ids": [first_id, second_id],
            "class_ids": [2],
        },
    )

    assert response.status_code == 201, response.text
    extracted = response.json()
    extracted_id = int(extracted["id"])
    assert extracted == {
        "id": extracted_id,
        "project_id": project_id,
        "name": result_name,
        "image_count": 2,
        "annotation_count": 3,
        "class_count": 1,
        "created_at": extracted["created_at"],
        "status": "ready",
        "is_merged": False,
    }

    classes = await client.get(f"/api/datasets/{extracted_id}/classes")
    assert classes.status_code == 200
    assert classes.json() == {
        "classes": [
            {"class_id": 2, "name": "helmet"},
        ]
    }

    async with app.state.session_factory() as session:
        images = list(
            (
                await session.scalars(
                    select(Image)
                    .where(Image.dataset_id == extracted_id)
                    .order_by(Image.stem)
                )
            ).all()
        )
        annotations = list(
            (
                await session.execute(
                    select(Image.stem, Annotation.class_id)
                    .join(Annotation, Annotation.image_id == Image.id)
                    .where(Image.dataset_id == extracted_id)
                    .order_by(Image.stem, Annotation.id)
                )
            ).all()
        )
        memberships = await session.scalar(
            select(func.count())
            .select_from(DatasetMergeSource)
            .where(DatasetMergeSource.merged_dataset_id == extracted_id)
        )
        usage = await session.get(UserStorage, 1)
        extracted_row = await session.get(Dataset, extracted_id)

    assert [image.stem for image in images] == ["same", "same (1)"]
    assert [image.box_count for image in images] == [1, 2]
    assert annotations == [("same", 2), ("same (1)", 2), ("same (1)", 2)]
    assert memberships == 0
    assert extracted_row is not None and extracted_row.is_extracted is True
    assert all(not Path(image.file_path).is_absolute() for image in images)
    assert all(
        contained_storage_path(
            app.state.settings.storage_dir,
            image.file_path,
        ).is_file()
        for image in images
    )
    extracted_bytes = sum(
        image.original_bytes + image.display_bytes + image.thumb_bytes
        for image in images
    )
    assert extracted_bytes > 0
    assert usage is not None and usage.bytes_used == source_physical_bytes

    listing = await client.get("/api/datasets?offset=0&limit=200")
    assert listing.status_code == 200
    listed_ids = {int(item["id"]) for item in listing.json()["items"]}
    assert {first_id, second_id, extracted_id} <= listed_ids
    assert next(
        item
        for item in listing.json()["items"]
        if int(item["id"]) == extracted_id
    )["source_datasets"] == []

    projects = await client.get("/api/projects")
    assert projects.status_code == 200
    project = next(
        item
        for item in projects.json()["items"]
        if int(item["id"]) == project_id
    )
    assert {int(item["id"]) for item in project["datasets"]} >= {
        first_id,
        second_id,
        extracted_id,
    }

    rename = await client.patch(
        f"/api/projects/{project_id}/classes/1",
        json={"name": "truck"},
    )
    assert rename.status_code == 200, rename.text
    renamed_classes = await client.get(
        f"/api/datasets/{extracted_id}/classes"
    )
    assert renamed_classes.json() == {
        "classes": [{"class_id": 2, "name": "helmet"}]
    }

    selected_rename = await client.patch(
        f"/api/projects/{project_id}/classes/2",
        json={"name": "hardhat"},
    )
    assert selected_rename.status_code == 200, selected_rename.text
    renamed_classes = await client.get(
        f"/api/datasets/{extracted_id}/classes"
    )
    assert renamed_classes.json() == {
        "classes": [{"class_id": 2, "name": "hardhat"}]
    }

    source_paths = []
    async with app.state.session_factory() as session:
        for source_id in (first_id, second_id):
            source = await session.get(Dataset, source_id)
            assert source is not None
            source_paths.append(
                contained_storage_path(
                    app.state.settings.storage_dir,
                    source.storage_path,
                )
            )

    deleted = await client.delete(f"/api/datasets/{extracted_id}")
    assert deleted.status_code == 204, deleted.text
    async with app.state.session_factory() as session:
        assert await session.get(Dataset, first_id) is not None
        assert await session.get(Dataset, second_id) is not None
        usage = await session.get(UserStorage, 1)
    assert all(path.is_dir() for path in source_paths)
    assert usage is not None and usage.bytes_used == source_physical_bytes


async def test_extract_rejects_invalid_sources_classes_and_empty_result(
    client: httpx.AsyncClient,
    app,
) -> None:
    project_id = await _create_project(client)
    pending_id = await _create_dataset(
        client,
        app,
        project_id=project_id,
        images=[("pending", [2], b"pending")],
        status="pending",
    )
    ready_id = await _create_dataset(
        client,
        app,
        project_id=project_id,
        images=[("person", [0], b"person")],
    )
    other_project_id = await _create_project(client)
    other_id = await _create_dataset(
        client,
        app,
        project_id=other_project_id,
        images=[("helmet", [2], b"helmet")],
    )

    cases = [
        ([pending_id], [2], 409),
        ([ready_id, other_id], [2], 409),
        ([ready_id], [999], 404),
        ([ready_id], [2], 409),
        ([999_999_999], [2], 404),
    ]
    for index, (dataset_ids, class_ids, expected_status) in enumerate(cases):
        response = await client.post(
            "/api/datasets/extract",
            json={
                "name": f"test-extract-invalid-{index}-{uuid4().hex}",
                "dataset_ids": dataset_ids,
                "class_ids": class_ids,
            },
        )
        assert response.status_code == expected_status, response.text

    for body in (
        {"name": "test-extract-empty", "dataset_ids": [], "class_ids": [2]},
        {
            "name": "test-extract-duplicate-dataset",
            "dataset_ids": [ready_id, ready_id],
            "class_ids": [2],
        },
        {
            "name": "test-extract-duplicate-class",
            "dataset_ids": [ready_id],
            "class_ids": [2, 2],
        },
        {
            "name": "test-extract-negative-class",
            "dataset_ids": [ready_id],
            "class_ids": [-1],
        },
    ):
        response = await client.post("/api/datasets/extract", json=body)
        assert response.status_code == 422, response.text


async def test_extract_keeps_only_selected_catalog_for_training(
    client: httpx.AsyncClient,
    app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _host_ready(monkeypatch)
    project_id = await _create_project(client)
    source_id = await _create_dataset(
        client,
        app,
        project_id=project_id,
        images=[
            (f"helmet-{index}", [2], f"helmet-{index}".encode())
            for index in range(10)
        ],
    )
    extracted = await client.post(
        "/api/datasets/extract",
        json={
            "name": f"test-extract-train-{uuid4().hex}",
            "dataset_ids": [source_id],
            "class_ids": [2],
        },
    )
    assert extracted.status_code == 201, extracted.text
    extracted_id = int(extracted.json()["id"])

    submitted = await client.post(
        f"/api/datasets/{extracted_id}/train",
        json={
            "weights": "yolo26n.pt",
            "epochs": 3,
            "imgsz": 640,
            "batch": -1,
        },
    )

    assert submitted.status_code == 201, submitted.text
    async with app.state.session_factory() as session:
        run = await session.get(TrainingRun, submitted.json()["run_id"])
        assert run is not None and run.dataset_id == extracted_id
        run_root = contained_storage_path(
            app.state.settings.storage_dir,
            run.out_dir,
        )
        run_image_count = await session.scalar(
            select(func.count(RunImage.id)).where(RunImage.run_id == run.id)
        )
        class_ids = list(
            (
                await session.scalars(
                    select(DatasetClass.class_id)
                    .where(DatasetClass.dataset_id == extracted_id)
                    .order_by(DatasetClass.class_id)
                )
            ).all()
        )
    assert run_image_count == 10
    assert class_ids == [2]
    data = yaml.safe_load(
        (run_root / "workdir" / "data.yaml").read_text(encoding="utf-8")
    )
    assert data["names"] == {0: "helmet"}
    labels = sorted((run_root / "workdir" / "labels").rglob("*.txt"))
    assert len(labels) == 10
    assert all(
        line.startswith("0 ")
        for label in labels
        for line in label.read_text(encoding="utf-8").splitlines()
    )


async def test_extract_hides_foreign_dataset_existence(
    client: httpx.AsyncClient,
    app,
    auth_headers,
) -> None:
    foreign_headers = auth_headers(73_001)
    project_id = await _create_project(client, owner_headers=foreign_headers)
    foreign_dataset_id = await _create_dataset(
        client,
        app,
        project_id=project_id,
        images=[("foreign", [2], b"foreign")],
        owner_headers=foreign_headers,
    )

    response = await client.post(
        "/api/datasets/extract",
        json={
            "name": f"test-extract-foreign-{uuid4().hex}",
            "dataset_ids": [foreign_dataset_id],
            "class_ids": [2],
        },
    )

    assert response.status_code == 404, response.text


async def test_extract_rejects_quota_without_persisting_partial_output(
    client: httpx.AsyncClient,
    app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app.state.settings = app.state.settings.model_copy(
        update={"quota_bytes_per_user": 1}
    )
    project_id = await _create_project(client)
    source_id = await _create_dataset(
        client,
        app,
        project_id=project_id,
        images=[("large", [2], b"larger-than-one-byte")],
    )
    async with app.state.session_factory() as session:
        source_usage = await session.get(UserStorage, 1)
        assert source_usage is not None
        source_physical_bytes = source_usage.bytes_used

    def fail_link(_source: Path, _target: Path) -> None:
        raise OSError(errno.EXDEV, "cross-device link")

    monkeypatch.setattr(dataset_merge_service.os, "link", fail_link)
    result_name = f"test-extract-quota-{uuid4().hex}"
    datasets_root = app.state.settings.storage_dir / "datasets"
    storage_entries_before = set(datasets_root.iterdir())

    response = await client.post(
        "/api/datasets/extract",
        json={
            "name": result_name,
            "dataset_ids": [source_id],
            "class_ids": [2],
        },
    )

    assert response.status_code == 413, response.text
    assert "잔여" in response.json()["detail"]
    assert "필요" in response.json()["detail"]
    async with app.state.session_factory() as session:
        output = await session.scalar(
            select(Dataset).where(Dataset.name == result_name)
        )
        usage = await session.get(UserStorage, 1)
    assert output is None
    assert usage is not None and usage.bytes_used == source_physical_bytes
    assert set(datasets_root.iterdir()) == storage_entries_before
