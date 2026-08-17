"""
User Sync Module
Periodically syncs users from the panel to local database for efficient filtering.
"""

import asyncio
from datetime import datetime, timezone
from typing import Optional

from utils.logs import get_logger
from utils.types import PanelType

sync_logger = get_logger("user_sync")

# Last sync timestamp and sync lock
_last_sync_time: Optional[datetime] = None
_sync_lock: asyncio.Lock = asyncio.Lock()

# In-Memory User Metadata Cache populated during User Sync
# Format: {username: {"group_ids": [...], "owner_username": "...", "is_excepted": bool, "special_limit": int, "is_monitored": bool, "effective_ip_limit": int}}
USER_METADATA_CACHE: dict[str, dict] = {}

# Background Queue & Throttled Worker for Unknown Users
_UNKNOWN_USERS_QUEUE: asyncio.Queue = asyncio.Queue()
_UNKNOWN_USERS_FETCHING: set[str] = set()


def calculate_user_effective_limit_and_monitoring(
    username: str,
    group_ids: list[int],
    is_excepted: bool = False,
    special_limit: int | None = None,
    config: dict = None,
) -> tuple[bool, int | None]:
    """
    Pre-compute is_monitored and effective_ip_limit for a user in O(1) time.
    
    Returns:
        (is_monitored: bool, effective_ip_limit: int | None)
    """
    if config is None:
        config = {}
        
    # 1. Exception / Whitelist check
    if is_excepted:
        return (False, None)
    
    # 2. Group Filter check
    group_filter = config.get("group_filter", {})
    is_monitored = True
    
    if group_filter.get("enabled", False):
        filter_mode = group_filter.get("mode", "include")
        filter_group_ids = [str(x) for x in group_filter.get("group_ids", [])]
        user_group_ids_str = [str(x) for x in (group_ids or [])]
        user_in_filter = any(g in filter_group_ids for g in user_group_ids_str)
        
        if filter_mode == "include":
            is_monitored = user_in_filter
        else:
            is_monitored = not user_in_filter

    if not is_monitored:
        return (False, None)

    # 3. Effective Limit Calculation
    # Priority A: Special limit set specifically for this user
    if special_limit is not None and special_limit > 0:
        return (True, special_limit)

    # Priority B: Group Limits from config
    group_limits = config.get("group_limits", {})
    if group_limits and group_ids:
        matching_limits = []
        for gid in group_ids:
            gid_str = str(gid)
            if gid_str in group_limits:
                try:
                    matching_limits.append(int(group_limits[gid_str]))
                except (ValueError, TypeError):
                    pass
        if matching_limits:
            return (True, max(matching_limits))

    # Priority C: Username Suffix Regex Pattern (e.g. .2.User or 2User)
    try:
        from utils.check_usage import extract_limit_from_username
        pattern_limit = extract_limit_from_username(username)
        if pattern_limit is not None:
            return (True, pattern_limit)
    except Exception:
        pass

    # Priority D: Default to General Limit (None indicates use default general limit)
    return (True, None)


async def recompute_all_user_limits(config: dict = None):
    """
    Instantly recompute is_monitored and effective_ip_limit for all users in RAM cache
    without making ANY network API calls to Pasargad panel.
    """
    global USER_METADATA_CACHE
    if config is None:
        try:
            from utils.read_config import read_config
            config = await read_config()
        except Exception as e:
            sync_logger.error(f"Error reading config during limit recompute: {e}")
            config = {}
    
    recomputed_count = 0
    for username, data in USER_METADATA_CACHE.items():
        is_monitored, effective_limit = calculate_user_effective_limit_and_monitoring(
            username=username,
            group_ids=data.get("group_ids", []),
            is_excepted=data.get("is_excepted", False),
            special_limit=data.get("special_limit"),
            config=config,
        )
        data["is_monitored"] = is_monitored
        data["effective_ip_limit"] = effective_limit
        recomputed_count += 1
    
    sync_logger.info(f"⚡ Recomputed effective limits in RAM for {recomputed_count} users (0ms network calls)")
    
    # Broadcast Pub/Sub signal for cross-process sync
    try:
        from utils.redis_cache import publish_cache_invalidation
        await publish_cache_invalidation(reason="recompute_limits")
    except Exception as err:
        sync_logger.debug(f"PubSub publish note: {err}")


