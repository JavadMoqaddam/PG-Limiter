"""Original log time and replay identity must survive stream and snapshot lifetimes."""

import asyncio
import time
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from utils.parse_logs import parse_logs
from utils.shared_state import ACTIVE_USERS, pop_active_users_snapshot

EPOCH = datetime(2026, 10, 23, 10, tzinfo=timezone.utc).timestamp()


def record(stamp="2026/10/23 10:00:20.123456", *, user="alice", ip="5.6.7.8", port=12345, arrow=">>"):
    return f"{stamp} from {ip}:{port} accepted tcp:example.com:443 [Vless {arrow} DIRECT] email: 1.{user}"


@pytest.fixture
async def logs(monkeypatch):
    import utils.parse_logs as parser

    clock = [EPOCH]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    monkeypatch.setattr(parser, "is_ip_accepted", AsyncMock(return_value=True))
    config = {"ip_source": "logs", "check_interval": 60, "log_node_timezones": {"1": "UTC", "2": "Europe/Istanbul"}}
    # Start observing a complete ordinary interval before the first enforcement.
    await parse_logs("", 1, "node", config)
    ready_fleet(1)
    clock[0] += 60
    return SimpleNamespace(clock=clock, config=config)


async def test_preserves_printed_microsecond_time(logs):
    await parse_logs(record(), 1, "node", logs.config)
    assert ACTIVE_USERS["alice"].device_info.connections[0].last_seen == EPOCH + 20.123456


async def test_replay_after_snapshot_is_not_new_activity(logs):
    await parse_logs(record(), 1, "node", logs.config)
    assert "alice" in await pop_active_users_snapshot()
    await parse_logs(record(), 1, "node", logs.config)
    assert ACTIVE_USERS == {}
    await parse_logs(record("2026/10/23 10:00:40.654321"), 1, "node", logs.config)
    assert ACTIVE_USERS["alice"].device_info.connections[0].last_seen == EPOCH + 40.654321


async def test_history_before_current_interval_never_becomes_fresh(logs):
    await parse_logs(record("2026/10/23 09:59:59.999999"), 1, "node", logs.config)
    assert ACTIVE_USERS == {}


async def test_same_timestamp_distinct_records_and_nodes_remain_distinct(logs):
    await parse_logs(record(), 1, "node", logs.config)
    await parse_logs(record(port=54321), 1, "node", logs.config)
    await parse_logs(record(), 1, "node", logs.config)
    await parse_logs(record(user="bob"), 1, "node", logs.config)
    await parse_logs(record("2026/10/23 13:00:20.123456", arrow="->"), 2, "other", logs.config)
    alice = ACTIVE_USERS["alice"].device_info.connections
    assert [(c.node_id, c.connection_count, c.inbound_protocol) for c in alice] == [(1, 2, "Vless"), (2, 1, "Vless")]
    assert "bob" in ACTIVE_USERS


async def test_overlapping_async_preparation_admits_duplicate_once(logs, monkeypatch):
    import utils.parse_logs as parser

    waiting = asyncio.Event()
    release = asyncio.Event()

    async def prepare(*args):
        waiting.set()
        await release.wait()
        return True

    monkeypatch.setattr(parser, "is_ip_accepted", prepare)
    first = asyncio.create_task(parse_logs(record(), 1, "node", logs.config))
    await waiting.wait()
    second = asyncio.create_task(parse_logs(record(), 1, "node", logs.config))
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(first, second)
    assert ACTIVE_USERS["alice"].device_info.connections[0].connection_count == 1


async def test_failed_preparation_can_retry_same_record(logs, monkeypatch):
    import utils.parse_logs as parser

    monkeypatch.setattr(parser, "is_ip_accepted", AsyncMock(side_effect=RuntimeError("temporary")))
    with pytest.raises(RuntimeError):
        await parse_logs(record(), 1, "node", logs.config)
    monkeypatch.setattr(parser, "is_ip_accepted", AsyncMock(return_value=True))
    await parse_logs(record(), 1, "node", logs.config)
    assert "alice" in ACTIVE_USERS


