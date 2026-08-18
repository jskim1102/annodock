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


async def _seed(app, *, admin_ids: list[int], usage: dict[int, int]) -> None:
    async with app.state.session_factory() as session:
        for owner_id in admin_ids:
            session.add(AdminUser(owner_id=owner_id))
        for owner_id, bytes_used in usage.items():
            session.add(UserStorage(owner_id=owner_id, bytes_used=bytes_used))
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
    await _seed(app, admin_ids=[1], usage={2: 500, 3: 1500})

    response = await client.get("/api/admin/users")
    assert response.status_code == 200
    users = response.json()["users"]
    assert [row["id"] for row in users] == [3, 2, 1]
    assert [row["bytes_used"] for row in users] == [1500, 500, 0]
    assert users[0]["email"] == "new@test.local"
    assert users[0]["login_methods"] == ["google"]
    assert users[1]["login_methods"] == ["이메일", "kakao"]
    assert users[2]["login_methods"] == ["이메일"]


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
