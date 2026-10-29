"""
User Sync Module
Periodically syncs users from the panel to local database for efficient filtering.
"""

import asyncio
from datetime import datetime, timezone
from html import escape
from typing import Optional
from urllib.parse import quote

from utils.logs import get_logger
from utils.panel_workload import serialized_panel_workload
from utils.types import PanelType
from utils.user_policy import evaluate_user_policy
from utils.user_policy import get_max_group_limit as get_max_group_limit
from utils.user_policy import resolve_group_limit as resolve_group_limit

sync_logger = get_logger("user_sync")

# Last sync timestamp and sync lock
_last_sync_time: Optional[datetime] = None
_sync_lock: asyncio.Lock = asyncio.Lock()

# In-Memory User Metadata Cache populated during User Sync
# Factual entries: {username: {"group_ids": [...], "owner_username": "..."}}
USER_METADATA_CACHE: dict[str, dict] = {}
# Lock to guard write operations on USER_METADATA_CACHE (reads are lock-free)
_metadata_cache_lock: asyncio.Lock = asyncio.Lock()

# Background Queue & Throttled Worker for Unknown Users
_UNKNOWN_USERS_QUEUE: Optional[asyncio.Queue] = None
_UNKNOWN_USERS_FETCHING: set[str] = set()


def get_unknown_users_queue() -> asyncio.Queue:
    """
    Return the process-wide queue of usernames waiting for a panel lookup.

    Created once per process. It used to be rebuilt whenever the caller ran on
    a different event loop - a leftover from the in-process restart in
    ``limiter.py`` - and every username still queued was dropped with it.
    """
    global _UNKNOWN_USERS_QUEUE
    if _UNKNOWN_USERS_QUEUE is None:
        _UNKNOWN_USERS_QUEUE = asyncio.Queue()
    return _UNKNOWN_USERS_QUEUE


def calculate_user_effective_limit_and_monitoring(
    username: str,
    group_ids: list[int],
    is_excepted: bool = False,
    special_limit: int | None = None,
    config: dict | None = None,
) -> tuple[bool, int | None]:
    """Compatibility display result, with None marking the general fallback.

    Legacy is_excepted/special_limit inputs are ignored: current canonical config
    supplies whitelist membership and special overrides.
    """
    policy = evaluate_user_policy(
        username=username,
        config=config or {},
        facts={"group_ids": group_ids, "owner_username": None},
    )
    limit = policy.limit if policy.eligible and policy.limit_source != "general" else None
    return policy.eligible, limit


def _parse_panel_user_dict(
    user_data: dict,
    config: dict | None = None,
    *,
    is_excepted: bool = False,
    special_limit: int | None = None,
) -> dict | None:
    """Parse only panel-owned facts; legacy policy arguments are ignored."""
    if not isinstance(user_data, dict):
        return None
    username = user_data.get("username")
    if not username:
        return None

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
        data_limit = data_limit / (1024**3)

    used_traffic = user_data.get("used_traffic", 0)
    if used_traffic:
        used_traffic = used_traffic / (1024**3)

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

    return {
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
    }


