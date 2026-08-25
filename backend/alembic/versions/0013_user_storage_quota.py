"""Add optional per-user storage quota overrides.

Revision ID: 0013_user_storage_quota
Revises: 0012_backfill_media_objects
Create Date: 2026-08-21
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0013_user_storage_quota"
down_revision: str | None = "0012_backfill_media_objects"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "user_storage",
        sa.Column("quota_limit_bytes", sa.BigInteger(), nullable=True),
    )
    op.create_check_constraint(
        "ck_user_storage_quota_limit_positive",
        "user_storage",
        "quota_limit_bytes IS NULL OR quota_limit_bytes > 0",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_user_storage_quota_limit_positive",
        "user_storage",
        type_="check",
    )
    op.drop_column("user_storage", "quota_limit_bytes")
