"""Logs enforcement ages original observations across asynchronous preparation."""

import json
from pathlib import Path

import pytest

import db.database as database
import utils.check_usage as usage
import utils.read_config as config
import utils.settings_service as policies
from db.crud.config import ConfigCRUD
from test_config_revisions import settings_db  # noqa: F401
from test_log_partial_enforcement import partial_cycle  # noqa: F401
from test_policy_enforcement import policy_cycle  # noqa: F401
from utils.enforcement_guard import EnforcementProtected, start_enforcement_effect, validate_enforcement
from utils.log_observations import LOG_OBSERVATIONS
from utils.shared_state import note_node_event


@pytest.fixture
async def fresh_cycle(partial_cycle, request):
    """Keep real policies, counting, warning persistence and fleet evidence."""
    cycle = partial_cycle
    cycle.complete = getattr(request, "param", False)
    async with database.get_db() as db:
        await ConfigCRUD.set(db, "check_interval", "300")
        await ConfigCRUD.set(db, "api_ip_freshness", "20")
    cfg = await config.read_config(refresh=True)
    cycle.config.clear()
    cycle.config.update(cfg)
    LOG_OBSERVATIONS.configure(cycle.config)
    LOG_OBSERVATIONS.register_fleet(cycle.fleet(recovered=cycle.complete))
    node_ids = (1, 2, 3) if cycle.complete else (1, 2)
    for node_id in node_ids:
        note_node_event(node_id)
    cycle.clock[0] += 301
    for node_id in node_ids:
        note_node_event(node_id)
    original_observed = cycle.observed
    original_warning = cycle.warning

    def observed(username, *, age=19.0, count=2):
        user = original_observed(username, count=count)
        for connection in user.device_info.connections:
            connection.last_seen = cycle.clock[0] - age
        return user

    def warning(username="alice", *, expired=False, count=2):
        record = original_warning(username, expired=expired, count=count)
        record.last_scan_time = cycle.clock[0] - 301
        record.trust_score = 80.0
        return record

    def advance(seconds=1.0):
        cycle.clock[0] += seconds
        LOG_OBSERVATIONS.register_fleet(cycle.fleet(recovered=cycle.complete))
        for node_id in node_ids:
            note_node_event(node_id)

    cycle.observed = observed
    cycle.warning = warning
    cycle.advance = advance
    return cycle


@pytest.mark.parametrize("age,warning_count", [(19.999, 1), (20.0, 0)])
async def test_logs_count_exact_freshness_boundary_independently_of_scan_cadence(fresh_cycle, age, warning_count):
    """Using the 300-second cadence as recency would warn on the expired event."""
    cycle = fresh_cycle
    await cycle.run([cycle.observed("alice", age=age)])

    assert len(cycle.system.warnings) == warning_count
    assert cycle.applied == []
    if warning_count:
        warning = cycle.system.warnings["alice"]
        assert warning.ip_count == 2
        assert warning.ips == {"198.51.100.10"}


@pytest.mark.parametrize("fresh_cycle", [False, True], indirect=True)
async def test_same_ip_on_two_nodes_drops_to_one_device_when_one_observation_expires(fresh_cycle, monkeypatch):
    """Expiring one node changes the device count while the unique IP remains active."""
    cycle = fresh_cycle
    record = cycle.warning()
    user = cycle.observed("alice")
    user.device_info.connections[1].last_seen = cycle.clock[0] - 1

    async def delayed_history(_users):
        cycle.advance()

    monkeypatch.setattr(usage.ip_history_tracker, "record_many", delayed_history)
    await cycle.run([user])

    assert cycle.applied == []
    assert record.consecutive_violations == 2
    if cycle.complete:
        assert cycle.system.warnings == {}
    else:
        assert cycle.system.warnings["alice"] is record
        assert record.trust_score == 80.0


