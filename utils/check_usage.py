"""
This module checks if a user (name and IP address)
appears more than two times in the ACTIVE_USERS list.
Enhanced with warning system and ISP detection.
"""

import asyncio
import re
import time
from collections import Counter

from telegram_bot.send_message import send_logs, send_user_message
from utils.logs import logger
from utils.panel_api import disable_user
from utils.read_config import read_config, get_config_value
from utils.types import PanelType, UserType, EnhancedUserInfo
from utils.device_count import (
    DeviceCountingConfig,
    count_devices,
    count_devices_and_details,
    count_devices_from_ips,
    group_ips_by_subnet,
)
from utils.warning_system import warning_system  # global shared instance
from utils.isp_detector import ISPDetector
from utils.ip_history_tracker import ip_history_tracker
from utils.user_group_filter import should_limit_user, get_filter_status_text
from utils.admin_filter import should_limit_user_by_admin

from utils.shared_state import (
    ACTIVE_USERS,
    ACTIVE_USERS_LOCK,
    get_active_users_snapshot,
    get_node_event_ages,
    nodes_seen_within,
    pop_active_users_snapshot,
    tracked_node_count,
)

# Re-export internal alias for backward compatibility
_active_users_lock = ACTIVE_USERS_LOCK

# Use global warning system instance imported above
# (previously a separate instance; having two caused reset button to
# clear one copy but leave the other untouched)
isp_detector = None  # Will be initialized when needed
_isp_detector_lock = asyncio.Lock()


async def _ensure_isp_detector(config_data: dict) -> ISPDetector:
    """
    Ensure the global ISPDetector singleton is initialized and updated with the latest config.
    Uses asyncio.Lock to prevent double-initialization if called concurrently.

    Args:
        config_data: Configuration dictionary containing ipinfo_token or api config.

    Returns:
        ISPDetector: The global ISPDetector instance.
    """
    global isp_detector
    async with _isp_detector_lock:
        api_config = config_data.get("api", {}) if isinstance(config_data.get("api"), dict) else {}
        ipinfo_token = config_data.get("ipinfo_token") or api_config.get("ipinfo_token", "")
        use_fallback_api = api_config.get("use_fallback_isp_api", False)

        if isp_detector is None:
            logger.info(f"Loading IPINFO_TOKEN from config: {'Present' if ipinfo_token else 'NOT FOUND'}")
            if ipinfo_token:
                logger.info(f"Token preview: {ipinfo_token[:20]}...")
            if use_fallback_api:
                logger.info("Using fallback ISP API (ip-api.com) for all requests")
            isp_detector = ISPDetector(token=ipinfo_token if ipinfo_token else None, use_fallback_only=use_fallback_api)
        elif ipinfo_token and getattr(isp_detector, "token", None) != ipinfo_token:
            isp_detector.update_token(ipinfo_token)

    return isp_detector


async def resolve_effective_limit(
    username: str,
    config: dict | None = None,
    metadata: dict | None = None,
    special_limit: dict[str, int] | None = None,
    group_limits: dict[str, int] | None = None,
) -> int:
    """
    Single source of truth for resolving a user's effective IP limit.

    Priority order:
    1. Special Limit (Direct user override in DB / special_limit dict)
    2. Pre-computed Metadata Limit (effective_ip_limit from RAM metadata)
    3. Group Limit (Batched group limit from Pasargad group)
    4. Direct Group Limit Fallback (Defense-in-depth from config & user group_ids)
    5. General Fallback Limit (Default config limit, e.g. 2)

    Args:
        username: Username to resolve limit for
        config: Full or partial configuration dictionary
        metadata: Cached metadata dictionary for the user (optional)
        special_limit: Mapping of username -> special limit override (optional)
        group_limits: Mapping of username -> group limit (optional)

    Returns:
        int: The resolved effective IP limit (>= 1)
    """
    # 1. Check direct special limit override
    if special_limit and username in special_limit:
        try:
            return int(special_limit[username])
        except (ValueError, TypeError):
            pass

    # 2. Check pre-computed metadata limit (if provided)
    if metadata and isinstance(metadata, dict):
        eff_limit = metadata.get("effective_ip_limit")
        if eff_limit is not None:
            try:
                return int(eff_limit)
            except (ValueError, TypeError):
                pass

    # 3. Check Group Limit (from pre-batched mapping)
    if group_limits and username in group_limits:
        try:
            return int(group_limits[username])
        except (ValueError, TypeError):
            pass

    # 4. Direct Group Limit Fallback (Defense-in-depth from config & user group_ids)
    cfg_group_limits = config.get("group_limits", {}) if (config and isinstance(config, dict)) else {}
    if cfg_group_limits:
        user_gids = None
        if metadata and isinstance(metadata, dict):
            user_gids = metadata.get("group_ids")
        if user_gids is None:
            try:
                from utils.user_sync import USER_METADATA_CACHE
                if username in USER_METADATA_CACHE:
                    user_gids = USER_METADATA_CACHE[username].get("group_ids")
            except Exception:
                pass

        if user_gids:
            from utils.user_sync import get_max_group_limit
            max_glim = get_max_group_limit(user_gids, cfg_group_limits)
            if max_glim is not None:
                return max_glim

    # 5. General Fallback Limit
    general_limit = 2
    if config and isinstance(config, dict):
        limits_sec = config.get("limits", {})
        if isinstance(limits_sec, dict):
            general_limit = limits_sec.get("general", 2)
        elif "general_limit" in config:
            general_limit = config.get("general_limit", 2)

    try:
        return int(general_limit)
    except (ValueError, TypeError):
        return 2


class MetadataUnavailable(RuntimeError):
    """
    The local database could not be read, so limits and monitoring flags are unknown.

    Raised only when the caller asked for a strict read. The enforcement path does,
    because the alternative - carrying on with a partial mapping - makes a database
    hiccup indistinguishable from "these users have no group limit", and the general
    fallback is stricter than most group limits.
    """


