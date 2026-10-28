"""Read-only source probe; run inside the existing container with ``python -``.

Only stdlib and httpx are imported: application imports can initialize logging,
migrate settings, or send Telegram messages. SQLite is opened with mode=ro.
The probe does not measure the running daemon's cache or enforce any policy.
"""

import argparse
import asyncio
import hashlib
import ipaddress
import json
import logging
import math
import os
import re
import sqlite3
import time
from collections import Counter, OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

TIMESTAMP = re.compile(r"^(\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2})(?:\.(\d{1,6}))?\s")
IPV6 = re.compile(r"\[([0-9a-fA-F:]+)\]:\d+\s+accepted")
IPV4 = re.compile(r"(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})")
EMAIL = re.compile(r"email:\s*([A-Za-z0-9._%+-]+)")
INVALID_EMAILS = {"API]", "Found", "(normal)", "timeout", "EOF", "address", "INFO", "request"}
STATUSES = {"connected", "connecting", "disconnected", "error", "disabled", "limited"}
CONFIG_KEYS = (
    "general_limit",
    "check_interval",
    "max_warning_count",
    "country_code",
    "disabled_nodes",
    "api_ip_freshness",
    "ip_source",
)
MAX_LINE_BYTES = 65536
MAX_JSON_BYTES = 2 * 1024 * 1024


