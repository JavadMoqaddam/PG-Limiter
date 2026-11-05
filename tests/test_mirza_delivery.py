"""Real HTTP contract and SQLite ownership; external services never contacted."""

import asyncio
import json
from copy import deepcopy
from unittest.mock import AsyncMock

import httpx
import pytest

from test_mirza_persistence import mirza_database  # noqa: F401
from test_mirza_settings import configure_route, edit_message
from utils import mirza_settings as settings


@pytest.mark.parametrize(
    "response,want",
    [
        (httpx.Response(200, json={"status": True, "msg": "ok", "obj": None}), "delivered"),
        (httpx.Response(200, json={"status": False, "msg": "private remote error"}), "failed"),
        (httpx.Response(403, text="private remote error"), "failed"),
        (httpx.Response(429, headers={"Retry-After": "99999"}), "retry"),
        (httpx.Response(503, text="private remote error"), "uncertain"),
        (httpx.Response(200, text="not json"), "uncertain"),
        (httpx.Response(200, json={"status": 1}), "uncertain"),
        (httpx.Response(302, headers={"Location": "https://wrong.example/api/users"}), "failed"),
    ],
)
async def test_http_contract_and_response_ownership(response, want):
    from utils.mirza_client import MirzaClient

    requests = []

    def transport(request):
        requests.append(request)
        return response

    client = MirzaClient(transport=httpx.MockTransport(transport))
    try:
        result = await client.send_message("https://one.example/api", "key-one", "5123456789", "<b>configured</b>")
    finally:
        await client.close()
    assert result.status == want
    assert len(requests) == 1
    request = requests[0]
    assert request.method == "POST" and str(request.url) == "https://one.example/api/users"
    assert request.headers["Token"] == "key-one"
    assert json.loads(request.content) == {
        "actions": "send_message",
        "chat_id": "5123456789",
        "text": "<b>configured</b>",
    }
    assert "private" not in result.reason and "key-one" not in result.reason
    if want == "retry":
        assert 1 <= result.retry_after <= 300


@pytest.mark.parametrize(
    "error,want",
    [
        (httpx.ConnectError, "retry"),
        (httpx.ConnectTimeout, "retry"),
        (httpx.PoolTimeout, "retry"),
        (httpx.ReadTimeout, "uncertain"),
        (httpx.WriteTimeout, "uncertain"),
        (httpx.ReadError, "uncertain"),
        (httpx.WriteError, "uncertain"),
    ],
)
async def test_only_definite_pretransmission_errors_retry(error, want):
    from utils.mirza_client import MirzaClient

    def transport(request):
        raise error("private key-one remote error", request=request)

    client = MirzaClient(transport=httpx.MockTransport(transport))
    try:
        result = await client.send_message("https://one.example/api", "key-one", "5123456789", "message")
    finally:
        await client.close()
    assert result.status == want
    assert "private" not in result.reason and "key-one" not in result.reason


@pytest.fixture
async def delivery_env(mirza_database, monkeypatch):
    import utils.panel_api.users as users

    route = await configure_route(settings, "owner", "https://one.example/api", "key-one", "<b>[Username]</b>: [Limit]")
    metadata = {
        "username": "config<&",
        "admin": {"id": 7, "username": "owner"},
        "note": "5123456789 | handle | buy",
        "status": "disabled",
        "group_ids": [999],
        "expire": None,
        "data_limit": None,
    }
    monkeypatch.setattr(users, "get_user_details", AsyncMock(side_effect=lambda *_: deepcopy(metadata)))
    return route, metadata


async def admit(sessions, route, incident="incident", *, username="config<&", limit=4):
    from db.crud.punishment_facts import PunishmentFactsCRUD

    async with sessions() as db:
        await PunishmentFactsCRUD.store(
            db,
            {
                "incident_id": incident,
                "username": username,
                "step_index": 0,
                "started_at": 99.0,
                "applied_at": 100.0,
                "state": "completed",
                "disable_succeeded": True,
                "revoke_succeeded": False,
                "uuid_reset_succeeded": False,
                "warning_succeeded": False,
                "notification_context": {
                    "candidates": [
                        {
                            "route_id": route["id"],
                            "revision": route["revision"],
                            "admin_usernames": route["admin_usernames"],
                            "message_id": route["messages"][0]["id"],
                        }
                    ],
                    "step_index": 0,
                    "step_count": 1,
                    "step_type": "disable",
                    "effective_limit": limit,
                },
            },
        )
        await db.commit()


async def row(sessions, incident="incident"):
    from db.crud.mirza_notifications import MirzaNotificationsCRUD

    async with sessions() as db:
        return await MirzaNotificationsCRUD.get(db, incident)


