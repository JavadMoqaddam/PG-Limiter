"""API-key authentication and safe panel notifications."""

from utils.logs import get_logger
from utils.types import PanelType


auth_logger = get_logger("panel_api.auth")


def get_panel_headers(panel_data: PanelType) -> dict[str, str]:
    """Return the only supported panel authentication header."""
    key = panel_data.panel_api_key
    if not isinstance(key, str) or not key.strip():
        raise ValueError("PANEL_API_KEY must be configured")
    key = key.strip()
    if any(not 33 <= ord(character) <= 126 for character in key):
        raise ValueError("PANEL_API_KEY must contain only visible ASCII characters")
    return {"X-Api-Key": key}


async def safe_send_logs_panel(message: str):
    """Send panel notifications while keeping external exception text private."""
    try:
        from telegram_bot.send_message import send_logs

        await send_logs(message)
    except ImportError:
        auth_logger.warning("Could not import send_logs")
    except Exception as error:
        auth_logger.error(f"Failed to send telegram message: {type(error).__name__}")
