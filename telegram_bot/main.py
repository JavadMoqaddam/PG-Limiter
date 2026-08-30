"""
Telegram Bot Main Module
Contains the main bot setup and handler registration.
"""

import os
import sys

try:
    from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
    from telegram.error import BadRequest, TelegramError
    from telegram.ext import (
        ApplicationBuilder,
        CommandHandler,
        ContextTypes,
        ConversationHandler,
        MessageHandler,
        CallbackQueryHandler,
        filters,
    )
except ImportError:
    print(
        "Module 'python-telegram-bot' is not installed. "
        "Use: 'pip install python-telegram-bot' to install it"
    )
    sys.exit(1)

# Import constants and keyboards
from telegram_bot.constants import (
    CallbackData,
    START_MESSAGE,
    HELP_TEXT,
    GET_DOMAIN,
    GET_USERNAME,
    GET_PASSWORD,
    GET_CONFIRMATION,
    GET_CHAT_ID,
    GET_SPECIAL_LIMIT,
    GET_LIMIT_NUMBER,
    GET_CHAT_ID_TO_REMOVE,
    SET_EXCEPT_USERS,
    REMOVE_EXCEPT_USER,
    GET_GENERAL_LIMIT_NUMBER,
    SET_IPINFO_TOKEN,
    RESTORE_CONFIG,
    WAITING_GROUP_ID,
)
from telegram_bot.keyboards import (
    create_main_menu_keyboard,
    create_settings_menu_keyboard,
    create_limits_menu_keyboard,
    create_users_menu_keyboard,
    create_monitoring_menu_keyboard,
    create_reports_menu_keyboard,
    create_admin_menu_keyboard,
    create_back_to_main_keyboard,
    create_disable_method_keyboard,
    create_whitelist_menu_keyboard,
    create_special_limits_menu_keyboard,
)

# Import handlers
from telegram_bot.handlers.admin import (
    add_admin,
    admins_list,
    check_admin_privilege,
    get_chat_id,
    get_chat_id_to_remove,
    remove_admin,
    handle_admins_list_callback,
    handle_admins_page_callback,
    handle_admin_info_callback,
    handle_delete_admin_callback,
)
from telegram_bot.handlers.limits import (
    set_special_limit,
    get_special_limit,
    get_limit_number,
    show_special_limit_function,
    get_general_limit_number,
    get_general_limit_number_handler,
    handle_general_limit_menu_callback,
    handle_general_limit_preset_callback,
    handle_general_limit_custom_callback,
    handle_set_special_limit_callback,
    handle_general_limit_input,
    handle_special_limit_username_input,
    handle_special_limit_number_input,
    handle_show_special_limit_callback,
    handle_special_limits_page_callback,
    handle_edit_special_limit_callback,
    handle_special_limit_info_callback,
    handle_remove_special_limit_callback,
    handle_special_limit_1_callback,
    handle_special_limit_2_callback,
)
from telegram_bot.handlers.users import (
    set_except_users,
    set_except_users_handler,
    remove_except_user,
    remove_except_user_handler,
    show_except_users,
    show_disabled_users_menu,
    enable_single_user,
    enable_all_disabled_users,
    cleanup_deleted_users_handler,
    handle_show_except_users_callback,
    handle_add_except_user_callback,
    handle_remove_except_user_callback,
    handle_except_user_input,
    handle_remove_except_user_input,
    handle_whitelist_page_callback,
    handle_whitelist_info_callback,
    handle_delete_whitelist_callback,
    handle_filtered_users_menu,
)
from telegram_bot.handlers.settings import (
    set_panel_domain,
    get_domain,
    get_username,
    get_password,
    set_ipinfo_token,
    ipinfo_token_handler,
    handle_enhanced_menu_callback,
    handle_enhanced_toggle_callback,
    handle_ipinfo_callback,
    handle_ipinfo_token_input,
    handle_disable_by_group_callback,
    handle_select_disabled_group_callback,
    handle_fallback_group_menu_callback,
    handle_select_fallback_group_callback,
    handle_clear_fallback_group_callback,
    handle_user_sync_menu_callback,
    handle_user_sync_interval_callback,
    handle_user_sync_now_callback,
    handle_pending_deletions_callback,
    handle_force_delete_callback,
)
from telegram_bot.handlers.monitoring import (
    monitoring_status,
    monitoring_details,
    clear_monitoring,
)
from telegram_bot.handlers.reports import (
    connection_report_command,
    node_usage_report_command,
    multi_device_users_command,
    users_by_node_command,
    users_by_protocol_command,
    ip_history_12h_command,
    ip_history_48h_command,
)
from telegram_bot.handlers.backup import (
    send_backup,
    restore_config,
    restore_config_handler,
    migrate_backup_start,
    migrate_backup_handler,
    migrate_backup_cancel,
    MIGRATE_WAITING_FILE,
)
from telegram_bot.handlers.punishment import (
    punishment_status,
    punishment_toggle,
    punishment_set_window,
    punishment_set_steps,
    user_violations,
    clear_user_violations,
)
from telegram_bot.handlers.group_filter import (
    group_filter_status,
    group_filter_toggle,
    group_filter_mode,
    group_filter_set,
    group_filter_add,
    group_filter_remove,
    handle_group_filter_menu_callback,
    handle_group_filter_toggle_callback,
    handle_group_filter_mode_callback,
    handle_group_filter_toggle_group_callback,
    handle_group_limit_menu_callback,
    handle_set_group_limit_callback,
    receive_group_limit,
)
from telegram_bot.handlers.admin_filter import (
    admin_filter_status,
    admin_filter_toggle,
    admin_filter_mode,
    admin_filter_set,
    admin_filter_add,
    admin_filter_remove,
    handle_admin_filter_menu_callback,
    handle_admin_filter_toggle_callback,
    handle_admin_filter_mode_callback,
    handle_admin_filter_toggle_admin_callback,
)

# Import utilities
from telegram_bot.utils import check_admin, add_admin_to_config, add_except_user, handel_special_limit
from utils.logs import get_logger
from utils.read_config import save_config_value, read_config

# Module logger
bot_logger = get_logger("telegram.bot")


# ═══════════════════════════════════════════════════════════════════════════════
# BOT INITIALIZATION
# ═══════════════════════════════════════════════════════════════════════════════

bot_token = None
try:
    bot_token = os.environ.get("BOT_TOKEN", "")
    if bot_token:
        bot_logger.info(f"✓ Bot token loaded from environment: {bot_token[:15]}...")
    else:
        bot_logger.warning("⚠ BOT_TOKEN environment variable is empty")
