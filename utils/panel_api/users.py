"""
User operations for panel API.
"""

import asyncio
import random
import time
from ssl import SSLError

import httpx

from utils.handel_dis_users import DisabledUsers
from utils.user_groups_storage import UserGroupsStorage
from utils.logs import logger, log_api_request, log_user_action, get_logger
from utils.read_config import read_config
from utils.types import PanelType, UserType
from utils.panel_api.auth import get_token, invalidate_token_cache, safe_send_logs_panel

# Module logger
users_logger = get_logger("panel_api.users")


async def fetch_all_users_raw(
    panel_data: PanelType,
    status: str | None = None,
    admin: list[str] | None = None,
    group: list[int] | None = None,
    search: str | None = None,
    limit: int = 1000,
    max_concurrent: int = 10,
) -> list[dict]:
    """
    Unified high-performance fetch of all users from Panel API using parallel pagination.
    
    Args:
        panel_data: Panel connection configuration.
        status: Optional status filter (e.g. 'active', 'disabled').
        admin: Optional admin username filter.
        group: Optional group ID filter.
        search: Optional search query.
        limit: Number of users per page (default: 1000).
        max_concurrent: Max concurrent requests for remaining pages (default: 10).
        
    Returns:
        list[dict]: List of raw user dictionaries from the panel API.
    """
    from utils.panel_api.request_helper import panel_get
    
    filter_desc = []
    if status:
        filter_desc.append(f"status={status}")
    if admin:
        filter_desc.append(f"admin={admin}")
    if group:
        filter_desc.append(f"group={group}")
    if search:
        filter_desc.append(f"search={search}")
    filter_str = f" ({', '.join(filter_desc)})" if filter_desc else ""
    users_logger.debug(f"📋 Fetching users from panel{filter_str}...")
    
    params = {"offset": 0, "limit": limit}
    if status:
        params["status"] = status
    if admin:
        params["admin"] = admin
    if group:
        params["group"] = group
    if search:
        params["search"] = search
        
    start_time = time.perf_counter()
    response = await panel_get(
        panel_data,
        "/api/users",
        params=params,
        timeout=60.0,
        max_retries=3,
    )
    if response is None:
        message = "Failed to fetch first page of users from Panel API"
        users_logger.error(message)
        raise ValueError(message)
        
    try:
        data = response.json()
    except Exception as e:
        users_logger.error(f"Failed to parse JSON response from /api/users: {e}")
        raise ValueError(f"Invalid JSON from /api/users: {e}") from e
        
    if isinstance(data, dict) and "users" in data:
        first_page_users = data["users"]
        total_users = data.get("total", len(first_page_users))
    elif isinstance(data, list):
        first_page_users = data
        total_users = len(first_page_users)
    else:
        users_logger.error(f"Unexpected /api/users response format: {type(data)}")
        return []
        
    users_logger.info(f"📊 Panel reports {total_users} total users{filter_str}")
    
    # If all users fit in first page, return immediately
    if len(first_page_users) >= total_users or len(first_page_users) < limit:
        elapsed = (time.perf_counter() - start_time) * 1000
        users_logger.info(f"📋 Fetched {len(first_page_users)} users in {elapsed:.0f}ms")
        return first_page_users
        
    # Calculate remaining offsets for parallel fetching
    offsets = list(range(limit, total_users, limit))
    users_logger.info(f"📥 Fetching {len(offsets)} remaining pages in parallel (max {max_concurrent} concurrent)...")
    
    semaphore = asyncio.Semaphore(max_concurrent)
    
    async def fetch_page_task(offset: int) -> list[dict]:
        async with semaphore:
            page_params = dict(params)
            page_params["offset"] = offset
            page_resp = await panel_get(
                panel_data,
                "/api/users",
                params=page_params,
                timeout=60.0,
                max_retries=3,
            )
            if page_resp is None:
                users_logger.warning(f"Failed to fetch page at offset {offset}")
                return []
            try:
                page_data = page_resp.json()
                if isinstance(page_data, dict) and "users" in page_data:
                    return page_data["users"]
                elif isinstance(page_data, list):
                    return page_data
                return []
            except Exception as parse_err:
                users_logger.warning(f"Error parsing page at offset {offset}: {parse_err}")
                return []
                
    tasks = [fetch_page_task(off) for off in offsets]
    pages = await asyncio.gather(*tasks, return_exceptions=True)
    
    all_users = list(first_page_users)
    for i, page in enumerate(pages):
        if isinstance(page, Exception):
            users_logger.error(f"Exception fetching page {i+1}: {page}")
            continue
        if isinstance(page, list):
            all_users.extend(page)
            
    elapsed = (time.perf_counter() - start_time) * 1000
    users_logger.info(f"📋 Fetched all {len(all_users)} users in {elapsed:.0f}ms")
    return all_users


async def all_user(panel_data: PanelType) -> list[UserType] | ValueError:
    """
    Get the list of all users from the panel API as UserType objects.

    Args:
        panel_data (PanelType): Panel connection data.

    Returns:
        list[UserType]: List of user objects.
    """
    try:
        raw_users = await fetch_all_users_raw(panel_data)
        users = []
        for user_data in raw_users:
            admin_info = user_data.get("admin")
            admin_username = admin_info.get("username") if isinstance(admin_info, dict) else None
            user = UserType(
                name=user_data["username"],
                panel_status=user_data.get("status"),
                data_limit=user_data.get("data_limit"),
                used_traffic=user_data.get("used_traffic"),
                lifetime_used_traffic=user_data.get("lifetime_used_traffic"),
                expire=user_data.get("expire"),
                group_ids=user_data.get("group_ids"),
                online_at=user_data.get("online_at"),
                admin_username=admin_username,
            )
            users.append(user)
        return users
    except Exception as e:
        message = f"Failed to get users after attempts: {e}"
        await safe_send_logs_panel(message)
        users_logger.error(message)
        raise ValueError(message) from e


