"""Cross-PR behavior on the isolated rehearsal branch."""

import asyncio
import time
from unittest.mock import AsyncMock

import pytest

from test_api_evidence import api_cycle  # noqa: F401


async def test_persisted_api_inbound_pause_preserves_both_snapshots_and_failure_streak(api_cycle, monkeypatch):
    import utils.check_usage as usage
    import utils.ip_source_api as api
    import utils.shared_state as state
    from utils.read_config import read_config, save_config_value
    from utils.types import UserType

    await api_cycle.run()
    warnings = {name: (warning, warning.consecutive_violations) for name, warning in api_cycle.warning.warnings.items()}
    await save_config_value("ip_source", "api")
    await save_config_value("device_count_mode", "inbound")
    persisted = await read_config()
    # A retained published sample can coexist with persisted incompatible settings.
    assert await api.collect_active_users_from_api(api_cycle.panel, api_cycle.config)
    private = state._api_snapshot
    public = UserType(name="logs-only", ip=["4.4.4.4"])
    state.ACTIVE_USERS.clear()
    state.ACTIVE_USERS["logs-only"] = public
    assert persisted["ip_source"] == "api" and persisted["device_count_mode"] == "inbound"
    collector = AsyncMock(wraps=api.collect_active_users_from_api)
    api_pop = AsyncMock(wraps=state.pop_api_users_snapshot)
    log_pop = AsyncMock(wraps=state.pop_active_users_snapshot)
    monkeypatch.setattr(api, "collect_active_users_from_api", collector)
    monkeypatch.setattr(usage, "pop_api_users_snapshot", api_pop)
    monkeypatch.setattr(usage, "pop_active_users_snapshot", log_pop)
    monkeypatch.setattr(api, "_consecutive_dead_cycles", 1)
    monkeypatch.setattr(usage, "read_config", AsyncMock(return_value=persisted))
    await usage.check_users_usage(api_cycle.panel, persisted)

    async def stop(_delay):
        raise asyncio.CancelledError

    with monkeypatch.context() as daemon_patch:
        daemon_patch.setattr(usage.asyncio, "sleep", stop)
        with pytest.raises(asyncio.CancelledError):
            await usage.run_check_users_usage(api_cycle.panel)
    assert state._api_snapshot is private
    assert state.ACTIVE_USERS == {"logs-only": public}
    assert api._consecutive_dead_cycles == 1
    assert collector.await_count == api_pop.await_count == log_pop.await_count == 0
    assert {
        name: (warning, warning.consecutive_violations) for name, warning in api_cycle.warning.warnings.items()
    } == warnings

    # A compatible setting consumes the invalidated API sample without enforcing it.
    await save_config_value("device_count_mode", "device")
    compatible = await read_config()
    # Restore the retained old generation to prove it cannot authorize enforcement.
    state._api_snapshot = private
    await usage.check_users_usage(api_cycle.panel, compatible)
    assert state._api_snapshot is None and state.ACTIVE_USERS == {}
    assert {
        name: (warning, warning.consecutive_violations) for name, warning in api_cycle.warning.warnings.items()
    } == warnings
    api_cycle.config["device_count_mode"] = "device"
    monkeypatch.setattr(time, "time", lambda: api_cycle.now + 60)
    assert await api_cycle.run()
    assert api_cycle.warning.warnings["bob"].consecutive_violations == 2
    assert api._consecutive_dead_cycles == 0


@pytest.mark.parametrize("path", ["scan", "persistent"])
async def test_first_disable_delay_requires_rechecking_second_api_user(api_cycle, monkeypatch, path):
    import utils.warning_system as warning_package
    import utils.warning_system.enhanced_system as warnings
    import utils.warning_system.helpers as helpers

    clock = [float(api_cycle.now)]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    await api_cycle.run()
    for warning in api_cycle.warning.warnings.values():
        warning.consecutive_violations = 3 if path == "persistent" else 2
        if path == "persistent":
            warning.monitoring_end_time = api_cycle.now + 59
    clock[0] += 60
    punished = []

    async def disable(_panel, user):
        punished.append(user.name)
        return {
            "action": "disabled",
            "duration_minutes": 15,
            "step_index": 1,
            "violation_count": 2,
            "newly_applied": True,
        }

    async def enrich(warning, _config):
        assert warning.username == "alice"
        assert "alice" not in api_cycle.warning.warnings
        clock[0] = api_cycle.now + 150
        return "optional ISP details"

    enrichment = AsyncMock(side_effect=enrich)
    monkeypatch.setattr(warning_package, "safe_disable_user_with_punishment", disable)
    monkeypatch.setattr(warnings, "safe_disable_user_with_punishment", disable)
    monkeypatch.setattr(helpers, "get_disable_ip_activity", enrichment)
    monkeypatch.setattr(warnings, "get_disable_ip_activity", enrichment)
    monkeypatch.setattr(warning_package, "safe_send_disable_notification", AsyncMock())
    monkeypatch.setattr(warnings, "safe_send_disable_notification", AsyncMock())
    add_warning = AsyncMock(wraps=api_cycle.warning.add_warning)
    monkeypatch.setattr(api_cycle.warning, "add_warning", add_warning)
    assert await api_cycle.run()
    assert punished == ["alice"]
    assert enrichment.await_count == 1
    assert ("bob" in [call.args[0] for call in add_warning.await_args_list]) is (path == "scan")
    assert set(api_cycle.warning.warnings) == {"bob"}
    assert api_cycle.warning.warnings["bob"].consecutive_violations == 3


