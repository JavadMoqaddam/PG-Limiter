#!/usr/bin/env python3
"""
Unit tests for device counting.

This is the number every ban is decided on, so each counting rule is pinned
down here: the two counting modes, CDN collapsing, disabled nodes, subnet
grouping, High Trust grouping and the no-connection fallback.
"""

import pytest

from utils.device_count import (
    COUNT_MODE_DEVICE,
    COUNT_MODE_IP,
    DeviceCountingConfig,
    build_ip_details,
    count_devices,
    count_devices_and_details,
    group_ips_by_subnet,
    subnet_key,
    wide_subnet_key,
)
from utils.types import ConnectionInfo, DeviceInfo, EnhancedUserInfo, UserType


def make_user(connections: list[tuple], name: str = "user1") -> UserType:
    """
    Build a user from ``(ip, inbound, node_id)`` triples.

    Mirrors what the log parser and the API collector write into ACTIVE_USERS.
    """
    conns = [
        ConnectionInfo(
            ip=ip,
            node_id=node_id,
            node_name=f"node-{node_id}",
            inbound_protocol=inbound,
            last_seen=0.0,
        )
        for ip, inbound, node_id in connections
    ]
    ips = sorted({c.ip for c in conns})
    return UserType(
        name=name,
        ip=ips,
        device_info=DeviceInfo(
            connections=conns,
            unique_ips=set(ips),
            unique_nodes={c.node_id for c in conns},
            inbound_protocols={c.inbound_protocol for c in conns},
        ),
    )


@pytest.fixture
def device_mode():
    """Default configuration: (ip-or-subnet, inbound) keys."""
    return DeviceCountingConfig(count_mode=COUNT_MODE_DEVICE)


@pytest.fixture
def ip_mode():
    """One client IP is exactly one device."""
    return DeviceCountingConfig(count_mode=COUNT_MODE_IP)


class TestCountingModes:
    """The node is never part of the key; the inbound is, in device mode only."""

    def test_same_ip_and_inbound_on_two_nodes_is_one_device(self, device_mode):
        # Several nodes serve one core config, so a single client shows up on
        # all of them at once. Counting per node was the false-positive bug.
        user = make_user([("5.6.7.8", "Vless", 1), ("5.6.7.8", "Vless", 2)])
        assert count_devices(user, device_mode) == 1

    def test_one_ip_on_two_inbounds_is_two_devices(self, device_mode):
        # How several people sharing one internet connection get caught.
        user = make_user([("5.6.7.8", "Vless", 1), ("5.6.7.8", "Vmess", 1)])
        assert count_devices(user, device_mode) == 2

    def test_distinct_ips_count_separately(self, device_mode):
        user = make_user([("5.6.7.8", "Vless", 1), ("9.9.9.9", "Vless", 1)])
        assert count_devices(user, device_mode) == 2

    def test_ip_mode_ignores_inbound_and_node(self, ip_mode):
        user = make_user(
            [
                ("5.6.7.8", "Vless", 1),
                ("5.6.7.8", "Vmess", 1),
                ("5.6.7.8", "Vless", 2),
            ]
        )
        assert count_devices(user, ip_mode) == 1

    def test_ip_mode_still_counts_distinct_ips(self, ip_mode):
        user = make_user([("5.6.7.8", "Vless", 1), ("9.9.9.9", "Vmess", 2)])
        assert count_devices(user, ip_mode) == 2