# Reuse real SQLite metadata and warning persistence, with external services isolated.
from test_api_evidence import api_cycle  # noqa: E402, F401


async def test_unknown_fleet_cannot_warn_or_clear(api_cycle, logs):
    import utils.check_usage as usage
    from utils.log_observations import LOG_OBSERVATIONS

    LOG_OBSERVATIONS.register_fleet(None)
    api_cycle.config.update(logs.config)
    await parse_logs(record(), 1, "node", api_cycle.config)
    await parse_logs(record(ip="9.9.9.9"), 1, "node", api_cycle.config)
    await usage.check_users_usage(api_cycle.panel, api_cycle.config)
    assert api_cycle.warning.warnings == {}


def ready_fleet(*ids):
    from utils.log_observations import LOG_OBSERVATIONS
    from utils.shared_state import note_node_event
    from utils.types import NodeType

    LOG_OBSERVATIONS.register_fleet([NodeType(i, f"node {i}", "192.0.2.1", "connected") for i in ids])
    for node_id in ids:
        note_node_event(node_id)


@pytest.mark.parametrize("stamp", ["bad time", "2026/10/23 10:01:01.000001", "2026/10/23 10:00:20.1234567"])
async def test_unusable_accepted_timestamp_blocks_whole_cycle(api_cycle, logs, stamp):
    import utils.check_usage as usage

    api_cycle.config.update(logs.config)
    ready_fleet(1)
    await parse_logs(record(), 1, "node", api_cycle.config)
    await parse_logs(record(ip="9.9.9.9"), 1, "node", api_cycle.config)
    await parse_logs(record(stamp, user="bob"), 1, "node", api_cycle.config)
    await usage.check_users_usage(api_cycle.panel, api_cycle.config)
    assert api_cycle.warning.warnings == {}


async def test_rejected_ip_with_invalid_time_does_not_poison_live_evidence(logs, monkeypatch):
    import utils.parse_logs as parser
    from utils.log_observations import LOG_OBSERVATIONS

    ready_fleet(1)
    monkeypatch.setattr(parser, "is_ip_accepted", AsyncMock(return_value=False))
    await parse_logs(record("bad time"), 1, "node", logs.config)
    assert not LOG_OBSERVATIONS.evidence().unavailable_reason


@pytest.mark.parametrize("zones", [{"1": "UTC"}, {"1": "UTC", "2": "bad/zone"}])
async def test_quiet_required_node_needs_valid_override_even_with_default(logs, zones):
    from utils.log_observations import LOG_OBSERVATIONS

    # Missing per-node override can use an explicit default; invalid cannot.
    config = dict(logs.config, log_node_timezones=zones)
    if "2" in zones:
        config["log_timezone"] = "UTC"
    await parse_logs("", 1, "node", config)
    logs.clock[0] += 60
    ready_fleet(1, 2)
    assert "timezone" in LOG_OBSERVATIONS.evidence().unavailable_reason


@pytest.mark.parametrize("stamp", ["2026/11/01 01:30:00.000001", "2026/03/08 02:30:00.000001"])
async def test_dst_fold_and_gap_are_unavailable(logs, stamp):
    from utils.log_observations import LOG_OBSERVATIONS

    config = dict(logs.config, log_node_timezones={"1": "America/New_York"})
    await parse_logs("", 1, "node", config)
    logs.clock[0] += 60
    ready_fleet(1)
    await parse_logs(record(stamp), 1, "node", config)
    assert ACTIVE_USERS == {}
    assert "uncertainty" in LOG_OBSERVATIONS.evidence().unavailable_reason


