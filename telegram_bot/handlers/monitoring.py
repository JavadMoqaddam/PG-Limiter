"""
Monitoring handlers for the Telegram bot.
Includes functions for viewing and managing user monitoring status.
"""

from telegram import Update
from telegram.ext import (
    ContextTypes,
    ConversationHandler,
)

from telegram_bot.keyboards import create_back_to_main_keyboard
from telegram_bot.handlers.admin import check_admin_privilege
from telegram_bot.utils import send_response


async def _send_response(update: Update, text: str, parse_mode: str = "HTML"):
    """Helper to send response for both message and callback query contexts."""
    reply_markup = create_back_to_main_keyboard() if getattr(update, "callback_query", None) else None
    await send_response(update, text=text, reply_markup=reply_markup, parse_mode=parse_mode)


async def monitoring_status(update: Update, _context: ContextTypes.DEFAULT_TYPE):
    """
    Shows the current monitoring status of users who are being watched after warnings.
    """
    check = await check_admin_privilege(update)
    if check:
        return check

    try:
        # Import here to avoid circular imports
        from utils.warning_system import warning_system

        if not warning_system.warnings:
            await _send_response(update, "🟢 No users are currently being monitored.")
            return ConversationHandler.END

        active_warnings = []
        expired_warnings = []

        for username, warning in warning_system.warnings.items():
            if warning.is_monitoring_active():
                remaining = warning.time_remaining()
                minutes = remaining // 60
                seconds = remaining % 60
                active_warnings.append(
                    f"• <code>{username}</code> - {warning.ip_count} IPs - {minutes}m {seconds}s remaining"
                )
            else:
                expired_warnings.append(username)

        message_parts = []

        if active_warnings:
            message_parts.append("🔍 <b>Currently Monitoring:</b>\n" + "\n".join(active_warnings))

        if expired_warnings:
            message_parts.append(f"⏰ <b>Expired Warnings:</b> {len(expired_warnings)} users")

        if not message_parts:
            message_parts.append("🟢 No active monitoring.")

        await _send_response(update, "\n\n".join(message_parts))

    except Exception as e:
        await _send_response(update, f"❌ Error getting monitoring status: {str(e)}")

    return ConversationHandler.END


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


async def monitoring_details(update: Update, _context: ContextTypes.DEFAULT_TYPE):
    """
    Shows detailed monitoring analytics for users being watched after warnings.
    """
    check = await check_admin_privilege(update)
    if check:
        return check

    try:
        # Import here to avoid circular imports
        from utils.warning_system import warning_system

        if not warning_system.warnings:
            await _send_response(update, "🟢 No users are currently being monitored.")
            return ConversationHandler.END

        message_parts = []

        for username, warning in warning_system.warnings.items():
            if warning.is_monitoring_active():
                remaining = warning.time_remaining()
                minutes = remaining // 60
                seconds = remaining % 60

                # Get analysis data
                analysis = await warning_system.analyze_user_activity_patterns(username)
                consistently_active_ips = analysis.get('consistently_active_ips', set())

                user_details = [
                    f"👤 <b>{username}</b>",
                    f"⏰ Time remaining: {minutes}m {seconds}s",
                    f"📊 Current IPs: {warning.ip_count}",
                    f"🔥 Consistently active IPs (4+ min): {len(consistently_active_ips)}",
                    f"📈 Monitoring snapshots: {analysis.get('total_snapshots', 0)}",
                    f"🔄 IP change frequency: {analysis.get('ip_change_frequency', 0):.2f}",
                    f"📊 Peak IP count: {analysis.get('peak_ip_count', 0)}",
                    f"📊 Average IP count: {analysis.get('average_ip_count', 0):.1f}"
                ]

                if consistently_active_ips:
                    user_details.append(f"🌐 Consistently active IPs: {', '.join(list(consistently_active_ips)[:5])}")
                    if len(consistently_active_ips) > 5:
                        user_details.append(f"... and {len(consistently_active_ips) - 5} more")

                message_parts.append("\n".join(user_details))

        if not message_parts:
            message_parts.append("🟢 No active monitoring.")

        final_message = "🔍 <b>Detailed Monitoring Analytics:</b>\n\n" + "\n\n".join(message_parts)

        await _send_response(update, final_message)

    except Exception as e:
        await _send_response(update, f"❌ Error getting monitoring details: {str(e)}")

    return ConversationHandler.END
