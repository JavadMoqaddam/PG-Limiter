"""Named bot membership is exclusive and revisioned with its shared settings."""

import asyncio
from copy import deepcopy
import json
import sqlite3

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from test_mirza_settings import configure_route, route_database  # noqa: F401
from test_mirza_persistence import alembic, confirmed


async def configured(settings, name="Sales", admins=None):
    return await configure_route(
        settings,
        name,
        "https://bot.example/api",
        "synthetic-key",
        "<b>[Username]</b>",
        admins=["alice", "bob"] if admins is None else admins,
    )


async def test_new_bot_has_no_members_and_enable_requires_one(route_database):
    from utils import mirza_settings as settings

    bot = await settings.create_route(" Sales ")
    assert bot.get("name") == "Sales"
    assert bot["admin_usernames"] == [] and not bot["enabled"]
    with pytest.raises(ValueError):
        await settings.update_route(
            bot["id"],
            {"base_url": "https://bot.example", "api_key": "key", "enabled": True},
        )
    assert await settings.get_route(bot["id"]) == bot


async def test_shared_snapshot_is_one_secret_free_group_and_defensive(route_database):
    from utils import mirza_settings as settings

    bot = await configured(settings)
    rules = await settings.capture_rules()
    assert rules == [
        {
            "route_id": bot["id"],
            "revision": bot["revision"],
            "admin_usernames": ["alice", "bob"],
            "messages": [{"id": bot["messages"][0]["id"], "selected_steps": [0]}],
        }
    ]
    context = settings.notification_context(rules, 0, 1, "disable", 3)
    assert context["candidates"] == [
        {
            "route_id": bot["id"],
            "revision": bot["revision"],
            "admin_usernames": ["alice", "bob"],
            "message_id": bot["messages"][0]["id"],
        }
    ]
    assert settings.validate_notification_context(context) == context
    context["candidates"][0]["admin_usernames"].append("mallory")
    assert "mallory" not in repr(rules)
    assert "synthetic-key" not in repr(rules) and "message_text" not in repr(rules)
    assert (await settings.get_route(bot["id"]))["admin_usernames"] == ["alice", "bob"]


async def test_disabled_bot_reserves_exact_case_and_unicode_members(route_database):
    from utils import mirza_settings as settings

    first = await settings.create_route("One")
    second = await settings.create_route("Two")
    first = await settings.update_route(first["id"], {"admin_usernames": ["Alice", "é", " alice "]})
    second = await settings.update_route(second["id"], {"admin_usernames": ["alice", "e\u0301"]})
    assert first["admin_usernames"] == [" alice ", "Alice", "é"]
    assert second["admin_usernames"] == ["alice", "e\u0301"]
    with pytest.raises(ValueError, match="assigned"):
        await settings.update_route(second["id"], {"name": "Changed", "admin_usernames": ["Alice", "new"]})
    assert await settings.get_route(second["id"]) == second
    await settings.delete_route(first["id"])
    assert (await settings.update_route(second["id"], {"admin_usernames": ["Alice"]}))["admin_usernames"] == ["Alice"]


async def test_concurrent_member_assignment_has_one_winner_and_no_partial_edit(route_database):
    from utils import mirza_settings as settings
    from db.crud.mirza_routes import MirzaRouteError

    bots = [await settings.create_route("One"), await settings.create_route("Two")]

    async def assign(bot):
        return await settings.update_route(
            bot["id"],
            {"admin_usernames": ["owner"], "name": bot["name"] + " changed", "api_key": "synthetic-secret"},
            expected_revision=1,
        )

    outcomes = await asyncio.gather(*(assign(bot) for bot in bots), return_exceptions=True)
    assert sum(isinstance(outcome, dict) for outcome in outcomes) == 1
    failure = next(outcome for outcome in outcomes if isinstance(outcome, Exception))
    assert isinstance(failure, MirzaRouteError) and "synthetic-secret" not in str(failure)
    saved = await settings.list_routes()
    assert sorted((bot["revision"], bot["name"].endswith(" changed"), bot["admin_usernames"]) for bot in saved) == [
        (1, False, []),
        (2, True, ["owner"]),
    ]


