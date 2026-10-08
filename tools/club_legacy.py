from __future__ import annotations

import hashlib
import json
import threading
from copy import deepcopy
from datetime import date, datetime
from typing import Any

from tools.account_store import load_document, save_document


SCHEMA_VERSION = 4
FACTS_KEY = "club_legacy"
PREFERENCES_KEY = "club_hall_of_fame_preferences"
TIERS = {"honoree", "icon", "legend"}
WORLD_WATCH_TEAM_ID = -1
BEST_TEAM_FORMATIONS = {
    "4231": {"name": "4-2-3-1", "slots": {
        "gk": "GK", "dl": "DL", "dc1": "DC", "dc2": "DC", "dr": "DR",
        "dm1": "DM", "dm2": "DM", "aml": "AML", "amc": "AMC", "amr": "AMR", "st": "ST",
    }},
    "433": {"name": "4-3-3", "slots": {
        "gk": "GK", "dl": "DL", "dc1": "DC", "dc2": "DC", "dr": "DR",
        "mc1": "MC", "dm": "DM", "mc2": "MC", "aml": "AML", "st": "ST", "amr": "AMR",
    }},
    "442": {"name": "4-4-2", "slots": {
        "gk": "GK", "dl": "DL", "dc1": "DC", "dc2": "DC", "dr": "DR",
        "ml": "ML", "mc1": "MC", "mc2": "MC", "mr": "MR", "st1": "ST", "st2": "ST",
    }},
    "3421": {"name": "3-4-2-1", "slots": {
        "gk": "GK", "dc1": "DC", "dc2": "DC", "dc3": "DC", "wbl": "WBL",
        "mc1": "MC", "mc2": "MC", "wbr": "WBR", "amc1": "AMC", "amc2": "AMC", "st": "ST",
    }},
    "352": {"name": "3-5-2", "slots": {
        "gk": "GK", "dc1": "DC", "dc2": "DC", "dc3": "DC", "wbl": "WBL",
        "mc1": "MC", "dm": "DM", "mc2": "MC", "wbr": "WBR", "st1": "ST", "st2": "ST",
    }},
    "4312": {"name": "4-3-1-2", "slots": {
        "gk": "GK", "dl": "DL", "dc1": "DC", "dc2": "DC", "dr": "DR",
        "mc1": "MC", "dm": "DM", "mc2": "MC", "amc": "AMC", "st1": "ST", "st2": "ST",
    }},
}
RECORD_INTEGER_FIELDS = {
    "appearances", "goals", "assists", "transfer_in_fee", "transfer_out_fee",
}
RECORD_TEXT_FIELDS = {"honors_text", "post_departure_text"}
MATCH_MOMENT_TAGS = {
    "进球", "助攻", "关键球", "制胜球", "扳平球", "全场最佳",
    "关键传球", "关键防守", "关键扑救", "首秀", "里程碑",
    "世界波", "倒钩", "凌空抽射", "任意球破门", "远射破门", "头球破门",
    "单骑闯关", "游龙", "绝妙助攻", "制胜助攻", "关键解围", "门线救险",
    "关键扑点", "关键封堵", "逆转进球", "造点",
    # Added by the native/scoreline late-goal classifier.  They are not
    # user-required checkboxes, but are valid persisted labels for automatic
    # match-moment enrichment.
    "绝杀球", "绝平球",
}
MATCH_MOMENT_MAX = 500
_LOCK = threading.RLock()


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _empty_facts() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "updated_at": None,
        "clubs": {},
    }


def _empty_preferences() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "updated_at": None,
        "clubs": {},
        "best_teams": {},
    }


def _load_facts(career_id: str) -> dict[str, Any]:
    payload = load_document(FACTS_KEY, None, career_id)
    if not isinstance(payload, dict):
        return _empty_facts()
    payload.setdefault("clubs", {})
    payload["schema_version"] = SCHEMA_VERSION
    _backfill_season_snapshots(payload)
    return payload


def _load_preferences(scope_id: str) -> dict[str, Any]:
    payload = load_document(PREFERENCES_KEY, None, scope_id)
    if not isinstance(payload, dict):
        return _empty_preferences()
    payload.setdefault("clubs", {})
    payload.setdefault("best_teams", {})
    payload["schema_version"] = SCHEMA_VERSION
    return payload


def _strip_addresses(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _strip_addresses(item)
            for key, item in value.items()
            if "address" not in str(key).casefold()
        }
    if isinstance(value, list):
        return [_strip_addresses(item) for item in value]
    return deepcopy(value)


def _player_snapshot(player: dict[str, Any], team: dict[str, Any], game_date: str) -> dict[str, Any]:
    fields = (
        "id", "name", "full_name", "first_name", "last_name", "common_name",
        "nationality_id", "nationality", "nationality_code", "date_of_birth", "age",
        "height_cm", "weight_kg", "positions", "primary_positions", "position_ratings",
        "preferred_foot", "ca", "pa", "home_reputation", "current_reputation",
        "world_reputation", "international_reputation", "fitness", "sharpness",
        "fatigue", "morale", "availability", "is_loaned_out", "loan",
        "player_contract", "transfer", "retirement", "career_status", "shirt_number",
        "asking_price", "market_value", "market_value_note", "season_stats",
        "career_stats",
        "international_stats", "international_appearances", "international_goals",
        "u21_international_appearances", "u21_international_goals",
        "previous_club_state", "previous_club_id", "previous_club_name",
        "attributes", "hidden_attributes", "training_ca",
    )
    snapshot = {
        key: _strip_addresses(player.get(key))
        for key in fields if key in player
    }
    snapshot.update({
        "id": int(player.get("id") or 0),
        "name": str(player.get("name") or player.get("full_name") or player.get("id") or "未知球员"),
        "observed_team_id": int(team.get("id") or 0),
        "observed_team_name": str(team.get("name") or team.get("id") or "未知俱乐部"),
        "observed_game_date": str(game_date or ""),
    })
    return snapshot


def _snapshot_shirt_number(snapshot: dict[str, Any]) -> int | None:
    raw = snapshot.get("shirt_number")
    if raw is None:
        raw = (snapshot.get("player_contract") or {}).get("squad_number")
    try:
        number = int(raw)
    except (TypeError, ValueError):
        return None
    return number if 1 <= number <= 99 else None


def _snapshot_asking_price(snapshot: dict[str, Any]) -> int | None:
    raw = snapshot.get("asking_price")
    if raw is None:
        raw = (snapshot.get("transfer") or {}).get("asking_price")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


def _history_date(raw: Any) -> date | None:
    try:
        return date.fromisoformat(str(raw or "")[:10])
    except (TypeError, ValueError):
        return None


def club_legacy_season_identity(
    game_date: str, season_start: str = "", season_end: str = "",
) -> dict[str, Any]:
    """Return a stable club-season key, preferring the runtime season window."""
    observed = _history_date(game_date)
    start = _history_date(season_start)
    end = _history_date(season_end)
    inferred = not bool(start and end and (not observed or start <= observed <= end))
    if inferred:
        if observed is None:
            return {}
        start_year = observed.year if observed.month >= 7 else observed.year - 1
        start = date(start_year, 7, 1)
        end = date(start_year + 1, 6, 30)
    assert start is not None and end is not None
    start_year = start.year
    end_year = end.year
    label = (
        f"{start_year}赛季" if start_year == end_year
        else f"{str(start_year)[-2:]}-{str(end_year)[-2:]}赛季"
    )
    return {
        "key": f"{start_year}-{end_year}",
        "label": label,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "inferred": inferred,
    }


