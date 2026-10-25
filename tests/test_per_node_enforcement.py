"""Warnings, punishment, normalization and reports share factual node counts."""

import json
import time

import pytest
from sqlalchemy import select

import utils.check_usage as usage
from db.models import User
from test_config_revisions import settings_db  # noqa: F401
from test_inbound_mode_enforcement import enforcement_cycle, existing_warning  # noqa: F401
from utils.log_observations import LOG_OBSERVATIONS
from utils.read_config import get_config_revision, read_config, save_config_value
from utils.shared_state import (
    ACTIVE_USERS,
    APISampleEvidence,
    begin_api_collection,
    note_node_event,
    publish_api_snapshot,
)
from utils.types import ConnectionInfo, NodeType, PanelType, UserType

PANEL = PanelType("admin", "secret", "panel.local")


@pytest.fixture
async def node_cycle(enforcement_cycle, completed_logs_evidence):
    system, get_db, messages = enforcement_cycle
    assert await save_config_value("device_count_mode", "node")
    config = await read_config()
    config.update(cdn_nodes=[0, 2], cdn_inbounds=["Vless"], high_trust_ip_grouping=True, subnet_ip_grouping=True)
    completed_logs_evidence(config)
    LOG_OBSERVATIONS.register_fleet(
        [NodeType(0, "core", "192.0.2.1", "connected"), NodeType(2, "node-2", "192.0.2.2", "connected")]
    )
    for node_id in (0, 2):
        LOG_OBSERVATIONS.zones[str(node_id)] = LOG_OBSERVATIONS._zone("UTC")
        note_node_event(node_id)
    LOG_OBSERVATIONS.fleet_floor = LOG_OBSERVATIONS.floor
    for node_id in LOG_OBSERVATIONS.required_nodes:
        LOG_OBSERVATIONS.node_states[node_id].floor = LOG_OBSERVATIONS.floor
    LOG_OBSERVATIONS.transport_floor = LOG_OBSERVATIONS.floor
    return system, get_db, messages, config


def observed_user(connections):
    user = UserType(name="alice", ip=sorted({ip for ip, _inbound, _node in connections}))
    user.device_info.connections = [
        ConnectionInfo(ip, node, f"node-{node}", inbound, time.time()) for ip, inbound, node in connections
    ]
    return user


async def publish_user(config, user, source, *, eligible=frozenset({0, 2}), unknown=frozenset()):
    if source == "api":
        assert await save_config_value("ip_source", "api")
        await read_config()
        config.update(ip_source="api", _revision=get_config_revision())
        evidence = APISampleEvidence(begin_api_collection(), get_config_revision(), 60, unknown, eligible)
        assert await publish_api_snapshot({"alice": user}, evidence)
    else:
        ACTIVE_USERS["alice"] = user


@pytest.mark.parametrize("source", ["logs", "api"])
async def test_two_nodes_create_warning_even_for_one_shared_ip(node_cycle, source, caplog):
    system, _get_db, _messages, config = node_cycle
    await publish_user(config, observed_user([("5.6.7.8", "Vless", 0), ("5.6.7.8", "Vless", 2)]), source)
    await usage.check_users_usage(PANEL, config_data=config)
    assert system.warnings["alice"].ip_count == 2
    assert system.warnings["alice"].consecutive_violations == 1
    assert system.warnings["alice"].ips == {"5.6.7.8"}
    assert "Report scope (group/admin filters applied): 1 users, 2 devices" in caplog.text


@pytest.mark.parametrize("source", ["logs", "api"])
async def test_persistent_node_violation_bans_using_node_count(node_cycle, source):
    system, get_db, messages, config = node_cycle
    warning = existing_warning()
    warning.consecutive_violations = 3
    warning.monitoring_end_time = time.time() - 1
    system.warnings["alice"] = warning
    await publish_user(config, observed_user([("5.6.7.8", "Vless", 0), ("5.6.7.8", "Vless", 2)]), source)
    await usage.check_users_usage(PANEL, config_data=config)
    async with get_db() as session:
        stored = await session.scalar(select(User).where(User.username == "alice"))
        assert stored.is_disabled_by_limiter is True
    assert "alice" not in system.warnings
    assert any("Active Devices: <code>2</code> (1 IPs)" in message for message in messages)


@pytest.mark.parametrize("source", ["logs", "api"])
async def test_mode_change_to_one_node_normalizes_multi_ip_warning(node_cycle, source, caplog):
    system, _get_db, _messages, config = node_cycle
    system.warnings["alice"] = existing_warning()
    user = observed_user([("5.6.7.8", "Vless", 0), ("9.9.9.9", "Vmess", 0), ("4.4.4.4", "Vless", 0)])
    await publish_user(config, user, source)
    await usage.check_users_usage(PANEL, config_data=config)
    assert "alice" not in system.warnings
    assert "Report scope (group/admin filters applied): 1 users, 1 devices" in caplog.text


