"""A complete panel snapshot replaces local membership without blocking settings writes."""

import asyncio
from types import SimpleNamespace
from urllib.parse import unquote
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession

import db.database as database
import utils.user_sync as sync
from db.crud.config import ConfigCRUD
from db.crud.users import UserCRUD
from db.models import User
from test_config_revisions import settings_db  # noqa: F401
from utils.types import PanelType


@pytest.fixture
def panel_snapshot(monkeypatch):
    from utils.panel_api import request_helper

    rows = []

    async def panel_get(_panel, path, *, params=None, **_kwargs):
        if path.startswith("/api/user/"):
            username = unquote(path.removeprefix("/api/user/"))
            user = next((row for row in rows if row["username"] == username), None)
            return SimpleNamespace(
                status_code=200 if user is not None else 404,
                json=lambda: user if user is not None else {"detail": "User not found"},
            )
        assert path == "/api/users"
        assert "status" not in params
        offset, limit = params["offset"], params["limit"]
        response = type(
            "Response",
            (),
            {"status_code": 200, "json": lambda self: {"users": rows[offset : offset + limit], "total": len(rows)}},
        )
        return response()

    monkeypatch.setattr(request_helper, "panel_get", panel_get)
    monkeypatch.setattr(sync, "_sync_lock", asyncio.Lock())
    monkeypatch.setattr(sync, "_last_sync_time", None)
    monkeypatch.setattr(sync, "USER_METADATA_CACHE", {})
    monkeypatch.setattr(sync, "_notify_deleted_users", AsyncMock())
    return rows


def panel_rows(count):
    return [{"username": f"user_{index}", "id": index + 1000, "group_ids": [7]} for index in range(count)]


async def test_complete_snapshot_removes_all_absent_users_and_preserves_local_policy(settings_db, panel_snapshot):
    panel_snapshot.extend(
        {"username": status, "id": index + 1, "status": status, "group_ids": [7]}
        for index, status in enumerate(["active", "disabled", "limited", "expired", "on_hold"])
    )
    async with database.get_db() as db:
        db.add_all(User(username=f"removed_{index}") for index in range(100))
        db.add(User(username="disabled", panel_id=2, special_limit=9, is_excepted=True, is_disabled_by_limiter=True))

    assert await sync.sync_users_to_database(PanelType(panel_domain="panel.test", panel_api_key="pg_key_test")) == (
        5,
        0,
        100,
    )
    async with database.get_db() as db:
        assert await UserCRUD.get_all_usernames(db) == {"active", "disabled", "limited", "expired", "on_hold"}
        row = await UserCRUD.get_by_username(db, "disabled")
        assert (row.special_limit, row.is_excepted, row.is_disabled_by_limiter, row.group_ids) == (9, True, True, [7])
    assert set(sync.USER_METADATA_CACHE) == {"active", "disabled", "limited", "expired", "on_hold"}
    assert sync._last_sync_time is not None


async def test_validated_empty_panel_clears_local_membership(settings_db, panel_snapshot):
    async with database.get_db() as db:
        db.add(User(username="removed"))
    assert await sync.sync_users_to_database(PanelType(panel_domain="panel.test", panel_api_key="pg_key_test")) == (
        0,
        0,
        1,
    )
    async with database.get_db() as db:
        assert await UserCRUD.get_all_usernames(db) == set()
    assert sync.USER_METADATA_CACHE == {}
    assert sync._last_sync_time is not None


