"""Topic setup preserves saved destinations and only repairs confirmed missing threads."""

import asyncio
import json
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.error import BadRequest, Forbidden, TimedOut

import telegram_bot.topics as tm
from telegram_bot.topics import TopicType, TopicsManager


# Literal saved categories make adding a new category observable in setup behavior.
SAVED_TOPICS = {
    "general": 10,
    "warnings": 11,
    "disable_enable": 12,
    "backups": 13,
    "no_limit": 14,
    "daily_report": 15,
    "mirza_notifications": 16,
}


@pytest.fixture
def setup_forum(tmp_path, monkeypatch):
    path = tmp_path / "topics.json"
    path.write_text(
        json.dumps(
            {
                "enabled": True,
                "group_id": -100111,
                "topics": SAVED_TOPICS,
                "message_cache": {"no_limit": {"seen": tm.time.time()}},
            }
        )
    )
    monkeypatch.setattr(tm, "TOPICS_FILE", str(path))
    monkeypatch.setattr(tm, "MESSAGE_CACHE_FILE", str(tmp_path / "legacy.json"))
    manager = TopicsManager()
    bot = SimpleNamespace(
        id=42,
        get_chat=AsyncMock(return_value=SimpleNamespace(is_forum=True, title="forum")),
        get_chat_member=AsyncMock(return_value=SimpleNamespace(status="creator")),
        edit_forum_topic=AsyncMock(return_value=True),
        create_forum_topic=AsyncMock(return_value=SimpleNamespace(message_thread_id=71)),
    )
    return SimpleNamespace(manager=manager, bot=bot, path=path)


async def test_all_saved_topics_are_verified_without_creating_or_writing(setup_forum):
    before = setup_forum.path.read_bytes()
    result = await setup_forum.manager.create_topics_for_group(setup_forum.bot)
    assert result == {}
    assert len(result.existing) == 7
    assert not result.cancelled
    assert setup_forum.manager.get_all_topics() == SAVED_TOPICS
    assert setup_forum.path.read_bytes() == before
    assert setup_forum.bot.create_forum_topic.await_count == 0
    assert {call.kwargs["message_thread_id"] for call in setup_forum.bot.edit_forum_topic.await_args_list} == {
        10,
        11,
        12,
        13,
        14,
        15,
        16,
    }
    assert all(
        call.kwargs.keys() == {"chat_id", "message_thread_id"}
        for call in setup_forum.bot.edit_forum_topic.await_args_list
    )


async def test_only_missing_mirza_topic_is_created_even_when_topics_disabled(setup_forum):
    await setup_forum.manager.remove_topic(TopicType("mirza_notifications"))
    await setup_forum.manager.set_enabled(False)
    result = await setup_forum.manager.create_topics_for_group(setup_forum.bot)
    assert result == {TopicType("mirza_notifications"): 71}
    assert len(result.existing) == 6
    assert setup_forum.manager.get_all_topics() == {**SAVED_TOPICS, "mirza_notifications": 71}
    assert TopicsManager().get_all_topics() == setup_forum.manager.get_all_topics()
    setup_forum.bot.create_forum_topic.assert_awaited_once_with(
        chat_id=-100111, name="🔔 Mirza Bot Notifications", icon_color=0xFFD67E
    )


@pytest.mark.parametrize("response", [True, BadRequest("TOPIC_NOT_MODIFIED")])
async def test_unchanged_topic_probe_retains_ids_and_message_receipts(setup_forum, response):
    setup_forum.bot.edit_forum_topic.side_effect = response if isinstance(response, Exception) else None
    result = await setup_forum.manager.create_topics_for_group(setup_forum.bot)
    assert result == {}
    assert len(result.existing) == 7
    assert setup_forum.manager.is_message_sent(TopicType.NO_LIMIT, "seen")
    assert TopicsManager().is_message_sent(TopicType.NO_LIMIT, "seen")


@pytest.mark.parametrize(
    "error", [BadRequest("TOPIC_ID_INVALID"), BadRequest("message thread not found"), BadRequest("TOPIC_DELETED")]
)
async def test_confirmed_deleted_topic_is_replaced_and_only_its_receipts_cleared(setup_forum, error):
    async def probe(**kwargs):
        if kwargs["message_thread_id"] == 14:
            raise error
        return True

    setup_forum.bot.edit_forum_topic.side_effect = probe
    result = await setup_forum.manager.create_topics_for_group(setup_forum.bot)
    assert result == {TopicType.NO_LIMIT: 71}
    assert len(result.existing) == 6
    assert setup_forum.manager.get_all_topics() == {**SAVED_TOPICS, "no_limit": 71}
    assert not TopicsManager().is_message_sent(TopicType.NO_LIMIT, "seen")
    assert setup_forum.bot.create_forum_topic.await_count == 1


