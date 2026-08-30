#!/usr/bin/env python3
"""
Tests for the disabled-users registry.

This registry decides when a punished user gets their service back, so what is
pinned here is the timing (custom timer, permanent, default window) and the fact
that every read comes from the single ``_entries`` store. The parallel ``set``
and ``dict`` mirrors this class used to publish were rebound on every mutation,
so anything that imported them kept a snapshot that never updated again.
"""

import json
import time

import pytest


@pytest.fixture
def registry_file(tmp_path):
    """Path for a throw-away registry file."""
    return str(tmp_path / "test_disabled.json")


@pytest.fixture
def registry(registry_file):
    """A registry bound to a temporary file."""
    from utils.handel_dis_users import DisabledUsers

    return DisabledUsers(filename=registry_file)


class TestLoading:
    """What the registry holds after reading its file."""

    def test_empty_when_the_file_is_missing(self, registry):
        assert len(registry) == 0
        assert registry.disabled_usernames() == set()

    def test_existing_file_is_loaded(self, registry_file):
        from utils.handel_dis_users import DisabledUsers

        with open(registry_file, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "disabled_users": {"user1": 1234567890.0},
                    "enable_at": {"user1": 1234567900.0},
                },
                handle,
            )

        dus = DisabledUsers(filename=registry_file)
        assert dus.is_disabled("user1")
        assert dus.disabled_at_of("user1") == 1234567890.0
        assert dus.enable_at_of("user1") == 1234567900.0

    def test_legacy_list_format_is_migrated(self, registry_file):
        from utils.handel_dis_users import DisabledUsers

        # Very old files stored a bare list with no timestamps.
        with open(registry_file, "w", encoding="utf-8") as handle:
            json.dump({"disable_user": ["user1", "user2"]}, handle)

        dus = DisabledUsers(filename=registry_file)
        assert dus.disabled_usernames() == {"user1", "user2"}
        assert isinstance(dus.disabled_at_of("user1"), float)
        # No timer is known for them, so the default window applies.
        assert dus.enable_at_of("user1") is None

    def test_corrupted_file_is_set_aside(self, registry_file):
        from utils.handel_dis_users import DisabledUsers

        with open(registry_file, "w", encoding="utf-8") as handle:
            handle.write("{not json")

        dus = DisabledUsers(filename=registry_file)
        # An unreadable file must not disable anybody, and must not crash boot.
        assert len(dus) == 0

    def test_unknown_user_reads_as_not_disabled(self, registry):
        assert registry.is_disabled("nobody") is False
        assert registry.disabled_at_of("nobody") is None
        assert registry.enable_at_of("nobody") is None


class TestAddAndRemove:
    """Adding, removing and clearing entries."""

    @pytest.mark.asyncio
    async def test_add_user_uses_the_default_window(self, registry):
        await registry.add_user("test_user")
        assert registry.is_disabled("test_user")
        assert registry.disabled_at_of("test_user") > 0
        # No explicit timer: the default time_to_active decides.
        assert registry.enable_at_of("test_user") is None

    @pytest.mark.asyncio
    async def test_add_user_with_duration_sets_a_timer(self, registry):
        before = time.time()
        await registry.add_user("timed_user", duration_seconds=3600)
        assert abs(registry.enable_at_of("timed_user") - (before + 3600)) < 5

    @pytest.mark.asyncio
    async def test_permanent_disable_is_marked_with_minus_one(self, registry):
        await registry.add_user("banned_user", permanent=True)
        assert registry.enable_at_of("banned_user") == -1.0

    @pytest.mark.asyncio
    async def test_remove_user(self, registry):
        await registry.add_user("to_remove")
        await registry.remove_user("to_remove")
        assert registry.is_disabled("to_remove") is False
        assert registry.disabled_at_map() == {}

    @pytest.mark.asyncio
    async def test_removing_an_absent_user_is_harmless(self, registry):
        await registry.remove_user("never_added")
        assert len(registry) == 0

    @pytest.mark.asyncio
    async def test_read_and_clear_returns_and_empties(self, registry):
        await registry.add_user("user1")
        await registry.add_user("user2")
        assert await registry.read_and_clear_users() == {"user1", "user2"}
        assert len(registry) == 0

    @pytest.mark.asyncio
    async def test_state_survives_a_reload(self, registry_file):
        from utils.handel_dis_users import DisabledUsers

        first = DisabledUsers(filename=registry_file)
        await first.add_user("persistent_user", duration_seconds=1800)

        second = DisabledUsers(filename=registry_file)
        assert second.is_disabled("persistent_user")
        assert second.enable_at_of("persistent_user") is not None