@pytest.mark.parametrize("failure", ["missing_page", "changing_total", "duplicate", "missing_total", "parse_error"])
async def test_invalid_snapshot_never_changes_local_users(settings_db, panel_snapshot, monkeypatch, failure):
    from utils.panel_api import request_helper

    async with database.get_db() as db:
        db.add(User(username="kept", group_ids=[1]))
    rows = panel_rows(1001)

    async def panel_get(_panel, _path, *, params, **_kwargs):
        offset = params["offset"]
        if failure == "missing_page" and offset:
            return None
        total = 1002 if failure == "changing_total" and offset else 1001
        page = rows[offset : offset + params["limit"]]
        if failure == "duplicate" and offset:
            page = [rows[0]]
        if failure == "parse_error":
            page = [dict(row, group_ids="invalid") for row in page]
        data = {"users": page, "total": total}
        if failure == "missing_total":
            data.pop("total")
        return type("Response", (), {"status_code": 200, "json": lambda self: data})()

    monkeypatch.setattr(request_helper, "panel_get", panel_get)
    synced, errors, deleted = await sync.sync_users_to_database(
        PanelType(panel_domain="panel.test", panel_api_key="pg_key_test")
    )
    assert (synced, deleted) == (0, 0)
    assert errors > 0
    async with database.get_db() as db:
        assert await UserCRUD.get_all_usernames(db) == {"kept"}
    assert sync._last_sync_time is None


async def test_settings_writer_can_commit_between_user_batches(settings_db, panel_snapshot, monkeypatch):
    panel_snapshot.extend(panel_rows(501))

    # Fail quickly if a sync transaction keeps SQLite's sole writer lock.
    @event.listens_for(settings_db.engine.sync_engine, "checkout")
    def short_timeout(connection, _record, _proxy):
        cursor = connection.cursor()
        cursor.execute("PRAGMA busy_timeout=50")
        cursor.close()

    second_insert = asyncio.Event()
    resume = asyncio.Event()
    original_execute = AsyncSession.execute
    inserts = 0

    async def pause_second_insert(db, statement, *args, **kwargs):
        nonlocal inserts
        if getattr(statement, "is_insert", False) and statement.table.name == "users":
            inserts += 1
            if inserts == 2:
                second_insert.set()
                await resume.wait()
        return await original_execute(db, statement, *args, **kwargs)

    monkeypatch.setattr(AsyncSession, "execute", pause_second_insert)
    task = asyncio.create_task(
        sync.sync_users_to_database(PanelType(panel_domain="panel.test", panel_api_key="pg_key_test"))
    )
    try:
        await asyncio.wait_for(second_insert.wait(), 5)
        async with database.get_db() as db:
            await ConfigCRUD.set(db, "api_ip_concurrency", "3")
        async with database.get_db() as db:
            assert await ConfigCRUD.get(db, "api_ip_concurrency") == "3"
    finally:
        resume.set()
        result = await asyncio.wait_for(task, 5)
    assert result == (501, 0, 0)


async def test_cleanup_preserves_users_added_between_batches(settings_db, panel_snapshot, monkeypatch):
    panel_snapshot.extend(panel_rows(501))
    second_insert = asyncio.Event()
    resume = asyncio.Event()
    original_execute = AsyncSession.execute
    inserts = 0

    async def pause_second_insert(db, statement, *args, **kwargs):
        nonlocal inserts
        if getattr(statement, "is_insert", False) and statement.table.name == "users":
            inserts += 1
            if inserts == 2:
                second_insert.set()
                await resume.wait()
        return await original_execute(db, statement, *args, **kwargs)

    monkeypatch.setattr(AsyncSession, "execute", pause_second_insert)
    task = asyncio.create_task(
        sync.sync_users_to_database(PanelType(panel_domain="panel.test", panel_api_key="pg_key_test"))
    )
    try:
        await asyncio.wait_for(second_insert.wait(), 5)
        async with database.get_db() as db:
            db.add(User(username="late_local"))
    finally:
        resume.set()
        result = await asyncio.wait_for(task, 5)

    assert result == (501, 1, 0)
    async with database.get_db() as db:
        assert "late_local" in await UserCRUD.get_all_usernames(db)
    assert "late_local" in sync.USER_METADATA_CACHE
    assert sync._last_sync_time is None
    sync._notify_deleted_users.assert_not_awaited()


