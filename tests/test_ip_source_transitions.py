"""Collection failures and source commits preserve evidence ownership and warnings."""

import asyncio
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


@pytest.fixture
async def source_runtime(monkeypatch, tmp_path, operational_topics):
    import utils.ip_source_api as api
    import utils.read_config as config
    import utils.shared_state as state
    import utils.panel_api.online_ips as online
    import utils.panel_api.request_helper as requests
    import utils.panel_api.nodes as nodes
    import telegram_bot.send_message as messages
    from db.models import Base
    from utils.types import PanelType

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'source.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    @asynccontextmanager
    async def database():
        async with sessions() as session:
            async with session.begin():
                yield session

    monkeypatch.setattr(config, "get_db", database)
    monkeypatch.setattr(config, "DB_AVAILABLE", True)
    monkeypatch.setattr(config, "_config_cache", None)
    monkeypatch.setattr(config, "_config_read_lock", asyncio.Lock())
    monkeypatch.setattr(config, "_config_write_lock", asyncio.Lock(), raising=False)
    monkeypatch.setattr(api, "_collection_lock", asyncio.Lock(), raising=False)
    monkeypatch.setattr(api, "_consecutive_dead_cycles", 0)
    monkeypatch.setattr(api, "_failure_revision", None, raising=False)
    monkeypatch.setattr(api, "_consecutive_coverage_skips", 0)
    monkeypatch.setattr(api, "_coverage_alert_sent", False)
    monkeypatch.setattr(api, "_forbidden_alert_sent", False)
    monkeypatch.setattr(state, "ACTIVE_USERS_LOCK", asyncio.Lock())
    state.begin_api_collection()
    state.ACTIVE_USERS.clear()
    async with database() as session:
        for key, value in {
            "ip_source": "api",
            "api_ip_freshness": "60",
            "api_ip_auto_fallback": "true",
            "api_ip_min_coverage": "0.5",
            "country_code": "",
        }.items():
            await config.ConfigCRUD.set(session, key, value)
    await config.invalidate_config_cache()
    runtime = SimpleNamespace(
        api=api, config=config, state=state, mode="failure", node_status=None, notifications=[], requests=0
    )
    monkeypatch.setattr(requests, "is_panel_available", lambda: runtime.mode != "unavailable")
    monkeypatch.setattr(online, "resolve_panel_token", AsyncMock(return_value="test-token"))

    async def get_nodes(_panel):
        if runtime.mode == "node_failure":
            return None
        fleet = [SimpleNamespace(node_id=1, node_name="node", node_ip="192.0.2.1", status="connected")]
        if runtime.node_status is not None:
            fleet.append(SimpleNamespace(node_id=2, node_name="other", node_ip="192.0.2.2", status=runtime.node_status))
        return fleet

    async def panel_get(_panel, path, **kwargs):
        assert path == "/api/users"
        if runtime.mode == "candidate_failure":
            return None
        users = [] if runtime.mode == "no_candidates" else [{"id": i, "username": f"u{i}"} for i in range(1, 5)]
        return SimpleNamespace(json=lambda: {"users": users, "total": len(users)})

    async def panel_request(_panel, method, path, token, **kwargs):
        assert method == "GET" and token == "test-token"
        runtime.requests += 1
        user_id = int(path.split("/")[-2])
        if runtime.mode == "cancelled":
            raise asyncio.CancelledError
        if runtime.mode == "failure" or (runtime.mode == "partial" and user_id == 4):
            return None, "timeout"
        if runtime.mode == "route_not_found":
            return SimpleNamespace(status_code=404, json=lambda: {"detail": "Not Found"}), None
        if runtime.mode == "not_found":
            return SimpleNamespace(status_code=404, json=lambda: {"detail": "User not found"}), None
        body = {"nodes": {"1": {"ips": {"5.6.7.8": int(time.time())}}}}
        if runtime.mode == "valid_empty":
            body["nodes"]["1"]["ips"] = {}
        elif runtime.mode == "all_expired":
            body["nodes"]["1"]["ips"] = {"5.6.7.8": int(time.time()) - 1000}
        elif runtime.mode == "invalid_response":
            body = {}
        elif runtime.mode == "missing_node":
            body = {"nodes": {"1": None}}
        if runtime.node_status == "connected" and "nodes" in body:
            body["nodes"]["2"] = {"ips": {}}
        return SimpleNamespace(status_code=200, json=lambda: body), None

    async def notify(message, *args, **kwargs):
        runtime.notifications.append(message)

    monkeypatch.setattr(nodes, "get_nodes", get_nodes)
    monkeypatch.setattr(requests, "panel_get", panel_get)
    monkeypatch.setattr(requests, "panel_request", panel_request)
    monkeypatch.setattr(messages, "send_logs", notify)

    async def collect(mode):
        runtime.mode = mode
        return await api.collect_active_users_from_api(
            PanelType("admin", "password", "panel.local"), await config.read_config()
        )

    runtime.collect = collect
    yield runtime
    state.ACTIVE_USERS.clear()
    state.begin_api_collection()
    await config.invalidate_config_cache()
    await engine.dispose()


