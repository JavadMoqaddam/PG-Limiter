"""Per Node is persisted, routed and displayed through existing settings."""

import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from db.crud.config import ConfigCRUD
from telegram_bot.handlers import settings_device_count as device_settings
from telegram_bot.handlers import settings_ip_source as source_settings
from test_inbound_mode_settings import LocalQuery, settings_store  # noqa: F401
from test_status_reports import facts, plain, reports  # noqa: F401
from utils import read_config as config_module


@pytest.mark.parametrize("stored", ["node", " NODE "])
async def test_node_mode_round_trips_canonical_and_legacy_settings(settings_store, stored):
    async with settings_store() as session:
        await ConfigCRUD.set(session, "device_count_mode", stored)
    config = await config_module.read_config()
    assert config["device_count_mode"] == "node"
    assert config_module.get_config_value(config, "DEVICE_COUNT_MODE") == "node"


def test_node_menu_choice_is_marked_when_active():
    buttons = [b for row in device_settings.create_device_count_keyboard("node").inline_keyboard for b in row]
    selections = [b for b in buttons if "Per Node" in b.text]
    assert len(selections) == 1
    assert selections[0].text.startswith("✅")
    assert selections[0].callback_data == "device_count_set_node"


async def test_node_callback_route_persists_and_displays_mode_in_api(settings_store):
    from telegram_bot.main import CALLBACK_ROUTES

    assert await config_module.save_config_value("ip_source", "api")
    route = CALLBACK_ROUTES.get("device_count_set_node")
    assert route is not None
    query = LocalQuery()
    await route(None, query, None)
    config = await config_module.read_config()
    assert config["ip_source"] == "api"
    assert config["device_count_mode"] == "node"
    assert any("Active:</b> 🖥️ <b>Per Node</b>" in m["text"] for m in query.messages)


async def test_failed_node_write_does_not_confirm_or_change_mode(settings_store, monkeypatch):
    async def failed_write(*_args):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(ConfigCRUD, "set", failed_write)
    query = LocalQuery()
    await device_settings._set_device_count_mode(query, None, "node", "per node")
    assert (await config_module.read_config())["device_count_mode"] == "device"
    assert query.answers and all("✅" not in answer for answer in query.answers)


async def test_api_source_selection_remains_available_while_counting_nodes(settings_store, monkeypatch):
    monkeypatch.setenv("PANEL_DOMAIN", "https://panel.invalid")
    monkeypatch.setenv("PANEL_API_KEY", "synthetic-panel-key")
    assert await config_module.save_config_value("device_count_mode", "node")
    monkeypatch.setattr(source_settings, "_find_preflight_user_id", AsyncMock(return_value=(7, "")))
    permission = AsyncMock(return_value=(True, ""))
    monkeypatch.setattr("utils.panel_api.online_ips.check_online_stats_permission", permission)
    await source_settings.handle_ip_source_set_api_callback(LocalQuery(), SimpleNamespace(user_data={}))
    config = await config_module.read_config()
    permission.assert_awaited_once()
    assert permission.await_args.args[0].panel_api_key == "synthetic-panel-key"
    assert config["device_count_mode"] == "node"
    assert config["ip_source"] == "api"


@pytest.mark.parametrize("mode, label", [("node", "Per Node"), ("inbound", "Per Inbound")])
def test_status_labels_identity_modes(reports, mode, label):
    text = plain(reports.render_status(facts(reports, config={"ip_source": "api", "device_count_mode": mode})))
    assert f"Counting mode: {label}" in text
