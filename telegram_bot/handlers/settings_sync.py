"""
User sync settings: how often panel users are mirrored into the local database.

Each successful sync mirrors the validated full panel user list, including
removing local users absent from the panel. The factual cache supports fast
group and admin filtering during a check cycle.
"""

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from telegram_bot.constants import CallbackData
from utils.read_config import read_config, save_config_value


def create_user_sync_keyboard(current_interval: int):
    """Create keyboard for user sync interval settings."""
    keyboard = []

    intervals = [
        (1, "1 minute"),
        (5, "5 minutes"),
        (10, "10 minutes"),
        (15, "15 minutes"),
    ]

    for value, label in intervals:
        prefix = "✅" if current_interval == value else "⬜"
        callback = getattr(CallbackData, f"USER_SYNC_{value}")
        keyboard.append([InlineKeyboardButton(f"{prefix} {label}", callback_data=callback)])

    keyboard.append([InlineKeyboardButton("🔄 Sync Now", callback_data=CallbackData.USER_SYNC_NOW)])
    keyboard.append([InlineKeyboardButton("« Back to Settings", callback_data=CallbackData.SETTINGS_MENU)])

    return InlineKeyboardMarkup(keyboard)


async def handle_user_sync_menu_callback(query, _context: ContextTypes.DEFAULT_TYPE):
    """Handle callback for user sync menu."""
    config_data = await read_config()
    current_interval = config_data.get("user_sync_interval", 5)

    # Get last sync time
    try:
        from utils.user_sync import get_last_sync_time

        last_sync = await get_last_sync_time()
        if last_sync:
            sync_status = f"Last sync: <code>{last_sync.strftime('%H:%M:%S')}</code>"
        else:
            sync_status = "Last sync: <i>Never</i>"
    except Exception:
        sync_status = "Last sync: <i>Unknown</i>"

    keyboard = create_user_sync_keyboard(current_interval)

    try:
        await query.edit_message_text(
            text=f"🔄 <b>User Sync Settings</b>\n\n"
            f"Automatically mirrors all panel users to the local database\n"
            f"for faster group/admin filtering. Each successful sync\n"
            f"removes local users absent from the validated full panel list.\n\n"
            f"<b>Current interval:</b> {current_interval} minutes\n"
            f"{sync_status}\n\n"
            f"Select sync interval:",
            reply_markup=keyboard,
            parse_mode="HTML",
        )
    except BadRequest as e:
        if "message is not modified" not in str(e).lower():
            raise


async def handle_user_sync_interval_callback(query, _context: ContextTypes.DEFAULT_TYPE, interval: int):
    """Handle callback for setting user sync interval."""
    from utils.read_config import invalidate_config_cache

    try:
        await save_config_value("user_sync_interval", str(interval))
        await invalidate_config_cache()

        # Refresh the menu
        await handle_user_sync_menu_callback(query, _context)

    except Exception as e:
        await query.edit_message_text(
            text=f"❌ Error: {str(e)}",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("« Back", callback_data=CallbackData.USER_SYNC_MENU)]]
            ),
            parse_mode="HTML",
        )


async def handle_user_sync_now_callback(query, _context: ContextTypes.DEFAULT_TYPE):
    """Handle callback for immediate user sync."""
    try:
        await query.edit_message_text(
            text="🔄 <b>Syncing users from panel...</b>\n\n<i>This may take a moment...</i>", parse_mode="HTML"
        )

        # Perform sync
        from utils.user_sync import get_last_sync_time, sync_users_to_database
        from utils.read_config import read_config
        from utils.types import PanelType

        config_data = await read_config()
        panel_config = config_data.get("panel", {})
        panel_data = PanelType(
            panel_config.get("username", ""), panel_config.get("password", ""), panel_config.get("domain", "")
        )

        last_sync_before = await get_last_sync_time()
        synced, errors, deleted = await sync_users_to_database(panel_data)
        last_sync_after = await get_last_sync_time()

        # Build result message
        result_lines = [
            f"Synced: <code>{synced}</code> users",
            f"Errors: <code>{errors}</code>",
        ]
        if deleted > 0:
            result_lines.append(f"Deleted: <code>{deleted}</code> users (removed from panel)")

        if errors:
            title = "❌ <b>User Sync Failed</b>" if synced == 0 else "⚠️ <b>User Sync Incomplete</b>"
            detail = "The panel mirror could not be completed. Please retry."
        elif synced == 0 and deleted == 0 and last_sync_after == last_sync_before:
            title = "⚠️ <b>User Sync Not Performed</b>"
            detail = "Another sync is already in progress. Try again shortly."
        else:
            title = "✅ <b>User Sync Complete</b>"
            detail = "The local database now mirrors the panel user list for faster filtering."

        await query.edit_message_text(
            text=f"{title}\n\n" + "\n".join(result_lines) + "\n\n" + detail,
            reply_markup=InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("🔄 Sync Again", callback_data=CallbackData.USER_SYNC_NOW)],
                    [InlineKeyboardButton("« Back to Settings", callback_data=CallbackData.SETTINGS_MENU)],
                ]
            ),
            parse_mode="HTML",
        )

    except Exception as e:
        await query.edit_message_text(
            text=f"❌ Sync Error: {str(e)}",
            reply_markup=InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("🔄 Retry", callback_data=CallbackData.USER_SYNC_NOW)],
                    [InlineKeyboardButton("« Back", callback_data=CallbackData.USER_SYNC_MENU)],
                ]
            ),
            parse_mode="HTML",
        )


async def handle_pending_deletions_callback(query, context: ContextTypes.DEFAULT_TYPE):
    """Redirect obsolete review buttons without fetching or deleting users."""
    context.user_data.pop("pending_deletions", None)
    await handle_user_sync_menu_callback(query, context)


async def handle_force_delete_callback(query, context: ContextTypes.DEFAULT_TYPE):
    """Redirect obsolete deletion buttons without applying cached deletions."""
    context.user_data.pop("pending_deletions", None)
    await handle_user_sync_menu_callback(query, context)
