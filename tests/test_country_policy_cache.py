"""Country verdicts must follow current policy while sharing cached IP facts."""

from unittest.mock import AsyncMock

from cachetools import TTLCache
import pytest

import utils.ip_facts as facts
import utils.parse_logs as parser


@pytest.fixture
def lookup(monkeypatch):
    monkeypatch.setattr(facts, "COUNTRY_CACHE", TTLCache(maxsize=10, ttl=43200))
    monkeypatch.setattr(facts, "VERDICT_CACHE", TTLCache(maxsize=10, ttl=86400))
    remote = AsyncMock(return_value="DE")
    monkeypatch.setattr(parser, "lookup_country", remote)
    return remote


async def test_country_change_reuses_fact_without_reusing_verdict(lookup):
    assert await facts.is_ip_accepted("5.6.7.8", {"country_code": "DE"})
    assert not await facts.is_ip_accepted("5.6.7.8", {"country_code": "TR"})
    assert await facts.is_ip_accepted("5.6.7.8", {"country_code": "de"})
    lookup.assert_awaited_once_with("5.6.7.8")


@pytest.mark.parametrize("disabled", ["", "off", "any"])
async def test_enabling_country_filter_does_not_reuse_unfiltered_verdict(lookup, disabled):
    assert await facts.is_ip_accepted("5.6.7.8", {"country_code": disabled})
    assert not await facts.is_ip_accepted("5.6.7.8", {"country_code": "TR"})
    lookup.assert_awaited_once_with("5.6.7.8")


async def test_disabling_country_filter_does_not_reuse_rejection(lookup):
    assert not await facts.is_ip_accepted("5.6.7.8", {"country_code": "TR"})
    assert await facts.is_ip_accepted("5.6.7.8", {"country_code": "off"})
    lookup.assert_awaited_once_with("5.6.7.8")


async def test_unknown_country_remains_retryable_under_same_policy(lookup):
    lookup.side_effect = [None, "DE"]
    assert await facts.is_ip_accepted("5.6.7.8", {"country_code": "TR"})
    assert not await facts.is_ip_accepted("5.6.7.8", {"country_code": "TR"})
    assert lookup.await_count == 2


async def test_monitored_country_mirror_uses_current_policy(lookup):
    assert await facts.is_ip_accepted("5.6.7.8", {"monitoring": {"country_code": "DE"}})
    assert not await facts.is_ip_accepted("5.6.7.8", {"monitoring": {"country_code": "TR"}})