def service_with_transport(handler, clock=lambda: 100.0):
    from utils.mirza_client import MirzaClient
    from utils.mirza_notifications import MirzaNotificationService

    return MirzaNotificationService(object(), client=MirzaClient(transport=httpx.MockTransport(handler)), clock=clock)


async def test_worker_pins_fresh_metadata_and_safe_incident_text(delivery_env, mirza_database):
    route, metadata = delivery_env
    await admit(mirza_database, route)
    metadata["note"] = "6123456789 | changed_handle | buy"
    requests = []

    def transport(request):
        requests.append(request)
        return httpx.Response(200, json={"status": True})

    service = service_with_transport(transport)
    try:
        await asyncio.gather(service.run_once(), service.run_once())
        await admit(mirza_database, route)
        await service.run_once()
    finally:
        await service.close()
    saved = await row(mirza_database)
    assert saved.state == "delivered" and saved.attempts == 1
    assert saved.recipient == "6123456789" and saved.message_text == "<b>config&lt;&amp;</b>: 4"
    assert len(requests) == 1 and requests[0].headers["Token"] == "key-one"
    assert json.loads(requests[0].content)["chat_id"] == "6123456789"


@pytest.mark.parametrize("change", ["note", "owner", "missing", "edit", "delete", "disable"])
async def test_invalid_metadata_or_old_unpinned_route_never_sends(delivery_env, mirza_database, monkeypatch, change):
    import utils.panel_api.users as users
    from utils.mirza_settings import update_route, delete_route

    route, metadata = delivery_env
    await admit(mirza_database, route)
    if change == "note":
        metadata["note"] = "@username | 5123456789"
    elif change == "owner":
        metadata["admin"]["username"] = "Owner"
    elif change == "missing":
        monkeypatch.setattr(users, "get_user_details", AsyncMock(return_value=None))
    elif change == "delete":
        await delete_route(route["id"])
    else:
        if change == "disable":
            await update_route(route["id"], {"enabled": False})
        else:
            await edit_message(settings, route, {"message_text": "new message"})
    requests = []
    service = service_with_transport(
        lambda request: requests.append(request) or httpx.Response(200, json={"status": True})
    )
    try:
        await service.run_once()
    finally:
        await service.close()
    assert (await row(mirza_database)).state == "skipped" and requests == []


async def test_retry_backoff_allows_other_owner_then_stops_at_three_attempts(delivery_env, mirza_database, monkeypatch):
    import utils.panel_api.users as users

    route, _ = delivery_env
    second = await configure_route(settings, "other-owner", "https://two.example/api", "key-two", "other text")
    await admit(mirza_database, route, "one", username="one")
    await admit(mirza_database, second, "two", username="two")

    async def details(_, username):
        return {
            "admin": {"username": "owner" if username == "one" else "other-owner"},
            "note": "5123456789 | first | buy" if username == "one" else "6123456789 | second | buy",
        }

    monkeypatch.setattr(users, "get_user_details", details)
    requests = []
    now = [100.0]

    def transport(request):
        requests.append(request)
        return (
            httpx.Response(429, headers={"Retry-After": "10"})
            if request.url.host == "one.example"
            else httpx.Response(200, json={"status": True})
        )

    service = service_with_transport(transport, lambda: now[0])
    try:
        await service.run_once()
        assert (await row(mirza_database, "one")).state == "retry"
        assert (await row(mirza_database, "two")).state == "delivered"
        await service.run_once()
        assert len(requests) == 2
        for _ in range(3):
            now[0] += 300
            await service.run_once()
    finally:
        await service.close()
    assert (await row(mirza_database, "one")).state == "failed"
    assert len(requests) == 4
    assert {(r.url.host, r.headers["Token"], json.loads(r.content)["chat_id"]) for r in requests} == {
        ("one.example", "key-one", "5123456789"),
        ("two.example", "key-two", "6123456789"),
    }


@pytest.mark.parametrize("cancel", [True, False])
async def test_interrupted_http_and_restart_never_resend(delivery_env, mirza_database, cancel):
    from db.crud.mirza_notifications import MirzaNotificationsCRUD

    route, _ = delivery_env
    await admit(mirza_database, route)
    entered = asyncio.Event()
    requests = []

    async def transport(request):
        requests.append(request)
        entered.set()
        await asyncio.Event().wait()

    service = service_with_transport(transport)
    if cancel:
        task = asyncio.create_task(service.run_once())
        await asyncio.wait_for(entered.wait(), 2)
        assert (await row(mirza_database)).state == "sending"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        async with mirza_database() as db:
            await MirzaNotificationsCRUD.transition(db, "incident", "pending", "sending")
            await db.commit()
    await service.close()
    restarted = service_with_transport(
        lambda request: requests.append(request) or httpx.Response(200, json={"status": True})
    )
    try:
        await restarted.run_once()
        await restarted.run_once()
    finally:
        await restarted.close()
    assert (await row(mirza_database)).state == "uncertain"
    assert len(requests) == int(cancel)


