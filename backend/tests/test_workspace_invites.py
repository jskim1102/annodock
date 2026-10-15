"""Authenticated invitation links: hashed tokens, roles, and atomic use limits."""

from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Workspace, WorkspaceInvite, WorkspaceMember


pytestmark = pytest.mark.asyncio
USERS = {"owner": 930_101, "admin": 930_102, "editor": 930_103, "viewer": 930_104}
NEW_USER = 930_201


async def _team(app):
    async with app.state.session_factory() as session:
        team = Workspace(kind="team", name=f"test-invite-{uuid4().hex}", created_by=USERS["owner"])
        session.add(team)
        await session.flush()
        session.add_all(WorkspaceMember(workspace_id=team.id, user_id=user, role=role)
                        for role, user in USERS.items())
        await session.commit()
        return team.id


async def _mint(client, auth_headers, team_id, *, actor="admin", **values):
    response = await client.post(f"/api/workspaces/{team_id}/invites", headers=auth_headers(USERS[actor]),
                                 json={"role": "editor", **values})
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["accept_url"].startswith("/invites/")
    return body["invite"], body["accept_url"].removeprefix("/invites/")


async def test_minted_token_is_returned_once_and_persisted_only_as_sha256(client, app, auth_headers):
    team_id = await _team(app)
    before = datetime.now(timezone.utc)
    invite, token = await _mint(client, auth_headers, team_id)
    assert len(token) >= 43
    assert invite["role"] == "editor" and invite["uses"] == 0
    assert invite["max_uses"] is None and invite["created_by"] == USERS["admin"]
    assert before + timedelta(days=7) <= datetime.fromisoformat(invite["expires_at"]) <= datetime.now(timezone.utc) + timedelta(days=7)
    async with app.state.session_factory() as session:
        row = await session.get(WorkspaceInvite, invite["id"])
        assert row.token_hash == hashlib.sha256(token.encode()).hexdigest()
        assert row.workspace_id == team_id
        assert token not in repr({column.name: getattr(row, column.name) for column in WorkspaceInvite.__table__.columns})
    listing = await client.get(f"/api/workspaces/{team_id}/invites", headers=auth_headers(USERS["admin"]))
    assert listing.status_code == 200, listing.text
    assert [row["id"] for row in listing.json()] == [invite["id"]]
    assert token not in listing.text and "token_hash" not in listing.text


async def test_link_preview_and_accept_use_link_role_without_changing_existing_members(client, app, auth_headers):
    team_id = await _team(app)
    invite, token = await _mint(client, auth_headers, team_id, role="editor")
    preview = await client.get(f"/api/invites/{token}", headers=auth_headers(NEW_USER))
    assert preview.status_code == 200, preview.text
    assert preview.json()["workspace_id"] == team_id
    assert preview.json()["role"] == "editor" and preview.json()["already_member"] is False
    accepted = await client.post(f"/api/invites/{token}/accept", headers=auth_headers(NEW_USER))
    assert accepted.status_code == 200, accepted.text
    assert accepted.json() == {"workspace_id": team_id, "role": "editor", "already_member": False}
    async with app.state.session_factory() as session:
        assert (await session.get(WorkspaceMember, (team_id, NEW_USER))).role == "editor"
        assert (await session.get(WorkspaceInvite, invite["id"])).uses == 1


@pytest.mark.parametrize("role", ["editor", "viewer"])
async def test_invite_creation_requires_workspace_admin(client, app, auth_headers, role):
    team_id = await _team(app)
    response = await client.post(f"/api/workspaces/{team_id}/invites", headers=auth_headers(USERS[role]), json={"role": "editor"})
    assert response.status_code == 403, response.text
    assert response.json()["detail"] == {"code": "insufficient_role", "required": "admin", "actual": role}


@pytest.mark.parametrize("actor", ["owner", "admin"])
@pytest.mark.parametrize("role", ["admin", "editor"])
async def test_admin_or_owner_can_mint_each_supported_link_role_with_explicit_limits(client, app, auth_headers, role, actor):
    team_id = await _team(app)
    before = datetime.now(timezone.utc)
    invite, _token = await _mint(client, auth_headers, team_id, actor=actor, role=role,
                                expires_in_days=30, max_uses=2)
    assert invite["role"] == role and invite["max_uses"] == 2
    assert invite["created_by"] == USERS[actor]
    assert before + timedelta(days=30) <= datetime.fromisoformat(invite["expires_at"]) <= datetime.now(timezone.utc) + timedelta(days=30)


