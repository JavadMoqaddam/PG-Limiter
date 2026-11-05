"""Node summaries own delayed outcomes without blocking collection or appending duplicates."""

import asyncio
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.error import BadRequest, TimedOut

import telegram_bot.dispatcher as dm
import telegram_bot.send_message as sm
import utils.get_logs as logs
import utils.monitoring_gate as gate
from test_outbound_ownership import outbound  # noqa: F401
from utils.types import NodeType


async def eventually(predicate):
    async with asyncio.timeout(1):
        while not predicate():
            await asyncio.sleep(0.001)


@pytest.fixture
async def summary(outbound, monkeypatch):
    monkeypatch.setattr(gate, "get_topics_manager", lambda: outbound.topics)
    monkeypatch.setattr(gate, "_synchronized_admission", None)
    monkeypatch.setattr(gate, "_sync_lock", asyncio.Lock())
    monkeypatch.setattr(logs, "_refresh_log_config", AsyncMock())
    monkeypatch.setattr(logs, "_report_node_health", AsyncMock())
    monkeypatch.setattr(logs.LOG_OBSERVATIONS, "monitors", lambda node: True)
    monkeypatch.setattr(logs, "_get_status_throttle_interval", AsyncMock(return_value=0))
    monkeypatch.setattr(logs, "_node_connection_status", {})
    monkeypatch.setattr(logs, "TASKS", [])
    monkeypatch.setattr(logs, "task_node_mapping", {})
    monkeypatch.setattr(logs, "task_monitoring_admissions", {})
    # Compress the old wrapper's 10s receipt timeout, while using the real
    # dispatcher and producer. The pending-send test outlives this timeout.
    wait_for = asyncio.wait_for
    sleep = asyncio.sleep

    async def short_wait(awaitable, timeout):
        return await wait_for(awaitable, 0.01 if timeout == 10 else timeout)

    async def fast_backoff(seconds):
        await sleep(0 if seconds >= 0.1 else seconds)

    monkeypatch.setattr(sm.asyncio, "wait_for", short_wait)
    monkeypatch.setattr(dm.asyncio, "sleep", fast_backoff)
    outbound.dispatcher.bucket = dm.TokenBucket(rate=1000, capacity=1000)
    edits = []
    handles = []
    enqueue = sm.enqueue_log_delivery

    async def capture(*args, **kwargs):
        handle = await enqueue(*args, **kwargs)
        handles.append(handle)
        return handle

    monkeypatch.setattr(logs, "enqueue_log_delivery", capture, raising=False)

    async def edit(**kwargs):
        edits.append(kwargs)
        return True

    outbound.bot.edit_message_text = AsyncMock(side_effect=edit)
    worker = asyncio.create_task(outbound.dispatcher.start_worker())
    state = SimpleNamespace(outbound=outbound, edits=edits, handles=handles, worker=worker)
    yield state
    await logs.retire_log_streams()
    await outbound.dispatcher.stop(wait_seconds=0.01)
    await worker


async def initialize(name="relay"):
    await asyncio.wait_for(logs.init_node_status_message([NodeType(1, name, "192.0.2.1", "connected")]), 0.1)


async def update(status="✅ Connected"):
    await asyncio.wait_for(logs._update_node_status(1, "relay", status), 0.1)


async def test_pending_initial_send_keeps_one_receipt_and_coalesces_latest_text(summary):
    entered, release = asyncio.Event(), asyncio.Event()
    remote = summary.outbound.sent
    send = summary.outbound.bot.send_message.side_effect

    async def pending(**kwargs):
        entered.set()
        await release.wait()
        return await send(**kwargs)

    summary.outbound.bot.send_message.side_effect = pending
    try:
        await initialize()
        await entered.wait()
        await asyncio.sleep(0.03)
        await update("⏳ Reconnecting...")
        await update("✅ Connected")
        assert summary.outbound.bot.send_message.await_count == 1
        release.set()
        await eventually(lambda: bool(summary.edits))
        assert len(remote) == 1
        assert summary.edits[-1]["message_id"] == 1
        assert "✅ Connected" in summary.edits[-1]["text"]
    finally:
        release.set()


@pytest.mark.parametrize("error", [None, TimedOut("synthetic lost edit response")])
async def test_pending_or_ambiguous_edit_keeps_known_id(summary, error):
    await initialize()
    await eventually(lambda: len(summary.outbound.sent) == 1)
    entered, release = asyncio.Event(), asyncio.Event()

    async def pending(**kwargs):
        summary.edits.append(kwargs)
        entered.set()
        await release.wait()
        if error:
            raise error
        return True

    summary.outbound.bot.edit_message_text.side_effect = pending
    try:
        await update("⏳ Reconnecting...")
        await entered.wait()
        await update("✅ Connected")
        assert len(summary.outbound.sent) == 1
        release.set()
        await eventually(lambda: any("✅ Connected" in e["text"] for e in summary.edits))
        await asyncio.wait_for(summary.outbound.dispatcher.queue.join(), 1)
        assert len(summary.outbound.sent) == 1
        assert {e["message_id"] for e in summary.edits} == {1}
    finally:
        release.set()


