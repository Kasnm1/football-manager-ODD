from __future__ import annotations

import json
import threading
import uuid
from collections import OrderedDict, defaultdict
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any

from tools.account_store import load_document, save_document
from tools.app_paths import save_data_root


_LOCK = threading.RLock()
_PUBLIC_STANDINGS_CACHE: OrderedDict[tuple[Any, ...], dict[str, Any]] = OrderedDict()
_PUBLIC_STANDINGS_CACHE_MAX = 8
ELO_INITIAL_RATING = 1500.0
ELO_UPDATE_K = 20.0
ELO_HOME_ADVANTAGE = 60.0
POINTS_ADJUSTMENT_MIN = -100
POINTS_ADJUSTMENT_MAX = 100
FM26_CHAMPIONS_LEAGUE_IDS = {1301394}
FM26_CHAMPIONS_LEAGUE_MARKERS = ("欧冠", "欧洲冠军联赛", "uefa champions league")


def adjustments_path() -> Path:
    return save_data_root() / "competitions" / "points_adjustments.json"


def _empty_state() -> dict[str, Any]:
    return {"schema_version": 1, "records": []}


def _load_state() -> dict[str, Any]:
    payload = load_document(
        "points_adjustments", None, legacy_path=adjustments_path(),
    )
    if not isinstance(payload, dict) or not isinstance(payload.get("records"), list):
        payload = _empty_state()
    return payload


def _save_state(payload: dict[str, Any]) -> None:
    save_document("points_adjustments", payload, legacy_path=adjustments_path())


def _expected_score(rating: float, opponent: float, advantage: float = 0.0) -> float:
    return 1.0 / (1.0 + 10.0 ** ((opponent - rating - advantage) / 400.0))


def _team(item: dict[str, Any], side: str) -> tuple[int, str, str | None]:
    team = item.get(side) or item.get(f"{side}_team") or {}
    return (
        int(team.get("id") or 0),
        str(team.get("name") or team.get("short_name") or ""),
        str(team.get("address")) if team.get("address") else None,
    )


def _fm26_table_results(output: dict[str, Any]) -> list[dict[str, Any]]:
    results = list(output.get("season_results") or [])
    if output.get("game_layout") != "fm26":
        return results
    candidates = []
    for item in results:
        competition_id = int(item.get("competition_id") or 0)
        name = str(item.get("competition_name") or "").casefold()
        if (
            competition_id not in FM26_CHAMPIONS_LEAGUE_IDS
            and not any(marker in name for marker in FM26_CHAMPIONS_LEAGUE_MARKERS)
        ):
            continue
        try:
            month = int(str(item.get("date") or "")[5:7])
        except ValueError:
            continue
        if month >= 9 or month == 1:
            candidates.append(item)
    counts: dict[tuple[int, int], int] = defaultdict(int)
    for item in sorted(candidates, key=lambda row: str(row.get("date") or "")):
        competition_id = int(item.get("competition_id") or 0)
        home_id = _team(item, "home")[0]
        away_id = _team(item, "away")[0]
        if not home_id or not away_id:
            continue
        home_key, away_key = (competition_id, home_id), (competition_id, away_id)
        if counts[home_key] >= 8 or counts[away_key] >= 8:
            continue
        item["competition_kind"] = "league"
        item["table_phase"] = "league_phase"
        counts[home_key] += 1
        counts[away_key] += 1
    return results


def _regular_league_results(output: dict[str, Any]) -> list[dict[str, Any]]:
    results = _fm26_table_results(output)
    opening_stages = {
        int(item.get("competition_id") or 0): int(item["opening_stage_index"])
        for item in output.get("competition_formats") or []
        if str(item.get("competition_kind") or "") == "league"
        and str(item.get("opening_stage_type") or "") == "league"
        and item.get("opening_stage_index") is not None
    }
    filtered = []
    for result in results:
        competition_id = int(result.get("competition_id") or 0)
        stage_index = result.get("competition_stage_index")
        if (
            stage_index is not None
            and competition_id in opening_stages
            and int(stage_index) != opening_stages[competition_id]
        ):
            continue
        filtered.append(result)
    return filtered


