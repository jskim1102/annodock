"""Catalog generations protect annotation writes across every class mutation."""

from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.pool import NullPool

from app.db import is_lock_not_available
from app.models import Annotation, Dataset, DatasetClass, Image, Project, UploadJob
from app.services.ingest import ingest_collected
from tests.conftest import TEST_DATABASE_URL
from tests.factories import image_with_media
from tests.test_annotation_version import (
    ADMIN, BOX_ZERO, _current, _finish_tasks, _headers, _snapshot, _url,
    annotation_case,  # noqa: F401 — shared real workspace/image fixture
)
from tests.test_class_resolution import _incoming_files, _pause_for_resolution, _project_dataset_job
from tests.test_project_merge import merge_with_annotation_versions
from tests.test_workspace_migration import _apply, _load_migration, _rows, _schema_snapshot


ROOT = Path(__file__).resolve().parents[2]
VERSIONS = ROOT / "backend" / "alembic" / "versions"
REVISION = "0023_dataset_catalog_version"


async def _classes(client, dataset_id, headers=None):
    response = await client.get(f"/api/datasets/{dataset_id}/classes", headers=headers)
    assert response.status_code == 200, response.text
    result = response.json()
    assert type(result["catalog_version"]) is int and result["catalog_version"] >= 0
    return result


async def _all_catalogs(client, case, auth_headers):
    return {dataset_id: await _classes(client, dataset_id, _headers(auth_headers, case))
            for dataset_id in case["dataset_ids"]}


@pytest.mark.asyncio
async def test_new_empty_dataset_starts_at_catalog_zero(client):
    project = await client.post("/api/projects", json={"name": f"test-empty-catalog-{uuid4().hex}", "classes": []})
    assert project.status_code == 201, project.text
    dataset = await client.post("/api/datasets", json={"name": f"test-empty-dataset-{uuid4().hex}", "project_id": project.json()["id"]})
    assert dataset.status_code == 201, dataset.text
    assert await _classes(client, dataset.json()["id"]) == {"catalog_version": 0, "classes": []}


@pytest.mark.asyncio
async def test_catalog_get_returns_404_when_dataset_is_deleted_before_join_snapshot(client, app, monkeypatch):
    project = await client.post("/api/projects", json={
        "name": f"test-delete-catalog-project-{uuid4().hex}", "classes": [],
    })
    assert project.status_code == 201, project.text
    dataset = await client.post("/api/datasets", json={
        "name": f"test-delete-catalog-dataset-{uuid4().hex}", "project_id": project.json()["id"],
    })
    assert dataset.status_code == 201, dataset.text
    dataset_id = dataset.json()["id"]
    assert await _classes(client, dataset_id) == {"catalog_version": 0, "classes": []}
    before_join, release = asyncio.Event(), asyncio.Event()
    original_execute = AsyncSession.execute

    async def pause_before_catalog_join(session, statement, *args, **kwargs):
        sql = str(statement).lower()
        if (not before_join.is_set() and getattr(statement, "is_select", False)
                and "dataset_classes" in sql and "class_catalog_version" in sql):
            # Ownership/existence lookup has already succeeded; the snapshot
            # query must handle a deletion committed before it executes.
            before_join.set()
            await asyncio.wait_for(release.wait(), timeout=10)
        return await original_execute(session, statement, *args, **kwargs)

    monkeypatch.setattr(AsyncSession, "execute", pause_before_catalog_join)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test", headers=client.headers,
    ) as reading_client:
        tasks = [asyncio.create_task(reading_client.get(f"/api/datasets/{dataset_id}/classes"))]
        try:
            await asyncio.wait_for(before_join.wait(), timeout=5)
            deleted = await asyncio.wait_for(client.delete(f"/api/datasets/{dataset_id}"), timeout=5)
            assert deleted.status_code == 204, deleted.text
            release.set()
            fetched = await asyncio.wait_for(tasks[0], timeout=5)
            assert fetched.status_code == 404, fetched.text
            assert (await client.get(f"/api/datasets/{dataset_id}")).status_code == 404
        finally:
            release.set()
            await _finish_tasks(tasks)


@pytest.mark.asyncio
@pytest.mark.parametrize("supplied", ["missing", "null"])
async def test_annotation_put_requires_catalog_version_without_writes(client, app, auth_headers, annotation_case, supplied):
    case = annotation_case
    before = await _snapshot(app, case["images"]["blank"])
    payload = {"expected_version": before["version"], "boxes": [BOX_ZERO]}
    if supplied == "null":
        payload["expected_catalog_version"] = None
    response = await client.put(_url(case), headers=_headers(auth_headers, case), json=payload)
    assert response.status_code == 422, response.text
    assert response.json()["detail"] == {"code": "expected_catalog_version_required"}
    assert await _snapshot(app, case["images"]["blank"]) == before