class ProbeError(Exception):
    """Carry a fixed diagnostic code, never a dependency's exception text."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def source_zone(value):
    if not isinstance(value, str) or not value or value != value.strip():
        return None
    try:
        return ZoneInfo(value)
    except (ValueError, ZoneInfoNotFoundError):
        return None


def zone_summary(value):
    zone = source_zone(value)
    return zone.key if zone is not None else ("missing" if value is None else "invalid")


@dataclass
class Settings:
    panel_base: str = field(repr=False)
    panel_api_key: str = field(repr=False)
    general_limit: int
    check_interval: int
    max_warning_count: int
    country_filter_enabled: bool
    default_timezone: str | None
    node_timezones: dict
    disabled_nodes: frozenset[int]
    capacity: int
    verify_ssl: bool = False
    timezone_mapping_valid: bool = True
    freshness_seconds: int = 60

    def summary(self):
        return {
            "kind": "configuration",
            "probe_format_version": 2,
            "general_limit": self.general_limit,
            "check_interval_seconds": self.check_interval,
            "freshness_seconds": self.freshness_seconds,
            "max_warning_count": self.max_warning_count,
            "country_filter_enabled": self.country_filter_enabled,
            "log_timezone": zone_summary(self.default_timezone),
            "log_node_timezone_overrides": {key: zone_summary(value) for key, value in self.node_timezones.items()},
            "timezone_mapping_valid": self.timezone_mapping_valid,
            "configured_replay_capacity": self.capacity,
            "record_scope": "potential_records_before_country_cdn_blocklist_and_user_policy_filters",
            "capacity_estimate": "conditional_potential_record_rate_not_daemon_occupancy",
        }


def as_int(value, fallback):
    try:
        return int(value)
    except (ValueError, TypeError, OverflowError):
        return fallback


def positive_seconds(value, fallback):
    """Match exact positive Last seen values without importing application code."""
    text = str(value).strip()
    return int(text) if text.isascii() and text.isdecimal() and int(text) > 0 else fallback


def read_stored_settings(url: str, keys: tuple[str, ...]) -> dict:
    """Read selected committed values without changing SQLite or importing the app."""
    prefix = next((item for item in ("sqlite+aiosqlite:///", "sqlite:///") if url.startswith(item)), None)
    if prefix is None or "?" in url or url.endswith(":memory:"):
        raise ProbeError("configuration_database_unsupported")
    path = Path(url[len(prefix) :]).resolve()
    try:
        with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=2) as connection:
            connection.execute("PRAGMA query_only=ON")
            rows = connection.execute(
                "SELECT key, value FROM config WHERE key IN (" + ",".join("?" for _ in keys) + ")",
                keys,
            ).fetchall()
        # The real JSON column has NUMERIC affinity: sqlite3 can return numbers
        # directly, while strings/containers/bools remain serialized JSON text.
        return {
            key: value if value is None or isinstance(value, (int, float)) else json.loads(value) for key, value in rows
        }
    except (OSError, sqlite3.Error, ValueError, TypeError):
        raise ProbeError("configuration_unavailable") from None


def load_settings(environ=None):
    """Read only the relevant ENV values and committed SQLite configuration rows."""
    env = os.environ if environ is None else environ
    url = env.get("DATABASE_URL", "sqlite+aiosqlite:///./data/pg_limiter.db")
    db = read_stored_settings(url, CONFIG_KEYS)
    general = as_int(env.get("GENERAL_LIMIT"), 2)
    if general < 1:
        general = 2
    general_db = as_int(db.get("general_limit"), general)
    general = general_db if general_db >= 1 else general
    interval = as_int(db.get("check_interval"), as_int(env.get("CHECK_INTERVAL"), 60))
    freshness = positive_seconds(env.get("API_IP_FRESHNESS"), 60)
    stored_freshness = db.get("api_ip_freshness")
    legacy_auto = str(stored_freshness).strip().lower() in {"0", "auto"} or (
        stored_freshness is None and "ip_source" in db and not env.get("API_IP_FRESHNESS")
    )
    freshness = max(60, interval) if legacy_auto else positive_seconds(stored_freshness, freshness)
    warnings = as_int(db.get("max_warning_count"), as_int(env.get("MAX_WARNING_COUNT"), 3))
    capacity = as_int(env.get("LOG_REPLAY_CACHE_SIZE"), 0) if "LOG_REPLAY_CACHE_SIZE" in env else 10000
    if interval <= 0 or capacity <= 0:
        raise ProbeError("configuration_invalid_interval_or_capacity")
    country = str(db.get("country_code", env.get("COUNTRY_CODE", "")) or "").strip().lower()
    try:
        disabled = frozenset(int(item.strip()) for item in (db.get("disabled_nodes") or "").split(",") if item.strip())
    except (ValueError, AttributeError):
        disabled = frozenset()
    valid_mapping = True
    try:
        zones = json.loads(env.get("LOG_NODE_TIMEZONES", "{}"))
        if not isinstance(zones, dict) or any(not str(key).isdecimal() for key in zones):
            raise ValueError
        zones = {str(key): value for key, value in zones.items()}
    except (ValueError, TypeError):
        zones, valid_mapping = {}, False
    domain = str(env.get("PANEL_DOMAIN", "")).rstrip("/")
    if not domain.startswith(("https://", "http://")):
        domain = "https://" + domain
    parsed = urlsplit(domain)
    if not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ProbeError("configuration_panel_invalid")
    return Settings(
        domain,
        env.get("PANEL_API_KEY", ""),
        general,
        interval,
        warnings,
        country not in {"", "none", "off", "disabled", "any", "all"},
        env.get("LOG_TIMEZONE"),
        zones,
        disabled,
        capacity,
        str(env.get("PANEL_VERIFY_SSL", "false")).lower() in {"true", "1", "yes"},
        valid_mapping,
        freshness,
    )


def event_timestamp(line: str, zone) -> float:
    """Match 1.7.0's strict microsecond and DST conversion, before the future check."""
    match = TIMESTAMP.match(line)
    if match is None:
        raise ProbeError("malformed_timestamp")
    if zone is None:
        raise ProbeError("missing_or_invalid_timezone")
    try:
        naive = datetime.strptime(match[1], "%Y/%m/%d %H:%M:%S")
        naive = naive.replace(microsecond=int((match[2] or "0").ljust(6, "0")))
        candidates = set()
        for fold in (0, 1):
            utc = naive.replace(tzinfo=zone, fold=fold).astimezone(timezone.utc)
            if utc.astimezone(zone).replace(tzinfo=None) == naive:
                candidates.add(utc.timestamp())
    except (ValueError, OverflowError, OSError):
        raise ProbeError("invalid_timestamp") from None
    if len(candidates) != 1:
        raise ProbeError("ambiguous_or_nonexistent_timestamp")
    return candidates.pop()


