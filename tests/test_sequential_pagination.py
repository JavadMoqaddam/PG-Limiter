"""Page HTTP attempts share completion-based pacing across callers and retries."""

import asyncio
import time

import httpx
import pytest

from test_panel_api_key_auth import key_http  # noqa: F401
from utils.panel_api import request_helper


async def test_slow_pages_serialize_but_online_ips_bypass_gate(key_http, monkeypatch):
    entered = asyncio.Event()
    release = asyncio.Event()
    starts = []
    finishes = []

    async def respond(request):
        if request.url.path != "/api/users":
            return httpx.Response(200, json={"nodes": {}})
        starts.append(time.monotonic())
        if len(starts) == 1:
            entered.set()
            await release.wait()
        finishes.append(time.monotonic())
        return httpx.Response(200, json={"users": [], "total": 0})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        monkeypatch.setattr(request_helper, "_panel_client", client)
        first = asyncio.create_task(request_helper.panel_get(key_http.panel, "/api/users"))
        await entered.wait()
        second = asyncio.create_task(request_helper.panel_get(key_http.panel, "/api/users"))
        try:
            ip = await asyncio.wait_for(request_helper.panel_get(key_http.panel, "/api/node/online_stats/1/ip"), 1)
            assert ip.status_code == 200
            await asyncio.sleep(0)
            assert len(starts) == 1
        finally:
            release.set()
            await asyncio.wait_for(asyncio.gather(first, second), 3)
    assert starts[1] - finishes[0] >= 0.99


async def test_failed_page_attempt_waits_before_retry(key_http, monkeypatch):
    finishes = []
    starts = []

    async def respond(request):
        starts.append(time.monotonic())
        finishes.append(time.monotonic())
        return httpx.Response(500 if len(starts) == 1 else 200, json={})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        monkeypatch.setattr(request_helper, "_panel_client", client)
        response = await request_helper.panel_get(key_http.panel, "/api/users", max_retries=2, retry_delay=0)
    assert response.status_code == 200
    assert len(starts) == 2
    assert starts[1] - finishes[0] >= 0.99


async def test_cancelled_page_releases_gate_and_preserves_pause(key_http, monkeypatch):
    entered = asyncio.Event()
    finishes = []
    starts = []

    async def respond(request):
        starts.append(time.monotonic())
        if len(starts) == 1:
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                finishes.append(time.monotonic())
        return httpx.Response(200, json={})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        monkeypatch.setattr(request_helper, "_panel_client", client)
        first = asyncio.create_task(request_helper.panel_get(key_http.panel, "/api/users"))
        await entered.wait()
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        response = await asyncio.wait_for(request_helper.panel_get(key_http.panel, "/api/users"), 3)
    assert response.status_code == 200
    assert starts[1] - finishes[0] >= 0.99
