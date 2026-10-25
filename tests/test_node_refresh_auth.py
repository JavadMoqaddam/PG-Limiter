"""Refreshing node data preserves the normal token cache and 401 recovery."""

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import parse_qs

import httpx
import pytest

from utils.panel_api import auth, nodes, request_helper
from utils.types import NodeType, PanelType


@pytest.fixture
async def node_refresh_http(monkeypatch):
    """Exercise the production transport chain with isolated caches and HTTP."""
    panel = PanelType("test-admin", "test-password", "https://panel.invalid")
    state = SimpleNamespace(panel=panel, requests=[], reject_cached_token=False, node_status=200)
    token_cache = {
        "token": "cached-token",
        "expires_at": time.time() + 3600,
        "panel_domain": panel.panel_domain,
    }
    node_cache = {
        "nodes": [NodeType(1, "relay", "192.0.2.1", "error", "old status")],
        "expires_at": time.time() + 3600,
        "panel_domain": panel.panel_domain,
        "enabled_only": False,
    }
    state.token_cache = token_cache
    state.node_cache = node_cache

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "panel.invalid"
        state.requests.append((request.method, request.url.path, request.headers.get("Authorization")))
        if request.method == "POST" and request.url.path == "/api/admin/token":
            assert parse_qs(request.content.decode()) == {
                "username": ["test-admin"],
                "password": ["test-password"],
            }
            return httpx.Response(200, json={"access_token": "fresh-token", "token_type": "bearer"})
        assert request.method == "GET" and request.url.path == "/api/nodes"
        if state.reject_cached_token and request.headers["Authorization"] == "Bearer cached-token":
            return httpx.Response(401, json={"detail": "Could not validate credentials"})
        if state.node_status != 200:
            return httpx.Response(state.node_status, json={"detail": "Panel unavailable"})
        return httpx.Response(
            200,
            json=[{"id": 1, "name": "relay", "address": "192.0.2.1", "status": "connected", "message": ""}],
        )

    monkeypatch.setattr(auth, "_token_cache", token_cache)
    monkeypatch.setattr(auth, "_auth_token_lock", asyncio.Lock())
    monkeypatch.setattr(nodes, "_nodes_cache", node_cache)
    monkeypatch.setattr(request_helper, "circuit_breaker", request_helper.PanelCircuitBreaker())
    monkeypatch.setattr(request_helper, "_panel_health", {**request_helper._panel_health, "consecutive_failures": 0})
    monkeypatch.setattr(request_helper, "asyncio", SimpleNamespace(sleep=AsyncMock()))
    for module, logger_name in ((auth, "auth_logger"), (nodes, "nodes_logger"), (request_helper, "request_logger")):
        monkeypatch.setattr(module, logger_name, MagicMock())
    monkeypatch.setattr(auth, "log_api_request", MagicMock())
    monkeypatch.setattr(request_helper, "log_api_request", MagicMock())
    monkeypatch.setattr(auth, "safe_send_logs_panel", AsyncMock())
    monkeypatch.setattr(nodes, "safe_send_logs_panel", AsyncMock())
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        monkeypatch.setattr(request_helper, "_panel_client", client)
        yield state


async def test_forced_node_refresh_reuses_valid_token(node_refresh_http):
    state = node_refresh_http

    result = await nodes.get_nodes(state.panel, force_refresh=True, enabled_only=False)

    assert result[0].status == "connected"
    assert state.node_cache["nodes"][0].status == "connected"
    assert state.requests == [("GET", "/api/nodes", "Bearer cached-token")]
    assert state.token_cache["token"] == "cached-token"


async def test_forced_node_refresh_acquires_expired_token(node_refresh_http):
    state = node_refresh_http
    state.token_cache["expires_at"] = 0

    result = await nodes.get_nodes(state.panel, force_refresh=True, enabled_only=False)

    assert result[0].status == "connected"
    assert state.requests == [
        ("POST", "/api/admin/token", None),
        ("GET", "/api/nodes", "Bearer fresh-token"),
    ]
    assert state.token_cache["token"] == "fresh-token"
    assert state.token_cache["expires_at"] > time.time()


async def test_forced_node_refresh_replays_rejected_cached_token(node_refresh_http):
    state = node_refresh_http
    state.reject_cached_token = True

    result = await nodes.get_nodes(state.panel, force_refresh=True, enabled_only=False)

    assert result[0].status == "connected"
    assert state.requests == [
        ("GET", "/api/nodes", "Bearer cached-token"),
        ("POST", "/api/admin/token", None),
        ("GET", "/api/nodes", "Bearer fresh-token"),
    ]
    assert state.token_cache["token"] == "fresh-token"


async def test_forced_node_refresh_preserves_token_after_server_failure(node_refresh_http):
    state = node_refresh_http
    state.node_status = 500

    with pytest.raises(ValueError, match="Failed to get nodes"):
        await nodes.get_nodes(state.panel, force_refresh=True, enabled_only=False)

    assert state.requests == [("GET", "/api/nodes", "Bearer cached-token")] * 3
    assert state.token_cache["token"] == "cached-token"
    assert state.node_cache["nodes"][0].status == "error"


async def test_unforced_node_refresh_uses_valid_node_cache(node_refresh_http):
    state = node_refresh_http

    result = await nodes.get_nodes(state.panel, enabled_only=False)

    assert result[0].status == "error"
    assert state.requests == []
