"""Management writes must land in the canonical store enforcement reads, not a
retired JSON plane that only the REST/CLI surfaces see."""

import contextlib

import pytest
from fastapi import HTTPException

import api_server
from api_server import ExceptUser


async def test_all_surfaces_share_canonical_state(monkeypatch):
    saved = []

    async def fake_save(key, value):
        saved.append((key, value))
        return True

    monkeypatch.setattr("utils.read_config.save_config_value", fake_save)

    result = await api_server.set_general_limit(limit=7, username="admin")

    # Written through the same canonical value read_config exposes to every surface.
    assert ("general_limit", 7) in saved
    assert result["success"] is True


async def test_general_limit_persistence_failure_returns_503(monkeypatch):
    async def fake_save(key, value):
        return False

    monkeypatch.setattr("utils.read_config.save_config_value", fake_save)

    with pytest.raises(HTTPException) as exc:
        await api_server.set_general_limit(limit=7, username="admin")
    assert exc.value.status_code == 503


async def test_rest_whitelist_changes_enforcement(monkeypatch):
    calls = []

    class FakeUserCRUD:
        @staticmethod
        async def set_excepted(db, username, excepted=True, **kwargs):
            calls.append((username, excepted))

    class FakeDB:
        async def commit(self):
            pass

    @contextlib.asynccontextmanager
    async def fake_get_db():
        yield FakeDB()

    monkeypatch.setattr("db.database.get_db", fake_get_db)
    monkeypatch.setattr("db.crud.UserCRUD", FakeUserCRUD)

    result = await api_server.add_except_user(ExceptUser(username="alice"), username="admin")

    # Whitelisting routes to the SQLite service the enforcement loop reads from.
    assert ("alice", True) in calls
    assert result["success"] is True