@pytest.mark.asyncio
async def test_catalog_mismatch_returns_complete_current_and_matching_write_succeeds(client, app, auth_headers, annotation_case):
    case = annotation_case
    headers = _headers(auth_headers, case)
    before = await _snapshot(app, case["images"]["blank"])
    response = await client.put(_url(case), headers=headers, json={
        "expected_version": before["version"], "expected_catalog_version": before["catalog_version"] + 1,
        "boxes": [BOX_ZERO],
    })
    assert response.status_code == 409, response.text
    assert response.json()["detail"] == {"code": "class_catalog_changed", "current": _current(before)}
    assert await _snapshot(app, case["images"]["blank"]) == before
    saved = await client.put(_url(case), headers=headers, json={
        "expected_version": before["version"], "expected_catalog_version": before["catalog_version"],
        "boxes": [BOX_ZERO],
    })
    assert saved.status_code == 200, saved.text
    assert saved.json()["catalog_version"] == before["catalog_version"]
    assert saved.json()["version"] == before["version"] + 1
    assert await _classes(client, case["dataset_ids"][0], headers) == {
        "classes": before["classes"], "catalog_version": before["catalog_version"],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("catalog_payload", [{}, {"expected_catalog_version": None}, {"expected_catalog_version": 999999}])
async def test_image_version_conflict_precedes_catalog_validation(client, app, auth_headers, annotation_case, catalog_payload):
    case = annotation_case
    before = await _snapshot(app, case["images"]["labeled"])
    response = await client.put(_url(case, "labeled"), headers=_headers(auth_headers, case), json={
        "expected_version": before["version"] - 1, "boxes": [BOX_ZERO], **catalog_payload,
    })
    assert response.status_code == 409, response.text
    assert response.json()["detail"] == {"code": "version_conflict", "current": _current(before)}
    assert await _snapshot(app, case["images"]["labeled"]) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["dataset_rename", "project_rename", "project_reorder"])
async def test_class_mutations_bump_each_affected_catalog_and_report_the_snapshot(client, app, auth_headers, annotation_case, operation):
    case = annotation_case
    headers = _headers(auth_headers, case, ADMIN)
    # Different versions make a single project-wide version an invalid shortcut.
    async with app.state.session_factory() as session:
        await session.execute(sa.text("UPDATE dataset_classes SET name = name WHERE dataset_id = :id AND class_id = 0"), {"id": case["dataset_ids"][1]})
        await session.commit()
    before = await _all_catalogs(client, case, auth_headers)
    first_id, second_id, outside_id = case["dataset_ids"]
    assert before[first_id]["catalog_version"] != before[second_id]["catalog_version"]
    if operation == "dataset_rename":
        response = await client.patch(f"/api/datasets/{first_id}/classes/0", headers=headers, json={"name": "human"})
    elif operation == "project_rename":
        response = await client.patch(f"/api/projects/{case['project_id']}/classes/0", headers=headers, json={"name": "human"})
    else:
        project = await client.get(f"/api/projects/{case['project_id']}", headers=headers)
        assert project.status_code == 200, project.text
        response = await client.put(f"/api/projects/{case['project_id']}/classes", headers=headers, json={
            "expected_updated_at": project.json()["updated_at"], "classes": [
                {"class_id": 1, "name": "vehicle", "color": "#654321"},
                {"class_id": 0, "name": "person", "color": "#123456"},
            ],
        })
    assert response.status_code == 200, response.text
    after = await _all_catalogs(client, case, auth_headers)
    assert after[outside_id] == before[outside_id]
    for dataset_id in (first_id, second_id):
        assert after[dataset_id]["catalog_version"] == before[dataset_id]["catalog_version"] + 1
        assert after[dataset_id]["classes"] == ([
            {"class_id": 0, "name": "vehicle"}, {"class_id": 1, "name": "person"},
        ] if operation == "project_reorder" else [
            {"class_id": 0, "name": "human"}, {"class_id": 1, "name": "vehicle"},
        ])
    if operation == "dataset_rename":
        assert response.json() == {"class_id": 0, "name": "human", **after[first_id]}
    else:
        assert sorted(response.json()["catalog_versions"], key=lambda item: item["dataset_id"]) == [
            {"dataset_id": dataset_id, "catalog_version": after[dataset_id]["catalog_version"]}
            for dataset_id in sorted((first_id, second_id))
        ]
    # Even a freshly loaded image version cannot authorize a stale class catalog.
    fresh = await client.get(_url(case, "labeled"), headers=headers)
    assert fresh.status_code == 200, fresh.text
    assert fresh.json()["catalog_version"] == after[first_id]["catalog_version"]
    unchanged = await _snapshot(app, case["images"]["labeled"])
    stale = await client.put(_url(case, "labeled"), headers=headers, json={
        "expected_version": fresh.json()["version"],
        "expected_catalog_version": before[first_id]["catalog_version"], "boxes": [BOX_ZERO],
    })
    assert stale.status_code == 409, stale.text
    assert stale.json()["detail"] == {"code": "class_catalog_changed", "current": _current(unchanged)}
    assert await _snapshot(app, case["images"]["labeled"]) == unchanged


