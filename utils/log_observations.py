"""Event-time evidence for raw Xray records, which have no zone or replay cursor."""

import hashlib
import json
import re
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_TIMESTAMP = re.compile(r"^(\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2})(?:\.(\d{1,6}))?\s")


@dataclass(frozen=True)
class LogSampleEvidence:
    """A completed ordinary interval, never moved forward during preparation."""

    generation: int
    config_revision: int
    interval_start: float
    interval_end: float
    fault_version: int
    unavailable_reason: str = ""
    eligible_node_ids: frozenset[int] | None = None


class LogObservations:
    """Process-lifetime per-node replay memory and fleet readiness."""

    def __init__(self):
        self.generation = 0
        self.signature = None
        self.floor = time.time()
        self.interval = 60.0
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
        self.uncertain_until = 0.0
        self.fault_version = 0

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
        )
        if signature == self.signature:
            return False
        self.invalidate()
        self.signature = signature
        self.source = signature[0]
        self.disabled_nodes = frozenset(signature[5])
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
        for cache in self.seen.values():
            while len(cache) > self.capacity:
                _, evicted_at = cache.popitem(last=False)
                if evicted_at + self.interval > time.time():
                    self.mark_uncertain(evicted_at + self.interval)
        return True

    def zone(self, node_id: int | None) -> ZoneInfo | None:
        # Presence, including an invalid override, wins over the default.
        return self.zones.get(str(node_id), self.default_zone)

    def register_fleet(self, nodes) -> None:
        """Record an authoritative panel list before any access payload is read."""
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
        self.fleet_seen = time.time()

    def monitors(self, node_id: int | None) -> bool:
        """Use the same sampling scope for transport, access records and snapshots."""
        return (
            node_id not in self.disabled_nodes
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
        if new != self.required_nodes:
            # A scope round trip must not revive a parser from an earlier fleet.
            # Keep replay memory and settings signature while retiring its epoch.
            self.generation += 1
            self.fault_version += 1
            self.fleet_floor = time.time()
        self.required_nodes = new
        if new is not None:
            # Removing a node bounds lifetime memory as well as per-node memory.
            for node_id in set(self.seen) - set(new):
                del self.seen[node_id]

    def note_transport_gap(self, when: float) -> None:
        """Retire incomplete intervals without erasing pending records or replay identity."""
        self.transport_floor = max(self.transport_floor, when)
        self.fault_version += 1

    def mark_uncertain(self, until: float | None = None) -> None:
        self.uncertain_until = max(self.uncertain_until, until if until is not None else time.time() + self.interval)
        self.fault_version += 1

    def timestamp(self, line: str, node_id: int | None) -> float:
        """Convert a printed local timestamp, rejecting DST folds and gaps."""
        zone = self.zone(node_id)
        match = _TIMESTAMP.match(line)
        if zone is None or match is None:
            raise ValueError("missing/invalid source timezone or timestamp")
        naive = datetime.strptime(match[1], "%Y/%m/%d %H:%M:%S")
        naive = naive.replace(microsecond=int((match[2] or "0").ljust(6, "0")))
        candidates = set()
        try:
            for fold in (0, 1):
                local = naive.replace(tzinfo=zone, fold=fold)
                utc = local.astimezone(timezone.utc)
                if utc.astimezone(zone).replace(tzinfo=None) == naive:
                    candidates.add(utc.timestamp())
        except (OverflowError, OSError) as error:
            # Zone conversion and epoch conversion can overflow at datetime's
            # supported year boundaries. These are unusable source timestamps.
            raise ValueError("source timestamp outside supported range") from error
        if len(candidates) != 1:
            raise ValueError("ambiguous or nonexistent source timestamp")
        observed_at = candidates.pop()
        if observed_at > time.time():
            raise ValueError("future source timestamp")
        return observed_at

    def admit(self, node_id: int, line: str, observed_at: float) -> bool:
        """Commit identity after preparation, atomically with the user-map mutation."""
        now = time.time()
        if observed_at < self.floor or not 0 <= now - observed_at < self.interval:
            return False
        cache = self.seen.setdefault(node_id, OrderedDict())
        # Amortized O(1) expiry. Out-of-order expired entries behind a live entry
        # remain bounded by capacity and are harmless when eventually evicted.
        while cache and next(iter(cache.values())) <= now - self.interval:
            cache.popitem(last=False)
        fingerprint = hashlib.sha256(line.encode()).digest()
        if fingerprint in cache:
            return False
        cache[fingerprint] = observed_at
        while len(cache) > self.capacity:
            _, evicted_at = cache.popitem(last=False)
            if evicted_at + self.interval > now:
                self.mark_uncertain(evicted_at + self.interval)
        return True

    def unavailable_reason(self, *, end: float | None = None) -> str:
        from utils.shared_state import NODE_LAST_EVENT, node_silence_window

        now = time.time() if end is None else end
        if self.source != "logs":
            return "Logs source is inactive"
        if self.config_error:
            return self.config_error
        # An authoritative empty scope is intentional zero; None is unknown.
        if self.required_nodes is None or now - self.fleet_seen > node_silence_window(self.interval):
            return "waiting for the node fleet"
        for node_id, connected in self.required_nodes.items():
            if self.zone(node_id) is None:
                return f"node {node_id}: source timezone missing or invalid"
            if not connected:
                return f"node {node_id}: panel connection unavailable"
            if now - NODE_LAST_EVENT.get(node_id, float("-inf")) > node_silence_window(self.interval):
                return f"node {node_id}: waiting for log transport"
        if now < self.uncertain_until:
            return "timestamp or replay uncertainty in the current interval"
        if now - self.interval < max(self.floor, self.fleet_floor, self.transport_floor):
            return "collecting a complete check interval"
        return ""

    def evidence(self) -> LogSampleEvidence:
        from utils.read_config import get_config_revision

        now = time.time()
        return LogSampleEvidence(
            self.generation,
            get_config_revision(),
            now - self.interval,
            now,
            self.fault_version,
            self.unavailable_reason(end=now),
            (
                frozenset(
                    node_id for node_id, connected in self.required_nodes.items() if type(node_id) is int and connected
                )
                if self.required_nodes is not None and all(type(node_id) is int for node_id in self.required_nodes)
                else None
            ),
        )

    def is_current(self, evidence: LogSampleEvidence | None) -> bool:
        from utils.read_config import get_config_revision

        return bool(
            evidence is not None
            and not evidence.unavailable_reason
            and evidence.generation == self.generation
            and evidence.config_revision == get_config_revision()
            and evidence.fault_version == self.fault_version
            and not self.unavailable_reason()
        )


LOG_OBSERVATIONS = LogObservations()
