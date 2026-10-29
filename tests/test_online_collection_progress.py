"""Online collection reports aggregate progress without per-user HTTP noise."""

import asyncio
import logging
from types import SimpleNamespace

import httpx
import pytest

from test_panel_api_key_auth import key_http  # noqa: F401
from utils.panel_api import request_helper
from utils.panel_api.online_ips import fetch_user_online_ips


@pytest.mark.parametrize("status", [200, 401, 403, 404, 500])
async def test_online_request_details_are_debug_only(key_http, monkeypatch, caplog, status):
    state = key_http
    state.status = status
    monkeypatch.setattr(request_helper, "online_stats_circuit_breaker", request_helper.PanelCircuitBreaker())
    with caplog.at_level(logging.INFO):
        await fetch_user_online_ips(state.panel, 12345, timeout=1)
    assert len(state.requests) == 1
    assert "/api/node/online_stats/12345/ip" not in caplog.text
    assert state.panel.panel_api_key not in caplog.text


async def test_online_network_failure_does_not_print_one_error_per_user(key_http, monkeypatch, caplog):
    key_http.error = httpx.ConnectError("private upstream detail")
    monkeypatch.setattr(request_helper, "online_stats_circuit_breaker", request_helper.PanelCircuitBreaker())
    with caplog.at_level(logging.INFO):
        payload, outcome = await fetch_user_online_ips(key_http.panel, 12345, timeout=1)
    assert payload is None and outcome == "error"
    assert "12345" not in caplog.text and "private upstream detail" not in caplog.text


async def test_non_collection_request_keeps_normal_http_logging(key_http, caplog):
    with caplog.at_level(logging.INFO):
        response = await request_helper.panel_get(key_http.panel, "/api/nodes", max_retries=1)
    assert response.status_code == 200
    assert "/api/nodes" in caplog.text


async def test_final_progress_distinguishes_success_from_absence_and_rejected_requests(
    key_http, operational_topics, monkeypatch, caplog
):
    from utils.ip_source_api import _fetch_all_online_ips

    def respond(request):
        uid = int(request.url.path.split("/")[-2])
        if uid == 1:
            return httpx.Response(200, json={"nodes": {"1": {"ips": {}}}})
        if uid == 2:
            return httpx.Response(404, json={"detail": "User not found"})
        return httpx.Response(403, json={"detail": "denied"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        monkeypatch.setattr(request_helper, "_panel_client", client)
        with caplog.at_level(logging.INFO):
            payloads, counters, _ = await _fetch_all_online_ips(
                key_http.panel, [("private_a", 1, {}), ("private_b", 2, {}), ("private_c", 3, {})], 3, 1
            )
    assert counters == {
        "ok": 1,
        "failed": 0,
        "not_found": 1,
        "forbidden": 1,
        "unauthorized": 0,
        "deferred": 0,
        "timed_out": 0,
        "read_timeouts": 0,
        "connect_timeouts": 0,
        "pool_timeouts": 0,
        "write_timeouts": 0,
    }
    assert set(payloads) == {"private_a"}
    assert "3/3" in caplog.text and "33.3%" in caplog.text and "success" in caplog.text.lower()
    assert "100.0%" in caplog.text  # Completion percentage is separate from success.
    assert "private_a" not in caplog.text and "/online_stats/" not in caplog.text


async def test_progress_is_visible_before_last_request_finishes(monkeypatch, key_http, operational_topics, caplog):
    import utils.ip_source_api as api
    import utils.panel_api.online_ips as online

    pending = asyncio.Event()
    release = asyncio.Event()
    clock = [0.0]
    monkeypatch.setattr(api, "time", SimpleNamespace(time=__import__("time").time, monotonic=lambda: clock[0]))

    async def fetch(_panel, uid, **_kwargs):
        if uid == 2:
            pending.set()
            await release.wait()
        else:
            await pending.wait()
            clock[0] = 11.0
        return {}, "ok"

    monkeypatch.setattr(online, "fetch_user_online_ips", fetch)
    with caplog.at_level(logging.INFO):
        task = asyncio.create_task(api._fetch_all_online_ips(key_http.panel, [("a", 1, {}), ("b", 2, {})], 2, 1))
        try:
            await asyncio.wait_for(pending.wait(), 2)
            for _ in range(10):
                await asyncio.sleep(0)
                if "1/2" in caplog.text:
                    break
            assert not task.done()
            assert "1/2" in caplog.text and "success 100.0%" in caplog.text
        finally:
            release.set()
            await task
    assert "2/2" in caplog.text


async def test_fast_collection_does_not_emit_a_progress_line_for_each_user(
    monkeypatch, key_http, operational_topics, caplog
):
    import utils.ip_source_api as api
    import utils.panel_api.online_ips as online

    async def fetch(*_args, **_kwargs):
        return {}, "ok"

    monkeypatch.setattr(online, "fetch_user_online_ips", fetch)
    with caplog.at_level(logging.INFO):
        _, counters, _ = await api._fetch_all_online_ips(
            key_http.panel, [(f"user_{uid}", uid, {}) for uid in range(100)], 5, 1
        )
    assert counters["ok"] == 100
    progress = [record.message for record in caplog.records if "API IP requests" in record.message]
    assert len(progress) == 2
    assert "100/100" in progress[-1] and "success 100.0%" in progress[-1]
