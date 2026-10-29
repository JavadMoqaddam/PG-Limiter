"""Cycle reports distinguish transport coverage from usable user evidence."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


@pytest.fixture
def cycle_stats():
    return {
        "ran_at": 1_800_000_000,
        "duration_ms": 242_000,
        "collection_outcome": "partial",
        "skipped_reason": "all user evidence unknown",
        "candidates": 2181,
        "prefiltered": 2181,
        "fetched_ok": 2181,
        "fetched_failed": 0,
        "not_found": 0,
        "forbidden": 0,
        "coverage": 1.0,
        "users_with_ips": 0,
        "total_ips": 0,
        "nodes_seen": 14,
        "nodes_expected": 16,
        "node_evidence_evaluated": True,
        "node_coverage": 0.875,
        "stale_ips": 2395,
        "future_ips": 0,
        "unknown_age_ips": 0,
        "unknown_users": 2181,
        "null_node_users": 0,
        "unknown_age_users": 0,
        "future_timestamp_users": 0,
        "consecutive_unsuccessful_cycles": 2,
        "geo_lookups": 0,
    }


async def test_last_cycle_separates_missing_users_from_global_node_absence(monkeypatch, cycle_stats):
    import telegram_bot.handlers.settings_ip_source as settings
    import utils.ip_source_api as api

    monkeypatch.setattr(settings, "read_config", AsyncMock(return_value={"ip_source": "logs"}))
    monkeypatch.setattr(api, "get_last_cycle_stats", lambda: cycle_stats)
    query = SimpleNamespace(edit_message_text=AsyncMock())
    await settings.handle_ip_source_stats_callback(query, SimpleNamespace())
    text = query.edit_message_text.call_args.kwargs["text"]

    assert "242000ms" in text
    assert "HTTP lookups: <code>2181</code> OK / <code>0</code> failed" in text
    assert "Users with unusable evidence: <code>2181</code>" in text
    assert "Nodes reporting across responses: <code>14/16</code>" in text
    assert "Nodes absent across all responses: <code>2</code>" in text
    assert "Consecutive API outage cycles: <code>2</code>" in text
    assert "2395 stale" in text
    assert "Historical" in text


async def test_cycle_skip_reason_is_escaped_before_telegram_html(monkeypatch, cycle_stats):
    import telegram_bot.handlers.settings_ip_source as settings
    import utils.ip_source_api as api

    cycle_stats["skipped_reason"] = "<b>incomplete & unknown</b>"
    monkeypatch.setattr(settings, "read_config", AsyncMock(return_value={"ip_source": "api"}))
    monkeypatch.setattr(api, "get_last_cycle_stats", lambda: cycle_stats)
    query = SimpleNamespace(edit_message_text=AsyncMock())
    await settings.handle_ip_source_stats_callback(query, SimpleNamespace())
    text = query.edit_message_text.call_args.kwargs["text"]

    assert "&lt;b&gt;incomplete &amp; unknown&lt;/b&gt;" in text
    assert "<b>incomplete & unknown</b>" not in text


@pytest.mark.parametrize("outcome,fallback", [("failure", False), ("superseded", False), ("partial", True)])
async def test_unusable_or_reverted_cycle_is_not_announced_as_available(monkeypatch, cycle_stats, outcome, fallback):
    import telegram_bot.handlers.settings_ip_source as settings
    import utils.ip_source_api as api

    cycle_stats.update(collection_outcome=outcome, fallback_applied=fallback, skipped_reason="")
    monkeypatch.setattr(settings, "read_config", AsyncMock(return_value={"ip_source": "logs" if fallback else "api"}))
    monkeypatch.setattr(api, "get_last_cycle_stats", lambda: cycle_stats)
    query = SimpleNamespace(edit_message_text=AsyncMock())
    await settings.handle_ip_source_stats_callback(query, SimpleNamespace())
    text = query.edit_message_text.call_args.kwargs["text"]

    assert "Sample available for enforcement" not in text
    assert "⚠️" in text


async def test_early_http_failure_does_not_report_unevaluated_nodes_as_absent(monkeypatch, cycle_stats):
    """Default zero node counts are unknown until node maps have been evaluated."""
    import telegram_bot.handlers.settings_ip_source as settings
    import utils.ip_source_api as api

    cycle_stats.update(
        fetched_ok=0,
        fetched_failed=2181,
        nodes_seen=0,
        node_evidence_evaluated=False,
        skipped_reason="no successful lookups",
    )
    monkeypatch.setattr(settings, "read_config", AsyncMock(return_value={"ip_source": "api"}))
    monkeypatch.setattr(api, "get_last_cycle_stats", lambda: cycle_stats)
    query = SimpleNamespace(edit_message_text=AsyncMock())
    await settings.handle_ip_source_stats_callback(query, SimpleNamespace())
    text = query.edit_message_text.call_args.kwargs["text"]

    assert "Nodes reporting across responses: unknown (expected <code>16</code>)" in text
    assert "Nodes absent across all responses: unknown" in text
    assert "Nodes absent across all responses: <code>16</code>" not in text