async def test_membership_edit_and_cancellation_share_caller_rollback(route_database):
    from utils import mirza_settings as settings
    from db.crud.mirza_routes import MirzaRoutesCRUD
    from db.models import MirzaNotification

    bot = await configured(settings)
    async with route_database() as db:
        db.add(
            MirzaNotification(
                incident_id="ready",
                username="customer",
                applied_at=100,
                context={},
                state="ready",
                route_id=bot["id"],
                route_revision=bot["revision"],
            )
        )
        await db.commit()
    async with route_database() as db:
        await MirzaRoutesCRUD.update(
            db, bot["id"], {"admin_usernames": ["alice"], "name": "Changed"}, expected_revision=bot["revision"]
        )
        await db.rollback()
    assert await settings.get_route(bot["id"]) == bot
    async with route_database() as db:
        assert (await db.get(MirzaNotification, "ready")).state == "ready"
    revision = bot["revision"]
    bot = await settings.update_route(bot["id"], {"admin_usernames": ["alice"]}, expected_revision=revision)
    assert bot["revision"] == revision + 1
    async with route_database() as db:
        assert (await db.get(MirzaNotification, "ready")).state == "skipped"
    with pytest.raises(ValueError):
        await settings.update_route(bot["id"], {"admin_usernames": []})
    assert (await settings.get_route(bot["id"]))["revision"] == revision + 1
    await settings.update_route(bot["id"], {"enabled": False, "admin_usernames": []})


@pytest.mark.parametrize("legacy", [False, True])
async def test_two_owners_share_delivery_and_legacy_context_still_resolves(
    route_database, monkeypatch, tmp_path, legacy
):
    from utils import mirza_settings as settings
    from db.crud.punishment_facts import PunishmentFactsCRUD
    from db.models import MirzaNotification
    from utils.punishment_facts import PunishmentFactJournal
    import utils.punishment_facts as facts
    import utils.panel_api.users as users
    from test_mirza_delivery import service_with_transport

    bot = await configured(settings)
    if legacy:
        # Simulate the migration's identity preservation, without a production compatibility API.
        from sqlalchemy import update
        from db.models import MirzaRoute

        messages = deepcopy(bot["messages"])
        messages[0]["id"] = bot["id"]
        async with route_database() as db:
            await db.execute(update(MirzaRoute).where(MirzaRoute.id == bot["id"]).values(messages=messages))
            await db.commit()
        bot = await settings.get_route(bot["id"])
    context = settings.notification_context(await settings.capture_rules(), 0, 1, "disable", 3)
    if legacy:
        context["candidates"] = [{"route_id": bot["id"], "revision": bot["revision"], "admin_username": "alice"}]
    monkeypatch.setattr(facts, "_journal", PunishmentFactJournal(str(tmp_path / "outbox.json")))
    async with route_database() as db:
        for owner in ("alice", "bob", "other"):
            await PunishmentFactsCRUD.store(
                db, {**confirmed(owner, context=context), "username": owner, "step_index": 0}
            )
        await db.commit()

    async def details(_, username):
        return {"admin": {"username": username}, "note": "5123456789 | handle | buy"}

    monkeypatch.setattr(users, "get_user_details", details)
    requests = []
    service = service_with_transport(
        lambda request: requests.append(request) or httpx.Response(200, json={"status": True})
    )
    try:
        await service.run_once()
        await service.run_once()
    finally:
        await service.close()
    async with route_database() as db:
        states = {row.username: row.state for row in await db.scalars(select(MirzaNotification))}
    assert states == {"alice": "delivered", "bob": "skipped" if legacy else "delivered", "other": "skipped"}
    assert len(requests) == (1 if legacy else 2)
    assert all(request.headers["Token"] == "synthetic-key" for request in requests)


