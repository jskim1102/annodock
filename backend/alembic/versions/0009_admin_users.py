"""Grant table for the read-only admin dashboard.

Revision ID: 0009_admin_users
Revises: 0008_image_label_source
Create Date: 2026-08-18
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0009_admin_users"
down_revision: str | None = "0008_image_label_source"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Accounts live in the read-only auth-service database, so admin grants
    # are kept here as logical references instead of a role column there.
    op.create_table(
        "admin_users",
        sa.Column(
            "owner_id", sa.Integer(), primary_key=True, autoincrement=False
        ),
        sa.Column(
            "granted_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )


def downgrade() -> None:
    op.drop_table("admin_users")
