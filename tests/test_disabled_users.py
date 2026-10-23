#!/usr/bin/env python3
"""
Tests for the disabled-users registry.

The registry decides when a punished user gets their service back, so the rules
pinned here are the ones a mistake would be felt on: the custom timer, the
permanent (manual-only) ban, the default window, and the guarantee that a user
who is not in the local database still gets recorded - otherwise they would be
disabled on the panel with nothing left to ever re-enable them.

Every test runs against a real throw-away SQLite file, so the SQL itself is
covered rather than a mock of it.
"""

import asyncio
import json
import os
import time

import pytest


@pytest.fixture
async def registry(tmp_path, monkeypatch):
    """Point the registry at a throw-away database and return its module."""
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    import db.database as database
    import utils.handel_dis_users as dis_registry
    from db.models import Base

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    monkeypatch.setattr(
        database,
        "AsyncSessionLocal",
        async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False),
    )
    # Never let a test touch the real /var/lib registry.
    monkeypatch.setattr(dis_registry, "LEGACY_JSON_PATH", str(tmp_path / "absent.json"))
    # The old signature bound its default at import time. Keep baseline tests
    # isolated too, before the automatic migration passes its path explicitly.
    monkeypatch.setattr(dis_registry.import_legacy_json, "__defaults__", (str(tmp_path / "absent.json"),))
    monkeypatch.setattr(dis_registry, "_migrated", False)
    monkeypatch.setattr(dis_registry, "_last_migration_attempt", 0.0)
    monkeypatch.setattr(dis_registry, "_migration_lock", asyncio.Lock())

    yield dis_registry

    await engine.dispose()


class TestRecordingADisable:
    """What ``disable()`` stores."""

    async def test_default_window(self, registry):
        assert await registry.disable("user1") is True
        entry = await registry.entry_of("user1")
        assert entry.disabled_at > 0
        # No explicit timer: time_to_active_users decides later.
        assert entry.enable_at is None
        assert entry.is_permanent is False

    async def test_custom_duration(self, registry):
        before = time.time()
        assert await registry.disable("user1", duration_seconds=600) is True
        assert abs(await registry.enable_at_of("user1") - (before + 600)) < 5

    async def test_permanent(self, registry):
        assert await registry.disable("user1", permanent=True) is True
        entry = await registry.entry_of("user1")
        assert entry.enable_at == registry.PERMANENT
        assert entry.is_permanent is True

    async def test_permanent_wins_over_duration(self, registry):
        await registry.disable("user1", duration_seconds=600, permanent=True)
        assert (await registry.entry_of("user1")).is_permanent is True

    async def test_a_user_missing_from_the_database_is_still_recorded(self, registry):
        # UserCRUD.set_disabled is a no-op for a user it cannot find, which would
        # mean disabling somebody on the panel and keeping no record of it.
        assert await registry.disable("never_synced") is True
        assert await registry.is_disabled("never_synced") is True

    async def test_disable_is_idempotent(self, registry):
        await registry.disable("user1", duration_seconds=60)
        await registry.disable("user1", permanent=True)
        assert (await registry.entry_of("user1")).is_permanent is True
        assert len(await registry.entries()) == 1


class TestClearing:
    """Giving the service back."""

    async def test_enable_clears_the_record(self, registry):
        await registry.disable("user1", duration_seconds=600)
        assert await registry.enable("user1") is True
        assert await registry.is_disabled("user1") is False
        assert await registry.entry_of("user1") is None

    async def test_enabling_an_untracked_user_is_harmless(self, registry):
        # No row at all: nothing to clear, and no exception either.
        assert await registry.enable("never_seen") is False

    async def test_clear_all_returns_the_names_it_cleared(self, registry):
        await registry.disable("user1")
        await registry.disable("user2", permanent=True)
        assert await registry.clear_all() == {"user1", "user2"}
        assert await registry.entries() == {}

    async def test_clear_all_on_an_empty_registry(self, registry):
        assert await registry.clear_all() == set()


