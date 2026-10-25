"""Event-time evidence for raw Xray records, which have no zone or replay cursor."""

import hashlib
import heapq
import json
import logging
import math
import re
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from utils.read_config import DEFAULT_API_IP_FRESHNESS, normalize_api_ip_freshness

_TIMESTAMP = re.compile(r"^(\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2})(?:\.(\d{1,6}))?\s")
_TIMESTAMP_FAILURE_REASONS = {
    "missing/invalid source timezone or timestamp": "invalid_timestamp",
    "source timestamp outside supported range": "out_of_range_timestamp",
    "ambiguous or nonexistent source timestamp": "ambiguous_timestamp",
    "future source timestamp": "future_timestamp",
}
_UNCERTAINTY_REASONS = frozenset(_TIMESTAMP_FAILURE_REASONS.values()) | {
    "live_cache_eviction",
    "cache_capacity_reduction",
    "unknown",
}
_MAX_DIAGNOSTIC_NODES = 128
_DIAGNOSTIC_LOG_INTERVAL = 60.0
_logger = logging.getLogger(__name__)


@lru_cache(maxsize=1024)
def _printed_second_timestamp(zone: ZoneInfo, printed_second: str) -> int:
    """Cache zone conversion only; fractions and future checks remain per record."""
    naive = datetime.fromisoformat(printed_second.replace("/", "-"))
    candidates = set()
    try:
        for fold in (0, 1):
            local = naive.replace(tzinfo=zone, fold=fold)
            utc = local.astimezone(timezone.utc)
            if utc.astimezone(zone).replace(tzinfo=None) == naive:
                candidates.add(int(utc.timestamp()))
    except (OverflowError, OSError) as error:
        raise ValueError("source timestamp outside supported range") from error
    if len(candidates) != 1:
        raise ValueError("ambiguous or nonexistent source timestamp")
    return candidates.pop()


@dataclass(frozen=True)
class LogSampleEvidence:
    """A completed source-time window, never moved forward during preparation."""

    generation: int
    config_revision: int
    interval_start: float
    interval_end: float
    fault_version: int
    unavailable_reason: str = ""
    eligible_node_ids: frozenset[int] | None = None
    required_node_ids: frozenset[int] | None = None
    node_versions: tuple[tuple[int, int], ...] = ()
    coverage_complete: bool = False
    freshness_seconds: int = DEFAULT_API_IP_FRESHNESS


@dataclass
class NodeLogState:
    """The independent lifetime and incomplete interval of one node stream."""

    epoch: int
    floor: float
    uncertain_until: float = 0.0


