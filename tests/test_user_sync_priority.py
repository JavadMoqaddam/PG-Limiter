"""A due configured full sync finishes before admitting the next IP sample."""

import asyncio
from itertools import groupby
from types import SimpleNamespace

import pytest

import db.database as database
import utils.ip_source_api as api
import utils.user_sync as sync
from db.crud.users import UserCRUD
from test_api_evidence import api_cycle  # noqa: F401
from test_panel_workload_serialization import workloads  # noqa: F401
from utils.panel_workload import serialized_panel_workload
from utils.read_config import save_config_value


@pytest.fixture
async def schedule(workloads, monkeypatch):
    # Only the sync scheduler's clock moves; IP freshness and asyncio stay real.
    clock = SimpleNamespace(now=900.0)
    monkeypatch.setattr(sync, "monotonic", lambda: clock.now)
    monkeypatch.setattr(sync, "_sync_schedule_ready_at", 0.0)
    monkeypatch.setattr(sync, "_last_sync_attempt_finished_at", 0.0)
    await save_config_value("user_sync_interval", 15)
    return clock


async def test_overdue_sync_precedes_collection_without_a_queued_timer(workloads, schedule):
    assert await workloads.start_collection() is True
    assert [kind for kind, _ in groupby(workloads.order)] == ["sync", "ip"]
    async with database.get_db() as db:
        assert (await UserCRUD.get_by_username(db, "alice")).group_ids == [1]


@pytest.mark.parametrize("elapsed,expected", [(899, ["ip"]), (900, ["sync", "ip"])])
async def test_fifteen_minute_boundary_is_respected(workloads, schedule, elapsed, expected):
    schedule.now = elapsed
    assert await workloads.start_collection() is True
    assert [kind for kind, _ in groupby(workloads.order)] == expected


@pytest.mark.parametrize("interval,expected", [(5, ["sync", "ip"]), (30, ["ip"])])
async def test_interval_comes_from_settings_not_a_fixed_fifteen_minutes(workloads, schedule, interval, expected):
    await save_config_value("user_sync_interval", interval)
    assert await workloads.start_collection() is True
    assert [kind for kind, _ in groupby(workloads.order)] == expected


async def test_deadline_crossed_while_waiting_is_checked_at_admission(workloads, schedule):
    schedule.now = 899
    async with serialized_panel_workload("occupied slot"):
        queued = workloads.start_collection()
        await asyncio.sleep(0.05)
        assert workloads.order == []
        schedule.now = 900
    assert await asyncio.wait_for(queued, 2) is True
    assert [kind for kind, _ in groupby(workloads.order)] == ["sync", "ip"]


async def test_new_configured_interval_is_read_after_queueing(workloads, schedule):
    await save_config_value("user_sync_interval", 30)
    async with serialized_panel_workload("occupied slot"):
        queued = workloads.start_collection()
        await asyncio.sleep(0.05)
        await save_config_value("user_sync_interval", 15)
    # The old observation may be superseded by the config revision. The due
    # mirror still runs before any IP request, using the current interval.
    await asyncio.wait_for(queued, 2)
    assert workloads.order[0] == "sync"


async def test_becoming_due_does_not_interrupt_an_active_collection(workloads, schedule):
    schedule.now = 899
    workloads.ip_release.clear()
    active = workloads.start_collection()
    await asyncio.wait_for(workloads.ip_started.wait(), 2)
    schedule.now = 900
    next_cycle = workloads.start_collection()
    await asyncio.sleep(0.05)
    assert not workloads.sync_started.is_set()
    workloads.ip_release.set()
    assert await asyncio.wait_for(active, 2) is True
    assert await asyncio.wait_for(next_cycle, 2) is True
    assert [kind for kind, _ in groupby(workloads.order)] == ["ip", "sync", "ip"]


@pytest.mark.parametrize("result", ["success", "failure", "live_listing"])
async def test_completed_attempt_waits_the_configured_interval_before_retry(workloads, schedule, monkeypatch, result):
    if result == "failure":
        workloads.sync_error = True
    elif result == "live_listing":
        from utils.panel_api.users import PanelUserListing

        original = sync.get_all_users_with_details

        async def live_listing(*args, **kwargs):
            return PanelUserListing(await original(*args, **kwargs), pagination_changed=True)

        monkeypatch.setattr(sync, "get_all_users_with_details", live_listing)

    assert await workloads.start_collection() is True
    assert workloads.order.count("sync") == 1
    last_success = await sync.get_last_sync_time()
    assert (last_success is not None) == (result == "success")
    schedule.now = 1799
    assert await workloads.start_collection() is True
    assert workloads.order.count("sync") == 1
    schedule.now = 1800
    assert await workloads.start_collection() is True
    assert workloads.order.count("sync") == 2


async def test_interval_starts_after_full_sync_finishes(workloads, schedule):
    workloads.sync_release.clear()
    pending = workloads.start_collection()
    await asyncio.wait_for(workloads.sync_started.wait(), 2)
    schedule.now = 1100
    workloads.sync_release.set()
    assert await asyncio.wait_for(pending, 2) is True
    schedule.now = 1800
    assert await workloads.start_collection() is True
    assert workloads.order.count("sync") == 1
    schedule.now = 2000
    assert await workloads.start_collection() is True
    assert workloads.order.count("sync") == 2


async def test_manual_sync_queued_after_collection_satisfies_due_priority(workloads, schedule):
    async with serialized_panel_workload("occupied slot"):
        queued = workloads.start_collection()
        await asyncio.sleep(0.05)
        manual = workloads.start_sync()
        await asyncio.sleep(0.05)
    assert await asyncio.wait_for(manual, 2) == (2, 0, 0)
    assert await asyncio.wait_for(queued, 2) is True
    assert [kind for kind, _ in groupby(workloads.order)] == ["sync", "ip"]
    assert workloads.order.count("sync") == 1


