"""
IP source: SSE node logs or the panel's online-stats API.

Switching to API mode is guarded by a live pre-flight, because without the
``nodes:stats`` permission every per-user lookup would fail and enforcement
would quietly stop. The menu also surfaces the last collection cycle, which is
the only way to tell a healthy cycle from a skipped one.
"""

import time

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from telegram_bot.constants import CallbackData
from utils.read_config import read_config, save_config_value


def _build_panel_data(config_data: dict):
    """Build a PanelType from the stored panel configuration."""
    from utils.types import PanelType

    panel_config = config_data.get("panel", {})
    return PanelType(
        panel_config.get("username", ""),
        panel_config.get("password", ""),
        panel_config.get("domain", ""),
    )


def create_ip_source_keyboard(current_source: str, concurrency: int):
    """Create the IP source selection keyboard with the active mode marked."""
    logs_prefix = "✅" if current_source == "logs" else "⬜"
    api_prefix = "✅" if current_source == "api" else "⬜"
    keyboard = [
        [InlineKeyboardButton(
            f"{logs_prefix} 📜 Node Logs (SSE)",
            callback_data=CallbackData.IP_SOURCE_SET_LOGS
        )],
        [InlineKeyboardButton(
            f"{api_prefix} 🛰️ Panel API",
            callback_data=CallbackData.IP_SOURCE_SET_API
        )],
        [InlineKeyboardButton(
            f"⚡ API Concurrency: {concurrency}",
            callback_data=CallbackData.IP_SOURCE_SET_CONCURRENCY
        )],
        [InlineKeyboardButton(
            "📊 Last API Cycle",
            callback_data=CallbackData.IP_SOURCE_STATS
        )],
        [InlineKeyboardButton("« Back to Settings", callback_data=CallbackData.SETTINGS_MENU)],
    ]
    return InlineKeyboardMarkup(keyboard)


def _build_ip_source_text(config_data: dict) -> str:
    """Render the IP source menu body for the current configuration."""
    current_source = str(config_data.get("ip_source") or "logs")
    candidate_mode = str(config_data.get("api_ip_candidate_mode") or "online")
    window = int(config_data.get("api_ip_online_window") or 0)
    interval = config_data.get("check_interval") or config_data.get(
        "monitoring", {}
    ).get("check_interval", 60)
    try:
        interval = int(float(interval))
    except (ValueError, TypeError):
        interval = 60
    window_label = (
        f"auto ({max(60, interval + 30)}s = interval + 30s)" if window == 0 else f"{window}s"
    )
    freshness = int(config_data.get("api_ip_freshness") or 0)
    freshness_label = (
        f"auto ({max(60, interval)}s = check interval)" if freshness == 0 else f"{freshness}s"
    )
    coverage = float(config_data.get("api_ip_min_coverage") or 0.0)
    auto_fallback = "✅ on" if config_data.get("api_ip_auto_fallback", True) else "❌ off"

    if current_source == "api":
        active = "🛰️ <b>Panel API</b>"
        active_note = (
            "IPs are pulled from <code>/api/node/online_stats/{id}/ip</code> once "
            "per check cycle. The SSE log streams are parked while this mode is active."
        )
    else:
        active = "📜 <b>Node Logs (SSE)</b>"
        active_note = (
            "IPs are streamed continuously from each node's log endpoint, which is "
            "the original behaviour."
        )

    return (
        "🛰️ <b>IP Source</b>\n\n"
        f"<b>Active:</b> {active}\n"
        f"<i>{active_note}</i>\n\n"
        "<b>API mode settings:</b>\n"
        f"• Check interval: <code>{interval}s</code>\n"
        f"• Candidate mode: <code>{candidate_mode}</code>\n"
        f"• Online window: <code>{window_label}</code>\n"
        f"• IP freshness: <code>{freshness_label}</code>\n"
        f"• Min coverage: <code>{coverage:.0%}</code>\n"
        f"• Auto-fallback to logs: {auto_fallback}\n\n"
        "<b>Trade-offs of API mode:</b>\n"
        "• A cycle is an <b>instant snapshot</b>, not the union of a whole interval, "
        "so it reports fewer violations — never more.\n"
        "• The panel keeps every IP with its last-seen timestamp, so IPs older "
        "than the freshness window above are dropped; without that a user who "
        "changed network looks like several devices.\n"
        "• A cycle with too few successful lookups is <b>skipped entirely</b>, "
        "keeping consecutive-violation counters intact.\n"
        "• Inbound protocol is not exposed by the API, so per-inbound CDN grouping "
        "is inactive (use <b>CDN Nodes</b> instead).\n"
        "• Requires the <code>nodes:stats</code> panel permission."
    )


