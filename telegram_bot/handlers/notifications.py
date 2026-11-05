"""Private, revision-guarded management of grouped Mirza Bot notifications."""

import base64
from contextlib import suppress
import hashlib
import html
import re
from time import monotonic
import unicodedata
import uuid

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from db.crud.mirza_notifications import MirzaNotificationsCRUD
from db.crud.mirza_routes import MirzaRouteError
from telegram_bot.constants import CallbackData
from telegram_bot.handlers.admin import check_admin_privilege
from telegram_bot.input_owner import accepts_inline, end_input
from telegram_bot.utils import send_response
from utils import mirza_settings as settings
from utils.mirza_client import MirzaClient, MirzaResult
from utils.settings_service import read_settings

INPUT_FLOW = "mirza_notification"


def _preview(value: str, units: int = 240) -> str:
    """Bound a display preview before escaping, preserving stored text verbatim."""
    encoded = value.encode("utf-16-le")
    if len(encoded) > units * 2:
        value = encoded[: units * 2].decode("utf-16-le", errors="ignore") + "…"
    return html.escape(value)


def _button(label: str, action: str, route: dict | None = None, extra: int | None = None) -> InlineKeyboardButton:
    payload = f"mn:{action}"
    if route is not None:
        payload += f":{route['id']}:{route['revision']}"
    if extra is not None:
        payload += f":{extra}"
    return InlineKeyboardButton(label, callback_data=payload)


def _back(route: dict | None = None) -> InlineKeyboardMarkup:
    rows = [[_button("« Back to Bot", "r", route)]] if route else []
    rows.extend(
        [
            [InlineKeyboardButton("« Back to Notifications", callback_data=CallbackData.NOTIFICATIONS_MENU)],
            [InlineKeyboardButton("« Back to Settings", callback_data=CallbackData.SETTINGS_MENU)],
        ]
    )
    return InlineKeyboardMarkup(rows)


async def _respond(update: Update, text: str, keyboard: InlineKeyboardMarkup):
    if update.callback_query:
        return await send_response(update, text, keyboard)
    # The router needs the returned Message to bind a replacement prompt.
    return await update.message.reply_text(text, reply_markup=keyboard, parse_mode="HTML")