@pytest.mark.parametrize(
    "error", [TimedOut(), Forbidden("not allowed"), BadRequest("TOPIC_CLOSED"), RuntimeError("unexpected")]
)
async def test_unverified_saved_topics_are_never_recreated(setup_forum, error):
    before = setup_forum.path.read_bytes()
    setup_forum.bot.edit_forum_topic.side_effect = error
    result = await setup_forum.manager.create_topics_for_group(setup_forum.bot)
    assert result == {}
    assert len(result.unverified) == 7
    assert not result.existing
    assert setup_forum.manager.get_all_topics() == SAVED_TOPICS
    assert setup_forum.path.read_bytes() == before
    assert setup_forum.bot.create_forum_topic.await_count == 0


async def test_partial_failure_retains_success_and_retry_creates_only_missing(setup_forum):
    await setup_forum.manager.remove_topic(TopicType.WARNINGS)
    await setup_forum.manager.remove_topic(TopicType.BACKUPS)

    async def create(**kwargs):
        if kwargs["name"] == "💾 Backups":
            raise BadRequest("cannot create topic")
        return SimpleNamespace(message_thread_id=71)

    setup_forum.bot.create_forum_topic.side_effect = create
    first = await setup_forum.manager.create_topics_for_group(setup_forum.bot)
    assert first == {TopicType.WARNINGS: 71}
    assert first.failed == {TopicType.BACKUPS}
    assert TopicsManager().get_all_topics()["warnings"] == 71
    setup_forum.bot.create_forum_topic.side_effect = None
    second = await setup_forum.manager.create_topics_for_group(setup_forum.bot)
    assert second == {TopicType.BACKUPS: 71}
    assert setup_forum.manager.get_all_topics() == {**SAVED_TOPICS, "warnings": 71, "backups": 71}
    assert setup_forum.bot.create_forum_topic.await_count == 3


async def test_create_timeout_is_reported_as_uncertain_without_claiming_success(setup_forum):
    await setup_forum.manager.remove_topic(TopicType.WARNINGS)
    setup_forum.bot.create_forum_topic.side_effect = TimedOut()
    result = await setup_forum.manager.create_topics_for_group(setup_forum.bot)
    assert result == {}
    assert result.uncertain == {TopicType.WARNINGS}
    assert not result.failed
    assert "warnings" not in TopicsManager().get_all_topics()


async def test_permission_failure_preserves_configuration_without_topic_operations(setup_forum):
    before = setup_forum.path.read_bytes()
    setup_forum.bot.get_chat_member.return_value = SimpleNamespace(status="member")
    result = await setup_forum.manager.create_topics_for_group(setup_forum.bot)
    assert result == {}
    assert result.permission_error
    assert setup_forum.path.read_bytes() == before
    assert setup_forum.bot.edit_forum_topic.await_count == 0
    assert setup_forum.bot.create_forum_topic.await_count == 0


async def test_failed_save_after_creation_keeps_prior_success_and_stops_further_creates(setup_forum, monkeypatch):
    await setup_forum.manager.remove_topic(TopicType.WARNINGS)
    await setup_forum.manager.remove_topic(TopicType.BACKUPS)
    original_write = tm.atomic_write_json

    def fail_second_save(path, data, **kwargs):
        if "backups" in data["topics"]:
            raise OSError("disk full")
        return original_write(path, data, **kwargs)

    monkeypatch.setattr(tm, "atomic_write_json", fail_second_save)
    with pytest.raises(OSError, match="disk full"):
        await setup_forum.manager.create_topics_for_group(setup_forum.bot)
    assert TopicsManager().get_all_topics() == {
        "general": 10,
        "warnings": 71,
        "disable_enable": 12,
        "no_limit": 14,
        "daily_report": 15,
        "mirza_notifications": 16,
    }
    assert setup_forum.manager.get_all_topics() == TopicsManager().get_all_topics()
    assert setup_forum.bot.create_forum_topic.await_count == 2


