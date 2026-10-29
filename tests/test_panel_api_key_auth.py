"""The real REST and SSE transports use only the configured panel API key."""

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from utils.panel_api import request_helper

KEY = "pg_key_00000000-0000-4000-8000-000000000001"


@pytest.fixture
async def key_http(monkeypatch):
    state = SimpleNamespace(
        panel=SimpleNamespace(panel_domain="https://panel.invalid", panel_api_key=KEY),
        requests=[],
        status=200,
        body={"nodes": {"1": {"ips": {}}}},
        error=None,
    )

    def respond(request):
        state.requests.append(request)
        if state.error is not None:
            raise state.error
        return httpx.Response(state.status, json=state.body)

    monkeypatch.setattr(request_helper, "circuit_breaker", request_helper.PanelCircuitBreaker())
    monkeypatch.setattr(request_helper, "_panel_health", {**request_helper._panel_health, "consecutive_failures": 0})
    monkeypatch.setattr(request_helper, "asyncio", SimpleNamespace(sleep=AsyncMock()))
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        monkeypatch.setattr(request_helper, "_panel_client", client)
        yield state


@pytest.mark.parametrize("method", ["GET", "POST", "PUT", "DELETE"])
async def test_rest_methods_send_api_key_without_login(key_http, method):
    """A bearer/login fallback would fail the captured HTTP header and request list."""
    state = key_http
    response = await getattr(request_helper, f"panel_{method.lower()}")(state.panel, "/api/users")

    assert response is not None and response.status_code == 200
    assert [(request.method, request.url.path) for request in state.requests] == [(method, "/api/users")]
    assert state.requests[0].headers["X-Api-Key"] == KEY
    assert "Authorization" not in state.requests[0].headers


@pytest.mark.parametrize("status", [401, 403])
async def test_rejected_key_is_returned_without_authentication_retry(key_http, status, caplog):
    state = key_http
    state.status = status
    state.body = {"detail": f"Rejected key {KEY}"}
    with caplog.at_level(logging.DEBUG):
        response = await request_helper.panel_get(state.panel, "/api/nodes", max_retries=3)

    assert response is not None and response.status_code == status
    assert [(request.method, str(request.url)) for request in state.requests] == [
        ("GET", "https://panel.invalid/api/nodes")
    ]
    assert KEY not in caplog.text


@pytest.mark.parametrize("domain,scheme", [("panel.invalid", "https"), ("http://panel.invalid", "http")])
@pytest.mark.parametrize("status", [200, 401, 403])
async def test_availability_probe_uses_key_and_configured_scheme(key_http, domain, scheme, status):
    state = key_http
    state.panel.panel_domain = domain
    state.status = status

    assert await request_helper.check_panel_availability(state.panel)
    assert [str(request.url) for request in state.requests] == [f"{scheme}://panel.invalid/api/"]
    assert state.requests[0].headers["X-Api-Key"] == KEY
    assert "Authorization" not in state.requests[0].headers


@pytest.mark.parametrize("setting,expected_verify", [(None, True), ("true", True), ("false", False), ("invalid", True)])
async def test_rest_tls_verification_setting_reaches_http_client(monkeypatch, setting, expected_verify):
    real_client = httpx.AsyncClient
    options = []

    def client_factory(**kwargs):
        options.append(kwargs)
        return real_client(**kwargs)

    if setting is None:
        monkeypatch.delenv("PANEL_VERIFY_SSL", raising=False)
    else:
        monkeypatch.setenv("PANEL_VERIFY_SSL", setting)
    monkeypatch.setattr(request_helper, "_panel_client", None)
    monkeypatch.setattr(request_helper.httpx, "AsyncClient", client_factory)
    async with await request_helper.get_panel_client():
        assert len(options) == 1
        assert options[0]["verify"] is expected_verify


@pytest.mark.parametrize("error_type", [httpx.ConnectError, RuntimeError])
async def test_external_errors_cannot_disclose_key(key_http, error_type, caplog):
    state = key_http
    state.error = error_type(f"Transport echoed {KEY}")
    with caplog.at_level(logging.DEBUG):
        response, error = await request_helper.panel_request(state.panel, "GET", "/api/users", max_retries=1)

    assert response is None
    assert error_type.__name__ in error
    assert KEY not in error
    assert KEY not in caplog.text
    assert "pg_key_00000000" not in error
    assert "pg_key_00000000" not in caplog.text
    assert [request.url.scheme for request in state.requests] == ["https"]


async def test_node_error_body_is_private(key_http, monkeypatch, caplog):
    from utils.panel_api import nodes

    state = key_http
    state.body = {"detail": f"Invalid API key {KEY}"}
    monkeypatch.setattr(nodes, "safe_send_logs_panel", AsyncMock())
    with caplog.at_level(logging.DEBUG), pytest.raises(ValueError) as rejected:
        await nodes.get_nodes(state.panel, force_refresh=True)

    assert KEY not in str(rejected.value)
    assert KEY not in caplog.text


