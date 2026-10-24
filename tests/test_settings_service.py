"""Shared commands use one committed transaction and report actual outcomes."""

import asyncio
from unittest.mock import AsyncMock

import pytest
from test_config_revisions import settings_db  # noqa: F401

import db.database as database
import utils.read_config as config
from db.crud.config import ConfigCRUD
from db.crud.config_revision import ConfigRevisionCRUD
from db.crud.users import UserCRUD


async def test_settings_batch_rolls_back_values_and_revision(settings_db, monkeypatch):
    from utils.settings_service import SettingsUnavailable, write_settings

    async with database.get_db() as db:
        await ConfigCRUD.set(db, "general_limit", "2")
        before = await ConfigRevisionCRUD.read(db)
    original = ConfigCRUD.set

    async def fail_second(db, key, value, **kwargs):
        if key == "check_interval":
            raise RuntimeError("second write failed")
        return await original(db, key, value, **kwargs)

    monkeypatch.setattr(ConfigCRUD, "set", fail_second)
    with pytest.raises(SettingsUnavailable):
        await write_settings({"general_limit": 7, "check_interval": 180})
    async with database.get_db() as db:
        assert await ConfigCRUD.get(db, "general_limit") == "2"
        assert await ConfigCRUD.get(db, "check_interval") is None
        assert await ConfigRevisionCRUD.read(db) == before


async def test_settings_delete_and_write_commit_together(settings_db):
    from utils.settings_service import read_settings, write_settings

    await write_settings({"general_limit": 7, "country_code": "IR", "check_interval": 180})
    await write_settings({"country_code": ""}, delete_keys=("check_interval",))
    readback = await read_settings()
    assert readback["limits"]["general"] == 7
    assert readback["country_code"] == ""
    async with database.get_db() as db:
        assert await ConfigCRUD.get(db, "check_interval") is None


async def test_user_fetch_runs_before_writer_and_rechecks_synced_user(settings_db, monkeypatch):
    from utils.settings_service import set_special_limit

    async def fetch(username):
        # Panel synchronization needs its own writer, before our writer lock.
        assert not config._config_write_lock.locked()
        async with database.get_db() as db:
            await ConfigCRUD.set(db, "general_limit", "4")
            await UserCRUD.create_or_update(db, username)
        return {"username": username}

    monkeypatch.setattr("utils.user_sync.fetch_and_sync_single_user", fetch)
    assert await asyncio.wait_for(set_special_limit("alice", 7), timeout=2)
    assert (await config.read_config(refresh=True))["limits"]["special"] == {"alice": 7}


async def test_user_commands_reject_missing_membership_without_revision_changes(settings_db, monkeypatch):
    from utils.settings_service import set_exception, set_special_limit

    async with database.get_db() as db:
        await UserCRUD.create_or_update(db, "alice")
        before = await ConfigRevisionCRUD.read(db)
    monkeypatch.setattr("utils.user_sync.fetch_and_sync_single_user", AsyncMock(return_value=None))
    assert not await set_special_limit("alice", 7, require_existing=True)
    assert not await set_special_limit("alice", None, require_existing=True)
    assert not await set_exception("alice", False, require_existing=True)
    assert not await set_special_limit("missing", 7)
    assert not await set_exception("missing", True)
    async with database.get_db() as db:
        assert await ConfigRevisionCRUD.read(db) == before


@pytest.mark.parametrize("limit", [0, -1])
async def test_invalid_special_limits_are_rejected_without_mutation(settings_db, limit):
    from utils.settings_service import set_special_limit

    async with database.get_db() as db:
        await UserCRUD.create_or_update(db, "alice")
    with pytest.raises(ValueError):
        await set_special_limit("alice", limit)
    async with database.get_db() as db:
        assert await ConfigRevisionCRUD.read(db) == 0
        assert await UserCRUD.get_special_limit(db, "alice") is None


async def test_degraded_settings_raise_unavailable(settings_db, monkeypatch):
    from utils.settings_service import SettingsUnavailable, read_settings

    monkeypatch.setattr(config, "read_config", AsyncMock(return_value={"config_degraded": True}))
    with pytest.raises(SettingsUnavailable):
        await read_settings()


async def test_unavailable_database_cannot_publish_env_defaults(settings_db, monkeypatch):
    from utils.settings_service import SettingsUnavailable, read_settings

    monkeypatch.setattr(config, "DB_AVAILABLE", False)
    with pytest.raises(SettingsUnavailable):
        await read_settings()


@pytest.mark.parametrize("command", ["settings", "special", "except"])
async def test_corrupt_revision_is_a_persistence_failure(settings_db, command):
    from db.crud.config_revision import REVISION_KEY
    from db.models import Config
    from utils import settings_service

    async with database.get_db() as db:
        await UserCRUD.create_or_update(db, "alice")
        db.add(Config(key=REVISION_KEY, value={"revision": "corrupt"}))
    with pytest.raises(settings_service.SettingsUnavailable):
        if command == "settings":
            await settings_service.write_settings({"general_limit": 7})
        elif command == "special":
            await settings_service.set_special_limit("alice", 7)
        else:
            await settings_service.set_exception("alice", True)
    async with database.get_db() as db:
        assert await ConfigCRUD.get(db, "general_limit") is None
        user = await UserCRUD.get_by_username(db, "alice")
        assert user.special_limit is None
        assert not user.is_excepted
        assert await ConfigCRUD.get(db, REVISION_KEY) == {"revision": "corrupt"}
