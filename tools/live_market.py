from __future__ import annotations

from functools import lru_cache
from typing import Any

from tools.preview_cup_odds import (
    BTTS_MARGIN,
    FIRST_SCORE_MARGIN,
    GOAL_PARITY_MARGIN,
    additional_score_markets,
    asian_handicap_options,
    asian_total_options,
    bookmaker_prices,
    choose_asian_handicap,
    choose_total_line,
    decimal_odds,
    exact_goals_odds,
    half_time_markets,
    knockout_market_prices,
    quote_decimal_odds,
    score_matrix,
    team_total_options,
)


OUTCOMES = ("home", "draw", "away")
LIVE_START_PRESSURE = 0.15
LIVE_THREE_HOUR_PRESSURE = 0.20
LIVE_FIFTEEN_MINUTE_PRESSURE = 0.30
LIVE_RANDOM_STRENGTH_MIN = 1 / 3
LIVE_RANDOM_STRENGTH_MAX = 1.00
LIVE_DUAL_SUPPORT_RATE = 0.08
LIVE_DUAL_DRAW_PAIR_RATE = 0.85
LIVE_GROWTH_TRAJECTORY_RATE = 0.70
LIVE_FLAT_TRAJECTORY_RATE = 0.15
LIVE_REVERSAL_TRAJECTORY_RATE = 0.05
LIVE_RETURN_TRAJECTORY_RATE = 0.10
LIVE_REVERSAL_FINAL_PRESSURE = -0.10
LIVE_ODDS_DROP_CAP = 0.15
LIVE_ODDS_RISE_CAP = 0.20
MINIMUM_ODDS = 1.01


def fixture_key(match: dict[str, Any]) -> str:
    return "|".join(map(str, (
        match.get("fixture_date"),
        match.get("competition_id"),
        (match.get("home") or {}).get("id"),
        (match.get("away") or {}).get("id"),
    )))


def _valid_prices(value: Any) -> dict[str, float] | None:
    if not isinstance(value, dict):
        return None
    try:
        prices = {outcome: float(value[outcome]) for outcome in OUTCOMES}
    except (KeyError, TypeError, ValueError):
        return None
    return prices if all(price >= MINIMUM_ODDS for price in prices.values()) else None


def prepare_early_market_prices(
    output: dict[str, Any], previous_output: dict[str, Any] | None = None,
) -> None:
    """Keep the last pre-match-day casino quote as the live-market anchor."""
    game_date = str(output.get("game_date") or "")
    previous_output = previous_output or {}
    previous_date = str(previous_output.get("game_date") or "")
    previous_matches = {
        fixture_key(match): match for match in previous_output.get("matches", [])
    }
    for match in output.get("matches", []):
        current = _valid_prices(match.get("casino_1x2") or match.get("fair_1x2"))
        if not current:
            continue
        fixture_date = str(match.get("fixture_date") or "")
        previous = previous_matches.get(fixture_key(match)) or {}
        previous_early = _valid_prices(previous.get("early_casino_1x2"))
        previous_quote = _valid_prices(previous.get("casino_1x2"))
        if fixture_date > game_date:
            early = current
        elif previous_early:
            early = previous_early
        elif previous_quote and previous_date and previous_date < fixture_date:
            early = previous_quote
        else:
            early = current
        match["early_casino_1x2"] = {
            outcome: round(early[outcome], 2) for outcome in OUTCOMES
        }
        current_handicap = match.get("asian_handicap") or {}
        previous_early_handicap = previous.get("early_asian_handicap") or {}
        previous_handicap = previous.get("asian_handicap") or {}
        if fixture_date > game_date:
            early_handicap = current_handicap
        elif previous_early_handicap:
            early_handicap = previous_early_handicap
        elif previous_handicap and previous_date and previous_date < fixture_date:
            early_handicap = previous_handicap
        else:
            early_handicap = current_handicap
        if early_handicap.get("home_line") is not None:
            match["early_asian_handicap"] = dict(early_handicap)
    output["live_market_model"] = {
        "pricing": "casino",
        "strong_support_model": "implied_probability_gap_with_extreme_odds_segment",
        "strong_home_support": {"ordinary": 0.60, "odds_1_10": 0.90},
        "strong_away_support": {"ordinary": 0.40, "odds_1_10": 0.60},
        "pressure": {
            "day_start": LIVE_START_PRESSURE,
            "three_hours": LIVE_THREE_HOUR_PRESSURE,
            "fifteen_minutes": LIVE_FIFTEEN_MINUTE_PRESSURE,
        },
        "random_strength": [1 / 3, 1.00],
        "dual_support_rate": LIVE_DUAL_SUPPORT_RATE,
        "dual_draw_pair_rate": LIVE_DUAL_DRAW_PAIR_RATE,
        "trajectory_rates": {
            "growth": LIVE_GROWTH_TRAJECTORY_RATE,
            "flat": LIVE_FLAT_TRAJECTORY_RATE,
            "reversal": LIVE_REVERSAL_TRAJECTORY_RATE,
            "return": LIVE_RETURN_TRAJECTORY_RATE,
        },
        "odds_drop_cap": 0.15,
        "odds_rise_cap": 0.20,
        "minimum_odds": MINIMUM_ODDS,
    }