async def test_same_count_panel_membership_churn_preserves_still_present_local_policy(
    settings_db, panel_snapshot, monkeypatch
):
    from utils.panel_api import request_helper

    async with database.get_db() as db:
        db.add(
            User(
                username="user_1000",
                panel_id=2000,
                special_limit=9,
                is_excepted=True,
                is_disabled_by_limiter=True,
                disable_provenance={"version": 1, "method": "status"},
            )
        )
    original = panel_rows(1002)
    current = original[1:] + [{"username": "z", "id": 9999, "group_ids": [7]}]

    async def panel_get(_panel, path, *, params=None, **_kwargs):
        if path == "/api/users":
            rows = original if params["offset"] == 0 else current
            page = rows[params["offset"] : params["offset"] + params["limit"]]
            return SimpleNamespace(status_code=200, json=lambda: {"users": page, "total": 1002})
        assert path == "/api/user/user_1000"
        return SimpleNamespace(status_code=200, json=lambda: original[1000])

    monkeypatch.setattr(request_helper, "panel_get", panel_get)
    assert await sync.sync_users_to_database(PanelType(panel_domain="panel.test", panel_api_key="pg_key_test")) == (
        1002,
        1,
        0,
    )
    async with database.get_db() as db:
        user = await UserCRUD.get_by_username(db, "user_1000")
        assert (user.special_limit, user.is_excepted, user.is_disabled_by_limiter) == (9, True, True)
        assert user.disable_provenance == {"version": 1, "method": "status"}
    assert sync._last_sync_time is None
    sync._notify_deleted_users.assert_not_awaited()


@pytest.mark.parametrize(
    "response_kind", ["missing", "network_error", "forbidden", "bare_404", "route_404", "bad_json_404"]
)
async def test_unconfirmed_user_absence_preserves_local_policy(
    settings_db, panel_snapshot, monkeypatch, caplog, response_kind
):
    from utils.panel_api import request_helper

    async with database.get_db() as db:
        db.add(User(username="kept", special_limit=9, is_excepted=True))
    original_get = request_helper.panel_get

    async def panel_get(_panel, path, **kwargs):
        if path == "/api/users":
            return await original_get(_panel, path, **kwargs)
        assert path == "/api/user/kept"
        if response_kind == "missing":
            return None
        if response_kind == "network_error":
            raise RuntimeError("private response must not be logged")
        if response_kind == "forbidden":
            return SimpleNamespace(status_code=403, json=lambda: {"detail": "Forbidden"})
        if response_kind == "bare_404":
            return SimpleNamespace(status_code=404)
        if response_kind == "route_404":
            return SimpleNamespace(status_code=404, json=lambda: {"detail": "Not Found"})

        def invalid_json():
            raise ValueError("private response must not be logged")

        return SimpleNamespace(status_code=404, json=invalid_json)

    monkeypatch.setattr(request_helper, "panel_get", panel_get)
    assert await sync.sync_users_to_database(PanelType(panel_domain="panel.test", panel_api_key="pg_key_test")) == (
        0,
        1,
        0,
    )
    async with database.get_db() as db:
        user = await UserCRUD.get_by_username(db, "kept")
        assert (user.special_limit, user.is_excepted) == (9, True)
    assert sync._last_sync_time is None
    sync._notify_deleted_users.assert_not_awaited()
    assert "private response" not in caplog.text


async def test_incomplete_cleanup_notifies_only_confirmed_committed_deletions(settings_db, panel_snapshot, monkeypatch):
    from utils.panel_api import request_helper

    async with database.get_db() as db:
        db.add_all([User(username="gone"), User(username="kept", special_limit=9)])
    original_get = request_helper.panel_get

    async def panel_get(_panel, path, **kwargs):
        if path == "/api/user/kept":
            return SimpleNamespace(status_code=200, json=lambda: {"username": "kept", "id": 9999})
        return await original_get(_panel, path, **kwargs)

    monkeypatch.setattr(request_helper, "panel_get", panel_get)
    assert await sync.sync_users_to_database(PanelType(panel_domain="panel.test", panel_api_key="pg_key_test")) == (
        0,
        1,
        1,
    )
    async with database.get_db() as db:
        assert await UserCRUD.get_all_usernames(db) == {"kept"}
        assert (await UserCRUD.get_by_username(db, "kept")).special_limit == 9
    assert sync._last_sync_time is None
    assert sync._notify_deleted_users.await_args.args == (["gone"],)


