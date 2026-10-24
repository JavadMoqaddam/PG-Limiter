"""Forum configuration changes preserve destination ownership and durable state."""

import asyncio
import json
import sys
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import telegram_bot.topics as tm
import telegram_bot.dispatcher as dm
import telegram_bot.send_message as sm
from telegram_bot.dispatcher import Priority, TelegramDispatcher
from telegram_bot.topics import TopicType, TopicsManager


@pytest.fixture
def forum(tmp_path, monkeypatch):
    config = tmp_path / "topics.json"
    cache = tmp_path / "topic_message_cache.json"
    config.write_text(json.dumps({"enabled": True, "group_id": -100111, "topics": {"general": 10, "no_limit": 30}}))
    cache.write_text(json.dumps({"no_limit": {"no_limit:alice": tm.time.time()}}))
    monkeypatch.setattr(tm, "TOPICS_FILE", str(config))
    monkeypatch.setattr(tm, "MESSAGE_CACHE_FILE", str(cache))
    manager = TopicsManager()
    return SimpleNamespace(manager=manager, config=config, cache=cache)


def assert_original_destination(manager):
    assert manager.group_id == -100111
    assert manager.enabled is True
    assert manager.get_all_topics() == {"general": 10, "no_limit": 30}
    assert manager.is_message_sent(TopicType.NO_LIMIT, "no_limit:alice")


@pytest.mark.parametrize("invalid_kind", ["not_forum", "not_admin", "cannot_manage", "not_found"])
async def test_rejected_group_keeps_original_destination(forum, monkeypatch, invalid_kind):
    import telegram_bot.handlers.topics_settings as handlers

    before = forum.config.read_bytes()
    observed = []

    async def get_chat(group_id):
        observed.append(group_id)
        assert_original_destination(forum.manager)
        if invalid_kind == "not_found":
            raise RuntimeError("<unavailable>")
        return SimpleNamespace(is_forum=invalid_kind != "not_forum", title="<new group>")

    bot = SimpleNamespace(
        id=42,
        get_chat=AsyncMock(side_effect=get_chat),
        get_chat_member=AsyncMock(
            return_value=SimpleNamespace(
                status="member" if invalid_kind == "not_admin" else "administrator",
                can_manage_topics=invalid_kind != "cannot_manage",
            )
        ),
    )
    monkeypatch.setitem(sys.modules, "telegram_bot.main", SimpleNamespace(application=SimpleNamespace(bot=bot)))
    monkeypatch.setattr(handlers, "check_admin_privilege", AsyncMock(return_value=None))
    monkeypatch.setattr(handlers, "get_topics_manager", lambda: forum.manager)
    monkeypatch.setattr(handlers, "topics_menu", AsyncMock())
    update = SimpleNamespace(message=SimpleNamespace(text="-100222", reply_html=AsyncMock()))
    await handlers.topics_set_group_receive(update, SimpleNamespace(user_data={}))
    assert_original_destination(forum.manager)
    assert forum.config.read_bytes() == before
    assert observed == [-100222]
    assert "Successfully" not in update.message.reply_html.await_args.args[0]
    assert "<unavailable>" not in update.message.reply_html.await_args.args[0]


async def test_group_switch_clears_threads_and_cache_durably(forum):
    await forum.manager.set_group_id(-100222)
    assert forum.manager.group_id == -100222
    assert forum.manager.enabled is True
    assert forum.manager.get_all_topics() == {}
    assert not forum.manager.is_message_sent(TopicType.NO_LIMIT, "no_limit:alice")
    restored = TopicsManager()
    assert restored.group_id == -100222
    assert restored.get_all_topics() == {}
    assert not restored.is_message_sent(TopicType.NO_LIMIT, "no_limit:alice")


async def test_same_group_preserves_topics_and_cache_without_write(forum, monkeypatch):
    def fail_write(*_args, **_kwargs):
        raise AssertionError("Idempotent group setup should not write")

    monkeypatch.setattr(tm, "atomic_write_json", fail_write)
    await forum.manager.set_group_id(-100111)
    assert_original_destination(forum.manager)


