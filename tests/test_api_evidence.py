"""Complete API evidence can be judged; unknown observations retain warnings."""

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


@pytest.fixture
async def api_cycle(tmp_path, monkeypatch, operational_topics):
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    import db.database as database
    import utils.check_usage as usage
    import utils.read_config as read_config
    import utils.ip_source_api as api
    import utils.panel_api.auth as auth
    import utils.panel_api.nodes as nodes
    import utils.panel_api.request_helper as requests
    import utils.shared_state as state
    import utils.user_sync as sync
    from db.crud import UserCRUD
    from db.models import Base
    from utils.types import NodeType, PanelType
    from utils.warning_system.enhanced_system import EnhancedWarningSystem

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'evidence.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    monkeypatch.setattr(
        database, "AsyncSessionLocal", async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    )
    import asyncio

    # This database owns a new canonical settings generation. A prior test's
    # cached database revision must not invalidate the first collected sample.
    monkeypatch.setattr(read_config, "_config_cache", None)
    monkeypatch.setattr(read_config, "_config_cache_db_revision", None)
    monkeypatch.setattr(read_config, "_config_read_lock", asyncio.Lock())
    monkeypatch.setattr(read_config, "_config_write_lock", asyncio.Lock())
    monkeypatch.setattr(api, "_collection_lock", asyncio.Lock())
    monkeypatch.setattr(api, "_consecutive_dead_cycles", 0)
    monkeypatch.setattr(api, "_failure_revision", None)
    state.ACTIVE_USERS.clear()
    sync.USER_METADATA_CACHE.clear()
    async with database.get_db() as db:
        for name in ("alice", "bob"):
            user = await UserCRUD.create_or_update(db, name)
            user.special_limit = 1
    warning = EnhancedWarningSystem(
        filename=str(tmp_path / "warnings.json"), history_filename=str(tmp_path / "history.json")
    )
    monkeypatch.setattr(usage, "warning_system", warning)
    monkeypatch.setattr(usage, "isp_detector", None)
    monkeypatch.setattr(usage.ISPDetector, "get_multiple_isp_info", AsyncMock(return_value={}))
    # Only external I/O is replaced; collection, metadata, counting and warnings run.
    monkeypatch.setattr(usage, "send_logs", AsyncMock())
    monkeypatch.setattr(usage, "send_user_message", AsyncMock())
    monkeypatch.setattr(auth, "get_token", AsyncMock(return_value=SimpleNamespace(panel_token="test")))
    monkeypatch.setattr(nodes, "get_nodes", AsyncMock(return_value=[NodeType(1, "node", "192.0.2.1", "connected")]))
    monkeypatch.setattr(requests, "is_panel_available", lambda: True)
    monkeypatch.setattr(api, "_notify", AsyncMock())
    now = int(time.time())
    bodies = {
        "alice": {"nodes": {"1": {"ips": {"5.6.7.8": now, "9.9.9.9": now}}}},
        "bob": {"nodes": {"1": {"ips": {"5.6.7.8": now, "9.9.9.9": now}}}},
    }

    async def get(_panel, path, **kwargs):
        assert path == "/api/users"
        return SimpleNamespace(
            json=lambda: {"users": [{"id": i, "username": n} for i, n in enumerate(bodies, 1)], "total": len(bodies)}
        )

    async def request(_panel, _method, path, _token, **kwargs):
        name = list(bodies)[int(path.split("/")[-2]) - 1]
        body = bodies[name]
        if body is None:
            return None, "unavailable"
        return SimpleNamespace(status_code=200, json=lambda: body), None

    monkeypatch.setattr(requests, "panel_get", get)
    monkeypatch.setattr(requests, "panel_request", request)
    config = {
        "ip_source": "api",
        "api_ip_freshness": 90,
        "check_interval": 60,
        "max_warning_count": 3,
        "limits": {"general": 1},
        "api_ip_min_coverage": 0,
        "api_ip_auto_fallback": False,
        "country_code": "",
        "except_users": [],
    }
    panel = PanelType("admin", "test", "panel.local")

    async def run():
        ready = await api.collect_active_users_from_api(panel, config)
        if ready:
            await usage.check_users_usage(panel, config)
        return ready

    yield SimpleNamespace(
        run=run, bodies=bodies, warning=warning, config=config, now=now, panel=panel, topics=operational_topics
    )
    state.ACTIVE_USERS.clear()
    sync.USER_METADATA_CACHE.clear()
    await engine.dispose()