def _fnv1a_32(value: str) -> int:
    result = 2166136261
    for byte in value.encode("utf-8"):
        result ^= byte
        result = (result * 16777619) & 0xFFFFFFFF
    return result


def strong_support_rate(early: dict[str, float], favourite: str) -> float:
    """Price-sensitive primary support with an extra extreme-favourite segment."""
    underdog = "away" if favourite == "home" else "home"
    overround = sum(1.0 / early[outcome] for outcome in OUTCOMES)
    favourite_probability = (1.0 / early[favourite]) / overround
    underdog_probability = (1.0 / early[underdog]) / overround
    advantage = max(0.0, favourite_probability - underdog_probability)
    if favourite == "home":
        ordinary = min(0.75, max(0.45, 0.42 + 0.45 * advantage))
        extreme_at_130, extreme_at_110 = 0.70, 0.90
    else:
        ordinary = min(0.55, max(0.25, 0.22 + 0.45 * advantage))
        extreme_at_130, extreme_at_110 = 0.50, 0.60
    favourite_odds = early[favourite]
    if favourite_odds >= 1.30:
        return ordinary
    extreme_progress = min(1.0, max(0.0, (1.30 - favourite_odds) / 0.20))
    return extreme_at_130 + (extreme_at_110 - extreme_at_130) * extreme_progress


@lru_cache(maxsize=8192)
def _cached_market_profile(
    match_key: str, home_odds: float, draw_odds: float, away_odds: float,
) -> tuple[tuple[str, ...], float]:
    early = {"home": home_odds, "draw": draw_odds, "away": away_odds}
    seed = _fnv1a_32(match_key)
    direction_roll = seed / 4294967296.0
    strength_seed = (1664525 * seed + 1013904223) & 0xFFFFFFFF
    strength = LIVE_RANDOM_STRENGTH_MIN + (
        LIVE_RANDOM_STRENGTH_MAX - LIVE_RANDOM_STRENGTH_MIN
    ) * (strength_seed / 4294967296.0)

    if early["home"] < early["away"]:
        strong_rate = strong_support_rate(early, "home")
        remaining = 1.0 - strong_rate
        draw_cutoff = strong_rate + remaining * 4.0 / 7.0
        if direction_roll < strong_rate:
            focus = "home"
        elif direction_roll < draw_cutoff:
            focus = "draw"
        else:
            focus = "away"
    elif early["away"] < early["home"]:
        strong_rate = strong_support_rate(early, "away")
        remaining = 1.0 - strong_rate
        home_cutoff = strong_rate + remaining * 7.0 / 11.0
        if direction_roll < strong_rate:
            focus = "away"
        elif direction_roll < home_cutoff:
            focus = "home"
        else:
            focus = "draw"
    else:
        balanced_home = 0.40
        balanced_away = 0.35
        balanced_draw = 0.20
        balanced_total = balanced_home + balanced_away + balanced_draw
        if direction_roll < balanced_home / balanced_total:
            focus = "home"
        elif direction_roll < (balanced_home + balanced_away) / balanced_total:
            focus = "away"
        else:
            focus = "draw"
    focuses = (focus,)
    pair_seed = (1664525 * strength_seed + 1013904223) & 0xFFFFFFFF
    if pair_seed / 4294967296.0 < LIVE_DUAL_SUPPORT_RATE:
        secondary_seed = (1664525 * pair_seed + 1013904223) & 0xFFFFFFFF
        secondary_roll = secondary_seed / 4294967296.0
        if focus in {"home", "away"} and secondary_roll < LIVE_DUAL_DRAW_PAIR_RATE:
            secondary = "draw"
        elif focus == "home":
            secondary = "away"
        elif focus == "away":
            secondary = "home"
        else:
            secondary = "home" if secondary_roll < 0.5 else "away"
        focuses = (focus, secondary)
    return focuses, strength


