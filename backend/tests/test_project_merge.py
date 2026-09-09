from __future__ import annotations

from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select

from app.models import (
    Annotation,
    Dataset,
    DatasetClass,
    DatasetMergeSource,
    Image,
    Project,
    TrainingRun,
    UploadSession,
    UserStorage,
)
from tests.factories import image_with_media


pytestmark = pytest.mark.asyncio


def unique_name(suffix: str) -> str:
    return f"test-{suffix}-{uuid4().hex}"


def project_revisions(*projects: dict) -> list[dict[str, int | str]]:
    return [
        {
            "project_id": project["id"],
            "expected_updated_at": project["updated_at"],
        }
        for project in projects
    ]


async def create_project(
    client: httpx.AsyncClient,
    suffix: str,
    classes: list[dict[str, str]],
    *,
    headers: dict[str, str] | None = None,
) -> dict:
    response = await client.post(
        "/api/projects",
        headers=headers,
        json={"name": unique_name(suffix), "classes": classes},
    )
    assert response.status_code == 201, response.text
    return response.json()


async def create_dataset(
    client: httpx.AsyncClient,
    project_id: int,
    suffix: str,
) -> dict:
    response = await client.post(
        "/api/datasets",
        json={"name": unique_name(suffix), "project_id": project_id},
    )
    assert response.status_code == 201, response.text
    return response.json()


