"""Whole-output fixtures for the FM-read / ODD-profile boundary."""
from copy import deepcopy
import hashlib
import json

import pytest

from tools import preview_cup_odds as facade
from tools.odds_profiles import build_team_profile


def profile_inputs(case):
    squad = [
        {
            "id": i, "ca": 200 - i * 4,
            "fitness_percent": (80, 85, 90, 95, 100)[i % 5],
            "sharpness_percent": (40, 50, 75, 90)[i % 4],
            "morale_raw": 10 + i % 11,
            "positions": [{"GK": 20}, {"DC": 20}, {"MC": 20}, {"ST": 20}][i % 4],
            "availability": {"injury_count": int(i == 1), "ban_count": int(i == 2)},
        }
        for i in range(1, 31)
    ]
    past = [
        {"date": f"2026-07-{i:02d}", "competition": {"id": 7},
         "home_team": {"id": 1 if i % 2 else 2},
         "away_team": {"id": 2 if i % 2 else 1},
         "home_goals": i % 4, "away_goals": i % 3}
        for i in range(1, 15)
    ]
    if case == "empty":
        return [], [], {}
    if case == "form_fallback":
        for player in squad:
            player["ca"] = 0
    if case == "short_squad":
        squad = squad[:5]
    if case == "zero_condition":
        for player in squad:
            player["fitness_percent"] = player["sharpness_percent"] = 0
    adjustments = {
        ((row["date"], 7, row["home_team"]["id"], row["away_team"]["id"]), 1): 0.25
        for row in past
    } if case == "elo" else {}
    return squad, past, adjustments


def profile_digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


# Captured from the pre-extraction facade; complete output, not selected fields.
BASELINE = {
    "empty": "84b26bb3126c26433e2d19d95bcca0528973b040f9f0b0c0cf0be2316d0f7d51",
    "form_fallback": "9d9aed0112e3ea6c32ab233407747a35edb9723b3e80164c6286d847c359678e",
    "short_squad": "72d5a8980af9bf8bb9d7c6cef8cf6e5153ff9e31eb5c5386273573c4afdc2e83",
    "zero_condition": "ab68d036890f359e4d4fcca328f7785fe43b5e8f5e9e38e583fc36891efd0f15",
    "elo": "9af3c4f22e85c96a12709a248fd3b5509b808d6788b6c7b8b7e5924be1c8a544",
    "full": "b586b915ac030f9623eede4b9208cf9404508601d375ff515d10461aae966e96",
}


@pytest.mark.parametrize("case", ["empty", "form_fallback", "short_squad", "zero_condition", "elo", "full"])
def test_profile_preserves_complete_output_and_input_ownership(case):
    squad, past, adjustments = profile_inputs(case)
    original = deepcopy((squad, past, adjustments))
    result = facade.team_profile(None, 0, 1, past, adjustments, squad=squad)
    assert profile_digest(result) == BASELINE[case]
    assert build_team_profile(squad, 1, past, adjustments) == result
    assert (squad, past, adjustments) == original


def test_facade_reads_once_or_uses_explicit_snapshot_without_reading():
    from unittest.mock import Mock

    squad, past, adjustments = profile_inputs("full")
    reader = Mock()
    reader.roster.return_value = squad
    assert facade.team_profile(reader, 123, 1, past, adjustments) == build_team_profile(
        squad, 1, past, adjustments,
    )
    reader.roster.assert_called_once_with(123)
    reader.roster.reset_mock()
    assert facade.team_profile(reader, 123, 1, [], squad=[]) == build_team_profile([], 1, [])
    reader.roster.assert_not_called()
    reader.roster.side_effect = RuntimeError("read failed")
    with pytest.raises(RuntimeError, match="read failed"):
        facade.team_profile(reader, 123, 1, [])


def test_pure_profile_import_does_not_load_fm_or_storage_runtime():
    import subprocess
    import sys

    subprocess.run([
        sys.executable, "-c",
        "import sys; from tools.odds_profiles import build_team_profile; "
        "assert build_team_profile([],1,[])['ca_source'] == 'default_fallback'; "
        "assert not {'tools.preview_cup_odds','tools.app_paths',"
        "'tools.game_session','tools.initial_data_audit','fm_collector.win32'}"
        ".intersection(sys.modules)",
    ], check=True)
