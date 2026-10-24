"""Outbound events keep their destination, cancellation and delivery receipt owner."""

import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.error import BadRequest, Forbidden

import telegram_bot.dispatcher as dm
import telegram_bot.send_message as sm
from telegram_bot.dispatcher import Priority, TelegramDispatcher
from telegram_bot.topics import TopicType


class Topics:
    """Test-owned forum settings with destination-scoped delivered keys."""

    def __init__(self):
        self.group_id = -100111
        self.enabled = True
        self.threads = {TopicType.GENERAL: 10, TopicType.DISABLE_ENABLE: 20, TopicType.NO_LIMIT: 30}
        self.delivered = set()

    def get_topic_id(self, topic):
        return self.threads.get(topic)

    def is_message_sent(self, topic, key):
        return (self.group_id, topic, key) in self.delivered

    async def mark_message_sent(self, topic, key):
        self.delivered.add((self.group_id, topic, key))

    async def set_enabled(self, value):
        self.enabled = value


@pytest.fixture
async def outbound(tmp_path, monkeypatch):
    pending = tmp_path / "pending.json"
    tracking = tmp_path / "receipts.json"
    topics = Topics()
    monkeypatch.setattr(dm, "PENDING_NOTIFICATIONS_FILE", str(pending))
    monkeypatch.setattr(sm, "DISABLE_MESSAGES_FILE", str(tracking))
    monkeypatch.setattr(sm, "_disable_msg_lock", asyncio.Lock())
    monkeypatch.setattr(dm, "get_topics_manager", lambda: topics)
    monkeypatch.setattr(sm, "get_topics_manager", lambda: topics)
    dispatcher = TelegramDispatcher()
    monkeypatch.setattr(sm, "get_dispatcher", lambda: dispatcher)
    sent = []
    deleted = []

    async def send(**kwargs):
        sent.append(kwargs)
        return SimpleNamespace(message_id=len(sent))

    async def delete(**kwargs):
        deleted.append(kwargs)
        return True

    bot = SimpleNamespace(send_message=AsyncMock(side_effect=send), delete_message=AsyncMock(side_effect=delete))
    dispatcher.set_bot(bot)
    yield SimpleNamespace(
        dispatcher=dispatcher, topics=topics, bot=bot, sent=sent, deleted=deleted, pending=pending, tracking=tracking
    )
    await dispatcher.stop(wait_seconds=0.05)
    for task in list(getattr(sm, "_pending_dedup_tasks", ())):
        task.cancel()
    await asyncio.sleep(0)


async def _finish_worker(dispatcher, worker):
    # Let start_worker register itself and reload persisted items before join.
    await asyncio.sleep(0)
    await asyncio.wait_for(dispatcher.queue.join(), timeout=1)
    await dispatcher.stop(wait_seconds=0.1)
    await asyncio.wait_for(worker, timeout=1)


@pytest.mark.parametrize("wait_stage", ["token", "pause"])
async def test_cancellation_during_wait_prevents_bot_send(outbound, monkeypatch, wait_stage):
    dispatcher = outbound.dispatcher
    waiting = asyncio.Event()
    release = asyncio.Event()

    async def wait_for_token(*_args):
        waiting.set()
        await release.wait()

    if wait_stage == "token":
        monkeypatch.setattr(dispatcher.bucket, "consume", wait_for_token)
    else:
        original_sleep = asyncio.sleep

        async def pause(seconds):
            if seconds > 10:
                waiting.set()
                await release.wait()
            else:
                await original_sleep(seconds)

        monkeypatch.setattr(dm.asyncio, "sleep", pause)
        dispatcher._paused_until = time.monotonic() + 100

    future = await dispatcher.enqueue_send(
        "ban", priority=Priority.CRITICAL, cancel_key="disable:alice", return_future=True
    )
    worker = asyncio.create_task(dispatcher.start_worker())
    try:
        await asyncio.wait_for(waiting.wait(), timeout=1)
        assert await dispatcher.cancel_pending("disable:alice") is True
        release.set()
        await _finish_worker(dispatcher, worker)
        assert await future is None
        outbound.bot.send_message.assert_not_awaited()
    finally:
        release.set()


