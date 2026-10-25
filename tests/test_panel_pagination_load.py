"""Invalid strict snapshots stop panel work and retain useful, private diagnostics."""

import asyncio
from types import SimpleNamespace

import pytest

from utils.panel_api import request_helper
from utils.panel_api.users import fetch_all_users_raw
from utils.types import PanelType


@pytest.fixture
def panel():
    return PanelType("admin", "password", "panel.test")


def page(offset, total=8):
    return SimpleNamespace(json=lambda: {"users": [{"id": offset + 1, "username": f"user_{offset}"}], "total": total})


@pytest.mark.parametrize("failure", ["missing", "changed_total", "network_error", "cancelled"])
async def test_strict_failure_stops_queued_http_requests(monkeypatch, panel, failure):
    requested = []

    async def get(_panel, path, *, params, **_kwargs):
        assert path == "/api/users"
        offset = params["offset"]
        requested.append(offset)
        if offset == 1:
            if failure == "missing":
                return None
            if failure == "changed_total":
                return page(offset, 9)
            if failure == "network_error":
                raise OSError("request failed")
            raise asyncio.CancelledError
        return page(offset)

    monkeypatch.setattr(request_helper, "panel_get", get)
    error = asyncio.CancelledError if failure == "cancelled" else ValueError
    with pytest.raises(error):
        await fetch_all_users_raw(panel, limit=1, max_concurrent=1, strict=True)
    assert requested == [0, 1]


async def test_strict_failure_cancels_and_settles_inflight_request(monkeypatch, panel):
    requested = []
    second_started = asyncio.Event()
    release_second = asyncio.Event()
    second_finished = asyncio.Event()
    cancelled = []

    async def get(_panel, _path, *, params, **_kwargs):
        offset = params["offset"]
        requested.append(offset)
        if offset == 1:
            await second_started.wait()
            return page(offset, 9)
        if offset == 2:
            second_started.set()
            try:
                await release_second.wait()
            except asyncio.CancelledError:
                cancelled.append(offset)
                raise
            finally:
                second_finished.set()
        return page(offset)

    monkeypatch.setattr(request_helper, "panel_get", get)
    collection = asyncio.create_task(fetch_all_users_raw(panel, limit=1, max_concurrent=2, strict=True))
    try:
        await asyncio.wait_for(second_started.wait(), 5)
        await asyncio.wait({collection}, timeout=1)
        settled_without_release = collection.done() and second_finished.is_set()
    finally:
        release_second.set()
        with pytest.raises(ValueError, match="Incomplete /api/users pagination"):
            await asyncio.wait_for(collection, 5)
    assert settled_without_release
    assert cancelled == [2]
    assert requested == [0, 1, 2]


async def test_caller_cancellation_settles_all_page_requests(monkeypatch, panel):
    started = []
    finished = []
    both_started = asyncio.Event()

    async def get(_panel, _path, *, params, **_kwargs):
        offset = params["offset"]
        if offset == 0:
            return page(offset)
        started.append(offset)
        if len(started) == 2:
            both_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            finished.append(offset)

    monkeypatch.setattr(request_helper, "panel_get", get)
    collection = asyncio.create_task(fetch_all_users_raw(panel, limit=1, max_concurrent=2, strict=True))
    try:
        await asyncio.wait_for(both_started.wait(), 5)
    finally:
        collection.cancel()
        with pytest.raises(asyncio.CancelledError):
            await collection
    assert started == [1, 2]
    assert sorted(finished) == [1, 2]


async def test_non_strict_fetch_retains_available_pages(monkeypatch, panel):
    requested = []

    async def get(_panel, _path, *, params, **_kwargs):
        offset = params["offset"]
        requested.append(offset)
        return None if offset == 1 else page(offset, 4)

    monkeypatch.setattr(request_helper, "panel_get", get)
    users = await fetch_all_users_raw(panel, limit=1, max_concurrent=1)
    assert requested == [0, 1, 2, 3]
    assert [user["id"] for user in users] == [1, 3, 4]


@pytest.mark.parametrize(
    "bad_total, missing, reason",
    [
        (None, True, "missing"),
        ("private-response-marker", False, "type=str"),
        (True, False, "type=bool"),
        (None, False, "type=NoneType"),
        (-1, False, "negative"),
        (9, False, "expected=8, received=9"),
    ],
)
async def test_total_failure_identifies_offset_and_reason_without_payload(
    monkeypatch, panel, caplog, bad_total, missing, reason
):
    async def get(_panel, _path, *, params, **_kwargs):
        offset = params["offset"]
        if offset == 0:
            return page(offset)
        body = {"users": [{"id": 2, "username": "private-user-marker"}], "total": bad_total}
        if missing:
            body.pop("total")
        return SimpleNamespace(json=lambda: body)

    monkeypatch.setattr(request_helper, "panel_get", get)
    with pytest.raises(ValueError):
        await fetch_all_users_raw(panel, limit=1, max_concurrent=1, strict=True)
    assert "offset 1" in caplog.text
    assert reason.casefold() in caplog.text.casefold()
    assert "private-response-marker" not in caplog.text
    assert "private-user-marker" not in caplog.text
