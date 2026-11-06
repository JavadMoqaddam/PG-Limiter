"""Keep Mirza Bot forum reports separate from customer delivery.

Revision ID: 015_notification_reports
Revises: 014_notification_messages
"""

from alembic import op

from db.notification_report_schema import ensure_notification_report_schema

revision = "015_notification_reports"
down_revision = "014_notification_messages"
branch_labels = None
depends_on = None


def upgrade():
    ensure_notification_report_schema(op.get_bind())


def downgrade():
    raise RuntimeError("Cannot downgrade notification reports; restore a full pre-upgrade backup of database and state")