@pytest.mark.parametrize("body", [
    {"role": "owner"}, {"role": "bogus"}, {},
    {"role": "editor", "expires_in_days": 0}, {"role": "editor", "expires_in_days": 31},
    {"role": "editor", "expires_in_days": 1.5},
    {"role": "editor", "max_uses": 0}, {"role": "editor", "max_uses": -1},
    {"role": "editor", "max_uses": 1.5},
])
async def test_invite_parameters_reject_invalid_roles_and_limits(client, app, auth_headers, body):
    team_id = await _team(app)
    response = await client.post(f"/api/workspaces/{team_id}/invites", headers=auth_headers(USERS["admin"]), json=body)
    assert response.status_code == 422, response.text
    async with app.state.session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(WorkspaceInvite).where(
            WorkspaceInvite.workspace_id == team_id)) == 0


async def test_personal_workspace_rejects_invite_creation(client, auth_headers):
    headers = auth_headers(USERS["owner"])
    listing = await client.get("/api/workspaces", headers=headers)
    assert listing.status_code == 200, listing.text
    personal_id = listing.json()["items"][0]["id"]
    response = await client.post(f"/api/workspaces/{personal_id}/invites", headers=headers, json={"role": "editor"})
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "personal_workspace_fixed"


async def test_invite_routes_require_login(client, app, auth_headers):
    team_id = await _team(app)
    invite, token = await _mint(client, auth_headers, team_id)
    headers = {"Authorization": "Bearer not-a-token"}
    cases = [("GET", f"/api/invites/{token}", None),
             ("POST", f"/api/invites/{token}/accept", None),
             ("GET", f"/api/workspaces/{team_id}/invites", None),
             ("POST", f"/api/workspaces/{team_id}/invites", {"role": "editor"}),
             ("DELETE", f"/api/workspaces/{team_id}/invites/{invite['id']}", None)]
    for method, path, body in cases:
        response = await client.request(method, path, headers=headers, json=body)
        assert response.status_code == 401, response.text


@pytest.mark.parametrize("role", ["editor", "viewer"])
async def test_listing_and_revocation_require_admin(client, app, auth_headers, role):
    team_id = await _team(app)
    invite, _token = await _mint(client, auth_headers, team_id)
    for method, suffix in (("GET", ""), ("DELETE", f"/{invite['id']}")):
        response = await client.request(method, f"/api/workspaces/{team_id}/invites{suffix}", headers=auth_headers(USERS[role]))
        assert response.status_code == 403, response.text
        assert response.json()["detail"] == {"code": "insufficient_role", "required": "admin", "actual": role}


async def test_invite_management_uses_path_workspace_ignoring_invalid_header(client, app, auth_headers):
    team_id = await _team(app)
    headers = {**auth_headers(USERS["admin"]), "X-Workspace-Id": "invalid-is-ignored"}
    minted = await client.post(f"/api/workspaces/{team_id}/invites", headers=headers, json={"role": "editor"})
    assert minted.status_code == 201, minted.text
    invite_id = minted.json()["invite"]["id"]
    listing = await client.get(f"/api/workspaces/{team_id}/invites", headers=headers)
    assert listing.status_code == 200, listing.text
    assert [item["id"] for item in listing.json()] == [invite_id]
    revoked = await client.delete(f"/api/workspaces/{team_id}/invites/{invite_id}", headers=headers)
    assert revoked.status_code == 204, revoked.text


async def test_nonmember_management_and_cross_workspace_invite_are_404(client, app, auth_headers):
    team_id, other_id = await _team(app), await _team(app)
    invite, _token = await _mint(client, auth_headers, team_id)
    for method, suffix, body in (("GET", "", None), ("POST", "", {"role": "editor"}),
                                 ("DELETE", f"/{invite['id']}", None)):
        response = await client.request(method, f"/api/workspaces/{team_id}/invites{suffix}",
                                        headers=auth_headers(NEW_USER), json=body)
        assert response.status_code == 404, response.text
    foreign = await client.delete(f"/api/workspaces/{other_id}/invites/{invite['id']}", headers=auth_headers(USERS["admin"]))
    assert foreign.status_code == 404, foreign.text


