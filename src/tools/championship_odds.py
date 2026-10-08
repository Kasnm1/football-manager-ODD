from __future__ import annotations

import hashlib
import json
import math
import random
from datetime import date
from pathlib import Path
from threading import RLock
from typing import Any

from tools.app_paths import cache_data_root, save_data_root
CHAMPIONSHIP_MARGIN = 0.205
CHAMPIONSHIP_SETTLEMENT_MINIMUM_MATCH_MINUTES = 105
CHAMPIONSHIP_SETTLEMENT_EXTENDED_MATCH_MINUTES = 150
CHAMPIONSHIP_SIMULATIONS = 12_000
CHAMPIONSHIP_GENERIC_SIMULATIONS = 1_500
CHAMPIONSHIP_MAX_ODDS = 2001.0
CHAMPIONSHIP_PROBABILITY_PRIOR = 0.25
LEAGUE_PRIOR_SIMULATION_SHARE = 0.40
LEAGUE_PRIOR_UNIFORM_SHARE = 0.01
LEAGUE_PRIOR_STRENGTH_TEMPERATURE = 0.50
LEAGUE_CA_COEFFICIENT = 0.0225
LEAGUE_REPUTATION_LOG_COEFFICIENT = 1.75
LEAGUE_ELO_COEFFICIENT = 0.0025
LEAGUE_REPUTATION_CA_BASE = 60.0
LEAGUE_REPUTATION_CA_SCALE = 0.01
LEAGUE_LIVE_PRIOR_MATCHES = 24.0
LEAGUE_LIVE_MIN_PPG = 0.45
LEAGUE_LIVE_MAX_PPG = 2.30
LEAGUE_LIVE_RATING_LIMIT = 1.25
LEAGUE_LIVE_GOAL_DIFFERENCE_COEFFICIENT = 0.15
CHAMPIONSHIP_LOW_ODDS_LADDER = tuple(value / 2 for value in range(3, 20))
CHAMPIONSHIP_INTEGER_ODDS_LADDER = tuple(float(value) for value in range(10, 41))
CHAMPIONSHIP_MID_ODDS_LADDER = tuple(float(value) for value in range(41, 101, 5))
CHAMPIONSHIP_HIGH_ODDS_LADDER = (
    tuple(float(value) for value in range(101, 502, 50))
    + (1001.0, 1501.0, 2001.0)
)
CHAMPIONSHIP_ODDS_LADDER = (
    CHAMPIONSHIP_LOW_ODDS_LADDER
    + CHAMPIONSHIP_INTEGER_ODDS_LADDER
    + CHAMPIONSHIP_MID_ODDS_LADDER
    + CHAMPIONSHIP_HIGH_ODDS_LADDER
)
CUP_MIN_PRIOR_COUNT = 4.0
CUP_PRIOR_LOG_SLOPE = 0.90
TEAM_REPUTATION_LOG_COEFFICIENT = 5.0
CUP_RATING_SCALE = 0.35
CHAMPIONSHIP_MODEL_VERSION = "championship-v25"
CHAMPIONSHIP_PRICE_CACHE_LIMIT = 256

# First release: famous, single-table competitions with a stable double
# round-robin format. Hybrid league/cup competitions deliberately stay out.
FAMOUS_LEAGUES: dict[int, dict[str, Any]] = {
    10: {"name": "荷甲联赛", "teams": 18, "matches": 34, "calendar": "cross_year"},
    11: {"name": "英超联赛", "teams": 20, "matches": 38, "calendar": "cross_year"},
    16: {"name": "法甲联赛", "teams": 18, "matches": 34, "calendar": "cross_year"},
    22: {"name": "德甲联赛", "teams": 18, "matches": 34, "calendar": "cross_year"},
    32: {"name": "意甲联赛", "teams": 20, "matches": 38, "calendar": "cross_year"},
    67: {"name": "西甲联赛", "teams": 20, "matches": 38, "calendar": "cross_year"},
}
CUP_MODEL_HINTS: dict[int, dict[str, Any]] = {
    1301394: {
        "league_matches": 8,
    },
    1301396: {
        "league_matches": 8,
    },
    31051584: {
        "league_matches": 6,
    },
    1301385: {},
}

_PRICE_CACHE_LOCK = RLock()
_PRICE_CACHE: dict[str, list[dict[str, Any]]] = {}
_CUP_STATE_LOCK = RLock()
_LEAGUE_STATE_LOCK = RLock()


def cup_state_path() -> Path:
    return cache_data_root() / "competitions" / "championship_cups.json"


def league_state_path() -> Path:
    """Return the save-scoped league championship settlement ledger path."""
    return cache_data_root() / "competitions" / "championship_leagues.json"


def _load_league_state() -> dict[str, Any]:
    try:
        payload = json.loads(league_state_path().read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or not isinstance(payload.get("seasons"), dict):
            raise ValueError("invalid league championship state")
        return payload
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError):
        return {"schema_version": 1, "seasons": {}}


def _save_league_state(payload: dict[str, Any]) -> None:
    path = league_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    temporary.replace(path)


