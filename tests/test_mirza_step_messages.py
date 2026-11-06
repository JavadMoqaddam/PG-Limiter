"""Explicit per-step message selection under the existing bot revision boundary."""

import asyncio
from copy import deepcopy

import pytest

from test_mirza_settings import route_database  # noqa: F401
from utils import mirza_settings as settings


async def add_message(bot, name, text="Hello [Username]", steps=None):
    bot = await settings.create_message(bot["id"], name, expected_revision=bot["revision"])
    message_id = bot["messages"][-1]["id"]
    return await settings.update_message(
        bot["id"],
        message_id,
        {"message_text": text, "selected_steps": steps if steps is not None else [0]},
        expected_revision=bot["revision"],
    )


async def enabled_bot(name="Sales", steps=None):
    bot = await add_message(await settings.create_route(name), "Notice", steps=steps)
    return await settings.update_route(
        bot["id"],
        {
            "admin_usernames": [name],
            "base_url": "https://sales.example/api",
            "api_key": "private-test-key",
            "enabled": True,
        },
    )


async def test_bot_public_contract_contains_messages_without_old_scalar_triggers(route_database):
    bot = await settings.create_route("Sales")
    assert bot["messages"] == []
    assert not {"message_text", "trigger_mode", "selected_steps", "api_key"}.intersection(bot)
    bot = await add_message(bot, "  Warning  ", steps=[2, 0, 2])
    message = bot["messages"][0]
    assert message["name"] == "Warning" and message["selected_steps"] == [0, 2]
    assert set(message) == {"id", "name", "message_text", "selected_steps"}
    message["selected_steps"].append(4)
    assert (await settings.get_route(bot["id"]))["messages"][0]["selected_steps"] == [0, 2]


async def test_numbered_assignments_are_exclusive_even_for_text_empty_drafts(route_database):
    bot = await add_message(await settings.create_route("Sales"), "First", text="", steps=[1, 3])
    bot = await settings.create_message(bot["id"], "Second", expected_revision=bot["revision"])
    with pytest.raises(ValueError, match="assigned"):
        await settings.update_message(
            bot["id"], bot["messages"][1]["id"], {"selected_steps": [0, 3]}, expected_revision=bot["revision"]
        )
    assert await settings.get_route(bot["id"]) == bot


async def test_names_are_unique_per_bot_and_other_bot_can_reuse_them(route_database):
    bot = await add_message(await settings.create_route("One"), "Notice")
    with pytest.raises(ValueError, match="name"):
        await settings.create_message(bot["id"], " Notice ", expected_revision=bot["revision"])
    other = await add_message(await settings.create_route("Two"), "Notice")
    assert other["messages"][0]["id"] != bot["messages"][0]["id"]


@pytest.mark.parametrize(
    "changes",
    [
        {"name": ""},
        {"name": "x\x00y"},
        {"name": "x" * 256},
        {"message_text": None},
        {"message_text": "😀" * 2049},
        {"selected_steps": [True]},
        {"selected_steps": [-1]},
        {"selected_steps": [10]},
        {"selected_steps": "1"},
        {"trigger_mode": "last"},
        {"api_key": "secret"},
    ],
)
async def test_invalid_message_changes_leave_bot_untouched(route_database, changes):
    bot = await add_message(await settings.create_route("Sales"), "Notice")
    with pytest.raises(ValueError):
        await settings.update_message(bot["id"], bot["messages"][0]["id"], changes, expected_revision=bot["revision"])
    assert await settings.get_route(bot["id"]) == bot


