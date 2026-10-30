"""
This module contains utility functions for managing admin IDs,
handling special limits for users, and interacting with the database.
"""

import asyncio
import json
import os
import sys

from utils.atomic_io import atomic_write_json
from utils.logs import get_logger
from utils import settings_service

settings_logger = get_logger("telegram.settings")

try:
    import httpx
except ImportError:
    print("Module 'httpx' is not installed use: 'pip install httpx' to install it")
    sys.exit()

_config_write_lock = asyncio.Lock()


async def send_response(update, text: str, reply_markup=None, parse_mode: str = "HTML"):
    """
    Unified response helper that works for both callback queries and regular messages.
    Gracefully handles Telegram 'Message is not modified' error.
    """
    from telegram.error import BadRequest

    if getattr(update, "callback_query", None):
        try:
            await update.callback_query.answer()
        except Exception:
            pass
        try:
            await update.callback_query.edit_message_text(
                text=text,
                reply_markup=reply_markup,
                parse_mode=parse_mode,
            )
        except BadRequest as e:
            if "message is not modified" in str(e).lower():
                return
            try:
                await update.callback_query.message.reply_text(
                    text=text,
                    reply_markup=reply_markup,
                    parse_mode=parse_mode,
                )
            except Exception:
                pass
        except Exception:
            try:
                await update.callback_query.message.reply_text(
                    text=text,
                    reply_markup=reply_markup,
                    parse_mode=parse_mode,
                )
            except Exception:
                pass
    elif getattr(update, "message", None):
        await update.message.reply_text(
            text=text,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
        )


async def safe_edit_message_text(query, text: str, **kwargs):
    """Safely edit message text ignoring 'Message is not modified' errors."""
    from telegram.error import BadRequest

    try:
        await query.edit_message_text(text, **kwargs)
    except BadRequest as e:
        if "message is not modified" not in str(e).lower():
            raise


def _sync_read_json_file() -> dict:
    """Synchronous file read for config.json."""
    with open("config.json", "r", encoding="utf-8") as f:
        return json.load(f)


async def read_json_file() -> dict:
    """
    Reads and returns the content of the config.json file.
    Uses asyncio.to_thread to avoid blocking the event loop.

    Returns:
        The content of the config.json file.
    """
    return await asyncio.to_thread(_sync_read_json_file)


def _sync_write_json_file(data: dict):
    """Synchronous file write for config.json."""
    atomic_write_json("config.json", data, ensure_ascii=False)


async def write_json_file(data: dict):
    """
    Writes the given data to the config.json file.
    Uses asyncio.Lock to prevent race conditions and atomic write for crash safety.

    Args:
        data: The data to write to the file.
    """
    async with _config_write_lock:
        await asyncio.to_thread(_sync_write_json_file, data)


async def add_admin_to_config(new_admin_id: int) -> int | None:
    """
    Adds a new admin ID to the config.json file.
    Note: For Docker deployment, admins should be set via ADMIN_IDS env variable.

    Args:
        new_admin_id: The ID of the new admin.

    Returns:
        The ID of the new admin if it was added, None otherwise.
    """
    # First check if admins are configured via environment variable
    admin_ids_env = os.environ.get("ADMIN_IDS", "")
    if admin_ids_env:
        # Admin IDs are managed via environment variable
        # Can't dynamically add to env var, but check if already in list
        try:
            admins = [int(id.strip()) for id in admin_ids_env.split(",") if id.strip()]
            if int(new_admin_id) in admins:
                return new_admin_id
        except ValueError:
            pass
        # Return None since we can't add to env var dynamically
        # User needs to update ADMIN_IDS env var
        return None

    # Fall back to config.json for non-Docker deployments
    if os.path.exists("config.json"):
        data = await read_json_file()
        if "telegram" not in data:
            data["telegram"] = {}
        admins = data.get("telegram", {}).get("admins", [])
        if int(new_admin_id) not in admins:
            admins.append(int(new_admin_id))
            data["telegram"]["admins"] = admins
            await write_json_file(data)
            return new_admin_id
    else:
        data = {"telegram": {"admins": [new_admin_id]}}
        await write_json_file(data)
        return new_admin_id
    return None


async def check_admin() -> list[int] | None:
    """
    Checks and returns the list of admins.
    First checks ADMIN_IDS environment variable, then falls back to config.json.

    Returns:
        The list of admins.
    """
    # First check environment variable (Docker deployment)
    admin_ids_env = os.environ.get("ADMIN_IDS", "")
    if admin_ids_env:
        try:
            return [int(id.strip()) for id in admin_ids_env.split(",") if id.strip()]
        except ValueError:
            pass

    # Fall back to config.json for non-Docker deployments
    if os.path.exists("config.json"):
        data = await read_json_file()
        return data.get("telegram", {}).get("admins", [])

    return []


SPECIAL_LIMIT_MIN = 1