class NodeStats:
    """Bounded full-record identities and counters for one independent probe stream."""

    def __init__(self, node_id: int, status: str, settings: Settings, started_at: float):
        self.node_id, self.status, self.settings, self.started_at = node_id, status, settings, started_at
        raw_zone = settings.node_timezones.get(str(node_id), settings.default_timezone)
        self.zone = source_zone(raw_zone) if settings.timezone_mapping_valid else None
        self.zone_name = zone_summary(raw_zone)
        self.seen = OrderedDict()
        self.counts = Counter()
        self.reasons = Counter()
        self.max_positive_delta = 0.0
        self.first_event = self.last_event = None
        self.oldest_source_event = self.newest_source_event = None
        self.min_record_age = self.max_record_age = None
        self.first_received = self.last_received = None
        self.receipt_bins = [0] * 16
        self.finished_at = None
        self.outcome = "total_deadline"
        self.connected = False
        self.elapsed = 0.0
        self.probe_capacity = min(settings.capacity, 100000)

    def observe(self, line: str, received_at: float):
        self.counts["sse_data_records"] += 1
        self.first_received = received_at if self.first_received is None else min(received_at, self.first_received)
        self.last_received = received_at if self.last_received is None else max(received_at, self.last_received)
        receipt_second = int(max(0, received_at - self.started_at))
        if receipt_second >= len(self.receipt_bins):
            self.counts["receipt_bin_overflow_records"] += 1
        self.receipt_bins[min(receipt_second, len(self.receipt_bins) - 1)] += 1
        if "accepted" not in line or "BLOCK]" in line:
            return
        email = EMAIL.search(line)
        if email is None or re.sub(r"^\d+\.", "", email[1]) in INVALID_EMAILS:
            return
        address = IPV6.search(line) or IPV4.search(line)
        try:
            if address is None or ipaddress.ip_address(address[1]).is_private:
                return
        except ValueError:
            return
        self.counts["potential_access_records"] += 1
        try:
            event = event_timestamp(line, self.zone)
        except ProbeError as error:
            self.reasons[error.code] += 1
            return
        self.counts["parsed_timestamp_records"] += 1
        self.oldest_source_event = event if self.oldest_source_event is None else min(event, self.oldest_source_event)
        self.newest_source_event = event if self.newest_source_event is None else max(event, self.newest_source_event)
        age = received_at - event
        self.min_record_age = age if self.min_record_age is None else min(age, self.min_record_age)
        self.max_record_age = age if self.max_record_age is None else max(age, self.max_record_age)
        delta = event - received_at
        self.max_positive_delta = max(self.max_positive_delta, delta)
        if delta > 0:
            self.reasons["future_timestamp"] += 1
            return
        if received_at - event >= self.settings.freshness_seconds:
            self.counts["stale_records"] += 1
            return
        if event < self.started_at:
            self.counts["recent_history_records"] += 1
        digest = hashlib.blake2b(line.encode(), digest_size=16).digest()
        if digest in self.seen:
            self.counts["duplicate_records"] += 1
            return
        cutoff = received_at - self.settings.freshness_seconds
        while self.seen and next(iter(self.seen.values())) <= cutoff:
            self.seen.popitem(last=False)
        if len(self.seen) >= self.probe_capacity:
            self.seen.popitem(last=False)
            self.counts["sample_cache_evictions"] += 1
        self.seen[digest] = event
        self.counts["recent_distinct_records"] += 1
        if event >= self.started_at:
            self.counts["live_distinct_records"] += 1
            self.first_event = event if self.first_event is None else min(event, self.first_event)
            self.last_event = event if self.last_event is None else max(event, self.last_event)

    def summary(self, elapsed=None):
        elapsed = self.elapsed if elapsed is None else elapsed
        count = self.counts["live_distinct_records"]
        rate = count / elapsed if elapsed > 0 else 0.0
        finished_at = self.finished_at if self.finished_at is not None else time.time()
        return {
            "kind": "node",
            "node_id": self.node_id,
            "status": self.status,
            "source_timezone": self.zone_name,
            "outcome": self.outcome,
            "sample_seconds": round(elapsed, 3),
            **{
                key: self.counts[key]
                for key in (
                    "potential_access_records",
                    "sse_data_records",
                    "parsed_timestamp_records",
                    "recent_distinct_records",
                    "live_distinct_records",
                    "duplicate_records",
                    "recent_history_records",
                    "stale_records",
                    "sample_cache_evictions",
                    "receipt_bin_overflow_records",
                )
            },
            "reason_counts": dict(self.reasons),
            "maximum_positive_clock_delta_seconds": round(self.max_positive_delta, 6),
            "oldest_source_epoch": self.oldest_source_event,
            "newest_source_epoch": self.newest_source_event,
            "source_event_span_seconds": (
                round(self.newest_source_event - self.oldest_source_event, 6)
                if self.oldest_source_event is not None
                else 0
            ),
            "minimum_record_age_at_receipt_seconds": (
                round(self.min_record_age, 6) if self.min_record_age is not None else None
            ),
            "maximum_record_age_at_receipt_seconds": (
                round(self.max_record_age, 6) if self.max_record_age is not None else None
            ),
            "newest_record_age_at_sample_end_seconds": (
                round(finished_at - self.newest_source_event, 6) if self.newest_source_event is not None else None
            ),
            "record_receive_span_seconds": (
                round(self.last_received - self.first_received, 6) if self.first_received is not None else 0
            ),
            "records_by_receipt_second": self.receipt_bins.copy(),
            "receipt_bin_seconds": 1,
            "live_distinct_records_per_second": round(rate, 3),
            "event_time_span_seconds": round(self.last_event - self.first_event, 6) if count else 0,
            "projected_interval_records_at_sample_rate": round(rate * self.settings.check_interval, 1),
            "projected_freshness_records_at_sample_rate": round(rate * self.settings.freshness_seconds, 1),
            "sample_cache_entries": len(self.seen),
            "sample_cache_scope": "recent_at_receipt_including_pre_probe_history",
            "sample_cache_at_capacity": len(self.seen) >= self.probe_capacity,
            "sample_deduplication_complete": not self.counts["sample_cache_evictions"],
            "probe_cache_capacity": self.probe_capacity,
            "capacity_estimate": "conditional_potential_record_rate_not_daemon_occupancy",
        }