def _market_profile(match: dict[str, Any], early: dict[str, float]) -> tuple[tuple[str, ...], float]:
    return _cached_market_profile(
        fixture_key(match),
        float(early["home"]),
        float(early["draw"]),
        float(early["away"]),
    )


def _interpolate(left_x: float, left_y: float, right_x: float, right_y: float, value: float) -> float:
    if right_x <= left_x:
        return right_y
    ratio = min(1.0, max(0.0, (value - left_x) / (right_x - left_x)))
    return left_y + (right_y - left_y) * ratio


def live_pressure(current_minutes: int, kickoff_minutes: int | None) -> float:
    if kickoff_minutes is None:
        return LIVE_START_PRESSURE
    kickoff = max(0, int(kickoff_minutes))
    current = max(0, int(current_minutes))
    three_hours = max(0, kickoff - 180)
    fifteen_minutes = max(three_hours, kickoff - 15)
    if current <= three_hours:
        return _interpolate(0, LIVE_START_PRESSURE, three_hours, LIVE_THREE_HOUR_PRESSURE, current)
    if current <= fifteen_minutes:
        return _interpolate(
            three_hours, LIVE_THREE_HOUR_PRESSURE,
            fifteen_minutes, LIVE_FIFTEEN_MINUTE_PRESSURE,
            current,
        )
    return LIVE_FIFTEEN_MINUTE_PRESSURE


@lru_cache(maxsize=8192)
def _cached_market_trajectory(match_key: str) -> str:
    roll = _fnv1a_32(f"{match_key}|trajectory") / 4294967296.0
    if roll < LIVE_GROWTH_TRAJECTORY_RATE:
        return "growth"
    if roll < LIVE_GROWTH_TRAJECTORY_RATE + LIVE_FLAT_TRAJECTORY_RATE:
        return "flat"
    if roll < (
        LIVE_GROWTH_TRAJECTORY_RATE
        + LIVE_FLAT_TRAJECTORY_RATE
        + LIVE_REVERSAL_TRAJECTORY_RATE
    ):
        return "reversal"
    return "return"


def market_trajectory(match: dict[str, Any]) -> str:
    return _cached_market_trajectory(fixture_key(match))