async def test_active_list_omits_revoked_expired_exhausted_links(client, app, auth_headers):
    team_id = await _team(app)
    invites = [await _mint(client, auth_headers, team_id, max_uses=2) for _ in range(4)]
    async with app.state.session_factory() as session:
        active = await session.get(WorkspaceInvite, invites[0][0]["id"])
        active.uses = 1
        revoked = await session.get(WorkspaceInvite, invites[1][0]["id"])
        revoked.revoked_at = datetime.now(timezone.utc)
        expired = await session.get(WorkspaceInvite, invites[2][0]["id"])
        expired.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        exhausted = await session.get(WorkspaceInvite, invites[3][0]["id"])
        exhausted.uses = 2
        await session.commit()
    response = await client.get(f"/api/workspaces/{team_id}/invites", headers=auth_headers(USERS["owner"]))
    assert response.status_code == 200, response.text
    assert [item["id"] for item in response.json()] == [invites[0][0]["id"]]
    assert all(token not in response.text for _invite, token in invites)
    assert "token_hash" not in response.text and "accept_url" not in response.text


@pytest.mark.parametrize("actor", ["owner", "admin"])
async def test_revoked_link_and_repeat_revocation_are_404(client, app, auth_headers, actor):
    team_id = await _team(app)
    invite, token = await _mint(client, auth_headers, team_id)
    headers = auth_headers(USERS[actor])
    url = f"/api/workspaces/{team_id}/invites/{invite['id']}"
    first = await client.delete(url, headers=headers)
    assert first.status_code == 204, first.text
    again = await client.delete(url, headers=headers)
    assert again.status_code == 404, again.text
    for user_id in (NEW_USER, USERS["viewer"]):
        for method, suffix in (("GET", ""), ("POST", "/accept")):
            response = await client.request(method, f"/api/invites/{token}{suffix}", headers=auth_headers(user_id))
            assert response.status_code == 404, response.text
    async with app.state.session_factory() as session:
        row = await session.get(WorkspaceInvite, invite["id"])
        assert row.revoked_at is not None and row.uses == 0


async def test_unknown_link_is_404(client, auth_headers):
    for method, suffix in (("GET", ""), ("POST", "/accept")):
        response = await client.request(method, f"/api/invites/{uuid4().hex}{suffix}", headers=auth_headers(NEW_USER))
        assert response.status_code == 404, response.text


@pytest.mark.parametrize("state,code", [("expired", "invite_expired"), ("exhausted", "invite_exhausted")])
async def test_expired_and_exhausted_links_reject_new_members_with_410(client, app, auth_headers, state, code):
    team_id = await _team(app)
    invite, token = await _mint(client, auth_headers, team_id, max_uses=1)
    async with app.state.session_factory() as session:
        row = await session.get(WorkspaceInvite, invite["id"])
        if state == "expired":
            row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        else:
            row.uses = 1
        await session.commit()
    for method, suffix in (("GET", ""), ("POST", "/accept")):
        response = await client.request(method, f"/api/invites/{token}{suffix}", headers=auth_headers(NEW_USER))
        assert response.status_code == 410, response.text
        assert response.json()["detail"]["code"] == code
    async with app.state.session_factory() as session:
        assert await session.get(WorkspaceMember, (team_id, NEW_USER)) is None


async def test_last_slot_retry_is_idempotent_and_does_not_consume_more_uses(client, app, auth_headers):
    team_id = await _team(app)
    invite, token = await _mint(client, auth_headers, team_id, role="editor", max_uses=1)
    headers = auth_headers(NEW_USER)
    first = await client.post(f"/api/invites/{token}/accept", headers=headers)
    assert first.status_code == 200 and first.json()["already_member"] is False, first.text
    second = await client.post(f"/api/invites/{token}/accept", headers=headers)
    assert second.status_code == 200, second.text
    assert second.json() == {"workspace_id": team_id, "role": "editor", "already_member": True}
    other = await client.post(f"/api/invites/{token}/accept", headers=auth_headers(NEW_USER + 1))
    assert other.status_code == 410 and other.json()["detail"]["code"] == "invite_exhausted"
    async with app.state.session_factory() as session:
        assert (await session.get(WorkspaceInvite, invite["id"])).uses == 1


