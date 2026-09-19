"""Punishment application must be idempotent under DB failure and concurrency.

A panel side effect (disable/revoke) must be applied at most once per incident, and
a failed violation record must be reconciled on the next attempt rather than causing
the panel action to be repeated.
"""

import asyncio
from unittest.mock import AsyncMock

import utils.panel_api.users as users_mod
import utils.punishment_system as ps_mod
from utils.punishment_system import PunishmentStep, PunishmentSystem, ViolationRecordError
from utils.types import PanelType, UserType

_CONFIG = {"punishment": {"enabled": True, "steps": [{"type": "disable", "duration": 15}]}}


def _panel():
    return PanelType(panel_username="u", panel_password="p", panel_domain="d")


async def test_db_failure_reconciles_without_reapplying(tmp_path, monkeypatch):
    system = PunishmentSystem(filename=str(tmp_path / "violations.json"))
    monkeypatch.setattr(ps_mod, "get_punishment_system", lambda: system)
    monkeypatch.setattr(users_mod, "check_user_exists", AsyncMock(return_value=True))
    monkeypatch.setattr(users_mod, "read_config", AsyncMock(return_value=_CONFIG))
    monkeypatch.setattr(
        ps_mod, "get_punishment_for_user", AsyncMock(return_value=(PunishmentStep("disable", 15), 1, 1))
    )

    disable_calls = []

    async def fake_disable(panel_data, username, *args, **kwargs):
        disable_calls.append(username.name)

    monkeypatch.setattr(users_mod, "disable_user", fake_disable)

    async def failing_record(username, step_index, duration):
        raise ViolationRecordError("db down")

    monkeypatch.setattr(ps_mod, "record_user_violation", failing_record)

    pd, user = _panel(), UserType(name="victim", ip=[])

    first = await users_mod.disable_user_with_punishment(pd, user)
    assert first["action"] == "applied_unrecorded"
    assert disable_calls == ["victim"]
    assert system.has_unrecorded_incident("victim")

    recorded = []

    async def ok_record(username, step_index, duration):
        recorded.append((username, step_index, duration))

    monkeypatch.setattr(ps_mod, "record_user_violation", ok_record)

    second = await users_mod.disable_user_with_punishment(pd, user)
    assert second["action"] == "reconciled"
    # The panel action must NOT have been applied a second time.
    assert disable_calls == ["victim"]
    assert recorded == [("victim", 1, 15)]
    assert not system.has_unrecorded_incident("victim")


async def test_concurrent_incident_applies_once(tmp_path, monkeypatch):
    system = PunishmentSystem(filename=str(tmp_path / "violations.json"))
    monkeypatch.setattr(ps_mod, "get_punishment_system", lambda: system)
    monkeypatch.setattr(users_mod, "check_user_exists", AsyncMock(return_value=True))
    monkeypatch.setattr(users_mod, "read_config", AsyncMock(return_value=_CONFIG))
    monkeypatch.setattr(
        ps_mod, "get_punishment_for_user", AsyncMock(return_value=(PunishmentStep("disable", 15), 1, 1))
    )

    disable_calls = []

    async def fake_disable(panel_data, username, *args, **kwargs):
        await asyncio.sleep(0.02)  # hold the incident so the duplicate overlaps
        disable_calls.append(username.name)

    monkeypatch.setattr(users_mod, "disable_user", fake_disable)
    monkeypatch.setattr(ps_mod, "record_user_violation", AsyncMock())

    pd, user = _panel(), UserType(name="dup", ip=[])

    first, second = await asyncio.gather(
        users_mod.disable_user_with_punishment(pd, user),
        users_mod.disable_user_with_punishment(pd, user),
    )

    assert disable_calls == ["dup"]  # applied exactly once
    assert "deduplicated" in {first["action"], second["action"]}
