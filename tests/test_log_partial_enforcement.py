"""Healthy Logs observations prove violations without proving missing users inactive."""

import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import db.database as database
import utils.check_usage as usage
import utils.read_config as config
from db.crud.config import ConfigCRUD
from db.crud.users import UserCRUD
from test_config_revisions import settings_db  # noqa: F401
from test_policy_enforcement import policy_cycle  # noqa: F401
from utils.log_observations import LOG_OBSERVATIONS
from utils.monitoring_gate import get_last_completed_scan
from utils.shared_state import ACTIVE_USERS, note_node_event
from utils.types import ConnectionInfo, NodeType, UserType
from utils.warning_system.user_warning import UserLimitWarning


@pytest.fixture
async def partial_cycle(policy_cycle, monkeypatch, request):
    """Use real fleet readiness, counting, policies, warning records and persistence."""
    mode = getattr(request, "param", "node")
    monkeypatch.setenv("LOG_TIMEZONE", "UTC")
    async with database.get_db() as db:
        await ConfigCRUD.set(db, "device_count_mode", mode)
        await ConfigCRUD.set(db, "group_filter_ids", "7,8")
        await ConfigCRUD.set(db, "group_limits", '{"7": 1, "8": 3}')
        await UserCRUD.set_special_limit(db, "alice", None, fetch_from_panel=False)
        for username in ("bob", "carol"):
            await UserCRUD.create_or_update(db, username, group_ids=[7], owner_username="admin")
    cfg = await config.read_config(refresh=True)
    monkeypatch.setattr("utils.shared_state.NODE_LAST_EVENT", {})
    clock = [time.time()]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    LOG_OBSERVATIONS.configure(cfg)

    def fleet(*, recovered=False):
        return [
            NodeType(1, "healthy-1", "192.0.2.1", "connected"),
            NodeType(2, "healthy-2", "192.0.2.2", "connected"),
            NodeType(3, "unavailable-3", "192.0.2.3", "connected" if recovered else "connecting"),
        ]

    LOG_OBSERVATIONS.register_fleet(fleet())
    for node_id in (1, 2):
        note_node_event(node_id)
    clock[0] += 61
    for node_id in (1, 2):
        note_node_event(node_id)

    def warning(username="alice", *, expired=False, count=2):
        record = UserLimitWarning(
            username=username,
            ip_count=2,
            ips={"198.51.100.10"},
            warning_time=clock[0] - 120,
            monitoring_end_time=clock[0] - 1 if expired else clock[0] + 180,
            consecutive_violations=count,
            last_scan_time=clock[0] - 61,
            max_warnings_at_creation=3,
        )
        policy_cycle.system.warnings[username] = record
        return record

    def observed(username, *, count=2):
        user = UserType(name=username, ip=["198.51.100.10"])
        user.device_info.connections = [
            ConnectionInfo("198.51.100.10", node_id, f"healthy-{node_id}", f"inbound-{node_id}", clock[0] - 1)
            for node_id in range(1, count + 1)
        ]
        user.device_info.inbound_protocols = {
            connection.inbound_protocol for connection in user.device_info.connections
        }
        return user

    async def run(users):
        ACTIVE_USERS.clear()
        ACTIVE_USERS.update({user.name: user for user in users})
        await usage.check_users_usage(policy_cycle.panel, cfg)

    return SimpleNamespace(
        system=policy_cycle.system,
        applied=policy_cycle.applied,
        panel=policy_cycle.panel,
        config=cfg,
        clock=clock,
        warning=warning,
        observed=observed,
        run=run,
        fleet=fleet,
    )


@pytest.mark.parametrize("partial_cycle", ["node", "inbound"], indirect=True)
async def test_partial_healthy_violation_creates_one_warning_and_receipt(partial_cycle):
    cycle = partial_cycle
    await cycle.run([cycle.observed("alice")])

    assert "alice" in cycle.system.warnings
    warning = cycle.system.warnings["alice"]
    assert warning.ip_count == 2
    assert warning.consecutive_violations == 1
    assert len(cycle.system.warnings) == 1
    assert cycle.applied == []
    receipt = get_last_completed_scan()
    assert receipt is not None
    assert receipt.users_counted == 1
    assert receipt.coverage_complete is False
    assert receipt.required_node_ids == frozenset({1, 2, 3})
    assert receipt.eligible_node_ids == frozenset({1, 2})


@pytest.mark.parametrize("partial_cycle", ["node", "inbound"], indirect=True)
@pytest.mark.parametrize("expired", [False, True])
async def test_partial_healthy_threshold_punishes_once(partial_cycle, expired):
    cycle = partial_cycle
    cycle.warning(expired=expired, count=3 if expired else 2)
    await cycle.run([cycle.observed("alice")])

    assert cycle.applied == ["alice"]
    assert "alice" not in cycle.system.warnings


