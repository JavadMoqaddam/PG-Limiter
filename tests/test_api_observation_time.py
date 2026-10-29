"""API snapshots describe each user's request, not the end of a fleet scan."""

import copy
from types import SimpleNamespace

import pytest

from test_api_evidence import api_cycle  # noqa: F401
from test_api_user_node_scope import two_groups  # noqa: F401
from test_api_per_ip_audit import per_ip_cycle  # noqa: F401


@pytest.fixture
async def observed_cycle(two_groups, monkeypatch):
    import utils.api_observations as observations
    import utils.ip_source_api as api
    import utils.panel_api.request_helper as requests

    r = two_groups
    monkeypatch.setattr(observations, "monotonic_time", lambda: r.clock[0])
    monkeypatch.setattr(api, "monotonic_time", lambda: r.clock[0])
    r.config.update(api_ip_freshness=20, check_interval=300, api_ip_concurrency=1)
    for node in r.nodes:
        node.core_version = "26.10.21"
    r.lookups = []
    original_get = requests.panel_get

    async def get(panel, path, **kwargs):
        if path.startswith("/api/user/"):
            name = path.removeprefix("/api/user/")
            body = {
                "id": list(r.bodies).index(name) + 1,
                "username": name,
                "group_ids": r.memberships[name],
                "status": "active",
            }
            return SimpleNamespace(status_code=200, json=lambda: body)
        return await original_get(panel, path, **kwargs)

    async def request(_panel, method, path, **kwargs):
        assert method == "GET"
        name = list(r.bodies)[int(path.split("/")[-2]) - 1]
        r.lookups.append(name)
        body = copy.deepcopy(r.bodies[name])
        if body is None:
            return None, "unavailable"
        return SimpleNamespace(status_code=200, json=lambda: body), None

    monkeypatch.setattr(requests, "panel_get", get)
    monkeypatch.setattr(requests, "panel_request", request)
    return r


async def test_open_xray_connections_with_old_start_timestamps_are_counted(observed_cycle):
    r = observed_cycle
    r.bodies["alice"]["nodes"]["31"]["ips"] = {"5.6.7.8": r.now - 3600, "9.9.9.9": r.now - 7200}
    assert await r.run()
    assert set(r.warning.warnings) == {"alice", "bob"}
    assert r.warning.warnings["alice"].consecutive_violations == 1


async def test_waiting_for_other_users_does_not_expire_the_first_response(observed_cycle, monkeypatch):
    import utils.panel_api.request_helper as requests

    r = observed_cycle
    original = requests.panel_request

    async def delayed(*args, **kwargs):
        if args[2].endswith("/2/ip"):
            r.clock[0] += 300
        return await original(*args, **kwargs)

    monkeypatch.setattr(requests, "panel_request", delayed)
    assert await r.run()
    assert set(r.warning.warnings) == {"alice", "bob"}


async def test_slow_warning_persistence_keeps_the_other_users_observation(observed_cycle, monkeypatch):
    r = observed_cycle
    original = r.warning.add_warning

    async def slow(*args, **kwargs):
        result = await original(*args, **kwargs)
        r.clock[0] += 300
        return result

    monkeypatch.setattr(r.warning, "add_warning", slow)
    assert await r.run()
    assert set(r.warning.warnings) == {"alice", "bob"}


@pytest.mark.parametrize("version", [None, "25.12.8", "unknown"])
async def test_unverified_xray_versions_do_not_promote_old_timestamps(observed_cycle, version):
    r = observed_cycle
    r.nodes[0].core_version = version
    r.bodies["alice"]["nodes"]["31"]["ips"] = {"5.6.7.8": r.now - 3600, "9.9.9.9": r.now - 7200}
    assert await r.run()
    assert set(r.warning.warnings) == {"bob"}


@pytest.mark.parametrize("age, expected", [(19, {"alice", "bob"}), (20, {"bob"})])
async def test_wireguard_activity_age_is_evaluated_at_its_own_request(observed_cycle, monkeypatch, age, expected):
    import utils.check_usage as usage

    r = observed_cycle
    r.cores[0].update(type="wg", config={"interface_name": "ordinary"})
    r.bodies["alice"]["nodes"]["31"]["ips"] = {"5.6.7.8": r.now - age, "9.9.9.9": r.now - age}
    original = usage.read_config_snapshot

    async def delayed(*args, **kwargs):
        result = await original(*args, **kwargs)
        r.clock[0] += 300
        return result

    monkeypatch.setattr(usage, "read_config_snapshot", delayed)
    assert await r.run()
    assert set(r.warning.warnings) == expected