except Exception as e:
    bot_logger.error(f"⚠ Error loading config at module import: {e}")

# Create application
if bot_token:
    bot_logger.debug("Creating Telegram application with real token...")
    application = ApplicationBuilder().token(bot_token).build()
    bot_logger.info("✓ Telegram application created successfully")
    try:
        from telegram_bot.dispatcher import get_dispatcher
        get_dispatcher().set_bot(application.bot)
    except Exception as e:
        bot_logger.debug(f"Dispatcher injection note: {e}")
else:
    # Dummy token for module loading - replaced at runtime
    bot_logger.warning("⚠ Using dummy token for module loading")
    application = ApplicationBuilder().token("0000000000:XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX").build()


# ═══════════════════════════════════════════════════════════════════════════════
# CORE COMMAND HANDLERS
# ═══════════════════════════════════════════════════════════════════════════════

async def start(update: Update, _context: ContextTypes.DEFAULT_TYPE):
    """Handle the /start command."""
    user_id = update.effective_user.id if update.effective_user else None
    bot_logger.info(f"📩 Received /start from user_id={user_id}")
    
    if not user_id:
        await update.message.reply_html(
            text="Sorry, you do not have permission to use this bot."
        )
        return
    
    admins = await check_admin()
    bot_logger.debug(f"Admin list: {admins}")
    
    if not admins:
        bot_logger.warning(f"No admins configured in system. Access denied for user_id={user_id}")
        await update.message.reply_html(
            text="⚠️ <b>Bot is not configured.</b>\n"
                 "Please configure <code>ADMIN_IDS</code> in your <code>.env</code> or <code>config.json</code> file to gain access."
        )
        return
    
    if user_id not in admins:
        bot_logger.warning(f"Unauthorized access attempt from user_id={user_id}")
        await update.message.reply_html(
            text="Sorry, you do not have permission to use this bot."
        )
        return
    
    bot_logger.info(f"Sending main menu to user_id={user_id}")
    await update.message.reply_html(
        text=START_MESSAGE,
        reply_markup=create_main_menu_keyboard()
    )


async def help_command(update: Update, _context: ContextTypes.DEFAULT_TYPE):
    """Handle the /help command."""
    check = await check_admin_privilege(update)
    if check is not None:
        return check
    await update.message.reply_html(text=HELP_TEXT)



# ═══════════════════════════════════════════════════════════════════════════════
# CALLBACK ROUTING
# ═══════════════════════════════════════════════════════════════════════════════

# Every entry below is one callback -> one coroutine. Adding a button means
# adding a single line here instead of another branch in a long if/elif chain.
# Routes are called as ``route(update, query, context)``; the small lambdas adapt
# the different handler signatures.

MENU_TEXTS = {
    "settings": "⚙️ <b>Settings Menu</b>\n\nConfigure your bot settings:",
    "limits": "🎯 <b>Limits Menu</b>\n\nManage user connection limits:",
    "users": "👥 <b>Users Menu</b>\n\nManage users and view disabled accounts:",
    "monitoring": "📡 <b>Monitoring Menu</b>\n\nView user monitoring status:",
    "reports": "📊 <b>Reports Menu</b>\n\nGenerate usage reports:",
    "admin": "👑 <b>Admin Menu</b>\n\nManage bot administrators:",
    "whitelist": (
        "✅ <b>Whitelist Users</b>\n\n"
        "Users in the whitelist are excluded from IP limits.\n"
        "They can have unlimited connections."
    ),
    "special_limits": (
        "🎯 <b>Special Limit Users</b>\n\n"
        "Users with custom connection limits.\n"
        "These limits override the general limit."
    ),
    "restore": (
        "📥 <b>Restore from Backup</b>\n\n"
        "Please send your backup file (zip or json format).\n\n"
        "<b>⚠️ Warning:</b> This will replace your current data!\n\n"
        "Use /restore command to upload your backup file."
    ),
    "create_config": (
        "⚙️ <b>Create Config</b>\n\nUse the command:\n<code>/create_config</code>"
    ),
    "add_admin": (
        "👤 <b>Add Admin</b>\n\nUse the command:\n<code>/add_admin</code>\n\n"
        "Then send the chat ID of the user to add."
    ),
    "remove_admin": (
        "🗑 <b>Remove Admin</b>\n\nUse the command:\n<code>/remove_admin</code>\n\n"
        "Then send the chat ID of the admin to remove."
    ),
}


async def _show_screen(query, text: str, keyboard) -> None:
    """Render a static menu screen."""
    await query.edit_message_text(text=text, reply_markup=keyboard, parse_mode="HTML")


