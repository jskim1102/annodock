"""Annotation optimistic concurrency with required expected_version."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import is_lock_not_available
from app.models import (
    Annotation,
    Dataset,
    DatasetClass,
    Image,
    Project,
    ProjectClass,
    Workspace,
    WorkspaceMember,
)
from tests.factories import image_with_media


pytestmark = pytest.mark.asyncio

OWNER, ADMIN, EDITOR, OTHER_EDITOR = 8701, 8702, 8703, 8704
HISTORY_TIME = datetime(2026, 1, 2, 3, 4, tzinfo=timezone.utc)
BOX_ZERO = {"class_id": 0, "cx": 0.5, "cy": 0.4, "w": 0.2, "h": 0.1}
BOX_ONE = {"class_id": 1, "cx": 0.2, "cy": 0.3, "w": 0.1, "h": 0.2}
COLLAB_FIELDS = (
    "version", "label_status", "assignee_id", "assigned_at", "labeled_by", "labeled_at",
)


@pytest_asyncio.fixture
async def annotation_case(app, tmp_path, client, auth_headers):
    """Own every row; do not depend on another test module's seeded dataset."""
    suffix = uuid4().hex
    async with app.state.session_factory() as session:
        workspace = Workspace(
            kind="team", name=f"test-annotation-version-{suffix}", created_by=OWNER,
        )
        session.add(workspace)
        await session.flush()
        session.add_all([
            WorkspaceMember(workspace_id=workspace.id, user_id=user_id, role=role)
            for user_id, role in (
                (OWNER, "owner"), (ADMIN, "admin"),
                (EDITOR, "editor"), (OTHER_EDITOR, "editor"),
            )
        ])
        projects = [
            Project(workspace_id=workspace.id, name=f"test-version-{name}-{suffix}")
            for name in ("target", "unrelated")
        ]
        session.add_all(projects)
        await session.flush()
        for project in projects:
            session.add_all([
                ProjectClass(project_id=project.id, class_id=0, name="person", color="#123456"),
                ProjectClass(project_id=project.id, class_id=1, name="vehicle", color="#654321"),
            ])
        datasets = [
            Dataset(
                workspace_id=workspace.id, project_id=projects[project_index].id,
                name=f"test-version-{name}-{suffix}", status="ready",
                storage_path=str(tmp_path / name), class_count=2,
            )
            for name, project_index in (("first", 0), ("second", 0), ("outside", 1))
        ]
        session.add_all(datasets)
        await session.flush()
        for dataset in datasets:
            dataset.image_count = dataset.annotation_count = 0
            session.add_all([
                DatasetClass(dataset_id=dataset.id, class_id=0, name="person"),
                DatasetClass(dataset_id=dataset.id, class_id=1, name="vehicle"),
            ])

        images = {}
        specs = (
            ("blank", 0, 0, None, None),
            ("labeled", 0, 3, BOX_ZERO, OTHER_EDITOR),
            ("imported", 1, 5, BOX_ONE, None),
            ("empty_peer", 1, 7, None, None),
            ("outside", 2, 9, BOX_ZERO, OTHER_EDITOR),
        )
        for name, dataset_index, version, box, labeled_by in specs:
            dataset = datasets[dataset_index]
            image = image_with_media(
                workspace_id=workspace.id, dataset_id=dataset.id,
                stem=name, filename=f"{name}.jpg", rel_path=f"images/{name}.jpg",
                split=None, width=800, height=600,
                file_path=str(tmp_path / f"{name}.jpg"),
                thumb_path=str(tmp_path / f"{name}-thumb.jpg"),
                box_count=int(box is not None), has_label_source=box is not None,
                is_modified=False, version=version,
                label_status="labeled" if box is not None else "unlabeled",
                assignee_id=None, assigned_at=None,
                labeled_by=labeled_by,
                labeled_at=HISTORY_TIME if labeled_by is not None else None,
            )
            session.add(image)
            await session.flush()
            if box is not None:
                session.add(Annotation(image_id=image.id, **box))
            dataset.image_count += 1
            dataset.annotation_count += int(box is not None)
            images[name] = image.id
        await session.commit()
        case = {
            "workspace_id": workspace.id,
            "project_id": projects[0].id,
            "dataset_ids": [dataset.id for dataset in datasets],
            "images": images,
        }
    catalog_versions = {}
    for dataset_id in case["dataset_ids"]:
        response = await client.get(f"/api/datasets/{dataset_id}/classes", headers=_headers(auth_headers, case))
        assert response.status_code == 200, response.text
        catalog_versions[dataset_id] = response.json()["catalog_version"]
    case["catalog_versions"] = {
        name: catalog_versions[case["dataset_ids"][dataset_index]]
        for name, dataset_index, *_rest in specs
    }
    return case


