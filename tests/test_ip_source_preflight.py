"""API source preflight needs one real user while preserving its live permission gate."""

import asyncio
from types import SimpleNamespace

import httpx
import pytest

from telegram_bot.handlers import settings_ip_source as settings
from test_inbound_mode_settings import LocalQuery, settings_store  # noqa: F401
from utils import read_config as config_module
from utils.panel_api import request_helper
from utils.types import PanelType


@pytest.fixture
async def panel_http(monkeypatch):
    """Keep the real auth/request helpers and replace only external HTTP I/O."""
    calls = []
    routes = {}

    def respond(request):
        calls.append(request)
        assert request.url.path != "/api/admin/token"
        assert request.headers["X-Api-Key"] == "test-panel-key"
        assert "Authorization" not in request.headers
        reply = routes[request.url.path]
        if callable(reply):
            reply = reply(request)
        if isinstance(reply, httpx.Response):
            return reply
        return httpx.Response(200, json=reply)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:

        async def get_client():
            return client

        monkeypatch.setattr(request_helper, "get_panel_client", get_client)
        monkeypatch.setattr(request_helper, "circuit_breaker", request_helper.PanelCircuitBreaker())
        monkeypatch.setattr(request_helper, "_panel_health", dict(request_helper._panel_health))
        monkeypatch.setenv("PANEL_DOMAIN", "https://panel.test")
        monkeypatch.setenv("PANEL_API_KEY", "test-panel-key")
        yield SimpleNamespace(panel=PanelType("https://panel.test", "test-panel-key"), calls=calls, routes=routes)


async def test_preflight_fetches_only_one_user_from_a_large_online_list(panel_http):
    def users(request):
        offset = int(request.url.params["offset"])
        limit = int(request.url.params["limit"])
        return {
            "users": [{"id": i + 7, "username": f"user_{i}"} for i in range(offset, min(offset + limit, 1951))],
            "total": 1951,
        }

    panel_http.routes["/api/users"] = users
    config = {"group_filter": {"enabled": True, "mode": "include", "group_ids": [7, 9]}}

    assert await settings._find_preflight_user_id(panel_http.panel, config) == (7, "")
    queries = [request for request in panel_http.calls if request.url.path == "/api/users"]
    assert len(queries) == 1
    assert queries[0].url.params.multi_items() == [
        ("offset", "0"),
        ("limit", "1"),
        ("status", "active"),
        ("group", "7"),
        ("group", "9"),
        ("online", "true"),
    ]
    assert queries[0].extensions["timeout"]["read"] == 60.0


async def test_preflight_falls_back_only_when_online_users_are_confirmed_empty(panel_http):
    panel_http.routes["/api/users"] = lambda request: (
        {"users": [], "total": 0}
        if request.url.params.get("online") == "true"
        else {"users": [{"id": 7, "username": "alice"}], "total": 1}
    )
    config = {"group_filter": {"enabled": True, "mode": "include", "group_ids": [9]}}

    assert await settings._find_preflight_user_id(panel_http.panel, config) == (7, "")
    queries = [request.url.params for request in panel_http.calls if request.url.path == "/api/users"]
    assert [dict(query) for query in queries] == [
        {"offset": "0", "limit": "1", "status": "active", "group": "9", "online": "true"},
        {"offset": "0", "limit": "1", "status": "active", "group": "9"},
    ]


@pytest.mark.parametrize(
    "body",
    [
        None,
        [],
        {},
        {"users": None, "total": 0},
        {"users": {}, "total": 0},
        {"users": []},
        {"users": [], "total": True},
        {"users": [], "total": "0"},
        {"users": [], "total": 0.0},
        {"users": [], "total": -1},
        {"users": [], "total": 1},
        {"users": [{"id": 7, "username": "alice"}], "total": 0},
        {"users": [None], "total": 1},
        {"users": [{"id": 7}], "total": 1},
        {"users": [{"id": 7, "username": None}], "total": 1},
        {"users": [{"id": 7, "username": "  "}], "total": 1},
        {"users": [{"id": 7, "username": "alice"}, {"id": 8, "username": "bob"}], "total": 2},
    ],
)
async def test_malformed_user_page_fails_without_wider_fallback(panel_http, body):
    panel_http.routes["/api/users"] = body

    user_id, error = await settings._find_preflight_user_id(panel_http.panel, {})

    assert user_id == 0
    assert error
    assert len([request for request in panel_http.calls if request.url.path == "/api/users"]) == 1


@pytest.mark.parametrize("user_id", [True, False, 1.5, "7", None, 0, -1])
async def test_preflight_rejects_ids_that_are_not_positive_integers(panel_http, user_id):
    panel_http.routes["/api/users"] = {"users": [{"id": user_id, "username": "alice"}], "total": 1}

    target, error = await settings._find_preflight_user_id(panel_http.panel, {})

    assert target == 0
    assert error
    assert len([request for request in panel_http.calls if request.url.path == "/api/users"]) == 1


@pytest.mark.parametrize("status", [201, 204, 401, 404])
async def test_preflight_rejects_non_200_even_with_valid_user_json(panel_http, status):
    panel_http.routes["/api/users"] = httpx.Response(
        status, json={"users": [{"id": 7, "username": "alice"}], "total": 1}
    )

    user_id, error = await settings._find_preflight_user_id(panel_http.panel, {})

    assert user_id == 0
    assert error
    queries = [request for request in panel_http.calls if request.url.path == "/api/users"]
    assert all(request.url.params.get("online") == "true" for request in queries)


