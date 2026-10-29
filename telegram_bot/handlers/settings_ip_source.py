"""
IP source: SSE node logs or the panel's online-stats API.

Switching to API mode is guarded by a live pre-flight, because without the
``nodes:stats`` permission every per-user lookup would fail and enforcement
would quietly stop. The menu also surfaces the last collection cycle, which is
the only way to tell a healthy cycle from a skipped one.
"""

import time
from html import escape

from utils.log_observations import LOG_OBSERVATIONS

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from telegram_bot.constants import CallbackData
from utils.device_count import COUNT_MODE_INBOUND
from utils.read_config import normalize_api_ip_freshness, normalize_min_coverage, read_config, save_config_value


def _build_panel_data(config_data: dict):
    """Build a PanelType from the stored panel configuration."""
    from utils.types import PanelType

    return PanelType.from_config(config_data)


def create_ip_source_keyboard(current_source: str, concurrency: int, freshness: int = 60):
    """Create the IP source selection keyboard with the active mode marked."""
    logs_prefix = "✅" if current_source == "logs" else "⬜"
    api_prefix = "✅" if current_source == "api" else "⬜"
    keyboard = [
        [InlineKeyboardButton(f"{logs_prefix} 📜 Node Logs (SSE)", callback_data=CallbackData.IP_SOURCE_SET_LOGS)],
        [InlineKeyboardButton(f"{api_prefix} 🛰️ Panel API", callback_data=CallbackData.IP_SOURCE_SET_API)],
        [
            InlineKeyboardButton(
                f"⚡ API Concurrency: {concurrency}", callback_data=CallbackData.IP_SOURCE_SET_CONCURRENCY
            )
        ],
        [
            InlineKeyboardButton(
                f"🕒 Last seen (API + Logs): {freshness}s", callback_data=CallbackData.IP_SOURCE_SET_FRESHNESS
            )
        ],
        [InlineKeyboardButton("📊 Last API Cycle", callback_data=CallbackData.IP_SOURCE_STATS)],
        [InlineKeyboardButton("« Back to Settings", callback_data=CallbackData.SETTINGS_MENU)],
    ]
    return InlineKeyboardMarkup(keyboard)


def _build_ip_source_text(config_data: dict) -> str:
    """Render the IP source menu body for the current configuration."""
    current_source = str(config_data.get("ip_source") or "logs")
    interval = config_data.get("check_interval", 60)
    freshness = normalize_api_ip_freshness(config_data.get("api_ip_freshness"))
    # normalize_min_coverage rather than "or 0.0": an absent key means the documented
    # default, and the old fallback would have shown 0% on this screen while the
    # collector was actually enforcing 80%.
    coverage = normalize_min_coverage(config_data.get("api_ip_min_coverage"))
    auto_fallback = "✅ on" if config_data.get("api_ip_auto_fallback", True) else "❌ off"

    if current_source == "api":
        active = "🛰️ <b>Panel API</b>"
        active_note = (
            "IPs are pulled from <code>/api/node/online_stats/{id}/ip</code> once "
            "per check cycle, with targeted refreshes before punishment when needed. "
            "The SSE log streams are parked while this mode is active."
        )
    else:
        active = "📜 <b>Node Logs (SSE)</b>"
        active_note = "IPs use the original Xray log timestamp and explicit node process timezones."
    logs_reason = LOG_OBSERVATIONS.unavailable_reason()
    logs_readiness = escape(logs_reason) if logs_reason else "ready"

    return (
        "🛰️ <b>IP Source</b>\n\n"
        f"<b>Active:</b> {active}\n"
        f"<i>{active_note}</i>\n\n"
        f"<b>Logs evidence:</b> {logs_readiness}\n"
        "Set <code>LOG_NODE_TIMEZONES</code> (or explicit <code>LOG_TIMEZONE</code>) for Xray containers. "
        "Missing zones, incomplete fleet or replay uncertainty preserve warning history. "
        "After source/settings changes or transport gaps, Logs needs a complete observation warm-up "
        "of <code>max(Check interval, Last seen)</code>.\n\n"
        "<b>Observation timing:</b>\n"
        f"• Last seen: <code>{freshness}s</code> for API and Logs\n"
        "• API: each user's request supplies one scan observation\n"
        "• Logs: original event age is checked at evaluation\n"
        "• Independent of <code>CHECK_INTERVAL</code>\n"
        "• Does not measure ongoing byte traffic\n\n"
        "<b>API mode settings:</b>\n"
        f"• Check interval: <code>{interval}s</code>\n"
        "• Candidates: active users with <code>online=true</code> and Group Filter\n"
        f"• Min coverage: <code>{coverage:.0%}</code>\n"
        f"• Auto-fallback to logs: {auto_fallback}\n\n"
        "<b>API observations:</b>\n"
        "• Xray 26.4.13+: open connections in the response count even with old opening timestamps.\n"
        "• WireGuard and older/unknown Xray: activity age is checked at that user's request.\n"
        "• Collection delays do not empty earlier users' scan results. Before punishment, "
        "observations older than Last seen are refreshed for that user.\n"
        "• Unknown user/node evidence preserves that user's warning record. "
        "Valid empty or expired observations can clear it.\n"
        "• Inbound protocol is not exposed by the API, so per-inbound CDN grouping "
        "is inactive (use <b>CDN Nodes</b> instead).\n"
        "• Requires the <code>nodes:stats</code> panel permission."
    )