def _headers(auth_headers, case, actor=EDITOR):
    return {**auth_headers(actor), "X-Workspace-Id": str(case["workspace_id"])}


def _url(case, name="blank"):
    return f"/api/images/{case['images'][name]}/annotations"


async def _snapshot(app, image_id):
    async with app.state.session_factory() as session:
        image = await session.get(Image, image_id)
        assert image is not None
        dataset = await session.get(Dataset, image.dataset_id)
        assert dataset is not None
        rows = (await session.scalars(
            select(Annotation).where(Annotation.image_id == image_id).order_by(Annotation.id)
        )).all()
        return {
            **{field: getattr(image, field) for field in COLLAB_FIELDS},
            "box_count": image.box_count,
            "is_modified": image.is_modified,
            "has_label_source": image.has_label_source,
            "dataset_annotation_count": dataset.annotation_count,
            "catalog_version": await session.scalar(text(
                "SELECT class_catalog_version FROM datasets WHERE id = :id"
            ), {"id": image.dataset_id}),
            "boxes": [
                {field: getattr(row, field) for field in ("id", "class_id", "cx", "cy", "w", "h")}
                for row in rows
            ],
            "annotation_timestamps": [(row.created_at, row.updated_at) for row in rows],
            "classes": [{"class_id": row.class_id, "name": row.name} for row in
                        (await session.scalars(select(DatasetClass).where(
                            DatasetClass.dataset_id == image.dataset_id,
                        ).order_by(DatasetClass.class_id))).all()],
        }


async def _set_state(app, image_id, **values):
    async with app.state.session_factory() as session:
        image = await session.get(Image, image_id)
        assert image is not None
        for field, value in values.items():
            setattr(image, field, value)
        await session.commit()


def _current(snapshot):
    return {key: snapshot[key] for key in ("version", "boxes", "label_status", "assignee_id", "catalog_version", "classes")}


@pytest.mark.parametrize("status,assignee", [
    ("unlabeled", None), ("in_progress", EDITOR), ("labeled", None),
])
async def test_get_and_put_expose_version_status_and_assignee(
    client, app, auth_headers, annotation_case, status, assignee,
):
    case = annotation_case
    await _set_state(
        app, case["images"]["blank"], version=12, label_status=status,
        assignee_id=assignee, assigned_at=HISTORY_TIME if assignee else None,
    )
    headers = _headers(auth_headers, case)
    fetched = await client.get(_url(case), headers=headers)
    assert fetched.status_code == 200, fetched.text
    assert (fetched.json()["version"], fetched.json()["label_status"], fetched.json()["assignee_id"]) == (
        12, status, assignee,
    )
    assert (fetched.json()["width"], fetched.json()["height"]) == (800, 600)

    saved = await client.put(
        _url(case), headers=headers, json={"expected_version": 12, "expected_catalog_version": fetched.json()["catalog_version"], "boxes": [BOX_ONE]},
    )
    assert saved.status_code == 200, saved.text
    expected_status = "labeled" if status == "labeled" else "in_progress"
    expected_assignee = None if status == "labeled" else EDITOR
    assert (saved.json()["version"], saved.json()["label_status"], saved.json()["assignee_id"]) == (
        13, expected_status, expected_assignee,
    )
    after = await client.get(_url(case), headers=headers)
    assert after.status_code == 200, after.text
    assert _current(after.json()) == _current({**saved.json(), "classes": fetched.json()["classes"]})