def _season_snapshot(
    snapshot: dict[str, Any], season: dict[str, Any], game_date: str,
) -> dict[str, Any]:
    result = {
        "season_key": str(season.get("key") or ""),
        "season_label": str(season.get("label") or ""),
        "season_start": str(season.get("start") or ""),
        "season_end": str(season.get("end") or ""),
        "first_observed": game_date or None,
        "last_observed": game_date or None,
        "partial": bool(season.get("inferred")),
    }
    for key in (
        "name", "full_name", "nationality", "nationality_code", "positions",
        "primary_positions", "position_ratings", "shirt_number", "ca", "pa",
        "season_stats",
    ):
        if key in snapshot:
            result[key] = _strip_addresses(snapshot.get(key))
    if "shirt_number" not in result:
        shirt_number = _snapshot_shirt_number(snapshot)
        if shirt_number is not None:
            result["shirt_number"] = shirt_number
    return result


def _record_season_snapshot(
    club: dict[str, Any], row: dict[str, Any], snapshot: dict[str, Any],
    season: dict[str, Any], game_date: str,
) -> None:
    season_key = str(season.get("key") or "")
    if not season_key:
        return
    seasons = club.setdefault("seasons", {})
    club_season = seasons.setdefault(season_key, {
        "key": season_key,
        "label": str(season.get("label") or season_key),
        "start": str(season.get("start") or ""),
        "end": str(season.get("end") or ""),
        "first_observed": game_date or None,
        "last_observed": game_date or None,
        "partial": bool(season.get("inferred")),
    })
    if game_date:
        first = str(club_season.get("first_observed") or game_date)
        club_season["first_observed"] = min(first, game_date)
        club_season["last_observed"] = max(str(club_season.get("last_observed") or game_date), game_date)
    club_season["partial"] = bool(club_season.get("partial")) or bool(season.get("inferred"))

    snapshots = row.setdefault("season_snapshots", {})
    previous = dict(snapshots.get(season_key) or {})
    current = _season_snapshot(snapshot, season, game_date)
    if previous.get("first_observed"):
        current["first_observed"] = previous["first_observed"]
    current["partial"] = bool(previous.get("partial")) or bool(current.get("partial"))
    snapshots[season_key] = current


def _backfill_season_snapshots(facts: dict[str, Any]) -> None:
    """Expose one conservative partial season for pre-v4 latest snapshots."""
    for club in (facts.get("clubs") or {}).values():
        if not isinstance(club, dict):
            continue
        club.setdefault("seasons", {})
        for row in (club.get("players") or {}).values():
            if not isinstance(row, dict) or row.get("season_snapshots"):
                continue
            snapshot = dict(row.get("snapshot") or {})
            observed = str(
                snapshot.get("observed_game_date")
                or row.get("last_seen_game_date")
                or row.get("first_seen_game_date")
                or ""
            )
            season = club_legacy_season_identity(observed)
            if season:
                season["inferred"] = True
                _record_season_snapshot(club, row, snapshot, season, observed)


def _record_observation_history(
    row: dict[str, Any], snapshot: dict[str, Any], game_date: str,
) -> None:
    shirt_number = _snapshot_shirt_number(snapshot)
    if shirt_number is not None:
        history = row.setdefault("shirt_number_history", [])
        if not history or int(history[-1].get("number") or 0) != shirt_number:
            history.append({"number": shirt_number, "first_observed": game_date or None})
        history[-1]["last_observed"] = game_date or history[-1].get("last_observed")

    asking_price = _snapshot_asking_price(snapshot)
    if asking_price is None:
        return
    values = row.setdefault("value_history", [])
    if values:
        previous = values[-1]
        current_day = _history_date(game_date)
        previous_day = _history_date(previous.get("game_date"))
        if current_day and previous_day:
            if current_day <= previous_day or (current_day - previous_day).days < 90:
                return
        elif previous.get("asking_price") == asking_price:
            return
    observation = {
        "game_date": game_date or None,
        "asking_price": asking_price,
    }
    values.append(observation)
    row["value_history"] = values[-48:]


def _game_month(game_date: str) -> str:
    value = str(game_date or "")
    if (
        len(value) >= 7 and value[4] == "-" and value[7:8] in {"", "-"}
        and value[:4].isdigit() and value[5:7].isdigit()
        and 1 <= int(value[5:7]) <= 12
    ):
        return value[:7]
    return ""


def _record_monthly_attribute_snapshot(
    row: dict[str, Any], snapshot: dict[str, Any], game_date: str,
) -> None:
    month = _game_month(game_date)
    attributes = snapshot.get("attributes")
    if not month or not isinstance(attributes, dict) or not attributes:
        return
    monthly = {
        "month": month,
        "observed_game_date": game_date,
        "ca": snapshot.get("ca"),
        "pa": snapshot.get("pa"),
        "training_ca": snapshot.get("training_ca"),
        "attributes": _strip_addresses(attributes),
        "hidden_attributes": _strip_addresses(snapshot.get("hidden_attributes") or {}),
    }
    history = row.setdefault("attribute_snapshots", [])
    history[:] = [item for item in history if str(item.get("month") or "") != month]
    history.append(monthly)
    history.sort(key=lambda item: (
        str(item.get("month") or ""), str(item.get("observed_game_date") or ""),
    ))


def _normalize_career_record(raw: Any) -> dict[str, Any]:
    source = raw if isinstance(raw, dict) else {}
    record: dict[str, Any] = {}
    for key in RECORD_INTEGER_FIELDS:
        value = source.get(key)
        if value in (None, ""):
            record[key] = None
            continue
        try:
            normalized = int(value)
        except (TypeError, ValueError) as error:
            raise ValueError(f"{key} 必须是整数") from error
        if normalized < 0:
            raise ValueError(f"{key} 不能小于 0")
        record[key] = normalized
    rating = source.get("average_rating")
    if rating in (None, ""):
        record["average_rating"] = None
    else:
        try:
            normalized_rating = round(float(rating), 2)
        except (TypeError, ValueError) as error:
            raise ValueError("平均评分必须是数字") from error
        if not 0 <= normalized_rating <= 10:
            raise ValueError("平均评分必须在 0 到 10 之间")
        record["average_rating"] = normalized_rating
    for key in RECORD_TEXT_FIELDS:
        value = str(source.get(key) or "").strip()
        if len(value) > 2000:
            raise ValueError("荣誉或离队后成就不能超过 2000 字")
        record[key] = value
    return record