async def _authorized(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if await check_admin_privilege(update) is not None:
        end_input(context)
        return False
    if not update.effective_chat or update.effective_chat.type != "private":
        end_input(context)
        await _respond(update, "🔒 Open Notifications in a private chat with this bot.", _back())
        return False
    return True


async def _list(update: Update, notice: str = ""):
    from db.database import AsyncSessionLocal

    routes = await settings.list_routes()
    async with AsyncSessionLocal() as db:
        counts = await MirzaNotificationsCRUD.counts(db)
    rows = [[_button(f"🤖 {route['name'][:80]} {'✅' if route['enabled'] else '⏸'}", "r", route)] for route in routes]
    rows.extend(
        [
            [_button("➕ Add Bot", "b")],
            [InlineKeyboardButton("« Back to Settings", callback_data=CallbackData.SETTINGS_MENU)],
        ]
    )
    statuses = "\n".join(f"{state.title()}: {count}" for state, count in counts.items())
    return await _respond(
        update,
        f"🔔 <b>Notifications</b>\n\n{notice}"
        f"Choose a Mirza Bot to manage its settings and included admins.\n"
        f"Bots: {len(routes)}\n\n{statuses}",
        InlineKeyboardMarkup(rows),
    )


async def _route(update: Update, route: dict, notice: str = ""):
    ready = sum(_ready(message) for message in route["messages"])
    text = (
        f"🤖 <b>{_preview(route['name'], 80)}</b>\n\n{notice}"
        f"Status: {'Enabled' if route['enabled'] else 'Disabled'}\n"
        f"Included admins: {len(route['admin_usernames'])}\n"
        f"URL: <code>{_preview(route['base_url'], 160) or 'Missing'}</code>\n"
        f"API key: {'Configured' if route['key_configured'] else 'Missing'}\n"
        f"Messages: {len(route['messages'])} · ready: {ready}\n\n"
        "Test Connection sends <code>test</code> to your own account through this Mirza Bot. Start it first."
    )
    rows = [
        [_button("⏸ Disable" if route["enabled"] else "✅ Enable", "e", route)],
        [_button("👤 Admins", "g", route), _button("✏️ Rename", "n", route)],
        [_button("🌐 URL", "u", route), _button("🔑 Replace API Key", "k", route)],
        [_button("🔌 Test Connection", "c", route)],
        [_button("💬 Messages", "h", route)],
        [_button("🗑 Delete", "d", route)],
        [InlineKeyboardButton("« Back to Notifications", callback_data=CallbackData.NOTIFICATIONS_MENU)],
    ]
    return await _respond(update, text, InlineKeyboardMarkup(rows))


def _test_notice(result: MirzaResult) -> str:
    """Present only fixed descriptions, never remote bodies or exception text."""
    if result.status == "delivered":
        return "✅ Mirza Bot accepted the test message. Check your chat with that bot.\n\n"
    return {
        "message_rejected": "⚠️ Mirza Bot rejected the test. Start or unblock that bot, then try again.\n\n",
        "request_rejected": "⚠️ Test rejected. Check the API URL, key and users permission.\n\n",
        "connection_unavailable": "⚠️ Could not connect to Mirza Bot. Check the URL, server and TLS certificate.\n\n",
        "rate_limited": "⚠️ Mirza Bot is rate limiting requests. Try again later.\n\n",
    }.get(
        result.reason,
        "⚠️ Test result is uncertain. Check your chat with this Mirza Bot before testing again; "
        "the message may already have arrived.\n\n",
    )


async def _test_alert(update: Update, text: str, *, show_alert: bool = False):
    # A callback acknowledgement must not replace a test receipt or trigger a new message.
    with suppress(Exception):
        return await update.callback_query.answer(text.strip(), show_alert=show_alert)


async def _test_result(update: Update, route: dict, notice: str):
    """Retain the send outcome even when refreshing the configuration fails."""
    try:
        current = await settings.get_route(route["id"])
    except Exception:
        return await _respond(update, notice + "Could not reload settings. Reopen Notifications.", _back())
    if current is None or current["revision"] != route["revision"]:
        return await _list(
            update,
            "⚠️ Configuration changed or was deleted during the test. "
            "This result applies to the previous settings.\n\n" + notice,
        )
    return await _route(update, current, notice)


async def _test_connection(update: Update, context: ContextTypes.DEFAULT_TYPE, route: dict):
    """Make one explicit test send to the pressing admin, outside the incident outbox."""
    user = update.effective_user
    if user is None or user.id <= 0:
        raise MirzaRouteError("Could not identify your Telegram account")
    # One small record per operator, independent of input-flow cleanup and bot count.
    state = context.user_data.setdefault("notification_test", {"running": False, "next_at": 0.0})
    if state["running"]:
        return await _test_alert(update, "A connection test is already in progress.")
    if monotonic() < state["next_at"]:
        return await _test_alert(update, "Please wait a few seconds before another test.")
    if not route["base_url"] or not route["key_configured"]:
        return await _route(update, route, "⚠️ Configure the URL and API key before testing.\n\n")
    state["running"] = True
    attempted = False
    try:
        await _route(update, route, "🔄 Testing connection; sending <code>test</code> to your Telegram account…\n\n")
        if not await _authorized(update, context):
            return
        # Read after the progress response so an edit during that await invalidates the test.
        base_url, key = await settings.get_test_credentials(route["id"], expected_revision=route["revision"])
        client = MirzaClient()
        result = MirzaResult("uncertain", "test_interrupted")
        try:
            attempted = True
            result = await client.send_message(base_url, key, str(user.id), "test")
        except Exception:
            # Unexpected failures after starting a request cannot justify resending it.
            pass
        finally:
            with suppress(Exception):
                await client.close()
        notice = _test_notice(result)
        try:
            return await _test_result(update, route, notice)
        except Exception:
            # Keep the known result if Telegram cannot edit the screen. Do not send a fallback message.
            return await _test_alert(update, notice, show_alert=True)
    finally:
        state["running"] = False
        if attempted:
            # Also suppress queued double-clicks after fast responses or cancellation.
            state["next_at"] = monotonic() + 5.0


def _compact_id(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _admin_token(username: str) -> str:
    # 96-bit identity plus compact UUID and full SQLite revision fits 64 bytes.
    # Resolve against exact names and refuse ambiguous matches, never list offsets.
    return _compact_id(hashlib.sha256(username.encode("utf-8")).digest()[:12])


async def _admin_choices(route: dict, *, force_refresh: bool = False) -> tuple[dict, dict, bool]:
    from utils.panel_api import get_admins
    from utils.read_config import read_config
    from utils.types import PanelType

    available = True
    try:
        panel = PanelType.from_config(await read_config())
        panel_admins = await get_admins(panel, force_refresh=force_refresh)
        if not isinstance(panel_admins, list):
            raise ValueError
    except Exception:
        available, panel_admins = False, []
    admins = {}
    for admin in panel_admins:
        if not isinstance(admin, dict):
            continue
        name = admin.get("username")
        if (
            isinstance(name, str)
            and 0 < len(name) <= 255
            and name == name.strip()
            and not any(unicodedata.category(char).startswith("C") for char in name)
        ):
            admins[name] = admin
    for name in route["admin_usernames"]:
        admins.setdefault(name, {"missing": True})
    assignments = {name: bot for bot in await settings.list_routes() for name in bot["admin_usernames"]}
    return admins, assignments, available


async def _admins(update: Update, route: dict, notice: str = "", *, force_refresh: bool = False):
    admins, assignments, available = await _admin_choices(route, force_refresh=force_refresh)
    rows = []
    included = set(route["admin_usernames"])
    for name, admin in admins.items():
        other = assignments.get(name)
        locked = other is not None and other["id"] != route["id"]
        prefix = "✅" if name in included else "🔒" if locked else "▫️"
        suffix = f" · {other['name'][:40]}" if locked else ""
        if admin.get("is_sudo"):
            suffix += " 👑"
        if admin.get("is_disabled"):
            suffix += " 🔒"
        if admin.get("missing"):
            suffix += " · not in panel list"
        payload = f"mn:i:{_compact_id(uuid.UUID(route['id']).bytes)}:{route['revision']}:{_admin_token(name)}"
        rows.append([InlineKeyboardButton(f"{prefix} {name[:80]}{suffix}", callback_data=payload)])
    rows.extend([[_button("🔄 Refresh", "f", route)], [_button("« Back to Bot", "r", route)]])
    panel_notice = ""
    if not available:
        panel_notice = "⚠️ Could not load admins from panel. Saved members remain available for removal.\n\n"
    elif not admins:
        panel_notice = "No admins found in the panel.\n\n"
    return await _respond(
        update,
        f"👤 <b>Admins · {_preview(route['name'], 80)}</b>\n\n{notice}{panel_notice}"
        "Tap an admin to include or remove them from this bot.\n"
        "Each admin can belong to only one bot.\n"
        "✅ Included · ▫️ Available · 🔒 Assigned to another bot\n\n"
        f"Included: {len(included)}",
        InlineKeyboardMarkup(rows),
    )


async def _select_admin(update: Update, parts: list[str]):
    if len(parts) != 5 or not re.fullmatch(r"[A-Za-z0-9_-]{22}", parts[2]):
        raise ValueError
    route_id = uuid.UUID(bytes=base64.urlsafe_b64decode(parts[2] + "=="))
    if _compact_id(route_id.bytes) != parts[2]:
        raise ValueError
    if not re.fullmatch(r"[0-9]{1,19}", parts[3]) or not re.fullmatch(r"[A-Za-z0-9_-]{16}", parts[4]):
        raise ValueError
    route = await settings.get_route(str(route_id))
    if route is None or route["revision"] != int(parts[3]):
        return await _list(update, notice="⚠️ Configuration changed or was deleted. Open it again.\n\n")
    admins, assignments, _ = await _admin_choices(route)
    matches = [name for name in admins if _admin_token(name) == parts[4]]
    if len(matches) != 1:
        return await _admins(update, route, "⚠️ Admin list changed. Select an admin again.\n\n")
    name = matches[0]
    other = assignments.get(name)
    if other is not None and other["id"] != route["id"]:
        return await _admins(
            update,
            route,
            f"⚠️ This admin belongs to another bot: <b>{_preview(other['name'], 80)}</b>. "
            "Remove them from that bot first.\n\n",
        )
    selected = set(route["admin_usernames"])
    selected.symmetric_difference_update({name})
    try:
        route = await settings.update_route(
            route["id"], {"admin_usernames": sorted(selected)}, expected_revision=route["revision"]
        )
    except MirzaRouteError as error:
        current = await settings.get_route(route["id"])
        if current is None:
            return await _list(update, notice="⚠️ Configuration was deleted.\n\n")
        return await _admins(update, current, f"⚠️ {html.escape(str(error))}\n\n")
    return await _admins(update, route)


def _message_token(message_id: str) -> str:
    """Use compact UUID identity, resolved unambiguously against saved entries."""
    return _compact_id(uuid.UUID(message_id).bytes[:12])


def _message_button(label: str, action: str, route: dict, message: dict) -> InlineKeyboardButton:
    payload = (
        f"mn:{action}:{_compact_id(uuid.UUID(route['id']).bytes)}:{route['revision']}:{_message_token(message['id'])}"
    )
    return InlineKeyboardButton(label, callback_data=payload)


def _message_back(route: dict, message: dict) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[_message_button("« Back to Message", "O", route, message)], [_button("« Back to Bot", "r", route)]]
    )


