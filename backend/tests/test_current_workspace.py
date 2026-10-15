from __future__ import annotations

import asyncio
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import func, select


pytestmark = pytest.mark.asyncio


def _headers(auth_headers, user_id: int, workspace_id: int | None = None) -> dict[str, str]:
    headers = auth_headers(user_id)
    if workspace_id is not None:
        headers["X-Workspace-Id"] = str(workspace_id)
    return headers


async def _workspace(app, user_id: int, *, kind: str = "team",
                     workspace_id: int | None = None, member: bool = True) -> int:
    from app.models import Workspace, WorkspaceMember

    async with app.state.session_factory() as session:
        workspace = Workspace(
            **({"id": workspace_id} if workspace_id is not None else {}),
            kind=kind,
            name=f"test-workspace-{uuid4().hex}",
            created_by=user_id,
        )
        session.add(workspace)
        await session.flush()
        if member:
            session.add(WorkspaceMember(
                workspace_id=workspace.id, user_id=user_id, role="owner",
            ))
        await session.commit()
        return workspace.id


async def test_headerless_request_resolves_personal_membership_not_user_id(
    app, client: httpx.AsyncClient, auth_headers,
) -> None:
    from app.models import Project, Workspace

    user_id = 900_001
    workspace_id = await _workspace(app, user_id, kind="personal", workspace_id=700_001)
    async with app.state.session_factory() as session:
        project = Project(workspace_id=workspace_id, name=f"test-personal-{uuid4().hex}")
        session.add(project)
        await session.commit()
        project_id = project.id

    response = await client.get("/api/projects", headers=auth_headers(user_id))

    assert response.status_code == 200, response.text
    assert [item["id"] for item in response.json()["items"]] == [project_id]
    padded = await client.get("/api/projects", headers={
        **auth_headers(user_id), "X-Workspace-Id": f"000{workspace_id}",
    })
    assert padded.status_code == 200, padded.text
    assert [item["id"] for item in padded.json()["items"]] == [project_id]
    async with app.state.session_factory() as session:
        assert await session.scalar(select(func.count(Workspace.id)).where(
            Workspace.kind == "personal", Workspace.created_by == user_id,
        )) == 1
        assert await session.get(Workspace, user_id) is None


@pytest.mark.parametrize("header", [
    b"", b" ", b" 1", b"1 ", b"+1", b"-1", b"0", b"1.0", b"1e3", b"1,2",
    b"\xb2", b"0000",
])
async def test_invalid_workspace_header_is_a_structured_400(
    client: httpx.AsyncClient, auth_headers, header: bytes,
) -> None:
    headers = [(name.encode("ascii"), value.encode("ascii"))
               for name, value in auth_headers(900_002).items()]
    headers.append((b"X-Workspace-Id", header))

    response = await client.get("/api/projects", headers=headers)

    assert response.status_code == 400, response.text
    assert response.json()["detail"]["code"] == "invalid_workspace_header"


@pytest.mark.parametrize("header", ["2147483648", "999999999999999999999999", "9" * 5000])
async def test_oversized_positive_workspace_id_is_hidden_without_database_overflow(
    client: httpx.AsyncClient, auth_headers, header: str,
) -> None:
    response = await client.get("/api/projects", headers={
        **auth_headers(900_003), "X-Workspace-Id": header,
    })
    assert response.status_code == 404, response.text


async def test_workspace_header_hides_existing_team_from_nonmember(
    app, client: httpx.AsyncClient, auth_headers,
) -> None:
    workspace_id = await _workspace(app, 900_004)
    response = await client.get(
        "/api/projects", headers=_headers(auth_headers, 900_005, workspace_id),
    )
    assert response.status_code == 404, response.text


