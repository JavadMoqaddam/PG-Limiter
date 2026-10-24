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


def test_panel_parser_returns_only_panel_owned_fields():
    from datetime import datetime

    parsed = user_sync._parse_panel_user_dict(
        {
            "username": "alice",
            "id": 12,
            "status": "active",
            "admin": None,
            "created_by": "owner",
            "group_ids": "7,8",
            "data_limit": 2 * 1024**3,
            "used_traffic": 1024**3,
            "expire": 100,
            "note": "panel note",
        },
        {"limits": {"general": 5, "special": {"alice": 9}}},
        is_excepted=True,
        special_limit=9,
    )
    assert parsed == {
        "username": "alice",
        "panel_id": 12,
        "status": "active",
        "owner_id": None,
        "owner_username": "owner",
        "group_ids": [7, 8],
        "data_limit": 2,
        "used_traffic": 1,
        "expire_at": datetime.fromtimestamp(100),
        "note": "panel note",
    }


def test_compatibility_display_ignores_stale_local_policy_inputs():
    monitored, limit = user_sync.calculate_user_effective_limit_and_monitoring(
        "alice",
        [7],
        is_excepted=True,
        special_limit=9,
        config={"limits": {"general": 5, "special": {}}, "except_users": [], "group_limits": {7: 3}},
    )
    assert (monitored, limit) == (True, 3)


def test_compatibility_display_preserves_general_fallback_marker():
    monitored, limit = user_sync.calculate_user_effective_limit_and_monitoring(
        "alice",
        [],
        config={"limits": {"general": 5, "special": {}}, "except_users": []},
    )
    assert (monitored, limit) == (True, None)
