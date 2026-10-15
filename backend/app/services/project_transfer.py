"""Move project ownership and physical storage accounting in one transaction."""

from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import CurrentWorkspace, check_workspace_role
from app.models import (
    Dataset, ExportArtifact, Image, MediaObject, Project, TrainingRun,
    UploadBatch, UploadJob, UploadSession, Workspace, WorkspaceMember,
)
from app.services.quota import QuotaStatus, _locked_storage


def _conflict(code: str, names: list[str] | None = None, **detail: object) -> None:
    raise HTTPException(409, detail={"code": code, "names": names or [], **detail})


async def _reject_active_uploads(session: AsyncSession, project_id: int) -> None:
    names = set()
    for model, active in (
        (UploadBatch, UploadBatch.state == "open"),
        (UploadSession, UploadSession.state.not_in(("complete", "aborted"))),
        (UploadJob, UploadJob.state.in_(("queued", "running", "awaiting_class_resolution"))),
    ):
        names.update((await session.scalars(select(Dataset.name).join(
            model, model.dataset_id == Dataset.id,
        ).where(Dataset.project_id == project_id, active))).all())
    if names:
        _conflict("transfer_upload_active", sorted(names))


async def _name_conflicts(
    session: AsyncSession, workspace_id: int, project_name: str, dataset_names: list[str],
) -> list[str]:
    names = set((await session.scalars(select(Project.name).where(
        Project.workspace_id == workspace_id, Project.name == project_name,
    ))).all())
    names.update((await session.scalars(select(Dataset.name).where(
        Dataset.workspace_id == workspace_id, Dataset.name.in_(dataset_names),
    ))).all())
    return sorted(names)


async def _membership(
    session: AsyncSession, workspace_id: int, user_id: int, minimum_role: str,
) -> CurrentWorkspace:
    row = (await session.execute(
        select(Workspace.id, Workspace.kind, WorkspaceMember.role)
        .join(WorkspaceMember, WorkspaceMember.workspace_id == Workspace.id)
        .where(Workspace.id == workspace_id, WorkspaceMember.user_id == user_id)
    )).one_or_none()
    if row is None:
        raise HTTPException(404, detail="워크스페이스를 찾을 수 없습니다.")
    workspace = CurrentWorkspace(row.id, row.kind, row.role)
    check_workspace_role(workspace, minimum_role)
    return workspace