@pytest.mark.asyncio
@pytest.mark.parametrize("order", [(1, 0, 2), (2, 0, 1)], ids=["swap", "three-cycle"])
async def test_project_reorder_bumps_full_and_sparse_catalogs_once_without_changing_box_meaning(
    client, app, order,
):
    names = ("cat", "dog", "bird")
    colors = ("#123456", "#654321", "#ABCDEF")
    suffix = uuid4().hex
    project_ids = []
    for label in ("target", "outside"):
        response = await client.post("/api/projects", json={
            "name": f"test-catalog-subsets-{label}-{suffix}",
            "classes": [{"name": name, "color": color} for name, color in zip(names, colors, strict=True)],
        })
        assert response.status_code == 201, response.text
        project_ids.append(response.json()["id"])

    # The subsets cover full key cycles, retained keys, moves into free keys,
    # and the empty transition table. All belong to one real reorder request.
    subsets = ((0, 1, 2), (0, 1, 2), (0,), (1,), (2,), (0, 1), (0, 2), (1, 2), ())
    rows = []
    async with app.state.session_factory() as session:
        workspace_id = await session.scalar(sa.select(Project.workspace_id).where(Project.id == project_ids[0]))
        for index, subset in enumerate((*subsets, (0, 1, 2))):
            outside = index == len(subsets)
            dataset = Dataset(
                workspace_id=workspace_id, project_id=project_ids[int(outside)],
                name=f"test-catalog-subset-{index}-{suffix}", status="ready",
                storage_path=f"datasets/catalog-subset-{index}-{suffix}",
                class_count=len(subset), image_count=1, annotation_count=len(subset),
            )
            session.add(dataset)
            await session.flush()
            session.add_all([
                DatasetClass(dataset_id=dataset.id, class_id=class_id, name=names[class_id])
                for class_id in subset
            ])
            image = image_with_media(
                workspace_id=workspace_id, dataset_id=dataset.id,
                stem=f"subset-{index}", filename=f"subset-{index}.jpg",
                rel_path=f"images/subset-{index}.jpg", split=None, width=800, height=600,
                file_path=f"{dataset.storage_path}/source.jpg",
                thumb_path=f"{dataset.storage_path}/thumb.jpg",
                box_count=len(subset), has_label_source=bool(subset), is_modified=False,
                version=10 + index, label_status="labeled" if subset else "unlabeled",
            )
            session.add(image)
            await session.flush()
            session.add_all([
                Annotation(image_id=image.id, class_id=class_id, cx=0.2 + class_id * 0.2,
                           cy=0.4, w=0.1, h=0.2)
                for class_id in subset
            ])
            rows.append((dataset.id, image.id, subset, outside))
        await session.commit()

    before_catalogs = {dataset_id: await _classes(client, dataset_id) for dataset_id, *_ in rows}
    before_images = {}
    for _dataset_id, image_id, _subset, _outside in rows:
        fetched = await client.get(f"/api/images/{image_id}/annotations")
        assert fetched.status_code == 200, fetched.text
        before_images[image_id] = fetched.json()
    project = await client.get(f"/api/projects/{project_ids[0]}")
    assert project.status_code == 200, project.text
    reordered = await client.put(f"/api/projects/{project_ids[0]}/classes", json={
        "expected_updated_at": project.json()["updated_at"],
        "classes": [{"class_id": old_id, "name": names[old_id], "color": colors[old_id]} for old_id in order],
    })
    assert reordered.status_code == 200, reordered.text
    assert reordered.json()["classes"] == [
        {"class_id": new_id, "name": names[old_id], "color": colors[old_id]}
        for new_id, old_id in enumerate(order)
    ]
    remap = {old_id: new_id for new_id, old_id in enumerate(order)}
    response_versions = {row["dataset_id"]: row["catalog_version"] for row in reordered.json()["catalog_versions"]}
    assert set(response_versions) == {dataset_id for dataset_id, _image_id, _subset, outside in rows if not outside}
    for dataset_id, image_id, subset, outside in rows:
        catalog = await _classes(client, dataset_id)
        fetched = await client.get(f"/api/images/{image_id}/annotations")
        assert fetched.status_code == 200, fetched.text
        image = fetched.json()
        before = before_images[image_id]
        if outside:
            assert catalog == before_catalogs[dataset_id]
            assert image == before
            continue
        expected_classes = sorted(
            [{"class_id": remap[old_id], "name": names[old_id]} for old_id in subset],
            key=lambda row: row["class_id"],
        )
        catalog_changed = expected_classes != before_catalogs[dataset_id]["classes"]
        expected_version = before_catalogs[dataset_id]["catalog_version"] + int(catalog_changed)
        assert catalog == {"catalog_version": expected_version, "classes": expected_classes}
        assert response_versions[dataset_id] == expected_version
        assert image["catalog_version"] == expected_version and image["classes"] == expected_classes
        assert image["version"] == before["version"] + 1
        expected_boxes = [{**box, "class_id": remap[box["class_id"]]} for box in before["boxes"]]
        assert image["boxes"] == expected_boxes
        after_names = {row["class_id"]: row["name"] for row in image["classes"]}
        assert [after_names[box["class_id"]] for box in image["boxes"]] == [
            names[box["class_id"]] for box in before["boxes"]
        ]
        assert image["label_status"] == before["label_status"]
        assert image["assignee_id"] == before["assignee_id"]


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["color-only", "identity"])
async def test_project_class_put_without_catalog_changes_keeps_labeler_save_valid(
    client, app, auth_headers, annotation_case, change,
):
    case = annotation_case
    admin_headers = _headers(auth_headers, case, ADMIN)
    labeler_headers = _headers(auth_headers, case)
    before = await _all_catalogs(client, case, auth_headers)
    image_before = await client.get(_url(case), headers=labeler_headers)
    assert image_before.status_code == 200, image_before.text
    other_before = await _snapshot(app, case["images"]["imported"])
    project = await client.get(f"/api/projects/{case['project_id']}", headers=admin_headers)
    assert project.status_code == 200, project.text
    colors = ("#112233", "#445566") if change == "color-only" else ("#123456", "#654321")
    classes = [{"class_id": class_id, "name": name, "color": color}
               for class_id, (name, color) in enumerate(zip(("person", "vehicle"), colors, strict=True))]
    updated = await client.put(f"/api/projects/{case['project_id']}/classes", headers=admin_headers, json={
        "expected_updated_at": project.json()["updated_at"], "classes": classes,
    })
    assert updated.status_code == 200, updated.text
    assert updated.json()["classes"] == classes
    assert await _all_catalogs(client, case, auth_headers) == before
    assert await _snapshot(app, case["images"]["imported"]) == other_before
    assert {row["dataset_id"]: row["catalog_version"] for row in updated.json()["catalog_versions"]} == {
        dataset_id: before[dataset_id]["catalog_version"] for dataset_id in case["dataset_ids"][:2]
    }
    image_after = await client.get(_url(case), headers=labeler_headers)
    assert image_after.status_code == 200 and image_after.json() == image_before.json()
    saved = await client.put(_url(case), headers=labeler_headers, json={
        "expected_version": image_before.json()["version"],
        "expected_catalog_version": image_before.json()["catalog_version"], "boxes": [BOX_ZERO],
    })
    assert saved.status_code == 200, saved.text
    assert saved.json()["catalog_version"] == image_before.json()["catalog_version"]
    assert saved.json()["version"] == image_before.json()["version"] + 1


