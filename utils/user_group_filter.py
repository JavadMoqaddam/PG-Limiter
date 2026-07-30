"""
User Group Filter Module
This module provides functionality to filter users based on their group membership.

Supports two modes:
- include: Only users in specified groups will be limited
- exclude: Users in specified groups will be whitelisted (not limited)

By default, when disabled, all users are subject to limiting.
"""

import time
from typing import Optional

from utils.logs import logger


# Cache for user group mappings
# Format: {username: {"group_ids": [1, 2, 3], "cached_at": timestamp}}
_user_groups_cache: dict[str, dict] = {}

# Cache for all groups from panel
# Format: {"groups": [{"id": 1, "name": "Group1"}, ...], "cached_at": timestamp}
_groups_cache: dict = {"groups": None, "cached_at": 0}

# Cache TTL in seconds (5 minutes)
CACHE_TTL = 300


def invalidate_user_groups_cache():
    """Invalidate the user groups cache"""
    global _user_groups_cache
    _user_groups_cache.clear()
    logger.info("User groups cache invalidated")


def invalidate_groups_cache():
    """Invalidate the groups list cache"""
    global _groups_cache
    _groups_cache = {"groups": None, "cached_at": 0}
    logger.info("Groups cache invalidated")


def get_cached_user_groups(username: str) -> Optional[list[int]]:
    """
    Get cached group IDs for a user.
    
    Args:
        username: The username to check
        
    Returns:
        List of group IDs if cached and valid, None otherwise
    """
    if username not in _user_groups_cache:
        return None
    
    cached = _user_groups_cache[username]
    if time.time() - cached["cached_at"] > CACHE_TTL:
        del _user_groups_cache[username]
        return None
    
    return cached["group_ids"]


def cache_user_groups(username: str, group_ids: list[int]):
    """
    Cache group IDs for a user.
    
    Args:
        username: The username
        group_ids: List of group IDs the user belongs to
    """
    _user_groups_cache[username] = {
        "group_ids": group_ids,
        "cached_at": time.time()
    }


async def get_user_groups(panel_data, username: str) -> list[int]:
    """
    Get the group IDs for a user, using database cache first, then memory cache, then API.
    
    Args:
        panel_data: Panel connection data
        username: The username to check
        
    Returns:
        List of group IDs the user belongs to
    """
    # Check database cache first (from user sync)
    try:
        from utils.user_sync import get_user_from_cache
        cached_user = await get_user_from_cache(username)
        if cached_user and cached_user.get("group_ids") is not None:
            return cached_user["group_ids"]
    except Exception:
        pass  # Fall back to other methods
    
    # Check memory cache
    cached = get_cached_user_groups(username)
    if cached is not None:
        return cached
    
    # Fetch from API as last resort
    try:
        from utils.panel_api import get_user_details
        
        user_data = await get_user_details(panel_data, username)
        if user_data is None:
            return []
        
        group_ids = user_data.get("group_ids", [])
        if group_ids is None:
            group_ids = []
        
        # Cache the result in memory
        cache_user_groups(username, group_ids)
        
        return group_ids
    except Exception as e:
        logger.error(f"Failed to get groups for user {username}: {e}")
        return []


async def get_all_groups(panel_data) -> list[dict]:
    """
    Get all groups from the panel, using cache when available.
    
    Args:
        panel_data: Panel connection data
        
    Returns:
        List of group dictionaries with id and name
    """
    global _groups_cache
    
    # Check cache
    if (_groups_cache["groups"] is not None and 
        time.time() - _groups_cache["cached_at"] < CACHE_TTL):
        return _groups_cache["groups"]
    
    # Fetch from API
    try:
        from utils.panel_api import get_groups
        
        groups = await get_groups(panel_data)
        if isinstance(groups, ValueError):
            return []
        
        _groups_cache = {
            "groups": groups,
            "cached_at": time.time()
        }
        
        return groups
    except Exception as e:
        logger.error(f"Failed to get groups: {e}")
        return []


