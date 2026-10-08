from contextlib import nullcontext
from pathlib import Path
import struct
import threading
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import fm_odds_web
from tools.club_economy import PRODUCTS
from tools import club_reader
from tools.club_reader import plan_age_reversal_ca
from tools.player_effects import PlayerEffectTarget


@pytest.mark.parametrize(("current", "expected"), ((200, 185), (100, 85), (16, 1)))
def test_age_reversal_ca_reduces_exactly_fifteen(current: int, expected: int) -> None:
    assert plan_age_reversal_ca(current) == expected


def test_age_reversal_rejects_ca_below_sixteen() -> None:
    with pytest.raises(ValueError, match="CA 不足16"):
        plan_age_reversal_ca(15)


def test_age_reversal_writes_only_birth_date_and_ca() -> None:
    player = 0x4200
    person = 0x8200
    birth_address = person + 0x40
    attribute_address = player + 0x100
    ca_address = player + 0x200
    pa_address = player + 0x202
    original_birth = struct.pack("<HH", 1, 2000)
    memory = {
        birth_address: bytearray(original_birth),
        attribute_address: bytearray([100] * 54),
        ca_address: bytearray(struct.pack("<H", 100)),
        pa_address: bytearray(struct.pack("<H", 120)),
    }
    layout = SimpleNamespace(
        module_name="game_plugin.dll", person_date_of_birth_offset=0x40,
        person_date_of_birth_day_year=True, player_attributes_offset=0x100,
        player_ca_offset=0x200, player_pa_offset=0x202,
        attribute_display_bias=0,
        module=lambda _process: SimpleNamespace(base_address=0x100000),
    )

    class FakeReader:
        def __init__(self) -> None:
            self.layout = layout

        @staticmethod
        def _slice(address: int, size: int) -> bytes | None:
            for base, raw in memory.items():
                offset = address - base
                if 0 <= offset and offset + size <= len(raw):
                    return bytes(raw[offset:offset + size])
            return None

        def bytes(self, address: int, size: int) -> bytes | None:
            return self._slice(address, size)

        def u8(self, address: int) -> int | None:
            raw = self._slice(address, 1)
            return raw[0] if raw else None

        def u16(self, address: int) -> int | None:
            raw = self._slice(address, 2)
            return struct.unpack("<H", raw)[0] if raw else None

        @staticmethod
        def invalidate_prefetch() -> None:
            return None

    writes: list[tuple[int, bytes]] = []

    def write(_process, address: int, raw: bytes) -> None:
        writes.append((address, bytes(raw)))
        for base, stored in memory.items():
            offset = address - base
            if 0 <= offset and offset + len(raw) <= len(stored):
                stored[offset:offset + len(raw)] = raw
                return
        raise AssertionError(f"unexpected write address: {address:#x}")

    with (
        patch.object(club_reader, "select_process_layout", return_value=(1, "fm.exe", layout)),
        patch.object(club_reader, "open_process", return_value=nullcontext(object())),
        patch.object(club_reader, "Reader", return_value=FakeReader()),
        patch.object(club_reader, "_validated_player_person", return_value=person),
        patch.object(club_reader, "write_process_memory", side_effect=write),
    ):
        result = club_reader.set_player_age(42, player, "2024-06-01", 18)
        club_reader.restore_player_birth_date(
            42, player, result["_original_birth_date"],
            original_ca=result["_original_ca"],
            original_pa=result["_original_pa"],
        )

    assert all(len(raw) in {1, 2, 4} for _address, raw in writes)
    assert not any(address == attribute_address and len(raw) == 54 for address, raw in writes)
    assert struct.unpack("<H", memory[pa_address])[0] == 120
    assert bytes(memory[birth_address]) == original_birth
    assert struct.unpack("<H", memory[ca_address])[0] == 100
    assert bytes(memory[attribute_address]) == bytes([100] * 54)
    assert result["ca_after"] == 85
    assert result["attribute_changes"] == {}
    assert {address for address, _raw in writes} == {birth_address, ca_address}


def test_age_reversal_product_description_matches_fixed_ca_effect() -> None:
    description = PRODUCTS["age_reversal_pill"]["description"]
    assert "CA降低15" in description
    assert "其他属性及PA不变" in description
    assert "清除已有退役计划" in description
    assert "随机降低" not in description


def _age_reversal_state(player: dict) -> fm_odds_web.LocalOddsState:
    state = fm_odds_web.LocalOddsState.__new__(fm_odds_web.LocalOddsState)
    state.memory_lock = threading.RLock()
    state.lock = threading.RLock()
    state.output = {"game_date": "2028-08-31"}
    state._bind_current_save = lambda: None
    state._live_player_effect_target = lambda *_args, **_kwargs: PlayerEffectTarget(
        player_id=int(player["id"]), team_id=7, address=player["address"],
        profile={"team": {"id": 7}}, player=player, team={"id": 7},
    )
    return state


