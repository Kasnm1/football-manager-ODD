from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from frontend_source import legacy_web_source
from tools import app_settings


_ORIGINAL_PATH_READ_TEXT = Path.read_text
_LEGACY_FRONTEND_ASSERTION_FILES = {
    "test_activity_centre_frontend.py", "test_bet_submission_frontend.py",
    "test_canteen_page.py", "test_championship_odds.py",
    "test_club_balance_ownership_gate.py", "test_club_frontend.py",
    "test_club_information.py", "test_club_legacy_frontend.py",
    "test_club_policies.py", "test_club_vision.py",
    "test_competition_navigation_frontend.py", "test_enlightenment_pills.py",
    "test_existing_page_hierarchy.py", "test_frontend_refresh_recovery.py",
    "test_global_statistics.py", "test_home_desktop.py",
    "test_knockout_results.py", "test_manager_coach_operations.py",
    "test_match_intelligence_frontend.py", "test_money_abbreviations_frontend.py",
    "test_owned_club_detail_frontend.py", "test_owned_club_operations.py",
    "test_pending_result_search.py", "test_player_card_frontend.py",
    "test_player_portraits.py", "test_portfolio_tool_frontend.py",
    "test_profit_analysis_colors.py", "test_relationship_frontend.py",
    "test_relationship_training_frontend.py", "test_result_settlement_regressions.py",
    "test_retirement.py", "test_shell_frontend_optimization.py",
    "test_state_payload_optimization.py", "test_training_ground.py",
    "test_utility_frontend_optimization.py", "test_world_clubs.py",
    "test_world_nations.py", "test_world_players.py", "test_youth_intake.py",
}


@pytest.fixture(autouse=True)
def isolate_app_settings(tmp_path, monkeypatch):
    """Keep tests deterministic and away from the developer's live settings."""
    monkeypatch.setattr(app_settings, "SETTINGS_PATH", tmp_path / "settings.json")


def _read_text_with_legacy_frontend_projection(self, *args, **kwargs):
    """Preserve old structural assertions while callers migrate explicitly."""
    if self.name not in {"app.js", "index.html"}:
        return _ORIGINAL_PATH_READ_TEXT(self, *args, **kwargs)
    callers = {Path(frame.filename).name for frame in inspect.stack(context=0)}
    if not callers.intersection(_LEGACY_FRONTEND_ASSERTION_FILES):
        return _ORIGINAL_PATH_READ_TEXT(self, *args, **kwargs)
    return legacy_web_source(self)


# Installed before test-module collection so old module-level constants retain
# compatibility. New tests should call frontend_source.read_frontend_source()
# with an explicit mode instead. Production code is unaffected.
Path.read_text = _read_text_with_legacy_frontend_projection
