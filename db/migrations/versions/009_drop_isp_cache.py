"""Retire the disposable ISP subnet cache.

Revision ID: 009_drop_isp_cache
Revises: 008_missing_indexes
Create Date: 2026-10-22

ISP lookups now run only for a successful disable notification, once per address.
The cached subnet results are no longer read or written. Existing user settings
and violation history remain in their own tables.
"""

from alembic import op
import sqlalchemy as sa

revision = "009_drop_isp_cache"
down_revision = "008_missing_indexes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # create_all and installer repair runs may already have removed this table.
    if "subnet_isp" in sa.inspect(op.get_bind()).get_table_names():
        op.drop_table("subnet_isp")


def downgrade() -> None:
    # Cached results are disposable; a downgrade recreates the schema, not data.
    if "subnet_isp" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "subnet_isp",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("subnet", sa.String(length=50), nullable=False),
        sa.Column("isp", sa.String(length=255), nullable=True),
        sa.Column("country", sa.String(length=10), nullable=True),
        sa.Column("city", sa.String(length=100), nullable=True),
        sa.Column("region", sa.String(length=100), nullable=True),
        sa.Column("asn", sa.String(length=50), nullable=True),
        sa.Column("as_name", sa.String(length=255), nullable=True),
        sa.Column("cached_at", sa.DateTime(), nullable=True),
        sa.Column("hit_count", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_subnet_isp_subnet", "subnet_isp", ["subnet"], unique=True)
    op.create_index("ix_subnet_isp_country", "subnet_isp", ["country"], unique=False)
    op.create_index("ix_subnet_isp_isp", "subnet_isp", ["isp"], unique=False)
    op.create_index(
        "ix_subnet_isp_cached_at", "subnet_isp", ["cached_at"], unique=False
    )