class TestCdnCollapsing:
    """A CDN presents many addresses for one client."""

    def test_cdn_node_counts_as_one_device(self):
        config = DeviceCountingConfig(cdn_nodes=[5])
        user = make_user(
            [
                ("104.21.45.1", "Vless", 5),
                ("104.21.45.2", "Vless", 5),
                ("104.21.45.3", "Vmess", 5),
            ]
        )
        assert count_devices(user, config) == 1

    def test_cdn_inbound_counts_as_one_device(self):
        config = DeviceCountingConfig(cdn_inbounds=["CDN_WS"])
        user = make_user(
            [
                ("104.21.45.1", "CDN_WS", 1),
                ("104.21.45.2", "CDN_WS", 2),
            ]
        )
        assert count_devices(user, config) == 1

    def test_cdn_and_direct_traffic_are_counted_separately(self):
        config = DeviceCountingConfig(cdn_nodes=[5], cdn_inbounds=["CDN_WS"])
        user = make_user(
            [
                ("104.21.45.1", "Vless", 5),   # CDN node -> 1
                ("104.21.45.2", "CDN_WS", 1),  # CDN inbound -> 1
                ("5.6.7.8", "Vless", 1),       # direct -> 1
            ]
        )
        assert count_devices(user, config) == 3

    def test_cdn_collapsing_also_applies_in_ip_mode(self):
        config = DeviceCountingConfig(count_mode=COUNT_MODE_IP, cdn_nodes=[5])
        user = make_user(
            [("104.21.45.1", "Vless", 5), ("104.21.45.2", "Vless", 5)]
        )
        assert count_devices(user, config) == 1


    def test_one_ip_on_several_cdn_nodes_is_one_device(self):
        # Regression: with one CDN key per node, a single-IP user connected to
        # seven CDN nodes was reported as seven devices and banned.
        config = DeviceCountingConfig(cdn_nodes=[5, 6, 7])
        user = make_user(
            [
                ("5.6.7.8", "Vless", 5),
                ("5.6.7.8", "Vless", 6),
                ("5.6.7.8", "Vless", 7),
            ]
        )
        assert count_devices(user, config) == 1

    def test_device_mode_keeps_cdn_inbounds_apart(self):
        config = DeviceCountingConfig(cdn_inbounds=["CDN_WS", "CDN_GRPC"])
        user = make_user([("5.6.7.8", "CDN_WS", 1), ("5.6.7.8", "CDN_GRPC", 1)])
        assert count_devices(user, config) == 2

    def test_ip_mode_collapses_all_cdn_inbounds(self):
        # In "ip" mode the inbound is part of no key, CDN inbounds included.
        config = DeviceCountingConfig(
            count_mode=COUNT_MODE_IP, cdn_inbounds=["CDN_WS", "CDN_GRPC"]
        )
        user = make_user([("5.6.7.8", "CDN_WS", 1), ("5.6.7.8", "CDN_GRPC", 1)])
        assert count_devices(user, config) == 1


class TestDisabledNodes:
    """Traffic from a disabled node is not evidence of anything."""

    def test_connections_from_disabled_nodes_are_ignored(self):
        config = DeviceCountingConfig(disabled_nodes=[9])
        user = make_user([("5.6.7.8", "Vless", 9), ("9.9.9.9", "Vless", 1)])
        assert count_devices(user, config) == 1

    def test_only_disabled_nodes_means_zero_devices(self):
        config = DeviceCountingConfig(disabled_nodes=[9])
        user = make_user([("5.6.7.8", "Vless", 9), ("9.9.9.9", "Vmess", 9)])
        # Not the fallback: connections existed, they were all discarded.
        assert count_devices(user, config, fallback_count=7) == 0

    def test_disabled_node_wins_over_cdn_node(self):
        config = DeviceCountingConfig(disabled_nodes=[5], cdn_nodes=[5])
        user = make_user([("104.21.45.1", "Vless", 5)])
        assert count_devices(user, config) == 0


