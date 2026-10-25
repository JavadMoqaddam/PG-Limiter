"""API transport, collector and warning integration checks for the Per IP audit."""

import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_api_evidence import api_cycle  # noqa: F401
from utils.panel_api.nodes import get_nodes as real_get_nodes


@pytest.fixture
async def per_ip_cycle(api_cycle, monkeypatch):
    """Reuse isolated real policies/warnings and exercise the real node adapter."""
    import db.database as database
    import utils.ip_facts as facts
    import utils.ip_source_api as api
    import utils.panel_api.nodes as nodes
    import utils.read_config as config
    from db.crud.config import ConfigCRUD

    cycle = api_cycle
    clock = [float(cycle.now)]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    blocked = set(facts.BLOCKED_IPS)
    facts.VERDICT_CACHE.clear()
    facts.COUNTRY_CACHE.clear()
    monkeypatch.setattr(api, "_consecutive_coverage_skips", 0)
    monkeypatch.setattr(api, "_coverage_alert_sent", False)
    monkeypatch.setattr(api, "_forbidden_alert_sent", False)
    monkeypatch.setattr(nodes, "_nodes_cache", dict(nodes=None, expires_at=0, panel_domain=None, enabled_only=None))
    monkeypatch.setattr(nodes, "get_nodes", real_get_nodes)
    cycle.fleet = [
        {"id": 1, "name": "direct-1", "address": "8.9.10.11", "status": "connected"},
        {"id": 2, "name": "direct-2", "address": "8.9.10.12", "status": "connected"},
    ]

    async def nodes_get(_panel, path):
        assert path == "/api/nodes?enabled=true"
        return SimpleNamespace(json=lambda: {"nodes": cycle.fleet})

    monkeypatch.setattr(nodes, "panel_get", nodes_get)
    async with database.get_db() as db:
        for key, value in {
            "ip_source": "api",
            "api_ip_freshness": "20",
            "check_interval": "300",
            "device_count_mode": "ip",
            "api_ip_min_coverage": "0",
            "api_ip_min_node_coverage": "0",
            "api_ip_auto_fallback": "false",
            "country_code": "",
        }.items():
            await ConfigCRUD.set(db, key, value)
    cycle.config.clear()
    cycle.config.update(await config.read_config(refresh=True))
    cycle.clock = clock

    async def settings(**values):
        for key, value in values.items():
            assert await config.save_config_value(key, value)
        cycle.config.clear()
        cycle.config.update(await config.read_config())

    def body(ips=("5.6.7.8", "5.6.7.9"), *, age=0):
        return {"nodes": {"1": {"ips": {ip: int(clock[0]) - age for ip in ips}}, "2": {"ips": {}}}}

    def advance():
        clock[0] += 300
        for name in cycle.bodies:
            cycle.bodies[name] = body()

    cycle.body = body
    cycle.advance = advance
    cycle.settings = settings
    for name in cycle.bodies:
        cycle.bodies[name] = body()
    yield cycle
    facts.BLOCKED_IPS.clear()
    facts.BLOCKED_IPS.update(blocked)
    facts.VERDICT_CACHE.clear()
    facts.COUNTRY_CACHE.clear()


@pytest.mark.parametrize("floor, all_failed, expected_unknown", [(0.8, False, 1), (0.0, True, 2)])
async def test_failed_lookup_diagnostics_retain_unknown_users_and_known_fleet(
    per_ip_cycle, floor, all_failed, expected_unknown
):
    """Early HTTP gates must not report failed candidates as zero unknown users."""
    from utils.ip_source_api import get_last_cycle_stats

    cycle = per_ip_cycle
    cycle.config["api_ip_min_coverage"] = floor
    cycle.bodies["alice"] = None
    if all_failed:
        cycle.bodies["bob"] = None

    assert await cycle.run() is False
    stats = get_last_cycle_stats()
    assert stats["fetched_failed"] == expected_unknown
    assert stats["unknown_users"] == expected_unknown
    assert stats["nodes_expected"] == 2


async def test_unexpected_reported_node_cannot_mask_a_missing_required_node(per_ip_cycle):
    """Only expected reporting IDs may establish fleet coverage or absent-node totals."""
    from utils.ip_source_api import format_api_evidence_summary, get_last_cycle_stats

    cycle = per_ip_cycle
    cycle.bodies["alice"] = {"nodes": {"1": {"ips": {}}, "3": {"ips": {}}}}
    cycle.bodies["bob"] = {"nodes": {"1": {"ips": {}}}}

    assert await cycle.run() is False
    stats = get_last_cycle_stats()
    assert stats["coverage"] == 1.0
    assert stats["missing_node_users"] == 2
    assert stats["missing_node_ids"] == (2,)
    assert stats["unknown_users"] == 2
    assert stats["nodes_expected"] == 2
    assert stats["nodes_seen"] == 1
    assert stats["node_coverage"] == 0.5
    summary = format_api_evidence_summary(stats)
    assert "Nodes reporting across responses: <code>1/2</code>" in summary
    assert "Nodes absent across all responses: <code>1</code>" in summary