async def test_metadata_failures_are_bounded_without_remote_delivery(delivery_env, mirza_database, monkeypatch):
    import utils.panel_api.users as users

    route, _ = delivery_env
    await admit(mirza_database, route)
    monkeypatch.setattr(users, "get_user_details", AsyncMock(side_effect=ValueError("private remote body")))
    requests = []
    now = [100.0]
    service = service_with_transport(lambda r: requests.append(r), lambda: now[0])
    try:
        for _ in range(5):
            await service.run_once()
            now[0] += 300
    finally:
        await service.close()
    saved = await row(mirza_database)
    assert saved.state == "failed" and saved.resolve_attempts == 3 and saved.attempts == 0
    assert "private" not in saved.last_error and requests == []


@pytest.mark.parametrize("template,limit", [("[Limit]", None), ("x" * 4086 + "[Username]", 4)])
async def test_unrenderable_payload_fails_without_http(delivery_env, mirza_database, template, limit):
    route, _ = delivery_env
    route = await edit_message(settings, route, {"message_text": template})
    await admit(mirza_database, route, limit=limit)
    requests = []
    service = service_with_transport(lambda r: requests.append(r))
    try:
        await service.run_once()
    finally:
        await service.close()
    assert (await row(mirza_database)).state == "failed" and requests == []


async def test_worker_flushes_journal_with_daily_topics_disabled(delivery_env, mirza_database, monkeypatch, tmp_path):
    import db.database as database
    import utils.punishment_facts as facts
    from utils.mirza_settings import capture_rules, notification_context
    import telegram_bot.topics as topics

    manager = topics.TopicsManager()
    manager._enabled = False
    monkeypatch.setattr(topics, "_topics_manager", manager)
    context = notification_context(await capture_rules(), 0, 1, "disable", 4)
    path = str(tmp_path / "pending.json")
    journal = facts.PunishmentFactJournal(path)
    monkeypatch.setattr(database, "DB_AVAILABLE", False)
    async with journal.action("config<&", 0, notification_context=context) as action:
        action.mark_disabled()
    monkeypatch.setattr(database, "DB_AVAILABLE", True)
    monkeypatch.setattr(facts, "_journal", facts.PunishmentFactJournal(path))
    service = service_with_transport(lambda r: httpx.Response(200, json={"status": True}))
    try:
        await service.run_once()
    finally:
        await service.close()
    assert (await row(mirza_database, action.incident_id)).state == "delivered"


@pytest.mark.parametrize("stage", ["sending", "delivered"])
async def test_commit_failure_never_reenters_http(delivery_env, mirza_database, monkeypatch, stage):
    from sqlalchemy.ext.asyncio import AsyncSession
    from db.crud.mirza_notifications import MirzaNotificationsCRUD

    route, _ = delivery_env
    await admit(mirza_database, route)
    original = AsyncSession.commit

    async def fail_commit(session):
        saved = await MirzaNotificationsCRUD.get(session, "incident")
        if saved and saved.state == stage:
            raise RuntimeError("synthetic database failure")
        return await original(session)

    monkeypatch.setattr(AsyncSession, "commit", fail_commit)
    requests = []
    service = service_with_transport(lambda r: requests.append(r) or httpx.Response(200, json={"status": True}))
    try:
        await service.run_once()
        if stage == "sending":
            assert requests == []
            assert (await row(mirza_database)).state == "ready"
        else:
            assert len(requests) == 1
            assert (await row(mirza_database)).state == "sending"
            await service.run_once()
            assert len(requests) == 1
    finally:
        await service.close()


async def test_cancel_during_acknowledgement_finishes_local_commit(delivery_env, mirza_database, monkeypatch):
    from sqlalchemy.ext.asyncio import AsyncSession
    from db.crud.mirza_notifications import MirzaNotificationsCRUD

    route, _ = delivery_env
    await admit(mirza_database, route)
    original = AsyncSession.commit
    entered, release = asyncio.Event(), asyncio.Event()

    async def blocked_commit(session):
        saved = await MirzaNotificationsCRUD.get(session, "incident")
        if saved and saved.state == "delivered":
            entered.set()
            await release.wait()
        return await original(session)

    monkeypatch.setattr(AsyncSession, "commit", blocked_commit)
    service = service_with_transport(lambda r: httpx.Response(200, json={"status": True}))
    task = asyncio.create_task(service.run_once())
    try:
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert (await row(mirza_database)).state == "delivered"
    finally:
        release.set()
        await service.close()


