"""
The registry of users this limiter has disabled - SQLite only.

One fact used to have three writers: this module's JSON file, ``api_server.py``
and ``cli/disabled.py``. The last two wrote only ``{"disabled_users": ...}`` and
so dropped the ``enable_at`` map, silently turning a permanent ban into a
default-window one for every other user in the file. The ``users`` table already
carried the same columns (``is_disabled_by_limiter``, ``disabled_at``,
``enable_at``), so that table is now the only store and this module is a thin
facade over ``UserCRUD``.

The trade-off, accepted deliberately: if the database is unavailable the limiter
does not know who is disabled and nobody is auto-enabled, so re-enabling becomes
a manual action. That is the safe direction - a user is never punished by
mistake, only left waiting.
"""

import asyncio
import json
import os
import time
from dataclasses import dataclass
from enum import Enum

from utils.logs import logger

# The retired JSON registry. Imported once on first use so that users who were
# disabled before this change do not stay disabled invisibly.
LEGACY_JSON_PATH = "/var/lib/pg-limiter/disable_users.json"

# ``enable_at`` sentinel: never auto-enable, manual action only.
PERMANENT = -1.0


class DisableStatus(str, Enum):
    NOT_DISABLED = "not_disabled"
    PERMANENT = "permanent"
    TIMED = "timed"
    READY_TO_ENABLE = "ready"


@dataclass(slots=True)
class RemainingTimeResult:
    """Structured result for user disabled remaining time."""
    status: DisableStatus
    seconds: int = 0

    @property
    def is_disabled(self) -> bool:
        return self.status != DisableStatus.NOT_DISABLED

    @property
    def is_permanent(self) -> bool:
        return self.status == DisableStatus.PERMANENT

    @property
    def is_ready(self) -> bool:
        return self.status == DisableStatus.READY_TO_ENABLE


@dataclass(slots=True)
class DisabledUserEntry:
    """
    One disabled user, as stored in the ``users`` table.

    ``enable_at``: ``None`` = use the default ``time_to_active_users`` window,
    ``-1`` = permanent (manual enable only), ``> 0`` = exact timestamp.
    """
    username: str
    disabled_at: float
    enable_at: float | None = None

    @property
    def is_permanent(self) -> bool:
        return self.enable_at == PERMANENT


# ── one-time import of the retired JSON file ────────────────────────────────

_migration_lock = asyncio.Lock()
_migrated = False


async def _ensure_migrated() -> None:
    """Import the retired JSON registry once per process, then never again."""
    global _migrated
    if _migrated:
        return
    async with _migration_lock:
        if _migrated:
            return
        _migrated = True
        try:
            imported = await import_legacy_json()
            if imported:
                logger.info(f"Imported {imported} disabled users from the retired JSON registry")
        except Exception as error:  # pylint: disable=broad-except
            logger.error(f"Could not import the legacy disabled-users file: {error}")


async def import_legacy_json(path: str = LEGACY_JSON_PATH) -> int:
    """
    Copy the old JSON registry into the ``users`` table and retire the file.

    Users already marked disabled in the database are left alone, so this can
    never resurrect a ban an operator has since lifted. The file is renamed to
    ``.migrated`` when done.

    Returns:
        int: how many users were carried over.
    """
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return 0

    def _read() -> tuple[dict, dict]:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        raw = data.get("disabled_users")
        if not isinstance(raw, dict):
            # Very old files stored a bare list of usernames.
            legacy = data.get("disable_user") or []
            now = time.time()
            raw = {u: now for u in legacy} if isinstance(legacy, list) else {}
        enable_at = data.get("enable_at")
        return raw, enable_at if isinstance(enable_at, dict) else {}

    disabled, enable_at_map = await asyncio.to_thread(_read)
    imported = 0
    for username, disabled_at in disabled.items():
        try:
            already = await is_disabled(username)
            if already:
                continue
            stored = await disable(
                username,
                disabled_at=float(disabled_at),
                enable_at=enable_at_map.get(username),
            )
            imported += 1 if stored else 0
        except Exception as error:  # pylint: disable=broad-except
            logger.error(f"Could not import disabled user {username}: {error}")

    try:
        await asyncio.to_thread(os.rename, path, path + ".migrated")
    except OSError as error:
        logger.warning(f"Imported the legacy registry but could not rename {path}: {error}")
    return imported


# ── database plumbing ───────────────────────────────────────────────────────


async def _run(operation: str, work, default):
    """
    Run one registry operation in its own session.

    A database failure is logged and degrades to ``default`` instead of raising:
    reads then report "nobody is disabled", so no user is auto-enabled on
    guesswork, and writes report failure to their caller.
    """
    from db.database import get_db

    try:
        async with get_db() as session:
            return await work(session)
    except Exception as error:  # pylint: disable=broad-except
        logger.error(f"Disabled-users registry: {operation} failed: {error}")
        return default


async def _ensure_user_row(session, username: str):
    """
    Guarantee a ``users`` row for this username.

    ``UserCRUD.set_disabled`` is a no-op for a user who was never synced, and
    with this table as the only store that would mean disabling somebody on the
    panel and recording it nowhere - they would never be auto-enabled. A minimal
    local row keeps the record without a panel round-trip in the ban path.
    """
    from db.crud import UserCRUD

    user = await UserCRUD.get_by_username(session, username)
    if user is None:
        logger.info(f"Creating a local record for {username} (not synced yet) so the disable is tracked")
        user = await UserCRUD.create_or_update(session, username=username)
    return user


def _entry_of(user) -> DisabledUserEntry:
    """Build an entry from a ``users`` row."""
    return DisabledUserEntry(
        username=user.username,
        disabled_at=float(user.disabled_at or 0.0),
        enable_at=None if user.enable_at is None else float(user.enable_at),
    )


