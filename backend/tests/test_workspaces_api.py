"""Workspace lifecycle contracts, using the isolated project test database."""

from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Dataset, Project, TrainingRun, Workspace, WorkspaceInvite, WorkspaceMember, WorkspaceStorage,
)
from app.services.admin import AuthDirectoryUser


pytestmark = pytest.mark.asyncio
USERS = {"owner": 920_101, "admin": 920_102, "editor": 920_103, "viewer": 920_104}


def _name(suffix="team"):
    return f"test-workspace-{suffix}-{uuid4().hex}"


async def _create(client, auth_headers, *, user_id=USERS["owner"], name=None, header=None):
    headers = auth_headers(user_id)
    if header is not None:
        headers["X-Workspace-Id"] = header
    response = await client.post("/api/workspaces", headers=headers, json={"name": name or _name()})
    assert response.status_code == 201, response.text
    return response.json()


async def _team(app):
    async with app.state.session_factory() as session:
        team = Workspace(kind="team", name=_name(), created_by=USERS["owner"])
        session.add(team)
        await session.flush()
        session.add_all(WorkspaceMember(workspace_id=team.id, user_id=user, role=role)
                        for role, user in USERS.items())
        session.add(WorkspaceStorage(workspace_id=team.id, bytes_used=0))
        await session.commit()
        return team.id


async def test_list_lazily_creates_personal_first_and_includes_memberships_only(client, app, auth_headers):
    team_id = await _team(app)
    user_id = USERS["editor"]
    response = await client.get("/api/workspaces", headers=auth_headers(user_id))
    assert response.status_code == 200, response.text
    rows = response.json()["items"]
    assert len(rows) == 2
    assert rows[0]["kind"] == "personal" and rows[0]["role"] == "owner"
    assert rows[1]["id"] == team_id and rows[1]["role"] == "editor"
    again = await client.get("/api/workspaces", headers=auth_headers(user_id))
    assert again.json() == response.json()
    async with app.state.session_factory() as session:
        personal = await session.get(Workspace, rows[0]["id"])
        assert personal.created_by == user_id
        membership = await session.get(WorkspaceMember, (personal.id, user_id))
        assert membership.role == "owner"


async def test_team_creation_initializes_owner_and_free_quota_and_default_limit(client, app, auth_headers):
    assert app.state.settings.team_create_limit == 2
    first = await _create(client, auth_headers)
    second = await _create(client, auth_headers)
    assert first["kind"] == second["kind"] == "team"
    assert first["role"] == second["role"] == "owner"
    async with app.state.session_factory() as session:
        row = await session.get(Workspace, first["id"])
        assert row.created_by == USERS["owner"]
        owner = await session.get(WorkspaceMember, (row.id, USERS["owner"]))
        storage = await session.get(WorkspaceStorage, row.id)
        assert owner.role == "owner"
        assert storage.bytes_used == 0 and storage.quota_limit_bytes is None
    third = await client.post("/api/workspaces", headers=auth_headers(USERS["owner"]), json={"name": _name()})
    assert third.status_code == 409, third.text
    detail = third.json()["detail"]
    assert detail["code"] == "team_limit_reached" and detail["limit"] == 2


@pytest.mark.parametrize("role,expected", [("viewer", 403), ("editor", 403), ("admin", 200), ("owner", 200)])
async def test_rename_uses_path_scope_and_requires_admin(client, app, auth_headers, role, expected):
    team_id = await _team(app)
    headers = {**auth_headers(USERS[role]), "X-Workspace-Id": "invalid-is-ignored"}
    response = await client.patch(f"/api/workspaces/{team_id}", headers=headers, json={"name": _name("renamed")})
    assert response.status_code == expected, response.text
    if expected == 403:
        assert response.json()["detail"] == {"code": "insufficient_role", "required": "admin", "actual": role}


@pytest.mark.parametrize("name", ["", "x" * 256])
async def test_team_names_reject_invalid_lengths_on_create_and_rename(client, app, auth_headers, name):
    team_id = await _team(app)
    for method, path in (("POST", "/api/workspaces"), ("PATCH", f"/api/workspaces/{team_id}")):
        response = await client.request(method, path, headers=auth_headers(USERS["owner"]), json={"name": name})
        assert response.status_code == 422, response.text


@pytest.mark.parametrize("name", ["a", "가" * 255])
async def test_team_names_accept_boundary_lengths(client, auth_headers, name):
    created = await _create(client, auth_headers, name=name, header="not-a-workspace-id")
    assert created["name"] == name


