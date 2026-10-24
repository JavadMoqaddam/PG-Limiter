"""Status cards report owner-published facts and remain bounded and read-only."""

import importlib
from html.parser import HTMLParser
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.error import BadRequest, TelegramError
from telegram.ext import ConversationHandler

from test_telegram_input_ownership import dispatcher  # noqa: F401 -- shared offline PTB fixture


@pytest.fixture
def reports():
    return importlib.import_module("telegram_bot.handlers.status_reports")


def facts(reports, **overrides):
    values = dict(
        config={"ip_source": "api", "device_count_mode": "device"},
        admission=None,
        completed=None,
        attempt=None,
        collection=None,
        config_revision=4,
    )
    values.update(overrides)
    return reports.ReportFacts(**values)


def receipt(**overrides):
    values = dict(
        completed_at=1000,
        source="logs",
        users_counted=7,
        config_revision=3,
        admission=SimpleNamespace(allowed=True, revision=1),
        protected_users_count=2,
        metadata_unknown_users_count=1,
        failed_users_count=0,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


class BalancedHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.text = []

    def handle_starttag(self, tag, attrs):
        assert tag in {"b", "i", "code"}
        self.stack.append(tag)

    def handle_endtag(self, tag):
        assert self.stack.pop() == tag

    def handle_data(self, data):
        self.text.append(data)


def plain(text):
    parser = BalancedHTML()
    parser.feed(text)
    parser.close()
    assert not parser.stack
    assert len(text.encode("utf-16-le")) // 2 <= 2000
    return "".join(parser.text)


def test_main_menu_has_one_status_entry():
    from telegram_bot.keyboards import create_main_menu_keyboard

    buttons = [button for row in create_main_menu_keyboard().inline_keyboard for button in row]
    assert len([b for b in buttons if "Status & Reports" in b.text]) == 1
    assert not any(b.text in {"📊 Reports", "📡 Monitoring"} for b in buttons)
    assert any("Users" in b.text for b in buttons)


def test_no_receipt_or_runtime_is_unavailable_not_a_success(reports):
    text = plain(reports.render_status(facts(reports), now=1200))
    assert "unavailable" in text.lower()
    assert "No completed scan" in text
    assert "Users counted: 0" not in text
    assert "running" not in text.lower()


@pytest.mark.parametrize("health", [False, True])
def test_historical_receipt_survives_pause_source_and_revision_change(reports, health):
    data = facts(reports, completed=receipt(), admission=SimpleNamespace(allowed=False, reason="Topics disabled"))
    render = reports.render_health if health else reports.render_status
    text = plain(render(data, now=1200))
    assert "paused" in text.lower()
    assert "Topics disabled" in text
    assert "Users counted: 7" in text
    assert "Logs" in text and "API" in text
    assert "200s ago" in text
    assert "Settings changed since this scan" in text
    assert "online" not in text.lower()


@pytest.mark.parametrize("outcome", ["failed", "skipped", "completed"])
def test_latest_attempt_never_replaces_completed_receipt(reports, outcome):
    attempt = SimpleNamespace(
        finished_at=1100,
        source="api",
        outcome=outcome,
        reason="Database configuration is unavailable",
        config_revision=4,
    )
    text = plain(reports.render_health(facts(reports, completed=receipt(), attempt=attempt), now=1200))
    assert "Users counted: 7" in text
    assert f"Latest finished attempt: {outcome}" in text
    assert "100s ago" in text
    if outcome == "skipped":
        assert "Configuration unavailable" in text
    assert "false positive" not in text.lower()


def test_collection_start_and_mutating_defaults_do_not_establish_finished_scan(reports):
    collection = {
        "ran_at": 1150,
        "collection_outcome": "failure",
        "fetched_failed": 2,
        "coverage": 0.5,
        "node_coverage": 0.25,
        "skipped_reason": "secret_user 1.2.3.4",
    }
    text = plain(reports.render_health(facts(reports, collection=collection), now=1200))
    assert "No completed scan" in text
    assert "API collection diagnostics" in text
    assert "started" in text and "50s ago" in text
    assert "Lookup failures: 2" in text
    assert "User coverage: 50%" in text and "Node coverage: 25%" in text
    assert "failure" not in text.lower().replace("failures", "")
    assert "secret_user" not in text and "1.2.3.4" not in text


def test_log_mode_does_not_relabel_api_collection_as_current(reports):
    text = plain(
        reports.render_health(
            facts(
                reports,
                config={"ip_source": "logs", "device_count_mode": "ip"},
                collection={"ran_at": 1000, "fetched_failed": 0},
            ),
            now=1200,
        )
    )
    assert "Historical API collection diagnostics" in text
    assert "heartbeat" not in text.lower()


@pytest.mark.parametrize("collection", [None, {}, {"ran_at": 0, "fetched_failed": 0}])
def test_missing_collection_and_event_metrics_never_become_zero(reports, collection):
    text = plain(reports.render_health(facts(reports, collection=collection), now=1200))
    assert "Collection diagnostics: unavailable" in text
    assert "Lookup failures: 0" not in text
    assert "Warnings: 0" not in text and "Punishments: 0" not in text


def test_only_owner_published_scan_aggregates_are_shown(reports):
    text = plain(reports.render_health(facts(reports, completed=receipt()), now=1200))
    assert "Protected: 2" in text and "Metadata unknown: 1" in text and "User failures: 0" in text
    assert "Warnings:" not in text and "Punishments:" not in text


@pytest.mark.parametrize("health", [False, True])
def test_long_reason_html_entities_and_emoji_keep_balanced_budget(reports, health):
    reason = "<>&😀" * 2000
    data = facts(
        reports,
        admission=SimpleNamespace(allowed=False, reason=reason),
        completed=receipt(),
        attempt=SimpleNamespace(finished_at=1100, source="api", outcome="skipped", reason=reason, config_revision=4),
    )
    render = reports.render_health if health else reports.render_status
    text = render(data, now=1200)
    decoded = plain(text)
    assert "<>&😀" not in decoded
    assert "Reason unavailable" in decoded


@pytest.mark.parametrize("outcome", ["failed", "skipped", "completed", "unknown"])
def test_attempt_reason_never_discloses_exception_or_individual_details(reports, outcome):
    reason = "password=private-token user=private_user ip=203.0.113.8 <>&😀"
    attempt = SimpleNamespace(finished_at=1100, source="api", outcome=outcome, reason=reason, config_revision=4)
    text = plain(reports.render_health(facts(reports, completed=receipt(), attempt=attempt), now=1200))
    assert "private-token" not in text
    assert "private_user" not in text
    assert "203.0.113.8" not in text
    assert "Users counted: 7" in text
    assert "Latest finished attempt:" in text


@pytest.mark.parametrize("health", [False, True])
def test_admission_unknown_reason_never_discloses_private_details(reports, health):
    data = facts(
        reports,
        admission=SimpleNamespace(allowed=False, reason="user=private_user ip=203.0.113.8 token=private-token"),
    )
    render = reports.render_health if health else reports.render_status
    text = plain(render(data, now=1200))
    assert "paused" in text.lower()
    assert "private_user" not in text and "203.0.113.8" not in text and "private-token" not in text


def test_failed_attempt_never_relabels_known_skip_as_failure_cause(reports):
    attempt = SimpleNamespace(
        finished_at=1100,
        source="api",
        outcome="failed",
        reason="Database configuration is unavailable",
        config_revision=4,
    )
    text = plain(reports.render_health(facts(reports, attempt=attempt), now=1200))
    assert "Latest finished attempt: failed" in text
    assert "Scan failed" in text
    assert "Configuration unavailable" not in text


def test_degraded_config_never_shows_enabled_or_default_modes(reports):
    text = plain(
        reports.render_status(
            facts(reports, config={"config_degraded": True}, admission=SimpleNamespace(allowed=True, reason="")),
            now=1200,
        )
    )
    assert "paused" in text.lower() and "Configuration unavailable" in text
    assert "Monitoring admission: enabled" not in text
    assert "IP source: unavailable" in text


def test_unavailable_runtime_does_not_claim_no_receipt_exists(reports):
    text = plain(reports.render_status(facts(reports, runtime_available=False), now=1200))
    assert "Completed scan telemetry: unavailable" in text
    assert "No completed scan" not in text


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["status_command", "health_report_command", "legacy_report_command"])
async def test_every_direct_route_authorizes_before_reads(reports, monkeypatch, name):
    monkeypatch.setattr(reports, "check_admin_privilege", AsyncMock(return_value=ConversationHandler.END))

    async def forbidden_read():
        pytest.fail("unauthorized health read")

    monkeypatch.setattr(reports, "collect_report_facts", forbidden_read)
    result = await getattr(reports, name)(SimpleNamespace(), SimpleNamespace(user_data={}))
    assert result == ConversationHandler.END


