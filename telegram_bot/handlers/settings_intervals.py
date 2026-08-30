"""
Timing settings: how often users are checked and how long they stay active.

Each value has three entry points that must stay in sync: the ``/set_*``
conversation, the preset buttons, and the free-text input after "Custom".
"""

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler

from telegram_bot.constants import (
    CallbackData,
    GET_CHECK_INTERVAL,
    GET_TIME_TO_ACTIVE_USERS,
)
from telegram_bot.handlers.admin import check_admin_privilege
from telegram_bot.keyboards import create_back_to_main_keyboard
from telegram_bot.utils import save_check_interval, save_time_to_active_users


def create_interval_keyboard():
    """Create check interval options keyboard."""
    keyboard = [
        [
            InlineKeyboardButton("2 min", callback_data=CallbackData.INTERVAL_120),
            InlineKeyboardButton("3 min", callback_data=CallbackData.INTERVAL_180),
            InlineKeyboardButton("4 min", callback_data=CallbackData.INTERVAL_240),
        ],
        [InlineKeyboardButton("✏️ Custom", callback_data=CallbackData.INTERVAL_CUSTOM)],
        [InlineKeyboardButton("« Back to Settings", callback_data=CallbackData.SETTINGS_MENU)],
    ]
    return InlineKeyboardMarkup(keyboard)


def create_time_to_active_keyboard():
    """Create time to active options keyboard."""
    keyboard = [
        [
            InlineKeyboardButton("5 min", callback_data=CallbackData.TIME_300),
            InlineKeyboardButton("10 min", callback_data=CallbackData.TIME_600),
            InlineKeyboardButton("15 min", callback_data=CallbackData.TIME_900),
        ],
        [InlineKeyboardButton("✏️ Custom", callback_data=CallbackData.TIME_CUSTOM)],
        [InlineKeyboardButton("« Back to Settings", callback_data=CallbackData.SETTINGS_MENU)],
    ]
    return InlineKeyboardMarkup(keyboard)


async def set_check_interval(update: Update, _context: ContextTypes.DEFAULT_TYPE):
    """Get the 'check_interval' variable"""
    check = await check_admin_privilege(update)
    if check is not None:
        return check
    await update.message.reply_text(
        "Please send the check interval time in seconds (recommended: 240)"
    )
    return GET_CHECK_INTERVAL


async def check_interval_handler(update: Update, _context: ContextTypes.DEFAULT_TYPE):
    """Save the 'check_interval' variable"""
    try:
        check_interval = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_html(
            text=f"Wrong input: <code>{update.message.text.strip()}</code>\n"
            + "try again <b>/set_check_interval</b>"
        )
        return ConversationHandler.END
    await save_check_interval(check_interval)
    await update.message.reply_text(f"CHECK_INTERVAL set to {check_interval}")
    return ConversationHandler.END


async def set_time_to_active(update: Update, _context: ContextTypes.DEFAULT_TYPE):
    """Get the 'time_to_active' variable"""
    check = await check_admin_privilege(update)
    if check is not None:
        return check
    await update.message.reply_text(
        "Please send the time to active users in seconds (e.g., 600)"
    )
    return GET_TIME_TO_ACTIVE_USERS


async def time_to_active_handler(update: Update, _context: ContextTypes.DEFAULT_TYPE):
    """Save the 'time_to_active' variable"""
    try:
        time_to_active_users = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_html(
            text=f"Wrong input: <code>{update.message.text.strip()}</code>\n"
            + "try again <b>/set_time_to_active_users</b>"
        )
        return ConversationHandler.END
    await save_time_to_active_users(time_to_active_users)
    await update.message.reply_text(f"TIME_TO_ACTIVE_USERS set to {time_to_active_users}")
    return ConversationHandler.END


async def handle_interval_menu_callback(query, _context: ContextTypes.DEFAULT_TYPE):
    """Handle callback for interval menu display."""
    await query.edit_message_text(
        text="⏱️ <b>Check Interval</b>\n\nHow often should the bot check users:",
        reply_markup=create_interval_keyboard(),
        parse_mode="HTML"
    )


async def handle_interval_preset_callback(query, _context: ContextTypes.DEFAULT_TYPE, interval: int):
    """Handle callback for interval preset selection."""
    await save_check_interval(interval)
    await query.edit_message_text(
        text=f"✅ Check interval set to <b>{interval} seconds</b> ({interval // 60} min)",
        reply_markup=create_back_to_main_keyboard(),
        parse_mode="HTML"
    )


async def handle_interval_custom_callback(query, context: ContextTypes.DEFAULT_TYPE):
    """Handle callback for custom interval input."""
    context.user_data["waiting_for"] = "check_interval"
    await query.edit_message_text(
        text="⏱️ <b>Custom Check Interval</b>\n\nSend the interval in seconds:",
        parse_mode="HTML"
    )


async def handle_time_menu_callback(query, _context: ContextTypes.DEFAULT_TYPE):
    """Handle callback for time to active menu display."""
    await query.edit_message_text(
        text="⏰ <b>Time to Active</b>\n\nHow long users stay active:",
        reply_markup=create_time_to_active_keyboard(),
        parse_mode="HTML"
    )


async def handle_time_preset_callback(query, _context: ContextTypes.DEFAULT_TYPE, time_val: int):
    """Handle callback for time preset selection."""
    await save_time_to_active_users(time_val)
    await query.edit_message_text(
        text=f"✅ Time to active set to <b>{time_val} seconds</b> ({time_val // 60} min)",
        reply_markup=create_back_to_main_keyboard(),
        parse_mode="HTML"
    )


async def handle_time_custom_callback(query, context: ContextTypes.DEFAULT_TYPE):
    """Handle callback for custom time input."""
    context.user_data["waiting_for"] = "time_to_active"
    await query.edit_message_text(
        text="⏰ <b>Custom Time to Active</b>\n\nSend the time in seconds:",
        parse_mode="HTML"
    )


async def handle_check_interval_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle text input for check interval."""
    text = update.message.text.strip()
    try:
        interval = int(text)
        await save_check_interval(interval)
        await update.message.reply_html(
            text=f"✅ Check interval set to <b>{interval} seconds</b>",
            reply_markup=create_back_to_main_keyboard()
        )
    except ValueError:
        await update.message.reply_html(
            text="❌ Invalid number.",
            reply_markup=create_back_to_main_keyboard()
        )
    context.user_data["waiting_for"] = None


async def handle_time_to_active_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle text input for time to active."""
    text = update.message.text.strip()
    try:
        time_val = int(text)
        await save_time_to_active_users(time_val)
        await update.message.reply_html(
            text=f"✅ Time to active set to <b>{time_val} seconds</b>",
            reply_markup=create_back_to_main_keyboard()
        )
    except ValueError:
        await update.message.reply_html(
            text="❌ Invalid number.",
            reply_markup=create_back_to_main_keyboard()
        )
    context.user_data["waiting_for"] = None
