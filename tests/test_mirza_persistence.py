"""Incident-owned notification admission survives replay, rollback and upgrades."""

import asyncio
from copy import deepcopy
import json
import os
import sqlite3
import subprocess
import sys
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


CONTEXT = {
    "candidates": [{"route_id": "10000000-0000-4000-8000-000000000001", "revision": 1, "admin_username": "alice"}],
    "step_index": 3,
    "step_count": 4,
    "step_type": "revoke",
    "effective_limit": 2,
}


def confirmed(incident="incident", *, context=CONTEXT, disabled=True, warning=False):
    return {
        "incident_id": incident,
        "username": "customer",
        "step_index": 3,
        "started_at": 99.0,
        "applied_at": 100.0,
        "disable_succeeded": disabled,
        "revoke_succeeded": False,
        "uuid_reset_succeeded": False,
        "warning_succeeded": warning,
        "state": "completed",
        "notification_context": deepcopy(context),
    }


@pytest.fixture
async def mirza_database(tmp_path, monkeypatch):
    import db.database as database
    from db.models import Base

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'mirza.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(database, "AsyncSessionLocal", sessions)
    monkeypatch.setattr(database, "DB_AVAILABLE", True)
    yield sessions
    await engine.dispose()


async def test_disable_admits_one_event_even_after_terminal_replay(mirza_database):
    from db.crud.punishment_facts import PunishmentFactsCRUD
    from db.crud.mirza_notifications import MirzaNotificationsCRUD
    from db.models import MirzaNotification

    async with mirza_database() as db:
        await PunishmentFactsCRUD.store(db, confirmed())
        await db.commit()
        event = await db.get(MirzaNotification, "incident")
        assert event.state == "pending"
        assert event.context == CONTEXT
        assert event.route_id is None and event.recipient is None
        assert await MirzaNotificationsCRUD.transition(db, "incident", "pending", "delivered")
        await db.commit()
        await PunishmentFactsCRUD.store(db, confirmed())
        await PunishmentFactsCRUD.store(db, confirmed("second-incident"))
        await db.commit()
        events = (
            await db.scalars(
                select(MirzaNotification)
                .order_by(MirzaNotification.incident_id)
                .execution_options(populate_existing=True)
            )
        ).all()
        assert len(events) == 2
        assert events[0].state == "delivered"


@pytest.mark.parametrize(
    "fact",
    [
        confirmed(context=None),
        confirmed(disabled=False),
        {**confirmed(), "step_index": None},
        {**confirmed(), "state": "prepared"},
        {**confirmed(), "notification_context": {**CONTEXT, "candidates": []}},
        {**confirmed(disabled=False), "revoke_succeeded": True, "uuid_reset_succeeded": True},
    ],
)
async def test_old_plain_and_no_confirmed_disable_facts_admit_no_event(mirza_database, fact):
    from db.crud.punishment_facts import PunishmentFactsCRUD
    from db.models import MirzaNotification

    async with mirza_database() as db:
        await PunishmentFactsCRUD.store(db, fact)
        await db.commit()
        assert (await db.scalars(select(MirzaNotification))).all() == []


async def test_warning_event_never_counts_as_a_daily_ban(mirza_database):
    from db.crud.punishment_facts import PunishmentFactsCRUD
    from db.models import MirzaNotification

    async with mirza_database() as db:
        await PunishmentFactsCRUD.store(
            db, confirmed(context={**CONTEXT, "step_type": "warning"}, disabled=False, warning=True)
        )
        await db.commit()
        assert (await db.get(MirzaNotification, "incident")).state == "pending"
        assert await PunishmentFactsCRUD.aggregate(db, 99, 101) == ({}, 0)


async def test_fact_and_notification_share_caller_rollback(mirza_database):
    from db.crud.punishment_facts import PunishmentFactsCRUD
    from db.models import MirzaNotification, PunishmentFact

    async with mirza_database() as db:
        await PunishmentFactsCRUD.store(db, confirmed())
        await db.rollback()
    async with mirza_database() as db:
        assert await db.get(PunishmentFact, "incident") is None
        assert await db.get(MirzaNotification, "incident") is None


async def test_context_is_immutable_across_prepared_and_completed_replays(mirza_database):
    from db.crud.punishment_facts import PunishmentFactsCRUD
    from db.models import MirzaNotification

    async with mirza_database() as db:
        prepared = {**confirmed(), "state": "prepared", "disable_succeeded": False, "applied_at": None}
        await PunishmentFactsCRUD.store(db, prepared)
        await db.commit()
        with pytest.raises(ValueError, match="immutable"):
            await PunishmentFactsCRUD.store(db, {**confirmed(), "notification_context": {**CONTEXT, "step_count": 5}})
        await db.rollback()
        await PunishmentFactsCRUD.store(db, confirmed())
        await db.commit()
        await PunishmentFactsCRUD.store(db, prepared)
        await db.commit()
        assert (await db.get(MirzaNotification, "incident")).context == CONTEXT