def _step_summary(message: dict) -> str:
    return ", ".join(str(index + 1) for index in message["selected_steps"]) or "none"


def _ready(message: dict) -> bool:
    return bool(message["message_text"].strip() and message["selected_steps"])


async def _messages(update: Update, route: dict, notice: str = ""):
    rows = [
        [
            _message_button(
                f"{'✅' if _ready(message) else '📝'} {message['name'][:80]} · steps: {_step_summary(message)}",
                "O",
                route,
                message,
            )
        ]
        for message in route["messages"]
    ]
    rows.extend([[_button("➕ Add Message", "a", route)], [_button("« Back to Bot", "r", route)]])
    return await _respond(
        update,
        f"💬 <b>Messages · {_preview(route['name'], 80)}</b>\n\n{notice}"
        "Choose a named message or add one. Each step belongs to at most one message.\n"
        "Messages with empty text or no selected steps are drafts. Unassigned stages send no notification.",
        InlineKeyboardMarkup(rows),
    )


async def _message(update: Update, route: dict, message: dict, notice: str = ""):
    return await _respond(
        update,
        f"💬 <b>{_preview(message['name'], 80)}</b>\n\n{notice}"
        f"Status: {'Ready' if _ready(message) else 'Draft'}\n"
        f"Steps: {_step_summary(message)}\n\n"
        f"<b>Text preview</b>\n{_preview(message['message_text']) or 'Empty'}",
        InlineKeyboardMarkup(
            [
                [_message_button("✏️ Text", "T", route, message), _message_button("⚖️ Steps", "S", route, message)],
                [_message_button("✏️ Rename", "N", route, message), _message_button("🗑 Delete", "D", route, message)],
                [_button("« Back to Messages", "h", route)],
                [_button("« Back to Bot", "r", route)],
            ]
        ),
    )


