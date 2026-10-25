"""
Shared in-memory state and synchronization primitives across PG-Limiter modules.
Decouples state ownership to eliminate circular dependencies between check_usage and parse_logs.
"""

import asyncio
import copy
import math
import time
from dataclasses import dataclass

from utils.types import UserType
from utils.log_observations import LOG_OBSERVATIONS, LogSampleEvidence

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
    eligible_node_ids: frozenset[int] | None = None


class ActiveUsersSnapshot(dict[str, UserType]):
    """A normal user mapping with source-specific collection evidence attached."""

    api_evidence: APISampleEvidence | None = None
    unknown_users: frozenset[str] = frozenset()
    log_evidence: LogSampleEvidence | None = None


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
        LOG_OBSERVATIONS.invalidate()
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


def refresh_log_user(user: UserType, evidence: LogSampleEvidence | None) -> bool:
    """Recount original-time observations using a current, frozen Logs scope.

    Valid expired observations are known empty evidence. A retired witness or
    an invalid/future source timestamp cannot authorize a warning or clearing.
    """
    if not LOG_OBSERVATIONS.is_current(evidence, allow_partial=True):
        return False
    evaluated_at = time.time()
    if evaluated_at < evidence.interval_end:
        return False
    eligible = evidence.eligible_node_ids or frozenset()
    versions = dict(evidence.node_versions)
    connections = [
        connection
        for connection in user.device_info.connections
        if type(connection.node_id) is int
        and connection.node_id in eligible
        and LOG_OBSERVATIONS.monitors(connection.node_id)
        and LOG_OBSERVATIONS.node_epoch(connection.node_id) == versions.get(connection.node_id)
    ]
    if any(
        not isinstance(connection.last_seen, (int, float))
        or not math.isfinite(connection.last_seen)
        or connection.last_seen > evaluated_at
        for connection in connections
    ):
        return False
    connections = [
        connection
        for connection in connections
        if connection.last_seen <= evidence.interval_end
        and 0 <= evaluated_at - connection.last_seen < evidence.freshness_seconds
    ]
    user.device_info.connections = connections
    user.ip = sorted({connection.ip for connection in connections})
    user.device_info.unique_ips = set(user.ip)
    user.device_info.unique_nodes = {connection.node_id for connection in connections}
    user.device_info.inbound_protocols = {connection.inbound_protocol for connection in connections}
    user.device_info.is_multi_device = (
        len(user.ip) > 2 or len(user.device_info.unique_nodes) > 1 or len(user.device_info.inbound_protocols) > 1
    )
    return True


async def publish_api_snapshot(users: dict[str, UserType], evidence: APISampleEvidence) -> bool:
    """Publish only a current sample, isolated from log tasks winding down."""
    global _api_snapshot
    from utils.monitoring_gate import monitoring_is_current, monitoring_snapshot

    admission = monitoring_snapshot()
    async with ACTIVE_USERS_LOCK:
        if not api_evidence_is_current(evidence) or not monitoring_is_current(admission):
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
    if node_id is None or not LOG_OBSERVATIONS.monitors(node_id):
        return
    now = time.time() if when is None else when
    previous = NODE_LAST_EVENT.get(node_id)
    if previous is None or now - previous > node_silence_window(LOG_OBSERVATIONS.interval) or now < previous:
        # Detect the gap on receipt too: no enforcement/status poll need have
        # observed the stale heartbeat before it was replaced by this one.
        LOG_OBSERVATIONS.note_transport_gap(now, node_id=node_id)
    NODE_LAST_EVENT[node_id] = now


def forget_node_event(node_id: int | None) -> None:
    """Drop a node's heartbeat when its task is cancelled or the node disappears."""
    if type(node_id) is not int:
        return
    if NODE_LAST_EVENT.pop(node_id, None) is not None and LOG_OBSERVATIONS.monitors(node_id):
        LOG_OBSERVATIONS.note_transport_gap(time.time(), node_id=node_id)


def clear_node_events() -> None:
    """Forget every heartbeat, for the periodic full rebuild of the node tasks."""
    now = time.time()
    for node_id in NODE_LAST_EVENT:
        if LOG_OBSERVATIONS.monitors(node_id):
            LOG_OBSERVATIONS.note_transport_gap(now, node_id=node_id)
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
    snapshot.log_evidence = getattr(users, "log_evidence", None)
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
        now = time.time()
        _validate_live_log_times(now)
        snapshot = _clone_user_map(ACTIVE_USERS)
        snapshot.log_evidence = LOG_OBSERVATIONS.evidence(end=now)
        retain_log_interval(snapshot, snapshot.log_evidence.interval_start, snapshot.log_evidence.interval_end)
        return snapshot


