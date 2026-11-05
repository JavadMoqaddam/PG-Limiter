"""Delivery uncertainty and warning freshness are exercised through the real worker."""

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter, TimedOut

import telegram_bot.dispatcher as dm
import telegram_bot.send_message as sm
from telegram_bot.dispatcher import DeliveryState, Priority, TokenBucket
from telegram_bot.topics import TopicType
from test_outbound_ownership import outbound  # noqa: F401


@pytest.fixture(autouse=True)
def fast_backoff(monkeypatch):
    monkeypatch.setenv("PTB_TIMEDELTA", "true")
    original_sleep = asyncio.sleep

    async def sleep(seconds):
        await original_sleep(0 if seconds >= 0.1 else seconds)

    monkeypatch.setattr(dm.asyncio, "sleep", sleep)


async def drain(dispatcher):
    dispatcher.bucket = TokenBucket(rate=1000, capacity=1000)
    worker = asyncio.create_task(dispatcher.start_worker())
    await asyncio.sleep(0)
    await asyncio.wait_for(dispatcher.queue.join(), 1)
    await dispatcher.stop(wait_seconds=0.05)
    await worker


@pytest.mark.parametrize("priority", [Priority.WARNINGS, Priority.SYSTEM])
async def test_accepted_then_timeout_is_once_per_noncritical_event(outbound, priority):
    visible = []

    async def ambiguous_send(**kwargs):
        visible.append(kwargs["text"])
        raise TimedOut("synthetic accepted send")

    outbound.bot.send_message.side_effect = ambiguous_send
    futures = await outbound.dispatcher.enqueue_send_batch([str(i) for i in range(1, 13)], priority=priority)
    items = list(outbound.dispatcher._events.values())
    await drain(outbound.dispatcher)
    assert visible == [str(i) for i in range(1, 13)]
    assert all(item.state == DeliveryState.UNCERTAIN for item in items)
    assert all(await asyncio.gather(*futures)) is False


async def test_critical_ambiguity_retries_same_durable_event(outbound):
    observed = []

    async def send(**kwargs):
        rows = json.loads(outbound.pending.read_text())
        observed.append(tuple(rows))
        if len(observed) == 1:
            raise TimedOut("synthetic accepted send")
        return SimpleNamespace(message_id=42)

    outbound.bot.send_message.side_effect = send
    future = await outbound.dispatcher.enqueue_send("critical", priority=Priority.CRITICAL, return_future=True)
    await drain(outbound.dispatcher)
    assert len(observed) == 2
    assert observed[0] == observed[1]
    assert len(observed[0]) == 1
    assert await future == (42, -100111)
    assert json.loads(outbound.pending.read_text()) == {}


@pytest.mark.parametrize(
    "cause_type,wrapper_type",
    [(httpx.ConnectError, NetworkError), (httpx.ConnectTimeout, TimedOut), (httpx.PoolTimeout, TimedOut)],
)
async def test_wrapped_pretransmission_failure_retries_without_duplicate_acceptance(outbound, cause_type, wrapper_type):
    attempts = []
    visible = []

    async def send(**kwargs):
        attempts.append(kwargs["text"])
        if len(attempts) == 1:
            try:
                raise cause_type("synthetic connect failure")
            except cause_type as cause:
                raise wrapper_type("wrapped transport failure") from cause
        visible.append(kwargs["text"])
        return SimpleNamespace(message_id=42)

    outbound.bot.send_message.side_effect = send
    handle = await sm.enqueue_log_delivery("safe retry")
    await drain(outbound.dispatcher)
    result = await handle.wait()
    assert attempts == ["safe retry", "safe retry"]
    assert visible == ["safe retry"]
    assert (result.status, result.receipt) == ("delivered", (42, -100111))


async def test_retry_after_retries_same_event_without_duplicate_acceptance(outbound):
    attempts = []
    visible = []

    async def send(**kwargs):
        attempts.append(tuple(outbound.dispatcher._events))
        if len(attempts) == 1:
            raise RetryAfter(0)
        visible.append(kwargs["text"])
        return SimpleNamespace(message_id=42)

    outbound.bot.send_message.side_effect = send
    handle = await sm.enqueue_log_delivery("rate limited")
    await drain(outbound.dispatcher)
    assert attempts == [handle.event_ids, handle.event_ids]
    assert visible == ["rate limited"]
    assert (await handle.wait()).status == "delivered"


