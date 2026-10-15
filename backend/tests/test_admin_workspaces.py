"""Platform-admin workspace reporting and workspace quota contracts."""

from __future__ import annotations

from collections import namedtuple
from contextlib import contextmanager
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from sqlalchemy import delete, event, func, select

from app.models import (
    AdminUser,
    Dataset,
    Project,
    UploadSession,
    Workspace,
    WorkspaceMember,
    WorkspaceStorage,
)
from app.services.admin import AuthDirectoryUser

pytestmark = pytest.mark.asyncio
DEFAULT_LIMIT = 10_000
USERS = (23_001, 23_002, 23_003)


@pytest.fixture(autouse=True)
def legacy_personal_workspaces():
    """These fixtures allocate workspace ids independently of auth ids."""


def _directory_user(user_id: int) -> AuthDirectoryUser:
    return AuthDirectoryUser(
        id=user_id,
        email=f"admin-workspaces-{user_id}@test.local",
        username=f"user-{user_id}",
        created_at=datetime(2026, 10, 1, tzinfo=timezone.utc),
        login_methods=("이메일",),
    )


@pytest.fixture
def directory(monkeypatch):
    state = {"users": [_directory_user(i) for i in (1, *USERS)], "calls": 0}

    async def load(_url):
        state["calls"] += 1
        return list(state["users"])

    monkeypatch.setattr("app.routers.admin.load_auth_directory", load)
    return state


@pytest_asyncio.fixture
async def workspace_factory(app, cleanup_test_datasets):
    """Only remove workspaces and grants created by this fixture."""
    workspace_ids = []
    app.state.settings = app.state.settings.model_copy(
        update={"quota_bytes_per_user": DEFAULT_LIMIT},
    )
    async with app.state.session_factory() as session:
        session.add(AdminUser(user_id=1))
        await session.commit()

    async def create(
        *, kind="team", name=None, creator=USERS[0], members=None,
        used=0, override=None, storage=True,
    ):
        async with app.state.session_factory() as session:
            # Backfilled fixture ids need not match the sequence state.
            workspace_id = await session.scalar(
                select(func.coalesce(func.max(Workspace.id), 0) + 1),
            )
            workspace = Workspace(
                id=workspace_id, kind=kind,
                name=name or f"test-admin-workspace-{workspace_id}",
                created_by=creator,
            )
            session.add(workspace)
            await session.flush()
            workspace_ids.append(workspace_id)
            memberships = [(creator, "owner")] if members is None else members
            session.add_all([
                WorkspaceMember(workspace_id=workspace_id, user_id=user_id, role=role)
                for user_id, role in memberships
            ])
            if storage:
                session.add(WorkspaceStorage(
                    workspace_id=workspace_id, bytes_used=used,
                    quota_limit_bytes=override,
                ))
            await session.commit()
            return workspace_id

    yield create

    async with app.state.session_factory() as session:
        await session.execute(delete(Dataset).where(Dataset.workspace_id.in_(workspace_ids)))
        await session.execute(delete(Project).where(Project.workspace_id.in_(workspace_ids)))
        await session.execute(delete(Workspace).where(Workspace.id.in_(workspace_ids)))
        await session.execute(delete(AdminUser).where(AdminUser.user_id == 1))
        await session.commit()


@pytest_asyncio.fixture
async def sample(workspace_factory, directory):
    personal_one = await workspace_factory(
        kind="personal", creator=USERS[0], used=500, override=9_000,
    )
    personal_two = await workspace_factory(kind="personal", creator=USERS[1], used=1_500)
    personal_three = await workspace_factory(
        kind="personal", creator=USERS[2], storage=False,
    )
    team = await workspace_factory(
        name="test-admin-workspace-shared", creator=USERS[0], used=3_000,
        override=5_000,
        members=[(USERS[1], "owner"), (USERS[0], "admin"), (USERS[2], "viewer"),
                 (23_099, "editor")],
    )
    orphan = await workspace_factory(creator=23_999, used=7_000)
    unused = await workspace_factory(creator=USERS[0], storage=False)
    return {
        "personal_one": personal_one, "personal_two": personal_two,
        "personal_three": personal_three, "team": team, "orphan": orphan,
        "unused": unused,
    }


