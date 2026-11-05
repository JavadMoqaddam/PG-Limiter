"""Routes validate operator input and snapshot independent secret-free rules."""

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


@pytest.fixture
async def route_database(tmp_path, monkeypatch):
    import db.database as database
    from db.models import Base

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'routes.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(database, "AsyncSessionLocal", sessions)
    monkeypatch.setattr(database, "DB_AVAILABLE", True)
    yield sessions
    await engine.dispose()


@pytest.mark.parametrize(
    "note,want",
    [
        ("5123456789 | example_user | buy", "5123456789"),
        ("  5123456789  | example_user | buy  ", "5123456789"),
        ("9999999999999999999999999 | user | buy", "9999999999999999999999999"),
        ("telegram:5123456789 | example_user | buy", None),
        ("۰۱۲۳ | example_user | buy", None),
        ("0 | example_user | buy", None),
        ("0000 | example_user | buy", None),
        ("-123 | user | buy", None),
        ("5123456789", None),
        ("user | 5123456789 | buy", None),
        ("123 | user", None),
        ("123 | user | buy | extra", None),
        (None, None),
    ],
)
def test_recipient_uses_only_positive_ascii_first_of_three_fields(note, want):
    from utils.mirza_settings import parse_recipient

    assert parse_recipient(note) == want


@pytest.mark.parametrize(
    "value,want",
    [
        ("https://bot.example.com", "https://bot.example.com/api"),
        ("https://bot.example.com/api/", "https://bot.example.com/api"),
        ("https://bot.example.com:8443/custom/v1/", "https://bot.example.com:8443/custom/v1"),
        (" https://bot.example.com/ ", "https://bot.example.com/api"),
    ],
)
def test_url_normalizes_origin_and_preserves_explicit_api_path(value, want):
    from utils.mirza_settings import normalize_base_url

    assert normalize_base_url(value) == want


@pytest.mark.parametrize(
    "value",
    [
        "http://bot.example.com",
        "https://user:pass@bot.example.com/api",
        "https://bot.example.com/api?x=1",
        "https://bot.example.com/api#x",
        "https://bot.example.com/users/",
        "https://bot.example.com:99999",
        "https://bot.example.com:",
        "https://",
        "https://bad host/api",
        "https://bot.example.com\\evil/api",
        "https://-bad.example/api",
    ],
)
def test_url_rejects_unsafe_or_endpoint_inputs(value):
    from utils.mirza_settings import normalize_base_url

    with pytest.raises(ValueError):
        normalize_base_url(value)


async def configure_route(settings, admin, url="https://bot.example.com", key="synthetic-key", text="Hello"):
    route = await settings.create_route(admin)
    return await settings.update_route(
        route["id"], {"base_url": url, "api_key": key, "message_text": text, "enabled": True}
    )


async def test_routes_hide_keys_and_versions_follow_actual_edits(route_database):
    from utils import mirza_settings as settings
    from db.crud.mirza_routes import MirzaRoutesCRUD

    route = await settings.create_route(" alice ")
    assert route["admin_username"] == "alice"
    assert (route["enabled"], route["trigger_mode"], route["selected_steps"], route["revision"]) == (
        False,
        "last",
        [],
        1,
    )
    assert route["key_configured"] is False
    with pytest.raises(ValueError):
        await settings.update_route(route["id"], {"enabled": True})
    updated = await settings.update_route(
        route["id"],
        {"base_url": "https://bot.example.com", "api_key": "synthetic-key", "message_text": "Hello", "enabled": True},
    )
    assert updated["revision"] == 2
    assert updated["key_configured"] is True
    assert "api_key" not in updated
    assert "synthetic-key" not in repr(await settings.list_routes())
    assert "api_key" not in await settings.get_route(route["id"])
    assert (await settings.update_route(route["id"], {"message_text": "Hello"}))["revision"] == 2
    async with route_database() as db:
        assert await MirzaRoutesCRUD.read_secret(db, route["id"], 1) is None
        assert await MirzaRoutesCRUD.read_secret(db, route["id"], 2) == "synthetic-key"
    await settings.update_route(route["id"], {"enabled": False})
    async with route_database() as db:
        assert await MirzaRoutesCRUD.read_secret(db, route["id"], 3) is None


async def test_duplicate_admin_rejected_and_recreation_has_new_identity(route_database):
    from utils import mirza_settings as settings

    route = await settings.create_route("alice")
    with pytest.raises(ValueError):
        await settings.create_route(" alice ")
    await settings.delete_route(route["id"])
    assert await settings.get_route(route["id"]) is None
    recreated = await settings.create_route("alice")
    assert recreated["id"] != route["id"]
    assert recreated["revision"] == 1


@pytest.mark.parametrize("admin", ["", " ", "a\nname", "a\x00name", "a" * 256])
async def test_admin_name_rejects_empty_controls_and_oversized_input(route_database, admin):
    from utils import mirza_settings as settings

    with pytest.raises(ValueError):
        await settings.create_route(admin)