async def test_old_completion_leaves_new_event_with_same_key_cancellable(outbound, monkeypatch):
    dispatcher = outbound.dispatcher
    first_sending = asyncio.Event()
    first_release = asyncio.Event()
    second_preparing = asyncio.Event()
    second_release = asyncio.Event()
    original_send = outbound.bot.send_message.side_effect

    async def first_send(**kwargs):
        first_sending.set()
        await first_release.wait()
        return await original_send(**kwargs)

    outbound.bot.send_message.side_effect = first_send
    consumes = 0

    async def consume(*_args):
        nonlocal consumes
        consumes += 1
        if consumes == 2:
            second_preparing.set()
            await second_release.wait()

    monkeypatch.setattr(dispatcher.bucket, "consume", consume)
    first = await dispatcher.enqueue_send(
        "first", priority=Priority.CRITICAL, cancel_key="disable:alice", return_future=True
    )
    worker = asyncio.create_task(dispatcher.start_worker())
    try:
        await asyncio.wait_for(first_sending.wait(), timeout=1)
        second = await dispatcher.enqueue_send(
            "second", priority=Priority.CRITICAL, cancel_key="disable:alice", return_future=True
        )
        first_release.set()
        assert await asyncio.wait_for(asyncio.shield(first), timeout=1) == (1, -100111)
        await asyncio.wait_for(second_preparing.wait(), timeout=1)
        assert await dispatcher.cancel_pending("disable:alice") is True
        second_release.set()
        await _finish_worker(dispatcher, worker)
        assert await second is None
        assert [call["text"] for call in outbound.sent] == ["first"]
    finally:
        first_release.set()
        second_release.set()


async def test_destination_is_pinned_before_persistence_await(outbound, monkeypatch):
    dispatcher = outbound.dispatcher
    persisting = asyncio.Event()
    release = asyncio.Event()
    original_save = dispatcher._save_pending_storage

    async def save():
        persisting.set()
        await release.wait()
        await original_save()

    monkeypatch.setattr(dispatcher, "_save_pending_storage", save)
    enqueue = asyncio.create_task(
        dispatcher.enqueue_send(
            "original forum", topic_type=TopicType.DISABLE_ENABLE, priority=Priority.CRITICAL, return_future=True
        )
    )
    await asyncio.wait_for(persisting.wait(), timeout=1)
    outbound.topics.group_id = -100222
    outbound.topics.threads[TopicType.DISABLE_ENABLE] = 99
    release.set()
    future = await enqueue
    worker = asyncio.create_task(dispatcher.start_worker())
    await _finish_worker(dispatcher, worker)
    assert await future == (1, -100111)
    assert outbound.sent[0]["chat_id"] == -100111
    assert outbound.sent[0]["message_thread_id"] == 20


async def test_restart_retains_original_group_and_thread(outbound):
    await outbound.dispatcher.enqueue_send("saved", topic_type=TopicType.DISABLE_ENABLE, priority=Priority.CRITICAL)
    outbound.topics.group_id = -100222
    outbound.topics.threads[TopicType.DISABLE_ENABLE] = 99
    reloaded = TelegramDispatcher()
    reloaded.set_bot(outbound.bot)
    worker = asyncio.create_task(reloaded.start_worker())
    await _finish_worker(reloaded, worker)
    assert outbound.sent[0]["chat_id"] == -100111
    assert outbound.sent[0]["message_thread_id"] == 20


@pytest.mark.parametrize(
    "error", [BadRequest("Message thread not found"), BadRequest("Chat not found"), Forbidden("Not enough rights")]
)
async def test_correctable_destination_failure_keeps_critical_pending(outbound, error):
    attempted = asyncio.Event()

    async def fail_send(**_kwargs):
        attempted.set()
        raise error

    outbound.bot.send_message.side_effect = fail_send
    future = await outbound.dispatcher.enqueue_send("critical", priority=Priority.CRITICAL, return_future=True)
    worker = asyncio.create_task(outbound.dispatcher.start_worker())
    await asyncio.wait_for(attempted.wait(), timeout=1)
    for _ in range(3):
        await asyncio.sleep(0)
    assert not future.done()
    assert json.loads(outbound.pending.read_text())
    await outbound.dispatcher.stop(wait_seconds=0.05)
    await asyncio.wait_for(worker, timeout=1)
    assert json.loads(outbound.pending.read_text())


async def test_invalid_payload_retains_failed_record_without_poisoning_next_send(outbound):
    original_send = outbound.bot.send_message.side_effect

    async def send(**kwargs):
        if kwargs["text"] == "invalid":
            raise BadRequest("Can't parse entities: unsupported start tag")
        return await original_send(**kwargs)

    outbound.bot.send_message.side_effect = send
    failed = await outbound.dispatcher.enqueue_send("invalid", priority=Priority.CRITICAL, return_future=True)
    healthy = await outbound.dispatcher.enqueue_send("healthy", return_future=True)
    worker = asyncio.create_task(outbound.dispatcher.start_worker())
    await _finish_worker(outbound.dispatcher, worker)
    assert await failed is None
    assert await healthy == (1, -100111)
    rows = list(json.loads(outbound.pending.read_text()).values())
    assert len(rows) == 1
    assert rows[0]["text"] == "invalid"
    assert rows[0]["status"] == "failed"
    assert "parse entities" in rows[0].get("error", "").lower()


