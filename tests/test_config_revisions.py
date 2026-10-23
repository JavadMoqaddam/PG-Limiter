"""Committed settings must refresh locally and across independent processes."""

import asyncio
import os
import sys
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, event, insert, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import db.database as database
import utils.read_config as config
from db.crud.config import ConfigCRUD
from db.crud.users import UserCRUD
from db.models import Base, Config, User


@pytest.fixture
async def settings_db(tmp_path, monkeypatch):
    url = f"sqlite+aiosqlite:///{tmp_path / 'settings.db'}"
    engine = create_async_engine(url)

    @event.listens_for(engine.sync_engine, "connect")
    def wal(connection, _record):
        cursor = connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(database, "AsyncSessionLocal", sessions)
    monkeypatch.setattr(config, "get_db", database.get_db)
    monkeypatch.setattr(config, "DB_AVAILABLE", True)
    monkeypatch.setattr(config, "_config_cache", None)
    monkeypatch.setattr(config, "_config_revision", 0)
    monkeypatch.setattr(config, "_config_cache_db_revision", None, raising=False)
    monkeypatch.setattr(config, "_config_read_lock", asyncio.Lock())
    monkeypatch.setattr(config, "_config_write_lock", asyncio.Lock())
    try:
        yield SimpleNamespace(sessions=sessions, engine=engine, url=url, path=tmp_path)
    finally:
        await engine.dispose()


async def test_direct_committed_crud_write_invalidates_merged_config(settings_db):
    async with database.get_db() as db:
        await ConfigCRUD.set(db, "general_limit", "2")
    assert (await config.read_config())["limits"]["general"] == 2
    async with database.get_db() as db:
        await ConfigCRUD.set(db, "general_limit", "7")
    assert (await config.read_config())["limits"]["general"] == 7


async def test_local_exception_commit_invalidates_merged_config(settings_db):
    async with database.get_db() as db:
        await UserCRUD.create_or_update(db, "alice")
    assert "alice" not in (await config.read_config())["except_users"]
    async with database.get_db() as db:
        await UserCRUD.set_excepted(db, "alice", True, fetch_from_panel=False)
    assert "alice" in (await config.read_config())["except_users"]


async def revision():
    from db.crud.config_revision import ConfigRevisionCRUD

    async with database.get_db() as db:
        return await ConfigRevisionCRUD.read(db)


@pytest.mark.parametrize("outcome,expected", [("commit", 2), ("rollback", 0), ("savepoint_rollback", 1)])
async def test_revision_tracks_only_committed_transaction_changes(settings_db, outcome, expected):
    async with settings_db.sessions() as db:
        await ConfigCRUD.set(db, "general_limit", "7")
        nested = await db.begin_nested()
        await ConfigCRUD.set(db, "check_interval", "180")
        if outcome == "savepoint_rollback":
            await nested.rollback()
        else:
            await nested.commit()
        assert await revision() == 0
        if outcome == "rollback":
            await db.rollback()
        else:
            await db.commit()
    assert await revision() == expected


@pytest.mark.parametrize("entry", ["fresh", "read", "begin", "immediate"])
@pytest.mark.parametrize("outcome", ["commit", "rollback", "nested_rollback"])
async def test_first_nested_write_waits_for_physical_root_resolution(settings_db, entry, outcome):
    await config.read_config()
    cached = config._config_cache
    generation = config.get_config_revision()
    async with settings_db.sessions() as writer:
        if entry == "read":
            assert await ConfigCRUD.get(writer, "general_limit") is None
        elif entry == "begin":
            await writer.execute(text("BEGIN"))
        elif entry == "immediate":
            await writer.execute(text("BEGIN IMMEDIATE"))
        nested = await writer.begin_nested()
        await ConfigCRUD.set(writer, "general_limit", "7")
        if outcome == "nested_rollback":
            await nested.rollback()
        else:
            await nested.commit()

        # RELEASE may not expose either the setting or counter before root commit.
        async with settings_db.sessions() as reader:
            assert await ConfigCRUD.get(reader, "general_limit") is None
        assert await revision() == 0
        assert config._config_cache is cached
        assert config.get_config_revision() == generation
        assert (await config.read_config())["limits"]["general"] == 2

        if outcome == "rollback":
            await writer.rollback()
        else:
            await writer.commit()

    committed = outcome == "commit"
    async with settings_db.sessions() as reader:
        assert await ConfigCRUD.get(reader, "general_limit") == ("7" if committed else None)
    assert await revision() == int(committed)
    assert config.get_config_revision() == generation + int(committed)
    if committed:
        assert config._config_cache is None
    else:
        assert config._config_cache is cached
    assert (await config.read_config())["limits"]["general"] == (7 if committed else 2)


