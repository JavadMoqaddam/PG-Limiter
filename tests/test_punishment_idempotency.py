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


@pytest.fixture
async def persistent_system(tmp_path, monkeypatch):
    """Exercise authoritative violation writes against an isolated SQLite database."""
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    import db.database as database
    from db.models import Base

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'violations.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    monkeypatch.setattr(
        database,
        "AsyncSessionLocal",
        async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False),
    )
    monkeypatch.setattr(database, "DB_AVAILABLE", True)
    system = PunishmentSystem(filename=str(tmp_path / "violations.json"))
    monkeypatch.setattr(ps_mod, "get_punishment_system", lambda: system)
    monkeypatch.setattr(users_mod, "check_user_exists", AsyncMock(return_value=True))
    try:
        yield system
    finally:
        await engine.dispose()


def _track_panel_actions(monkeypatch):
    """Replace external panel requests while retaining their observable action order."""
    actions = []

    async def disable(panel_data, user, *args, **kwargs):
        actions.append("disable")

    async def revoke(panel_data, username):
        actions.append("revoke")
        return True

    async def reset_uuid(panel_data, username):
        actions.append("reset_uuid")
        return True

    monkeypatch.setattr(users_mod, "disable_user", disable)
    monkeypatch.setattr(users_mod, "revoke_user_subscription", revoke)
    monkeypatch.setattr(users_mod, "reset_user_uuid", reset_uuid)
    return actions


@pytest.mark.parametrize(
    ("step_type", "duration", "expected_actions"),
    [("disable", 15, ["disable"]), ("revoke", 0, ["revoke", "reset_uuid", "disable"])],
)
async def test_cancelled_initial_record_retries_without_reapplying(
    persistent_system, monkeypatch, step_type, duration, expected_actions
):
    from db.crud.violations import ViolationHistoryCRUD

    system = persistent_system
    config = {"punishment": {"enabled": True, "steps": [{"type": step_type, "duration": duration}]}}
    monkeypatch.setattr(users_mod, "read_config", AsyncMock(return_value=config))
    actions = _track_panel_actions(monkeypatch)
    recording = asyncio.Event()
    release = asyncio.Event()
    original_add = ViolationHistoryCRUD.add

    async def paused_add(*args, **kwargs):
        recording.set()
        await release.wait()
        return await original_add(*args, **kwargs)

    monkeypatch.setattr(ViolationHistoryCRUD, "add", paused_add)
    user = UserType(name="victim", ip=[])
    first = asyncio.create_task(users_mod.disable_user_with_punishment(_panel(), user))
    try:
        await asyncio.wait_for(recording.wait(), timeout=1)
    finally:
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
    assert await system.get_violation_count_async("victim") == 0

    monkeypatch.setattr(ViolationHistoryCRUD, "add", original_add)
    result = await users_mod.disable_user_with_punishment(_panel(), user)

    assert actions == expected_actions
    assert result["action"] == "reconciled"
    assert await system.get_violation_count_async("victim") == 1
    assert system.get_violation_count("victim") == 1
    assert not system.has_unrecorded_incident("victim")


@pytest.mark.parametrize(
    ("incident", "expected_actions"),
    [("disable", ["disable"]), ("revoke", ["revoke", "reset_uuid", "disable"]), ("reconciliation", [])],
)
async def test_cancelled_mirror_retries_without_duplicate_persistence(
    persistent_system, monkeypatch, incident, expected_actions
):
    system = persistent_system
    config = {"punishment": {"enabled": True, "steps": [{"type": "disable", "duration": 15}]}}
    if incident == "revoke":
        config["punishment"]["steps"] = [{"type": "revoke", "duration": 0}]
    elif incident == "reconciliation":
        system.mark_unrecorded("victim", 2, 60)
    monkeypatch.setattr(users_mod, "read_config", AsyncMock(return_value=config))
    actions = _track_panel_actions(monkeypatch)
    mirroring = asyncio.Event()
    release = asyncio.Event()
    original_save = system.save_violations

    async def paused_save():
        mirroring.set()
        await release.wait()
        await original_save()

    monkeypatch.setattr(system, "save_violations", paused_save)
    user = UserType(name="victim", ip=[])
    first = asyncio.create_task(users_mod.disable_user_with_punishment(_panel(), user))
    try:
        await asyncio.wait_for(mirroring.wait(), timeout=1)
    finally:
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
    assert await system.get_violation_count_async("victim") == 1

    monkeypatch.setattr(system, "save_violations", original_save)
    result = await users_mod.disable_user_with_punishment(_panel(), user)

    assert await system.get_violation_count_async("victim") == 1
    assert system.get_violation_count("victim") == 1
    assert actions == expected_actions
    assert result["action"] == "reconciled"
    assert not system.has_unrecorded_incident("victim")

    # Consuming reconciliation must still allow a later independent violation.
    await users_mod.disable_user_with_punishment(_panel(), user)
    assert await system.get_violation_count_async("victim") == 2
    assert system.get_violation_count("victim") == 2


