"""Regression tests for commit-coupled user metadata cache invalidation."""

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import db.database as database
from db.crud.users import UserCRUD
from db.models import Base, User
from utils import user_sync


@pytest.fixture
async def sessions(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'transactions.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        yield factory
    finally:
        user_sync.USER_METADATA_CACHE.clear()
        await engine.dispose()


async def add_user(factory, username: str, *, special_limit: int | None = None) -> None:
    async with factory() as session:
        session.add(User(username=username, special_limit=special_limit))
        await session.commit()


async def test_concurrent_refill_cannot_restore_precommit_state(sessions):
    await add_user(sessions, "alice", special_limit=2)

    async with sessions() as writer:
        await UserCRUD.set_special_limit(
            writer, "alice", 9, fetch_from_panel=False
        )

        async with sessions() as reader:
            stale_limit = await reader.scalar(
                select(User.special_limit).where(User.username == "alice")
            )
        assert stale_limit == 2
        user_sync.USER_METADATA_CACHE["alice"] = {"special_limit": stale_limit}

        await writer.commit()

    assert "alice" not in user_sync.USER_METADATA_CACHE


async def test_rollback_does_not_publish_invalidation(sessions):
    await add_user(sessions, "alice", special_limit=2)
    cached = {"special_limit": 2}
    user_sync.USER_METADATA_CACHE["alice"] = cached

    async with sessions() as writer:
        await UserCRUD.set_special_limit(
            writer, "alice", 9, fetch_from_panel=False
        )
        await writer.rollback()

    assert user_sync.USER_METADATA_CACHE["alice"] is cached
    async with sessions() as reader:
        assert await reader.scalar(
            select(User.special_limit).where(User.username == "alice")
        ) == 2


async def test_delete_many_invalidates_every_deleted_username(sessions):
    await add_user(sessions, "alice")
    await add_user(sessions, "bob")
    user_sync.USER_METADATA_CACHE.update(
        {
            "alice": {"special_limit": 2},
            "bob": {"special_limit": 3},
            "carol": {"special_limit": 4},
        }
    )

    async with sessions() as writer:
        assert await UserCRUD.delete_many(writer, ["alice", "bob"]) == 2
        assert {"alice", "bob", "carol"} <= user_sync.USER_METADATA_CACHE.keys()
        await writer.commit()

    assert user_sync.USER_METADATA_CACHE == {"carol": {"special_limit": 4}}


async def test_explicit_commit_publishes_registered_invalidation(sessions):
    await add_user(sessions, "alice", special_limit=2)
    user_sync.USER_METADATA_CACHE["alice"] = {"special_limit": 2}

    async with sessions() as writer:
        await UserCRUD.set_excepted(
            writer,
            "alice",
            excepted=True,
            fetch_from_panel=False,
        )
        assert "alice" in user_sync.USER_METADATA_CACHE
        await writer.commit()
        assert "alice" not in user_sync.USER_METADATA_CACHE


async def test_nested_commit_does_not_publish_before_outer_rollback(sessions):
    await add_user(sessions, "outer", special_limit=2)
    cached = {"special_limit": 2}
    user_sync.USER_METADATA_CACHE["outer"] = cached

    async with sessions() as writer:
        await UserCRUD.set_special_limit(
            writer, "outer", 9, fetch_from_panel=False
        )
        nested = await writer.begin_nested()
        await nested.commit()
        assert user_sync.USER_METADATA_CACHE["outer"] is cached
        await writer.rollback()

    assert user_sync.USER_METADATA_CACHE["outer"] is cached


async def test_nested_rollback_preserves_outer_invalidation(sessions):
    await add_user(sessions, "outer", special_limit=2)
    user_sync.USER_METADATA_CACHE["outer"] = {"special_limit": 2}

    async with sessions() as writer:
        await UserCRUD.set_special_limit(
            writer, "outer", 9, fetch_from_panel=False
        )
        nested = await writer.begin_nested()
        await nested.rollback()
        await writer.commit()

    assert "outer" not in user_sync.USER_METADATA_CACHE


async def test_nested_only_rollback_does_not_publish(sessions):
    await add_user(sessions, "nested", special_limit=2)
    cached = {"special_limit": 2}
    user_sync.USER_METADATA_CACHE["nested"] = cached

    async with sessions() as writer:
        nested = await writer.begin_nested()
        await UserCRUD.set_special_limit(
            writer, "nested", 9, fetch_from_panel=False
        )
        await nested.rollback()
        await writer.commit()

    assert user_sync.USER_METADATA_CACHE["nested"] is cached


async def test_get_db_context_publishes_on_automatic_commit(sessions, monkeypatch):
    await add_user(sessions, "alice", special_limit=2)
    user_sync.USER_METADATA_CACHE["alice"] = {"special_limit": 2}
    monkeypatch.setattr(database, "AsyncSessionLocal", sessions)

    async with database.get_db() as writer:
        await UserCRUD.set_special_limit(
            writer, "alice", 9, fetch_from_panel=False
        )
        assert "alice" in user_sync.USER_METADATA_CACHE

    assert "alice" not in user_sync.USER_METADATA_CACHE
