"""Unavailable nodes and individual users cannot retire unrelated observations."""

import copy

import pytest

import db.database as database
import utils.ip_source_api as api
from db.crud.config import ConfigCRUD
from test_api_evidence import api_cycle  # noqa: F401
from test_api_user_node_scope import two_groups  # noqa: F401
from test_api_observation_time import observed_cycle  # noqa: F401
from test_config_revisions import settings_db  # noqa: F401
from test_policy_enforcement import policy_cycle  # noqa: F401
from test_log_partial_enforcement import partial_cycle  # noqa: F401
from utils.monitoring_gate import get_last_completed_scan


@pytest.fixture
def shared_cycle(observed_cycle):
    r = observed_cycle
    r.config.update(api_ip_auto_fallback=True, api_ip_min_coverage=0.95, max_warning_count=100)
    r.memberships = {"alice": [1], "bob": [1]}
    for node in r.nodes:
        node.core_config_id = 1
    r.bodies["bob"] = copy.deepcopy(r.bodies["alice"])
    advance = r.advance

    def next_interval():
        r.clock[0] += 240
        advance()

    r.advance = next_interval
    return r


@pytest.mark.parametrize("initial,final", [("connected", "error"), ("error", "connected")])
@pytest.mark.parametrize("discarded", [None, {"ips": {"11.1.1.1": 1}}, {"ips": {"11.1.1.1": 4_000_000_000}}])
async def test_health_transition_keeps_healthy_same_group_users(shared_cycle, initial, final, discarded):
    r = shared_cycle
    r.nodes[1].status = initial
    for body in r.bodies.values():
        body["nodes"]["24"] = copy.deepcopy(discarded)

    def transition(path):
        if path == "/api/groups" and r.paths.count(path) == 2:
            r.nodes[1].status = final

    r.before_response = transition
    assert await r.run()
    assert set(r.warning.warnings) == {"alice", "bob"}
    for warning in r.warning.warnings.values():
        assert warning.ip_count == 2
        assert warning.ips == {"5.6.7.8", "9.9.9.9"}
    assert api.get_last_cycle_stats()["unknown_users"] == 0


async def test_repeated_shared_node_transitions_do_not_select_logs(shared_cycle):
    r = shared_cycle

    def transition(path):
        if path == "/api/groups" and r.paths.count(path) == 2:
            r.nodes[1].status = "error" if r.nodes[1].status == "connected" else "connected"

    r.before_response = transition
    for count in (1, 2, 3):
        r.paths.clear()
        assert await r.run()
        assert r.warning.warnings["alice"].consecutive_violations == count
        assert r.warning.warnings["bob"].consecutive_violations == count
        r.advance()
    async with database.get_db() as db:
        assert await ConfigCRUD.get(db, "ip_source", "api") == "api"


async def test_all_nodes_down_preserves_warnings_without_selecting_logs(shared_cycle):
    r = shared_cycle
    assert await r.run()
    before = {
        name: (record, record.consecutive_violations, record.trust_score) for name, record in r.warning.warnings.items()
    }
    for node in r.nodes:
        node.status = "error"
    for name in r.bodies:
        r.bodies[name] = {"nodes": {}}
    for _ in range(2):
        r.advance()
        assert not await r.run()
    async with database.get_db() as db:
        assert await ConfigCRUD.get(db, "ip_source", "api") == "api"
    for name, (record, count, trust) in before.items():
        assert r.warning.warnings[name] is record
        assert (record.consecutive_violations, record.trust_score) == (count, trust)


async def test_empty_aggregate_is_unknown_not_a_healthy_empty_node(shared_cycle):
    r = shared_cycle
    assert await r.run()
    before = dict(r.warning.warnings)
    for name in r.bodies:
        r.bodies[name] = {"nodes": {}}
    for _ in range(2):
        r.advance()
        assert not await r.run()
    assert r.warning.warnings == before
    assert api.get_last_cycle_stats()["unknown_users"] == 2
    async with database.get_db() as db:
        assert await ConfigCRUD.get(db, "ip_source", "api") == "api"


@pytest.mark.parametrize("bad", [None, {}, {"nodes": {"31": None}}])
async def test_one_invalid_user_does_not_block_valid_user_below_coverage_threshold(observed_cycle, bad):
    r = observed_cycle
    r.config.update(api_ip_min_coverage=0.95, api_ip_auto_fallback=True, max_warning_count=100)
    r.bodies["alice"] = bad
    assert await r.run()
    assert set(r.warning.warnings) == {"bob"}
    assert r.warning.warnings["bob"].consecutive_violations == 1
    assert api.get_last_cycle_stats()["unknown_users"] == 1