async def test_workspace_routes_require_authentication(client):
    headers = {"Authorization": "Bearer not-a-token"}
    for method, path, body in (("GET", "/api/workspaces", None),
                               ("POST", "/api/workspaces", {"name": _name()}),
                               ("GET", "/api/workspaces/1/members", None)):
        response = await client.request(method, path, headers=headers, json=body)
        assert response.status_code == 401, response.text


async def test_creation_limit_counts_creator_after_transfer_and_releases_deleted_slot(client, app, auth_headers):
    first = await _create(client, auth_headers)
    second = await _create(client, auth_headers)
    async with app.state.session_factory() as session:
        session.add(WorkspaceMember(workspace_id=first["id"], user_id=USERS["admin"], role="admin"))
        await session.commit()
    transferred = await client.post(f"/api/workspaces/{first['id']}/owner", headers=auth_headers(USERS["owner"]),
                                    json={"user_id": USERS["admin"]})
    assert transferred.status_code == 200, transferred.text
    denied = await client.post("/api/workspaces", headers=auth_headers(USERS["owner"]), json={"name": _name()})
    assert denied.status_code == 409 and denied.json()["detail"]["code"] == "team_limit_reached"
    # Receiving someone else's team does not consume this user's creator cap.
    await _create(client, auth_headers, user_id=USERS["admin"])
    await _create(client, auth_headers, user_id=USERS["admin"])
    deleted = await client.delete(f"/api/workspaces/{second['id']}", headers=auth_headers(USERS["owner"]))
    assert deleted.status_code == 204, deleted.text
    await _create(client, auth_headers)
    async with app.state.session_factory() as session:
        transferred_team = await session.get(Workspace, first["id"])
        assert transferred_team.created_by == USERS["owner"]


async def test_configured_team_limit_is_used(client, app, auth_headers):
    app.state.settings = app.state.settings.model_copy(update={"team_create_limit": 1})
    await _create(client, auth_headers)
    denied = await client.post("/api/workspaces", headers=auth_headers(USERS["owner"]), json={"name": _name()})
    assert denied.status_code == 409, denied.text
    assert denied.json()["detail"]["limit"] == 1


async def test_concurrent_creates_claim_exactly_one_remaining_slot(client, app, auth_headers, monkeypatch):
    await _create(client, auth_headers)
    original = AsyncSession.execute
    both_attempting_lock = asyncio.Event()
    arrivals = 0

    async def synchronize_before_creator_lock(session, statement, *args, **kwargs):
        nonlocal arrivals
        sql = str(statement).upper()
        if "WORKSPACES" in sql and "FOR UPDATE" in sql:
            arrivals += 1
            if arrivals == 2:
                both_attempting_lock.set()
            # Synchronize before acquisition: waiting after the first row lock
            # would prevent the second request from ever reaching the barrier.
            await asyncio.wait_for(both_attempting_lock.wait(), timeout=10)
        return await original(session, statement, *args, **kwargs)

    monkeypatch.setattr(AsyncSession, "execute", synchronize_before_creator_lock)
    async def create():
        return await client.post("/api/workspaces", headers=auth_headers(USERS["owner"]), json={"name": _name()})
    results = await asyncio.wait_for(asyncio.gather(create(), create()), timeout=15)
    assert arrivals == 2
    assert sorted(response.status_code for response in results) == [201, 409], [r.text for r in results]
    loser = next(response for response in results if response.status_code == 409)
    assert loser.json()["detail"]["code"] == "team_limit_reached"
    async with app.state.session_factory() as session:
        count = await session.scalar(select(func.count()).select_from(Workspace).where(
            Workspace.kind == "team", Workspace.created_by == USERS["owner"]))
        assert count == 2


async def test_nonmembers_receive_404_on_every_path_scoped_operation(client, app, auth_headers):
    team_id = await _team(app)
    headers = {**auth_headers(929_999), "X-Workspace-Id": "1"}
    base = f"/api/workspaces/{team_id}"
    requests = [
        ("PATCH", base, {"name": _name()}), ("DELETE", base, None),
        ("POST", f"{base}/owner", {"user_id": USERS["admin"]}),
        ("GET", f"{base}/members", None),
        ("PATCH", f"{base}/members/{USERS['viewer']}", {"role": "editor"}),
        ("DELETE", f"{base}/members/{USERS['viewer']}", None),
        ("DELETE", f"{base}/members/me", None),
    ]
    for method, path, body in requests:
        response = await client.request(method, path, headers=headers, json=body)
        assert response.status_code == 404, (method, path, response.text)