async def test_handle_timeout_and_cancellation_preserve_original_owner(outbound):
    handle = await sm.enqueue_log_delivery("owned", message_key="same")
    assert (await handle.wait(timeout=0.001)).status == "pending"
    waiter = asyncio.create_task(handle.wait())
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    duplicate = await sm.enqueue_log_delivery("owned", message_key="same")
    assert duplicate.event_ids == handle.event_ids
    await drain(outbound.dispatcher)
    result = await handle.wait()
    assert (result.status, result.receipt, result.replacement_allowed) == ("delivered", (1, -100111), False)
    assert len(outbound.sent) == 1


@pytest.mark.parametrize(
    "error,replacement_allowed",
    [
        (BadRequest("Message to edit not found"), True),
        (TimedOut("ambiguous edit"), False),
        (Forbidden("permission"), False),
    ],
)
async def test_edit_handle_replacement_requires_confirmed_unavailability(outbound, error, replacement_allowed):
    async def edit(**kwargs):
        raise error

    outbound.bot.edit_message_text = edit
    handle = await sm.enqueue_edit_delivery((7, -100111), "updated")
    await drain(outbound.dispatcher)
    result = await handle.wait()
    assert result.status == "failed"
    assert result.replacement_allowed is replacement_allowed


async def test_uncertain_batch_reserves_same_key_and_forbids_full_append_fallback(outbound):
    async def send(**kwargs):
        if kwargs["text"].startswith("A"):
            raise TimedOut("synthetic accepted send")
        return SimpleNamespace(message_id=42)

    outbound.bot.send_message.side_effect = send
    handle = await sm.enqueue_log_delivery("A" * 3900 + "B", message_key="batch")
    worker = asyncio.create_task(outbound.dispatcher.start_worker())
    result = await handle.wait(timeout=1)
    assert (result.status, result.receipt, result.replacement_allowed) == ("uncertain", None, False)
    duplicate = await sm.enqueue_log_delivery("A" * 3900 + "B", message_key="batch")
    assert duplicate.event_ids == handle.event_ids
    assert outbound.bot.send_message.await_count == 2
    await outbound.dispatcher.stop(wait_seconds=0.05)
    await worker


@pytest.mark.parametrize("stage", ["queued", "token", "backoff", "sending"])
async def test_new_warning_scan_supersedes_old_pending_chunks(outbound, monkeypatch, stage):
    entered = asyncio.Event()
    release = asyncio.Event()
    original_sleep = asyncio.sleep
    old = await sm.send_warning_report(["old1", "old2"], ttl=600)
    original_send = outbound.bot.send_message.side_effect
    attempts = []

    async def send(**kwargs):
        attempts.append(kwargs["text"])
        if kwargs["text"] == "old1":
            if stage == "backoff":
                raise RetryAfter(0)
            if stage == "sending":
                entered.set()
                await release.wait()
        return await original_send(**kwargs)

    async def consume(*args):
        entered.set()
        await release.wait()

    async def sleep(seconds):
        if seconds >= 0.1:
            entered.set()
            await release.wait()
        else:
            await original_sleep(seconds)

    outbound.bot.send_message.side_effect = send
    if stage == "token":
        monkeypatch.setattr(outbound.dispatcher.bucket, "consume", consume)
    if stage == "backoff":
        monkeypatch.setattr(dm.asyncio, "sleep", sleep)
    worker = None
    try:
        if stage != "queued":
            worker = asyncio.create_task(outbound.dispatcher.start_worker())
            await asyncio.wait_for(entered.wait(), 1)
        new = await sm.send_warning_report(["new1", "new2"], ttl=600)
        release.set()
        if worker is None:
            worker = asyncio.create_task(outbound.dispatcher.start_worker())
        assert (await new.wait(timeout=1)).status == "delivered"
        result = await old.wait(timeout=1)
        assert result.status == "cancelled"
        assert [row["text"] for row in outbound.sent] == (["old1"] if stage == "sending" else []) + ["new1", "new2"]
        assert attempts.count("old1") <= 1
    finally:
        release.set()
        await outbound.dispatcher.stop(wait_seconds=0.05)
        if worker:
            await worker


async def test_empty_current_warning_scan_retires_previous_without_empty_message(outbound):
    old = await sm.send_warning_report(["old"], ttl=600)
    assert await sm.send_warning_report([], ttl=600) is None
    await drain(outbound.dispatcher)
    assert (await old.wait()).status == "cancelled"
    assert outbound.sent == []


