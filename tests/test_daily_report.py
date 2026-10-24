"""Calendar windows, compact summaries and durable daily delivery."""

from datetime import date, datetime, timezone
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest

from tests.test_daily_punishment_facts import fact_database  # noqa: F401


@pytest.mark.parametrize(
    "day,zone,hours",
    [
        (date(2026, 3, 8), "America/New_York", 23),
        (date(2026, 11, 1), "America/New_York", 25),
        (date(2018, 11, 4), "America/Sao_Paulo", 23),
        (date(2011, 12, 30), "Pacific/Apia", 0),
        (date(2026, 10, 23), "Asia/Tehran", 24),
    ],
)
def test_calendar_window_uses_actual_midnights(day, zone, hours):
    from utils.daily_report import calendar_window

    start, end = calendar_window(day, ZoneInfo(zone))
    assert (end - start).total_seconds() == hours * 3600
    assert start.tzinfo == timezone.utc


def test_tz_unset_defaults_utc_but_explicit_invalid_pauses(monkeypatch):
    from utils.daily_report import configured_zone

    monkeypatch.delenv("TZ", raising=False)
    assert configured_zone().key == "UTC"
    monkeypatch.setenv("TZ", "Asia/Tehran")
    assert configured_zone().key == "Asia/Tehran"
    monkeypatch.setenv("TZ", "")
    assert configured_zone() is None
    monkeypatch.setenv("TZ", "Does/NotExist")
    assert configured_zone() is None


def test_summary_is_private_and_one_message_even_with_many_steps():
    from utils.daily_report import render_summary

    summary = render_summary(date(2026, 10, 23), "UTC", {None: 2, 0: 1, 3: 3}, 4)
    assert "Step 1: 1" in summary and "Step 4: 3" in summary
    assert "Plain ban: 2" in summary and "Total: 4 users" in summary
    assert "Total: 0 users" in render_summary(date(2026, 10, 23), "UTC", {}, 0)
    huge = render_summary(date(2026, 10, 23), "UTC", {i: 100 for i in range(10000)}, 100)
    assert len(huge.encode("utf-16-le")) // 2 <= 2000
    assert "Other steps" in huge


@pytest.fixture
async def report_service(fact_database, tmp_path, monkeypatch):
    import telegram_bot.topics as topics
    from utils.daily_report import DailyReportService
    from utils.punishment_facts import PunishmentFactJournal
    from telegram_bot.dispatcher import TelegramDispatcher

    monkeypatch.setenv("TZ", "UTC")
    manager = topics.TopicsManager()
    manager._enabled = True
    manager._group_id = -100123
    manager._topics = {"general": 1, "daily_report": 9}
    monkeypatch.setattr(topics, "_topics_manager", manager)
    import telegram_bot.dispatcher as dispatcher

    monkeypatch.setattr(dispatcher, "get_topics_manager", lambda: manager)
    monkeypatch.setattr(dispatcher, "PENDING_NOTIFICATIONS_FILE", str(tmp_path / "pending.json"))
    dispatch = TelegramDispatcher()
    dispatch.set_bot(MagicMock(send_message=AsyncMock(return_value=MagicMock(message_id=77))))
    journal = PunishmentFactJournal(str(tmp_path / "facts.json"))
    service = DailyReportService(dispatch, journal)
    return service, dispatch, manager


async def test_unresolved_pre_midnight_completion_delays_daily_run(report_service, fact_database):
    from db.crud.punishment_facts import PunishmentFactsCRUD
    from db.models import DailyReportRun

    service, dispatch, manager = report_service
    now = datetime(2026, 10, 24, tzinfo=timezone.utc)
    fact = dict(
        incident_id="late",
        username="alice",
        step_index=2,
        started_at=now.timestamp() - 1,
        applied_at=now.timestamp() - 1,
        disable_succeeded=True,
        revoke_succeeded=False,
        uuid_reset_succeeded=False,
        state="prepared",
    )
    async with fact_database() as db:
        await PunishmentFactsCRUD.store(db, fact)
        await db.commit()
    assert not await service.report_previous_day(now)
    assert dispatch.queue.empty()
    async with fact_database() as db:
        await PunishmentFactsCRUD.store(db, {**fact, "state": "completed"})
        await db.commit()
    assert await service.report_previous_day(now)
    async with fact_database() as db:
        run = await db.get(DailyReportRun, "2026-10-23")
        assert "Step 3: 1" in run.payload
        assert run.start_utc == datetime(2026, 10, 23, tzinfo=timezone.utc).timestamp()