async def test_repeated_expected_version_returns_complete_current_without_writes(
    client, app, auth_headers, annotation_case,
):
    case = annotation_case
    headers = _headers(auth_headers, case)
    first = await client.put(
        _url(case), headers=headers,
        json={"expected_version": 0, "expected_catalog_version": case["catalog_versions"]["blank"], "boxes": [BOX_ZERO, BOX_ONE]},
    )
    assert first.status_code == 200, first.text
    before = await _snapshot(app, case["images"]["blank"])
    stale = await client.put(
        _url(case), headers=headers,
        json={"expected_version": 0, "expected_catalog_version": before["catalog_version"], "boxes": [{**BOX_ZERO, "cx": 0.8}]},
    )
    assert stale.status_code == 409, stale.text
    assert stale.json()["detail"] == {"code": "version_conflict", "current": _current(before)}
    assert before["version"] == 1
    assert before["label_status"] == "in_progress" and before["assignee_id"] == EDITOR
    assert await _snapshot(app, case["images"]["blank"]) == before


async def test_stale_autosave_cannot_implicitly_reclaim_after_admin_returns_assignment(
    client, app, auth_headers, annotation_case,
):
    case = annotation_case
    await _set_state(app, case["images"]["blank"], label_status="in_progress",
                     assignee_id=EDITOR, assigned_at=HISTORY_TIME, version=4)
    returned = await client.post(
        f"/api/datasets/{case['dataset_ids'][0]}/assignments/reclaim",
        headers=_headers(auth_headers, case, ADMIN), json={"user_id": EDITOR},
    )
    assert returned.status_code == 200 and returned.json() == {"reclaimed": 1}
    before = await _snapshot(app, case["images"]["blank"])
    assert (before["version"], before["label_status"], before["assignee_id"]) == (5, "unlabeled", None)
    late = await client.put(_url(case), headers=_headers(auth_headers, case),
                            json={"expected_version": 4, "expected_catalog_version": before["catalog_version"], "boxes": [BOX_ZERO]})
    assert late.status_code == 409, late.text
    assert late.json()["detail"] == {"code": "version_conflict", "current": _current(before)}
    assert await _snapshot(app, case["images"]["blank"]) == before


@pytest.mark.parametrize("payload", [{"boxes": [BOX_ZERO]}, {"boxes": [], "expected_version": None}], ids=["missing", "null"])
async def test_expected_version_is_required_without_writes(
    client, app, auth_headers, annotation_case, payload,
):
    case = annotation_case
    before = await _snapshot(app, case["images"]["blank"])
    saved = await client.put(_url(case), headers=_headers(auth_headers, case),
                             json={**payload, "expected_catalog_version": before["catalog_version"]})
    assert saved.status_code == 422, saved.text
    assert saved.json()["detail"] == {"code": "expected_version_required"}
    assert await _snapshot(app, case["images"]["blank"]) == before


@pytest.mark.parametrize("boxes", [[], [BOX_ZERO, BOX_ONE]], ids=["empty", "boxes"])
async def test_unlabeled_put_implicitly_claims_and_increments_once(
    client, app, auth_headers, annotation_case, boxes,
):
    case = annotation_case
    before = await _snapshot(app, case["images"]["blank"])
    assert before["label_status"] == "unlabeled" and before["assignee_id"] is None
    saved = await client.put(
        _url(case), headers=_headers(auth_headers, case),
        json={"expected_version": 0, "expected_catalog_version": before["catalog_version"], "boxes": boxes},
    )
    assert saved.status_code == 200, saved.text
    after = await _snapshot(app, case["images"]["blank"])
    assert after["version"] == 1
    assert after["label_status"] == "in_progress" and after["assignee_id"] == EDITOR
    assert after["assigned_at"] is not None
    assert after["labeled_by"] is after["labeled_at"] is None
    assert after["box_count"] == len(boxes)
    assert after["dataset_annotation_count"] == before["dataset_annotation_count"] + len(boxes)


