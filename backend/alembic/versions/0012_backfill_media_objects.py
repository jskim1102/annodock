"""Backfill shared media objects for every existing account.

Revision ID: 0012_backfill_media_objects
Revises: 0011_shared_media_objects
Create Date: 2026-08-21
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0012_backfill_media_objects"
down_revision: str | None = "0011_shared_media_objects"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # A temporary mapping keeps this set-based for large installations while
    # allowing the media-object sequence to allocate collision-free IDs.
    op.execute(
        sa.text(
            """
            CREATE TEMPORARY TABLE media_object_backfill_map (
                image_id INTEGER PRIMARY KEY,
                media_object_id INTEGER NOT NULL UNIQUE
            ) ON COMMIT DROP
            """
        )
    )
    op.execute(
        sa.text(
            """
            INSERT INTO media_object_backfill_map (image_id, media_object_id)
            SELECT
                image.id,
                nextval(pg_get_serial_sequence('media_objects', 'id'))
            FROM images AS image
            WHERE image.media_object_id IS NULL
            ORDER BY image.id
            """
        )
    )
    op.execute(
        sa.text(
            """
            INSERT INTO media_objects (
                id,
                owner_id,
                created_by_dataset_id,
                original_bytes,
                display_bytes,
                thumb_bytes
            )
            SELECT
                mapping.media_object_id,
                dataset.owner_id,
                image.dataset_id,
                image.original_bytes,
                image.display_bytes,
                image.thumb_bytes
            FROM media_object_backfill_map AS mapping
            JOIN images AS image ON image.id = mapping.image_id
            JOIN datasets AS dataset ON dataset.id = image.dataset_id
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE images AS image
            SET media_object_id = mapping.media_object_id
            FROM media_object_backfill_map AS mapping
            WHERE image.id = mapping.image_id
            """
        )
    )
    op.alter_column(
        "images",
        "media_object_id",
        existing_type=sa.Integer(),
        nullable=False,
    )


def downgrade() -> None:
    # Retain backfilled references and data; only relax the invariant so the
    # previous application revision remains able to read every row.
    op.alter_column(
        "images",
        "media_object_id",
        existing_type=sa.Integer(),
        nullable=True,
    )