class TestReads:
    """The shapes the bot, the API and the CLI consume."""

    async def test_unknown_user(self, registry):
        assert await registry.is_disabled("nobody") is False
        assert await registry.entry_of("nobody") is None
        assert await registry.disabled_at_of("nobody") is None
        assert await registry.enable_at_of("nobody") is None

    async def test_maps_and_sets_agree(self, registry):
        await registry.disable("user1")
        await registry.disable("user2")

        entries = await registry.entries()
        assert set(entries) == {"user1", "user2"}
        assert await registry.disabled_usernames() == {"user1", "user2"}
        assert set(await registry.disabled_at_map()) == {"user1", "user2"}
        assert (await registry.disabled_at_map())["user1"] == entries["user1"].disabled_at

    async def test_an_enabled_user_disappears_from_every_read(self, registry):
        await registry.disable("user1")
        await registry.enable("user1")
        assert await registry.entries() == {}
        assert await registry.disabled_usernames() == set()
        assert await registry.disabled_at_map() == {}


class TestUsersToEnable:
    """Who is due to come back on this pass."""

    async def test_expired_timer_is_due_and_a_live_one_is_not(self, registry):
        now = time.time()
        await registry.disable("expired_user", enable_at=now - 50)
        await registry.disable("waiting_user", enable_at=now + 3600)
        assert await registry.users_to_enable(60) == ["expired_user"]

    async def test_permanent_is_never_due(self, registry):
        await registry.disable("banned_user", permanent=True)
        # Even with a zero default window a permanent ban holds.
        assert await registry.users_to_enable(0) == []

    async def test_default_window_applies_when_no_timer_is_set(self, registry):
        now = time.time()
        await registry.disable("old_enough", disabled_at=now - 400)
        await registry.disable("too_recent", disabled_at=now - 10)
        assert await registry.users_to_enable(300) == ["old_enough"]

    async def test_nobody_disabled_means_nobody_due(self, registry):
        assert await registry.users_to_enable(0) == []


class TestRemainingTime:
    """What the Telegram menus show next to a disabled user."""

    async def test_not_disabled(self, registry):
        result = await registry.remaining_time("nobody", 300)
        assert result.status == registry.DisableStatus.NOT_DISABLED
        assert result.is_disabled is False

    async def test_permanent(self, registry):
        await registry.disable("banned_user", permanent=True)
        result = await registry.remaining_time("banned_user", 300)
        assert result.status == registry.DisableStatus.PERMANENT
        assert result.is_permanent is True
        assert result.seconds == 0

    async def test_timed_counts_down(self, registry):
        await registry.disable("timed_user", duration_seconds=600)
        result = await registry.remaining_time("timed_user", 300)
        assert result.status == registry.DisableStatus.TIMED
        assert 500 < result.seconds <= 600

    async def test_ready_once_the_default_window_passed(self, registry):
        await registry.disable("old_user", disabled_at=time.time() - 400)
        result = await registry.remaining_time("old_user", 300)
        assert result.status == registry.DisableStatus.READY_TO_ENABLE
        assert result.is_ready is True


