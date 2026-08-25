from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from app.models import Annotation, Dataset, ExportArtifact, Image, UploadJob
from tests.factories import image_with_media


pytestmark = pytest.mark.asyncio


def unique_name(suffix: str) -> str:
    return f"test-{suffix}-{uuid4().hex}"


async def test_project_creation_keeps_the_project_empty_and_persists_classes(
    client: httpx.AsyncClient,
) -> None:
    project_name = unique_name("empty-project")

    created = await client.post(
        "/api/projects",
        json={
            "name": project_name,
            "classes": [
                {"name": "person", "color": "#EF4444"},
                {"name": "car", "color": "#F59E0B"},
            ],
        },
    )

    assert created.status_code == 201
    body = created.json()
    assert body["name"] == project_name
    assert body["dataset_count"] == 0
    assert body["datasets"] == []
    assert body["classes"] == [
        {"class_id": 0, "name": "person", "color": "#EF4444"},
        {"class_id": 1, "name": "car", "color": "#F59E0B"},
    ]

    datasets = await client.get("/api/datasets?offset=0&limit=200")
    assert datasets.status_code == 200
    assert all(row["name"] != project_name for row in datasets.json()["items"])


async def test_dataset_is_created_inside_a_project_and_inherits_project_classes(
    client: httpx.AsyncClient,
) -> None:
    project_name = unique_name("dataset-parent")
    dataset_name = unique_name("child-dataset")
    project_response = await client.post(
        "/api/projects",
        json={
            "name": project_name,
            "classes": [
                {"name": "person", "color": "#22C55E"},
                {"name": "vehicle", "color": "#3B82F6"},
            ],
        },
    )
    project_id = project_response.json()["id"]

    created = await client.post(
        "/api/datasets",
        json={"name": dataset_name, "project_id": project_id},
    )

    assert created.status_code == 201
    dataset = created.json()
    assert dataset["project_id"] == project_id
    classes = await client.get(f"/api/datasets/{dataset['id']}/classes")
    assert classes.status_code == 200
    assert classes.json() == {
        "classes": [
            {"class_id": 0, "name": "person"},
            {"class_id": 1, "name": "vehicle"},
        ]
    }

    listing = await client.get("/api/projects")
    assert listing.status_code == 200
    project = next(
        row for row in listing.json()["items"] if row["id"] == project_id
    )
    assert project["dataset_count"] == 1
    assert [row["id"] for row in project["datasets"]] == [dataset["id"]]
    assert project["datasets"][0]["project_id"] == project_id


async def test_project_dataset_rows_report_actual_accounted_storage_bytes(
    client: httpx.AsyncClient,
    app,
) -> None:
    project_response = await client.post(
        "/api/projects",
        json={"name": unique_name("dataset-storage"), "classes": []},
    )
    project_id = project_response.json()["id"]
    dataset_response = await client.post(
        "/api/datasets",
        json={
            "name": unique_name("dataset-storage-child"),
            "project_id": project_id,
        },
    )
    dataset_id = dataset_response.json()["id"]

    async with app.state.session_factory() as session:
        dataset = await session.get(Dataset, dataset_id)
        assert dataset is not None
        dataset.status = "ready"
        dataset.image_count = 1
        session.add(
            image_with_media(
                owner_id=dataset.owner_id,
                dataset_id=dataset_id,
                stem="storage-sample",
                filename="storage-sample.jpg",
                rel_path="images/storage-sample.jpg",
                split="train",
                width=32,
                height=24,
                file_path="datasets/storage-sample.jpg",
                display_path="datasets/storage-sample.display.jpg",
                thumb_path="datasets/storage-sample.thumb.jpg",
                original_bytes=1_024,
                display_bytes=512,
                thumb_bytes=256,
            )
        )
        export_job = UploadJob(
            dataset_id=dataset_id,
            kind="export",
            state="done",
            phase="done",
            total=1,
            processed=1,
            failed=0,
        )
        session.add(export_job)
        await session.flush()
        session.add(
            ExportArtifact(
                job_id=export_job.id,
                dataset_id=dataset_id,
                archive_path="datasets/storage-sample.zip",
                archive_bytes=128,
            )
        )
        await session.commit()

    listing = await client.get(f"/api/projects/{project_id}")

    assert listing.status_code == 200, listing.text
    dataset_row = listing.json()["datasets"][0]
    assert dataset_row["id"] == dataset_id
    assert dataset_row["storage_bytes"] == 1_920
    assert dataset_row["physical_storage_bytes"] == 1_920