async def test_delivered_run_survives_cache_and_destination_change(report_service, fact_database):
    import asyncio
    from db.models import DailyReportRun
    from utils.daily_report import DailyReportService

    service, dispatch, manager = report_service
    worker = asyncio.create_task(dispatch.start_worker())
    try:
        assert await service.report_previous_day(datetime(2026, 10, 24, tzinfo=timezone.utc))
        for _ in range(100):
            async with fact_database() as db:
                row = await db.get(DailyReportRun, "2026-10-23")
                if row.state == "delivered":
                    assert row.message_id == 77
                    break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("report receipt was not durably persisted")
        manager._group_id = -100999
        manager._destination_revision += 1
        manager._message_cache = {}
        restarted = DailyReportService(dispatch, service.journal)
        await restarted.report_previous_day(datetime(2026, 10, 24, tzinfo=timezone.utc))
        assert dispatch._bot.send_message.call_count == 1
    finally:
        await dispatch.stop()
        await worker


async def test_topics_off_creates_only_checkpoint_and_no_disabled_day_backlog(report_service, fact_database):
    from db.models import DailyReportRun

    service, dispatch, manager = report_service
    manager._enabled = False
    assert not await service.report_previous_day(datetime(2026, 10, 24, tzinfo=timezone.utc))
    async with fact_database() as db:
        assert await db.get(DailyReportRun, "2026-10-23") is None
    manager._enabled = True
    assert not await service.report_previous_day(datetime(2026, 10, 24, tzinfo=timezone.utc))
    assert dispatch.queue.empty()


async def test_known_sending_crash_is_uncertain_and_not_automatically_resent(report_service, fact_database):
    from db.models import DailyReportRun
    from utils.daily_report import DailyReportService

    service, dispatch, manager = report_service
    await service.report_previous_day(datetime(2026, 10, 24, tzinfo=timezone.utc))
    async with fact_database() as db:
        row = await db.get(DailyReportRun, "2026-10-23")
        row.state = "sending"
        await db.commit()
    dispatch._events.clear()
    restarted = DailyReportService(dispatch, service.journal)
    assert not await restarted.report_previous_day(datetime(2026, 10, 24, tzinfo=timezone.utc))
    async with fact_database() as db:
        assert (await db.get(DailyReportRun, "2026-10-23")).state == "uncertain"


async def test_scheduler_replaces_midnight_on_live_tz_change_and_rejects_old_callback(monkeypatch):
    from utils.daily_report_scheduler import DailyReportScheduler

    now = datetime(2026, 10, 23, 20, tzinfo=timezone.utc)
    jobs = []

    class Job:
        def __init__(self, callback, when, data, name):
            self.callback, self.when, self.data, self.name = callback, when, data, name
            self.removed = False

        def schedule_removal(self):
            self.removed = True

    class Queue:
        def run_once(self, callback, when, *, data, name):
            job = Job(callback, when, data, name)
            jobs.append(job)
            return job

        def run_repeating(self, *args, **kwargs):
            return MagicMock()

    service = MagicMock(report_previous_day=AsyncMock(), retry_pending=AsyncMock())
    scheduler = DailyReportScheduler(service, clock=lambda: now)
    app = MagicMock(job_queue=Queue(), bot_data={})
    monkeypatch.setenv("TZ", "UTC")
    await scheduler.start(app)
    old = jobs[-1]
    assert old.when == datetime(2026, 10, 24, tzinfo=timezone.utc)
    monkeypatch.setenv("TZ", "Asia/Tehran")
    await scheduler.watch(None)
    assert old.removed
    assert jobs[-1].when == datetime(2026, 10, 23, 20, 30, tzinfo=timezone.utc)
    before = service.report_previous_day.call_count
    now = datetime(2026, 10, 24, tzinfo=timezone.utc)
    await old.callback(MagicMock(job=old))
    assert service.report_previous_day.call_count == before
    current = jobs[-1]
    await current.callback(MagicMock(job=current))
    assert service.report_previous_day.call_count == before + 1


