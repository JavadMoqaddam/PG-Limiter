"""Interaction behavior through the real PTB dispatcher and Telegram handlers."""

import importlib
import json
from itertools import count
from types import SimpleNamespace

import pytest
import pytest_asyncio
from telegram import Update
from telegram.ext import ApplicationBuilder
from telegram.request import BaseRequest

import telegram_bot.handlers.admin as admin_handlers
import telegram_bot.handlers.limits as limit_handlers
import telegram_bot.handlers.settings_display as display_handlers
import telegram_bot.handlers.settings_panel as panel_handlers
import telegram_bot.handlers.settings_ip_source as ip_source_handlers
import telegram_bot.handlers.users as user_handlers
import telegram_bot.main as tgmain


class TelegramTransport(BaseRequest):
    """Offline Telegram HTTP boundary; retain sent text and message identities."""

    def __init__(self):
        self.messages = []
        self.sent_ids = []
        self.ids = count(1000)

    @property
    def read_timeout(self):
        return 5

    async def initialize(self):
        pass

    async def shutdown(self):
        pass

    async def do_request(self, url, method, request_data=None, **kwargs):
        endpoint = url.rsplit("/", 1)[-1]
        params = request_data.parameters if request_data else {}
        if endpoint == "getMe":
            result = {"id": 99, "is_bot": True, "first_name": "Limiter", "username": "limiter_bot"}
        elif endpoint == "getFile":
            result = {"file_id": "file", "file_unique_id": "unique", "file_path": "documents/invalid.txt"}
        elif endpoint == "invalid.txt":
            return 200, b"invalid backup"
        elif endpoint in {"sendMessage", "editMessageText"}:
            self.messages.append(params["text"])
            result = {
                "message_id": params["message_id"] if "message_id" in params else next(self.ids),
                "date": 1,
                "chat": {"id": params["chat_id"], "type": "supergroup"},
                "from": {"id": 99, "is_bot": True, "first_name": "Limiter"},
                "text": params["text"],
            }
            if params.get("message_thread_id"):
                result["message_thread_id"] = params["message_thread_id"]
            self.sent_ids.append(result["message_id"])
        else:
            result = True
        return 200, json.dumps({"ok": True, "result": result}).encode()


