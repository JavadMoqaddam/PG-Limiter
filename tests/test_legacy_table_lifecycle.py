"""Startup compatibility imports must never replay over later local commands."""

import asyncio
import os
import sys
from types import SimpleNamespace

import pytest
from sqlalchemy import event, insert, select, text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import db.database as database
from db.crud.config import ConfigCRUD
from db.crud.config_revision import ConfigRevisionCRUD
from db.crud.users import UserCRUD
from db.models import Base, Config, User


MARKER_KEY = "_deprecated_legacy_tables_import"


@pytest.fixture
async def legacy_db(tmp_path, monkeypatch):
    url = f"sqlite+aiosqlite:///{tmp_path / 'legacy.db'}"
    engine = create_async_engine(url, connect_args={"timeout": 5})

    @event.listens_for(engine.sync_engine, "connect")
    def wal(connection, _record):
        cursor = connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(database, "DATABASE_URL", url)
    monkeypatch.setattr(database, "AsyncSessionLocal", sessions)
    monkeypatch.setattr(database, "_DB_INITIALIZED", False)
    monkeypatch.setattr(database, "_init_db_lock", asyncio.Lock())
    try:
        yield SimpleNamespace(engine=engine, sessions=sessions)
    finally:
        await engine.dispose()


async def restart():
    """Exercise startup again, as a new process with no initialization flag would."""
    database._DB_INITIALIZED = False
    await database.init_db()


async def seed(legacy_db):
    async with legacy_db.engine.begin() as connection:
        await connection.execute(text("INSERT INTO user_limits (username, \"limit\") VALUES ('alice', 7)"))
        await connection.execute(
            text(
                "INSERT INTO except_users (username, reason, created_by) VALUES ('alice', 'legacy exception', 'admin')"
            )
        )
        await connection.execute(
            text("""INSERT INTO disabled_users
                (username, disabled_at, enable_at, original_groups, reason, punishment_step)
                VALUES ('alice', 100, -1, '[3, 5]', 'legacy disable', 2)""")
        )


async def persisted(legacy_db):
    async with legacy_db.sessions() as session:
        marker = await session.scalar(select(Config.value).where(Config.key == MARKER_KEY))
        revision = await ConfigRevisionCRUD.read(session)
        users = {user.username: user for user in (await session.scalars(select(User))).all()}
    return marker, revision, users


async def sources(legacy_db):
    async with legacy_db.engine.connect() as connection:
        return {
            name: (await connection.execute(text(f"SELECT * FROM {name}"))).all()
            for name in ("user_limits", "except_users", "disabled_users")
        }


async def test_startup_import_completes_once_and_preserves_sources(legacy_db):
    await seed(legacy_db)
    before = await sources(legacy_db)
    await restart()
    marker, revision, users = await persisted(legacy_db)
    assert marker == {"version": 1, "completed": True}
    assert revision == 1
    alice = users["alice"]
    assert alice.special_limit == 7
    assert alice.is_excepted is True
    assert (alice.exception_reason, alice.excepted_by) == ("legacy exception", "admin")
    assert alice.is_disabled_by_limiter is True
    assert (alice.disabled_at, alice.enable_at, alice.original_groups) == (100, -1, [3, 5])
    assert (alice.disable_reason, alice.punishment_step) == ("legacy disable", 2)
    await restart()
    assert (await persisted(legacy_db))[:2] == (marker, revision)
    assert await sources(legacy_db) == before


@pytest.mark.parametrize("command", ["enable", "remove_limit", "remove_exception", "delete_user"])
async def test_restart_preserves_intentional_commands_after_import(legacy_db, command):
    await seed(legacy_db)
    await restart()
    async with database.get_db() as session:
        if command == "enable":
            await UserCRUD.set_disabled(session, "alice", False, fetch_from_panel=False)
        elif command == "remove_limit":
            await UserCRUD.set_special_limit(session, "alice", None, fetch_from_panel=False)
        elif command == "remove_exception":
            await UserCRUD.set_excepted(session, "alice", False, fetch_from_panel=False)
        else:
            assert await UserCRUD.delete(session, "alice")
    revision = (await persisted(legacy_db))[1]
    await restart()
    _, after_revision, users = await persisted(legacy_db)
    assert after_revision == revision
    if command == "delete_user":
        assert users == {}
    elif command == "enable":
        assert users["alice"].is_disabled_by_limiter is False
        assert users["alice"].disabled_at is None
        assert users["alice"].enable_at is None
    elif command == "remove_limit":
        assert users["alice"].special_limit is None
    else:
        assert users["alice"].is_excepted is False


