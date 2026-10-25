"""Excluded nodes cannot contribute Logs data or block included-node evidence."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from test_log_history_replay import EPOCH, api_cycle, logs, record  # noqa: F401
from utils.log_observations import LOG_OBSERVATIONS
from utils.parse_logs import parse_logs
from utils.read_config import read_config, save_config_value
from utils.shared_state import ACTIVE_USERS, forget_node_event, note_node_event, pop_active_users_snapshot
from utils.types import NodeType


@pytest.fixture
async def streams(api_cycle, logs, monkeypatch):
    import utils.get_logs as collector

    monkeypatch.setenv("LOG_NODE_TIMEZONES", '{"1":"UTC","9":"UTC"}')
    monkeypatch.delenv("LOG_TIMEZONE", raising=False)
    for key, value in (("ip_source", "logs"), ("check_interval", "60"), ("disabled_nodes", "")):
        assert await save_config_value(key, value)
    config = await read_config()
    await pop_active_users_snapshot(config)
    monkeypatch.setattr(collector, "TASKS", [])
    monkeypatch.setattr(collector, "task_node_mapping", {})
    monkeypatch.setattr(collector, "_node_connection_status", {})
    monkeypatch.setattr(collector, "_pending_status_update_task", None)
    monkeypatch.setattr(collector, "send_logs", AsyncMock(return_value=77))
    monkeypatch.setattr(collector, "edit_message", AsyncMock(return_value=True))
    monkeypatch.setattr(collector, "get_token", AsyncMock(return_value=SimpleNamespace(panel_token="test")))
    opened = []
    started = {i: asyncio.Event() for i in (1, 9)}
    closed = {i: asyncio.Event() for i in (1, 9)}

    class Response:
        status_code = 200

        def __init__(self, node_id):
            self.node_id = node_id

        async def __aenter__(self):
            opened.append(self.node_id)
            return self

        async def __aexit__(self, *args):
            closed[self.node_id].set()
            return False

        async def aiter_lines(self):
            started[self.node_id].set()
            yield ": ping"
            await asyncio.Event().wait()

    class Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def stream(self, method, url, **kwargs):
            assert method == "GET"
            return Response(int(url.split("/")[-2]))

    monkeypatch.setattr(collector.httpx, "AsyncClient", Client)
    yield SimpleNamespace(module=collector, opened=opened, started=started, closed=closed)
    pending = collector._pending_status_update_task
    if pending is not None:
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.parametrize(
    "status, disabled",
    [("connected", "9"), ("error", "9"), ("connecting", "9"), ("unknown", "9"), ("disabled", ""), ("limited", "")],
)
async def test_saved_exclusion_applies_to_collection_and_enforcement(
    api_cycle, logs, streams, monkeypatch, status, disabled
):
    import utils.check_usage as usage

    # An excluded node must not even need a valid override, despite the default.
    monkeypatch.setenv("LOG_NODE_TIMEZONES", '{"1":"UTC","9":"bad/zone"}')
    assert await save_config_value("disabled_nodes", disabled)
    nodes = [NodeType(1, "included", "192.0.2.1", "connected"), NodeType(9, "excluded", "192.0.2.9", status)]
    await streams.module.init_node_status_message(nodes)
    async with asyncio.TaskGroup() as group:
        for node in nodes:
            await streams.module.create_node_task(api_cycle.panel, group, node)
        await asyncio.wait_for(streams.started[1].wait(), 1)
        logs.clock[0] += 60
        note_node_event(1)
        config = await read_config()
        for ip in ("5.6.7.8", "9.9.9.9"):
            await parse_logs(record("2026/10/23 10:01:20", ip=ip), 1, "included", config)
        await parse_logs(record("bad time", user="bob"), 9, "excluded", config)
        note_node_event(9)
        forget_node_event(9)
        assert not LOG_OBSERVATIONS.evidence().unavailable_reason
        assert set(ACTIVE_USERS) == {"alice"}
        await usage.check_users_usage(api_cycle.panel, config)
        assert set(api_cycle.warning.warnings) == {"alice"}
        assert streams.opened == [1]
        for task in streams.module.TASKS:
            task.cancel()


async def test_discovery_uses_saved_exclusion_and_quiet_included_stream(api_cycle, logs, streams, monkeypatch):
    assert await save_config_value("disabled_nodes", "9")
    nodes = [NodeType(i, str(i), f"192.0.2.{i}", "connected") for i in (1, 9)]
    monkeypatch.setattr(streams.module, "get_nodes", AsyncMock(return_value=nodes))
    async with asyncio.TaskGroup() as group:
        watcher = group.create_task(streams.module.check_and_add_new_nodes(api_cycle.panel, group))
        await asyncio.wait_for(streams.started[1].wait(), 1)
        logs.clock[0] += 60
        note_node_event(1)
        assert not LOG_OBSERVATIONS.evidence().unavailable_reason
        assert ACTIVE_USERS == {}
        assert streams.opened == [1]
        watcher.cancel()
        for task in streams.module.TASKS:
            task.cancel()


async def test_live_exclusion_and_reinclusion_require_full_interval(api_cycle, logs, streams, monkeypatch):
    from utils.shared_state import merge_active_users_snapshot

    nodes = [NodeType(i, str(i), f"192.0.2.{i}", "connected") for i in (1, 9)]
    monkeypatch.setattr(streams.module, "get_nodes", AsyncMock(return_value=nodes))
    await streams.module.init_node_status_message(nodes)
    async with asyncio.TaskGroup() as group:
        for node in nodes:
            await streams.module.create_node_task(api_cycle.panel, group, node)
        for event in streams.started.values():
            await asyncio.wait_for(event.wait(), 1)
        logs.clock[0] += 60
        for node_id in (1, 9):
            note_node_event(node_id)
        await parse_logs(record("2026/10/23 10:01:20"), 1, "included", await read_config())
        old_batch = await pop_active_users_snapshot()
        assert LOG_OBSERVATIONS.is_current(old_batch.log_evidence)

        assert await save_config_value("disabled_nodes", "9")
        await pop_active_users_snapshot(await read_config())
        assert "complete check interval" in LOG_OBSERVATIONS.evidence().unavailable_reason
        assert not LOG_OBSERVATIONS.is_current(old_batch.log_evidence)
        assert await merge_active_users_snapshot(old_batch) == 0
        # Closing the excluded stream later must not start another global warmup.
        logs.clock[0] += 30
        canceller = group.create_task(streams.module.handle_cancel(api_cycle.panel, streams.module.TASKS))
        await asyncio.wait_for(streams.closed[9].wait(), 1)
        canceller.cancel()
        logs.clock[0] += 29
        note_node_event(1)
        assert LOG_OBSERVATIONS.evidence().unavailable_reason
        logs.clock[0] += 1
        note_node_event(1)
        assert not LOG_OBSERVATIONS.evidence().unavailable_reason

        assert await save_config_value("disabled_nodes", "")
        await pop_active_users_snapshot(await read_config())
        streams.started[9].clear()
        watcher = group.create_task(streams.module.check_and_add_new_nodes(api_cycle.panel, group))
        await asyncio.wait_for(streams.started[9].wait(), 1)
        assert LOG_OBSERVATIONS.evidence().unavailable_reason
        logs.clock[0] += 59
        for node_id in (1, 9):
            note_node_event(node_id)
        assert LOG_OBSERVATIONS.evidence().unavailable_reason
        logs.clock[0] += 1
        for node_id in (1, 9):
            note_node_event(node_id)
        assert not LOG_OBSERVATIONS.evidence().unavailable_reason
        assert streams.opened == [1, 9, 9]
        watcher.cancel()
        for task in streams.module.TASKS:
            task.cancel()


@pytest.mark.parametrize("status", ["error", "connecting", "unknown"])
async def test_included_nonconnected_node_still_blocks(logs, status):
    nodes = [NodeType(1, "included", "192.0.2.1", "connected"), NodeType(2, "required", "192.0.2.2", status)]
    LOG_OBSERVATIONS.register_fleet(nodes)
    logs.clock[0] += 60
    note_node_event(1)
    assert "node 2: panel connection unavailable" == LOG_OBSERVATIONS.evidence().unavailable_reason


@pytest.mark.parametrize("status", ["disabled", "limited"])
async def test_panel_exclusion_removes_pending_connections_before_counting(logs, status):
    from utils.device_count import DeviceCountingConfig, count_devices

    nodes = [NodeType(i, str(i), f"192.0.2.{i}", "connected") for i in (1, 2)]
    LOG_OBSERVATIONS.register_fleet(nodes)
    await parse_logs(record(), 1, "included", logs.config)
    await parse_logs(record("2026/10/23 13:00:20.123456", ip="9.9.9.9"), 2, "excluded", logs.config)
    nodes[1].status = status
    LOG_OBSERVATIONS.register_fleet(nodes)
    batch = await pop_active_users_snapshot(logs.config)
    assert batch["alice"].ip == ["5.6.7.8"]
    assert count_devices(batch["alice"], DeviceCountingConfig()) == 1
    assert batch.log_evidence.coverage_complete
    assert LOG_OBSERVATIONS.is_current(batch.log_evidence)


@pytest.mark.parametrize("status, disabled", [("connected", [1]), ("disabled", []), ("limited", [])])
async def test_known_empty_scope_is_valid_after_warmup_but_unknown_fleet_is_not(logs, status, disabled):
    config = dict(logs.config, disabled_nodes=disabled, log_node_timezones={"1": "bad/zone"})
    await pop_active_users_snapshot(config)
    nodes = [NodeType(1, "excluded", "192.0.2.1", status)]
    LOG_OBSERVATIONS.register_fleet(nodes)
    await parse_logs(record("bad time"), 1, "excluded", config)
    logs.clock[0] += 59
    assert "complete check interval" in LOG_OBSERVATIONS.evidence().unavailable_reason
    logs.clock[0] += 1
    batch = await pop_active_users_snapshot(config)
    assert batch == {}
    assert LOG_OBSERVATIONS.is_current(batch.log_evidence)
    LOG_OBSERVATIONS.register_fleet(None)
    assert "waiting for the node fleet" == LOG_OBSERVATIONS.evidence().unavailable_reason

    # A node re-entering the scope requires its timezone, transport and a full interval.
    await pop_active_users_snapshot(logs.config)
    LOG_OBSERVATIONS.register_fleet([NodeType(1, "included", "192.0.2.1", "connected")])
    note_node_event(1)
    logs.clock[0] += 59
    note_node_event(1)
    assert LOG_OBSERVATIONS.evidence().unavailable_reason
    logs.clock[0] += 1
    note_node_event(1)
    assert not LOG_OBSERVATIONS.evidence().unavailable_reason


async def test_node_excluded_during_preparation_cannot_poison_completed_scope(logs, monkeypatch):
    import utils.parse_logs as parser

    nodes = [NodeType(i, str(i), f"192.0.2.{i}", "connected") for i in (1, 2)]
    LOG_OBSERVATIONS.register_fleet(nodes)
    waiting, release = asyncio.Event(), asyncio.Event()

    async def prepare(*args):
        waiting.set()
        await release.wait()
        return True

    monkeypatch.setattr(parser, "is_ip_accepted", prepare)
    task = asyncio.create_task(parse_logs(record("bad time"), 2, "excluded", logs.config))
    await waiting.wait()
    nodes[1].status = "limited"
    LOG_OBSERVATIONS.register_fleet(nodes)
    logs.clock[0] += 60
    note_node_event(1)
    assert not LOG_OBSERVATIONS.evidence().unavailable_reason
    release.set()
    await task
    assert ACTIVE_USERS == {}
    assert not LOG_OBSERVATIONS.evidence().unavailable_reason


async def test_parser_scope_roundtrip_does_not_poison_completed_scope(logs, monkeypatch):
    import asyncio
    import utils.parse_logs as parser
    from utils.log_observations import LOG_OBSERVATIONS
    from utils.shared_state import ACTIVE_USERS, note_node_event
    from utils.types import NodeType
    from test_log_history_replay import record

    nodes = [NodeType(i, str(i), f"192.0.2.{i}", "connected") for i in (1, 2)]
    LOG_OBSERVATIONS.register_fleet(nodes)
    started, release = asyncio.Event(), asyncio.Event()

    async def prepare(*args):
        started.set()
        await release.wait()
        return True

    monkeypatch.setattr(parser, "is_ip_accepted", prepare)
    task = asyncio.create_task(parser.parse_logs(record("bad time"), 2, "node", logs.config))
    await started.wait()
    nodes[1].status = "limited"
    LOG_OBSERVATIONS.register_fleet(nodes)
    logs.clock[0] += 60
    note_node_event(1)
    assert not LOG_OBSERVATIONS.evidence().unavailable_reason
    nodes[1].status = "connected"
    LOG_OBSERVATIONS.register_fleet(nodes)
    note_node_event(2)
    logs.clock[0] += 60
    note_node_event(1)
    note_node_event(2)
    before = LOG_OBSERVATIONS.evidence().unavailable_reason
    assert before == ""
    release.set()
    await task
    after = LOG_OBSERVATIONS.evidence().unavailable_reason
    assert ACTIVE_USERS == {}
    assert after == "", "Old parser must not poison a later completed monitoring scope"


async def test_reenabled_healthy_stream_survives_stale_exclusion(api_cycle, logs, streams, monkeypatch):
    import asyncio
    from unittest.mock import AsyncMock
    from utils.read_config import read_config, save_config_value
    from utils.shared_state import pop_active_users_snapshot
    from utils.types import NodeType

    nodes = [NodeType(i, str(i), f"192.0.2.{i}", "connected") for i in (1, 9)]
    monkeypatch.setattr(streams.module, "get_nodes", AsyncMock(return_value=nodes))
    monkeypatch.setattr(streams.module, "_update_node_status", AsyncMock())
    assert await save_config_value("disabled_nodes", "9")
    await streams.module.init_node_status_message(nodes)
    first_poll, allow_second, second_poll = asyncio.Event(), asyncio.Event(), asyncio.Event()
    sleeps = 0

    async def controlled_sleep(delay):
        nonlocal sleeps
        sleeps += 1
        if sleeps == 1:
            first_poll.set()
            await allow_second.wait()
        else:
            second_poll.set()
            await asyncio.Event().wait()

    monkeypatch.setattr(streams.module.asyncio, "sleep", controlled_sleep)
    async with asyncio.TaskGroup() as group:
        await streams.module.create_node_task(api_cycle.panel, group, nodes[0])
        await asyncio.wait_for(streams.started[1].wait(), 1)
        canceller = group.create_task(streams.module.handle_cancel(api_cycle.panel, streams.module.TASKS))
        await asyncio.wait_for(first_poll.wait(), 1)
        assert await save_config_value("disabled_nodes", "")
        await pop_active_users_snapshot(await read_config())
        await streams.module.create_node_task(api_cycle.panel, group, nodes[1])
        await asyncio.wait_for(streams.started[9].wait(), 1)
        allow_second.set()
        await asyncio.wait_for(second_poll.wait(), 1)
        try:
            assert not streams.closed[9].is_set(), "Re-included healthy stream must not be cancelled by stale exclusion"
        finally:
            canceller.cancel()
            for task in streams.module.TASKS:
                task.cancel()


async def test_unknown_fleet_keeps_last_known_panel_exclusions(logs, monkeypatch):
    from utils.log_observations import LOG_OBSERVATIONS
    from utils.parse_logs import parse_logs
    from utils.shared_state import note_node_event, NODE_LAST_EVENT
    from utils.types import NodeType
    from test_log_history_replay import record

    LOG_OBSERVATIONS.register_fleet(
        [
            NodeType(1, "included", "192.0.2.1", "connected"),
            NodeType(2, "excluded", "192.0.2.2", "limited"),
        ]
    )
    assert not LOG_OBSERVATIONS.monitors(2)
    LOG_OBSERVATIONS.register_fleet(None)
    previous_heartbeat = NODE_LAST_EVENT.get(2)
    await parse_logs(record("bad time"), 2, "excluded", logs.config)
    note_node_event(2)
    assert NODE_LAST_EVENT.get(2) == previous_heartbeat
    assert LOG_OBSERVATIONS.uncertain_until <= logs.clock[0]
    assert LOG_OBSERVATIONS.evidence().unavailable_reason == "waiting for the node fleet"
    assert not LOG_OBSERVATIONS.monitors(2), "Failed lookup cannot positively reinclude a last-known excluded node"