async def test_project_merge_moves_datasets_and_remaps_classes_without_copying(
    client: httpx.AsyncClient,
    app,
) -> None:
    first_project = await create_project(
        client,
        "merge-first",
        [
            {"name": "person", "color": "#EF4444"},
            {"name": "forklift", "color": "#F59E0B"},
        ],
    )
    second_project = await create_project(
        client,
        "merge-second",
        [
            {"name": "helmet", "color": "#84CC16"},
            {"name": "person", "color": "#3B82F6"},
        ],
    )
    leaf = await create_dataset(client, first_project["id"], "merge-leaf")
    merged_child = await create_dataset(
        client, first_project["id"], "merge-existing-snapshot"
    )
    second_child = await create_dataset(
        client, second_project["id"], "merge-second-child"
    )

    async with app.state.session_factory() as session:
        leaf_row = await session.get(Dataset, leaf["id"])
        merged_row = await session.get(Dataset, merged_child["id"])
        second_row = await session.get(Dataset, second_child["id"])
        assert leaf_row is not None
        assert merged_row is not None
        assert second_row is not None
        leaf_row.status = "ready"
        merged_row.status = "ready"
        merged_row.is_merged = True
        second_row.status = "ready"

        first_image = image_with_media(
            owner_id=1,
            dataset_id=merged_row.id,
            stem="project-merge-first",
            filename="project-merge-first.jpg",
            rel_path="images/project-merge-first.jpg",
            split="train",
            width=64,
            height=48,
            file_path="datasets/original/project-merge-first.jpg",
            display_path="datasets/display/project-merge-first.jpg",
            thumb_path="datasets/thumbs/project-merge-first.jpg",
            original_bytes=1_024,
            display_bytes=512,
            thumb_bytes=128,
            box_count=1,
        )
        second_image = image_with_media(
            owner_id=1,
            dataset_id=second_row.id,
            stem="project-merge-second",
            filename="project-merge-second.jpg",
            rel_path="images/project-merge-second.jpg",
            split="valid",
            width=64,
            height=48,
            file_path="datasets/original/project-merge-second.jpg",
            display_path=None,
            thumb_path="datasets/thumbs/project-merge-second.jpg",
            original_bytes=2_048,
            thumb_bytes=256,
            box_count=2,
        )
        session.add_all([first_image, second_image])
        await session.flush()
        session.add_all(
            [
                Annotation(
                    image_id=first_image.id,
                    class_id=0,
                    cx=0.2,
                    cy=0.3,
                    w=0.1,
                    h=0.2,
                ),
                Annotation(
                    image_id=second_image.id,
                    class_id=0,
                    cx=0.4,
                    cy=0.5,
                    w=0.2,
                    h=0.2,
                ),
                Annotation(
                    image_id=second_image.id,
                    class_id=1,
                    cx=0.6,
                    cy=0.7,
                    w=0.3,
                    h=0.2,
                ),
                DatasetMergeSource(
                    merged_dataset_id=merged_row.id,
                    source_dataset_id=leaf_row.id,
                    position=0,
                ),
                TrainingRun(
                    owner_id=1,
                    dataset_id=merged_row.id,
                    dataset_name=merged_row.name,
                    weights="yolo11n.pt",
                    epochs=1,
                    imgsz=640,
                    batch=1,
                    split_mode="2way",
                    ratios={"train": 0.8, "valid": 0.2},
                    seed=7,
                    training_args={},
                    state="done",
                    out_dir="runs/project-merge",
                ),
                UserStorage(owner_id=1, bytes_used=9_999),
            ]
        )
        await session.commit()
        image_snapshot = {
            first_image.id: (
                first_image.dataset_id,
                first_image.media_object_id,
                first_image.file_path,
                first_image.display_path,
                first_image.thumb_path,
            ),
            second_image.id: (
                second_image.dataset_id,
                second_image.media_object_id,
                second_image.file_path,
                second_image.display_path,
                second_image.thumb_path,
            ),
        }

    first_revision = (await client.get(
        f"/api/projects/{first_project['id']}"
    )).json()
    second_revision = (await client.get(
        f"/api/projects/{second_project['id']}"
    )).json()

    response = await client.post(
        "/api/projects/merge",
        json={
            # Reusing a selected source name must be possible because that
            # source project disappears only when the transaction succeeds.
            "name": first_project["name"],
            "projects": project_revisions(first_revision, second_revision),
            "classes": [
                {
                    "name": "helmet",
                    "color": "#84CC16",
                    "sources": [
                        {"project_id": second_project["id"], "class_id": 0}
                    ],
                },
                {
                    "name": "person",
                    "color": "#EF4444",
                    "sources": [
                        {"project_id": first_project["id"], "class_id": 0},
                        {"project_id": second_project["id"], "class_id": 1},
                    ],
                },
                {
                    "name": "forklift",
                    "color": "#F59E0B",
                    "sources": [
                        {"project_id": first_project["id"], "class_id": 1}
                    ],
                },
            ],
        },
    )

    assert response.status_code == 201, response.text
    result = response.json()
    assert result["id"] not in {first_project["id"], second_project["id"]}
    assert result["name"] == first_project["name"]
    assert result["classes"] == [
        {"class_id": 0, "name": "helmet", "color": "#84CC16"},
        {"class_id": 1, "name": "person", "color": "#EF4444"},
        {"class_id": 2, "name": "forklift", "color": "#F59E0B"},
    ]

    assert (await client.get(f"/api/projects/{first_project['id']}")).status_code == 404
    assert (await client.get(f"/api/projects/{second_project['id']}")).status_code == 404

    all_dataset_ids = [leaf["id"], merged_child["id"], second_child["id"]]
    async with app.state.session_factory() as session:
        datasets = list(
            (
                await session.scalars(
                    select(Dataset)
                    .where(Dataset.id.in_(all_dataset_ids))
                    .order_by(Dataset.id)
                )
            ).all()
        )
        assert {dataset.project_id for dataset in datasets} == {result["id"]}

        annotations = list(
            (
                await session.scalars(
                    select(Annotation.class_id).order_by(Annotation.id)
                )
            ).all()
        )
        assert annotations[-3:] == [1, 0, 1]

        classes = list(
            (
                await session.execute(
                    select(
                        DatasetClass.dataset_id,
                        DatasetClass.class_id,
                        DatasetClass.name,
                    )
                    .where(DatasetClass.dataset_id.in_(all_dataset_ids))
                    .order_by(DatasetClass.dataset_id, DatasetClass.class_id)
                )
            ).all()
        )
        expected_by_dataset = {
            leaf["id"]: [(1, "person"), (2, "forklift")],
            merged_child["id"]: [(1, "person"), (2, "forklift")],
            second_child["id"]: [(0, "helmet"), (1, "person")],
        }
        for dataset_id, expected in expected_by_dataset.items():
            assert [
                (class_id, name)
                for row_dataset_id, class_id, name in classes
                if row_dataset_id == dataset_id
            ] == expected

        images = list(
            (
                await session.scalars(
                    select(Image).where(Image.id.in_(image_snapshot))
                )
            ).all()
        )
        assert {
            image.id: (
                image.dataset_id,
                image.media_object_id,
                image.file_path,
                image.display_path,
                image.thumb_path,
            )
            for image in images
        } == image_snapshot
        assert (await session.get(UserStorage, 1)).bytes_used == 9_999
        run = await session.scalar(
            select(TrainingRun).where(TrainingRun.dataset_id == merged_child["id"])
        )
        assert run is not None
        membership = await session.get(
            DatasetMergeSource, (merged_child["id"], leaf["id"])
        )
        assert membership is not None