async def transfer_project(
    session: AsyncSession, *, project_id: int, source_workspace_id: int,
    target_workspace_id: int, user_id: int, limit_bytes: int,
) -> dict[str, int]:
    # Exclude role/member changes and other transfers, but allow FK KEY SHARE
    # checks by creators already holding a project/dataset lock. FOR UPDATE
    # would deadlock with those creators (Workspace -> Project -> Workspace).
    await session.execute(select(Workspace.id).where(
        Workspace.id.in_([source_workspace_id, target_workspace_id]),
    ).order_by(Workspace.id).with_for_update(key_share=True))
    if await session.scalar(select(Project.id).where(
        Project.id == project_id, Project.workspace_id == source_workspace_id,
    )) is None:
        raise HTTPException(404, detail="프로젝트를 찾을 수 없습니다.")
    await _membership(session, source_workspace_id, user_id, "owner")
    target = await _membership(session, target_workspace_id, user_id, "admin")
    if source_workspace_id == target_workspace_id:
        _conflict("transfer_same_workspace")
    # Completion workers acquire upload rows before updating their dataset.
    # Reject durable active work before taking parent locks to avoid inversion.
    await _reject_active_uploads(session, project_id)
    # Every resource is locked before storage accounting. In particular, the
    # two storage rows are always acquired in workspace-ID order, even for
    # simultaneous transfers in opposite directions.
    project = await session.scalar(select(Project).where(
        Project.id == project_id, Project.workspace_id == source_workspace_id,
    ).with_for_update().execution_options(populate_existing=True))
    if project is None:
        raise HTTPException(404, detail="프로젝트를 찾을 수 없습니다.")
    datasets = list((await session.scalars(select(Dataset).where(
        Dataset.project_id == project_id,
    ).order_by(Dataset.id).with_for_update().execution_options(populate_existing=True))).all())
    dataset_ids = [row.id for row in datasets]
    names_by_id = {row.id: row.name for row in datasets}
    await _reject_active_uploads(session, project_id)

    # Lock terminal rows too: a retry must not reopen an old upload while
    # ownership changes. A manifest without sessions is also an active upload.
    batches = (await session.scalars(select(UploadBatch).where(
        UploadBatch.dataset_id.in_(dataset_ids),
    ).order_by(UploadBatch.id).with_for_update())).all()
    uploads = (await session.scalars(select(UploadSession).where(
        UploadSession.dataset_id.in_(dataset_ids),
    ).order_by(UploadSession.id).with_for_update())).all()
    jobs = (await session.scalars(select(UploadJob).where(
        UploadJob.dataset_id.in_(dataset_ids),
    ).order_by(UploadJob.id).with_for_update())).all()
    busy_dataset_ids = (
        {row.dataset_id for row in batches if row.state == "open"}
        | {row.dataset_id for row in uploads if row.state not in {"complete", "aborted"}}
        | {row.dataset_id for row in jobs if row.state in {"queued", "running", "awaiting_class_resolution"}}
    )
    if busy_dataset_ids:
        _conflict("transfer_upload_active", [names_by_id[key] for key in sorted(busy_dataset_ids)])

    referenced_media = select(Image.media_object_id).where(Image.dataset_id.in_(dataset_ids))
    media = (await session.scalars(select(MediaObject).where(
        MediaObject.id.in_(referenced_media),
    ).order_by(MediaObject.id).with_for_update())).all()
    shared = (await session.execute(select(Dataset.id, Dataset.name).join(
        Image, Image.dataset_id == Dataset.id,
    ).where(
        Image.media_object_id.in_([row.id for row in media]),
        Dataset.id.not_in(dataset_ids),
    ).distinct().order_by(Dataset.id))).all()
    if shared:
        _conflict("transfer_shared_media", [row.name for row in shared],
                  datasets=[{"id": row.id, "name": row.name} for row in shared])

    # Ingest locks jobs before writing images; deletion locks media before
    # cascading to images. Keep image locks after both resource groups.
    await session.execute(select(Image.id).where(
        Image.dataset_id.in_(dataset_ids),
    ).order_by(Image.id).with_for_update(of=Image))
    target_members = list(await session.scalars(select(WorkspaceMember.user_id).where(
        WorkspaceMember.workspace_id == target_workspace_id,
    )))

    runs = (await session.scalars(select(TrainingRun).where(
        TrainingRun.dataset_id.in_(dataset_ids),
    ).order_by(TrainingRun.id).with_for_update())).all()
    busy_runs = {row.dataset_id for row in runs if row.state in {"running", "canceling"}}
    if busy_runs:
        _conflict("transfer_run_active", [names_by_id[key] for key in sorted(busy_runs)])

    project_name, dataset_names = project.name, [row.name for row in datasets]
    conflicting_names = await _name_conflicts(session, target_workspace_id, project_name, dataset_names)
    if conflicting_names:
        _conflict("transfer_name_conflict", sorted(conflicting_names))

    export_bytes = await session.scalar(select(func.coalesce(func.sum(ExportArtifact.archive_bytes), 0)).where(
        ExportArtifact.dataset_id.in_(dataset_ids),
    ))
    moved_bytes = (
        sum(row.original_bytes + row.display_bytes + row.thumb_bytes for row in media)
        + int(export_bytes or 0)
        + sum(row.artifact_bytes or 0 for row in runs)
    )
    storage = {}
    for workspace_id in sorted((source_workspace_id, target_workspace_id)):
        storage[workspace_id] = await _locked_storage(session, workspace_id)
    source_storage, target_storage = storage[source_workspace_id], storage[target_workspace_id]
    quota = QuotaStatus(
        used_bytes=target_storage.bytes_used,
        limit_bytes=target_storage.quota_limit_bytes or limit_bytes,
        required_bytes=moved_bytes, workspace_kind=target.kind,
    )
    if not quota.allowed:
        raise HTTPException(413, detail={
            "code": "transfer_quota_exceeded", "message": quota.detail,
            "required_bytes": moved_bytes, "remaining_bytes": quota.remaining_bytes,
        })

    # Return only assignments that have no member in the destination. Keep
    # annotation/labeled history and bump the version for open stale editors.
    await session.execute(update(Image).where(
        Image.dataset_id.in_(dataset_ids), Image.label_status == "in_progress",
        Image.assignee_id.not_in(target_members),
    ).values(label_status="unlabeled", assignee_id=None, assigned_at=None,
             version=Image.version + 1))
    # Keep IDs, paths, class versions and history. No files are copied.
    project.workspace_id = target_workspace_id
    for row in (*datasets, *media, *runs):
        row.workspace_id = target_workspace_id
    source_storage.bytes_used -= moved_bytes
    target_storage.bytes_used += moved_bytes
    try:
        await session.flush()
    except IntegrityError as error:
        # Concurrent destination creates can win after the name preflight.
        constraint = getattr(error.orig.__cause__, "constraint_name", None)
        if constraint not in {"uq_projects_workspace_name", "uq_datasets_workspace_name"}:
            raise
        await session.rollback()
        _conflict("transfer_name_conflict", await _name_conflicts(
            session, target_workspace_id, project_name, dataset_names,
        ))
    return {"project_id": project_id, "workspace_id": target_workspace_id, "moved_bytes": moved_bytes}
