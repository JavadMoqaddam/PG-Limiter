"""Actual confirmed effects remain independent of clearable escalation history."""

import asyncio
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


@pytest.fixture
async def fact_database(tmp_path, monkeypatch):
    import db.database as database
    from db.models import Base

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'facts.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(database, "AsyncSessionLocal", sessions)
    monkeypatch.setattr(database, "DB_AVAILABLE", True)
    yield sessions
    await engine.dispose()


async def test_actual_flags_and_distinct_counts_survive_history_clear(fact_database):
    from db.crud.punishment_facts import PunishmentFactsCRUD
    from db.crud.violations import ViolationHistoryCRUD

    async with fact_database() as db:
        for incident, name, step, disabled, revoked, reset in [
            ("a", "alice", 1, True, False, False),
            ("b", "alice", 1, True, False, False),
            ("c", "alice", 2, True, True, False),
            ("d", "bob", None, True, False, False),
            ("e", "carol", 2, False, True, True),
            ("f", "warning", 0, False, False, False),
        ]:
            fact = dict(
                incident_id=incident,
                username=name,
                step_index=step,
                started_at=99.0,
                applied_at=100.0 if disabled or revoked else None,
                disable_succeeded=disabled,
                revoke_succeeded=revoked,
                uuid_reset_succeeded=reset,
                state="completed",
            )
            await PunishmentFactsCRUD.store(db, fact)
            await PunishmentFactsCRUD.store(db, fact)
        await ViolationHistoryCRUD.clear_all(db)
        await db.commit()
        counts, total = await PunishmentFactsCRUD.aggregate(db, 100.0, 101.0)
        assert counts == {1: 1, 2: 1, None: 1}
        assert total == 2
        assert await PunishmentFactsCRUD.aggregate(db, 99.0, 100.0) == ({}, 0)


async def test_fact_identity_cannot_rewrite_completed_success(fact_database):
    from db.crud.punishment_facts import PunishmentFactsCRUD

    fact = dict(
        incident_id="same",
        username="alice",
        step_index=4,
        started_at=10.0,
        applied_at=11.0,
        disable_succeeded=True,
        revoke_succeeded=False,
        uuid_reset_succeeded=False,
        state="completed",
    )
    async with fact_database() as db:
        await PunishmentFactsCRUD.store(db, fact)
        await db.commit()
        with pytest.raises(ValueError, match="immutable"):
            await PunishmentFactsCRUD.store(db, {**fact, "step_index": 0})


async def test_pending_original_success_defers_aggregate_until_sql_recovers(fact_database, tmp_path, monkeypatch):
    from utils.punishment_facts import PunishmentFactJournal

    journal = PunishmentFactJournal(str(tmp_path / "outbox.json"), clock=lambda: 99.0)
    original_store = journal._store_sql
    journal._store_sql = AsyncMock(side_effect=RuntimeError("database unavailable"))
    async with journal.action("alice", 3) as action:
        action.mark_disabled()
    assert not await journal.ready_before(100.0)
    journal._store_sql = original_store
    assert await journal.ready_before(100.0)
    from db.crud.punishment_facts import PunishmentFactsCRUD

    async with fact_database() as db:
        assert await PunishmentFactsCRUD.aggregate(db, 98.0, 100.0) == ({3: 1}, 1)


