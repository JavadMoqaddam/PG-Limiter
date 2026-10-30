"""General health notices reflect evidence transitions without exposing access data."""

import importlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from telegram_bot.dispatcher import Priority
from telegram_bot.topics import TopicType
from utils.types import NodeType


@pytest.fixture
def health(monkeypatch):
    module = importlib.import_module("utils.node_health_notifications")
    admission = SimpleNamespace(allowed=True, manager_id=1, revision=0, group_id=-100)
    messages = []

    async def send(message, **kwargs):
        messages.append((message, kwargs))

    monkeypatch.setattr(module, "send_logs", send)
    monkeypatch.setattr(module, "monitoring_is_current", lambda value: value is admission and admission.allowed)
    monkeypatch.setattr(module, "get_config_revision", lambda: 0)
    return SimpleNamespace(
        module=module, reporter=module.NodeHealthNotifications(), admission=admission, messages=messages
    )


def node_view(reasons, *, source="logs", generation=1, required=None, eligible=None, global_reason=""):
    """Supply the same immutable readiness boundary used by the streaming hooks."""
    required = frozenset(reasons) if required is None else required
    eligible = frozenset(node for node, reason in reasons.items() if not reason) if eligible is None else eligible
    evidence = SimpleNamespace(
        generation=generation,
        config_revision=0,
        interval_start=1000.0,
        interval_end=1060.0,
        required_node_ids=required,
        eligible_node_ids=eligible,
        coverage_complete=required is not None and required == eligible,
        unavailable_reason=global_reason,
    )
    observations = SimpleNamespace(
        source=source,
        generation=generation,
        evidence=lambda: evidence,
        node_unavailable_reason=lambda node_id, **_kwargs: reasons[node_id],
        node_interval_floor=lambda _node_id: None,
    )
    nodes = [NodeType(node_id, f"node {node_id}", "192.0.2.1", "connected") for node_id in reasons]
    return observations, evidence, nodes


async def observe(health, reasons, **kwargs):
    observations, evidence, nodes = node_view(reasons, **kwargs)
    await health.reporter.report_nodes(observations, health.admission, nodes=nodes, evidence=evidence)


@pytest.mark.asyncio
async def test_included_disconnect_and_fresh_interval_recovery_are_distinct(health):
    await observe(health, {1: "", 28: ""})
    assert health.messages == []  # Initial healthy fleet already has its status summary.
    await observe(health, {1: "", 28: "panel connection unavailable"})
    await observe(health, {1: "", 28: "panel connection unavailable"})
    assert len(health.messages) == 1
    assert "28" in health.messages[0][0] and "panel connection unavailable" in health.messages[0][0]
    await observe(health, {1: "", 28: "collecting a complete check interval"})
    assert len(health.messages) == 2
    assert "fresh interval" in health.messages[-1][0]
    await observe(health, {1: "", 28: ""})
    assert len(health.messages) == 3
    assert "ready" in health.messages[-1][0].lower()
    assert all(options["topic_type"] == TopicType.GENERAL for _, options in health.messages)
    assert all(options["priority"] == Priority.SYSTEM for _, options in health.messages)
    assert all(not options.get("return_message_id") for _, options in health.messages)


@pytest.mark.asyncio
async def test_transport_silence_and_replay_uncertainty_notify_once_per_reason(health):
    await observe(health, {1: ""})
    await observe(health, {1: "waiting for log transport"})
    await observe(health, {1: "waiting for log transport"})
    await observe(health, {1: "timestamp or replay uncertainty in the current interval"})
    await observe(health, {1: "timestamp or replay uncertainty in the current interval"})
    assert len(health.messages) == 2
    assert "log transport" in health.messages[0][0]
    assert "timestamp or replay uncertainty" in health.messages[1][0]


@pytest.mark.asyncio
async def test_initial_included_failure_is_reported_even_without_a_stream_task(health):
    await observe(health, {28: "panel connection unavailable"})
    assert len(health.messages) == 1 and "28" in health.messages[0][0]