async def test_lost_node_does_not_normalize_a_partial_under_limit_user(shared_cycle):
    r = shared_cycle
    assert await r.run()
    previous = r.warning.warnings["alice"]
    r.advance()
    r.bodies["alice"]["nodes"]["31"]["ips"].pop("9.9.9.9")
    r.nodes[1].status = "error"
    assert await r.run()
    assert r.warning.warnings["alice"] is previous
    assert previous.consecutive_violations == 1
    assert r.warning.warnings["bob"].consecutive_violations == 2


@pytest.mark.parametrize("partial_cycle", ["ip", "device"], indirect=True)
async def test_logs_continue_with_healthy_nodes_and_preserve_missing_users(partial_cycle):
    r = partial_cycle
    retained = r.warning("bob", count=2)
    user = r.observed("alice")
    user.device_info.connections[1].ip = "203.0.113.20"
    user.ip.append("203.0.113.20")
    await r.run([user])
    assert r.system.warnings["alice"].consecutive_violations == 1
    assert r.system.warnings["bob"] is retained
    assert retained.consecutive_violations == 2
    receipt = get_last_completed_scan()
    assert receipt is not None and receipt.users_counted == 1
    assert receipt.coverage_complete is False


@pytest.mark.parametrize("bad", [{}, {"nodes": {"31": None}}])
async def test_all_invalid_http_answers_preserve_records_and_api_source(shared_cycle, bad):
    r = shared_cycle
    assert await r.run()
    before = copy.deepcopy({name: vars(record) for name, record in r.warning.warnings.items()})
    for name in r.bodies:
        r.bodies[name] = copy.deepcopy(bad)
    for _ in range(3):
        r.advance()
        assert not await r.run()
        assert api.get_last_cycle_stats()["coverage"] == 0
    assert {name: vars(record) for name, record in r.warning.warnings.items()} == before
    async with database.get_db() as db:
        assert await ConfigCRUD.get(db, "ip_source", "api") == "api"


async def test_panel_unavailable_keeps_existing_fallback_behavior(shared_cycle, monkeypatch):
    import utils.panel_api.request_helper as requests

    r = shared_cycle
    assert await r.run()
    before = copy.deepcopy({name: vars(record) for name, record in r.warning.warnings.items()})
    monkeypatch.setattr(requests, "is_panel_available", lambda: False)
    for _ in range(2):
        r.advance()
        assert not await r.run()
    assert {name: vars(record) for name, record in r.warning.warnings.items()} == before
    assert api.get_last_cycle_stats()["fallback_applied"]


@pytest.mark.parametrize("partial_cycle", ["ip", "device"], indirect=True)
async def test_partial_cdn_overlap_cannot_create_a_false_warning(partial_cycle):
    r = partial_cycle
    r.config["cdn_nodes"] = [2]
    user = r.observed("alice")
    # Ordinary and CDN observations appear independent in the partial sample,
    # but the missing node might link both to the same ordinary device.
    user.device_info.connections[1].ip = "203.0.113.20"
    user.ip.append("203.0.113.20")
    retained = r.warning("alice", expired=True, count=3)
    await r.run([user])
    assert r.applied == []
    assert r.system.warnings["alice"] is retained
    assert retained.consecutive_violations == 3
    assert get_last_completed_scan().coverage_complete is False


async def test_recheck_retains_healthy_nodes_after_one_node_disconnects(shared_cycle, monkeypatch):
    import utils.panel_api.request_helper as requests
    from utils.shared_state import pop_api_users_snapshot

    r = shared_cycle
    assert await api.collect_active_users_from_api(r.panel, r.config)
    sample = await pop_api_users_snapshot()
    original = requests.panel_request

    async def changed(*args, **kwargs):
        response = await original(*args, **kwargs)
        r.nodes[1].status = "error"
        return response

    monkeypatch.setattr(requests, "panel_request", changed)
    refreshed = await api.recheck_api_user(r.panel, "alice", r.config, sample.api_evidence)
    assert refreshed is not None
    user, window, partial = refreshed
    assert set(user.ip) == {"5.6.7.8", "9.9.9.9"}
    assert {c.node_id for c in user.device_info.connections} == {31}
    assert partial