async def test_confirmed_disable_is_owned_before_registry_cancellation(fact_database, tmp_path, monkeypatch):
    import utils.panel_api.users as users
    import utils.punishment_facts as facts
    from utils.punishment_system import PunishmentSystem
    from utils.types import PanelType, UserType

    journal = facts.PunishmentFactJournal(str(tmp_path / "outbox.json"), clock=lambda: 99.0)
    monkeypatch.setattr(facts, "_journal", journal)
    system = PunishmentSystem(filename=str(tmp_path / "violations.json"))
    monkeypatch.setattr(users, "check_user_exists", AsyncMock(return_value=True))
    monkeypatch.setattr(users, "read_config", AsyncMock(return_value={}))
    monkeypatch.setattr(users, "_read_disable_provenance", AsyncMock(return_value=None))
    monkeypatch.setattr(users, "disable_user_by_status", AsyncMock(return_value=True))

    async def cancelled_registry(*args, **kwargs):
        from db.models import PunishmentFact
        from sqlalchemy import select

        assert system.get_unrecorded_incident("alice") == (2, 15)
        async with fact_database() as db:
            assert (await db.scalar(select(PunishmentFact.state))) == "completed"
        raise asyncio.CancelledError

    monkeypatch.setattr(users.dis_users, "disable", cancelled_registry)
    with pytest.raises(asyncio.CancelledError):
        async with journal.action("alice", 2, on_disabled=lambda: system.mark_unrecorded("alice", 2, 15)):
            await users.disable_user(
                PanelType(panel_domain="d", panel_api_key="pg_key_test"), UserType(name="alice", ip=[])
            )
    assert await journal.ready_before(100.0)
    from db.crud.punishment_facts import PunishmentFactsCRUD

    async with fact_database() as db:
        assert await PunishmentFactsCRUD.aggregate(db, 98.0, 100.0) == ({2: 1}, 1)


async def test_restart_keeps_unknown_intent_pending(fact_database, tmp_path):
    from utils.punishment_facts import PunishmentFactJournal

    journal = PunishmentFactJournal(str(tmp_path / "outbox.json"), clock=lambda: 99.0)
    with pytest.raises(asyncio.CancelledError):
        async with journal.action("alice", 2):
            raise asyncio.CancelledError
    restarted = PunishmentFactJournal(str(tmp_path / "outbox.json"))
    assert not await restarted.ready_before(100.0)


@pytest.mark.parametrize(
    "step_type,duration,flags",
    [
        ("disable", 15, (True, False, False)),
        ("disable", 0, (True, False, False)),
        ("revoke", 0, (True, True, False)),
        ("warning", 0, None),
    ],
)
async def test_selected_step_captures_actual_flags(fact_database, tmp_path, monkeypatch, step_type, duration, flags):
    from sqlalchemy import select
    from db.models import PunishmentFact
    import utils.panel_api.users as users
    import utils.punishment_system as punishment
    import utils.punishment_facts as facts
    from utils.types import PanelType, UserType

    system = punishment.PunishmentSystem(filename=str(tmp_path / "history.json"))
    monkeypatch.setattr(punishment, "get_punishment_system", lambda: system)
    monkeypatch.setattr(facts, "_journal", facts.PunishmentFactJournal(str(tmp_path / "outbox.json")))
    monkeypatch.setattr(users, "check_user_exists", AsyncMock(return_value=True))
    monkeypatch.setattr(users, "read_config", AsyncMock(return_value={"punishment": {"enabled": True}}))
    monkeypatch.setattr(
        punishment,
        "get_punishment_for_user",
        AsyncMock(return_value=(punishment.PunishmentStep(step_type, duration), 4, 4)),
    )
    monkeypatch.setattr(users, "disable_user", AsyncMock())
    monkeypatch.setattr(users, "revoke_user_subscription", AsyncMock(return_value=True))
    monkeypatch.setattr(users, "reset_user_uuid", AsyncMock(return_value=False))
    await users.disable_user_with_punishment(
        PanelType(panel_domain="d", panel_api_key="pg_key_test"), UserType("alice", [])
    )
    await system.clear_all_history()
    async with fact_database() as db:
        rows = (await db.scalars(select(PunishmentFact))).all()
        if flags is None:
            assert rows == []
        else:
            assert len(rows) == 1
            assert rows[0].step_index == 4
            assert (rows[0].disable_succeeded, rows[0].revoke_succeeded, rows[0].uuid_reset_succeeded) == flags