@pytest.mark.asyncio
async def test_excluded_and_panel_disabled_nodes_cannot_generate_incidents(health):
    await observe(health, {1: "", 9: ""}, required=frozenset({1}), eligible=frozenset({1}))
    await observe(
        health,
        {1: "", 9: "panel connection unavailable"},
        required=frozenset({1}),
        eligible=frozenset({1}),
    )
    assert health.messages == []


@pytest.mark.asyncio
async def test_pause_and_api_source_closures_do_not_become_sse_incidents(health):
    await observe(health, {1: ""})
    health.admission.allowed = False
    await observe(health, {1: "waiting for log transport"})
    health.admission.allowed = True
    await observe(health, {1: ""}, source="api", generation=2)
    await observe(health, {1: "waiting for log transport"}, source="api", generation=2)
    await observe(health, {1: ""}, generation=3)
    assert health.messages == []


@pytest.mark.asyncio
async def test_planned_rebuild_suppresses_closure_and_expected_warmup(health):
    await observe(health, {1: ""})
    with health.reporter.planned_refresh({1}):
        await observe(health, {1: "waiting for log transport"})
        await observe(health, {1: "collecting a complete check interval"})
    await observe(health, {1: "collecting a complete check interval"})
    await observe(health, {1: ""})
    assert health.messages == []


@pytest.mark.asyncio
async def test_planned_rebuild_suppresses_only_fresh_interval_cycles_after_context_exit(health):
    await observe(health, {1: "", 28: ""})
    await cycle(health, eligible_node_ids=frozenset({1, 28}))
    with health.reporter.planned_refresh({1, 28}):
        await observe(health, {1: "collecting a complete check interval", 28: "collecting a complete check interval"})
    await cycle(health, "skipped", reason="collecting a complete check interval")
    assert len(health.messages) == 1
    await cycle(health, "skipped", reason="waiting for the node fleet")
    assert len(health.messages) == 2 and "fleet" in health.messages[-1][0].lower()
    await cycle(health, "failed", reason="collecting a complete check interval")
    assert len(health.messages) == 3 and "failed" in health.messages[-1][0].lower()


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["pause", "api", "config"])
async def test_unrelated_scope_warmup_cannot_reuse_planned_refresh_suppression(health, monkeypatch, boundary):
    await observe(health, {1: "", 28: ""})
    with health.reporter.planned_refresh({1, 28}):
        await observe(health, {1: "collecting a complete check interval", 28: "collecting a complete check interval"})
    if boundary == "pause":
        health.admission.allowed = False
        await observe(health, {1: "waiting for log transport", 28: "waiting for log transport"})
        health.admission.allowed = True
    elif boundary == "api":
        await observe(health, {1: "", 28: ""}, source="api", generation=2)
    else:
        monkeypatch.setattr(health.module, "get_config_revision", lambda: 1)
    await cycle(health, "skipped", reason="collecting a complete check interval")
    assert len(health.messages) == 1 and "skipped" in health.messages[0][0].lower()


@pytest.mark.asyncio
async def test_real_rebuild_failure_becomes_visible_after_maintenance(health):
    await observe(health, {1: ""})
    with health.reporter.planned_refresh({1}):
        await observe(health, {1: "waiting for log transport"})
    await observe(health, {1: "waiting for log transport"})
    assert len(health.messages) == 1 and "log transport" in health.messages[0][0]


@pytest.mark.asyncio
async def test_initial_warmup_does_not_emit_recovery_notices_for_every_node(health):
    await observe(health, {1: "collecting a complete check interval", 28: "collecting a complete check interval"})
    await observe(health, {1: "", 28: ""})
    assert health.messages == []


@pytest.mark.asyncio
async def test_scope_change_during_admission_stops_remaining_old_fleet_notices(health, monkeypatch):
    observations, evidence, nodes = node_view({1: "waiting for log transport", 28: "waiting for log transport"})

    async def change_source(text, **kwargs):
        health.messages.append((text, kwargs))
        observations.source = "api"
        observations.generation += 1

    monkeypatch.setattr(health.module, "send_logs", change_source)
    await health.reporter.report_nodes(observations, health.admission, nodes=nodes, evidence=evidence)
    assert len(health.messages) == 1


