"""
This module checks if a user (name and IP address)
appears more than two times in the ACTIVE_USERS list.
Enhanced with warning monitoring and device counting.
"""

import asyncio
import html
import re
import time
from collections.abc import Mapping

from telegram_bot.send_message import send_logs, send_user_message
from utils.logs import logger
from utils.read_config import read_config, read_config_snapshot, get_config_revision, get_config_value
from utils.types import PanelType, UserType, EnhancedUserInfo
from utils.device_count import (
    COUNT_MODE_INBOUND,
    COUNT_MODE_NODE,
    DeviceCountingConfig,
    count_devices,
    count_devices_and_details,
    count_devices_from_ips,
    group_ips_by_subnet,
)
from utils.log_observations import LOG_OBSERVATIONS
from utils.monitoring_gate import (
    get_last_completed_scan,
    monitoring_is_current,
    monitoring_snapshot,
    record_completed_scan,
    record_cycle_outcome,
    synchronize_monitoring,
    wait_for_monitoring_change,
)
from utils.warning_system import warning_system  # global shared instance
from utils.warning_system.helpers import capture_monitoring_guard
from utils.enforcement_guard import enforcement_guard
from utils.isp_detector import ISPDetector
from utils.ip_history_tracker import ip_history_tracker
from utils.user_policy import (
    DEFAULT_GENERAL_LIMIT,
    MIN_EFFECTIVE_LIMIT,
    UserPolicy,
    _calculate_user_limit,
    evaluate_user_policy,
)
from utils.user_group_filter import should_limit_user, get_filter_status_text

