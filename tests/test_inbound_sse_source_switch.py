"""An incompatible API switch must park SSE without discarding observed inbounds."""

import asyncio
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import utils.get_logs as collector
import utils.parse_logs as parser
import utils.read_config as configuration
from utils.device_count import DeviceCountingConfig, count_devices
from utils.shared_state import ACTIVE_USERS, NODE_LAST_EVENT
from utils.types import NodeType, PanelType


@pytest.mark.parametrize("mode, expected_count", [("inbound", 1), (" INBOUND ", 1), ("device", 0), ("ip", 0)])
async def test_api_source_switch_preserves_only_inbound_samples(monkeypatch, mode, expected_count):
    """Exercise the live collector, parser and clearing path with an external stream double."""
    config = {"ip_source": "logs", "device_count_mode": mode}
    monkeypatch.setattr(configuration, "_config_cache", config)
    monkeypatch.setattr(parser, "_PARSER_CONFIG_CACHE", config)
    monkeypatch.setattr(parser, "_PARSER_CONFIG_CACHE_TIME", time.time())
    monkeypatch.setattr(collector, "_IP_SOURCE_RECHECK_SECONDS", 0)
    monkeypatch.setattr(collector, "get_token", AsyncMock(return_value=SimpleNamespace(panel_token="test-token")))
    monkeypatch.setattr(collector, "_update_node_status", AsyncMock())
    monkeypatch.setattr(parser, "CURRENT_NODE_INFO", {"node_id": None, "node_name": None})
    saved_heartbeats = dict(NODE_LAST_EVENT)
    state = {"opened": 0, "closed": False, "parked": False}

    class StreamResponse:
        status_code = 200

        async def aiter_lines(self):
            yield (
                "data: 2023/07/07 03:09:00 151.232.190.86:57288 accepted tcp:example.com:443 "
                "[Vless TCP >> DIRECT] email: 22.alice"
            )
            config["ip_source"] = "api"
            yield (
                "data: 2023/07/07 03:09:01 151.232.190.86:57288 accepted tcp:example.com:443 "
                "[Vless WS >> DIRECT] email: 22.alice"
            )
            pytest.fail("SSE kept reading after the API source switch")

    class StreamClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

        @asynccontextmanager
        async def stream(self, method, url, **_kwargs):
            assert method == "GET" and url == "https://panel.local/api/node/7/logs"
            state["opened"] += 1
            try:
                yield StreamResponse()
            finally:
                state["closed"] = True

    async def stop_when_parked(_delay):
        assert config["ip_source"] == "api" and state["closed"]
        state["parked"] = True
        raise asyncio.CancelledError

    monkeypatch.setattr(collector.httpx, "AsyncClient", StreamClient)
    monkeypatch.setattr(collector.asyncio, "sleep", stop_when_parked)
    try:
        with pytest.raises(asyncio.CancelledError):
            await collector.get_nodes_logs(
                PanelType("admin", "secret", "panel.local"), NodeType(7, "node-7", "1.2.3.4", "connected")
            )
        assert state == {"opened": 1, "closed": True, "parked": True}
        user = ACTIVE_USERS.get("alice")
        assert count_devices(user, DeviceCountingConfig(count_mode="inbound")) == expected_count
        if expected_count:
            assert user.ip == ["151.232.190.86"]
            assert [(conn.node_id, conn.inbound_protocol) for conn in user.device_info.connections] == [
                (7, "Vless TCP")
            ]
        else:
            assert user is None
    finally:
        NODE_LAST_EVENT.clear()
        NODE_LAST_EVENT.update(saved_heartbeats)
