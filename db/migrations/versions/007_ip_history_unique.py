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

Rows written before the constraint existed may contain duplicate pairs, which would make
the unique index impossible to create. Those duplicates are *merged*, not pruned. Keeping
only the highest id of each pair - which is what this revision used to do - silently threw
away every earlier row, so a user's first_seen jumped forward to whenever the surviving row
happened to be written and connection_count collapsed to that one row's tally. Whatever
the removed rows still hold is recovered instead of dropped:

  first_seen        MIN over the group   the earliest sighting is the real one
  last_seen         MAX over the group   the latest sighting is the real one
  connection_count  SUM over the group   every counted connection stays counted
  every other column   merged per column, not copied wholesale: the newest row's value
                       when it has one, otherwise the next row down the same ordering
                       that does. A node_name only an earlier row recorded therefore
                       survives rather than being overwritten with the newest row's NULL,
                       which is the same rule IPHistoryCRUD.record_ip() applies to a live
                       write - metadata that is absent does not erase what is on file.

The newest row of a group is its last_seen leader, ties broken on the highest id, and it
is that row that carries the merged values forward, keeping its own id.

The merge is staged: the aggregate is built and checked in full before a single source row
is deleted, so a failure part-way through cannot leave the table half-collapsed. Groups
that were never duplicated are not touched at all and keep their ids.

SQLite DDL under Alembic is non-transactional, and CREATE TABLE ... AS SELECT commits on
its own here because no transaction is open yet when it runs. The aggregating statements
that follow do open one, so a run that trips a guard rolls the aggregate back but leaves
the _ip_history_dedup staging table behind holding an unaggregated copy of each duplicate
group's newest row: real usernames and IPs, carrying that one row's own first_seen,
last_seen and connection_count rather than the merged values. No source row is affected.
That copy persists until the upgrade is retried, and the retry drops it before rebuilding,
so re-running the upgrade is the fix; an operator who never retries has to drop it by hand.

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '007_ip_history_unique'
down_revision = '006_drop_patterns'
branch_labels = None
depends_on = None

INDEX_NAME = 'uq_ip_history_username_ip'

# Staging table for the merged rows. Underscore-prefixed so it cannot collide with
# anything db/models.py declares, and dropped again before this revision returns.
STAGING_TABLE = '_ip_history_dedup'

# Which row of a duplicate group speaks first for every column that cannot be
# recomputed. ORDER BY rather than a MAX(last_seen) equi-join on purpose: rows written
# before the ORM defaults existed have a NULL last_seen, and "last_seen =
# MAX(last_seen)" is never true when both sides are NULL - such a group would silently
# produce no row at all. Under ORDER BY ... DESC SQLite sorts NULLs last, so a real
# timestamp always beats a missing one and an all-NULL group falls through to the
# highest id. One definition, used both to pick the winning row and to fall through
# past its NULLs, so the two cannot drift apart.
DETERMINISTIC_ORDER = 'ORDER BY {alias}.last_seen DESC, {alias}.id DESC'

# Recomputed over the whole group, so they are never carried from a single row.
RECOMPUTED_COLUMNS = ('first_seen', 'last_seen', 'connection_count')

# Not merged at all: username and ip are the group key, and id belongs to whichever row
# wins the group.
IDENTITY_COLUMNS = ('id', 'username', 'ip')

# IS rather than = throughout the key correlations. GROUP BY puts NULLs in one group
# while = never matches them, so with = a group keyed on a NULL username or ip would
# yield no staged row, the count guard would trip, and the upgrade would abort on every
# attempt with nothing an operator could do but edit the database by hand. 001_initial
# declares both columns NOT NULL, which is why that is unreachable today; nothing here
# needs to depend on it.
LATEST_ID_PER_DUPLICATE_GROUP = f'''
    SELECT (
        SELECT latest.id FROM ip_history AS latest
        WHERE latest.username IS duplicated.username AND latest.ip IS duplicated.ip
        {DETERMINISTIC_ORDER.format(alias="latest")}
        LIMIT 1
    )
    FROM (
        SELECT username, ip FROM ip_history
        GROUP BY username, ip HAVING COUNT(*) > 1
    ) AS duplicated
'''