async def handle_ip_source_menu_callback(query, _context: ContextTypes.DEFAULT_TYPE):
    """Show the IP source selection menu."""
    config_data = await read_config()
    _context.user_data.pop("waiting_for", None)
    current_source = str(config_data.get("ip_source") or "logs")
    concurrency = int(config_data.get("api_ip_concurrency") or 20)

    try:
        await query.edit_message_text(
            text=_build_ip_source_text(config_data),
            reply_markup=create_ip_source_keyboard(current_source, concurrency, config_data["api_ip_freshness"]),
            parse_mode="HTML",
        )
    except BadRequest as e:
        if "message is not modified" in str(e).lower():
            await query.answer("🔄 Already up to date")
        else:
            raise


async def handle_ip_source_set_logs_callback(query, context: ContextTypes.DEFAULT_TYPE):
    """Switch the IP source back to SSE log streaming."""
    config_data = await read_config()
    if str(config_data.get("ip_source") or "logs") == "logs":
        await query.answer("Already using node logs")
        return

    if not await save_config_value("ip_source", "logs"):
        await query.answer("❌ Could not save the IP source. Please try again.", show_alert=True)
        return
    await query.answer("✅ IP source: node logs (SSE)")
    await handle_ip_source_menu_callback(query, context)


async def _find_preflight_user_id(panel_data, config_data) -> tuple[int, str]:
    """
    Pick one real user id to probe the online-stats endpoint with.

    Each query requests at most one user. Only a confirmed empty online
    list falls back to the wider active-user query.

    Returns:
        ``(user_id, error)`` — ``user_id`` is ``0`` when no probe target could
        be resolved, in which case ``error`` explains why.
    """
    from utils.ip_source_api import resolve_monitored_group_ids
    from utils.panel_api.request_helper import panel_get

    group_ids = resolve_monitored_group_ids(config_data)
    query_error = (0, "Could not query the panel user list.")
    for online in (True, False):
        params = {"offset": 0, "limit": 1, "status": "active"}
        if group_ids:
            params["group"] = group_ids
        if online:
            params["online"] = "true"
        try:
            response = await panel_get(panel_data, "/api/users", params=params, timeout=60.0, max_retries=3)
            if response is None or response.status_code != 200:
                return query_error
            data = response.json()
        except Exception:
            return query_error
        if not isinstance(data, dict) or not isinstance(data.get("users"), list):
            return query_error
        total = data.get("total")
        if type(total) is not int or total < 0 or len(data["users"]) != min(1, total):
            return query_error
        if not data["users"]:
            continue
        raw = data["users"][0]
        if (
            not isinstance(raw, dict)
            or type(raw.get("id")) is not int
            or raw["id"] <= 0
            or not isinstance(raw.get("username"), str)
            or not raw["username"].strip()
        ):
            return query_error
        return raw["id"], ""
    return 0, "No active user was found to probe the endpoint with."


