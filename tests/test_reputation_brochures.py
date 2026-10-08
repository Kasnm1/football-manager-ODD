from __future__ import annotations

import struct
import threading
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from fm_odds_web import LocalOddsState
from tools.club_economy import PRODUCTS, SHOP_SKUS
from tools.club_reader import apply_player_world_reputation_delta
from tools.game_layout import (
    FM24_240_LAYOUT, FM24_241_LAYOUT, FM24_EPIC_LAYOUT, FM24_LAYOUT,
    FM24_XGP_LAYOUT, FM26_LAYOUT, FM26_XGP_TEMPLATE,
)
from tools.preview_cup_odds import apply_competition_reputation_delta


def _state() -> LocalOddsState:
    state = LocalOddsState.__new__(LocalOddsState)
    state.lock = threading.RLock()
    state.memory_lock = threading.RLock()
    state.output = {
        "save_instance_id": "save-1",
        "managed_teams": [
            {"id": 10, "name": "Managed Club", "address": "0x1000", "team_type": "club"},
            {"id": 11, "name": "National Team", "address": "0x1100", "team_type": "national"},
        ],
        "matches": [{
            "competition_id": 100,
            "competition_reputation_native": 150,
        }],
        "competitions": [{"id": 100, "name": "League", "reputation": 150}],
        "competition_formats": [
            {
                "competition_id": 100, "competition_name": "League",
                "competition_kind": "league", "competition_season_address": "0x5000",
            },
            {
                "competition_id": 101, "competition_name": "Cup",
                "competition_kind": "cup", "competition_season_address": "0x5100",
            },
        ],
    }
    state.club_profile = None
    state.club_profiles = {}
    state.club_refreshing = False
    state.club_loading_profiles = {}
    state.club_error = None
    state.world_club_cache = None
    return state


def test_club_brochure_is_removed_from_shop_but_legacy_product_remains() -> None:
    assert "club_brochure" not in SHOP_SKUS
    assert "player_brochure" in SHOP_SKUS
    assert PRODUCTS["club_brochure"]["price"] == 20_000_000.0
    assert PRODUCTS["player_brochure"]["price"] == 20_000_000.0
    assert PRODUCTS["club_brochure"]["duration"] == "instant"
    assert PRODUCTS["player_brochure"]["duration"] == "instant"
    assert "league_brochure" in SHOP_SKUS
    assert PRODUCTS["league_brochure"]["price"] == 40_000_000.0
    assert PRODUCTS["league_brochure"]["duration"] == "instant"
    assert PRODUCTS["league_brochure"]["description"] == "使指定联赛的原生声望提升10点。"


def test_league_targets_only_include_addressable_leagues() -> None:
    targets = _state()._reputation_league_targets()

    assert targets == [{
        "id": 100, "name": "League",
        "competition_season_address": "0x5000", "reputation": 150,
    }]


def test_club_targets_include_managed_and_current_acquired_clubs_only() -> None:
    state = _state()
    native = {
        "addresses_current": True,
        "clubs": [
            {"id": 20, "name": "Acquired Club", "address": "0x2000", "reputation": 7000},
        ],
    }
    with (
        patch.object(state, "_data_scope_id", return_value="save-1"),
        patch.object(state, "_world_club_native_cache", return_value=native),
        patch.object(state, "_owned_world_club_address_overrides", return_value={}),
        patch("fm_odds_web.load_acquired_clubs", return_value={"clubs": [{"id": 20}, {"id": 30}]}),
    ):
        result = state._reputation_club_targets()

    assert [(row["id"], row["source"]) for row in result["clubs"]] == [
        (10, "managed"), (20, "acquired"),
    ]
    assert result["unavailable_acquired_count"] == 1


def test_reputation_targets_return_lightweight_managed_club_players() -> None:
    state = _state()
    state.club_profiles = {
        10: {
            "team": {"id": 10, "name": "Managed Club", "team_type": "club"},
            "players": [{
                "id": 42, "name": "Player", "world_reputation": 5000,
                "address": "0x4200", "training_ca": {"costs": {"large": [1] * 5000}},
            }],
        },
        11: {
            "team": {"id": 11, "name": "National Team", "team_type": "national"},
            "players": [{"id": 43, "name": "International Player"}],
        },
    }
    with (
        patch.object(state, "_bind_current_save"),
        patch.object(state, "_reputation_club_targets", return_value={
            "clubs": [], "unavailable_acquired_count": 0,
        }),
    ):
        result = state.reputation_item_targets()

    assert result["players"] == [{
        "id": 42, "name": "Player", "team_id": 10,
        "team_name": "Managed Club", "reputation": 5000,
    }]
    assert result["players_ready"] is True
    assert "training_ca" not in result["players"][0]
    assert "address" not in result["players"][0]


