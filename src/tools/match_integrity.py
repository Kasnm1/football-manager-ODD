from __future__ import annotations

import json
import threading
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from tools.account_store import load_document, save_document
from tools.app_paths import save_data_root
from tools.betting_account import (
    attach_legacy_integrity_evidence,
    integrity_bet_snapshot,
    legacy_integrity_bet_snapshot,
    mark_integrity_bets_assessed,
)
from tools.club_economy import collect_match_integrity_penalty


_LOCK = threading.RLock()
LEGACY_AUDIT_VERSION = "v1.6.2-startup-ca"
INTEGRITY_REVIEW_DELAY_DAYS = 1
MATCH_ANOMALOUS_STAKE_SHARE_THRESHOLD = 0.75
MATCH_ANOMALOUS_STAKE_MINIMUM = 10_000_000.0
OPPONENT_WIN_DIRECT_ODDS_THRESHOLD = 8.0
RECENT_ATTEMPT_WINDOW = 4
RECENT_ATTEMPT_HITS = 3


def integrity_path() -> Path:
    return save_data_root() / "betting" / "match_integrity.json"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _generic_manager_name(value: Any) -> bool:
    name = str(value or "").strip()
    if not name or name in {"经理", "教练", "未知", "待读取"}:
        return True
    return name.startswith("经理 ") and name[3:].isdigit()


def _resolved_manager_name(output: dict[str, Any], preferred: str = "") -> str:
    manager = output.get("manager") or {}
    manager_id = int(output.get("selected_manager_id") or manager.get("id") or 0)
    option = next((
        row for row in output.get("manager_options") or []
        if int(row.get("id") or 0) == manager_id
    ), {})
    candidates = [preferred, option.get("name"), manager.get("name")]
    specific = next((str(value).strip() for value in candidates if not _generic_manager_name(value)), "")
    if specific:
        return specific
    return next((str(value).strip() for value in candidates if str(value or "").strip()), "经理")


def _event_display_stake(event: dict[str, Any]) -> float:
    stakes = event.get("stake_by_bet")
    if isinstance(stakes, dict):
        total = round(sum(max(0.0, float(value or 0)) for value in stakes.values()), 2)
        if total > 0:
            return total
    for field in ("stake", "anomalous_fixture_stake"):
        amount = round(max(0.0, float(event.get(field) or 0)), 2)
        if amount > 0:
            return amount
    return 0.0


def _new_state() -> dict[str, Any]:
    return {"schema_version": 3, "events": [], "attempts": [], "penalties": []}


def _load_state() -> dict[str, Any]:
    payload = load_document("match_integrity", None, legacy_path=integrity_path())
    if not isinstance(payload, dict):
        payload = _new_state()
    payload.setdefault("events", [])
    payload.setdefault("attempts", [])
    payload.setdefault("penalties", [])
    try:
        payload["schema_version"] = max(3, int(payload.get("schema_version") or 1))
    except (TypeError, ValueError):
        payload["schema_version"] = 3
    for penalty in payload["penalties"]:
        if penalty.get("fourth_penalty_kinds") and not penalty.get("fourth_waived"):
            penalty["fourth_effective"] = True
            waiver = penalty.get("fourth_waiver")
            if isinstance(waiver, dict):
                waiver["available"] = False
    return payload


def _save_state(payload: dict[str, Any]) -> None:
    save_document("match_integrity", payload, legacy_path=integrity_path())


def _fixture_key(item: dict[str, Any]) -> str:
    return "|".join(map(str, (
        item.get("fixture_date") or item.get("date"), item.get("competition_id"),
        item.get("home_id") or (item.get("home") or {}).get("id"),
        item.get("away_id") or (item.get("away") or {}).get("id"),
    )))


_SAFE_MANAGED_SCORES = {
    (1, 0), (2, 0), (3, 0), (2, 1),
    (1, 1), (3, 1), (4, 0), (4, 1),
}


def _market_side(code: str, managed_team_id: int) -> str | None:
    if code.startswith("home"):
        return "home"
    if code.startswith("away"):
        return "away"
    try:
        return "managed" if int(code.split("_", 1)[0]) == managed_team_id else "opponent"
    except (TypeError, ValueError):
        return None


def _monitoring_group(market: str, code: str, managed_side: str, managed_team_id: int) -> str:
    if market in {"TEAM_GOALS", "HT_TEAM_GOALS", "SH_TEAM_GOALS", "TEAM_OU", "CLEAN_SHEET"}:
        side = _market_side(code, managed_team_id)
        scope = "managed" if side == managed_side else "opponent" if side in {"home", "away"} else side
        return f"{market}:{scope or 'unknown'}"
    if market == "ADVANCE_METHOD":
        return f"{market}:{_market_side(code, managed_team_id) or 'unknown'}"
    return market


def _selection_key(code: str, line: Any) -> str:
    normalized_line = line
    if line is not None:
        try:
            numeric_line = float(line)
            normalized_line = int(numeric_line) if numeric_line.is_integer() else round(numeric_line, 4)
        except (TypeError, ValueError):
            normalized_line = str(line)
    return json.dumps([str(code), normalized_line], ensure_ascii=False, separators=(",", ":"))


def _managed_score(line: Any, managed_side: str) -> tuple[int, int] | None:
    try:
        home_goals, away_goals = (
            int(value) for value in str(line).replace(":", "-").split("-", 1)
        )
    except (TypeError, ValueError):
        return None
    return (home_goals, away_goals) if managed_side == "home" else (away_goals, home_goals)


def _line_at_least(line: Any, minimum: float) -> bool:
    try:
        return float(line) >= minimum
    except (TypeError, ValueError):
        return False


def _monitoring_exemption(
    market: str, code: str, line: Any, managed_side: str, managed_team_id: int,
) -> str | None:
    if market in {"GOAL_PARITY", "BTTS"}:
        return "goal_parity" if market == "GOAL_PARITY" else "both_teams_to_score"
    if market == "TOTAL_GOALS" and code == "exact":
        try:
            if int(float(line)) in {1, 2, 3} and float(line).is_integer():
                return "safe_total_goals"
        except (TypeError, ValueError):
            pass
    if market in {"1X2", "HT_1X2", "SH_1X2"} and code == managed_side:
        return "managed_team_win"
    if market in {"AH", "HT_AH", "SH_AH"} and code == managed_side:
        return "managed_team_handicap"
    if market == "DOUBLE_CHANCE" and code == (
        "home_draw" if managed_side == "home" else "draw_away"
    ):
        return "managed_team_double_chance"
    if market == "HTFT":
        parts = code.split("_", 1)
        if (
            len(parts) == 2
            and parts[1] == managed_side
            and parts[0] in {managed_side, "draw"}
        ):
            return "managed_team_full_time_win"
    if market == "CLEAN_SHEET" and code == f"{managed_side}_yes":
        return "managed_team_clean_sheet"
    if market == "WIN_TO_NIL" and code == managed_side:
        return "managed_team_win_to_nil"
    if market == "WINNING_MARGIN" and code.startswith(f"{managed_side}_"):
        return "managed_team_winning_margin"
    if market == "ADVANCE":
        try:
            if int(code) == managed_team_id:
                return "managed_team_advance"
        except (TypeError, ValueError):
            pass
    if market == "ADVANCE_METHOD":
        try:
            if int(code.split("_", 1)[0]) == managed_team_id:
                return "managed_team_advance_method"
        except (TypeError, ValueError):
            pass
    if market == "SCORE" and _managed_score(line, managed_side) in _SAFE_MANAGED_SCORES:
        return "safe_managed_score"
    return None


def _monitored_control_market(
    market: str, code: str, line: Any, managed_side: str, managed_team_id: int,
) -> bool:
    if _monitoring_exemption(market, code, line, managed_side, managed_team_id):
        return False
    if market in {"SCORE", "HT_SCORE", "SH_SCORE"}:
        return True
    if market == "TOTAL_GOALS":
        return code == "exact" or (code == "seven_plus" and _line_at_least(line, 7))
    if market in {"HT_TOTAL_GOALS", "SH_TOTAL_GOALS"}:
        return code == "exact"
    if market in {"TEAM_GOALS", "HT_TEAM_GOALS", "SH_TEAM_GOALS"}:
        side = _market_side(code, managed_team_id)
        opponent_side = "away" if managed_side == "home" else "home"
        if code in {"home", "away"}:
            return True
        return bool(
            market == "TEAM_GOALS"
            and side == opponent_side
            and code.endswith("_plus")
            and _line_at_least(line, 5)
        )
    if market in {
        "HTFT", "WINNING_MARGIN", "CLEAN_SHEET", "WIN_TO_NIL", "ADVANCE_METHOD",
    }:
        return True
    if market == "TEAM_OU":
        side = _market_side(code, managed_team_id)
        direction = str(code).rsplit("_", 1)[-1]
        opponent_side = "away" if managed_side == "home" else "home"
        return side == opponent_side and direction == "over"
    return False


