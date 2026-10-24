"""Inbound settings must persist real configuration and reject unsupported sources."""

from contextlib import asynccontextmanager

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from db.crud.config import ConfigCRUD
from db.models import Base
from telegram_bot.handlers import settings_device_count as device_settings
from telegram_bot.handlers import settings_ip_source as source_settings
from utils import read_config as config_module


class LocalQuery:
    """Capture Telegram output without sending messages outside the test."""

    def __init__(self):
        self.answers = []
        self.messages = []

    async def answer(self, text):
        self.answers.append(text)

    async def edit_message_text(self, **message):
        self.messages.append(message)


@pytest.fixture
async def settings_store(monkeypatch, tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'settings.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    @asynccontextmanager
    async def get_db():
        async with sessions() as session:
            yield session
            await session.commit()

    monkeypatch.setattr(config_module, "get_db", get_db)
    await config_module.invalidate_config_cache()
    yield get_db
    await config_module.invalidate_config_cache()
    await engine.dispose()


@pytest.mark.parametrize("stored", ["inbound", " INBOUND "])
async def test_persisted_inbound_mode_is_read_without_defaulting(settings_store, stored):
    async with settings_store() as session:
        await ConfigCRUD.set(session, "device_count_mode", stored)
    config = await config_module.read_config()
    assert config["device_count_mode"] == "inbound"


async def test_inbound_menu_choice_is_available_and_marked_when_active():
    keyboard = device_settings.create_device_count_keyboard("inbound")
    selections = [button for row in keyboard.inline_keyboard for button in row if "Inbound" in button.text]
    assert len(selections) == 1
    assert selections[0].text.startswith("✅")
    assert selections[0].callback_data == "device_count_set_inbound"


async def test_inbound_selection_persists_and_refreshes_menu(settings_store):
    query = LocalQuery()
    await device_settings._set_device_count_mode(query, None, "inbound", "per inbound")
    config = await config_module.read_config()
    assert config["device_count_mode"] == "inbound"
    assert any("Per Inbound" in message["text"] for message in query.messages)


async def test_inbound_selection_is_rejected_while_using_api(settings_store):
    await config_module.save_config_value("ip_source", "api")
    query = LocalQuery()
    await device_settings._set_device_count_mode(query, None, "inbound", "per inbound")
    config = await config_module.read_config()
    async with settings_store() as session:
        saved_mode = await ConfigCRUD.get(session, "device_count_mode")
    assert saved_mode is None
    assert config["ip_source"] == "api"
    assert any("logs" in answer.lower() for answer in query.answers)


async def test_api_selection_is_rejected_while_counting_inbounds(settings_store, monkeypatch):
    await config_module.save_config_value("device_count_mode", "inbound")

    async def preflight_user(*_args):
        return 7, ""

    async def permission(*_args):
        return True, ""

    monkeypatch.setattr(source_settings, "_find_preflight_user_id", preflight_user)
    monkeypatch.setattr("utils.panel_api.online_ips.check_online_stats_permission", permission)
    query = LocalQuery()
    await source_settings.handle_ip_source_set_api_callback(query, None)
    config = await config_module.read_config()
    assert config["ip_source"] == "logs"
    assert config["device_count_mode"] == "inbound"
    assert any("inbound" in answer.lower() for answer in query.answers)


async def test_failed_inbound_write_does_not_confirm_or_change_settings(settings_store, monkeypatch):
    async def failed_write(*_args):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(ConfigCRUD, "set", failed_write)
    query = LocalQuery()
    await device_settings._set_device_count_mode(query, None, "inbound", "per inbound")
    assert (await config_module.read_config())["device_count_mode"] == "device"
    assert query.answers and all("✅" not in answer for answer in query.answers)


async def test_inbound_callback_route_resolves_to_the_inbound_handler(settings_store):
    from telegram_bot.main import CALLBACK_ROUTES

    route = CALLBACK_ROUTES.get("device_count_set_inbound")
    assert route is not None
    await route(None, LocalQuery(), None)
    assert (await config_module.read_config())["device_count_mode"] == "inbound"
