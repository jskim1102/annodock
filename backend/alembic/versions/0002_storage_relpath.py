"""Persist storage-backed database paths relative to STORAGE_DIR.

Revision ID: 0002_storage_relpath
Revises: 0001_initial_schema
Create Date: 2026-08-05
"""

from __future__ import annotations

import os
from collections.abc import Callable, Sequence
from pathlib import Path, PurePosixPath

from alembic import op
import sqlalchemy as sa


revision: str = "0002_storage_relpath"
down_revision: str | None = "0001_initial_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_COLUMNS = (
    ("datasets", "storage_path", False),
    ("images", "file_path", False),
    ("images", "display_path", True),
    ("images", "thumb_path", False),
    ("training_runs", "out_dir", False),
)


def _storage_root() -> Path:
    raw_root = os.getenv("STORAGE_DIR")
    if not raw_root:
        raise RuntimeError("STORAGE_DIR is required for storage path migration")
    root = Path(raw_root).expanduser()
    if not root.is_absolute():
        root = Path(__file__).resolve().parents[2] / root
    return root.resolve()


def _safe_relative(value: str, root: Path) -> str:
    if not value or "\x00" in value or "\\" in value:
        raise ValueError("storage path is empty or not POSIX")

    raw = Path(value).expanduser()
    if raw.is_absolute():
        resolved = raw.resolve()
    else:
        relative = PurePosixPath(value)
        if any(part in {"", ".", ".."} for part in relative.parts):
            raise ValueError("storage path has an unsafe segment")
        resolved = (root / Path(*relative.parts)).resolve()

    if resolved == root or root not in resolved.parents:
        raise ValueError("storage path is outside STORAGE_DIR")
    return resolved.relative_to(root).as_posix()


def _absolute_value(value: str, root: Path) -> str:
    relative = _safe_relative(value, root)
    return str((root / Path(*PurePosixPath(relative).parts)).resolve())


def _collect_updates(
    convert: Callable[[str, Path], str],
) -> list[tuple[str, str, int, str]]:
    bind = op.get_bind()
    root = _storage_root()
    updates: list[tuple[str, str, int, str]] = []
    for table, column, nullable in _COLUMNS:
        rows = bind.execute(
            sa.text(f"SELECT id, {column} AS value FROM {table} ORDER BY id")
        ).mappings()
        for row in rows:
            value = row["value"]
            if value is None and nullable:
                continue
            if value is None:
                raise RuntimeError(f"{table}.{column} row {row['id']} is NULL")
            try:
                converted = convert(str(value), root)
            except ValueError as error:
                raise RuntimeError(
                    f"invalid {table}.{column} path for row {row['id']}: {error}"
                ) from error
            if converted != value:
                updates.append((table, column, int(row["id"]), converted))
    return updates


def _apply_updates(updates: list[tuple[str, str, int, str]]) -> None:
    bind = op.get_bind()
    for table, column, row_id, value in updates:
        bind.execute(
            sa.text(f"UPDATE {table} SET {column} = :value WHERE id = :row_id"),
            {"value": value, "row_id": row_id},
        )


def upgrade() -> None:
    _apply_updates(_collect_updates(_safe_relative))


def downgrade() -> None:
    _apply_updates(_collect_updates(_absolute_value))