@pytest.mark.parametrize(
    "reply, applied",
    [("over", ["alice"]), ("normal", []), ("incomplete", []), ("partial_over", ["alice"]), ("partial_normal", [])],
)
@pytest.mark.parametrize("expired", [False, True])
async def test_delayed_punishment_checks_a_new_response_without_resetting_unknown_warning(
    observed_cycle, monkeypatch, reply, applied, expired
):
    import utils.check_usage as usage
    import utils.warning_system as package
    import utils.warning_system.enhanced_system as warnings
    from utils.enforcement_guard import EnforcementProtected, start_enforcement_effect, validate_enforcement

    r = observed_cycle
    if reply.startswith("partial_"):
        r.nodes.append(
            SimpleNamespace(
                node_id=32,
                node_name="ordinary-2",
                node_ip="192.0.2.32",
                status="connected",
                core_config_id=1,
                core_version="26.10.21",
            )
        )
        r.bodies["alice"]["nodes"]["32"] = {"ips": {}}
    r.config["max_warning_count"] = 3
    assert await r.run()
    record = r.warning.warnings["alice"]
    r.clock[0] += 240
    r.advance()
    r.config["max_warning_count"] = 3
    record.consecutive_violations = 3 if expired else 2
    if expired:
        record.monitoring_end_time = r.clock[0] - 1
    r.bodies["bob"] = {"nodes": {"24": {"ips": {"5.6.7.8": int(r.clock[0])}}}}
    r.lookups.clear()
    original = usage.read_config_snapshot
    delayed_once = False

    async def delayed(*args, **kwargs):
        nonlocal delayed_once
        result = await original(*args, **kwargs)
        if not delayed_once:
            delayed_once = True
            r.clock[0] += 300
            ips = {"5.6.7.8": int(r.clock[0]), "9.9.9.9": int(r.clock[0])}
            if reply in ("normal", "partial_normal"):
                ips.pop("9.9.9.9")
            r.bodies["alice"] = {"nodes": {"31": {"ips": ips}}} if reply != "incomplete" else {"nodes": {}}
        return result

    effects = []

    async def punish(_panel, user):
        try:
            await validate_enforcement()
            start_enforcement_effect()
        except EnforcementProtected:
            return {"protected": True}
        effects.append(user.name)
        return {"action": "warning", "violation_count": 1}

    monkeypatch.setattr(usage, "read_config_snapshot", delayed)
    monkeypatch.setattr(package, "safe_disable_user_with_punishment", punish)
    monkeypatch.setattr(warnings, "safe_disable_user_with_punishment", punish)
    assert await r.run()
    assert effects == applied
    assert r.lookups.count("alice") == 2
    assert r.lookups.count("bob") == 1
    if reply in ("incomplete", "partial_normal"):
        assert r.warning.warnings["alice"] is record
        assert record.consecutive_violations == 3


async def test_clock_rollback_cannot_turn_empty_maps_into_warning_clearing(observed_cycle, monkeypatch):
    import utils.check_usage as usage

    r = observed_cycle
    assert await r.run()
    before = dict(r.warning.warnings)
    r.advance()
    r.bodies["alice"] = {"nodes": {"31": {"ips": {}}}}
    r.bodies["bob"] = {"nodes": {"24": {"ips": {}}}}
    original = usage.read_config_snapshot

    async def rollback(*args, **kwargs):
        result = await original(*args, **kwargs)
        r.clock[0] -= 1
        return result

    monkeypatch.setattr(usage, "read_config_snapshot", rollback)
    assert await r.run()
    assert r.warning.warnings == before


async def test_request_queue_wait_is_not_part_of_the_next_users_activity_window(observed_cycle, monkeypatch):
    import utils.panel_api.request_helper as requests

    r = observed_cycle
    r.cores[1].update(type="wg", config={"interface_name": "premium"})
    r.bodies["bob"]["nodes"]["24"]["ips"] = {"5.6.7.8": r.now - 15, "9.9.9.9": r.now - 15}
    original = requests.panel_request

    async def delayed(*args, **kwargs):
        response = await original(*args, **kwargs)
        if args[2].endswith("/1/ip"):
            r.clock[0] += 5
        return response

    monkeypatch.setattr(requests, "panel_request", delayed)
    assert await r.run()
    assert set(r.warning.warnings) == {"alice"}


@pytest.mark.parametrize("change", ["membership", "node"])
async def test_punishment_recheck_confirms_scope_after_receiving_ips(observed_cycle, monkeypatch, change):
    import utils.ip_source_api as api
    import utils.panel_api.request_helper as requests
    from utils.shared_state import pop_api_users_snapshot

    r = observed_cycle
    assert await api.collect_active_users_from_api(r.panel, r.config)
    sample = await pop_api_users_snapshot()
    original = requests.panel_request

    async def changed(*args, **kwargs):
        response = await original(*args, **kwargs)
        if change == "membership":
            r.memberships["alice"] = [2]
        else:
            r.nodes[0].status = "error"
        return response

    monkeypatch.setattr(requests, "panel_request", changed)
    assert await api.recheck_api_user(r.panel, "alice", r.config, sample.api_evidence) is None


@pytest.mark.parametrize("second_ip, count", [("5.6.7.9", 0), ("5.6.8.9", 2)])
async def test_live_map_uses_real_node_adapter_and_preserves_per_ip_subnet_counting(per_ip_cycle, second_ip, count):
    r = per_ip_cycle
    for row in r.fleet:
        row["xray_version"] = "26.10.21"
    r.config.update(subnet_ip_grouping=True, subnet_grouping_mode="/24")
    for name in r.bodies:
        r.bodies[name] = r.body(("5.6.7.8", second_ip), age=3600)
    assert await r.run()
    assert len(r.warning.warnings) == count
