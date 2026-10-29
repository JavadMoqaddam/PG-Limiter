"""Group-specific node evidence must isolate failures between user populations."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
import time

import pytest

from test_api_evidence import api_cycle  # noqa: F401


@pytest.fixture
async def two_groups(api_cycle, monkeypatch):
    import utils.panel_api.nodes as nodes
    import utils.panel_api.request_helper as requests

    r = api_cycle
    r.clock = [float(r.now)]
    monkeypatch.setattr(time, "time", lambda: r.clock[0])
    r.config.update(device_count_mode="ip", max_warning_count=100)
    r.memberships = {"alice": [1], "bob": [2]}
    r.groups = [
        {"id": 1, "name": "ordinary", "inbound_tags": ["ordinary"], "is_disabled": False},
        {"id": 2, "name": "premium", "inbound_tags": ["premium"], "is_disabled": False},
    ]
    r.cores = [
        {
            "id": 1,
            "type": "xray",
            "config": {"inbounds": [{"tag": "ordinary", "protocol": "vless"}]},
            "exclude_inbound_tags": [],
            "fallbacks_inbound_tags": [],
        },
        {
            "id": 2,
            "type": "xray",
            "config": {"inbounds": [{"tag": "premium", "protocol": "vless"}]},
            "exclude_inbound_tags": [],
            "fallbacks_inbound_tags": [],
        },
    ]
    r.nodes = [
        SimpleNamespace(node_id=31, node_name="ordinary", node_ip="192.0.2.31", status="connected", core_config_id=1),
        SimpleNamespace(node_id=24, node_name="pm", node_ip="192.0.2.24", status="connected", core_config_id=2),
    ]
    r.bodies["alice"] = {"nodes": {"31": {"ips": {"5.6.7.8": r.now, "9.9.9.9": r.now}}}}
    r.bodies["bob"] = {"nodes": {"24": {"ips": {"5.6.7.8": r.now, "9.9.9.9": r.now}}}}
    r.paths = []
    r.hidden = set()
    r.before_response = lambda path: None

    def advance():
        r.clock[0] += 60
        for body in r.bodies.values():
            if body:
                for value in body.get("nodes", {}).values():
                    if value:
                        value["ips"] = {ip: int(r.clock[0]) for ip in value.get("ips", {})}

    r.advance = advance

    async def get(_panel, path, **kwargs):
        r.paths.append(path)
        r.before_response(path)
        if path == "/api/groups":
            body = {"groups": r.groups, "total": len(r.groups)}
        elif path == "/api/cores":
            body = {"cores": r.cores, "count": len(r.cores)}
        else:
            assert path == "/api/users"
            body = {
                "users": [
                    {"id": i, "username": n, "group_ids": r.memberships[n]}
                    for i, n in enumerate(r.bodies, 1)
                    if n not in r.hidden
                ],
                "total": len(r.bodies) - len(r.hidden),
            }
        return SimpleNamespace(status_code=200, json=lambda: body)

    monkeypatch.setattr(nodes, "get_nodes", AsyncMock(side_effect=lambda *a, **kw: r.nodes))
    monkeypatch.setattr(requests, "panel_get", get)
    return r


async def test_two_disjoint_groups_are_monitored_with_all_nodes_included(two_groups):
    r = two_groups
    assert await r.run() is True
    assert set(r.warning.warnings) == {"alice", "bob"}
    assert {w.consecutive_violations for w in r.warning.warnings.values()} == {1}


async def test_disconnected_node_only_protects_its_users_and_does_not_trigger_fallback(two_groups):
    r = two_groups
    assert await r.run() is True
    bob = r.warning.warnings["bob"]
    r.nodes[1].status = "error"
    r.bodies["bob"] = {"nodes": {}}
    r.config["max_warning_count"] = 100
    r.config["api_ip_auto_fallback"] = True
    for expected in (2, 3):
        r.advance()
        assert await r.run() is True
        assert r.warning.warnings["alice"].consecutive_violations == expected
        assert r.warning.warnings["bob"] is bob
        assert bob.consecutive_violations == 1
        assert r.config["ip_source"] == "api"


async def test_multigroup_user_requires_the_union_of_both_node_sets(two_groups):
    r = two_groups
    r.memberships["alice"] = [1, 2]
    assert await r.run() is True
    assert set(r.warning.warnings) == {"bob"}
    r.bodies["alice"]["nodes"]["24"] = {"ips": {}}
    assert await r.run() is True
    assert set(r.warning.warnings) == {"alice", "bob"}


@pytest.mark.parametrize("bad", [{"nodes": {}}, {"nodes": {"24": None}}, None])
async def test_incomplete_user_is_skipped_without_resetting_its_warning(two_groups, bad):
    from utils.ip_source_api import get_last_cycle_stats

    r = two_groups
    assert await r.run()
    bob = r.warning.warnings["bob"]
    trust = bob.trust_score
    r.bodies["bob"] = bad
    r.config["api_ip_min_coverage"] = 0
    r.config["api_ip_auto_fallback"] = True
    for expected in (2, 3):
        r.advance()
        assert await r.run()
        assert r.warning.warnings["alice"].consecutive_violations == expected
        assert r.warning.warnings["bob"] is bob
        assert bob.consecutive_violations == 1
        assert bob.trust_score == trust
        assert get_last_cycle_stats()["consecutive_unsuccessful_cycles"] == 0
    r.advance()
    r.bodies["bob"] = {"nodes": {"24": {"ips": {}}}}
    assert await r.run()
    assert "bob" not in r.warning.warnings


async def test_unavailable_node_does_not_stop_other_nodes_of_the_same_group(two_groups):
    r = two_groups
    r.nodes.append(
        SimpleNamespace(node_id=32, node_name="ordinary-2", node_ip="192.0.2.32", status="error", core_config_id=1)
    )
    assert await r.run()
    assert set(r.warning.warnings) == {"alice", "bob"}


async def test_absent_pending_warning_is_preserved_during_node_outage(two_groups):
    r = two_groups
    assert await r.run()
    bob = r.warning.warnings["bob"]
    r.nodes[1].status = "error"
    r.hidden.add("bob")
    r.advance()
    assert await r.run()
    assert r.warning.warnings["bob"] is bob
    assert bob.consecutive_violations == 1
    assert r.warning.warnings["alice"].consecutive_violations == 2


@pytest.mark.parametrize("change", ["membership", "group", "core", "node"])
async def test_changed_scope_only_retires_the_affected_user(two_groups, change):
    r = two_groups
    assert await r.run()
    bob = r.warning.warnings["bob"]
    r.advance()
    r.paths.clear()

    def mutate(path):
        if path == "/api/groups" and r.paths.count(path) == 2:
            if change == "membership":
                r.memberships["bob"] = [1]
            elif change == "group":
                r.groups[1]["inbound_tags"] = ["ordinary"]
            elif change == "core":
                r.cores[1]["config"]["inbounds"][0]["tag"] = "replacement"
            else:
                r.nodes[1].status = "error"

    r.before_response = mutate
    assert await r.run()
    assert r.warning.warnings["bob"] is bob
    assert bob.consecutive_violations == 1
    assert r.warning.warnings["alice"].consecutive_violations == 2


@pytest.mark.parametrize("groups", [None, [True], ["1", 2], [999]])
async def test_unknown_membership_only_skips_that_user(two_groups, groups):
    r = two_groups
    r.memberships["alice"] = groups
    assert await r.run()
    assert set(r.warning.warnings) == {"bob"}


async def test_empty_candidate_list_cannot_clear_warnings_when_node_fails_during_query(two_groups):
    r = two_groups
    assert await r.run()
    before = dict(r.warning.warnings)
    r.hidden = {"alice", "bob"}
    r.advance()

    def fail_node(path):
        if path == "/api/users":
            r.nodes[1].status = "error"

    r.before_response = fail_node
    assert await r.run()
    assert r.warning.warnings == before


async def test_topology_changed_during_ip_admission_retires_only_affected_user(two_groups, monkeypatch):
    import utils.ip_source_api as api

    r = two_groups
    assert await r.run()
    bob = r.warning.warnings["bob"]
    r.advance()
    real_validate = api._validate_ips

    async def validate(ips, config):
        result = await real_validate(ips, config)
        r.groups[1]["inbound_tags"] = ["ordinary"]
        return result

    monkeypatch.setattr(api, "_validate_ips", validate)
    assert await r.run()
    assert r.warning.warnings["bob"] is bob
    assert bob.consecutive_violations == 1
    assert r.warning.warnings["alice"].consecutive_violations == 2


async def test_expired_incomplete_warning_retains_history_until_complete_evidence(two_groups):
    from copy import deepcopy

    r = two_groups
    assert await r.run()
    bob = r.warning.warnings["bob"]
    bob.monitoring_end_time -= 1000
    history = deepcopy(bob.monitoring_history)
    r.advance()
    r.bodies["bob"] = {"nodes": {}}
    assert await r.run()
    assert r.warning.warnings["bob"] is bob
    assert bob.consecutive_violations == 1
    assert bob.monitoring_history == history


@pytest.mark.parametrize("status", [403, 500])
async def test_shared_topology_permission_or_server_failure_preserves_warnings(two_groups, monkeypatch, status):
    import utils.panel_api.request_helper as requests

    r = two_groups
    assert await r.run()
    before = dict(r.warning.warnings)
    original = requests.panel_get

    async def get(panel, path, **kwargs):
        if path == "/api/cores":
            return SimpleNamespace(status_code=status, json=lambda: {"detail": "failed"})
        return await original(panel, path, **kwargs)

    monkeypatch.setattr(requests, "panel_get", get)
    r.advance()
    assert await r.run() is False
    assert r.warning.warnings == before
    assert {w.consecutive_violations for w in before.values()} == {1}