async def test_transition_claims_only_expected_state_and_due_queries_do_not_block(mirza_database):
    from db.crud.punishment_facts import PunishmentFactsCRUD
    from db.crud.mirza_notifications import MirzaNotificationsCRUD
    from db.models import MirzaNotification

    async with mirza_database() as db:
        await PunishmentFactsCRUD.store(db, confirmed())
        await PunishmentFactsCRUD.store(db, confirmed("second"))
        assert await MirzaNotificationsCRUD.transition(db, "incident", "pending", "pending", next_attempt_at=500)
        assert await MirzaNotificationsCRUD.transition(db, "second", "pending", "ready", recipient="5123456789")
        assert not await MirzaNotificationsCRUD.transition(db, "second", "pending", "skipped")
        await db.commit()
        assert [event.incident_id for event in await MirzaNotificationsCRUD.due(db, ("pending", "ready"), 100)] == [
            "second"
        ]
        assert (await db.get(MirzaNotification, "second", populate_existing=True)).recipient == "5123456789"


async def test_journal_restarts_sql_outage_without_losing_context_or_duplicating_event(mirza_database, tmp_path):
    from utils.punishment_facts import PunishmentFactJournal
    from db.models import MirzaNotification

    path = tmp_path / "journal.json"
    journal = PunishmentFactJournal(str(path), clock=lambda: 100)
    journal._store_sql = AsyncMock(side_effect=RuntimeError("synthetic outage"))
    context = deepcopy(CONTEXT)
    async with journal.action("customer", 3, notification_context=context) as action:
        context["candidates"][0]["admin_username"] = "changed"
        action.mark_disabled()
        snapshot = action.snapshot()
        snapshot["notification_context"]["candidates"][0]["admin_username"] = "also-changed"
    assert "changed" not in path.read_text()
    assert "api_key" not in path.read_text()
    restarted = PunishmentFactJournal(str(path))
    assert await restarted.ready_before(101)
    assert await restarted.ready_before(101)
    async with mirza_database() as db:
        events = (await db.scalars(select(MirzaNotification))).all()
        assert len(events) == 1 and events[0].context == CONTEXT


async def test_mark_warning_captures_synchronously_before_cancel(mirza_database, tmp_path):
    from utils.punishment_facts import PunishmentFactJournal
    from db.crud.punishment_facts import PunishmentFactsCRUD
    from db.models import MirzaNotification

    journal = PunishmentFactJournal(str(tmp_path / "journal.json"), clock=lambda: 100)
    with pytest.raises(asyncio.CancelledError):
        async with journal.action("customer", 3, notification_context={**CONTEXT, "step_type": "warning"}) as action:
            action.mark_warning()
            assert action.warning_succeeded and not action.disable_succeeded
            assert journal.pending[action.incident_id]["warning_succeeded"]
            raise asyncio.CancelledError
    async with mirza_database() as db:
        assert (await db.get(MirzaNotification, action.incident_id)).state == "pending"
        assert await PunishmentFactsCRUD.aggregate(db, 99, 101) == ({}, 0)


async def test_legacy_journal_and_crud_defaults_remain_replayable(mirza_database, tmp_path):
    from utils.punishment_facts import PunishmentFactJournal
    from db.crud.punishment_facts import PunishmentFactsCRUD
    from db.models import MirzaNotification

    legacy = confirmed(context=None)
    legacy.pop("notification_context")
    legacy.pop("warning_succeeded")
    path = tmp_path / "journal.json"
    path.write_text(json.dumps({"incident": legacy}))
    restarted = PunishmentFactJournal(str(path))
    assert not restarted._load_failed
    assert restarted.pending["incident"]["notification_context"] is None
    assert restarted.pending["incident"]["warning_succeeded"] is False
    assert await restarted.ready_before(101)
    async with mirza_database() as db:
        await PunishmentFactsCRUD.store(db, legacy)
        await db.commit()
        assert (await db.scalars(select(MirzaNotification))).all() == []
        assert await PunishmentFactsCRUD.aggregate(db, 99, 101) == ({3: 1}, 1)


