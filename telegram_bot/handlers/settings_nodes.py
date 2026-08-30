"""
Per-node settings: which nodes sit behind a CDN and which are ignored.

Both lists feed device counting directly:
  * a **CDN node** contributes exactly one device no matter how many addresses
    it reports;
  * a **disabled node** contributes nothing at all - its connections are
    dropped before counting, so it can never trigger a ban.
"""

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from telegram_bot.constants import CallbackData
from utils.read_config import read_config, save_config_value


def create_node_settings_keyboard():
    """Create node settings menu keyboard."""
    keyboard = [
        [InlineKeyboardButton("☁️ CDN Nodes", callback_data=CallbackData.NODE_CDN_MENU)],
        [InlineKeyboardButton("🚫 Disabled Nodes", callback_data=CallbackData.NODE_DISABLED_MENU)],
        [InlineKeyboardButton("🔄 Refresh Nodes", callback_data=CallbackData.NODE_SETTINGS_REFRESH)],
        [InlineKeyboardButton("« Back to Settings", callback_data=CallbackData.SETTINGS_MENU)],
    ]
    return InlineKeyboardMarkup(keyboard)


async def _get_nodes_list():
    """Helper to get nodes list from panel."""
    config_data = await read_config()
    panel_config = config_data.get("panel", {})

    if not panel_config.get("domain") or not panel_config.get("password"):
        return None, "Panel not configured"

    from utils.types import PanelType
    from utils.panel_api.nodes import get_nodes

    panel_data = PanelType(
        panel_config.get("username", "admin"),
        panel_config.get("password", ""),
        panel_config.get("domain", "")
    )

    try:
        nodes = await get_nodes(panel_data, enabled_only=False)
        if isinstance(nodes, ValueError):
            return None, str(nodes)
        return nodes, None
    except Exception as e:
        return None, str(e)


async def node_settings_menu_callback(query, context: ContextTypes.DEFAULT_TYPE):
    """Handle node settings menu callback."""
    config_data = await read_config()
    cdn_nodes = config_data.get("cdn_nodes", [])
    disabled_nodes = config_data.get("disabled_nodes", [])

    status_text = (
        f"<b>CDN Nodes:</b> {len(cdn_nodes)} configured\n"
        f"<b>Disabled Nodes:</b> {len(disabled_nodes)} configured"
    )

    try:
        await query.edit_message_text(
            text=(
                "🖥️ <b>Node Settings</b>\n\n"
                f"{status_text}\n\n"
                "<b>CDN Nodes:</b>\n"
                "All IPs from CDN nodes count as <b>1 device</b>.\n"
                "Use for nodes behind CDN (Cloudflare, etc.)\n\n"
                "<b>Disabled Nodes:</b>\n"
                "Connections from disabled nodes are <b>ignored</b>.\n"
                "Use for nodes you don't want to monitor."
            ),
            reply_markup=create_node_settings_keyboard(),
            parse_mode="HTML"
        )
    except BadRequest as e:
        if "Message is not modified" in str(e):
            await query.answer("🔄 Settings are already up to date")
        else:
            raise


async def node_settings_refresh_callback(query, context: ContextTypes.DEFAULT_TYPE):
    """Refresh nodes list from panel."""
    from utils.panel_api.nodes import invalidate_nodes_cache

    await invalidate_nodes_cache()
    await query.answer("✅ Nodes cache refreshed")
    await node_settings_menu_callback(query, context)


async def node_cdn_menu_callback(query, context: ContextTypes.DEFAULT_TYPE):
    """Show CDN nodes menu with toggle buttons."""
    config_data = await read_config()
    cdn_nodes = config_data.get("cdn_nodes", [])

    nodes, error = await _get_nodes_list()

    if error:
        try:
            await query.edit_message_text(
                text=f"❌ Failed to get nodes: {error}",
                reply_markup=create_node_settings_keyboard(),
                parse_mode="HTML"
            )
        except BadRequest:
            pass
        return

    if not nodes:
        try:
            await query.edit_message_text(
                text="❌ No nodes found in panel.",
                reply_markup=create_node_settings_keyboard(),
                parse_mode="HTML"
            )
        except BadRequest:
            pass
        return

    # Build node buttons
    keyboard = []
    for node in nodes:
        is_cdn = node.node_id in cdn_nodes
        status = "☁️" if is_cdn else "⬜"
        btn_text = f"{status} {node.node_name} (#{node.node_id})"
        callback_data = f"node_cdn_toggle:{node.node_id}"
        keyboard.append([InlineKeyboardButton(btn_text, callback_data=callback_data)])

    # Add clear all and back buttons
    if cdn_nodes:
        keyboard.append([InlineKeyboardButton("🗑️ Clear All CDN", callback_data=CallbackData.NODE_CDN_CLEAR)])
    keyboard.append([InlineKeyboardButton("« Back", callback_data=CallbackData.NODE_SETTINGS_MENU)])

    try:
        await query.edit_message_text(
            text=(
                "☁️ <b>CDN Nodes</b>\n\n"
                "Select nodes that are behind CDN.\n"
                "All IPs from these nodes will count as <b>1 device</b>.\n\n"
                f"<b>Currently enabled:</b> {len(cdn_nodes)} nodes\n\n"
                "<i>Click a node to toggle CDN mode:</i>"
            ),
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML"
        )
    except BadRequest as e:
        if "Message is not modified" in str(e):
            await query.answer()
        else:
            raise


