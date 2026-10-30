"""Closed SSE transports must back off without retrying intentional retirement."""

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from utils.log_observations import LOG_OBSERVATIONS
from utils.shared_state import NODE_LAST_EVENT
from utils.types import NodeType


@pytest.fixture
def sse(monkeypatch, operational_topics):
    import utils.get_logs as collector

    node = NodeType(7, "synthetic", "192.0.2.7", "connected")
    panel = SimpleNamespace(panel_domain="https://panel.invalid", panel_token="synthetic", panel_api_key="synthetic")
    LOG_OBSERVATIONS.configure({"ip_source": "logs", "check_interval": 60, "log_timezone": "UTC"})
    LOG_OBSERVATIONS.register_fleet([node])
    monkeypatch.setattr(collector, "_refresh_log_config", AsyncMock())
    monkeypatch.setattr(collector, "_ip_source_is_api", AsyncMock(return_value=False))
    monkeypatch.setattr(collector, "set_current_node_info", AsyncMock())
    monkeypatch.setattr(collector, "_update_node_status", AsyncMock())
    monkeypatch.setattr(collector, "_report_node_health", AsyncMock())
    # The transport contract is shared by the token and API-key PR bases.
    if hasattr(collector, "get_token"):
        monkeypatch.setattr(collector, "get_token", AsyncMock(return_value=panel))
    return SimpleNamespace(collector=collector, node=node, panel=panel, topics=operational_topics)


def http_boundary(sse, monkeypatch, respond):
    client_type = httpx.AsyncClient
    monkeypatch.setattr(
        sse.collector.httpx,
        "AsyncClient",
        lambda **kwargs: client_type(transport=httpx.MockTransport(respond), **kwargs),
    )


@pytest.mark.parametrize("body", [b"", b": heartbeat\n\n"])
async def test_eof_waits_before_reconnecting(sse, monkeypatch, body):
    requests, delays, responses = [], [], []

    def respond(request):
        requests.append(request.url.path)
        if len(requests) == 2:
            raise asyncio.CancelledError
        response = httpx.Response(200, content=body)
        responses.append(response)
        return response

    async def retry(delay):
        delays.append(delay)
        assert responses[0].is_closed
        assert sse.node.node_id not in NODE_LAST_EVENT
        raise asyncio.CancelledError

    http_boundary(sse, monkeypatch, respond)
    monkeypatch.setattr(sse.collector, "asyncio", SimpleNamespace(sleep=retry))
    with pytest.raises(asyncio.CancelledError):
        await sse.collector.get_nodes_logs(sse.panel, sse.node)

    assert delays and delays[0] >= 5, "SSE EOF reopened immediately without reconnect backoff"
    assert requests == ["/api/node/7/logs"]


async def test_repeated_eof_keeps_request_rate_bounded(sse, monkeypatch):
    attempts, delays = [], []
    elapsed = 0

    def respond(request):
        attempts.append(elapsed)
        if len(attempts) == 4:
            raise asyncio.CancelledError
        return httpx.Response(200, content=b"")

    async def retry(delay):
        nonlocal elapsed
        delays.append(delay)
        elapsed += delay
        if len(delays) == 3:
            raise asyncio.CancelledError

    http_boundary(sse, monkeypatch, respond)
    monkeypatch.setattr(sse.collector, "asyncio", SimpleNamespace(sleep=retry))
    with pytest.raises(asyncio.CancelledError):
        await sse.collector.get_nodes_logs(sse.panel, sse.node)

    assert len(attempts) == 3 and len(delays) == 3
    assert all(5 <= delay <= 60 for delay in delays)
    assert all(later - earlier >= 5 for earlier, later in zip(attempts, attempts[1:]))


@pytest.mark.parametrize("change", ["api", "excluded", "retired"])
@pytest.mark.parametrize("has_heartbeat", [False, True])
async def test_intentional_retirement_is_not_a_transport_retry(sse, monkeypatch, change, has_heartbeat):
    clock = [time.time()]
    api_mode = False
    delays, requests = [], []
    monkeypatch.setattr(time, "time", lambda: clock[0])

    class RetiringStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            nonlocal api_mode
            clock[0] += 60
            if change == "api":
                api_mode = True
            elif change == "excluded":
                LOG_OBSERVATIONS.configure(
                    {"ip_source": "logs", "check_interval": 60, "log_timezone": "UTC", "disabled_nodes": [7]}
                )
            else:
                sse.topics._enabled = False
            if has_heartbeat:
                yield b": heartbeat\n\n"

    def respond(request):
        requests.append(request.url.path)
        return httpx.Response(200, stream=RetiringStream())

    async def current_source():
        return api_mode

    async def pause(delay):
        delays.append(delay)
        raise asyncio.CancelledError

    http_boundary(sse, monkeypatch, respond)
    monkeypatch.setattr(sse.collector, "_ip_source_is_api", current_source)
    monkeypatch.setattr(sse.collector, "asyncio", SimpleNamespace(sleep=pause))
    if change == "api":
        with pytest.raises(asyncio.CancelledError):
            await sse.collector.get_nodes_logs(sse.panel, sse.node)
    else:
        await sse.collector.get_nodes_logs(sse.panel, sse.node)

    assert requests == ["/api/node/7/logs"]
    assert delays == ([10] if change == "api" else [])
    assert all("❌" not in call.args[2] for call in sse.collector._update_node_status.await_args_list)


async def test_cancellation_closes_stream_without_reconnecting(sse, monkeypatch):
    started = asyncio.Event()
    closed = False
    requests = []

    class PendingStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            started.set()
            yield b": heartbeat\n\n"
            await asyncio.Event().wait()

        async def aclose(self):
            nonlocal closed
            closed = True

    def respond(request):
        requests.append(request.url.path)
        return httpx.Response(200, stream=PendingStream())

    async def unexpected_retry(delay):
        pytest.fail("Cancellation scheduled an SSE retry")

    http_boundary(sse, monkeypatch, respond)
    monkeypatch.setattr(sse.collector, "asyncio", SimpleNamespace(sleep=unexpected_retry))
    task = asyncio.create_task(sse.collector.get_nodes_logs(sse.panel, sse.node))
    try:
        await asyncio.wait_for(started.wait(), timeout=2)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert closed
    assert requests == ["/api/node/7/logs"]
    assert sse.node.node_id not in NODE_LAST_EVENT


async def test_loopback_delivers_access_after_eof_recovery(sse, monkeypatch):
    requests, parsed, delays = [], [], []

    async def serve(reader, writer):
        try:
            requests.append(await reader.readuntil(b"\r\n\r\n"))
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\n\r\n")
            if len(requests) == 2:
                writer.write(b"data: synthetic access\n\n")
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    async def retry(delay):
        delays.append(delay)
        await asyncio.sleep(0)

    async def parse(line, node_id, name, **receipt):
        parsed.append((line, node_id, name))
        raise asyncio.CancelledError

    monkeypatch.setattr(sse.collector, "parse_logs", parse)
    monkeypatch.setattr(sse.collector, "asyncio", SimpleNamespace(sleep=retry))
    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    sse.panel.panel_domain = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
    try:
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(sse.collector.get_nodes_logs(sse.panel, sse.node), timeout=3)
    finally:
        server.close()
        await server.wait_closed()

    assert len(requests) == 2
    assert parsed == [("synthetic access", 7, "synthetic")]
    assert len(delays) == 1 and delays[0] >= 5
