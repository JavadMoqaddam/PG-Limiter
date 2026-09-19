"""A consumed active-users batch must survive a mid-cycle failure.

pop_active_users_snapshot() clears ACTIVE_USERS, so any dependency failure after it
(e.g. metadata unavailable) would lose the batch and let the next cycle clear the
warnings of users who are still connected but did not re-emit. The batch is requeued
on failure, merged by connection identity, never overwriting a newer observation.
"""

from utils.shared_state import (
    ACTIVE_USERS,
    merge_active_users_snapshot,
    pop_active_users_snapshot,
)
from utils.types import UserType


async def test_metadata_failure_requeues_consumed_batch_without_overwriting_new_events():
    ACTIVE_USERS.clear()
    ACTIVE_USERS["alice"] = UserType(name="alice", ip=["1.1.1.1"])
    ACTIVE_USERS["bob"] = UserType(name="bob", ip=["2.2.2.2"])

    # The cycle consumes the batch.
    batch = await pop_active_users_snapshot()
    assert set(batch) == {"alice", "bob"}
    assert ACTIVE_USERS == {}

    # A newer event for bob arrives while the cycle is running.
    ACTIVE_USERS["bob"] = UserType(name="bob", ip=["9.9.9.9"])

    # Metadata was unavailable, so the consumed batch is requeued.
    restored = await merge_active_users_snapshot(batch)

    assert set(ACTIVE_USERS) == {"alice", "bob"}
    # Absent user is restored from the batch...
    assert ACTIVE_USERS["alice"].ip == ["1.1.1.1"]
    assert restored == 1
    # ...but the newer bob observation is kept, not overwritten by the stale batch.
    assert ACTIVE_USERS["bob"].ip == ["9.9.9.9"]


async def test_requeue_is_noop_when_every_user_has_a_newer_event():
    ACTIVE_USERS.clear()
    ACTIVE_USERS["carol"] = UserType(name="carol", ip=["1.1.1.1"])

    batch = await pop_active_users_snapshot()

    # Both users reconnect during the cycle before the requeue runs.
    ACTIVE_USERS["carol"] = UserType(name="carol", ip=["5.5.5.5"])

    restored = await merge_active_users_snapshot(batch)

    assert restored == 0
    assert ACTIVE_USERS["carol"].ip == ["5.5.5.5"]