async def test_noop_selection_does_not_increment_revision_and_stale_message_edits_fail(route_database):
    bot = await add_message(await settings.create_route("Sales"), "Notice", steps=[0, 2])
    message_id = bot["messages"][0]["id"]
    assert (
        await settings.update_message(
            bot["id"], message_id, {"selected_steps": [2, 0, 2]}, expected_revision=bot["revision"]
        )
        == bot
    )
    results = await asyncio.gather(
        *(
            settings.update_message(bot["id"], message_id, {"message_text": value}, expected_revision=bot["revision"])
            for value in ("Winner one", "Winner two")
        ),
        return_exceptions=True,
    )
    winner = next(value for value in results if isinstance(value, dict))
    assert sum(isinstance(value, ValueError) for value in results) == 1
    assert await settings.get_route(bot["id"]) == winner


async def test_drafts_do_not_block_enabled_messages_but_last_ready_cannot_be_removed(route_database):
    bot = await enabled_bot()
    bot = await settings.create_message(bot["id"], "Draft", expected_revision=bot["revision"])
    assert bot["enabled"]
    with pytest.raises(ValueError, match="message"):
        await settings.delete_message(bot["id"], bot["messages"][0]["id"], expected_revision=bot["revision"])
    assert await settings.get_route(bot["id"]) == bot
    bot = await settings.update_route(bot["id"], {"enabled": False})
    old_id = bot["messages"][0]["id"]
    bot = await settings.delete_message(bot["id"], old_id, expected_revision=bot["revision"])
    bot = await add_message(bot, "Notice")
    assert bot["messages"][-1]["id"] != old_id


async def test_capture_pins_one_message_and_numbered_mapping_never_follows_last_stage(route_database):
    bot = await enabled_bot(steps=[0, 2])
    bot = await add_message(bot, "Final notice", text="Final [Limit]", steps=[4])
    rules = await settings.capture_rules()
    first = settings.notification_context(rules, 2, 5, "disable", 3)
    assert first["candidates"] == [
        {
            "route_id": bot["id"],
            "revision": bot["revision"],
            "admin_usernames": ["Sales"],
            "message_id": bot["messages"][0]["id"],
        }
    ]
    final = settings.notification_context(rules, 4, 5, "revoke", 3)
    assert final["candidates"][0]["message_id"] == bot["messages"][1]["id"]
    assert settings.notification_context(rules, 3, 4, "revoke", 3) is None
    assert settings.notification_context(rules, 1, 5, "warning", 3) is None
    assert "private-test-key" not in repr(rules) + repr(first) and "Final [Limit]" not in repr(rules)
    frozen = deepcopy(first)
    await settings.update_message(
        bot["id"], bot["messages"][0]["id"], {"message_text": "Edited"}, expected_revision=bot["revision"]
    )
    assert first == frozen
    assert settings.validate_notification_context(first) == first


@pytest.mark.parametrize("owners", [{"admin_username": "owner"}, {"admin_usernames": ["owner"]}])
def test_legacy_contexts_remain_unmodified_and_new_message_id_is_validated(owners):
    context = {
        "candidates": [{"route_id": "10000000-0000-4000-8000-000000000001", "revision": 7, **owners}],
        "step_index": 1,
        "step_count": 5,
        "step_type": "disable",
        "effective_limit": 2,
    }
    assert settings.validate_notification_context(context) == context
    invalid = deepcopy(context)
    invalid["candidates"][0]["message_id"] = "invalid"
    with pytest.raises(ValueError):
        settings.validate_notification_context(invalid)


async def test_message_edit_and_receipt_cancellation_share_transaction(route_database):
    from db.crud.mirza_routes import MirzaRoutesCRUD
    from db.models import MirzaNotification

    bot = await enabled_bot()
    async with route_database() as db:
        db.add(
            MirzaNotification(
                incident_id="ready",
                username="customer",
                applied_at=1,
                context={},
                state="ready",
                route_id=bot["id"],
                route_revision=bot["revision"],
            )
        )
        await db.commit()
    messages = deepcopy(bot["messages"])
    messages[0]["message_text"] = "Edited"
    async with route_database() as db:
        await MirzaRoutesCRUD.update(db, bot["id"], {"messages": messages}, expected_revision=bot["revision"])
        await db.rollback()
    assert await settings.get_route(bot["id"]) == bot
    async with route_database() as db:
        assert (await db.get(MirzaNotification, "ready")).state == "ready"
    await settings.update_message(
        bot["id"], messages[0]["id"], {"message_text": "Edited"}, expected_revision=bot["revision"]
    )
    async with route_database() as db:
        assert (await db.get(MirzaNotification, "ready")).state == "skipped"


