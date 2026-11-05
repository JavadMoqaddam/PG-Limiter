"""Named Mirza message management through authorized Telegram routing."""

import json

import pytest

import db.database as database
from db.crud.config import ConfigCRUD
from test_config_revisions import settings_db  # noqa: F401
from test_mirza_notification_menu import complete_route, menu, open_route  # noqa: F401
from utils import mirza_settings as settings


async def open_message(menu, route, name="Default message"):
    await open_route(menu, route)
    await menu.click(menu.button("Messages"))
    await menu.click(menu.button(name))


async def test_new_bot_has_message_list_and_adds_named_draft(menu):
    route = await settings.create_route("Sales Bot")
    await open_route(menu, route)
    assert "Messages" in repr(menu.buttons())
    assert "Triggers" not in repr(menu.buttons())
    await menu.click(menu.button("Messages"))
    assert "Add Message" in repr(menu.buttons())
    await menu.click(menu.button("Add Message"))
    await menu.text("First warning")
    saved = await settings.get_route(route["id"])
    assert len(saved["messages"]) == 1
    assert saved["messages"][0]["name"] == "First warning"
    assert saved["messages"][0]["message_text"] == ""
    assert saved["messages"][0]["selected_steps"] == []
    assert "draft" in menu.messages[-1].lower()


async def test_named_message_can_edit_text_rename_and_cover_multiple_explicit_steps(menu):
    route = await complete_route()
    await open_message(menu, route)
    await menu.click(menu.button("Text"))
    await menu.text("<b>[Username]</b> allowed [Limit]")
    await menu.click(menu.button("Rename"))
    await menu.text("Reminder <East>")
    assert "Reminder &lt;East&gt;" in menu.messages[-1]
    await menu.click(menu.button("Steps"))
    assert not any("Last" in button["text"] for button in menu.buttons())
    await menu.click(menu.button("Step 2"))
    await menu.click(menu.button("Step 3"))
    saved = await settings.get_route(route["id"])
    assert saved["messages"][0]["name"] == "Reminder <East>"
    assert saved["messages"][0]["message_text"] == "<b>[Username]</b> allowed [Limit]"
    assert saved["messages"][0]["selected_steps"] == [0, 1, 2]


async def test_steps_owned_by_another_message_are_locked_without_changing_data(menu):
    route = await complete_route()
    route = await settings.create_message(route["id"], "Second", expected_revision=route["revision"])
    await open_message(menu, route, "Second")
    await menu.click(menu.button("Steps"))
    button = next(button for button in menu.buttons() if "Step 1" in button["text"])
    assert "🔒" in button["text"] and "Default message" in button["text"]
    await menu.click(button["callback_data"])
    assert await settings.get_route(route["id"]) == route
    assert "another message" in menu.messages[-1].lower()


async def test_message_delete_is_confirmed_and_uses_bot_revision(menu):
    route = await complete_route()
    await open_message(menu, route)
    await menu.click(menu.button("Delete"))
    assert await settings.get_route(route["id"]) == route
    stale = menu.button("Confirm")
    newer = await settings.update_message(
        route["id"], route["messages"][0]["id"], {"name": "Updated"}, expected_revision=route["revision"]
    )
    await menu.click(stale)
    assert await settings.get_route(route["id"]) == newer
    await open_message(menu, newer, "Updated")
    await menu.click(menu.button("Delete"))
    await menu.click(menu.button("Confirm"))
    assert (await settings.get_route(route["id"]))["messages"] == []
    assert "Add Message" in repr(menu.buttons())


@pytest.mark.parametrize("kwargs", [{"chat": 8}, {"topic": 42}, {"user": 8}, {"reply_to": 999}, {"private": False}])
async def test_message_text_requires_owned_private_origin(menu, kwargs):
    route = await complete_route()
    await open_message(menu, route)
    await menu.click(menu.button("Text"))
    assert menu.app.user_data[7]["mirza_message_id"] == route["messages"][0]["id"]
    await menu.text("Wrong origin", **kwargs)
    assert await settings.get_route(route["id"]) == route


async def test_message_cancel_cleans_identity_and_stale_text_cannot_edit(menu):
    route = await complete_route()
    await open_message(menu, route)
    await menu.click(menu.button("Text"))
    await menu.text("/cancel")
    assert not any(key.startswith("mirza_") for key in menu.app.user_data[7])
    await open_message(menu, route)
    await menu.click(menu.button("Text"))
    newer = await settings.update_route(route["id"], {"name": "Changed"}, expected_revision=route["revision"])
    await menu.text("Late text")
    assert await settings.get_route(route["id"]) == newer
    assert not menu.app.user_data[7]


@pytest.mark.parametrize("action", ["m", "t", "l", "s"])
async def test_old_message_and_trigger_buttons_open_list_without_mutation(menu, action):
    route = await complete_route()
    data = f"mn:{action}:{route['id']}:{route['revision']}" + (":2" if action == "s" else "")
    await menu.click(data)
    assert await settings.get_route(route["id"]) == route
    assert "Add Message" in repr(menu.buttons())
    assert not menu.app.user_data[7].get("waiting_for")