async def test_legacy_destination_waits_then_freezes_once(outbound):
    outbound.pending.write_text(
        json.dumps(
            {"legacy": {"action_type": "send", "text": "legacy", "topic_type": "disable_enable", "created_at": 100.0}}
        )
    )
    outbound.topics.group_id = None
    reloaded = TelegramDispatcher()
    reloaded.set_bot(outbound.bot)
    await reloaded.reload_pending_critical_messages()
    assert reloaded.queue.empty()
    assert json.loads(outbound.pending.read_text())

    outbound.topics.group_id = -100111
    await reloaded.reload_pending_critical_messages()
    rows = list(json.loads(outbound.pending.read_text()).values())
    assert rows[0]["chat_id"] == -100111
    assert rows[0]["message_thread_id"] == 20
    outbound.topics.group_id = -100222
    outbound.topics.threads[TopicType.DISABLE_ENABLE] = 99
    worker = asyncio.create_task(reloaded.start_worker())
    await _finish_worker(reloaded, worker)
    assert outbound.sent[0]["chat_id"] == -100111
    assert outbound.sent[0]["message_thread_id"] == 20


async def test_cancellation_waits_for_owned_tracking_then_deletes_receipt(outbound, monkeypatch):
    tracking = asyncio.Event()
    release = asyncio.Event()
    original_track = sm.track_disable_message

    async def track(*args, **kwargs):
        tracking.set()
        await release.wait()
        await original_track(*args, **kwargs)

    monkeypatch.setattr(sm, "track_disable_message", track)
    await outbound.dispatcher.enqueue_send("ban", priority=Priority.CRITICAL, cancel_key="disable:alice")
    worker = asyncio.create_task(outbound.dispatcher.start_worker())
    deletion = None
    try:
        await asyncio.wait_for(tracking.wait(), timeout=1)
        deletion = asyncio.create_task(sm.cancel_or_delete_disable_message("alice"))
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(deletion), timeout=0.03)
        release.set()
        assert await asyncio.wait_for(deletion, timeout=1) is True
        await _finish_worker(outbound.dispatcher, worker)
        assert outbound.deleted == [{"chat_id": -100111, "message_id": 1}]
        assert await sm.get_disable_message("alice") is None
    finally:
        release.set()
        if deletion and not deletion.done():
            deletion.cancel()
            await asyncio.gather(deletion, return_exceptions=True)


@pytest.mark.parametrize("length, expected_chunks", [(5, 1), (8000, 3)])
async def test_concurrent_queued_key_reserves_all_chunks_once(outbound, length, expected_chunks):
    await asyncio.gather(
        sm.send_logs("A" * length, topic_type=TopicType.NO_LIMIT, message_key="same:key", priority=Priority.CRITICAL),
        sm.send_logs("A" * length, topic_type=TopicType.NO_LIMIT, message_key="same:key", priority=Priority.CRITICAL),
    )
    assert outbound.dispatcher.queue.qsize() == expected_chunks
    worker = asyncio.create_task(outbound.dispatcher.start_worker())
    await _finish_worker(outbound.dispatcher, worker)
    assert len(outbound.sent) == expected_chunks
    assert outbound.topics.is_message_sent(TopicType.NO_LIMIT, "same:key")


@pytest.mark.parametrize("length, expected_chunks", [(5, 1), (8000, 3)])
async def test_caller_timeout_preserves_shared_chunk_delivery(outbound, monkeypatch, length, expected_chunks):
    original_wait_for = asyncio.wait_for

    async def short_caller_wait(awaitable, timeout):
        return await original_wait_for(awaitable, 0.02 if timeout == 10.0 else timeout)

    monkeypatch.setattr(sm.asyncio, "wait_for", short_caller_wait)
    assert (
        await sm.send_logs("A" * length, return_message_id=True, topic_type=TopicType.NO_LIMIT, message_key="later")
        is None
    )
    queued = []
    while not outbound.dispatcher.queue.empty():
        entry = outbound.dispatcher.queue.get_nowait()
        outbound.dispatcher.queue.task_done()
        queued.append(entry)
        assert not entry[2].future.cancelled()
    assert len(queued) == expected_chunks
    for entry in queued:
        await outbound.dispatcher.queue.put(entry)
    worker = asyncio.create_task(outbound.dispatcher.start_worker())
    await _finish_worker(outbound.dispatcher, worker)
    assert len(outbound.sent) == expected_chunks
    assert outbound.topics.is_message_sent(TopicType.NO_LIMIT, "later")