async def recompute_all_user_limits(config: dict = None):
    """
    Recompute legacy display fields from current configuration and factual cache entries.
    These fields are not authoritative inputs to policy evaluation.
    """
    global USER_METADATA_CACHE
    if config is None:
        try:
            from utils.read_config import read_config

            config = await read_config()
        except Exception as e:
            # Falling back to {} here used to be the same hazard as a degraded config:
            # an empty config has no group filter, which reads as "monitor everyone".
            sync_logger.error(
                f"⛔ Skipping the effective-limit recompute: the configuration could not "
                f"be read ({e}). The existing flags are kept."
            )
            return

    # A configuration whose database half failed has no group filter and no group
    # limits, and an absent group filter reads as "monitor everyone". Recomputing from
    # it would write is_monitored=True over every user the operator had filtered out -
    # straight into the cache the enforcement gate reads - so the filter would stop
    # being honoured until the next successful sync. Enforcement is skipped while the
    # flag is set, but the flags would outlive it.
    if config.get("config_degraded"):
        sync_logger.error(
            "⛔ Skipping the effective-limit recompute: the configuration is degraded "
            "(no database settings), so the group filter and group limits are unknown. "
            "The existing flags are kept until a database read succeeds."
        )
        return

    recomputed_count = 0
    for username, data in list(USER_METADATA_CACHE.items()):
        policy = evaluate_user_policy(username=username, config=config, facts=data)
        data["is_monitored"] = policy.eligible
        data["effective_ip_limit"] = policy.limit if policy.limit_source != "general" else None
        recomputed_count += 1

    sync_logger.info(f"⚡ Recomputed effective limits in RAM for {recomputed_count} users (0ms network calls)")


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
            )
            result = await session.execute(stmt)
            new_cache = {}
            for row in result:
                new_cache[row.username] = {
                    "group_ids": row.group_ids or [],
                    "owner_username": row.owner_username,
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
        queue = get_unknown_users_queue()
        await queue.put(username)


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

    queue = get_unknown_users_queue()
    while True:
        try:
            username = await queue.get()
            asyncio.create_task(_fetch(username))
            queue.task_done()
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

    # Validate every page while allowing natural population changes. The caller
    # defers membership deletion when this listing reports churn. Username
    # supplies deterministic ordering on the full /api/users endpoint.
    return await fetch_all_users_raw(panel_data, status=status, sort="username", strict=True, allow_live_changes=True)


async def _panel_confirms_user_absent(panel_data: PanelType, username: str) -> bool:
    """Require the panel's explicit missing-user response, preserving unknowns."""
    from utils.panel_api.request_helper import panel_get

    try:
        response = await panel_get(panel_data, f"/api/user/{quote(username, safe='')}", timeout=15.0, max_retries=1)
        if response is None or response.status_code != 404:
            return False
        body = response.json()
        return isinstance(body, dict) and body.get("detail") == "User not found"
    except Exception:
        # Neither dependency exception text nor panel bodies belong in sync logs.
        return False


async def sync_users_to_database(panel_data: PanelType) -> tuple[int, int, int]:
    """
    Sync all users from panel to local database.
    Mirror the validated panel list, confirming current absence before deletion.
    Wait for API IP collection/rechecks before starting the full sync.
    Commit bounded batches so settings and enforcement writers can keep running.
    Failed fetches/parses never change membership; failed writes never prune it.

    Args:
        panel_data: Panel connection data

    Returns:
        Tuple of (synced_count, error_count, deleted_count)
    """
    global _last_sync_time

    if _sync_lock.locked():
        sync_logger.warning("Sync already in progress, skipping")
        return (0, 0, 0)

    async with _sync_lock, serialized_panel_workload("user sync"):
        synced = 0
        errors = 0
        deleted = 0
        deleted_usernames = []

        try:
            sync_logger.info("🔄 Starting user sync from panel to database...")
            start_time = datetime.now(timezone.utc)

            # Snapshot local rows before any network wait, then close the read
            # transaction. Later edits and inserts belong to concurrent writers.
            from db.database import get_db
            from db.crud.users import UserCRUD
            from db.models import User
            from sqlalchemy import select, text

            batch_size = 500
            columns = tuple(User.__table__.columns.keys())
            async with get_db() as db:
                result = await db.execute(select(User))
                deletion_snapshots = {
                    user.username: tuple(getattr(user, column) for column in columns) for user in result.scalars()
                }

            # Fetch ALL users from panel (all statuses) to avoid losing disabled users
            users = await get_all_users_with_details(panel_data, status=None)
            pagination_changed = getattr(users, "pagination_changed", False)

            sync_logger.info(f"📥 Processing {len(users)} users...")

            # Parse the entire validated snapshot before making any writes.
            users_to_upsert = []
            for user_data in users:
                try:
                    parsed = _parse_panel_user_dict(user_data)
                    if parsed is None:
                        raise ValueError("Invalid panel user")
                    users_to_upsert.append(parsed)
                except Exception:
                    errors += 1
            if errors:
                sync_logger.error(f"❌ User sync skipped: {errors} invalid panel records")
                return (0, errors, 0)
            panel_usernames = {user["username"] for user in users_to_upsert}
            deletion_snapshots = {
                name: snapshot for name, snapshot in deletion_snapshots.items() if name not in panel_usernames
            }

            sync_logger.info("📂 Opening database connection for sync...")

            async with get_db() as db:
                sync_logger.info("✅ Database connection opened")
                for offset in range(0, len(users_to_upsert), batch_size):
                    count = await UserCRUD.bulk_upsert_users(db, users_to_upsert[offset : offset + batch_size])
                    await db.commit()
                    synced += count
                    # Give other ready writers an opportunity between batches.
                    await asyncio.sleep(0)

            # Offset pagination can span same-count membership changes. Validate
            # each proposed deletion against the current user-detail endpoint,
            # with no database transaction retained during these network waits.
            async with get_db() as db:
                local_usernames = await UserCRUD.get_all_usernames(db)
            potentially_deleted = sorted(local_usernames - panel_usernames)
            if pagination_changed:
                # Refresh validated observations, but do not turn gaps in a live
                # offset listing into membership changes or a deletion fan-out.
                potentially_deleted = []
                errors += 1
                sync_logger.warning("Live user listing changed; observed users refreshed, membership cleanup deferred")
            confirmation_slots = asyncio.Semaphore(10)

            async def confirm_absence(username):
                async with confirmation_slots:
                    return await _panel_confirms_user_absent(panel_data, username)

            for offset in range(0, len(potentially_deleted), batch_size):
                batch = potentially_deleted[offset : offset + batch_size]
                # A newly inserted local row belongs to a later writer. Leave it
                # for a subsequent sync even if this run's panel view is older.
                existing = [name for name in batch if name in deletion_snapshots]
                errors += len(batch) - len(existing)
                confirmed = await asyncio.gather(*(confirm_absence(name) for name in existing))
                errors += sum(not absent for absent in confirmed)
                absent_names = [name for name, absent in zip(existing, confirmed) if absent]
                if not absent_names:
                    continue

                async with get_db() as db:
                    # Serialize the comparison and delete with other writers.
                    # Full rows include policy, recovery state and panel refresh
                    # times, all of which must survive a concurrent update.
                    if db.bind.dialect.name == "sqlite":
                        await db.execute(text("BEGIN IMMEDIATE"))
                    result = await db.execute(select(User).where(User.username.in_(absent_names)).with_for_update())
                    unchanged = []
                    for user in result.scalars():
                        if tuple(getattr(user, column) for column in columns) == deletion_snapshots[user.username]:
                            unchanged.append(user.username)
                        else:
                            errors += 1
                    count = await UserCRUD.delete_many(db, unchanged)
                    await db.commit()
                deleted += count
                deleted_usernames.extend(unchanged)
                await asyncio.sleep(0)

            # Publish the cache after committed membership cleanup.
            await refresh_user_metadata_cache()

            elapsed = (datetime.now(timezone.utc) - start_time).total_seconds()
            if errors:
                sync_logger.warning(
                    f"⚠️ User sync incomplete: {synced} synced, {deleted} deleted, "
                    f"{errors} unconfirmed or changed users in {elapsed:.1f}s"
                )
            else:
                _last_sync_time = datetime.now(timezone.utc)
                sync_logger.info(
                    f"✅ User sync completed: {synced} synced, {deleted} deleted, 0 errors in {elapsed:.1f}s"
                )

            # Send Telegram notification for deleted users
            if deleted_usernames:
                await _notify_deleted_users(deleted_usernames)

        except Exception as e:
            import traceback

            errors += 1
            sync_logger.error(f"❌ User sync failed: {type(e).__name__}: {e}")
            sync_logger.error(f"Traceback: {traceback.format_exc()}")

        return (synced, errors, deleted)


async def _notify_deleted_users(usernames: list[str]) -> None:
    """Send Telegram notification for users deleted from panel."""
    try:
        from telegram_bot.send_message import send_logs

        if len(usernames) <= 10:
            user_list = "\n".join(f"• <code>{escape(u)}</code>" for u in usernames)
        else:
            user_list = "\n".join(f"• <code>{escape(u)}</code>" for u in usernames[:10])
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
            panel_data = PanelType.from_config(config)

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

        # Save to database
        from db.database import get_db
        from db.crud.users import UserCRUD

        async with get_db() as db:
            parsed = _parse_panel_user_dict(user_data)
            if not parsed:
                sync_logger.error(f"❌ Failed to parse user data for {username}")
                return None

            user = await UserCRUD.create_or_update(
                db,
                username=parsed["username"],
                panel_id=parsed["panel_id"],
                status=parsed["status"],
                owner_id=parsed["owner_id"],
                owner_username=parsed["owner_username"],
                group_ids=parsed["group_ids"],
                data_limit=parsed["data_limit"],
                used_traffic=parsed["used_traffic"],
                expire_at=parsed["expire_at"],
                note=parsed["note"],
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
    sync_logger.info("🔄 Initial user sync requested...")
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
