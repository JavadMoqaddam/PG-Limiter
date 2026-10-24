"""Original enforcement admission survives punishment journal preparation awaits."""

import asyncio
import copy
import json
import time
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select

from test_config_revisions import settings_db  # noqa: F401
from test_inbound_mode_enforcement import enforcement_cycle, existing_warning  # noqa: F401
from test_per_node_enforcement import node_cycle, observed_user, publish_user, PANEL  # noqa: F401


@pytest.fixture
async def real_node_punishment(node_cycle, operational_topics, completed_logs_evidence, tmp_path, monkeypatch):
    import db.database as database
    import utils.panel_api as panel_api
    import utils.panel_api.users as users
    import utils.punishment_system as punishment
    import utils.punishment_facts as facts
    from utils.read_config import save_config_value, read_config

    system, get_db, messages, config = node_cycle
    assert await save_config_value("punishment_steps", json.dumps([{"type": "revoke", "duration": 0}]))
    assert await save_config_value("group_filter_enabled", "true")
    assert await save_config_value("group_filter_mode", "exclude")
    assert await save_config_value("group_filter_ids", "99")
    config = await read_config()
    monkeypatch.setattr(database, "DB_AVAILABLE", True)
    punishment_system = punishment.PunishmentSystem(str(tmp_path / "violations.json"))
    monkeypatch.setattr(punishment, "_punishment_system", punishment_system)
    monkeypatch.setattr(punishment, "get_punishment_system", lambda: punishment_system)
    monkeypatch.setattr(panel_api, "disable_user_with_punishment", users.disable_user_with_punishment)
    monkeypatch.setattr(users, "check_user_exists", AsyncMock(return_value=True))
    monkeypatch.setattr(users, "_read_disable_provenance", AsyncMock(return_value=None))
    disable = AsyncMock(return_value=True)
    disable.real_status = users.disable_user_by_status
    disable.real_revoke = users.revoke_user_subscription
    disable.real_reset = users.reset_user_uuid
    monkeypatch.setattr(users, "disable_user_by_status", disable)
    monkeypatch.setattr(users, "revoke_user_subscription", AsyncMock(return_value=True))
    monkeypatch.setattr(users, "reset_user_uuid", AsyncMock(return_value=False))
    monkeypatch.setattr(users.dis_users, "disable", AsyncMock(return_value=True))
    original_time = datetime(2026, 10, 23, 23, 59, 59, tzinfo=timezone.utc).timestamp()
    journal = facts.PunishmentFactJournal(str(tmp_path / "owned.json"), clock=lambda: original_time)
    monkeypatch.setattr(facts, "_journal", journal)
    operational_topics._topics = {"general": 1, "daily_report": 9}
    monkeypatch.setenv("TZ", "UTC")
    # The config save invalidates Logs evidence, so rebuild the admitted scope.
    completed_logs_evidence(config)
    from utils.log_observations import LOG_OBSERVATIONS
    from utils.shared_state import note_node_event
    from utils.types import NodeType

    LOG_OBSERVATIONS.register_fleet(
        [NodeType(0, "core", "192.0.2.1", "connected"), NodeType(2, "node", "192.0.2.2", "connected")]
    )
    for node_id in (0, 2):
        LOG_OBSERVATIONS.zones[str(node_id)] = LOG_OBSERVATIONS._zone("UTC")
        note_node_event(node_id)
    LOG_OBSERVATIONS.fleet_floor = LOG_OBSERVATIONS.floor
    LOG_OBSERVATIONS.transport_floor = LOG_OBSERVATIONS.floor
    return system, get_db, messages, config, journal, disable, original_time


