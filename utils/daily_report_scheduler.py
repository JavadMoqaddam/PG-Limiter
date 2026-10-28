"""One managed midnight job plus a short watcher for dynamic TZ changes."""

from datetime import datetime, timezone
import os
from typing import Callable

from apscheduler.jobstores.base import JobLookupError

from utils.daily_report import DailyReportService, configured_zone, next_midnight
from utils.logs import get_logger

logger = get_logger("daily_report.scheduler")
SCHEDULER_KEY = "daily_report_scheduler"


class DailyReportScheduler:
    """Use the application's JobQueue lifecycle instead of an unowned infinite task."""

    def __init__(self, service=None, clock: Callable[[], datetime] | None = None):
        self.service = service or DailyReportService()
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.application = None
        self.midnight_job = None
        self.zone_name = None
        self.generation = 0
        self.due = None

    def _reschedule(self) -> None:
        self.generation += 1
        previous_job = self.midnight_job
        self.midnight_job = None
        self.due = None
        if previous_job is not None:
            try:
                previous_job.schedule_removal()
            except JobLookupError:
                # APScheduler removes date jobs before invoking their callback.
                pass
        self.zone_name = os.environ.get("TZ", "UTC")
        zone = configured_zone()
        if zone is None:
            return
        due = next_midnight(self.clock(), zone)
        self.due = due
        self.midnight_job = self.application.job_queue.run_once(
            self.midnight,
            due,
            data={"generation": self.generation, "zone": zone.key, "due": due},
            name="daily_report_midnight",
        )

    async def start(self, application) -> None:
        self.application = application
        self._reschedule()
        application.job_queue.run_repeating(self.watch, interval=60, first=60, name="daily_report_env_watcher")
        # Restart recovery is limited to the previous complete calendar day.
        # Its durable checkpoint excludes dates observed while Topics was off.
        await self.service.report_previous_day(self.clock(), recover=True)

    async def watch(self, context) -> None:
        if os.environ.get("TZ", "UTC") != self.zone_name:
            self._reschedule()
            return
        if configured_zone() is not None:
            try:
                if self.due is not None and self.clock() >= self.due:
                    # Recover the most recent missed boundary if APScheduler
                    # dropped a misfire while the application was busy/suspended.
                    generation = self.generation
                    self.due = None  # Claim this boundary before yielding to the callback.
                    try:
                        await self.service.report_previous_day(self.clock())
                    finally:
                        if generation == self.generation:
                            self._reschedule()
                else:
                    await self.service.retry_pending(self.clock())
            except Exception as error:
                logger.error("Daily report retry remains pending: %s", error)

    async def midnight(self, context) -> None:
        data = context.job.data
        if data["generation"] != self.generation or os.environ.get("TZ", "UTC") != data["zone"]:
            return
        zone = configured_zone()
        if zone is None or self.clock() < data["due"] or self.due != data["due"]:
            return
        # The accepted one-shot is already consumed. Claim its boundary so the
        # watcher cannot independently recover the same occurrence while we await.
        self.midnight_job = None
        self.due = None
        try:
            await self.service.report_previous_day(self.clock())
        finally:
            if data["generation"] == self.generation:
                self._reschedule()


async def start_daily_report_scheduler(application) -> DailyReportScheduler | None:
    """Register exactly one service per application; JobQueue handles stop/shutdown."""
    if application.job_queue is None:
        logger.warning("Daily reports paused: application JobQueue is unavailable")
        return None
    existing = application.bot_data.get(SCHEDULER_KEY)
    if existing is not None:
        return existing
    scheduler = DailyReportScheduler()
    application.bot_data[SCHEDULER_KEY] = scheduler
    await scheduler.start(application)
    return scheduler