@pytest.mark.parametrize("partial_cycle", ["node", "inbound"], indirect=True)
@pytest.mark.parametrize("expired", [False, True])
async def test_partial_preserves_under_limit_and_absent_records_while_warning_positive_user(partial_cycle, expired):
    cycle = partial_cycle
    retained = {name: cycle.warning(name, expired=expired, count=3) for name in ("bob", "carol")}
    await cycle.system.save_warnings()
    before = json.loads(Path(cycle.system.filename).read_text())
    await cycle.run([cycle.observed("alice"), cycle.observed("bob", count=1)])

    assert "alice" in cycle.system.warnings
    assert cycle.system.warnings["alice"].consecutive_violations == 1
    assert cycle.applied == []
    saved = json.loads(Path(cycle.system.filename).read_text())
    for name, record in retained.items():
        assert cycle.system.warnings[name] is record
        assert saved[name] == before[name]
    assert get_last_completed_scan().protected_users_count == 2


@pytest.mark.parametrize("partial_cycle", ["device", "ip"], indirect=True)
async def test_partial_nonmonotone_modes_cannot_warn_or_publish_completed_receipt(partial_cycle):
    cycle = partial_cycle
    await cycle.run([cycle.observed("alice")])

    assert cycle.system.warnings == {}
    assert cycle.applied == []
    assert get_last_completed_scan() is None


@pytest.mark.parametrize("partial_cycle", ["node", "inbound"], indirect=True)
async def test_partial_report_includes_healthy_violating_user(partial_cycle):
    cycle = partial_cycle
    ACTIVE_USERS["alice"] = cycle.observed("alice")

    report = await usage.check_ip_used(cycle.config)

    assert "alice" in report
    assert report["alice"] == ["198.51.100.10"]


@pytest.mark.parametrize("partial_cycle", ["node", "inbound"], indirect=True)
@pytest.mark.parametrize("expired", [False, True])
async def test_partial_limit_raise_after_policy_await_preserves_record(partial_cycle, monkeypatch, expired):
    import utils.settings_service as policies

    cycle = partial_cycle
    record = cycle.warning(expired=expired, count=3 if expired else 2)
    await cycle.system.save_warnings()
    revision = config.get_config_revision()

    async def raise_limit():
        async with database.get_db() as db:
            await UserCRUD.create_or_update(db, "alice", group_ids=[8])

    if expired:
        original = policies.read_user_policy
        changed = False

        async def changed_policy(*args, **kwargs):
            nonlocal changed
            if not changed:
                changed = True
                await raise_limit()
            return await original(*args, **kwargs)

        monkeypatch.setattr(policies, "read_user_policy", changed_policy)
    else:
        original = cycle.system.add_warning

        async def changed_warning(*args, **kwargs):
            result = await original(*args, **kwargs)
            await raise_limit()
            return result

        monkeypatch.setattr(cycle.system, "add_warning", changed_warning)

    await cycle.run([cycle.observed("alice")])

    assert config.get_config_revision() == revision
    assert cycle.applied == []
    assert cycle.system.warnings.get("alice") is record
    assert record.consecutive_violations == 3
    assert json.loads(Path(cycle.system.filename).read_text())["alice"]["consecutive_violations"] == 3


@pytest.mark.parametrize("partial_cycle", ["node", "inbound"], indirect=True)
async def test_partial_expired_cleanup_rechecks_limit_after_report(partial_cycle, monkeypatch):
    cycle = partial_cycle
    original = usage.dispatch_chunked_warnings

    async def delayed_report(*args, **kwargs):
        await original(*args, **kwargs)
        # Model ordinary healthy heartbeats during a slow report, so the original
        # completed interval remains valid while the warning window expires.
        for _ in range(4):
            cycle.clock[0] += 60
            LOG_OBSERVATIONS.register_fleet(cycle.fleet())
            for node_id in (1, 2):
                note_node_event(node_id)
        async with database.get_db() as db:
            await UserCRUD.create_or_update(db, "alice", group_ids=[8])

    monkeypatch.setattr(usage, "dispatch_chunked_warnings", delayed_report)
    await cycle.run([cycle.observed("alice")])

    assert "alice" in cycle.system.warnings
    record = cycle.system.warnings["alice"]
    assert not record.is_monitoring_active()
    assert record.consecutive_violations == 1
    assert cycle.applied == []