def _direct_selection_kind(
    market: str, code: str, line: Any, managed_side: str, managed_team_id: int,
) -> str | None:
    opponent_side = "away" if managed_side == "home" else "home"
    if market == "1X2":
        if code == opponent_side:
            return "opponent_full_time_win"
        return "full_time_draw" if code == "draw" else None
    if market in {"HT_1X2", "SH_1X2"}:
        period = "first_half" if market == "HT_1X2" else "second_half"
        if code == opponent_side:
            return f"opponent_{period}_win"
        return f"{period}_draw" if code == "draw" else None
    if market == "HTFT":
        parts = code.split("_", 1)
        if len(parts) != 2:
            return None
        if opponent_side in parts:
            return "opponent_half_full_result"
        return "half_full_draw_result" if "draw" in parts else None
    if market == "TEAM_GOALS":
        return (
            "opponent_five_plus_goals"
            if _market_side(code, managed_team_id) == opponent_side
            and code.endswith("_plus")
            and _line_at_least(line, 5)
            else None
        )
    if market == "CLEAN_SHEET":
        return "opponent_clean_sheet" if code == f"{opponent_side}_yes" else None
    if market == "WIN_TO_NIL":
        return "opponent_win_to_nil" if code == opponent_side else None
    if market in {"AH", "HT_AH", "SH_AH"}:
        return "opponent_handicap_win" if code == opponent_side else None
    if market == "WINNING_MARGIN":
        if code == "draw":
            return "winning_margin_draw"
        return "opponent_winning_margin" if code.startswith(f"{opponent_side}_") else None
    return None


def _qualified_direct_selection_kind(
    market: str, code: str, line: Any, managed_side: str, managed_team_id: int,
    opponent_win_odds: Any, selected_odds: Any,
) -> str | None:
    kind = _direct_selection_kind(market, code, line, managed_side, managed_team_id)
    if not kind:
        return None
    try:
        base_odds = float(opponent_win_odds)
    except (TypeError, ValueError):
        base_odds = 0.0
    if base_odds <= OPPONENT_WIN_DIRECT_ODDS_THRESHOLD:
        return None
    if market in {"AH", "HT_AH", "SH_AH"}:
        try:
            if float(selected_odds) <= OPPONENT_WIN_DIRECT_ODDS_THRESHOLD:
                return None
        except (TypeError, ValueError):
            return None
    return kind


def integrity_snapshot(
    output: dict[str, Any], match: dict[str, Any], market: str, code: str,
    selected_odds: float | None = None, *, line: Any = None,
    managed_win_odds: float | None = None, opponent_win_odds: float | None = None,
    selection_label: str | None = None,
) -> dict[str, Any] | None:
    managed = {
        int(team["id"]): team
        for team in (output.get("managed_teams") or ([output.get("managed_team")] if output.get("managed_team") else []))
        if team and team.get("id")
    }
    home, away = match["home"], match["away"]
    managed_side = "home" if int(home["id"]) in managed else "away" if int(away["id"]) in managed else None
    if not managed_side:
        return None
    try:
        numeric_line = float(line) if line is not None else None
    except (TypeError, ValueError):
        numeric_line = None
    managed_team = home if managed_side == "home" else away
    opponent = away if managed_side == "home" else home
    managed_profile = managed_team.get("profile") or {}
    opponent_profile = opponent.get("profile") or {}
    ca_known = managed_profile.get("ca_source") == "squad" and opponent_profile.get("ca_source") == "squad"
    managed_ca = float(managed_profile.get("candidate_20_ca") or 0) if ca_known else None
    opponent_ca = float(opponent_profile.get("candidate_20_ca") or 0) if ca_known else None
    gap = round(managed_ca - opponent_ca, 2) if ca_known else None
    exemption = _monitoring_exemption(
        str(market), str(code), line, managed_side, int(managed_team["id"]),
    )
    direct_kind = _qualified_direct_selection_kind(
        str(market), str(code), numeric_line if numeric_line is not None else line,
        managed_side, int(managed_team["id"]), opponent_win_odds, selected_odds,
    )
    return {
        "fixture_key": _fixture_key(match),
        "managed_team_id": int(managed_team["id"]),
        "managed_team_name": managed_team.get("name"),
        "managed_team_address": managed_team.get("address"),
        "opponent_team_id": int(opponent["id"]),
        "opponent_team_name": opponent.get("name"),
        "managed_side": managed_side,
        "managed_ca": round(managed_ca, 2) if managed_ca is not None else None,
        "opponent_ca": round(opponent_ca, 2) if opponent_ca is not None else None,
        "ca_gap": gap,
        "selected_odds": round(float(selected_odds), 4) if selected_odds is not None else None,
        "managed_win_odds": round(float(managed_win_odds), 4) if managed_win_odds is not None else None,
        "opponent_win_odds": round(float(opponent_win_odds), 4) if opponent_win_odds is not None else None,
        "integrity_pattern": "upset_direct_selection" if direct_kind else "managed_market_monitor",
        "direct_trigger_kind": direct_kind,
        "monitoring_suspicious": _monitored_control_market(
            str(market), str(code), numeric_line if numeric_line is not None else line,
            managed_side, int(managed_team["id"]),
        ),
        "selection_label": selection_label,
        "monitoring_group": _monitoring_group(
            str(market), str(code), managed_side, int(managed_team["id"]),
        ),
        "monitoring_selection_key": _selection_key(str(code), line),
        "monitoring_exempt": exemption is not None,
        "monitoring_exemption": exemption,
    }


def _refresh_attempt_exemptions(payload: dict[str, Any]) -> bool:
    """Reclassify persisted contributions with the current monitoring rules."""
    managed_outcome_exemptions = {
        "managed_team_win", "managed_team_handicap", "managed_team_double_chance",
        "managed_team_full_time_win", "managed_team_clean_sheet",
        "managed_team_win_to_nil", "managed_team_advance", "managed_team_advance_method",
        "managed_team_winning_margin",
    }
    changed = False
    for attempt in payload.get("attempts") or []:
        attempt_changed = False
        for row in attempt.get("contributions") or []:
            try:
                selection = json.loads(str(row.get("selection_key") or ""))
                code = str(selection[0])
                line = selection[1] if len(selection) > 1 else None
            except (IndexError, TypeError, ValueError, json.JSONDecodeError):
                continue
            market = str(row.get("group") or "").split(":", 1)[0]
            managed_side = str(row.get("managed_side") or attempt.get("managed_side") or "")
            managed_team_id = int(row.get("managed_team_id") or attempt.get("managed_team_id") or 0)
            exemption = _monitoring_exemption(
                market, code, line, managed_side, managed_team_id,
            )
            if exemption is None and row.get("exemption") in managed_outcome_exemptions:
                row["exempt"] = False
                row["exemption"] = None
                attempt_changed = True
            elif exemption is not None and (
                not row.get("exempt") or row.get("exemption") != exemption
            ):
                row["exempt"] = True
                row["exemption"] = exemption
                attempt_changed = True
            suspicious = _monitored_control_market(
                market, code, line, managed_side, managed_team_id,
            )
            if bool(row.get("suspicious")) != suspicious:
                row["suspicious"] = suspicious
                attempt_changed = True
            opponent_side = "away" if managed_side == "home" else "home"
            opponent_win_odds = row.get("opponent_win_odds")
            if opponent_win_odds is None and market == "1X2" and code == opponent_side:
                opponent_win_odds = row.get("odds")
            direct_kind = _qualified_direct_selection_kind(
                market, code, line, managed_side, managed_team_id,
                opponent_win_odds, row.get("odds"),
            )
            direct_candidate = bool(direct_kind and row.get("outcome") == "won")
            if row.get("direct_trigger_kind") != direct_kind:
                row["direct_trigger_kind"] = direct_kind
                attempt_changed = True
            if bool(row.get("direct_candidate")) != direct_candidate:
                row["direct_candidate"] = direct_candidate
                attempt_changed = True
        if attempt_changed:
            _recompute_attempt(attempt)
            changed = True
    return changed