@pytest.mark.parametrize("mode", ["device", "ip"])
@pytest.mark.parametrize("path", ["scan", "persistent"])
@pytest.mark.parametrize("direct_ips, expected", [(["5.6.7.8"], 1), (["5.6.7.8", "9.9.9.9"], 2)])
async def test_cdn_api_overlap_controls_warning_normalization_and_persistent_path(
    api_cycle, monkeypatch, mode, path, direct_ips, expected
):
    import utils.panel_api.nodes as nodes
    import utils.warning_system.enhanced_system as warnings
    from utils.types import NodeType

    monkeypatch.setattr(time, "time", lambda: float(api_cycle.now))
    await api_cycle.run()
    pending = api_cycle.warning.warnings["alice"]
    if path == "persistent":
        pending.consecutive_violations = 3
        pending.monitoring_end_time = api_cycle.now + 59
    monkeypatch.setattr(time, "time", lambda: api_cycle.now + 60)
    monkeypatch.setattr(
        nodes,
        "get_nodes",
        AsyncMock(
            return_value=[NodeType(1, "direct", "192.0.2.1", "connected"), NodeType(5, "cdn", "192.0.2.5", "connected")]
        ),
    )
    api_cycle.config.update(device_count_mode=mode, cdn_nodes=[5])
    api_cycle.bodies.pop("bob")
    api_cycle.bodies["alice"] = {
        "nodes": {"1": {"ips": {ip: api_cycle.now for ip in direct_ips}}, "5": {"ips": {"5.6.7.8": api_cycle.now}}}
    }
    punished = []

    async def punish(_panel, user):
        punished.append(user.name)
        return {"action": "warning", "violation_count": 1}

    monkeypatch.setattr(warnings, "safe_disable_user_with_punishment", punish)
    notice = AsyncMock()
    monkeypatch.setattr(warnings, "safe_send_warning_log", notice)
    assert await api_cycle.run()
    if expected == 1:
        assert api_cycle.warning.warnings == {} and punished == []
    elif path == "persistent":
        assert punished == ["alice"]
        assert "Active Devices: <code>2</code> (2 IPs)" in notice.await_args.args[0]
    else:
        assert api_cycle.warning.warnings["alice"].ip_count == expected
        assert punished == []


async def test_request_snapshot_retains_direct_and_cdn_observations_after_preparation(api_cycle, monkeypatch):
    import utils.check_usage as usage
    import utils.panel_api.nodes as nodes
    from utils.types import NodeType

    clock = [float(api_cycle.now)]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    await api_cycle.run()
    api_cycle.bodies.pop("bob")
    api_cycle.config["cdn_nodes"] = [5]
    monkeypatch.setattr(
        nodes,
        "get_nodes",
        AsyncMock(
            return_value=[NodeType(1, "direct", "192.0.2.1", "connected"), NodeType(5, "cdn", "192.0.2.5", "connected")]
        ),
    )
    clock[0] += 60
    api_cycle.bodies["alice"] = {
        "nodes": {"1": {"ips": {"5.6.7.8": api_cycle.now}}, "5": {"ips": {"5.6.7.8": api_cycle.now + 50}}}
    }
    prepare = usage.read_config_snapshot

    async def delayed(*args, **kwargs):
        metadata = await prepare(*args, **kwargs)
        clock[0] = api_cycle.now + 90
        return metadata

    monkeypatch.setattr(usage, "read_config_snapshot", delayed)
    counts = []
    count = usage.count_devices

    def capture(user, *args, **kwargs):
        result = count(user, *args, **kwargs)
        counts.append(
            (result, [(connection.node_id, connection.last_seen) for connection in user.device_info.connections])
        )
        return result

    monkeypatch.setattr(usage, "count_devices", capture)
    assert await api_cycle.run()
    assert (1, [(1, api_cycle.now), (5, api_cycle.now + 50)]) in counts
    assert api_cycle.warning.warnings == {}


async def test_unknown_logs_evidence_preserves_history_without_warning_ban_clear_or_cleanup(api_cycle, monkeypatch):
    import utils.check_usage as usage
    import utils.warning_system as warning_package
    import utils.warning_system.enhanced_system as warnings
    from utils.log_observations import LOG_OBSERVATIONS
    from utils.parse_logs import parse_logs
    from utils.shared_state import note_node_event
    from utils.types import NodeType

    clock = [float(api_cycle.now)]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    await api_cycle.run()
    prior = api_cycle.warning.warnings["alice"]
    prior.consecutive_violations = 3
    prior.monitoring_end_time = api_cycle.now - 1
    api_cycle.config.update(ip_source="logs", log_node_timezones={"1": "UTC"})
    await parse_logs("", 1, "node", api_cycle.config)
    LOG_OBSERVATIONS.register_fleet([NodeType(1, "node", "192.0.2.1", "connected")])
    note_node_event(1)
    # Shared Last seen is 90s in this fixture, longer than its 60s cadence.
    clock[0] += 90
    assert LOG_OBSERVATIONS.is_current(LOG_OBSERVATIONS.evidence())
    LOG_OBSERVATIONS.register_fleet(None)
    mutation_names = ("add_warning", "check_persistent_violations", "clear_user_trust_data", "cleanup_expired_warnings")
    mutations = []
    for name in mutation_names:
        observed = AsyncMock(wraps=getattr(api_cycle.warning, name))
        monkeypatch.setattr(api_cycle.warning, name, observed)
        mutations.append(observed)
    punish = AsyncMock()
    monkeypatch.setattr(warning_package, "safe_disable_user_with_punishment", punish)
    monkeypatch.setattr(warnings, "safe_disable_user_with_punishment", punish)
    await usage.check_users_usage(api_cycle.panel, api_cycle.config)
    assert api_cycle.warning.warnings["alice"] is prior
    assert prior.consecutive_violations == 3
    assert punish.await_count == 0
    assert all(mutation.await_count == 0 for mutation in mutations)
