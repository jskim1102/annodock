"""Distinguish ordinary merged datasets from training-purpose merges.

Revision ID: 0018_dataset_merge_purpose
Revises: 0017_durable_upload_batches
Create Date: 2026-09-04
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0018_dataset_merge_purpose"
down_revision: str | None = "0017_durable_upload_batches"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "datasets",
        sa.Column("merge_purpose", sa.String(length=16), nullable=True),
    )
    # Historical merged snapshots predate an explicit purpose. A persisted
    # training run is the strongest available evidence that the snapshot was
    # used for training; all other historical snapshots remain plain merges.
    op.execute(
        sa.text(
            """
            UPDATE datasets
            SET merge_purpose = CASE
                WHEN EXISTS (
                    SELECT 1
                    FROM training_runs
                    WHERE training_runs.dataset_id = datasets.id
                ) THEN 'train'
                ELSE 'merge'
            END
            WHERE is_merged IS TRUE
            """
        )
    )
    op.create_check_constraint(
        "ck_datasets_merge_purpose",
        "datasets",
        "merge_purpose IS NULL OR "
        "(is_merged AND merge_purpose IN ('merge', 'train'))",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_datasets_merge_purpose",
        "datasets",
        type_="check",
    )
    op.drop_column("datasets", "merge_purpose")