async def _storage_snapshot(app):
    async with app.state.session_factory() as session:
        return list((await session.execute(
            select(WorkspaceStorage.workspace_id, WorkspaceStorage.bytes_used,
                   WorkspaceStorage.quota_limit_bytes).order_by(WorkspaceStorage.workspace_id),
        )).all())


async def test_users_include_memberships_without_multiplying_personal_usage(
    app, client, sample, directory,
):
    response = await client.get("/api/admin/users")
    assert response.status_code == 200, response.text
    rows = response.json()["users"]
    assert [row["id"] for row in rows] == [USERS[1], USERS[0], 1, USERS[2]]
    assert [row["bytes_used"] for row in rows] == [1_500, 500, 0, 0]
    assert [row["limit_bytes"] for row in rows] == [DEFAULT_LIMIT, 9_000, DEFAULT_LIMIT, DEFAULT_LIMIT]
    by_user = {row["id"]: row for row in rows}
    assert by_user[1]["workspaces"] == []
    memberships = {row["id"]: row for row in by_user[USERS[0]]["workspaces"]}
    assert set(memberships) == {sample["personal_one"], sample["team"], sample["unused"]}
    assert memberships[sample["team"]] == {
        "id": sample["team"], "kind": "team", "name": "test-admin-workspace-shared",
        "role": "admin", "bytes_used": 3_000, "limit_bytes": 5_000,
        "quota_override": True,
    }
    assert memberships[sample["personal_one"]]["role"] == "owner"
    assert memberships[sample["personal_one"]]["bytes_used"] == 500
    assert memberships[sample["unused"]]["bytes_used"] == 0
    assert memberships[sample["unused"]]["limit_bytes"] == DEFAULT_LIMIT
    assert memberships[sample["unused"]]["quota_override"] is False
    for user_id, role in ((USERS[1], "owner"), (USERS[2], "viewer")):
        team = next(row for row in by_user[user_id]["workspaces"] if row["id"] == sample["team"])
        assert team["role"] == role
        assert team["bytes_used"] == 3_000
    assert directory["calls"] == 1
    # Reporting must not lazily create missing storage rows.
    async with app.state.session_factory() as session:
        assert await session.get(WorkspaceStorage, sample["unused"]) is None


async def test_workspaces_report_all_scopes_once_and_owner_from_membership(
    client, sample, directory,
):
    response = await client.get("/api/admin/workspaces")
    assert response.status_code == 200, response.text
    rows = response.json()["workspaces"]
    assert [row["id"] for row in rows] == [
        sample["orphan"], sample["team"], sample["personal_two"],
        sample["personal_one"], sample["personal_three"], sample["unused"],
    ]
    assert sum(row["bytes_used"] for row in rows) == 12_000
    team = next(row for row in rows if row["id"] == sample["team"])
    assert team == {
        "id": sample["team"], "kind": "team", "name": "test-admin-workspace-shared",
        "owner_user_id": USERS[1], "member_count": 4,
        "bytes_used": 3_000, "limit_bytes": 5_000, "quota_override": True,
    }
    for row in rows:
        if row["id"] in (sample["personal_three"], sample["unused"]):
            assert (row["bytes_used"], row["limit_bytes"], row["quota_override"]) == (0, DEFAULT_LIMIT, False)
            assert row["member_count"] == 1
    assert directory["calls"] == 1


async def test_overview_sums_team_and_directory_orphan_storage(client, sample):
    response = await client.get("/api/admin/overview")
    assert response.status_code == 200, response.text
    assert response.json() == {"user_count": 4, "storage_total_bytes": 12_000}


