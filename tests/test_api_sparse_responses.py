"""Successful API responses are counted without requiring every eligible node."""

from types import SimpleNamespace

import pytest

from test_api_evidence import api_cycle  # noqa: F401
from test_api_user_node_scope import two_groups  # noqa: F401


@pytest.mark.parametrize("mode", ["ip", "device", "node"])
async def test_missing_map_does_not_discard_observed_violation(two_groups, mode):
    from utils.ip_source_api import get_last_cycle_stats

    r = two_groups
    r.config["device_count_mode"] = mode
    r.memberships["alice"] = [1, 2]
    r.nodes.append(
        SimpleNamespace(node_id=32, node_name="ordinary-2", node_ip="192.0.2.32", status="connected", core_config_id=1)
    )
    r.bodies["alice"]["nodes"]["32"] = {"ips": {"9.9.9.9": r.now}}
    for count in (1, 2):
        assert await r.run()
        assert r.warning.warnings["alice"].consecutive_violations == count
        assert r.warning.warnings["alice"].ip_count == 2
        assert get_last_cycle_stats()["unknown_users"] == 0
        r.advance()


@pytest.mark.parametrize("reply", ["under", "empty", "missing", "stale"])
@pytest.mark.parametrize("expired", [False, True])
async def test_sparse_under_limit_response_clears_warning(two_groups, reply, expired):
    r = two_groups
    r.memberships["alice"] = [1, 2]
    r.bodies["alice"]["nodes"]["24"] = {"ips": {}}
    assert await r.run()
    record = r.warning.warnings["alice"]
    if expired:
        record.monitoring_end_time -= 1000
    r.advance()
    ips = {"5.6.7.8": int(r.clock[0])}
    if reply == "empty":
        ips = {}
    elif reply == "stale":
        ips = {"5.6.7.8": int(r.clock[0]) - 1000}
    r.bodies["alice"] = {"nodes": {} if reply == "missing" else {"31": {"ips": ips}}}
    assert await r.run()
    if reply == "missing":
        assert r.warning.warnings["alice"] is record
        assert record.consecutive_violations == 1
    else:
        assert "alice" not in r.warning.warnings


async def test_empty_aggregate_preserves_warnings_without_fallback(two_groups):
    from utils.ip_source_api import get_last_cycle_stats

    r = two_groups
    assert await r.run()
    assert set(r.warning.warnings) == {"alice", "bob"}
    r.config["api_ip_auto_fallback"] = True
    for _ in range(3):
        r.advance()
        for name in r.bodies:
            r.bodies[name] = {"nodes": {}}
        assert not await r.run()
        assert r.config["ip_source"] == "api"
        assert set(r.warning.warnings) == {"alice", "bob"}
        assert all(record.consecutive_violations == 1 for record in r.warning.warnings.values())
        assert get_last_cycle_stats()["consecutive_unsuccessful_cycles"] == 0


@pytest.mark.parametrize("mode", ["ip", "device"])
@pytest.mark.parametrize("direct_ips, expected", [(["5.6.7.8"], 2), (["5.6.7.8", "9.9.9.9"], 3)])
async def test_sparse_response_uses_normal_cdn_counting(two_groups, mode, direct_ips, expected):
    r = two_groups
    r.config.update(device_count_mode=mode, cdn_nodes=[24, 32])
    r.memberships["alice"] = [1, 2]
    r.nodes.append(
        SimpleNamespace(node_id=32, node_name="ordinary-2", node_ip="192.0.2.32", status="connected", core_config_id=1)
    )
    r.bodies["alice"] = {
        "nodes": {
            "31": {"ips": {ip: r.now for ip in direct_ips}},
            "24": {"ips": {"11.12.13.14": r.now}},
        }
    }
    assert await r.run()
    assert r.warning.warnings["alice"].ip_count == expected
    r.advance()
    r.bodies["alice"]["nodes"]["32"] = {"ips": {}}
    assert await r.run()
    assert "alice" in r.warning.warnings


@pytest.mark.parametrize("bad", [None, {"ips": {"5.6.7.8": 1}}, "future"])
async def test_sparse_maps_do_not_relax_invalid_returned_evidence(two_groups, bad):
    r = two_groups
    r.memberships["alice"] = [1, 2]
    r.nodes.append(
        SimpleNamespace(node_id=32, node_name="ordinary-2", node_ip="192.0.2.32", status="connected", core_config_id=1)
    )
    if bad == "future":
        bad = {"ips": {"5.6.7.8": r.now + 120}}
    r.bodies["alice"]["nodes"]["32"] = bad
    assert await r.run()
    assert "alice" not in r.warning.warnings
