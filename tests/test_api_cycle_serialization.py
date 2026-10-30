"""Full user sync must wait for collection AND enforcement of an API cycle."""

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import db.database as database
import utils.api_observations as observations
import utils.check_usage as usage
import utils.ip_source_api as api
import utils.panel_api.request_helper as requests
import utils.warning_system as warning_package
import utils.warning_system.enhanced_system as warning_module
from db.crud.users import UserCRUD
from test_api_admission_retry import configured  # noqa: F401
from test_api_evidence import api_cycle  # noqa: F401
from test_panel_workload_serialization import workloads  # noqa: F401
from test_user_sync_priority import schedule  # noqa: F401
from utils.enforcement_guard import start_enforcement_effect, validate_enforcement
from utils.monitoring_gate import get_last_completed_scan, get_last_cycle_outcome
from utils.read_config import get_config_revision, read_config, save_config_value


class StopLoop(Exception):
    """End a daemon test at its real scheduler boundary."""


@pytest.fixture
async def enforcing(configured, monkeypatch):
    """Keep actual SQLite, collection, warnings and sync; control external I/O."""
    w = configured
    clock = [float(w.cycle.now)]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    monkeypatch.setattr(observations, "monotonic_time", lambda: clock[0])
    monkeypatch.setattr(api, "monotonic_time", lambda: clock[0])
    await save_config_value("max_warning_count", 3)
    w.cycle.config = await read_config(refresh=True)
    assert await w.cycle.run()
    w.previous_receipt = get_last_completed_scan()
    w.alice = w.cycle.warning.warnings["alice"]
    w.alice.consecutive_violations = 3
    w.alice.monitoring_end_time = clock[0] - 1
    w.bob = w.cycle.warning.warnings["bob"]
    w.enforcement_started = asyncio.Event()
    w.enforcement_release = asyncio.Event()
    w.enforcement_release.set()
    w.effects = []
    original_enforce = usage.check_users_usage

    async def enforce(*args, **kwargs):
        # Age the original requests like a slow fleet scan; fresh panel replies
        # still contain current activity. The asyncio scheduler stays real.
        clock[0] += 300
        for body in w.cycle.bodies.values():
            for node in body["nodes"].values():
                node["ips"] = {ip: int(clock[0]) for ip in node["ips"]}
        w.enforcement_started.set()
        await w.enforcement_release.wait()
        result = await original_enforce(*args, **kwargs)
        w.order.append("enforced")
        return result

    monkeypatch.setattr(usage, "check_users_usage", enforce)

    async def punish(_panel, user):
        await validate_enforcement()
        start_enforcement_effect()
        w.effects.append(user.name)
        return {"action": "warning", "violation_count": 1}

    monkeypatch.setattr(warning_package, "safe_disable_user_with_punishment", punish)
    monkeypatch.setattr(warning_module, "safe_disable_user_with_punishment", punish)
    monkeypatch.setattr(warning_package, "safe_send_disable_notification", AsyncMock())
    monkeypatch.setattr(warning_module, "safe_send_disable_notification", AsyncMock())

    async def scheduled_wait(_admission, _seconds):
        raise StopLoop

    monkeypatch.setattr(usage, "wait_for_monitoring_change", scheduled_wait)
    original_get = requests.panel_get

    async def get(panel, path, **kwargs):
        if path == "/api/user/retired":
            return SimpleNamespace(status_code=404, json=lambda: {"detail": "User not found"})
        return await original_get(panel, path, **kwargs)

    monkeypatch.setattr(requests, "panel_get", get)
    original_account = api._account_collection_cycle

    async def account(config, result):
        w.evidence = result.evidence
        return await original_account(config, result)

    monkeypatch.setattr(api, "_account_collection_cycle", account)
    w.order.clear()
    w.ip_started.clear()
    return w


