#!/usr/bin/env python3
"""
Tests for the API-based IP source.

Two things are covered: the adapter that turns panel online-stats payloads into
the same ACTIVE_USERS structure log mode produces, and the fail-safe rules that
decide whether a cycle is trustworthy enough for enforcement to run at all.
"""

import time

import pytest


def fresh() -> int:
    """
    A last-seen timestamp that passes the freshness filter.

    The panel reports each IP's last-seen Unix epoch. Values below
    ``STALE_EPOCH_FLOOR`` cannot be timestamps, so their age is unknown and they are
    not counted as connected - a payload literal like ``3`` therefore tests the
    rejection path, not the happy path.
    """
    return int(time.time())


@pytest.fixture
def panel():
    """Panel credentials; every network call is patched out."""
    from utils.types import PanelType

    return PanelType("admin", "pass", "panel.local")


@pytest.fixture
def api_config():
    """Minimal API-mode configuration."""
    return {
        "ip_source": "api",
        "check_interval": 240,
        "api_ip_concurrency": 5,
        "api_ip_candidate_mode": "online",
        "api_ip_online_window": 0,
        "api_ip_page_size": 500,
        "api_ip_timeout": 8.0,
        "api_ip_sentinel_inbound": "API",
        "api_ip_min_coverage": 0.8,
        # Off so a dead cycle never writes to the config database from a test
        "api_ip_auto_fallback": False,
        "country_code": "",
        "disabled_nodes": [],
    }


@pytest.fixture(autouse=True)
def clean_collector_state():
    """Reset collector globals, shared maps and the IP caches per test."""
    import utils.ip_source_api as api_mod
    from utils.ip_facts import BLOCKED_IPS, COUNTRY_CACHE, VERDICT_CACHE
    from utils.shared_state import ACTIVE_USERS
    from utils.user_sync import USER_METADATA_CACHE

    blocklist_seed = set(BLOCKED_IPS)

    ACTIVE_USERS.clear()
    USER_METADATA_CACHE.clear()
    VERDICT_CACHE.clear()
    COUNTRY_CACHE.clear()
    api_mod._consecutive_dead_cycles = 0
    api_mod._forbidden_alert_sent = False
    api_mod._consecutive_coverage_skips = 0
    api_mod._coverage_alert_sent = False
    yield
    ACTIVE_USERS.clear()
    USER_METADATA_CACHE.clear()
    VERDICT_CACHE.clear()
    COUNTRY_CACHE.clear()
    # The blocklist is process state; restore it so an address blocked by one
    # test cannot reject an address another test relies on.
    BLOCKED_IPS.clear()
    BLOCKED_IPS.update(blocklist_seed)
    api_mod._consecutive_dead_cycles = 0
    api_mod._forbidden_alert_sent = False
    api_mod._consecutive_coverage_skips = 0
    api_mod._coverage_alert_sent = False


def _patch_collector(
    monkeypatch,
    candidates,
    payloads=None,
    counters=None,
    node_name_map=None,
    disabled_nodes=None,
    expected_node_ids=None,
    panel_available=True,
):
    """
    Replace every network boundary of ``collect_active_users_from_api``.

    Patching happens on the source modules because the collector imports them
    lazily inside the function body, so the lookup resolves at call time.
    """
    import utils.ip_source_api as api_mod
    import utils.panel_api.online_ips as online_ips_mod
    import utils.panel_api.request_helper as request_helper_mod

    notifications: list[str] = []

    async def fake_candidates(*_args, **_kwargs):
        return candidates

    async def fake_node_context(*_args, **_kwargs):
        names = dict(node_name_map or {})
        disabled = set(disabled_nodes or [])
        # Third element: the connected nodes the operator has not disabled, i.e.
        # the set that ought to appear in a healthy cycle. Defaults to every node
        # in node_name_map minus the disabled ones, which is what a healthy panel
        # looks like; pass expected_node_ids explicitly to simulate a fleet where
        # some nodes have gone quiet.
        expected = (
            set(expected_node_ids)
            if expected_node_ids is not None
            else set(names) - disabled
        )
        return names, disabled, expected

    async def fake_fetch(_panel, targets, **_kwargs):
        default = {"ok": len(targets), "failed": 0, "not_found": 0,
                   "forbidden": 0, "unauthorized": 0}
        resolved = dict(payloads or {})
        now = time.time()
        return resolved, dict(counters or default), {name: now for name in resolved}

    async def fake_notify(message: str):
        notifications.append(message)

    monkeypatch.setattr(online_ips_mod, "fetch_online_candidates", fake_candidates)
    monkeypatch.setattr(request_helper_mod, "is_panel_available", lambda: panel_available)
    monkeypatch.setattr(api_mod, "_resolve_node_context", fake_node_context)
    monkeypatch.setattr(api_mod, "_fetch_all_online_ips", fake_fetch)
    monkeypatch.setattr(api_mod, "_notify", fake_notify)
    return notifications