# ═══════════════════════════════════════════════════════════════════════════════
# NEW: Smart Batching for Group Limits (Performance Optimized)
# ═══════════════════════════════════════════════════════════════════════════════
async def get_group_limits_batch(
    usernames: list[str],
    config_data: dict,
    panel_data: PanelType,
    strict: bool = False,
) -> dict[str, int]:
    """
    Smart Batching: Fetches group limits for MULTIPLE users in ONE query / RAM cache.
    Handles multi-group logic by taking the maximum limit.
    """
    group_limits = config_data.get("group_limits", {})
    if not group_limits or not usernames:
        return {}

    result_limits = {}
    users_group_mapping = {}
    missing_usernames = []

    from utils.user_sync import USER_METADATA_CACHE

    # 1. Check RAM cache first
    for u in usernames:
        if u in USER_METADATA_CACHE and USER_METADATA_CACHE[u].get("group_ids") is not None:
            users_group_mapping[u] = USER_METADATA_CACHE[u].get("group_ids", [])
        else:
            missing_usernames.append(u)

    # 2. Fetch group_ids from local DB for missing usernames
    if missing_usernames:
        try:
            from db.database import get_db
            from db.models import User
            from sqlalchemy import select

            async with get_db() as db:
                chunk_size = 900
                for i in range(0, len(missing_usernames), chunk_size):
                    chunk = missing_usernames[i:i + chunk_size]
                    stmt = select(User.username, User.group_ids).where(User.username.in_(chunk))
                    result = await db.execute(stmt)

                    for row in result:
                        gids = row.group_ids or []
                        users_group_mapping[row.username] = gids
                        # Cache write-back to RAM for O(1) in future cycles.
                        # A fresh entry created here carries only group_ids, so it
                        # is flagged partial: get_active_users_metadata_batch used
                        # to treat mere presence in this cache as "fully cached"
                        # and skip the read that fills in is_monitored,
                        # is_excepted and effective_ip_limit.
                        if row.username not in USER_METADATA_CACHE:
                            USER_METADATA_CACHE[row.username] = {"_partial": True}
                        USER_METADATA_CACHE[row.username]["group_ids"] = gids
        except Exception as e:
            logger.error(f"Batch fetch group_ids failed: {e}")
            if strict:
                raise MetadataUnavailable(
                    "group limits could not be read from the local database"
                ) from e

    # 3. Process limits (and fallback to API only if user is completely missing from local DB)
    for username in usernames:
        max_limit = -1
        gids = users_group_mapping.get(username)

        # If user not found in RAM or local DB, fallback to panel API
        if gids is None:
            try:
                from utils.panel_api import get_user
                user_info = await get_user(panel_data, username)
                if user_info and isinstance(user_info, dict):
                    gids = user_info.get("group_ids", [])
                    if not gids and "group_id" in user_info and user_info["group_id"] is not None:
                        gids = [user_info["group_id"]]
                    # Cache write-back to RAM for O(1) in future cycles.
                    # Flagged partial for the same reason as the local-DB branch
                    # above: this entry holds group_ids and nothing else.
                    if username not in USER_METADATA_CACHE:
                        USER_METADATA_CACHE[username] = {"_partial": True}
                    USER_METADATA_CACHE[username]["group_ids"] = gids
            except Exception:
                gids = []

        # Calculate the max limit if user is in multiple groups
        if gids:
            from utils.user_sync import get_max_group_limit
            max_glim = get_max_group_limit(gids, group_limits)
            if max_glim is not None:
                result_limits[username] = max_glim

    return result_limits


async def get_active_users_metadata_batch(
    usernames: list[str],
    strict: bool = False,
) -> dict[str, dict]:
    """
    Fetch group_ids, owner_username, is_excepted, and special_limit for multiple users.
    FIRST checks RAM cache (USER_METADATA_CACHE). Zero database queries if cached!
    """
    if not usernames:
        return {}

    metadata = {}
    missing_usernames = []

    from utils.user_sync import USER_METADATA_CACHE

    for u in usernames:
        cached = USER_METADATA_CACHE.get(u)
        # A partial entry (group_ids only, written by get_group_limits_batch) is a
        # cache MISS here. Treating it as a hit left the user without is_monitored,
        # is_excepted or effective_ip_limit for the rest of the sync period, which
        # silently disabled their whitelist and dropped their limit to the general
        # fallback. The full read below replaces the entry wholesale.
        if cached is not None and not cached.get("_partial"):
            metadata[u] = cached
        else:
            missing_usernames.append(u)

    if not missing_usernames:
        return metadata

    try:
        from db.database import get_db
        from db.models import User
        from sqlalchemy import select

        async with get_db() as db:
            chunk_size = 900
            for i in range(0, len(missing_usernames), chunk_size):
                chunk = missing_usernames[i:i + chunk_size]
                stmt = select(
                    User.username,
                    User.group_ids,
                    User.owner_username,
                    User.is_excepted,
                    User.special_limit,
                    User.is_monitored,
                    User.effective_ip_limit
                ).where(User.username.in_(chunk))
                result = await db.execute(stmt)
                for row in result:
                    item = {
                        "group_ids": row.group_ids or [],
                        "owner_username": row.owner_username,
                        "is_excepted": bool(row.is_excepted),
                        "special_limit": row.special_limit,
                        "is_monitored": bool(row.is_monitored) if row.is_monitored is not None else True,
                        "effective_ip_limit": row.effective_ip_limit,
                    }
                    metadata[row.username] = item
                    USER_METADATA_CACHE[row.username] = item
    except Exception as e:
        logger.error(f"Batch fetch active users metadata failed: {e}")
        if strict:
            raise MetadataUnavailable(
                "user metadata could not be read from the local database"
            ) from e
    return metadata