async def test_other_editors_assignment_conflicts_without_any_writes(
    client, app, auth_headers, annotation_case,
):
    case = annotation_case
    await _set_state(
        app, case["images"]["blank"], label_status="in_progress", version=4,
        assignee_id=OTHER_EDITOR, assigned_at=HISTORY_TIME,
    )
    before = await _snapshot(app, case["images"]["blank"])
    rejected = await client.put(
        _url(case), headers=_headers(auth_headers, case),
        json={"expected_version": 4, "expected_catalog_version": before["catalog_version"], "boxes": [BOX_ONE]},
    )
    assert rejected.status_code == 409, rejected.text
    assert rejected.json()["detail"] == {
        "code": "image_assigned_to_other", "assignee_id": OTHER_EDITOR,
    }
    assert await _snapshot(app, case["images"]["blank"]) == before


@pytest.mark.parametrize("actor", [ADMIN, OWNER], ids=["admin", "owner"])
async def test_admin_can_edit_another_members_in_progress_image(
    client, app, auth_headers, annotation_case, actor,
):
    case = annotation_case
    await _set_state(
        app, case["images"]["blank"], label_status="in_progress", version=4,
        assignee_id=OTHER_EDITOR, assigned_at=HISTORY_TIME,
    )
    saved = await client.put(
        _url(case), headers=_headers(auth_headers, case, actor),
        json={"expected_version": 4, "expected_catalog_version": case["catalog_versions"]["blank"], "boxes": [BOX_ONE]},
    )
    assert saved.status_code == 200, saved.text
    after = await _snapshot(app, case["images"]["blank"])
    assert after["version"] == 5
    assert after["label_status"] == "in_progress"
    assert after["assignee_id"] == OTHER_EDITOR and after["assigned_at"] == HISTORY_TIME
    assert after["boxes"][0]["class_id"] == 1


@pytest.mark.parametrize("name", ["labeled", "imported"])
async def test_labeled_put_preserves_completion_state_author_and_time(
    client, app, auth_headers, annotation_case, name,
):
    case = annotation_case
    before = await _snapshot(app, case["images"][name])
    saved = await client.put(
        _url(case, name), headers=_headers(auth_headers, case),
        json={"expected_version": before["version"], "expected_catalog_version": before["catalog_version"], "boxes": [BOX_ZERO, BOX_ONE]},
    )
    assert saved.status_code == 200, saved.text
    after = await _snapshot(app, case["images"][name])
    assert after["version"] == before["version"] + 1
    assert after["label_status"] == "labeled"
    for field in COLLAB_FIELDS[1:]:
        assert after[field] == before[field], field
    assert after["box_count"] == 2
    assert after["dataset_annotation_count"] == before["dataset_annotation_count"] + 1


async def test_class_id_swap_bumps_all_project_images_including_boxless_and_rejects_stale_puts(
    client, app, auth_headers, annotation_case,
):
    case = annotation_case
    before = {name: await _snapshot(app, image_id) for name, image_id in case["images"].items()}
    project = await client.get(
        f"/api/projects/{case['project_id']}", headers=_headers(auth_headers, case, ADMIN),
    )
    assert project.status_code == 200, project.text
    swapped = await client.put(
        f"/api/projects/{case['project_id']}/classes",
        headers=_headers(auth_headers, case, ADMIN),
        json={
            "expected_updated_at": project.json()["updated_at"],
            "classes": [
                {"class_id": 1, "name": "vehicle", "color": "#654321"},
                {"class_id": 0, "name": "person", "color": "#123456"},
            ],
        },
    )
    assert swapped.status_code == 200, swapped.text
    after = {name: await _snapshot(app, image_id) for name, image_id in case["images"].items()}
    assert after["outside"] == before["outside"]
    for name in ("blank", "labeled", "imported", "empty_peer"):
        assert after[name]["version"] == before[name]["version"] + 1, name
        assert after[name]["catalog_version"] > before[name]["catalog_version"], name
        assert after[name]["boxes"] == [
            {**box, "class_id": 1 - box["class_id"]} for box in before[name]["boxes"]
        ]
        for field in COLLAB_FIELDS[1:]:
            assert after[name][field] == before[name][field], (name, field)
        assert after[name]["box_count"] == before[name]["box_count"]
        assert after[name]["dataset_annotation_count"] == before[name]["dataset_annotation_count"]

        # Both old class IDs still exist: only version validation can catch this.
        stale = await client.put(
            _url(case, name), headers=_headers(auth_headers, case),
            json={
                "expected_version": before[name]["version"],
                "expected_catalog_version": after[name]["catalog_version"],
                "boxes": [{key: value for key, value in box.items() if key != "id"}
                          for box in before[name]["boxes"]],
            },
        )
        assert stale.status_code == 409, (name, stale.text)
        assert stale.json()["detail"] == {"code": "version_conflict", "current": _current(after[name])}
        assert await _snapshot(app, case["images"][name]) == after[name]


