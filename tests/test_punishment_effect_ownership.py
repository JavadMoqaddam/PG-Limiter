"""Only an actual mutation or a confirmed adapter effect owns later punishment."""

import asyncio
import copy
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select

from test_config_revisions import settings_db  # noqa: F401
from test_inbound_mode_enforcement import enforcement_cycle  # noqa: F401
from test_per_node_enforcement import node_cycle  # noqa: F401
from test_post_journal_enforcement_guard import PANEL, prepare_overflow, real_node_punishment  # noqa: F401


def real_transports(real_node_punishment, monkeypatch, client):
    """Keep the production revoke/reset/status and request-boundary guard in use."""
    import utils.panel_api.users as users
    import utils.panel_api.request_helper as requests

    disable = real_node_punishment[5]
    monkeypatch.setattr(users, "revoke_user_subscription", disable.real_revoke)
    monkeypatch.setattr(users, "reset_user_uuid", disable.real_reset)
    monkeypatch.setattr(users, "disable_user_by_status", disable.real_status)
    monkeypatch.setattr(requests, "get_panel_client", AsyncMock(return_value=client))
    monkeypatch.setattr(requests, "_get_token_for_request", AsyncMock(return_value="token"))
    monkeypatch.setattr(requests, "circuit_breaker", requests.PanelCircuitBreaker())
    return requests


