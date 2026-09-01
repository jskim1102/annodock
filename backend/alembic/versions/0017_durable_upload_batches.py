"""Add durable manifests for large, idempotent uploads.

Revision ID: 0017_durable_upload_batches
Revises: 0016_dataset_upload_partitions
Create Date: 2026-08-27
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0017_durable_upload_batches"
down_revision: str | None = "0016_dataset_upload_partitions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "upload_batches",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "dataset_id",
            sa.Integer(),
            sa.ForeignKey("datasets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("expected_file_count", sa.Integer(), nullable=False),
        sa.Column("expected_total_size", sa.BigInteger(), nullable=False),
        sa.Column("expected_extracted_size", sa.BigInteger(), nullable=False),
        sa.Column("largest_file_size", sa.BigInteger(), nullable=False),
        sa.Column(
            "state",
            sa.String(length=32),
            server_default="open",
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "expected_file_count > 0",
            name="ck_upload_batches_expected_file_count_positive",
        ),
        sa.CheckConstraint(
            "expected_total_size >= 0",
            name="ck_upload_batches_expected_total_size_nonnegative",
        ),
        sa.CheckConstraint(
            "expected_extracted_size >= 0",
            name="ck_upload_batches_expected_extracted_size_nonnegative",
        ),
        sa.CheckConstraint(
            "largest_file_size >= 0 "
            "AND largest_file_size <= expected_total_size",
            name="ck_upload_batches_largest_file_size_valid",
        ),
    )
    op.create_index(
        "ix_upload_batches_dataset_id",
        "upload_batches",
        ["dataset_id"],
    )
    op.add_column(
        "upload_sessions",
        sa.Column("upload_batch_id", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "upload_sessions",
        sa.Column("file_key", sa.String(length=64), nullable=True),
    )
    op.create_foreign_key(
        "fk_upload_sessions_upload_batch_id",
        "upload_sessions",
        "upload_batches",
        ["upload_batch_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_unique_constraint(
        "uq_upload_sessions_batch_file_key",
        "upload_sessions",
        ["upload_batch_id", "file_key"],
    )
    op.create_index(
        "ix_upload_sessions_upload_batch_id",
        "upload_sessions",
        ["upload_batch_id"],
    )
    op.add_column(
        "upload_jobs",
        sa.Column("upload_batch_id", sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        "fk_upload_jobs_upload_batch_id",
        "upload_jobs",
        "upload_batches",
        ["upload_batch_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_unique_constraint(
        "uq_upload_jobs_upload_batch_id",
        "upload_jobs",
        ["upload_batch_id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_upload_jobs_upload_batch_id",
        "upload_jobs",
        type_="unique",
    )
    op.drop_constraint(
        "fk_upload_jobs_upload_batch_id",
        "upload_jobs",
        type_="foreignkey",
    )
    op.drop_column("upload_jobs", "upload_batch_id")
    op.drop_index(
        "ix_upload_sessions_upload_batch_id",
        table_name="upload_sessions",
    )
    op.drop_constraint(
        "uq_upload_sessions_batch_file_key",
        "upload_sessions",
        type_="unique",
    )
    op.drop_constraint(
        "fk_upload_sessions_upload_batch_id",
        "upload_sessions",
        type_="foreignkey",
    )
    op.drop_column("upload_sessions", "file_key")
    op.drop_column("upload_sessions", "upload_batch_id")
    op.drop_index("ix_upload_batches_dataset_id", table_name="upload_batches")
    op.drop_table("upload_batches")