def trajectory_pressure(
    trajectory: str, current_minutes: int, kickoff_minutes: int | None,
) -> float:
    if trajectory == "growth":
        return live_pressure(current_minutes, kickoff_minutes)
    if trajectory == "flat" or kickoff_minutes is None:
        return LIVE_START_PRESSURE
    kickoff = max(0, int(kickoff_minutes))
    current = max(0, int(current_minutes))
    three_hours = max(0, kickoff - 180)
    fifteen_minutes = max(three_hours, kickoff - 15)
    if trajectory == "return":
        if current <= three_hours:
            return LIVE_START_PRESSURE
        if current <= fifteen_minutes:
            return _interpolate(
                three_hours, LIVE_START_PRESSURE,
                fifteen_minutes, 0.0,
                current,
            )
        return 0.0
    if current <= three_hours:
        return _interpolate(0, LIVE_START_PRESSURE, three_hours, LIVE_THREE_HOUR_PRESSURE, current)
    if current <= fifteen_minutes:
        return _interpolate(
            three_hours, LIVE_THREE_HOUR_PRESSURE,
            fifteen_minutes, LIVE_REVERSAL_FINAL_PRESSURE,
            current,
        )
    return LIVE_REVERSAL_FINAL_PRESSURE


def live_market_quote(
    match: dict[str, Any], game_clock: dict[str, Any] | None, *,
    include_catalog: bool = False,
) -> dict[str, Any]:
    early = _valid_prices(match.get("early_casino_1x2"))
    if not early:
        early = _valid_prices(match.get("casino_1x2") or match.get("fair_1x2"))
    if not early:
        return {"phase": "early", "odds": {}, "movement": {}}

    fixture_date = str(match.get("fixture_date") or "")
    game_date = str((game_clock or {}).get("date") or "")
    kickoff = match.get("kickoff_minutes")
    current_minutes = int((game_clock or {}).get("minutes") or 0)
    if not game_date or game_date < fixture_date:
        phase = "early"
    elif game_date > fixture_date or (kickoff is not None and current_minutes > int(kickoff)):
        phase = "closed"
    else:
        phase = "live"

    focuses, strength = _market_profile(match, early)
    trajectory = market_trajectory(match)
    pressure = trajectory_pressure(
        trajectory, current_minutes, int(kickoff) if kickoff is not None else None,
    ) if phase == "live" else 0.0
    quoted = dict(early)
    if phase == "live" and focuses:
        overround = sum(1.0 / early[outcome] for outcome in OUTCOMES)
        probabilities = {outcome: (1.0 / early[outcome]) / overround for outcome in OUTCOMES}
        for focus in focuses:
            probabilities[focus] *= 1.0 + pressure * strength
        total = sum(probabilities.values())
        probabilities = {outcome: value / total for outcome, value in probabilities.items()}
        raw = {outcome: 1.0 / (probabilities[outcome] * overround) for outcome in OUTCOMES}
        quoted = {
            outcome: max(
                MINIMUM_ODDS,
                min(early[outcome] * (1.0 + LIVE_ODDS_RISE_CAP),
                    max(early[outcome] * (1.0 - LIVE_ODDS_DROP_CAP), raw[outcome])),
            )
            for outcome in OUTCOMES
        }

    rounded = {
        outcome: quote_decimal_odds(quoted[outcome])
        for outcome in OUTCOMES
    }
    movement = {
        outcome: "rise" if rounded[outcome] > early[outcome] else "fall" if rounded[outcome] < early[outcome] else "flat"
        for outcome in OUTCOMES
    }
    if phase == "live":
        try:
            live_matrix, handicap_market, market_catalog = _cached_live_market_surface(
                float(match["xg"]["home"]), float(match["xg"]["away"]),
                float(rounded["home"]), float(rounded["draw"]), float(rounded["away"]),
                include_catalog,
            )
        except (KeyError, TypeError, ValueError):
            live_matrix, handicap_market, market_catalog = None, {}, None
    else:
        live_matrix, handicap_market, market_catalog = None, {}, None
    knockout_market = _live_knockout_market(match, live_matrix) if live_matrix else None
    if market_catalog is not None and knockout_market is not None:
        market_catalog = {**market_catalog, "knockout": knockout_market}
    early_handicap = match.get("early_asian_handicap") or match.get("asian_handicap") or {}
    current_handicap = handicap_market.get("asian_handicap") or early_handicap
    try:
        early_home_line = float(early_handicap["home_line"])
        current_home_line = float(current_handicap["home_line"])
    except (KeyError, TypeError, ValueError):
        early_home_line = current_home_line = 0.0
    line_change = round(current_home_line - early_home_line, 2)
    result = {
        "phase": phase,
        "odds": rounded,
        "early_odds": {outcome: round(early[outcome], 2) for outcome in OUTCOMES},
        "movement": movement,
        "focus": list(focuses),
        "pressure": round(pressure, 4),
        "strength": round(strength, 4),
        "trajectory": trajectory,
        "asian_handicap": handicap_market.get("asian_handicap"),
        "handicap_options": handicap_market.get("handicap_options", []),
        "knockout": knockout_market,
        "handicap_movement": {
            "early_home_line": early_home_line,
            "current_home_line": current_home_line,
            "change": line_change,
            "direction": (
                "home_gives_more" if line_change < 0
                else "away_gives_more" if line_change > 0 else "flat"
            ),
        },
    }
    if market_catalog is not None:
        result["market_catalog"] = market_catalog
        result["score_matrix"] = live_matrix
    return result


