"""Reporting tables must exist in upgraded and create_all installations."""

import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize("create_all", [False, True])
def test_daily_reporting_schema_matches_migrations(tmp_path, create_all):
    root = Path(__file__).resolve().parents[1]
    db = tmp_path / "migration.db"
    env = {**os.environ, "DATABASE_URL": f"sqlite+aiosqlite:///{db}"}

    def run(*args):
        result = subprocess.run([sys.executable, *args], cwd=root, env=env, capture_output=True, text=True)
        assert result.returncode == 0, result.stdout + result.stderr

    if create_all:
        run("tools/ci_migration_checks.py", "create-all", str(db))
        run("-m", "alembic", "stamp", "008_missing_indexes")
    run("-m", "alembic", "upgrade", "head")
    run("tools/ci_migration_checks.py", "parity", str(db))
