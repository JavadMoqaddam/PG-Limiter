"""Active-warning serialization must snapshot on the event loop.

The writer thread must never iterate the live warnings dict, or a concurrent clear
can race the serialization. save_warnings builds a detached snapshot first.
"""

from utils.warning_system.enhanced_system import EnhancedWarningSystem
from utils.warning_system.user_warning import UserLimitWarning


def _warning(name):
    return UserLimitWarning(
        username=name,
        ip_count=1,
        ips={"1.1.1.1"},
        warning_time=1.0,
        monitoring_end_time=2.0,
    )


def test_concurrent_clear_cannot_race_snapshot(tmp_path):
    system = EnhancedWarningSystem(
        filename=str(tmp_path / "warnings.json"),
        history_filename=str(tmp_path / "history.json"),
    )
    system.warnings = {"a": _warning("a"), "b": _warning("b")}

    snapshot = system._serialize_warnings()
    assert set(snapshot.keys()) == {"a", "b"}

    # A clear happening after the snapshot is taken must not alter it.
    system.warnings.clear()
    assert set(snapshot.keys()) == {"a", "b"}
