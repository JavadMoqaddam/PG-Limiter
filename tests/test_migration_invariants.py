"""Regression coverage for database migration invariants.

These tests drive the real Alembic chain over a real SQLite file - the same way
start.sh and CI do - because the bugs they guard against only exist in the gap
between db/models.py and the migration scripts. An in-process fake schema would
not reproduce them.
"""

import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

HISTORY_COLUMNS = (
    "username",
    "ip",
    "node_name",
    "inbound_protocol",
    "first_seen",
    "last_seen",
    "connection_count",
)

STAGING_TABLE = "_ip_history_dedup"

# Three divergent duplicate groups plus one row that is not duplicated at all.
#
#   alice  distinct last_seen, and the newest row has *lost* its node_name, so the
#          merge has to fall through to the row below it to keep "old-node"
#   bob    identical last_seen, so the tie must break on the highest id
#   carol  no duplicate, so the migration must leave the row (and its id) alone
#   erin   no node_name or protocol anywhere in the group, so the merge must leave
#          both NULL rather than invent a value
DIVERGENT_ROWS = [
    ("alice", "198.51.100.1", "old-node", "tcp", "2026-01-01 00:00:00", "2026-01-02 00:00:00", 2),
    ("alice", "198.51.100.1", None, "grpc", "2026-01-03 00:00:00", "2026-01-05 00:00:00", 4),
    ("bob", "198.51.100.2", "tie-old", "tcp", "2026-02-01 00:00:00", "2026-02-02 00:00:00", 3),
    ("bob", "198.51.100.2", "tie-new", "ws", "2026-02-01 12:00:00", "2026-02-02 00:00:00", 5),
    ("carol", "198.51.100.3", "solo-node", "tcp", "2026-03-01 00:00:00", "2026-03-02 00:00:00", 7),
    ("erin", "198.51.100.5", None, None, "2026-04-01 00:00:00", "2026-04-02 00:00:00", 1),
    ("erin", "198.51.100.5", None, None, "2026-04-03 00:00:00", "2026-04-04 00:00:00", 2),
]

# The one row per duplicate group whose values are authoritative: newest last_seen,
# ties broken on the highest id. Also exactly what an aborted run leaves staged.
NEWEST_ROWS = [DIVERGENT_ROWS[1], DIVERGENT_ROWS[3], DIVERGENT_ROWS[6]]

# first_seen/last_seen/connection_count are recomputed over the whole group; every
# optional column takes the newest row's value and falls through, per column, to the
# next row down the same ordering that still has one.
MERGED_ROWS = [
    ("alice", "198.51.100.1", "old-node", "grpc", "2026-01-01 00:00:00", "2026-01-05 00:00:00", 6),
    ("bob", "198.51.100.2", "tie-new", "ws", "2026-02-01 00:00:00", "2026-02-02 00:00:00", 8),
    ("carol", "198.51.100.3", "solo-node", "tcp", "2026-03-01 00:00:00", "2026-03-02 00:00:00", 7),
    ("erin", "198.51.100.5", None, None, "2026-04-01 00:00:00", "2026-04-04 00:00:00", 3),
]

# The shape rows written before the ORM defaults existed actually have, and the
# shape tools/ci_migration_checks.py seeds: no timestamps, sometimes no count.
UNTIMED_ROWS = [
    ("dana", "198.51.100.4", None, None, None, None, None),
    ("dana", "198.51.100.4", None, None, None, None, 5),
    ("dana", "198.51.100.4", "late-node", "grpc", None, None, None),
]

