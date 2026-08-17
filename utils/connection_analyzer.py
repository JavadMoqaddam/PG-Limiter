"""
Connection analyzer utility for tracking IP-Node-Inbound relationships.
Provides pure synchronous analytics functions and async facades for Telegram bot reports.
"""

from typing import Dict, List, Tuple
from utils.types import UserType
from utils.shared_state import get_active_users_snapshot


# ---------------------------------------------------------------------------
# Pure Synchronous Analytics Functions (CPU-bound, no implicit global state)
# ---------------------------------------------------------------------------

def analyze_connections(active_users: Dict[str, UserType]) -> str:
    """
    Pure synchronous generator of IP-Node-Inbound connection report.

    Args:
        active_users: Mapping of usernames to UserType data.

    Returns:
        str: Formatted human-readable connection analysis report.
    """
    if not active_users:
        return "No active user connections found."

    report_lines = ["=== CONNECTION ANALYSIS REPORT ===\n"]

    for username, user in active_users.items():
        if not user or not user.device_info:
            continue
        report_lines.append(f"User: {username}")
        report_lines.append(f"Total IPs: {len(user.device_info.unique_ips)}")
        report_lines.append(f"Total Nodes: {len(user.device_info.unique_nodes)}")
        report_lines.append(f"Inbound Protocols: {', '.join(user.device_info.inbound_protocols)}")
        report_lines.append(f"Multi-device: {'Yes' if user.device_info.is_multi_device else 'No'}")
        report_lines.append("")

        if user.device_info.connections:
            report_lines.append("  Connections:")
            for conn in user.device_info.connections:
                report_lines.append(
                    f"    IP: {conn.ip} | Node: {conn.node_name} (ID: {conn.node_id}) | "
                    f"Protocol: {conn.inbound_protocol} | Count: {conn.connection_count}"
                )

        report_lines.append("-" * 60)

    return "\n".join(report_lines)


def filter_users_by_node(active_users: Dict[str, UserType], node_id: int) -> List[Tuple[str, str, str]]:
    """
    Pure synchronous filter returning users connected to a specific node.

    Args:
        active_users: Mapping of usernames to UserType data.
        node_id: Target node identifier.

    Returns:
        List[Tuple[str, str, str]]: List of (username, ip, inbound_protocol) tuples.
    """
    users_on_node = []
    for username, user in active_users.items():
        if not user or not user.device_info:
            continue
        for conn in user.device_info.connections:
            if conn.node_id == node_id:
                users_on_node.append((username, conn.ip, conn.inbound_protocol))
    return users_on_node


def filter_users_by_protocol(active_users: Dict[str, UserType], protocol: str) -> List[Tuple[str, str, str]]:
    """
    Pure synchronous filter returning users using a specific inbound protocol.

    Args:
        active_users: Mapping of usernames to UserType data.
        protocol: Target inbound protocol name.

    Returns:
        List[Tuple[str, str, str]]: List of (username, ip, node_name) tuples.
    """
    users_with_protocol = []
    for username, user in active_users.items():
        if not user or not user.device_info:
            continue
        for conn in user.device_info.connections:
            if conn.inbound_protocol == protocol:
                users_with_protocol.append((username, conn.ip, conn.node_name))
    return users_with_protocol


def extract_multi_device_users(active_users: Dict[str, UserType]) -> List[Tuple[str, int, int, List[str]]]:
    """
    Pure synchronous extractor returning all users identified as multi-device.

    Args:
        active_users: Mapping of usernames to UserType data.

    Returns:
        List[Tuple[str, int, int, List[str]]]: List of (username, ip_count, node_count, protocols) tuples.
    """
    multi_device_users = []
    for username, user in active_users.items():
        if not user or not user.device_info:
            continue
        if user.device_info.is_multi_device:
            multi_device_users.append((
                username,
                len(user.device_info.unique_ips),
                len(user.device_info.unique_nodes),
                list(user.device_info.inbound_protocols)
            ))
    return multi_device_users