@pytest.mark.asyncio
async def test_delivery_final_failure_is_observable(reports, caplog):
    query = SimpleNamespace(
        answer=AsyncMock(),
        edit_message_text=AsyncMock(side_effect=TelegramError("edit")),
        message=SimpleNamespace(reply_text=AsyncMock(side_effect=TelegramError("fallback"))),
    )
    with pytest.raises(TelegramError, match="fallback"):
        await reports.deliver_card(SimpleNamespace(callback_query=query), "short", None)
    assert "Status/report delivery failed" in caplog.text


@pytest.mark.asyncio
async def test_unchanged_refresh_is_success_without_fallback(reports):
    async def forbidden_reply(**kwargs):
        pytest.fail("unchanged card should not create a duplicate")

    query = SimpleNamespace(
        answer=AsyncMock(),
        edit_message_text=AsyncMock(side_effect=BadRequest("Message is not modified")),
        message=SimpleNamespace(reply_text=forbidden_reply),
    )
    await reports.deliver_card(SimpleNamespace(callback_query=query), "short", None)


@pytest.mark.asyncio
async def test_snapshot_retries_config_revision_change(reports, monkeypatch):
    revision = [1]
    calls = []

    async def read():
        calls.append(1)
        old = revision[0]
        revision[0] = 2
        return {"ip_source": "logs" if old == 1 else "api", "device_count_mode": "ip"}, old

    monkeypatch.setattr(reports, "_read_current_config", read)
    monkeypatch.setattr(reports, "_config_revision", lambda: revision[0])
    monkeypatch.setattr(reports, "_runtime_module", lambda: None)
    data = await reports.collect_report_facts()
    assert data.config["ip_source"] == "api"
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_failed_config_read_is_masked_and_unavailable(reports, monkeypatch):
    async def read():
        raise RuntimeError("password=my-secret")

    monkeypatch.setattr(reports, "_read_current_config", read)
    data = await reports.collect_report_facts()
    text = plain(reports.render_health(data, now=1200))
    assert "Configuration unavailable" in text
    assert "my-secret" not in text