async def test_all_delivered_events_are_deleted_for_one_disable_key(outbound):
    await outbound.dispatcher.enqueue_send("first", priority=Priority.CRITICAL, cancel_key="disable:alice")
    await outbound.dispatcher.enqueue_send("second", priority=Priority.CRITICAL, cancel_key="disable:alice")
    worker = asyncio.create_task(outbound.dispatcher.start_worker())
    await asyncio.wait_for(outbound.dispatcher.queue.join(), timeout=1)
    assert await sm.cancel_or_delete_disable_message("alice") is True
    await _finish_worker(outbound.dispatcher, worker)
    assert {row["message_id"] for row in outbound.deleted} == {1, 2}
    assert await sm.get_disable_message("alice") is None


async def test_direct_enqueue_waiter_cannot_cancel_owned_delivery(outbound):
    future = await outbound.dispatcher.enqueue_send("late", return_future=True)
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(future, timeout=0.01)
    duplicate = await outbound.dispatcher.enqueue_send_batch(["dedup"], message_key="direct")
    duplicate[0].cancel()
    shared = await outbound.dispatcher.enqueue_send_batch(["dedup"], message_key="direct")
    assert not shared[0].cancelled()
    worker = asyncio.create_task(outbound.dispatcher.start_worker())
    await _finish_worker(outbound.dispatcher, worker)
    assert [row["text"] for row in outbound.sent] == ["late", "dedup"]
    assert await shared[0] == (2, -100111)


async def test_completed_receipt_survives_failed_pending_removal(outbound, monkeypatch):
    original_write = dm.atomic_write_json
    failed = False

    def fail_removal_once(path, payload):
        nonlocal failed
        if not payload and not failed:
            failed = True
            raise OSError("cleanup write failed")
        return original_write(path, payload)

    monkeypatch.setattr(dm, "atomic_write_json", fail_removal_once)
    original_sleep = asyncio.sleep

    async def fast_retry(seconds):
        await original_sleep(0 if seconds >= 2 else seconds)

    monkeypatch.setattr(dm.asyncio, "sleep", fast_retry)
    future = await outbound.dispatcher.enqueue_send("durable receipt", priority=Priority.CRITICAL, return_future=True)
    worker = asyncio.create_task(outbound.dispatcher.start_worker())
    await _finish_worker(outbound.dispatcher, worker)
    assert await future == (1, -100111)
    assert outbound.bot.send_message.await_count == 1
    assert json.loads(outbound.pending.read_text()) == {}


async def test_failed_legacy_pin_write_keeps_the_first_resolved_destination(outbound, monkeypatch):
    outbound.pending.write_text(json.dumps({"legacy": {"text": "legacy", "topic_type": "disable_enable"}}))
    reloaded = TelegramDispatcher()
    original_write = dm.atomic_write_json

    def fail_write(*_args):
        raise OSError("pin write failed")

    monkeypatch.setattr(dm, "atomic_write_json", fail_write)
    with pytest.raises(OSError, match="pin write failed"):
        await reloaded.reload_pending_critical_messages()
    assert reloaded.queue.empty()
    outbound.topics.group_id = -100222
    outbound.topics.threads[TopicType.DISABLE_ENABLE] = 99
    monkeypatch.setattr(dm, "atomic_write_json", original_write)
    await reloaded.reload_pending_critical_messages()
    saved = json.loads(outbound.pending.read_text())["legacy"]
    assert (saved["chat_id"], saved["message_thread_id"]) == (-100111, 20)


async def test_failed_inflight_attempt_can_be_cancelled_without_waiting_for_retry(outbound):
    entered = asyncio.Event()
    release = asyncio.Event()

    async def fail_send(**_kwargs):
        entered.set()
        await release.wait()
        raise Forbidden("temporary permission failure")

    outbound.bot.send_message.side_effect = fail_send
    await outbound.dispatcher.enqueue_send("ban", priority=Priority.CRITICAL, cancel_key="disable:alice")
    worker = asyncio.create_task(outbound.dispatcher.start_worker())
    await asyncio.wait_for(entered.wait(), timeout=1)
    cancellation = asyncio.create_task(outbound.dispatcher.cancel_pending("disable:alice"))
    await asyncio.sleep(0)
    release.set()
    assert await asyncio.wait_for(cancellation, timeout=1) is True
    await outbound.dispatcher.stop(wait_seconds=0.05)
    await worker
    assert json.loads(outbound.pending.read_text()) == {}


