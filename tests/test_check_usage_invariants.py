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
from utils.types import UserType, ConnectionInfo
import time


async def test_metadata_failure_requeues_consumed_batch_without_overwriting_new_events(completed_logs_evidence):
    completed_logs_evidence()
    ACTIVE_USERS.clear()
    ACTIVE_USERS["alice"] = UserType(name="alice", ip=["1.1.1.1"])
    ACTIVE_USERS["bob"] = UserType(name="bob", ip=["2.2.2.2"])

    # The cycle consumes the batch.
    for user in ACTIVE_USERS.values():
        user.device_info.connections = [ConnectionInfo(ip, 1, "node", "Vless", time.time() - 1) for ip in user.ip]
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


async def test_requeue_is_noop_when_every_user_has_a_newer_event(completed_logs_evidence):
    completed_logs_evidence()
    ACTIVE_USERS.clear()
    ACTIVE_USERS["carol"] = UserType(name="carol", ip=["1.1.1.1"])

    for user in ACTIVE_USERS.values():
        user.device_info.connections = [ConnectionInfo(ip, 1, "node", "Vless", time.time() - 1) for ip in user.ip]
    batch = await pop_active_users_snapshot()

    # Both users reconnect during the cycle before the requeue runs.
    ACTIVE_USERS["carol"] = UserType(name="carol", ip=["5.5.5.5"])

    restored = await merge_active_users_snapshot(batch)

    assert restored == 0
    assert ACTIVE_USERS["carol"].ip == ["5.5.5.5"]


async def test_api_batch_is_never_merged_back_after_failed_preparation():
    from utils.read_config import get_config_revision
    from utils.shared_state import APISampleEvidence, begin_api_collection, pop_api_users_snapshot, publish_api_snapshot
    from utils.types import ConnectionInfo

    ACTIVE_USERS.clear()
    user = UserType(name="alice", ip=["5.6.7.8"])
    user.device_info.connections = [ConnectionInfo("5.6.7.8", 1, "node", "API", 1_800_000_000)]
    evidence = APISampleEvidence(begin_api_collection(), get_config_revision(), 90)
    assert await publish_api_snapshot({"alice": user}, evidence)
    batch = await pop_api_users_snapshot()
    assert batch["alice"].device_info.connections[0].last_seen == 1_800_000_000
    assert batch["alice"] is not user
    assert await merge_active_users_snapshot(batch) == 0
    assert ACTIVE_USERS == {}
