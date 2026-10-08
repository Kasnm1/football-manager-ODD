from __future__ import annotations

import threading
from pathlib import Path
from unittest.mock import Mock, patch

from fm_odds_web import LocalOddsState
from tools import club_economy


def _state() -> LocalOddsState:
    state = LocalOddsState.__new__(LocalOddsState)
    state.lock = threading.RLock()
    state.memory_lock = threading.RLock()
    state.output = {
        "save_instance_id": "save-1",
        "data_scope_id": "scope-1",
        "managed_teams": [{
            "id": 20, "team_type": "national", "address": "0xB000",
        }],
    }
    state.save_change_pending = False
    state.canteen_plan = "nutrition"
    state.club_context = {
        "team": {"address": "0x9000", "club_address": "0xA000"},
    }
    state.ca_growth_hook = Mock()
    state.ca_growth_hook.sync.return_value = {
        "installed": True, "multiplier": 2, "error": None,
    }
    state._bind_current_save = lambda: "scope-1"
    return state


def test_new_canteen_accounts_default_to_free_basic_plan() -> None:
    assert club_economy._new_state()["canteen_plan"] == "basic"
    with patch.object(club_economy, "load_economy", return_value={}):
        assert club_economy.canteen_plan() == "basic"


def test_public_economy_restores_saved_canteen_plan() -> None:
    economy = club_economy._new_state()
    economy["canteen_plan"] = "peak"

    public = club_economy._public_economy_payload(economy, 0)

    assert public["canteen_plan"] == "peak"


def test_elite_canteen_plan_enables_ca_hook() -> None:
    state = _state()
    with (
        patch("fm_odds_web.canteen_plan", return_value="basic"),
        patch("fm_odds_web.set_canteen_plan") as persist,
        patch("fm_odds_web.active_effect_skus", return_value=[]),
        patch("fm_odds_web.public_economy", return_value={"canteen_plan": "elite"}),
    ):
        result = state.canteen_plan_update({"plan": "elite"})

    assert result["canteen_plan"] == "elite"
    assert state.canteen_plan == "elite"
    persist.assert_called_once_with("elite")
    state.ca_growth_hook.sync.assert_called_once_with(
        True, 0x9000, 0xA000, 2, national_team_address=0xB000,
    )


def test_nutrition_canteen_plan_enables_real_one_point_five_hook() -> None:
    state = _state()
    state.ca_growth_hook.sync.return_value = {
        "installed": True, "multiplier": 1.5, "error": None,
    }
    with (
        patch("fm_odds_web.canteen_plan", return_value="basic"),
        patch("fm_odds_web.set_canteen_plan") as persist,
        patch("fm_odds_web.active_effect_skus", return_value=[]),
        patch("fm_odds_web.public_economy", return_value={"canteen_plan": "nutrition"}),
    ):
        result = state.canteen_plan_update({"plan": "nutrition"})

    assert result["canteen_plan"] == "nutrition"
    assert state.canteen_plan == "nutrition"
    persist.assert_called_once_with("nutrition")
    state.ca_growth_hook.sync.assert_called_once_with(
        True, 0x9000, 0xA000, 1.5, national_team_address=0xB000,
    )


def test_peak_canteen_plan_enables_three_times_ca_hook() -> None:
    state = _state()
    state.ca_growth_hook.sync.return_value = {
        "installed": True, "multiplier": 3, "error": None,
    }
    with (
        patch("fm_odds_web.canteen_plan", return_value="basic"),
        patch("fm_odds_web.set_canteen_plan") as persist,
        patch("fm_odds_web.active_effect_skus", return_value=[]),
        patch("fm_odds_web.public_economy", return_value={"canteen_plan": "peak"}),
        patch("fm_odds_web.sync_doping_effects") as sync_doping,
    ):
        result = state.canteen_plan_update({"plan": "peak"})

    assert result["canteen_plan"] == "peak"
    assert state.canteen_plan == "peak"
    persist.assert_called_once_with("peak")
    state.ca_growth_hook.sync.assert_called_once_with(
        True, 0x9000, 0xA000, 3, national_team_address=0xB000,
    )
    sync_doping.assert_not_called()


def test_canteen_plan_preserves_standalone_ca_growth_effect() -> None:
    state = _state()
    state.canteen_plan = "elite"
    with patch("fm_odds_web.active_effect_skus", return_value=["ca_growth"]):
        state._sync_ca_growth_hook(team_address=0x9000)

    state.ca_growth_hook.sync.assert_called_once_with(
        True, 0x9000, 0xA000, 2, national_team_address=0xB000,
    )


def test_canteen_hook_uses_national_assignment_without_club_context() -> None:
    state = _state()
    state.club_context = None

    state._sync_ca_growth_hook(set())

    state.ca_growth_hook.sync.assert_called_once_with(
        True, 0, 0, 1.5, national_team_address=0xB000,
    )


def test_refresh_paths_forward_current_club_address_to_ca_hook() -> None:
    source = (Path(__file__).parents[1] / "fm_odds_web.py").read_text(encoding="utf-8")

    assert source.count(
        'managed_team_address(output, "national")'
    ) == 2


def test_canteen_plan_rolls_back_when_hook_rejected() -> None:
    state = _state()
    state.ca_growth_hook.sync.return_value = {
        "installed": False, "multiplier": 2, "error": "unsupported build",
    }
    with (
        patch("fm_odds_web.set_canteen_plan") as persist,
        patch("fm_odds_web.canteen_plan", return_value="nutrition"),
        patch("fm_odds_web.active_effect_skus", return_value=[]),
    ):
        try:
            state.canteen_plan_update({"plan": "elite"})
        except RuntimeError as error:
            assert "unsupported build" in str(error)
        else:
            raise AssertionError("expected canteen plan activation failure")

    assert state.canteen_plan == "basic"
    assert persist.call_args_list == [
        (("elite",),), (("nutrition",),), (("basic",),),
    ]