@pytest.mark.parametrize("key", ["team", "personal_one", "unused", "orphan"])
async def test_workspace_override_changes_only_requested_scope(
    app, client, sample, key,
):
    before = dict((row[0], (row[1], row[2])) for row in await _storage_snapshot(app))
    target = sample[key]
    response = await client.patch(f"/api/admin/workspaces/{target}/quota", json={"limit_bytes": 8_000})
    assert response.status_code == 200, response.text
    assert response.json() == {"workspace_id": target, "limit_bytes": 8_000}
    after = dict((row[0], (row[1], row[2])) for row in await _storage_snapshot(app))
    assert after[target] == (before.get(target, (0, None))[0], 8_000)
    assert {key: value for key, value in after.items() if key != target} == {
        key: value for key, value in before.items() if key != target
    }


async def test_legacy_user_override_remains_personal_only(app, client, sample):
    response = await client.patch(
        f"/api/admin/users/{USERS[0]}/quota", json={"limit_bytes": 12_345},
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"user_id": USERS[0], "limit_bytes": 12_345}
    async with app.state.session_factory() as session:
        personal = await session.get(WorkspaceStorage, sample["personal_one"])
        team = await session.get(WorkspaceStorage, sample["team"])
        assert (personal.bytes_used, personal.quota_limit_bytes) == (500, 12_345)
        assert (team.bytes_used, team.quota_limit_bytes) == (3_000, 5_000)


@pytest.mark.parametrize("role", ["owner", "admin", "editor", "viewer"])
async def test_workspace_role_does_not_grant_platform_admin_access(
    app, client, workspace_factory, directory, auth_headers, role,
):
    actor = USERS[0]
    members = [(actor, role)]
    if role != "owner":
        members.append((USERS[1], "owner"))
    target = await workspace_factory(members=members, used=321, override=777)
    before = await _storage_snapshot(app)
    headers = {**auth_headers(actor), "X-Workspace-Id": str(target)}
    for method, path, body in (
        ("GET", "/api/admin/workspaces", None),
        ("GET", "/api/admin/users", None),
        ("GET", "/api/admin/unknown", None),
        ("PATCH", f"/api/admin/workspaces/{target}/quota", {"limit_bytes": 999}),
        ("PATCH", f"/api/admin/workspaces/{target}/quota", {}),
        ("PATCH", "/api/admin/workspaces/2147483647/quota", {"limit_bytes": 999}),
    ):
        response = await client.request(method, path, headers=headers, json=body)
        assert response.status_code == 404, response.text
        assert response.json() == {"detail": "Not Found"}
    assert directory["calls"] == 0
    assert await _storage_snapshot(app) == before


@pytest.mark.parametrize("limit", [0, -1, 2**63, True, "3000", 1.5, None])
async def test_invalid_workspace_override_is_rejected_without_write(
    app, client, sample, limit,
):
    before = await _storage_snapshot(app)
    response = await client.patch(
        f"/api/admin/workspaces/{sample['team']}/quota", json={"limit_bytes": limit},
    )
    assert response.status_code == 422, response.text
    assert await _storage_snapshot(app) == before


async def test_unknown_workspace_override_is_404_without_write(app, client, sample):
    before = await _storage_snapshot(app)
    response = await client.patch(
        "/api/admin/workspaces/2147483647/quota", json={"limit_bytes": 1_000},
    )
    assert response.status_code == 404, response.text
    assert response.json() == {"detail": "Not Found"}
    assert await _storage_snapshot(app) == before


@pytest.mark.parametrize("path", ["/api/admin/users", "/api/admin/workspaces", "/api/admin/overview"])
async def test_directory_outage_remains_503(client, sample, monkeypatch, path):
    async def unavailable(_url):
        raise RuntimeError("auth directory unavailable")

    monkeypatch.setattr("app.routers.admin.load_auth_directory", unavailable)
    response = await client.get(path)
    assert response.status_code == 503, response.text