async def test_failed_group_write_preserves_live_and_persisted_destination(forum, monkeypatch):
    before = forum.config.read_bytes()

    def fail_write(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(tm, "atomic_write_json", fail_write)
    with pytest.raises(OSError, match="disk full"):
        await forum.manager.set_group_id(-100222)
    assert_original_destination(forum.manager)
    assert forum.config.read_bytes() == before


async def test_group_publish_waits_for_write_and_serializes_following_changes(forum, monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    original_write = tm.atomic_write_json

    def blocked_write(path, data, **kwargs):
        if data.get("group_id") == -100222 and not entered.is_set():
            entered.set()
            assert release.wait(timeout=3)
        return original_write(path, data, **kwargs)

    monkeypatch.setattr(tm, "atomic_write_json", blocked_write)
    switching = asyncio.create_task(forum.manager.set_group_id(-100222))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        assert_original_destination(forum.manager)
        enabling = asyncio.create_task(forum.manager.set_enabled(False))
        release.set()
        await asyncio.gather(switching, enabling)
    finally:
        release.set()
        await switching
    restored = TopicsManager()
    assert restored.group_id == forum.manager.group_id == -100222
    assert restored.enabled is forum.manager.enabled is False
    assert restored.get_all_topics() == forum.manager.get_all_topics() == {}


async def test_cancelled_group_write_finishes_and_publishes_coherently(forum, monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    original_write = tm.atomic_write_json

    def blocked_write(path, data, **kwargs):
        entered.set()
        assert release.wait(timeout=3)
        return original_write(path, data, **kwargs)

    monkeypatch.setattr(tm, "atomic_write_json", blocked_write)
    switching = asyncio.create_task(forum.manager.set_group_id(-100222))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        switching.cancel()
        await asyncio.sleep(0)
        assert_original_destination(forum.manager)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await switching
    finally:
        release.set()
        await asyncio.gather(switching, return_exceptions=True)
    restored = TopicsManager()
    assert restored.group_id == forum.manager.group_id == -100222
    assert restored.get_all_topics() == forum.manager.get_all_topics() == {}


@pytest.mark.parametrize("cancel_count", [2, 4])
async def test_repeated_cancellation_retains_writer_until_following_mutation(forum, monkeypatch, cancel_count):
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    original_write = tm.atomic_write_json

    def blocked_write(path, data, **kwargs):
        if data["enabled"]:
            try:
                entered.set()
                assert release.wait(timeout=3)
                return original_write(path, data, **kwargs)
            finally:
                finished.set()
        return original_write(path, data, **kwargs)

    monkeypatch.setattr(tm, "atomic_write_json", blocked_write)
    switching = asyncio.create_task(forum.manager.set_group_id(-100222))
    following = None
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        for _ in range(cancel_count):
            switching.cancel()
            await asyncio.sleep(0)
            assert not switching.done()
        assert_original_destination(forum.manager)
        following = asyncio.create_task(forum.manager.set_enabled(False))
        await asyncio.sleep(0)
        assert not following.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await switching
        await following
    finally:
        release.set()
        await asyncio.gather(switching, *([following] if following is not None else []), return_exceptions=True)
        assert await asyncio.to_thread(finished.wait, 2)

    persisted = json.loads(forum.config.read_text())
    assert persisted == {
        "version": 2,
        "enabled": False,
        "group_id": -100222,
        "topics": {},
        "message_cache": {},
        "monitoring_revision": 1,
        "destination_revision": 1,
    }
    assert forum.manager._snapshot() == TopicsManager()._snapshot() == persisted


@pytest.mark.parametrize("cancel_count", [1, 2, 4])
async def test_cancelled_failed_write_preserves_snapshot_and_reports_failure(forum, monkeypatch, cancel_count):
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    before = forum.config.read_bytes()

    def failed_write(*_args, **_kwargs):
        entered.set()
        assert release.wait(timeout=3)
        finished.set()
        raise OSError("disk full")

    monkeypatch.setattr(tm, "atomic_write_json", failed_write)
    switching = asyncio.create_task(forum.manager.set_group_id(-100222))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        for _ in range(cancel_count):
            switching.cancel()
            await asyncio.sleep(0)
            assert not switching.done()
        release.set()
        with pytest.raises(OSError, match="disk full"):
            await switching
        assert finished.is_set()
    finally:
        release.set()
        await asyncio.gather(switching, return_exceptions=True)
        assert await asyncio.to_thread(finished.wait, 2)

    assert_original_destination(forum.manager)
    assert forum.manager.monitoring_revision == forum.manager.destination_revision == 0
    assert forum.config.read_bytes() == before
    assert_original_destination(TopicsManager())


async def test_old_destination_mark_waiting_for_switch_cannot_poison_cache(forum):
    async with forum.manager._write_lock:
        switching = asyncio.create_task(forum.manager.set_group_id(-100222))
        marking = asyncio.create_task(
            forum.manager.mark_message_sent(TopicType.NO_LIMIT, "late:alice", chat_id=-100111, message_thread_id=30)
        )
        await asyncio.sleep(0)
    await asyncio.gather(switching, marking)
    assert not forum.manager.is_message_sent(TopicType.NO_LIMIT, "late:alice")
    assert not TopicsManager().is_message_sent(TopicType.NO_LIMIT, "late:alice")


async def test_legacy_cache_migrates_into_authoritative_snapshot(forum):
    await forum.manager.set_topic_id(TopicType.WARNINGS, 99)
    forum.cache.write_text("{}")
    restored = TopicsManager()
    assert restored.is_message_sent(TopicType.NO_LIMIT, "no_limit:alice")
    assert restored.get_topic_id(TopicType.WARNINGS) == 99


@pytest.mark.parametrize("return_to_original", [False, True])
async def test_created_thread_for_previous_group_is_not_installed_in_new_group(forum, return_to_original):
    entered = asyncio.Event()
    release = asyncio.Event()
    chats = []

    async def create_topic(**kwargs):
        chats.append(kwargs["chat_id"])
        entered.set()
        await release.wait()
        return SimpleNamespace(message_thread_id=71)

    bot = SimpleNamespace(
        id=42,
        get_chat=AsyncMock(return_value=SimpleNamespace(is_forum=True, title="group")),
        get_chat_member=AsyncMock(return_value=SimpleNamespace(status="creator")),
        create_forum_topic=AsyncMock(side_effect=create_topic),
    )
    creating = asyncio.create_task(forum.manager.create_topics_for_group(bot))
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        await forum.manager.set_group_id(-100222)
        if return_to_original:
            await forum.manager.set_group_id(-100111)
        release.set()
        assert await creating == {}
    finally:
        release.set()
        await creating
    assert chats == [-100111]
    assert forum.manager.get_all_topics() == TopicsManager().get_all_topics() == {}


@pytest.mark.parametrize("return_to_original", [False, True])
async def test_failed_topic_creation_cannot_resume_after_destination_replacement(forum, return_to_original):
    entered = asyncio.Event()
    release = asyncio.Event()
    created_count = 0

    async def create_topic(**_kwargs):
        nonlocal created_count
        created_count += 1
        if created_count == 1:
            entered.set()
            await release.wait()
            raise RuntimeError("previous destination unavailable")
        return SimpleNamespace(message_thread_id=70 + created_count)

    bot = SimpleNamespace(
        id=42,
        get_chat=AsyncMock(return_value=SimpleNamespace(is_forum=True, title="group")),
        get_chat_member=AsyncMock(return_value=SimpleNamespace(status="creator")),
        create_forum_topic=AsyncMock(side_effect=create_topic),
    )
    creating = asyncio.create_task(forum.manager.create_topics_for_group(bot))
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        await forum.manager.set_group_id(-100222)
        if return_to_original:
            await forum.manager.set_group_id(-100111)
        release.set()
        assert await creating == {}
    finally:
        release.set()
        await creating
    assert created_count == 1
    assert forum.manager.get_all_topics() == TopicsManager().get_all_topics() == {}


async def test_current_destination_creation_continues_after_one_topic_error(forum):
    async def create_topic(**kwargs):
        if kwargs["name"] == tm.TOPIC_CONFIG[TopicType.GENERAL]["name"]:
            raise RuntimeError("cannot create General topic")
        return SimpleNamespace(message_thread_id=71)

    bot = SimpleNamespace(
        id=42,
        get_chat=AsyncMock(return_value=SimpleNamespace(is_forum=True, title="group")),
        get_chat_member=AsyncMock(return_value=SimpleNamespace(status="creator")),
        create_forum_topic=AsyncMock(side_effect=create_topic),
    )
    created = await forum.manager.create_topics_for_group(bot)
    expected = {
        TopicType.WARNINGS: 71,
        TopicType.DISABLE_ENABLE: 71,
        TopicType.BACKUPS: 71,
        TopicType.NO_LIMIT: 71,
    }
    assert created == expected
    assert (
        forum.manager.get_all_topics()
        == TopicsManager().get_all_topics()
        == {"general": 10, **{topic.value: thread for topic, thread in expected.items()}}
    )


async def drain_dispatcher(dispatcher):
    worker = asyncio.create_task(dispatcher.start_worker())
    await asyncio.sleep(0)
    await asyncio.wait_for(dispatcher.queue.join(), timeout=1)
    await dispatcher.stop(wait_seconds=0.1)
    await asyncio.wait_for(worker, timeout=1)


@pytest.fixture
async def outbound_forum(forum, tmp_path, monkeypatch):
    pending = tmp_path / "pending.json"
    monkeypatch.setattr(dm, "PENDING_NOTIFICATIONS_FILE", str(pending))
    monkeypatch.setattr(dm, "get_topics_manager", lambda: forum.manager)
    monkeypatch.setattr(sm, "get_topics_manager", lambda: forum.manager)
    dispatcher = TelegramDispatcher()
    monkeypatch.setattr(sm, "get_dispatcher", lambda: dispatcher)
    bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=42)))
    dispatcher.set_bot(bot)
    yield SimpleNamespace(**vars(forum), dispatcher=dispatcher, bot=bot, pending=pending)
    await dispatcher.stop(wait_seconds=0.1)