async def pop_active_users_snapshot(config_data: dict | None = None) -> ActiveUsersSnapshot:
    """
    Atomically take an isolated snapshot while retaining fresh Logs observations.
    Original source times bound the rolling store, including when Last seen is
    longer than the scan interval. Concurrent ingestion never aliases the snapshot.
    """
    global _api_snapshot
    from utils.read_config import get_config_revision

    async with ACTIVE_USERS_LOCK:
        if config_data is not None and config_data.get("_revision", get_config_revision()) != get_config_revision():
            return ActiveUsersSnapshot()
        if _api_snapshot is not None:
            _api_snapshot = None
            ACTIVE_USERS.clear()
            return ActiveUsersSnapshot()
        if config_data is not None and LOG_OBSERVATIONS.configure(config_data):
            ACTIVE_USERS.clear()
        now = time.time()
        _validate_live_log_times(now)
        evidence = LOG_OBSERVATIONS.evidence(end=now)
        # Prune the live store without the snapshot's ready-node filter. Records
        # from monitored nodes still warming up must remain available to later scans.
        retain_log_interval(ACTIVE_USERS, max(LOG_OBSERVATIONS.floor, evidence.interval_start), evidence.interval_end)
        snapshot = _clone_user_map(ACTIVE_USERS)
        snapshot.log_evidence = evidence
        retain_log_interval(snapshot, snapshot.log_evidence.interval_start, snapshot.log_evidence.interval_end)
        return snapshot


async def merge_active_users_snapshot(batch: dict[str, UserType]) -> int:
    """Requeue a consumed batch when the cycle failed before it could be evaluated.

    Rolling Logs observations normally remain in ACTIVE_USERS. Preserve the
    restoration contract for callers holding an unevaluated batch, without
    overwriting newer observations or crossing source generations.

    Returns the number of users actually restored (those without a newer observation).
    """
    from utils.monitoring_gate import monitoring_is_current, monitoring_snapshot

    admission = monitoring_snapshot()
    if not batch or getattr(batch, "api_evidence", None) is not None or not monitoring_is_current(admission):
        # API observations belong to exactly one cycle; only Logs are requeued.
        return 0
    restored = 0
    async with ACTIVE_USERS_LOCK:
        if not monitoring_is_current(admission) or not LOG_OBSERVATIONS.is_current(
            getattr(batch, "log_evidence", None)
        ):
            return 0
        retain_log_interval(
            batch, max(LOG_OBSERVATIONS.floor, time.time() - LOG_OBSERVATIONS.freshness_seconds), time.time()
        )
        for email, user in batch.items():
            if not email or not user:
                continue
            if email in ACTIVE_USERS:
                # A newer observation arrived during the cycle; do not overwrite it.
                continue
            ACTIVE_USERS[email] = user
            restored += 1
    return restored


def _validate_live_log_times(now: float) -> None:
    """Retire invalid monitored-node evidence before snapshot pruning removes it."""
    for user in ACTIVE_USERS.values():
        for connection in user.device_info.connections:
            if LOG_OBSERVATIONS.monitors(connection.node_id):
                LOG_OBSERVATIONS.validate_source_time(connection.node_id, connection.last_seen, now=now)


def retain_log_interval(users: dict[str, UserType], start: float, end: float) -> None:
    """Retain original-time observations from a snapshot's frozen ready scope."""
    evidence = getattr(users, "log_evidence", None)
    eligible = evidence.eligible_node_ids if evidence is not None else None
    for name, user in list(users.items()):
        connections = [
            c
            for c in user.device_info.connections
            if type(c.last_seen) in (int, float)
            and math.isfinite(c.last_seen)
            and start < c.last_seen <= end
            and LOG_OBSERVATIONS.monitors(c.node_id)
            and (evidence is None or (type(c.node_id) is int and eligible is not None and c.node_id in eligible))
        ]
        user.device_info.connections = connections
        user.ip = sorted({c.ip for c in connections})
        user.device_info.unique_ips = set(user.ip)
        user.device_info.unique_nodes = {c.node_id for c in connections}
        user.device_info.inbound_protocols = {c.inbound_protocol for c in connections}
        user.device_info.is_multi_device = (
            len(user.ip) > 2 or len(user.device_info.unique_nodes) > 1 or len(user.device_info.inbound_protocols) > 1
        )
        if not connections:
            del users[name]