@pytest.mark.asyncio
async def test_config_failure_keeps_actual_historical_receipt(reports, monkeypatch):
    runtime = SimpleNamespace(
        monitoring_snapshot=lambda: SimpleNamespace(allowed=True, reason=""),
        get_last_completed_scan=lambda: receipt(),
        get_last_cycle_outcome=lambda: None,
    )
    monkeypatch.setattr(reports, "_runtime_module", lambda: runtime)
    monkeypatch.setattr(reports, "_config_revision", lambda: 4)

    async def read():
        raise OSError("private password")

    monkeypatch.setattr(reports, "_read_current_config", read)
    text = plain(reports.render_status(await reports.collect_report_facts(), now=1200))
    assert "Configuration unavailable" in text
    assert "Users counted: 7" in text
    assert "No completed scan" not in text
    assert "private password" not in text


@pytest.mark.asyncio
async def test_continuously_changing_config_never_presents_enabled(reports, monkeypatch):
    monkeypatch.setattr(reports, "_read_current_config", AsyncMock(return_value=({"ip_source": "api"}, 1)))
    monkeypatch.setattr(reports, "_config_revision", lambda: 2)
    monkeypatch.setattr(reports, "_runtime_module", lambda: None)
    text = plain(reports.render_status(await reports.collect_report_facts(), now=1200))
    assert "Configuration unavailable" in text
    assert "Monitoring admission: enabled" not in text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "module,name",
    [
        ("reports", "connection_report_command"),
        ("reports", "ip_history_12h_command"),
        ("monitoring", "monitoring_details"),
    ],
)
async def test_legacy_direct_entries_do_not_read_when_denied(reports, monkeypatch, module, name):
    monkeypatch.setattr(reports, "check_admin_privilege", AsyncMock(return_value=ConversationHandler.END))

    async def forbidden_read():
        pytest.fail("legacy unauthorized health read")

    monkeypatch.setattr(reports, "collect_report_facts", forbidden_read)
    legacy = importlib.import_module(f"telegram_bot.handlers.{module}")
    assert await getattr(legacy, name)(SimpleNamespace(), SimpleNamespace(user_data={})) == ConversationHandler.END


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "module,name",
    [
        ("reports", "connection_report_command"),
        ("reports", "node_usage_report_command"),
        ("reports", "multi_device_users_command"),
        ("reports", "users_by_node_command"),
        ("reports", "users_by_protocol_command"),
        ("reports", "ip_history_12h_command"),
        ("reports", "ip_history_48h_command"),
        ("monitoring", "monitoring_status"),
        ("monitoring", "monitoring_details"),
    ],
)
async def test_old_report_functions_are_safe_aggregate_entries(reports, monkeypatch, module, name):
    legacy = importlib.import_module(f"telegram_bot.handlers.{module}")
    monkeypatch.setattr(reports, "check_admin_privilege", AsyncMock(return_value=None))
    monkeypatch.setattr(reports, "collect_report_facts", AsyncMock(return_value=facts(reports)))
    message = SimpleNamespace(reply_text=AsyncMock())
    update = SimpleNamespace(message=message, callback_query=None)
    context = SimpleNamespace(user_data={}, args=[])
    await getattr(legacy, name)(update, context)
    delivered = " ".join(call.kwargs.get("text", str(call.args)) for call in message.reply_text.await_args_list)
    assert "Limiter status" in delivered or "Aggregate health report" in delivered
    assert "No completed scan" in delivered


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "route",
    [
        "status_reports",
        "health_report",
        "reports_menu",
        "monitoring_menu",
        "report_connection",
        "report_node_usage",
        "report_multi_device",
        "report_ip_12h",
        "report_ip_48h",
        "monitoring_status",
        "monitoring_details",
    ],
)
async def test_callback_routes_enter_bounded_authenticated_tree(reports, monkeypatch, route):
    import telegram_bot.main as main

    monkeypatch.setattr(reports, "check_admin_privilege", AsyncMock(return_value=None))
    monkeypatch.setattr(reports, "collect_report_facts", AsyncMock(return_value=facts(reports)))
    query = SimpleNamespace(answer=AsyncMock(), edit_message_text=AsyncMock(), message=SimpleNamespace())
    update = SimpleNamespace(callback_query=query)
    context = SimpleNamespace(user_data={})
    assert route in main.CALLBACK_ROUTES
    await main.CALLBACK_ROUTES[route](update, query, context)
    text = query.edit_message_text.await_args.kwargs["text"]
    assert "No completed scan" in plain(text)


