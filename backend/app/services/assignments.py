"""Image assignments and labeling transitions inside the caller's transaction."""

from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import CurrentWorkspace, check_workspace_role
from app.models import Dataset, Image, Workspace, WorkspaceMember


def image_order():
    return Image.stem, Image.split.asc().nullsfirst(), Image.id


async def lock_actor_workspace(
    session: AsyncSession, workspace_id: int, actor_id: int, minimum_role: str,
) -> str:
    # Shared locks let image workers proceed concurrently, while member removal
    # takes the workspace's exclusive lock before returning their assignments.
    workspace = await session.scalar(select(Workspace).where(
        Workspace.id == workspace_id,
    ).with_for_update(read=True))
    role = await session.scalar(select(WorkspaceMember.role).where(
        WorkspaceMember.workspace_id == workspace_id,
        WorkspaceMember.user_id == actor_id,
    ))
    if workspace is None or role is None:
        raise HTTPException(status_code=404, detail="워크스페이스를 찾을 수 없습니다.")
    check_workspace_role(CurrentWorkspace(workspace.id, workspace.kind, role), minimum_role)
    return role


async def dataset_or_404(session: AsyncSession, dataset_id: int, workspace_id: int) -> Dataset:
    if not 0 < dataset_id <= 2**31 - 1:
        raise HTTPException(status_code=404, detail="데이터셋을 찾을 수 없습니다.")
    dataset = await session.scalar(select(Dataset).where(
        Dataset.id == dataset_id, Dataset.workspace_id == workspace_id,
    ))
    if dataset is None:
        raise HTTPException(status_code=404, detail="데이터셋을 찾을 수 없습니다.")
    return dataset


async def locked_image_or_404(session: AsyncSession, image_id: int, workspace_id: int) -> Image:
    if not 0 < image_id <= 2**31 - 1:
        raise HTTPException(status_code=404, detail="이미지를 찾을 수 없습니다.")
    image = await session.scalar(select(Image).join(Dataset).where(
        Image.id == image_id, Dataset.workspace_id == workspace_id,
    ).with_for_update(of=Image))
    if image is None:
        raise HTTPException(status_code=404, detail="이미지를 찾을 수 없습니다.")
    return image


def assign_image(image: Image, user_id: int | None) -> None:
    image.label_status = "in_progress" if user_id is not None else "unlabeled"
    image.assignee_id = user_id
    image.assigned_at = datetime.now(timezone.utc) if user_id is not None else None
    image.version += 1


async def claim_image(
    session: AsyncSession, *, dataset_id: int, actor_id: int, split: str | None,
) -> tuple[Image | None, int | None]:
    filters = [Image.dataset_id == dataset_id]
    if split is not None:
        filters.append(Image.split == split)
    image = await session.scalar(select(Image).where(
        *filters, Image.label_status == "unlabeled", Image.assignee_id.is_(None),
    ).order_by(*image_order()).limit(1).with_for_update(of=Image, skip_locked=True))
    if image is None:
        return None, None
    # Count position in the complete filtered Viewer list, including completed
    # and already assigned rows, rather than just in the claimable subset.
    ranked = select(
        Image.id, (func.row_number().over(order_by=image_order()) - 1).label("position"),
    ).where(*filters).subquery()
    position = await session.scalar(select(ranked.c.position).where(ranked.c.id == image.id))
    assign_image(image, actor_id)
    await session.flush()
    return image, position


def complete_image(image: Image, *, actor_id: int, role: str) -> None:
    if image.label_status == "in_progress":
        if image.assignee_id != actor_id and role not in {"admin", "owner"}:
            raise HTTPException(status_code=409, detail={
                "code": "image_assigned_to_other", "assignee_id": image.assignee_id,
            })
    elif image.label_status != "unlabeled" or image.assignee_id is not None:
        raise HTTPException(status_code=409, detail={"code": "invalid_label_state"})
    image.label_status = "labeled"
    image.labeled_by = actor_id
    image.labeled_at = datetime.now(timezone.utc)
    image.assignee_id = None
    image.assigned_at = None
    image.version += 1