@pytest.mark.parametrize("partial_cycle", ["node", "inbound"], indirect=True)
async def test_complete_recovered_coverage_can_clear_previously_unknown_records(partial_cycle):
    cycle = partial_cycle
    records = {name: cycle.warning(name, expired=True, count=3) for name in ("bob", "carol")}
    await cycle.run([cycle.observed("alice"), cycle.observed("bob", count=1)])
    assert all(cycle.system.warnings.get(name) is record for name, record in records.items())

    LOG_OBSERVATIONS.register_fleet(cycle.fleet(recovered=True))
    note_node_event(3)
    cycle.clock[0] += 61
    for node_id in (1, 2, 3):
        note_node_event(node_id)
    await cycle.run([cycle.observed("bob", count=1)])

    assert cycle.system.warnings == {}
    assert cycle.applied == []
    assert get_last_completed_scan().coverage_complete is True


@pytest.mark.parametrize("partial_cycle", ["node", "inbound"], indirect=True)
@pytest.mark.parametrize("retirement", ["included_fault", "source", "pause"])
async def test_partial_retirement_during_warning_save_prevents_punishment(
    partial_cycle, monkeypatch, operational_topics, retirement
):
    cycle = partial_cycle
    record = cycle.warning()
    original = cycle.system.add_warning

    async def changed_warning(*args, **kwargs):
        result = await original(*args, **kwargs)
        if retirement == "included_fault":
            LOG_OBSERVATIONS.mark_uncertain(cycle.clock[0] + 60, node_id=1, reason="future_timestamp")
        elif retirement == "source":
            assert await config.save_config_value("ip_source", "api")
        else:
            operational_topics.enabled = False
        return result

    monkeypatch.setattr(cycle.system, "add_warning", changed_warning)
    await cycle.run([cycle.observed("alice")])

    assert cycle.applied == []
    assert cycle.system.warnings.get("alice") is record
    assert record.consecutive_violations == 3
    assert json.loads(Path(cycle.system.filename).read_text())["alice"]["consecutive_violations"] == 3


@pytest.mark.parametrize("partial_cycle", ["node", "inbound"], indirect=True)
async def test_partial_unready_node_fault_during_await_keeps_healthy_enforcement(partial_cycle, monkeypatch):
    cycle = partial_cycle
    cycle.warning()
    original = cycle.system.add_warning

    async def changed_warning(*args, **kwargs):
        result = await original(*args, **kwargs)
        LOG_OBSERVATIONS.mark_uncertain(cycle.clock[0] + 60, node_id=3, reason="future_timestamp")
        return result

    monkeypatch.setattr(cycle.system, "add_warning", changed_warning)
    await cycle.run([cycle.observed("alice"), cycle.observed("bob")])

    assert cycle.applied == ["alice"]
    assert "alice" not in cycle.system.warnings
    assert cycle.system.warnings["bob"].consecutive_violations == 1


@pytest.mark.parametrize("outcome", ["completed", "skipped", "failed"])
async def test_runtime_reports_current_receipt_or_safe_unavailable_outcome(monkeypatch, operational_topics, outcome):
    cfg = {
        "ip_source": "logs",
        "device_count_mode": "node",
        "check_interval": 60,
        "_revision": config.get_config_revision(),
    }
    # A historical receipt must not make a skipped/failed attempt look complete.
    admission = usage.monitoring_snapshot()
    usage.record_completed_scan(admission, "logs", 99, required_node_ids={0}, eligible_node_ids={0})
    reports = []

    async def enforce(_panel, config_data):
        if outcome == "completed":
            usage.record_completed_scan(
                admission,
                "logs",
                5,
                protected_users_count=2,
                coverage_complete=False,
                required_node_ids={1, 2, 3},
                eligible_node_ids={1, 2},
                device_count_mode="node",
            )
        elif outcome == "failed":
            raise RuntimeError("internal detail must stay local")

    async def report_cycle(*args, **kwargs):
        reports.append((args, kwargs))

    async def stop_after_cycle(*_args):
        raise asyncio.CancelledError

    monkeypatch.setattr(usage, "read_config", AsyncMock(return_value=cfg))
    monkeypatch.setattr(usage, "check_users_usage", enforce)
    monkeypatch.setattr(usage, "wait_for_monitoring_change", stop_after_cycle)
    monkeypatch.setattr("utils.node_health_notifications.report_cycle", report_cycle)
    monkeypatch.setattr("utils.node_health_notifications.report_node_health", AsyncMock())
    with pytest.raises(asyncio.CancelledError):
        await usage.run_check_users_usage(SimpleNamespace())

    assert len(reports) == 1
    args, metadata = reports[0]
    assert args == (admission, "logs", outcome)
    assert metadata["device_count_mode"] == "node"
    assert metadata["config_revision"] == cfg["_revision"]
    if outcome == "completed":
        assert metadata["coverage_complete"] is False
        assert metadata["required_node_ids"] == frozenset({1, 2, 3})
        assert metadata["eligible_node_ids"] == frozenset({1, 2})
        assert metadata["users_counted"] == 5
        assert metadata["protected_users_count"] == 2
    else:
        assert metadata["users_counted"] == 0
        assert metadata["required_node_ids"] is None
        assert metadata["eligible_node_ids"] is None
        assert metadata["reason"]
        assert "internal detail" not in metadata["reason"]