async def test_scheduler_invalid_tz_pauses_and_start_is_one_owner(monkeypatch):
    from utils.daily_report_scheduler import start_daily_report_scheduler

    monkeypatch.setenv("TZ", "Does/NotExist")
    app = MagicMock(job_queue=MagicMock(), bot_data={})
    one = await start_daily_report_scheduler(app)
    two = await start_daily_report_scheduler(app)
    assert one is two
    assert app.job_queue.run_once.call_count == 0
    assert app.job_queue.run_repeating.call_count == 1


async def test_receipt_settles_when_worker_cancelled_during_send(report_service, fact_database):
    import asyncio
    from db.models import DailyReportRun

    service, dispatch, manager = report_service
    sending = asyncio.Event()
    release = asyncio.Event()

    async def send(**kwargs):
        sending.set()
        await release.wait()
        return MagicMock(message_id=88)

    dispatch._bot.send_message = AsyncMock(side_effect=send)
    await service.report_previous_day(datetime(2026, 10, 24, tzinfo=timezone.utc))
    worker = asyncio.create_task(dispatch.start_worker())
    await asyncio.wait_for(sending.wait(), 1)
    worker.cancel()
    release.set()
    await worker
    async with fact_database() as db:
        row = await db.get(DailyReportRun, "2026-10-23")
        assert row.state == "delivered" and row.message_id == 88
    assert dispatch._bot.send_message.call_count == 1


@pytest.mark.parametrize("change", ["pause", "destination", "tz"])
async def test_queued_report_is_skipped_when_admission_changes(report_service, fact_database, monkeypatch, change):
    import asyncio
    from db.models import DailyReportRun

    service, dispatch, manager = report_service
    await service.report_previous_day(datetime(2026, 10, 24, tzinfo=timezone.utc))
    if change == "pause":
        manager._enabled = False
    elif change == "destination":
        manager._destination_revision += 1
    else:
        monkeypatch.setenv("TZ", "Asia/Tehran")
    worker = asyncio.create_task(dispatch.start_worker())
    await asyncio.wait_for(dispatch.queue.join(), 1)
    await dispatch.stop()
    await worker
    async with fact_database() as db:
        assert (await db.get(DailyReportRun, "2026-10-23")).state == "skipped"
    dispatch._bot.send_message.assert_not_called()


async def test_network_timeout_remains_uncertain_without_resending(report_service, fact_database):
    import asyncio
    from db.models import DailyReportRun
    from telegram.error import TimedOut

    service, dispatch, manager = report_service
    dispatch._bot.send_message = AsyncMock(side_effect=TimedOut("lost response"))
    await service.report_previous_day(datetime(2026, 10, 24, tzinfo=timezone.utc))
    worker = asyncio.create_task(dispatch.start_worker())
    await asyncio.wait_for(dispatch.queue.join(), 1)
    await dispatch.stop()
    await worker
    async with fact_database() as db:
        assert (await db.get(DailyReportRun, "2026-10-23")).state == "uncertain"
    assert dispatch._bot.send_message.call_count == 1


async def test_recent_restart_recovery_does_not_replay_all_missed_dates(report_service, fact_database):
    from db.crud.daily_reports import DailyReportsCRUD
    from db.models import DailyReportRun
    from sqlalchemy import select

    service, dispatch, manager = report_service
    await DailyReportsCRUD.advance_checkpoint("2026-10-01", True)
    await service.report_previous_day(datetime(2026, 10, 24, 12, tzinfo=timezone.utc))
    async with fact_database() as db:
        dates = list(await db.scalars(select(DailyReportRun.report_date)))
        assert dates == ["2026-10-23"]


async def test_actual_midnight_retries_waiting_report_after_late_fact(report_service, fact_database):
    from db.crud.punishment_facts import PunishmentFactsCRUD
    from db.models import DailyReportRun

    service, dispatch, manager = report_service
    now = datetime(2026, 10, 24, tzinfo=timezone.utc)
    fact = dict(
        incident_id="late",
        username="alice",
        step_index=2,
        started_at=now.timestamp() - 1,
        applied_at=now.timestamp() - 1,
        disable_succeeded=True,
        revoke_succeeded=False,
        uuid_reset_succeeded=False,
        state="prepared",
    )
    async with fact_database() as db:
        await PunishmentFactsCRUD.store(db, fact)
        await db.commit()
    assert not await service.report_previous_day(now)
    async with fact_database() as db:
        await PunishmentFactsCRUD.store(db, {**fact, "state": "completed"})
        await db.commit()
    assert await service.retry_pending(now)
    async with fact_database() as db:
        assert "Step 3: 1" in (await db.get(DailyReportRun, "2026-10-23")).payload


