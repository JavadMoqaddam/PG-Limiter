"""Calendar-day ban summaries, backed by immutable action facts and run receipts."""

import asyncio
from datetime import date, datetime, time, timedelta, timezone
import os
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from db.crud.daily_reports import DailyReportsCRUD
from db.crud.punishment_facts import PunishmentFactsCRUD
from utils.logs import get_logger
from utils.punishment_facts import get_punishment_fact_journal, settle_owned

logger = get_logger("daily_report")


def configured_zone() -> ZoneInfo | None:
    """Read the current environment; explicitly invalid zones pause reporting."""
    name = os.environ.get("TZ", "UTC")
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        logger.error("Daily reports paused: invalid TZ environment setting")
        return None


def _calendar_boundary(day: date, zone: ZoneInfo) -> datetime:
    """Find the first real instant of a local date, including imaginary midnight."""
    midnight = datetime.combine(day, time.min, zone)
    candidates = sorted({midnight.replace(fold=fold).astimezone(timezone.utc) for fold in (0, 1)})
    valid = [
        candidate
        for candidate in candidates
        if candidate.astimezone(zone).replace(tzinfo=None) == midnight.replace(tzinfo=None)
    ]
    if valid:
        return valid[0]
    # For imaginary midnight, the two offset interpretations bracket the date
    # change. Round-tripping midnight itself can land later inside the new day
    # when a gap starts before midnight. Find the actual transition instead.
    lower, upper = candidates[0], candidates[-1]
    while upper - lower > timedelta(microseconds=1):
        middle = lower + (upper - lower) // 2
        if middle.astimezone(zone).date() >= day:
            upper = middle
        else:
            lower = middle
    # If this date was skipped entirely, this is the next date's boundary;
    # adjacent equal boundaries correctly give an empty calendar window.
    return upper


def calendar_window(day: date, zone: ZoneInfo) -> tuple[datetime, datetime]:
    """Return real UTC calendar boundaries, including midnight gaps/folds."""
    return _calendar_boundary(day, zone), _calendar_boundary(day + timedelta(days=1), zone)


def next_midnight(now: datetime, zone: ZoneInfo) -> datetime:
    """Find the next calendar boundary, rather than adding 86400 seconds."""
    return calendar_window(now.astimezone(zone).date(), zone)[1]


def render_summary(day: date, zone_name: str, counts: dict[int | None, int], total: int) -> str:
    """Keep all output private and within one 2000 UTF-16-unit Telegram message."""
    header = f"Daily bans — {day.isoformat()} ({zone_name})"
    footer = f"Total: {total} users"
    lines = [header]
    entries = sorted(counts.items(), key=lambda pair: -1 if pair[0] is None else pair[0])
    for position, (step, count) in enumerate(entries):
        label = "Plain ban" if step is None else f"Step {step + 1}"
        line = f"{label}: {count} users"
        overflow = f"Other steps: {sum(value for _, value in entries[position:])} user/step counts"
        candidate = "\n".join([*lines, line, overflow, footer])
        if len(candidate.encode("utf-16-le")) // 2 > 2000:
            lines.append(overflow)
            break
        lines.append(line)
    return "\n".join([*lines, footer])