def special_limit_rejection(limit: int) -> str | None:
    """
    Return an admin-facing rejection message for a bad special limit, or None if usable.

    0 and negatives used to be stored as-is, and a stored limit of 0 makes a single
    device a violation - so an admin reaching for "no limit" got the user banned on
    their first connection. Unlimited has its own mechanism in this project: the
    whitelist. A special limit is always a real device count of 1 or more.
    """
    if limit < SPECIAL_LIMIT_MIN:
        return (
            f"❌ A special limit must be <b>{SPECIAL_LIMIT_MIN}</b> or greater "
            f"(you sent <code>{limit}</code>).\n\n"
            "If you want this user to have <b>no limit at all</b>, add them to the "
            "whitelist instead — a special limit of 0 would ban them on their first "
            "device, not exempt them."
        )
    return None


GENERAL_LIMIT_MIN = 1


def general_limit_rejection(limit: int) -> str | None:
    """
    Return an admin-facing rejection message for a bad general limit, or None if usable.

    Same trap as ``special_limit_rejection``, one blast radius wider: the general limit
    applies to every user who has no special or group limit, so a stored 0 does not mean
    "no limit" - it makes the first device a violation for the whole installation and
    bans everyone after the usual consecutive scans. Exemption is the whitelist.
    """
    if limit < GENERAL_LIMIT_MIN:
        return (
            f"❌ The general limit must be <b>{GENERAL_LIMIT_MIN}</b> or greater "
            f"(you sent <code>{limit}</code>).\n\n"
            "A general limit of 0 would ban <b>every user</b> on their first device "
            "rather than exempt them. To exempt specific users, add them to the "
            "whitelist; to give a group more devices, use group limits."
        )
    return None


async def handle_special_limit(username: str, limit: int) -> list | None:
    """
    Handles the special limit for a given username using database.

    Args:
        username: The username to handle the special limit for.
        limit: The limit to set.

    Returns:
        A list where the first element is a flag indicating whether the limit was set before,
        and the second element is the new limit.
    """
    try:
        special_limits = (await settings_service.read_settings())["limits"]["special"]
        if not await settings_service.set_special_limit(username, limit):
            return None
        return [int(username in special_limits), limit]
    except settings_service.SettingsUnavailable:
        settings_logger.exception("Failed to save special limit")
        return None


# Backward-compatible alias
handel_special_limit = handle_special_limit


class LastAdminError(Exception):
    """Raised when a removal would leave the bot with no administrator."""


async def remove_admin_from_config(admin_id: int) -> bool:
    """
    Removes an admin from the configuration.
    Note: In Docker deployment, admins are managed via ADMIN_IDS env var.

    Args:
        admin_id (int): The ID of the admin to be removed.

    Returns:
        bool: True if the admin was successfully removed, False otherwise.

    Raises:
        LastAdminError: if the target is the only remaining administrator. Enforced
            here so every path (command continuation and callback) is covered.
    """
    data = await read_json_file()
    admins = data.get("telegram", {}).get("admins", [])
    if admin_id in admins:
        if len(admins) <= 1:
            raise LastAdminError("Refusing to remove the last administrator; add another admin first.")
        admins.remove(admin_id)
        data["telegram"]["admins"] = admins
        await write_json_file(data)
        return True
    return False


async def get_special_limits_dict() -> dict:
    """Read the canonical special overrides shown to administrators."""
    return (await settings_service.read_settings())["limits"]["special"]


async def get_special_limit_list() -> list | None:
    """Split special overrides into messages of at most 100 entries."""
    special_limits = await get_special_limits_dict()
    messages = [f"{username} : {limit}" for username, limit in special_limits.items()]
    return ["\n".join(messages[i : i + 100]) for i in range(0, len(messages), 100)] or None


async def add_except_user(except_user: str) -> str | None:
    """Return the username only after canonical whitelist membership commits."""
    try:
        if await settings_service.set_exception(except_user, True):
            return except_user
    except settings_service.SettingsUnavailable:
        settings_logger.exception("Failed to add whitelist user")
    return None


async def get_except_users_list() -> list:
    """Read the canonical whitelist."""
    return (await settings_service.read_settings())["except_users"]


async def show_except_users_handler() -> list | None:
    """Split whitelist entries into messages of at most 100 entries."""
    messages = await get_except_users_list()
    return ["\n".join(messages[i : i + 100]) for i in range(0, len(messages), 100)] or None


async def remove_except_user_from_config(user: str) -> str | None:
    """Remove actual canonical membership and report committed success."""
    try:
        if await settings_service.set_exception(user, False, require_existing=True):
            return user
    except settings_service.SettingsUnavailable:
        settings_logger.exception("Failed to remove whitelist user")
    return None


async def save_general_limit(limit: int) -> int | None:
    """Return the new limit only after the canonical setting commits."""
    try:
        await settings_service.write_settings({"general_limit": limit})
        return limit
    except settings_service.SettingsUnavailable:
        settings_logger.exception("Failed to save general limit")
        return None
