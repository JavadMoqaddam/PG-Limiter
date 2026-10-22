"""ISP requests belong to successful disable notices, never warning scans or reports."""

import asyncio

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

import utils.check_usage as usage
import utils.user_sync as user_sync
import utils.warning_system as warning_package
import utils.warning_system.enhanced_system as warning_module
from db.crud import UserCRUD
from utils.isp_detector import ISPDetector
from utils.types import ConnectionInfo, PanelType, UserType
from utils.warning_system.enhanced_system import EnhancedWarningSystem


@pytest.fixture
def notice_cycle(tmp_path, monkeypatch):
    """Keep counting/warnings real; isolate database, panel and Telegram boundaries."""
    now = [1000.0]
    monkeypatch.setattr(warning_module.time, "time", lambda: now[0])
    system = EnhancedWarningSystem(
        filename=str(tmp_path / "warnings.json"), history_filename=str(tmp_path / "history.json")
    )
    monkeypatch.setattr(usage, "warning_system", system)
    users = {"alice": UserType(name="alice", ip=["5.6.7.10", "5.6.7.20"])}
    metadata = {"alice": {"is_monitored": True, "effective_ip_limit": 1}}
    monkeypatch.setattr(user_sync, "USER_METADATA_CACHE", metadata)

    @asynccontextmanager
    async def database():
        yield object()

    monkeypatch.setattr("db.database.get_db", database)
    monkeypatch.setattr(UserCRUD, "get_all_special_limits", AsyncMock(return_value={"alice": 1}))
    monkeypatch.setattr(usage, "get_active_users_metadata_batch", AsyncMock(return_value=metadata))
    monkeypatch.setattr(usage, "get_group_limits_batch", AsyncMock(return_value={}))
    monkeypatch.setattr(usage, "pop_active_users_snapshot", AsyncMock(side_effect=lambda: users.copy()))
    monkeypatch.setattr(usage.ip_history_tracker, "record_many", AsyncMock())
    monkeypatch.setattr(usage, "_sample_is_trustworthy", lambda *_: True)
    monkeypatch.setattr(usage, "dispatch_chunked_warnings", AsyncMock())

    messages = []
    outcomes = []
    requests = []
    request_states = []
    failure = [None]

    def transport(request):
        requests.append(request)
        request_states.append("alice" in system.warnings)
        if failure[0] == "timeout":
            raise httpx.ReadTimeout("provider unavailable", request=request)
        if failure[0] == "unknown":
            return httpx.Response(200, json={})
        return httpx.Response(
            200,
            json={"org": "AS123 Example <ISP> & Co", "country": "IR", "city": "Tehran", "region": "Tehran"},
        )

    detector = ISPDetector(token="notice-token")
    detector._client = httpx.AsyncClient(transport=httpx.MockTransport(transport))
    monkeypatch.setattr(usage, "isp_detector", detector)

    async def send_notice(message, username):
        messages.append((message, username))

    async def punish(_panel, user):
        outcomes.append(user.name)
        return {
            "action": action[0],
            "duration_minutes": 15,
            "step_index": 1,
            "violation_count": 2,
            "message": "panel outcome",
            "revoke_success": True,
            "uuid_reset_success": True,
        }

    action = ["disabled"]
    monkeypatch.setattr(warning_package, "safe_disable_user_with_punishment", punish)
    monkeypatch.setattr(warning_module, "safe_disable_user_with_punishment", punish)
    monkeypatch.setattr("telegram_bot.send_message.send_disable_notification", send_notice)
    monkeypatch.setattr("telegram_bot.topics.get_topics_manager", lambda: SimpleNamespace(group_id=-100123))
    monkeypatch.setattr(warning_module, "safe_send_warning_log", AsyncMock())
    config = {"limits": {"general": 1}, "check_interval": 60, "ipinfo_token": "notice-token"}

    return {
        "system": system,
        "now": now,
        "users": users,
        "config": config,
        "detector": detector,
        "messages": messages,
        "outcomes": outcomes,
        "requests": requests,
        "request_states": request_states,
        "failure": failure,
        "action": action,
    }