async def test_disappearing_user_is_not_a_plain_ban(fact_database, tmp_path, monkeypatch):
    from db.crud.punishment_facts import PunishmentFactsCRUD
    import utils.panel_api.users as users
    import utils.punishment_facts as facts
    from utils.types import PanelType, UserType

    journal = facts.PunishmentFactJournal(str(tmp_path / "outbox.json"))
    monkeypatch.setattr(facts, "_journal", journal)
    monkeypatch.setattr(users, "check_user_exists", AsyncMock(return_value=False))
    monkeypatch.setattr(users, "safe_send_logs_panel", AsyncMock())
    async with journal.action("gone", None):
        await users.disable_user(PanelType(panel_domain="d", panel_api_key="pg_key_test"), UserType("gone", []))
        facts.current_action().mark_disabled()
    async with fact_database() as db:
        assert await PunishmentFactsCRUD.aggregate(db, 0, 9999999999) == ({}, 0)


async def test_callback_failure_after_physical_fact_commit_is_confirmed(fact_database, tmp_path):
    from sqlalchemy import event
    from sqlalchemy.orm import Session
    from utils.punishment_facts import PunishmentFactJournal

    journal = PunishmentFactJournal(str(tmp_path / "outbox.json"), clock=lambda: 99.0)

    def after_commit(session):
        raise RuntimeError("callback after physical commit")

    event.listen(Session, "after_commit", after_commit)
    try:
        async with journal.action("alice", 2) as action:
            action.mark_disabled()
        assert await journal.ready_before(100.0)
    finally:
        event.remove(Session, "after_commit", after_commit)


async def test_stale_prepared_snapshot_cannot_resurrect_committed_completion(fact_database, tmp_path):
    from utils.punishment_facts import PunishmentFactJournal

    journal = PunishmentFactJournal(str(tmp_path / "outbox.json"), clock=lambda: 99.0)
    async with journal.action("alice", 2) as action:
        stale = action.snapshot()
        action.mark_disabled()
    await journal.store(stale)
    assert await journal.ready_before(100.0)


async def test_new_success_marker_survives_an_older_inflight_snapshot(fact_database, tmp_path):
    from utils.punishment_facts import PunishmentFactJournal

    journal = PunishmentFactJournal(str(tmp_path / "outbox.json"), clock=lambda: 99.0)
    original = journal._store_sql
    writing = asyncio.Event()
    release = asyncio.Event()
    async with journal.action("alice", 2) as action:

        async def slow_store(snapshot):
            writing.set()
            await release.wait()
            await original(snapshot)

        journal._store_sql = slow_store
        writer = asyncio.create_task(action.save())
        await asyncio.wait_for(writing.wait(), 1)
        action.mark_disabled()
        release.set()
        await writer
        assert journal.pending[action.incident_id]["disable_succeeded"]
        journal._store_sql = original


@pytest.mark.parametrize("invalid", ["[]", '{"broken":{"applied_at":99}}'])
async def test_corrupt_outbox_pauses_reports_without_blocking_enforcement(fact_database, tmp_path, invalid):
    from utils.punishment_facts import PunishmentFactJournal

    path = tmp_path / "outbox.json"
    path.write_text(invalid)
    journal = PunishmentFactJournal(str(path), clock=lambda: 99.0)
    async with journal.action("alice", 2) as action:
        action.mark_disabled()
    assert not await journal.ready_before(100.0)
    assert path.read_text() == invalid
    from db.crud.punishment_facts import PunishmentFactsCRUD

    async with fact_database() as db:
        assert await PunishmentFactsCRUD.aggregate(db, 98.0, 100.0) == ({2: 1}, 1)


async def test_owned_acknowledgement_preserves_cancellation_after_local_failure():
    from utils.punishment_facts import settle_owned

    entered, release = asyncio.Event(), asyncio.Event()

    async def failing_acknowledgement():
        entered.set()
        await release.wait()
        raise RuntimeError("local persistence failure")

    caller = asyncio.create_task(settle_owned(failing_acknowledgement()))
    await entered.wait()
    caller.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await caller