class TestLegacyJsonImport:
    """Nobody may be left disabled invisibly by the move to SQLite."""

    def _write(self, path, payload):
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)

    async def test_entries_and_timers_are_carried_over(self, registry, tmp_path):
        legacy = tmp_path / "disable_users.json"
        now = time.time()
        self._write(
            legacy,
            {
                "disabled_users": {"timed": now - 10, "forever": now - 20, "plain": now - 30},
                "enable_at": {"timed": now + 600, "forever": -1},
            },
        )

        assert await registry.import_legacy_json(str(legacy)) == 3

        assert (await registry.entry_of("timed")).enable_at == pytest.approx(now + 600)
        assert (await registry.entry_of("forever")).is_permanent is True
        assert (await registry.entry_of("plain")).enable_at is None
        # The file is retired so a later restart cannot import it twice.
        assert not os.path.exists(legacy)
        assert os.path.exists(str(legacy) + ".migrated")

    async def test_the_old_list_format_is_understood(self, registry, tmp_path):
        legacy = tmp_path / "disable_users.json"
        self._write(legacy, {"disable_user": ["user1", "user2"]})

        assert await registry.import_legacy_json(str(legacy)) == 2
        assert await registry.disabled_usernames() == {"user1", "user2"}

    async def test_a_lifted_ban_is_not_resurrected(self, registry, tmp_path):
        legacy = tmp_path / "disable_users.json"
        self._write(legacy, {"disabled_users": {"user1": time.time()}})

        await registry.disable("user1", permanent=True)
        assert await registry.import_legacy_json(str(legacy)) == 0
        # The database record wins; the JSON copy cannot downgrade it.
        assert (await registry.entry_of("user1")).is_permanent is True

    async def test_a_missing_file_is_not_an_error(self, registry, tmp_path):
        assert await registry.import_legacy_json(str(tmp_path / "nope.json")) == 0

    async def test_automatic_import_under_the_guard_preserves_timers(self, registry, tmp_path, monkeypatch):
        legacy = tmp_path / "disable_users.json"
        self._write(
            legacy,
            {"disabled_users": {"timed": 100.0, "forever": 200.0}, "enable_at": {"timed": 900.0, "forever": -1}},
        )
        monkeypatch.setattr(registry, "LEGACY_JSON_PATH", str(legacy))
        monkeypatch.setattr(registry.import_legacy_json, "__defaults__", (str(legacy),))

        entries = await asyncio.wait_for(registry.entries(), timeout=1)

        assert entries["timed"].disabled_at == 100.0
        assert entries["timed"].enable_at == 900.0
        assert entries["forever"].disabled_at == 200.0
        assert entries["forever"].is_permanent is True
        assert not legacy.exists()
        assert legacy.with_suffix(".json.migrated").exists()

    async def test_automatic_import_uses_the_configured_path(self, registry, tmp_path, monkeypatch):
        legacy = tmp_path / "configured.json"
        self._write(legacy, {"disabled_users": {"configured": 100.0}, "enable_at": {"configured": -1}})
        monkeypatch.setattr(registry, "LEGACY_JSON_PATH", str(legacy))

        entries = await asyncio.wait_for(registry.entries(), timeout=1)

        assert set(entries) == {"configured"}
        assert entries["configured"].is_permanent is True

    @pytest.mark.parametrize("failure_stage", ["read", "write"])
    async def test_record_failure_keeps_the_whole_source_retryable(
        self, registry, tmp_path, monkeypatch, failure_stage
    ):
        from db.crud import UserCRUD

        legacy = tmp_path / "disable_users.json"
        self._write(legacy, {"disabled_users": {"first": 100.0, "retry": 200.0}, "enable_at": {"retry": -1}})
        operation = "get_by_username" if failure_stage == "read" else "set_disabled"
        original = getattr(UserCRUD, operation)

        async def fail_second(session, username, **kwargs):
            if username == "retry":
                raise RuntimeError("temporary database failure")
            return await original(session, username, **kwargs)

        monkeypatch.setattr(UserCRUD, operation, fail_second)
        with pytest.raises(RuntimeError, match="temporary database failure"):
            await registry.import_legacy_json(str(legacy))

        assert legacy.exists()
        assert not legacy.with_suffix(".json.migrated").exists()
        assert await registry.entries() == {}

        monkeypatch.setattr(UserCRUD, operation, original)
        assert await registry.import_legacy_json(str(legacy)) == 2
        entries = await registry.entries()
        assert entries["first"].disabled_at == 100.0
        assert entries["retry"].is_permanent is True
        assert not legacy.exists()

    async def test_failed_completion_marker_rolls_back_the_import(self, registry, tmp_path, monkeypatch):
        from db.crud import ConfigCRUD

        legacy = tmp_path / "disable_users.json"
        self._write(legacy, {"disabled_users": {"retry": 100.0}})
        original = ConfigCRUD.set

        async def fail_marker(*_args, **_kwargs):
            raise RuntimeError("completion write failed")

        monkeypatch.setattr(ConfigCRUD, "set", fail_marker)
        with pytest.raises(RuntimeError, match="completion write failed"):
            await registry.import_legacy_json(str(legacy))

        assert legacy.exists()
        assert await registry.entries() == {}
        monkeypatch.setattr(ConfigCRUD, "set", original)
        assert await registry.import_legacy_json(str(legacy)) == 1

    async def test_commit_failure_keeps_the_source_and_rows_retryable(self, registry, tmp_path, monkeypatch):
        from sqlalchemy.ext.asyncio import AsyncSession

        legacy = tmp_path / "disable_users.json"
        self._write(legacy, {"disabled_users": {"retry": 100.0}})

        async def fail_commit(_session):
            raise RuntimeError("commit failed")

        with monkeypatch.context() as patch:
            patch.setattr(AsyncSession, "commit", fail_commit)
            with pytest.raises(RuntimeError, match="commit failed"):
                await registry.import_legacy_json(str(legacy))

        assert legacy.exists()
        assert await registry.entries() == {}
        assert await registry.import_legacy_json(str(legacy)) == 1

    async def test_cancellation_rolls_back_import_and_allows_retry(self, registry, tmp_path, monkeypatch):
        from db.crud import UserCRUD

        legacy = tmp_path / "disable_users.json"
        self._write(legacy, {"disabled_users": {"first": 100.0, "paused": 200.0}, "enable_at": {"paused": -1}})
        monkeypatch.setattr(registry, "LEGACY_JSON_PATH", str(legacy))
        monkeypatch.setattr(registry.import_legacy_json, "__defaults__", (str(legacy),))
        reached_write = asyncio.Event()
        original = UserCRUD.set_disabled

        async def pause_second(session, username, **kwargs):
            if username == "paused":
                reached_write.set()
                await asyncio.Event().wait()
            return await original(session, username, **kwargs)

        monkeypatch.setattr(UserCRUD, "set_disabled", pause_second)
        task = asyncio.create_task(registry.entries())
        try:
            await asyncio.wait_for(reached_write.wait(), timeout=1)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        assert legacy.exists()
        assert registry._migrated is False
        assert await registry.entries() == {}

        monkeypatch.setattr(UserCRUD, "set_disabled", original)
        monkeypatch.setattr(registry, "_last_migration_attempt", 0.0)
        entries = await asyncio.wait_for(registry.entries(), timeout=1)
        assert set(entries) == {"first", "paused"}
        assert entries["paused"].is_permanent is True

    async def test_rename_failure_cannot_resurrect_a_lifted_ban(self, registry, tmp_path, monkeypatch):
        legacy = tmp_path / "disable_users.json"
        self._write(legacy, {"disabled_users": {"lifted": 100.0}})
        original = os.rename

        def fail_rename(*_args):
            raise OSError("archive destination unavailable")

        monkeypatch.setattr(registry.os, "rename", fail_rename)
        assert await registry.import_legacy_json(str(legacy)) == 1
        assert legacy.exists()
        assert await registry.enable("lifted") is True

        monkeypatch.setattr(registry.os, "rename", original)
        monkeypatch.setattr(registry, "_migrated", False)
        monkeypatch.setattr(registry, "_last_migration_attempt", 0.0)
        assert await registry.import_legacy_json(str(legacy)) == 0
        assert await registry.is_disabled("lifted") is False
        assert not legacy.exists()

    async def test_a_completed_source_path_is_not_imported_again(self, registry, tmp_path):
        legacy = tmp_path / "disable_users.json"
        self._write(legacy, {"disabled_users": {"original": 100.0}})
        assert await registry.import_legacy_json(str(legacy)) == 1
        assert await registry.enable("original") is True

        self._write(legacy, {"disabled_users": {"original": 100.0, "replacement": 200.0}})
        assert await registry.import_legacy_json(str(legacy)) == 0
        assert await registry.entries() == {}

    async def test_overlapping_import_cannot_replay_after_manual_enable(self, registry, tmp_path, monkeypatch):
        from db.crud import ConfigCRUD

        legacy = tmp_path / "disable_users.json"
        self._write(legacy, {"disabled_users": {"lifted": 100.0}})
        first_read = asyncio.Event()
        release_first = asyncio.Event()
        original_get = ConfigCRUD.get
        first_task = None

        async def pause_first_read(session, key, default=None):
            value = await original_get(session, key, default)
            if asyncio.current_task() is first_task and key.startswith("migration.disabled_users_json."):
                first_read.set()
                await release_first.wait()
            return value

        def fail_rename(*_args):
            raise OSError("archive destination unavailable")

        monkeypatch.setattr(ConfigCRUD, "get", pause_first_read)
        monkeypatch.setattr(registry.os, "rename", fail_rename)
        first_task = asyncio.create_task(registry.import_legacy_json(str(legacy)))
        second_task = None
        try:
            await asyncio.wait_for(first_read.wait(), timeout=1)
            second_task = asyncio.create_task(registry.import_legacy_json(str(legacy)))
            try:
                # Independent sessions either serialize their marker check, or
                # expose the stale-read replay when the second writer finishes.
                await asyncio.wait_for(asyncio.shield(second_task), timeout=0.1)
            except asyncio.TimeoutError:
                release_first.set()
                await first_task
            assert await registry.enable("lifted") is True
            release_first.set()
            await asyncio.gather(first_task, second_task)
            assert await registry.is_disabled("lifted") is False
        finally:
            release_first.set()
            await asyncio.gather(first_task, *([second_task] if second_task else []), return_exceptions=True)

    async def test_automatic_failure_retries_after_backoff(self, registry, tmp_path, monkeypatch):
        from db.crud import UserCRUD

        legacy = tmp_path / "disable_users.json"
        self._write(legacy, {"disabled_users": {"retry": 100.0}, "enable_at": {"retry": -1}})
        monkeypatch.setattr(registry, "LEGACY_JSON_PATH", str(legacy))
        monkeypatch.setattr(registry.import_legacy_json, "__defaults__", (str(legacy),))
        original = UserCRUD.set_disabled

        async def fail_write(*_args, **_kwargs):
            raise RuntimeError("database temporarily unavailable")

        monkeypatch.setattr(UserCRUD, "set_disabled", fail_write)
        assert await asyncio.wait_for(registry.entries(), timeout=1) == {}
        assert legacy.exists()
        assert registry._migrated is False

        monkeypatch.setattr(UserCRUD, "set_disabled", original)
        assert await registry.entries() == {}
        monkeypatch.setattr(registry, "_last_migration_attempt", 0.0)
        entries = await asyncio.wait_for(registry.entries(), timeout=1)
        assert entries["retry"].is_permanent is True