def _standings_dependency_signature(
    output: dict[str, Any], results: list[dict[str, Any]], records: list[dict[str, Any]],
) -> tuple[Any, ...]:
    result_signature = tuple(
        (
            str(item.get("date") or ""),
            int(item.get("competition_id") or 0),
            str(item.get("competition_name") or ""),
            str(item.get("competition_kind") or ""),
            item.get("competition_stage_index"),
            *_team(item, "home"), *_team(item, "away"),
            item.get("home_goals"), item.get("away_goals"),
        )
        for item in results
    )
    format_signature = tuple(
        (
            int(item.get("competition_id") or 0),
            str(item.get("competition_name") or ""),
            str(item.get("competition_kind") or ""),
            str(item.get("competition_season_address") or ""),
            str(item.get("actual_competition_address") or ""),
            str(item.get("opening_stage_type") or ""),
            item.get("opening_stage_index"),
            int(item.get("opening_slot_count") or 0),
            bool(item.get("opening_field_complete")),
            str(item.get("first_fixture_date") or ""),
            str(item.get("last_fixture_date") or ""),
            bool(item.get("season_fixture_bounds_verified")),
            tuple(int(value) for value in item.get("knockout_active_team_ids") or []),
            tuple(int(value) for value in item.get("knockout_eliminated_team_ids") or []),
            tuple(
                (
                    int(stage.get("stage_index") or 0),
                    str(stage.get("stage_type") or ""),
                    tuple(int(value) for value in stage.get("team_ids") or []),
                )
                for stage in item.get("stages") or []
            ),
            tuple(
                (
                    int(team.get("id") or 0), str(team.get("name") or ""),
                    team.get("address"), team.get("native_standing_address"),
                    bool(team.get("native_standing_verified")),
                    int(team.get("played") or 0), int(team.get("won") or 0),
                    int(team.get("drawn") or 0), int(team.get("lost") or 0),
                    int(team.get("goals_for") or 0),
                    int(team.get("goals_against") or 0),
                    int(team.get("goal_difference") or 0),
                    int(team.get("points") or 0), int(team.get("position") or 0),
                )
                for team in item.get("opening_teams") or []
            ),
        )
        for item in output.get("competition_formats") or []
    )
    competition_signature = tuple(
        (int(item.get("id") or 0), int(item.get("reputation") or 0))
        for item in output.get("competitions") or []
    )
    record_signature = tuple(
        json.dumps(
            record, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            default=str,
        )
        for record in records
    )
    season_start_signature = tuple(sorted(
        (
            int(competition_id), str(season_start or ""),
        )
        for competition_id, season_start in (
            output.get("competition_season_starts") or {}
        ).items()
        if str(competition_id).isdigit()
    ))
    playoff_result_signature = tuple(
        (
            str(item.get("date") or ""),
            int(item.get("competition_id") or 0),
            item.get("competition_stage_index"),
            *_team(item, "home"), *_team(item, "away"),
            item.get("home_goals"), item.get("away_goals"),
            item.get("advanced_team_id"), item.get("winner_side"),
            item.get("after_extra_time_home_goals"),
            item.get("after_extra_time_away_goals"),
            item.get("penalty_shootout_home_goals"),
            item.get("penalty_shootout_away_goals"),
        )
        for item in output.get("season_results") or []
        if item.get("competition_stage_index") is not None
    )
    return (
        str(output.get("save_instance_id") or ""),
        str(output.get("game_layout") or ""),
        str(output.get("game_date") or ""),
        str(output.get("season_start") or ""),
        str(output.get("season_end") or ""),
        season_start_signature,
        result_signature, playoff_result_signature, format_signature,
        competition_signature,
        record_signature,
    )


