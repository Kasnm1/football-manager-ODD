"""Persistent player-manager records derived from verified match results."""

from __future__ import annotations

import json
import threading
from datetime import date, datetime
from typing import Any

from tools.account_store import load_document, save_document


_LOCK = threading.RLock()


def _parse_date(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _result_team(row: dict[str, Any], side: str) -> dict[str, Any]:
    value = row.get(f"{side}_team") or row.get(side) or {}
    return value if isinstance(value, dict) else {}


def _readable_results(
    results: list[dict[str, Any]], team_id: int, tenure_start: str | None,
) -> list[dict[str, str]]:
    earliest = _parse_date(tenure_start)
    readable: dict[str, dict[str, str]] = {}
    for row in results:
        if not isinstance(row, dict):
            continue
        result_date = _parse_date(row.get("date"))
        if not result_date or (earliest and result_date < earliest):
            continue
        home = _result_team(row, "home")
        away = _result_team(row, "away")
        competition = row.get("competition") or {}
        competition = competition if isinstance(competition, dict) else {}
        try:
            home_id = int(home.get("id") or 0)
            away_id = int(away.get("id") or 0)
            home_goals = int(row["home_goals"])
            away_goals = int(row["away_goals"])
            competition_id = int(
                row.get("competition_id")
                or competition.get("id")
                or 0
            )
        except (KeyError, TypeError, ValueError):
            continue
        if team_id not in {home_id, away_id} or home_id == away_id:
            continue
        if home_goals == away_goals:
            outcome = "draw"
        else:
            won = (team_id == home_id) == (home_goals > away_goals)
            outcome = "win" if won else "loss"
        key = json.dumps(
            [result_date.isoformat(), competition_id, home_id, away_id],
            separators=(",", ":"),
        )
        readable[key] = {
            "key": key, "date": result_date.isoformat(), "outcome": outcome,
        }
    return list(readable.values())


def _summary(rows: list[dict[str, Any]]) -> dict[str, int | float]:
    wins = sum(row.get("outcome") == "win" for row in rows)
    draws = sum(row.get("outcome") == "draw" for row in rows)
    losses = sum(row.get("outcome") == "loss" for row in rows)
    played = wins + draws + losses
    return {
        "played": played,
        "wins": wins,
        "draws": draws,
        "losses": losses,
        "win_rate": round(wins * 100 / played, 1) if played else 0.0,
    }


def sync_player_manager_record(
    manager_id: int,
    team_id: int,
    tenure_start: str | None,
    results: list[dict[str, Any]],
    scope_id: str = "",
) -> dict[str, Any] | None:
    """Accumulate verified results for one player-manager and club pairing."""
    manager_id = int(manager_id or 0)
    team_id = int(team_id or 0)
    if manager_id <= 0 or team_id <= 0:
        return None

    with _LOCK:
        payload = load_document(
            "manager_records", {"schema_version": 1, "records": {}},
            scope_id or None,
        )
        if not isinstance(payload, dict):
            payload = {"schema_version": 1, "records": {}}
        records = payload.get("records")
        if not isinstance(records, dict):
            records = {}
            payload["records"] = records
        record_key = f"{manager_id}:{team_id}"
        previous = records.get(record_key)
        state = dict(previous) if isinstance(previous, dict) else {}

        stored_start = str(state.get("tenure_start") or "") or None
        supplied_start = str(tenure_start or "") or None
        valid_starts = [
            value for value in (stored_start, supplied_start) if _parse_date(value)
        ]
        effective_start = min(valid_starts) if valid_starts else None
        available = _readable_results(results, team_id, effective_start)
        counted = {
            str(row.get("key")): dict(row)
            for row in state.get("counted_results") or []
            if isinstance(row, dict) and row.get("key")
            and row.get("outcome") in {"win", "draw", "loss"}
        }
        counted.update({row["key"]: row for row in available})
        counted_rows = sorted(
            counted.values(), key=lambda row: (str(row.get("date") or ""), row["key"]),
        )
        next_state = {
            "schema_version": 1,
            "manager_id": manager_id,
            "team_id": team_id,
            "tenure_start": effective_start,
            "tracking_started_at": state.get("tracking_started_at")
            or datetime.now().isoformat(timespec="seconds"),
            "counted_results": counted_rows,
        }
        if next_state != previous:
            records[record_key] = next_state
            payload["schema_version"] = 1
            save_document("manager_records", payload, scope_id or None)

    return {
        "manager_id": manager_id,
        "team_id": team_id,
        "tenure_start": effective_start,
        "total": _summary(counted_rows),
        "source": "verified_result_history",
    }


__all__ = ["sync_player_manager_record"]
