"""ISP requests belong to successful disable notices, never warning scans or reports."""

import asyncio
import json

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

import utils.check_usage as usage
import utils.panel_api.users as panel_users
import utils.punishment_system as punishment_module
import utils.user_sync as user_sync
import utils.warning_system as warning_package
import utils.warning_system.enhanced_system as warning_module
from db.crud import UserCRUD
from db.crud.config import ConfigCRUD
from test_config_revisions import settings_db  # noqa: F401
from utils.isp_detector import ISPDetector
from utils.types import ConnectionInfo, PanelType, UserType
from utils.warning_system.enhanced_system import EnhancedWarningSystem
from utils.warning_system.helpers import safe_disable_user_with_punishment


@pytest.fixture
async def notice_cycle(tmp_path, monkeypatch, completed_logs_evidence, settings_db):
    """Keep counting/warnings real; isolate database, panel and Telegram boundaries."""
    now = [1000.0]
    monkeypatch.setattr(warning_module.time, "time", lambda: now[0])
    system = EnhancedWarningSystem(
        filename=str(tmp_path / "warnings.json"), history_filename=str(tmp_path / "history.json")
    )
    monkeypatch.setattr(usage, "warning_system", system)
    users = {"alice": UserType(name="alice", ip=["5.6.7.10", "5.6.7.20"])}
    import db.database as database
    from utils.read_config import read_config

    monkeypatch.setattr(user_sync, "USER_METADATA_CACHE", {})
    async with database.get_db() as db:
        await UserCRUD.create_or_update(db, "alice", group_ids=[], owner_username="admin")
        await UserCRUD.set_special_limit(db, "alice", 1, fetch_from_panel=False)
        await ConfigCRUD.set(db, "general_limit", "1")
        await ConfigCRUD.set(db, "check_interval", "60")
        await ConfigCRUD.set(db, "ipinfo_token", "notice-token")
    config = await read_config()

    def snapshot(config):
        from utils.shared_state import _clone_user_map

        batch = _clone_user_map(users)
        for user in batch.values():
            if not user.device_info.connections:
                # IP-only fixtures model newly observed traffic in each scan.
                # Preserve explicit inbound observations and isolate fixture data
                # from the enforcement path's local freshness pruning.
                user.device_info.connections = [ConnectionInfo(ip, 1, "node", "Vless", now[0]) for ip in user.ip]
        batch.log_evidence = completed_logs_evidence(config)
        return batch

    monkeypatch.setattr(usage, "pop_active_users_snapshot", AsyncMock(side_effect=snapshot))
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
            json={
                "ip": request.url.path.rsplit("/", 1)[-1],
                "asn": "AS123",
                "as_name": "Example <ISP> & Co",
                "as_domain": "example.net",
                "country_code": "IR",
                "country": "Iran",
                "continent_code": "AS",
                "continent": "Asia",
            },
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
    await usage.check_users_usage(PanelType(panel_domain="panel.example", panel_api_key="pg_key_test"), case["config"])


@pytest.mark.parametrize("action", ["disabled", "revoked", "warning", "error"])
async def test_scan_consumes_warning_without_erasing_prior_disable_history(notice_cycle, action):
    case = notice_cycle
    await case["system"].add_warning("alice", 2, set(case["users"]["alice"].ip), 1, send_telegram_notification=False)
    case["system"].warnings["alice"].consecutive_violations = 2
    case["system"].warning_history["alice"] = [900.0]
    case["action"][0] = action
    case["now"][0] += 60
    try:
        await run_cycle(case)
        expected = [900.0, 1060.0] if action in ("disabled", "revoked") else [900.0]
        assert case["system"].warning_history["alice"] == expected
    finally:
        await case["detector"].close()