async def get_all_panel_users(
    panel_data: PanelType,
    status: str | None = None,
    admin: list[str] | None = None,
    group: list[int] | None = None,
    search: str | None = None,
) -> set[str] | ValueError:
    """
    Get all usernames from the panel API matching filters as a set of strings.

    Args:
        panel_data (PanelType): Panel connection data.
        status (str | None): Filter by user status (active/disabled/limited/expired/on_hold).
        admin (list[str] | None): Filter by admin username(s).
        group (list[int] | None): Filter by group ID(s).
        search (str | None): Search query for usernames.

    Returns:
        set[str]: Set of matching usernames.
    """
    try:
        raw_users = await fetch_all_users_raw(
            panel_data, status=status, admin=admin, group=group, search=search
        )
        return {u["username"] for u in raw_users if isinstance(u, dict) and "username" in u}
    except Exception as e:
        users_logger.error(f"Failed to get panel usernames: {e}")
        raise ValueError(f"Failed to get users: {e}") from e
        offset += limit


async def check_user_exists(panel_data: PanelType, username: str) -> bool:
    """
    Check if a user exists in the panel.

    Args:
        panel_data (PanelType): A PanelType object containing
        the username, password, and domain for the panel API.
        username (str): The username to check.

    Returns:
        bool: True if user exists, False otherwise.
    """
    users_logger.debug(f"👤 Checking if user exists: {username}")
    from utils.panel_api.request_helper import panel_get
    
    response = await panel_get(
        panel_data,
        f"/api/user/{username}",
        timeout=10.0,
        max_retries=2,
    )
    
    if response is not None:
        if response.status_code == 200:
            users_logger.debug(f"👤 User {username} exists")
            return True
        elif response.status_code == 404:
            users_logger.debug(f"👤 User {username} not found")
            return False
    
    users_logger.warning(f"Could not verify if user {username} exists, assuming exists")
    return True


async def get_user_details(panel_data: PanelType, username: str) -> dict | ValueError:
    """
    Get user details including group_ids from the panel API.

    Args:
        panel_data (PanelType): A PanelType object containing
        the username, password, and domain for the panel API.
        username (str): The username to get details for.

    Returns:
        dict: The user details including group_ids.

    Raises:
        ValueError: If the function fails to get user details from the API.
    """
    from utils.panel_api.request_helper import panel_get
    
    users_logger.debug(f"👤 Getting details for user: {username}")
    max_attempts = 3
    
    for attempt in range(max_attempts):
        force_refresh = attempt > 0
        
        response = await panel_get(
            panel_data, 
            f"/api/user/{username}",
            force_refresh=force_refresh,
            timeout=10.0,
            max_retries=2
        )
        
        if response is not None:
            if response.status_code == 200:
                try:
                    user_data = response.json()
                    users_logger.debug(f"👤 Got details for {username}: groups={user_data.get('group_ids', [])}")
                    return user_data
                except Exception as json_error:
                    users_logger.error(f"Failed to parse JSON for {username}: {json_error}")
            elif response.status_code == 404:
                users_logger.warning(f"User {username} not found")
                return None
            elif response.status_code == 401:
                await invalidate_token_cache()
                users_logger.warning("Got 401 error, invalidating token cache and retrying")
                continue
        
        users_logger.warning(f"Attempt {attempt + 1}/{max_attempts} failed")
        
        if attempt < max_attempts - 1:
            wait_time = min(10, random.randint(1, 3) * (attempt + 1))
            await asyncio.sleep(wait_time)
    
    message = f"Failed to get user details for {username} after {max_attempts} attempts."
    users_logger.error(message)
    raise ValueError(message)


async def get_user_admin(panel_data: PanelType, username: str) -> str | None:
    """
    Get the admin (owner) username for a specific user.

    Args:
        panel_data (PanelType): Panel connection data.
        username (str): The username to check.

    Returns:
        str | None: The admin username who owns this user, or None if not found.
    """
    users_logger.debug(f"👤 Getting admin for user: {username}")
    try:
        user_details = await get_user_details(panel_data, username)
        if user_details and "admin" in user_details:
            admin_info = user_details["admin"]
            if isinstance(admin_info, dict) and "username" in admin_info:
                admin_name = admin_info["username"]
                users_logger.debug(f"👤 User {username} owned by admin: {admin_name}")
                return admin_name
            elif isinstance(admin_info, str):
                users_logger.debug(f"👤 User {username} owned by admin: {admin_info}")
                return admin_info
        users_logger.debug(f"👤 No admin found for user: {username}")
        return None
    except Exception as e:
        users_logger.error(f"Error getting admin for user {username}: {e}")
        return None


async def update_user_groups(panel_data: PanelType, username: str, group_ids: list[int]) -> bool:
    """
    Update user's group_ids in the panel.

    Args:
        panel_data (PanelType): A PanelType object containing
        the username, password, and domain for the panel API.
        username (str): The username to update.
        group_ids (list[int]): The list of group IDs to set for the user.

    Returns:
        bool: True if successful, False otherwise.
    """
    from utils.panel_api.request_helper import panel_put
    
    users_logger.info(f"👥 Updating groups for user {username} to {group_ids}")
    payload = {"group_ids": group_ids}
    
    response = await panel_put(
        panel_data,
        f"/api/user/{username}",
        json_data=payload,
        timeout=15.0,
        max_retries=3,
    )
    
    if response is not None:
        if response.status_code in (200, 201):
            log_user_action("UPDATE_GROUPS", username, f"groups={group_ids}", success=True)
            users_logger.info(f"👥 Updated groups for user {username} to {group_ids}")
            return True
        elif response.status_code == 404:
            users_logger.warning(f"User {username} not found")
            return False
    
    message = f"Failed to update groups for user {username}"
    log_user_action("UPDATE_GROUPS", username, message, success=False)
    users_logger.error(message)
    return False


