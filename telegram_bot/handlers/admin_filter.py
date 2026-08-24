"""
Admin filter handlers for the Telegram bot.
Commands for managing admin-based user filtering.
"""

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler

from telegram_bot.utils import read_json_file, write_json_file, send_response as _send_response
from telegram_bot.keyboards import create_back_to_main_keyboard
from telegram_bot.constants import CallbackData
from telegram_bot.handlers.admin import check_admin_privilege
from utils.read_config import read_config


async def admin_filter_status(update: Update, _context: ContextTypes.DEFAULT_TYPE):
    """Show the current admin filter configuration."""
    check = await check_admin_privilege(update)
    if check:
        return check
    
    try:
        from utils.admin_filter import get_admin_filter_status_text, get_all_admins
        from utils.types import PanelType
        
        config_data = await read_config()
        
        # Get panel data for admin lookup
        panel_config = config_data.get("panel", {})
        panel_data = PanelType(
            panel_config.get("username", ""),
            panel_config.get("password", ""),
            panel_config.get("domain", "")
        )
        
        # Get all admins for display
        admins = await get_all_admins(panel_data)
        
        # Get filter status
        status_text = get_admin_filter_status_text(config_data, admins)
        
        # Build admins list
        admins_list = []
        for admin in admins:
            username = admin.get("username", "?")
            is_sudo = "👑" if admin.get("is_sudo", False) else ""
            is_disabled = "🔒" if admin.get("is_disabled", False) else ""
            admins_list.append(f"  • <code>{username}</code> {is_sudo}{is_disabled}")
        
        admins_display = "\n".join(admins_list) if admins_list else "  No admins found"
        
        message = (
            f"👤 <b>Admin Filter Status</b>\n\n"
            f"{status_text}\n\n"
            f"<b>Available Admins:</b>\n{admins_display}\n\n"
            f"<b>Commands:</b>\n"
            f"/admin_filter_toggle - Enable/disable\n"
            f"/admin_filter_mode - Set include/exclude\n"
            f"/admin_filter_set - Set admin usernames\n"
            f"/admin_filter_add - Add admin\n"
            f"/admin_filter_remove - Remove admin"
        )
        
        await _send_response(update, message, reply_markup=create_back_to_main_keyboard())
        
    except Exception as e:
        await _send_response(update, f"❌ Error: {str(e)}")
    
    return ConversationHandler.END


async def admin_filter_toggle(update: Update, _context: ContextTypes.DEFAULT_TYPE):
    """Toggle admin filter on/off."""
    check = await check_admin_privilege(update)
    if check:
        return check
    
    try:
        from utils.read_config import save_config_value, invalidate_config_cache
        
        config_data = await read_config()
        filter_config = config_data.get("admin_filter", {})
        current_state = filter_config.get("enabled", False)
        new_state = not current_state
        
        await save_config_value("admin_filter_enabled", "true" if new_state else "false")
        await invalidate_config_cache()
        
        status = "✅ Enabled" if new_state else "❌ Disabled"
        await _send_response(update, f"👤 Admin filter is now: {status}")
        
    except Exception as e:
        await _send_response(update, f"❌ Error: {str(e)}")
    
    return ConversationHandler.END


async def admin_filter_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Set admin filter mode (include/exclude)."""
    check = await check_admin_privilege(update)
    if check:
        return check
    
    if context.args:
        mode = context.args[0].lower()
        if mode not in ["include", "exclude"]:
            await _send_response(
                update,
                "❌ Invalid mode. Use <code>include</code> or <code>exclude</code>"
            )
            return ConversationHandler.END
        
        try:
            from utils.read_config import save_config_value, invalidate_config_cache
            
            await save_config_value("admin_filter_mode", mode)
            await invalidate_config_cache()
            
            if mode == "include":
                desc = "Only users of specified admins will be monitored"
            else:
                desc = "Users of specified admins will be whitelisted"
            
            await _send_response(
                update,
                f"✅ Admin filter mode set to: <code>{mode}</code>\n{desc}"
            )
            
        except Exception as e:
            await _send_response(update, f"❌ Error: {str(e)}")
        
        return ConversationHandler.END
    
    await _send_response(
        update,
        "👤 <b>Set Admin Filter Mode</b>\n\n"
        "<code>/admin_filter_mode include</code>\n"
        "  → Only users of specified admins are monitored\n\n"
        "<code>/admin_filter_mode exclude</code>\n"
        "  → Users of specified admins are whitelisted (not limited)"
    )
    return ConversationHandler.END


async def admin_filter_set(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Set the list of admin usernames for filtering."""
    check = await check_admin_privilege(update)
    if check:
        return check
    
    if context.args:
        try:
            from utils.read_config import save_config_value, invalidate_config_cache
            
            # Parse admin usernames from arguments
            admin_usernames = []
            for arg in context.args:
                # Support comma-separated and space-separated
                for username in arg.split(","):
                    username = username.strip()
                    if username:
                        admin_usernames.append(username)
            
            # Save as comma-separated string
            await save_config_value("admin_filter_usernames", ",".join(admin_usernames))
            await invalidate_config_cache()
            
            await _send_response(
                update,
                f"✅ Admin filter set to: <code>{admin_usernames}</code>"
            )
            
        except Exception as e:
            await _send_response(update, f"❌ Error: {str(e)}")
        
        return ConversationHandler.END
    
    await _send_response(
        update,
        "👤 <b>Set Admin Filter Admins</b>\n\n"
        "Usage: <code>/admin_filter_set admin1 admin2</code>\n"
        "Or: <code>/admin_filter_set admin1,admin2</code>\n\n"
        "Use /admin_filter_status to see available admins."
    )
    return ConversationHandler.END