def invalidate_user_metadata_cache(username: Optional[str] = None):
    """
    Invalidate in-memory USER_METADATA_CACHE (L0).
    If username is None, clears the entire cache.
    """
    global USER_METADATA_CACHE
    if username is None:
        USER_METADATA_CACHE.clear()
        sync_logger.debug("🧹 Cleared entire in-memory USER_METADATA_CACHE")
    else:
        if username in USER_METADATA_CACHE:
            USER_METADATA_CACHE.pop(username, None)
            sync_logger.debug(f"🧹 Invalidated {username} from in-memory USER_METADATA_CACHE")


async def refresh_user_metadata_cache(db=None):
    """Refresh the in-memory USER_METADATA_CACHE from SQLite database."""
    global USER_METADATA_CACHE
    try:
        from db.models import User
        from sqlalchemy import select
        
        async def _fetch(session):
            stmt = select(
                User.username,
                User.group_ids,
                User.owner_username,
                User.is_excepted,
                User.special_limit,
                User.is_monitored,
                User.effective_ip_limit
            )
            result = await session.execute(stmt)
            new_cache = {}
            for row in result:
                new_cache[row.username] = {
                    "group_ids": row.group_ids or [],
                    "owner_username": row.owner_username,
                    "is_excepted": bool(row.is_excepted),
                    "special_limit": row.special_limit,
                    "is_monitored": bool(row.is_monitored) if row.is_monitored is not None else True,
                    "effective_ip_limit": row.effective_ip_limit
                }
            return new_cache

        if db is not None:
            USER_METADATA_CACHE = await _fetch(db)
        else:
            from db.database import get_db
            async with get_db() as session:
                USER_METADATA_CACHE = await _fetch(session)
                
        sync_logger.info(f"🧠 USER_METADATA_CACHE updated in RAM with {len(USER_METADATA_CACHE)} users")
    except Exception as e:
        sync_logger.error(f"Failed to refresh USER_METADATA_CACHE: {e}")


async def queue_unknown_user_fetch(username: str):
    """Queue an unknown user for background fetch without blocking or spamming the API."""
    if username and username not in USER_METADATA_CACHE and username not in _UNKNOWN_USERS_FETCHING:
        _UNKNOWN_USERS_FETCHING.add(username)
        await _UNKNOWN_USERS_QUEUE.put(username)


async def run_unknown_user_worker(panel_data: PanelType):
    """Worker task that processes unknown users with a semaphore to prevent API throttling."""
    semaphore = asyncio.Semaphore(5)  # Max 5 concurrent API fetches
    
    async def _fetch(u: str):
        async with semaphore:
            try:
                await fetch_and_sync_single_user(u, panel_data)
            except Exception as ex:
                sync_logger.error(f"Error fetching unknown user {u}: {ex}")
            finally:
                _UNKNOWN_USERS_FETCHING.discard(u)
    
    while True:
        try:
            username = await _UNKNOWN_USERS_QUEUE.get()
            asyncio.create_task(_fetch(username))
            _UNKNOWN_USERS_QUEUE.task_done()
        except asyncio.CancelledError:
            break
        except Exception as e:
            sync_logger.error(f"Error in unknown user worker loop: {e}")
            await asyncio.sleep(1)