def _load_cup_state() -> dict[str, Any]:
    try:
        payload = json.loads(cup_state_path().read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or not isinstance(payload.get("seasons"), dict):
            raise ValueError("invalid cup championship state")
        return payload
    except (OSError, ValueError, json.JSONDecodeError):
        return {"schema_version": 1, "seasons": {}}


def _save_cup_state(payload: dict[str, Any]) -> None:
    path = cup_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def pending_cup_final_result_keys(
    game_date: str,
) -> set[tuple[str, int, int, int]]:
    """Return exact overdue cup-final keys that still need a winner result."""
    try:
        current_date = date.fromisoformat(str(game_date or ""))
        with _CUP_STATE_LOCK:
            state = _load_cup_state()
    except (OSError, RuntimeError, ValueError):
        return set()

    keys: set[tuple[str, int, int, int]] = set()
    for record in state.get("seasons", {}).values():
        if not isinstance(record, dict) or int(record.get("winner_team_id") or 0):
            continue
        final_fixture = record.get("final_fixture") or {}
        try:
            final_date = date.fromisoformat(str(final_fixture.get("date") or ""))
            competition_id = int(record.get("competition_id") or 0)
            home_id = int(final_fixture.get("home_id") or 0)
            away_id = int(final_fixture.get("away_id") or 0)
        except (TypeError, ValueError):
            continue
        if (
            final_date <= current_date
            and competition_id > 0 and home_id > 0 and away_id > 0
        ):
            keys.add((
                final_date.isoformat(), competition_id, home_id, away_id,
            ))
    return keys


def championship_season_window(game_date: str, calendar: str) -> tuple[str, str, str]:
    current = date.fromisoformat(str(game_date))
    if calendar == "calendar_year":
        year = current.year
        return str(year), f"{year}-01-01", f"{year}-12-31"
    start_year = current.year if current.month >= 7 else current.year - 1
    return (
        f"{start_year}/{str(start_year + 1)[-2:]}",
        f"{start_year}-07-01",
        f"{start_year + 1}-06-30",
    )


def _native_season_calendar(
    game_date: str, first_fixture_date: str, competition_kind: str = "",
    season_fixture_bounds_verified: bool = False,
) -> str:
    if competition_kind == "national":
        return "calendar_year"
    if not season_fixture_bounds_verified:
        return "cross_year"
    try:
        current = date.fromisoformat(str(game_date))
        first_fixture = date.fromisoformat(str(first_fixture_date))
    except ValueError:
        return "cross_year"
    return (
        "calendar_year"
        if first_fixture.month <= 6
        else "cross_year"
    )


def _competition_season_calendar(
    output: dict[str, Any], format_row: dict[str, Any], competition_kind: str,
) -> str:
    competition_id = int(format_row.get("competition_id") or 0)
    inferred_starts = output.get("competition_season_starts") or {}
    inferred_start = str(
        inferred_starts.get(competition_id)
        or inferred_starts.get(str(competition_id))
        or ""
    )
    try:
        current = date.fromisoformat(str(output.get("game_date") or ""))
        inferred = date.fromisoformat(inferred_start)
    except ValueError:
        inferred = None
        current = None
    if inferred and current:
        return (
            "calendar_year"
            if inferred.month <= 6
            else "cross_year"
        )
    return _native_season_calendar(
        str(output.get("game_date") or ""),
        str(format_row.get("first_fixture_date") or ""),
        competition_kind,
        bool(format_row.get("season_fixture_bounds_verified")),
    )


def _format_season_window(
    output: dict[str, Any], config: dict[str, Any],
) -> tuple[str, str, str]:
    """Bind a market to the selected format's observed season, not today's label."""
    anchor = str(config.get("format_first_fixture_date") or "")
    try:
        date.fromisoformat(anchor)
    except ValueError:
        anchor = str(output.get("game_date") or "")
    return championship_season_window(anchor, str(config["calendar"]))


def _final_fixture_from_rounds(
    output: dict[str, Any], competition_id: int,
) -> dict[str, Any] | None:
    """Resolve the deciding fixture only from explicit native final identity."""
    candidates = []
    for match in output.get("matches") or []:
        if int(match.get("competition_id") or 0) != competition_id:
            continue
        final = match.get("competition_final")
        if isinstance(final, dict):
            candidates.append(dict(final))
            continue
        round_row = match.get("competition_round") or {}
        if (
            round_row.get("match_role") != "final"
            or int(round_row.get("stage_count") or 0) <= 0
            or int(round_row.get("round_count") or 0) <= 0
            or int(round_row.get("stage_index") or 0) != int(round_row["stage_count"]) - 1
            or int(round_row.get("round_index") or 0) != int(round_row["round_count"]) - 1
            or int(round_row.get("round_tie_count") or 0) != 1
        ):
            continue
        knockout = match.get("knockout") or {}
        deciding = knockout.get("settlement_fixture") or {}
        if int(knockout.get("leg_count") or 1) == 2 and not deciding:
            # A first leg alone is not the deciding final.
            continue
        candidates.append({
            "date": str(deciding.get("date") or match.get("fixture_date") or ""),
            "home_id": int(deciding.get("home_id") or (match.get("home") or {}).get("id") or 0),
            "away_id": int(deciding.get("away_id") or (match.get("away") or {}).get("id") or 0),
            "kickoff_minutes": deciding.get("kickoff_minutes", match.get("kickoff_minutes")),
            "tie_address": int(round_row.get("tie_address") or 0),
            "stage_index": int(round_row.get("stage_index") or 0),
            "round_index": int(round_row.get("round_index") or 0),
            "leg_count": int(knockout.get("leg_count") or 1),
        })
    identities = {
        (item.get("date"), item.get("home_id"), item.get("away_id"), item.get("tie_address"))
        for item in candidates
    }
    return candidates[0] if len(identities) == 1 else None


def _team_sources(output: dict[str, Any], competition_id: int) -> dict[int, dict[str, Any]]:
    teams: dict[int, dict[str, Any]] = {}
    format_row = next(
        (
            item for item in output.get("competition_formats") or []
            if int(item.get("competition_id") or 0) == competition_id
        ),
        None,
    )
    for team in (format_row or {}).get("opening_teams") or []:
        team_id = int(team.get("id") or 0)
        if team_id:
            teams[team_id] = {
                "team_id": team_id,
                "team_name": str(team.get("name") or team_id),
                "reputation": int(team.get("reputation") or 0),
                "profile": dict(team.get("profile") or {}),
            }
    for match in output.get("matches") or []:
        if int(match.get("competition_id") or 0) != competition_id:
            continue
        for side in ("home", "away"):
            team = match.get(side) or {}
            team_id = int(team.get("id") or 0)
            if team_id:
                teams[team_id] = {
                    "team_id": team_id,
                    "team_name": str(team.get("name") or team_id),
                    "reputation": int(team.get("reputation") or 0),
                    "profile": dict(team.get("profile") or {}),
                }
    return teams


def _native_league_team_ids(
    output: dict[str, Any], competition_id: int,
) -> set[int]:
    format_row = next((
        item for item in output.get("competition_formats") or []
        if int(item.get("competition_id") or 0) == competition_id
    ), None)
    if (
        not format_row
        or str(format_row.get("competition_kind") or "") != "league"
        or str(format_row.get("opening_stage_type") or "") != "league"
        or not bool(format_row.get("opening_field_complete"))
        or bool(format_row.get("has_terminal_cup_stage"))
    ):
        return set()
    expected = int(format_row.get("opening_slot_count") or 0)
    team_ids = {
        int(team.get("id") or 0)
        for team in format_row.get("opening_teams") or []
        if int(team.get("id") or 0) > 0
    }
    return team_ids if expected >= 2 and len(team_ids) == expected else set()


def _reconciled_league_team_ids(
    native_team_ids: set[int], table: dict[int, dict[str, Any]],
    evidence_team_ids: set[int], expected_teams: int,
) -> tuple[set[int], str]:
    """Repair a small native field mismatch only with current-season evidence."""
    table_team_ids = {int(team_id) for team_id in table if int(team_id) > 0}
    observed_team_ids = table_team_ids | {
        int(team_id) for team_id in evidence_team_ids if int(team_id) > 0
    }
    candidate_team_ids = (
        table_team_ids
        if len(table_team_ids) == expected_teams else observed_team_ids
    )
    if len(native_team_ids) != expected_teams:
        if len(candidate_team_ids) == expected_teams:
            return candidate_team_ids, "complete_current_evidence"
        return set(native_team_ids), "native_incomplete"
    if len(candidate_team_ids) != expected_teams or candidate_team_ids == native_team_ids:
        return set(native_team_ids), "native"

    table_only = candidate_team_ids - native_team_ids
    native_only = native_team_ids - candidate_team_ids
    mismatch_limit = max(1, min(2, round(expected_teams * 0.10)))
    if (
        len(table_only) != len(native_only)
        or len(table_only) > mismatch_limit
        or not table_only <= evidence_team_ids
        or bool(native_only & evidence_team_ids)
    ):
        return set(native_team_ids), "native_conflict_unverified"
    return candidate_team_ids, "current_season_table_repair"


def _final_winner_prices(
    output: dict[str, Any], competition_id: int, finalist_ids: set[int],
) -> dict[int, dict[str, float]]:
    if len(finalist_ids) != 2:
        return {}
    for match in output.get("matches") or []:
        if int(match.get("competition_id") or 0) != competition_id:
            continue
        final = match.get("competition_final") or {}
        match_ids = {
            int(final.get("home_id") or (match.get("home") or {}).get("id") or 0),
            int(final.get("away_id") or (match.get("away") or {}).get("id") or 0),
        }
        knockout = match.get("knockout") or {}
        advance = knockout.get("advance") or {}
        if match_ids != finalist_ids or not isinstance(advance, dict):
            continue
        try:
            odds = {team_id: float(advance[str(team_id)]) for team_id in finalist_ids}
        except (KeyError, TypeError, ValueError):
            continue
        if any(not math.isfinite(value) or value < 1.01 for value in odds.values()):
            continue
        fair_advance = knockout.get("fair_advance") or {}
        probabilities: dict[int, float] = {}
        for team_id in finalist_ids:
            try:
                fair_odds = float(fair_advance[str(team_id)])
            except (KeyError, TypeError, ValueError):
                fair_odds = 0.0
            if math.isfinite(fair_odds) and fair_odds >= 1.0:
                probabilities[team_id] = 1.0 / fair_odds
        if len(probabilities) != 2:
            implied = {team_id: 1.0 / value for team_id, value in odds.items()}
            total = sum(implied.values())
            probabilities = {team_id: value / total for team_id, value in implied.items()}
        return {
            team_id: {
                "odds": round(odds[team_id], 2),
                "probability": probabilities[team_id],
            }
            for team_id in finalist_ids
        }
    return {}


def _discovered_cup_configs(output: dict[str, Any]) -> dict[int, dict[str, Any]]:
    # Keep qualification tournaments out of the championship market.  They
    # can expose a native group stage, but there is no single competition
    # champion to price or settle.
    from tools.preview_cup_odds import is_national_qualification_competition

    configs: dict[int, dict[str, Any]] = {}
    catalog = {
        int(item.get("id") or 0): item
        for item in output.get("competitions") or []
    }
    for item in output.get("competition_formats") or []:
        competition_id = int(item.get("competition_id") or 0)
        expected = int(item.get("opening_slot_count") or 0)
        qualifiers = int(item.get("knockout_slot_count") or expected)
        round_counts = [
            int(value) for value in item.get("knockout_round_tie_counts") or []
            if int(value) >= 0
        ]
        first_round_ties = next((value for value in round_counts if value > 0), 0)
        final_round_ties = next((value for value in reversed(round_counts) if value > 0), 0)
        has_terminal_stage = bool(item.get("has_terminal_cup_stage"))
        observed_name = str(
            item.get("competition_name")
            or (catalog.get(competition_id) or {}).get("name")
            or ""
        )
        provisional_group_stage = bool(
            not has_terminal_stage
            and str(item.get("opening_stage_type") or "") == "group"
            and expected >= 16
        )
        if provisional_group_stage:
            qualifiers = 1 << (expected.bit_length() - 1)
        calendar = _competition_season_calendar(
            output,
            item,
            str(item.get("competition_kind") or ""),
        )
        _season_label, current_season_start, _current_season_end = (
            championship_season_window(str(output.get("game_date") or ""), calendar)
        )
        format_first_fixture = str(item.get("first_fixture_date") or "")
        format_last_observed = str(item.get("format_last_observed_at") or "")
        stale_persisted_format = bool(
            str(item.get("competition_season_address") or "")
            and format_first_fixture and format_first_fixture < current_season_start
            and format_last_observed and format_last_observed < current_season_start
        )
        if (
            not competition_id or competition_id in FAMOUS_LEAGUES
            or str(item.get("competition_kind") or "") not in {"cup", "national"}
            or is_national_qualification_competition(
                observed_name, competition_id,
            )
            or not (has_terminal_stage or provisional_group_stage)
            or not item.get("opening_field_complete")
            or expected < 2 or qualifiers < 2 or qualifiers > expected
            or has_terminal_stage and final_round_ties != 1
            or stale_persisted_format
        ):
            continue
        byes = (
            max(0, qualifiers - first_round_ties * 2)
            if first_round_ties else 0
        )
        hints = CUP_MODEL_HINTS.get(competition_id) or {}
        configs[competition_id] = {
            "name": str(item.get("competition_name") or competition_id),
            "calendar": calendar,
            "participants": expected,
            "qualifiers": qualifiers,
            "byes": min(byes, qualifiers),
            "competition_season_address": str(
                item.get("competition_season_address") or ""
            ),
            "format_last_observed_at": format_last_observed,
            "format_first_fixture_date": format_first_fixture,
            "format_last_fixture_date": str(item.get("last_fixture_date") or ""),
            "season_fixture_bounds_verified": bool(
                item.get("season_fixture_bounds_verified")
            ),
            "league_matches": int(hints.get("league_matches") or 0) or None,
            "first_fixture_date": str(item.get("first_fixture_date") or ""),
            "opening_stage_type": str(item.get("opening_stage_type") or ""),
            "source": "native_competition_format",
            "provisional_group_stage": provisional_group_stage,
            "terminal_final_fixture": dict(item.get("terminal_final_fixture") or {}),
            "competition_reputation": int(
                (catalog.get(competition_id) or {}).get("reputation") or 0
            ),
        }
    return configs


def _discovered_league_configs(
    output: dict[str, Any], standings: dict[str, Any],
) -> dict[int, dict[str, Any]]:
    from tools.preview_cup_odds import is_national_qualification_competition

    catalog = {
        int(item.get("id") or 0): item
        for item in output.get("competitions") or []
    }
    standing_rows = {
        int(item.get("competition_id") or 0): item
        for item in standings.get("competitions") or []
        if int(item.get("competition_id") or 0) > 0
    }
    format_rows = {
        int(item.get("competition_id") or 0): item
        for item in output.get("competition_formats") or []
        if int(item.get("competition_id") or 0) > 0
        and str(item.get("competition_kind") or "") == "league"
    }
    configs: dict[int, dict[str, Any]] = {}
    for competition_id in sorted(set(standing_rows) | set(format_rows)):
        competition = standing_rows.get(competition_id) or {}
        teams = competition.get("teams") or []
        metadata = catalog.get(competition_id) or {}
        format_row = format_rows.get(competition_id) or {}
        native_team_ids = _native_league_team_ids(output, competition_id)
        table_team_ids = {
            int(team.get("team_id") or 0) for team in teams
            if int(team.get("team_id") or 0) > 0
        }
        team_count = len(native_team_ids or table_team_ids)
        expected_matches = 2 * (team_count - 1)
        maximum_played = max((int(team.get("played") or 0) for team in teams), default=0)
        observed_name = str(
            competition.get("competition_name")
            or metadata.get("name")
            or format_row.get("competition_name")
            or ""
        )
        if (
            not competition_id
            or str(metadata.get("kind") or format_row.get("competition_kind") or ("league" if competition_id in FAMOUS_LEAGUES else "")) != "league"
            or is_national_qualification_competition(observed_name, competition_id)
            or bool(format_row.get("has_terminal_cup_stage"))
            or team_count < 6 or team_count > 40
            or competition_id in FAMOUS_LEAGUES and not native_team_ids
        ):
            continue
        format_last_fixture_date = str(format_row.get("last_fixture_date") or "")
        terminal_date_verified = bool(
            format_last_fixture_date
            and (
                bool(format_row.get("season_fixture_bounds_verified"))
            )
        )
        configs[competition_id] = {
            "name": str(competition.get("competition_name") or metadata.get("name") or competition_id),
            "teams": team_count,
            "matches": expected_matches,
            "calendar": (
                str(FAMOUS_LEAGUES[competition_id]["calendar"])
                if competition_id in FAMOUS_LEAGUES and not format_row.get("season_fixture_bounds_verified")
                else _competition_season_calendar(output, format_row, str(metadata.get("kind") or ""))
            ),
            "competition_reputation": int(metadata.get("reputation") or 1),
            "format_first_fixture_date": str(format_row.get("first_fixture_date") or ""),
            "competition_season_address": str(format_row.get("competition_season_address") or ""),
            "actual_competition_address": str(format_row.get("actual_competition_address") or ""),
            "format_last_fixture_date": (
                format_last_fixture_date if terminal_date_verified else ""
            ),
            "settlement_requires_terminal": terminal_date_verified,
            "observed_nonstandard_schedule": maximum_played > expected_matches,
            "source": (
                "native_league_format"
                if native_team_ids else "native_league_standings"
            ),
        }
    return configs


def _championship_discovery_diagnostics(
    output: dict[str, Any],
    league_configs: dict[int, dict[str, Any]],
    cup_configs: dict[int, dict[str, Any]],
    competitions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    from tools.preview_cup_odds import is_national_qualification_competition

    published_ids = {
        int(item.get("competition_id") or 0) for item in competitions
    }
    diagnostics = []
    for item in output.get("competition_formats") or []:
        competition_id = int(item.get("competition_id") or 0)
        kind = str(item.get("competition_kind") or "")
        if not competition_id or kind not in {"league", "cup", "national"}:
            continue
        if competition_id in published_ids:
            continue
        if competition_id in league_configs or competition_id in cup_configs:
            reason = "market_inputs_incomplete"
        elif is_national_qualification_competition(
            str(
                item.get("competition_name")
                or next(
                    (
                        competition.get("name")
                        for competition in output.get("competitions") or []
                        if int(competition.get("id") or 0) == competition_id
                    ),
                    "",
                )
                or ""
            ),
            competition_id,
        ):
            reason = "qualification_competition_excluded"
        elif kind == "league" and bool(item.get("has_terminal_cup_stage")):
            reason = "hybrid_league_unsupported"
        elif not bool(item.get("opening_field_complete")):
            reason = "opening_field_incomplete"
        elif kind == "league" and str(item.get("opening_stage_type") or "") != "league":
            reason = "single_table_stage_unavailable"
        elif kind in {"cup", "national"} and not bool(item.get("has_terminal_cup_stage")):
            reason = "terminal_stage_unavailable"
        else:
            reason = "format_rejected"
        diagnostics.append({
            "competition_id": competition_id,
            "competition_name": str(item.get("competition_name") or competition_id),
            "competition_kind": kind,
            "competition_season_address": str(
                item.get("competition_season_address") or ""
            ),
            "opening_stage_type": str(item.get("opening_stage_type") or ""),
            "opening_slot_count": int(item.get("opening_slot_count") or 0),
            "opening_field_complete": bool(item.get("opening_field_complete")),
            "has_terminal_cup_stage": bool(item.get("has_terminal_cup_stage")),
            "reason": reason,
        })
    diagnostics.sort(key=lambda row: (
        str(row["competition_kind"]), str(row["competition_name"]).casefold(),
        int(row["competition_id"]),
    ))
    return diagnostics


def _table_map(standings: dict[str, Any], competition_id: int) -> tuple[str, dict[int, dict[str, Any]]]:
    competition = next(
        (
            item for item in standings.get("competitions") or []
            if int(item.get("competition_id") or 0) == competition_id
        ),
        None,
    )
    if not competition:
        return "", {}
    return str(competition.get("competition_name") or ""), {
        int(row.get("team_id") or 0): dict(row)
        for row in competition.get("teams") or []
        if int(row.get("team_id") or 0)
    }


def _profile_ca(team: dict[str, Any]) -> float:
    profile = team.get("profile") or {}
    return float(
        profile.get("weighted_raw_ca")
        or profile.get("candidate_20_ca")
        or 0.0
    )


def _profile_is_usable(team: dict[str, Any]) -> bool:
    profile = team.get("profile") or {}
    return (
        int(profile.get("candidate_players") or 0) >= 11
        and 20 <= _profile_ca(team) <= 200
        and 1 <= int(team.get("reputation") or 0) <= 10_000
    )


def _market_version(
    competition_id: int,
    season_key: str,
    game_date: str,
    teams: list[dict[str, Any]],
) -> str:
    payload = {
        "model_version": CHAMPIONSHIP_MODEL_VERSION,
        "competition_id": competition_id,
        "season_key": season_key,
        "game_date": game_date,
        "teams": [
            (
                int(team["team_id"]), int(team.get("played") or 0),
                int(team.get("points") or 0), round(float(team.get("elo") or 1500), 1),
                round(_profile_ca(team), 2),
                int(team.get("reputation") or 0),
            )
            for team in sorted(teams, key=lambda item: int(item["team_id"]))
        ],
    }
    encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()[:20]


def _display_championship_odds(raw_odds: float) -> float:
    clipped = min(CHAMPIONSHIP_MAX_ODDS, max(1.01, float(raw_odds)))
    if clipped > 501.0:
        if clipped <= 880.0:
            return 501.0
        if clipped <= 1250.0:
            return 1001.0
        if clipped <= 1750.0:
            return 1501.0
        return 2001.0
    return min(
        CHAMPIONSHIP_ODDS_LADDER,
        key=lambda value: (abs(value - clipped), value),
    )


def _spread_league_longshots(priced: list[dict[str, Any]]) -> None:
    """Compress a crowded league tail into sparse, strength-ordered quotes."""
    longshots = [team for team in priced if float(team.get("odds") or 0) >= 501.0]
    if not longshots:
        return
    exceptional = any(float(team.get("odds") or 0) >= 1001.0 for team in longshots)
    ordered = sorted(
        longshots,
        key=lambda item: (
            -float(item.get("champion_probability") or 0),
            -float(item.get("strength_rating") or 0),
            -int(item.get("points") or 0),
            int(item["team_id"]),
        ),
    )
    ladder = [301.0, 351.0, 401.0, 451.0, 501.0]
    if exceptional:
        ladder.append(1001.0)
    start = max(0, len(ladder) - len(ordered))
    tiers = ([ladder[0]] * max(0, len(ordered) - len(ladder))) + ladder[start:]
    for team, tier in zip(ordered, tiers):
        team["odds"] = tier


def _spread_cup_longshots(priced: list[dict[str, Any]]) -> None:
    """Keep large cup tails sparse without inventing high-odds teams."""
    if len(priced) < 24:
        return
    longshots = [team for team in priced if float(team.get("odds") or 0) >= 501.0]
    if not longshots:
        return

    tail_limit = max(4, round(len(priced) * 0.22))
    capacities = _weighted_tail_tier_counts(tail_limit)
    tiers = (501.0, 1001.0, 1501.0, 2001.0)
    overflow: list[dict[str, Any]] = []
    ordered = sorted(
        longshots,
        key=lambda item: (
            float(item.get("strength_rating") or 0),
            float(item.get("champion_probability") or 0),
            int(item["team_id"]),
        ),
    )
    for team in ordered:
        raw_odds = float(team.get("odds") or 0)
        desired_index = next(
            index for index in range(len(tiers) - 1, -1, -1)
            if raw_odds >= tiers[index]
        )
        available_index = next(
            (
                index for index in range(desired_index, -1, -1)
                if capacities[index] > 0
            ),
            None,
        )
        if available_index is None:
            overflow.append(team)
            continue
        capacities[available_index] -= 1
        team["odds"] = tiers[available_index]

    low_ladder = [301.0, 351.0, 401.0, 451.0]
    overflow.sort(key=lambda item: (
        -float(item.get("champion_probability") or 0),
        -float(item.get("strength_rating") or 0),
        int(item["team_id"]),
    ))
    start = max(0, len(low_ladder) - len(overflow))
    low_tiers = (
        [low_ladder[0]] * max(0, len(overflow) - len(low_ladder))
        + low_ladder[start:]
    )
    for team, tier in zip(overflow, low_tiers):
        team["odds"] = tier


def _ratings(teams: list[dict[str, Any]]) -> dict[int, float]:
    cas = [_profile_ca(team) for team in teams]
    reputations = [math.log(max(1, int(team["reputation"]))) for team in teams]
    elos = [float(team.get("elo") or 1500.0) for team in teams]
    mean_ca = sum(cas) / len(cas)
    mean_reputation = sum(reputations) / len(reputations)
    mean_elo = sum(elos) / len(elos)
    return {
        int(team["team_id"]): (
            (_profile_ca(team) - mean_ca) * 0.055
            + (
                math.log(max(1, int(team["reputation"]))) - mean_reputation
            ) * TEAM_REPUTATION_LOG_COEFFICIENT
            + (float(team.get("elo") or 1500.0) - mean_elo) * 0.0025
        )
        for team in teams
    }


def _league_ratings(teams: list[dict[str, Any]]) -> dict[int, float]:
    adjusted_cas = [
        max(
            _profile_ca(team),
            min(
                175.0,
                LEAGUE_REPUTATION_CA_BASE
                + int(team["reputation"]) * LEAGUE_REPUTATION_CA_SCALE,
            ),
        )
        for team in teams
    ]
    reputations = [math.log(max(1, int(team["reputation"]))) for team in teams]
    elos = [float(team.get("elo") or 1500.0) for team in teams]
    mean_ca = sum(adjusted_cas) / len(adjusted_cas)
    mean_reputation = sum(reputations) / len(reputations)
    mean_elo = sum(elos) / len(elos)
    return {
        int(team["team_id"]): (
            (adjusted_cas[index] - mean_ca) * LEAGUE_CA_COEFFICIENT
            + (reputations[index] - mean_reputation)
            * LEAGUE_REPUTATION_LOG_COEFFICIENT
            + (elos[index] - mean_elo) * LEAGUE_ELO_COEFFICIENT
        )
        for index, team in enumerate(teams)
    }


def _live_league_ratings(
    teams: list[dict[str, Any]], preseason_ratings: dict[int, float],
) -> dict[int, float]:
    observed: dict[int, tuple[float, int]] = {}
    for team in teams:
        team_id = int(team["team_id"])
        played = max(0, int(team.get("played") or 0))
        if played <= 0:
            continue
        points_per_game = float(team.get("points") or 0) / played
        bounded_ppg = min(
            LEAGUE_LIVE_MAX_PPG,
            max(LEAGUE_LIVE_MIN_PPG, points_per_game),
        )
        decisive_win_share = (
            (bounded_ppg - 0.25) / (3.0 * (1.0 - 0.25))
        )
        performance_rating = math.log(
            decisive_win_share / (1.0 - decisive_win_share)
        )
        goal_difference_per_game = (
            float(team.get("goal_difference") or 0) / played
        )
        performance_rating += min(
            0.5,
            max(
                -0.5,
                goal_difference_per_game
                * LEAGUE_LIVE_GOAL_DIFFERENCE_COEFFICIENT,
            ),
        )
        observed[team_id] = (performance_rating, played)
    if not observed:
        return dict(preseason_ratings)

    observed_weight = sum(played for _rating, played in observed.values())
    observed_mean = sum(
        rating * played for rating, played in observed.values()
    ) / max(1, observed_weight)
    ratings = dict(preseason_ratings)
    for team_id, (performance_rating, played) in observed.items():
        live_rating = min(
            LEAGUE_LIVE_RATING_LIMIT,
            max(
                -LEAGUE_LIVE_RATING_LIMIT,
                performance_rating - observed_mean,
            ),
        )
        live_weight = played / (played + LEAGUE_LIVE_PRIOR_MATCHES)
        ratings[team_id] = (
            preseason_ratings[team_id] * (1.0 - live_weight)
            + live_rating * live_weight
        )
    return ratings


def _expected_points(rating: float, opponents: list[float]) -> tuple[float, float]:
    draw_probability = 0.25
    outcome_values: list[tuple[float, float]] = []
    for opponent in opponents:
        for home_edge in (0.16, -0.16):
            decisive_win = 1.0 / (1.0 + math.exp(-(rating - opponent + home_edge)))
            win = (1.0 - draw_probability) * decisive_win
            loss = (1.0 - draw_probability) - win
            mean = win * 3.0 + draw_probability
            variance = win * 9.0 + draw_probability - mean * mean
            outcome_values.append((mean, max(0.05, variance)))
    return (
        sum(value[0] for value in outcome_values) / len(outcome_values),
        sum(value[1] for value in outcome_values) / len(outcome_values),
    )


def _simulate_prices(
    teams: list[dict[str, Any]], expected_matches: int, seed: str,
    eligible_team_ids: set[int] | None = None,
    simulations: int = CHAMPIONSHIP_SIMULATIONS,
) -> list[dict[str, Any]]:
    ratings = _live_league_ratings(teams, _league_ratings(teams))
    eligible = set(eligible_team_ids or ratings)
    projections: dict[int, tuple[float, float]] = {}
    rating_values = list(ratings.values())
    for team in teams:
        team_id = int(team["team_id"])
        opponents = [value for other_id, value in ratings.items() if other_id != team_id]
        projections[team_id] = _expected_points(ratings[team_id], opponents or rating_values)

    rng = random.Random(seed)
    champions = {int(team["team_id"]): 0 for team in teams}
    for _index in range(simulations):
        scores = []
        for team in teams:
            team_id = int(team["team_id"])
            remaining = max(0, expected_matches - int(team.get("played") or 0))
            mean, variance = projections[team_id]
            future_points = min(
                3.0 * remaining,
                max(0.0, rng.gauss(remaining * mean, math.sqrt(remaining * variance))),
            )
            projected = (
                float(team.get("points") or 0)
                + future_points
                + rng.random() * 0.001
            )
            if team_id in eligible:
                scores.append((projected, ratings[team_id], team_id))
        champions[max(scores)[2]] += 1

    average_remaining_fraction = sum(
        max(0, expected_matches - int(team.get("played") or 0))
        / max(1, expected_matches)
        for team in teams if int(team["team_id"]) in eligible
    ) / max(1, len(eligible))
    maximum_rating = max(ratings[team_id] for team_id in eligible)
    strength_weights = {
        team_id: math.exp(
            (ratings[team_id] - maximum_rating)
            / LEAGUE_PRIOR_STRENGTH_TEMPERATURE
        )
        for team_id in eligible
    }
    strength_total = sum(strength_weights.values())
    prior_total = max(
        CHAMPIONSHIP_PROBABILITY_PRIOR * len(eligible),
        simulations * LEAGUE_PRIOR_SIMULATION_SHARE * average_remaining_fraction,
    )
    prior_counts = {
        team_id: prior_total * (
            LEAGUE_PRIOR_UNIFORM_SHARE / len(eligible)
            + (1.0 - LEAGUE_PRIOR_UNIFORM_SHARE)
            * strength_weights[team_id] / strength_total
        )
        for team_id in eligible
    }
    denominator = simulations + sum(prior_counts.values())
    priced = []
    for team in teams:
        team_id = int(team["team_id"])
        if team_id not in eligible:
            continue
        probability = (
            champions[team_id] + prior_counts[team_id]
        ) / denominator
        odds = _display_championship_odds(
            1.0 / (probability * (1.0 + CHAMPIONSHIP_MARGIN)),
        )
        priced.append({
            **team,
            "champion_probability": round(probability, 6),
            "odds": odds,
            "strength_rating": round(ratings[team_id], 4),
        })
    _spread_league_longshots(priced)
    priced.sort(key=lambda item: (float(item["odds"]), str(item["team_name"]).casefold()))
    return priced


def _cached_prices(
    teams: list[dict[str, Any]], expected_matches: int, market_version: str, seed: str,
    eligible_team_ids: set[int] | None = None,
    simulations: int = CHAMPIONSHIP_SIMULATIONS,
) -> list[dict[str, Any]]:
    with _PRICE_CACHE_LOCK:
        cached = _PRICE_CACHE.get(market_version)
        if cached is not None:
            return [dict(item) for item in cached]
    prices = _simulate_prices(
        teams, expected_matches, seed, eligible_team_ids=eligible_team_ids,
        simulations=simulations,
    )
    with _PRICE_CACHE_LOCK:
        if len(_PRICE_CACHE) >= CHAMPIONSHIP_PRICE_CACHE_LIMIT:
            _PRICE_CACHE.pop(next(iter(_PRICE_CACHE)))
        _PRICE_CACHE[market_version] = [dict(item) for item in prices]
    return prices


def _simulate_cup_prices(
    teams: list[dict[str, Any]], qualifiers: int, byes: int, seed: str,
    league_matches: int | None = None,
) -> list[dict[str, Any]]:
    qualifiers = min(max(0, int(qualifiers)), len(teams))
    byes = min(max(0, int(byes)), qualifiers)
    first_round_size = byes + (qualifiers - byes) // 2
    if (
        not teams or qualifiers <= 0
        or (qualifiers - byes) % 2
        or first_round_size <= 0
        or first_round_size & (first_round_size - 1)
    ):
        return []
    ratings = {
        team_id: rating * CUP_RATING_SCALE
        for team_id, rating in _ratings(teams).items()
    }
    team_map = {int(team["team_id"]): team for team in teams}
    rng = random.Random(seed)
    champions = {int(team["team_id"]): 0 for team in teams}

    def play(left: int, right: int) -> int:
        probability = 1.0 / (1.0 + math.exp(-(ratings[left] - ratings[right])))
        return left if rng.random() < probability else right

    for _index in range(CHAMPIONSHIP_SIMULATIONS):
        def qualification_score(team_id: int) -> float:
            if not league_matches:
                return ratings[team_id] + rng.gauss(0.0, 1.05)
            team = team_map[team_id]
            remaining = max(0, league_matches - int(team.get("played") or 0))
            expected_points = 1.5 + 0.75 * math.tanh(ratings[team_id])
            return (
                float(team.get("points") or 0)
                + remaining * expected_points
                + rng.gauss(0.0, math.sqrt(max(1, remaining)) * 1.35)
            )
        stage = sorted(
            ratings,
            key=qualification_score,
            reverse=True,
        )[:qualifiers]
        automatic = stage[:byes]
        playoff = stage[byes:]
        rng.shuffle(playoff)
        qualified = automatic + [
            play(playoff[index], playoff[index + 1])
            for index in range(0, len(playoff), 2)
        ]
        while len(qualified) > 1:
            rng.shuffle(qualified)
            qualified = [
                play(qualified[index], qualified[index + 1])
                for index in range(0, len(qualified), 2)
            ]
        champions[qualified[0]] += 1

    minimum_rating = min(ratings.values())
    prior_counts = {
        team_id: CUP_MIN_PRIOR_COUNT * math.exp(
            CUP_PRIOR_LOG_SLOPE * (rating - minimum_rating)
        )
        for team_id, rating in ratings.items()
    }
    denominator = CHAMPIONSHIP_SIMULATIONS + sum(prior_counts.values())
    priced = []
    for team in teams:
        team_id = int(team["team_id"])
        probability = (champions[team_id] + prior_counts[team_id]) / denominator
        odds = _display_championship_odds(
            1.0 / (probability * (1.0 + CHAMPIONSHIP_MARGIN)),
        )
        priced.append({
            **team,
            "champion_probability": round(probability, 6),
            "odds": odds,
            "strength_rating": round(ratings[team_id], 4),
        })
    _spread_cup_longshots(priced)
    priced.sort(key=lambda item: (float(item["odds"]), str(item["team_name"]).casefold()))
    return priced


def _cached_cup_prices(
    teams: list[dict[str, Any]], qualifiers: int, byes: int,
    market_version: str, seed: str, league_matches: int | None = None,
) -> list[dict[str, Any]]:
    cache_key = f"cup:{market_version}"
    with _PRICE_CACHE_LOCK:
        cached = _PRICE_CACHE.get(cache_key)
        if cached is not None:
            return [dict(item) for item in cached]
    prices = _simulate_cup_prices(
        teams, qualifiers, byes, seed, league_matches=league_matches,
    )
    with _PRICE_CACHE_LOCK:
        if len(_PRICE_CACHE) >= CHAMPIONSHIP_PRICE_CACHE_LIMIT:
            _PRICE_CACHE.pop(next(iter(_PRICE_CACHE)))
        _PRICE_CACHE[cache_key] = [dict(item) for item in prices]
    return prices


def _weighted_tail_tier_counts(total: int) -> list[int]:
    """Split any tail size across the 4:3:2:1 high-odds tiers."""
    total = max(0, int(total))
    base, remainder = divmod(total, 10)
    counts = [4 * base, 3 * base, 2 * base, base]
    for index in range(remainder):
        counts[index % len(counts)] += 1
    return counts


def _result_rows(output: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    save_id = str(output.get("save_instance_id") or "")
    if save_id:
        try:
            from tools.preview_cup_odds import read_result_history

            rows.extend(read_result_history(save_id))
        except (OSError, RuntimeError, ValueError):
            pass
    rows.extend(output.get("season_results") or [])
    rows.extend(output.get("settlement_results") or [])
    merged = {}
    for row in rows:
        try:
            merged[_result_identity(row)] = row
        except (KeyError, TypeError, ValueError):
            continue
    game_date = str(output.get("game_date") or "")
    confirmed_same_day = {
        _result_identity(row) for row in output.get("season_results") or []
    }
    confirmed_same_day.update(
        _result_identity(row) for row in output.get("settlement_results") or []
    )
    try:
        hours, minutes = (
            int(value) for value in str(output.get("game_time") or "").split(":", 1)
        )
        game_minutes = hours * 60 + minutes
    except ValueError:
        game_minutes = -1
    if game_minutes >= 0:
        confirmed_same_day.update(
            (
                game_date,
                int(match.get("competition_id") or 0),
                int((match.get("home") or {}).get("id") or 0),
                int((match.get("away") or {}).get("id") or 0),
            )
            for match in output.get("matches") or []
            if str(match.get("fixture_date") or "") == game_date
            and match.get("kickoff_minutes") is not None
            and game_minutes >= int(match["kickoff_minutes"])
            + CHAMPIONSHIP_SETTLEMENT_MINIMUM_MATCH_MINUTES
        )
    return [
        row for identity, row in merged.items()
        if identity[0] < game_date
        or identity[0] == game_date and identity in confirmed_same_day
    ]


def _result_identity(row: dict[str, Any]) -> tuple[str, int, int, int]:
    competition = row.get("competition") or {}
    home = row.get("home") or row.get("home_team") or {}
    away = row.get("away") or row.get("away_team") or {}
    return (
        str(row.get("date") or ""),
        int(row.get("competition_id") or competition.get("id") or 0),
        int(home.get("id") or 0), int(away.get("id") or 0),
    )


def _result_winner_id(row: dict[str, Any]) -> int | None:
    advanced = row.get("advanced_team") or {}
    if int(row.get("advanced_team_id") or advanced.get("id") or 0):
        return int(row.get("advanced_team_id") or advanced.get("id"))
    side = str(row.get("winner_side") or row.get("advanced_side") or "")
    team = row.get(side) or row.get(f"{side}_team") or {}
    winner_id = int(team.get("id") or 0)
    return winner_id or None


def _final_result_winner_id(
    row: dict[str, Any], final_fixture: dict[str, Any] | None = None,
) -> int | None:
    _date, _competition_id, home_id, away_id = _result_identity(row)
    advanced = row.get("advanced_team") or {}
    advanced_id = int(row.get("advanced_team_id") or advanced.get("id") or 0)
    if advanced_id:
        return advanced_id if advanced_id in {home_id, away_id} else None
    advanced_side = str(row.get("advanced_side") or "")
    if advanced_side in {"home", "away"}:
        team = row.get(advanced_side) or row.get(f"{advanced_side}_team") or {}
        if int(team.get("id") or 0):
            return int(team["id"])
    try:
        home_outcome = int(row["home_outcome_code"])
        away_outcome = int(row["away_outcome_code"])
    except (KeyError, TypeError, ValueError):
        home_outcome = away_outcome = -1
    if away_outcome == 10 and home_outcome != 10:
        return home_id or None
    if home_outcome == 10 and away_outcome != 10:
        return away_id or None
    if int((final_fixture or {}).get("leg_count") or 1) == 2:
        return None
    winner_id = _result_winner_id(row)
    if winner_id:
        return winner_id
    for home_field, away_field in (
        ("penalty_shootout_home_goals", "penalty_shootout_away_goals"),
        ("after_extra_time_home_goals", "after_extra_time_away_goals"),
    ):
        try:
            home_score = int(row[home_field])
            away_score = int(row[away_field])
        except (KeyError, TypeError, ValueError):
            continue
        if home_score != away_score:
            return home_id if home_score > away_score else away_id
    try:
        home_goals = int(row["home_goals"])
        away_goals = int(row["away_goals"])
    except (KeyError, TypeError, ValueError):
        return None
    if home_goals == away_goals:
        return None
    return home_id if home_goals > away_goals else away_id


def _confirmed_league_table(
    table: dict[int, dict[str, Any]], results: list[dict[str, Any]],
    competition_id: int, season_start: str, season_end: str,
) -> dict[int, dict[str, Any]]:
    if not table:
        return table
    stats = {
        team_id: {"played": 0, "points": 0, "goal_difference": 0, "goals_for": 0}
        for team_id in table
    }
    for row in results:
        result_date, result_competition, home_id, away_id = _result_identity(row)
        if (
            result_competition != competition_id
            or not season_start <= result_date <= season_end
            or home_id not in stats or away_id not in stats
        ):
            continue
        home_goals = int(row.get("home_goals") or 0)
        away_goals = int(row.get("away_goals") or 0)
        stats[home_id]["played"] += 1
        stats[away_id]["played"] += 1
        stats[home_id]["goals_for"] += home_goals
        stats[away_id]["goals_for"] += away_goals
        stats[home_id]["goal_difference"] += home_goals - away_goals
        stats[away_id]["goal_difference"] += away_goals - home_goals
        if home_goals > away_goals:
            stats[home_id]["points"] += 3
        elif away_goals > home_goals:
            stats[away_id]["points"] += 3
        else:
            stats[home_id]["points"] += 1
            stats[away_id]["points"] += 1
    deltas = [
        int(row.get("played") or 0) - stats[team_id]["played"]
        for team_id, row in table.items()
    ]
    if not deltas or not any(deltas):
        return table
    table_ahead = min(deltas) >= 0 and max(deltas) <= 2
    results_ahead = max(deltas) <= 0 and min(deltas) >= -2
    if not table_ahead and not results_ahead:
        return table
    rebuilt = {}
    for team_id, row in table.items():
        rebuilt_stats = dict(stats[team_id])
        rebuilt_stats["points"] += int(row.get("points_adjustment") or 0)
        rebuilt[team_id] = {**row, **rebuilt_stats}
    return rebuilt


def _league_record_market(record: dict[str, Any]) -> dict[str, Any] | None:
    winner_id = int(record.get("winner_team_id") or 0)
    if not winner_id:
        return None
    participants = {
        int(team.get("team_id") or 0): team
        for team in record.get("participants") or []
        if int(team.get("team_id") or 0) > 0
    }
    winner = participants.get(winner_id) or {}
    return {
        "competition_id": int(record.get("competition_id") or 0),
        "competition_name": str(record.get("competition_name") or ""),
        "season_key": str(record.get("season_key") or ""),
        "season_label": str(record.get("season_label") or ""),
        "season_start": str(record.get("season_start") or ""),
        "season_end": str(record.get("season_end") or ""),
        "settlement_date": str(
            record.get("settlement_date") or record.get("season_end") or ""
        ),
        "expected_teams": int(record.get("expected_teams") or len(participants)),
        "expected_matches": int(record.get("expected_matches") or 0) or None,
        "market_kind": "league",
        "competition_reputation": int(record.get("competition_reputation") or 0),
        "status": "complete",
        "winner_team_id": winner_id,
        "winner_team_name": str(
            record.get("winner_team_name") or winner.get("team_name") or winner_id
        ),
        "teams": [],
        "settlement_source": str(record.get("winner_source") or "league_ledger"),
    }


def _resolve_league_record(record: dict[str, Any], game_date: str) -> bool:
    """Resolve a persisted league snapshot after its season has ended."""
    if int(record.get("winner_team_id") or 0):
        return False
    if not str(record.get("season_end") or "") or game_date <= str(record["season_end"]):
        return False
    rows = [dict(row) for row in record.get("latest_table") or []]
    if record.get("resolve_from_table") is False:
        return False
    expected = int(
        record.get("nominal_expected_matches")
        or record.get("expected_matches") or 0
    )
    if not rows or expected <= 0 or any(int(row.get("played") or 0) < expected for row in rows):
        return False
    ranked = sorted(rows, key=lambda row: (
        -int(row.get("points") or 0),
        -int(row.get("goal_difference") or 0),
        -int(row.get("goals_for") or 0),
        int(row.get("position") or 999),
        int(row.get("team_id") or 0),
    ))
    if len(ranked) < 2:
        return False
    top, second = ranked[0], ranked[1]
    if (
        int(top.get("points") or 0) == int(second.get("points") or 0)
        and int(top.get("goal_difference") or 0) == int(second.get("goal_difference") or 0)
        and int(top.get("goals_for") or 0) == int(second.get("goals_for") or 0)
    ):
        return False
    record["winner_team_id"] = int(top.get("team_id") or 0)
    record["winner_team_name"] = str(top.get("team_name") or top.get("team_id") or "")
    record["winner_source"] = "persisted_terminal_table"
    record["settlement_date"] = str(record.get("season_end") or "")
    return bool(record["winner_team_id"])


def _final_settlement_due(
    output: dict[str, Any], final_fixture: dict[str, Any],
    final_result: dict[str, Any] | None = None,
) -> bool:
    game_date = str(output.get("game_date") or "")
    final_date = str(final_fixture.get("date") or "")
    if not game_date or not final_date or game_date < final_date:
        return False
    if game_date > final_date:
        return True
    if not final_result:
        return False
    confirmed_same_day = {
        _result_identity(row) for row in output.get("season_results") or []
    }
    trusted_same_day = {
        _result_identity(row) for row in output.get("settlement_results") or []
    }
    if _result_identity(final_result) in trusted_same_day:
        return True
    extended = str(final_result.get("decided_by") or "") in {"extra_time", "penalties"}
    if _result_identity(final_result) in confirmed_same_day and not extended:
        return True
    try:
        kickoff = int(final_fixture["kickoff_minutes"])
        hours, minutes = (int(value) for value in str(output.get("game_time") or "").split(":", 1))
        game_minutes = hours * 60 + minutes
    except (KeyError, TypeError, ValueError):
        return False
    duration = (
        CHAMPIONSHIP_SETTLEMENT_EXTENDED_MATCH_MINUTES
        if extended else CHAMPIONSHIP_SETTLEMENT_MINIMUM_MATCH_MINUTES
    )
    return game_minutes >= kickoff + duration


def _final_market_closed(
    output: dict[str, Any], final_fixture: dict[str, Any],
) -> bool:
    game_date = str(output.get("game_date") or "")
    final_date = str(final_fixture.get("date") or "")
    if not game_date or not final_date or game_date < final_date:
        return False
    if game_date > final_date:
        return True
    try:
        kickoff = int(final_fixture["kickoff_minutes"])
        hours, minutes = (
            int(value) for value in str(output.get("game_time") or "").split(":", 1)
        )
    except (KeyError, TypeError, ValueError):
        return False
    return hours * 60 + minutes >= kickoff


def _registered_final_result(
    results: list[dict[str, Any]], final_fixture: dict[str, Any],
    competition_id: int,
) -> dict[str, Any] | None:
    final_date = str(final_fixture.get("date") or "")
    home_id = int(final_fixture.get("home_id") or 0)
    away_id = int(final_fixture.get("away_id") or 0)
    final_key = (final_date, competition_id, home_id, away_id)
    if not all(final_key):
        return None
    exact = next(
        (row for row in results if _result_identity(row) == final_key),
        None,
    )
    if exact is not None:
        return exact

    try:
        target_date = date.fromisoformat(final_date)
    except ValueError:
        return None
    finalist_ids = {home_id, away_id}
    candidates = []
    for row in results:
        result_date, _result_competition, result_home, result_away = (
            _result_identity(row)
        )
        if {result_home, result_away} != finalist_ids:
            continue
        try:
            date_distance = abs((date.fromisoformat(result_date) - target_date).days)
        except ValueError:
            continue
        if date_distance <= 1:
            candidates.append((date_distance, result_home != home_id, row))
    if not candidates:
        return None
    best_rank = min((distance, reversed_sides) for distance, reversed_sides, _row in candidates)
    best = [
        row for distance, reversed_sides, row in candidates
        if (distance, reversed_sides) == best_rank
    ]
    outcomes = {
        (
            int(row.get("home_goals") or 0),
            int(row.get("away_goals") or 0),
            int(_final_result_winner_id(row) or 0),
            str(row.get("decided_by") or ""),
        )
        for row in best
    }
    return best[0] if len(outcomes) == 1 else None


def _cup_final_state(
    output: dict[str, Any], results: list[dict[str, Any]],
    record: dict[str, Any], config: dict[str, Any], competition_id: int,
    contender_ids: set[int],
) -> tuple[dict[str, Any], dict[str, Any] | None, bool]:
    final_fixture = dict(config.get("terminal_final_fixture") or record.get("final_fixture") or {})
    fallback_date = str(
        config.get("format_last_fixture_date")
        or record.get("season_end")
        or ""
    )
    if (
        not final_fixture
        and bool(record.get("knockout_started"))
        and len(contender_ids) == 2
        and fallback_date
    ):
        finalist_ids = set(contender_ids)
        candidates = []
        try:
            target_date = date.fromisoformat(fallback_date)
        except ValueError:
            target_date = None
        if target_date:
            for row in results:
                result_date, result_competition, home_id, away_id = (
                    _result_identity(row)
                )
                if {home_id, away_id} != finalist_ids:
                    continue
                try:
                    distance = abs(
                        (date.fromisoformat(result_date) - target_date).days
                    )
                except ValueError:
                    continue
                if distance <= 1:
                    candidates.append((
                        distance,
                        0 if result_competition == competition_id else 1,
                        row,
                    ))
        if candidates:
            best_rank = min(
                (distance, competition_rank)
                for distance, competition_rank, _row in candidates
            )
            best = [
                row for distance, competition_rank, row in candidates
                if (distance, competition_rank) == best_rank
            ]
            outcomes = {
                (
                    _result_identity(row)[2:],
                    int(row.get("home_goals") or 0),
                    int(row.get("away_goals") or 0),
                    int(_final_result_winner_id(row) or 0),
                )
                for row in best
            }
            if len(outcomes) == 1:
                inferred_result = best[0]
                result_date, _result_competition, home_id, away_id = (
                    _result_identity(inferred_result)
                )
                final_fixture = {
                    "date": result_date,
                    "home_id": home_id,
                    "away_id": away_id,
                    "inferred_from_last_fixture": True,
                }
                record["final_fixture"] = dict(final_fixture)

    final_result = _registered_final_result(
        results, final_fixture, competition_id,
    )
    if final_fixture:
        return (
            final_fixture,
            final_result,
            _final_market_closed(output, final_fixture),
        )
    game_date = str(output.get("game_date") or "")
    closed = bool(
        record.get("knockout_started")
        and len(contender_ids) == 2
        and fallback_date and game_date > fallback_date
    )
    return {}, None, closed


def _league_settlement_checkpoint(
    output: dict[str, Any], results: list[dict[str, Any]], competition_id: int,
    season_start: str, season_end: str,
) -> tuple[str, bool]:
    competition_results = [
        row for row in results
        if _result_identity(row)[1] == competition_id
        and season_start <= _result_identity(row)[0] <= season_end
    ]
    if not competition_results:
        return "", False
    result_date = max(_result_identity(row)[0] for row in competition_results)
    game_date = str(output.get("game_date") or "")
    if result_date < game_date:
        return result_date, True
    if result_date != game_date:
        return result_date, False
    confirmed = any(
        _result_identity(row)[0] == result_date for row in competition_results
    )
    return result_date, confirmed


def _league_terminal_due(
    output: dict[str, Any], competition_id: int, terminal_date: str,
    expected_teams: int = 0, results: list[dict[str, Any]] | None = None,
) -> bool:
    game_date = str(output.get("game_date") or "")
    if not terminal_date or not game_date or game_date < terminal_date:
        return False
    if game_date > terminal_date:
        return True

    fixtures = [
        match for match in output.get("matches") or []
        if int(match.get("competition_id") or 0) == competition_id
        and str(match.get("fixture_date") or "") == terminal_date
    ]
    kickoffs = [
        int(match["kickoff_minutes"]) for match in fixtures
        if match.get("kickoff_minutes") is not None
    ]
    fixture_team_ids = {
        int((match.get(side) or {}).get("id") or 0)
        for match in fixtures for side in ("home", "away")
        if int((match.get(side) or {}).get("id") or 0) > 0
    }
    full_terminal_round = bool(
        fixtures and (
            expected_teams <= 0 or len(fixture_team_ids) >= expected_teams
        )
    )
    try:
        hours, minutes = (
            int(value) for value in str(output.get("game_time") or "").split(":", 1)
        )
    except ValueError:
        hours = minutes = -1
    if full_terminal_round and len(kickoffs) == len(fixtures) and hours >= 0:
        return hours * 60 + minutes >= max(kickoffs) + CHAMPIONSHIP_SETTLEMENT_MINIMUM_MATCH_MINUTES

    confirmed_source = results if results is not None else [
        *(output.get("season_results") or []),
        *(output.get("settlement_results") or []),
    ]
    confirmed = [
        row for row in confirmed_source
        if _result_identity(row)[1] == competition_id
        and _result_identity(row)[0] == terminal_date
    ]
    if not confirmed:
        return False
    if not fixtures:
        return True
    confirmed_keys = {_result_identity(row) for row in confirmed}
    fixture_keys = {
        (
            terminal_date,
            competition_id,
            int((match.get("home") or {}).get("id") or 0),
            int((match.get("away") or {}).get("id") or 0),
        )
        for match in fixtures
    }
    return full_terminal_round and fixture_keys <= confirmed_keys


def _cup_form(
    participant_map: dict[int, dict[str, Any]],
    results: list[dict[str, Any]],
    table: dict[int, dict[str, Any]],
) -> dict[int, dict[str, Any]]:
    form = {
        team_id: {"played": 0, "points": 0, "elo": 1500.0}
        for team_id in participant_map
    }
    for row in sorted(results, key=_result_identity):
        _date, _competition_id, home_id, away_id = _result_identity(row)
        if home_id not in form or away_id not in form:
            continue
        home_goals = int(row.get("home_goals") or 0)
        away_goals = int(row.get("away_goals") or 0)
        home_rating, away_rating = form[home_id]["elo"], form[away_id]["elo"]
        expected_home = 1.0 / (1.0 + 10.0 ** ((away_rating - home_rating - 60.0) / 400.0))
        winner_id = _result_winner_id(row)
        if winner_id == home_id:
            home_score, away_score = 1.0, 0.0
        elif winner_id == away_id:
            home_score, away_score = 0.0, 1.0
        else:
            home_score = away_score = 0.5
        form[home_id]["elo"] = home_rating + 20.0 * (home_score - expected_home)
        form[away_id]["elo"] = away_rating + 20.0 * (away_score - (1.0 - expected_home))
        form[home_id]["played"] += 1
        form[away_id]["played"] += 1
        if home_goals > away_goals:
            form[home_id]["points"] += 3
        elif away_goals > home_goals:
            form[away_id]["points"] += 3
        else:
            form[home_id]["points"] += 1
            form[away_id]["points"] += 1
    for team_id, row in table.items():
        if team_id not in form:
            continue
        form[team_id].update({
            "played": int(row.get("played") or form[team_id]["played"]),
            "points": int(row.get("points") or form[team_id]["points"]),
            "elo": float(row.get("elo") or form[team_id]["elo"]),
        })
    return form


def _current_knockout_contenders(
    output: dict[str, Any], competition_id: int,
    participant_map: dict[int, dict[str, Any]],
    table: dict[int, dict[str, Any]], config: dict[str, Any],
) -> set[int] | None:
    format_row = next((
        item for item in output.get("competition_formats") or []
        if int(item.get("competition_id") or 0) == competition_id
    ), None)
    native_eliminated = {
        int(team_id) for team_id in (format_row or {}).get("knockout_eliminated_team_ids") or []
        if int(team_id)
    }
    native_active = {
        int(team_id) for team_id in (format_row or {}).get("knockout_active_team_ids") or []
        if int(team_id)
    }
    participant_ids = set(participant_map)
    scheduled_team_ids = {
        int((match.get(side) or {}).get("id") or 0)
        for match in output.get("matches") or []
        if int(match.get("competition_id") or 0) == competition_id
        and str(match.get("fixture_date") or "") >= str(output.get("game_date") or "")
        for side in ("home", "away")
    } - {0}
    if (
        native_eliminated and native_active
        and native_active < participant_ids
        and native_active <= participant_ids
        and native_eliminated.isdisjoint(native_active)
        and not (scheduled_team_ids - native_active)
    ):
        return native_active

    final = dict(config.get("terminal_final_fixture") or _final_fixture_from_rounds(output, competition_id) or {})
    if final:
        finalists = {int(final.get("home_id") or 0), int(final.get("away_id") or 0)} - {0}
        return finalists if len(finalists) == 2 and finalists <= participant_ids else None

    rounds: dict[tuple[int, int], list[dict[str, Any]]] = {}
    for match in output.get("matches") or []:
        if int(match.get("competition_id") or 0) != competition_id:
            continue
        context = match.get("competition_round")
        if not isinstance(context, dict):
            continue
        key = (int(context.get("stage_index") or 0), int(context.get("round_index") or 0))
        rounds.setdefault(key, []).append(match)
    if not rounds:
        return None
    key = max(rounds)
    rows = rounds[key]
    expected_ties = int((rows[0].get("competition_round") or {}).get("round_tie_count") or 0)
    tie_addresses = {
        int((row.get("competition_round") or {}).get("tie_address") or 0)
        for row in rows
    } - {0}
    contenders = {
        int((row.get(side) or {}).get("id") or 0)
        for row in rows for side in ("home", "away")
    } - {0}
    if not expected_ties or len(tie_addresses) != expected_ties or len(contenders) != expected_ties * 2:
        return None
    if int(config.get("byes") or 0) and key[1] == 0:
        ranked = sorted(
            table.values(),
            key=lambda row: (
                -int(row.get("points") or 0), -int(row.get("goal_difference") or 0),
                -int(row.get("goals_for") or 0), int(row.get("position") or 999),
                int(row.get("team_id") or 0),
            ),
        )
        if len(ranked) == len(participant_map) and all(int(row.get("played") or 0) >= 8 for row in ranked):
            contenders.update(
                int(row["team_id"]) for row in ranked[:int(config["byes"])]
            )
        else:
            return None
    return contenders if contenders <= set(participant_map) else None


def _league_phase_contenders(
    participant_map: dict[int, dict[str, Any]],
    table: dict[int, dict[str, Any]], config: dict[str, Any],
) -> set[int] | None:
    league_matches = int(config.get("league_matches") or 0)
    qualifiers = int(config.get("qualifiers") or 0)
    if (
        not league_matches or not qualifiers
        or len(table) != len(participant_map) or len(table) < qualifiers
    ):
        return None
    ranked = sorted(
        table.values(),
        key=lambda row: (
            -int(row.get("points") or 0), -int(row.get("goal_difference") or 0),
            -int(row.get("goals_for") or 0), int(row.get("position") or 999),
            int(row.get("team_id") or 0),
        ),
    )
    cutoff_points = int(ranked[qualifiers - 1].get("points") or 0)
    return {
        int(row["team_id"])
        for row in ranked
        if int(row.get("points") or 0) + 3 * max(
            0, league_matches - int(row.get("played") or 0),
        ) >= cutoff_points
    }


def _competition_name(output: dict[str, Any], competition_id: int, fallback: str) -> str:
    for item in output.get("competitions") or []:
        if int(item.get("id") or 0) == competition_id:
            return str(item.get("name") or fallback)
    for match in output.get("matches") or []:
        if int(match.get("competition_id") or 0) == competition_id:
            return str(match.get("competition_name") or fallback)
    return fallback


def _build_cup_markets(
    output: dict[str, Any], standings: dict[str, Any],
    all_results: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    game_date = str(output.get("game_date") or "")
    results: list[dict[str, Any]] | None = list(all_results) if all_results is not None else None
    competitions: list[dict[str, Any]] = []
    competition_catalog = {
        int(item.get("id") or 0): item
        for item in output.get("competitions") or []
    }
    with _CUP_STATE_LOCK:
        state = _load_cup_state()
        changed = False
        processed_keys: set[str] = set()
        cup_configs = _discovered_cup_configs(output)
        active_format_identities = {
            int(item.get("competition_id") or 0): str(
                item.get("competition_season_address") or ""
            )
            for item in output.get("competition_formats") or []
            if int(item.get("competition_id") or 0) > 0
        }
        for stored in state["seasons"].values():
            competition_id = int(stored.get("competition_id") or 0)
            stored_config = stored.get("format_config")
            active_identity = active_format_identities.get(competition_id, "")
            stored_identity = str(
                (stored_config or {}).get("competition_season_address") or ""
            )
            if (
                competition_id and isinstance(stored_config, dict)
                and str(stored.get("season_start") or "") <= game_date
                <= str(stored.get("season_end") or "")
                and (not active_identity or stored_identity == active_identity)
            ):
                cup_configs.setdefault(competition_id, dict(stored_config))
        for competition_id, config in cup_configs.items():
            _table_name, table = _table_map(standings, competition_id)
            season_label, season_start, season_end = _format_season_window(output, config)
            season_key = f"{competition_id}:{season_label}"
            record = state["seasons"].get(season_key)
            current_format_identity = str(
                config.get("competition_season_address") or ""
            )
            stored_format_identity = str(
                ((record or {}).get("format_config") or {}).get(
                    "competition_season_address"
                ) or ""
            )
            if (
                record is not None and current_format_identity
                and stored_format_identity != current_format_identity
            ):
                # Reallocated pointers with the same observed interval describe
                # the same tournament. A new tournament in the same calendar
                # gets a date-qualified key without deleting pending evidence.
                old_config = record.get("format_config") or {}
                old_start = str(old_config.get("format_first_fixture_date") or "")
                new_start = str(config.get("format_first_fixture_date") or "")
                old_terminal = str((record.get("final_fixture") or {}).get("date") or (
                    old_config.get("format_last_fixture_date")
                    if old_config.get("season_fixture_bounds_verified") else ""
                ) or "")
                if old_start and new_start and old_terminal and new_start > old_terminal:
                    season_key = f"{competition_id}:{season_label}@{new_start}"
                    record = state["seasons"].get(season_key)
                elif not record.get("participants") and not record.get("final_fixture") and not record.get("winner_team_id"):
                    record = None
            sources = _team_sources(output, competition_id)
            expected = int(config["participants"])
            if record is None and len(sources) != expected:
                continue
            if results is None:
                results = _result_rows(output)
            competition_results = [
                row for row in results
                if _result_identity(row)[1] == competition_id
                and season_start <= _result_identity(row)[0] <= season_end
            ]
            if record is None and len(sources) == expected:
                candidate_teams = list(sources.values())
                initial_contenders = _current_knockout_contenders(
                    output, competition_id, sources, table, config,
                )
                required_profile_ids = initial_contenders or set(sources)
                if all(
                    _profile_is_usable(sources[team_id])
                    for team_id in required_profile_ids
                ):
                    fixture_dates = [
                        str(match.get("fixture_date") or "")
                        for match in output.get("matches") or []
                        if int(match.get("competition_id") or 0) == competition_id
                    ]
                    record = {
                        "competition_id": competition_id,
                        "competition_name": _competition_name(
                            output, competition_id, str(config["name"]),
                        ),
                        "season_key": season_key,
                        "season_label": season_label,
                        "season_start": season_start,
                        "season_end": season_end,
                        "field_confirmed_at": game_date,
                        "first_fixture_date": min(fixture_dates) if fixture_dates else game_date,
                        "participants": candidate_teams,
                        "format_config": dict(config),
                    }
                    if initial_contenders and initial_contenders < set(sources):
                        record["contender_team_ids"] = sorted(initial_contenders)
                        record["knockout_started"] = True
                    state["seasons"][season_key] = record
                    changed = True
            if record is None:
                continue
            processed_keys.add(season_key)

            participant_map = {
                int(team["team_id"]): dict(team)
                for team in record.get("participants") or []
            }
            if len(participant_map) != expected:
                continue
            for team_id, source in sources.items():
                if team_id in participant_map and _profile_is_usable(source):
                    participant_map[team_id].update(source)
            record["participants"] = list(participant_map.values())
            updated_format_config = {
                **dict(record.get("format_config") or {}),
                **dict(config),
            }
            if record.get("format_config") != updated_format_config:
                record["format_config"] = updated_format_config
                changed = True

            final = dict(config.get("terminal_final_fixture") or _final_fixture_from_rounds(output, competition_id) or {})
            if final and record.get("final_fixture") != final:
                record["final_fixture"] = final
                changed = True

            inferred_contenders = _current_knockout_contenders(
                output, competition_id, participant_map, table, config,
            )
            stored_contenders = {
                int(team_id) for team_id in record.get("contender_team_ids") or participant_map
            }
            if stored_contenders - set(participant_map):
                # Older builds could retain contender IDs after the native cup
                # field had changed. Drop only the derived knockout state and
                # rebuild it from current native evidence on the next refresh.
                record.pop("contender_team_ids", None)
                record.pop("knockout_started", None)
                changed = True
                continue
            phase_contenders = _league_phase_contenders(
                participant_map, table, config,
            )
            if phase_contenders and phase_contenders < stored_contenders:
                stored_contenders &= phase_contenders
                record["contender_team_ids"] = sorted(stored_contenders)
                changed = True
            if inferred_contenders and inferred_contenders <= stored_contenders:
                if inferred_contenders != stored_contenders:
                    stored_contenders = set(inferred_contenders)
                    record["contender_team_ids"] = sorted(stored_contenders)
                    changed = True
                if not record.get("knockout_started"):
                    record["knockout_started"] = True
                    changed = True

            previous_final_fixture = dict(record.get("final_fixture") or {})
            final_fixture, final_result, final_market_closed = _cup_final_state(
                output, results, record, config, competition_id,
                stored_contenders,
            )
            if final_fixture != previous_final_fixture:
                changed = True
            final_key = (
                str(final_fixture.get("date") or ""), competition_id,
                int(final_fixture.get("home_id") or 0),
                int(final_fixture.get("away_id") or 0),
            )
            settlement_due = _final_settlement_due(output, final_fixture, final_result)
            winner_id = (
                int(record.get("winner_team_id") or 0) or None
                if settlement_due else None
            )
            if settlement_due and winner_id is None and all(final_key):
                winner_id = _final_result_winner_id(final_result or {}, final_fixture)
                if winner_id:
                    record["winner_team_id"] = winner_id
                    changed = True
            base = {
                "competition_id": competition_id,
                "competition_name": str(record.get("competition_name") or config["name"]),
                "season_key": season_key,
                "season_label": season_label,
                "season_start": str(record.get("first_fixture_date") or season_start),
                "season_window_start": season_start,
                "season_end": season_end,
                "settlement_date": str(
                    final_fixture.get("date")
                    or config.get("format_last_fixture_date")
                    or record.get("season_end")
                    or ""
                ),
                "expected_teams": expected,
                "expected_matches": None,
                "market_kind": "cup",
                "competition_reputation": int(
                    config.get("competition_reputation")
                    or (competition_catalog.get(competition_id) or {}).get("reputation")
                    or 0
                ),
            }
            if winner_id and winner_id in participant_map:
                competitions.append({
                    **base, "status": "complete", "winner_team_id": winner_id,
                    "winner_team_name": participant_map[winner_id]["team_name"], "teams": [],
                })
                continue

            if final_market_closed:
                competitions.append({
                    **base,
                    "status": "awaiting_result",
                    "winner_team_id": None,
                    "winner_team_name": None,
                    "teams": [],
                })
                continue

            form = _cup_form(participant_map, competition_results, table)
            teams = [
                {
                    **team,
                    "played": int(form[team_id]["played"]),
                    "points": int(form[team_id]["points"]),
                    "goal_difference": int((table.get(team_id) or {}).get("goal_difference") or 0),
                    "elo": float(form[team_id]["elo"]),
                    "position": int((table.get(team_id) or {}).get("position") or 0),
                }
                for team_id, team in participant_map.items()
                if team_id in stored_contenders
            ]
            if not teams or not all(_profile_is_usable(team) for team in teams):
                continue
            market_version = _market_version(
                competition_id, season_key, game_date, teams,
            )
            final_prices = _final_winner_prices(
                output, competition_id, {int(team["team_id"]) for team in teams},
            )
            if final_prices:
                quote_payload = json.dumps(final_prices, sort_keys=True, separators=(",", ":"))
                market_version = hashlib.sha256(
                    f"{market_version}:{quote_payload}".encode("ascii")
                ).hexdigest()[:20]
            knockout_started = bool(record.get("knockout_started"))
            qualifiers = len(teams) if knockout_started else min(
                len(teams), int(config["qualifiers"]),
            )
            byes = (
                (1 << (len(teams) - 1).bit_length()) - len(teams)
                if knockout_started else int(config["byes"])
            )
            prices = _cached_cup_prices(
                teams, qualifiers, byes,
                market_version, f"{season_key}:{market_version}",
                league_matches=None if knockout_started else (
                    int(config.get("league_matches") or 0) or None
                ),
            )
            if not prices:
                continue
            if final_prices:
                for team in prices:
                    quote = final_prices.get(int(team["team_id"]))
                    if quote:
                        team["odds"] = quote["odds"]
                        team["champion_probability"] = round(quote["probability"], 6)
                        team["pricing_source"] = "final_winner_market"
                prices.sort(key=lambda item: (float(item["odds"]), str(item["team_name"]).casefold()))
            competitions.append({
                **base, "status": "open", "market_version": market_version,
                "pricing_source": "final_winner_market" if final_prices else "championship_model",
                "teams": prices,
            })
        for season_key, record in state["seasons"].items():
            if season_key in processed_keys:
                continue
            competition_id = int(record.get("competition_id") or 0)
            config = record.get("format_config") or cup_configs.get(competition_id)
            if not config:
                continue
            participant_map = {
                int(team["team_id"]): dict(team)
                for team in record.get("participants") or []
            }
            contender_ids = {
                int(team_id)
                for team_id in record.get("contender_team_ids") or participant_map
            }
            if results is None:
                results = _result_rows(output)
            previous_final_fixture = dict(record.get("final_fixture") or {})
            final_fixture, final_result, final_market_closed = _cup_final_state(
                output, results, record, config, competition_id,
                contender_ids,
            )
            if final_fixture != previous_final_fixture:
                changed = True
            final_key = (
                str(final_fixture.get("date") or ""), competition_id,
                int(final_fixture.get("home_id") or 0),
                int(final_fixture.get("away_id") or 0),
            )
            settlement_due = _final_settlement_due(output, final_fixture, final_result)
            winner_id = (
                int(record.get("winner_team_id") or 0) or None
                if settlement_due else None
            )
            if settlement_due and winner_id is None and all(final_key):
                winner_id = _final_result_winner_id(final_result or {}, final_fixture)
                if winner_id:
                    record["winner_team_id"] = winner_id
                    changed = True
            settlement_date = str(
                final_fixture.get("date")
                or config.get("format_last_fixture_date")
                or record.get("season_end")
                or ""
            )
            if winner_id and winner_id in participant_map:
                competitions.append({
                    "competition_id": competition_id,
                    "competition_name": str(record.get("competition_name") or config["name"]),
                    "season_key": season_key,
                    "_settlement_only": any(key.startswith(f"{competition_id}:") for key in processed_keys),
                    "season_label": str(record.get("season_label") or ""),
                    "season_start": str(record.get("first_fixture_date") or record.get("season_start") or ""),
                    "season_end": str(record.get("season_end") or ""),
                    "settlement_date": settlement_date,
                    "expected_teams": int(config["participants"]),
                    "expected_matches": None,
                    "market_kind": "cup",
                    "competition_reputation": int(
                        config.get("competition_reputation")
                        or (competition_catalog.get(competition_id) or {}).get("reputation")
                        or 0
                    ),
                    "status": "complete",
                    "winner_team_id": winner_id,
                    "winner_team_name": participant_map[winner_id]["team_name"],
                    "teams": [],
                })
            elif final_market_closed:
                competitions.append({
                    "competition_id": competition_id,
                    "competition_name": str(
                        record.get("competition_name") or config["name"]
                    ),
                    "season_key": season_key,
                    "_settlement_only": any(key.startswith(f"{competition_id}:") for key in processed_keys),
                    "season_label": str(record.get("season_label") or ""),
                    "season_start": str(
                        record.get("first_fixture_date")
                        or record.get("season_start") or ""
                    ),
                    "season_end": str(record.get("season_end") or ""),
                    "settlement_date": settlement_date,
                    "expected_teams": int(config["participants"]),
                    "expected_matches": None,
                    "market_kind": "cup",
                    "competition_reputation": int(
                        config.get("competition_reputation")
                        or (competition_catalog.get(competition_id) or {}).get(
                            "reputation"
                        )
                        or 0
                    ),
                    "status": "awaiting_result",
                    "winner_team_id": None,
                    "winner_team_name": None,
                    "teams": [],
                })
        if changed:
            _save_cup_state(state)
    return competitions


def build_championship_markets(
    output: dict[str, Any], standings: dict[str, Any],
) -> dict[str, Any]:
    game_date = str(output.get("game_date") or "")
    try:
        date.fromisoformat(game_date)
    except ValueError:
        return {"model_version": CHAMPIONSHIP_MODEL_VERSION, "competitions": []}

    competitions = []
    all_results = _result_rows(output)
    league_state: dict[str, Any] = {"schema_version": 1, "seasons": {}}
    historical_settlements: list[dict[str, Any]] = []
    league_state_changed = False
    try:
        with _LEAGUE_STATE_LOCK:
            league_state = _load_league_state()
            for record in league_state.get("seasons", {}).values():
                if not isinstance(record, dict):
                    continue
                if _resolve_league_record(record, game_date):
                    league_state_changed = True
                market = _league_record_market(record)
                if market:
                    historical_settlements.append(market)
            if league_state_changed:
                _save_league_state(league_state)
    except (OSError, RuntimeError, ValueError):
        # Unit tests and pre-identity startup may not have a cache scope yet.
        league_state = {"schema_version": 1, "seasons": {}}
        historical_settlements = []
    league_configs = dict(FAMOUS_LEAGUES)
    league_configs.update(_discovered_league_configs(output, standings))
    for format_row in output.get("competition_formats") or []:
        if format_row.get("has_terminal_cup_stage") and format_row.get("competition_kind") == "league":
            league_configs.pop(int(format_row.get("competition_id") or 0), None)
    competition_catalog = {
        int(item.get("id") or 0): item
        for item in output.get("competitions") or []
    }
    for competition_id, config in league_configs.items():
        table_name, table = _table_map(standings, competition_id)
        native_team_ids = _native_league_team_ids(output, competition_id)
        season_label, season_start, season_end = _format_season_window(output, config)
        table = _confirmed_league_table(
            table, all_results, competition_id, season_start, season_end,
        )
        # No native identity/current result means a completed table cannot be
        # relabelled as a new season or cleared using a guessed match count.
        if (
            not config.get("format_first_fixture_date")
            and not config.get("observed_nonstandard_schedule")
            and not config.get("settlement_requires_terminal")
            and table and all(int(row.get("played") or 0) >= int(config["matches"]) for row in table.values())
            and not any(
                _result_identity(row)[1] == competition_id
                and season_start <= _result_identity(row)[0] <= season_end
                for row in all_results
            )
        ):
            continue
        evidence_team_ids: set[int] = set()
        evidence_team_names: dict[int, str] = {}
        for result in all_results:
            result_date, result_competition, home_id, away_id = _result_identity(result)
            if (
                result_competition == competition_id
                and season_start <= result_date <= season_end
            ):
                evidence_team_ids.update((home_id, away_id))
                for side, team_id in (("home", home_id), ("away", away_id)):
                    team = result.get(side) or result.get(f"{side}_team") or {}
                    if team_id > 0 and str(team.get("name") or ""):
                        evidence_team_names[team_id] = str(team["name"])
        for match in output.get("matches") or []:
            fixture_date = str(match.get("fixture_date") or "")
            if (
                int(match.get("competition_id") or 0) == competition_id
                and season_start <= fixture_date <= season_end
            ):
                evidence_team_ids.update(
                    int((match.get(side) or {}).get("id") or 0)
                    for side in ("home", "away")
                )
                for side in ("home", "away"):
                    team = match.get(side) or {}
                    team_id = int(team.get("id") or 0)
                    if team_id > 0 and str(team.get("name") or ""):
                        evidence_team_names[team_id] = str(team["name"])
        evidence_team_ids.discard(0)
        participant_team_ids, participant_source = _reconciled_league_team_ids(
            native_team_ids, table, evidence_team_ids, int(config["teams"]),
        )
        if len(participant_team_ids) == int(config["teams"]):
            table = {
                team_id: row for team_id, row in table.items()
                if team_id in participant_team_ids
            }
        sources = _team_sources(output, competition_id)
        if len(participant_team_ids) == int(config["teams"]):
            sources = {
                team_id: source for team_id, source in sources.items()
                if team_id in participant_team_ids
            }
        team_ids = (
            set(participant_team_ids)
            if len(participant_team_ids) == int(config["teams"])
            else set(table) | set(sources)
        )
        if len(team_ids) != int(config["teams"]):
            continue
        teams = []
        for team_id in sorted(team_ids):
            source = sources.get(team_id) or {}
            row = table.get(team_id) or {}
            elo = float(row.get("elo") or 1500.0)
            profile = dict(source.get("profile") or {})
            reputation = int(source.get("reputation") or 0)
            if not _profile_is_usable({
                "profile": profile, "reputation": reputation,
            }):
                synthetic_ca = min(175.0, max(55.0, 100.0 + (elo - 1500.0) * 0.08))
                profile = {
                    "candidate_players": 20,
                    "candidate_13_ca": round(synthetic_ca + 2.0, 2),
                    "candidate_20_ca": round(synthetic_ca, 2),
                }
                if not 1 <= reputation <= 10_000:
                    reputation = min(
                        10_000,
                        max(1, int(config.get("competition_reputation") or 1) * 100),
                    )
            teams.append({
                "team_id": team_id,
                "team_name": str(
                    source.get("team_name") or row.get("team_name")
                    or evidence_team_names.get(team_id) or team_id
                ),
                "reputation": reputation,
                "profile": profile,
                "played": int(row.get("played") or 0),
                "points": int(row.get("points") or 0),
                "goal_difference": int(row.get("goal_difference") or 0),
                "goals_for": int(row.get("goals_for") or 0),
                "elo": elo,
                "position": int(row.get("position") or 0),
            })
        observed_dates = [
            _result_identity(row)[0]
            for row in all_results
            if _result_identity(row)[1] == competition_id
            and season_start <= _result_identity(row)[0] <= season_end
        ]
        observed_dates.extend(
            str(match.get("fixture_date") or "")
            for match in output.get("matches") or []
            if int(match.get("competition_id") or 0) == competition_id
            and season_start <= str(match.get("fixture_date") or "") <= season_end
        )
        actual_season_start = min(observed_dates) if observed_dates else season_start
        played_counts = [int(team["played"]) for team in teams]
        terminal_date = str(config.get("format_last_fixture_date") or "")
        if not season_start <= terminal_date <= season_end:
            terminal_date = ""
        terminal_due = _league_terminal_due(
            output, competition_id, terminal_date, len(teams), all_results,
        )
        terminal_required = bool(
            config.get("settlement_requires_terminal") and terminal_date
        )
        equal_completed_schedule = bool(
            played_counts and min(played_counts) > 0
            and min(played_counts) == max(played_counts)
        )
        expected_matches = int(config["matches"])
        schedule_count_uncertain = bool(
            terminal_required and not terminal_due
            and max(played_counts) >= expected_matches
        )
        if terminal_required:
            complete = terminal_due and equal_completed_schedule
            expected_matches = (
                max(played_counts)
                if complete else max(expected_matches, max(played_counts) + 1)
            )
        elif config.get("observed_nonstandard_schedule"):
            complete = False
            expected_matches = max(expected_matches, max(played_counts) + 1)
        else:
            complete = all(
                played >= expected_matches for played in played_counts
            )
        sorted_table = sorted(
            teams,
            key=lambda item: (
                -int(item["points"]), -int(item["goal_difference"]),
                str(item["team_name"]).casefold(), int(item["team_id"]),
            ),
        )
        native_leaders = [
            team for team in teams if int(team.get("position") or 0) == 1
        ]
        winner = (
            native_leaders[0]
            if complete and len(native_leaders) == 1
            else sorted_table[0]
        )
        unique_winner = bool(
            complete and len(sorted_table) > 1
            and (
                len(native_leaders) == 1
                or int(sorted_table[0]["points"]) > int(sorted_table[1]["points"])
            )
        )
        competition_name = table_name or str(config["name"])
        season_key = f"{competition_id}:{season_label}"
        base = {
            "competition_id": competition_id,
            "competition_name": competition_name,
            "season_key": f"{competition_id}:{season_label}",
            "season_label": season_label,
            "season_start": actual_season_start,
            "season_window_start": season_start,
            "season_end": season_end,
            "settlement_date": season_end,
            "expected_teams": int(config["teams"]),
            "expected_matches": expected_matches,
            "market_kind": "league",
            "participant_source": participant_source,
            "competition_reputation": int(
                config.get("competition_reputation")
                or (competition_catalog.get(competition_id) or {}).get("reputation")
                or 0
            ),
        }
        try:
            with _LEAGUE_STATE_LOCK:
                league_record = league_state.setdefault("seasons", {}).setdefault(
                    season_key,
                    {
                        "competition_id": competition_id,
                        "competition_name": competition_name,
                        "season_key": season_key,
                        "season_label": season_label,
                        "season_start": actual_season_start,
                        "season_end": season_end,
                        "expected_teams": int(config["teams"]),
                        "expected_matches": expected_matches,
                        "nominal_expected_matches": int(config["matches"]),
                        "resolve_from_table": not bool(
                            config.get("observed_nonstandard_schedule") or terminal_required
                        ),
                        "competition_reputation": int(
                            config.get("competition_reputation") or 0
                        ),
                    },
                )
                league_record.update({
                    "competition_id": competition_id,
                    "competition_name": competition_name,
                    "season_key": season_key,
                    "season_label": season_label,
                    "season_start": actual_season_start,
                    "season_end": season_end,
                    "expected_teams": int(config["teams"]),
                    "expected_matches": expected_matches,
                    "nominal_expected_matches": int(config["matches"]),
                    "resolve_from_table": not bool(
                        config.get("observed_nonstandard_schedule") or terminal_required
                    ),
                    "competition_reputation": int(
                        config.get("competition_reputation") or 0
                    ),
                    "participants": [
                        {
                            "team_id": int(team["team_id"]),
                            "team_name": str(team["team_name"]),
                        }
                        for team in teams
                    ],
                    "latest_table": [
                        {
                            "team_id": int(team["team_id"]),
                            "team_name": str(team["team_name"]),
                            "played": int(team.get("played") or 0),
                            "points": int(team.get("points") or 0),
                            "goal_difference": int(
                                team.get("goal_difference") or 0
                            ),
                            "goals_for": int(team.get("goals_for") or 0),
                            "position": int(team.get("position") or 0),
                        }
                        for team in teams
                    ],
                    "last_seen_game_date": game_date,
                })
                if unique_winner and (terminal_due if terminal_required else _league_settlement_checkpoint(
                    output, all_results, competition_id, season_start, season_end,
                )[1]):
                    league_record["winner_team_id"] = int(winner["team_id"])
                    league_record["winner_team_name"] = str(winner["team_name"])
                    league_record["winner_source"] = "current_season_table"
                    league_record["settlement_date"] = season_end
                league_state_changed = True
        except (OSError, RuntimeError, ValueError):
            pass
        checkpoint_date, settlement_due = _league_settlement_checkpoint(
            output, all_results, competition_id, season_start, season_end,
        )
        completion_checkpoint_date = (
            terminal_date if terminal_required else checkpoint_date
        )
        completion_settlement_due = (
            terminal_due if terminal_required else settlement_due
        )
        if complete:
            if unique_winner:
                competitions.append({
                    **base,
                    "settlement_date": completion_checkpoint_date or season_end,
                    "status": "complete" if completion_settlement_due else "locked",
                    "winner_team_id": int(winner["team_id"]),
                    "winner_team_name": str(winner["team_name"]),
                    "teams": [] if completion_settlement_due else [{
                        **winner, "champion_probability": 1.0, "odds": None,
                    }],
                })
                continue
            competitions.append({
                **base,
                "status": "awaiting_tiebreak",
                "winner_team_id": None,
                "winner_team_name": None,
                "teams": [],
            })
            continue
        if not all(_profile_is_usable(team) for team in teams):
            continue
        leader_points = max(int(team["points"]) for team in teams)
        eligible_team_ids = (
            {int(team["team_id"]) for team in teams}
            if schedule_count_uncertain else {
                int(team["team_id"])
                for team in teams
                if int(team["points"]) + 3 * max(
                    0, expected_matches - int(team["played"]),
                ) >= leader_points
            }
        )
        # A mathematically locked regular-season leader is not necessarily the
        # competition champion when FM exposes playoffs or another
        # non-standard schedule.  Those formats must wait for their verified
        # native terminal stage; otherwise the latest regular-season result
        # could be mistaken for the final result during the break.
        if (
            len(eligible_team_ids) == 1
            and not terminal_required
            and not config.get("observed_nonstandard_schedule")
        ):
            winner_id = next(iter(eligible_team_ids))
            winner = next(
                team for team in teams if int(team["team_id"]) == winner_id
            )
            competitions.append({
                **base,
                "settlement_date": checkpoint_date or season_end,
                "status": "complete" if settlement_due else "locked",
                "winner_team_id": winner_id,
                "winner_team_name": str(winner["team_name"]),
                "teams": [] if settlement_due else [{
                    **winner,
                    "champion_probability": 1.0,
                    "odds": None,
                }],
            })
            continue
        market_version = _market_version(
            competition_id, str(base["season_key"]), game_date, teams,
        )
        competitions.append({
            **base,
            "status": "open",
            "market_version": market_version,
            "teams": _cached_prices(
                teams, expected_matches,
                market_version,
                f"{base['season_key']}:{market_version}",
                eligible_team_ids=eligible_team_ids,
                simulations=(
                    CHAMPIONSHIP_GENERIC_SIMULATIONS
                    if str(config.get("source") or "").startswith("native_league_")
                    else CHAMPIONSHIP_SIMULATIONS
                ),
            ),
        })
    if league_state_changed:
        try:
            with _LEAGUE_STATE_LOCK:
                _save_league_state(league_state)
        except (OSError, RuntimeError, ValueError):
            pass
    current_competitions = []
    for item in competitions:
        if str(item.get("season_window_start") or item.get("season_start") or "") <= game_date <= str(item.get("season_end") or ""):
            current_competitions.append(item)
        elif item.get("status") == "complete":
            historical_settlements.append(item)
    competitions = current_competitions
    # A refresh can roll into a new season before the account settlement pass.
    # Keep resolved prior-season records available to that pass without mixing
    # them into the current UI market list.
    settled_by_key: dict[str, dict[str, Any]] = {}
    for item in historical_settlements:
        key = str(item.get("season_key") or "")
        if key:
            settled_by_key[key] = item
    cup_configs = _discovered_cup_configs(output)
    for item in _build_cup_markets(output, standings, all_results):
        settlement_only = item.pop("_settlement_only", False)
        if not settlement_only and str(item.get("season_window_start") or item.get("season_start") or "") <= game_date <= str(item.get("season_end") or ""):
            competitions.append(item)
        else:
            settled_by_key[str(item.get("season_key") or "")] = item
    competitions.sort(key=lambda item: (
        0 if item.get("status") == "open" else 1,
        -int(item.get("competition_reputation") or 0),
        str(item.get("competition_name") or "").casefold(),
    ))
    return {
        "model_version": CHAMPIONSHIP_MODEL_VERSION,
        "margin": CHAMPIONSHIP_MARGIN,
        "simulations": CHAMPIONSHIP_SIMULATIONS,
        "competitions": competitions,
        "settlement_competitions": list(settled_by_key.values()),
        "discovery_diagnostics": _championship_discovery_diagnostics(
            output, league_configs, cup_configs, competitions,
        ),
    }