async def test_workspace_override_directory_outage_is_503_without_write(
    app, client, sample, monkeypatch,
):
    async def unavailable(_url):
        raise RuntimeError("auth directory unavailable")

    monkeypatch.setattr("app.routers.admin.load_auth_directory", unavailable)
    before = await _storage_snapshot(app)
    response = await client.patch(
        f"/api/admin/workspaces/{sample['team']}/quota", json={"limit_bytes": 1_000},
    )
    assert response.status_code == 503, response.text
    assert await _storage_snapshot(app) == before


@contextmanager
def _count_selects(app):
    statements = []

    def before_cursor_execute(_conn, _cursor, statement, _parameters, _context, _many):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement.lower())

    engine = app.state.engine.sync_engine
    event.listen(engine, "before_cursor_execute", before_cursor_execute)
    try:
        yield statements
    finally:
        event.remove(engine, "before_cursor_execute", before_cursor_execute)


@pytest.mark.parametrize("path", ["/api/admin/users", "/api/admin/workspaces"])
async def test_reporting_query_count_is_constant_as_members_and_users_double(
    app, client, workspace_factory, directory, path,
):
    directory["users"] = [_directory_user(i) for i in USERS]
    for user_id in USERS:
        await workspace_factory(kind="personal", creator=user_id, used=user_id)
    await workspace_factory(members=[(USERS[0], "owner"), (USERS[1], "admin"), (USERS[2], "viewer")])

    counts = []
    for round_index in range(2):
        directory["calls"] = 0
        with _count_selects(app) as statements:
            response = await client.get(path)
        assert response.status_code == 200, response.text
        assert directory["calls"] == 1
        joined = [sql for sql in statements if all(
            table in sql for table in ("workspace_members", "workspaces", "workspace_storage")
        )]
        assert len(joined) == 1, statements
        counts.append(len(statements))
        response_key = "users" if path.endswith("users") else "workspaces"
        expected_count = (3 if round_index == 0 else 6)
        if response_key == "workspaces":
            expected_count += 1  # The shared team has several members but one row.
        assert len(response.json()[response_key]) == expected_count
        if round_index == 0:
            for user_id in (23_004, 23_005, 23_006):
                directory["users"].append(_directory_user(user_id))
                await workspace_factory(kind="personal", creator=user_id, used=user_id)
    assert counts[0] == counts[1], counts
    # The platform-admin grant check plus one joined reporting query.
    assert counts[0] == 2, counts


async def test_workspace_override_controls_real_upload_preflight(
    app, client, sample, auth_headers, monkeypatch,
):
    target = sample["team"]
    async with app.state.session_factory() as session:
        project = Project(workspace_id=target, name="test-admin-workspace-upload")
        session.add(project)
        await session.flush()
        dataset = Dataset(
            workspace_id=target, project_id=project.id,
            name="test-admin-workspace-upload", status="ready",
            storage_path="datasets/test-admin-workspace-upload",
        )
        session.add(dataset)
        await session.commit()
        dataset_id = dataset.id

    disk_usage = namedtuple("DiskUsage", "total used free")
    monkeypatch.setattr("app.services.validate.shutil.disk_usage", lambda _path: disk_usage(10**15, 0, 10**15))
    headers = {**auth_headers(USERS[0]), "X-Workspace-Id": str(target)}
    path = f"/api/datasets/{dataset_id}/upload-batches/preflight"
    body = {"total_size": 200, "largest_file_size": 100, "file_count": 2,
            "expected_extracted_size": 250}
    before = await client.post(path, headers=headers, json=body)
    assert before.status_code == 204, before.text

    changed = await client.patch(
        f"/api/admin/workspaces/{target}/quota", json={"limit_bytes": 3_000},
    )
    assert changed.status_code == 200, changed.text
    rejected = await client.post(path, headers=headers, json=body)
    assert rejected.status_code == 413, rejected.text
    assert "워크스페이스" in rejected.json()["detail"]
    assert "잔여 용량 0 B" in rejected.json()["detail"]
    async with app.state.session_factory() as session:
        storage = await session.get(WorkspaceStorage, target)
        assert storage.bytes_used == 3_000
        assert await session.scalar(select(UploadSession.id).where(UploadSession.dataset_id == dataset_id)) is None