async def test_team_members_cannot_access_another_selected_workspaces_resources(
    app, client: httpx.AsyncClient, auth_headers,
) -> None:
    from app.models import Dataset, Project, UploadJob, WorkspaceMember
    from app.services.storage import storage_relative_path
    from tests.factories import image_with_media

    actor = 900_006
    team_a = await _workspace(app, 900_007)
    team_b = await _workspace(app, 900_007)
    async with app.state.session_factory() as session:
        session.add_all([
            WorkspaceMember(workspace_id=workspace, user_id=actor, role="admin")
            for workspace in (team_a, team_b)
        ])
        await session.commit()
    headers_a = _headers(auth_headers, actor, team_a)
    headers_b = _headers(auth_headers, actor, team_b)
    project_response = await client.post("/api/projects", headers=headers_a, json={
        "name": f"test-team-project-{uuid4().hex}", "classes": [],
    })
    assert project_response.status_code == 201, project_response.text
    project_id = project_response.json()["id"]
    dataset_response = await client.post("/api/datasets", headers=headers_a, json={
        "name": f"test-team-dataset-{uuid4().hex}", "project_id": project_id,
    })
    assert dataset_response.status_code == 201, dataset_response.text
    dataset_id = dataset_response.json()["id"]
    uploaded = await client.post(f"/api/datasets/{dataset_id}/uploads", headers=headers_a, json={
        "filename": "incoming.jpg", "size": 1, "chunk_size": 1, "kind": "file",
    })
    assert uploaded.status_code == 201, uploaded.text
    upload_id = uploaded.json()["upload_id"]
    image_path = app.state.settings.storage_dir / f"workspace-image-{uuid4().hex}.jpg"
    image_path.write_bytes(b"test-workspace-image")
    image_relative_path = storage_relative_path(app.state.settings.storage_dir, image_path)
    async with app.state.session_factory() as session:
        image = image_with_media(
            workspace_id=team_a, dataset_id=dataset_id, stem="original",
            filename="original.jpg", rel_path="images/original.jpg", split="train",
            width=16, height=12, file_path=image_relative_path,
            display_path=None, thumb_path=image_relative_path,
            original_bytes=image_path.stat().st_size,
        )
        job = UploadJob(dataset_id=dataset_id, kind="file", state="done", phase="done",
                        total=1, processed=1, failed=0)
        session.add_all([image, job])
        await session.commit()
        image_id, job_id = image.id, job.id
        project = await session.get(Project, project_id)
        dataset = await session.get(Dataset, dataset_id)
        assert project is not None and project.workspace_id == team_a
        assert dataset is not None and dataset.workspace_id == team_a

    paths = [
        f"/api/projects/{project_id}", f"/api/datasets/{dataset_id}",
        f"/api/images/{image_id}/file", f"/api/images/{image_id}/thumb",
        f"/api/uploads/{upload_id}", f"/api/jobs/{job_id}",
    ]
    for path in paths:
        allowed = await client.get(path, headers=headers_a)
        rejected = await client.get(path, headers=headers_b)
        assert allowed.status_code == 200, (path, allowed.text)
        assert rejected.status_code == 404, (path, rejected.text)
    for path, expected_id in (("/api/projects", project_id), ("/api/datasets", dataset_id)):
        allowed = await client.get(path, headers=headers_a)
        other = await client.get(path, headers=headers_b)
        assert allowed.status_code == other.status_code == 200
        assert [item["id"] for item in allowed.json()["items"]] == [expected_id]
        assert other.json()["items"] == []

    # A caller who belongs to both teams still cannot attach a new dataset
    # to a project selected through the other workspace header.
    rejected_create = await client.post("/api/datasets", headers=headers_b, json={
        "project_id": project_id, "name": f"test-cross-workspace-{uuid4().hex}",
    })
    assert rejected_create.status_code == 404, rejected_create.text