@pytest.mark.parametrize(
    "failure",
    [
        "failure",
        "unavailable",
        "candidate_failure",
        "node_failure",
        "invalid_response",
        "missing_node",
        "partial",
        "route_not_found",
    ],
)
async def test_second_unsuccessful_cycle_commits_logs(source_runtime, failure):
    r = source_runtime
    first = await r.collect(failure)
    assert first is (failure == "partial")
    assert (await r.config.read_config())["ip_source"] == "api"
    assert r.notifications == []
    assert await r.collect(failure) is False
    assert (await r.config.read_config())["ip_source"] == "logs"
    assert len(r.notifications) == 1
    assert "reverted to Logs" in r.notifications[0]
    assert r.state.ACTIVE_USERS == {}
    assert await r.state.pop_api_users_snapshot() is None
    assert await r.state.pop_active_users_snapshot() == {}


@pytest.mark.parametrize("success", ["success", "valid_empty", "all_expired", "no_candidates", "not_found"])
async def test_valid_success_breaks_failure_streak(source_runtime, success):
    r = source_runtime
    await r.collect("failure")
    assert await r.collect(success) is True
    await r.collect("failure")
    assert (await r.config.read_config())["ip_source"] == "api"
    assert r.notifications == []
    await r.collect("failure")
    assert (await r.config.read_config())["ip_source"] == "logs"


@pytest.mark.parametrize("status", ["error", "connecting"])
@pytest.mark.parametrize("sample", ["valid_empty", "success", "no_candidates"])
async def test_two_completed_unavailable_fleet_cycles_commit_fallback(source_runtime, status, sample):
    r = source_runtime
    r.node_status = status
    assert await r.collect(sample) is False
    assert r.state.ACTIVE_USERS == {}
    assert await r.state.pop_api_users_snapshot() is None
    assert (await r.config.read_config())["ip_source"] == "api"
    assert r.notifications == []
    assert await r.collect(sample) is False
    assert (await r.config.read_config())["ip_source"] == "logs"
    assert len(r.notifications) == 1
    assert "reverted to Logs" in r.notifications[0]
    assert r.state.ACTIVE_USERS == {}
    assert await r.state.pop_api_users_snapshot() is None


@pytest.mark.parametrize("status", ["error", "connecting"])
@pytest.mark.parametrize("success", ["valid_empty", "no_candidates", "all_expired", "not_found"])
async def test_healthy_fleet_success_resets_unavailable_cycle_streak(source_runtime, status, success):
    r = source_runtime
    r.node_status = status
    assert await r.collect("valid_empty") is False
    r.node_status = "connected"
    assert await r.collect(success) is True
    assert r.api.get_last_cycle_stats()["collection_outcome"] == "success"
    r.node_status = status
    assert await r.collect("valid_empty") is False
    assert (await r.config.read_config())["ip_source"] == "api"
    assert r.notifications == []
    assert await r.collect("valid_empty") is False
    assert (await r.config.read_config())["ip_source"] == "logs"
    assert len(r.notifications) == 1