async def test_day_after_topics_off_midnight_reports_when_enabled_again(report_service):
    service, dispatch, manager = report_service
    manager._enabled = False
    await service.report_previous_day(datetime(2026, 10, 24, tzinfo=timezone.utc))
    manager._enabled = True
    assert await service.report_previous_day(datetime(2026, 10, 25, tzinfo=timezone.utc))


async def test_environment_watcher_recovers_a_missed_midnight_callback(monkeypatch):
    from utils.daily_report_scheduler import DailyReportScheduler

    now = datetime(2026, 10, 23, 23, 59, tzinfo=timezone.utc)
    service = MagicMock(report_previous_day=AsyncMock(), retry_pending=AsyncMock())
    scheduler = DailyReportScheduler(service, clock=lambda: now)
    app = MagicMock(job_queue=MagicMock(), bot_data={})
    monkeypatch.setenv("TZ", "UTC")
    await scheduler.start(app)
    before = service.report_previous_day.call_count
    now = datetime(2026, 10, 24, 0, 2, tzinfo=timezone.utc)
    await scheduler.watch(None)
    assert service.report_previous_day.call_count == before + 1
    assert app.job_queue.run_once.call_count == 2


@pytest.mark.parametrize("change", ["pause", "destination", "tz"])
async def test_actual_send_rechecks_admission_after_physical_sending_commit(
    report_service, fact_database, tmp_path, monkeypatch, change
):
    """A setting transition during the real SQL commit precedes transport admission."""
    import asyncio
    import threading

    import aiosqlite
    import telegram_bot.topics as topics
    from db.crud.daily_reports import DailyReportsCRUD

    service, dispatch, manager = report_service
    monkeypatch.setattr(topics, "TOPICS_FILE", str(tmp_path / "topics.json"))
    monkeypatch.setattr(topics, "MESSAGE_CACHE_FILE", str(tmp_path / "cache.json"))
    assert await service.report_previous_day(datetime(2026, 10, 24, tzinfo=timezone.utc))
    loop = asyncio.get_running_loop()
    entered, finished = asyncio.Event(), asyncio.Event()
    release = threading.Event()
    execute = aiosqlite.Connection._execute
    intercepted = False

    async def suspend_real_commit(connection, function, *args, **kwargs):
        nonlocal intercepted
        if getattr(function, "__name__", None) == "commit":

            def physical_commit():
                nonlocal intercepted
                row = function.__self__.execute(
                    "SELECT state FROM daily_report_runs WHERE report_date = '2026-10-23'"
                ).fetchone()
                if row == ("sending",) and not intercepted:
                    intercepted = True
                    loop.call_soon_threadsafe(entered.set)
                    if not release.wait(5):
                        raise RuntimeError("physical sending commit was not released")
                    result = function(*args, **kwargs)
                    loop.call_soon_threadsafe(finished.set)
                    return result
                return function(*args, **kwargs)

            return await execute(connection, physical_commit)
        return await execute(connection, function, *args, **kwargs)

    monkeypatch.setattr(aiosqlite.Connection, "_execute", suspend_real_commit)
    worker = asyncio.create_task(dispatch.start_worker())
    try:
        await asyncio.wait_for(entered.wait(), 2)
        dispatch._bot.send_message.assert_not_called()

        async def transition():
            if change == "pause":
                await manager.set_enabled(False)
            elif change == "destination":
                await manager.set_group_id(-100999)
            else:
                monkeypatch.setenv("TZ", "Asia/Tehran")

        await asyncio.wait_for(asyncio.create_task(transition()), 2)
        dispatch._bot.send_message.assert_not_called()
        release.set()
        await asyncio.wait_for(dispatch.queue.join(), 2)
        assert finished.is_set()
        dispatch._bot.send_message.assert_not_called()
        run = await DailyReportsCRUD.get("2026-10-23")
        assert run["state"] == "skipped" and run["message_id"] is None
        assert dispatch._events == {}
    finally:
        release.set()
        await dispatch.stop()
        await worker


