"""Replay expiry must reclaim old event times without discarding live identities."""

import hashlib
from collections import OrderedDict
from types import SimpleNamespace

import pytest

import utils.log_observations as observations


@pytest.fixture
def replay_cache(monkeypatch):
    clock = [800.0]
    monkeypatch.setattr(observations.time, "time", lambda: clock[0])
    state = observations.LogObservations()
    config = {"log_timezone": "UTC", "check_interval": 300, "api_ip_freshness": 300, "log_replay_cache_size": 10000}
    state.configure(config)
    clock[0] = 1000.0
    return SimpleNamespace(state=state, clock=clock, config=config)


@pytest.mark.parametrize("capacity", [2, 10000])
def test_expired_out_of_order_records_do_not_evict_live_identity(replay_cache, capacity):
    state, clock = replay_cache.state, replay_cache.clock
    state.capacity = capacity
    assert state.admit(1, "live-first", 950.0)
    for index in range(capacity - 1):
        assert state.admit(1, f"older-later-{index}", 900.0)

    clock[0] = 1201.0
    assert state.admit(1, "new-live", 1201.0)

    assert state.uncertain_until == 0.0
    assert len(state.seen[1]) == 2
    assert not state.admit(1, "live-first", 950.0)
    assert not state.admit(1, "new-live", 1201.0)


def test_duplicate_admission_reclaims_expired_records_without_refreshing_live_time(replay_cache):
    state, clock = replay_cache.state, replay_cache.clock
    assert state.admit(1, "live-first", 950.0)
    assert state.admit(1, "older-later", 900.0)

    clock[0] = 1201.0
    assert not state.admit(1, "live-first", 950.0)

    assert list(state.seen[1].values()) == [950.0]
    assert state.uncertain_until == 0.0


def test_source_time_expiry_is_inclusive_at_the_interval_boundary(replay_cache):
    state, clock = replay_cache.state, replay_cache.clock
    assert state.admit(1, "live-first", 950.0)
    assert state.admit(1, "older-later", 900.0)

    clock[0] = 1199.999999
    assert not state.admit(1, "live-first", 950.0)
    assert len(state.seen[1]) == 2

    clock[0] = 1200.0
    assert not state.admit(1, "live-first", 950.0)
    assert list(state.seen[1].values()) == [950.0]
    assert not state.admit(1, "older-later", 900.0)


def test_real_live_overflow_preserves_fifo_eviction_and_original_expiry_deadline(replay_cache):
    state = replay_cache.state
    state.capacity = 2
    assert state.admit(1, "newer-first", 990.0)
    assert state.admit(1, "older-second", 950.0)
    assert state.admit(1, "newest-third", 995.0)

    assert list(state.seen[1].values()) == [950.0, 995.0]
    assert state.uncertain_until == 1290.0
    assert state.uncertainty_diagnostics()["reason_counts"] == {"live_cache_eviction": 1}


def test_capacity_reduction_expires_old_records_before_evicting_live_records(replay_cache):
    state, clock = replay_cache.state, replay_cache.clock
    assert state.admit(1, "live-first", 950.0)
    for index in range(3):
        assert state.admit(1, f"older-later-{index}", 900.0 + index)

    clock[0] = 1203.0
    state.configure(dict(replay_cache.config, log_replay_cache_size=1))

    assert list(state.seen[1].values()) == [950.0]
    assert state.uncertain_until == 0.0


def test_capacity_reduction_still_marks_true_live_eviction(replay_cache):
    state = replay_cache.state
    for index in range(3):
        assert state.admit(1, f"live-{index}", 990.0 + index)

    state.configure(dict(replay_cache.config, log_replay_cache_size=1))

    assert list(state.seen[1].values()) == [992.0]
    assert state.uncertain_until == 1291.0
    assert state.uncertainty_diagnostics()["reason_counts"] == {"cache_capacity_reduction": 2}


def test_readded_identity_is_not_expired_by_its_evicted_older_index_entry(replay_cache):
    state, clock = replay_cache.state, replay_cache.clock
    state.capacity = 2
    assert state.admit(1, "readded", 900.0)
    assert state.admit(1, "other", 950.0)
    assert state.admit(1, "newer", 960.0)
    assert state.admit(1, "readded", 990.0)

    clock[0] = 1201.0
    assert not state.admit(1, "readded", 990.0)
    assert state.seen[1][hashlib.sha256(b"readded").digest()] == 990.0

    clock[0] = 1290.0
    assert state.admit(1, "fresh", 1290.0)
    assert list(state.seen[1].values()) == [1290.0]


