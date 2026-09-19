"""get_db must roll back and close on any exit, including cancellation, and the
cleanup must not be skipped when the body is cancelled."""

import asyncio

import pytest

import db.database as db_database


class FakeSession:
    def __init__(self):
        self.calls = []

    async def commit(self):
        self.calls.append("commit")

    async def rollback(self):
        self.calls.append("rollback")

    async def close(self):
        self.calls.append("close")


async def test_get_db_commits_and_closes_on_success(monkeypatch):
    fake = FakeSession()
    monkeypatch.setattr(db_database, "AsyncSessionLocal", lambda: fake)

    async with db_database.get_db() as session:
        assert session is fake

    assert fake.calls == ["commit", "close"]


async def test_get_db_rolls_back_and_closes_on_error(monkeypatch):
    fake = FakeSession()
    monkeypatch.setattr(db_database, "AsyncSessionLocal", lambda: fake)

    with pytest.raises(ValueError):
        async with db_database.get_db():
            raise ValueError("boom")

    assert "rollback" in fake.calls
    assert "close" in fake.calls
    assert "commit" not in fake.calls


async def test_get_db_rolls_back_and_closes_on_cancellation(monkeypatch):
    fake = FakeSession()
    monkeypatch.setattr(db_database, "AsyncSessionLocal", lambda: fake)

    with pytest.raises(asyncio.CancelledError):
        async with db_database.get_db():
            raise asyncio.CancelledError()

    # Cancellation is not a subclass of Exception; it must still roll back and close.
    assert "rollback" in fake.calls
    assert "close" in fake.calls


async def test_get_db_closes_even_if_rollback_fails(monkeypatch):
    class BadRollback(FakeSession):
        async def rollback(self):
            self.calls.append("rollback")
            raise RuntimeError("rollback interrupted")

    fake = BadRollback()
    monkeypatch.setattr(db_database, "AsyncSessionLocal", lambda: fake)

    with pytest.raises(Exception):
        async with db_database.get_db():
            raise ValueError("boom")

    assert "close" in fake.calls
