from __future__ import annotations

import struct
import threading
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import patch

from fm_odds_web import LocalOddsState
from tools import club_reader


def _state() -> tuple[LocalOddsState, dict, dict]:
    state = LocalOddsState.__new__(LocalOddsState)
    state.lock = threading.RLock()
    state.memory_lock = threading.RLock()
    state.output = {
        "manager": {"id": 7, "manager_address": "0x7000"},
        "selected_manager_id": 7,
        "game_date": "2026-07-24",
    }
    state._bind_current_save = lambda: None
    player = {
        "id": 42, "name": "Test Player", "address": "0x4200",
        "manager_intimacy": 12, "fitness": 74.0, "sharpness": 68.0,
    }
    profile = {
        "team": {"id": 9, "team_type": "club", "address": "0x9000"},
        "players": [player],
    }
    state._live_player_profile = lambda _player_id, _team_id=0: (profile, player)
    return state, profile, player


def test_sea_cucumber_charges_and_applies_both_effects() -> None:
    state, _profile, player = _state()
    payment = {"bank": 40_000.0, "wallet": 20_000.0, "total": 60_000.0}
    condition = {
        "field": "canteen_sea_cucumber",
        "fitness": {"field": "fitness", "before": 74.0, "after": 100.0},
        "sharpness": {"field": "sharpness", "before": 68.0, "after": 100.0},
        "_restores": [(0x1234, b"old-fitness"), (0x5678, b"old-sharpness")],
    }
    with (
        patch("fm_odds_web.read_game_clock", return_value={"date": "2026-07-24"}),
        patch("fm_odds_web.charge_combined_funds", return_value=payment) as charge,
        patch("fm_odds_web.apply_canteen_sea_cucumber_effect", return_value=condition),
        patch(
            "fm_odds_web.apply_manager_intimacy_points",
            return_value={"before": 12, "after": 13, "applied": 1},
        ),
        patch("fm_odds_web.public_economy", return_value={"bank_balance": 0}),
    ):
        result = state.canteen_sea_cucumber({"player_id": 42})

    assert result["price_gbp"] == 60_000.0
    assert result["fitness"]["after"] == 100.0
    assert result["sharpness"]["after"] == 100.0
    assert result["intimacy"]["applied"] == 1
    assert player["fitness"] == 100.0
    assert player["sharpness"] == 100.0
    assert player["manager_intimacy"] == 13
    charge.assert_called_once()
    assert charge.call_args.args[:2] == (60_000.0, "canteen_sea_cucumber")


def test_sea_cucumber_restores_fitness_and_refunds_on_intimacy_failure() -> None:
    state, _profile, _player = _state()
    payment = {"bank": 60_000.0, "wallet": 0.0, "total": 60_000.0}
    condition = {
        "field": "canteen_sea_cucumber",
        "fitness": {"field": "fitness", "before": 74.0, "after": 100.0},
        "sharpness": {"field": "sharpness", "before": 68.0, "after": 100.0},
        "_restores": [(0x1234, b"old-fitness"), (0x5678, b"old-sharpness")],
    }
    with (
        patch("fm_odds_web.read_game_clock", return_value={"date": "2026-07-24"}),
        patch("fm_odds_web.charge_combined_funds", return_value=payment),
        patch("fm_odds_web.apply_canteen_sea_cucumber_effect", return_value=condition),
        patch("fm_odds_web.apply_manager_intimacy_points", side_effect=RuntimeError("write failed")),
        patch("fm_odds_web.restore_player_activity_effect") as restore,
        patch("fm_odds_web.refund_combined_funds") as refund,
    ):
        try:
            state.canteen_sea_cucumber({"player_id": 42})
        except RuntimeError as error:
            assert str(error) == "write failed"
        else:
            raise AssertionError("expected intimacy write failure")

    restore.assert_called_once_with(condition)
    refund.assert_called_once()
    assert refund.call_args.args[:2] == (payment, "canteen_sea_cucumber_rollback")


def test_bulk_sea_cucumber_reuses_one_memory_session() -> None:
    state, profile, first_player = _state()
    second_player = {
        "id": 43, "name": "Second Player", "address": "0x4300",
        "manager_intimacy": 5, "fitness": 80.0, "sharpness": 70.0,
    }
    profile["players"].append(second_player)
    players = {42: first_player, 43: second_player}
    state._live_player_profile = lambda player_id, _team_id=0: (
        profile, players.get(player_id),
    )
    calls: list[tuple[str, int]] = []

    class MemorySession:
        def apply_effect(self, player_id, _player_address):
            calls.append(("effect", player_id))
            return {
                "field": "canteen_sea_cucumber",
                "fitness": {"before": 70.0, "after": 100.0},
                "sharpness": {"before": 70.0, "after": 100.0},
                "_restores": [(player_id, b"old")],
            }

        def apply_intimacy(self, player_id, *_args, **_kwargs):
            calls.append(("intimacy", player_id))
            return {"before": 1, "after": 2, "applied": 1}

        def restore(self, _condition):
            raise AssertionError("successful batch must not roll back")

    session = MemorySession()
    with (
        patch("fm_odds_web.read_game_clock", return_value={"date": "2026-07-24"}),
        patch("fm_odds_web.canteen_sea_cucumber_memory_session", return_value=nullcontext(session)) as open_session,
        patch("fm_odds_web.charge_combined_funds", return_value={"total": 60_000.0}) as charge,
        patch("fm_odds_web.public_economy", return_value={"bank_balance": 10}),
    ):
        result = state.canteen_sea_cucumbers({"player_ids": [42, 43], "team_id": 9})

    assert result["requested"] == 2
    assert result["completed"] == 2
    assert result["failure"] is None
    assert result["economy"] == {"bank_balance": 10}
    assert calls == [
        ("effect", 42), ("intimacy", 42),
        ("effect", 43), ("intimacy", 43),
    ]
    open_session.assert_called_once_with()
    assert charge.call_count == 2