from utils.shared_state import (
    ACTIVE_USERS,
    ACTIVE_USERS_LOCK,
    get_active_users_snapshot,
    get_node_event_ages,
    merge_active_users_snapshot,
    node_silence_window,
    nodes_seen_within,
    pop_active_users_snapshot,
    pop_api_users_snapshot,
    api_evidence_is_current,
    refresh_api_user,
    refresh_log_user,
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
            if use_fallback_api:
                logger.info("Using fallback ISP API (ip-api.com) for all requests")
            isp_detector = ISPDetector(token=ipinfo_token if ipinfo_token else None, use_fallback_only=use_fallback_api)
        else:
            # Reconfigure on any change - including a token that was removed, which the
            # old truthy-only update ignored, leaving the old secret live until restart.
            desired = (ipinfo_token or None, bool(use_fallback_api))
            if isp_detector.config_signature != desired:
                await isp_detector.reconfigure(token=ipinfo_token or None, fallback=use_fallback_api)

    return isp_detector


async def resolve_effective_limit(
    username: str,
    config: dict | None = None,
    metadata: dict | None = None,
    special_limit: dict[str, int] | None = None,
    group_limits: dict[str, int] | None = None,
) -> int:
    """Resolve current special > group > general limits for legacy report callers.

    Optional mappings contain current batched overrides. Derived metadata fields,
    including effective_ip_limit and special_limit, never supply policy authority.
    """
    facts = metadata
    if facts is None:
        from utils.user_sync import USER_METADATA_CACHE

        facts = USER_METADATA_CACHE.get(username)
    limit, _source = _calculate_user_limit(
        username=username,
        config=config or {},
        facts=facts,
        special_limits=special_limit,
        batched_group_limits=group_limits,
    )
    return limit


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
                    chunk = missing_usernames[i : i + chunk_size]
                    stmt = select(User.username, User.group_ids).where(User.username.in_(chunk))
                    result = await db.execute(stmt)

                    for row in result:
                        gids = row.group_ids or []
                        users_group_mapping[row.username] = gids
                        # Cache write-back to RAM for O(1) in future cycles.
                        # A fresh entry created here carries only group_ids, so it
                        # is flagged partial: get_active_users_metadata_batch used
                        # to treat mere presence in this cache as "fully cached"
                        # and skip the read that fills in owner_username.
                        if row.username not in USER_METADATA_CACHE:
                            USER_METADATA_CACHE[row.username] = {"_partial": True}
                        USER_METADATA_CACHE[row.username]["group_ids"] = gids
        except Exception as e:
            logger.error(f"Batch fetch group_ids failed: {e}")
            if strict:
                raise MetadataUnavailable("group limits could not be read from the local database") from e

    # 3. Process limits (and fallback to API only if user is completely missing from local DB)
    for username in usernames:
        max_limit = -1
        gids = users_group_mapping.get(username)

        # If user not found in RAM or local DB, fallback to panel API
        if gids is None:
            try:
                # get_user_details, not get_user: panel_api has never exported a
                # `get_user`, so this import raised ImportError every time, the bare
                # `except Exception` below turned it into `gids = []`, and the panel
                # fallback advertised here has in fact never run once.
                from utils.panel_api import get_user_details

                user_info = await get_user_details(panel_data, username)
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
            except Exception as lookup_error:
                # Say so rather than swallowing it. An empty list here means the
                # user gets no group limit, and check_users_usage then skips them
                # entirely - safe, but invisible unless it is logged.
                logger.warning(
                    f"Group lookup from the panel failed for {username}: {lookup_error}. "
                    f"They will be skipped this cycle rather than judged on a default."
                )
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
    Fetch factual group_ids and owner_username for multiple users.
    FIRST checks RAM cache (USER_METADATA_CACHE). Zero database queries if cached!
    """
    if not usernames:
        return {}

    metadata = {}
    missing_usernames = []

    from utils.user_sync import USER_METADATA_CACHE

    for u in usernames:
        cached = USER_METADATA_CACHE.get(u)
        # Group-only cache entries do not establish the current owner. Complete the
        # factual entry before report or policy callers consume it.
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
                chunk = missing_usernames[i : i + chunk_size]
                stmt = select(
                    User.username,
                    User.group_ids,
                    User.owner_username,
                ).where(User.username.in_(chunk))
                result = await db.execute(stmt)
                for row in result:
                    item = {
                        "group_ids": row.group_ids or [],
                        "owner_username": row.owner_username,
                    }
                    metadata[row.username] = item
                    USER_METADATA_CACHE[row.username] = item
    except Exception as e:
        logger.error(f"Batch fetch active users metadata failed: {e}")
        if strict:
            raise MetadataUnavailable("user metadata could not be read from the local database") from e
    return metadata


async def check_ip_used(
    config_data: dict | None = None,
    active_users_snapshot: dict | None = None,
    *,
    user_policies: Mapping[str, UserPolicy] | None = None,
) -> dict:
    """
    Check active users and display them.
    1. Shows all active users with device count >= general_limit in ONE combined message
    2. Sends SEPARATE action messages for users who don't have special limit set
       (for setting their limit via inline buttons)
    3. Respects group filter and admin filter settings
    """
    admission = monitoring_snapshot()
    if not monitoring_is_current(admission):
        return {}
    await synchronize_monitoring()
    if not monitoring_is_current(admission):
        return {}

    if config_data is None:
        config_data = await read_config()
    if not monitoring_is_current(admission):
        return {}
    general_limit = config_data.get("limits", {}).get("general", 2)
    # read_config exposes this as a root-level bool named "enhanced_details" (the key the
    # Telegram toggle writes). This used to read config["display"]["show_enhanced_details"],
    # a path read_config never creates, so it always fell back to True and the toggle did
    # nothing. It only decides whether the per-IP detail lines are rendered - the device
    # count itself is computed before the flag is consulted - so no ban outcome changes.
    show_enhanced_details = bool(config_data.get("enhanced_details", True))
    if active_users_snapshot is None:
        active_users_snapshot = await get_active_users_snapshot()
    evidence = getattr(active_users_snapshot, "api_evidence", None) or getattr(
        active_users_snapshot, "log_evidence", None
    )
    eligible_node_ids = getattr(evidence, "eligible_node_ids", None)
    if config_data.get("device_count_mode") in (COUNT_MODE_NODE, COUNT_MODE_INBOUND):
        if config_data.get("device_count_mode") == COUNT_MODE_NODE and eligible_node_ids is None:
            return {}
        if config_data.get("ip_source") == "api":
            if config_data.get("device_count_mode") == COUNT_MODE_INBOUND:
                return {}
            if not getattr(active_users_snapshot, "api_evidence", None) or not api_evidence_is_current(evidence):
                return {}
            # Keep unknown observations in the enforcement-owned mapping so its
            # later freshness check can protect their warning records.
            active_users_snapshot = dict(active_users_snapshot)
            for username, user in list(active_users_snapshot.items()):
                if username in evidence.unknown_users or not refresh_api_user(user, evidence.freshness_seconds):
                    del active_users_snapshot[username]
        elif not LOG_OBSERVATIONS.is_current(evidence, allow_partial=True):
            return {}

    log_evidence = getattr(active_users_snapshot, "log_evidence", None)
    if log_evidence is not None:
        # A report can await configuration or Telegram before enforcement resumes.
        # Keep its visible population separate while aging the original user data.
        active_users_snapshot = dict(active_users_snapshot)

    def refresh_report_evidence() -> bool:
        if log_evidence is None:
            return True
        if not LOG_OBSERVATIONS.is_current(
            log_evidence, allow_partial=config_data.get("device_count_mode") in (COUNT_MODE_NODE, COUNT_MODE_INBOUND)
        ):
            return False
        for username, user in list(active_users_snapshot.items()):
            if not refresh_log_user(user, log_evidence) or not user.ip:
                del active_users_snapshot[username]
        return True

    if not refresh_report_evidence():
        return {}
    device_config = DeviceCountingConfig.from_config(config_data, eligible_node_ids=eligible_node_ids)
    if user_policies is None:
        snapshot = await read_config_snapshot(usernames=active_users_snapshot)
        if snapshot.config.get("config_degraded"):
            return {}
        user_policies = {
            name: evaluate_user_policy(username=name, config=snapshot.config, facts=snapshot.user_facts.get(name))
            for name in active_users_snapshot
        }
    if not monitoring_is_current(admission) or not refresh_report_evidence():
        return {}
    logger.info(f"📊 Processing {len(active_users_snapshot)} active users...")

    all_users_log = {}
    enhanced_users_info = {}
    filtered_users = set()  # Users filtered out by group/admin filters

    # Keep display grouping separate from the raw addresses used for counting.
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

    # Pre-filter users based on group and admin filter settings (In-Memory Batch)
    logger.debug("🔍 Applying user filters (In-Memory Batch)...")
    active_usernames_list = [u for u in active_users_snapshot.keys() if u and u.strip()]
    for email in active_usernames_list:
        policy = user_policies.get(email)
        if policy is None or not policy.known or (not policy.eligible and policy.reason != "whitelisted"):
            filtered_users.add(email)

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
        actual_ips_for_counting = []

        for formatted_ip in formatted_ips:
            actual_ips_for_counting.extend(ip_mapping.get(formatted_ip, [formatted_ip]))

        is_monitored = warning_system.is_user_being_monitored(email)
        time_remaining = 0
        if is_monitored and email in warning_system.warnings:
            time_remaining = warning_system.warnings[email].time_remaining()

        enhanced_users_info[email] = EnhancedUserInfo(
            user=UserType(name=email, ip=actual_ips_for_counting),
            formatted_ips=formatted_ips,
            is_being_monitored=is_monitored,
            warning_time_remaining=time_remaining,
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

        ip_details, device_count = count_devices_and_details(
            user_info,
            original_user,
            show_enhanced_details,
            device_config=device_config,
            user_trust_score=user_trust_score,
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
    sorted_users = sorted(enhanced_users_info.items(), key=lambda x: all_user_device_counts.get(x[0], 0), reverse=True)

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

        policy = user_policies[email]
        is_except = policy.reason == "whitelisted"
        user_limit = policy.limit
        has_special_limit = policy.limit_source == "special"
        has_group_limit = policy.limit_source == "group"

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
            users_needing_limit.append({"email": email, "device_count": device_count, "ip_count": ip_count})

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
            if not monitoring_is_current(admission):
                return {}
            email = user_data["email"]
            device_count = user_data["device_count"]
            ip_count = user_data["ip_count"]

            action_message = (
                f"⚙️ <b>Set Limit for: {html.escape(email)}</b>\n"
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
    new_warnings: list[dict], check_interval: float, total_monitored: int, max_warnings: int = 3
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
    admission = monitoring_snapshot()
    if not new_warnings or not monitoring_is_current(admission):
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
        if not monitoring_is_current(admission):
            return
        chunk = new_warnings[idx : idx + chunk_size]
        batch_num = (idx // chunk_size) + 1

        header = (
            f"⚠️ <b>WARNINGS REPORT</b> ({batch_num}/{total_chunks}) - <code>{now_str}</code>\n"
            f"📡 Monitoring Window (current setting): <code>{max_warnings} cycles (~{period_min} min)</code>\n"
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


def _sample_is_trustworthy(all_users_actual_ips: dict, check_interval: int) -> bool:
    """
    Whether this cycle's sample is complete enough to act on monitoring records.

    An empty sample while users are under monitoring is not 2,350 people
    disconnecting at once - it is the log pipeline having stopped. Acting on that
    evidence hands every real offender a fresh start each cycle, which is how a
    flaky stream stops bans altogether.

    Two separate signals, because they catch different failures:

      * an empty sample catches "everything is down";
      * the per-node heartbeat catches *partial* failure, which an empty sample
        cannot see. get_logs opens its client with read timeout None, so a half-open
        stream never raises and the node keeps reporting "✅ Connected" while
        delivering nothing. One dead node out of forty-nine still leaves plenty of
        active users in the sample - and every user who was on that node looks like
        they disconnected.

    The staleness window is three check intervals (see node_silence_window). Every
    line the panel sends counts as a heartbeat, keep-alives included, so a node with
    no user traffic at all still reports in and a genuinely idle node never blocks
    enforcement. Silence for three whole intervals is a broken stream, and the node
    poller in get_logs reconnects on the same window.

    Under-counting is the failure direction here, so the safe answer when in doubt
    is False: leave every counter and every record exactly as it is.
    """
    if not all_users_actual_ips and warning_system.warnings:
        logger.error(
            f"⛔ No active users this cycle while {len(warning_system.warnings)} are under "
            f"monitoring - treating the sample as unusable and leaving every counter alone"
        )
        return False

    tracked_nodes = tracked_node_count()
    if tracked_nodes and warning_system.warnings:
        stale_window = node_silence_window(check_interval)
        live_nodes = nodes_seen_within(stale_window)
        if live_nodes < tracked_nodes:
            silent_nodes = sorted(node_id for node_id, age in get_node_event_ages().items() if age > stale_window)
            logger.error(
                f"⛔ {len(silent_nodes)} of {tracked_nodes} log streams produced nothing in "
                f"the last {int(stale_window)}s (node ids {silent_nodes}) while "
                f"{len(warning_system.warnings)} users are under monitoring - the sample is "
                f"incomplete, so no counter is cleared and no record is judged this cycle"
            )
            return False

    return True


async def check_users_usage(panel_data: PanelType, config_data: dict | None = None):
    """
    Check device usage and monitor consecutive violations before applying punishment.

    Return the initial Logs wait reason when source evidence blocks the scan.
    """
    global isp_detector

    policy_monitoring_is_current = capture_monitoring_guard()
    if not policy_monitoring_is_current():
        return
    admission = monitoring_snapshot()
    if not monitoring_is_current(admission):
        return
    await synchronize_monitoring()
    if not monitoring_is_current(admission):
        return
    if config_data is None:
        config_data = await read_config()
    if not monitoring_is_current(admission):
        return
    if config_data.get("device_count_mode") == COUNT_MODE_INBOUND and config_data.get("ip_source") == "api":
        logger.error("⛔ Per Inbound requires node logs - skipping enforcement; samples and counters are preserved")
        return
    api_mode = config_data.get("ip_source") == "api"
    active_users_snapshot = await pop_api_users_snapshot() if api_mode else await pop_active_users_snapshot(config_data)
    if active_users_snapshot is None or not monitoring_is_current(admission):
        return
    api_evidence = getattr(active_users_snapshot, "api_evidence", None)
    log_evidence = getattr(active_users_snapshot, "log_evidence", None)
    allow_partial_logs = config_data.get("device_count_mode") in (COUNT_MODE_NODE, COUNT_MODE_INBOUND)
    eligible_node_ids = getattr(api_evidence or log_evidence, "eligible_node_ids", None)
    if config_data.get("device_count_mode") == COUNT_MODE_NODE and eligible_node_ids is None:
        logger.warning("Per Node enforcement waiting: missing authoritative node evidence")
        return
    if not api_mode and not LOG_OBSERVATIONS.is_current(log_evidence, allow_partial=allow_partial_logs):
        reason = log_evidence.unavailable_reason if log_evidence else "missing Logs evidence"
        logger.warning(f"Logs enforcement waiting: {reason}")
        return reason

    def cycle_is_current():
        return (
            policy_monitoring_is_current()
            and monitoring_is_current(admission)
            and (
                api_evidence_is_current(api_evidence)
                if api_evidence
                else LOG_OBSERVATIONS.is_current(log_evidence, allow_partial=allow_partial_logs)
            )
        )

    protected_users = set(api_evidence.unknown_users) if api_evidence else set()
    cycle_warning_candidates = dict(warning_system.warnings)
    policy_names = set(active_users_snapshot) | set(warning_system.warnings)
    try:
        cycle_snapshot = await read_config_snapshot(usernames=policy_names)
    except BaseException:
        # Restore only a current, unevaluated Logs batch. The merge helper rejects
        # retired generations and API evidence.
        await merge_active_users_snapshot(active_users_snapshot)
        raise
    evidence = api_evidence or log_evidence
    if cycle_snapshot.config.get("config_degraded"):
        logger.error("Enforcement waiting: current settings and user facts are unavailable")
        await merge_active_users_snapshot(active_users_snapshot)
        return
    if not cycle_is_current() or cycle_snapshot.local_revision != evidence.config_revision:
        return
    user_policies = {
        name: evaluate_user_policy(
            username=name, config=cycle_snapshot.config, facts=cycle_snapshot.user_facts.get(name)
        )
        for name in policy_names
    }
    unknown_metadata_users = {name for name, policy in user_policies.items() if not policy.known}
    protected_users.update(name for name, policy in user_policies.items() if not policy.eligible)
    all_users_log = await check_ip_used(
        config_data=config_data, active_users_snapshot=active_users_snapshot, user_policies=user_policies
    )
    if not cycle_is_current():
        return

    # Sync dynamic check interval and max warning count to warning system
    check_interval = config_data.get("check_interval", 60)
    max_warning_count = config_data.get("max_warning_count", 3)
    warning_system.update_settings(check_interval, max_warning_count)

    logger.info("📊 Building user info from active connections...")

    # Build user info with actual unique IP counts for ALL active users
    # This is critical for warning system to work correctly
    all_users_actual_ips = {}  # Maps username to set of actual unique IPs
    all_users_data = {}  # Maps username to UserType with full data
    untrustworthy_users = set()

    def refresh_sample_users() -> None:
        for username, user in active_users_snapshot.items():
            trustworthy = (
                refresh_api_user(user, api_evidence.freshness_seconds)
                if api_evidence
                else refresh_log_user(user, log_evidence)
            )
            if not trustworthy:
                protected_users.add(username)
                untrustworthy_users.add(username)
        all_users_actual_ips.clear()
        all_users_data.clear()
        for username, user in active_users_snapshot.items():
            if username not in (protected_users if api_evidence else untrustworthy_users):
                all_users_actual_ips[username] = set(user.ip)
                all_users_data[username] = user

    refresh_sample_users()

    logger.info("📊 Found %s active users", len(all_users_actual_ips))

    # Record IPs to history for the long-term reports (one statement per cycle)
    logger.debug("📝 Recording IPs to history tracker...")
    await ip_history_tracker.record_many(all_users_actual_ips)
    if not cycle_is_current():
        return

    # History persistence is another await; recency uses original source times.
    refresh_sample_users()

    # ------------------------------------------------------------------
    # Single source of truth for the device count of this cycle.
    # Warning, ban and clearing decisions MUST all use the same number,
    # otherwise the clearing path can silently undo a warning that the
    # violation path just issued (or vice versa).
    # ------------------------------------------------------------------
    device_config = DeviceCountingConfig.from_config(config_data, eligible_node_ids=eligible_node_ids)
    all_users_device_counts: dict[str, int] = {}
    cycle_trust_scores = {name: warning.trust_score for name, warning in warning_system.warnings.items()}
    for _u_name, _u_ips in all_users_actual_ips.items():
        _u_data = all_users_data.get(_u_name)
        _w_curr = warning_system.warnings.get(_u_name)
        all_users_device_counts[_u_name] = count_devices(
            _u_data,
            device_config,
            trust_score=_w_curr.trust_score if _w_curr else 0.0,
            # No connection detail for this user, so the addresses are all there
            # is. This used to pass len(_u_ips) - the raw IP count - which ignores
            # subnet grouping and would read a single /24 as N separate devices,
            # exactly the measure that caused the earlier wave of false bans.
            fallback_count=count_devices_from_ips(_u_ips, device_config),
        )

    def refresh_user_evidence(username: str) -> bool:
        # Later users may wait behind warning persistence or a panel punishment.
        # This is a local age/configuration check, with no network refetch.
        if not cycle_is_current() or username in protected_users:
            return False
        user = active_users_snapshot.get(username)
        if user is not None:
            trustworthy = (
                refresh_api_user(user, api_evidence.freshness_seconds)
                if api_evidence
                else refresh_log_user(user, log_evidence)
            )
            if not trustworthy:
                protected_users.add(username)
                return False
            ips = set(user.ip)
            all_users_actual_ips[username] = ips
            all_users_data[username] = user
            all_users_device_counts[username] = count_devices(
                user,
                device_config,
                trust_score=cycle_trust_scores.get(username, 0.0),
                fallback_count=count_devices_from_ips(ips, device_config),
            )
        if log_evidence and not log_evidence.coverage_complete:
            policy = user_policies.get(username)
            if policy is None or not policy.eligible or all_users_device_counts.get(username, 0) <= policy.limit:
                # A lower bound can expire or a policy await can raise the limit.
                # Neither proves normalization or absence on missing nodes.
                protected_users.add(username)
                return False
        return True

    async def refresh_current_policy(username: str) -> UserPolicy | None:
        """Recheck factual eligibility and evidence after every preparation await."""
        from utils.settings_service import SettingsUnavailable, read_user_policy

        if not cycle_is_current():
            return None
        try:
            policy = await read_user_policy(username, expected_db_revision=cycle_snapshot.db_revision)
        except SettingsUnavailable as error:
            protected_users.add(username)
            logger.error(f"Current policy unavailable for {username}: {error}")
            return None
        if not cycle_is_current() or get_config_revision() != cycle_snapshot.local_revision:
            return None
        if policy is None or not policy.eligible:
            protected_users.add(username)
            if policy is not None and not policy.known:
                unknown_metadata_users.add(username)
            return None
        user_policies[username] = policy
        if not refresh_user_evidence(username):
            return None
        return policy

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

    if log_evidence and not log_evidence.coverage_complete:
        # Healthy Node/Inbound observations are a lower bound: they can prove
        # a violation, but cannot prove normalization or absence on missing nodes.
        # Include retained warning names, not just users observed this cycle.
        protected_users.update(
            name
            for name, policy in user_policies.items()
            if policy.eligible and all_users_device_counts.get(name, 0) <= policy.limit
        )
        logger.info(
            "Logs enforcement has partial coverage: %s/%s included nodes ready; %s user records protected",
            len(log_evidence.eligible_node_ids),
            len(log_evidence.required_node_ids),
            len(protected_users),
        )

    # Judge the sample BEFORE the second ban path runs. This used to be computed
    # ~200 lines below, after check_persistent_violations had already deleted every
    # expired record - so the guard meant to stop a partial view from wiping counters
    # ran too late to protect the records that path touches.
    counters_are_trustworthy = cycle_is_current()

    disabled_users, warned_users = await warning_system.check_persistent_violations(
        panel_data,
        all_users_actual_ips,
        config_data,
        device_counts=all_users_device_counts,
        sample_is_trustworthy=counters_are_trustworthy,
        protected_users=protected_users,
        refresh_user_evidence=refresh_user_evidence,
        user_policies=user_policies,
        refresh_user_policy=refresh_current_policy,
        enforcement_is_current=lambda: cycle_is_current() and get_config_revision() == cycle_snapshot.local_revision,
    )

    # Combine disabled and warned users to skip them in the loop
    processed_users = disabled_users | warned_users

    # -------------------------------------------------------
    # Check current violations for ALL users (not just those in all_users_log)
    # Track users skipped due to group filter or admin filter
    group_filtered_users = {name for name, policy in user_policies.items() if policy.reason == "group_filtered"}
    admin_filtered_users = {name for name, policy in user_policies.items() if policy.reason == "admin_filtered"}
    failed_users = set()
    cycle_new_warnings = []

    for user_name, unique_ips in all_users_actual_ips.items():
        if not cycle_is_current():
            return
        # Inverted from `if not except and not processed:` to an early continue so
        # that the `try` below sits at the indentation the old `if` had. The body is
        # unchanged and un-reindented on purpose: a single exception in here used to
        # abandon the whole cycle for every remaining user, and hand-reindenting 130
        # lines to fix that is how silent bugs get made.
        if user_name in processed_users or user_name in protected_users:
            continue

        try:
            policy = user_policies[user_name]
            user_limit_number = policy.limit

            if api_evidence or log_evidence:
                if not refresh_user_evidence(user_name):
                    continue
                unique_ips = all_users_actual_ips[user_name]

            # Get user data and ISP info for this user
            user_data = all_users_data.get(user_name)

            # Effective device count comes from the cycle-wide precompute so
            # that warn / ban / clear all agree on the same number.
            effective_device_count = all_users_device_counts.get(user_name, len(unique_ips))

            if effective_device_count > user_limit_number:
                # A record may exist but be expired; treat any existing record as
                # "already monitored" so that its consecutive counter keeps growing
                # and a reached limit can never be silently dropped.
                was_monitored = user_name in warning_system.warnings
                observed_warning = warning_system.warnings.get(user_name)
                if was_monitored and observed_warning is not cycle_warning_candidates.get(user_name):
                    protected_users.add(user_name)
                    continue

                result = await warning_system.add_warning(
                    user_name,
                    effective_device_count,
                    unique_ips,
                    user_limit_number,
                    user_data=user_data,
                    panel_data=panel_data,
                    send_telegram_notification=False,
                )

                if was_monitored:
                    logger.info(
                        f"Updated monitoring for user {user_name} with {effective_device_count} devices "
                        f"({len(unique_ips)} IPs, limit={user_limit_number}) (result={result})"
                    )

                if not cycle_is_current():
                    return
                current_warning = warning_system.warnings.get(user_name)
                if was_monitored and current_warning is not observed_warning:
                    protected_users.add(user_name)
                    continue
                if current_warning is not None:
                    cycle_warning_candidates[user_name] = current_warning
                if not refresh_user_evidence(user_name):
                    continue
                effective_device_count = all_users_device_counts[user_name]
                unique_ips = all_users_actual_ips[user_name]
                if effective_device_count <= user_policies[user_name].limit:
                    continue
                if result == "violation_limit_reached":
                    candidate_warning = warning_system.warnings.get(user_name)
                    if candidate_warning is None or candidate_warning.consecutive_violations < (
                        warning_system.effective_max_warnings(candidate_warning)
                    ):
                        continue
                    current_policy = await refresh_current_policy(user_name)
                    if current_policy is None or warning_system.warnings.get(user_name) is not candidate_warning:
                        continue
                    user_limit_number = current_policy.limit
                    effective_device_count = all_users_device_counts[user_name]
                    unique_ips = all_users_actual_ips[user_name]
                    if effective_device_count <= user_limit_number:
                        continue
                    from utils.warning_system import (
                        safe_disable_user_with_punishment,
                        safe_send_disable_notification,
                    )
                    from datetime import datetime

                    def punishment_is_current():
                        return (
                            cycle_is_current()
                            and get_config_revision() == cycle_snapshot.local_revision
                            and warning_system.warnings.get(user_name) is candidate_warning
                            and candidate_warning.consecutive_violations
                            >= warning_system.effective_max_warnings(candidate_warning)
                            and refresh_user_evidence(user_name)
                            and all_users_device_counts.get(user_name, 0) > user_policies[user_name].limit
                        )

                    async def revalidate_punishment():
                        policy = await refresh_current_policy(user_name)
                        return (
                            policy is not None
                            and punishment_is_current()
                            and all_users_device_counts[user_name] > policy.limit
                        )

                    with enforcement_guard(revalidate_punishment, punishment_is_current):
                        punishment_result = await safe_disable_user_with_punishment(
                            panel_data, UserType(name=user_name, ip=[])
                        )
                    if punishment_result.get("protected"):
                        protected_users.add(user_name)
                        continue
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

                    ban_action = str(punishment_result.get("action") or "")
                    notice_action = ban_action
                    record_note = ""
                    if ban_action == "applied_unrecorded":
                        # The panel disable succeeded even though its history write failed.
                        notice_action = "revoked" if "revoke_success" in punishment_result else "disabled"
                        record_note = (
                            f"⚠️ {html.escape(str(punishment_result.get('message') or 'Violation not yet recorded'))}\n"
                        )
                    # Clear the incident before optional network enrichment or Telegram.
                    # A provider failure must never repeat an applied punishment.
                    await warning_system.clear_user_warning(user_name, expected_warning=candidate_warning)
                    if ban_action in ("reconciled", "deduplicated") or (
                        ban_action == "applied_unrecorded" and not punishment_result.get("newly_applied", False)
                    ):
                        logger.info(
                            "Punishment result for %s: %s; no new panel action was applied", user_name, ban_action
                        )
                        continue
                    await warning_system.record_disable_outcome(user_name, ban_action)
                    if notice_action in ("disabled", "revoked") and w_obj is not None:
                        from utils.warning_system.helpers import get_disable_ip_activity

                        activity_summary = await get_disable_ip_activity(w_obj, config_data)
                    if notice_action == "revoked":
                        revoke_note = (
                            "✅ Sub revoked" if punishment_result.get("revoke_success", False) else "⚠️ Revoke failed"
                        )
                        uuid_note = (
                            "✅ UUID reset" if punishment_result.get("uuid_reset_success", False) else "⚠️ UUID failed"
                        )
                        msg = (
                            f"🔄 <b>SUBSCRIPTION REVOKED + DISABLED</b> - {time_str}\n\n"
                            f"User: <code>{html.escape(user_name)}</code>\n"
                            f"Active Devices: <code>{effective_device_count}</code> ({len(unique_ips)} IPs)\n"
                            f"User limit: <code>{user_limit_number}</code>\n"
                            f"Trust Level: {trust_level} (<code>{trust_score:.0f}</code>)\n\n"
                            f"📊 Violation #{punishment_result.get('violation_count', 1)} (Step {punishment_result.get('step_index', 0) + 1})\n"
                            f"{revoke_note}, {uuid_note}\n"
                            f"Duration: <code>Until manual enable</code>\n"
                            f"{record_note}"
                            f"📊 IP Activity:\n<code>{html.escape(activity_summary)}</code>"
                        )
                    elif ban_action in ("error", "skipped"):
                        # No punishment was applied. This used to fall into the
                        # "USER DISABLED ... Until manual enable" text below, because
                        # that wording is chosen by duration_minutes == 0 - which is
                        # exactly what a failure returns. The operator was told a user
                        # had been banned forever while nothing had happened at all.
                        from html import escape as _esc

                        reason = _esc(str(punishment_result.get("message") or ban_action))
                        msg = (
                            f"❗️ <b>BAN FAILED</b> - {time_str}\n\n"
                            f"User: <code>{html.escape(user_name)}</code>\n"
                            f"Active Devices: <code>{effective_device_count}</code> ({len(unique_ips)} IPs)\n"
                            f"User limit: <code>{user_limit_number}</code>\n"
                            f"Outcome: <code>{ban_action}</code>\n"
                            f"Reason: <code>{reason}</code>\n\n"
                            f"⚠️ The user was NOT disabled. Their counter was reset, so "
                            f"{max_warning_count} fresh consecutive scans are needed again."
                        )
                    elif ban_action == "warning":
                        msg = (
                            f"⚠️ <b>WARNING STEP APPLIED</b> - {time_str}\n\n"
                            f"User: <code>{html.escape(user_name)}</code>\n"
                            f"Active Devices: <code>{effective_device_count}</code> ({len(unique_ips)} IPs)\n"
                            f"User limit: <code>{user_limit_number}</code>\n\n"
                            f"📊 Violation #{punishment_result.get('violation_count', 1)} (Step {punishment_result.get('step_index', 0) + 1})\n"
                            f"This escalation step only warns - the user was not disabled."
                        )
                    else:
                        msg = (
                            f"🚫 <b>USER DISABLED</b> - {time_str}\n\n"
                            f"User: <code>{html.escape(user_name)}</code>\n"
                            f"Active Devices: <code>{effective_device_count}</code> ({len(unique_ips)} IPs)\n"
                            f"User limit: <code>{user_limit_number}</code>\n"
                            f"Trust Level: {trust_level} (<code>{trust_score:.0f}</code>)\n\n"
                            f"📊 Violation #{punishment_result.get('violation_count', 1)} (Step {punishment_result.get('step_index', 0) + 1})\n"
                            f"{duration_text}"
                            f"{record_note}"
                            f"📊 IP Activity:\n<code>{html.escape(activity_summary)}</code>"
                        )
                    await safe_send_disable_notification(msg, user_name)
                    if ban_action in ("error", "skipped"):
                        logger.error(
                            f"❗️ Ban FAILED for {user_name} (action={ban_action}): "
                            f"{punishment_result.get('message')}. The user is still enabled."
                        )
                    else:
                        logger.warning(
                            f"🚫 Disabled user {user_name} after {max_warning_count} "
                            f"consecutive violation scans (limit: {user_limit_number}, "
                            f"action={ban_action or 'disabled'})"
                        )
                elif result in ("new", "updated"):
                    w_obj = warning_system.warnings.get(user_name)
                    trust_score = w_obj.trust_score if w_obj else 0.0
                    trust_level = w_obj.get_trust_level() if w_obj else "🟡 MEDIUM"
                    behavior = w_obj.get_behavior_summary() if w_obj else ""
                    default_consecutive = 2 if result == "updated" else 1
                    consecutive = (
                        getattr(w_obj, "consecutive_violations", default_consecutive) if w_obj else default_consecutive
                    )
                    # The record's own threshold, not the live setting: lowering
                    # max_warning_count does not apply to a streak already in progress,
                    # so "Scan 2 of 3" has to quote the rule this user is judged by.
                    item_max_warnings = (
                        warning_system.effective_max_warnings(w_obj) if w_obj else int(max_warning_count)
                    )
                    cycle_new_warnings.append(
                        {
                            "username": user_name,
                            "ip_count": effective_device_count,
                            "limit": user_limit_number,
                            "trust_score": trust_score,
                            "trust_level": trust_level,
                            "behavior": behavior,
                            "consecutive_violations": consecutive,
                            "max_warnings": item_max_warnings,
                        }
                    )
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

    # Queue the whole cycle's unknown set after policy evaluation. The queue helper
    # deduplicates requests already cached or in flight, and batching the awaits here
    # keeps panel-synchronization bookkeeping out of the per-user policy loop.
    if unknown_metadata_users:
        from utils.user_sync import queue_unknown_user_fetch

        for username in sorted(unknown_metadata_users):
            if not cycle_is_current():
                return
            await queue_unknown_user_fetch(username)

    # Clear the records of users who came back inside their limit or went away.
    # counters_are_trustworthy was decided before check_persistent_violations ran
    # (see _sample_is_trustworthy); reusing it here keeps both paths on one verdict
    # and stops the log from reporting the same partial sample twice.
    for monitored_user in list(warning_system.warnings.keys()):
        if not counters_are_trustworthy:
            break
        if not cycle_is_current():
            return
        if monitored_user in protected_users or monitored_user in failed_users:
            # Their evaluation raised an exception this cycle, so nothing about
            # them is known well enough to justify clearing their record.
            continue
        candidate_warning = cycle_warning_candidates.get(monitored_user)
        if candidate_warning is None or warning_system.warnings.get(monitored_user) is not candidate_warning:
            continue
        current_policy = await refresh_current_policy(monitored_user)
        if current_policy is None or warning_system.warnings.get(monitored_user) is not candidate_warning:
            continue
        if monitored_user not in all_users_actual_ips:
            await warning_system.clear_user_warning(monitored_user, expected_warning=candidate_warning)
            logger.info(f"✅ User {monitored_user} inactive, monitoring cleared")
        else:
            user_current_ips = all_users_actual_ips.get(monitored_user, set())
            u_lim = current_policy.limit
            # Use the same unified device count the warning path used, so a
            # warning issued this cycle cannot be cleared in the same cycle.
            user_device_count = all_users_device_counts.get(monitored_user, len(user_current_ips))
            if not cycle_is_current():
                return
            if user_device_count <= u_lim:
                await warning_system.clear_user_warning(monitored_user, expected_warning=candidate_warning)
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
    check_interval = (
        float(config_data.get("check_interval") or config_data.get("monitoring", {}).get("check_interval", 60))
        if config_data
        else 60.0
    )
    total_monitored = len(warning_system.get_monitoring_users())
    if not cycle_is_current():
        return
    if cycle_new_warnings:
        await dispatch_chunked_warnings(
            cycle_new_warnings, check_interval, total_monitored, max_warnings=int(max_warning_count)
        )

    # Clean up expired warnings. Gated on the same trustworthiness verdict as
    # check_persistent_violations: unconditional cleanup deleted exactly the records
    # that function had just preserved.
    if not cycle_is_current():
        return

    async def authorize_expired_clear(username: str) -> bool:
        policy = await refresh_current_policy(username)
        return policy is not None and all_users_device_counts.get(username, 0) <= policy.limit

    await warning_system.cleanup_expired_warnings(
        sample_is_trustworthy=counters_are_trustworthy,
        protected_users=protected_users | failed_users,
        expected_warnings=cycle_warning_candidates,
        authorize_clear=authorize_expired_clear,
    )

    if cycle_is_current():
        record_completed_scan(
            admission,
            "api" if api_mode else "logs",
            len(all_users_device_counts),
            protected_users_count=len(protected_users),
            metadata_unknown_users_count=len(unknown_metadata_users),
            failed_users_count=len(failed_users),
            coverage_complete=log_evidence.coverage_complete if log_evidence else True,
            required_node_ids=log_evidence.required_node_ids if log_evidence else None,
            eligible_node_ids=log_evidence.eligible_node_ids if log_evidence else None,
            device_count_mode=device_config.count_mode,
        )

    all_users_log.clear()


async def run_check_users_usage(panel_data: PanelType) -> None:
    """run check_ip_used() function and then run check_users_usage()"""
    while True:
        cycle_start = time.monotonic()
        config_data = await read_config(refresh=True)
        config_revision = config_data.get("_revision", get_config_revision())
        admission = await synchronize_monitoring()
        source = str(config_data.get("ip_source") or "logs")
        skip_reason = ""
        failure_reason = ""
        completed_receipt = None
        enforcement_wait_reason = ""

        # In API mode the connected IPs are collected right here, immediately
        # before enforcement, instead of being streamed in continuously by the
        # SSE log tasks. When the collector reports an untrustworthy sample the
        # cycle is skipped entirely: feeding partial data to check_users_usage
        # would clear the consecutive-violation counters of real offenders.
        run_enforcement = True
        if not monitoring_is_current(admission):
            run_enforcement = False
            skip_reason = admission.reason or "Topics monitoring changed"
        elif config_data.get("config_degraded"):
            # read_config could not reach the database, so the whitelist, the special
            # limits and the group limits are all unknown - and a group_filter that
            # reads as disabled means "limit every user". Enforcing on that would ban
            # excepted users and users whose real limit is higher than the general one.
            logger.critical(
                "⛔ Configuration is degraded (database settings unavailable) - skipping "
                "enforcement this cycle. Counters keep their values."
            )
            run_enforcement = False
            skip_reason = "Database configuration is unavailable"
        elif config_data.get("device_count_mode") == COUNT_MODE_INBOUND and config_data.get("ip_source") == "api":
            logger.error(
                "⛔ Per Inbound requires node logs - skipping collection and enforcement; counters are preserved"
            )
            run_enforcement = False
            skip_reason = "Per Inbound requires node logs"
        elif str(config_data.get("ip_source") or "logs") == "api":
            from utils.ip_source_api import collect_active_users_from_api

            try:
                run_enforcement = await collect_active_users_from_api(panel_data, config_data)
                if not run_enforcement:
                    from utils.ip_source_api import get_last_cycle_stats

                    skip_reason = (
                        get_last_cycle_stats().get("skipped_reason") or "API sample unavailable or source changed"
                    )
            except Exception as error:  # pylint: disable=broad-except
                logger.error(f"🛰️ API IP collection failed: {error}")
                run_enforcement = False
                failure_reason = str(error)

        if run_enforcement and monitoring_is_current(admission):
            # One cycle must not be able to end the process. check_users_usage is
            # awaited directly inside limiter.py's TaskGroup, so anything raising
            # here - one malformed user, one Telegram error, one DB hiccup - used to
            # abort the whole group and exit non-zero for a supervisor restart. The
            # restart then ran a fresh cycle, and because consecutive_violations
            # counts add_warning calls rather than elapsed time, a crash loop could
            # walk a user from first sighting to ban far faster than
            # check_interval * max_warnings.
            try:
                previous_receipt = get_last_completed_scan()
                enforcement_wait_reason = await check_users_usage(panel_data, config_data=config_data)
                receipt = get_last_completed_scan()
                if receipt is None or receipt is previous_receipt:
                    skip_reason = "Scan did not complete: source evidence or policy unavailable"
                else:
                    completed_receipt = receipt
            except Exception as error:  # pylint: disable=broad-except
                logger.error(
                    f"❌ Enforcement cycle failed and was abandoned: {error}. "
                    f"Counters keep their values; the next cycle re-evaluates everyone.",
                    exc_info=True,
                )
                failure_reason = str(error)

        if not monitoring_is_current(admission):
            current = monitoring_snapshot()
            skip_reason = current.reason or "Topics monitoring changed during the scan"
        if failure_reason:
            record_cycle_outcome(admission, source, "failed", failure_reason, config_revision=config_revision)
        elif skip_reason:
            record_cycle_outcome(admission, source, "skipped", skip_reason, config_revision=config_revision)

        check_interval = int(
            config_data.get("check_interval") or config_data.get("monitoring", {}).get("check_interval", 60)
        )

        # Sleep relative to the START of the cycle so that the real period stays
        # equal to check_interval. Otherwise collection time (which can be
        # minutes in API mode) is added on top of the interval and the monitoring
        # window expires before the required consecutive scans can complete.
        elapsed = time.monotonic() - cycle_start
        # General health delivery cannot abort enforcement or shorten its cadence.
        # Successful scan facts come from the immutable receipt, never a newer
        # live node snapshot. Failure details stay in local logs.
        try:
            from utils.node_health_notifications import report_cycle, report_node_health

            outcome = "failed" if failure_reason else "skipped" if skip_reason else "completed"
            receipt = completed_receipt if outcome == "completed" else None
            reason = (
                "Enforcement cycle failed"
                if failure_reason
                else "Current source evidence or policy unavailable"
                if skip_reason
                else ""
            )
            # A later live warmup must not hide an unrelated skipped scan.
            if (
                outcome == "skipped"
                and source == "logs"
                and LOG_OBSERVATIONS.source == "logs"
                and enforcement_wait_reason == "collecting a complete check interval"
                and monitoring_is_current(admission)
                and get_config_revision() == config_revision
            ):
                reason = enforcement_wait_reason
            await report_cycle(
                receipt.admission if receipt else admission,
                receipt.source if receipt else source,
                outcome,
                device_count_mode=receipt.device_count_mode
                if receipt
                else config_data.get("device_count_mode", "device"),
                coverage_complete=receipt.coverage_complete if receipt else source != "logs",
                required_node_ids=receipt.required_node_ids if receipt else None,
                eligible_node_ids=receipt.eligible_node_ids if receipt else None,
                users_counted=receipt.users_counted if receipt else 0,
                protected_users_count=receipt.protected_users_count if receipt else 0,
                duration_seconds=elapsed,
                reason=reason,
                config_revision=receipt.config_revision if receipt else config_revision,
            )
            if source == "logs" and monitoring_is_current(admission) and get_config_revision() == config_revision:
                await report_node_health(admission)
        except Exception:
            logger.warning("Node health reporting failed; the enforcement cycle result is preserved")

        elapsed = time.monotonic() - cycle_start
        if elapsed > check_interval:
            logger.warning(
                f"⏱️ Cycle took {elapsed:.1f}s, longer than check_interval={check_interval}s. "
                f"Consecutive-scan timing is degraded - lower the API fan-out cost or raise "
                f"the interval."
            )
        await wait_for_monitoring_change(admission, max(5.0, check_interval - elapsed))
