"""Panel credentials come only from ENV and stay private on management surfaces."""

import pytest
from fastapi import HTTPException
from test_config_revisions import settings_db  # noqa: F401
from test_inbound_mode_settings import LocalQuery
from types import SimpleNamespace

import api_server
import cli.config as cli_config
import utils.read_config as config_module
from telegram_bot.handlers import settings_groups, settings_ip_source, settings_nodes


@pytest.fixture
def key_environment(settings_db, monkeypatch):
    monkeypatch.setenv("PANEL_DOMAIN", "https://panel.test")
    monkeypatch.setenv("PANEL_API_KEY", "private-panel-key")
    monkeypatch.setenv("BOT_TOKEN", "private-bot-token")
    monkeypatch.setenv("ADMIN_IDS", "7")
    monkeypatch.setenv("API_USERNAME", "management-admin")
    monkeypatch.setenv("API_PASSWORD", "private-management-password")
    monkeypatch.delenv("PANEL_USERNAME", raising=False)
    monkeypatch.delenv("PANEL_PASSWORD", raising=False)


def test_env_loads_only_panel_api_key(key_environment, monkeypatch):
    monkeypatch.setenv("PANEL_USERNAME", "retired-admin")
    monkeypatch.setenv("PANEL_PASSWORD", "retired-password")

    config = config_module.load_env_config()

    assert config["panel"] == {"domain": "https://panel.test", "api_key": "private-panel-key"}
    assert config["api"]["password"] == "private-management-password"
    assert config_module.get_config_value(config, "PANEL_API_KEY") == "private-panel-key"
    assert config_module.get_config_value(config, "PANEL_USERNAME") is None
    assert config_module.get_config_value(config, "PANEL_PASSWORD") is None


async def test_required_config_accepts_panel_key_without_legacy_credentials(key_environment):
    config = await config_module.read_config(check_required_elements=True)

    assert config["panel"]["api_key"] == "private-panel-key"
    assert config["api"]["password"] == "private-management-password"


@pytest.mark.parametrize("key", [None, "", "   "])
async def test_required_config_rejects_missing_key_even_with_legacy_password(key_environment, monkeypatch, key):
    if key is None:
        monkeypatch.delenv("PANEL_API_KEY")
    else:
        monkeypatch.setenv("PANEL_API_KEY", key)
    monkeypatch.setenv("PANEL_PASSWORD", "retired-password")

    with pytest.raises(ValueError, match="PANEL_API_KEY") as raised:
        await config_module.read_config(check_required_elements=True)

    assert "retired-password" not in str(raised.value)


async def test_api_panel_connection_uses_key_only_config(key_environment):
    panel = await api_server.get_panel_data()

    assert panel.panel_domain == "https://panel.test"
    assert panel.panel_api_key == "private-panel-key"
    assert "private-panel-key" not in repr(panel)


async def test_api_panel_connection_rejects_missing_key(key_environment, monkeypatch):
    monkeypatch.delenv("PANEL_API_KEY")

    with pytest.raises(HTTPException) as raised:
        await api_server.get_panel_data()

    assert raised.value.status_code == 400
    assert "PANEL_API_KEY" in raised.value.detail


async def test_api_config_masks_key_without_mutating_runtime_config(key_environment):
    data = (await api_server.get_config(username="management-admin"))["data"]

    assert data["panel"]["api_key"] == "***"
    assert data["telegram"]["bot_token"] == "***"
    assert data["api"]["password"] == "***"
    assert "private-panel-key" not in repr(data)
    assert (await config_module.read_config())["panel"]["api_key"] == "private-panel-key"


def test_cli_shows_key_presence_without_secret_or_legacy_credentials(key_environment, monkeypatch, capsys):
    config = config_module.load_env_config()
    monkeypatch.setattr(cli_config, "load_config", lambda: config)

    cli_config.show_config()

    output = capsys.readouterr().out
    assert "API key: Set" in output
    assert "Username:" not in output
    assert "Password:" not in output
    assert "private-panel-key" not in output
    assert "private-bot-token" not in output
    assert "private-management-password" not in output


async def test_ip_source_panel_connection_uses_key_only_config(key_environment):
    panel = settings_ip_source._build_panel_data(await config_module.read_config())

    assert panel.panel_api_key == "private-panel-key"


async def test_ip_source_switch_refuses_missing_key_without_raising(key_environment, monkeypatch):
    monkeypatch.delenv("PANEL_API_KEY")
    query = LocalQuery()

    await settings_ip_source.handle_ip_source_set_api_callback(query, SimpleNamespace(user_data={}))

    assert (await config_module.read_config())["ip_source"] == "logs"
    assert "PANEL_API_KEY" in query.messages[-1]["text"]
    assert not query.answers


async def test_nodes_settings_accept_key_only_configuration(key_environment, monkeypatch):
    async def nodes(panel, *, enabled_only):
        assert panel.panel_api_key == "private-panel-key"
        assert enabled_only is False
        return [{"id": 1, "name": "node-one"}]

    monkeypatch.setattr("utils.panel_api.nodes.get_nodes", nodes)

    assert await settings_nodes._get_nodes_list() == ([{"id": 1, "name": "node-one"}], None)


async def test_groups_settings_accept_key_only_configuration(key_environment, monkeypatch):
    async def groups(panel):
        assert panel.panel_api_key == "private-panel-key"
        return [{"id": 1, "name": "group-one"}]

    monkeypatch.setattr("utils.user_group_filter.get_all_groups", groups)

    groups_result, config = await settings_groups._get_groups_from_panel()

    assert groups_result == [{"id": 1, "name": "group-one"}]
    assert config["panel"]["api_key"] == "private-panel-key"


async def test_single_user_sync_uses_key_only_configuration(key_environment, monkeypatch):
    from utils.user_sync import fetch_and_sync_single_user

    panels = []

    async def absent_user(panel, username):
        panels.append(panel)
        assert username == "missing-user"
        return None

    monkeypatch.setattr("utils.panel_api.get_user_details", absent_user)

    assert await fetch_and_sync_single_user("missing-user") is None
    assert len(panels) == 1
    assert panels[0].panel_api_key == "private-panel-key"


async def test_api_cleanup_uses_key_only_configuration(key_environment, monkeypatch):
    async def cleanup(panel):
        assert panel.panel_api_key == "private-panel-key"
        return {
            "special_limits_removed": [],
            "except_users_removed": [],
            "disabled_users_removed": [],
            "user_groups_backup_removed": [],
        }

    monkeypatch.setattr("utils.panel_api.cleanup_deleted_users", cleanup)

    result = await api_server.cleanup_deleted_users(username="management-admin")

    assert result.total_removed == 0