@pytest.mark.parametrize("fraction,offset", [("", 20.0), (".1", 20.1), (".123", 20.123), (".123456", 20.123456)])
async def test_supported_printed_precision(logs, fraction, offset):
    await parse_logs(record("2026/10/23 10:00:20" + fraction), 1, "node", logs.config)
    assert ACTIVE_USERS["alice"].device_info.connections[0].last_seen == EPOCH + offset


async def test_capacity_eviction_blocks_until_forgotten_record_expires(logs):
    from utils.log_observations import LOG_OBSERVATIONS

    config = dict(logs.config, log_replay_cache_size=1)
    logs.clock[0] = EPOCH
    await parse_logs("", 1, "node", config)
    logs.clock[0] = EPOCH + 60
    ready_fleet(1)
    await parse_logs(record(), 1, "node", config)
    await parse_logs(record("2026/10/23 10:00:40.000000"), 1, "node", config)
    assert len(LOG_OBSERVATIONS.seen[1]) == 1
    assert "uncertainty" in LOG_OBSERVATIONS.evidence().unavailable_reason
    logs.clock[0] = EPOCH + 80.123456
    assert not LOG_OBSERVATIONS.evidence().unavailable_reason
    await parse_logs(record(), 1, "node", config)
    assert len(LOG_OBSERVATIONS.seen[1]) == 1


async def test_preparation_delay_retains_original_event_time(logs, monkeypatch):
    import utils.parse_logs as parser

    async def prepare(*args):
        logs.clock[0] += 10
        return True

    monkeypatch.setattr(parser, "is_ip_accepted", prepare)
    await parse_logs(record(), 1, "node", logs.config)
    assert ACTIVE_USERS["alice"].device_info.connections[0].last_seen == EPOCH + 20.123456


async def test_source_switch_during_parser_preparation_cannot_write_api_map(logs, monkeypatch):
    import utils.parse_logs as parser
    from utils.shared_state import discard_source_observations
    from utils.types import UserType

    async def prepare(*args):
        await discard_source_observations()
        ACTIVE_USERS["api-only"] = UserType(name="api-only", ip=["9.9.9.9"])
        return True

    monkeypatch.setattr(parser, "is_ip_accepted", prepare)
    await parse_logs(record(), 1, "node", logs.config)
    assert set(ACTIVE_USERS) == {"api-only"}


async def test_fixed_completed_interval_survives_slow_enforcement_preparation(api_cycle, logs, monkeypatch):
    import utils.check_usage as usage

    api_cycle.config.update(logs.config)
    ready_fleet(1)
    await parse_logs(record(), 1, "node", api_cycle.config)
    await parse_logs(record(ip="9.9.9.9"), 1, "node", api_cycle.config)

    async def slow_isp(*args):
        logs.clock[0] = EPOCH + 125
        return {}

    monkeypatch.setattr(usage.ISPDetector, "get_multiple_isp_info", slow_isp)
    await usage.check_users_usage(api_cycle.panel, api_cycle.config)
    assert api_cycle.warning.warnings["alice"].consecutive_violations == 1


async def test_valid_empty_completed_interval_clears_warning(api_cycle, logs):
    import utils.check_usage as usage

    api_cycle.config.update(logs.config)
    ready_fleet(1)
    await parse_logs(record(), 1, "node", api_cycle.config)
    await parse_logs(record(ip="9.9.9.9"), 1, "node", api_cycle.config)
    await usage.check_users_usage(api_cycle.panel, api_cycle.config)
    assert "alice" in api_cycle.warning.warnings
    logs.clock[0] += 60
    ready_fleet(1)
    await usage.check_users_usage(api_cycle.panel, api_cycle.config)
    assert api_cycle.warning.warnings == {}