@pytest.mark.parametrize("active", [True, False])
async def test_ordinary_warning_cleanup_preserves_disable_history(notice_cycle, active):
    case = notice_cycle
    await case["system"].add_warning("alice", 2, set(case["users"]["alice"].ip), 1, send_telegram_notification=False)
    case["system"].warning_history["alice"] = [900.0]
    if active:
        case["users"]["alice"].ip = ["5.6.7.10"]
    else:
        case["users"].clear()
    case["now"][0] += 60
    try:
        await run_cycle(case)
        assert "alice" not in case["system"].warnings
        assert case["system"].warning_history["alice"] == [900.0]
    finally:
        await case["detector"].close()


async def test_operator_trust_reset_still_clears_warning_and_disable_history(notice_cycle):
    case = notice_cycle
    await case["system"].add_warning("alice", 2, set(case["users"]["alice"].ip), 1, send_telegram_notification=False)
    case["system"].warning_history["alice"] = [900.0]
    try:
        assert await case["system"].clear_user_trust_data("alice")
        assert "alice" not in case["system"].warnings
        assert "alice" not in case["system"].warning_history
    finally:
        await case["detector"].close()


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

        for _ in range(2):
            case["now"][0] += 60
            observe(["Vless TCP", "Vless WS"])
            await run_cycle(case)
        assert case["system"].warnings["alice"].consecutive_violations == 2
        assert case["requests"] == []

        case["now"][0] += 60
        observe(["Vless TCP", "Vless WS"])
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
        return httpx.Response(200, json={"as_name": "Example ISP", "country_code": "IR"})

    detector = ISPDetector(token="notice-token")
    detector._client = httpx.AsyncClient(transport=httpx.MockTransport(transport))
    try:
        ips = ["5.6.7.10", "5.6.7.20"]
        first = await detector.get_multiple_isp_info(ips)
        second = await detector.get_multiple_isp_info(ips)
        assert len(requests) == 4
        assert {request.url.host for request in requests} == {"api.ipinfo.io"}
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
        assert {request.url.host for request in case["requests"]} == {"api.ipinfo.io"}
        assert {request.url.path for request in case["requests"]} == {"/lite/5.6.7.10", "/lite/5.6.7.20"}
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
@pytest.mark.parametrize("step_type", ["disable", "revoke"])
async def test_applied_disable_with_failed_record_still_enriches_and_notifies(
    notice_cycle, monkeypatch, path, step_type
):
    """A failed history write cannot hide an applied panel disable or start another warning."""
    case = notice_cycle
    system = punishment_module.PunishmentSystem(filename=str(case["system"].filename) + ".violations")
    monkeypatch.setattr(punishment_module, "get_punishment_system", lambda: system)
    monkeypatch.setattr(panel_users, "check_user_exists", AsyncMock(return_value=True))
    monkeypatch.setattr(
        panel_users,
        "read_config",
        AsyncMock(return_value={"punishment": {"enabled": True, "steps": [{"type": step_type, "duration": 15}]}}),
    )
    monkeypatch.setattr(
        punishment_module,
        "get_punishment_for_user",
        AsyncMock(return_value=(punishment_module.PunishmentStep(step_type, 15), 1, 1)),
    )

    panel_disables = []

    async def disable(_panel, user, *_args, **_kwargs):
        panel_disables.append(user.name)

    async def fail_record(*_args):
        raise punishment_module.ViolationRecordError("history unavailable after panel success")

    monkeypatch.setattr(panel_users, "disable_user", disable)
    monkeypatch.setattr(panel_users, "revoke_user_subscription", AsyncMock(return_value=True))
    monkeypatch.setattr(panel_users, "reset_user_uuid", AsyncMock(return_value=True))
    monkeypatch.setattr(punishment_module, "record_user_violation", fail_record)
    monkeypatch.setattr(warning_package, "safe_disable_user_with_punishment", safe_disable_user_with_punishment)
    monkeypatch.setattr(warning_module, "safe_disable_user_with_punishment", safe_disable_user_with_punishment)

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

        assert panel_disables == ["alice"]
        assert system.has_unrecorded_incident("alice")
        assert "alice" not in case["system"].warnings
        assert "alice" not in json.loads(Path(case["system"].filename).read_text())
        assert len(case["messages"]) == 1
        message, username = case["messages"][0]
        assert username == "alice"
        assert "DISABLED" in message
        assert "not yet recorded" in message
        if step_type == "revoke":
            assert "SUBSCRIPTION REVOKED + DISABLED" in message
            assert "✅ Sub revoked" in message and "✅ UUID reset" in message
        assert "Example &lt;ISP&gt; &amp; Co" in message
        assert "5.6.7.10" in message and "5.6.7.20" in message
        assert len(case["requests"]) == 2
        assert case["request_states"] == [False, False]
        assert case["system"].warning_history["alice"] == [1060.0]

        # A still-failing ledger retry applies no new punishment and must not
        # produce another disable notice, lookup, or trust-history entry.
        history_after_disable = {name: list(times) for name, times in case["system"].warning_history.items()}
        case["now"][0] += 60
        await case["system"].add_warning(
            "alice", 2, set(case["users"]["alice"].ip), 1, send_telegram_notification=False
        )
        retry_warning = case["system"].warnings["alice"]
        retry_warning.consecutive_violations = 2 if path == "scan" else 3
        if path == "expired":
            retry_warning.monitoring_end_time = case["now"][0] - 1
        case["now"][0] += 60
        await run_cycle(case)

        assert panel_disables == ["alice"]
        assert system.has_unrecorded_incident("alice")
        assert "alice" not in case["system"].warnings
        assert "alice" not in json.loads(Path(case["system"].filename).read_text())
        assert len(case["messages"]) == 1
        assert len(case["requests"]) == 2
        assert case["system"].warning_history == history_after_disable

        # A recovered history writer reconciles the original ledger without another panel action.
        monkeypatch.setattr(punishment_module, "record_user_violation", AsyncMock())
        case["now"][0] += 60
        await case["system"].add_warning(
            "alice", 2, set(case["users"]["alice"].ip), 1, send_telegram_notification=False
        )
        recovery_warning = case["system"].warnings["alice"]
        recovery_warning.consecutive_violations = 2 if path == "scan" else 3
        if path == "expired":
            recovery_warning.monitoring_end_time = case["now"][0] - 1
        case["now"][0] += 60
        await run_cycle(case)

        assert not system.has_unrecorded_incident("alice")
        assert panel_disables == ["alice"]
        assert "alice" not in case["system"].warnings
        assert "alice" not in json.loads(Path(case["system"].filename).read_text())
        assert len(case["messages"]) == 1
        assert len(case["requests"]) == 2
        assert case["system"].warning_history == history_after_disable
    finally:
        await case["detector"].close()


@pytest.mark.parametrize("path", ["scan", "expired"])
async def test_duplicate_incident_does_not_report_a_disable_or_rewarn(notice_cycle, monkeypatch, path):
    """A claimed incident causes the real helper to deduplicate, with no new panel action."""
    case = notice_cycle
    system = punishment_module.PunishmentSystem(filename=str(case["system"].filename) + ".violations")
    monkeypatch.setattr(punishment_module, "get_punishment_system", lambda: system)
    monkeypatch.setattr(panel_users, "check_user_exists", AsyncMock(return_value=True))
    monkeypatch.setattr(warning_package, "safe_disable_user_with_punishment", safe_disable_user_with_punishment)
    monkeypatch.setattr(warning_module, "safe_disable_user_with_punishment", safe_disable_user_with_punishment)
    assert system.begin_incident("alice")
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

        assert "alice" not in case["system"].warnings
        assert "alice" not in json.loads(Path(case["system"].filename).read_text())
        assert case["messages"] == []
        assert case["requests"] == []
        assert case["system"].warning_history == {}
    finally:
        system.end_incident("alice")
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