async def test_four_failed_http_requests_are_one_failed_cycle(source_runtime):
    r = source_runtime
    await r.collect("failure")
    assert r.requests == 4
    assert (await r.config.read_config())["ip_source"] == "api"
    await r.collect("failure")
    assert r.requests == 8
    assert (await r.config.read_config())["ip_source"] == "logs"


async def test_disabled_fallback_preserves_api_and_known_partial_users(source_runtime):
    r = source_runtime
    await r.config.save_config_value("api_ip_auto_fallback", "false")
    for _ in range(4):
        assert await r.collect("partial") is True
        assert set(r.state.ACTIVE_USERS) == {"u1", "u2", "u3"}
    assert (await r.config.read_config())["ip_source"] == "api"
    assert r.notifications == []


async def test_cancelled_cycle_is_neutral(source_runtime):
    r = source_runtime
    await r.collect("failure")
    with pytest.raises(asyncio.CancelledError):
        await r.collect("cancelled")
    assert (await r.config.read_config())["ip_source"] == "api"
    await r.collect("failure")
    assert (await r.config.read_config())["ip_source"] == "logs"


async def test_degraded_configuration_is_neutral(source_runtime):
    from utils.types import PanelType

    r = source_runtime
    await r.collect("failure")
    cfg = await r.config.read_config()
    cfg["config_degraded"] = True
    previous_requests = r.requests
    assert await r.api.collect_active_users_from_api(PanelType("a", "p", "panel.local"), cfg) is False
    assert r.requests == previous_requests
    assert (await r.config.read_config())["ip_source"] == "api"
    await r.collect("failure")
    assert (await r.config.read_config())["ip_source"] == "logs"


async def test_source_round_trip_starts_a_new_failure_streak(source_runtime):
    r = source_runtime
    await r.collect("failure")
    assert await r.config.save_config_value("ip_source", "logs") is True
    assert await r.config.save_config_value("ip_source", "api") is True
    await r.collect("failure")
    assert (await r.config.read_config())["ip_source"] == "api"
    await r.collect("failure")
    assert (await r.config.read_config())["ip_source"] == "logs"


async def test_failed_fallback_write_retries_after_its_own_invalidation(source_runtime, monkeypatch):
    r = source_runtime
    original = r.config.ConfigCRUD.set
    attempts = 0

    async def write(session, key, value, **kwargs):
        nonlocal attempts
        if key == "ip_source" and value == "logs":
            attempts += 1
            if attempts == 1:
                raise RuntimeError("disk unavailable")
        return await original(session, key, value, **kwargs)

    monkeypatch.setattr(r.config.ConfigCRUD, "set", write)
    await r.collect("partial")
    assert await r.collect("partial") is True
    assert attempts == 1
    assert (await r.config.read_config())["ip_source"] == "api"
    assert set(r.state.ACTIVE_USERS) == {"u1", "u2", "u3"}
    assert r.notifications == []
    assert await r.collect("partial") is False
    assert attempts == 2
    assert (await r.config.read_config())["ip_source"] == "logs"


async def test_notification_failure_cannot_undo_or_repeat_committed_switch(source_runtime, monkeypatch):
    import telegram_bot.send_message as messages

    r = source_runtime
    notifications = []

    async def fail(message):
        notifications.append(message)
        assert (await r.config.read_config())["ip_source"] == "logs"
        assert r.state.ACTIVE_USERS == {}
        raise RuntimeError("Telegram unavailable")

    monkeypatch.setattr(messages, "send_logs", fail)
    await r.collect("partial")
    assert await r.collect("partial") is False
    assert (await r.config.read_config())["ip_source"] == "logs"
    assert await r.collect("failure") is False
    assert len(notifications) == 1


async def test_manual_source_save_discards_both_api_views(source_runtime):
    r = source_runtime
    assert await r.collect("success") is True
    assert r.state.ACTIVE_USERS
    assert await r.config.save_config_value("ip_source", "logs") is True
    assert r.state.ACTIVE_USERS == {}
    assert await r.state.pop_api_users_snapshot() is None
    assert await r.state.pop_active_users_snapshot() == {}


