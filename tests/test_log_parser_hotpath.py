"""Repeated SSE records must reuse bounded work without weakening evidence checks."""

import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import utils.log_observations as observations
import utils.parse_logs as parser
from test_log_history_replay import EPOCH, record
from utils.log_observations import LOG_OBSERVATIONS
from utils.read_config import get_config_revision, invalidate_config_cache
from utils.shared_state import ACTIVE_USERS


@pytest.fixture
def timestamp_work(monkeypatch):
    calls = []

    class CountedDatetime(datetime):
        @classmethod
        def strptime(cls, value, pattern):
            calls.append(value)
            return super().strptime(value, pattern)

        @classmethod
        def fromisoformat(cls, value):
            calls.append(value)
            return super().fromisoformat(value)

    monkeypatch.setattr(observations, "datetime", CountedDatetime)
    helper = getattr(observations, "_printed_second_timestamp", None)
    if helper is not None:
        helper.cache_clear()
    clock = [EPOCH + 36000]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    state = observations.LogObservations()
    state.configure({"log_timezone": "UTC"})
    return SimpleNamespace(state=state, calls=calls, clock=clock)


def test_distinct_fractions_reuse_printed_second_conversion(timestamp_work):
    state = timestamp_work.state
    for fraction in ("000001", "123456", "900000"):
        assert state.timestamp(record("2026/10/23 10:00:20." + fraction), 1) == EPOCH + 20 + int(fraction) / 1e6
    assert len(timestamp_work.calls) == 1


def test_cached_conversion_rechecks_future_time_on_every_record(timestamp_work):
    state = timestamp_work.state
    line = record("2026/10/23 10:00:20.123456")
    assert state.timestamp(line, 1) == EPOCH + 20.123456
    timestamp_work.clock[0] = EPOCH + 20
    with pytest.raises(ValueError, match="future source timestamp"):
        state.timestamp(line, 1)


def test_timestamp_cache_separates_zones_and_invalid_override(timestamp_work):
    state = timestamp_work.state
    state.zones["2"] = state._zone("America/New_York")
    assert state.timestamp(record(), 1) == EPOCH + 20.123456
    assert state.timestamp(record(), 2) == EPOCH + 14420.123456
    state.zones["1"] = None
    with pytest.raises(ValueError, match="missing/invalid source timezone"):
        state.timestamp(record(), 1)


@pytest.mark.parametrize(
    "stamp", ["1969/12/31 23:59:59.999999", "0001/01/01 00:00:00.123456", "9999/12/31 23:59:59.123456"]
)
def test_fractional_timestamp_matches_aware_datetime_at_epoch_boundaries(timestamp_work, stamp):
    timestamp_work.clock[0] = float("inf")
    expected = datetime.strptime(stamp, "%Y/%m/%d %H:%M:%S.%f").replace(tzinfo=timezone.utc).timestamp()
    assert timestamp_work.state.timestamp(record(stamp), 1) == expected


def test_printed_second_cache_is_bounded_under_distinct_source_times(timestamp_work):
    state = timestamp_work.state
    start = datetime(2026, 10, 23, 10)
    for index in range(1100):
        state.timestamp(record((start + timedelta(seconds=index)).strftime("%Y/%m/%d %H:%M:%S")), 1)
    before = len(timestamp_work.calls)
    state.timestamp(record("2026/10/23 10:00:00"), 1)
    assert len(timestamp_work.calls) == before + 1
    state.timestamp(record("2026/10/23 10:00:00"), 1)
    assert len(timestamp_work.calls) == before + 1


@pytest.fixture
def cached_config(monkeypatch, operational_topics):
    clock = [EPOCH + 60]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    config = {"ip_source": "logs", "log_timezone": "UTC", "check_interval": 60, "_revision": get_config_revision()}
    monkeypatch.setattr(parser, "_PARSER_CONFIG_CACHE", None)
    monkeypatch.setattr(parser, "_PARSER_CONFIG_CACHE_TIME", 0)
    monkeypatch.setattr(parser, "_PARSER_CONFIG_REVISION", -1)
    monkeypatch.setattr(parser, "read_config", AsyncMock(return_value=config))
    monkeypatch.setattr(parser, "is_ip_accepted", AsyncMock(return_value=True))
    calls = []
    configure = LOG_OBSERVATIONS.configure

    def counted_configure(data):
        calls.append(data)
        return configure(data)

    monkeypatch.setattr(LOG_OBSERVATIONS, "configure", counted_configure)
    return SimpleNamespace(config=config, calls=calls, clock=clock)


