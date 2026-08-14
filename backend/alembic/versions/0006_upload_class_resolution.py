"""Persist upload inputs and class-name resolution decisions.

Revision ID: 0006_upload_class_resolution
Revises: 0005_project_dataset_hierarchy
Create Date: 2026-08-11
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0006_upload_class_resolution"
down_revision: str | None = "0005_project_dataset_hierarchy"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "upload_jobs",
        sa.Column(
            "upload_ids",
            sa.JSON(),
            server_default="[]",
            nullable=False,
        ),
    )
    op.add_column(
        "upload_jobs",
        sa.Column("class_resolution_plan", sa.JSON(), nullable=True),
    )
    op.add_column(
        "upload_jobs",
        sa.Column("class_resolutions", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("upload_jobs", "class_resolutions")
    op.drop_column("upload_jobs", "class_resolution_plan")
    op.drop_column("upload_jobs", "upload_ids")
