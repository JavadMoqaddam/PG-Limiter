"""Delivery-coupled batch deduplication through the real dispatcher."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram.error import BadRequest

import telegram_bot.dispatcher as dm
import telegram_bot.send_message as sm
from telegram_bot.dispatcher import TelegramDispatcher
from telegram_bot.topics import TopicType


@pytest.fixture
async def sending(tmp_path, monkeypatch):
    monkeypatch.setattr(dm, "PENDING_NOTIFICATIONS_FILE", str(tmp_path / "pending.json"))
    topics = MagicMock()
    topics.group_id = -1001234567890
    topics.enabled = True
    topics.monitoring_revision = 0
    topics.destination_revision = 0
    topics.get_topic_id.return_value = 5
    topics.is_message_sent.return_value = False
    topics.mark_message_sent = AsyncMock()
    monkeypatch.setattr(sm, "get_topics_manager", lambda: topics)
    monkeypatch.setattr(dm, "get_topics_manager", lambda: topics)
    dispatcher = TelegramDispatcher()
    monkeypatch.setattr(sm, "get_dispatcher", lambda: dispatcher)
    bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=123)))
    dispatcher.set_bot(bot)
    yield dispatcher, topics, bot
    await dispatcher.stop(wait_seconds=0.1)


async def _drain(dispatcher, worker):
    await asyncio.sleep(0)
    await asyncio.wait_for(dispatcher.queue.join(), timeout=1)
    await dispatcher.stop(wait_seconds=0.1)
    await worker


async def test_success_marks_key_after_delivery(sending):
    dispatcher, topics, bot = sending
    entered = asyncio.Event()
    release = asyncio.Event()

    async def send(**_kwargs):
        entered.set()
        await release.wait()
        return SimpleNamespace(message_id=123)

    bot.send_message.side_effect = send
    await sm.send_logs("hello", topic_type=TopicType.NO_LIMIT, message_key="no_limit:u")
    topics.mark_message_sent.assert_not_awaited()
    worker = asyncio.create_task(dispatcher.start_worker())
    await asyncio.wait_for(entered.wait(), timeout=1)
    topics.mark_message_sent.assert_not_awaited()
    release.set()
    await _drain(dispatcher, worker)
    topics.mark_message_sent.assert_awaited_once_with(
        TopicType.NO_LIMIT,
        "no_limit:u",
        chat_id=-1001234567890,
        message_thread_id=5,
        monitoring_revision=0,
        destination_revision=0,
    )


async def test_failed_delivery_does_not_mark_dedup_key(sending):
    dispatcher, topics, bot = sending
    bot.send_message.side_effect = BadRequest("Can't parse entities")
    await sm.send_logs("hello", topic_type=TopicType.NO_LIMIT, message_key="no_limit:u")
    worker = asyncio.create_task(dispatcher.start_worker())
    await _drain(dispatcher, worker)
    topics.mark_message_sent.assert_not_awaited()


async def test_split_message_marks_only_after_all_chunks_succeed(sending):
    dispatcher, topics, bot = sending
    calls = 0

    async def send(**_kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise BadRequest("Can't parse entities")
        return SimpleNamespace(message_id=calls)

    bot.send_message.side_effect = send
    await sm.send_logs("A" * 8000, topic_type=TopicType.WARNINGS, message_key="warn:batch")
    worker = asyncio.create_task(dispatcher.start_worker())
    await _drain(dispatcher, worker)
    assert calls == 3
    topics.mark_message_sent.assert_not_awaited()


async def test_dedup_reservation_is_retained_until_complete(sending):
    dispatcher, topics, bot = sending
    await sm.send_logs("hi", topic_type=TopicType.NO_LIMIT, message_key="k")
    await sm.send_logs("hi", topic_type=TopicType.NO_LIMIT, message_key="k")
    assert dispatcher.queue.qsize() == 1
    topics.mark_message_sent.assert_not_awaited()
    worker = asyncio.create_task(dispatcher.start_worker())
    await _drain(dispatcher, worker)
    assert bot.send_message.await_count == 1
    topics.mark_message_sent.assert_awaited_once_with(
        TopicType.NO_LIMIT,
        "k",
        chat_id=-1001234567890,
        message_thread_id=5,
        monitoring_revision=0,
        destination_revision=0,
    )