async def test_retry_keeps_pinned_recipient_and_edits_cancel_it(delivery_env, mirza_database):
    from utils.mirza_settings import update_route

    route, metadata = delivery_env
    await admit(mirza_database, route)
    requests = []
    now = [100.0]
    service = service_with_transport(lambda r: requests.append(r) or httpx.Response(429), lambda: now[0])
    try:
        await service.run_once()
        metadata["note"] = "6123456789 | other | buy"
        now[0] += 300
        await service.run_once()
        assert len(requests) == 2 and all(json.loads(r.content)["chat_id"] == "5123456789" for r in requests)
        await update_route(route["id"], {"api_key": "new-key"})
        now[0] += 300
        await service.run_once()
        assert (await row(mirza_database)).state == "skipped" and len(requests) == 2
    finally:
        await service.close()


async def test_worker_pass_is_bounded_and_leaves_later_events_for_next_pass(delivery_env, mirza_database):
    route, _ = delivery_env
    for i in range(26):
        await admit(mirza_database, route, f"incident-{i:02}")
    requests = []
    service = service_with_transport(lambda r: requests.append(r) or httpx.Response(200, json={"status": True}))
    try:
        await service.run_once()
        assert len(requests) == 25 and (await row(mirza_database, "incident-25")).state == "pending"
        await service.run_once()
        assert len(requests) == 26
    finally:
        await service.close()


async def test_root_lifecycle_settles_delivery_and_closes_client_before_database(
    delivery_env, mirza_database, monkeypatch
):
    import limiter
    import db.database as database
    import utils.mirza_notifications as notifications

    route, _ = delivery_env
    await admit(mirza_database, route)
    entered = asyncio.Event()
    closed = []

    async def transport(request):
        entered.set()
        await asyncio.Event().wait()

    service = service_with_transport(transport)
    original_close = service.close

    async def close_client():
        assert (await row(mirza_database)).state == "uncertain"
        await original_close()
        closed.append("client")

    monkeypatch.setattr(service, "close", close_client)
    monkeypatch.setattr(notifications, "MirzaNotificationService", lambda panel: service)

    async def close_db():
        assert closed == ["client"]
        closed.append("database")

    monkeypatch.setattr(database, "close_db", close_db)
    limiter.start_mirza_notifications(object())
    await asyncio.wait_for(entered.wait(), 2)
    await limiter.cleanup_resources()
    assert closed == ["client", "database"]


@pytest.mark.parametrize("stage,want,requests_count", [("sending", "sending", 0), ("delivered", "delivered", 1)])
async def test_after_commit_failure_preserves_nonretryable_ownership(
    delivery_env, mirza_database, monkeypatch, stage, want, requests_count
):
    from sqlalchemy.ext.asyncio import AsyncSession
    from db.crud.mirza_notifications import MirzaNotificationsCRUD

    route, _ = delivery_env
    await admit(mirza_database, route)
    original = AsyncSession.commit

    async def interrupted_ack(session):
        saved = await MirzaNotificationsCRUD.get(session, "incident")
        interrupted = saved and saved.state == stage
        await original(session)
        if interrupted:
            raise RuntimeError("synthetic after-commit failure")

    monkeypatch.setattr(AsyncSession, "commit", interrupted_ack)
    requests = []
    service = service_with_transport(lambda r: requests.append(r) or httpx.Response(200, json={"status": True}))
    try:
        await service.run_once()
        await service.run_once()
        assert (await row(mirza_database)).state == want and len(requests) == requests_count
    finally:
        await service.close()


async def test_cancellation_survives_failed_uncertain_finalization(delivery_env, mirza_database, monkeypatch):
    from sqlalchemy.ext.asyncio import AsyncSession
    from db.crud.mirza_notifications import MirzaNotificationsCRUD

    route, _ = delivery_env
    await admit(mirza_database, route)
    original = AsyncSession.commit

    async def fail_uncertain(session):
        saved = await MirzaNotificationsCRUD.get(session, "incident")
        if saved and saved.state == "uncertain":
            raise RuntimeError("synthetic finalization outage")
        await original(session)

    monkeypatch.setattr(AsyncSession, "commit", fail_uncertain)
    entered = asyncio.Event()

    async def transport(request):
        entered.set()
        await asyncio.Event().wait()

    service = service_with_transport(transport)
    caller = asyncio.create_task(service.run_once())
    try:
        await asyncio.wait_for(entered.wait(), 2)
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(caller, 2)
        assert (await row(mirza_database)).state == "sending"
    finally:
        await service.close()