async def enable_all_user(panel_data: PanelType) -> None | ValueError:
    """
    Enable all users on the panel.

    Args:
        panel_data (PanelType): A PanelType object containing
        the username, password, and domain for the panel API.

    Returns:
        None
    """
    from utils.panel_api.request_helper import panel_put
    
    users_logger.info("✅ Enabling all users...")
    users = await all_user(panel_data)
    if isinstance(users, ValueError):
        raise users
    
    enabled_count = 0
    failed_count = 0
    status = {"status": "active"}
    
    for user_obj in users:
        username = user_obj.name
        response = await panel_put(
            panel_data,
            f"/api/user/{username}",
            json_data=status,
            timeout=10.0,
            max_retries=2,
        )
        if response is not None and response.status_code in (200, 201):
            log_user_action("ENABLE", username, success=True)
            users_logger.debug(f"Enabled user: {username}")
            enabled_count += 1
        else:
            message = f"Failed to enable user: {username}"
            await safe_send_logs_panel(message)
            log_user_action("ENABLE", username, message, success=False)
            users_logger.error(message)
            failed_count += 1
            
    users_logger.info(f"✅ Enabled all users: {enabled_count} success, {failed_count} failed")


async def enable_user_by_status(panel_data: PanelType, username: str) -> tuple[bool, bool]:
    """
    Enable a user by changing their status to 'active'.

    Args:
        panel_data (PanelType): Panel connection data.
        username (str): The username to enable.

    Returns:
        tuple[bool, bool]: (success, not_found)
            - success: True if user was successfully enabled
            - not_found: True if user doesn't exist (404), should be removed from disabled list
    """
    from utils.panel_api.request_helper import panel_put
    
    users_logger.debug(f"✅ Enabling user by status: {username}")
    status = {"status": "active"}
    
    response = await panel_put(
        panel_data,
        f"/api/user/{username}",
        json_data=status,
        timeout=10.0,
        max_retries=3,
    )
    
    if response is not None:
        if response.status_code in (200, 201):
            log_user_action("ENABLE", username, "status=active", success=True)
            users_logger.info(f"✅ Enabled user by status: {username}")
            return (True, False)  # success, not deleted
        elif response.status_code == 404:
            users_logger.warning(f"User {username} not found (deleted from panel)")
            log_user_action("ENABLE", username, "User not found (deleted)", success=False)
            return (False, True)  # failed, user was deleted
    
    log_user_action("ENABLE", username, "Failed to enable user", success=False)
    return (False, False)  # failed, but user might still exist


async def _clear_database_disable_flags(username: str) -> None:
    """
    Clear disable flags in database when a user is enabled.
    
    Args:
        username: The username to clear flags for.
    """
    try:
        from db.database import get_db_session
        from db.crud import UserCRUD
        
        async with get_db_session() as db:
            user_record = await UserCRUD.get_by_username(db, username)
            if user_record:
                user_record.is_disabled_by_limiter = False
                user_record.original_groups = []
                user_record.disabled_at = None
                user_record.enable_at = None
                await db.commit()
                users_logger.debug(f"📦 Cleared disable flags in database for {username}")
    except Exception as db_error:
        users_logger.warning(f"Could not clear disable flags in database: {db_error}")


