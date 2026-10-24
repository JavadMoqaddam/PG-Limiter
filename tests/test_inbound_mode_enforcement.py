"""Unsupported API/inbound combinations must preserve unevaluated state."""

import asyncio
import time

import pytest
from sqlalchemy import select

import utils.check_usage as usage
from db.models import User
from test_config_revisions import settings_db  # noqa: F401
from utils.isp_detector import ISPDetector
from utils.shared_state import ACTIVE_USERS
from utils.types import ConnectionInfo, PanelType, UserType
from utils.warning_system.enhanced_system import EnhancedWarningSystem
from utils.warning_system.user_warning import UserLimitWarning


def existing_warning():
    now = time.time()
    return UserLimitWarning(
        username="alice",
        ip_count=3,
        ips={"5.6.7.8", "9.9.9.9"},
        warning_time=now,
        monitoring_end_time=now + 180,
        consecutive_violations=2,
    )


async def test_direct_incompatible_enforcement_keeps_sample_and_warning(monkeypatch):
    user = UserType(name="alice", ip=["5.6.7.8", "9.9.9.9"])
    warning = existing_warning()
    ACTIVE_USERS["alice"] = user
    monkeypatch.setattr(usage.warning_system, "warnings", {"alice": warning})

    async def stop_before_reporting(**_kwargs):
        # Reporting follows the real destructive snapshot read. Stop here if the
        # unsupported mode reaches it, before any database or Telegram work.
        assert ACTIVE_USERS.get("alice") is user, "unsupported mode consumed the sample"

    monkeypatch.setattr(usage, "check_ip_used", stop_before_reporting)
    await usage.check_users_usage(
        PanelType("admin", "secret", "panel.local"),
        config_data={"ip_source": "api", "device_count_mode": "inbound"},
    )
    assert ACTIVE_USERS["alice"] is user
    assert usage.warning_system.warnings["alice"] is warning
    assert warning.consecutive_violations == 2


async def test_daemon_incompatible_mode_keeps_sample_without_collection(monkeypatch, operational_topics):
    user = UserType(name="alice", ip=["5.6.7.8", "9.9.9.9"])
    warning = existing_warning()
    ACTIVE_USERS["alice"] = user
    monkeypatch.setattr(usage.warning_system, "warnings", {"alice": warning})

    async def config(*, refresh=False):
        assert refresh is True
        return {"ip_source": "api", "device_count_mode": "inbound", "check_interval": 60}

    collections = []

    async def replace_snapshot(*_args):
        # A successful API collection normally replaces the shared sample.
        collections.append(True)
        ACTIVE_USERS.clear()
        return False

    async def stop_after_cycle(_delay):
        raise asyncio.CancelledError

    monkeypatch.setattr(usage, "read_config", config)
    monkeypatch.setattr("utils.ip_source_api.collect_active_users_from_api", replace_snapshot)
    monkeypatch.setattr(usage.asyncio, "sleep", stop_after_cycle)
    with pytest.raises(asyncio.CancelledError):
        await usage.run_check_users_usage(PanelType("admin", "secret", "panel.local"))
    assert ACTIVE_USERS["alice"] is user
    assert usage.warning_system.warnings["alice"] is warning
    assert warning.consecutive_violations == 2
    assert collections == []


