from __future__ import annotations

from copy import deepcopy
import threading
from unittest.mock import MagicMock, call, patch

import pytest

from fm_odds_web import LocalOddsState
from tools.club_economy import (
    activate_scheduled_referee_item,
    reconcile_inventory,
    referee_hook_candidate,
)
from tools.referee_hook_sync import RefereeHookController


def referee_item(*, status: str = "scheduled", fixture_date: str = "2030-01-10") -> dict:
    return {
        "id": "referee-1",
        "sku": "referee_level2",
        "status": status,
        "duration": "fixture",
        "referee_level": 2,
        "fixture_key": f"{fixture_date}|1|10|20",
        "fixture_identity": "1|10|20",
        "fixture_date": fixture_date,
        "managed_team_id": 10,
    }


def referee_state(hook_status: dict) -> LocalOddsState:
    state = LocalOddsState.__new__(LocalOddsState)
    state.club_contexts = {
        10: {"team": {"address": "0x1000", "club_address": "0x2000"}},
    }
    state.club_context = None
    state.output = {"managed_teams": [{"id": 10}]}
    state.referee_hook = MagicMock()
    state.referee_hook.sync.return_value = hook_status
    return state


def test_match_day_reconcile_does_not_activate_referee_before_hook() -> None:
    economy = {"inventory": [referee_item()], "match_item_history": [], "transactions": []}
    with patch("tools.club_economy.load_economy", side_effect=lambda: economy), patch(
        "tools.club_economy.save_economy",
    ), patch(
        "tools.club_economy.public_economy", side_effect=lambda: deepcopy(economy),
    ):
        reconcile_inventory(
            "2030-01-10", {"1|10|20": {"date": "2030-01-10"}},
            activate_referee=False,
        )

    assert economy["inventory"][0]["status"] == "scheduled"


def test_hook_ready_commit_activates_the_scheduled_referee_item() -> None:
    economy = {"inventory": [referee_item()], "transactions": []}
    with patch("tools.club_economy.load_economy", side_effect=lambda: economy), patch(
        "tools.club_economy.save_economy",
    ) as save:
        activated = activate_scheduled_referee_item("referee-1", "2030-01-10")

    assert activated["status"] == "active"
    assert economy["inventory"][0]["status"] == "active"
    save.assert_called_once_with(economy)


def test_future_referee_item_does_not_install_hook_immediately() -> None:
    economy = {"inventory": [referee_item(fixture_date="2030-01-11")]}
    state = referee_state({"installed": False, "armed": False, "error": None})
    with patch("tools.club_economy.load_economy", side_effect=lambda: economy):
        assert referee_hook_candidate("2030-01-10") is None
        result = state._sync_referee_fixture_hook("2030-01-10")

    assert result["state"] == "inactive"
    state.referee_hook.sync.assert_called_once_with(0, 0, 0)


def test_match_day_hook_failure_keeps_referee_item_pending() -> None:
    item = referee_item()
    state = referee_state({"installed": False, "armed": False, "error": "AOB missing"})
    with patch("fm_odds_web.referee_hook_candidate", return_value=item), patch(
        "fm_odds_web.activate_scheduled_referee_item",
    ) as activate:
        result = state._sync_referee_fixture_hook("2030-01-10")

    assert result["state"] == "pending"
    activate.assert_not_called()
    state.referee_hook.sync.assert_called_once_with(2, 0x1000, 0x2000)


def test_same_day_background_maintenance_retries_pending_referee_hook() -> None:
    state = LocalOddsState.__new__(LocalOddsState)
    state.output = {
        "save_instance_id": "save-1", "account_scope_id": "account-1",
        "game_date": "2030-01-10", "matches": [],
    }
    state.memory_lock = threading.Lock()
    state.fixture_item_sync_key = ("account-1", "2030-01-10")
    state.fixture_effect_maintenance_at = 0.0
    state.fixture_item_sync_error = None
    state._data_scope_id = MagicMock(return_value="account-1")
    state._sync_referee_fixture_hook = MagicMock(return_value={
        "state": "pending", "hook": {"error": "AOB missing"},
    })
    state._remember_fixture_effect_status = MagicMock()
    with patch("fm_odds_web.monotonic", return_value=10.0), patch(
        "fm_odds_web.active_doping_items", return_value=[],
    ), patch(
        "fm_odds_web.active_goalkeeper_bribes", return_value=[],
    ), patch(
        "fm_odds_web.referee_hook_candidate", return_value=referee_item(),
    ):
        state._sync_fixture_items_for_clock({"date": "2030-01-10", "minutes": 1})

    state._sync_referee_fixture_hook.assert_called_once_with("2030-01-10")
    assert state.fixture_item_sync_error == "AOB missing"
    assert state.fixture_effect_maintenance_at == 15.0