@pytest.mark.asyncio
async def test_project_merge_catalog_rewrites_are_bumped_by_the_trigger(client, app):
    case = await merge_with_annotation_versions(client, app)
    for key, image_id in case["image_ids"].items():
        response = await client.get(f"/api/images/{image_id}/annotations")
        assert response.status_code == 200, response.text
        assert response.json()["catalog_version"] > case["before"][key]["catalog_version"]
        assert response.json()["classes"] == [
            {"class_id": 0, "name": "dog"}, {"class_id": 1, "name": "cat"}, {"class_id": 2, "name": "bird"},
        ]


@pytest.mark.asyncio
async def test_upload_class_resolution_bumps_target_and_sibling_catalogs(client, app, tmp_path):
    _project_id, dataset_id, job_id, sibling_id = await _project_dataset_job(client, app, sibling=True)
    assert sibling_id is not None
    before = {key: await _classes(client, key) for key in (dataset_id, sibling_id)}
    files = _incoming_files(tmp_path)
    plan = await _pause_for_resolution(app, job_id, files)
    accepted = await client.post(f"/api/jobs/{job_id}/class-resolution", json={
        "revision": plan["revision"], "resolutions": [{"key": "class:1", "action": "use_upload"}],
    })
    assert accepted.status_code == 202, accepted.text
    await ingest_collected(app.state.settings, app.state.session_factory, job_id, files, require_class_resolution=True)
    for key in (dataset_id, sibling_id):
        after = await _classes(client, key)
        assert after["catalog_version"] > before[key]["catalog_version"]
        assert {"class_id": 1, "name": "orklift"} in after["classes"]