@pytest.fixture
async def enforcement_cycle(monkeypatch, tmp_path, completed_logs_evidence, settings_db):
    """Use real counting, warning state, persistence and policy with local I/O."""
    from utils.shared_state import NODE_LAST_EVENT
    from utils.user_sync import USER_METADATA_CACHE

    saved_metadata = dict(USER_METADATA_CACHE)
    saved_heartbeats = dict(NODE_LAST_EVENT)
    USER_METADATA_CACHE.clear()
    NODE_LAST_EVENT.clear()
    import db.database as database
    from db.crud.config import ConfigCRUD
    from db.crud.users import UserCRUD
    from utils.read_config import read_config

    get_db = database.get_db
    async with get_db() as session:
        await UserCRUD.create_or_update(session, "alice", group_ids=[])
        await UserCRUD.set_special_limit(session, "alice", 1, fetch_from_panel=False)
        await ConfigCRUD.set(session, "general_limit", "1")
        await ConfigCRUD.set(session, "device_count_mode", "inbound")
        await ConfigCRUD.set(session, "check_interval", "60")
    await read_config()
    system = EnhancedWarningSystem(str(tmp_path / "warnings.json"), str(tmp_path / "history.json"))
    monkeypatch.setattr(usage, "warning_system", system)
    monkeypatch.setattr(usage, "isp_detector", None)
    completed_logs_evidence({"device_count_mode": "inbound", "limits": {"general": 1}, "check_interval": 60})

    async def no_external_isp(self, ips):
        return {}

    messages = []

    async def send_log(message, **_kwargs):
        messages.append(message)

    async def send_disable(message, username):
        messages.append(message)

    async def disable_user(panel, user):
        async with get_db() as session:
            stored = await session.scalar(select(User).where(User.username == user.name))
            stored.is_disabled_by_limiter = True
        return {
            "action": "disabled",
            "step_index": 0,
            "violation_count": 1,
            "duration_minutes": 15,
        }

    monkeypatch.setattr(ISPDetector, "get_multiple_isp_info", no_external_isp)
    monkeypatch.setattr(usage, "send_logs", send_log)
    monkeypatch.setattr("telegram_bot.send_message.send_logs", send_log)
    monkeypatch.setattr("telegram_bot.send_message.send_warning_log", send_log)
    monkeypatch.setattr("telegram_bot.send_message.send_disable_notification", send_disable)
    monkeypatch.setattr("utils.panel_api.disable_user_with_punishment", disable_user)
    yield system, get_db, messages
    USER_METADATA_CACHE.clear()
    USER_METADATA_CACHE.update(saved_metadata)
    NODE_LAST_EVENT.clear()
    NODE_LAST_EVENT.update(saved_heartbeats)


def observed_user(pairs):
    user = UserType(name="alice", ip=sorted({ip for ip, _inbound in pairs}))
    user.device_info.connections = [ConnectionInfo(ip, 1, "node-1", inbound, time.time()) for ip, inbound in pairs]
    user.device_info.inbound_protocols = {inbound for _ip, inbound in pairs}
    return user


@pytest.mark.parametrize("prior_warning", [False, True])
async def test_many_ips_on_one_inbound_stay_within_limit(enforcement_cycle, prior_warning):
    system, _get_db, _messages = enforcement_cycle
    if prior_warning:
        system.warnings["alice"] = existing_warning()
    ACTIVE_USERS["alice"] = observed_user([("5.6.7.8", "Vless"), ("9.9.9.9", "Vless"), ("4.4.4.4", "Vless")])
    await usage.check_users_usage(
        PanelType("admin", "secret", "panel.local"),
        config_data={"device_count_mode": "inbound", "limits": {"general": 1}, "check_interval": 60},
    )
    assert "alice" not in system.warnings


async def test_one_ip_on_two_inbounds_creates_and_retains_warning(enforcement_cycle):
    system, _get_db, _messages = enforcement_cycle
    ACTIVE_USERS["alice"] = observed_user([("5.6.7.8", "Vless TCP"), ("5.6.7.8", "Vless WS")])
    await usage.check_users_usage(
        PanelType("admin", "secret", "panel.local"),
        config_data={"device_count_mode": "inbound", "limits": {"general": 1}, "check_interval": 60},
    )
    warning = system.warnings["alice"]
    assert warning.ip_count == 2
    assert warning.consecutive_violations == 1
    assert warning.ips == {"5.6.7.8"}


async def test_persistent_violation_bans_using_inbounds_instead_of_ip_count(enforcement_cycle):
    system, get_db, messages = enforcement_cycle
    warning = existing_warning()
    warning.consecutive_violations = 3
    warning.monitoring_end_time = time.time() - 1
    system.warnings["alice"] = warning
    ACTIVE_USERS["alice"] = observed_user([("5.6.7.8", "Vless TCP"), ("5.6.7.8", "Vless WS")])
    await usage.check_users_usage(
        PanelType("admin", "secret", "panel.local"),
        config_data={"device_count_mode": "inbound", "limits": {"general": 1}, "check_interval": 60},
    )
    async with get_db() as session:
        stored = await session.scalar(select(User).where(User.username == "alice"))
        assert stored.is_disabled_by_limiter is True
    assert "alice" not in system.warnings
    assert any("Active Devices: <code>2</code> (1 IPs)" in message for message in messages)
