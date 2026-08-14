"""Preserve whether an imported image had a matching label source.

Revision ID: 0008_image_label_source
Revises: 0007_training_parameters
Create Date: 2026-08-14
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0008_image_label_source"
down_revision: str | None = "0007_training_parameters"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Default true preserves legacy training behavior when an old source
    # cannot be reconstructed. Exact import issues provide a safe backfill for
    # ordinary datasets, and merge insertion order lets us carry that value to
    # existing snapshot datasets.
    op.add_column(
        "images",
        sa.Column(
            "has_label_source",
            sa.Boolean(),
            server_default=sa.true(),
            nullable=False,
        ),
    )
    op.execute(
        sa.text(
            """
            UPDATE images AS image
            SET has_label_source = false
            FROM upload_jobs AS job, import_issues AS issue
            WHERE job.dataset_id = image.dataset_id
              AND issue.job_id = job.id
              AND issue.kind = 'image_without_label'
              AND issue.path = image.rel_path
            """
        )
    )
    op.execute(
        sa.text(
            """
            WITH source_images AS (
                SELECT
                    membership.merged_dataset_id,
                    source_image.has_label_source,
                    row_number() OVER (
                        PARTITION BY membership.merged_dataset_id
                        ORDER BY membership.position, source_image.id
                    ) AS ordinal
                FROM dataset_merge_sources AS membership
                JOIN images AS source_image
                  ON source_image.dataset_id = membership.source_dataset_id
            ),
            merged_images AS (
                SELECT
                    image.id,
                    image.dataset_id AS merged_dataset_id,
                    row_number() OVER (
                        PARTITION BY image.dataset_id
                        ORDER BY image.id
                    ) AS ordinal
                FROM images AS image
                JOIN datasets AS dataset ON dataset.id = image.dataset_id
                WHERE dataset.is_merged = true
            )
            UPDATE images AS target
            SET has_label_source = source.has_label_source
            FROM source_images AS source, merged_images AS merged
            WHERE target.id = merged.id
              AND source.merged_dataset_id = merged.merged_dataset_id
              AND source.ordinal = merged.ordinal
            """
        )
    )


def downgrade() -> None:
    op.drop_column("images", "has_label_source")