class TestResolveMonitoredGroupIds:
    """
    Group narrowing decides how small the candidate query stays.

    The rule it has to obey: never drop a user enforcement would judge. Only the
    Group Filter in ``include`` mode gives that guarantee, because it is the one
    setting that also makes those users ``is_monitored=False``.
    """

    def test_group_filter_include_wins(self):
        from utils.ip_source_api import resolve_monitored_group_ids

        config = {
            "group_filter": {"enabled": True, "mode": "include", "group_ids": [12, 15]},
            "group_limits": {"99": 1},
        }
        assert resolve_monitored_group_ids(config) == [12, 15]

    def test_group_limits_alone_do_not_narrow(self):
        """
        A group limit is a limit, not a monitoring scope.

        Users in no limited group are still monitored and judged against the general
        limit, so narrowing to the group_limits keys used to leave them uncollected
        while coverage - measured over the narrowed set - still read 100%.
        """
        from utils.ip_source_api import resolve_monitored_group_ids

        config = {"group_limits": {"15": 2, "12": 1}}
        assert resolve_monitored_group_ids(config) is None

    def test_exclude_mode_does_not_narrow(self):
        """Exclude mode cannot be expressed panel-side, and group_limits must not stand in."""
        from utils.ip_source_api import resolve_monitored_group_ids

        config = {
            "group_filter": {"enabled": True, "mode": "exclude", "group_ids": [12]},
            "group_limits": {"12": 4},
        }
        assert resolve_monitored_group_ids(config) is None

    def test_disabled_filter_does_not_narrow(self):
        """A disabled filter means every active user is monitored."""
        from utils.ip_source_api import resolve_monitored_group_ids

        config = {
            "group_filter": {"enabled": False, "mode": "include", "group_ids": [7]},
            "group_limits": {"3": 1},
        }
        assert resolve_monitored_group_ids(config) is None

    def test_include_mode_with_no_group_ids_does_not_narrow(self):
        """
        Degenerate config: nobody is monitored, but an empty list means "no filter" to
        the panel, so this asks wide and lets the client-side prefilter drop them.
        """
        from utils.ip_source_api import resolve_monitored_group_ids

        config = {
            "group_filter": {"enabled": True, "mode": "include", "group_ids": []},
            "group_limits": {"3": 1},
        }
        assert resolve_monitored_group_ids(config) is None

    def test_no_configuration_returns_none(self):
        from utils.ip_source_api import resolve_monitored_group_ids

        assert resolve_monitored_group_ids({}) is None


class TestResolveMonitoredAdmins:
    """Admin narrowing mirrors the group logic."""

    def test_include_mode_returns_usernames(self):
        from utils.ip_source_api import resolve_monitored_admins

        config = {
            "admin_filter": {
                "enabled": True, "mode": "include", "admin_usernames": ["reseller1"],
            }
        }
        assert resolve_monitored_admins(config) == ["reseller1"]

    def test_exclude_mode_returns_none(self):
        from utils.ip_source_api import resolve_monitored_admins

        config = {
            "admin_filter": {
                "enabled": True, "mode": "exclude", "admin_usernames": ["reseller1"],
            }
        }
        assert resolve_monitored_admins(config) is None

    def test_missing_filter_returns_none(self):
        from utils.ip_source_api import resolve_monitored_admins

        assert resolve_monitored_admins({}) is None


class TestPrefilterCandidates:
    """Candidates enforcement would ignore are dropped before the fan-out."""

    def test_excepted_and_unmonitored_users_are_dropped(self):
        from utils.ip_source_api import _prefilter_candidates
        from utils.user_sync import USER_METADATA_CACHE

        USER_METADATA_CACHE["white"] = {"is_excepted": True, "is_monitored": True}
        USER_METADATA_CACHE["outside"] = {"is_excepted": False, "is_monitored": False}
        USER_METADATA_CACHE["watched"] = {"is_excepted": False, "is_monitored": True}

        candidates = [
            {"username": "white", "id": 1},
            {"username": "outside", "id": 2},
            {"username": "watched", "id": 3},
        ]
        assert [name for name, _, _ in _prefilter_candidates(candidates)] == ["watched"]

    def test_unknown_users_are_kept_fail_open(self):
        from utils.ip_source_api import _prefilter_candidates

        # Not yet in the metadata cache: log mode would still watch them, so
        # dropping them here could hide a real violation.
        targets = _prefilter_candidates([{"username": "fresh", "id": 42}])
        assert targets == [("fresh", 42, {"username": "fresh", "id": 42})]

    def test_malformed_entries_are_ignored(self):
        from utils.ip_source_api import _prefilter_candidates

        candidates = [
            None,
            "not-a-dict",
            {"username": "", "id": 5},
            {"username": "no_id"},
            {"username": "bad_id", "id": "abc"},
            {"username": "ok", "id": "7"},
        ]
        assert _prefilter_candidates(candidates) == [
            ("ok", 7, {"username": "ok", "id": "7"})
        ]


