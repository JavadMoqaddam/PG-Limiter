"""Punishment application must be idempotent under DB failure and concurrency.

A panel side effect (disable/revoke) must be applied at most once per incident, and
a failed violation record must be reconciled on the next attempt rather than causing
the panel action to be repeated.
"""

import asyncio
from unittest.mock import AsyncMock

import pytest

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


async def test_reconciliation_claim_blocks_overlapping_panel_action(tmp_path, monkeypatch):
    system = PunishmentSystem(filename=str(tmp_path / "violations.json"))
    system.mark_unrecorded("victim", 1, 15)
    monkeypatch.setattr(ps_mod, "get_punishment_system", lambda: system)
    monkeypatch.setattr(users_mod, "check_user_exists", AsyncMock(return_value=True))
    apply = AsyncMock(return_value={"action": "disabled"})
    monkeypatch.setattr(users_mod, "_apply_punishment_locked", apply)
    recording = asyncio.Event()
    release = asyncio.Event()

    async def record(username, step_index, duration):
        recording.set()
        await release.wait()

    monkeypatch.setattr(ps_mod, "record_user_violation", record)
    user = UserType(name="victim", ip=[])
    first = asyncio.create_task(users_mod.disable_user_with_punishment(_panel(), user))
    try:
        await asyncio.wait_for(recording.wait(), timeout=1)
        duplicate = await users_mod.disable_user_with_punishment(_panel(), user)
        assert duplicate["action"] == "deduplicated"
        apply.assert_not_awaited()
    finally:
        release.set()
        result = await first
    assert result["action"] == "reconciled"
    assert not system.has_unrecorded_incident("victim")
    assert system.begin_incident("victim")
    system.end_incident("victim")


@pytest.mark.parametrize("failure", [asyncio.CancelledError, RuntimeError, ViolationRecordError])
async def test_interrupted_reconciliation_preserves_pending_record(tmp_path, monkeypatch, failure):
    system = PunishmentSystem(filename=str(tmp_path / "violations.json"))
    system.mark_unrecorded("victim", 2, 60)
    monkeypatch.setattr(ps_mod, "get_punishment_system", lambda: system)
    monkeypatch.setattr(users_mod, "check_user_exists", AsyncMock(return_value=True))
    apply = AsyncMock(return_value={"action": "disabled"})
    monkeypatch.setattr(users_mod, "_apply_punishment_locked", apply)
    monkeypatch.setattr(ps_mod, "record_user_violation", AsyncMock(side_effect=failure("interrupted")))
    user = UserType(name="victim", ip=[])

    if failure is ViolationRecordError:
        result = await users_mod.disable_user_with_punishment(_panel(), user)
        assert result["action"] == "applied_unrecorded"
    else:
        with pytest.raises(failure):
            await users_mod.disable_user_with_punishment(_panel(), user)

    assert system.has_unrecorded_incident("victim")
    assert system.begin_incident("victim")
    system.end_incident("victim")
    record = AsyncMock()
    monkeypatch.setattr(ps_mod, "record_user_violation", record)
    result = await users_mod.disable_user_with_punishment(_panel(), user)
    assert result["action"] == "reconciled"
    record.assert_awaited_once_with("victim", 2, 60)
    apply.assert_not_awaited()