async def test_concurrent_first_requests_create_one_personal_workspace_and_membership(
    app, client: httpx.AsyncClient, auth_headers, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import deps
    from app.models import Workspace, WorkspaceMember

    actor = 900_008
    original = deps.ensure_personal_workspace
    both_entered = asyncio.Event()
    arrivals = 0

    async def arrive_together(session, user_id):
        nonlocal arrivals
        if user_id == actor:
            arrivals += 1
            if arrivals == 2:
                both_entered.set()
            await asyncio.wait_for(both_entered.wait(), timeout=10)
        return await original(session, user_id)

    monkeypatch.setattr(deps, "ensure_personal_workspace", arrive_together)
    first, second = await asyncio.gather(
        client.get("/api/projects", headers=auth_headers(actor)),
        client.get("/api/projects", headers=auth_headers(actor)),
    )

    assert arrivals == 2
    assert first.status_code == second.status_code == 200
    assert first.json()["items"] == second.json()["items"] == []
    async with app.state.session_factory() as session:
        personal = list((await session.scalars(select(Workspace).where(
            Workspace.kind == "personal", Workspace.created_by == actor,
        ))).all())
        assert len(personal) == 1
        members = list((await session.scalars(select(WorkspaceMember).where(
            WorkspaceMember.workspace_id == personal[0].id,
        ))).all())
        assert [(member.user_id, member.role) for member in members] == [(actor, "owner")]


async def test_storage_and_upload_quota_use_selected_team_instead_of_actor(
    app, client: httpx.AsyncClient, auth_headers,
) -> None:
    from app.models import WorkspaceMember, WorkspaceStorage

    actor = 900_009
    personal = await _workspace(app, actor, kind="personal")
    team = await _workspace(app, 900_010)
    async with app.state.session_factory() as session:
        session.add_all([
            WorkspaceMember(workspace_id=team, user_id=actor, role="admin"),
            WorkspaceStorage(workspace_id=personal, bytes_used=90, quota_limit_bytes=100),
            WorkspaceStorage(workspace_id=team, bytes_used=7, quota_limit_bytes=1000),
        ])
        await session.commit()
    personal_headers = auth_headers(actor)
    team_headers = _headers(auth_headers, actor, team)
    personal_quota = await client.get("/api/storage", headers=personal_headers)
    team_quota = await client.get("/api/storage", headers=team_headers)
    assert personal_quota.status_code == team_quota.status_code == 200
    assert personal_quota.json() == {"used_bytes": 90, "referenced_bytes": 0, "limit_bytes": 100}
    assert team_quota.json() == {"used_bytes": 7, "referenced_bytes": 0, "limit_bytes": 1000}

    datasets = []
    for headers in (personal_headers, team_headers):
        created = await client.post("/api/datasets", headers=headers, json={
            "name": f"test-quota-workspace-{uuid4().hex}",
        })
        assert created.status_code == 201, created.text
        datasets.append(created.json()["id"])
    manifest = {"total_size": 20, "largest_file_size": 20, "file_count": 1,
                "expected_extracted_size": 20}
    denied = await client.post(
        f"/api/datasets/{datasets[0]}/upload-batches/preflight",
        headers=personal_headers, json=manifest,
    )
    allowed = await client.post(
        f"/api/datasets/{datasets[1]}/upload-batches/preflight",
        headers=team_headers, json=manifest,
    )
    assert denied.status_code == 413, denied.text
    assert allowed.status_code == 204, allowed.text
    async with app.state.session_factory() as session:
        personal_storage = await session.get(WorkspaceStorage, personal)
        team_storage = await session.get(WorkspaceStorage, team)
        assert personal_storage is not None and personal_storage.bytes_used == 90
        assert team_storage is not None and team_storage.bytes_used == 7


@pytest.mark.parametrize(("kind", "used_bytes", "required_bytes", "detail"), [
    ("personal", 876, 2345,
     "사용자 저장공간 쿼터를 초과합니다. 잔여 용량 1,124 B, 필요 용량 2,345 B입니다."),
    ("team", 876, 2345,
     "워크스페이스 저장공간 쿼터를 초과합니다. 잔여 용량 1,124 B, 필요 용량 2,345 B입니다."),
    ("personal", None, 6000,
     "사용자 저장공간 쿼터를 초과합니다. 잔여 용량 5,000 B, 필요 용량 6,000 B입니다."),
    ("team", None, 6000,
     "워크스페이스 저장공간 쿼터를 초과합니다. 잔여 용량 5,000 B, 필요 용량 6,000 B입니다."),
])
async def test_upload_quota_detail_preserves_personal_message_and_names_team(
    app, client: httpx.AsyncClient, auth_headers, kind: str,
    used_bytes: int | None, required_bytes: int, detail: str,
) -> None:
    from app.models import WorkspaceStorage

    actor = 900_020
    workspace_id = await _workspace(app, actor, kind=kind, workspace_id=700_020)
    app.state.settings = app.state.settings.model_copy(
        update={"quota_bytes_per_user": 5000},
    )
    if used_bytes is not None:
        async with app.state.session_factory() as session:
            session.add(WorkspaceStorage(
                workspace_id=workspace_id, bytes_used=used_bytes,
                quota_limit_bytes=2000,
            ))
            await session.commit()
    headers = _headers(auth_headers, actor, workspace_id if kind == "team" else None)
    created = await client.post("/api/datasets", headers=headers, json={
        "name": f"test-quota-detail-{uuid4().hex}",
    })
    assert created.status_code == 201, created.text

    response = await client.post(
        f"/api/datasets/{created.json()['id']}/upload-batches/preflight",
        headers=headers,
        json={"total_size": required_bytes, "largest_file_size": required_bytes,
              "file_count": 1, "expected_extracted_size": required_bytes},
    )

    assert response.status_code == 413, response.text
    assert response.json() == {"detail": detail}
    async with app.state.session_factory() as session:
        storage = await session.get(WorkspaceStorage, workspace_id)
        if used_bytes is None:
            assert storage is None
        else:
            assert storage is not None and storage.bytes_used == used_bytes
