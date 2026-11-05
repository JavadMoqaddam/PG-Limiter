"""Private, revision-guarded management of per-owner Mirza notifications."""

import html
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
from utils.settings_service import read_settings

PAGE_SIZE = 10
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
    rows = [[_button("« Back to Admin", "r", route)]] if route else []
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


async def _list(update: Update, page: int = 0, notice: str = ""):
    from db.database import AsyncSessionLocal

    routes = await settings.list_routes()
    async with AsyncSessionLocal() as db:
        counts = await MirzaNotificationsCRUD.counts(db)
    pages = max(1, (len(routes) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(page, pages - 1)
    rows = [
        [_button(f"{'✅' if route['enabled'] else '⏸'} {route['admin_username'][:80]}", "r", route)]
        for route in routes[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]
    ]
    nav = []
    if page:
        nav.append(_button("← Previous", "p", extra=page - 1))
    if page + 1 < pages:
        nav.append(_button("Next →", "p", extra=page + 1))
    if nav:
        rows.append(nav)
    rows.extend(
        [
            [_button("➕ Add Admin", "a")],
            [InlineKeyboardButton("« Back to Settings", callback_data=CallbackData.SETTINGS_MENU)],
        ]
    )
    statuses = "\n".join(f"{state.title()}: {count}" for state, count in counts.items())
    return await _respond(
        update,
        f"🔔 <b>Notifications</b>\n\n{notice}"
        f"Each exact PasarGuard admin has its own configuration.\n"
        f"Admins: {len(routes)} · Page {page + 1}/{pages}\n\n{statuses}",
        InlineKeyboardMarkup(rows),
    )


async def _route(update: Update, route: dict, notice: str = ""):
    mode = (
        "Last step (dynamic)"
        if route["trigger_mode"] == "last"
        else ("Selected steps: " + (", ".join(str(step + 1) for step in route["selected_steps"]) or "none"))
    )
    text = (
        f"🔔 <b>{_preview(route['admin_username'], 80)}</b>\n\n{notice}"
        f"Status: {'Enabled' if route['enabled'] else 'Disabled'}\n"
        f"URL: <code>{_preview(route['base_url'], 160) or 'Missing'}</code>\n"
        f"API key: {'Configured' if route['key_configured'] else 'Missing'}\n"
        f"Triggers: {mode}\n\n<b>Message preview</b>\n{_preview(route['message_text']) or 'Missing'}"
    )
    rows = [
        [_button("⏸ Disable" if route["enabled"] else "✅ Enable", "e", route)],
        [_button("🌐 URL", "u", route), _button("🔑 Replace API Key", "k", route)],
        [_button("✏️ Message", "m", route), _button("⚖️ Triggers", "t", route)],
        [_button("🗑 Delete", "d", route)],
        [InlineKeyboardButton("« Back to Notifications", callback_data=CallbackData.NOTIFICATIONS_MENU)],
    ]
    return await _respond(update, text, InlineKeyboardMarkup(rows))


async def _triggers(update: Update, route: dict, notice: str = ""):
    config = await read_settings()
    steps = config.get("punishment", {}).get("steps", [])[:10]
    selected = set(route["selected_steps"]) if route["trigger_mode"] == "selected" else set()
    rows = [[_button(f"{'✅ ' if route['trigger_mode'] == 'last' else ''}Last step (dynamic)", "l", route)]]
    for index, step in enumerate(steps):
        label = {"warning": "Warning", "disable": "Disable", "revoke": "Revoke + Disable"}.get(
            step.get("type"), "Unknown"
        )
        if step.get("duration"):
            label += f" · {step['duration']} min"
        rows.append([_button(f"{'✅' if index in selected else '▫️'} Step {index + 1}: {label}", "s", route, index)])
    for index in sorted(selected - set(range(len(steps)))):
        rows.append([_button(f"✅ Step {index + 1}: inactive (remove)", "s", route, index)])
    rows.append([_button("« Back to Admin", "r", route)])
    text = (
        f"⚖️ <b>Notification triggers</b>\n\n{notice}"
        "Last step is dynamic and follows the ladder for each incident.\n"
        "Choose selected steps below; each checked position toggles independently.\n"
        "Inactive positions remain saved until you remove them.\n"
        "An enabled configuration must keep at least one selected step."
    )
    return await _respond(update, text, InlineKeyboardMarkup(rows))


async def _prompt(update: Update, context: ContextTypes.DEFAULT_TYPE, field: str, route: dict | None, notice: str = ""):
    context.user_data.update(
        waiting_for=INPUT_FLOW,
        mirza_field=field,
        mirza_route_id=route["id"] if route else None,
        mirza_revision=route["revision"] if route else None,
    )
    prompts = {
        "admin_username": "Enter the exact PasarGuard admin username. This does not create a panel admin.",
        "base_url": "Enter the HTTPS Mirza API base URL, e.g. <code>https://bot.example.com/api/</code>.\n"
        "Enter the base, not its /users endpoint.",
        "api_key": "Enter the replacement Mirza API key with users/send_message permission.\n"
        "The key is never displayed. Its incoming message will be deleted best-effort after saving.",
        "message_text": "Enter the fixed HTML message. Use literal <code>[Username]</code> for the punished "
        "configuration username and <code>[Limit]</code> for its effective allowed concurrent "
        "connection count. Both tokens are case-sensitive.\n"
        "Example: کانفیگ [Username] به‌دلیل تخلف غیرفعال شد.\n"
        "The expanded message must fit 4096 UTF-16 units.",
    }
    text = f"✏️ <b>Notifications</b>\n\n{notice}{prompts[field]}\n\n/cancel to cancel."
    if field == "message_text" and route:
        text += f"\n\n<b>Current preview</b>\n{_preview(route['message_text'], 350)}"
    return await _respond(update, text, _back(route))


async def notifications_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List independent notification configurations in an authorized private chat."""
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
        if parts == ["mn", "a"]:
            return await _prompt(update, context, "admin_username", None)
        if action == "p" and len(parts) == 3 and parts[2].isascii() and parts[2].isdigit() and len(parts[2]) <= 9:
            return await _list(update, int(parts[2]))
        if action not in {"r", "e", "u", "k", "m", "t", "l", "s", "d", "x"}:
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
        if action in {"u", "k", "m"}:
            return await _prompt(update, context, {"u": "base_url", "k": "api_key", "m": "message_text"}[action], route)
        if action == "t":
            return await _triggers(update, route)
        if action == "d":
            return await _respond(
                update,
                f"🗑 Delete configuration for <b>{_preview(route['admin_username'], 80)}</b>?\n"
                "Older unsent notifications will be cancelled.",
                InlineKeyboardMarkup(
                    [[_button("Confirm Delete", "x", route)], [_button("« Back to Admin", "r", route)]]
                ),
            )
        if action == "x":
            await settings.delete_route(route["id"], expected_revision=revision)
            return await _list(update, notice="✅ Configuration deleted.\n\n")
        changes = {"enabled": not route["enabled"]} if action == "e" else {"trigger_mode": "last", "selected_steps": []}
        if action == "s":
            if not parts[4].isascii() or not parts[4].isdigit() or len(parts[4]) != 1:
                raise ValueError
            index = int(parts[4])
            selected = set(route["selected_steps"]) if route["trigger_mode"] == "selected" else set()
            steps = (await read_settings()).get("punishment", {}).get("steps", [])[:10]
            if index >= len(steps) and index not in selected:
                raise ValueError
            selected.symmetric_difference_update({index})
            if route["enabled"] and not selected:
                return await _triggers(
                    update, route, "⚠️ Keep at least one selected step, choose Last step, or disable first.\n\n"
                )
            changes = {"trigger_mode": "selected", "selected_steps": sorted(selected)}
        route = await settings.update_route(route["id"], changes, expected_revision=revision)
        return await (_triggers(update, route) if action in {"l", "s"} else _route(update, route))
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
    try:
        if field not in {"admin_username", "base_url", "api_key", "message_text"}:
            raise ValueError
        if field == "admin_username":
            route = await settings.create_route(update.message.text)
        else:
            route = await settings.get_route(context.user_data["mirza_route_id"])
            revision = context.user_data["mirza_revision"]
            if route is None or route["revision"] != revision:
                end_input(context)
                return await _list(update, notice="⚠️ Configuration changed or was deleted. Open it again.\n\n")
            route = await settings.update_route(route["id"], {field: update.message.text}, expected_revision=revision)
        if field == "api_key":
            try:
                await update.message.delete()
            except Exception:
                pass
        end_input(context)
        return await _route(update, route, "✅ Saved.\n\n")
    except MirzaRouteError as error:
        return await _prompt(update, context, field, route, f"⚠️ {html.escape(str(error))}\n\n")
    except Exception:
        end_input(context)
        return await _respond(update, "⚠️ Could not save Notifications. Open the configuration and try again.", _back())
