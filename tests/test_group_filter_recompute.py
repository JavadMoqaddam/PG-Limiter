#!/usr/bin/env python3
"""Group-filter saves affect current policy independently of legacy display fields."""

import pytest

from test_config_revisions import settings_db  # noqa: F401


@pytest.fixture
def metadata_cache():
    """Seed USER_METADATA_CACHE with bob in group 5 and vip in group 7."""
    import utils.user_sync as user_sync

    saved = dict(user_sync.USER_METADATA_CACHE)
    user_sync.USER_METADATA_CACHE.clear()
    user_sync.USER_METADATA_CACHE.update(
        {
            "bob": {
                "group_ids": [5],
                "owner_username": "adm",
                "is_excepted": False,
                "special_limit": None,
                "is_monitored": True,
                "effective_ip_limit": None,
            },
            "vip": {
                "group_ids": [7],
                "owner_username": "adm",
                "is_excepted": False,
                "special_limit": None,
                "is_monitored": True,
                "effective_ip_limit": None,
            },
        }
    )
    yield user_sync.USER_METADATA_CACHE
    user_sync.USER_METADATA_CACHE.clear()
    user_sync.USER_METADATA_CACHE.update(saved)


class TestLegacyDisplayRecompute:
    """Compatibility recomputation remains available for display callers."""

    @pytest.mark.asyncio
    async def test_include_filter_unmonitors_users_outside_it(self, metadata_cache):
        from utils.user_sync import recompute_all_user_limits

        await recompute_all_user_limits(
            {
                "group_filter": {"enabled": True, "mode": "include", "group_ids": [7]},
            }
        )
        assert metadata_cache["bob"]["is_monitored"] is False
        assert metadata_cache["vip"]["is_monitored"] is True

    @pytest.mark.asyncio
    async def test_disabling_the_filter_monitors_everyone_again(self, metadata_cache):
        from utils.user_sync import recompute_all_user_limits

        await recompute_all_user_limits(
            {
                "group_filter": {"enabled": True, "mode": "include", "group_ids": [7]},
            }
        )
        assert metadata_cache["bob"]["is_monitored"] is False

        await recompute_all_user_limits({"group_filter": {"enabled": False}})
        assert metadata_cache["bob"]["is_monitored"] is True
        assert metadata_cache["vip"]["is_monitored"] is True


@pytest.mark.parametrize(
    "key,value",
    [
        ("group_filter_enabled", "false"),
        ("group_filter_mode", "exclude"),
        ("group_filter_ids", "5"),
    ],
)
async def test_committed_filter_save_changes_current_policy_without_recomputing_display_cache(
    settings_db,
    metadata_cache,
    key,
    value,
):
    import db.database as database
    import telegram_bot.handlers.group_filter as handler
    from db.crud.config import ConfigCRUD
    from utils.read_config import read_config
    from utils.user_policy import evaluate_user_policy

    async with database.get_db() as db:
        await ConfigCRUD.set(db, "group_filter_enabled", "true")
        await ConfigCRUD.set(db, "group_filter_mode", "include")
        await ConfigCRUD.set(db, "group_filter_ids", "7")
    metadata_cache["bob"]["is_monitored"] = False
    before = await read_config()
    assert not evaluate_user_policy(username="bob", config=before, facts=metadata_cache["bob"]).eligible

    assert await handler._save_group_filter_setting(key, value) is True
    after = await read_config(refresh=True)
    assert evaluate_user_policy(username="bob", config=after, facts=metadata_cache["bob"]).eligible
    assert metadata_cache["bob"]["is_monitored"] is False
