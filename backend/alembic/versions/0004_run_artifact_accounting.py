"""Persist exact training artifact bytes for quota reconciliation.

Revision ID: 0004_run_artifact_accounting
Revises: 0003_multiuser_ownership
Create Date: 2026-08-06
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0004_run_artifact_accounting"
down_revision: str | None = "0003_multiuser_ownership"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Legacy runs remain NULL until their artifacts are touched by retention
    # or manual deletion; all new terminal transitions write an exact value.
    op.add_column(
        "training_runs",
        sa.Column("artifact_bytes", sa.BigInteger(), nullable=True),
    )
    op.create_check_constraint(
        "ck_training_runs_artifact_bytes_nonnegative",
        "training_runs",
        "artifact_bytes IS NULL OR artifact_bytes >= 0",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_training_runs_artifact_bytes_nonnegative",
        "training_runs",
        type_="check",
    )
    op.drop_column("training_runs", "artifact_bytes")
