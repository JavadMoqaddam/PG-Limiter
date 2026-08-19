"""
This module checks if a user (name and IP address)
appears more than two times in the ACTIVE_USERS list.
Enhanced with warning system and ISP detection.
"""

import asyncio
from dataclasses import dataclass, field
import ipaddress
import re
import time
from collections import Counter

from telegram_bot.send_message import send_logs, send_user_message
from utils.logs import logger
from utils.panel_api import disable_user
from utils.read_config import read_config, get_config_value
from utils.types import PanelType, UserType, EnhancedUserInfo
from utils.warning_system import warning_system  # global shared instance
from utils.isp_detector import ISPDetector
from utils.ip_history_tracker import ip_history_tracker
from utils.user_group_filter import should_limit_user, get_filter_status_text
from utils.admin_filter import should_limit_user_by_admin

from utils.shared_state import ACTIVE_USERS, ACTIVE_USERS_LOCK, get_active_users_snapshot, pop_active_users_snapshot

# Re-export internal alias for backward compatibility
_active_users_lock = ACTIVE_USERS_LOCK

# Use global warning system instance imported above
# (previously a separate instance; having two caused reset button to
# clear one copy but leave the other untouched)
isp_detector = None  # Will be initialized when needed


def _ensure_isp_detector(config_data: dict) -> ISPDetector:
    """
    Ensure the global ISPDetector singleton is initialized and updated with the latest config.
    
    Args:
        config_data: Configuration dictionary containing ipinfo_token or api config.
        
    Returns:
        ISPDetector: The global ISPDetector instance.
    """
    global isp_detector
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

# Pattern to match usernames ending with .X.User where X is a number (e.g., amir.2.User)
USERNAME_LIMIT_PATTERN = re.compile(r'\.(\d+)\.User$')
# Pattern to match usernames ending with XUser where X is a number (e.g., Bastami22User, MVHHe2User)
USERNAME_LIMIT_PATTERN_SIMPLE = re.compile(r'(\d+)User$')

# Cache for limit patterns from database
_limit_patterns_cache: list | None = None
_limit_patterns_cache_time: float = 0


async def get_limit_from_patterns(username: str) -> int | None:
    """
    Get IP limit from database patterns (prefix/postfix).
    
    Args:
        username: The username to check
        
    Returns:
        The IP limit if pattern matches, None otherwise
    """
    global _limit_patterns_cache, _limit_patterns_cache_time
    import time
    
    # Refresh cache every 60 seconds
    current_time = time.time()
    if _limit_patterns_cache is None or (current_time - _limit_patterns_cache_time) > 60:
        try:
            from db.database import get_db
            from db.crud import LimitPatternCRUD
            
            async with get_db() as db:
                _limit_patterns_cache = await LimitPatternCRUD.get_all(db)
                _limit_patterns_cache_time = current_time
        except Exception as e:
            logger.error(f"Failed to load limit patterns: {e}")
            _limit_patterns_cache = []
            return None
    
    if not _limit_patterns_cache:
        return None
    
    # Check patterns in order of creation (first match wins)
    for pattern in _limit_patterns_cache:
        if pattern.pattern_type == "prefix":
            if username.startswith(pattern.pattern):
                return pattern.ip_limit
        elif pattern.pattern_type == "postfix":
            if username.endswith(pattern.pattern):
                return pattern.ip_limit
    
    return None


def extract_limit_from_username(username: str) -> int | None:
    """
    Extract limit number from username if it ends with pattern like .2.User or 2User.
    
    Args:
        username: The username to check
        
    Returns:
        The limit number if pattern matches, None otherwise
        
    Examples:
        "amir.1.User" -> 1
        "mjd.2.User" -> 2
        "Bastami22User" -> 2 (takes last digit before 'User')
        "MVHHe2User" -> 2
        "normal_user" -> None
    """
    # First try the .X.User pattern
    match = USERNAME_LIMIT_PATTERN.search(username)
    if match:
        return int(match.group(1))
    
    # Then try the simple XUser pattern (like 2User at the end)
    match = USERNAME_LIMIT_PATTERN_SIMPLE.search(username)
    if match:
        # Get the number - if it's multi-digit like "22User", take the last digit
        number_str = match.group(1)
        # Take only the last digit to get the limit
        return int(number_str[-1])
    
    return None


