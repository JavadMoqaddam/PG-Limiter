"""Notifications management through real PTB routing, SQLite and offline Telegram."""

import importlib
import json
from itertools import count
from types import SimpleNamespace

import pytest
from telegram import Update
from telegram.ext import ApplicationBuilder

import db.database as database
from db.crud.config import ConfigCRUD
from test_config_revisions import settings_db  # noqa: F401
from test_telegram_input_ownership import TelegramTransport
from utils import mirza_settings as settings


class MenuTransport(TelegramTransport):
    """Keep the Telegram boundary offline and record visible messages/buttons."""

    def __init__(self):
        super().__init__()
        self.requests = []

    async def do_request(self, url, method, request_data=None, **kwargs):
        endpoint = url.rsplit("/", 1)[-1]
        params = request_data.parameters if request_data else {}
        self.requests.append((endpoint, params))
        return await super().do_request(url, method, request_data, **kwargs)


@pytest.fixture
async def menu(settings_db, monkeypatch):
    import telegram_bot.handlers.admin as admin_handlers
    import telegram_bot.main as main

    transport = MenuTransport()
    build = ApplicationBuilder.build
    monkeypatch.setattr(
        ApplicationBuilder,
        "build",
        lambda builder: build(builder.request(transport).get_updates_request(MenuTransport())),
    )
    monkeypatch.delenv("BOT_TOKEN", raising=False)
    importlib.reload(main)
    admins = {7, 8}
    errors = []

    async def get_admins():
        return list(admins)

    async def collect_error(update, context):
        errors.append(context.error)

    monkeypatch.setattr(main, "check_admin", get_admins)
    monkeypatch.setattr(admin_handlers, "check_admin", get_admins)
    monkeypatch.setattr(database, "DB_AVAILABLE", True)
    async with database.get_db() as db:
        await ConfigCRUD.set(
            db,
            "punishment_steps",
            json.dumps(
                [
                    {"type": "warning", "duration": 0},
                    {"type": "disable", "duration": 10},
                    {"type": "revoke", "duration": 0},
                ]
            ),
        )
    app = main.application
    app.add_error_handler(collect_error)
    await app.initialize()
    ids = count(1)

    def message(text, *, chat=7, user=7, topic=None, reply_to=None, message_id=None, private=True):
        value = {
            "message_id": message_id or next(ids),
            "date": 1,
            "from": {"id": user, "is_bot": False, "first_name": "Admin"},
            "chat": {"id": chat, "type": "private" if private else "supergroup"},
            "text": text,
        }
        if topic:
            value["message_thread_id"] = topic
        if reply_to:
            value["reply_to_message"] = {
                "message_id": reply_to,
                "date": 1,
                "chat": value["chat"],
                "text": "Earlier prompt",
                "from": {"id": 99, "is_bot": True, "first_name": "Limiter"},
            }
        if text.startswith("/"):
            value["entities"] = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
        return value

    async def click(data, *, user=7, prompt=50, **kwargs):
        update = Update.de_json(
            {
                "update_id": next(ids),
                "callback_query": {
                    "id": str(next(ids)),
                    "chat_instance": "offline",
                    "data": data,
                    "from": {"id": user, "is_bot": False, "first_name": "Admin"},
                    "message": message("Screen", user=user, message_id=prompt, **kwargs),
                },
            },
            app.bot,
        )
        await app.process_update(update)

    async def text(value, **kwargs):
        update = Update.de_json({"update_id": next(ids), "message": message(value, **kwargs)}, app.bot)
        await app.process_update(update)

    def buttons():
        params = next(
            params
            for endpoint, params in reversed(transport.requests)
            if endpoint in {"sendMessage", "editMessageText"}
        )
        markup = params.get("reply_markup", {"inline_keyboard": []})
        if isinstance(markup, str):
            markup = json.loads(markup)
        return [button for row in markup["inline_keyboard"] for button in row]

    def button(label):
        return next(item["callback_data"] for item in buttons() if label in item["text"])

    yield SimpleNamespace(
        click=click,
        text=text,
        buttons=buttons,
        button=button,
        admins=admins,
        messages=transport.messages,
        requests=transport.requests,
        app=app,
        main=main,
    )
    await app.shutdown()
    assert not errors


async def complete_route(admin="alice", **changes):
    route = await settings.create_route(admin)
    return await settings.update_route(
        route["id"],
        {
            "base_url": "https://bot.example.com/api",
            "api_key": "private-test-key",
            "message_text": "[Username] [Limit]",
            **changes,
        },
    )


async def open_route(menu, route):
    await menu.click("notifications_menu")
    await menu.click(menu.button(route["admin_username"]))


