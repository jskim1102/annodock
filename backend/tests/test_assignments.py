"""Assignment lifecycle and concurrency through the authenticated HTTP API."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from fastapi import Request
from sqlalchemy import delete, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import CurrentUser, require_workspace_role
from app.models import AdminUser, Dataset, Image, Project, Workspace, WorkspaceMember
from app.services.admin import AuthDirectoryUser
from app.services.workspaces import change_workspace_member_role
from tests.factories import image_with_media


pytestmark = pytest.mark.asyncio
USERS = {"owner": 950_101, "admin": 950_102, "editor": 950_103, "viewer": 950_104}
OTHER_EDITOR = 950_105
OUTSIDER = 950_999
NOW = datetime(2026, 10, 14, 1, 0, tzinfo=timezone.utc)
STATE_FIELDS = ("label_status", "assignee_id", "assigned_at", "labeled_by", "labeled_at", "version")


def _headers(auth_headers, graph, actor="editor"):
    user_id = USERS[actor] if isinstance(actor, str) else actor
    return {**auth_headers(user_id), "X-Workspace-Id": str(graph["workspace_id"])}


async def _seed(app, images, *, workspace_id=None, extra_members=None):
    async with app.state.session_factory() as session:
        if workspace_id is None:
            workspace = Workspace(kind="team", name=f"test-assignment-team-{uuid4().hex}", created_by=USERS["owner"])
            session.add(workspace)
            await session.flush()
            workspace_id = workspace.id
            members = {user_id: role for role, user_id in USERS.items()}
            members.update({OTHER_EDITOR: "editor", **(extra_members or {})})
            session.add_all(WorkspaceMember(workspace_id=workspace_id, user_id=user_id, role=role)
                            for user_id, role in members.items())
        project = Project(workspace_id=workspace_id, name=f"test-assignment-project-{uuid4().hex}")
        session.add(project)
        await session.flush()
        dataset = Dataset(workspace_id=workspace_id, project_id=project.id,
                          name=f"test-assignment-dataset-{uuid4().hex}", status="ready",
                          storage_path="", image_count=len(images))
        session.add(dataset)
        await session.flush()
        rows = []
        for index, values in enumerate(images):
            values = dict(values)
            stem = values.pop("stem", f"image-{index:03d}")
            status = values.get("label_status", "unlabeled")
            assignee_id = values.get("assignee_id")
            labeled_by = values.get("labeled_by")
            row = image_with_media(
                workspace_id=workspace_id, dataset_id=dataset.id,
                stem=stem, filename=f"{stem}.jpg", rel_path=f"{index}/{stem}.jpg",
                width=64, height=48, file_path=f"fixture/{index}/{stem}.jpg",
                thumb_path=f"fixture/{index}/{stem}-thumb.jpg",
                **{"split": None, "has_label_source": False, "box_count": 0,
                   "label_status": status, "assignee_id": assignee_id,
                   "assigned_at": NOW if assignee_id is not None else None,
                   "labeled_by": labeled_by, "labeled_at": NOW if labeled_by is not None else None,
                   "version": 7, **values},
            )
            session.add(row)
            rows.append(row)
        await session.commit()
        assert workspace_id not in USERS.values(), "fixtures must not conflate a user and workspace id"
        return {"workspace_id": workspace_id, "dataset_id": dataset.id,
                "project_id": project.id, "image_ids": [row.id for row in rows]}


async def _states(app, image_ids):
    async with app.state.session_factory() as session:
        rows = (await session.scalars(select(Image).where(Image.id.in_(image_ids)))).all()
        return {row.id: {field: getattr(row, field) for field in STATE_FIELDS} for row in rows}


async def _change_hidden_viewer_role(app, workspace_id, user_id):
    """Exercise retained service behavior without the public role input schema."""
    async with app.state.session_factory() as session:
        # Reuse the real mutation dependency, including Workspace FOR UPDATE
        # and fresh actor membership checks, rather than simulating its lock.
        scope = await require_workspace_role("admin")(
            workspace_id=workspace_id,
            request=Request({"type": "http", "method": "PATCH", "headers": [],
                             "path": f"/api/workspaces/{workspace_id}/members/{user_id}"}),
            current_user=CurrentUser(id=USERS["owner"], subject=str(USERS["owner"]), claims={}),
            session=session,
        )
        member = await change_workspace_member_role(
            session, workspace_id=scope.id, user_id=user_id, role="viewer",
        )
        result = {"user_id": member.user_id, "role": member.role}
        await session.commit()
        return result


def _image_response(response, image_id, status, assignee_id, version):
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["id"] == image_id
    assert body["label_status"] == status
    assert body["assignee_id"] == assignee_id
    assert body["version"] == version
    assert "labeled_by" in body and "labeled_at" in body
    return body


async def test_claim_follows_viewer_stem_split_nullsfirst_and_reports_list_position(client, app, auth_headers):
    graph = await _seed(app, [
        {"stem": "z-last"}, {"stem": "a-first", "split": "valid"},
        {"stem": "a-first", "split": None}, {"stem": "a-first", "split": "train"},
        {"stem": "0-completed", "label_status": "labeled", "has_label_source": True},
    ])
    headers = _headers(auth_headers, graph)
    listing = await client.get(f"/api/datasets/{graph['dataset_id']}/images", headers=headers)
    assert listing.status_code == 200, listing.text
    ids = graph["image_ids"]
    assert [row["id"] for row in listing.json()["items"]] == [ids[4], ids[2], ids[3], ids[1], ids[0]]
    for position, image_id in enumerate([ids[2], ids[3], ids[1], ids[0]], start=1):
        response = await client.post(f"/api/datasets/{graph['dataset_id']}/claim", headers=headers, json={"split": None})
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["position"] == position
        image = body["image"]
        assert image["id"] == image_id and image["version"] == 8
        assert image["label_status"] == "in_progress" and image["assignee_id"] == USERS["editor"]
        assert image["labeled_by"] is None and image["labeled_at"] is None
        assert image["stem"] == listing.json()["items"][position]["stem"]
        assert image["split"] == listing.json()["items"][position]["split"]
    states = await _states(app, ids)
    assert all(states[image_id]["assigned_at"] is not None for image_id in ids[:4])
    assert states[ids[4]]["version"] == 7


async def test_claim_split_filter_position_counts_completed_and_other_assignees(client, app, auth_headers):
    graph = await _seed(app, [
        {"stem": "0-all", "split": None},
        {"stem": "a", "split": "train", "label_status": "labeled", "has_label_source": True},
        {"stem": "b", "split": "valid"},
        {"stem": "c", "split": "train", "label_status": "in_progress", "assignee_id": OTHER_EDITOR},
        {"stem": "d", "split": "train"},
    ])
    response = await client.post(f"/api/datasets/{graph['dataset_id']}/claim",
                                 headers=_headers(auth_headers, graph), json={"split": "train"})
    assert response.status_code == 200, response.text
    assert response.json()["image"]["id"] == graph["image_ids"][4]
    assert response.json()["position"] == 2
    empty = await client.post(f"/api/datasets/{graph['dataset_id']}/claim",
                              headers=_headers(auth_headers, graph), json={"split": "train"})
    assert empty.status_code == 200 and empty.json() == {"image": None}, empty.text
    states = await _states(app, graph["image_ids"])
    for index in (0, 2):
        assert states[graph["image_ids"][index]]["label_status"] == "unlabeled"


async def test_eight_concurrent_claims_assign_distinct_images_once(client, app, auth_headers):
    actors = list(range(950_200, 950_208))
    graph = await _seed(app, [{} for _ in actors], extra_members={actor: "editor" for actor in actors})
    results = await asyncio.wait_for(asyncio.gather(*(
        client.post(f"/api/datasets/{graph['dataset_id']}/claim",
                    headers=_headers(auth_headers, graph, actor), json={"split": None})
        for actor in actors
    )), timeout=15)
    assert all(response.status_code == 200 for response in results), [r.text for r in results]
    claimed = [response.json()["image"]["id"] for response in results]
    assert len(set(claimed)) == 8 and set(claimed) == set(graph["image_ids"])
    states = await _states(app, claimed)
    for actor, image_id in zip(actors, claimed):
        assert states[image_id]["assignee_id"] == actor
        assert states[image_id]["label_status"] == "in_progress"
        assert states[image_id]["version"] == 8


async def test_locked_candidate_returns_null_without_marking_completion_or_waiting_on_dataset(client, app, auth_headers):
    graph = await _seed(app, [{}])
    image_id = graph["image_ids"][0]
    headers = _headers(auth_headers, graph)
    async with app.state.session_factory() as locker:
        # This executes before claim starts: both locks are actually held,
        # matching an annotation transaction's Dataset + Image lock boundary.
        await locker.execute(select(Dataset, Image).join(Image, Image.dataset_id == Dataset.id)
                             .where(Image.id == image_id).with_for_update())
        response = await asyncio.wait_for(client.post(
            f"/api/datasets/{graph['dataset_id']}/claim", headers=headers, json={"split": None},
        ), timeout=5)
        assert response.status_code == 200 and response.json() == {"image": None}, response.text
        progress = await client.get(f"/api/datasets/{graph['dataset_id']}/progress", headers=headers)
        assert progress.status_code == 200, progress.text
        assert (progress.json()["unlabeled"], progress.json()["in_progress"], progress.json()["labeled"]) == (1, 0, 0)
        await locker.rollback()
    claimed = await client.post(f"/api/datasets/{graph['dataset_id']}/claim", headers=headers, json={"split": None})
    assert claimed.status_code == 200 and claimed.json()["image"]["id"] == image_id, claimed.text


async def test_claim_skips_image_while_complete_is_paused_after_actual_row_lock(client, app, auth_headers, monkeypatch):
    graph = await _seed(app, [{"stem": "first"}, {"stem": "second"}])
    locked, release = asyncio.Event(), asyncio.Event()
    paused = False

    async def after_lock(statement):
        nonlocal paused
        sql = str(statement.compile(dialect=postgresql.dialect())).upper()
        if not paused and "IMAGES" in sql and "FOR UPDATE" in sql and "SKIP LOCKED" not in sql:
            paused = True
            locked.set()
            await asyncio.wait_for(release.wait(), timeout=15)

    original_execute, original_scalar = AsyncSession.execute, AsyncSession.scalar

    async def execute_then_pause(session, statement, *args, **kwargs):
        result = await original_execute(session, statement, *args, **kwargs)
        await after_lock(statement)
        return result

    async def scalar_then_pause(session, statement, *args, **kwargs):
        result = await original_scalar(session, statement, *args, **kwargs)
        await after_lock(statement)
        return result

    monkeypatch.setattr(AsyncSession, "execute", execute_then_pause)
    monkeypatch.setattr(AsyncSession, "scalar", scalar_then_pause)
    headers = _headers(auth_headers, graph)
    completion = asyncio.create_task(client.post(f"/api/images/{graph['image_ids'][0]}/complete", headers=headers))
    try:
        await asyncio.wait_for(locked.wait(), timeout=5)
        claim = await asyncio.wait_for(client.post(
            f"/api/datasets/{graph['dataset_id']}/claim", headers=headers, json={"split": None},
        ), timeout=5)
        assert claim.status_code == 200, claim.text
        assert claim.json()["image"]["id"] == graph["image_ids"][1]
        assert claim.json()["position"] == 1
    finally:
        release.set()
        results = await asyncio.wait_for(asyncio.gather(completion, return_exceptions=True), timeout=10)
    assert not isinstance(results[0], BaseException), results[0]
    _image_response(results[0], graph["image_ids"][0], "labeled", None, 8)


async def test_progress_and_metadata_include_imported_labels_and_per_user_counts(client, app, auth_headers):
    graph = await _seed(app, [
        {}, {},
        {"label_status": "in_progress", "assignee_id": USERS["editor"]},
        {"label_status": "in_progress", "assignee_id": USERS["editor"]},
        {"label_status": "in_progress", "assignee_id": USERS["admin"]},
        {"label_status": "labeled", "labeled_by": USERS["editor"]},
        {"label_status": "labeled", "labeled_by": OTHER_EDITOR},
        {"label_status": "labeled", "has_label_source": True},
        {"label_status": "labeled", "has_label_source": True, "box_count": 2},
    ])
    headers = _headers(auth_headers, graph, "viewer")
    response = await client.get(f"/api/datasets/{graph['dataset_id']}/progress", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert {key: body[key] for key in ("total", "unlabeled", "in_progress", "labeled", "imported_labeled")} == {
        "total": 9, "unlabeled": 2, "in_progress": 3, "labeled": 4, "imported_labeled": 2,
    }
    members = {row["user_id"]: (row["in_progress"], row["labeled"]) for row in body["members"]}
    assert members[USERS["editor"]] == (2, 1)
    assert members[USERS["admin"]] == (1, 0)
    assert members[OTHER_EDITOR] == (0, 1)
    assert sum(value[0] for value in members.values()) == 3
    assert sum(value[1] for value in members.values()) == 2
    listing = await client.get(f"/api/datasets/{graph['dataset_id']}/images", headers=headers)
    assert listing.status_code == 200, listing.text
    states = await _states(app, graph["image_ids"])
    for row in listing.json()["items"]:
        expected = states[row["id"]]
        for key in ("label_status", "assignee_id", "version", "labeled_by"):
            assert row[key] == expected[key]
        assert (datetime.fromisoformat(row["labeled_at"]) if row["labeled_at"] else None) == expected["labeled_at"]
    detail = await client.get(f"/api/datasets/{graph['dataset_id']}", headers=headers)
    assert detail.status_code == 200, detail.text
    assert (detail.json()["workspace_id"], detail.json()["my_role"]) == (graph["workspace_id"], "viewer")


@pytest.mark.parametrize("actor,assignee", [("editor", None), ("editor", USERS["editor"]),
                                            ("admin", OTHER_EDITOR), ("owner", OTHER_EDITOR)])
async def test_complete_records_actor_clears_assignment_and_allows_empty_labels(client, app, auth_headers, actor, assignee):
    graph = await _seed(app, [{"label_status": "in_progress" if assignee else "unlabeled", "assignee_id": assignee}])
    image_id = graph["image_ids"][0]
    response = await client.post(f"/api/images/{image_id}/complete", headers=_headers(auth_headers, graph, actor))
    body = _image_response(response, image_id, "labeled", None, 8)
    assert body["labeled_by"] == USERS[actor] and body["labeled_at"] is not None
    state = (await _states(app, [image_id]))[image_id]
    assert state["assigned_at"] is None and state["labeled_by"] == USERS[actor]
    assert state["labeled_at"] is not None
    async with app.state.session_factory() as session:
        assert (await session.get(Image, image_id)).box_count == 0


async def test_editor_cannot_complete_someone_elses_assignment(client, app, auth_headers):
    graph = await _seed(app, [{"label_status": "in_progress", "assignee_id": OTHER_EDITOR}])
    before = await _states(app, graph["image_ids"])
    response = await client.post(f"/api/images/{graph['image_ids'][0]}/complete", headers=_headers(auth_headers, graph))
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "image_assigned_to_other"
    assert await _states(app, graph["image_ids"]) == before


@pytest.mark.parametrize("actor,labeled_by,allowed", [
    ("editor", USERS["editor"], True), ("editor", OTHER_EDITOR, False), ("editor", None, False),
    ("admin", OTHER_EDITOR, True), ("admin", None, True), ("owner", None, True),
])
async def test_uncomplete_requires_original_labeler_or_admin_and_claims_for_actor(client, app, auth_headers, actor, labeled_by, allowed):
    graph = await _seed(app, [{"label_status": "labeled", "labeled_by": labeled_by, "has_label_source": labeled_by is None}])
    image_id = graph["image_ids"][0]
    before = await _states(app, [image_id])
    response = await client.post(f"/api/images/{image_id}/uncomplete", headers=_headers(auth_headers, graph, actor))
    if not allowed:
        assert response.status_code == 403, response.text
        assert await _states(app, [image_id]) == before
        return
    body = _image_response(response, image_id, "in_progress", USERS[actor], 8)
    assert body["labeled_by"] is None and body["labeled_at"] is None
    state = (await _states(app, [image_id]))[image_id]
    assert state["assigned_at"] is not None


@pytest.mark.parametrize("operation,status", [("complete", "labeled"), ("uncomplete", "unlabeled"),
                                              ("uncomplete", "in_progress"), ("assignment", "labeled")])
async def test_invalid_state_transitions_leave_version_and_history_unchanged(client, app, auth_headers, operation, status):
    graph = await _seed(app, [{"label_status": status,
                              "assignee_id": USERS["editor"] if status == "in_progress" else None,
                              "labeled_by": USERS["editor"] if status == "labeled" else None}])
    before = await _states(app, graph["image_ids"])
    response = await client.request("PATCH" if operation == "assignment" else "POST",
                                    f"/api/images/{graph['image_ids'][0]}/{operation}",
                                    headers=_headers(auth_headers, graph, "admin"),
                                    json={"assignee_id": OTHER_EDITOR} if operation == "assignment" else None)
    assert response.status_code == 409, response.text
    assert await _states(app, graph["image_ids"]) == before


async def test_distribute_is_deterministic_round_robin_and_preserves_existing_work(client, app, auth_headers):
    graph = await _seed(app, [
        {"stem": "z"}, {"stem": "a", "split": "valid"}, {"stem": "a", "split": None},
        {"stem": "a", "split": "train"}, {"stem": "b"},
        {"stem": "busy", "label_status": "in_progress", "assignee_id": USERS["admin"]},
        {"stem": "done", "label_status": "labeled", "labeled_by": USERS["editor"]},
        {"stem": "already-assigned", "assignee_id": USERS["admin"]},
    ])
    before = await _states(app, graph["image_ids"])
    response = await client.post(f"/api/datasets/{graph['dataset_id']}/assignments/distribute",
                                 headers=_headers(auth_headers, graph, "admin"),
                                 json={"user_ids": [OTHER_EDITOR, USERS["editor"]]})
    assert response.status_code == 200, response.text
    assert response.json() == {"assigned": {str(OTHER_EDITOR): 3, str(USERS["editor"]): 2}}
    after = await _states(app, graph["image_ids"])
    for index, source_index in enumerate([2, 3, 1, 4, 0]):
        state = after[graph["image_ids"][source_index]]
        assert state["assignee_id"] == [OTHER_EDITOR, USERS["editor"]][index % 2]
        assert state["label_status"] == "in_progress" and state["version"] == 8
        assert state["assigned_at"] is not None
    for image_id in graph["image_ids"][5:]:
        assert after[image_id] == before[image_id]


async def test_distribution_skips_locked_candidates_without_overwriting_or_waiting(client, app, auth_headers):
    graph = await _seed(app, [{"stem": "first"}, {"stem": "second"}])
    async with app.state.session_factory() as locker:
        await locker.scalar(select(Image).where(Image.id == graph["image_ids"][0]).with_for_update())
        response = await asyncio.wait_for(client.post(
            f"/api/datasets/{graph['dataset_id']}/assignments/distribute",
            headers=_headers(auth_headers, graph, "owner"), json={"user_ids": [USERS["editor"]]},
        ), timeout=5)
        assert response.status_code == 200, response.text
        assert response.json() == {"assigned": {str(USERS["editor"]): 1}}
        await locker.rollback()
    states = await _states(app, graph["image_ids"])
    assert states[graph["image_ids"][0]]["version"] == 7
    assert states[graph["image_ids"][0]]["assignee_id"] is None
    assert states[graph["image_ids"][1]]["assignee_id"] == USERS["editor"]


@pytest.mark.parametrize("target_role", ["owner", "admin"])
async def test_owner_and_admin_are_eligible_assignment_targets(client, app, auth_headers, target_role):
    graph = await _seed(app, [{}, {}])
    headers = _headers(auth_headers, graph, "admin")
    target_id = USERS[target_role]
    assigned = await client.patch(f"/api/images/{graph['image_ids'][0]}/assignment",
                                  headers=headers, json={"assignee_id": target_id})
    _image_response(assigned, graph["image_ids"][0], "in_progress", target_id, 8)
    distributed = await client.post(f"/api/datasets/{graph['dataset_id']}/assignments/distribute",
                                    headers=headers, json={"user_ids": [target_id]})
    assert distributed.status_code == 200, distributed.text
    assert distributed.json() == {"assigned": {str(target_id): 1}}
    states = await _states(app, graph["image_ids"])
    assert all(row["assignee_id"] == target_id and row["version"] == 8 for row in states.values())


async def test_duplicate_distribution_targets_do_not_weight_the_round_robin(client, app, auth_headers):
    graph = await _seed(app, [{} for _ in range(6)])
    response = await client.post(f"/api/datasets/{graph['dataset_id']}/assignments/distribute",
                                 headers=_headers(auth_headers, graph, "admin"),
                                 json={"user_ids": [OTHER_EDITOR, OTHER_EDITOR, USERS["editor"]]})
    assert response.status_code == 200, response.text
    assert response.json() == {"assigned": {str(OTHER_EDITOR): 3, str(USERS["editor"]): 3}}
    states = await _states(app, graph["image_ids"])
    assert [states[image_id]["assignee_id"] for image_id in graph["image_ids"]] == [OTHER_EDITOR, USERS["editor"]] * 3


@pytest.mark.parametrize("operation", ["distribute", "assignment"])
@pytest.mark.parametrize("target", [USERS["viewer"], OUTSIDER])
async def test_assignments_reject_ineligible_targets_before_any_changes(client, app, auth_headers, operation, target):
    graph = await _seed(app, [{}, {}])
    before = await _states(app, graph["image_ids"])
    if operation == "distribute":
        response = await client.post(f"/api/datasets/{graph['dataset_id']}/assignments/distribute",
                                     headers=_headers(auth_headers, graph, "admin"),
                                     json={"user_ids": [USERS["editor"], target]})
    else:
        response = await client.patch(f"/api/images/{graph['image_ids'][0]}/assignment",
                                      headers=_headers(auth_headers, graph, "admin"), json={"assignee_id": target})
    assert response.status_code == 422, response.text
    assert await _states(app, graph["image_ids"]) == before


async def test_reclaim_is_scoped_and_reassignment_increments_each_changed_version(client, app, auth_headers):
    graph = await _seed(app, [
        {"label_status": "in_progress", "assignee_id": USERS["editor"]},
        {"label_status": "in_progress", "assignee_id": USERS["editor"]},
        {"label_status": "in_progress", "assignee_id": OTHER_EDITOR},
        {"label_status": "labeled", "labeled_by": USERS["editor"]},
    ])
    other = await _seed(app, [{"label_status": "in_progress", "assignee_id": USERS["editor"]}],
                        workspace_id=graph["workspace_id"])
    ids = [*graph["image_ids"], *other["image_ids"]]
    before = await _states(app, ids)
    headers = _headers(auth_headers, graph, "admin")
    response = await client.post(f"/api/datasets/{graph['dataset_id']}/assignments/reclaim",
                                 headers=headers, json={"user_id": USERS["editor"]})
    assert response.status_code == 200 and response.json() == {"reclaimed": 2}, response.text
    after = await _states(app, ids)
    for image_id in graph["image_ids"][:2]:
        assert after[image_id]["label_status"] == "unlabeled"
        assert after[image_id]["assignee_id"] is None and after[image_id]["assigned_at"] is None
        assert after[image_id]["version"] == 8
    for image_id in [*graph["image_ids"][2:], *other["image_ids"]]:
        assert after[image_id] == before[image_id]
    target_id = graph["image_ids"][2]
    changed = await client.patch(f"/api/images/{target_id}/assignment", headers=headers, json={"assignee_id": USERS["editor"]})
    _image_response(changed, target_id, "in_progress", USERS["editor"], 8)
    cleared = await client.patch(f"/api/images/{target_id}/assignment", headers=headers, json={"assignee_id": None})
    _image_response(cleared, target_id, "unlabeled", None, 9)
    assert (await _states(app, [target_id]))[target_id]["assigned_at"] is None


@pytest.mark.parametrize("actor,operation,required", [
    ("viewer", "claim", "editor"), ("viewer", "complete", "editor"), ("viewer", "uncomplete", "editor"),
    ("viewer", "distribute", "admin"), ("editor", "distribute", "admin"),
    ("viewer", "reclaim", "admin"), ("editor", "reclaim", "admin"),
    ("viewer", "assignment", "admin"), ("editor", "assignment", "admin"),
])
async def test_assignment_role_matrix_rejects_mutations_without_changes(client, app, auth_headers, actor, operation, required):
    graph = await _seed(app, [{}])
    before = await _states(app, graph["image_ids"])
    method, path, body = _operation_request(graph, operation)
    response = await client.request(method, path, headers=_headers(auth_headers, graph, actor), json=body)
    assert response.status_code == 403, response.text
    assert response.json()["detail"] == {"code": "insufficient_role", "required": required, "actual": actor}
    assert await _states(app, graph["image_ids"]) == before


def _operation_request(graph, operation):
    if operation == "claim":
        return "POST", f"/api/datasets/{graph['dataset_id']}/claim", {"split": None}
    if operation == "progress":
        return "GET", f"/api/datasets/{graph['dataset_id']}/progress", None
    if operation in {"distribute", "reclaim"}:
        body = {"user_ids": [USERS["editor"]]} if operation == "distribute" else {"user_id": USERS["editor"]}
        return "POST", f"/api/datasets/{graph['dataset_id']}/assignments/{operation}", body
    return ("PATCH" if operation == "assignment" else "POST",
            f"/api/images/{graph['image_ids'][0]}/{operation}",
            {"assignee_id": USERS["editor"]} if operation == "assignment" else None)


@pytest.mark.parametrize("scope", ["foreign_resource", "nonmember"])
async def test_assignment_resources_are_404_outside_verified_workspace_even_for_viewers(client, app, auth_headers, scope):
    graph = await _seed(app, [{}])
    other = await _seed(app, [{}])
    headers = (_headers(auth_headers, other, "viewer") if scope == "foreign_resource"
               else _headers(auth_headers, graph, OUTSIDER))
    before = await _states(app, graph["image_ids"])
    for operation in ("claim", "progress", "complete", "uncomplete", "distribute", "reclaim", "assignment"):
        method, path, body = _operation_request(graph, operation)
        response = await client.request(method, path, headers=headers, json=body)
        assert response.status_code == 404, (operation, response.text)
    assert await _states(app, graph["image_ids"]) == before


@pytest.mark.parametrize("action", ["leave", "remove"])
async def test_member_departure_returns_in_progress_across_datasets_and_preserves_labeled_history(client, app, auth_headers, action):
    graph = await _seed(app, [
        {"label_status": "in_progress", "assignee_id": USERS["editor"]},
        {"label_status": "labeled", "labeled_by": USERS["editor"]},
        {"label_status": "in_progress", "assignee_id": OTHER_EDITOR},
    ])
    same_team = await _seed(app, [{"label_status": "in_progress", "assignee_id": USERS["editor"]}],
                            workspace_id=graph["workspace_id"])
    other_team = await _seed(app, [{"label_status": "in_progress", "assignee_id": USERS["editor"]}])
    ids = [*graph["image_ids"], *same_team["image_ids"], *other_team["image_ids"]]
    before = await _states(app, ids)
    suffix, actor = ("me", "editor") if action == "leave" else (str(USERS["editor"]), "admin")
    response = await client.delete(f"/api/workspaces/{graph['workspace_id']}/members/{suffix}",
                                   headers=_headers(auth_headers, graph, actor))
    assert response.status_code == 204, response.text
    after = await _states(app, ids)
    released = [graph["image_ids"][0], same_team["image_ids"][0]]
    for image_id in released:
        assert after[image_id]["label_status"] == "unlabeled"
        assert after[image_id]["assignee_id"] is None and after[image_id]["assigned_at"] is None
        assert after[image_id]["version"] == before[image_id]["version"] + 1
    for image_id in set(ids) - set(released):
        assert after[image_id] == before[image_id]
    async with app.state.session_factory() as session:
        assert await session.get(WorkspaceMember, (graph["workspace_id"], USERS["editor"])) is None
        assert (await session.get(WorkspaceMember, (other_team["workspace_id"], USERS["editor"]))).role == "editor"
    progress = await client.get(f"/api/datasets/{graph['dataset_id']}/progress",
                                headers=_headers(auth_headers, graph, "admin"))
    assert progress.status_code == 200, progress.text
    former_member = next(row for row in progress.json()["members"] if row["user_id"] == USERS["editor"])
    assert (former_member["in_progress"], former_member["labeled"]) == (0, 1)


@pytest.mark.parametrize("previous_role", ["editor", "admin"])
async def test_viewer_demotion_after_distribution_returns_scoped_assignments_and_allows_other_editor_put(
    client, app, auth_headers, previous_role,
):
    target_id = USERS[previous_role]
    graph = await _seed(app, [
        {}, {}, {}, {}, {"label_status": "labeled", "labeled_by": target_id},
    ])
    same_team = await _seed(app, [{
        "label_status": "in_progress", "assignee_id": target_id,
        "labeled_by": OTHER_EDITOR,
    }], workspace_id=graph["workspace_id"])
    other_team = await _seed(app, [{"label_status": "in_progress", "assignee_id": target_id}])
    distributed = await client.post(
        f"/api/datasets/{graph['dataset_id']}/assignments/distribute",
        headers=_headers(auth_headers, graph, "owner"),
        json={"user_ids": [target_id, OTHER_EDITOR]},
    )
    assert distributed.status_code == 200, distributed.text
    assert distributed.json() == {"assigned": {str(target_id): 2, str(OTHER_EDITOR): 2}}
    ids = [*graph["image_ids"], *same_team["image_ids"], *other_team["image_ids"]]
    before = await _states(app, ids)
    released = [graph["image_ids"][0], graph["image_ids"][2], same_team["image_ids"][0]]
    assert all(before[image_id]["assignee_id"] == target_id for image_id in released)

    response = await client.patch(
        f"/api/workspaces/{graph['workspace_id']}/members/{target_id}",
        headers=_headers(auth_headers, graph, "owner"), json={"role": "viewer"},
    )
    assert response.status_code == 422, response.text
    assert await _states(app, ids) == before
    assert await _change_hidden_viewer_role(app, graph["workspace_id"], target_id) == {
        "user_id": target_id, "role": "viewer",
    }
    after = await _states(app, ids)
    for image_id in released:
        assert after[image_id] == {
            **before[image_id], "label_status": "unlabeled", "assignee_id": None,
            "assigned_at": None, "version": before[image_id]["version"] + 1,
        }, "returning work must preserve labeled_by and labeled_at"
    for image_id in set(ids) - set(released):
        assert after[image_id] == before[image_id]
    async with app.state.session_factory() as session:
        assert (await session.get(WorkspaceMember, (graph["workspace_id"], target_id))).role == "viewer"
        assert (await session.get(WorkspaceMember, (other_team["workspace_id"], target_id))).role == previous_role
    progress = await client.get(
        f"/api/datasets/{graph['dataset_id']}/progress", headers=_headers(auth_headers, graph, "owner"),
    )
    assert progress.status_code == 200, progress.text
    member = next(row for row in progress.json()["members"] if row["user_id"] == target_id)
    assert (member["in_progress"], member["labeled"]) == (0, 1)

    image_id = released[0]
    current = await client.get(
        f"/api/images/{image_id}/annotations", headers=_headers(auth_headers, graph, OTHER_EDITOR),
    )
    assert current.status_code == 200, current.text
    assert current.json()["version"] == after[image_id]["version"]
    saved = await client.put(
        f"/api/images/{image_id}/annotations", headers=_headers(auth_headers, graph, OTHER_EDITOR),
        json={"boxes": [], "expected_version": current.json()["version"],
              "expected_catalog_version": current.json()["catalog_version"]},
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["assignee_id"] == OTHER_EDITOR
    assert saved.json()["label_status"] == "in_progress"
    assert saved.json()["version"] == after[image_id]["version"] + 1


async def test_admin_to_editor_demotion_keeps_assignments_and_labeled_history_unchanged(client, app, auth_headers):
    graph = await _seed(app, [
        {}, {}, {"label_status": "labeled", "labeled_by": USERS["admin"]},
    ])
    same_team = await _seed(app, [{"label_status": "in_progress", "assignee_id": USERS["admin"]}],
                            workspace_id=graph["workspace_id"])
    distributed = await client.post(
        f"/api/datasets/{graph['dataset_id']}/assignments/distribute",
        headers=_headers(auth_headers, graph, "owner"), json={"user_ids": [USERS["admin"]]},
    )
    assert distributed.status_code == 200, distributed.text
    assert distributed.json() == {"assigned": {str(USERS["admin"]): 2}}
    ids = [*graph["image_ids"], *same_team["image_ids"]]
    before = await _states(app, ids)
    response = await client.patch(
        f"/api/workspaces/{graph['workspace_id']}/members/{USERS['admin']}",
        headers=_headers(auth_headers, graph, "owner"), json={"role": "editor"},
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"user_id": USERS["admin"], "role": "editor"}
    assert await _states(app, ids) == before


async def test_viewer_demotion_waits_for_distribution_workspace_lock_and_returns_new_assignments(
    client, app, auth_headers, monkeypatch,
):
    graph = await _seed(app, [{}, {}])
    acquired, release, contender = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original_scalar = AsyncSession.scalar
    paused = False

    async def scalar_with_workspace_pause(session, statement, *args, **kwargs):
        nonlocal paused
        sql = str(statement.compile(dialect=postgresql.dialect())).upper()
        workspace_query = "FROM WORKSPACES" in sql
        if workspace_query and "FOR UPDATE" in sql and acquired.is_set():
            contender.set()
        result = await original_scalar(session, statement, *args, **kwargs)
        if workspace_query and "FOR SHARE" in sql and not paused:
            # Pause only after PostgreSQL granted the distribution's real lock.
            # The role change must acquire its exclusive lock after this commits.
            paused = True
            acquired.set()
            await asyncio.wait_for(release.wait(), timeout=15)
        return result

    monkeypatch.setattr(AsyncSession, "scalar", scalar_with_workspace_pause)
    requests = [asyncio.create_task(client.post(
        f"/api/datasets/{graph['dataset_id']}/assignments/distribute",
        headers=_headers(auth_headers, graph, "owner"), json={"user_ids": [USERS["editor"]]},
    ))]
    try:
        await asyncio.wait_for(acquired.wait(), timeout=5)
        requests.append(asyncio.create_task(_change_hidden_viewer_role(
            app, graph["workspace_id"], USERS["editor"],
        )))
        await asyncio.wait_for(contender.wait(), timeout=5)
        assert not requests[1].done(), "membership change waits for the active distribution"
    finally:
        release.set()
        results = await asyncio.wait_for(asyncio.gather(*requests, return_exceptions=True), timeout=10)
    assert len(results) == 2
    for result in results:
        assert not isinstance(result, BaseException), result
    assert results[0].status_code == 200, results[0].text
    assert results[0].json() == {"assigned": {str(USERS["editor"]): 2}}
    assert results[1] == {"user_id": USERS["editor"], "role": "viewer"}
    states = await _states(app, graph["image_ids"])
    for state in states.values():
        assert state == {
            "label_status": "unlabeled", "assignee_id": None, "assigned_at": None,
            "labeled_by": None, "labeled_at": None, "version": 9,
        }, "distribution and subsequent demotion each advance version exactly once"


@pytest.mark.parametrize("auth_fails_once", [False, True])
async def test_account_deletion_returns_all_team_assignments_before_auth_and_preserves_labeler(client, app, auth_headers, monkeypatch, auth_fails_once):
    graphs = [await _seed(app, [
        {"label_status": "in_progress", "assignee_id": USERS["editor"]},
        {"label_status": "labeled", "labeled_by": USERS["editor"]},
        {"label_status": "in_progress", "assignee_id": OTHER_EDITOR},
    ]) for _ in range(2)]
    ids = [image_id for graph in graphs for image_id in graph["image_ids"]]
    before = await _states(app, ids)
    async with app.state.session_factory() as session:
        session.add(AdminUser(user_id=1))
        await session.commit()
    calls = []

    async def directory(_url):
        return [AuthDirectoryUser(id=USERS["editor"], email="assignment@test.local", username="assignment",
                                  created_at=NOW, login_methods=("이메일",))]

    async def delete_auth(_url, user_id):
        calls.append(user_id)
        states = await _states(app, ids)
        for graph in graphs:
            claimed, labeled, others = graph["image_ids"]
            assert states[claimed]["label_status"] == "unlabeled"
            assert states[claimed]["assignee_id"] is None and states[claimed]["assigned_at"] is None
            assert states[claimed]["version"] == 8
            assert states[labeled] == before[labeled]
            assert states[others] == before[others]
        if auth_fails_once and len(calls) == 1:
            raise RuntimeError("fixture auth outage after assignment release")

    monkeypatch.setattr("app.routers.admin.load_auth_directory", directory)
    monkeypatch.setattr("app.routers.admin.delete_auth_account", delete_auth)
    try:
        response = await client.delete(f"/api/admin/users/{USERS['editor']}?confirm=true", headers=auth_headers(1))
        assert response.status_code == (502 if auth_fails_once else 204), response.text
        if auth_fails_once:
            retried = await client.delete(f"/api/admin/users/{USERS['editor']}?confirm=true", headers=auth_headers(1))
            assert retried.status_code == 204, retried.text
        assert calls == [USERS["editor"]] * (2 if auth_fails_once else 1)
        async with app.state.session_factory() as session:
            for graph in graphs:
                assert await session.get(WorkspaceMember, (graph["workspace_id"], USERS["editor"])) is None
    finally:
        async with app.state.session_factory() as session:
            await session.execute(delete(AdminUser).where(AdminUser.user_id == 1))
            await session.commit()
