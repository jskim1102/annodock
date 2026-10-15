"""Replace user ownership with personal and team workspaces.

Revision ID: 0021_workspace_ownership
Revises: 0020_upload_size_bigint
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0021_workspace_ownership"
down_revision: str | None = "0020_upload_size_bigint"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OWNED_TABLES = ("projects", "datasets", "media_objects", "training_runs")


def _created_at() -> sa.Column:
    return sa.Column("created_at", sa.DateTime(timezone=True),
                     nullable=False, server_default=sa.func.now())


def upgrade() -> None:
    connection = op.get_bind()
    counts = {table: connection.scalar(sa.text(f"SELECT count(*) FROM {table}"))
              for table in ("orgs", "teams", "memberships")}
    if any(counts.values()):
        raise RuntimeError(f"Legacy collaboration tables must be empty: {counts}")
    for table in ("memberships", "teams", "orgs"):
        op.drop_table(table)

    op.create_table(
        "workspaces",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("created_by", sa.Integer(), nullable=False),
        _created_at(),
        sa.CheckConstraint("kind IN ('personal','team')", name="ck_workspaces_kind"),
    )
    op.create_index("uq_workspaces_personal_creator", "workspaces", ["created_by"],
                    unique=True, postgresql_where=sa.text("kind = 'personal'"))
    op.create_table(
        "workspace_members",
        sa.Column("workspace_id", sa.Integer(),
                  sa.ForeignKey("workspaces.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("user_id", sa.Integer(), primary_key=True),
        sa.Column("role", sa.String(32), nullable=False),
        _created_at(),
        sa.CheckConstraint("role IN ('owner','admin','editor','viewer')",
                           name="ck_workspace_members_role"),
    )
    op.create_index("ix_workspace_members_user_id", "workspace_members", ["user_id"])
    op.create_index("uq_workspace_members_owner", "workspace_members", ["workspace_id"],
                    unique=True, postgresql_where=sa.text("role = 'owner'"))
    op.create_table(
        "workspace_invites",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("workspace_id", sa.Integer(),
                  sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
        sa.Column("role", sa.String(32), nullable=False),
        sa.Column("token_hash", sa.CHAR(64), nullable=False),
        sa.Column("created_by", sa.Integer(), nullable=False),
        _created_at(),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("max_uses", sa.Integer(), nullable=True),
        sa.Column("uses", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("token_hash", name="uq_workspace_invites_token_hash"),
        sa.CheckConstraint("role IN ('admin','editor','viewer')",
                           name="ck_workspace_invites_role"),
        sa.CheckConstraint("uses >= 0", name="ck_workspace_invites_uses_nonnegative"),
    )
    op.execute(sa.text("""
        INSERT INTO workspaces (id, kind, name, created_by)
        SELECT owner_id, 'personal', '개인', owner_id
        FROM (
            SELECT owner_id FROM projects UNION SELECT owner_id FROM datasets
            UNION SELECT owner_id FROM media_objects
            UNION SELECT owner_id FROM training_runs
            UNION SELECT owner_id FROM user_storage
        ) AS owners
    """))
    op.execute(sa.text("""
        INSERT INTO workspace_members (workspace_id, user_id, role)
        SELECT id, created_by, 'owner' FROM workspaces
    """))
    op.execute(sa.text("""
        SELECT setval(pg_get_serial_sequence('workspaces', 'id'), max(id), true)
        FROM workspaces HAVING count(*) > 0
    """))
    for table in OWNED_TABLES:
        op.alter_column(table, "owner_id", new_column_name="workspace_id")
        op.create_foreign_key(f"fk_{table}_workspace_id_workspaces", table,
                              "workspaces", ["workspace_id"], ["id"], ondelete="RESTRICT")
    for table in ("projects", "datasets"):
        op.execute(f"ALTER TABLE {table} RENAME CONSTRAINT uq_{table}_owner_name "
                   f"TO uq_{table}_workspace_name")
    for table in ("media_objects", "training_runs"):
        op.execute(f"ALTER INDEX ix_{table}_owner_id RENAME TO ix_{table}_workspace_id")
    op.add_column("training_runs", sa.Column("created_by", sa.Integer(), nullable=True))
    op.execute("UPDATE training_runs SET created_by = workspace_id")
    op.alter_column("training_runs", "created_by", nullable=False)

    op.rename_table("user_storage", "workspace_storage")
    op.alter_column("workspace_storage", "owner_id", new_column_name="workspace_id")
    op.create_foreign_key("fk_workspace_storage_workspace_id_workspaces", "workspace_storage",
                          "workspaces", ["workspace_id"], ["id"], ondelete="CASCADE")
    for suffix in ("bytes_used_nonnegative", "quota_limit_positive"):
        op.execute(f"ALTER TABLE workspace_storage RENAME CONSTRAINT ck_user_storage_{suffix} "
                   f"TO ck_workspace_storage_{suffix}")
    op.alter_column("admin_users", "owner_id", new_column_name="user_id")


def downgrade() -> None:
    connection = op.get_bind()
    team_count = connection.scalar(sa.text("SELECT count(*) FROM workspaces WHERE kind = 'team'"))
    if team_count:
        raise RuntimeError(f"Cannot downgrade with team workspaces: {team_count}")

    # New personal workspace IDs need not equal user IDs. Remove references
    # and immediate uniqueness checks while remapping, including permutations
    # of IDs that would otherwise collide midway through one UPDATE.
    for table in OWNED_TABLES:
        op.drop_constraint(f"fk_{table}_workspace_id_workspaces", table, type_="foreignkey")
    op.drop_constraint("fk_workspace_storage_workspace_id_workspaces", "workspace_storage",
                       type_="foreignkey")
    for table in ("projects", "datasets"):
        op.drop_constraint(f"uq_{table}_workspace_name", table, type_="unique")
    op.drop_constraint("uq_datasets_upload_group_part", "datasets", type_="unique")
    op.drop_constraint("user_storage_pkey", "workspace_storage", type_="primary")
    for table in (*OWNED_TABLES, "workspace_storage"):
        op.execute(sa.text(f"""
            UPDATE {table} AS resource SET workspace_id = workspace.created_by
            FROM workspaces AS workspace WHERE resource.workspace_id = workspace.id
        """))
        op.alter_column(table, "workspace_id", new_column_name="owner_id")
    for table in ("projects", "datasets"):
        op.create_unique_constraint(f"uq_{table}_owner_name", table, ["owner_id", "name"])
    op.create_unique_constraint("uq_datasets_upload_group_part", "datasets",
                                ["owner_id", "upload_group_id", "upload_part_index"])
    op.create_primary_key("user_storage_pkey", "workspace_storage", ["owner_id"])
    for table in ("media_objects", "training_runs"):
        op.execute(f"ALTER INDEX ix_{table}_workspace_id RENAME TO ix_{table}_owner_id")
    op.drop_column("training_runs", "created_by")
    for suffix in ("bytes_used_nonnegative", "quota_limit_positive"):
        op.execute(f"ALTER TABLE workspace_storage RENAME CONSTRAINT ck_workspace_storage_{suffix} "
                   f"TO ck_user_storage_{suffix}")
    op.rename_table("workspace_storage", "user_storage")
    op.alter_column("admin_users", "user_id", new_column_name="owner_id")
    for table in ("workspace_invites", "workspace_members", "workspaces"):
        op.drop_table(table)
    _restore_legacy_skeleton()


def _restore_legacy_skeleton() -> None:
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