def test_player_brochure_frontend_uses_lightweight_targets_and_team_id() -> None:
    script = ((Path(__file__).parents[1] / "src") / "web" / "app.js").read_text(encoding="utf-8")
    start = script.index("async function openReputationItem")
    end = script.index("async function openHealingItem", start)
    block = script[start:end]

    assert 'loadReputationTargets(targetKind === "player")' in block
    assert "response.players || []" in block
    assert "ensureClubLoaded(true)" not in block
    assert 'data-team-id="${teamId}"' in block
    assert "selectedTarget?.dataset.teamId" in script


def test_club_brochure_rolls_back_when_inventory_consumption_fails() -> None:
    state = _state()
    item = {"id": "brochure-1", "sku": "club_brochure", "status": "available"}
    write_result = {"team_id": 10, "before": 9900, "after": 10000, "applied": 100}
    with (
        patch.object(state, "_bind_current_save"),
        patch.object(state, "_reputation_club_targets", return_value={
            "clubs": [{"id": 10, "name": "Managed Club", "address": "0x1000", "source": "managed"}],
        }),
        patch("fm_odds_web.public_economy", return_value={"inventory": [item]}),
        patch("fm_odds_web.apply_team_reputation_delta", side_effect=[write_result, write_result]) as apply_reputation,
        patch("fm_odds_web.consume_instant_item", side_effect=RuntimeError("disk full")),
    ):
        with pytest.raises(RuntimeError, match="disk full"):
            state.inventory_use_reputation({"item_id": "brochure-1", "team_id": 10})

    assert apply_reputation.call_args_list[0].args == (10, "0x1000", 200)
    assert apply_reputation.call_args_list[1].args == (10, "0x1000", -100)


def test_player_brochure_updates_managed_club_player_cache() -> None:
    state = _state()
    item = {"id": "brochure-2", "sku": "player_brochure", "status": "available"}
    profile = {"team": {"id": 10, "team_type": "club"}}
    player = {"id": 42, "name": "Player", "address": "0x4200", "world_reputation": 5000}
    result = {"player_id": 42, "before": 5000, "after": 5100, "applied": 100}
    with (
        patch.object(state, "_bind_current_save"),
        patch.object(state, "_live_player_profile", return_value=(profile, player)) as live_profile,
        patch("fm_odds_web.public_economy", return_value={"inventory": [item]}),
        patch("fm_odds_web.apply_player_world_reputation_delta", return_value=result),
        patch("fm_odds_web.consume_instant_item", return_value={"inventory": []}),
    ):
        response = state.inventory_use_reputation({
            "item_id": "brochure-2", "player_id": 42, "team_id": 10,
        })

    live_profile.assert_called_once_with(42, 10)
    assert response["player"]["after"] == 5100
    assert player["world_reputation"] == 5100
    assert player["international_reputation"] == 5100


def test_league_brochure_rolls_back_when_inventory_consumption_fails() -> None:
    state = _state()
    item = {"id": "brochure-3", "sku": "league_brochure", "status": "available"}
    write_result = {
        "competition_id": 100, "competition_name": "League",
        "before": 150, "after": 160, "applied": 10,
    }
    with (
        patch.object(state, "_bind_current_save"),
        patch("fm_odds_web.public_economy", return_value={"inventory": [item]}),
        patch(
            "fm_odds_web.apply_competition_reputation_delta",
            side_effect=[write_result, write_result],
        ) as apply_reputation,
        patch("fm_odds_web.consume_instant_item", side_effect=RuntimeError("disk full")),
    ):
        with pytest.raises(RuntimeError, match="disk full"):
            state.inventory_use_reputation({"item_id": "brochure-3", "competition_id": 100})

    assert apply_reputation.call_args_list[0].args == (100, "0x5000", 10)
    assert apply_reputation.call_args_list[1].args == (100, "0x5000", -10)