async def enable_user_by_group(panel_data: PanelType, username: str) -> tuple[bool, bool]:
    """
    Enable a user by restoring their original groups and setting status to active.
    Combines both operations into a single API request.
    
    Tries to get original groups from:
    1. JSON file backup (.user_groups_backup.json)
    2. Database (User.original_groups field)
    3. Falls back to removing from disabled group without restoring groups

    Args:
        panel_data (PanelType): Panel connection data.
        username (str): The username to enable.

    Returns:
        tuple[bool, bool]: (success, not_found)
            - success: True if user was successfully enabled
            - not_found: True if user doesn't exist (404), should be removed from disabled list
    """
    users_logger.debug(f"✅ Enabling user by group restore: {username}")
    try:
        # Try 1: Get from JSON file storage
        groups_storage = UserGroupsStorage()
        original_groups = await groups_storage.get_user_groups(username)
        groups_source = "json"
        
        # Try 2: If not in JSON, try database
        if original_groups is None:
            users_logger.debug(f"📦 No groups in JSON for {username}, checking database...")
            try:
                from db.database import get_db_session
                from db.crud import UserCRUD
                
                async with get_db_session() as db:
                    user_record = await UserCRUD.get_disabled_record(db, username)
                    if user_record and user_record.original_groups:
                        original_groups = user_record.original_groups
                        groups_source = "database"
                        users_logger.info(f"📦 Found original groups in database for {username}: {original_groups}")
            except Exception as db_error:
                users_logger.debug(f"Could not check database for groups: {db_error}")
        
        if original_groups is None:
            users_logger.warning(f"No saved groups found for user {username} in JSON or database, will use fallback group")
            # Get config to check if user is in disabled group and get fallback group
            data = await read_config()
            disabled_group_id = data.get("disabled_group_id", None)
            fallback_group_id = data.get("fallback_group_id", None)
            
            # Always get current user details to check their group status
            user_data = await get_user_details(panel_data, username)
            if user_data is None:
                # User doesn't exist in panel (404)
                users_logger.warning(f"User {username} not found (deleted from panel)")
                # Clean up from JSON storage if exists
                await groups_storage.remove_user(username)
                return (False, True)  # failed, user was deleted
            
            current_groups = user_data.get("group_ids", []) or []
            
            # Calculate new groups: remove disabled group, add fallback group if set
            new_groups = [g for g in current_groups if g != disabled_group_id]
            
            # If fallback group is set and not already in new_groups, add it
            if fallback_group_id is not None and fallback_group_id not in new_groups:
                new_groups.append(fallback_group_id)
                users_logger.info(f"👥 Adding fallback group {fallback_group_id} for user {username}")
            
            # If no groups at all, use fallback group only
            if not new_groups and fallback_group_id is not None:
                new_groups = [fallback_group_id]
            
            users_logger.info(f"👥 Enabling user {username} with groups: {new_groups} (no saved original groups)")
            success, not_found = await _update_user_groups_and_status(panel_data, username, new_groups, "active")
            if not_found:
                await groups_storage.remove_user(username)
                return (False, True)
            if success:
                # Clear database disable flags
                await _clear_database_disable_flags(username)
                log_user_action("ENABLE", username, f"set groups to {new_groups}, status active (fallback)", success=True)
                users_logger.info(f"✅ Enabled user: {username} (groups: {new_groups}, status active)")
                return (True, False)
            else:
                users_logger.error(f"❌ Failed to enable {username}")
                return (False, False)
        
        # We have original_groups - restore them
        # But first, ensure we don't accidentally restore the disabled group
        data = await read_config()
        disabled_group_id = data.get("disabled_group_id", None)
        fallback_group_id = data.get("fallback_group_id", None)
        
        # Filter out the disabled group from original_groups if present
        if disabled_group_id is not None and disabled_group_id in original_groups:
            users_logger.warning(f"⚠️ Removing disabled_group_id {disabled_group_id} from original_groups for {username}")
            original_groups = [g for g in original_groups if g != disabled_group_id]
        
        # If original_groups is empty after filtering, use fallback group
        if not original_groups and fallback_group_id is not None:
            original_groups = [fallback_group_id]
            users_logger.info(f"👥 Original groups empty, using fallback group {fallback_group_id} for {username}")
        # Ensure fallback group is in the groups if set
        elif fallback_group_id is not None and fallback_group_id not in original_groups:
            original_groups.append(fallback_group_id)
            users_logger.info(f"👥 Adding fallback group {fallback_group_id} to original groups for {username}")
        
        users_logger.debug(f"👥 Restoring original groups for {username} (from {groups_source}): {original_groups}")
        # Combined API call: set both group_ids and status in one request
        success, not_found = await _update_user_groups_and_status(panel_data, username, original_groups, "active")
        
        if not_found:
            # User was deleted - clean up from storage
            await groups_storage.remove_user(username)
            log_user_action("ENABLE", username, "User not found (deleted)", success=False)
            return (False, True)
        
        if success:
            # Clean up from JSON storage
            await groups_storage.remove_user(username)
            
            # Clear database disable flags
            await _clear_database_disable_flags(username)
            
            log_user_action("ENABLE", username, f"restored groups {original_groups} (from {groups_source}), status active", success=True)
            users_logger.info(f"✅ Enabled user by group: {username} (restored groups {original_groups}, status active)")
            return (True, False)
        log_user_action("ENABLE", username, "Failed to restore groups", success=False)
        return (False, False)
    except Exception as error:
        users_logger.error(f"Error enabling user by group: {error}")
        log_user_action("ENABLE", username, str(error), success=False)
        return (False, False)


async def _update_user_groups_and_status(panel_data: PanelType, username: str, group_ids: list[int], status: str) -> tuple[bool, bool]:
    """
    Internal helper to update both user groups and status in a single API call.
    
    Args:
        panel_data: Panel connection data.
        username: The username to update.
        group_ids: List of group IDs to set.
        status: Status to set ("active" or "disabled").
    
    Returns:
        tuple[bool, bool]: (success, not_found)
            - success: True if successful
            - not_found: True if user doesn't exist (404)
    """
    from utils.panel_api.request_helper import panel_put
    
    payload = {"group_ids": group_ids, "status": status}
    response = await panel_put(
        panel_data,
        f"/api/user/{username}",
        json_data=payload,
        timeout=10.0,
        max_retries=3,
    )
    
    if response is not None:
        if response.status_code in (200, 201):
            users_logger.debug(f"Updated user {username}: groups={group_ids}, status={status}")
            return (True, False)  # success, not deleted
        elif response.status_code == 404:
            users_logger.warning(f"User {username} not found (deleted from panel)")
            return (False, True)  # failed, user was deleted
            
    return (False, False)  # failed, but user might still exist


async def enable_selected_users(
    panel_data: PanelType, inactive_users: set[str]
) -> dict[str, list[str]]:
    """
    Enable selected users on the panel.
    Uses either status-based or group-based enabling depending on config.

    Args:
        panel_data (PanelType): A PanelType object containing
        the username, password, and domain for the panel API.
        inactive_users (set[str]): A list of user str that are currently inactive.

    Returns:
        dict with 'enabled', 'failed', and 'not_found' lists of usernames.
        - enabled: Users successfully enabled
        - failed: Users that failed but might still exist (retry later)
        - not_found: Users that were deleted from panel (should be removed from disabled list)
    """
    users_logger.info(f"✅ Enabling {len(inactive_users)} selected users...")
    data = await read_config()
    disable_method = data.get("disable_method", "status")
    disabled_group_id = data.get("disabled_group_id", None)
    use_group_method = disable_method == "group" and disabled_group_id is not None
    
    users_logger.debug(f"Using enable method: {'group' if use_group_method else 'status'}")
    
    enabled_users: list[str] = []
    failed_users: list[str] = []
    not_found_users: list[str] = []  # Users deleted from panel
    
    for username in inactive_users:
        try:
            if use_group_method:
                # Always try group-based enable when using group method
                # enable_user_by_group now handles cases with and without saved groups
                success, not_found = await enable_user_by_group(panel_data, username)
                if not_found:
                    message = f"User {username} was deleted from panel, removing from disabled list"
                    await safe_send_logs_panel(message)
                    users_logger.warning(message)
                    not_found_users.append(username)
                elif success:
                    message = f"Enabled user (group method): {username}"
                    await safe_send_logs_panel(message)
                    enabled_users.append(username)
                else:
                    message = f"Failed to enable user: {username}"
                    await safe_send_logs_panel(message)
                    users_logger.error(message)
                    failed_users.append(username)
            else:
                success, not_found = await enable_user_by_status(panel_data, username)
                if not_found:
                    message = f"User {username} was deleted from panel, removing from disabled list"
                    await safe_send_logs_panel(message)
                    users_logger.warning(message)
                    not_found_users.append(username)
                elif success:
                    message = f"Enabled user: {username}"
                    await safe_send_logs_panel(message)
                    enabled_users.append(username)
                else:
                    message = f"Failed to enable user: {username}"
                    await safe_send_logs_panel(message)
                    users_logger.error(message)
                    failed_users.append(username)
        except Exception as e:
            message = f"Failed to enable user {username}: {e}"
            await safe_send_logs_panel(message)
            users_logger.error(message)
            failed_users.append(username)
    
    users_logger.info(f"✅ Enabled selected users: {len(enabled_users)} success, {len(failed_users)} failed, {len(not_found_users)} not found")
    return {"enabled": enabled_users, "failed": failed_users, "not_found": not_found_users}


