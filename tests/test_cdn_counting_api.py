"""CDN deduplication must apply to real per-node API observations."""

import time

import pytest

from utils.device_count import COUNT_MODE_DEVICE, COUNT_MODE_IP, DeviceCountingConfig, count_devices
from utils.ip_source_api import _build_users_from_payloads


@pytest.mark.parametrize("mode", [COUNT_MODE_DEVICE, COUNT_MODE_IP])
@pytest.mark.parametrize("direct_ips, expected", [(["5.6.7.8"], 1), (["5.6.7.8", "9.9.9.9"], 2)])
async def test_api_cdn_overlap_preserves_distinct_direct_devices(mode, direct_ips, expected):
    now = int(time.time())
    config_data = {
        "country_code": "",
        "check_interval": 60,
        "api_ip_sentinel_inbound": "API",
        "cdn_nodes": [5],
        "device_count_mode": mode,
    }
    payloads = {"alice": {5: {"5.6.7.8": now}, 1: {ip: now for ip in direct_ips}}}
    users, _ = await _build_users_from_payloads(payloads, {}, {5: "cdn-node", 1: "direct-node"}, set(), config_data)

    user = users["alice"]
    assert {connection.node_id for connection in user.device_info.connections} == {1, 5}
    assert count_devices(user, DeviceCountingConfig.from_config(config_data)) == expected
