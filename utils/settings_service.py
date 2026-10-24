"""Committed management commands shared by the API, CLI and Telegram bot."""

import json
from collections.abc import Iterable, Mapping
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

import db.database as database
import utils.read_config as configuration
from db.crud.config import ConfigCRUD
from db.crud.config_revision import REVISION_KEY
from db.crud.users import UserCRUD
from db.models import User
from utils.user_policy import UserPolicy, evaluate_user_policy


class SettingsUnavailable(RuntimeError):
    """The canonical settings could not be read or committed."""


async def read_settings() -> dict[str, Any]:
    """Read a fresh, private canonical configuration or fail visibly."""
    if not configuration.DB_AVAILABLE:
        raise SettingsUnavailable("The settings database is unavailable")
    try:
        settings = await configuration.read_config(refresh=True)
    except Exception as error:
        raise SettingsUnavailable("Could not read canonical settings") from error
    if settings.get("config_degraded"):
        raise SettingsUnavailable("Canonical settings are unavailable")
    return settings


async def read_user_policy(username: str, *, expected_db_revision: int | None = None) -> UserPolicy | None:
    """Read current factual policy, rejecting a superseded configuration revision."""
    if not configuration.DB_AVAILABLE:
        raise SettingsUnavailable("The settings database is unavailable")
    try:
        snapshot = await configuration.read_config_snapshot(usernames=(username,))
    except Exception as error:
        raise SettingsUnavailable("Could not read current user policy") from error
    if snapshot.config.get("config_degraded"):
        raise SettingsUnavailable("Current user policy is unavailable")
    if expected_db_revision is not None and snapshot.db_revision != expected_db_revision:
        return None
    return evaluate_user_policy(username=username, config=snapshot.config, facts=snapshot.user_facts.get(username))


def _stored_value(value: Any) -> str:
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, (dict, list)):
        return json.dumps(value)
    return str(value)


async def write_settings(values: Mapping[str, Any], *, delete_keys: Iterable[str] = ()) -> None:
    """Commit a settings batch atomically, including its revision changes."""
    if not configuration.DB_AVAILABLE:
        raise SettingsUnavailable("The settings database is unavailable")
    # Validate/serialize command input before entering persistence. ValueError
    # from revision advancement or any later DB operation means unavailable
    # committed state, rather than rejected user input.
    values = {key: _stored_value(value) for key, value in values.items()}
    delete_keys = tuple(delete_keys)
    if REVISION_KEY in values or REVISION_KEY in delete_keys:
        raise ValueError("The configuration revision is internal")
    try:
        async with configuration.config_write_transaction():
            async with database.get_db() as db:
                for key, value in values.items():
                    await ConfigCRUD.set(db, key, value)
                for key in delete_keys:
                    await ConfigCRUD.delete(db, key)
            # Source observations must retire before releasing the writer lock.
            if "ip_source" in values or "ip_source" in delete_keys:
                await configuration._finish_source_change()
    except Exception as error:
        raise SettingsUnavailable("Could not commit settings") from error


async def _ensure_user(username: str, *, fetch: bool) -> bool:
    """Finish any panel synchronization before acquiring a writer transaction."""
    if not configuration.DB_AVAILABLE:
        raise SettingsUnavailable("The settings database is unavailable")
    try:
        async with database.get_db() as db:
            if await UserCRUD.get_by_username(db, username) is not None:
                return True
    except Exception as error:
        raise SettingsUnavailable("Could not read the user") from error
    if not fetch:
        return False
    from utils.user_sync import fetch_and_sync_single_user

    try:
        return bool(await fetch_and_sync_single_user(username))
    except Exception:
        return False


async def _locked_user(db: AsyncSession, username: str) -> User | None:
    """Serialize the final membership check with the ensuing mutation."""
    if db.get_bind().dialect.name == "sqlite":
        # Legacy SQLite SELECT does not start a physical transaction. Acquire
        # its writer lock before checking membership so another process cannot
        # remove the override between this check and the write.
        await db.execute(text("BEGIN IMMEDIATE"))
    return await db.scalar(select(User).where(User.username == username).with_for_update())


async def set_special_limit(username: str, limit: int | None, *, require_existing: bool = False) -> bool:
    """Set an override, or remove an existing override with None."""
    if limit is not None and (not isinstance(limit, int) or isinstance(limit, bool) or limit < 1):
        raise ValueError("A special limit must be an integer of 1 or greater")
    if not await _ensure_user(username, fetch=limit is not None and not require_existing):
        return False
    try:
        async with configuration.config_write_transaction(), database.get_db() as db:
            user = await _locked_user(db, username)
            if user is None or ((require_existing or limit is None) and user.special_limit is None):
                return False
            changed = await UserCRUD.set_special_limit(db, username, limit, fetch_from_panel=False)
        return changed is not None
    except Exception as error:
        raise SettingsUnavailable("Could not commit the special limit") from error


async def set_exception(
    username: str,
    excepted: bool,
    *,
    reason: str | None = None,
    excepted_by: str | None = None,
    require_existing: bool = False,
) -> bool:
    """Change whitelist membership after rechecking the current user state."""
    if not await _ensure_user(username, fetch=excepted and not require_existing):
        return False
    try:
        async with configuration.config_write_transaction(), database.get_db() as db:
            user = await _locked_user(db, username)
            if user is None or ((require_existing or not excepted) and not user.is_excepted):
                return False
            changed = await UserCRUD.set_excepted(
                db, username, excepted, reason=reason, excepted_by=excepted_by, fetch_from_panel=False
            )
        return changed is not None
    except Exception as error:
        raise SettingsUnavailable("Could not commit whitelist membership") from error