@pytest.mark.asyncio
async def test_unknown_or_stale_fleet_does_not_fabricate_node_incidents(health):
    await observe(health, {1: ""})
    await observe(health, {1: "waiting for log transport"}, required=None, global_reason="waiting for the node fleet")
    assert health.messages == []


@pytest.mark.asyncio
async def test_names_are_escaped_and_unknown_reasons_do_not_leak(health):
    observations, evidence, nodes = node_view({28: "secret at https://panel.invalid/token=private"})
    nodes[0].node_name = "relay <b>&broken</b>"
    await health.reporter.report_nodes(observations, health.admission, nodes=nodes, evidence=evidence)
    assert len(health.messages) == 1
    text = health.messages[0][0]
    assert "relay &lt;b&gt;&amp;broken&lt;/b&gt;" in text
    assert "secret" not in text and "https://" not in text and "private" not in text


async def cycle(health, outcome="completed", **kwargs):
    await health.reporter.report_cycle(
        health.admission,
        "logs",
        outcome,
        device_count_mode="node",
        required_node_ids=frozenset({1, 28}),
        **kwargs,
    )


@pytest.mark.asyncio
async def test_cycle_reports_first_completion_partial_entry_and_full_recovery(health):
    await cycle(health, eligible_node_ids=frozenset({1, 28}), users_counted=100, duration_seconds=2)
    await cycle(health, eligible_node_ids=frozenset({1, 28}), users_counted=120, duration_seconds=3)
    assert len(health.messages) == 1
    await cycle(
        health,
        coverage_complete=False,
        eligible_node_ids=frozenset({1}),
        users_counted=80,
        protected_users_count=12,
    )
    await cycle(health, coverage_complete=False, eligible_node_ids=frozenset({1}), users_counted=90)
    assert len(health.messages) == 2
    text = health.messages[-1][0]
    assert "partial" in text.lower() and "1/2" in text and "28" in text
    assert "12" in text
    await cycle(health, eligible_node_ids=frozenset({1, 28}))
    assert len(health.messages) == 3
    assert "complete" in health.messages[-1][0].lower()


@pytest.mark.asyncio
async def test_skipped_failed_cycles_use_safe_reasons_and_do_not_claim_completion(health):
    await cycle(health, "skipped", reason="waiting for the node fleet")
    await cycle(health, "skipped", reason="waiting for the node fleet", duration_seconds=4)
    await cycle(health, "failed", reason="private-user at https://panel.invalid/key")
    assert len(health.messages) == 2
    assert "skipped" in health.messages[0][0].lower()
    assert "failed" in health.messages[1][0].lower()
    assert "private-user" not in health.messages[1][0] and "https://" not in health.messages[1][0]


@pytest.mark.asyncio
async def test_notification_admission_failure_isolated_and_retried(health, monkeypatch):
    attempts = []

    async def fail_once(text, **kwargs):
        attempts.append(text)
        if len(attempts) == 1:
            raise RuntimeError("dispatcher unavailable")
        health.messages.append((text, kwargs))

    monkeypatch.setattr(health.module, "send_logs", fail_once)
    await observe(health, {28: "waiting for log transport"})
    await observe(health, {28: "waiting for log transport"})
    assert len(health.messages) == 1


@pytest.mark.asyncio
async def test_old_frozen_evidence_cannot_describe_current_node_recovery(health):
    observations, evidence, nodes = node_view({28: ""})
    observations.generation += 1
    await health.reporter.report_nodes(observations, health.admission, nodes=nodes, evidence=evidence)
    assert health.messages == []


@pytest.mark.asyncio
async def test_global_warmup_cannot_be_reported_as_a_node_failure(health):
    await observe(health, {1: ""}, eligible=frozenset(), global_reason="collecting a complete check interval")
    assert health.messages == []