async def run_cycle(case):
    await usage.check_users_usage(PanelType("admin", "secret", "panel.example"), case["config"])


async def test_warning_scans_make_no_isp_requests(notice_cycle):
    case = notice_cycle
    try:
        await run_cycle(case)
        case["now"][0] += 60
        await run_cycle(case)
        assert case["system"].warnings["alice"].consecutive_violations == 2
        assert case["requests"] == []
        assert case["outcomes"] == []
    finally:
        await case["detector"].close()


async def test_inbound_counting_fetches_isp_only_after_two_inbounds_cause_disable(notice_cycle):
    case = notice_cycle
    case["config"]["device_count_mode"] = "inbound"
    user = case["users"]["alice"]

    def observe(inbounds):
        user.device_info.connections = [
            ConnectionInfo(ip, 1, "node", inbound, case["now"][0]) for ip, inbound in zip(user.ip, inbounds)
        ]
        user.device_info.inbound_protocols = set(inbounds)

    try:
        observe(["Vless TCP", "Vless TCP"])
        await run_cycle(case)
        assert "alice" not in case["system"].warnings
        assert case["requests"] == []

        observe(["Vless TCP", "Vless WS"])
        for _ in range(2):
            case["now"][0] += 60
            await run_cycle(case)
        assert case["system"].warnings["alice"].consecutive_violations == 2
        assert case["requests"] == []

        case["now"][0] += 60
        await run_cycle(case)
        assert case["outcomes"] == ["alice"]
        assert len(case["requests"]) == 2
        assert case["request_states"] == [False, False]
        message, _username = case["messages"][-1]
        assert "USER DISABLED" in message
        assert "Example &lt;ISP&gt; &amp; Co" in message
        assert "5.6.7.10" in message and "5.6.7.20" in message
        assert "alice" not in case["system"].warnings
    finally:
        await case["detector"].close()


@pytest.mark.parametrize("path", ["scan", "expired"])
async def test_disabled_user_without_configured_telegram_group_makes_no_lookup(notice_cycle, monkeypatch, path):
    case = notice_cycle
    monkeypatch.setattr("telegram_bot.topics.get_topics_manager", lambda: SimpleNamespace(group_id=None))
    try:
        await case["system"].add_warning(
            "alice", 2, set(case["users"]["alice"].ip), 1, send_telegram_notification=False
        )
        warning = case["system"].warnings["alice"]
        warning.consecutive_violations = 2 if path == "scan" else 3
        if path == "expired":
            warning.monitoring_end_time = 900
        case["now"][0] += 60
        await run_cycle(case)
        assert case["requests"] == []
        assert case["outcomes"] == ["alice"]
        assert "alice" not in case["system"].warnings
    finally:
        await case["detector"].close()


async def test_repeated_isp_lookups_fetch_each_raw_ip_again():
    requests = []

    def transport(request):
        requests.append(request)
        return httpx.Response(200, json={"org": "Example ISP", "country": "IR"})

    detector = ISPDetector(token="notice-token")
    detector._client = httpx.AsyncClient(transport=httpx.MockTransport(transport))
    try:
        ips = ["5.6.7.10", "5.6.7.20"]
        first = await detector.get_multiple_isp_info(ips)
        second = await detector.get_multiple_isp_info(ips)
        assert len(requests) == 4
        assert {request.url.host for request in requests} == {"ipinfo.io"}
        assert first["5.6.7.10"]["isp"] == second["5.6.7.20"]["isp"] == "Example ISP"
    finally:
        await detector.close()