@pytest.mark.asyncio
async def test_concurrent_ingest_catalog_rename_does_not_deadlock_on_sibling_dataset(
    client, app, tmp_path, monkeypatch,
):
    """Cross real checkpoint locks with the other upload's real planning locks."""
    _project_id, first_dataset, first_job, second_dataset = await _project_dataset_job(client, app, sibling=True)
    assert second_dataset is not None
    async with app.state.session_factory() as session:
        second = UploadJob(dataset_id=second_dataset, kind="folder", state="queued", phase="uploading",
                           total=0, processed=0, failed=0, upload_ids=[])
        session.add(second)
        await session.commit()
        second_job = second.id
    jobs = [first_job, second_job]
    dataset_ids = [first_dataset, second_dataset]
    before = {dataset_id: await _classes(client, dataset_id) for dataset_id in dataset_ids}
    files_by_job = {}
    for job_id in jobs:
        source = tmp_path / f"incoming-{job_id}"
        source.mkdir()
        files = _incoming_files(source)
        files_by_job[job_id] = files
        plan = await _pause_for_resolution(app, job_id, files)
        accepted = await client.post(f"/api/jobs/{job_id}/class-resolution", json={
            "revision": plan["revision"], "resolutions": [{"key": "class:1", "action": "use_upload"}],
        })
        assert accepted.status_code == 202, accepted.text

    task_names = [f"catalog-ingest-{job_id}" for job_id in jobs]
    first_checkpoint_locked, second_project_attempted = asyncio.Event(), asyncio.Event()
    release_first = asyncio.Event()
    original_scalar = AsyncSession.scalar
    first_project_locks = 0
    backend_pids = {}
    evidence = {"second_dataset_locked_before_project": False}

    async def scalar_at_ingest_lock(session, statement, *args, **kwargs):
        nonlocal first_project_locks
        task = asyncio.current_task()
        label = task.get_name() if task else None
        lock = getattr(statement, "_for_update_arg", None)
        is_write_lock = getattr(statement, "is_select", False) and lock is not None and not lock.read
        sql = str(statement).lower()
        project_lock = is_write_lock and "from projects" in sql
        if label == task_names[1] and project_lock and not second_project_attempted.is_set():
            backend_pids["second"] = await original_scalar(session, sa.text("SELECT pg_backend_pid()"))
            second_project_attempted.set()
        result = await original_scalar(session, statement, *args, **kwargs)
        if label == task_names[1] and is_write_lock and "from datasets" in sql and not second_project_attempted.is_set():
            evidence["second_dataset_locked_before_project"] = True
        if label == task_names[0] and project_lock:
            first_project_locks += 1
            # First is planning; second is the first durable image checkpoint.
            if first_project_locks == 2:
                backend_pids["first"] = await original_scalar(session, sa.text("SELECT pg_backend_pid()"))
                first_checkpoint_locked.set()
                await asyncio.wait_for(release_first.wait(), timeout=15)
        return result

    monkeypatch.setattr(AsyncSession, "scalar", scalar_at_ingest_lock)

    def start(job_id, name):
        return asyncio.create_task(ingest_collected(
            app.state.settings, app.state.session_factory, job_id, files_by_job[job_id],
            require_class_resolution=True,
        ), name=name)

    tasks = [start(first_job, task_names[0])]
    try:
        await asyncio.wait_for(first_checkpoint_locked.wait(), timeout=10)
        tasks.append(start(second_job, task_names[1]))
        await asyncio.wait_for(second_project_attempted.wait(), timeout=10)

        async def observe_project_wait():
            async with app.state.session_factory() as observer:
                while not tasks[1].done():
                    blocked = await observer.scalar(sa.text(
                        "SELECT CAST(:first AS INTEGER) = ANY(pg_blocking_pids(CAST(:second AS INTEGER)))"
                    ), backend_pids)
                    if blocked:
                        return True
            return False  # A NOWAIT refusal can finish before the observer query.

        evidence["second_waited_for_first_project"] = await asyncio.wait_for(observe_project_wait(), timeout=5)
        release_first.set()
        results = await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=15)
        for job_id, result in zip(jobs, results, strict=True):
            if isinstance(result, BaseException):
                # A retryable 55P03 refusal is safe; a deadlock (40P01) is not.
                assert is_lock_not_available(result), f"ingest {job_id}: {result!r}; locks={evidence}"
                async with app.state.session_factory() as session:
                    job = await session.get(UploadJob, job_id)
                    assert job is not None and job.state != "failed"
                await asyncio.wait_for(ingest_collected(
                    app.state.settings, app.state.session_factory, job_id, files_by_job[job_id],
                    require_class_resolution=True,
                ), timeout=15)
        async with app.state.session_factory() as session:
            for job_id, dataset_id in zip(jobs, dataset_ids, strict=True):
                job = await session.get(UploadJob, job_id)
                dataset = await session.get(Dataset, dataset_id)
                assert job is not None and job.state == "done" and job.ingest_cursor == 1
                assert dataset is not None and dataset.image_count == 1
                assert await session.scalar(sa.select(sa.func.count(Image.id)).where(Image.dataset_id == dataset_id)) == 1
        for dataset_id in dataset_ids:
            after = await _classes(client, dataset_id)
            assert after["catalog_version"] > before[dataset_id]["catalog_version"]
            assert {"class_id": 1, "name": "orklift"} in after["classes"]
    finally:
        release_first.set()
        await _finish_tasks(tasks)


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["annotations", "classes"])
async def test_catalog_get_uses_one_sql_snapshot_while_name_changes(client, auth_headers, annotation_case, monkeypatch, endpoint):
    case = annotation_case
    headers = _headers(auth_headers, case, ADMIN)
    dataset_id = case["dataset_ids"][0]
    url = _url(case, "labeled") if endpoint == "annotations" else f"/api/datasets/{dataset_id}/classes"
    before_response = await client.get(url, headers=headers)
    assert before_response.status_code == 200, before_response.text
    before = before_response.json()
    acquired, release = asyncio.Event(), asyncio.Event()
    original_execute = AsyncSession.execute

    async def pause_snapshot(session, statement, *args, **kwargs):
        result = await original_execute(session, statement, *args, **kwargs)
        sql = str(statement).lower()
        if (not acquired.is_set() and getattr(statement, "is_select", False)
                and "dataset_classes" in sql and "class_catalog_version" in sql):
            acquired.set()
            await asyncio.wait_for(release.wait(), timeout=10)
        return result

    monkeypatch.setattr(AsyncSession, "execute", pause_snapshot)
    tasks = [asyncio.create_task(client.get(url, headers=headers))]
    try:
        await asyncio.wait_for(acquired.wait(), timeout=5)
        renamed = await asyncio.wait_for(client.patch(f"/api/datasets/{dataset_id}/classes/0", headers=headers, json={"name": "human"}), timeout=5)
        assert renamed.status_code == 200, renamed.text
        release.set()
        fetched = await asyncio.wait_for(tasks[0], timeout=5)
        assert fetched.status_code == 200, fetched.text
        assert fetched.json() == before
        after_response = await client.get(url, headers=headers)
        assert after_response.status_code == 200, after_response.text
        after = after_response.json()
        assert after["catalog_version"] > before["catalog_version"]
        assert after["classes"] == [{"class_id": 0, "name": "human"}, {"class_id": 1, "name": "vehicle"}]
        if endpoint == "annotations":
            assert after["version"] == before["version"]
            assert after["boxes"] == before["boxes"]
    finally:
        release.set()
        await _finish_tasks(tasks)