class TestSubnetGrouping:
    """Subnet grouping forgives a client that hops addresses within one ISP."""

    def test_same_24_and_inbound_is_one_device(self):
        config = DeviceCountingConfig(subnet_ip_grouping=True)
        user = make_user([("5.6.7.10", "Vless", 1), ("5.6.7.99", "Vless", 2)])
        assert count_devices(user, config) == 1

    def test_same_24_on_two_inbounds_stays_two_devices(self):
        config = DeviceCountingConfig(subnet_ip_grouping=True)
        user = make_user([("5.6.7.10", "Vless", 1), ("5.6.7.99", "Vmess", 1)])
        assert count_devices(user, config) == 2

    def test_different_24_are_two_devices(self):
        config = DeviceCountingConfig(subnet_ip_grouping=True)
        user = make_user([("5.6.7.10", "Vless", 1), ("5.6.9.10", "Vless", 1)])
        assert count_devices(user, config) == 2

    def test_ip_mode_collapses_a_subnet_across_inbounds(self):
        config = DeviceCountingConfig(count_mode=COUNT_MODE_IP, subnet_ip_grouping=True)
        user = make_user([("5.6.7.10", "Vless", 1), ("5.6.7.99", "Vmess", 2)])
        assert count_devices(user, config) == 1

    def test_wide_mode_groups_a_16_when_the_isp_matches(self):
        config = DeviceCountingConfig(subnet_ip_grouping=True, subnet_grouping_mode="/16")
        user = make_user([("5.6.7.10", "Vless", 1), ("5.6.200.4", "Vless", 1)])
        isp_info = {
            "5.6.7.10": {"isp": "Irancell"},
            "5.6.200.4": {"isp": "Irancell"},
        }
        assert count_devices(user, config, isp_info=isp_info) == 1

    def test_wide_mode_keeps_different_isps_apart(self):
        config = DeviceCountingConfig(subnet_ip_grouping=True, subnet_grouping_mode="/16")
        user = make_user([("5.6.7.10", "Vless", 1), ("5.6.200.4", "Vless", 1)])
        isp_info = {
            "5.6.7.10": {"isp": "Irancell"},
            "5.6.200.4": {"isp": "Mobinnet"},
        }
        assert count_devices(user, config, isp_info=isp_info) == 2

    def test_wide_mode_without_isp_info_falls_back_to_24(self):
        # Grouping a whole /16 of unrelated customers would hide real sharing,
        # so an unknown ISP narrows the key instead of widening it.
        config = DeviceCountingConfig(subnet_ip_grouping=True, subnet_grouping_mode="/16")
        user = make_user([("5.6.7.10", "Vless", 1), ("5.6.200.4", "Vless", 1)])
        assert count_devices(user, config) == 2

    def test_org_is_used_when_isp_is_missing(self):
        config = DeviceCountingConfig(subnet_ip_grouping=True, subnet_grouping_mode="/16")
        user = make_user([("5.6.7.10", "Vless", 1), ("5.6.200.4", "Vless", 1)])
        isp_info = {
            "5.6.7.10": {"org": "AS44244 Irancell"},
            "5.6.200.4": {"isp": "", "org": "AS44244 Irancell"},
        }
        assert count_devices(user, config, isp_info=isp_info) == 1


class TestHighTrustGrouping:
    """Trusted users are allowed to look like WiFi/mobile switching."""

    def test_trusted_user_collapses_ips_per_inbound(self):
        config = DeviceCountingConfig(high_trust_ip_grouping=True, high_trust_threshold=20)
        user = make_user(
            [
                ("5.6.7.8", "Vless", 1),
                ("9.9.9.9", "Vless", 1),
                ("4.4.4.4", "Vless", 2),
            ]
        )
        assert count_devices(user, config, trust_score=50.0) == 1

    def test_below_the_threshold_nothing_is_grouped(self):
        config = DeviceCountingConfig(high_trust_ip_grouping=True, high_trust_threshold=20)
        user = make_user([("5.6.7.8", "Vless", 1), ("9.9.9.9", "Vless", 1)])
        assert count_devices(user, config, trust_score=19.0) == 2

    def test_threshold_is_inclusive(self):
        config = DeviceCountingConfig(high_trust_ip_grouping=True, high_trust_threshold=20)
        user = make_user([("5.6.7.8", "Vless", 1), ("9.9.9.9", "Vless", 1)])
        assert count_devices(user, config, trust_score=20.0) == 1

    def test_disabled_setting_ignores_a_high_score(self):
        config = DeviceCountingConfig(high_trust_ip_grouping=False, high_trust_threshold=20)
        user = make_user([("5.6.7.8", "Vless", 1), ("9.9.9.9", "Vless", 1)])
        assert count_devices(user, config, trust_score=90.0) == 2

    def test_high_trust_is_inactive_in_ip_mode(self):
        # In ip mode the address itself is the device, so trust cannot merge two
        # different addresses into one.
        config = DeviceCountingConfig(
            count_mode=COUNT_MODE_IP, high_trust_ip_grouping=True, high_trust_threshold=20
        )
        user = make_user([("5.6.7.8", "Vless", 1), ("9.9.9.9", "Vless", 1)])
        assert count_devices(user, config, trust_score=90.0) == 2

    def test_high_trust_takes_precedence_over_subnet_grouping(self):
        config = DeviceCountingConfig(
            high_trust_ip_grouping=True, high_trust_threshold=20, subnet_ip_grouping=True
        )
        user = make_user([("5.6.7.8", "Vless", 1), ("9.9.9.9", "Vless", 1)])
        assert count_devices(user, config, trust_score=90.0) == 1