# A guard-tripping run, forced from outside the revision so 007 itself carries no
# test hook. One statement is rewritten: the duplicate-group COUNT whose result the
# staged aggregate is checked against. Reporting one group too many makes the staged
# aggregate look incomplete, which is the exact situation the guard between the
# aggregating UPDATE and the DELETE exists to refuse. Every other statement, and the
# whole rest of the chain, runs untouched.
ALEMBIC_WITH_INFLATED_GROUP_COUNT = '''
import sys

import sqlalchemy
from sqlalchemy.engine import Connection

COUNT_PREFIX = "SELECT COUNT(*) FROM ("
GROUPED_DUPLICATES = "HAVING COUNT(*) > 1"

_execute = Connection.execute
_injected = False


def execute(self, statement, *args, **kwargs):
    # Only a TextClause has .text; every other construct is passed straight through
    # untouched, and must not be coerced to a string or a bool on the way. The rewrite
    # applies to the FIRST duplicate-group count only - the one the staged rows are
    # checked against - so the post-merge count stays honest.
    global _injected
    sql = getattr(statement, "text", None)
    if (
        not _injected
        and isinstance(sql, str)
        and sql.startswith(COUNT_PREFIX)
        and GROUPED_DUPLICATES in sql
    ):
        _injected = True
        statement = sqlalchemy.text(
            sql.replace(COUNT_PREFIX, "SELECT COUNT(*) + 1 FROM (", 1)
        )
    return _execute(self, statement, *args, **kwargs)


Connection.execute = execute

from alembic.config import main

main(argv=sys.argv[1:])
'''


# The same trick used to record what 007 executes, in order, so the position of the
# staged-count guard relative to the first destructive statement can be asserted rather
# than assumed. Every statement runs untouched.
ALEMBIC_LOGGING_STATEMENTS = '''
import os
import sys

from sqlalchemy.engine import Connection

LOG_PATH = os.environ["STATEMENT_LOG"]

_execute = Connection.execute


def execute(self, statement, *args, **kwargs):
    sql = getattr(statement, "text", None)
    if isinstance(sql, str):
        with open(LOG_PATH, "a", encoding="utf-8") as log:
            log.write(" ".join(sql.split()) + "\\n")
    return _execute(self, statement, *args, **kwargs)


Connection.execute = execute

from alembic.config import main

main(argv=sys.argv[1:])
'''


def _alembic_env(db_path: Path) -> dict:
    env = os.environ.copy()
    env["DATABASE_URL"] = f"sqlite+aiosqlite:///{db_path}"
    return env