def classify_late_goal(
    *,
    minute: Any,
    player_team_id: Any,
    home_id: Any,
    away_id: Any,
    home_goals: Any,
    away_goals: Any,
    goal_events: Any = None,
    player_id: Any = None,
) -> list[str]:
    """Classify a player's 85'+ goal as a winner/equalizer when provable.

    Native ``goal_events`` are preferred when supplied: the scorer and event
    side are matched, then the score immediately before that event is used to
    prove that it changed a loss/draw into the final draw/win.  A compact
    scoreline-only fallback is retained for older manually captured moments.
    Ambiguous or inconsistent native vectors return no automatic label rather
    than guessing from the final score.
    """
    try:
        normalized_minute = int(minute)
        selected_team = int(player_team_id)
        home = int(home_id)
        away = int(away_id)
        final_home = max(0, int(home_goals))
        final_away = max(0, int(away_goals))
    except (TypeError, ValueError):
        return []
    if normalized_minute < 85 or selected_team not in {home, away}:
        return []

    def event_minute(event: dict[str, Any]) -> int | None:
        value = event.get("minute")
        if value in (None, ""):
            value = event.get("time")
        if isinstance(value, dict):
            value = value.get("minute")
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def event_side(event: dict[str, Any]) -> str | None:
        side = str(event.get("team_side") or event.get("side") or "").casefold()
        if side in {"home", "主队"}:
            return "home"
        if side in {"away", "客队"}:
            return "away"
        try:
            team_id = int(event.get("team_id"))
        except (TypeError, ValueError):
            return None
        return "home" if team_id == home else "away" if team_id == away else None

    def scorer_id(event: dict[str, Any]) -> int | None:
        scorer = event.get("scorer")
        if isinstance(scorer, dict):
            scorer = scorer.get("id") or scorer.get("player_id")
        else:
            scorer = event.get("scorer_id") or scorer
        try:
            value = int(scorer)
        except (TypeError, ValueError):
            return None
        return value if value > 0 else None

    # A non-empty native vector is authoritative.  Do not silently fall back
    # to a scoreline when it proves that this player did not score late.
    if isinstance(goal_events, list):
        try:
            ordered = [item for item in goal_events if isinstance(item, dict)]
            ordered = sorted(
                enumerate(ordered),
                key=lambda pair: (
                    event_minute(pair[1]) if event_minute(pair[1]) is not None else 10_000,
                    pair[0],
                ),
            )
            home_score = away_score = 0
            target = None
            for _index, event in ordered:
                side = event_side(event)
                if side is None:
                    return []
                # ``team_side`` is the awarded/scoring side in the native
                # parser.  An own goal still counts toward that side, but the
                # selected player did not score for their team and cannot be
                # tagged as an individual late winner/equalizer.
                own_goal = bool(event.get("own_goal"))
                effective_side = side
                before = (home_score, away_score)
                if effective_side == "home":
                    home_score += 1
                else:
                    away_score += 1
                if (
                    target is None
                    and not own_goal
                    and scorer_id(event) is not None
                    and player_id is not None
                    and scorer_id(event) == int(player_id)
                    and ((effective_side == "home" and selected_team == home)
                         or (effective_side == "away" and selected_team == away))
                    and event_minute(event) is not None
                    and event_minute(event) == normalized_minute
                    and event_minute(event) >= 85
                ):
                    target = (before, (home_score, away_score))
            if (home_score, away_score) != (final_home, final_away) or target is None:
                return []
            before, after = target
            selected_before = before[0] if selected_team == home else before[1]
            opponent_before = before[1] if selected_team == home else before[0]
            selected_after = after[0] if selected_team == home else after[1]
            opponent_after = after[1] if selected_team == home else after[0]
            if final_home == final_away and selected_after == opponent_after and selected_before < opponent_before:
                return ["绝平球"]
            if final_home != final_away and selected_after == opponent_after + 1 and selected_before <= opponent_before:
                return ["绝杀球"]
            return []
        except (TypeError, ValueError, OverflowError):
            return []

    # Legacy/manual records have no event vector.  Keep the conservative
    # scoreline heuristic used by existing archives.
    player_goals = final_home if selected_team == home else final_away
    opponent_goals = final_away if selected_team == home else final_home
    if player_goals == opponent_goals and player_goals > 0:
        return ["绝平球"]
    if player_goals == opponent_goals + 1:
        return ["绝杀球"]
    return []


