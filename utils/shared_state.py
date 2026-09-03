"""
Shared in-memory state and synchronization primitives across PG-Limiter modules.
Decouples state ownership to eliminate circular dependencies between check_usage and parse_logs.
"""

import asyncio
import time

from utils.types import UserType

# Global state of currently active connected users
ACTIVE_USERS: dict[str, UserType] = {}

# Module-level lock protecting concurrent access to ACTIVE_USERS
ACTIVE_USERS_LOCK = asyncio.Lock()

# Per-node "last event seen" wall clock, {node_id: timestamp}.
#
# ACTIVE_USERS on its own cannot tell a dead pipeline from a quiet one: an empty
# map reads identically for "nobody is connected" and "every SSE stream is
# half-open". get_logs connects with ``timeout=None``, so a stalled stream never
# raises - the node keeps reporting "✅ Connected" while delivering nothing, and
# the cycle that follows clears the consecutive-violation counter of every absent
# user, so a genuine offender never reaches the third scan.
#
# No lock. This is written once per log line, and assigning a float into a dict is
# atomic under the GIL; taking a lock per line would cost more than it protects,
# and making writers wait on ACTIVE_USERS_LOCK would serialise ingestion behind
# snapshotting. Readers copy with list() so they never iterate a mutating dict.
NODE_LAST_EVENT: dict[int, float] = {}

# How many check intervals of total silence mean a log stream is broken rather than
# quiet. Every line the panel sends counts as a heartbeat, keep-alives included, so a
# healthy node with no user traffic still reports in - silence past this window is a
# dead stream, not an idle one. Three intervals also matches the warning system's own
# monitoring window (check_interval x max_warnings at the default max of 3), so a node
# cannot be declared silent for less than the time a user needs to earn a ban.
NODE_SILENCE_INTERVALS = 3


def node_silence_window(check_interval: float) -> float:
    """The staleness window, in seconds, for deciding a node's stream has gone quiet."""
    return max(120.0, float(check_interval) * NODE_SILENCE_INTERVALS)


def note_node_event(node_id: int | None, when: float | None = None) -> None:
    """Record that ``node_id`` produced a log event."""
    if node_id is None:
        return
    NODE_LAST_EVENT[node_id] = time.time() if when is None else when


def forget_node_event(node_id: int | None) -> None:
    """Drop a node's heartbeat when its task is cancelled or the node disappears."""
    if node_id is None:
        return
    NODE_LAST_EVENT.pop(node_id, None)


def clear_node_events() -> None:
    """Forget every heartbeat, for the periodic full rebuild of the node tasks."""
    NODE_LAST_EVENT.clear()


def nodes_seen_within(max_age: float) -> int:
    """How many nodes produced an event within the last ``max_age`` seconds."""
    cutoff = time.time() - max_age
    return sum(1 for seen in list(NODE_LAST_EVENT.values()) if seen >= cutoff)


def tracked_node_count() -> int:
    """How many nodes are currently being tracked at all."""
    return len(NODE_LAST_EVENT)


def get_node_event_ages() -> dict[int, float]:
    """``{node_id: seconds since its last event}``, for diagnostics and reports."""
    now = time.time()
    return {node_id: now - seen for node_id, seen in list(NODE_LAST_EVENT.items())}


def _clone_user_map(users: dict[str, UserType]) -> dict[str, UserType]:
    """Clone mapping of active users to guarantee point-in-time isolation."""
    snapshot = {}
    for email, user in users.items():
        if not email or not user:
            continue
        snapshot[email] = UserType(
            name=user.name,
            status=user.status,
            ip=list(user.ip) if hasattr(user, "ip") and user.ip else [],
            isp_info=user.isp_info,
            device_info=user.device_info,
            panel_status=user.panel_status,
            data_limit=user.data_limit,
            used_traffic=user.used_traffic,
            lifetime_used_traffic=user.lifetime_used_traffic,
            expire=user.expire,
            group_ids=user.group_ids,
            online_at=user.online_at,
            admin_username=user.admin_username,
            is_monitored=getattr(user, "is_monitored", True),
            effective_ip_limit=getattr(user, "effective_ip_limit", None),
        )
    return snapshot


async def get_active_users_snapshot() -> dict[str, UserType]:
    """
    Take an atomic point-in-time snapshot of ACTIVE_USERS with cloned IP lists.
    Guarantees callers have an isolated, thread-safe view without blocking ongoing log streaming.
    """
    async with ACTIVE_USERS_LOCK:
        return _clone_user_map(ACTIVE_USERS)


async def pop_active_users_snapshot() -> dict[str, UserType]:
    """
    Atomically take an isolated point-in-time snapshot of ACTIVE_USERS and clear the collection.
    Guarantees that new connections arriving during cycle analysis are preserved for the next cycle
    and never orphaned or dropped by a delayed clear.
    """
    async with ACTIVE_USERS_LOCK:
        snapshot = _clone_user_map(ACTIVE_USERS)
        ACTIVE_USERS.clear()
        return snapshot
