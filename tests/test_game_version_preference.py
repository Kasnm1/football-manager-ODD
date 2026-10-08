from __future__ import annotations

import tempfile
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from fm_odds_web import (
    LocalOddsState, remembered_account_scope_for_game, restore_preferred_game_layout,
)
from tools import app_settings


def test_preferred_game_version_is_persisted() -> None:
    with tempfile.TemporaryDirectory() as directory:
        settings_path = Path(directory) / "settings.json"
        with patch.object(app_settings, "SETTINGS_PATH", settings_path):
            saved = app_settings.set_preferred_game_version("fm24")
            loaded = app_settings.load_settings()

    assert saved["preferred_game_version"] == "fm24"
    assert loaded["preferred_game_version"] == "fm24"


def test_invalid_preferred_game_version_is_rejected() -> None:
    with pytest.raises(ValueError, match="fm24 or fm26"):
        app_settings.set_preferred_game_version("fm25")


def test_invalid_stored_preference_is_ignored() -> None:
    with tempfile.TemporaryDirectory() as directory:
        settings_path = Path(directory) / "settings.json"
        settings_path.write_text('{"preferred_game_version":"fm25"}', encoding="utf-8")
        with patch.object(app_settings, "SETTINGS_PATH", settings_path):
            loaded = app_settings.load_settings()

    assert loaded["preferred_game_version"] == ""


def test_startup_restores_preferred_running_generation() -> None:
    with (
        patch("fm_odds_web.load_settings", return_value={"preferred_game_version": "fm24"}),
        patch("fm_odds_web.select_game_layout") as select,
    ):
        restore_preferred_game_layout()

    select.assert_called_once_with("fm24")


def test_startup_ignores_temporarily_unavailable_preference() -> None:
    with (
        patch("fm_odds_web.load_settings", return_value={"preferred_game_version": "fm24"}),
        patch("fm_odds_web.select_game_layout", side_effect=ValueError("not running")) as select,
    ):
        restore_preferred_game_layout()

    select.assert_called_once_with("fm24")


def test_manual_account_scope_is_reused_for_same_manager_and_generation() -> None:
    with (
        patch("fm_odds_web.active_save_id", return_value="account-old"),
        patch("fm_odds_web.restore_active_save_id", return_value="account-old"),
        patch(
            "fm_odds_web.saved_account_scopes",
            return_value=[{
                "scope_id": "account-old", "manager_id": 42, "namespace": "fm24",
            }],
        ),
    ):
        assert remembered_account_scope_for_game(42, "fm24") == "account-old"


def test_manual_account_scope_does_not_cross_generation() -> None:
    with (
        patch("fm_odds_web.active_save_id", return_value="account-old"),
        patch("fm_odds_web.restore_active_save_id", return_value="account-old"),
        patch(
            "fm_odds_web.saved_account_scopes",
            return_value=[{
                "scope_id": "account-old", "manager_id": 42, "namespace": "fm24",
            }],
        ),
    ):
        assert remembered_account_scope_for_game(42, "fm26") is None


def test_unconnected_game_version_switch_skips_account_effect_storage() -> None:
    state = LocalOddsState.__new__(LocalOddsState)
    state.lock = threading.RLock()
    state.memory_lock = threading.RLock()
    state.output = {}
    state.cache_verified = False
    state.wallet_ready = False
    state.connection_scope_id = None
    state.refreshing = False
    state.reconciling = False
    state.club_refreshing = False
    state.data_version = 1
    for name in (
            "redbull_hook", "referee_hook", "ca_growth_hook", "attribute_growth_hook",
            "team_nuclear_hooks", "club_policy_hooks", "board_listens_hook", "simple_effects",
    ):
        setattr(state, name, MagicMock())
    state._close_youth_generation_hook = MagicMock()

    with (
        patch("fm_odds_web.restore_all_doping_effects") as restore_doping,
        patch("fm_odds_web.restore_all_goalkeeper_bribes") as restore_goalkeeper,
        patch("fm_odds_web.select_game_layout", return_value={"selected": "fm24"}),
        patch("fm_odds_web.set_preferred_game_version"),
        patch("fm_odds_web.RedBullHookController", return_value=MagicMock()),
        patch("fm_odds_web.RefereeHookController", return_value=MagicMock()),
        patch("fm_odds_web.CAGrowthHookController", return_value=MagicMock()),
        patch("fm_odds_web.AttributeGrowthHookController", return_value=MagicMock()),
            patch("fm_odds_web.TeamNuclearHookController", return_value=MagicMock()),
            patch("fm_odds_web.ClubPolicyHookController", return_value=MagicMock()),
        patch("fm_odds_web.BoardListensHookController", return_value=MagicMock()),
        patch("fm_odds_web.YouthGenerationHookController", return_value=MagicMock()),
        patch("fm_odds_web.SimpleNuclearController", return_value=MagicMock()),
    ):
        result = state.switch_game_version({"key": "fm24"})

    restore_doping.assert_not_called()
    restore_goalkeeper.assert_not_called()
    assert result["selected"] == "fm24"
    assert result["refresh_started"] is False
    assert state.output == {}
    assert state.connection_scope_id is None
