"""Configuration handlers must not confirm a settings change that failed to persist."""

from unittest.mock import AsyncMock, MagicMock

import telegram_bot.handlers.settings_device_count as sdc


def _query():
    query = MagicMock()
    query.answer = AsyncMock()
    return query


def _answered_text(query):
    return " ".join(str(call.args[0]) for call in query.answer.call_args_list if call.args)


async def test_device_count_mode_reports_save_failure(monkeypatch):
    monkeypatch.setattr(sdc, "read_config", AsyncMock(return_value={"device_count_mode": "device"}))
    monkeypatch.setattr(sdc, "save_config_value", AsyncMock(return_value=False))
    monkeypatch.setattr(sdc, "handle_device_count_menu_callback", AsyncMock())

    query = _query()
    await sdc._set_device_count_mode(query, MagicMock(), "ip", "per IP")

    # A failed persist must not be confirmed with a success tick.
    assert "✅" not in _answered_text(query)
    assert query.answer.await_count >= 1


async def test_device_count_mode_confirms_on_success(monkeypatch):
    monkeypatch.setattr(sdc, "read_config", AsyncMock(return_value={"device_count_mode": "device"}))
    monkeypatch.setattr(sdc, "save_config_value", AsyncMock(return_value=True))
    monkeypatch.setattr(sdc, "handle_device_count_menu_callback", AsyncMock())

    query = _query()
    await sdc._set_device_count_mode(query, MagicMock(), "ip", "per IP")

    assert "✅" in _answered_text(query)