def run_alembic(db_path: Path, *arguments: str) -> subprocess.CompletedProcess:
    """Run one alembic command against ``db_path`` and fail loudly if it errors."""
    completed = subprocess.run(
        [sys.executable, "-m", "alembic", *arguments],
        cwd=PROJECT_ROOT,
        env=_alembic_env(db_path),
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, (
        f"alembic {' '.join(arguments)} exited {completed.returncode}\n"
        f"--- stdout ---\n{completed.stdout}\n--- stderr ---\n{completed.stderr}"
    )
    return completed


def run_alembic_with_inflated_group_count(
    db_path: Path, tmp_path: Path, *arguments: str
) -> subprocess.CompletedProcess:
    """Run alembic with 007's duplicate-group count reported one too high."""
    driver = tmp_path / "alembic_with_inflated_group_count.py"
    driver.write_text(ALEMBIC_WITH_INFLATED_GROUP_COUNT)
    return subprocess.run(
        [sys.executable, str(driver), *arguments],
        cwd=PROJECT_ROOT,
        env=_alembic_env(db_path),
        capture_output=True,
        text=True,
    )


def run_alembic_recording_statements(db_path: Path, tmp_path: Path, *arguments: str) -> list:
    """Run alembic and return every textual statement it executed, in order."""
    driver = tmp_path / "alembic_recording_statements.py"
    driver.write_text(ALEMBIC_LOGGING_STATEMENTS)
    log = tmp_path / "statements.log"
    env = _alembic_env(db_path)
    env["STATEMENT_LOG"] = str(log)
    completed = subprocess.run(
        [sys.executable, str(driver), *arguments],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, (
        f"alembic {' '.join(arguments)} exited {completed.returncode}\n"
        f"--- stdout ---\n{completed.stdout}\n--- stderr ---\n{completed.stderr}"
    )
    return log.read_text(encoding="utf-8").splitlines()


def positions(statements: list, prefix: str) -> list:
    found = [index for index, sql in enumerate(statements) if sql.startswith(prefix)]
    assert found, f"no statement starting {prefix!r} in:\n" + "\n".join(statements)
    return found


def seed_ip_history(db_path: Path, rows) -> None:
    with closing(sqlite3.connect(db_path)) as connection:
        connection.executemany(
            f"INSERT INTO ip_history ({', '.join(HISTORY_COLUMNS)}) "
            f"VALUES ({', '.join('?' * len(HISTORY_COLUMNS))})",
            rows,
        )
        connection.commit()


def read_history(db_path: Path) -> list:
    with closing(sqlite3.connect(db_path)) as connection:
        return connection.execute(
            f"SELECT {', '.join(HISTORY_COLUMNS)} FROM ip_history ORDER BY username, ip"
        ).fetchall()


def read_history_in_insertion_order(db_path: Path) -> list:
    """Every row as stored, ordered by id, so an untouched table compares equal to its seed."""
    with closing(sqlite3.connect(db_path)) as connection:
        return connection.execute(
            f"SELECT {', '.join(HISTORY_COLUMNS)} FROM ip_history ORDER BY id"
        ).fetchall()


def read_staging(db_path: Path) -> list:
    """Whatever the staging table holds, or None when it does not exist."""
    with closing(sqlite3.connect(db_path)) as connection:
        present = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (STAGING_TABLE,)
        ).fetchone()
        if not present:
            return None
        return connection.execute(
            f"SELECT {', '.join(HISTORY_COLUMNS)} FROM {STAGING_TABLE} ORDER BY id"
        ).fetchall()


def recorded_revisions(db_path: Path) -> list:
    with closing(sqlite3.connect(db_path)) as connection:
        return [
            row[0] for row in connection.execute("SELECT version_num FROM alembic_version")
        ]


def history_ids(db_path: Path) -> dict:
    with closing(sqlite3.connect(db_path)) as connection:
        return {
            (username, ip): row_id
            for row_id, username, ip in connection.execute(
                "SELECT id, username, ip FROM ip_history"
            )
        }


def unique_indexes_over(db_path: Path, table: str, columns) -> list:
    """
    Every unique index on ``table`` covering exactly ``columns``, in order.

    PRAGMA is used rather than the SQLAlchemy inspector on purpose: it reports a
    ``UniqueConstraint`` written inline by create_all (backed by a
    ``sqlite_autoindex_*``) as well as a named index, and SQLAlchemy filters the
    former out of ``get_indexes``.
    """
    wanted = list(columns)
    names = []
    with closing(sqlite3.connect(db_path)) as connection:
        listing = connection.execute(f"PRAGMA index_list('{table}')").fetchall()
        for entry in listing:
            name, is_unique = entry[1], entry[2]
            if not is_unique:
                continue
            covered = [
                info[2]
                for info in connection.execute(f"PRAGMA index_info('{name}')")
            ]
            if covered == wanted:
                names.append(name)
    return names


def unique_pair_present(db_path: Path, table: str, columns) -> bool:
    return bool(unique_indexes_over(db_path, table, columns))


def test_007_merges_duplicate_history_without_losing_information(tmp_path):
    """007 must fold duplicates together, not throw the losing rows away."""
    db_path = tmp_path / "history.db"
    run_alembic(db_path, "upgrade", "006_drop_patterns")
    seed_ip_history(db_path, DIVERGENT_ROWS)
    ids_before = history_ids(db_path)

    run_alembic(db_path, "upgrade", "007_ip_history_unique")

    merged = read_history(db_path)
    assert merged == MERGED_ROWS
    assert unique_pair_present(db_path, "ip_history", ("username", "ip"))
    # Optional metadata is merged per column, not taken wholesale from the newest
    # row: alice's newest row carries "grpc" and no node_name, so the protocol comes
    # from it and node_name falls through to the next row down the same ordering,
    # which still knows "old-node". Taking the newest row whole destroys it.
    assert (merged[0][2], merged[0][3]) == ("old-node", "grpc")
    # And nothing is invented where the whole group is silent.
    assert (merged[3][2], merged[3][3]) == (None, None)
    # A row that was never duplicated must not be rewritten at all.
    assert history_ids(db_path)[("carol", "198.51.100.3")] == ids_before[("carol", "198.51.100.3")]


def test_007_merges_rows_that_carry_no_timestamps(tmp_path):
    """Rows with NULL first_seen/last_seen/connection_count must still merge."""
    db_path = tmp_path / "untimed.db"
    run_alembic(db_path, "upgrade", "006_drop_patterns")
    seed_ip_history(db_path, UNTIMED_ROWS)

    run_alembic(db_path, "upgrade", "007_ip_history_unique")

    assert read_history(db_path) == [
        ("dana", "198.51.100.4", "late-node", "grpc", None, None, 5)
    ]
    assert unique_pair_present(db_path, "ip_history", ("username", "ip"))


def test_007_aborts_without_stamping_when_the_staged_merge_is_incomplete(tmp_path):
    """
    A tripped guard must delete nothing, index nothing and stamp nothing.

    "Verify before delete" holds only while the staged-count guard sits between the
    aggregating UPDATE and the DELETE. The count 007 checks the staged rows against is
    forced one too high from outside the revision, and then the three things an abort
    must leave behind are asserted: the recorded revision is still 006, every seeded
    row is byte-identical, and nothing enforces the pair. Moving the DELETE/INSERT
    above the guard, or dropping the guard, fails this test.
    """
    db_path = tmp_path / "aborted.db"
    run_alembic(db_path, "upgrade", "006_drop_patterns")
    seed_ip_history(db_path, DIVERGENT_ROWS)

    completed = run_alembic_with_inflated_group_count(db_path, tmp_path, "upgrade", "head")
    output = f"{completed.stdout}\n{completed.stderr}"

    assert completed.returncode != 0, f"007 should have aborted\n{output}"
    assert "staged 3 of 4 duplicate (username, ip) group(s)" in output, output
    assert recorded_revisions(db_path) == ["006_drop_patterns"], output
    assert read_history_in_insertion_order(db_path) == DIVERGENT_ROWS, output
    assert unique_indexes_over(db_path, "ip_history", ("username", "ip")) == [], output
    # What the abort really leaves behind, as 007's docstring says: the staging table,
    # holding an unaggregated copy of each group's newest row, because
    # CREATE TABLE ... AS SELECT commits on its own while the aggregating UPDATE runs
    # inside the transaction that rolls back. The retry drops it before rebuilding.
    assert read_staging(db_path) == NEWEST_ROWS, output

    run_alembic(db_path, "upgrade", "head")

    assert read_history(db_path) == MERGED_ROWS
    assert read_staging(db_path) is None
    assert unique_indexes_over(db_path, "ip_history", ("username", "ip")) == [
        "uq_ip_history_username_ip"
    ]


def test_007_verifies_the_staged_merge_before_deleting_any_source_row(tmp_path):
    """
    The staged-count guard must sit between the aggregating writes and the DELETE.

    That ordering is the whole safety argument: the aggregate is computed while the
    source rows still exist, checked complete, and only then are those rows replaced.
    The abort test proves a tripped guard costs nothing; this proves the guard is
    actually reached before anything destructive runs, on the ordinary success path,
    which is not observable from the data afterwards.
    """
    db_path = tmp_path / "ordering.db"
    run_alembic(db_path, "upgrade", "006_drop_patterns")
    seed_ip_history(db_path, DIVERGENT_ROWS)

    statements = run_alembic_recording_statements(
        db_path, tmp_path, "upgrade", "007_ip_history_unique"
    )

    aggregated = positions(statements, f"UPDATE {STAGING_TABLE} SET")
    verified = positions(statements, f"SELECT COUNT(*) FROM {STAGING_TABLE}")
    deleted = positions(statements, "DELETE FROM ip_history")
    inserted = positions(statements, "INSERT INTO ip_history")
    assert max(aggregated) < min(verified), statements
    assert max(verified) < min(deleted + inserted), statements
    # And the aggregate is built from rows that are still there to be read.
    assert min(aggregated) < min(deleted), statements


def test_007_is_idempotent_after_restamping_to_006(tmp_path):
    """start.sh restamps to 006 and re-runs; the merge must not run twice."""
    db_path = tmp_path / "restamped.db"
    run_alembic(db_path, "upgrade", "006_drop_patterns")
    seed_ip_history(db_path, DIVERGENT_ROWS)
    run_alembic(db_path, "upgrade", "head")
    merged = read_history(db_path)
    ids_after_first_upgrade = history_ids(db_path)

    run_alembic(db_path, "stamp", "006_drop_patterns")
    run_alembic(db_path, "upgrade", "head")

    assert read_history(db_path) == merged
    assert history_ids(db_path) == ids_after_first_upgrade
    # Exactly one thing enforces the pair - a second run must not add another.
    assert unique_indexes_over(db_path, "ip_history", ("username", "ip")) == [
        "uq_ip_history_username_ip"
    ]