@pytest.mark.parametrize("delete_missing", [False, True])
@pytest.mark.parametrize("queue_during", ["collection", "enforcement"])
async def test_sync_waits_for_completed_enforcement_and_punishment_refresh(enforcing, delete_missing, queue_during):
    w = enforcing
    if delete_missing:
        async with database.get_db() as db:
            await UserCRUD.create_or_update(db, "retired")
    original_revision = get_config_revision()
    w.ip_release.clear()
    w.enforcement_release.clear()
    w.sync_release.clear()
    runner = asyncio.create_task(usage.run_check_users_usage(w.cycle.panel))
    try:
        await asyncio.wait_for(w.ip_started.wait(), 2)
        if queue_during == "collection":
            syncing = w.start_sync()
            await asyncio.sleep(0.05)
            assert not w.sync_started.is_set()
        w.ip_release.set()
        await asyncio.wait_for(w.enforcement_started.wait(), 2)
        if queue_during == "enforcement":
            syncing = w.start_sync()
        await asyncio.sleep(0.05)
        assert not w.sync_started.is_set(), "Full sync interrupted the unfinished API enforcement cycle"
        w.enforcement_release.set()
        with pytest.raises(StopLoop):
            await asyncio.wait_for(runner, 2)
        assert get_last_completed_scan() is not w.previous_receipt
        assert get_last_cycle_outcome().outcome == "completed"
        assert w.effects == ["alice"]
        assert w.bob.consecutive_violations == 2
        # Two collection requests plus Alice's required fresh punishment check.
        assert w.order[:4] == ["ip", "ip", "ip", "enforced"]
        await asyncio.wait_for(w.sync_started.wait(), 2)
        assert w.order[4:] == ["sync"]
        w.sync_release.set()
        assert await asyncio.wait_for(syncing, 2) == (2, 0, int(delete_missing))
        assert (get_config_revision() > original_revision) is delete_missing
        async with database.get_db() as db:
            assert await UserCRUD.get_by_username(db, "retired") is None
        w.cycle.config = await read_config(refresh=True)
        assert await asyncio.wait_for(w.start_collection(), 2) is True
        assert w.order == ["ip", "ip", "ip", "enforced", "sync", "ip", "ip"]
    finally:
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)


async def test_cancelled_enforcement_releases_sync_and_next_collection(enforcing):
    w = enforcing
    w.enforcement_release.clear()
    runner = asyncio.create_task(usage.run_check_users_usage(w.cycle.panel))
    try:
        await asyncio.wait_for(w.enforcement_started.wait(), 2)
        syncing = w.start_sync()
        await asyncio.sleep(0.05)
        assert not w.sync_started.is_set()
        runner.cancel()
        with pytest.raises(asyncio.CancelledError):
            await runner
        assert await asyncio.wait_for(syncing, 2) == (2, 0, 0)
        assert await asyncio.wait_for(w.start_collection(), 2) is True
        assert w.order == ["ip", "ip", "sync", "ip", "ip"]
        assert w.bob.consecutive_violations == 1
        assert get_last_completed_scan() is w.previous_receipt
    finally:
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)


async def test_actual_settings_change_during_enforcement_still_rejects_old_evidence(enforcing):
    w = enforcing
    w.enforcement_release.clear()
    runner = asyncio.create_task(usage.run_check_users_usage(w.cycle.panel))
    try:
        await asyncio.wait_for(w.enforcement_started.wait(), 2)
        await save_config_value("general_limit", 10)
        w.enforcement_release.set()
        with pytest.raises(StopLoop):
            await asyncio.wait_for(runner, 2)
        assert get_last_cycle_outcome().outcome == "skipped"
        assert get_last_completed_scan() is w.previous_receipt
        assert w.bob.consecutive_violations == 1
        assert not w.effects
    finally:
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)


async def test_sync_becoming_due_inside_an_admitted_cycle_waits_for_next_cycle(enforcing, schedule, monkeypatch):
    w = enforcing
    schedule.now = 899
    original_collect = api.collect_active_users_from_api

    async def collect(*args, **kwargs):
        # Admission has already happened. Crossing the deadline here must not
        # run sync from a nested collector while its outer turn is still held.
        schedule.now = 900
        return await original_collect(*args, **kwargs)

    monkeypatch.setattr(api, "collect_active_users_from_api", collect)
    with pytest.raises(StopLoop):
        await asyncio.wait_for(usage.run_check_users_usage(w.cycle.panel), 2)
    assert w.order == ["ip", "ip", "ip", "enforced"]
    assert w.effects == ["alice"]
    assert w.bob.consecutive_violations == 2
    w.cycle.config = await read_config(refresh=True)
    assert await asyncio.wait_for(w.start_collection(), 2) is True
    assert w.order == ["ip", "ip", "ip", "enforced", "sync", "ip", "ip"]


