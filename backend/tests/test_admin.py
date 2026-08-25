from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest
import pytest_asyncio

from app.models import AdminUser, UserStorage
from app.services.admin import AuthDirectoryUser

pytestmark = pytest.mark.asyncio


BASE_TIME = datetime(2026, 8, 1, tzinfo=timezone.utc)


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
        for owner_id in admin_ids:
            session.add(AdminUser(owner_id=owner_id))
        for owner_id in usage.keys() | quota_limits.keys():
            session.add(
                UserStorage(
                    owner_id=owner_id,
                    bytes_used=usage.get(owner_id, 0),
                    quota_limit_bytes=quota_limits.get(owner_id),
                )
            )
        await session.commit()


async def _cleanup(app) -> None:
    from sqlalchemy import delete

    async with app.state.session_factory() as session:
        await session.execute(delete(AdminUser))
        await session.execute(
            delete(UserStorage).where(UserStorage.owner_id.in_([1, 2, 3]))
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

    # Orphaned storage row (owner 999 absent from the directory) must not
    # inflate the headline total — it has to equal the per-user column.
    async with app.state.session_factory() as session:
        session.add(UserStorage(owner_id=999, bytes_used=77_000))
        await session.commit()
    try:
        response = await client.get("/api/admin/overview")
        assert response.status_code == 200
        body = response.json()
        assert body["user_count"] == 3
        assert body["storage_total_bytes"] == 2000
        assert "recent_users" not in body
    finally:
        from sqlalchemy import delete

        async with app.state.session_factory() as session:
            await session.execute(
                delete(UserStorage).where(UserStorage.owner_id == 999)
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
        storage = await session.get(UserStorage, 3)
        assert storage is not None
        assert storage.bytes_used == 1500
        assert storage.quota_limit_bytes == 8 * 1024**3


async def test_admin_quota_update_creates_storage_row_for_unused_user(
    app, client: httpx.AsyncClient, fake_directory
):
    await _seed(app, admin_ids=[1], usage={})

    response = await client.patch(
        "/api/admin/users/2/quota",
        json={"limit_bytes": 3 * 1024**3},
    )

    assert response.status_code == 200, response.text
    async with app.state.session_factory() as session:
        storage = await session.get(UserStorage, 2)
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


async def _seed_owner_data(app, owner_id: int) -> tuple[int, int]:
    """Create one project + one dataset for owner_id; return their ids."""
    from app.models import Dataset, Project

    async with app.state.session_factory() as session:
        project = Project(owner_id=owner_id, name=f"proj-{owner_id}")
        session.add(project)
        await session.flush()
        dataset = Dataset(
            owner_id=owner_id,
            project_id=project.id,
            name=f"ds-{owner_id}",
            status="ready",
            storage_path=f"datasets/test-admin-delete-{owner_id}",
        )
        session.add(dataset)
        await session.commit()
        return project.id, dataset.id


async def _cleanup_owner_data(app, owner_id: int) -> None:
    from sqlalchemy import delete

    from app.models import Dataset, Project, TrainingRun

    async with app.state.session_factory() as session:
        await session.execute(
            delete(TrainingRun).where(TrainingRun.owner_id == owner_id)
        )
        await session.execute(
            delete(Dataset).where(Dataset.owner_id == owner_id)
        )
        await session.execute(
            delete(Project).where(Project.owner_id == owner_id)
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
    await _seed_owner_data(app, 3)
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
        await _cleanup_owner_data(app, 3)


async def test_delete_user_cascades_data_and_auth_account(
    app, client: httpx.AsyncClient, fake_directory, fake_auth_delete
):
    from sqlalchemy import select as sa_select

    from app.models import Dataset, Project

    await _seed(app, admin_ids=[1], usage={3: 1500})
    project_id, dataset_id = await _seed_owner_data(app, 3)
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
                sa_select(UserStorage.owner_id).where(
                    UserStorage.owner_id == 3
                )
            ) is None
        assert fake_auth_delete == [3]
    finally:
        await _cleanup_owner_data(app, 3)


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
    await _seed_owner_data(app, 3)
    async with app.state.session_factory() as session:
        session.add(
            TrainingRun(
                owner_id=3,
                dataset_id=None,
                dataset_name="ds-3",
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
        await _cleanup_owner_data(app, 3)
