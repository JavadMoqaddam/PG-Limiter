"""Delivery-coupled deduplication tests for send_logs.

A dedup key must be marked as sent only once the message is actually delivered,
so a failed/expired/lost send does not suppress the next legitimate attempt.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import telegram_bot.send_message as sm
from telegram_bot.topics import TopicType


def _fake_topics_manager():
    tm = MagicMock()
    tm.group_id = -1001234567890
    tm.enabled = True
    tm.is_message_sent.return_value = False
    tm.set_enabled = AsyncMock()
    tm.mark_message_sent = AsyncMock()
    return tm


async def _settle():
    # Let any background marking task observe the resolved futures.
    for _ in range(3):
        await asyncio.sleep(0)


async def test_success_marks_key_after_delivery(monkeypatch):
    tm = _fake_topics_manager()
    monkeypatch.setattr(sm, "get_topics_manager", lambda: tm)

    loop = asyncio.get_running_loop()
    delivered = loop.create_future()
    dispatcher = MagicMock()

    async def fake_enqueue(**kwargs):
        return delivered

    dispatcher.enqueue_send = fake_enqueue
    monkeypatch.setattr(sm, "get_dispatcher", lambda: dispatcher)

    await sm.send_logs("hello", topic_type=TopicType.NO_LIMIT, message_key="no_limit:u")

    # Delivery has not happened yet, so nothing may be marked.
    tm.mark_message_sent.assert_not_called()

    delivered.set_result((123, -1001234567890))
    await _settle()
    tm.mark_message_sent.assert_awaited_once_with(TopicType.NO_LIMIT, "no_limit:u")


async def test_failed_delivery_does_not_mark_dedup_key(monkeypatch):
    tm = _fake_topics_manager()
    monkeypatch.setattr(sm, "get_topics_manager", lambda: tm)

    loop = asyncio.get_running_loop()
    dropped = loop.create_future()
    dispatcher = MagicMock()

    async def fake_enqueue(**kwargs):
        return dropped

    dispatcher.enqueue_send = fake_enqueue
    monkeypatch.setattr(sm, "get_dispatcher", lambda: dispatcher)

    await sm.send_logs("hello", topic_type=TopicType.NO_LIMIT, message_key="no_limit:u")

    # Dispatcher reports the message was not delivered (None).
    dropped.set_result(None)
    await _settle()
    tm.mark_message_sent.assert_not_called()


async def test_split_message_marks_only_after_all_chunks_succeed(monkeypatch):
    tm = _fake_topics_manager()
    monkeypatch.setattr(sm, "get_topics_manager", lambda: tm)

    loop = asyncio.get_running_loop()
    futures = []
    dispatcher = MagicMock()

    async def fake_enqueue(**kwargs):
        fut = loop.create_future()
        futures.append(fut)
        return fut

    dispatcher.enqueue_send = fake_enqueue
    monkeypatch.setattr(sm, "get_dispatcher", lambda: dispatcher)

    long_msg = "A" * 8000  # exceeds the 3900 split threshold -> multiple chunks
    await sm.send_logs(long_msg, topic_type=TopicType.WARNINGS, message_key="warn:batch")

    assert len(futures) >= 2

    # One chunk fails to deliver; the rest succeed.
    futures[0].set_result((1, -1001234567890))
    futures[1].set_result(None)
    for fut in futures[2:]:
        fut.set_result((2, -1001234567890))

    await _settle()
    tm.mark_message_sent.assert_not_called()
