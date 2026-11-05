"""Only confirmed incidents admit notifications using their original policy."""

import asyncio
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import event, select

from test_mirza_persistence import mirza_database  # noqa: F401
from test_mirza_settings import configure_route, edit_message
from utils import mirza_settings as settings
from utils.types import PanelType, UserType

PANEL = PanelType(panel_domain="panel.example", panel_api_key="pg_key_test")
LADDER = [
    {"type": "disable", "duration": 10},
    {"type": "disable", "duration": 30},
    {"type": "disable", "duration": 60},
    {"type": "disable", "duration": 240},
    {"type": "revoke", "duration": 0},
]


@pytest.fixture
async def punishment_env(mirza_database, tmp_path, monkeypatch):
    import utils.panel_api.users as users
    import utils.punishment_system as punishment
    from db.crud.config import ConfigCRUD
    from db.crud.users import UserCRUD
    import utils.read_config as configuration

    monkeypatch.setattr(configuration, "_config_cache", None)
    monkeypatch.setattr(configuration, "_config_cache_db_revision", None)
    monkeypatch.setattr(configuration, "_config_read_lock", asyncio.Lock())
    monkeypatch.setattr(configuration, "DB_AVAILABLE", True)
    async with mirza_database() as db:
        await ConfigCRUD.set(db, "general_limit", "2")
        await ConfigCRUD.set(db, "group_limits", '{"7":4}')
        await UserCRUD.create_or_update(db, "customer", group_ids=[7])
        await db.commit()

    system = punishment.PunishmentSystem(str(tmp_path / "violations.json"))
    monkeypatch.setattr(punishment, "get_punishment_system", lambda: system)
    config = {
        "punishment": {"enabled": True, "window_hours": 72, "steps": deepcopy(LADDER)},
        "limits": {"general": 2, "special": {}},
        "group_limits": {"7": 4},
    }
    monkeypatch.setattr(users, "read_config", AsyncMock(side_effect=lambda: deepcopy(config)))
    monkeypatch.setattr(users, "check_user_exists", AsyncMock(return_value=True))
    monkeypatch.setattr(users, "disable_user_by_status", AsyncMock(return_value=True))
    monkeypatch.setattr(users, "revoke_user_subscription", AsyncMock(return_value=False))
    monkeypatch.setattr(users, "reset_user_uuid", AsyncMock(return_value=False))
    route = await configure_route(
        settings, "owner", "https://one.example/api", "key-one", "[Username]: [Limit]", steps=[0, 1]
    )
    return system, config, route


async def events(sessions):
    from db.models import MirzaNotification

    async with sessions() as db:
        return list(await db.scalars(select(MirzaNotification).order_by(MirzaNotification.applied_at)))


@pytest.mark.parametrize("selected,want", [([4], [4, 4]), ([0, 2], [0, 2])])
async def test_actual_ladder_positions_and_repeated_final_incidents(punishment_env, mirza_database, selected, want):
    from utils.panel_api.users import disable_user_with_punishment

    _, _, route = punishment_env
    await edit_message(settings, route, {"selected_steps": selected})
    for _ in range(6):
        await disable_user_with_punishment(PANEL, UserType("customer", group_ids=[7]))
    rows = await events(mirza_database)
    assert [row.context["step_index"] for row in rows] == want
    assert all(row.context["step_count"] == 5 and row.context["effective_limit"] == 4 for row in rows)


async def test_shorter_ladder_requires_explicit_reselection_without_remapping(punishment_env, mirza_database):
    from utils.panel_api.users import disable_user_with_punishment

    _, config, route = punishment_env
    route = await edit_message(settings, route, {"selected_steps": [4]})
    config["punishment"]["steps"] = deepcopy(LADDER[:3])
    for _ in range(3):
        await disable_user_with_punishment(PANEL, UserType("customer"))
    assert await events(mirza_database) == []
    assert (await settings.get_route(route["id"]))["messages"][0]["selected_steps"] == [4]
    await edit_message(settings, route, {"selected_steps": [2]})
    await disable_user_with_punishment(PANEL, UserType("customer"))
    rows = await events(mirza_database)
    assert len(rows) == 1 and rows[0].context["step_index"] == 2 and rows[0].context["step_count"] == 3


