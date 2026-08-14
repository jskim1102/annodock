"""Persist the complete Ultralytics training argument snapshot.

Revision ID: 0007_training_parameters
Revises: 0006_upload_class_resolution
Create Date: 2026-08-13
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0007_training_parameters"
down_revision: str | None = "0006_upload_class_resolution"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Empty objects mark pre-migration runs. The worker maps them to the exact
    # legacy Ultralytics defaults instead of changing an existing run recipe.
    op.add_column(
        "training_runs",
        sa.Column(
            "training_args",
            sa.JSON(),
            server_default="{}",
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("training_runs", "training_args")