async def _effective_steps() -> list:
    """Read the normalized enforcement ladder without constructing its state."""
    from utils.punishment_system import PunishmentSystem, _parse_step

    config = await read_settings()
    punishment = config.get("punishment", {})
    raw = punishment.get("steps", []) if isinstance(punishment, dict) else []
    parsed = [_parse_step(index + 1, step) for index, step in enumerate(raw)] if isinstance(raw, list) else []
    return [step for step in parsed if step is not None] or PunishmentSystem.DEFAULT_STEPS.copy()


async def _steps(update: Update, route: dict, message: dict, notice: str = ""):
    steps = (await _effective_steps())[:10]
    selected = set(message["selected_steps"])
    owners = {
        index: entry for entry in route["messages"] if entry["id"] != message["id"] for index in entry["selected_steps"]
    }
    rows = []
    for index, step in enumerate(steps):
        owner = owners.get(index)
        prefix = "✅" if index in selected else "🔒" if owner else "▫️"
        suffix = f" · {owner['name'][:60]}" if owner else ""
        rows.append(
            [
                _message_button(
                    f"{prefix} Step {index + 1}: {step.get_display_text()}{suffix}",
                    str(index),
                    route,
                    message,
                )
            ]
        )
    for index in sorted(selected - set(range(len(steps)))):
        rows.append([_message_button(f"✅ Step {index + 1}: inactive (remove)", str(index), route, message)])
    rows.extend(_message_back(route, message).inline_keyboard)
    return await _respond(
        update,
        f"⚖️ <b>Steps · {_preview(message['name'], 80)}</b>\n\n{notice}"
        "Tap numbered stages to include or remove them. Locked stages belong to another message.\n"
        "Review assignments after reordering or replacing the punishment ladder.\n"
        "Inactive saved positions remain removable; they never move automatically.",
        InlineKeyboardMarkup(rows),
    )