async def test_project_merge_rejects_incomplete_mapping_without_partial_changes(
    client: httpx.AsyncClient,
    app,
) -> None:
    first = await create_project(
        client,
        "merge-invalid-first",
        [{"name": "person", "color": "#EF4444"}],
    )
    second = await create_project(
        client,
        "merge-invalid-second",
        [{"name": "vehicle", "color": "#3B82F6"}],
    )
    dataset = await create_dataset(client, first["id"], "merge-invalid-child")
    first_revision = (await client.get(f"/api/projects/{first['id']}")).json()

    response = await client.post(
        "/api/projects/merge",
        json={
            "name": unique_name("merge-invalid-result"),
            "projects": project_revisions(first_revision, second),
            "classes": [
                {
                    "name": "person",
                    "color": "#EF4444",
                    "sources": [
                        {"project_id": first["id"], "class_id": 0}
                    ],
                }
            ],
        },
    )

    assert response.status_code == 409
    assert "모든 원본 클래스" in response.json()["detail"]
    assert (await client.get(f"/api/projects/{first['id']}")).status_code == 200
    assert (await client.get(f"/api/projects/{second['id']}")).status_code == 200
    async with app.state.session_factory() as session:
        dataset_row = await session.get(Dataset, dataset["id"])
        assert dataset_row is not None
        assert dataset_row.project_id == first["id"]


async def test_project_merge_rejects_a_stale_class_catalog_revision(
    client: httpx.AsyncClient,
) -> None:
    first = await create_project(
        client,
        "merge-stale-first",
        [{"name": "person", "color": "#EF4444"}],
    )
    second = await create_project(
        client,
        "merge-stale-second",
        [{"name": "vehicle", "color": "#3B82F6"}],
    )
    changed = await client.patch(
        f"/api/projects/{first['id']}/classes/0",
        json={"name": "human"},
    )
    assert changed.status_code == 200

    response = await client.post(
        "/api/projects/merge",
        json={
            "name": unique_name("merge-stale-result"),
            "projects": [
                {
                    "project_id": first["id"],
                    "expected_updated_at": first["updated_at"],
                },
                {
                    "project_id": second["id"],
                    "expected_updated_at": second["updated_at"],
                },
            ],
            "classes": [
                {
                    "name": "person",
                    "color": "#EF4444",
                    "sources": [
                        {"project_id": first["id"], "class_id": 0}
                    ],
                },
                {
                    "name": "vehicle",
                    "color": "#3B82F6",
                    "sources": [
                        {"project_id": second["id"], "class_id": 0}
                    ],
                },
            ],
        },
    )

    assert response.status_code == 409
    assert "변경" in response.json()["detail"]
    assert (await client.get(f"/api/projects/{first['id']}")).status_code == 200
    assert (await client.get(f"/api/projects/{second['id']}")).status_code == 200


async def test_project_merge_is_owner_scoped_and_blocked_by_open_uploads(
    client: httpx.AsyncClient,
    app,
    auth_headers,
) -> None:
    first = await create_project(
        client,
        "merge-guard-first",
        [{"name": "person", "color": "#EF4444"}],
    )
    second = await create_project(
        client,
        "merge-guard-second",
        [{"name": "person", "color": "#3B82F6"}],
    )
    dataset = await create_dataset(client, first["id"], "merge-guard-child")
    foreign = await create_project(
        client,
        "merge-foreign",
        [{"name": "person", "color": "#22C55E"}],
        headers=auth_headers(2),
    )
    first_revision = (await client.get(f"/api/projects/{first['id']}")).json()
    valid_classes = [
        {
            "name": "person",
            "color": "#EF4444",
            "sources": [
                {"project_id": first["id"], "class_id": 0},
                {"project_id": second["id"], "class_id": 0},
            ],
        }
    ]

    foreign_response = await client.post(
        "/api/projects/merge",
        json={
            "name": unique_name("merge-foreign-result"),
            "projects": project_revisions(first_revision, foreign),
            "classes": [
                {
                    "name": "person",
                    "color": "#EF4444",
                    "sources": [
                        {"project_id": first["id"], "class_id": 0},
                        {"project_id": foreign["id"], "class_id": 0},
                    ],
                }
            ],
        },
    )
    assert foreign_response.status_code == 404

    async with app.state.session_factory() as session:
        session.add(
            UploadSession(
                dataset_id=dataset["id"],
                filename="still-uploading.zip",
                size=100,
                chunk_size=10,
                received_chunks=[0],
                kind="zip",
                state="open",
            )
        )
        await session.commit()

    active_response = await client.post(
        "/api/projects/merge",
        json={
            "name": unique_name("merge-active-result"),
            "projects": project_revisions(first_revision, second),
            "classes": valid_classes,
        },
    )
    assert active_response.status_code == 409
    assert "업로드" in active_response.json()["detail"]

    assert (await client.get(f"/api/projects/{first['id']}")).status_code == 200
    assert (await client.get(f"/api/projects/{second['id']}")).status_code == 200
