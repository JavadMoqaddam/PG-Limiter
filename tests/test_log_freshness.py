"""Shared Last seen bounds source-time Logs observations independently of scans."""

import time
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from utils.log_observations import LOG_OBSERVATIONS
from utils.parse_logs import parse_logs
from utils.shared_state import ACTIVE_USERS, get_active_users_snapshot, note_node_event, pop_active_users_snapshot
from utils.types import ConnectionInfo, NodeType

EPOCH = datetime(2026, 10, 23, 10, tzinfo=timezone.utc).timestamp()


def record(stamp, *, port=12345):
    printed = datetime.fromtimestamp(stamp, timezone.utc).strftime("%Y/%m/%d %H:%M:%S.%f")
    return f"{printed} from 5.6.7.8:{port} accepted tcp:example.com:443 [Vless >> DIRECT] email: 1.alice"


@pytest.fixture
async def log_clock(monkeypatch, operational_topics):
    clock = [EPOCH]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    monkeypatch.setattr("utils.parse_logs.is_ip_accepted", AsyncMock(return_value=True))
    nodes = [NodeType(1, "node", "192.0.2.1", "connected")]

    async def configure(*, cadence=300, freshness=20):
        config = {
            "ip_source": "logs",
            "check_interval": cadence,
            "api_ip_freshness": freshness,
            "log_timezone": "UTC",
        }
        await parse_logs("", 1, "node", config)
        LOG_OBSERVATIONS.register_fleet(nodes)
        note_node_event(1)
        return config

    def advance(seconds):
        clock[0] += seconds
        LOG_OBSERVATIONS.register_fleet(nodes)
        note_node_event(1)

    return SimpleNamespace(clock=clock, configure=configure, advance=advance)


@pytest.mark.parametrize("age,qualifies", [(19.999999, True), (20, False), (21, False)])
async def test_shared_last_seen_rejects_old_access_events_with_long_scan_interval(log_clock, age, qualifies):
    config = await log_clock.configure()
    log_clock.advance(300)
    await parse_logs(record(log_clock.clock[0] - age), 1, "node", config)

    assert ("alice" in ACTIVE_USERS) is qualifies


async def test_access_observations_survive_scans_until_original_time_expires(log_clock):
    config = await log_clock.configure(cadence=60, freshness=180)
    log_clock.advance(180)
    stamp = log_clock.clock[0] - 1
    await parse_logs(record(stamp), 1, "node", config)
    first = await pop_active_users_snapshot(config)
    assert first["alice"].device_info.connections[0].last_seen == stamp

    log_clock.advance(60)
    second = await pop_active_users_snapshot(config)
    assert "alice" in second
    assert second["alice"].device_info.connections[0].last_seen == stamp
    assert second["alice"].device_info.connections[0].connection_count == 1

    log_clock.advance(119)
    assert await pop_active_users_snapshot(config) == {}
    assert ACTIVE_USERS == {}


async def test_long_last_seen_requires_its_complete_observation_horizon(log_clock):
    await log_clock.configure(cadence=60, freshness=180)
    log_clock.advance(60)
    assert not LOG_OBSERVATIONS.evidence().coverage_complete
    log_clock.advance(120)
    assert LOG_OBSERVATIONS.evidence().coverage_complete


async def test_replay_eviction_uncertainty_ends_at_shared_freshness_boundary(log_clock):
    config = await log_clock.configure()
    config["log_replay_cache_size"] = 1
    await parse_logs("", 1, "node", config)
    log_clock.advance(300)
    await parse_logs(record(log_clock.clock[0] - 10), 1, "node", config)
    await parse_logs(record(log_clock.clock[0] - 5, port=54321), 1, "node", config)

    assert not LOG_OBSERVATIONS.evidence().coverage_complete
    log_clock.advance(10)
    assert LOG_OBSERVATIONS.evidence().coverage_complete


async def test_snapshot_carries_shared_freshness_while_transport_uses_scan_interval(log_clock):
    await log_clock.configure()
    log_clock.advance(300)
    evidence = LOG_OBSERVATIONS.evidence()
    assert getattr(evidence, "freshness_seconds", None) == 20
    assert evidence.interval_start == EPOCH + 280
    epoch = LOG_OBSERVATIONS.node_epoch(1)
    log_clock.advance(121)
    assert LOG_OBSERVATIONS.node_epoch(1) == epoch
    assert LOG_OBSERVATIONS.evidence().coverage_complete


async def test_refresh_recounts_expired_connections_without_renewing_source_time(log_clock):
    from utils.shared_state import refresh_log_user

    config = await log_clock.configure()
    log_clock.advance(300)
    stamp = log_clock.clock[0] - 1
    await parse_logs(record(stamp), 1, "node", config)
    snapshot = await pop_active_users_snapshot(config)
    user = snapshot["alice"]

    log_clock.advance(19)
    assert refresh_log_user(user, snapshot.log_evidence)
    assert user.ip == []
    assert user.device_info.connections == []
    assert user.device_info.unique_ips == set()
    assert user.device_info.unique_nodes == set()
    assert user.device_info.inbound_protocols == set()
    assert ACTIVE_USERS["alice"].device_info.connections[0].last_seen == stamp


@pytest.mark.parametrize("invalid", ["future", "nan", "infinite"])
async def test_refresh_protects_untrustworthy_source_timestamps(log_clock, invalid):
    from utils.shared_state import refresh_log_user

    config = await log_clock.configure()
    log_clock.advance(300)
    await parse_logs(record(log_clock.clock[0] - 1), 1, "node", config)
    snapshot = await pop_active_users_snapshot(config)
    user = snapshot["alice"]
    user.device_info.connections[0].last_seen = {
        "future": log_clock.clock[0] + 1,
        "nan": float("nan"),
        "infinite": float("inf"),
    }[invalid]

    assert not refresh_log_user(user, snapshot.log_evidence)
    assert user.ip == ["5.6.7.8"]


