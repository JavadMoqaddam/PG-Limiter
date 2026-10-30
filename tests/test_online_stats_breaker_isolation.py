"""Online-stat failures must not suppress otherwise healthy user synchronization."""

import httpx
import pytest

from utils.panel_api import request_helper
from utils.panel_api.online_ips import OUTCOME_DEFERRED, OUTCOME_ERROR, OUTCOME_OK, fetch_user_online_ips
from utils.panel_api.users import fetch_all_users_raw
from utils.types import PanelType


@pytest.fixture
async def panel_transport(monkeypatch):
    calls = []
    stats_status = [500]

    async def respond(request):
        calls.append(request.url.path)
        if request.url.path.startswith("/api/node/online_stats/"):
            body = {"nodes": {"1": {"ips": {}}}} if stats_status[0] == 200 else {"detail": "upstream error"}
            return httpx.Response(stats_status[0], json=body)
        assert request.url.path == "/api/users"
        offset = int(request.url.params["offset"])
        return httpx.Response(200, json={"users": [{"id": offset + 1, "username": f"user_{offset}"}], "total": 2})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        monkeypatch.setattr(request_helper, "_panel_client", client)
        monkeypatch.setattr(request_helper, "circuit_breaker", request_helper.PanelCircuitBreaker())
        # Test the new independent breaker when present; before the fix the
        # online lookup still uses the global breaker and suppresses sync.
        monkeypatch.setattr(
            request_helper, "online_stats_circuit_breaker", request_helper.PanelCircuitBreaker(), raising=False
        )
        yield PanelType(panel_domain="panel.test", panel_api_key="pg_synthetic"), calls, stats_status


async def test_failed_stats_burst_does_not_block_complete_user_snapshot(panel_transport):
    panel, calls, _ = panel_transport
    for user_id in range(1, 6):
        assert await fetch_user_online_ips(panel, user_id) == (None, OUTCOME_ERROR)
    assert await fetch_user_online_ips(panel, 6) == (None, OUTCOME_DEFERRED)
    assert len(calls) == 5  # Further stats work is still bounded by its own breaker.
    users = await fetch_all_users_raw(panel, strict=True, limit=1)
    assert [row["id"] for row in users] == [1, 2]
    assert calls[-2:] == ["/api/users", "/api/users"]


async def test_open_general_panel_breaker_does_not_discard_valid_stats(panel_transport):
    panel, calls, status = panel_transport
    for _ in range(5):
        request_helper.circuit_breaker.record_result(is_server_failure=True)
    status[0] = 200
    assert await fetch_user_online_ips(panel, 1) == ({1: {}}, OUTCOME_OK)
    assert calls == ["/api/node/online_stats/1/ip"]
    with pytest.raises(ValueError, match="first page"):
        await fetch_all_users_raw(panel, strict=True)
    assert calls == ["/api/node/online_stats/1/ip"]