def _normalize_match_moment(raw: Any, *, player_id: int | None = None) -> dict[str, Any]:
    source = raw if isinstance(raw, dict) else {}
    game_date = str(source.get("game_date") or source.get("date") or "").strip()[:10]
    if not _history_date(game_date):
        raise ValueError("比赛日期无效")
    competition_id = int(source.get("competition_id") or (source.get("competition") or {}).get("id") or 0)
    if competition_id < 0:
        raise ValueError("赛事标识无效")
    home = source.get("home") or source.get("home_team") or {}
    away = source.get("away") or source.get("away_team") or {}
    if not isinstance(home, dict) or not isinstance(away, dict):
        raise ValueError("比赛球队信息无效")
    home_id = int(home.get("id") or 0)
    away_id = int(away.get("id") or 0)
    if home_id <= 0 or away_id <= 0:
        raise ValueError("比赛球队标识无效")
    tags = source.get("tags") or source.get("contributions") or []
    if isinstance(tags, str):
        tags = [tags]
    tags = list(dict.fromkeys(str(tag).strip() for tag in tags if str(tag).strip()))
    if not tags or any(tag not in MATCH_MOMENT_TAGS for tag in tags):
        raise ValueError("至少选择一个有效的比赛贡献类型")
    if len(tags) > 5:
        raise ValueError("比赛贡献类型不能超过 5 项")
    minute = source.get("minute")
    if minute in (None, ""):
        normalized_minute = None
    else:
        try:
            normalized_minute = int(minute)
        except (TypeError, ValueError) as error:
            raise ValueError("比赛分钟必须是整数") from error
        if not 0 <= normalized_minute <= 130:
            raise ValueError("比赛分钟必须在 0 到 130 之间")
    note = str(source.get("note") or "").strip()
    if len(note) > 240:
        raise ValueError("比赛备注不能超过 240 字")
    player_team_id = int(source.get("player_team_id") or 0)
    auto_tags: list[str] = []
    if "进球" in tags:
        auto_tags = classify_late_goal(
            minute=normalized_minute,
            player_team_id=player_team_id,
            home_id=home_id,
            away_id=away_id,
            home_goals=source.get("home_goals") or 0,
            away_goals=source.get("away_goals") or 0,
            goal_events=source.get("goal_events"),
            player_id=player_id,
        )
    tags = list(dict.fromkeys([*tags, *auto_tags]))
    if len(tags) > 5:
        # Automatic classification should never make an otherwise valid
        # manual record fail validation; reserve a slot for the classifier.
        tags = [*tags[:max(0, 5 - len(auto_tags))], *auto_tags][:5]
    fixture_key = f"{game_date}|{competition_id}|{home_id}|{away_id}"
    normalized = {
        "fixture_key": fixture_key,
        "game_date": game_date,
        "competition_id": competition_id,
        "competition_name": str(source.get("competition_name") or (source.get("competition") or {}).get("name") or competition_id),
        "home": {"id": home_id, "name": str(home.get("name") or home_id)},
        "away": {"id": away_id, "name": str(away.get("name") or away_id)},
        "home_goals": max(0, int(source.get("home_goals") or 0)),
        "away_goals": max(0, int(source.get("away_goals") or 0)),
        "tags": tags,
        "auto_tags": auto_tags,
        "auto_tag_source": (
            "native_goal_events"
            if auto_tags and isinstance(source.get("goal_events"), list)
            else "scoreline_heuristic" if auto_tags else None
        ),
        "player_team_id": player_team_id or None,
        "minute": normalized_minute,
        "note": note,
        "source": "manual",
    }
    normalized["id"] = hashlib.sha256(
        json.dumps(normalized, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:24]
    return normalized


def _event_id(player_id: int, event_type: str, game_date: str, detail: str, epoch: int) -> str:
    raw = f"{player_id}|{event_type}|{game_date}|{detail}|{epoch}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:24]


def _append_event(
    player: dict[str, Any], event_type: str, game_date: str,
    title: str, detail: str, epoch: int, extra: dict[str, Any] | None = None,
) -> None:
    event_id = _event_id(int(player["id"]), event_type, game_date, detail, epoch)
    events = player.setdefault("events", [])
    if any(str(event.get("id") or "") == event_id for event in events):
        return
    event = {
        "id": event_id,
        "type": event_type,
        "game_date": game_date,
        "title": title,
        "detail": detail,
        "epoch": epoch,
        "recorded_at": _now(),
    }
    if extra:
        event.update(_strip_addresses(extra))
    events.append(event)
    events.sort(key=lambda row: (str(row.get("game_date") or ""), str(row.get("recorded_at") or "")))


def _injured(snapshot: dict[str, Any]) -> bool:
    availability = snapshot.get("availability") or {}
    return bool(
        int(availability.get("injury_count") or 0) > 0
        or list(availability.get("injuries") or [])
    )


def _snapshot_injuries(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    availability = snapshot.get("availability") or {}
    injuries = availability.get("injuries") or []
    if not isinstance(injuries, list):
        return []
    result = []
    for item in injuries:
        if not isinstance(item, dict):
            continue
        row = {
            key: str(item.get(key) or "")
            for key in (
                "type", "start_date", "estimated_return_from", "estimated_return_to",
            ) if item.get(key) not in (None, "")
        }
        # Preserve a native recurrence flag if a future verified reader adds
        # one; current builds infer recurrence from repeated ODD snapshots.
        if item.get("recurring") is not None:
            row["recurring"] = bool(item.get("recurring"))
        result.append(row)
    return result


def _record_injury_episodes(
    row: dict[str, Any], injuries: list[dict[str, Any]],
    game_date: str, epoch: int,
) -> list[dict[str, Any]]:
    """Append de-duplicated injury episodes and mark repeated types.

    This is deliberately a persistence-layer inference: ODD observes the
    player every refresh, so a recovered injury followed by the same injury
    type is strong evidence of recurrence even when the native ``Recurring``
    flag is not mapped for this build.
    """
    history = [item for item in (row.get("injury_history") or []) if isinstance(item, dict)]
    row["injury_history"] = history
    episodes: list[dict[str, Any]] = []
    for injury in injuries:
        label = str(injury.get("type") or "").strip()
        if not label:
            continue
        start = str(injury.get("start_date") or game_date or "")
        if any(
            str(item.get("type") or "") == label
            and str(item.get("start_date") or "") == start
            for item in history
        ):
            continue
        prior = [item for item in history if str(item.get("type") or "") == label]
        episode = {
            "type": label,
            "start_date": start or None,
            "estimated_return_from": injury.get("estimated_return_from"),
            "estimated_return_to": injury.get("estimated_return_to"),
            "recurring": bool(prior) or bool(injury.get("recurring")),
            "recurrence_number": len(prior) + 1,
            "observed_game_date": game_date or None,
            "epoch": int(epoch),
            "source": "native_flag" if injury.get("recurring") is not None else "odd_snapshot_inference",
        }
        history.append(episode)
        episodes.append(episode)
    if history:
        row["injury_history"] = history[-100:]
    recurring = [item for item in history if item.get("recurring")]
    row["injury_summary"] = {
        "active": bool(injuries),
        "active_injuries": deepcopy(injuries),
        "episode_count": len(history),
        "recurring_count": len(recurring),
        "recurring_types": list(dict.fromkeys(str(item.get("type") or "") for item in recurring)),
        "last_observed_game_date": game_date or None,
        "source": "native_flag_or_odd_snapshot_inference",
    }
    return episodes


def _injury_detail(injuries: list[dict[str, Any]], fallback: str) -> str:
    labels = [str(item.get("type") or "").strip() for item in injuries]
    labels = list(dict.fromkeys(label for label in labels if label))
    detail = f"具体伤病：{'、'.join(labels)}。" if labels else "具体伤病未读取到。"
    returns = [
        str(item.get("estimated_return_to") or "").strip()
        for item in injuries if item.get("estimated_return_to")
    ]
    if returns:
        detail += f"预计恢复：{max(returns)}。"
    return f"{detail}{fallback}"


def _retirement_date(snapshot: dict[str, Any]) -> str:
    retirement = snapshot.get("retirement") or {}
    if not retirement.get("has_plan") or retirement.get("cancelled"):
        return ""
    return str(retirement.get("retirement_date") or "")


def _contract_expiry(snapshot: dict[str, Any]) -> str:
    return str((snapshot.get("player_contract") or {}).get("expiry_date") or "")


def _joined_club_date(snapshot: dict[str, Any]) -> str:
    value = str((snapshot.get("player_contract") or {}).get("joined_club_date") or "")
    return value[:10] if _history_date(value) else ""


def _record_joined_current_club(
    club: dict[str, Any], row: dict[str, Any], snapshot: dict[str, Any],
    *, game_date: str, previous_sync_date: str, epoch: int,
) -> bool:
    events = row.get("events") or []
    if any(
        str(event.get("type") or "") == "joined_current_club"
        and not event.get("superseded")
        for event in events
    ):
        return True
    joined_date = _joined_club_date(snapshot)
    joined_day = _history_date(joined_date)
    observed_date = str(row.get("first_seen_game_date") or game_date or "")
    observed_day = _history_date(observed_date)
    previous_sync_day = _history_date(previous_sync_date)
    tracking_started_day = _history_date(club.get("tracking_started"))
    observed_between_refreshes = bool(
        previous_sync_day is not None
        and joined_day is not None
        and observed_day is not None
        and previous_sync_day <= joined_day <= observed_day
    )
    eligible_backfill = bool(
        tracking_started_day is not None
        and joined_day is not None
        and observed_day is not None
        and tracking_started_day < joined_day <= observed_day
    )
    if not (observed_between_refreshes or eligible_backfill):
        return False
    _append_event(
        row, "joined_current_club", joined_date, "转入本队",
        f"原生合同记录该球员于 {joined_date} 加盟{club['name']}；"
        f"FMODD 于 {observed_date} 的一线名单刷新中首次确认。",
        epoch,
        {"observed_game_date": observed_date, "source": "native_joined_club_date"},
    )
    return True


def _reputation(snapshot: dict[str, Any]) -> int | None:
    for key in ("world_reputation", "current_reputation", "international_reputation"):
        value = snapshot.get(key)
        if value is not None:
            try:
                return int(value)
            except (TypeError, ValueError):
                continue
    return None


def _record_snapshot_changes(
    row: dict[str, Any], previous: dict[str, Any], current: dict[str, Any],
    game_date: str, epoch: int,
) -> None:
    if not previous:
        return
    before_injuries = _snapshot_injuries(previous)
    after_injuries = _snapshot_injuries(current)
    if _injured(previous) != _injured(current):
        if _injured(current):
            episodes = _record_injury_episodes(row, after_injuries, game_date, epoch)
            _append_event(
                row, "injury_started", game_date, "出现伤病",
                _injury_detail(after_injuries, "本次名单读取发现球员处于伤病状态。"), epoch,
                {"injuries": after_injuries, "injury_episodes": episodes},
            )
        else:
            recovered_types = {
                str(item.get("type") or "").strip()
                for item in before_injuries if str(item.get("type") or "").strip()
            }
            for episode in row.get("injury_history") or []:
                if (
                    str(episode.get("type") or "") in recovered_types
                    and not episode.get("recovered_game_date")
                ):
                    episode["recovered_game_date"] = game_date or None
            _append_event(
                row, "injury_recovered", game_date, "伤病恢复",
                _injury_detail(before_injuries, "本次名单读取未再发现活动伤病。"), epoch,
                {"injuries": before_injuries},
            )
    elif before_injuries and after_injuries and before_injuries != after_injuries:
        episodes = _record_injury_episodes(row, after_injuries, game_date, epoch)
        _append_event(
            row, "injury_changed", game_date, "伤病变化",
            f"原伤病：{_injury_detail(before_injuries, '').strip()} → 新伤病：{_injury_detail(after_injuries, '').strip()}。",
            epoch, {"injuries": after_injuries, "injury_episodes": episodes},
        )
    before_loan = bool(previous.get("is_loaned_out") or (previous.get("loan") or {}).get("is_loaned_out"))
    after_loan = bool(current.get("is_loaned_out") or (current.get("loan") or {}).get("is_loaned_out"))
    if before_loan != after_loan:
        loan_team = str((current.get("loan") or {}).get("team_name") or "其他俱乐部")
        _append_event(
            row, "loan_started" if after_loan else "loan_ended", game_date,
            "开始外租" if after_loan else "结束外租",
            f"当前租借球队：{loan_team}。" if after_loan else "球员已不再显示为外租状态。",
            epoch,
        )
    before_ca = previous.get("ca")
    after_ca = current.get("ca")
    if before_ca is not None and after_ca is not None and int(before_ca) != int(after_ca):
        delta = int(after_ca) - int(before_ca)
        _append_event(
            row, "ca_changed", game_date, "当前能力变化",
            f"CA {int(before_ca)} → {int(after_ca)}（{delta:+d}）。", epoch,
        )
    before_rep = _reputation(previous)
    after_rep = _reputation(current)
    if before_rep is not None and after_rep is not None and abs(after_rep - before_rep) >= 100:
        _append_event(
            row, "reputation_changed", game_date, "声望显著变化",
            f"声望 {before_rep} → {after_rep}（{after_rep - before_rep:+d}）。", epoch,
        )
    before_expiry = _contract_expiry(previous)
    after_expiry = _contract_expiry(current)
    if before_expiry and after_expiry and before_expiry != after_expiry:
        _append_event(
            row, "contract_changed", game_date, "合同期限变化",
            f"合同到期日 {before_expiry} → {after_expiry}。", epoch,
        )
    before_retirement = _retirement_date(previous)
    after_retirement = _retirement_date(current)
    if before_retirement != after_retirement:
        _append_event(
            row, "retirement_changed", game_date,
            "出现退役计划" if after_retirement else "退役计划取消",
            f"计划退役日期：{after_retirement}。" if after_retirement else "当前未读取到活动退役计划。",
            epoch,
        )


def _handle_rewind(club: dict[str, Any], game_date: str) -> int:
    previous_date = str(club.get("last_synced_game_date") or "")
    epoch = int(club.get("timeline_epoch") or 0)
    if not previous_date or not game_date or game_date >= previous_date:
        return epoch
    epoch += 1
    club["timeline_epoch"] = epoch
    for row in (club.get("players") or {}).values():
        row["roster_state"] = "unknown_after_rewind"
        snapshot = dict(row.get("snapshot") or {})
        if str(snapshot.get("observed_game_date") or "") > game_date:
            archived = row.setdefault("superseded_snapshots", [])
            archived.append(snapshot)
            row["superseded_snapshots"] = archived[-3:]
            row["snapshot"] = {
                key: deepcopy(snapshot.get(key))
                for key in (
                    "id", "name", "full_name", "first_name", "last_name",
                    "common_name", "nationality_id", "nationality",
                    "nationality_code", "date_of_birth", "positions",
                    "primary_positions",
                ) if key in snapshot
            }
            row["snapshot"]["rewind_unverified"] = True
        if str(row.get("last_seen_game_date") or "") > game_date:
            row["last_seen_game_date"] = None
        monthly = list(row.get("attribute_snapshots") or [])
        future_monthly = [
            item for item in monthly
            if str(item.get("observed_game_date") or "") > game_date
        ]
        if future_monthly:
            archived_monthly = row.setdefault("superseded_attribute_snapshots", [])
            archived_monthly.extend(future_monthly)
            row["superseded_attribute_snapshots"] = archived_monthly[-24:]
            row["attribute_snapshots"] = [
                item for item in monthly
                if str(item.get("observed_game_date") or "") <= game_date
            ]
        for event in row.get("events", []):
            if str(event.get("game_date") or "") > game_date:
                event["superseded"] = True
    return epoch


def sync_club_profile(
    career_id: str, profile: dict[str, Any], *, game_layout: str = "",
    season_start: str = "", season_end: str = "",
) -> dict[str, Any]:
    career = str(career_id or "").strip()
    team = dict(profile.get("team") or {})
    team_id = int(team.get("id") or 0)
    if not career or not team_id or str(team.get("team_type") or "club") != "club":
        return {}
    game_date = str(profile.get("game_date") or "")
    season = club_legacy_season_identity(game_date, season_start, season_end)
    players = [row for row in (profile.get("players") or []) if int(row.get("id") or 0) > 0]
    with _LOCK:
        facts = _load_facts(career)
        clubs = facts.setdefault("clubs", {})
        club = clubs.setdefault(str(team_id), {
            "id": team_id,
            "name": str(team.get("name") or team_id),
            "tracking_started": game_date or None,
            "last_synced_game_date": None,
            "timeline_epoch": 0,
            "observed_layouts": [],
            "players": {},
        })
        club["name"] = str(team.get("name") or club.get("name") or team_id)
        if game_layout and game_layout not in club.setdefault("observed_layouts", []):
            club["observed_layouts"].append(game_layout)
        epoch = _handle_rewind(club, game_date)
        current_ids: set[int] = set()
        stored_players = club.setdefault("players", {})
        previous_sync_date = str(club.get("last_synced_game_date") or "")
        for source in players:
            player_id = int(source["id"])
            current_ids.add(player_id)
            snapshot = _player_snapshot(source, team, game_date)
            row = stored_players.setdefault(str(player_id), {
                "id": player_id,
                "first_seen_game_date": game_date or None,
                "last_seen_game_date": game_date or None,
                "roster_state": "current",
                "events": [],
                "snapshot": {},
            })
            previous = dict(row.get("snapshot") or {})
            previous_state = str(row.get("roster_state") or "")
            joined_current_club = _record_joined_current_club(
                club, row, snapshot, game_date=game_date,
                previous_sync_date=previous_sync_date, epoch=epoch,
            )
            if not previous:
                if not joined_current_club:
                    _append_event(
                        row, "first_observed", game_date, "首次记录于一线名单",
                        "FMODD 首次在本俱乐部一线名单中记录到该球员；这不等同于实际加盟日期。",
                        epoch,
                    )
            elif previous_state != "current":
                _append_event(
                    row, "returned_to_roster", game_date, "重新进入当前名单",
                    "球员再次出现在本俱乐部一线名单读取结果中。", epoch,
                )
            _record_snapshot_changes(row, previous, snapshot, game_date, epoch)
            if not previous and _injured(snapshot):
                _record_injury_episodes(row, _snapshot_injuries(snapshot), game_date, epoch)
            elif not _injured(snapshot) and row.get("injury_summary"):
                row["injury_summary"] = {
                    **dict(row.get("injury_summary") or {}),
                    "active": False,
                    "active_injuries": [],
                    "last_observed_game_date": game_date or None,
                }
            _record_observation_history(row, snapshot, game_date)
            _record_monthly_attribute_snapshot(row, snapshot, game_date)
            _record_season_snapshot(club, row, snapshot, season, game_date)
            row["snapshot"] = snapshot
            row["last_seen_game_date"] = game_date or row.get("last_seen_game_date")
            row["roster_state"] = "current"
            row["last_observed_epoch"] = epoch
        for player_id, row in stored_players.items():
            if int(player_id) in current_ids or str(row.get("roster_state") or "") != "current":
                continue
            row["roster_state"] = "history"
            row["left_current_roster_date"] = game_date or None
            _append_event(
                row, "left_current_roster", game_date, "离开当前一线名单",
                "本次读取未在一线名单中发现该球员；可能是转会、下放、外租或名单调整。",
                epoch,
            )
        club["last_synced_game_date"] = game_date or club.get("last_synced_game_date")
        club["updated_at"] = _now()
        facts["updated_at"] = club["updated_at"]
        save_document(FACTS_KEY, facts, career)
        return deepcopy(club)


def sync_world_watch_player(
    career_id: str, player: dict[str, Any], *, game_date: str = "",
    game_layout: str = "",
) -> dict[str, Any]:
    career = str(career_id or "").strip()
    player_id = int(player.get("id") or 0)
    if not career or player_id <= 0:
        raise ValueError("世界关注球员标识无效")
    observed_date = str(game_date or player.get("observed_game_date") or "")
    team = {
        "id": int(player.get("team_id") or 0),
        "name": str(player.get("team_name") or "自由球员"),
    }
    snapshot = _player_snapshot(player, team, observed_date)
    with _LOCK:
        facts = _load_facts(career)
        club = facts.setdefault("clubs", {}).setdefault(str(WORLD_WATCH_TEAM_ID), {
            "id": WORLD_WATCH_TEAM_ID,
            "name": "世界关注",
            "tracking_started": observed_date or None,
            "last_synced_game_date": None,
            "timeline_epoch": 0,
            "observed_layouts": [],
            "players": {},
        })
        if game_layout and game_layout not in club.setdefault("observed_layouts", []):
            club["observed_layouts"].append(game_layout)
        row = club.setdefault("players", {}).setdefault(str(player_id), {
            "id": player_id,
            "first_seen_game_date": observed_date or None,
            "last_seen_game_date": observed_date or None,
            "roster_state": "world_watch",
            "events": [],
            "snapshot": {},
        })
        previous = dict(row.get("snapshot") or {})
        epoch = int(club.get("timeline_epoch") or 0)
        if not previous:
            _append_event(
                row, "world_watch_started", observed_date, "开始持续关注",
                "从世界球员搜索加入持续关注；后续记录从本次观察开始。", epoch,
            )
        _record_snapshot_changes(row, previous, snapshot, observed_date, epoch)
        if not previous and _injured(snapshot):
            _record_injury_episodes(row, _snapshot_injuries(snapshot), observed_date, epoch)
        elif not _injured(snapshot) and row.get("injury_summary"):
            row["injury_summary"] = {
                **dict(row.get("injury_summary") or {}),
                "active": False,
                "active_injuries": [],
                "last_observed_game_date": observed_date or None,
            }
        _record_observation_history(row, snapshot, observed_date)
        _record_monthly_attribute_snapshot(row, snapshot, observed_date)
        row["snapshot"] = snapshot
        row["last_seen_game_date"] = observed_date or row.get("last_seen_game_date")
        row["roster_state"] = "world_watch"
        club["last_synced_game_date"] = observed_date or club.get("last_synced_game_date")
        club["updated_at"] = _now()
        facts["updated_at"] = club["updated_at"]
        save_document(FACTS_KEY, facts, career)
        return deepcopy(club)


def _status(row: dict[str, Any]) -> tuple[str, str]:
    snapshot = row.get("snapshot") or {}
    if str(row.get("roster_state") or "") == "world_watch":
        return "watched", "世界关注"
    if str(row.get("roster_state") or "") == "current":
        if bool(snapshot.get("is_loaned_out") or (snapshot.get("loan") or {}).get("is_loaned_out")):
            return "loaned", "外租"
        return "current", "当前名单"
    if str(row.get("roster_state") or "") == "unknown_after_rewind":
        return "unknown", "读档后待确认"
    return "history", "历史成员"


def _normalize_best_team_lineup(
    raw: Any, *, lineup_kind: str, season_key: str = "",
) -> dict[str, Any]:
    source = raw if isinstance(raw, dict) else {}
    formation = str(source.get("formation") or "4231")
    if formation not in BEST_TEAM_FORMATIONS:
        formation = "4231"
    allowed_slots = BEST_TEAM_FORMATIONS[formation]["slots"]
    raw_assignments = source.get("assignments") or {}
    if isinstance(raw_assignments, list):
        raw_assignments = {
            str(item.get("slot_id") or ""): item.get("player_id")
            for item in raw_assignments if isinstance(item, dict)
        }
    assignments = []
    used_players: set[int] = set()
    if isinstance(raw_assignments, dict):
        for slot_id, position in allowed_slots.items():
            try:
                player_id = int(raw_assignments.get(slot_id) or 0)
            except (TypeError, ValueError):
                player_id = 0
            if player_id <= 0 or player_id in used_players:
                continue
            used_players.add(player_id)
            assignments.append({
                "slot_id": slot_id, "position": position, "player_id": player_id,
            })
    return {
        "kind": lineup_kind,
        "season_key": season_key or None,
        "formation": formation,
        "formation_name": str(BEST_TEAM_FORMATIONS[formation]["name"]),
        "assignments": assignments,
        "filled": len(assignments),
        "updated_at": source.get("updated_at"),
    }


def _public_best_teams(raw: Any) -> dict[str, Any]:
    source = raw if isinstance(raw, dict) else {}
    season_rows = []
    for season_key, lineup in (source.get("seasons") or {}).items():
        season_rows.append(_normalize_best_team_lineup(
            lineup, lineup_kind="season", season_key=str(season_key),
        ))
    season_rows.sort(key=lambda row: str(row.get("season_key") or ""), reverse=True)
    return {
        "all_time": _normalize_best_team_lineup(
            source.get("all_time"), lineup_kind="all_time",
        ),
        "seasons": season_rows,
    }


def public_club_legacy(career_id: str, scope_id: str, team_id: int) -> dict[str, Any]:
    career = str(career_id or "").strip()
    scope = str(scope_id or career).strip()
    with _LOCK:
        facts = _load_facts(career) if career else _empty_facts()
        preferences = _load_preferences(scope) if scope else _empty_preferences()
        club = deepcopy((facts.get("clubs") or {}).get(str(int(team_id)), {}))
        club_preferences = (preferences.get("clubs") or {}).get(str(int(team_id)), {})
        best_teams = deepcopy((preferences.get("best_teams") or {}).get(str(int(team_id)), {}))
    rows = []
    active_date = str(club.get("last_synced_game_date") or "")
    for player_id, stored in (club.get("players") or {}).items():
        if (
            active_date and str(stored.get("first_seen_game_date") or "") > active_date
            and str(stored.get("roster_state") or "") == "unknown_after_rewind"
        ):
            continue
        row = deepcopy(stored)
        pref = deepcopy(club_preferences.get(str(player_id), {}))
        status, status_label = _status(row)
        events = [
            event for event in row.get("events", [])
            if not event.get("superseded")
            and (not active_date or str(event.get("game_date") or "") <= active_date)
        ]
        events.sort(key=lambda event: (str(event.get("game_date") or ""), str(event.get("recorded_at") or "")), reverse=True)
        attribute_snapshots = [
            deepcopy(item) for item in row.get("attribute_snapshots", [])
            if not active_date or str(item.get("observed_game_date") or "") <= active_date
        ]
        attribute_snapshots.sort(key=lambda item: (
            str(item.get("month") or ""), str(item.get("observed_game_date") or ""),
        ), reverse=True)
        season_snapshots = [
            deepcopy(item) for item in (row.get("season_snapshots") or {}).values()
            if isinstance(item, dict)
            and (not active_date or str(item.get("first_observed") or "") <= active_date)
        ]
        season_snapshots.sort(key=lambda item: str(item.get("season_key") or ""), reverse=True)
        row.update({
            "status": status,
            "status_label": status_label,
            "events": events,
            "inducted": bool(pref.get("inducted")),
            "favorite": bool(pref.get("favorite")),
            "tier": str(pref.get("tier") or "") or None,
            "note": str(pref.get("note") or ""),
            "career_record": _normalize_career_record(pref.get("career_record")),
            "career_record_updated_at": pref.get("career_record_updated_at"),
            "shirt_numbers": list(row.get("shirt_number_history") or []),
            "value_history": list(row.get("value_history") or []),
            "attribute_snapshots": attribute_snapshots,
            "season_snapshots": season_snapshots,
            "match_moments": [
                deepcopy(item) for item in (pref.get("match_moments") or [])
                if isinstance(item, dict)
            ],
        })
        rows.append(row)
    rows.sort(key=lambda row: (
        not bool(row.get("inducted")),
        0 if row.get("status") in {"current", "loaned"} else 1,
        str((row.get("snapshot") or {}).get("name") or "").casefold(),
    ))
    seasons = [
        deepcopy(item) for item in (club.get("seasons") or {}).values()
        if isinstance(item, dict)
        and (not active_date or str(item.get("first_observed") or "") <= active_date)
    ]
    seasons.sort(key=lambda item: str(item.get("key") or ""), reverse=True)
    public_best_teams = _public_best_teams(best_teams)
    inducted_ids = {int(row.get("id") or 0) for row in rows if row.get("inducted")}
    season_ids = {
        str(season_row.get("key") or ""): {
            int(row.get("id") or 0) for row in rows
            if any(
                str(item.get("season_key") or "") == str(season_row.get("key") or "")
                for item in (row.get("season_snapshots") or [])
            )
        }
        for season_row in seasons
    }
    lineup_rows = [public_best_teams["all_time"], *public_best_teams["seasons"]]
    for lineup in lineup_rows:
        valid_ids = (
            inducted_ids if lineup.get("kind") == "all_time"
            else season_ids.get(str(lineup.get("season_key") or ""), set())
        )
        lineup["assignments"] = [
            item for item in lineup.get("assignments") or []
            if int(item.get("player_id") or 0) in valid_ids
        ]
        lineup["filled"] = len(lineup["assignments"])
    return {
        "schema_version": SCHEMA_VERSION,
        "club": {
            "id": int(club.get("id") or team_id),
            "name": str(club.get("name") or team_id),
        },
        "players": rows,
        "seasons": seasons,
        "best_teams": public_best_teams,
        "summary": {
            "total": len(rows),
            "current": sum(row["status"] in {"current", "loaned"} for row in rows),
            "history": sum(row["status"] in {"history", "unknown"} for row in rows),
            "inducted": sum(bool(row.get("inducted")) for row in rows),
            "favorites": sum(bool(row.get("favorite")) for row in rows),
        },
        "coverage": {
            "tracking_started": club.get("tracking_started"),
            "last_synced_game_date": club.get("last_synced_game_date"),
            "observed_layouts": list(club.get("observed_layouts") or []),
            "native_history_complete": False,
            "level": "fmodd_tracking",
            "label": "FMODD 持续追踪",
            "detail": "当前名单与启用后的变化可验证；启用前的完整俱乐部履历尚未补全。",
        },
    }


def public_club_legacy_index(career_id: str, scope_id: str) -> dict[str, Any]:
    career = str(career_id or "").strip()
    scope = str(scope_id or career).strip()
    with _LOCK:
        facts = _load_facts(career) if career else _empty_facts()
        club_ids = sorted(
            int(team_id) for team_id in (facts.get("clubs") or {})
            if str(team_id).lstrip("-").isdigit()
            and (int(team_id) > 0 or int(team_id) == WORLD_WATCH_TEAM_ID)
        )
    clubs = []
    for team_id in club_ids:
        payload = public_club_legacy(career, scope, team_id)
        clubs.append({
            **payload["club"],
            "summary": payload["summary"],
            "coverage": payload["coverage"],
        })
    clubs.sort(key=lambda row: (
        str((row.get("coverage") or {}).get("last_synced_game_date") or ""),
        str(row.get("name") or "").casefold(),
    ), reverse=True)
    return {
        "schema_version": SCHEMA_VERSION,
        "clubs": clubs,
        "summary": {
            "clubs": len(clubs),
            "players": sum(int((row.get("summary") or {}).get("total") or 0) for row in clubs),
            "inducted": sum(int((row.get("summary") or {}).get("inducted") or 0) for row in clubs),
        },
    }


def set_club_legacy_preference(
    career_id: str, scope_id: str, team_id: int, player_id: int,
    *, inducted: bool | None = None, favorite: bool | None = None,
    tier: str | None = None, note: str | None = None,
    career_record: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if (int(team_id) <= 0 and int(team_id) != WORLD_WATCH_TEAM_ID) or int(player_id) <= 0:
        raise ValueError("俱乐部或球员标识无效")
    current = public_club_legacy(career_id, scope_id, int(team_id))
    if not any(int(row.get("id") or 0) == int(player_id) for row in current.get("players", [])):
        raise ValueError("名人堂人物库中不存在该球员")
    normalized_tier = None if tier is None else str(tier or "").strip()
    if normalized_tier and normalized_tier not in TIERS:
        raise ValueError("名人堂等级无效")
    if note is not None and len(str(note)) > 500:
        raise ValueError("人物备注不能超过 500 字")
    normalized_record = (
        _normalize_career_record(career_record)
        if career_record is not None else None
    )
    with _LOCK:
        preferences = _load_preferences(scope_id)
        club = preferences.setdefault("clubs", {}).setdefault(str(int(team_id)), {})
        row = club.setdefault(str(int(player_id)), {})
        if inducted is not None:
            row["inducted"] = bool(inducted)
            if not inducted:
                row["tier"] = None
        if favorite is not None:
            row["favorite"] = bool(favorite)
        if tier is not None:
            row["tier"] = normalized_tier or None
            if normalized_tier:
                row["inducted"] = True
        if note is not None:
            row["note"] = str(note).strip()
        if normalized_record is not None:
            row["career_record"] = normalized_record
            row["career_record_updated_at"] = _now()
        row["updated_at"] = _now()
        preferences["updated_at"] = row["updated_at"]
        save_document(PREFERENCES_KEY, preferences, scope_id)
    return public_club_legacy(career_id, scope_id, int(team_id))


def set_club_legacy_best_team(
    career_id: str, scope_id: str, team_id: int, *, lineup_kind: str,
    formation: str, assignments: Any, season_key: str = "",
) -> dict[str, Any]:
    career = str(career_id or "").strip()
    scope = str(scope_id or career).strip()
    team = int(team_id or 0)
    kind = str(lineup_kind or "").strip().lower()
    season = str(season_key or "").strip()
    formation_key = str(formation or "").strip()
    if not career or not scope or team <= 0:
        raise ValueError("俱乐部最佳阵容标识无效")
    if kind not in {"all_time", "season"}:
        raise ValueError("最佳阵容范围无效")
    if kind == "season" and not season:
        raise ValueError("赛季最佳阵容缺少赛季标识")
    if formation_key not in BEST_TEAM_FORMATIONS:
        raise ValueError("最佳阵容阵型无效")
    raw_assignments = assignments
    if isinstance(raw_assignments, list):
        raw_assignments = {
            str(item.get("slot_id") or ""): item.get("player_id")
            for item in raw_assignments if isinstance(item, dict)
        }
    if not isinstance(raw_assignments, dict):
        raise ValueError("最佳阵容位置数据无效")
    allowed_slots = BEST_TEAM_FORMATIONS[formation_key]["slots"]
    invalid_slots = set(str(key) for key in raw_assignments) - set(allowed_slots)
    if invalid_slots:
        raise ValueError("最佳阵容包含当前阵型不存在的位置")
    normalized_assignments: dict[str, int] = {}
    used_players: set[int] = set()
    for slot_id, raw_player_id in raw_assignments.items():
        try:
            player_id = int(raw_player_id or 0)
        except (TypeError, ValueError) as error:
            raise ValueError("最佳阵容球员标识无效") from error
        if player_id <= 0:
            continue
        if player_id in used_players:
            raise ValueError("同一球员不能重复进入最佳阵容")
        used_players.add(player_id)
        normalized_assignments[str(slot_id)] = player_id

    with _LOCK:
        facts = _load_facts(career)
        club = (facts.get("clubs") or {}).get(str(team))
        if not isinstance(club, dict):
            raise ValueError("人物库中不存在该俱乐部")
        stored_players = club.get("players") or {}
        missing = [player_id for player_id in used_players if str(player_id) not in stored_players]
        if missing:
            raise ValueError("最佳阵容包含人物库中不存在的球员")
        preferences = _load_preferences(scope)
        club_preferences = (preferences.get("clubs") or {}).get(str(team), {})
        if kind == "all_time":
            unavailable = [
                player_id for player_id in used_players
                if not bool((club_preferences.get(str(player_id)) or {}).get("inducted"))
            ]
            if unavailable:
                raise ValueError("历史最佳阵容只能选择本俱乐部名人堂成员")
        else:
            if season not in (club.get("seasons") or {}):
                raise ValueError("人物库中不存在该赛季记录")
            unavailable = [
                player_id for player_id in used_players
                if season not in ((stored_players.get(str(player_id)) or {}).get("season_snapshots") or {})
            ]
            if unavailable:
                raise ValueError("赛季最佳阵容只能选择该赛季实际记录过的球员")

        best_team = preferences.setdefault("best_teams", {}).setdefault(str(team), {
            "all_time": {}, "seasons": {},
        })
        lineup = {
            "formation": formation_key,
            "assignments": normalized_assignments,
            "updated_at": _now(),
        }
        if kind == "all_time":
            best_team["all_time"] = lineup
        else:
            best_team.setdefault("seasons", {})[season] = lineup
        preferences["updated_at"] = lineup["updated_at"]
        save_document(PREFERENCES_KEY, preferences, scope)
    return public_club_legacy(career, scope, team)


def add_club_legacy_match_moment(
    career_id: str, scope_id: str, team_id: int, player_id: int,
    moment: dict[str, Any],
) -> dict[str, Any]:
    """Append one user-curated match contribution to a player's permanent archive."""
    if (int(team_id) <= 0 and int(team_id) != WORLD_WATCH_TEAM_ID) or int(player_id) <= 0:
        raise ValueError("俱乐部或球员标识无效")
    current = public_club_legacy(career_id, scope_id, int(team_id))
    if not any(int(row.get("id") or 0) == int(player_id) for row in current.get("players", [])):
        raise ValueError("名人堂人物库中不存在该球员")
    normalized = _normalize_match_moment(moment, player_id=int(player_id))
    with _LOCK:
        preferences = _load_preferences(scope_id)
        club = preferences.setdefault("clubs", {}).setdefault(str(int(team_id)), {})
        row = club.setdefault(str(int(player_id)), {})
        moments = [item for item in (row.get("match_moments") or []) if isinstance(item, dict)]
        existing = next((item for item in moments if str(item.get("fixture_key") or "") == normalized["fixture_key"]), None)
        if existing is not None:
            existing["tags"] = list(dict.fromkeys([*(existing.get("tags") or []), *normalized["tags"]]))[:5]
            existing["auto_tags"] = list(dict.fromkeys([*(existing.get("auto_tags") or []), *normalized["auto_tags"]]))
            existing["auto_tag_source"] = normalized.get("auto_tag_source") or existing.get("auto_tag_source")
            if normalized["minute"] is not None:
                existing["minute"] = normalized["minute"]
            if normalized["note"]:
                existing["note"] = normalized["note"]
            existing["competition_name"] = normalized["competition_name"]
            existing["home"] = normalized["home"]
            existing["away"] = normalized["away"]
            existing["home_goals"] = normalized["home_goals"]
            existing["away_goals"] = normalized["away_goals"]
        else:
            normalized["created_at"] = _now()
            moments.append(normalized)
        moments.sort(key=lambda item: (str(item.get("game_date") or ""), str(item.get("created_at") or "")), reverse=True)
        row["match_moments"] = moments[:MATCH_MOMENT_MAX]
        row["updated_at"] = _now()
        preferences["updated_at"] = row["updated_at"]
        save_document(PREFERENCES_KEY, preferences, scope_id)
    return public_club_legacy(career_id, scope_id, int(team_id))


__all__ = [
    "public_club_legacy", "public_club_legacy_index",
    "set_club_legacy_preference", "sync_club_profile",
    "add_club_legacy_match_moment", "classify_late_goal",
    "sync_world_watch_player", "WORLD_WATCH_TEAM_ID",
]