def _event_from_leg(record: dict[str, Any], leg: dict[str, Any], evaluation: dict[str, Any]) -> dict[str, Any] | None:
    evidence = leg.get("integrity")
    if not isinstance(evidence, dict) or evaluation.get("outcome") != "won":
        return None
    market = str(leg.get("market") or "")
    code = str(leg.get("selection_code") or "")
    line = leg.get("line")
    selected_odds = float(evidence.get("selected_odds") or leg.get("odds") or 0)
    opponent_win_odds = evidence.get("opponent_win_odds")
    opponent_side = "away" if evidence.get("managed_side") == "home" else "home"
    if opponent_win_odds is None and market == "1X2" and code == opponent_side:
        opponent_win_odds = selected_odds
    direct_kind = _qualified_direct_selection_kind(
        market, code, line, str(evidence.get("managed_side") or ""),
        int(evidence.get("managed_team_id") or 0), opponent_win_odds, selected_odds,
    )
    if not direct_kind:
        return None
    try:
        home_goals, away_goals = (int(value) for value in str(evaluation.get("score", "")).split("-", 1))
    except (TypeError, ValueError):
        return None
    managed_home = evidence.get("managed_side") == "home"
    managed_goals = home_goals if managed_home else away_goals
    opponent_goals = away_goals if managed_home else home_goals
    margin = opponent_goals - managed_goals
    return {
        "fixture_key": str(evidence["fixture_key"]),
        "game_date": str(evaluation.get("result_date") or leg.get("fixture_date") or record.get("game_date")),
        "fixture_date": str(leg.get("fixture_date") or evaluation.get("result_date") or ""),
        "competition_id": leg.get("competition_id"),
        "competition_name": leg.get("competition_name") or "-",
        "competition_kind": leg.get("competition_kind") or "",
        "home_id": int(leg.get("home_id") or 0),
        "away_id": int(leg.get("away_id") or 0),
        "home": leg.get("home") or "-",
        "away": leg.get("away") or "-",
        "home_goals": home_goals,
        "away_goals": away_goals,
        "managed_team_id": int(evidence.get("managed_team_id") or 0),
        "managed_team_name": evidence.get("managed_team_name") or "-",
        "managed_team_address": evidence.get("managed_team_address"),
        "opponent_team_id": int(evidence.get("opponent_team_id") or 0),
        "opponent_team_name": evidence.get("opponent_team_name") or "-",
        "managed_side": evidence.get("managed_side"),
        "managed_ca": evidence.get("managed_ca"),
        "opponent_ca": evidence.get("opponent_ca"),
        "ca_gap": evidence.get("ca_gap"),
        "selected_odds": selected_odds,
        "managed_win_odds": evidence.get("managed_win_odds"),
        "opponent_win_odds": float(opponent_win_odds),
        "integrity_pattern": "upset_direct_selection",
        "direct_trigger_kind": direct_kind,
        "market": market,
        "selection_code": code,
        "line": line,
        "monitoring_group": evidence.get("monitoring_group"),
        "selection_label": evidence.get("selection_label") or leg.get("selection") or "-",
        "half_score": evaluation.get("half_score"),
        "loss_margin": margin,
        "upset_direct_candidate": True,
        "direct_trigger_kinds": [direct_kind],
        "direct_trigger": False,
        "manager_name": str(record.get("integrity_manager_name") or "经理"),
        "weekly_salary": round(float(record.get("integrity_weekly_salary") or 0), 2),
        "bet_ids": [str(record.get("bet_id") or "")],
        "stake": round(float(record.get("stake") or 0), 2),
        "net_profit": 0.0,
    }


def _refresh_direct_trigger(event: dict[str, Any]) -> None:
    legacy_opponent_win = event.get("integrity_pattern") == "opponent_full_time_win"
    direct_kind = event.get("direct_trigger_kind") or (
        "opponent_full_time_win" if legacy_opponent_win else None
    )
    opponent_win_odds = event.get("opponent_win_odds")
    if opponent_win_odds is None and direct_kind == "opponent_full_time_win":
        opponent_win_odds = event.get("selected_odds")
    handicap_odds_qualified = bool(
        direct_kind != "opponent_handicap_win"
        or float(event.get("selected_odds") or 0) > OPPONENT_WIN_DIRECT_ODDS_THRESHOLD
    )
    event["direct_trigger"] = bool(
        (event.get("upset_direct_candidate") or legacy_opponent_win)
        and direct_kind
        and float(opponent_win_odds or 0) > OPPONENT_WIN_DIRECT_ODDS_THRESHOLD
        and handicap_odds_qualified
        and _event_display_stake(event) >= MATCH_ANOMALOUS_STAKE_MINIMUM
        and event.get("fixture_anomaly_qualified") is True
    )


def _attempt_contribution(
    record: dict[str, Any], leg: dict[str, Any], evaluation: dict[str, Any], leg_index: int,
) -> dict[str, Any] | None:
    evidence = leg.get("integrity")
    if not isinstance(evidence, dict) or not evidence.get("monitoring_group"):
        return None
    outcome = str(evaluation.get("outcome") or "")
    if outcome not in {"won", "half_won", "lost", "half_lost", "void"}:
        return None
    try:
        home_goals, away_goals = (
            int(value) for value in str(evaluation.get("score", "")).split("-", 1)
        )
    except (TypeError, ValueError):
        return None
    bet_id = str(record.get("bet_id") or "")
    stake_weight = (
        float(record.get("unit_stake") or 0)
        if record.get("type") == "system_parlay"
        else float(record.get("stake") or 0)
    )
    if stake_weight <= 0:
        stake_weight = float(record.get("stake") or 0)
    direct_candidate = _event_from_leg(record, leg, evaluation) is not None
    return {
        "id": f"{bet_id}:{leg_index}",
        "bet_id": bet_id,
        "fixture_key": str(evidence.get("fixture_key") or _fixture_key(leg)),
        "group": str(evidence["monitoring_group"]),
        "selection_key": str(evidence.get("monitoring_selection_key") or ""),
        "selection_label": str(evidence.get("selection_label") or leg.get("selection") or "-"),
        "exempt": bool(evidence.get("monitoring_exempt")),
        "exemption": evidence.get("monitoring_exemption"),
        "suspicious": bool(evidence.get("monitoring_suspicious")),
        "direct_candidate": direct_candidate,
        "direct_trigger_kind": evidence.get("direct_trigger_kind"),
        "opponent_win_odds": evidence.get("opponent_win_odds"),
        "outcome": outcome,
        "stake": round(stake_weight, 2),
        "ticket_stake": round(float(record.get("stake") or 0), 2),
        "odds": round(float(evidence.get("selected_odds") or leg.get("odds") or 0), 4),
        "profit": round(max(float(record.get("payout") or 0) - float(record.get("stake") or 0), 0), 2),
        "game_date": str(evaluation.get("result_date") or leg.get("fixture_date") or record.get("game_date") or ""),
        "fixture_date": str(leg.get("fixture_date") or evaluation.get("result_date") or ""),
        "competition_id": leg.get("competition_id"),
        "competition_name": leg.get("competition_name") or "-",
        "competition_kind": leg.get("competition_kind") or "",
        "home_id": int(leg.get("home_id") or 0),
        "away_id": int(leg.get("away_id") or 0),
        "home": leg.get("home") or "-",
        "away": leg.get("away") or "-",
        "home_goals": home_goals,
        "away_goals": away_goals,
        "half_score": evaluation.get("half_score"),
        "managed_team_id": int(evidence.get("managed_team_id") or 0),
        "managed_team_name": evidence.get("managed_team_name") or "-",
        "managed_team_address": evidence.get("managed_team_address"),
        "opponent_team_id": int(evidence.get("opponent_team_id") or 0),
        "opponent_team_name": evidence.get("opponent_team_name") or "-",
        "managed_side": evidence.get("managed_side"),
        "managed_ca": evidence.get("managed_ca"),
        "opponent_ca": evidence.get("opponent_ca"),
        "ca_gap": evidence.get("ca_gap"),
        "manager_name": str(record.get("integrity_manager_name") or "经理"),
        "weekly_salary": round(float(record.get("integrity_weekly_salary") or 0), 2),
    }