@pytest_asyncio.fixture
async def dispatcher(monkeypatch):
    transport = TelegramTransport()
    build = ApplicationBuilder.build

    def build_offline(builder):
        return build(builder.request(transport).get_updates_request(TelegramTransport()))

    monkeypatch.setattr(ApplicationBuilder, "build", build_offline)
    monkeypatch.delenv("BOT_TOKEN", raising=False)
    importlib.reload(tgmain)
    app = tgmain.application
    admins = {7}
    limits = {}
    except_users = set()
    config = {"ip_source": "api", "api_ip_concurrency": 20}
    errors = []

    async def configured_admins():
        return list(admins)

    async def save_limit(username, value):
        existed = username in limits
        limits[username] = value
        return existed, value

    async def collect_error(update, context):
        errors.append(context.error)

    async def save_except(username):
        except_users.add(username)

    async def configured_settings():
        return dict(config)

    async def save_setting(key, value):
        config[key] = value

    async def save_general_limit(value):
        config["general_limit"] = value

    async def save_token(value):
        config["ipinfo_token"] = value
        return True

    async def save_panel(domain, password, username):
        config["panel"] = {"domain": domain, "username": username, "password": password}

    async def add_admin(value):
        admins.add(int(value))
        return True

    async def remove_admin(value):
        admins.remove(value)
        return True

    async def remove_except(value):
        except_users.discard(value)
        return value

    monkeypatch.setattr(tgmain, "check_admin", configured_admins)
    monkeypatch.setattr(admin_handlers, "check_admin", configured_admins)
    monkeypatch.setattr(tgmain, "handel_special_limit", save_limit)
    monkeypatch.setattr(limit_handlers, "handel_special_limit", save_limit)
    monkeypatch.setattr(user_handlers, "add_except_user", save_except)
    monkeypatch.setattr(user_handlers, "remove_except_user_from_config", remove_except)
    monkeypatch.setattr(admin_handlers, "add_admin_to_config", add_admin)
    monkeypatch.setattr(admin_handlers, "remove_admin_from_config", remove_admin)
    monkeypatch.setattr(limit_handlers, "save_general_limit", save_general_limit)
    monkeypatch.setattr(display_handlers, "save_ipinfo_token", save_token)
    monkeypatch.setattr(panel_handlers, "add_base_information", save_panel)
    monkeypatch.setattr(ip_source_handlers, "read_config", configured_settings)
    monkeypatch.setattr(ip_source_handlers, "save_config_value", save_setting)
    app.add_error_handler(collect_error)
    await app.initialize()
    ids = count(1)

    def message(text, chat=-100, topic=None, reply_to=None, message_id=None, user=7):
        msg = {
            "message_id": message_id or next(ids),
            "date": 1,
            "from": {"id": user, "is_bot": False, "first_name": "Admin"},
            "chat": {"id": chat, "type": "supergroup"},
            "text": text,
        }
        if topic:
            msg.update(message_thread_id=topic, is_topic_message=True)
        if text.startswith("/"):
            msg["entities"] = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
        if reply_to:
            msg["reply_to_message"] = {
                "message_id": reply_to,
                "date": 1,
                "chat": msg["chat"],
                "from": {"id": 99, "is_bot": True, "first_name": "Limiter"},
                "text": "Earlier prompt",
            }
        return msg

    async def text(value, **kwargs):
        update = Update.de_json({"update_id": next(ids), "message": message(value, **kwargs)}, app.bot)
        await app.process_update(update)

    async def click(value, chat=-100, topic=None, prompt=50):
        update = Update.de_json(
            {
                "update_id": next(ids),
                "callback_query": {
                    "id": str(next(ids)),
                    "chat_instance": "test-chat",
                    "from": {"id": 7, "is_bot": False, "first_name": "Admin"},
                    "data": value,
                    "message": message("Prompt", chat=chat, topic=topic, message_id=prompt),
                },
            },
            app.bot,
        )
        await app.process_update(update)

    async def document(chat=-100, topic=None, reply_to=None):
        msg = message("", chat=chat, topic=topic, reply_to=reply_to)
        del msg["text"]
        msg["document"] = {"file_id": "file", "file_unique_id": "unique", "file_name": "invalid.txt"}
        await app.process_update(Update.de_json({"update_id": next(ids), "message": msg}, app.bot))

    yield SimpleNamespace(
        text=text,
        click=click,
        document=document,
        limits=limits,
        except_users=except_users,
        config=config,
        admins=admins,
        messages=transport.messages,
        sent_ids=transport.sent_ids,
        app=app,
        errors=errors,
    )
    await app.shutdown()
    assert not errors


async def test_command_supersedes_inline_prompt_and_consumes_username_once(dispatcher):
    await dispatcher.click("custom_limit:alice")
    await dispatcher.text("/set_special_limit")
    await dispatcher.text("bob")
    assert not any("Invalid number" in text for text in dispatcher.messages)
    await dispatcher.text("3")
    assert dispatcher.limits == {"bob": 3}


@pytest.mark.parametrize("navigation", ["limits_menu", "main_menu", "settings_menu"])
async def test_navigation_ends_pending_inline_input(dispatcher, navigation):
    await dispatcher.click("custom_limit:alice")
    await dispatcher.click(navigation)
    await dispatcher.text("3")
    assert dispatcher.limits == {}


@pytest.mark.parametrize("command", ["/cancel", "/help", "/start", "/unknown"])
async def test_commands_end_pending_inline_input(dispatcher, command):
    await dispatcher.click("custom_limit:alice")
    await dispatcher.text(command)
    await dispatcher.text("3")
    assert dispatcher.limits == {}


async def test_cancel_ends_native_conversation(dispatcher):
    await dispatcher.text("/set_special_limit")
    await dispatcher.text("alice")
    await dispatcher.text("/cancel")
    await dispatcher.text("3")
    assert dispatcher.limits == {}
    assert any("cancel" in text.lower() for text in dispatcher.messages)