class TestNoConnectionInfo:
    """Without connection detail the unique-IP count is all there is."""

    def test_user_without_connections_uses_the_fallback(self):
        user = UserType(name="user1", ip=["5.6.7.8", "9.9.9.9"])
        assert count_devices(user, DeviceCountingConfig(), fallback_count=2) == 2

    def test_missing_user_uses_the_fallback(self):
        assert count_devices(None, DeviceCountingConfig(), fallback_count=3) == 3

    def test_fallback_defaults_to_zero(self):
        assert count_devices(None) == 0

    def test_config_is_optional(self):
        user = make_user([("5.6.7.8", "Vless", 1), ("5.6.7.8", "Vmess", 2)])
        # Defaults are device mode with no grouping.
        assert count_devices(user) == 2


class TestFromConfig:
    """The dataclass is the only place the config keys are read."""

    def test_defaults(self):
        config = DeviceCountingConfig.from_config({})
        assert config.count_mode == COUNT_MODE_DEVICE
        assert config.subnet_grouping_mode == "/24"
        assert config.high_trust_threshold == 20
        assert config.subnet_ip_grouping is False
        assert config.high_trust_ip_grouping is False
        assert config.cdn_inbounds == []
        assert config.cdn_nodes == []
        assert config.disabled_nodes == []

    def test_reads_every_key(self):
        config = DeviceCountingConfig.from_config(
            {
                "cdn_inbounds": ["CDN_WS"],
                "cdn_nodes": [5],
                "disabled_nodes": [9],
                "subnet_ip_grouping": True,
                "subnet_grouping_mode": "/16",
                "high_trust_ip_grouping": True,
                "high_trust_threshold": 35,
                "device_count_mode": "ip",
            }
        )
        assert config.count_mode == COUNT_MODE_IP
        assert config.cdn_inbounds == ["CDN_WS"]
        assert config.cdn_nodes == [5]
        assert config.disabled_nodes == [9]
        assert config.subnet_grouping_mode == "/16"
        assert config.high_trust_threshold == 35

    def test_null_lists_become_empty_lists(self):
        config = DeviceCountingConfig.from_config(
            {"cdn_inbounds": None, "cdn_nodes": None, "disabled_nodes": None}
        )
        assert (config.cdn_inbounds, config.cdn_nodes, config.disabled_nodes) == ([], [], [])


class TestSubnetKeys:
    """The two subnet widths used by the grouping rules."""

    def test_24_key_drops_the_last_octet(self):
        assert subnet_key("5.6.7.8") == "5.6.7"

    def test_16_key_keeps_two_octets(self):
        assert wide_subnet_key("5.6.7.8") == "5.6"

    def test_malformed_address_is_its_own_key(self):
        assert subnet_key("not-an-ip") == "not-an-ip"
        assert wide_subnet_key("not-an-ip") == "not-an-ip"

    def test_ipv6_is_never_truncated(self):
        address = "2a01:5ec0:5011:9962:d8ed:c723:c32:ac2a"
        assert subnet_key(address) == address
        assert wide_subnet_key(address) == address