@pytest.mark.parametrize("operation", ["nodes", "groups", "admins", "users", "details"])
async def test_json_dependency_exception_is_private(key_http, monkeypatch, caplog, operation):
    from utils.panel_api import admins, groups, nodes, users

    class EchoResponse(httpx.Response):
        def json(self, **_kwargs):
            raise ValueError(f"Decoder echoed {KEY}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _request: EchoResponse(200))) as client:
        monkeypatch.setattr(request_helper, "_panel_client", client)
        operations = {
            "nodes": lambda: nodes.get_nodes(key_http.panel, force_refresh=True),
            "groups": lambda: groups.get_groups(key_http.panel, force_refresh=True),
            "admins": lambda: admins.get_admins(key_http.panel, force_refresh=True),
            "users": lambda: users.fetch_all_users_raw(key_http.panel),
            "details": lambda: users.get_user_details(key_http.panel, "alice"),
        }
        with caplog.at_level(logging.DEBUG), pytest.raises(ValueError) as rejected:
            await operations[operation]()

    assert KEY not in str(rejected.value)
    assert KEY not in caplog.text
    assert rejected.value.__cause__ is None


@pytest.mark.parametrize("strict", [False, True])
async def test_later_user_page_decoder_error_keeps_key_private(key_http, monkeypatch, caplog, strict):
    import traceback
    from utils.panel_api import users

    class EchoResponse(httpx.Response):
        def json(self, **_kwargs):
            raise ValueError(f"Decoder echoed {KEY}")

    first_user = {"id": 1, "username": "alice"}

    def respond(request):
        if request.url.params["offset"] == "0":
            return httpx.Response(200, json={"users": [first_user], "total": 2})
        return EchoResponse(200)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        monkeypatch.setattr(request_helper, "_panel_client", client)
        with caplog.at_level(logging.DEBUG):
            if strict:
                with pytest.raises(ValueError) as rejected:
                    await users.fetch_all_users_raw(key_http.panel, limit=1, strict=True)
                assert KEY not in "".join(traceback.format_exception(rejected.value))
            else:
                assert await users.fetch_all_users_raw(key_http.panel, limit=1) == [first_user]
    assert KEY not in caplog.text


@pytest.mark.parametrize("status", [401, 403])
@pytest.mark.parametrize("offset", [0, 1])
@pytest.mark.parametrize("strict", [False, True])
async def test_rejected_key_never_becomes_a_successful_user_list(key_http, monkeypatch, status, offset, strict):
    from utils.panel_api import users

    requests = []

    def respond(request):
        requests.append(request)
        if int(request.url.params["offset"]) == offset:
            return httpx.Response(status, json={"detail": "Authentication refused"})
        return httpx.Response(200, json={"users": [{"id": 1, "username": "alice"}], "total": 2})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        monkeypatch.setattr(request_helper, "_panel_client", client)
        with pytest.raises(ValueError):
            await users.fetch_all_users_raw(key_http.panel, limit=1, strict=strict)

    assert [int(request.url.params["offset"]) for request in requests] == list(range(offset + 1))
    assert all(request.headers["X-Api-Key"] == KEY for request in requests)


async def test_online_fanout_counts_rejected_keys_once(key_http, operational_topics):
    from utils.ip_source_api import _fetch_all_online_ips

    state = key_http
    state.status = 401
    payloads, counters, fetch_times = await _fetch_all_online_ips(
        state.panel, [("alice", 1, {}), ("bob", 2, {})], concurrency=2, timeout=1
    )

    assert payloads == {} and fetch_times == {}
    assert counters == {
        "invalid": 0,
        "ok": 0,
        "failed": 2,
        "not_found": 0,
        "forbidden": 0,
        "unauthorized": 2,
        "deferred": 0,
        "timed_out": 0,
        "read_timeouts": 0,
        "connect_timeouts": 0,
        "pool_timeouts": 0,
        "write_timeouts": 0,
    }
    assert sorted(request.url.path for request in state.requests) == [
        "/api/node/online_stats/1/ip",
        "/api/node/online_stats/2/ip",
    ]
    assert all(request.headers["X-Api-Key"] == KEY for request in state.requests)


@pytest.fixture
def key_sse(monkeypatch, operational_topics):
    import utils.get_logs as collector
    from utils.log_observations import LOG_OBSERVATIONS
    from utils.types import NodeType

    node = NodeType(7, "relay", "192.0.2.7", "connected")
    LOG_OBSERVATIONS.configure({"check_interval": 60})
    LOG_OBSERVATIONS.register_fleet([node])
    monkeypatch.setattr(collector, "_refresh_log_config", AsyncMock())
    monkeypatch.setattr(collector, "_ip_source_is_api", AsyncMock(return_value=False))
    monkeypatch.setattr(collector, "set_current_node_info", AsyncMock())
    monkeypatch.setattr(collector, "_update_node_status", AsyncMock())
    monkeypatch.setattr(collector, "_report_node_health", AsyncMock())
    return collector, node


@pytest.mark.parametrize("setting,expected_verify", [(None, True), ("true", True), ("false", False), ("invalid", True)])
async def test_sse_tls_verification_setting_reaches_http_client(key_sse, monkeypatch, setting, expected_verify):
    collector, node = key_sse
    real_client = httpx.AsyncClient
    options = []

    def client_factory(**kwargs):
        options.append(kwargs)
        return real_client(transport=httpx.MockTransport(lambda _request: httpx.Response(401)), **kwargs)

    if setting is None:
        monkeypatch.delenv("PANEL_VERIFY_SSL", raising=False)
    else:
        monkeypatch.setenv("PANEL_VERIFY_SSL", setting)
    monkeypatch.setattr(collector.httpx, "AsyncClient", client_factory)
    await collector.get_nodes_logs(SimpleNamespace(panel_domain="https://panel.invalid", panel_api_key=KEY), node)

    assert len(options) == 1
    assert options[0]["verify"] is expected_verify
    assert options[0]["timeout"].read is None
    assert options[0]["timeout"].connect == 10


async def test_sse_loopback_receives_api_key_and_consumes_stream(key_sse, monkeypatch):
    """A local HTTP server verifies the actual stream header and parsed event."""
    collector, node = key_sse
    requests = []
    parsed = []

    async def serve(reader, writer):
        requests.append(await reader.readuntil(b"\r\n\r\n"))
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\n\r\ndata: access event\n\n"
        )
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    async def parse(line, node_id, name, **receipt):
        assert set(receipt) == {"received_wall", "received_monotonic"}
        assert all(isinstance(value, float) for value in receipt.values())
        parsed.append((line, node_id, name))
        raise asyncio.CancelledError

    monkeypatch.setattr(collector, "parse_logs", parse)
    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    panel = SimpleNamespace(panel_domain=f"http://127.0.0.1:{port}", panel_api_key=KEY)
    try:
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(collector.get_nodes_logs(panel, node), timeout=3)
    finally:
        server.close()
        await server.wait_closed()

    assert parsed == [("access event", 7, "relay")]
    assert len(requests) == 1
    assert f"X-Api-Key: {KEY}".lower().encode() in requests[0].lower()
    assert b"authorization:" not in requests[0].lower()
    assert requests[0].startswith(b"GET /api/node/7/logs HTTP/1.1")