async def handle_ip_source_set_api_callback(query, context: ContextTypes.DEFAULT_TYPE):
    """
    Switch the IP source to the panel API, but only after a live pre-flight.

    Without the ``nodes:stats`` permission every per-user lookup would fail and
    enforcement would silently stop, so the switch is refused unless the
    endpoint actually answers.
    """
    config_data = await read_config()
    if config_data.get("device_count_mode") == COUNT_MODE_INBOUND:
        await query.answer("⚠️ Per Inbound requires node logs; select Per Device, Per IP or Per Node first")
        return
    if str(config_data.get("ip_source") or "logs") == "api":
        await query.answer("Already using the panel API")
        return

    await query.edit_message_text(
        text="🛰️ <b>Checking panel API access...</b>\n\n"
        "<i>Probing /api/node/online_stats — this takes a few seconds.</i>",
        parse_mode="HTML",
    )

    from utils.panel_api.online_ips import check_online_stats_permission

    try:
        panel_data = _build_panel_data(config_data)
        user_id, probe_error = await _find_preflight_user_id(panel_data, config_data)
        if not user_id:
            ok, reason = False, probe_error
        else:
            ok, reason = await check_online_stats_permission(panel_data, user_id)
    except Exception as error:  # pylint: disable=broad-except
        ok, reason = False, f"Pre-flight failed: {error}"

    if not ok:
        await query.edit_message_text(
            text="❌ <b>Cannot switch to Panel API</b>\n\n"
            f"{reason}\n\n"
            "The IP source is unchanged and node logs keep being used.",
            reply_markup=InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("🔄 Retry", callback_data=CallbackData.IP_SOURCE_SET_API)],
                    [InlineKeyboardButton("« Back", callback_data=CallbackData.IP_SOURCE_MENU)],
                ]
            ),
            parse_mode="HTML",
        )
        return

    if not await save_config_value("ip_source", "api"):
        await query.answer("❌ Could not save the IP source. Please try again.", show_alert=True)
        return
    await query.answer("✅ IP source: panel API")
    await handle_ip_source_menu_callback(query, context)


async def handle_ip_source_concurrency_callback(query, context: ContextTypes.DEFAULT_TYPE):
    """Prompt for the API fan-out concurrency value."""
    config_data = await read_config()
    current = int(config_data.get("api_ip_concurrency") or 20)

    await query.edit_message_text(
        text="⚡ <b>API Concurrency</b>\n\n"
        f"Current: <code>{current}</code>\n\n"
        "How many per-user online-IP requests may run at once during a "
        "collection cycle.\n\n"
        "• Allowed range: <code>1</code> – <code>40</code>\n"
        "• Higher = faster cycles, more panel load\n"
        "• The shared HTTP pool holds 50 connections, so values above "
        "<code>40</code> only cause queueing\n\n"
        "Send the number, or /cancel to abort.",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("« Cancel", callback_data=CallbackData.IP_SOURCE_MENU)]]
        ),
        parse_mode="HTML",
    )
    context.user_data["waiting_for"] = "ip_source_concurrency"