async def test_multiple_first_savepoints_remain_owned_by_one_root(settings_db):
    await config.read_config()
    generation = config.get_config_revision()
    async with settings_db.sessions() as writer:
        first = await writer.begin_nested()
        second = await writer.begin_nested()
        await ConfigCRUD.set(writer, "general_limit", "7")
        await second.commit()
        await first.commit()
        assert await revision() == 0
        await writer.rollback()
    assert await revision() == 0
    assert config.get_config_revision() == generation
    assert (await config.read_config())["limits"]["general"] == 2


async def test_sync_connection_savepoint_uses_physical_root(settings_db):
    from db.crud.config_revision import ConfigRevisionCRUD

    engine = create_engine(settings_db.url.replace("sqlite+aiosqlite", "sqlite"))
    try:
        with engine.connect() as writer:
            nested = writer.begin_nested()
            ConfigRevisionCRUD.advance_connection(writer)
            writer.execute(insert(Config).values(key="general_limit", value="7"))
            nested.commit()
            assert await revision() == 0
            async with settings_db.sessions() as reader:
                assert await ConfigCRUD.get(reader, "general_limit") is None
            writer.rollback()
        assert await revision() == 0
    finally:
        engine.dispose()


async def test_rollback_keeps_local_cache_and_generation(settings_db):
    await config.read_config()
    before = config._config_cache
    generation = config.get_config_revision()
    async with settings_db.sessions() as db:
        await ConfigCRUD.set(db, "general_limit", "7")
        await db.rollback()
    assert config._config_cache is before
    assert config.get_config_revision() == generation


async def test_cancellation_rolls_back_counter_and_setting(settings_db):
    with pytest.raises(asyncio.CancelledError):
        async with database.get_db() as db:
            await ConfigCRUD.set(db, "general_limit", "7")
            raise asyncio.CancelledError
    assert await revision() == 0
    assert (await config.read_config())["limits"]["general"] == 2


async def test_two_writers_keep_both_changes_and_counter_increments(settings_db):
    async def write(key, value):
        async with database.get_db() as db:
            await ConfigCRUD.set(db, key, value)

    await asyncio.gather(write("general_limit", "7"), write("check_interval", "180"))
    assert await revision() == 2
    cfg = await config.read_config(refresh=True)
    assert (cfg["limits"]["general"], cfg["check_interval"]) == (7, 180)


async def test_panel_sync_keeps_config_revision_while_local_policy_changes_advance_it(settings_db):
    async with database.get_db() as db:
        await UserCRUD.create_or_update(db, "alice", group_ids=[7], owner_username="admin")
    assert await revision() == 0
    async with database.get_db() as db:
        await UserCRUD.set_special_limit(db, "alice", 7, fetch_from_panel=False)
        await UserCRUD.set_excepted(db, "alice", True, fetch_from_panel=False)
    assert await revision() == 2
    async with database.get_db() as db:
        await UserCRUD.delete(db, "alice")
    assert await revision() == 3
    cfg = await config.read_config()
    assert cfg["except_users"] == [] and cfg["limits"]["special"] == {}


async def test_reserved_version_cannot_be_exported_or_edited(settings_db):
    from db.crud.config_revision import REVISION_KEY

    async with database.get_db() as db:
        await ConfigCRUD.set(db, "general_limit", "7")
        assert REVISION_KEY not in await ConfigCRUD.get_all(db)
        with pytest.raises(ValueError):
            await ConfigCRUD.set(db, REVISION_KEY, {"revision": 0})
        with pytest.raises(ValueError):
            await ConfigCRUD.delete(db, REVISION_KEY)
    assert await revision() == 1