@pytest.mark.parametrize("source", ["logs", "api"])
@pytest.mark.parametrize("expired", [False, True])
async def test_real_node_overflow_journal_to_report(
    real_node_punishment, operational_topics, tmp_path, monkeypatch, source, expired
):
    import utils.check_usage as usage
    from db.models import PunishmentFact, ViolationHistory
    from telegram_bot.dispatcher import TelegramDispatcher
    import telegram_bot.dispatcher as dispatcher_module
    from utils.daily_report import DailyReportService
    from db.crud.daily_reports import DailyReportsCRUD

    system, get_db, messages, config, journal, disable, original_time = real_node_punishment
    warning = existing_warning()
    warning.last_scan_time = time.time() - 60
    warning.consecutive_violations = 3 if expired else 2
    if expired:
        warning.monitoring_end_time = 1
    system.warnings["alice"] = warning
    await publish_user(config, observed_user([("5.6.7.8", "Vless", 0), ("5.6.7.8", "Vless", 2)]), source)
    await usage.check_users_usage(PANEL, config_data=config)
    assert disable.call_count == 1
    assert "alice" not in system.warnings
    async with get_db() as db:
        rows = list(await db.scalars(select(PunishmentFact)))
        assert len(rows) == 1
        fact = rows[0]
        assert fact.step_index == 0 and fact.applied_at == original_time
        assert (fact.disable_succeeded, fact.revoke_succeeded, fact.uuid_reset_succeeded) == (True, True, False)
        assert fact.state == "completed"
        assert len(list(await db.scalars(select(ViolationHistory)))) == 1
    monkeypatch.setattr(dispatcher_module, "PENDING_NOTIFICATIONS_FILE", str(tmp_path / "pending.json"))
    dispatcher = TelegramDispatcher()
    dispatcher.set_bot(MagicMock(send_message=AsyncMock(return_value=MagicMock(message_id=903))))
    service = DailyReportService(dispatcher, journal)
    worker = asyncio.create_task(dispatcher.start_worker())
    try:
        assert await service.report_previous_day(datetime(2026, 10, 24, tzinfo=timezone.utc))
        await asyncio.wait_for(dispatcher.queue.join(), 2)
        run = await DailyReportsCRUD.get("2026-10-23")
        assert run["state"] == "delivered" and run["message_id"] == 903
        assert "Step 1: 1 users" in run["payload"] and "Total: 1 users" in run["payload"]
        assert "alice" not in run["payload"] and "5.6.7.8" not in run["payload"]
        assert not await service.report_previous_day(datetime(2026, 10, 24, tzinfo=timezone.utc))
        assert dispatcher._bot.send_message.call_count == 1
    finally:
        await dispatcher.stop()
        await worker


@pytest.mark.parametrize("source", ["logs", "api"])
@pytest.mark.parametrize("expired", [False, True])
@pytest.mark.parametrize("change", ["pause", "off_on", "source", "count_mode", "factual_group"])
async def test_prepared_fact_await_respects_completed_invalidation(
    real_node_punishment, operational_topics, monkeypatch, source, expired, change
):
    import aiosqlite
    import utils.check_usage as usage
    import utils.panel_api.users as users
    from db.models import PunishmentFact, ViolationHistory

    system, get_db, messages, config, journal, disable, _time = real_node_punishment
    warning = existing_warning()
    warning.last_scan_time = time.time() - 60
    warning.consecutive_violations = 3 if expired else 2
    if expired:
        warning.monitoring_end_time = 1
    system.warnings["alice"] = warning
    await publish_user(config, observed_user([("5.6.7.8", "Vless", 0), ("5.6.7.8", "Vless", 2)]), source)
    outcomes = []

    async def capture_punishment(panel, user):
        result = await users.disable_user_with_punishment(panel, user)
        outcomes.append(result)
        return result

    monkeypatch.setattr("utils.panel_api.disable_user_with_punishment", capture_punishment)
    from utils.isp_detector import ISPDetector

    isp = AsyncMock(return_value={})
    monkeypatch.setattr(ISPDetector, "get_multiple_isp_info", isp)
    original_commit = aiosqlite.Connection.commit
    entered, release = asyncio.Event(), asyncio.Event()
    intercepted = False

    async def commit_barrier(connection):
        nonlocal intercepted
        cursor = await connection.execute("SELECT state FROM punishment_facts LIMIT 1")
        row = await cursor.fetchone()
        await cursor.close()
        if row == ("prepared",) and not intercepted:
            intercepted = True
            # Commit physically before suspending acknowledgement, permitting a
            # separate canonical writer to complete during the new prepare await.
            await original_commit(connection)
            entered.set()
            await release.wait()
            return
        await original_commit(connection)

    monkeypatch.setattr(aiosqlite.Connection, "commit", commit_barrier)
    caller = asyncio.create_task(usage.check_users_usage(PANEL, config_data=config))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        disable.assert_not_called()
        users.revoke_user_subscription.assert_not_called()
        saved_warning = copy.deepcopy(
            {key: value for key, value in vars(warning).items() if key != "active_monitoring_task"}
        )
        saved_history = copy.deepcopy(system.warning_history)
        isp.reset_mock()
        if change == "pause":
            await asyncio.wait_for(operational_topics.set_enabled(False), 2)
        elif change == "off_on":
            await asyncio.wait_for(operational_topics.set_enabled(False), 2)
            await asyncio.wait_for(operational_topics.set_enabled(True), 2)
        elif change in {"source", "count_mode"}:
            from utils.read_config import save_config_value, read_config

            key, value = (
                ("ip_source", "api" if source == "logs" else "logs")
                if change == "source"
                else ("device_count_mode", "ip")
            )
            assert await asyncio.wait_for(save_config_value(key, value), 2)
            await read_config()
        else:
            from db.crud.users import UserCRUD

            async with get_db() as db:
                await UserCRUD.create_or_update(db, "alice", group_ids=[99])
            from utils.settings_service import read_user_policy

            changed_policy = await read_user_policy("alice")
            assert changed_policy.known and not changed_policy.eligible and changed_policy.reason == "group_filtered"
        disable.assert_not_called()
        users.revoke_user_subscription.assert_not_called()
        release.set()
        await asyncio.wait_for(caller, 3)

        async with get_db() as db:
            rows = list(await db.scalars(select(PunishmentFact)))
        disable.assert_not_called()
        users.revoke_user_subscription.assert_not_called()
        assert system.warnings.get("alice") is warning
        assert {key: value for key, value in vars(warning).items() if key != "active_monitoring_task"} == saved_warning
        assert system.warning_history == saved_history
        users.reset_user_uuid.assert_not_called()
        users.dis_users.disable.assert_not_called()
        isp.assert_not_called()
        assert len(outcomes) == 1 and outcomes[0]["action"] == "skipped" and outcomes[0]["protected"]
        assert len(rows) == 1 and rows[0].state == "completed"
        assert (rows[0].disable_succeeded, rows[0].revoke_succeeded, rows[0].uuid_reset_succeeded) == (
            False,
            False,
            False,
        )
        assert rows[0].applied_at is None
        async with get_db() as db:
            assert not list(await db.scalars(select(ViolationHistory)))
        assert await journal.ready_before(datetime(2026, 10, 24, tzinfo=timezone.utc).timestamp())
    finally:
        release.set()
        await asyncio.wait_for(caller, 3)


