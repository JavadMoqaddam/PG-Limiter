"""
CDN mode for inbounds.

When an inbound sits behind a CDN every user arrives from the CDN's edge
addresses, so the real client IP has to be taken from the
``X-Forwarded-For`` header instead. The inbound list here is what
[utils/device_count.py](utils/device_count.py) collapses into a single device.
For whole nodes behind a CDN use Node Settings instead.
"""

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler

from telegram_bot.constants import CallbackData, SET_CDN_INBOUND
from utils.read_config import read_config, save_config_value


def create_cdn_mode_keyboard(use_xff: bool = True, provider: str = "cloudflare"):
    """Create CDN mode settings keyboard."""
    xff_status = "✅" if use_xff else "❌"
    keyboard = [
        [InlineKeyboardButton("➕ Add Inbound", callback_data=CallbackData.CDN_MODE_ADD)],
        [InlineKeyboardButton("➖ Remove Inbound", callback_data=CallbackData.CDN_MODE_REMOVE)],
        [InlineKeyboardButton(f"{xff_status} Use X-Forwarded-For", callback_data=CallbackData.CDN_USE_XFF_TOGGLE)],
        [InlineKeyboardButton(f"📡 Provider: {provider.title()}", callback_data=CallbackData.CDN_PROVIDER_MENU)],
        [InlineKeyboardButton("🗑️ Clear All", callback_data=CallbackData.CDN_MODE_CLEAR)],
        [InlineKeyboardButton("« Back to Settings", callback_data=CallbackData.SETTINGS_MENU)],
    ]
    return InlineKeyboardMarkup(keyboard)


async def cdn_mode_menu_callback(query, context: ContextTypes.DEFAULT_TYPE):
    """Handle CDN mode menu callback."""
    config_data = await read_config()
    cdn_inbounds = config_data.get("cdn_inbounds", [])
    use_xff = config_data.get("cdn_use_xff", True)
    provider = config_data.get("cdn_provider", "cloudflare")

    if cdn_inbounds:
        inbounds_list = "\n".join(f"  • <code>{inbound}</code>" for inbound in cdn_inbounds)
        status_text = f"<b>CDN Inbounds ({len(cdn_inbounds)}):</b>\n{inbounds_list}"
    else:
        status_text = "<i>No inbounds in CDN mode</i>"

    xff_status = "✅ Enabled" if use_xff else "❌ Disabled"

    await query.edit_message_text(
        text=(
            "☁️ <b>CDN Mode Settings</b>\n\n"
            f"{status_text}\n\n"
            f"<b>Provider:</b> {provider.title()}\n"
            f"<b>X-Forwarded-For:</b> {xff_status}\n\n"
            "<b>How it works:</b>\n"
            "When an inbound is in CDN mode and X-Forwarded-For is enabled, "
            "the system will extract the <b>real user IP</b> from the "
            "X-Forwarded-For header instead of using the CDN edge IP.\n\n"
            "This allows accurate IP counting for users behind CDN."
        ),
        reply_markup=create_cdn_mode_keyboard(use_xff, provider),
        parse_mode="HTML"
    )


async def cdn_mode_add_callback(query, context: ContextTypes.DEFAULT_TYPE):
    """Handle adding an inbound to CDN mode."""
    await query.edit_message_text(
        text=(
            "➕ <b>Add Inbound to CDN Mode</b>\n\n"
            "Send the <b>exact</b> inbound protocol name to add.\n\n"
            "Examples:\n"
            "• <code>VLESS XHTTP TLS</code>\n"
            "• <code>Vmess CDN</code>\n"
            "• <code>Trojan WS TLS</code>\n\n"
            "💡 <i>You can find inbound names in the connection report or user logs.</i>\n\n"
            "Send /cancel to cancel."
        ),
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("« Cancel", callback_data=CallbackData.CDN_MODE_MENU)]
        ]),
        parse_mode="HTML"
    )

    context.user_data["cdn_mode_action"] = "add"
    return SET_CDN_INBOUND


async def cdn_mode_remove_callback(query, context: ContextTypes.DEFAULT_TYPE):
    """Handle removing an inbound from CDN mode."""
    config_data = await read_config()
    cdn_inbounds = config_data.get("cdn_inbounds", [])
    use_xff = config_data.get("cdn_use_xff", True)
    provider = config_data.get("cdn_provider", "cloudflare")

    if not cdn_inbounds:
        await query.edit_message_text(
            text="❌ No inbounds are currently in CDN mode.",
            reply_markup=create_cdn_mode_keyboard(use_xff, provider),
            parse_mode="HTML"
        )
        return

    # Create buttons for each inbound
    keyboard = []
    for inbound in cdn_inbounds:
        callback_data = f"cdn_remove_{inbound[:50]}"  # Limit length
        keyboard.append([InlineKeyboardButton(f"❌ {inbound}", callback_data=callback_data)])

    keyboard.append([InlineKeyboardButton("« Back", callback_data=CallbackData.CDN_MODE_MENU)])

    await query.edit_message_text(
        text="➖ <b>Remove Inbound from CDN Mode</b>\n\nSelect an inbound to remove:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML"
    )