class TestValidateIps:
    """IP admission must reach the same verdict as log mode."""

    @pytest.mark.asyncio
    async def test_private_and_malformed_ips_are_rejected(self, api_config):
        from utils.ip_source_api import _validate_ips

        raw = {"192.168.1.10", "10.0.0.5", "127.0.0.1", "not-an-ip", "5.6.7.8"}
        accepted, geo_lookups = await _validate_ips(raw, api_config)
        assert accepted == {"5.6.7.8"}
        assert geo_lookups == 0

    @pytest.mark.asyncio
    async def test_blocklisted_node_ips_are_rejected(self, api_config):
        from utils.ip_facts import BLOCKED_IPS
        from utils.ip_source_api import _validate_ips

        BLOCKED_IPS.add("77.77.77.77")
        accepted, _ = await _validate_ips({"77.77.77.77", "5.6.7.8"}, api_config)
        assert accepted == {"5.6.7.8"}

    @pytest.mark.asyncio
    async def test_country_code_is_read_from_config(self, api_config, monkeypatch):
        import utils.parse_logs as parse_logs_mod
        from utils.ip_source_api import _validate_ips

        countries = {"5.6.7.1": "IR", "5.6.7.2": "DE"}

        async def fake_lookup(ip):
            return countries.get(ip)

        monkeypatch.setattr(parse_logs_mod, "lookup_country", fake_lookup)
        api_config["country_code"] = "IR"

        accepted, geo_lookups = await _validate_ips(set(countries), api_config)
        assert accepted == {"5.6.7.1"}
        assert geo_lookups == 2

    @pytest.mark.asyncio
    async def test_country_code_from_monitoring_section(self, api_config, monkeypatch):
        import utils.parse_logs as parse_logs_mod
        from utils.ip_source_api import _validate_ips

        async def fake_lookup(_ip):
            return "DE"

        monkeypatch.setattr(parse_logs_mod, "lookup_country", fake_lookup)
        api_config["country_code"] = ""
        api_config["monitoring"] = {"country_code": "IR"}

        accepted, _ = await _validate_ips({"5.6.7.2"}, api_config)
        assert accepted == set()

    @pytest.mark.asyncio
    async def test_sentinel_country_values_disable_geo(self, api_config, monkeypatch):
        import utils.parse_logs as parse_logs_mod
        from utils.ip_source_api import _validate_ips

        async def fail_lookup(_ip):
            raise AssertionError("geo lookup must not run when geo is disabled")

        monkeypatch.setattr(parse_logs_mod, "lookup_country", fail_lookup)
        for value in ("", "none", "OFF", "any", "all", "disabled"):
            api_config["country_code"] = value
            accepted, geo_lookups = await _validate_ips({"5.6.7.8"}, api_config)
            assert accepted == {"5.6.7.8"}
            assert geo_lookups == 0

    @pytest.mark.asyncio
    async def test_failed_geo_lookup_accepts_the_ip(self, api_config, monkeypatch):
        import utils.parse_logs as parse_logs_mod
        from utils.ip_facts import VERDICT_CACHE
        from utils.ip_source_api import _validate_ips

        async def fake_lookup(_ip):
            return None

        monkeypatch.setattr(parse_logs_mod, "lookup_country", fake_lookup)
        api_config["country_code"] = "IR"

        accepted, _ = await _validate_ips({"5.6.7.8"}, api_config)
        # Accepted so a geo outage cannot hide traffic, but deliberately not
        # cached, so the address is re-checked once the provider recovers.
        assert accepted == {"5.6.7.8"}
        assert "5.6.7.8" not in VERDICT_CACHE


