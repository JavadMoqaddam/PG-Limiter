"""
Helper functions for warning system.
Provides safe wrappers for external calls.
"""

from utils.logs import get_logger
from utils.types import PanelType, UserType

helpers_logger = get_logger("warning_helpers")


import asyncio

async def _bg_send_wrapper(coro):
    """Wrapper to run a sending coroutine in background with exception logging."""
    try:
        await coro
    except Exception as e:
        helpers_logger.error(f"❌ Background Telegram send failed: {e}")


async def safe_send_logs(message: str, is_warning: bool = False):
    """
    Safely send logs in background, handling import errors gracefully.
    """
    try:
        if is_warning:
            from telegram_bot.send_message import send_warning_log
            helpers_logger.debug(f"📤 Scheduling background warning log message ({len(message)} chars)")
            asyncio.create_task(_bg_send_wrapper(send_warning_log(message)))
        else:
            from telegram_bot.send_message import send_logs
            helpers_logger.debug(f"📤 Scheduling background Telegram log message ({len(message)} chars)")
            asyncio.create_task(_bg_send_wrapper(send_logs(message)))
        helpers_logger.debug("✅ Telegram log scheduled successfully")
    except ImportError as e:
        helpers_logger.warning(f"⚠️ Telegram not configured: {e}")
    except Exception as e:
        helpers_logger.error(f"❌ Failed to schedule telegram message: {e}")


async def safe_send_monitoring_log(message: str):
    """Safely send monitoring log to monitoring topic in background."""
    try:
        from telegram_bot.send_message import send_monitoring_log
        helpers_logger.debug(f"📤 Scheduling background monitoring log message ({len(message)} chars)")
        asyncio.create_task(_bg_send_wrapper(send_monitoring_log(message)))
        helpers_logger.debug("✅ Monitoring log scheduled successfully")
    except ImportError as e:
        helpers_logger.warning(f"⚠️ Telegram not configured: {e}")
    except Exception as e:
        helpers_logger.error(f"❌ Failed to schedule monitoring message: {e}")


async def safe_send_warning_log(message: str):
    """Safely send warning log to warnings topic in background."""
    await safe_send_logs(message, is_warning=True)


async def safe_send_disable_notification(message: str, username: str):
    """Safely send disable notification in background."""
    try:
        from telegram_bot.send_message import send_disable_notification
        helpers_logger.debug(f"📤 Scheduling background disable notification for {username}")
        asyncio.create_task(_bg_send_wrapper(send_disable_notification(message, username)))
        helpers_logger.debug(f"✅ Disable notification scheduled for {username}")
    except ImportError as e:
        helpers_logger.warning(f"⚠️ Telegram not configured: {e}")
        await safe_send_logs(message)
    except Exception as e:
        helpers_logger.error(f"❌ Failed to schedule disable notification for {username}: {e}")
        await safe_send_logs(message)


async def safe_disable_user(panel_data: PanelType, user: UserType):
    """Safely disable user, handling import errors gracefully"""
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
        helpers_logger.info(f"✅ Punishment result for {user.name}: action={result.get('action')}, step={result.get('step_index')}")
        return result
    except ImportError as e:
        helpers_logger.error(f"❌ Failed to import disable_user_with_punishment: {e}")
        return {"action": "error", "message": str(e), "step_index": 0, "violation_count": 0, "duration_minutes": 0}
    except Exception as e:
        helpers_logger.error(f"❌ Failed to disable user {user.name} with punishment: {e}")
        return {"action": "error", "message": str(e), "step_index": 0, "violation_count": 0, "duration_minutes": 0}