def test_bulk_sea_cucumber_stops_after_failure_and_reports_partial_completion() -> None:
    state, profile, first_player = _state()
    second_player = {
        "id": 43, "name": "Second Player", "address": "0x4300",
        "manager_intimacy": 5, "fitness": 80.0, "sharpness": 70.0,
    }
    profile["players"].append(second_player)
    players = {42: first_player, 43: second_player}
    state._live_player_profile = lambda player_id, _team_id=0: (
        profile, players.get(player_id),
    )
    restored: list[int] = []

    class MemorySession:
        def apply_effect(self, player_id, _player_address):
            return {
                "field": "canteen_sea_cucumber",
                "fitness": {"before": 70.0, "after": 100.0},
                "sharpness": {"before": 70.0, "after": 100.0},
                "_restores": [(player_id, b"old")],
            }

        def apply_intimacy(self, player_id, *_args, **_kwargs):
            if player_id == 43:
                raise RuntimeError("write failed")
            return {"before": 1, "after": 2, "applied": 1}

        def restore(self, condition):
            restored.append(int(condition["_restores"][0][0]))

    with (
        patch("fm_odds_web.read_game_clock", return_value={"date": "2026-07-24"}),
        patch("fm_odds_web.canteen_sea_cucumber_memory_session", return_value=nullcontext(MemorySession())),
        patch("fm_odds_web.charge_combined_funds", return_value={"total": 60_000.0}),
        patch("fm_odds_web.refund_combined_funds") as refund,
        patch("fm_odds_web.public_economy", return_value={"bank_balance": 10}),
    ):
        result = state.canteen_sea_cucumbers({"player_ids": [42, 43], "team_id": 9})

    assert result["completed"] == 1
    assert result["failure"]["player_id"] == 43
    assert result["failure"]["error"] == "write failed"
    assert restored == [43]
    refund.assert_called_once()


def _condition_memory_patches(memory: dict[int, int], *, fail_sharpness: bool = False):
    player = 0x4200
    layout = SimpleNamespace(
        player_fitness_offset=0x10,
        player_sharpness_offset=0x12,
        module=lambda _process: SimpleNamespace(base_address=0x1000),
    )

    class Reader:
        def __init__(self, *_args):
            pass

        @staticmethod
        def u16(address):
            return memory.get(address)

    def write(_process, address, data):
        value = struct.unpack("<H", data)[0]
        if fail_sharpness and address == player + 0x12 and value == 10000:
            return
        memory[address] = value

    return (
        patch.object(club_reader, "select_process_layout", return_value=(1, "fm.exe", layout)),
        patch.object(club_reader, "open_process", return_value=nullcontext(object())),
        patch.object(club_reader, "Reader", Reader),
        patch.object(club_reader, "_validated_player_person", return_value=0x5000),
        patch.object(club_reader, "write_process_memory", side_effect=write),
    )


def test_sea_cucumber_memory_effect_maximizes_fitness_and_sharpness() -> None:
    memory = {0x4210: 7400, 0x4212: 6800}
    patches = _condition_memory_patches(memory)
    with patches[0], patches[1], patches[2], patches[3], patches[4]:
        result = club_reader.apply_canteen_sea_cucumber_effect(42, "0x4200")

    assert memory == {0x4210: 10000, 0x4212: 10000}
    assert result["fitness"]["before"] == 74.0
    assert result["fitness"]["after"] == 100.0
    assert result["sharpness"]["before"] == 68.0
    assert result["sharpness"]["after"] == 100.0
    assert len(result["_restores"]) == 2


def test_sea_cucumber_memory_effect_rolls_back_both_fields_on_readback_failure() -> None:
    memory = {0x4210: 7400, 0x4212: 6800}
    patches = _condition_memory_patches(memory, fail_sharpness=True)
    with patches[0], patches[1], patches[2], patches[3], patches[4]:
        try:
            club_reader.apply_canteen_sea_cucumber_effect(42, "0x4200")
        except RuntimeError as error:
            assert str(error) == "球员比赛状态写入校验失败"
        else:
            raise AssertionError("expected sharpness read-back failure")

    assert memory == {0x4210: 7400, 0x4212: 6800}


def test_bulk_memory_session_opens_one_writer_for_multiple_players() -> None:
    memory = {
        0x4210: 7400, 0x4212: 6800,
        0x4310: 8100, 0x4312: 7200,
    }
    layout = SimpleNamespace(
        player_fitness_offset=0x10,
        player_sharpness_offset=0x12,
        module=lambda _process: SimpleNamespace(base_address=0x1000),
    )

    class Reader:
        def __init__(self, *_args):
            pass

        @staticmethod
        def u16(address):
            return memory.get(address)

    def write(_process, address, data):
        memory[address] = struct.unpack("<H", data)[0]

    with (
        patch.object(club_reader, "select_process_layout", return_value=(1, "fm.exe", layout)),
        patch.object(club_reader, "open_process", return_value=nullcontext(object())) as opened,
        patch.object(club_reader, "Reader", Reader),
        patch.object(club_reader, "_validated_player_person", return_value=0x5000),
        patch.object(club_reader, "write_process_memory", side_effect=write),
    ):
        with club_reader.canteen_sea_cucumber_memory_session() as session:
            session.apply_effect(42, "0x4200")
            session.apply_effect(43, "0x4300")

    assert opened.call_count == 1
    assert memory == {
        0x4210: 10000, 0x4212: 10000,
        0x4310: 10000, 0x4312: 10000,
    }