async def test_runtime_report_failure_keeps_completed_result_and_next_interval(monkeypatch, operational_topics):
    cfg = {"ip_source": "logs", "device_count_mode": "node", "check_interval": 60}

    async def enforce(_panel, config_data):
        usage.record_completed_scan(usage.monitoring_snapshot(), "logs", 5)

    async def report_failure(*_args, **_kwargs):
        raise RuntimeError("dispatcher unavailable")

    async def stop_after_cycle(*_args):
        raise asyncio.CancelledError

    monkeypatch.setattr(usage, "read_config", AsyncMock(return_value=cfg))
    monkeypatch.setattr(usage, "check_users_usage", enforce)
    monkeypatch.setattr(usage, "wait_for_monitoring_change", stop_after_cycle)
    monkeypatch.setattr("utils.node_health_notifications.report_cycle", report_failure)
    monkeypatch.setattr("utils.node_health_notifications.report_node_health", AsyncMock())
    with pytest.raises(asyncio.CancelledError):
        await usage.run_check_users_usage(SimpleNamespace())

    assert get_last_completed_scan().users_counted == 5


@pytest.mark.parametrize(
    ("scenario", "expected_outcome", "expected_reason"),
    [
        ("warmup", "skipped", "collecting a complete check interval"),
        ("node_unavailable", "skipped", "Current source evidence or policy unavailable"),
        ("fleet_unavailable", "skipped", "Current source evidence or policy unavailable"),
        ("timestamp_uncertainty", "skipped", "Current source evidence or policy unavailable"),
        ("degraded_configuration", "skipped", "Current source evidence or policy unavailable"),
        ("unrelated_skip", "skipped", "Current source evidence or policy unavailable"),
        ("enforcement_failure", "failed", "Enforcement cycle failed"),
        ("admission_changed", "skipped", "Current source evidence or policy unavailable"),
        ("config_changed", "skipped", "Current source evidence or policy unavailable"),
        ("source_changed", "skipped", "Current source evidence or policy unavailable"),
    ],
)
async def test_runtime_forwards_only_current_evidence_warmup(
    monkeypatch, operational_topics, scenario, expected_outcome, expected_reason
):
    """Only a real initial evidence warmup qualifies for planned refresh suppression."""
    cfg = {
        "ip_source": "logs",
        "device_count_mode": "node",
        "check_interval": 60,
        "log_timezone": "UTC",
        "_revision": config.get_config_revision(),
    }
    monkeypatch.setattr("utils.shared_state.NODE_LAST_EVENT", {})
    LOG_OBSERVATIONS.configure(cfg)
    LOG_OBSERVATIONS.register_fleet(
        [NodeType(1, "healthy", "192.0.2.1", "connecting" if scenario == "node_unavailable" else "connected")]
    )
    note_node_event(1)
    if scenario == "fleet_unavailable":
        LOG_OBSERVATIONS.register_fleet(None)
    elif scenario == "timestamp_uncertainty":
        LOG_OBSERVATIONS.mark_uncertain(time.time() + 60, reason="future_timestamp")
    elif scenario == "degraded_configuration":
        cfg["config_degraded"] = True
    reports = []
    original_enforce = usage.check_users_usage

    async def enforce(panel, config_data):
        if scenario == "unrelated_skip":
            return
        wait_reason = await original_enforce(panel, config_data)
        if scenario == "enforcement_failure":
            raise RuntimeError("internal detail must stay local")
        if scenario == "admission_changed":
            await operational_topics.set_enabled(False)
        elif scenario == "config_changed":
            await config.invalidate_config_cache()
        elif scenario == "source_changed":
            LOG_OBSERVATIONS.configure({**config_data, "ip_source": "api"})
        return wait_reason

    async def report_cycle(*args, **kwargs):
        reports.append((args, kwargs))

    async def stop_after_cycle(*_args):
        raise asyncio.CancelledError

    monkeypatch.setattr(usage, "read_config", AsyncMock(return_value=cfg))
    monkeypatch.setattr(usage, "check_users_usage", enforce)
    monkeypatch.setattr(usage, "wait_for_monitoring_change", stop_after_cycle)
    monkeypatch.setattr("utils.node_health_notifications.report_cycle", report_cycle)
    monkeypatch.setattr("utils.node_health_notifications.report_node_health", AsyncMock())
    with pytest.raises(asyncio.CancelledError):
        await usage.run_check_users_usage(SimpleNamespace())

    assert get_last_completed_scan() is None
    assert len(reports) == 1
    args, metadata = reports[0]
    assert args[1:] == ("logs", expected_outcome)
    assert metadata["reason"] == expected_reason
    assert metadata["config_revision"] == cfg["_revision"]
