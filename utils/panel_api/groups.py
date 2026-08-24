import time
from utils.logs import get_logger
from utils.types import PanelType
from utils.panel_api.request_helper import panel_get

# Module logger
groups_logger = get_logger("panel_api.groups")

# Fallback in-memory groups cache (10 minutes TTL)
_groups_cache = {
    "groups": None,
    "expires_at": 0.0,
    "panel_domain": None,
}


async def invalidate_groups_cache():
    """Invalidate the cached groups list."""
    _groups_cache["groups"] = None
    _groups_cache["expires_at"] = 0.0
    groups_logger.debug("👥 Groups cache invalidated")


async def get_groups(panel_data: PanelType, force_refresh: bool = False) -> list[dict] | ValueError:
    """
    Get all groups from the panel API with in-memory caching (10 minutes).

    Args:
        panel_data (PanelType): A PanelType object containing
        the username, password, and domain for the panel API.
        force_refresh (bool): If True, bypass cache and fetch fresh groups.

    Returns:
        list[dict]: The list of groups with id, name, and other info.

    Raises:
        ValueError: If the function fails to get groups from the API.
    """
    current_time = time.time()
    if (not force_refresh and 
        _groups_cache["groups"] is not None and 
        _groups_cache["panel_domain"] == panel_data.panel_domain and
        current_time < _groups_cache["expires_at"]):
        groups_logger.debug("👥 Using cached groups list")
        return _groups_cache["groups"]

    groups_logger.debug(f"👥 Fetching groups from panel (force_refresh={force_refresh})...")
    
    response = await panel_get(panel_data, "/api/groups", force_refresh=force_refresh)
    
    if response is None:
        message = "Failed to get groups after all retries"
        groups_logger.error(message)
        raise ValueError(message)
    
    try:
        data = response.json()
    except Exception as json_error:
        groups_logger.error(f"Failed to parse JSON: {json_error}")
        raise ValueError(f"Failed to parse groups response: {json_error}")
    
    # Handle response structure
    groups = None
    if isinstance(data, dict) and "groups" in data:
        groups = data["groups"]
    elif isinstance(data, list):
        groups = data
    else:
        message = f"Unexpected groups response format: {type(data)}"
        groups_logger.error(message)
        raise ValueError(message)
    
    # Cache result for 10 minutes (600s)
    _groups_cache["groups"] = groups
    _groups_cache["expires_at"] = current_time + 600.0
    _groups_cache["panel_domain"] = panel_data.panel_domain

    groups_logger.info(f"👥 Fetched {len(groups)} groups")
    for group in groups:
        groups_logger.debug(f"  └─ {group.get('name', 'Unknown')} (id={group.get('id', '?')})")
    return groups