async def prepare_overflow(real_node_punishment, source, expired):
    system, _get_db, _messages, config, _journal, _disable, _time = real_node_punishment
    warning = existing_warning()
    warning.last_scan_time = time.time() - 60
    warning.consecutive_violations = 3 if expired else 2
    if expired:
        warning.monitoring_end_time = 1
    system.warnings["alice"] = warning
    await publish_user(config, observed_user([("5.6.7.8", "Vless", 0), ("5.6.7.8", "Vless", 2)]), source)
    return warning


def suspend_return(monkeypatch, target, name):
    """Suspend acknowledgement after the actual preparation operation completes."""
    original = getattr(target, name)
    entered, release = asyncio.Event(), asyncio.Event()

    async def barrier(*args, **kwargs):
        result = await original(*args, **kwargs)
        entered.set()
        await release.wait()
        return result

    monkeypatch.setattr(target, name, barrier)
    return entered, release


@pytest.mark.parametrize("source", ["logs", "api"])
@pytest.mark.parametrize("expired", [False, True])
@pytest.mark.parametrize("change", ["warning_identity", "evidence_generation"])
async def test_prepared_fact_preserves_replaced_warning_or_retired_evidence(
    real_node_punishment, monkeypatch, source, expired, change
):
    import utils.check_usage as usage
    import utils.panel_api.users as users
    from db.models import PunishmentFact, ViolationHistory

    system, get_db, _messages, config, journal, disable, _time = real_node_punishment
    warning = await prepare_overflow(real_node_punishment, source, expired)
    original_store = journal.store
    entered, release = asyncio.Event(), asyncio.Event()

    async def prepared_barrier(snapshot):
        await original_store(snapshot)
        if snapshot["state"] == "prepared":
            entered.set()
            await release.wait()

    monkeypatch.setattr(journal, "store", prepared_barrier)
    caller = asyncio.create_task(usage.check_users_usage(PANEL, config_data=config))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        if change == "warning_identity":
            warning = existing_warning()
            system.warnings["alice"] = warning
        elif source == "api":
            from utils.shared_state import begin_api_collection

            begin_api_collection()
        else:
            from utils.log_observations import LOG_OBSERVATIONS

            LOG_OBSERVATIONS.invalidate()
        release.set()
        await asyncio.wait_for(caller, 3)
        disable.assert_not_called()
        users.revoke_user_subscription.assert_not_called()
        assert system.warnings.get("alice") is warning
        async with get_db() as db:
            facts = list(await db.scalars(select(PunishmentFact)))
            assert len(facts) == 1 and facts[0].state == "completed" and facts[0].applied_at is None
            assert not list(await db.scalars(select(ViolationHistory)))
    finally:
        release.set()
        await asyncio.wait_for(caller, 3)


