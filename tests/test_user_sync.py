"""Tests for deduplicated background synchronization of unknown users."""

import utils.user_sync as user_sync


async def test_duplicate_unknown_user_requests_enqueue_once():
    saved_cache = dict(user_sync.USER_METADATA_CACHE)
    saved_fetching = set(user_sync._UNKNOWN_USERS_FETCHING)
    saved_queue = user_sync._UNKNOWN_USERS_QUEUE
    try:
        user_sync.USER_METADATA_CACHE.clear()
        user_sync._UNKNOWN_USERS_FETCHING.clear()
        user_sync._UNKNOWN_USERS_QUEUE = None

        await user_sync.queue_unknown_user_fetch("alice")
        await user_sync.queue_unknown_user_fetch("alice")

        queue = user_sync.get_unknown_users_queue()
        assert queue.qsize() == 1
        assert await queue.get() == "alice"
        queue.task_done()
        assert user_sync._UNKNOWN_USERS_FETCHING == {"alice"}
    finally:
        user_sync.USER_METADATA_CACHE.clear()
        user_sync.USER_METADATA_CACHE.update(saved_cache)
        user_sync._UNKNOWN_USERS_FETCHING.clear()
        user_sync._UNKNOWN_USERS_FETCHING.update(saved_fetching)
        user_sync._UNKNOWN_USERS_QUEUE = saved_queue
