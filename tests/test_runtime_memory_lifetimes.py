"""Discard preparation-only objects before the next expensive phase."""

import weakref
from datetime import datetime

from sqlalchemy import select

import db.database as database
import utils.user_sync as sync
from db.crud.users import UserCRUD
from db.models import User
from test_config_revisions import settings_db  # noqa: F401
import test_api_evidence
from utils.types import PanelType

api_cycle = test_api_evidence.api_cycle


async def test_sync_snapshot_preserves_all_typed_fields_for_concurrent_edit_detection(settings_db):
    async with database.get_db() as db:
        db.add(
            User(
                username="snapshot-user",
                panel_id=42,
                status="disabled",
                owner_id=7,
                owner_username="owner",
                group_ids=[3, 8],
                note="preserve note",
                data_limit=42.5,
                used_traffic=1.25,
                created_at=datetime(2026, 1, 1),
                expire_at=datetime(2027, 1, 1),
                last_synced_at=datetime(2026, 1, 2),
                is_monitored=False,
                effective_ip_limit=4,
                is_excepted=True,
                exception_reason="reason",
                excepted_by="operator",
                excepted_at=datetime(2026, 1, 3),
                special_limit=5,
                special_limit_updated_at=datetime(2026, 1, 4),
                is_disabled_by_limiter=True,
                disabled_at=100.5,
                enable_at=200.5,
                original_groups=[4, 9],
                disable_provenance={"incident": "saved", "groups": [4, 9]},
                disable_reason="limit",
                punishment_step=2,
            )
        )
        db.add(User(username="nullable-user"))
    async with database.get_db() as db:
        users = (await db.scalars(select(User))).all()
        expected = {user.username: tuple(getattr(user, c.name) for c in User.__table__.columns) for user in users}
        snapshot = await UserCRUD.get_sync_deletion_snapshots(db)
    assert snapshot == expected
    assert {k: tuple(type(v) for v in row) for k, row in snapshot.items()} == {
        k: tuple(type(v) for v in row) for k, row in expected.items()
    }


async def test_sync_releases_raw_panel_listing_before_database_writes(settings_db, monkeypatch):
    class Listing(list):
        pass

    references = []

    async def fetch(*_args, **_kwargs):
        listing = Listing([{"username": "memory-user", "id": 42, "group_ids": [3]}])
        references.append(weakref.ref(listing))
        return listing

    original = UserCRUD.bulk_upsert_users

    async def write(db, values):
        assert references[0]() is None, "Raw API listing survives into the write phase"
        return await original(db, values)

    monkeypatch.setattr(sync, "get_all_users_with_details", fetch)
    monkeypatch.setattr(UserCRUD, "bulk_upsert_users", write)
    assert await sync.sync_users_to_database(PanelType(panel_domain="panel.test", panel_api_key="test")) == (1, 0, 0)
    async with database.get_db() as db:
        assert (await UserCRUD.get_by_username(db, "memory-user")).group_ids == [3]


async def test_api_releases_preconfirmation_user_graph_before_membership_io(api_cycle, monkeypatch):
    import utils.ip_source_api as api

    references = []
    original_build = api._build_users_from_payloads
    original_context = api._resolve_node_context

    async def build(*args, **kwargs):
        users, stats = await original_build(*args, **kwargs)
        references.append(weakref.ref(users))
        return users, stats

    async def context(*args, **kwargs):
        if references:
            assert references[0]() is None, "First user graph survives across membership confirmation"
        return await original_context(*args, **kwargs)

    monkeypatch.setattr(api, "_build_users_from_payloads", build)
    monkeypatch.setattr(api, "_resolve_node_context", context)
    assert await api_cycle.run()
    assert len(references) == 2
    assert set(api_cycle.warning.warnings) == {"alice", "bob"}