@pytest.mark.parametrize("expired", [False, True])
@pytest.mark.parametrize("step", ["disable", "plain"])
@pytest.mark.parametrize("boundary", ["read_provenance", "prepare_provenance", "group_details", "token", "client"])
@pytest.mark.parametrize("change", ["pause", "factual_group"])
async def test_late_plain_or_timed_disable_preparation_preserves_warning(
    real_node_punishment, operational_topics, completed_logs_evidence, monkeypatch, expired, step, boundary, change
):
    import utils.check_usage as usage
    import utils.panel_api.users as users
    import utils.panel_api.request_helper as requests
    from db.models import PunishmentFact, ViolationHistory
    from db.crud.users import UserCRUD
    from utils.isp_detector import ISPDetector
    from utils.read_config import save_config_value, read_config

    system, get_db, _messages, _config, journal, disable, _time = real_node_punishment
    assert await save_config_value("punishment_steps", json.dumps([{"type": "disable", "duration": 15}]))
    assert await save_config_value("punishment_enabled", "false" if step == "plain" else "true")
    if boundary == "group_details":
        assert await save_config_value("disable_method", "group")
        assert await save_config_value("disabled_group_id", "7")
    config = await read_config()
    completed_logs_evidence(config)
    from utils.log_observations import LOG_OBSERVATIONS
    from utils.types import NodeType

    LOG_OBSERVATIONS.register_fleet(
        [NodeType(0, "core", "192.0.2.1", "connected"), NodeType(2, "node", "192.0.2.2", "connected")]
    )
    LOG_OBSERVATIONS.fleet_floor = LOG_OBSERVATIONS.floor
    await prepare_overflow((system, get_db, [], config, journal, disable, _time), "logs", expired)
    monkeypatch.setattr(users, "disable_user_by_status", disable.real_status)
    monkeypatch.setattr(users, "get_user_details", AsyncMock(return_value={"group_ids": []}))
    client = MagicMock(put=AsyncMock(return_value=MagicMock(status_code=200)))
    monkeypatch.setattr(requests, "get_panel_client", AsyncMock(return_value=client))
    monkeypatch.setattr(requests, "_get_token_for_request", AsyncMock(return_value="token"))
    monkeypatch.setattr(requests, "circuit_breaker", requests.PanelCircuitBreaker())
    targets = {
        "read_provenance": (users, "_read_disable_provenance"),
        "prepare_provenance": (users, "_prepare_disable_provenance"),
        "group_details": (users, "get_user_details"),
        "token": (requests, "_get_token_for_request"),
        "client": (requests, "get_panel_client"),
    }
    entered, release = suspend_return(monkeypatch, *targets[boundary])
    outcomes = []

    async def capture_punishment(panel, user):
        outcome = await users.disable_user_with_punishment(panel, user)
        outcomes.append(outcome)
        return outcome

    monkeypatch.setattr("utils.panel_api.disable_user_with_punishment", capture_punishment)
    isp = AsyncMock(return_value={})
    monkeypatch.setattr(ISPDetector, "get_multiple_isp_info", isp)
    caller = asyncio.create_task(usage.check_users_usage(PANEL, config_data=config))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        warning = system.warnings["alice"]
        saved_warning = copy.deepcopy(
            {key: value for key, value in vars(warning).items() if key != "active_monitoring_task"}
        )
        saved_history = copy.deepcopy(system.warning_history)
        isp.reset_mock()
        if change == "pause":
            await operational_topics.set_enabled(False)
        else:
            async with get_db() as db:
                await UserCRUD.create_or_update(db, "alice", group_ids=[99])
        release.set()
        await asyncio.wait_for(caller, 3)
        client.put.assert_not_called()
        users.dis_users.disable.assert_not_called()
        isp.assert_not_called()
        assert system.warnings.get("alice") is warning
        assert {key: value for key, value in vars(warning).items() if key != "active_monitoring_task"} == saved_warning
        assert system.warning_history == saved_history
        assert len(outcomes) == 1 and outcomes[0]["action"] == "skipped" and outcomes[0]["protected"]
        async with get_db() as db:
            facts = list(await db.scalars(select(PunishmentFact)))
            assert len(facts) == 1 and facts[0].state == "completed" and facts[0].applied_at is None
            assert not facts[0].disable_succeeded
            assert not list(await db.scalars(select(ViolationHistory)))
        assert await journal.ready_before(datetime(2026, 10, 24, tzinfo=timezone.utc).timestamp())
    finally:
        release.set()
        await asyncio.wait_for(caller, 3)


