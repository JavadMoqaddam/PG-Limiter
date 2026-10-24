"""Exercise incident recovery through real SQLite and the panel request boundary."""

import copy
import json
import time
from types import SimpleNamespace
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine


@pytest.fixture
async def recovery(tmp_path, monkeypatch, mock_panel_type):
    import db.database as database
    from db.crud import UserCRUD
    from db.models import Base
    from utils import handel_dis_users as registry
    from utils.panel_api import request_helper, users
    from utils.user_groups_storage import UserGroupsStorage
    from utils.user_sync import USER_METADATA_CACHE

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'recovery.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(database, "AsyncSessionLocal", sessions)
    monkeypatch.setattr(registry, "_migrated", True)
    monkeypatch.chdir(tmp_path)
    storage = UserGroupsStorage(str(tmp_path / ".user_groups_backup.json"))
    config = {"disable_method": "group", "disabled_group_id": 99, "fallback_group_id": 77}

    async def read_config():
        return copy.deepcopy(config)

    monkeypatch.setattr(users, "read_config", read_config)
    state = SimpleNamespace(
        panel=mock_panel_type,
        config=config,
        accounts={"alice": {"username": "alice", "group_ids": [1, 2], "status": "active"}},
        puts=[],
        fail_put=False,
        fail_get=False,
        registry=registry,
        storage=storage,
        sessions=sessions,
    )

    async def row(username="alice"):
        async with database.get_db() as session:
            return await UserCRUD.get_by_username(session, username)

    state.row = row

    async def panel_get(panel, path, **kwargs):
        if state.fail_get:
            return None
        username = path.removeprefix("/api/user/")
        account = state.accounts.get(username)
        return SimpleNamespace(status_code=200 if account else 404, json=lambda: copy.deepcopy(account))

    async def panel_put(panel, path, json_data, **kwargs):
        # UserModify accepts a nullable group list including []; CRUD replaces
        # groups before status. Node serialization excludes disabled inbounds.
        assert set(json_data) <= {"group_ids", "status"}
        assert json_data.get("status") in {"active", "disabled"}
        assert kwargs["max_retries"] == 3
        username = path.removeprefix("/api/user/")
        state.puts.append((username, copy.deepcopy(json_data)))
        if json_data["status"] == "disabled":
            prepared = await row(username)
            assert prepared is not None
            assert prepared.is_disabled_by_limiter is False
        if state.fail_put:
            return SimpleNamespace(status_code=500)
        account = state.accounts.get(username)
        if account is None:
            return SimpleNamespace(status_code=404)
        if "group_ids" in json_data:
            account["group_ids"] = list(json_data["group_ids"])
        account["status"] = json_data["status"]
        return SimpleNamespace(status_code=200, json=lambda: copy.deepcopy(account))

    monkeypatch.setattr(request_helper, "panel_get", panel_get)
    monkeypatch.setattr(request_helper, "panel_put", panel_put)

    async def disable(username="alice", **kwargs):
        from utils.types import UserType

        return await users.disable_user(mock_panel_type, UserType(name=username, ip=[]), **kwargs)

    async def enable(*usernames):
        result = await users.enable_selected_users(mock_panel_type, set(usernames or ("alice",)))
        # Management and daemon callers clear only confirmed outcomes.
        for username in result["enabled"] + result["not_found"]:
            await registry.enable(username)
        return result

    state.disable = disable
    state.enable = enable
    yield state
    USER_METADATA_CACHE.clear()
    await engine.dispose()


async def test_confirmed_group_disable_stores_db_inverse_and_timer(recovery):
    before = time.time()
    await recovery.disable(duration_seconds=600)
    row = await recovery.row()
    assert row.disable_provenance == {
        "version": 1,
        "method": "group",
        "original_groups": [1, 2],
        "applied_groups": [99],
    }
    assert row.is_disabled_by_limiter is True
    assert row.disabled_at >= before
    assert row.enable_at == pytest.approx(row.disabled_at + 600)
    assert recovery.puts == [("alice", {"group_ids": [99], "status": "disabled"})]
    assert not recovery.storage.user_groups
    assert not Path(recovery.storage.filename).exists()


