"""Allow individual upload sessions larger than two GiB.

Revision ID: 0020_upload_size_bigint
Revises: 0019_training_split_provenance
Create Date: 2026-10-06
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0020_upload_size_bigint"
down_revision: str | None = "0019_training_split_provenance"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "upload_sessions",
        "size",
        existing_type=sa.Integer(),
        type_=sa.BigInteger(),
        existing_nullable=False,
    )


def downgrade() -> None:
    # PostgreSQL rejects this conversion if any large session still exists,
    # preserving its size rather than silently truncating it.
    op.alter_column(
        "upload_sessions",
        "size",
        existing_type=sa.BigInteger(),
        type_=sa.Integer(),
        existing_nullable=False,
    )