async def check_ip_used(config_data: dict | None = None, active_users_snapshot: dict | None = None) -> dict:
    """
    Check active users and display them.
    1. Shows all active users with device count >= general_limit in ONE combined message
    2. Sends SEPARATE action messages for users who don't have special limit set
       (for setting their limit via inline buttons)
    3. Respects group filter and admin filter settings
    """
    global isp_detector

    if config_data is None:
        config_data = await read_config()
    general_limit = config_data.get("limits", {}).get("general", 2)
    except_users = config_data.get("except_users", [])  # except_users is at root level
    show_enhanced_details = config_data.get("display", {}).get("show_enhanced_details", True)
    device_config = DeviceCountingConfig.from_config(config_data)

    # Read special limits from database instead of config
    from db.database import get_db
    from db.crud import UserCRUD
    async with get_db() as db:
        special_limit = await UserCRUD.get_all_special_limits(db)

    # Get panel data for filter checks
    panel_config = config_data.get("panel", {})
    panel_data = PanelType(
        panel_config.get("username", ""),
        panel_config.get("password", ""),
        panel_config.get("domain", "")
    )

    # Initialize or update ISP detector with token from config
    isp_detector = await _ensure_isp_detector(config_data)

    if active_users_snapshot is None:
        active_users_snapshot = await get_active_users_snapshot()
    logger.info(f"📊 Processing {len(active_users_snapshot)} active users...")

    all_users_log = {}
    enhanced_users_info = {}
    filtered_users = set()  # Users filtered out by group/admin filters

    # Collect all unique IPs for batch ISP lookup
    all_ips = set()
    ip_mappings = {}
    all_actual_ips = set()

    for email in list(active_users_snapshot.keys()):
        # Skip empty usernames
        if not email or not email.strip():
            continue

        data = active_users_snapshot[email]

        # Add ALL IPs for total count
        for ip in data.ip:
            all_actual_ips.add(ip)

        # Include all unique IPs for this user
        all_unique_ips = list(set(data.ip))

        # Group IPs by subnet
        subnet_ips, ip_mapping = group_ips_by_subnet(all_unique_ips)
        all_users_log[email] = subnet_ips
        ip_mappings[email] = ip_mapping

        for ip in all_unique_ips:
            all_ips.add(ip)

    logger.info(f"📊 Collected {len(all_ips)} unique IPs from {len(all_users_log)} users")

    # Get ISP information for all IPs
    if all_ips:
        logger.info(f"🔍 Looking up ISP info for {len(all_ips)} IPs...")
        isp_info_batch = await isp_detector.get_multiple_isp_info(list(all_ips))
        logger.info(f"✅ ISP lookup complete: {len(isp_info_batch)} results")
    else:
        isp_info_batch = {}
        logger.info("📊 No IPs to look up (no active connections)")

    # Pre-filter users based on group and admin filter settings (In-Memory Batch)
    logger.debug("🔍 Applying user filters (In-Memory Batch)...")
    active_usernames_list = [u for u in active_users_snapshot.keys() if u and u.strip()]
    users_metadata = await get_active_users_metadata_batch(active_usernames_list)

    group_filter = config_data.get("group_filter", {})
    group_filter_enabled = group_filter.get("enabled", False)
    group_filter_mode = group_filter.get("mode", "include")
    group_filter_ids = [str(x) for x in group_filter.get("group_ids", [])]

    admin_filter = config_data.get("admin_filter", {})
    admin_filter_enabled = admin_filter.get("enabled", False)
    admin_filter_mode = admin_filter.get("mode", "include")
    admin_filter_names = admin_filter.get("admin_usernames", [])

    for email in active_usernames_list:
        user_meta = users_metadata.get(email, {})

        # Check group filter
        if group_filter_enabled and group_filter_ids:
            user_gids = [str(x) for x in user_meta.get("group_ids", [])]
            user_in_group = any(g in group_filter_ids for g in user_gids)
            if group_filter_mode == "include" and not user_in_group:
                filtered_users.add(email)
                continue
            elif group_filter_mode == "exclude" and user_in_group:
                filtered_users.add(email)
                continue

        # Check admin filter
        if admin_filter_enabled and admin_filter_names:
            user_admin = user_meta.get("owner_username")
            if user_admin is not None:
                user_in_admin = user_admin in admin_filter_names
                if admin_filter_mode == "include" and not user_in_admin:
                    filtered_users.add(email)
                    continue
                elif admin_filter_mode == "exclude" and user_in_admin:
                    filtered_users.add(email)
                    continue

    if filtered_users:
        logger.info(f"Filters applied: {len(filtered_users)} users excluded from monitoring")

    # Create enhanced user info with ISP details (only for non-filtered users)
    for email, formatted_ips in all_users_log.items():
        if not formatted_ips:
            continue

        # Skip empty usernames
        if not email or not email.strip():
            continue

        # Skip filtered users
        if email in filtered_users:
            continue

        ip_mapping = ip_mappings.get(email, {})
        enhanced_formatted_ips = []
        actual_ips_for_counting = []

        for formatted_ip in formatted_ips:
            actual_ips = ip_mapping.get(formatted_ip, [formatted_ip])
            actual_ips_for_counting.extend(actual_ips)

            if len(actual_ips) == 1:
                ip = actual_ips[0]
                if ip in isp_info_batch:
                    isp_info = isp_info_batch[ip]
                    enhanced_ip = isp_detector.format_ip_with_isp(ip, isp_info)
                else:
                    enhanced_ip = ip
                enhanced_formatted_ips.append(enhanced_ip)
            else:
                first_ip = actual_ips[0]
                if first_ip in isp_info_batch:
                    isp_info = isp_info_batch[first_ip]
                    enhanced_ip = f"{formatted_ip} ({isp_info['isp']}, {isp_info['country']})"
                else:
                    enhanced_ip = formatted_ip
                enhanced_formatted_ips.append(enhanced_ip)

        is_monitored = warning_system.is_user_being_monitored(email)
        time_remaining = 0
        if is_monitored and email in warning_system.warnings:
            time_remaining = warning_system.warnings[email].time_remaining()

        enhanced_users_info[email] = EnhancedUserInfo(
            user=UserType(name=email, ip=actual_ips_for_counting),
            formatted_ips=enhanced_formatted_ips,
            is_being_monitored=is_monitored,
            warning_time_remaining=time_remaining
        )

    total_ips = len(all_actual_ips)

    # Calculate device counts and pre-build IP details for all users in one pass
    all_user_device_counts = {}
    all_user_ip_details = {}
    total_devices = 0

    for email, user_info in enhanced_users_info.items():
        if not user_info.user.ip:
            all_user_device_counts[email] = 0
            all_user_ip_details[email] = []
            continue

        original_user = active_users_snapshot.get(email)

        # Get user's trust score from warning system (if available)
        user_trust_score = 0.0
        if email in warning_system.warnings:
            user_trust_score = warning_system.warnings[email].trust_score

        # Get ISP info for this user's IPs
        user_isp_info = {ip: isp_info_batch.get(ip, {}) for ip in user_info.user.ip if ip in isp_info_batch}

        ip_details, device_count = count_devices_and_details(
            user_info, original_user, show_enhanced_details,
            device_config=device_config,
            user_trust_score=user_trust_score,
            isp_info=user_isp_info,
        )
        all_user_device_counts[email] = device_count
        all_user_ip_details[email] = ip_details
        total_devices += device_count

    # Two different populations and two different measures used to share one log
    # line, which is why the report number and the enforcement number below never
    # matched. Said plainly here: `total_ips` is a global set over every active
    # user, while `total_devices` sums only the users that survived the group and
    # admin filters (see the `email in filtered_users` skip above).
    logger.info("📊 Snapshot: %s unique IPs across all active users", str(total_ips))
    logger.info(
        "📊 Report scope (group/admin filters applied): %s users, %s devices",
        str(len(enhanced_users_info)),
        str(total_devices),
    )

    # Sort users by device count (descending)
    sorted_users = sorted(
        enhanced_users_info.items(),
        key=lambda x: all_user_device_counts.get(x[0], 0),
        reverse=True
    )

    # ---- NEW: Get Group Limits in ONE Query (Batching) ----
    active_usernames_for_batch = [email for email in active_users_snapshot.keys() if email and email.strip()]
    batched_group_limits = await get_group_limits_batch(active_usernames_for_batch, config_data, panel_data)
    # -------------------------------------------------------
    # Build combined message for all users with >= general_limit devices
    combined_message_parts = []
    users_needing_limit = []  # Users without special limit who need action messages
    users_shown = 0

    for email, user_info in sorted_users:
        if not user_info.user.ip:
            continue

        original_user = active_users_snapshot.get(email)
        ip_count = len(user_info.formatted_ips)
        device_count = all_user_device_counts.get(email, 0)

        is_except = email in except_users
        has_special_limit_before = email in special_limit
        user_limit = await resolve_effective_limit(
            username=email,
            config=config_data,
            metadata=None,
            special_limit=special_limit,
            group_limits=batched_group_limits,
        )
        has_special_limit = (email in special_limit)
        has_group_limit = (not has_special_limit_before) and bool(batched_group_limits and email in batched_group_limits)

        # Skip users who are not exceeding their limit
        # A user violates when device_count > user_limit
        if device_count <= user_limit:
            continue

        users_shown += 1

        # Retrieve pre-computed IP details (avoids duplicate O(N) connection iterations & subnet calculations)
        ip_details = all_user_ip_details.get(email, [])

        # Build status indicators
        status_text = ""
        if user_info.is_being_monitored:
            minutes = user_info.warning_time_remaining // 60
            seconds = user_info.warning_time_remaining % 60
            status_text = f" ⚠️ {minutes}m{seconds}s"

        # Build limit indicator
        if is_except:
            limit_str = "🔓"
        elif has_special_limit:
            limit_str = f"🎯 {user_limit}"
        elif has_group_limit:
            limit_str = f"👥 {user_limit}"
        else:
            limit_str = f"📊 {user_limit}"
            # Add to list of users needing limit setting
            users_needing_limit.append({
                "email": email,
                "device_count": device_count,
                "ip_count": ip_count
            })

        user_header = f"👤 <b>{email}</b>{status_text}\n   📱 {device_count} 🌐 {ip_count} {limit_str}"

        # Add IP details
        if ip_details:
            ip_lines = "\n".join(f"  {detail}" for detail in ip_details)
            user_block = f"{user_header}\n{ip_lines}"
        else:
            ip_lines = "\n".join(f"  • {ip}" for ip in user_info.formatted_ips)
            user_block = f"{user_header}\n{ip_lines}"

        combined_message_parts.append(user_block)

    # Send SEPARATE action messages for users who need limit setting
    # (users without special limit and not in except list)
    if users_needing_limit:
        await asyncio.sleep(0.5)

        for user_data in users_needing_limit:
            email = user_data["email"]
            device_count = user_data["device_count"]
            ip_count = user_data["ip_count"]

            action_message = (
                f"⚙️ <b>Set Limit for: {email}</b>\n"
                f"📱 Devices: {device_count} | 🌐 IPs: {ip_count}\n"
                f"No special limit set - using general limit ({general_limit})"
            )

            try:
                # Send with inline buttons (has_special_limit=False, is_except=False)
                await send_user_message(action_message, email, device_count, False, False, general_limit)
                await asyncio.sleep(0.5)
            except Exception as e:
                logger.error(f"Failed to send action message for user {email}: {e}")

    return all_users_log