@pytest.mark.parametrize("method,group_id", [("status", 99), ("group", 88), ("group", None)])
async def test_group_incident_restores_original_after_settings_change(recovery, method, group_id):
    await recovery.disable()
    recovery.config.update(disable_method=method, disabled_group_id=group_id)
    result = await recovery.enable()
    assert result == {"enabled": ["alice"], "failed": [], "not_found": []}
    assert recovery.puts[-1] == ("alice", {"group_ids": [1, 2], "status": "active"})
    assert (await recovery.row()).disable_provenance is None


async def test_status_incident_leaves_groups_after_switch_to_group(recovery):
    recovery.config["disable_method"] = "status"
    await recovery.disable()
    recovery.accounts["alice"]["group_ids"] = [8, 99]
    recovery.config["disable_method"] = "group"
    await recovery.enable()
    assert recovery.puts[-1] == ("alice", {"status": "active"})
    assert recovery.accounts["alice"]["group_ids"] == [8, 99]


@pytest.mark.parametrize("edited,expected", [([99, 8], [8]), ([8, 9], [8, 9]), ([], []), ([88, 99, 8], [88, 8])])
async def test_group_enable_preserves_manual_edits_and_removes_only_applied_group(recovery, edited, expected):
    await recovery.disable()
    recovery.config["disabled_group_id"] = 88
    recovery.accounts["alice"]["group_ids"] = edited
    await recovery.enable()
    assert recovery.puts[-1] == ("alice", {"group_ids": expected, "status": "active"})


async def test_original_empty_groups_remain_empty_without_fallback(recovery):
    recovery.accounts["alice"]["group_ids"] = []
    await recovery.disable()
    await recovery.enable()
    assert recovery.puts[-1] == ("alice", {"group_ids": [], "status": "active"})


async def test_original_membership_in_applied_group_is_restored(recovery):
    recovery.accounts["alice"]["group_ids"] = [99, 2]
    await recovery.disable()
    await recovery.enable()
    assert recovery.puts[-1][1]["group_ids"] == [99, 2]


async def test_manual_edit_preserves_preexisting_applied_group_membership(recovery):
    recovery.accounts["alice"]["group_ids"] = [99, 2]
    await recovery.disable()
    recovery.accounts["alice"]["group_ids"] = [99, 8]
    await recovery.enable()
    assert recovery.puts[-1][1]["group_ids"] == [99, 8]


async def test_failed_disable_stages_inverse_without_confirming_registry(recovery):
    recovery.fail_put = True
    with pytest.raises(ValueError, match="Failed to disable"):
        await recovery.disable()
    assert await recovery.registry.is_disabled("alice") is False
    assert (await recovery.row()).disable_provenance["original_groups"] == [1, 2]
    assert recovery.accounts["alice"]["group_ids"] == [1, 2]
    assert not recovery.storage.user_groups


async def test_failed_disable_retry_keeps_the_original_snapshot(recovery):
    recovery.fail_put = True
    with pytest.raises(ValueError):
        await recovery.disable()
    recovery.fail_put = False
    # An unconfirmed response can still have applied the effect remotely.
    recovery.accounts["alice"].update(group_ids=[99], status="disabled")
    recovery.config.update(disable_method="status", disabled_group_id=88)
    await recovery.disable()
    await recovery.enable()
    assert recovery.puts[-1][1] == {"group_ids": [1, 2], "status": "active"}


@pytest.mark.parametrize("failure", ["get", "put"])
async def test_failed_enable_preserves_provenance_and_permanent_timer(recovery, failure):
    await recovery.disable(permanent=True)
    original = (await recovery.row()).disable_provenance
    setattr(recovery, f"fail_{failure}", True)
    assert await recovery.enable() == {"enabled": [], "failed": ["alice"], "not_found": []}
    row = await recovery.row()
    assert row.disable_provenance == original
    assert row.is_disabled_by_limiter is True
    assert row.enable_at == -1
    assert await recovery.registry.users_to_enable(0) == []


async def test_db_read_failure_does_not_fall_back_or_clear(recovery, monkeypatch):
    from contextlib import asynccontextmanager

    import db.database as database

    await recovery.disable()
    before = len(recovery.puts)
    original_get_db = database.get_db

    @asynccontextmanager
    async def unavailable():
        raise RuntimeError("DB read unavailable")
        yield

    monkeypatch.setattr(database, "get_db", unavailable)
    assert await recovery.enable() == {"enabled": [], "failed": ["alice"], "not_found": []}
    assert len(recovery.puts) == before
    monkeypatch.setattr(database, "get_db", original_get_db)
    assert (await recovery.row()).is_disabled_by_limiter is True