def test_retry_arms_hook_before_committing_active_status() -> None:
    events: list[str] = []
    item = referee_item()
    state = referee_state({"installed": True, "armed": True, "error": None})
    state.referee_hook.sync.side_effect = lambda *_args: (
        events.append("hook") or {"installed": True, "armed": True, "error": None}
    )
    with patch("fm_odds_web.referee_hook_candidate", return_value=item), patch(
        "fm_odds_web.activate_scheduled_referee_item",
        side_effect=lambda *_args: events.append("active"),
    ):
        result = state._sync_referee_fixture_hook("2030-01-10")

    assert events == ["hook", "active"]
    assert result["state"] == "active"


def test_activation_commit_failure_uninstalls_the_hook() -> None:
    item = referee_item()
    state = referee_state({"installed": True, "armed": True, "error": None})
    with patch("fm_odds_web.referee_hook_candidate", return_value=item), patch(
        "fm_odds_web.activate_scheduled_referee_item", side_effect=RuntimeError("save failed"),
    ), pytest.raises(RuntimeError, match="save failed"):
        state._sync_referee_fixture_hook("2030-01-10")

    assert state.referee_hook.sync.call_args_list == [
        call(2, 0x1000, 0x2000),
        call(0, 0, 0),
    ]


def test_next_game_day_consumes_active_item_and_uninstalls_hook() -> None:
    economy = {"inventory": [referee_item(status="active")], "match_item_history": [], "transactions": []}
    state = referee_state({"installed": False, "armed": False, "error": None})
    with patch("tools.club_economy.load_economy", side_effect=lambda: economy), patch(
        "tools.club_economy.save_economy",
    ), patch(
        "tools.club_economy.public_economy", side_effect=lambda: deepcopy(economy),
    ):
        reconcile_inventory("2030-01-11", {}, activate_referee=False)
        result = state._sync_referee_fixture_hook("2030-01-11")

    assert economy["inventory"] == []
    assert economy["match_item_history"][0]["status"] == "consumed"
    assert result["state"] == "inactive"
    state.referee_hook.sync.assert_called_once_with(0, 0, 0)


def test_missing_team_address_cannot_report_referee_hook_as_active() -> None:
    status = RefereeHookController().sync(1, 0, 0)

    assert status["installed"] is False
    assert status["armed"] is False
    assert status["error"] == "尚未读取到黑哨目标球队地址"


def test_inventory_use_does_not_report_an_unrelated_active_effect_error() -> None:
    state = LocalOddsState.__new__(LocalOddsState)
    state.lock = threading.RLock()
    state.memory_lock = threading.RLock()
    state._bind_current_save = MagicMock()
    state.club_profile = None
    state.club_profiles = {}
    state.club_context = None
    state.club_contexts = {}
    state.output = {
        "managed_teams": [{"id": 10, "name": "Club", "address": "0x1000"}],
        "matches": [{
            "fixture_date": "2030-01-10", "competition_id": 1,
            "kickoff_minutes": 900,
            "home": {"id": 10, "name": "Club"},
            "away": {"id": 20, "name": "Opponent"},
        }],
    }
    state.redbull_hook = MagicMock()
    state.redbull_hook.sync.return_value = {"error": "unrelated drink Hook failed"}
    state._sync_opponent_flu_items = MagicMock()
    state._remember_fixture_effect_status = MagicMock()
    pending = {"state": "pending", "item_id": "referee-1", "hook": {"error": "AOB missing"}}
    item = {"id": "referee-1", "sku": "referee", "status": "available"}
    with patch("fm_odds_web.public_economy", return_value={"inventory": [item]}), patch(
        "fm_odds_web.select_process_layout",
    ), patch(
        "fm_odds_web.read_game_clock", return_value={"date": "2030-01-10", "minutes": 0},
    ), patch(
        "fm_odds_web.assign_fixture_item",
    ), patch(
        "fm_odds_web.reconcile_inventory",
    ), patch(
        "fm_odds_web.active_doping_items", return_value=[],
    ), patch(
        "fm_odds_web.active_goalkeeper_bribes", return_value=[],
    ), patch(
        "fm_odds_web.active_player_ids", return_value=[],
    ), patch(
        "fm_odds_web.sync_doping_effects", return_value={"errors": []},
    ), patch(
        "fm_odds_web.sync_goalkeeper_bribes", return_value={"errors": [], "player_ids": [99]},
    ), patch(
        "fm_odds_web.refund_active_sku",
    ), patch.object(
        state, "_sync_referee_fixture_hook", return_value=pending,
    ):
        result = state.inventory_use({
            "item_id": "referee-1", "team_id": 10,
            "fixture_key": "2030-01-10|1|10|20",
        })

    assert result["referee_activation"] == pending


def test_frontend_and_all_locales_cover_pending_referee_activation() -> None:
    from pathlib import Path

    root = (Path(__file__).resolve().parents[1] / "src")
    script = (root / "web" / "app.js").read_text(encoding="utf-8")
    assert 'refereeActivationState === "pending" ? "inventory.referee_pending"' in script
    for name in (
        "i18n.facilities.js", "i18n.tw.js", "i18n.de.js", "i18n.es.js",
        "i18n.fr.js", "i18n.ru.js", "i18n.ja.js", "i18n.pt-BR.js", "i18n.pt-PT.js",
    ):
        assert '"inventory.referee_pending"' in (root / "web" / name).read_text(encoding="utf-8")
