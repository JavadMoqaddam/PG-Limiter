"""
Trust data reset.

Wipes the warning records and the 12h/24h punishment history for every user, so
everyone starts from a clean trust score. Destructive by design and only
reachable behind an explicit confirmation button.
"""

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from telegram_bot.constants import CallbackData


async def trust_reset_menu_callback(query, context: ContextTypes.DEFAULT_TYPE):
    """Handle trust reset menu callback."""
    from utils.warning_system import warning_system

    warnings_count = len(warning_system.warnings)
    history_count = len(warning_system.warning_history)

    await query.edit_message_text(
        text=(
            "🗑️ <b>Reset Trust Data</b>\n\n"
            f"<b>Active Warnings:</b> {warnings_count} users\n"
            f"<b>Warning History:</b> {history_count} users\n\n"
            "<b>What this does:</b>\n"
            "• Clears all active monitoring warnings\n"
            "• Clears trust score history (12h/24h counters)\n"
            "• Users will start fresh with default trust score\n\n"
            "⚠️ <b>Warning:</b> This will reset ALL trust data for ALL users. "
            "Users who were flagged as suspicious will get a clean slate."
        ),
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🗑️ Reset ALL Trust Data", callback_data=CallbackData.TRUST_RESET_ALL)],
            [InlineKeyboardButton("« Back to Settings", callback_data=CallbackData.SETTINGS_MENU)]
        ]),
        parse_mode="HTML"
    )


async def trust_reset_all_callback(query, context: ContextTypes.DEFAULT_TYPE):
    """Handle reset all trust data callback."""
    from utils.warning_system import warning_system

    try:
        warnings_cleared, history_cleared = await warning_system.clear_all_trust_data()

        await query.answer("✅ All trust data cleared")

        await query.edit_message_text(
            text=(
                "✅ <b>Trust Data Cleared</b>\n\n"
                f"<b>Warnings cleared:</b> {warnings_cleared}\n"
                f"<b>History entries cleared:</b> {history_cleared}\n\n"
                "All users now start with a fresh trust score."
            ),
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("« Back to Settings", callback_data=CallbackData.SETTINGS_MENU)]
            ]),
            parse_mode="HTML"
        )
    except Exception as e:
        await query.answer("❌ Error clearing trust data")
        await query.edit_message_text(
            text=f"❌ Error: {str(e)}",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("« Back", callback_data=CallbackData.TRUST_RESET_MENU)]
            ]),
            parse_mode="HTML"
        )
