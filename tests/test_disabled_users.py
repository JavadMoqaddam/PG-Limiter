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
    monkeypatch.setattr(dis_registry, "_migrated", False)

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