@pytest.mark.parametrize("child_operation", ["collection", "refresh"])
async def test_child_task_cannot_borrow_its_parents_active_cycle(enforcing, monkeypatch, child_operation):
    w = enforcing
    original_enforce = usage.check_users_usage
    children = []

    async def enforce(*args, **kwargs):
        # Create within the owning task: a ContextVar alone would leak its
        # admission to this child and allow overlapping panel requests.
        children.append(w.start_collection() if child_operation == "collection" else w.start_recheck(w.evidence))
        return await original_enforce(*args, **kwargs)

    monkeypatch.setattr(usage, "check_users_usage", enforce)
    w.enforcement_release.clear()
    runner = asyncio.create_task(usage.run_check_users_usage(w.cycle.panel))
    try:
        await asyncio.wait_for(w.enforcement_started.wait(), 2)
        await asyncio.sleep(0.05)
        assert not children[0].done()
        assert w.order == ["ip", "ip"]
        w.enforcement_release.set()
        with pytest.raises(StopLoop):
            await asyncio.wait_for(runner, 2)
        assert await asyncio.wait_for(children[0], 2) is not None
        assert w.order[:4] == ["ip", "ip", "ip", "enforced"]
        assert w.order[4:] == (["ip", "ip"] if child_operation == "collection" else ["ip"])
        assert get_last_completed_scan() is not w.previous_receipt
        assert w.bob.consecutive_violations == 2
    finally:
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)


async def test_cancelled_punishment_refresh_releases_the_cycle(enforcing):
    w = enforcing
    w.enforcement_release.clear()
    runner = asyncio.create_task(usage.run_check_users_usage(w.cycle.panel))
    try:
        await asyncio.wait_for(w.enforcement_started.wait(), 2)
        w.ip_started.clear()
        w.ip_release.clear()
        w.enforcement_release.set()
        await asyncio.wait_for(w.ip_started.wait(), 2)
        syncing = w.start_sync()
        await asyncio.sleep(0.05)
        assert not w.sync_started.is_set()
        runner.cancel()
        with pytest.raises(asyncio.CancelledError):
            await runner
        assert await asyncio.wait_for(syncing, 2) == (2, 0, 0)
        w.ip_release.set()
        assert await asyncio.wait_for(w.start_collection(), 2) is True
        assert not w.effects
        assert w.bob.consecutive_violations == 1
        assert get_last_completed_scan() is w.previous_receipt
    finally:
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)


@pytest.mark.parametrize("phase", ["collection", "enforcement"])
async def test_failed_cycle_releases_queued_sync_and_next_collection(enforcing, monkeypatch, phase):
    w = enforcing
    entered = asyncio.Event()
    release = asyncio.Event()

    async def fail(*_args, **_kwargs):
        entered.set()
        await release.wait()
        raise RuntimeError("synthetic cycle failure")

    module, name = (api, "_collect_api_cycle") if phase == "collection" else (usage, "check_users_usage")
    original = getattr(module, name)
    monkeypatch.setattr(module, name, fail)
    runner = asyncio.create_task(usage.run_check_users_usage(w.cycle.panel))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        syncing = w.start_sync()
        await asyncio.sleep(0.05)
        assert not w.sync_started.is_set()
        release.set()
        with pytest.raises(StopLoop):
            await asyncio.wait_for(runner, 2)
        assert get_last_cycle_outcome().outcome == "failed"
        assert get_last_completed_scan() is w.previous_receipt
        assert await asyncio.wait_for(syncing, 2) == (2, 0, 0)
        monkeypatch.setattr(module, name, original)
        assert await asyncio.wait_for(w.start_collection(), 2) is True
    finally:
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)