async def test_count_await_cannot_change_selected_ladder_or_window(punishment_env, mirza_database, monkeypatch):
    from contextlib import asynccontextmanager
    import time
    from sqlalchemy import update
    from db.models import ViolationHistory
    import db.database as database
    from utils.panel_api.users import disable_user_with_punishment

    system, config, _ = punishment_env
    await system.record_violation("customer", 0, 10)
    async with mirza_database() as db:
        await db.execute(update(ViolationHistory).values(timestamp=time.time() - 7200))
        await db.commit()
    config["punishment"]["steps"] = deepcopy(LADDER[:2])
    original = database.get_db

    @asynccontextmanager
    async def reload():
        async with original() as db:
            system.load_config({"punishment": {"window_hours": 1, "steps": [{"type": "warning", "duration": 0}]}})
            yield db

    monkeypatch.setattr(database, "get_db", reload)
    result = await disable_user_with_punishment(PANEL, UserType("customer"))
    assert result["duration_minutes"] == 30 and result["step_index"] == 1
    rows = await events(mirza_database)
    assert len(rows) == 1 and rows[0].context["step_count"] == 2


@pytest.mark.parametrize("special,groups,want", [({"customer": 9}, [7], 9), ({}, [7], 4), ({}, [], 2)])
async def test_limit_uses_canonical_policy_before_disable(
    punishment_env, mirza_database, monkeypatch, special, groups, want
):
    import utils.panel_api.users as users
    from db.crud.users import UserCRUD

    _, config, _ = punishment_env
    config["punishment"]["steps"] = deepcopy(LADDER[:1])
    config["limits"]["special"] = special
    async with mirza_database() as db:
        await UserCRUD.create_or_update(db, "customer", group_ids=groups)
        if special:
            await UserCRUD.set_special_limit(db, "customer", special["customer"], fetch_from_panel=False)
        await db.commit()
    user = UserType("customer", group_ids=groups, effective_ip_limit=123)

    async def disable(*args):
        from db.crud.config import ConfigCRUD

        user.group_ids = [999]
        config["limits"]["general"] = 55
        async with mirza_database() as db:
            await UserCRUD.create_or_update(db, "customer", group_ids=[999])
            await UserCRUD.set_special_limit(db, "customer", 55, fetch_from_panel=False)
            await ConfigCRUD.set(db, "general_limit", "55")
            await db.commit()
        return True

    monkeypatch.setattr(users, "disable_user_by_status", disable)
    await users.disable_user_with_punishment(PANEL, user)
    assert (await events(mirza_database))[0].context["effective_limit"] == want


@pytest.mark.parametrize("boundary", ["normal", "after_commit", "cancel"])
@pytest.mark.parametrize("route_enabled", [True, False])
async def test_warning_admission_follows_sql_commit_even_when_acknowledgement_interrupted(
    punishment_env, mirza_database, monkeypatch, boundary, route_enabled
):
    from sqlalchemy.orm import Session
    from utils.panel_api.users import disable_user_with_punishment

    system, config, route = punishment_env
    if not route_enabled:
        from utils.mirza_settings import update_route

        await update_route(route["id"], {"enabled": False})
    config["punishment"]["steps"] = [{"type": "warning", "duration": 0}]
    caller = asyncio.current_task()
    fired = False

    def after_commit(session):
        nonlocal fired
        from db.models import ViolationHistory

        if fired or not any(isinstance(row, ViolationHistory) for row in session.identity_map.values()):
            return
        fired = True
        if boundary == "after_commit":
            raise RuntimeError("synthetic acknowledgement interruption")
        if boundary == "cancel":
            caller.cancel()

    event.listen(Session, "after_commit", after_commit)
    try:
        if boundary == "cancel":
            with pytest.raises(asyncio.CancelledError):
                await disable_user_with_punishment(PANEL, UserType("customer"))
        else:
            await disable_user_with_punishment(PANEL, UserType("customer"))
    finally:
        event.remove(Session, "after_commit", after_commit)
    assert await system.get_violation_count_async("customer") == 1
    rows = await events(mirza_database)
    assert len(rows) == int(route_enabled)
    from db.models import PunishmentFact
    from db.crud.punishment_facts import PunishmentFactsCRUD

    async with mirza_database() as db:
        fact = (await db.scalars(select(PunishmentFact))).one()
        assert fact.state == "completed" and fact.warning_succeeded
        assert await PunishmentFactsCRUD.aggregate(db, 0, float("inf")) == ({}, 0)