def _playoff_result_winner(
    results: list[dict[str, Any]], team_ids: set[int],
) -> int | None:
    clean = [
        row for row in results
        if not row.get("result_conflict")
        and {_team(row, "home")[0], _team(row, "away")[0]} == team_ids
    ]
    if not clean:
        return None
    for row in reversed(sorted(clean, key=lambda item: str(item.get("date") or ""))):
        advanced = row.get("advanced_team") or {}
        advanced_id = int(
            row.get("advanced_team_id") or advanced.get("id") or 0
        )
        if advanced_id in team_ids:
            return advanced_id
    totals = {team_id: 0 for team_id in team_ids}
    for row in clean:
        home_id = _team(row, "home")[0]
        away_id = _team(row, "away")[0]
        try:
            totals[home_id] += int(row["home_goals"])
            totals[away_id] += int(row["away_goals"])
        except (KeyError, TypeError, ValueError):
            return None
    ordered = sorted(totals.items(), key=lambda item: item[1], reverse=True)
    if ordered[0][1] != ordered[1][1]:
        return ordered[0][0]
    last = max(clean, key=lambda item: str(item.get("date") or ""))
    for home_field, away_field in (
        ("penalty_shootout_home_goals", "penalty_shootout_away_goals"),
        ("after_extra_time_home_goals", "after_extra_time_away_goals"),
    ):
        try:
            home_score = int(last[home_field])
            away_score = int(last[away_field])
        except (KeyError, TypeError, ValueError):
            continue
        if home_score != away_score:
            return _team(last, "home" if home_score > away_score else "away")[0]
    side = str(last.get("winner_side") or last.get("advanced_side") or "")
    winner_id = _team(last, side)[0] if side in {"home", "away"} else 0
    return winner_id if winner_id in team_ids else None