async def ip_source_concurrency_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle the text input for the API concurrency value."""
    text = (update.message.text or "").strip()
    config_data = await read_config()
    current_source = str(config_data.get("ip_source") or "logs")

    if text.lower() in ("/cancel", "cancel"):
        await update.message.reply_html(
            "❌ Cancelled.",
            reply_markup=create_ip_source_keyboard(
                current_source,
                int(config_data.get("api_ip_concurrency") or 20),
                normalize_api_ip_freshness(config_data.get("api_ip_freshness")),
            ),
        )
        return

    try:
        value = int(text)
    except ValueError:
        context.user_data["waiting_for"] = "ip_source_concurrency"
        return await update.message.reply_html(
            "❌ Please send a whole number between <code>1</code> and <code>40</code>."
        )

    clamped = max(1, min(40, value))
    await save_config_value("api_ip_concurrency", str(clamped))

    note = "" if clamped == value else f"\n\n<i>Clamped from {value} to the allowed range.</i>"
    await update.message.reply_html(
        f"✅ API concurrency set to <code>{clamped}</code>.{note}",
        reply_markup=create_ip_source_keyboard(
            current_source, clamped, normalize_api_ip_freshness(config_data.get("api_ip_freshness"))
        ),
    )


async def handle_ip_source_freshness_callback(query, context: ContextTypes.DEFAULT_TYPE):
    """Prompt for an exact Last seen threshold in seconds."""
    config_data = await read_config()
    current = normalize_api_ip_freshness(config_data.get("api_ip_freshness"))
    await query.edit_message_text(
        text=(
            "🕒 <b>Last seen</b>\n\n"
            f"Current: <code>{current}s</code>\n\n"
            "API keeps each user's request result for its scan and refreshes old observations "
            "before punishment. Supported Xray cores count open connections; WireGuard and "
            "older/unknown Xray use activity age at request time. "
            "Logs counts original events younger than this value at evaluation. "
            "Independent of <code>CHECK_INTERVAL</code>; does not measure ongoing byte traffic.\n\n"
            "Send a positive whole number of seconds, or /cancel."
        ),
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("« Cancel", callback_data=CallbackData.IP_SOURCE_MENU)]]
        ),
        parse_mode="HTML",
    )
    context.user_data["waiting_for"] = "ip_source_freshness"


async def ip_source_freshness_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Persist exact positive seconds; retain the prompt when validation/save fails."""
    text = (update.message.text or "").strip()
    config_data = await read_config()
    source = str(config_data.get("ip_source") or "logs")
    concurrency = int(config_data.get("api_ip_concurrency") or 20)
    context.user_data.pop("waiting_for", None)
    if text.lower() in ("/cancel", "cancel"):
        await update.message.reply_html(
            "❌ Cancelled.",
            reply_markup=create_ip_source_keyboard(
                source, concurrency, normalize_api_ip_freshness(config_data.get("api_ip_freshness"))
            ),
        )
        return
    if not text.isascii() or not text.isdecimal() or int(text) <= 0:
        context.user_data["waiting_for"] = "ip_source_freshness"
        return await update.message.reply_html("❌ Send a positive whole number of seconds.")
    value = int(text)
    if not await save_config_value("api_ip_freshness", str(value)):
        context.user_data["waiting_for"] = "ip_source_freshness"
        return await update.message.reply_html("❌ Could not save Last seen. Try again or /cancel.")
    await update.message.reply_html(
        f"✅ Last seen for API and Logs set to <code>{value}s</code>.",
        reply_markup=create_ip_source_keyboard(source, concurrency, value),
    )