async def test_old_inflight_receipt_does_not_mark_replacement_group(outbound_forum):
    manager = outbound_forum.manager
    dispatcher = outbound_forum.dispatcher
    future = await dispatcher.enqueue_send(
        "before switch", topic_type=TopicType.NO_LIMIT, message_key="late:alice", return_future=True
    )
    async with manager._write_lock:
        switching = asyncio.create_task(manager.set_group_id(-100222))
        await asyncio.sleep(0)
        worker = asyncio.create_task(dispatcher.start_worker())
        assert await asyncio.wait_for(future, timeout=1) == (42, -100111)
    await switching
    await asyncio.wait_for(dispatcher.queue.join(), timeout=1)
    await dispatcher.stop(wait_seconds=0.1)
    await worker
    assert outbound_forum.bot.send_message.await_args.kwargs["chat_id"] == -100111
    assert not manager.is_message_sent(TopicType.NO_LIMIT, "late:alice")
    assert not TopicsManager().is_message_sent(TopicType.NO_LIMIT, "late:alice")


async def test_restart_replays_pinned_critical_backlog_even_if_topics_disabled(outbound_forum):
    await outbound_forum.dispatcher.enqueue_send(
        "applied incident", topic_type=TopicType.NO_LIMIT, priority=Priority.CRITICAL, message_key="incident:alice"
    )
    record = next(iter(json.loads(outbound_forum.pending.read_text()).values()))
    assert (record["chat_id"], record["message_thread_id"]) == (-100111, 30)
    await outbound_forum.manager.set_group_id(-100222)
    await outbound_forum.manager.set_enabled(False)
    await outbound_forum.dispatcher.stop(wait_seconds=0.1)
    restored = TelegramDispatcher()
    restored.set_bot(outbound_forum.bot)
    await drain_dispatcher(restored)
    delivered = outbound_forum.bot.send_message.await_args.kwargs
    assert (delivered["chat_id"], delivered["message_thread_id"]) == (-100111, 30)
    assert json.loads(outbound_forum.pending.read_text()) == {}
    assert not TopicsManager().enabled