async def request_json(client, method, url, stage, request_timeout, **kwargs):
    try:
        async with asyncio.timeout(request_timeout):
            async with client.stream(method, url, **kwargs) as response:
                if response.status_code != 200:
                    raise ProbeError(f"{stage}_http_{response.status_code}")
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(body) + len(chunk) > MAX_JSON_BYTES:
                        raise ProbeError(f"{stage}_payload_oversized")
                    body.extend(chunk)
                try:
                    return json.loads(body)
                except (ValueError, TypeError):
                    raise ProbeError(f"{stage}_payload_invalid") from None
    except (TimeoutError, httpx.TimeoutException):
        raise ProbeError(f"{stage}_timeout") from None
    except httpx.HTTPError:
        raise ProbeError(f"{stage}_network_error") from None


async def observe_node(stats, client, headers, duration, request_timeout):
    sampling_started = None
    try:
        async with asyncio.timeout(duration):
            url = f"{stats.settings.panel_base}/api/node/{stats.node_id}/logs"
            timeout = httpx.Timeout(connect=request_timeout, read=None, write=request_timeout, pool=request_timeout)
            request = client.build_request("GET", url, headers=headers, timeout=timeout)
            response = await asyncio.wait_for(client.send(request, stream=True), timeout=request_timeout)
            try:
                if response.status_code != 200:
                    stats.outcome = f"http_{response.status_code}"
                    return
                stats.connected = True
                stats.started_at = time.time()
                sampling_started = time.monotonic()
                pending = b""
                oversized = False
                async for chunk in response.aiter_bytes():
                    parts = chunk.split(b"\n")
                    for index, part in enumerate(parts):
                        complete = index < len(parts) - 1
                        if len(pending) + len(part) > MAX_LINE_BYTES:
                            if not oversized:
                                stats.reasons["oversized_record"] += 1
                            oversized, pending = True, b""
                        elif not oversized:
                            pending += part
                        if complete:
                            if not oversized and pending.startswith(b"data: "):
                                stats.observe(pending[6:].rstrip(b"\r").decode("utf-8", errors="replace"), time.time())
                            pending, oversized = b"", False
                stats.outcome = "stream_ended"
            finally:
                await response.aclose()
    except TimeoutError:
        stats.outcome = "sample_complete" if stats.connected else "connection_deadline"
    except httpx.TimeoutException:
        stats.outcome = "stream_timeout"
    except httpx.HTTPError:
        stats.outcome = "stream_network_error"
    finally:
        stats.finished_at = time.time()
        stats.elapsed = time.monotonic() - sampling_started if sampling_started is not None else 0.0


def print_json(value):
    print(json.dumps(value, sort_keys=True), flush=True)


