"""A config change during sync must not consume a whole unstarted API scan."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import db.database as database
import utils.check_usage as usage
import utils.ip_source_api as api
import utils.panel_api.request_helper as requests
import utils.user_sync as sync
from db.crud.users import UserCRUD
from test_api_evidence import api_cycle  # noqa: F401
from test_panel_workload_serialization import workloads  # noqa: F401
from utils.read_config import get_config_revision, read_config, save_config_value


class StopLoop(Exception):
    """End a daemon-loop test at its real scheduler boundary."""


@pytest.fixture
async def configured(workloads, monkeypatch):
    for key, value in {
        "ip_source": "api",
        "check_interval": 300,
        "max_warning_count": 100,
        "country_code": "",
        "api_ip_freshness": 90,
    }.items():
        await save_config_value(key, value)
    workloads.cycle.config = await read_config(refresh=True)
    monkeypatch.setattr(sync, "_notify_deleted_users", AsyncMock())
    return workloads


async def test_sync_deletion_retries_pending_scan_with_fresh_settings(configured, monkeypatch):
    w = configured
    async with database.get_db() as db:
        await UserCRUD.create_or_update(db, "retired")
    original = requests.panel_get

    async def get(panel, path, **kwargs):
        if path == "/api/user/retired":
            return SimpleNamespace(status_code=404, json=lambda: {"detail": "User not found"})
        return await original(panel, path, **kwargs)

    monkeypatch.setattr(requests, "panel_get", get)
    old_revision = get_config_revision()
    w.sync_release.clear()
    syncing = w.start_sync()
    await asyncio.wait_for(w.sync_started.wait(), 2)
    waits = []
    outcomes = []

    async def scheduled_wait(_admission, seconds):
        waits.append(seconds)
        outcomes.append(api.get_last_cycle_stats())
        if len(waits) == 1:
            assert not w.ip_started.is_set()
            assert not w.cycle.warning.warnings
        else:
            raise StopLoop

    monkeypatch.setattr(usage, "wait_for_monitoring_change", scheduled_wait)
    runner = asyncio.create_task(usage.run_check_users_usage(w.cycle.panel))
    try:
        await asyncio.sleep(0.05)
        assert not w.ip_started.is_set()
        w.sync_release.set()
        assert await asyncio.wait_for(syncing, 2) == (2, 0, 1)
        with pytest.raises(StopLoop):
            await asyncio.wait_for(runner, 3)
        assert get_config_revision() > old_revision
        assert outcomes[0]["collection_outcome"] == "superseded"
        assert outcomes[0]["fetched_ok"] == 0
        assert waits[0] == 5.0
        assert waits[1] > 250  # Real collection keeps the configured 300s cadence.
        assert w.order == ["sync", "ip", "ip"]
        assert outcomes[1]["fetched_ok"] == 2
        assert all(warning.consecutive_violations == 1 for warning in w.cycle.warning.warnings.values())
    finally:
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)


async def test_stale_admission_does_not_begin_sample_or_count_outage(configured, monkeypatch):
    import utils.shared_state as state

    w = configured
    old_config = w.cycle.config
    generation = state._api_generation
    monkeypatch.setattr(api, "_consecutive_dead_cycles", 1)
    await save_config_value("general_limit", 2)
    assert await api.collect_active_users_from_api(w.cycle.panel, old_config) is False
    assert state._api_generation == generation
    assert api._consecutive_dead_cycles == 1
    assert not w.ip_started.is_set()
    assert api.get_last_cycle_stats()["retry_pending_collection"] is True


async def test_change_after_ip_request_keeps_normal_cadence(configured, monkeypatch):
    w = configured
    original = requests.panel_request
    changed = False

    async def request(*args, **kwargs):
        nonlocal changed
        if not changed:
            changed = True
            await save_config_value("general_limit", 2)
        return await original(*args, **kwargs)

    waits = []

    async def scheduled_wait(_admission, seconds):
        waits.append(seconds)
        raise StopLoop

    monkeypatch.setattr(requests, "panel_request", request)
    monkeypatch.setattr(usage, "wait_for_monitoring_change", scheduled_wait)
    with pytest.raises(StopLoop):
        await asyncio.wait_for(usage.run_check_users_usage(w.cycle.panel), 3)
    assert w.ip_started.is_set()
    assert waits[0] > 250
    assert not api.get_last_cycle_stats().get("retry_pending_collection")
    assert not w.cycle.warning.warnings


@pytest.mark.parametrize("next_state", ["success", "unavailable", "degraded", "logs"])
async def test_retry_hint_does_not_leak_into_the_next_attempt(configured, monkeypatch, next_state):
    w = configured
    await save_config_value("general_limit", 2)
    assert await api.collect_active_users_from_api(w.cycle.panel, w.cycle.config) is False
    assert api.get_last_cycle_stats()["retry_pending_collection"] is True
    fresh = await read_config(refresh=True)
    if next_state == "unavailable":
        monkeypatch.setattr(requests, "is_panel_available", lambda: False)
    elif next_state == "degraded":
        fresh["config_degraded"] = True
    elif next_state == "logs":
        fresh["ip_source"] = "logs"
    result = await api.collect_active_users_from_api(w.cycle.panel, fresh)
    assert result is (next_state == "success")
    assert api.get_last_cycle_stats()["retry_pending_collection"] is False


async def test_panel_outage_keeps_normal_cadence(configured, monkeypatch):
    monkeypatch.setattr(requests, "is_panel_available", lambda: False)
    waits = []

    async def scheduled_wait(_admission, seconds):
        waits.append(seconds)
        raise StopLoop

    monkeypatch.setattr(usage, "wait_for_monitoring_change", scheduled_wait)
    with pytest.raises(StopLoop):
        await asyncio.wait_for(usage.run_check_users_usage(configured.cycle.panel), 3)
    assert waits[0] > 250
    assert api.get_last_cycle_stats()["collection_outcome"] == "failure"
    assert not configured.ip_started.is_set()