@pytest.mark.parametrize("bootstrap", [False, True])
async def test_populated_012_upgrade_preserves_settings_memberships_and_receipts(tmp_path, monkeypatch, bootstrap):
    import db.database as database

    path = tmp_path / "upgrade.db"
    alembic(path, "upgrade", "012_mirza_notifications")
    context = {
        "candidates": [{"route_id": "10000000-0000-4000-8000-000000000001", "revision": 7, "admin_username": "alice"}],
        "step_index": 0,
        "step_count": 1,
        "step_type": "disable",
        "effective_limit": 2,
    }
    with sqlite3.connect(path) as db:
        db.execute(
            "INSERT INTO mirza_routes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                context["candidates"][0]["route_id"],
                "alice",
                "https://bot.example/api",
                "synthetic-key",
                "Hello",
                1,
                "selected",
                "[0, 3]",
                7,
            ),
        )
        db.execute(
            "INSERT INTO mirza_notifications (incident_id, username, applied_at, context, state, route_id, route_revision, recipient, attempts) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "receipt",
                "customer",
                100,
                json.dumps(context),
                "delivered",
                context["candidates"][0]["route_id"],
                7,
                "5123456789",
                1,
            ),
        )
    if bootstrap:
        engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
        monkeypatch.setattr(database, "engine", engine)
        monkeypatch.setattr(database, "DATABASE_URL", f"sqlite+aiosqlite:///{path}")
        monkeypatch.setattr(database, "_DB_INITIALIZED", False)
        try:
            await database.init_db()
        finally:
            await engine.dispose()
    alembic(path, "upgrade", "head")
    with sqlite3.connect(path) as db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(mirza_routes)")}
        assert "name" in columns and "admin_username" not in columns
        assert db.execute(
            "SELECT id, name, base_url, api_key, message_text, enabled, trigger_mode, selected_steps, revision FROM mirza_routes"
        ).fetchone() == (
            context["candidates"][0]["route_id"],
            "alice",
            "https://bot.example/api",
            "synthetic-key",
            "Hello",
            1,
            "selected",
            "[0, 3]",
            7,
        )
        assert json.loads(db.execute("SELECT messages FROM mirza_routes").fetchone()[0]) == [
            {
                "id": context["candidates"][0]["route_id"],
                "name": "Default message",
                "message_text": "Hello",
                "selected_steps": [0, 3],
            }
        ]
        assert db.execute("SELECT admin_username, route_id FROM mirza_route_admins").fetchall() == [
            ("alice", context["candidates"][0]["route_id"])
        ]
        assert db.execute("SELECT state, context, attempts FROM mirza_notifications").fetchall() == [
            ("delivered", json.dumps(context), 1)
        ]
        db.execute("DELETE FROM mirza_route_admins")
    alembic(path, "stamp", "006_drop_patterns")
    alembic(path, "upgrade", "head")
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT count(*) FROM mirza_route_admins").fetchone() == (0,)


def test_failed_013_upgrade_rolls_back_rename_and_memberships(tmp_path):
    """A failed owner insert must not leave a half-migrated route schema."""
    import subprocess

    path = tmp_path / "failed-upgrade.db"
    alembic(path, "upgrade", "012_mirza_notifications")
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO mirza_routes (id, admin_username) VALUES ('route', 'alice')")
        db.execute(
            "CREATE TABLE mirza_route_admins (admin_username VARCHAR(255) PRIMARY KEY, route_id VARCHAR(36) NOT NULL REFERENCES mirza_routes(id))"
        )
        db.execute(
            "CREATE TRIGGER reject_owner BEFORE INSERT ON mirza_route_admins BEGIN SELECT RAISE(ABORT, 'synthetic failure'); END"
        )
    with pytest.raises(subprocess.CalledProcessError):
        alembic(path, "upgrade", "013_notification_bots")
    with sqlite3.connect(path) as db:
        assert "admin_username" in {row[1] for row in db.execute("PRAGMA table_info(mirza_routes)")}
        assert db.execute("SELECT id, admin_username FROM mirza_routes").fetchall() == [("route", "alice")]
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == ("012_mirza_notifications",)
        assert db.execute("SELECT count(*) FROM mirza_route_admins").fetchone() == (0,)
        db.execute("DROP TRIGGER reject_owner")
    alembic(path, "upgrade", "013_notification_bots")
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT * FROM mirza_route_admins").fetchall() == [("alice", "route")]


@pytest.mark.parametrize("state_kind", ["empty", "legacy", "grouped_pending", "grouped_fact"])
def test_013_downgrade_requires_full_backup_without_changing_schema_or_notifications(tmp_path, state_kind):
    import subprocess

    path = tmp_path / "rollback.db"
    alembic(path, "upgrade", "012_mirza_notifications")
    route_id = "10000000-0000-4000-8000-000000000001"
    if state_kind != "empty":
        with sqlite3.connect(path) as db:
            db.execute(
                "INSERT INTO mirza_routes (id, admin_username, api_key, revision) VALUES (?, 'alice', 'synthetic-key', 7)",
                (route_id,),
            )
    alembic(path, "upgrade", "013_notification_bots")
    context = json.dumps(
        {
            "candidates": [{"route_id": route_id, "revision": 7, "admin_usernames": ["alice"]}],
            "step_index": 0,
            "step_count": 1,
            "step_type": "disable",
            "effective_limit": 2,
        }
    )
    with sqlite3.connect(path) as db:
        if state_kind == "grouped_pending":
            db.execute(
                "INSERT INTO mirza_notifications (incident_id, username, applied_at, context, state) "
                "VALUES ('event', 'customer', 100, ?, 'pending')",
                (context,),
            )
        if state_kind == "grouped_fact":
            db.execute(
                "INSERT INTO punishment_facts (incident_id, username, started_at, state, notification_context, "
                "disable_succeeded, revoke_succeeded, uuid_reset_succeeded) "
                "VALUES ('fact', 'customer', 100, 'completed', ?, 1, 0, 0)",
                (context,),
            )
    with sqlite3.connect(path) as db:
        before = list(db.iterdump())
    with pytest.raises(subprocess.CalledProcessError) as error:
        alembic(path, "downgrade", "012_mirza_notifications")
    assert "pre-upgrade backup" in error.value.stderr
    with sqlite3.connect(path) as db:
        assert list(db.iterdump()) == before
    alembic(path, "upgrade", "013_notification_bots")