async def cdn_mode_remove_inbound_callback(query, context: ContextTypes.DEFAULT_TYPE):
    """Handle removing a specific inbound from CDN mode."""
    callback_data = query.data
    inbound_name = callback_data.replace("cdn_remove_", "")

    config_data = await read_config()
    cdn_inbounds = config_data.get("cdn_inbounds", [])

    # Find and remove the inbound (handle truncated names)
    removed = None
    for inbound in cdn_inbounds:
        if inbound.startswith(inbound_name) or inbound == inbound_name:
            removed = inbound
            cdn_inbounds.remove(inbound)
            break

    if removed:
        # Save updated list
        await save_config_value("cdn_inbounds", ",".join(cdn_inbounds))

        await query.answer(f"✅ Removed: {removed}")
    else:
        await query.answer("❌ Inbound not found", show_alert=True)

    # Return to CDN mode menu
    await cdn_mode_menu_callback(query, context)


async def cdn_mode_clear_callback(query, context: ContextTypes.DEFAULT_TYPE):
    """Handle clearing all CDN inbounds."""
    await save_config_value("cdn_inbounds", "")
    await query.answer("✅ All CDN inbounds cleared")
    await cdn_mode_menu_callback(query, context)


async def cdn_use_xff_toggle_callback(query, context: ContextTypes.DEFAULT_TYPE):
    """Handle toggling X-Forwarded-For extraction."""
    config_data = await read_config()
    current_xff = config_data.get("cdn_use_xff", True)

    # Toggle the value
    new_xff = not current_xff
    await save_config_value("cdn_use_xff", "true" if new_xff else "false")

    status = "enabled" if new_xff else "disabled"
    await query.answer(f"✅ X-Forwarded-For extraction {status}")
    await cdn_mode_menu_callback(query, context)


async def cdn_provider_menu_callback(query, context: ContextTypes.DEFAULT_TYPE):
    """Handle CDN provider selection menu."""
    config_data = await read_config()
    current_provider = config_data.get("cdn_provider", "cloudflare")

    # Currently only Cloudflare is supported
    cf_prefix = "✅" if current_provider == "cloudflare" else "⬜"

    keyboard = [
        [InlineKeyboardButton(f"{cf_prefix} Cloudflare", callback_data=CallbackData.CDN_PROVIDER_CLOUDFLARE)],
        [InlineKeyboardButton("« Back", callback_data=CallbackData.CDN_MODE_MENU)],
    ]

    await query.edit_message_text(
        text=(
            "📡 <b>CDN Provider</b>\n\n"
            "Select the CDN provider for your inbounds:\n\n"
            f"Current: <b>{current_provider.title()}</b>\n\n"
            "<i>Currently only Cloudflare is supported.\n"
            "More providers may be added in the future.</i>"
        ),
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML"
    )


async def cdn_provider_cloudflare_callback(query, context: ContextTypes.DEFAULT_TYPE):
    """Handle setting CDN provider to Cloudflare."""
    await save_config_value("cdn_provider", "cloudflare")
    await query.answer("✅ CDN provider set to Cloudflare")
    await cdn_mode_menu_callback(query, context)


async def cdn_mode_add_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle text input for adding CDN inbound."""
    text = update.message.text.strip()

    # Load current CDN inbounds for keyboard
    config_data = await read_config()
    use_xff = config_data.get("cdn_use_xff", True)
    provider = config_data.get("cdn_provider", "cloudflare")
    cdn_inbounds = config_data.get("cdn_inbounds", [])

    if text.lower() == "/cancel":
        await update.message.reply_html(
            "❌ Cancelled.",
            reply_markup=create_cdn_mode_keyboard(use_xff, provider)
        )
        return ConversationHandler.END

    if not text:
        await update.message.reply_html(
            "❌ Please send a valid inbound name.\n\nSend /cancel to cancel."
        )
        return SET_CDN_INBOUND

    # Check if already exists
    if text in cdn_inbounds:
        await update.message.reply_html(
            f"⚠️ <code>{text}</code> is already in CDN mode.",
            reply_markup=create_cdn_mode_keyboard(use_xff, provider)
        )
        return ConversationHandler.END

    # Add new inbound
    cdn_inbounds.append(text)
    await save_config_value("cdn_inbounds", ",".join(cdn_inbounds))

    await update.message.reply_html(
        f"✅ Added <code>{text}</code> to CDN mode.\n\n"
        "Real user IPs will be extracted from X-Forwarded-For header.",
        reply_markup=create_cdn_mode_keyboard(use_xff, provider)
    )

    return ConversationHandler.END