async def test_current_nonempty_values_keep_merge_precedence(legacy_db):
    await seed(legacy_db)
    async with legacy_db.sessions.begin() as session:
        session.add(
            User(
                username="alice",
                special_limit=11,
                is_excepted=True,
                exception_reason="current",
                is_disabled_by_limiter=True,
                disabled_at=200,
                enable_at=300,
                original_groups=[8],
            )
        )
    await restart()
    marker, revision, users = await persisted(legacy_db)
    assert marker == {"version": 1, "completed": True}
    assert revision == 0
    assert users["alice"].special_limit == 11
    assert users["alice"].exception_reason == "current"
    assert (users["alice"].disabled_at, users["alice"].enable_at, users["alice"].original_groups) == (200, 300, [8])


async def test_malformed_schema_rolls_back_entire_import_and_retries_after_repair(legacy_db):
    await seed(legacy_db)
    async with legacy_db.engine.begin() as connection:
        await connection.execute(text("ALTER TABLE except_users RENAME COLUMN created_by TO obsolete_created_by"))
    with pytest.raises(OperationalError):
        await restart()
    assert database._DB_INITIALIZED is False
    assert await persisted(legacy_db) == (None, 0, {})
    async with legacy_db.engine.begin() as connection:
        await connection.execute(text("ALTER TABLE except_users RENAME COLUMN obsolete_created_by TO created_by"))
    await restart()
    assert (await persisted(legacy_db))[0:2] == ({"version": 1, "completed": True}, 1)


async def test_later_table_failure_rolls_back_all_records_marker_and_revision(legacy_db):
    await seed(legacy_db)
    before = await sources(legacy_db)
    async with legacy_db.engine.begin() as connection:
        await connection.execute(
            text("""CREATE TRIGGER reject_legacy_disable BEFORE UPDATE ON users
            WHEN NEW.is_disabled_by_limiter = 1
            BEGIN SELECT RAISE(ABORT, 'disabled import rejected'); END""")
        )
    with pytest.raises(IntegrityError, match="disabled import rejected"):
        await restart()
    assert await persisted(legacy_db) == (None, 0, {})
    assert await sources(legacy_db) == before
    async with legacy_db.engine.begin() as connection:
        await connection.execute(text("DROP TRIGGER reject_legacy_disable"))
    await restart()
    assert (await persisted(legacy_db))[0:2] == ({"version": 1, "completed": True}, 1)


@pytest.mark.parametrize("malformed", ["username", "limit", "groups"])
async def test_malformed_source_records_are_retryable_and_never_completed(legacy_db, malformed):
    await seed(legacy_db)
    statements = {
        "username": "UPDATE except_users SET username = ''",
        "limit": "UPDATE user_limits SET \"limit\" = 'invalid'",
        "groups": "UPDATE disabled_users SET original_groups = 'invalid-json'",
    }
    async with legacy_db.engine.begin() as connection:
        await connection.execute(text(statements[malformed]))
    before = await sources(legacy_db)
    with pytest.raises(ValueError, match="Invalid legacy"):
        await restart()
    assert await persisted(legacy_db) == (None, 0, {})
    assert await sources(legacy_db) == before


async def test_cancelled_startup_before_commit_keeps_import_retryable(legacy_db):
    await seed(legacy_db)

    def interrupt(_connection):
        raise asyncio.CancelledError("interrupted startup")

    event.listen(legacy_db.engine.sync_engine, "commit", interrupt)
    try:
        with pytest.raises(asyncio.CancelledError):
            await restart()
    finally:
        event.remove(legacy_db.engine.sync_engine, "commit", interrupt)
    assert database._DB_INITIALIZED is False
    assert await persisted(legacy_db) == (None, 0, {})
    await restart()
    assert (await persisted(legacy_db))[0:2] == ({"version": 1, "completed": True}, 1)


@pytest.mark.parametrize("winner_commits", [True, False])
async def test_independent_startup_connections_serialize_and_retry_rolled_back_claims(legacy_db, winner_commits):
    await seed(legacy_db)
    first_imported = asyncio.Event()
    release_first = asyncio.Event()
    second_started = asyncio.Event()

    async def first():
        try:
            async with legacy_db.engine.begin() as connection:
                await connection.run_sync(database._sync_deprecated_legacy_tables_sync)
                first_imported.set()
                await release_first.wait()
                if not winner_commits:
                    raise RuntimeError("rollback first startup")
        except RuntimeError:
            if winner_commits:
                raise

    async def second():
        async with legacy_db.engine.begin() as connection:
            second_started.set()
            await connection.run_sync(database._sync_deprecated_legacy_tables_sync)

    first_task = asyncio.create_task(first())
    await asyncio.wait_for(first_imported.wait(), 5)
    second_task = asyncio.create_task(second())
    try:
        await asyncio.wait_for(second_started.wait(), 5)
        assert await persisted(legacy_db) == (None, 0, {})
        assert not second_task.done()
    finally:
        release_first.set()
        await asyncio.wait_for(asyncio.gather(first_task, second_task), 5)
    assert (await persisted(legacy_db))[0:2] == ({"version": 1, "completed": True}, 1)


