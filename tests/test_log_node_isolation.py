"""A failed included node cannot contaminate independent completed Logs evidence."""

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from test_log_history_replay import EPOCH, record
from utils.log_observations import LOG_OBSERVATIONS
from utils.parse_logs import parse_logs
from utils.shared_state import (
    ACTIVE_USERS,
    forget_node_event,
    note_node_event,
    pop_active_users_snapshot,
    retain_log_interval,
)
from utils.types import NodeType


@pytest.fixture
async def fleet(monkeypatch, operational_topics):
    clock = [EPOCH]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    monkeypatch.setattr("utils.parse_logs.is_ip_accepted", AsyncMock(return_value=True))
    config = {"ip_source": "logs", "check_interval": 60, "log_timezone": "UTC"}
    await parse_logs("", 1, "node", config)
    nodes = [NodeType(i, f"node {i}", "192.0.2.1", "connected") for i in (1, 2, 28)]
    LOG_OBSERVATIONS.register_fleet(nodes)
    for node in nodes:
        note_node_event(node.node_id)
    clock[0] += 60
    return SimpleNamespace(clock=clock, nodes=nodes, config=config)


def disconnect(fleet, node_id=28):
    next(node for node in fleet.nodes if node.node_id == node_id).status = "error"
    LOG_OBSERVATIONS.register_fleet(fleet.nodes)


async def test_unavailable_node_leaves_healthy_completed_evidence_usable(fleet):
    await parse_logs(record(), 1, "healthy", fleet.config)
    await parse_logs(record(), 2, "healthy", fleet.config)
    await parse_logs(record(), 28, "failing", fleet.config)
    disconnect(fleet)
    batch = await pop_active_users_snapshot(fleet.config)
    assert {c.node_id for c in batch["alice"].device_info.connections} == {1, 2}
    assert batch.log_evidence.required_node_ids == frozenset({1, 2, 28})
    assert batch.log_evidence.eligible_node_ids == frozenset({1, 2})
    assert not batch.log_evidence.coverage_complete
    assert not LOG_OBSERVATIONS.is_current(batch.log_evidence)
    assert LOG_OBSERVATIONS.is_current(batch.log_evidence, allow_partial=True)


@pytest.mark.parametrize("fault", ["timestamp", "transport", "silence", "timezone"])
async def test_node_faults_only_remove_the_affected_node(fleet, fault):
    if fault == "timestamp":
        LOG_OBSERVATIONS.mark_uncertain(node_id=28, reason="invalid_timestamp")
    elif fault == "transport":
        forget_node_event(28)
    elif fault == "silence":
        for offset in (60, 60, 61):
            fleet.clock[0] += offset
            LOG_OBSERVATIONS.register_fleet(fleet.nodes)
            for node_id in (1, 2):
                note_node_event(node_id)
    else:
        LOG_OBSERVATIONS.zones["28"] = None
    evidence = LOG_OBSERVATIONS.evidence()
    assert evidence.eligible_node_ids == frozenset({1, 2})
    assert LOG_OBSERVATIONS.is_current(evidence, allow_partial=True)


async def test_recovery_cannot_enter_an_old_partial_snapshot(fleet):
    disconnect(fleet)
    await parse_logs(record(), 1, "healthy", fleet.config)
    batch = await pop_active_users_snapshot(fleet.config)
    fleet.nodes[-1].status = "connected"
    LOG_OBSERVATIONS.register_fleet(fleet.nodes)
    note_node_event(28)
    assert LOG_OBSERVATIONS.evidence().eligible_node_ids == frozenset({1, 2})
    assert LOG_OBSERVATIONS.is_current(batch.log_evidence, allow_partial=True)
    fleet.clock[0] += 60
    for node_id in (1, 2, 28):
        note_node_event(node_id)
    assert LOG_OBSERVATIONS.evidence().coverage_complete
    assert batch.log_evidence.eligible_node_ids == frozenset({1, 2})
    # Even a mapping receiving a recovered-node record retains its captured scope.
    await parse_logs(record("2026/10/23 10:01:40.000000"), 28, "recovered", fleet.config)
    batch["bob"] = ACTIVE_USERS["alice"]
    retain_log_interval(batch, EPOCH, EPOCH + 120)
    assert "bob" not in batch


