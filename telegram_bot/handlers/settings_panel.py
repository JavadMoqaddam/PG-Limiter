"""
Panel credentials conversation: domain, username and password.

When the credentials come from ``.env`` the conversation refuses to start, so
the file stays the single source of truth.
"""

from telegram import Update
from telegram.ext import ContextTypes, ConversationHandler

from telegram_bot.constants import (
    GET_DOMAIN,
    GET_USERNAME,
    GET_PASSWORD,
)
from telegram_bot.handlers.admin import check_admin_privilege
from telegram_bot.utils import add_base_information


async def set_panel_domain(update: Update, _context: ContextTypes.DEFAULT_TYPE):
    """Add panel domain, username, and password to the config file."""
    check = await check_admin_privilege(update)
    if check is not None:
        return check

    # Check if environment variables are already set
    from utils.read_config import load_env_config
    env_config = load_env_config()
    panel_config = env_config.get("panel", {})
    domain = panel_config.get("domain")
    password = panel_config.get("password")

    if domain and password:
        await update.message.reply_html(
            text="⚠️ Panel credentials are stored in <code>.env</code> file.\n"
            + "To change them, edit the .env file or use:\n"
            + "<code>pg-limiter config</code>\n\n"
            + f"<b>Current domain:</b> <code>{domain}</code>"
        )
        return ConversationHandler.END

    await update.message.reply_html(
        text="Send your <b>panel address</b>\n"
        + "Format: <code>sub.domain.com:8333</code>\n"
        + "<b>without</b> <code>https://</code> or <code>http://</code>",
    )
    return GET_DOMAIN


async def get_domain(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Get panel domain from user"""
    context.user_data["domain"] = update.message.text.strip()
    await update.message.reply_text("Send Your Username: (For example: 'admin')")
    return GET_USERNAME


async def get_username(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Get panel username from user"""
    context.user_data["username"] = update.message.text.strip()
    await update.message.reply_text("Send Your Password:")
    return GET_PASSWORD


async def get_password(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Get panel password from user and save config"""
    context.user_data["password"] = update.message.text.strip()
    await update.message.reply_text("Please wait to check panel credentials...")
    try:
        await add_base_information(
            context.user_data["domain"],
            context.user_data["password"],
            context.user_data["username"],
        )
        await update.message.reply_text("Config saved successfully 🎊")
    except ValueError:
        await update.message.reply_html(
            text="<b>Error with your information!</b>\n"
            + f"Domain: <code>{context.user_data['domain']}</code>\n"
            + f"Username: <code>{context.user_data['username']}</code>\n"
            + "Try again /create_config",
        )
    return ConversationHandler.END