async def diagnose(
    settings, client, *, duration=8, concurrency=4, total_timeout=180, request_timeout=8, nodes=None, emit=print_json
):
    """List the fleet and observe relevant nodes using only the configured API key."""
    emit(settings.summary())
    emit(
        {
            "kind": "probe",
            "utc_epoch": time.time(),
            "duration_per_node_seconds": duration,
            "max_concurrent_streams": concurrency,
            "total_deadline_seconds": total_timeout,
            "selected_node_ids": sorted(nodes) if nodes is not None else "all",
        }
    )
    stats_list = []
    try:
        async with asyncio.timeout(total_timeout):
            key = settings.panel_api_key.strip()
            if not key or not key.isascii() or any(ord(char) < 33 or ord(char) == 127 for char in key):
                raise ProbeError("configuration_panel_api_key_invalid")
            headers = {"X-Api-Key": key, "Accept": "text/event-stream"}
            fleet = await request_json(
                client, "GET", settings.panel_base + "/api/nodes", "nodes", request_timeout, headers=headers
            )
            if isinstance(fleet, dict):
                fleet = fleet.get("nodes", fleet.get("data"))
            if (
                not isinstance(fleet, list)
                or len(fleet) > 4096
                or any(
                    not isinstance(node, dict) or type(node.get("id")) is not int or node["id"] <= 0 for node in fleet
                )
                or len({node["id"] for node in fleet}) != len(fleet)
            ):
                raise ProbeError("nodes_payload_invalid")
            semaphore = asyncio.Semaphore(concurrency)

            async def worker(node):
                status = node.get("status")
                status = status if isinstance(status, str) and status in STATUSES else "unknown"
                stats = NodeStats(node["id"], status, settings, time.time())
                stats_list.append(stats)
                try:
                    if nodes is not None and stats.node_id not in nodes:
                        stats.outcome = "excluded_selection"
                    elif stats.node_id in settings.disabled_nodes:
                        stats.outcome = "excluded_local"
                    elif stats.status in {"disabled", "limited"}:
                        stats.outcome = "excluded_panel"
                    else:
                        async with semaphore:
                            await observe_node(stats, client, headers, duration, request_timeout)
                finally:
                    try:
                        emit(stats.summary())
                    finally:
                        stats.seen.clear()

            await asyncio.gather(*(worker(node) for node in fleet))
    except ProbeError as error:
        emit({"kind": "error", "code": error.code})
        return
    except TimeoutError:
        emit({"kind": "error", "code": "total_deadline"})
        return
    emit({"kind": "complete", "nodes_reported": len(stats_list), "utc_epoch": time.time()})


def main():
    """Keep command-line errors and unexpected dependency failures free of secrets."""
    logging.disable(logging.CRITICAL)

    class PrivateArgumentParser(argparse.ArgumentParser):
        def error(self, _message):
            raise ProbeError("arguments_invalid")

    parser = PrivateArgumentParser(description=__doc__)
    parser.add_argument("--duration", type=float, default=8, help="Seconds per node, 1 through 15 (default 8).")
    parser.add_argument("--nodes", help="Optional comma-separated panel node IDs; default is the full fleet.")
    parser.add_argument("--request-timeout", type=float, default=8, help="HTTP request deadline, 1 through 15 seconds.")
    try:
        args = parser.parse_args()
        if not all(math.isfinite(value) and 1 <= value <= 15 for value in (args.duration, args.request_timeout)):
            raise ProbeError("arguments_invalid")
        if args.nodes is not None and not re.fullmatch(r"[0-9]+(?:,[0-9]+)*", args.nodes):
            raise ProbeError("arguments_invalid")
        nodes = frozenset(int(item) for item in args.nodes.split(",")) if args.nodes is not None else None
        if nodes is not None and any(item <= 0 for item in nodes):
            raise ProbeError("arguments_invalid")
        settings = load_settings()

        async def run():
            async with httpx.AsyncClient(verify=settings.verify_ssl, timeout=args.request_timeout) as client:
                await diagnose(
                    settings, client, duration=args.duration, request_timeout=args.request_timeout, nodes=nodes
                )

        asyncio.run(run())
    except ProbeError as error:
        print_json({"kind": "error", "code": error.code})
    except KeyboardInterrupt:
        print_json({"kind": "error", "code": "interrupted"})
    except Exception:
        print_json({"kind": "error", "code": "probe_failed"})


if __name__ == "__main__":
    main()