async def test_personal_workspace_cannot_be_renamed_deleted_or_left(client, auth_headers):
    headers = auth_headers(USERS["owner"])
    listing = await client.get("/api/workspaces", headers=headers)
    assert listing.status_code == 200, listing.text
    personal_id = listing.json()["items"][0]["id"]
    for method, suffix, body in (("PATCH", "", {"name": "renamed"}),
                                 ("DELETE", "", None), ("DELETE", "/members/me", None)):
        response = await client.request(method, f"/api/workspaces/{personal_id}{suffix}", headers=headers, json=body)
        assert response.status_code == 409, response.text
        assert response.json()["detail"]["code"] == "personal_workspace_fixed"


@pytest.mark.parametrize("role", ["viewer", "editor", "admin"])
async def test_only_owner_can_delete_or_transfer_workspace(client, app, auth_headers, role):
    team_id = await _team(app)
    headers = auth_headers(USERS[role])
    for method, path, body in (("DELETE", f"/api/workspaces/{team_id}", None),
                               ("POST", f"/api/workspaces/{team_id}/owner", {"user_id": USERS["viewer"]})):
        response = await client.request(method, path, headers=headers, json=body)
        assert response.status_code == 403, response.text
        assert response.json()["detail"] == {"code": "insufficient_role", "required": "owner", "actual": role}


async def test_empty_team_delete_cascades_members_invites_and_storage_only_for_target(client, app, auth_headers):
    team_id = await _team(app)
    other_id = await _team(app)
    async with app.state.session_factory() as session:
        invite = WorkspaceInvite(workspace_id=team_id, role="viewer",
                                  token_hash=hashlib.sha256(uuid4().bytes).hexdigest(),
                                  created_by=USERS["owner"], expires_at=datetime.now(timezone.utc) + timedelta(days=1))
        session.add(invite)
        await session.commit()
        invite_id = invite.id
    removed = await client.delete(f"/api/workspaces/{team_id}",
                                   headers={**auth_headers(USERS["owner"]), "X-Workspace-Id": str(other_id)})
    assert removed.status_code == 204, removed.text
    async with app.state.session_factory() as session:
        assert await session.get(Workspace, team_id) is None
        assert await session.get(WorkspaceStorage, team_id) is None
        assert await session.get(WorkspaceInvite, invite_id) is None
        assert await session.scalar(select(func.count()).select_from(WorkspaceMember).where(
            WorkspaceMember.workspace_id == team_id)) == 0
        assert await session.get(Workspace, other_id) is not None
        assert await session.get(WorkspaceStorage, other_id) is not None
        assert await session.scalar(select(func.count()).select_from(WorkspaceMember).where(
            WorkspaceMember.workspace_id == other_id)) == 4


async def test_nonempty_team_delete_lists_blocking_projects(client, app, auth_headers):
    team_id = await _team(app)
    async with app.state.session_factory() as session:
        projects = [Project(workspace_id=team_id, name=_name("project")) for _ in range(2)]
        session.add_all(projects)
        await session.commit()
        expected = {(project.id, project.name) for project in projects}
    response = await client.delete(f"/api/workspaces/{team_id}", headers=auth_headers(USERS["owner"]))
    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "workspace_not_empty"
    assert {(row["id"], row["name"]) for row in detail["projects"]} == expected
    async with app.state.session_factory() as session:
        assert await session.get(Workspace, team_id) is not None