async def test_missing_api_node_authority_preserves_warning(node_cycle):
    system, _get_db, _messages, config = node_cycle
    warning = existing_warning()
    system.warnings["alice"] = warning
    await publish_user(config, observed_user([("5.6.7.8", "API", 0)]), "api", eligible=None)
    await usage.check_users_usage(PANEL, config_data=config)
    assert system.warnings.get("alice") is warning
    assert warning.consecutive_violations == 2


@pytest.mark.parametrize("fault", ["unknown", "disconnected", "silent"])
async def test_unready_logs_preserve_warning(node_cycle, fault):
    system, _get_db, _messages, config = node_cycle
    warning = existing_warning()
    system.warnings["alice"] = warning
    ACTIVE_USERS["alice"] = observed_user([("5.6.7.8", "Vless", 0)])
    if fault == "unknown":
        LOG_OBSERVATIONS.register_fleet(None)
    elif fault == "disconnected":
        LOG_OBSERVATIONS.register_fleet([NodeType(0, "core", "192.0.2.1", "connecting")])
    else:
        from utils.shared_state import NODE_LAST_EVENT

        NODE_LAST_EVENT[0] = time.time() - 1000
    await usage.check_users_usage(PANEL, config_data=config)
    assert system.warnings.get("alice") is warning
    assert warning.consecutive_violations == 2


async def test_api_unknown_user_preserves_warning(node_cycle):
    system, _get_db, _messages, config = node_cycle
    warning = existing_warning()
    system.warnings["alice"] = warning
    await publish_user(config, observed_user([("5.6.7.8", "API", 0)]), "api", unknown=frozenset({"alice"}))
    await usage.check_users_usage(PANEL, config_data=config)
    assert system.warnings.get("alice") is warning
    assert warning.consecutive_violations == 2


async def test_api_future_observation_preserves_warning_during_reporting(node_cycle):
    system, _get_db, _messages, config = node_cycle
    warning = existing_warning()
    system.warnings["alice"] = warning
    user = observed_user([("5.6.7.8", "API", 0)])
    user.device_info.connections[0].last_seen = time.time() + 100
    await publish_user(config, user, "api")
    await usage.check_users_usage(PANEL, config_data=config)
    assert system.warnings.get("alice") is warning
    assert warning.consecutive_violations == 2


async def test_api_expired_node_observations_normalize_using_fresh_nodes_only(node_cycle, caplog):
    system, _get_db, _messages, config = node_cycle
    system.warnings["alice"] = existing_warning()
    user = observed_user([("5.6.7.8", "API", 0), ("5.6.7.8", "API", 2)])
    user.device_info.connections[0].last_seen = time.time() - 100
    await publish_user(config, user, "api")
    await usage.check_users_usage(PANEL, config_data=config)
    assert "alice" not in system.warnings
    assert "Report scope (group/admin filters applied): 1 users, 1 devices" in caplog.text


@pytest.mark.parametrize("bad_id", [False, 0.0])
@pytest.mark.parametrize("bad_first", [False, True])
@pytest.mark.parametrize("bad_status", ["connected", "disabled"])
@pytest.mark.parametrize("expired", [False, True])
async def test_raw_fleet_alias_preserves_normal_and_expired_warning(node_cycle, bad_id, bad_first, bad_status, expired):
    system, get_db, _messages, config = node_cycle
    warning = existing_warning()
    if expired:
        warning.consecutive_violations = 3
        warning.monitoring_end_time = time.time() - 1
    system.warnings["alice"] = warning
    await system.save_warnings()
    with open(system.filename, encoding="utf-8") as file:
        saved_warning = json.load(file)["alice"]
    valid = NodeType(0, "valid-core", "192.0.2.1", "connected")
    malformed = NodeType(bad_id, "malformed-core", "192.0.2.9", bad_status)
    raw_fleet = [malformed, valid] if bad_first else [valid, malformed]
    LOG_OBSERVATIONS.register_fleet([*raw_fleet, NodeType(2, "valid-node", "192.0.2.2", "connected")])
    # Model a complete interval and healthy surviving transport, so preservation
    # depends on unknown raw fleet authority rather than incidental warm-up.
    for node_id in (0, 2):
        note_node_event(node_id)
    LOG_OBSERVATIONS.fleet_floor = LOG_OBSERVATIONS.floor
    LOG_OBSERVATIONS.transport_floor = LOG_OBSERVATIONS.floor
    ACTIVE_USERS["alice"] = observed_user([("5.6.7.8", "Vless", 0), ("5.6.7.8", "Vless", 2)])
    await usage.check_users_usage(PANEL, config_data=config)
    assert system.warnings.get("alice") is warning
    with open(system.filename, encoding="utf-8") as file:
        assert json.load(file)["alice"] == saved_warning
    async with get_db() as session:
        stored = await session.scalar(select(User).where(User.username == "alice"))
        assert stored.is_disabled_by_limiter is False
