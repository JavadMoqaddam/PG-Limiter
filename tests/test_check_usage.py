"""Regression tests for fetching active users missing from local metadata."""

from contextlib import asynccontextmanager

import pytest

import utils.check_usage as check_usage
import utils.user_sync as user_sync
from db.crud import UserCRUD
from utils.types import PanelType, UserType


class FakeDetector:
    async def get_multiple_isp_info(self, _ips):
        return {}


@asynccontextmanager
async def fake_get_db():
    yield object()


async def test_unknown_active_users_are_queued_once_without_default_enforcement(
    monkeypatch,
):
    """The enforcement cycle queues unknown users but never judges them on defaults."""
    users = {
        "alice": UserType(name="alice", ip=["192.0.2.1"]),
        "bob": UserType(name="bob", ip=["192.0.2.2"]),
    }
    queued: list[str] = []
    resolved: list[str] = []
    warned: list[str] = []

    async def fake_pop_snapshot():
        return users

    async def fake_check_ip_used(*, config_data, active_users_snapshot):
        assert config_data["limits"]["general"] == 1
        assert active_users_snapshot is users
        return {}

    async def fake_special_limits(_db):
        return {}

    async def fake_detector(_config):
        return FakeDetector()

    async def fake_group_limits(_usernames, _config, _panel, *, strict):
        assert strict is True
        return {}

    async def fake_metadata(_usernames, *, strict):
        assert strict is True
        return {}

    async def fake_record_many(_users):
        return None

    async def fake_persistent(*_args, **_kwargs):
        return set(), set()

    async def fail_resolve(*, username, **_kwargs):
        resolved.append(username)
        return 1

    async def fail_warning(username, *_args, **_kwargs):
        warned.append(username)
        return "new"

    async def fake_cleanup(*, sample_is_trustworthy):
        assert sample_is_trustworthy is True

    async def fake_queue(username):
        queued.append(username)

    monkeypatch.setattr(check_usage, "pop_active_users_snapshot", fake_pop_snapshot)
    monkeypatch.setattr(check_usage, "check_ip_used", fake_check_ip_used)
    monkeypatch.setattr("db.database.get_db", fake_get_db)
    monkeypatch.setattr(UserCRUD, "get_all_special_limits", fake_special_limits)
    monkeypatch.setattr(check_usage, "_ensure_isp_detector", fake_detector)
    monkeypatch.setattr(check_usage, "get_group_limits_batch", fake_group_limits)
    monkeypatch.setattr(check_usage, "get_active_users_metadata_batch", fake_metadata)
    monkeypatch.setattr(check_usage.ip_history_tracker, "record_many", fake_record_many)
    monkeypatch.setattr(check_usage, "count_devices", lambda *_args, **_kwargs: 1)
    monkeypatch.setattr(
        check_usage, "count_devices_from_ips", lambda *_args, **_kwargs: 1
    )
    monkeypatch.setattr(check_usage, "_sample_is_trustworthy", lambda *_args: True)
    monkeypatch.setattr(
        check_usage.warning_system, "check_persistent_violations", fake_persistent
    )
    monkeypatch.setattr(check_usage.warning_system, "add_warning", fail_warning)
    monkeypatch.setattr(
        check_usage.warning_system, "cleanup_expired_warnings", fake_cleanup
    )
    monkeypatch.setattr(
        check_usage.warning_system, "get_monitoring_users", lambda: set()
    )
    monkeypatch.setattr(check_usage.warning_system, "warnings", {})
    monkeypatch.setattr(user_sync, "queue_unknown_user_fetch", fake_queue)
    monkeypatch.setattr(
        check_usage, "resolve_effective_limit", fail_resolve
    )

    await check_usage.check_users_usage(
        PanelType("admin", "secret", "panel.example"),
        config_data={"limits": {"general": 1}, "check_interval": 60},
    )

    assert sorted(queued) == ["alice", "bob"]
    assert resolved == []
    assert warned == []