async def test_caller_cancellation_during_durable_admission_preserves_ownership(outbound, monkeypatch):
    entered = asyncio.Event()
    release = asyncio.Event()
    original_save = outbound.dispatcher._save_pending_storage

    async def save():
        entered.set()
        await release.wait()
        await original_save()

    monkeypatch.setattr(outbound.dispatcher, "_save_pending_storage", save)
    caller = asyncio.create_task(sm.send_logs("accepted", priority=Priority.CRITICAL))
    await asyncio.wait_for(entered.wait(), timeout=1)
    caller.cancel()
    await asyncio.sleep(0)
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await caller
    assert outbound.dispatcher.queue.qsize() == 1
    assert json.loads(outbound.pending.read_text())
    worker = asyncio.create_task(outbound.dispatcher.start_worker())
    await _finish_worker(outbound.dispatcher, worker)
    assert [row["text"] for row in outbound.sent] == ["accepted"]


async def test_unreadable_receipt_store_is_not_overwritten_as_empty(outbound):
    original = "{incomplete receipt file"
    outbound.tracking.write_text(original)
    with pytest.raises(json.JSONDecodeError):
        await sm.track_disable_message("alice", 5, -100111)
    assert outbound.tracking.read_text() == original


async def test_cancel_key_cancels_every_queued_event(outbound):
    first = await outbound.dispatcher.enqueue_send(
        "first", priority=Priority.CRITICAL, cancel_key="disable:alice", return_future=True
    )
    second = await outbound.dispatcher.enqueue_send(
        "second", priority=Priority.CRITICAL, cancel_key="disable:alice", return_future=True
    )
    assert await outbound.dispatcher.cancel_pending("disable:alice") is True
    assert first.done() and first.result() is None
    assert second.done() and second.result() is None
    worker = asyncio.create_task(outbound.dispatcher.start_worker())
    await _finish_worker(outbound.dispatcher, worker)
    outbound.bot.send_message.assert_not_awaited()
    assert json.loads(outbound.pending.read_text()) == {}


async def test_started_send_is_awaited_then_its_receipt_is_deleted(outbound):
    sending = asyncio.Event()
    release = asyncio.Event()
    original_send = outbound.bot.send_message.side_effect

    async def send(**kwargs):
        sending.set()
        await release.wait()
        return await original_send(**kwargs)

    outbound.bot.send_message.side_effect = send
    await outbound.dispatcher.enqueue_send("ban", priority=Priority.CRITICAL, cancel_key="disable:alice")
    worker = asyncio.create_task(outbound.dispatcher.start_worker())
    cancellation = None
    try:
        await asyncio.wait_for(sending.wait(), timeout=1)
        cancellation = asyncio.create_task(sm.cancel_or_delete_disable_message("alice"))
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(cancellation), timeout=0.03)
        release.set()
        assert await asyncio.wait_for(cancellation, timeout=1) is True
        await _finish_worker(outbound.dispatcher, worker)
        assert len(outbound.sent) == 1
        assert outbound.deleted == [{"chat_id": -100111, "message_id": 1}]
        assert await sm.get_disable_message("alice") is None
    finally:
        release.set()
        if cancellation and not cancellation.done():
            cancellation.cancel()
            await asyncio.gather(cancellation, return_exceptions=True)


async def test_caller_cancellation_preserves_delivery_future(outbound):
    caller = asyncio.create_task(
        sm.send_logs("later", return_message_id=True, topic_type=TopicType.NO_LIMIT, message_key="later")
    )

    async def wait_until_queued():
        while outbound.dispatcher.queue.empty():
            await asyncio.sleep(0)

    await asyncio.wait_for(wait_until_queued(), timeout=1)
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    entry = outbound.dispatcher.queue.get_nowait()
    outbound.dispatcher.queue.task_done()
    assert not entry[2].future.cancelled()
    await outbound.dispatcher.queue.put(entry)
    worker = asyncio.create_task(outbound.dispatcher.start_worker())
    await _finish_worker(outbound.dispatcher, worker)
    assert outbound.topics.is_message_sent(TopicType.NO_LIMIT, "later")


