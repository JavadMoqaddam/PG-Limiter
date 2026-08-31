"""
Telegram Bot Constants
Contains callback data constants and conversation states.
"""

from enum import IntEnum


class ConversationState(IntEnum):
    """Conversation states for ConversationHandler.

    Using IntEnum instead of range() unpacking ensures that
    adding or removing a state does not silently shift all
    subsequent values and break existing conversations.
    """
    GET_DOMAIN = 0
    GET_PORT = 1
    GET_USERNAME = 2
    GET_PASSWORD = 3
    GET_CONFIRMATION = 4
    GET_CHAT_ID = 5
    GET_SPECIAL_LIMIT = 6
    GET_LIMIT_NUMBER = 7
    GET_CHAT_ID_TO_REMOVE = 8
    SET_COUNTRY_CODE = 9
    SET_EXCEPT_USERS = 10
    REMOVE_EXCEPT_USER = 11
    GET_GENERAL_LIMIT_NUMBER = 12
    GET_CHECK_INTERVAL = 13
    GET_TIME_TO_ACTIVE_USERS = 14
    SET_IPINFO_TOKEN = 15
    SET_ENHANCED_DETAILS = 16
    RESTORE_CONFIG = 17
    WAITING_USERNAME_FOR_LIMIT = 18
    SET_CDN_INBOUND = 19
    SET_NODE_SETTINGS = 20
    WAITING_GROUP_ID = 21
    SET_GROUP_LIMIT = 22


# Re-export as module-level names for backward compatibility
GET_DOMAIN = ConversationState.GET_DOMAIN
GET_PORT = ConversationState.GET_PORT
GET_USERNAME = ConversationState.GET_USERNAME
GET_PASSWORD = ConversationState.GET_PASSWORD
GET_CONFIRMATION = ConversationState.GET_CONFIRMATION
GET_CHAT_ID = ConversationState.GET_CHAT_ID
GET_SPECIAL_LIMIT = ConversationState.GET_SPECIAL_LIMIT
GET_LIMIT_NUMBER = ConversationState.GET_LIMIT_NUMBER
GET_CHAT_ID_TO_REMOVE = ConversationState.GET_CHAT_ID_TO_REMOVE
SET_COUNTRY_CODE = ConversationState.SET_COUNTRY_CODE
SET_EXCEPT_USERS = ConversationState.SET_EXCEPT_USERS
REMOVE_EXCEPT_USER = ConversationState.REMOVE_EXCEPT_USER
GET_GENERAL_LIMIT_NUMBER = ConversationState.GET_GENERAL_LIMIT_NUMBER
GET_CHECK_INTERVAL = ConversationState.GET_CHECK_INTERVAL
GET_TIME_TO_ACTIVE_USERS = ConversationState.GET_TIME_TO_ACTIVE_USERS
SET_IPINFO_TOKEN = ConversationState.SET_IPINFO_TOKEN
SET_ENHANCED_DETAILS = ConversationState.SET_ENHANCED_DETAILS
RESTORE_CONFIG = ConversationState.RESTORE_CONFIG
WAITING_USERNAME_FOR_LIMIT = ConversationState.WAITING_USERNAME_FOR_LIMIT
SET_CDN_INBOUND = ConversationState.SET_CDN_INBOUND
SET_NODE_SETTINGS = ConversationState.SET_NODE_SETTINGS
WAITING_GROUP_ID = ConversationState.WAITING_GROUP_ID
SET_GROUP_LIMIT = ConversationState.SET_GROUP_LIMIT