@pytest.mark.parametrize("partial_cycle", ["ip", "device"], indirect=True)
@pytest.mark.parametrize("fault", ["disconnect", "timestamp"])
async def test_node_fault_during_report_keeps_other_nodes_actionable(partial_cycle, monkeypatch, fault):
    import utils.check_usage as usage
    from utils.log_observations import LOG_OBSERVATIONS
    from utils.types import ConnectionInfo

    r = partial_cycle
    retained = r.warning("bob", expired=True, count=3)
    user = r.observed("alice", count=1)
    user.device_info.connections = [
        ConnectionInfo(ip, 2, "healthy-2", "inbound-2", r.clock[0] - 1) for ip in ("198.51.100.10", "203.0.113.20")
    ]
    user.ip = [c.ip for c in user.device_info.connections]
    original = usage.check_ip_used

    async def report(*args, **kwargs):
        result = await original(*args, **kwargs)
        if fault == "disconnect":
            fleet = r.fleet()
            fleet[0].status = "error"
            LOG_OBSERVATIONS.register_fleet(fleet)
        else:
            LOG_OBSERVATIONS.mark_uncertain(r.clock[0] + 60, node_id=1, reason="future_timestamp")
        return result

    monkeypatch.setattr(usage, "check_ip_used", report)
    await r.run([user, r.observed("bob", count=1)])
    assert r.system.warnings["alice"].consecutive_violations == 1
    assert r.system.warnings["bob"] is retained
    assert retained.consecutive_violations == 3
    assert r.applied == []
    assert get_last_completed_scan().eligible_node_ids == frozenset({2})


@pytest.mark.parametrize("expired", [False, True])
@pytest.mark.parametrize("reply", ["timeout", "malformed", "no_nodes", "null", "undatable", "future", "node_down"])
async def test_skipped_user_keeps_entire_warning_record(observed_cycle, expired, reply):
    r = observed_cycle
    r.config.update(api_ip_min_coverage=0.95, api_ip_auto_fallback=True)
    assert await r.run()
    record = r.warning.warnings["alice"]
    if expired:
        record.monitoring_end_time = r.clock[0] - 1
    before = copy.deepcopy(vars(record))
    r.clock[0] += 240
    r.advance()
    r.bodies["alice"] = {
        "timeout": None,
        "malformed": {},
        "no_nodes": {"nodes": {}},
        "null": {"nodes": {"31": None}},
        "undatable": {"nodes": {"31": {"ips": {"5.6.7.8": 1}}}},
        "future": {"nodes": {"31": {"ips": {"5.6.7.8": int(r.clock[0]) + 3600}}}},
        "node_down": {"nodes": {}},
    }[reply]
    if reply == "node_down":
        r.nodes[0].status = "error"
    assert await r.run()
    assert r.warning.warnings["alice"] is record
    assert vars(record) == before
    assert r.warning.warnings["bob"].consecutive_violations == 2
    assert api.get_last_cycle_stats()["coverage"] == 0.5


@pytest.mark.parametrize("mode", ["ip", "device", "node", "inbound"])
@pytest.mark.parametrize("grouping", ["plain", "subnet", "high_trust"])
def test_partial_count_never_exceeds_any_completed_sample(mode, grouping):
    from dataclasses import replace
    from itertools import combinations
    from utils.device_count import DeviceCountingConfig, count_devices
    from utils.types import ConnectionInfo, UserType

    config = DeviceCountingConfig(
        count_mode=mode,
        cdn_nodes=[2],
        cdn_inbounds=["cdn-a", "cdn-b"],
        eligible_node_ids={1, 2, 3},
        subnet_ip_grouping=grouping == "subnet",
        high_trust_ip_grouping=grouping == "high_trust",
    )
    observations = [
        ConnectionInfo(ip, node, f"node-{node}", inbound, 1_800_000_000)
        for ip, node, inbound in (
            ("198.51.100.1", 1, "direct"),
            ("198.51.100.2", 1, "direct"),
            ("203.0.113.1", 2, "cdn-a"),
            ("203.0.113.1", 3, "cdn-a"),
            ("203.0.113.2", 3, "cdn-b"),
            ("203.0.113.1", 1, "cdn-a"),
            ("203.0.113.2", 1, "direct"),
        )
    ]
    full = UserType(name="synthetic", ip=[])
    full.device_info.connections = observations
    exact = count_devices(full, config, trust_score=100)
    for size in range(len(observations) + 1):
        for subset in combinations(observations, size):
            partial = UserType(name="synthetic", ip=[])
            partial.device_info.connections = list(subset)
            assert count_devices(partial, replace(config, partial_evidence=True), trust_score=100) <= exact


async def test_malformed_http_body_lowers_lookup_success_percentage(observed_cycle, caplog):
    r = observed_cycle
    r.bodies["alice"] = {}
    assert await r.run()
    progress = [record.getMessage() for record in caplog.records if "API IP requests: 2/2" in record.getMessage()]
    assert len(progress) == 1
    assert "success 50.0% (1 ok)" in progress[0]
    assert "1 invalid bodies" in progress[0]
    assert api.get_last_cycle_stats()["fetched_ok"] == 2
    assert api.get_last_cycle_stats()["invalid_responses"] == 1
    assert api.get_last_cycle_stats()["http_coverage"] == 1
    assert api.get_last_cycle_stats()["coverage"] == 0.5
