"""The old /16 ISP mode must resolve to /24 without offering an ISP-dependent toggle."""

from unittest.mock import AsyncMock

from telegram_bot.handlers import settings_subnet


async def test_old_subnet_mode_button_cannot_reenable_wide_grouping(monkeypatch):
    saved = {}

    async def config():
        return {"subnet_ip_grouping": True, "subnet_grouping_mode": "/24"}

    async def save(key, value):
        saved[key] = value

    monkeypatch.setattr(settings_subnet, "read_config", config)
    monkeypatch.setattr(settings_subnet, "save_config_value", save)
    query = AsyncMock()
    await settings_subnet.subnet_ip_grouping_mode_toggle_callback(query, object())
    assert saved.get("subnet_grouping_mode", "/24") == "/24"
    text = query.edit_message_text.call_args.kwargs["text"]
    assert "/16" not in text and "ISP" not in text


async def test_subnet_menu_preserves_24_toggle_without_mode_option(monkeypatch):
    async def config():
        return {"subnet_ip_grouping": True, "subnet_grouping_mode": "/16"}

    monkeypatch.setattr(settings_subnet, "read_config", config)
    query = AsyncMock()
    await settings_subnet.subnet_ip_grouping_menu_callback(query, object())
    kwargs = query.edit_message_text.call_args.kwargs
    assert "/24" in kwargs["text"] and "/16" not in kwargs["text"]
    buttons = kwargs["reply_markup"].inline_keyboard
    assert any(button.callback_data == "subnet_ip_grouping_toggle" for row in buttons for button in row)
    assert not any(button.callback_data == "subnet_ip_grouping_mode_toggle" for row in buttons for button in row)
