"""Telegram success messages must describe committed canonical settings."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import db.database as database
import utils.read_config as config
from db.crud.config import ConfigCRUD
from db.crud.config_revision import ConfigRevisionCRUD
from db.crud.users import UserCRUD
from telegram_bot import utils as bot_utils
from telegram_bot.handlers import backup, limits, punishment, users
from test_config_revisions import settings_db  # noqa: F401


def message_update(text="", payload=None):
    """Keep Telegram I/O external while exercising real handlers and SQLite."""
    message = SimpleNamespace(text=text, reply_html=AsyncMock(), reply_text=AsyncMock())
    if payload is not None:
        uploaded = SimpleNamespace(download_as_bytearray=AsyncMock(return_value=json.dumps(payload).encode()))
        message.document = SimpleNamespace(file_name="config.json", get_file=AsyncMock(return_value=uploaded))
    return SimpleNamespace(message=message, callback_query=None)


@pytest.fixture
def absent_panel_user(monkeypatch):
    monkeypatch.setattr("utils.user_sync.fetch_and_sync_single_user", AsyncMock(return_value=None))


async def test_missing_special_user_does_not_report_saved_limit(settings_db, absent_panel_user):
    assert not await bot_utils.handle_special_limit("missing", 7)
    assert (await config.read_config(refresh=True))["limits"]["special"] == {}


async def test_missing_whitelist_user_does_not_report_membership(settings_db, absent_panel_user):
    assert await bot_utils.add_except_user("missing") is None
    assert "missing" not in (await config.read_config(refresh=True))["except_users"]


async def test_remove_special_limit_requires_existing_override(settings_db):
    async with database.get_db() as db:
        await UserCRUD.create_or_update(db, "alice")
    query = SimpleNamespace(edit_message_text=AsyncMock())
    await limits.handle_remove_special_limit_callback(query, SimpleNamespace(), "alice")
    assert "No special limit" in query.edit_message_text.call_args.kwargs["text"]


async def test_special_limit_removal_escapes_username_and_clears_override(settings_db):
    async with database.get_db() as db:
        await UserCRUD.create_or_update(db, "a<b>")
        await UserCRUD.set_special_limit(db, "a<b>", 7, fetch_from_panel=False)
    query = SimpleNamespace(edit_message_text=AsyncMock())
    await limits.handle_remove_special_limit_callback(query, SimpleNamespace(), "a<b>")
    assert "a&lt;b&gt;" in query.edit_message_text.call_args.kwargs["text"]
    assert (await config.read_config(refresh=True))["limits"]["special"] == {}


@pytest.mark.parametrize(
    "handler_name",
    [
        "get_limit_number",
        "handle_special_limit_number_input",
        "handle_special_limit_1_callback",
        "handle_special_limit_2_callback",
    ],
)
async def test_special_limit_success_escapes_username(settings_db, handler_name):
    async with database.get_db() as db:
        await UserCRUD.create_or_update(db, "a<b>")
    context = SimpleNamespace(user_data={"selected_user": "a<b>"})
    if handler_name.endswith("callback"):
        query = SimpleNamespace(edit_message_text=AsyncMock())
        await getattr(limits, handler_name)(query, context)
        message = query.edit_message_text.call_args.kwargs["text"]
    else:
        update = message_update("7")
        await getattr(limits, handler_name)(update, context)
        message = update.message.reply_html.call_args.kwargs["text"]
    assert "a&lt;b&gt;" in message
    assert "a<b>" not in message


@pytest.mark.parametrize("handler_name", ["set_except_users_handler", "handle_except_user_input"])
async def test_whitelist_handlers_report_missing_user(settings_db, absent_panel_user, handler_name):
    update = message_update("missing")
    await getattr(users, handler_name)(update, SimpleNamespace(user_data={}))
    reply = update.message.reply_html.call_args
    message = reply.kwargs.get("text", reply.args[0] if reply.args else "")
    assert "successfully" not in message
    assert "❌" in message or "⚠️" in message


async def test_general_limit_persistence_error_reaches_telegram(settings_db, monkeypatch):
    monkeypatch.setattr(ConfigCRUD, "set", AsyncMock(side_effect=RuntimeError("disk unavailable")))
    query = SimpleNamespace(edit_message_text=AsyncMock())
    await limits.handle_general_limit_preset_callback(query, SimpleNamespace(), 7)
    message = query.edit_message_text.call_args.kwargs["text"]
    assert "✅" not in message
    assert "save" in message.lower()


async def test_failed_punishment_window_write_does_not_render_success(settings_db, monkeypatch):
    monkeypatch.setattr(punishment, "check_admin_privilege", AsyncMock(return_value=None))
    monkeypatch.setattr(ConfigCRUD, "set", AsyncMock(side_effect=RuntimeError("disk unavailable")))
    update = message_update()
    await punishment.punishment_set_window_hours(update, SimpleNamespace(), 48)
    message = update.message.reply_text.call_args.kwargs.get("text", "")
    assert "save" in message.lower()
    assert "48h" not in message


@pytest.mark.parametrize("handler_name", ["restore_config_handler", "migrate_backup_handler"])
async def test_restore_fetches_missing_user_before_writer_transaction(settings_db, monkeypatch, handler_name):
    async def panel_sync(username):
        # A sync/import owns its writer; running it under the restore writer deadlocks.
        async with config.config_write_transaction(), database.get_db() as db:
            await UserCRUD.create_or_update(db, username)
        return {"username": username}

    monkeypatch.setattr("utils.user_sync.fetch_and_sync_single_user", panel_sync)
    update = message_update(payload={"disable_method": "user", "limits": {"special": {"alice": 7}}})
    await asyncio.wait_for(getattr(backup, handler_name)(update, SimpleNamespace(user_data={})), timeout=2)
    assert (await config.read_config(refresh=True))["limits"]["special"] == {"alice": 7}


async def test_restore_settings_batch_rolls_back_revision_and_values(settings_db, monkeypatch):
    async with database.get_db() as db:
        initial_revision = await ConfigRevisionCRUD.read(db)
    real_set = ConfigCRUD.set

    async def fail_second(db, key, value, **kwargs):
        if key == "disabled_group_id":
            raise RuntimeError("disk unavailable")
        return await real_set(db, key, value, **kwargs)

    monkeypatch.setattr(ConfigCRUD, "set", fail_second)
    update = message_update(payload={"disable_method": "group", "disabled_group_id": 9})
    await backup.restore_config_handler(update, SimpleNamespace(user_data={}))
    async with database.get_db() as db:
        assert await ConfigCRUD.get(db, "disable_method") is None
        assert await ConfigCRUD.get(db, "disabled_group_id") is None
        assert await ConfigRevisionCRUD.read(db) == initial_revision
    assert "restored successfully" not in update.message.reply_html.call_args.args[0]


async def test_corrupt_revision_general_write_reaches_telegram(settings_db):
    from db.crud.config_revision import REVISION_KEY
    from db.models import Config

    async with database.get_db() as db:
        db.add(Config(key=REVISION_KEY, value={"revision": "corrupt"}))
    query = SimpleNamespace(edit_message_text=AsyncMock())
    await limits.handle_general_limit_preset_callback(query, SimpleNamespace(), 7)
    message = query.edit_message_text.call_args.kwargs["text"]
    assert "✅" not in message
    assert "save" in message.lower()
    async with database.get_db() as db:
        assert await ConfigCRUD.get(db, "general_limit") is None


@pytest.mark.parametrize("handler_name", ["restore_config_handler", "migrate_backup_handler"])
@pytest.mark.parametrize("kind", ["special", "except"])
async def test_corrupt_revision_aborts_restored_user_mutations(settings_db, handler_name, kind):
    from db.crud.config_revision import REVISION_KEY
    from db.models import Config

    async with database.get_db() as db:
        await UserCRUD.create_or_update(db, "alice")
        db.add(Config(key=REVISION_KEY, value={"revision": "corrupt"}))
    payload = {"limits": {"special": {"alice": 7}}} if kind == "special" else {"except_users": ["alice"]}
    update = message_update(payload=payload)
    await getattr(backup, handler_name)(update, SimpleNamespace(user_data={}))
    async with database.get_db() as db:
        user = await UserCRUD.get_by_username(db, "alice")
        assert user.special_limit is None
        assert not user.is_excepted
        assert await ConfigCRUD.get(db, REVISION_KEY) == {"revision": "corrupt"}
    assert "❌" in update.message.reply_html.call_args.args[0]


@pytest.mark.parametrize("handler_name", ["restore_config_handler", "migrate_backup_handler"])
async def test_revision_error_on_second_restored_user_rolls_back_whole_batch(settings_db, monkeypatch, handler_name):
    from sqlalchemy import update as sql_update
    from db.crud.config_revision import REVISION_KEY
    from db.models import Config

    async with database.get_db() as db:
        await UserCRUD.create_or_update(db, "alice")
        await UserCRUD.create_or_update(db, "bob")
        db.add(Config(key=REVISION_KEY, value={"revision": 0}))
    original = UserCRUD.set_special_limit

    async def corrupt_after_first(db, username, limit, **kwargs):
        result = await original(db, username, limit, **kwargs)
        if username == "alice":
            await db.execute(sql_update(Config).where(Config.key == REVISION_KEY).values(value={"revision": "corrupt"}))
        return result

    monkeypatch.setattr(UserCRUD, "set_special_limit", corrupt_after_first)
    update = message_update(payload={"limits": {"special": {"alice": 7, "bob": 8}}})
    await getattr(backup, handler_name)(update, SimpleNamespace(user_data={}))
    async with database.get_db() as db:
        assert await UserCRUD.get_special_limit(db, "alice") is None
        assert await UserCRUD.get_special_limit(db, "bob") is None
        assert await ConfigRevisionCRUD.read(db) == 0
    assert "❌" in update.message.reply_html.call_args.args[0]


@pytest.mark.parametrize("handler_name", ["restore_config_handler", "migrate_backup_handler"])
async def test_invalid_import_limits_are_skipped_before_valid_user_write(settings_db, handler_name):
    async with database.get_db() as db:
        for username in ("invalid", "badtext", "badshape", "alice"):
            await UserCRUD.create_or_update(db, username)
    update = message_update(
        payload={"limits": {"special": {"invalid": 0, "badtext": "bad", "badshape": {}, "alice": 7}}}
    )
    await getattr(backup, handler_name)(update, SimpleNamespace(user_data={}))
    async with database.get_db() as db:
        assert await UserCRUD.get_special_limit(db, "alice") == 7
        for username in ("invalid", "badtext", "badshape"):
            assert await UserCRUD.get_special_limit(db, username) is None
        assert await ConfigRevisionCRUD.read(db) == 1
    assert "✅" in update.message.reply_html.call_args.args[0]