async def revoke_user_subscription(panel_data: PanelType, username: str) -> bool:
    """
    Revoke a user's subscription (subscription link and proxies).
    This will regenerate the vless/vmess UUID and subscription URL.

    Args:
        panel_data (PanelType): Panel connection data.
        username (str): The username whose subscription to revoke.

    Returns:
        bool: True if successful, False otherwise.
    """
    from utils.panel_api.request_helper import panel_post
    
    users_logger.info(f"🔄 Revoking subscription for user: {username}")
    max_attempts = 3
    
    for attempt in range(max_attempts):
        force_refresh = attempt > 0
        
        response = await panel_post(
            panel_data,
            f"/api/user/{username}/revoke_sub",
            json_data={},
            force_refresh=force_refresh,
            timeout=10.0,
            max_retries=2
        )
        
        if response is not None:
            if response.status_code in (200, 201):
                log_user_action("REVOKE_SUB", username, "subscription revoked", success=True)
                users_logger.info(f"🔄 Revoked subscription for user: {username}")
                return True
            elif response.status_code == 401:
                await invalidate_token_cache()
                users_logger.warning("Got 401 error, retrying...")
                continue
            elif response.status_code == 404:
                log_user_action("REVOKE_SUB", username, "User not found", success=False)
                users_logger.warning(f"User {username} not found in panel")
                return False
            else:
                users_logger.error(f"Failed to revoke subscription: {response.status_code}")
                continue
        
        wait_time = min(30, random.randint(2, 5) * (attempt + 1))
        await asyncio.sleep(wait_time)
    
    log_user_action("REVOKE_SUB", username, "Failed after retries", success=False)
    return False


async def reset_user_uuid(panel_data: PanelType, username: str) -> bool:
    """
    Reset/change a user's vless and vmess UUID.
    This generates new UUIDs and updates the user's proxy settings.

    Args:
        panel_data (PanelType): Panel connection data.
        username (str): The username whose UUID to reset.

    Returns:
        bool: True if successful, False otherwise.
    """
    import uuid
    from utils.panel_api.request_helper import panel_put
    
    users_logger.info(f"🔑 Resetting UUID for user: {username}")
    
    # Generate new UUIDs
    new_vless_uuid = str(uuid.uuid4())
    new_vmess_uuid = str(uuid.uuid4())
    
    max_attempts = 3
    
    for attempt in range(max_attempts):
        force_refresh = attempt > 0
        
        # Build proxy_settings payload with new UUIDs
        payload = {
            "proxy_settings": {
                "vless": {"id": new_vless_uuid},
                "vmess": {"id": new_vmess_uuid}
            }
        }
        
        response = await panel_put(
            panel_data,
            f"/api/user/{username}",
            json_data=payload,
            force_refresh=force_refresh,
            timeout=10.0,
            max_retries=2
        )
        
        if response is not None:
            if response.status_code in (200, 201):
                log_user_action("RESET_UUID", username, f"vless={new_vless_uuid[:8]}..., vmess={new_vmess_uuid[:8]}...", success=True)
                users_logger.info(f"🔑 Reset UUID for user {username}: vless={new_vless_uuid[:8]}..., vmess={new_vmess_uuid[:8]}...")
                return True
            elif response.status_code == 401:
                await invalidate_token_cache()
                users_logger.warning("Got 401 error, retrying...")
                continue
            elif response.status_code == 404:
                log_user_action("RESET_UUID", username, "User not found", success=False)
                users_logger.warning(f"User {username} not found in panel")
                return False
            else:
                users_logger.error(f"Failed to reset UUID: {response.status_code} - {response.text}")
                continue
        
        wait_time = min(30, random.randint(2, 5) * (attempt + 1))
        await asyncio.sleep(wait_time)
    
    log_user_action("RESET_UUID", username, "Failed after retries", success=False)
    return False


async def disable_user_by_status(panel_data: PanelType, username: str) -> bool:
    """
    Disable a user by changing their status to 'disabled'.

    Args:
        panel_data (PanelType): Panel connection data.
        username (str): The username to disable.

    Returns:
        bool: True if successful, False otherwise.
    """
    from utils.panel_api.request_helper import panel_put
    
    users_logger.debug(f"🚫 Disabling user by status: {username}")
    status = {"status": "disabled"}
    
    response = await panel_put(
        panel_data,
        f"/api/user/{username}",
        json_data=status,
        timeout=10.0,
        max_retries=3,
    )
    
    if response is not None:
        if response.status_code in (200, 201):
            log_user_action("DISABLE", username, "status=disabled", success=True)
            users_logger.info(f"🚫 Disabled user by status: {username}")
            return True
        elif response.status_code == 404:
            users_logger.warning(f"User {username} not found")
            return False
            
    log_user_action("DISABLE", username, "Failed to disable user", success=False)
    return False


