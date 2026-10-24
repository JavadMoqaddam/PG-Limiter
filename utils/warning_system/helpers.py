"""
Helper functions for warning system.
Provides safe wrappers for external calls.
"""

from utils.logs import get_logger
from utils.types import PanelType, UserType
from collections.abc import Callable

helpers_logger = get_logger("warning_helpers")


async def get_disable_ip_activity(warning, config_data: dict) -> str:
    """Enrich recorded activity only after the caller has applied a disable.

    The warning record must already be cleared before this optional network I/O,
    so a timeout or cancellation cannot repeat an applied punishment next scan.
    """
    from utils.check_usage import _ensure_isp_detector
    from utils.isp_detector import ISPDetector
    from telegram_bot.topics import get_topics_manager

    if not get_topics_manager().group_id:
        return warning.get_ip_activity_summary()

    ips = list(warning.ip_first_seen)
    formatted_ips = {ip: ISPDetector.format_ip_with_isp(ip, {}) for ip in ips}
    try:
        detector = await _ensure_isp_detector(config_data)
        results = await detector.get_multiple_isp_info(ips)
        formatted_ips.update({ip: detector.format_ip_with_isp(ip, results.get(ip, {})) for ip in ips})
    except Exception as error:
        helpers_logger.warning("Failed to enrich disable activity for %s: %s", warning.username, type(error).__name__)
    return warning.get_ip_activity_summary(formatted_ips)


def capture_monitoring_guard() -> Callable[[], bool]:
    """Capture Topics admission when the optional monitoring gate is installed."""
    from importlib import import_module

    try:
        gate = import_module("utils.monitoring_gate")
    except ModuleNotFoundError as error:
        if error.name != "utils.monitoring_gate":
            raise
        # The standalone policy stack predates the independent Topics gate PR.
        return lambda: True
    admission = gate.monitoring_snapshot()
    return lambda: gate.monitoring_is_current(admission)


async def safe_send_logs(message: str, is_warning: bool = False):
    """
    Safely send logs via Telegram Dispatcher.
    """
    try:
        if is_warning:
            from telegram_bot.send_message import send_warning_log

            helpers_logger.debug(f"📤 Enqueueing warning log message ({len(message)} chars)")
            await send_warning_log(message)
        else:
            from telegram_bot.send_message import send_logs

            helpers_logger.debug(f"📤 Enqueueing Telegram log message ({len(message)} chars)")
            await send_logs(message)
        helpers_logger.debug("✅ Telegram log enqueued successfully")
    except ImportError as e:
        helpers_logger.warning(f"⚠️ Telegram not configured: {e}")
    except Exception as e:
        helpers_logger.error(f"❌ Failed to enqueue telegram message: {e}")


async def safe_send_warning_log(message: str, ttl: float = None):
    """Safely send warning log to warnings topic."""
    try:
        from telegram_bot.send_message import send_warning_log

        await send_warning_log(message, ttl=ttl)
    except Exception as e:
        helpers_logger.error(f"❌ Failed to enqueue warning log: {e}")


async def safe_send_disable_notification(message: str, username: str):
    """Safely send disable notification with Priority.CRITICAL."""
    try:
        from telegram_bot.send_message import send_disable_notification

        helpers_logger.debug(f"📤 Enqueueing disable notification for {username}")
        await send_disable_notification(message, username)
        helpers_logger.debug(f"✅ Disable notification enqueued for {username}")
    except ImportError as e:
        helpers_logger.warning(f"⚠️ Telegram not configured: {e}")
    except Exception as e:
        helpers_logger.error(f"❌ Failed to enqueue disable notification for {username}: {e}")


async def safe_disable_user(panel_data: PanelType, user: UserType):
    """Safely disable user, handling import errors gracefully."""
    try:
        from utils.panel_api import disable_user

        helpers_logger.debug(f"🚫 Disabling user {user.name}")
        await disable_user(panel_data, user)
        helpers_logger.info(f"✅ User {user.name} disabled successfully")
    except ImportError as e:
        helpers_logger.error(f"❌ Failed to import disable_user: {e}")
    except Exception as e:
        helpers_logger.error(f"❌ Failed to disable user {user.name}: {e}")


async def safe_disable_user_with_punishment(panel_data: PanelType, user: UserType) -> dict:
    """
    Safely disable user with smart punishment system.
    Returns punishment result dict or error dict.
    """
    try:
        from utils.panel_api import disable_user_with_punishment

        helpers_logger.debug(f"🚫 Disabling user {user.name} with punishment system")
        result = await disable_user_with_punishment(panel_data, user)
        helpers_logger.info(
            f"✅ Punishment result for {user.name}: action={result.get('action')}, step={result.get('step_index')}"
        )
        return result
    except ImportError as e:
        helpers_logger.error(f"❌ Failed to import disable_user_with_punishment: {e}")
        return {"action": "error", "message": str(e), "step_index": 0, "violation_count": 0, "duration_minutes": 0}
    except Exception as e:
        helpers_logger.error(f"❌ Failed to disable user {user.name} with punishment: {e}")
        return {"action": "error", "message": str(e), "step_index": 0, "violation_count": 0, "duration_minutes": 0}