async def test_cleanup_preserves_local_edit_committed_during_full_panel_fetch(settings_db, panel_snapshot, monkeypatch):
    from utils.panel_api import request_helper

    async with database.get_db() as db:
        db.add(User(username="kept", special_limit=2))
    original_get = request_helper.panel_get

    async def panel_get(_panel, path, **kwargs):
        if path == "/api/users":
            async with database.get_db() as db:
                await UserCRUD.set_special_limit(db, "kept", 9, fetch_from_panel=False)
        return await original_get(_panel, path, **kwargs)

    monkeypatch.setattr(request_helper, "panel_get", panel_get)
    assert await sync.sync_users_to_database(PanelType(panel_domain="panel.test", panel_api_key="pg_key_test")) == (
        0,
        1,
        0,
    )
    async with database.get_db() as db:
        assert (await UserCRUD.get_by_username(db, "kept")).special_limit == 9
    assert sync._last_sync_time is None
    sync._notify_deleted_users.assert_not_awaited()


@pytest.mark.parametrize("edit_kind", ["policy", "recovery", "panel_refresh"])
async def test_cleanup_confirmation_releases_database_and_preserves_concurrent_local_edit(
    settings_db, panel_snapshot, monkeypatch, edit_kind
):
    from utils.panel_api import request_helper

    async with database.get_db() as db:
        db.add(User(username="kept", special_limit=2))
    original_get = request_helper.panel_get

    async def panel_get(_panel, path, **kwargs):
        if path == "/api/users":
            return await original_get(_panel, path, **kwargs)
        assert path == "/api/user/kept"
        # A separate writer must be free to commit during the network wait.
        async with database.get_db() as db:
            if edit_kind == "policy":
                await UserCRUD.set_special_limit(db, "kept", 9, fetch_from_panel=False)
            elif edit_kind == "recovery":
                await UserCRUD.prepare_disable_provenance(db, "kept", {"version": 1, "method": "status"})
                await UserCRUD.set_disabled(db, "kept", enable_at=123.0, fetch_from_panel=False)
            else:
                await UserCRUD.create_or_update(db, "kept", status="expired", group_ids=[7])
        return SimpleNamespace(status_code=404, json=lambda: {"detail": "User not found"})

    monkeypatch.setattr(request_helper, "panel_get", panel_get)
    assert await sync.sync_users_to_database(PanelType(panel_domain="panel.test", panel_api_key="pg_key_test")) == (
        0,
        1,
        0,
    )
    async with database.get_db() as db:
        user = await UserCRUD.get_by_username(db, "kept")
        if edit_kind == "policy":
            assert user.special_limit == 9
        elif edit_kind == "recovery":
            assert user.disable_provenance == {"version": 1, "method": "status"}
            assert user.enable_at == 123.0
        else:
            assert (user.status, user.group_ids) == ("expired", [7])
    assert sync._last_sync_time is None
    sync._notify_deleted_users.assert_not_awaited()


async def test_failed_later_batch_keeps_committed_count_and_skips_pruning(settings_db, panel_snapshot, monkeypatch):
    panel_snapshot.extend(panel_rows(501))
    async with database.get_db() as db:
        db.add(User(username="kept"))
    original_execute = AsyncSession.execute
    inserts = 0

    async def fail_second_insert(db, statement, *args, **kwargs):
        nonlocal inserts
        if getattr(statement, "is_insert", False) and statement.table.name == "users":
            inserts += 1
            if inserts == 2:
                raise RuntimeError("injected second batch failure")
        return await original_execute(db, statement, *args, **kwargs)

    monkeypatch.setattr(AsyncSession, "execute", fail_second_insert)
    assert await sync.sync_users_to_database(PanelType(panel_domain="panel.test", panel_api_key="pg_key_test")) == (
        500,
        1,
        0,
    )
    async with database.get_db() as db:
        names = await UserCRUD.get_all_usernames(db)
    assert len(names) == 501 and "kept" in names and "user_500" not in names
    assert sync._last_sync_time is None