async def external_write(settings_db, key="general_limit", value="7"):
    script = """
import asyncio, sys
from db.crud.config import ConfigCRUD
from db.database import get_db, close_db
async def write():
    try:
        async with get_db() as db:
            await ConfigCRUD.set(db, sys.argv[1], sys.argv[2])
    finally:
        await close_db()
asyncio.run(write())
"""
    env = os.environ.copy()
    env.update(DATABASE_URL=settings_db.url, LOG_LEVEL="CRITICAL")
    child = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        script,
        key,
        value,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _out, err = await asyncio.wait_for(child.communicate(), 10)
    assert child.returncode == 0, err.decode()


async def test_external_process_write_is_visible_at_refresh_boundary(settings_db):
    assert (await config.read_config())["limits"]["general"] == 2
    generation = config.get_config_revision()
    await external_write(settings_db)
    assert config.get_config_revision() == generation
    assert (await config.read_config(refresh=True))["limits"]["general"] == 7
    assert config.get_config_revision() > generation


async def test_external_process_supersedes_guarded_source_save(settings_db):
    async with database.get_db() as db:
        await ConfigCRUD.set(db, "ip_source", "api")
        await ConfigCRUD.set(db, "api_ip_freshness", "60")
    await config.read_config()
    generation = config.get_config_revision()
    await external_write(settings_db, "check_interval", "180")
    assert await config.save_config_value("ip_source", "logs", expected_revision=generation) is None
    assert (await config.read_config(refresh=True))["ip_source"] == "api"


async def test_guarded_source_save_without_loaded_db_revision_cannot_overwrite_external_writer(settings_db):
    generation = config.get_config_revision()
    await external_write(settings_db, "ip_source", "api")
    assert config._config_cache_db_revision is None
    assert await config.save_config_value("ip_source", "logs", expected_revision=generation) is None
    assert (await config.read_config(refresh=True))["ip_source"] == "api"


async def test_snapshot_rejects_late_old_publication(settings_db, monkeypatch):
    async with database.get_db() as db:
        await UserCRUD.create_or_update(db, "alice", group_ids=[7])
        await UserCRUD.set_special_limit(db, "alice", 1, fetch_from_panel=False)
        await ConfigCRUD.set(db, "general_limit", "2")
    reached, release = asyncio.Event(), asyncio.Event()
    original = ConfigCRUD.get_all
    first = True

    async def paused(db):
        nonlocal first
        result = await original(db)
        if first:
            first = False
            reached.set()
            await release.wait()
        return result

    monkeypatch.setattr(ConfigCRUD, "get_all", paused)
    reader = asyncio.create_task(config.read_config_snapshot(usernames=["alice"]))
    await asyncio.wait_for(reached.wait(), 2)
    async with database.get_db() as db:
        await ConfigCRUD.set(db, "general_limit", "7")
        await UserCRUD.set_special_limit(db, "alice", 9, fetch_from_panel=False)
        await UserCRUD.create_or_update(db, "alice", group_ids=[9], owner_username="new_owner")
    release.set()
    snapshot = await asyncio.wait_for(reader, 2)
    assert (snapshot.config["limits"]["general"], snapshot.config["limits"]["special"]["alice"]) == (7, 9)
    assert snapshot.user_facts["alice"] == {"group_ids": [9], "owner_username": "new_owner"}
    assert snapshot.db_revision == await revision()


async def test_snapshot_retry_bound_preserves_degraded_state(settings_db, monkeypatch):
    original = ConfigCRUD.get_all
    loads = 0

    async def changing(db):
        nonlocal loads
        result = await original(db)
        loads += 1
        await config.invalidate_config_cache()
        return result

    monkeypatch.setattr(ConfigCRUD, "get_all", changing)
    cfg = await asyncio.wait_for(config.read_config(refresh=True), 2)
    assert cfg["config_degraded"] is True
    assert config._config_cache is None
    assert loads == 3