async def test_selected_dataset_class_image_counts_count_each_image_once(
    client: httpx.AsyncClient,
    app,
) -> None:
    project_response = await client.post(
        "/api/projects",
        json={
            "name": unique_name("class-image-counts"),
            "classes": [
                {"name": "person", "color": "#EF4444"},
                {"name": "vehicle", "color": "#3B82F6"},
                {"name": "unused", "color": "#84CC16"},
            ],
        },
    )
    project_id = project_response.json()["id"]
    first_response = await client.post(
        "/api/datasets",
        json={"name": unique_name("class-images-first"), "project_id": project_id},
    )
    second_response = await client.post(
        "/api/datasets",
        json={"name": unique_name("class-images-second"), "project_id": project_id},
    )
    first_id = first_response.json()["id"]
    second_id = second_response.json()["id"]

    async with app.state.session_factory() as session:
        first = await session.get(Dataset, first_id)
        second = await session.get(Dataset, second_id)
        assert first is not None
        assert second is not None
        first.status = "ready"
        second.status = "ready"
        first.image_count = 2
        second.image_count = 1
        images = [
            image_with_media(
                owner_id=first.owner_id,
                dataset_id=first_id,
                stem="first-a",
                filename="first-a.jpg",
                rel_path="first-a.jpg",
                split=None,
                width=32,
                height=32,
                file_path="first-a.jpg",
                display_path=None,
                thumb_path="first-a-thumb.jpg",
                box_count=3,
            ),
            image_with_media(
                owner_id=first.owner_id,
                dataset_id=first_id,
                stem="first-b",
                filename="first-b.jpg",
                rel_path="first-b.jpg",
                split=None,
                width=32,
                height=32,
                file_path="first-b.jpg",
                display_path=None,
                thumb_path="first-b-thumb.jpg",
                box_count=1,
            ),
            image_with_media(
                owner_id=second.owner_id,
                dataset_id=second_id,
                stem="second-a",
                filename="second-a.jpg",
                rel_path="second-a.jpg",
                split=None,
                width=32,
                height=32,
                file_path="second-a.jpg",
                display_path=None,
                thumb_path="second-a-thumb.jpg",
                box_count=1,
            ),
        ]
        session.add_all(images)
        await session.flush()
        session.add_all(
            [
                Annotation(
                    image_id=images[0].id,
                    class_id=0,
                    cx=0.2,
                    cy=0.2,
                    w=0.1,
                    h=0.1,
                ),
                Annotation(
                    image_id=images[0].id,
                    class_id=0,
                    cx=0.4,
                    cy=0.4,
                    w=0.1,
                    h=0.1,
                ),
                Annotation(
                    image_id=images[0].id,
                    class_id=1,
                    cx=0.6,
                    cy=0.6,
                    w=0.1,
                    h=0.1,
                ),
                Annotation(
                    image_id=images[1].id,
                    class_id=0,
                    cx=0.5,
                    cy=0.5,
                    w=0.2,
                    h=0.2,
                ),
                Annotation(
                    image_id=images[2].id,
                    class_id=1,
                    cx=0.5,
                    cy=0.5,
                    w=0.2,
                    h=0.2,
                ),
            ]
        )
        await session.commit()

    combined = await client.get(
        f"/api/projects/{project_id}/class-image-counts",
        params=[("dataset_ids", first_id), ("dataset_ids", second_id)],
    )
    second_only = await client.get(
        f"/api/projects/{project_id}/class-image-counts",
        params={"dataset_ids": second_id},
    )

    assert combined.status_code == 200, combined.text
    assert combined.json() == {
        "items": [
            {"class_id": 0, "name": "person", "color": "#EF4444", "image_count": 2},
            {"class_id": 1, "name": "vehicle", "color": "#3B82F6", "image_count": 2},
            {"class_id": 2, "name": "unused", "color": "#84CC16", "image_count": 0},
        ]
    }
    assert second_only.status_code == 200, second_only.text
    assert [item["image_count"] for item in second_only.json()["items"]] == [0, 1, 0]