@pytest.mark.parametrize("save_ok", [True, False])
async def test_warning_json_fallback_requires_durable_save(punishment_env, mirza_database, monkeypatch, save_ok):
    import db.database as database
    from utils.panel_api.users import disable_user_with_punishment
    from utils.punishment_facts import get_punishment_fact_journal

    system, config, _ = punishment_env
    config["punishment"]["steps"] = [{"type": "warning", "duration": 0}]
    import utils.mirza_settings as settings

    capture = settings.capture_rules

    async def outage_after_admission():
        rules = await capture()
        monkeypatch.setattr(database, "DB_AVAILABLE", False)
        return rules

    monkeypatch.setattr(settings, "capture_rules", outage_after_admission)
    if not save_ok:

        def fail():
            raise OSError("synthetic disk failure")

        monkeypatch.setattr(system, "_sync_save_violations", fail)
    await disable_user_with_punishment(PANEL, UserType("customer"))
    monkeypatch.setattr(database, "DB_AVAILABLE", True)
    journal = get_punishment_fact_journal()
    for snapshot in list(journal.pending.values()):
        await journal.store(snapshot)
    assert len(await events(mirza_database)) == int(save_ok)


@pytest.mark.parametrize("case", ["disabled", "failed", "missing", "guarded"])
async def test_nonqualifying_actions_do_not_notify(punishment_env, mirza_database, monkeypatch, case):
    import utils.panel_api.users as users
    from utils.enforcement_guard import enforcement_guard

    _, config, _ = punishment_env
    config["punishment"]["steps"] = deepcopy(LADDER[:1])
    if case == "disabled":
        config["punishment"]["enabled"] = False
    if case == "failed":
        monkeypatch.setattr(users, "disable_user_by_status", AsyncMock(return_value=False))
    if case == "missing":
        monkeypatch.setattr(users, "check_user_exists", AsyncMock(side_effect=[True, False]))
    with enforcement_guard(AsyncMock(return_value=case != "guarded"), lambda: True):
        await users.disable_user_with_punishment(PANEL, UserType("customer"))
    assert await events(mirza_database) == []


async def test_route_read_failure_does_not_block_punishment(punishment_env, mirza_database, monkeypatch):
    from utils.panel_api.users import disable_user_with_punishment
    import utils.mirza_settings as settings

    system, config, _ = punishment_env
    config["punishment"]["steps"] = deepcopy(LADDER[:1])
    monkeypatch.setattr(settings, "capture_rules", AsyncMock(side_effect=RuntimeError("synthetic db failure")))
    result = await disable_user_with_punishment(PANEL, UserType("customer"))
    assert result["action"] == "disabled" and await system.get_violation_count_async("customer") == 1
    assert await events(mirza_database) == []


async def test_sparse_user_argument_uses_canonical_group_facts(punishment_env, mirza_database, monkeypatch):
    import utils.user_sync as sync
    from utils.panel_api.users import disable_user_with_punishment

    _, config, _ = punishment_env
    config["punishment"]["steps"] = deepcopy(LADDER[:1])
    monkeypatch.setattr(sync, "USER_METADATA_CACHE", {"customer": {"group_ids": [999], "effective_ip_limit": 123}})
    await disable_user_with_punishment(PANEL, UserType("customer", effective_ip_limit=999))
    assert (await events(mirza_database))[0].context["effective_limit"] == 4


async def test_unknown_group_facts_do_not_guess_general_limit(punishment_env, mirza_database, monkeypatch):
    import utils.user_sync as sync
    from utils.panel_api.users import disable_user_with_punishment

    _, config, _ = punishment_env
    config["punishment"]["steps"] = deepcopy(LADDER[:1])
    monkeypatch.setattr(sync, "USER_METADATA_CACHE", {})
    from sqlalchemy import delete
    from db.models import User

    async with mirza_database() as db:
        await db.execute(delete(User).where(User.username == "customer"))
        await db.commit()
    await disable_user_with_punishment(PANEL, UserType("customer"))
    assert (await events(mirza_database))[0].context["effective_limit"] is None