async def test_guarded_save_rejects_superseded_revision(source_runtime):
    r = source_runtime
    revision = r.config.get_config_revision()
    await r.config.save_config_value("api_ip_freshness", "90")
    result = await r.config.save_config_value("ip_source", "logs", expected_revision=revision)
    assert result is None
    assert (await r.config.read_config())["ip_source"] == "api"


async def test_stats_mutation_cannot_change_cycle_outcome(source_runtime, monkeypatch):
    r = source_runtime
    original = r.api._handle_coverage_skip

    async def skip(reason):
        await original(reason)
        r.api.LAST_CYCLE_STATS["collection_outcome"] = "success"

    monkeypatch.setattr(r.api, "_handle_coverage_skip", skip)
    await r.collect("missing_node")
    await r.collect("missing_node")
    assert (await r.config.read_config())["ip_source"] == "logs"


async def test_settings_change_during_collection_is_not_an_outage(source_runtime, monkeypatch):
    r = source_runtime
    await r.collect("failure")
    original = r.api._fetch_all_online_ips

    async def fetch(*args, **kwargs):
        result = await original(*args, **kwargs)
        await r.config.save_config_value("api_ip_freshness", "90")
        return result

    monkeypatch.setattr(r.api, "_fetch_all_online_ips", fetch)
    assert await r.collect("failure") is False
    assert (await r.config.read_config())["ip_source"] == "api"
    monkeypatch.setattr(r.api, "_fetch_all_online_ips", original)
    await r.collect("failure")
    assert (await r.config.read_config())["ip_source"] == "api"
    await r.collect("failure")
    assert (await r.config.read_config())["ip_source"] == "logs"


async def test_manual_choice_before_guarded_write_wins(source_runtime, monkeypatch):
    r = source_runtime
    original = r.config.save_config_value

    async def save(key, value, **kwargs):
        if kwargs.get("expected_revision") is not None:
            assert await original("ip_source", "logs") is True
            assert await original("ip_source", "api") is True
        return await original(key, value, **kwargs)

    monkeypatch.setattr(r.config, "save_config_value", save)
    await r.collect("failure")
    await r.collect("failure")
    assert (await r.config.read_config())["ip_source"] == "api"
    assert r.notifications == []
    monkeypatch.setattr(r.config, "save_config_value", original)
    await r.collect("failure")
    assert (await r.config.read_config())["ip_source"] == "api"
    await r.collect("failure")
    assert (await r.config.read_config())["ip_source"] == "logs"


async def test_new_manual_source_waits_for_committed_cleanup(source_runtime, monkeypatch):
    r = source_runtime
    await r.collect("success")
    entered = asyncio.Event()
    proceed = asyncio.Event()
    original = r.config.ConfigCRUD.set
    writes = []

    async def set_value(session, key, value, **kwargs):
        writes.append(value)
        if value == "logs":
            entered.set()
            await proceed.wait()
        return await original(session, key, value, **kwargs)

    monkeypatch.setattr(r.config.ConfigCRUD, "set", set_value)
    fallback = asyncio.create_task(r.config.save_config_value("ip_source", "logs"))
    await entered.wait()
    manual = asyncio.create_task(r.config.save_config_value("ip_source", "api"))
    await asyncio.sleep(0)
    # A queued manual write may not overtake an in-flight transition.
    pending_writes = list(writes)
    proceed.set()
    assert await fallback is True
    assert await manual is True
    assert pending_writes == ["logs"]
    assert (await r.config.read_config())["ip_source"] == "api"
    assert await r.collect("success") is True
    assert set(r.state.ACTIVE_USERS) == {"u1", "u2", "u3", "u4"}


