"""Explicitly paused Topics must not admit source observations or enforcement."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from utils.types import NodeType, PanelType, UserType

PANEL = PanelType("admin", "test", "panel.local")
NODE = NodeType(1, "node", "192.0.2.1", "connected")


@pytest.fixture
def paused_topics(operational_topics):
    operational_topics._enabled = False
    return operational_topics


async def test_paused_startup_does_not_create_sse_task(paused_topics):
    from utils.get_logs import TASKS, create_node_task

    created = []

    class Group:
        def create_task(self, coroutine, **kwargs):
            coroutine.close()
            created.append(kwargs["name"])
            return object()

    before = list(TASKS)
    try:
        await create_node_task(PANEL, Group(), NODE)
        assert created == []
    finally:
        TASKS[:] = before


async def test_paused_parser_does_not_prepare_or_accept_observations(paused_topics, monkeypatch):
    import utils.parse_logs as parser
    from utils.shared_state import ACTIVE_USERS

    async def forbidden(*args):
        pytest.fail("paused parser prepared an observation")

    monkeypatch.setattr(parser, "is_ip_accepted", forbidden)
    await parser.parse_logs(
        "2026/10/24 10:00:00 from 5.6.7.8:123 accepted tcp:example.com:443 [Vless >> DIRECT] email: 1.alice",
        1,
        "node",
        {"ip_source": "logs", "log_timezone": "UTC"},
    )
    assert ACTIVE_USERS == {}


async def test_paused_api_does_not_start_collection(paused_topics, monkeypatch):
    import utils.ip_source_api as api
    import utils.panel_api.request_helper as requests

    async def forbidden(*args):
        pytest.fail("paused collector requested node context")

    monkeypatch.setattr(api, "_resolve_node_context", forbidden)
    monkeypatch.setattr(requests, "is_panel_available", lambda: True)
    assert await api.collect_active_users_from_api(PANEL, {"ip_source": "api"}) is False


async def test_paused_direct_check_keeps_counters_and_skips_snapshot(paused_topics, monkeypatch):
    import utils.check_usage as usage
    from utils.warning_system.user_warning import UserLimitWarning

    warning = UserLimitWarning(
        "alice",
        3,
        {"5.6.7.8", "8.8.8.8", "9.9.9.9"},
        1,
        2,
        consecutive_violations=2,
        trust_score=42.0,
    )
    monkeypatch.setattr(usage.warning_system, "warnings", {"alice": warning})
    monkeypatch.setattr(usage.warning_system, "warning_history", {"alice": [1]})

    async def forbidden(*args):
        pytest.fail("paused checker consumed observations")

    monkeypatch.setattr(usage, "pop_active_users_snapshot", forbidden)
    before = dict(usage.warning_system.warnings)
    await usage.check_users_usage(PANEL, {"ip_source": "logs"})
    assert usage.warning_system.warnings == before
    assert warning.consecutive_violations == 2
    assert warning.trust_score == 42.0
    assert usage.warning_system.warning_history == {"alice": [1]}


async def test_paused_usage_loop_does_not_collect_or_enforce(paused_topics, monkeypatch):
    import utils.check_usage as usage
    import utils.ip_source_api as api

    monkeypatch.setattr(usage, "read_config", AsyncMock(return_value={"ip_source": "api", "check_interval": 60}))

    async def forbidden(*args, **kwargs):
        pytest.fail("paused usage loop collected or enforced")

    async def stop(_delay):
        raise asyncio.CancelledError

    monkeypatch.setattr(api, "collect_active_users_from_api", forbidden)
    monkeypatch.setattr(usage, "check_users_usage", forbidden)
    monkeypatch.setattr(usage.asyncio, "sleep", stop)
    with pytest.raises(asyncio.CancelledError):
        await usage.run_check_users_usage(PANEL)


async def test_pause_retirement_invalidates_samples_without_clearing_warning_history(paused_topics):
    from utils.monitoring_gate import synchronize_monitoring
    from utils.log_observations import LOG_OBSERVATIONS
    from utils.shared_state import ACTIVE_USERS, NODE_LAST_EVENT
    import utils.check_usage as usage

    ACTIVE_USERS["alice"] = UserType(name="alice", ip=["5.6.7.8"])
    NODE_LAST_EVENT[1] = 10
    before = dict(usage.warning_system.warnings)
    generation = LOG_OBSERVATIONS.generation
    admission = await synchronize_monitoring()
    assert admission.allowed is False
    assert ACTIVE_USERS == {}
    assert NODE_LAST_EVENT == {}
    assert LOG_OBSERVATIONS.generation > generation
    assert usage.warning_system.warnings == before


@pytest.mark.parametrize("resume", [False, True])
async def test_disable_during_parser_preparation_discards_observation(operational_topics, monkeypatch, resume):
    import time
    import utils.parse_logs as parser
    from utils.log_observations import LOG_OBSERVATIONS
    from utils.shared_state import ACTIVE_USERS

    config = {"ip_source": "logs", "log_timezone": "UTC"}
    await parser.parse_logs("", 1, "node", config)
    LOG_OBSERVATIONS.floor = time.time() - 60

    async def prepare(*args):
        await operational_topics.set_enabled(False)
        if resume:
            await operational_topics.set_enabled(True)
        return True

    monkeypatch.setattr(parser, "is_ip_accepted", prepare)
    from datetime import datetime, timezone

    stamp = datetime.fromtimestamp(time.time() - 1, timezone.utc).strftime("%Y/%m/%d %H:%M:%S")
    await parser.parse_logs(
        f"{stamp} from 5.6.7.8:123 accepted tcp:example.com:443 [Vless >> DIRECT] email: 1.alice",
        1,
        "node",
        config,
    )
    assert ACTIVE_USERS == {}


@pytest.mark.parametrize("resume", [False, True])
async def test_disable_during_api_node_query_skips_candidate_request(operational_topics, monkeypatch, resume):
    import utils.ip_source_api as api
    import utils.panel_api.online_ips as online
    import utils.panel_api.request_helper as requests
    from utils.shared_state import ACTIVE_USERS

    async def nodes(*args):
        await operational_topics.set_enabled(False)
        if resume:
            await operational_topics.set_enabled(True)
        return {1: "node"}, set(), {1}

    async def forbidden(*args, **kwargs):
        pytest.fail("paused API collector requested user candidates")

    monkeypatch.setattr(api, "_resolve_node_context", nodes)
    monkeypatch.setattr(online, "fetch_online_candidates", forbidden)
    monkeypatch.setattr(requests, "is_panel_available", lambda: True)
    assert await api.collect_active_users_from_api(PANEL, {"ip_source": "api"}) is False
    assert ACTIVE_USERS == {}


async def test_disable_during_usage_preparation_skips_history_and_warnings(
    operational_topics, completed_logs_evidence, monkeypatch
):
    import utils.check_usage as usage
    from utils.shared_state import ActiveUsersSnapshot

    sample = ActiveUsersSnapshot()
    sample.log_evidence = completed_logs_evidence()
    monkeypatch.setattr(usage, "pop_active_users_snapshot", AsyncMock(return_value=sample))

    async def report(**kwargs):
        await operational_topics.set_enabled(False)
        return {}

    async def forbidden(*args, **kwargs):
        pytest.fail("paused usage checker persisted or evaluated observations")

    monkeypatch.setattr(usage, "check_ip_used", report)
    from db.crud import UserCRUD

    monkeypatch.setattr(UserCRUD, "get_all_special_limits", AsyncMock(return_value={}))
    monkeypatch.setattr(usage.ip_history_tracker, "record_many", forbidden)
    monkeypatch.setattr(usage.warning_system, "check_persistent_violations", forbidden)
    monkeypatch.setattr(usage, "_ensure_isp_detector", forbidden)
    await usage.check_users_usage(PANEL, {"ip_source": "logs"})


async def test_paused_sse_entry_does_not_request_token(paused_topics, monkeypatch):
    import utils.get_logs as logs

    async def forbidden(*args, **kwargs):
        pytest.fail("paused SSE stream requested panel token")

    monkeypatch.setattr(logs, "get_token", forbidden)
    await logs.get_nodes_logs(PANEL, NODE)


async def test_monitoring_watcher_cancels_silent_sse_and_discards_source(paused_topics, monkeypatch):
    import utils.get_logs as logs
    from utils.monitoring_gate import watch_monitoring_admission
    from utils.shared_state import ACTIVE_USERS

    closed = asyncio.Event()

    async def stream():
        try:
            await asyncio.Event().wait()
        finally:
            closed.set()

    task = asyncio.create_task(stream(), name="Task-1-node")
    await asyncio.sleep(0)
    monkeypatch.setattr(logs, "TASKS", [task])
    monkeypatch.setattr(logs, "task_node_mapping", {task: NODE})
    ACTIVE_USERS["alice"] = UserType(name="alice", ip=["5.6.7.8"])

    async def stop(_delay):
        raise asyncio.CancelledError

    monkeypatch.setattr(asyncio, "sleep", stop)
    try:
        with pytest.raises(asyncio.CancelledError):
            await watch_monitoring_admission()
        assert closed.is_set()
        assert logs.TASKS == []
        assert ACTIVE_USERS == {}
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_paused_daemon_starts_management_and_timed_reenable_without_nodes(paused_topics, monkeypatch):
    import limiter
    import db.database as database
    import utils.user_sync as sync

    registered = []

    class Group:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def create_task(self, coroutine, **kwargs):
            registered.append(kwargs["name"])
            coroutine.close()
            return object()

    async def forbidden(*args, **kwargs):
        pytest.fail("paused daemon fetched monitoring nodes")

    async def finish(*args, **kwargs):
        assert "enable_disabled_users" in registered
        assert "user_sync" in registered
        assert "telegram_watchdog" in registered
        assert "monitoring_admission" in registered
        raise asyncio.CancelledError

    monkeypatch.setattr(database, "init_db", AsyncMock())
    monkeypatch.setattr(limiter, "_supervise", AsyncMock())
    monkeypatch.setattr(limiter, "run_telegram_bot", AsyncMock())
    monkeypatch.setattr(
        limiter,
        "read_config",
        AsyncMock(return_value={"panel": {"username": "a", "password": "b", "domain": "panel.local"}}),
    )
    monkeypatch.setattr(limiter, "get_nodes", forbidden)
    monkeypatch.setattr(limiter, "run_check_users_usage", finish)
    monkeypatch.setattr(limiter, "cleanup_resources", AsyncMock())
    monkeypatch.setattr(sync, "refresh_user_metadata_cache", AsyncMock())
    monkeypatch.setattr(sync, "recompute_all_user_limits", AsyncMock())
    monkeypatch.setattr(limiter.asyncio, "TaskGroup", Group)
    monkeypatch.setattr(limiter.asyncio, "sleep", AsyncMock())
    with pytest.raises(asyncio.CancelledError):
        await limiter.main()


async def test_pause_notice_is_direct_private_and_attempted_once(paused_topics, monkeypatch):
    from telegram_bot import main as bot_main
    import telegram_bot.send_message as messages
    from utils.monitoring_gate import monitoring_snapshot, warn_paused_admins

    deliveries = []

    class Bot:
        async def send_message(self, **kwargs):
            deliveries.append(kwargs)

    async def forbidden(*args, **kwargs):
        pytest.fail("pause notice entered observational delivery queue")

    monkeypatch.setenv("ADMIN_IDS", "123456789,-1001234567890")
    monkeypatch.setattr(bot_main, "application", SimpleNamespace(bot=Bot(), running=True))
    monkeypatch.setattr(messages, "send_logs", forbidden)
    admission = monitoring_snapshot()
    await warn_paused_admins(admission)
    await warn_paused_admins(admission)
    assert len(deliveries) == 1
    assert deliveries[0]["chat_id"] == 123456789
    assert "paused" in deliveries[0]["text"]


async def test_pause_notice_retries_when_bot_becomes_available(paused_topics, monkeypatch):
    from telegram_bot import main as bot_main
    from utils.monitoring_gate import monitoring_snapshot, warn_paused_admins

    deliveries = []

    class Bot:
        async def send_message(self, **kwargs):
            deliveries.append(kwargs)
            raise RuntimeError("network unavailable")

    app = SimpleNamespace(bot=Bot(), running=False)
    monkeypatch.setenv("ADMIN_IDS", "123456789")
    monkeypatch.setattr(bot_main, "application", app)
    admission = monitoring_snapshot()
    await warn_paused_admins(admission)
    assert deliveries == []
    app.running = True
    await warn_paused_admins(admission)
    await warn_paused_admins(admission)
    assert len(deliveries) == 1
    app.running = False
    await warn_paused_admins(admission)
    app.running = True
    await warn_paused_admins(admission)
    assert len(deliveries) == 2


async def test_resume_accepts_fresh_logs_only(operational_topics, monkeypatch):
    import time
    import utils.parse_logs as parser
    from utils.monitoring_gate import synchronize_monitoring
    from utils.shared_state import ACTIVE_USERS
    from datetime import datetime, timezone

    epoch = datetime(2026, 10, 24, 10, tzinfo=timezone.utc).timestamp()
    now = [epoch]
    monkeypatch.setattr(time, "time", lambda: now[0])
    monkeypatch.setattr(parser, "is_ip_accepted", AsyncMock(return_value=True))
    config = {"ip_source": "logs", "log_timezone": "UTC", "check_interval": 60}

    def record(offset, username):
        stamp = datetime.fromtimestamp(epoch + offset, timezone.utc).strftime("%Y/%m/%d %H:%M:%S")
        return f"{stamp} from 5.6.7.8:123 accepted tcp:example.com:443 [Vless >> DIRECT] email: 1.{username}"

    await parser.parse_logs("", 1, "node", config)
    now[0] += 60
    await parser.parse_logs(record(20, "before"), 1, "node", config)
    assert "before" in ACTIVE_USERS
    await operational_topics.set_enabled(False)
    await synchronize_monitoring()
    await parser.parse_logs(record(80, "during"), 1, "node", config)
    now[0] += 60
    await operational_topics.set_enabled(True)
    await synchronize_monitoring()
    await parser.parse_logs("", 1, "node", config)
    now[0] += 60
    await parser.parse_logs(record(20, "before"), 1, "node", config)
    await parser.parse_logs(record(80, "during"), 1, "node", config)
    await parser.parse_logs(record(140, "fresh"), 1, "node", config)
    assert set(ACTIVE_USERS) == {"fresh"}


async def test_off_on_during_api_publish_lock_rejects_old_collection(operational_topics, monkeypatch):
    from utils.monitoring_gate import monitoring_snapshot, synchronize_monitoring
    from utils.read_config import get_config_revision
    from utils.shared_state import (
        APISampleEvidence,
        ACTIVE_USERS,
        ACTIVE_USERS_LOCK,
        begin_api_collection,
        publish_api_snapshot,
    )

    await synchronize_monitoring()
    admission = monitoring_snapshot()
    evidence = APISampleEvidence(begin_api_collection(), get_config_revision(), 90)
    async with ACTIVE_USERS_LOCK:
        task = asyncio.create_task(publish_api_snapshot({"alice": UserType(name="alice", ip=["5.6.7.8"])}, evidence))
        await asyncio.sleep(0)
        await operational_topics.set_enabled(False)
        await operational_topics.set_enabled(True)
        assert monitoring_snapshot() != admission
    assert await task is False
    assert ACTIVE_USERS == {}


async def test_fast_off_on_invalidates_admission_without_an_intermediate_snapshot(operational_topics):
    from utils.monitoring_gate import monitoring_is_current, monitoring_snapshot

    admission = monitoring_snapshot()
    await operational_topics.set_enabled(False)
    await operational_topics.set_enabled(True)

    assert monitoring_snapshot().allowed is True
    assert monitoring_snapshot().revision > admission.revision
    assert monitoring_is_current(admission) is False


async def test_scheduled_sse_task_keeps_its_admission_before_first_execution(operational_topics, monkeypatch):
    import utils.get_logs as logs

    scheduled = []
    requested = []

    class Group:
        def create_task(self, coroutine, **kwargs):
            scheduled.append(coroutine)
            return object()

    async def token(*args):
        requested.append("token")
        raise asyncio.CancelledError

    monkeypatch.setattr(logs, "get_token", token)
    monkeypatch.setattr(logs, "_ip_source_is_api", AsyncMock(return_value=False))
    await logs.create_node_task(PANEL, Group(), NODE)
    await operational_topics.set_enabled(False)
    await operational_topics.set_enabled(True)
    try:
        await scheduled[0]
    except asyncio.CancelledError:
        pass
    assert requested == []


async def test_scheduled_node_report_keeps_its_admission_before_first_execution(operational_topics, monkeypatch):
    import time
    import utils.get_logs as logs

    scheduled = []
    delivered = []

    def schedule(coroutine):
        scheduled.append(coroutine)
        return SimpleNamespace(done=lambda: False)

    async def edit(*args):
        delivered.append(args)
        return True

    monkeypatch.setattr(logs, "_node_status_message_id", 123)
    monkeypatch.setattr(logs, "_last_status_edit_time", time.time())
    monkeypatch.setattr(logs, "_last_status_text", "")
    monkeypatch.setattr(logs, "_get_status_throttle_interval", AsyncMock(return_value=60))
    monkeypatch.setattr(logs, "edit_message", edit)
    with monkeypatch.context() as scheduling:
        scheduling.setattr(asyncio, "create_task", schedule)
        await logs._update_node_status(1, "old node", "✅ Connected")
    await operational_topics.set_enabled(False)
    await operational_topics.set_enabled(True)
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())
    await scheduled[0]
    assert delivered == []


async def test_queued_api_requests_do_not_start_after_disable(operational_topics, monkeypatch):
    import utils.ip_source_api as api
    import utils.panel_api.online_ips as online

    requests = []
    monkeypatch.setattr(online, "resolve_panel_token", AsyncMock(return_value="token"))

    async def fetch(_panel, user_id, token, **kwargs):
        requests.append(user_id)
        await operational_topics.set_enabled(False)
        return {}, online.OUTCOME_OK

    monkeypatch.setattr(online, "fetch_user_online_ips", fetch)
    payloads, _, _ = await api._fetch_all_online_ips(PANEL, [("alice", 1, {}), ("bob", 2, {})], 1, 8)
    assert requests == [1]
    assert payloads == {}


@pytest.mark.parametrize("resume", [False, True])
async def test_history_orders_owned_database_write_before_topics_transition(
    operational_topics, monkeypatch, tmp_path, resume
):
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    import db.database as database
    from db.models import Base, IPHistory
    from db.crud.ip_history import IPHistoryCRUD
    from utils.ip_history_tracker import IPHistoryTracker

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'history.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    monkeypatch.setattr(database, "AsyncSessionLocal", async_sessionmaker(engine, expire_on_commit=False))
    original = IPHistoryCRUD.bulk_record

    transition_task = None
    transition_started = asyncio.Event()

    async def transition():
        transition_started.set()
        await operational_topics.set_enabled(False)
        if resume:
            await operational_topics.set_enabled(True)

    async def write(db, pairs):
        nonlocal transition_task
        written = await original(db, pairs)
        transition_task = asyncio.create_task(transition())
        await transition_started.wait()
        assert operational_topics.enabled is True
        assert not transition_task.done()
        return written

    monkeypatch.setattr(IPHistoryCRUD, "bulk_record", write)
    try:
        written = await IPHistoryTracker().record_many({"alice": {"5.6.7.8"}})
        await asyncio.wait_for(transition_task, 2)
        async with database.get_db() as db:
            records = (await db.execute(select(IPHistory))).scalars().all()
        assert written == 1
        assert [(row.username, row.ip) for row in records] == [("alice", "5.6.7.8")]
        assert operational_topics.enabled is resume
    finally:
        if transition_task is not None:
            await asyncio.gather(transition_task, return_exceptions=True)
        await engine.dispose()


async def test_usage_report_does_not_start_while_paused(paused_topics, monkeypatch):
    import utils.check_usage as usage
    from db.crud import UserCRUD

    async def forbidden(*args, **kwargs):
        pytest.fail("paused usage report requested observation metadata")

    monkeypatch.setattr(UserCRUD, "get_all_special_limits", forbidden)
    assert await usage.check_ip_used(config_data={}) == {}


async def test_paused_cycle_receipt_does_not_claim_a_completed_scan(paused_topics, monkeypatch):
    import utils.check_usage as usage
    from utils.monitoring_gate import get_last_completed_scan, get_last_cycle_outcome

    monkeypatch.setattr(usage, "read_config", AsyncMock(return_value={"ip_source": "api", "check_interval": 60}))

    async def stop(_delay):
        raise asyncio.CancelledError

    monkeypatch.setattr(asyncio, "sleep", stop)
    with pytest.raises(asyncio.CancelledError):
        await usage.run_check_users_usage(PANEL)
    assert get_last_completed_scan() is None
    outcome = get_last_cycle_outcome()
    assert outcome.outcome == "skipped"
    assert outcome.source == "api"
    assert "disabled" in outcome.reason


async def test_cycle_outcome_retains_the_configuration_revision_used_for_the_attempt(operational_topics, monkeypatch):
    import utils.check_usage as usage
    import utils.ip_source_api as api
    import utils.read_config as config
    from utils.monitoring_gate import get_last_completed_scan, get_last_cycle_outcome

    revision = [7]
    monkeypatch.setattr(config, "get_config_revision", lambda: revision[0])
    monkeypatch.setattr(
        usage, "read_config", AsyncMock(return_value={"ip_source": "api", "check_interval": 60, "_revision": 7})
    )

    async def collect(*args):
        revision[0] = 8
        return False

    async def stop(*args):
        raise asyncio.CancelledError

    monkeypatch.setattr(api, "collect_active_users_from_api", collect)
    monkeypatch.setattr(usage, "wait_for_monitoring_change", stop)
    with pytest.raises(asyncio.CancelledError):
        await usage.run_check_users_usage(PANEL)

    assert get_last_completed_scan() is None
    assert get_last_cycle_outcome().outcome == "skipped"
    assert get_last_cycle_outcome().config_revision == 7


async def test_existing_timed_ban_is_lifted_while_topics_are_paused(paused_topics, monkeypatch):
    import utils.panel_api.users as users
    import utils.panel_api.request_helper as requests
    import telegram_bot.send_message as messages

    enabled = []
    removed = []
    sleeps = [0]

    async def sleep(_delay):
        sleeps[0] += 1
        if sleeps[0] > 1:
            raise asyncio.CancelledError

    async def lift(_panel, names):
        enabled.extend(names)
        return {"enabled": ["alice"], "failed": [], "not_found": []}

    async def remove(name):
        removed.append(name)

    monkeypatch.setattr(asyncio, "sleep", sleep)
    monkeypatch.setattr(requests, "is_panel_available", lambda: True)
    monkeypatch.setattr(users, "read_config", AsyncMock(return_value={"monitoring": {"time_to_active_users": 60}}))
    monkeypatch.setattr(users.dis_users, "users_to_enable", AsyncMock(return_value=["alice"]))
    monkeypatch.setattr(users.dis_users, "enable", remove)
    monkeypatch.setattr(users, "enable_selected_users", lift)
    monkeypatch.setattr(messages, "send_enable_notification", AsyncMock())
    with pytest.raises(asyncio.CancelledError):
        await users.enable_dis_user(PANEL)
    assert enabled == ["alice"]
    assert removed == ["alice"]


async def test_missing_forum_group_pauses_collection(operational_topics, monkeypatch):
    import utils.ip_source_api as api
    from utils.monitoring_gate import monitoring_snapshot

    operational_topics._group_id = None

    async def forbidden(*args, **kwargs):
        pytest.fail("collector started without a forum group")

    monkeypatch.setattr(api, "_resolve_node_context", forbidden)
    assert monitoring_snapshot().allowed is False
    assert "group" in monitoring_snapshot().reason
    assert await api.collect_active_users_from_api(PANEL, {"ip_source": "api"}) is False


async def test_telegram_unavailability_does_not_pause_enabled_topics(operational_topics, monkeypatch):
    from telegram_bot import main as bot_main
    from utils.monitoring_gate import monitoring_snapshot, monitoring_is_current

    monkeypatch.setattr(bot_main, "application", None)
    admission = monitoring_snapshot()
    assert admission.allowed is True
    assert monitoring_is_current(admission) is True


async def test_warning_batches_stop_when_topics_disable_during_delivery(operational_topics, monkeypatch):
    import utils.check_usage as usage
    import telegram_bot.send_message as messages

    batches = []

    async def send(message, **kwargs):
        batches.append(message)
        await operational_topics.set_enabled(False)

    monkeypatch.setattr(messages, "send_warning_log", send)
    await usage.dispatch_chunked_warnings([{"username": f"user{i}"} for i in range(11)], 60, 11)
    assert len(batches) == 1


async def test_usage_loop_rechecks_topics_after_awaited_api_collection(operational_topics, monkeypatch):
    import utils.check_usage as usage
    import utils.ip_source_api as api

    monkeypatch.setattr(usage, "read_config", AsyncMock(return_value={"ip_source": "api", "check_interval": 60}))

    async def collect(*args):
        await operational_topics.set_enabled(False)
        return True

    async def forbidden(*args, **kwargs):
        pytest.fail("usage loop enforced a collection after Topics were disabled")

    async def stop(*args):
        raise asyncio.CancelledError

    monkeypatch.setattr(api, "collect_active_users_from_api", collect)
    monkeypatch.setattr(usage, "check_users_usage", forbidden)
    monkeypatch.setattr(asyncio, "sleep", stop)
    with pytest.raises(asyncio.CancelledError):
        await usage.run_check_users_usage(PANEL)


async def test_parser_does_not_adopt_resume_while_waiting_to_synchronize(operational_topics, monkeypatch):
    import utils.monitoring_gate as gate
    import utils.parse_logs as parser

    prepared = []

    async def accept(*args):
        prepared.append(args)
        return True

    monkeypatch.setattr(parser, "is_ip_accepted", accept)
    await gate.synchronize_monitoring()
    async with gate._sync_lock:
        task = asyncio.create_task(
            parser.parse_logs(
                "2026/10/24 10:00:00 from 5.6.7.8:123 accepted tcp:example.com:443 [Vless >> DIRECT] email: 1.alice",
                1,
                "old node",
                {"ip_source": "logs", "log_timezone": "UTC"},
            )
        )
        await asyncio.sleep(0)
        await operational_topics.set_enabled(False)
        await operational_topics.set_enabled(True)
    await task
    assert prepared == []


async def test_usage_report_does_not_adopt_resume_while_waiting_to_synchronize(operational_topics, monkeypatch):
    import utils.monitoring_gate as gate
    import utils.check_usage as usage
    from db.crud import UserCRUD

    requested = []

    async def limits(*args):
        requested.append("limits")
        return {}

    monkeypatch.setattr(UserCRUD, "get_all_special_limits", limits)
    monkeypatch.setattr(
        usage,
        "_ensure_isp_detector",
        AsyncMock(return_value=SimpleNamespace(get_multiple_isp_info=AsyncMock(return_value={}))),
    )
    monkeypatch.setattr(usage, "get_active_users_metadata_batch", AsyncMock(return_value={}))
    monkeypatch.setattr(usage, "get_group_limits_batch", AsyncMock(return_value={}))
    await gate.synchronize_monitoring()
    async with gate._sync_lock:
        task = asyncio.create_task(usage.check_ip_used(config_data={}, active_users_snapshot={}))
        await asyncio.sleep(0)
        await operational_topics.set_enabled(False)
        await operational_topics.set_enabled(True)
    assert await task == {}
    assert requested == []


async def test_old_sse_does_not_publish_api_status_after_awaited_pause_resume(operational_topics, monkeypatch):
    import utils.get_logs as logs

    delivered = []

    async def mode():
        await operational_topics.set_enabled(False)
        await operational_topics.set_enabled(True)
        return True

    async def notify(message, **kwargs):
        delivered.append(message)
        return 123

    async def stop(*args):
        raise asyncio.CancelledError

    monkeypatch.setattr(logs, "_ip_source_is_api", mode)
    monkeypatch.setattr(logs, "_get_status_throttle_interval", AsyncMock(return_value=0))
    monkeypatch.setattr(logs, "_node_status_message_id", None)
    monkeypatch.setattr(logs, "_last_status_text", "")
    monkeypatch.setattr(logs, "send_logs", notify)
    monkeypatch.setattr(logs.asyncio, "sleep", stop)
    try:
        await logs.get_nodes_logs(PANEL, NODE)
    except asyncio.CancelledError:
        pass
    assert delivered == []


async def test_sse_refresh_does_not_adopt_nodes_queried_before_pause_resume(operational_topics, monkeypatch):
    import utils.get_logs as logs

    scheduled = []
    periodic_sleeps = 0

    class Group:
        def create_task(self, coroutine, **kwargs):
            coroutine.close()
            scheduled.append(kwargs["name"])
            return object()

    async def sleep(delay):
        nonlocal periodic_sleeps
        if delay == 7200:
            periodic_sleeps += 1
            if periodic_sleeps > 1:
                raise asyncio.CancelledError

    async def nodes(*args, **kwargs):
        await operational_topics.set_enabled(False)
        await operational_topics.set_enabled(True)
        return [NODE]

    monkeypatch.setattr(logs.asyncio, "sleep", sleep)
    monkeypatch.setattr(logs, "_ip_source_is_api", AsyncMock(return_value=False))
    monkeypatch.setattr(logs, "get_nodes", nodes)
    monkeypatch.setattr(logs, "send_logs", AsyncMock(return_value=123))
    monkeypatch.setattr(
        "utils.read_config.read_config", AsyncMock(return_value={"ip_source": "logs", "log_timezone": "UTC"})
    )
    with pytest.raises(asyncio.CancelledError):
        await logs.handle_cancel_all([], PANEL, Group())
    assert scheduled == []


async def test_node_discovery_keeps_admission_after_awaited_status_delivery(operational_topics, monkeypatch):
    import utils.get_logs as logs

    scheduled = []

    class Group:
        def create_task(self, coroutine, **kwargs):
            coroutine.close()
            scheduled.append(kwargs["name"])
            return object()

    async def notify(message, **kwargs):
        await operational_topics.set_enabled(False)
        await operational_topics.set_enabled(True)
        return 123

    async def stop(*args):
        raise asyncio.CancelledError

    monkeypatch.setattr(logs, "get_nodes", AsyncMock(return_value=[NODE]))
    monkeypatch.setattr(logs, "_get_status_throttle_interval", AsyncMock(return_value=0))
    monkeypatch.setattr(logs, "_get_node_poll_interval", AsyncMock(return_value=60))
    monkeypatch.setattr(logs, "_node_status_message_id", None)
    monkeypatch.setattr(logs, "_last_status_text", "")
    monkeypatch.setattr(logs, "send_logs", notify)
    monkeypatch.setattr(logs, "wait_for_monitoring_change", stop)
    with pytest.raises(asyncio.CancelledError):
        await logs.check_and_add_new_nodes(PANEL, Group())
    assert scheduled == []


@pytest.mark.parametrize("resume", [False, True])
async def test_failed_cycle_requeue_cannot_restore_logs_after_topics_change(
    operational_topics, completed_logs_evidence, monkeypatch, resume
):
    import time
    from utils.shared_state import ACTIVE_USERS, ACTIVE_USERS_LOCK, ActiveUsersSnapshot, merge_active_users_snapshot
    from utils.types import ConnectionInfo, DeviceInfo, UserType

    batch = ActiveUsersSnapshot()
    batch.log_evidence = completed_logs_evidence()
    batch["alice"] = UserType(
        name="alice",
        ip=["5.6.7.8"],
        device_info=DeviceInfo(
            connections=[
                ConnectionInfo(
                    ip="5.6.7.8", node_id=1, node_name="node", inbound_protocol="Vless", last_seen=time.time() - 1
                )
            ]
        ),
    )
    async with ACTIVE_USERS_LOCK:
        task = asyncio.create_task(merge_active_users_snapshot(batch))
        await asyncio.sleep(0)
        await operational_topics.set_enabled(False)
        if resume:
            await operational_topics.set_enabled(True)
    assert await task == 0
    assert ACTIVE_USERS == {}


@pytest.mark.parametrize("resume", [False, True])
async def test_parser_stops_preparation_after_numeric_id_lookup_changes_topics(operational_topics, monkeypatch, resume):
    import utils.parse_logs as parser
    from db.crud.users import UserCRUD

    prepared = []

    async def resolve(*args):
        await operational_topics.set_enabled(False)
        if resume:
            await operational_topics.set_enabled(True)
        return "alice"

    async def accept(ip, config):
        prepared.append(ip)
        return True

    monkeypatch.setattr(UserCRUD, "get_username_by_panel_id", resolve)
    monkeypatch.setattr(parser, "is_ip_accepted", accept)
    parser.LOCAL_ID_CACHE.pop("771188", None)
    await parser.parse_logs(
        "2026/10/24 10:00:00 from 5.6.7.8:123 accepted tcp:example.com:443 [Vless >> DIRECT] email: 771188",
        1,
        "node",
        {"ip_source": "logs", "log_timezone": "UTC"},
    )
    assert prepared == []


@pytest.mark.parametrize("resume", [False, True])
async def test_parser_stops_preparation_of_later_lines_after_topics_change(operational_topics, monkeypatch, resume):
    import utils.parse_logs as parser

    prepared = []

    async def accept(ip, config):
        prepared.append(ip)
        await operational_topics.set_enabled(False)
        if resume:
            await operational_topics.set_enabled(True)
        return True

    monkeypatch.setattr(parser, "is_ip_accepted", accept)
    await parser.parse_logs(
        "2026/10/24 10:00:00 from 5.6.7.8:123 accepted tcp:example.com:443 [Vless >> DIRECT] email: 1.alice\n"
        "2026/10/24 10:00:00 from 8.8.8.8:123 accepted tcp:example.com:443 [Vless >> DIRECT] email: 2.bob",
        1,
        "node",
        {"ip_source": "logs", "log_timezone": "UTC"},
    )
    assert prepared == ["5.6.7.8"]


@pytest.mark.parametrize("nested", [False, True])
async def test_history_retries_rolled_back_pruning_after_commit_failure(
    operational_topics, monkeypatch, tmp_path, nested
):
    from datetime import datetime, timedelta, timezone
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
    from db.crud.ip_history import IPHistoryCRUD
    from db.models import Base, IPHistory
    import db.database as database
    from utils.ip_history_tracker import IPHistoryTracker

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'commit-history.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    commit_attempts = 0

    class Session(AsyncSession):
        async def commit(self):
            nonlocal commit_attempts
            commit_attempts += 1
            if commit_attempts == 1:
                raise RuntimeError("commit unavailable")
            await super().commit()

    monkeypatch.setattr(
        database, "AsyncSessionLocal", async_sessionmaker(engine, class_=Session, expire_on_commit=False)
    )
    if nested:
        original = IPHistoryCRUD.bulk_record

        async def record_in_savepoint(db, pairs):
            async with db.begin_nested():
                return await original(db, pairs)

        monkeypatch.setattr(IPHistoryCRUD, "bulk_record", record_in_savepoint)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        tracker = IPHistoryTracker()
        written = await tracker.record_many({"alice": {"5.6.7.8"}})
        async with sessions() as session:
            rows = (await session.execute(select(IPHistory))).scalars().all()
        assert rows == []
        assert written == 0

        # Rolled-back pruning must remain due for the next successful transaction.
        old = datetime.now(timezone.utc) - timedelta(hours=72)
        async with sessions() as session:
            session.add(IPHistory(username="stale", ip="8.8.8.8", first_seen=old, last_seen=old))
            await session.commit()
        await operational_topics.set_enabled(True)
        assert await tracker.record_many({"bob": {"5.6.7.9"}}) == 1
        async with sessions() as session:
            rows = (await session.execute(select(IPHistory))).scalars().all()
        assert [(row.username, row.ip) for row in rows] == [("bob", "5.6.7.9")]
    finally:
        await engine.dispose()


@pytest.mark.parametrize("resume", [False, True])
async def test_history_commit_is_ordered_before_concurrent_topics_transition(
    operational_topics, monkeypatch, tmp_path, resume
):
    import asyncio
    import aiosqlite
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from db.models import Base, IPHistory
    import db.database as database
    from utils.ip_history_tracker import IPHistoryTracker

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'ordering.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    monkeypatch.setattr(database, "AsyncSessionLocal", async_sessionmaker(engine, expire_on_commit=False))
    commit_entered, commit_release, transition_started = asyncio.Event(), asyncio.Event(), asyncio.Event()
    events = []
    original_commit = aiosqlite.Connection.commit

    async def backend_commit(connection):
        events.append("commit-entered")
        commit_entered.set()
        await commit_release.wait()
        await original_commit(connection)
        events.append("commit-finished")

    monkeypatch.setattr(aiosqlite.Connection, "commit", backend_commit)

    async def transition():
        transition_started.set()
        await operational_topics.set_enabled(False)
        if resume:
            await operational_topics.set_enabled(True)
        events.append("transition-acknowledged")

    history_task = asyncio.create_task(IPHistoryTracker().record_many({"alice": {"5.6.7.8"}}))
    transition_task = None
    try:
        await asyncio.wait_for(commit_entered.wait(), 2)
        transition_task = asyncio.create_task(transition())
        await transition_started.wait()
        try:
            await asyncio.wait_for(asyncio.shield(transition_task), 0.2)
        except asyncio.TimeoutError:
            pass
        assert operational_topics.enabled is True
        assert not transition_task.done(), "Topics transition acknowledged before admitted history became durable"
        commit_release.set()
        assert await asyncio.wait_for(history_task, 2) == 1
        await asyncio.wait_for(transition_task, 2)
        assert events.index("commit-finished") < events.index("transition-acknowledged")
        async with async_sessionmaker(engine)() as session:
            rows = (await session.execute(select(IPHistory))).scalars().all()
        assert len(rows) == 1
        assert operational_topics.enabled is resume
    finally:
        commit_release.set()
        await asyncio.wait_for(
            asyncio.gather(history_task, *([transition_task] if transition_task else []), return_exceptions=True), 2
        )
        await engine.dispose()


@pytest.mark.parametrize("resume", [False, True])
async def test_history_waiting_behind_topics_transition_rejects_its_old_admission(
    operational_topics, monkeypatch, tmp_path, resume
):
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from db.models import Base, IPHistory
    import db.database as database
    from utils.ip_history_tracker import IPHistoryTracker

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'waiting-history.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    monkeypatch.setattr(database, "AsyncSessionLocal", async_sessionmaker(engine, expire_on_commit=False))
    history_waiting, transition_started = asyncio.Event(), asyncio.Event()
    history_task = None
    transition_task = None

    class SignalingLock(asyncio.Lock):
        async def acquire(self):
            # History may acquire through a private transaction owner task.
            if history_task is not None and asyncio.current_task() is not transition_task:
                history_waiting.set()
            return await super().acquire()

    lock = SignalingLock()
    monkeypatch.setattr(operational_topics, "_write_lock", lock)

    async def transition():
        transition_started.set()
        await operational_topics.set_enabled(False)
        if resume:
            await operational_topics.set_enabled(True)

    try:
        async with lock:
            transition_task = asyncio.create_task(transition())
            await transition_started.wait()
            history_task = asyncio.create_task(IPHistoryTracker().record_many({"alice": {"5.6.7.8"}}))
            await asyncio.wait_for(history_waiting.wait(), 2)
        await asyncio.wait_for(transition_task, 2)
        assert await asyncio.wait_for(history_task, 2) == 0
        async with async_sessionmaker(engine)() as session:
            rows = (await session.execute(select(IPHistory))).scalars().all()
        assert rows == []
        assert operational_topics.enabled is resume
    finally:
        await asyncio.gather(
            *(task for task in (history_task, transition_task) if task is not None), return_exceptions=True
        )
        await engine.dispose()
