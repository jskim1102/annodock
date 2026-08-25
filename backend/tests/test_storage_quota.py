from __future__ import annotations

import httpx
import pytest

from app.services.quota import increase_bytes_used, set_quota_limit


pytestmark = pytest.mark.asyncio

QUOTA_LIMIT_BYTES = 5 * 1024**3
NEW_OWNER_ID = 61_001
ACCOUNTED_OWNER_ID = 61_002
ACCOUNTED_BYTES = 1_234_567


def _set_quota_limit(app) -> None:
    app.state.settings = app.state.settings.model_copy(
        update={"quota_bytes_per_user": QUOTA_LIMIT_BYTES}
    )


async def test_storage_quota_requires_authentication(app) -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
    ) as unauthenticated_client:
        response = await unauthenticated_client.get("/api/storage")

    assert response.status_code == 401


async def test_storage_quota_returns_zero_for_a_new_user(
    app,
    client: httpx.AsyncClient,
    auth_headers,
) -> None:
    _set_quota_limit(app)

    response = await client.get(
        "/api/storage",
        headers=auth_headers(NEW_OWNER_ID),
    )

    assert response.status_code == 200
    assert response.json() == {
        "used_bytes": 0,
        "referenced_bytes": 0,
        "limit_bytes": QUOTA_LIMIT_BYTES,
    }


async def test_storage_quota_reflects_the_persisted_counter(
    app,
    client: httpx.AsyncClient,
    auth_headers,
) -> None:
    _set_quota_limit(app)
    async with app.state.session_factory() as session:
        used_bytes = await increase_bytes_used(
            session,
            ACCOUNTED_OWNER_ID,
            ACCOUNTED_BYTES,
        )
        await session.commit()
    assert used_bytes == ACCOUNTED_BYTES

    response = await client.get(
        "/api/storage",
        headers=auth_headers(ACCOUNTED_OWNER_ID),
    )

    assert response.status_code == 200
    assert response.json() == {
        "used_bytes": ACCOUNTED_BYTES,
        "referenced_bytes": 0,
        "limit_bytes": QUOTA_LIMIT_BYTES,
    }


async def test_storage_quota_returns_the_users_persisted_override(
    app,
    client: httpx.AsyncClient,
    auth_headers,
) -> None:
    _set_quota_limit(app)
    override = 12 * 1024**3
    async with app.state.session_factory() as session:
        await set_quota_limit(session, ACCOUNTED_OWNER_ID, override)
        await session.commit()

    response = await client.get(
        "/api/storage",
        headers=auth_headers(ACCOUNTED_OWNER_ID),
    )

    assert response.status_code == 200
    assert response.json() == {
        "used_bytes": 0,
        "referenced_bytes": 0,
        "limit_bytes": override,
    }
