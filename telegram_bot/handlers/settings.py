"""
Settings handlers for the Telegram bot.

This module used to hold every settings handler in one 2300-line file. The
handlers now live in one ``settings_*`` module per domain and this file only
re-exports them, so ``from telegram_bot.handlers.settings import X`` and the
router's ``_lazy("telegram_bot.handlers.settings", "X")`` lookups keep working
unchanged.

    settings_common.py        root menu + shared keyboard
    settings_panel.py         panel domain / username / password
    settings_display.py       country filter, ipinfo token, report detail
    settings_groups.py        disabled group, fallback group
    settings_sync.py          user sync interval, pending deletions
    settings_subnet.py        subnet grouping, high trust grouping
    settings_trust.py         trust data reset
    settings_cdn.py           CDN inbounds
    settings_nodes.py         CDN nodes, disabled nodes
    settings_ip_source.py     SSE logs vs panel API
    settings_device_count.py  per-device vs per-IP counting
"""

from telegram_bot.handlers.settings_common import (
    create_back_to_settings_keyboard,
    handle_settings_menu_callback,
)
from telegram_bot.handlers.settings_panel import (
    set_panel_domain,
    get_domain,
    get_username,
    get_password,
)
from telegram_bot.handlers.settings_display import (
    create_enhanced_details_keyboard,
    set_ipinfo_token,
    ipinfo_token_handler,
    save_ipinfo_token,
    handle_enhanced_menu_callback,
    handle_enhanced_toggle_callback,
    handle_ipinfo_callback,
    handle_ipinfo_token_input,
)
from telegram_bot.handlers.settings_groups import (
    _get_groups_from_panel,
    create_disable_group_keyboard,
    create_fallback_group_keyboard,
    handle_disable_by_group_callback,
    handle_select_disabled_group_callback,
    handle_fallback_group_menu_callback,
    handle_select_fallback_group_callback,
    handle_clear_fallback_group_callback,
)
from telegram_bot.handlers.settings_sync import (
    create_user_sync_keyboard,
    handle_user_sync_menu_callback,
    handle_user_sync_interval_callback,
    handle_user_sync_now_callback,
    handle_pending_deletions_callback,
    handle_force_delete_callback,
)
from telegram_bot.handlers.settings_subnet import (
    subnet_ip_grouping_menu_callback,
    subnet_ip_grouping_toggle_callback,
    subnet_ip_grouping_mode_toggle_callback,
    high_trust_ip_grouping_menu_callback,
    high_trust_ip_grouping_toggle_callback,
)
from telegram_bot.handlers.settings_trust import (
    trust_reset_menu_callback,
    trust_reset_all_callback,
)
from telegram_bot.handlers.settings_cdn import (
    create_cdn_mode_keyboard,
    cdn_mode_menu_callback,
    cdn_mode_add_callback,
    cdn_mode_remove_callback,
    cdn_mode_remove_inbound_callback,
    cdn_mode_clear_callback,
    cdn_use_xff_toggle_callback,
    cdn_provider_menu_callback,
    cdn_provider_cloudflare_callback,
    cdn_mode_add_handler,
)
from telegram_bot.handlers.settings_nodes import (
    create_node_settings_keyboard,
    _get_nodes_list,
    node_settings_menu_callback,
    node_settings_refresh_callback,
    node_cdn_menu_callback,
    node_cdn_toggle_callback,
    node_cdn_clear_callback,
    node_disabled_menu_callback,
    node_disabled_toggle_callback,
    node_disabled_clear_callback,
)
from telegram_bot.handlers.settings_ip_source import (
    _build_panel_data,
    _build_ip_source_text,
    _find_preflight_user_id,
    create_ip_source_keyboard,
    handle_ip_source_menu_callback,
    handle_ip_source_set_logs_callback,
    handle_ip_source_set_api_callback,
    handle_ip_source_concurrency_callback,
    ip_source_concurrency_handler,
    handle_ip_source_stats_callback,
)
from telegram_bot.handlers.settings_device_count import (
    create_device_count_keyboard,
    _build_device_count_text,
    _set_device_count_mode,
    handle_device_count_menu_callback,
    handle_device_count_set_device_callback,
    handle_device_count_set_ip_callback,
)