async def test_warning_supersession_is_scoped_and_keeps_standalone_and_critical(outbound):
    original = await sm.send_warning_report(["destination one"], ttl=600)
    standalone = await sm.enqueue_log_delivery("standalone", topic_type=TopicType.WARNINGS, priority=Priority.WARNINGS)
    critical = await sm.enqueue_log_delivery("incident", priority=Priority.CRITICAL)
    outbound.topics.group_id = -100222
    other = await sm.send_warning_report(["destination two"], ttl=600)
    await drain(outbound.dispatcher)
    assert all([(await handle.wait()).status == "delivered" for handle in [original, standalone, critical, other]])
    assert {(row["text"], row["chat_id"]) for row in outbound.sent} == {
        ("destination one", -100111),
        ("standalone", -100111),
        ("incident", -100111),
        ("destination two", -100222),
    }


async def test_distinct_repeated_warning_scans_are_not_deduplicated(outbound):
    first = await sm.send_warning_report(["same text"], ttl=600)
    worker = asyncio.create_task(outbound.dispatcher.start_worker())
    assert (await first.wait(timeout=1)).status == "delivered"
    second = await sm.send_warning_report(["same text"], ttl=600)
    assert (await second.wait(timeout=1)).status == "delivered"
    assert first.event_ids != second.event_ids
    assert [row["text"] for row in outbound.sent] == ["same text", "same text"]
    assert not outbound.dispatcher._warning_reports
    await outbound.dispatcher.stop(wait_seconds=0.05)
    await worker


async def test_failed_warning_admission_does_not_supersede_live_report(outbound, monkeypatch):
    old = await sm.send_warning_report(["old"], ttl=600)
    original_queue = outbound.dispatcher._queue

    async def reject(item):
        if item.text == "new":
            raise RuntimeError("synthetic admission failure")
        await original_queue(item)

    monkeypatch.setattr(outbound.dispatcher, "_queue", reject)
    with pytest.raises(RuntimeError, match="synthetic admission failure"):
        await sm.send_warning_report(["new"], ttl=600)
    await drain(outbound.dispatcher)
    assert (await old.wait()).status == "delivered"
    assert [row["text"] for row in outbound.sent] == ["old"]


async def test_cancelled_warning_admission_keeps_the_prior_scan_live(outbound, monkeypatch):
    old = await sm.send_warning_report(["old"], ttl=600)
    entered = asyncio.Event()
    original_queue = outbound.dispatcher._queue

    async def wait_to_admit(item):
        if item.text == "new":
            entered.set()
            await asyncio.Event().wait()
        await original_queue(item)

    monkeypatch.setattr(outbound.dispatcher, "_queue", wait_to_admit)
    caller = asyncio.create_task(sm.send_warning_report(["new"], ttl=600))
    await asyncio.wait_for(entered.wait(), 1)
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    await drain(outbound.dispatcher)
    assert (await old.wait()).status == "delivered"
    assert [row["text"] for row in outbound.sent] == ["old"]


@pytest.mark.parametrize("stage,status", [("token", "pending"), ("sending", "uncertain")])
async def test_worker_cancellation_classifies_whether_transport_started(outbound, monkeypatch, stage, status):
    entered = asyncio.Event()
    release = asyncio.Event()

    async def block(*args, **kwargs):
        entered.set()
        await release.wait()

    if stage == "token":
        monkeypatch.setattr(outbound.dispatcher.bucket, "consume", block)
    else:
        outbound.bot.send_message.side_effect = block
    handle = await sm.enqueue_log_delivery("owned")
    worker = asyncio.create_task(outbound.dispatcher.start_worker())
    await asyncio.wait_for(entered.wait(), 1)
    worker.cancel()
    await worker
    result = await handle.wait(timeout=0.001)
    assert (result.status, result.replacement_allowed) == (status, False)
    if stage == "sending":
        assert outbound.dispatcher.queue.empty()
        assert outbound.bot.send_message.await_count == 1
    release.set()


async def test_cancel_pending_cannot_downgrade_an_ambiguous_inflight_send(outbound):
    entered = asyncio.Event()
    release = asyncio.Event()

    async def send(**kwargs):
        entered.set()
        await release.wait()
        raise TimedOut("synthetic accepted send")

    outbound.bot.send_message.side_effect = send
    handle = await sm.enqueue_log_delivery("owned", cancel_key="observation")
    worker = asyncio.create_task(outbound.dispatcher.start_worker())
    await asyncio.wait_for(entered.wait(), 1)
    cancelling = asyncio.create_task(outbound.dispatcher.cancel_pending("observation"))
    await asyncio.sleep(0)
    release.set()
    await cancelling
    assert (await handle.wait(timeout=1)).status == "uncertain"
    await outbound.dispatcher.stop(wait_seconds=0.05)
    await worker