async def handle_ip_source_stats_callback(query, _context: ContextTypes.DEFAULT_TYPE):
    """Show the statistics of the most recent API collection cycle."""
    from utils.ip_source_api import format_api_evidence_summary, get_last_cycle_stats

    config_data = await read_config()
    stats = get_last_cycle_stats()
    ran_at = float(stats.get("ran_at") or 0)

    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🔄 Refresh", callback_data=CallbackData.IP_SOURCE_STATS)],
            [InlineKeyboardButton("« Back", callback_data=CallbackData.IP_SOURCE_MENU)],
        ]
    )

    if not ran_at:
        hint = (
            "No collection cycle has run yet."
            if str(config_data.get("ip_source") or "logs") == "api"
            else "API mode is not active, so no cycle has run."
        )
        try:
            await query.edit_message_text(
                text=f"📊 <b>Last API Cycle</b>\n\n{hint}", reply_markup=keyboard, parse_mode="HTML"
            )
        except BadRequest as e:
            if "message is not modified" not in str(e).lower():
                raise
            await query.answer("🔄 Already up to date")
        return

    age = max(0, int(time.time() - ran_at))
    age_label = f"{age}s ago" if age < 120 else f"{age // 60}m ago"
    skipped = stats.get("skipped_reason") or ""
    outcome = stats.get("collection_outcome")
    fallback = stats.get("fallback_applied", False)
    if skipped:
        verdict = f"⚠️ Cycle skipped — {escape(str(skipped))}"
    elif fallback:
        verdict = "⚠️ Cycle skipped — IP source reverted to Logs"
    elif outcome in {"success", "partial"}:
        verdict = "✅ Sample available for enforcement"
    else:
        verdict = "⚠️ Collection unavailable or still in progress; no usable sample recorded"
    if not skipped and not fallback and outcome == "partial":
        verdict += f" ({stats.get('unknown_users', 0)} users protected: incomplete evidence)"

    dropped = [f"{stats.get('stale_ips', 0)} stale"]
    if stats.get("unknown_age_ips"):
        dropped.append(f"{stats['unknown_age_ips']} undatable")
    if stats.get("future_ips"):
        dropped.append(f"{stats['future_ips']} future-dated")
    historical = "Historical API collection; current source is Logs.\n" if config_data.get("ip_source") != "api" else ""

    text = (
        "📊 <b>Last API Cycle</b>\n\n"
        f"{historical}"
        f"<b>Ran:</b> {age_label} in <code>{stats.get('duration_ms', 0)}ms</code>\n"
        f"{verdict}\n\n"
        f"Consecutive incomplete API collections: <code>{stats.get('consecutive_unsuccessful_cycles', 0)}</code>\n\n"
        "<b>Candidate narrowing:</b>\n"
        f"• From panel: <code>{stats.get('candidates', 0)}</code>\n"
        f"• After local filters: <code>{stats.get('prefiltered', 0)}</code>\n\n"
        "<b>Per-user lookups:</b>\n"
        f"• OK: <code>{stats.get('fetched_ok', 0)}</code>\n"
        f"• Failed: <code>{stats.get('fetched_failed', 0)}</code>\n"
        f"• Timeouts (in failed): <code>{stats.get('fetched_timeouts', 0)}</code>\n"
        f"• Unsent/deferred: <code>{stats.get('fetched_deferred', 0)}</code>\n"
        f"• Not found (404): <code>{stats.get('not_found', 0)}</code>\n"
        f"• Forbidden (403): <code>{stats.get('forbidden', 0)}</code>\n"
        f"• Coverage: <code>{float(stats.get('coverage') or 0):.1%}</code>\n\n"
        "<b>Evidence:</b>\n"
        f"{format_api_evidence_summary(stats)}\n"
        "HTTP success does not confirm complete node evidence for each user.\n\n"
        "<b>Result:</b>\n"
        f"• Users with IPs: <code>{stats.get('users_with_ips', 0)}</code>\n"
        f"• Accepted IPs: <code>{stats.get('total_ips', 0)}</code>\n"
        f"• IPs dropped: <code>{', '.join(dropped)}</code>\n"
        f"• Geo lookups: <code>{stats.get('geo_lookups', 0)}</code>"
    )

    try:
        await query.edit_message_text(text=text, reply_markup=keyboard, parse_mode="HTML")
    except BadRequest as e:
        if "message is not modified" not in str(e).lower():
            raise
        await query.answer("🔄 No new cycle yet")