@pytest.mark.parametrize("name", ["blank", "labeled"])
async def test_invalid_class_does_not_claim_or_increment_version(
    client, app, auth_headers, annotation_case, name,
):
    case = annotation_case
    before = await _snapshot(app, case["images"][name])
    rejected = await client.put(
        _url(case, name), headers=_headers(auth_headers, case),
        json={
            "expected_version": before["version"],
            "expected_catalog_version": before["catalog_version"],
            "boxes": [BOX_ONE, {**BOX_ZERO, "class_id": 999}],
        },
    )
    assert rejected.status_code == 422, rejected.text
    assert rejected.json()["detail"]["code"] == "unknown_annotation_class"
    assert rejected.json()["detail"]["class_ids"] == [999]
    assert await _snapshot(app, case["images"][name]) == before


def _pause_first_annotation_lock(monkeypatch):
    """Pause only after the real SELECT FOR UPDATE has returned with its locks."""
    acquired, release, contender = asyncio.Event(), asyncio.Event(), asyncio.Event()
    execute = AsyncSession.execute
    arrivals = 0

    async def execute_with_pause(session, statement, *args, **kwargs):
        nonlocal arrivals
        is_image_lock = (
            getattr(statement, "is_select", False)
            and getattr(statement, "_for_update_arg", None) is not None
            and "images" in str(statement).lower()
        )
        position = None
        if is_image_lock:
            arrivals += 1
            position = arrivals
            if position == 2:
                contender.set()
        result = await execute(session, statement, *args, **kwargs)
        if position == 1:
            acquired.set()
            await asyncio.wait_for(release.wait(), timeout=10)
        return result

    monkeypatch.setattr(AsyncSession, "execute", execute_with_pause)
    return acquired, release, contender


async def _finish_tasks(tasks):
    for task in tasks:
        if not task.done():
            task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


async def test_claim_skips_a_real_annotation_put_after_its_image_lock_is_acquired(
    client, app, auth_headers, annotation_case, monkeypatch,
):
    case = annotation_case
    acquired, release, _contender = _pause_first_annotation_lock(monkeypatch)
    headers = _headers(auth_headers, case)
    saving = asyncio.create_task(client.put(_url(case), headers=headers,
        json={"expected_version": 0, "expected_catalog_version": case["catalog_versions"]["blank"], "boxes": [BOX_ZERO]}))
    try:
        await asyncio.wait_for(acquired.wait(), timeout=5)
        claim = await asyncio.wait_for(client.post(
            f"/api/datasets/{case['dataset_ids'][0]}/claim", headers=headers, json={"split": None},
        ), timeout=5)
        assert claim.status_code == 200 and claim.json() == {"image": None}, claim.text
        progress = await client.get(f"/api/datasets/{case['dataset_ids'][0]}/progress", headers=headers)
        assert progress.json()["unlabeled"] == 1, "a locked candidate is not completed"
        release.set()
        saved = await asyncio.wait_for(saving, timeout=5)
        assert saved.status_code == 200, saved.text
        state = await _snapshot(app, case["images"]["blank"])
        assert (state["version"], state["assignee_id"]) == (1, EDITOR)
    finally:
        release.set()
        await _finish_tasks([saving])


