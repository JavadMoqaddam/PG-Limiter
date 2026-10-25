"""Completed scan receipts preserve the coverage actually used by enforcement."""

from dataclasses import FrozenInstanceError

import pytest

from utils.monitoring_gate import get_last_completed_scan, monitoring_snapshot, record_completed_scan


def test_completed_receipt_freezes_partial_node_coverage(operational_topics):
    required = {1, 2, 9}
    eligible = {1, 2}
    record_completed_scan(
        monitoring_snapshot(),
        "logs",
        7,
        coverage_complete=False,
        required_node_ids=required,
        eligible_node_ids=eligible,
        device_count_mode="node",
    )
    receipt = get_last_completed_scan()
    required.clear()
    eligible.add(9)
    assert receipt.coverage_complete is False
    assert receipt.required_node_ids == frozenset({1, 2, 9})
    assert receipt.eligible_node_ids == frozenset({1, 2})
    assert receipt.device_count_mode == "node"
    with pytest.raises(FrozenInstanceError):
        receipt.coverage_complete = True


def test_status_and_health_explain_historical_partial_coverage(operational_topics):
    from telegram_bot.handlers.status_reports import ReportFacts, render_health, render_status

    record_completed_scan(
        monitoring_snapshot(),
        "logs",
        7,
        coverage_complete=False,
        required_node_ids={1, 2, 9},
        eligible_node_ids={1, 2},
        device_count_mode="node",
    )
    facts = ReportFacts(
        config={"ip_source": "logs", "device_count_mode": "node"},
        admission=monitoring_snapshot(),
        completed=get_last_completed_scan(),
        attempt=None,
        collection=None,
        config_revision=0,
    )
    for render in (render_status, render_health):
        text = render(facts)
        assert "partial" in text.lower()
        assert "2/3" in text
        assert "Missing node IDs: 9" in text


def test_complete_receipt_labels_full_coverage(operational_topics):
    from telegram_bot.handlers.status_reports import ReportFacts, render_status

    record_completed_scan(
        monitoring_snapshot(), "logs", 0, required_node_ids={1}, eligible_node_ids={1}, device_count_mode="node"
    )
    facts = ReportFacts(
        config={"ip_source": "logs", "device_count_mode": "node"},
        admission=monitoring_snapshot(),
        completed=get_last_completed_scan(),
        attempt=None,
        collection=None,
        config_revision=0,
    )
    assert "complete" in render_status(facts).lower()
    assert "1/1" in render_status(facts)