def test_readded_same_timestamp_identity_remains_a_duplicate_until_true_expiry(replay_cache):
    state, clock = replay_cache.state, replay_cache.clock
    state.capacity = 2
    assert state.admit(1, "readded", 950.0)
    assert state.admit(1, "other", 990.0)
    assert state.admit(1, "newer", 995.0)
    assert state.admit(1, "readded", 950.0)

    clock[0] = 1249.0
    assert not state.admit(1, "readded", 950.0)

    clock[0] = 1250.0
    assert state.admit(1, "fresh", 1250.0)
    assert list(state.seen[1].values()) == [995.0, 1250.0]


def test_backward_clock_step_preserves_live_replay_identity(replay_cache):
    state, clock = replay_cache.state, replay_cache.clock
    assert state.admit(1, "live", 950.0)
    assert state.admit(1, "old", 900.0)
    clock[0] = 1201.0
    assert not state.admit(1, "live", 950.0)

    clock[0] = 1100.0
    assert state.admit(1, "fresh-after-step", 1100.0)
    assert not state.admit(1, "live", 950.0)
    assert list(state.seen[1].values()) == [950.0, 1100.0]
    assert state.uncertain_until == 0.0


def test_expiry_index_stays_bounded_during_repeated_live_overflow(replay_cache):
    state = replay_cache.state
    state.capacity = 7
    for index in range(1000):
        assert state.admit(1, f"live-{index}", 950.0 + index / 1000)
        assert len(state.seen[1]) <= 7
        assert len(state._seen_expiry[1]) <= 14

    assert state.uncertainty_diagnostics()["reason_counts"] == {"live_cache_eviction": 993}
    assert not state.admit(1, "live-999", 950.999)


def test_capacity_reduction_bounds_expiry_index_immediately(replay_cache):
    state = replay_cache.state
    for index in range(1000):
        assert state.admit(1, f"live-{index}", 950.0)

    state.configure(dict(replay_cache.config, log_replay_cache_size=1))

    assert len(state.seen[1]) == 1
    assert len(state._seen_expiry[1]) <= 2
    assert state.uncertain_until == 1250.0


def test_node_scope_removal_clears_membership_and_expiry_index(replay_cache):
    state = replay_cache.state
    nodes = [SimpleNamespace(node_id=node_id, status="connected") for node_id in (1, 2)]
    state.register_fleet(nodes)
    assert state.admit(1, "node-one", 950.0)
    assert state.admit(2, "node-two", 950.0)

    state.register_fleet(nodes[1:])

    assert set(state.seen) == {2}
    assert set(state._seen_expiry) == {2}
    assert not state.admit(2, "node-two", 950.0)


def test_invalidation_preserves_replay_membership_and_expiry_index(replay_cache):
    state = replay_cache.state
    assert state.admit(1, "retained", 950.0)
    state.invalidate()

    assert len(state.seen[1]) == len(state._seen_expiry[1]) == 1
    assert not state.admit(1, "retained", 950.0)


def test_healthy_admission_does_not_scan_retained_cache(replay_cache):
    class ScanTrackingCache(OrderedDict):
        visited_entries = 0

        def items(self):
            for item in super().items():
                self.visited_entries += 1
                yield item

        def values(self):
            for value in super().values():
                self.visited_entries += 1
                yield value

        def __iter__(self):
            for key in super().__iter__():
                self.visited_entries += 1
                yield key

    state = replay_cache.state
    for index in range(2000):
        assert state.admit(1, f"retained-{index}", 950.0)
    tracked = ScanTrackingCache(state.seen[1])
    state.seen[1] = tracked
    for index in range(5000):
        assert state.admit(1, f"new-{index}", 990.0)

    assert len(tracked) == 7000
    # O(1) dictionary traversal per admission is allowed; repeatedly sweeping
    # the retained thousands of identities exceeds this linear operation bound.
    assert tracked.visited_entries <= 10000
