"""Snapshot pruning must preserve warnings when original timestamps are unknown."""

import pytest

from test_log_enforcement_freshness import fresh_cycle, partial_cycle, policy_cycle, settings_db  # noqa: F401


@pytest.mark.parametrize("fresh_cycle", [True], indirect=True)
@pytest.mark.parametrize("invalid", ["future", "nan", "positive_infinity", "negative_infinity"])
async def test_pre_snapshot_timestamp_uncertainty_preserves_warning(fresh_cycle, invalid):
    cycle = fresh_cycle
    cycle.advance(300)
    record = cycle.warning()
    user = cycle.observed("alice", age=0)
    if invalid == "future":
        cycle.clock[0] -= 1
    else:
        stamp = {
            "nan": float("nan"),
            "positive_infinity": float("inf"),
            "negative_infinity": float("-inf"),
        }[invalid]
        for connection in user.device_info.connections:
            connection.last_seen = stamp

    await cycle.run([user])

    assert cycle.system.warnings.get("alice") is record
    assert record.consecutive_violations == 2
    assert record.trust_score == 80.0
    assert cycle.applied == []


@pytest.mark.parametrize("fresh_cycle", [True], indirect=True)
async def test_future_timestamp_preserves_warning_until_original_source_expiry(fresh_cycle):
    cycle = fresh_cycle
    cycle.advance(300)
    record = cycle.warning()
    user = cycle.observed("alice", age=0)
    cycle.clock[0] -= 1
    await cycle.run([user])
    assert cycle.system.warnings.get("alice") is record

    cycle.advance(20)
    await cycle.run([])
    assert cycle.system.warnings.get("alice") is record
    assert record.consecutive_violations == 2
    assert record.trust_score == 80.0
    assert cycle.applied == []

    cycle.advance(1)
    await cycle.run([])
    assert cycle.system.warnings == {}