class CallbackData:
    """Callback data constants for inline keyboards."""
    
    # Main menu
    MAIN_MENU = "main_menu"
    
    # Settings menu
    SETTINGS_MENU = "settings_menu"
    LIMITS_MENU = "limits_menu"
    USERS_MENU = "users_menu"
    MONITORING_MENU = "monitoring_menu"
    REPORTS_MENU = "reports_menu"
    ADMIN_MENU = "admin_menu"
    
    # Special limit options
    SPECIAL_LIMIT_1 = "special_limit_1"
    SPECIAL_LIMIT_2 = "special_limit_2"
    SPECIAL_LIMIT_CUSTOM = "special_limit_custom"
    
    # General limit options
    GENERAL_LIMIT_2 = "general_limit_2"
    GENERAL_LIMIT_3 = "general_limit_3"
    GENERAL_LIMIT_4 = "general_limit_4"
    GENERAL_LIMIT_CUSTOM = "general_limit_custom"
    
    # Country code options
    COUNTRY_IR = "country_ir"
    COUNTRY_RU = "country_ru"
    COUNTRY_CN = "country_cn"
    COUNTRY_NONE = "country_none"
    
    # Check interval options
    INTERVAL_120 = "interval_120"
    INTERVAL_180 = "interval_180"
    INTERVAL_240 = "interval_240"
    INTERVAL_CUSTOM = "interval_custom"
    
    # Time to active options
    TIME_300 = "time_300"
    TIME_600 = "time_600"
    TIME_900 = "time_900"
    TIME_CUSTOM = "time_custom"
    
    # Enhanced details toggle
    # ENHANCED_MENU shows the current state with the ON/OFF keyboard. The settings
    # button used to send ENHANCED_ON directly, so pressing "Enhanced Details" set
    # the option instead of opening its screen, and handle_enhanced_menu_callback -
    # which exists and does exactly the right thing - was imported by main.py and
    # never routed.
    ENHANCED_MENU = "enhanced_menu"
    ENHANCED_ON = "enhanced_on"
    ENHANCED_OFF = "enhanced_off"
    
    # Single IP users toggle
    SINGLE_IP_ON = "single_ip_on"
    SINGLE_IP_OFF = "single_ip_off"
    
    # Monitoring actions
    MONITORING_STATUS = "monitoring_status"
    MONITORING_DETAILS = "monitoring_details"
    MONITORING_CLEAR = "monitoring_clear"
    
    # Reports
    REPORT_CONNECTION = "report_connection"
    REPORT_NODE_USAGE = "report_node_usage"
    REPORT_MULTI_DEVICE = "report_multi_device"
    REPORT_IP_12H = "report_ip_12h"
    REPORT_IP_48H = "report_ip_48h"
    
    # User management
    SHOW_EXCEPT_USERS = "show_except_users"
    SET_EXCEPT_USER = "set_except_user"
    REMOVE_EXCEPT_USER = "remove_except_user"
    SHOW_SPECIAL_LIMIT = "show_special_limit"
    SET_SPECIAL_LIMIT = "set_special_limit"
    SHOW_DISABLED_USERS = "show_disabled_users"
    ENABLE_ALL_DISABLED = "enable_all_disabled"
    WHITELIST_MENU = "whitelist_menu"
    SPECIAL_LIMITS_MENU = "special_limits_menu"
    FILTERED_USERS_MENU = "filtered_users_menu"
    BACK_USERS = "back_users"
    
    # Admin management
    ADD_ADMIN = "add_admin"
    LIST_ADMINS = "list_admins"
    REMOVE_ADMIN = "remove_admin"
    
    # Backup/Restore
    BACKUP = "backup"
    RESTORE = "restore"
    
    # Config
    CREATE_CONFIG = "create_config"
    SET_IPINFO = "set_ipinfo"
    
    # Disable method settings
    DISABLE_METHOD_MENU = "disable_method_menu"
    DISABLE_BY_STATUS = "disable_by_status"
    DISABLE_BY_GROUP = "disable_by_group"
    SELECT_DISABLED_GROUP = "select_disabled_group"
    FALLBACK_GROUP_MENU = "fallback_group_menu"
    SELECT_FALLBACK_GROUP = "select_fallback_group"
    CLEAR_FALLBACK_GROUP = "clear_fallback_group"
    
    # Punishment system
    PUNISHMENT_MENU = "punishment_menu"
    PUNISHMENT_TOGGLE = "punishment_toggle"
    PUNISHMENT_WINDOW = "punishment_window"
    PUNISHMENT_STEPS = "punishment_steps"
    PUNISHMENT_WINDOW_24 = "punishment_window_24"
    PUNISHMENT_WINDOW_48 = "punishment_window_48"
    PUNISHMENT_WINDOW_72 = "punishment_window_72"
    PUNISHMENT_WINDOW_168 = "punishment_window_168"
    PUNISHMENT_WINDOW_CUSTOM = "punishment_window_custom"
    # Punishment steps configuration
    PUNISHMENT_ADD_STEP = "punishment_add_step"
    PUNISHMENT_REMOVE_STEP = "punishment_remove_step"
    PUNISHMENT_STEP_WARNING = "punishment_step_warning"
    PUNISHMENT_STEP_DISABLE_10 = "punishment_step_disable_10"
    PUNISHMENT_STEP_DISABLE_30 = "punishment_step_disable_30"
    PUNISHMENT_STEP_DISABLE_60 = "punishment_step_disable_60"
    PUNISHMENT_STEP_DISABLE_240 = "punishment_step_disable_240"
    PUNISHMENT_STEP_DISABLE_UNLIMITED = "punishment_step_disable_unlimited"
    PUNISHMENT_STEP_REVOKE = "punishment_step_revoke"
    PUNISHMENT_STEPS_RESET = "punishment_steps_reset"
    PUNISHMENT_BACK = "punishment_back"
    
    # Group filter
    GROUP_FILTER_MENU = "group_filter_menu"
    GROUP_FILTER_TOGGLE = "group_filter_toggle"
    GROUP_FILTER_MODE_INCLUDE = "group_filter_mode_include"
    GROUP_FILTER_MODE_EXCLUDE = "group_filter_mode_exclude"
    GROUP_LIMIT_SET = "group_limit_set"
    
    # Admin filter
    ADMIN_FILTER_MENU = "admin_filter_menu"
    ADMIN_FILTER_TOGGLE = "admin_filter_toggle"
    ADMIN_FILTER_MODE_INCLUDE = "admin_filter_mode_include"
    ADMIN_FILTER_MODE_EXCLUDE = "admin_filter_mode_exclude"
    
    # CDN mode settings
    CDN_MODE_MENU = "cdn_mode_menu"
    CDN_MODE_ADD = "cdn_mode_add"
    CDN_MODE_REMOVE = "cdn_mode_remove"
    CDN_MODE_LIST = "cdn_mode_list"
    CDN_MODE_CLEAR = "cdn_mode_clear"
    CDN_PROVIDER_MENU = "cdn_provider_menu"
    CDN_PROVIDER_CLOUDFLARE = "cdn_provider_cloudflare"
    CDN_USE_XFF_TOGGLE = "cdn_use_xff_toggle"
    
    # Subnet IP Grouping (relaxed mode)
    SUBNET_IP_GROUPING_MENU = "subnet_ip_grouping_menu"
    SUBNET_IP_GROUPING_TOGGLE = "subnet_ip_grouping_toggle"
    SUBNET_IP_GROUPING_MODE_TOGGLE = "subnet_ip_grouping_mode_toggle"
    
    # High Trust IP Grouping (for trusted users, same node+inbound = 1 device)
    HIGH_TRUST_IP_GROUPING_MENU = "high_trust_ip_grouping_menu"
    HIGH_TRUST_IP_GROUPING_TOGGLE = "high_trust_ip_grouping_toggle"
    
    # Trust data reset
    TRUST_RESET_MENU = "trust_reset_menu"
    TRUST_RESET_ALL = "trust_reset_all"
    TRUST_RESET_USER = "trust_reset_user"
    
    # Node settings
    NODE_SETTINGS_MENU = "node_settings_menu"
    NODE_SETTINGS_REFRESH = "node_settings_refresh"
    NODE_CDN_MENU = "node_cdn_menu"
    NODE_DISABLED_MENU = "node_disabled_menu"
    NODE_CDN_CLEAR = "node_cdn_clear"
    NODE_DISABLED_CLEAR = "node_disabled_clear"
    
    # User sync settings
    USER_SYNC_MENU = "user_sync_menu"
    USER_SYNC_1 = "user_sync_1"
    USER_SYNC_5 = "user_sync_5"
    USER_SYNC_10 = "user_sync_10"
    USER_SYNC_15 = "user_sync_15"
    USER_SYNC_NOW = "user_sync_now"
    USER_SYNC_PENDING = "user_sync_pending"  # Review pending deletions
    USER_SYNC_FORCE_DELETE = "user_sync_force_delete"  # Force delete pending users
    
    # Topics settings
    TOPICS_MENU = "topics_menu"
    TOPICS_TOGGLE = "topics_toggle"
    TOPICS_SETUP = "topics_setup"
    TOPICS_CLEAR = "topics_clear"
    TOPICS_SET_GROUP = "topics_set_group"
    TOPICS_CHECK_PERMISSIONS = "topics_check_permissions"
    TOPICS_CLEAR_CACHE = "topics_clear_cache"
    
    # Cleanup
    CLEANUP_DELETED_USERS = "cleanup_deleted_users"

    # IP source selection (SSE log streaming vs panel API)
    IP_SOURCE_MENU = "ip_source_menu"
    IP_SOURCE_SET_LOGS = "ip_source_set_logs"
    IP_SOURCE_SET_API = "ip_source_set_api"
    IP_SOURCE_SET_CONCURRENCY = "ip_source_set_concurrency"
    IP_SOURCE_STATS = "ip_source_stats"

    # Device counting mode (node-aware devices vs node-agnostic IPs)
    DEVICE_COUNT_MENU = "device_count_menu"
    DEVICE_COUNT_SET_DEVICE = "device_count_set_device"
    DEVICE_COUNT_SET_IP = "device_count_set_ip"
    
    # Auto-backup settings
    AUTO_BACKUP_MENU = "auto_backup_menu"
    AUTO_BACKUP_TOGGLE = "auto_backup_toggle"
    AUTO_BACKUP_INTERVAL_1H = "auto_backup_interval_1h"
    AUTO_BACKUP_INTERVAL_3H = "auto_backup_interval_3h"
    AUTO_BACKUP_INTERVAL_6H = "auto_backup_interval_6h"
    AUTO_BACKUP_INTERVAL_12H = "auto_backup_interval_12h"
    AUTO_BACKUP_NOW = "auto_backup_now"
    
    # Back buttons
    BACK_MAIN = "back_main"
    BACK_SETTINGS = "back_settings"
    BACK_LIMITS = "back_limits"


