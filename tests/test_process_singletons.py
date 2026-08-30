"""
Tests for the process-wide singletons that used to be rebuilt per event loop.

``limiter.py`` no longer restarts itself in-process: it calls
``asyncio.run(main())`` once and a crash exits non-zero so the supervisor
(start.sh, then Docker's ``restart: always``) starts a fresh interpreter.

Both accessors below used to react to a change of event loop by silently
throwing the old object away, which meant losing whatever it held in memory:
the dispatcher's queued Priority 2/3 backlog and cancel-key bookkeeping, and
the usernames waiting for a panel lookup in the unknown-users queue. These
tests pin the new rule: one object per process, and queued work survives.
"""

import asyncio
import unittest

from telegram_bot import dispatcher as dispatcher_module
from telegram_bot.dispatcher import Priority, get_dispatcher
from utils import user_sync


async def _read_dispatcher():
    """Fetch the singleton from inside a running event loop."""
    return get_dispatcher()


async def _read_queue():
    """Fetch the unknown-users queue from inside a running event loop."""
    return user_sync.get_unknown_users_queue()


class TestDispatcherSingleton(unittest.TestCase):
    """get_dispatcher() must return the same object for the life of the process."""

    def setUp(self):
        self._saved_instance = dispatcher_module._dispatcher_instance
        dispatcher_module._dispatcher_instance = None

    def tearDown(self):
        dispatcher_module._dispatcher_instance = self._saved_instance

    def test_repeated_calls_return_the_same_instance(self):
        self.assertIs(get_dispatcher(), get_dispatcher())

    def test_instance_survives_a_new_event_loop(self):
        first = asyncio.run(_read_dispatcher())
        second = asyncio.run(_read_dispatcher())
        self.assertIs(first, second)

    def test_queued_messages_survive_a_new_event_loop(self):
        async def enqueue():
            dispatcher = get_dispatcher()
            dispatcher.queue.put_nowait((Priority.WARNINGS, 0, "chunked-warning-report"))
            return dispatcher

        first = asyncio.run(enqueue())
        second = asyncio.run(_read_dispatcher())

        self.assertIs(first, second)
        self.assertIs(first.queue, second.queue)
        self.assertEqual(second.queue.qsize(), 1, "a rebuilt dispatcher would have dropped the queue")


class TestUnknownUsersQueueSingleton(unittest.TestCase):
    """get_unknown_users_queue() must keep the pending usernames it was given."""

    def setUp(self):
        self._saved_queue = user_sync._UNKNOWN_USERS_QUEUE
        user_sync._UNKNOWN_USERS_QUEUE = None

    def tearDown(self):
        user_sync._UNKNOWN_USERS_QUEUE = self._saved_queue

    def test_repeated_calls_return_the_same_queue(self):
        self.assertIs(user_sync.get_unknown_users_queue(), user_sync.get_unknown_users_queue())

    def test_pending_usernames_survive_a_new_event_loop(self):
        async def enqueue():
            queue = user_sync.get_unknown_users_queue()
            queue.put_nowait("waiting_user")
            return queue

        first = asyncio.run(enqueue())
        second = asyncio.run(_read_queue())

        self.assertIs(first, second)
        self.assertEqual(second.qsize(), 1, "a rebuilt queue would have dropped the pending lookup")
        self.assertEqual(second.get_nowait(), "waiting_user")


if __name__ == "__main__":
    unittest.main()
