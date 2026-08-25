"""Mark selected-class extraction snapshots.

Revision ID: 0010_dataset_extracted_marker
Revises: 0009_admin_users
Create Date: 2026-08-20
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0010_dataset_extracted_marker"
down_revision: str | None = "0009_admin_users"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "datasets",
        sa.Column(
            "is_extracted",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    # Restore the former dense project catalog before removing the marker so
    # pre-0010 training code can still consume datasets created after upgrade.
    op.execute(
        sa.text(
            """
            INSERT INTO dataset_classes (dataset_id, class_id, name)
            SELECT dataset.id, project_class.class_id, project_class.name
            FROM datasets AS dataset
            JOIN project_classes AS project_class
              ON project_class.project_id = dataset.project_id
            WHERE dataset.is_extracted = true
            ON CONFLICT (dataset_id, class_id)
            DO UPDATE SET name = EXCLUDED.name
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE datasets AS dataset
            SET class_count = (
                SELECT count(*)
                FROM project_classes AS project_class
                WHERE project_class.project_id = dataset.project_id
            )
            WHERE dataset.is_extracted = true
            """
        )
    )
    op.drop_column("datasets", "is_extracted")
