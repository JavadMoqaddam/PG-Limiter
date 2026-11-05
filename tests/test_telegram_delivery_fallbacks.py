"""Direct Telegram delivery must not append after an ambiguous acknowledgement."""

import asyncio
import threading
import warnings
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter, TimedOut
from telegram.warnings import PTBDeprecationWarning


def _rate_limit_error():
    """PTB reads its deprecated retry_after property internally during construction."""
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message=r".*attribute `retry_after` will be of type.*",
            category=PTBDeprecationWarning,
        )
        return RetryAfter(timedelta(seconds=3))


@pytest.fixture(params=["response", "card"])
def deliver(request):
    from telegram_bot.handlers.status_reports import deliver_card
    from telegram_bot.utils import send_response

    return send_response if request.param == "response" else deliver_card


def callback_boundary(error=None, *, accepted=False, answer_error=None, replacement_error=None):
    """Model remote edits and appends independently of the helper under test."""
    remote = {41: "old card"}

    async def edit(**kwargs):
        if accepted or error is None:
            remote[41] = kwargs["text"]
        if error is not None:
            raise error

    async def reply(**kwargs):
        if replacement_error is not None:
            raise replacement_error
        remote[max(remote) + 1] = kwargs["text"]

    query = SimpleNamespace(
        answer=AsyncMock(side_effect=answer_error),
        edit_message_text=edit,
        message=SimpleNamespace(reply_text=reply),
    )
    return SimpleNamespace(callback_query=query), remote


async def test_accepted_edit_with_lost_receipt_does_not_append(deliver):
    update, remote = callback_boundary(TimedOut("synthetic lost acknowledgement"), accepted=True)
    caught = None
    try:
        await deliver(update, "updated card", None)
    except TimedOut as error:
        caught = error
    assert remote == {41: "updated card"}
    assert isinstance(caught, TimedOut)


@pytest.mark.parametrize(
    "error",
    [
        Forbidden("bot cannot edit this chat"),
        BadRequest("Can't parse entities: unsupported start tag"),
        NetworkError("synthetic network interruption"),
        _rate_limit_error(),
    ],
)
async def test_edit_failure_without_unavailable_message_does_not_append(deliver, error):
    update, remote = callback_boundary(error)
    with pytest.raises(type(error)):
        await deliver(update, "updated card", None)
    assert remote == {41: "old card"}


@pytest.mark.parametrize(
    "description", ["Message to edit not found", "Message can't be edited", "Message can not be edited"]
)
async def test_definitely_unavailable_edit_allows_one_replacement(deliver, description):
    update, remote = callback_boundary(BadRequest(description))
    await deliver(update, "replacement card", None)
    assert remote == {41: "old card", 42: "replacement card"}


async def test_unchanged_edit_is_success_without_replacement(deliver):
    update, remote = callback_boundary(BadRequest("Message is not modified"))
    await deliver(update, "old card", None)
    assert remote == {41: "old card"}


async def test_callback_acknowledgement_is_best_effort(deliver):
    update, remote = callback_boundary(answer_error=TimedOut("callback acknowledgement lost"))
    await deliver(update, "updated card", None)
    assert remote == {41: "updated card"}


async def test_definite_replacement_failure_is_observable(deliver):
    update, remote = callback_boundary(
        BadRequest("Message to edit not found"), replacement_error=Forbidden("replacement rejected")
    )
    with pytest.raises(Forbidden, match="replacement rejected"):
        await deliver(update, "replacement card", None)
    assert remote == {41: "old card"}


async def test_regular_message_still_replies(deliver):
    replies = []

    async def reply(**kwargs):
        replies.append(kwargs)

    await deliver(SimpleNamespace(message=SimpleNamespace(reply_text=reply)), "new card", None)
    assert replies == [{"text": "new card", "reply_markup": None, "parse_mode": "HTML"}]


@pytest.fixture
def backup_boundary(tmp_path, monkeypatch):
    import telegram_bot.handlers.backup as backup
    import telegram_bot.main as main
    import telegram_bot.topics as topics
    import telegram_bot.utils as bot_utils

    directory = tmp_path / "dummy-backup"
    directory.mkdir()
    archive = directory / "synthetic.zip"
    archive.write_bytes(b"synthetic archive without credentials")
    state = SimpleNamespace(
        calls=[], copies=[], group_error=None, accept_group=False, admin_error=None, cleanup_calls=[]
    )
    manager = SimpleNamespace(enabled=True, group_id=-100123, get_topic_id=lambda topic: 17)

    async def send_document(**kwargs):
        assert kwargs["document"].read() == b"synthetic archive without credentials"
        destination = (kwargs["chat_id"], kwargs.get("message_thread_id"))
        state.calls.append(destination)
        error = state.group_error if destination[0] < 0 else state.admin_error
        if error is None or (destination[0] < 0 and state.accept_group):
            state.copies.append(destination)
        if error is not None:
            raise error
        return SimpleNamespace(message_id=len(state.copies))

    monkeypatch.setattr(backup, "get_auto_backup_config", lambda: {"interval_hours": 2})
    monkeypatch.setattr(
        backup, "_sync_create_auto_backup_zip", lambda hours: (str(archive), archive.name, str(directory))
    )
    monkeypatch.setattr(main, "application", SimpleNamespace(bot=SimpleNamespace(send_document=send_document)))
    monkeypatch.setattr(topics, "get_topics_manager", lambda: manager)
    monkeypatch.setattr(bot_utils, "check_admin", AsyncMock(return_value=[71, 72]))
    return backup, state, manager, directory