class TestUsersToEnable:
    """Who gets their service back on this pass."""

    def _put(self, registry, username, disabled_ago, enable_at=None):
        """Write one entry straight into the store and persist it."""
        from utils.handel_dis_users import DisabledUserEntry

        registry._entries[username] = DisabledUserEntry(
            username=username,
            disabled_at=time.time() - disabled_ago,
            enable_at=enable_at,
        )
        registry._sync_save_disabled_users()

    @pytest.mark.asyncio
    async def test_expired_custom_timer_is_ready(self, registry):
        now = time.time()
        self._put(registry, "expired_user", 100, enable_at=now - 50)
        self._put(registry, "waiting_user", 10, enable_at=now + 3600)

        ready = await registry.get_users_to_enable(60)
        assert ready == ["expired_user"]

    @pytest.mark.asyncio
    async def test_permanent_is_never_ready(self, registry):
        self._put(registry, "banned_user", 86400, enable_at=-1)
        # Even with a zero default window, a permanent disable holds.
        assert await registry.get_users_to_enable(0) == []

    @pytest.mark.asyncio
    async def test_default_window_decides_when_no_timer_is_set(self, registry):
        self._put(registry, "old_enough", 400)
        self._put(registry, "too_recent", 10)

        ready = await registry.get_users_to_enable(300)
        assert ready == ["old_enough"]


class TestRemainingTime:
    """What the Telegram menus show next to a disabled user."""

    def test_not_disabled(self, registry):
        from utils.handel_dis_users import DisableStatus

        result = registry.get_user_remaining_time("nobody", 300)
        assert result.status == DisableStatus.NOT_DISABLED
        assert result.is_disabled is False

    @pytest.mark.asyncio
    async def test_permanent(self, registry):
        from utils.handel_dis_users import DisableStatus

        await registry.add_user("banned_user", permanent=True)
        result = registry.get_user_remaining_time("banned_user", 300)
        assert result.status == DisableStatus.PERMANENT
        assert result.is_permanent is True
        assert result.seconds == 0

    @pytest.mark.asyncio
    async def test_timed_counts_down(self, registry):
        from utils.handel_dis_users import DisableStatus

        await registry.add_user("timed_user", duration_seconds=600)
        result = registry.get_user_remaining_time("timed_user", 300)
        assert result.status == DisableStatus.TIMED
        assert 500 < result.seconds <= 600

    @pytest.mark.asyncio
    async def test_ready_once_the_default_window_passed(self, registry):
        from utils.handel_dis_users import DisableStatus, DisabledUserEntry

        registry._entries["old_user"] = DisabledUserEntry(
            username="old_user", disabled_at=time.time() - 400
        )
        result = registry.get_user_remaining_time("old_user", 300)
        assert result.status == DisableStatus.READY_TO_ENABLE
        assert result.is_ready is True


class TestSharedRegistry:
    """Everything in the process must read and write the same registry."""

    def test_accessor_returns_one_instance(self, monkeypatch):
        import utils.handel_dis_users as dis_mod

        # monkeypatch restores the module global afterwards, so a singleton built
        # here cannot leak into the next test.
        monkeypatch.setattr(dis_mod, "_registry", None)
        first = dis_mod.get_disabled_users()
        second = dis_mod.get_disabled_users()
        assert first is second

    @pytest.mark.asyncio
    async def test_a_write_is_visible_to_every_reader(self, registry):
        # There are no mirrors left to fall out of sync: the map is derived from
        # the store on every call.
        await registry.add_user("shared_user", duration_seconds=120)
        assert "shared_user" in registry.disabled_at_map()
        assert "shared_user" in registry.disabled_usernames()
        assert registry.entries()["shared_user"].enable_at is not None

        await registry.remove_user("shared_user")
        assert registry.disabled_at_map() == {}
        assert registry.disabled_usernames() == set()
        assert registry.entries() == {}
