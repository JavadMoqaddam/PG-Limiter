"""Regression tests for fetching active users missing from local metadata."""

import utils.check_usage as check_usage
import utils.user_sync as user_sync
from test_config_revisions import settings_db  # noqa: F401
from utils.types import PanelType, UserType


class FakeDetector:
    async def get_multiple_isp_info(self, _ips):
        return {}


async def test_unknown_active_users_are_queued_once_without_default_enforcement(
    monkeypatch,
    completed_logs_evidence,
    settings_db,
):
    """The enforcement cycle queues unknown users but never judges them on defaults."""
    from utils.shared_state import ActiveUsersSnapshot

    users = ActiveUsersSnapshot(
        {
            "alice": UserType(name="alice", ip=["192.0.2.1"]),
            "bob": UserType(name="bob", ip=["192.0.2.2"]),
        }
    )
    users.log_evidence = completed_logs_evidence()
    queued: list[str] = []
    resolved: list[str] = []
    warned: list[str] = []

    async def fake_pop_snapshot(_config):
        return users

    async def fake_check_ip_used(*, config_data, active_users_snapshot, user_policies):
        assert config_data["limits"]["general"] == 1
        assert active_users_snapshot is users
        assert all(not policy.known for policy in user_policies.values())
        return {}

    async def fake_detector(_config):
        return FakeDetector()

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

    async def fake_cleanup(*, sample_is_trustworthy, protected_users, expected_warnings, authorize_clear):
        assert sample_is_trustworthy is True
        assert protected_users == {"alice", "bob"}
        assert expected_warnings == {}

    async def fake_queue(username):
        queued.append(username)

    monkeypatch.setattr(check_usage, "pop_active_users_snapshot", fake_pop_snapshot)
    monkeypatch.setattr(check_usage, "check_ip_used", fake_check_ip_used)
    monkeypatch.setattr(check_usage, "_ensure_isp_detector", fake_detector)
    monkeypatch.setattr(check_usage.ip_history_tracker, "record_many", fake_record_many)
    monkeypatch.setattr(check_usage, "count_devices", lambda *_args, **_kwargs: 1)
    monkeypatch.setattr(check_usage, "count_devices_from_ips", lambda *_args, **_kwargs: 1)
    monkeypatch.setattr(check_usage, "_sample_is_trustworthy", lambda *_args: True)
    monkeypatch.setattr(check_usage.warning_system, "check_persistent_violations", fake_persistent)
    monkeypatch.setattr(check_usage.warning_system, "add_warning", fail_warning)
    monkeypatch.setattr(check_usage.warning_system, "cleanup_expired_warnings", fake_cleanup)
    monkeypatch.setattr(check_usage.warning_system, "get_monitoring_users", lambda: set())
    monkeypatch.setattr(check_usage.warning_system, "warnings", {})
    monkeypatch.setattr(user_sync, "queue_unknown_user_fetch", fake_queue)
    monkeypatch.setattr(check_usage, "resolve_effective_limit", fail_resolve)

    await check_usage.check_users_usage(
        PanelType(panel_domain="panel.example", panel_api_key="pg_key_test"),
        config_data={"limits": {"general": 1}, "check_interval": 60},
    )

    assert sorted(queued) == ["alice", "bob"]
    assert resolved == []
    assert warned == []