@pytest.mark.parametrize(
    "changes",
    [
        {"api_key": "secret with space"},
        {"api_key": "x\n"},
        {"api_key": "é"},
        {"api_key": "x" * 513},
        {"message_text": "😀" * 2049},
        {"trigger_mode": "all"},
        {"trigger_mode": []},
        {"selected_steps": [True]},
        {"selected_steps": [-1]},
        {"selected_steps": [10]},
        {"revision": 99},
        {"id": "replacement"},
    ],
)
async def test_invalid_edit_rolls_back_route(route_database, changes):
    from utils import mirza_settings as settings

    route = await settings.create_route("alice")
    with pytest.raises(ValueError):
        await settings.update_route(route["id"], changes)
    assert (await settings.get_route(route["id"]))["revision"] == 1


async def test_selected_route_requires_steps_and_text_uses_utf16_limit(route_database):
    from utils import mirza_settings as settings

    route = await configure_route(settings, "alice", text="😀" * 2048)
    with pytest.raises(ValueError):
        await settings.update_route(route["id"], {"trigger_mode": "selected"})
    with pytest.raises(ValueError):
        await settings.update_route(route["id"], {"message_text": "  "})
    route = await settings.update_route(route["id"], {"trigger_mode": "selected", "selected_steps": [3, 0, 3]})
    assert route["selected_steps"] == [0, 3]


async def test_independent_routes_capture_defensive_dynamic_and_selected_rules(route_database):
    from utils import mirza_settings as settings
    from db.crud.mirza_routes import MirzaRoutesCRUD

    alice = await configure_route(settings, "alice", key="alice-key", text="Alice text")
    bob = await configure_route(settings, "bob", "https://other.example.com/v2", "bob-key", "Bob text")
    await settings.update_route(bob["id"], {"trigger_mode": "selected", "selected_steps": [0, 3]})
    await settings.create_route("disabled")
    rules = await settings.capture_rules()
    assert {r["admin_username"] for r in rules} == {"alice", "bob"}
    assert "api_key" not in repr(rules) and "Alice text" not in repr(rules)
    context = settings.notification_context(rules, 3, 4, "revoke")
    assert context["step_index"] == 3 and context["step_count"] == 4
    assert {r["admin_username"] for r in context["candidates"]} == {"alice", "bob"}
    assert [r["admin_username"] for r in settings.notification_context(rules, 0, 4, "warning")["candidates"]] == ["bob"]
    assert [r["admin_username"] for r in settings.notification_context(rules, 1, 2, "disable")["candidates"]] == [
        "alice"
    ]
    assert settings.notification_context(rules, 1, 4, "disable") is None
    rules[0]["selected_steps"].append(9)
    context["candidates"][0]["admin_username"] = "mutated"
    assert "mutated" not in repr(await settings.capture_rules())
    async with route_database() as db:
        assert await MirzaRoutesCRUD.read_secret(db, alice["id"], 2) == "alice-key"
        assert await MirzaRoutesCRUD.read_secret(db, bob["id"], 3) == "bob-key"
    assert (await settings.get_route(bob["id"]))["message_text"] == "Bob text"


@pytest.mark.parametrize(
    "index,count,kind",
    [
        (-1, 4, "disable"),
        (4, 4, "disable"),
        (0, 0, "warning"),
        (0, 11, "disable"),
        (True, 4, "disable"),
        (0, 4, "unknown"),
    ],
)
def test_context_rejects_invalid_ladder_positions(index, count, kind):
    from utils.mirza_settings import notification_context

    assert notification_context([], index, count, kind) is None


async def test_persistence_error_does_not_expose_api_key(route_database, monkeypatch):
    from utils import mirza_settings as settings
    from db.crud.mirza_routes import MirzaRoutesCRUD

    route = await settings.create_route("alice")

    async def failing_update(*args, **kwargs):
        raise RuntimeError("SQL parameters include synthetic-secret")

    monkeypatch.setattr(MirzaRoutesCRUD, "update", failing_update)
    with pytest.raises(RuntimeError) as error:
        await settings.update_route(route["id"], {"api_key": "synthetic-secret"})
    assert "synthetic-secret" not in str(error.value)
    assert error.value.__suppress_context__


async def test_stale_revision_cannot_overwrite_or_delete_newer_route(route_database):
    from utils import mirza_settings as settings

    route = await settings.create_route("alice")
    await settings.update_route(route["id"], {"message_text": "new"}, expected_revision=1)
    with pytest.raises(ValueError, match="changed"):
        await settings.update_route(route["id"], {"message_text": "stale"}, expected_revision=1)
    with pytest.raises(ValueError, match="changed"):
        await settings.delete_route(route["id"], expected_revision=1)
    assert (await settings.get_route(route["id"]))["message_text"] == "new"