# Correlates a duplicate group in the staging table back to its source rows.
SAME_PAIR = (
    f'source.username IS {STAGING_TABLE}.username AND source.ip IS {STAGING_TABLE}.ip'
)


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

    Uniqueness is checked on every branch, the name branch included. This function is
    also the post-condition after create_index, and an index that merely bears the
    right name proves nothing about whether ON CONFLICT can match it.
    """
    for index in inspector.get_indexes('ip_history'):
        if not index.get('unique'):
            continue
        if index['name'] == INDEX_NAME:
            return True
        if list(index.get('column_names') or []) == ['username', 'ip']:
            return True

    try:
        constraints = inspector.get_unique_constraints('ip_history')
    except NotImplementedError:
        return False
    for constraint in constraints:
        if list(constraint.get('column_names') or []) == ['username', 'ip']:
            return True
    return False


def _count(conn, statement: str) -> int:
    # scalar_one rather than "scalar() or 0": a duplicate-group count of zero is what
    # skips the merge entirely, so a count that did not come back has to raise instead
    # of quietly reading as "nothing to do".
    return int(conn.execute(sa.text(statement)).scalar_one())


def _duplicate_group_count(conn) -> int:
    """How many (username, ip) pairs appear on more than one row."""
    return _count(
        conn,
        'SELECT COUNT(*) FROM ('
        'SELECT 1 FROM ip_history GROUP BY username, ip HAVING COUNT(*) > 1)'
    )


def _mergeable_columns(conn) -> list:
    """
    Every ip_history column whose value has to be merged down the group.

    Read from the live schema rather than listed here, for the same reason the staging
    table is built with SELECT *: start.sh restamps to 006 without rolling the schema
    back, so this revision can run against a table carrying columns it has never heard
    of, and their values are exactly as reconstructable as node_name's.
    """
    skip = set(IDENTITY_COLUMNS) | set(RECOMPUTED_COLUMNS)
    return [
        column['name']
        for column in sa.inspect(conn).get_columns('ip_history')
        if column['name'] not in skip
    ]


def _merge_duplicate_pairs(conn) -> None:
    """
    Collapse every duplicate (username, ip) group onto one row, losing nothing.

    The staging table starts as a copy of each group's newest row - SELECT *, so every
    column travels, including its id and any column a later revision adds - and then
    every value that the rows about to be deleted still hold is merged into it: MIN,
    MAX and SUM for the three that can be recomputed, and a per-column fall-through
    down the same deterministic ordering for the rest.

    Nothing is deleted until that aggregate has been proven complete: exactly one row
    per duplicate group. If it is not, the revision raises instead of replacing the
    source rows, so the upgrade stops with the history still intact and unstamped.
    """
    expected_groups = _duplicate_group_count(conn)
    if not expected_groups:
        return

    mergeable = _mergeable_columns(conn)

    conn.execute(sa.text(f'DROP TABLE IF EXISTS {STAGING_TABLE}'))
    conn.execute(sa.text(
        f'CREATE TABLE {STAGING_TABLE} AS SELECT * FROM ip_history '
        f'WHERE id IN ({LATEST_ID_PER_DUPLICATE_GROUP})'
    ))

    # What the whole group says, rather than what its newest row happens to say.
    conn.execute(sa.text(
        f'UPDATE {STAGING_TABLE} SET '
        'first_seen = (SELECT MIN(source.first_seen) FROM ip_history AS source '
        f'WHERE {SAME_PAIR}), '
        'last_seen = (SELECT MAX(source.last_seen) FROM ip_history AS source '
        f'WHERE {SAME_PAIR}), '
        'connection_count = (SELECT SUM(COALESCE(source.connection_count, 0)) '
        f'FROM ip_history AS source WHERE {SAME_PAIR})'
    ))

    # Everything else the losing rows still hold. The newest row's value wins whenever
    # it has one; where it does not, the next row down the same ordering that does is
    # used, and a column no row in the group ever filled stays NULL. Taking these from
    # the newest row wholesale would erase a node_name that is still on file one row
    # down and leave the merged row claiming that node was never seen.
    for column in mergeable:
        conn.execute(sa.text(
            f'UPDATE {STAGING_TABLE} SET "{column}" = ('
            f'SELECT source."{column}" FROM ip_history AS source '
            f'WHERE {SAME_PAIR} AND source."{column}" IS NOT NULL '
            f'{DETERMINISTIC_ORDER.format(alias="source")} LIMIT 1)'
        ))

    staged = _count(conn, f'SELECT COUNT(*) FROM {STAGING_TABLE}')
    if staged != expected_groups:
        raise RuntimeError(
            f'007 aborted before touching ip_history: staged {staged} of '
            f'{expected_groups} duplicate (username, ip) group(s), so replacing the '
            'source rows now would lose history'
        )

    conn.execute(sa.text(
        'DELETE FROM ip_history WHERE EXISTS ('
        f'SELECT 1 FROM {STAGING_TABLE} AS merged '
        'WHERE merged.username IS ip_history.username AND merged.ip IS ip_history.ip)'
    ))
    # SELECT * on both sides: the staging table was created from ip_history in this
    # same run, so its columns are that table's columns in that table's order.
    conn.execute(sa.text(f'INSERT INTO ip_history SELECT * FROM {STAGING_TABLE}'))

    conn.execute(sa.text(f'DROP TABLE {STAGING_TABLE}'))

    remaining = _duplicate_group_count(conn)
    if remaining:
        raise RuntimeError(
            f'007 aborted: {remaining} duplicate (username, ip) group(s) survived the '
            'merge, so the unique index cannot be created'
        )


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    if 'ip_history' not in inspector.get_table_names():
        return

    if _already_unique(inspector):
        return

    _merge_duplicate_pairs(conn)

    op.create_index(INDEX_NAME, 'ip_history', ['username', 'ip'], unique=True)

    # Re-inspect rather than trust create_index: everything downstream - the upsert in
    # IPHistoryCRUD.bulk_record, the parity check, start.sh's schema probe - depends on
    # the pair really being enforced now, and a silent no-op here is the original bug.
    if not _already_unique(sa.inspect(conn)):
        raise RuntimeError(
            f'007 created {INDEX_NAME} but UNIQUE(username, ip) is still not enforced on '
            'ip_history; every ON CONFLICT (username, ip) upsert would keep raising'
        )


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if 'ip_history' not in inspector.get_table_names():
        return
    if INDEX_NAME in {ix['name'] for ix in inspector.get_indexes('ip_history')}:
        op.drop_index(INDEX_NAME, table_name='ip_history')
