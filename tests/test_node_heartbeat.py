"""
Tests for the per-node heartbeat that separates a dead SSE stream from a quiet node.

The enforcement cycle clears a user's consecutive-violation counter when they are
absent from the sample. Before this map existed, "absent" could equally mean the
user disconnected or the node they were on had stopped delivering: get_logs opens
its stream with ``timeout=None``, so a half-open connection never raises and the
node keeps reporting "Connected" while producing nothing. One dead node out of
forty-nine still leaves a healthy-looking sample, so an empty-sample check cannot
see it - and every offender on that node gets a fresh start each cycle.

What matters most here is the distinction the last two tests pin down: a stale
node must stay *tracked*, because a node that vanishes from the map looks exactly
like a healthy fleet.
"""

import time

import pytest

from utils.shared_state import (
    NODE_LAST_EVENT,
    clear_node_events,
    forget_node_event,
    get_node_event_ages,
    nodes_seen_within,
    note_node_event,
    tracked_node_count,
)


@pytest.fixture(autouse=True)
def clean_heartbeats():
    """The map is process state, so each test gets it empty and restores it."""
    saved = dict(NODE_LAST_EVENT)
    NODE_LAST_EVENT.clear()
    yield
    NODE_LAST_EVENT.clear()
    NODE_LAST_EVENT.update(saved)


class TestRecording:
    def test_an_event_is_recorded_and_tracked(self):
        note_node_event(7)
        assert tracked_node_count() == 1
        assert nodes_seen_within(60) == 1

    def test_an_explicit_timestamp_is_used_as_given(self):
        note_node_event(7, time.time() - 500)
        assert nodes_seen_within(60) == 0
        assert nodes_seen_within(600) == 1

    def test_a_missing_node_id_is_ignored(self):
        # API mode has no SSE nodes at all; a None must not create a phantom entry
        # that would then read as a permanently silent node.
        note_node_event(None)
        assert tracked_node_count() == 0

    def test_a_later_event_refreshes_the_same_node(self):
        note_node_event(7, time.time() - 500)
        note_node_event(7)
        assert tracked_node_count() == 1
        assert nodes_seen_within(60) == 1


class TestStaleness:
    def test_only_fresh_nodes_are_counted_as_live(self):
        now = time.time()
        note_node_event(1, now)
        note_node_event(2, now - 30)
        note_node_event(3, now - 900)

        assert tracked_node_count() == 3
        assert nodes_seen_within(60) == 2

    def test_a_silent_node_stays_tracked(self):
        # The load-bearing behaviour: a half-open stream must leave its stale
        # timestamp behind. Dropping the entry would make the fleet look complete
        # and put us straight back to clearing counters on a partial sample.
        note_node_event(1, time.time() - 3600)
        assert tracked_node_count() == 1
        assert nodes_seen_within(300) == 0

    def test_ages_are_reported_per_node(self):
        note_node_event(1)
        note_node_event(2, time.time() - 120)
        ages = get_node_event_ages()

        assert set(ages) == {1, 2}
        assert ages[1] < 5
        assert 115 < ages[2] < 130


class TestForgetting:
    def test_a_removed_node_is_no_longer_tracked(self):
        # The panel reported this node as disconnected, so it is deliberately down
        # and must not drag the live/tracked ratio down with it.
        note_node_event(1, time.time() - 3600)
        note_node_event(2)
        forget_node_event(1)

        assert tracked_node_count() == 1
        assert nodes_seen_within(60) == 1

    def test_forgetting_an_unknown_node_is_harmless(self):
        forget_node_event(99)
        forget_node_event(None)
        assert tracked_node_count() == 0

    def test_clearing_empties_the_map(self):
        note_node_event(1)
        note_node_event(2)
        clear_node_events()
        assert tracked_node_count() == 0
        assert get_node_event_ages() == {}
