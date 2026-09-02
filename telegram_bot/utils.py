"""
This module contains utility functions for managing admin IDs,
handling special limits for users, and interacting with the database.
"""

import asyncio
import json
import os
import sys

from utils.types import PanelType
from utils.read_config import invalidate_config_cache
from utils.atomic_io import atomic_write_json

try:
    import httpx
except ImportError:
    print("Module 'httpx' is not installed use: 'pip install httpx' to install it")
    sys.exit()

# Import database utilities
try:
    from db import get_db, UserCRUD, ConfigCRUD
    DB_AVAILABLE = True
except ImportError:
    DB_AVAILABLE = False


_config_write_lock = asyncio.Lock()


async def get_token(panel_data: PanelType) -> PanelType:
    """Canonical token fetcher delegating to panel_api.auth module."""
    from utils.panel_api.auth import get_token as auth_get_token
    return await auth_get_token(panel_data)


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


async def handle_special_limit(username: str, limit: int) -> list:
    """
    Handles the special limit for a given username using database.

    Args:
        username: The username to handle the special limit for.
        limit: The limit to set.

    Returns:
        A list where the first element is a flag indicating whether the limit was set before,
        and the second element is the new limit.
    """
    if DB_AVAILABLE:
        async with get_db() as db:
            # Check if limit was set before
            existing_limit = await UserCRUD.get_special_limit(db, username)
            set_before = 1 if existing_limit is not None else 0
            
            # Set the new limit
            await UserCRUD.set_special_limit(db, username, limit)
            await db.commit()
            return [set_before, limit]
    
    # Fallback to config.json
    set_before = 0
    if os.path.exists("config.json"):
        data = await read_json_file()
        if "limits" not in data:
            data["limits"] = {}
        special_limit = data.get("limits", {}).get("special", {})
        if special_limit.get(username):
            set_before = 1
        special_limit[username] = limit
        data["limits"]["special"] = special_limit
        await write_json_file(data)
        return [set_before, special_limit[username]]
    data = {"limits": {"special": {username: limit}}}
    await write_json_file(data)
    return [0, limit]


# Backward-compatible alias
handel_special_limit = handle_special_limit


async def remove_admin_from_config(admin_id: int) -> bool:
    """
    Removes an admin from the configuration.
    Note: In Docker deployment, admins are managed via ADMIN_IDS env var.

    Args:
        admin_id (int): The ID of the admin to be removed.

    Returns:
        bool: True if the admin was successfully removed, False otherwise.
    """
    data = await read_json_file()
    admins = data.get("telegram", {}).get("admins", [])
    if admin_id in admins:
        admins.remove(admin_id)
        data["telegram"]["admins"] = admins
        await write_json_file(data)
        return True
    return False


async def add_base_information(domain: str, password: str, username: str):
    """
    Adds base information including domain, password, and username.

    Args:
        domain (str): The domain for the panel.
        password (str): The password for the panel.
        username (str): The username for the panel.

    Returns:
        None
    """
    await get_token(
        PanelType(panel_domain=domain, panel_password=password, panel_username=username)
    )
    if os.path.exists("config.json"):
        data = await read_json_file()
    else:
        data = {}
    if "panel" not in data:
        data["panel"] = {}
    data["panel"]["domain"] = domain
    data["panel"]["username"] = username
    data["panel"]["password"] = password
    await write_json_file(data)


async def get_special_limits_dict() -> dict:
    """
    This function retrieves the special limits from database as a dictionary.

    Returns:
        dict: Dictionary of username -> limit
    """
    if DB_AVAILABLE:
        async with get_db() as db:
            special_limits = await UserCRUD.get_all_special_limits(db)
            return special_limits or {}
    
    # Fallback to config.json
    if os.path.exists("config.json"):
        data = await read_json_file()
        return data.get("limits", {}).get("special", {})
    return {}


async def get_special_limit_list() -> list | None:
    """
    This function retrieves the list of special limits from database,
    and returns this list in a format suitable for messaging (split into shorter messages).

    Returns:
        list
    """
    if DB_AVAILABLE:
        async with get_db() as db:
            special_limits = await UserCRUD.get_all_special_limits(db)
            if not special_limits:
                return None
            special_list = "\n".join(
                [f"{key} : {value}" for key, value in special_limits.items()]
            )
            messages = special_list.split("\n")
            shorter_messages = [
                "\n".join(messages[i : i + 100]) for i in range(0, len(messages), 100)
            ]
            return shorter_messages
    
    # Fallback to config.json
    if os.path.exists("config.json"):
        data = await read_json_file()
        special_list = data.get("limits", {}).get("special", None)
        if not special_list:
            return None
        special_list = "\n".join(
            [f"{key} : {value}" for key, value in special_list.items()]
        )
        messages = special_list.split("\n")
        shorter_messages = [
            "\n".join(messages[i : i + 100]) for i in range(0, len(messages), 100)
        ]
        return shorter_messages
    return None


