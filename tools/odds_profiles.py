"""Pure ODD team profiles from supplied roster and result facts; no FM or storage I/O."""
from __future__ import annotations

from typing import Any

from tools.result_evidence import _result_identity


STAR_PLAYER_COUNT = 4

OTHER_STARTER_COUNT = 7

BENCH_PLAYER_COUNT = 11

PRICED_PLAYER_COUNT = STAR_PLAYER_COUNT + OTHER_STARTER_COUNT + BENCH_PLAYER_COUNT

STAR_CA_WEIGHT = 0.50

OTHER_STARTER_CA_WEIGHT = 0.30

BENCH_CA_WEIGHT = 0.20

RAW_CA_WEIGHT = 0.75

CONDITIONED_CA_WEIGHT = 0.25

RECENT_MATCH_LIMIT = 10

RECENT_MATCH_WEIGHTS = (
    1 / 6, 1 / 6, 1 / 6,
    3 / 40, 3 / 40, 3 / 40, 3 / 40,
    1 / 15, 1 / 15, 1 / 15,
)

_POSITION_ROLE_NAMES = {
    "GK": ("GK",),
    "DEF": ("SW", "DL", "DC", "DR", "WBL", "WBR"),
    "MID": ("DM", "ML", "MC", "MR", "AML", "AMC", "AMR"),
    "FWD": ("ST", "AML", "AMC", "AMR"),
}

_BALANCED_STARTING_SLOTS = ("GK", "DEF", "DEF", "DEF", "MID", "MID", "FWD")


def fitness_factor(fitness: float) -> float:
    if fitness >= 95:
        return 1.00
    if fitness >= 90:
        return 0.985
    if fitness >= 85:
        return 0.96
    if fitness >= 80:
        return 0.92
    return 0.86


def sharpness_factor(sharpness: float) -> float:
    if sharpness >= 90:
        return 1.00
    if sharpness >= 75:
        return 0.985
    if sharpness >= 50:
        return 0.96
    return 0.91


def nonlinear_ca(ca: float) -> float:
    """Use raw CA directly; high-CA tiers have no additional weighting."""
    return float(ca)


def _position_role_rating(player: dict[str, Any], role: str) -> int:
    positions = player.get("positions")
    if not isinstance(positions, dict):
        return 0
    ratings = []
    for name in _POSITION_ROLE_NAMES[role]:
        try:
            ratings.append(int(positions.get(name) or 0))
        except (TypeError, ValueError):
            continue
    return max(ratings, default=0)