async def test_fresh_startup_processes_serialize_schema_creation(tmp_path):
    """A second process must wait before inspecting a schema still being created."""
    child = """
import asyncio
import sys
import time
from pathlib import Path
from sqlalchemy import event
import db.database as database

role, ready_name, release_name = sys.argv[1:]
ready, release = Path(ready_name), Path(release_name)

@event.listens_for(database.engine.sync_engine, "before_cursor_execute")
def coordinate(connection, cursor, statement, parameters, context, executemany):
    if role == "second":
        ready.touch()
    if role == "first" and "CREATE TABLE users" in statement:
        ready.touch()
        deadline = time.monotonic() + 10
        while not release.exists():
            if time.monotonic() >= deadline:
                raise TimeoutError("startup test release missing")
            time.sleep(0.01)

async def main():
    await database.init_db()
    await database.close_db()

asyncio.run(main())
"""
    environment = dict(os.environ, DATABASE_URL=f"sqlite+aiosqlite:///{tmp_path / 'fresh.db'}")
    release = tmp_path / "release"
    first_ready, second_ready = tmp_path / "first-ready", tmp_path / "second-ready"

    async def wait_ready(path):
        async def wait():
            while not path.exists():
                await asyncio.sleep(0.01)

        await asyncio.wait_for(wait(), 5)

    async def launch(role, ready):
        return await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            child,
            role,
            str(ready),
            str(release),
            env=environment,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

    first = await launch("first", first_ready)
    second = None
    second_waited = False
    try:
        await wait_ready(first_ready)
        second = await launch("second", second_ready)
        await wait_ready(second_ready)
        try:
            await asyncio.wait_for(second.wait(), 0.2)
        except TimeoutError:
            second_waited = True
    finally:
        release.touch()
        processes = [first] if second is None else [first, second]
        results = await asyncio.wait_for(asyncio.gather(*(process.communicate() for process in processes)), 10)
    assert second_waited, "second startup inspected and created tables before the first finished"
    for process, (stdout, stderr) in zip(processes, results):
        assert process.returncode == 0, (stdout.decode(), stderr.decode())


@pytest.mark.parametrize("absent", [False, True])
async def test_empty_or_absent_sources_complete_and_ignore_later_legacy_rows(legacy_db, absent):
    if absent:
        async with legacy_db.engine.begin() as connection:
            await connection.execute(text("DROP TABLE user_limits"))
            await connection.execute(text("DROP TABLE except_users"))
            await connection.execute(text("DROP TABLE disabled_users"))
            await connection.run_sync(database._sync_deprecated_legacy_tables_sync)
    else:
        await restart()
    assert await persisted(legacy_db) == ({"version": 1, "completed": True}, 0, {})
    async with legacy_db.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    await seed(legacy_db)
    before = await sources(legacy_db)
    await restart()
    assert await persisted(legacy_db) == ({"version": 1, "completed": True}, 0, {})
    assert await sources(legacy_db) == before


@pytest.mark.parametrize("existing", [False, True])
async def test_disabled_only_import_versions_new_user_facts_only(legacy_db, existing):
    async with legacy_db.sessions.begin() as session:
        if existing:
            session.add(User(username="alice"))
    async with legacy_db.engine.begin() as connection:
        await connection.execute(text("INSERT INTO disabled_users (username, disabled_at) VALUES ('alice', 100)"))
    await restart()
    marker, revision, users = await persisted(legacy_db)
    assert marker == {"version": 1, "completed": True}
    assert revision == (0 if existing else 1)
    assert users["alice"].is_disabled_by_limiter is True


async def test_completion_marker_is_hidden_and_protected_from_management(legacy_db):
    await restart()
    async with database.get_db() as session:
        assert MARKER_KEY not in await ConfigCRUD.get_all(session)
        with pytest.raises(ValueError, match="internal"):
            await ConfigCRUD.set(session, MARKER_KEY, {"version": 1, "completed": False})
        with pytest.raises(ValueError, match="internal"):
            await ConfigCRUD.delete(session, MARKER_KEY)
    assert await persisted(legacy_db) == ({"version": 1, "completed": True}, 0, {})


@pytest.mark.parametrize("value", [None, {}, {"version": 2, "completed": True}, {"version": 1, "completed": "yes"}])
async def test_invalid_completion_marker_fails_closed_without_importing(legacy_db, value):
    await seed(legacy_db)
    async with legacy_db.engine.begin() as connection:
        await connection.execute(insert(Config).values(key=MARKER_KEY, value=value))
    with pytest.raises(ValueError, match="Invalid legacy"):
        await restart()
    assert database._DB_INITIALIZED is False
    assert await persisted(legacy_db) == (value, 0, {})
