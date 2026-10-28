"""Exercise the SSE release target through persisted settings, reports and warnings."""

import asyncio
import json
import os
import sys

import pytest

import utils.read_config as config
from test_config_revisions import settings_db  # noqa: F401
from test_log_enforcement_freshness import fresh_cycle  # noqa: F401
from test_log_partial_enforcement import partial_cycle  # noqa: F401
from test_policy_enforcement import policy_cycle  # noqa: F401
from utils.device_count import DeviceCountingConfig, count_devices_and_details
from utils.log_observations import LOG_OBSERVATIONS
from utils.monitoring_gate import get_last_completed_scan
from utils.shared_state import note_node_event
from utils.types import ConnectionInfo, EnhancedUserInfo


async def save_target():
    for key, value in {
        "ip_source": "logs",
        "device_count_mode": "ip",
        "subnet_ip_grouping": "true",
        "subnet_grouping_mode": "/24",
        "api_ip_freshness": "20",
    }.items():
        assert await config.save_config_value(key, value) is True


async def test_target_settings_survive_a_new_process(settings_db):
    await save_target()
    script = """
import asyncio, json
from utils.read_config import read_config
from db.database import close_db
async def read():
    try:
        cfg = await read_config(refresh=True)
        print(json.dumps({key: cfg[key] for key in (
            'ip_source', 'device_count_mode', 'subnet_ip_grouping',
            'subnet_grouping_mode', 'api_ip_freshness')}))
    finally:
        await close_db()
asyncio.run(read())
"""
    child = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        script,
        env={**os.environ, "DATABASE_URL": settings_db.url, "LOG_LEVEL": "CRITICAL"},
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await asyncio.wait_for(child.communicate(), 10)
    assert child.returncode == 0, err.decode()
    assert json.loads(out) == {
        "ip_source": "logs",
        "device_count_mode": "ip",
        "subnet_ip_grouping": True,
        "subnet_grouping_mode": "/24",
        "api_ip_freshness": 20,
    }


@pytest.mark.parametrize("partial_cycle", ["ip"], indirect=True)
@pytest.mark.parametrize("fresh_cycle", [True], indirect=True)
@pytest.mark.parametrize(
    "addresses,expected",
    [
        (["203.0.113.10", "203.0.113.20"], 1),
        (["203.0.113.10", "203.0.113.20", "203.0.114.10"], 2),
        (["203.0.113.10", "203.0.113.10", "203.0.113.10"], 1),
        (["2001:db8::10", "2001:db8::20"], 2),
    ],
)
async def test_same_ip24_sample_agrees_in_report_and_warning_cycle(fresh_cycle, addresses, expected):
    cycle = fresh_cycle
    await save_target()
    cfg = await config.read_config(refresh=True)
    cycle.config.clear()
    cycle.config.update(cfg)
    LOG_OBSERVATIONS.configure(cfg)
    LOG_OBSERVATIONS.register_fleet(cycle.fleet(recovered=True))
    for node_id in (1, 2, 3):
        note_node_event(node_id)
    cycle.advance(301)
    user = cycle.observed("alice")
    user.ip = list(dict.fromkeys(addresses))
    user.device_info.connections = [
        ConnectionInfo(address, i + 1, f"node-{i + 1}", f"inbound-{i + 1}", cycle.clock[0] - 1)
        for i, address in enumerate(addresses)
    ]
    user.device_info.inbound_protocols = {connection.inbound_protocol for connection in user.device_info.connections}
    counting = DeviceCountingConfig.from_config(cfg, eligible_node_ids=frozenset({1, 2, 3}))
    for details in (False, True):
        _, reported = count_devices_and_details(
            EnhancedUserInfo(user=user, formatted_ips=user.ip), user, details, device_config=counting
        )
        assert reported == expected

    await cycle.run([user])

    assert cycle.applied == []
    if expected > 1:
        assert cycle.system.warnings["alice"].ip_count == expected
        assert cycle.system.warnings["alice"].consecutive_violations == 1
    else:
        assert cycle.system.warnings == {}
    receipt = get_last_completed_scan()
    assert receipt is not None and receipt.coverage_complete
