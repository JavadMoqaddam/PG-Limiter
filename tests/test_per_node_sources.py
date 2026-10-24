"""Source snapshots carry eligible node authority without inventing provenance."""

import time
from unittest.mock import AsyncMock

import pytest

from test_ip_source_api import _patch_collector, api_config, clean_collector_state, panel  # noqa: F401
from utils.device_count import DeviceCountingConfig, count_devices
from utils.ip_source_api import _build_users_from_payloads, _resolve_node_context, collect_active_users_from_api
from utils.log_observations import LOG_OBSERVATIONS
from utils.panel_api.online_ips import _parse_ip_payload
from utils.shared_state import get_active_users_snapshot, note_node_event, pop_api_users_snapshot
from utils.types import NodeType


@pytest.mark.parametrize("node_key", [True, False, 1.5, 1.0])
def test_api_parser_does_not_coerce_malformed_node_keys(node_key):
    with pytest.raises(ValueError, match="node ID"):
        _parse_ip_payload({"nodes": {node_key: {"ips": {"5.6.7.8": int(time.time())}}}})


def test_api_parser_preserves_zero_node_key():
    assert _parse_ip_payload({"nodes": {"0": {"ips": {}}}}) == {0: {}}


@pytest.mark.parametrize("status", ["disabled", "limited"])
async def test_node_context_excludes_panel_disabled_nodes_and_preserves_zero(monkeypatch, panel, status):
    nodes = [NodeType(0, "core", "192.0.2.1", "connected"), NodeType(9, "excluded", "192.0.2.9", status)]
    monkeypatch.setattr("utils.panel_api.nodes.get_nodes", AsyncMock(return_value=nodes))
    _names, disabled, eligible = await _resolve_node_context(panel, {"device_count_mode": "node"})
    assert eligible == {0}
    assert 9 in disabled


@pytest.mark.parametrize("node_id", [True, 1.0, "1"])
async def test_malformed_authoritative_node_list_is_unknown(monkeypatch, panel, node_id):
    monkeypatch.setattr(
        "utils.panel_api.nodes.get_nodes", AsyncMock(return_value=[NodeType(node_id, "bad", "192.0.2.1", "connected")])
    )
    with pytest.raises(ValueError, match="node"):
        await _resolve_node_context(panel, {"device_count_mode": "node"})


async def test_api_node_snapshot_counts_known_nodes_even_with_the_same_ip(monkeypatch, panel, api_config):
    api_config.update(device_count_mode="node", cdn_nodes=[0, 2], subnet_ip_grouping=True)
    stamp = int(time.time())
    _patch_collector(
        monkeypatch,
        candidates=[{"id": 7, "username": "alice"}],
        payloads={"alice": {0: {"5.6.7.8": stamp}, 2: {"5.6.7.8": stamp}, 99: {"9.9.9.9": stamp}}},
        node_name_map={0: "core", 2: "node-2"},
    )
    assert await collect_active_users_from_api(panel, api_config)
    snapshot = await pop_api_users_snapshot()
    assert snapshot.api_evidence.eligible_node_ids == frozenset({0, 2})
    user = snapshot["alice"]
    assert {c.node_id for c in user.device_info.connections} == {0, 2}
    config = DeviceCountingConfig.from_config(api_config, eligible_node_ids=snapshot.api_evidence.eligible_node_ids)
    assert count_devices(user, config, trust_score=100) == 2


async def test_malformed_connection_key_cannot_supply_a_missing_expected_node(api_config):
    api_config["device_count_mode"] = "node"
    users, _stats = await _build_users_from_payloads(
        {"alice": {True: {"5.6.7.8": int(time.time())}}}, {}, {1: "node-1"}, set(), api_config, expected_node_ids={1}
    )
    assert "alice" in users.unknown_users
    assert "alice" not in users


async def test_logs_snapshot_captures_known_connected_monitored_nodes(completed_logs_evidence):
    completed_logs_evidence({"check_interval": 60, "disabled_nodes": [9]})
    LOG_OBSERVATIONS.register_fleet(
        [NodeType(0, "core", "192.0.2.1", "connected"), NodeType(9, "excluded", "192.0.2.9", "connected")]
    )
    note_node_event(0)
    snapshot = await get_active_users_snapshot()
    assert snapshot.log_evidence.eligible_node_ids == frozenset({0})
    LOG_OBSERVATIONS.register_fleet(None)
    snapshot = await get_active_users_snapshot()
    assert snapshot.log_evidence.eligible_node_ids is None


async def test_malformed_logs_fleet_cannot_authorize_integer_connections(completed_logs_evidence):
    completed_logs_evidence()
    LOG_OBSERVATIONS.register_fleet([NodeType(True, "bad", "192.0.2.1", "connected")])
    snapshot = await get_active_users_snapshot()
    assert snapshot.log_evidence.eligible_node_ids is None


@pytest.mark.parametrize("bad_id", [False, 0.0, True, 2.0])
@pytest.mark.parametrize("bad_first", [False, True])
def test_raw_fleet_alias_cannot_become_integer_node_authority(completed_logs_evidence, bad_id, bad_first):
    completed_logs_evidence()
    valid = NodeType(int(bad_id), "valid", "192.0.2.1", "connected")
    malformed = NodeType(bad_id, "malformed", "192.0.2.2", "connected")
    LOG_OBSERVATIONS.register_fleet([malformed, valid] if bad_first else [valid, malformed])
    assert LOG_OBSERVATIONS.evidence().eligible_node_ids is None


def test_raw_alias_fault_retires_prior_node_evidence_after_fleet_recovery(completed_logs_evidence):
    original = completed_logs_evidence()
    assert LOG_OBSERVATIONS.is_current(original)
    valid = NodeType(1, "valid", "192.0.2.1", "connected")
    LOG_OBSERVATIONS.register_fleet([valid, NodeType(True, "malformed", "192.0.2.2", "connected")])
    assert LOG_OBSERVATIONS.evidence().eligible_node_ids is None
    assert not LOG_OBSERVATIONS.is_current(original)
    LOG_OBSERVATIONS.register_fleet([valid])
    assert LOG_OBSERVATIONS.evidence().eligible_node_ids == frozenset({1})
    assert not LOG_OBSERVATIONS.is_current(original)


@pytest.mark.parametrize("last_status, eligible", [("connected", frozenset({0})), ("disabled", frozenset())])
def test_repeated_valid_zero_id_retains_last_entry_fleet_semantics(completed_logs_evidence, last_status, eligible):
    completed_logs_evidence()
    LOG_OBSERVATIONS.register_fleet(
        [NodeType(0, "first", "192.0.2.1", "connected"), NodeType(0, "last", "192.0.2.2", last_status)]
    )
    assert LOG_OBSERVATIONS.evidence().eligible_node_ids == eligible
