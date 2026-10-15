"""Version dataset class catalogs once per affected dataset and statement.

Revision ID: 0023_dataset_catalog_version
Revises: 0022_image_label_status
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0023_dataset_catalog_version"
down_revision: str | None = "0022_image_label_status"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if "class_catalog_version" not in {column["name"] for column in sa.inspect(op.get_bind()).get_columns("datasets")}:
        op.add_column("datasets", sa.Column(
            "class_catalog_version", sa.Integer(), nullable=False, server_default="0",
        ))
    # Replace either unpublished trigger layout without resetting existing versions.
    for name in ("dataset_classes_catalog_version", *(
        f"dataset_classes_catalog_version_{event}" for event in ("insert", "update", "delete")
    )):
        op.execute(sa.text(f"DROP TRIGGER IF EXISTS {name} ON dataset_classes"))
    op.execute(sa.text("""
        CREATE OR REPLACE FUNCTION bump_dataset_catalog_version() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE
            affected_ids integer[];
            affected_dataset_id integer;
        BEGIN
            IF TG_OP = 'INSERT' THEN
                SELECT array_agg(dataset_id ORDER BY dataset_id) INTO affected_ids
                FROM (SELECT DISTINCT dataset_id FROM new_catalog_rows) AS affected;
            ELSIF TG_OP = 'DELETE' THEN
                SELECT array_agg(dataset_id ORDER BY dataset_id) INTO affected_ids
                FROM (SELECT DISTINCT dataset_id FROM old_catalog_rows) AS affected;
            ELSE
                SELECT array_agg(dataset_id ORDER BY dataset_id) INTO affected_ids
                FROM (SELECT dataset_id FROM old_catalog_rows
                      UNION SELECT dataset_id FROM new_catalog_rows) AS affected;
            END IF;
            -- Stable dataset order also covers direct multi-dataset statements.
            FOREACH affected_dataset_id IN ARRAY COALESCE(affected_ids, ARRAY[]::integer[])
            LOOP
                UPDATE datasets SET class_catalog_version = class_catalog_version + 1
                WHERE id = affected_dataset_id;
            END LOOP;
            RETURN NULL;
        END;
        $$
    """))
    for event, transition in (
        ("INSERT", "NEW TABLE AS new_catalog_rows"),
        ("UPDATE", "OLD TABLE AS old_catalog_rows NEW TABLE AS new_catalog_rows"),
        ("DELETE", "OLD TABLE AS old_catalog_rows"),
    ):
        op.execute(sa.text(f"""
            CREATE TRIGGER dataset_classes_catalog_version_{event.lower()}
            AFTER {event} ON dataset_classes REFERENCING {transition}
            FOR EACH STATEMENT EXECUTE FUNCTION bump_dataset_catalog_version()
        """))


def downgrade() -> None:
    # The unpublished revision's earlier row-trigger form may exist in ws_test.
    for name in ("dataset_classes_catalog_version", *(
        f"dataset_classes_catalog_version_{event}" for event in ("insert", "update", "delete")
    )):
        op.execute(sa.text(f"DROP TRIGGER IF EXISTS {name} ON dataset_classes"))
    op.execute(sa.text("DROP FUNCTION bump_dataset_catalog_version()"))
    op.drop_column("datasets", "class_catalog_version")
