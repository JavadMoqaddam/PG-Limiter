"""
Display and lookup settings: country filter, ipinfo token, report detail level.

These are the toggles that change what the reports show and which IPs are
counted at all - the country filter is the one with teeth, since an IP outside
the configured country is never counted as a device.

Live here: ``set_ipinfo_token`` / ``ipinfo_token_handler`` (registered as a
conversation in main.py), ``handle_ipinfo_callback``,
``handle_enhanced_menu_callback`` and ``handle_enhanced_toggle_callback``.

Unreachable here, verified against main.py's ``CALLBACK_ROUTES`` and handler
registrations: ``set_country_code`` / ``country_code_handler`` (no
``CommandHandler``, ``SET_COUNTRY_CODE`` belongs to no ``ConversationHandler``),
``handle_country_menu_callback`` / ``handle_country_selection_callback`` (the
``COUNTRY_*`` callbacks are routed nowhere and no keyboard emits them), and
``handle_single_ip_menu_callback`` / ``handle_single_ip_toggle_callback`` (same
for ``SINGLE_IP_*``). The country filter is set through ``COUNTRY_CODE`` in the
environment instead. Documented rather than deleted so the next reader does not
have to re-derive it, and so nobody edits a screen that cannot be opened.
"""

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler

from telegram_bot.constants import (
    CallbackData,
    SET_COUNTRY_CODE,
    SET_IPINFO_TOKEN,
)
from telegram_bot.handlers.admin import check_admin_privilege
from telegram_bot.keyboards import create_back_to_main_keyboard
from telegram_bot.utils import write_country_code_json
from utils.read_config import read_config, save_config_value


def create_country_keyboard():
    """Create country code options keyboard."""
    keyboard = [
        [
            InlineKeyboardButton("🇮🇷 Iran", callback_data=CallbackData.COUNTRY_IR),
            InlineKeyboardButton("🇷🇺 Russia", callback_data=CallbackData.COUNTRY_RU),
        ],
        [
            InlineKeyboardButton("🇨🇳 China", callback_data=CallbackData.COUNTRY_CN),
            InlineKeyboardButton("🌐 None", callback_data=CallbackData.COUNTRY_NONE),
        ],
        [InlineKeyboardButton("« Back to Settings", callback_data=CallbackData.SETTINGS_MENU)],
    ]
    return InlineKeyboardMarkup(keyboard)


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


def create_single_ip_keyboard():
    """Create single IP users toggle keyboard."""
    keyboard = [
        [
            InlineKeyboardButton("✅ ON", callback_data=CallbackData.SINGLE_IP_ON),
            InlineKeyboardButton("❌ OFF", callback_data=CallbackData.SINGLE_IP_OFF),
        ],
        [InlineKeyboardButton("« Back to Settings", callback_data=CallbackData.SETTINGS_MENU)],
    ]
    return InlineKeyboardMarkup(keyboard)


async def set_country_code(update: Update, _context: ContextTypes.DEFAULT_TYPE):
    """Set the country code for the bot."""
    check = await check_admin_privilege(update)
    if check is not None:
        return check
    await update.message.reply_html(
        "Select country code:\n"
        + "1. <code>IR</code> (Iran)\n"
        + "2. <code>RU</code> (Russia)\n"
        + "3. <code>CN</code> (China)\n"
        + "4. <code>None</code>\n"
        + "Send number: <code>1</code>, <code>2</code>, <code>3</code>, or <code>4</code>"
    )
    return SET_COUNTRY_CODE


async def country_code_handler(update: Update, _context: ContextTypes.DEFAULT_TYPE):
    """Write the country code to the config file."""
    country_code = update.message.text.strip()
    country_codes = {"1": "IR", "2": "RU", "3": "CN", "4": "None"}
    selected_country = country_codes.get(country_code, "None")
    await write_country_code_json(selected_country)
    await update.message.reply_html(
        f"Country code <code>{selected_country}</code> set successfully!"
    )
    return ConversationHandler.END


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
        await save_ipinfo_token("")
        await update.message.reply_html("✅ IPINFO_TOKEN removed!")
        return ConversationHandler.END

    if len(token) < 10:
        await update.message.reply_html(
            "❌ Invalid token format!\nTry again with <b>/set_ipinfo_token</b>"
        )
        return ConversationHandler.END

    await save_ipinfo_token(token)
    await update.message.reply_html("✅ IPINFO_TOKEN set successfully!")
    return ConversationHandler.END


async def save_ipinfo_token(token: str):
    """Save the ipinfo.io token to database for persistent storage across restarts."""
    try:
        await save_config_value("ipinfo_token", token)
        return True
    except Exception as e:
        print(f"Error saving ipinfo token: {e}")
        return False


async def handle_country_menu_callback(query, _context: ContextTypes.DEFAULT_TYPE):
    """Handle callback for country menu display."""
    await query.edit_message_text(
        text="🌍 <b>Select Country</b>\n\nOnly IPs from the selected country will be counted:",
        reply_markup=create_country_keyboard(),
        parse_mode="HTML"
    )


async def handle_country_selection_callback(query, _context: ContextTypes.DEFAULT_TYPE, country_code: str):
    """Handle callback for country selection."""
    country_names = {"IR": "🇮🇷 Iran", "RU": "🇷🇺 Russia", "CN": "🇨🇳 China", "None": "🌐 None"}
    await write_country_code_json(country_code)
    await query.edit_message_text(
        text=f"✅ Country set to <b>{country_names.get(country_code, country_code)}</b>",
        reply_markup=create_back_to_main_keyboard(),
        parse_mode="HTML"
    )


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


async def handle_single_ip_menu_callback(query, _context: ContextTypes.DEFAULT_TYPE):
    """Handle callback for single IP menu display."""
    try:
        config = await read_config()
        value = config.get("show_single_ip_users", False)
        status = "ON ✅" if value else "OFF ❌"
    except Exception:
        status = "Unknown"
    await query.edit_message_text(
        text=f"1️⃣ <b>Single IP Users</b>\n\nCurrently: <b>{status}</b>\n\n"
             + "• <b>ON</b>: Include users with 1 IP in reports\n"
             + "• <b>OFF</b>: Only show users with multiple IPs",
        reply_markup=create_single_ip_keyboard(),
        parse_mode="HTML"
    )


async def handle_single_ip_toggle_callback(query, _context: ContextTypes.DEFAULT_TYPE, enable: bool):
    """Handle callback for single IP toggle."""
    try:
        await save_config_value("show_single_ip_users", str(enable).lower())
        status = "ON ✅" if enable else "OFF ❌"
        await query.edit_message_text(
            text=f"1️⃣ <b>Single IP Users</b>\n\nCurrently: <b>{status}</b>\n\n"
                 + "• <b>ON</b>: Include users with 1 IP in reports\n"
                 + "• <b>OFF</b>: Only show users with multiple IPs",
            reply_markup=create_single_ip_keyboard(),
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
        await save_ipinfo_token("")
        await update.message.reply_html(
            text="✅ IPInfo token removed!",
            reply_markup=create_back_to_main_keyboard()
        )
    elif len(text) < 10:
        await update.message.reply_html(
            text="❌ Invalid token format!",
            reply_markup=create_back_to_main_keyboard()
        )
    else:
        await save_ipinfo_token(text)
        await update.message.reply_html(
            text="✅ IPInfo token set successfully!",
            reply_markup=create_back_to_main_keyboard()
        )
    context.user_data["waiting_for"] = None