async def node_cdn_toggle_callback(query, context: ContextTypes.DEFAULT_TYPE, node_id: int):
    """Toggle CDN mode for a node."""
    config_data = await read_config()
    cdn_nodes = config_data.get("cdn_nodes", [])

    if node_id in cdn_nodes:
        cdn_nodes.remove(node_id)
        await query.answer(f"❌ Node #{node_id} removed from CDN mode")
    else:
        cdn_nodes.append(node_id)
        await query.answer(f"✅ Node #{node_id} added to CDN mode")

    # Save updated list
    await save_config_value("cdn_nodes", ",".join(str(n) for n in cdn_nodes))

    # Refresh menu
    await node_cdn_menu_callback(query, context)


async def node_cdn_clear_callback(query, context: ContextTypes.DEFAULT_TYPE):
    """Clear all CDN nodes."""
    await save_config_value("cdn_nodes", "")
    await query.answer("✅ All CDN nodes cleared")
    await node_cdn_menu_callback(query, context)


async def node_disabled_menu_callback(query, context: ContextTypes.DEFAULT_TYPE):
    """Show disabled nodes menu with toggle buttons."""
    config_data = await read_config()
    disabled_nodes = config_data.get("disabled_nodes", [])

    nodes, error = await _get_nodes_list()

    if error:
        try:
            await query.edit_message_text(
                text=f"❌ Failed to get nodes: {error}",
                reply_markup=create_node_settings_keyboard(),
                parse_mode="HTML"
            )
        except BadRequest:
            pass
        return

    if not nodes:
        try:
            await query.edit_message_text(
                text="❌ No nodes found in panel.",
                reply_markup=create_node_settings_keyboard(),
                parse_mode="HTML"
            )
        except BadRequest:
            pass
        return

    # Build node buttons
    keyboard = []
    for node in nodes:
        is_disabled = node.node_id in disabled_nodes
        status = "🚫" if is_disabled else "✅"
        btn_text = f"{status} {node.node_name} (#{node.node_id})"
        callback_data = f"node_disabled_toggle:{node.node_id}"
        keyboard.append([InlineKeyboardButton(btn_text, callback_data=callback_data)])

    # Add clear all and back buttons
    if disabled_nodes:
        keyboard.append([InlineKeyboardButton("🗑️ Clear All Disabled", callback_data=CallbackData.NODE_DISABLED_CLEAR)])
    keyboard.append([InlineKeyboardButton("« Back", callback_data=CallbackData.NODE_SETTINGS_MENU)])

    try:
        await query.edit_message_text(
            text=(
                "🚫 <b>Disabled Nodes</b>\n\n"
                "Select nodes to exclude from monitoring.\n"
                "Connections from these nodes will be <b>ignored</b>.\n\n"
                f"<b>Currently disabled:</b> {len(disabled_nodes)} nodes\n\n"
                "<i>Click a node to toggle:</i>\n"
                "✅ = Monitored | 🚫 = Disabled"
            ),
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML"
        )
    except BadRequest as e:
        if "Message is not modified" in str(e):
            await query.answer()
        else:
            raise


async def node_disabled_toggle_callback(query, context: ContextTypes.DEFAULT_TYPE, node_id: int):
    """Toggle disabled state for a node."""
    config_data = await read_config()
    disabled_nodes = config_data.get("disabled_nodes", [])

    if node_id in disabled_nodes:
        disabled_nodes.remove(node_id)
        await query.answer(f"✅ Node #{node_id} is now monitored")
    else:
        disabled_nodes.append(node_id)
        await query.answer(f"🚫 Node #{node_id} is now disabled")

    # Save updated list
    await save_config_value("disabled_nodes", ",".join(str(n) for n in disabled_nodes))

    # Refresh menu
    await node_disabled_menu_callback(query, context)


async def node_disabled_clear_callback(query, context: ContextTypes.DEFAULT_TYPE):
    """Clear all disabled nodes."""
    await save_config_value("disabled_nodes", "")
    await query.answer("✅ All nodes are now monitored")
    await node_disabled_menu_callback(query, context)
