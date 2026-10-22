"""Real access logs must preserve inbound tags for counting."""

import pytest

from utils.device_count import COUNT_MODE_INBOUND, DeviceCountingConfig, count_devices
from utils.parse_logs import parse_logs


@pytest.mark.parametrize("first_separator,second_separator", [(">>", ">>"), ("->", "->"), (">>", "->"), ("->", ">>")])
async def test_access_log_separators_preserve_distinct_inbounds(first_separator, second_separator):
    logs = (
        "2026/10/22 01:48:32 151.232.190.86:57288 accepted tcp:example.com:443 "
        f"[REALITY TCP 0 {first_separator} IPv4] email: 22.alice\n"
        "2026/10/22 01:48:33 [2a01:5ec0:5011:9962:d8ed:c723:c32:ac2a]:62316 accepted tcp:example.com:443 "
        f"[GRPC 6 {second_separator} DIRECT] email: 22.alice"
    )

    users = await parse_logs(logs, node_id=1, node_name="node-1", config_data={})

    assert users["alice"].device_info.inbound_protocols == {"REALITY TCP 0", "GRPC 6"}
    assert count_devices(users["alice"], DeviceCountingConfig(count_mode=COUNT_MODE_INBOUND)) == 2
