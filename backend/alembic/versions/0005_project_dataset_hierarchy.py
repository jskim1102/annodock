"""Add the project-to-dataset hierarchy and project class catalog.

Revision ID: 0005_project_dataset_hierarchy
Revises: 0004_run_artifact_accounting
Create Date: 2026-08-07
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0005_project_dataset_hierarchy"
down_revision: str | None = "0004_run_artifact_accounting"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "projects",
        sa.Column("id", sa.Integer(), primary_key=True),
        # Logical auth-service user reference; cross-database FKs are not
        # possible because auth-service owns a separate database.
        sa.Column("owner_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("owner_id", "name", name="uq_projects_owner_name"),
    )
    op.create_table(
        "project_classes",
        sa.Column(
            "project_id",
            sa.Integer(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("class_id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("color", sa.String(length=7), nullable=False),
        sa.UniqueConstraint(
            "project_id",
            "name",
            name="uq_project_classes_project_name",
        ),
    )
    op.add_column(
        "datasets",
        sa.Column("project_id", sa.Integer(), nullable=True),
    )
    op.add_column(
        "datasets",
        sa.Column(
            "is_placeholder",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
    )

    # Every legacy dataset receives a same-named project. There was no project
    # identifier in the old schema, so inferring that one row belongs to a
    # differently named row would risk silently moving user data.
    op.execute(
        sa.text(
            """
            INSERT INTO projects (owner_id, name, created_at, updated_at)
            SELECT owner_id, name, created_at, created_at
            FROM datasets
            ORDER BY id
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE datasets AS dataset
            SET project_id = project.id
            FROM projects AS project
            WHERE project.owner_id = dataset.owner_id
              AND project.name = dataset.name
            """
        )
    )

    # The faulty UI created a pending Dataset row when the user intended to
    # create an empty Project. Retain those untouched rows rather than deleting
    # them, but hide them from dataset APIs and project children.
    op.execute(
        sa.text(
            """
            UPDATE datasets AS dataset
            SET is_placeholder = true
            WHERE dataset.status = 'pending'
              AND dataset.image_count = 0
              AND dataset.annotation_count = 0
              AND dataset.class_count = 0
              AND dataset.is_merged = false
              AND NOT EXISTS (
                  SELECT 1 FROM images WHERE images.dataset_id = dataset.id
              )
              AND NOT EXISTS (
                  SELECT 1 FROM dataset_classes
                  WHERE dataset_classes.dataset_id = dataset.id
              )
              AND NOT EXISTS (
                  SELECT 1 FROM upload_sessions
                  WHERE upload_sessions.dataset_id = dataset.id
              )
              AND NOT EXISTS (
                  SELECT 1 FROM upload_jobs
                  WHERE upload_jobs.dataset_id = dataset.id
              )
              AND NOT EXISTS (
                  SELECT 1 FROM training_runs
                  WHERE training_runs.dataset_id = dataset.id
              )
              AND NOT EXISTS (
                  SELECT 1 FROM exports WHERE exports.dataset_id = dataset.id
              )
              AND NOT EXISTS (
                  SELECT 1 FROM dataset_merge_sources
                  WHERE dataset_merge_sources.merged_dataset_id = dataset.id
                     OR dataset_merge_sources.source_dataset_id = dataset.id
              )
            """
        )
    )

    # Preserve existing class catalogs at the new project level. The palette is
    # deterministic so migrated classes render consistently across reloads.
    op.execute(
        sa.text(
            """
            INSERT INTO project_classes (project_id, class_id, name, color)
            SELECT
                dataset.project_id,
                dataset_class.class_id,
                dataset_class.name,
                CASE MOD(ABS(dataset_class.class_id), 8)
                    WHEN 0 THEN '#EF4444'
                    WHEN 1 THEN '#F59E0B'
                    WHEN 2 THEN '#22C55E'
                    WHEN 3 THEN '#3B82F6'
                    WHEN 4 THEN '#8B5CF6'
                    WHEN 5 THEN '#EC4899'
                    WHEN 6 THEN '#06B6D4'
                    ELSE '#84CC16'
                END
            FROM dataset_classes AS dataset_class
            JOIN datasets AS dataset ON dataset.id = dataset_class.dataset_id
            ON CONFLICT (project_id, class_id) DO NOTHING
            """
        )
    )

    op.alter_column("datasets", "project_id", nullable=False)
    op.create_foreign_key(
        "fk_datasets_project_id_projects",
        "datasets",
        "projects",
        ["project_id"],
        ["id"],
    )
    op.create_index(
        "ix_datasets_project_id",
        "datasets",
        ["project_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_datasets_project_id", table_name="datasets")
    op.drop_constraint(
        "fk_datasets_project_id_projects",
        "datasets",
        type_="foreignkey",
    )
    op.drop_column("datasets", "is_placeholder")
    op.drop_column("datasets", "project_id")
    op.drop_table("project_classes")
    op.drop_table("projects")