@pytest.mark.parametrize("with_project", [False, True], ids=["run-only", "project-and-run"])
async def test_team_delete_with_completed_run_reports_blockers_without_cascading(
    client, app, auth_headers, with_project,
):
    team_id = await _team(app)
    async with app.state.session_factory() as session:
        project = dataset = None
        if with_project:
            project = Project(workspace_id=team_id, name=_name("blocking-project"))
            session.add(project)
            await session.flush()
            dataset = Dataset(workspace_id=team_id, project_id=project.id,
                              name=_name("blocking-dataset"), status="ready", storage_path="")
            session.add(dataset)
            await session.flush()
        # The snapshot column is NOT NULL even after the source dataset is gone.
        # The deletion response retains this name after the source is gone.
        run = TrainingRun(
            workspace_id=team_id, created_by=USERS["owner"],
            dataset_id=dataset.id if dataset else None,
            dataset_name=dataset.name if dataset else _name("deleted-dataset-snapshot"),
            weights="yolo26n.pt", epochs=1, imgsz=640, batch=4,
            split_mode="2way", ratios={"train": .8, "valid": .2}, seed=17,
            state="done", out_dir="", artifact_bytes=0,
            started_at=datetime.now(timezone.utc), finished_at=datetime.now(timezone.utc),
        )
        invite = WorkspaceInvite(
            workspace_id=team_id, role="viewer", created_by=USERS["owner"],
            token_hash=hashlib.sha256(uuid4().bytes).hexdigest(),
            expires_at=datetime.now(timezone.utc) + timedelta(days=1),
        )
        session.add_all([run, invite])
        storage = await session.get(WorkspaceStorage, team_id)
        storage.bytes_used = 64
        await session.commit()
        run_id, invite_id = run.id, invite.id
        project_id = project.id if project else None
        dataset_id = dataset.id if dataset else None
        expected_projects = [{"id": project.id, "name": project.name}] if project else []
        expected_runs = [{"id": run.id, "dataset_name": run.dataset_name}]
        original_snapshot = run.dataset_name

    response = await client.delete(f"/api/workspaces/{team_id}", headers=auth_headers(USERS["owner"]))
    assert response.status_code == 409, response.text
    assert response.json()["detail"] == {
        "code": "workspace_not_empty", "projects": expected_projects, "runs": expected_runs,
    }
    async with app.state.session_factory() as session:
        assert await session.get(Workspace, team_id) is not None
        preserved_run = await session.get(TrainingRun, run_id)
        assert preserved_run is not None and preserved_run.state == "done"
        assert preserved_run.dataset_id == dataset_id
        assert preserved_run.dataset_name == original_snapshot
        assert (await session.get(WorkspaceStorage, team_id)).bytes_used == 64
        assert (await session.get(WorkspaceInvite, invite_id)).revoked_at is None
        roles = (await session.execute(select(WorkspaceMember.user_id, WorkspaceMember.role).where(
            WorkspaceMember.workspace_id == team_id))).all()
        assert {(row.user_id, row.role) for row in roles} == {(user, role) for role, user in USERS.items()}
        if with_project:
            assert await session.get(Project, project_id) is not None
            assert await session.get(Dataset, dataset_id) is not None
        else:
            # Only explicit deletion of the retained run makes the team empty.
            await session.delete(preserved_run)
            await session.commit()
    if not with_project:
        removed = await client.delete(f"/api/workspaces/{team_id}", headers=auth_headers(USERS["owner"]))
        assert removed.status_code == 204, removed.text
        async with app.state.session_factory() as session:
            assert await session.get(Workspace, team_id) is None
            assert await session.get(WorkspaceStorage, team_id) is None
            assert await session.get(WorkspaceInvite, invite_id) is None
            assert await session.scalar(select(func.count()).select_from(WorkspaceMember).where(
                WorkspaceMember.workspace_id == team_id)) == 0


async def test_owner_transfer_keeps_one_owner_and_demotes_previous_owner_atomically(client, app, auth_headers):
    team_id = await _team(app)
    response = await client.post(f"/api/workspaces/{team_id}/owner", json={"user_id": USERS["editor"]},
                                  headers={**auth_headers(USERS["owner"]), "X-Workspace-Id": "invalid"})
    assert response.status_code == 200, response.text
    assert response.json()["role"] == "admin"
    async with app.state.session_factory() as session:
        members = (await session.scalars(select(WorkspaceMember).where(WorkspaceMember.workspace_id == team_id))).all()
        assert [row.user_id for row in members if row.role == "owner"] == [USERS["editor"]]
        assert next(row.role for row in members if row.user_id == USERS["owner"]) == "admin"
        assert (await session.get(Workspace, team_id)).created_by == USERS["owner"]


async def test_owner_transfer_to_nonmember_is_404_and_preserves_current_owner(client, app, auth_headers):
    team_id = await _team(app)
    response = await client.post(f"/api/workspaces/{team_id}/owner", headers=auth_headers(USERS["owner"]),
                                  json={"user_id": 929_999})
    assert response.status_code == 404, response.text
    async with app.state.session_factory() as session:
        assert (await session.get(WorkspaceMember, (team_id, USERS["owner"]))).role == "owner"


