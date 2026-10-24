"""Topics admission and source retirement, independent of Telegram connectivity."""

import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from telegram_bot.topics import get_topics_manager
from utils.logs import get_logger

gate_logger = get_logger("limiter.monitoring")


@dataclass(frozen=True)
class MonitoringAdmission:
    """A Topics configuration captured before asynchronous observation work."""

    allowed: bool
    reason: str
    revision: int
    manager_id: int
    group_id: int | None


@dataclass(frozen=True)
class CompletedScan:
    """Historical receipt written only by a completed, admitted usage scan."""

    completed_at: float
    source: str
    users_counted: int
    admission: MonitoringAdmission
    config_revision: int
    protected_users_count: int
    metadata_unknown_users_count: int
    failed_users_count: int


@dataclass(frozen=True)
class CycleOutcome:
    """Latest finished attempt, distinct from the last completed scan."""

    finished_at: float
    source: str
    outcome: str
    reason: str
    admission: MonitoringAdmission
    config_revision: int


_fallback_manager = None
_fallback_state = None
_fallback_revision = 0
_synchronized_admission: MonitoringAdmission | None = None
_sync_lock = asyncio.Lock()
_warning_attempt = None
_last_completed_scan: CompletedScan | None = None
_last_cycle_outcome: CycleOutcome | None = None


def monitoring_snapshot() -> MonitoringAdmission:
    """Read explicit Topics state; network availability never pauses enforcement."""
    global _fallback_manager, _fallback_state, _fallback_revision
    manager = get_topics_manager()
    enabled, group_id = bool(manager.enabled), manager.group_id
    state = (enabled, group_id)
    if manager is not _fallback_manager:
        _fallback_manager, _fallback_state, _fallback_revision = manager, state, 0
    elif state != _fallback_state:
        _fallback_state = state
        _fallback_revision += 1
    revision = getattr(manager, "monitoring_revision", _fallback_revision)
    allowed = enabled and bool(group_id)
    reason = "" if allowed else ("Topics are disabled" if not enabled else "No forum group is configured")
    return MonitoringAdmission(allowed, reason, revision, id(manager), group_id)


def monitoring_is_current(admission: MonitoringAdmission) -> bool:
    """Reject paused work and work started before a Topics configuration change."""
    current = monitoring_snapshot()
    return current.allowed and current == admission


@asynccontextmanager
async def monitoring_admission_fence(admission: MonitoringAdmission) -> AsyncIterator[bool]:
    """Order an observation transaction against durable Topics mutations.

    TopicsManager._update owns _write_lock through persistence and publication.
    Holding that same lock lets an admitted history transaction finish before
    a pause can be acknowledged. Never synchronize sources or mutate Topics
    inside this context; either would invert ownership or reacquire the lock.
    """
    manager = get_topics_manager()
    if id(manager) != admission.manager_id:
        yield False
        return
    async with manager._write_lock:
        yield monitoring_is_current(admission)


async def synchronize_monitoring() -> MonitoringAdmission:
    """Retire old source evidence on transitions without changing warning history."""
    global _synchronized_admission
    from utils.shared_state import clear_node_events, discard_source_observations
    from utils.log_observations import LOG_OBSERVATIONS

    async with _sync_lock:
        current = monitoring_snapshot()
        if current != _synchronized_admission:
            if _synchronized_admission is not None or not current.allowed:
                await discard_source_observations()
                clear_node_events()
                LOG_OBSERVATIONS.register_fleet(None)
                from utils.ip_source_api import retire_api_collection_state

                retire_api_collection_state(current.reason)
            _synchronized_admission = current
        return current


async def warn_paused_admins(admission: MonitoringAdmission) -> None:
    """Attempt one private notice per pause transition or newly available bot."""
    global _warning_attempt
    if admission.allowed:
        _warning_attempt = None
        return
    from telegram_bot import main as bot_main
    from telegram_bot.utils import check_admin

    application = getattr(bot_main, "application", None)
    bot = getattr(application, "bot", None)
    if bot is None or not getattr(application, "running", False):
        _warning_attempt = None
        return
    attempt = (admission, id(application), id(bot))
    if attempt == _warning_attempt:
        return
    _warning_attempt = attempt
    try:
        admins = await check_admin()
        if monitoring_snapshot() != admission:
            return
        for admin in set(admins or []):
            if monitoring_snapshot() != admission:
                return
            if not isinstance(admin, int) or admin <= 0:
                continue
            try:
                async with asyncio.timeout(10):
                    await bot.send_message(
                        chat_id=admin,
                        text=(
                            f"⚠️ User monitoring is paused: {admission.reason}. "
                            "Enable Topics and configure a forum group to resume fresh monitoring. "
                            "Existing timed bans can still be lifted."
                        ),
                    )
            except Exception as error:
                gate_logger.warning(f"Could not send monitoring pause notice to admin {admin}: {error}")
    except Exception as error:
        gate_logger.warning(f"Could not resolve administrators for monitoring pause notice: {error}")


async def watch_monitoring_admission(interval: float = 1.0) -> None:
    """Cancel silent SSE streams promptly, keeping management tasks alive."""
    from utils.get_logs import retire_log_streams

    previous = None
    while True:
        current = monitoring_snapshot()
        if current != previous:
            if previous is not None or not current.allowed:
                await retire_log_streams(current)
            current = await synchronize_monitoring()
            gate_logger.info(
                "User monitoring resumed" if current.allowed else f"User monitoring paused: {current.reason}"
            )
            previous = current
        await warn_paused_admins(current)
        await asyncio.sleep(interval)


async def wait_for_monitoring_change(admission: MonitoringAdmission, timeout: float) -> None:
    """Keep idle collectors responsive to Topics changes without a long sleep."""
    deadline = time.monotonic() + timeout
    while monitoring_snapshot() == admission:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        await asyncio.sleep(min(1.0, remaining))


def record_completed_scan(
    admission: MonitoringAdmission,
    source: str,
    users_counted: int,
    *,
    protected_users_count: int = 0,
    metadata_unknown_users_count: int = 0,
    failed_users_count: int = 0,
) -> None:
    """Publish a real completed scan receipt; rejected scans leave history intact."""
    global _last_completed_scan
    from utils.read_config import get_config_revision

    if monitoring_is_current(admission):
        _last_completed_scan = CompletedScan(
            time.time(),
            source,
            users_counted,
            admission,
            get_config_revision(),
            protected_users_count,
            metadata_unknown_users_count,
            failed_users_count,
        )
        record_cycle_outcome(admission, source, "completed")


def get_last_completed_scan() -> CompletedScan | None:
    """Return the last historical receipt, including its source and admission."""
    return _last_completed_scan


def record_cycle_outcome(
    admission: MonitoringAdmission,
    source: str,
    outcome: str,
    reason: str = "",
    *,
    config_revision: int | None = None,
) -> None:
    """Record a finished attempt without claiming a skipped or failed scan completed."""
    global _last_cycle_outcome
    from utils.read_config import get_config_revision

    _last_cycle_outcome = CycleOutcome(
        time.time(),
        source,
        outcome,
        reason,
        admission,
        get_config_revision() if config_revision is None else config_revision,
    )


def get_last_cycle_outcome() -> CycleOutcome | None:
    """Return the last process-local attempt; absence is not evidence of running."""
    return _last_cycle_outcome