@pytest.mark.parametrize(
    "context",
    [
        {**CONTEXT, "api_key": "synthetic-secret"},
        {**CONTEXT, "candidates": [{**CONTEXT["candidates"][0], "api_key": "synthetic-secret"}]},
        {**CONTEXT, "step_index": True},
        {**CONTEXT, "step_type": []},
        {**CONTEXT, "candidates": [{**CONTEXT["candidates"][0], "revision": False}]},
        {**CONTEXT, "candidates": [{**CONTEXT["candidates"][0], "route_id": "not-uuid"}]},
    ],
)
async def test_malformed_new_context_rejected_before_sql_or_journal(mirza_database, tmp_path, context):
    from utils.punishment_facts import PunishmentFactJournal
    from db.crud.punishment_facts import PunishmentFactsCRUD

    journal = PunishmentFactJournal(str(tmp_path / "journal.json"))
    with pytest.raises(ValueError, match="notification context"):
        async with journal.action("customer", 3, notification_context=context):
            pass
    assert journal.pending == {}
    async with mirza_database() as db:
        with pytest.raises(ValueError, match="notification context"):
            await PunishmentFactsCRUD.store(db, confirmed(context=context))


def alembic(path, *args):
    env = dict(os.environ, DATABASE_URL=f"sqlite+aiosqlite:///{path}")
    subprocess.run([sys.executable, "-m", "alembic", *args], env=env, check=True, capture_output=True, text=True)


def test_upgrade_populated_011_and_roundtrip_preserve_facts_without_backfill(tmp_path):
    path = tmp_path / "upgrade.db"
    alembic(path, "upgrade", "011_daily_reports")
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO punishment_facts VALUES ('legacy','customer',3,99,100,1,0,0,'completed')")
    # Round-trip revision 012 before advancing to the current schema for parity.
    alembic(path, "upgrade", "012_mirza_notifications")
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT notification_context, warning_succeeded FROM punishment_facts").fetchone() == (
            None,
            0,
        )
        assert db.execute("SELECT count(*) FROM mirza_notifications").fetchone() == (0,)
    alembic(path, "downgrade", "011_daily_reports")
    alembic(path, "upgrade", "012_mirza_notifications")
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT incident_id FROM punishment_facts").fetchone() == ("legacy",)
    alembic(path, "upgrade", "head")
    subprocess.run(
        [sys.executable, "tools/ci_migration_checks.py", "parity", str(path)],
        check=True,
        capture_output=True,
        text=True,
    )


async def test_schema_bootstrap_adds_new_columns_to_existing_fact_table(tmp_path, monkeypatch):
    import db.database as database

    path = tmp_path / "bootstrap.db"
    alembic(path, "upgrade", "011_daily_reports")
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(database, "DATABASE_URL", f"sqlite+aiosqlite:///{path}")
    monkeypatch.setattr(database, "_DB_INITIALIZED", False)
    try:
        await database.init_db()
        async with async_sessionmaker(engine)() as db:
            from db.crud.punishment_facts import PunishmentFactsCRUD

            await PunishmentFactsCRUD.store(db, confirmed())
            await db.commit()
        alembic(path, "stamp", "006_drop_patterns")
        alembic(path, "upgrade", "head")
    finally:
        await engine.dispose()


async def test_competing_claims_have_one_winner(mirza_database):
    from db.crud.punishment_facts import PunishmentFactsCRUD
    from db.crud.mirza_notifications import MirzaNotificationsCRUD

    async with mirza_database() as db:
        await PunishmentFactsCRUD.store(db, confirmed())
        await db.commit()

    async def claim():
        async with mirza_database() as db:
            won = await MirzaNotificationsCRUD.transition(db, "incident", "pending", "sending")
            await db.commit()
            return won

    assert sorted(await asyncio.gather(claim(), claim())) == [False, True]


async def test_aggregate_counts_include_terminal_rows_without_secret_data(mirza_database):
    from db.crud.punishment_facts import PunishmentFactsCRUD
    from db.crud.mirza_notifications import MirzaNotificationsCRUD

    async with mirza_database() as db:
        for incident, state in [("one", "pending"), ("two", "delivered"), ("three", "uncertain")]:
            await PunishmentFactsCRUD.store(db, confirmed(incident))
            await MirzaNotificationsCRUD.transition(db, incident, "pending", state)
        await db.commit()
        counts = await MirzaNotificationsCRUD.counts(db)
        assert counts["pending"] == counts["delivered"] == counts["uncertain"] == 1
        assert counts["failed"] == 0


async def test_arbitrary_remote_body_cannot_be_saved_as_last_error(mirza_database):
    from db.crud.punishment_facts import PunishmentFactsCRUD
    from db.crud.mirza_notifications import MirzaNotificationsCRUD

    async with mirza_database() as db:
        await PunishmentFactsCRUD.store(db, confirmed())
        with pytest.raises(ValueError, match="safe error"):
            await MirzaNotificationsCRUD.transition(
                db, "incident", "pending", "failed", last_error="Remote body: secret"
            )
