#!/usr/bin/env python3
"""
Schema checks CI runs against a real SQLite file.

Why this exists: ``Base.metadata.create_all`` and the Alembic chain are two
independent descriptions of the same schema and nothing kept them honest.
create_all is a no-op for a table that already exists, so anything added only to
db/models.py is simply absent from every database built by an older release, and
absent silently. ``ip_history`` lost its ``UNIQUE(username, ip)`` exactly that
way: 001_initial created the pair as a *non-unique* index, the model declares a
UniqueConstraint, and no revision ever reconciled them. The visible effect was
every ``ON CONFLICT (username, ip)`` upsert raising, being caught one frame up,
and logging a warning - so the IP-history reports stayed empty forever with no
other symptom.

The old CI could not have caught it. It created ./data/test_ci.db with
create_all and then ran ``alembic upgrade head``, which reads alembic.ini and
went to ./data/pg_limiter.db - a different, empty file. No scenario ever ran a
migration against a populated database.

Subcommands, each taking a path to a SQLite file:
  create-all <db>       build the schema the way a pre-Alembic release did
  seed-ip-history <db>  insert duplicate (username, ip) rows
  parity <db>           compare the file against db/models.py
  assert-dedup <db>     check 007 kept the newest row of each duplicate pair
  assert-upsert <db>    run the ON CONFLICT statement bulk_record relies on
"""

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import PrimaryKeyConstraint, UniqueConstraint, create_engine, inspect  # noqa: E402

from db.models import Base  # noqa: E402

# (username, ip, connection_count) - the two duplicate groups collapse to one row
# each, and the survivor must be the highest id, i.e. the last one inserted here.
SEED_ROWS = [
    ("ci_dup_user", "198.51.100.1", 1),
    ("ci_dup_user", "198.51.100.1", 2),
    ("ci_dup_user", "198.51.100.1", 3),
    ("ci_other_user", "198.51.100.2", 7),
    ("ci_other_user", "198.51.100.2", 8),
]


def _fail(message: str) -> None:
    print(f"::error::{message}")


def cmd_create_all(path: str) -> int:
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    engine.dispose()
    print(f"create_all built {path} from db/models.py")
    return 0


def cmd_seed_ip_history(path: str) -> int:
    conn = sqlite3.connect(path)
    try:
        conn.executemany(
            "INSERT INTO ip_history (username, ip, connection_count) VALUES (?, ?, ?)",
            SEED_ROWS,
        )
        conn.commit()
    finally:
        conn.close()
    print(f"seeded {len(SEED_ROWS)} ip_history rows ({len(SEED_ROWS) - 2} of them duplicates)")
    return 0


def _db_unique_groups(inspector, table: str) -> set[frozenset[str]]:
    """
    Every column group the database actually enforces as unique.

    Three shapes have to be collected. A named unique index shows up in
    ``get_indexes``. A ``UniqueConstraint`` written inline by create_all is backed
    by a ``sqlite_autoindex_*``, which SQLAlchemy filters out of ``get_indexes``
    and reports only under ``get_unique_constraints``. The primary key is unique
    too and is reported by neither. Column order is ignored: SQLite matches an
    ON CONFLICT target against a unique index as a set.
    """
    groups: set[frozenset[str]] = set()

    for index in inspector.get_indexes(table):
        columns = index.get("column_names") or []
        if index.get("unique") and all(columns):
            groups.add(frozenset(columns))

    try:
        constraints = inspector.get_unique_constraints(table)
    except NotImplementedError:
        constraints = []
    for constraint in constraints:
        columns = constraint.get("column_names") or []
        if all(columns):
            groups.add(frozenset(columns))

    primary_key = inspector.get_pk_constraint(table).get("constrained_columns") or []
    if primary_key:
        groups.add(frozenset(primary_key))

    return groups


def _model_unique_groups(table) -> set[frozenset[str]]:
    groups: set[frozenset[str]] = set()
    for constraint in table.constraints:
        if isinstance(constraint, (UniqueConstraint, PrimaryKeyConstraint)):
            columns = frozenset(column.name for column in constraint.columns)
            if columns:
                groups.add(columns)
    for index in table.indexes:
        if index.unique:
            columns = frozenset(column.name for column in index.columns)
            if columns:
                groups.add(columns)
    return groups


