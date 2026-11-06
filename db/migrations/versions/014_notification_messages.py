"""Assign named Mirza Bot messages to explicit punishment steps.

Revision ID: 014_notification_messages
Revises: 013_notification_bots
"""

from alembic import op

from db.notification_message_schema import ensure_message_list_schema

revision = "014_notification_messages"
down_revision = "013_notification_bots"
branch_labels = None
depends_on = None


def upgrade() -> None:
    ensure_message_list_schema(op.get_bind())


def downgrade() -> None:
    # Older code cannot read message-aware candidates in the external fact journal.
    raise RuntimeError(
        "Cannot downgrade notification messages; restore a full pre-upgrade backup of database and state"
    )