async def test_concurrent_same_version_puts_only_commit_one_replacement(
    client, app, auth_headers, annotation_case, monkeypatch,
):
    case = annotation_case
    headers = _headers(auth_headers, case)
    acquired, release, contender = _pause_first_annotation_lock(monkeypatch)
    tasks = [asyncio.create_task(client.put(
        _url(case), headers=headers, json={"expected_version": 0, "expected_catalog_version": case["catalog_versions"]["blank"], "boxes": [BOX_ZERO]},
    ))]
    try:
        await asyncio.wait_for(acquired.wait(), timeout=10)
        tasks.append(asyncio.create_task(client.put(
            _url(case), headers=headers, json={"expected_version": 0, "expected_catalog_version": case["catalog_versions"]["blank"], "boxes": [BOX_ONE]},
        )))
        await asyncio.wait_for(contender.wait(), timeout=10)
        release.set()
        first, second = await asyncio.wait_for(asyncio.gather(*tasks), timeout=10)
        assert first.status_code == 200, first.text
        assert second.status_code == 409, second.text
        final = await _snapshot(app, case["images"]["blank"])
        assert final["version"] == 1
        assert final["box_count"] == 1 and final["dataset_annotation_count"] == 2
        assert [{key: value for key, value in box.items() if key != "id"}
                for box in final["boxes"]] == [BOX_ZERO]
        assert second.json()["detail"] == {"code": "version_conflict", "current": _current(final)}
    finally:
        release.set()
        await _finish_tasks(tasks)


async def test_annotation_put_retains_dataset_snapshot_lock_until_commit(
    client, app, auth_headers, annotation_case, monkeypatch,
):
    case = annotation_case
    acquired, release, _contender = _pause_first_annotation_lock(monkeypatch)
    task = asyncio.create_task(client.put(
        _url(case), headers=_headers(auth_headers, case),
        json={"expected_version": 0, "expected_catalog_version": case["catalog_versions"]["blank"], "boxes": [BOX_ZERO]},
    ))
    try:
        await asyncio.wait_for(acquired.wait(), timeout=10)
        # A snapshot reader must not lock this dataset while its annotations change.
        async with app.state.session_factory() as session:
            with pytest.raises(DBAPIError) as caught:
                await session.scalar(
                    select(Dataset.id).where(Dataset.id == case["dataset_ids"][0])
                    .with_for_update(nowait=True)
                )
            assert is_lock_not_available(caught.value)
            await session.rollback()
        release.set()
        saved = await asyncio.wait_for(task, timeout=10)
        assert saved.status_code == 200, saved.text
        async with app.state.session_factory() as session:
            assert await session.scalar(
                select(Dataset.id).where(Dataset.id == case["dataset_ids"][0])
                .with_for_update(nowait=True)
            ) == case["dataset_ids"][0]
            await session.rollback()
    finally:
        release.set()
        await _finish_tasks([task])


@pytest.mark.parametrize("name", ["blank", "labeled"])
async def test_annotation_snapshot_and_conflict_include_sorted_current_classes(
    client, app, auth_headers, annotation_case, name,
):
    case = annotation_case
    headers = _headers(auth_headers, case)
    before = await _snapshot(app, case["images"][name])
    fetched = await client.get(_url(case, name), headers=headers)
    assert fetched.status_code == 200, fetched.text
    assert fetched.json()["catalog_version"] == before["catalog_version"]
    assert fetched.json()["classes"] == before["classes"] == [
        {"class_id": 0, "name": "person"}, {"class_id": 1, "name": "vehicle"},
    ]
    rejected = await client.put(_url(case, name), headers=headers,
                               json={"boxes": [BOX_ZERO], "expected_version": before["version"] + 1,
                                     "expected_catalog_version": before["catalog_version"]})
    assert rejected.status_code == 409, rejected.text
    assert rejected.json()["detail"]["current"] == _current(before)
    assert await _snapshot(app, case["images"][name]) == before