async def handle_ip_source_menu_callback(query, _context: ContextTypes.DEFAULT_TYPE):
    """Show the IP source selection menu."""
    config_data = await read_config()
    current_source = str(config_data.get("ip_source") or "logs")
    concurrency = int(config_data.get("api_ip_concurrency") or 20)

    try:
        await query.edit_message_text(
            text=_build_ip_source_text(config_data),
            reply_markup=create_ip_source_keyboard(current_source, concurrency),
            parse_mode="HTML"
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

    await save_config_value("ip_source", "logs")
    await query.answer("✅ IP source: node logs (SSE)")
    await handle_ip_source_menu_callback(query, context)


async def _find_preflight_user_id(panel_data, config_data) -> tuple[int, str]:
    """
    Pick one real user id to probe the online-stats endpoint with.

    Online users are queried first because that response is tiny; only if
    nobody is currently online does it fall back to the wider query.

    Returns:
        ``(user_id, error)`` — ``user_id`` is ``0`` when no probe target could
        be resolved, in which case ``error`` explains why.
    """
    from utils.ip_source_api import resolve_monitored_group_ids
    from utils.panel_api.online_ips import fetch_online_candidates

    group_ids = resolve_monitored_group_ids(config_data)
    for window in (110, 0):
        candidates = await fetch_online_candidates(
            panel_data,
            group_ids=group_ids,
            status="active",
            online_window=window,
            page_size=100 if window else 500,
        )
        if candidates is None:
            return 0, "Could not query the panel user list."
        for raw in candidates:
            try:
                user_id = int(raw.get("id"))
            except (ValueError, TypeError):
                continue
            if user_id > 0:
                return user_id, ""
    return 0, "No active user was found to probe the endpoint with."


async def handle_ip_source_set_api_callback(query, context: ContextTypes.DEFAULT_TYPE):
    """
    Switch the IP source to the panel API, but only after a live pre-flight.

    Without the ``nodes:stats`` permission every per-user lookup would fail and
    enforcement would silently stop, so the switch is refused unless the
    endpoint actually answers.
    """
    config_data = await read_config()
    if str(config_data.get("ip_source") or "logs") == "api":
        await query.answer("Already using the panel API")
        return

    await query.edit_message_text(
        text="🛰️ <b>Checking panel API access...</b>\n\n"
             "<i>Probing /api/node/online_stats — this takes a few seconds.</i>",
        parse_mode="HTML"
    )

    from utils.panel_api.online_ips import check_online_stats_permission

    panel_data = _build_panel_data(config_data)
    try:
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
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Retry", callback_data=CallbackData.IP_SOURCE_SET_API)],
                [InlineKeyboardButton("« Back", callback_data=CallbackData.IP_SOURCE_MENU)],
            ]),
            parse_mode="HTML"
        )
        return

    await save_config_value("ip_source", "api")
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
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("« Cancel", callback_data=CallbackData.IP_SOURCE_MENU)]
        ]),
        parse_mode="HTML"
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
                current_source, int(config_data.get("api_ip_concurrency") or 20)
            )
        )
        return

    try:
        value = int(text)
    except ValueError:
        await update.message.reply_html(
            "❌ Please send a whole number between <code>1</code> and <code>40</code>."
        )
        context.user_data["waiting_for"] = "ip_source_concurrency"
        return

    clamped = max(1, min(40, value))
    await save_config_value("api_ip_concurrency", str(clamped))

    note = "" if clamped == value else f"\n\n<i>Clamped from {value} to the allowed range.</i>"
    await update.message.reply_html(
        f"✅ API concurrency set to <code>{clamped}</code>.{note}",
        reply_markup=create_ip_source_keyboard(current_source, clamped)
    )


async def handle_ip_source_stats_callback(query, _context: ContextTypes.DEFAULT_TYPE):
    """Show the statistics of the most recent API collection cycle."""
    from utils.ip_source_api import get_last_cycle_stats

    config_data = await read_config()
    stats = get_last_cycle_stats()
    ran_at = float(stats.get("ran_at") or 0)

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Refresh", callback_data=CallbackData.IP_SOURCE_STATS)],
        [InlineKeyboardButton("« Back", callback_data=CallbackData.IP_SOURCE_MENU)],
    ])

    if not ran_at:
        hint = (
            "No collection cycle has run yet."
            if str(config_data.get("ip_source") or "logs") == "api"
            else "API mode is not active, so no cycle has run."
        )
        try:
            await query.edit_message_text(
                text=f"📊 <b>Last API Cycle</b>\n\n{hint}",
                reply_markup=keyboard,
                parse_mode="HTML"
            )
        except BadRequest as e:
            if "message is not modified" not in str(e).lower():
                raise
            await query.answer("🔄 Already up to date")
        return

    age = max(0, int(time.time() - ran_at))
    age_label = f"{age}s ago" if age < 120 else f"{age // 60}m ago"
    skipped = stats.get("skipped_reason") or ""
    verdict = f"⚠️ Cycle skipped — {skipped}" if skipped else "✅ Enforcement ran on this cycle"

    text = (
        "📊 <b>Last API Cycle</b>\n\n"
        f"<b>Ran:</b> {age_label} in <code>{stats.get('duration_ms', 0)}ms</code>\n"
        f"{verdict}\n\n"
        "<b>Candidate narrowing:</b>\n"
        f"• From panel: <code>{stats.get('candidates', 0)}</code>\n"
        f"• After local filters: <code>{stats.get('prefiltered', 0)}</code>\n\n"
        "<b>Per-user lookups:</b>\n"
        f"• OK: <code>{stats.get('fetched_ok', 0)}</code>\n"
        f"• Failed: <code>{stats.get('fetched_failed', 0)}</code>\n"
        f"• Not found (404): <code>{stats.get('not_found', 0)}</code>\n"
        f"• Forbidden (403): <code>{stats.get('forbidden', 0)}</code>\n"
        f"• Coverage: <code>{float(stats.get('coverage') or 0):.1%}</code>\n\n"
        "<b>Result:</b>\n"
        f"• Users with IPs: <code>{stats.get('users_with_ips', 0)}</code>\n"
        f"• Accepted IPs: <code>{stats.get('total_ips', 0)}</code>\n"
        f"• Stale IPs dropped: <code>{stats.get('stale_ips', 0)}</code>\n"
        f"• Nodes seen: <code>{stats.get('nodes_seen', 0)}</code>\n"
        f"• Geo lookups: <code>{stats.get('geo_lookups', 0)}</code>"
    )

    try:
        await query.edit_message_text(text=text, reply_markup=keyboard, parse_mode="HTML")
    except BadRequest as e:
        if "message is not modified" not in str(e).lower():
            raise
        await query.answer("🔄 No new cycle yet")