def balanced_starting_eleven(
    candidates: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    """Build a diagnostic position-aware XI without changing live pricing yet."""
    remaining = list(candidates)
    selected: list[dict[str, Any]] = []
    filled_slots = 0
    for role in _BALANCED_STARTING_SLOTS:
        eligible = [
            player for player in remaining
            if _position_role_rating(player, role) > 1
        ]
        if not eligible:
            continue
        choice = max(
            eligible,
            key=lambda player: (
                float(player.get("effective_ca") or 0)
                * (0.75 + 0.25 * min(_position_role_rating(player, role), 20) / 20),
                float(player.get("effective_ca") or 0),
            ),
        )
        selected.append(choice)
        remaining.remove(choice)
        filled_slots += 1
    selected.extend(remaining[:max(0, 11 - len(selected))])
    return selected[:11], filled_slots


def tiered_squad_average(
    players: list[dict[str, Any]], key: str, default: float = 100.0,
) -> float:
    """Average up to 22 inferred matchday players with fixed per-slot weights."""
    tiers = (
        (0, STAR_PLAYER_COUNT, STAR_CA_WEIGHT),
        (STAR_PLAYER_COUNT, OTHER_STARTER_COUNT, OTHER_STARTER_CA_WEIGHT),
        (
            STAR_PLAYER_COUNT + OTHER_STARTER_COUNT,
            BENCH_PLAYER_COUNT,
            BENCH_CA_WEIGHT,
        ),
    )
    weighted_total = 0.0
    occupied_weight = 0.0
    for start, capacity, tier_weight in tiers:
        slot_weight = tier_weight / capacity
        for player in players[start:start + capacity]:
            try:
                raw_value = player.get(key)
                value = float(default if raw_value is None else raw_value)
            except (TypeError, ValueError):
                value = float(default)
            weighted_total += value * slot_weight
            occupied_weight += slot_weight
    return weighted_total / occupied_weight if occupied_weight else float(default)



def build_team_profile(
    squad: list[dict[str, Any]], team_id: int, past: list[dict[str, Any]],
    elo_adjustments: dict[tuple[tuple[str, int, int, int], int], float] | None = None,
) -> dict[str, Any]:
    recent = sorted(past, key=lambda item: item["date"], reverse=True)[:RECENT_MATCH_LIMIT]
    goals_for = []
    goals_against = []
    adjusted_goal_differences = []
    opponent_adjustments = []
    points = []
    for item, weight in zip(recent, RECENT_MATCH_WEIGHTS):
        home = item["home_team"]["id"] == team_id
        scored = item["home_goals"] if home else item["away_goals"]
        conceded = item["away_goals"] if home else item["home_goals"]
        goals_for.append((scored, weight))
        goals_against.append((conceded, weight))
        opponent_adjustment = float(
            (elo_adjustments or {}).get((_result_identity(item), team_id), 0.0)
        )
        opponent_adjustments.append((opponent_adjustment, weight))
        adjusted_goal_differences.append((scored - conceded + opponent_adjustment, weight))
        points.append((3 if scored > conceded else 1 if scored == conceded else 0, weight))

    recent_coverage = sum(weight for _value, weight in goals_for)

    def weighted_average(values: list[tuple[float, float]]) -> float | None:
        total_weight = sum(weight for _value, weight in values)
        if not total_weight:
            return None
        return sum(float(value) * weight for value, weight in values) / total_weight

    recent_goals_for = weighted_average(goals_for)
    recent_goals_against = weighted_average(goals_against)
    recent_adjusted_goal_difference = weighted_average(adjusted_goal_differences)
    recent_opponent_elo_adjustment = weighted_average(opponent_adjustments)
    recent_points = weighted_average(points)

    eligible = [
        player for player in squad
        if not player.get("availability")
        or (player["availability"]["injury_count"] == 0 and player["availability"]["ban_count"] == 0)
    ]
    candidates = []
    for player in eligible:
        ca = player.get("ca") or 0
        fitness = player.get("fitness_percent") or 0
        sharpness = player.get("sharpness_percent") or 0
        candidates.append({
            **player,
            "effective_ca": ca * fitness_factor(fitness) * sharpness_factor(sharpness),
            "nonlinear_ca": nonlinear_ca(ca),
            "effective_nonlinear_ca": (
                nonlinear_ca(ca) * fitness_factor(fitness) * sharpness_factor(sharpness)
            ),
        })
    valid_ca_players = [player for player in candidates if player["ca"] > 0]
    if not valid_ca_players:
        if recent:
            form_ca = 20.0 + 40.0 * float(recent_points or 0.0) / 3.0
            fallback_ca = round(max(20.0, min(60.0, form_ca)), 2)
            ca_source = "recent_form_fallback"
        else:
            fallback_ca = 40.0
            ca_source = "default_fallback"
        fallback_nonlinear_ca = nonlinear_ca(fallback_ca)
        return {
            "squad_size": len(squad),
            "eligible_players": len(eligible),
            "candidate_players": 0,
            "candidate_13_ca": fallback_ca,
            "candidate_13_effective_ca": fallback_ca,
            "candidate_20_ca": fallback_ca,
            "candidate_20_effective_ca": fallback_ca,
            "candidate_22_ca": fallback_ca,
            "candidate_22_effective_ca": fallback_ca,
            "star_top4_ca": fallback_ca,
            "starting_other7_ca": fallback_ca,
            "bench_11_ca": fallback_ca,
            "weighted_raw_ca": fallback_ca,
            "weighted_effective_ca": fallback_ca,
            "balanced_candidate_11_ca": fallback_ca,
            "balanced_candidate_11_effective_ca": fallback_ca,
            "position_slots_filled": 0,
            "position_coverage": 0.0,
            "candidate_13_nonlinear_ca": round(fallback_nonlinear_ca, 2),
            "candidate_20_nonlinear_ca": round(fallback_nonlinear_ca, 2),
            "raw_ca_strength": round(fallback_nonlinear_ca, 2),
            "conditioned_ca_strength": round(fallback_nonlinear_ca, 2),
            "squad_strength": round(fallback_nonlinear_ca, 2),
            "candidate_20_fitness": 90.0,
            "candidate_20_sharpness": 75.0,
            "candidate_20_morale_raw": 15.0,
            "ca_source": ca_source,
            "recent_matches": len(recent),
            "recent_coverage": round(recent_coverage, 4),
            "recent_goals_for": round(recent_goals_for, 2) if recent_goals_for is not None else None,
            "recent_goals_against": round(recent_goals_against, 2) if recent_goals_against is not None else None,
            "recent_adjusted_goal_difference": (
                round(recent_adjusted_goal_difference, 3)
                if recent_adjusted_goal_difference is not None else None
            ),
            "recent_opponent_elo_adjustment": (
                round(recent_opponent_elo_adjustment, 3)
                if recent_opponent_elo_adjustment is not None else 0.0
            ),
        }
    candidates = valid_ca_players
    candidates.sort(
        key=lambda player: (
            float(player.get("ca") or 0),
            float(player.get("effective_ca") or 0),
        ),
        reverse=True,
    )
    candidate_22 = candidates[:PRICED_PLAYER_COUNT]
    candidate_20 = candidate_22[:20]
    candidate_13 = candidate_20[:13]
    balanced_candidate_11, position_slots_filled = balanced_starting_eleven(
        candidate_22,
    )
    average = lambda players, key, default: sum((player.get(key) or default) for player in players) / max(len(players), 1)
    raw_ca = average(candidate_20, "ca", 100)
    core_raw_ca = average(candidate_13, "ca", 100)
    possible_ca = sum(player["effective_ca"] for player in candidate_20) / max(len(candidate_20), 1)
    core_possible_ca = sum(player["effective_ca"] for player in candidate_13) / max(len(candidate_13), 1)
    balanced_core_raw_ca = average(balanced_candidate_11, "ca", 100)
    balanced_core_possible_ca = average(
        balanced_candidate_11, "effective_ca", 100,
    )
    nonlinear_depth_ca = average(candidate_20, "nonlinear_ca", 100)
    nonlinear_core_ca = average(candidate_13, "nonlinear_ca", 100)
    effective_nonlinear_depth_ca = average(candidate_20, "effective_nonlinear_ca", 100)
    effective_nonlinear_core_ca = average(candidate_13, "effective_nonlinear_ca", 100)
    weighted_raw_ca = tiered_squad_average(candidate_22, "ca")
    weighted_effective_ca = tiered_squad_average(candidate_22, "effective_ca")
    raw_ca_strength = tiered_squad_average(candidate_22, "nonlinear_ca")
    conditioned_ca_strength = tiered_squad_average(
        candidate_22, "effective_nonlinear_ca",
    )
    squad_strength = RAW_CA_WEIGHT * raw_ca_strength + CONDITIONED_CA_WEIGHT * conditioned_ca_strength
    fitness = average(candidate_20, "fitness_percent", 90)
    sharpness = average(candidate_20, "sharpness_percent", 75)
    morale = average(candidate_20, "morale_raw", 15)
    return {
        "squad_size": len(squad),
        "eligible_players": len(eligible),
        "candidate_players": len(candidate_22),
        "candidate_13_ca": round(core_raw_ca, 2),
        "candidate_13_effective_ca": round(core_possible_ca, 2),
        "candidate_20_ca": round(raw_ca, 2),
        "candidate_20_effective_ca": round(possible_ca, 2),
        "candidate_22_ca": round(average(candidate_22, "ca", 100), 2),
        "candidate_22_effective_ca": round(
            average(candidate_22, "effective_ca", 100), 2,
        ),
        "star_top4_ca": round(average(candidate_22[:4], "ca", 100), 2),
        "starting_other7_ca": round(
            average(candidate_22[4:11], "ca", 100), 2,
        ),
        "bench_11_ca": round(average(candidate_22[11:22], "ca", 100), 2),
        "weighted_raw_ca": round(weighted_raw_ca, 2),
        "weighted_effective_ca": round(weighted_effective_ca, 2),
        "balanced_candidate_11_ca": round(balanced_core_raw_ca, 2),
        "balanced_candidate_11_effective_ca": round(
            balanced_core_possible_ca, 2,
        ),
        "position_slots_filled": position_slots_filled,
        "position_coverage": round(
            position_slots_filled / len(_BALANCED_STARTING_SLOTS), 4,
        ),
        "candidate_13_nonlinear_ca": round(nonlinear_core_ca, 2),
        "candidate_20_nonlinear_ca": round(nonlinear_depth_ca, 2),
        "raw_ca_strength": round(raw_ca_strength, 2),
        "conditioned_ca_strength": round(conditioned_ca_strength, 2),
        "squad_strength": round(squad_strength, 2),
        "candidate_20_fitness": round(fitness, 2),
        "candidate_20_sharpness": round(sharpness, 2),
        "candidate_20_morale_raw": round(morale, 2),
        "ca_source": "squad",
        "recent_matches": len(recent),
        "recent_coverage": round(recent_coverage, 4),
        "recent_goals_for": round(recent_goals_for, 2) if recent_goals_for is not None else None,
        "recent_goals_against": round(recent_goals_against, 2) if recent_goals_against is not None else None,
        "recent_adjusted_goal_difference": (
            round(recent_adjusted_goal_difference, 3)
            if recent_adjusted_goal_difference is not None else None
        ),
        "recent_opponent_elo_adjustment": (
            round(recent_opponent_elo_adjustment, 3)
            if recent_opponent_elo_adjustment is not None else 0.0
        ),
    }