class TestGroupIpsBySubnet:
    """Display grouping: a CDN must not flood the report with addresses."""

    def test_single_ip_is_shown_as_is(self):
        assert group_ips_by_subnet(["5.6.7.8"]) == (["5.6.7.8"], {"5.6.7.8": ["5.6.7.8"]})

    def test_two_ips_in_one_subnet_stay_individual(self):
        formatted, mapping = group_ips_by_subnet(["5.6.7.8", "5.6.7.9"])
        assert formatted == ["5.6.7.8", "5.6.7.9"]
        assert mapping == {"5.6.7.8": ["5.6.7.8"], "5.6.7.9": ["5.6.7.9"]}

    def test_three_ips_in_one_subnet_collapse(self):
        ips = ["5.6.7.8", "5.6.7.9", "5.6.7.10"]
        formatted, mapping = group_ips_by_subnet(ips)
        assert formatted == ["5.6.7.x (3)"]
        assert mapping == {"5.6.7.x (3)": ips}

    def test_mixed_subnets(self):
        formatted, mapping = group_ips_by_subnet(
            ["5.6.7.1", "5.6.7.2", "5.6.7.3", "9.9.9.9"]
        )
        assert formatted == ["5.6.7.x (3)", "9.9.9.9"]
        assert mapping["9.9.9.9"] == ["9.9.9.9"]

    def test_malformed_address_is_kept(self):
        assert group_ips_by_subnet(["..."]) == (["..."], {"...": ["..."]})

    def test_empty_input(self):
        assert group_ips_by_subnet([]) == ([], {})


class TestCountDevicesAndDetails:
    """The combined entry point used by the report path."""

    def test_details_are_skipped_when_not_requested(self):
        user = make_user([("5.6.7.8", "Vless", 1), ("5.6.7.8", "Vmess", 1)])
        info = EnhancedUserInfo(user=user, formatted_ips=["5.6.7.8"])
        assert count_devices_and_details(info, user, False) == ([], 2)

    def test_detail_line_shows_node_and_inbound(self):
        user = make_user([("5.6.7.8", "Vless", 1)])
        info = EnhancedUserInfo(user=user, formatted_ips=["5.6.7.8 (Irancell, IR)"])
        details, count = count_devices_and_details(info, user, True)
        assert count == 1
        assert details == ["  • 5.6.7.8 (Irancell, IR) → node-1(1) | Vless"]

    def test_detail_line_lists_several_inbounds_of_one_ip(self):
        user = make_user([("5.6.7.8", "Vless", 1), ("5.6.7.8", "Vmess", 1)])
        info = EnhancedUserInfo(user=user, formatted_ips=["5.6.7.8"])
        details, count = count_devices_and_details(info, user, True)
        assert count == 2
        assert len(details) == 1
        assert details[0].startswith("  • 5.6.7.8 → node-1(1) | [")
        assert "Vless" in details[0] and "Vmess" in details[0]

    def test_without_connections_the_formatted_ips_are_counted(self):
        user = UserType(name="user1", ip=["5.6.7.8", "9.9.9.9"])
        info = EnhancedUserInfo(user=user, formatted_ips=["5.6.7.8", "9.9.9.9"])
        assert count_devices_and_details(info, user, True) == ([], 2)

    def test_details_skip_ips_the_report_does_not_list(self):
        user = make_user([("5.6.7.8", "Vless", 1), ("9.9.9.9", "Vless", 1)])
        info = EnhancedUserInfo(
            user=UserType(name="user1", ip=["5.6.7.8"]), formatted_ips=["5.6.7.8"]
        )
        assert build_ip_details(info, user) == ["  • 5.6.7.8 → node-1(1) | Vless"]

    def test_details_of_a_user_without_connections_are_empty(self):
        user = UserType(name="user1", ip=["5.6.7.8"])
        info = EnhancedUserInfo(user=user, formatted_ips=["5.6.7.8"])
        assert build_ip_details(info, user) == []
