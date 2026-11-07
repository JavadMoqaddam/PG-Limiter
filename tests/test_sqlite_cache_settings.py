"""Cache settings reach every physical SQLite connection, including overflow."""

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize(
    "setting,expected",
    [
        (None, -16384),
        ("32768", -32768),
        ("64000", -64000),
        ("0", -16384),
        ("-8192", -16384),
        ("invalid", -16384),
        ("2147483648", -16384),
    ],
)
def test_cache_budget_applies_to_pooled_overflow_and_reopened_connections(tmp_path, setting, expected):
    environment = os.environ.copy()
    environment["DATABASE_URL"] = f"sqlite+aiosqlite:///{tmp_path / 'cache.db'}"
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    environment.pop("SQLITE_CACHE_SIZE_KIB", None)
    if setting is not None:
        environment["SQLITE_CACHE_SIZE_KIB"] = setting
    script = """
import asyncio, json
from sqlalchemy import text
from db.database import engine

async def run():
    readings = []
    connections = []
    try:
        for _ in range(6):
            connection = await engine.connect()
            connections.append(connection)
            readings.append([await connection.scalar(text('PRAGMA ' + name))
                             for name in ('cache_size','journal_mode','synchronous','busy_timeout')])
    finally:
        for connection in connections:
            await connection.close()
    await engine.dispose()
    async with engine.connect() as connection:
        readings.append([await connection.scalar(text('PRAGMA ' + name))
                         for name in ('cache_size','journal_mode','synchronous','busy_timeout')])
    await engine.dispose()
    print(json.dumps(readings))

asyncio.run(run())
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    assert json.loads(result.stdout.splitlines()[-1]) == [[expected, "wal", 1, 30000]] * 7
