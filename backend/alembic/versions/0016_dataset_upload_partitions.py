"""Track automatically partitioned upload datasets.

Revision ID: 0016_dataset_upload_partitions
Revises: 0015_upload_job_image_progress
Create Date: 2026-08-24
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0016_dataset_upload_partitions"
down_revision: str | None = "0015_upload_job_image_progress"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "datasets",
        sa.Column("upload_group_id", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "datasets",
        sa.Column("upload_part_index", sa.Integer(), nullable=True),
    )
    op.add_column(
        "datasets",
        sa.Column("upload_part_count", sa.Integer(), nullable=True),
    )
    op.create_check_constraint(
        "ck_datasets_upload_partition_fields",
        "datasets",
        "(upload_group_id IS NULL AND upload_part_index IS NULL "
        "AND upload_part_count IS NULL) OR "
        "(upload_group_id IS NOT NULL AND upload_part_index >= 1 "
        "AND upload_part_count >= 2 "
        "AND upload_part_index <= upload_part_count)",
    )
    op.create_unique_constraint(
        "uq_datasets_upload_group_part",
        "datasets",
        ["owner_id", "upload_group_id", "upload_part_index"],
    )
    op.create_index(
        "ix_datasets_upload_group_id",
        "datasets",
        ["upload_group_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_datasets_upload_group_id", table_name="datasets")
    op.drop_constraint(
        "uq_datasets_upload_group_part",
        "datasets",
        type_="unique",
    )
    op.drop_constraint(
        "ck_datasets_upload_partition_fields",
        "datasets",
        type_="check",
    )
    op.drop_column("datasets", "upload_part_count")
    op.drop_column("datasets", "upload_part_index")
    op.drop_column("datasets", "upload_group_id")
