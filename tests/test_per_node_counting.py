"""Per Node counts factual node identities, independent of IP grouping."""

import pytest

from test_device_count import make_user
from utils.device_count import DeviceCountingConfig, count_devices, count_devices_and_details, count_devices_from_ips
from utils.types import EnhancedUserInfo, UserType


def node_config(**options):
    return DeviceCountingConfig(count_mode="node", eligible_node_ids=frozenset({0, 1, 2}), **options)


def test_multiple_ips_and_inbounds_on_one_node_count_once():
    user = make_user([("5.6.7.8", "Vless", 1), ("9.9.9.9", "Vmess", 1), ("4.4.4.4", "Unknown", 1)])
    assert count_devices(user, node_config()) == 1


def test_same_ip_on_two_nodes_counts_twice_despite_all_grouping_rules():
    user = make_user([("5.6.7.8", "Vless", 1), ("5.6.7.8", "Vless", 2)])
    config = node_config(cdn_nodes=[1, 2], cdn_inbounds=["Vless"], subnet_ip_grouping=True, high_trust_ip_grouping=True)
    assert count_devices(user, config, trust_score=100) == 2


@pytest.mark.parametrize("node_id", [None, True, False, "1", 1.0, 99, "invalid"])
def test_unproven_node_ids_contribute_nothing(node_id):
    user = make_user([("5.6.7.8", "Vless", node_id)])
    assert count_devices(user, node_config(), fallback_count=7) == 0


def test_authoritative_zero_node_id_is_counted_and_disabled_nodes_are_excluded():
    user = make_user([("5.6.7.8", "Vless", 0), ("9.9.9.9", "Vmess", 2)])
    assert count_devices(user, node_config(disabled_nodes=[2])) == 1


def test_missing_node_authority_does_not_count_connection_ids():
    user = make_user([("5.6.7.8", "Vless", 1)])
    assert count_devices(user, DeviceCountingConfig(count_mode="node"), fallback_count=7) == 0


def test_boolean_in_eligible_set_cannot_authorize_an_integer_node():
    user = make_user([("5.6.7.8", "Vless", 1)])
    assert count_devices(user, DeviceCountingConfig(count_mode="node", eligible_node_ids=frozenset({True}))) == 0


@pytest.mark.parametrize("user", [None, UserType(name="alice", ip=["5.6.7.8", "9.9.9.9"])])
def test_no_node_connections_means_no_ip_fallback(user):
    assert count_devices(user, DeviceCountingConfig(count_mode="node"), fallback_count=7) == 0
    assert count_devices_from_ips(["5.6.7.8", "9.9.9.9"], DeviceCountingConfig(count_mode="node")) == 0


@pytest.mark.parametrize("details", [False, True])
def test_report_uses_the_same_node_count_with_or_without_ip_details(details):
    user = make_user([("5.6.7.8", "Vless", 1), ("5.6.7.8", "Vless", 2)])
    info = EnhancedUserInfo(user=user, formatted_ips=["5.6.7.8"])
    _lines, count = count_devices_and_details(info, user, details, device_config=node_config())
    assert count == 2


@pytest.mark.parametrize("ips", [[], ["5.6.7.8"]])
def test_connectionless_report_never_uses_display_strings(ips):
    user = UserType(name="alice", ip=ips)
    info = EnhancedUserInfo(user=user, formatted_ips=["5.6.7.8", "9.9.9.9"])
    assert count_devices_and_details(info, user, device_config=DeviceCountingConfig(count_mode="node")) == ([], 0)