async def disable_user_by_group(panel_data: PanelType, username: str, disabled_group_id: int) -> bool:
    """
    Disable a user by moving them to the disabled group and setting status to disabled.
    Combines both operations into a single API request.
    Saves original groups to both JSON file and database for redundancy.

    Args:
        panel_data (PanelType): Panel connection data.
        username (str): The username to disable.
        disabled_group_id (int): The group ID to move user to.

    Returns:
        bool: True if successful, False otherwise.
    """
    users_logger.debug(f"🚫 Disabling user by group: {username} -> group {disabled_group_id}")
    try:
        user_data = await get_user_details(panel_data, username)
        if user_data is None:
            users_logger.error(f"User {username} not found")
            return False
        
        current_groups = user_data.get("group_ids", []) or []
        
        # IMPORTANT: Filter out the disabled_group_id from saved groups
        # This prevents saving the disabled group as an "original" group
        original_groups_to_save = [g for g in current_groups if g != disabled_group_id]
        
        users_logger.debug(f"👥 Saving current groups for {username}: {original_groups_to_save} (filtered from {current_groups})")
        
        # Save to JSON file (primary backup)
        groups_storage = UserGroupsStorage()
        await groups_storage.save_user_groups(username, original_groups_to_save)
        
        # Also save to database (secondary backup for redundancy)
        try:
            from db.database import get_db_session
            from db.crud import UserCRUD
            
            async with get_db_session() as db:
                user_record = await UserCRUD.get_by_username(db, username)
                if user_record:
                    user_record.original_groups = original_groups_to_save
                    user_record.is_disabled_by_limiter = True
                    await db.commit()
                    users_logger.debug(f"📦 Saved groups to database for {username}: {original_groups_to_save}")
        except Exception as db_error:
            users_logger.warning(f"Could not save groups to database (JSON backup exists): {db_error}")
        
        # Combined API call: set both group_ids and status in one request via panel_put
        from utils.panel_api.request_helper import panel_put
        payload = {"group_ids": [disabled_group_id], "status": "disabled"}
        
        response = await panel_put(
            panel_data,
            f"/api/user/{username}",
            json_data=payload,
            timeout=10.0,
            max_retries=3,
        )
        
        if response is not None and response.status_code in (200, 201):
            log_user_action("DISABLE", username, f"moved to group {disabled_group_id}, status disabled", success=True)
            users_logger.info(f"🚫 Disabled user by group: {username} (moved to group {disabled_group_id}, status disabled)")
            return True
            
        log_user_action("DISABLE", username, "Failed to move to disabled group", success=False)
        return False
    except Exception as error:
        users_logger.error(f"Error disabling user by group: {error}")
        log_user_action("DISABLE", username, str(error), success=False)
        return False


async def disable_user(panel_data: PanelType, username: UserType, duration_seconds: int = 0, permanent: bool = False) -> None | ValueError:
    """
    Disable a user on the panel.
    Uses either status-based or group-based disabling depending on config.

    Args:
        panel_data (PanelType): A PanelType object containing
        the username, password, and domain for the panel API.
        username (user): The username of the user to disable.
        duration_seconds (int): Optional custom disable duration in seconds.
        permanent (bool): If True, user will never be auto-enabled (manual only).

    Returns:
        None

    Raises:
        ValueError: If the function fails to disable the user.
    """
    users_logger.info(f"🚫 Disabling user: {username.name} (duration={duration_seconds}s, permanent={permanent})")
    
    user_exists = await check_user_exists(panel_data, username.name)
    if not user_exists:
        message = f"User {username.name} not found in panel (deleted?), skipping disable"
        users_logger.warning(message)
        await safe_send_logs_panel(message)
        return None
    
    data = await read_config()
    disable_method = data.get("disable_method", "status")
    disabled_group_id = data.get("disabled_group_id", None)
    
    users_logger.debug(f"Using disable method: {disable_method} (disabled_group_id={disabled_group_id})")
    
    success = False
    
    if disable_method == "group" and disabled_group_id is not None:
        success = await disable_user_by_group(panel_data, username.name, disabled_group_id)
        if success:
            message = f"Disabled user (moved to disabled group): {username.name}"
            await safe_send_logs_panel(message)
    else:
        success = await disable_user_by_status(panel_data, username.name)
        if success:
            message = f"Disabled user: {username.name}"
            await safe_send_logs_panel(message)
    
    if success:
        dis_obj = DisabledUsers()
        await dis_obj.add_user(username.name, duration_seconds, permanent=permanent)
        users_logger.info(f"🚫 User {username.name} added to disabled users list (permanent={permanent})")
        return None
    
    message = f"Failed to disable user: {username.name}"
    await safe_send_logs_panel(message)
    users_logger.error(message)
    raise ValueError(message)