async def test_failed_durable_payload_is_not_replayed_on_restart(outbound):
    outbound.pending.write_text(
        json.dumps(
            {
                "invalid": {
                    "action_type": "send",
                    "text": "invalid",
                    "topic_type": "disable_enable",
                    "chat_id": -100111,
                    "message_thread_id": 20,
                    "status": "failed",
                    "error": "Can't parse entities",
                }
            }
        )
    )
    reloaded = TelegramDispatcher()
    reloaded.set_bot(outbound.bot)
    await reloaded.reload_pending_critical_messages()
    assert reloaded.queue.empty()
    assert list(json.loads(outbound.pending.read_text()).values())[0]["status"] == "failed"


async def test_persisted_receipt_finishes_tracking_without_resending(outbound):
    outbound.pending.write_text(
        json.dumps(
            {
                "delivered": {
                    "action_type": "send",
                    "text": "already sent",
                    "topic_type": "disable_enable",
                    "chat_id": -100111,
                    "message_thread_id": 20,
                    "cancel_key": "disable:alice",
                    "status": "delivered",
                    "receipt": [77, -100111],
                }
            }
        )
    )
    reloaded = TelegramDispatcher()
    reloaded.set_bot(outbound.bot)
    worker = asyncio.create_task(reloaded.start_worker())
    await _finish_worker(reloaded, worker)
    outbound.bot.send_message.assert_not_awaited()
    assert await sm.get_disable_message("alice") == (77, -100111)
    assert json.loads(outbound.pending.read_text()) == {}