@pytest.mark.parametrize(
    "unknown", [None, {}, {"nodes": {"1": None}}, {"nodes": {}}, {"nodes": {"1": {"ips": {"5.6.7.8": 1}}}}]
)
async def test_unknown_user_keeps_counter_while_complete_user_advances(api_cycle, unknown, monkeypatch):
    await api_cycle.run()
    alice = api_cycle.warning.warnings["alice"]
    before = alice.consecutive_violations
    monkeypatch.setattr(time, "time", lambda: api_cycle.now + 60)
    api_cycle.bodies["alice"] = unknown
    assert await api_cycle.run()
    assert api_cycle.warning.warnings["alice"] is alice
    assert alice.consecutive_violations == before
    assert api_cycle.warning.warnings["bob"].consecutive_violations == 2


async def test_all_valid_expired_observations_clear_pending_warnings(api_cycle):
    await api_cycle.run()
    assert set(api_cycle.warning.warnings) == {"alice", "bob"}
    for name in api_cycle.bodies:
        api_cycle.bodies[name] = {"nodes": {"1": {"ips": {"5.6.7.8": api_cycle.now - 100}}}}
    assert await api_cycle.run()
    assert api_cycle.warning.warnings == {}


async def test_completed_scan_receipt_records_actual_count_and_protective_skips(api_cycle):
    from utils.monitoring_gate import get_last_completed_scan, get_last_cycle_outcome

    api_cycle.bodies["alice"] = None
    assert await api_cycle.run() is True
    receipt = get_last_completed_scan()
    assert receipt.source == "api"
    assert receipt.users_counted == 1
    assert receipt.protected_users_count == 1
    assert receipt.metadata_unknown_users_count == 0
    assert receipt.failed_users_count == 0
    assert get_last_cycle_outcome().outcome == "completed"
    await api_cycle.topics.set_enabled(False)
    assert await api_cycle.run() is False
    assert get_last_completed_scan() is receipt


async def test_expired_unknown_warning_survives_persistent_and_cleanup_paths(api_cycle):
    await api_cycle.run()
    warning = api_cycle.warning.warnings["alice"]
    warning.monitoring_end_time -= 1000
    warning.consecutive_violations = 3
    api_cycle.bodies["alice"] = None
    assert await api_cycle.run()
    assert api_cycle.warning.warnings["alice"] is warning


async def test_setting_change_during_collection_discards_batch(api_cycle, monkeypatch):
    import utils.panel_api.request_helper as requests
    from utils.read_config import save_config_value
    import utils.shared_state as state

    original = requests.panel_request

    async def request(*args, **kwargs):
        await save_config_value("api_ip_freshness", "10")
        return await original(*args, **kwargs)

    monkeypatch.setattr(requests, "panel_request", request)
    assert await api_cycle.run() is False
    assert state.ACTIVE_USERS == {}
    assert api_cycle.warning.warnings == {}


async def test_freshness_is_rechecked_after_async_preparation(api_cycle, monkeypatch):
    import utils.check_usage as usage

    await api_cycle.run()
    clock = [float(api_cycle.now)]
    monkeypatch.setattr(time, "time", lambda: clock[0])

    prepare = usage.read_config_snapshot

    async def delayed_metadata(*args, **kwargs):
        metadata = await prepare(*args, **kwargs)
        clock[0] = api_cycle.now + 90
        return metadata

    monkeypatch.setattr(usage, "read_config_snapshot", delayed_metadata)
    assert await api_cycle.run()
    assert api_cycle.warning.warnings == {}


async def test_config_change_during_preparation_keeps_counters(api_cycle, monkeypatch):
    import utils.check_usage as usage
    from utils.read_config import save_config_value

    await api_cycle.run()

    monkeypatch.setattr(time, "time", lambda: api_cycle.now + 60)

    prepare = usage.read_config_snapshot

    async def changed_metadata(*args, **kwargs):
        metadata = await prepare(*args, **kwargs)
        await save_config_value("ip_source", "logs")
        return metadata

    monkeypatch.setattr(usage, "read_config_snapshot", changed_metadata)
    assert await api_cycle.run()
    assert {n: w.consecutive_violations for n, w in api_cycle.warning.warnings.items()} == {"alice": 1, "bob": 1}