def test_new_commands_are_registered_to_authorized_entries(reports):
    from telegram.ext import CommandHandler
    import telegram_bot.main as main

    commands = {
        command: handler.callback
        for group in main.application.handlers.values()
        for handler in group
        if isinstance(handler, CommandHandler)
        for command in handler.commands
    }
    assert commands["status"] is reports.status_command
    assert commands["health_report"] is reports.health_report_command


@pytest.mark.parametrize("origin", ["inline", "command"])
@pytest.mark.parametrize(
    "entry",
    [
        "status_reports",
        "health_report",
        "reports_menu",
        "monitoring_menu",
        "monitoring_details",
        "report_connection",
        "/status",
        "/health_report",
    ],
)
async def test_status_navigation_retires_pending_mutation(dispatcher, reports, monkeypatch, origin, entry):
    monkeypatch.setattr(reports, "collect_report_facts", AsyncMock(return_value=facts(reports)))
    if origin == "inline":
        await dispatcher.click("custom_limit:private_user")
    else:
        await dispatcher.text("/set_special_limit")
        await dispatcher.text("private_user")
    if entry.startswith("/"):
        await dispatcher.text(entry)
    else:
        await dispatcher.click(entry)
    card = plain(dispatcher.messages[-1])
    assert "Limiter status" in card or "Aggregate health report" in card
    assert "private_user" not in card
    await dispatcher.text("3")
    assert dispatcher.limits == {}
    assert "input_owner" not in dispatcher.app.user_data[7]
    assert "waiting_for" not in dispatcher.app.user_data[7]


@pytest.mark.parametrize("entry", ["status_reports", "health_report", "reports_menu", "report_connection", "/status"])
async def test_denied_status_navigation_never_reads_health(dispatcher, reports, monkeypatch, entry):
    async def forbidden_read():
        pytest.fail("denied route read aggregate health telemetry")

    monkeypatch.setattr(reports, "collect_report_facts", forbidden_read)
    dispatcher.admins.clear()
    if entry.startswith("/"):
        await dispatcher.text(entry)
    else:
        await dispatcher.click(entry)
    assert "permission" in dispatcher.messages[-1].lower()


@pytest.mark.parametrize("name", ["status_command", "health_report_command", "legacy_report_command"])
async def test_direct_status_entry_retires_native_owner(dispatcher, reports, monkeypatch, name):
    from telegram import Update
    from telegram.ext import ContextTypes

    monkeypatch.setattr(reports, "collect_report_facts", AsyncMock(return_value=facts(reports)))
    await dispatcher.text("/set_special_limit")
    await dispatcher.text("private_user")
    update = Update.de_json(
        {
            "update_id": 200,
            "message": {
                "message_id": 200,
                "date": 1,
                "from": {"id": 7, "is_bot": False, "first_name": "Admin"},
                "chat": {"id": -100, "type": "supergroup"},
                "text": "/status",
            },
        },
        dispatcher.app.bot,
    )
    context = ContextTypes.DEFAULT_TYPE.from_update(update, dispatcher.app)
    assert await getattr(reports, name)(update, context) == ConversationHandler.END
    assert "private_user" not in plain(dispatcher.messages[-1])
    await dispatcher.text("3")
    assert dispatcher.limits == {}