@pytest.mark.parametrize(
    "raw, labels",
    [
        ([None, {"type": "unknown"}, {"type": "disable"}], ["Step 1: ⚠️ Warning", "Step 2: 🔒 10 minute"]),
        ([], ["Step 1: ⚠️ Warning", "Step 5: 🚫 Unlimited"]),
        ([None, "bad"], ["Step 1: ⚠️ Warning", "Step 5: 🚫 Unlimited"]),
    ],
)
async def test_picker_describes_canonical_effective_ladder_without_constructing_system(menu, monkeypatch, raw, labels):
    from utils.punishment_system import PunishmentSystem

    route = await complete_route()
    async with database.get_db() as db:
        await ConfigCRUD.set(db, "punishment_steps", json.dumps(raw))

    def forbidden(*args, **kwargs):
        raise AssertionError("Menu must not construct enforcement state")

    monkeypatch.setattr(PunishmentSystem, "__init__", forbidden)
    await open_message(menu, route)
    await menu.click(menu.button("Steps"))
    for label in labels:
        assert label in repr(menu.buttons())


async def test_message_callbacks_fit_largest_revision_and_reject_identity_tampering(menu):
    from sqlalchemy import update
    from db.models import MirzaRoute

    route = await complete_route()
    async with database.get_db() as db:
        await db.execute(update(MirzaRoute).where(MirzaRoute.id == route["id"]).values(revision=2**63 - 1))
    route = await settings.get_route(route["id"])
    await open_message(menu, route)
    for label in ["Steps", "Back to Message", "Text"]:
        await menu.click(menu.button(label))
        assert all(len(button["callback_data"].encode()) <= 64 for button in menu.buttons())
    await open_message(menu, route)
    payload = menu.button("Rename")
    tampered = payload[:-1] + ("A" if payload[-1] != "A" else "B")
    await menu.click(tampered)
    await menu.text("Tampered")
    assert await settings.get_route(route["id"]) == route


async def test_ambiguous_message_token_does_not_select_or_edit_a_message(menu, monkeypatch):
    import telegram_bot.handlers.notifications as handlers

    route = await complete_route()
    route = await settings.create_message(route["id"], "Second", expected_revision=route["revision"])
    monkeypatch.setattr(handlers, "_message_token", lambda message_id: "A" * 16)
    await open_route(menu, route)
    await menu.click(menu.button("Messages"))
    await menu.click(menu.button("Second"))
    await menu.text("Cannot select")
    assert await settings.get_route(route["id"]) == route
    assert "changed" in menu.messages[-1].lower()


@pytest.mark.parametrize("kwargs", [{"user": 99}, {"chat": -100, "private": False}])
async def test_message_controls_reject_unauthorized_and_public_callbacks(menu, kwargs):
    route = await complete_route()
    await open_message(menu, route)
    payload = menu.button("Rename")
    await menu.click(payload, **kwargs)
    await menu.text("Unauthorized edit", **kwargs)
    assert await settings.get_route(route["id"]) == route


async def test_enabled_bot_can_add_drafts_but_must_disable_before_deleting_last_ready_message(menu):
    route = await complete_route(enabled=True)
    await open_route(menu, route)
    await menu.click(menu.button("Messages"))
    await menu.click(menu.button("Add Message"))
    await menu.text("Draft")
    saved = await settings.get_route(route["id"])
    assert saved["enabled"] and len(saved["messages"]) == 2
    await open_message(menu, saved)
    await menu.click(menu.button("Delete"))
    await menu.click(menu.button("Confirm"))
    assert await settings.get_route(route["id"]) == saved
    assert "disable" in menu.messages[-1].lower()
    await menu.click(menu.button("Back to Bot"))
    await menu.click(menu.button("Disable"))
    saved = await settings.get_route(route["id"])
    await open_message(menu, saved)
    await menu.click(menu.button("Delete"))
    await menu.click(menu.button("Confirm"))
    assert [message["name"] for message in (await settings.get_route(route["id"]))["messages"]] == ["Draft"]


async def test_duplicate_message_name_reprompts_with_ownership_and_does_not_mutate(menu):
    route = await complete_route()
    route = await settings.create_message(route["id"], "Second", expected_revision=route["revision"])
    await open_message(menu, route, "Second")
    await menu.click(menu.button("Rename"), prompt=80)
    await menu.text("Default message")
    assert await settings.get_route(route["id"]) == route
    pending = menu.app.user_data[7]
    assert pending["mirza_message_id"] == route["messages"][1]["id"]
    assert pending["input_owner"].prompt_id != 80
    await menu.text("Renamed", reply_to=pending["input_owner"].prompt_id)
    assert (await settings.get_route(route["id"]))["messages"][1]["name"] == "Renamed"


async def test_stage_ten_and_maximum_unicode_names_have_bounded_revision_callbacks(menu):
    from sqlalchemy import update
    from db.models import MirzaRoute

    route = await complete_route(selected_steps=[9])
    name = "فروشنده<&>" + "آ" * 245
    route = await settings.update_message(
        route["id"], route["messages"][0]["id"], {"name": name}, expected_revision=route["revision"]
    )
    async with database.get_db() as db:
        await ConfigCRUD.set(db, "punishment_steps", json.dumps([{"type": "warning"}] * 10))
        await db.execute(update(MirzaRoute).where(MirzaRoute.id == route["id"]).values(revision=2**63 - 1))
    route = await settings.get_route(route["id"])
    await open_message(menu, route, name[:40])
    await menu.click(menu.button("Steps"))
    step = next(button for button in menu.buttons() if "Step 10:" in button["text"])
    assert "✅" in step["text"] and len(step["callback_data"].encode()) == 64
    assert all(len(button["callback_data"].encode()) <= 64 for button in menu.buttons())
    assert name not in repr([button["callback_data"] for button in menu.buttons()])
