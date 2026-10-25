"""The standalone probe observes source faults without application side effects or secrets."""

import asyncio
import importlib.util
import json
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest

PROBE_PATH = Path(__file__).resolve().parents[1] / "tools" / "diagnose_log_source.py"
EPOCH = datetime(2026, 10, 25, 10, tzinfo=timezone.utc).timestamp()


def load_probe():
    assert PROBE_PATH.is_file(), "The standalone read-only diagnostic is missing"
    spec = importlib.util.spec_from_file_location("log_source_probe_test", PROBE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def access_record(stamp="2026/10/25 10:00:01.123456", *, port=12345, ip="5.6.7.8", user="PRIVATE_USER"):
    return f"{stamp} from {ip}:{port} accepted tcp:PRIVATE_DOMAIN:443 [Vless >> DIRECT] email: 1.{user}"


def settings(probe, **overrides):
    values = {
        "panel_base": "https://PRIVATE_PANEL",
        "username": "PRIVATE_ADMIN",
        "password": "PRIVATE_PASSWORD",
        "general_limit": 2,
        "check_interval": 60,
        "max_warning_count": 3,
        "country_filter_enabled": False,
        "default_timezone": "UTC",
        "node_timezones": {},
        "disabled_nodes": frozenset(),
        "capacity": 10000,
    }
    values.update(overrides)
    return probe.Settings(**values)


def test_reads_only_requested_committed_settings_without_changing_database(tmp_path):
    probe = load_probe()
    path = tmp_path / "limiter.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE config (key TEXT PRIMARY KEY, value TEXT)")
        for key, value in {
            "general_limit": "7",
            "check_interval": "120",
            "max_warning_count": "5",
            "country_code": "off",
            "disabled_nodes": "2,3",
            "api_ip_freshness": "auto",
            "private_setting": "PRIVATE_SECRET",
        }.items():
            connection.execute("INSERT INTO config VALUES (?, ?)", (key, json.dumps(value)))
    original = path.read_bytes()
    modified_at = path.stat().st_mtime_ns

    result = probe.load_settings(
        {
            "DATABASE_URL": f"sqlite+aiosqlite:///{path}",
            "GENERAL_LIMIT": "4",
            "COUNTRY_CODE": "IR",
            "LOG_TIMEZONE": "UTC",
            "LOG_NODE_TIMEZONES": '{"1":"Europe/Istanbul","01":"UTC"}',
            "PANEL_DOMAIN": "PRIVATE_PANEL",
            "PANEL_PASSWORD": "PRIVATE_PASSWORD",
        }
    )

    assert result.general_limit == 7
    assert result.check_interval == 120
    assert result.max_warning_count == 5
    assert not result.country_filter_enabled
    assert result.disabled_nodes == frozenset({2, 3})
    assert result.node_timezones == {"1": "Europe/Istanbul", "01": "UTC"}
    assert path.read_bytes() == original
    assert path.stat().st_mtime_ns == modified_at
    assert set(tmp_path.iterdir()) == {path}
    assert "PRIVATE_SECRET" not in json.dumps(result.summary())


def test_missing_database_is_not_created(tmp_path):
    probe = load_probe()
    path = tmp_path / "absent.db"
    with pytest.raises(probe.ProbeError) as error:
        probe.load_settings({"DATABASE_URL": f"sqlite+aiosqlite:///{path}"})
    assert error.value.code == "configuration_unavailable"
    assert not path.exists()


@pytest.mark.parametrize("general", ["0", "-2", "bad"])
def test_invalid_environment_general_limit_uses_the_application_default(tmp_path, general):
    probe = load_probe()
    path = tmp_path / "limiter.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE config (key TEXT PRIMARY KEY, value TEXT)")
    result = probe.load_settings(
        {
            "DATABASE_URL": f"sqlite+aiosqlite:///{path}",
            "GENERAL_LIMIT": general,
            "PANEL_DOMAIN": "PRIVATE_PANEL",
        }
    )
    assert result.general_limit == 2


def test_timezone_override_keys_match_the_application_exactly():
    probe = load_probe()
    stats = probe.NodeStats(1, "connected", settings(probe, node_timezones={"01": "Europe/Istanbul"}), EPOCH)
    assert stats.zone_name == "UTC"
    override = probe.NodeStats(1, "connected", settings(probe, node_timezones={"1": "INVALID_PRIVATE_ZONE"}), EPOCH)
    override.observe(access_record(), EPOCH + 2)
    assert override.summary(2)["reason_counts"] == {"missing_or_invalid_timezone": 1}
    assert "INVALID_PRIVATE_ZONE" not in json.dumps(override.summary(2))