def _live_market_catalog(
    quoted_1x2: dict[str, float],
    live_matrix: list[list[float]], *,
    handicap_market: dict[str, Any],
) -> dict[str, Any]:
    """Price every matrix-dependent market from one live probability surface."""
    outcomes = {outcome: 0.0 for outcome in OUTCOMES}
    home_xg = away_xg = 0.0
    btts = odd_goals = 0.0
    for home_goals, row in enumerate(live_matrix):
        for away_goals, probability in enumerate(row):
            outcome = (
                "home" if home_goals > away_goals
                else "away" if away_goals > home_goals else "draw"
            )
            outcomes[outcome] += probability
            home_xg += home_goals * probability
            away_xg += away_goals * probability
            if home_goals and away_goals:
                btts += probability
            if (home_goals + away_goals) % 2:
                odd_goals += probability

    no_goal = live_matrix[0][0]
    total_xg = home_xg + away_xg
    first_score_probabilities = {
        "home": (1.0 - no_goal) * home_xg / total_xg if total_xg else 0.0,
        "away": (1.0 - no_goal) * away_xg / total_xg if total_xg else 0.0,
        "none": no_goal,
    }
    total_line, fair_over, fair_under = choose_total_line(live_matrix)
    total_options = asian_total_options(live_matrix, total_line)
    main_total = next(item for item in total_options if item["is_main"])
    first_score = bookmaker_prices(first_score_probabilities, FIRST_SCORE_MARGIN)
    first_score["none"] = exact_goals_odds(no_goal)
    catalog: dict[str, Any] = {
        "xg": {"home": round(home_xg, 3), "away": round(away_xg, 3)},
        "fair_1x2": {
            outcome: decimal_odds(probability)
            for outcome, probability in outcomes.items()
        },
        "casino_1x2": dict(quoted_1x2),
        "asian_handicap": handicap_market.get("asian_handicap"),
        "handicap_options": handicap_market.get("handicap_options", []),
        "total_goals": {
            "line": total_line,
            "over_odds": main_total["over_odds"],
            "fair_over_odds": fair_over,
            "under_odds": main_total["under_odds"],
            "fair_under_odds": fair_under,
        },
        "total_options": total_options,
        "team_total_options": {
            "home": team_total_options(live_matrix, "home"),
            "away": team_total_options(live_matrix, "away"),
        },
        "fair_btts": {"yes": decimal_odds(btts), "no": decimal_odds(1.0 - btts)},
        "btts": bookmaker_prices({"yes": btts, "no": 1.0 - btts}, BTTS_MARGIN),
        "fair_goal_parity": {
            "odd": decimal_odds(odd_goals), "even": decimal_odds(1.0 - odd_goals),
        },
        "goal_parity": bookmaker_prices(
            {"odd": odd_goals, "even": 1.0 - odd_goals}, GOAL_PARITY_MARGIN,
        ),
        "fair_first_score": {
            key: decimal_odds(probability)
            for key, probability in first_score_probabilities.items()
        },
        "first_score": first_score,
        "half_time": half_time_markets(home_xg, away_xg),
        "market_catalog_state": "full",
        "markets_loaded": True,
    }
    catalog.update(additional_score_markets(live_matrix))
    return catalog