def _admin_screen_keyboard() -> InlineKeyboardMarkup:
    """Keyboard shared by the add/remove admin instruction screens."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("« Back to Admins", callback_data=CallbackData.LIST_ADMINS)],
        [InlineKeyboardButton("« Back to Main Menu", callback_data=CallbackData.MAIN_MENU)],
    ])


def _lazy(module_name: str, func_name: str, *args, pass_update: bool = False):
    """
    Build a route whose handler module is imported on first use.

    Keeps the heavier handler modules out of the startup import graph, exactly
    as the previous inline ``from ... import ...`` statements did.
    """
    async def _route(update, query, context):
        import importlib

        handler = getattr(importlib.import_module(module_name), func_name)
        if pass_update:
            await handler(update, context, *args)
        else:
            await handler(query, context, *args)

    return _route


DISABLE_METHOD_BULLETS = (
    "• <b>By Status</b>: Set user status to 'disabled'\n"
    "• <b>By Group</b>: Move user to a disabled group\n"
    "• <b>Fallback Group</b>: Default group for re-enabled users"
)
DISABLE_METHOD_TEXT_HEADER = "🚫 <b>Disable Method</b>\n\n"
DISABLE_METHOD_TEXT = (
    f"{DISABLE_METHOD_TEXT_HEADER}Choose how users should be disabled:\n\n{DISABLE_METHOD_BULLETS}"
)


async def _show_disable_method_menu(update, query, context) -> None:
    """Render the disable-method screen with the resolved group names."""
    config_data = await read_config()
    current_method = config_data.get("disable_method", "status")
    disabled_group_id = config_data.get("disabled_group_id")
    fallback_group_id = config_data.get("fallback_group_id")
    disabled_group_name = None
    fallback_group_name = None

    if (current_method == "group" and disabled_group_id) or fallback_group_id:
        try:
            from utils.user_group_filter import get_all_groups
            from utils.types import PanelType

            panel_config = config_data.get("panel", {})
            panel_data = PanelType(
                panel_config.get("username", ""),
                panel_config.get("password", ""),
                panel_config.get("domain", ""),
            )
            for group in await get_all_groups(panel_data):
                if disabled_group_id and group.get("id") == int(disabled_group_id):
                    disabled_group_name = group.get("name", "Unknown")
                if fallback_group_id and group.get("id") == int(fallback_group_id):
                    fallback_group_name = group.get("name", "Unknown")
        except Exception:
            pass

    await _show_screen(
        query,
        DISABLE_METHOD_TEXT,
        create_disable_method_keyboard(current_method, disabled_group_name, fallback_group_name),
    )


async def _set_disable_by_status(update, query, context) -> None:
    """Switch the disable method to status and redraw the screen."""
    await save_config_value("disable_method", "status")
    await _show_screen(
        query,
        f"{DISABLE_METHOD_TEXT_HEADER}✅ Method set to <b>By Status</b>\n\n{DISABLE_METHOD_BULLETS}",
        create_disable_method_keyboard("status", None),
    )


async def _send_backup_from_button(update, query, context) -> None:
    """Backup expects an Update-like object carrying the message."""
    class _FakeUpdate:
        def __init__(self, message, effective_user, effective_chat):
            self.message = message
            self.effective_user = effective_user
            self.effective_chat = effective_chat

    fake_update = _FakeUpdate(query.message, update.effective_user, update.effective_chat)
    await query.message.reply_text("📦 Creating backup... Please wait.")
    await send_backup(fake_update, context)


async def _cdn_mode_add(update, query, context) -> None:
    """Adding a CDN inbound continues as a text prompt when the handler asks for it."""
    from telegram_bot.handlers.settings import cdn_mode_add_callback

    if await cdn_mode_add_callback(query, context) is not None:
        context.user_data["waiting_for"] = "cdn_inbound"


CALLBACK_ROUTES = {
    # --- Main navigation ----------------------------------------------------
    CallbackData.MAIN_MENU: lambda u, q, c: _show_screen(q, START_MESSAGE, create_main_menu_keyboard()),
    CallbackData.BACK_MAIN: lambda u, q, c: _show_screen(q, START_MESSAGE, create_main_menu_keyboard()),
    CallbackData.SETTINGS_MENU: lambda u, q, c: _show_screen(q, MENU_TEXTS["settings"], create_settings_menu_keyboard()),
    CallbackData.BACK_SETTINGS: lambda u, q, c: _show_screen(q, MENU_TEXTS["settings"], create_settings_menu_keyboard()),
    CallbackData.LIMITS_MENU: lambda u, q, c: _show_screen(q, MENU_TEXTS["limits"], create_limits_menu_keyboard()),
    CallbackData.USERS_MENU: lambda u, q, c: _show_screen(q, MENU_TEXTS["users"], create_users_menu_keyboard()),
    CallbackData.BACK_USERS: lambda u, q, c: _show_screen(q, MENU_TEXTS["users"], create_users_menu_keyboard()),
    CallbackData.MONITORING_MENU: lambda u, q, c: _show_screen(q, MENU_TEXTS["monitoring"], create_monitoring_menu_keyboard()),
    CallbackData.REPORTS_MENU: lambda u, q, c: _show_screen(q, MENU_TEXTS["reports"], create_reports_menu_keyboard()),
    CallbackData.ADMIN_MENU: lambda u, q, c: _show_screen(q, MENU_TEXTS["admin"], create_admin_menu_keyboard()),
    "noop": lambda u, q, c: q.answer(),

    # --- Users --------------------------------------------------------------
    CallbackData.WHITELIST_MENU: lambda u, q, c: _show_screen(q, MENU_TEXTS["whitelist"], create_whitelist_menu_keyboard()),
    CallbackData.SPECIAL_LIMITS_MENU: lambda u, q, c: _show_screen(q, MENU_TEXTS["special_limits"], create_special_limits_menu_keyboard()),
    CallbackData.FILTERED_USERS_MENU: lambda u, q, c: handle_filtered_users_menu(q, c),
    CallbackData.SHOW_DISABLED_USERS: lambda u, q, c: show_disabled_users_menu(q),
    CallbackData.ENABLE_ALL_DISABLED: lambda u, q, c: enable_all_disabled_users(q),
    CallbackData.CLEANUP_DELETED_USERS: lambda u, q, c: cleanup_deleted_users_handler(q),
    "view_users_in_disabled_group": _lazy("telegram_bot.handlers.users", "show_users_in_disabled_group"),
    "fix_stuck_users": _lazy("telegram_bot.handlers.users", "fix_stuck_users_handler"),
    CallbackData.SHOW_EXCEPT_USERS: lambda u, q, c: handle_show_except_users_callback(q, c),
    CallbackData.SET_EXCEPT_USER: lambda u, q, c: handle_add_except_user_callback(q, c),
    CallbackData.REMOVE_EXCEPT_USER: lambda u, q, c: handle_remove_except_user_callback(q, c),
    CallbackData.SHOW_SPECIAL_LIMIT: lambda u, q, c: handle_show_special_limit_callback(q, c),
    CallbackData.SET_SPECIAL_LIMIT: lambda u, q, c: handle_set_special_limit_callback(q, c),

    # --- Admins & backup ----------------------------------------------------
    CallbackData.LIST_ADMINS: lambda u, q, c: handle_admins_list_callback(q, c),
    CallbackData.ADD_ADMIN: lambda u, q, c: _show_screen(q, MENU_TEXTS["add_admin"], _admin_screen_keyboard()),
    CallbackData.REMOVE_ADMIN: lambda u, q, c: _show_screen(q, MENU_TEXTS["remove_admin"], _admin_screen_keyboard()),
    CallbackData.BACKUP: _send_backup_from_button,
    CallbackData.RESTORE: lambda u, q, c: _show_screen(q, MENU_TEXTS["restore"], create_back_to_main_keyboard()),
    # --- Monitoring & reports ------------------------------------------------
    CallbackData.MONITORING_STATUS: lambda u, q, c: monitoring_status(u, c),
    CallbackData.MONITORING_DETAILS: lambda u, q, c: monitoring_details(u, c),
    CallbackData.MONITORING_CLEAR: lambda u, q, c: clear_monitoring(u, c),
    CallbackData.REPORT_CONNECTION: lambda u, q, c: connection_report_command(u, c),
    CallbackData.REPORT_NODE_USAGE: lambda u, q, c: node_usage_report_command(u, c),
    CallbackData.REPORT_MULTI_DEVICE: lambda u, q, c: multi_device_users_command(u, c),
    CallbackData.REPORT_IP_12H: lambda u, q, c: ip_history_12h_command(u, c),
    CallbackData.REPORT_IP_48H: lambda u, q, c: ip_history_48h_command(u, c),

    # --- Limits -------------------------------------------------------------
    CallbackData.GENERAL_LIMIT_2: lambda u, q, c: handle_general_limit_preset_callback(q, c, 2),
    CallbackData.GENERAL_LIMIT_3: lambda u, q, c: handle_general_limit_preset_callback(q, c, 3),
    CallbackData.GENERAL_LIMIT_4: lambda u, q, c: handle_general_limit_preset_callback(q, c, 4),
    CallbackData.GENERAL_LIMIT_CUSTOM: lambda u, q, c: handle_general_limit_custom_callback(q, c),
    CallbackData.SPECIAL_LIMIT_1: lambda u, q, c: handle_special_limit_1_callback(q, c),
    CallbackData.SPECIAL_LIMIT_2: lambda u, q, c: handle_special_limit_2_callback(q, c),
    CallbackData.SPECIAL_LIMIT_CUSTOM: _lazy("telegram_bot.handlers.limits", "handle_special_limit_custom_callback"),

    # --- Core settings ------------------------------------------------------
    CallbackData.CREATE_CONFIG: lambda u, q, c: _show_screen(q, MENU_TEXTS["create_config"], create_back_to_main_keyboard()),
    CallbackData.SET_IPINFO: lambda u, q, c: handle_ipinfo_callback(q, c),
    CallbackData.ENHANCED_ON: lambda u, q, c: handle_enhanced_toggle_callback(q, c, True),
    CallbackData.ENHANCED_OFF: lambda u, q, c: handle_enhanced_toggle_callback(q, c, False),

    # --- Disable method -----------------------------------------------------
    CallbackData.DISABLE_METHOD_MENU: _show_disable_method_menu,
    CallbackData.DISABLE_BY_STATUS: _set_disable_by_status,
    CallbackData.DISABLE_BY_GROUP: lambda u, q, c: handle_disable_by_group_callback(q, c),
    CallbackData.FALLBACK_GROUP_MENU: lambda u, q, c: handle_fallback_group_menu_callback(q, c),
    CallbackData.CLEAR_FALLBACK_GROUP: lambda u, q, c: handle_clear_fallback_group_callback(q, c),

    # --- User sync ----------------------------------------------------------
    CallbackData.USER_SYNC_MENU: lambda u, q, c: handle_user_sync_menu_callback(q, c),
    CallbackData.USER_SYNC_1: lambda u, q, c: handle_user_sync_interval_callback(q, c, 1),
    CallbackData.USER_SYNC_5: lambda u, q, c: handle_user_sync_interval_callback(q, c, 5),
    CallbackData.USER_SYNC_10: lambda u, q, c: handle_user_sync_interval_callback(q, c, 10),
    CallbackData.USER_SYNC_15: lambda u, q, c: handle_user_sync_interval_callback(q, c, 15),
    CallbackData.USER_SYNC_NOW: lambda u, q, c: handle_user_sync_now_callback(q, c),
    CallbackData.USER_SYNC_PENDING: lambda u, q, c: handle_pending_deletions_callback(q, c),
    CallbackData.USER_SYNC_FORCE_DELETE: lambda u, q, c: handle_force_delete_callback(q, c),
    # --- Topics -------------------------------------------------------------
    CallbackData.TOPICS_MENU: _lazy("telegram_bot.handlers.topics_settings", "handle_topics_menu_callback"),
    CallbackData.TOPICS_TOGGLE: _lazy("telegram_bot.handlers.topics_settings", "handle_topics_toggle_callback"),
    CallbackData.TOPICS_SETUP: _lazy("telegram_bot.handlers.topics_settings", "handle_topics_setup_callback"),
    CallbackData.TOPICS_CLEAR: _lazy("telegram_bot.handlers.topics_settings", "handle_topics_clear_callback"),
    CallbackData.TOPICS_SET_GROUP: _lazy("telegram_bot.handlers.topics_settings", "handle_topics_set_group_callback"),
    CallbackData.TOPICS_CHECK_PERMISSIONS: _lazy("telegram_bot.handlers.topics_settings", "handle_topics_check_permissions_callback"),
    CallbackData.TOPICS_CLEAR_CACHE: _lazy("telegram_bot.handlers.topics_settings", "handle_topics_clear_cache_callback"),

    # --- Auto-backup (handlers take the Update) ------------------------------
    CallbackData.AUTO_BACKUP_MENU: _lazy("telegram_bot.handlers.backup", "auto_backup_menu", pass_update=True),
    CallbackData.AUTO_BACKUP_TOGGLE: _lazy("telegram_bot.handlers.backup", "auto_backup_toggle", pass_update=True),
    CallbackData.AUTO_BACKUP_INTERVAL_1H: _lazy("telegram_bot.handlers.backup", "handle_auto_backup_interval_1h", pass_update=True),
    CallbackData.AUTO_BACKUP_INTERVAL_3H: _lazy("telegram_bot.handlers.backup", "handle_auto_backup_interval_3h", pass_update=True),
    CallbackData.AUTO_BACKUP_INTERVAL_6H: _lazy("telegram_bot.handlers.backup", "handle_auto_backup_interval_6h", pass_update=True),
    CallbackData.AUTO_BACKUP_INTERVAL_12H: _lazy("telegram_bot.handlers.backup", "handle_auto_backup_interval_12h", pass_update=True),
    CallbackData.AUTO_BACKUP_NOW: _lazy("telegram_bot.handlers.backup", "auto_backup_now", pass_update=True),

    # --- Punishment (handlers take the Update) -------------------------------
    CallbackData.PUNISHMENT_MENU: lambda u, q, c: punishment_status(u, c),
    CallbackData.PUNISHMENT_TOGGLE: lambda u, q, c: punishment_toggle(u, c),
    CallbackData.PUNISHMENT_WINDOW: lambda u, q, c: punishment_set_window(u, c),
    CallbackData.PUNISHMENT_STEPS: lambda u, q, c: punishment_set_steps(u, c),
    CallbackData.PUNISHMENT_WINDOW_24: _lazy("telegram_bot.handlers.punishment", "punishment_set_window_hours", 24, pass_update=True),
    CallbackData.PUNISHMENT_WINDOW_48: _lazy("telegram_bot.handlers.punishment", "punishment_set_window_hours", 48, pass_update=True),
    CallbackData.PUNISHMENT_WINDOW_72: _lazy("telegram_bot.handlers.punishment", "punishment_set_window_hours", 72, pass_update=True),
    CallbackData.PUNISHMENT_WINDOW_168: _lazy("telegram_bot.handlers.punishment", "punishment_set_window_hours", 168, pass_update=True),
    CallbackData.PUNISHMENT_ADD_STEP: _lazy("telegram_bot.handlers.punishment", "punishment_add_step_menu", pass_update=True),
    CallbackData.PUNISHMENT_STEPS_RESET: _lazy("telegram_bot.handlers.punishment", "punishment_reset_steps", pass_update=True),
    CallbackData.PUNISHMENT_STEP_WARNING: _lazy("telegram_bot.handlers.punishment", "punishment_add_step", "warning", 0, pass_update=True),
    CallbackData.PUNISHMENT_STEP_DISABLE_10: _lazy("telegram_bot.handlers.punishment", "punishment_add_step", "disable", 10, pass_update=True),
    CallbackData.PUNISHMENT_STEP_DISABLE_30: _lazy("telegram_bot.handlers.punishment", "punishment_add_step", "disable", 30, pass_update=True),
    CallbackData.PUNISHMENT_STEP_DISABLE_60: _lazy("telegram_bot.handlers.punishment", "punishment_add_step", "disable", 60, pass_update=True),
    CallbackData.PUNISHMENT_STEP_DISABLE_240: _lazy("telegram_bot.handlers.punishment", "punishment_add_step", "disable", 240, pass_update=True),
    CallbackData.PUNISHMENT_STEP_DISABLE_UNLIMITED: _lazy("telegram_bot.handlers.punishment", "punishment_add_step", "disable", 0, pass_update=True),
    CallbackData.PUNISHMENT_STEP_REVOKE: _lazy("telegram_bot.handlers.punishment", "punishment_add_step", "revoke", 0, pass_update=True),
    # --- Group & admin filters ----------------------------------------------
    CallbackData.GROUP_FILTER_MENU: lambda u, q, c: handle_group_filter_menu_callback(q, c),
    CallbackData.GROUP_LIMIT_SET: lambda u, q, c: handle_group_limit_menu_callback(q, c),
    CallbackData.GROUP_FILTER_TOGGLE: lambda u, q, c: handle_group_filter_toggle_callback(q, c),
    CallbackData.GROUP_FILTER_MODE_INCLUDE: lambda u, q, c: handle_group_filter_mode_callback(q, c, "include"),
    CallbackData.GROUP_FILTER_MODE_EXCLUDE: lambda u, q, c: handle_group_filter_mode_callback(q, c, "exclude"),
    CallbackData.ADMIN_FILTER_MENU: lambda u, q, c: handle_admin_filter_menu_callback(q, c),
    CallbackData.ADMIN_FILTER_TOGGLE: lambda u, q, c: handle_admin_filter_toggle_callback(q, c),
    CallbackData.ADMIN_FILTER_MODE_INCLUDE: lambda u, q, c: handle_admin_filter_mode_callback(q, c, "include"),
    CallbackData.ADMIN_FILTER_MODE_EXCLUDE: lambda u, q, c: handle_admin_filter_mode_callback(q, c, "exclude"),

    # --- Device counting & grouping -----------------------------------------
    CallbackData.SUBNET_IP_GROUPING_MENU: _lazy("telegram_bot.handlers.settings", "subnet_ip_grouping_menu_callback"),
    CallbackData.SUBNET_IP_GROUPING_TOGGLE: _lazy("telegram_bot.handlers.settings", "subnet_ip_grouping_toggle_callback"),
    CallbackData.SUBNET_IP_GROUPING_MODE_TOGGLE: _lazy("telegram_bot.handlers.settings", "subnet_ip_grouping_mode_toggle_callback"),
    CallbackData.HIGH_TRUST_IP_GROUPING_MENU: _lazy("telegram_bot.handlers.settings", "high_trust_ip_grouping_menu_callback"),
    CallbackData.HIGH_TRUST_IP_GROUPING_TOGGLE: _lazy("telegram_bot.handlers.settings", "high_trust_ip_grouping_toggle_callback"),
    CallbackData.DEVICE_COUNT_MENU: _lazy("telegram_bot.handlers.settings", "handle_device_count_menu_callback"),
    CallbackData.DEVICE_COUNT_SET_DEVICE: _lazy("telegram_bot.handlers.settings", "handle_device_count_set_device_callback"),
    CallbackData.DEVICE_COUNT_SET_IP: _lazy("telegram_bot.handlers.settings", "handle_device_count_set_ip_callback"),

    # --- Trust ---------------------------------------------------------------
    CallbackData.TRUST_RESET_MENU: _lazy("telegram_bot.handlers.settings", "trust_reset_menu_callback"),
    CallbackData.TRUST_RESET_ALL: _lazy("telegram_bot.handlers.settings", "trust_reset_all_callback"),

    # --- CDN -----------------------------------------------------------------
    CallbackData.CDN_MODE_MENU: _lazy("telegram_bot.handlers.settings", "cdn_mode_menu_callback"),
    CallbackData.CDN_MODE_REMOVE: _lazy("telegram_bot.handlers.settings", "cdn_mode_remove_callback"),
    CallbackData.CDN_MODE_CLEAR: _lazy("telegram_bot.handlers.settings", "cdn_mode_clear_callback"),
    CallbackData.CDN_USE_XFF_TOGGLE: _lazy("telegram_bot.handlers.settings", "cdn_use_xff_toggle_callback"),
    CallbackData.CDN_PROVIDER_MENU: _lazy("telegram_bot.handlers.settings", "cdn_provider_menu_callback"),
    CallbackData.CDN_PROVIDER_CLOUDFLARE: _lazy("telegram_bot.handlers.settings", "cdn_provider_cloudflare_callback"),
    CallbackData.CDN_MODE_ADD: _cdn_mode_add,

    # --- Nodes ---------------------------------------------------------------
    CallbackData.NODE_SETTINGS_MENU: _lazy("telegram_bot.handlers.settings", "node_settings_menu_callback"),
    CallbackData.NODE_SETTINGS_REFRESH: _lazy("telegram_bot.handlers.settings", "node_settings_refresh_callback"),
    CallbackData.NODE_CDN_MENU: _lazy("telegram_bot.handlers.settings", "node_cdn_menu_callback"),
    CallbackData.NODE_DISABLED_MENU: _lazy("telegram_bot.handlers.settings", "node_disabled_menu_callback"),
    CallbackData.NODE_CDN_CLEAR: _lazy("telegram_bot.handlers.settings", "node_cdn_clear_callback"),
    CallbackData.NODE_DISABLED_CLEAR: _lazy("telegram_bot.handlers.settings", "node_disabled_clear_callback"),

    # --- IP source -----------------------------------------------------------
    CallbackData.IP_SOURCE_MENU: _lazy("telegram_bot.handlers.settings", "handle_ip_source_menu_callback"),
    CallbackData.IP_SOURCE_SET_LOGS: _lazy("telegram_bot.handlers.settings", "handle_ip_source_set_logs_callback"),
    CallbackData.IP_SOURCE_SET_API: _lazy("telegram_bot.handlers.settings", "handle_ip_source_set_api_callback"),
    CallbackData.IP_SOURCE_SET_CONCURRENCY: _lazy("telegram_bot.handlers.settings", "handle_ip_source_concurrency_callback"),
    CallbackData.IP_SOURCE_STATS: _lazy("telegram_bot.handlers.settings", "handle_ip_source_stats_callback"),
}


# ═══════════════════════════════════════════════════════════════════════════════
# CALLBACK QUERY HANDLER
# ═══════════════════════════════════════════════════════════════════════════════

async def callback_query_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle all callback queries from inline keyboards."""
    query = update.callback_query
    await query.answer()
    
    data = query.data
    
    # Admin check - use effective_user.id for group compatibility
    user_id = update.effective_user.id if update.effective_user else None
    admins = await check_admin()
    if not user_id or user_id not in admins:
        await query.edit_message_text(
            text="Sorry, you do not have permission to use this bot."
        )
        return

    # Exact-match callbacks come from the routing table; only callbacks that
    # carry a payload (``prefix:value``) are matched below.
    route = CALLBACK_ROUTES.get(data)
    if route is not None:
        await route(update, query, context)
        return

    if data.startswith("disabled_group_page:"):
        from telegram_bot.handlers.users import show_users_in_disabled_group
        page = int(data.split(":")[1])
        await show_users_in_disabled_group(query, page)
        return

    # Handle select disabled group callbacks
    if data.startswith("select_disabled_group:"):
        group_id = int(data.split(":")[1])
        await handle_select_disabled_group_callback(query, context, group_id)
        return

    if data.startswith("select_fallback_group:"):
        group_id = int(data.split(":")[1])
        await handle_select_fallback_group_callback(query, context, group_id)
        return

    # Punishment step callbacks that carry an index
    if data.startswith("punishment_remove_step:"):
        step_index = int(data.split(":")[1])
        from telegram_bot.handlers.punishment import punishment_remove_step
        await punishment_remove_step(update, context, step_index)
        return
    
    # Handle edit step callbacks (click on step to edit)
    if data.startswith("punishment_edit_step:"):
        step_index = int(data.split(":")[1])
        from telegram_bot.handlers.punishment import punishment_edit_step
        await punishment_edit_step(update, context, step_index)
        return
    
    # Handle update step callbacks (apply new type/duration to step)
    if data.startswith("punishment_update_step:"):
        parts = data.split(":")
        step_index = int(parts[1])
        step_type = parts[2]
        duration = int(parts[3])
        from telegram_bot.handlers.punishment import punishment_update_step
        await punishment_update_step(update, context, step_index, step_type, duration)
        return
    
    # Handle group filter toggle group callbacks
    if data.startswith("gf_toggle_group:"):
        group_id = int(data.split(":")[1])
        await handle_group_filter_toggle_group_callback(query, context, group_id)
        return

    # Handle set group limit callbacks
    if data.startswith("set_glimit:"):
        group_id = int(data.split(":")[1])
        await handle_set_group_limit_callback(query, context, group_id)
        context.user_data["waiting_for"] = "group_limit"
        return

    # Handle admin filter toggle admin callbacks
    if data.startswith("af_toggle_admin:"):
        username = data.split(":")[1]
        await handle_admin_filter_toggle_admin_callback(query, context, username)
        return
    
    # Handle CDN remove inbound callbacks
    if data.startswith("cdn_remove_"):
        from telegram_bot.handlers.settings import cdn_mode_remove_inbound_callback
        await cdn_mode_remove_inbound_callback(query, context)
        return
    
    # Handle node toggle callbacks
    if data.startswith("node_cdn_toggle:"):
        node_id = int(data.split(":")[1])
        from telegram_bot.handlers.settings import node_cdn_toggle_callback
        await node_cdn_toggle_callback(query, context, node_id)
        return

    if data.startswith("node_disabled_toggle:"):
        node_id = int(data.split(":")[1])
        from telegram_bot.handlers.settings import node_disabled_toggle_callback
        await node_disabled_toggle_callback(query, context, node_id)
        return

    # Handle dynamic callbacks
    if data.startswith("enable_user:"):
        username = data.split(":", 1)[1]
        await enable_single_user(query, username)
        return
    
    if data.startswith("disabled_page:"):
        page = int(data.split(":", 1)[1])
        await show_disabled_users_menu(query, page=page)
        return
    
    # Handle special limits pagination
    if data.startswith("special_limits_page:"):
        page = int(data.split(":", 1)[1])
        await handle_special_limits_page_callback(query, context, page)
        return
    
    # Handle edit special limit callback
    if data.startswith("edit_special_limit:"):
        username = data.split(":", 1)[1]
        await handle_edit_special_limit_callback(query, context, username)
        return
    
    # Handle special limit info callback
    if data.startswith("special_limit_info:"):
        username = data.split(":", 1)[1]
        await handle_special_limit_info_callback(query, context, username)
        return
    
    # Handle remove special limit callback
    if data.startswith("remove_special_limit:"):
        username = data.split(":", 1)[1]
        await handle_remove_special_limit_callback(query, context, username)
        return
    
    # Handle whitelist pagination
    if data.startswith("whitelist_page:"):
        page = int(data.split(":", 1)[1])
        await handle_whitelist_page_callback(query, context, page)
        return
    
    # Handle whitelist info callback
    if data.startswith("whitelist_info:"):
        username = data.split(":", 1)[1]
        await handle_whitelist_info_callback(query, context, username)
        return
    
    # Handle delete whitelist callback
    if data.startswith("delete_whitelist:"):
        username = data.split(":", 1)[1]
        await handle_delete_whitelist_callback(query, context, username)
        return
    
    # Handle filtered users pagination
    if data.startswith("filtered_page:"):
        page = int(data.split(":", 1)[1])
        await handle_filtered_users_menu(query, context, page)
        return
    
    # Handle filtered user info callback (placeholder - just shows user is monitored)
    if data.startswith("filtered_info:"):
        username = data.split(":", 1)[1]
        await query.answer(f"User {username} - under general limit", show_alert=True)
        return
    
    # Handle admins pagination
    if data.startswith("admins_page:"):
        page = int(data.split(":", 1)[1])
        await handle_admins_page_callback(query, context, page)
        return
    
    # Handle admin info callback
    if data.startswith("admin_info:"):
        admin_id = data.split(":", 1)[1]
        await handle_admin_info_callback(query, context, admin_id)
        return
    
    # Handle delete admin callback
    if data.startswith("delete_admin:"):
        admin_id = data.split(":", 1)[1]
        await handle_delete_admin_callback(query, context, admin_id)
        return
    
    # Handle add_except:username callback (from notification buttons)
    if data.startswith("add_except:"):
        username = data.split(":", 1)[1]
        result = await add_except_user(username)
        if result:
            await query.edit_message_text(
                text=f"✅ User <code>{username}</code> added to except list!",
                parse_mode="HTML"
            )
        else:
            await query.edit_message_text(
                text=f"⚠️ Failed to add <code>{username}</code> to except list.",
                parse_mode="HTML"
            )
        return
    
    # Handle set_limit:username:limit callback (from notification buttons)
    if data.startswith("set_limit:"):
        parts = data.split(":")
        if len(parts) >= 3:
            username = parts[1]
            limit = int(parts[2])
            result = await handel_special_limit(username, limit)
            if result:
                await query.edit_message_text(
                    text=f"✅ Special limit <b>{limit}</b> set for <code>{username}</code>!",
                    parse_mode="HTML"
                )
            else:
                await query.edit_message_text(
                    text=f"⚠️ Failed to set special limit for <code>{username}</code>.",
                    parse_mode="HTML"
                )
        return
    
    # Handle custom_limit:username callback (from notification buttons)
    if data.startswith("custom_limit:"):
        username = data.split(":", 1)[1]
        context.user_data["selected_user"] = username
        context.user_data["waiting_for"] = "notification_custom_limit"
        await query.edit_message_text(
            text=f"🎯 <b>Set Custom Limit for: {username}</b>\n\n"
                 "Send the device limit number (e.g., <code>3</code>):",
            parse_mode="HTML"
        )
        return
    
    # Handle user_info: callback (informational only, from disabled users list)
    if data.startswith("user_info:"):
        username = data.split(":", 1)[1]
        await query.answer(f"User: {username}", show_alert=False)
        return

    # Fallback for unhandled callbacks
    bot_logger.warning(f"Unhandled callback data: {data}")
    await query.edit_message_text(
        text=f"⚠️ Unhandled callback: {data}",
        reply_markup=create_back_to_main_keyboard(),
        parse_mode="HTML"
    )