async def test_definitely_unavailable_edit_permits_one_replacement(summary):
    await initialize()
    await eventually(lambda: len(summary.outbound.sent) == 1)

    async def unavailable(**kwargs):
        summary.edits.append(kwargs)
        if kwargs["message_id"] == 1:
            raise BadRequest("Message to edit not found")
        return True

    summary.outbound.bot.edit_message_text.side_effect = unavailable
    await update("⏳ Reconnecting...")
    await eventually(lambda: len(summary.outbound.sent) == 2)
    await update("✅ Connected")
    await eventually(lambda: any(e["message_id"] == 2 for e in summary.edits))
    assert len(summary.outbound.sent) == 2


async def test_ambiguous_initial_send_blocks_automatic_replacement_until_new_scope(summary):
    async def ambiguous(**kwargs):
        summary.outbound.sent.append(kwargs)
        raise TimedOut("synthetic accepted append")

    summary.outbound.bot.send_message.side_effect = ambiguous
    await initialize()
    await eventually(lambda: len(summary.outbound.sent) == 1)
    await asyncio.wait_for(summary.outbound.dispatcher.queue.join(), 1)
    await update("⏳ Reconnecting...")
    await update()
    await asyncio.sleep(0.03)
    assert len(summary.outbound.sent) == 1
    assert summary.edits == []
    await initialize("new scope")
    await eventually(lambda: len(summary.outbound.sent) == 2)


@pytest.mark.parametrize("boundary", ["pause", "generation", "same_admission_init", "retire", "shutdown"])
async def test_retirement_fences_late_receipt_and_preserves_dispatcher_owner(summary, monkeypatch, boundary):
    entered, release = asyncio.Event(), asyncio.Event()
    send = summary.outbound.bot.send_message.side_effect

    async def pending(**kwargs):
        if not entered.is_set():
            entered.set()
            await release.wait()
        return await send(**kwargs)

    summary.outbound.bot.send_message.side_effect = pending
    try:
        await initialize("old scope")
        await entered.wait()
        await update("⏳ Reconnecting...")
        await eventually(lambda: len(summary.handles) == 1)
        original = summary.handles[0]
        if boundary == "pause":
            summary.outbound.topics.enabled = False
            await logs.retire_log_streams(gate.monitoring_snapshot())
        elif boundary == "generation":
            summary.outbound.topics.monitoring_revision += 1
            await logs.retire_log_streams(gate.monitoring_snapshot())
            await initialize("new scope")
        elif boundary == "same_admission_init":
            await initialize("new scope")
        elif boundary == "retire":
            await logs.retire_log_streams()
        else:
            monkeypatch.setattr(sys, "argv", ["limiter"])
            import limiter
            import db.database
            import utils.panel_api.request_helper
            import utils.check_usage
            import telegram_bot.main

            monkeypatch.setattr(limiter, "close_geo_client", AsyncMock())
            monkeypatch.setattr(db.database, "close_db", AsyncMock())
            monkeypatch.setattr(utils.panel_api.request_helper, "close_panel_client", AsyncMock())
            monkeypatch.setattr(utils.check_usage, "isp_detector", None)
            monkeypatch.setattr(telegram_bot.main, "application", None)
            monkeypatch.setattr(dm, "get_dispatcher", lambda: summary.outbound.dispatcher)
            stop = summary.outbound.dispatcher.stop

            async def drain(**kwargs):
                assert not any(t.get_name() == "node-status-delivery" for t in asyncio.all_tasks())
                release.set()
                await stop(wait_seconds=0.1)

            monkeypatch.setattr(summary.outbound.dispatcher, "stop", drain)
            await limiter.cleanup_resources()
            monkeypatch.setattr(summary.outbound.dispatcher, "stop", stop)
        release.set()
        assert (await original.wait(timeout=1)).receipt == (1, -100111)
        if boundary in {"generation", "same_admission_init"}:
            await eventually(lambda: len(summary.outbound.sent) == 2)
            await update()
            await eventually(lambda: bool(summary.edits))
            assert {e["message_id"] for e in summary.edits} == {2}
        else:
            await asyncio.sleep(0.03)
            assert summary.edits == []
            assert not any(t.get_name() == "node-status-delivery" for t in asyncio.all_tasks())
    finally:
        release.set()