async def test_inline_prompt_supersedes_native_conversation(dispatcher):
    await dispatcher.text("/set_special_limit")
    await dispatcher.text("alice")
    await dispatcher.click("custom_limit:bob")
    await dispatcher.text("3")
    assert dispatcher.limits == {"bob": 3}


@pytest.mark.parametrize("origin", ["inline", "command"])
@pytest.mark.parametrize("other", [{"chat": -200, "topic": 10}, {"chat": -100, "topic": 20}])
async def test_reply_in_another_chat_or_topic_preserves_origin_owner(dispatcher, origin, other):
    if origin == "inline":
        await dispatcher.click("custom_limit:alice", topic=10)
    else:
        await dispatcher.text("/set_special_limit", topic=10)
        await dispatcher.text("alice", topic=10)
    await dispatcher.text("8", **other)
    assert dispatcher.limits == {}
    await dispatcher.text("3", topic=10)
    assert dispatcher.limits == {"alice": 3}


async def test_explicit_reply_to_old_prompt_cannot_consume_new_prompt(dispatcher):
    await dispatcher.click("custom_limit:alice", prompt=50)
    await dispatcher.click("custom_limit:bob", prompt=60)
    await dispatcher.text("8", reply_to=50)
    assert dispatcher.limits == {}
    await dispatcher.text("3", reply_to=60)
    assert dispatcher.limits == {"bob": 3}


async def test_stale_choice_button_cannot_use_new_prompt_selected_user(dispatcher):
    await dispatcher.click("edit_special_limit:alice", prompt=50)
    await dispatcher.click("custom_limit:bob", prompt=60)
    await dispatcher.click("special_limit_1", prompt=50)
    assert dispatcher.limits == {}
    await dispatcher.text("3", reply_to=60)
    assert dispatcher.limits == {"bob": 3}


async def test_current_inline_choice_button_applies_to_its_selected_user(dispatcher):
    await dispatcher.click("edit_special_limit:alice", prompt=50)
    await dispatcher.click("special_limit_custom", prompt=50)
    await dispatcher.text("3", reply_to=50)
    assert dispatcher.limits == {"alice": 3}


async def test_username_reply_rebinds_choice_buttons_to_new_message(dispatcher):
    await dispatcher.click("set_special_limit", prompt=50)
    await dispatcher.text("alice")
    await dispatcher.click("special_limit_2", prompt=dispatcher.sent_ids[-1])
    assert dispatcher.limits == {"alice": 2}


@pytest.mark.parametrize("origin", ["inline", "command"])
async def test_revoked_admin_continuation_ends_ownership(dispatcher, origin):
    if origin == "inline":
        await dispatcher.click("custom_limit:alice")
    else:
        await dispatcher.text("/set_special_limit")
        await dispatcher.text("alice")
    dispatcher.admins.clear()
    await dispatcher.text("3")
    assert dispatcher.limits == {}
    assert any("permission" in text for text in dispatcher.messages)
    dispatcher.admins.add(7)
    await dispatcher.text("4")
    assert dispatcher.limits == {}


async def test_new_command_conversation_supersedes_earlier_conversation(dispatcher):
    await dispatcher.text("/set_special_limit")
    await dispatcher.text("alice")
    await dispatcher.text("/set_except_user")

    await dispatcher.text("3")
    assert dispatcher.limits == {}
    assert dispatcher.except_users == {"3"}


async def test_reentering_same_command_starts_fresh_username_step(dispatcher):
    await dispatcher.text("/set_special_limit")
    await dispatcher.text("alice")
    await dispatcher.text("/set_special_limit")
    await dispatcher.text("bob")
    await dispatcher.text("3")
    assert dispatcher.limits == {"bob": 3}