@lru_cache(maxsize=8192)
def _cached_live_market_surface(
    home_xg: float, away_xg: float,
    home_odds: float, draw_odds: float, away_odds: float,
    include_catalog: bool,
) -> tuple[
    list[list[float]] | None,
    dict[str, Any],
    dict[str, Any] | None,
]:
    """Cache pure live-market math for identical rounded prices and base xG."""
    match = {"xg": {"home": home_xg, "away": away_xg}}
    quoted = {"home": home_odds, "draw": draw_odds, "away": away_odds}
    live_matrix = _live_score_matrix(match, quoted)
    if not live_matrix:
        return None, {}, None
    handicap_market = _live_handicap_market(
        match, quoted, live_matrix=live_matrix,
    )
    market_catalog = (
        _live_market_catalog(
            quoted, live_matrix, handicap_market=handicap_market,
        )
        if include_catalog else None
    )
    return live_matrix, handicap_market, market_catalog


def apply_live_market_quote(
    match: dict[str, Any], quote: dict[str, Any],
) -> dict[str, Any]:
    """Overlay a quote on a match copy without mutating the published source."""
    priced = dict(match)
    catalog = quote.get("market_catalog")
    if isinstance(catalog, dict):
        priced.update(catalog)
    if quote.get("odds"):
        priced["casino_1x2"] = quote["odds"]
        priced["market_phase"] = quote["phase"]
        priced["odds_movement"] = quote["movement"]
        priced["live_market"] = {
            key: quote[key] for key in ("focus", "pressure", "strength", "trajectory")
        }
        priced["handicap_movement"] = quote["handicap_movement"]
    if quote.get("asian_handicap"):
        priced["asian_handicap"] = quote["asian_handicap"]
    if quote.get("handicap_options"):
        priced["handicap_options"] = quote["handicap_options"]
    if quote.get("knockout"):
        priced["knockout"] = quote["knockout"]
    return priced


def _live_handicap_market(
    match: dict[str, Any], quoted_1x2: dict[str, float], *,
    live_matrix: list[list[float]] | None = None,
) -> dict[str, Any]:
    """Rebuild the Asian main line and ladder from the current live 1X2 pressure."""
    live_matrix = live_matrix or _live_score_matrix(match, quoted_1x2)
    if not live_matrix:
        return {}
    main_line, fair_home_odds, fair_away_odds = choose_asian_handicap(live_matrix)
    options = asian_handicap_options(
        live_matrix, main_line,
        equivalent_1x2_prices=quoted_1x2,
    )
    main_option = next(option for option in options if option["is_main"])
    return {
        "asian_handicap": {
            "home_line": main_line,
            "home_odds": main_option["home_odds"],
            "fair_home_odds": fair_home_odds,
            "away_line": -main_line,
            "away_odds": main_option["away_odds"],
            "fair_away_odds": fair_away_odds,
        },
        "handicap_options": options,
    }