async def test_cancelled_caller_during_admission_does_not_cancel_summary_receipt(summary, monkeypatch):
    admitted, release, returned = asyncio.Event(), asyncio.Event(), asyncio.Event()
    queue = summary.outbound.dispatcher._queue

    async def pending(item):
        admitted.set()
        await release.wait()
        await queue(item)

    async def caller():
        await initialize()
        returned.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(summary.outbound.dispatcher, "_queue", pending)
    task = asyncio.create_task(caller())
    try:
        await asyncio.wait_for(admitted.wait(), 0.2)
        await eventually(returned.is_set)
        assert summary.outbound.sent == []
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

        await update()
        release.set()
        await eventually(lambda: bool(summary.edits))
        assert len(summary.outbound.sent) == 1
        assert summary.edits[-1]["message_id"] == 1
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_periodic_refresh_retires_pending_summary_before_fetch(summary, monkeypatch):
    entered, release, refreshed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    send = summary.outbound.bot.send_message.side_effect
    sleep = asyncio.sleep
    cycles = 0

    async def pending(**kwargs):
        if not entered.is_set():
            entered.set()
            await release.wait()
        return await send(**kwargs)

    async def periodic_sleep(seconds):
        nonlocal cycles
        if seconds == 7200:
            cycles += 1
            if cycles > 1:
                await asyncio.Event().wait()
        elif seconds != 2:
            await sleep(seconds)

    async def fetch(*args):
        assert not any(t.get_name() == "node-status-delivery" for t in asyncio.all_tasks())
        refreshed.set()
        return None

    summary.outbound.bot.send_message.side_effect = pending
    await initialize()
    await entered.wait()
    await update()
    await eventually(lambda: bool(summary.handles))
    original = summary.handles[0]
    monkeypatch.setattr(logs, "_ip_source_is_api", AsyncMock(return_value=False))
    monkeypatch.setattr(logs, "get_nodes", fetch)
    monkeypatch.setattr(logs.asyncio, "sleep", periodic_sleep)
    refresh = asyncio.create_task(logs.handle_cancel_all([], None, None))
    try:
        await asyncio.wait_for(refreshed.wait(), 1)
        release.set()
        assert (await original.wait(timeout=1)).receipt == (1, -100111)
        await asyncio.wait_for(summary.outbound.dispatcher.queue.join(), 1)
        assert summary.edits == []
    finally:
        release.set()
        refresh.cancel()
        await asyncio.gather(refresh, return_exceptions=True)


async def test_identical_confirmed_summary_is_not_edited(summary, monkeypatch):
    await initialize()
    await eventually(lambda: bool(summary.outbound.sent))
    text = summary.outbound.sent[0]["text"]
    monkeypatch.setattr(logs, "_build_node_status_message", AsyncMock(return_value=text))
    await update()
    await asyncio.sleep(0.03)
    assert len(summary.outbound.sent) == 1
    assert summary.edits == []


async def test_trailing_throttle_flushes_latest_state(summary, monkeypatch):
    await initialize()
    await eventually(lambda: bool(summary.outbound.sent))
    monkeypatch.setattr(logs, "_get_status_throttle_interval", AsyncMock(return_value=0.03))
    await update("⏳ Reconnecting...")
    await update("✅ Connected")
    assert summary.edits == []
    await eventually(lambda: bool(summary.edits))
    assert len(summary.edits) == 1
    assert "✅ Connected" in summary.edits[0]["text"]


async def test_ambiguous_edit_cannot_treat_previous_confirmed_text_as_current(summary, monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    render = AsyncMock(return_value="state A")
    monkeypatch.setattr(logs, "_build_node_status_message", render)
    await initialize()
    await eventually(lambda: bool(summary.outbound.sent))
    remote_text = ["state A"]

    async def edit(**kwargs):
        remote_text[0] = kwargs["text"]
        if kwargs["text"] == "state B":
            entered.set()
            await release.wait()
            raise TimedOut("synthetic accepted edit")
        return True

    summary.outbound.bot.edit_message_text.side_effect = edit
    try:
        render.return_value = "state B"
        await update()
        await entered.wait()
        render.return_value = "state A"
        await update()
        release.set()
        await eventually(lambda: remote_text == ["state A"])
        assert len(summary.outbound.sent) == 1
    finally:
        release.set()


async def test_initial_preparation_cannot_overwrite_newer_desired_status(summary, monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()

    async def throttle():
        if not entered.is_set():
            entered.set()
            await release.wait()
        return 0

    monkeypatch.setattr(logs, "_get_status_throttle_interval", throttle)
    initial = asyncio.create_task(logs.init_node_status_message([NodeType(1, "relay", "192.0.2.1", "connected")]))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        await update("✅ Connected")
        await eventually(lambda: bool(summary.outbound.sent))
        release.set()
        await initial
        await asyncio.sleep(0.03)
        assert "✅ Connected" in summary.outbound.sent[0]["text"]
        assert all("⏳ Connecting..." not in edit["text"] for edit in summary.edits)
    finally:
        release.set()
        await initial