@pytest.mark.asyncio
async def test_project_and_dataset_renames_serialize_without_deadlock(client, auth_headers, annotation_case, monkeypatch):
    case = annotation_case
    headers = _headers(auth_headers, case, ADMIN)
    before = await _all_catalogs(client, case, auth_headers)
    first_locked, second_attempted, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original_scalar, original_execute = AsyncSession.scalar, AsyncSession.execute
    arrivals = 0

    async def coordinate_project_lock(original, session, statement, args, kwargs):
        nonlocal arrivals
        lock = getattr(statement, "_for_update_arg", None)
        position = None
        if (getattr(statement, "is_select", False) and lock is not None and not lock.read
                and "from projects" in str(statement).lower()):
            arrivals += 1
            position = arrivals
            if position == 2:
                second_attempted.set()
        result = await original(session, statement, *args, **kwargs)
        if position == 1:
            first_locked.set()  # First rename already owns its real Project row lock.
            await asyncio.wait_for(release.wait(), timeout=10)
        return result

    async def scalar_with_lock(session, statement, *args, **kwargs):
        return await coordinate_project_lock(original_scalar, session, statement, args, kwargs)

    async def execute_with_lock(session, statement, *args, **kwargs):
        return await coordinate_project_lock(original_execute, session, statement, args, kwargs)

    monkeypatch.setattr(AsyncSession, "scalar", scalar_with_lock)
    monkeypatch.setattr(AsyncSession, "execute", execute_with_lock)
    requests = [
        (f"/api/projects/{case['project_id']}/classes/0", {"name": "human"}),
        (f"/api/datasets/{case['dataset_ids'][0]}/classes/1", {"name": "car"}),
    ]
    tasks = [asyncio.create_task(client.patch(requests[0][0], headers=headers, json=requests[0][1]))]
    try:
        await asyncio.wait_for(first_locked.wait(), timeout=5)
        tasks.append(asyncio.create_task(client.patch(requests[1][0], headers=headers, json=requests[1][1])))
        await asyncio.wait_for(second_attempted.wait(), timeout=5)
        release.set()
        responses = await asyncio.wait_for(asyncio.gather(*tasks), timeout=10)
        assert any(response.status_code == 200 for response in responses)
        for (url, body), response in zip(requests, responses, strict=True):
            assert response.status_code in (200, 409), response.text
            if response.status_code == 409:
                retry = await asyncio.wait_for(client.patch(url, headers=headers, json=body), timeout=5)
                assert retry.status_code == 200, retry.text
        after = await _all_catalogs(client, case, auth_headers)
        for dataset_id in case["dataset_ids"][:2]:
            assert after[dataset_id]["catalog_version"] == before[dataset_id]["catalog_version"] + 2
            assert after[dataset_id]["classes"] == [{"class_id": 0, "name": "human"}, {"class_id": 1, "name": "car"}]
        assert after[case["dataset_ids"][2]] == before[case["dataset_ids"][2]]
    finally:
        release.set()
        await _finish_tasks(tasks)


@pytest.fixture
def catalog_predecessor_db(tmp_path, monkeypatch):
    """Exercise 0023 in a private rollback-only schema, never shared public."""
    assert ROOT.name in ("annodock", "annodock-ws")
    url = make_url(TEST_DATABASE_URL)
    assert url.database.endswith("_test") and url.get_backend_name() == "postgresql"
    engine = sa.create_engine(url.set(drivername="postgresql+psycopg"), poolclass=NullPool)
    monkeypatch.setenv("STORAGE_DIR", str(tmp_path / "migration-storage"))
    try:
        with engine.connect() as connection:
            transaction = connection.begin()
            try:
                assert connection.scalar(sa.text("SELECT current_database()")).endswith("_test")
                schema = f"catalog_migration_{uuid4().hex}"
                connection.execute(sa.schema.CreateSchema(schema))
                connection.exec_driver_sql(f'SET LOCAL search_path TO "{schema}"')
                previous = None
                for path in sorted(VERSIONS.glob("[0-9][0-9][0-9][0-9]_*.py")):
                    if int(path.name[:4]) > 22:
                        continue
                    step = _load_migration(path)
                    assert step.down_revision == previous
                    _apply(connection, step, "upgrade")
                    previous = step.revision
                assert previous == "0022_image_label_status"
                yield connection
            finally:
                transaction.rollback()
    finally:
        engine.dispose()