@pytest.mark.parametrize("fresh_cycle", [False, True], indirect=True)
@pytest.mark.parametrize("phase", ["snapshot", "report", "history"])
async def test_logs_expiring_during_preparation_do_not_advance_warning(fresh_cycle, monkeypatch, phase):
    """A stale precomputed count must neither warn nor clear a partial record."""
    cycle = fresh_cycle
    record = cycle.warning()
    await cycle.system.save_warnings()
    before = json.loads(Path(cycle.system.filename).read_text())["alice"]
    if phase == "snapshot":
        original = usage.read_config_snapshot

        async def delayed_snapshot(*args, **kwargs):
            result = await original(*args, **kwargs)
            cycle.advance()
            return result

        monkeypatch.setattr(usage, "read_config_snapshot", delayed_snapshot)
    elif phase == "report":
        original = usage.check_ip_used

        async def delayed_report(*args, **kwargs):
            result = await original(*args, **kwargs)
            cycle.advance()
            return result

        monkeypatch.setattr(usage, "check_ip_used", delayed_report)
    else:

        async def delayed_history(_users):
            cycle.advance()

        monkeypatch.setattr(usage.ip_history_tracker, "record_many", delayed_history)
    await cycle.run([cycle.observed("alice")])

    assert cycle.applied == []
    assert record.consecutive_violations == 2
    if cycle.complete:
        assert cycle.system.warnings == {}
    else:
        assert cycle.system.warnings["alice"] is record
        assert json.loads(Path(cycle.system.filename).read_text())["alice"] == before


@pytest.mark.parametrize("fresh_cycle", [False, True], indirect=True)
async def test_logs_expiring_during_warning_save_do_not_punish(fresh_cycle, monkeypatch):
    """The saved third observation cannot authorize a later stale punishment."""
    cycle = fresh_cycle
    record = cycle.warning()
    original = cycle.system.add_warning

    async def delayed_warning(*args, **kwargs):
        result = await original(*args, **kwargs)
        cycle.advance()
        return result

    monkeypatch.setattr(cycle.system, "add_warning", delayed_warning)
    await cycle.run([cycle.observed("alice")])

    assert cycle.applied == []
    assert record.consecutive_violations == 3
    if cycle.complete:
        assert cycle.system.warnings == {}
    else:
        assert cycle.system.warnings["alice"] is record
        assert json.loads(Path(cycle.system.filename).read_text())["alice"]["consecutive_violations"] == 3


@pytest.mark.parametrize("fresh_cycle", [False, True], indirect=True)
async def test_logs_expiring_during_persistent_policy_read_do_not_punish(fresh_cycle, monkeypatch):
    """A final policy read must recount an expired ready-node lower bound."""
    cycle = fresh_cycle
    record = cycle.warning(expired=True, count=3)
    original = policies.read_user_policy
    advanced = False

    async def delayed_policy(*args, **kwargs):
        nonlocal advanced
        result = await original(*args, **kwargs)
        if not advanced:
            cycle.advance()
            advanced = True
        return result

    monkeypatch.setattr(policies, "read_user_policy", delayed_policy)
    await cycle.run([cycle.observed("alice")])

    assert cycle.applied == []
    assert record.consecutive_violations == 3
    if cycle.complete:
        assert cycle.system.warnings == {}
    else:
        assert cycle.system.warnings["alice"] is record


@pytest.mark.parametrize("fresh_cycle", [False, True], indirect=True)
@pytest.mark.parametrize("expired", [False, True])
async def test_logs_expiring_at_final_panel_boundary_preserve_incident(fresh_cycle, monkeypatch, expired):
    """Refreshing without comparing the resulting count would start a stale disable."""
    import utils.warning_system as warning_module
    import utils.warning_system.enhanced_system as warnings

    cycle = fresh_cycle
    record = cycle.warning(expired=expired, count=3 if expired else 2)

    async def prepared_punishment(_panel, user):
        await validate_enforcement()
        cycle.advance()
        try:
            start_enforcement_effect()
        except EnforcementProtected:
            return {"protected": True}
        cycle.applied.append(user.name)
        return {"action": "disabled", "duration_minutes": 15, "violation_count": 1, "step_index": 0}

    monkeypatch.setattr(warning_module, "safe_disable_user_with_punishment", prepared_punishment)
    monkeypatch.setattr(warnings, "safe_disable_user_with_punishment", prepared_punishment)
    await cycle.run([cycle.observed("alice")])

    assert cycle.applied == []
    assert cycle.system.warnings["alice"] is record
    assert record.consecutive_violations == 3
