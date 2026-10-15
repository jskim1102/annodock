from __future__ import annotations

import asyncio
from contextvars import ContextVar
from pathlib import Path
from uuid import uuid4

import pytest
import yaml
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Annotation, Dataset, DatasetClass, Image, Project, ProjectClass, TrainingRun
from app.services import training
from app.services.storage import contained_storage_path
from tests.factories import image_with_media


pytestmark = pytest.mark.asyncio


def _name(suffix: str) -> str:
    return f"test-class-rename-{suffix}-{uuid4().hex}"


def _host_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(training, "is_container_environment", lambda: False)
    monkeypatch.setattr(training.torch.cuda, "is_available", lambda: True)
    monkeypatch.setattr(
        training.torch.cuda,
        "mem_get_info",
        lambda: (24 * 1024**3, 24 * 1024**3),
    )
    monkeypatch.setattr(
        training,
        "spawn_worker",
        lambda _run_id, _owner_id, _out_dir, _database_url: training.SpawnedWorker(
            pid=4242,
            pid_started_at="123456",
            boot_id="test-boot-id",
        ),
    )


async def _dataset_with_labels(client, app) -> int:
    response = await client.post("/api/datasets", json={"name": _name("data")})
    assert response.status_code == 201
    dataset_id = response.json()["id"]
    async with app.state.session_factory() as session:
        dataset = await session.get(Dataset, dataset_id)
        assert dataset is not None
        storage = contained_storage_path(
            app.state.settings.storage_dir,
            dataset.storage_path,
        )
        session.add_all(
            [
                DatasetClass(dataset_id=dataset_id, class_id=0, name="orklift"),
                DatasetClass(dataset_id=dataset_id, class_id=1, name="person"),
            ]
        )
        for index in range(10):
            source = storage / f"image-{index}.jpg"
            source.write_bytes(f"image-{index}".encode())
            image = image_with_media(
                workspace_id=dataset.workspace_id,
                dataset_id=dataset_id,
                stem=f"image-{index}",
                filename=source.name,
                rel_path=f"incoming/{source.name}",
                split=None,
                width=32,
                height=24,
                file_path=str(source),
                display_path=None,
                thumb_path=str(storage / f"thumb-{index}.jpg"),
                box_count=2,
            )
            session.add(image)
            await session.flush()
            session.add_all(
                [
                    Annotation(
                        image_id=image.id,
                        class_id=0,
                        cx=0.25,
                        cy=0.25,
                        w=0.2,
                        h=0.2,
                    ),
                    Annotation(
                        image_id=image.id,
                        class_id=1,
                        cx=0.75,
                        cy=0.75,
                        w=0.2,
                        h=0.2,
                    ),
                ]
            )
        dataset.status = "ready"
        dataset.image_count = 10
        dataset.annotation_count = 20
        dataset.class_count = 2
        await session.commit()
    return dataset_id


async def test_class_rename_changes_name_only_and_rejects_extra_fields(
    client,
    app,
) -> None:
    dataset_id = await _dataset_with_labels(client, app)

    invalid = await client.patch(
        f"/api/datasets/{dataset_id}/classes/0",
        json={"name": "forklift", "class_id": 7},
    )
    renamed = await client.patch(
        f"/api/datasets/{dataset_id}/classes/0",
        json={"name": "forklift"},
    )

    assert invalid.status_code == 422
    assert renamed.status_code == 200
    async with app.state.session_factory() as session:
        catalog_version = await session.scalar(select(Dataset.class_catalog_version).where(
            Dataset.id == dataset_id,
        ))
        classes = (
            await session.scalars(
                select(DatasetClass)
                .where(DatasetClass.dataset_id == dataset_id)
                .order_by(DatasetClass.class_id)
            )
        ).all()
        annotation_ids = set(
            await session.scalars(
                select(Annotation.class_id)
                .join(Image, Image.id == Annotation.image_id)
                .where(Image.dataset_id == dataset_id)
            )
        )
    assert renamed.json() == {
        "class_id": 0, "name": "forklift", "catalog_version": catalog_version,
        "classes": [{"class_id": 0, "name": "forklift"}, {"class_id": 1, "name": "person"}],
    }
    assert [(row.class_id, row.name) for row in classes] == [
        (0, "forklift"),
        (1, "person"),
    ]
    assert annotation_ids == {0, 1}
    duplicate = await client.patch(
        f"/api/datasets/{dataset_id}/classes/0",
        json={"name": "person"},
    )
    assert duplicate.status_code == 200