async def get_all_users_with_details(panel_data: PanelType, status: str | None = None) -> list[dict]:
    """
    Fetch all users from panel with their full details (groups, owner, etc.).
    Uses centralized parallel pagination with persistent connection pooling for high throughput.
    
    Args:
        panel_data: Panel connection data
        status: Filter by user status (active/disabled/limited/expired/on_hold).
                Default is None to fetch ALL users (recommended for sync).
        
    Returns:
        List of user dictionaries with full details
    """
    from utils.panel_api.users import fetch_all_users_raw
    try:
        return await fetch_all_users_raw(panel_data, status=status)
    except Exception as e:
        sync_logger.error(f"❌ Failed to fetch users with details: {e}")
        return []


async def sync_users_to_database(panel_data: PanelType) -> tuple[int, int, int]:
    """
    Sync all users from panel to local database.
    Also detects users deleted from panel and removes them from limiter.
    
    Args:
        panel_data: Panel connection data
        
    Returns:
        Tuple of (synced_count, error_count, deleted_count)
    """
    global _last_sync_time
    
    if _sync_lock.locked():
        sync_logger.warning("Sync already in progress, skipping")
        return (0, 0, 0)
    
    async with _sync_lock:
        synced = 0
        errors = 0
        deleted = 0
        deleted_usernames = []
        
        try:
            sync_logger.info("🔄 Starting user sync from panel to database...")
            start_time = datetime.now(timezone.utc)
            
            # Fetch ALL users from panel (all statuses) to avoid losing disabled users
            users = await get_all_users_with_details(panel_data, status=None)
            
            if not users:
                sync_logger.warning("⚠️ No users fetched from panel - skipping sync entirely")
                return (0, 0, 0)
            
            sync_logger.info(f"📥 Processing {len(users)} users...")
            
            # Build set of usernames from panel
            panel_usernames = {u.get("username") for u in users if u.get("username")}
            
            if not panel_usernames:
                sync_logger.warning("⚠️ No valid usernames in panel response - skipping sync")
                return (0, 0, 0)
            
            # Import database modules here to avoid circular imports
            sync_logger.info("📂 Importing database modules...")
            from db.database import get_db
            from db.crud.users import UserCRUD
            
            sync_logger.info("📂 Opening database connection for sync...")
            
            from utils.read_config import read_config
            config = await read_config()
            
            async with get_db() as db:
                sync_logger.info("✅ Database connection opened")
                # Get existing usernames in local DB
                local_usernames = await UserCRUD.get_all_usernames(db)
                sync_logger.info(f"📊 Found {len(local_usernames)} existing users in local DB")
                
                # Sync users from panel using native Bulk Upsert
                users_to_upsert = []
                for user_data in users:
                    try:
                        username = user_data.get("username")
                        if not username:
                            continue
                        
                        panel_id = user_data.get("id")
                        status = user_data.get("status", "active")
                        
                        admin_info = user_data.get("admin", {}) or {}
                        owner_id = admin_info.get("id") if isinstance(admin_info, dict) else None
                        owner_username = admin_info.get("username") if isinstance(admin_info, dict) else None
                        if not owner_username:
                            owner_username = user_data.get("created_by")
                        
                        group_ids = user_data.get("group_ids") or user_data.get("groups") or []
                        if isinstance(group_ids, str):
                            group_ids = [int(g.strip()) for g in group_ids.split(",") if g.strip()]
                        
                        data_limit = user_data.get("data_limit")
                        if data_limit:
                            data_limit = data_limit / (1024 ** 3)
                        
                        used_traffic = user_data.get("used_traffic", 0)
                        if used_traffic:
                            used_traffic = used_traffic / (1024 ** 3)
                        
                        expire_at = None
                        expire_value = user_data.get("expire")
                        if expire_value:
                            if isinstance(expire_value, int):
                                if expire_value > 0:
                                    expire_at = datetime.fromtimestamp(expire_value)
                            elif isinstance(expire_value, str):
                                try:
                                    expire_at = datetime.fromisoformat(expire_value.replace("Z", "+00:00"))
                                except ValueError:
                                    pass
                        
                        note = user_data.get("note")
                        
                        # Pre-compute is_monitored and effective_ip_limit
                        is_monitored, effective_limit = calculate_user_effective_limit_and_monitoring(
                            username=username,
                            group_ids=group_ids,
                            is_excepted=False,
                            special_limit=None,
                            config=config,
                        )
                        
                        users_to_upsert.append({
                            "username": username,
                            "panel_id": panel_id,
                            "status": status,
                            "owner_id": owner_id,
                            "owner_username": owner_username,
                            "group_ids": group_ids,
                            "data_limit": data_limit,
                            "used_traffic": used_traffic,
                            "expire_at": expire_at,
                            "note": note,
                            "is_monitored": is_monitored,
                            "effective_ip_limit": effective_limit,
                        })
                    except Exception as e:
                        sync_logger.error(f"Error parsing user {user_data.get('username', '?')}: {e}")
                        errors += 1
                
                # Execute Native SQLite Bulk Upsert
                synced = await UserCRUD.bulk_upsert_users(db, users_to_upsert)
                await db.commit()
                sync_logger.info(f"✅ Native Bulk Upsert committed: {synced} synced in single transaction")
                
                # Refresh in-memory RAM metadata cache
                await refresh_user_metadata_cache(db)
                await recompute_all_user_limits(config)
                
                # SAFETY CHECKS before deleting users
                # Only delete if sync was mostly successful (less than 10% errors)
                # and we received a reasonable number of users from panel
                potentially_deleted = list(local_usernames - panel_usernames)
                error_rate = errors / max(len(users), 1)
                
                if potentially_deleted:
                    # Check if auto-deletion is enabled in config
                    auto_delete_enabled = config.get("user_sync", {}).get("auto_delete_users", False)
                    
                    if not auto_delete_enabled:
                        sync_logger.info(
                            f"ℹ️ Auto-deletion disabled. {len(potentially_deleted)} users not in panel but kept in local DB. "
                            f"Enable 'auto_delete_users' in config or use Telegram bot to review."
                        )
                        # Log the users that would have been deleted
                        if len(potentially_deleted) <= 20:
                            sync_logger.info(f"Users not in panel: {', '.join(potentially_deleted)}")
                        else:
                            sync_logger.info(f"Users not in panel (first 20): {', '.join(potentially_deleted[:20])}...")
                    
                    # Safety check 1: Don't delete if there were too many sync errors
                    elif error_rate > 0.1:  # More than 10% errors
                        sync_logger.warning(
                            f"⚠️ Skipping deletion: too many sync errors ({errors}/{len(users)} = {error_rate:.1%})"
                        )
                    # Safety check 2: Don't delete if panel returned significantly fewer users
                    # This could indicate a pagination or API issue
                    elif len(local_usernames) > 0 and len(panel_usernames) < len(local_usernames) * 0.5:
                        sync_logger.warning(
                            f"⚠️ Skipping deletion: panel returned too few users "
                            f"({len(panel_usernames)} vs {len(local_usernames)} local). "
                            f"This may indicate an API issue."
                        )
                    # Safety check 3: Don't delete more than 10% of users in one sync (stricter)
                    elif len(potentially_deleted) > len(local_usernames) * 0.1:
                        sync_logger.warning(
                            f"⚠️ Skipping deletion: too many users to delete "
                            f"({len(potentially_deleted)}/{len(local_usernames)} = "
                            f"{len(potentially_deleted)/len(local_usernames):.1%}). "
                            f"Manual review recommended via Telegram bot."
                        )
                    # Safety check 4: Don't delete more than 50 users at once
                    elif len(potentially_deleted) > 50:
                        sync_logger.warning(
                            f"⚠️ Skipping deletion: {len(potentially_deleted)} users is too many to delete at once. "
                            f"Manual review recommended via Telegram bot."
                        )
                    else:
                        # All safety checks passed - proceed with deletion
                        sync_logger.info(f"🗑️ Deleting {len(potentially_deleted)} users removed from panel")
                        deleted_usernames = potentially_deleted
                        deleted = await UserCRUD.delete_many(db, deleted_usernames)
                
                await db.commit()
            
            elapsed = (datetime.now(timezone.utc) - start_time).total_seconds()
            _last_sync_time = datetime.now(timezone.utc)
            
            sync_logger.info(
                f"✅ User sync completed: {synced} synced, {deleted} deleted, "
                f"{errors} errors in {elapsed:.1f}s"
            )
            
            # Send Telegram notification for deleted users
            if deleted_usernames:
                await _notify_deleted_users(deleted_usernames)
            
        except Exception as e:
            import traceback
            sync_logger.error(f"❌ User sync failed: {type(e).__name__}: {e}")
            sync_logger.error(f"Traceback: {traceback.format_exc()}")
        
        return (synced, errors, deleted)


