"""Persist resumable upload ingestion checkpoints.

Revision ID: 0014_upload_ingest_checkpoints
Revises: 0013_user_storage_quota
Create Date: 2026-08-21
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0014_upload_ingest_checkpoints"
down_revision: str | None = "0013_user_storage_quota"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "upload_jobs",
        sa.Column(
            "ingest_cursor",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
    )
    op.create_check_constraint(
        "ck_upload_jobs_ingest_cursor_nonnegative",
        "upload_jobs",
        "ingest_cursor >= 0",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_upload_jobs_ingest_cursor_nonnegative",
        "upload_jobs",
        type_="check",
    )
    op.drop_column("upload_jobs", "ingest_cursor")
