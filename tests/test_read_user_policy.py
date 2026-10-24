"""Final policy reads use coherent current settings and fresh user facts."""

import asyncio

import pytest

import db.database as database
import utils.read_config as configuration
from db.crud.config import ConfigCRUD
from db.crud.config_revision import ConfigRevisionCRUD
from db.crud.users import UserCRUD
from db.models import User
from test_config_revisions import settings_db  # noqa: F401


async def test_policy_reader_uses_current_canonical_overrides_and_whitelist(settings_db):
    from utils.settings_service import read_user_policy

    async with database.get_db() as db:
        db.add(User(username="alice", group_ids=[7], is_monitored=False, effective_ip_limit=99))
        await db.flush()
        await ConfigCRUD.set(db, "general_limit", "5")
        await ConfigCRUD.set(db, "group_limits", '{"7":4}')
        await UserCRUD.set_special_limit(db, "alice", 3, fetch_from_panel=False)
        await UserCRUD.set_excepted(db, "alice", True, fetch_from_panel=False)
    before = await read_user_policy("alice")
    assert (before.known, before.eligible, before.limit, before.reason, before.limit_source) == (
        True,
        False,
        3,
        "whitelisted",
        "special",
    )
    async with database.get_db() as db:
        await UserCRUD.set_special_limit(db, "alice", None, fetch_from_panel=False)
        await UserCRUD.set_excepted(db, "alice", False, fetch_from_panel=False)
    after = await read_user_policy("alice")
    assert (after.known, after.eligible, after.limit, after.reason, after.limit_source) == (
        True,
        True,
        4,
        "eligible",
        "group",
    )


@pytest.mark.parametrize("groups,owner,reason", [([], "owner", "group_filtered"), ([7], "other", "admin_filtered")])
async def test_policy_reader_refreshes_facts_even_when_configuration_revision_is_unchanged(
    settings_db,
    groups,
    owner,
    reason,
):
    from utils.settings_service import read_user_policy

    async with database.get_db() as db:
        await UserCRUD.create_or_update(db, "alice", group_ids=[7], owner_username="owner")
        await ConfigCRUD.set(db, "general_limit", "5")
        await ConfigCRUD.set(db, "group_filter_enabled", "true")
        await ConfigCRUD.set(db, "group_filter_ids", "7")
        await ConfigCRUD.set(db, "admin_filter_enabled", "true")
        await ConfigCRUD.set(db, "admin_filter_usernames", "owner")
        revision = await ConfigRevisionCRUD.read(db)
    assert (await read_user_policy("alice", expected_db_revision=revision)).eligible
    async with database.get_db() as db:
        await UserCRUD.create_or_update(db, "alice", group_ids=groups, owner_username=owner)
        assert await ConfigRevisionCRUD.read(db) == revision
    current = await read_user_policy("alice", expected_db_revision=revision)
    assert (current.known, current.eligible, current.limit, current.reason) == (True, False, 5, reason)


async def test_policy_reader_rejects_superseded_settings_revision(settings_db):
    from utils.settings_service import read_user_policy

    async with database.get_db() as db:
        await UserCRUD.create_or_update(db, "alice")
        await ConfigCRUD.set(db, "general_limit", "5")
        old_revision = await ConfigRevisionCRUD.read(db)
    assert (await read_user_policy("alice", expected_db_revision=old_revision)).limit == 5
    async with database.get_db() as db:
        await ConfigCRUD.set(db, "general_limit", "7")
        new_revision = await ConfigRevisionCRUD.read(db)
    assert await read_user_policy("alice", expected_db_revision=old_revision) is None
    assert (await read_user_policy("alice", expected_db_revision=new_revision)).limit == 7


async def test_policy_reader_returns_unknown_without_fetching_missing_user(settings_db, monkeypatch):
    from utils.settings_service import read_user_policy

    async def unexpected_fetch(*_args, **_kwargs):
        raise AssertionError("A policy read must not fetch panel facts")

    monkeypatch.setattr("utils.user_sync.fetch_and_sync_single_user", unexpected_fetch)
    unknown = await read_user_policy("missing")
    assert (unknown.known, unknown.eligible, unknown.limit, unknown.reason, unknown.limit_source) == (
        False,
        False,
        None,
        "unknown",
        None,
    )
    async with database.get_db() as db:
        assert await UserCRUD.get_by_username(db, "missing") is None


@pytest.mark.parametrize("failure", ["disabled", "snapshot_exception", "facts_exception"])
async def test_policy_reader_reports_unavailable_instead_of_default_or_superseded_policy(
    settings_db,
    monkeypatch,
    failure,
):
    from utils.settings_service import SettingsUnavailable, read_user_policy

    async with database.get_db() as db:
        await UserCRUD.create_or_update(db, "alice")
        await ConfigCRUD.set(db, "general_limit", "5")
    await configuration.read_config()

    async def unreadable(*_args, **_kwargs):
        raise RuntimeError("database read failed")

    if failure == "disabled":
        monkeypatch.setattr(configuration, "DB_AVAILABLE", False)
    elif failure == "snapshot_exception":
        monkeypatch.setattr(configuration, "read_config_snapshot", unreadable)
    else:
        monkeypatch.setattr(UserCRUD, "get_policy_facts", unreadable)
    with pytest.raises(SettingsUnavailable):
        await read_user_policy("alice", expected_db_revision=-1)


async def test_policy_reader_propagates_cancellation(settings_db, monkeypatch):
    from utils.settings_service import read_user_policy

    async def cancelled(*_args, **_kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr(configuration, "read_config_snapshot", cancelled)
    with pytest.raises(asyncio.CancelledError):
        await read_user_policy("alice")
