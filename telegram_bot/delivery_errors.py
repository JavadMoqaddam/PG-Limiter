"""Classify confirmed Telegram rejections without treating uncertainty as failure."""

from telegram.error import BadRequest


def is_message_unavailable(error: BaseException) -> bool:
    """Whether Telegram confirms that the target message is missing or not editable."""
    return isinstance(error, BadRequest) and any(
        phrase in str(error).lower()
        for phrase in ("message to edit not found", "message can't be edited", "message can not be edited")
    )