async def test_failed_owner_transfer_commit_rolls_back_both_role_changes(client, app, auth_headers, monkeypatch):
    team_id = await _team(app)
    original = AsyncSession.commit
    async def fail_commit(_session):
        raise RuntimeError("injected workspace transfer commit failure")
    monkeypatch.setattr(AsyncSession, "commit", fail_commit)
    with pytest.raises(RuntimeError, match="injected workspace transfer"):
        await client.post(f"/api/workspaces/{team_id}/owner", headers=auth_headers(USERS["owner"]),
                           json={"user_id": USERS["editor"]})
    monkeypatch.setattr(AsyncSession, "commit", original)
    async with app.state.session_factory() as session:
        assert (await session.get(WorkspaceMember, (team_id, USERS["owner"]))).role == "owner"
        assert (await session.get(WorkspaceMember, (team_id, USERS["editor"]))).role == "editor"


def _directory_rows():
    return [AuthDirectoryUser(id=user_id, email=f"{role}@example.test", username=role,
                              created_at=datetime(2026, 1, 1, tzinfo=timezone.utc), login_methods=("이메일",))
            for role, user_id in {**USERS, "outsider": 929_999}.items()]


@pytest.fixture
def fake_directory(monkeypatch):
    async def load(_auth_database_url):
        return _directory_rows()
    monkeypatch.setattr("app.routers.workspaces.load_auth_directory", load)


@pytest.mark.parametrize("role", USERS)
async def test_every_member_can_read_member_directory_ignoring_selected_header(client, app, auth_headers, fake_directory, role):
    team_id = await _team(app)
    response = await client.get(f"/api/workspaces/{team_id}/members",
                                headers={**auth_headers(USERS[role]), "X-Workspace-Id": "invalid"})
    assert response.status_code == 200, response.text
    members = response.json()["items"]
    assert {row["user_id"] for row in members} == set(USERS.values())
    for row in members:
        expected_role = next(role for role, user_id in USERS.items() if user_id == row["user_id"])
        assert row["role"] == expected_role
        assert row["email"] == f"{expected_role}@example.test"
        assert row["username"] == expected_role
        assert datetime.fromisoformat(row["joined_at"]).tzinfo is not None


async def test_member_directory_outage_is_503(client, app, auth_headers, monkeypatch):
    team_id = await _team(app)
    async def unavailable(_auth_database_url):
        raise RuntimeError("fixture auth directory unavailable")
    monkeypatch.setattr("app.routers.workspaces.load_auth_directory", unavailable)
    response = await client.get(f"/api/workspaces/{team_id}/members", headers=auth_headers(USERS["viewer"]))
    assert response.status_code == 503, response.text


@pytest.mark.parametrize("role", ["viewer", "editor"])
async def test_member_management_requires_admin(client, app, auth_headers, role):
    team_id = await _team(app)
    for method, body in (("PATCH", {"role": "editor"}), ("DELETE", None)):
        response = await client.request(method, f"/api/workspaces/{team_id}/members/{USERS['viewer']}",
                                        headers=auth_headers(USERS[role]), json=body)
        assert response.status_code == 403, response.text
        assert response.json()["detail"] == {"code": "insufficient_role", "required": "admin", "actual": role}


@pytest.mark.parametrize("new_role", ["admin", "editor"])
async def test_admin_can_assign_nonowner_roles(client, app, auth_headers, new_role):
    team_id = await _team(app)
    response = await client.patch(f"/api/workspaces/{team_id}/members/{USERS['viewer']}",
                                  headers=auth_headers(USERS["admin"]), json={"role": new_role})
    assert response.status_code == 200, response.text
    assert response.json()["user_id"] == USERS["viewer"] and response.json()["role"] == new_role
    async with app.state.session_factory() as session:
        assert (await session.get(WorkspaceMember, (team_id, USERS["viewer"]))).role == new_role


@pytest.mark.parametrize("role", ["admin", "owner"])
async def test_owner_role_cannot_be_edited_or_removed_through_member_endpoints(client, app, auth_headers, role):
    team_id = await _team(app)
    for method, body in (("PATCH", {"role": "admin"}), ("DELETE", None)):
        response = await client.request(method, f"/api/workspaces/{team_id}/members/{USERS['owner']}",
                                        headers=auth_headers(USERS[role]), json=body)
        assert response.status_code == 409, response.text
        if method == "PATCH":
            assert response.json()["detail"]["code"] == "owner_role_fixed"


@pytest.mark.parametrize("role", ["owner", "bogus"])
async def test_owner_promotion_requires_transfer_endpoint(client, app, auth_headers, role):
    team_id = await _team(app)
    response = await client.patch(f"/api/workspaces/{team_id}/members/{USERS['viewer']}",
                                  headers=auth_headers(USERS["owner"]), json={"role": role})
    assert response.status_code == 422, response.text