class TestDegradedDatabase:
    """
    An outage must fail in the safe direction.

    Reads report "nobody is disabled" so no user is auto-enabled on guesswork,
    and writes report failure so the caller can log that the user is disabled on
    the panel with nothing tracking them.
    """

    async def test_reads_are_empty_and_writes_report_failure(self, registry, monkeypatch):
        import db.database as database

        def explode(*_args, **_kwargs):
            raise RuntimeError("database is gone")

        monkeypatch.setattr(database, "AsyncSessionLocal", explode)

        assert await registry.entries() == {}
        assert await registry.disabled_at_map() == {}
        assert await registry.disabled_usernames() == set()
        assert await registry.users_to_enable(0) == []
        assert await registry.is_disabled("user1") is False
        assert await registry.entry_of("user1") is None

        assert await registry.disable("user1") is False
        assert await registry.enable("user1") is False
        assert await registry.clear_all() == set()

    async def test_remaining_time_of_an_unreachable_user(self, registry, monkeypatch):
        import db.database as database

        def explode(*_args, **_kwargs):
            raise RuntimeError("database is gone")

        monkeypatch.setattr(database, "AsyncSessionLocal", explode)

        result = await registry.remaining_time("user1", 300)
        assert result.status == registry.DisableStatus.NOT_DISABLED
