"""
Group settings for disabling and re-enabling users.

Two independent group choices live here:
  * the **disabled group** a punished user is moved into when
    ``disable_method`` is ``group`` instead of ``status``;
  * the **fallback group** a user is put back into when their original groups
    can no longer be resolved on re-enable, so nobody ends up group-less.
"""

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from telegram_bot.constants import CallbackData
from utils.read_config import read_config, save_config_value


async def _get_groups_from_panel():
    """Helper to get groups from panel."""
    try:
        from utils.user_group_filter import get_all_groups
        from utils.types import PanelType

        config_data = await read_config()
        panel_config = config_data.get("panel", {})
        panel_data = PanelType(
            panel_config.get("username", ""),
            panel_config.get("password", ""),
            panel_config.get("domain", "")
        )

        groups = await get_all_groups(panel_data)
        return groups, config_data
    except Exception as e:
        return [], {}


def create_disable_group_keyboard(groups: list, current_group_id: int = None):
    """Create keyboard for selecting disabled group."""
    keyboard = []

    for group in groups:
        gid = group.get("id", 0)
        name = group.get("name", "Unknown")
        is_selected = gid == current_group_id
        prefix = "✅" if is_selected else "⬜"

        keyboard.append([
            InlineKeyboardButton(
                f"{prefix} {name} (ID: {gid})",
                callback_data=f"select_disabled_group:{gid}"
            )
        ])

    keyboard.append([InlineKeyboardButton("🔄 Refresh", callback_data=CallbackData.DISABLE_BY_GROUP)])
    keyboard.append([InlineKeyboardButton("« Back to Disable Method", callback_data=CallbackData.DISABLE_METHOD_MENU)])

    return InlineKeyboardMarkup(keyboard)


async def handle_disable_by_group_callback(query, _context: ContextTypes.DEFAULT_TYPE):
    """Handle callback for selecting group to use for disabled users."""
    groups, config_data = await _get_groups_from_panel()

    if not groups:
        try:
            await query.edit_message_text(
                text="📁 <b>Disable by Group</b>\n\n"
                     "❌ Could not load groups from panel.\n"
                     "Please check your panel connection.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔄 Retry", callback_data=CallbackData.DISABLE_BY_GROUP)],
                    [InlineKeyboardButton("« Back", callback_data=CallbackData.DISABLE_METHOD_MENU)]
                ]),
                parse_mode="HTML"
            )
        except BadRequest as e:
            if "message is not modified" not in str(e).lower():
                raise
        return

    # Get current disabled group ID
    current_group_id = config_data.get("disabled_group_id")
    if current_group_id:
        try:
            current_group_id = int(current_group_id)
        except (ValueError, TypeError):
            current_group_id = None

    keyboard = create_disable_group_keyboard(groups, current_group_id)

    try:
        await query.edit_message_text(
            text="📁 <b>Disable by Group</b>\n\n"
                 "Select the group where disabled users will be moved:\n\n"
                 "<i>When a user exceeds their IP limit, they will be moved to this group.</i>",
            reply_markup=keyboard,
            parse_mode="HTML"
        )
    except BadRequest as e:
        if "message is not modified" not in str(e).lower():
            raise


async def handle_select_disabled_group_callback(query, _context: ContextTypes.DEFAULT_TYPE, group_id: int):
    """Handle callback for selecting specific disabled group."""
    from telegram_bot.keyboards import create_disable_method_keyboard

    try:
        await save_config_value("disable_method", "group")
        await save_config_value("disabled_group_id", str(group_id))

        # Get group name for confirmation
        groups, _ = await _get_groups_from_panel()
        group_name = "Unknown"
        for group in groups:
            if group.get("id") == group_id:
                group_name = group.get("name", "Unknown")
                break

        await query.edit_message_text(
            text="🚫 <b>Disable Method</b>\n\n"
                 f"✅ Method set to <b>By Group</b>\n"
                 f"Group: <b>{group_name}</b> (ID: {group_id})\n\n"
                 "• <b>By Status</b>: Set user status to 'disabled'\n"
                 "• <b>By Group</b>: Move user to a disabled group",
            reply_markup=create_disable_method_keyboard("group", group_name),
            parse_mode="HTML"
        )

    except Exception as e:
        await query.edit_message_text(
            text=f"❌ Error: {str(e)}",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("« Back", callback_data=CallbackData.DISABLE_BY_GROUP)]
            ]),
            parse_mode="HTML"
        )


def create_fallback_group_keyboard(groups: list, current_group_id: int = None):
    """Create keyboard for selecting fallback group."""
    keyboard = []

    for group in groups:
        gid = group.get("id", 0)
        name = group.get("name", "Unknown")
        is_selected = gid == current_group_id
        prefix = "✅" if is_selected else "⬜"

        keyboard.append([
            InlineKeyboardButton(
                f"{prefix} {name} (ID: {gid})",
                callback_data=f"select_fallback_group:{gid}"
            )
        ])

    # Add option to clear fallback group
    if current_group_id:
        keyboard.append([InlineKeyboardButton("❌ Clear Fallback Group", callback_data=CallbackData.CLEAR_FALLBACK_GROUP)])

    keyboard.append([InlineKeyboardButton("🔄 Refresh", callback_data=CallbackData.FALLBACK_GROUP_MENU)])
    keyboard.append([InlineKeyboardButton("« Back to Disable Method", callback_data=CallbackData.DISABLE_METHOD_MENU)])

    return InlineKeyboardMarkup(keyboard)


