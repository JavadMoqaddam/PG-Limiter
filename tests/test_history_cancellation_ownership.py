"""Topics must wait for an owned history transaction despite caller cancellation."""

import asyncio
from datetime import datetime, timedelta, timezone
import threading
from types import SimpleNamespace

import aiosqlite
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


@pytest.fixture
async def history_database(operational_topics, monkeypatch, tmp_path):
    import db.database as database
    from db.models import Base, IPHistory
    from utils.ip_history_tracker import IPHistoryTracker

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'history.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(database, "AsyncSessionLocal", sessions)

    async def rows():
        async with sessions() as session:
            records = (await session.execute(select(IPHistory).order_by(IPHistory.username))).scalars().all()
        return [(row.username, row.ip) for row in records]

    yield SimpleNamespace(engine=engine, sessions=sessions, tracker=IPHistoryTracker(), rows=rows)
    await engine.dispose()


@pytest.fixture
def backend_barrier(monkeypatch):
    """Delay the original sqlite3 operation on its actual aiosqlite worker thread."""
    execute = aiosqlite.Connection._execute

    def block(operation):
        loop = asyncio.get_running_loop()
        entered, finished = asyncio.Event(), asyncio.Event()
        release = threading.Event()
        events = []
        blocked = False

        async def instrument(connection, function, *args, **kwargs):
            nonlocal blocked
            if getattr(function, "__name__", None) == operation and not blocked:
                blocked = True

                def physical_operation():
                    events.append(f"{operation}-entered")
                    loop.call_soon_threadsafe(entered.set)
                    if not release.wait(5):
                        raise RuntimeError("test backend release timed out")
                    result = function(*args, **kwargs)
                    events.append(f"{operation}-finished")
                    loop.call_soon_threadsafe(finished.set)
                    return result

                return await execute(connection, physical_operation)
            return await execute(connection, function, *args, **kwargs)

        monkeypatch.setattr(aiosqlite.Connection, "_execute", instrument)
        return SimpleNamespace(entered=entered, finished=finished, release=release, events=events)

    return block


async def cancel_repeatedly(task, count):
    for _ in range(count):
        task.cancel()
        # Allow each cancellation to reach the caller's current await.
        await asyncio.sleep(0)
        await asyncio.sleep(0)


async def stop_test_tasks(*tasks):
    """Clean a failing old implementation only after its evidence is recorded."""
    pending = {task for task in tasks if task is not None and not task.done()}
    for _ in range(4):
        for task in pending:
            task.cancel()
        await asyncio.sleep(0)
        pending = {task for task in pending if not task.done()}
    if pending:
        _, pending = await asyncio.wait(pending, timeout=1)
    assert not pending, "test tasks did not finish cleanup"
    for task in tasks:
        if task is not None and task.done() and not task.cancelled():
            task.exception()


@pytest.mark.parametrize("resume", [False, True])
@pytest.mark.parametrize("cancel_count", [2, 4])
async def test_cancelled_history_waits_for_physical_commit_before_topics_transition(
    operational_topics, history_database, backend_barrier, resume, cancel_count
):
    barrier = backend_barrier("commit")
    caller = asyncio.create_task(history_database.tracker.record_many({"alice": {"5.6.7.8"}}))
    transition = None
    try:
        await asyncio.wait_for(barrier.entered.wait(), 2)

        async def toggle():
            await operational_topics.set_enabled(False)
            if resume:
                await operational_topics.set_enabled(True)
            barrier.events.append("topics-acknowledged")

        transition = asyncio.create_task(toggle())
        await cancel_repeatedly(caller, cancel_count)
        # A timeout here observes progress without cancelling either product task.
        await asyncio.wait({transition}, timeout=0.05)
        acknowledged_early = transition.done()
        visible_enabled = operational_topics.enabled
        barrier.release.set()
        _, pending = await asyncio.wait({caller, transition}, timeout=1)
        rows = await history_database.rows()
        assert not acknowledged_early, f"Topics overtook physical commit: {barrier.events}; rows={rows}"
        assert visible_enabled is True
        assert not pending, "history cleanup remained blocked after physical commit finished"
        assert caller.cancelled()
        assert barrier.finished.is_set()
        assert barrier.events.index("commit-finished") < barrier.events.index("topics-acknowledged")
        assert rows == [("alice", "5.6.7.8")]
        assert operational_topics.enabled is resume
        assert not operational_topics._write_lock.locked()
        assert history_database.tracker._last_prune > 0
    finally:
        barrier.release.set()
        await stop_test_tasks(caller, transition)