@pytest.mark.parametrize("producer", ["general", "warning", "critical", "backup", "no_limit", "user", "disable"])
async def test_topics_off_never_admits_or_persists_new_logs(outbound_forum, producer):
    await outbound_forum.manager.set_enabled(False)
    calls = {
        "general": lambda: sm.send_logs("off-period log", message_key="off"),
        "warning": lambda: sm.send_warning_log("off-period warning"),
        "critical": lambda: sm.send_disable_enable_log("off-period critical"),
        "backup": lambda: sm.send_backup_log("off-period backup"),
        "no_limit": lambda: sm.send_no_limit_log("off-period no-limit"),
        "user": lambda: sm.send_user_message("off-period user", "alice", 3, False, False),
        "disable": lambda: sm.send_disable_notification("off-period disable", "alice"),
    }
    await calls[producer]()
    assert not outbound_forum.manager.enabled
    assert not TopicsManager().enabled
    assert outbound_forum.dispatcher.queue.empty()
    assert not outbound_forum.pending.exists()
    await outbound_forum.manager.set_enabled(True)
    await drain_dispatcher(outbound_forum.dispatcher)
    outbound_forum.bot.send_message.assert_not_awaited()


async def test_topics_off_direct_dispatcher_admission_is_rejected(outbound_forum):
    await outbound_forum.manager.set_enabled(False)
    futures = await outbound_forum.dispatcher.enqueue_send_batch(
        ["off-period incident"], priority=Priority.CRITICAL, chat_id=-100111, message_thread_id=30
    )
    assert futures == []
    assert outbound_forum.dispatcher.queue.empty()
    assert not outbound_forum.pending.exists()