async def test_annotation_get_keeps_version_boxes_and_classes_coherent_during_remap(
    client, app, auth_headers, annotation_case, monkeypatch,
):
    case = annotation_case
    headers = _headers(auth_headers, case)
    admin_headers = _headers(auth_headers, case, ADMIN)
    before = await _snapshot(app, case["images"]["labeled"])
    project = await client.get(f"/api/projects/{case['project_id']}", headers=admin_headers)
    assert project.status_code == 200, project.text
    reader_locked, release_reader = asyncio.Event(), asyncio.Event()
    writer_attempted, writer_locked = asyncio.Event(), asyncio.Event()
    backend_pids = {}
    original_scalar, original_execute = AsyncSession.scalar, AsyncSession.execute

    async def scalar_after_share_lock(session, statement, *args, **kwargs):
        result = await original_scalar(session, statement, *args, **kwargs)
        lock = getattr(statement, "_for_update_arg", None)
        if (not reader_locked.is_set() and lock is not None and lock.read
                and "images" in str(statement).lower()):
            backend_pids["reader"] = await original_scalar(session, text("SELECT pg_backend_pid()"))
            reader_locked.set()  # The real Image SHARE query has completed.
            await asyncio.wait_for(release_reader.wait(), timeout=10)
        return result

    async def execute_at_image_write_lock(session, statement, *args, **kwargs):
        lock = getattr(statement, "_for_update_arg", None)
        image_write_lock = (getattr(statement, "is_select", False)
                            and lock is not None and not lock.read
                            and "images" in str(statement).lower())
        if image_write_lock:
            backend_pids["writer"] = await original_scalar(session, text("SELECT pg_backend_pid()"))
            writer_attempted.set()
        result = await original_execute(session, statement, *args, **kwargs)
        if image_write_lock:
            writer_locked.set()
        return result

    monkeypatch.setattr(AsyncSession, "scalar", scalar_after_share_lock)
    monkeypatch.setattr(AsyncSession, "execute", execute_at_image_write_lock)
    tasks = [asyncio.create_task(client.get(_url(case, "labeled"), headers=headers))]
    try:
        await asyncio.wait_for(reader_locked.wait(), timeout=5)
        tasks.append(asyncio.create_task(client.put(
            f"/api/projects/{case['project_id']}/classes", headers=admin_headers,
            json={"expected_updated_at": project.json()["updated_at"], "classes": [
                {"class_id": 1, "name": "vehicle", "color": "#654321"},
                {"class_id": 0, "name": "person", "color": "#123456"},
            ]},
        )))
        await asyncio.wait_for(writer_attempted.wait(), timeout=5)

        async def wait_for_database_lock_wait():
            # Confirm PostgreSQL sees this precise blocker, not merely a task
            # scheduled near the lock query. No sleep-based race assumption.
            async with app.state.session_factory() as observer:
                while not await observer.scalar(text(
                    "SELECT CAST(:reader AS INTEGER) = ANY("
                    "pg_blocking_pids(CAST(:writer AS INTEGER)))"
                ), backend_pids):
                    assert not tasks[1].done(), "remap finished before the reader released its lock"

        await asyncio.wait_for(wait_for_database_lock_wait(), timeout=5)
        assert not writer_locked.is_set()
        assert not tasks[0].done() and not tasks[1].done()
        release_reader.set()
        fetched, remapped = await asyncio.wait_for(asyncio.gather(*tasks), timeout=10)
        assert fetched.status_code == 200, fetched.text
        assert remapped.status_code == 200, remapped.text
        assert writer_locked.is_set()
        assert _current(fetched.json()) == _current(before)

        fresh = await client.get(_url(case, "labeled"), headers=headers)
        assert fresh.status_code == 200, fresh.text
        assert fresh.json()["version"] == before["version"] + 1
        assert fresh.json()["catalog_version"] > before["catalog_version"]
        assert fresh.json()["boxes"] == [{**box, "class_id": 1} for box in before["boxes"]]
        assert fresh.json()["classes"] == [
            {"class_id": 0, "name": "vehicle"}, {"class_id": 1, "name": "person"},
        ]
        assert _current(fresh.json()) == _current(await _snapshot(app, case["images"]["labeled"]))
    finally:
        release_reader.set()
        await _finish_tasks(tasks)