async def test_group_accepted_then_timed_out_never_contacts_admins(backup_boundary):
    backup, state, _, directory = backup_boundary
    state.group_error = TimedOut("synthetic lost acknowledgement")
    state.accept_group = True
    result = await backup.send_automatic_backup()
    assert state.copies == [(-100123, 17)]
    assert state.calls == [(-100123, 17)]
    assert result is False
    assert not directory.exists()


async def test_confirmed_group_delivery_survives_cleanup_failure(backup_boundary, monkeypatch):
    backup, state, _, directory = backup_boundary

    def fail_cleanup(path, **kwargs):
        state.cleanup_calls.append(path)
        raise OSError("synthetic cleanup failure")

    monkeypatch.setattr(backup.shutil, "rmtree", fail_cleanup)
    assert await backup.send_automatic_backup() is True
    assert state.copies == [(-100123, 17)]
    assert state.calls == [(-100123, 17)]
    assert state.cleanup_calls == [str(directory)]


async def test_confirmed_group_delivery_survives_file_close_failure(backup_boundary, monkeypatch):
    backup, state, _, directory = backup_boundary

    class FailingClose:
        def __enter__(self):
            self.file = (directory / "synthetic.zip").open("rb")
            return self.file

        def __exit__(self, *args):
            self.file.close()
            raise OSError("synthetic close failure after receipt")

    monkeypatch.setattr(backup, "open", lambda *args: FailingClose(), raising=False)
    assert await backup.send_automatic_backup() is True
    assert state.calls == [(-100123, 17)]
    assert state.copies == [(-100123, 17)]
    assert not directory.exists()


async def test_backup_pins_group_before_resolving_thread(backup_boundary):
    backup, state, manager, _ = backup_boundary

    def topic_id(topic):
        manager.group_id = -100999
        return 17

    manager.get_topic_id = topic_id
    assert await backup.send_automatic_backup() is True
    assert state.copies == [(-100123, 17)]


@pytest.mark.parametrize(
    "error",
    [
        Forbidden("bot was kicked from the group"),
        BadRequest("Chat not found"),
        BadRequest("Message thread not found"),
        BadRequest("TOPIC_CLOSED"),
    ],
)
async def test_definite_group_destination_rejection_falls_back_to_admins(backup_boundary, error):
    backup, state, _, directory = backup_boundary
    state.group_error = error
    assert await backup.send_automatic_backup() is True
    assert state.calls == [(-100123, 17), (71, None), (72, None)]
    assert state.copies == [(71, None), (72, None)]
    assert not directory.exists()


@pytest.mark.parametrize(
    "error",
    [
        NetworkError("synthetic interruption"),
        _rate_limit_error(),
        BadRequest("Can't parse entities: bad HTML"),
    ],
)
async def test_other_group_errors_do_not_change_destination(backup_boundary, error):
    backup, state, _, directory = backup_boundary
    state.group_error = error
    assert await backup.send_automatic_backup() is False
    assert state.calls == [(-100123, 17)]
    assert state.copies == []
    assert not directory.exists()


@pytest.mark.parametrize("enabled,group_id", [(False, -100123), (True, None)])
async def test_disabled_or_unconfigured_topics_send_to_all_admins(backup_boundary, enabled, group_id):
    backup, state, manager, directory = backup_boundary
    manager.enabled, manager.group_id = enabled, group_id
    assert await backup.send_automatic_backup() is True
    assert state.copies == [(71, None), (72, None)]
    assert not directory.exists()


async def test_admin_rejections_still_clean_up(backup_boundary):
    backup, state, manager, directory = backup_boundary
    manager.enabled = False
    state.admin_error = Forbidden("synthetic admin rejection")
    assert await backup.send_automatic_backup() is False
    assert state.calls == [(71, None), (72, None)]
    assert state.copies == []
    assert not directory.exists()