async def admin_filter_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Add an admin username to the filter."""
    check = await check_admin_privilege(update)
    if check:
        return check
    
    if not context.args:
        await _send_response(
            update,
            "❌ Please provide an admin username.\n"
            "Example: <code>/admin_filter_add admin1</code>"
        )
        return ConversationHandler.END
    
    try:
        from utils.read_config import save_config_value, invalidate_config_cache
        
        admin_username = context.args[0].strip()
        
        config_data = await read_config()
        filter_config = config_data.get("admin_filter", {})
        current_admins = filter_config.get("admin_usernames", [])
        
        if admin_username in current_admins:
            await _send_response(
                update,
                f"ℹ️ Admin <code>{admin_username}</code> is already in the filter."
            )
            return ConversationHandler.END
        
        current_admins.append(admin_username)
        await save_config_value("admin_filter_usernames", ",".join(current_admins))
        await invalidate_config_cache()
        
        await _send_response(
            update,
            f"✅ Added admin <code>{admin_username}</code> to filter.\n"
            f"Current admins: <code>{current_admins}</code>"
        )
        
    except Exception as e:
        await _send_response(update, f"❌ Error: {str(e)}")
    
    return ConversationHandler.END


async def admin_filter_remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Remove an admin username from the filter."""
    check = await check_admin_privilege(update)
    if check:
        return check
    
    if not context.args:
        await _send_response(
            update,
            "❌ Please provide an admin username.\n"
            "Example: <code>/admin_filter_remove admin1</code>"
        )
        return ConversationHandler.END
    
    try:
        from utils.read_config import save_config_value, invalidate_config_cache
        
        admin_username = context.args[0].strip()
        
        config_data = await read_config()
        filter_config = config_data.get("admin_filter", {})
        current_admins = filter_config.get("admin_usernames", [])
        
        if admin_username not in current_admins:
            await _send_response(
                update,
                f"ℹ️ Admin <code>{admin_username}</code> is not in the filter."
            )
            return ConversationHandler.END
        
        current_admins.remove(admin_username)
        await save_config_value("admin_filter_usernames", ",".join(current_admins))
        await invalidate_config_cache()
        
        await _send_response(
            update,
            f"✅ Removed admin <code>{admin_username}</code> from filter.\n"
            f"Remaining admins: <code>{current_admins}</code>"
        )
        
    except Exception as e:
        await _send_response(update, f"❌ Error: {str(e)}")
    
    return ConversationHandler.END

# ═══════════════════════════════════════════════════════════════════════════════
# CALLBACK HANDLERS FOR GLASS BUTTON UI
# ═══════════════════════════════════════════════════════════════════════════════


async def _get_admins_from_panel():
    """Helper to get admins from panel."""
    try:
        from utils.admin_filter import get_all_admins
        from utils.types import PanelType
        
        config_data = await read_config()
        panel_config = config_data.get("panel", {})
        panel_data = PanelType(
            panel_config.get("username", ""),
            panel_config.get("password", ""),
            panel_config.get("domain", "")
        )
        
        admins = await get_all_admins(panel_data)
        return admins, config_data
    except Exception as e:
        return [], {}


def create_admin_filter_keyboard(config_data: dict, admins: list):
    """Create keyboard for admin filter with mode and admin selection."""
    filter_config = config_data.get("admin_filter", {})
    enabled = filter_config.get("enabled", False)
    mode = filter_config.get("mode", "include")
    selected_admins = filter_config.get("admin_usernames", [])
    
    keyboard = []
    
    # Enable/Disable toggle
    toggle_text = "🔴 Disable Filter" if enabled else "🟢 Enable Filter"
    keyboard.append([InlineKeyboardButton(toggle_text, callback_data=CallbackData.ADMIN_FILTER_TOGGLE)])
    
    # Mode selection
    include_text = "✅ Include" if mode == "include" else "⬜ Include"
    exclude_text = "✅ Exclude" if mode == "exclude" else "⬜ Exclude"
    keyboard.append([
        InlineKeyboardButton(include_text, callback_data=CallbackData.ADMIN_FILTER_MODE_INCLUDE),
        InlineKeyboardButton(exclude_text, callback_data=CallbackData.ADMIN_FILTER_MODE_EXCLUDE),
    ])
    
    # Mode description
    if mode == "include":
        mode_desc = "Only users of selected admins will be monitored"
    else:
        mode_desc = "Users of selected admins will be whitelisted"
    
    # Admin selection buttons
    for admin in admins:
        username = admin.get("username", "Unknown")
        is_sudo = admin.get("is_sudo", False)
        is_disabled = admin.get("is_disabled", False)
        is_selected = username in selected_admins
        
        prefix = "✅" if is_selected else "⬜"
        suffix = ""
        if is_sudo:
            suffix += " 👑"
        if is_disabled:
            suffix += " 🔒"
        
        keyboard.append([
            InlineKeyboardButton(
                f"{prefix} {username}{suffix}",
                callback_data=f"af_toggle_admin:{username}"
            )
        ])
    
    # Back button
    keyboard.append([InlineKeyboardButton("🔄 Refresh", callback_data=CallbackData.ADMIN_FILTER_MENU)])
    keyboard.append([InlineKeyboardButton("« Back to Settings", callback_data=CallbackData.SETTINGS_MENU)])
    
    return InlineKeyboardMarkup(keyboard), mode_desc


