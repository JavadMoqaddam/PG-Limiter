"""Exercise manual Mirza Bot tests through real routing, SQLite and HTTP parsing."""

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

import db.database as database
from db.crud.mirza_notifications import MirzaNotificationsCRUD
from test_config_revisions import settings_db  # noqa: F401
from test_mirza_notification_menu import complete_route, menu, open_route  # noqa: F401
from utils import mirza_settings as settings
from utils.mirza_client import MirzaClient


@pytest.fixture
def mirza_http(monkeypatch):
    import telegram_bot.handlers.notifications as notifications

    state = SimpleNamespace(
        requests=[],
        clients=[],
        response=httpx.Response(200, json={"status": True, "msg": "ok", "obj": None}),
        handler=None,
    )

    async def transport(request):
        state.requests.append(request)
        if state.handler is not None:
            return await state.handler(request)
        return state.response

    def client():
        instance = MirzaClient(transport=httpx.MockTransport(transport))
        state.clients.append(instance)
        return instance

    monkeypatch.setattr(notifications, "MirzaClient", client, raising=False)
    return state


@pytest.mark.parametrize("user", [7, 8])
async def test_connection_sends_literal_test_to_pressing_admin_with_selected_bot_credentials(menu, mirza_http, user):
    await complete_route("Other bot")
    route = await settings.create_route("Selected bot")
    route = await settings.update_route(
        route["id"],
        {
            "base_url": "https://selected.example.com/api/",
            "api_key": "selected-private-key",
        },
    )
    assert route["messages"] == []
    async with database.get_db() as db:
        counts_before = await MirzaNotificationsCRUD.counts(db)
    await open_route(menu, route)
    assert mirza_http.requests == []
    await menu.click(menu.button("Test Connection"), user=user, chat=user)
    assert len(mirza_http.requests) == 1
    request = mirza_http.requests[0]
    assert str(request.url) == "https://selected.example.com/api/users"
    assert request.method == "POST" and request.headers["Token"] == "selected-private-key"
    assert json.loads(request.content) == {"actions": "send_message", "chat_id": str(user), "text": "test"}
    assert "accepted" in menu.messages[-1].lower()
    assert "check your chat" in menu.messages[-1].lower()
    assert "selected-private-key" not in repr(menu.requests)
    assert mirza_http.clients[0]._client.is_closed
    assert await settings.get_route(route["id"]) == route
    async with database.get_db() as db:
        assert await MirzaNotificationsCRUD.counts(db) == counts_before


@pytest.mark.parametrize("values", [{}, {"api_key": "key-only"}, {"base_url": "https://bot.example.com/api"}])
async def test_connection_requires_url_and_key_before_network_request(menu, mirza_http, values):
    route = await settings.create_route("Incomplete")
    route = await settings.update_route(route["id"], values)
    await open_route(menu, route)
    await menu.click(menu.button("Test Connection"))
    assert mirza_http.requests == []
    assert "url" in menu.messages[-1].lower() and "key" in menu.messages[-1].lower()


@pytest.mark.parametrize("kwargs", [{"user": 99}, {"chat": -100, "private": False}])
async def test_connection_rejects_unauthorized_and_group_callbacks(menu, mirza_http, kwargs):
    route = await complete_route()
    await open_route(menu, route)
    await menu.click(menu.button("Test Connection"), **kwargs)
    assert mirza_http.requests == []


@pytest.mark.parametrize("change", ["edit", "delete", "revoke_admin", "inject_recipient"])
async def test_old_or_tampered_test_button_cannot_send(menu, mirza_http, change):
    route = await complete_route()
    await open_route(menu, route)
    payload = menu.button("Test Connection")
    if change == "edit":
        await settings.update_route(route["id"], {"api_key": "new-private-key"})
    elif change == "delete":
        await settings.delete_route(route["id"])
    elif change == "revoke_admin":
        menu.admins.remove(7)
    else:
        payload += ":999"
    await menu.click(payload)
    assert mirza_http.requests == []