async def test_refresh_rejects_evidence_retired_by_its_witness_node(log_clock):
    from utils.shared_state import refresh_log_user

    config = await log_clock.configure()
    log_clock.advance(300)
    await parse_logs(record(log_clock.clock[0] - 1), 1, "node", config)
    snapshot = await pop_active_users_snapshot(config)
    LOG_OBSERVATIONS.mark_uncertain(node_id=1, reason="invalid_timestamp")

    assert not refresh_log_user(snapshot["alice"], snapshot.log_evidence)


async def test_refresh_keeps_only_the_frozen_snapshot_node_scope(log_clock):
    from utils.shared_state import refresh_log_user

    config = await log_clock.configure()
    nodes = [NodeType(i, "node", "192.0.2.1", "connected") for i in (1, 2)]
    LOG_OBSERVATIONS.register_fleet(nodes)
    note_node_event(2)
    log_clock.clock[0] += 300
    LOG_OBSERVATIONS.register_fleet(nodes)
    note_node_event(1)
    note_node_event(2)
    await parse_logs(record(log_clock.clock[0] - 1), 1, "node", config)
    nodes[1].status = "error"
    LOG_OBSERVATIONS.register_fleet(nodes)
    snapshot = await pop_active_users_snapshot(config)
    user = snapshot["alice"]
    user.device_info.connections.append(ConnectionInfo("9.9.9.9", 2, "node", "Vmess", log_clock.clock[0] - 1))
    nodes[1].status = "connected"
    LOG_OBSERVATIONS.register_fleet(nodes)
    note_node_event(2)

    assert refresh_log_user(user, snapshot.log_evidence)
    assert user.ip == ["5.6.7.8"]
    assert user.device_info.unique_nodes == {1}
    assert user.device_info.inbound_protocols == {"Vless"}


async def test_reporting_snapshot_filters_expired_rolling_observations(log_clock):
    config = await log_clock.configure()
    log_clock.advance(300)
    await parse_logs(record(log_clock.clock[0] - 1), 1, "node", config)
    log_clock.advance(19)

    assert await get_active_users_snapshot() == {}


async def test_editing_scan_interval_does_not_widen_shared_last_seen(log_clock):
    config = await log_clock.configure(cadence=300, freshness=20)
    log_clock.advance(300)
    config["check_interval"] = 120
    await parse_logs("", 1, "node", config)
    log_clock.advance(120)
    await parse_logs(record(log_clock.clock[0] - 21), 1, "node", config)

    assert ACTIVE_USERS == {}
    assert LOG_OBSERVATIONS.evidence().coverage_complete


@pytest.mark.parametrize("invalid", ["future", "nan", "positive_infinity", "negative_infinity"])
@pytest.mark.parametrize("snapshot_kind", ["pop", "report"])
async def test_invalid_live_timestamp_cannot_be_pruned_into_complete_empty_evidence(log_clock, invalid, snapshot_kind):
    config = await log_clock.configure()
    log_clock.advance(600)
    await parse_logs(record(log_clock.clock[0]), 1, "node", config)
    connection = ACTIVE_USERS["alice"].device_info.connections[0]
    if invalid == "future":
        # No heartbeat has yet detected this backwards step. Readiness floors
        # are mature, so the stored source time is the only uncertainty signal.
        log_clock.clock[0] -= 1
    else:
        connection.last_seen = {
            "nan": float("nan"),
            "positive_infinity": float("inf"),
            "negative_infinity": float("-inf"),
        }[invalid]

    snapshot = await pop_active_users_snapshot(config) if snapshot_kind == "pop" else await get_active_users_snapshot()

    assert not snapshot.log_evidence.coverage_complete
    assert not LOG_OBSERVATIONS.is_current(snapshot.log_evidence)
    expected_reason = "future_timestamp" if invalid == "future" else "invalid_timestamp"
    assert LOG_OBSERVATIONS.uncertainty_diagnostics()["reason_counts"] == {expected_reason: 1}


async def test_clock_step_during_parser_preparation_preserves_uncertainty(log_clock, monkeypatch):
    config = await log_clock.configure()
    log_clock.advance(600)
    stamp = log_clock.clock[0]

    async def step_clock_back(*args):
        log_clock.clock[0] -= 1
        return True

    monkeypatch.setattr("utils.parse_logs.is_ip_accepted", step_clock_back)
    await parse_logs(record(stamp), 1, "node", config)
    snapshot = await pop_active_users_snapshot(config)

    assert snapshot == {}
    assert not snapshot.log_evidence.coverage_complete
    assert LOG_OBSERVATIONS.uncertainty_diagnostics()["reason_counts"] == {"future_timestamp": 1}


async def test_discarded_future_source_time_blocks_absence_until_its_original_expiry(log_clock):
    config = await log_clock.configure()
    log_clock.advance(600)
    stamp = log_clock.clock[0]
    await parse_logs(record(stamp), 1, "node", config)
    log_clock.clock[0] -= 1
    await pop_active_users_snapshot(config)

    log_clock.advance(20)
    assert not LOG_OBSERVATIONS.evidence().coverage_complete
    log_clock.advance(1)
    assert LOG_OBSERVATIONS.evidence().coverage_complete
