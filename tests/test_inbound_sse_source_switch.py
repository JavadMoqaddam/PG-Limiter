"""API source changes park SSE and retire the previous source's evidence."""

import asyncio
import time
from datetime import datetime, timezone
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import utils.get_logs as collector
import utils.parse_logs as parser
import utils.read_config as configuration
from utils.device_count import DeviceCountingConfig, count_devices
from utils.log_observations import LOG_OBSERVATIONS
from utils.shared_state import ACTIVE_USERS, NODE_LAST_EVENT
from utils.types import NodeType, PanelType


@pytest.mark.parametrize("mode", ["inbound", " INBOUND ", "device", "ip"])
async def test_api_source_switch_retires_observed_samples(monkeypatch, mode, operational_topics):
    """Exercise the live collector, parser and clearing path with an external stream double."""
    clock = [datetime(2026, 10, 23, 10, tzinfo=timezone.utc).timestamp()]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    config = {"ip_source": "logs", "device_count_mode": mode, "log_node_timezones": {"7": "UTC"}}
    monkeypatch.setattr(configuration, "_config_cache", config)
    monkeypatch.setattr(configuration, "read_config", AsyncMock(return_value=config))
    monkeypatch.setattr(parser, "_PARSER_CONFIG_CACHE", config)
    monkeypatch.setattr(parser, "_PARSER_CONFIG_CACHE_TIME", time.time())
    monkeypatch.setattr(parser, "_PARSER_CONFIG_REVISION", configuration.get_config_revision())
    monkeypatch.setattr(collector, "_IP_SOURCE_RECHECK_SECONDS", 0)
    monkeypatch.setattr(collector, "get_token", AsyncMock(return_value=SimpleNamespace(panel_token="test-token")))
    monkeypatch.setattr(collector, "_update_node_status", AsyncMock())
    monkeypatch.setattr(parser, "CURRENT_NODE_INFO", {"node_id": None, "node_name": None})
    saved_heartbeats = dict(NODE_LAST_EVENT)
    state = {"opened": 0, "closed": False, "parked": False}
    observed_generation = []

    class StreamResponse:
        status_code = 200

        async def aiter_lines(self):
            clock[0] += 1
            yield (
                "data: 2026/10/23 10:00:01 151.232.190.86:57288 accepted tcp:example.com:443 "
                "[Vless TCP >> DIRECT] email: 22.alice"
            )
            user = ACTIVE_USERS["alice"]
            assert count_devices(user, DeviceCountingConfig(count_mode="inbound")) == 1
            assert user.ip == ["151.232.190.86"]
            assert [(conn.node_id, conn.inbound_protocol) for conn in user.device_info.connections] == [
                (7, "Vless TCP")
            ]
            observed_generation.append(LOG_OBSERVATIONS.generation)
            config["ip_source"] = "api"
            yield (
                "data: 2026/10/23 10:00:02 151.232.190.86:57288 accepted tcp:example.com:443 "
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
        assert len(observed_generation) == 1
        assert LOG_OBSERVATIONS.generation > observed_generation[0]
        assert ACTIVE_USERS == {}
    finally:
        NODE_LAST_EVENT.clear()
        NODE_LAST_EVENT.update(saved_heartbeats)