async def add_except_user(except_user: str) -> str | None:
    """
    Add a user to the exception list using database.
    Falls back to config.json if database is not available.
    """
    if DB_AVAILABLE:
        async with get_db() as db:
            await UserCRUD.set_excepted(db, except_user, True)
            await db.commit()
        # The enforcement loop's only whitelist gate in the warn/ban path reads
        # config["except_users"] out of the process-wide config cache. Without this
        # invalidation the freshly excepted user keeps accruing violation scans and is
        # banned anyway, until some other setting is written or the process restarts.
        await invalidate_config_cache()
        return except_user
    
    # Fallback to config.json
    if os.path.exists("config.json"):
        data = await read_json_file()
        if "limits" not in data:
            data["limits"] = {}
        users = data.get("limits", {}).get("except_users", [])
        if except_user not in users:
            users.append(except_user)
            data["limits"]["except_users"] = users
            await write_json_file(data)
            return except_user
    else:
        data = {"limits": {"except_users": [except_user]}}
        await write_json_file(data)
        return except_user
    return None


async def get_except_users_list() -> list:
    """
    Retrieve the list of exception users from the database as a list.
    
    Returns:
        list: List of usernames in the whitelist
    """
    if DB_AVAILABLE:
        async with get_db() as db:
            except_users = await UserCRUD.get_all_excepted(db)
            return except_users or []
    
    # Fallback to config.json
    if os.path.exists("config.json"):
        data = await read_json_file()
        return data.get("except_users", [])
    return []


async def show_except_users_handler() -> list | None:
    """
    Retrieve the list of exception users from the database.
    If the list is too long, it splits the list into shorter messages.
    """
    if DB_AVAILABLE:
        async with get_db() as db:
            except_users = await UserCRUD.get_all_excepted(db)
            if not except_users:
                return None
            except_users_str = "\n".join([f"{user}" for user in except_users])
            messages = except_users_str.split("\n")
            shorter_messages = [
                "\n".join(messages[i : i + 100]) for i in range(0, len(messages), 100)
            ]
            return shorter_messages
    
    # Fallback to config.json
    if os.path.exists("config.json"):
        data = await read_json_file()
        except_users = data.get("limits", {}).get("except_users", None)
        if not except_users:
            return None
        except_users = "\n".join([f"{key}" for key in except_users])
        messages = except_users.split("\n")
        shorter_messages = [
            "\n".join(messages[i : i + 100]) for i in range(0, len(messages), 100)
        ]
        return shorter_messages
    return None


async def remove_except_user_from_config(user: str) -> str | None:
    """
    Remove a user from the exception list using database.
    """
    if DB_AVAILABLE:
        async with get_db() as db:
            result = await UserCRUD.set_excepted(db, user, False)
            await db.commit()
        # Same reason as add_except_user: without this the cached except_users list
        # keeps shielding a user the operator just removed from the whitelist.
        await invalidate_config_cache()
        return user if result is not None else None
    
    # Fallback to config.json
    if not os.path.exists("config.json"):
        return None
    data = await read_json_file()
    except_users = data.get("limits", {}).get("except_users", [])
    if user in except_users:
        except_users.remove(user)
        data["limits"]["except_users"] = except_users
        await write_json_file(data)
        return user
    return None


async def save_general_limit(limit: int) -> int:
    """
    Save the general limit to the database.
    Falls back to config.json if database is not available.

    Every path invalidates the configuration cache. That cache has no expiry - it is
    dropped only by an explicit invalidation - and read_config() overlays the stored
    general_limit onto config["limits"]["general"], which is what the enforcement loop
    compares device counts against. Without the invalidation the new limit was written
    to the database and then ignored for the life of the process, so raising the limit
    from the bot did not stop the old one from banning people.
    """
    if DB_AVAILABLE:
        async with get_db() as db:
            await ConfigCRUD.set(db, "general_limit", limit)
            await db.commit()
        await invalidate_config_cache()
        return limit

    # Fallback to config.json
    if os.path.exists("config.json"):
        data = await read_json_file()
        if "limits" not in data:
            data["limits"] = {}
        data["limits"]["general"] = limit
        await write_json_file(data)
        await invalidate_config_cache()
        return limit
    data = {"limits": {"general": limit}}
    await write_json_file(data)
    await invalidate_config_cache()
    return limit