@pytest.mark.parametrize("source", ["logs", "api"])
@pytest.mark.parametrize("expired", [False, True])
@pytest.mark.parametrize("refusal", ["token", "circuit", "adapter_false"])
async def test_revoke_without_any_started_effect_preserves_paused_warning(
    real_node_punishment, operational_topics, monkeypatch, source, expired, refusal
):
    import utils.check_usage as usage
    import utils.panel_api.users as users
    from db.models import PunishmentFact, ViolationHistory
    from utils.isp_detector import ISPDetector

    system, get_db, messages, config, journal, _disable, _time = real_node_punishment
    warning = await prepare_overflow(real_node_punishment, source, expired)
    client = MagicMock(
        post=AsyncMock(return_value=MagicMock(status_code=200)),
        put=AsyncMock(return_value=MagicMock(status_code=200)),
    )
    requests = real_transports(real_node_punishment, monkeypatch, client)
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def preparation(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            await release.wait()
            return None if refusal == "token" else (False if refusal == "adapter_false" else "token")
        return "token"

    if refusal == "adapter_false":
        monkeypatch.setattr(users, "revoke_user_subscription", preparation)
    else:
        monkeypatch.setattr(requests, "_get_token_for_request", preparation)
        if refusal == "circuit":
            monkeypatch.setattr(requests.circuit_breaker, "allow_request", MagicMock(side_effect=[False, True, True]))
    outcomes = []

    async def capture_punishment(panel, user):
        outcome = await users.disable_user_with_punishment(panel, user)
        outcomes.append(outcome)
        return outcome

    monkeypatch.setattr("utils.panel_api.disable_user_with_punishment", capture_punishment)
    isp = AsyncMock(return_value={})
    monkeypatch.setattr(ISPDetector, "get_multiple_isp_info", isp)
    caller = asyncio.create_task(usage.check_users_usage(PANEL, config_data=config))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        client.post.assert_not_called()
        client.put.assert_not_called()
        saved_warning = copy.deepcopy(
            {key: value for key, value in vars(warning).items() if key != "active_monitoring_task"}
        )
        saved_history = copy.deepcopy(system.warning_history)
        saved_messages = list(messages)
        isp.reset_mock()
        await operational_topics.set_enabled(False)
        release.set()
        await asyncio.wait_for(caller, 3)
        client.post.assert_not_called()
        client.put.assert_not_called()
        users.dis_users.disable.assert_not_called()
        isp.assert_not_called()
        assert system.warnings.get("alice") is warning
        assert {key: value for key, value in vars(warning).items() if key != "active_monitoring_task"} == saved_warning
        assert system.warning_history == saved_history
        assert messages == saved_messages
        assert len(outcomes) == 1 and outcomes[0]["action"] == "skipped" and outcomes[0]["protected"]
        async with get_db() as db:
            facts = list(await db.scalars(select(PunishmentFact)))
            assert len(facts) == 1 and facts[0].state == "completed" and facts[0].applied_at is None
            assert (facts[0].disable_succeeded, facts[0].revoke_succeeded, facts[0].uuid_reset_succeeded) == (
                False,
                False,
                False,
            )
            assert not list(await db.scalars(select(ViolationHistory)))
        assert await journal.ready_before(datetime(2026, 10, 24, tzinfo=timezone.utc).timestamp())
    finally:
        release.set()
        await asyncio.wait_for(caller, 3)


@pytest.mark.parametrize("source", ["logs", "api"])
@pytest.mark.parametrize("expired", [False, True])
@pytest.mark.parametrize("first_effect", ["http_success", "http_failure", "adapter_success", "uuid_adapter_success"])
async def test_started_or_confirmed_revoke_retains_remaining_effects_after_pause(
    real_node_punishment, operational_topics, monkeypatch, source, expired, first_effect
):
    import utils.check_usage as usage
    import utils.panel_api.users as users
    from db.models import PunishmentFact, ViolationHistory

    system, get_db, _messages, config, journal, _disable, original_time = real_node_punishment
    await prepare_overflow(real_node_punishment, source, expired)
    entered, release = asyncio.Event(), asyncio.Event()

    async def first_mutation(*args, **kwargs):
        entered.set()
        await release.wait()
        if first_effect in {"adapter_success", "uuid_adapter_success"}:
            return True
        return MagicMock(status_code=404 if first_effect == "http_failure" else 200)

    client = MagicMock(
        post=AsyncMock(side_effect=first_mutation), put=AsyncMock(return_value=MagicMock(status_code=200))
    )
    requests = real_transports(real_node_punishment, monkeypatch, client)
    if first_effect == "adapter_success":
        monkeypatch.setattr(users, "revoke_user_subscription", first_mutation)
    elif first_effect == "uuid_adapter_success":
        monkeypatch.setattr(requests, "_get_token_for_request", AsyncMock(side_effect=[None, "token"]))
        monkeypatch.setattr(users, "reset_user_uuid", first_mutation)
    caller = asyncio.create_task(usage.check_users_usage(PANEL, config_data=config))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        client.put.assert_not_called()
        await operational_topics.set_enabled(False)
        release.set()
        await asyncio.wait_for(caller, 3)
        assert client.post.call_count == (0 if first_effect in {"adapter_success", "uuid_adapter_success"} else 1)
        assert client.put.call_count == (1 if first_effect == "uuid_adapter_success" else 2)
        users.dis_users.disable.assert_awaited_once()
        assert "alice" not in system.warnings
        async with get_db() as db:
            facts = list(await db.scalars(select(PunishmentFact)))
            assert len(facts) == 1 and facts[0].state == "completed" and facts[0].applied_at == original_time
            assert (facts[0].disable_succeeded, facts[0].revoke_succeeded, facts[0].uuid_reset_succeeded) == (
                True,
                first_effect not in {"http_failure", "uuid_adapter_success"},
                True,
            )
            assert len(list(await db.scalars(select(ViolationHistory)))) == 1
        assert await journal.ready_before(datetime(2026, 10, 24, tzinfo=timezone.utc).timestamp())
    finally:
        release.set()
        await asyncio.wait_for(caller, 3)


@pytest.mark.parametrize("source", ["logs", "api"])
@pytest.mark.parametrize("expired", [False, True])
@pytest.mark.parametrize("refusal", ["token", "circuit"])
async def test_unstarted_failed_revoke_allows_current_later_punishment(
    real_node_punishment, monkeypatch, source, expired, refusal
):
    import utils.check_usage as usage
    import utils.panel_api.users as users
    from db.models import PunishmentFact, ViolationHistory

    system, get_db, _messages, config, _journal, _disable, _time = real_node_punishment
    await prepare_overflow(real_node_punishment, source, expired)
    client = MagicMock(post=AsyncMock(), put=AsyncMock(return_value=MagicMock(status_code=200)))
    requests = real_transports(real_node_punishment, monkeypatch, client)
    if refusal == "token":
        monkeypatch.setattr(requests, "_get_token_for_request", AsyncMock(side_effect=[None, "token", "token"]))
    else:
        monkeypatch.setattr(requests.circuit_breaker, "allow_request", MagicMock(side_effect=[False, True, True]))
    await usage.check_users_usage(PANEL, config_data=config)
    client.post.assert_not_called()
    assert client.put.call_count == 2
    users.dis_users.disable.assert_awaited_once()
    assert "alice" not in system.warnings
    async with get_db() as db:
        fact = await db.scalar(select(PunishmentFact))
        assert fact.state == "completed"
        assert (fact.disable_succeeded, fact.revoke_succeeded, fact.uuid_reset_succeeded) == (True, False, True)
        assert len(list(await db.scalars(select(ViolationHistory)))) == 1


async def test_direct_management_no_token_revoke_keeps_existing_fallback(
    real_node_punishment, operational_topics, monkeypatch
):
    import utils.panel_api.users as users
    from db.crud.users import UserCRUD
    from utils.types import UserType

    _system, get_db, _messages, _config, _journal, _disable, _time = real_node_punishment
    client = MagicMock(post=AsyncMock(), put=AsyncMock(return_value=MagicMock(status_code=200)))
    requests = real_transports(real_node_punishment, monkeypatch, client)
    monkeypatch.setattr(requests, "_get_token_for_request", AsyncMock(side_effect=[None, "token", "token"]))
    await operational_topics.set_enabled(False)
    async with get_db() as db:
        await UserCRUD.create_or_update(db, "alice", group_ids=[99])
    outcome = await users.disable_user_with_punishment(PANEL, UserType(name="alice", ip=[]))
    assert outcome["action"] == "revoked" and outcome["revoke_success"] is False
    client.post.assert_not_called()
    assert client.put.call_count == 2
    users.dis_users.disable.assert_awaited_once()