async def test_private_admin_can_add_and_edit_own_route_without_a_test_send(menu, caplog):
    bob = await complete_route("bob", message_text="Bob unchanged")
    await menu.click("settings_menu")
    await menu.click(menu.button("Notifications"))
    await menu.click(menu.button("Add Admin"))
    await menu.text("alice")
    route = next(route for route in await settings.list_routes() if route["admin_username"] == "alice")
    assert not route["enabled"]
    for label, value in [
        ("URL", "https://alice.example.com/"),
        ("API Key", "alice-private-key"),
        ("Message", "<b>[Username]</b> allowed [Limit]"),
    ]:
        await menu.click(menu.button(label))
        await menu.text(value)
    await menu.click(menu.button("Enable"))
    saved = await settings.get_route(route["id"])
    assert saved["enabled"] and saved["key_configured"]
    assert saved["base_url"] == "https://alice.example.com/api"
    assert saved["message_text"] == "<b>[Username]</b> allowed [Limit]"
    assert await settings.get_route(bob["id"]) == bob
    assert any(endpoint == "deleteMessage" for endpoint, _ in menu.requests)
    assert "alice-private-key" not in repr(menu.requests) + caplog.text
    assert "private-test-key" not in repr(menu.requests) + caplog.text


@pytest.mark.parametrize("kwargs", [{"user": 99}, {"private": False, "chat": -100}])
async def test_menu_rejects_unauthorized_or_public_chat(menu, kwargs):
    await menu.click("notifications_menu", **kwargs)
    assert "Add Admin" not in repr(menu.buttons())
    assert await settings.list_routes() == []


@pytest.mark.parametrize("kwargs", [{"chat": 8}, {"topic": 42}, {"user": 8}, {"reply_to": 999}, {"private": False}])
async def test_text_from_wrong_origin_cannot_edit_route(menu, kwargs):
    route = await complete_route()
    await open_route(menu, route)
    await menu.click(menu.button("URL"))
    await menu.text("https://wrong.example.com", **kwargs)
    assert await settings.get_route(route["id"]) == route


async def test_revoked_admin_cannot_finish_key_entry(menu):
    route = await complete_route()
    await open_route(menu, route)
    await menu.click(menu.button("API Key"))
    menu.admins.remove(7)
    await menu.text("revoked-key")
    assert await settings.get_route(route["id"]) == route
    assert not menu.app.user_data[7]


@pytest.mark.parametrize("navigation", ["/cancel", "/start", "settings_menu", "notifications_menu"])
async def test_cancel_command_or_navigation_clears_all_pending_notification_fields(menu, navigation):
    route = await complete_route()
    await open_route(menu, route)
    await menu.click(menu.button("Message"))
    assert menu.app.user_data[7].get("waiting_for")
    if navigation.startswith("/"):
        await menu.text(navigation)
    else:
        await menu.click(navigation)
    await menu.text("late message")
    assert await settings.get_route(route["id"]) == route
    assert not any(key.startswith("mirza_") for key in menu.app.user_data[7])


async def test_invalid_key_reprompts_and_rebinds_ownership_without_echo(menu, caplog):
    route = await complete_route()
    await open_route(menu, route)
    await menu.click(menu.button("API Key"), prompt=80)
    await menu.text("bad secret with space")
    prompt = menu.app.user_data[7]["input_owner"].prompt_id
    assert prompt != 80
    await menu.text("replacement-secret", reply_to=prompt)
    assert (await settings.get_route(route["id"]))["revision"] == route["revision"] + 1
    assert "bad secret with space" not in repr(menu.requests) + caplog.text
    assert "replacement-secret" not in repr(menu.requests) + caplog.text


async def test_enable_incomplete_route_explains_required_fields(menu):
    route = await settings.create_route("alice")
    await open_route(menu, route)
    await menu.click(menu.button("Enable"))
    assert not (await settings.get_route(route["id"]))["enabled"]
    assert all(word in menu.messages[-1].lower() for word in ["url", "key", "message"])
    assert any("Back" in button["text"] for button in menu.buttons())