# ═══════════════════════════════════════════════════════════════════════════════
# TEXT MESSAGE HANDLER
# ═══════════════════════════════════════════════════════════════════════════════

async def text_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle text messages for inline keyboard flows."""
    waiting_for = context.user_data.get("waiting_for")
    
    if not waiting_for:
        return
    
    # Handle different input types based on waiting_for state
    if waiting_for == "general_limit":
        await handle_general_limit_input(update, context)
        return
    
    if waiting_for == "special_limit_username":
        await handle_special_limit_username_input(update, context)
        return
    
    if waiting_for == "special_limit_number":
        await handle_special_limit_number_input(update, context)
        return
    
    if waiting_for == "ipinfo_token":
        await handle_ipinfo_token_input(update, context)
        return
    
    # Handle except user input
    if waiting_for == "except_user":
        await handle_except_user_input(update, context)
        return
    
    if waiting_for == "remove_except_user":
        await handle_remove_except_user_input(update, context)
        return
    
    # Handle notification custom limit input
    if waiting_for == "notification_custom_limit":
        username = context.user_data.get("selected_user")
        if username:
            text = update.message.text.strip()
            try:
                limit = int(text)
                result = await handel_special_limit(username, limit)
                if result:
                    await update.message.reply_html(
                        text=f"✅ Special limit <b>{limit}</b> set for <code>{username}</code>!",
                        reply_markup=create_back_to_main_keyboard()
                    )
                else:
                    await update.message.reply_html(
                        text=f"⚠️ Failed to set special limit for <code>{username}</code>.",
                        reply_markup=create_back_to_main_keyboard()
                    )
            except ValueError:
                await update.message.reply_html(
                    text="❌ Invalid number. Please send a valid number.",
                    reply_markup=create_back_to_main_keyboard()
                )
            context.user_data.pop("selected_user", None)
        context.user_data["waiting_for"] = None
        return
    # Handle group limit input
    if waiting_for == "group_limit":
        await receive_group_limit(update, context)
        context.user_data["waiting_for"] = None
        return
    
    # Handle CDN inbound input
    if waiting_for == "cdn_inbound":
        from telegram_bot.handlers.settings import cdn_mode_add_handler
        await cdn_mode_add_handler(update, context)
        context.user_data["waiting_for"] = None
        return
    
    # Handle IP source concurrency input. ``waiting_for`` is cleared first so
    # the handler can re-arm it when the value is rejected.
    if waiting_for == "ip_source_concurrency":
        from telegram_bot.handlers.settings import ip_source_concurrency_handler
        context.user_data["waiting_for"] = None
        await ip_source_concurrency_handler(update, context)
        return

    # Handle forum group ID input
    if waiting_for == "forum_group_id":
        from telegram_bot.handlers.topics_settings import topics_set_group_receive
        await topics_set_group_receive(update, context)
        context.user_data["waiting_for"] = None
        return
    
    # Reset if no handler found
    context.user_data["waiting_for"] = None


# ═══════════════════════════════════════════════════════════════════════════════
# DOCUMENT MESSAGE HANDLER
# ═══════════════════════════════════════════════════════════════════════════════

async def document_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle document uploads (for restore)."""
    waiting_for = context.user_data.get("waiting_for")
    
    if waiting_for == "restore":
        await restore_config_handler(update, context)
        return


