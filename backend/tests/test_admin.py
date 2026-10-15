from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import delete, select

from app.models import (
    AdminUser,
    Dataset,
    Image,
    MediaObject,
    Project,
    TrainingRun,
    Workspace,
    WorkspaceMember,
    WorkspaceStorage,
)
from app.services.admin import AuthDirectoryUser
from tests.factories import image_with_media

pytestmark = pytest.mark.asyncio


BASE_TIME = datetime(2026, 8, 1, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def legacy_personal_workspaces():
    """These tests deliberately use different workspace and user ids."""


async def _personal_workspace(session, user_id: int) -> Workspace:
    workspace = await session.scalar(
        select(Workspace)
        .join(WorkspaceMember, WorkspaceMember.workspace_id == Workspace.id)
        .where(Workspace.kind == "personal", WorkspaceMember.user_id == user_id)
    )
    if workspace is None:
        # Deliberately keep workspace ids distinct from auth user ids.
        workspace = Workspace(
            id=10_000 + user_id, kind="personal", name="개인", created_by=user_id,
        )
        session.add(workspace)
        await session.flush()
        session.add(WorkspaceMember(
            workspace_id=workspace.id, user_id=user_id, role="owner",
        ))
        await session.flush()
    return workspace


def _directory() -> list[AuthDirectoryUser]:
    # Newest-first, matching the real query's ORDER BY created_at DESC.
    return [
        AuthDirectoryUser(
            id=3,
            email="new@test.local",
            username="new",
            created_at=BASE_TIME + timedelta(days=2),
            login_methods=("google",),
        ),
        AuthDirectoryUser(
            id=2,
            email="mid@test.local",
            username="mid",
            created_at=BASE_TIME + timedelta(days=1),
            login_methods=("이메일", "kakao"),
        ),
        AuthDirectoryUser(
            id=1,
            email="admin@test.local",
            username="admin",
            created_at=BASE_TIME,
            login_methods=("이메일",),
        ),
    ]


async def _seed(
    app,
    *,
    admin_ids: list[int],
    usage: dict[int, int],
    quota_limits: dict[int, int] | None = None,
) -> None:
    quota_limits = quota_limits or {}
    async with app.state.session_factory() as session:
        for user_id in admin_ids:
            session.add(AdminUser(user_id=user_id))
        for user_id in usage.keys() | quota_limits.keys():
            workspace = await _personal_workspace(session, user_id)
            session.add(
                WorkspaceStorage(
                    workspace_id=workspace.id,
                    bytes_used=usage.get(user_id, 0),
                    quota_limit_bytes=quota_limits.get(user_id),
                )
            )
        await session.commit()


async def _cleanup(app) -> None:
    async with app.state.session_factory() as session:
        await session.execute(delete(AdminUser))
        await session.execute(
            delete(Workspace).where(
                Workspace.kind == "personal", Workspace.created_by.in_([1, 2, 3, 999]),
            )
        )
        await session.commit()


@pytest_asyncio.fixture(autouse=True)
async def _admin_cleanup(app):
    await _cleanup(app)
    yield
    await _cleanup(app)


@pytest.fixture
def fake_directory(monkeypatch):
    async def load(_auth_database_url):
        return _directory()

    monkeypatch.setattr("app.routers.admin.load_auth_directory", load)


async def test_non_admin_gets_bare_404(client: httpx.AsyncClient, fake_directory):
    # Identical body to Starlette's genuine no-route 404 — one request to a
    # real admin path and one to a fake path must be indistinguishable.
    for path in ("/api/admin/overview", "/api/admin/users", "/api/admin/zzz"):
        response = await client.get(path)
        assert response.status_code == 404
        assert response.json() == {"detail": "Not Found"}


async def test_method_mismatch_does_not_reveal_routes(
    client: httpx.AsyncClient, fake_directory
):
    # Without the catch-all, POST to an existing admin route returns 405 and
    # betrays the router. Both real and fake paths must give the same 404.
    for path in ("/api/admin/overview", "/api/admin/nope"):
        response = await client.post(path)
        assert response.status_code == 404
        assert response.json() == {"detail": "Not Found"}


async def test_admin_gets_404_on_unknown_admin_path(
    app, client: httpx.AsyncClient, fake_directory
):
    await _seed(app, admin_ids=[1], usage={})
    response = await client.get("/api/admin/zzz")
    assert response.status_code == 404
    assert response.json() == {"detail": "Not Found"}


async def test_unauthenticated_is_rejected(client: httpx.AsyncClient):
    response = await client.get(
        "/api/admin/overview",
        headers={"Authorization": "Bearer not-a-token"},
    )
    assert response.status_code == 401


async def test_admin_overview_counts_and_recent(
    app, client: httpx.AsyncClient, fake_directory
):
    await _seed(app, admin_ids=[1], usage={2: 500, 3: 1500})

    # Workspace accounting includes storage whose auth account no longer
    # appears in the directory, so the platform total still reflects it.
    async with app.state.session_factory() as session:
        workspace = await _personal_workspace(session, 999)
        session.add(WorkspaceStorage(workspace_id=workspace.id, bytes_used=77_000))
        await session.commit()
    try:
        response = await client.get("/api/admin/overview")
        assert response.status_code == 200
        body = response.json()
        assert body["user_count"] == 3
        assert body["storage_total_bytes"] == 79_000
        assert "recent_users" not in body
    finally:
        from sqlalchemy import delete

        async with app.state.session_factory() as session:
            await session.execute(
                delete(Workspace).where(Workspace.id == workspace.id)
            )
            await session.commit()


async def test_admin_users_sorted_by_usage_desc(
    app, client: httpx.AsyncClient, fake_directory
):
    default_limit = 5_000
    app.state.settings = app.state.settings.model_copy(
        update={"quota_bytes_per_user": default_limit}
    )
    await _seed(
        app,
        admin_ids=[1],
        usage={2: 500, 3: 1500},
        quota_limits={3: 9_000},
    )

    response = await client.get("/api/admin/users")
    assert response.status_code == 200
    users = response.json()["users"]
    assert [row["id"] for row in users] == [3, 2, 1]
    assert [row["bytes_used"] for row in users] == [1500, 500, 0]
    assert [row["limit_bytes"] for row in users] == [9_000, 5_000, 5_000]
    assert users[0]["email"] == "new@test.local"
    assert users[0]["login_methods"] == ["google"]
    assert users[1]["login_methods"] == ["이메일", "kakao"]
    assert users[2]["login_methods"] == ["이메일"]


async def test_admin_can_change_one_users_quota_without_changing_usage(
    app, client: httpx.AsyncClient, fake_directory
):
    await _seed(app, admin_ids=[1], usage={3: 1500})

    response = await client.patch(
        "/api/admin/users/3/quota",
        json={"limit_bytes": 8 * 1024**3},
    )

    assert response.status_code == 200, response.text
    assert response.json() == {
        "user_id": 3,
        "limit_bytes": 8 * 1024**3,
    }
    async with app.state.session_factory() as session:
        workspace = await _personal_workspace(session, 3)
        assert workspace.id != 3
        storage = await session.get(WorkspaceStorage, workspace.id)
        assert storage is not None
        assert storage.bytes_used == 1500
        assert storage.quota_limit_bytes == 8 * 1024**3


async def test_admin_quota_update_creates_storage_row_for_unused_user(
    app, client: httpx.AsyncClient, fake_directory
):
    await _seed(app, admin_ids=[1], usage={})

    async with app.state.session_factory() as session:
        assert await session.scalar(select(Workspace.id).where(
            Workspace.kind == "personal", Workspace.created_by == 2,
        )) is None

    response = await client.patch(
        "/api/admin/users/2/quota",
        json={"limit_bytes": 3 * 1024**3},
    )

    assert response.status_code == 200, response.text
    async with app.state.session_factory() as session:
        workspace = await session.scalar(
            select(Workspace)
            .join(WorkspaceMember, WorkspaceMember.workspace_id == Workspace.id)
            .where(Workspace.kind == "personal", WorkspaceMember.user_id == 2)
        )
        assert workspace is not None
        member = await session.get(WorkspaceMember, (workspace.id, 2))
        assert member.role == "owner"
        storage = await session.get(WorkspaceStorage, workspace.id)
        assert storage is not None
        assert storage.bytes_used == 0
        assert storage.quota_limit_bytes == 3 * 1024**3


async def test_non_admin_quota_update_is_indistinguishable_from_unknown_route(
    client: httpx.AsyncClient, fake_directory
):
    for path, body in (
        ("/api/admin/users/3/quota", {"limit_bytes": 1024**3}),
        ("/api/admin/does-not-exist", {}),
        ("/api/admin/users/3/quota", {}),
    ):
        response = await client.patch(path, json=body)
        assert response.status_code == 404
        assert response.json() == {"detail": "Not Found"}


async def test_admin_quota_update_rejects_unknown_user_and_nonpositive_limit(
    app, client: httpx.AsyncClient, fake_directory
):
    await _seed(app, admin_ids=[1], usage={})

    unknown = await client.patch(
        "/api/admin/users/99/quota",
        json={"limit_bytes": 1024**3},
    )
    assert unknown.status_code == 404
    assert unknown.json() == {"detail": "Not Found"}

    invalid = await client.patch(
        "/api/admin/users/3/quota",
        json={"limit_bytes": 0},
    )
    assert invalid.status_code == 422


async def test_directory_outage_is_503_not_empty(
    app, client: httpx.AsyncClient, monkeypatch
):
    await _seed(app, admin_ids=[1], usage={})

    async def broken(_auth_database_url):
        raise RuntimeError("AUTH_DATABASE_URL is not configured")

    monkeypatch.setattr("app.routers.admin.load_auth_directory", broken)
    response = await client.get("/api/admin/overview")
    assert response.status_code == 503


async def test_real_db_outage_becomes_503(
    app, client: httpx.AsyncClient, monkeypatch
):
    # An OperationalError from the auth engine must surface as the documented
    # 503, not fall through to the DBAPIError handler as an opaque 500.
    from sqlalchemy.exc import OperationalError

    from app.services import admin as admin_service

    await _seed(app, admin_ids=[1], usage={})

    async def broken(_engine):
        raise OperationalError("SELECT 1", {}, Exception("connection refused"))

    monkeypatch.setattr(admin_service, "_query_directory", broken)
    response = await client.get("/api/admin/overview")
    assert response.status_code == 503

    # asyncpg can surface a dead host as a raw OS error (not wrapped in
    # SQLAlchemyError) — that path must also be the documented 503.
    async def refused(_engine):
        raise ConnectionRefusedError("connect call failed")

    monkeypatch.setattr(admin_service, "_query_directory", refused)
    response = await client.get("/api/admin/overview")
    assert response.status_code == 503


def test_login_methods_labels():
    from app.services.admin import _login_methods

    assert _login_methods(True, ["local"]) == ("이메일",)
    assert _login_methods(False, ["naver", "naver", "local"]) == ("이메일", "naver")
    assert _login_methods(False, ["kakao"]) == ("kakao",)
    assert _login_methods(False, None) == ()
    assert _login_methods(True, ["google", "kakao"]) == ("이메일", "google", "kakao")


async def _seed_user_data(app, user_id: int) -> tuple[int, int]:
    """Create one personal project and dataset; return their ids."""

    async with app.state.session_factory() as session:
        workspace = await _personal_workspace(session, user_id)
        project = Project(workspace_id=workspace.id, name=f"test-admin-proj-{user_id}")
        session.add(project)
        await session.flush()
        dataset = Dataset(
            workspace_id=workspace.id,
            project_id=project.id,
            name=f"test-admin-ds-{user_id}",
            status="ready",
            storage_path=f"datasets/test-admin-delete-{user_id}",
        )
        session.add(dataset)
        await session.commit()
        return project.id, dataset.id


async def _cleanup_user_data(app, user_id: int) -> None:
    async with app.state.session_factory() as session:
        workspace_ids = select(Workspace.id).where(
            Workspace.kind == "personal", Workspace.created_by == user_id,
        )
        await session.execute(
            delete(TrainingRun).where(TrainingRun.workspace_id.in_(workspace_ids))
        )
        await session.execute(
            delete(Dataset).where(Dataset.workspace_id.in_(workspace_ids))
        )
        await session.execute(
            delete(MediaObject).where(MediaObject.workspace_id.in_(workspace_ids))
        )
        await session.execute(
            delete(Project).where(Project.workspace_id.in_(workspace_ids))
        )
        await session.commit()


@pytest.fixture
def fake_auth_delete(monkeypatch):
    calls: list[int] = []

    async def delete_account(_auth_database_url, user_id: int) -> None:
        calls.append(user_id)

    monkeypatch.setattr(
        "app.routers.admin.delete_auth_account", delete_account
    )
    return calls


async def test_delete_user_requires_confirmation(
    app, client: httpx.AsyncClient, fake_directory, fake_auth_delete
):
    await _seed(app, admin_ids=[1], usage={3: 1500})
    await _seed_user_data(app, 3)
    try:
        response = await client.delete("/api/admin/users/3")
        assert response.status_code == 409
        detail = response.json()["detail"]
        assert detail["code"] == "admin-user-delete-confirmation-required"
        assert detail["requires_confirmation"] is True
        assert detail["email"] == "new@test.local"
        assert detail["project_count"] == 1
        assert detail["dataset_count"] == 1
        assert detail["bytes_used"] == 1500
        assert fake_auth_delete == []
    finally:
        await _cleanup_user_data(app, 3)


async def test_delete_user_cascades_data_and_auth_account(
    app, client: httpx.AsyncClient, fake_directory, fake_auth_delete
):
    from sqlalchemy import select as sa_select

    from app.models import Dataset, Project

    await _seed(app, admin_ids=[1], usage={3: 1500})
    project_id, dataset_id = await _seed_user_data(app, 3)
    try:
        response = await client.delete("/api/admin/users/3?confirm=true")
        assert response.status_code == 204

        async with app.state.session_factory() as session:
            assert await session.scalar(
                sa_select(Project.id).where(Project.id == project_id)
            ) is None
            assert await session.scalar(
                sa_select(Dataset.id).where(Dataset.id == dataset_id)
            ) is None
            assert await session.scalar(
                sa_select(Workspace.id).where(
                    Workspace.kind == "personal", Workspace.created_by == 3,
                )
            ) is None
        assert fake_auth_delete == [3]
    finally:
        await _cleanup_user_data(app, 3)


async def _seed_media_for_deletion(app, *, referenced: bool, orphan: bool, dataset: bool):
    await _seed(app, admin_ids=[1], usage={3: 0})
    dataset_id = (await _seed_user_data(app, 3))[1] if dataset else None
    payloads = {}
    async with app.state.session_factory() as session:
        workspace = await _personal_workspace(session, 3)
        for label, enabled in (("referenced", referenced), ("orphan", orphan)):
            if not enabled:
                continue
            if dataset_id is not None:
                base = f"datasets/test-admin-delete-3/{label}"
                for kind in ("original", "display", "thumb"):
                    path = app.state.settings.storage_dir / base / f"{kind}.jpg"
                    path.parent.mkdir(parents=True, exist_ok=True)
                    payloads[path] = f"{label}-{kind}".encode()
                    path.write_bytes(payloads[path])
                sizes = {
                    f"{kind}_bytes": len(f"{label}-{kind}".encode())
                    for kind in ("original", "display", "thumb")
                }
            else:
                sizes = {"original_bytes": 31, "display_bytes": 17, "thumb_bytes": 11}
            if label == "referenced":
                session.add(image_with_media(
                    workspace_id=workspace.id, dataset_id=dataset_id,
                    stem=label, filename=f"{label}.jpg", rel_path=f"{label}.jpg",
                    width=4, height=4, file_path=f"{base}/original.jpg",
                    display_path=f"{base}/display.jpg", thumb_path=f"{base}/thumb.jpg",
                    **sizes,
                ))
            else:
                session.add(MediaObject(
                    workspace_id=workspace.id, created_by_dataset_id=dataset_id, **sizes,
                ))
        await session.flush()
        media = list((await session.scalars(
            select(MediaObject).where(MediaObject.workspace_id == workspace.id)
        )).all())
        usage = sum(m.original_bytes + m.display_bytes + m.thumb_bytes for m in media)
        storage = await session.get(WorkspaceStorage, workspace.id)
        storage.bytes_used = usage
        await session.commit()
        return workspace.id, [m.id for m in media], usage, payloads


@pytest.mark.parametrize(
    ("referenced", "orphan", "dataset"),
    [(False, True, False), (False, True, True), (True, False, True), (True, True, True)],
    ids=["orphan-without-dataset", "orphan-files", "referenced-media", "mixed-media"],
)
async def test_delete_user_cleans_personal_media_and_files(
    app, client, fake_directory, fake_auth_delete, referenced, orphan, dataset,
):
    workspace_id, media_ids, _, payloads = await _seed_media_for_deletion(
        app, referenced=referenced, orphan=orphan, dataset=dataset,
    )
    try:
        response = await client.delete("/api/admin/users/3?confirm=true")
        assert response.status_code == 204
        async with app.state.session_factory() as session:
            assert await session.get(Workspace, workspace_id) is None
            assert await session.get(WorkspaceStorage, workspace_id) is None
            assert not (await session.scalars(
                select(MediaObject.id).where(MediaObject.id.in_(media_ids))
            )).all()
        assert all(not path.exists() for path in payloads)
        assert fake_auth_delete == [3]
    finally:
        await _cleanup_user_data(app, 3)


async def test_delete_user_restores_orphan_media_and_files_on_commit_failure(
    app, client, fake_directory, fake_auth_delete, monkeypatch,
):
    from sqlalchemy.ext.asyncio import AsyncSession

    workspace_id, media_ids, usage, payloads = await _seed_media_for_deletion(
        app, referenced=True, orphan=True, dataset=True,
    )

    async def fail_commit(_session):
        raise RuntimeError("injected admin delete commit failure")

    try:
        with monkeypatch.context() as patch:
            patch.setattr(AsyncSession, "commit", fail_commit)
            with pytest.raises(RuntimeError, match="injected admin delete commit failure"):
                await client.delete("/api/admin/users/3?confirm=true")
        async with app.state.session_factory() as session:
            assert await session.get(Workspace, workspace_id) is not None
            storage = await session.get(WorkspaceStorage, workspace_id)
            assert storage.bytes_used == usage
            assert set((await session.scalars(
                select(MediaObject.id).where(MediaObject.id.in_(media_ids))
            )).all()) == set(media_ids)
            assert await session.scalar(select(Image.id).join(Dataset).where(
                Dataset.workspace_id == workspace_id,
            )) is not None
        assert all(path.read_bytes() == content for path, content in payloads.items())
        assert fake_auth_delete == []
    finally:
        await _cleanup_user_data(app, 3)


async def test_delete_self_is_rejected(
    app, client: httpx.AsyncClient, fake_directory, fake_auth_delete
):
    await _seed(app, admin_ids=[1], usage={})
    response = await client.delete("/api/admin/users/1?confirm=true")
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "admin-self-delete"
    assert fake_auth_delete == []


async def test_delete_admin_target_is_rejected(
    app, client: httpx.AsyncClient, fake_directory, fake_auth_delete
):
    await _seed(app, admin_ids=[1, 2], usage={})
    response = await client.delete("/api/admin/users/2?confirm=true")
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "admin-target-admin"
    assert fake_auth_delete == []


async def test_delete_unknown_user_is_404(
    app, client: httpx.AsyncClient, fake_directory, fake_auth_delete
):
    await _seed(app, admin_ids=[1], usage={})
    response = await client.delete("/api/admin/users/99?confirm=true")
    assert response.status_code == 404
    assert fake_auth_delete == []


async def test_delete_user_with_active_run_is_rejected(
    app, client: httpx.AsyncClient, fake_directory, fake_auth_delete
):
    from app.models import TrainingRun

    await _seed(app, admin_ids=[1], usage={})
    await _seed_user_data(app, 3)
    async with app.state.session_factory() as session:
        session.add(
            TrainingRun(
                workspace_id=(await _personal_workspace(session, 3)).id,
                created_by=3,
                dataset_id=None,
                dataset_name="test-admin-ds-3",
                weights="yolo26n.pt",
                epochs=1,
                imgsz=640,
                batch=1,
                split_mode="2way",
                ratios={"train": 0.9, "valid": 0.1},
                seed=0,
                state="running",
                out_dir="runs/test-admin-delete-3",
            )
        )
        await session.commit()
    try:
        response = await client.delete("/api/admin/users/3?confirm=true")
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "admin-user-active-runs"
        assert fake_auth_delete == []
    finally:
        await _cleanup_user_data(app, 3)


async def _seed_team(app, workspace_id: int = 3) -> tuple[int, int]:
    """A team has the same id as a user but belongs to a different scope."""
    async with app.state.session_factory() as session:
        session.add(Workspace(
            id=workspace_id, kind="team", name="test-admin-team", created_by=3,
        ))
        await session.flush()
        session.add_all([
            WorkspaceMember(workspace_id=workspace_id, user_id=2, role="owner"),
            WorkspaceMember(workspace_id=workspace_id, user_id=3, role="editor"),
            WorkspaceStorage(
                workspace_id=workspace_id, bytes_used=90_000, quota_limit_bytes=99_000,
            ),
        ])
        project = Project(workspace_id=workspace_id, name="test-admin-team-project")
        session.add(project)
        await session.flush()
        dataset = Dataset(
            workspace_id=workspace_id, project_id=project.id,
            name="test-admin-team-dataset", status="ready",
            storage_path="datasets/test-admin-team-dataset",
        )
        session.add(dataset)
        await session.commit()
        return project.id, dataset.id


async def _cleanup_team(app, workspace_id: int = 3) -> None:
    async with app.state.session_factory() as session:
        await session.execute(delete(Dataset).where(Dataset.workspace_id == workspace_id))
        await session.execute(delete(MediaObject).where(MediaObject.workspace_id == workspace_id))
        await session.execute(delete(Project).where(Project.workspace_id == workspace_id))
        await session.execute(delete(Workspace).where(Workspace.id == workspace_id))
        await session.commit()


async def test_admin_usage_and_quota_are_personal_even_when_user_id_is_a_team_id(
    app, client: httpx.AsyncClient, fake_directory,
):
    await _seed(app, admin_ids=[1], usage={3: 1_500}, quota_limits={3: 9_000})
    await _seed_team(app)
    try:
        overview = await client.get("/api/admin/overview")
        assert overview.status_code == 200
        assert overview.json() == {"user_count": 3, "storage_total_bytes": 91_500}
        users = await client.get("/api/admin/users")
        assert users.status_code == 200
        target = next(row for row in users.json()["users"] if row["id"] == 3)
        assert target["bytes_used"] == 1_500
        assert target["limit_bytes"] == 9_000

        response = await client.patch("/api/admin/users/3/quota", json={"limit_bytes": 12_000})
        assert response.status_code == 200
        async with app.state.session_factory() as session:
            personal = await _personal_workspace(session, 3)
            assert personal.id != 3
            personal_storage = await session.get(WorkspaceStorage, personal.id)
            team_storage = await session.get(WorkspaceStorage, 3)
            assert personal_storage.quota_limit_bytes == 12_000
            assert personal_storage.bytes_used == 1_500
            assert team_storage.quota_limit_bytes == 99_000
            assert team_storage.bytes_used == 90_000
    finally:
        await _cleanup_team(app)


async def test_quota_creates_personal_instead_of_updating_a_team_with_the_user_id(
    app, client: httpx.AsyncClient, fake_directory,
):
    await _seed(app, admin_ids=[1], usage={})
    await _seed_team(app)
    try:
        response = await client.patch("/api/admin/users/3/quota", json={"limit_bytes": 7_000})
        assert response.status_code == 200
        assert response.json() == {"user_id": 3, "limit_bytes": 7_000}
        async with app.state.session_factory() as session:
            personal = await session.scalar(select(Workspace).where(
                Workspace.kind == "personal", Workspace.created_by == 3,
            ))
            assert personal is not None
            assert personal.id != 3
            member = await session.get(WorkspaceMember, (personal.id, 3))
            assert member.role == "owner"
            storage = await session.get(WorkspaceStorage, personal.id)
            assert (storage.bytes_used, storage.quota_limit_bytes) == (0, 7_000)
            team_storage = await session.get(WorkspaceStorage, 3)
            assert team_storage.quota_limit_bytes == 99_000
    finally:
        await _cleanup_team(app)


async def test_delete_user_removes_personal_but_preserves_team_resources(
    app, client: httpx.AsyncClient, fake_directory, fake_auth_delete,
):
    await _seed(app, admin_ids=[1], usage={3: 1_500})
    personal_project, personal_dataset = await _seed_user_data(app, 3)
    team_project, team_dataset = await _seed_team(app)
    async with app.state.session_factory() as session:
        team_media = MediaObject(
            workspace_id=3, created_by_dataset_id=team_dataset, original_bytes=90_000,
        )
        session.add(team_media)
        await session.commit()
        team_media_id = team_media.id
    try:
        confirmation = await client.delete("/api/admin/users/3")
        assert confirmation.status_code == 409
        detail = confirmation.json()["detail"]
        assert (detail["project_count"], detail["dataset_count"], detail["bytes_used"]) == (1, 1, 1_500)
        response = await client.delete("/api/admin/users/3?confirm=true")
        assert response.status_code == 204, response.text
        assert fake_auth_delete == [3]
        async with app.state.session_factory() as session:
            assert await session.get(Project, personal_project) is None
            assert await session.get(Dataset, personal_dataset) is None
            assert await session.get(Project, team_project) is not None
            assert await session.get(Dataset, team_dataset) is not None
            assert await session.get(Workspace, 3) is not None
            assert await session.get(MediaObject, team_media_id) is not None
            assert (await session.get(WorkspaceStorage, 3)).bytes_used == 90_000
    finally:
        await _cleanup_user_data(app, 3)
        await _cleanup_team(app)


async def test_delete_auth_failure_can_retry_without_recreating_personal(
    app, client: httpx.AsyncClient, fake_directory, monkeypatch,
):
    await _seed(app, admin_ids=[1], usage={3: 1_500})
    await _seed_user_data(app, 3)
    calls = []

    async def delete_account(_url, user_id):
        calls.append(user_id)
        if len(calls) == 1:
            raise RuntimeError("auth unavailable")

    monkeypatch.setattr("app.routers.admin.delete_auth_account", delete_account)
    try:
        first = await client.delete("/api/admin/users/3?confirm=true")
        assert first.status_code == 502
        async with app.state.session_factory() as session:
            assert await session.scalar(select(Workspace.id).where(
                Workspace.kind == "personal", Workspace.created_by == 3,
            )) is None
        second = await client.delete("/api/admin/users/3?confirm=true")
        assert second.status_code == 204, second.text
        assert calls == [3, 3]
        async with app.state.session_factory() as session:
            assert await session.scalar(select(Workspace.id).where(
                Workspace.kind == "personal", Workspace.created_by == 3,
            )) is None
    finally:
        await _cleanup_user_data(app, 3)
