"""Punishment and normalization use current canonical eligibility after awaits."""

import asyncio
import json
import os
import sys
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import db.database as database
import utils.check_usage as usage
import utils.read_config as config
import utils.user_sync as sync
from db.crud.config import ConfigCRUD
from db.crud.users import UserCRUD
from test_config_revisions import settings_db  # noqa: F401
from utils.shared_state import ACTIVE_USERS
from utils.types import ConnectionInfo, PanelType, UserType
from utils.warning_system.enhanced_system import EnhancedWarningSystem
from utils.warning_system.user_warning import UserLimitWarning


@pytest.fixture
async def policy_cycle(settings_db, monkeypatch, completed_logs_evidence):
    import utils.warning_system as warning_module
    import utils.warning_system.enhanced_system as warnings

    async with database.get_db() as db:
        await ConfigCRUD.set(db, "general_limit", "1")
        await ConfigCRUD.set(db, "group_filter_enabled", "true")
        await ConfigCRUD.set(db, "group_filter_ids", "7")
        await ConfigCRUD.set(db, "ip_source", "logs")
        await UserCRUD.create_or_update(db, "alice", group_ids=[7], owner_username="admin")
        await UserCRUD.set_special_limit(db, "alice", 1, fetch_from_panel=False)
    cfg = await config.read_config()
    system = EnhancedWarningSystem(
        filename=str(settings_db.path / "warnings.json"), history_filename=str(settings_db.path / "history.json")
    )
    applied = []

    async def punish(_panel, user):
        applied.append(user.name)
        return {"action": "disabled", "duration_minutes": 15, "violation_count": 1, "step_index": 0}

    monkeypatch.setattr(usage, "warning_system", system)
    monkeypatch.setattr(usage, "isp_detector", None)
    monkeypatch.setattr(usage.ISPDetector, "get_multiple_isp_info", AsyncMock(return_value={}))
    monkeypatch.setattr(usage, "send_logs", AsyncMock())
    monkeypatch.setattr(usage, "send_user_message", AsyncMock())
    monkeypatch.setattr(usage.ip_history_tracker, "record_many", AsyncMock())
    monkeypatch.setattr(warning_module, "safe_disable_user_with_punishment", punish)
    monkeypatch.setattr(warnings, "safe_disable_user_with_punishment", punish)
    for module in (warnings, warning_module):
        for name in ("safe_send_disable_notification", "safe_send_warning_log", "safe_send_logs"):
            if hasattr(module, name):
                monkeypatch.setattr(module, name, AsyncMock())
    sync.USER_METADATA_CACHE.clear()
    ACTIVE_USERS.clear()
    panel = PanelType("admin", "test", "panel.local")

    def warning(*, expired=False, count=2):
        now = time.time()
        record = UserLimitWarning(
            "alice",
            2,
            {"5.6.7.8", "9.9.9.9"},
            now - 120,
            now - 1 if expired else now + 600,
            consecutive_violations=count,
            last_scan_time=now - 61,
            max_warnings_at_creation=3,
        )
        system.warnings["alice"] = record
        return record

    async def run():
        current = await config.read_config(refresh=True)
        completed_logs_evidence(current)
        user = UserType(name="alice", ip=["5.6.7.8", "9.9.9.9"])
        user.device_info.connections = [ConnectionInfo(ip, 1, "node", "Vless", time.time() - 1) for ip in user.ip]
        ACTIVE_USERS["alice"] = user
        await usage.check_users_usage(panel, current)

    yield SimpleNamespace(system=system, config=cfg, panel=panel, applied=applied, warning=warning, run=run)
    ACTIVE_USERS.clear()
    sync.USER_METADATA_CACHE.clear()