async def test_disabling_topics_warns_monitoring_is_paused_and_logs_not_queued(forum, monkeypatch):
    import telegram_bot.handlers.topics_settings as handlers

    monkeypatch.setattr(handlers, "check_admin_privilege", AsyncMock(return_value=None))
    monkeypatch.setattr(handlers, "get_topics_manager", lambda: forum.manager)
    responses = AsyncMock()
    monkeypatch.setattr(handlers, "_send_response", responses)
    await handlers.topics_toggle(SimpleNamespace(), SimpleNamespace())
    assert not forum.manager.monitoring_allowed
    assert not TopicsManager().monitoring_allowed
    text = responses.await_args.args[1].lower()
    assert "monitoring is paused" in text
    assert "logs are not queued" in text


async def test_monitoring_requires_enabled_topics_and_a_configured_group(forum):
    assert forum.manager.monitoring_allowed
    assert forum.manager.monitoring_block_reason is None
    await forum.manager.set_enabled(False)
    assert not forum.manager.monitoring_allowed
    assert "paused" in forum.manager.monitoring_block_reason
    forum.config.write_text(json.dumps({"enabled": True, "topics": {}}))
    restored = TopicsManager()
    assert not restored.monitoring_allowed
    assert "No forum group" in restored.monitoring_block_reason


@pytest.mark.parametrize("priority", [Priority.SYSTEM, Priority.WARNINGS])
async def test_disable_reenable_discards_previously_queued_observations(outbound_forum, priority):
    future = await outbound_forum.dispatcher.enqueue_send(
        "stale observation", priority=priority, message_key="observation:alice", return_future=True
    )
    await outbound_forum.manager.set_enabled(False)
    await outbound_forum.manager.set_enabled(True)
    await drain_dispatcher(outbound_forum.dispatcher)
    assert await future is None
    outbound_forum.bot.send_message.assert_not_awaited()
    assert not outbound_forum.manager.is_message_sent(TopicType.GENERAL, "observation:alice")


async def test_disable_during_rate_limit_wait_discards_observation(outbound_forum, monkeypatch):
    entered = asyncio.Event()
    release = asyncio.Event()

    async def wait_for_token(*_args):
        entered.set()
        await release.wait()

    monkeypatch.setattr(outbound_forum.dispatcher.bucket, "consume", wait_for_token)
    await outbound_forum.dispatcher.enqueue_send("stale observation")
    worker = asyncio.create_task(outbound_forum.dispatcher.start_worker())
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        await outbound_forum.manager.set_enabled(False)
        release.set()
        await asyncio.wait_for(outbound_forum.dispatcher.queue.join(), timeout=1)
        await outbound_forum.dispatcher.stop(wait_seconds=0.1)
        await worker
    finally:
        release.set()
    outbound_forum.bot.send_message.assert_not_awaited()


async def test_reenable_starts_new_delivered_key_namespace(outbound_forum):
    dispatcher = outbound_forum.dispatcher
    worker = asyncio.create_task(dispatcher.start_worker())
    try:
        await sm.send_logs("first observation", topic_type=TopicType.NO_LIMIT, message_key="observation:alice")
        await asyncio.wait_for(dispatcher.queue.join(), timeout=1)
        assert outbound_forum.manager.is_message_sent(TopicType.NO_LIMIT, "observation:alice")
        await outbound_forum.manager.set_enabled(False)
        await outbound_forum.manager.set_enabled(True)
        assert not outbound_forum.manager.is_message_sent(TopicType.NO_LIMIT, "observation:alice")
        await sm.send_logs("new observation", topic_type=TopicType.NO_LIMIT, message_key="observation:alice")
        await asyncio.wait_for(dispatcher.queue.join(), timeout=1)
        assert outbound_forum.bot.send_message.await_count == 2
    finally:
        await dispatcher.stop(wait_seconds=0.1)
        await worker


async def test_pre_pause_inflight_receipt_cannot_mark_reenabled_cache(outbound_forum):
    entered = asyncio.Event()
    release = asyncio.Event()

    async def send(**_kwargs):
        entered.set()
        await release.wait()
        return SimpleNamespace(message_id=42)

    outbound_forum.bot.send_message.side_effect = send
    await sm.send_logs("already sending", topic_type=TopicType.NO_LIMIT, message_key="observation:alice")
    worker = asyncio.create_task(outbound_forum.dispatcher.start_worker())
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        await outbound_forum.manager.set_enabled(False)
        await outbound_forum.manager.set_enabled(True)
        release.set()
        await asyncio.wait_for(outbound_forum.dispatcher.queue.join(), timeout=1)
        await outbound_forum.dispatcher.stop(wait_seconds=0.1)
        await worker
    finally:
        release.set()
    assert not outbound_forum.manager.is_message_sent(TopicType.NO_LIMIT, "observation:alice")