async def test_reconnect_preserves_pending_observation_and_ignores_history(logs, monkeypatch):
    import utils.get_logs as streams
    from utils.types import NodeType, PanelType

    await parse_logs(record(), 1, "node", logs.config)
    count = [0]

    class Response:
        status_code = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def aiter_lines(self):
            yield ": ping"
            yield "data: " + record()
            count[0] += 1
            if count[0] == 2:
                raise asyncio.CancelledError

    class Client(Response):
        def __init__(self, **kwargs):
            pass

        def stream(self, *args, **kwargs):
            return Response()

    monkeypatch.setattr(streams.httpx, "AsyncClient", Client)
    monkeypatch.setattr(streams, "get_token", AsyncMock(return_value=SimpleNamespace(panel_token="test")))
    monkeypatch.setattr(streams, "_ip_source_is_api", AsyncMock(return_value=False))
    monkeypatch.setattr(streams, "_update_node_status", AsyncMock())
    monkeypatch.setattr("utils.parse_logs.get_cached_parser_config", AsyncMock(return_value=logs.config))
    with pytest.raises(asyncio.CancelledError):
        await streams.get_nodes_logs(PanelType("a", "b", "panel.local"), NodeType(1, "node", "192.0.2.1", "connected"))
    assert ACTIVE_USERS["alice"].device_info.connections[0].connection_count == 1
    assert ACTIVE_USERS["alice"].device_info.connections[0].last_seen == EPOCH + 20.123456


async def test_node_registration_precedes_status_notification_and_access(logs, monkeypatch):
    import utils.get_logs as streams
    from utils.log_observations import LOG_OBSERVATIONS
    from utils.types import NodeType

    observed = []

    async def notify(*args, **kwargs):
        observed.append(LOG_OBSERVATIONS.required_nodes)

    monkeypatch.setattr(streams, "send_logs", notify)
    await streams.init_node_status_message([NodeType(1, "node", "192.0.2.1", "connected")])
    assert observed == [{1: True}]


async def test_log_timezone_env_is_explicit_unset_by_default(monkeypatch):
    from utils.read_config import load_env_config

    monkeypatch.delenv("LOG_TIMEZONE", raising=False)
    monkeypatch.delenv("LOG_NODE_TIMEZONES", raising=False)
    monkeypatch.setenv("LOG_REPLAY_CACHE_SIZE", "123")
    config = load_env_config()
    assert config.get("log_timezone") is None
    assert config.get("log_replay_cache_size") == "123"
    monkeypatch.setenv("LOG_NODE_TIMEZONES", '{"17":"Asia/Tehran"}')
    assert load_env_config()["log_node_timezones"] == '{"17":"Asia/Tehran"}'


@pytest.mark.parametrize(
    "change", [{"check_interval": 120}, {"log_node_timezones": {"1": "Etc/UTC"}}, {"ip_source": "api"}]
)
async def test_sampling_change_retires_pending_batch_and_requires_full_interval(logs, change):
    from utils.log_observations import LOG_OBSERVATIONS
    from utils.shared_state import merge_active_users_snapshot

    ready_fleet(1)
    await parse_logs(record(), 1, "node", logs.config)
    batch = await pop_active_users_snapshot()
    newer = dict(logs.config, **change)
    await pop_active_users_snapshot(newer)
    assert not LOG_OBSERVATIONS.is_current(batch.log_evidence)
    assert await merge_active_users_snapshot(batch) == 0
    assert ACTIVE_USERS == {}
    if newer["ip_source"] != "api":
        assert "complete check interval" in LOG_OBSERVATIONS.evidence().unavailable_reason
        await parse_logs(record(), 1, "node", newer)
        assert ACTIVE_USERS == {}
        logs.clock[0] += newer["check_interval"]
        ready_fleet(1)
        assert not LOG_OBSERVATIONS.evidence().unavailable_reason


async def test_old_configuration_cannot_reset_newer_generation(logs):
    from utils.read_config import get_config_revision, invalidate_config_cache
    from utils.log_observations import LOG_OBSERVATIONS

    old = dict(logs.config, _revision=get_config_revision())
    await invalidate_config_cache()
    newer = dict(logs.config, check_interval=120, _revision=get_config_revision())
    await pop_active_users_snapshot(newer)
    generation = LOG_OBSERVATIONS.generation
    await pop_active_users_snapshot(old)
    assert LOG_OBSERVATIONS.generation == generation
    assert LOG_OBSERVATIONS.interval == 120