async def test_run_keeps_submit_time_class_name_snapshot_after_rename(
    client,
    app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _host_ready(monkeypatch)
    dataset_id = await _dataset_with_labels(client, app)

    started = await client.post(
        f"/api/datasets/{dataset_id}/train",
        json={
            "weights": "yolo26n.pt",
            "epochs": 2,
            "imgsz": 640,
            "batch": 2,
            "seed": 123,
        },
    )
    assert started.status_code == 201
    async with app.state.session_factory() as session:
        run = await session.get(TrainingRun, started.json()["run_id"])
        assert run is not None
        data_yaml = (
            contained_storage_path(
                app.state.settings.storage_dir,
                run.out_dir,
            )
            / "workdir"
            / "data.yaml"
        )

    before = yaml.safe_load(data_yaml.read_text(encoding="utf-8"))
    renamed = await client.patch(
        f"/api/datasets/{dataset_id}/classes/0",
        json={"name": "forklift"},
    )
    after = yaml.safe_load(data_yaml.read_text(encoding="utf-8"))
    label_ids = {
        line.split()[0]
        for label in data_yaml.parent.glob("labels/*/*.txt")
        for line in label.read_text(encoding="utf-8").splitlines()
        if line
    }

    assert renamed.status_code == 200
    assert list(before["names"]) == [0, 1]
    assert before["names"] == {0: "orklift", 1: "person"}
    assert after == before
    assert label_ids == {"0", "1"}


async def _rename_case(client, app):
    created = await client.post("/api/projects", json={
        "name": _name("catalog-guard"),
        "classes": [{"name": "cat", "color": "#EF4444"}, {"name": "dog", "color": "#22C55E"}],
    })
    assert created.status_code == 201, created.text
    project_id = created.json()["id"]
    dataset_ids, image_ids = [], []
    for index in range(2):
        response = await client.post("/api/datasets", json={
            "name": _name(f"catalog-child-{index}"), "project_id": project_id,
        })
        assert response.status_code == 201, response.text
        dataset_id = response.json()["id"]
        dataset_ids.append(dataset_id)
        async with app.state.session_factory() as session:
            dataset = await session.get(Dataset, dataset_id)
            root = contained_storage_path(app.state.settings.storage_dir, dataset.storage_path)
            source = root / "source.jpg"
            source.write_bytes(b"class-rename-image")
            image = image_with_media(
                workspace_id=dataset.workspace_id, dataset_id=dataset_id,
                stem="source", filename="source.jpg", rel_path="source.jpg", split=None,
                width=32, height=24, file_path=str(source), thumb_path=str(source), box_count=1,
            )
            session.add(image)
            await session.flush()
            session.add(Annotation(image_id=image.id, class_id=0, cx=.5, cy=.5, w=.2, h=.2))
            dataset.status, dataset.image_count, dataset.annotation_count = "ready", 1, 1
            await session.commit()
            image_ids.append(image.id)
    return {"project_id": project_id, "dataset_ids": dataset_ids, "image_ids": image_ids}


async def _rename_snapshot(app, *cases):
    project_ids = [case["project_id"] for case in cases]
    dataset_ids = [value for case in cases for value in case["dataset_ids"]]
    image_ids = [value for case in cases for value in case["image_ids"]]
    statements = {
        "projects": select(Project.id, Project.name, Project.updated_at)
            .where(Project.id.in_(project_ids)).order_by(Project.id),
        "project_classes": select(ProjectClass.project_id, ProjectClass.class_id, ProjectClass.name, ProjectClass.color)
            .where(ProjectClass.project_id.in_(project_ids)).order_by(ProjectClass.project_id, ProjectClass.class_id),
        "datasets": select(Dataset.id, Dataset.project_id, Dataset.name, Dataset.class_catalog_version)
            .where(Dataset.id.in_(dataset_ids)).order_by(Dataset.id),
        "classes": select(DatasetClass.dataset_id, DatasetClass.class_id, DatasetClass.name)
            .where(DatasetClass.dataset_id.in_(dataset_ids)).order_by(DatasetClass.dataset_id, DatasetClass.class_id),
        "images": select(Image.id, Image.version, Image.box_count, Image.is_modified, Image.label_status, Image.assignee_id)
            .where(Image.id.in_(image_ids)).order_by(Image.id),
        "annotations": select(Annotation.id, Annotation.image_id, Annotation.class_id,
                              Annotation.cx, Annotation.cy, Annotation.w, Annotation.h)
            .where(Annotation.image_id.in_(image_ids)).order_by(Annotation.id),
    }
    async with app.state.session_factory() as session:
        return {key: [tuple(row) for row in (await session.execute(statement)).all()]
                for key, statement in statements.items()}


def _rename_url(case, endpoint):
    if endpoint == "dataset":
        return f"/api/datasets/{case['dataset_ids'][0]}/classes/0"
    return f"/api/projects/{case['project_id']}/classes/0"


@pytest.mark.parametrize("endpoint", ["dataset", "project"])
async def test_class_rename_rejects_duplicate_name_without_writes(client, app, endpoint):
    case = await _rename_case(client, app)
    before = await _rename_snapshot(app, case)
    body = {"name": "dog", "expected_catalog_version": before["datasets"][0][3]}
    if endpoint == "project":
        body["dataset_id"] = case["dataset_ids"][0]

    response = await client.patch(_rename_url(case, endpoint), json=body)

    assert response.status_code == 409, response.text
    assert response.json()["detail"] == "이미 있는 클래스 이름입니다."
    assert await _rename_snapshot(app, case) == before


@pytest.mark.parametrize("endpoint", ["dataset", "project"])
async def test_class_rename_rejects_stale_catalog_after_reorder_without_writes(client, app, endpoint):
    case = await _rename_case(client, app)
    stale_catalog = (await _rename_snapshot(app, case))["datasets"][0][3]
    detail = await client.get(f"/api/projects/{case['project_id']}")
    assert detail.status_code == 200, detail.text
    reordered = await client.put(f"/api/projects/{case['project_id']}/classes", json={
        "expected_updated_at": detail.json()["updated_at"],
        "classes": [{"class_id": 1, "name": "dog", "color": "#22C55E"},
                    {"class_id": 0, "name": "cat", "color": "#EF4444"}],
    })
    assert reordered.status_code == 200, reordered.text
    before = await _rename_snapshot(app, case)
    assert before["datasets"][0][3] > stale_catalog
    body = {"name": "lion", "expected_catalog_version": stale_catalog}
    if endpoint == "project":
        body["dataset_id"] = case["dataset_ids"][0]
    response = await client.patch(_rename_url(case, endpoint), json=body)
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "class_catalog_changed"
    assert await _rename_snapshot(app, case) == before


@pytest.mark.parametrize("endpoint", ["dataset", "project"])
@pytest.mark.parametrize("include_version", [False, True], ids=["legacy", "matching-version"])
async def test_class_rename_accepts_matching_or_omitted_catalog_version(client, app, endpoint, include_version):
    case = await _rename_case(client, app)
    before = await _rename_snapshot(app, case)
    body = {"name": "lion"}
    if include_version:
        body["expected_catalog_version"] = before["datasets"][0][3]
        if endpoint == "project":
            body["dataset_id"] = case["dataset_ids"][0]
    response = await client.patch(_rename_url(case, endpoint), json=body)
    assert response.status_code == 200, response.text
    after = await _rename_snapshot(app, case)
    assert after["images"] == before["images"]
    assert after["annotations"] == before["annotations"]
    assert after["classes"] == [(dataset_id, class_id, "lion" if class_id == 0 else "dog")
                                for dataset_id in case["dataset_ids"] for class_id in (0, 1)]
    assert after["project_classes"] == [
        (case["project_id"], 0, "lion", "#EF4444"), (case["project_id"], 1, "dog", "#22C55E"),
    ]
    for old, new in zip(before["datasets"], after["datasets"], strict=True):
        assert new[:3] == old[:3]
        assert new[3] > old[3]


async def test_project_class_rename_requires_dataset_context_for_catalog_precondition(client, app):
    case = await _rename_case(client, app)
    before = await _rename_snapshot(app, case)
    response = await client.patch(_rename_url(case, "project"), json={
        "name": "lion", "expected_catalog_version": before["datasets"][0][3],
    })
    assert response.status_code == 422, response.text
    assert await _rename_snapshot(app, case) == before


async def test_project_class_rename_rejects_foreign_project_dataset_without_writes(client, app):
    case, foreign = await _rename_case(client, app), await _rename_case(client, app)
    before = await _rename_snapshot(app, case, foreign)
    foreign_catalog = next(row[3] for row in before["datasets"] if row[0] == foreign["dataset_ids"][0])
    response = await client.patch(_rename_url(case, "project"), json={
        "name": "lion", "dataset_id": foreign["dataset_ids"][0], "expected_catalog_version": foreign_catalog,
    })
    assert response.status_code == 404, response.text
    assert await _rename_snapshot(app, case, foreign) == before


@pytest.mark.parametrize("endpoint", ["dataset", "project"])
async def test_class_rename_waits_for_sibling_autosave_then_succeeds(client, app, monkeypatch, endpoint):
    case = await _rename_case(client, app)
    before = await _rename_snapshot(app, case)
    image_id = case["image_ids"][1]
    current = await client.get(f"/api/images/{image_id}/annotations")
    assert current.status_code == 200, current.text
    acquired, release, rename_attempted = asyncio.Event(), asyncio.Event(), asyncio.Event()
    backend_pids = {}
    original_execute, original_scalar = AsyncSession.execute, AsyncSession.scalar
    actor = ContextVar("class_rename_lock_actor", default=None)

    async def execute_at_dataset_lock(session, statement, *args, **kwargs):
        who = actor.get()
        lock = getattr(statement, "_for_update_arg", None)
        write_lock = getattr(statement, "is_select", False) and lock is not None and not lock.read
        sql = str(statement).lower()
        if who == "rename" and write_lock and "from datasets" in sql:
            backend_pids["waiter"] = await original_scalar(session, text("SELECT pg_backend_pid()"))
            rename_attempted.set()
        result = await original_execute(session, statement, *args, **kwargs)
        if who == "autosave" and write_lock and "join images" in sql and not acquired.is_set():
            # Annotation PUT already owns both its real Dataset and Image locks.
            backend_pids["holder"] = await original_scalar(session, text("SELECT pg_backend_pid()"))
            acquired.set()
            await asyncio.wait_for(release.wait(), timeout=15)
        return result

    monkeypatch.setattr(AsyncSession, "execute", execute_at_dataset_lock)

    async def as_actor(name, operation):
        # Middleware may create another task; ContextVar follows that request.
        token = actor.set(name)
        try:
            return await operation()
        finally:
            actor.reset(token)

    async def autosave():
        return await client.put(f"/api/images/{image_id}/annotations", json={
            "expected_version": current.json()["version"],
            "expected_catalog_version": current.json()["catalog_version"],
            "boxes": [{"class_id": 0, "cx": .7, "cy": .4, "w": .1, "h": .2}],
        })

    async def rename():
        return await client.patch(_rename_url(case, endpoint), json={"name": "lion"})

    requests = [asyncio.create_task(as_actor("autosave", autosave))]
    waited = False
    try:
        await asyncio.wait_for(acquired.wait(), timeout=5)
        requests.append(asyncio.create_task(as_actor("rename", rename)))
        await asyncio.wait_for(rename_attempted.wait(), timeout=5)

        async def observe_database_wait():
            async with app.state.session_factory() as observer:
                while not requests[1].done():
                    blocked = await observer.scalar(text(
                        "SELECT CAST(:holder AS INTEGER) = ANY(pg_blocking_pids(CAST(:waiter AS INTEGER)))"
                    ), backend_pids)
                    if blocked:
                        return True
                return False  # A NOWAIT 409 completes instead of waiting for the autosave.

        waited = await asyncio.wait_for(observe_database_wait(), timeout=5)
    finally:
        release.set()
        results = await asyncio.wait_for(asyncio.gather(*requests, return_exceptions=True), timeout=10)
    assert len(results) == 2
    for result in results:
        assert not isinstance(result, BaseException), result
        assert result.status_code == 200, result.text
    assert waited, "the rename must wait for the actual sibling Dataset lock"
    after = await _rename_snapshot(app, case)
    assert after["images"][0] == before["images"][0]
    assert after["images"][1][1] == before["images"][1][1] + 1
    assert after["classes"] == [(dataset_id, class_id, "lion" if class_id == 0 else "dog")
                                for dataset_id in case["dataset_ids"] for class_id in (0, 1)]
    assert [row for row in after["annotations"] if row[1] == case["image_ids"][0]] == [
        row for row in before["annotations"] if row[1] == case["image_ids"][0]
    ]
    sibling_boxes = [row[2:] for row in after["annotations"] if row[1] == image_id]
    assert len(sibling_boxes) == 1 and sibling_boxes[0][0] == 0
    assert sibling_boxes[0][1:] == pytest.approx((.7, .4, .1, .2))
    assert all(new[3] > old[3] for old, new in zip(before["datasets"], after["datasets"], strict=True))


@pytest.mark.parametrize("endpoint", ["dataset", "project"])
async def test_class_rename_times_out_behind_real_merge_without_blocking_sibling_autosave(
    client, app, monkeypatch, endpoint,
):
    from tests.test_annotation_version import _finish_tasks

    case = await _rename_case(client, app)
    before = await _rename_snapshot(app, case)
    image_id = case["image_ids"][1]
    current = await client.get(f"/api/images/{image_id}/annotations")
    assert current.status_code == 200, current.text
    async with app.state.session_factory() as session:
        default_timeout = await session.scalar(text("SHOW lock_timeout"))
    actor = ContextVar("rename_timeout_actor", default=None)
    acquired, attempted, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original_scalar, original_execute = AsyncSession.scalar, AsyncSession.execute
    pids, observed = {}, {}

    async def at_project_lock(original, session, statement, args, kwargs):
        who = actor.get()
        lock = getattr(statement, "_for_update_arg", None)
        project_lock = (getattr(statement, "is_select", False) and lock is not None and not lock.read
                        and "from projects" in str(statement).lower())
        if who == "rename" and project_lock:
            pids["waiter"] = await original_scalar(session, text("SELECT pg_backend_pid()"))
            observed["rename_timeout"] = await original_scalar(session, text("SHOW lock_timeout"))
            observed["started"] = asyncio.get_running_loop().time()
            attempted.set()
        result = await original(session, statement, *args, **kwargs)
        if who == "merge" and project_lock and not acquired.is_set():
            pids["holder"] = await original_scalar(session, text("SELECT pg_backend_pid()"))
            observed["merge_timeout"] = await original_scalar(session, text("SHOW lock_timeout"))
            acquired.set()  # Real merge owns Project, before it takes any Dataset lock.
            await asyncio.wait_for(release.wait(), timeout=15)
        return result

    async def scalar(session, statement, *args, **kwargs):
        return await at_project_lock(original_scalar, session, statement, args, kwargs)

    async def execute(session, statement, *args, **kwargs):
        return await at_project_lock(original_execute, session, statement, args, kwargs)

    monkeypatch.setattr(AsyncSession, "scalar", scalar)
    monkeypatch.setattr(AsyncSession, "execute", execute)

    async def as_actor(name, operation):
        token = actor.set(name)
        try:
            return await operation()
        finally:
            actor.reset(token)

    async def merge():
        return await client.post("/api/datasets/merge", json={
            "name": _name("rename-timeout-merge"), "dataset_ids": case["dataset_ids"],
        })

    async def rename():
        return await client.patch(_rename_url(case, endpoint), json={"name": "lion"})

    tasks = [asyncio.create_task(as_actor("merge", merge))]
    try:
        await asyncio.wait_for(acquired.wait(), timeout=5)
        tasks.append(asyncio.create_task(as_actor("rename", rename)))
        await asyncio.wait_for(attempted.wait(), timeout=5)

        async def observe_wait():
            async with app.state.session_factory() as observer:
                while not tasks[1].done():
                    if await original_scalar(observer, text(
                        "SELECT CAST(:holder AS INTEGER) = ANY(pg_blocking_pids(CAST(:waiter AS INTEGER)))"
                    ), pids):
                        return True
                return False

        assert await asyncio.wait_for(observe_wait(), timeout=2)
        saved = await asyncio.wait_for(client.put(f"/api/images/{image_id}/annotations", json={
            "expected_version": current.json()["version"],
            "expected_catalog_version": current.json()["catalog_version"],
            "boxes": [{"class_id": 0, "cx": .7, "cy": .4, "w": .1, "h": .2}],
        }), timeout=1.5)
        assert saved.status_code == 200, saved.text
        assert not tasks[0].done(), "merge must still hold Project during sibling autosave"
        response = await asyncio.wait_for(tasks[1], timeout=5)
        elapsed = asyncio.get_running_loop().time() - observed["started"]
        assert response.status_code == 409, response.text
        assert response.json()["detail"] == "다른 데이터 작업이 진행 중이에요. 잠시 후 다시 시도해 주세요."
        setting = observed["rename_timeout"]
        seconds = float(setting[:-2]) / 1000 if setting.endswith("ms") else float(setting.removesuffix("s"))
        assert 2 <= seconds <= 3
        assert seconds - .15 <= elapsed < seconds + 2
        assert observed["merge_timeout"] == default_timeout
        async with app.state.session_factory() as observer:
            assert await original_scalar(observer, text("SHOW lock_timeout")) == default_timeout
        after = await _rename_snapshot(app, case)
        for key in ("projects", "project_classes", "datasets", "classes"):
            assert after[key] == before[key]
        assert after["images"][1][1] == before["images"][1][1] + 1
        release.set()
        merged = await asyncio.wait_for(tasks[0], timeout=5)
        assert merged.status_code == 201, merged.text
    finally:
        release.set()
        await _finish_tasks(tasks)