class TestBuildUsersFromPayloads:
    """The adapter must produce exactly what log mode produces."""

    @pytest.mark.asyncio
    async def test_device_info_matches_log_mode(self, api_config):
        from utils.ip_source_api import _build_users_from_payloads

        payloads = {"alice": {1: {"5.6.7.8": fresh(), "9.9.9.9": fresh()}}}
        users, stats = await _build_users_from_payloads(
            payloads,
            {"alice": {"status": "active", "group_ids": [12]}},
            {1: "de-node"},
            set(),
            api_config,
        )

        user = users["alice"]
        assert user.name == "alice"
        assert user.ip == ["5.6.7.8", "9.9.9.9"]
        assert user.panel_status == "active"
        assert user.group_ids == [12]
        # ``status`` is the local enum, never the panel's textual status.
        assert user.status is None
        assert {c.ip for c in user.device_info.connections} == {"5.6.7.8", "9.9.9.9"}
        assert {c.node_name for c in user.device_info.connections} == {"de-node"}
        assert {c.inbound_protocol for c in user.device_info.connections} == {"API"}
        assert stats == {
            "geo_lookups": 0, "nodes_seen": 1, "total_ips": 2, "users_with_ips": 1,
            "stale_ips": 0, "future_ips": 0, "unknown_age_ips": 0,
        }

    @pytest.mark.asyncio
    async def test_stale_ips_are_dropped(self, api_config):
        from utils.ip_source_api import _build_users_from_payloads

        # The panel keeps an IP in the map with its last-seen timestamp long
        # after the client left; anything older than the freshness window is not
        # a currently connected device.
        now = int(time.time())
        api_config["check_interval"] = 180
        payloads = {"alice": {1: {"5.6.7.8": now, "9.9.9.9": now - 3600}}}
        users, stats = await _build_users_from_payloads(
            payloads, {}, {1: "node"}, set(), api_config
        )
        assert users["alice"].ip == ["5.6.7.8"]
        assert stats["stale_ips"] == 1

    @pytest.mark.asyncio
    async def test_values_below_the_epoch_floor_are_not_counted(self, api_config):
        from utils.ip_source_api import _build_users_from_payloads

        # A value too small to be a last-seen epoch has an unknown age, so it cannot be
        # shown to be a current connection. It used to be read as a legacy connection
        # count and counted as live, which skipped the freshness filter entirely - and
        # the panel never expires an entry, so a user who merely changed network looked
        # like several simultaneous devices. Every unparseable value lands here too:
        # _parse_ip_payload coerces an ISO-8601 string or a float-formatted epoch to 1.
        payloads = {"alice": {1: {"5.6.7.8": 3, "9.9.9.9": 1}}}
        users, stats = await _build_users_from_payloads(
            payloads, {}, {1: "node"}, set(), api_config
        )
        assert users == {}
        assert stats["unknown_age_ips"] == 2
        # Counted as stale as well, so the existing "every IP filtered" gate skips the
        # cycle rather than publishing an empty snapshot that clears pending warnings.
        assert stats["stale_ips"] == 2

    @pytest.mark.asyncio
    async def test_a_fresh_ip_survives_alongside_an_unknown_age_one(self, api_config):
        from utils.ip_source_api import _build_users_from_payloads

        payloads = {"alice": {1: {"5.6.7.8": fresh(), "9.9.9.9": 1}}}
        users, stats = await _build_users_from_payloads(
            payloads, {}, {1: "node"}, set(), api_config
        )
        assert users["alice"].ip == ["5.6.7.8"]
        assert stats["unknown_age_ips"] == 1
        assert stats["total_ips"] == 1
        # Also counted as stale, which is what lets the all-stale gate fire when a whole
        # payload comes back sub-floor.
        assert stats["stale_ips"] == 1

    @pytest.mark.asyncio
    async def test_freshness_is_measured_from_the_fetch_time(self, api_config):
        from utils.ip_source_api import _build_users_from_payloads

        # A slow fan-out can finish minutes after the first users were sampled.
        # Judging their IPs against "now" would discard data that was live when
        # it was read, silently turning early users into inactive ones.
        now = int(time.time())
        api_config["check_interval"] = 180
        fetched_at = now - 400
        payloads = {"alice": {1: {"5.6.7.8": fetched_at - 10, "9.9.9.9": fetched_at - 900}}}
        users, stats = await _build_users_from_payloads(
            payloads, {}, {1: "node"}, set(), api_config,
            fetch_times={"alice": fetched_at},
        )
        assert users["alice"].ip == ["5.6.7.8"]
        assert stats["stale_ips"] == 1

    @pytest.mark.asyncio
    async def test_disabled_nodes_are_excluded(self, api_config):
        from utils.ip_source_api import _build_users_from_payloads

        payloads = {"alice": {1: {"5.6.7.8": fresh()}, 2: {"9.9.9.9": fresh()}}}
        users, stats = await _build_users_from_payloads(
            payloads, {}, {1: "keep", 2: "drop"}, {2}, api_config
        )
        assert users["alice"].ip == ["5.6.7.8"]
        assert stats["nodes_seen"] == 1

    @pytest.mark.asyncio
    async def test_user_with_only_rejected_ips_is_omitted(self, api_config):
        from utils.ip_source_api import _build_users_from_payloads

        payloads = {
            "private_only": {1: {"192.168.1.5": fresh()}},
            "real": {1: {"5.6.7.8": fresh()}},
        }
        users, stats = await _build_users_from_payloads(
            payloads, {}, {1: "node"}, set(), api_config
        )
        assert set(users) == {"real"}
        assert stats["users_with_ips"] == 1
        assert stats["total_ips"] == 1

    @pytest.mark.asyncio
    async def test_same_ip_on_two_nodes_stays_one_ip(self, api_config):
        from utils.ip_source_api import _build_users_from_payloads

        payloads = {"alice": {1: {"5.6.7.8": fresh()}, 2: {"5.6.7.8": fresh()}}}
        users, stats = await _build_users_from_payloads(
            payloads, {}, {1: "a", 2: "b"}, set(), api_config
        )
        user = users["alice"]
        assert user.ip == ["5.6.7.8"]
        assert stats["total_ips"] == 1
        # Two nodes were seen, and both connections are recorded so per-node
        # grouping downstream still has the data it needs.
        assert stats["nodes_seen"] == 2
        assert len(user.device_info.connections) == 2

    @pytest.mark.asyncio
    async def test_sentinel_inbound_is_configurable(self, api_config):
        from utils.ip_source_api import _build_users_from_payloads

        api_config["api_ip_sentinel_inbound"] = "PANEL"
        users, _ = await _build_users_from_payloads(
            {"alice": {1: {"5.6.7.8": fresh()}}}, {}, {1: "node"}, set(), api_config
        )
        connection = users["alice"].device_info.connections[0]
        assert connection.inbound_protocol == "PANEL"

    @pytest.mark.asyncio
    async def test_unknown_node_gets_placeholder_name(self, api_config):
        from utils.ip_source_api import _build_users_from_payloads

        users, _ = await _build_users_from_payloads(
            {"alice": {7: {"5.6.7.8": fresh()}}}, {}, {}, set(), api_config
        )
        assert users["alice"].device_info.connections[0].node_name == "Node-7"

    @pytest.mark.asyncio
    async def test_admin_username_is_extracted_from_either_shape(self, api_config):
        from utils.ip_source_api import _build_users_from_payloads

        payloads = {
            "nested": {1: {"5.6.7.8": fresh()}},
            "flat": {1: {"9.9.9.9": fresh()}},
            "legacy": {1: {"4.4.4.4": fresh()}},
        }
        raw_by_name = {
            "nested": {"admin": {"username": "reseller1"}},
            "flat": {"admin": "reseller2"},
            "legacy": {"admin_username": "reseller3"},
        }
        users, _ = await _build_users_from_payloads(
            payloads, raw_by_name, {1: "node"}, set(), api_config
        )
        assert users["nested"].admin_username == "reseller1"
        assert users["flat"].admin_username == "reseller2"
        assert users["legacy"].admin_username == "reseller3"