async def test_included_witness_fault_retires_positive_evidence(fleet):
    disconnect(fleet)
    evidence = LOG_OBSERVATIONS.evidence()
    LOG_OBSERVATIONS.mark_uncertain(node_id=28)
    assert LOG_OBSERVATIONS.is_current(evidence, allow_partial=True)
    LOG_OBSERVATIONS.mark_uncertain(node_id=1)
    assert not LOG_OBSERVATIONS.is_current(evidence, allow_partial=True)


async def test_fleet_addition_cannot_leave_old_evidence_claiming_complete_coverage(fleet):
    evidence = LOG_OBSERVATIONS.evidence()
    assert evidence.coverage_complete
    fleet.nodes.append(NodeType(29, "new", "192.0.2.1", "connected"))
    LOG_OBSERVATIONS.register_fleet(fleet.nodes)
    note_node_event(29)
    assert not LOG_OBSERVATIONS.is_current(evidence, allow_partial=True)


@pytest.mark.parametrize("node_id", [True, 1.0, "1"])
async def test_malformed_stream_identity_cannot_alias_a_healthy_node(fleet, node_id):
    evidence = LOG_OBSERVATIONS.evidence()
    await parse_logs(record("bad timestamp"), node_id, "malformed", fleet.config)
    note_node_event(node_id)
    forget_node_event(node_id)
    assert not LOG_OBSERVATIONS.uncertainty_diagnostics()["reason_counts"]
    assert LOG_OBSERVATIONS.is_current(evidence)


async def test_diagnostic_node_bound_does_not_turn_owned_faults_into_global_faults(fleet):
    nodes = [NodeType(i, f"node {i}", "192.0.2.1", "connected") for i in range(1, 133)]
    LOG_OBSERVATIONS.register_fleet(nodes)
    for node in nodes:
        note_node_event(node.node_id)
    fleet.clock[0] += 60
    for node in nodes:
        note_node_event(node.node_id)
    for node_id in range(3, 133):
        LOG_OBSERVATIONS.mark_uncertain(node_id=node_id, reason="invalid_timestamp")
    evidence = LOG_OBSERVATIONS.evidence()
    assert evidence.eligible_node_ids == frozenset({1, 2})
    assert LOG_OBSERVATIONS.is_current(evidence, allow_partial=True)


@pytest.mark.parametrize("change", ["unknown_fleet", "stale_fleet", "source", "global_uncertainty"])
async def test_global_evidence_faults_block_partial_actions(fleet, change):
    disconnect(fleet)
    evidence = LOG_OBSERVATIONS.evidence()
    if change == "unknown_fleet":
        LOG_OBSERVATIONS.register_fleet(None)
    elif change == "stale_fleet":
        fleet.clock[0] += 181
    elif change == "source":
        LOG_OBSERVATIONS.invalidate()
    else:
        LOG_OBSERVATIONS.mark_uncertain(node_id=None)
    assert not LOG_OBSERVATIONS.is_current(evidence, allow_partial=True)


@pytest.mark.parametrize("own_node", [False, True])
async def test_prepared_parser_survives_only_unrelated_node_status_round_trip(fleet, monkeypatch, own_node):
    waiting, release = asyncio.Event(), asyncio.Event()

    async def prepare(*args):
        waiting.set()
        await release.wait()
        return True

    monkeypatch.setattr("utils.parse_logs.is_ip_accepted", prepare)
    parsing = asyncio.create_task(parse_logs(record(), 1, "healthy", fleet.config))
    await waiting.wait()
    generation = LOG_OBSERVATIONS.generation
    node_id = 1 if own_node else 28
    disconnect(fleet, node_id)
    next(node for node in fleet.nodes if node.node_id == node_id).status = "connected"
    LOG_OBSERVATIONS.register_fleet(fleet.nodes)
    release.set()
    await parsing
    assert ("alice" in ACTIVE_USERS) is (not own_node)
    assert LOG_OBSERVATIONS.generation == generation
