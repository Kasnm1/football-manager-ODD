from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from tools import club_economy


def _economy_state() -> dict:
    state = club_economy._new_state()
    state["owned_activity_centres"] = ["entertainment"]
    state["owned_activity_facilities"] = ["drinks", "massage"]
    return state


def test_staff_activity_writes_before_persist_and_records_cooldown(monkeypatch):
    state = _economy_state()
    calls = []
    monkeypatch.setattr(club_economy, "load_economy", lambda: deepcopy(state))

    def save(payload, adjustments):
        calls.append("save")
        state.clear()
        state.update(deepcopy(payload))

    monkeypatch.setattr(club_economy, "_save_economy_with_wallet_adjustments", save)

    def apply(points):
        calls.append("write")
        assert points == 1
        return {
            "a_to_b": {
                "before": 4, "after": 5, "applied": 1,
                "reason": 5, "undo": {"token": 1},
            },
            "b_to_a": {
                "before": 7, "after": 8, "applied": 1,
                "reason": 5, "undo": {"token": 2},
            },
        }

    result = club_economy.perform_staff_activity(
        1, 2, "Staff", "massage", "2026-08-20", 4,
        apply, lambda _result: calls.append("rollback"),
        reverse_intimacy=7,
    )
    assert calls == ["write", "save"]
    assert result["intimacy_before"] == 4
    assert result["intimacy_after"] == 5
    assert result["reverse_intimacy_before"] == 7
    assert result["reverse_intimacy_after"] == 8
    assert result["relationship_directions"]["a_to_b"]["reason"] == 5
    assert result["relationship_directions"]["b_to_a"]["reason"] == 5
    assert "staff:1:2:entertainment" in state["activity_floor_cooldowns"]
    assert state["activity_history"][-1]["target_kind"] == "staff"


def test_player_and_staff_with_same_id_have_independent_cooldowns(monkeypatch):
    state = _economy_state()
    state["activity_floor_cooldowns"]["1:2:entertainment"] = {
        "last_game_date": "2026-08-20", "cooldown_until": "2026-08-27",
    }
    monkeypatch.setattr(club_economy, "load_economy", lambda: deepcopy(state))
    monkeypatch.setattr(
        club_economy, "_save_economy_with_wallet_adjustments", lambda *_args: None,
    )
    result = club_economy.perform_staff_activity(
        1, 2, "Staff", "drinks", "2026-08-20", 4,
        lambda _points: {}, lambda _result: None,
    )
    assert result["target_kind"] == "staff"


def test_staff_activity_rolls_back_native_write_when_account_save_fails(monkeypatch):
    state = _economy_state()
    token = {
        "a_to_b": {"before": 9, "after": 10, "applied": 1, "undo": {"token": 2}},
        "b_to_a": {"before": 12, "after": 13, "applied": 1, "undo": {"token": 3}},
    }
    rollbacks = []
    monkeypatch.setattr(club_economy, "load_economy", lambda: deepcopy(state))
    monkeypatch.setattr(
        club_economy, "_save_economy_with_wallet_adjustments",
        lambda *_args: (_ for _ in ()).throw(OSError("save failed")),
    )
    with pytest.raises(OSError, match="save failed"):
        club_economy.perform_staff_activity(
            1, 2, "Staff", "massage", "2026-08-20", 9,
            lambda points: token, lambda result: rollbacks.append(result),
            reverse_intimacy=12,
        )
    assert rollbacks == [token]
    assert state["activity_floor_cooldowns"] == {}


def test_staff_activity_rejects_non_entertainment_activity(monkeypatch):
    with pytest.raises(ValueError, match="仅限娱乐中心"):
        club_economy.perform_staff_activity(
            1, 2, "Staff", "media_interview", "2026-08-20", 0,
            lambda _points: {}, lambda _result: None,
        )


def test_staff_activity_requires_both_directional_write_results(monkeypatch):
    state = _economy_state()
    monkeypatch.setattr(club_economy, "load_economy", lambda: deepcopy(state))
    with pytest.raises(RuntimeError, match="完整的双向关系"):
        club_economy.perform_staff_activity(
            1, 2, "Staff", "massage", "2026-08-20", 4,
            lambda _points: {"before": 4, "after": 5, "applied": 1},
            lambda _result: None, reverse_intimacy=7,
        )


def test_staff_activity_api_uses_atomic_bidirectional_relationship_writer():
    source = (Path(__file__).resolve().parents[1] / "fm_odds_web.py").read_text(
        encoding="utf-8",
    )
    handler = source.split("def staff_activity(", 1)[1].split(
        "def purchase_activity_centre", 1,
    )[0]
    assert "write_bidirectional_intimacy(" in handler
    assert "reason_a2b=5, reason_b2a=5" in handler
    assert "rollback_bidirectional_intimacy" in handler
