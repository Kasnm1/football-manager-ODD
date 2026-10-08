from __future__ import annotations

import unittest
import math
from types import SimpleNamespace
from unittest.mock import patch

from tools.preview_cup_odds import (
    MODEL_CACHE_VERSION,
    MODEL_VERSION,
    build_match_odds,
    balanced_starting_eleven,
    cached_team_profile,
    competition_metadata,
    expected_goals,
    half_time_markets,
    has_resolved_team_name,
    reusable_snapshot_model_cache,
    shrink_competition_home_away_split,
    standings_model_profiles,
    team_form_signature,
    team_profile,
    nonlinear_ca,
    STRENGTH_LOG_COEFFICIENT,
    STRENGTH_LOG_EDGE_CAP,
    EXTREME_STRENGTH_GAP_TAIL_CAP,
    EXTREME_STRENGTH_GAP_THRESHOLD,
    EXTREME_STRENGTH_LOG_COEFFICIENT,
    RECENT_FORM_LOG_EDGE_CAP,
)


class _RosterReader:
    def __init__(self) -> None:
        self.players = [
            {
                "id": index,
                "ca": 100,
                "fitness_percent": 100,
                "sharpness_percent": 100,
                "morale_raw": 15,
                "availability": None,
            }
            for index in range(1, 21)
        ]

    def roster(self, _team_address: int) -> list[dict]:
        return self.players


def _result(day: int, home_goals: int, away_goals: int) -> dict:
    return {
        "date": f"2026-07-{day:02d}",
        "competition": {"id": 7},
        "home_team": {"id": 1, "name": "Home"},
        "away_team": {"id": 2, "name": "Away"},
        "home_goals": home_goals,
        "away_goals": away_goals,
    }


def _neutral_profile() -> dict:
    return {
        "squad_strength": 100.0,
        "candidate_20_ca": 100.0,
        "candidate_20_morale_raw": 15.0,
        "ca_source": "squad",
        "recent_matches": 0,
        "recent_coverage": 0.0,
        "recent_goals_for": None,
        "recent_goals_against": None,
        "recent_adjusted_goal_difference": None,
    }