@pytest.mark.parametrize("source", ["logs", "api"])
@pytest.mark.parametrize("expired", [False, True])
@pytest.mark.parametrize("change", ["pause", "off_on", "factual_group"])
async def test_started_revoke_retains_incident_ownership(
    real_node_punishment, operational_topics, monkeypatch, source, expired, change
):
    import utils.check_usage as usage
    import utils.panel_api.users as users
    import utils.panel_api.request_helper as requests
    from db.models import PunishmentFact, ViolationHistory
    from db.crud.users import UserCRUD

    system, get_db, _messages, config, journal, disable, original_time = real_node_punishment
    await prepare_overflow(real_node_punishment, source, expired)
    monkeypatch.setattr(users, "revoke_user_subscription", disable.real_revoke)
    entered, release = asyncio.Event(), asyncio.Event()

    async def panel_effect(*args, **kwargs):
        entered.set()
        await release.wait()
        return MagicMock(status_code=200)

    client = MagicMock(post=AsyncMock(side_effect=panel_effect))
    monkeypatch.setattr(requests, "get_panel_client", AsyncMock(return_value=client))
    monkeypatch.setattr(requests, "_get_token_for_request", AsyncMock(return_value="token"))
    monkeypatch.setattr(requests, "circuit_breaker", requests.PanelCircuitBreaker())
    caller = asyncio.create_task(usage.check_users_usage(PANEL, config_data=config))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        if change in {"pause", "off_on"}:
            await operational_topics.set_enabled(False)
            if change == "off_on":
                await operational_topics.set_enabled(True)
        else:
            async with get_db() as db:
                await UserCRUD.create_or_update(db, "alice", group_ids=[99])
        release.set()
        await asyncio.wait_for(caller, 3)
        client.post.assert_awaited_once()
        users.reset_user_uuid.assert_awaited_once()
        disable.assert_awaited_once()
        assert "alice" not in system.warnings
        async with get_db() as db:
            facts = list(await db.scalars(select(PunishmentFact)))
            assert len(facts) == 1 and facts[0].state == "completed" and facts[0].applied_at == original_time
            assert (facts[0].disable_succeeded, facts[0].revoke_succeeded, facts[0].uuid_reset_succeeded) == (
                True,
                True,
                False,
            )
            assert len(list(await db.scalars(select(ViolationHistory)))) == 1
        assert await journal.ready_before(datetime(2026, 10, 24, tzinfo=timezone.utc).timestamp())
    finally:
        release.set()
        await asyncio.wait_for(caller, 3)


@pytest.mark.parametrize("source", ["logs", "api"])
@pytest.mark.parametrize("expired", [False, True])
async def test_reporting_storage_failure_does_not_prevent_current_punishment(
    real_node_punishment, monkeypatch, source, expired
):
    import utils.check_usage as usage
    import utils.panel_api.users as users
    import utils.punishment_facts as facts
    from db.models import PunishmentFact, ViolationHistory

    system, get_db, _messages, config, journal, disable, original_time = real_node_punishment
    await prepare_overflow(real_node_punishment, source, expired)
    original_store = journal._store_sql
    monkeypatch.setattr(journal, "_store_sql", AsyncMock(side_effect=RuntimeError("fact SQL unavailable")))
    monkeypatch.setattr(facts, "atomic_write_json", MagicMock(side_effect=OSError("fact disk unavailable")))
    await usage.check_users_usage(PANEL, config_data=config)
    disable.assert_awaited_once()
    users.revoke_user_subscription.assert_awaited_once()
    assert "alice" not in system.warnings
    assert len(journal.pending) == 1
    snapshot = next(iter(journal.pending.values()))
    assert snapshot["state"] == "completed" and snapshot["applied_at"] == original_time
    async with get_db() as db:
        assert len(list(await db.scalars(select(ViolationHistory)))) == 1
    monkeypatch.setattr(journal, "_store_sql", original_store)
    assert await journal.ready_before(datetime(2026, 10, 24, tzinfo=timezone.utc).timestamp())
    async with get_db() as db:
        fact = await db.scalar(select(PunishmentFact))
        assert fact.state == "completed" and fact.disable_succeeded


async def test_direct_management_still_applies_when_monitoring_is_paused(real_node_punishment, operational_topics):
    import utils.panel_api.users as users
    from db.crud.users import UserCRUD
    from utils.types import UserType

    _system, get_db, _messages, _config, _journal, disable, _time = real_node_punishment
    await operational_topics.set_enabled(False)
    async with get_db() as db:
        await UserCRUD.create_or_update(db, "alice", group_ids=[99])
    outcome = await users.disable_user_with_punishment(PANEL, UserType(name="alice", ip=[]))
    assert outcome["action"] == "revoked"
    disable.assert_awaited_once()
