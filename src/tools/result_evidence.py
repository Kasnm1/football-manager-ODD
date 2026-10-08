"""Pure identity and validation rules for completed-match evidence."""

from __future__ import annotations

from datetime import date
from typing import Any


RESULT_DETAIL_FIELDS = ("half_home_goals", "half_away_goals", "first_scoring_team")


def _result_identity(item: dict[str, Any]) -> tuple[str, int, int, int]:
    """Return the stable public identity for a completed result."""
    date_text = item.get("date")
    if not isinstance(date_text, str) or not date_text:
        raise ValueError("result date is unavailable")
    date.fromisoformat(date_text)
    return (
        date_text, int(item["competition"]["id"]),
        int(item["home_team"]["id"]), int(item["away_team"]["id"]),
    )


def halftime_details_consistent(item: dict[str, Any]) -> bool:
    """Validate a complete half-time score against the immutable final score."""
    try:
        home_goals = int(item["home_goals"])
        away_goals = int(item["away_goals"])
        half_home = int(item["half_home_goals"])
        half_away = int(item["half_away_goals"])
    except (KeyError, TypeError, ValueError):
        return False
    return 0 <= half_home <= home_goals and 0 <= half_away <= away_goals


def first_scorer_detail_consistent(item: dict[str, Any]) -> bool:
    try:
        total_goals = int(item["home_goals"]) + int(item["away_goals"])
    except (KeyError, TypeError, ValueError):
        return False
    first_side = item.get("first_scoring_team")
    return first_side == "none" if total_goals == 0 else first_side in {"home", "away"}


def result_details_consistent(item: dict[str, Any]) -> bool:
    """Return true only when every settlement detail is present and valid."""
    return halftime_details_consistent(item) and first_scorer_detail_consistent(item)


def sanitize_result_details(item: dict[str, Any]) -> dict[str, Any]:
    """Discard only invalid detail families, preserving independent evidence."""
    cleaned = dict(item)
    if not halftime_details_consistent(cleaned):
        cleaned.pop("half_home_goals", None)
        cleaned.pop("half_away_goals", None)
    if not first_scorer_detail_consistent(cleaned):
        cleaned.pop("first_scoring_team", None)
    return cleaned


def discard_result_details(item: dict[str, Any]) -> dict[str, Any]:
    """Return a copied result without settlement-detail fields."""
    cleaned = dict(item)
    for field in RESULT_DETAIL_FIELDS:
        cleaned.pop(field, None)
    return cleaned


def result_snapshot_matches(expected: dict[str, Any], current: dict[str, Any] | None) -> bool:
    """Confirm that a reusable result slot still owns the expected result."""
    if current is None:
        return False
    try:
        expected_score = (
            int(expected.get("aggregate_home_goals", expected["home_goals"])),
            int(expected.get("aggregate_away_goals", expected["away_goals"])),
        )
        return (
            _result_identity(current) == _result_identity(expected)
            and int(current["home_goals"]) == expected_score[0]
            and int(current["away_goals"]) == expected_score[1]
        )
    except (KeyError, TypeError, ValueError):
        return False
