"""Track image labeling state, assignment and optimistic versions.

Revision ID: 0022_image_label_status
Revises: 0021_workspace_ownership
Create Date: 2026-10-14
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0022_image_label_status"
down_revision: str | None = "0021_workspace_ownership"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("images", sa.Column(
        "label_status", sa.String(16), nullable=False, server_default="unlabeled",
    ))
    op.add_column("images", sa.Column("assignee_id", sa.Integer(), nullable=True))
    op.add_column("images", sa.Column("assigned_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("images", sa.Column("labeled_by", sa.Integer(), nullable=True))
    op.add_column("images", sa.Column("labeled_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("images", sa.Column("version", sa.Integer(), nullable=False, server_default="0"))
    # Imported labels have no known worker or completion time. Preserve that
    # distinction instead of attributing historical work to a current member.
    op.execute(sa.text("""
        UPDATE images SET label_status = 'labeled'
        WHERE has_label_source OR box_count > 0
    """))
    op.create_check_constraint(
        "ck_images_label_status", "images",
        "label_status IN ('unlabeled', 'in_progress', 'labeled')",
    )
    op.create_check_constraint(
        "ck_images_in_progress_assignee", "images",
        "label_status != 'in_progress' OR assignee_id IS NOT NULL",
    )
    op.create_index(
        "ix_images_dataset_label_status_assignee", "images",
        ["dataset_id", "label_status", "assignee_id"],
    )


def downgrade() -> None:
    # This discards collaboration history; production rollback needs the
    # explicit migration approval gate, even though existing boxes survive.
    op.drop_index("ix_images_dataset_label_status_assignee", table_name="images")
    op.drop_constraint("ck_images_in_progress_assignee", "images", type_="check")
    op.drop_constraint("ck_images_label_status", "images", type_="check")
    for column in ("version", "labeled_at", "labeled_by", "assigned_at", "assignee_id", "label_status"):
        op.drop_column("images", column)