# Start message template
START_MESSAGE = """
🔒 <b>PG-Limiter Control Panel</b>

Welcome to the IP connection limiter for PasarGuard Panel.

Use the menu below or type /help for all commands.
"""

# Help text for commands
HELP_TEXT = """
✨<b>All Commands:</b>

<b>🔧 Configuration:</b>
/start - Show main menu
/create_config - Setup panel info

<b>🎯 Limits:</b>
/set_special_limit - Set user-specific limit
/show_special_limit - Show special limits
/set_general_limit_number - Set default limit

<b>👥 Users:</b>
/set_except_user - Add to except list
/remove_except_user - Remove from except list
/show_except_users - Show except users

<b>⚙️ Settings:</b>
Settings → Enhanced Details - Toggle node names, IDs and protocols in reports
/set_ipinfo_token - Set IPInfo API token

<b>🛰️ IP Source:</b>
Settings → IP Source - Switch between node logs (SSE) and the panel API

<b>🧮 Device Counting:</b>
Settings → Device Counting - Count per device (by inbound) or per IP

<b>📡 Monitoring:</b>
/monitoring_status - Current monitoring status
/monitoring_details - Detailed analytics
/clear_monitoring - Clear all warnings

<b>⚖️ Punishment:</b>
/punishment_status - Show punishment settings
/punishment_toggle - Enable/disable smart punishment
/punishment_set_window - Set violation window
/punishment_set_steps - Set the escalation steps
/user_violations - Check user violations
/clear_user_violations - Clear user violations

<b>🔍 Group Filter:</b>
/group_filter_status - Show group filter status
/group_filter_toggle - Enable/disable filter
/group_filter_mode - Set include/exclude mode
/group_filter_set - Set group IDs
/group_filter_add - Add one group ID
/group_filter_remove - Remove one group ID

<b>👤 Admin Filter:</b>
/admin_filter_status - Show admin filter status
/admin_filter_toggle - Enable/disable filter
/admin_filter_set - Set admin usernames
/admin_filter_add - Add one admin username
/admin_filter_remove - Remove one admin username

<b>📊 Reports:</b>
/connection_report - Connection analysis
/node_usage - Node usage report
/users_by_node - Active users grouped by node
/users_by_protocol - Active users grouped by inbound
/multi_device_users - Multi-device detection
/ip_history_12h - 12-hour IP history
/ip_history_48h - 48-hour IP history

<b>👑 Admin Management:</b>
/add_admin - Add new admin
/remove_admin - Remove admin
/admins_list - List all admins

<b>💾 Backup:</b>
/backup - Create config backup
/restore - Restore from backup
/migrate_backup - Migrate JSON backup to database

<b>ℹ️ Info:</b>
/help - Show this help
"""