async def test_config_deletion_serializes_with_source_writes(source_runtime, monkeypatch):
    r = source_runtime
    entered = asyncio.Event()
    proceed = asyncio.Event()
    original = r.config.ConfigCRUD.set
    deleted = []
    original_delete = r.config.ConfigCRUD.delete

    async def write(session, key, value, **kwargs):
        entered.set()
        await proceed.wait()
        return await original(session, key, value, **kwargs)

    async def delete(session, key):
        deleted.append(key)
        return await original_delete(session, key)

    monkeypatch.setattr(r.config.ConfigCRUD, "set", write)
    monkeypatch.setattr(r.config.ConfigCRUD, "delete", delete)
    saving = asyncio.create_task(r.config.save_config_value("ip_source", "logs"))
    await entered.wait()
    deleting = asyncio.create_task(r.config.delete_config_value("ip_source"))
    await asyncio.sleep(0)
    pending_deletes = list(deleted)
    proceed.set()
    assert await saving is True
    assert await deleting is True
    assert pending_deletes == []
    assert await r.config.get_config_value_from_db("ip_source") is None


async def test_committed_source_cleanup_finishes_despite_caller_cancellation(source_runtime):
    r = source_runtime
    assert await r.collect("success") is True
    async with r.state.ACTIVE_USERS_LOCK:
        saving = asyncio.create_task(r.config.save_config_value("ip_source", "logs"))
        # The real commit has completed before cancellation, while cleanup waits.
        for _ in range(200):
            if await r.config.get_config_value_from_db("ip_source") == "logs":
                break
            await asyncio.sleep(0)
        assert await r.config.get_config_value_from_db("ip_source") == "logs"
        saving.cancel()
        await asyncio.sleep(0)
        assert not saving.done()
    with pytest.raises(asyncio.CancelledError):
        await saving
    assert r.state.ACTIVE_USERS == {}
    assert await r.state.pop_api_users_snapshot() is None