async def _message_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, parts: list[str]):
    if len(parts) != 5 or not re.fullmatch(r"[A-Za-z0-9_-]{22}", parts[2]):
        raise ValueError
    route_id = uuid.UUID(bytes=base64.urlsafe_b64decode(parts[2] + "=="))
    if _compact_id(route_id.bytes) != parts[2]:
        raise ValueError
    if not re.fullmatch(r"[0-9]{1,19}", parts[3]) or not re.fullmatch(r"[A-Za-z0-9_-]{16}", parts[4]):
        raise ValueError
    route = await settings.get_route(str(route_id))
    if route is None or route["revision"] != int(parts[3]):
        return await _list(update, "⚠️ Configuration changed or was deleted. Open it again.\n\n")
    matches = [message for message in route["messages"] if _message_token(message["id"]) == parts[4]]
    if len(matches) != 1:
        return await _messages(update, route, "⚠️ Message list changed. Select a message again.\n\n")
    message = matches[0]
    action = parts[1]
    if action == "O":
        return await _message(update, route, message)
    if action == "S":
        return await _steps(update, route, message)
    if action in {"T", "N"}:
        return await _prompt(
            update,
            context,
            "message_text" if action == "T" else "message_name",
            route,
            message=message,
        )
    if action == "D":
        return await _respond(
            update,
            f"🗑 Delete message <b>{_preview(message['name'], 80)}</b>?\n"
            "Its steps will become unassigned and older unsent bot notifications will be cancelled.",
            InlineKeyboardMarkup(
                [
                    [_message_button("Confirm Delete", "X", route, message)],
                    [_message_button("« Back to Message", "O", route, message)],
                ]
            ),
        )
    try:
        if action == "X":
            route = await settings.delete_message(route["id"], message["id"], expected_revision=route["revision"])
            return await _messages(update, route, "✅ Message deleted.\n\n")
        index = int(action)
        selected = set(message["selected_steps"])
        steps = (await _effective_steps())[:10]
        if index >= len(steps) and index not in selected:
            raise ValueError
        owner = next(
            (entry for entry in route["messages"] if entry["id"] != message["id"] and index in entry["selected_steps"]),
            None,
        )
        if owner:
            return await _steps(
                update,
                route,
                message,
                f"⚠️ This step belongs to another message: <b>{_preview(owner['name'], 80)}</b>. "
                "Remove it there first.\n\n",
            )
        selected.symmetric_difference_update({index})
        route = await settings.update_message(
            route["id"],
            message["id"],
            {"selected_steps": sorted(selected)},
            expected_revision=route["revision"],
        )
    except MirzaRouteError as error:
        current = await settings.get_route(route["id"])
        if current is None or current["revision"] != route["revision"]:
            return await _list(update, "⚠️ Configuration changed or was deleted. Open it again.\n\n")
        notice = f"⚠️ {html.escape(str(error))}\n\n"
        if action == "X":
            return await _message(update, route, message, notice)
        return await _steps(update, route, message, notice)
    message = next(entry for entry in route["messages"] if entry["id"] == message["id"])
    return await _steps(update, route, message)


