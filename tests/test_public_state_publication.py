"""Publication tests bypass startup, disk accounts and live game access."""
from unittest.mock import Mock
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from fm_odds_web import LocalOddsState
from tools.state_publication import CoalescedStatePublisher, PublicationCandidate


def state_for_publication():
    state = LocalOddsState.__new__(LocalOddsState)
    state.data_version = 8
    state.output = {"save_instance_id": "career-a", "account_scope_id": "account-a"}
    state.connection_scope_id = "account-a"
    state.connection_process_pid = 101
    state._build_public_state = Mock(return_value={
        "data_version": 8, "data_scope_id": "account-a",
    })
    state._capture_public_state = lambda: PublicationCandidate(
        state._public_state_context_key(), state._build_public_state(),
    )
    return state


def test_scope_change_without_version_increment_does_not_reuse_snapshot():
    state = state_for_publication()
    first = state.public_state()
    state.connection_scope_id = "account-b"
    state._build_public_state.return_value = {
        "data_version": 8, "data_scope_id": "account-b",
    }

    second = state.public_state()

    assert first["data_scope_id"] == "account-a"
    assert second["data_scope_id"] == "account-b"
    assert state._build_public_state.call_count == 2


def test_version_change_during_projection_is_not_cached_as_new_version():
    state = state_for_publication()

    def build():
        version = state.data_version
        state.data_version = 9
        return {"data_version": version}

    state._build_public_state.side_effect = build
    first = state.public_state()
    second = state.public_state()

    assert first["data_version"] == 8
    assert second["data_version"] == 9
    assert state._build_public_state.call_count == 2


@pytest.mark.parametrize("field,value", [
    ("manual_account_scope_id", "manual-b"),
    ("account_scope_id", "account-b"),
    ("selected_manager_id", 42),
    ("game_layout", "fm26"),
])
def test_output_identity_changes_invalidate_snapshot(field, value):
    state = state_for_publication()
    state.public_state()
    state.output[field] = value
    state.public_state()
    assert state._build_public_state.call_count == 2


def test_same_manager_changes_team_without_version_increment():
    state = state_for_publication()
    state.output["managed_teams"] = [{"id": 1, "team_type": "club"}]
    state.public_state()
    state.output["managed_teams"][0]["id"] = 2
    state.public_state()
    assert state._build_public_state.call_count == 2


@pytest.mark.parametrize("field", ["connection_requested", "connection_suspended"])
def test_connection_transition_invalidates_snapshot(field):
    state = state_for_publication()
    setattr(state, field, False)
    state.public_state()
    setattr(state, field, True)
    state.public_state()
    assert state._build_public_state.call_count == 2


def test_expired_snapshot_is_rebuilt_with_same_version():
    now = [0.0]
    publisher = CoalescedStatePublisher(clock=lambda: now[0])
    build = Mock(side_effect=[
        PublicationCandidate("a", {"revision": 1}),
        PublicationCandidate("a", {"revision": 2}),
    ])
    assert publisher.read(lambda: "a", build)["revision"] == 1
    now[0] = 0.75
    assert publisher.read(lambda: "a", build)["revision"] == 2


@pytest.mark.parametrize("change_scope", [False, True])
def test_concurrent_readers_recheck_scope_after_waiting(change_scope):
    publisher = CoalescedStatePublisher()
    identity = ["a"]
    started, release, second_observed = Event(), Event(), Event()
    calls = []

    def build():
        captured = identity[0]
        calls.append(captured)
        if len(calls) == 1:
            started.set()
            assert release.wait(5)
        return PublicationCandidate(captured, {"scope": captured})

    def waiting_key():
        second_observed.set()
        return identity[0]

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(publisher.read, lambda: identity[0], build)
        try:
            assert started.wait(5)
            if change_scope:
                identity[0] = "b"
            second = pool.submit(publisher.read, waiting_key, build)
            assert second_observed.wait(5)
        finally:
            release.set()
        first_result, second_result = first.result(5), second.result(5)
    assert first_result == {"scope": "a"}
    assert second_result == {"scope": "b" if change_scope else "a"}
    assert calls == (["a", "b"] if change_scope else ["a"])


def test_failed_builder_does_not_poison_next_reader():
    publisher = CoalescedStatePublisher()
    build = Mock(side_effect=[
        ValueError("failed projection"), PublicationCandidate("a", {"scope": "a"}),
    ])
    with pytest.raises(ValueError, match="failed projection"):
        publisher.read(lambda: "a", build)
    assert publisher.read(lambda: "a", build) == {"scope": "a"}


def test_account_round_trip_during_build_does_not_publish_intermediate_account():
    state = state_for_publication()
    calls = []

    def capture():
        if not calls:
            state.connection_scope_id = "account-b"
        candidate = PublicationCandidate(
            state._public_state_context_key(),
            {"data_scope_id": state.connection_scope_id},
        )
        calls.append(candidate)
        state.connection_scope_id = "account-a"
        return candidate

    state._capture_public_state = capture
    state._build_public_state = lambda: capture().payload
    assert state.public_state()["data_scope_id"] == "account-b"
    assert state.public_state()["data_scope_id"] == "account-a"
    assert len(calls) == 2
