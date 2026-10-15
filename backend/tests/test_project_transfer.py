"""Project ownership transfer contracts through the real authenticated API."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Annotation, Dataset, DatasetClass, ExportArtifact, Image, MediaObject, Project,
    ProjectClass, TrainingRun, UploadBatch, UploadJob, UploadSession, Workspace,
    WorkspaceMember, WorkspaceStorage,
)
from tests.factories import image_with_media


pytestmark = pytest.mark.asyncio


def _headers(auth_headers, graph, *, actor=None, workspace_id=None):
    return {
        **auth_headers(graph["actor"] if actor is None else actor),
        "X-Workspace-Id": str(graph["source"] if workspace_id is None else workspace_id),
    }


def _run(workspace_id, dataset, actor, artifact_bytes):
    return TrainingRun(
        workspace_id=workspace_id, created_by=actor, dataset_id=dataset.id,
        dataset_name=dataset.name, weights="yolo26n.pt", epochs=1, imgsz=64,
        batch=1, split_mode="2way", ratios={"train": .8, "valid": .2},
        seed=0, state="done", out_dir=f"transfer/{dataset.id}/run",
        artifact_bytes=artifact_bytes,
    )


@pytest_asyncio.fixture
async def transfer_graph(app):
    """Keep all seeded IDs in two private workspaces, including teardown."""
    workspace_ids = []

    async def seed(*, source_kind="team", target_kind="team", target_role="owner"):
        token = uuid4().hex
        actor = 1_000_000_000 + int(token[:7], 16)
        graph = {"actor": actor, "files": {}, "roles": {}, "token": token}
        root = Path(app.state.settings.storage_dir)

        def stored(name, size):
            relative = f"transfer-{token}/{name}"
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = bytes([len(graph["files"]) + 1]) * size
            path.write_bytes(payload)
            graph["files"][relative] = payload
            return relative

        async with app.state.session_factory() as session:
            source = Workspace(kind=source_kind, name=f"test-transfer-source-{token}", created_by=actor)
            target_owner = actor if target_role == "owner" else actor + 1
            target = Workspace(kind=target_kind, name=f"test-transfer-target-{token}", created_by=target_owner)
            session.add_all([source, target])
            await session.flush()
            workspace_ids.extend([source.id, target.id])
            graph.update(source=source.id, target=target.id)
            session.add(WorkspaceMember(workspace_id=source.id, user_id=actor, role="owner"))
            session.add(WorkspaceMember(workspace_id=target.id, user_id=target_owner, role="owner"))
            if target_role != "owner":
                session.add(WorkspaceMember(workspace_id=target.id, user_id=actor, role=target_role))
            for offset, role in enumerate(("admin", "editor", "viewer"), start=2):
                user_id = actor + offset
                graph["roles"][role] = user_id
                session.add_all([
                    WorkspaceMember(workspace_id=source.id, user_id=user_id, role=role),
                    WorkspaceMember(workspace_id=target.id, user_id=user_id, role="admin"),
                ])
            projects = [Project(workspace_id=ws, name=f"test-transfer-{suffix}-{token}")
                        for ws, suffix in ((source.id, "move"), (source.id, "stay"), (target.id, "target"))]
            session.add_all(projects)
            await session.flush()
            graph.update(project=projects[0].id, project_name=projects[0].name,
                         external_project=projects[1].id, reverse_project=projects[2].id)
            datasets = [Dataset(workspace_id=project.workspace_id, project_id=project.id,
                                name=f"test-transfer-data-{index}-{token}", status="ready",
                                storage_path=f"transfer-{token}/dataset-{index}", image_count=1,
                                class_count=1, class_catalog_version=7)
                        for index, project in enumerate([projects[0], projects[0], projects[1], projects[2]])]
            session.add_all(datasets)
            await session.flush()
            graph.update(datasets=[row.id for row in datasets[:2]],
                         dataset_names=[row.name for row in datasets[:2]],
                         external_dataset=datasets[2].id, reverse_dataset=datasets[3].id)
            session.add(ProjectClass(project_id=projects[0].id, class_id=0, name="person", color="#123456"))
            session.add_all(DatasetClass(dataset_id=row.id, class_id=0, name="person") for row in datasets)
            images = []
            for index, (dataset, sizes) in enumerate(zip(datasets, [(13, 7, 3), (11, 5, 2), (100, 200, 100), (200, 300, 200)])):
                original, display, thumb = sizes
                image = image_with_media(
                    workspace_id=dataset.workspace_id, dataset_id=dataset.id,
                    stem=f"image-{index}", filename=f"image-{index}.jpg", rel_path=f"image-{index}.jpg",
                    width=64, height=48, file_path=stored(f"image-{index}.jpg", original),
                    display_path=stored(f"display-{index}.jpg", display),
                    thumb_path=stored(f"thumb-{index}.jpg", thumb),
                    original_bytes=original, display_bytes=display, thumb_bytes=thumb,
                    label_status="labeled", version=9, box_count=1,
                )
                session.add(image)
                images.append(image)
            await session.flush()
            # Two logical image rows share one physical media object inside
            # the moved project; summing Image bytes would overcharge by 23 B.
            duplicate = image_with_media(
                workspace_id=source.id, dataset_id=datasets[1].id,
                media_object_id=images[0].media_object_id,
                stem="shared-inside", filename="shared-inside.jpg", rel_path="shared-inside.jpg",
                width=64, height=48, file_path=images[0].file_path,
                display_path=images[0].display_path, thumb_path=images[0].thumb_path,
                original_bytes=13, display_bytes=7, thumb_bytes=3, label_status="labeled", version=12,
            )
            session.add(duplicate)
            await session.flush()
            datasets[1].image_count = 2
            graph.update(images=[images[0].id, images[1].id, duplicate.id],
                         media=[images[0].media_object_id, images[1].media_object_id])
            session.add_all(Annotation(image_id=row.id, class_id=0, cx=.5, cy=.5, w=.2, h=.3,
                                       serialized_bytes=23) for row in images[:2])
            runs = [_run(source.id, row, actor, size) for row, size in zip(datasets[:2], [29, 31])]
            session.add_all(runs)
            jobs = [UploadJob(dataset_id=row.id, kind="export", state="done", phase="done") for row in datasets[:2]]
            session.add_all(jobs)
            await session.flush()
            for index, (run, size) in enumerate(zip(runs, [29, 31])):
                run.out_dir = f"transfer-{token}/run-{index}"
                stored(f"run-{index}/weights/best.pt", size)
            session.add_all(ExportArtifact(
                job_id=job.id, dataset_id=dataset.id,
                archive_path=stored(f"export-{index}.zip", size), archive_bytes=size,
            ) for index, (job, dataset, size) in enumerate(zip(jobs, datasets[:2], [17, 19])))
            graph.update(runs=[row.id for row in runs], jobs=[row.id for row in jobs], moved_bytes=137)
            session.add_all([
                WorkspaceStorage(workspace_id=source.id, bytes_used=537),
                WorkspaceStorage(workspace_id=target.id, bytes_used=700),
            ])
            await session.commit()
        return graph

    yield seed
    async with app.state.session_factory() as session:
        dataset_ids = select(Dataset.id).where(Dataset.workspace_id.in_(workspace_ids))
        media_ids = select(MediaObject.id).where(MediaObject.workspace_id.in_(workspace_ids))
        await session.execute(delete(TrainingRun).where(TrainingRun.workspace_id.in_(workspace_ids)))
        await session.execute(delete(Dataset).where(Dataset.id.in_(dataset_ids)))
        await session.execute(delete(Project).where(Project.workspace_id.in_(workspace_ids)))
        await session.execute(delete(MediaObject).where(MediaObject.id.in_(media_ids)))
        await session.execute(delete(Workspace).where(Workspace.id.in_(workspace_ids)))
        await session.commit()


async def _snapshot(app, graph):
    scopes = [graph["source"], graph["target"]]
    async with app.state.session_factory() as session:
        datasets = select(Dataset.id).where(Dataset.workspace_id.in_(scopes))
        projects = select(Project.id).where(Project.workspace_id.in_(scopes))
        images = select(Image.id).where(Image.dataset_id.in_(datasets))
        selectors = {
            Project: Project.workspace_id.in_(scopes), Dataset: Dataset.workspace_id.in_(scopes),
            MediaObject: MediaObject.workspace_id.in_(scopes), TrainingRun: TrainingRun.workspace_id.in_(scopes),
            WorkspaceStorage: WorkspaceStorage.workspace_id.in_(scopes),
            ProjectClass: ProjectClass.project_id.in_(projects), DatasetClass: DatasetClass.dataset_id.in_(datasets),
            Image: Image.dataset_id.in_(datasets), Annotation: Annotation.image_id.in_(images),
            ExportArtifact: ExportArtifact.dataset_id.in_(datasets),
            UploadBatch: UploadBatch.dataset_id.in_(datasets),
            UploadJob: UploadJob.dataset_id.in_(datasets), UploadSession: UploadSession.dataset_id.in_(datasets),
        }
        result = {}
        for model, predicate in selectors.items():
            rows = (await session.execute(select(model.__table__).where(predicate)
                                          .order_by(*model.__table__.primary_key.columns))).mappings().all()
            result[model.__tablename__] = [dict(row) for row in rows]
        return result


def _files_unchanged(app, graph):
    for relative, expected in graph["files"].items():
        assert (Path(app.state.settings.storage_dir) / relative).read_bytes() == expected


async def _post(client, auth_headers, graph, **kwargs):
    return await client.post(
        f"/api/projects/{graph['project']}/transfer",
        headers=_headers(auth_headers, graph, **kwargs),
        json={"target_workspace_id": graph["target"]},
    )


async def _reject(client, app, auth_headers, graph, status, code, **kwargs):
    before = await _snapshot(app, graph)
    response = await _post(client, auth_headers, graph, **kwargs)
    assert response.status_code == status, response.text
    assert response.json()["detail"]["code"] == code, response.text
    assert await _snapshot(app, graph) == before
    _files_unchanged(app, graph)
    return response.json()["detail"]


@pytest.mark.parametrize("source_kind,target_kind,target_role", [
    ("personal", "team", "admin"), ("personal", "team", "owner"), ("team", "personal", "owner"),
])
async def test_transfer_moves_distinct_media_exports_runs_and_preserves_resource_identity(
    client, app, auth_headers, transfer_graph, source_kind, target_kind, target_role,
):
    graph = await transfer_graph(source_kind=source_kind, target_kind=target_kind, target_role=target_role)
    before = await _snapshot(app, graph)
    response = await _post(client, auth_headers, graph)
    assert response.status_code == 200, response.text
    assert response.json() == {"project_id": graph["project"], "workspace_id": graph["target"], "moved_bytes": 137}
    after = await _snapshot(app, graph)
    for table in ("images", "annotations", "exports", "upload_jobs", "project_classes", "dataset_classes"):
        assert after[table] == before[table], table
    for table, identifiers in (("projects", [graph["project"]]), ("datasets", graph["datasets"]),
                               ("media_objects", graph["media"]), ("training_runs", graph["runs"])):
        for previous, current in zip(before[table], after[table], strict=True):
            expected_scope = graph["target"] if current["id"] in identifiers else previous["workspace_id"]
            assert current["workspace_id"] == expected_scope
            assert {key: value for key, value in current.items() if key not in {"workspace_id", "updated_at"}} == {
                key: value for key, value in previous.items() if key not in {"workspace_id", "updated_at"}
            }
    usage = {row["workspace_id"]: row["bytes_used"] for row in after["workspace_storage"]}
    assert usage == {graph["source"]: 400, graph["target"]: 837}
    assert sum(usage.values()) == sum(row["bytes_used"] for row in before["workspace_storage"])
    recalculated = sum(row["original_bytes"] + row["display_bytes"] + row["thumb_bytes"]
                       for row in after["media_objects"] if row["id"] in graph["media"])
    recalculated += sum(row["archive_bytes"] for row in after["exports"] if row["dataset_id"] in graph["datasets"])
    recalculated += sum(row["artifact_bytes"] or 0 for row in after["training_runs"] if row["id"] in graph["runs"])
    assert response.json()["moved_bytes"] == recalculated
    for path in [f"/api/projects/{graph['project']}", f"/api/datasets/{graph['datasets'][0]}",
                 f"/api/images/{graph['images'][0]}", f"/api/images/{graph['images'][0]}/annotations",
                 f"/api/images/{graph['images'][0]}/file", f"/api/runs/{graph['runs'][0]}"]:
        old = await client.get(path, headers=_headers(auth_headers, graph))
        new = await client.get(path, headers=_headers(auth_headers, graph, workspace_id=graph["target"]))
        assert old.status_code == 404, (path, old.text)
        assert new.status_code == 200, (path, new.text)
    _files_unchanged(app, graph)


@pytest.mark.parametrize("role", ["admin", "editor", "viewer"])
async def test_transfer_requires_source_owner_without_changes(client, app, auth_headers, transfer_graph, role):
    graph = await transfer_graph()
    detail = await _reject(client, app, auth_headers, graph, 403, "insufficient_role", actor=graph["roles"][role])
    assert detail == {"code": "insufficient_role", "required": "owner", "actual": role}


@pytest.mark.parametrize("role", ["editor", "viewer"])
async def test_transfer_requires_target_admin_without_changes(client, app, auth_headers, transfer_graph, role):
    graph = await transfer_graph(target_role=role)
    detail = await _reject(client, app, auth_headers, graph, 403, "insufficient_role")
    assert detail == {"code": "insufficient_role", "required": "admin", "actual": role}


@pytest.mark.parametrize("case", ["source_nonmember", "target_nonmember", "header_mismatch", "project_missing", "target_missing"])
async def test_transfer_scope_mismatch_and_nonmembers_are_404_without_changes(
    client, app, auth_headers, transfer_graph, case,
):
    graph = await transfer_graph()
    actor, header = graph["actor"], graph["source"]
    project_id, target_id = graph["project"], graph["target"]
    if case == "source_nonmember":
        actor += 100
    elif case == "target_nonmember":
        async with app.state.session_factory() as session:
            await session.execute(delete(WorkspaceMember).where(
                WorkspaceMember.workspace_id == graph["target"], WorkspaceMember.user_id == actor))
            await session.commit()
    elif case == "header_mismatch":
        header = graph["target"]
    elif case == "project_missing":
        project_id = 2**31 - 1
    else:
        target_id = 2**31 - 1
    before = await _snapshot(app, graph)
    response = await client.post(f"/api/projects/{project_id}/transfer",
                                 headers=_headers(auth_headers, graph, actor=actor, workspace_id=header),
                                 json={"target_workspace_id": target_id})
    assert response.status_code == 404, response.text
    assert await _snapshot(app, graph) == before


async def test_transfer_same_workspace_is_409_without_changes(client, app, auth_headers, transfer_graph):
    graph = await transfer_graph()
    before = await _snapshot(app, graph)
    response = await client.post(f"/api/projects/{graph['project']}/transfer", headers=_headers(auth_headers, graph),
                                 json={"target_workspace_id": graph["source"]})
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "transfer_same_workspace"
    assert await _snapshot(app, graph) == before


@pytest.mark.parametrize("kind,state", [("session", "open"), ("session", "pending"),
                                         ("job", "queued"), ("job", "running"), ("job", "awaiting_class_resolution")])
async def test_transfer_active_upload_rejects_without_changes(client, app, auth_headers, transfer_graph, kind, state):
    graph = await transfer_graph()
    async with app.state.session_factory() as session:
        if kind == "session":
            session.add(UploadSession(dataset_id=graph["datasets"][0], filename="busy.zip", size=5,
                                      chunk_size=5, kind="archive", state=state))
        else:
            session.add(UploadJob(dataset_id=graph["datasets"][0], kind="ingest", state=state, phase=state))
        await session.commit()
    detail = await _reject(client, app, auth_headers, graph, 409, "transfer_upload_active")
    assert detail["names"] == [graph["dataset_names"][0]]


@pytest.mark.parametrize("state", ["running", "canceling"])
async def test_transfer_active_run_rejects_without_changes(client, app, auth_headers, transfer_graph, state):
    graph = await transfer_graph()
    async with app.state.session_factory() as session:
        run = await session.get(TrainingRun, graph["runs"][0])
        run.state = state
        await session.commit()
    detail = await _reject(client, app, auth_headers, graph, 409, "transfer_run_active")
    assert detail["names"] == [graph["dataset_names"][0]]


async def test_transfer_shared_media_reports_external_dataset_and_does_not_copy(
    client, app, auth_headers, transfer_graph,
):
    graph = await transfer_graph()
    async with app.state.session_factory() as session:
        original = await session.get(Image, graph["images"][0])
        dataset = await session.get(Dataset, graph["external_dataset"])
        session.add(image_with_media(
            workspace_id=graph["source"], dataset_id=dataset.id, media_object_id=original.media_object_id,
            stem="external-shared", filename="external-shared.jpg", rel_path="external-shared.jpg",
            width=64, height=48, file_path=original.file_path, thumb_path=original.thumb_path,
        ))
        await session.commit()
        dataset_name = dataset.name
    detail = await _reject(client, app, auth_headers, graph, 409, "transfer_shared_media")
    assert detail["datasets"] == [{"id": graph["external_dataset"], "name": dataset_name}]


@pytest.mark.parametrize("kind", ["project", "dataset"])
async def test_transfer_target_name_collision_rejects_without_changes(client, app, auth_headers, transfer_graph, kind):
    graph = await transfer_graph()
    async with app.state.session_factory() as session:
        if kind == "project":
            row = await session.get(Project, graph["reverse_project"])
            name = graph["project_name"]
        else:
            row = await session.get(Dataset, graph["reverse_dataset"])
            name = graph["dataset_names"][0]
        row.name = name
        await session.commit()
    detail = await _reject(client, app, auth_headers, graph, 409, "transfer_name_conflict")
    assert detail["names"] == [name]


@pytest.mark.parametrize("override", [False, True])
async def test_transfer_quota_shortage_preserves_every_row(client, app, auth_headers, transfer_graph, override):
    graph = await transfer_graph()
    if override:
        async with app.state.session_factory() as session:
            storage = await session.get(WorkspaceStorage, graph["target"])
            storage.quota_limit_bytes = 836
            await session.commit()
    else:
        app.state.settings = app.state.settings.model_copy(update={"quota_bytes_per_user": 836})
    detail = await _reject(client, app, auth_headers, graph, 413, "transfer_quota_exceeded")
    assert detail["required_bytes"] == 137
    assert detail["remaining_bytes"] == 136


async def test_transfer_terminal_uploads_and_exact_quota_boundary_are_allowed(client, app, auth_headers, transfer_graph):
    graph = await transfer_graph()
    async with app.state.session_factory() as session:
        storage = await session.get(WorkspaceStorage, graph["target"])
        storage.quota_limit_bytes = 837
        session.add_all(UploadSession(dataset_id=graph["datasets"][0], filename=f"{state}.zip", size=5,
                                      chunk_size=5, kind="archive", state=state) for state in ["complete", "aborted"])
        session.add_all(UploadJob(dataset_id=graph["datasets"][0], kind="ingest", state=state, phase=state)
                        for state in ["done", "failed"])
        await session.commit()
    response = await _post(client, auth_headers, graph)
    assert response.status_code == 200, response.text
    assert response.json()["moved_bytes"] == 137
    _files_unchanged(app, graph)


async def test_opposite_transfers_wait_after_actual_workspace_locks_without_deadlock(
    client, app, auth_headers, transfer_graph, monkeypatch,
):
    graph = await transfer_graph()
    first_locked, second_attempting, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original_execute, original_scalar = AsyncSession.execute, AsyncSession.scalar
    first_session = None
    backend_pids = {}
    guarded = set()

    async def around_lock(original, session, statement, args, kwargs):
        nonlocal first_session
        sql = str(statement.compile(dialect=postgresql.dialect())).upper()
        is_workspace_lock = "FROM WORKSPACES" in sql and ("FOR UPDATE" in sql or "FOR NO KEY UPDATE" in sql)
        if not is_workspace_lock or id(session) in guarded:
            return await original(session, statement, *args, **kwargs)
        guarded.add(id(session))
        try:
            pid = (await original_execute(session, text("SELECT pg_backend_pid()"))).scalar_one()
            backend_pids[id(session)] = pid
            if first_session is not None and id(session) != first_session:
                second_attempting.set()
            result = await original(session, statement, *args, **kwargs)
            if first_session is None:
                first_session = id(session)
                first_locked.set()
                await asyncio.wait_for(release.wait(), timeout=15)
            return result
        finally:
            guarded.discard(id(session))

    async def execute(session, statement, *args, **kwargs):
        return await around_lock(original_execute, session, statement, args, kwargs)

    async def scalar(session, statement, *args, **kwargs):
        return await around_lock(original_scalar, session, statement, args, kwargs)

    monkeypatch.setattr(AsyncSession, "execute", execute)
    monkeypatch.setattr(AsyncSession, "scalar", scalar)
    first = asyncio.create_task(_post(client, auth_headers, graph))
    second = None
    try:
        await asyncio.wait_for(first_locked.wait(), timeout=5)
        second = asyncio.create_task(client.post(
            f"/api/projects/{graph['reverse_project']}/transfer",
            headers=_headers(auth_headers, graph, workspace_id=graph["target"]),
            json={"target_workspace_id": graph["source"]},
        ))
        await asyncio.wait_for(second_attempting.wait(), timeout=5)
        second_pid = next(pid for key, pid in backend_pids.items() if key != first_session)
        async with app.state.session_factory() as observer:
            for _ in range(100):
                blockers = await observer.scalar(select(func.pg_blocking_pids(second_pid)))
                if backend_pids[first_session] in blockers:
                    break
                await asyncio.sleep(.01)
            else:
                pytest.fail("the opposite transfer did not wait on the actual acquired workspace row lock")
        assert not first.done() and not second.done()
    finally:
        release.set()
        results = await asyncio.wait_for(asyncio.gather(*([first, second] if second else [first]),
                                                        return_exceptions=True), timeout=15)
    assert len(results) == 2
    assert all(not isinstance(result, BaseException) for result in results), results
    assert [result.status_code for result in results] == [200, 200], [result.text for result in results]
    assert [result.json()["moved_bytes"] for result in results] == [137, 700]
    after = await _snapshot(app, graph)
    assert {row["workspace_id"]: row["bytes_used"] for row in after["workspace_storage"]} == {
        graph["source"]: 1100, graph["target"]: 137,
    }
    assert sum(row["bytes_used"] for row in after["workspace_storage"]) == 1237
    ownership = {row["id"]: row["workspace_id"] for row in after["projects"]}
    assert ownership[graph["project"]] == graph["target"]
    assert ownership[graph["reverse_project"]] == graph["source"]
    _files_unchanged(app, graph)


async def test_transfer_open_manifest_without_sessions_rejects_without_changes(
    client, app, auth_headers, transfer_graph,
):
    graph = await transfer_graph()
    async with app.state.session_factory() as session:
        session.add(UploadBatch(
            id=uuid4(), dataset_id=graph["datasets"][0], expected_file_count=2,
            expected_total_size=8, expected_extracted_size=8, largest_file_size=4,
            state="open",
        ))
        await session.commit()
        assert await session.scalar(select(func.count()).select_from(UploadSession).where(
            UploadSession.dataset_id.in_(graph["datasets"]))) == 0
    detail = await _reject(client, app, auth_headers, graph, 409, "transfer_upload_active")
    assert detail["names"] == [graph["dataset_names"][0]]


@pytest.mark.parametrize("batched", [False, True], ids=["single", "legacy-batch"])
async def test_upload_creation_holds_dataset_lock_until_publish_before_transfer_can_check_uploads(
    client, app, auth_headers, transfer_graph, monkeypatch, batched,
):
    from app.routers import uploads as upload_routes

    graph = await transfer_graph()
    before = await _snapshot(app, graph)
    checked, release = asyncio.Event(), asyncio.Event()
    original = upload_routes._dataset_or_404
    upload_pid = None

    async def pause_after_actual_dataset_lookup(session, dataset_id, workspace_id, **kwargs):
        nonlocal upload_pid
        dataset = await original(session, dataset_id, workspace_id, **kwargs)
        if dataset_id == graph["datasets"][0] and upload_pid is None:
            upload_pid = await session.scalar(select(func.pg_backend_pid()))
            checked.set()
            await asyncio.wait_for(release.wait(), timeout=15)
        return dataset

    monkeypatch.setattr(upload_routes, "_dataset_or_404", pause_after_actual_dataset_lookup)
    file = {"filename": "race.jpg", "size": 4, "chunk_size": 4, "kind": "file"}
    suffix = "/batch" if batched else ""
    upload = asyncio.create_task(client.post(
        f"/api/datasets/{graph['datasets'][0]}/uploads{suffix}",
        headers=_headers(auth_headers, graph), json={"files": [file]} if batched else file,
    ))
    transfer = None
    try:
        await asyncio.wait_for(checked.wait(), timeout=5)
        transfer = asyncio.create_task(_post(client, auth_headers, graph))
        async with app.state.session_factory() as observer:
            for _ in range(100):
                # This proves a real acquired lock protects the checked scope;
                # a barrier before SQL or a mere for_update argument is insufficient.
                waiting = (await observer.execute(text(
                    "SELECT pid FROM pg_stat_activity "
                    "WHERE datname = current_database() "
                    "AND :upload_pid = ANY(pg_blocking_pids(pid))"
                ), {"upload_pid": upload_pid})).scalars().all()
                if waiting:
                    break
                if transfer.done():
                    pytest.fail("transfer passed an upload's checked dataset before its session was published")
                await asyncio.sleep(.01)
            else:
                pytest.fail("transfer did not wait on the upload's acquired dataset row lock")
        assert not upload.done() and not transfer.done()
    finally:
        release.set()
        results = await asyncio.wait_for(asyncio.gather(*([upload, transfer] if transfer else [upload]),
                                                        return_exceptions=True), timeout=15)
    assert len(results) == 2
    assert all(not isinstance(result, BaseException) for result in results), results
    created, refused = results
    assert created.status_code == 201, created.text
    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"]["code"] == "transfer_upload_active"
    assert refused.json()["detail"]["names"] == [graph["dataset_names"][0]]
    after = await _snapshot(app, graph)
    for table in before.keys() - {"upload_sessions"}:
        assert after[table] == before[table], table
    assert len(after["upload_sessions"]) == 1
    session_row = after["upload_sessions"][0]
    assert session_row["dataset_id"] == graph["datasets"][0] and session_row["state"] == "open"
    for workspace_id, status in [(graph["source"], 200), (graph["target"], 404)]:
        read = await client.get(f"/api/uploads/{session_row['id']}",
                                headers=_headers(auth_headers, graph, workspace_id=workspace_id))
        assert read.status_code == status, read.text
    _files_unchanged(app, graph)


@pytest.mark.parametrize("kind", ["project", "dataset"])
async def test_transfer_concurrent_destination_name_change_rolls_back_after_actual_storage_lock(
    client, app, auth_headers, transfer_graph, monkeypatch, kind,
):
    from app.services import project_transfer

    graph = await transfer_graph()
    before = await _snapshot(app, graph)
    locked, release = asyncio.Event(), asyncio.Event()
    original = project_transfer._locked_storage
    acquired = []

    async def pause_after_storage_lock(session, workspace_id):
        storage = await original(session, workspace_id)
        acquired.append(workspace_id)
        # Both real FOR UPDATE calls have completed; the service's name
        # preflight is now behind us, but ownership updates have not begun.
        if len(acquired) == 2:
            locked.set()
            await asyncio.wait_for(release.wait(), timeout=15)
        return storage

    monkeypatch.setattr(project_transfer, "_locked_storage", pause_after_storage_lock)
    transfer = asyncio.create_task(_post(client, auth_headers, graph))
    after_insert = None
    try:
        await asyncio.wait_for(locked.wait(), timeout=5)
        assert acquired == sorted([graph["source"], graph["target"]])
        async with app.state.session_factory() as contender:
            if kind == "project":
                name = graph["project_name"]
                row = await contender.get(Project, graph["reverse_project"])
            else:
                name = graph["dataset_names"][0]
                row = await contender.get(Dataset, graph["reverse_dataset"])
            # An existing row can be renamed without inserting a workspace FK.
            # Inserts correctly wait on the transfer's new Workspace lock.
            row.name = name
            await contender.commit()
            renamed_id = row.id
        assert not transfer.done()
        after_insert = await _snapshot(app, graph)
        table_name = "projects" if kind == "project" else "datasets"
        for table, previous in before.items():
            current = after_insert[table]
            if table == table_name:
                assert len(current) == len(previous)
                renamed = next(item for item in current if item["id"] == renamed_id)
                assert renamed["name"] == name
                current = [item for item in current if item["id"] != renamed_id]
                previous = [item for item in previous if item["id"] != renamed_id]
            assert current == previous, table
    finally:
        release.set()
        results = await asyncio.wait_for(asyncio.gather(transfer, return_exceptions=True), timeout=15)
    assert not isinstance(results[0], BaseException), results[0]
    response = results[0]
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "transfer_name_conflict"
    assert response.json()["detail"]["names"] == [name]
    # The intentional destination rename survives; even statements flushed
    # before the unique-constraint failure must have been rolled back.
    assert await _snapshot(app, graph) == after_insert
    _files_unchanged(app, graph)


async def _wait_for_lock_waiter(app, holder_pid, waiter):
    """Require PostgreSQL blocking, rather than a pause before executing SQL."""
    async with app.state.session_factory() as observer:
        for _ in range(100):
            blocked = (await observer.execute(text(
                "SELECT pid FROM pg_stat_activity "
                "WHERE datname = current_database() "
                "AND :holder_pid = ANY(pg_blocking_pids(pid))"
            ), {"holder_pid": holder_pid})).scalars().all()
            if blocked:
                return
            if waiter.done():
                pytest.fail("the competing request completed without waiting for the acquired row lock")
            await asyncio.sleep(.01)
    pytest.fail("the competing request never waited on the acquired row lock")


@pytest.mark.parametrize("first_operation", ["create", "transfer"])
async def test_dataset_name_lookup_and_transfer_keep_project_workspace_together(
    client, app, auth_headers, transfer_graph, monkeypatch, first_operation,
):
    graph = await transfer_graph()
    acquired, release = asyncio.Event(), asyncio.Event()
    original_scalar = AsyncSession.scalar
    holder_pid = None

    async def scalar_with_project_pause(session, statement, *args, **kwargs):
        nonlocal holder_pid
        sql = str(statement.compile(dialect=postgresql.dialect())).upper()
        project_lookup = "FROM PROJECTS" in sql and "PROJECTS.NAME =" in sql
        transfer_lock = "FROM PROJECTS" in sql and "FOR UPDATE" in sql and not project_lookup
        result = await original_scalar(session, statement, *args, **kwargs)
        if holder_pid is None and ((first_operation == "create" and project_lookup)
                                   or (first_operation == "transfer" and transfer_lock)):
            # The old name path is deliberately paused after its real SELECT
            # too, so baseline failure detects the missing protection.
            holder_pid = await original_scalar(session, select(func.pg_backend_pid()))
            acquired.set()
            await asyncio.wait_for(release.wait(), timeout=15)
        return result

    monkeypatch.setattr(AsyncSession, "scalar", scalar_with_project_pause)

    async def create():
        return await client.post("/api/datasets", headers=_headers(auth_headers, graph),
                                 json={"name": graph["project_name"]})

    operations = {"create": create, "transfer": lambda: _post(client, auth_headers, graph)}
    second_operation = "transfer" if first_operation == "create" else "create"
    first = asyncio.create_task(operations[first_operation]())
    second = None
    try:
        await asyncio.wait_for(acquired.wait(), timeout=5)
        second = asyncio.create_task(operations[second_operation]())
        await _wait_for_lock_waiter(app, holder_pid, second)
        assert not first.done() and not second.done()
    finally:
        release.set()
        results = await asyncio.wait_for(asyncio.gather(*([first, second] if second else [first]),
                                                        return_exceptions=True), timeout=15)
    assert len(results) == 2
    assert all(not isinstance(result, BaseException) for result in results), results
    responses = dict(zip([first_operation, second_operation], results, strict=True))
    assert responses["create"].status_code == 201, responses["create"].text
    assert responses["transfer"].status_code == 200, responses["transfer"].text
    assert responses["transfer"].json()["moved_bytes"] == 137
    async with app.state.session_factory() as session:
        dataset = await session.get(Dataset, responses["create"].json()["id"])
        project = await session.get(Project, dataset.project_id)
        assert dataset.workspace_id == project.workspace_id
        if first_operation == "create":
            assert dataset.project_id == graph["project"]
            assert dataset.workspace_id == graph["target"]
        else:
            # After the blocked name lookup resumes, its WHERE clause must
            # see that the original project has moved and create a new one.
            assert dataset.project_id != graph["project"]
            assert dataset.workspace_id == graph["source"]
            assert project.name == graph["project_name"]
        mismatches = (await session.execute(select(Dataset.id).join(Project).where(
            Dataset.workspace_id.in_([graph["source"], graph["target"]]),
            Dataset.workspace_id != Project.workspace_id,
        ))).scalars().all()
        assert mismatches == []
    _files_unchanged(app, graph)


@pytest.mark.parametrize("first_operation", ["remove", "transfer"])
async def test_target_member_removal_serializes_with_transfer_authorization(
    client, app, auth_headers, transfer_graph, monkeypatch, first_operation,
):
    from app.routers import workspaces as workspace_routes
    from app.services import project_transfer

    graph = await transfer_graph(target_role="admin")
    before = await _snapshot(app, graph)
    acquired, release = asyncio.Event(), asyncio.Event()
    holder_pid = None
    original_remove, original_membership = workspace_routes.remove_workspace_member, project_transfer._membership

    async def pause(session):
        nonlocal holder_pid
        holder_pid = await session.scalar(select(func.pg_backend_pid()))
        acquired.set()
        await asyncio.wait_for(release.wait(), timeout=15)

    async def remove_after_workspace_lock(session, **kwargs):
        if first_operation == "remove" and holder_pid is None:
            # The route's mutation dependency has already obtained the real
            # Workspace lock, while membership still exists until commit.
            await pause(session)
        return await original_remove(session, **kwargs)

    async def membership_after_workspace_lock(session, workspace_id, user_id, minimum_role):
        result = await original_membership(session, workspace_id, user_id, minimum_role)
        if first_operation == "transfer" and workspace_id == graph["target"] and holder_pid is None:
            await pause(session)
        return result

    monkeypatch.setattr(workspace_routes, "remove_workspace_member", remove_after_workspace_lock)
    monkeypatch.setattr(project_transfer, "_membership", membership_after_workspace_lock)

    async def remove():
        return await client.delete(
            f"/api/workspaces/{graph['target']}/members/{graph['actor']}",
            headers=_headers(auth_headers, graph, actor=graph["actor"] + 1,
                             workspace_id=graph["target"]),
        )

    operations = {"remove": remove, "transfer": lambda: _post(client, auth_headers, graph)}
    second_operation = "transfer" if first_operation == "remove" else "remove"
    first = asyncio.create_task(operations[first_operation]())
    second = None
    try:
        await asyncio.wait_for(acquired.wait(), timeout=5)
        second = asyncio.create_task(operations[second_operation]())
        await _wait_for_lock_waiter(app, holder_pid, second)
        assert not first.done() and not second.done()
    finally:
        release.set()
        results = await asyncio.wait_for(asyncio.gather(*([first, second] if second else [first]),
                                                        return_exceptions=True), timeout=15)
    assert len(results) == 2
    assert all(not isinstance(result, BaseException) for result in results), results
    responses = dict(zip([first_operation, second_operation], results, strict=True))
    assert responses["remove"].status_code == 204, responses["remove"].text
    if first_operation == "remove":
        assert responses["transfer"].status_code == 404, responses["transfer"].text
        assert await _snapshot(app, graph) == before
    else:
        # Removal is allowed after a transfer that committed with valid roles;
        # it must never race between authorization and the transfer commit.
        assert responses["transfer"].status_code == 200, responses["transfer"].text
        assert responses["transfer"].json()["moved_bytes"] == 137
    async with app.state.session_factory() as session:
        assert await session.get(WorkspaceMember, (graph["target"], graph["actor"])) is None
        project = await session.get(Project, graph["project"])
        assert project.workspace_id == graph["source" if first_operation == "remove" else "target"]
    _files_unchanged(app, graph)


@pytest.mark.parametrize("source_kind,target_kind", [("team", "personal"), ("personal", "team")])
async def test_transfer_reclaims_only_assignments_outside_target_membership_and_keeps_history(
    client, app, auth_headers, transfer_graph, source_kind, target_kind,
):
    graph = await transfer_graph(source_kind=source_kind, target_kind=target_kind)
    now = datetime(2026, 10, 15, 0, 0, tzinfo=timezone.utc)
    outsider = graph["roles"]["editor"]
    async with app.state.session_factory() as session:
        # Personal workspaces have only their owner. Keep the graph realistic
        # while retaining a former team labeler's historical identity.
        for side, kind in (("source", source_kind), ("target", target_kind)):
            if kind == "personal":
                await session.execute(delete(WorkspaceMember).where(
                    WorkspaceMember.workspace_id == graph[side],
                    WorkspaceMember.user_id != graph["actor"],
                ))
        first = await session.get(Image, graph["images"][0])
        own = await session.get(Image, graph["images"][1])
        labeled = await session.get(Image, graph["images"][2])
        first.label_status = own.label_status = "in_progress"
        first.assignee_id = outsider if source_kind == "team" else graph["actor"]
        own.assignee_id = graph["actor"]
        first.assigned_at = own.assigned_at = now
        # A previously labeled image being edited still retains its history
        # when a stale assignment is reclaimed.
        first.labeled_by = outsider
        first.labeled_at = now
        labeled.label_status = "labeled"
        labeled.assignee_id = outsider
        labeled.assigned_at = now
        labeled.labeled_by = outsider
        labeled.labeled_at = now
        idle = image_with_media(
            workspace_id=graph["source"], dataset_id=graph["datasets"][0],
            media_object_id=first.media_object_id, stem="idle", filename="idle.jpg", rel_path="idle.jpg",
            width=64, height=48, file_path=first.file_path, thumb_path=first.thumb_path,
            label_status="unlabeled", version=17,
        )
        session.add(idle)
        await session.commit()
    before = await _snapshot(app, graph)
    response = await _post(client, auth_headers, graph)
    assert response.status_code == 200, response.text
    assert response.json()["moved_bytes"] == graph["moved_bytes"] == 137
    after = await _snapshot(app, graph)
    for previous, current in zip(before["images"], after["images"], strict=True):
        expected = dict(previous)
        if source_kind == "team" and current["id"] == graph["images"][0]:
            expected.update(label_status="unlabeled", assignee_id=None, assigned_at=None,
                            version=previous["version"] + 1)
        assert current == expected
    assert after["annotations"] == before["annotations"]
    usage = {row["workspace_id"]: row["bytes_used"] for row in after["workspace_storage"]}
    assert usage == {graph["source"]: 400, graph["target"]: 837}
    assert sum(usage.values()) == sum(row["bytes_used"] for row in before["workspace_storage"])
    _files_unchanged(app, graph)
