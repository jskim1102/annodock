"""Persist source balancing and temporal group keys for training splits.

Revision ID: 0019_training_split_provenance
Revises: 0018_dataset_merge_purpose
Create Date: 2026-09-04
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0019_training_split_provenance"
down_revision: str | None = "0018_dataset_merge_purpose"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "images",
        sa.Column("training_source_key", sa.Text(), nullable=True),
    )
    op.add_column(
        "images",
        sa.Column("training_group_key", sa.Text(), nullable=True),
    )

    # Physical media keeps the strongest surviving origin signal across
    # ordinary merge/extract snapshots. Automatic upload partitions deliberately
    # share one source key so they are balanced as one logical upload.
    op.execute(
        sa.text(
            """
            UPDATE images AS image
            SET training_source_key = CASE
                WHEN source_dataset.upload_group_id IS NOT NULL
                    THEN 'upload:' || source_dataset.upload_group_id::text
                ELSE 'dataset:' || source_dataset.id::text
            END
            FROM media_objects AS media, datasets AS source_dataset
            WHERE image.media_object_id = media.id
              AND source_dataset.id = media.created_by_dataset_id
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE images AS image
            SET training_source_key = CASE
                WHEN dataset.upload_group_id IS NOT NULL
                    THEN 'upload:' || dataset.upload_group_id::text
                ELSE 'dataset:' || dataset.id::text
            END
            FROM datasets AS dataset
            WHERE dataset.id = image.dataset_id
              AND image.training_source_key IS NULL
            """
        )
    )

    # Frame counters are an immutable leakage boundary. Still images use their
    # shared media id so exact copies also remain in one split.
    op.execute(
        sa.text(
            """
            UPDATE images
            SET training_group_key = CASE
                WHEN lower(stem) ~ '[_. -][0-9]{3,}$' THEN
                    'sequence:' || training_source_key || ':' ||
                    regexp_replace(
                        lower(stem),
                        '[_. -][0-9]{3,}$',
                        ''
                    )
                WHEN lower(stem) ~ '[^0-9][0-9]{6,}$' THEN
                    'sequence:' || training_source_key || ':' ||
                    regexp_replace(lower(stem), '[0-9]{6,}$', '')
                ELSE 'media:' || media_object_id::text
            END
            """
        )
    )

    # For an existing merged snapshot, balance by the direct datasets the user
    # selected. The underlying group key stays inherited from the source image,
    # which prevents an overlapping extract from leaking into another split.
    op.execute(
        sa.text(
            """
            WITH ranked_source AS (
                SELECT
                    target.id AS target_image_id,
                    CASE
                        WHEN source_dataset.upload_group_id IS NOT NULL
                            THEN 'upload:' || source_dataset.upload_group_id::text
                        ELSE 'dataset:' || source_dataset.id::text
                    END AS source_key,
                    source_image.training_group_key AS group_key,
                    row_number() OVER (
                        PARTITION BY target.id
                        ORDER BY merge_source.position, source_image.id
                    ) AS source_rank
                FROM images AS target
                JOIN dataset_merge_sources AS merge_source
                  ON merge_source.merged_dataset_id = target.dataset_id
                JOIN datasets AS source_dataset
                  ON source_dataset.id = merge_source.source_dataset_id
                JOIN images AS source_image
                  ON source_image.dataset_id = merge_source.source_dataset_id
                 AND source_image.media_object_id = target.media_object_id
            )
            UPDATE images AS target
            SET training_source_key = ranked_source.source_key,
                training_group_key = ranked_source.group_key
            FROM ranked_source
            WHERE ranked_source.target_image_id = target.id
              AND ranked_source.source_rank = 1
            """
        )
    )

    op.alter_column(
        "images",
        "training_source_key",
        existing_type=sa.Text(),
        nullable=False,
    )
    op.alter_column(
        "images",
        "training_group_key",
        existing_type=sa.Text(),
        nullable=False,
    )


def downgrade() -> None:
    op.drop_column("images", "training_group_key")
    op.drop_column("images", "training_source_key")