async def test_uncertain_local_completion_error_cannot_requeue_transport(outbound, monkeypatch):
    outbound.bot.send_message.side_effect = TimedOut("synthetic accepted send")
    original_finish = outbound.dispatcher._finish
    failures = []

    async def finish(item, result=None):
        if item.state == DeliveryState.UNCERTAIN and not failures:
            failures.append(item.event_id)
            raise OSError("synthetic completion failure")
        await original_finish(item, result)

    monkeypatch.setattr(outbound.dispatcher, "_finish", finish)
    handle = await sm.enqueue_log_delivery("owned")
    await drain(outbound.dispatcher)
    assert (await handle.wait()).status == "uncertain"
    assert outbound.bot.send_message.await_count == 1


async def test_critical_receipt_write_failure_retries_only_local_ack(outbound, monkeypatch):
    original_write = dm.atomic_write_json
    failed = []

    def write(path, data):
        if any(row.get("status") == "delivered" for row in data.values()) and not failed:
            failed.append(tuple(data))
            raise OSError("synthetic receipt acknowledgement failure")
        original_write(path, data)

    monkeypatch.setattr(dm, "atomic_write_json", write)
    handle = await sm.enqueue_log_delivery("incident", priority=Priority.CRITICAL)
    await drain(outbound.dispatcher)
    assert failed == [handle.event_ids]
    assert (await handle.wait()).receipt == (1, -100111)
    assert outbound.bot.send_message.await_count == 1
    assert json.loads(outbound.pending.read_text()) == {}


async def test_warning_physical_chunks_are_bounded_and_remain_one_scan(outbound):
    handle = await sm.send_warning_report(["A" * 8000, "B"], ttl=600)
    await drain(outbound.dispatcher)
    assert (await handle.wait()).status == "delivered"
    assert len(handle.event_ids) == 4
    assert [len(row["text"]) for row in outbound.sent] == [3900, 3900, 200, 1]


async def test_obsolete_empty_scan_cannot_retire_a_current_report(outbound, monkeypatch):
    import utils.check_usage as usage

    current = await sm.send_warning_report(["current"], ttl=600)
    monkeypatch.setattr(usage, "monitoring_is_current", lambda snapshot: False)
    await usage.dispatch_chunked_warnings([], 60, 0)
    await drain(outbound.dispatcher)
    assert (await current.wait()).status == "delivered"
    assert [row["text"] for row in outbound.sent] == ["current"]


@pytest.mark.parametrize("error_type,status", [(RetryAfter, "cancelled"), (TimedOut, "uncertain")])
async def test_inflight_old_warning_never_retries_behind_new_scan(outbound, error_type, status):
    entered = asyncio.Event()
    release = asyncio.Event()
    attempts = []
    original_send = outbound.bot.send_message.side_effect

    async def send(**kwargs):
        attempts.append(kwargs["text"])
        if kwargs["text"] == "old":
            entered.set()
            await release.wait()
            raise error_type(0) if error_type is RetryAfter else error_type("synthetic accepted send")
        return await original_send(**kwargs)

    outbound.bot.send_message.side_effect = send
    old = await sm.send_warning_report(["old"], ttl=600)
    worker = asyncio.create_task(outbound.dispatcher.start_worker())
    await asyncio.wait_for(entered.wait(), 1)
    new = await sm.send_warning_report(["new"], ttl=600)
    release.set()
    assert (await new.wait(timeout=1)).status == "delivered"
    assert (await old.wait()).status == status
    assert attempts == ["old", "new"]
    await outbound.dispatcher.stop(wait_seconds=0.05)
    await worker


@pytest.mark.parametrize("revision", ["monitoring_revision", "destination_revision"])
async def test_warning_supersession_does_not_cross_revisions(outbound, revision):
    old = await sm.send_warning_report(["old revision"], ttl=600)
    setattr(outbound.topics, revision, 1)
    await sm.send_warning_report([], ttl=600)
    assert (await old.wait(timeout=0.001)).status == "pending"
    await drain(outbound.dispatcher)
    expected = "cancelled" if revision == "monitoring_revision" else "delivered"
    assert (await old.wait()).status == expected