async def _notify_deleted_users(usernames: list[str]) -> None:
    """Send Telegram notification for users deleted from panel."""
    try:
        from telegram_bot.send_message import send_logs
        
        if len(usernames) <= 10:
            user_list = "\n".join(f"• <code>{u}</code>" for u in usernames)
        else:
            user_list = "\n".join(f"• <code>{u}</code>" for u in usernames[:10])
            user_list += f"\n... and {len(usernames) - 10} more"
        
        message = (
            "🗑️ <b>Users Deleted from Panel</b>\n\n"
            "The following users were deleted from panel and "
            "have been removed from PG-Limiter:\n\n"
            f"{user_list}\n\n"
            f"📊 Total: {len(usernames)} users"
        )
        
        await send_logs(message)
        sync_logger.info(f"📤 Sent notification for {len(usernames)} deleted users")
        
    except Exception as e:
        sync_logger.error(f"Failed to send deletion notification: {e}")


async def get_user_from_cache(username: str) -> Optional[dict]:
    """
    Get user data from local database cache.
    
    Args:
        username: The username to look up
        
    Returns:
        User dict with group_ids and owner_username, or None if not found
    """
    try:
        from db.database import get_db
        from db.crud.users import UserCRUD
        
        async with get_db() as db:
            user = await UserCRUD.get_by_username(db, username)
            if user:
                return {
                    "username": user.username,
                    "status": user.status,
                    "owner_id": user.owner_id,
                    "owner_username": user.owner_username,
                    "group_ids": user.group_ids or [],
                    "data_limit": user.data_limit,
                    "used_traffic": user.used_traffic,
                    "expire_at": user.expire_at,
                    "last_synced_at": user.last_synced_at,
                }
        return None
    except Exception as e:
        sync_logger.error(f"Error getting user from cache: {e}")
        return None


