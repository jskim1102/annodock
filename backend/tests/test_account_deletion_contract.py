"""Account deletion removes personal data while retaining team history."""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Dataset, MediaObject, Project, TrainingRun, Workspace, WorkspaceInvite,
    WorkspaceMember, WorkspaceStorage,
)
from tests.test_admin import (
    _cleanup, _cleanup_team, _cleanup_user_data, _seed, _seed_media_for_deletion,
    _seed_team, _seed_user_data, fake_auth_delete, fake_directory, legacy_personal_workspaces,
)

pytestmark = pytest.mark.asyncio


async def _cleanup_contract(app, user_id=3):
    await _cleanup_user_data(app, user_id)
    async with app.state.session_factory() as session:
        await session.execute(delete(TrainingRun).where(TrainingRun.workspace_id == 3))
        await session.commit()
    await _cleanup_team(app)
    async with app.state.session_factory() as session:
        await session.execute(delete(Workspace).where(Workspace.name == "test-account-former-team"))
        await session.commit()


@pytest_asyncio.fixture(autouse=True)
async def _admin_cleanup(app):
    # Contract fixtures also include team runs, so clean those before the
    # shared admin fixture removes personal workspace rows.
    await _cleanup_contract(app, 2)
    await _cleanup_contract(app, 3)
    await _cleanup(app)
    yield
    await _cleanup_contract(app, 2)
    await _cleanup_contract(app, 3)
    await _cleanup(app)


def _invite_history(row):
    return (row.workspace_id, row.role, row.created_by, row.uses, row.max_uses,
            row.token_hash, row.created_at, row.expires_at)


async def _seed_historical_invites_without_membership(app):
    """A departed creator still has active, expired and exhausted link history."""
    async with app.state.session_factory() as session:
        # Backfilled IDs may be ahead of the sequence in a populated test DB.
        workspace_id = await session.scalar(select(func.coalesce(func.max(Workspace.id), 0) + 1))
        other_team = Workspace(id=workspace_id, kind="team", name="test-account-former-team", created_by=2)
        session.add(other_team)
        await session.flush()
        session.add(WorkspaceMember(workspace_id=other_team.id, user_id=2, role="owner"))
        now = datetime.now(timezone.utc)
        created = [WorkspaceInvite(
            workspace_id=other_team.id, role=role, token_hash=uuid4().hex * 2,
            created_by=3, expires_at=now + timedelta(days=1), uses=1, max_uses=3,
        ) for role in ("admin", "editor", "viewer", "editor")]
        created[1].expires_at = now - timedelta(days=1)
        created[2].uses = 3
        created[3].revoked_at = now - timedelta(days=2)
        other_creator = [WorkspaceInvite(
            workspace_id=workspace_id, role="viewer", token_hash=uuid4().hex * 2,
            created_by=2, expires_at=now + timedelta(days=1), uses=1, max_uses=3,
        ) for workspace_id in (3, other_team.id)]
        session.add_all([*created, *other_creator])
        await session.commit()
        assert await session.get(WorkspaceMember, (other_team.id, 3)) is None
        return (
            other_team.id, [row.id for row in created], [row.id for row in other_creator],
            {row.id: _invite_history(row) for row in [*created, *other_creator]},
            (created[3].id, created[3].revoked_at),
        )