async def test_returning_to_previous_group_starts_a_new_dedup_namespace(outbound_forum):
    dispatcher = outbound_forum.dispatcher
    worker = asyncio.create_task(dispatcher.start_worker())
    try:
        await sm.send_logs("first group", topic_type=TopicType.NO_LIMIT, message_key="observation:alice")
        await asyncio.wait_for(dispatcher.queue.join(), timeout=1)
        await outbound_forum.manager.set_group_id(-100222)
        await outbound_forum.manager.set_group_id(-100111)
        await outbound_forum.manager.set_topic_id(TopicType.NO_LIMIT, 30)
        await sm.send_logs("returned group", topic_type=TopicType.NO_LIMIT, message_key="observation:alice")
        await asyncio.wait_for(dispatcher.queue.join(), timeout=1)
        assert outbound_forum.bot.send_message.await_count == 2
    finally:
        await dispatcher.stop(wait_seconds=0.1)
        await worker


async def test_group_switch_preserves_pinned_observation_destination(outbound_forum):
    future = await outbound_forum.dispatcher.enqueue_send(
        "owned observation", topic_type=TopicType.NO_LIMIT, return_future=True
    )
    await outbound_forum.manager.set_group_id(-100222)
    await drain_dispatcher(outbound_forum.dispatcher)
    assert await future == (42, -100111)
    assert outbound_forum.bot.send_message.await_args.kwargs["message_thread_id"] == 30


async def test_legacy_direct_send_cannot_mark_replacement_group(forum, monkeypatch):
    entered = asyncio.Event()
    release = asyncio.Event()

    async def send(**_kwargs):
        entered.set()
        await release.wait()
        return SimpleNamespace(message_id=42)

    monkeypatch.setattr(tm, "get_topics_manager", lambda: forum.manager)
    bot = SimpleNamespace(send_message=AsyncMock(side_effect=send))
    sending = asyncio.create_task(tm.send_to_topic(bot, "old group", TopicType.NO_LIMIT, message_key="late:alice"))
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        await forum.manager.set_group_id(-100222)
        release.set()
        assert (await sending).message_id == 42
    finally:
        release.set()
        await sending
    assert not forum.manager.is_message_sent(TopicType.NO_LIMIT, "late:alice")


def test_invalid_legacy_cache_does_not_remove_valid_forum_configuration(forum):
    forum.cache.write_text("not json")
    restored = TopicsManager()
    assert restored.enabled
    assert restored.group_id == -100111
    assert restored.get_all_topics() == {"general": 10, "no_limit": 30}
    assert not restored.is_message_sent(TopicType.NO_LIMIT, "no_limit:alice")


async def test_accepted_group_is_validated_before_switch_and_html_is_escaped(forum, monkeypatch):
    import telegram_bot.handlers.topics_settings as handlers

    async def get_chat(group_id):
        assert group_id == -100222
        assert_original_destination(forum.manager)
        return SimpleNamespace(is_forum=True, title="<new group>")

    bot = SimpleNamespace(
        id=42,
        get_chat=AsyncMock(side_effect=get_chat),
        get_chat_member=AsyncMock(return_value=SimpleNamespace(status="creator")),
    )
    monkeypatch.setitem(sys.modules, "telegram_bot.main", SimpleNamespace(application=SimpleNamespace(bot=bot)))
    monkeypatch.setattr(handlers, "check_admin_privilege", AsyncMock(return_value=None))
    monkeypatch.setattr(handlers, "get_topics_manager", lambda: forum.manager)
    monkeypatch.setattr(handlers, "topics_menu", AsyncMock())
    update = SimpleNamespace(message=SimpleNamespace(text="-100222", reply_html=AsyncMock()))
    await handlers.topics_set_group_receive(update, SimpleNamespace(user_data={}))
    assert forum.manager.group_id == TopicsManager().group_id == -100222
    assert forum.manager.get_all_topics() == {}
    text = update.message.reply_html.await_args.args[0]
    assert "Successfully" in text
    assert "&lt;new group&gt;" in text