async def fetch_and_sync_single_user(username: str, panel_data: Optional[PanelType] = None) -> Optional[dict]:
    """
    Fetch a single user from the panel API and sync to local database.
    This is useful when a new user appears in active connections before the regular sync.
    
    Args:
        username: The username to fetch
        panel_data: Panel connection data (will be fetched from config if not provided)
        
    Returns:
        User dict if found and synced, None otherwise
    """
    try:
        sync_logger.info(f"🔄 Fetching single user from panel: {username}")
        
        # Validate username
        if not username or not username.strip():
            sync_logger.warning("⚠️ Cannot fetch user with empty username")
            return None
        
        # Get panel_data from config if not provided
        if panel_data is None:
            from utils.read_config import read_config
            config = await read_config()
            panel_config = config.get("panel", {})
            panel_data = PanelType(
                panel_username=panel_config.get("username", ""),
                panel_password=panel_config.get("password", ""),
                panel_domain=panel_config.get("domain", ""),
            )
        
        # Validate panel_data has required fields
        if not panel_data.panel_domain:
            sync_logger.error("❌ Panel domain is not configured - cannot fetch user from panel")
            return None
        
        # Fetch user details from panel
        from utils.panel_api import get_user_details
        user_data = await get_user_details(panel_data, username)
        
        if user_data is None:
            sync_logger.warning(f"⚠️ User {username} not found in panel")
            return None
        
        if isinstance(user_data, ValueError):
            sync_logger.error(f"❌ Error fetching user {username}: {user_data}")
            return None
        
        # Extract user details (same logic as sync_users_to_database)
        panel_id = user_data.get("id")
        status = user_data.get("status", "active")
        
        # Get admin/owner info
        admin_info = user_data.get("admin", {}) or {}
        owner_id = admin_info.get("id") if isinstance(admin_info, dict) else None
        owner_username = admin_info.get("username") if isinstance(admin_info, dict) else None
        
        # Alternative: check for "created_by" field
        if not owner_username:
            owner_username = user_data.get("created_by")
        
        # Get group IDs
        group_ids = user_data.get("group_ids") or user_data.get("groups") or []
        if isinstance(group_ids, str):
            group_ids = [int(g.strip()) for g in group_ids.split(",") if g.strip()]
        
        # Get data limits
        data_limit = user_data.get("data_limit")
        if data_limit:
            data_limit = data_limit / (1024 ** 3)  # Convert to GB
        
        used_traffic = user_data.get("used_traffic", 0)
        if used_traffic:
            used_traffic = used_traffic / (1024 ** 3)  # Convert to GB
        
        # Get expiry
        expire_at = None
        expire_value = user_data.get("expire")
        if expire_value:
            if isinstance(expire_value, int):
                # Unix timestamp
                if expire_value > 0:
                    expire_at = datetime.fromtimestamp(expire_value)
            elif isinstance(expire_value, str):
                # ISO datetime string
                try:
                    expire_at = datetime.fromisoformat(
                        expire_value.replace("Z", "+00:00")
                    )
                except ValueError:
                    pass
        
        # Get note
        note = user_data.get("note")
        
        # Save to database
        from db.database import get_db
        from db.crud.users import UserCRUD
        
        async with get_db() as db:
            user = await UserCRUD.create_or_update(
                db,
                username=username,
                panel_id=panel_id,
                status=status,
                owner_id=owner_id,
                owner_username=owner_username,
                group_ids=group_ids,
                data_limit=data_limit,
                used_traffic=used_traffic,
                expire_at=expire_at,
                note=note,
            )
            await db.commit()
            
            sync_logger.info(f"✅ User {username} synced from panel to database")
            
            return {
                "username": user.username,
                "status": user.status,
                "owner_id": user.owner_id,
                "owner_username": user.owner_username,
                "group_ids": user.group_ids or [],
                "data_limit": user.data_limit,
                "used_traffic": user.used_traffic,
                "expire_at": user.expire_at,
            }
            
    except Exception as e:
        sync_logger.error(f"❌ Error fetching/syncing user {username}: {e}")
        import traceback
        sync_logger.error(f"Traceback: {traceback.format_exc()}")
        return None


