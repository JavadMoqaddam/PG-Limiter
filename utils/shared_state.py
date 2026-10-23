"""
Shared in-memory state and synchronization primitives across PG-Limiter modules.
Decouples state ownership to eliminate circular dependencies between check_usage and parse_logs.
"""

import asyncio
import copy
import time
from dataclasses import dataclass

from utils.types import UserType

# Global state of currently active connected users
ACTIVE_USERS: dict[str, UserType] = {}

# Module-level lock protecting concurrent access to ACTIVE_USERS
ACTIVE_USERS_LOCK = asyncio.Lock()


@dataclass(frozen=True)
class APISampleEvidence:
    """The settings and unknown users belonging to one completed API collection."""

    generation: int
    config_revision: int
    freshness_seconds: int
    unknown_users: frozenset[str] = frozenset()


class ActiveUsersSnapshot(dict[str, UserType]):
    """A normal user mapping with evidence attached to API observations only."""

    api_evidence: APISampleEvidence | None = None
    unknown_users: frozenset[str] = frozenset()


_api_generation = 0
_api_snapshot: ActiveUsersSnapshot | None = None


def begin_api_collection() -> int:
    """Invalidate the previous API sample before beginning another collection."""
    global _api_generation, _api_snapshot
    _api_generation += 1
    if _api_snapshot is not None:
        ACTIVE_USERS.clear()
    _api_snapshot = None
    return _api_generation


async def discard_source_observations() -> None:
    """Discard previous-source samples without touching user warning history."""
    global _api_generation, _api_snapshot
    async with ACTIVE_USERS_LOCK:
        _api_generation += 1
        _api_snapshot = None
        ACTIVE_USERS.clear()


def api_evidence_is_current(evidence: APISampleEvidence) -> bool:
    """A newer collection or configuration write invalidates an older sample."""
    from utils.read_config import get_config_revision

    return evidence.generation == _api_generation and evidence.config_revision == get_config_revision()


def refresh_api_user(user: UserType, freshness_seconds: int) -> bool:
    """Evaluate source timestamps locally, including after asynchronous preparation."""
    evaluated_at = time.time()
    connections = user.device_info.connections
    if any(connection.last_seen > evaluated_at for connection in connections):
        return False
    connections = [
        connection for connection in connections if 0 <= evaluated_at - connection.last_seen < freshness_seconds
    ]
    user.device_info.connections = connections
    user.ip = sorted({connection.ip for connection in connections})
    user.device_info.unique_ips = set(user.ip)
    user.device_info.unique_nodes = {connection.node_id for connection in connections}
    user.device_info.inbound_protocols = {connection.inbound_protocol for connection in connections}
    user.device_info.is_multi_device = len(user.ip) > 1
    return True


async def publish_api_snapshot(users: dict[str, UserType], evidence: APISampleEvidence) -> bool:
    """Publish only a current sample, isolated from log tasks winding down."""
    global _api_snapshot
    async with ACTIVE_USERS_LOCK:
        if not api_evidence_is_current(evidence):
            return False
        snapshot = _clone_user_map(users)
        snapshot.api_evidence = evidence
        _api_snapshot = snapshot
        ACTIVE_USERS.clear()
        # Log readers still see the ordinary public mapping. Its objects must not
        # alias the enforcement sample: parked SSE tasks can finish a pending read.
        ACTIVE_USERS.update(_clone_user_map(users))
        return True


async def pop_api_users_snapshot() -> ActiveUsersSnapshot | None:
    """Consume API evidence once; an old or failed cycle cannot be reused."""
    global _api_snapshot
    async with ACTIVE_USERS_LOCK:
        snapshot = _api_snapshot
        _api_snapshot = None
        ACTIVE_USERS.clear()
        if snapshot is None or not api_evidence_is_current(snapshot.api_evidence):
            return None
        return snapshot


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


def _clone_user_map(users: dict[str, UserType]) -> ActiveUsersSnapshot:
    """Clone mapping of active users to guarantee point-in-time isolation."""
    snapshot = ActiveUsersSnapshot()
    snapshot.api_evidence = getattr(users, "api_evidence", None)
    snapshot.unknown_users = getattr(users, "unknown_users", frozenset())
    for email, user in users.items():
        if not email or not user:
            continue
        snapshot[email] = UserType(
            name=user.name,
            status=user.status,
            ip=list(user.ip) if hasattr(user, "ip") and user.ip else [],
            # Deep-clone the nested state too: a shallow copy aliased isp_info,
            # device_info and group_ids, so ongoing ingestion into the live map
            # mutated a report already reading the snapshot.
            isp_info=copy.deepcopy(user.isp_info),
            device_info=copy.deepcopy(user.device_info),
            panel_status=user.panel_status,
            data_limit=user.data_limit,
            used_traffic=user.used_traffic,
            lifetime_used_traffic=user.lifetime_used_traffic,
            expire=user.expire,
            group_ids=copy.deepcopy(user.group_ids),
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
        if _api_snapshot is not None:
            if not api_evidence_is_current(_api_snapshot.api_evidence):
                return ActiveUsersSnapshot()
            return _clone_user_map(_api_snapshot)
        return _clone_user_map(ACTIVE_USERS)


async def pop_active_users_snapshot() -> dict[str, UserType]:
    """
    Atomically take an isolated point-in-time snapshot of ACTIVE_USERS and clear the collection.
    Guarantees that new connections arriving during cycle analysis are preserved for the next cycle
    and never orphaned or dropped by a delayed clear.
    """
    global _api_snapshot
    async with ACTIVE_USERS_LOCK:
        if _api_snapshot is not None:
            _api_snapshot = None
            ACTIVE_USERS.clear()
            return ActiveUsersSnapshot()
        snapshot = _clone_user_map(ACTIVE_USERS)
        ACTIVE_USERS.clear()
        return snapshot


async def merge_active_users_snapshot(batch: dict[str, UserType]) -> int:
    """Requeue a consumed batch when the cycle failed before it could be evaluated.

    A ``pop_active_users_snapshot()`` clears ACTIVE_USERS, so a failure later in the
    cycle would lose the batch: the next cycle would see those still-connected users as
    absent and clear their warnings. This restores each user from the batch, but merges
    by connection identity - a newer event that arrived during the cycle is kept, never
    overwritten by the stale sample.

    Returns the number of users actually restored (those without a newer observation).
    """
    if not batch or getattr(batch, "api_evidence", None) is not None:
        # API observations belong to exactly one cycle; only Logs are requeued.
        return 0
    restored = 0
    async with ACTIVE_USERS_LOCK:
        for email, user in batch.items():
            if not email or not user:
                continue
            if email in ACTIVE_USERS:
                # A newer observation arrived during the cycle; do not overwrite it.
                continue
            ACTIVE_USERS[email] = user
            restored += 1
    return restored