async def test_warning_await_source_change_cannot_warn_later_user_or_ban(api_cycle, logs, monkeypatch):
    import utils.check_usage as usage
    import utils.warning_system as warning_module
    from utils.shared_state import discard_source_observations

    api_cycle.config.update(logs.config)
    ready_fleet(1)
    for username in ("alice", "bob"):
        await parse_logs(record(user=username), 1, "node", api_cycle.config)
        await parse_logs(record(user=username, ip="9.9.9.9"), 1, "node", api_cycle.config)
    add = api_cycle.warning.add_warning
    disable = AsyncMock()

    async def changed_warning(*args, **kwargs):
        result = await add(*args, **kwargs)
        await discard_source_observations()
        return result

    monkeypatch.setattr(api_cycle.warning, "add_warning", changed_warning)
    monkeypatch.setattr(warning_module, "safe_disable_user_with_punishment", disable)
    await usage.check_users_usage(api_cycle.panel, api_cycle.config)
    assert set(api_cycle.warning.warnings) == {"alice"}
    disable.assert_not_awaited()


async def test_unknown_cycle_preserves_expired_warning_and_never_cleans_up(api_cycle, logs):
    import utils.check_usage as usage

    api_cycle.config.update(logs.config)
    ready_fleet(1)
    for ip in ("5.6.7.8", "9.9.9.9"):
        await parse_logs(record(ip=ip), 1, "node", api_cycle.config)
    await usage.check_users_usage(api_cycle.panel, api_cycle.config)
    warning = api_cycle.warning.warnings["alice"]
    warning.monitoring_end_time -= 1000
    warning.consecutive_violations = 3
    logs.clock[0] += 60
    ready_fleet(1, 99)
    await usage.check_users_usage(api_cycle.panel, api_cycle.config)
    assert api_cycle.warning.warnings["alice"] is warning
    assert warning.consecutive_violations == 3


async def test_metadata_failure_after_source_change_does_not_requeue_old_log_data(api_cycle, logs, monkeypatch):
    import utils.check_usage as usage
    from utils.shared_state import discard_source_observations
    from utils.types import UserType

    api_cycle.config.update(logs.config)
    ready_fleet(1)
    await parse_logs(record(), 1, "node", api_cycle.config)

    async def fail(*args, **kwargs):
        await discard_source_observations()
        ACTIVE_USERS["api-only"] = UserType(name="api-only", ip=["9.9.9.9"])
        from utils.read_config import ConfigSnapshot, get_config_revision

        return ConfigSnapshot({"config_degraded": True}, -1, get_config_revision(), {})

    monkeypatch.setattr(usage, "read_config_snapshot", fail)
    await usage.check_users_usage(api_cycle.panel, api_cycle.config)
    assert set(ACTIVE_USERS) == {"api-only"}


async def test_stale_parser_waiting_for_map_lock_cannot_clear_new_source(logs):
    from utils.shared_state import ACTIVE_USERS_LOCK
    from utils.log_observations import LOG_OBSERVATIONS
    from utils.read_config import get_config_revision, invalidate_config_cache
    from utils.types import UserType

    config = dict(logs.config, _revision=get_config_revision())
    async with ACTIVE_USERS_LOCK:
        parsing = asyncio.create_task(parse_logs(record(), 1, "node", config))
        await asyncio.sleep(0)
        await invalidate_config_cache()
        LOG_OBSERVATIONS.invalidate()
        ACTIVE_USERS["new-source"] = UserType(name="new-source", ip=["9.9.9.9"])
    await parsing
    assert set(ACTIVE_USERS) == {"new-source"}