async def test_deleted_user_clears_only_after_panel_not_found(recovery):
    await recovery.disable()
    del recovery.accounts["alice"]
    assert await recovery.enable() == {"enabled": [], "failed": [], "not_found": ["alice"]}
    assert (await recovery.row()).disable_provenance is None


async def test_restarted_registry_restores_db_inverse_and_permanent_incident(recovery, monkeypatch):
    await recovery.disable(permanent=True)
    monkeypatch.setattr(recovery.registry, "_migrated", False)
    recovery.config.update(disable_method="status", disabled_group_id=88)
    assert (await recovery.registry.entry_of("alice")).is_permanent
    await recovery.enable()
    assert recovery.puts[-1][1]["group_ids"] == [1, 2]


async def test_new_provenance_wins_over_stale_json_backup(recovery):
    await recovery.storage.save_user_groups("alice", [5, 6])
    await recovery.disable()
    await recovery.enable()
    assert recovery.puts[-1][1]["group_ids"] == [1, 2]


async def test_old_ban_backups_are_ignored_without_provenance(recovery):
    from db.crud import UserCRUD

    await recovery.storage.save_user_groups("alice", [5, 6])
    await recovery.registry.disable("alice")
    async with recovery.sessions() as session:
        row = await UserCRUD.get_by_username(session, "alice")
        row.original_groups = [3, 4]
        await session.commit()
    recovery.accounts["alice"].update(group_ids=[8, 99], status="disabled")
    await recovery.enable()
    assert recovery.puts[-1][1] == {"status": "active"}
    assert recovery.accounts["alice"]["group_ids"] == [8, 99]


async def test_cleared_incident_does_not_replay_stale_group_backup(recovery):
    await recovery.storage.save_user_groups("alice", [5, 6])
    await recovery.disable()
    await recovery.enable()
    recovery.accounts["alice"]["group_ids"] = [8, 9]
    await recovery.enable()
    assert recovery.puts[-1][1] == {"status": "active"}
    assert recovery.accounts["alice"]["group_ids"] == [8, 9]
    assert json.loads(Path(recovery.storage.filename).read_text())["user_groups"]["alice"]["groups"] == [5, 6]


async def test_panel_sync_preserves_incident_inverse(recovery):
    from db.crud import UserCRUD

    await recovery.disable()
    async with recovery.sessions() as session:
        await UserCRUD.bulk_upsert_users(session, [{"username": "alice", "group_ids": [8], "status": "disabled"}])
        await session.commit()
    assert (await recovery.row()).disable_provenance["original_groups"] == [1, 2]


async def test_one_enable_failure_does_not_skip_other_users(recovery):
    recovery.accounts["bob"] = {"username": "bob", "group_ids": [3], "status": "active"}
    await recovery.disable()
    await recovery.disable("bob")
    from utils.panel_api import users

    original_get_details = users.get_user_details

    async def details(panel, username):
        if username == "alice":
            raise ValueError("one user's panel read failed")
        return await original_get_details(panel, username)

    from unittest.mock import patch

    with patch.object(users, "get_user_details", details):
        result = await recovery.enable("alice", "bob")
    assert result == {"enabled": ["bob"], "failed": ["alice"], "not_found": []}
    assert await recovery.registry.is_disabled("alice") is True
    assert await recovery.registry.is_disabled("bob") is False


@pytest.mark.parametrize("clear_operation", ["enable", "clear_all"])
async def test_local_registry_clear_preserves_inverse_for_later_panel_enable(recovery, clear_operation):
    await recovery.disable()
    provenance = (await recovery.row()).disable_provenance
    before = len(recovery.puts)
    if clear_operation == "enable":
        assert await recovery.registry.enable("alice") is True
    else:
        assert await recovery.registry.clear_all() == {"alice"}
    row = await recovery.row()
    assert row.is_disabled_by_limiter is False
    assert row.disable_provenance == provenance
    assert len(recovery.puts) == before
    assert recovery.accounts["alice"] == {"username": "alice", "group_ids": [99], "status": "disabled"}
    await recovery.enable()
    assert recovery.puts[-1][1] == {"group_ids": [1, 2], "status": "active"}
    assert (await recovery.row()).disable_provenance is None


