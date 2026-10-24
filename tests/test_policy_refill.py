"""Cold factual cache refills cannot resurrect removed overrides or derived policy."""

import json

import pytest

import db.database as database
import utils.read_config as config
import utils.user_sync as user_sync
from db.crud.config import ConfigCRUD
from db.crud.users import UserCRUD
from db.models import User
from test_config_revisions import settings_db  # noqa: F401
from utils.check_usage import get_active_users_metadata_batch, resolve_effective_limit
from utils.user_policy import evaluate_user_policy


@pytest.fixture
def clean_metadata(monkeypatch):
    monkeypatch.setattr(user_sync, "USER_METADATA_CACHE", {})


@pytest.mark.parametrize("groups,group_limits,want", [([], {}, 5), ([7], {"7": 3}, 3)])
async def test_removed_override_and_exception_survive_committed_eviction_and_cold_refill(
    settings_db,
    clean_metadata,
    groups,
    group_limits,
    want,
):
    async with database.get_db() as db:
        db.add(User(username="alice", group_ids=groups, owner_username=None, is_monitored=False, effective_ip_limit=2))
        await db.flush()
        await UserCRUD.set_special_limit(db, "alice", 2, fetch_from_panel=False)
        await UserCRUD.set_excepted(db, "alice", True, fetch_from_panel=False)
        await ConfigCRUD.set(db, "general_limit", "5")
        await ConfigCRUD.set(db, "group_limits", json.dumps(group_limits))
    before = (await get_active_users_metadata_batch(["alice"]))["alice"]
    assert not evaluate_user_policy(username="alice", config=await config.read_config(), facts=before).eligible
    assert "alice" in user_sync.USER_METADATA_CACHE

    async with database.get_db() as db:
        await UserCRUD.set_special_limit(db, "alice", None, fetch_from_panel=False)
        await UserCRUD.set_excepted(db, "alice", False, fetch_from_panel=False)
    assert "alice" not in user_sync.USER_METADATA_CACHE

    facts = (await get_active_users_metadata_batch(["alice"], strict=True))["alice"]
    current = await config.read_config(refresh=True)
    policy = evaluate_user_policy(username="alice", config=current, facts=facts)
    assert (policy.known, policy.eligible, policy.limit) == (True, True, want)
    assert await resolve_effective_limit("alice", config=current, metadata=facts, special_limit={}) == want
    assert "is_monitored" not in facts and "effective_ip_limit" not in facts

    await user_sync.refresh_user_metadata_cache()
    refreshed = user_sync.USER_METADATA_CACHE["alice"]
    assert refreshed == {"group_ids": groups, "owner_username": None}
    assert evaluate_user_policy(username="alice", config=current, facts=refreshed).limit == want
    async with database.get_db() as db:
        row = await UserCRUD.get_by_username(db, "alice")
        assert (row.is_monitored, row.effective_ip_limit) == (False, 2)


async def test_bulk_panel_upsert_cannot_write_local_or_derived_columns(settings_db):
    async with database.get_db() as db:
        db.add(
            User(
                username="alice",
                group_ids=[1],
                owner_username="old",
                is_monitored=False,
                effective_ip_limit=11,
                special_limit=7,
                is_excepted=True,
                exception_reason="local",
                excepted_by="operator",
                is_disabled_by_limiter=True,
                original_groups=[2],
                punishment_step=3,
            )
        )
    incoming = {
        "username": "alice",
        "panel_id": 10,
        "status": "limited",
        "owner_id": 5,
        "owner_username": "new",
        "group_ids": [7],
        "note": "panel note",
        "data_limit": 4,
        "used_traffic": 2,
        "special_limit": 99,
        "is_excepted": False,
        "is_monitored": True,
        "effective_ip_limit": 99,
        "exception_reason": "bad",
        "excepted_by": "panel",
        "is_disabled_by_limiter": False,
        "original_groups": [99],
        "punishment_step": 99,
    }
    async with database.get_db() as db:
        assert (
            await UserCRUD.bulk_upsert_users(
                db, [incoming, {"username": "bob", "is_monitored": False, "effective_ip_limit": 99}]
            )
            == 2
        )
    async with database.get_db() as db:
        row = await UserCRUD.get_by_username(db, "alice")
        assert (
            row.panel_id,
            row.status,
            row.owner_id,
            row.owner_username,
            row.group_ids,
            row.note,
            row.data_limit,
            row.used_traffic,
        ) == (10, "limited", 5, "new", [7], "panel note", 4, 2)
        assert (
            row.special_limit,
            row.is_excepted,
            row.exception_reason,
            row.excepted_by,
            row.is_disabled_by_limiter,
            row.original_groups,
            row.punishment_step,
        ) == (
            7,
            True,
            "local",
            "operator",
            True,
            [2],
            3,
        )
        assert (row.is_monitored, row.effective_ip_limit) == (False, 11)
        new = await UserCRUD.get_by_username(db, "bob")
        assert (new.is_monitored, new.effective_ip_limit) == (True, None)