async def test_conflicting_normalized_node_ids_cannot_clear_a_pending_warning(per_ip_cycle):
    """Normalizing two distinct response keys must not discard future uncertainty."""
    cycle = per_ip_cycle
    assert await cycle.run()
    record = cycle.warning.warnings["alice"]
    cycle.advance()
    cycle.bodies["alice"] = {
        "nodes": {
            "1": {"ips": {"5.6.7.8": int(cycle.clock[0]) + 1}},
            "01": {"ips": {}},
            "2": {"ips": {}},
        }
    }

    assert await cycle.run()
    assert cycle.warning.warnings.get("alice") is record
    assert record.consecutive_violations == 1


async def test_conflicting_normalized_ip_keys_cannot_clear_a_pending_warning(per_ip_cycle):
    """Trimming an alias must not overwrite a future timestamp with stale absence."""
    cycle = per_ip_cycle
    assert await cycle.run()
    record = cycle.warning.warnings["alice"]
    cycle.advance()
    cycle.bodies["alice"] = {
        "nodes": {
            "1": {"ips": {"5.6.7.8": int(cycle.clock[0]) + 1, " 5.6.7.8 ": int(cycle.clock[0]) - 20}},
            "2": {"ips": {}},
        }
    }

    assert await cycle.run()
    assert cycle.warning.warnings.get("alice") is record
    assert record.consecutive_violations == 1


@pytest.mark.parametrize("mode, warnings", [("ip", 0), ("node", 2)])
async def test_same_accepted_ip_on_two_nodes_counts_once_per_ip_and_twice_per_node(per_ip_cycle, mode, warnings):
    """Adding the node ID to Per IP identities would warn a one-address user."""
    cycle = per_ip_cycle
    await cycle.settings(device_count_mode=mode)
    for name in cycle.bodies:
        cycle.bodies[name] = {"nodes": {str(node): {"ips": {"5.6.7.8": int(cycle.clock[0])}} for node in (1, 2)}}

    assert await cycle.run()
    assert len(cycle.warning.warnings) == warnings
    if warnings:
        assert cycle.warning.warnings["alice"].ip_count == 2
        assert cycle.warning.warnings["alice"].ips == {"5.6.7.8"}


@pytest.mark.parametrize(
    "settings, ips, warning_count",
    [
        ({}, ("5.6.7.8", "5.6.7.9"), 2),
        ({"subnet_ip_grouping": "true"}, ("5.6.7.8", "5.6.7.9"), 0),
        (
            {"subnet_ip_grouping": "true", "subnet_grouping_mode": "/16"},
            ("5.6.7.8", "5.6.8.9"),
            2,
        ),
        ({"cdn_nodes": "1"}, ("5.6.7.8", "5.6.7.9"), 0),
        ({"cdn_inbounds": "real-ws-tag"}, ("5.6.7.8", "5.6.7.9"), 2),
    ],
)
async def test_per_ip_grouping_settings_control_real_api_warning_counts(per_ip_cycle, settings, ips, warning_count):
    """Grouping must be opt-in; absent API inbound tags cannot match log tags."""
    cycle = per_ip_cycle
    await cycle.settings(**settings)
    for name in cycle.bodies:
        cycle.bodies[name] = cycle.body(ips)

    assert await cycle.run()
    assert len(cycle.warning.warnings) == warning_count
    if warning_count:
        assert cycle.warning.warnings["alice"].ip_count == 2
        assert cycle.warning.warnings["alice"].ips == set(ips)


@pytest.mark.parametrize("mode, remains", [("ip", True), ("device", False)])
async def test_high_trust_grouping_keeps_per_ip_addresses_distinct(per_ip_cycle, mode, remains):
    """High Trust groups by inbound only in Per Device; API uses one sentinel."""
    cycle = per_ip_cycle
    assert await cycle.run()
    record = cycle.warning.warnings["alice"]
    record.trust_score = 100
    await cycle.settings(device_count_mode=mode, high_trust_ip_grouping="true", high_trust_threshold="20")
    cycle.advance()

    assert await cycle.run()
    assert ("alice" in cycle.warning.warnings) is remains
    if remains:
        assert record.ip_count == 2
        assert record.consecutive_violations == 2