@pytest.mark.parametrize("confirm", [False, True])
async def test_team_owner_account_deletion_is_rejected_before_any_data_changes(
    app, client, fake_directory, fake_auth_delete, confirm,
):
    await _seed(app, admin_ids=[1], usage={2: 1234})
    personal_project, personal_dataset = await _seed_user_data(app, 2)
    team_project, team_dataset = await _seed_team(app)
    async with app.state.session_factory() as session:
        owner_link = WorkspaceInvite(
            workspace_id=3, role="viewer", token_hash=uuid4().hex * 2, created_by=2,
            expires_at=datetime.now(timezone.utc) + timedelta(days=1), uses=2, max_uses=4,
        )
        session.add(owner_link)
        await session.commit()
        owner_link_id = owner_link.id
    try:
        response = await client.delete(f"/api/admin/users/2?confirm={str(confirm).lower()}")
        assert response.status_code == 409, response.text
        assert response.json()["detail"] == {
            "code": "admin-user-team-owner",
            "workspaces": [{"id": 3, "name": "test-admin-team"}],
        }
        async with app.state.session_factory() as session:
            for project_id in (personal_project, team_project):
                assert await session.get(Project, project_id) is not None
            for dataset_id in (personal_dataset, team_dataset):
                assert await session.get(Dataset, dataset_id) is not None
            assert (await session.get(WorkspaceStorage, 10_002)).bytes_used == 1234
            assert (await session.get(WorkspaceStorage, 3)).bytes_used == 90_000
            assert (await session.get(WorkspaceMember, (3, 2))).role == "owner"
            saved_link = await session.get(WorkspaceInvite, owner_link_id)
            assert saved_link.revoked_at is None and saved_link.uses == 2
        assert fake_auth_delete == []
    finally:
        await _cleanup_contract(app, 2)


@pytest.mark.parametrize("auth_fails_once", [False, True])
async def test_member_account_deletion_preserves_team_data_and_history_and_retries_auth(
    app, client, fake_directory, monkeypatch, auth_headers, auth_fails_once,
):
    personal_id, personal_media, _, payloads = await _seed_media_for_deletion(
        app, referenced=True, orphan=True, dataset=True,
    )
    team_project, team_dataset = await _seed_team(app)
    team_file = app.state.settings.storage_dir / "datasets/test-admin-team-dataset/keep.jpg"
    team_file.parent.mkdir(parents=True, exist_ok=True)
    team_file.write_bytes(b"team content must survive")
    async with app.state.session_factory() as session:
        invite = WorkspaceInvite(
            workspace_id=3, role="editor", token_hash="a" * 64, created_by=3,
            expires_at=datetime.now(timezone.utc) + timedelta(days=1), uses=2, max_uses=10,
        )
        run = TrainingRun(
            workspace_id=3, created_by=3, dataset_id=team_dataset,
            dataset_name="test-team-history", weights="yolo26n.pt", epochs=1,
            imgsz=64, batch=1, split_mode="2way", ratios={"train": .5, "valid": .5},
            seed=1, state="done", out_dir="training-runs/test-team-history",
            artifact_bytes=100,
        )
        session.add_all([invite, run])
        await session.commit()
        invite_id, run_id = invite.id, run.id
    other_team_id, creator_invite_ids, other_invite_ids, history, manual_revocation = (
        await _seed_historical_invites_without_membership(app)
    )
    auth_calls = []
    committed_revocations = []

    async def delete_auth(_url, user_id):
        auth_calls.append(user_id)
        # Cross-database ordering: personal removal must already be committed.
        async with app.state.session_factory() as session:
            assert await session.get(Workspace, personal_id) is None
            assert await session.get(WorkspaceMember, (3, 3)) is None
            revocations = {}
            for row_id in [invite_id, *creator_invite_ids]:
                row = await session.get(WorkspaceInvite, row_id)
                assert row.revoked_at is not None, "all creator links must be revoked before auth deletion"
                revocations[row_id] = row.revoked_at
            if committed_revocations:
                assert revocations == committed_revocations[0], "an auth retry must not rewrite history"
            committed_revocations.append(revocations)
        if auth_fails_once and len(auth_calls) == 1:
            raise RuntimeError("injected auth outage")

    monkeypatch.setattr("app.routers.admin.delete_auth_account", delete_auth)
    try:
        response = await client.delete("/api/admin/users/3?confirm=true")
        assert response.status_code == (502 if auth_fails_once else 204), response.text
        if auth_fails_once:
            retried = await client.delete("/api/admin/users/3?confirm=true")
            assert retried.status_code == 204, retried.text
        assert auth_calls == ([3, 3] if auth_fails_once else [3])
        async with app.state.session_factory() as session:
            assert await session.get(Workspace, personal_id) is None
            assert await session.get(WorkspaceStorage, personal_id) is None
            for media_id in personal_media:
                assert await session.get(MediaObject, media_id) is None
            assert await session.get(Project, team_project) is not None
            assert await session.get(Dataset, team_dataset) is not None
            assert await session.get(WorkspaceMember, (3, 3)) is None
            assert (await session.get(WorkspaceMember, (3, 2))).role == "owner"
            storage = await session.get(WorkspaceStorage, 3)
            assert (storage.bytes_used, storage.quota_limit_bytes) == (90_000, 99_000)
            saved_invite = await session.get(WorkspaceInvite, invite_id)
            assert (saved_invite.created_by, saved_invite.role, saved_invite.uses) == (3, "editor", 2)
            assert saved_invite.revoked_at is not None
            for row_id, expected_history in history.items():
                row = await session.get(WorkspaceInvite, row_id)
                assert _invite_history(row) == expected_history
                assert (row.revoked_at is not None) == (row_id in creator_invite_ids)
            assert (await session.get(WorkspaceInvite, manual_revocation[0])).revoked_at == manual_revocation[1]
            assert await session.get(WorkspaceMember, (other_team_id, 3)) is None
            assert (await session.get(WorkspaceMember, (other_team_id, 2))).role == "owner"
            saved_run = await session.get(TrainingRun, run_id)
            assert (saved_run.created_by, saved_run.workspace_id, saved_run.artifact_bytes) == (3, 3, 100)
        for workspace_id, expected_id in zip((3, other_team_id), other_invite_ids):
            listing = await client.get(f"/api/workspaces/{workspace_id}/invites", headers=auth_headers(2))
            assert listing.status_code == 200, listing.text
            assert [row["id"] for row in listing.json()] == [expected_id]
        assert all(not path.exists() for path in payloads)
        assert team_file.read_bytes() == b"team content must survive"
    finally:
        await _cleanup_contract(app)