async def test_rearmed_inline_prompt_accepts_explicit_reply_to_validation_message(dispatcher):
    await dispatcher.click("ip_source_set_concurrency")
    await dispatcher.text("invalid", reply_to=50)
    assert "whole number" in dispatcher.messages[-1]
    await dispatcher.text("8", reply_to=dispatcher.sent_ids[-1])
    assert dispatcher.config["api_ip_concurrency"] == "8"


@pytest.mark.parametrize("origin", ["inline", "command"])
async def test_unauthorized_entry_cannot_arm_input(dispatcher, origin):
    dispatcher.admins.clear()
    if origin == "inline":
        await dispatcher.click("custom_limit:alice")
    else:
        await dispatcher.text("/set_special_limit")
    assert any("permission" in text for text in dispatcher.messages)
    dispatcher.admins.add(7)
    await dispatcher.text("alice")
    await dispatcher.text("3")
    assert dispatcher.limits == {}


async def test_native_document_owner_is_scoped_and_ends_after_rejection(dispatcher):
    await dispatcher.text("/migrate_backup", topic=10)
    await dispatcher.document(topic=20)
    assert not any("Please send a .json file" in text for text in dispatcher.messages)
    await dispatcher.document(topic=10)
    assert sum("Please send a .json file" in text for text in dispatcher.messages) == 1
    await dispatcher.document(topic=10)
    assert sum("Please send a .json file" in text for text in dispatcher.messages) == 1


async def test_revoked_admin_cannot_continue_native_document_flow(dispatcher):
    await dispatcher.text("/migrate_backup")
    dispatcher.admins.clear()
    await dispatcher.document()
    assert "permission" in dispatcher.messages[-1]
    dispatcher.admins.add(7)
    await dispatcher.document()
    assert not any("Please send a .json file" in text for text in dispatcher.messages)


async def test_old_inline_reply_cannot_complete_native_command(dispatcher):
    await dispatcher.click("custom_limit:alice", prompt=50)
    await dispatcher.text("/set_special_limit")
    await dispatcher.text("bob")
    current_prompt = dispatcher.sent_ids[-1]
    await dispatcher.text("8", reply_to=50)
    assert dispatcher.limits == {}
    await dispatcher.text("3", reply_to=current_prompt)
    assert dispatcher.limits == {"bob": 3}


async def test_old_command_reply_cannot_complete_reentered_command(dispatcher):
    await dispatcher.text("/set_special_limit")
    await dispatcher.text("alice")
    old_prompt = dispatcher.sent_ids[-1]
    await dispatcher.text("/set_special_limit")
    username_prompt = dispatcher.sent_ids[-1]
    await dispatcher.text("bob", reply_to=username_prompt)
    current_prompt = dispatcher.sent_ids[-1]
    await dispatcher.text("8", reply_to=old_prompt)
    assert dispatcher.limits == {}
    await dispatcher.text("3", reply_to=current_prompt)
    assert dispatcher.limits == {"bob": 3}


@pytest.mark.parametrize(
    ("command", "value", "target", "expected"),
    [
        ("/add_admin", "8", "admins", {7, 8}),
        ("/remove_admin", "8", "admins", {7}),
        ("/set_general_limit_number", "3", "config", {"general_limit": 3}),
        ("/set_except_user", "bob", "except_users", {"bob"}),
        ("/remove_except_user", "bob", "except_users", set()),
        ("/set_ipinfo_token", "test-token-123", "config", {"ipinfo_token": "test-token-123"}),
    ],
)
async def test_native_entry_rejects_retired_prompt_and_accepts_delivered_prompt(
    dispatcher, command, value, target, expected
):
    if command == "/remove_admin":
        dispatcher.admins.add(8)
    if command == "/remove_except_user":
        dispatcher.except_users.add("bob")
    await dispatcher.click("custom_limit:alice", prompt=50)
    await dispatcher.text(command)
    current_prompt = dispatcher.sent_ids[-1]
    before = getattr(dispatcher, target).copy()
    await dispatcher.text(value, reply_to=50)
    assert getattr(dispatcher, target) == before
    await dispatcher.text(value, reply_to=current_prompt)
    if target == "config":
        assert all(dispatcher.config[key] == value for key, value in expected.items())
    else:
        assert getattr(dispatcher, target) == expected