@pytest.mark.parametrize("state", ["active", "expired", "exhausted"])
async def test_existing_member_keeps_actual_role_and_never_consumes_uses(client, app, auth_headers, state):
    team_id = await _team(app)
    invite, token = await _mint(client, auth_headers, team_id, role="editor", max_uses=1)
    uses = 1 if state == "exhausted" else 0
    async with app.state.session_factory() as session:
        row = await session.get(WorkspaceInvite, invite["id"])
        row.uses = uses
        if state == "expired":
            row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        await session.commit()
    headers = auth_headers(USERS["admin"])
    preview = await client.get(f"/api/invites/{token}", headers=headers)
    assert preview.status_code == 200 and preview.json()["already_member"] is True, preview.text
    accepted = await client.post(f"/api/invites/{token}/accept", headers=headers)
    assert accepted.status_code == 200, accepted.text
    assert accepted.json() == {"workspace_id": team_id, "role": "admin", "already_member": True}
    async with app.state.session_factory() as session:
        assert (await session.get(WorkspaceInvite, invite["id"])).uses == uses
        assert (await session.get(WorkspaceMember, (team_id, USERS["admin"]))).role == "admin"


async def test_unlimited_link_accepts_multiple_accounts(client, app, auth_headers):
    team_id = await _team(app)
    invite, token = await _mint(client, auth_headers, team_id, max_uses=None)
    for user_id in range(NEW_USER, NEW_USER + 3):
        response = await client.post(f"/api/invites/{token}/accept", headers=auth_headers(user_id))
        assert response.status_code == 200, response.text
        assert response.json()["already_member"] is False
    async with app.state.session_factory() as session:
        row = await session.get(WorkspaceInvite, invite["id"])
        assert row.max_uses is None and row.uses == 3


def _synchronize_workspace_lock(monkeypatch, participants):
    barrier = asyncio.Event()
    arrivals = []
    execute = AsyncSession.execute
    scalar = AsyncSession.scalar

    async def arrive(statement):
        sql = str(statement).upper()
        if "WORKSPACES" in sql and "WORKSPACE_INVITES" not in sql and "FOR UPDATE" in sql:
            arrivals.append(True)
            if len(arrivals) == participants:
                barrier.set()
            await asyncio.wait_for(barrier.wait(), timeout=10)

    async def execute_before_lock(session, statement, *args, **kwargs):
        await arrive(statement)
        return await execute(session, statement, *args, **kwargs)

    async def scalar_before_lock(session, statement, *args, **kwargs):
        await arrive(statement)
        return await scalar(session, statement, *args, **kwargs)

    monkeypatch.setattr(AsyncSession, "execute", execute_before_lock)
    monkeypatch.setattr(AsyncSession, "scalar", scalar_before_lock)
    return arrivals


async def test_concurrent_acceptances_cannot_exceed_remaining_use_limit(client, app, auth_headers, monkeypatch):
    team_id = await _team(app)
    invite, token = await _mint(client, auth_headers, team_id, max_uses=2)
    arrivals = _synchronize_workspace_lock(monkeypatch, 3)
    results = await asyncio.wait_for(asyncio.gather(*(
        client.post(f"/api/invites/{token}/accept", headers=auth_headers(user_id))
        for user_id in range(NEW_USER, NEW_USER + 3))), timeout=15)
    assert len(arrivals) == 3
    assert sorted(response.status_code for response in results) == [200, 200, 410], [r.text for r in results]
    assert next(r for r in results if r.status_code == 410).json()["detail"]["code"] == "invite_exhausted"
    async with app.state.session_factory() as session:
        assert (await session.get(WorkspaceInvite, invite["id"])).uses == 2
        assert await session.scalar(select(func.count()).select_from(WorkspaceMember).where(
            WorkspaceMember.workspace_id == team_id,
            WorkspaceMember.user_id.in_(range(NEW_USER, NEW_USER + 3)))) == 2