def cmd_parity(path: str) -> int:
    """
    Fail on anything db/models.py declares that the database does not have.

    Missing tables, columns and unique groups are errors: they break queries and
    upserts at runtime. Missing plain indexes are reported but do not fail the
    job - they cost query time, not correctness, and a hard failure there would
    block a release over a table scan.
    """
    engine = create_engine(f"sqlite:///{path}")
    inspector = inspect(engine)
    present_tables = set(inspector.get_table_names())

    errors: list[str] = []
    warnings: list[str] = []

    for name, table in sorted(Base.metadata.tables.items()):
        if name not in present_tables:
            errors.append(f"table '{name}' is declared in db/models.py but missing from {path}")
            continue

        db_columns = {column["name"] for column in inspector.get_columns(name)}
        for column in table.columns:
            if column.name not in db_columns:
                errors.append(f"{name}.{column.name} is declared in db/models.py but missing")

        db_groups = _db_unique_groups(inspector, name)
        for group in sorted(_model_unique_groups(table), key=sorted):
            if group not in db_groups:
                errors.append(
                    f"{name}: UNIQUE({', '.join(sorted(group))}) is declared in db/models.py but "
                    f"nothing enforces it - any ON CONFLICT naming those columns will raise"
                )

        db_indexes = {tuple(ix.get("column_names") or ()) for ix in inspector.get_indexes(name)}
        db_indexes |= {tuple(sorted(group)) for group in db_groups}
        for index in table.indexes:
            columns = tuple(column.name for column in index.columns)
            if columns not in db_indexes and tuple(sorted(columns)) not in db_indexes:
                warnings.append(f"{name}: index on ({', '.join(columns)}) is declared but missing")

    engine.dispose()

    for warning in warnings:
        print(f"::warning::{warning}")
    for error in errors:
        _fail(error)

    if errors:
        print(f"\nschema parity FAILED: {len(errors)} problem(s) in {path}")
        return 1
    print(
        f"schema parity OK: {len(Base.metadata.tables)} tables in {path} match db/models.py"
        + (f" ({len(warnings)} index warning(s))" if warnings else "")
    )
    return 0


def cmd_assert_dedup(path: str) -> int:
    """Check 007 collapsed each duplicate pair onto its newest row."""
    conn = sqlite3.connect(path)
    try:
        rows = conn.execute(
            "SELECT username, ip, connection_count FROM ip_history ORDER BY username"
        ).fetchall()
    finally:
        conn.close()

    expected = [("ci_dup_user", "198.51.100.1", 3), ("ci_other_user", "198.51.100.2", 8)]
    if rows != expected:
        _fail(f"007 de-duplication left {rows}, expected {expected}")
        return 1
    print(f"de-duplication OK: {len(SEED_ROWS)} seeded rows collapsed to {rows}")
    return 0


def cmd_assert_upsert(path: str) -> int:
    """
    Run the statement IPHistoryCRUD.bulk_record emits, against a real file.

    ``on_conflict_do_update(index_elements=["username", "ip"])`` renders exactly
    this. Without a unique index over the pair SQLite answers "ON CONFLICT clause
    does not match any PRIMARY KEY or UNIQUE constraint" - the production failure
    this whole job exists to catch.
    """
    statement = (
        "INSERT INTO ip_history (username, ip, connection_count) VALUES (?, ?, 1) "
        "ON CONFLICT (username, ip) DO UPDATE SET "
        "connection_count = ip_history.connection_count + 1"
    )
    conn = sqlite3.connect(path)
    try:
        for _ in range(3):
            conn.execute(statement, ("ci_upsert_user", "203.0.113.7"))
        conn.commit()
        rows = conn.execute(
            "SELECT connection_count FROM ip_history WHERE username = ?", ("ci_upsert_user",)
        ).fetchall()
    except sqlite3.OperationalError as error:
        _fail(f"the bulk_record upsert cannot run against {path}: {error}")
        return 1
    finally:
        conn.close()

    if rows != [(3,)]:
        _fail(f"upsert produced {rows}, expected a single row with connection_count 3")
        return 1
    print("upsert OK: ON CONFLICT (username, ip) matched a unique index and merged 3 writes")
    return 0


COMMANDS = {
    "create-all": cmd_create_all,
    "seed-ip-history": cmd_seed_ip_history,
    "parity": cmd_parity,
    "assert-dedup": cmd_assert_dedup,
    "assert-upsert": cmd_assert_upsert,
}


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[0] not in COMMANDS:
        print(f"usage: {Path(__file__).name} {{{'|'.join(COMMANDS)}}} <sqlite-path>")
        return 2
    return COMMANDS[argv[0]](argv[1])


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
