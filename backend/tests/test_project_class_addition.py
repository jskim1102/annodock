"""Adding project classes preserves existing annotation identities."""

from __future__ import annotations

from datetime import datetime, timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, select

from app.models import Annotation, Dataset, DatasetClass, Image, Project, ProjectClass
from tests.factories import image_with_media


pytestmark = pytest.mark.asyncio
ORIGINAL_CLASSES = [
    {"name": "person", "color": "#EF4444"},
    {"name": "car", "color": "#F59E0B"},
]


async def _create_case(app, client, *, classes=None, dataset_count=2):
    classes = ORIGINAL_CLASSES if classes is None else classes
    response = await client.post("/api/projects", json={
        "name": f"test-class-addition-{uuid4().hex}", "classes": classes,
    })
    assert response.status_code == 201, response.text
    project_id = response.json()["id"]
    dataset_ids = []
    for index in range(dataset_count):
        child = await client.post("/api/datasets", json={
            "name": f"test-class-addition-child-{index}-{uuid4().hex}",
            "project_id": project_id,
        })
        assert child.status_code == 201, child.text
        dataset_ids.append(child.json()["id"])

    async with app.state.session_factory() as session:
        for dataset_id in dataset_ids:
            dataset = await session.get(Dataset, dataset_id)
            dataset.status = "ready"
            for kind in ("labeled", "blank"):
                image = image_with_media(
                    workspace_id=dataset.workspace_id, dataset_id=dataset_id,
                    stem=kind, filename=f"{kind}.jpg", rel_path=f"{kind}.jpg",
                    width=32, height=32, file_path=f"test-class-addition/{dataset_id}/{kind}.jpg",
                    thumb_path=f"test-class-addition/{dataset_id}/{kind}-thumb.jpg",
                    display_path=None,
                    box_count=len(classes) if kind == "labeled" else 0, version=7,
                    label_status="labeled" if kind == "labeled" and classes else "unlabeled",
                )
                session.add(image)
                await session.flush()
                if kind == "labeled":
                    session.add_all([
                        Annotation(image_id=image.id, class_id=class_id,
                                   cx=0.2 + class_id * 0.2, cy=0.4, w=0.1, h=0.2)
                        for class_id in range(len(classes))
                    ])
        await session.commit()
    return {"project_id": project_id, "dataset_ids": dataset_ids}


@pytest_asyncio.fixture
async def case(app, client):
    return await _create_case(app, client)


async def _snapshot(app, case):
    async with app.state.session_factory() as session:
        project = await session.get(Project, case["project_id"])
        return {
            "updated_at": project.updated_at.isoformat(),
            "project_classes": list((await session.execute(
                select(ProjectClass.class_id, ProjectClass.name, ProjectClass.color)
                .where(ProjectClass.project_id == project.id).order_by(ProjectClass.class_id),
            )).all()),
            "datasets": list((await session.execute(
                select(Dataset.id, Dataset.class_count, Dataset.class_catalog_version)
                .where(Dataset.id.in_(case["dataset_ids"])).order_by(Dataset.id),
            )).all()),
            "dataset_classes": list((await session.execute(
                select(DatasetClass.dataset_id, DatasetClass.class_id, DatasetClass.name)
                .where(DatasetClass.dataset_id.in_(case["dataset_ids"]))
                .order_by(DatasetClass.dataset_id, DatasetClass.class_id),
            )).all()),
            "images": list((await session.execute(
                select(Image.id, Image.dataset_id, Image.version, Image.box_count)
                .where(Image.dataset_id.in_(case["dataset_ids"])).order_by(Image.id),
            )).all()),
            "annotations": list((await session.execute(
                select(Annotation.id, Annotation.image_id, Annotation.class_id,
                       Annotation.cx, Annotation.cy, Annotation.w, Annotation.h)
                .join(Image, Image.id == Annotation.image_id)
                .where(Image.dataset_id.in_(case["dataset_ids"])).order_by(Annotation.id),
            )).all()),
        }


def _existing_classes():
    return [{"class_id": index, **row} for index, row in enumerate(ORIGINAL_CLASSES)]


async def _put(client, case, before, classes):
    return await client.put(f"/api/projects/{case['project_id']}/classes", json={
        "expected_updated_at": before["updated_at"], "classes": classes,
    })