def _recompute_attempt(attempt: dict[str, Any]) -> None:
    contributions = list(attempt.get("contributions") or [])
    groups: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for row in contributions:
        groups.setdefault(str(row["group"]), {}).setdefault(
            str(row["selection_key"]), [],
        ).append(row)

    details = []
    monitored = []
    anomalous_rows: dict[str, dict[str, Any]] = {}
    for group_key, selections in sorted(groups.items()):
        ranked = sorted(
            (
                (round(sum(float(row.get("stake") or 0) for row in rows), 2), key, rows)
                for key, rows in selections.items()
            ),
            key=lambda item: (-item[0], item[1]),
        )
        largest_stake, largest_key, dominant_rows = ranked[0]
        second_stake = ranked[1][0] if len(ranked) > 1 else 0.0
        balanced_multi = len(ranked) > 1 and second_stake >= largest_stake * 0.5
        dominant = dominant_rows[0]
        dominant_odds = max((float(row.get("odds") or 0) for row in dominant_rows), default=0.0)
        detail = {
            "group": group_key,
            "selection_key": largest_key,
            "selection_label": dominant.get("selection_label") or "-",
            "largest_stake": largest_stake,
            "second_stake": second_stake,
            "option_count": len(ranked),
            "selected_odds": dominant_odds,
        }
        direct_candidate = any(row.get("direct_candidate") for row in dominant_rows)
        suspicious = any(row.get("suspicious") for row in dominant_rows)
        if balanced_multi:
            detail["status"] = "ignored_multi_choice"
        elif dominant.get("exempt") and not direct_candidate:
            detail["status"] = "ignored_whitelist"
            detail["exemption"] = dominant.get("exemption")
        else:
            if suspicious or direct_candidate:
                anomalous_rows.update({str(row.get("id") or id(row)): row for row in dominant_rows})
            if suspicious:
                outcomes = {str(row.get("outcome") or "") for row in dominant_rows}
                if outcomes.intersection({"lost", "half_lost"}):
                    detail["status"] = "miss"
                    monitored.append(("miss", dominant_rows, detail))
                elif outcomes.intersection({"won", "half_won"}):
                    detail["status"] = "hit"
                    monitored.append(("hit", dominant_rows, detail))
                else:
                    detail["status"] = "ignored_void"
            elif direct_candidate:
                detail["status"] = "direct_candidate"
            else:
                detail["status"] = "ignored_market"
        details.append(detail)

    total_fixture_stake = round(sum(float(row.get("stake") or 0) for row in contributions), 2)
    anomalous_fixture_stake = round(sum(
        float(row.get("stake") or 0) for row in anomalous_rows.values()
    ), 2)
    anomalous_share = round(
        anomalous_fixture_stake / total_fixture_stake, 6
    ) if total_fixture_stake > 0 else 0.0
    share_qualified = bool(
        anomalous_fixture_stake > 0
        and anomalous_share >= MATCH_ANOMALOUS_STAKE_SHARE_THRESHOLD
    )
    amount_qualified = anomalous_fixture_stake >= MATCH_ANOMALOUS_STAKE_MINIMUM
    fixture_qualified = share_qualified and amount_qualified
    attempt["total_fixture_stake"] = total_fixture_stake
    attempt["anomalous_fixture_stake"] = anomalous_fixture_stake
    attempt["anomalous_stake_share"] = anomalous_share
    attempt["fixture_anomaly_share_qualified"] = share_qualified
    attempt["fixture_anomaly_amount_qualified"] = amount_qualified
    attempt["fixture_anomaly_qualified"] = fixture_qualified
    if not fixture_qualified:
        for _status, _rows, detail in monitored:
            detail["market_status"] = detail["status"]
            detail["status"] = (
                "ignored_match_share" if not share_qualified else "ignored_minimum_stake"
            )
        monitored = []

    attempt["groups"] = details
    if not monitored:
        attempt["status"] = "ignored"
    elif any(status == "hit" for status, _rows, _detail in monitored):
        attempt["status"] = "hit"
    else:
        attempt["status"] = "miss"

    selected_rows = [row for _status, rows, _detail in monitored for row in rows]
    selected_details = [detail for _status, _rows, detail in monitored]
    attempt["selection_labels"] = [str(detail["selection_label"]) for detail in selected_details]
    attempt["bet_ids"] = sorted({str(row.get("bet_id") or "") for row in selected_rows if row.get("bet_id")})
    attempt["stake_by_bet"] = {
        bet_id: max(
            float(row.get("ticket_stake") or row.get("stake") or 0)
            for row in selected_rows if str(row.get("bet_id") or "") == bet_id
        )
        for bet_id in attempt["bet_ids"]
    }
    attempt["stake"] = round(sum(attempt["stake_by_bet"].values()), 2)
    attempt["profit_by_bet"] = {
        bet_id: max(float(row.get("profit") or 0) for row in selected_rows if str(row.get("bet_id") or "") == bet_id)
        for bet_id in attempt["bet_ids"]
    }
    attempt["net_profit"] = round(sum(attempt["profit_by_bet"].values()), 2)
    attempt["selected_odds"] = max((float(row.get("odds") or 0) for row in selected_rows), default=0.0)
    attempt["weekly_salary"] = max((float(row.get("weekly_salary") or 0) for row in selected_rows), default=0.0)
    attempt["updated_at"] = _now()


def _sync_event_fixture_shares(payload: dict[str, Any]) -> bool:
    changed = False
    for attempt in payload.get("attempts") or []:
        if all(field in attempt for field in (
            "fixture_anomaly_share_qualified", "fixture_anomaly_amount_qualified",
            "fixture_anomaly_qualified",
        )):
            continue
        _recompute_attempt(attempt)
        changed = True
    attempts = {
        str(row.get("fixture_key") or ""): row
        for row in payload.get("attempts") or []
        if row.get("fixture_key")
    }
    for event in payload.get("events") or []:
        if event.get("penalty_id"):
            continue
        attempt = attempts.get(str(event.get("fixture_key") or ""))
        if not attempt:
            continue
        updates = {
            "total_fixture_stake": float(attempt.get("total_fixture_stake") or 0),
            "anomalous_fixture_stake": float(attempt.get("anomalous_fixture_stake") or 0),
            "anomalous_stake_share": float(attempt.get("anomalous_stake_share") or 0),
            "fixture_anomaly_share_qualified": bool(
                attempt.get("fixture_anomaly_share_qualified")
            ),
            "fixture_anomaly_amount_qualified": bool(
                attempt.get("fixture_anomaly_amount_qualified")
            ),
            "fixture_anomaly_qualified": bool(
                attempt.get("fixture_anomaly_qualified")
            ),
            "season_key": str(attempt.get("season_key") or ""),
        }
        if any(event.get(key) != value for key, value in updates.items()):
            event.update(updates)
            changed = True
        was_direct = bool(event.get("direct_trigger"))
        _refresh_direct_trigger(event)
        changed = bool(event.get("direct_trigger")) != was_direct or changed
    return changed


def _repair_event_metadata(
    event: dict[str, Any], attempt: dict[str, Any] | None, manager_name: str,
) -> bool:
    changed = False
    if not _generic_manager_name(manager_name) and _generic_manager_name(event.get("manager_name")):
        event["manager_name"] = manager_name
        changed = True
    if not attempt:
        return changed
    if _event_display_stake(event) <= 0:
        rows = list(attempt.get("contributions") or [])
        bet_ids = {str(value) for value in event.get("bet_ids") or [] if value}
        if bet_ids:
            rows = [row for row in rows if str(row.get("bet_id") or "") in bet_ids]
        group_key = str(event.get("sequence_group") or event.get("monitoring_group") or "")
        if group_key:
            grouped = [row for row in rows if str(row.get("group") or "") == group_key]
            if grouped:
                rows = grouped
        selection_label = str(event.get("selection_label") or "")
        if selection_label:
            selected = [
                row for row in rows
                if str(row.get("selection_label") or "") == selection_label
            ]
            if selected:
                rows = selected
        stake_by_bet = {
            bet_id: max(
                float(row.get("ticket_stake") or row.get("stake") or 0)
                for row in rows if str(row.get("bet_id") or "") == bet_id
            )
            for bet_id in {str(row.get("bet_id") or "") for row in rows if row.get("bet_id")}
        }
        stake_by_bet = {key: round(value, 2) for key, value in stake_by_bet.items() if value > 0}
        if stake_by_bet:
            event["stake_by_bet"] = stake_by_bet
            event["stake"] = round(sum(stake_by_bet.values()), 2)
            changed = True
        elif float(attempt.get("anomalous_fixture_stake") or 0) > 0:
            event["stake"] = round(float(attempt["anomalous_fixture_stake"]), 2)
            changed = True
    if float(event.get("net_profit") or 0) <= 0:
        profits = event.get("profit_by_bet")
        if isinstance(profits, dict):
            profit = round(sum(max(0.0, float(value or 0)) for value in profits.values()), 2)
            if profit > 0:
                event["net_profit"] = profit
                changed = True
    return changed


def _repair_penalty_metadata(
    payload: dict[str, Any], output: dict[str, Any], manager_name: str,
) -> bool:
    resolved_name = _resolved_manager_name(output, manager_name)
    attempts = {
        str(row.get("fixture_key") or ""): row
        for row in payload.get("attempts") or []
        if row.get("fixture_key")
    }
    changed = False
    for attempt in attempts.values():
        if not _generic_manager_name(resolved_name) and _generic_manager_name(attempt.get("manager_name")):
            attempt["manager_name"] = resolved_name
            changed = True
    for event in payload.get("events") or []:
        changed = _repair_event_metadata(
            event, attempts.get(str(event.get("fixture_key") or "")), resolved_name,
        ) or changed
    for penalty in payload.get("penalties") or []:
        events = penalty.get("events") or []
        for event in events:
            changed = _repair_event_metadata(
                event, attempts.get(str(event.get("fixture_key") or "")), resolved_name,
            ) or changed
        zero_stake_events = [event for event in events if _event_display_stake(event) <= 0]
        penalty_stake_rate = float(penalty.get("stake_confiscation_rate", 0.5))
        original_penalty_stake = (
            float(penalty.get("confiscated_stake") or 0) / penalty_stake_rate
            if penalty_stake_rate > 0 else 0.0
        )
        remaining_stake = max(
            0.0,
            round(
                original_penalty_stake
                - sum(_event_display_stake(event) for event in events),
                2,
            ),
        )
        if zero_stake_events and remaining_stake > 0:
            per_event = round(remaining_stake / len(zero_stake_events), 2)
            unassigned = remaining_stake
            for index, event in enumerate(zero_stake_events):
                amount = unassigned if index == len(zero_stake_events) - 1 else per_event
                event["stake"] = round(amount, 2)
                unassigned = round(unassigned - amount, 2)
            changed = True
        zero_profit_events = [event for event in events if float(event.get("net_profit") or 0) <= 0]
        penalty_profit_rate = float(penalty.get("profit_confiscation_rate", 1.0))
        original_penalty_profit = (
            float(penalty.get("confiscated_profit") or 0) / penalty_profit_rate
            if penalty_profit_rate > 0 else 0.0
        )
        remaining_profit = max(
            0.0,
            round(
                original_penalty_profit
                - sum(max(0.0, float(event.get("net_profit") or 0)) for event in events),
                2,
            ),
        )
        if zero_profit_events and remaining_profit > 0:
            per_event = round(remaining_profit / len(zero_profit_events), 2)
            unassigned = remaining_profit
            for index, event in enumerate(zero_profit_events):
                amount = unassigned if index == len(zero_profit_events) - 1 else per_event
                event["net_profit"] = round(amount, 2)
                unassigned = round(unassigned - amount, 2)
            changed = True
        if events:
            penalty_name = next((
                str(event.get("manager_name") or "") for event in reversed(events)
                if not _generic_manager_name(event.get("manager_name"))
            ), resolved_name)
            body = _penalty_body(
                penalty_name, events, penalty, str(penalty.get("game_date") or ""),
            )
            if penalty.get("body") != body:
                penalty["body"] = body
                changed = True
    return changed