async def test_history_reconciliation_and_replayed_fact_keep_one_event(punishment_env, mirza_database, monkeypatch):
    from db.crud.violations import ViolationHistoryCRUD
    from db.crud.punishment_facts import PunishmentFactsCRUD
    from db.models import PunishmentFact
    from utils.panel_api.users import disable_user_with_punishment

    system, config, _ = punishment_env
    config["punishment"]["steps"] = deepcopy(LADDER[:1])
    original = ViolationHistoryCRUD.add
    monkeypatch.setattr(ViolationHistoryCRUD, "add", AsyncMock(side_effect=RuntimeError("history unavailable")))
    first = await disable_user_with_punishment(PANEL, UserType("customer"))
    assert first["action"] == "applied_unrecorded"
    monkeypatch.setattr(ViolationHistoryCRUD, "add", original)
    assert (await disable_user_with_punishment(PANEL, UserType("customer")))["action"] == "reconciled"
    async with mirza_database() as db:
        fact = (await db.scalars(select(PunishmentFact))).one()
        snapshot = {
            key: getattr(fact, key)
            for key in (
                "incident_id",
                "username",
                "step_index",
                "started_at",
                "applied_at",
                "disable_succeeded",
                "revoke_succeeded",
                "uuid_reset_succeeded",
                "warning_succeeded",
                "notification_context",
                "state",
            )
        }
        await PunishmentFactsCRUD.store(db, snapshot)
        await db.commit()
    assert len(await events(mirza_database)) == 1
    assert await system.get_violation_count_async("customer") == 1


async def test_slow_mirza_request_does_not_hold_new_punishment(punishment_env, mirza_database, monkeypatch):
    import httpx
    from test_mirza_delivery import service_with_transport
    from utils.panel_api.users import disable_user_with_punishment

    _, config, _ = punishment_env
    config["punishment"]["steps"] = deepcopy(LADDER[:1])
    await disable_user_with_punishment(PANEL, UserType("customer"))
    entered = asyncio.Event()

    async def slow_remote(request):
        entered.set()
        await asyncio.Event().wait()
        return httpx.Response(200, json={"status": True})

    import utils.panel_api.users as users

    monkeypatch.setattr(
        users,
        "get_user_details",
        AsyncMock(return_value={"admin": {"username": "owner"}, "note": "5123456789 | handle | buy"}),
    )
    service = service_with_transport(slow_remote)
    task = asyncio.create_task(service.run_once())
    try:
        await asyncio.wait_for(entered.wait(), 2)
        result = await asyncio.wait_for(disable_user_with_punishment(PANEL, UserType("customer")), 2)
        assert result["action"] == "disabled"
        assert len(await events(mirza_database)) == 2
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await service.close()


@pytest.mark.parametrize(
    "username,step,kind", [("other", 0, "warning"), ("customer", 1, "warning"), ("customer", 0, "disable")]
)
async def test_warning_acknowledgement_requires_matching_incident(punishment_env, mirza_database, username, step, kind):
    from utils.punishment_facts import get_punishment_fact_journal
    from utils.mirza_settings import capture_rules, notification_context

    system, _, _ = punishment_env
    context = notification_context(await capture_rules(), 0, 1, kind, 4)
    async with get_punishment_fact_journal().action(
        "customer", 0, notification_context=context, step_type=kind
    ) as fact:
        await system.record_violation(username, step, 0)
    assert not fact.warning_succeeded
    assert await events(mirza_database) == []


async def test_warning_fallback_cancellation_waits_for_file_acknowledgement(
    punishment_env, mirza_database, monkeypatch
):
    import db.database as database
    import utils.mirza_settings as settings
    import utils.punishment_facts as facts
    from utils.panel_api.users import disable_user_with_punishment

    system, config, _ = punishment_env
    config["punishment"]["steps"] = [{"type": "warning", "duration": 0}]
    capture = settings.capture_rules

    async def outage_after_admission():
        rules = await capture()
        monkeypatch.setattr(database, "DB_AVAILABLE", False)
        return rules

    monkeypatch.setattr(settings, "capture_rules", outage_after_admission)
    original = system.save_violations
    entered, release = asyncio.Event(), asyncio.Event()

    async def durable_save():
        result = await original()
        entered.set()
        await release.wait()
        return result

    monkeypatch.setattr(system, "save_violations", durable_save)
    caller = asyncio.create_task(disable_user_with_punishment(PANEL, UserType("customer")))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        caller.cancel()
        await asyncio.sleep(0)
        assert not caller.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await caller
    monkeypatch.setattr(database, "DB_AVAILABLE", True)
    journal = facts.get_punishment_fact_journal()
    for snapshot in list(journal.pending.values()):
        await journal.store(snapshot)
    assert len(await events(mirza_database)) == 1
