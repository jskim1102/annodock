"""Transactional per-user storage accounting and quota estimates."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ExportArtifact, Image, UserStorage


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


def _validated_amount(value: int, *, field: str) -> int:
    amount = int(value)
    if amount < 0:
        raise ValueError(f"{field} must be non-negative")
    return amount


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
    return QuotaStatus(
        used_bytes=await get_bytes_used(session, owner_id),
        limit_bytes=_validated_amount(limit_bytes, field="limit_bytes"),
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
