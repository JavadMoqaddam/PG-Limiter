"""Brief outages hold bounded work; persistent outages report unsent users."""

import asyncio
import logging
import time

import httpx
import pytest

from utils import ip_source_api as api
from utils.panel_api import request_helper
from utils.types import PanelType

PANEL = PanelType(panel_domain="panel.test", panel_api_key="pg_synthetic")


@pytest.fixture
def breaker(monkeypatch):
    breaker = request_helper.PanelCircuitBreaker(failure_threshold=1, recovery_timeout=0.04, single_probe=True)
    monkeypatch.setattr(request_helper, "online_stats_circuit_breaker", breaker)
    return breaker


async def test_queue_recovers_with_one_probe_and_bounded_tasks(monkeypatch, operational_topics, breaker):
    calls = []
    release = asyncio.Event()
    probing = asyncio.Event()
    max_tasks = 0

    async def respond(request):
        nonlocal max_tasks
        calls.append(time.monotonic())
        max_tasks = max(max_tasks, len(asyncio.all_tasks()))
        if len(calls) <= 3:
            await asyncio.sleep(0)
            return httpx.Response(500)
        if len(calls) == 4:
            probing.set()
            await release.wait()
        return httpx.Response(200, json={"nodes": {}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        monkeypatch.setattr(request_helper, "_panel_client", client)
        task = asyncio.create_task(api._fetch_all_online_ips(PANEL, [(f"u{i}", i, {}) for i in range(1, 101)], 3, 1))
        try:
            await asyncio.wait_for(probing.wait(), 2)
            await asyncio.sleep(0)
            assert len(calls) == 4
        finally:
            release.set()
            payloads, counters, _ = await asyncio.wait_for(task, 2)
    assert len(payloads) == 97
    assert counters["failed"] == 3
    assert counters["deferred"] == 0
    assert len(calls) == 100
    assert calls[3] - calls[2] >= 0.035
    assert max_tasks < 12


async def test_persistent_outage_stops_with_unsent_users_separate(monkeypatch, operational_topics, breaker):
    calls = []

    async def respond(request):
        calls.append(request.url.path)
        raise httpx.ReadTimeout("private transport detail")

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        monkeypatch.setattr(request_helper, "_panel_client", client)
        _, counters, _ = await asyncio.wait_for(
            api._fetch_all_online_ips(PANEL, [(f"u{i}", i, {}) for i in range(1, 101)], 1, 1, max_breaker_wait=0.06), 1
        )
    assert len(calls) == 2
    assert counters["failed"] == 2
    assert counters["timed_out"] == 2
    assert counters["deferred"] == 98


@pytest.mark.parametrize("kind", ["Read", "Connect", "Pool", "Write"])
async def test_timeout_kind_survives_transport_and_collection(monkeypatch, operational_topics, breaker, kind, caplog):
    async def respond(request):
        raise getattr(httpx, f"{kind}Timeout")("synthetic private detail")

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        monkeypatch.setattr(request_helper, "_panel_client", client)
        with caplog.at_level(logging.INFO):
            _, counters, _ = await api._fetch_all_online_ips(PANEL, [("synthetic_user", 1, {})], 1, 20)
    assert counters["failed"] == counters["timed_out"] == 1
    assert counters["deferred"] == counters["ok"] == 0
    for candidate in ("read", "connect", "pool", "write"):
        assert counters[f"{candidate}_timeouts"] == int(candidate == kind.lower())
    assert f"1 {kind.lower()}" in caplog.text
    assert "timeout 20.0s, concurrency 1, breaker wait budget 65.0s" in caplog.text
    assert "synthetic private detail" not in caplog.text
    assert "synthetic_user" not in caplog.text
    assert PANEL.panel_api_key not in caplog.text


@pytest.mark.parametrize("timeout", [None, 8.0, 13.0])
async def test_online_transport_uses_default_or_explicit_timeout(monkeypatch, breaker, timeout):
    from utils.panel_api.online_ips import fetch_user_online_ips

    observed = []

    async def respond(request):
        observed.append(request.extensions["timeout"]["read"])
        return httpx.Response(200, json={"nodes": {}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        monkeypatch.setattr(request_helper, "_panel_client", client)
        kwargs = {} if timeout is None else {"timeout": timeout}
        _, outcome = await fetch_user_online_ips(PANEL, 1, **kwargs)
    assert outcome == "ok"
    assert observed == [20.0 if timeout is None else timeout]


async def test_configuration_change_stops_breaker_wait(monkeypatch, operational_topics, breaker):
    breaker.record_result(True)
    current = [True]
    breaker.recovery_timeout = 60
    calls = []

    async def respond(request):
        calls.append(request)
        return httpx.Response(200, json={"nodes": {}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        monkeypatch.setattr(request_helper, "_panel_client", client)
        task = asyncio.create_task(
            api._fetch_all_online_ips(PANEL, [("a", 1, {})], 1, 1, is_current=lambda: current[0])
        )
        await asyncio.sleep(0)
        current[0] = False
        _, counters, _ = await asyncio.wait_for(task, 1)
    assert calls == []
    assert counters["failed"] == 0 and counters["deferred"] == 1


async def test_cancelling_collection_cancels_active_http(monkeypatch, operational_topics, breaker):
    entered = asyncio.Event()
    stopped = asyncio.Event()

    async def respond(request):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        monkeypatch.setattr(request_helper, "_panel_client", client)
        task = asyncio.create_task(api._fetch_all_online_ips(PANEL, [("a", 1, {}), ("b", 2, {})], 1, 1))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert stopped.is_set()


async def test_rejected_mutation_releases_single_probe_without_sending(monkeypatch, breaker):
    from utils.enforcement_guard import EnforcementProtected, enforcement_guard

    breaker.recovery_timeout = 0
    breaker.record_result(True)
    calls = []

    async def respond(request):
        calls.append(request)
        return httpx.Response(200)

    async def denied():
        return False

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        monkeypatch.setattr(request_helper, "_panel_client", client)
        with enforcement_guard(denied, lambda: True), pytest.raises(EnforcementProtected):
            await request_helper.panel_request(PANEL, "POST", "/api/user/1", breaker=breaker)
    assert calls == []
    assert breaker.allow_request() is True


async def test_general_breaker_recovery_allows_guarded_mutation_to_revalidate(monkeypatch):
    from utils.enforcement_guard import enforcement_guard

    general = request_helper.PanelCircuitBreaker(failure_threshold=1, recovery_timeout=0)
    general.record_result(True)
    monkeypatch.setattr(request_helper, "circuit_breaker", general)
    calls = []

    async def respond(request):
        calls.append(request.method)
        return httpx.Response(200)

    async def revalidate():
        response = await request_helper.panel_get(PANEL, "/api/user/1")
        return response is not None and response.status_code == 200

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        monkeypatch.setattr(request_helper, "_panel_client", client)
        with enforcement_guard(revalidate, lambda: True):
            response, error = await request_helper.panel_request(PANEL, "POST", "/api/user/1")
    assert response.status_code == 200 and error is None
    assert calls == ["GET", "POST"]