class TestOnlineWindow:
    """The freshness window has to track CHECK_INTERVAL from ENV/DB."""

    def test_auto_window_is_interval_plus_grace(self, api_config):
        from utils.ip_source_api import _resolve_online_window

        api_config["check_interval"] = 240
        assert _resolve_online_window(api_config) == 270

    def test_explicit_window_wins(self, api_config):
        from utils.ip_source_api import _resolve_online_window

        api_config["api_ip_online_window"] = 600
        assert _resolve_online_window(api_config) == 600

    def test_short_interval_keeps_a_floor(self, api_config):
        from utils.ip_source_api import _resolve_online_window

        api_config["check_interval"] = 10
        assert _resolve_online_window(api_config) == 60

    def test_falls_back_to_monitoring_interval(self, api_config):
        from utils.ip_source_api import _resolve_online_window

        api_config.pop("check_interval")
        api_config["monitoring"] = {"check_interval": 180}
        assert _resolve_online_window(api_config) == 210


class TestFreshnessWindow:
    """An IP older than this window is not a currently connected device."""

    def test_auto_window_is_the_check_interval(self, api_config):
        from utils.ip_source_api import _resolve_freshness_window

        api_config["check_interval"] = 240
        assert _resolve_freshness_window(api_config) == 240

    def test_explicit_value_wins(self, api_config):
        from utils.ip_source_api import _resolve_freshness_window

        api_config["api_ip_freshness"] = 90
        assert _resolve_freshness_window(api_config) == 90

    def test_short_interval_keeps_a_floor(self, api_config):
        from utils.ip_source_api import _resolve_freshness_window

        api_config["check_interval"] = 10
        assert _resolve_freshness_window(api_config) == 60