async def test_concurrent_collectors_are_accounted_in_order(source_runtime, monkeypatch):
    r = source_runtime
    entered = asyncio.Event()
    proceed = asyncio.Event()
    original = r.api._fetch_all_online_ips
    calls = 0

    async def fetch(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            await proceed.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(r.api, "_fetch_all_online_ips", fetch)
    first = asyncio.create_task(r.collect("failure"))
    await entered.wait()
    second = asyncio.create_task(r.collect("failure"))
    await asyncio.sleep(0)
    pending_calls = calls
    proceed.set()
    assert await first is False
    assert await second is False
    assert pending_calls == 1
    assert (await r.config.read_config())["ip_source"] == "logs"


async def test_cancelled_ip_validation_is_neutral(source_runtime, monkeypatch):
    import utils.ip_facts as facts

    r = source_runtime
    await r.collect("failure")
    original = facts.is_ip_accepted

    async def cancelled(*args, **kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr(facts, "is_ip_accepted", cancelled)
    with pytest.raises(asyncio.CancelledError):
        await r.collect("success")
    monkeypatch.setattr(facts, "is_ip_accepted", original)
    await r.collect("failure")
    assert (await r.config.read_config())["ip_source"] == "logs"


@pytest.mark.parametrize("transition", ["manual", "fallback", "delete"])
async def test_source_changes_preserve_warning_history(source_runtime, monkeypatch, transition):
    from copy import deepcopy
    from utils.warning_system import warning_system
    from utils.warning_system.user_warning import UserLimitWarning

    r = source_runtime
    warning = UserLimitWarning("u1", 3, {"5.6.7.8"}, 1.0, 2.0, consecutive_violations=2)
    warning.monitoring_history.append({"ip_count": 3, "time": 1.0})
    monkeypatch.setattr(warning_system, "warnings", {"u1": warning})
    before = deepcopy(warning)
    await r.collect("success")
    if transition == "fallback":
        await r.collect("partial")
        await r.collect("partial")
    elif transition == "delete":
        await r.config.delete_config_value("ip_source")
    else:
        await r.config.save_config_value("ip_source", "logs")
    assert warning_system.warnings == {"u1": before}
    assert r.state.ACTIVE_USERS == {}
    assert await r.state.pop_api_users_snapshot() is None


async def test_guard_checks_revision_after_waiting_for_writer(source_runtime, monkeypatch):
    r = source_runtime
    entered = asyncio.Event()
    proceed = asyncio.Event()
    original = r.config.ConfigCRUD.set

    async def write(session, key, value, **kwargs):
        if key == "api_ip_freshness":
            entered.set()
            await proceed.wait()
        return await original(session, key, value, **kwargs)

    monkeypatch.setattr(r.config.ConfigCRUD, "set", write)
    revision = r.config.get_config_revision()
    manual = asyncio.create_task(r.config.save_config_value("api_ip_freshness", "90"))
    await entered.wait()
    fallback = asyncio.create_task(r.config.save_config_value("ip_source", "logs", expected_revision=revision))
    await asyncio.sleep(0)
    proceed.set()
    assert await manual is True
    assert await fallback is None
    assert (await r.config.read_config())["ip_source"] == "api"


async def test_source_cleanup_holds_writer_lock_until_old_observations_are_gone(source_runtime):
    r = source_runtime
    assert await r.collect("success") is True
    async with r.state.ACTIVE_USERS_LOCK:
        fallback = asyncio.create_task(r.config.save_config_value("ip_source", "logs"))
        for _ in range(200):
            if await r.config.get_config_value_from_db("ip_source") == "logs":
                break
            await asyncio.sleep(0)
        assert await r.config.get_config_value_from_db("ip_source") == "logs"
        manual = asyncio.create_task(r.config.save_config_value("ip_source", "api"))
        await asyncio.sleep(0)
        while_cleanup_waits = await r.config.get_config_value_from_db("ip_source")
    assert await fallback is True
    assert await manual is True
    assert while_cleanup_waits == "logs"
    assert (await r.config.read_config())["ip_source"] == "api"
    assert r.state.ACTIVE_USERS == {}
    assert await r.collect("success") is True
    assert set(r.state.ACTIVE_USERS) == {"u1", "u2", "u3", "u4"}


async def test_manual_setting_after_failed_save_discards_retry_streak(source_runtime, monkeypatch):
    r = source_runtime
    original = r.config.ConfigCRUD.set
    await r.collect("failure")
    monkeypatch.setattr(r.config.ConfigCRUD, "set", AsyncMock(side_effect=RuntimeError("disk unavailable")))
    await r.collect("failure")
    monkeypatch.setattr(r.config.ConfigCRUD, "set", original)
    assert await r.config.save_config_value("api_ip_freshness", "90") is True
    await r.collect("failure")
    assert (await r.config.read_config())["ip_source"] == "api"
    await r.collect("failure")
    assert (await r.config.read_config())["ip_source"] == "logs"
    assert len(r.notifications) == 1


@pytest.fixture
async def live_config_writer(source_runtime, monkeypatch, request):
    """Use live Telegram writers with only their DB and Telegram boundaries isolated."""
    import json

    import db
    import db.database as database
    import telegram_bot.utils as bot_utils
    from telegram_bot.handlers import backup, limits, punishment

    r = source_runtime
    monkeypatch.setattr(db, "get_db", r.config.get_db)
    monkeypatch.setattr(database, "get_db", r.config.get_db)
    # The shared commands only mutate actual mirrored users. Seed the user so
    # these source-evidence tests exercise a committed policy change.
    from db.crud.users import UserCRUD

    async with r.config.get_db() as session:
        await UserCRUD.create_or_update(session, "u1")
    kind = request.param
    if kind == "punishment":
        return lambda: punishment._save_punishment_setting("punishment_enabled", "false")
    if kind == "general_limit":
        return lambda: bot_utils.save_general_limit(7)
    if kind == "special_limit":
        return lambda: bot_utils.handle_special_limit("u1", 7)
    if kind == "except_user":
        return lambda: bot_utils.add_except_user("u1")
    if kind == "remove_except_user":
        await bot_utils.add_except_user("u1")
        return lambda: bot_utils.remove_except_user_from_config("u1")
    if kind == "remove_special_limit":
        await bot_utils.handle_special_limit("u1", 7)
        return lambda: limits.handle_remove_special_limit_callback(
            SimpleNamespace(edit_message_text=AsyncMock()), SimpleNamespace(), "u1"
        )
    data = {"disable_method": "group", "disabled_group_id": 7, "timing": {"check_interval": 120}}
    telegram_file = SimpleNamespace(download_as_bytearray=AsyncMock(return_value=json.dumps(data).encode()))
    update = SimpleNamespace(
        message=SimpleNamespace(
            text=None,
            document=SimpleNamespace(file_name="config.json", get_file=AsyncMock(return_value=telegram_file)),
            reply_html=AsyncMock(),
            reply_text=AsyncMock(),
        )
    )
    assert kind == "migrate_backup"
    return lambda: backup.migrate_backup_handler(update, SimpleNamespace(user_data={}))


@pytest.mark.parametrize("fail_save", [False, True])
@pytest.mark.parametrize("live_config_writer", ["punishment"], indirect=True)
async def test_live_punishment_edit_cannot_overtake_fallback_or_inherit_its_retry(
    source_runtime, live_config_writer, monkeypatch, fail_save
):
    r = source_runtime
    await r.collect("failure")
    entered = asyncio.Event()
    proceed = asyncio.Event()
    original = r.config.ConfigCRUD.set
    manual_writes = []
    paused = False

    async def write(session, key, value, **kwargs):
        nonlocal paused
        if key == "ip_source" and value == "logs" and not paused:
            paused = True
            entered.set()
            await proceed.wait()
            if fail_save:
                raise RuntimeError("controlled persistence failure")
        elif key == "punishment_enabled":
            manual_writes.append(key)
        return await original(session, key, value, **kwargs)

    monkeypatch.setattr(r.config.ConfigCRUD, "set", write)
    fallback = asyncio.create_task(r.collect("failure"))
    await entered.wait()
    manual = asyncio.create_task(live_config_writer())
    await asyncio.sleep(0)
    pending_manual_writes = list(manual_writes)
    proceed.set()
    assert await fallback is False
    assert await manual is True
    assert pending_manual_writes == []
    assert (await r.config.read_config())["punishment"]["enabled"] is False
    if fail_save:
        await r.collect("failure")
        assert (await r.config.read_config())["ip_source"] == "api"
        await r.collect("failure")
    assert (await r.config.read_config())["ip_source"] == "logs"


@pytest.mark.parametrize(
    "live_config_writer",
    [
        "general_limit",
        "special_limit",
        "except_user",
        "remove_except_user",
        "remove_special_limit",
        "migrate_backup",
    ],
    indirect=True,
)
async def test_live_config_writers_wait_for_source_transition(source_runtime, live_config_writer):
    r = source_runtime
    revision = r.config.get_config_revision()
    async with r.config._config_write_lock:
        writing = asyncio.create_task(live_config_writer())
        # The writer must wait before opening/committing its own transaction.
        await asyncio.sleep(0)
        waiting = not writing.done()
        # A DB read yields long enough for an unguarded writer to commit.
        for _ in range(20):
            await r.config.get_config_value_from_db("ip_source")
        same_revision = r.config.get_config_revision() == revision
    await writing
    assert waiting and same_revision
    assert r.config.get_config_revision() > revision


@pytest.mark.parametrize("live_config_writer", ["migrate_backup"], indirect=True)
async def test_backup_config_batch_rolls_back_and_invalidates_on_failure(
    source_runtime, live_config_writer, monkeypatch
):
    r = source_runtime
    revision = r.config.get_config_revision()
    original = r.config.ConfigCRUD.set
    writes = 0

    async def write(session, key, value, **kwargs):
        nonlocal writes
        writes += 1
        if writes == 2:
            raise RuntimeError("cannot persist second setting")
        return await original(session, key, value, **kwargs)

    monkeypatch.setattr(r.config.ConfigCRUD, "set", write)
    await live_config_writer()
    assert writes == 2
    assert await r.config.get_config_value_from_db("disable_method") is None
    assert await r.config.get_config_value_from_db("check_interval") is None
    assert r.config.get_config_revision() > revision
