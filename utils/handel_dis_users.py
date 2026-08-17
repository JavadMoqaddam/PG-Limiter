"""
This module contains the DisabledUsers class and DisabledUserEntry dataclass
which provides unified, thread-safe methods for managing disabled users.
"""

import asyncio
import json
import os
import time
from dataclasses import dataclass
from enum import Enum

from utils.logs import logger
from utils.atomic_io import atomic_write_json


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
    Unified entry representing a disabled user.
    Eliminates multiple split dictionaries and provides a single source of truth.
    """
    username: str
    disabled_at: float
    enable_at: float | None = None  # None = default time_to_active, -1 = permanent, >0 = custom timestamp


# Shared module-level lock for all DisabledUsers instances and operations
DISABLED_USERS_LOCK = asyncio.Lock()
_disabled_users_lock = DISABLED_USERS_LOCK

# Global state collections maintained for backward-compatibility
DISABLED_USERS: set[str] = set()
DISABLED_USERS_TIMESTAMPS: dict[str, float] = {}
DISABLED_USERS_ENABLE_AT: dict[str, float] = {}


class DisabledUsers:
    """
    Unified manager for disabled users using DisabledUserEntry dataclass.
    Thread-safe with atomic JSON persistence and cross-instance asyncio.Lock synchronization.
    """
    _lock = _disabled_users_lock

    def __init__(self, filename: str = "/var/lib/pg-limiter/disable_users.json"):
        self.filename = filename
        self._entries: dict[str, DisabledUserEntry] = {}
        self.disabled_users: dict[str, float] = {}  # {username: disabled_timestamp}
        self.enable_at: dict[str, float] = {}  # {username: enable_at_timestamp}
        self._write_lock = self._lock
        self.load_disabled_users()

    def _sync_views_from_entries(self):
        """Synchronize internal views and global structures from the single source of truth (_entries)."""
        self.disabled_users = {u: e.disabled_at for u, e in self._entries.items()}
        self.enable_at = {u: e.enable_at for u, e in self._entries.items() if e.enable_at is not None}
        
        global DISABLED_USERS, DISABLED_USERS_TIMESTAMPS, DISABLED_USERS_ENABLE_AT
        DISABLED_USERS = set(self.disabled_users.keys())
        DISABLED_USERS_TIMESTAMPS = self.disabled_users.copy()
        DISABLED_USERS_ENABLE_AT = self.enable_at.copy()

    def load_disabled_users(self):
        """
        Loads the disabled users from the JSON file into unified DisabledUserEntry registry.
        """
        global DISABLED_USERS, DISABLED_USERS_TIMESTAMPS, DISABLED_USERS_ENABLE_AT
        try:
            if os.path.exists(self.filename) and os.path.getsize(self.filename) > 0:
                with open(self.filename, "r", encoding="utf-8") as file:
                    data = json.load(file)
                    
                    raw_disabled = {}
                    if "disabled_users" in data:
                        raw_disabled = data.get("disabled_users", {})
                    elif "disable_user" in data:
                        old_users = data.get("disable_user", [])
                        if isinstance(old_users, list):
                            current_time = time.time()
                            raw_disabled = {user: current_time for user in old_users}
                        elif isinstance(old_users, dict):
                            raw_disabled = old_users
                    
                    raw_enable_at = data.get("enable_at", {})
                    
                    self._entries.clear()
                    for username, dis_time in raw_disabled.items():
                        enable_time = raw_enable_at.get(username)
                        self._entries[username] = DisabledUserEntry(
                            username=username,
                            disabled_at=float(dis_time),
                            enable_at=float(enable_time) if enable_time is not None else None
                        )
                    self._sync_views_from_entries()
            else:
                self._entries.clear()
                self.disabled_users = {}
                self.enable_at = {}
                DISABLED_USERS = set()
                DISABLED_USERS_TIMESTAMPS = {}
                DISABLED_USERS_ENABLE_AT = {}
        except Exception as error:  # pylint: disable=broad-except
            logger.error(f"Failed to load disabled users file: {error}")
            try:
                backup_path = self.filename + ".corrupted"
                os.rename(self.filename, backup_path)
                logger.warning(f"Renamed corrupted file to {backup_path}")
            except OSError:
                pass
            self._entries.clear()
            self.disabled_users = {}
            self.enable_at = {}
            DISABLED_USERS = set()
            DISABLED_USERS_TIMESTAMPS = {}
            DISABLED_USERS_ENABLE_AT = {}

    async def reload_disabled_users(self):
        """
        Thread-safe asynchronous reload of disabled users from the JSON file.
        Guaranteed to execute under the shared DISABLED_USERS_LOCK.
        """
        async with self._lock:
            await asyncio.to_thread(self.load_disabled_users)

    def _sync_save_disabled_users(self):
        """Synchronous file write for disabled users data."""
        atomic_write_json(self.filename, {
            "disabled_users": {u: e.disabled_at for u, e in self._entries.items()},
            "enable_at": {u: e.enable_at for u, e in self._entries.items() if e.enable_at is not None}
        })

    async def save_disabled_users(self):
        """
        Saves the disabled users with timestamps to the JSON file.
        Uses shared asyncio.Lock to prevent race conditions and atomic write for crash safety.
        """
        async with self._lock:
            await asyncio.to_thread(self._sync_save_disabled_users)
        logger.info(f"Saved {len(self._entries)} disabled users to {self.filename}")

    async def add_user(self, username: str, duration_seconds: int = 0, permanent: bool = False):
        """
        Adds a user to the set of disabled users with current timestamp
        and saves the updated data to the JSON file.
        
        Args:
            username: The username to disable
            duration_seconds: Optional custom duration in seconds. 
                              0 means use default time_to_active_users from config.
                              Ignored if permanent=True.
            permanent: If True, user will never be auto-enabled (until manual enable).
        """
        async with self._lock:
            current_time = time.time()
            enable_at_val: float | None = None
            
            if permanent:
                enable_at_val = -1.0
                logger.info(f"User {username} disabled permanently at {time.strftime('%H:%M:%S', time.localtime(current_time))}, "
                           f"will NOT be auto-enabled (manual only)")
            elif duration_seconds > 0:
                enable_at_val = current_time + duration_seconds
                enable_time = time.strftime('%H:%M:%S', time.localtime(enable_at_val))
                logger.info(f"User {username} disabled at {time.strftime('%H:%M:%S', time.localtime(current_time))}, "
                           f"will be enabled at {enable_time} ({duration_seconds}s)")
            else:
                enable_time = time.strftime('%H:%M:%S', time.localtime(current_time + 1800))
                logger.info(f"User {username} disabled at {time.strftime('%H:%M:%S', time.localtime(current_time))}, "
                           f"will be enabled around {enable_time} (default)")
            
            self._entries[username] = DisabledUserEntry(
                username=username,
                disabled_at=current_time,
                enable_at=enable_at_val
            )
            self._sync_views_from_entries()
            await asyncio.to_thread(self._sync_save_disabled_users)
        
        # Synchronize with SQLite database if available
        try:
            from db.database import get_db, DB_AVAILABLE
            from db.crud import UserCRUD
            if DB_AVAILABLE:
                async with get_db() as session:
                    await UserCRUD.set_disabled(
                        session,
                        username=username,
                        disabled=True,
                        disabled_at=current_time,
                        enable_at=enable_at_val,
                        fetch_from_panel=False,
                    )
                    await session.commit()
        except Exception as e:
            logger.debug(f"Database sync on disable skipped for {username}: {e}")

        # Synchronize with Redis cache if available
        try:
            from utils.redis_cache import add_disabled_user
            await add_disabled_user(username, current_time)
        except Exception:
            pass

        logger.info(f"Saved {len(self._entries)} disabled users to {self.filename}")

    async def remove_user(self, username: str):
        """
        Removes a user from the disabled users registry.
        """
        async with self._lock:
            if username in self._entries:
                del self._entries[username]
            self._sync_views_from_entries()
            await asyncio.to_thread(self._sync_save_disabled_users)
        
        # Synchronize with SQLite database if available
        try:
            from db.database import get_db, DB_AVAILABLE
            from db.crud import UserCRUD
            if DB_AVAILABLE:
                async with get_db() as session:
                    await UserCRUD.set_disabled(
                        session,
                        username=username,
                        disabled=False,
                        fetch_from_panel=False,
                    )
                    await session.commit()
        except Exception as e:
            logger.debug(f"Database sync on enable skipped for {username}: {e}")

        # Synchronize with Redis cache if available
        try:
            from utils.redis_cache import remove_disabled_user
            await remove_disabled_user(username)
        except Exception:
            pass

        logger.info(f"Saved {len(self._entries)} disabled users to {self.filename}")

    async def get_users_to_enable(self, default_time_to_active: int) -> list[str]:
        """
        Returns a list of users who should be enabled now.
        Uses custom enable_at time if set, otherwise uses default_time_to_active.
        
        Args:
            default_time_to_active: Default time in seconds to wait before enabling
            
        Returns:
            List of usernames ready to be enabled
        """
        async with self._lock:
            await asyncio.to_thread(self.load_disabled_users)
            
            current_time = time.time()
            users_to_enable = []
            
            if self._entries:
                logger.info(f"Checking {len(self._entries)} disabled users (default={default_time_to_active}s)")
            
            for username, entry in list(self._entries.items()):
                if entry.enable_at is not None:
                    if entry.enable_at == -1:
                        logger.debug(f"User {username} is permanently disabled (manual enable only)")
                        continue
                    if current_time >= entry.enable_at:
                        users_to_enable.append(username)
                        logger.info(f"User {username} ready to enable (custom timer expired)")
                    else:
                        remaining = int(entry.enable_at - current_time)
                        logger.debug(f"User {username} has {remaining}s remaining on custom timer")
                else:
                    elapsed = current_time - entry.disabled_at
                    remaining = default_time_to_active - elapsed
                    if elapsed >= default_time_to_active:
                        users_to_enable.append(username)
                        logger.info(f"User {username} ready to enable (disabled {int(elapsed)}s ago)")
                    else:
                        logger.debug(f"User {username} needs {int(remaining)}s more before enable")
            
            return users_to_enable

    def get_user_remaining_time(self, username: str, default_time_to_active: int) -> RemainingTimeResult:
        """
        Get structured remaining disable time for a user.
        
        Args:
            username: The username to check
            default_time_to_active: Default time in seconds
            
        Returns:
            RemainingTimeResult: Structured status and remaining seconds
        """
        entry = self._entries.get(username)
        if not entry:
            return RemainingTimeResult(status=DisableStatus.NOT_DISABLED, seconds=0)
        
        current_time = time.time()
        if entry.enable_at is not None:
            if entry.enable_at == -1:
                return RemainingTimeResult(status=DisableStatus.PERMANENT, seconds=0)
            remaining = int(entry.enable_at - current_time)
            if remaining <= 0:
                return RemainingTimeResult(status=DisableStatus.READY_TO_ENABLE, seconds=0)
            return RemainingTimeResult(status=DisableStatus.TIMED, seconds=remaining)
        
        elapsed = current_time - entry.disabled_at
        remaining = int(default_time_to_active - elapsed)
        if remaining <= 0:
            return RemainingTimeResult(status=DisableStatus.READY_TO_ENABLE, seconds=0)
        return RemainingTimeResult(status=DisableStatus.TIMED, seconds=remaining)

    def is_disabled(self, username: str) -> bool:
        """Check if a user is currently registered as disabled."""
        return username in self._entries

    async def read_and_clear_users(self) -> set[str]:
        """
        Returns a set of all disabled users, clears the registry
        and saves the empty data to the JSON file.
        """
        async with self._lock:
            disabled_users = set(self._entries.keys())
            self._entries.clear()
            self._sync_views_from_entries()
            await asyncio.to_thread(self._sync_save_disabled_users)
        return disabled_users