@pytest.mark.parametrize("change", ["pause", "destination", "tz"])
async def test_started_report_keeps_its_receipt_after_admission_changes(
    report_service, fact_database, tmp_path, monkeypatch, change
):
    """Settings changes after transport starts preserve its owned pinned receipt."""
    import asyncio
    import telegram_bot.topics as topics
    from db.crud.daily_reports import DailyReportsCRUD

    service, dispatch, manager = report_service
    monkeypatch.setattr(topics, "TOPICS_FILE", str(tmp_path / "topics.json"))
    monkeypatch.setattr(topics, "MESSAGE_CACHE_FILE", str(tmp_path / "cache.json"))
    entered, release = asyncio.Event(), asyncio.Event()

    async def send(**kwargs):
        entered.set()
        await release.wait()
        return MagicMock(message_id=77)

    dispatch._bot.send_message = AsyncMock(side_effect=send)
    assert await service.report_previous_day(datetime(2026, 10, 24, tzinfo=timezone.utc))
    worker = asyncio.create_task(dispatch.start_worker())
    try:
        await asyncio.wait_for(entered.wait(), 2)
        if change == "pause":
            await manager.set_enabled(False)
        elif change == "destination":
            await manager.set_group_id(-100999)
        else:
            monkeypatch.setenv("TZ", "Asia/Tehran")
        worker.cancel()
        await asyncio.sleep(0)
        worker.cancel()
        release.set()
        await worker
        run = await DailyReportsCRUD.get("2026-10-23")
        assert run["state"] == "delivered" and run["message_id"] == 77
        assert dispatch._bot.send_message.call_count == 1
        assert dispatch._bot.send_message.call_args.kwargs["chat_id"] == -100123
        assert dispatch._bot.send_message.call_args.kwargs["message_thread_id"] == 9
        assert dispatch._events == {}
    finally:
        release.set()
        await dispatch.stop()
        await worker


def test_midnight_crossing_gap_resolves_to_first_real_date_instant():
    from datetime import timedelta
    from utils.daily_report import calendar_window, next_midnight

    zone = ZoneInfo("America/Toronto")
    first_real = datetime(1919, 3, 31, 4, 30, tzinfo=timezone.utc)
    assert (first_real - timedelta(microseconds=1)).astimezone(zone).date() == date(1919, 3, 30)
    assert first_real.astimezone(zone).isoformat() == "1919-03-31T00:30:00-04:00"
    assert calendar_window(date(1919, 3, 30), zone)[1] == first_real
    assert calendar_window(date(1919, 3, 31), zone)[0] == first_real
    assert next_midnight(datetime(1919, 3, 31, 4, tzinfo=timezone.utc), zone) == first_real


@pytest.mark.parametrize(
    "day,zone,expected",
    [
        (date(2026, 11, 1), "America/Havana", datetime(2026, 11, 1, 4, tzinfo=timezone.utc)),
        (date(2018, 11, 4), "America/Sao_Paulo", datetime(2018, 11, 4, 3, tzinfo=timezone.utc)),
        (date(2011, 12, 30), "Pacific/Apia", datetime(2011, 12, 30, 10, tzinfo=timezone.utc)),
    ],
)
def test_calendar_boundary_preserves_midnight_fold_gap_and_skipped_date(day, zone, expected):
    from utils.daily_report import calendar_window

    assert calendar_window(day, ZoneInfo(zone))[0] == expected


async def test_ban_after_crossing_midnight_gap_belongs_only_to_its_actual_date(fact_database):
    from db.crud.punishment_facts import PunishmentFactsCRUD
    from utils.daily_report import calendar_window

    success = datetime(1919, 3, 31, 4, 45, tzinfo=timezone.utc).timestamp()
    async with fact_database() as db:
        await PunishmentFactsCRUD.store(
            db,
            dict(
                incident_id="crossing",
                username="alice",
                step_index=2,
                started_at=success,
                applied_at=success,
                disable_succeeded=True,
                revoke_succeeded=False,
                uuid_reset_succeeded=False,
                state="completed",
            ),
        )
        await db.commit()
        previous = calendar_window(date(1919, 3, 30), ZoneInfo("America/Toronto"))
        current = calendar_window(date(1919, 3, 31), ZoneInfo("America/Toronto"))
        assert await PunishmentFactsCRUD.aggregate(db, *(instant.timestamp() for instant in previous)) == ({}, 0)
        assert await PunishmentFactsCRUD.aggregate(db, *(instant.timestamp() for instant in current)) == ({2: 1}, 1)