@pytest.mark.parametrize(
    "template,username,want",
    [
        ("کانفیگ [Username] محدود شد", "alice", "کانفیگ alice محدود شد"),
        ("[Username] / [Username]", "alice", "alice / alice"),
        ("<b>Fixed</b> [Other]", "alice", "<b>Fixed</b> [Other]"),
        ("<b>[Username]</b>", "<evil>&\"'", "<b>&lt;evil&gt;&amp;&quot;&#x27;</b>"),
    ],
)
def test_message_renders_only_literal_username_token_with_html_escaping(template, username, want):
    from utils.mirza_settings import render_message

    assert render_message(template, username) == want


def test_message_rejects_expanded_utf16_overflow_without_truncation():
    from utils.mirza_settings import render_message

    with pytest.raises(ValueError):
        render_message("😀" * 2040 + "[Username]", "x" * 17)
    assert render_message("😀" * 2040 + "[Username]", "x" * 16).endswith("x" * 16)


@pytest.mark.parametrize("delete", [False, True])
async def test_edit_or_delete_cancels_pinned_unsent_events_and_retains_terminal_identity(route_database, delete):
    from utils import mirza_settings as settings
    from db.crud.punishment_facts import PunishmentFactsCRUD
    from db.crud.mirza_notifications import MirzaNotificationsCRUD
    from db.models import MirzaNotification

    route = await configure_route(settings, "alice")
    context = {
        "candidates": [{"route_id": route["id"], "revision": 2, "admin_username": "alice"}],
        "step_index": 0,
        "step_count": 1,
        "step_type": "disable",
    }
    async with route_database() as db:
        for incident, state in [("waiting", "ready"), ("complete", "delivered"), ("in-flight", "sending")]:
            await PunishmentFactsCRUD.store(
                db,
                {
                    "incident_id": incident,
                    "username": "customer",
                    "step_index": 0,
                    "started_at": 99,
                    "applied_at": 100,
                    "disable_succeeded": True,
                    "revoke_succeeded": False,
                    "uuid_reset_succeeded": False,
                    "state": "completed",
                    "notification_context": context,
                },
            )
            await MirzaNotificationsCRUD.transition(
                db, incident, "pending", state, route_id=route["id"], route_revision=2
            )
        await db.commit()
    if delete:
        await settings.delete_route(route["id"])
    else:
        await settings.update_route(route["id"], {"message_text": "replacement"})
    async with route_database() as db:
        assert (await db.get(MirzaNotification, "waiting")).state == "skipped"
        assert (await db.get(MirzaNotification, "complete")).state == "delivered"
        assert (await db.get(MirzaNotification, "in-flight")).state == "sending"


async def test_crud_route_create_obeys_caller_rollback(route_database):
    from db.crud.mirza_routes import MirzaRoutesCRUD

    async with route_database() as db:
        route = await MirzaRoutesCRUD.create(db, "alice")
        identity = route.id
        await db.rollback()
    async with route_database() as db:
        assert await MirzaRoutesCRUD.get(db, identity) is None


@pytest.mark.parametrize(
    "template,username,limit,want",
    [
        ("کانفیگ [Username] حداکثر [Limit] اتصال", "alice", 2, "کانفیگ alice حداکثر 2 اتصال"),
        ("[Limit]/[Limit]", "alice", 12, "12/12"),
        ("عدد 3 [Other]", "alice", None, "عدد 3 [Other]"),
    ],
)
def test_message_expands_admitted_numeric_limit_without_changing_literals(template, username, limit, want):
    from utils.mirza_settings import render_message

    assert render_message(template, username, limit) == want


@pytest.mark.parametrize("limit", [None, 0, -1, True, "2", 2.0])
def test_limit_token_requires_positive_integer_admission_value(limit):
    from utils.mirza_settings import render_message

    with pytest.raises(ValueError):
        render_message("[Username]: [Limit]", "alice", limit)


async def test_context_captures_effective_limit_and_legacy_context_defaults_to_null(route_database):
    from utils import mirza_settings as settings

    await configure_route(settings, "alice")
    rules = await settings.capture_rules()
    context = settings.notification_context(rules, 0, 1, "disable", effective_limit=3)
    assert context["effective_limit"] == 3
    context.pop("effective_limit")
    assert settings.validate_notification_context(context)["effective_limit"] is None


@pytest.mark.parametrize("limit", [0, -1, True, "2"])
def test_stored_context_rejects_invalid_effective_limit(limit):
    from utils.mirza_settings import validate_notification_context

    with pytest.raises(ValueError):
        validate_notification_context(
            {"candidates": [], "step_index": 0, "step_count": 1, "step_type": "warning", "effective_limit": limit}
        )


def test_template_tokens_do_not_expand_inside_inserted_username():
    from utils.mirza_settings import render_message

    assert render_message("[Username]: [Limit]", "customer[Limit]", 3) == "customer[Limit]: 3"
