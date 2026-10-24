"""Compatibility shortcuts for the aggregate health card.

Individual connection activity remains in Topics; these routes do no collection.
"""

from telegram_bot.handlers.status_reports import legacy_report_command

connection_report_command = legacy_report_command
node_usage_report_command = legacy_report_command
multi_device_users_command = legacy_report_command
users_by_node_command = legacy_report_command
users_by_protocol_command = legacy_report_command
ip_history_12h_command = legacy_report_command
ip_history_48h_command = legacy_report_command


def _build_report_isp_detector(config_data: dict):
    """Build a report-scoped ISP detector from the canonical config keys.

    The old lookups read IPINFO_TOKEN / USE_FALLBACK_ISP_API, which the config never
    contains, so the report always ran tokenless. The keys are ipinfo_token (root or
    under api) and api.use_fallback_isp_api, matching the enforcement path.
    """
    from utils.isp_detector import ISPDetector

    api_config = config_data.get("api", {}) if isinstance(config_data.get("api"), dict) else {}
    ipinfo_token = config_data.get("ipinfo_token") or api_config.get("ipinfo_token", "")
    use_fallback_api = api_config.get("use_fallback_isp_api", False)
    if ipinfo_token or use_fallback_api:
        return ISPDetector(token=ipinfo_token or None, use_fallback_only=use_fallback_api)
    return None