class LogObservations:
    """Process-lifetime per-node replay memory and fleet readiness."""

    def __init__(self):
        self.generation = 0
        self.signature = None
        self.floor = time.time()
        self.interval = 60.0
        self.freshness_seconds = DEFAULT_API_IP_FRESHNESS
        self.capacity = 10000
        self.source = "logs"
        self.zones: dict[str, ZoneInfo | None] = {}
        self.default_zone: ZoneInfo | None = None
        self.config_error = ""
        self.disabled_nodes: frozenset[int] = frozenset()
        self.panel_nodes: dict[int, str] | None = None
        self.excluded_panel_nodes: frozenset[int] = frozenset()
        self.required_nodes: dict[int, bool] | None = None
        self.fleet_seen = 0.0
        self.fleet_floor = time.time()
        self.transport_floor = 0.0
        self.seen: dict[int, OrderedDict[bytes, float]] = {}
        self._seen_expiry: dict[int, list[tuple[float, bytes]]] = {}
        self.uncertain_until = 0.0
        self.fault_version = 0
        self.node_states: dict[int, NodeLogState] = {}
        self._next_node_epoch = 0
        self._global_uncertain_until = 0.0
        self._uncertainty_counts: dict[int | None, dict[str, int]] = {}
        self._next_diagnostic_log = 0.0

    def invalidate(self):
        """A committed source change retires parsers and pending snapshots."""
        self.generation += 1
        self.signature = None
        self.floor = time.time()

    @staticmethod
    def _zone(value) -> ZoneInfo | None:
        if not isinstance(value, str) or not value or value != value.strip():
            return None
        try:
            return ZoneInfo(value)
        except (ValueError, ZoneInfoNotFoundError):
            return None

    def configure(self, config: dict) -> bool:
        """Refresh only when sampling settings change; return whether data must reset."""
        raw_zones = config.get("log_node_timezones", {})
        signature = (
            config.get("ip_source", "logs"),
            config.get("check_interval", 60),
            json.dumps(raw_zones, sort_keys=True),
            config.get("log_timezone"),
            config.get("log_replay_cache_size", 10000),
            tuple(sorted(set(config.get("disabled_nodes", []) or []))),
            normalize_api_ip_freshness(config.get("api_ip_freshness")),
        )
        if signature == self.signature:
            return False
        self.invalidate()
        self.signature = signature
        self.source = signature[0]
        self.disabled_nodes = frozenset(signature[5])
        self.freshness_seconds = signature[6]
        self._refresh_required_nodes()
        self.config_error = ""
        try:
            self.interval = float(signature[1])
            self.capacity = int(signature[4])
            if not 0 < self.interval < float("inf") or self.capacity < 1:
                raise ValueError
        except (ValueError, TypeError, OverflowError):
            self.interval, self.capacity = 60.0, 10000
            self.config_error = "invalid Logs interval or replay capacity"
        try:
            mapping = json.loads(raw_zones) if isinstance(raw_zones, str) else raw_zones
            if not isinstance(mapping, dict) or any(not str(k).isdecimal() for k in mapping):
                raise ValueError
            self.zones = {str(k): self._zone(v) for k, v in mapping.items()}
        except (ValueError, TypeError):
            self.zones = {}
            self.config_error = "invalid LOG_NODE_TIMEZONES JSON mapping"
        self.default_zone = self._zone(config.get("log_timezone"))
        now = time.time()
        for node_id, cache in self.seen.items():
            self._expire_replay_cache(node_id, now)
            while len(cache) > self.capacity:
                _, evicted_at = cache.popitem(last=False)
                if evicted_at + self.freshness_seconds > now:
                    self.mark_uncertain(
                        evicted_at + self.freshness_seconds, node_id=node_id, reason="cache_capacity_reduction"
                    )
            self._compact_replay_cache(node_id)
        return True

    def zone(self, node_id: int | None) -> ZoneInfo | None:
        # Presence, including an invalid override, wins over the default.
        return self.zones.get(str(node_id), self.default_zone)

    def register_fleet(self, nodes) -> None:
        """Record an authoritative panel list before any access payload is read."""
        from utils.shared_state import node_silence_window

        now = time.time()
        if self.required_nodes is not None and now - self.fleet_seen > node_silence_window(self.interval):
            # Receipt must detect an unobserved fleet gap before renewing freshness.
            self.generation += 1
            self.fault_version += 1
            self.fleet_floor = now
        raw_nodes = tuple(nodes) if nodes is not None else None
        # Validate before dict equality can merge integer IDs with bool/float
        # aliases. A malformed fleet is unknown, even if a valid ID came first.
        self.panel_nodes = (
            {node.node_id: node.status for node in raw_nodes}
            if raw_nodes is not None and all(type(node.node_id) is int for node in raw_nodes)
            else None
        )
        if self.panel_nodes is not None:
            self.excluded_panel_nodes = frozenset(
                node_id for node_id, status in self.panel_nodes.items() if status in ("disabled", "limited")
            )
        self._refresh_required_nodes()
        self.fleet_seen = now

    def monitors(self, node_id: int | None) -> bool:
        """Use the same sampling scope for transport, access records and snapshots."""
        return (
            type(node_id) is int
            and node_id not in self.disabled_nodes
            and node_id not in self.excluded_panel_nodes
            and (self.required_nodes is None or node_id in self.required_nodes)
        )

    def _refresh_required_nodes(self) -> None:
        # Panel-disabled and limited nodes are outside the enabled fleet. Every
        # other status stays required and must become connected for readiness.
        new = (
            {
                node_id: status == "connected"
                for node_id, status in self.panel_nodes.items()
                if node_id not in self.disabled_nodes and status not in ("disabled", "limited")
            }
            if self.panel_nodes is not None
            else None
        )
        previous = self.required_nodes
        if (new is None) != (previous is None):
            # Unknown fleet transitions retire every parser and pending snapshot.
            self.generation += 1
            self.fault_version += 1
            self.fleet_floor = time.time()
        self.required_nodes = new
        if new is not None:
            for node_id, connected in new.items():
                if previous is None or node_id not in previous or previous[node_id] != connected:
                    self._retire_node(node_id, time.time())
            for node_id in set(self.node_states) - set(new):
                del self.node_states[node_id]
            if not new and previous:
                # Intentional zero coverage still needs an ordinary empty interval.
                self.fleet_floor = time.time()
            # Removing a node bounds lifetime memory as well as per-node memory.
            for node_id in set(self.seen) - set(new):
                del self.seen[node_id]
                self._seen_expiry.pop(node_id, None)

    def _retire_node(self, node_id: int, when: float) -> NodeLogState:
        self._next_node_epoch += 1
        state = self.node_states.get(node_id)
        if state is None:
            state = NodeLogState(self._next_node_epoch, when)
            self.node_states[node_id] = state
        else:
            state.epoch = self._next_node_epoch
            state.floor = max(state.floor, when)
        return state

    def node_epoch(self, node_id: int | None) -> int | None:
        """Capture a stream lifetime so preparation cannot cross its own recovery."""
        if type(node_id) is not int:
            return None
        state = self.node_states.get(node_id)
        return state.epoch if state is not None else 0

    def node_interval_floor(self, node_id: int) -> float | None:
        """Return the current node's observation floor without creating state."""
        state = self.node_states.get(node_id) if type(node_id) is int else None
        return state.floor if state is not None else None

    def note_transport_gap(self, when: float, *, node_id: int | None = None) -> None:
        """Retire incomplete intervals without erasing pending records or replay identity."""
        if type(node_id) is int:
            self._retire_node(node_id, when)
        else:
            self.transport_floor = max(self.transport_floor, when)
            self.fault_version += 1

    def mark_uncertain(
        self, until: float | None = None, *, node_id: int | None = None, reason: str = "unknown"
    ) -> None:
        """Record a bounded diagnostic alongside the existing uncertainty deadline."""
        now = time.time()
        if now >= self.uncertain_until:
            self._uncertainty_counts.clear()
        deadline = until if until is not None else now + self.freshness_seconds
        self.uncertain_until = max(self.uncertain_until, deadline)
        if type(node_id) is int and self.required_nodes is not None and node_id in self.required_nodes:
            # A fault retires captured witnesses, but does not move healthy floors.
            state = self.node_states.get(node_id)
            if state is None:
                state = self._retire_node(node_id, now)
            self._next_node_epoch += 1
            state.epoch = self._next_node_epoch
            state.uncertain_until = max(state.uncertain_until, deadline)
        else:
            self._global_uncertain_until = max(self._global_uncertain_until, deadline)
            self.fault_version += 1
        if not isinstance(reason, str) or reason not in _UNCERTAINTY_REASONS:
            reason = "unknown"
        if type(node_id) is not int:
            node_id = None
        if node_id not in self._uncertainty_counts and len(self._uncertainty_counts) >= _MAX_DIAGNOSTIC_NODES:
            node_id = None
        counts = self._uncertainty_counts.setdefault(node_id, {})
        counts[reason] = counts.get(reason, 0) + 1

    @staticmethod
    def timestamp_failure_reason(error: ValueError) -> str:
        """Translate fixed errors; never retain an arbitrary date-parser message."""
        return _TIMESTAMP_FAILURE_REASONS.get(str(error), "invalid_timestamp")

    def uncertainty_diagnostics(self) -> dict:
        """Describe the current/latest fault episode without records or user identities.

        At most 128 node IDs are retained; a null node aggregates other IDs.
        Counts restart after an expired episode, independently of log throttling.
        """
        reason_counts: dict[str, int] = {}
        nodes = []
        for node_id, counts in sorted(
            self._uncertainty_counts.items(), key=lambda item: (item[0] is None, item[0] or 0)
        ):
            for reason, count in counts.items():
                reason_counts[reason] = reason_counts.get(reason, 0) + count
            nodes.append(
                {
                    "node_id": node_id,
                    "reason_counts": dict(counts),
                    "cache_entries": len(self.seen.get(node_id, ())) if node_id is not None else None,
                }
            )
        return {
            "reason_counts": reason_counts,
            "nodes": nodes,
            "remaining_seconds": max(0.0, self.uncertain_until - time.time()),
            "uncertain_until": self.uncertain_until,
            "interval_seconds": self.interval,
            "freshness_seconds": self.freshness_seconds,
            "cache_capacity": self.capacity,
        }

    def _report_uncertainty(self) -> None:
        """Serialize and write diagnostics only from readiness, once per minute."""
        now = time.monotonic()
        if not self._uncertainty_counts or now < self._next_diagnostic_log:
            return
        self._next_diagnostic_log = now + _DIAGNOSTIC_LOG_INTERVAL
        _logger.warning("Logs uncertainty diagnostics: %s", json.dumps(self.uncertainty_diagnostics(), sort_keys=True))

    def timestamp(self, line: str, node_id: int | None) -> float:
        """Convert a printed local timestamp, rejecting DST folds and gaps."""
        zone = self.zone(node_id)
        match = _TIMESTAMP.match(line)
        if zone is None or match is None:
            raise ValueError("missing/invalid source timezone or timestamp")
        seconds = _printed_second_timestamp(zone, match[1])
        micros = int((match[2] or "0").ljust(6, "0"))
        # Integer composition preserves aware datetime.timestamp rounding, also
        # for dates before 1970 and near datetime's supported year boundaries.
        observed_at = (seconds * 1_000_000 + micros) / 1_000_000
        if observed_at > time.time():
            raise ValueError("future source timestamp")
        return observed_at

    def _expire_replay_cache(self, node_id: int, now: float) -> None:
        """Reclaim expired source times even when admission order differs."""
        cache = self.seen[node_id]
        expiry = self._seen_expiry[node_id]
        cutoff = now - self.freshness_seconds
        while expiry and expiry[0][0] <= cutoff:
            observed_at, fingerprint = heapq.heappop(expiry)
            # Live overflow can leave an index entry for an evicted identity.
            # A re-admitted identity only expires at its current source time.
            if cache.get(fingerprint) == observed_at:
                del cache[fingerprint]

    def _compact_replay_cache(self, node_id: int) -> None:
        """Bound lazy entries from FIFO eviction with amortized linear rebuilding."""
        cache = self.seen[node_id]
        if len(self._seen_expiry[node_id]) > 2 * len(cache):
            expiry = [(observed_at, fingerprint) for fingerprint, observed_at in cache.items()]
            heapq.heapify(expiry)
            self._seen_expiry[node_id] = expiry

    def validate_source_time(self, node_id: int, observed_at: float, *, now: float | None = None) -> bool:
        """Preserve uncertainty before an unusable original time can be discarded."""
        evaluated_at = time.time() if now is None else now
        reason = ""
        if type(observed_at) not in (int, float) or not math.isfinite(observed_at):
            reason = "invalid_timestamp"
        elif observed_at > evaluated_at:
            reason = "future_timestamp"
        if reason:
            # A discarded future observation can qualify until its original
            # expiry; a backwards clock step must not shorten that deadline.
            expiry_base = observed_at if reason == "future_timestamp" else evaluated_at
            self.mark_uncertain(expiry_base + self.freshness_seconds, node_id=node_id, reason=reason)
            return False
        return True

    def admit(self, node_id: int, line: str, observed_at: float) -> bool:
        """Commit identity after preparation, atomically with the user-map mutation."""
        now = time.time()
        if not self.validate_source_time(node_id, observed_at, now=now):
            return False
        if observed_at < self.floor or not 0 <= now - observed_at < self.freshness_seconds:
            return False
        cache = self.seen.setdefault(node_id, OrderedDict())
        self._seen_expiry.setdefault(node_id, [])
        self._expire_replay_cache(node_id, now)
        fingerprint = hashlib.sha256(line.encode()).digest()
        if fingerprint in cache:
            self._compact_replay_cache(node_id)
            return False
        cache[fingerprint] = observed_at
        heapq.heappush(self._seen_expiry[node_id], (observed_at, fingerprint))
        while len(cache) > self.capacity:
            _, evicted_at = cache.popitem(last=False)
            if evicted_at + self.freshness_seconds > now:
                self.mark_uncertain(evicted_at + self.freshness_seconds, node_id=node_id, reason="live_cache_eviction")
        self._compact_replay_cache(node_id)
        return True

    def _global_unavailable_reason(self, now: float) -> str:
        from utils.shared_state import node_silence_window

        if self.source != "logs":
            return "Logs source is inactive"
        if self.config_error:
            return self.config_error
        # An authoritative empty scope is intentional zero; None is unknown.
        if self.required_nodes is None or now - self.fleet_seen > node_silence_window(self.interval):
            return "waiting for the node fleet"
        if now < self._global_uncertain_until:
            self._report_uncertainty()
            return "timestamp or replay uncertainty in the current interval"
        if now - max(self.interval, self.freshness_seconds) < max(self.floor, self.fleet_floor, self.transport_floor):
            return "collecting a complete check interval"
        return ""

    def node_unavailable_reason(self, node_id: int, *, end: float | None = None) -> str:
        """Give fixed safe readiness reasons for one included node."""
        from utils.shared_state import NODE_LAST_EVENT, node_silence_window

        now = time.time() if end is None else end
        if type(node_id) is not int or self.required_nodes is None or node_id not in self.required_nodes:
            return "node is outside the monitoring scope"
        if self.zone(node_id) is None:
            return "source timezone missing or invalid"
        if not self.required_nodes[node_id]:
            return "panel connection unavailable"
        if now - NODE_LAST_EVENT.get(node_id, float("-inf")) > node_silence_window(self.interval):
            return "waiting for log transport"
        state = self.node_states.get(node_id)
        if state is not None and now < state.uncertain_until:
            return "timestamp or replay uncertainty in the current interval"
        if state is None or now - max(self.interval, self.freshness_seconds) < state.floor:
            return "collecting a complete check interval"
        return ""

    def unavailable_reason(self, *, end: float | None = None) -> str:
        """Preserve strict whole-fleet readiness for absence and grouped counts."""
        now = time.time() if end is None else end
        global_reason = self._global_unavailable_reason(now)
        # Preserve node fault diagnostics ahead of ordinary interval warmup.
        if global_reason and global_reason != "collecting a complete check interval":
            return global_reason
        for node_id in self.required_nodes or ():
            reason = self.node_unavailable_reason(node_id, end=now)
            if reason == "timestamp or replay uncertainty in the current interval":
                self._report_uncertainty()
                return reason
            if reason and reason != "collecting a complete check interval":
                return f"node {node_id}: {reason}"
        if global_reason or any(
            self.node_unavailable_reason(node_id, end=now) for node_id in self.required_nodes or ()
        ):
            return "collecting a complete check interval"
        return ""

    def evidence(self, *, end: float | None = None) -> LogSampleEvidence:
        from utils.read_config import get_config_revision

        now = time.time() if end is None else end
        required = frozenset(self.required_nodes) if self.required_nodes is not None else None
        global_reason = self._global_unavailable_reason(now)
        eligible = (
            frozenset(node_id for node_id in required if not self.node_unavailable_reason(node_id, end=now))
            if required is not None and not global_reason
            else (frozenset() if required is not None else None)
        )
        return LogSampleEvidence(
            self.generation,
            get_config_revision(),
            now - self.freshness_seconds,
            now,
            self.fault_version,
            self.unavailable_reason(end=now),
            eligible,
            required,
            tuple((node_id, self.node_states[node_id].epoch) for node_id in sorted(eligible or ())),
            required is not None and eligible == required and not global_reason,
            self.freshness_seconds,
        )

    def is_current(self, evidence: LogSampleEvidence | None, *, allow_partial: bool = False) -> bool:
        from utils.read_config import get_config_revision

        now = time.time()
        globally_current = bool(
            evidence is not None
            and evidence.generation == self.generation
            and evidence.config_revision == get_config_revision()
            and evidence.fault_version == self.fault_version
            and evidence.required_node_ids is not None
            and not self._global_unavailable_reason(now)
        )
        if not globally_current:
            return False
        if evidence.coverage_complete and evidence.required_node_ids != frozenset(self.required_nodes):
            # A captured complete sample cannot claim absence in a changed fleet.
            return False
        if not allow_partial and (
            not evidence.coverage_complete
            or evidence.unavailable_reason
            or evidence.required_node_ids != frozenset(self.required_nodes)
            or self.unavailable_reason()
        ):
            return False
        if not evidence.eligible_node_ids and not evidence.coverage_complete:
            return False
        return all(
            self.node_states.get(node_id) is not None
            and self.node_states[node_id].epoch == epoch
            and not self.node_unavailable_reason(node_id, end=now)
            for node_id, epoch in evidence.node_versions
        )


LOG_OBSERVATIONS = LogObservations()
