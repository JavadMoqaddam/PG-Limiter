"""
Node operations for panel API.
"""

import time

from utils.logs import get_logger
from utils.types import PanelType, NodeType
from utils.panel_api.auth import safe_send_logs_panel
from utils.panel_api.request_helper import panel_get

# Module logger
nodes_logger = get_logger("panel_api.nodes")

# In-process nodes cache (1 hour). Keyed on the panel AND on enabled_only: the two are
# different result sets, and storing them under one key meant a caller asking for the
# full fleet overwrote the entry that enabled-only callers then read - silently handing
# them disabled nodes, and vice versa.
_nodes_cache = {
    "nodes": None,
    "expires_at": 0,
    "panel_domain": None,
    "enabled_only": None,
}


async def invalidate_nodes_cache():
    """Invalidate the cached nodes list"""
    _nodes_cache["nodes"] = None
    _nodes_cache["expires_at"] = 0
    _nodes_cache["enabled_only"] = None
    nodes_logger.info("🖥️ Nodes cache invalidated")


async def get_nodes(
    panel_data: PanelType,
    force_refresh: bool = False,
    enabled_only: bool = True,
) -> list[NodeType] | ValueError:
    """
    Get the IDs of all nodes from the panel API.
    Results are cached in-process for 1 hour.

    Args:
        panel_data (PanelType): A PanelType object containing
            the username, password, and domain for the panel API.
        force_refresh (bool): If True, bypass cache and fetch fresh nodes.
        enabled_only (bool): If True (default), only fetch enabled nodes.

    Returns:
        list[NodeType]: The list of IDs and other information of all nodes.

    Raises:
        ValueError: If the function fails to get the nodes from both the HTTP
        and HTTPS endpoints.
    """
    if not force_refresh and _nodes_cache["nodes"] is not None:
        current_time = time.time()
        # Check if cache is still valid, for the same panel, and for the same scope
        if (_nodes_cache["expires_at"] > current_time and
                _nodes_cache["panel_domain"] == panel_data.panel_domain and
                _nodes_cache["enabled_only"] == enabled_only):
            time_left = int(_nodes_cache["expires_at"] - current_time)
            nodes_logger.debug(f"🖥️ Using in-memory cached nodes list (expires in {time_left // 60} minutes)")
            return _nodes_cache["nodes"]
    
    nodes_logger.info(f"🖥️ Fetching nodes from panel (force_refresh={force_refresh})...")
    
    # Build endpoint with optional enabled filter
    endpoint = "/api/nodes?enabled=true" if enabled_only else "/api/nodes"
    
    response = await panel_get(panel_data, endpoint, force_refresh=force_refresh)
    
    if response is None:
        message = (
            "Failed to get nodes after all retries. Make sure the panel is running "
            "and the username and password are correct."
        )
        await safe_send_logs_panel(message)
        nodes_logger.error(message)
        raise ValueError(message)
    
    try:
        user_inform = response.json()
    except Exception as json_error:
        nodes_logger.error(f"Failed to parse JSON: {json_error}")
        raise ValueError(f"Failed to parse nodes response: {json_error}")
    
    # Handle both list and dict responses
    nodes_list = None
    if isinstance(user_inform, list):
        nodes_list = user_inform
    elif isinstance(user_inform, dict):
        if "nodes" in user_inform:
            nodes_list = user_inform["nodes"]
        elif "data" in user_inform:
            nodes_list = user_inform["data"]
        else:
            nodes_logger.warning(f"Unexpected nodes dict structure. Keys: {list(user_inform.keys())}")
            if "id" in user_inform and "name" in user_inform:
                nodes_list = [user_inform]
            else:
                message = f"Cannot parse nodes from dict: {user_inform}"
                nodes_logger.error(message)
                raise ValueError(message)
    else:
        message = f"Nodes response is neither list nor dict: {type(user_inform)}"
        nodes_logger.error(message)
        raise ValueError(message)
    
    if not nodes_list or not isinstance(nodes_list, list):
        message = "Failed to extract nodes list from response"
        nodes_logger.error(message)
        raise ValueError(message)
    
    all_nodes = []
    for node in nodes_list:
        all_nodes.append(
            NodeType(
                node_id=node["id"],
                node_name=node["name"],
                node_ip=node["address"],
                status=node["status"],
                message=node.get("message", ""),
            )
        )
    
    # Cache the nodes list for 1 hour (3600 seconds)
    _nodes_cache["nodes"] = all_nodes
    _nodes_cache["expires_at"] = time.time() + 3600
    _nodes_cache["panel_domain"] = panel_data.panel_domain
    _nodes_cache["enabled_only"] = enabled_only
    
    nodes_logger.info(f"🖥️ Fetched {len(all_nodes)} nodes (cached for 1 hour)")
    for node in all_nodes:
        nodes_logger.debug(f"  └─ {node.node_name} (id={node.node_id}, status={node.status})")
    
    return all_nodes