# ═══════════════════════════════════════════════════════════════════════════════
# HANDLER REGISTRATION
# ═══════════════════════════════════════════════════════════════════════════════

# Core commands
application.add_handler(CommandHandler("start", start))
application.add_handler(CommandHandler("help", help_command))

# Callback and message handlers
application.add_handler(CallbackQueryHandler(callback_query_handler))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_message_handler))

# NOTE: Document handler is registered AFTER all ConversationHandlers
# to allow ConversationHandlers to handle documents first

# Admin management
application.add_handler(
    ConversationHandler(
        entry_points=[CommandHandler("add_admin", add_admin)],
        states={GET_CHAT_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_chat_id)]},
        fallbacks=[],
    )
)
application.add_handler(
    ConversationHandler(
        entry_points=[CommandHandler("remove_admin", remove_admin)],
        states={GET_CHAT_ID_TO_REMOVE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_chat_id_to_remove)]},
        fallbacks=[],
    )
)
application.add_handler(CommandHandler("admins_list", admins_list))

# Config management
application.add_handler(
    ConversationHandler(
        entry_points=[CommandHandler("create_config", set_panel_domain)],
        states={
            GET_DOMAIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_domain)],
            GET_USERNAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_username)],
            GET_PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_password)],
        },
        fallbacks=[],
    )
)