def summarize_node_usage(active_users: Dict[str, UserType]) -> Dict[str, Dict[str, int]]:
    """
    Pure synchronous aggregator summarizing node usage statistics.

    Args:
        active_users: Mapping of usernames to UserType data.

    Returns:
        Dict[str, Dict[str, int]]: Aggregated statistics per node.
    """
    node_stats = {}
    for username, user in active_users.items():
        if not user or not user.device_info:
            continue
        for conn in user.device_info.connections:
            node_key = f"{conn.node_name} (ID: {conn.node_id})"
            if node_key not in node_stats:
                node_stats[node_key] = {
                    "unique_users": set(),
                    "unique_ips": set(),
                    "protocols": set(),
                    "total_connections": 0
                }
            node_stats[node_key]["unique_users"].add(username)
            node_stats[node_key]["unique_ips"].add(conn.ip)
            node_stats[node_key]["protocols"].add(conn.inbound_protocol)
            node_stats[node_key]["total_connections"] += conn.connection_count

    # Convert sets to counts
    for node_key in node_stats:
        node_stats[node_key]["unique_users"] = len(node_stats[node_key]["unique_users"])
        node_stats[node_key]["unique_ips"] = len(node_stats[node_key]["unique_ips"])
        node_stats[node_key]["protocols"] = len(node_stats[node_key]["protocols"])

    return node_stats


# ---------------------------------------------------------------------------
# Async Facade Functions (For Telegram bot handlers and external callers)
# ---------------------------------------------------------------------------

async def generate_connection_report(active_users: Dict[str, UserType] | None = None) -> str:
    """Generate comprehensive connection report, resolving active users snapshot if needed."""
    if active_users is None:
        active_users = await get_active_users_snapshot()
    return analyze_connections(active_users)


async def get_users_by_node(node_id: int, active_users: Dict[str, UserType] | None = None) -> List[Tuple[str, str, str]]:
    """Get all users connected to a node, resolving active users snapshot if needed."""
    if active_users is None:
        active_users = await get_active_users_snapshot()
    return filter_users_by_node(active_users, node_id)


async def get_users_by_inbound_protocol(protocol: str, active_users: Dict[str, UserType] | None = None) -> List[Tuple[str, str, str]]:
    """Get all users using an inbound protocol, resolving active users snapshot if needed."""
    if active_users is None:
        active_users = await get_active_users_snapshot()
    return filter_users_by_protocol(active_users, protocol)


async def get_multi_device_users(active_users: Dict[str, UserType] | None = None) -> List[Tuple[str, int, int, List[str]]]:
    """Get multi-device users, resolving active users snapshot if needed."""
    if active_users is None:
        active_users = await get_active_users_snapshot()
    return extract_multi_device_users(active_users)


async def get_node_usage_summary(active_users: Dict[str, UserType] | None = None) -> Dict[str, Dict[str, int]]:
    """Get node usage statistics summary, resolving active users snapshot if needed."""
    if active_users is None:
        active_users = await get_active_users_snapshot()
    return summarize_node_usage(active_users)


async def generate_node_usage_report(active_users: Dict[str, UserType] | None = None) -> str:
    """Generate human-readable node usage report, resolving active users snapshot if needed."""
    node_stats = await get_node_usage_summary(active_users)
    if not node_stats:
        return "No node usage data available."

    report_lines = ["=== NODE USAGE REPORT ===\n"]
    for node_key, stats in node_stats.items():
        report_lines.append(f"Node: {node_key}")
        report_lines.append(f"  Unique Users: {stats['unique_users']}")
        report_lines.append(f"  Unique IPs: {stats['unique_ips']}")
        report_lines.append(f"  Protocol Types: {stats['protocols']}")
        report_lines.append(f"  Total Connections: {stats['total_connections']}")
        report_lines.append("")

    return "\n".join(report_lines)
