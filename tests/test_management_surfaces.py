"""Management interfaces publish committed state that enforcement can read."""

import json
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from test_config_revisions import settings_db  # noqa: F401

import api_server
import db.database as database
import utils.read_config as config
from db.crud.config import ConfigCRUD
from db.crud.config_revision import ConfigRevisionCRUD
from db.crud.users import UserCRUD


@pytest.fixture(autouse=True)
def retired_files(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.json").write_text(json.dumps({"limits": {"general": 99, "special": {"ghost": 99}}}))
    (tmp_path / "backup.json").write_text(json.dumps({"special": {"ghost": 100}}))
    monkeypatch.setattr(api_server, "load_disabled_users", AsyncMock(return_value={}))


async def seed_user(username="alice"):
    async with database.get_db() as db:
        await UserCRUD.create_or_update(db, username)


async def test_api_special_limit_is_visible_to_daemon(settings_db):
    await seed_user()
    await config.read_config()
    response = await api_server.add_user_limit(api_server.UserLimit(username="alice", limit=7), username="admin")
    assert response["success"]
    assert (await config.read_config(refresh=True))["limits"]["special"] == {"alice": 7}
    assert (await api_server.list_user_limits(username="admin"))["data"] == [{"username": "alice", "limit": 7}]
    assert (await api_server.get_user_limit("alice", username="admin"))["data"]["limit"] == 7


async def test_api_timing_and_status_use_committed_canonical_keys(settings_db):
    await config.read_config()
    await api_server.set_general_limit(limit=7, username="admin")
    await api_server.set_check_interval(interval=180, username="admin")
    await api_server.set_reenable_time(seconds=900, username="admin")
    readback = await config.read_config(refresh=True)
    assert (readback["limits"]["general"], readback["check_interval"], readback["time_to_active_users"]) == (
        7,
        180,
        900,
    )
    status = await api_server.get_status(username="admin")
    assert (status.general_limit, status.check_interval, status.reenable_time) == (7, 180, 900)


async def test_api_whitelist_readback_and_missing_membership(settings_db):
    await seed_user()
    await config.read_config()
    assert (await api_server.add_except_user(api_server.ExceptUser(username="alice"), username="admin"))["success"]
    assert (await api_server.list_except_users(username="admin"))["data"] == ["alice"]
    assert (await api_server.get_status(username="admin")).except_users_count == 1
    assert (await config.read_config(refresh=True))["except_users"] == ["alice"]
    await api_server.delete_except_user("alice", username="admin")
    assert (await api_server.list_except_users(username="admin"))["data"] == []
    with pytest.raises(HTTPException) as raised:
        await api_server.delete_except_user("alice", username="admin")
    assert raised.value.status_code == 404


async def test_api_put_upserts_and_delete_exposes_general_fallback(settings_db):
    await seed_user()
    await api_server.set_general_limit(limit=4, username="admin")
    await api_server.update_user_limit("alice", api_server.UpdateLimit(limit=8), username="admin")
    await api_server.delete_user_limit("alice", username="admin")
    assert (await api_server.get_user_limit("alice", username="admin"))["data"] == {
        "username": "alice",
        "limit": 4,
        "is_special": False,
        "general_limit": 4,
    }
    with pytest.raises(HTTPException) as raised:
        await api_server.delete_user_limit("alice", username="admin")
    assert raised.value.status_code == 404


@pytest.mark.parametrize("kind", ["special", "except"])
async def test_api_missing_user_never_reports_success(settings_db, monkeypatch, kind):
    monkeypatch.setattr("utils.user_sync.fetch_and_sync_single_user", AsyncMock(return_value=None))
    with pytest.raises(HTTPException) as raised:
        if kind == "special":
            await api_server.add_user_limit(api_server.UserLimit(username="missing", limit=7), username="admin")
        else:
            await api_server.add_except_user(api_server.ExceptUser(username="missing"), username="admin")
    assert raised.value.status_code == 404


async def test_api_read_and_write_failures_return_503(settings_db, monkeypatch):
    async def fail(*args, **kwargs):
        raise RuntimeError("disk unavailable")

    monkeypatch.setattr(ConfigCRUD, "set", fail)
    with pytest.raises(HTTPException) as raised:
        await api_server.set_general_limit(limit=7, username="admin")
    assert raised.value.status_code == 503
    monkeypatch.setattr(config, "read_config", AsyncMock(return_value={"config_degraded": True}))
    with pytest.raises(HTTPException) as raised:
        await api_server.get_status(username="admin")
    assert raised.value.status_code == 503


async def test_api_config_masks_copied_credentials_and_projects_timing(settings_db, monkeypatch):
    monkeypatch.setenv("PANEL_PASSWORD", "private-panel-password")
    monkeypatch.setenv("API_PASSWORD", "private-api-password")
    await api_server.set_check_interval(interval=180, username="admin")
    data = (await api_server.get_config(username="admin"))["data"]
    assert data["timing"]["check_interval"] == 180
    assert data["panel"]["password"] == "***"
    assert data["api"]["password"] == "***"
    assert "config_degraded" not in data
    assert "_runtime_config_revision" not in data
    assert (await config.read_config())["panel"]["password"] == "private-panel-password"


async def test_api_failed_write_does_not_advance_revision(settings_db, monkeypatch):
    async with database.get_db() as db:
        before = await ConfigRevisionCRUD.read(db)
    monkeypatch.setattr(ConfigCRUD, "set", AsyncMock(side_effect=RuntimeError("disk unavailable")))
    with pytest.raises(HTTPException):
        await api_server.set_check_interval(interval=180, username="admin")
    async with database.get_db() as db:
        assert await ConfigRevisionCRUD.read(db) == before
        assert await ConfigCRUD.get(db, "check_interval") is None


@pytest.mark.parametrize("command", ["general", "special"])
async def test_api_corrupt_revision_returns_503(settings_db, command):
    from db.crud.config_revision import REVISION_KEY
    from db.models import Config

    await seed_user()
    async with database.get_db() as db:
        db.add(Config(key=REVISION_KEY, value={"revision": "corrupt"}))
    with pytest.raises(HTTPException) as raised:
        if command == "general":
            await api_server.set_general_limit(limit=7, username="admin")
        else:
            await api_server.add_user_limit(api_server.UserLimit(username="alice", limit=7), username="admin")
    assert raised.value.status_code == 503
    async with database.get_db() as db:
        assert await ConfigCRUD.get(db, "general_limit") is None
        assert await UserCRUD.get_special_limit(db, "alice") is None