# Limits management
application.add_handler(
    ConversationHandler(
        entry_points=[CommandHandler("set_special_limit", set_special_limit)],
        states={
            GET_SPECIAL_LIMIT: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_special_limit)],
            GET_LIMIT_NUMBER: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_limit_number)],
        },
        fallbacks=[],
    )
)
application.add_handler(CommandHandler("show_special_limit", show_special_limit_function))
application.add_handler(
    ConversationHandler(
        entry_points=[CommandHandler("set_general_limit_number", get_general_limit_number)],
        states={GET_GENERAL_LIMIT_NUMBER: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_general_limit_number_handler)]},
        fallbacks=[],
    )
)

# User management
application.add_handler(
    ConversationHandler(
        entry_points=[CommandHandler("set_except_user", set_except_users)],
        states={SET_EXCEPT_USERS: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_except_users_handler)]},
        fallbacks=[],
    )
)
application.add_handler(
    ConversationHandler(
        entry_points=[CommandHandler("remove_except_user", remove_except_user)],
        states={REMOVE_EXCEPT_USER: [MessageHandler(filters.TEXT & ~filters.COMMAND, remove_except_user_handler)]},
        fallbacks=[],
    )
)
application.add_handler(CommandHandler("show_except_users", show_except_users))

# Settings
application.add_handler(
    ConversationHandler(
        entry_points=[CommandHandler("set_ipinfo_token", set_ipinfo_token)],
        states={SET_IPINFO_TOKEN: [MessageHandler(filters.TEXT & ~filters.COMMAND, ipinfo_token_handler)]},
        fallbacks=[],
    )
)

