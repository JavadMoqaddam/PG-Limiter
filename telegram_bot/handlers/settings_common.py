"""
Pieces shared by every settings submenu.

The settings handlers live in ``settings_*.py`` modules per domain;
[settings.py](telegram_bot/handlers/settings.py) re-exports all of them so
existing imports and the callback router keep working unchanged.
"""

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from telegram_bot.constants import CallbackData
from telegram_bot.keyboards import create_settings_menu_keyboard


def create_back_to_settings_keyboard():
    """Create a keyboard with only a back to settings button."""
    keyboard = [
        [InlineKeyboardButton("« Back to Settings", callback_data=CallbackData.SETTINGS_MENU)],
    ]
    return InlineKeyboardMarkup(keyboard)


async def handle_settings_menu_callback(query, _context: ContextTypes.DEFAULT_TYPE):
    """
    Handle callback for settings menu display.

    NOT ROUTED. ``SETTINGS_MENU`` and ``BACK_SETTINGS`` are both handled by inline
    lambdas in main.py's ``CALLBACK_ROUTES``, so this copy never runs. It is left
    here because it is the readable version of what those lambdas do - but it is a
    divergence trap: editing this function changes nothing at runtime.
    """
    await query.edit_message_text(
        text="⚙️ <b>Settings Menu</b>\n\nConfigure your bot settings:",
        reply_markup=create_settings_menu_keyboard(),
        parse_mode="HTML"
    )
