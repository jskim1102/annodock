"""Transactional per-user storage accounting and quota estimates."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Dataset,
    ExportArtifact,
    Image,
    MediaObject,
    TrainingRun,
    UserStorage,
)


MIB = 1024**2
TRAINING_METADATA_BYTES_PER_EPOCH = 64 * 1024
EXPORT_ENTRY_OVERHEAD_BYTES = 256


class SyncCursor(Protocol):
    def execute(self, query: str, parameters: tuple[object, ...]) -> object: ...


@dataclass(frozen=True)
class QuotaStatus:
    used_bytes: int
    limit_bytes: int
    required_bytes: int

    @property
    def remaining_bytes(self) -> int:
        return max(0, self.limit_bytes - self.used_bytes)

    @property
    def allowed(self) -> bool:
        return self.required_bytes <= self.remaining_bytes

    @property
    def detail(self) -> str:
        return (
            "사용자 저장공간 쿼터를 초과합니다. "
            f"잔여 용량 {self.remaining_bytes:,} B, "
            f"필요 용량 {self.required_bytes:,} B입니다."
        )


@dataclass(frozen=True)
class DatasetStorageReleasePlan:
    """Physical bytes and shared objects released by one dataset deletion."""

    released_bytes: int
    orphan_media_objects: tuple[MediaObject, ...]


def _validated_amount(value: int, *, field: str) -> int:
    amount = int(value)
    if amount < 0:
        raise ValueError(f"{field} must be non-negative")
    return amount


def _validated_limit(value: int) -> int:
    limit = int(value)
    if limit <= 0:
        raise ValueError("limit_bytes must be positive")
    return limit


async def _locked_storage(
    session: AsyncSession,
    owner_id: int,
) -> UserStorage:
    await session.execute(
        postgresql_insert(UserStorage)
        .values(owner_id=owner_id, bytes_used=0)
        .on_conflict_do_nothing(index_elements=[UserStorage.owner_id])
    )
    storage = await session.scalar(
        select(UserStorage)
        .where(UserStorage.owner_id == owner_id)
        .with_for_update()
    )
    if storage is None:  # pragma: no cover - protected by the upsert
        raise RuntimeError("user storage row could not be created")
    return storage


async def get_bytes_used(session: AsyncSession, owner_id: int) -> int:
    """Read only the persisted counter; never scan the storage tree."""

    value = await session.scalar(
        select(UserStorage.bytes_used).where(UserStorage.owner_id == owner_id)
    )
    return int(value or 0)


async def set_quota_limit(
    session: AsyncSession,
    owner_id: int,
    limit_bytes: int,
) -> int:
    """Persist one user's override without overwriting their usage counter."""

    limit = _validated_limit(limit_bytes)
    await session.execute(
        postgresql_insert(UserStorage)
        .values(
            owner_id=owner_id,
            bytes_used=0,
            quota_limit_bytes=limit,
        )
        .on_conflict_do_update(
            index_elements=[UserStorage.owner_id],
            set_={
                "quota_limit_bytes": limit,
                "updated_at": func.now(),
            },
        )
    )
    return limit


async def get_referenced_bytes(session: AsyncSession, owner_id: int) -> int:
    """Return logical bytes referenced by all of one owner's resources.

    Shared media is intentionally counted once per ``Image`` row here.  This
    value answers how much material the datasets expose, while ``bytes_used``
    remains the physical quota counter.
    """

    image_bytes = await session.scalar(
        select(
            func.coalesce(
                func.sum(
                    Image.original_bytes
                    + Image.display_bytes
                    + Image.thumb_bytes
                ),
                0,
            )
        )
        .join(Dataset, Dataset.id == Image.dataset_id)
        .where(Dataset.owner_id == owner_id)
    )
    export_bytes = await session.scalar(
        select(func.coalesce(func.sum(ExportArtifact.archive_bytes), 0))
        .join(Dataset, Dataset.id == ExportArtifact.dataset_id)
        .where(Dataset.owner_id == owner_id)
    )
    run_bytes = await session.scalar(
        select(func.coalesce(func.sum(TrainingRun.artifact_bytes), 0)).where(
            TrainingRun.owner_id == owner_id,
            TrainingRun.artifacts_deleted_at.is_(None),
            TrainingRun.artifact_bytes.is_not(None),
        )
    )
    return (
        int(image_bytes or 0)
        + int(export_bytes or 0)
        + int(run_bytes or 0)
    )


async def increase_bytes_used(
    session: AsyncSession,
    owner_id: int,
    amount: int,
) -> int:
    amount = _validated_amount(amount, field="amount")
    storage = await _locked_storage(session, owner_id)
    storage.bytes_used += amount
    return int(storage.bytes_used)


async def decrease_bytes_used(
    session: AsyncSession,
    owner_id: int,
    amount: int,
) -> int:
    """Release accounted bytes, clamping legacy under-counted rows at zero."""

    amount = _validated_amount(amount, field="amount")
    storage = await _locked_storage(session, owner_id)
    storage.bytes_used = max(0, int(storage.bytes_used) - amount)
    return int(storage.bytes_used)


async def quota_status(
    session: AsyncSession,
    owner_id: int,
    *,
    limit_bytes: int,
    required_bytes: int,
) -> QuotaStatus:
    default_limit = _validated_limit(limit_bytes)
    row = (
        await session.execute(
            select(
                UserStorage.bytes_used,
                UserStorage.quota_limit_bytes,
            ).where(UserStorage.owner_id == owner_id)
        )
    ).one_or_none()
    used_bytes = int(row.bytes_used) if row is not None else 0
    override = row.quota_limit_bytes if row is not None else None
    return QuotaStatus(
        used_bytes=used_bytes,
        limit_bytes=(int(override) if override is not None else default_limit),
        required_bytes=_validated_amount(required_bytes, field="required_bytes"),
    )


