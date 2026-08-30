"""
User sync settings: how often panel users are mirrored into the local database.

The mirror is what makes group and admin filtering an O(1) RAM lookup during a
check cycle, so the sync interval and the deletion review both live here.
Deletions are never automatic beyond the safety checks in ``utils.user_sync``;
the operator confirms them from the pending list.
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
    keyboard.append([InlineKeyboardButton("🗑️ Review Pending Deletions", callback_data=CallbackData.USER_SYNC_PENDING)])
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
                 f"Periodically syncs user data from panel to local database\n"
                 f"for faster group/admin filtering.\n\n"
                 f"<b>Current interval:</b> {current_interval} minutes\n"
                 f"{sync_status}\n\n"
                 f"Select sync interval:",
            reply_markup=keyboard,
            parse_mode="HTML"
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
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("« Back", callback_data=CallbackData.USER_SYNC_MENU)]
            ]),
            parse_mode="HTML"
        )


async def handle_user_sync_now_callback(query, _context: ContextTypes.DEFAULT_TYPE):
    """Handle callback for immediate user sync."""
    try:
        await query.edit_message_text(
            text="🔄 <b>Syncing users from panel...</b>\n\n"
                 "<i>This may take a moment...</i>",
            parse_mode="HTML"
        )

        # Perform sync
        from utils.user_sync import sync_users_to_database
        from utils.read_config import read_config
        from utils.types import PanelType

        config_data = await read_config()
        panel_config = config_data.get("panel", {})
        panel_data = PanelType(
            panel_config.get("username", ""),
            panel_config.get("password", ""),
            panel_config.get("domain", "")
        )

        synced, errors, deleted = await sync_users_to_database(panel_data)

        # Build result message
        result_lines = [
            f"Synced: <code>{synced}</code> users",
            f"Errors: <code>{errors}</code>",
        ]
        if deleted > 0:
            result_lines.append(f"Deleted: <code>{deleted}</code> users (removed from panel)")

        await query.edit_message_text(
            text=f"✅ <b>User Sync Complete</b>\n\n"
                 + "\n".join(result_lines) + "\n\n"
                 f"User data is now cached locally for faster filtering.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Sync Again", callback_data=CallbackData.USER_SYNC_NOW)],
                [InlineKeyboardButton("« Back to Settings", callback_data=CallbackData.SETTINGS_MENU)]
            ]),
            parse_mode="HTML"
        )

    except Exception as e:
        await query.edit_message_text(
            text=f"❌ Sync Error: {str(e)}",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Retry", callback_data=CallbackData.USER_SYNC_NOW)],
                [InlineKeyboardButton("« Back", callback_data=CallbackData.USER_SYNC_MENU)]
            ]),
            parse_mode="HTML"
        )


async def handle_pending_deletions_callback(query, context: ContextTypes.DEFAULT_TYPE):
    """Handle callback for reviewing pending user deletions."""
    try:
        await query.edit_message_text(
            text="🔍 <b>Checking pending deletions...</b>\n\n"
                 "<i>Comparing local database with panel...</i>",
            parse_mode="HTML"
        )

        from utils.user_sync import get_pending_deletions
        from utils.types import PanelType

        config_data = await read_config()
        panel_config = config_data.get("panel", {})
        panel_data = PanelType(
            panel_config.get("username", ""),
            panel_config.get("password", ""),
            panel_config.get("domain", "")
        )

        result = await get_pending_deletions(panel_data)
        pending = result["pending_deletions"]

        if not pending:
            await query.edit_message_text(
                text="✅ <b>No Pending Deletions</b>\n\n"
                     f"Local users: <code>{result['local_count']}</code>\n"
                     f"Panel users: <code>{result['panel_count']}</code>\n\n"
                     "All local users exist in the panel.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔄 Refresh", callback_data=CallbackData.USER_SYNC_PENDING)],
                    [InlineKeyboardButton("« Back", callback_data=CallbackData.USER_SYNC_MENU)]
                ]),
                parse_mode="HTML"
            )
            return

        # Build user list (limit to 30 for display)
        display_limit = 30
        if len(pending) <= display_limit:
            user_list = "\n".join(f"• <code>{u}</code>" for u in pending)
        else:
            user_list = "\n".join(f"• <code>{u}</code>" for u in pending[:display_limit])
            user_list += f"\n... and {len(pending) - display_limit} more"

        # Store pending list in context for force delete
        context.user_data["pending_deletions"] = pending

        status_icon = "⚠️" if not result["safe_to_delete"] else "📋"
        safety_note = ""
        if not result["safe_to_delete"]:
            safety_note = f"\n\n⚠️ <b>Safety Warning:</b>\n{result['reason']}"

        keyboard = [
            [InlineKeyboardButton("🔄 Refresh", callback_data=CallbackData.USER_SYNC_PENDING)]
        ]

        # Only show force delete if there are pending deletions
        if pending:
            keyboard.append([InlineKeyboardButton(
                f"🗑️ Force Delete All ({len(pending)} users)",
                callback_data=CallbackData.USER_SYNC_FORCE_DELETE
            )])

        keyboard.append([InlineKeyboardButton("« Back", callback_data=CallbackData.USER_SYNC_MENU)])

        await query.edit_message_text(
            text=f"{status_icon} <b>Pending Deletions</b>\n\n"
                 f"Local users: <code>{result['local_count']}</code>\n"
                 f"Panel users: <code>{result['panel_count']}</code>\n"
                 f"Would delete: <code>{len(pending)}</code> ({result['deletion_percentage']:.1f}%)\n"
                 f"{safety_note}\n\n"
                 f"<b>Users not in panel:</b>\n{user_list}",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML"
        )

    except Exception as e:
        await query.edit_message_text(
            text=f"❌ Error: {str(e)}",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Retry", callback_data=CallbackData.USER_SYNC_PENDING)],
                [InlineKeyboardButton("« Back", callback_data=CallbackData.USER_SYNC_MENU)]
            ]),
            parse_mode="HTML"
        )


async def handle_force_delete_callback(query, context: ContextTypes.DEFAULT_TYPE):
    """Handle callback for force deleting pending users."""
    try:
        pending = context.user_data.get("pending_deletions", [])

        if not pending:
            await query.edit_message_text(
                text="⚠️ No pending deletions found.\n\n"
                     "Please refresh the pending deletions list first.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔄 Refresh", callback_data=CallbackData.USER_SYNC_PENDING)],
                    [InlineKeyboardButton("« Back", callback_data=CallbackData.USER_SYNC_MENU)]
                ]),
                parse_mode="HTML"
            )
            return

        await query.edit_message_text(
            text=f"🗑️ <b>Deleting {len(pending)} users...</b>\n\n"
                 "<i>This may take a moment...</i>",
            parse_mode="HTML"
        )

        from utils.user_sync import force_delete_users

        deleted, errors = await force_delete_users(pending)

        # Clear the stored list
        context.user_data.pop("pending_deletions", None)

        error_text = ""
        if errors:
            error_text = "\n\n<b>Errors:</b>\n" + "\n".join(f"• {e}" for e in errors[:5])
            if len(errors) > 5:
                error_text += f"\n... and {len(errors) - 5} more errors"

        await query.edit_message_text(
            text=f"✅ <b>Force Delete Complete</b>\n\n"
                 f"Deleted: <code>{deleted}</code> users\n"
                 f"Failed: <code>{len(errors)}</code>"
                 f"{error_text}",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Check Again", callback_data=CallbackData.USER_SYNC_PENDING)],
                [InlineKeyboardButton("« Back", callback_data=CallbackData.USER_SYNC_MENU)]
            ]),
            parse_mode="HTML"
        )

    except Exception as e:
        await query.edit_message_text(
            text=f"❌ Error: {str(e)}",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("« Back", callback_data=CallbackData.USER_SYNC_MENU)]
            ]),
            parse_mode="HTML"
        )