def test_live_records_preserve_event_time_and_deduplicate_the_entire_record():
    probe = load_probe()
    stats = probe.NodeStats(1, "connected", settings(probe), EPOCH)
    stats.observe(access_record(), EPOCH + 2)
    stats.observe(access_record(), EPOCH + 3)
    stats.observe(access_record(port=54321), EPOCH + 4)
    stats.observe(access_record("2026/10/25 09:59:50"), EPOCH + 4)
    stats.observe(access_record("2026/10/25 09:58:00"), EPOCH + 4)
    report = stats.summary(4)
    assert report["live_distinct_records"] == 2
    assert report["duplicate_records"] == 1
    assert report["recent_history_records"] == 1
    assert report["stale_records"] == 1
    assert report["live_distinct_records_per_second"] == 0.5
    assert report["projected_interval_records_at_sample_rate"] == 30
    assert stats.first_event == EPOCH + 1.123456
    assert report["recent_distinct_records"] == 3
    assert len(stats.seen) == 3


@pytest.mark.parametrize(
    "stamp,zone,reason",
    [
        ("bad time", "UTC", "malformed_timestamp"),
        ("2026/10/25 10:00:01.1234567", "UTC", "malformed_timestamp"),
        ("2026/99/25 10:00:01", "UTC", "invalid_timestamp"),
        ("2026/10/25 10:00:01", None, "missing_or_invalid_timezone"),
        ("2026/11/01 01:30:00", "America/New_York", "ambiguous_or_nonexistent_timestamp"),
    ],
)
def test_timestamp_failures_have_fixed_reasons(stamp, zone, reason):
    probe = load_probe()
    stats = probe.NodeStats(1, "connected", settings(probe, default_timezone=zone), EPOCH)
    stats.observe(access_record(stamp), EPOCH + 2)
    assert stats.summary(2)["reason_counts"] == {reason: 1}


def test_future_clock_error_is_strict_and_reports_the_size_of_the_delta():
    probe = load_probe()
    stats = probe.NodeStats(1, "connected", settings(probe), EPOCH)
    stats.observe(access_record("2026/10/25 10:00:02.000001"), EPOCH + 2)
    stats.observe(access_record("2026/10/25 13:00:02"), EPOCH + 2)
    report = stats.summary(2)
    assert report["reason_counts"] == {"future_timestamp": 2}
    assert report["maximum_positive_clock_delta_seconds"] == 10800
    assert report["live_distinct_records"] == 0


def test_replay_cache_is_bounded_and_projection_does_not_claim_daemon_occupancy():
    probe = load_probe()
    stats = probe.NodeStats(1, "connected", settings(probe, capacity=2), EPOCH)
    for port in (12345, 12346, 12347):
        stats.observe(access_record(port=port), EPOCH + 2)
    report = stats.summary(2)
    assert len(stats.seen) == 2
    assert report["sample_cache_evictions"] == 1
    assert not report["sample_deduplication_complete"]
    assert report["sample_cache_at_capacity"]
    assert report["capacity_estimate"] == "conditional_potential_record_rate_not_daemon_occupancy"


def test_history_age_and_distinct_records_are_measured_without_a_post_start_timestamp(monkeypatch):
    probe = load_probe()
    monkeypatch.setattr(probe.time, "time", lambda: EPOCH + 8)
    stats = probe.NodeStats(1, "connected", settings(probe, check_interval=300), EPOCH)
    history = access_record("2026/10/25 09:58:00")
    stats.observe(history, EPOCH + 1)
    stats.observe(history, EPOCH + 2)
    stats.observe(access_record("2026/10/25 09:54:00"), EPOCH + 3)
    stats.observe(access_record("2026/10/25 09:59:59"), EPOCH + 4)

    report = stats.summary(4)
    assert report["live_distinct_records"] == 0
    assert report["recent_history_records"] == 3
    assert report["stale_records"] == 1
    assert report["recent_distinct_records"] == 2
    assert report["duplicate_records"] == 1
    assert report["sample_cache_entries"] == 2
    assert report["parsed_timestamp_records"] == 4
    assert report["oldest_source_epoch"] == EPOCH - 360
    assert report["newest_source_epoch"] == EPOCH - 1
    assert report["source_event_span_seconds"] == 359
    assert report["minimum_record_age_at_receipt_seconds"] == 5
    assert report["maximum_record_age_at_receipt_seconds"] == 363
    assert report["newest_record_age_at_sample_end_seconds"] == 9
    assert report["sample_cache_scope"] == "recent_at_receipt_including_pre_probe_history"
    assert "PRIVATE" not in json.dumps(report)