def get_group_name(groups: list[dict], group_id: int) -> str:
    """
    Get group name by ID.
    
    Args:
        groups: List of group dictionaries
        group_id: The group ID to look up
        
    Returns:
        Group name or "Unknown" if not found
    """
    for group in groups:
        if group.get("id") == group_id:
            return group.get("name", f"Group {group_id}")
    return f"Group {group_id}"


async def should_limit_user(panel_data, username: str, config_data: dict) -> tuple[bool, str]:
    """
    Check if a user should be subject to limiting based on group filter settings.
    Uses pre-computed O(1) RAM cache from user_sync.
    
    Args:
        panel_data: Panel connection data
        username: The username to check
        config_data: Configuration data with group_filter settings
        
    Returns:
        Tuple of (should_limit: bool, reason: str)
    """
    group_filter = config_data.get("group_filter", {})
    if not group_filter.get("enabled", False):
        return (True, "")

    from utils.user_sync import USER_METADATA_CACHE, queue_unknown_user_fetch

    # Fast O(1) RAM lookup
    if username in USER_METADATA_CACHE:
        is_monitored = USER_METADATA_CACHE[username].get("is_monitored", True)
        if is_monitored:
            return (True, "")
        else:
            return (False, "User excluded by pre-computed group filter")

    # Unknown user: Queue in background worker without blocking
    await queue_unknown_user_fetch(username)

    # Fallback to standard check if not yet in cache
    user_group_ids = await get_user_groups(panel_data, username)
    filter_group_ids = group_filter.get("group_ids", [])
    if not filter_group_ids:
        return (True, "")

    filter_mode = group_filter.get("mode", "include")
    str_filter_ids = [str(x) for x in filter_group_ids]
    str_user_group_ids = [str(x) for x in user_group_ids]
    user_in_filter_groups = any(gid in str_filter_ids for gid in str_user_group_ids)

    if filter_mode == "include":
        return (True, "") if user_in_filter_groups else (False, "User not in monitored groups")
    else:
        return (False, "User in whitelisted group") if user_in_filter_groups else (True, "")


async def batch_filter_users(panel_data, usernames: list[str], config_data: dict) -> tuple[set[str], set[str]]:
    """
    Filter a batch of users based on group settings.
    
    Args:
        panel_data: Panel connection data
        usernames: List of usernames to filter
        config_data: Configuration data
        
    Returns:
        Tuple of (users_to_limit, users_skipped)
    """
    group_filter = config_data.get("group_filter", {})
    
    # If filtering is disabled, return all users as to_limit
    if not group_filter.get("enabled", False):
        return (set(usernames), set())
    
    filter_group_ids = group_filter.get("group_ids", [])
    if not filter_group_ids:
        return (set(usernames), set())
    
    users_to_limit = set()
    users_skipped = set()
    
    for username in usernames:
        should_limit, _ = await should_limit_user(panel_data, username, config_data)
        if should_limit:
            users_to_limit.add(username)
        else:
            users_skipped.add(username)
    
    if users_skipped:
        logger.info(f"Group filter: {len(users_skipped)} users skipped, {len(users_to_limit)} users monitored")
    
    return (users_to_limit, users_skipped)


def get_filter_status_text(config_data: dict, groups: list[dict] = None) -> str:
    """
    Get a human-readable status of the group filter.
    
    Args:
        config_data: Configuration data
        groups: Optional list of group dictionaries for name lookup
        
    Returns:
        Formatted status text
    """
    group_filter = config_data.get("group_filter", {})
    
    if not group_filter.get("enabled", False):
        return "❌ Group filter disabled (all users monitored)"
    
    mode = group_filter.get("mode", "include")
    group_ids = group_filter.get("group_ids", [])
    
    if not group_ids:
        return "⚠️ Group filter enabled but no groups selected"
    
    # Get group names if available
    group_names = []
    for gid in group_ids:
        if groups:
            name = get_group_name(groups, gid)
            group_names.append(f"{name} ({gid})")
        else:
            group_names.append(f"ID: {gid}")
    
    groups_text = ", ".join(group_names)
    
    if mode == "include":
        return f"✅ Include mode: Only users in [{groups_text}] are monitored"
    else:
        return f"✅ Exclude mode: Users in [{groups_text}] are whitelisted"