async def test_late_log_updates_cannot_contaminate_api_enforcement(api_cycle):
    import utils.check_usage as usage
    import utils.ip_source_api as api
    from utils.shared_state import ACTIVE_USERS
    from utils.types import UserType

    for name in api_cycle.bodies:
        api_cycle.bodies[name] = {"nodes": {"1": {"ips": {}}}}
    assert await api.collect_active_users_from_api(api_cycle.panel, api_cycle.config)
    ACTIVE_USERS["alice"] = UserType(name="alice", ip=["5.6.7.8", "9.9.9.9"])
    await usage.check_users_usage(api_cycle.panel, api_cycle.config)
    assert api_cycle.warning.warnings == {}


async def test_slow_warning_persistence_does_not_warn_later_users_on_expired_ips(api_cycle, monkeypatch):
    import utils.warning_system.enhanced_system as warnings

    await api_cycle.run()
    clock = [api_cycle.now + 60]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    write = warnings.atomic_write_json

    def delayed_write(*args, **kwargs):
        write(*args, **kwargs)
        clock[0] = api_cycle.now + 150

    monkeypatch.setattr(warnings, "atomic_write_json", delayed_write)
    assert await api_cycle.run()
    assert "bob" not in api_cycle.warning.warnings


async def test_missing_node_list_skips_enforcement(api_cycle, monkeypatch):
    import utils.panel_api.nodes as nodes

    monkeypatch.setattr(nodes, "get_nodes", AsyncMock(side_effect=RuntimeError("unavailable")))
    assert await api_cycle.run() is False
    assert api_cycle.warning.warnings == {}


@pytest.mark.parametrize("status", ["error", "connecting"])
@pytest.mark.parametrize("sample", ["empty_ips", "nonempty_ips", "no_candidates", "omitted_warning_user"])
async def test_unavailable_enabled_node_preserves_entire_warning_cycle(api_cycle, monkeypatch, status, sample):
    import utils.ip_source_api as api
    import utils.panel_api.nodes as nodes
    from utils.types import NodeType

    assert await api_cycle.run()
    warnings = dict(api_cycle.warning.warnings)
    before = {name: record.consecutive_violations for name, record in warnings.items()}
    monkeypatch.setattr(time, "time", lambda: api_cycle.now + 60)
    monkeypatch.setattr(
        nodes,
        "get_nodes",
        AsyncMock(
            return_value=[NodeType(1, "healthy", "192.0.2.1", "connected"), NodeType(2, "other", "192.0.2.2", status)]
        ),
    )
    if sample == "empty_ips":
        for name in api_cycle.bodies:
            api_cycle.bodies[name] = {"nodes": {"1": {"ips": {}}}}
        # Unknown empty observations must protect persistent/cleanup paths too.
        for record in warnings.values():
            record.monitoring_end_time -= 1000
    elif sample == "no_candidates":
        api_cycle.bodies.clear()
    elif sample == "omitted_warning_user":
        api_cycle.bodies.pop("alice")

    ready = await api_cycle.run()
    assert api_cycle.warning.warnings == warnings
    assert {name: record.consecutive_violations for name, record in warnings.items()} == before
    assert ready is False
    assert api.get_last_cycle_stats()["collection_outcome"] == "failure"
    assert api.get_last_cycle_stats()["skipped_reason"] == "node availability unknown"


@pytest.mark.parametrize("status", ["error", "connecting"])
async def test_operator_excluded_unavailable_node_allows_valid_empty_cycle(api_cycle, monkeypatch, status):
    import utils.ip_source_api as api
    import utils.panel_api.nodes as nodes
    from utils.types import NodeType

    assert await api_cycle.run()
    monkeypatch.setattr(
        nodes,
        "get_nodes",
        AsyncMock(
            return_value=[NodeType(1, "healthy", "192.0.2.1", "connected"), NodeType(2, "other", "192.0.2.2", status)]
        ),
    )
    api_cycle.config["disabled_nodes"] = [2]
    for name in api_cycle.bodies:
        api_cycle.bodies[name] = {"nodes": {"1": {"ips": {}}}}
    assert await api_cycle.run()
    assert api_cycle.warning.warnings == {}
    assert api.get_last_cycle_stats()["collection_outcome"] == "success"