async def get_last_sync_time() -> Optional[datetime]:
    """Get the last sync timestamp."""
    return _last_sync_time


async def is_sync_needed(sync_interval_minutes: int) -> bool:
    """
    Check if a sync is needed based on interval.
    
    Args:
        sync_interval_minutes: Sync interval in minutes
        
    Returns:
        True if sync is needed
    """
    if _last_sync_time is None:
        return True
    
    elapsed = (datetime.now(timezone.utc) - _last_sync_time).total_seconds()
    return elapsed >= sync_interval_minutes * 60


async def run_user_sync_loop(panel_data: PanelType):
    """
    Run the user sync loop. This should be started as a background task.
    
    Args:
        panel_data: Panel connection data
    """
    from utils.read_config import read_config
    
    sync_logger.info("🚀 Starting user sync background loop...")
    
    # Delay initial sync to allow other startup operations to complete
    # This reduces memory pressure during startup
    sync_logger.info("⏳ Waiting 30 seconds before initial user sync...")
    await asyncio.sleep(30)
    
    # Initial sync
    sync_logger.info("🔄 Running initial user sync...")
    await sync_users_to_database(panel_data)
    
    while True:
        try:
            config = await read_config()
            sync_interval = config.get("user_sync_interval", 5)  # Default 5 minutes
            
            # Wait for interval
            await asyncio.sleep(sync_interval * 60)
            
            # Check if sync is needed
            if await is_sync_needed(sync_interval):
                await sync_users_to_database(panel_data)
                
        except asyncio.CancelledError:
            sync_logger.info("User sync loop cancelled")
            break
        except Exception as e:
            sync_logger.error(f"Error in user sync loop: {e}")
            await asyncio.sleep(60)  # Wait before retry