class DailyReportService:
    """Prepare at most one recent eligible day and hand delivery to the dispatcher."""

    def __init__(self, dispatcher=None, journal=None):
        if dispatcher is None:
            from telegram_bot.dispatcher import get_dispatcher

            dispatcher = get_dispatcher()
        self.dispatcher = dispatcher
        self.journal = journal or get_punishment_fact_journal()
        self._lock = asyncio.Lock()

    async def report_previous_day(self, now: datetime, *, recover: bool = False) -> bool:
        if now.tzinfo is None:
            raise ValueError("Daily report clock must be timezone-aware")
        zone = configured_zone()
        if zone is None:
            return False
        return await self._report_date(now.astimezone(zone).date() - timedelta(days=1), zone, recover=recover)

    async def retry_pending(self, now: datetime) -> bool:
        """Retry only an already owned run, never create midday backlog reports."""
        zone = configured_zone()
        if zone is None:
            return False
        latest_date = (now.astimezone(zone).date() - timedelta(days=1)).isoformat()
        key = await DailyReportsCRUD.latest_pending(latest_date)
        if key is None:
            return False
        return await self._report_date(date.fromisoformat(key), zone)

    async def _report_date(self, day: date, zone: ZoneInfo, *, recover: bool = False) -> bool:
        async with self._lock:
            key = day.isoformat()
            from telegram_bot.topics import TopicType, get_topics_manager

            manager = get_topics_manager()
            try:
                run = await DailyReportsCRUD.get(key)
                if not manager.monitoring_allowed or manager.get_topic_id(TopicType.DAILY_REPORT) is None:
                    await settle_owned(DailyReportsCRUD.advance_checkpoint(key, False))
                    if run and run["state"] in {"waiting", "prepared"}:
                        await settle_owned(DailyReportsCRUD.transition(key, "skipped"))
                    return False
                if run is not None:
                    if run["state"] in {"delivered", "skipped", "uncertain", "failed"}:
                        return False
                    if run["event_id"] in self.dispatcher._events:
                        return True
                    if run["state"] == "sending":
                        await settle_owned(DailyReportsCRUD.transition(key, "uncertain"))
                        logger.error("Daily report outcome is uncertain after interrupted send: %s", key)
                        return False
                else:
                    checkpoint = await DailyReportsCRUD.checkpoint()
                    if checkpoint and (checkpoint[0] >= key or (recover and not checkpoint[1])):
                        await settle_owned(DailyReportsCRUD.advance_checkpoint(key, True))
                        return False
                    start, end = calendar_window(day, zone)
                    # Serialize admission with persistent Topics setters. Once this
                    # preparation is owned, later transitions are checked at enqueue/send.
                    async with manager._write_lock:
                        if not manager.monitoring_allowed or configured_zone() != zone:
                            return False
                        thread = manager.get_topic_id(TopicType.DAILY_REPORT)
                        if thread is None:
                            return False
                        run = await settle_owned(
                            DailyReportsCRUD.prepare(
                                dict(
                                    report_date=key,
                                    timezone=zone.key,
                                    start_utc=start.timestamp(),
                                    end_utc=end.timestamp(),
                                    payload=None,
                                    event_id=f"daily:{key}",
                                    state="waiting",
                                    chat_id=manager.group_id,
                                    message_thread_id=thread,
                                    monitoring_revision=manager.monitoring_revision,
                                    destination_revision=manager.destination_revision,
                                )
                            )
                        )
                if run["state"] == "waiting":
                    if not await self.journal.ready_before(run["end_utc"]):
                        logger.warning("Daily report deferred while punishment evidence is unresolved: %s", key)
                        return False
                    from db.database import AsyncSessionLocal

                    async with AsyncSessionLocal() as db:
                        counts, total = await PunishmentFactsCRUD.aggregate(db, run["start_utc"], run["end_utc"])
                    if (
                        not manager.monitoring_allowed
                        or configured_zone() != zone
                        or zone.key != run["timezone"]
                        or manager.monitoring_revision != run["monitoring_revision"]
                        or manager.destination_revision != run["destination_revision"]
                    ):
                        await settle_owned(DailyReportsCRUD.transition(key, "skipped"))
                        return False
                    run = await settle_owned(
                        DailyReportsCRUD.set_payload(key, render_summary(day, run["timezone"], counts, total))
                    )
                admitted = await self.dispatcher.enqueue_daily_report(run)
                await settle_owned(DailyReportsCRUD.advance_checkpoint(key, True))
                return admitted
            except Exception as error:
                logger.error("Daily report remains pending after persistence/delivery admission failure: %s", error)
                return False