def test_receipt_progress_exposes_continuing_delayed_records_without_calling_them_live(monkeypatch):
    probe = load_probe()
    monkeypatch.setattr(probe.time, "time", lambda: EPOCH + 4)
    stats = probe.NodeStats(1, "connected", settings(probe), EPOCH)
    stats.observe("old diagnostic line without an access record", EPOCH + 0.1)
    stats.observe(access_record("2026/10/25 09:59:55"), EPOCH + 1)
    stats.observe(access_record("2026/10/25 09:59:57"), EPOCH + 3)

    report = stats.summary(4)
    assert report["live_distinct_records"] == 0
    assert report["recent_distinct_records"] == 2
    assert report["sse_data_records"] == 3
    assert report["records_by_receipt_second"][:4] == [1, 1, 0, 1]
    assert report["minimum_record_age_at_receipt_seconds"] == 6
    assert report["maximum_record_age_at_receipt_seconds"] == 6
    assert report["source_event_span_seconds"] == 2
    assert report["record_receive_span_seconds"] == pytest.approx(2.9)


def test_buffered_recent_history_deduplication_is_bounded():
    probe = load_probe()
    stats = probe.NodeStats(1, "connected", settings(probe, capacity=2), EPOCH)
    for port in (12345, 12346, 12347):
        stats.observe(access_record("2026/10/25 09:59:50", port=port), EPOCH + 2)
    report = stats.summary(2)
    assert report["live_distinct_records"] == 0
    assert report["recent_distinct_records"] == 3
    assert report["sample_cache_entries"] == 2
    assert report["sample_cache_evictions"] == 1
    assert not report["sample_deduplication_complete"]


def test_empty_samples_do_not_invent_source_ages_and_receipt_bins_stay_bounded():
    probe = load_probe()
    stats = probe.NodeStats(1, "connected", settings(probe), EPOCH)
    report = stats.summary(0)
    assert report["oldest_source_epoch"] is None
    assert report["newest_source_epoch"] is None
    assert report["minimum_record_age_at_receipt_seconds"] is None
    assert report["maximum_record_age_at_receipt_seconds"] is None
    assert report["newest_record_age_at_sample_end_seconds"] is None
    for offset in range(100):
        stats.observe("diagnostic message", EPOCH + offset)
    report = stats.summary(100)
    assert len(report["records_by_receipt_second"]) == 16
    assert sum(report["records_by_receipt_second"]) == 100
    assert report["receipt_bin_overflow_records"] == 84


def test_ignored_records_do_not_create_timestamp_faults():
    probe = load_probe()
    stats = probe.NodeStats(1, "connected", settings(probe), EPOCH)
    stats.observe(access_record("bad time", ip="127.0.0.1"), EPOCH + 2)
    stats.observe(access_record("bad time", user="EOF"), EPOCH + 2)
    stats.observe("bad time [Info] ping", EPOCH + 2)
    assert stats.summary(2)["reason_counts"] == {}


class IdleStream(httpx.AsyncByteStream):
    def __init__(self, counters=None):
        self.closed = False
        self.counters = counters

    async def __aiter__(self):
        if self.counters is not None:
            self.counters["active"] += 1
            self.counters["peak"] = max(self.counters["peak"], self.counters["active"])
        try:
            await asyncio.Event().wait()
            yield b""
        finally:
            if self.counters is not None:
                self.counters["active"] -= 1

    async def aclose(self):
        self.closed = True


