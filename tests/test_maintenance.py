"""Retention prunes violation history without touching the retired ISP cache."""

import time
from contextlib import asynccontextmanager

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import utils.maintenance as maintenance
from db.models import Base, ViolationHistory


async def test_retention_removes_only_expired_violations(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'retention.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with sessions() as session:
        session.add_all(
            [
                ViolationHistory(
                    username="old", timestamp=time.time() - 31 * 86400, step_applied=1, disable_duration=30
                ),
                ViolationHistory(username="recent", timestamp=time.time(), step_applied=1, disable_duration=30),
            ]
        )
        await session.commit()

    @asynccontextmanager
    async def get_test_db():
        async with sessions() as session:
            yield session
            await session.commit()

    monkeypatch.setattr(maintenance, "get_db", get_test_db)
    monkeypatch.setattr(maintenance, "DB_AVAILABLE", True)
    try:
        assert await maintenance.run_retention_once(days=30) == {"violations": 1}
        async with sessions() as session:
            assert list(await session.scalars(select(ViolationHistory.username))) == ["recent"]
    finally:
        await engine.dispose()


async def test_run_retention_once_noop_without_db(monkeypatch):
    monkeypatch.setattr(maintenance, "DB_AVAILABLE", False)
    result = await maintenance.run_retention_once(days=30)
    assert result == {"violations": 0}
