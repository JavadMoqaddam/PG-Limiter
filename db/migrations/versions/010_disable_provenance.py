"""Persist the method and group snapshots of limiter disable incidents.

Revision ID: 010_disable_provenance
Revises: 009_drop_isp_cache
"""

from alembic import op
import sqlalchemy as sa

revision = "010_disable_provenance"
down_revision = "009_drop_isp_cache"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Support migrated databases and create_all schemas replayed from 006."""
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("users")}
    if "disable_provenance" not in columns:
        op.add_column("users", sa.Column("disable_provenance", sa.JSON(), nullable=True))


def downgrade() -> None:
    """Leave pre-existing disable flags, timers, and legacy groups intact."""
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("users")}
    if "disable_provenance" in columns:
        op.drop_column("users", "disable_provenance")