async def disable_user_with_punishment(panel_data: PanelType, username: UserType) -> dict:
    """
    Disable a user using the smart punishment system.
    Applies escalating punishments based on violation history.

    Args:
        panel_data (PanelType): A PanelType object containing
        the username, password, and domain for the panel API.
        username (UserType): The username of the user to disable.

    Returns:
        dict: Result containing action, step_index, violation_count, duration_minutes, message
    """
    from utils.punishment_system import get_punishment_for_user, record_user_violation
    
    users_logger.info(f"⚖️ Processing punishment for user: {username.name}")
    
    user_exists = await check_user_exists(panel_data, username.name)
    if not user_exists:
        message = f"User {username.name} not found in panel (deleted?), skipping"
        users_logger.warning(message)
        return {
            "action": "skipped",
            "step_index": 0,
            "violation_count": 0,
            "duration_minutes": 0,
            "message": message
        }
    
    data = await read_config()
    punishment, step_index, violation_count = await get_punishment_for_user(username.name, data)
    
    users_logger.debug(f"⚖️ Punishment for {username.name}: step={step_index}, violations={violation_count}, type={punishment.step_type if punishment else 'none'}")
    
    punishment_enabled = data.get("punishment", {}).get("enabled", True)
    
    if not punishment_enabled:
        users_logger.debug(f"⚖️ Punishment system disabled, using simple disable for {username.name}")
        try:
            await disable_user(panel_data, username)
            return {
                "action": "disabled",
                "step_index": 0,
                "violation_count": 0,
                "duration_minutes": 0,
                "message": f"User {username.name} disabled (punishment system disabled)"
            }
        except ValueError as e:
            return {
                "action": "error",
                "step_index": 0,
                "violation_count": 0,
                "duration_minutes": 0,
                "message": str(e)
            }
    
    if punishment.is_warning():
        await record_user_violation(username.name, step_index, 0)
        message = (f"⚠️ Warning #{violation_count + 1} for {username.name}\n"
                   f"Next violation will result in: {punishment.get_display_text() if step_index + 1 >= len(data.get('punishment', {}).get('steps', [])) else 'disable'}")
        users_logger.info(f"⚠️ Warning issued to {username.name} (violation #{violation_count + 1})")
        return {
            "action": "warning",
            "step_index": step_index,
            "violation_count": violation_count + 1,
            "duration_minutes": 0,
            "message": message
        }
    
    # Handle revoke punishment type - revoke subscription and permanently disable
    if punishment.is_revoke():
        try:
            # First revoke the subscription (changes subscription URL)
            revoke_success = await revoke_user_subscription(panel_data, username.name)
            if revoke_success:
                users_logger.info(f"🔄 Revoked subscription for {username.name}")
            else:
                users_logger.warning(f"⚠️ Failed to revoke subscription for {username.name}")
            
            # Also reset the UUID directly (changes vless/vmess UUID)
            uuid_reset_success = await reset_user_uuid(panel_data, username.name)
            if uuid_reset_success:
                users_logger.info(f"🔑 Reset UUID for {username.name}")
            else:
                users_logger.warning(f"⚠️ Failed to reset UUID for {username.name}")
            
            # Then permanently disable the user
            await disable_user(panel_data, username, 0, permanent=True)
            await record_user_violation(username.name, step_index, 0)
            
            # Build status message
            status_parts = []
            if revoke_success:
                status_parts.append("✅ sub revoked")
            else:
                status_parts.append("⚠️ sub revoke failed")
            if uuid_reset_success:
                status_parts.append("✅ UUID reset")
            else:
                status_parts.append("⚠️ UUID reset failed")
            
            revoke_note = ", ".join(status_parts)
            message = f"🔄 User {username.name} subscription revoked + UUID reset + disabled permanently ({revoke_note}) (violation #{violation_count + 1})"
            users_logger.info(f"🔄 Revoke + UUID reset + permanent disable for {username.name} (violation #{violation_count + 1})")
            
            return {
                "action": "revoked",
                "step_index": step_index,
                "violation_count": violation_count + 1,
                "duration_minutes": 0,
                "revoke_success": revoke_success,
                "uuid_reset_success": uuid_reset_success,
                "message": message
            }
        except ValueError as e:
            users_logger.error(f"⚖️ Revoke punishment failed for {username.name}: {e}")
            return {
                "action": "error",
                "step_index": step_index,
                "violation_count": violation_count,
                "duration_minutes": 0,
                "message": str(e)
            }
    
    duration_seconds = punishment.get_duration_seconds()
    is_permanent = punishment.is_unlimited_disable()
    
    try:
        await disable_user(panel_data, username, duration_seconds, permanent=is_permanent)
        await record_user_violation(username.name, step_index, punishment.duration_minutes)
        
        if is_permanent:
            message = f"🚫 User {username.name} disabled permanently (violation #{violation_count + 1})"
            users_logger.info(f"🚫 Permanent disable for {username.name} (violation #{violation_count + 1})")
        else:
            message = f"🔒 User {username.name} disabled for {punishment.duration_minutes} minutes (violation #{violation_count + 1})"
            users_logger.info(f"🔒 Timed disable for {username.name}: {punishment.duration_minutes}min (violation #{violation_count + 1})")
        
        return {
            "action": "disabled",
            "step_index": step_index,
            "violation_count": violation_count + 1,
            "duration_minutes": punishment.duration_minutes,
            "message": message
        }
    except ValueError as e:
        users_logger.error(f"⚖️ Punishment failed for {username.name}: {e}")
        return {
            "action": "error",
            "step_index": step_index,
            "violation_count": violation_count,
            "duration_minutes": 0,
            "message": str(e)
        }


async def enable_dis_user(panel_data: PanelType):
    """
    Enable disabled users individually based on when each was disabled.
    Each user is enabled after 'time_to_active_users' seconds from their disable time.
    Handles partial success - removes only successfully enabled users from disabled list.
    Also removes users that were deleted from the panel to stop retry loops.
    Waits for panel to be available during restarts.
    """
    from utils.panel_api.request_helper import is_panel_available, wait_for_panel
    
    users_logger.info("🔄 Starting disabled user enable loop...")
    while True:
        await asyncio.sleep(30)
        
        try:
            # Check if panel is available, wait if not
            if not is_panel_available():
                users_logger.warning("⏳ Panel unavailable, waiting for it to come back...")
                if not await wait_for_panel(panel_data):
                    users_logger.error("❌ Panel still unavailable after waiting, skipping this cycle")
                    continue
            
            data = await read_config()
            time_to_active = data.get("monitoring", {}).get("time_to_active_users", 1800)
            
            dis_obj = DisabledUsers()
            users_to_enable = await dis_obj.get_users_to_enable(time_to_active)
            
            if users_to_enable:
                users_logger.info(f"✅ Enabling {len(users_to_enable)} users: {users_to_enable}")
                result = await enable_selected_users(panel_data, set(users_to_enable))
                
                # Remove successfully enabled users from disabled list
                enabled = result.get("enabled", [])
                failed = result.get("failed", [])
                not_found = result.get("not_found", [])
                
                for username in enabled:
                    await dis_obj.remove_user(username)
                    users_logger.info(f"✅ User {username} has been re-enabled")
                    # Delete disable message and send enable notification
                    try:
                        from telegram_bot.send_message import send_enable_notification
                        await send_enable_notification(username, delete_disable_msg=True)
                    except Exception as notify_error:
                        users_logger.warning(f"Could not send enable notification for {username}: {notify_error}")
                
                # Remove users that were deleted from panel (404) to stop retry loops
                for username in not_found:
                    await dis_obj.remove_user(username)
                    users_logger.info(f"🗑️ User {username} was deleted from panel, removed from disabled list")
                    # Also delete the disable message for deleted users
                    try:
                        from telegram_bot.send_message import delete_disable_message_for_user
                        await delete_disable_message_for_user(username)
                    except Exception:
                        pass
                
                # Log failed users but don't remove them - they will be retried next cycle
                if failed:
                    users_logger.warning(f"⚠️ Failed to enable {len(failed)} users (will retry): {failed}")
        except Exception as e:
            users_logger.error(f"Error in enable_dis_user loop: {e}")


