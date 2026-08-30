"""
Device counting mode: per device (inbound-aware) or per IP.

Which one is right depends on the deployment - with two nodes per inbound one
client legitimately appears twice, with one node per inbound it does not. The
counting rules themselves live in
[utils/device_count.py](utils/device_count.py); this menu only flips the
``device_count_mode`` key and explains the consequence.
"""

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from telegram_bot.constants import CallbackData
from utils.read_config import read_config, save_config_value


def create_device_count_keyboard(current_mode: str):
    """Create the device counting mode keyboard with the active mode marked."""
    device_prefix = "✅" if current_mode == "device" else "⬜"
    ip_prefix = "✅" if current_mode == "ip" else "⬜"
    keyboard = [
        [InlineKeyboardButton(
            f"{device_prefix} 🖥️ Per Device (by inbound)",
            callback_data=CallbackData.DEVICE_COUNT_SET_DEVICE
        )],
        [InlineKeyboardButton(
            f"{ip_prefix} 🌐 Per IP",
            callback_data=CallbackData.DEVICE_COUNT_SET_IP
        )],
        [InlineKeyboardButton("« Back to Settings", callback_data=CallbackData.SETTINGS_MENU)],
    ]
    return InlineKeyboardMarkup(keyboard)


def _build_device_count_text(config_data: dict) -> str:
    """Render the device counting menu body for the current configuration."""
    current_mode = str(config_data.get("device_count_mode") or "device")
    ip_source = str(config_data.get("ip_source") or "logs")
    subnet_on = "✅ on" if config_data.get("subnet_ip_grouping") else "❌ off"
    subnet_mode = str(config_data.get("subnet_grouping_mode") or "/24")
    high_trust_on = "✅ on" if config_data.get("high_trust_ip_grouping") else "❌ off"

    if current_mode == "ip":
        active = "🌐 <b>Per IP</b>"
        active_note = (
            "One client IP counts as <b>one device</b>, whatever it connects to. "
            "Use this when only the number of distinct internet connections "
            "matters."
        )
    else:
        active = "🖥️ <b>Per Device (by inbound)</b>"
        active_note = (
            "The inbound stays in the device key, so one IP reaching two inbounds "
            "counts as <b>two devices</b> - this is what catches several people "
            "sharing a single connection."
        )

    return (
        "🧮 <b>Device Counting</b>\n\n"
        f"<b>Active:</b> {active}\n"
        f"<i>{active_note}</i>\n\n"
        "<b>Related settings:</b>\n"
        f"• IP source: <code>{ip_source}</code>\n"
        f"• Subnet grouping: {subnet_on} (<code>{subnet_mode}</code>)\n"
        f"• High trust grouping: {high_trust_on}\n\n"
        "<b>Notes:</b>\n"
        "• The node is <b>never</b> part of the device key in either mode: when "
        "several nodes serve the same core config, one client is registered on "
        "all of them at once.\n"
        "• Subnet grouping applies in both modes (same <code>/24</code> counts "
        "once). High Trust grouping is inactive in <b>Per IP</b> mode.\n"
        "• CDN nodes and CDN inbounds keep their own grouping rules.\n"
        "• In API mode the panel does not expose the inbound, so both modes "
        "behave the same there."
    )


async def handle_device_count_menu_callback(query, _context: ContextTypes.DEFAULT_TYPE):
    """Show the device counting mode menu."""
    config_data = await read_config()
    current_mode = str(config_data.get("device_count_mode") or "device")

    try:
        await query.edit_message_text(
            text=_build_device_count_text(config_data),
            reply_markup=create_device_count_keyboard(current_mode),
            parse_mode="HTML"
        )
    except BadRequest as e:
        if "message is not modified" in str(e).lower():
            await query.answer("🔄 Already up to date")
        else:
            raise


async def _set_device_count_mode(query, context, mode: str, label: str):
    """Persist the device counting mode and refresh the menu."""
    config_data = await read_config()
    if str(config_data.get("device_count_mode") or "device") == mode:
        await query.answer(f"Already counting {label}")
        return

    await save_config_value("device_count_mode", mode)
    await query.answer(f"✅ Now counting {label}")
    await handle_device_count_menu_callback(query, context)


async def handle_device_count_set_device_callback(query, context: ContextTypes.DEFAULT_TYPE):
    """Switch to inbound-aware device counting (default)."""
    await _set_device_count_mode(query, context, "device", "per device (by inbound)")


async def handle_device_count_set_ip_callback(query, context: ContextTypes.DEFAULT_TYPE):
    """Switch to pure IP counting."""
    await _set_device_count_mode(query, context, "ip", "per IP")