async def dispatch_chunked_warnings(
    new_warnings: list[dict],
    check_interval: float,
    total_monitored: int,
    max_warnings: int = 3
) -> None:
    """
    Dispatch new warnings aggregated into chunks of 10 to avoid rate limits and message drops.
    Includes item-level error handling and HTML escaping.

    Args:
        new_warnings: List of warning dicts collected in this scan cycle
        check_interval: Dynamic scan interval from config/ENV used as TTL
        total_monitored: Total count of users currently in monitoring
        max_warnings: Configurable max warning cycles before disable
    """
    if not new_warnings:
        return

    from telegram_bot.send_message import send_warning_log
    from datetime import datetime
    import html

    chunk_size = 10
    total_violators = len(new_warnings)
    total_chunks = (total_violators + chunk_size - 1) // chunk_size
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    logger.info(f"📨 Enqueueing {total_chunks} warning report batches ({total_violators} users) to Warnings topic")

    period_min = max(1, round(check_interval * max_warnings / 60))
    for idx in range(0, total_violators, chunk_size):
        chunk = new_warnings[idx : idx + chunk_size]
        batch_num = (idx // chunk_size) + 1

        header = (
            f"⚠️ <b>WARNINGS REPORT</b> ({batch_num}/{total_chunks}) - <code>{now_str}</code>\n"
            f"📡 Monitoring Window: <code>{max_warnings} cycles (~{period_min} min)</code>\n"
            f"📊 Violators in cycle: <code>{total_violators}</code> | In batch: <code>{len(chunk)}</code>"
        )

        user_blocks = []
        for item in chunk:
            try:
                username = html.escape(str(item.get("username", "Unknown")))
                ip_count = item.get("ip_count", 0)
                limit = item.get("limit", 1)
                trust_level = item.get("trust_level", "🟡 MEDIUM")
                trust_score = item.get("trust_score", 0.0)
                behavior = html.escape(str(item.get("behavior", "")))
                consecutive = item.get("consecutive_violations", 1)
                item_max_warnings = item.get("max_warnings", 3)

                user_line = (
                    f"👤 <code>{username}</code>\n"
                    f"   ⚠️ Warning: <code>{consecutive}/{item_max_warnings}</code> (Scan {consecutive} of {item_max_warnings})\n"
                    f"   🌐 Active Devices: <code>{ip_count}</code> (Limit: <code>{limit}</code>)\n"
                    f"   Trust Level: {trust_level} (<code>{trust_score:.0f}</code>)"
                )
                if behavior and behavior != "No specific pattern detected":
                    user_line += f"\n   Behavior: <code>{behavior}</code>"
                user_blocks.append(user_line)
            except Exception as item_err:
                logger.error(f"Error rendering warning item for batch: {item_err}")
                continue

        footer = (
            f"📈 Total users currently monitored: <code>{total_monitored}</code>\n"
            f"Users with persistent violations across warning cycles will be disabled."
        )

        batch_msg = f"{header}\n\n" + "\n\n".join(user_blocks) + f"\n\n{footer}"
        # Enqueue with safe TTL (10 minutes) so reports are not prematurely dropped
        warning_ttl = max(600.0, check_interval * 10)
        await send_warning_log(batch_msg, ttl=warning_ttl)


async def check_users_usage(panel_data: PanelType, config_data: dict | None = None):
    """
    Enhanced function to check usage with warning system and ISP detection
    """
    global isp_detector

    if config_data is None:
        config_data = await read_config()
    active_users_snapshot = await pop_active_users_snapshot()
    all_users_log = await check_ip_used(config_data=config_data, active_users_snapshot=active_users_snapshot)

    # Use new config format
    limits_config = config_data.get("limits", {})
    api_config = config_data.get("api", {})

    except_users = config_data.get("except_users", [])  # except_users is at root level
    limit_number = limits_config.get("general", 2)

    # Read special limits from database instead of config
    from db.database import get_db
    from db.crud import UserCRUD
    async with get_db() as db:
        special_limit = await UserCRUD.get_all_special_limits(db)

    # Initialize or update ISP detector
    isp_detector = await _ensure_isp_detector(config_data)

    # Sync dynamic check interval and max warning count to warning system
    check_interval = config_data.get("check_interval", 60)
    max_warning_count = config_data.get("max_warning_count", 3)
    warning_system.update_settings(check_interval, max_warning_count)

    logger.info("📊 Building user info from active connections...")

    # Build user info with actual unique IP counts for ALL active users
    # This is critical for warning system to work correctly
    all_users_actual_ips = {}  # Maps username to set of actual unique IPs
    all_users_data = {}  # Maps username to UserType with full data
    all_ips_for_isp_lookup = set()  # Collect all IPs for batch ISP lookup

    for email in list(active_users_snapshot.keys()):
        data = active_users_snapshot[email]
        # Get ALL unique IPs for this user (not just filtered ones)
        unique_ips = set(data.ip)
        all_users_actual_ips[email] = unique_ips
        all_users_data[email] = data

        # Add to ISP lookup set
        all_ips_for_isp_lookup.update(unique_ips)

    logger.info(f"📊 Found {len(all_users_actual_ips)} active users with {len(all_ips_for_isp_lookup)} unique IPs")

    # Batch fetch ISP info for all IPs
    logger.info(f"🔍 Looking up ISP info for {len(all_ips_for_isp_lookup)} IPs...")
    isp_info_batch = await isp_detector.get_multiple_isp_info(list(all_ips_for_isp_lookup))
    logger.info(f"✅ ISP lookup complete")

    # Record IPs to history for the long-term reports (one statement per cycle)
    logger.debug("📝 Recording IPs to history tracker...")
    await ip_history_tracker.record_many(all_users_actual_ips)

    # Batch fetch group limits & metadata for active users.
    #
    # Strict here on purpose. A partial read used to be indistinguishable from
    # "this user has no group limit", which resolve_effective_limit turns into the
    # general limit - and that is stricter than most group limits, so a single
    # database hiccup warned and then banned users who were inside their real
    # limit. Skipping the cycle costs nothing: no counter is touched, and the next
    # cycle re-evaluates everyone from scratch.
    active_usernames = list(all_users_actual_ips.keys())
    try:
        batched_group_limits = await get_group_limits_batch(
            active_usernames, config_data, panel_data, strict=True
        )
        users_metadata_usage = await get_active_users_metadata_batch(
            active_usernames, strict=True
        )
    except MetadataUnavailable as error:
        logger.error(
            f"⛔ Enforcement skipped this cycle: {error}. Nobody is warned, banned "
            f"or cleared while limits and monitoring flags are unknown."
        )
        all_users_log.clear()
        return

    # ------------------------------------------------------------------
    # Single source of truth for the device count of this cycle.
    # Warning, ban and clearing decisions MUST all use the same number,
    # otherwise the clearing path can silently undo a warning that the
    # violation path just issued (or vice versa).
    # ------------------------------------------------------------------
    device_config = DeviceCountingConfig.from_config(config_data)
    all_users_device_counts: dict[str, int] = {}
    for _u_name, _u_ips in all_users_actual_ips.items():
        _u_data = all_users_data.get(_u_name)
        _w_curr = warning_system.warnings.get(_u_name)
        _u_isp = {ip: isp_info_batch.get(ip, {}) for ip in _u_ips if ip in isp_info_batch}
        all_users_device_counts[_u_name] = count_devices(
            _u_data,
            device_config,
            trust_score=_w_curr.trust_score if _w_curr else 0.0,
            isp_info=_u_isp,
            # No connection detail for this user, so the addresses are all there
            # is. This used to pass len(_u_ips) - the raw IP count - which ignores
            # subnet grouping and would read a single /24 as N separate devices,
            # exactly the measure that caused the earlier wave of false bans.
            fallback_count=count_devices_from_ips(_u_ips, device_config, _u_isp),
        )

    total_devices_cycle = sum(all_users_device_counts.values())
    total_ips_cycle = sum(len(v) for v in all_users_actual_ips.values())
    # Deliberately spelled out, because this number is larger than the report's and
    # that used to look like a bug: this covers EVERY active user (filters are
    # applied per user further down, not here), and the IP figure is the sum of
    # per-user counts, so an IP shared by two users is counted twice - unlike the
    # report's global set.
    logger.info(
        f"📊 Enforcement scope (all active users), counting mode "
        f"'{device_config.count_mode}': {total_devices_cycle} devices from "
        f"{total_ips_cycle} per-user IPs (summed per user, not de-duplicated)"
    )

    # Check for users who still violate limits after warning period
    # Pass actual IPs, group limits and the cycle's unified device counts
    disabled_users, warned_users = await warning_system.check_persistent_violations(
        panel_data, all_users_actual_ips, config_data, batched_group_limits,
        device_counts=all_users_device_counts
    )

    # Combine disabled and warned users to skip them in the loop
    processed_users = disabled_users | warned_users

    group_filter_data = config_data.get("group_filter", {})
    group_filter_enabled_u = group_filter_data.get("enabled", False)
    group_filter_mode_u = group_filter_data.get("mode", "include")
    group_filter_ids_u = [str(x) for x in group_filter_data.get("group_ids", [])]

    admin_filter_data = config_data.get("admin_filter", {})
    admin_filter_enabled_u = admin_filter_data.get("enabled", False)
    admin_filter_mode_u = admin_filter_data.get("mode", "include")
    admin_filter_names_u = admin_filter_data.get("admin_usernames", [])
    # -------------------------------------------------------
    # Check current violations for ALL users (not just those in all_users_log)
    # Track users skipped due to group filter or admin filter
    group_filtered_users = set()
    admin_filtered_users = set()
    unknown_metadata_users = set()
    failed_users = set()
    cycle_new_warnings = []

    for user_name, unique_ips in all_users_actual_ips.items():
        # Inverted from `if not except and not processed:` to an early continue so
        # that the `try` below sits at the indentation the old `if` had. The body is
        # unchanged and un-reindented on purpose: a single exception in here used to
        # abandon the whole cycle for every remaining user, and hand-reindenting 130
        # lines to fix that is how silent bugs get made.
        if user_name in except_users or user_name in processed_users:
            continue

        try:
            if user_name not in users_metadata_usage:
                # No local row for this user, so neither their monitoring flag nor
                # their limit is known. Enforcing on defaults would judge somebody
                # the operator never asked us to watch against the general limit;
                # the unknown-user worker syncs them within a cycle or two.
                unknown_metadata_users.add(user_name)
                continue

            user_meta = users_metadata_usage[user_name]

            # Check group filter (In-Memory)
            # Check pre-computed group_filter status (O(1) RAM lookup)
            if not user_meta.get("is_monitored", True):
                group_filtered_users.add(user_name)
                continue

            # Check admin filter (In-Memory)
            if admin_filter_enabled_u and admin_filter_names_u:
                user_admin = user_meta.get("owner_username")
                if user_admin is not None:
                    user_in_admin = user_admin in admin_filter_names_u
                    should_limit_admin = user_in_admin if admin_filter_mode_u == "include" else not user_in_admin
                    if not should_limit_admin:
                        admin_filtered_users.add(user_name)
                        continue

            # Resolve effective IP limit using single source of truth
            user_limit_number = await resolve_effective_limit(
                username=user_name,
                config=config_data,
                metadata=user_meta,
                special_limit=special_limit,
                group_limits=batched_group_limits,
            )

            # Get user data and ISP info for this user
            user_data = all_users_data.get(user_name)
            user_isp_info = {ip: isp_info_batch.get(ip, {}) for ip in unique_ips if ip in isp_info_batch}

            # Effective device count comes from the cycle-wide precompute so
            # that warn / ban / clear all agree on the same number.
            effective_device_count = all_users_device_counts.get(user_name, len(unique_ips))

            if effective_device_count > user_limit_number:
                # A record may exist but be expired; treat any existing record as
                # "already monitored" so that its consecutive counter keeps growing
                # and a reached limit can never be silently dropped.
                was_monitored = user_name in warning_system.warnings

                result = await warning_system.add_warning(
                    user_name, effective_device_count, unique_ips, user_limit_number,
                    user_data=user_data, isp_info=user_isp_info, panel_data=panel_data,
                    send_telegram_notification=False
                )

                if was_monitored:
                    logger.info(
                        f"Updated monitoring for user {user_name} with {effective_device_count} devices "
                        f"({len(unique_ips)} IPs, limit={user_limit_number}) (result={result})"
                    )

                if result == "violation_limit_reached":
                    from utils.warning_system import safe_disable_user_with_punishment
                    from telegram_bot.send_message import send_disable_notification
                    from datetime import datetime

                    punishment_result = await safe_disable_user_with_punishment(
                        panel_data, UserType(name=user_name, ip=[])
                    )
                    disabled_users.add(user_name)
                    time_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

                    duration_text = ""
                    if punishment_result.get("duration_minutes", 0) > 0:
                        duration_text = f"Duration: <code>{punishment_result['duration_minutes']} minutes</code>\n"
                    else:
                        duration_text = "Duration: <code>Until manual enable</code>\n"

                    w_obj = warning_system.warnings.get(user_name)
                    trust_score = w_obj.trust_score if w_obj else 0.0
                    trust_level = w_obj.get_trust_level() if w_obj else "🟡 MEDIUM"
                    activity_summary = w_obj.get_ip_activity_summary() if w_obj else ""

                    if punishment_result.get("action") == "revoked":
                        revoke_note = "✅ Sub revoked" if punishment_result.get("revoke_success", False) else "⚠️ Revoke failed"
                        uuid_note = "✅ UUID reset" if punishment_result.get("uuid_reset_success", False) else "⚠️ UUID failed"
                        msg = (
                            f"🔄 <b>SUBSCRIPTION REVOKED + DISABLED</b> - {time_str}\n\n"
                            f"User: <code>{user_name}</code>\n"
                            f"Active Devices: <code>{effective_device_count}</code> ({len(unique_ips)} IPs)\n"
                            f"User limit: <code>{user_limit_number}</code>\n"
                            f"Trust Level: {trust_level} (<code>{trust_score:.0f}</code>)\n\n"
                            f"📊 Violation #{punishment_result.get('violation_count', 1)} (Step {punishment_result.get('step_index', 0) + 1})\n"
                            f"{revoke_note}, {uuid_note}\n"
                            f"Duration: <code>Until manual enable</code>\n"
                            f"📊 IP Activity:\n<code>{activity_summary}</code>"
                        )
                    else:
                        msg = (
                            f"🚫 <b>USER DISABLED</b> - {time_str}\n\n"
                            f"User: <code>{user_name}</code>\n"
                            f"Active Devices: <code>{effective_device_count}</code> ({len(unique_ips)} IPs)\n"
                            f"User limit: <code>{user_limit_number}</code>\n"
                            f"Trust Level: {trust_level} (<code>{trust_score:.0f}</code>)\n\n"
                            f"📊 Violation #{punishment_result.get('violation_count', 1)} (Step {punishment_result.get('step_index', 0) + 1})\n"
                            f"{duration_text}"
                            f"📊 IP Activity:\n<code>{activity_summary}</code>"
                        )
                    await send_disable_notification(msg, user_name)
                    await warning_system.clear_user_trust_data(user_name)
                    logger.warning(f"🚫 Disabled user {user_name} after {max_warning_count} consecutive violation scans (limit: {user_limit_number})")
                elif result == "instant_disabled":
                    disabled_users.add(user_name)
                    logger.warning(f"User {user_name} instantly disabled due to low trust score")
                elif result in ("new", "updated"):
                    w_obj = warning_system.warnings.get(user_name)
                    trust_score = w_obj.trust_score if w_obj else 0.0
                    trust_level = w_obj.get_trust_level() if w_obj else "🟡 MEDIUM"
                    behavior = w_obj.get_behavior_summary() if w_obj else ""
                    default_consecutive = 2 if result == "updated" else 1
                    consecutive = getattr(w_obj, "consecutive_violations", default_consecutive) if w_obj else default_consecutive
                    cycle_new_warnings.append({
                        "username": user_name,
                        "ip_count": effective_device_count,
                        "limit": user_limit_number,
                        "trust_score": trust_score,
                        "trust_level": trust_level,
                        "behavior": behavior,
                        "consecutive_violations": consecutive,
                        "max_warnings": max_warning_count,
                    })
                    if result == "new":
                        logger.warning(
                            f"User {user_name} has {effective_device_count} devices "
                            f"({len(unique_ips)} active ips, limit: {user_limit_number}). "
                            f"Warning issued - monitoring for {max_warning_count} consecutive scans."
                        )
                else:
                    logger.warning(
                        f"Unhandled warning result '{result}' for user {user_name} "
                        f"({effective_device_count} devices, limit={user_limit_number})"
                    )
        except Exception as user_error:  # pylint: disable=broad-except
            # One malformed user must not cost every user after them in the dict
            # their evaluation. The counter is deliberately left untouched: it is
            # the record of how many consecutive scans they have violated, and
            # resetting it here would restart their escalation from zero.
            failed_users.add(user_name)
            logger.error(
                f"❌ Enforcement failed for {user_name}: {user_error}. Their counter is "
                f"left as it was and the cycle continues with the next user.",
                exc_info=True,
            )
            continue

    # Check for users whose usage normalized (active devices <= limit or disconnected)
    #
    # An empty sample while users are under monitoring is not 2,350 people
    # disconnecting at once - it is the log pipeline having stopped. Clearing on that
    # evidence hands every real offender a fresh start each cycle, which is how a
    # flaky stream stops bans altogether.
    #
    # Two separate signals, because they catch different failures:
    #
    #  * an empty sample catches "everything is down";
    #  * the per-node heartbeat catches *partial* failure, which an empty sample
    #    cannot see. get_logs opens its client with timeout=None, so a half-open
    #    stream never raises and the node keeps reporting "✅ Connected" while
    #    delivering nothing. One dead node out of forty-nine still leaves plenty of
    #    active users in the sample - and every user who was on that node looks
    #    like they disconnected.
    #
    # The staleness window is generous on purpose. A node with no traffic at all in
    # two whole check intervals is not merely quiet on this installation, and
    # erring long means a genuinely idle node never blocks enforcement.
    counters_are_trustworthy = bool(all_users_actual_ips) or not warning_system.warnings
    if not counters_are_trustworthy:
        logger.error(
            f"⛔ No active users this cycle while {len(warning_system.warnings)} are under "
            f"monitoring - treating the sample as unusable and leaving every counter alone"
        )

    tracked_nodes = tracked_node_count()
    if counters_are_trustworthy and tracked_nodes and warning_system.warnings:
        stale_window = max(120.0, float(check_interval) * 2)
        live_nodes = nodes_seen_within(stale_window)
        if live_nodes < tracked_nodes:
            silent_nodes = sorted(
                node_id
                for node_id, age in get_node_event_ages().items()
                if age > stale_window
            )
            # Under-counting is the failure direction here, so the safe response is
            # to leave every counter alone rather than to clear on a partial view.
            counters_are_trustworthy = False
            logger.error(
                f"⛔ {len(silent_nodes)} of {tracked_nodes} log streams produced nothing in "
                f"the last {int(stale_window)}s (node ids {silent_nodes}) while "
                f"{len(warning_system.warnings)} users are under monitoring - the sample is "
                f"incomplete, so no counter is cleared this cycle"
            )

    for monitored_user in list(warning_system.warnings.keys()):
        if not counters_are_trustworthy:
            break
        if monitored_user in failed_users:
            # Their evaluation raised an exception this cycle, so nothing about
            # them is known well enough to justify clearing their record.
            continue
        if monitored_user not in all_users_actual_ips:
            await warning_system.clear_user_trust_data(monitored_user)
            logger.info(f"✅ User {monitored_user} inactive, monitoring cleared")
        else:
            user_current_ips = all_users_actual_ips.get(monitored_user, set())
            if monitored_user not in users_metadata_usage:
                # Their limit is unknown, so "within limit" cannot be established.
                # Clearing on a guess would hand a real offender a fresh start
                # every cycle, so the record is left exactly as it is.
                continue
            user_meta = users_metadata_usage[monitored_user]
            u_lim = await resolve_effective_limit(
                username=monitored_user,
                config=config_data,
                metadata=user_meta,
                special_limit=special_limit,
                group_limits=batched_group_limits,
            )
            # Use the same unified device count the warning path used, so a
            # warning issued this cycle cannot be cleared in the same cycle.
            user_device_count = all_users_device_counts.get(monitored_user, len(user_current_ips))
            if user_device_count <= u_lim:
                await warning_system.clear_user_trust_data(monitored_user)
                logger.info(
                    f"✅ User {monitored_user} normalized usage "
                    f"({user_device_count} devices / {len(user_current_ips)} IPs <= {u_lim}), monitoring cleared"
                )

    # Log group filter stats if any users were filtered
    if group_filtered_users:
        logger.debug(f"Group filter: {len(group_filtered_users)} users skipped")

    # Log admin filter stats if any users were filtered
    if admin_filtered_users:
        logger.debug(f"Admin filter: {len(admin_filtered_users)} users skipped")

    # Users the local database knows nothing about yet. Visible on purpose: a count
    # that keeps growing means user_sync is not keeping up, and every one of these
    # users is currently unenforced.
    if unknown_metadata_users:
        logger.warning(
            f"⏭️ {len(unknown_metadata_users)} active users skipped - not synced to the "
            f"local database yet, so their limit is unknown"
        )

    # Kept at ERROR: a non-zero count here means some users were not enforced this
    # cycle for a reason that is a defect, not a policy.
    if failed_users:
        logger.error(
            f"❌ {len(failed_users)} users could not be evaluated this cycle "
            f"({', '.join(sorted(failed_users)[:10])}"
            f"{'...' if len(failed_users) > 10 else ''}) - see the tracebacks above"
        )

    # Dispatch chunked warning reports in batches of 10
    check_interval = float(config_data.get("check_interval") or config_data.get("monitoring", {}).get("check_interval", 60)) if config_data else 60.0
    total_monitored = len(warning_system.get_monitoring_users())
    if cycle_new_warnings:
        await dispatch_chunked_warnings(cycle_new_warnings, check_interval, total_monitored, max_warnings=int(max_warning_count))

    # Clean up expired warnings
    await warning_system.cleanup_expired_warnings()

    all_users_log.clear()


async def run_check_users_usage(panel_data: PanelType) -> None:
    """run check_ip_used() function and then run check_users_usage()"""
    while True:
        cycle_start = time.monotonic()
        config_data = await read_config()

        # In API mode the connected IPs are collected right here, immediately
        # before enforcement, instead of being streamed in continuously by the
        # SSE log tasks. When the collector reports an untrustworthy sample the
        # cycle is skipped entirely: feeding partial data to check_users_usage
        # would clear the consecutive-violation counters of real offenders.
        run_enforcement = True
        if str(config_data.get("ip_source") or "logs") == "api":
            from utils.ip_source_api import collect_active_users_from_api

            try:
                run_enforcement = await collect_active_users_from_api(
                    panel_data, config_data
                )
            except Exception as error:  # pylint: disable=broad-except
                logger.error(f"🛰️ API IP collection failed: {error}")
                run_enforcement = False

        if run_enforcement:
            # One cycle must not be able to end the process. check_users_usage is
            # awaited directly inside limiter.py's TaskGroup, so anything raising
            # here - one malformed user, one Telegram error, one DB hiccup - used to
            # abort the whole group and exit non-zero for a supervisor restart. The
            # restart then ran a fresh cycle, and because consecutive_violations
            # counts add_warning calls rather than elapsed time, a crash loop could
            # walk a user from first sighting to ban far faster than
            # check_interval * max_warnings.
            try:
                await check_users_usage(panel_data, config_data=config_data)
            except Exception as error:  # pylint: disable=broad-except
                logger.error(
                    f"❌ Enforcement cycle failed and was abandoned: {error}. "
                    f"Counters keep their values; the next cycle re-evaluates everyone.",
                    exc_info=True,
                )

        check_interval = int(
            config_data.get("check_interval")
            or config_data.get("monitoring", {}).get("check_interval", 60)
        )

        # Sleep relative to the START of the cycle so that the real period stays
        # equal to check_interval. Otherwise collection time (which can be
        # minutes in API mode) is added on top of the interval and the monitoring
        # window expires before the required consecutive scans can complete.
        elapsed = time.monotonic() - cycle_start
        if elapsed > check_interval:
            logger.warning(
                f"⏱️ Cycle took {elapsed:.1f}s, longer than check_interval={check_interval}s. "
                f"Consecutive-scan timing is degraded - lower the API fan-out cost or raise "
                f"the interval."
            )
        await asyncio.sleep(max(5.0, check_interval - elapsed))