# ── writes ──────────────────────────────────────────────────────────────────


async def _store(username: str, disabled_at: float, enable_at: float | None) -> bool:
    """Write one disable record, creating the user row if it is missing."""
    from db.crud import UserCRUD

    async def work(session) -> bool:
        await _ensure_user_row(session, username)
        stored = await UserCRUD.set_disabled(
            session,
            username=username,
            disabled=True,
            disabled_at=disabled_at,
            enable_at=enable_at,
            fetch_from_panel=False,
        )
        return stored is not None

    return await _run(f"disable {username}", work, False)


async def disable(
    username: str,
    duration_seconds: int = 0,
    permanent: bool = False,
    disabled_at: float | None = None,
    enable_at: float | None = None,
) -> bool:
    """
    Record that the limiter disabled a user.

    Args:
        username: the user just disabled on the panel.
        duration_seconds: custom window in seconds; ``0`` means use the default
            ``time_to_active_users``. Ignored when ``permanent`` is set.
        permanent: never auto-enable; only a manual enable brings them back.
        disabled_at, enable_at: explicit values, used by the legacy import.

    Returns:
        bool: ``True`` when the record was stored. ``False`` means the user is
        disabled on the panel but not tracked here, which the caller should log:
        nothing will auto-enable them.
    """
    await _ensure_migrated()

    moment = time.time() if disabled_at is None else disabled_at
    if enable_at is None and disabled_at is None:
        if permanent:
            enable_at = PERMANENT
        elif duration_seconds > 0:
            enable_at = moment + duration_seconds

    if permanent:
        logger.info(f"User {username} disabled permanently (manual enable only)")
    elif enable_at:
        when = time.strftime("%H:%M:%S", time.localtime(enable_at))
        logger.info(f"User {username} disabled, will be enabled at {when}")
    else:
        logger.info(f"User {username} disabled, will be enabled after the default window")

    return await _store(username, moment, enable_at)


async def enable(username: str) -> bool:
    """Clear a user's disable record. Returns ``True`` when the row was updated."""
    from db.crud import UserCRUD

    async def work(session) -> bool:
        updated = await UserCRUD.set_disabled(
            session, username=username, disabled=False, fetch_from_panel=False
        )
        return updated is not None

    return await _run(f"enable {username}", work, False)


async def clear_all() -> set[str]:
    """Clear every disable record and return the usernames that were cleared."""
    from db.crud import UserCRUD

    async def work(session) -> set[str]:
        rows = await UserCRUD.get_all_disabled(session)
        names = {row.username for row in rows}
        for name in names:
            await UserCRUD.set_disabled(
                session, username=name, disabled=False, fetch_from_panel=False
            )
        return names

    return await _run("clear all", work, set())


# ── reads ───────────────────────────────────────────────────────────────────


async def entries() -> dict[str, DisabledUserEntry]:
    """Every disable record, keyed by username."""
    from db.crud import UserCRUD

    await _ensure_migrated()

    async def work(session) -> dict[str, DisabledUserEntry]:
        rows = await UserCRUD.get_all_disabled(session)
        return {row.username: _entry_of(row) for row in rows}

    return await _run("list entries", work, {})


async def disabled_usernames() -> set[str]:
    """Every username currently registered as disabled."""
    return set(await entries())


async def disabled_at_map() -> dict[str, float]:
    """``{username: disabled_at}``, for the menus and reports."""
    return {name: entry.disabled_at for name, entry in (await entries()).items()}


async def entry_of(username: str) -> DisabledUserEntry | None:
    """One user's disable record, or ``None`` when they are not disabled."""
    from db.crud import UserCRUD

    async def work(session) -> DisabledUserEntry | None:
        row = await UserCRUD.get_disabled_record(session, username)
        return _entry_of(row) if row else None

    return await _run(f"read {username}", work, None)


async def is_disabled(username: str) -> bool:
    """Whether the limiter currently has this user disabled."""
    return await entry_of(username) is not None


async def disabled_at_of(username: str) -> float | None:
    """When the user was disabled, or ``None`` when they are not."""
    entry = await entry_of(username)
    return entry.disabled_at if entry else None


async def enable_at_of(username: str) -> float | None:
    """Their re-enable timestamp (``-1`` = permanent), ``None`` when unset."""
    entry = await entry_of(username)
    return entry.enable_at if entry else None


async def users_to_enable(default_time_to_active: int) -> list[str]:
    """
    Who is due to be re-enabled now.

    A permanent record (``enable_at == -1``) is never returned; a custom
    timestamp is honoured exactly; otherwise the default window applies to
    ``disabled_at``.
    """
    from db.crud import UserCRUD

    await _ensure_migrated()

    async def work(session) -> list[str]:
        return await UserCRUD.get_users_to_enable(session, default_time_to_active)

    return await _run("collect users to enable", work, [])


async def remaining_time(username: str, default_time_to_active: int) -> RemainingTimeResult:
    """How long this user still has to wait, for display in the bot."""
    entry = await entry_of(username)
    if entry is None:
        return RemainingTimeResult(status=DisableStatus.NOT_DISABLED)

    if entry.is_permanent:
        return RemainingTimeResult(status=DisableStatus.PERMANENT)

    if entry.enable_at is not None:
        remaining = int(entry.enable_at - time.time())
    else:
        remaining = int(default_time_to_active - (time.time() - entry.disabled_at))

    if remaining <= 0:
        return RemainingTimeResult(status=DisableStatus.READY_TO_ENABLE)
    return RemainingTimeResult(status=DisableStatus.TIMED, seconds=remaining)