async def get_pending_deletions(panel_data: PanelType) -> dict:
    """
    Get the list of users that would be deleted during sync.
    This allows manual review before force-deleting.
    
    Args:
        panel_data: Panel connection data
        
    Returns:
        Dictionary with pending deletions info:
        {
            "pending_deletions": ["user1", "user2", ...],
            "local_count": 100,
            "panel_count": 80,
            "deletion_percentage": 20.0,
            "safe_to_delete": True/False,
            "reason": "reason if not safe"
        }
    """
    from db.database import get_db
    from db.crud import UserCRUD
    
    result = {
        "pending_deletions": [],
        "local_count": 0,
        "panel_count": 0,
        "deletion_percentage": 0.0,
        "safe_to_delete": True,
        "reason": ""
    }
    
    try:
        # Get ALL users from panel (not just active) for proper deletion comparison
        users = await get_all_users_with_details(panel_data, status=None)
        panel_usernames = {u.get("username") for u in users if u.get("username")}
        result["panel_count"] = len(panel_usernames)
        
        # Get local users
        async with get_db() as db:
            local_users = await UserCRUD.get_all(db)
            local_usernames = {u.username for u in local_users}
            result["local_count"] = len(local_usernames)
        
        # Find users to delete
        pending = list(local_usernames - panel_usernames)
        result["pending_deletions"] = sorted(pending)
        
        if local_usernames:
            result["deletion_percentage"] = (len(pending) / len(local_usernames)) * 100
        
        # Check safety
        if len(pending) > len(local_usernames) * 0.2:
            result["safe_to_delete"] = False
            result["reason"] = f"Would delete more than 20% of users ({result['deletion_percentage']:.1f}%)"
        elif len(panel_usernames) < len(local_usernames) * 0.5:
            result["safe_to_delete"] = False
            result["reason"] = f"Panel returned significantly fewer users ({len(panel_usernames)} vs {len(local_usernames)})"
            
    except Exception as e:
        sync_logger.error(f"Error getting pending deletions: {e}")
        result["reason"] = f"Error: {e}"
        result["safe_to_delete"] = False
    
    return result


async def force_delete_users(usernames: list[str]) -> tuple[int, list[str]]:
    """
    Force delete specific users from local database.
    Use after manual review of pending deletions.
    
    Args:
        usernames: List of usernames to delete
        
    Returns:
        Tuple of (deleted_count, errors)
    """
    from db.database import get_db
    from db.crud import UserCRUD
    
    deleted = 0
    errors = []
    
    async with get_db() as db:
        for username in usernames:
            try:
                result = await UserCRUD.delete(db, username)
                if result:
                    deleted += 1
                else:
                    errors.append(f"{username}: not found")
            except Exception as e:
                errors.append(f"{username}: {e}")
        
        await db.commit()
    
    if deleted:
        await _notify_deleted_users(usernames[:deleted])
    
    return deleted, errors