async def test_membership_conflict_rolls_back_removed_owners_and_secret(route_database):
    from utils import mirza_settings as settings
    from db.models import MirzaNotification

    first = await configured(settings)
    second = await settings.create_route("Second")
    second = await settings.update_route(second["id"], {"admin_usernames": ["carol"]})
    async with route_database() as db:
        db.add(
            MirzaNotification(
                incident_id="ready",
                username="customer",
                applied_at=100,
                context={},
                state="ready",
                route_id=first["id"],
                route_revision=first["revision"],
            )
        )
        await db.commit()
    with pytest.raises(ValueError, match="assigned"):
        await settings.update_route(
            first["id"],
            {"name": "Changed", "admin_usernames": ["carol"], "api_key": "replacement"},
            expected_revision=first["revision"],
        )
    assert await settings.get_route(first["id"]) == first
    assert await settings.get_route(second["id"]) == second
    async with route_database() as db:
        from db.crud.mirza_routes import MirzaRoutesCRUD

        assert await MirzaRoutesCRUD.read_secret(db, first["id"], first["revision"]) == "synthetic-key"
        assert (await db.get(MirzaNotification, "ready")).state == "ready"


async def test_concurrent_edits_of_one_bot_preserve_winning_membership(route_database):
    from utils import mirza_settings as settings
    from db.crud.mirza_routes import MirzaRouteError

    bot = await settings.create_route("One")
    results = await asyncio.gather(
        *(
            settings.update_route(bot["id"], {"admin_usernames": [owner]}, expected_revision=1)
            for owner in ["alice", "bob"]
        ),
        return_exceptions=True,
    )
    winner = next(result for result in results if isinstance(result, dict))
    assert sum(isinstance(result, MirzaRouteError) for result in results) == 1
    assert await settings.get_route(bot["id"]) == winner
    assert winner["revision"] == 2


@pytest.mark.parametrize("members", [None, "alice", [None], [True], [""], ["\x00name"], ["a" * 256]])
async def test_invalid_membership_rolls_back(route_database, members):
    from utils import mirza_settings as settings

    bot = await settings.create_route("One")
    with pytest.raises(ValueError):
        await settings.update_route(bot["id"], {"admin_usernames": members})
    assert await settings.get_route(bot["id"]) == bot


async def test_member_order_and_duplicates_are_a_revision_noop(route_database):
    from utils import mirza_settings as settings

    bot = await configured(settings)
    assert await settings.update_route(bot["id"], {"admin_usernames": ["bob", "alice", "bob"]}) == bot


async def test_ambiguous_owner_candidates_and_changed_memberships_never_send(route_database, monkeypatch):
    from utils import mirza_settings as settings
    from db.models import MirzaNotification, MirzaRouteAdmin
    from test_mirza_delivery import service_with_transport
    import utils.panel_api.users as users
    from sqlalchemy import delete

    bot = await configured(settings)
    context = settings.notification_context(await settings.capture_rules(), 0, 1, "disable", 3)
    ambiguous = deepcopy(context)
    ambiguous["candidates"].append(
        {"route_id": "20000000-0000-4000-8000-000000000001", "revision": 1, "admin_username": "alice"}
    )
    async with route_database() as db:
        db.add(MirzaNotification(incident_id="ambiguous", username="customer", applied_at=100, context=ambiguous))
        await db.commit()

    async def details(*_):
        return {"admin": "alice", "note": "5123456789 | handle | buy"}

    monkeypatch.setattr(users, "get_user_details", details)
    requests = []
    service = service_with_transport(
        lambda request: requests.append(request) or httpx.Response(200, json={"status": True})
    )
    try:
        await service.run_once()
        async with route_database() as db:
            assert (await db.get(MirzaNotification, "ambiguous")).state == "skipped"
            db.add(MirzaNotification(incident_id="changed", username="customer", applied_at=100, context=context))
            # An external edit without a revision change still fails membership resolution.
            await db.execute(delete(MirzaRouteAdmin).where(MirzaRouteAdmin.admin_username == "alice"))
            await db.commit()
        await service.run_once()
    finally:
        await service.close()
    async with route_database() as db:
        assert (await db.get(MirzaNotification, "ambiguous")).state == "skipped"
        assert (await db.get(MirzaNotification, "changed")).state == "skipped"
    assert requests == []