@pytest.mark.parametrize("cancellation_count", [1, 2])
async def test_cancelled_commit_finishes_record_once(persistent_system, monkeypatch, cancellation_count):
    from db.crud.violations import ViolationHistoryCRUD

    system = persistent_system
    system.mark_unrecorded("victim", 2, 60)
    actions = _track_panel_actions(monkeypatch)
    committed = asyncio.Event()
    release = asyncio.Event()
    original_add = ViolationHistoryCRUD.add

    async def pause_commit_completion(*args, **kwargs):
        record = await original_add(*args, **kwargs)
        db = kwargs["db"]
        original_commit = db.commit

        async def paused_commit():
            await original_commit()
            committed.set()
            await release.wait()

        monkeypatch.setattr(db, "commit", paused_commit)
        return record

    monkeypatch.setattr(ViolationHistoryCRUD, "add", pause_commit_completion)
    user = UserType(name="victim", ip=[])
    first = asyncio.create_task(users_mod.disable_user_with_punishment(_panel(), user))
    try:
        await asyncio.wait_for(committed.wait(), timeout=1)
        for _ in range(cancellation_count):
            first.cancel()
            await asyncio.sleep(0)
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await first
    assert await system.get_violation_count_async("victim") == 1

    monkeypatch.setattr(ViolationHistoryCRUD, "add", original_add)
    result = await users_mod.disable_user_with_punishment(_panel(), user)

    assert await system.get_violation_count_async("victim") == 1
    assert system.get_violation_count("victim") == 1
    assert actions == []
    assert result["action"] == "reconciled"
    assert not system.has_unrecorded_incident("victim")


@pytest.mark.parametrize("cancellation_count", [0, 2])
async def test_commit_listener_failure_keeps_confirmed_record(persistent_system, monkeypatch, cancellation_count):
    from sqlalchemy import event

    from db.crud.violations import ViolationHistoryCRUD

    system = persistent_system
    monkeypatch.setattr(users_mod, "read_config", AsyncMock(return_value=_CONFIG))
    actions = _track_panel_actions(monkeypatch)
    committing = asyncio.Event()
    release = asyncio.Event()
    original_add = ViolationHistoryCRUD.add

    async def add_with_listener_failure(*args, **kwargs):
        record = await original_add(*args, **kwargs)
        db = kwargs["db"]

        def after_commit(session):
            raise RuntimeError("after_commit callback interrupted")

        event.listen(db.sync_session, "after_commit", after_commit, once=True)
        if cancellation_count:
            original_commit = db.commit

            async def paused_commit():
                committing.set()
                await release.wait()
                await original_commit()

            monkeypatch.setattr(db, "commit", paused_commit)
        return record

    monkeypatch.setattr(ViolationHistoryCRUD, "add", add_with_listener_failure)
    user = UserType(name="victim", ip=[])
    first = asyncio.create_task(users_mod.disable_user_with_punishment(_panel(), user))
    try:
        if cancellation_count:
            await asyncio.wait_for(committing.wait(), timeout=1)
            for _ in range(cancellation_count):
                first.cancel()
                await asyncio.sleep(0)
    finally:
        release.set()
    if cancellation_count:
        with pytest.raises(asyncio.CancelledError):
            await first
    else:
        assert (await first)["action"] == "applied_unrecorded"
    assert await system.get_violation_count_async("victim") == 1
    assert system.get_violation_count("victim") == 1

    monkeypatch.setattr(ViolationHistoryCRUD, "add", original_add)
    result = await users_mod.disable_user_with_punishment(_panel(), user)

    assert await system.get_violation_count_async("victim") == 1
    assert system.get_violation_count("victim") == 1
    assert actions == ["disable"]
    assert result["action"] == "reconciled"
    assert not system.has_unrecorded_incident("victim")


async def test_unavailable_commit_confirmation_is_checked_before_retry_insert(persistent_system, monkeypatch):
    from sqlalchemy import event

    from db.crud.violations import ViolationHistoryCRUD

    system = persistent_system
    monkeypatch.setattr(users_mod, "read_config", AsyncMock(return_value=_CONFIG))
    actions = _track_panel_actions(monkeypatch)
    original_add = ViolationHistoryCRUD.add
    original_is_persisted = ViolationHistoryCRUD.is_persisted
    unavailable = True

    async def add_with_listener_failure(*args, **kwargs):
        record = await original_add(*args, **kwargs)

        def after_commit(session):
            raise RuntimeError("after_commit callback interrupted")

        event.listen(kwargs["db"].sync_session, "after_commit", after_commit, once=True)
        return record

    async def unavailable_once(*args, **kwargs):
        nonlocal unavailable
        if unavailable:
            unavailable = False
            raise RuntimeError("confirmation query unavailable")
        return await original_is_persisted(*args, **kwargs)

    monkeypatch.setattr(ViolationHistoryCRUD, "add", add_with_listener_failure)
    monkeypatch.setattr(ViolationHistoryCRUD, "is_persisted", unavailable_once)
    user = UserType(name="victim", ip=[])
    first = await users_mod.disable_user_with_punishment(_panel(), user)
    assert first["action"] == "applied_unrecorded"
    assert await system.get_violation_count_async("victim") == 1
    # An unavailable confirmation must not claim that the record is known to exist.
    assert system.get_violation_count("victim") == 0

    monkeypatch.setattr(ViolationHistoryCRUD, "add", original_add)
    retry = await users_mod.disable_user_with_punishment(_panel(), user)

    assert await system.get_violation_count_async("victim") == 1
    assert system.get_violation_count("victim") == 1
    assert actions == ["disable"]
    assert retry["action"] == "reconciled"
    assert not system.has_unrecorded_incident("victim")