@pytest.mark.parametrize(
    "response,visible",
    [
        (httpx.Response(200, json={"status": False, "msg": "private-test-key secret error", "obj": None}), "rejected"),
        (httpx.Response(403, text="private-test-key secret error"), "permission"),
        (httpx.Response(429, headers={"Retry-After": "5"}), "rate"),
        (httpx.Response(503, text="private-test-key secret error"), "uncertain"),
        (httpx.Response(200, text="<html>private-test-key secret error</html>"), "uncertain"),
        (httpx.Response(200, json={"status": 1}), "uncertain"),
        (httpx.Response(302, headers={"Location": "https://wrong.example.com/api/users"}), "rejected"),
    ],
)
async def test_connection_reports_safe_failure_without_retry_or_following_redirect(
    menu, mirza_http, caplog, response, visible
):
    route = await complete_route()
    mirza_http.response = response
    await open_route(menu, route)
    await menu.click(menu.button("Test Connection"))
    assert len(mirza_http.requests) == 1
    assert visible in menu.messages[-1].lower()
    assert "accepted" not in menu.messages[-1].lower()
    assert "secret error" not in repr(menu.requests) + caplog.text
    assert "private-test-key" not in repr(menu.requests) + caplog.text
    assert mirza_http.clients[0]._client.is_closed


@pytest.mark.parametrize(
    "error,visible",
    [
        (httpx.ConnectTimeout, "could not connect"),
        (httpx.ReadTimeout, "uncertain"),
        (httpx.WriteError, "uncertain"),
    ],
)
async def test_transport_failure_never_resends_manual_test(menu, mirza_http, caplog, error, visible):
    async def fail(request):
        raise error("private-test-key secret error", request=request)

    mirza_http.handler = fail
    route = await complete_route()
    await open_route(menu, route)
    await menu.click(menu.button("Test Connection"))
    assert len(mirza_http.requests) == 1
    assert visible in menu.messages[-1].lower()
    assert "secret error" not in repr(menu.requests) + caplog.text
    assert "private-test-key" not in repr(menu.requests) + caplog.text
    assert mirza_http.clients[0]._client.is_closed


async def test_concurrent_and_queued_double_clicks_send_once_but_later_explicit_test_can_send(
    menu, mirza_http, monkeypatch
):
    import telegram_bot.handlers.notifications as notifications

    clock = [100.0]
    monkeypatch.setattr(notifications, "monotonic", lambda: clock[0], raising=False)
    entered, release = asyncio.Event(), asyncio.Event()

    async def hold(request):
        entered.set()
        await release.wait()
        return mirza_http.response

    mirza_http.handler = hold
    route = await complete_route()
    await open_route(menu, route)
    payload = menu.button("Test Connection")
    first = asyncio.create_task(menu.click(payload))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        progress = menu.messages[-1]
        await menu.click(payload)
        assert len(mirza_http.requests) == 1
        assert menu.messages[-1] == progress
        assert any(
            "progress" in params.get("text", "") for method, params in menu.requests if method == "answerCallbackQuery"
        )
    finally:
        release.set()
        await first
    result_message = menu.messages[-1]
    await menu.click(payload)
    assert len(mirza_http.requests) == 1
    assert menu.messages[-1] == result_message
    assert any("wait" in params.get("text", "") for method, params in menu.requests if method == "answerCallbackQuery")
    clock[0] += 6
    await menu.click(payload)
    assert len(mirza_http.requests) == 2


async def test_configuration_edit_during_progress_prevents_using_stale_credentials(menu, mirza_http, monkeypatch):
    import telegram_bot.handlers.notifications as notifications

    route = await complete_route()
    await open_route(menu, route)
    respond = notifications._route

    async def edit_during_progress(update, selected, notice=""):
        result = await respond(update, selected, notice)
        if "Testing connection" in notice:
            await settings.update_route(route["id"], {"api_key": "replacement-secret"})
        return result

    monkeypatch.setattr(notifications, "_route", edit_during_progress)
    await menu.click(menu.button("Test Connection"))
    assert mirza_http.requests == []
    assert "changed" in menu.messages[-1].lower()