async def cleanup_deleted_users(panel_data: PanelType) -> dict:
    """
    DISABLED: This function is currently disabled as it was incorrectly removing valid users.
    
    The function was checking against old JSON config format while special limits
    are now stored in the database, causing it to incorrectly identify users
    as "deleted" when they actually exist.
    
    Use the Telegram bot's "Review Pending Deletions" feature instead:
    Settings -> User Sync Settings -> Review Pending Deletions
    
    Args:
        panel_data (PanelType): A PanelType object containing
        the username, password, and domain for the panel API.

    Returns:
        dict: Empty result as function is disabled.
    """
    users_logger.warning("⚠️ cleanup_deleted_users is DISABLED - use Telegram bot instead")
    users_logger.warning("📱 Telegram: Settings -> User Sync -> Review Pending Deletions")
    return {
        "special_limits_removed": [],
        "except_users_removed": [],
        "disabled_users_removed": [],
        "user_groups_backup_removed": [],
        "error": "Function disabled - was removing valid users. Use Telegram bot instead."
    }


async def fix_stuck_disabled_users(panel_data: PanelType) -> dict:
    """
    Find and fix users who are stuck in the disabled group.
    
    This handles cases where users are in the disabled group but:
    - Have "active" status (should be disabled or restored)
    - Were not properly tracked in the disabled users list
    - Have their previous groups saved but not restored
    
    Args:
        panel_data (PanelType): Panel connection data.
        
    Returns:
        dict: Results with 'fixed', 'failed', and 'not_in_disabled_group' lists.
    """
    users_logger.info("🔍 Scanning for users stuck in disabled group...")
    
    data = await read_config()
    disabled_group_id = data.get("disabled_group_id", None)
    
    if disabled_group_id is None:
        users_logger.warning("⚠️ No disabled_group_id configured, cannot scan for stuck users")
        return {
            "fixed": [],
            "failed": [],
            "found_in_disabled_group": [],
            "error": "disabled_group_id not configured"
        }
    
    # Get all users from panel
    all_users = await all_user(panel_data)
    if isinstance(all_users, ValueError):
        users_logger.error(f"Failed to get users: {all_users}")
        return {
            "fixed": [],
            "failed": [],
            "found_in_disabled_group": [],
            "error": str(all_users)
        }
    
    # Find users in the disabled group
    stuck_users = []
    for user in all_users:
        user_groups = getattr(user, 'group_ids', []) or []
        if disabled_group_id in user_groups:
            stuck_users.append(user)
    
    users_logger.info(f"📋 Found {len(stuck_users)} users in disabled group {disabled_group_id}")
    
    if not stuck_users:
        return {
            "fixed": [],
            "failed": [],
            "found_in_disabled_group": [],
            "message": "No users found in disabled group"
        }
    
    fixed_users = []
    failed_users = []
    
    for user in stuck_users:
        username = user.name
        users_logger.info(f"🔧 Fixing stuck user: {username}")
        
        try:
            # Try to enable the user (this will restore groups if available)
            success, not_found = await enable_user_by_group(panel_data, username)
            
            if not_found:
                # User was deleted from panel
                dis_obj = DisabledUsers()
                await dis_obj.remove_user(username)
                users_logger.info(f"🗑️ User {username} was deleted from panel")
                continue
            
            if success:
                # Also remove from disabled users tracking if present
                dis_obj = DisabledUsers()
                await dis_obj.remove_user(username)
                
                fixed_users.append(username)
                users_logger.info(f"✅ Fixed stuck user: {username}")
            else:
                failed_users.append(username)
                users_logger.error(f"❌ Failed to fix stuck user: {username}")
        except Exception as e:
            failed_users.append(username)
            users_logger.error(f"❌ Error fixing stuck user {username}: {e}")
    
    users_logger.info(f"✅ Fix complete: {len(fixed_users)} fixed, {len(failed_users)} failed")
    
    return {
        "fixed": fixed_users,
        "failed": failed_users,
        "found_in_disabled_group": [u.name for u in stuck_users],
        "disabled_group_id": disabled_group_id
    }


async def get_users_in_disabled_group(panel_data: PanelType) -> list[str]:
    """
    Get list of usernames that are currently in the disabled group.
    
    Args:
        panel_data (PanelType): Panel connection data.
        
    Returns:
        list[str]: List of usernames in disabled group.
    """
    data = await read_config()
    disabled_group_id = data.get("disabled_group_id", None)
    
    if disabled_group_id is None:
        users_logger.warning("⚠️ No disabled_group_id configured")
        return []
    
    all_users = await all_user(panel_data)
    if isinstance(all_users, ValueError):
        users_logger.error(f"Failed to get users: {all_users}")
        return []
    
    stuck_users = []
    for user in all_users:
        user_groups = getattr(user, 'group_ids', []) or []
        if disabled_group_id in user_groups:
            stuck_users.append(user.name)
    
    return stuck_users
