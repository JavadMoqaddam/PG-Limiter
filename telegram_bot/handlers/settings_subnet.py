"""
Grouping relaxations that make device counting more forgiving.

Both toggles here decide when several addresses of one user collapse into a
single device, so they are the operator's main lever against false positives:

  * **Subnet grouping** - same /24 (or same /16 + same ISP) is one device.
  * **High Trust grouping** - for users whose trust score is high enough, every
    IP reaching the same inbound is one device (WiFi/mobile switching).

The rules themselves live in [utils/device_count.py](utils/device_count.py);
this module only reads and writes the config keys.
"""

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from telegram_bot.constants import CallbackData
from utils.read_config import read_config, save_config_value


async def subnet_ip_grouping_menu_callback(query, context: ContextTypes.DEFAULT_TYPE):
    """Handle subnet IP grouping menu display."""
    config_data = await read_config()
    current_status = config_data.get("subnet_ip_grouping", False)
    current_mode = config_data.get("subnet_grouping_mode", "/24")
    if current_mode not in ["/24", "/16"]:
        current_mode = "/24"

    status_emoji = "✅" if current_status else "❌"
    status_text = "enabled" if current_status else "disabled"

    if current_mode == "/16":
        mode_label = "🌐 Mode: /16 (Wide - 65k IPs + ISP)"
        mode_desc = (
            "• <b>Active Subnet Mode:</b> <code>/16 Subnet (Wide)</code>\n"
            "• IPs sharing the same <b>/16 block</b> (<code>192.168.x.x</code>) + <b>same ISP</b> count as <b>1 device</b>.\n"
            "• <i>Best for aggressive cellular carrier IP rotation across different subnets.</i>"
        )
        example_text = (
            "• <code>192.146.2.57</code> → Node1 | VLESS\n"
            "• <code>192.146.24.35</code> → Node1 | VLESS (same /16 & ISP: Irancell)\n"
            "Counts as: <b>1 device</b>"
        )
    else:
        mode_label = "🎯 Mode: /24 (Standard - 256 IPs)"
        mode_desc = (
            "• <b>Active Subnet Mode:</b> <code>/24 Subnet (Standard - Recommended)</code>\n"
            "• IPs in the same <b>/24 block</b> (<code>192.168.1.x</code>) count as <b>1 device</b> (256 IPs).\n"
            "• <i>Safest setting: Prevents multi-location account sharing while stopping false CGNAT bans.</i>"
        )
        example_text = (
            "• <code>31.7.122.108</code> → Node1 | VLESS\n"
            "• <code>31.7.122.66</code> → Node1 | VLESS (same /24: Mokhaberat)\n"
            "Counts as: <b>1 device</b>"
        )

    await query.edit_message_text(
        text=(
            f"🌐 <b>Subnet IP Grouping</b>\n\n"
            f"<b>Status:</b> {status_emoji} {status_text.title()}\n"
            f"{mode_desc}\n\n"
            f"<b>Example ({'Irancell' if current_mode == '/16' else 'Mokhaberat'}):</b>\n"
            f"{example_text}\n\n"
            f"With grouping <b>enabled</b>: counts as <b>1 device</b>\n"
            f"With grouping <b>disabled</b>: counts as <b>2 devices</b>\n\n"
            "<i>💡 Use the toggle button below to switch between /24 and /16 modes.</i>"
        ),
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(
                f"{'❌ Disable' if current_status else '✅ Enable'} Subnet Grouping",
                callback_data=CallbackData.SUBNET_IP_GROUPING_TOGGLE
            )],
            [InlineKeyboardButton(
                mode_label,
                callback_data=CallbackData.SUBNET_IP_GROUPING_MODE_TOGGLE
            )],
            [InlineKeyboardButton("« Back to Settings", callback_data=CallbackData.SETTINGS_MENU)]
        ]),
        parse_mode="HTML"
    )


