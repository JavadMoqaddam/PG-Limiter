"""Regression tests for panel-first disabled-user recovery routes."""

import time

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import api_server
from db.models import Base, User
from utils import handel_dis_users as dis_registry
from utils import panel_api


class FakeRegistry:
    def __init__(self, usernames: set[str], events: list[str]):
        self.usernames = set(usernames)
        self.events = events
        self.clear_all_called = False

    async def is_disabled(self, username: str) -> bool:
        self.events.append(f"registry:check:{username}")
        return username in self.usernames

    async def disabled_usernames(self) -> set[str]:
        self.events.append("registry:list")
        return set(self.usernames)

    async def enable(self, username: str) -> bool:
        self.events.append(f"registry:enable:{username}")
        if username not in self.usernames:
            return False
        self.usernames.remove(username)
        return True

    async def clear_all(self) -> set[str]:
        self.events.append("registry:clear_all")
        self.clear_all_called = True
        cleared = set(self.usernames)
        self.usernames.clear()
        return cleared


def install_registry(monkeypatch, usernames: set[str], events: list[str]) -> FakeRegistry:
    registry = FakeRegistry(usernames, events)
    monkeypatch.setattr(dis_registry, "is_disabled", registry.is_disabled)
    monkeypatch.setattr(dis_registry, "disabled_usernames", registry.disabled_usernames)
    monkeypatch.setattr(dis_registry, "enable", registry.enable)
    monkeypatch.setattr(dis_registry, "clear_all", registry.clear_all)
    return registry


def install_panel(monkeypatch, events: list[str], result: dict[str, list[str]]) -> None:
    panel_data = object()

    async def fake_get_panel_data():
        events.append("panel:data")
        return panel_data

    async def fake_enable_selected_users(actual_panel_data, usernames):
        assert actual_panel_data is panel_data
        events.append(f"panel:enable:{','.join(sorted(usernames))}")
        return result

    monkeypatch.setattr(api_server, "get_panel_data", fake_get_panel_data, raising=False)
    monkeypatch.setattr(panel_api, "enable_selected_users", fake_enable_selected_users)


async def test_enable_disabled_user_updates_panel_before_clearing_registry(monkeypatch):
    events: list[str] = []
    registry = install_registry(monkeypatch, {"alice"}, events)
    install_panel(
        monkeypatch,
        events,
        {"enabled": ["alice"], "failed": [], "not_found": []},
    )

    response = await api_server.enable_disabled_user("alice", username="admin")

    assert response["success"] is True
    assert registry.usernames == set()
    assert events.index("panel:enable:alice") < events.index("registry:enable:alice")


async def test_enable_disabled_user_preserves_record_when_panel_fails(monkeypatch):
    events: list[str] = []
    registry = install_registry(monkeypatch, {"alice"}, events)
    install_panel(
        monkeypatch,
        events,
        {"enabled": [], "failed": ["alice"], "not_found": []},
    )

    with pytest.raises(HTTPException) as raised:
        await api_server.enable_disabled_user("alice", username="admin")

    assert raised.value.status_code == 502
    assert raised.value.detail == "Panel enable failed; record preserved"
    assert registry.usernames == {"alice"}
    assert "registry:enable:alice" not in events


async def test_enable_all_clears_only_confirmed_users(monkeypatch):
    events: list[str] = []
    registry = install_registry(monkeypatch, {"alice", "bob", "carol"}, events)
    install_panel(
        monkeypatch,
        events,
        {"enabled": ["alice"], "failed": ["bob"], "not_found": ["carol"]},
    )

    with pytest.raises(HTTPException) as raised:
        await api_server.enable_all_disabled_users(username="admin")

    assert raised.value.status_code == 502
    assert registry.usernames == {"bob"}
    assert registry.clear_all_called is False
    assert {event for event in events if event.startswith("registry:enable:")} == {
        "registry:enable:alice",
        "registry:enable:carol",
    }


@pytest.fixture
async def isolated_database(tmp_path, monkeypatch):
    import db.database as database

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'api.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(database, "AsyncSessionLocal", sessions)
    monkeypatch.setattr(dis_registry, "LEGACY_JSON_PATH", str(tmp_path / "absent.json"))
    monkeypatch.setattr(dis_registry, "_migrated", True)
    yield sessions
    await engine.dispose()


async def test_group_enable_preserves_original_groups_until_panel_success(
    monkeypatch, isolated_database
):
    async with isolated_database() as session:
        session.add(
            User(
                username="grouped",
                status="disabled",
                is_disabled_by_limiter=True,
                disabled_at=time.time(),
                original_groups=[7, 11],
            )
        )
        await session.commit()

    install_panel(
        monkeypatch,
        [],
        {"enabled": [], "failed": ["grouped"], "not_found": []},
    )

    status_code = None
    try:
        await api_server.enable_disabled_user("grouped", username="admin")
    except HTTPException as error:
        status_code = error.status_code

    async with isolated_database() as session:
        user = await session.scalar(select(User).where(User.username == "grouped"))
        assert user.is_disabled_by_limiter is True
        assert user.original_groups == [7, 11]
    assert status_code == 502
