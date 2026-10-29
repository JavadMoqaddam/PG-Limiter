"""Request-owned API observations, independent of fleet collection duration."""

import math
import re
import time
from dataclasses import dataclass
from time import monotonic as monotonic_time


@dataclass(frozen=True)
class APIObservationWindow:
    started_at: float
    received_at: float
    started_monotonic: float
    received_monotonic: float

    def is_valid(self) -> bool:
        values = (self.started_at, self.received_at, self.started_monotonic, self.received_monotonic)
        return (
            all(type(value) in (int, float) and math.isfinite(value) for value in values)
            and 1_000_000_000 <= self.started_at <= self.received_at
            and self.started_monotonic <= self.received_monotonic
            and abs((self.received_at - self.started_at) - (self.received_monotonic - self.started_monotonic)) <= 1
        )

    def clock_is_current(self) -> bool:
        return self.is_valid() and time.time() >= self.received_at and monotonic_time() >= self.received_monotonic

    def is_fresh(self, freshness_seconds: int) -> bool:
        """Bound age from the earliest possible node sample, using both clocks."""
        return (
            self.clock_is_current()
            and max(time.time() - self.started_at, monotonic_time() - self.started_monotonic) < freshness_seconds
        )


@dataclass(frozen=True)
class APIUserContext:
    user_id: int
    group_ids: frozenset[int]
    required_nodes: frozenset[int]
    live_nodes: frozenset[int]


def xray_has_live_ip_map(version: str | None) -> bool:
    """v26.4.13 first returns timestamps from the reference-counted online map.

    Older/unknown producers retain source-age filtering. Their response keys
    alone cannot establish a currently open connection.
    """
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", version) if isinstance(version, str) else None
    return bool(match and tuple(map(int, match.groups())) >= (26, 4, 13))