def _seed_catalog_dataset(connection, suffix):
    actor = 974001 + connection.scalar(sa.text("SELECT count(*) FROM workspaces"))
    workspace = connection.scalar(sa.text("INSERT INTO workspaces(kind,name,created_by) VALUES ('personal',:name,:actor) RETURNING id"), {"name": f"test-catalog-{suffix}", "actor": actor})
    project = connection.scalar(sa.text("INSERT INTO projects(workspace_id,name) VALUES (:workspace,:name) RETURNING id"), {"workspace": workspace, "name": f"test-catalog-project-{suffix}"})
    return connection.scalar(sa.text("INSERT INTO datasets(workspace_id,project_id,name,storage_path) VALUES (:workspace,:project,:name,'datasets/catalog') RETURNING id"), {"workspace": workspace, "project": project, "name": f"test-catalog-dataset-{suffix}"})


def _stored_version(connection, dataset_id):
    return connection.scalar(sa.text("SELECT class_catalog_version FROM datasets WHERE id = :id"), {"id": dataset_id})


def test_catalog_migration_defaults_statement_trigger_and_transaction_rollback(catalog_predecessor_db):
    connection = catalog_predecessor_db
    dataset_id = _seed_catalog_dataset(connection, "existing")
    connection.execute(sa.text("INSERT INTO dataset_classes(dataset_id,class_id,name) VALUES (:id,0,'person')"), {"id": dataset_id})
    migration = _load_migration(VERSIONS / f"{REVISION}.py")
    assert migration.revision == REVISION and migration.down_revision == "0022_image_label_status"
    _apply(connection, migration, "upgrade")
    assert _stored_version(connection, dataset_id) == 0
    schema = connection.scalar(sa.text("SELECT current_schema()"))
    column = next(row for row in sa.inspect(connection).get_columns("datasets", schema=schema) if row["name"] == "class_catalog_version")
    assert isinstance(column["type"], sa.Integer) and not column["nullable"] and column["default"] == "0"
    new_id = _seed_catalog_dataset(connection, "new")
    assert _stored_version(connection, new_id) == 0
    connection.execute(sa.text("INSERT INTO dataset_classes(dataset_id,class_id,name) VALUES (:id,0,'person'),(:id,1,'vehicle')"), {"id": new_id})
    assert _stored_version(connection, new_id) == 1
    connection.execute(sa.text("UPDATE dataset_classes SET name='human' WHERE dataset_id=:id AND class_id=0"), {"id": new_id})
    assert _stored_version(connection, new_id) == 2
    connection.execute(sa.text("DELETE FROM dataset_classes WHERE dataset_id=:id AND class_id=1"), {"id": new_id})
    assert _stored_version(connection, new_id) == 3
    savepoint = connection.begin_nested()
    connection.execute(sa.text("UPDATE dataset_classes SET name='rolled-back' WHERE dataset_id=:id"), {"id": new_id})
    assert _stored_version(connection, new_id) == 4
    savepoint.rollback()
    assert _stored_version(connection, new_id) == 3
    assert _rows(connection, "SELECT class_id,name FROM dataset_classes WHERE dataset_id=:id", id=new_id) == [(0, "human")]
    assert _stored_version(connection, dataset_id) == 0


def test_catalog_statement_trigger_bumps_each_affected_dataset_once(catalog_predecessor_db):
    connection = catalog_predecessor_db
    migration = _load_migration(VERSIONS / f"{REVISION}.py")
    _apply(connection, migration, "upgrade")
    first = _seed_catalog_dataset(connection, "statement-first")
    second = _seed_catalog_dataset(connection, "statement-second")
    untouched = _seed_catalog_dataset(connection, "statement-untouched")
    parameters = {"first": first, "second": second}

    def versions():
        assert _stored_version(connection, untouched) == 0
        return [_stored_version(connection, dataset_id) for dataset_id in (first, second)]

    connection.execute(sa.text("""
        INSERT INTO dataset_classes(dataset_id,class_id,name) VALUES
        (:first,0,'person'),(:first,1,'vehicle'),(:second,0,'person'),(:second,1,'vehicle')
    """), parameters)
    assert versions() == [1, 1]
    connection.execute(sa.text("""
        UPDATE dataset_classes SET name=name || '-renamed'
        WHERE dataset_id IN (:first,:second)
    """), parameters)
    assert versions() == [2, 2]
    # UPDATE transition tables must include both the old and new dataset IDs.
    connection.execute(sa.text("""
        UPDATE dataset_classes SET dataset_id=:second,class_id=2
        WHERE dataset_id=:first AND class_id=0
    """), parameters)
    assert versions() == [3, 3]
    connection.execute(sa.text("DELETE FROM dataset_classes WHERE dataset_id IN (:first,:second)"), parameters)
    assert versions() == [4, 4]
    # A statement with no affected rows must not manufacture a new catalog.
    connection.execute(sa.text("UPDATE dataset_classes SET name=name WHERE dataset_id IN (:first,:second)"), parameters)
    connection.execute(sa.text("DELETE FROM dataset_classes WHERE dataset_id IN (:first,:second)"), parameters)
    assert versions() == [4, 4]


