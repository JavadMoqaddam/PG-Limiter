#!/usr/bin/env python3
"""
Pytest configuration and fixtures
Shared test fixtures and configuration for all tests.
"""

import os
import sys
import pytest
import asyncio
import json
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))


# There is deliberately no `event_loop` fixture here. Overriding it was the old way
# to widen the loop's scope; pytest-asyncio deprecated that in 0.23 and removed the
# fixture in 1.0, and a session-scoped override alongside pytest.ini's
# asyncio_default_fixture_loop_scope = function is exactly the combination that
# either errors at collection or silently leaves async tests on a closed loop.
# pytest.ini owns the loop scope now.


@pytest.fixture
def sample_config():
    """Provide sample configuration for tests"""
    return {
        "panel_username": "test_admin",
        "panel_password": "test_password",
        "panel_domain": "test.panel.com",
        "telegram_bot_token": "123456789:ABCdefGHIjklMNOpqrsTUVwxyz",
        "telegram_chat_id": "-1001234567890",
        "ip_limit": 3,
        "check_interval": 60,
        "punishment": {
            "enabled": True,
            "window_hours": 72,
            "steps": [
                {"type": "warning", "duration": 0},
                {"type": "disable", "duration": 15},
                {"type": "disable", "duration": 60},
                {"type": "disable", "duration": 0},
            ],
        },
    }


@pytest.fixture
def sample_users():
    """Provide sample user data for tests"""
    return [
        {"name": "user1", "ip": ["1.1.1.1", "2.2.2.2"]},
        {"name": "user2", "ip": ["3.3.3.3"]},
        {"name": "user3", "ip": ["4.4.4.4", "5.5.5.5", "6.6.6.6"]},
    ]


@pytest.fixture
def sample_log_lines():
    """Provide sample log lines for parsing tests"""
    return [
        "2023/07/07 03:09:00 151.232.190.86:57288 accepted tcp:gateway.instagram.com:443 [REALITY TCP 4 -> IPv4] email: 22.User_22",
        "2023/07/07 03:08:59 [2a01:5ec0:5011:9962:d8ed:c723:c32:ac2a]:62316 accepted tcp:2.56.98.255:8000 [GRPC 6 >> DIRECT] email: 6.TEST_user",
        "2023/07/07 03:09:18 [2a01:5ec0:5013:4ca8:1:0:d554:7f0e]:45572 accepted udp:1.1.1.1:53 [REALITY TCP 6 >> DIRECT] email: 2.another_user",
    ]


@pytest.fixture
def mock_panel_type():
    """Provide a mock PanelType for tests"""
    from utils.types import PanelType

    return PanelType(panel_domain="test.panel.com", panel_api_key="pg_key_test")


@pytest.fixture
def mock_user_type():
    """Provide a mock UserType for tests"""
    from utils.types import UserType

    return UserType(name="test_user", ip=["1.1.1.1", "2.2.2.2"])


@pytest.fixture
def temp_config_dir(tmp_path):
    """Create a temporary config directory"""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    return config_dir


@pytest.fixture
def mock_telegram_bot():
    """Provide a mock Telegram bot"""
    bot = MagicMock()
    bot.send_message = AsyncMock(return_value=MagicMock(message_id=12345))
    bot.edit_message_text = AsyncMock()
    bot.answer_callback_query = AsyncMock()
    return bot


@pytest.fixture
def mock_httpx_client():
    """Provide a mock httpx client"""
    client = MagicMock()
    client.get = AsyncMock(
        return_value=MagicMock(status_code=200, json=MagicMock(return_value={"country": "US", "isp": "Test ISP"}))
    )
    client.post = AsyncMock(
        return_value=MagicMock(status_code=200, json=MagicMock(return_value={"access_token": "test_token"}))
    )
    return client


@pytest.fixture(autouse=True)
def reset_globals(monkeypatch):
    """Reset global state before each test"""
    from utils import panel_workload, user_sync

    monkeypatch.setattr(panel_workload, "_workload_lock", asyncio.Lock())
    monkeypatch.setattr(panel_workload, "_active_workload", "")
    monkeypatch.setattr(user_sync, "_sync_lock", asyncio.Lock())
    monkeypatch.setattr(user_sync, "_sync_schedule_ready_at", None)
    monkeypatch.setattr(user_sync, "_last_sync_attempt_finished_at", None)

    # Import and reset globals that might persist between tests
    try:
        import utils.handel_dis_users as dis_mod

        # The registry itself lives in SQLite; the only process state left is the
        # one-shot flag for importing the retired JSON file.
        dis_mod._migrated = False
    except ImportError:
        pass

    try:
        from utils.check_usage import ACTIVE_USERS

        ACTIVE_USERS.clear()
        from utils.shared_state import begin_api_collection

        begin_api_collection()
    except ImportError:
        pass

    from utils.log_observations import LOG_OBSERVATIONS, LogObservations

    monkeypatch.setattr(LOG_OBSERVATIONS, "__dict__", LogObservations().__dict__)
    yield  # Run test

    # Cleanup after test if needed