@pytest.fixture
def actual_nodes(health, monkeypatch):
    from utils.log_observations import LOG_OBSERVATIONS
    import utils.shared_state as shared

    now = [1000.0]
    monkeypatch.setattr("utils.log_observations.time.time", lambda: now[0])
    monkeypatch.setattr("utils.read_config.get_config_revision", lambda: 0)
    monkeypatch.setattr(shared, "NODE_LAST_EVENT", {})
    monkeypatch.setattr(health.module, "NODE_HEALTH_NOTIFICATIONS", health.reporter)
    nodes = [NodeType(node_id, f"relay {node_id}", "192.0.2.1", "connected") for node_id in (1, 28)]
    LOG_OBSERVATIONS.configure({"ip_source": "logs", "check_interval": 60, "log_timezone": "UTC"})
    LOG_OBSERVATIONS.register_fleet(nodes)
    shared.note_node_event(1)
    shared.note_node_event(28)

    def ready():
        now[0] += 61
        LOG_OBSERVATIONS.register_fleet(nodes)
        shared.note_node_event(1)
        shared.note_node_event(28)

    return SimpleNamespace(observations=LOG_OBSERVATIONS, shared=shared, nodes=nodes, now=now, ready=ready)


@pytest.mark.asyncio
async def test_startup_poll_allows_initial_connection_grace(actual_nodes, health):
    actual_nodes.shared.forget_node_event(1)
    actual_nodes.shared.forget_node_event(28)
    await health.reporter.report_nodes(actual_nodes.observations, health.admission, nodes=actual_nodes.nodes)
    assert health.messages == []
    actual_nodes.now[0] += 61
    actual_nodes.observations.register_fleet(actual_nodes.nodes)
    await health.reporter.report_nodes(actual_nodes.observations, health.admission, nodes=actual_nodes.nodes)
    assert len(health.messages) == 2


@pytest.mark.asyncio
async def test_new_included_node_has_its_own_initial_connection_grace(actual_nodes, health):
    actual_nodes.ready()
    await health.reporter.report_nodes(actual_nodes.observations, health.admission, nodes=actual_nodes.nodes)
    actual_nodes.ready()
    actual_nodes.nodes.append(NodeType(173, "new relay", "192.0.2.2", "connected"))
    actual_nodes.observations.register_fleet(actual_nodes.nodes)
    actual_nodes.shared.note_node_event(1)
    actual_nodes.shared.note_node_event(28)
    await health.reporter.report_nodes(actual_nodes.observations, health.admission, nodes=actual_nodes.nodes)
    assert health.messages == []
    actual_nodes.shared.note_node_event(173)
    await health.reporter.report_nodes(actual_nodes.observations, health.admission, nodes=actual_nodes.nodes)
    actual_nodes.now[0] += 61
    actual_nodes.observations.register_fleet(actual_nodes.nodes)
    for node_id in (1, 28, 173):
        actual_nodes.shared.note_node_event(node_id)
    await health.reporter.report_nodes(actual_nodes.observations, health.admission, nodes=actual_nodes.nodes)
    assert health.messages == []


@pytest.mark.asyncio
async def test_new_node_initial_connection_grace_expires_without_heartbeat(actual_nodes, health):
    actual_nodes.ready()
    await health.reporter.report_nodes(actual_nodes.observations, health.admission, nodes=actual_nodes.nodes)
    actual_nodes.ready()
    actual_nodes.nodes.append(NodeType(173, "new relay", "192.0.2.2", "connected"))
    actual_nodes.observations.register_fleet(actual_nodes.nodes)
    for node_id in (1, 28):
        actual_nodes.shared.note_node_event(node_id)
    await health.reporter.report_nodes(actual_nodes.observations, health.admission, nodes=actual_nodes.nodes)
    assert health.messages == []
    actual_nodes.now[0] += 61
    actual_nodes.observations.register_fleet(actual_nodes.nodes)
    for node_id in (1, 28):
        actual_nodes.shared.note_node_event(node_id)
    await health.reporter.report_nodes(actual_nodes.observations, health.admission, nodes=actual_nodes.nodes)
    assert len(health.messages) == 1 and "173" in health.messages[0][0] and "log transport" in health.messages[0][0]


@pytest.mark.asyncio
async def test_unexpected_transport_closure_reports_the_affected_node(actual_nodes, health):
    import utils.get_logs as streams

    actual_nodes.ready()
    await health.reporter.report_nodes(actual_nodes.observations, health.admission, nodes=actual_nodes.nodes)
    async with streams._track_log_transport(28, health.admission):
        actual_nodes.shared.note_node_event(28)
        actual_nodes.ready()
    assert len(health.messages) == 1
    assert "28" in health.messages[0][0] and "log transport" in health.messages[0][0]
    assert "Node 1:" not in health.messages[0][0]