@pytest.mark.parametrize("method", ["group", "status"])
async def test_local_clear_preserves_pending_inverse_and_next_disable_method(recovery, method):
    recovery.config["disable_method"] = method
    recovery.fail_put = True
    with pytest.raises(ValueError):
        await recovery.disable()
    provenance = (await recovery.row()).disable_provenance
    assert await recovery.registry.enable("alice") is True
    assert (await recovery.row()).disable_provenance == provenance
    recovery.config.update(disable_method="status" if method == "group" else "group", disabled_group_id=88)
    recovery.fail_put = False
    await recovery.disable()
    expected = {"group_ids": [99], "status": "disabled"} if method == "group" else {"status": "disabled"}
    assert recovery.puts[-1][1] == expected
    assert (await recovery.row()).disable_provenance == provenance
    await recovery.enable()
    assert recovery.accounts["alice"]["group_ids"] == [1, 2]
    assert (await recovery.row()).disable_provenance is None


@pytest.mark.parametrize("method", ["group", "status"])
async def test_confirmed_panel_helper_enable_clears_provenance_without_registry_clear(recovery, method):
    from utils.panel_api import users

    recovery.config["disable_method"] = method
    await recovery.disable()
    helper = users.enable_user_by_group if method == "group" else users.enable_user_by_status
    assert await helper(recovery.panel, "alice") == (True, False)
    row = await recovery.row()
    assert row.is_disabled_by_limiter is False
    assert row.disable_provenance is None


@pytest.mark.parametrize("source", ["group_get", "group_put", "status_put"])
async def test_authoritative_panel_not_found_clears_provenance_without_registry_clear(recovery, monkeypatch, source):
    from utils.panel_api import request_helper, users

    if source == "status_put":
        recovery.config["disable_method"] = "status"
    await recovery.disable()
    helper = users.enable_user_by_status if source == "status_put" else users.enable_user_by_group
    if source == "group_put":
        original_put = request_helper.panel_put

        async def deleted_during_put(panel, path, **kwargs):
            del recovery.accounts["alice"]
            return await original_put(panel, path, **kwargs)

        monkeypatch.setattr(request_helper, "panel_put", deleted_during_put)
    else:
        del recovery.accounts["alice"]
    assert await helper(recovery.panel, "alice") == (False, True)
    row = await recovery.row()
    assert row.is_disabled_by_limiter is False
    assert row.disable_provenance is None


@pytest.mark.parametrize("method", ["group", "status"])
async def test_confirmed_cleanup_failure_then_local_clear_preserves_inverse(recovery, monkeypatch, method):
    from db.crud import UserCRUD
    from utils.panel_api import users

    recovery.config["disable_method"] = method
    await recovery.disable()
    provenance = (await recovery.row()).disable_provenance
    original_set_disabled = UserCRUD.set_disabled
    fail_next_clear = True

    async def first_clear_fails(db, username, **kwargs):
        nonlocal fail_next_clear
        if kwargs.get("disabled") is False and fail_next_clear:
            fail_next_clear = False
            raise RuntimeError("confirmed cleanup transaction failed")
        return await original_set_disabled(db, username, **kwargs)

    monkeypatch.setattr(UserCRUD, "set_disabled", first_clear_fails)
    helper = users.enable_user_by_group if method == "group" else users.enable_user_by_status
    assert await helper(recovery.panel, "alice") == (True, False)
    assert recovery.accounts["alice"]["status"] == "active"
    assert (await recovery.row()).disable_provenance == provenance
    assert await recovery.registry.enable("alice") is True
    assert (await recovery.row()).disable_provenance == provenance
    await recovery.enable()
    assert recovery.accounts["alice"]["group_ids"] == [1, 2]
    assert (await recovery.row()).disable_provenance is None


@pytest.mark.parametrize("failure", ["get", "put"])
async def test_failed_panel_enable_after_local_clear_retains_inverse(recovery, failure):
    await recovery.disable()
    provenance = (await recovery.row()).disable_provenance
    await recovery.registry.enable("alice")
    setattr(recovery, f"fail_{failure}", True)
    assert await recovery.enable() == {"enabled": [], "failed": ["alice"], "not_found": []}
    row = await recovery.row()
    assert row.is_disabled_by_limiter is False
    assert row.disable_provenance == provenance