async def dataset_accounted_bytes(
    session: AsyncSession,
    dataset_id: int,
) -> int:
    image_bytes = await session.scalar(
        select(
            func.coalesce(
                func.sum(
                    Image.original_bytes
                    + Image.display_bytes
                    + Image.thumb_bytes
                ),
                0,
            )
        ).where(Image.dataset_id == dataset_id)
    )
    export_bytes = await session.scalar(
        select(func.coalesce(func.sum(ExportArtifact.archive_bytes), 0)).where(
            ExportArtifact.dataset_id == dataset_id
        )
    )
    return int(image_bytes or 0) + int(export_bytes or 0)


async def plan_dataset_storage_release(
    session: AsyncSession,
    dataset_ids: list[int],
) -> DatasetStorageReleasePlan:
    """Lock shared media and calculate physical bytes a delete will release.

    Pre-0011 image rows have no media object and retain their legacy accounting
    behavior.  A shared object is released only when no image outside the
    deletion set references it.  Physical attribution moves to the lowest-id
    surviving dataset when its current creator is deleted.
    """

    target_ids = sorted(set(dataset_ids))
    if not target_ids:
        return DatasetStorageReleasePlan(0, ())

    legacy_image_bytes = await session.scalar(
        select(
            func.coalesce(
                func.sum(
                    Image.original_bytes
                    + Image.display_bytes
                    + Image.thumb_bytes
                ),
                0,
            )
        ).where(
            Image.dataset_id.in_(target_ids),
            Image.media_object_id.is_(None),
        )
    )
    export_bytes = await session.scalar(
        select(func.coalesce(func.sum(ExportArtifact.archive_bytes), 0)).where(
            ExportArtifact.dataset_id.in_(target_ids)
        )
    )
    media_object_ids = list(
        (
            await session.scalars(
                select(Image.media_object_id)
                .where(
                    Image.dataset_id.in_(target_ids),
                    Image.media_object_id.is_not(None),
                )
                .distinct()
                .order_by(Image.media_object_id)
            )
        ).all()
    )
    media_objects = (
        list(
            (
                await session.scalars(
                    select(MediaObject)
                    .where(MediaObject.id.in_(media_object_ids))
                    .order_by(MediaObject.id)
                    .with_for_update()
                )
            ).all()
        )
        if media_object_ids
        else []
    )
    survivor_rows = (
        (
            await session.execute(
                select(
                    Image.media_object_id,
                    func.min(Image.dataset_id),
                )
                .where(
                    Image.media_object_id.in_(media_object_ids),
                    Image.dataset_id.not_in(target_ids),
                )
                .group_by(Image.media_object_id)
            )
        ).all()
        if media_object_ids
        else []
    )
    surviving_dataset_by_object = {
        int(media_object_id): int(dataset_id)
        for media_object_id, dataset_id in survivor_rows
    }
    target_id_set = set(target_ids)
    orphan_media_objects: list[MediaObject] = []
    released_bytes = int(legacy_image_bytes or 0) + int(export_bytes or 0)
    for media_object in media_objects:
        surviving_dataset_id = surviving_dataset_by_object.get(media_object.id)
        if surviving_dataset_id is None:
            released_bytes += (
                int(media_object.original_bytes)
                + int(media_object.display_bytes)
                + int(media_object.thumb_bytes)
            )
            orphan_media_objects.append(media_object)
            continue
        if (
            media_object.created_by_dataset_id is None
            or media_object.created_by_dataset_id in target_id_set
        ):
            media_object.created_by_dataset_id = surviving_dataset_id

    return DatasetStorageReleasePlan(
        released_bytes=released_bytes,
        orphan_media_objects=tuple(orphan_media_objects),
    )


async def apply_dataset_storage_release(
    session: AsyncSession,
    plan: DatasetStorageReleasePlan,
) -> None:
    """Remove object metadata after target image rows have been deleted."""

    for media_object in plan.orphan_media_objects:
        await session.delete(media_object)


def estimate_training_artifact_bytes(
    *,
    weight_bytes: int,
    epochs: int,
) -> int:
    """Conservatively budget best/last weights plus metric/checkpoint metadata."""

    weight_bytes = _validated_amount(weight_bytes, field="weight_bytes")
    epochs = _validated_amount(epochs, field="epochs")
    return max(
        1,
        (weight_bytes * 2)
        + (max(1, epochs) * TRAINING_METADATA_BYTES_PER_EPOCH)
        + MIB,
    )


def path_tree_bytes(path: Path) -> int:
    """Measure one deletion target without following symbolic links."""

    try:
        if path.is_symlink():
            return 0
        if path.is_file():
            return path.stat().st_size
        if not path.is_dir():
            return 0
    except OSError:
        return 0

    total = 0
    with os.scandir(path) as entries:
        for entry in entries:
            if entry.is_symlink():
                continue
            if entry.is_file(follow_symlinks=False):
                total += entry.stat(follow_symlinks=False).st_size
            elif entry.is_dir(follow_symlinks=False):
                total += path_tree_bytes(Path(entry.path))
    return total


def increase_bytes_used_sync(
    cursor: SyncCursor,
    owner_id: int,
    amount: int,
) -> None:
    """Worker-side equivalent of the async transactional counter update."""

    amount = _validated_amount(amount, field="amount")
    cursor.execute(
        """
        INSERT INTO user_storage (owner_id, bytes_used)
        VALUES (%s, %s)
        ON CONFLICT (owner_id) DO UPDATE
        SET bytes_used = user_storage.bytes_used + EXCLUDED.bytes_used,
            updated_at = now()
        """,
        (owner_id, amount),
    )