@pytest.mark.parametrize("unknown_status", ["PRIVATE_STATUS", ["PRIVATE_STATUS"], {"private": "PRIVATE_STATUS"}])
async def test_http_probe_is_read_only_private_and_honors_node_scope(monkeypatch, unknown_status):
    probe = load_probe()
    monkeypatch.setattr(probe.time, "time", lambda: EPOCH)
    requests = []
    emitted = []

    def respond(request):
        requests.append(request)
        if request.url.path == "/api/admin/token":
            assert parse_qs(request.content.decode()) == {
                "username": ["PRIVATE_ADMIN"],
                "password": ["PRIVATE_PASSWORD"],
            }
            return httpx.Response(200, json={"access_token": "PRIVATE_TOKEN"})
        if request.url.path == "/api/nodes":
            return httpx.Response(
                200,
                json=[
                    {"id": 1, "status": "connected", "name": "PRIVATE_NODE", "address": "PRIVATE_IP"},
                    {"id": 2, "status": "connected"},
                    {"id": 3, "status": "limited"},
                    {"id": 4, "status": unknown_status},
                ],
            )
        assert request.headers["Authorization"] == "Bearer PRIVATE_TOKEN"
        return httpx.Response(200, content=f"data: {access_record('2026/10/25 10:00:00')}\n\n".encode())

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        await probe.diagnose(settings(probe, disabled_nodes=frozenset({2})), client, emit=emitted.append)

    assert [(request.method, request.url.path) for request in requests] == [
        ("POST", "/api/admin/token"),
        ("GET", "/api/nodes"),
        ("GET", "/api/node/1/logs"),
        ("GET", "/api/node/4/logs"),
    ]
    reports = {item["node_id"]: item for item in emitted if item.get("kind") == "node"}
    assert reports[2]["outcome"] == "excluded_local"
    assert reports[3]["outcome"] == "excluded_panel"
    assert reports[4]["status"] == "unknown"
    output = json.dumps(emitted)
    for private in (
        "PRIVATE_ADMIN",
        "PRIVATE_PASSWORD",
        "PRIVATE_TOKEN",
        "PRIVATE_PANEL",
        "PRIVATE_NODE",
        "PRIVATE_USER",
        "PRIVATE_DOMAIN",
        "PRIVATE_IP",
        "PRIVATE_STATUS",
        "5.6.7.8",
    ):
        assert private not in output


async def test_silent_stream_is_closed_at_the_sampling_deadline():
    probe = load_probe()
    stream = IdleStream()

    def respond(request):
        if request.url.path.endswith("/token"):
            return httpx.Response(200, json={"access_token": "PRIVATE_TOKEN"})
        if request.url.path == "/api/nodes":
            return httpx.Response(200, json=[{"id": 1, "status": "connected"}])
        return httpx.Response(200, stream=stream)

    emitted = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        await asyncio.wait_for(probe.diagnose(settings(probe), client, duration=0.03, emit=emitted.append), 0.5)
    assert stream.closed
    assert next(item for item in emitted if item.get("kind") == "node")["outcome"] == "sample_complete"


async def test_connection_headers_obey_the_request_deadline():
    probe = load_probe()
    emitted = []

    async def respond(request):
        if request.url.path.endswith("/token"):
            return httpx.Response(200, json={"access_token": "PRIVATE_TOKEN"})
        if request.url.path == "/api/nodes":
            return httpx.Response(200, json=[{"id": 1, "status": "connected"}])
        await asyncio.Event().wait()

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        await asyncio.wait_for(
            probe.diagnose(settings(probe), client, duration=0.4, request_timeout=0.03, emit=emitted.append),
            0.2,
        )
    assert next(item for item in emitted if item.get("kind") == "node")["outcome"] == "connection_deadline"


async def test_completed_streams_release_replay_memory_after_reporting(monkeypatch):
    probe = load_probe()
    monkeypatch.setattr(probe.time, "time", lambda: EPOCH)
    created = []
    real_stats = probe.NodeStats

    def tracked_stats(*args):
        stats = real_stats(*args)
        created.append(stats)
        return stats

    monkeypatch.setattr(probe, "NodeStats", tracked_stats)

    def respond(request):
        if request.url.path.endswith("/token"):
            return httpx.Response(200, json={"access_token": "PRIVATE_TOKEN"})
        if request.url.path == "/api/nodes":
            return httpx.Response(200, json=[{"id": 1, "status": "connected"}])
        return httpx.Response(200, content=f"data: {access_record('2026/10/25 10:00:00')}\n\n".encode())

    emitted = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        await probe.diagnose(settings(probe), client, emit=emitted.append)
    report = next(item for item in emitted if item.get("kind") == "node")
    assert report["sample_cache_entries"] == 1
    assert all(not stats.seen for stats in created)


async def test_live_rate_uses_observation_time_after_connection_headers(monkeypatch):
    probe = load_probe()
    clock = [0.0]
    monkeypatch.setattr(probe.time, "time", lambda: EPOCH + clock[0])
    monkeypatch.setattr(probe.time, "monotonic", lambda: clock[0])

    class TimedStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            clock[0] = 5
            yield f"data: {access_record('2026/10/25 10:00:05')}\n\n".encode()
            clock[0] = 6

    def respond(request):
        if request.url.path.endswith("/token"):
            return httpx.Response(200, json={"access_token": "PRIVATE_TOKEN"})
        if request.url.path == "/api/nodes":
            return httpx.Response(200, json=[{"id": 1, "status": "connected"}])
        clock[0] = 4
        return httpx.Response(200, stream=TimedStream())

    emitted = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        await probe.diagnose(settings(probe), client, emit=emitted.append)
    report = next(item for item in emitted if item.get("kind") == "node")
    assert report["sample_seconds"] == 2
    assert report["live_distinct_records_per_second"] == 0.5
    assert report["projected_interval_records_at_sample_rate"] == 30