@pytest.mark.parametrize("excluded", ["group", "admin", "whitelist", "unknown"])
async def test_expired_current_excluded_policy_retains_warning(policy_cycle, excluded):
    async with database.get_db() as db:
        if excluded == "group":
            await UserCRUD.create_or_update(db, "alice", group_ids=[])
        elif excluded == "admin":
            await ConfigCRUD.set(db, "admin_filter_enabled", "true")
            await ConfigCRUD.set(db, "admin_filter_mode", "exclude")
            await ConfigCRUD.set(db, "admin_filter_usernames", "admin")
        elif excluded == "whitelist":
            await UserCRUD.set_excepted(db, "alice", True, fetch_from_panel=False)
        else:
            await UserCRUD.delete(db, "alice")
    cfg = await config.read_config(refresh=True)
    record = policy_cycle.warning(expired=True, count=3)
    await policy_cycle.system.save_warnings()
    await policy_cycle.system.check_persistent_violations(
        policy_cycle.panel, {"alice": record.ips}, cfg, device_counts={"alice": 2}, known_users={"alice"}
    )
    assert policy_cycle.applied == []
    assert policy_cycle.system.warnings["alice"] is record
    assert record.consecutive_violations == 3


@pytest.mark.parametrize("edit", ["group", "owner"])
async def test_normal_fact_edit_after_warning_preserves_saved_third_observation(policy_cycle, monkeypatch, edit):
    if edit == "owner":
        async with database.get_db() as db:
            await ConfigCRUD.set(db, "admin_filter_enabled", "true")
            await ConfigCRUD.set(db, "admin_filter_usernames", "admin")
    record = policy_cycle.warning()
    await policy_cycle.system.save_warnings()
    original = policy_cycle.system.add_warning
    revisions = []

    async def changed_warning(*args, **kwargs):
        result = await original(*args, **kwargs)
        revisions.append(config.get_config_revision())
        async with database.get_db() as db:
            facts = {"group_ids": []} if edit == "group" else {"owner_username": "other"}
            await UserCRUD.create_or_update(db, "alice", **facts)
        revisions.append(config.get_config_revision())
        return result

    monkeypatch.setattr(policy_cycle.system, "add_warning", changed_warning)
    await policy_cycle.run()
    assert revisions[0] == revisions[1]
    assert policy_cycle.applied == []
    assert policy_cycle.system.warnings["alice"] is record
    assert record.consecutive_violations == 3
    assert json.loads(open(policy_cycle.system.filename).read())["alice"]["consecutive_violations"] == 3


@pytest.mark.parametrize("expired", [False, True])
async def test_eligible_threshold_ignores_cached_derived_authority(policy_cycle, expired):
    record = policy_cycle.warning(expired=expired, count=3 if expired else 2)
    sync.USER_METADATA_CACHE["alice"] = {
        "group_ids": [],
        "owner_username": "other",
        "is_monitored": False,
        "is_excepted": True,
        "effective_ip_limit": 99,
        "special_limit": 99,
    }
    await policy_cycle.run()
    assert policy_cycle.applied == ["alice"]
    assert "alice" not in policy_cycle.system.warnings
    assert record.consecutive_violations == 3