async def test_concurrent_duplicate_acceptance_adds_one_member_and_one_use(client, app, auth_headers, monkeypatch):
    team_id = await _team(app)
    invite, token = await _mint(client, auth_headers, team_id, max_uses=1)
    arrivals = _synchronize_workspace_lock(monkeypatch, 2)
    results = await asyncio.wait_for(asyncio.gather(*(
        client.post(f"/api/invites/{token}/accept", headers=auth_headers(NEW_USER)) for _ in range(2))), timeout=15)
    assert len(arrivals) == 2
    assert all(response.status_code == 200 for response in results), [r.text for r in results]
    assert sorted(response.json()["already_member"] for response in results) == [False, True]
    async with app.state.session_factory() as session:
        assert (await session.get(WorkspaceInvite, invite["id"])).uses == 1
        assert (await session.get(WorkspaceMember, (team_id, NEW_USER))).role == "editor"


async def test_failed_accept_commit_rolls_back_membership_and_use_together(client, app, auth_headers, monkeypatch):
    team_id = await _team(app)
    invite, token = await _mint(client, auth_headers, team_id, max_uses=1)
    original = AsyncSession.commit
    async def fail_commit(_session):
        raise RuntimeError("fixture invite commit failed")
    monkeypatch.setattr(AsyncSession, "commit", fail_commit)
    with pytest.raises(RuntimeError, match="fixture invite commit failed"):
        await client.post(f"/api/invites/{token}/accept", headers=auth_headers(NEW_USER))
    monkeypatch.setattr(AsyncSession, "commit", original)
    async with app.state.session_factory() as session:
        assert (await session.get(WorkspaceInvite, invite["id"])).uses == 0
        assert await session.get(WorkspaceMember, (team_id, NEW_USER)) is None


async def test_invite_lifecycle_never_queries_or_mutates_auth_database(client, app, auth_headers, monkeypatch):
    from app.services import admin
    def forbidden_engine(*_args, **_kwargs):
        pytest.fail("invitation links must not open the auth database")
    async def forbidden_directory(*_args, **_kwargs):
        pytest.fail("invitation links must not query or mutate auth accounts")
    monkeypatch.setattr(admin, "_engine_for", forbidden_engine)
    monkeypatch.setattr(admin, "load_auth_directory", forbidden_directory)
    monkeypatch.setattr(admin, "delete_auth_account", forbidden_directory)
    team_id = await _team(app)
    invite, token = await _mint(client, auth_headers, team_id)
    preview = await client.get(f"/api/invites/{token}", headers=auth_headers(NEW_USER))
    assert preview.status_code == 200, preview.text
    accepted = await client.post(f"/api/invites/{token}/accept", headers=auth_headers(NEW_USER))
    assert accepted.status_code == 200, accepted.text
    listing = await client.get(f"/api/workspaces/{team_id}/invites", headers=auth_headers(USERS["admin"]))
    assert listing.status_code == 200, listing.text
    revoked = await client.delete(f"/api/workspaces/{team_id}/invites/{invite['id']}", headers=auth_headers(USERS["admin"]))
    assert revoked.status_code == 204, revoked.text


@pytest.mark.parametrize("creator_role", [None, "editor", "viewer"])
async def test_ineligible_creator_is_rejected_before_existing_member_shortcut(
    client, app, auth_headers, creator_role,
):
    team_id = await _team(app)
    invite, token = await _mint(client, auth_headers, team_id, role="admin", max_uses=5)
    async with app.state.session_factory() as session:
        creator = await session.get(WorkspaceMember, (team_id, USERS["admin"]))
        if creator_role is None:
            await session.delete(creator)
        else:
            creator.role = creator_role
        row = await session.get(WorkspaceInvite, invite["id"])
        row.uses = 2
        # Simulate a historical link from before automatic revocation existed.
        assert row.revoked_at is None
        await session.commit()

    for user_id in (NEW_USER, USERS["viewer"], USERS["admin"]):
        for method, suffix in (("GET", ""), ("POST", "/accept")):
            response = await client.request(
                method, f"/api/invites/{token}{suffix}", headers=auth_headers(user_id),
            )
            assert response.status_code == 410, response.text
            assert response.json()["detail"] == {"code": "invite_revoked"}
    async with app.state.session_factory() as session:
        row = await session.get(WorkspaceInvite, invite["id"])
        assert (row.created_by, row.role, row.uses) == (USERS["admin"], "admin", 2)
        assert await session.get(WorkspaceMember, (team_id, NEW_USER)) is None
        assert (await session.get(WorkspaceMember, (team_id, USERS["viewer"]))).role == "viewer"
        creator = await session.get(WorkspaceMember, (team_id, USERS["admin"]))
        assert (creator.role if creator else None) == creator_role