async def test_admin_removes_member_only_from_path_workspace(client, app, auth_headers):
    team_id, other_id = await _team(app), await _team(app)
    response = await client.delete(f"/api/workspaces/{team_id}/members/{USERS['editor']}",
                                   headers={**auth_headers(USERS["admin"]), "X-Workspace-Id": str(other_id)})
    assert response.status_code == 204, response.text
    async with app.state.session_factory() as session:
        assert await session.get(WorkspaceMember, (team_id, USERS["editor"])) is None
        assert (await session.get(WorkspaceMember, (other_id, USERS["editor"]))).role == "editor"


async def test_missing_target_member_remains_404(client, app, auth_headers):
    team_id = await _team(app)
    for method, body in (("PATCH", {"role": "editor"}), ("DELETE", None)):
        response = await client.request(method, f"/api/workspaces/{team_id}/members/929999",
                                        headers=auth_headers(USERS["admin"]), json=body)
        assert response.status_code == 404, response.text


@pytest.mark.parametrize("role", ["admin", "editor", "viewer"])
async def test_nonowner_can_leave_and_loses_workspace_access(client, app, auth_headers, role):
    team_id = await _team(app)
    headers = auth_headers(USERS[role])
    left = await client.delete(f"/api/workspaces/{team_id}/members/me", headers=headers)
    assert left.status_code == 204, left.text
    inaccessible = await client.get(f"/api/workspaces/{team_id}/members", headers=headers)
    assert inaccessible.status_code == 404, inaccessible.text
    async with app.state.session_factory() as session:
        assert await session.get(WorkspaceMember, (team_id, USERS[role])) is None
        assert (await session.get(WorkspaceMember, (team_id, USERS["owner"]))).role == "owner"


async def test_owner_must_transfer_before_leaving(client, app, auth_headers):
    team_id = await _team(app)
    response = await client.delete(f"/api/workspaces/{team_id}/members/me", headers=auth_headers(USERS["owner"]))
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "owner_must_transfer"


@pytest.mark.parametrize("user_id", [0, -1, -(2 ** 31), 2 ** 31, 10 ** 100, -(10 ** 100)])
@pytest.mark.parametrize("method", ["PATCH", "DELETE"])
async def test_out_of_range_member_id_returns_korean_404(
    client, app, auth_headers, user_id, method,
):
    team_id = await _team(app)
    response = await client.request(
        method, f"/api/workspaces/{team_id}/members/{user_id}",
        headers=auth_headers(USERS["admin"]),
        **({"json": {"role": "editor"}} if method == "PATCH" else {}),
    )
    assert response.status_code == 404, response.text
    assert response.json()["detail"] == "멤버를 찾을 수 없습니다."
    async with app.state.session_factory() as session:
        rows = (await session.execute(select(WorkspaceMember.user_id, WorkspaceMember.role).where(
            WorkspaceMember.workspace_id == team_id))).all()
        assert {(row.user_id, row.role) for row in rows} == {(user, role) for role, user in USERS.items()}


@pytest.mark.parametrize("actor", ["admin", "owner"])
@pytest.mark.parametrize("target_role", ["admin", "editor", "viewer"])
async def test_viewer_role_change_is_rejected_without_membership_or_invite_changes(
    client, app, auth_headers, actor, target_role,
):
    team_id = await _team(app)
    async with app.state.session_factory() as session:
        session.add(WorkspaceInvite(
            workspace_id=team_id, role="editor", created_by=USERS["admin"],
            token_hash=uuid4().hex * 2, expires_at=datetime.now(timezone.utc) + timedelta(days=7),
        ))
        await session.commit()

    async def snapshot():
        async with app.state.session_factory() as session:
            members = (await session.execute(select(*WorkspaceMember.__table__.c).where(
                WorkspaceMember.workspace_id == team_id,
            ).order_by(WorkspaceMember.user_id))).all()
            invites = (await session.execute(select(*WorkspaceInvite.__table__.c).where(
                WorkspaceInvite.workspace_id == team_id,
            ).order_by(WorkspaceInvite.id))).all()
            return members, invites

    before = await snapshot()
    response = await client.patch(
        f"/api/workspaces/{team_id}/members/{USERS[target_role]}",
        headers=auth_headers(USERS[actor]), json={"role": "viewer"},
    )
    assert response.status_code == 422, response.text
    assert any(error["loc"] == ["body", "role"] for error in response.json()["detail"])
    assert await snapshot() == before