# Markers for different test categories
def pytest_configure(config):
    """Configure custom markers"""
    config.addinivalue_line("markers", "slow: marks tests as slow (deselect with '-m \"not slow\"')")
    config.addinivalue_line("markers", "integration: marks tests as integration tests")
    config.addinivalue_line("markers", "unit: marks tests as unit tests")


# Skip tests that require external services
def pytest_collection_modifyitems(config, items):
    """Modify test collection"""
    if not config.getoption("--run-integration", default=False):
        skip_integration = pytest.mark.skip(reason="need --run-integration option to run")
        for item in items:
            if "integration" in item.keywords:
                item.add_marker(skip_integration)


def pytest_addoption(parser):
    """Add custom command line options"""
    parser.addoption("--run-integration", action="store_true", default=False, help="run integration tests")


@pytest.fixture
def operational_topics(monkeypatch, tmp_path):
    """Use a real, isolated Topics destination for tests exercising monitoring."""
    import telegram_bot.topics as topics

    monkeypatch.setattr(topics, "TOPICS_FILE", str(tmp_path / "topics.json"))
    monkeypatch.setattr(topics, "MESSAGE_CACHE_FILE", str(tmp_path / "topic-cache.json"))
    manager = topics.TopicsManager()
    manager._enabled = True
    manager._group_id = -1001234567890
    monkeypatch.setattr(topics, "_topics_manager", manager)
    import utils.monitoring_gate as gate

    monkeypatch.setattr(gate, "_synchronized_admission", None)
    monkeypatch.setattr(gate, "_sync_lock", asyncio.Lock())
    monkeypatch.setattr(gate, "_warning_attempt", None)
    monkeypatch.setattr(gate, "_last_completed_scan", None)
    monkeypatch.setattr(gate, "_last_cycle_outcome", None)
    import utils.shared_state as state
    import utils.parse_logs as parser

    active_lock = asyncio.Lock()
    monkeypatch.setattr(state, "ACTIVE_USERS_LOCK", active_lock)
    monkeypatch.setattr(parser, "ACTIVE_USERS_LOCK", active_lock)
    import utils.get_logs as logs

    monkeypatch.setattr(logs, "TASKS", [])
    monkeypatch.setattr(logs, "task_node_mapping", {})
    monkeypatch.setattr(logs, "task_monitoring_admissions", {})
    monkeypatch.setattr(logs, "_pending_status_update_task", None)
    monkeypatch.setattr(logs, "_status_lock", asyncio.Lock())
    yield manager


@pytest.fixture
def completed_logs_evidence(operational_topics):
    """Build current completed evidence for tests focused on policy, not collection."""
    import time
    from utils.log_observations import LOG_OBSERVATIONS
    from utils.shared_state import note_node_event
    from utils.types import NodeType

    def build(config=None):
        config = config or {"check_interval": 60}
        LOG_OBSERVATIONS.configure(config)
        # Policy fixtures supply a known monitored interval and a known fleet.
        LOG_OBSERVATIONS.floor = time.time() - float(config.get("check_interval", 60)) - 1
        LOG_OBSERVATIONS.zones["1"] = LOG_OBSERVATIONS._zone("UTC")
        LOG_OBSERVATIONS.register_fleet([NodeType(1, "node", "192.0.2.1", "connected")])
        note_node_event(1)
        LOG_OBSERVATIONS.fleet_floor = LOG_OBSERVATIONS.floor
        LOG_OBSERVATIONS.transport_floor = LOG_OBSERVATIONS.floor
        for node_id in LOG_OBSERVATIONS.required_nodes or ():
            LOG_OBSERVATIONS.node_states[node_id].floor = LOG_OBSERVATIONS.floor
        return LOG_OBSERVATIONS.evidence()

    return build


@pytest.fixture(autouse=True)
def isolate_punishment_fact_outbox(tmp_path, monkeypatch):
    """Keep actual-effect reporting state local to each test."""
    import utils.punishment_facts as facts

    monkeypatch.setattr(facts, "OUTBOX_FILE", str(tmp_path / "punishment_fact_outbox.json"))
    monkeypatch.setattr(facts, "_journal", None)