def uncomplete_image(image: Image, *, actor_id: int, role: str) -> None:
    if image.label_status != "labeled":
        raise HTTPException(status_code=409, detail={"code": "invalid_label_state"})
    if image.labeled_by != actor_id and role not in {"admin", "owner"}:
        raise HTTPException(status_code=403, detail={"code": "label_completion_forbidden"})
    assign_image(image, actor_id)
    image.labeled_by = None
    image.labeled_at = None


async def eligible_assignees(session: AsyncSession, workspace_id: int, user_ids: list[int]) -> list[int]:
    distinct = list(dict.fromkeys(user_ids))
    eligible = set(await session.scalars(select(WorkspaceMember.user_id).where(
        WorkspaceMember.workspace_id == workspace_id,
        WorkspaceMember.user_id.in_(distinct),
        WorkspaceMember.role.in_(["editor", "admin", "owner"]),
    )))
    if any(user_id not in eligible for user_id in distinct):
        raise HTTPException(status_code=422, detail={"code": "invalid_assignee"})
    return distinct


async def distribute_images(
    session: AsyncSession, *, dataset_id: int, workspace_id: int, user_ids: list[int],
) -> dict[int, int]:
    assignees = await eligible_assignees(session, workspace_id, user_ids)
    images = (await session.scalars(select(Image).where(
        Image.dataset_id == dataset_id, Image.label_status == "unlabeled", Image.assignee_id.is_(None),
    ).order_by(*image_order()).with_for_update(of=Image, skip_locked=True))).all()
    assigned = dict.fromkeys(assignees, 0)
    for index, image in enumerate(images):
        user_id = assignees[index % len(assignees)]
        assign_image(image, user_id)
        assigned[user_id] += 1
    await session.flush()
    return assigned


async def reclaim_images(session: AsyncSession, *, dataset_ids, user_id: int) -> int:
    image_ids = list(await session.scalars(select(Image.id).where(
        Image.dataset_id.in_(dataset_ids), Image.assignee_id == user_id,
        Image.label_status == "in_progress",
    ).order_by(Image.id).with_for_update(of=Image)))
    if not image_ids:
        return 0
    rows = await session.scalars(update(Image).where(
        Image.id.in_(image_ids),
    ).values(label_status="unlabeled", assignee_id=None, assigned_at=None,
             version=Image.version + 1).returning(Image.id))
    return len(rows.all())


async def reclaim_workspace_assignments(session: AsyncSession, workspace_id: int, user_id: int) -> None:
    await reclaim_images(session, dataset_ids=select(Dataset.id).where(
        Dataset.workspace_id == workspace_id,
    ), user_id=user_id)


async def dataset_progress(session: AsyncSession, dataset_id: int) -> dict:
    rows = (await session.execute(select(
        Image.label_status, Image.assignee_id, Image.labeled_by, func.count(Image.id).label("count"),
    ).where(Image.dataset_id == dataset_id).group_by(
        Image.label_status, Image.assignee_id, Image.labeled_by,
    ))).all()
    result = {"total": 0, "unlabeled": 0, "in_progress": 0, "labeled": 0, "imported_labeled": 0}
    members = {}
    for row in rows:
        result["total"] += row.count
        result[row.label_status] += row.count
        user_id = None
        if row.label_status == "in_progress":
            user_id = row.assignee_id
        elif row.label_status == "labeled":
            user_id = row.labeled_by
            if user_id is None:
                result["imported_labeled"] += row.count
        if user_id is not None:
            member = members.setdefault(user_id, {"user_id": user_id, "in_progress": 0, "labeled": 0})
            member[row.label_status] += row.count
    return {**result, "members": [members[user_id] for user_id in sorted(members)]}