async def test_oversized_stream_line_is_dropped_and_later_records_are_observed(monkeypatch):
    probe = load_probe()
    monkeypatch.setattr(probe.time, "time", lambda: EPOCH)
    emitted = []

    class PayloadStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"data: " + b"x" * probe.MAX_LINE_BYTES
            yield b"PRIVATE_RECORD_REMAINDER\n"
            yield f"data: {access_record('2026/10/25 10:00:00')}\n\n".encode()

    def respond(request):
        if request.url.path.endswith("/token"):
            return httpx.Response(200, json={"access_token": "PRIVATE_TOKEN"})
        if request.url.path == "/api/nodes":
            return httpx.Response(200, json=[{"id": 1, "status": "connected"}])
        return httpx.Response(200, stream=PayloadStream())

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        await probe.diagnose(settings(probe), client, emit=emitted.append)
    result = next(item for item in emitted if item.get("kind") == "node")
    assert result["reason_counts"] == {"oversized_record": 1}
    assert result["live_distinct_records"] == 1
    assert "PRIVATE" not in json.dumps(emitted)


def test_stdin_execution_with_bad_arguments_does_not_echo_private_input():
    load_probe()
    result = subprocess.run(
        [sys.executable, "-", "--duration", "PRIVATE_ARGUMENT"],
        input=PROBE_PATH.read_text(),
        capture_output=True,
        text=True,
        timeout=3,
    )
    assert "PRIVATE_ARGUMENT" not in result.stdout + result.stderr
    assert json.loads(result.stdout)["code"] == "arguments_invalid"


def test_stdin_execution_does_not_create_logs_or_database_when_configuration_is_missing(tmp_path):
    load_probe()
    result = subprocess.run(
        [sys.executable, "-"],
        input=PROBE_PATH.read_text(),
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env={"DATABASE_URL": f"sqlite+aiosqlite:///{tmp_path / 'missing.db'}"},
        timeout=3,
    )
    assert json.loads(result.stdout) == {"kind": "error", "code": "configuration_unavailable"}
    assert result.stderr == ""
    assert list(tmp_path.iterdir()) == []


async def test_concurrency_and_total_runtime_are_bounded_even_with_silent_nodes():
    probe = load_probe()
    counters = {"active": 0, "peak": 0}
    streams = []

    def respond(request):
        if request.url.path.endswith("/token"):
            return httpx.Response(200, json={"access_token": "PRIVATE_TOKEN"})
        if request.url.path == "/api/nodes":
            return httpx.Response(200, json=[{"id": index, "status": "connected"} for index in range(1, 7)])
        stream = IdleStream(counters)
        streams.append(stream)
        return httpx.Response(200, stream=stream)

    emitted = []
    started = time.monotonic()
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        await asyncio.wait_for(
            probe.diagnose(
                settings(probe), client, duration=0.05, concurrency=2, total_timeout=0.08, emit=emitted.append
            ),
            0.5,
        )
    assert time.monotonic() - started < 0.5
    assert counters["peak"] <= 2
    assert counters["active"] == 0
    assert all(stream.closed for stream in streams)
    reports = [item for item in emitted if item.get("kind") == "node"]
    assert {item["node_id"] for item in reports} == set(range(1, 7))
    assert any(item["outcome"] == "total_deadline" for item in reports)


@pytest.mark.parametrize("failure", ["status", "exception"])
async def test_authentication_errors_do_not_expose_exception_or_response_text(failure):
    probe = load_probe()
    emitted = []

    def respond(request):
        if failure == "status":
            return httpx.Response(403, text="PRIVATE_RESPONSE_BODY")
        raise httpx.ConnectError("PRIVATE_EXCEPTION_WITH_TOKEN", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        await probe.diagnose(settings(probe), client, emit=emitted.append)
    assert emitted[-1]["kind"] == "error"
    assert emitted[-1]["code"] in {"auth_http_403", "auth_network_error"}
    assert "PRIVATE" not in json.dumps(emitted)
