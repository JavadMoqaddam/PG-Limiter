"""Midnight ownership must survive the real Telegram/APS date-job lifecycle."""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from apscheduler.events import EVENT_JOB_EXECUTED
from telegram.ext import Application

from utils.daily_report_scheduler import DailyReportScheduler


class Reports:
    def __init__(self):
        self.reports = []
        self.retries = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()
        self.error = None

    async def report_previous_day(self, now, **kwargs):
        self.reports.append((now, kwargs))
        self.started.set()
        await self.release.wait()
        if self.error:
            raise self.error

    async def retry_pending(self, now):
        self.retries.append(now)
        if self.error:
            raise self.error


@pytest.fixture
async def scheduled(monkeypatch):
    monkeypatch.setenv("TZ", "UTC")
    app = Application.builder().token("123456:synthetic").build()
    app.job_queue.scheduler.start(paused=True)
    errors = []

    async def on_error(_update, context):
        errors.append(context.error)

    app.add_error_handler(on_error)
    clock = [datetime.now(timezone.utc)]
    service = Reports()
    owner = DailyReportScheduler(service, clock=lambda: clock[0])
    owner.application = app
    owner._reschedule()
    try:
        yield SimpleNamespace(app=app, clock=clock, service=service, owner=owner, errors=errors)
    finally:
        service.release.set()
        await app.job_queue.stop()


def midnight_jobs(runtime):
    return runtime.app.job_queue.get_jobs_by_name("daily_report_midnight")


async def test_automatically_removed_date_job_schedules_one_next_midnight(scheduled):
    r = scheduled
    original = r.owner.midnight_job
    r.clock[0] = r.owner.due
    finished = asyncio.Event()

    def completed(event):
        if event.job_id == original.id:
            finished.set()

    r.app.job_queue.scheduler.add_listener(completed, EVENT_JOB_EXECUTED)
    r.app.job_queue.scheduler.modify_job(
        original.id, next_run_time=datetime.now(timezone.utc) + timedelta(milliseconds=5)
    )
    r.app.job_queue.scheduler.resume()
    await asyncio.wait_for(finished.wait(), 5)
    assert r.errors == []
    assert len(r.service.reports) == 1
    assert len(midnight_jobs(r)) == 1
    assert midnight_jobs(r)[0].id != original.id
    assert r.owner.due == r.clock[0] + timedelta(days=1)


@pytest.mark.parametrize("first", ["midnight", "watcher"])
async def test_callback_and_misfire_recovery_claim_boundary_once(scheduled, first):
    r = scheduled
    job = r.owner.midnight_job
    r.clock[0] = r.owner.due
    r.app.job_queue.scheduler.remove_job(job.id)
    r.service.release.clear()
    context = SimpleNamespace(job=job)
    call = r.owner.midnight(context) if first == "midnight" else r.owner.watch(None)
    task = asyncio.create_task(call)
    try:
        await asyncio.wait_for(r.service.started.wait(), 5)
        if first == "midnight":
            await asyncio.wait_for(r.owner.watch(None), 1)
        else:
            await asyncio.wait_for(r.owner.midnight(context), 1)
    finally:
        r.service.release.set()
        await asyncio.wait_for(task, 5)
    assert len(r.service.reports) == 1
    assert len(midnight_jobs(r)) == 1


async def test_old_callback_finishing_after_timezone_change_preserves_replacement(scheduled, monkeypatch):
    r = scheduled
    job = r.owner.midnight_job
    r.clock[0] = r.owner.due
    r.app.job_queue.scheduler.remove_job(job.id)
    r.service.release.clear()
    task = asyncio.create_task(r.owner.midnight(SimpleNamespace(job=job)))
    try:
        await asyncio.wait_for(r.service.started.wait(), 5)
        monkeypatch.setenv("TZ", "Asia/Tehran")
        await r.owner.watch(None)
        replacement = r.owner.midnight_job
        generation = r.owner.generation
    finally:
        r.service.release.set()
        await asyncio.wait_for(task, 5)
    assert r.owner.midnight_job is replacement
    assert r.owner.generation == generation
    assert len(midnight_jobs(r)) == 1


@pytest.mark.parametrize("path", ["midnight", "watcher"])
async def test_failed_report_keeps_next_midnight_scheduled(scheduled, path):
    r = scheduled
    job = r.owner.midnight_job
    r.clock[0] = r.owner.due
    r.app.job_queue.scheduler.remove_job(job.id)
    r.service.error = RuntimeError("report failed")
    if path == "midnight":
        with pytest.raises(RuntimeError, match="report failed"):
            await r.owner.midnight(SimpleNamespace(job=job))
    else:
        await r.owner.watch(None)
    assert len(midnight_jobs(r)) == 1
    assert r.owner.due == r.clock[0] + timedelta(days=1)


async def test_failed_retry_and_stale_callback_leave_current_job_intact(scheduled, monkeypatch):
    r = scheduled
    old = r.owner.midnight_job
    monkeypatch.setenv("TZ", "Asia/Tehran")
    await r.owner.watch(None)
    replacement = r.owner.midnight_job
    r.service.error = RuntimeError("retry failed")
    await r.owner.watch(None)
    await r.owner.midnight(SimpleNamespace(job=old))
    assert r.owner.midnight_job is replacement
    assert len(midnight_jobs(r)) == 1
    assert r.service.reports == []