@pytest.mark.parametrize("resume", [False, True])
@pytest.mark.parametrize("cancel_count", [2, 4])
async def test_cancelled_failed_history_waits_for_physical_rollback_and_keeps_pruning_due(
    operational_topics, history_database, backend_barrier, monkeypatch, resume, cancel_count
):
    from db.models import IPHistory

    barrier = backend_barrier("rollback")
    commit = aiosqlite.Connection.commit
    failed = False

    async def fail_first_commit(connection):
        nonlocal failed
        if not failed:
            failed = True
            raise RuntimeError("commit unavailable")
        return await commit(connection)

    monkeypatch.setattr(aiosqlite.Connection, "commit", fail_first_commit)
    caller = asyncio.create_task(history_database.tracker.record_many({"alice": {"5.6.7.8"}}))
    transition = None
    try:
        await asyncio.wait_for(barrier.entered.wait(), 2)

        async def toggle():
            await operational_topics.set_enabled(False)
            if resume:
                await operational_topics.set_enabled(True)
            barrier.events.append("topics-acknowledged")

        transition = asyncio.create_task(toggle())
        await cancel_repeatedly(caller, cancel_count)
        await asyncio.wait({transition}, timeout=0.05)
        acknowledged_early = transition.done()
        visible_enabled = operational_topics.enabled
        barrier.release.set()
        _, pending = await asyncio.wait({caller, transition}, timeout=1)
        assert not acknowledged_early, f"Topics overtook physical rollback: {barrier.events}"
        assert visible_enabled is True
        assert not pending, "history cleanup remained blocked after physical rollback finished"
        assert caller.cancelled()
        assert barrier.finished.is_set()
        assert barrier.events.index("rollback-finished") < barrier.events.index("topics-acknowledged")
        assert await history_database.rows() == []
        assert history_database.tracker._last_prune == 0
        assert not operational_topics._write_lock.locked()

        # A cancelled, failed transaction cannot acknowledge rolled-back pruning.
        old = datetime.now(timezone.utc) - timedelta(hours=72)
        async with history_database.sessions() as session:
            session.add(IPHistory(username="expired", ip="8.8.8.8", first_seen=old, last_seen=old))
            await session.commit()
        await operational_topics.set_enabled(True)
        assert await history_database.tracker.record_many({"bob": {"5.6.7.9"}}) == 1
        assert await history_database.rows() == [("bob", "5.6.7.9")]
        assert history_database.tracker._last_prune > 0
    finally:
        barrier.release.set()
        await stop_test_tasks(caller, transition)


async def test_cancelled_history_waiting_for_ownership_never_starts_sql(
    operational_topics, history_database, monkeypatch
):
    from db.crud.ip_history import IPHistoryCRUD

    lock = operational_topics._write_lock
    acquire = lock.acquire
    queued = asyncio.Event()
    bulk_calls = []

    async def note_waiter():
        if lock.locked():
            queued.set()
        return await acquire()

    record = IPHistoryCRUD.bulk_record

    async def observe(db, pairs):
        bulk_calls.append(pairs)
        return await record(db, pairs)

    monkeypatch.setattr(lock, "acquire", note_waiter)
    monkeypatch.setattr(IPHistoryCRUD, "bulk_record", observe)
    caller = None
    try:
        async with lock:
            caller = asyncio.create_task(history_database.tracker.record_many({"alice": {"5.6.7.8"}}))
            await asyncio.wait_for(queued.wait(), 2)
            await cancel_repeatedly(caller, 4)
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(caller, 1)
        assert bulk_calls == []
        assert await history_database.rows() == []
        assert history_database.tracker._last_prune == 0
        await asyncio.wait_for(operational_topics.set_enabled(False), 1)
    finally:
        await stop_test_tasks(caller)