def test_player_world_reputation_write_validates_id_and_clamps_to_maximum() -> None:
    base = 0x100000
    player_address = 0x4200
    reputation_address = player_address + 0x100
    current = {"value": 9950}
    layout = SimpleNamespace(
        player_world_reputation_offset=0x100,
        player_and_non_player_vtable_rvas=set(),
        actual_player_vtable_rvas={0x123},
        player_and_non_player_person_offset=0x300,
        player_person_offset=0x200,
        module_name="fm.exe",
        module=lambda _process: SimpleNamespace(base_address=base),
    )

    class FakeReader:
        def ptr(self, address: int) -> int:
            return base + 0x123 if address == player_address else 0

        def u32(self, address: int) -> int:
            return 42 if address == player_address + 0x200 + 0x0C else 0

        def u16(self, address: int) -> int | None:
            return current["value"] if address == reputation_address else None

    def write(_process: object, address: int, raw: bytes) -> None:
        assert address == reputation_address
        current["value"] = struct.unpack("<H", raw)[0]

    with (
        patch("tools.club_reader.select_process_layout", return_value=(1, "fm.exe", layout)),
        patch("tools.club_reader.open_process", return_value=nullcontext(object())),
        patch("tools.club_reader.Reader", return_value=FakeReader()),
        patch("tools.club_reader.write_process_memory", side_effect=write),
    ):
        result = apply_player_world_reputation_delta(42, hex(player_address), 100)

    assert result == {"player_id": 42, "before": 9950, "after": 10000, "applied": 50}


@pytest.mark.parametrize(
    ("width", "offset", "before"),
    ((1, 0x188, 154), (2, 0x180, 185)),
)
def test_competition_reputation_write_uses_generation_width(
    width: int, offset: int, before: int,
) -> None:
    base = 0x100000
    season_address = 0x5000
    competition_address = 0x6000
    field_address = competition_address + offset
    current = {"value": before}
    layout = SimpleNamespace(
        competition_reputation_offset=offset,
        competition_reputation_bytes=width,
        module_name="game.dll",
        module=lambda _process: SimpleNamespace(base_address=base),
    )

    class FakeReader:
        def competition(self, address: int) -> dict[str, object] | None:
            if address != season_address:
                return None
            return {"id": 100, "name": "League", "address": hex(competition_address)}

        def u8(self, address: int) -> int | None:
            return current["value"] if address == field_address else None

        def u16(self, address: int) -> int | None:
            return current["value"] if address == field_address else None

    def write(_process: object, address: int, raw: bytes) -> None:
        assert address == field_address
        assert len(raw) == width
        current["value"] = struct.unpack("<B" if width == 1 else "<H", raw)[0]

    with (
        patch("tools.preview_cup_odds.select_process_layout", return_value=(1, "fm.exe", layout)),
        patch("tools.preview_cup_odds.open_process", return_value=nullcontext(object())),
        patch("tools.preview_cup_odds.Reader", return_value=FakeReader()),
        patch("tools.preview_cup_odds.write_process_memory", side_effect=write),
    ):
        result = apply_competition_reputation_delta(100, hex(season_address), 200)

    assert result["before"] == before
    assert result["after"] == 200
    assert result["applied"] == 200 - before


def test_competition_reputation_uses_the_shared_generation_layout() -> None:
    assert (FM24_LAYOUT.competition_reputation_offset, FM24_LAYOUT.competition_reputation_bytes) == (0x180, 2)
    assert (FM26_LAYOUT.competition_reputation_offset, FM26_LAYOUT.competition_reputation_bytes) == (0x188, 1)
    for layout in (FM24_240_LAYOUT, FM24_241_LAYOUT):
        assert layout.competition_reputation_offset is None
        assert layout.competition_reputation_bytes is None
    for layout in (FM24_EPIC_LAYOUT, FM24_XGP_LAYOUT):
        assert (layout.competition_reputation_offset, layout.competition_reputation_bytes) == (0x180, 2)
    assert (FM26_XGP_TEMPLATE.competition_reputation_offset, FM26_XGP_TEMPLATE.competition_reputation_bytes) == (0x188, 1)