@pytest.mark.parametrize("failure", ["group", "admin", "interrupted", "cleanup"])
async def test_automatic_backup_diagnostics_exclude_exception_payload(backup_boundary, monkeypatch, caplog, failure):
    backup, state, manager, _ = backup_boundary
    payload = "SYNTHETIC_PRIVATE_BACKUP_PAYLOAD"
    if failure == "group":
        state.group_error = Forbidden(payload)
        error_name, action, result, delivered = "Forbidden", "send_group", "destination_rejected", True
    elif failure == "admin":
        manager.enabled = False
        state.admin_error = Forbidden(payload)
        error_name, action, result, delivered = "Forbidden", "send_admin", "failed", False
    elif failure == "interrupted":
        state.group_error = NetworkError(payload)
        error_name, action, result, delivered = "NetworkError", "send", "interrupted", False
    else:

        def fail_cleanup(*args, **kwargs):
            raise OSError(payload)

        monkeypatch.setattr(backup.shutil, "rmtree", fail_cleanup)
        error_name, action, result, delivered = "OSError", "cleanup", "failed", True

    assert await backup.send_automatic_backup() is delivered
    assert payload not in caplog.text
    assert f"action={action}" in caplog.text
    assert f"result={result}" in caplog.text
    assert f"error={error_name}" in caplog.text


async def test_cancelled_group_delivery_cleans_up_without_admin_fallback(backup_boundary):
    backup, state, _, directory = backup_boundary
    state.group_error = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await backup.send_automatic_backup()
    assert state.calls == [(-100123, 17)]
    assert not directory.exists()


@pytest.mark.parametrize("cancellations", [1, 2])
async def test_cancellation_during_archive_creation_cleans_up_worker_result(
    backup_boundary, monkeypatch, cancellations
):
    backup, state, _, directory = backup_boundary
    loop = asyncio.get_running_loop()
    started, finished = asyncio.Event(), asyncio.Event()
    release = threading.Event()

    def create_archive(hours):
        loop.call_soon_threadsafe(started.set)
        release.wait(timeout=5)
        loop.call_soon_threadsafe(finished.set)
        return str(directory / "synthetic.zip"), "synthetic.zip", str(directory)

    monkeypatch.setattr(backup, "_sync_create_auto_backup_zip", create_archive)
    task = asyncio.create_task(backup.send_automatic_backup())
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        for _ in range(cancellations):
            task.cancel()
            await asyncio.sleep(0)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.wait_for(finished.wait(), timeout=5)
    finally:
        release.set()
    assert state.calls == []
    assert not directory.exists()


@pytest.mark.parametrize("cancellations", [1, 2])
async def test_queued_backup_cleanup_survives_caller_cancellation(backup_boundary, monkeypatch, cancellations):
    import telegram_bot.main as main

    backup, state, _, directory = backup_boundary
    loop = asyncio.get_running_loop()
    occupied, cleanup_queued = asyncio.Event(), asyncio.Event()
    release = threading.Event()
    blocking_future = None

    class ObservedExecutor(ThreadPoolExecutor):
        cleanup_expected = False

        def submit(self, fn, *args, **kwargs):
            future = super().submit(fn, *args, **kwargs)
            if self.cleanup_expected:
                cleanup_queued.set()
            return future

    executor = ObservedExecutor(max_workers=1)
    loop.set_default_executor(executor)

    def occupy_worker():
        loop.call_soon_threadsafe(occupied.set)
        release.wait(timeout=5)

    send_document = main.application.bot.send_document

    async def send_then_occupy_executor(**kwargs):
        nonlocal blocking_future
        receipt = await send_document(**kwargs)
        blocking_future = loop.run_in_executor(None, occupy_worker)
        await asyncio.wait_for(occupied.wait(), timeout=5)
        executor.cleanup_expected = True
        return receipt

    monkeypatch.setattr(main.application.bot, "send_document", send_then_occupy_executor)
    task = asyncio.create_task(backup.send_automatic_backup())
    try:
        await asyncio.wait_for(cleanup_queued.wait(), timeout=5)
        for _ in range(cancellations):
            task.cancel()
            await asyncio.sleep(0)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
        if blocking_future is not None:
            await blocking_future
    assert state.copies == [(-100123, 17)]
    assert state.calls == [(-100123, 17)]
    assert not directory.exists()


@pytest.mark.parametrize(
    "error,expected",
    [
        (BadRequest("Message to edit not found"), True),
        (BadRequest("Message can't be edited"), True),
        (BadRequest("MESSAGE CAN NOT BE EDITED"), True),
        (BadRequest("Message is not modified"), False),
        (BadRequest("Can't parse entities"), False),
        (TimedOut("Message to edit not found"), False),
        (NetworkError("Message can't be edited"), False),
        (Forbidden("Message can not be edited"), False),
        (RuntimeError("Message to edit not found"), False),
        (_rate_limit_error(), False),
        (asyncio.CancelledError(), False),
    ],
)
def test_message_unavailable_classifier_requires_specific_bad_request(error, expected):
    from telegram_bot.delivery_errors import is_message_unavailable

    assert is_message_unavailable(error) is expected