async def test_class_image_counts_reject_invalid_selection_scope(
    client: httpx.AsyncClient,
    app,
    auth_headers,
) -> None:
    first_project = await client.post(
        "/api/projects",
        json={"name": unique_name("counts-scope-first"), "classes": []},
    )
    second_project = await client.post(
        "/api/projects",
        json={"name": unique_name("counts-scope-second"), "classes": []},
    )
    first_project_id = first_project.json()["id"]
    second_project_id = second_project.json()["id"]
    pending_dataset = await client.post(
        "/api/datasets",
        json={
            "name": unique_name("counts-scope-pending"),
            "project_id": first_project_id,
        },
    )
    other_project_dataset = await client.post(
        "/api/datasets",
        json={
            "name": unique_name("counts-scope-other"),
            "project_id": second_project_id,
        },
    )
    pending_dataset_id = pending_dataset.json()["id"]
    other_project_dataset_id = other_project_dataset.json()["id"]
    endpoint = f"/api/projects/{first_project_id}/class-image-counts"

    pending_response = await client.get(
        endpoint,
        params={"dataset_ids": pending_dataset_id},
    )
    assert pending_response.status_code == 404

    async with app.state.session_factory() as session:
        first = await session.get(Dataset, pending_dataset_id)
        second = await session.get(Dataset, other_project_dataset_id)
        assert first is not None
        assert second is not None
        first.status = "ready"
        second.status = "ready"
        await session.commit()

    wrong_project_response = await client.get(
        endpoint,
        params={"dataset_ids": other_project_dataset_id},
    )
    foreign_owner_response = await client.get(
        endpoint,
        params={"dataset_ids": pending_dataset_id},
        headers=auth_headers(2),
    )
    valid_response = await client.get(
        endpoint,
        params={"dataset_ids": pending_dataset_id},
    )

    assert wrong_project_response.status_code == 404
    assert foreign_owner_response.status_code == 404
    assert valid_response.status_code == 200
    assert valid_response.json() == {"items": []}


async def test_class_image_counts_accept_hidden_merged_source_datasets(
    client: httpx.AsyncClient,
    app,
) -> None:
    project_response = await client.post(
        "/api/projects",
        json={
            "name": unique_name("hidden-source-counts"),
            "classes": [{"name": "person", "color": "#EF4444"}],
        },
    )
    project_id = project_response.json()["id"]
    source_responses = [
        await client.post(
            "/api/datasets",
            json={
                "name": unique_name(f"hidden-source-{index}"),
                "project_id": project_id,
            },
        )
        for index in range(2)
    ]
    source_ids = [response.json()["id"] for response in source_responses]

    async with app.state.session_factory() as session:
        for source_id in source_ids:
            source = await session.get(Dataset, source_id)
            assert source is not None
            source.status = "ready"
        first_source = await session.get(Dataset, source_ids[0])
        assert first_source is not None
        first_source.image_count = 1
        first_source.annotation_count = 1
        first_source.class_count = 1
        storage_root = Path(app.state.settings.storage_dir)
        original_path = storage_root / "hidden-source-image.jpg"
        thumb_path = storage_root / "hidden-source-image-thumb.jpg"
        original_path.write_bytes(b"hidden-source-original")
        thumb_path.write_bytes(b"hidden-source-thumb")
        source_image = image_with_media(
            owner_id=first_source.owner_id,
            dataset_id=source_ids[0],
            stem="hidden-source-image",
            filename="hidden-source-image.jpg",
            rel_path="hidden-source-image.jpg",
            split=None,
            width=32,
            height=32,
            file_path=str(original_path.relative_to(storage_root)),
            display_path=None,
            thumb_path=str(thumb_path.relative_to(storage_root)),
            box_count=1,
        )
        session.add(source_image)
        await session.flush()
        session.add(
            Annotation(
                image_id=source_image.id,
                class_id=0,
                cx=0.5,
                cy=0.5,
                w=0.25,
                h=0.25,
            )
        )
        await session.commit()

    merged = await client.post(
        "/api/datasets/merge",
        json={
            "name": unique_name("hidden-source-merged"),
            "dataset_ids": source_ids,
        },
    )
    assert merged.status_code == 201, merged.text

    listing = await client.get("/api/projects")
    project = next(
        row for row in listing.json()["items"] if row["id"] == project_id
    )
    assert [row["id"] for row in project["datasets"]] == [merged.json()["id"]]

    counts = await client.get(
        f"/api/projects/{project_id}/class-image-counts",
        params={"dataset_ids": source_ids[0]},
    )

    assert counts.status_code == 200, counts.text
    assert counts.json() == {
        "items": [
            {
                "class_id": 0,
                "name": "person",
                "color": "#EF4444",
                "image_count": 1,
            }
        ]
    }