# Monitoring
application.add_handler(CommandHandler("monitoring_status", monitoring_status))
application.add_handler(CommandHandler("monitoring_details", monitoring_details))
application.add_handler(CommandHandler("clear_monitoring", clear_monitoring))

# Reports
application.add_handler(CommandHandler("connection_report", connection_report_command))
application.add_handler(CommandHandler("node_usage", node_usage_report_command))
application.add_handler(CommandHandler("multi_device_users", multi_device_users_command))
application.add_handler(CommandHandler("users_by_node", users_by_node_command))
application.add_handler(CommandHandler("users_by_protocol", users_by_protocol_command))
application.add_handler(CommandHandler("ip_history_12h", ip_history_12h_command))
application.add_handler(CommandHandler("ip_history_48h", ip_history_48h_command))

# Backup
application.add_handler(CommandHandler("backup", send_backup))
application.add_handler(
    ConversationHandler(
        entry_points=[CommandHandler("restore", restore_config)],
        states={RESTORE_CONFIG: [MessageHandler(filters.Document.ALL, restore_config_handler)]},
        fallbacks=[],
    )
)

# Migrate backup (JSON to SQLite)
application.add_handler(
    ConversationHandler(
        entry_points=[CommandHandler("migrate_backup", migrate_backup_start)],
        states={
            MIGRATE_WAITING_FILE: [
                MessageHandler(filters.Document.ALL, migrate_backup_handler),
                MessageHandler(filters.TEXT & ~filters.COMMAND, migrate_backup_handler),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", migrate_backup_cancel),
        ],
        allow_reentry=True,
    )
)

# Punishment system
application.add_handler(CommandHandler("punishment_status", punishment_status))
application.add_handler(CommandHandler("punishment_toggle", punishment_toggle))
application.add_handler(CommandHandler("punishment_set_window", punishment_set_window))
application.add_handler(CommandHandler("punishment_set_steps", punishment_set_steps))
application.add_handler(CommandHandler("user_violations", user_violations))
application.add_handler(CommandHandler("clear_user_violations", clear_user_violations))

# Group filter
application.add_handler(CommandHandler("group_filter_status", group_filter_status))
application.add_handler(CommandHandler("group_filter_toggle", group_filter_toggle))
application.add_handler(CommandHandler("group_filter_mode", group_filter_mode))
application.add_handler(CommandHandler("group_filter_set", group_filter_set))
application.add_handler(CommandHandler("group_filter_add", group_filter_add))
application.add_handler(CommandHandler("group_filter_remove", group_filter_remove))

# Admin filter
application.add_handler(CommandHandler("admin_filter_status", admin_filter_status))
application.add_handler(CommandHandler("admin_filter_toggle", admin_filter_toggle))
application.add_handler(CommandHandler("admin_filter_mode", admin_filter_mode))
application.add_handler(CommandHandler("admin_filter_set", admin_filter_set))
application.add_handler(CommandHandler("admin_filter_add", admin_filter_add))
application.add_handler(CommandHandler("admin_filter_remove", admin_filter_remove))

# Fallback document handler (must be after all ConversationHandlers)
application.add_handler(MessageHandler(filters.Document.ALL, document_message_handler))


async def global_telegram_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle errors occurring during Telegram update processing."""
    error = context.error
    if not error:
        return

    # Gracefully ignore benign Telegram API errors
    if isinstance(error, BadRequest):
        err_msg = str(error).lower()
        if (
            "message is not modified" in err_msg
            or "query is too old" in err_msg
            or "message to edit not found" in err_msg
            or "chat not found" in err_msg
        ):
            bot_logger.debug(f"ℹ️ Benign Telegram BadRequest ignored: {error}")
            return

    bot_logger.warning(f"⚠️ Telegram handler error: {error}")


# Register global error handler for the Telegram Application
application.add_error_handler(global_telegram_error_handler)