def _apply_same_league_playoff_positions(
    output: dict[str, Any], competitions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Apply a two-team same-league playoff only to final table positions."""
    formats = {
        int(item.get("competition_id") or 0): item
        for item in output.get("competition_formats") or []
    }
    all_results = _fm26_table_results(output)
    adjusted = []
    for competition in competitions:
        competition = dict(competition)
        competition_id = int(competition.get("competition_id") or 0)
        format_row = formats.get(competition_id) or {}
        opening_stage = format_row.get("opening_stage_index")
        terminal_stages = [
            stage for stage in format_row.get("stages") or []
            if str(stage.get("stage_type") or "") == "cup"
            and (
                opening_stage is None
                or int(stage.get("stage_index") or 0) > int(opening_stage)
            )
        ]
        terminal = terminal_stages[-1] if terminal_stages else None
        playoff_ids = {
            int(value) for value in (terminal or {}).get("team_ids") or []
            if int(value) > 0
        }
        rows = [dict(row) for row in competition.get("teams") or []]
        row_ids = {int(row.get("team_id") or 0) for row in rows}
        if len(playoff_ids) != 2 or not playoff_ids.issubset(row_ids):
            adjusted.append(competition)
            continue
        active_ids = {
            int(value) for value in format_row.get("knockout_active_team_ids") or []
            if int(value) in playoff_ids
        }
        eliminated_ids = {
            int(value) for value in format_row.get("knockout_eliminated_team_ids") or []
            if int(value) in playoff_ids
        }
        winner_id = (
            next(iter(active_ids))
            if len(active_ids) == 1 and len(eliminated_ids) == 1 else None
        )
        if winner_id is None:
            playoff_results = [
                result for result in all_results
                if int(result.get("competition_id") or 0) == competition_id
                and result.get("competition_stage_index") is not None
                and (
                    opening_stage is None
                    or int(result["competition_stage_index"]) != int(opening_stage)
                )
            ]
            winner_id = _playoff_result_winner(playoff_results, playoff_ids)
        if winner_id not in playoff_ids:
            adjusted.append(competition)
            continue
        loser_id = next(team_id for team_id in playoff_ids if team_id != winner_id)
        by_id = {int(row.get("team_id") or 0): row for row in rows}
        winner = by_id[winner_id]
        loser = by_id[loser_id]
        winner_position = int(winner.get("position") or 0)
        loser_position = int(loser.get("position") or 0)
        if winner_position > loser_position > 0:
            winner["position"], loser["position"] = loser_position, winner_position
            rows.sort(key=lambda row: (
                int(row.get("position") or 999), int(row.get("team_id") or 0),
            ))
            competition["teams"] = rows
            competition["playoff_position_applied"] = True
        adjusted.append(competition)
    return adjusted


def build_standings(
    results: list[dict[str, Any]], records: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    tables: dict[int, dict[int, dict[str, Any]]] = defaultdict(dict)
    competition_names: dict[int, str] = {}
    ordered = sorted(
        (item for item in results if item.get("competition_kind") == "league"),
        key=lambda item: (
            str(item.get("date") or ""), int(item.get("competition_id") or 0),
            _team(item, "home")[0], _team(item, "away")[0],
        ),
    )
    ratings: dict[tuple[int, int], float] = {}
    elo_matches: dict[tuple[int, int], int] = defaultdict(int)
    for result in ordered:
        competition_id = int(result.get("competition_id") or 0)
        home_id, home_name, home_address = _team(result, "home")
        away_id, away_name, away_address = _team(result, "away")
        if not competition_id or not home_id or not away_id:
            continue
        try:
            home_goals = int(result["home_goals"])
            away_goals = int(result["away_goals"])
        except (KeyError, TypeError, ValueError):
            continue
        competition_names[competition_id] = str(result.get("competition_name") or competition_id)
        table = tables[competition_id]
        for team_id, team_name, team_address in (
            (home_id, home_name, home_address), (away_id, away_name, away_address),
        ):
            table.setdefault(team_id, {
                "team_id": team_id, "team_name": team_name or str(team_id),
                "team_address": team_address,
                "played": 0, "won": 0, "drawn": 0, "lost": 0,
                "goals_for": 0, "goals_against": 0, "base_points": 0,
            })
        home, away = table[home_id], table[away_id]
        home["played"] += 1
        away["played"] += 1
        home["goals_for"] += home_goals
        home["goals_against"] += away_goals
        away["goals_for"] += away_goals
        away["goals_against"] += home_goals
        if home_goals > away_goals:
            home["won"] += 1; home["base_points"] += 3; away["lost"] += 1
            actual_home = 1.0
        elif home_goals < away_goals:
            away["won"] += 1; away["base_points"] += 3; home["lost"] += 1
            actual_home = 0.0
        else:
            home["drawn"] += 1; away["drawn"] += 1
            home["base_points"] += 1; away["base_points"] += 1
            actual_home = 0.5
        home_key, away_key = (competition_id, home_id), (competition_id, away_id)
        home_rating = ratings.get(home_key, ELO_INITIAL_RATING)
        away_rating = ratings.get(away_key, ELO_INITIAL_RATING)
        change = ELO_UPDATE_K * (
            actual_home - _expected_score(home_rating, away_rating, ELO_HOME_ADVANTAGE)
        )
        ratings[home_key] = home_rating + change
        ratings[away_key] = away_rating - change
        elo_matches[home_key] += 1
        elo_matches[away_key] += 1

    adjustments: dict[tuple[int, int], int] = defaultdict(int)
    for record in records or []:
        try:
            adjustments[(int(record["competition_id"]), int(record["team_id"]))] += int(record["delta"])
        except (KeyError, TypeError, ValueError):
            continue

    output = []
    for competition_id, teams in tables.items():
        rows = []
        for team_id, row in teams.items():
            row = dict(row)
            row["goal_difference"] = row["goals_for"] - row["goals_against"]
            row["points_adjustment"] = adjustments[(competition_id, team_id)]
            row["points"] = row["base_points"] + row["points_adjustment"]
            row["elo"] = round(ratings.get((competition_id, team_id), ELO_INITIAL_RATING), 1)
            row["elo_matches"] = int(elo_matches[(competition_id, team_id)])
            rows.append(row)
        rows.sort(key=lambda row: (
            -row["points"], -row["goal_difference"], -row["goals_for"],
            row["team_name"].casefold(), row["team_id"],
        ))
        for position, row in enumerate(rows, 1):
            row["position"] = position
        output.append({
            "competition_id": competition_id,
            "competition_name": competition_names[competition_id],
            "teams": rows,
        })
    output.sort(key=lambda item: item["competition_name"].casefold())
    return output


def _native_format_standings(
    output: dict[str, Any],
    reconstructed: list[dict[str, Any]],
    records: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    reconstructed_competitions = {
        int(competition.get("competition_id") or 0): competition
        for competition in reconstructed
    }
    reconstructed_rows = {
        (int(competition.get("competition_id") or 0), int(team.get("team_id") or 0)): team
        for competition in reconstructed
        for team in competition.get("teams") or []
    }
    adjustments: dict[tuple[int, int], int] = defaultdict(int)
    for record in records:
        try:
            adjustments[(
                int(record["competition_id"]), int(record["team_id"]),
            )] += int(record["delta"])
        except (KeyError, TypeError, ValueError):
            continue
    competitions = []
    for item in output.get("competition_formats") or []:
        competition_id = int(item.get("competition_id") or 0)
        teams = list(item.get("opening_teams") or [])
        expected = int(item.get("opening_slot_count") or 0)
        if (
            not competition_id
            or str(item.get("competition_kind") or "") != "league"
            or str(item.get("opening_stage_type") or "") != "league"
            or not item.get("opening_field_complete")
            or expected < 2 or len(teams) != expected
            or any(not team.get("native_standing_verified") for team in teams)
        ):
            continue
        rows = []
        for native in teams:
            team_id = int(native.get("id") or 0)
            if not team_id:
                rows = []
                break
            reconstructed_row = reconstructed_rows.get(
                (competition_id, team_id),
            ) or {}
            points = int(native.get("points") or 0)
            points_adjustment = adjustments[(competition_id, team_id)]
            rows.append({
                "team_id": team_id,
                "team_name": str(native.get("name") or team_id),
                "team_address": native.get("address"),
                "native_standing_address": native.get("native_standing_address"),
                "played": int(native.get("played") or 0),
                "won": int(native.get("won") or 0),
                "drawn": int(native.get("drawn") or 0),
                "lost": int(native.get("lost") or 0),
                "goals_for": int(native.get("goals_for") or 0),
                "goals_against": int(native.get("goals_against") or 0),
                "base_points": points - points_adjustment,
                "goal_difference": int(native.get("goal_difference") or 0),
                "points_adjustment": points_adjustment,
                "points": points,
                "elo": float(reconstructed_row.get("elo") or ELO_INITIAL_RATING),
                "elo_matches": int(reconstructed_row.get("elo_matches") or 0),
                "position": int(native.get("position") or len(rows) + 1),
            })
        if len(rows) != expected:
            continue
        reconstructed_teams = {
            int(team.get("team_id") or 0): team
            for team in (
                reconstructed_competitions.get(competition_id, {}).get("teams")
                or []
            )
        }
        native_teams = {int(team["team_id"]): team for team in rows}
        if (
            len(reconstructed_teams) == expected
            and reconstructed_teams.keys() == native_teams.keys()
            and all(
                int(reconstructed_teams[team_id].get("played") or 0)
                >= int(native_teams[team_id].get("played") or 0)
                for team_id in native_teams
            )
            and any(
                int(reconstructed_teams[team_id].get("played") or 0)
                > int(native_teams[team_id].get("played") or 0)
                for team_id in native_teams
            )
        ):
            continue
        rows.sort(key=lambda row: (int(row["position"]), int(row["team_id"])))
        competitions.append({
            "competition_id": competition_id,
            "competition_name": str(
                item.get("competition_name") or competition_id
            ),
            "teams": rows,
            "source": "native_league_stage",
        })
    return competitions


def public_league_standings(output: dict[str, Any]) -> dict[str, Any]:
    with _LOCK:
        state = _load_state()
    records = list(state["records"])
    native_write_available = output.get("game_layout") in {"fm24", "fm26"}
    applied_records = (
        [record for record in records if record.get("native_applied")]
        if native_write_available else records
    )
    regular_results = _regular_league_results(output)
    dependency_signature = _standings_dependency_signature(
        output, regular_results, records,
    )
    with _LOCK:
        cached = _PUBLIC_STANDINGS_CACHE.get(dependency_signature)
        if cached is not None:
            _PUBLIC_STANDINGS_CACHE.move_to_end(dependency_signature)
            return deepcopy(cached)
    competitions = build_standings(regular_results, applied_records)
    native_competitions = _native_format_standings(
        output, competitions, applied_records,
    )
    if native_competitions:
        by_competition_id = {
            int(item.get("competition_id") or 0): item for item in competitions
        }
        for native in native_competitions:
            by_competition_id[int(native["competition_id"])] = native
        competitions = list(by_competition_id.values())
    competitions = _apply_same_league_playoff_positions(output, competitions)
    metadata = {
        int(item.get("id") or 0): item for item in output.get("competitions") or []
    }
    for competition in competitions:
        details = metadata.get(int(competition["competition_id"])) or {}
        competition["reputation"] = int(details.get("reputation") or 0)
    competitions.sort(key=lambda item: (
        -int(item.get("reputation") or 0), item["competition_name"].casefold(),
    ))
    result = {
        "source": (
            "native_league_stage_with_reconstructed_fallback"
            if native_competitions else "reconstructed_results"
        ),
        "native_write_available": native_write_available,
        "competitions": competitions,
        "history": list(reversed(records)),
    }
    with _LOCK:
        _PUBLIC_STANDINGS_CACHE[dependency_signature] = deepcopy(result)
        _PUBLIC_STANDINGS_CACHE.move_to_end(dependency_signature)
        while len(_PUBLIC_STANDINGS_CACHE) > _PUBLIC_STANDINGS_CACHE_MAX:
            _PUBLIC_STANDINGS_CACHE.popitem(last=False)
    return result


def add_points_adjustment(output: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    with _LOCK:
        return _add_points_adjustment_locked(output, payload)


def _sync_native_points_snapshot(
    output: dict[str, Any], competition_id: int, team_id: int, points: Any,
) -> None:
    try:
        points_value = int(points)
    except (TypeError, ValueError):
        return
    for competition in output.get("competition_formats") or []:
        if int(competition.get("competition_id") or 0) != competition_id:
            continue
        for team in competition.get("opening_teams") or []:
            if int(team.get("id") or 0) == team_id:
                team["points"] = points_value


def _add_points_adjustment_locked(
    output: dict[str, Any], payload: dict[str, Any],
) -> dict[str, Any]:
    competition_id = int(payload.get("competition_id") or 0)
    team_id = int(payload.get("team_id") or 0)
    delta = int(payload.get("delta") or 0)
    reason = str(payload.get("reason") or "").strip()
    source_id = str(payload.get("source_id") or "").strip()
    if not POINTS_ADJUSTMENT_MIN <= delta <= POINTS_ADJUSTMENT_MAX or delta == 0:
        raise ValueError("积分调整必须是 -100 至 100 之间的非零整数")
    if not reason or len(reason) > 80:
        raise ValueError("请输入不超过80字的调整原因")
    state = _load_state()
    if source_id:
        existing = next((
            row for row in state["records"]
            if str(row.get("source_id") or "") == source_id
        ), None)
        if existing:
            expected = (competition_id, team_id, delta)
            recorded = (
                int(existing.get("competition_id") or 0),
                int(existing.get("team_id") or 0),
                int(existing.get("delta") or 0),
            )
            if recorded != expected:
                raise RuntimeError("积分调整操作键与既有记录冲突")
            return public_league_standings(output)
        legacy_created_after = str(payload.get("legacy_source_created_after") or "")
        legacy_game_date = str(payload.get("legacy_source_game_date") or "")
        reversed_ids = {
            str(row.get("reversal_of") or "") for row in state["records"]
            if row.get("reversal_of")
        }
        legacy_candidates = sorted((
            row for row in state["records"]
            if not row.get("source_id")
            and not row.get("reversal_of")
            and str(row.get("id") or "") not in reversed_ids
            and bool(row.get("native_applied"))
            and int(row.get("competition_id") or 0) == competition_id
            and int(row.get("team_id") or 0) == team_id
            and int(row.get("delta") or 0) == delta
            and str(row.get("reason") or "") == reason
            and (
                not legacy_created_after
                or str(row.get("created_at") or "") >= legacy_created_after
            )
            and (
                not legacy_game_date
                or str(row.get("game_date") or "") >= legacy_game_date
            )
        ), key=lambda row: str(row.get("created_at") or ""))
        if legacy_candidates:
            adopted = legacy_candidates[0]
            adopted["source_id"] = source_id
            adopted["source_adopted_at"] = datetime.now().isoformat(timespec="seconds")
            for duplicate in legacy_candidates[1:]:
                duplicate.setdefault("suspected_duplicate_of", str(adopted.get("id") or ""))
            _save_state(state)
            return public_league_standings(output)
    current = public_league_standings(output)
    competition = next((row for row in current["competitions"] if row["competition_id"] == competition_id), None)
    team = next((row for row in (competition or {}).get("teams", []) if row["team_id"] == team_id), None)
    if not competition or not team:
        raise ValueError("所选联赛或球队不在当前赛季积分榜中")
    if not team.get("team_address"):
        for managed in output.get("managed_teams") or []:
            if int(managed.get("id") or 0) == team_id and managed.get("address"):
                team["team_address"] = managed["address"]
                break
    if not team.get("team_address"):
        for match in output.get("matches") or []:
            side = next((
                match.get(key) for key in ("home", "away")
                if int((match.get(key) or {}).get("id") or 0) == team_id
            ), None)
            if side and side.get("address"):
                team["team_address"] = side["address"]
                break
    native_result = None
    if output.get("game_layout") in {"fm24", "fm26"}:
        from tools.league_table_memory import update_league_points
        native_result = update_league_points(team.get("team_address"), team, delta)
    record = {
        "id": str(uuid.uuid4()), "created_at": datetime.now().isoformat(timespec="seconds"),
        "game_date": str(output.get("game_date") or ""),
        "competition_id": competition_id, "competition_name": competition["competition_name"],
        "team_id": team_id, "team_name": team["team_name"],
        "delta": delta, "reason": reason, "reversal_of": None,
        "native_applied": bool(native_result),
    }
    if source_id:
        record["source_id"] = source_id
    if native_result:
        record["native_before"] = native_result["before"]
        record["native_after"] = native_result["after"]
    with _LOCK:
        try:
            state["records"].append(record)
            _save_state(state)
        except Exception:
            if native_result:
                from tools.league_table_memory import restore_league_points
                restore_league_points(native_result)
            raise
    if native_result:
        _sync_native_points_snapshot(
            output, competition_id, team_id, native_result.get("after"),
        )
    return public_league_standings(output)


def reverse_points_adjustment(output: dict[str, Any], record_id: str) -> dict[str, Any]:
    with _LOCK:
        state = _load_state()
        original = next((row for row in state["records"] if row.get("id") == record_id), None)
        if not original:
            raise ValueError("积分调整记录不存在")
        if original.get("reversal_of"):
            raise ValueError("撤销记录不能再次撤销")
        if any(row.get("reversal_of") == record_id for row in state["records"]):
            raise ValueError("该积分调整已经撤销")
        native_result = None
        if original.get("native_applied"):
            current = public_league_standings(output)
            competition = next((
                row for row in current["competitions"]
                if row["competition_id"] == int(original["competition_id"])
            ), None)
            team = next((
                row for row in (competition or {}).get("teams", [])
                if row["team_id"] == int(original["team_id"])
            ), None)
            if not team:
                raise ValueError("原积分调整对应的球队不在当前积分榜中")
            from tools.league_table_memory import update_league_points
            native_result = update_league_points(
                team.get("team_address"), team, -int(original["delta"]),
            )
        reversal = {
            **{key: original[key] for key in (
                "competition_id", "competition_name", "team_id", "team_name",
            )},
            "id": str(uuid.uuid4()), "created_at": datetime.now().isoformat(timespec="seconds"),
            "game_date": str(output.get("game_date") or ""),
            "delta": -int(original["delta"]), "reason": f"撤销：{original['reason']}",
            "reversal_of": record_id, "native_applied": bool(native_result),
        }
        if native_result:
            reversal["native_before"] = native_result["before"]
            reversal["native_after"] = native_result["after"]
        try:
            state["records"].append(reversal)
            _save_state(state)
        except Exception:
            if native_result:
                from tools.league_table_memory import restore_league_points
                restore_league_points(native_result)
            raise
    return public_league_standings(output)