async def test_registered_public_node_address_is_rejected_even_when_cached_as_client(per_ip_cycle):
    """Node registration must override an earlier accepted verdict for a relay IP."""
    from utils.ip_facts import remember_verdict

    cycle = per_ip_cycle
    remember_verdict("8.9.10.11", True)
    for name in cycle.bodies:
        cycle.bodies[name] = cycle.body(("8.9.10.11", "5.6.7.8"))

    assert await cycle.run()
    assert cycle.warning.warnings == {}


async def test_disabled_node_does_not_contribute_to_per_ip_count(per_ip_cycle):
    """A configured excluded node must not raise a one-address user above limit."""
    cycle = per_ip_cycle
    await cycle.settings(disabled_nodes="2")
    for name in cycle.bodies:
        cycle.bodies[name] = {
            "nodes": {
                "1": {"ips": {"5.6.7.8": int(cycle.clock[0])}},
                "2": {"ips": {"5.6.7.9": int(cycle.clock[0])}},
            }
        }

    assert await cycle.run()
    assert cycle.warning.warnings == {}


@pytest.mark.parametrize("age, warnings", [(19, 2), (20, 0)])
async def test_per_ip_source_freshness_twenty_seconds_is_independent_of_interval_300(per_ip_cycle, age, warnings):
    """Using the scan cadence or a sixty-second clamp would warn on age twenty."""
    cycle = per_ip_cycle
    for name in cycle.bodies:
        cycle.bodies[name] = cycle.body(age=age)

    assert await cycle.run()
    assert len(cycle.warning.warnings) == warnings
    if warnings:
        assert cycle.warning.warnings["alice"].ip_count == 2


@pytest.mark.parametrize("phase", ["admission", "snapshot", "report", "history"])
async def test_per_ip_observations_expiring_during_preparation_clear_only_known_complete_users(
    per_ip_cycle, monkeypatch, phase
):
    """A pre-await fresh sample cannot authorize a post-await stale warning."""
    import utils.check_usage as usage
    import utils.ip_source_api as api

    cycle = per_ip_cycle
    assert await cycle.run()
    cycle.advance()
    for name in cycle.bodies:
        cycle.bodies[name] = cycle.body(age=19)
    if phase == "admission":
        original = api._validate_ips

        async def delayed(*args, **kwargs):
            result = await original(*args, **kwargs)
            cycle.clock[0] += 1
            return result

        monkeypatch.setattr(api, "_validate_ips", delayed)
    elif phase in ("snapshot", "report"):
        attribute = "read_config_snapshot" if phase == "snapshot" else "check_ip_used"
        original = getattr(usage, attribute)

        async def delayed(*args, **kwargs):
            result = await original(*args, **kwargs)
            cycle.clock[0] += 1
            return result

        monkeypatch.setattr(usage, attribute, delayed)
    else:
        original = usage.ip_history_tracker.record_many

        async def delayed(*args, **kwargs):
            result = await original(*args, **kwargs)
            cycle.clock[0] += 1
            return result

        monkeypatch.setattr(usage.ip_history_tracker, "record_many", delayed)

    assert await cycle.run()
    assert cycle.warning.warnings == {}


@pytest.mark.parametrize("kind", ["http_failure", "missing_node", "null_node", "invalid_timestamp", "future"])
@pytest.mark.parametrize("expired", [False, True])
async def test_unknown_per_ip_user_preserves_persisted_warning_while_complete_user_clears(
    per_ip_cycle, monkeypatch, kind, expired
):
    """Incomplete evidence must protect scan, persistent and cleanup mutations."""
    import utils.warning_system as package
    import utils.warning_system.enhanced_system as warnings

    cycle = per_ip_cycle
    assert await cycle.run()
    record = cycle.warning.warnings["alice"]
    cycle.advance()
    if expired:
        record.monitoring_end_time = cycle.clock[0] - 1
        record.consecutive_violations = 3
    await cycle.warning.save_warnings()
    before = json.loads(Path(cycle.warning.filename).read_text())["alice"]
    cycle.bodies["bob"] = cycle.body(("5.6.7.8",))
    if kind == "http_failure":
        cycle.bodies["alice"] = None
    elif kind == "missing_node":
        cycle.bodies["alice"] = {"nodes": {"1": {"ips": {}}}}
    elif kind == "null_node":
        cycle.bodies["alice"] = {"nodes": {"1": None, "2": {"ips": {}}}}
    elif kind == "invalid_timestamp":
        cycle.bodies["alice"] = cycle.body()
        cycle.bodies["alice"]["nodes"]["1"]["ips"]["5.6.7.8"] = True
    else:
        cycle.bodies["alice"] = cycle.body(age=-1)
    applied = []

    async def punish(_panel, user):
        applied.append(user.name)
        return {"action": "warning", "violation_count": 1}

    monkeypatch.setattr(package, "safe_disable_user_with_punishment", punish)
    monkeypatch.setattr(warnings, "safe_disable_user_with_punishment", punish)

    assert await cycle.run()
    assert applied == []
    assert cycle.warning.warnings == {"alice": record}
    assert json.loads(Path(cycle.warning.filename).read_text())["alice"] == before