async def _change_invite_creator(client, auth_headers, team_id, action):
    if action == "leave":
        return await client.delete(
            f"/api/workspaces/{team_id}/members/me", headers=auth_headers(USERS["admin"]),
        )
    url = f"/api/workspaces/{team_id}/members/{USERS['admin']}"
    if action == "remove":
        return await client.delete(url, headers=auth_headers(USERS["owner"]))
    return await client.patch(url, headers=auth_headers(USERS["owner"]), json={"role": action})


@pytest.mark.parametrize("action", ["remove", "leave", "editor"])
async def test_membership_loss_revokes_all_creator_links_in_only_that_workspace(
    client, app, auth_headers, action,
):
    team_id, other_team_id = await _team(app), await _team(app)
    links = [await _mint(client, auth_headers, team_id, role="admin", max_uses=3)
             for _ in range(4)]
    owner_link, owner_token = await _mint(client, auth_headers, team_id, actor="owner")
    other_link, other_token = await _mint(client, auth_headers, other_team_id)
    previous_revocation = datetime.now(timezone.utc) - timedelta(days=2)
    async with app.state.session_factory() as session:
        for index, (invite, _token) in enumerate(links):
            row = await session.get(WorkspaceInvite, invite["id"])
            row.uses = 3 if index == 2 else 1
            if index == 1:
                row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            if index == 3:
                row.revoked_at = previous_revocation
        await session.commit()

    changed = await _change_invite_creator(client, auth_headers, team_id, action)
    assert changed.status_code == (200 if action == "editor" else 204), changed.text
    async with app.state.session_factory() as session:
        for index, (invite, token) in enumerate(links):
            row = await session.get(WorkspaceInvite, invite["id"])
            assert row.revoked_at is not None, "expired/exhausted links also require durable revocation"
            if index == 3:
                assert row.revoked_at == previous_revocation
            assert (row.created_by, row.role, row.uses) == (
                USERS["admin"], "admin", 3 if index == 2 else 1,
            )
            assert row.token_hash == hashlib.sha256(token.encode()).hexdigest()
        assert (await session.get(WorkspaceInvite, owner_link["id"])).revoked_at is None
        assert (await session.get(WorkspaceInvite, other_link["id"])).revoked_at is None
        assert (await session.get(WorkspaceMember, (other_team_id, USERS["admin"]))).role == "admin"

    for workspace_id, expected_id in ((team_id, owner_link["id"]), (other_team_id, other_link["id"])):
        listing = await client.get(f"/api/workspaces/{workspace_id}/invites", headers=auth_headers(USERS["owner"]))
        assert listing.status_code == 200, listing.text
        assert [row["id"] for row in listing.json()] == [expected_id]
    for _invite, token in links[:3]:
        for user_id in (NEW_USER, USERS["viewer"], USERS["admin"]):
            for method, suffix in (("GET", ""), ("POST", "/accept")):
                rejected = await client.request(method, f"/api/invites/{token}{suffix}", headers=auth_headers(user_id))
                assert rejected.status_code == 410, rejected.text
                assert rejected.json()["detail"] == {"code": "invite_revoked"}
    async with app.state.session_factory() as session:
        creator = await session.get(WorkspaceMember, (team_id, USERS["admin"]))
        assert (creator.role if creator else None) == (action if action == "editor" else None)
        assert (await session.get(WorkspaceMember, (team_id, USERS["viewer"]))).role == "viewer"
        assert await session.get(WorkspaceMember, (team_id, NEW_USER)) is None
        assert [(await session.get(WorkspaceInvite, row["id"])).uses for row, _token in links] == [1, 1, 3, 1]

    # A different creator and this same creator in another team remain eligible.
    for token, workspace_id in ((owner_token, team_id), (other_token, other_team_id)):
        accepted = await client.post(f"/api/invites/{token}/accept", headers=auth_headers(NEW_USER))
        assert accepted.status_code == 200, accepted.text
        assert accepted.json() == {"workspace_id": workspace_id, "role": "editor", "already_member": False}


