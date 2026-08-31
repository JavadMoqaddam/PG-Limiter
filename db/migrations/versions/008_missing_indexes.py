"""Create the indexes db/models.py declares but no migration ever made

Revision ID: 008_missing_indexes
Revises: 007_ip_history_unique
Create Date: 2026-08-31

Seven indexes exist only in db/models.py. A database built by
``Base.metadata.create_all`` has them; a database built by this migration chain
does not, and create_all never retrofits an index onto a table that already
exists - so every deployment that has been upgraded rather than freshly created
is missing all seven.

None of them affects correctness, which is why this is a separate revision from
007 and not part of it: the missing UNIQUE there broke an upsert, while these
only cost query time. They still cost it on every cycle:

  users(owner_username)                 admin-scoped user lookups and reports
  users(special_limit)                   the `special_limit IS NOT NULL` listings
  users(status, is_disabled_by_limiter)  the disable/re-enable sweep
  subnet_isp(cached_at)                  the ISP cache cleanup delete
  violation_history(username, timestamp) the per-user violation window
  ip_history(username)                   mostly covered by the composite index
  ip_history(username, last_seen)        the 24h/48h per-user history queries

Each one is created only if it is absent, so this is safe to run against a
create_all database that already has them.

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '008_missing_indexes'
down_revision = '007_ip_history_unique'
branch_labels = None
depends_on = None

# (index name, table, columns)
MISSING_INDEXES = [
    ('ix_users_owner_username', 'users', ['owner_username']),
    ('ix_users_special_limit', 'users', ['special_limit']),
    ('ix_users_status_disabled', 'users', ['status', 'is_disabled_by_limiter']),
    ('ix_subnet_isp_cached_at', 'subnet_isp', ['cached_at']),
    ('ix_violation_username_timestamp', 'violation_history', ['username', 'timestamp']),
    ('ix_ip_history_username', 'ip_history', ['username']),
    ('ix_ip_history_username_last_seen', 'ip_history', ['username', 'last_seen']),
]


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    tables = set(inspector.get_table_names())

    for name, table, columns in MISSING_INDEXES:
        if table not in tables:
            continue

        existing_columns = {column['name'] for column in inspector.get_columns(table)}
        if not set(columns).issubset(existing_columns):
            # A database this old is not one this chain can index; leave it alone
            # rather than fail the whole upgrade and keep the container down.
            continue

        if name in {index['name'] for index in inspector.get_indexes(table)}:
            continue

        op.create_index(name, table, columns, unique=False)


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    tables = set(inspector.get_table_names())

    for name, table, _columns in reversed(MISSING_INDEXES):
        if table not in tables:
            continue
        if name in {index['name'] for index in inspector.get_indexes(table)}:
            op.drop_index(name, table_name=table)