async def test_admin_revoked_during_progress_cannot_send_test(menu, mirza_http, monkeypatch):
    import telegram_bot.handlers.notifications as notifications

    route = await complete_route()
    await open_route(menu, route)
    respond = notifications._route

    async def revoke_during_progress(update, selected, notice=""):
        result = await respond(update, selected, notice)
        if "Testing connection" in notice:
            menu.admins.remove(7)
        return result

    monkeypatch.setattr(notifications, "_route", revoke_during_progress)
    await menu.click(menu.button("Test Connection"))
    assert mirza_http.requests == []


async def test_inflight_result_is_not_presented_as_validation_of_new_configuration(menu, mirza_http):
    route = await complete_route()

    async def replace_during_request(request):
        await settings.update_route(route["id"], {"base_url": "https://changed.example.com/api"})
        return mirza_http.response

    mirza_http.handler = replace_during_request
    await open_route(menu, route)
    await menu.click(menu.button("Test Connection"))
    assert len(mirza_http.requests) == 1
    assert "previous" in menu.messages[-1].lower() and "changed" in menu.messages[-1].lower()


async def test_cancelled_test_closes_client_and_releases_inflight_guard(menu, mirza_http, monkeypatch):
    import telegram_bot.handlers.notifications as notifications

    clock = [100.0]
    monkeypatch.setattr(notifications, "monotonic", lambda: clock[0], raising=False)
    entered = asyncio.Event()

    async def hold(request):
        entered.set()
        await asyncio.Event().wait()

    mirza_http.handler = hold
    route = await complete_route()
    await open_route(menu, route)
    payload = menu.button("Test Connection")
    task = asyncio.create_task(menu.click(payload))
    try:
        await asyncio.wait_for(entered.wait(), 2)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert mirza_http.clients[0]._client.is_closed
    mirza_http.handler = None
    clock[0] += 6
    await menu.click(payload)
    assert len(mirza_http.requests) == 2
    assert "accepted" in menu.messages[-1].lower()


@pytest.mark.parametrize("failure,visible", [("send", "uncertain"), ("close", "accepted"), ("refresh", "accepted")])
async def test_unexpected_failure_after_send_preserves_known_result_or_uncertainty(
    menu, mirza_http, monkeypatch, caplog, failure, visible
):
    route = await complete_route()
    await open_route(menu, route)
    get_route = settings.get_route

    async def failing_refresh(route_id):
        if mirza_http.requests:
            raise RuntimeError("private-test-key refresh secret")
        return await get_route(route_id)

    async def fail(request):
        if failure == "send":
            raise RuntimeError("private-test-key send secret")
        close = mirza_http.clients[0].close

        async def failing_close():
            await close()
            raise RuntimeError("private-test-key close secret")

        mirza_http.clients[0].close = failing_close
        return mirza_http.response

    if failure == "refresh":
        monkeypatch.setattr(settings, "get_route", failing_refresh)
    else:
        mirza_http.handler = fail
    await menu.click(menu.button("Test Connection"))
    assert len(mirza_http.requests) == 1
    assert visible in menu.messages[-1].lower()
    assert "try again" not in menu.messages[-1].lower()
    assert "private-test-key" not in repr(menu.requests) + caplog.text
    assert mirza_http.clients[0]._client.is_closed


async def test_failed_result_edit_uses_callback_alert_without_new_message_or_test(menu, mirza_http, monkeypatch):
    import telegram_bot.handlers.notifications as notifications

    route = await complete_route()
    await open_route(menu, route)
    respond = notifications._route

    async def fail_result(update, selected, notice=""):
        if "accepted" in notice:
            raise RuntimeError("private-test-key result secret")
        return await respond(update, selected, notice)

    monkeypatch.setattr(notifications, "_route", fail_result)
    await menu.click(menu.button("Test Connection"))
    assert len(mirza_http.requests) == 1
    assert any(
        "accepted" in params.get("text", "") and params.get("show_alert")
        for method, params in menu.requests
        if method == "answerCallbackQuery"
    )
    assert "Could not load or save" not in menu.messages[-1]