@pytest.mark.parametrize("reorder", [False, True], ids=["append", "insert-and-reorder"])
async def test_add_classes_preserves_annotations_and_propagates_catalogs(
    app, client, case, reorder,
):
    before = await _snapshot(app, case)
    new_classes = [
        {"class_id": None, "name": " cone ", "color": "#abcdef"},
        {"class_id": None, "name": "sign", "color": "#123abc"},
    ]
    requested = (
        [new_classes[0], {"class_id": 1, "name": "vehicle", "color": "#654abc"},
         new_classes[1], {"class_id": 0, "name": "human", "color": "#112233"}]
        if reorder else _existing_classes() + new_classes
    )
    response = await _put(client, case, before, requested)
    assert response.status_code == 200, response.text
    expected = [{"class_id": index, "name": row["name"].strip(), "color": row["color"].upper()}
                for index, row in enumerate(requested)]
    assert response.json()["classes"] == expected
    after = await _snapshot(app, case)
    assert after["project_classes"] == [(row["class_id"], row["name"], row["color"]) for row in expected]
    old_versions = {row[0]: row[2] for row in before["datasets"]}
    response_versions = {row["dataset_id"]: row["catalog_version"]
                         for row in response.json()["catalog_versions"]}
    assert set(response_versions) == set(case["dataset_ids"])
    for dataset_id, class_count, version in after["datasets"]:
        assert class_count == 4
        assert version > old_versions[dataset_id]
        assert response_versions[dataset_id] == version
        catalog = await client.get(f"/api/datasets/{dataset_id}/classes")
        assert catalog.status_code == 200, catalog.text
        assert catalog.json() == {"catalog_version": version, "classes": [
            {"class_id": row["class_id"], "name": row["name"]} for row in expected
        ]}
    remap = {0: 3, 1: 1} if reorder else {0: 0, 1: 1}
    assert after["annotations"] == [
        (row[0], row[1], remap[row[2]], *row[3:]) for row in before["annotations"]
    ]
    assert after["images"] == [
        (row[0], row[1], row[2] + int(reorder), row[3]) for row in before["images"]
    ]
    assert after["updated_at"] != before["updated_at"]


async def test_addition_preserves_sparse_existing_dataset_membership(app, client, case):
    sparse_id = case["dataset_ids"][1]
    async with app.state.session_factory() as session:
        await session.execute(delete(Annotation).where(
            Annotation.image_id.in_(select(Image.id).where(Image.dataset_id == sparse_id)),
            Annotation.class_id == 0,
        ))
        await session.execute(delete(DatasetClass).where(
            DatasetClass.dataset_id == sparse_id, DatasetClass.class_id == 0,
        ))
        await session.commit()
    before = await _snapshot(app, case)
    response = await _put(client, case, before, [
        {"class_id": None, "name": "cone", "color": "#ABCDEF"},
        {"class_id": 0, "name": "human", "color": "#EF4444"},
        {"class_id": 1, "name": "vehicle", "color": "#F59E0B"},
    ])
    assert response.status_code == 200, response.text
    catalog = await client.get(f"/api/datasets/{sparse_id}/classes")
    assert catalog.json()["classes"] == [
        {"class_id": 0, "name": "cone"}, {"class_id": 2, "name": "vehicle"},
    ]
    after = await _snapshot(app, case)
    assert after["annotations"] == [
        (row[0], row[1], row[2] + 1, *row[3:]) for row in before["annotations"]
    ]


@pytest.mark.parametrize("dataset_count", [0, 2], ids=["empty-project", "empty-child-catalogs"])
async def test_add_first_class_to_empty_project_and_children(app, client, dataset_count):
    case = await _create_case(app, client, classes=[], dataset_count=dataset_count)
    before = await _snapshot(app, case)
    response = await _put(client, case, before, [
        {"class_id": None, "name": "person", "color": "#ef4444"},
    ])
    assert response.status_code == 200, response.text
    assert response.json()["classes"] == [{"class_id": 0, "name": "person", "color": "#EF4444"}]
    after = await _snapshot(app, case)
    assert after["dataset_classes"] == [(dataset_id, 0, "person") for dataset_id in case["dataset_ids"]]
    assert all(row[1] == 1 and row[2] > 0 for row in after["datasets"])
    assert after["annotations"] == before["annotations"] == []
    assert after["images"] == before["images"]


@pytest.mark.parametrize("problem,expected_status", [
    ("missing-old-id", 409), ("duplicate-old-id", 409), ("unknown-old-id", 409),
    ("duplicate-new-name", 409), ("duplicate-existing-name", 409),
    ("blank-name", 422), ("invalid-color", 422),
])
async def test_invalid_addition_rolls_back_every_catalog_and_annotation(
    app, client, case, problem, expected_status,
):
    before = await _snapshot(app, case)
    requested = _existing_classes() + [{"class_id": None, "name": "cone", "color": "#ABCDEF"}]
    if problem == "missing-old-id":
        requested.pop(1)
    elif problem == "duplicate-old-id":
        requested.insert(0, {"class_id": 0, "name": "second-person", "color": "#123456"})
    elif problem == "unknown-old-id":
        requested[-1]["class_id"] = 999
    elif problem == "duplicate-new-name":
        requested.append({"class_id": None, "name": " cone ", "color": "#123456"})
    elif problem == "duplicate-existing-name":
        requested[-1]["name"] = " person "
    elif problem == "blank-name":
        requested[-1]["name"] = "   "
    elif problem == "invalid-color":
        requested[-1]["color"] = "red"
    response = await _put(client, case, before, requested)
    assert response.status_code == expected_status, response.text
    assert await _snapshot(app, case) == before


async def test_stale_addition_does_not_replace_the_current_catalog(app, client, case):
    before = await _snapshot(app, case)
    stale = {**before, "updated_at": (
        datetime.fromisoformat(before["updated_at"]) - timedelta(seconds=1)
    ).isoformat()}
    response = await _put(client, case, stale, _existing_classes() + [
        {"class_id": None, "name": "cone", "color": "#ABCDEF"},
    ])
    assert response.status_code == 409, response.text
    assert response.json()["detail"].startswith("프로젝트가 변경")
    assert await _snapshot(app, case) == before