async def test_projects_are_owner_scoped_and_foreign_projects_return_404(
    client: httpx.AsyncClient,
    auth_headers,
) -> None:
    project_name = unique_name("owner-scope")
    created = await client.post(
        "/api/projects",
        json={"name": project_name, "classes": []},
    )
    project_id = created.json()["id"]

    foreign_detail = await client.get(
        f"/api/projects/{project_id}",
        headers=auth_headers(2),
    )
    foreign_dataset = await client.post(
        "/api/datasets",
        headers=auth_headers(2),
        json={"name": unique_name("foreign-child"), "project_id": project_id},
    )
    foreign_listing = await client.get(
        "/api/projects",
        headers=auth_headers(2),
    )

    assert foreign_detail.status_code == 404
    assert foreign_dataset.status_code == 404
    assert all(
        row["id"] != project_id for row in foreign_listing.json()["items"]
    )


async def test_class_rename_updates_the_project_catalog_and_sibling_datasets(
    client: httpx.AsyncClient,
) -> None:
    project_response = await client.post(
        "/api/projects",
        json={
            "name": unique_name("shared-classes"),
            "classes": [{"name": "person", "color": "#8B5CF6"}],
        },
    )
    project_id = project_response.json()["id"]
    first = await client.post(
        "/api/datasets",
        json={"name": unique_name("first-child"), "project_id": project_id},
    )
    second = await client.post(
        "/api/datasets",
        json={"name": unique_name("second-child"), "project_id": project_id},
    )

    renamed = await client.patch(
        f"/api/datasets/{first.json()['id']}/classes/0",
        json={"name": "human"},
    )

    assert renamed.status_code == 200
    detail = await client.get(f"/api/projects/{project_id}")
    sibling_classes = await client.get(
        f"/api/datasets/{second.json()['id']}/classes"
    )
    assert detail.json()["classes"] == [
        {"class_id": 0, "name": "human", "color": "#8B5CF6"}
    ]
    assert sibling_classes.json() == {
        "classes": [{"class_id": 0, "name": "human"}]
    }


async def test_project_class_update_changes_name_color_and_propagates(
    client: httpx.AsyncClient,
) -> None:
    project_response = await client.post(
        "/api/projects",
        json={
            "name": unique_name("catalog-edit"),
            "classes": [
                {"name": "person", "color": "#8B5CF6"},
                {"name": "forklift", "color": "#F59E0B"},
            ],
        },
    )
    project_id = project_response.json()["id"]
    child = await client.post(
        "/api/datasets",
        json={"name": unique_name("catalog-child"), "project_id": project_id},
    )
    child_id = child.json()["id"]

    updated = await client.patch(
        f"/api/projects/{project_id}/classes/0",
        json={"name": "human", "color": "#ef4444"},
    )
    assert updated.status_code == 200
    assert updated.json() == {
        "class_id": 0,
        "name": "human",
        "color": "#EF4444",
    }

    detail = await client.get(f"/api/projects/{project_id}")
    assert detail.json()["classes"][0] == {
        "class_id": 0,
        "name": "human",
        "color": "#EF4444",
    }
    child_classes = await client.get(f"/api/datasets/{child_id}/classes")
    assert {"class_id": 0, "name": "human"} in child_classes.json()["classes"]

    # 색만 변경 — 이름 전파 없음
    color_only = await client.patch(
        f"/api/projects/{project_id}/classes/1",
        json={"color": "#22C55E"},
    )
    assert color_only.status_code == 200
    assert color_only.json()["name"] == "forklift"

    # 중복 이름 409
    duplicate = await client.patch(
        f"/api/projects/{project_id}/classes/1",
        json={"name": "human"},
    )
    assert duplicate.status_code == 409

    # 빈 본문 422 / 없는 클래스 404 / 타 소유자 404
    empty = await client.patch(
        f"/api/projects/{project_id}/classes/0", json={}
    )
    assert empty.status_code == 422
    missing = await client.patch(
        f"/api/projects/{project_id}/classes/99", json={"name": "ghost"}
    )
    assert missing.status_code == 404
