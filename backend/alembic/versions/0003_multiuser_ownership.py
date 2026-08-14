"""Add multi-user ownership, accounting, and collaboration skeletons.

Revision ID: 0003_multiuser_ownership
Revises: 0002_storage_relpath
Create Date: 2026-08-05
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0003_multiuser_ownership"
down_revision: str | None = "0002_storage_relpath"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Existing rows predate authentication. In this local-first build, user 1 is
# the stable bootstrap owner for those rows. New rows receive no DB default:
# authenticated creation paths must always supply the verified subject.
LEGACY_OWNER_ID = 1


def _legacy_owner_statement(sql: str) -> sa.TextClause:
    return sa.text(sql).bindparams(
        sa.bindparam("legacy_owner_id", value=LEGACY_OWNER_ID)
    )


def _backfill_owner_ids() -> None:
    op.execute(
        _legacy_owner_statement(
            """
            UPDATE datasets
            SET owner_id = :legacy_owner_id
            WHERE owner_id IS NULL
            """
        )
    )
    op.execute(
        _legacy_owner_statement(
            """
            UPDATE training_runs AS run
            SET owner_id = COALESCE(dataset.owner_id, :legacy_owner_id)
            FROM datasets AS dataset
            WHERE run.dataset_id = dataset.id
              AND run.owner_id IS NULL
            """
        )
    )
    # Runs retain their dataset name when a dataset is deleted, so historical
    # rows can legitimately have dataset_id NULL. Keep those records and give
    # them the same explicit bootstrap owner instead of discarding history.
    op.execute(
        _legacy_owner_statement(
            """
            UPDATE training_runs
            SET owner_id = :legacy_owner_id
            WHERE owner_id IS NULL
            """
        )
    )


def upgrade() -> None:
    op.add_column(
        "datasets",
        sa.Column("owner_id", sa.Integer(), nullable=True),
    )
    op.add_column(
        "training_runs",
        sa.Column("owner_id", sa.Integer(), nullable=True),
    )
    _backfill_owner_ids()
    op.alter_column("datasets", "owner_id", nullable=False)
    op.alter_column("training_runs", "owner_id", nullable=False)

    op.drop_constraint("uq_datasets_name", "datasets", type_="unique")
    op.create_unique_constraint(
        "uq_datasets_owner_name",
        "datasets",
        ["owner_id", "name"],
    )
    op.create_index(
        "ix_training_runs_owner_id",
        "training_runs",
        ["owner_id"],
    )

    op.add_column(
        "images",
        sa.Column(
            "original_bytes",
            sa.BigInteger(),
            server_default=sa.text("0"),
            nullable=False,
        ),
    )
    op.add_column(
        "images",
        sa.Column(
            "display_bytes",
            sa.BigInteger(),
            server_default=sa.text("0"),
            nullable=False,
        ),
    )
    op.add_column(
        "images",
        sa.Column(
            "thumb_bytes",
            sa.BigInteger(),
            server_default=sa.text("0"),
            nullable=False,
        ),
    )
    op.create_check_constraint(
        "ck_images_bytes_nonnegative",
        "images",
        "original_bytes >= 0 AND display_bytes >= 0 AND thumb_bytes >= 0",
    )
    op.add_column(
        "annotations",
        sa.Column(
            "serialized_bytes",
            sa.BigInteger(),
            server_default=sa.text("0"),
            nullable=False,
        ),
    )
    op.create_check_constraint(
        "ck_annotations_serialized_bytes_nonnegative",
        "annotations",
        "serialized_bytes >= 0",
    )

    op.create_table(
        "exports",
        sa.Column(
            "job_id",
            sa.Integer(),
            sa.ForeignKey("upload_jobs.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "dataset_id",
            sa.Integer(),
            sa.ForeignKey("datasets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("archive_path", sa.Text(), nullable=False),
        sa.Column(
            "archive_bytes",
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
            "archive_bytes >= 0",
            name="ck_exports_archive_bytes_nonnegative",
        ),
    )
    op.create_index(
        "ix_exports_dataset_id",
        "exports",
        ["dataset_id"],
    )

    op.create_table(
        "user_storage",
        sa.Column(
            "owner_id",
            sa.Integer(),
            primary_key=True,
            autoincrement=False,
        ),
        sa.Column(
            "bytes_used",
            sa.BigInteger(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "bytes_used >= 0",
            name="ck_user_storage_bytes_used_nonnegative",
        ),
    )
    op.execute(
        _legacy_owner_statement(
            """
            INSERT INTO user_storage (owner_id, bytes_used)
            VALUES (:legacy_owner_id, 0)
            ON CONFLICT (owner_id) DO NOTHING
            """
        )
    )

    op.create_table(
        "orgs",
        sa.Column("id", sa.Integer(), primary_key=True),
        # Logical auth-service user reference: no cross-database FK.
        sa.Column("owner_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("owner_id", "name", name="uq_orgs_owner_name"),
    )
    op.create_table(
        "teams",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "org_id",
            sa.Integer(),
            sa.ForeignKey("orgs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("org_id", "name", name="uq_teams_org_name"),
    )
    op.create_table(
        "memberships",
        sa.Column(
            "team_id",
            sa.Integer(),
            sa.ForeignKey("teams.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        # Logical auth-service user reference: no cross-database FK.
        sa.Column("user_id", sa.Integer(), primary_key=True),
        sa.Column(
            "role",
            sa.String(length=32),
            server_default="viewer",
            nullable=False,
        ),
        sa.Column(
            "can_view",
            sa.Boolean(),
            server_default=sa.true(),
            nullable=False,
        ),
        sa.Column(
            "can_edit",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
        sa.Column(
            "can_manage",
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
        sa.CheckConstraint(
            "role IN ('owner','admin','editor','viewer')",
            name="ck_memberships_role",
        ),
    )
    op.create_index(
        "ix_memberships_user_id",
        "memberships",
        ["user_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_memberships_user_id", table_name="memberships")
    op.drop_table("memberships")
    op.drop_table("teams")
    op.drop_table("orgs")
    op.drop_table("user_storage")
    op.drop_index("ix_exports_dataset_id", table_name="exports")
    op.drop_table("exports")

    op.drop_constraint(
        "ck_annotations_serialized_bytes_nonnegative",
        "annotations",
        type_="check",
    )
    op.drop_column("annotations", "serialized_bytes")
    op.drop_constraint(
        "ck_images_bytes_nonnegative",
        "images",
        type_="check",
    )
    op.drop_column("images", "thumb_bytes")
    op.drop_column("images", "display_bytes")
    op.drop_column("images", "original_bytes")

    op.drop_index("ix_training_runs_owner_id", table_name="training_runs")
    op.drop_constraint(
        "uq_datasets_owner_name",
        "datasets",
        type_="unique",
    )
    op.create_unique_constraint("uq_datasets_name", "datasets", ["name"])
    op.drop_column("training_runs", "owner_id")
    op.drop_column("datasets", "owner_id")