async def test_cancelling_priority_sync_does_not_postpone_the_next_attempt(workloads, schedule):
    workloads.sync_release.clear()
    cancelled = workloads.start_collection()
    await asyncio.wait_for(workloads.sync_started.wait(), 2)
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled
    assert not workloads.ip_started.is_set()
    workloads.sync_release.set()
    assert await asyncio.wait_for(workloads.start_collection(), 2) is True
    assert workloads.order.count("sync") == 2


async def test_simultaneous_periodic_requests_are_coalesced_with_boundary_sync(workloads, schedule):
    async with serialized_panel_workload("occupied slot"):
        queued = workloads.start_collection()
        await asyncio.sleep(0.05)
        periodic = [
            asyncio.create_task(sync.sync_users_to_database(workloads.cycle.panel, only_if_due=True)) for _ in range(2)
        ]
        await asyncio.sleep(0.05)
    try:
        assert await asyncio.wait_for(queued, 2) is True
        await asyncio.wait_for(asyncio.gather(*periodic), 2)
        assert [kind for kind, _ in groupby(workloads.order)] == ["sync", "ip"]
        assert workloads.order.count("sync") == 1
    finally:
        for task in periodic:
            task.cancel()
        await asyncio.gather(*periodic, return_exceptions=True)


async def test_extending_interval_while_queued_defers_the_mirror(workloads, schedule):
    async with serialized_panel_workload("occupied slot"):
        queued = workloads.start_collection()
        await asyncio.sleep(0.05)
        await save_config_value("user_sync_interval", 30)
    assert await asyncio.wait_for(queued, 2) is True
    assert not workloads.sync_started.is_set()


async def test_wait_for_due_sync_does_not_start_sample_or_count_api_failure(workloads, schedule, monkeypatch):
    from utils.shared_state import pop_api_users_snapshot

    schedule.now = 899
    assert await workloads.start_collection() is True
    previous = (await pop_api_users_snapshot()).api_evidence
    schedule.now = 900
    workloads.sync_release.clear()
    workloads.ip_started.clear()
    workloads.sync_error = True
    monkeypatch.setattr(api, "_consecutive_dead_cycles", 1)
    queued = workloads.start_collection()
    await asyncio.wait_for(workloads.sync_started.wait(), 2)
    assert not workloads.ip_started.is_set()
    assert api._consecutive_dead_cycles == 1
    assert api.api_evidence_is_current(previous)
    workloads.sync_release.set()
    assert await asyncio.wait_for(queued, 2) is True
    assert api._consecutive_dead_cycles == 0
    assert not api.api_evidence_is_current(previous)


@pytest.fixture
def scheduler_sleeps(monkeypatch):
    """Control only the background scheduler's sleeps; preserve DB batch yields."""
    sleeps = asyncio.Queue()

    async def sleep(delay):
        if delay == 0:
            await asyncio.sleep(0)
            return
        release = asyncio.Event()
        await sleeps.put((delay, release))
        await release.wait()

    monkeypatch.setattr(sync, "asyncio", SimpleNamespace(**dict(vars(asyncio), sleep=sleep)))
    return sleeps


@pytest.mark.parametrize("initial", ["boundary", "manual"])
async def test_startup_grace_and_completed_sync_avoid_duplicate_initial_mirror(
    workloads, schedule, scheduler_sleeps, monkeypatch, initial
):
    schedule.now = 0
    monkeypatch.setattr(sync, "_last_sync_attempt_finished_at", None)
    background = asyncio.create_task(sync.run_user_sync_loop(workloads.cycle.panel))
    try:
        delay, wake = await asyncio.wait_for(scheduler_sleeps.get(), 2)
        assert delay == 30
        assert await workloads.start_collection() is True
        assert not workloads.sync_started.is_set()
        if initial == "manual":
            assert await workloads.start_sync() == (2, 0, 0)
            schedule.now = 30
        else:
            schedule.now = 30
            assert await workloads.start_collection() is True
        assert workloads.order.count("sync") == 1
        wake.set()
        await asyncio.wait_for(scheduler_sleeps.get(), 2)
        assert workloads.order.count("sync") == 1
    finally:
        background.cancel()
        await asyncio.gather(background, return_exceptions=True)


async def test_background_sync_respects_interval_without_api_cycles(workloads, schedule, scheduler_sleeps, monkeypatch):
    schedule.now = 0
    monkeypatch.setattr(sync, "_last_sync_attempt_finished_at", None)
    background = asyncio.create_task(sync.run_user_sync_loop(workloads.cycle.panel))
    try:
        _, wake = await asyncio.wait_for(scheduler_sleeps.get(), 2)
        schedule.now = 30
        wake.set()
        _, wake = await asyncio.wait_for(scheduler_sleeps.get(), 2)
        assert workloads.order == ["sync"]
        schedule.now = 929
        wake.set()
        _, wake = await asyncio.wait_for(scheduler_sleeps.get(), 2)
        assert workloads.order == ["sync"]
        schedule.now = 930
        wake.set()
        await asyncio.wait_for(scheduler_sleeps.get(), 2)
        assert workloads.order == ["sync", "sync"]
    finally:
        background.cancel()
        await asyncio.gather(background, return_exceptions=True)


@pytest.mark.parametrize("interval", [0, -1])
async def test_invalid_nonpositive_interval_cannot_starve_ip_collection(workloads, schedule, interval):
    await save_config_value("user_sync_interval", interval)
    assert await asyncio.wait_for(workloads.start_collection(), 2) is True
    assert [kind for kind, _ in groupby(workloads.order)] == ["sync", "ip"]
    assert workloads.order.count("sync") == 1