async def test_recovered_fleet_waits_for_complete_interval(logs):
    from utils.log_observations import LOG_OBSERVATIONS

    ready_fleet(1)
    logs.clock[0] += 1
    LOG_OBSERVATIONS.register_fleet(None)
    logs.clock[0] += 1
    ready_fleet(1)
    assert LOG_OBSERVATIONS.evidence().unavailable_reason
    logs.clock[0] += 60
    ready_fleet(1)
    assert not LOG_OBSERVATIONS.evidence().unavailable_reason


async def test_persistent_pending_clear_rechecks_evidence_after_later_await(api_cycle, logs, monkeypatch):
    import utils.check_usage as usage
    import utils.settings_service as policies
    from utils.log_observations import LOG_OBSERVATIONS
    from utils.shared_state import discard_source_observations

    api_cycle.config.update(logs.config)
    for user in ("alice", "bob"):
        for ip in ("5.6.7.8", "9.9.9.9"):
            await parse_logs(record(user=user, ip=ip), 1, "node", api_cycle.config)
    await usage.check_users_usage(api_cycle.panel, api_cycle.config)
    for warning in api_cycle.warning.warnings.values():
        warning.monitoring_end_time -= 1000
    evidence = LOG_OBSERVATIONS.evidence()
    original = policies.read_user_policy

    async def changed_limit(username, **kwargs):
        if username == "bob":
            await discard_source_observations()
        return await original(username, **kwargs)

    monkeypatch.setattr(policies, "read_user_policy", changed_limit)
    await api_cycle.warning.check_persistent_violations(
        api_cycle.panel,
        {},
        api_cycle.config,
        {},
        sample_is_trustworthy=True,
        known_users={"alice", "bob"},
        refresh_user_evidence=lambda _name: LOG_OBSERVATIONS.is_current(evidence),
    )
    assert set(api_cycle.warning.warnings) == {"alice", "bob"}


async def test_lower_capacity_bounds_existing_quiet_node_cache_immediately(logs):
    from utils.log_observations import LOG_OBSERVATIONS

    await parse_logs(record(), 1, "node", logs.config)
    await parse_logs(record(port=54321), 1, "node", logs.config)
    await pop_active_users_snapshot(dict(logs.config, log_replay_cache_size=1))
    assert len(LOG_OBSERVATIONS.seen[1]) <= 1


@pytest.mark.parametrize(
    "stamp, zone",
    [("9999/12/31 23:59:59.999999", "America/New_York"), ("0001/01/01 00:00:00.000001", "Asia/Tehran")],
)
async def test_out_of_range_timestamp_preserves_warning_history(api_cycle, logs, stamp, zone):
    import utils.check_usage as usage
    from utils.log_observations import LOG_OBSERVATIONS

    api_cycle.config.update(logs.config)
    for ip in ("5.6.7.8", "9.9.9.9"):
        await parse_logs(record(ip=ip), 1, "node", api_cycle.config)
    await usage.check_users_usage(api_cycle.panel, api_cycle.config)
    warning = api_cycle.warning.warnings["alice"]
    warning.monitoring_end_time -= 1000
    config = dict(api_cycle.config, log_node_timezones={"1": zone})
    await parse_logs("", 1, "node", config)
    logs.clock[0] += 60
    ready_fleet(1)
    assert not LOG_OBSERVATIONS.evidence().unavailable_reason
    await parse_logs(record(stamp), 1, "node", config)
    assert LOG_OBSERVATIONS.evidence().unavailable_reason
    await usage.check_users_usage(api_cycle.panel, config)
    assert api_cycle.warning.warnings["alice"] is warning