def test_age_reversal_clears_detected_retirement_plan() -> None:
    player = {
        "id": 42, "name": "Test Player", "address": "0x4200",
        "retirement": {"has_plan": True, "retirement_date": "2029-06-30", "cancelled": False},
    }
    state = _age_reversal_state(player)
    native = {
        "before": "1990-01-01", "after": "2010-01-01", "age": 18,
        "ca_before": 100, "ca_after": 85, "pa": 120,
        "attribute_changes": {}, "_original_birth_date": b"old!",
        "_original_ca": 100, "_original_pa": 120,
    }
    updated_retirement = {
        "has_plan": False, "cancelled": True, "state": 0x80,
        "_rollback": [(0x9000, b"old!", b"new!")],
    }
    with (
        patch.object(fm_odds_web, "public_economy", return_value={
            "inventory": [{"id": "item-1", "sku": "age_reversal_pill", "status": "available"}],
        }),
        patch.object(fm_odds_web, "read_game_clock", return_value={"date": "2028-08-31"}),
        patch.object(fm_odds_web, "set_player_age", return_value=dict(native)),
        patch.object(fm_odds_web, "update_retirement_plan", return_value=dict(updated_retirement)) as cancel,
        patch.object(fm_odds_web, "consume_instant_item", return_value={"inventory": []}) as consume,
    ):
        result = state.inventory_reverse_player_age({
            "item_id": "item-1", "player_id": 42, "team_id": 7, "age": 18,
        })

    cancel.assert_called_once_with(
        42, "0x4200", action="cancel", capture_rollback=True, missing_ok=True,
    )
    assert consume.call_args.kwargs["retirement_cleared"] is True
    assert result["player"]["retirement_cleared"] is True
    assert result["player"]["retirement"]["has_plan"] is False
    assert "_retirement_rollback" not in result["player"]
    assert player["retirement"]["has_plan"] is False


def test_age_reversal_restores_retirement_and_age_when_item_commit_fails() -> None:
    player = {
        "id": 42, "name": "Test Player", "address": "0x4200",
        "retirement": {"has_plan": True, "retirement_date": "2029-06-30", "cancelled": False},
    }
    state = _age_reversal_state(player)
    native = {
        "before": "1990-01-01", "after": "2010-01-01", "age": 18,
        "ca_before": 100, "ca_after": 85, "pa": 120,
        "attribute_changes": {}, "_original_birth_date": b"old!",
        "_original_ca": 100, "_original_pa": 120,
    }
    snapshot = [(0x9000, b"old!", b"new!")]
    rollback_order: list[str] = []
    with (
        patch.object(fm_odds_web, "public_economy", return_value={
            "inventory": [{"id": "item-1", "sku": "age_reversal_pill", "status": "available"}],
        }),
        patch.object(fm_odds_web, "read_game_clock", return_value={"date": "2028-08-31"}),
        patch.object(fm_odds_web, "set_player_age", return_value=dict(native)),
        patch.object(fm_odds_web, "update_retirement_plan", return_value={
            "has_plan": False, "cancelled": True, "state": 0x80, "_rollback": snapshot,
        }),
        patch.object(fm_odds_web, "consume_instant_item", side_effect=ValueError("commit failed")),
        patch.object(
            fm_odds_web, "restore_retirement_plan",
            side_effect=lambda *_args: rollback_order.append("retirement"),
        ) as restore_retirement,
        patch.object(
            fm_odds_web, "restore_player_birth_date",
            side_effect=lambda *_args, **_kwargs: rollback_order.append("age"),
        ) as restore_age,
    ):
        with pytest.raises(ValueError, match="commit failed"):
            state.inventory_reverse_player_age({
                "item_id": "item-1", "player_id": 42, "team_id": 7, "age": 18,
            })

    restore_retirement.assert_called_once_with(42, "0x4200", snapshot)
    restore_age.assert_called_once()
    assert rollback_order == ["retirement", "age"]


def test_age_reversal_frontend_copy_matches_fixed_ca_effect() -> None:
    root = (Path(__file__).resolve().parents[1] / "src")
    frontend = "\n".join(
        (root / path).read_text(encoding="utf-8")
        for path in ("web/index.html", "web/app.js")
    )
    catalog = (root / "web" / "i18n.static.js").read_text(encoding="utf-8")
    assert 'data-i18n="dialog.age_reversal.subtitle"' in frontend
    assert '"dialog.age_reversal.subtitle": "CA is reduced by 15; other attributes and PA remain unchanged; any retirement plan is cleared"' in catalog
    assert "CA固定降低15，其他属性及PA不变；已有退役计划会一并清除" in catalog
    assert "三类属性合计随机降低8点" not in frontend


def test_age_reversal_frontend_updates_retirement_without_obsolete_warning() -> None:
    script = (
        (Path(__file__).resolve().parents[1] / "src") / "web" / "app.js"
    ).read_text(encoding="utf-8")

    assert "function retirementPlanWithinYears(" not in script
    assert "function ageReversalRetirementWarning(" not in script
    assert "const retirementWarning = ageReversalRetirementWarning(player);" not in script
    assert "retirement:response.player.retirement" in script