async def subnet_ip_grouping_toggle_callback(query, context: ContextTypes.DEFAULT_TYPE):
    """Handle subnet IP grouping toggle callback."""
    config_data = await read_config()
    current_status = config_data.get("subnet_ip_grouping", False)

    # Toggle the status
    new_status = not current_status
    await save_config_value("subnet_ip_grouping", str(new_status).lower())

    status_text = "enabled" if new_status else "disabled"
    await query.answer(f"Subnet IP Grouping {status_text}")

    # Reload the menu
    await subnet_ip_grouping_menu_callback(query, context)


async def subnet_ip_grouping_mode_toggle_callback(query, context: ContextTypes.DEFAULT_TYPE):
    """Handle subnet IP grouping mode toggle callback (/24 <-> /16)."""
    config_data = await read_config()
    current_mode = config_data.get("subnet_grouping_mode", "/24")

    # Toggle between /24 and /16
    new_mode = "/16" if current_mode == "/24" else "/24"
    await save_config_value("subnet_grouping_mode", new_mode)

    mode_text = "Wide /16 (+ISP)" if new_mode == "/16" else "Standard /24"
    await query.answer(f"Subnet Mode: {mode_text}")

    # Reload the menu
    await subnet_ip_grouping_menu_callback(query, context)


async def high_trust_ip_grouping_menu_callback(query, context: ContextTypes.DEFAULT_TYPE):
    """Handle high trust IP grouping menu display."""
    config_data = await read_config()
    current_status = config_data.get("high_trust_ip_grouping", False)
    threshold = config_data.get("high_trust_threshold", 20)

    status_emoji = "✅" if current_status else "❌"
    status_text = "enabled" if current_status else "disabled"

    await query.edit_message_text(
        text=(
            f"⭐ <b>High Trust IP Grouping</b>\n\n"
            f"<b>Status:</b> {status_emoji} {status_text.title()}\n"
            f"<b>Trust Threshold:</b> ≥{threshold}\n\n"
            "<b>What it does:</b>\n"
            "For users with <b>high trust score</b>, if multiple IPs use <b>exactly</b> "
            "the <b>same node</b> AND <b>same inbound protocol</b>, they are counted as "
            "<b>one device</b>.\n\n"
            "<b>Use case:</b>\n"
            "When a user switches between WiFi and Mobile data on the <b>same phone</b>, "
            "they get different IPs but connect through the same node and inbound. "
            "This mode detects such patterns for trusted users and doesn't penalize them.\n\n"
            "<b>Example:</b>\n"
            "• <code>192.168.1.5</code> → Node1 | VLESS (WiFi)\n"
            "• <code>85.12.45.120</code> → Node1 | VLESS (Mobile)\n\n"
            f"With this mode <b>enabled</b> + trust ≥{threshold}: <b>1 device</b>\n"
            "With this mode <b>disabled</b>: <b>2 devices</b>\n\n"
            "<i>💡 This only applies to users who have built up trust through consistent behavior.</i>"
        ),
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(
                f"{'❌ Disable' if current_status else '✅ Enable'} High Trust Mode",
                callback_data=CallbackData.HIGH_TRUST_IP_GROUPING_TOGGLE
            )],
            [InlineKeyboardButton("« Back to Settings", callback_data=CallbackData.SETTINGS_MENU)]
        ]),
        parse_mode="HTML"
    )


async def high_trust_ip_grouping_toggle_callback(query, context: ContextTypes.DEFAULT_TYPE):
    """Handle high trust IP grouping toggle callback."""
    config_data = await read_config()
    current_status = config_data.get("high_trust_ip_grouping", False)

    # Toggle the status
    new_status = not current_status
    await save_config_value("high_trust_ip_grouping", str(new_status).lower())

    status_text = "enabled" if new_status else "disabled"
    await query.answer(f"High Trust Mode {status_text}")

    # Reload the menu
    await high_trust_ip_grouping_menu_callback(query, context)