@pytest.mark.parametrize("action", ["remove", "leave", "editor"])
async def test_failed_membership_change_rolls_back_role_and_invite_revocation_together(
    client, app, auth_headers, monkeypatch, action,
):
    team_id = await _team(app)
    invite, token = await _mint(client, auth_headers, team_id, role="admin")
    observed = []

    async def fail_after_flush(session):
        await session.flush()
        role = await session.scalar(select(WorkspaceMember.role).where(
            WorkspaceMember.workspace_id == team_id, WorkspaceMember.user_id == USERS["admin"],
        ))
        revoked_at = await session.scalar(select(WorkspaceInvite.revoked_at).where(
            WorkspaceInvite.id == invite["id"],
        ))
        observed.append((role, revoked_at is not None))
        raise RuntimeError("fixture membership and invite commit failed")

    with monkeypatch.context() as patch:
        patch.setattr(AsyncSession, "commit", fail_after_flush)
        with pytest.raises(RuntimeError, match="fixture membership and invite commit failed"):
            await _change_invite_creator(client, auth_headers, team_id, action)
    assert observed == [(action if action == "editor" else None, True)]
    async with app.state.session_factory() as session:
        assert (await session.get(WorkspaceMember, (team_id, USERS["admin"]))).role == "admin"
        row = await session.get(WorkspaceInvite, invite["id"])
        assert row.revoked_at is None and row.uses == 0
    accepted = await client.post(f"/api/invites/{token}/accept", headers=auth_headers(NEW_USER))
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["role"] == "admin"


@pytest.mark.parametrize("actor", ["admin", "owner"])
async def test_viewer_invite_is_rejected_without_creating_or_changing_rows(client, app, auth_headers, actor):
    team_id = await _team(app)
    await _mint(client, auth_headers, team_id, role="editor")

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
    response = await client.post(f"/api/workspaces/{team_id}/invites",
                                 headers=auth_headers(USERS[actor]), json={"role": "viewer"})
    assert response.status_code == 422, response.text
    assert any(error["loc"] == ["body", "role"] for error in response.json()["detail"])
    assert await snapshot() == before


@pytest.mark.parametrize("method,suffix", [("GET", ""), ("POST", "/accept")])
@pytest.mark.parametrize("user_id", [NEW_USER, USERS["admin"]])
async def test_historical_viewer_invite_is_revoked_without_membership_or_use_changes(
    client, app, auth_headers, method, suffix, user_id,
):
    team_id = await _team(app)
    invite, token = await _mint(client, auth_headers, team_id, max_uses=5)
    async with app.state.session_factory() as session:
        row = await session.get(WorkspaceInvite, invite["id"])
        # Persist the old role directly because new viewer invites are rejected.
        row.role = "viewer"
        row.uses = 2
        await session.commit()
        before = (await session.execute(select(*WorkspaceMember.__table__.c).where(
            WorkspaceMember.workspace_id == team_id,
        ).order_by(WorkspaceMember.user_id))).all()

    response = await client.request(
        method, f"/api/invites/{token}{suffix}", headers=auth_headers(user_id),
    )
    assert response.status_code == 410, response.text
    assert response.json()["detail"] == {"code": "invite_revoked"}
    async with app.state.session_factory() as session:
        after = (await session.execute(select(*WorkspaceMember.__table__.c).where(
            WorkspaceMember.workspace_id == team_id,
        ).order_by(WorkspaceMember.user_id))).all()
        assert after == before
        assert (await session.get(WorkspaceInvite, invite["id"])).uses == 2