class OddsModelWeightingTests(unittest.TestCase):
    def test_ca_strength_is_linear_without_high_ca_tier_bonus(self):
        for ca in (90.0, 130.0, 150.0, 200.0):
            self.assertEqual(nonlinear_ca(ca), ca)

    def test_expected_goals_and_half_handicap_have_no_fixed_upper_line(self):
        home = {
            **_neutral_profile(),
            "squad_strength": 200.0,
            "candidate_20_ca": 200.0,
            "candidate_20_morale_raw": 20.0,
            "recent_matches": 10,
            "recent_coverage": 1.0,
            "recent_goals_for": 5.0,
            "recent_goals_against": 0.0,
            "recent_adjusted_goal_difference": 6.0,
        }
        away = {
            **_neutral_profile(),
            "squad_strength": 20.0,
            "candidate_20_ca": 20.0,
            "candidate_20_morale_raw": 1.0,
            "recent_matches": 10,
            "recent_coverage": 1.0,
            "recent_goals_for": 0.0,
            "recent_goals_against": 5.0,
            "recent_adjusted_goal_difference": -6.0,
        }

        home_xg, away_xg = expected_goals(
            home, away, 3.0, 0.3,
            home_reputation=10000, away_reputation=1,
        )
        half_markets = half_time_markets(home_xg, away_xg)

        self.assertGreater(home_xg, 4.2)
        self.assertLessEqual(half_markets["asian_handicap"]["home_line"], -3.0)
        self.assertIn(
            half_markets["asian_handicap"]["home_line"],
            [item["home_line"] for item in half_markets["handicap_options"]],
        )

    def test_competition_home_away_split_is_shrunk_without_changing_goal_level(self):
        home, away = shrink_competition_home_away_split(1.574, 0.900)

        self.assertAlmostEqual(home, 1.4392)
        self.assertAlmostEqual(away, 1.0348)
        self.assertAlmostEqual(home + away, 1.574 + 0.900)
        self.assertLess(home / away, 1.574 / 0.900)

    def test_team_signals_outweigh_a_skewed_competition_home_split(self):
        home_profile = {
            **_neutral_profile(),
            "squad_strength": 164.28,
            "candidate_20_ca": 156.80,
            "candidate_20_morale_raw": 14.85,
            "recent_matches": 10,
            "recent_coverage": 1.0,
            "recent_goals_for": 1.87,
            "recent_goals_against": 1.00,
            "recent_adjusted_goal_difference": 0.833,
        }
        away_profile = {
            **_neutral_profile(),
            "squad_strength": 169.25,
            "candidate_20_ca": 158.55,
            "candidate_20_morale_raw": 17.35,
            "recent_matches": 10,
            "recent_coverage": 1.0,
            "recent_goals_for": 2.13,
            "recent_goals_against": 1.03,
            "recent_adjusted_goal_difference": 1.094,
        }

        odds = build_match_odds(
            {"id": 1, "name": "Home", "short_name": "Home", "reputation": 8919},
            {"id": 2, "name": "Away", "short_name": "Away", "reputation": 8756},
            home_profile, away_profile, 1.574, 0.900,
        )

        self.assertGreaterEqual(odds["casino_1x2"]["home"], 2.30)
        self.assertIn(odds["asian_handicap"]["home_line"], {-0.25, 0.0})

    def test_empty_team_name_is_not_treated_as_resolved(self):
        self.assertFalse(has_resolved_team_name({"name": None, "short_name": None}))
        self.assertTrue(has_resolved_team_name({"name": "Home", "short_name": None}))

    def test_unknown_national_competition_is_not_misclassified_as_friendly(self):
        metadata = competition_metadata(
            987654321, "987654321", national_teams=True,
        )

        self.assertEqual(metadata["kind"], "national")
        self.assertEqual(metadata["name"], "国家队比赛")

    def test_runtime_generated_competition_id_ignores_fixed_metadata(self):
        with patch.dict(
            "tools.preview_cup_odds.WATCHED_COMPETITIONS",
            {2000999999: ("过期杯赛名称", "cup", 99)},
        ):
            metadata = competition_metadata(2000999999, "随机超级联赛")

        self.assertEqual(metadata, {
            "id": 2000999999,
            "name": "随机超级联赛",
            "kind": "league",
            "reputation": 60,
        })

    def test_current_fm26_dynamic_competitions_use_live_names(self):
        world_masters = competition_metadata(2000534802, "世界大师联赛")
        womens_champions_league = competition_metadata(2000179924, "欧女冠联赛")

        self.assertEqual(world_masters["kind"], "league")
        self.assertEqual(world_masters["name"], "世界大师联赛")
        self.assertEqual(womens_champions_league["kind"], "cup")
        self.assertEqual(womens_champions_league["name"], "欧女冠联赛")

    def test_match_odds_supports_lightweight_and_extended_payloads(self):
        profile = _neutral_profile()
        home = {"id": 1, "name": "Home", "short_name": "Home", "reputation": 100}
        away = {"id": 2, "name": "Away", "short_name": "Away", "reputation": 100}
        light = build_match_odds(
            home, away, profile, profile, 1.45, 1.25, 7,
            include_extended_markets=False,
        )
        self.assertEqual(set(light), {
            "home", "away", "xg", "fair_1x2", "casino_1x2",
            "pricing_mode", "confidence", "confidence_score",
            "market_catalog_state",
        })
        self.assertEqual(light["market_catalog_state"], "core")
        self.assertNotIn("half_time", light)

        extended = build_match_odds(
            home, away, profile, profile, 1.45, 1.25, 7,
        )
        self.assertIn("half_time", extended)
        self.assertIn("handicap_options", extended)
        self.assertIn("team_total_options", extended)
        self.assertEqual(extended["market_catalog_state"], "full")

    def test_match_winner_and_minus_half_share_the_same_executable_price(self):
        profile = _neutral_profile()
        home = {"id": 1, "name": "Home", "short_name": "Home", "reputation": 100}
        away = {"id": 2, "name": "Away", "short_name": "Away", "reputation": 100}
        odds = build_match_odds(
            home, away, profile, profile, 1.35, 1.35,
            competition_id=1301385,
            fixture_date=SimpleNamespace(year=2026),
        )

        home_minus_half = next(
            item for item in odds["handicap_options"]
            if item["home_line"] == -0.5
        )
        away_minus_half = next(
            item for item in odds["handicap_options"]
            if item["away_line"] == -0.5
        )
        self.assertEqual(home_minus_half["home_odds"], odds["casino_1x2"]["home"])
        self.assertEqual(away_minus_half["away_odds"], odds["casino_1x2"]["away"])

    def test_missing_ca_fallback_is_deterministic_without_team_id_noise(self) -> None:
        reader = _RosterReader()
        for player in reader.players:
            player["ca"] = 0
        past = [_result(20, 2, 1), _result(19, 1, 1)]
        equivalent_past = [
            {
                **row,
                "home_team": {"id": 999, "name": "Equivalent Home"},
                "away_team": {"id": 998, "name": "Equivalent Away"},
            }
            for row in past
        ]

        first = team_profile(reader, 0x1000, 1, past)
        second = team_profile(reader, 0x2000, 999, equivalent_past)

        self.assertEqual(first["candidate_20_ca"], second["candidate_20_ca"])
        self.assertEqual(first["ca_source"], "recent_form_fallback")
        self.assertEqual(first["position_coverage"], 0.0)

    def test_balanced_starting_eleven_reserves_core_position_groups(self) -> None:
        candidates = []
        roles = [
            {"GK": 20},
            {"DC": 20}, {"DL": 20}, {"DR": 20},
            {"MC": 20}, {"DM": 20},
            {"ST": 20},
        ]
        for index, positions in enumerate(roles, start=1):
            candidates.append({
                "id": index,
                "positions": positions,
                "ca": 100 + index,
                "effective_ca": 100 + index,
            })
        candidates.extend({
            "id": index,
            "positions": {"MC": 20},
            "ca": 200,
            "effective_ca": 200,
        } for index in range(8, 16))

        selected, filled = balanced_starting_eleven(candidates)

        self.assertEqual(filled, 7)
        self.assertEqual(len(selected), 11)
        self.assertTrue(any(player["positions"].get("GK") for player in selected))

    def test_model_confidence_reflects_profile_completeness(self) -> None:
        profile = team_profile(_RosterReader(), 0x1000, 1, [
            _result(20 - index, 2, 1) for index in range(10)
        ])
        odds = build_match_odds(
            {"id": 1, "name": "A", "short_name": "A", "reputation": 100},
            {"id": 2, "name": "B", "short_name": "B", "reputation": 100},
            profile, profile, 1.4, 1.2, include_extended_markets=False,
        )

        self.assertEqual(odds["confidence"], "medium")
        self.assertGreaterEqual(odds["confidence_score"], 0.5)

    def test_snapshot_team_profile_reuses_only_matching_form_and_roster_signatures(self) -> None:
        past = [_result(20, 2, 1)]
        form_signature = team_form_signature(past, 1, {})
        snapshot = {
            "save_instance_id": "save-1",
            "game_date": "2026-07-21",
            "model_version": MODEL_VERSION,
            "model_cache": {
                "version": MODEL_CACHE_VERSION,
                "game_date": "2026-07-21",
                "profiles": [{
                    "team_address": "0x1000",
                    "team_id": 1,
                    "form_signature": form_signature,
                    "roster_signature": "roster-a",
                    "profile": _neutral_profile(),
                }],
                "baselines": [],
            },
        }
        persisted, _baselines = reusable_snapshot_model_cache(
            snapshot, "save-1", "2026-07-21",
        )
        reader = SimpleNamespace(roster_model_signature=lambda _address: "roster-a")
        state = {"profiles": {}}
        with (
            patch("tools.preview_cup_odds._runtime_state", return_value=state),
            patch("tools.preview_cup_odds.team_profile") as rebuild,
        ):
            profile, hit, _metadata = cached_team_profile(
                reader, "2026-07-21", 0x1000, 1, past, {}, persisted=persisted,
            )

        self.assertTrue(hit)
        self.assertEqual(profile["squad_strength"], 100.0)
        rebuild.assert_not_called()

    def test_snapshot_profile_can_cross_game_date_when_signatures_still_match(self) -> None:
        past = [_result(20, 2, 1)]
        form_signature = team_form_signature(past, 1, {})
        snapshot = {
            "save_instance_id": "save-1",
            "model_version": MODEL_VERSION,
            "model_cache": {
                "version": MODEL_CACHE_VERSION,
                "game_date": "2026-07-20",
                "process_id": 26,
                "module_base": "0x100000",
                "profiles": [{
                    "team_address": "0x1000", "team_id": 1,
                    "form_signature": form_signature,
                    "roster_signature": "roster-a",
                    "profile": _neutral_profile(),
                }],
                "baselines": [],
            },
        }

        persisted, _baselines = reusable_snapshot_model_cache(
            snapshot, "save-1", "2026-07-21",
            process_id=26, module_base=0x100000,
        )

        self.assertIn((0x1000, 1, form_signature, "roster-a"), persisted)

        rejected, _baselines = reusable_snapshot_model_cache(
            snapshot, "save-1", "2026-07-21",
            process_id=27, module_base=0x100000,
        )
        self.assertEqual(rejected, {})

        stale_version = {
            **snapshot,
            "model_cache": {
                **snapshot["model_cache"],
                "version": MODEL_CACHE_VERSION - 1,
            },
        }
        rejected, _baselines = reusable_snapshot_model_cache(
            stale_version, "save-1", "2026-07-21",
            process_id=26, module_base=0x100000,
        )
        self.assertEqual(rejected, {})

    def test_snapshot_team_profile_rebuilds_when_roster_signature_changes(self) -> None:
        past = [_result(20, 2, 1)]
        form_signature = team_form_signature(past, 1, {})
        persisted = {
            (0x1000, 1, form_signature, "roster-a"): _neutral_profile(),
        }
        reader = SimpleNamespace(roster_model_signature=lambda _address: "roster-b")
        rebuilt = {**_neutral_profile(), "squad_strength": 88.0}
        with (
            patch("tools.preview_cup_odds._runtime_state", return_value={"profiles": {}}),
            patch("tools.preview_cup_odds.team_profile", return_value=rebuilt) as rebuild,
        ):
            profile, hit, _metadata = cached_team_profile(
                reader, "2026-07-21", 0x1000, 1, past, {}, persisted=persisted,
            )

        self.assertFalse(hit)
        self.assertEqual(profile["squad_strength"], 88.0)
        rebuild.assert_called_once()

    def test_profile_rebuild_reuses_the_signature_squad_snapshot(self) -> None:
        squad = [{
            "id": 1, "ca": 150, "fitness_percent": 98.0,
            "sharpness_percent": 90.0, "morale_raw": 15,
            "availability": None,
        }]
        reader = SimpleNamespace(
            roster_model_snapshot=lambda _address: ("roster-live", squad),
        )
        rebuilt = {**_neutral_profile(), "squad_strength": 99.0}
        with (
            patch("tools.preview_cup_odds._runtime_state", return_value={"profiles": {}}),
            patch("tools.preview_cup_odds.team_profile", return_value=rebuilt) as build,
        ):
            profile, hit, metadata = cached_team_profile(
                reader, "2026-07-21", 0x1000, 1, [], {}, persisted={},
            )

        self.assertFalse(hit)
        self.assertEqual(profile["squad_strength"], 99.0)
        self.assertEqual(metadata["roster_signature"], "roster-live")
        self.assertIs(build.call_args.kwargs["squad"], squad)

    def test_recent_ten_matches_use_segment_weights(self) -> None:
        scores = [3, 3, 3, 2, 2, 2, 2, 1, 1, 1]
        past = [
            _result(20 - index, score, 1)
            for index, score in enumerate(scores)
        ]

        profile = team_profile(_RosterReader(), 0x1000, 1, past)

        self.assertEqual(profile["recent_matches"], 10)
        self.assertAlmostEqual(profile["recent_coverage"], 1.0)
        self.assertAlmostEqual(profile["recent_goals_for"], 2.3)
        self.assertAlmostEqual(profile["recent_goals_against"], 1.0)

    def test_three_matches_cover_half_and_normalize_average(self) -> None:
        profile = team_profile(
            _RosterReader(), 0x1000, 1,
            [_result(20, 3, 0), _result(19, 2, 0), _result(18, 1, 0)],
        )

        self.assertEqual(profile["recent_matches"], 3)
        self.assertAlmostEqual(profile["recent_coverage"], 0.5)
        self.assertAlmostEqual(profile["recent_goals_for"], 2.0)

    def test_standings_profiles_include_league_goal_average_and_elo(self) -> None:
        rows = [
            {
                "date": "2026-07-01", "competition_id": 7,
                "competition_name": "League", "competition_kind": "league",
                "home": {"id": 1, "name": "A"}, "away": {"id": 2, "name": "B"},
                "home_goals": 3, "away_goals": 1,
            },
            {
                "date": "2026-07-02", "competition_id": 7,
                "competition_name": "League", "competition_kind": "league",
                "home": {"id": 2, "name": "B"}, "away": {"id": 1, "name": "A"},
                "home_goals": 0, "away_goals": 2,
            },
        ]

        profiles = standings_model_profiles(rows)

        self.assertAlmostEqual(profiles[(7, 1)]["league_average_goals"], 1.5)
        self.assertGreater(profiles[(7, 1)]["elo"], profiles[(7, 2)]["elo"])

    def test_standings_attack_defence_and_elo_move_goals_in_right_direction(self) -> None:
        home = _neutral_profile()
        away = _neutral_profile()
        baseline_home, baseline_away = expected_goals(home, away, 1.45, 1.25)
        home_table = {
            "played": 10, "goals_for": 25, "goals_against": 7,
            "elo": 1580, "elo_matches": 10, "league_average_goals": 1.4,
        }
        away_table = {
            "played": 10, "goals_for": 7, "goals_against": 25,
            "elo": 1420, "elo_matches": 10, "league_average_goals": 1.4,
        }

        adjusted_home, adjusted_away = expected_goals(
            home, away, 1.45, 1.25,
            home_standings=home_table, away_standings=away_table,
        )

        self.assertGreater(adjusted_home, baseline_home)
        self.assertLess(adjusted_away, baseline_away)

    def test_team_reputation_is_bounded_directional_signal(self) -> None:
        home = _neutral_profile()
        away = _neutral_profile()
        baseline_home, baseline_away = expected_goals(home, away, 1.45, 1.25)

        adjusted_home, adjusted_away = expected_goals(
            home, away, 1.45, 1.25,
            home_reputation=8000, away_reputation=2000,
        )

        self.assertGreater(adjusted_home, baseline_home)
        self.assertLess(adjusted_away, baseline_away)
        self.assertLessEqual(adjusted_home / baseline_home, 1.084)
        self.assertGreaterEqual(adjusted_away / baseline_away, 0.923)

    def test_star_four_other_seven_and_bench_eleven_drive_strength(self) -> None:
        reader = _RosterReader()
        reader.players = [
            {
                "id": index,
                "ca": 180 if index <= 4 else 150 if index <= 11 else 100,
                "fitness_percent": 100,
                "sharpness_percent": 100,
                "morale_raw": 15,
                "availability": None,
            }
            for index in range(1, 23)
        ]

        profile = team_profile(reader, 0x1000, 1, [])

        self.assertEqual(profile["star_top4_ca"], 180.0)
        self.assertEqual(profile["starting_other7_ca"], 150.0)
        self.assertEqual(profile["bench_11_ca"], 100.0)
        self.assertEqual(profile["weighted_raw_ca"], 155.0)
        self.assertEqual(profile["raw_ca_strength"], 155.0)
        self.assertEqual(profile["squad_strength"], 155.0)

    def test_star_ca_change_outweighs_same_bench_ca_change(self) -> None:
        base = [
            {
                "id": index,
                "ca": 180 if index <= 4 else 150 if index <= 11 else 100,
                "fitness_percent": 100,
                "sharpness_percent": 100,
                "morale_raw": 15,
                "availability": None,
            }
            for index in range(1, 23)
        ]
        baseline = team_profile(_RosterReader(), 0x1000, 1, [], squad=base)
        star_changed = [dict(player) for player in base]
        star_changed[0]["ca"] += 10
        bench_changed = [dict(player) for player in base]
        bench_changed[-1]["ca"] += 10

        star = team_profile(_RosterReader(), 0x1000, 1, [], squad=star_changed)
        bench = team_profile(_RosterReader(), 0x1000, 1, [], squad=bench_changed)

        star_effect = star["weighted_raw_ca"] - baseline["weighted_raw_ca"]
        bench_effect = bench["weighted_raw_ca"] - baseline["weighted_raw_ca"]
        self.assertGreater(star_effect, bench_effect * 6)

    def test_unavailable_player_is_excluded_from_tiered_strength(self) -> None:
        reader = _RosterReader()
        reader.players = [
            {
                "id": index,
                "ca": 200 if index == 1 else 100,
                "fitness_percent": 100,
                "sharpness_percent": 100,
                "morale_raw": 15,
                "availability": {
                    "injury_count": 1 if index == 1 else 0,
                    "ban_count": 0,
                },
            }
            for index in range(1, 23)
        ]

        profile = team_profile(reader, 0x1000, 1, [])

        self.assertEqual(profile["eligible_players"], 21)
        self.assertEqual(profile["candidate_players"], 21)
        self.assertEqual(profile["weighted_raw_ca"], 100.0)

    def test_strength_and_form_edges_are_capped(self) -> None:
        self.assertAlmostEqual(STRENGTH_LOG_COEFFICIENT, 0.0145)
        self.assertAlmostEqual(STRENGTH_LOG_EDGE_CAP, 0.825)
        self.assertAlmostEqual(RECENT_FORM_LOG_EDGE_CAP, 0.225)
        home = {
            **_neutral_profile(),
            "squad_strength": 250.0,
            "recent_matches": 10, "recent_coverage": 1.0,
            "recent_goals_for": 6.0, "recent_goals_against": 0.0,
            "recent_adjusted_goal_difference": 6.0,
        }
        away = {
            **_neutral_profile(),
            "squad_strength": 0.0,
            "recent_matches": 10, "recent_coverage": 1.0,
            "recent_goals_for": 0.0, "recent_goals_against": 6.0,
            "recent_adjusted_goal_difference": -6.0,
        }

        home_xg, away_xg = expected_goals(home, away, 1.5, 1.5)

        self.assertAlmostEqual(
            math.log(home_xg / away_xg),
            2 * (min(
                min(STRENGTH_LOG_COEFFICIENT * 50, STRENGTH_LOG_EDGE_CAP)
                + EXTREME_STRENGTH_LOG_COEFFICIENT * EXTREME_STRENGTH_GAP_TAIL_CAP,
                STRENGTH_LOG_EDGE_CAP,
            ) + RECENT_FORM_LOG_EDGE_CAP),
            places=2,
        )

    def test_extreme_strength_tail_increases_edge_without_affecting_normal_range(self) -> None:
        home = {**_neutral_profile(), "squad_strength": 130.0}
        away = {**_neutral_profile(), "squad_strength": 80.0}
        normal_home, normal_away = expected_goals(home, away, 1.5, 1.5)

        extreme_home = {**home, "squad_strength": 160.0}
        extreme_home_xg, extreme_away_xg = expected_goals(
            extreme_home, away, 1.5, 1.5,
        )

        self.assertGreater(
            math.log(extreme_home_xg / extreme_away_xg),
            math.log(normal_home / normal_away),
        )
        # The tail is bounded; it must not turn a finite mismatch into an
        # unbounded odds ratio.
        self.assertLessEqual(
            math.log(extreme_home_xg / extreme_away_xg),
            2 * STRENGTH_LOG_EDGE_CAP + 0.01,
        )

    def test_team_reputation_is_disabled_when_either_side_is_missing(self) -> None:
        home = _neutral_profile()
        away = _neutral_profile()
        baseline = expected_goals(home, away, 1.45, 1.25)
        adjusted = expected_goals(
            home, away, 1.45, 1.25,
            home_reputation=8000, away_reputation=None,
        )
        self.assertEqual(adjusted, baseline)


if __name__ == "__main__":
    unittest.main()