async def test_deleted_user_is_valid_absence(api_cycle, monkeypatch):
    import utils.panel_api.request_helper as requests

    api_cycle.config["api_ip_min_node_coverage"] = 1.0
    await api_cycle.run()
    monkeypatch.setattr(
        requests,
        "panel_request",
        AsyncMock(return_value=(SimpleNamespace(status_code=404, json=lambda: {"detail": "User not found"}), None)),
    )
    assert await api_cycle.run()
    assert api_cycle.warning.warnings == {}


async def test_concurrent_collections_publish_in_order(api_cycle, monkeypatch):
    import asyncio
    import utils.ip_source_api as api
    import utils.panel_api.request_helper as requests
    from utils.shared_state import ACTIVE_USERS

    waiting = asyncio.Event()
    release = asyncio.Event()
    original = requests.panel_request
    calls = 0

    async def request(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls <= 2:
            waiting.set()
            await release.wait()
            return await original(*args, **kwargs)
        return SimpleNamespace(status_code=200, json=lambda: {"nodes": {"1": {"ips": {}}}}), None

    monkeypatch.setattr(requests, "panel_request", request)
    old = asyncio.create_task(api.collect_active_users_from_api(api_cycle.panel, api_cycle.config))
    await waiting.wait()
    newer = asyncio.create_task(api.collect_active_users_from_api(api_cycle.panel, api_cycle.config))
    await asyncio.sleep(0)
    pending_calls = calls
    release.set()
    assert await old is True
    assert await newer is True
    assert pending_calls == 2
    assert ACTIVE_USERS == {}


async def test_stale_config_argument_cannot_start_a_new_api_batch(api_cycle):
    from utils.read_config import read_config, save_config_value
    import utils.ip_source_api as api

    await save_config_value("ip_source", "api")
    await save_config_value("api_ip_freshness", "90")
    config = await read_config()
    await save_config_value("ip_source", "logs")
    assert await api.collect_active_users_from_api(api_cycle.panel, config) is False


async def test_partial_cycle_reports_completeness_separately_from_enforcement(api_cycle):
    from utils.ip_source_api import get_last_cycle_stats

    api_cycle.bodies["alice"] = None
    assert await api_cycle.run()
    stats = get_last_cycle_stats()
    assert stats.get("collection_outcome") == "partial"
    assert stats.get("unknown_users") == 1
    api_cycle.bodies.pop("alice")
    assert await api_cycle.run()
    assert get_last_cycle_stats().get("collection_outcome") == "success"


def _not_found_response(body):
    if body == "bare":
        return SimpleNamespace(status_code=404)

    def json_body():
        if body == "non-json":
            raise ValueError("Proxy HTML is not JSON")
        return body

    return SimpleNamespace(status_code=404, json=json_body)


@pytest.mark.parametrize("body", ["bare", "non-json", None, {}, [], {"detail": "Not Found"}])
async def test_unconfirmed_404_preserves_warning_while_known_user_advances(api_cycle, monkeypatch, body):
    import utils.panel_api.request_helper as requests

    await api_cycle.run()
    warning = api_cycle.warning.warnings["alice"]
    before = warning.consecutive_violations
    monkeypatch.setattr(time, "time", lambda: api_cycle.now + 60)
    original = requests.panel_request

    async def request(panel, method, path, token, **kwargs):
        if path == "/api/node/online_stats/1/ip":
            return _not_found_response(body), None
        return await original(panel, method, path, token, **kwargs)

    monkeypatch.setattr(requests, "panel_request", request)
    assert await api_cycle.run()
    assert api_cycle.warning.warnings["alice"] is warning
    assert warning.consecutive_violations == before
    assert api_cycle.warning.warnings["bob"].consecutive_violations == 2


@pytest.mark.parametrize(
    "body, allowed",
    [("bare", False), ("non-json", False), ({"detail": "Not Found"}, False), ({"detail": "User not found"}, True)],
)
async def test_preflight_requires_confirmed_deleted_user_for_404(api_cycle, monkeypatch, body, allowed):
    import utils.panel_api.request_helper as requests
    from utils.panel_api.online_ips import check_online_stats_permission

    monkeypatch.setattr(requests, "panel_request", AsyncMock(return_value=(_not_found_response(body), None)))
    ok, _ = await check_online_stats_permission(api_cycle.panel, 1)
    assert ok is allowed