async def test_heartbeat_recovery_without_outage_poll_requires_complete_interval(logs):
    from utils.log_observations import LOG_OBSERVATIONS
    from utils.shared_state import note_node_event

    before_gap = LOG_OBSERVATIONS.evidence()
    assert LOG_OBSERVATIONS.is_current(before_gap)
    # No readiness query observes the gap. The arriving heartbeat must detect it.
    logs.clock[0] = EPOCH + 241
    ready_fleet(1)
    assert LOG_OBSERVATIONS.evidence().unavailable_reason
    assert not LOG_OBSERVATIONS.is_current(before_gap)
    for offset in (256, 271, 286, 300):
        logs.clock[0] = EPOCH + offset
        note_node_event(1)
        assert LOG_OBSERVATIONS.evidence().unavailable_reason
    logs.clock[0] = EPOCH + 301
    note_node_event(1)
    assert not LOG_OBSERVATIONS.evidence().unavailable_reason


async def test_healthy_quiet_heartbeat_does_not_restart_interval(logs):
    from utils.log_observations import LOG_OBSERVATIONS
    from utils.shared_state import note_node_event

    evidence = LOG_OBSERVATIONS.evidence()
    for _ in range(4):
        logs.clock[0] += 15
        note_node_event(1)
        assert LOG_OBSERVATIONS.is_current(evidence)


@pytest.mark.parametrize("gap", [5, 181])
async def test_real_reconnect_heartbeat_cannot_clear_warning_before_full_interval(api_cycle, logs, monkeypatch, gap):
    import utils.check_usage as usage
    import utils.get_logs as streams
    from utils.log_observations import LOG_OBSERVATIONS
    from utils.shared_state import note_node_event
    from utils.types import NodeType

    api_cycle.config.update(logs.config)
    for ip in ("5.6.7.8", "9.9.9.9"):
        await parse_logs(record(ip=ip), 1, "node", api_cycle.config)
    await usage.check_users_usage(api_cycle.panel, api_cycle.config)
    warning = api_cycle.warning.warnings["alice"]
    # An unconsumed observation and its fingerprint must survive the reconnect.
    await parse_logs(record("2026/10/23 10:00:50.000000", user="bob"), 1, "node", api_cycle.config)
    retained_fingerprints = dict(LOG_OBSERVATIONS.seen[1])
    logs.clock[0] += gap
    ready_fleet(1)
    resumed = logs.clock[0]
    verdicts = []

    class Response:
        status_code = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def aiter_lines(self):
            assert dict(LOG_OBSERVATIONS.seen[1]) == retained_fingerprints
            assert "bob" in ACTIVE_USERS
            yield ": ping"
            await usage.check_users_usage(api_cycle.panel, api_cycle.config)
            verdicts.append(api_cycle.warning.warnings.get("alice") is warning)
            logs.clock[0] = resumed + 59
            note_node_event(1)
            await usage.check_users_usage(api_cycle.panel, api_cycle.config)
            verdicts.append(api_cycle.warning.warnings.get("alice") is warning)
            logs.clock[0] = resumed + 60
            note_node_event(1)
            await usage.check_users_usage(api_cycle.panel, api_cycle.config)
            verdicts.append("alice" not in api_cycle.warning.warnings)
            raise asyncio.CancelledError

    class Client(Response):
        def __init__(self, **kwargs):
            pass

        def stream(self, *args, **kwargs):
            return Response()

    monkeypatch.setattr(streams.httpx, "AsyncClient", Client)
    monkeypatch.setattr(streams, "get_token", AsyncMock(return_value=SimpleNamespace(panel_token="test")))
    monkeypatch.setattr(streams, "_ip_source_is_api", AsyncMock(return_value=False))
    monkeypatch.setattr(streams, "_update_node_status", AsyncMock())
    with pytest.raises(asyncio.CancelledError):
        await streams.get_nodes_logs(api_cycle.panel, NodeType(1, "node", "192.0.2.1", "connected"))
    assert verdicts == [True, True, True]
    assert dict(LOG_OBSERVATIONS.seen[1]) == retained_fingerprints
    assert "waiting for log transport" in LOG_OBSERVATIONS.evidence().unavailable_reason