@pytest.mark.parametrize("edit", ["special", "whitelist"])
@pytest.mark.parametrize("external", [False, True])
async def test_config_edit_after_warning_retires_evidence_and_retains_record(
    policy_cycle, settings_db, monkeypatch, edit, external
):
    record = policy_cycle.warning()
    original = policy_cycle.system.add_warning

    async def changed_warning(*args, **kwargs):
        result = await original(*args, **kwargs)
        if external:
            script = """
import asyncio, sys
from db.crud.users import UserCRUD
from db.database import get_db, close_db
async def write():
    try:
        async with get_db() as db:
            if sys.argv[1] == "special":
                await UserCRUD.set_special_limit(db, "alice", 9, fetch_from_panel=False)
            else:
                await UserCRUD.set_excepted(db, "alice", True, fetch_from_panel=False)
    finally:
        await close_db()
asyncio.run(write())
"""
            env = os.environ.copy()
            env.update(DATABASE_URL=settings_db.url, LOG_LEVEL="CRITICAL")
            child = await asyncio.create_subprocess_exec(
                sys.executable,
                "-c",
                script,
                edit,
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _out, err = await asyncio.wait_for(child.communicate(), 10)
            assert child.returncode == 0, err.decode()
        else:
            async with database.get_db() as db:
                if edit == "special":
                    await UserCRUD.set_special_limit(db, "alice", 9, fetch_from_panel=False)
                else:
                    await UserCRUD.set_excepted(db, "alice", True, fetch_from_panel=False)
        return result

    monkeypatch.setattr(policy_cycle.system, "add_warning", changed_warning)
    await policy_cycle.run()
    assert policy_cycle.applied == []
    assert policy_cycle.system.warnings["alice"] is record
    assert record.consecutive_violations == 3
    assert json.loads(open(policy_cycle.system.filename).read())["alice"]["consecutive_violations"] == 3


@pytest.mark.parametrize("excluded", ["group", "admin", "whitelist", "unknown"])
async def test_full_scan_never_cleans_expired_excluded_user(policy_cycle, excluded):
    record = policy_cycle.warning(expired=True, count=3)
    async with database.get_db() as db:
        if excluded == "group":
            await UserCRUD.create_or_update(db, "alice", group_ids=[])
        elif excluded == "admin":
            await ConfigCRUD.set(db, "admin_filter_enabled", "true")
            await ConfigCRUD.set(db, "admin_filter_mode", "exclude")
            await ConfigCRUD.set(db, "admin_filter_usernames", "admin")
        elif excluded == "whitelist":
            await UserCRUD.set_excepted(db, "alice", True, fetch_from_panel=False)
        else:
            await UserCRUD.delete(db, "alice")
    await policy_cycle.run()
    assert policy_cycle.applied == []
    assert policy_cycle.system.warnings["alice"] is record
    assert record.consecutive_violations == 3


@pytest.mark.parametrize("expired", [False, True])
async def test_owner_none_preserves_exact_admin_predicate(policy_cycle, expired):
    async with database.get_db() as db:
        await ConfigCRUD.set(db, "admin_filter_enabled", "true")
        await ConfigCRUD.set(db, "admin_filter_usernames", "other")
        # create_or_update intentionally preserves owner for a missing argument.
        from sqlalchemy import update
        from db.models import User

        await db.execute(update(User).where(User.username == "alice").values(owner_username=None))
    policy_cycle.warning(expired=expired, count=3 if expired else 2)
    await policy_cycle.run()
    assert policy_cycle.applied == ["alice"]


@pytest.mark.parametrize("phase", ["snapshot", "final_policy"])
async def test_source_change_during_factual_read_never_punishes_or_normalizes(policy_cycle, monkeypatch, phase):
    import utils.settings_service as policies
    from utils.shared_state import discard_source_observations

    record = policy_cycle.warning()
    if phase == "snapshot":
        original = config.read_config_snapshot

        async def changed(*args, **kwargs):
            snapshot = await original(*args, **kwargs)
            await discard_source_observations()
            return snapshot

        monkeypatch.setattr(usage, "read_config_snapshot", changed, raising=False)
    else:
        original = policies.read_user_policy

        async def changed(*args, **kwargs):
            policy = await original(*args, **kwargs)
            await discard_source_observations()
            return policy

        monkeypatch.setattr(policies, "read_user_policy", changed)
    await policy_cycle.run()
    assert policy_cycle.applied == []
    assert policy_cycle.system.warnings["alice"] is record
    assert record.consecutive_violations == (2 if phase == "snapshot" else 3)
    assert ACTIVE_USERS == {}


async def test_unavailable_current_facts_during_normalization_retain_saved_counter(policy_cycle, monkeypatch):
    import utils.settings_service as policies

    record = policy_cycle.warning(count=1)

    async def unavailable(*args, **kwargs):
        raise policies.SettingsUnavailable("current facts unavailable")

    monkeypatch.setattr(policies, "read_user_policy", unavailable)
    await policy_cycle.run()
    assert policy_cycle.applied == []
    assert policy_cycle.system.warnings["alice"] is record
    assert record.consecutive_violations == 2


@pytest.mark.parametrize("expired", [False, True])
async def test_current_group_limit_after_fact_edit_is_shared_by_both_paths(policy_cycle, monkeypatch, expired):
    import utils.settings_service as policies

    async with database.get_db() as db:
        await ConfigCRUD.set(db, "group_filter_ids", "7,8")
        await ConfigCRUD.set(db, "group_limits", '{"7": 1, "8": 4}')
        await UserCRUD.set_special_limit(db, "alice", None, fetch_from_panel=False)
    record = policy_cycle.warning(expired=expired, count=3 if expired else 2)
    revisions = []

    async def change_group():
        revisions.append(config.get_config_revision())
        async with database.get_db() as db:
            await UserCRUD.create_or_update(db, "alice", group_ids=[8])
        revisions.append(config.get_config_revision())

    if expired:
        original = policies.read_user_policy
        changed = False

        async def changed_policy(*args, **kwargs):
            nonlocal changed
            if not changed:
                changed = True
                await change_group()
            return await original(*args, **kwargs)

        monkeypatch.setattr(policies, "read_user_policy", changed_policy)
    else:
        original = policy_cycle.system.add_warning

        async def changed_warning(*args, **kwargs):
            result = await original(*args, **kwargs)
            await change_group()
            return result

        monkeypatch.setattr(policy_cycle.system, "add_warning", changed_warning)
    await policy_cycle.run()
    assert revisions[0] == revisions[1]
    assert policy_cycle.applied == []
    assert "alice" not in policy_cycle.system.warnings
    assert record.consecutive_violations == 3


@pytest.fixture
def topics_admission(monkeypatch):
    """Expose the real gate contract while the standalone PR lacks its module."""
    from dataclasses import dataclass
    from types import ModuleType

    @dataclass(frozen=True)
    class Admission:
        allowed: bool
        reason: str
        revision: int
        manager_id: int
        group_id: int | None

    state = SimpleNamespace(allowed=True, revision=0)
    module = ModuleType("utils.monitoring_gate")

    def snapshot():
        return Admission(state.allowed, "" if state.allowed else "Topics are disabled", state.revision, 1, -100)

    module.monitoring_snapshot = snapshot
    module.monitoring_is_current = lambda admission: state.allowed and snapshot() == admission
    monkeypatch.setitem(sys.modules, module.__name__, module)

    def transition(*, resume=False):
        state.allowed = False
        state.revision += 1
        if resume:
            state.allowed = True
            state.revision += 1

    return SimpleNamespace(state=state, transition=transition, module=module)


@pytest.mark.parametrize("resume", [False, True])
async def test_topics_transition_during_warning_save_retains_single_observation(
    policy_cycle, topics_admission, monkeypatch, resume
):
    record = policy_cycle.warning()
    original = policy_cycle.system.add_warning

    async def changed(*args, **kwargs):
        result = await original(*args, **kwargs)
        topics_admission.transition(resume=resume)
        return result

    monkeypatch.setattr(policy_cycle.system, "add_warning", changed)
    await policy_cycle.run()
    assert policy_cycle.applied == []
    assert policy_cycle.system.warnings["alice"] is record
    assert record.consecutive_violations == 3
    assert json.loads(open(policy_cycle.system.filename).read())["alice"]["consecutive_violations"] == 3


@pytest.mark.parametrize("expired", [False, True])
@pytest.mark.parametrize("resume", [False, True])
async def test_topics_transition_during_final_policy_read_never_dispatches(
    policy_cycle, topics_admission, monkeypatch, expired, resume
):
    import utils.settings_service as policies

    record = policy_cycle.warning(expired=expired, count=3 if expired else 2)
    original = policies.read_user_policy

    async def changed(*args, **kwargs):
        policy = await original(*args, **kwargs)
        topics_admission.transition(resume=resume)
        return policy

    monkeypatch.setattr(policies, "read_user_policy", changed)
    await policy_cycle.run()
    assert policy_cycle.applied == []
    assert policy_cycle.system.warnings["alice"] is record
    assert record.consecutive_violations == 3


@pytest.mark.parametrize("resume", [False, True])
async def test_direct_expired_path_rechecks_topics_after_policy_await(
    policy_cycle, topics_admission, monkeypatch, resume
):
    import utils.settings_service as policies

    record = policy_cycle.warning(expired=True, count=3)
    original = policies.read_user_policy

    async def changed(*args, **kwargs):
        policy = await original(*args, **kwargs)
        topics_admission.transition(resume=resume)
        return policy

    monkeypatch.setattr(policies, "read_user_policy", changed)
    await policy_cycle.system.check_persistent_violations(
        policy_cycle.panel, {"alice": record.ips}, policy_cycle.config, device_counts={"alice": 2}
    )
    assert policy_cycle.applied == []
    assert policy_cycle.system.warnings["alice"] is record


async def test_topics_paused_at_admission_preserves_unconsumed_sample(policy_cycle, topics_admission):
    record = policy_cycle.warning()
    topics_admission.transition()
    await policy_cycle.run()
    assert policy_cycle.applied == []
    assert policy_cycle.system.warnings["alice"] is record
    assert record.consecutive_violations == 2
    assert "alice" in ACTIVE_USERS


@pytest.mark.parametrize("excluded", ["group", "owner"])
async def test_delayed_report_cannot_cleanup_newly_excluded_warning(policy_cycle, monkeypatch, excluded):
    import utils.settings_service as policies
    from utils.log_observations import LOG_OBSERVATIONS
    from utils.shared_state import note_node_event

    if excluded == "owner":
        async with database.get_db() as db:
            await ConfigCRUD.set(db, "admin_filter_enabled", "true")
            await ConfigCRUD.set(db, "admin_filter_usernames", "admin")
    record = policy_cycle.warning(count=1)
    observed = []

    async def report(*args, **kwargs):
        revision = config.get_config_revision()
        async with database.get_db() as db:
            facts = {"group_ids": []} if excluded == "group" else {"owner_username": "other"}
            await UserCRUD.create_or_update(db, "alice", **facts)
        assert config.get_config_revision() == revision
        # Keep real transport/fleet evidence healthy while the report outlasts
        # the warning window. A single clock jump would model an outage instead.
        later = record.monitoring_end_time + 1
        now = time.time()
        while now < later:
            now = min(now + 60, later)
            monkeypatch.setattr(time, "time", lambda: now)
            note_node_event(1)
        LOG_OBSERVATIONS.fleet_seen = later
        assert LOG_OBSERVATIONS.is_current(LOG_OBSERVATIONS.evidence())
        observed.append(await policies.read_user_policy("alice"))
        assert not observed[-1].eligible

    monkeypatch.setattr(usage, "dispatch_chunked_warnings", report)
    await policy_cycle.run()
    assert observed and not observed[-1].eligible
    assert policy_cycle.applied == []
    assert record.consecutive_violations == 2
    assert policy_cycle.system.warnings.get("alice") is record
    assert json.loads(open(policy_cycle.system.filename).read())["alice"]["consecutive_violations"] == 2


async def test_delayed_report_cannot_remove_replacement_warning(policy_cycle, monkeypatch):
    original_record = policy_cycle.warning(count=1)
    replacement = None

    async def report(*args, **kwargs):
        nonlocal replacement
        replacement = policy_cycle.warning(expired=True, count=1)
        await policy_cycle.system.save_warnings()

    monkeypatch.setattr(usage, "dispatch_chunked_warnings", report)
    await policy_cycle.run()
    assert replacement is not None and replacement is not original_record
    assert policy_cycle.system.warnings.get("alice") is replacement
    assert json.loads(open(policy_cycle.system.filename).read())["alice"]["consecutive_violations"] == 1


async def test_threshold_clear_during_remote_await_cannot_remove_replacement_warning(policy_cycle, monkeypatch):
    import utils.warning_system as warning_module

    original_record = policy_cycle.warning(count=2)
    replacement = None

    async def punish(panel, user):
        nonlocal replacement
        policy_cycle.applied.append(user.name)
        await policy_cycle.system.clear_user_trust_data("alice")
        replacement = policy_cycle.warning(count=1)
        await policy_cycle.system.save_warnings()
        return {"action": "disabled", "duration_minutes": 15, "violation_count": 1, "step_index": 0}

    monkeypatch.setattr(warning_module, "safe_disable_user_with_punishment", punish)
    await policy_cycle.run()
    assert policy_cycle.applied == ["alice"]
    assert original_record.consecutive_violations == 3
    assert replacement is not None
    assert policy_cycle.system.warnings.get("alice") is replacement
    assert replacement.consecutive_violations == 1
    assert json.loads(open(policy_cycle.system.filename).read())["alice"]["consecutive_violations"] == 1


@pytest.mark.parametrize(
    ("action", "newly_applied", "attempts"),
    [
        ("disabled", True, 1),
        ("reconciled", False, 1),
        ("applied_unrecorded", True, 1),
        ("applied_unrecorded", False, 1),
        ("reconciled", False, 2),
    ],
)
async def test_expired_remote_incident_preserves_replacement_warning(
    policy_cycle, monkeypatch, action, newly_applied, attempts
):
    import utils.warning_system.enhanced_system as warning_module

    for _attempt in range(attempts):
        original_record = policy_cycle.warning(expired=True, count=3)
        entered = asyncio.Event()
        release = asyncio.Event()

        async def punish(_panel, _user):
            entered.set()
            await release.wait()
            return {
                "action": action,
                "newly_applied": newly_applied,
                "duration_minutes": 15,
                "violation_count": 1,
                "step_index": 0,
            }

        monkeypatch.setattr(warning_module, "safe_disable_user_with_punishment", punish)
        running = asyncio.create_task(policy_cycle.run())
        try:
            await asyncio.wait_for(entered.wait(), timeout=5)
            # Management can clear this incident and create a new owner while
            # the old panel operation is still awaiting its response.
            await policy_cycle.system.clear_user_trust_data("alice")
            replacement = policy_cycle.warning(count=1)
            await policy_cycle.system.save_warnings()
            with open(policy_cycle.system.filename, encoding="utf-8") as file:
                saved_replacement = json.load(file)["alice"]
        finally:
            release.set()
            await asyncio.wait_for(running, timeout=5)

        assert original_record.consecutive_violations == 3
        assert policy_cycle.system.warnings.get("alice") is replacement
        assert replacement.consecutive_violations == 1
        with open(policy_cycle.system.filename, encoding="utf-8") as file:
            assert json.load(file)["alice"] == saved_replacement


async def test_normalization_save_cannot_clear_new_warning_history(policy_cycle, monkeypatch):
    async with database.get_db() as db:
        await UserCRUD.set_special_limit(db, "alice", 9, fetch_from_panel=False)
    original_record = policy_cycle.warning(count=1)
    original_save = policy_cycle.system.save_warnings
    replacement = None
    new_history = [time.time()]

    async def save_and_replace():
        nonlocal replacement
        await original_save()
        if replacement is None:
            replacement = policy_cycle.warning(count=1)
            policy_cycle.system.warning_history["alice"] = new_history
            await original_save()
            await policy_cycle.system.save_warning_history()

    monkeypatch.setattr(policy_cycle.system, "save_warnings", save_and_replace)
    await policy_cycle.run()
    assert policy_cycle.applied == []
    assert replacement is not None and replacement is not original_record
    assert policy_cycle.system.warnings.get("alice") is replacement
    assert policy_cycle.system.warning_history.get("alice") is new_history
    assert json.loads(open(policy_cycle.system.history_filename).read())["alice"] == new_history


@pytest.mark.parametrize("excluded", ["group", "admin", "whitelist", "unknown"])
async def test_direct_expired_cleanup_preserves_current_excluded_policy(policy_cycle, excluded):
    record = policy_cycle.warning(expired=True, count=3)
    async with database.get_db() as db:
        if excluded == "group":
            await UserCRUD.create_or_update(db, "alice", group_ids=[])
        elif excluded == "admin":
            await ConfigCRUD.set(db, "admin_filter_enabled", "true")
            await ConfigCRUD.set(db, "admin_filter_mode", "exclude")
            await ConfigCRUD.set(db, "admin_filter_usernames", "admin")
        elif excluded == "whitelist":
            await UserCRUD.set_excepted(db, "alice", True, fetch_from_panel=False)
        else:
            await UserCRUD.delete(db, "alice")
    await policy_cycle.system.save_warnings()
    await policy_cycle.system.cleanup_expired_warnings()
    assert policy_cycle.system.warnings.get("alice") is record
    assert record.consecutive_violations == 3


async def test_expired_cleanup_cannot_remove_warning_renewed_during_policy_await(policy_cycle, monkeypatch):
    import utils.settings_service as policies

    record = policy_cycle.warning(expired=True, count=1)
    original_read = policies.read_user_policy

    async def renew_warning(*args, **kwargs):
        policy = await original_read(*args, **kwargs)
        await policy_cycle.system.add_warning("alice", 2, record.ips, 1, send_telegram_notification=False)
        return policy

    monkeypatch.setattr(policies, "read_user_policy", renew_warning)
    await policy_cycle.system.cleanup_expired_warnings()
    assert policy_cycle.applied == []
    assert policy_cycle.system.warnings.get("alice") is record
    assert record.consecutive_violations == 2
    assert record.is_monitoring_active()
    assert json.loads(open(policy_cycle.system.filename).read())["alice"]["consecutive_violations"] == 2


@pytest.mark.parametrize("phase", ["warning_save", "final_policy"])
@pytest.mark.parametrize("replace", [False, True])
async def test_cleared_or_replaced_threshold_warning_cannot_authorize_punishment(
    policy_cycle, monkeypatch, phase, replace
):
    import utils.settings_service as policies

    original_record = policy_cycle.warning()
    replacement = None

    async def clear_or_replace():
        nonlocal replacement
        await policy_cycle.system.clear_user_trust_data("alice")
        if replace:
            replacement = policy_cycle.warning(count=1)
            await policy_cycle.system.save_warnings()

    if phase == "warning_save":
        original = policy_cycle.system.add_warning

        async def changed(*args, **kwargs):
            result = await original(*args, **kwargs)
            await clear_or_replace()
            return result

        monkeypatch.setattr(policy_cycle.system, "add_warning", changed)
    else:
        original = policies.read_user_policy

        async def changed(*args, **kwargs):
            result = await original(*args, **kwargs)
            await clear_or_replace()
            return result

        monkeypatch.setattr(policies, "read_user_policy", changed)
    await policy_cycle.run()
    assert policy_cycle.applied == []
    assert original_record.consecutive_violations == 3
    if replace:
        assert policy_cycle.system.warnings["alice"] is replacement
        assert replacement.consecutive_violations == 1
    else:
        assert "alice" not in policy_cycle.system.warnings


async def test_pending_expired_normalization_does_not_remove_replaced_record(policy_cycle, monkeypatch):
    import utils.settings_service as policies

    original_record = policy_cycle.warning(expired=True, count=3)
    replacement = None
    reads = 0
    original = policies.read_user_policy

    async def changed(*args, **kwargs):
        nonlocal replacement, reads
        result = await original(*args, **kwargs)
        reads += 1
        if reads == 2:
            replacement = policy_cycle.warning(count=1)
        return result

    monkeypatch.setattr(policies, "read_user_policy", changed)
    await policy_cycle.system.check_persistent_violations(
        policy_cycle.panel, {"alice": original_record.ips}, policy_cycle.config, device_counts={"alice": 1}
    )
    assert policy_cycle.applied == []
    assert policy_cycle.system.warnings["alice"] is replacement
    assert replacement.consecutive_violations == 1


async def test_normalization_does_not_clear_warning_replaced_during_policy_read(policy_cycle, monkeypatch):
    import utils.settings_service as policies

    async with database.get_db() as db:
        await UserCRUD.set_special_limit(db, "alice", 9, fetch_from_panel=False)
    original_record = policy_cycle.warning(count=1)
    replacement = None
    original = policies.read_user_policy

    async def changed(*args, **kwargs):
        nonlocal replacement
        policy = await original(*args, **kwargs)
        if replacement is None:
            replacement = policy_cycle.warning(count=1)
        return policy

    monkeypatch.setattr(policies, "read_user_policy", changed)
    await policy_cycle.run()
    assert policy_cycle.applied == []
    assert policy_cycle.system.warnings["alice"] is replacement
    assert replacement is not original_record


@pytest.mark.parametrize("missing_dependency", [False, True])
async def test_present_broken_topics_gate_cannot_fall_back_to_allowed(
    policy_cycle, topics_admission, monkeypatch, missing_dependency
):
    import importlib

    record = policy_cycle.warning()
    if missing_dependency:
        original = importlib.import_module

        def broken_import(name, *args, **kwargs):
            if name == "utils.monitoring_gate":
                raise ModuleNotFoundError("gate dependency missing", name="gate_dependency")
            return original(name, *args, **kwargs)

        monkeypatch.setattr(importlib, "import_module", broken_import)
        error = ModuleNotFoundError
    else:

        def broken_snapshot():
            raise RuntimeError("gate unavailable")

        monkeypatch.setattr(topics_admission.module, "monitoring_snapshot", broken_snapshot)
        error = RuntimeError
    with pytest.raises(error):
        await policy_cycle.run()
    assert policy_cycle.applied == []
    assert policy_cycle.system.warnings["alice"] is record
    assert record.consecutive_violations == 2
    assert "alice" in ACTIVE_USERS
