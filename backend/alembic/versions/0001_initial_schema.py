"""Create the consolidated dataset trainer schema.

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-07-29
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0001_initial_schema"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "datasets",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=32), server_default="pending", nullable=False),
        sa.Column("storage_path", sa.Text(), nullable=False),
        sa.Column("image_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("annotation_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("class_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "is_merged",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("name", name="uq_datasets_name"),
    )

    op.create_table(
        "dataset_classes",
        sa.Column(
            "dataset_id",
            sa.Integer(),
            sa.ForeignKey("datasets.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("class_id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=255), nullable=False),
    )

    op.create_table(
        "images",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "dataset_id",
            sa.Integer(),
            sa.ForeignKey("datasets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("stem", sa.String(length=1024), nullable=False),
        sa.Column("filename", sa.String(length=1024), nullable=False),
        sa.Column("rel_path", sa.Text(), nullable=False),
        sa.Column("split", sa.String(length=64), nullable=True),
        sa.Column("width", sa.Integer(), nullable=False),
        sa.Column("height", sa.Integer(), nullable=False),
        sa.Column("file_path", sa.Text(), nullable=False),
        sa.Column("display_path", sa.Text(), nullable=True),
        sa.Column("thumb_path", sa.Text(), nullable=False),
        sa.Column("box_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("is_modified", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "dataset_id",
            "split",
            "stem",
            name="uq_images_dataset_split_stem",
            postgresql_nulls_not_distinct=True,
        ),
    )
    op.create_index(
        "ix_images_dataset_split_stem",
        "images",
        ["dataset_id", "split", "stem"],
        unique=False,
    )

    op.create_table(
        "annotations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "image_id",
            sa.Integer(),
            sa.ForeignKey("images.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("class_id", sa.Integer(), nullable=False),
        sa.Column("cx", sa.Float(), nullable=False),
        sa.Column("cy", sa.Float(), nullable=False),
        sa.Column("w", sa.Float(), nullable=False),
        sa.Column("h", sa.Float(), nullable=False),
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
    )
    op.create_index(
        "ix_annotations_image_id", "annotations", ["image_id"], unique=False
    )

    op.create_table(
        "upload_sessions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "dataset_id",
            sa.Integer(),
            sa.ForeignKey("datasets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("filename", sa.String(length=1024), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("chunk_size", sa.Integer(), nullable=False),
        sa.Column("received_chunks", sa.JSON(), server_default="[]", nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("state", sa.String(length=32), server_default="pending", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )

    op.create_table(
        "upload_jobs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "dataset_id",
            sa.Integer(),
            sa.ForeignKey("datasets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("state", sa.String(length=32), server_default="queued", nullable=False),
        sa.Column("phase", sa.String(length=64), server_default="queued", nullable=False),
        sa.Column("total", sa.Integer(), server_default="0", nullable=False),
        sa.Column("processed", sa.Integer(), server_default="0", nullable=False),
        sa.Column("failed", sa.Integer(), server_default="0", nullable=False),
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
    )

    op.create_table(
        "import_issues",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "job_id",
            sa.Integer(),
            sa.ForeignKey("upload_jobs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("detail", sa.Text(), nullable=False),
    )
    op.create_index("ix_import_issues_job_id", "import_issues", ["job_id"], unique=False)

    op.create_table(
        "training_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "dataset_id",
            sa.Integer(),
            sa.ForeignKey("datasets.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("dataset_name", sa.String(length=255), nullable=False),
        sa.Column("weights", sa.String(length=255), nullable=False),
        sa.Column("epochs", sa.Integer(), nullable=False),
        sa.Column("imgsz", sa.Integer(), nullable=False),
        sa.Column("batch", sa.Integer(), nullable=False),
        sa.Column("split_mode", sa.String(length=16), nullable=False),
        sa.Column("ratios", sa.JSON(), nullable=False),
        sa.Column("seed", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("pid", sa.Integer(), nullable=True),
        sa.Column("pid_started_at", sa.String(length=64), nullable=True),
        sa.Column("boot_id", sa.String(length=64), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("out_dir", sa.Text(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("artifacts_deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "split_mode IN ('2way','3way')",
            name="ck_training_runs_split_mode",
        ),
        sa.CheckConstraint(
            "state IN ('queued','running','canceling','done','failed','canceled')",
            name="ck_training_runs_state",
        ),
    )
    op.create_index(
        "uq_single_active_run",
        "training_runs",
        [sa.text("(true)")],
        unique=True,
        postgresql_where=sa.text("state IN ('running','canceling')"),
    )

    op.create_table(
        "run_images",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "run_id",
            sa.Integer(),
            sa.ForeignKey("training_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "image_id",
            sa.Integer(),
            sa.ForeignKey("images.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("split", sa.String(length=16), nullable=False),
        sa.Column("stem", sa.String(length=1024), nullable=False),
        sa.Column("filename", sa.String(length=1024), nullable=False),
        sa.Column("rel_path", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "split IN ('train','valid','test')",
            name="ck_run_images_split",
        ),
        sa.UniqueConstraint("run_id", "image_id", name="uq_run_images_run_image"),
    )

    op.create_table(
        "run_metrics",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "run_id",
            sa.Integer(),
            sa.ForeignKey("training_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("epoch", sa.Integer(), nullable=False),
        sa.Column("box_loss", sa.Float(), nullable=True),
        sa.Column("cls_loss", sa.Float(), nullable=True),
        sa.Column("dfl_loss", sa.Float(), nullable=True),
        sa.Column("map50", sa.Float(), nullable=True),
        sa.Column("map5095", sa.Float(), nullable=True),
        sa.Column("lr", sa.JSON(), nullable=True),
        sa.UniqueConstraint("run_id", "epoch", name="uq_run_metrics_run_epoch"),
    )

    op.create_table(
        "dataset_merge_sources",
        sa.Column(
            "merged_dataset_id",
            sa.Integer(),
            sa.ForeignKey("datasets.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "source_dataset_id",
            sa.Integer(),
            sa.ForeignKey("datasets.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "merged_dataset_id <> source_dataset_id",
            name="ck_dataset_merge_distinct",
        ),
        sa.UniqueConstraint("source_dataset_id", name="uq_dataset_merge_source"),
    )
    op.create_index(
        "ix_dataset_merge_source",
        "dataset_merge_sources",
        ["source_dataset_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_dataset_merge_source", table_name="dataset_merge_sources")
    op.drop_table("dataset_merge_sources")
    op.drop_table("run_metrics")
    op.drop_table("run_images")
    op.drop_index("uq_single_active_run", table_name="training_runs")
    op.drop_table("training_runs")
    op.drop_index("ix_import_issues_job_id", table_name="import_issues")
    op.drop_table("import_issues")
    op.drop_table("upload_jobs")
    op.drop_table("upload_sessions")
    op.drop_index("ix_annotations_image_id", table_name="annotations")
    op.drop_table("annotations")
    op.drop_index("ix_images_dataset_split_stem", table_name="images")
    op.drop_table("images")
    op.drop_table("dataset_classes")
    op.drop_table("datasets")
