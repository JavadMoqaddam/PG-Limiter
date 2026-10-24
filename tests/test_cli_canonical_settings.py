"""CLI commands must update committed settings without retired JSON overrides."""

import asyncio
import json
import os
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session
from typer.testing import CliRunner

import db.database as database
import utils.read_config as config
from db.models import Base, Config, User

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def cli_db(tmp_path, monkeypatch):
    """Share a real SQLite file between the CLI and its independent reader."""
    path = tmp_path / "settings.db"
    url = f"sqlite+aiosqlite:///{path}"
    sync_engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(sync_engine)
    with Session(sync_engine) as db:
        db.add(User(username="alice", status="active"))
        db.commit()
    engine = create_async_engine(url)
    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(database, "AsyncSessionLocal", async_sessionmaker(engine, expire_on_commit=False))
    monkeypatch.setattr(database, "DATABASE_URL", url)
    monkeypatch.setattr(database, "_DB_INITIALIZED", True)
    monkeypatch.setattr(database, "_init_db_lock", asyncio.Lock())
    monkeypatch.setattr(config, "get_db", database.get_db)
    monkeypatch.setattr(config, "DB_AVAILABLE", True)
    monkeypatch.setattr(config, "_config_cache", None)
    monkeypatch.setattr(config, "_config_revision", 0)
    monkeypatch.setattr(config, "_config_cache_db_revision", None)
    monkeypatch.setattr(config, "_config_read_lock", asyncio.Lock())
    monkeypatch.setattr(config, "_config_write_lock", asyncio.Lock())
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("GENERAL_LIMIT", "2")
    monkeypatch.setenv("IP_SOURCE", "logs")
    monkeypatch.chdir(tmp_path)
    yield SimpleNamespace(path=path, url=url, cwd=tmp_path, engine=engine, sync_engine=sync_engine)
    asyncio.run(engine.dispose())
    sync_engine.dispose()


async def run_cli(cli_db, *args):
    env = os.environ.copy()
    env.update(DATABASE_URL=cli_db.url, LOG_LEVEL="CRITICAL", COLUMNS="120")
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(PROJECT_ROOT / "cli_main.py"),
        *args,
        cwd=cli_db.cwd,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(process.communicate(), 20)
    return SimpleNamespace(code=process.returncode, out=stdout.decode(), err=stderr.decode())


async def test_cli_general_limit_and_status_use_database_without_json(cli_db):
    assert (await config.read_config())["limits"]["general"] == 2
    written = await run_cli(cli_db, "config", "set-limit", "--limit", "7")
    assert written.code == 0, written.out + written.err
    assert (await config.read_config(refresh=True))["limits"]["general"] == 7
    status = await run_cli(cli_db, "status")
    assert status.code == 0, status.out + status.err
    assert re.search(r"General Limit\s+[│ ]*7\b", status.out)
    assert not (cli_db.cwd / "config.json").exists()
    assert not (cli_db.cwd / "backup.json").exists()


async def test_cli_user_limit_and_exception_flows_commit_to_canonical_state(cli_db):
    async def command(*args):
        result = await run_cli(cli_db, *args)
        assert result.code == 0, result.out + result.err
        return result.out

    await command("user", "add", "--name", "alice", "--limit", "5")
    assert (await config.read_config(refresh=True))["limits"]["special"] == {"alice": 5}
    await command("user", "update", "--name", "alice", "--limit", "8")
    assert (await config.read_config(refresh=True))["limits"]["special"] == {"alice": 8}
    assert "alice" in await command("user", "list")
    assert "special limit: 8" in await command("user", "show", "--name", "alice")
    await command("user", "delete", "--name", "alice")
    assert (await config.read_config(refresh=True))["limits"]["special"] == {}
    missing = await run_cli(cli_db, "user", "update", "--name", "alice", "--limit", "8")
    assert missing.code == 1
    await command("except", "add", "--name", "alice")
    assert (await config.read_config(refresh=True))["except_users"] == ["alice"]
    assert " IS in the except list" in await command("except", "check", "--name", "alice")
    duplicate = await run_cli(cli_db, "except", "add", "--name", "alice")
    assert duplicate.code == 1
    await command("except", "delete", "--name", "alice")
    assert (await config.read_config(refresh=True))["except_users"] == []
    assert " is NOT in the except list" in await command("except", "check", "--name", "alice")
    missing = await run_cli(cli_db, "except", "delete", "--name", "alice")
    assert missing.code == 1


async def test_cli_ignores_conflicting_retired_config_and_backup(cli_db):
    retired = {
        "config.json": {"limits": {"general": 99, "special": {"ghost": 99}}, "users": {"except": ["ghost"]}},
        "backup.json": {"special": {"ghost": 88}, "except_users": ["ghost"]},
    }
    for name, value in retired.items():
        (cli_db.cwd / name).write_text(json.dumps(value))
    result = await run_cli(cli_db, "config", "set-limit", "--limit", "7")
    assert result.code == 0, result.out + result.err
    assert (await config.read_config(refresh=True))["limits"]["general"] == 7
    result = await run_cli(cli_db, "user", "list")
    assert "No users with special limits found" in result.out
    result = await run_cli(cli_db, "except", "check", "--name", "ghost")
    assert " is NOT in the except list" in result.out
    result = await run_cli(cli_db, "status")
    assert re.search(r"General Limit\s+[│ ]*7\b", result.out)
    for name, value in retired.items():
        assert json.loads((cli_db.cwd / name).read_text()) == value


def test_repeated_cli_invocations_close_database_on_their_own_loop(cli_db, monkeypatch):
    import cli_main

    calls = []
    original_init, original_close = database.init_db, database.close_db

    async def init():
        calls.append(("init", asyncio.get_running_loop()))
        await original_init()

    async def close():
        calls.append(("close", asyncio.get_running_loop()))
        await original_close()

    monkeypatch.setattr(database, "init_db", init)
    monkeypatch.setattr(database, "close_db", close)
    runner = CliRunner()
    commands = [
        ["config", "set-limit", "--limit", "7"],
        ["config", "set-interval", "--interval", "180"],
        ["config", "set-reenable-time", "--time", "900"],
        ["config", "set-country", "--code", "none"],
        ["config", "show"],
        ["status"],
    ]
    for args in commands:
        result = runner.invoke(cli_main.app, args)
        assert result.exit_code == 0, result.output
        assert cli_db.engine.pool.checkedout() == 0
    assert len(calls) == 2 * len(commands)
    for index in range(0, len(calls), 2):
        assert calls[index][0] == "init" and calls[index + 1][0] == "close"
        assert calls[index][1] is calls[index + 1][1]
        assert calls[index][1].is_closed()
    assert len({loop for _, loop in calls}) == len(commands)
    assert re.search(r"General Limit\s+[│ ]*7\b", result.output)
    assert "180s" in result.output
    with Session(cli_db.sync_engine) as db:
        settings = dict(db.execute(select(Config.key, Config.value)).all())
    assert int(settings["general_limit"]) == 7
    assert int(settings["check_interval"]) == 180
    assert int(settings["time_to_active_users"]) == 900
    assert settings["country_code"] == ""
