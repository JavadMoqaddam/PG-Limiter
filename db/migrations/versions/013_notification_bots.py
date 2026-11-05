"""Group exact panel owners under named Mirza Bot destinations without rewriting receipts.

Revision ID: 013_notification_bots
Revises: 012_mirza_notifications
"""

from alembic import op
import sqlalchemy as sa

revision = "013_notification_bots"
down_revision = "012_mirza_notifications"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    # SQLite legacy transaction mode does not begin a physical transaction for
    # DDL. Start one through a no-op write before the rename and owner copy.
    if bind.dialect.name == "sqlite":
        bind.execute(sa.text("UPDATE mirza_routes SET id = id WHERE 1 = 0"))
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("mirza_routes")}
    legacy = "admin_username" in columns
    if legacy:
        op.alter_column("mirza_routes", "admin_username", new_column_name="name")
    if "mirza_route_admins" not in inspector.get_table_names():
        op.create_table(
            "mirza_route_admins",
            sa.Column("admin_username", sa.String(255), primary_key=True),
            sa.Column("route_id", sa.String(36), sa.ForeignKey("mirza_routes.id", ondelete="CASCADE"), nullable=False),
        )
    indexes = {index["name"] for index in sa.inspect(bind).get_indexes("mirza_route_admins")}
    if "ix_mirza_route_admins_route_id" not in indexes:
        op.create_index("ix_mirza_route_admins_route_id", "mirza_route_admins", ["route_id"])
    # Only the old column proves these are pre-group configurations. Restamps
    # and create_all bootstraps must never restore deliberately removed owners.
    if legacy:
        bind.execute(
            sa.text("INSERT INTO mirza_route_admins (admin_username, route_id) SELECT name, id FROM mirza_routes")
        )


def downgrade() -> None:
    # v1.7.3 cannot read grouped contexts in either SQLite or the external
    # punishment-fact journal. SQL migration alone cannot roll back both stores,
    # even when the current bot memberships happen to fit the previous schema.
    raise RuntimeError("Cannot downgrade notification bots; restore a full pre-upgrade backup of database and state")
