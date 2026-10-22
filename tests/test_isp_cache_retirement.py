"""The ISP cache disappears without changing user data or violation history."""

import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

from sqlalchemy import create_engine

from db.models import Base

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def migrate(path: Path, *arguments: str):
    env = os.environ.copy()
    env["DATABASE_URL"] = f"sqlite+aiosqlite:///{path}"
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *arguments],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def tables(path: Path) -> set[str]:
    with closing(sqlite3.connect(path)) as connection:
        return {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def test_upgrade_drops_only_disposable_isp_cache(tmp_path):
    path = tmp_path / "existing.db"
    migrate(path, "upgrade", "008_missing_indexes")
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("INSERT INTO subnet_isp (subnet, isp) VALUES ('49.12.186.0/24', 'Example ISP')")
        connection.execute("INSERT INTO users (username, special_limit) VALUES ('alice', 3)")
        connection.execute(
            "INSERT INTO violation_history (username, timestamp, step_applied, disable_duration) VALUES ('alice', 1, 2, 30)"
        )
        connection.commit()
    migrate(path, "upgrade", "head")
    assert "subnet_isp" not in tables(path)
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("SELECT username, special_limit FROM users").fetchall() == [("alice", 3)]
        assert connection.execute("SELECT username, step_applied FROM violation_history").fetchall() == [("alice", 2)]

    # Startup may repair the recorded revision and replay the tail of the chain.
    migrate(path, "stamp", "006_drop_patterns")
    migrate(path, "upgrade", "head")
    assert "subnet_isp" not in tables(path)


def test_fresh_and_create_all_databases_do_not_create_isp_cache(tmp_path):
    fresh = tmp_path / "fresh.db"
    migrate(fresh, "upgrade", "head")
    assert "subnet_isp" not in tables(fresh)

    legacy = tmp_path / "create-all.db"
    engine = create_engine(f"sqlite:///{legacy}")
    try:
        Base.metadata.create_all(engine)
    finally:
        engine.dispose()
    assert "subnet_isp" not in tables(legacy)
    migrate(legacy, "stamp", "006_drop_patterns")
    migrate(legacy, "upgrade", "head")
    assert "subnet_isp" not in tables(legacy)


def test_downgrade_recreates_cache_schema_and_reupgrade_removes_it(tmp_path):
    path = tmp_path / "downgrade.db"
    migrate(path, "upgrade", "head")
    migrate(path, "downgrade", "008_missing_indexes")
    assert "subnet_isp" in tables(path)
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("INSERT INTO subnet_isp (subnet, isp) VALUES ('49.12.45.0/24', 'Example ISP')")
        indexes = {row[1] for row in connection.execute("PRAGMA index_list('subnet_isp')")}
        assert {
            "ix_subnet_isp_subnet",
            "ix_subnet_isp_country",
            "ix_subnet_isp_isp",
            "ix_subnet_isp_cached_at",
        } <= indexes
    migrate(path, "upgrade", "head")
    assert "subnet_isp" not in tables(path)