def _live_score_matrix(
    match: dict[str, Any], quoted_1x2: dict[str, float],
) -> list[list[float]] | None:
    """Reweight the score matrix to the current live 1X2 probabilities."""
    try:
        matrix = score_matrix(float(match["xg"]["home"]), float(match["xg"]["away"]))
        inverse = {outcome: 1.0 / float(quoted_1x2[outcome]) for outcome in OUTCOMES}
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return None
    target_total = sum(inverse.values())
    if target_total <= 0:
        return None
    targets = {outcome: inverse[outcome] / target_total for outcome in OUTCOMES}
    base_totals = {outcome: 0.0 for outcome in OUTCOMES}
    for home_goals, row in enumerate(matrix):
        for away_goals, probability in enumerate(row):
            outcome = "home" if home_goals > away_goals else "away" if away_goals > home_goals else "draw"
            base_totals[outcome] += probability
    factors = {
        outcome: targets[outcome] / base_totals[outcome] if base_totals[outcome] > 0 else 1.0
        for outcome in OUTCOMES
    }
    live_matrix = []
    for home_goals, row in enumerate(matrix):
        live_row = []
        for away_goals, probability in enumerate(row):
            outcome = "home" if home_goals > away_goals else "away" if away_goals > home_goals else "draw"
            live_row.append(probability * factors[outcome])
        live_matrix.append(live_row)
    total = sum(sum(row) for row in live_matrix)
    if total <= 0:
        return None
    return [[probability / total for probability in row] for row in live_matrix]


def _live_knockout_market(
    match: dict[str, Any], live_matrix: list[list[float]],
) -> dict[str, Any] | None:
    """Reprice knockout derivatives from the same live probability surface."""
    knockout = match.get("knockout")
    if not isinstance(knockout, dict):
        return None
    try:
        leg_index = int(knockout["leg_index"])
        leg_count = int(knockout["leg_count"])
        context: dict[str, Any] = {
            "orientation": "first" if leg_index == 1 else "second",
            "leg_index": leg_index,
            "leg_count": leg_count,
            "settlement_fixture": {
                "date": knockout["settlement_fixture_date"],
                "home_id": int(knockout["settlement_home_id"]),
                "away_id": int(knockout["settlement_away_id"]),
                "kickoff_minutes": knockout.get("settlement_kickoff_minutes"),
                "kickoff_time": knockout.get("settlement_kickoff_time"),
            },
            "current_score_matrix": live_matrix,
        }
    except (KeyError, TypeError, ValueError):
        return None
    first_leg = knockout.get("first_leg")
    if isinstance(first_leg, dict):
        context["first_home_goals"] = first_leg.get("home_goals")
        context["first_away_goals"] = first_leg.get("away_goals")
    pricing_context = knockout.get("pricing_context")
    if isinstance(pricing_context, dict):
        for key in ("first_leg_xg", "second_leg_xg"):
            if isinstance(pricing_context.get(key), dict):
                context[key] = dict(pricing_context[key])
    return knockout_market_prices(match, context)


def _live_handicap_options(
    match: dict[str, Any], quoted_1x2: dict[str, float],
) -> list[dict[str, Any]]:
    """Compatibility wrapper for callers that only need the live line ladder."""
    return _live_handicap_market(match, quoted_1x2).get("handicap_options", [])


def market_output(output: dict[str, Any], game_clock: dict[str, Any] | None) -> dict[str, Any]:
    if not output:
        return output
    public_output = dict(output)
    for internal_field in (
        "championship_markets", "competition_formats", "model_cache",
        "performance", "settlement_results",
    ):
        public_output.pop(internal_field, None)
    public_matches = []
    for source in output.get("matches", []):
        match = dict(source)
        for side in ("home", "away"):
            source_team = source.get(side)
            if not isinstance(source_team, dict):
                continue
            team = dict(source_team)
            profile = source_team.get("profile")
            if isinstance(profile, dict):
                team["profile"] = {
                    "candidate_20_ca": profile.get("candidate_20_ca"),
                }
            match[side] = team
        quote = live_market_quote(
            source, game_clock,
            include_catalog=source.get("market_catalog_state") == "full",
        )
        match = apply_live_market_quote(match, quote)
        public_matches.append(match)
    public_output["matches"] = public_matches
    return public_output


def live_clock_quotes(output: dict[str, Any], game_clock: dict[str, Any]) -> dict[str, Any]:
    game_date = str(game_clock.get("date") or "")
    return {
        fixture_key(match): live_market_quote(match, game_clock)
        for match in output.get("matches", [])
        if str(match.get("fixture_date") or "") == game_date
    }