def _merge_attempt_contributions(payload: dict[str, Any], rows: list[dict[str, Any]]) -> bool:
    if not rows:
        return False
    attempts = payload.setdefault("attempts", [])
    by_fixture = {str(row.get("fixture_key") or ""): row for row in attempts}
    changed = False
    for contribution in rows:
        fixture_key = str(contribution["fixture_key"])
        attempt = by_fixture.get(fixture_key)
        if attempt is None:
            attempt = {
                "id": str(uuid.uuid4()), "fixture_key": fixture_key,
                "created_at": _now(), "penalty_id": None, "contributions": [],
            }
            for key in (
                "game_date", "fixture_date", "competition_id", "competition_name", "competition_kind",
                "home_id", "away_id", "home", "away", "home_goals", "away_goals", "half_score",
                "managed_team_id", "managed_team_name", "managed_team_address", "opponent_team_id",
                "opponent_team_name", "managed_side", "managed_ca", "opponent_ca", "ca_gap", "manager_name",
            ):
                attempt[key] = contribution.get(key)
            attempts.append(attempt)
            by_fixture[fixture_key] = attempt
        known = {str(row.get("id") or "") for row in attempt.get("contributions") or []}
        if str(contribution["id"]) in known:
            continue
        attempt.setdefault("contributions", []).append(contribution)
        _recompute_attempt(attempt)
        changed = True
    return changed


def _season_key(output: dict[str, Any], game_date: str = "") -> str:
    season_start = str(output.get("season_start") or "")
    season_end = str(output.get("season_end") or "")
    if season_start and (not game_date or not season_end or season_start <= game_date <= season_end):
        return season_start
    try:
        parsed = date.fromisoformat(str(game_date or output.get("game_date") or ""))
    except ValueError:
        return season_start or "unknown"
    start_year = parsed.year if parsed.month >= 7 else parsed.year - 1
    return f"{start_year}-07-01"


def _assign_attempt_seasons(payload: dict[str, Any], output: dict[str, Any]) -> bool:
    changed = False
    for attempt in payload.get("attempts") or []:
        key = _season_key(output, str(attempt.get("game_date") or attempt.get("fixture_date") or ""))
        if attempt.get("season_key") != key:
            attempt["season_key"] = key
            changed = True
    return changed


def _qualifying_attempts(
    payload: dict[str, Any], season_key: str | None = None,
) -> list[dict[str, Any]]:
    return sorted((
        attempt for attempt in payload.get("attempts") or []
        if attempt.get("status") in {"hit", "miss"}
        and attempt.get("fixture_anomaly_qualified")
        and not attempt.get("cycle_penalty_id")
        and (not season_key or str(attempt.get("season_key") or "") == season_key)
    ),
        key=lambda row: (str(row.get("game_date") or ""), str(row.get("fixture_key") or "")),
    )


def _sequence_pair(
    payload: dict[str, Any], current_game_date: str, season_key: str | None = None,
) -> dict[str, Any] | None:
    try:
        today = date.fromisoformat(str(current_game_date))
    except ValueError:
        return None
    attempts = _qualifying_attempts(payload, season_key)
    window = attempts[-RECENT_ATTEMPT_WINDOW:]
    hits = [attempt for attempt in window if attempt.get("status") == "hit"]
    if len(hits) < RECENT_ATTEMPT_HITS:
        return None
    try:
        latest_date = date.fromisoformat(str(window[-1].get("game_date") or ""))
    except ValueError:
        return None
    if today - latest_date >= timedelta(days=INTEGRITY_REVIEW_DELAY_DAYS):
        return {
            "attempts": window,
            "entries": [{"attempt": attempt} for attempt in hits],
            "season_key": season_key or str(window[-1].get("season_key") or ""),
        }
    return None


def _event_from_attempt(
    attempt: dict[str, Any], sequence_id: str, group_key: str | None = None,
) -> dict[str, Any]:
    managed_home = attempt.get("managed_side") == "home"
    managed_goals = int(attempt.get("home_goals") or 0) if managed_home else int(attempt.get("away_goals") or 0)
    opponent_goals = int(attempt.get("away_goals") or 0) if managed_home else int(attempt.get("home_goals") or 0)
    details = [
        row for row in attempt.get("groups") or []
        if row.get("status") == "hit" and (
            not group_key or str(row.get("group") or "") == str(group_key)
        )
    ]
    selected_keys = {
        (str(detail.get("group") or ""), str(detail.get("selection_key") or ""))
        for detail in details
    }
    selected_rows = [
        row for row in attempt.get("contributions") or []
        if (str(row.get("group") or ""), str(row.get("selection_key") or "")) in selected_keys
    ]
    bet_ids = sorted({str(row.get("bet_id") or "") for row in selected_rows if row.get("bet_id")})
    stake_by_bet = {
        bet_id: max(
            float(row.get("ticket_stake") or row.get("stake") or 0)
            for row in selected_rows if str(row.get("bet_id") or "") == bet_id
        )
        for bet_id in bet_ids
    }
    profit_by_bet = {
        bet_id: max(
            float(row.get("profit") or 0)
            for row in selected_rows if str(row.get("bet_id") or "") == bet_id
        )
        for bet_id in bet_ids
    }
    return {
        **{
            key: attempt.get(key) for key in (
                "fixture_key", "game_date", "fixture_date", "competition_id", "competition_name",
                "competition_kind", "home_id", "away_id", "home", "away", "home_goals", "away_goals",
                "managed_team_id", "managed_team_name", "managed_team_address", "opponent_team_id",
                "opponent_team_name", "managed_side", "managed_ca", "opponent_ca", "ca_gap", "half_score",
                "manager_name", "total_fixture_stake", "anomalous_fixture_stake",
                "anomalous_stake_share", "fixture_anomaly_share_qualified",
                "fixture_anomaly_amount_qualified", "fixture_anomaly_qualified",
                "season_key",
            )
        },
        "id": str(uuid.uuid4()), "created_at": _now(), "penalty_id": None,
        "review_after_game_date": (
            date.fromisoformat(str(attempt["game_date"])) + timedelta(days=INTEGRITY_REVIEW_DELAY_DAYS)
        ).isoformat(),
        "integrity_pattern": "managed_market_sequence",
        "selection_label": "、".join(
            str(detail.get("selection_label") or "-") for detail in details
        ) or "-",
        "sequence_id": sequence_id,
        "sequence_group": group_key or "fixture",
        "sequence_qualified": True,
        "bet_ids": bet_ids,
        "stake_by_bet": stake_by_bet,
        "stake": round(sum(stake_by_bet.values()), 2),
        "profit_by_bet": profit_by_bet,
        "net_profit": round(sum(profit_by_bet.values()), 2),
        "selected_odds": max((float(row.get("odds") or 0) for row in selected_rows), default=0.0),
        "weekly_salary": max((float(row.get("weekly_salary") or 0) for row in selected_rows), default=0.0),
        "loss_margin": opponent_goals - managed_goals,
        "direct_trigger": False,
    }


def _confiscation_for_events(
    events: list[dict[str, Any]], prior_penalties: list[dict[str, Any]] | None = None,
    *, profit_rate: float = 1.0, stake_rate: float = 0.5,
) -> dict[str, float]:
    already_confiscated = {
        str(bet_id)
        for penalty in prior_penalties or []
        for event in penalty.get("events") or []
        for bet_id in event.get("bet_ids") or []
    }
    profit_by_bet: dict[str, float] = {}
    stake_by_bet: dict[str, float] = {}
    legacy_profit = 0.0
    legacy_stake = 0.0
    for event in events:
        contributions = event.get("profit_by_bet")
        if isinstance(contributions, dict):
            for bet_id, amount in contributions.items():
                if str(bet_id) not in already_confiscated:
                    profit_by_bet[str(bet_id)] = max(profit_by_bet.get(str(bet_id), 0.0), float(amount or 0))
        else:
            legacy_profit += float(event.get("net_profit") or 0)
        stakes = event.get("stake_by_bet")
        if isinstance(stakes, dict):
            recorded_stake = False
            for bet_id, amount in stakes.items():
                if str(bet_id) not in already_confiscated:
                    value = float(amount or 0)
                    stake_by_bet[str(bet_id)] = max(stake_by_bet.get(str(bet_id), 0.0), value)
                    recorded_stake = recorded_stake or value > 0
            stake_references = {
                str(value) for value in (event.get("bet_ids") or stakes.keys()) if value
            }
            if not recorded_stake and (
                not stake_references or any(value not in already_confiscated for value in stake_references)
            ):
                legacy_stake += _event_display_stake(event)
        elif not isinstance(contributions, dict):
            legacy_stake += _event_display_stake(event)
    eligible_profit = round(sum(profit_by_bet.values()) + legacy_profit, 2)
    eligible_stake = round(sum(stake_by_bet.values()) + legacy_stake, 2)
    profit = round(eligible_profit * profit_rate, 2)
    stake = round(eligible_stake * stake_rate, 2)
    return {
        "eligible_profit": eligible_profit, "eligible_stake": eligible_stake,
        "profit": profit, "stake": stake, "total": round(profit + stake, 2),
    }


