"""Confirm only scanned identities; never fall back to an unfiltered fleet."""

from types import SimpleNamespace

import pytest

from test_panel_pagination_load import panel  # noqa: F401
from utils.panel_api import online_ips, request_helper
from utils.panel_api.users import fetch_all_users_raw


@pytest.mark.parametrize(
    "body",
    [
        {"users": [{"id": 999, "username": "outsider"}], "total": 1},
        {"users": [{"id": 1, "username": "alice"}], "total": 55000},
    ],
)
async def test_id_filter_ignored_by_panel_stops_without_fetching_fleet(monkeypatch, panel, body):
    calls = []

    async def get(_panel, path, **kwargs):
        calls.append(kwargs["params"])
        return SimpleNamespace(status_code=200, json=lambda: body)

    monkeypatch.setattr(request_helper, "panel_get", get)
    with pytest.raises(ValueError):
        await fetch_all_users_raw(panel, ids=[1], limit=1, strict=True)
    assert len(calls) == 1
    assert calls[0]["ids"] == [1]


async def test_membership_lookup_batches_ids_without_online_filter(monkeypatch, panel):
    queries = []

    async def get(_panel, path, **kwargs):
        params = kwargs["params"]
        queries.append(params)
        rows = [{"id": uid, "username": f"user_{uid}", "group_ids": [7]} for uid in params["ids"]]
        return SimpleNamespace(status_code=200, json=lambda: {"users": rows, "total": len(rows)})

    monkeypatch.setattr(request_helper, "panel_get", get)
    rows = await online_ips.fetch_user_memberships(panel, list(range(1, 406)), group_ids=[7], admin_usernames=["owner"])
    assert [row["id"] for row in rows] == list(range(1, 406))
    assert [len(query["ids"]) for query in queries] == [200, 200, 5]
    assert all("online" not in query and "online_after" not in query for query in queries)
    assert all(
        query["group"] == [7] and query["admin"] == ["owner"] and query["status"] == "active" for query in queries
    )