async def test_each_panel_step_rejects_earlier_prompt_and_accepts_current_reply(dispatcher, monkeypatch):
    monkeypatch.setattr("utils.read_config.load_env_config", lambda: {})
    await dispatcher.text("/create_config")
    domain_prompt = dispatcher.sent_ids[-1]
    await dispatcher.text("wrong.example", reply_to=50)
    assert dispatcher.sent_ids[-1] == domain_prompt
    await dispatcher.text("panel.example", reply_to=domain_prompt)
    username_prompt = dispatcher.sent_ids[-1]
    await dispatcher.text("wrong-user", reply_to=domain_prompt)
    assert dispatcher.sent_ids[-1] == username_prompt
    await dispatcher.text("operator", reply_to=username_prompt)
    password_prompt = dispatcher.sent_ids[-1]
    await dispatcher.text("wrong-password", reply_to=username_prompt)
    assert "panel" not in dispatcher.config
    await dispatcher.text("test-password", reply_to=password_prompt)
    assert dispatcher.config["panel"] == {
        "domain": "panel.example",
        "username": "operator",
        "password": "test-password",
    }


async def test_native_numeric_step_rejects_earlier_step_prompt(dispatcher):
    await dispatcher.text("/set_special_limit")
    username_prompt = dispatcher.sent_ids[-1]
    await dispatcher.text("bob", reply_to=username_prompt)
    number_prompt = dispatcher.sent_ids[-1]
    await dispatcher.text("8", reply_to=username_prompt)
    assert dispatcher.limits == {}
    await dispatcher.text("3", reply_to=number_prompt)
    assert dispatcher.limits == {"bob": 3}


@pytest.mark.parametrize("command", ["/restore", "/migrate_backup"])
async def test_native_document_entry_rejects_retired_prompt(dispatcher, command):
    await dispatcher.click("custom_limit:alice", prompt=50)
    await dispatcher.text(command)
    current_prompt = dispatcher.sent_ids[-1]
    before = len(dispatcher.messages)
    await dispatcher.document(reply_to=50)
    assert len(dispatcher.messages) == before
    await dispatcher.document(reply_to=current_prompt)
    assert len(dispatcher.messages) == before + 1
    await dispatcher.document(reply_to=current_prompt)
    assert len(dispatcher.messages) == before + 1


async def test_native_document_retry_retires_previous_prompt(dispatcher):
    await dispatcher.text("/migrate_backup")
    initial_prompt = dispatcher.sent_ids[-1]
    await dispatcher.text("not a file", reply_to=initial_prompt)
    retry_prompt = dispatcher.sent_ids[-1]
    assert retry_prompt != initial_prompt
    before = len(dispatcher.messages)
    await dispatcher.document(reply_to=initial_prompt)
    assert len(dispatcher.messages) == before
    await dispatcher.document(reply_to=retry_prompt)
    assert len(dispatcher.messages) == before + 1


async def test_another_users_reply_cannot_consume_current_command(dispatcher):
    dispatcher.admins.add(8)
    await dispatcher.text("/set_special_limit")
    username_prompt = dispatcher.sent_ids[-1]
    await dispatcher.text("alice", user=8, reply_to=username_prompt)
    await dispatcher.text("bob", reply_to=username_prompt)
    await dispatcher.text("3", reply_to=dispatcher.sent_ids[-1])
    assert dispatcher.limits == {"bob": 3}


async def test_rejected_freshness_reply_retires_previous_prompt(dispatcher):
    await dispatcher.click("ip_source_set_freshness", prompt=50)
    await dispatcher.text("invalid", reply_to=50)
    retry_prompt = dispatcher.sent_ids[-1]
    assert retry_prompt != 50
    await dispatcher.text("90", reply_to=50)
    assert "api_ip_freshness" not in dispatcher.config
    await dispatcher.text("60", reply_to=retry_prompt)
    assert dispatcher.config["api_ip_freshness"] == "60"