async def test_failed_account_delete_rolls_back_membership_personal_data_and_all_link_revocations(
    app, client, fake_directory, fake_auth_delete, monkeypatch,
):
    await _seed(app, admin_ids=[1], usage={3: 1234})
    personal_project, personal_dataset = await _seed_user_data(app, 3)
    await _seed_team(app)
    _other_team_id, creator_invite_ids, _other_invite_ids, history, manual_revocation = (
        await _seed_historical_invites_without_membership(app)
    )
    async with app.state.session_factory() as session:
        current_link = WorkspaceInvite(
            workspace_id=3, role="admin", token_hash=uuid4().hex * 2, created_by=3,
            expires_at=datetime.now(timezone.utc) + timedelta(days=1), uses=2, max_uses=4,
        )
        session.add(current_link)
        await session.commit()
        current_link_id = current_link.id
    observed = []

    async def fail_after_flush(session):
        await session.flush()
        observed.append({
            "personal_deleted": await session.get(Workspace, 10_003) is None,
            "membership_deleted": await session.get(WorkspaceMember, (3, 3)) is None,
            "revocations": [(await session.get(WorkspaceInvite, row_id)).revoked_at is not None
                            for row_id in [current_link_id, *creator_invite_ids]],
        })
        raise RuntimeError("fixture account invite commit failed")

    with monkeypatch.context() as patch:
        patch.setattr(AsyncSession, "commit", fail_after_flush)
        with pytest.raises(RuntimeError, match="fixture account invite commit failed"):
            await client.delete("/api/admin/users/3?confirm=true")
    assert observed == [{
        "personal_deleted": True, "membership_deleted": True,
        "revocations": [True] * (len(creator_invite_ids) + 1),
    }]
    assert fake_auth_delete == []
    async with app.state.session_factory() as session:
        assert await session.get(Workspace, 10_003) is not None
        assert await session.get(Project, personal_project) is not None
        assert await session.get(Dataset, personal_dataset) is not None
        assert (await session.get(WorkspaceStorage, 10_003)).bytes_used == 1234
        assert (await session.get(WorkspaceMember, (3, 3))).role == "editor"
        current = await session.get(WorkspaceInvite, current_link_id)
        assert (current.created_by, current.role, current.uses, current.revoked_at) == (3, "admin", 2, None)
        for row_id, expected_history in history.items():
            row = await session.get(WorkspaceInvite, row_id)
            assert _invite_history(row) == expected_history
            assert row.revoked_at == (manual_revocation[1] if row_id == manual_revocation[0] else None)