async def _prompt(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    field: str,
    route: dict | None,
    notice: str = "",
    *,
    message: dict | None = None,
):
    context.user_data.update(
        waiting_for=INPUT_FLOW,
        mirza_field=field,
        mirza_route_id=route["id"] if route else None,
        mirza_revision=route["revision"] if route else None,
        mirza_message_id=message["id"] if message else None,
    )
    prompts = {
        "new_message_name": "Enter a unique name for the new message, e.g. <code>First warning</code>. It starts as a draft with no text or steps.",
        "message_name": "Enter a unique name for this message, e.g. <code>Final disable</code>.",
        "name": "Enter a name for this Mirza Bot, e.g. <code>Sales Bot</code>.\n"
        "Choose its admins from the panel list after saving.",
        "base_url": "Enter the HTTPS Mirza Bot API base URL, e.g. <code>https://bot.example.com/api/</code>.\n"
        "Enter the base, not its /users endpoint.",
        "api_key": "Enter the replacement Mirza Bot API key with the users permission.\n"
        "The key is never displayed. Its incoming message will be deleted best-effort after saving.",
        "message_text": "Enter the fixed HTML message. Use literal <code>[Username]</code> for the punished "
        "configuration username and <code>[Limit]</code> for its effective allowed concurrent "
        "connection count. Both tokens are case-sensitive.\n"
        "Example: کانفیگ [Username] به‌دلیل استفاده از بیش از [Limit] دستگاه غیرفعال شد.\n"
        "The expanded message must fit 4096 UTF-16 units.",
    }
    text = f"✏️ <b>Notifications</b>\n\n{notice}{prompts[field]}\n\n/cancel to cancel."
    if field == "message_text" and message:
        text += f"\n\n<b>Current preview</b>\n{_preview(message['message_text'], 350)}"
    return await _respond(update, text, _message_back(route, message) if message else _back(route))


