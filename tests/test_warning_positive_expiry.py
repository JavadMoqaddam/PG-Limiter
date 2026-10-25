"""Positive expired observations keep their record until the scan-gap rule runs."""

import json
import time
from pathlib import Path

import pytest

import db.database as database
import utils.read_config as config
from db.crud.config import ConfigCRUD
from test_config_revisions import settings_db  # noqa: F401
from test_policy_enforcement import policy_cycle  # noqa: F401
from utils.warning_system.user_warning import UserLimitWarning


@pytest.mark.parametrize(
    "max_warnings,prior_count,gap,expected_result,expected_count",
    [(3, 2, 200, "violation_limit_reached", 3), (3, 2, 1000, "updated", 1), (5, 3, 200, "updated", 4)],
)
async def test_expired_positive_record_survives_until_current_scan_gap_decides_streak(
    policy_cycle, monkeypatch, max_warnings, prior_count, gap, expected_result, expected_count
):
    async with database.get_db() as db:
        await ConfigCRUD.set(db, "check_interval", "300")
        await ConfigCRUD.set(db, "max_warning_count", str(max_warnings))
    cfg = await config.read_config(refresh=True)
    system = policy_cycle.system
    system.update_settings(300, max_warnings)
    monkeypatch.setattr(time, "time", lambda: 1200.0)
    previous_scan = 1200.0 - gap
    record = UserLimitWarning(
        username="alice",
        ip_count=2,
        ips={"198.51.100.10"},
        warning_time=previous_scan,
        monitoring_end_time=previous_scan + 180,
        consecutive_violations=prior_count,
        last_scan_time=previous_scan,
        max_warnings_at_creation=3,
        trust_score=80,
        previous_warnings_12h=1,
    )
    system.warnings["alice"] = record
    await system.save_warnings()
    before = json.loads(Path(system.filename).read_text())["alice"]

    disabled, warned = await system.check_persistent_violations(
        policy_cycle.panel, {"alice": record.ips}, cfg, device_counts={"alice": 2}
    )

    assert disabled == warned == set()
    assert policy_cycle.applied == []
    assert system.warnings.get("alice") is record
    assert json.loads(Path(system.filename).read_text())["alice"] == before

    result = await system.add_warning("alice", 2, record.ips, 1, send_telegram_notification=False)

    assert result == expected_result
    assert system.warnings["alice"] is record
    assert record.consecutive_violations == expected_count
    assert record.previous_warnings_12h == 1
    assert record.last_scan_time == 1200.0