def _current_team_ca(output: dict[str, Any]) -> dict[int, float]:
    values: dict[int, float] = {}
    for match in output.get("matches", []):
        for team in (match.get("home") or {}, match.get("away") or {}):
            team_id = int(team.get("id") or 0)
            profile = team.get("profile") or {}
            if (
                team_id > 0
                and profile.get("ca_source") == "squad"
                and profile.get("candidate_20_ca") is not None
            ):
                values[team_id] = float(profile["candidate_20_ca"])
    return values


def audit_legacy_bets(
    output: dict[str, Any], manager_name: str, gross_weekly: float,
) -> dict[str, Any] | None:
    """Assess settled V1.5-era bets once using CA read during this startup."""
    records = legacy_integrity_bet_snapshot(LEGACY_AUDIT_VERSION)
    if not records:
        return None
    managed = {
        int(team.get("id") or 0): team
        for team in (output.get("managed_teams") or ([output.get("managed_team")] if output.get("managed_team") else []))
        if team and int(team.get("id") or 0) > 0
    }
    for option in output.get("manager_options") or []:
        for team in option.get("teams") or []:
            team_id = int(team.get("id") or 0)
            if team_id > 0:
                managed.setdefault(team_id, team)
    current_ca = _current_team_ca(output)
    evidence_by_bet: dict[str, list[dict[str, Any] | None]] = {}
    for record in records:
        bet_id = str(record.get("bet_id") or "")
        if not bet_id:
            continue
        legs = record.get("legs") or [record]
        if record.get("legacy_integrity_audit_version") == LEGACY_AUDIT_VERSION:
            evidence_by_bet[bet_id] = [leg.get("integrity") for leg in legs]
            continue
        evidence_rows: list[dict[str, Any] | None] = []
        for leg in legs:
            market = str(leg.get("market") or "")
            code = str(leg.get("selection_code") or "")
            home_id = int(leg.get("home_id") or 0)
            away_id = int(leg.get("away_id") or 0)
            managed_side = (
                "home" if home_id in managed and away_id not in managed
                else "away" if away_id in managed and home_id not in managed
                else None
            )
            if market != "1X2" or code not in {"home", "away"} or not managed_side or code == managed_side:
                evidence_rows.append(None)
                continue
            managed_id = home_id if managed_side == "home" else away_id
            opponent_id = away_id if managed_side == "home" else home_id
            managed_ca = current_ca.get(managed_id)
            opponent_ca = current_ca.get(opponent_id)
            ca_known = managed_ca is not None and opponent_ca is not None
            evidence_rows.append({
                "fixture_key": _fixture_key(leg),
                "managed_team_id": managed_id,
                "managed_team_name": leg.get("home") if managed_side == "home" else leg.get("away"),
                "opponent_team_id": opponent_id,
                "opponent_team_name": leg.get("away") if managed_side == "home" else leg.get("home"),
                "managed_side": managed_side,
                "managed_ca": round(managed_ca, 2) if ca_known else None,
                "opponent_ca": round(opponent_ca, 2) if ca_known else None,
                "ca_gap": round(managed_ca - opponent_ca, 2) if ca_known else None,
                "selected_odds": round(float(leg.get("odds") or 0), 4),
                "ca_reference": LEGACY_AUDIT_VERSION,
            })
        evidence_by_bet[bet_id] = evidence_rows
    identifiers = attach_legacy_integrity_evidence(
        LEGACY_AUDIT_VERSION, evidence_by_bet, manager_name, gross_weekly,
    )
    return process_settled_bets(identifiers, output, manager_name, gross_weekly)


def _penalty_body(manager_name: str, events: list[dict[str, Any]], financial: dict[str, Any], game_date: str) -> str:
    sections = []
    for index, event in enumerate(events, 1):
        sequence_pattern = event.get("integrity_pattern") == "managed_market_sequence"
        bet_description = str(event.get("selection_label") or "-")
        result_description = (
            f"半场 {event.get('half_score') or '—'}，全场 {event['home_goals']}-{event['away_goals']}，上述精确预测全部命中"
            if sequence_pattern else
            f"全场 {event['home_goals']}-{event['away_goals']}，上述异常选项命中"
        )
        sections.append(
            f"【可疑比赛{('一', '二', '三')[index - 1] if index <= 3 else index}】\n"
            f"比赛日期：{event['game_date']}\n"
            f"比赛：{event['home']} {event['home_goals']}-{event['away_goals']} {event['away']}\n"
            f"赛事：{event['competition_name']}\n\n"
            f"投注内容：{bet_description}\n"
            f"投注金额：£{_event_display_stake(event):,.2f}\n\n"
            f"最终结果：{result_description}\n"
            f"可疑投注净盈利：£{float(event['net_profit']):,.2f}"
        )
    match_text = "\n\n".join(sections)
    singular = len(events) == 1
    offense_number = int(financial.get("season_offense_number") or 2)
    first_offense = offense_number == 1
    profit_rate = float(financial.get("profit_confiscation_rate", 1.0))
    stake_rate = float(financial.get("stake_confiscation_rate", 0.5))
    salary_weeks = int(financial.get(
        "salary_weeks", 4 if float(financial.get("standard_fine") or 0) > 0 else 0,
    ))
    blocked_match_count = int(financial.get("blocked_match_count") or 2)
    fourth_clauses = []
    if not first_offense and any(event.get("competition_kind") == "league" for event in events):
        fourth_clauses.append("扣除您执教俱乐部当前联赛积分3分")
    if not first_offense and any(event.get("competition_kind") in {"league", "cup"} for event in events):
        fourth_clauses.append("降低您执教俱乐部声望100点")
    sporting_ordinal = "四" if salary_weeks > 0 else "三"
    fourth = f"{sporting_ordinal}、{'；'.join(fourth_clauses)}。" if fourth_clauses else ""
    effective_notice = "本决定全部处罚即时生效。"
    confiscated_profit = float(financial.get("confiscated_profit", financial["confiscated"]))
    confiscated_stake = float(financial.get("confiscated_stake", 0))
    confiscation_requested = float(financial.get("confiscation_requested", financial["confiscated"]))
    profit_clause = f"没收相关异常投注净盈利的{profit_rate * 100:g}% £{confiscated_profit:,.2f}"
    if stake_rate > 0:
        profit_clause += (
            f"，并追加没收异常投注本金的{stake_rate * 100:g}% "
            f"£{confiscated_stake:,.2f}"
        )
    salary_clause = (
        f"三、处以相当于{salary_weeks}周工资的罚款，标准罚款金额为 £{float(financial['standard_fine']):,.2f}。"
        f"本次实际扣除 £{float(financial['actual_fine']):,.2f}。\n\n"
        if salary_weeks > 0 or float(financial.get("standard_fine") or 0) > 0 else ""
    )
    return (
        f"尊敬的{manager_name or '经理'}：\n\n"
        "足协纪律委员会在例行比赛诚信审查中，发现有异常投注行为。\n\n"
        f"经核查，以下比赛被认定为可疑比赛：\n\n{match_text}\n\n"
        "上述行为已达到异常投注处罚标准。足协纪律委员会现作出如下决定：\n\n"
        f"一、{profit_clause}，应没收合计 £{confiscation_requested:,.2f}。本次实际扣除 £{float(financial['confiscated']):,.2f}。\n\n"
        f"二、取消您执教球队未来{blocked_match_count}场比赛盘口。\n\n"
        f"{salary_clause}"
        f"{fourth}"
        "\n\n"
        f"{effective_notice}盘口停用将在执教球队完成{blocked_match_count}场正式比赛后自动解除。\n\n"
        "比赛诚信是足球竞赛的基本原则。任何利用执教身份参与针对本队的异常投注行为，都将受到持续审查。再次发生类似行为，足协纪律委员会可能采取更严厉的处罚措施。\n\n"
        f"足协纪律委员会\n{game_date}"
    )


