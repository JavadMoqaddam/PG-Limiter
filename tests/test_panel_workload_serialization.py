"""Full user sync and online-IP work take turns without losing pending work."""

import asyncio
from itertools import groupby
from types import SimpleNamespace

import pytest

import db.database as database
import utils.ip_source_api as api
import utils.panel_api.request_helper as requests
import utils.user_sync as sync
from db.crud.users import UserCRUD
from test_api_evidence import api_cycle  # noqa: F401
from utils.shared_state import pop_api_users_snapshot


@pytest.fixture
async def workloads(api_cycle, monkeypatch):
    """Keep real collection, synchronization and SQLite; pause only panel I/O."""
    original_get = requests.panel_get
    original_request = requests.panel_request
    state = SimpleNamespace(
        cycle=api_cycle,
        sync_started=asyncio.Event(),
        ip_started=asyncio.Event(),
        sync_release=asyncio.Event(),
        ip_release=asyncio.Event(),
        sync_error=False,
        order=[],
    )
    state.sync_release.set()
    state.ip_release.set()
    monkeypatch.setattr(sync, "_sync_lock", asyncio.Lock())
    monkeypatch.setattr(sync, "_last_sync_time", None)

    async def get(panel, path, **kwargs):
        params = kwargs.get("params") or {}
        if path == "/api/users" and not any(key in params for key in ("status", "online", "ids")):
            state.order.append("sync")
            state.sync_started.set()
            await state.sync_release.wait()
            if state.sync_error:
                raise RuntimeError("synthetic sync failure")
        return await original_get(panel, path, **kwargs)

    async def request(*args, **kwargs):
        state.order.append("ip")
        state.ip_started.set()
        await state.ip_release.wait()
        return await original_request(*args, **kwargs)

    monkeypatch.setattr(requests, "panel_get", get)
    monkeypatch.setattr(requests, "panel_request", request)
    tasks = []

    def start_sync():
        task = asyncio.create_task(sync.sync_users_to_database(api_cycle.panel))
        tasks.append(task)
        return task

    def start_collection():
        task = asyncio.create_task(api.collect_active_users_from_api(api_cycle.panel, api_cycle.config))
        tasks.append(task)
        return task

    def start_recheck(evidence):
        task = asyncio.create_task(api.recheck_api_user(api_cycle.panel, "alice", api_cycle.config, evidence))
        tasks.append(task)
        return task

    state.start_sync = start_sync
    state.start_collection = start_collection
    state.start_recheck = start_recheck
    yield state
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


async def pending_without_request(task, entered):
    """Allow the real DB/network boundary to run, then check admission held it."""
    await asyncio.sleep(0.05)
    assert not entered.is_set(), "The queued operation sent a panel request before the active one finished"
    assert not task.done(), "Pending work must wait, not be dropped"


async def test_sync_waits_for_entire_ip_collection_then_updates_database(workloads):
    w = workloads
    w.ip_release.clear()
    collector = w.start_collection()
    await asyncio.wait_for(w.ip_started.wait(), 2)
    syncing = w.start_sync()
    await pending_without_request(syncing, w.sync_started)
    w.ip_release.set()
    assert await asyncio.wait_for(collector, 2) is True
    assert await asyncio.wait_for(syncing, 2) == (2, 0, 0)
    async with database.get_db() as db:
        assert (await UserCRUD.get_by_username(db, "alice")).group_ids == [1]


async def test_collection_waits_for_sync_without_counting_an_api_failure(workloads):
    w = workloads
    w.sync_release.clear()
    syncing = w.start_sync()
    await asyncio.wait_for(w.sync_started.wait(), 2)
    api._consecutive_dead_cycles = 1
    collector = w.start_collection()
    await pending_without_request(collector, w.ip_started)
    assert api._consecutive_dead_cycles == 1
    w.sync_release.set()
    assert await asyncio.wait_for(syncing, 2) == (2, 0, 0)
    assert await asyncio.wait_for(collector, 2) is True
    assert api._consecutive_dead_cycles == 0


async def test_collection_waits_until_sync_commits_and_publishes_metadata(workloads, monkeypatch):
    w = workloads
    publishing = asyncio.Event()
    release = asyncio.Event()
    original = sync.refresh_user_metadata_cache

    async def publish():
        publishing.set()
        await release.wait()
        await original()

    monkeypatch.setattr(sync, "refresh_user_metadata_cache", publish)
    syncing = w.start_sync()
    await asyncio.wait_for(publishing.wait(), 2)
    collector = w.start_collection()
    await pending_without_request(collector, w.ip_started)
    release.set()
    assert await asyncio.wait_for(syncing, 2) == (2, 0, 0)
    assert await asyncio.wait_for(collector, 2) is True


