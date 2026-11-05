"""Add versioned Mirza Bot routes and incident-owned notifications without backfill.

Revision ID: 012_mirza_notifications
Revises: 011_daily_reports
"""

from alembic import op
import sqlalchemy as sa

revision = "012_mirza_notifications"
down_revision = "011_daily_reports"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    columns = {column["name"] for column in sa.inspect(bind).get_columns("punishment_facts")}
    if "notification_context" not in columns:
        op.add_column("punishment_facts", sa.Column("notification_context", sa.JSON(), nullable=True))
    if "warning_succeeded" not in columns:
        op.add_column(
            "punishment_facts", sa.Column("warning_succeeded", sa.Boolean(), nullable=False, server_default="0")
        )
    if "mirza_routes" not in tables:
        op.create_table(
            "mirza_routes",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("admin_username", sa.String(255), nullable=False, unique=True),
            sa.Column("base_url", sa.Text(), nullable=False, server_default=""),
            sa.Column("api_key", sa.String(512), nullable=False, server_default=""),
            sa.Column("message_text", sa.Text(), nullable=False, server_default=""),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default="0"),
            sa.Column("trigger_mode", sa.String(20), nullable=False, server_default="last"),
            sa.Column("selected_steps", sa.JSON(), nullable=False, server_default="[]"),
            sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        )
    if "mirza_notifications" not in tables:
        op.create_table(
            "mirza_notifications",
            sa.Column("incident_id", sa.String(64), primary_key=True),
            sa.Column("username", sa.String(255), nullable=False),
            sa.Column("applied_at", sa.Float(), nullable=False),
            sa.Column("context", sa.JSON(), nullable=False),
            sa.Column("state", sa.String(20), nullable=False, server_default="pending"),
            sa.Column("route_id", sa.String(36), nullable=True),
            sa.Column("route_revision", sa.Integer(), nullable=True),
            sa.Column("recipient", sa.String(255), nullable=True),
            sa.Column("base_url", sa.Text(), nullable=True),
            sa.Column("message_text", sa.Text(), nullable=True),
            sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("resolve_attempts", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("next_attempt_at", sa.Float(), nullable=False, server_default="0"),
            sa.Column("last_error", sa.String(64), nullable=True),
        )
    indexes = {index["name"] for index in sa.inspect(bind).get_indexes("mirza_notifications")}
    for name, fields in (
        ("ix_mirza_notifications_state_next_attempt", ["state", "next_attempt_at"]),
        ("ix_mirza_notifications_route_revision", ["route_id", "route_revision"]),
    ):
        if name not in indexes:
            op.create_index(name, "mirza_notifications", fields)


def downgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    for table in ("mirza_notifications", "mirza_routes"):
        if table in tables:
            op.drop_table(table)
    if "punishment_facts" in tables:
        columns = {column["name"] for column in sa.inspect(bind).get_columns("punishment_facts")}
        for column in ("warning_succeeded", "notification_context"):
            if column in columns:
                op.drop_column("punishment_facts", column)