async def test_multi_selection_changes_independent_steps_and_dynamic_last(menu):
    route = await complete_route(enabled=True)
    await open_route(menu, route)
    await menu.click(menu.button("Triggers"))
    assert "dynamic" in menu.messages[-1].lower()
    assert "Warning" in repr(menu.buttons()) and "10" in repr(menu.buttons())
    assert (await settings.get_route(route["id"]))["trigger_mode"] == "last"
    await menu.click(menu.button("Step 1"))
    await menu.click(menu.button("Step 3"))
    selected = await settings.get_route(route["id"])
    assert selected["enabled"] and selected["selected_steps"] == [0, 2]
    assert sum("✅" in b["text"] for b in menu.buttons() if "Step" in b["text"]) == 2
    await menu.click(menu.button("Step 1"))
    assert (await settings.get_route(route["id"]))["selected_steps"] == [2]
    await menu.click(menu.button("Step 3"))
    assert (await settings.get_route(route["id"]))["selected_steps"] == [2]
    assert "at least one" in menu.messages[-1].lower()
    await menu.click(menu.button("Last step"))
    assert (await settings.get_route(route["id"]))["trigger_mode"] == "last"


async def test_selected_removed_steps_are_visible_and_can_be_removed_without_remapping(menu):
    route = await complete_route(trigger_mode="selected", selected_steps=[0, 8])
    await open_route(menu, route)
    await menu.click(menu.button("Triggers"))
    assert "Step 9" in repr(menu.buttons()) and "inactive" in repr(menu.buttons()).lower()
    await menu.click(menu.button("Step 9"))
    assert (await settings.get_route(route["id"]))["selected_steps"] == [0]


@pytest.mark.parametrize("action", ["Enable", "Delete", "URL"])
async def test_stale_callbacks_do_not_mutate_newer_route_revision(menu, action):
    route = await complete_route()
    await open_route(menu, route)
    payload = menu.button(action)
    newer = await settings.update_route(route["id"], {"message_text": "Newer settings"})
    await menu.click(payload)
    await menu.text("https://stale.example.com")
    assert await settings.get_route(route["id"]) == newer
    assert "changed" in " ".join(menu.messages[-2:]).lower()


async def test_stale_text_and_delete_confirmation_cannot_overwrite_settings(menu):
    route = await complete_route()
    await open_route(menu, route)
    await menu.click(menu.button("Message"))
    newer = await settings.update_route(route["id"], {"message_text": "Newer settings"})
    await menu.text("Stale message")
    assert await settings.get_route(route["id"]) == newer
    await open_route(menu, newer)
    await menu.click(menu.button("Delete"))
    confirm = menu.button("Confirm")
    newest = await settings.update_route(route["id"], {"message_text": "Newest settings"})
    await menu.click(confirm)
    assert await settings.get_route(route["id"]) == newest
    await open_route(menu, newest)
    await menu.click(menu.button("Delete"))
    await menu.click(menu.button("Confirm"))
    assert await settings.get_route(route["id"]) is None
    assert "Add Admin" in repr(menu.buttons())


@pytest.mark.parametrize(
    "data",
    [
        "mn:r:not-uuid:1",
        "mn:r:11111111-1111-4111-8111-111111111111:1",
        "mn:p:-1",
        "mn:p:invalid",
        "mn:s:11111111-1111-4111-8111-111111111111:1:99",
        "mn:unknown",
        "mn:r:11111111-1111-4111-8111-111111111111:invalid",
    ],
)
async def test_invalid_or_deleted_route_callbacks_offer_safe_navigation(menu, data):
    await menu.click(data)
    assert await settings.list_routes() == []
    assert any("Back" in item["text"] for item in menu.buttons())
    assert data not in menu.messages[-1]


async def test_routes_paginate_ten_at_a_time_with_short_secret_free_callbacks(menu):
    routes = [await settings.create_route(f"admin-{index:02}") for index in range(23)]
    await menu.click("notifications_menu")
    assert sum("admin-" in b["text"] for b in menu.buttons()) == 10
    await menu.click(menu.button("Next"))
    assert sum("admin-" in b["text"] for b in menu.buttons()) == 10
    assert "admin-10" in repr(menu.buttons()) and "admin-00" not in repr(menu.buttons())
    await menu.click(menu.button("Next"))
    assert sum("admin-" in b["text"] for b in menu.buttons()) == 3
    await open_route(menu, routes[0])
    await menu.click(menu.button("Triggers"))
    for endpoint, params in menu.requests:
        if endpoint in {"sendMessage", "editMessageText"} and params.get("reply_markup"):
            markup = params["reply_markup"]
            if isinstance(markup, str):
                markup = json.loads(markup)
            assert all(len(b["callback_data"].encode()) <= 64 for row in markup["inline_keyboard"] for b in row)