async def test_unchanged_authoritative_default_config_is_not_resigned_for_every_event(cached_config):
    for _ in range(10):
        await parser.parse_logs("", 1, "synthetic")
    assert len(cached_config.calls) == 1


async def test_default_config_revalidates_after_global_retirement(cached_config):
    await parser.parse_logs("", 1, "synthetic")
    LOG_OBSERVATIONS.invalidate()
    await parser.parse_logs("", 1, "synthetic")
    assert len(cached_config.calls) == 2
    assert LOG_OBSERVATIONS.zone(1).key == "UTC"


async def test_default_config_revalidates_after_revision_change(cached_config):
    await parser.parse_logs("", 1, "synthetic")
    await invalidate_config_cache()
    cached_config.config.update(_revision=get_config_revision(), log_timezone="Europe/Istanbul")
    await parser.parse_logs("", 1, "synthetic")
    assert len(cached_config.calls) == 2
    assert LOG_OBSERVATIONS.zone(1).key == "Europe/Istanbul"


async def test_explicit_mutable_config_still_revalidates_nested_changes(cached_config):
    config = dict(cached_config.config, log_node_timezones={"1": "UTC"})
    await parser.parse_logs("", 1, "synthetic", config)
    config["log_node_timezones"]["1"] = "Europe/Istanbul"
    await parser.parse_logs("", 1, "synthetic", config)
    assert len(cached_config.calls) == 2
    assert LOG_OBSERVATIONS.zone(1).key == "Europe/Istanbul"


async def test_access_record_reuses_regex_matches_across_preparation(cached_config, monkeypatch):
    calls = {name: 0 for name in ("EMAIL_REGEX", "IP_V4_REGEX", "IP_V6_REGEX", "INBOUND_REGEX")}

    class CountedPattern:
        def __init__(self, name, pattern):
            self.name, self.pattern = name, pattern

        def search(self, value):
            calls[self.name] += 1
            return self.pattern.search(value)

    for name in calls:
        monkeypatch.setattr(parser, name, CountedPattern(name, getattr(parser, name)))
    await parser.parse_logs("", 1, "synthetic")
    cached_config.clock[0] += 60
    await parser.parse_logs(record("2026/10/23 10:01:20.123456"), 1, "synthetic")
    assert ACTIVE_USERS["alice"].device_info.connections[0].last_seen == EPOCH + 80.123456
    assert calls == {"EMAIL_REGEX": 1, "IP_V4_REGEX": 1, "IP_V6_REGEX": 1, "INBOUND_REGEX": 1}


@pytest.mark.parametrize("raw_email,expected_user", [("1.1234", "1234"), ("1234", "resolved")])
async def test_prepared_email_preserves_numeric_username_and_panel_id_distinction(
    cached_config, monkeypatch, raw_email, expected_user
):
    monkeypatch.setitem(parser.LOCAL_ID_CACHE, "1234", "resolved")
    await parser.parse_logs("", 1, "synthetic")
    cached_config.clock[0] += 60
    line = record("2026/10/23 10:01:20.123456").replace("email: 1.alice", "email: " + raw_email)
    await parser.parse_logs(line, 1, "synthetic")
    assert set(ACTIVE_USERS) == {expected_user}


@pytest.mark.parametrize("use_xff,expected_ip", [(True, "5.6.7.8"), (False, "9.9.9.9")])
async def test_prepared_inbound_keeps_cdn_real_ip_admission(cached_config, use_xff, expected_ip):
    cached_config.config.update(cdn_inbounds=["Vless"], cdn_use_xff=use_xff)
    await parser.parse_logs("", 1, "synthetic")
    cached_config.clock[0] += 60
    line = record("2026/10/23 10:01:20.123456", ip="9.9.9.9") + " X-Forwarded-For: 5.6.7.8"
    await parser.parse_logs(line, 1, "synthetic")
    assert ACTIVE_USERS["alice"].ip == [expected_ip]


async def test_prepared_ipv6_record_keeps_ipv6_over_embedded_ipv4(cached_config):
    await parser.parse_logs("", 1, "synthetic")
    cached_config.clock[0] += 60
    line = record("2026/10/23 10:01:20.123456", ip="[2001:db8::1]") + " X-Forwarded-For: 5.6.7.8"
    await parser.parse_logs(line, 1, "synthetic")
    assert ACTIVE_USERS["alice"].ip == ["2001:db8::1"]
