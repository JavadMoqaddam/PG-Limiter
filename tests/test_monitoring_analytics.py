"""The detailed-monitoring view calls analyze_user_activity_patterns; the warning
system must actually provide it with a stable contract."""

from utils.warning_system.enhanced_system import EnhancedWarningSystem
from utils.warning_system.user_warning import UserLimitWarning


def _system(tmp_path):
    return EnhancedWarningSystem(
        filename=str(tmp_path / "warnings.json"),
        history_filename=str(tmp_path / "history.json"),
    )


async def test_analyze_user_activity_patterns_contract(tmp_path):
    system = _system(tmp_path)
    warning = UserLimitWarning(
        username="u",
        ip_count=2,
        ips={"1.1.1.1", "2.2.2.2"},
        warning_time=0.0,
        monitoring_end_time=1000.0,
    )
    warning.monitoring_history = [
        {"timestamp": 0.0, "ips": {"1.1.1.1"}, "ip_count": 1},
        {"timestamp": 60.0, "ips": {"1.1.1.1", "2.2.2.2"}, "ip_count": 2},
    ]
    warning.ip_first_seen = {"1.1.1.1": 0.0, "2.2.2.2": 60.0}
    warning.ip_last_seen = {"1.1.1.1": 300.0, "2.2.2.2": 120.0}
    system.warnings["u"] = warning

    result = await system.analyze_user_activity_patterns("u")

    assert result["total_snapshots"] == 2
    assert result["peak_ip_count"] == 2
    assert result["average_ip_count"] == 1.5
    # Only 1.1.1.1 spans >= 4 minutes.
    assert result["consistently_active_ips"] == {"1.1.1.1"}
    assert isinstance(result["ip_change_frequency"], float)


async def test_analyze_unknown_user_returns_empty_contract(tmp_path):
    system = _system(tmp_path)
    result = await system.analyze_user_activity_patterns("nobody")
    assert result["total_snapshots"] == 0
    assert result["consistently_active_ips"] == set()
    assert result["peak_ip_count"] == 0
