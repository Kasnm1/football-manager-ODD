from __future__ import annotations

import pytest

from tools.result_evidence import (
    _result_identity,
    discard_result_details,
    first_scorer_detail_consistent,
    halftime_details_consistent,
    result_details_consistent,
    result_snapshot_matches,
    sanitize_result_details,
)


def _result(home_goals: int = 2, away_goals: int = 1, **details: object) -> dict[str, object]:
    return {
        "date": "2028-06-06",
        "competition": {"id": "100"},
        "home_team": {"id": "10"},
        "away_team": {"id": "20"},
        "home_goals": home_goals,
        "away_goals": away_goals,
        **details,
    }


def test_result_identity_normalizes_ids_and_rejects_invalid_dates() -> None:
    assert _result_identity(_result()) == ("2028-06-06", 100, 10, 20)

    with pytest.raises(ValueError, match="result date is unavailable"):
        _result_identity({**_result(), "date": ""})
    with pytest.raises(ValueError):
        _result_identity({**_result(), "date": "2028-99-06"})


def test_sanitize_preserves_independent_valid_detail_family_without_mutating_input() -> None:
    source = _result(
        half_home_goals=1,
        half_away_goals=0,
        first_scoring_team="none",
    )

    assert halftime_details_consistent(source)
    assert not first_scorer_detail_consistent(source)
    assert not result_details_consistent(source)
    assert sanitize_result_details(source) == _result(half_home_goals=1, half_away_goals=0)
    assert source["first_scoring_team"] == "none"


def test_first_scorer_requires_none_only_for_scoreless_results() -> None:
    assert first_scorer_detail_consistent(_result(0, 0, first_scoring_team="none"))
    assert not first_scorer_detail_consistent(_result(0, 0, first_scoring_team="home"))
    assert first_scorer_detail_consistent(_result(1, 0, first_scoring_team="home"))
    assert not first_scorer_detail_consistent(_result(1, 0, first_scoring_team="none"))


def test_discard_result_details_returns_a_copy_without_settlement_details() -> None:
    source = _result(
        half_home_goals=1,
        half_away_goals=0,
        first_scoring_team="home",
    )

    assert discard_result_details(source) == _result()
    assert "half_home_goals" in source


def test_snapshot_match_uses_aggregate_score_and_rejects_stale_or_invalid_rows() -> None:
    expected = _result(1, 2, aggregate_home_goals=3, aggregate_away_goals=4)
    current = _result(3, 4, result_address="0x2000")

    assert result_snapshot_matches(expected, current)
    assert not result_snapshot_matches(expected, _result(1, 2))
    assert not result_snapshot_matches(expected, None)
    assert not result_snapshot_matches(expected, {"date": "not-a-date"})