async def notifications_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List named bot configurations in an authorized private chat."""
    end_input(context)
    if not await _authorized(update, context):
        return
    try:
        return await _list(update)
    except Exception:
        return await _respond(update, "⚠️ Could not load Notifications. Try again.", _back())


async def notification_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle short UUID/revision payloads without trusting stale route buttons."""
    end_input(context)
    if not await _authorized(update, context):
        return
    route = None
    try:
        parts = update.callback_query.data.split(":")
        action = parts[1]
        if parts == ["mn", "b"]:
            return await _prompt(update, context, "name", None)
        if parts[0] != "mn":
            raise ValueError
        if action == "i":
            return await _select_admin(update, parts)
        if action in {"O", "T", "S", "N", "D", "X", *"0123456789"}:
            return await _message_callback(update, context, parts)
        if action == "p" and len(parts) == 3 and parts[2].isascii() and parts[2].isdigit() and len(parts[2]) <= 9:
            return await _list(update)
        if action not in {"r", "e", "u", "k", "m", "n", "g", "f", "t", "l", "s", "d", "x", "c", "h", "a"}:
            raise ValueError
        if len(parts) != (5 if action == "s" else 4) or str(uuid.UUID(parts[2])) != parts[2]:
            raise ValueError
        if not parts[3].isascii() or not parts[3].isdigit() or not 0 < len(parts[3]) <= 19:
            raise ValueError
        revision = int(parts[3])
        route = await settings.get_route(parts[2])
        if route is None or route["revision"] != revision:
            return await _list(update, notice="⚠️ Configuration changed or was deleted. Open it again.\n\n")
        if action == "r":
            return await _route(update, route)
        if action == "c":
            return await _test_connection(update, context, route)
        if action in {"g", "f"}:
            return await _admins(update, route, force_refresh=action == "f")
        if action in {"u", "k", "n"}:
            field = {"u": "base_url", "k": "api_key", "n": "name"}[action]
            return await _prompt(update, context, field, route)
        if action == "a":
            return await _prompt(update, context, "new_message_name", route)
        if action in {"h", "m", "t", "l", "s"}:
            if action == "s" and not re.fullmatch(r"[0-9]", parts[4]):
                raise ValueError
            return await _messages(update, route)
        if action == "d":
            return await _respond(
                update,
                f"🗑 Delete bot <b>{_preview(route['name'], 80)}</b>?\n"
                "Its admins will be unassigned and older unsent notifications will be cancelled.",
                InlineKeyboardMarkup([[_button("Confirm Delete", "x", route)], [_button("« Back to Bot", "r", route)]]),
            )
        if action == "x":
            await settings.delete_route(route["id"], expected_revision=revision)
            return await _list(update, notice="✅ Configuration deleted.\n\n")
        route = await settings.update_route(route["id"], {"enabled": not route["enabled"]}, expected_revision=revision)
        return await _route(update, route)
    except MirzaRouteError as error:
        # Settings validation errors contain fixed safe descriptions, never values.
        return await _respond(
            update, f"⚠️ {html.escape(str(error))}\nOpen the configuration again to continue.", _back(route)
        )
    except ValueError:
        return await _respond(update, "⚠️ This button is invalid or expired. Open Notifications again.", _back())
    except Exception:
        end_input(context)
        return await _respond(update, "⚠️ Could not load or save Notifications. Try again.", _back())


async def notification_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Commit only an owned, authorized private-chat continuation at its revision."""
    if not accepts_inline(update, context) or context.user_data.get("waiting_for") != INPUT_FLOW:
        return
    if not await _authorized(update, context):
        return
    field = context.user_data.get("mirza_field")
    route = None
    message = None
    try:
        if field not in {"name", "base_url", "api_key", "message_text", "message_name", "new_message_name"}:
            raise ValueError
        if field == "name" and context.user_data.get("mirza_route_id") is None:
            route = await settings.create_route(update.message.text)
        else:
            route = await settings.get_route(context.user_data["mirza_route_id"])
            revision = context.user_data["mirza_revision"]
            if route is None or route["revision"] != revision:
                end_input(context)
                return await _list(update, notice="⚠️ Configuration changed or was deleted. Open it again.\n\n")
            if field == "new_message_name":
                route = await settings.create_message(route["id"], update.message.text, expected_revision=revision)
                message = route["messages"][-1]
            elif field in {"message_text", "message_name"}:
                message_id = context.user_data.get("mirza_message_id")
                message = next((entry for entry in route["messages"] if entry["id"] == message_id), None)
                if message is None:
                    raise ValueError
                route = await settings.update_message(
                    route["id"],
                    message_id,
                    {"name" if field == "message_name" else field: update.message.text},
                    expected_revision=revision,
                )
                message = next(entry for entry in route["messages"] if entry["id"] == message_id)
            else:
                route = await settings.update_route(
                    route["id"], {field: update.message.text}, expected_revision=revision
                )
        if field == "api_key":
            try:
                await update.message.delete()
            except Exception:
                pass
        end_input(context)
        return await (
            _message(update, route, message, "✅ Saved.\n\n") if message else _route(update, route, "✅ Saved.\n\n")
        )
    except MirzaRouteError as error:
        return await _prompt(update, context, field, route, f"⚠️ {html.escape(str(error))}\n\n", message=message)
    except Exception:
        end_input(context)
        return await _respond(update, "⚠️ Could not save Notifications. Open the configuration and try again.", _back())