async def resolve_effective_limit(
    username: str,
    config: dict | None = None,
    metadata: dict | None = None,
    special_limit: dict[str, int] | None = None,
    group_limits: dict[str, int] | None = None,
    auto_persist_pattern: bool = False,
) -> int:
    """
    Single source of truth for resolving a user's effective IP limit.
    
    Priority order:
    1. Special Limit (Direct user override in DB / special_limit dict)
    2. Pre-computed Metadata Limit (effective_ip_limit from RAM metadata)
    3. Database Limit Patterns (Prefix/Postfix patterns from DB)
    4. Username Regex Patterns (.X.User or XUser)
    5. Group Limit (Batched group limit from Pasargad group)
    6. General Fallback Limit (Default config limit, e.g. 2)
    
    Args:
        username: Username to resolve limit for
        config: Full or partial configuration dictionary
        metadata: Cached metadata dictionary for the user (optional)
        special_limit: Mapping of username -> special limit override (optional)
        group_limits: Mapping of username -> group limit (optional)
        auto_persist_pattern: If True, save auto-detected pattern limits into DB
        
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

    # 3. Check database limit patterns (prefix/postfix)
    pattern_limit = await get_limit_from_patterns(username)
    
    # 4. Fallback to username regex pattern (.2.User or 2User)
    if pattern_limit is None:
        pattern_limit = extract_limit_from_username(username)
        
    if pattern_limit is not None:
        if auto_persist_pattern:
            try:
                from db.database import get_db
                from db.crud import UserCRUD
                async with get_db() as db:
                    await UserCRUD.set_special_limit(db, username, pattern_limit)
                    await db.commit()
                if special_limit is not None:
                    special_limit[username] = pattern_limit
                logger.info(f"✅ Auto-set limit for {username} to {pattern_limit} based on username pattern")
            except Exception as e:
                logger.error(f"Failed to auto-set limit for {username}: {e}")
        return int(pattern_limit)

    # 5. Check Group Limit (from pre-batched mapping)
    if group_limits and username in group_limits:
        try:
            return int(group_limits[username])
        except (ValueError, TypeError):
            pass

    # 5b. Direct Group Limit Fallback (Defense-in-depth from config & user group_ids)
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

    # 6. General Fallback Limit
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


# ═══════════════════════════════════════════════════════════════════════════════
# NEW: Smart Batching for Group Limits (Performance Optimized)
# ═══════════════════════════════════════════════════════════════════════════════
async def get_group_limits_batch(usernames: list[str], config_data: dict, panel_data: PanelType) -> dict[str, int]:
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
                        # Cache write-back to RAM for O(1) in future cycles
                        if row.username not in USER_METADATA_CACHE:
                            USER_METADATA_CACHE[row.username] = {}
                        USER_METADATA_CACHE[row.username]["group_ids"] = gids
        except Exception as e:
            logger.error(f"Batch fetch group_ids failed: {e}")
        
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
                    # Cache write-back to RAM for O(1) in future cycles
                    if username not in USER_METADATA_CACHE:
                        USER_METADATA_CACHE[username] = {}
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


async def get_active_users_metadata_batch(usernames: list[str]) -> dict[str, dict]:
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
        if u in USER_METADATA_CACHE:
            metadata[u] = USER_METADATA_CACHE[u]
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
    return metadata


def group_ips_by_subnet(ip_list: list[str]) -> tuple[list[str], dict[str, list[str]]]:
    """
    Group IPs by their /24 subnet and return formatted representations.
    Shows individual IPs when 2 or fewer, shows subnet.x (count) when more than 2.

    Args:
        ip_list (list[str]): List of IP addresses

    Returns:
        tuple[list[str], dict[str, list[str]]]: 
            - List of formatted subnet representations
            - Dictionary mapping formatted representations to actual IPs
    """
    subnet_groups = {}
    ip_mapping = {}

    for ip in ip_list:
        try:
            # Parse the IP address
            ip_obj = ipaddress.ip_address(ip)

            # For IPv4, group by /24 subnet (first 3 octets)
            if ip_obj.version == 4:
                # Get the network address for /24 subnet
                network = ipaddress.ip_network(f"{ip}/24", strict=False)
                subnet_base = network.network_address.exploded.rsplit('.', 1)[0]
                subnet_key = f"{subnet_base}.x"
            else:
                # For IPv6, use the full IP as is (less common for CDN scenarios)
                subnet_key = str(ip_obj)

            if subnet_key not in subnet_groups:
                subnet_groups[subnet_key] = []
            subnet_groups[subnet_key].append(ip)

        except ValueError:
            # If IP parsing fails, treat as individual IP
            subnet_key = ip
            if subnet_key not in subnet_groups:
                subnet_groups[subnet_key] = []
            subnet_groups[subnet_key].append(ip)

    # Format the output based on count
    formatted_results = []
    
    for subnet_key, ips in subnet_groups.items():
        if len(ips) <= 2:
            # Show individual IPs when 2 or fewer
            for ip in ips:
                formatted_results.append(ip)
                ip_mapping[ip] = [ip]
        else:
            # Show subnet.x (count) when more than 2
            formatted_subnet = f"{subnet_key} ({len(ips)})"
            formatted_results.append(formatted_subnet)
            ip_mapping[formatted_subnet] = ips
    
    return formatted_results, ip_mapping


@dataclass(slots=True)
class DeviceCountingConfig:
    """Configuration container for device counting and IP grouping rules."""
    cdn_inbounds: list[str] = field(default_factory=list)
    cdn_nodes: list[int] = field(default_factory=list)
    disabled_nodes: list[int] = field(default_factory=list)
    subnet_ip_grouping: bool = False
    high_trust_ip_grouping: bool = False
    high_trust_threshold: int = 20

    @classmethod
    def from_config(cls, config_data: dict) -> "DeviceCountingConfig":
        """Build DeviceCountingConfig from application config dictionary."""
        return cls(
            cdn_inbounds=config_data.get("cdn_inbounds", []) or [],
            cdn_nodes=config_data.get("cdn_nodes", []) or [],
            disabled_nodes=config_data.get("disabled_nodes", []) or [],
            subnet_ip_grouping=config_data.get("subnet_ip_grouping", False),
            high_trust_ip_grouping=config_data.get("high_trust_ip_grouping", False),
            high_trust_threshold=config_data.get("high_trust_threshold", 20),
        )


def _build_ip_details(
    user_info: EnhancedUserInfo,
    original_user: UserType,
    show_enhanced_details: bool,
    device_config: DeviceCountingConfig | None = None,
    user_trust_score: float = 0.0,
    isp_info: dict | None = None,
) -> tuple[list[str], int]:
    """
    Build IP details with connection info for a user.
    
    Args:
        user_info: Enhanced user information
        original_user: Original user data with device info
        show_enhanced_details: Whether to show detailed connection info
        device_config: DeviceCountingConfig containing CDN and grouping parameters
        user_trust_score: The user's current trust score (from warning system)
        isp_info: Dict mapping IP addresses to their ISP info (for subnet grouping)
        
    Returns:
        Tuple of (list of formatted IP detail strings, device count)
        Device count = unique (IP, inbound) combinations, with CDN inbounds/nodes counting as 1
    """
    if device_config is None:
        device_config = DeviceCountingConfig()
    if isp_info is None:
        isp_info = {}
        
    cdn_inbounds = device_config.cdn_inbounds
    cdn_nodes = device_config.cdn_nodes
    disabled_nodes = device_config.disabled_nodes
    subnet_ip_grouping = device_config.subnet_ip_grouping
    high_trust_ip_grouping = device_config.high_trust_ip_grouping
    high_trust_threshold = device_config.high_trust_threshold

    # Check if high trust mode should be applied for this user
    apply_high_trust_grouping = (
        high_trust_ip_grouping and 
        user_trust_score >= high_trust_threshold
    )
    
    device_count = 0
    unique_devices = set()  # Track unique (IP, inbound) combinations
    cdn_inbound_seen = set()  # Track CDN inbounds we've already counted
    cdn_node_seen = set()  # Track CDN nodes we've already counted
    if not original_user or not original_user.device_info or not original_user.device_info.connections:
        # Fallback: count IPs as devices if no connection info
        return [], len(user_info.formatted_ips)
    
    # Helper function to get subnet key for an IP
    def get_subnet_key(ip: str) -> str:
        """Get /24 subnet key for an IP address."""
        try:
            ip_obj = ipaddress.ip_address(ip)
            if ip_obj.version == 4:
                network = ipaddress.ip_network(f"{ip}/24", strict=False)
                return network.network_address.exploded.rsplit('.', 1)[0]
            else:
                return ip  # IPv6: use full IP
        except ValueError:
            return ip
    
    # Helper function to get /16 subnet key for an IP
    def get_wide_subnet_key(ip: str) -> str:
        """Get /16 subnet key for an IP address (first two octets)."""
        try:
            ip_obj = ipaddress.ip_address(ip)
            if ip_obj.version == 4:
                network = ipaddress.ip_network(f"{ip}/16", strict=False)
                # Get first two octets (e.g., "192.146" from "192.146.2.57")
                parts = network.network_address.exploded.split('.')
                return f"{parts[0]}.{parts[1]}"
            else:
                return ip  # IPv6: use full IP
        except ValueError:
            return ip
    
    # Helper function to get ISP name for an IP
    def get_isp_name(ip: str) -> str:
        """Get ISP name for an IP, or empty string if not available."""
        info = isp_info.get(ip, {})
        return info.get('isp', '') or info.get('org', '') or ''
    
    # Count unique devices (IP + inbound combinations)
    # For CDN inbounds/nodes, all IPs count as 1 device per inbound/node
    for conn in original_user.device_info.connections:
        # Skip connections from disabled nodes
        if conn.node_id in disabled_nodes:
            continue
        
        # Check if this is a CDN node
        if conn.node_id in cdn_nodes:
            # CDN node: count this node as 1 device regardless of IP count
            if conn.node_id not in cdn_node_seen:
                cdn_node_seen.add(conn.node_id)
                unique_devices.add(("CDN_NODE", conn.node_id))
        elif conn.inbound_protocol in cdn_inbounds:
            # CDN inbound: count this inbound as 1 device regardless of IP count
            if conn.inbound_protocol not in cdn_inbound_seen:
                cdn_inbound_seen.add(conn.inbound_protocol)
                unique_devices.add(("CDN_INBOUND", conn.inbound_protocol))
        elif apply_high_trust_grouping:
            # High Trust mode: IPs using same node AND inbound are one device
            # (for users who have built trust - detecting WiFi/Mobile switching)
            unique_devices.add(("HIGH_TRUST", conn.node_id, conn.inbound_protocol))
        elif subnet_ip_grouping:
            # Subnet IP Grouping mode:
            # - If ISP info available: use /16 subnet + ISP + node + inbound
            # - Otherwise: use /24 subnet + node + inbound
            ip_isp = get_isp_name(conn.ip)
            if ip_isp:
                # Wide subnet grouping (/16) with ISP matching
                wide_subnet_key = get_wide_subnet_key(conn.ip)
                unique_devices.add(("SUBNET_ISP_GROUP", wide_subnet_key, ip_isp, conn.node_id, conn.inbound_protocol))
            else:
                # Narrow subnet grouping (/24) without ISP info
                subnet_key = get_subnet_key(conn.ip)
                unique_devices.add(("SUBNET_GROUP", subnet_key, conn.node_id, conn.inbound_protocol))
        else:
            # Normal mode: each unique (IP, inbound) is a device
            unique_devices.add((conn.ip, conn.inbound_protocol))
    device_count = len(unique_devices)
    
    if not show_enhanced_details:
        return [], device_count
    
    ip_details = []
    
    # Group connections by IP
    ip_to_connections = {}
    for conn in original_user.device_info.connections:
        if conn.ip in user_info.user.ip:
            if conn.ip not in ip_to_connections:
                ip_to_connections[conn.ip] = []
            ip_to_connections[conn.ip].append(conn)
    
    # Create mapping of raw IP to formatted IP with ISP info
    raw_to_formatted = {}
    for formatted_ip in user_info.formatted_ips:
        if ' (' in formatted_ip:
            raw_ip = formatted_ip.split(' (')[0]
        else:
            raw_ip = formatted_ip.split(' ')[0]
        raw_to_formatted[raw_ip] = formatted_ip
    
    # Build details for each IP with inbound info
    for ip, connections in ip_to_connections.items():
        formatted_ip = raw_to_formatted.get(ip, ip)
        
        # Get unique inbounds for this IP
        unique_inbounds = list(set(c.inbound_protocol for c in connections))
        node_info = f"{connections[0].node_name}({connections[0].node_id})"
        
        if len(unique_inbounds) == 1:
            ip_details.append(f"  • {formatted_ip} → {node_info} | {unique_inbounds[0]}")
        else:
            # Multiple inbounds on same IP = multiple devices
            inbounds_str = ", ".join(unique_inbounds)
            ip_details.append(f"  • {formatted_ip} → {node_info} | [{inbounds_str}]")
    
    return ip_details, device_count


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
    isp_detector = _ensure_isp_detector(config_data)
    
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
        
        ip_details, device_count = _build_ip_details(
            user_info, original_user, show_enhanced_details, 
            device_config=device_config,
            user_trust_score=user_trust_score,
            isp_info=user_isp_info,
        )
        all_user_device_counts[email] = device_count
        all_user_ip_details[email] = ip_details
        total_devices += device_count
    
    logger.info("Number of all active ips: %s, devices: %s", str(total_ips), str(total_devices))
    
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
            auto_persist_pattern=(not is_except),
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
    isp_detector = _ensure_isp_detector(config_data)
    
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
    
    # Record IPs to history tracker for long-term tracking (Redis ZSETs)
    logger.debug("📝 Recording IPs to history tracker...")
    for username, unique_ips in all_users_actual_ips.items():
        await ip_history_tracker.record_user_ips(username, unique_ips)
    
    # Sync active users ZSET to Redis pg_limiter:active_users
    try:
        from utils.redis_cache import get_cache
        cache = await get_cache()
        if cache.is_connected and all_users_actual_ips:
            now_ts = time.time()
            cutoff_15m = now_ts - 900
            async with cache.client.pipeline(transaction=True) as pipe:
                active_mapping = {user: now_ts for user in all_users_actual_ips.keys()}
                pipe.zadd("pg_limiter:active_users", active_mapping)
                pipe.zremrangebyscore("pg_limiter:active_users", "-inf", cutoff_15m)
                await pipe.execute()
    except Exception as sync_err:
        logger.warning(f"Active users Redis ZSET sync note: {sync_err}")
    
    # Batch fetch group limits & metadata for active users
    active_usernames = list(all_users_actual_ips.keys())
    batched_group_limits = await get_group_limits_batch(active_usernames, config_data, panel_data)
    users_metadata_usage = await get_active_users_metadata_batch(active_usernames)
    
    # Check for users who still violate limits after warning period
    # Pass actual IPs and group limits
    disabled_users, warned_users = await warning_system.check_persistent_violations(
        panel_data, all_users_actual_ips, config_data, batched_group_limits
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
    
    for user_name, unique_ips in all_users_actual_ips.items():
        if user_name not in except_users and user_name not in processed_users:
            user_meta = users_metadata_usage.get(user_name, {})
            
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
                auto_persist_pattern=False,
            )
            
            if len(unique_ips) > user_limit_number:
                # Get user data and ISP info for this user
                user_data = all_users_data.get(user_name)
                user_isp_info = {ip: isp_info_batch.get(ip, {}) for ip in unique_ips if ip in isp_info_batch}
                
                # Check if user is already being monitored
                if warning_system.is_user_being_monitored(user_name):
                    # User is being monitored, update their IP count and activity tracking
                    result = await warning_system.add_warning(
                        user_name, len(unique_ips), unique_ips, user_limit_number,
                        user_data=user_data, isp_info=user_isp_info, panel_data=panel_data
                    )
                    logger.info(f"Updated monitoring for user {user_name} with {len(unique_ips)} IPs")
                else:
                    # New violation - may start monitoring or instant disable
                    result = await warning_system.add_warning(
                        user_name, len(unique_ips), unique_ips, user_limit_number,
                        user_data=user_data, isp_info=user_isp_info, panel_data=panel_data
                    )
                    
                    if result == "instant_disabled":
                        disabled_users.add(user_name)
                        logger.warning(f"User {user_name} instantly disabled due to low trust score")
                    elif result == "new":
                        message = (
                            f"User {user_name} has {len(unique_ips)} active ips (limit: {user_limit_number}). "
                            f"Warning issued - monitoring for 3 minutes."
                        )
                        logger.warning(message)
    
    # Log group filter stats if any users were filtered
    if group_filtered_users:
        logger.debug(f"Group filter: {len(group_filtered_users)} users skipped")
    
    # Log admin filter stats if any users were filtered
    if admin_filtered_users:
        logger.debug(f"Admin filter: {len(admin_filtered_users)} users skipped")
    
    # Clean up expired warnings
    await warning_system.cleanup_expired_warnings()
    
    # Send monitoring status every few cycles (optional)
    # await warning_system.send_monitoring_status()
    
    all_users_log.clear()


async def run_check_users_usage(panel_data: PanelType) -> None:
    """run check_ip_used() function and then run check_users_usage()"""
    while True:
        config_data = await read_config()
        await check_users_usage(panel_data, config_data=config_data)
        check_interval = config_data.get("monitoring", {}).get("check_interval", 60)
        await asyncio.sleep(int(check_interval))
