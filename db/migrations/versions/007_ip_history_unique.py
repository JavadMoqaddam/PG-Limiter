"""Unique index on ip_history(username, ip)

Revision ID: 007_ip_history_unique
Revises: 006_drop_patterns
Create Date: 2026-08-30

db/models.py declares UniqueConstraint("username", "ip", name="uq_ip_history_username_ip")
on ip_history, but 001_initial created only a NON-unique index over the same pair, and
no later revision added the constraint. A database built by Base.metadata.create_all
therefore has it and a database built by the migrations does not - and create_all never
retrofits a constraint onto a table that already exists.

That matters because IPHistoryCRUD.bulk_record names exactly this pair as its ON CONFLICT
target. Without a unique index SQLite answers "ON CONFLICT clause does not match any
PRIMARY KEY or UNIQUE constraint", IPHistoryTracker.record_many catches it and logs a
warning, and the 24h/48h IP-history reports stay empty forever with no other symptom.

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '007_ip_history_unique'
down_revision = '006_drop_patterns'
branch_labels = None
depends_on = None

INDEX_NAME = 'uq_ip_history_username_ip'


def _already_unique(inspector) -> bool:
    """
    Whether (username, ip) is already enforced as unique.

    Two shapes have to be recognised. A database built by these migrations gets a
    named index, which shows up in ``get_indexes``. A database built by
    ``Base.metadata.create_all`` gets the model's ``UniqueConstraint`` as an inline
    table constraint, which SQLite backs with an auto-index named
    ``sqlite_autoindex_*`` - and SQLAlchemy filters those out of ``get_indexes``, so
    it only appears in ``get_unique_constraints``. Checking just the first would
    create a second, redundant unique index on such a database.
    """
    for index in inspector.get_indexes('ip_history'):
        if index['name'] == INDEX_NAME:
            return True
        if index.get('unique') and list(index.get('column_names') or []) == ['username', 'ip']:
            return True

    try:
        constraints = inspector.get_unique_constraints('ip_history')
    except NotImplementedError:
        return False
    for constraint in constraints:
        if list(constraint.get('column_names') or []) == ['username', 'ip']:
            return True
    return False


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    if 'ip_history' not in inspector.get_table_names():
        return

    if _already_unique(inspector):
        return

    # Rows written before the constraint existed may contain duplicate pairs, which
    # would make the unique index impossible to create. Keep the highest id of each
    # pair: ids grow monotonically, so that row carries the most recent last_seen.
    conn.execute(
        sa.text(
            'DELETE FROM ip_history WHERE id NOT IN ('
            'SELECT MAX(id) FROM ip_history GROUP BY username, ip)'
        )
    )

    op.create_index(INDEX_NAME, 'ip_history', ['username', 'ip'], unique=True)


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if 'ip_history' not in inspector.get_table_names():
        return
    if INDEX_NAME in {ix['name'] for ix in inspector.get_indexes('ip_history')}:
        op.drop_index(INDEX_NAME, table_name='ip_history')