async def handle_admin_filter_menu_callback(query, _context: ContextTypes.DEFAULT_TYPE):
    """Handle callback for admin filter menu with glass buttons."""
    from telegram.error import BadRequest
    
    admins, config_data = await _get_admins_from_panel()
    
    if not admins:
        try:
            await query.edit_message_text(
                text="👤 <b>Admin Filter</b>\n\n"
                     "❌ Could not load admins from panel.\n"
                     "Please check your panel connection.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔄 Retry", callback_data=CallbackData.ADMIN_FILTER_MENU)],
                    [InlineKeyboardButton("« Back", callback_data=CallbackData.SETTINGS_MENU)]
                ]),
                parse_mode="HTML"
            )
        except BadRequest as e:
            if "message is not modified" not in str(e).lower():
                raise
        return
    
    keyboard, mode_desc = create_admin_filter_keyboard(config_data, admins)
    filter_config = config_data.get("admin_filter", {})
    enabled = filter_config.get("enabled", False)
    status = "✅ Enabled" if enabled else "❌ Disabled"
    
    try:
        await query.edit_message_text(
            text=f"👤 <b>Admin Filter</b>\n\n"
                 f"<b>Status:</b> {status}\n"
                 f"<b>Mode:</b> {mode_desc}\n\n"
                 f"Select admins to include/exclude:",
            reply_markup=keyboard,
            parse_mode="HTML"
        )
    except BadRequest as e:
        if "message is not modified" not in str(e).lower():
            raise


async def handle_admin_filter_toggle_callback(query, _context: ContextTypes.DEFAULT_TYPE):
    """Handle toggle callback for admin filter."""
    try:
        from utils.read_config import save_config_value, invalidate_config_cache
        
        config_data = await read_config()
        filter_config = config_data.get("admin_filter", {})
        current_state = filter_config.get("enabled", False)
        
        await save_config_value("admin_filter_enabled", "true" if not current_state else "false")
        await invalidate_config_cache()
        
        # Refresh the menu
        await handle_admin_filter_menu_callback(query, _context)
        
    except Exception as e:
        await query.edit_message_text(
            text=f"❌ Error: {str(e)}",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("« Back", callback_data=CallbackData.ADMIN_FILTER_MENU)]
            ]),
            parse_mode="HTML"
        )


async def handle_admin_filter_mode_callback(query, _context: ContextTypes.DEFAULT_TYPE, mode: str):
    """Handle mode selection callback for admin filter."""
    try:
        from utils.read_config import save_config_value, invalidate_config_cache
        
        await save_config_value("admin_filter_mode", mode)
        await invalidate_config_cache()
        
        # Refresh the menu
        await handle_admin_filter_menu_callback(query, _context)
        
    except Exception as e:
        await query.edit_message_text(
            text=f"❌ Error: {str(e)}",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("« Back", callback_data=CallbackData.ADMIN_FILTER_MENU)]
            ]),
            parse_mode="HTML"
        )


async def handle_admin_filter_toggle_admin_callback(query, _context: ContextTypes.DEFAULT_TYPE, admin_username: str):
    """Handle admin toggle callback for admin filter."""
    try:
        from utils.read_config import save_config_value, invalidate_config_cache
        
        config_data = await read_config()
        filter_config = config_data.get("admin_filter", {})
        current_admins = filter_config.get("admin_usernames", [])
        
        if admin_username in current_admins:
            current_admins.remove(admin_username)
        else:
            current_admins.append(admin_username)
        
        await save_config_value("admin_filter_usernames", ",".join(current_admins))
        await invalidate_config_cache()
        
        # Refresh the menu
        await handle_admin_filter_menu_callback(query, _context)
        
    except Exception as e:
        await query.edit_message_text(
            text=f"❌ Error: {str(e)}",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("« Back", callback_data=CallbackData.ADMIN_FILTER_MENU)]
            ]),
            parse_mode="HTML"
        )
