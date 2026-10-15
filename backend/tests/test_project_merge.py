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
    WorkspaceStorage,
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


async def merge_with_annotation_versions(client: httpx.AsyncClient, app) -> dict:
    """Merge swapped catalogs alongside a source whose class IDs stay intact."""
    colors = {"cat": "#EF4444", "dog": "#3B82F6", "bird": "#84CC16"}
    changed = await create_project(client, "version-remap", [
        {"name": name, "color": colors[name]} for name in ("cat", "dog", "bird")
    ])
    unchanged = await create_project(client, "version-identity", [
        {"name": name, "color": colors[name]} for name in ("dog", "cat", "bird")
    ])
    datasets = [
        await create_dataset(client, changed["id"], "version-first"),
        await create_dataset(client, changed["id"], "version-second"),
        await create_dataset(client, unchanged["id"], "version-identity"),
    ]
    image_ids = {}
    async with app.state.session_factory() as session:
        for index, dataset in enumerate(datasets):
            row = await session.get(Dataset, dataset["id"])
            assert row is not None
            row.status = "ready"
            row.image_count = 3
            row.annotation_count = row.labeled_image_count = 2
            for offset, (name, class_id) in enumerate((
                ("cat", 0 if index < 2 else 1), ("bird", 2), ("blank", None),
            )):
                stem = f"version-{index}-{name}"
                image = image_with_media(
                    workspace_id=1, dataset_id=row.id, stem=stem,
                    filename=f"{stem}.jpg", rel_path=f"images/{stem}.jpg",
                    width=64, height=48, file_path=f"datasets/{row.id}/{stem}.jpg",
                    thumb_path=f"datasets/{row.id}/{stem}-thumb.jpg",
                    box_count=int(class_id is not None), version=3 + index * 10 + offset,
                    label_status="labeled" if class_id is not None else "unlabeled",
                )
                session.add(image)
                await session.flush()
                image_ids[index, name] = image.id
                if class_id is not None:
                    session.add(Annotation(
                        image_id=image.id, class_id=class_id,
                        cx=0.2, cy=0.3, w=0.1, h=0.2,
                    ))
        await session.commit()

    before = {}
    for key, image_id in image_ids.items():
        response = await client.get(f"/api/images/{image_id}/annotations")
        assert response.status_code == 200, response.text
        before[key] = response.json()
    revisions = []
    for project in (changed, unchanged):
        response = await client.get(f"/api/projects/{project['id']}")
        assert response.status_code == 200, response.text
        revisions.append(response.json())
    response = await client.post("/api/projects/merge", json={
        "name": unique_name("version-result"),
        "projects": project_revisions(*revisions),
        "classes": [
            {"name": name, "color": colors[name], "sources": [
                {"project_id": changed["id"], "class_id": old_id},
                {"project_id": unchanged["id"], "class_id": new_id},
            ]}
            for new_id, (name, old_id) in enumerate((("dog", 1), ("cat", 0), ("bird", 2)))
        ],
    })
    assert response.status_code == 201, response.text
    return {"image_ids": image_ids, "before": before, "project": response.json()}


async def test_project_merge_bumps_every_remapped_image_but_keeps_identity_versions(
    client: httpx.AsyncClient, app,
) -> None:
    case = await merge_with_annotation_versions(client, app)
    for (dataset_index, name), image_id in case["image_ids"].items():
        before = case["before"][dataset_index, name]
        response = await client.get(f"/api/images/{image_id}/annotations")
        assert response.status_code == 200, response.text
        after = response.json()
        # Rebuilding DatasetClass rows advances the catalog even for identity maps.
        assert after["catalog_version"] > before["catalog_version"]
        if dataset_index == 2:
            # An identity mapping must leave versions, boxes and status intact.
            assert after == {**before, "catalog_version": after["catalog_version"]}
            continue
        assert after["version"] == before["version"] + 1, (dataset_index, name)
        assert after["label_status"] == before["label_status"]
        assert after["assignee_id"] == before["assignee_id"]
        expected_boxes = [
            {**box, "class_id": 1 if box["class_id"] == 0 else box["class_id"]}
            for box in before["boxes"]
        ]
        assert after["boxes"] == expected_boxes
        # The unchanged bird box and boxless image still share the new catalog.
        if name == "blank":
            assert after["boxes"] == []


@pytest.mark.parametrize("dataset_index,name", [(0, "cat"), (1, "bird"), (1, "blank")])
async def test_project_merge_rejects_stale_annotations_and_accepts_current_class_ids(
    client: httpx.AsyncClient, app, dataset_index: int, name: str,
) -> None:
    case = await merge_with_annotation_versions(client, app)
    image_id = case["image_ids"][dataset_index, name]
    before = case["before"][dataset_index, name]
    stale = await client.put(f"/api/images/{image_id}/annotations", json={
        "expected_version": before["version"], "boxes": before["boxes"],
        "expected_catalog_version": before["catalog_version"],
    })
    assert stale.status_code == 409, stale.text
    conflict = stale.json()["detail"]
    assert conflict["code"] == "version_conflict"
    current = conflict["current"]
    assert current["version"] == before["version"] + 1
    assert current["label_status"] == before["label_status"]
    assert current["assignee_id"] == before["assignee_id"]
    assert current["boxes"] == [
        {**box, "class_id": 1 if box["class_id"] == 0 else box["class_id"]}
        for box in before["boxes"]
    ]
    persisted = await client.get(f"/api/images/{image_id}/annotations")
    assert persisted.status_code == 200, persisted.text
    assert persisted.json()["version"] == current["version"]
    assert persisted.json()["boxes"] == current["boxes"]

    # cat is now ID 1; reusing its old ID 0 would silently mean dog.
    new_cat = {"class_id": 1, "cx": 0.7, "cy": 0.3, "w": 0.1, "h": 0.2}
    saved = await client.put(f"/api/images/{image_id}/annotations", json={
        "expected_version": current["version"], "boxes": [new_cat],
        "expected_catalog_version": current["catalog_version"],
    })
    assert saved.status_code == 200, saved.text
    assert saved.json()["version"] == current["version"] + 1
    assert [{key: box[key] for key in new_cat} for box in saved.json()["boxes"]] == [new_cat]
    persisted = await client.get(f"/api/images/{image_id}/annotations")
    assert persisted.status_code == 200, persisted.text
    assert persisted.json()["boxes"] == saved.json()["boxes"]
    async with app.state.session_factory() as session:
        image = await session.get(Image, image_id)
        assert image is not None
        saved_class = await session.scalar(select(DatasetClass.name).where(
            DatasetClass.dataset_id == image.dataset_id, DatasetClass.class_id == 1,
        ))
        assert saved_class == "cat"


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
            workspace_id=1,
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
            workspace_id=1,
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
                    workspace_id=1,
                    created_by=1,
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
                WorkspaceStorage(workspace_id=1, bytes_used=9_999),
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
        assert (await session.get(WorkspaceStorage, 1)).bytes_used == 9_999
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