@pytest.mark.parametrize("path", ["scan", "expired"])
@pytest.mark.parametrize("action", ["disabled", "revoked"])
async def test_successful_disable_freshly_enriches_each_activity_ip(notice_cycle, path, action):
    case = notice_cycle
    case["action"][0] = action
    try:
        await case["system"].add_warning(
            "alice", 2, set(case["users"]["alice"].ip), 1, send_telegram_notification=False
        )
        warning = case["system"].warnings["alice"]
        warning.consecutive_violations = 2 if path == "scan" else 3
        if path == "expired":
            warning.monitoring_end_time = 900
        case["now"][0] += 60
        await run_cycle(case)

        assert len(case["requests"]) == 2
        assert {request.url.host for request in case["requests"]} == {"ipinfo.io"}
        assert {request.url.path for request in case["requests"]} == {"/5.6.7.10/json", "/5.6.7.20/json"}
        assert all(request.headers["Authorization"] == "Bearer notice-token" for request in case["requests"])
        assert case["request_states"] == [False, False]
        assert "alice" not in case["system"].warnings
        message, username = case["messages"][-1]
        assert username == "alice"
        assert "Example &lt;ISP&gt; &amp; Co" in message
        assert "5.6.7.10" in message and "5.6.7.20" in message
        assert message.count("60s active, seen 2x") == 2
    finally:
        await case["detector"].close()


@pytest.mark.parametrize("path", ["scan", "expired"])
@pytest.mark.parametrize("action", ["warning", "error", "skipped"])
async def test_non_disable_outcomes_make_no_isp_requests(notice_cycle, path, action):
    case = notice_cycle
    case["action"][0] = action
    try:
        await case["system"].add_warning(
            "alice", 2, set(case["users"]["alice"].ip), 1, send_telegram_notification=False
        )
        warning = case["system"].warnings["alice"]
        warning.consecutive_violations = 2 if path == "scan" else 3
        if path == "expired":
            warning.monitoring_end_time = 900
        case["now"][0] += 60
        await run_cycle(case)
        assert case["requests"] == []
    finally:
        await case["detector"].close()


@pytest.mark.parametrize("path", ["scan", "expired"])
@pytest.mark.parametrize("failure", ["timeout", "unknown"])
async def test_failed_lookup_still_notifies_and_clears_applied_punishment(notice_cycle, path, failure):
    case = notice_cycle
    case["failure"][0] = failure
    try:
        await case["system"].add_warning(
            "alice", 2, set(case["users"]["alice"].ip), 1, send_telegram_notification=False
        )
        warning = case["system"].warnings["alice"]
        warning.consecutive_violations = 2 if path == "scan" else 3
        if path == "expired":
            warning.monitoring_end_time = 900
        case["now"][0] += 60
        await run_cycle(case)
        message, _username = case["messages"][-1]
        assert "USER DISABLED" in message
        assert "5.6.7.10" in message and "5.6.7.20" in message
        assert "Unknown ISP" in message
        assert "alice" not in case["system"].warnings
        case["now"][0] += 60
        await run_cycle(case)
        assert case["outcomes"] == ["alice"]
    finally:
        await case["detector"].close()


@pytest.mark.parametrize("path", ["scan", "expired"])
async def test_cancellation_during_lookup_keeps_applied_punishment_cleared(notice_cycle, path):
    case = notice_cycle
    started = asyncio.Event()

    async def transport(_request):
        started.set()
        await asyncio.Event().wait()

    await case["detector"].close()
    case["detector"]._client = httpx.AsyncClient(transport=httpx.MockTransport(transport))
    try:
        await case["system"].add_warning(
            "alice", 2, set(case["users"]["alice"].ip), 1, send_telegram_notification=False
        )
        warning = case["system"].warnings["alice"]
        warning.consecutive_violations = 2 if path == "scan" else 3
        if path == "expired":
            warning.monitoring_end_time = 900
        case["now"][0] += 60
        cycle = asyncio.create_task(run_cycle(case))
        await asyncio.wait_for(started.wait(), timeout=1)
        cycle.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cycle
        assert "alice" not in case["system"].warnings
        case["now"][0] += 60
        await run_cycle(case)
        assert case["outcomes"] == ["alice"]
    finally:
        await case["detector"].close()
