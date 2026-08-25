"""Add image-specific upload progress counters.

Revision ID: 0015_upload_job_image_progress
Revises: 0014_upload_ingest_checkpoints
Create Date: 2026-08-21
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0015_upload_job_image_progress"
down_revision: str | None = "0014_upload_ingest_checkpoints"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "upload_jobs",
        sa.Column(
            "image_total",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
    )
    op.add_column(
        "upload_jobs",
        sa.Column(
            "image_processed",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
    )
    op.create_check_constraint(
        "ck_upload_jobs_image_total_nonnegative",
        "upload_jobs",
        "image_total >= 0",
    )
    op.create_check_constraint(
        "ck_upload_jobs_image_processed_nonnegative",
        "upload_jobs",
        "image_processed >= 0",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_upload_jobs_image_processed_nonnegative",
        "upload_jobs",
        type_="check",
    )
    op.drop_constraint(
        "ck_upload_jobs_image_total_nonnegative",
        "upload_jobs",
        type_="check",
    )
    op.drop_column("upload_jobs", "image_processed")
    op.drop_column("upload_jobs", "image_total")
