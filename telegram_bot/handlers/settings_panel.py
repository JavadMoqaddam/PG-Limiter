"""Guide authorized administrators to configure panel credentials in ENV."""

from html import escape

from telegram import Update
from telegram.ext import ContextTypes, ConversationHandler

from telegram_bot.handlers.admin import check_admin_privilege
from telegram_bot.input_owner import end_input


async def set_panel_domain(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Explain panel setup without collecting credentials through Telegram."""
    check = await check_admin_privilege(update)
    if check is not None:
        return check

    end_input(context)
    from utils.read_config import load_env_config

    panel_config = load_env_config().get("panel", {})
    domain = panel_config.get("domain", "")
    current_domain = f"\n\n<b>Current domain:</b> <code>{escape(domain)}</code>" if domain else ""
    await update.message.reply_html(
        text="⚙️ <b>Panel configuration</b>\n\n"
        "Set <code>PANEL_DOMAIN</code> and <code>PANEL_API_KEY</code> in the limiter's "
        "<code>.env</code> file, or use <code>pg-limiter config</code> on the server.\n"
        "Create a panel API key with the permissions required by the limiter. "
        "Keep the API key in the environment file and avoid sharing it in Telegram.\n"
        "Restart the limiter after changing these settings."
        f"{current_domain}"
    )
    return ConversationHandler.END
