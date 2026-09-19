"""The API must own the database lifecycle through its FastAPI lifespan."""

from unittest.mock import AsyncMock

import api_server
import db.database as db_database


async def test_database_initialized(monkeypatch):
    init = AsyncMock()
    close = AsyncMock()
    monkeypatch.setattr(db_database, "init_db", init)
    monkeypatch.setattr(db_database, "close_db", close)

    async with api_server.lifespan(api_server.app):
        init.assert_awaited_once()
        close.assert_not_awaited()


async def test_database_closed(monkeypatch):
    init = AsyncMock()
    close = AsyncMock()
    monkeypatch.setattr(db_database, "init_db", init)
    monkeypatch.setattr(db_database, "close_db", close)

    async with api_server.lifespan(api_server.app):
        pass

    close.assert_awaited_once()
