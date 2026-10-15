"""Workspace role contracts exercised through real authenticated HTTP requests."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import APIRouter
from fastapi.routing import APIRoute
from sqlalchemy import select

from app.models import (
    Annotation, Dataset, DatasetClass, Project, ProjectClass, RunImage,
    TrainingRun, UploadJob, UploadSession, Workspace, WorkspaceMember,
    WorkspaceStorage,
)
from app.services import training
from app.services.storage import storage_relative_path
from tests.factories import image_with_media


pytestmark = pytest.mark.asyncio
USERS = {"viewer": 910_101, "editor": 910_102, "admin": 910_103, "owner": 910_104}
ROLES = tuple(USERS)
TRAIN_BODY = {"weights": "yolo26n.pt", "epochs": 3, "imgsz": 640, "batch": -1,
              "ratios": {"train": .5, "valid": .5}}
MANIFEST = {"total_size": 1, "largest_file_size": 1, "file_count": 1,
            "expected_extracted_size": 1}
FILE = {"filename": "incoming.jpg", "size": 1, "chunk_size": 1, "kind": "file"}


def _name(suffix: str) -> str:
    return f"test-role-{suffix}-{uuid4().hex}"


def _headers(auth_headers, graph, role):
    return {**auth_headers(USERS[role]), "X-Workspace-Id": str(graph["workspace_id"])}


def _denied(response, required, actual):
    assert response.status_code == 403, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "insufficient_role"
    assert detail["required"] == required
    assert detail["actual"] == actual


async def _seed_graph(app, *, members=None):
    """Use distinct user/workspace IDs and real files, without any worker process."""
    members = USERS if members is None else members
    root = app.state.settings.storage_dir
    async with app.state.session_factory() as session:
        workspace = Workspace(kind="team", name=_name("team"), created_by=members["owner"])
        session.add(workspace)
        await session.flush()
        session.add_all(WorkspaceMember(workspace_id=workspace.id, user_id=user, role=role)
                        for role, user in members.items())
        project = Project(workspace_id=workspace.id, name=_name("project"))
        session.add(project)
        await session.flush()
        session.add(ProjectClass(project_id=project.id, class_id=0, name="box", color="#22AA66"))
        dataset_ids = []
        images = []
        for dataset_index in range(3):
            dataset = Dataset(workspace_id=workspace.id, project_id=project.id,
                              name=_name("dataset"), status="ready", storage_path="",
                              image_count=4, annotation_count=4, class_count=1)
            session.add(dataset)
            await session.flush()
            dataset_root = root / "datasets" / str(dataset.id)
            dataset_root.mkdir(parents=True)
            dataset.storage_path = storage_relative_path(root, dataset_root)
            session.add(DatasetClass(dataset_id=dataset.id, class_id=0, name="box"))
            dataset_ids.append(dataset.id)
            for index in range(4):
                source = dataset_root / f"image-{index}.jpg"
                source.write_bytes(b"fixture-image")
                image = image_with_media(
                    workspace_id=workspace.id, dataset_id=dataset.id,
                    stem=f"image-{index}", filename=source.name, rel_path=source.name,
                    split=None, width=32, height=24,
                    file_path=storage_relative_path(root, source), display_path=None,
                    thumb_path=storage_relative_path(root, source), original_bytes=13,
                    has_label_source=True, box_count=1,
                )
                image.annotations.append(Annotation(class_id=0, cx=.5, cy=.5, w=.25, h=.25))
                session.add(image)
                images.append(image)
        await session.flush()
        run_root = root / "training-runs" / uuid4().hex
        artifacts = run_root / "artifacts"
        artifacts.mkdir(parents=True)
        (artifacts / "best.pt").write_bytes(b"fixture-checkpoint")
        (artifacts / "log").write_text("fixture log\n")
        run = TrainingRun(
            workspace_id=workspace.id, created_by=members["admin"],
            dataset_id=dataset_ids[0], dataset_name=_name("run"), weights="yolo26n.pt",
            epochs=3, imgsz=640, batch=4, split_mode="2way",
            ratios={"train": .8, "valid": .2}, seed=17, state="done",
            started_at=datetime.now(timezone.utc), finished_at=datetime.now(timezone.utc),
            out_dir=storage_relative_path(root, run_root), artifact_bytes=0,
        )
        session.add(run)
        upload = UploadSession(dataset_id=dataset_ids[0], **FILE, received_chunks=[], state="open")
        job = UploadJob(dataset_id=dataset_ids[0], kind="file", state="done", phase="done",
                        total=1, processed=1, failed=0)
        session.add_all([upload, job, WorkspaceStorage(workspace_id=workspace.id, bytes_used=156)])
        await session.flush()
        run_image = RunImage(run_id=run.id, image_id=images[0].id, split="valid",
                             stem=images[0].stem, filename=images[0].filename,
                             rel_path=images[0].rel_path)
        session.add(run_image)
        await session.commit()
        return {"workspace_id": workspace.id, "project_id": project.id,
                "dataset_ids": dataset_ids, "dataset_id": dataset_ids[0],
                "image_id": images[0].id, "run_id": run.id, "run_image_id": run_image.id,
                "upload_id": upload.id, "job_id": job.id,
                "project_updated_at": project.updated_at.isoformat()}


@pytest_asyncio.fixture
async def graph(app):
    return await _seed_graph(app)


@pytest.mark.parametrize("role", ROLES)
async def test_row1_all_roles_can_read_download_and_preview(client, auth_headers, graph,
                                                          role, monkeypatch):
    headers = _headers(auth_headers, graph, role)
    paths = ["/api/projects", f"/api/projects/{graph['project_id']}",
             "/api/datasets", f"/api/datasets/{graph['dataset_id']}",
             f"/api/datasets/{graph['dataset_id']}/classes",
             f"/api/datasets/{graph['dataset_id']}/images",
             f"/api/datasets/{graph['dataset_id']}/issues",
             f"/api/images/{graph['image_id']}/file", f"/api/images/{graph['image_id']}/thumb",
             f"/api/images/{graph['image_id']}/annotations", f"/api/uploads/{graph['upload_id']}",
             f"/api/jobs/{graph['job_id']}", "/api/runs", f"/api/runs/{graph['run_id']}",
             f"/api/runs/{graph['run_id']}/metrics", f"/api/runs/{graph['run_id']}/log",
             f"/api/runs/{graph['run_id']}/artifacts/best.pt",
             f"/api/runs/{graph['run_id']}/inference-images", "/api/storage"]
    for path in paths:
        response = await client.get(path, headers=headers)
        assert response.status_code == 200, (role, path, response.text)
    monkeypatch.setattr("app.routers.runs.render_prediction_file", lambda *_args: b"preview")
    preview = await client.post(
        f"/api/runs/{graph['run_id']}/inference-images/{graph['run_image_id']}", headers=headers)
    assert preview.status_code == 200, preview.text
    assert preview.content == b"preview"


@pytest.mark.parametrize("role", ROLES)
async def test_row2_label_write_requires_editor(client, app, auth_headers, graph, role):
    headers = _headers(auth_headers, graph, role)
    current = await client.get(f"/api/images/{graph['image_id']}/annotations", headers=headers)
    assert current.status_code == 200, current.text
    response = await client.put(f"/api/images/{graph['image_id']}/annotations",
                                headers=headers,
                                json={"boxes": [], "expected_version": current.json()["version"],
                                      "expected_catalog_version": current.json()["catalog_version"]})
    if role == "viewer":
        _denied(response, "editor", role)
    else:
        assert response.status_code == 200, response.text
        assert response.json()["boxes"] == []
    async with app.state.session_factory() as session:
        boxes = (await session.scalars(select(Annotation).where(
            Annotation.image_id == graph["image_id"]))).all()
        assert len(boxes) == (1 if role == "viewer" else 0)



async def test_dataset_without_project_requires_admin_and_creates_nothing_for_editor(
    client, app, auth_headers, graph,
):
    async with app.state.session_factory() as session:
        before_projects = set(await session.scalars(select(Project.id).where(
            Project.workspace_id == graph["workspace_id"])))
        before_datasets = set(await session.scalars(select(Dataset.id).where(
            Dataset.workspace_id == graph["workspace_id"])))
    response = await client.post("/api/datasets", headers=_headers(auth_headers, graph, "editor"),
                                 json={"name": _name("implicit-project-denied")})
    _denied(response, "admin", "editor")
    async with app.state.session_factory() as session:
        assert set(await session.scalars(select(Project.id).where(
            Project.workspace_id == graph["workspace_id"]))) == before_projects
        assert set(await session.scalars(select(Dataset.id).where(
            Dataset.workspace_id == graph["workspace_id"]))) == before_datasets


async def test_admin_dataset_without_project_creates_an_implicit_project(
    client, app, auth_headers, graph,
):
    name = _name("implicit-project-admin")
    async with app.state.session_factory() as session:
        before_projects = set(await session.scalars(select(Project.id).where(
            Project.workspace_id == graph["workspace_id"])))
    response = await client.post("/api/datasets", headers=_headers(auth_headers, graph, "admin"),
                                 json={"name": name})
    assert response.status_code == 201, response.text
    async with app.state.session_factory() as session:
        dataset = await session.get(Dataset, response.json()["id"])
        project = await session.get(Project, dataset.project_id)
        assert project.workspace_id == dataset.workspace_id == graph["workspace_id"]
        assert project.name == dataset.name == name
        assert set(await session.scalars(select(Project.id).where(
            Project.workspace_id == graph["workspace_id"]))) == before_projects | {project.id}
        assert project.id not in before_projects


async def test_editor_dataset_with_project_preserves_project_count(
    client, app, auth_headers, graph,
):
    async with app.state.session_factory() as session:
        before_projects = set(await session.scalars(select(Project.id).where(
            Project.workspace_id == graph["workspace_id"])))
    response = await client.post("/api/datasets", headers=_headers(auth_headers, graph, "editor"),
                                 json={"name": _name("explicit-project"), "project_id": graph["project_id"]})
    assert response.status_code == 201, response.text
    async with app.state.session_factory() as session:
        dataset = await session.get(Dataset, response.json()["id"])
        assert dataset.project_id == graph["project_id"]
        assert dataset.workspace_id == graph["workspace_id"]
        assert set(await session.scalars(select(Project.id).where(
            Project.workspace_id == graph["workspace_id"]))) == before_projects


async def test_personal_dataset_without_project_keeps_implicit_project_creation(
    client, app, auth_headers, graph,
):
    # This user is an editor in a team but still owns their personal workspace.
    name = _name("implicit-personal")
    response = await client.post("/api/datasets", headers=auth_headers(USERS["editor"]),
                                 json={"name": name})
    assert response.status_code == 201, response.text
    async with app.state.session_factory() as session:
        dataset = await session.get(Dataset, response.json()["id"])
        project = await session.get(Project, dataset.project_id)
        workspace = await session.get(Workspace, dataset.workspace_id)
        membership = await session.get(WorkspaceMember, (workspace.id, USERS["editor"]))
        assert workspace.kind == "personal" and membership.role == "owner"
        assert workspace.id != graph["workspace_id"]
        assert project.workspace_id == workspace.id and project.name == name


def _upload_requests(graph):
    dataset = graph["dataset_id"]
    upload = graph["upload_id"]
    return [
        ("POST", "/api/datasets", {"json": {"name": _name("new"), "project_id": graph["project_id"]}}),
        ("POST", f"/api/datasets/{dataset}/uploads", {"json": FILE}),
        ("POST", f"/api/datasets/{dataset}/uploads/batch", {"json": {"files": [FILE]}}),
        ("PUT", f"/api/datasets/{dataset}/upload-batches/{uuid4()}", {"json": MANIFEST}),
        ("POST", f"/api/datasets/{dataset}/upload-batches/preflight", {"json": MANIFEST}),
        ("POST", f"/api/datasets/{dataset}/uploads/chunks/batch", {"data": {"metadata": json.dumps(
            {"chunks": [{"upload_id": upload, "chunk_number": 0, "size": 1}]})},
            "files": [("chunks", ("part", b"x", "application/octet-stream"))]}),
        ("PUT", f"/api/uploads/{upload}/chunks/0", {"content": b"x"}),
        ("POST", f"/api/uploads/{upload}/complete", {}),
        ("POST", f"/api/datasets/{dataset}/upload-batches/complete", {"json": {"upload_ids": [upload]}}),
        ("DELETE", f"/api/uploads/{upload}", {}),
        ("POST", f"/api/jobs/{graph['job_id']}/class-resolution",
         {"json": {"revision": "0" * 64, "resolutions": []}}),
    ]


async def test_row3_viewer_cannot_mutate_any_upload_stage(client, app, auth_headers, graph):
    from app.models import UploadBatch
    batch_id = uuid4()
    async with app.state.session_factory() as session:
        session.add(UploadBatch(id=batch_id, dataset_id=graph["dataset_id"], expected_file_count=1,
                                 expected_total_size=1, expected_extracted_size=1, largest_file_size=1))
        await session.commit()
    cases = [*_upload_requests(graph), ("POST",
             f"/api/datasets/{graph['dataset_id']}/upload-batches/{batch_id}/complete", {})]
    for method, path, kwargs in cases:
        response = await client.request(method, path, headers=_headers(auth_headers, graph, "viewer"), **kwargs)
        _denied(response, "editor", "viewer")


@pytest.mark.parametrize("kind", ["single", "batch", "manifest", "chunk-batch", "abort"])
async def test_row3_editor_upload_workflows(client, app, auth_headers, graph, kind):
    headers = _headers(auth_headers, graph, "editor")
    created = await client.post("/api/datasets", headers=headers,
                                json={"name": _name(kind), "project_id": graph["project_id"]})
    assert created.status_code == 201, created.text
    dataset_id = created.json()["id"]
    preflight = await client.post(f"/api/datasets/{dataset_id}/upload-batches/preflight",
                                  headers=headers, json=MANIFEST)
    assert preflight.status_code == 204, preflight.text
    batch_id = uuid4()
    if kind == "manifest":
        manifest = await client.put(f"/api/datasets/{dataset_id}/upload-batches/{batch_id}",
                                    headers=headers, json=MANIFEST)
        assert manifest.status_code == 200, manifest.text
    if kind in {"batch", "manifest", "chunk-batch"}:
        body = {"files": [FILE]}
        if kind == "manifest":
            body["batch_id"] = str(batch_id)
        sessions = await client.post(f"/api/datasets/{dataset_id}/uploads/batch", headers=headers, json=body)
        assert sessions.status_code == 201, sessions.text
        upload_id = sessions.json()["uploads"][0]["upload_id"]
    else:
        upload = await client.post(f"/api/datasets/{dataset_id}/uploads", headers=headers, json=FILE)
        assert upload.status_code == 201, upload.text
        upload_id = upload.json()["upload_id"]
    if kind == "abort":
        aborted = await client.delete(f"/api/uploads/{upload_id}", headers=headers)
        assert aborted.status_code == 204, aborted.text
        return
    if kind == "chunk-batch":
        chunk = await client.post(f"/api/datasets/{dataset_id}/uploads/chunks/batch", headers=headers,
                                  data={"metadata": json.dumps({"chunks": [
                                      {"upload_id": upload_id, "chunk_number": 0, "size": 1}]})},
                                  files=[("chunks", ("part", b"x", "application/octet-stream"))])
    else:
        chunk = await client.put(f"/api/uploads/{upload_id}/chunks/0", headers=headers, content=b"x")
    assert chunk.status_code == 204, chunk.text
    if kind == "manifest":
        done = await client.post(f"/api/datasets/{dataset_id}/upload-batches/{batch_id}/complete", headers=headers)
    elif kind in {"batch", "chunk-batch"}:
        done = await client.post(f"/api/datasets/{dataset_id}/upload-batches/complete", headers=headers,
                                 json={"upload_ids": [upload_id]})
    else:
        done = await client.post(f"/api/uploads/{upload_id}/complete", headers=headers)
    assert done.status_code == 202, done.text
    async with app.state.session_factory() as session:
        dataset = await session.get(Dataset, dataset_id)
        job = await session.get(UploadJob, done.json()["job_id"])
        assert dataset.workspace_id == graph["workspace_id"]
        assert job.dataset_id == dataset_id and job.state == "queued"


async def test_row3_editor_can_resolve_upload_class_conflicts(client, app, auth_headers, graph):
    async with app.state.session_factory() as session:
        job = await session.get(UploadJob, graph["job_id"])
        job.state = "awaiting_class_resolution"
        job.class_resolution_plan = {"revision": "0" * 64, "conflicts": [{
            "key": "class:0", "class_id": 0, "source_path": "data.yaml",
            "project_name": "box", "uploaded_name": "object"}]}
        await session.commit()
    response = await client.post(f"/api/jobs/{graph['job_id']}/class-resolution",
                                 headers=_headers(auth_headers, graph, "editor"), json={
                                     "revision": "0" * 64,
                                     "resolutions": [{"key": "class:0", "action": "use_project"}]})
    assert response.status_code == 202, response.text


def _admin_requests(graph):
    dataset, project, run = graph["dataset_id"], graph["project_id"], graph["run_id"]
    return [
        ("POST", "/api/projects", {"json": {"name": _name("project-create")}}),
        ("PATCH", f"/api/projects/{project}", {"json": {"name": _name("renamed")}}),
        ("PATCH", f"/api/projects/{project}/classes/0", {"json": {"name": "new-box"}}),
        ("PUT", f"/api/projects/{project}/classes", {"json": {
            "expected_updated_at": graph["project_updated_at"],
            "classes": [{"class_id": 0, "name": "box", "color": "#22AA66"}]}}),
        ("DELETE", f"/api/projects/{project}", {}),
        ("PATCH", f"/api/datasets/{dataset}", {"json": {"name": _name("renamed")}}),
        ("PATCH", f"/api/datasets/{dataset}/classes/0", {"json": {"name": "new-box"}}),
        ("DELETE", f"/api/datasets/{dataset}", {}),
        ("POST", "/api/datasets/merge", {"json": {"name": _name("merge"),
            "dataset_ids": graph["dataset_ids"][:2]}}),
        ("POST", "/api/datasets/extract", {"json": {"name": _name("extract"),
            "dataset_ids": [dataset], "class_ids": [0]}}),
        ("POST", f"/api/datasets/{dataset}/merge-sources", {"json": {
            "dataset_ids": [graph["dataset_ids"][1]]}}),
        ("POST", f"/api/datasets/{dataset}/train", {"json": TRAIN_BODY}),
        ("POST", f"/api/runs/{run}/cancel", {}),
        ("DELETE", f"/api/runs/{run}/artifacts", {}),
        ("DELETE", f"/api/runs/{run}", {}),
    ]


@pytest.mark.parametrize("role", ["viewer", "editor"])
async def test_rows4_and5_lower_roles_cannot_manage_catalog_or_training(client, auth_headers, graph, role):
    for method, path, kwargs in _admin_requests(graph):
        response = await client.request(method, path, headers=_headers(auth_headers, graph, role), **kwargs)
        _denied(response, "admin", role)


@pytest.mark.parametrize("role", ["admin", "owner"])
async def test_row4_admin_and_owner_manage_projects_classes_and_dataset_names(client, auth_headers, graph, role):
    headers = _headers(auth_headers, graph, role)
    project, dataset = graph["project_id"], graph["dataset_id"]
    for path in (f"/api/projects/{project}", f"/api/datasets/{dataset}"):
        renamed = await client.patch(path, headers=headers, json={"name": _name("rename")})
        assert renamed.status_code == 200, renamed.text
    for path in (f"/api/projects/{project}/classes/0", f"/api/datasets/{dataset}/classes/0"):
        renamed = await client.patch(path, headers=headers, json={"name": "renamed-box"})
        assert renamed.status_code == 200, renamed.text
    latest = await client.get(f"/api/projects/{project}", headers=headers)
    replaced = await client.put(f"/api/projects/{project}/classes", headers=headers, json={
        "expected_updated_at": latest.json()["updated_at"],
        "classes": [{"class_id": 0, "name": "renamed-box", "color": "#22AA66"}]})
    assert replaced.status_code == 200, replaced.text
    created = await client.post("/api/projects", headers=headers, json={"name": _name("new-project")})
    assert created.status_code == 201, created.text
    deleted = await client.delete(f"/api/projects/{created.json()['id']}", headers=headers)
    assert deleted.status_code == 204, deleted.text
    deleted_dataset = await client.delete(f"/api/datasets/{graph['dataset_ids'][2]}", headers=headers)
    assert deleted_dataset.status_code == 204, deleted_dataset.text


async def test_row4_editor_denied_admin_can_merge_projects(client, auth_headers, graph):
    admin = _headers(auth_headers, graph, "admin")
    projects = []
    for _ in range(2):
        created = await client.post("/api/projects", headers=admin, json={"name": _name("merge-source")})
        assert created.status_code == 201, created.text
        projects.append({"project_id": created.json()["id"], "expected_updated_at": created.json()["updated_at"]})
    body = {"name": _name("project-merge"), "projects": projects, "classes": []}
    denied = await client.post("/api/projects/merge", headers=_headers(auth_headers, graph, "editor"), json=body)
    _denied(denied, "admin", "editor")
    allowed = await client.post("/api/projects/merge", headers=admin, json=body)
    assert allowed.status_code == 201, allowed.text


async def test_row4_admin_can_extract_merge_and_extend_datasets(client, auth_headers, graph):
    headers = _headers(auth_headers, graph, "admin")
    extracted = await client.post("/api/datasets/extract", headers=headers, json={
        "name": _name("extracted"), "dataset_ids": [graph["dataset_id"]], "class_ids": [0]})
    assert extracted.status_code == 201, extracted.text
    merged = await client.post("/api/datasets/merge", headers=headers, json={
        "name": _name("merged"), "dataset_ids": graph["dataset_ids"][:2]})
    assert merged.status_code == 201, merged.text
    extended = await client.post(f"/api/datasets/{merged.json()['id']}/merge-sources", headers=headers,
                                 json={"dataset_ids": [graph["dataset_ids"][2]]})
    assert extended.status_code == 200, extended.text


def _host_ready(monkeypatch):
    monkeypatch.setattr(training, "is_container_environment", lambda: False)
    monkeypatch.setattr(training.torch.cuda, "is_available", lambda: True)
    monkeypatch.setattr(training.torch.cuda, "mem_get_info", lambda: (24 * 1024**3, 24 * 1024**3))
    monkeypatch.setattr(training, "_weight_is_available", lambda _name: True)
    monkeypatch.setattr(training, "spawn_worker", lambda *_args: training.SpawnedWorker(
        pid=4242, pid_started_at="fixture", boot_id="fixture"))


@pytest.mark.parametrize("role", ["admin", "owner"])
async def test_row5_admin_and_owner_submit_cancel_and_delete_runs(client, app, auth_headers, graph, role, monkeypatch):
    headers = _headers(auth_headers, graph, role)
    _host_ready(monkeypatch)
    submitted = await client.post(f"/api/datasets/{graph['dataset_id']}/train", headers=headers, json=TRAIN_BODY)
    assert submitted.status_code == 201, submitted.text
    run_id = submitted.json()["run_id"]
    async with app.state.session_factory() as session:
        run = await session.get(TrainingRun, run_id)
        assert run.workspace_id == graph["workspace_id"] and run.created_by == USERS[role]
        # No process was started; the existing no-PID recovery path cancels safely.
        run.pid = None
        await session.commit()
    canceled = await client.post(f"/api/runs/{run_id}/cancel", headers=headers)
    assert canceled.status_code == 202, canceled.text
    artifacts = await client.delete(f"/api/runs/{graph['run_id']}/artifacts", headers=headers)
    assert artifacts.status_code == 204, artifacts.text
    deleted = await client.delete(f"/api/runs/{graph['run_id']}", headers=headers)
    assert deleted.status_code == 409, deleted.text
    assert deleted.json()["detail"]["code"] == "run-delete-confirmation-required"
    deleted = await client.delete(f"/api/runs/{graph['run_id']}?confirm=true", headers=headers)
    assert deleted.status_code == 204, deleted.text


async def test_row5_active_run_exclusion_is_global_across_teams(client, app, auth_headers, graph, monkeypatch):
    other = await _seed_graph(app)
    async with app.state.session_factory() as session:
        run = await session.get(TrainingRun, graph["run_id"])
        run.state = "running"
        run.finished_at = None
        await session.commit()
    _host_ready(monkeypatch)
    def must_not_spawn(*_args):
        pytest.fail("a second team must not start a second training process")
    monkeypatch.setattr(training, "spawn_worker", must_not_spawn)
    rejected = await client.post(f"/api/datasets/{other['dataset_id']}/train",
                                  headers=_headers(auth_headers, other, "admin"), json=TRAIN_BODY)
    assert rejected.status_code == 409, rejected.text
    assert rejected.json()["detail"] == training.ACTIVE_RUN_DETAIL


@pytest.mark.parametrize("scope", ["nonmember", "other-member-workspace"])
async def test_scope_misses_stay_404_before_insufficient_role(client, app, auth_headers, graph, scope):
    current = await client.get(f"/api/images/{graph['image_id']}/annotations",
                               headers=_headers(auth_headers, graph, "owner"))
    assert current.status_code == 200, current.text
    if scope == "nonmember":
        headers = {**auth_headers(919_999), "X-Workspace-Id": str(graph["workspace_id"])}
    else:
        other = await _seed_graph(app)
        headers = _headers(auth_headers, other, "viewer")
    # Resource IDs in paths belong to the first workspace. Body-only creation
    # routes have no existing path resource to hide from an insufficient role.
    for method, path, kwargs in [*_upload_requests(graph), *_admin_requests(graph)]:
        if path in {"/api/projects", "/api/datasets", "/api/datasets/merge", "/api/datasets/extract"}:
            continue
        response = await client.request(method, path, headers=headers, **kwargs)
        assert response.status_code == 404, (method, path, response.text)
    image = await client.put(f"/api/images/{graph['image_id']}/annotations", headers=headers,
                             json={"boxes": [], "expected_version": current.json()["version"],
                                   "expected_catalog_version": current.json()["catalog_version"]})
    assert image.status_code == 404, image.text


ROLE_FREE_MUTATIONS = {("POST", "/api/workspaces"), ("POST", "/api/invites/{token}/accept")}


def _declared_roles(dependant):
    role = getattr(dependant.call, "minimum_role", None)
    result = {role} if role in ROLES else set()
    for child in dependant.dependencies:
        result.update(_declared_roles(child))
    return result


def _unguarded_mutations(routes):
    missing = set()
    for route in routes:
        if not isinstance(route, APIRoute) or not route.path.startswith("/api/"):
            continue
        if route.path.startswith("/api/admin/") or route.path == "/api/health":
            continue
        for method in route.methods - {"GET", "HEAD", "OPTIONS"}:
            if (method, route.path) not in ROLE_FREE_MUTATIONS and not _declared_roles(route.dependant):
                missing.add((method, route.path))
    return missing


async def test_mutation_route_registry_requires_explicit_role_dependency(app):
    assert not _unguarded_mutations(app.routes)


async def test_registry_detects_new_unguarded_mutation_even_when_other_routes_are_guarded(app):
    router = APIRouter()
    @router.post("/api/role-registry-probe")
    async def unguarded_probe():
        return {"ok": True}
    assert _unguarded_mutations([*app.routes, *router.routes]) >= {("POST", "/api/role-registry-probe")}


async def test_registry_follows_nested_dependency_tree():
    from fastapi import Depends
    from app.deps import require_role

    # The guard may live below a path-scoped resource dependency.
    guard = require_role("admin")
    async def nested(scope=Depends(guard)):
        return scope
    router = APIRouter()
    @router.patch("/api/role-registry-nested", dependencies=[Depends(nested)])
    async def guarded_probe():
        return {"ok": True}
    assert _declared_roles(router.routes[0].dependant) == {"admin"}
    assert not _unguarded_mutations(router.routes)