@pytest.mark.parametrize("status", [401, 403])
async def test_sse_rejected_key_stops_without_reconnect(key_sse, monkeypatch, caplog, status):
    collector, node = key_sse
    requests = []
    real_client = httpx.AsyncClient

    def respond(request):
        requests.append(request)
        return httpx.Response(status, json={"detail": f"Rejected {KEY}"})

    def client_factory(**kwargs):
        return real_client(transport=httpx.MockTransport(respond), **kwargs)

    async def unexpected_retry(_delay):
        pytest.fail("Rejected API key started an SSE authentication retry")

    monkeypatch.setattr(collector.httpx, "AsyncClient", client_factory)
    monkeypatch.setattr(collector, "asyncio", SimpleNamespace(sleep=unexpected_retry))
    panel = SimpleNamespace(panel_domain="https://panel.invalid", panel_api_key=KEY)
    with caplog.at_level(logging.DEBUG):
        await collector.get_nodes_logs(panel, node)

    assert len(requests) == 1
    assert requests[0].headers["X-Api-Key"] == KEY
    assert KEY not in caplog.text


async def test_sse_external_error_does_not_disclose_key(key_sse, monkeypatch, caplog):
    collector, node = key_sse
    real_client = httpx.AsyncClient

    def respond(request):
        raise httpx.ConnectError(f"Transport echoed {KEY}", request=request)

    def client_factory(**kwargs):
        return real_client(transport=httpx.MockTransport(respond), **kwargs)

    async def stop_after_error(_delay):
        raise asyncio.CancelledError

    monkeypatch.setattr(collector.httpx, "AsyncClient", client_factory)
    monkeypatch.setattr(collector, "asyncio", SimpleNamespace(sleep=stop_after_error))
    panel = SimpleNamespace(panel_domain="https://panel.invalid", panel_api_key=KEY)
    with caplog.at_level(logging.DEBUG), pytest.raises(asyncio.CancelledError):
        await collector.get_nodes_logs(panel, node)

    assert KEY not in caplog.text
