"""Track immutable image bundles shared across datasets.

Revision ID: 0011_shared_media_objects
Revises: 0010_dataset_extracted_marker
Create Date: 2026-08-21
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0011_shared_media_objects"
down_revision: str | None = "0010_dataset_extracted_marker"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "media_objects",
        sa.Column("id", sa.Integer(), primary_key=True),
        # auth-service is a separate database; owner_id is a logical reference.
        sa.Column("owner_id", sa.Integer(), nullable=False),
        sa.Column(
            "created_by_dataset_id",
            sa.Integer(),
            sa.ForeignKey("datasets.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "original_bytes",
            sa.BigInteger(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "display_bytes",
            sa.BigInteger(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "thumb_bytes",
            sa.BigInteger(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "original_bytes >= 0 AND display_bytes >= 0 AND thumb_bytes >= 0",
            name="ck_media_objects_bytes_nonnegative",
        ),
    )
    op.create_index(
        "ix_media_objects_owner_id",
        "media_objects",
        ["owner_id"],
    )
    op.create_index(
        "ix_media_objects_created_by_dataset_id",
        "media_objects",
        ["created_by_dataset_id"],
    )
    op.add_column(
        "images",
        sa.Column(
            "media_object_id",
            sa.Integer(),
            sa.ForeignKey("media_objects.id", ondelete="RESTRICT"),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_images_media_object_id",
        "images",
        ["media_object_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_images_media_object_id", table_name="images")
    op.drop_column("images", "media_object_id")
    op.drop_index(
        "ix_media_objects_created_by_dataset_id",
        table_name="media_objects",
    )
    op.drop_index("ix_media_objects_owner_id", table_name="media_objects")
    op.drop_table("media_objects")