async def test_refresh_retries_when_commit_invalidates_cache_during_version_probe(settings_db, monkeypatch):
    await config.read_config()
    reached, release = asyncio.Event(), asyncio.Event()
    original = config._read_db_revision
    first = True

    async def paused():
        nonlocal first
        result = await original()
        if first:
            first = False
            reached.set()
            await release.wait()
        return result

    monkeypatch.setattr(config, "_read_db_revision", paused)
    reader = asyncio.create_task(config.read_config(refresh=True))
    await asyncio.wait_for(reached.wait(), 2)
    async with database.get_db() as db:
        await ConfigCRUD.set(db, "general_limit", "7")
    release.set()
    refreshed = await asyncio.wait_for(reader, 2)
    assert refreshed["limits"]["general"] == 7
    assert refreshed["_revision"] == config.get_config_revision()


async def test_external_snapshot_retires_previous_local_evidence_generation(settings_db):
    await config.read_config()
    generation = config.get_config_revision()
    await external_write(settings_db)
    snapshot = await config.read_config_snapshot()
    assert snapshot.config["limits"]["general"] == 7
    assert snapshot.local_revision > generation
    assert snapshot.local_revision == config.get_config_revision()


async def test_panel_fact_commit_cannot_split_one_physical_snapshot(settings_db, monkeypatch):
    from db.crud.config_revision import ConfigRevisionCRUD

    async with database.get_db() as db:
        await UserCRUD.create_or_update(db, "alice", group_ids=[7], owner_username="original")
    reached, release = asyncio.Event(), asyncio.Event()
    original = ConfigRevisionCRUD.read
    first = True

    async def paused(db):
        nonlocal first
        result = await original(db)
        if first:
            first = False
            reached.set()
            await release.wait()
        return result

    monkeypatch.setattr(ConfigRevisionCRUD, "read", paused)
    reader = asyncio.create_task(config.read_config_snapshot(usernames=["alice"]))
    await asyncio.wait_for(reached.wait(), 2)
    async with database.get_db() as db:
        await UserCRUD.create_or_update(db, "alice", group_ids=[9], owner_username="updated")
    release.set()
    snapshot = await asyncio.wait_for(reader, 2)
    assert snapshot.db_revision == 0
    assert snapshot.user_facts["alice"] == {"group_ids": [7], "owner_username": "original"}
    next_snapshot = await config.read_config_snapshot(usernames=["alice"])
    assert next_snapshot.db_revision == 0
    assert next_snapshot.user_facts["alice"] == {"group_ids": [9], "owner_username": "updated"}


@pytest.mark.parametrize("value", [None, {}, {"revision": True}, {"revision": -1}, "invalid"])
async def test_corrupt_revision_is_never_current_or_writable(settings_db, value):
    from db.crud.config_revision import ConfigRevisionCRUD, REVISION_KEY

    await config.read_config()
    async with settings_db.sessions.begin() as db:
        db.add(Config(key=REVISION_KEY, value=value))
    async with database.get_db() as db:
        with pytest.raises(ValueError, match="Invalid persisted configuration revision"):
            await ConfigRevisionCRUD.read(db)
    refreshed = await config.read_config(refresh=True)
    assert refreshed["config_degraded"] is True
    assert config._config_cache is None
    with pytest.raises(ValueError, match="Invalid persisted configuration revision"):
        async with database.get_db() as db:
            await ConfigCRUD.set(db, "general_limit", "7", expected_db_revision=0)
    async with database.get_db() as db:
        assert await ConfigCRUD.get(db, "general_limit") is None


async def test_refresh_with_unchanged_version_keeps_full_config_cache(settings_db, monkeypatch):
    cfg = await config.read_config()
    generation = config.get_config_revision()

    async def unexpected_reload(_db):
        raise AssertionError("An unchanged revision should avoid loading all settings")

    monkeypatch.setattr(ConfigCRUD, "get_all", unexpected_reload)
    assert await config.read_config(refresh=True) == cfg
    assert config.get_config_revision() == generation


async def test_version_read_failure_drops_warm_cache(settings_db, monkeypatch):
    from db.crud.config_revision import ConfigRevisionCRUD

    await config.read_config()

    async def unavailable(_db):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(ConfigRevisionCRUD, "read", unavailable)
    refreshed = await config.read_config(refresh=True)
    assert refreshed["config_degraded"] is True
    assert config._config_cache is None
    assert config._config_cache_db_revision is None


