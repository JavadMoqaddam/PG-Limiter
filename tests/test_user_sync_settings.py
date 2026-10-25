"""Telegram sync controls reflect completed mirrors and retire unsafe stale actions."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import db.database as database
import telegram_bot.handlers.settings_sync as settings
import utils.read_config as config
import utils.user_sync as user_sync
from db.crud.users import UserCRUD
from telegram_bot.constants import CallbackData
from test_config_revisions import settings_db  # noqa: F401


@pytest.fixture
def sync_menu_config(monkeypatch):
    panel_config = {
        "user_sync_interval": 5,
        "panel": {"username": "admin", "password": "password", "domain": "https://panel.example"},
    }
    monkeypatch.setattr(settings, "read_config", AsyncMock(return_value=panel_config))
    monkeypatch.setattr(config, "read_config", AsyncMock(return_value=panel_config))
    monkeypatch.setattr(user_sync, "_last_sync_time", None)


@pytest.mark.parametrize("handler_name", ["handle_pending_deletions_callback", "handle_force_delete_callback"])
async def test_stale_deletion_buttons_preserve_users_and_return_to_sync_menu(
    settings_db, sync_menu_config, monkeypatch, handler_name
):
    async with database.get_db() as db:
        await UserCRUD.create_or_update(db, "alice")
    panel_requests = []

    async def fetch_users(*args, **kwargs):
        panel_requests.append((args, kwargs))
        return []

    monkeypatch.setattr(user_sync, "get_all_users_with_details", fetch_users)
    monkeypatch.setattr(user_sync, "_notify_deleted_users", AsyncMock())
    query = SimpleNamespace(edit_message_text=AsyncMock())
    context = SimpleNamespace(user_data={"pending_deletions": ["alice"], "other_setting": "preserved"})

    await getattr(settings, handler_name)(query, context)

    async with database.get_db() as db:
        assert await UserCRUD.get_by_username(db, "alice") is not None
    assert panel_requests == []
    assert context.user_data == {"other_setting": "preserved"}
    message = query.edit_message_text.call_args.kwargs
    assert "User Sync Settings" in message["text"]
    callbacks = {button.callback_data for row in message["reply_markup"].inline_keyboard for button in row}
    assert CallbackData.USER_SYNC_NOW in callbacks
    assert CallbackData.USER_SYNC_PENDING not in callbacks
    assert CallbackData.USER_SYNC_FORCE_DELETE not in callbacks


@pytest.mark.parametrize("result", [(0, 1, 0), (3, 1, 0)])
async def test_sync_errors_never_announce_a_complete_mirror(sync_menu_config, monkeypatch, result):
    monkeypatch.setattr(user_sync, "sync_users_to_database", AsyncMock(return_value=result))
    query = SimpleNamespace(edit_message_text=AsyncMock())

    await settings.handle_user_sync_now_callback(query, SimpleNamespace(user_data={}))

    message = query.edit_message_text.call_args.kwargs["text"]
    assert "✅" not in message
    assert "❌" in message or "⚠️" in message
    assert "Errors: <code>1</code>" in message


@pytest.mark.parametrize("last_sync", [None, datetime(2026, 10, 25, tzinfo=timezone.utc)])
async def test_skipped_sync_does_not_announce_success(sync_menu_config, monkeypatch, last_sync):
    monkeypatch.setattr(user_sync, "_last_sync_time", last_sync)
    monkeypatch.setattr(user_sync, "sync_users_to_database", AsyncMock(return_value=(0, 0, 0)))
    query = SimpleNamespace(edit_message_text=AsyncMock())

    await settings.handle_user_sync_now_callback(query, SimpleNamespace(user_data={}))

    message = query.edit_message_text.call_args.kwargs["text"]
    assert "✅" not in message
    assert "not performed" in message.lower()


@pytest.mark.parametrize("deleted", [0, 2])
async def test_completed_empty_panel_mirror_reports_success(sync_menu_config, monkeypatch, deleted):
    last_sync = datetime(2026, 10, 25, tzinfo=timezone.utc)
    monkeypatch.setattr(user_sync, "_last_sync_time", last_sync)

    async def mirror_empty_panel(_panel_data):
        user_sync._last_sync_time = last_sync + timedelta(seconds=1)
        return 0, 0, deleted

    monkeypatch.setattr(user_sync, "sync_users_to_database", mirror_empty_panel)
    query = SimpleNamespace(edit_message_text=AsyncMock())

    await settings.handle_user_sync_now_callback(query, SimpleNamespace(user_data={}))

    message = query.edit_message_text.call_args.kwargs["text"]
    assert "✅" in message
    assert "Synced: <code>0</code>" in message
    if deleted:
        assert "Deleted: <code>2</code>" in message
