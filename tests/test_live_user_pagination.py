"""Live count changes cannot discard valid users or prove missing users absent."""

import asyncio
from types import SimpleNamespace

import pytest

from test_api_evidence import api_cycle  # noqa: F401
from test_api_user_node_scope import two_groups  # noqa: F401
from test_config_revisions import settings_db  # noqa: F401
from test_panel_user_mirror import panel_snapshot, panel_rows  # noqa: F401
from utils.panel_api import request_helper
from utils.panel_api.online_ips import fetch_online_candidates
from utils.types import PanelType


@pytest.fixture
def panel():
    return PanelType(panel_domain="panel.test", panel_api_key="pg_key_test")


def response(ids, total):
    return SimpleNamespace(
        status_code=200,
        json=lambda: {"users": [{"id": uid, "username": f"user_{uid}"} for uid in ids], "total": total},
    )


@pytest.mark.parametrize(
    "pages,expected",
    [
        ({0: ([1, 2], 3), 2: ([3, 4], 4)}, [1, 2, 3, 4]),
        ({0: ([1, 2], 4), 2: ([4], 3)}, [1, 2, 4]),
        ({0: ([1, 2], 3), 2: ([2, 3], 4)}, [1, 2, 3]),
    ],
)
async def test_online_candidates_survive_valid_live_changes(monkeypatch, panel, pages, expected):
    async def get(_panel, path, *, params, **_kwargs):
        assert path == "/api/users" and params["online"] == "true"
        return response(*pages[params["offset"]])

    monkeypatch.setattr(request_helper, "panel_get", get)
    result = await fetch_online_candidates(panel, online_window=120, page_size=2)
    assert result is not None
    assert [row["id"] for row in result] == expected
    assert result.pagination_changed is True


async def test_growth_past_initial_tail_is_fetched_once(monkeypatch, panel):
    calls = []
    pages = {0: ([1, 2], 3), 2: ([3, 4], 5), 4: ([5], 5)}

    async def get(_panel, _path, *, params, **_kwargs):
        calls.append(params["offset"])
        return response(*pages[params["offset"]])

    monkeypatch.setattr(request_helper, "panel_get", get)
    result = await fetch_online_candidates(panel, online_window=120, page_size=2, max_concurrent=1)
    assert result is not None
    assert [row["id"] for row in result] == [1, 2, 3, 4, 5]
    assert calls == [0, 2, 4]
    assert result.pagination_changed


async def test_continuously_growing_listing_cannot_add_unbounded_requests(monkeypatch, panel):
    calls = []

    async def get(_panel, _path, *, params, **_kwargs):
        offset = params["offset"]
        calls.append(offset)
        assert len(calls) <= 4, "live pagination kept chasing new users"
        return response([offset + 1], offset + 2)

    monkeypatch.setattr(request_helper, "panel_get", get)
    result = await fetch_online_candidates(panel, online_window=120, page_size=1, max_concurrent=1)
    assert result is not None and result.pagination_changed
    assert [row["id"] for row in result] == [1, 2, 3]
    assert calls == [0, 1, 2]


@pytest.mark.parametrize("fault", ["missing", "short", "bad_total", "unauthorized", "identity_conflict"])
async def test_live_churn_does_not_hide_broken_pages(monkeypatch, panel, fault):
    async def get(_panel, _path, *, params, **_kwargs):
        if params["offset"] == 0:
            return response([1, 2], 3)
        if fault == "missing":
            return None
        if fault == "short":
            return response([3], 4)
        if fault == "bad_total":
            return response([3, 4], True)
        if fault == "unauthorized":
            return SimpleNamespace(status_code=401, json=lambda: {"detail": "private key"})
        return SimpleNamespace(
            status_code=200,
            json=lambda: {"users": [{"id": 1, "username": "different"}, {"id": 4, "username": "user_4"}], "total": 4},
        )

    monkeypatch.setattr(request_helper, "panel_get", get)
    assert await fetch_online_candidates(panel, online_window=120, page_size=2) is None


async def test_live_query_cancellation_finishes_outstanding_pages(monkeypatch, panel):
    started = asyncio.Event()
    finished = asyncio.Event()

    async def get(_panel, _path, *, params, **_kwargs):
        if params["offset"] == 0:
            return response([1], 2)
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            finished.set()

    monkeypatch.setattr(request_helper, "panel_get", get)
    task = asyncio.create_task(fetch_online_candidates(panel, online_window=120, page_size=1))
    await asyncio.wait_for(started.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished.is_set()


async def test_live_sync_refreshes_users_without_deleting_missing_local_policy(
    settings_db, panel_snapshot, monkeypatch
):
    import db.database as database
    from db.crud.users import UserCRUD
    from db.models import User
    import utils.user_sync as sync

    async with database.get_db() as db:
        db.add(User(username="unobserved", special_limit=9, is_excepted=True, is_disabled_by_limiter=True))
    rows = panel_rows(1002)

    async def get(_panel, path, *, params=None, **_kwargs):
        assert path == "/api/users", "a changing listing must defer membership deletion"
        offset = params["offset"]
        body = {"users": rows[offset : offset + 1000], "total": 1001 if offset == 0 else 1002}
        return SimpleNamespace(status_code=200, json=lambda: body)

    monkeypatch.setattr(request_helper, "panel_get", get)
    synced, errors, deleted = await sync.sync_users_to_database(
        PanelType(panel_domain="panel.test", panel_api_key="pg_key_test")
    )
    assert synced == 1002 and errors > 0 and deleted == 0
    async with database.get_db() as db:
        assert len(await UserCRUD.get_all_usernames(db)) == 1003
        kept = await UserCRUD.get_by_username(db, "unobserved")
        assert (kept.special_limit, kept.is_excepted, kept.is_disabled_by_limiter) == (9, True, True)
    assert sync._last_sync_time is None


async def test_changing_candidate_pages_preserve_unobserved_warning_and_enforce_observed_users(two_groups, monkeypatch):
    import db.database as database
    from db.crud.users import UserCRUD

    r = two_groups
    assert await r.run()
    bob = r.warning.warnings["bob"]
    r.advance()
    r.config.update(api_ip_page_size=1, api_ip_auto_fallback=True)
    r.bodies["carol"] = {"nodes": {"24": {"ips": {"5.6.7.8": int(r.clock[0]), "9.9.9.9": int(r.clock[0])}}}}
    r.memberships["carol"] = [2]
    async with database.get_db() as db:
        carol = await UserCRUD.create_or_update(db, "carol")
        carol.special_limit = 1
    original_get = request_helper.panel_get

    async def get(panel, path, **kwargs):
        if path != "/api/users":
            return await original_get(panel, path, **kwargs)
        params = kwargs["params"]
        offset = params["offset"]
        rows = [
            {"id": 1, "username": "alice", "group_ids": [1]},
            {"id": 3, "username": "carol", "group_ids": [2]},
        ]
        if "ids" in params:
            assert params["ids"] == [1, 3] and "online" not in params
            body = {"users": rows, "total": 2}
        else:
            body = {"users": rows[offset : offset + 1], "total": 3 if offset == 0 else 2}
        return SimpleNamespace(status_code=200, json=lambda: body)

    monkeypatch.setattr(request_helper, "panel_get", get)
    assert await r.run()
    assert r.warning.warnings["bob"] is bob and bob.consecutive_violations == 1
    assert r.warning.warnings["alice"].consecutive_violations == 2
    assert r.warning.warnings["carol"].consecutive_violations == 1
    assert r.config["ip_source"] == "api"