async def test_punishment_recheck_waits_for_sync_before_observing_ips(workloads):
    w = workloads
    assert await w.start_collection() is True
    evidence = (await pop_api_users_snapshot()).api_evidence
    w.ip_started.clear()
    w.sync_release.clear()
    syncing = w.start_sync()
    await asyncio.wait_for(w.sync_started.wait(), 2)
    refreshed = w.start_recheck(evidence)
    await pending_without_request(refreshed, w.ip_started)
    w.sync_release.set()
    assert await asyncio.wait_for(syncing, 2) == (2, 0, 0)
    result = await asyncio.wait_for(refreshed, 2)
    assert result is not None
    assert set(result[0].ip) == {"5.6.7.8", "9.9.9.9"}


async def test_queued_sync_gets_a_turn_before_another_collection(workloads):
    w = workloads
    w.ip_release.clear()
    first = w.start_collection()
    await asyncio.wait_for(w.ip_started.wait(), 2)
    syncing = w.start_sync()
    await pending_without_request(syncing, w.sync_started)
    second = w.start_collection()
    w.ip_release.set()
    assert await asyncio.wait_for(first, 2) is True
    assert await asyncio.wait_for(syncing, 2) == (2, 0, 0)
    assert await asyncio.wait_for(second, 2) is True
    assert [operation for operation, _ in groupby(w.order)] == ["ip", "sync", "ip"]


@pytest.mark.parametrize("owner", ["sync", "ip"])
async def test_cancelling_active_work_releases_the_other_operation(workloads, owner):
    w = workloads
    if owner == "sync":
        w.sync_release.clear()
        active = w.start_sync()
        await asyncio.wait_for(w.sync_started.wait(), 2)
        queued = w.start_collection()
        queued_request = w.ip_started
        expected = True
    else:
        w.ip_release.clear()
        active = w.start_collection()
        await asyncio.wait_for(w.ip_started.wait(), 2)
        queued = w.start_sync()
        queued_request = w.sync_started
        expected = (2, 0, 0)
    await pending_without_request(queued, queued_request)
    active.cancel()
    with pytest.raises(asyncio.CancelledError):
        await active
    assert await asyncio.wait_for(queued, 2) == expected


async def test_cancelling_queued_sync_does_not_release_active_collection(workloads):
    w = workloads
    w.ip_release.clear()
    collector = w.start_collection()
    await asyncio.wait_for(w.ip_started.wait(), 2)
    cancelled = w.start_sync()
    await pending_without_request(cancelled, w.sync_started)
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled
    next_sync = w.start_sync()
    await pending_without_request(next_sync, w.sync_started)
    w.ip_release.set()
    assert await asyncio.wait_for(collector, 2) is True
    assert await asyncio.wait_for(next_sync, 2) == (2, 0, 0)


async def test_failed_sync_releases_waiting_collection(workloads):
    w = workloads
    w.sync_release.clear()
    w.sync_error = True
    syncing = w.start_sync()
    await asyncio.wait_for(w.sync_started.wait(), 2)
    collector = w.start_collection()
    await pending_without_request(collector, w.ip_started)
    w.sync_release.set()
    assert await asyncio.wait_for(syncing, 2) == (0, 1, 0)
    assert await asyncio.wait_for(collector, 2) is True


@pytest.mark.parametrize("operation", ["collection", "recheck"])
async def test_settings_changed_while_queued_prevent_old_api_work(workloads, operation):
    from utils.read_config import read_config, save_config_value

    w = workloads
    await save_config_value("ip_source", "api")
    w.cycle.config = await read_config()
    assert await w.start_collection() is True
    evidence = (await pop_api_users_snapshot()).api_evidence
    w.ip_started.clear()
    w.sync_release.clear()
    syncing = w.start_sync()
    await asyncio.wait_for(w.sync_started.wait(), 2)
    queued = w.start_collection() if operation == "collection" else w.start_recheck(evidence)
    await pending_without_request(queued, w.ip_started)
    await save_config_value("ip_source", "logs")
    w.sync_release.set()
    assert await asyncio.wait_for(syncing, 2) == (2, 0, 0)
    assert await asyncio.wait_for(queued, 2) is (False if operation == "collection" else None)
    assert not w.ip_started.is_set()
