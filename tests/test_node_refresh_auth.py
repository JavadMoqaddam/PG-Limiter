"""Node refresh uses the configured API key without an authentication retry."""

import asyncio
import time
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from utils.panel_api import nodes, request_helper
from utils.types import NodeType, PanelType


@pytest.fixture
async def node_refresh_http(monkeypatch):
    """Exercise real panel requests with an isolated node cache and HTTP transport."""
    panel = PanelType("https://panel.invalid", "test-api-key")
    node_cache = {
        "nodes": [NodeType(1, "relay", "192.0.2.1", "error", "old status")],
        "expires_at": time.time() + 3600,
        "panel_domain": panel.panel_domain,
        "enabled_only": False,
    }
    state = SimpleNamespace(panel=panel, requests=[], node_status=200, node_cache=node_cache, cancel_request=False)

    def respond(request: httpx.Request) -> httpx.Response:
        state.requests.append(
            (
                request.method,
                request.url.scheme,
                request.url.path,
                request.headers.get("X-Api-Key"),
                request.headers.get("Authorization"),
            )
        )
        assert request.url.host == "panel.invalid"
        assert request.method == "GET" and request.url.path == "/api/nodes"
        if state.cancel_request:
            raise asyncio.CancelledError
        if state.node_status != 200:
            return httpx.Response(state.node_status, json={"detail": "Panel request failed"})
        return httpx.Response(
            200,
            json=[{"id": 1, "name": "relay", "address": "192.0.2.1", "status": "connected", "message": ""}],
        )

    monkeypatch.setattr(nodes, "_nodes_cache", node_cache)
    monkeypatch.setattr(request_helper, "circuit_breaker", request_helper.PanelCircuitBreaker())
    monkeypatch.setattr(
        request_helper,
        "_panel_health",
        {**request_helper._panel_health, "consecutive_failures": 0, "panel_available": True},
    )
    monkeypatch.setattr(request_helper.asyncio, "sleep", AsyncMock())
    monkeypatch.setattr(nodes, "nodes_logger", MagicMock())
    monkeypatch.setattr(request_helper, "request_logger", MagicMock())
    monkeypatch.setattr(request_helper, "log_api_request", MagicMock())
    monkeypatch.setattr(nodes, "safe_send_logs_panel", AsyncMock())
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        monkeypatch.setattr(request_helper, "_panel_client", client)
        yield state


async def test_forced_node_refresh_uses_api_key_without_login(node_refresh_http):
    state = node_refresh_http

    result = await nodes.get_nodes(state.panel, force_refresh=True, enabled_only=False)

    assert result[0].status == "connected"
    assert state.node_cache["nodes"][0].status == "connected"
    assert state.requests == [("GET", "https", "/api/nodes", "test-api-key", None)]


@pytest.mark.parametrize("status", [401, 403])
async def test_forced_node_refresh_does_not_retry_rejected_api_key(node_refresh_http, status):
    state = node_refresh_http
    state.node_status = status
    original_cache = deepcopy(state.node_cache)

    with pytest.raises(ValueError):
        await nodes.get_nodes(state.panel, force_refresh=True, enabled_only=False)

    assert state.requests == [("GET", "https", "/api/nodes", "test-api-key", None)]
    assert state.panel.panel_api_key == "test-api-key"
    assert state.node_cache == original_cache


async def test_forced_node_refresh_preserves_key_and_node_cache_after_server_failure(node_refresh_http):
    state = node_refresh_http
    state.node_status = 500
    original_cache = deepcopy(state.node_cache)

    with pytest.raises(ValueError, match="Failed to get nodes"):
        await nodes.get_nodes(state.panel, force_refresh=True, enabled_only=False)

    assert state.requests == [("GET", "https", "/api/nodes", "test-api-key", None)] * 3
    assert state.panel.panel_api_key == "test-api-key"
    assert state.node_cache == original_cache


async def test_unforced_node_refresh_uses_valid_node_cache(node_refresh_http):
    state = node_refresh_http

    result = await nodes.get_nodes(state.panel, enabled_only=False)

    assert result[0].status == "error"
    assert state.requests == []


async def test_forced_node_refresh_propagates_cancellation(node_refresh_http):
    state = node_refresh_http
    state.cancel_request = True
    original_cache = deepcopy(state.node_cache)

    with pytest.raises(asyncio.CancelledError):
        await nodes.get_nodes(state.panel, force_refresh=True, enabled_only=False)

    assert state.requests == [("GET", "https", "/api/nodes", "test-api-key", None)]
    assert state.panel.panel_api_key == "test-api-key"
    assert state.node_cache == original_cache
