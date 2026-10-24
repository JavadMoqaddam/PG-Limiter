"""Legacy status shortcuts and the existing authorized warning-clear command."""

from telegram import Update
from telegram.ext import ContextTypes, ConversationHandler

from telegram_bot.handlers.admin import check_admin_privilege
from telegram_bot.handlers.status_reports import (
    deliver_card,
    health_report_command,
    status_command,
)
from telegram_bot.keyboards import create_back_to_main_keyboard

monitoring_status = status_command
monitoring_details = health_report_command


async def _send_response(update: Update, text: str, parse_mode: str = "HTML"):
    reply_markup = create_back_to_main_keyboard() if getattr(update, "callback_query", None) else None
    await deliver_card(update, text, reply_markup)


async def clear_monitoring(update: Update, _context: ContextTypes.DEFAULT_TYPE):
    """
    Clears all monitoring warnings (admin only).
    """
    check = await check_admin_privilege(update)
    if check:
        return check

    try:
        from utils.warning_system import warning_system

        count = len(warning_system.warnings)
        warning_system.warnings.clear()
        await warning_system.save_warnings()

        await _send_response(update, f"✅ Cleared {count} monitoring warnings.")

    except Exception as e:
        await _send_response(update, f"❌ Error clearing monitoring: {str(e)}")

    return ConversationHandler.END