async def test_scanner_refreshes_external_commit_on_next_iteration(settings_db, monkeypatch):
    import utils.check_usage as usage

    await config.read_config()
    await external_write(settings_db)
    observed = []

    async def enforce(_panel, *, config_data):
        observed.append(config_data["limits"]["general"])

    async def stop(_delay):
        raise asyncio.CancelledError

    monkeypatch.setattr(usage, "read_config", config.read_config)
    monkeypatch.setattr(usage, "check_users_usage", enforce)
    monkeypatch.setattr(usage.asyncio, "sleep", stop)
    with pytest.raises(asyncio.CancelledError):
        await usage.run_check_users_usage(None)
    assert observed == [7]


async def test_missing_mutations_and_empty_delete_do_not_advance_revision(settings_db):
    async with database.get_db() as db:
        assert await UserCRUD.set_special_limit(db, "missing", 7, fetch_from_panel=False) is None
        assert await UserCRUD.set_excepted(db, "missing", True, fetch_from_panel=False) is None
        assert await ConfigCRUD.delete(db, "missing") is False
        assert await UserCRUD.delete_many(db, ["missing"]) == 0
    assert await revision() == 0
    async with database.get_db() as db:
        await UserCRUD.create_or_update(db, "alice")
        await UserCRUD.create_or_update(db, "bob")
    async with database.get_db() as db:
        assert await UserCRUD.delete_many(db, ["alice", "bob", "missing"]) == 2
    assert await revision() == 1


async def test_policy_facts_batch_boundary_keeps_all_existing_names(settings_db):
    names = [f"user{index:04}" for index in range(501)]
    async with settings_db.sessions.begin() as db:
        db.add_all(User(username=name, group_ids=[index], owner_username="admin") for index, name in enumerate(names))
    snapshot = await config.read_config_snapshot(usernames=[*names, names[0], "missing"])
    assert len(snapshot.user_facts) == 501
    assert snapshot.user_facts[names[-1]] == {"group_ids": [500], "owner_username": "admin"}
    assert "missing" not in snapshot.user_facts


async def seed_legacy_policy_tables(settings_db):
    async with settings_db.engine.begin() as connection:
        await connection.execute(text("INSERT INTO user_limits (username, \"limit\") VALUES ('alice', 7)"))
        await connection.execute(
            text("INSERT INTO except_users (username, reason, created_by) VALUES ('alice', 'legacy', 'admin')")
        )


async def test_raw_legacy_policy_import_versions_actual_changes(settings_db):
    await seed_legacy_policy_tables(settings_db)
    await config.read_config()
    async with settings_db.engine.begin() as connection:
        await connection.run_sync(database._sync_deprecated_legacy_tables_sync)
    assert await revision() == 1
    refreshed = await config.read_config(refresh=True)
    assert refreshed["limits"]["special"] == {"alice": 7}
    assert refreshed["except_users"] == ["alice"]


async def test_raw_legacy_policy_import_no_change_keeps_revision(settings_db):
    await seed_legacy_policy_tables(settings_db)
    async with settings_db.engine.begin() as connection:
        await connection.run_sync(database._sync_deprecated_legacy_tables_sync)
    async with settings_db.engine.begin() as connection:
        await connection.run_sync(database._sync_deprecated_legacy_tables_sync)
    assert await revision() == 1


async def test_raw_legacy_policy_import_rollback_keeps_revision_and_policy(settings_db):
    await seed_legacy_policy_tables(settings_db)
    with pytest.raises(RuntimeError, match="cancel startup"):
        async with settings_db.engine.begin() as connection:
            await connection.run_sync(database._sync_deprecated_legacy_tables_sync)
            raise RuntimeError("cancel startup")
    assert await revision() == 0
    snapshot = await config.read_config_snapshot(usernames=["alice"])
    assert snapshot.config["limits"]["special"] == {}
    assert snapshot.config["except_users"] == []
    assert snapshot.user_facts == {}