def _upcoming_managed_matches(output: dict[str, Any], team_ids: set[int], excluded: set[str]) -> list[dict[str, Any]]:
    rows = []
    for match in output.get("matches", []):
        if int(match["home"]["id"]) not in team_ids and int(match["away"]["id"]) not in team_ids:
            continue
        key = _fixture_key(match)
        if key in excluded:
            continue
        rows.append({
            "fixture_key": key, "fixture_date": match.get("fixture_date"),
            "competition_id": match.get("competition_id"),
            "home_id": int(match["home"]["id"]), "away_id": int(match["away"]["id"]),
            "home": match["home"].get("name"), "away": match["away"].get("name"),
            "completed": False,
        })
    return sorted(rows, key=lambda row: (str(row["fixture_date"]), row["fixture_key"]))


def _sync_blocks(payload: dict[str, Any], output: dict[str, Any]) -> bool:
    result_keys = {
        _fixture_key(item)
        for item in [*output.get("season_results", []), *output.get("settlement_results", [])]
    }
    changed = False
    reserved: set[str] = set()
    for penalty in payload["penalties"]:
        blocks = penalty.setdefault("blocked_fixtures", [])
        target_count = max(0, int(penalty.get("blocked_match_count") or 2))
        for block in blocks:
            if not block.get("completed") and block.get("fixture_key") in result_keys:
                block["completed"] = True
                changed = True
        if len(blocks) >= target_count:
            reserved.update(str(row.get("fixture_key") or "") for row in blocks)
            continue
        team_ids = {int(value) for value in penalty.get("managed_team_ids", []) if int(value) > 0}
        excluded = set(reserved)
        excluded.update(str(row.get("fixture_key")) for row in blocks)
        excluded.update(str(event.get("fixture_key")) for event in penalty.get("events", []))
        for match in _upcoming_managed_matches(output, team_ids, excluded):
            blocks.append(match)
            excluded.add(match["fixture_key"])
            reserved.add(match["fixture_key"])
            changed = True
            if len(blocks) >= target_count:
                break
        reserved.update(str(row.get("fixture_key") or "") for row in blocks)
    return changed


def _reviewable_events(payload: dict[str, Any], current_game_date: str) -> list[dict[str, Any]]:
    """Return unpenalized events whose next-game-day review delay has elapsed."""
    if not current_game_date:
        return []
    try:
        today = date.fromisoformat(current_game_date)
    except ValueError:
        return []
    reviewable = []
    for event in payload.get("events", []):
        if event.get("penalty_id"):
            continue
        try:
            event_date = date.fromisoformat(str(event.get("game_date") or ""))
        except ValueError:
            continue
        if today - event_date >= timedelta(days=INTEGRITY_REVIEW_DELAY_DAYS):
            reviewable.append(event)
    return reviewable


def _season_offense_number(
    payload: dict[str, Any], output: dict[str, Any], season_key: str,
) -> int:
    season_start = str(output.get("season_start") or season_key)
    season_end = str(output.get("season_end") or "")
    if season_start and not season_end:
        try:
            start = date.fromisoformat(season_start)
            season_end = date(start.year + 1, 6, 30).isoformat()
        except ValueError:
            pass
    count = 0
    for penalty in payload.get("penalties") or []:
        recorded_key = str(penalty.get("season_key") or "")
        if recorded_key:
            count += int(recorded_key == season_key)
            continue
        penalty_date = str(penalty.get("game_date") or "")
        if season_start and penalty_date >= season_start and (
            not season_end or penalty_date <= season_end
        ):
            count += 1
    return count + 1


def process_settled_bets(settled_bet_ids: list[str], output: dict[str, Any], manager_name: str, gross_weekly: float) -> dict[str, Any] | None:
    identifiers = {str(value) for value in settled_bet_ids if value}
    with _LOCK:
        records = integrity_bet_snapshot(identifiers)
        payload = _load_state()
        existing = {str(event.get("fixture_key")): event for event in payload["events"]}
        new_events: list[dict[str, Any]] = []
        attempt_contributions: list[dict[str, Any]] = []
        assessments: dict[str, list[str]] = {}
        for record in records:
            if str(record.get("bet_id")) not in identifiers or record.get("integrity_assessed"):
                continue
            legs = record.get("legs") or [record]
            evaluations = record.get("settlement") or []
            assessment_keys: set[str] = set()
            for leg_index, (leg, evaluation) in enumerate(zip(legs, evaluations)):
                contribution = _attempt_contribution(record, leg, evaluation, leg_index)
                if contribution is not None:
                    attempt_contributions.append(contribution)
                    assessment_keys.add(str(contribution["fixture_key"]))
            candidates = [
                event for leg, evaluation in zip(legs, evaluations)
                if (event := _event_from_leg(record, leg, evaluation)) is not None
            ]
            unique_candidates: dict[str, dict[str, Any]] = {}
            for candidate in candidates:
                fixture_key = str(candidate["fixture_key"])
                previous = unique_candidates.get(fixture_key)
                if previous is None:
                    unique_candidates[fixture_key] = candidate
                    continue
                labels = list(previous.get("direct_selection_labels") or [])
                label = str(candidate.get("selection_label") or "-")
                if label not in labels:
                    labels.append(label)
                previous["direct_selection_labels"] = labels
                previous["selection_label"] = "；".join(labels)
                kinds = list(previous.get("direct_trigger_kinds") or [])
                kinds.extend(
                    kind for kind in candidate.get("direct_trigger_kinds") or []
                    if kind not in kinds
                )
                previous["direct_trigger_kinds"] = kinds
                previous["selected_odds"] = max(
                    float(previous.get("selected_odds") or 0),
                    float(candidate.get("selected_odds") or 0),
                )
            profit = round(max(float(record.get("payout") or 0) - float(record.get("stake") or 0), 0), 2)
            for event in unique_candidates.values():
                event["net_profit"] = profit
                event["profit_by_bet"] = {str(record["bet_id"]): profit}
                event["stake_by_bet"] = {str(record["bet_id"]): float(record.get("stake") or 0)}
                _refresh_direct_trigger(event)
                current = existing.get(event["fixture_key"])
                if current:
                    was_direct = bool(current.get("direct_trigger"))
                    current.setdefault("bet_ids", [])
                    if record["bet_id"] not in current["bet_ids"]:
                        current["bet_ids"].append(record["bet_id"])
                        current["stake"] = round(float(current.get("stake") or 0) + event["stake"], 2)
                        current["net_profit"] = round(float(current.get("net_profit") or 0) + profit, 2)
                        if isinstance(current.get("profit_by_bet"), dict):
                            current["profit_by_bet"][str(record["bet_id"])] = profit
                        if isinstance(current.get("stake_by_bet"), dict):
                            current["stake_by_bet"][str(record["bet_id"])] = float(record.get("stake") or 0)
                        current["selected_odds"] = max(
                            float(current.get("selected_odds") or 0), float(event.get("selected_odds") or 0),
                        )
                        labels = list(current.get("direct_selection_labels") or [])
                        labels.extend(
                            label for label in event.get("direct_selection_labels") or [event.get("selection_label")]
                            if label and label not in labels
                        )
                        current["direct_selection_labels"] = labels
                        current["selection_label"] = "；".join(labels)
                        current["upset_direct_candidate"] = True
                        kinds = list(current.get("direct_trigger_kinds") or [])
                        kinds.extend(
                            kind for kind in event.get("direct_trigger_kinds") or []
                            if kind not in kinds
                        )
                        current["direct_trigger_kinds"] = kinds
                        _refresh_direct_trigger(current)
                    if (
                        not was_direct and current.get("direct_trigger") and not current.get("penalty_id")
                        and all(candidate is not current for candidate in new_events)
                    ):
                        new_events.append(current)
                else:
                    event.update({
                        "id": str(uuid.uuid4()), "created_at": _now(), "penalty_id": None,
                        "review_after_game_date": (
                            date.fromisoformat(str(event["game_date"])) + timedelta(days=INTEGRITY_REVIEW_DELAY_DAYS)
                        ).isoformat(),
                    })
                    payload["events"].append(event)
                    existing[event["fixture_key"]] = event
                    new_events.append(event)
            assessment_keys.update(str(value) for value in unique_candidates)
            assessments[str(record["bet_id"])] = sorted(assessment_keys)
        attempts_changed = _refresh_attempt_exemptions(payload)
        attempts_changed = _merge_attempt_contributions(payload, attempt_contributions) or attempts_changed
        attempts_changed = _assign_attempt_seasons(payload, output) or attempts_changed
        attempts_changed = _sync_event_fixture_shares(payload) or attempts_changed
        metadata_changed = _repair_penalty_metadata(payload, output, manager_name)
        mark_integrity_bets_assessed(assessments)

        trigger_events: list[dict[str, Any]] = []
        consumed_attempts: list[dict[str, Any]] = []
        direct_triggered = False
        current_season_key = _season_key(output, str(output.get("game_date") or ""))
        reviewable_events = _reviewable_events(payload, str(output.get("game_date") or ""))
        direct = next((
            event for event in reviewable_events
            if event.get("direct_trigger")
            and str(event.get("season_key") or current_season_key) == current_season_key
        ), None)
        if direct:
            trigger_events = [direct]
            direct_triggered = True
            direct_fixture_attempts = [
                attempt for attempt in payload.get("attempts") or []
                if str(attempt.get("fixture_key") or "") == str(direct.get("fixture_key") or "")
            ]
            prior_window = [
                attempt for attempt in _qualifying_attempts(payload, current_season_key)
                if str(attempt.get("game_date") or "") <= str(direct.get("game_date") or "")
            ][-RECENT_ATTEMPT_WINDOW:]
            consumed_attempts = list({
                str(attempt.get("id") or attempt.get("fixture_key")): attempt
                for attempt in [*prior_window, *direct_fixture_attempts]
            }.values())
        if not trigger_events:
            sequence = _sequence_pair(
                payload, str(output.get("game_date") or ""), current_season_key,
            )
            if sequence:
                sequence_id = str(uuid.uuid4())
                trigger_events = [
                    _event_from_attempt(entry["attempt"], sequence_id)
                    for entry in sequence["entries"]
                ]
                consumed_attempts = list(sequence["attempts"])
                payload["events"].extend(trigger_events)
                new_events.extend(trigger_events)
        if not trigger_events:
            if _sync_blocks(payload, output) or new_events or attempts_changed or metadata_changed:
                _save_state(payload)
            return None

        offense_number = _season_offense_number(
            payload, output, current_season_key,
        )
        first_offense = offense_number == 1
        profit_rate = 0.5
        stake_rate = 0.0
        salary_weeks = 0
        blocked_match_count = 1 if first_offense else 2
        confiscation = _confiscation_for_events(
            trigger_events, payload.get("penalties"),
            profit_rate=profit_rate, stake_rate=stake_rate,
        )
        salary_basis = max((float(event.get("weekly_salary") or 0) for event in trigger_events), default=0.0)
        if salary_basis <= 0:
            salary_basis = max(0.0, gross_weekly)
        financial = collect_match_integrity_penalty(
            confiscation["total"], 0.0,
            maximum_total=confiscation["eligible_profit"],
        )
        financial.update({
            "confiscated_profit": confiscation["profit"],
            "confiscated_stake": confiscation["stake"],
            "profit_confiscation_rate": profit_rate,
            "stake_confiscation_rate": stake_rate,
            "salary_weeks": salary_weeks,
        })
        penalty_id = str(uuid.uuid4())
        managed_team_ids = sorted({int(event["managed_team_id"]) for event in trigger_events})
        evidence_game_date = max(str(event["game_date"]) for event in trigger_events)
        game_date = str(output.get("game_date") or evidence_game_date)
        try:
            date.fromisoformat(game_date)
        except ValueError:
            game_date = evidence_game_date
        penalty = {
            "id": penalty_id, "created_at": _now(), "game_date": game_date,
            "acknowledged": False, "direct_trigger": direct_triggered,
            "events": [dict(event) for event in trigger_events],
            "managed_team_ids": managed_team_ids, "blocked_fixtures": [],
            "season_key": current_season_key, "season_offense_number": offense_number,
            "blocked_match_count": blocked_match_count,
            **financial,
        }
        fourth_kinds = sorted({
            str(event.get("competition_kind") or "")
            for event in trigger_events
            if event.get("competition_kind") in {"league", "cup"}
        })
        if fourth_kinds and not first_offense:
            penalty["fourth_penalty_kinds"] = fourth_kinds
            penalty["fourth_effective"] = True
            penalty["reputation_delta"] = -100
        penalty["title"] = "足协纪律委员会：关于异常投注行为的处罚决定"
        penalty_manager = _resolved_manager_name(
            output, str(trigger_events[-1].get("manager_name") or manager_name or ""),
        )
        penalty["body"] = _penalty_body(penalty_manager, trigger_events, penalty, game_date)
        for event in trigger_events:
            event["penalty_id"] = penalty_id
        for attempt in consumed_attempts:
            attempt["cycle_penalty_id"] = penalty_id
            attempt["cycle_consumed_at"] = _now()
        payload["penalties"].append(penalty)
        _sync_blocks(payload, output)
        _save_state(payload)
        return penalty