async def test_tracking_error_retries_receipt_without_resending(outbound, monkeypatch):
    original_track = sm.track_disable_message
    recorded = asyncio.Event()
    attempts = 0

    async def track(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("tracking disk temporarily unavailable")
        await original_track(*args, **kwargs)
        recorded.set()

    original_sleep = asyncio.sleep

    async def skip_retry_delay(seconds):
        await original_sleep(0 if seconds >= 1 else seconds)

    monkeypatch.setattr(sm, "track_disable_message", track)
    monkeypatch.setattr(dm.asyncio, "sleep", skip_retry_delay)
    future = await outbound.dispatcher.enqueue_send(
        "ban", priority=Priority.CRITICAL, cancel_key="disable:alice", return_future=True
    )
    worker = asyncio.create_task(outbound.dispatcher.start_worker())
    await asyncio.wait_for(recorded.wait(), timeout=1)
    assert await asyncio.wait_for(asyncio.shield(future), timeout=1) == (1, -100111)
    await _finish_worker(outbound.dispatcher, worker)
    assert len(outbound.sent) == 1
    assert await sm.get_disable_message("alice") == (1, -100111)


async def test_expired_batch_releases_key_for_a_later_legitimate_send(outbound):
    await sm.send_logs(
        "A" * 8000, topic_type=TopicType.WARNINGS, message_key="batch", priority=Priority.WARNINGS, ttl=0.001
    )
    await asyncio.sleep(0.005)
    worker = asyncio.create_task(outbound.dispatcher.start_worker())
    await asyncio.wait_for(outbound.dispatcher.queue.join(), timeout=1)
    assert outbound.sent == []
    assert not outbound.topics.is_message_sent(TopicType.WARNINGS, "batch")
    await sm.send_logs("A" * 8000, topic_type=TopicType.WARNINGS, message_key="batch", priority=Priority.WARNINGS)
    await _finish_worker(outbound.dispatcher, worker)
    assert len(outbound.sent) == 3
    assert outbound.topics.is_message_sent(TopicType.WARNINGS, "batch")


async def test_retry_retains_original_destination_after_group_change(outbound, monkeypatch):
    backoff = asyncio.Event()
    release = asyncio.Event()
    original_sleep = asyncio.sleep
    original_send = outbound.bot.send_message.side_effect
    failed = False

    async def pause(seconds):
        if seconds >= 1:
            backoff.set()
            await release.wait()
        else:
            await original_sleep(seconds)

    async def send(**kwargs):
        nonlocal failed
        if not failed:
            failed = True
            raise Forbidden("Temporary topic permission failure")
        return await original_send(**kwargs)

    monkeypatch.setattr(dm.asyncio, "sleep", pause)
    outbound.bot.send_message.side_effect = send
    future = await outbound.dispatcher.enqueue_send(
        "ban", topic_type=TopicType.DISABLE_ENABLE, priority=Priority.CRITICAL, return_future=True
    )
    worker = asyncio.create_task(outbound.dispatcher.start_worker())
    try:
        await asyncio.wait_for(backoff.wait(), timeout=1)
        outbound.topics.group_id = -100222
        outbound.topics.threads[TopicType.DISABLE_ENABLE] = 99
        release.set()
        assert await asyncio.wait_for(asyncio.shield(future), timeout=1) == (1, -100111)
        await _finish_worker(outbound.dispatcher, worker)
        assert len(outbound.bot.send_message.call_args_list) == 2
        assert all(call.kwargs["chat_id"] == -100111 for call in outbound.bot.send_message.call_args_list)
        assert all(call.kwargs["message_thread_id"] == 20 for call in outbound.bot.send_message.call_args_list)
    finally:
        release.set()


async def test_deleting_old_receipt_does_not_remove_new_tracking(outbound, monkeypatch):
    await sm.track_disable_message("alice", 1, -100111)
    deleting = asyncio.Event()
    release = asyncio.Event()

    async def delete(_message_info):
        deleting.set()
        await release.wait()
        return True

    monkeypatch.setattr(sm, "delete_message", delete)
    cleanup = asyncio.create_task(sm.cancel_or_delete_disable_message("alice"))
    try:
        await asyncio.wait_for(deleting.wait(), timeout=1)
        await sm.track_disable_message("alice", 2, -100111)
        release.set()
        assert await cleanup is True
        assert await sm.get_disable_message("alice") == (2, -100111)
    finally:
        release.set()


@pytest.mark.parametrize("admission_succeeds", [False, True])
async def test_reload_does_not_publish_inflight_critical_admission(outbound, monkeypatch, admission_succeeds):
    dispatcher = outbound.dispatcher
    entered, release = asyncio.Event(), asyncio.Event()
    original_save = dispatcher._save_pending_storage
    writes = 0

    async def save():
        nonlocal writes
        writes += 1
        if writes == 1:
            entered.set()
            await release.wait()
            if not admission_succeeds:
                raise OSError("initial admission storage failure")
        await original_save()

    monkeypatch.setattr(dispatcher, "_save_pending_storage", save)
    admission = asyncio.create_task(
        dispatcher.enqueue_send("must await admission", priority=Priority.CRITICAL, cancel_key="disable:alice")
    )
    worker = None
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        await dispatcher.reload_pending_critical_messages()
        assert dispatcher.queue.empty(), "Reload cannot queue an uncommitted critical admission"
        outbound.bot.send_message.assert_not_awaited()
        worker = asyncio.create_task(dispatcher.start_worker())
        await asyncio.sleep(0)
        outbound.bot.send_message.assert_not_awaited()
    finally:
        release.set()
        if admission_succeeds:
            await admission
        else:
            with pytest.raises(OSError, match="initial admission storage failure"):
                await admission
        if worker:
            await _finish_worker(dispatcher, worker)
    if admission_succeeds:
        assert [row["text"] for row in outbound.sent] == ["must await admission"]
    else:
        await dispatcher.reload_pending_critical_messages()
        assert dispatcher.queue.empty()
        outbound.bot.send_message.assert_not_awaited()


@pytest.mark.parametrize("checkpoint_chunk", [0, 1])
async def test_interrupted_batch_cancellation_releases_reservation_after_restart(
    outbound, monkeypatch, checkpoint_chunk
):
    dispatcher = outbound.dispatcher
    await dispatcher.enqueue_send_batch(
        ["part0", "part1"], priority=Priority.CRITICAL, cancel_key="disable:alice", message_key="batch-key"
    )
    persisted, release = asyncio.Event(), asyncio.Event()
    original_save = dispatcher._save_pending_storage
    calls = 0

    async def save():
        nonlocal calls
        calls += 1
        await original_save()
        if calls == checkpoint_chunk + 1:
            persisted.set()
            await release.wait()

    monkeypatch.setattr(dispatcher, "_save_pending_storage", save)
    cancellation = asyncio.create_task(dispatcher.cancel_pending("disable:alice"))
    reloaded, worker = None, None
    try:
        await asyncio.wait_for(persisted.wait(), timeout=1)
        # Restart reads the real checkpoint while the original cancellation is
        # frozen immediately after the selected durable chunk transition.
        snapshot = json.loads(outbound.pending.read_text())
        reloaded = TelegramDispatcher()
        reloaded.set_bot(outbound.bot)
        await reloaded.reload_pending_critical_messages()
        worker = asyncio.create_task(reloaded.start_worker())
        await asyncio.sleep(0)
        await asyncio.wait_for(reloaded.queue.join(), timeout=1)
        # Freeze the recovered worker after settlement so the later admission's
        # complete queue can be inspected without racing another Bot API call.
        worker.cancel()
        await worker
        assert not outbound.topics.is_message_sent(TopicType.GENERAL, "batch-key")
        duplicate = await reloaded.enqueue_send_batch(
            ["part0", "part1"], priority=Priority.CRITICAL, message_key="batch-key"
        )
        assert len(duplicate) == 2, "A cancelled batch cannot retain an incomplete dedup reservation after restart"
        assert reloaded.queue.qsize() == 2
        cancelled_rows = [row for row in snapshot.values() if row["status"] == "cancelled"]
        assert len(cancelled_rows) == checkpoint_chunk + 1
        assert sorted(row["chunk_index"] for row in cancelled_rows) == list(range(checkpoint_chunk + 1))
        assert len(snapshot) == 2
    finally:
        if worker:
            await reloaded.stop(wait_seconds=0.05)
            await worker
        release.set()
        await cancellation


async def test_known_absent_old_receipt_does_not_block_newer_cleanup(outbound):
    await sm.track_disable_message("alice", 1, -100111, event_id="old")
    await sm.track_disable_message("alice", 2, -100111, event_id="new")
    attempts = []

    async def delete(**kwargs):
        attempts.append(kwargs["message_id"])
        if kwargs["message_id"] == 1:
            raise BadRequest("Message to delete not found")
        return True

    outbound.bot.delete_message.side_effect = delete
    worker = asyncio.create_task(outbound.dispatcher.start_worker())
    try:
        result = await asyncio.wait_for(sm.cancel_or_delete_disable_message("alice"), timeout=1)
        assert attempts == [1, 2], "Known absence must not prevent later receipt deletion"
        assert result is True
        assert await sm.get_disable_messages("alice") == []
    finally:
        await outbound.dispatcher.stop(wait_seconds=0.05)
        await worker


async def test_failed_old_receipt_preserves_only_failed_and_new_ownership(outbound, monkeypatch):
    await sm.track_disable_message("alice", 1, -100111, event_id="failed")
    await sm.track_disable_message("alice", 2, -100111, event_id="deleted")
    attempts = []

    async def delete(receipt):
        attempts.append(receipt[0])
        if receipt[0] == 1:
            return False
        await sm.track_disable_message("alice", 3, -100111, event_id="new-during-delete")
        return True

    monkeypatch.setattr(sm, "delete_message", delete)
    assert await sm.cancel_or_delete_disable_message("alice") is False
    assert attempts == [1, 2], "A genuine failure must not abandon later delivered receipts"
    assert await sm.get_disable_messages("alice") == [(1, -100111), (3, -100111)]


async def test_other_critical_write_does_not_publish_failed_admission(outbound, monkeypatch):
    dispatcher = outbound.dispatcher
    entered, release = asyncio.Event(), asyncio.Event()
    original_save = dispatcher._save_pending_storage
    writes = 0

    async def save():
        nonlocal writes
        writes += 1
        if writes == 1:
            entered.set()
            await release.wait()
            raise OSError("initial admission storage failure")
        await original_save()

    monkeypatch.setattr(dispatcher, "_save_pending_storage", save)
    admission = asyncio.create_task(dispatcher.enqueue_send("rejected", priority=Priority.CRITICAL))
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        await dispatcher.enqueue_send("accepted", priority=Priority.CRITICAL)
        rows = json.loads(outbound.pending.read_text())
        assert [row["text"] for row in rows.values()] == ["accepted"]
    finally:
        release.set()
        with pytest.raises(OSError, match="initial admission storage failure"):
            await admission
    reloaded = TelegramDispatcher()
    reloaded.set_bot(outbound.bot)
    worker = asyncio.create_task(reloaded.start_worker())
    await _finish_worker(reloaded, worker)
    assert [row["text"] for row in outbound.sent] == ["accepted"]


async def test_reload_settles_cancelled_and_failed_batch_without_delivery_claim(outbound):
    await outbound.dispatcher.enqueue_send_batch(
        ["cancelled", "invalid"], priority=Priority.CRITICAL, message_key="batch-key"
    )
    rows = json.loads(outbound.pending.read_text())
    for row in rows.values():
        row["status"] = "cancelled" if row["chunk_index"] == 0 else "failed"
    outbound.pending.write_text(json.dumps(rows))
    reloaded = TelegramDispatcher()
    await reloaded.reload_pending_critical_messages()
    assert reloaded.queue.empty()
    assert not outbound.topics.is_message_sent(TopicType.GENERAL, "batch-key")
    retained = json.loads(outbound.pending.read_text())
    assert [row["status"] for row in retained.values()] == ["failed"]
    await reloaded.enqueue_send_batch(["new0", "new1"], priority=Priority.CRITICAL, message_key="batch-key")
    assert reloaded.queue.qsize() == 2
    await reloaded.stop(wait_seconds=0.05)