async def handle_fallback_group_menu_callback(query, _context: ContextTypes.DEFAULT_TYPE):
    """Handle callback for selecting fallback group."""
    groups, config_data = await _get_groups_from_panel()

    if not groups:
        try:
            await query.edit_message_text(
                text="🔄 <b>Fallback Group</b>\n\n"
                     "❌ Could not load groups from panel.\n"
                     "Please check your panel connection.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔄 Retry", callback_data=CallbackData.FALLBACK_GROUP_MENU)],
                    [InlineKeyboardButton("« Back", callback_data=CallbackData.DISABLE_METHOD_MENU)]
                ]),
                parse_mode="HTML"
            )
        except BadRequest as e:
            if "message is not modified" not in str(e).lower():
                raise
        return

    # Get current fallback group ID
    current_group_id = config_data.get("fallback_group_id")
    if current_group_id:
        try:
            current_group_id = int(current_group_id)
        except (ValueError, TypeError):
            current_group_id = None

    keyboard = create_fallback_group_keyboard(groups, current_group_id)

    current_name = "Not set"
    if current_group_id:
        for group in groups:
            if group.get("id") == current_group_id:
                current_name = group.get("name", "Unknown")
                break

    try:
        await query.edit_message_text(
            text="🔄 <b>Fallback Group</b>\n\n"
                 f"Current: <b>{current_name}</b>\n\n"
                 "Select the group that will be assigned to users when:\n"
                 "• Their original groups cannot be found when re-enabling\n"
                 "• All active users should have this group\n\n"
                 "<i>This ensures all enabled users have at least one valid group.</i>",
            reply_markup=keyboard,
            parse_mode="HTML"
        )
    except BadRequest as e:
        if "message is not modified" not in str(e).lower():
            raise


async def handle_select_fallback_group_callback(query, _context: ContextTypes.DEFAULT_TYPE, group_id: int):
    """Handle callback for selecting specific fallback group."""
    from telegram_bot.keyboards import create_disable_method_keyboard

    try:
        await save_config_value("fallback_group_id", str(group_id))

        # Get group name for confirmation
        groups, config_data = await _get_groups_from_panel()
        group_name = "Unknown"
        disabled_group_name = None
        for group in groups:
            if group.get("id") == group_id:
                group_name = group.get("name", "Unknown")
            disabled_group_id = config_data.get("disabled_group_id")
            if disabled_group_id and group.get("id") == int(disabled_group_id):
                disabled_group_name = group.get("name", "Unknown")

        current_method = config_data.get("disable_method", "status")

        await query.edit_message_text(
            text="🚫 <b>Disable Method</b>\n\n"
                 f"✅ Fallback group set to <b>{group_name}</b> (ID: {group_id})\n\n"
                 "• <b>By Status</b>: Set user status to 'disabled'\n"
                 "• <b>By Group</b>: Move user to a disabled group\n"
                 "• <b>Fallback Group</b>: Default group for re-enabled users",
            reply_markup=create_disable_method_keyboard(current_method, disabled_group_name, group_name),
            parse_mode="HTML"
        )

    except Exception as e:
        await query.edit_message_text(
            text=f"❌ Error: {str(e)}",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("« Back", callback_data=CallbackData.FALLBACK_GROUP_MENU)]
            ]),
            parse_mode="HTML"
        )


async def handle_clear_fallback_group_callback(query, _context: ContextTypes.DEFAULT_TYPE):
    """Handle callback for clearing fallback group."""
    from telegram_bot.keyboards import create_disable_method_keyboard

    try:
        await save_config_value("fallback_group_id", "")

        groups, config_data = await _get_groups_from_panel()
        current_method = config_data.get("disable_method", "status")
        disabled_group_name = None
        disabled_group_id = config_data.get("disabled_group_id")
        if disabled_group_id:
            for group in groups:
                if group.get("id") == int(disabled_group_id):
                    disabled_group_name = group.get("name", "Unknown")
                    break

        await query.edit_message_text(
            text="🚫 <b>Disable Method</b>\n\n"
                 "✅ Fallback group has been cleared.\n\n"
                 "• <b>By Status</b>: Set user status to 'disabled'\n"
                 "• <b>By Group</b>: Move user to a disabled group\n"
                 "• <b>Fallback Group</b>: Default group for re-enabled users",
            reply_markup=create_disable_method_keyboard(current_method, disabled_group_name, None),
            parse_mode="HTML"
        )

    except Exception as e:
        await query.edit_message_text(
            text=f"❌ Error: {str(e)}",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("« Back", callback_data=CallbackData.FALLBACK_GROUP_MENU)]
            ]),
            parse_mode="HTML"
        )