@pytest.mark.parametrize("phase", ["collection", "snapshot", "history", "policy"])
@pytest.mark.parametrize("setting, value", [("api_ip_freshness", "21"), ("ip_source", "logs")])
async def test_settings_epoch_change_retires_api_per_ip_evidence_without_warning_mutation(
    per_ip_cycle, monkeypatch, phase, setting, value
):
    """A same-source settings write and a source switch both retire old evidence."""
    import utils.check_usage as usage
    import utils.panel_api.request_helper as requests
    import utils.read_config as config
    import utils.settings_service as policies
    from utils.shared_state import ACTIVE_USERS

    cycle = per_ip_cycle
    assert await cycle.run()
    cycle.advance()
    expected_consecutive = {"alice": 1, "bob": 1}
    if phase == "policy":
        record = cycle.warning.warnings["alice"]
        record.monitoring_end_time = cycle.clock[0] - 1
        record.consecutive_violations = 3
        expected_consecutive["alice"] = 3
        await cycle.warning.save_warnings()
    before = json.loads(Path(cycle.warning.filename).read_text())
    if phase == "collection":
        module, attribute = requests, "panel_request"
    elif phase == "snapshot":
        module, attribute = usage, "read_config_snapshot"
    elif phase == "history":
        module, attribute = usage.ip_history_tracker, "record_many"
    else:
        module, attribute = policies, "read_user_policy"
    original = getattr(module, attribute)
    changed = False

    async def change_settings(*args, **kwargs):
        nonlocal changed
        result = await original(*args, **kwargs)
        if not changed:
            changed = True
            assert await config.save_config_value(setting, value)
        return result

    monkeypatch.setattr(module, attribute, change_settings)

    assert await cycle.run() is (phase != "collection")
    assert changed
    assert json.loads(Path(cycle.warning.filename).read_text()) == before
    assert {
        name: record.consecutive_violations for name, record in cycle.warning.warnings.items()
    } == expected_consecutive
    assert ACTIVE_USERS == {}


@pytest.mark.parametrize("expired", [False, True])
async def test_per_ip_expiry_at_final_enforcement_boundary_preserves_incident(per_ip_cycle, monkeypatch, expired):
    """A final refresh must compare the recounted IPs before starting punishment."""
    import utils.warning_system as package
    import utils.warning_system.enhanced_system as warnings
    from utils.enforcement_guard import EnforcementProtected, start_enforcement_effect, validate_enforcement

    cycle = per_ip_cycle
    assert await cycle.run()
    record = cycle.warning.warnings["alice"]
    cycle.advance()
    record.consecutive_violations = 3 if expired else 2
    if expired:
        record.monitoring_end_time = cycle.clock[0] - 1
    cycle.bodies["bob"] = cycle.body(("5.6.7.8",))
    cycle.bodies["alice"] = cycle.body(age=19)
    applied = []

    async def prepared_punishment(_panel, user):
        await validate_enforcement()
        cycle.clock[0] += 1
        try:
            start_enforcement_effect()
        except EnforcementProtected:
            return {"protected": True}
        applied.append(user.name)
        return {"action": "warning", "violation_count": 1}

    monkeypatch.setattr(package, "safe_disable_user_with_punishment", prepared_punishment)
    monkeypatch.setattr(warnings, "safe_disable_user_with_punishment", prepared_punishment)

    assert await cycle.run()
    assert applied == []
    assert cycle.warning.warnings.get("alice") is record
    assert record.consecutive_violations == 3


@pytest.mark.parametrize("node_key", [0, "0", "1", 1])
def test_single_valid_node_key_and_trimmed_ip_remain_usable(node_key):
    """Rejecting collisions must preserve standalone integer/string IDs and IP trimming."""
    from utils.panel_api.online_ips import _parse_ip_payload

    stamp = 1_800_000_000
    assert _parse_ip_payload({"nodes": {node_key: {"ips": {" 5.6.7.8 ": stamp}}}}) == {
        int(node_key): {"5.6.7.8": stamp}
    }