def enforce_fourth_penalties(
    output: dict[str, Any],
    *,
    apply_points: Callable[[dict[str, Any], dict[str, Any]], Any],
    apply_reputation: Callable[[int, Any, int], Any],
) -> list[dict[str, Any]]:
    """Apply second-and-later offense sporting penalties once per effect."""
    results: list[dict[str, Any]] = []
    with _LOCK:
        payload = _load_state()
        for penalty in payload.get("penalties", []):
            if int(penalty.get("season_offense_number") or 0) == 1:
                continue
            if not penalty.get("fourth_effective") or penalty.get("fourth_waived"):
                continue
            applied = penalty.setdefault("fourth_applied", {
                "league_points": [], "team_reputation": [],
            })
            applied.setdefault("league_points", [])
            applied.setdefault(
                "team_reputation", list(applied.get("cup_reputation") or []),
            )
            events = penalty.get("events") or []
            for event in events:
                team_id = int(event.get("managed_team_id") or 0)
                kind = str(event.get("competition_kind") or "")
                if team_id <= 0:
                    continue
                if kind == "league":
                    competition_id = int(event.get("competition_id") or 0)
                    effect_key = f"{team_id}:{competition_id}"
                    if effect_key in applied["league_points"]:
                        continue
                    try:
                        apply_points(output, {
                            "competition_id": competition_id,
                            "team_id": team_id,
                            "delta": -3,
                            "reason": "异常投注处罚",
                            "source_id": (
                                f"match_integrity:{penalty.get('id')}:"
                                f"{effect_key}:league_points"
                            ),
                            "legacy_source_created_after": str(
                                penalty.get("created_at") or ""
                            ),
                            "legacy_source_game_date": str(
                                penalty.get("game_date") or ""
                            ),
                        })
                    except Exception as error:
                        results.append({"penalty_id": penalty.get("id"), "effect": "league_points", "error": str(error)})
                        continue
                    applied["league_points"].append(effect_key)
                    _save_state(payload)
                    results.append({"penalty_id": penalty.get("id"), "effect": "league_points", "applied": True})
            reputation_events = {
                int(event.get("managed_team_id") or 0): event
                for event in events
                if event.get("competition_kind") in {"league", "cup"}
                and int(event.get("managed_team_id") or 0) > 0
            }
            for team_id, event in reputation_events.items():
                effect_key = str(team_id)
                if effect_key in applied["team_reputation"]:
                    continue
                team_address = event.get("managed_team_address")
                if not team_address:
                    for managed in output.get("managed_teams") or []:
                        if int(managed.get("id") or 0) == team_id:
                            team_address = managed.get("address")
                            break
                try:
                    apply_reputation(
                        team_id, team_address, int(penalty.get("reputation_delta") or -100),
                    )
                except Exception as error:
                    results.append({
                        "penalty_id": penalty.get("id"),
                        "effect": "team_reputation", "error": str(error),
                    })
                    continue
                applied["team_reputation"].append(effect_key)
                _save_state(payload)
                results.append({
                    "penalty_id": penalty.get("id"),
                    "effect": "team_reputation", "applied": True,
                })
            expected_league = {
                f"{int(event.get('managed_team_id') or 0)}:{int(event.get('competition_id') or 0)}"
                for event in events if event.get("competition_kind") == "league"
            }
            expected_reputation = {
                str(int(event.get("managed_team_id") or 0))
                for event in events if event.get("competition_kind") in {"league", "cup"}
            }
            if (
                expected_league.issubset(set(applied["league_points"]))
                and expected_reputation.issubset(set(applied["team_reputation"]))
            ):
                penalty["fourth_applied_at"] = penalty.get("fourth_applied_at") or _now()
                _save_state(payload)
    return results


def public_integrity_status(output: dict[str, Any]) -> dict[str, Any]:
    # Review recorded events lazily as the FM game date advances, even when no
    # new bet settled on the next game day.
    process_settled_bets([], output, "", 0.0)
    with _LOCK:
        payload = _load_state()
        if _sync_blocks(payload, output):
            _save_state(payload)
        notice = next((row for row in reversed(payload["penalties"]) if not row.get("acknowledged")), None)
        active_blocks = [
            dict(block)
            for penalty in payload["penalties"]
            for block in penalty.get("blocked_fixtures", [])
            if not block.get("completed")
        ]
        return {
            "notice": dict(notice) if notice else None,
            "blocked_fixtures": active_blocks,
            "remaining_matches": len(active_blocks),
            "_mail_notices": [dict(row) for row in payload["penalties"]],
        }


def assert_bets_allowed(legs: list[dict[str, Any]], output: dict[str, Any]) -> None:
    status = public_integrity_status(output)
    blocked = {str(row.get("fixture_key")) for row in status["blocked_fixtures"]}
    if any(_fixture_key(leg) in blocked for leg in legs):
        raise ValueError("足协处罚生效中：相关比赛暂停接受注单")


def acknowledge_notice(notice_id: str) -> dict[str, Any]:
    with _LOCK:
        payload = _load_state()
        notice = next((row for row in payload["penalties"] if row.get("id") == notice_id), None)
        if not notice:
            raise ValueError("足协处罚通知不存在")
        notice["acknowledged"] = True
        notice["acknowledged_at"] = _now()
        _save_state(payload)
        return {"acknowledged": True, "notice": dict(notice)}