async def test_existing_tuple_none_bool_wrappers_keep_delivery_results(outbound):
    async def edit(**kwargs):
        return True

    outbound.bot.edit_message_text = edit
    worker = asyncio.create_task(outbound.dispatcher.start_worker())
    assert await sm.send_logs("legacy", return_message_id=True) == (1, -100111)
    assert await sm.send_logs("fire and forget") is None
    assert await sm.edit_message((1, -100111), "edit") is True
    assert await sm.delete_message((1, -100111)) is True
    await outbound.dispatcher.stop(wait_seconds=0.05)
    await worker


async def test_restored_handle_and_legacy_futures_follow_logical_chunk_order(outbound, monkeypatch):
    handle = await sm.enqueue_log_delivery("A" * 3900 + "B", message_key="restored", priority=Priority.CRITICAL)
    rows = json.loads(outbound.pending.read_text())
    outbound.pending.write_text(json.dumps(dict(reversed(list(rows.items())))))
    restored = dm.TelegramDispatcher()
    restored.set_bot(outbound.bot)
    await restored.reload_pending_critical_messages()
    monkeypatch.setattr(sm, "get_dispatcher", lambda: restored)
    recovered = await sm.enqueue_log_delivery("unused", message_key="restored", priority=Priority.CRITICAL)
    assert recovered.event_ids == handle.event_ids
    futures = await restored.enqueue_send_batch(["unused"], priority=Priority.CRITICAL, message_key="restored")
    await drain(restored)
    assert await asyncio.gather(*futures) == [(2, -100111), (1, -100111)]
    assert (await recovered.wait()).receipt == (1, -100111)


@pytest.mark.parametrize("stage", ["queued", "token", "sending"])
async def test_shutdown_settles_abandoned_edit_handle(outbound, monkeypatch, stage):
    entered = asyncio.Event()

    async def block(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    if stage == "token":
        monkeypatch.setattr(outbound.dispatcher.bucket, "consume", block)
    else:
        outbound.bot.edit_message_text = block
    handle = await sm.enqueue_edit_delivery((7, -100111), "synthetic replacement text")
    item = outbound.dispatcher._events[handle.event_ids[0]]
    worker = None
    if stage != "queued":
        worker = asyncio.create_task(outbound.dispatcher.start_worker())
        await asyncio.wait_for(entered.wait(), 1)
    await outbound.dispatcher.stop(wait_seconds=0.005)
    if worker:
        await worker

    result = await handle.wait(timeout=0.1)
    assert (result.status, result.receipt, result.replacement_allowed) == ("cancelled", None, False)
    assert item.future.done()
    assert outbound.dispatcher.queue.empty()
    assert json.loads(outbound.pending.read_text()) == {}


async def test_shutdown_cancels_nondurable_delete_despite_critical_priority(outbound):
    future = await outbound.dispatcher.enqueue_delete(-100111, 7, priority=Priority.CRITICAL, return_future=True)
    item = next(iter(outbound.dispatcher._events.values()))
    await outbound.dispatcher.stop(wait_seconds=0.005)
    assert item.state == DeliveryState.CANCELLED
    assert item.replacement_allowed is False
    assert await future is None
    assert outbound.dispatcher.queue.empty()
    assert json.loads(outbound.pending.read_text()) == {}


async def test_shutdown_keeps_durable_critical_append_pending(outbound):
    handle = await sm.enqueue_log_delivery("durable incident", priority=Priority.CRITICAL)
    await outbound.dispatcher.stop(wait_seconds=0.005)
    result = await handle.wait(timeout=0.1)
    assert (result.status, result.receipt, result.replacement_allowed) == ("pending", None, False)
    rows = json.loads(outbound.pending.read_text())
    assert tuple(rows) == handle.event_ids
    assert rows[handle.event_ids[0]]["status"] == "pending"


@pytest.mark.parametrize("ambiguous", [False, True])
async def test_shutdown_preserves_completed_append_outcome(outbound, ambiguous):
    if ambiguous:
        outbound.bot.send_message.side_effect = TimedOut("synthetic ambiguous acknowledgement")
    handle = await sm.enqueue_log_delivery("owned outcome")
    worker = asyncio.create_task(outbound.dispatcher.start_worker())
    before = await handle.wait(timeout=1)
    assert before.status == ("uncertain" if ambiguous else "delivered")
    await outbound.dispatcher.stop(wait_seconds=0.005)
    await worker
    assert await handle.wait(timeout=0.1) == before