async def test_worker_renders_exact_message_pinned_for_each_incident(route_database, monkeypatch, tmp_path):
    import json
    import httpx
    from sqlalchemy import select
    from db.models import MirzaNotification
    from test_mirza_delivery import service_with_transport
    from utils.punishment_facts import PunishmentFactJournal
    import utils.punishment_facts as facts
    import utils.panel_api.users as users

    bot = await enabled_bot(steps=[0])
    bot = await add_message(bot, "Revoke", text="Revoked [Username], [Limit] devices", steps=[2])
    rules = await settings.capture_rules()
    monkeypatch.setattr(facts, "_journal", PunishmentFactJournal(str(tmp_path / "journal.json")))

    async def details(*_):
        return {"admin": {"username": "Sales"}, "note": "5123456789 | handle | buy"}

    monkeypatch.setattr(users, "get_user_details", details)
    async with route_database() as db:
        for incident, step in [("first", 0), ("last", 2), ("last-again", 2)]:
            context = settings.notification_context(rules, step, 3, "disable", 2)
            db.add(MirzaNotification(incident_id=incident, username="config<&", applied_at=1, context=context))
        await db.commit()
    requests = []
    service = service_with_transport(
        lambda request: requests.append(request) or httpx.Response(200, json={"status": True})
    )
    try:
        await service.run_once()
        await service.run_once()
    finally:
        await service.close()
    assert [json.loads(request.content)["text"] for request in requests] == [
        "Hello config&lt;&amp;",
        "Revoked config&lt;&amp;, 2 devices",
        "Revoked config&lt;&amp;, 2 devices",
    ]
    async with route_database() as db:
        assert {row.state for row in await db.scalars(select(MirzaNotification))} == {"delivered"}


@pytest.mark.parametrize("migrated", [False, True])
async def test_legacy_candidate_only_uses_migrated_identity_not_replacement_message(
    route_database, monkeypatch, tmp_path, migrated
):
    import httpx
    from sqlalchemy import update
    from db.models import MirzaNotification, MirzaRoute
    from test_mirza_delivery import service_with_transport
    from utils.punishment_facts import PunishmentFactJournal
    import utils.punishment_facts as facts
    import utils.panel_api.users as users

    bot = await enabled_bot()
    if migrated:
        messages = deepcopy(bot["messages"])
        messages[0]["id"] = bot["id"]
        async with route_database() as db:
            await db.execute(update(MirzaRoute).where(MirzaRoute.id == bot["id"]).values(messages=messages))
            await db.commit()
    context = {
        "candidates": [{"route_id": bot["id"], "revision": bot["revision"], "admin_username": "Sales"}],
        "step_index": 0,
        "step_count": 1,
        "step_type": "disable",
        "effective_limit": 2,
    }
    monkeypatch.setattr(facts, "_journal", PunishmentFactJournal(str(tmp_path / "journal.json")))

    async def details(*_):
        return {"admin": "Sales", "note": "5123456789 | handle | buy"}

    monkeypatch.setattr(users, "get_user_details", details)
    async with route_database() as db:
        db.add(MirzaNotification(incident_id="legacy", username="config", applied_at=1, context=context))
        await db.commit()
    requests = []
    service = service_with_transport(
        lambda request: requests.append(request) or httpx.Response(200, json={"status": True})
    )
    try:
        await service.run_once()
    finally:
        await service.close()
    assert len(requests) == int(migrated)
    async with route_database() as db:
        assert (await db.get(MirzaNotification, "legacy")).state == ("delivered" if migrated else "skipped")
