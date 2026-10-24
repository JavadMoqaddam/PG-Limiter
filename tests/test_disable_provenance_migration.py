"""Keep incident provenance intact across real upgrades and startup replay."""

import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROVENANCE = {"version": 1, "method": "group", "original_groups": [], "applied_groups": [99]}


def migrate(database: Path, *args: str) -> str:
    environment = os.environ.copy()
    environment["DATABASE_URL"] = f"sqlite+aiosqlite:///{database}"
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout + result.stderr


def test_upgrade_keeps_old_disable_state_without_inventing_provenance(tmp_path):
    database = tmp_path / "upgrade.db"
    migrate(database, "upgrade", "009_drop_isp_cache")
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO users (username, is_disabled_by_limiter, disabled_at, enable_at, original_groups, disable_reason) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("alice", 1, 100.0, -1.0, "[1, 2]", "Human-readable reason"),
        )
    migrate(database, "upgrade", "head")
    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT is_disabled_by_limiter, disabled_at, enable_at, original_groups, disable_reason, "
            "disable_provenance FROM users WHERE username = 'alice'"
        ).fetchone()
    assert row == (1, 100.0, -1.0, "[1, 2]", "Human-readable reason", None)


def test_provenance_has_one_head_and_keeps_retired_isp_cache_absent(tmp_path):
    database = tmp_path / "head.db"
    heads = migrate(database, "heads")
    assert heads.count("(head)") == 1
    assert "010_disable_provenance (head)" in heads
    migrate(database, "upgrade", "head")
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT name FROM sqlite_master WHERE name = 'subnet_isp'").fetchone() is None


def test_replay_preserves_provenance_and_downgrade_keeps_other_disable_fields(tmp_path):
    database = tmp_path / "replay.db"
    migrate(database, "upgrade", "head")
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO users (username, is_disabled_by_limiter, disabled_at, enable_at, disable_provenance) "
            "VALUES (?, ?, ?, ?, ?)",
            ("alice", 1, 100.0, -1.0, json.dumps(PROVENANCE)),
        )
    migrate(database, "stamp", "006_drop_patterns")
    migrate(database, "upgrade", "head")
    with sqlite3.connect(database) as connection:
        stored = connection.execute("SELECT disable_provenance FROM users WHERE username = 'alice'").fetchone()[0]
    assert json.loads(stored) == PROVENANCE
    migrate(database, "downgrade", "-1")
    with sqlite3.connect(database) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(users)")}
        assert "disable_provenance" not in columns
        assert connection.execute("SELECT name FROM sqlite_master WHERE name = 'subnet_isp'").fetchone() is None
        assert connection.execute(
            "SELECT is_disabled_by_limiter, disabled_at, enable_at FROM users WHERE username = 'alice'"
        ).fetchone() == (1, 100.0, -1.0)
    migrate(database, "upgrade", "head")
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT disable_provenance FROM users WHERE username = 'alice'").fetchone() == (None,)