def test_catalog_migration_downgrade_upgrade_preserves_data_and_reinstalls_trigger(catalog_predecessor_db):
    connection = catalog_predecessor_db
    dataset_id = _seed_catalog_dataset(connection, "roundtrip")
    connection.execute(sa.text("INSERT INTO dataset_classes(dataset_id,class_id,name) VALUES (:id,0,'person'),(:id,1,'vehicle')"), {"id": dataset_id})
    before_schema = _schema_snapshot(connection)
    before_data = _rows(connection, "SELECT * FROM datasets ORDER BY id")
    before_classes = _rows(connection, "SELECT * FROM dataset_classes ORDER BY dataset_id,class_id")
    migration = _load_migration(VERSIONS / f"{REVISION}.py")
    _apply(connection, migration, "upgrade")
    functions = _rows(connection, "SELECT t.tgfoid FROM pg_trigger t WHERE t.tgrelid='dataset_classes'::regclass AND NOT t.tgisinternal")
    assert functions
    connection.execute(sa.text("UPDATE dataset_classes SET name=name WHERE dataset_id=:id"), {"id": dataset_id})
    assert _stored_version(connection, dataset_id) == 1
    _apply(connection, migration, "downgrade")
    assert _schema_snapshot(connection) == before_schema
    assert _rows(connection, "SELECT * FROM datasets ORDER BY id") == before_data
    assert _rows(connection, "SELECT * FROM dataset_classes ORDER BY dataset_id,class_id") == before_classes
    assert _rows(connection, "SELECT t.tgfoid FROM pg_trigger t WHERE t.tgrelid='dataset_classes'::regclass AND NOT t.tgisinternal") == []
    for (oid,) in functions:
        assert connection.scalar(sa.text("SELECT count(*) FROM pg_proc WHERE oid=:oid"), {"oid": oid}) == 0
    _apply(connection, migration, "upgrade")
    assert _stored_version(connection, dataset_id) == 0
    connection.execute(sa.text("UPDATE dataset_classes SET name='human' WHERE dataset_id=:id"), {"id": dataset_id})
    assert _stored_version(connection, dataset_id) == 1


@pytest.mark.parametrize("existing_trigger", ["statement", "legacy-row"])
def test_catalog_migration_upgrade_is_repeatable_and_replaces_legacy_row_trigger(
    catalog_predecessor_db, existing_trigger,
):
    connection = catalog_predecessor_db
    migration = _load_migration(VERSIONS / f"{REVISION}.py")
    dataset_id = _seed_catalog_dataset(connection, f"repeat-{existing_trigger}")
    _apply(connection, migration, "upgrade")
    connection.execute(sa.text("""
        INSERT INTO dataset_classes(dataset_id,class_id,name)
        VALUES (:id,0,'person'),(:id,1,'vehicle')
    """), {"id": dataset_id})
    if existing_trigger == "legacy-row":
        # Recreate the unpublished first form in this rollback-only schema.
        for operation in ("insert", "update", "delete"):
            connection.execute(sa.text(f"DROP TRIGGER dataset_classes_catalog_version_{operation} ON dataset_classes"))
        connection.execute(sa.text("""
            CREATE OR REPLACE FUNCTION bump_dataset_catalog_version() RETURNS trigger
            LANGUAGE plpgsql AS $$
            BEGIN
                IF TG_OP = 'DELETE' THEN
                    UPDATE datasets SET class_catalog_version=class_catalog_version+1 WHERE id=OLD.dataset_id;
                    RETURN OLD;
                END IF;
                UPDATE datasets SET class_catalog_version=class_catalog_version+1 WHERE id=NEW.dataset_id;
                RETURN NEW;
            END;
            $$
        """))
        connection.execute(sa.text("""
            CREATE TRIGGER dataset_classes_catalog_version
            AFTER INSERT OR UPDATE OR DELETE ON dataset_classes
            FOR EACH ROW EXECUTE FUNCTION bump_dataset_catalog_version()
        """))
    connection.execute(sa.text("UPDATE dataset_classes SET name=name WHERE dataset_id=:id"), {"id": dataset_id})
    before_version = _stored_version(connection, dataset_id)
    before_data = _rows(connection, "SELECT * FROM datasets ORDER BY id")
    before_classes = _rows(connection, "SELECT * FROM dataset_classes ORDER BY dataset_id,class_id")

    _apply(connection, migration, "upgrade")

    assert _rows(connection, "SELECT * FROM datasets ORDER BY id") == before_data
    assert _rows(connection, "SELECT * FROM dataset_classes ORDER BY dataset_id,class_id") == before_classes
    triggers = _rows(connection, """
        SELECT tgname, (tgtype::integer & 1) FROM pg_trigger
        WHERE tgrelid='dataset_classes'::regclass AND NOT tgisinternal ORDER BY tgname
    """)
    assert triggers == [(f"dataset_classes_catalog_version_{operation}", 0)
                        for operation in ("delete", "insert", "update")]
    connection.execute(sa.text("UPDATE dataset_classes SET name=name || '-new' WHERE dataset_id=:id"), {"id": dataset_id})
    assert _stored_version(connection, dataset_id) == before_version + 1
