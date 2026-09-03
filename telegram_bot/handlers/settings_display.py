"""
Display and lookup settings: the ipinfo.io token and the report detail level.

The token screens store the ipinfo.io API key that turns an IP into a country
and an ISP, and accept ``remove`` to clear it. There are two ways in, sharing a
single save helper: the ``/set_ipinfo_token`` command conversation, and the
settings keyboard, which parks the chat on a ``waiting_for`` marker and lets
main.py's free-text router hand the next message back here. The enhanced-details
toggle is the simpler one - it decides whether a report names the node, ID and
protocol behind each connection or just lists bare IP addresses.

The country filter is not configured from the bot. It comes from the
``COUNTRY_CODE`` environment variable, read at config load, and the menus that
once pretended to set it here were unreachable and have been removed.
"""

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler

from telegram_bot.constants import (
    CallbackData,
    SET_IPINFO_TOKEN,
)
from telegram_bot.handlers.admin import check_admin_privilege
from telegram_bot.keyboards import create_back_to_main_keyboard
from utils.read_config import read_config, save_config_value

# Shown instead of a confirmation when the database write did not commit. Saying
# "saved" for a write that failed is worse than saying nothing.
SAVE_FAILED_NOTE = (
    "❌ Could not write the setting to the database - it is unchanged. "
    "Check the container logs."
)


def create_enhanced_details_keyboard():
    """Create enhanced details toggle keyboard."""
    keyboard = [
        [
            InlineKeyboardButton("✅ ON", callback_data=CallbackData.ENHANCED_ON),
            InlineKeyboardButton("❌ OFF", callback_data=CallbackData.ENHANCED_OFF),
        ],
        [InlineKeyboardButton("« Back to Settings", callback_data=CallbackData.SETTINGS_MENU)],
    ]
    return InlineKeyboardMarkup(keyboard)


async def set_ipinfo_token(update: Update, _context: ContextTypes.DEFAULT_TYPE):
    """Set the ipinfo.io API token."""
    check = await check_admin_privilege(update)
    if check is not None:
        return check
    await update.message.reply_html(
        "Send your ipinfo.io API token:\n\n"
        + "Get one at https://ipinfo.io\n"
        + "Or send <code>remove</code> to remove the token"
    )
    return SET_IPINFO_TOKEN


async def ipinfo_token_handler(update: Update, _context: ContextTypes.DEFAULT_TYPE):
    """Write the ipinfo.io token to the config file."""
    token = update.message.text.strip()

    if token.lower() == "remove":
        saved = await save_ipinfo_token("")
        await update.message.reply_html(
            "✅ IPINFO_TOKEN removed!" if saved else SAVE_FAILED_NOTE
        )
        return ConversationHandler.END

    if len(token) < 10:
        await update.message.reply_html(
            "❌ Invalid token format!\nTry again with <b>/set_ipinfo_token</b>"
        )
        return ConversationHandler.END

    saved = await save_ipinfo_token(token)
    await update.message.reply_html(
        "✅ IPINFO_TOKEN set successfully!" if saved else SAVE_FAILED_NOTE
    )
    return ConversationHandler.END


async def save_ipinfo_token(token: str) -> bool:
    """
    Save the ipinfo.io token to the database so it survives a restart.

    Returns whether the write committed. It used to return ``True`` unconditionally -
    ``save_config_value`` reports a failed write by returning ``False``, not by
    raising, so the ``except`` branch never ran and every caller told the admin the
    token was stored whether or not it was.
    """
    return await save_config_value("ipinfo_token", token)


async def handle_enhanced_menu_callback(query, _context: ContextTypes.DEFAULT_TYPE):
    """Handle callback for enhanced details menu display."""
    try:
        config = await read_config()
        value = config.get("enhanced_details", True)
        status = "ON ✅" if value else "OFF ❌"
    except Exception:
        status = "Unknown"
    await query.edit_message_text(
        text=f"📋 <b>Enhanced Details</b>\n\nCurrently: <b>{status}</b>\n\n"
             + "• <b>ON</b>: Shows node names, IDs, and protocols\n"
             + "• <b>OFF</b>: Shows only IP addresses",
        reply_markup=create_enhanced_details_keyboard(),
        parse_mode="HTML"
    )


async def handle_enhanced_toggle_callback(query, _context: ContextTypes.DEFAULT_TYPE, enable: bool):
    """Handle callback for enhanced details toggle."""
    try:
        await save_config_value("enhanced_details", str(enable).lower())
        status = "ON ✅" if enable else "OFF ❌"
        await query.edit_message_text(
            text=f"📋 <b>Enhanced Details</b>\n\nCurrently: <b>{status}</b>\n\n"
                 + "• <b>ON</b>: Shows node names, IDs, and protocols\n"
                 + "• <b>OFF</b>: Shows only IP addresses",
            reply_markup=create_enhanced_details_keyboard(),
            parse_mode="HTML"
        )
    except Exception as e:
        await query.edit_message_text(
            text=f"❌ Error: {e}",
            reply_markup=create_back_to_main_keyboard(),
            parse_mode="HTML"
        )


async def handle_ipinfo_callback(query, context: ContextTypes.DEFAULT_TYPE):
    """Handle callback for IPInfo token input."""
    context.user_data["waiting_for"] = "ipinfo_token"
    await query.edit_message_text(
        text="🔑 <b>IPInfo Token</b>\n\n"
             + "Send your ipinfo.io API token:\n\n"
             + "Get one at: https://ipinfo.io\n\n"
             + "Or send <code>remove</code> to remove the token\n\n"
             + "<i>Or click Back to cancel.</i>",
        reply_markup=create_back_to_main_keyboard(),
        parse_mode="HTML"
    )


async def handle_ipinfo_token_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle text input for IPInfo token."""
    text = update.message.text.strip()
    if text.lower() == "remove":
        saved = await save_ipinfo_token("")
        await update.message.reply_html(
            text="✅ IPInfo token removed!" if saved else SAVE_FAILED_NOTE,
            reply_markup=create_back_to_main_keyboard()
        )
    elif len(text) < 10:
        await update.message.reply_html(
            text="❌ Invalid token format!",
            reply_markup=create_back_to_main_keyboard()
        )
    else:
        saved = await save_ipinfo_token(text)
        await update.message.reply_html(
            text="✅ IPInfo token set successfully!" if saved else SAVE_FAILED_NOTE,
            reply_markup=create_back_to_main_keyboard()
        )
    context.user_data["waiting_for"] = None