@pytest.mark.parametrize("bootstrap_first", [False, True])
async def test_fresh_create_all_and_bootstrap_do_not_infer_bot_members(tmp_path, monkeypatch, bootstrap_first):
    import db.database as database
    from db.models import Base, MirzaRoute
    from utils import mirza_settings as settings

    path = tmp_path / "fresh.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(database, "AsyncSessionLocal", sessions)
    monkeypatch.setattr(database, "DB_AVAILABLE", True)
    monkeypatch.setattr(database, "DATABASE_URL", f"sqlite+aiosqlite:///{path}")
    monkeypatch.setattr(database, "_DB_INITIALIZED", False)
    try:
        if bootstrap_first:
            await database.init_db()
        else:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
        async with sessions() as db:
            db.add(MirzaRoute(id="route", name="alice", admins=[]))
            await db.commit()
        alembic(path, "stamp", "006_drop_patterns")
        alembic(path, "upgrade", "head")
        monkeypatch.setattr(database, "_DB_INITIALIZED", False)
        await database.init_db()
        assert (await settings.get_route("route"))["admin_usernames"] == []
    finally:
        await engine.dispose()


@pytest.mark.parametrize("owners", [[], ["alice", "alice"], [True], [None], "alice"])
def test_malformed_grouped_context_is_rejected_before_admission(owners):
    from utils.mirza_settings import validate_notification_context

    with pytest.raises(ValueError, match="notification context"):
        validate_notification_context(
            {
                "candidates": [
                    {"route_id": "10000000-0000-4000-8000-000000000001", "revision": 1, "admin_usernames": owners}
                ],
                "step_index": 0,
                "step_count": 1,
                "step_type": "disable",
            }
        )


async def test_grouped_journal_survives_outage_and_replay_without_duplicate_delivery(
    route_database, tmp_path, monkeypatch
):
    from unittest.mock import AsyncMock
    from utils import mirza_settings as settings
    from utils.punishment_facts import PunishmentFactJournal
    import utils.punishment_facts as facts
    import utils.panel_api.users as users
    from test_mirza_delivery import service_with_transport
    from db.models import MirzaNotification

    await configured(settings)
    context = settings.notification_context(await settings.capture_rules(), 0, 1, "disable", 3)
    path = tmp_path / "outbox.json"
    journal = PunishmentFactJournal(str(path), clock=lambda: 100)
    journal._store_sql = AsyncMock(side_effect=RuntimeError("synthetic outage"))
    async with journal.action("customer", 0, notification_context=context) as action:
        context["candidates"][0]["admin_usernames"].append("mallory")
        action.mark_disabled()
    original = path.read_text()
    assert "mallory" not in original and "synthetic-key" not in original
    restarted = PunishmentFactJournal(str(path))
    monkeypatch.setattr(facts, "_journal", restarted)

    async def details(*_):
        return {"admin": "bob", "note": "5123456789 | handle | buy"}

    monkeypatch.setattr(users, "get_user_details", details)
    requests = []
    service = service_with_transport(
        lambda request: requests.append(request) or httpx.Response(200, json={"status": True})
    )
    try:
        await service.run_once()
        # A recovered copy of the old journal must respect the durable receipt.
        path.write_text(original)
        monkeypatch.setattr(facts, "_journal", PunishmentFactJournal(str(path)))
        await service.run_once()
    finally:
        await service.close()
    async with route_database() as db:
        rows = list(await db.scalars(select(MirzaNotification)))
    assert len(rows) == 1 and rows[0].state == "delivered" and len(requests) == 1
    assert rows[0].context["candidates"][0]["admin_usernames"] == ["alice", "bob"]