async def test_preflight_rejects_invalid_json_without_wider_fallback(panel_http):
    panel_http.routes["/api/users"] = httpx.Response(200, content=b"not json")

    user_id, error = await settings._find_preflight_user_id(panel_http.panel, {})

    assert user_id == 0
    assert error
    assert len([request for request in panel_http.calls if request.url.path == "/api/users"]) == 1


@pytest.mark.parametrize("failure", [None, RuntimeError("query unavailable")])
async def test_preflight_returns_failure_for_unavailable_query(panel_http, monkeypatch, failure):
    calls = []

    async def unavailable(*args, **kwargs):
        calls.append(kwargs["params"])
        if isinstance(failure, Exception):
            raise failure
        return None

    monkeypatch.setattr(request_helper, "panel_get", unavailable)

    user_id, error = await settings._find_preflight_user_id(panel_http.panel, {})

    assert user_id == 0
    assert error
    assert len(calls) == 1


@pytest.fixture
async def node_logs_config(settings_store, panel_http):
    assert await config_module.save_config_value("device_count_mode", "node")
    assert await config_module.save_config_value("ip_source", "logs")


@pytest.mark.parametrize(
    "response, allowed",
    [
        (httpx.Response(200, json={"nodes": {}}), True),
        (httpx.Response(200, json={"nodes": {"1": {"ips": {}}}}), True),
        (httpx.Response(200, json={}), False),
        (httpx.Response(404, json={"detail": "User not found"}), True),
        (httpx.Response(404, json={"detail": "Not Found"}), False),
        (httpx.Response(404, content=b"not json"), False),
        (httpx.Response(401, json={"detail": "Unauthorized"}), False),
        (httpx.Response(403, json={"detail": "Forbidden"}), False),
        (httpx.Response(503, json={"detail": "Unavailable"}), False),
    ],
)
async def test_source_switch_keeps_the_real_endpoint_permission_gate(node_logs_config, panel_http, response, allowed):
    panel_http.routes["/api/users"] = {"users": [{"id": 7, "username": "alice"}], "total": 1}
    panel_http.routes["/api/node/online_stats/7/ip"] = response
    query = LocalQuery()

    await settings.handle_ip_source_set_api_callback(query, SimpleNamespace(user_data={}))

    config = await config_module.read_config()
    assert config["ip_source"] == ("api" if allowed else "logs")
    assert config["device_count_mode"] == "node"
    assert len([request for request in panel_http.calls if request.url.path == "/api/node/online_stats/7/ip"]) == 1
    assert any("✅" in answer for answer in query.answers) is allowed


@pytest.mark.parametrize("body", [{"users": [], "total": 0}, {"users": [], "total": 1}, {}])
async def test_no_probe_target_preserves_logs_without_endpoint_request(node_logs_config, panel_http, body):
    panel_http.routes["/api/users"] = body
    query = LocalQuery()

    await settings.handle_ip_source_set_api_callback(query, SimpleNamespace(user_data={}))

    config = await config_module.read_config()
    assert config["ip_source"] == "logs"
    assert config["device_count_mode"] == "node"
    assert not [request for request in panel_http.calls if request.url.path.startswith("/api/node/")]
    queries = [request for request in panel_http.calls if request.url.path == "/api/users"]
    assert len(queries) == (2 if body == {"users": [], "total": 0} else 1)
    assert not any("✅" in answer for answer in query.answers)


async def test_open_circuit_breaker_preserves_logs_without_http(node_logs_config, panel_http, monkeypatch):
    breaker = request_helper.circuit_breaker
    breaker.state = breaker.STATE_OPEN

    await settings.handle_ip_source_set_api_callback(LocalQuery(), SimpleNamespace(user_data={}))

    assert (await config_module.read_config())["ip_source"] == "logs"
    assert panel_http.calls == []


async def test_list_401_preserves_source_without_login_fallback(node_logs_config, panel_http):
    attempts = []

    def users(request):
        attempts.append(request)
        if len(attempts) == 1:
            return httpx.Response(401, json={"detail": "Unauthorized"})
        return {"users": [{"id": 7, "username": "alice"}], "total": 1}

    panel_http.routes["/api/users"] = users
    panel_http.routes["/api/node/online_stats/7/ip"] = {"nodes": {}}

    await settings.handle_ip_source_set_api_callback(LocalQuery(), SimpleNamespace(user_data={}))

    assert (await config_module.read_config())["ip_source"] == "logs"
    assert not [request for request in panel_http.calls if request.url.path == "/api/admin/token"]
    assert len(attempts) == 1


@pytest.mark.parametrize("path", ["/api/users", "/api/node/online_stats/7/ip"])
async def test_cancelled_preflight_propagates_without_changing_source(node_logs_config, panel_http, path):
    def cancelled(request):
        raise asyncio.CancelledError

    panel_http.routes["/api/users"] = {"users": [{"id": 7, "username": "alice"}], "total": 1}
    panel_http.routes[path] = cancelled

    with pytest.raises(asyncio.CancelledError):
        await settings.handle_ip_source_set_api_callback(LocalQuery(), SimpleNamespace(user_data={}))

    assert (await config_module.read_config())["ip_source"] == "logs"


async def test_runtime_candidates_still_fetch_every_page(panel_http):
    from utils.panel_api.online_ips import fetch_online_candidates

    panel_http.routes["/api/users"] = lambda request: {
        "users": [{"id": 1, "username": "alice"}]
        if request.url.params["offset"] == "0"
        else [{"id": 2, "username": "bob"}],
        "total": 2,
    }

    candidates = await fetch_online_candidates(panel_http.panel, online_window=110, page_size=1)

    assert candidates == [{"id": 1, "username": "alice"}, {"id": 2, "username": "bob"}]
    assert [request.url.params["offset"] for request in panel_http.calls if request.url.path == "/api/users"] == [
        "0",
        "1",
    ]
