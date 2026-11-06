"""Terminal customer delivery has a separate, durable forum report owner."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from test_mirza_persistence import mirza_database  # noqa: F401
from test_outbound_ownership import outbound  # noqa: F401
from telegram_bot.dispatcher import DeliveryResult


async def event(sessions, state="delivered", incident="incident", **changes):
    from db.models import MirzaNotification

    values = dict(
        incident_id=incident,
        username="config<&",
        applied_at=100,
        context={"step_index": 4, "candidates": []},
        state=state,
        bot_name="Sales<&",
        recipient="5123456789",
        attempts=1,
    )
    values.update(changes)
    async with sessions() as db:
        db.add(MirzaNotification(**values))
        await db.commit()


async def saved(sessions):
    from db.models import MirzaNotification

    async with sessions() as db:
        return (await db.scalars(select(MirzaNotification))).one()


def reporter(monkeypatch, outcome="delivered"):
    import utils.mirza_reports as reports

    manager = SimpleNamespace(enabled=True, group_id=-1001, get_topic_id=lambda _: 88)
    monkeypatch.setattr(reports, "get_topics_manager", lambda: manager)
    handle = SimpleNamespace(
        wait=AsyncMock(return_value=DeliveryResult(outcome, (77, -1001) if outcome == "delivered" else None))
    )
    dispatcher = SimpleNamespace(enqueue_send_delivery=AsyncMock(return_value=handle))
    monkeypatch.setattr(reports, "get_dispatcher", lambda: dispatcher)
    return reports.MirzaNotificationReporter(), manager, dispatcher, handle


@pytest.mark.parametrize("state", ["delivered", "failed", "skipped", "uncertain"])
async def test_final_result_reports_identity_step_and_state_once(mirza_database, monkeypatch, state):
    await event(mirza_database, state, last_error=None if state == "delivered" else "request_rejected")
    worker, _, dispatcher, _ = reporter(monkeypatch)
    await worker.run_once()
    await worker.run_once()
    call = dispatcher.enqueue_send_delivery.call_args
    assert dispatcher.enqueue_send_delivery.await_count == 1
    body = call.args[0][0]
    assert "config&lt;&amp;" in body and "Sales&lt;&amp;" in body
    assert "5123456789" in body and "Step: <b>5</b>" in body
    assert state.title() in body
    assert call.kwargs["chat_id"] == -1001 and call.kwargs["message_thread_id"] == 88
    row = await saved(mirza_database)
    assert row.state == state and row.attempts == 1
    assert row.report_state == "delivered" and row.report_message_id == 77
    # New reporter on restart cannot create a second visible report.
    another, _, dispatcher2, _ = reporter(monkeypatch)
    await another.run_once()
    dispatcher2.enqueue_send_delivery.assert_not_awaited()


@pytest.mark.parametrize("state", ["pending", "ready", "retry", "sending"])
async def test_nonterminal_customer_result_does_not_report(mirza_database, monkeypatch, state):
    await event(mirza_database, state)
    worker, _, dispatcher, _ = reporter(monkeypatch)
    await worker.run_once()
    dispatcher.enqueue_send_delivery.assert_not_awaited()


async def test_missing_dedicated_topic_waits_without_falling_back(mirza_database, monkeypatch):
    await event(mirza_database)
    worker, manager, dispatcher, _ = reporter(monkeypatch)
    manager.get_topic_id = lambda _: None
    await worker.run_once()
    dispatcher.enqueue_send_delivery.assert_not_awaited()
    assert (await saved(mirza_database)).report_state == "pending"
    manager.get_topic_id = lambda _: 88
    await worker.run_once()
    assert dispatcher.enqueue_send_delivery.await_count == 1


@pytest.mark.parametrize("outcome", ["uncertain", "failed", "cancelled"])
async def test_report_transport_failure_never_changes_or_repeats_customer_send(mirza_database, monkeypatch, outcome):
    await event(mirza_database)
    worker, _, dispatcher, _ = reporter(monkeypatch, outcome)
    await worker.run_once()
    await worker.run_once()
    row = await saved(mirza_database)
    assert row.state == "delivered" and row.attempts == 1
    assert row.report_state == ("failed" if outcome == "cancelled" else outcome)
    assert dispatcher.enqueue_send_delivery.await_count == 1


async def test_restart_does_not_repeat_interrupted_report(mirza_database, monkeypatch):
    await event(mirza_database, report_state="sending", report_chat_id=-1001, report_thread_id=88)
    worker, _, dispatcher, _ = reporter(monkeypatch)
    await worker.run_once()
    assert (await saved(mirza_database)).report_state == "uncertain"
    dispatcher.enqueue_send_delivery.assert_not_awaited()


async def test_cancellation_retains_report_ownership(mirza_database, monkeypatch):
    await event(mirza_database)
    worker, _, dispatcher, handle = reporter(monkeypatch)
    entered = asyncio.Event()

    async def pending():
        entered.set()
        await asyncio.Event().wait()

    handle.wait.side_effect = pending
    task = asyncio.create_task(worker.run_once())
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (await saved(mirza_database)).report_state == "uncertain"
    await worker.run_once()
    assert dispatcher.enqueue_send_delivery.await_count == 1


async def test_unknown_identity_is_explicit_and_private_payload_is_absent(mirza_database, monkeypatch):
    await event(
        mirza_database,
        "skipped",
        bot_name=None,
        recipient=None,
        base_url="https://private.example",
        message_text="private customer text",
        last_error="owner_unmatched",
    )
    worker, _, dispatcher, _ = reporter(monkeypatch)
    await worker.run_once()
    body = dispatcher.enqueue_send_delivery.call_args.args[0][0]
    assert "Unknown" in body and "owner_unmatched" in body
    assert "private" not in body


async def test_concurrent_report_passes_claim_one_append(mirza_database, monkeypatch):
    await event(mirza_database)
    worker, _, dispatcher, _ = reporter(monkeypatch)
    await asyncio.gather(worker.run_once(), worker.run_once())
    assert dispatcher.enqueue_send_delivery.await_count == 1


@pytest.mark.parametrize("note,want", [("5123456789 | handle | buy", "delivered"), ("bad note", "skipped")])
async def test_resolution_persists_bot_identity_even_when_recipient_is_invalid(mirza_database, monkeypatch, note, want):
    import httpx
    from test_mirza_delivery import configure_route, admit, service_with_transport
    from utils import mirza_settings as settings
    import utils.panel_api.users as users

    bot = await configure_route(settings, "owner", "https://one.example/api", "synthetic-key", "Hello")
    monkeypatch.setattr(
        users, "get_user_details", AsyncMock(return_value={"admin": {"username": "owner"}, "note": note})
    )
    await admit(mirza_database, bot)
    worker = service_with_transport(lambda _: httpx.Response(200, json={"status": True}))
    try:
        await worker.run_once()
    finally:
        await worker.close()
    result = await saved(mirza_database)
    assert result.bot_name == bot["name"] and result.route_id == bot["id"]
    assert result.state == want
    assert result.recipient == ("5123456789" if want == "delivered" else None)


async def test_cancelled_report_exits_even_when_database_finish_fails(mirza_database, monkeypatch):
    await event(mirza_database)
    worker, _, _, handle = reporter(monkeypatch)
    entered = asyncio.Event()

    async def pending():
        entered.set()
        await asyncio.Event().wait()

    handle.wait.side_effect = pending
    monkeypatch.setattr(worker, "_finish", AsyncMock(side_effect=RuntimeError("database unavailable")))
    task = asyncio.create_task(worker.run_once())
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (await saved(mirza_database)).report_state == "sending"


async def test_real_dispatcher_receipt_is_saved_without_repeating_report(mirza_database, outbound, monkeypatch):
    import utils.mirza_reports as reports
    from telegram_bot.topics import TopicType

    await event(mirza_database)
    outbound.topics.threads[TopicType.MIRZA_NOTIFICATIONS] = 88
    monkeypatch.setattr(reports, "get_topics_manager", lambda: outbound.topics)
    monkeypatch.setattr(reports, "get_dispatcher", lambda: outbound.dispatcher)
    worker = reports.MirzaNotificationReporter()
    dispatching = asyncio.create_task(outbound.dispatcher.start_worker())
    try:
        await asyncio.wait_for(worker.run_once(), 2)
        result = await saved(mirza_database)
        assert result.report_state == "delivered"
        assert (result.report_message_id, result.report_chat_id, result.report_thread_id) == (1, -100111, 88)
        await worker.run_once()
        assert len(outbound.sent) == 1
        assert outbound.sent[0]["message_thread_id"] == 88
        assert result.state == "delivered" and result.attempts == 1
    finally:
        await outbound.dispatcher.stop(wait_seconds=0.05)
        await asyncio.wait_for(dispatching, 2)
