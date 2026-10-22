"""Last seen settings use exact seconds and report persistence failures."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


@pytest.mark.parametrize("value", ["10", "90", "500"])
async def test_last_seen_saves_exact_positive_seconds(monkeypatch, value):
    import telegram_bot.handlers.settings_ip_source as settings

    saved = []

    async def save(key, text):
        saved.append((key, text))
        return True

    monkeypatch.setattr(settings, "read_config", AsyncMock(return_value={"api_ip_freshness": 60}))
    monkeypatch.setattr(settings, "save_config_value", save)
    update = SimpleNamespace(message=SimpleNamespace(text=value, reply_html=AsyncMock()))
    context = SimpleNamespace(user_data={})
    handler = getattr(settings, "ip_source_freshness_handler", None)
    assert handler is not None, "Last seen has no input handler"
    await handler(update, context)
    assert saved == [("api_ip_freshness", value)]
    assert f"{value}" in update.message.reply_html.call_args.args[0]


@pytest.mark.parametrize("value", ["0", "-1", "1.5", "1e2", "True", "1_0"])
async def test_last_seen_rejects_non_positive_or_non_integer_input(monkeypatch, value):
    import telegram_bot.handlers.settings_ip_source as settings

    monkeypatch.setattr(settings, "read_config", AsyncMock(return_value={"api_ip_freshness": 60}))
    save = AsyncMock(return_value=True)
    monkeypatch.setattr(settings, "save_config_value", save)
    update = SimpleNamespace(message=SimpleNamespace(text=value, reply_html=AsyncMock()))
    context = SimpleNamespace(user_data={})
    handler = getattr(settings, "ip_source_freshness_handler", None)
    assert handler is not None, "Last seen has no input handler"
    await handler(update, context)
    assert save.call_count == 0
    assert context.user_data["waiting_for"] == "ip_source_freshness"


async def test_last_seen_save_failure_keeps_prompt_and_reports_failure(monkeypatch):
    import telegram_bot.handlers.settings_ip_source as settings

    monkeypatch.setattr(settings, "read_config", AsyncMock(return_value={"api_ip_freshness": 60}))
    monkeypatch.setattr(settings, "save_config_value", AsyncMock(return_value=False))
    update = SimpleNamespace(message=SimpleNamespace(text="10", reply_html=AsyncMock()))
    context = SimpleNamespace(user_data={})
    handler = getattr(settings, "ip_source_freshness_handler", None)
    assert handler is not None, "Last seen has no input handler"
    await handler(update, context)
    assert context.user_data["waiting_for"] == "ip_source_freshness"
    assert "❌" in update.message.reply_html.call_args.args[0]


async def test_last_seen_cancel_does_not_save(monkeypatch):
    import telegram_bot.handlers.settings_ip_source as settings

    monkeypatch.setattr(settings, "read_config", AsyncMock(return_value={"api_ip_freshness": 10}))
    save = AsyncMock()
    monkeypatch.setattr(settings, "save_config_value", save)
    update = SimpleNamespace(message=SimpleNamespace(text="/cancel", reply_html=AsyncMock()))
    context = SimpleNamespace(user_data={"waiting_for": "ip_source_freshness"})
    handler = getattr(settings, "ip_source_freshness_handler", None)
    assert handler is not None, "Last seen has no input handler"
    await handler(update, context)
    assert save.call_count == 0
    assert not context.user_data.get("waiting_for")


async def test_freshness_callback_and_text_router_recheck_admin(monkeypatch):
    from unittest.mock import MagicMock

    import telegram_bot.main as main
    import telegram_bot.handlers.settings_ip_source as settings
    from telegram.ext import ConversationHandler

    save = AsyncMock(return_value=True)
    monkeypatch.setattr(settings, "save_config_value", save)
    monkeypatch.setattr(settings, "read_config", AsyncMock(return_value={"api_ip_freshness": 10}))
    monkeypatch.setattr(main, "check_admin", AsyncMock(return_value=[1]))
    update = MagicMock()
    update.effective_user.id = 1
    update.callback_query.data = "ip_source_set_freshness"
    update.callback_query.answer = AsyncMock()
    update.callback_query.edit_message_text = AsyncMock()
    update.message.text = "25"
    update.message.reply_html = AsyncMock()
    context = SimpleNamespace(user_data={})
    await main.callback_query_handler(update, context)
    assert context.user_data["waiting_for"] == "ip_source_freshness"
    assert "10s" in update.callback_query.edit_message_text.call_args.kwargs["text"]
    monkeypatch.setattr(main, "check_admin_privilege", AsyncMock(return_value=ConversationHandler.END))
    await main.text_message_handler(update, context)
    assert not context.user_data.get("waiting_for")
    assert save.call_count == 0
    context.user_data["waiting_for"] = "ip_source_freshness"
    monkeypatch.setattr(main, "check_admin_privilege", AsyncMock(return_value=None))
    await main.text_message_handler(update, context)
    assert save.call_args.args == ("api_ip_freshness", "25")


async def test_cancel_button_clears_freshness_waiting_state(monkeypatch):
    import telegram_bot.handlers.settings_ip_source as settings

    monkeypatch.setattr(settings, "read_config", AsyncMock(return_value={"api_ip_freshness": 10}))
    query = SimpleNamespace(edit_message_text=AsyncMock())
    context = SimpleNamespace(user_data={"waiting_for": "ip_source_freshness"})
    await settings.handle_ip_source_menu_callback(query, context)
    assert not context.user_data.get("waiting_for")
    keyboard = query.edit_message_text.call_args.kwargs["reply_markup"]
    assert any(button.text == "🕒 Last seen: 10s" for row in keyboard.inline_keyboard for button in row)