async def test_failed_forgetting_deleted_topic_preserves_mapping_and_never_creates(setup_forum, monkeypatch):
    before = setup_forum.path.read_bytes()
    setup_forum.bot.edit_forum_topic.side_effect = BadRequest("TOPIC_DELETED")

    def fail_write(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(tm, "atomic_write_json", fail_write)
    with pytest.raises(OSError, match="disk full"):
        await setup_forum.manager.create_topics_for_group(setup_forum.bot)
    assert setup_forum.manager.get_all_topics() == SAVED_TOPICS
    assert setup_forum.path.read_bytes() == before
    assert setup_forum.bot.create_forum_topic.await_count == 0


async def test_destination_change_after_own_install_cannot_be_absorbed_by_next_iteration(setup_forum, monkeypatch):
    await setup_forum.manager.remove_topic(TopicType.WARNINGS)
    await setup_forum.manager.remove_topic(TopicType.BACKUPS)
    original_install = setup_forum.manager.set_topic_id

    async def install_and_change_destination(*args, **kwargs):
        changed = await original_install(*args, **kwargs)
        await setup_forum.manager.clear_message_cache()
        return changed

    monkeypatch.setattr(setup_forum.manager, "set_topic_id", install_and_change_destination)
    result = await setup_forum.manager.create_topics_for_group(setup_forum.bot)
    assert result.cancelled
    assert setup_forum.bot.create_forum_topic.await_count == 1
    assert TopicsManager().get_all_topics()["warnings"] == 71
    assert "backups" not in TopicsManager().get_all_topics()


async def test_concurrent_clicks_create_missing_topics_once(setup_forum):
    await setup_forum.manager.remove_topic(TopicType.WARNINGS)
    entered, release = asyncio.Event(), asyncio.Event()

    async def create(**kwargs):
        entered.set()
        await release.wait()
        return SimpleNamespace(message_thread_id=71)

    setup_forum.bot.create_forum_topic.side_effect = create
    first = asyncio.create_task(setup_forum.manager.create_topics_for_group(setup_forum.bot))
    second = None
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        second = asyncio.create_task(setup_forum.manager.create_topics_for_group(setup_forum.bot))
        await asyncio.sleep(0)
        release.set()
        results = await asyncio.gather(first, second)
    finally:
        release.set()
        await first
        if second is not None:
            await second
    assert setup_forum.bot.create_forum_topic.await_count == 1
    assert results[0] == {TopicType.WARNINGS: 71}
    assert results[1].cancelled
    assert TopicsManager().get_all_topics() == {**SAVED_TOPICS, "warnings": 71}


@pytest.mark.parametrize("stage", ["permission", "probe", "creation"])
@pytest.mark.parametrize("change", ["switch_back", "clear", "cache", "mapping"])
async def test_destination_changes_during_await_cancel_setup(setup_forum, stage, change):
    entered, release = asyncio.Event(), asyncio.Event()
    if stage == "creation":
        await setup_forum.manager.remove_topic(TopicType.WARNINGS)

    async def blocked(**kwargs):
        entered.set()
        await release.wait()
        if stage == "permission":
            return SimpleNamespace(is_forum=True, title="forum")
        if stage == "creation":
            return SimpleNamespace(message_thread_id=71)
        return True

    if stage == "permission":

        async def get_chat(_chat_id):
            return await blocked()

        setup_forum.bot.get_chat.side_effect = get_chat
    elif stage == "probe":
        setup_forum.bot.edit_forum_topic.side_effect = blocked
    else:
        setup_forum.bot.create_forum_topic.side_effect = blocked
    creating = asyncio.create_task(setup_forum.manager.create_topics_for_group(setup_forum.bot))
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        if change == "switch_back":
            await setup_forum.manager.set_group_id(-100222)
            await setup_forum.manager.set_group_id(-100111)
        elif change == "clear":
            await setup_forum.manager.clear_all_topics()
        elif change == "cache":
            await setup_forum.manager.clear_message_cache()
        else:
            await setup_forum.manager.set_topic_id(TopicType.BACKUPS, 99)
        after_operator_change = setup_forum.path.read_bytes()
        release.set()
        result = await creating
    finally:
        release.set()
        await creating
    assert result == {}
    assert result.cancelled
    assert setup_forum.path.read_bytes() == after_operator_change
    assert setup_forum.bot.create_forum_topic.await_count == (1 if stage == "creation" else 0)


async def test_setup_ui_reports_existing_and_unverified_without_claiming_creation(setup_forum, monkeypatch):
    import telegram_bot.handlers.topics_settings as handlers

    setup_forum.bot.edit_forum_topic.side_effect = TimedOut()
    monkeypatch.setitem(
        sys.modules, "telegram_bot.main", SimpleNamespace(application=SimpleNamespace(bot=setup_forum.bot))
    )
    monkeypatch.setattr(handlers, "get_topics_manager", lambda: setup_forum.manager)
    monkeypatch.setattr(handlers, "check_admin_privilege", AsyncMock(return_value=None))
    response = AsyncMock()
    monkeypatch.setattr(handlers, "_send_response", response)
    await handlers.topics_setup(SimpleNamespace(), SimpleNamespace())
    text = response.await_args.args[1]
    assert "could not be verified" in text.lower()
    assert "preserved" in text.lower()
    assert "Topics Created Successfully" not in text
    setup_forum.bot.edit_forum_topic.side_effect = None
    await handlers.topics_setup(SimpleNamespace(), SimpleNamespace())
    text = response.await_args.args[1]
    assert "already" in text.lower()
    assert "7" in text


@pytest.mark.parametrize("mode", ["new", "partial", "uncertain", "permission", "cancelled"])
async def test_setup_ui_reports_actual_creation_outcomes(setup_forum, monkeypatch, mode):
    import telegram_bot.handlers.topics_settings as handlers

    if mode in {"new", "partial", "uncertain"}:
        await setup_forum.manager.remove_topic(TopicType.WARNINGS)
    if mode == "partial":
        await setup_forum.manager.remove_topic(TopicType.BACKUPS)

        async def create(**kwargs):
            if kwargs["name"] == "💾 Backups":
                raise BadRequest("cannot create topic")
            return SimpleNamespace(message_thread_id=71)

        setup_forum.bot.create_forum_topic.side_effect = create
    elif mode == "uncertain":
        setup_forum.bot.create_forum_topic.side_effect = TimedOut()
    elif mode == "permission":
        setup_forum.bot.get_chat.side_effect = BadRequest("<unavailable>")
    elif mode == "cancelled":

        async def probe(**kwargs):
            await setup_forum.manager.set_group_id(-100222)
            return True

        setup_forum.bot.edit_forum_topic.side_effect = probe
    monkeypatch.setitem(
        sys.modules, "telegram_bot.main", SimpleNamespace(application=SimpleNamespace(bot=setup_forum.bot))
    )
    monkeypatch.setattr(handlers, "get_topics_manager", lambda: setup_forum.manager)
    monkeypatch.setattr(handlers, "check_admin_privilege", AsyncMock(return_value=None))
    response = AsyncMock()
    monkeypatch.setattr(handlers, "_send_response", response)
    await handlers.topics_setup(SimpleNamespace(), SimpleNamespace())
    text = response.await_args.args[1]
    if mode == "new":
        assert "Created 1 topics" in text
        assert "Verified 6 existing" in text
        assert "Topics Created Successfully" in text
    elif mode == "partial":
        assert "Created 1 topics" in text
        assert "Could not create 1 topics" in text
        assert "Incomplete" in text
        assert "Topics Created Successfully" not in text
    elif mode == "uncertain":
        assert "uncertain" in text
        assert "may already exist" in text
    elif mode == "permission":
        assert "Cannot Set Up" in text
        assert "&lt;unavailable&gt;" in text
        assert "<unavailable>" not in text
    else:
        assert "Stopped" in text
        assert "configuration changed" in text


async def test_ui_destination_change_while_sending_progress_cancels_before_topic_requests(setup_forum, monkeypatch):
    import telegram_bot.handlers.topics_settings as handlers

    monkeypatch.setitem(
        sys.modules, "telegram_bot.main", SimpleNamespace(application=SimpleNamespace(bot=setup_forum.bot))
    )
    monkeypatch.setattr(handlers, "get_topics_manager", lambda: setup_forum.manager)
    monkeypatch.setattr(handlers, "check_admin_privilege", AsyncMock(return_value=None))
    replies = []

    async def respond(_update, text, *args, **kwargs):
        replies.append(text)
        if len(replies) == 1:
            await setup_forum.manager.set_group_id(-100222)

    monkeypatch.setattr(handlers, "_send_response", respond)
    await handlers.topics_setup(SimpleNamespace(), SimpleNamespace())
    assert "Stopped" in replies[-1]
    assert setup_forum.bot.create_forum_topic.await_count == 0
    assert setup_forum.bot.edit_forum_topic.await_count == 0
    assert setup_forum.manager.get_all_topics() == {}


def test_menu_counts_known_topics_and_includes_mirza_setup(setup_forum, monkeypatch):
    import telegram_bot.handlers.topics_settings as handlers

    setup_forum.manager._topics["retired_topic"] = 99
    monkeypatch.setattr(handlers, "get_topics_manager", lambda: setup_forum.manager)
    keyboard = handlers.create_topics_menu_keyboard(True)
    labels = [button.text for row in keyboard.inline_keyboard for button in row]
    assert any("(7/7)" in label for label in labels)
