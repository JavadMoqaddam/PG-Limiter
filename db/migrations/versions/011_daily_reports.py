"""Add independent punishment evidence and durable daily-report ownership.

Revision ID: 011_daily_reports
Revises: 010_disable_provenance

Legacy violation history is deliberately not backfilled: duration zero does not
prove whether a warning, permanent ban, or failed side effect actually happened.
"""

from alembic import op
import sqlalchemy as sa

revision = "011_daily_reports"
down_revision = "010_disable_provenance"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    if "punishment_facts" not in tables:
        op.create_table(
            "punishment_facts",
            sa.Column("incident_id", sa.String(64), primary_key=True),
            sa.Column("username", sa.String(255), nullable=False),
            sa.Column("step_index", sa.Integer(), nullable=True),
            sa.Column("started_at", sa.Float(), nullable=False),
            sa.Column("applied_at", sa.Float(), nullable=True),
            sa.Column("disable_succeeded", sa.Boolean(), nullable=False),
            sa.Column("revoke_succeeded", sa.Boolean(), nullable=False),
            sa.Column("uuid_reset_succeeded", sa.Boolean(), nullable=False),
            sa.Column("state", sa.String(20), nullable=False),
        )
    if "ix_punishment_facts_applied_at" not in {
        index["name"] for index in sa.inspect(op.get_bind()).get_indexes("punishment_facts")
    }:
        op.create_index("ix_punishment_facts_applied_at", "punishment_facts", ["applied_at"])
    if "daily_report_runs" not in tables:
        op.create_table(
            "daily_report_runs",
            sa.Column("report_date", sa.String(10), primary_key=True),
            sa.Column("timezone", sa.String(255), nullable=False),
            sa.Column("start_utc", sa.Float(), nullable=False),
            sa.Column("end_utc", sa.Float(), nullable=False),
            sa.Column("payload", sa.Text(), nullable=True),
            sa.Column("event_id", sa.String(64), nullable=False, unique=True),
            sa.Column("state", sa.String(20), nullable=False),
            sa.Column("chat_id", sa.Integer(), nullable=False),
            sa.Column("message_thread_id", sa.Integer(), nullable=False),
            sa.Column("monitoring_revision", sa.Integer(), nullable=False),
            sa.Column("destination_revision", sa.Integer(), nullable=False),
            sa.Column("message_id", sa.Integer(), nullable=True),
        )
    if "daily_report_checkpoint" not in tables:
        op.create_table(
            "daily_report_checkpoint",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("closed_date", sa.String(10), nullable=False),
            sa.Column("enabled", sa.Boolean(), nullable=False),
        )


def downgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    for table in ("daily_report_checkpoint", "daily_report_runs", "punishment_facts"):
        if table in tables:
            op.drop_table(table)
