#!/usr/bin/env python3
"""
Tests for the group-filter write path.

``is_monitored`` is not evaluated per request: it is pre-computed per user and read
out of the RAM metadata cache by the enforcement gate. Writing the filter config
without recomputing left every user's flag on the *previous* filter until the next
user sync - up to one ``user_sync_interval``, five minutes by default. Tightening the
filter in that window kept enforcing users the operator had just excluded, which is
the over-enforcement direction this project treats as an incident.
"""

import ast
from pathlib import Path

import pytest

HANDLER_PATH = (
    Path(__file__).resolve().parent.parent / "telegram_bot" / "handlers" / "group_filter.py"
)


@pytest.fixture
def metadata_cache():
    """Seed USER_METADATA_CACHE with bob in group 5 and vip in group 7."""
    import utils.user_sync as user_sync

    saved = dict(user_sync.USER_METADATA_CACHE)
    user_sync.USER_METADATA_CACHE.clear()
    user_sync.USER_METADATA_CACHE.update({
        "bob": {"group_ids": [5], "owner_username": "adm", "is_excepted": False,
                "special_limit": None, "is_monitored": True, "effective_ip_limit": None},
        "vip": {"group_ids": [7], "owner_username": "adm", "is_excepted": False,
                "special_limit": None, "is_monitored": True, "effective_ip_limit": None},
    })
    yield user_sync.USER_METADATA_CACHE
    user_sync.USER_METADATA_CACHE.clear()
    user_sync.USER_METADATA_CACHE.update(saved)


class TestRecomputeFlipsTheEnforcementFlag:
    """The recompute has to change what check_usage reads, not just the config."""

    @pytest.mark.asyncio
    async def test_include_filter_unmonitors_users_outside_it(self, metadata_cache):
        from utils.user_sync import recompute_all_user_limits

        await recompute_all_user_limits({
            "group_filter": {"enabled": True, "mode": "include", "group_ids": [7]},
        })
        assert metadata_cache["bob"]["is_monitored"] is False
        assert metadata_cache["vip"]["is_monitored"] is True

    @pytest.mark.asyncio
    async def test_disabling_the_filter_monitors_everyone_again(self, metadata_cache):
        from utils.user_sync import recompute_all_user_limits

        await recompute_all_user_limits({
            "group_filter": {"enabled": True, "mode": "include", "group_ids": [7]},
        })
        assert metadata_cache["bob"]["is_monitored"] is False

        await recompute_all_user_limits({"group_filter": {"enabled": False}})
        assert metadata_cache["bob"]["is_monitored"] is True
        assert metadata_cache["vip"]["is_monitored"] is True


class TestSaveGroupFilterSetting:
    """Every filter write goes through one helper so the two steps cannot drift."""

    @pytest.mark.asyncio
    async def test_saves_invalidates_and_recomputes(self, metadata_cache, monkeypatch):
        import telegram_bot.handlers.group_filter as handler
        import utils.user_sync as user_sync

        saved, invalidated, recomputed = [], [], []

        async def fake_save(key, value):
            saved.append((key, value))
            return True

        async def fake_invalidate():
            invalidated.append(1)

        async def fake_recompute(config=None):
            recomputed.append(config)
            # Stand in for the live config the real call reads after invalidation.
            for data in metadata_cache.values():
                data["is_monitored"] = 7 in data["group_ids"]

        monkeypatch.setattr(handler, "save_config_value", fake_save)
        monkeypatch.setattr(handler, "invalidate_config_cache", fake_invalidate)
        monkeypatch.setattr(user_sync, "recompute_all_user_limits", fake_recompute)

        result = await handler._save_group_filter_setting("group_filter_ids", "7")

        assert result is True
        assert saved == [("group_filter_ids", "7")]
        assert invalidated == [1]
        assert len(recomputed) == 1
        # The flag is live now, not at the next sync.
        assert metadata_cache["bob"]["is_monitored"] is False

    @pytest.mark.asyncio
    async def test_failed_recompute_does_not_fail_a_durable_save(
        self, metadata_cache, monkeypatch, caplog
    ):
        """
        The setting is already committed when the recompute runs, so a failure there
        must not be reported as a failed save - it has to be logged instead, naming
        the consequence.
        """
        import telegram_bot.handlers.group_filter as handler
        import utils.user_sync as user_sync

        async def fake_save(_key, _value):
            return True

        async def fake_invalidate():
            return None

        async def boom(_config=None):
            raise RuntimeError("database gone")

        monkeypatch.setattr(handler, "save_config_value", fake_save)
        monkeypatch.setattr(handler, "invalidate_config_cache", fake_invalidate)
        monkeypatch.setattr(user_sync, "recompute_all_user_limits", boom)

        result = await handler._save_group_filter_setting("group_filter_enabled", "true")

        assert result is True
        assert "next user sync" in caplog.text


class TestEveryWriteUsesTheHelper:
    """Static check: a new handler that forgets the recompute reopens the window."""

    def test_no_raw_save_for_a_group_filter_key(self):
        tree = ast.parse(HANDLER_PATH.read_text(encoding="utf-8"))
        offenders = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if name != "save_config_value":
                continue
            first = node.args[0]
            if isinstance(first, ast.Constant) and str(first.value).startswith(
                "group_filter"
            ):
                offenders.append((first.value, node.lineno))
        assert offenders == [], (
            f"these write a group_filter key without recomputing is_monitored: {offenders}"
        )

    def test_all_filter_write_sites_are_converted(self):
        tree = ast.parse(HANDLER_PATH.read_text(encoding="utf-8"))
        calls = sum(
            1 for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and getattr(node.func, "id", None) == "_save_group_filter_setting"
        )
        assert calls == 8, f"expected 8 helper call sites, found {calls}"