class TestCollectFailSafe:
    """
    A cycle must never be allowed to escalate an innocent user.

    Every degraded outcome returns ``False`` so ``check_usage`` skips
    enforcement entirely, which leaves the consecutive-violation counters
    exactly as they were.
    """

    @pytest.mark.asyncio
    async def test_unavailable_panel_skips_the_cycle(self, panel, api_config, monkeypatch):
        from utils.ip_source_api import collect_active_users_from_api, get_last_cycle_stats

        _patch_collector(monkeypatch, candidates=[], panel_available=False)
        assert await collect_active_users_from_api(panel, api_config) is False
        assert get_last_cycle_stats()["skipped_reason"] == "panel unavailable"

    @pytest.mark.asyncio
    async def test_failed_candidate_query_keeps_previous_state(
        self, panel, api_config, monkeypatch
    ):
        from utils.ip_source_api import collect_active_users_from_api, get_last_cycle_stats
        from utils.shared_state import ACTIVE_USERS
        from utils.types import UserType

        ACTIVE_USERS["carol"] = UserType(name="carol", ip=["5.6.7.8"])
        _patch_collector(monkeypatch, candidates=None)

        assert await collect_active_users_from_api(panel, api_config) is False
        # Treating a query failure as "nobody online" would wipe every pending
        # warning, so ACTIVE_USERS must be left untouched.
        assert list(ACTIVE_USERS) == ["carol"]
        assert get_last_cycle_stats()["skipped_reason"] == "candidate query failed"

    @pytest.mark.asyncio
    async def test_zero_successful_lookups_skips_the_cycle(
        self, panel, api_config, monkeypatch
    ):
        from utils.ip_source_api import collect_active_users_from_api, get_last_cycle_stats

        _patch_collector(
            monkeypatch,
            candidates=[{"username": "alice", "id": 1}],
            counters={"ok": 0, "failed": 1, "not_found": 0,
                      "forbidden": 0, "unauthorized": 0},
        )
        assert await collect_active_users_from_api(panel, api_config) is False
        assert get_last_cycle_stats()["skipped_reason"] == "no successful lookups"

    @pytest.mark.asyncio
    async def test_low_coverage_skips_the_cycle(self, panel, api_config, monkeypatch):
        from utils.ip_source_api import collect_active_users_from_api, get_last_cycle_stats
        from utils.shared_state import ACTIVE_USERS
        from utils.types import UserType

        ACTIVE_USERS["carol"] = UserType(name="carol", ip=["5.6.7.8"])
        _patch_collector(
            monkeypatch,
            candidates=[{"username": f"u{i}", "id": i} for i in range(1, 5)],
            payloads={"u1": {1: {"5.6.7.8": fresh()}}},
            counters={"ok": 1, "failed": 3, "not_found": 0,
                      "forbidden": 0, "unauthorized": 0},
        )

        # 1 of 4 answered = 25% coverage, below the configured 80%.
        assert await collect_active_users_from_api(panel, api_config) is False
        assert list(ACTIVE_USERS) == ["carol"]
        stats = get_last_cycle_stats()
        assert stats["coverage"] == 0.25
        assert "below" in stats["skipped_reason"]

    @pytest.mark.asyncio
    async def test_not_found_counts_as_covered(self, panel, api_config, monkeypatch):
        from utils.ip_source_api import collect_active_users_from_api, get_last_cycle_stats
        _patch_collector(
            monkeypatch,
            candidates=[{"username": f"u{i}", "id": i} for i in range(1, 5)],
            payloads={"u1": {1: {"5.6.7.8": fresh()}}},
            counters={"ok": 1, "failed": 0, "not_found": 3,
                      "forbidden": 0, "unauthorized": 0},
            node_name_map={1: "node"},
        )

        # A 404 is a definitive answer, so coverage is full and enforcement runs.
        assert await collect_active_users_from_api(panel, api_config) is True
        assert get_last_cycle_stats()["coverage"] == 1.0

    @pytest.mark.asyncio
    async def test_node_coverage_is_reported_even_with_the_gate_off(
        self, panel, api_config, monkeypatch
    ):
        from utils.ip_source_api import collect_active_users_from_api, get_last_cycle_stats

        seen_at = int(time.time())
        _patch_collector(
            monkeypatch,
            candidates=[{"username": "alice", "id": 1}],
            payloads={"alice": {1: {"5.6.7.8": seen_at}}},
            node_name_map={1: "de", 2: "nl", 3: "fr"},
        )

        # Per-user coverage is a perfect 1.0 while two of three nodes said nothing.
        # That is exactly the blind spot the node ratio exists to expose, so the
        # cycle still runs but the numbers are now on the record.
        assert await collect_active_users_from_api(panel, api_config) is True
        stats = get_last_cycle_stats()
        assert stats["coverage"] == 1.0
        assert stats["nodes_seen"] == 1
        assert stats["nodes_expected"] == 3
        assert stats["node_coverage"] == 0.3333

    @pytest.mark.asyncio
    async def test_low_node_coverage_skips_the_cycle_when_configured(
        self, panel, api_config, monkeypatch
    ):
        from utils.ip_source_api import collect_active_users_from_api, get_last_cycle_stats
        from utils.shared_state import ACTIVE_USERS
        from utils.types import UserType

        ACTIVE_USERS["carol"] = UserType(name="carol", ip=["1.2.3.4"])
        api_config["api_ip_min_node_coverage"] = 0.8
        seen_at = int(time.time())
        _patch_collector(
            monkeypatch,
            candidates=[{"username": "alice", "id": 1}],
            payloads={"alice": {1: {"5.6.7.8": seen_at}}},
            node_name_map={1: "de", 2: "nl", 3: "fr"},
        )

        assert await collect_active_users_from_api(panel, api_config) is False
        assert list(ACTIVE_USERS) == ["carol"]
        stats = get_last_cycle_stats()
        assert "node coverage" in stats["skipped_reason"]
        # What the build actually found still has to be reported, or the
        # diagnostics read like a dead panel instead of a partial fleet.
        assert stats["users_with_ips"] == 1
        assert stats["total_ips"] == 1

    @pytest.mark.asyncio
    async def test_full_node_coverage_passes_the_gate(self, panel, api_config, monkeypatch):
        from utils.ip_source_api import collect_active_users_from_api
        from utils.shared_state import ACTIVE_USERS

        api_config["api_ip_min_node_coverage"] = 0.8
        seen_at = int(time.time())
        _patch_collector(
            monkeypatch,
            candidates=[{"username": "alice", "id": 1}],
            payloads={"alice": {1: {"5.6.7.8": seen_at}}},
            node_name_map={1: "de", 2: "nl"},
            expected_node_ids={1},
        )

        # Node 2 is deliberately absent from expected_node_ids - a node the operator
        # disabled, or one the panel no longer lists, must not drag the ratio down.
        assert await collect_active_users_from_api(panel, api_config) is True
        assert list(ACTIVE_USERS) == ["alice"]

    @pytest.mark.asyncio
    async def test_all_stale_ips_skip_the_cycle(self, panel, api_config, monkeypatch):
        from utils.ip_source_api import collect_active_users_from_api, get_last_cycle_stats
        from utils.shared_state import ACTIVE_USERS
        from utils.types import UserType

        # Timestamps that are uniformly ancient point at clock skew, not at
        # 1300 users going offline at once. Publishing the empty snapshot would
        # clear every pending warning, so the cycle must be abandoned.
        ACTIVE_USERS["carol"] = UserType(name="carol", ip=["5.6.7.8"])
        _patch_collector(
            monkeypatch,
            candidates=[{"username": "alice", "id": 1}],
            payloads={"alice": {1: {"5.6.7.8": int(time.time()) - 86400}}},
            node_name_map={1: "de"},
        )

        assert await collect_active_users_from_api(panel, api_config) is False
        assert list(ACTIVE_USERS) == ["carol"]
        assert get_last_cycle_stats()["skipped_reason"] == "every IP filtered as stale"

    @pytest.mark.asyncio
    async def test_low_coverage_does_not_count_as_a_dead_cycle(
        self, panel, api_config, monkeypatch
    ):
        import utils.ip_source_api as api_mod

        api_mod._consecutive_dead_cycles = 2
        _patch_collector(
            monkeypatch,
            candidates=[{"username": f"u{i}", "id": i} for i in range(1, 5)],
            counters={"ok": 1, "failed": 3, "not_found": 0,
                      "forbidden": 0, "unauthorized": 0},
        )
        assert await api_mod.collect_active_users_from_api(panel, api_config) is False
        # The panel answered, so the auto-fallback streak resets.
        assert api_mod._consecutive_dead_cycles == 0

    @pytest.mark.asyncio
    async def test_repeated_coverage_skips_raise_one_alert(
        self, panel, api_config, monkeypatch
    ):
        """
        A run of coverage skips means enforcement has stopped entirely.

        One skip is the correct, safe outcome. Three in a row means nobody is being
        warned or banned at all, and the only trace used to be one WARNING per cycle
        that reads exactly like the previous one. Reverting to log mode is not the
        remedy here - the panel is answering, just incompletely - so this is an
        alarm, not a mode change.
        """
        import utils.ip_source_api as api_mod

        notifications = _patch_collector(
            monkeypatch,
            candidates=[{"username": f"u{i}", "id": i} for i in range(1, 5)],
            counters={"ok": 1, "failed": 3, "not_found": 0,
                      "forbidden": 0, "unauthorized": 0},
        )

        for _ in range(api_mod.COVERAGE_SKIP_ALERT_THRESHOLD + 2):
            assert await api_mod.collect_active_users_from_api(panel, api_config) is False

        assert api_mod._consecutive_coverage_skips == api_mod.COVERAGE_SKIP_ALERT_THRESHOLD + 2
        # Alarmed once, not once per cycle.
        assert len(notifications) == 1
        assert "enforcement has stopped" in notifications[0]

    @pytest.mark.asyncio
    async def test_forbidden_raises_one_alert_only(self, panel, api_config, monkeypatch):
        import utils.ip_source_api as api_mod

        notifications = _patch_collector(
            monkeypatch,
            candidates=[{"username": "alice", "id": 1}],
            counters={"ok": 0, "failed": 0, "not_found": 0,
                      "forbidden": 1, "unauthorized": 0},
        )

        assert await api_mod.collect_active_users_from_api(panel, api_config) is False
        assert await api_mod.collect_active_users_from_api(panel, api_config) is False
        assert api_mod.get_last_cycle_stats()["forbidden"] == 1
        assert len(notifications) == 1
        assert "nodes:stats" in notifications[0]

    @pytest.mark.asyncio
    async def test_auto_fallback_reverts_after_three_dead_cycles(
        self, panel, api_config, monkeypatch
    ):
        import utils.ip_source_api as api_mod
        import utils.read_config as read_config_mod

        saved: list[tuple[str, str]] = []

        async def fake_save(key, value):
            saved.append((key, value))
            return True

        monkeypatch.setattr(read_config_mod, "save_config_value", fake_save)
        api_config["api_ip_auto_fallback"] = True
        _patch_collector(monkeypatch, candidates=None)

        for _ in range(2):
            assert await api_mod.collect_active_users_from_api(panel, api_config) is False
        assert saved == []

        assert await api_mod.collect_active_users_from_api(panel, api_config) is False
        assert saved == [("ip_source", "logs")]
        assert api_mod._consecutive_dead_cycles == 0

    @pytest.mark.asyncio
    async def test_auto_fallback_off_never_writes_config(
        self, panel, api_config, monkeypatch
    ):
        import utils.ip_source_api as api_mod
        import utils.read_config as read_config_mod

        async def fail_save(*_args, **_kwargs):
            raise AssertionError("config must not be written when fallback is off")

        monkeypatch.setattr(read_config_mod, "save_config_value", fail_save)
        _patch_collector(monkeypatch, candidates=None)

        for _ in range(4):
            assert await api_mod.collect_active_users_from_api(panel, api_config) is False