async def test_long_html_template_and_url_have_safe_bounded_previews_without_storage_loss(menu):
    template = "😀<>&" * 819
    url = "https://bot.example.com/" + "x" * 3500
    route = await complete_route("admin<>&", message_text=template, base_url=url)
    await open_route(menu, route)
    assert "admin&lt;&gt;&amp;" in menu.messages[-1]
    assert "&lt;&gt;&amp;" in menu.messages[-1]
    assert len(menu.messages[-1].encode("utf-16-le")) // 2 <= 4096
    await menu.click(menu.button("Message"))
    assert "[Username]" in menu.messages[-1] and "[Limit]" in menu.messages[-1]
    assert len(menu.messages[-1].encode("utf-16-le")) // 2 <= 4096
    assert await settings.get_route(route["id"]) == route


async def test_menu_shows_real_aggregate_outbox_status_counts(menu):
    from db.models import MirzaNotification

    async with database.get_db() as db:
        for index, state in enumerate(["pending", "delivered", "delivered", "failed", "skipped", "uncertain"]):
            db.add(
                MirzaNotification(
                    incident_id=f"incident-{index}", username="alice", applied_at=1, context={}, state=state
                )
            )
    await menu.click("notifications_menu")
    assert "Delivered: 2" in menu.messages[-1]
    assert all(f"{label}: 1" in menu.messages[-1] for label in ["Pending", "Failed", "Skipped", "Uncertain"])


@pytest.mark.parametrize("kwargs", [{"user": 99}, {"chat": -100, "private": False}])
async def test_unauthorized_or_public_callback_cannot_toggle_existing_route(menu, kwargs):
    route = await complete_route()
    await open_route(menu, route)
    payload = menu.button("Enable")
    await menu.click(payload, **kwargs)
    assert await settings.get_route(route["id"]) == route


async def test_disabling_through_ui_cancels_only_that_route_unsent_events(menu):
    from db.crud.mirza_notifications import MirzaNotificationsCRUD
    from db.models import MirzaNotification

    alice = await complete_route(enabled=True)
    bob = await complete_route("bob", enabled=True)
    async with database.get_db() as db:
        for route in [alice, bob]:
            db.add(
                MirzaNotification(
                    incident_id=route["id"],
                    username="customer",
                    applied_at=1,
                    context={},
                    state="ready",
                    route_id=route["id"],
                    route_revision=route["revision"],
                )
            )
    await open_route(menu, alice)
    await menu.click(menu.button("Disable"))
    saved = await settings.get_route(alice["id"])
    assert not saved["enabled"] and saved["revision"] == alice["revision"] + 1
    async with database.get_db() as db:
        assert (await MirzaNotificationsCRUD.get(db, alice["id"])).state == "skipped"
        assert (await MirzaNotificationsCRUD.get(db, bob["id"])).state == "ready"
    assert await settings.get_route(bob["id"]) == bob


async def test_current_ladder_changes_leave_selected_positions_visible_as_inactive(menu):
    route = await complete_route(trigger_mode="selected", selected_steps=[0, 2])
    await open_route(menu, route)
    async with database.get_db() as db:
        await ConfigCRUD.set(db, "punishment_steps", json.dumps([{"type": "warning", "duration": 0}]))
    await menu.click(menu.button("Triggers"))
    assert "Step 3: inactive" in repr(menu.buttons())
    assert (await settings.get_route(route["id"]))["selected_steps"] == [0, 2]
    await menu.click(menu.button("Step 3"))
    assert (await settings.get_route(route["id"]))["selected_steps"] == [0]


async def test_key_entry_database_failure_is_localized_and_does_not_echo_key(menu, monkeypatch, caplog):
    route = await complete_route()
    await open_route(menu, route)
    await menu.click(menu.button("API Key"))
    monkeypatch.setattr(database, "DB_AVAILABLE", False)
    await menu.text("never-display-this-key")
    monkeypatch.setattr(database, "DB_AVAILABLE", True)
    assert await settings.get_route(route["id"]) == route
    assert "Could not save" in menu.messages[-1]
    assert "never-display-this-key" not in repr(menu.requests) + caplog.text
    assert not menu.app.user_data[7]
    await menu.click("notifications_menu")
    assert "Add Admin" in repr(menu.buttons())


async def test_valid_routes_at_largest_sqlite_revision_keep_callbacks_within_64_bytes(menu):
    from sqlalchemy import update
    from db.models import MirzaRoute

    route = await complete_route()
    async with database.get_db() as db:
        await db.execute(update(MirzaRoute).where(MirzaRoute.id == route["id"]).values(revision=2**63 - 1))
    await open_route(menu, route)
    for label in ["Triggers", "Back to Admin", "Message", "Back to Admin", "Delete"]:
        await menu.click(menu.button(label))
        assert all(len(button["callback_data"].encode()) <= 64 for button in menu.buttons())