@pytest.mark.asyncio
async def test_failed_connection_notice_does_not_alarm_other_starting_nodes(actual_nodes, health, monkeypatch):
    import utils.get_logs as streams

    actual_nodes.shared.forget_node_event(1)
    actual_nodes.shared.forget_node_event(28)
    monkeypatch.setattr(streams, "monitoring_is_current", lambda admission: admission is health.admission)
    monkeypatch.setattr(streams, "_get_status_throttle_interval", AsyncMock(return_value=60))
    monkeypatch.setattr(streams, "_status_initializing", True)
    await streams._update_node_status(28, "relay 28", "❌ HTTP Error", health.admission)
    assert len(health.messages) == 1
    assert "28" in health.messages[0][0] and "Node 1:" not in health.messages[0][0]


@pytest.mark.asyncio
async def test_panel_poll_reports_included_failure_even_without_an_active_task(actual_nodes, health, monkeypatch):
    import asyncio
    import utils.get_logs as streams
    from utils.types import PanelType

    actual_nodes.ready()
    await health.reporter.report_nodes(actual_nodes.observations, health.admission, nodes=actual_nodes.nodes)
    actual_nodes.nodes[1].status = "error"

    async def finish(*_args):
        raise asyncio.CancelledError

    monkeypatch.setattr(streams, "synchronize_monitoring", AsyncMock(return_value=health.admission))
    monkeypatch.setattr(streams, "monitoring_is_current", lambda admission: admission is health.admission)
    monkeypatch.setattr(streams, "_get_node_poll_interval", AsyncMock(return_value=180))
    monkeypatch.setattr(streams, "_refresh_log_config", AsyncMock())
    monkeypatch.setattr(streams, "get_nodes", AsyncMock(return_value=actual_nodes.nodes))
    monkeypatch.setattr(streams, "wait_for_monitoring_change", finish)
    with pytest.raises(asyncio.CancelledError):
        await streams.handle_cancel(PanelType(panel_domain="panel.invalid", panel_api_key="pg_key_test"), [])
    assert len(health.messages) == 1
    assert "28" in health.messages[0][0] and "panel connection unavailable" in health.messages[0][0]


@pytest.mark.asyncio
async def test_scheduled_stream_rebuild_does_not_report_a_failed_monitoring_incident(actual_nodes, health, monkeypatch):
    import asyncio
    import utils.get_logs as streams
    from utils.types import PanelType

    actual_nodes.ready()
    await health.reporter.report_nodes(actual_nodes.observations, health.admission, nodes=actual_nodes.nodes)
    periodic_waits = 0
    original_sleep = asyncio.sleep

    async def sleep(delay):
        nonlocal periodic_waits
        if delay == 7200:
            periodic_waits += 1
            if periodic_waits > 1:
                raise asyncio.CancelledError
        else:
            await original_sleep(0)

    async def fetch(*_args, **_kwargs):
        await cycle(health, "skipped", reason="waiting for the node fleet")
        return actual_nodes.nodes

    monkeypatch.setattr(streams.asyncio, "sleep", sleep)
    monkeypatch.setattr(streams, "synchronize_monitoring", AsyncMock(return_value=health.admission))
    monkeypatch.setattr(streams, "monitoring_is_current", lambda admission: admission is health.admission)
    monkeypatch.setattr(streams, "_ip_source_is_api", AsyncMock(return_value=False))
    monkeypatch.setattr(streams, "get_nodes", fetch)
    monkeypatch.setattr(streams, "init_node_status_message", AsyncMock())
    monkeypatch.setattr(streams, "create_node_task", AsyncMock())
    monkeypatch.setattr(streams, "send_logs", AsyncMock())
    with pytest.raises(asyncio.CancelledError):
        await streams.handle_cancel_all(
            [], PanelType(panel_domain="panel.invalid", panel_api_key="pg_key_test"), SimpleNamespace()
        )
    assert health.messages == []