class TestCollectHappyPath:
    """A trustworthy cycle publishes a complete, fresh snapshot."""

    @pytest.mark.asyncio
    async def test_good_cycle_replaces_active_users(self, panel, api_config, monkeypatch):
        from utils.ip_source_api import collect_active_users_from_api, get_last_cycle_stats
        from utils.shared_state import ACTIVE_USERS
        from utils.types import UserType

        # A stale entry from the previous cycle must not survive: merging would
        # accumulate IPs across cycles and inflate device counts.
        ACTIVE_USERS["gone"] = UserType(name="gone", ip=["1.2.3.4"])
        _patch_collector(
            monkeypatch,
            candidates=[
                {"username": "alice", "id": 1, "status": "active"},
                {"username": "bob", "id": 2, "status": "active"},
            ],
            payloads={
                "alice": {1: {"5.6.7.8": 2}},
                "bob": {1: {"9.9.9.9": 1}, 2: {"4.4.4.4": 1}},
            },
            node_name_map={1: "de", 2: "nl"},
        )

        assert await collect_active_users_from_api(panel, api_config) is True
        assert set(ACTIVE_USERS) == {"alice", "bob"}
        assert ACTIVE_USERS["bob"].ip == ["4.4.4.4", "9.9.9.9"]

        stats = get_last_cycle_stats()
        assert stats["candidates"] == 2
        assert stats["prefiltered"] == 2
        assert stats["users_with_ips"] == 2
        assert stats["total_ips"] == 3
        assert stats["nodes_seen"] == 2
        assert stats["coverage"] == 1.0
        assert stats["skipped_reason"] == ""

    @pytest.mark.asyncio
    async def test_empty_candidate_set_still_runs_enforcement(
        self, panel, api_config, monkeypatch
    ):
        from utils.ip_source_api import collect_active_users_from_api, get_last_cycle_stats
        from utils.shared_state import ACTIVE_USERS
        from utils.types import UserType

        ACTIVE_USERS["gone"] = UserType(name="gone", ip=["1.2.3.4"])
        _patch_collector(monkeypatch, candidates=[])

        # Nobody online is a valid answer, not a failure: enforcement has to run
        # so users who normalized get their warnings cleared.
        assert await collect_active_users_from_api(panel, api_config) is True
        assert ACTIVE_USERS == {}
        assert get_last_cycle_stats()["coverage"] == 1.0

    @pytest.mark.asyncio
    async def test_excepted_users_are_never_looked_up(self, panel, api_config, monkeypatch):
        import utils.ip_source_api as api_mod
        from utils.shared_state import ACTIVE_USERS
        from utils.user_sync import USER_METADATA_CACHE

        USER_METADATA_CACHE["white"] = {"is_excepted": True, "is_monitored": True}
        seen: list[list[str]] = []
        _patch_collector(
            monkeypatch,
            candidates=[
                {"username": "white", "id": 1},
                {"username": "alice", "id": 2},
            ],
            payloads={"alice": {1: {"5.6.7.8": fresh()}}},
            node_name_map={1: "de"},
        )

        original_fetch = api_mod._fetch_all_online_ips

        async def spy_fetch(panel_data, targets, **kwargs):
            seen.append([name for name, _, _ in targets])
            return await original_fetch(panel_data, targets, **kwargs)

        monkeypatch.setattr(api_mod, "_fetch_all_online_ips", spy_fetch)

        assert await api_mod.collect_active_users_from_api(panel, api_config) is True
        assert seen == [["alice"]]
        assert set(ACTIVE_USERS) == {"alice"}
        assert api_mod.get_last_cycle_stats()["prefiltered"] == 1

    @pytest.mark.asyncio
    async def test_candidate_query_is_narrowed_by_the_include_filter(
        self, panel, api_config, monkeypatch
    ):
        import utils.panel_api.online_ips as online_ips_mod
        from utils.ip_source_api import collect_active_users_from_api

        captured: dict = {}

        async def capture(_panel, **kwargs):
            captured.update(kwargs)
            return []
        _patch_collector(monkeypatch, candidates=[])
        monkeypatch.setattr(online_ips_mod, "fetch_online_candidates", capture)

        api_config["group_filter"] = {
            "enabled": True, "mode": "include", "group_ids": [12, 15]
        }
        api_config["check_interval"] = 240

        assert await collect_active_users_from_api(panel, api_config) is True
        assert captured["group_ids"] == [12, 15]
        assert captured["status"] == "active"
        assert captured["online_window"] == 270

    @pytest.mark.asyncio
    async def test_group_limits_do_not_narrow_the_candidate_query(
        self, panel, api_config, monkeypatch
    ):
        """
        The regression that matters: group limits are limits, not a monitoring scope.

        Users outside the limited groups are still judged against the general limit, so
        asking the panel only about the limited groups left them uncollected - and the
        coverage gate could not see it, because coverage is the answer rate over
        whatever was asked for.
        """
        import utils.panel_api.online_ips as online_ips_mod
        from utils.ip_source_api import collect_active_users_from_api

        captured: dict = {}

        async def capture(_panel, **kwargs):
            captured.update(kwargs)
            return []
        _patch_collector(monkeypatch, candidates=[])
        monkeypatch.setattr(online_ips_mod, "fetch_online_candidates", capture)

        api_config["group_limits"] = {"12": 1, "15": 2}

        assert await collect_active_users_from_api(panel, api_config) is True
        assert captured["group_ids"] is None
        assert captured["status"] == "active"

    @pytest.mark.asyncio
    async def test_exclude_filter_does_not_narrow_to_the_excluded_group(
        self, panel, api_config, monkeypatch
    ):
        """
        The worst shape of the old bug: exclude mode fell through to group_limits, so a
        limit on the very group being excluded made the query ask for exactly the users
        enforcement must ignore - and enforcement then ran against nobody.
        """
        import utils.panel_api.online_ips as online_ips_mod
        from utils.ip_source_api import collect_active_users_from_api

        captured: dict = {}

        async def capture(_panel, **kwargs):
            captured.update(kwargs)
            return []
        _patch_collector(monkeypatch, candidates=[])
        monkeypatch.setattr(online_ips_mod, "fetch_online_candidates", capture)

        api_config["group_filter"] = {
            "enabled": True, "mode": "exclude", "group_ids": [7]
        }
        api_config["group_limits"] = {"7": 4}

        assert await collect_active_users_from_api(panel, api_config) is True
        assert captured["group_ids"] is None

    @pytest.mark.asyncio
    async def test_all_monitored_mode_drops_the_online_window(
        self, panel, api_config, monkeypatch
    ):
        import utils.panel_api.online_ips as online_ips_mod
        from utils.ip_source_api import collect_active_users_from_api

        captured: dict = {}

        async def capture(_panel, **kwargs):
            captured.update(kwargs)
            return []
        _patch_collector(monkeypatch, candidates=[])
        monkeypatch.setattr(online_ips_mod, "fetch_online_candidates", capture)

        api_config["api_ip_candidate_mode"] = "all_monitored"
        api_config["api_ip_page_size"] = 250

        assert await collect_active_users_from_api(panel, api_config) is True
        assert captured["online_window"] == 0
        assert captured["page_size"] == 250
