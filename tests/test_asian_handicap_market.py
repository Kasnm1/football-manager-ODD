from __future__ import annotations

import unittest
from threading import RLock
from types import SimpleNamespace

from fm_odds_web import LocalOddsState, priced_special_market_odds
from tools.betting_account import _record_settlement_amount, evaluate_leg
from tools.live_market import (
    LIVE_FIFTEEN_MINUTE_PRESSURE,
    LIVE_RANDOM_STRENGTH_MAX,
    LIVE_RANDOM_STRENGTH_MIN,
    LIVE_START_PRESSURE,
    LIVE_ODDS_DROP_CAP,
    LIVE_ODDS_RISE_CAP,
    _cached_market_profile,
    _cached_market_trajectory,
    _cached_live_market_surface,
    _live_handicap_market,
    _market_profile,
    live_market_quote,
    market_output,
    market_trajectory,
)
from tools.preview_cup_odds import (
    BTTS_MARGIN,
    CLEAN_SHEET_MARGIN,
    DOUBLE_CHANCE_MARGIN,
    EXACT_GOALS_ODDS_DIVISOR,
    EXACT_SCORE_ODDS_DIVISOR,
    EXACT_SCORE_TARGET_RETURN_RATE,
    FIRST_SCORE_MARGIN,
    GOAL_PARITY_MARGIN,
    HALF_FULL_MARGIN,
    HALF_MARKET_TWO_WAY_MARGIN,
    HALF_MARKET_ONE_X_TWO_MARGIN,
    HIGHEST_SCORING_HALF_MARGIN,
    KNOCKOUT_ADVANCE_MARGIN,
    KNOCKOUT_BINARY_MARGIN,
    KNOCKOUT_METHOD_MARGIN,
    ONE_X_TWO_MARGIN,
    TEAM_TOTAL_MARGIN,
    TWO_WAY_MARGIN,
    WINNING_MARGIN_MARGIN,
    asian_handicap_options,
    asian_total_options,
    asian_weights,
    bookmaker_weighted_odds,
    choose_asian_handicap,
    choose_half_total_line,
    choose_total_line,
    ensure_match_market_catalog,
    exact_score_divisor,
    exact_score_grid_mass,
    half_time_markets,
    score_matrix,
    team_total_options,
    total_weights,
)


def _leg(line: float, side: str = "home", odds: float = 2.0) -> dict:
    return {
        "market": "AH",
        "selection_code": side,
        "line": line,
        "odds": odds,
    }


def _result(home: int, away: int) -> dict:
    return {
        "date": "2030-01-02",
        "home_goals": home,
        "away_goals": away,
    }


class AsianHandicapMarketTests(unittest.TestCase):
    def test_static_market_identity_calculations_are_cached(self):
        _cached_market_profile.cache_clear()
        _cached_market_trajectory.cache_clear()
        _cached_live_market_surface.cache_clear()
        match = {
            "fixture_date": "2030-01-02",
            "competition_id": 1,
            "home": {"id": 10},
            "away": {"id": 20},
        }
        early = {"home": 1.85, "draw": 3.4, "away": 4.2}

        first_profile = _market_profile(match, early)
        first_trajectory = market_trajectory(match)
        second_profile = _market_profile(dict(match), dict(early))
        second_trajectory = market_trajectory(dict(match))

        self.assertEqual(second_profile, first_profile)
        self.assertEqual(second_trajectory, first_trajectory)
        self.assertEqual(_cached_market_profile.cache_info().misses, 1)
        self.assertEqual(_cached_market_profile.cache_info().hits, 1)
        self.assertEqual(_cached_market_trajectory.cache_info().misses, 1)
        self.assertEqual(_cached_market_trajectory.cache_info().hits, 1)

    def test_identical_live_probability_surfaces_are_cached(self):
        _cached_live_market_surface.cache_clear()
        match = {
            "fixture_date": "2030-01-02", "competition_id": 1,
            "kickoff_minutes": 900,
            "home": {"id": 10}, "away": {"id": 20},
            "xg": {"home": 1.8, "away": 0.9},
            "casino_1x2": {"home": 1.5, "draw": 4.0, "away": 6.0},
        }
        clock = {"date": "2030-01-02", "minutes": 885}

        live_market_quote(match, clock, include_catalog=True)
        live_market_quote(dict(match), dict(clock), include_catalog=True)

        self.assertEqual(_cached_live_market_surface.cache_info().misses, 1)
        self.assertEqual(_cached_live_market_surface.cache_info().hits, 1)

    def test_every_fixture_has_a_live_market_focus(self):
        price_sets = (
            {"home": 2.4, "draw": 3.2, "away": 2.9},
            {"home": 3.1, "draw": 3.2, "away": 2.2},
            {"home": 2.8, "draw": 3.2, "away": 2.8},
        )
        for early in price_sets:
            for team_id in range(1, 501):
                match = {
                    "fixture_date": "2030-01-02",
                    "competition_id": 1,
                    "home": {"id": team_id},
                    "away": {"id": team_id + 1000},
                }
                focuses, _strength = _market_profile(match, early)
                self.assertTrue(focuses)
                self.assertTrue(set(focuses) <= {"home", "draw", "away"})

        self.assertEqual(LIVE_ODDS_DROP_CAP, 0.15)
        self.assertEqual(LIVE_ODDS_RISE_CAP, 0.20)
        self.assertEqual(LIVE_FIFTEEN_MINUTE_PRESSURE, 0.30)

    def test_focus_probability_support_multiplier_range(self):
        self.assertAlmostEqual(
            1.0 + LIVE_START_PRESSURE * LIVE_RANDOM_STRENGTH_MIN,
            1.05,
        )
        self.assertAlmostEqual(
            1.0 + LIVE_FIFTEEN_MINUTE_PRESSURE * LIVE_RANDOM_STRENGTH_MAX,
            1.30,
        )

    def test_market_margin_configuration_matches_pricing_policy(self):
        self.assertEqual(ONE_X_TWO_MARGIN, 0.055)
        self.assertEqual(TWO_WAY_MARGIN, 0.05)
        self.assertEqual(TEAM_TOTAL_MARGIN, 0.055)
        self.assertEqual(HALF_MARKET_ONE_X_TWO_MARGIN, 0.085)
        self.assertEqual(HALF_MARKET_TWO_WAY_MARGIN, 0.08)
        self.assertEqual(BTTS_MARGIN, 0.07)
        self.assertEqual(DOUBLE_CHANCE_MARGIN, 0.07)
        self.assertEqual(CLEAN_SHEET_MARGIN, 0.09)
        self.assertEqual(FIRST_SCORE_MARGIN, 0.08)
        self.assertEqual(GOAL_PARITY_MARGIN, 0.03)
        self.assertEqual(HALF_FULL_MARGIN, 0.17)
        self.assertEqual(WINNING_MARGIN_MARGIN, 0.19)
        self.assertEqual(HIGHEST_SCORING_HALF_MARGIN, 0.07)
        self.assertEqual(KNOCKOUT_ADVANCE_MARGIN, 0.09)
        self.assertEqual(KNOCKOUT_BINARY_MARGIN, 0.11)
        self.assertEqual(KNOCKOUT_METHOD_MARGIN, 0.19)
        self.assertEqual(EXACT_SCORE_ODDS_DIVISOR, 1.26)
        self.assertEqual(EXACT_GOALS_ODDS_DIVISOR, 1.17)

        self.assertEqual(priced_special_market_odds(0.2), 4.27)
        self.assertEqual(
            priced_special_market_odds(0.2, divisor=EXACT_SCORE_ODDS_DIVISOR), 3.97,
        )
        self.assertEqual(priced_special_market_odds(0.2, "fair"), 5.0)

    def test_full_time_score_market_return_rate_target(self):
        self.assertEqual(EXACT_SCORE_TARGET_RETURN_RATE, 0.73)
        for home_xg, away_xg in ((1.2, 0.8), (1.5, 1.1), (1.8, 1.4), (2.2, 1.6)):
            matrix = score_matrix(home_xg, away_xg)
            divisor = exact_score_divisor(
                exact_score_grid_mass(matrix), matrix[0][0],
            )
            overround = sum(
                1.0 / priced_special_market_odds(
                    matrix[home][away],
                    divisor=(
                        EXACT_GOALS_ODDS_DIVISOR
                        if home == 0 and away == 0
                        else divisor
                    ),
                )
                for home in range(6)
                for away in range(6)
            )
            self.assertTrue(0.72 <= 1.0 / overround <= 0.75)

    def test_half_time_markets_use_dedicated_higher_margin(self):
        markets = half_time_markets(1.7, 1.1)
        matrix = score_matrix(markets["xg"]["home"], markets["xg"]["away"])

        one_x_two_overround = sum(1 / price for price in markets["casino_1x2"].values())
        self.assertAlmostEqual(one_x_two_overround, 1.085, delta=0.005)

        handicap = markets["asian_handicap"]
        home_handicap_weights = asian_weights(matrix, handicap["home_line"], "home")
        self.assertEqual(
            handicap["home_odds"],
            bookmaker_weighted_odds(home_handicap_weights, HALF_MARKET_TWO_WAY_MARGIN),
        )
        self.assertLess(
            handicap["home_odds"], bookmaker_weighted_odds(home_handicap_weights),
        )

        totals = markets["total_goals"]
        over_weights = total_weights(matrix, totals["line"], True)
        self.assertEqual(
            totals["over_odds"],
            bookmaker_weighted_odds(over_weights, HALF_MARKET_TWO_WAY_MARGIN),
        )
        self.assertLess(totals["over_odds"], bookmaker_weighted_odds(over_weights))

        second = markets["second_half"]
        second_overround = sum(1 / price for price in second["casino_1x2"].values())
        self.assertAlmostEqual(second_overround, 1.085, delta=0.005)
        second_matrix = score_matrix(second["xg"]["home"], second["xg"]["away"], rho=0.0)
        second_handicap = second["asian_handicap"]
        second_home_weights = asian_weights(
            second_matrix, second_handicap["home_line"], "home",
        )
        self.assertEqual(
            second_handicap["home_odds"],
            bookmaker_weighted_odds(second_home_weights, HALF_MARKET_TWO_WAY_MARGIN),
        )
        self.assertLess(
            second_handicap["home_odds"], bookmaker_weighted_odds(second_home_weights),
        )

        symmetric = half_time_markets(1.0, 1.0)
        home_minus_half = next(
            item for item in symmetric["handicap_options"]
            if item["home_line"] == -0.5
        )
        away_minus_half = next(
            item for item in symmetric["handicap_options"]
            if item["away_line"] == -0.5
        )
        self.assertEqual(
            home_minus_half["home_odds"], symmetric["casino_1x2"]["home"],
        )
        self.assertEqual(
            away_minus_half["away_odds"], symmetric["casino_1x2"]["away"],
        )

    def test_main_line_uses_quarter_grid_with_five_line_ladder(self):
        matrix = score_matrix(1.7, 0.9)
        main_line = choose_asian_handicap(matrix)[0]
        options = asian_handicap_options(matrix, main_line)

        self.assertEqual(len(options), 5)
        self.assertEqual([item["home_line"] for item in options], [-1.25, -1.0, -0.75, -0.5, -0.25])
        self.assertEqual([item["is_main"] for item in options], [False, False, True, False, False])
        self.assertTrue(all(item["home_odds"] >= 1.01 for item in options))
        self.assertTrue(all(item["away_odds"] >= 1.01 for item in options))

    def test_handicap_main_line_is_not_capped_for_large_strength_gaps(self):
        strong_home_matrix = score_matrix(6.0, 0.2)
        strong_away_matrix = score_matrix(0.2, 6.0)

        home_line = choose_asian_handicap(strong_home_matrix)[0]
        away_line = choose_asian_handicap(strong_away_matrix)[0]

        self.assertEqual(home_line, -5.5)
        self.assertEqual(away_line, 5.5)
        self.assertEqual(
            [item["home_line"] for item in asian_handicap_options(strong_home_matrix, home_line)],
            [-6.0, -5.75, -5.5, -5.25, -5.0],
        )
        self.assertEqual(
            [item["is_main"] for item in asian_handicap_options(strong_home_matrix, home_line)],
            [False, False, True, False, False],
        )

    def test_total_main_line_is_not_capped_for_high_scoring_matches(self):
        matrix = score_matrix(4.503, 0.237)

        main_line = choose_total_line(matrix)[0]
        options = asian_total_options(matrix, main_line)

        self.assertEqual(main_line, 4.5)
        self.assertEqual(
            [item["line"] for item in options],
            [4.0, 4.25, 4.5, 4.75, 5.0],
        )
        self.assertEqual(
            [item["is_main"] for item in options],
            [False, False, True, False, False],
        )

        very_high_matrix = score_matrix(6.0, 1.0)
        very_high_main = choose_total_line(very_high_matrix)[0]
        self.assertEqual(very_high_main, 6.75)
        self.assertEqual(choose_half_total_line(very_high_matrix)[0], 6.75)
        self.assertEqual(
            [item["line"] for item in asian_total_options(very_high_matrix, very_high_main)],
            [6.25, 6.5, 6.75, 7.0, 7.25],
        )

    def test_team_total_main_line_is_not_capped(self):
        strong_home_matrix = score_matrix(6.0, 0.2)
        strong_away_matrix = score_matrix(0.2, 6.0)

        home_options = team_total_options(strong_home_matrix, "home")
        away_options = team_total_options(strong_away_matrix, "away")

        self.assertEqual(
            [item["line"] for item in home_options],
            [5.25, 5.5, 5.75, 6.0, 6.25],
        )
        self.assertEqual(
            [item["is_main"] for item in home_options],
            [False, False, True, False, False],
        )
        self.assertEqual(
            [item["line"] for item in away_options],
            [5.25, 5.5, 5.75, 6.0, 6.25],
        )

    def test_live_pressure_reselects_the_main_line(self):
        match = {"xg": {"home": 1.7, "away": 0.9}}

        supported_home = _live_handicap_market(
            match, {"home": 1.35, "draw": 5.0, "away": 9.0},
        )
        supported_away = _live_handicap_market(
            match, {"home": 4.8, "draw": 3.6, "away": 1.8},
        )

        self.assertEqual(supported_home["asian_handicap"]["home_line"], -1.25)
        self.assertEqual(supported_away["asian_handicap"]["home_line"], 0.5)
        self.assertEqual(len(supported_home["handicap_options"]), 5)

        symmetric_prices = {"home": 3.05, "draw": 3.05, "away": 3.05}
        symmetric = _live_handicap_market(
            {"xg": {"home": 1.0, "away": 1.0}}, symmetric_prices,
        )
        home_minus_half = next(
            item for item in symmetric["handicap_options"]
            if item["home_line"] == -0.5
        )
        away_minus_half = next(
            item for item in symmetric["handicap_options"]
            if item["away_line"] == -0.5
        )
        self.assertEqual(home_minus_half["home_odds"], symmetric_prices["home"])
        self.assertEqual(away_minus_half["away_odds"], symmetric_prices["away"])

    def test_future_match_keeps_early_line(self):
        matrix = score_matrix(1.7, 0.9)
        main_line, home_odds, away_odds = choose_asian_handicap(matrix)
        match = {
            "fixture_date": "2030-01-02",
            "competition_id": 1,
            "home": {"id": 10},
            "away": {"id": 20},
            "kickoff_minutes": 900,
            "xg": {"home": 1.7, "away": 0.9},
            "casino_1x2": {"home": 1.8, "draw": 3.6, "away": 4.8},
            "asian_handicap": {
                "home_line": main_line, "home_odds": home_odds,
                "away_line": -main_line, "away_odds": away_odds,
            },
        }

        quote = live_market_quote(match, {"date": "2030-01-01", "minutes": 1200})

        self.assertEqual(quote["phase"], "early")
        self.assertEqual(quote["handicap_options"], [])
        self.assertEqual(quote["handicap_movement"]["current_home_line"], main_line)

    def test_live_catalog_reprices_every_matrix_dependent_market(self):
        match = {
            "fixture_date": "2030-01-02", "competition_id": 1,
            "kickoff_minutes": 900,
            "home": {"id": 10, "name": "Home", "profile": {}},
            "away": {"id": 20, "name": "Away", "profile": {}},
            "xg": {"home": 1.8, "away": 0.9},
            "casino_1x2": {"home": 1.5, "draw": 4.0, "away": 6.0},
        }
        self.assertTrue(ensure_match_market_catalog(match))
        early_total = dict(match["total_goals"])
        early_btts = dict(match["btts"])
        early_half = dict(match["half_time"]["casino_1x2"])
        clock = {"date": "2030-01-02", "minutes": 885}

        quote = live_market_quote(match, clock, include_catalog=True)
        catalog = quote["market_catalog"]
        public_match = market_output({"matches": [match]}, clock)["matches"][0]

        self.assertEqual(quote["phase"], "live")
        for key in (
            "asian_handicap", "total_goals", "team_total_options", "btts",
            "goal_parity", "first_score", "double_chance", "winning_margin",
            "clean_sheet", "win_to_nil", "half_time",
        ):
            self.assertIn(key, catalog)
        self.assertNotEqual(catalog["total_goals"], early_total)
        self.assertNotEqual(catalog["btts"], early_btts)
        self.assertNotEqual(catalog["half_time"]["casino_1x2"], early_half)
        self.assertEqual(public_match["total_goals"], catalog["total_goals"])
        self.assertEqual(public_match["btts"], catalog["btts"])
        self.assertEqual(public_match["half_time"], catalog["half_time"])

        live_matrix = quote["score_matrix"]
        home_probability = sum(
            probability
            for home_goals, row in enumerate(live_matrix)
            for away_goals, probability in enumerate(row)
            if home_goals > away_goals
        )
        self.assertAlmostEqual(
            1.0 / catalog["fair_1x2"]["home"], home_probability, delta=0.002,
        )
        home_minus_half = next(
            item for item in catalog["handicap_options"]
            if item["home_line"] == -0.5
        )
        self.assertEqual(home_minus_half["home_odds"], quote["odds"]["home"])

    def test_backend_uses_live_catalog_for_detailed_market_prices(self):
        match = {
            "fixture_date": "2030-01-02", "competition_id": 1,
            "kickoff_minutes": 900, "competition_name": "测试联赛",
            "home": {"id": 10, "name": "主队", "profile": {}},
            "away": {"id": 20, "name": "客队", "profile": {}},
            "xg": {"home": 1.8, "away": 0.9},
            "casino_1x2": {"home": 1.5, "draw": 4.0, "away": 6.0},
        }
        self.assertTrue(ensure_match_market_catalog(match))
        output = {"matches": [match], "managed_teams": [{"id": 10}]}
        clock = {"date": "2030-01-02", "minutes": 885}
        quote = live_market_quote(match, clock, include_catalog=True)
        catalog = quote["market_catalog"]
        base = {
            "fixture_date": "2030-01-02", "competition_id": 1,
            "home_id": 10, "away_id": 20, "pricing_mode": "casino",
        }

        btts = LocalOddsState._validated_leg(output, {
            **base, "market": "BTTS", "selection_code": "yes",
        }, clock)
        total_line = catalog["total_goals"]["line"]
        over = LocalOddsState._validated_leg(output, {
            **base, "market": "OU", "selection_code": "over", "line": total_line,
        }, clock)
        half_home = LocalOddsState._validated_leg(output, {
            **base, "market": "HT_1X2", "selection_code": "home",
        }, clock)
        exact_score = LocalOddsState._validated_leg(output, {
            **base, "market": "SCORE", "selection_code": "exact", "line": "1-0",
        }, clock)

        live_over = next(
            item for item in catalog["total_options"]
            if item["line"] == total_line
        )
        self.assertEqual(btts["odds"], catalog["btts"]["yes"])
        self.assertEqual(over["odds"], live_over["over_odds"])
        self.assertEqual(
            half_home["odds"], catalog["half_time"]["casino_1x2"]["home"],
        )
        live_matrix = quote["score_matrix"]
        self.assertEqual(
            exact_score["odds"],
            priced_special_market_odds(
                live_matrix[1][0],
                divisor=exact_score_divisor(
                    exact_score_grid_mass(live_matrix), live_matrix[0][0],
                ),
            ),
        )
        self.assertNotEqual(btts["odds"], match["btts"]["yes"])

    def test_match_markets_api_returns_the_live_detailed_catalog(self):
        match = {
            "fixture_date": "2030-01-02", "competition_id": 1,
            "kickoff_minutes": 900,
            "home": {"id": 10, "name": "主队", "profile": {}},
            "away": {"id": 20, "name": "客队", "profile": {}},
            "xg": {"home": 1.8, "away": 0.9},
            "casino_1x2": {"home": 1.5, "draw": 4.0, "away": 6.0},
        }
        self.assertTrue(ensure_match_market_catalog(match))
        early_btts = dict(match["btts"])
        state = SimpleNamespace(
            lock=RLock(), output={"matches": [match]}, data_version=7,
            last_connection_clock={"date": "2030-01-02", "minutes": 885},
        )

        response = LocalOddsState.match_markets(state, {
            "fixture_key": "2030-01-02|1|10|20",
        })

        self.assertEqual(response["data_version"], 7)
        self.assertEqual(response["match"]["market_phase"], "live")
        self.assertNotEqual(response["match"]["btts"], early_btts)
        self.assertIn("half_time", response["match"])

    def test_quarter_line_outcomes_and_returns(self):
        cases = (
            (_leg(-0.25), _result(1, 1), "half_lost", 0.5),
            (_leg(0.25), _result(1, 1), "half_won", 1.5),
            (_leg(-0.75), _result(2, 1), "half_won", 1.5),
            (_leg(0.75), _result(0, 1), "half_lost", 0.5),
        )
        for leg, result, outcome, multiplier in cases:
            with self.subTest(line=leg["line"], score=(result["home_goals"], result["away_goals"])):
                evaluation = evaluate_leg(leg, result)
                self.assertEqual(evaluation["outcome"], outcome)
                self.assertEqual(evaluation["return_multiplier"], multiplier)

    def test_quarter_total_outcomes_and_returns(self):
        cases = (
            ({"market": "OU", "selection_code": "over", "line": 2.25, "odds": 2.0}, _result(1, 1), "half_lost", 0.5),
            ({"market": "OU", "selection_code": "under", "line": 2.25, "odds": 2.0}, _result(1, 1), "half_won", 1.5),
            ({"market": "OU", "selection_code": "over", "line": 2.75, "odds": 2.0}, _result(2, 1), "half_won", 1.5),
            ({"market": "OU", "selection_code": "under", "line": 2.75, "odds": 2.0}, _result(2, 1), "half_lost", 0.5),
        )
        for leg, result, outcome, multiplier in cases:
            with self.subTest(code=leg["selection_code"], line=leg["line"]):
                evaluation = evaluate_leg(leg, result)
                self.assertEqual(evaluation["outcome"], outcome)
                self.assertEqual(evaluation["return_multiplier"], multiplier)

    def test_cached_fixed_lines_are_upgraded_with_extended_markets(self):
        match = {
            "xg": {"home": 2.1, "away": 0.8},
            "handicap_options": [
                {"home_line": -0.5}, {"home_line": -1.5}, {"home_line": -2.5},
            ],
        }

        self.assertTrue(ensure_match_market_catalog(match))
        self.assertEqual(match["market_catalog_state"], "full")
        self.assertTrue(match["markets_loaded"])
        self.assertEqual(len(match["handicap_options"]), 5)
        self.assertEqual(
            [item["home_line"] for item in match["handicap_options"]],
            sorted(item["home_line"] for item in match["handicap_options"]),
        )
        self.assertEqual(len(match["total_options"]), 5)
        self.assertEqual(len(match["team_total_options"]["home"]), 5)
        self.assertEqual(len(match["team_total_options"]["away"]), 5)
        for key in ("double_chance", "winning_margin", "clean_sheet", "win_to_nil"):
            self.assertIn(key, match)
        for key in ("btts", "goal_parity", "first_score"):
            self.assertIn(key, match)
            self.assertTrue(match[key])
        self.assertIn("handicap_options", match["half_time"])
        self.assertIn("highest_scoring_half", match["half_time"])
        self.assertEqual(len(match["half_time"]["total_options"]), 5)
        self.assertEqual(len(match["half_time"]["second_half"]["handicap_options"]), 5)
        self.assertEqual(len(match["half_time"]["second_half"]["total_options"]), 5)

    def test_new_score_and_half_markets_settle_from_existing_result(self):
        result = {
            **_result(3, 0),
            "half_home_goals": 2,
            "half_away_goals": 0,
        }
        winning_legs = (
            {"market": "DOUBLE_CHANCE", "selection_code": "home_draw", "odds": 1.2},
            {"market": "WINNING_MARGIN", "selection_code": "home_3_plus", "odds": 4.0},
            {"market": "CLEAN_SHEET", "selection_code": "home_yes", "odds": 2.0},
            {"market": "WIN_TO_NIL", "selection_code": "home", "odds": 3.0},
            {"market": "HIGHEST_SCORING_HALF", "selection_code": "first", "odds": 3.0},
        )
        self.assertTrue(all(evaluate_leg(leg, result)["outcome"] == "won" for leg in winning_legs))

        half_draw = {**_result(1, 1), "half_home_goals": 0, "half_away_goals": 0}
        half_handicap = {"market": "HT_AH", "selection_code": "home", "line": -0.25, "odds": 2.0}
        self.assertEqual(evaluate_leg(half_handicap, half_draw)["outcome"], "half_lost")

    def test_exact_six_and_seven_plus_do_not_overlap(self):
        result = {
            **_result(3, 3),
            "half_home_goals": 2,
            "half_away_goals": 1,
        }
        legs = (
            {"market": "HT_TOTAL_GOALS", "selection_code": "exact", "line": 3, "odds": 4.0},
            {"market": "HT_TOTAL_GOALS", "selection_code": "three_plus", "line": 3, "odds": 2.5},
            {"market": "TOTAL_GOALS", "selection_code": "exact", "line": 6, "odds": 8.0},
        )
        self.assertTrue(all(evaluate_leg(leg, result)["outcome"] == "won" for leg in legs))
        seven_plus = {"market": "TOTAL_GOALS", "selection_code": "seven_plus", "line": 7, "odds": 5.0}
        self.assertEqual(evaluate_leg(seven_plus, result)["outcome"], "lost")
        self.assertEqual(evaluate_leg(seven_plus, _result(4, 3))["outcome"], "won")

        legacy_six_plus = {"market": "TOTAL_GOALS", "selection_code": "six_plus", "line": 6, "odds": 5.0}
        self.assertEqual(evaluate_leg(legacy_six_plus, result)["outcome"], "won")

    def test_half_time_quarter_total_settles_half_outcomes(self):
        result = {
            **_result(2, 1),
            "half_home_goals": 1,
            "half_away_goals": 0,
        }
        over = {"market": "HT_OU", "selection_code": "over", "line": 1.25, "odds": 2.0}
        under = {"market": "HT_OU", "selection_code": "under", "line": 1.25, "odds": 2.0}
        self.assertEqual(evaluate_leg(over, result)["outcome"], "half_lost")
        self.assertEqual(evaluate_leg(under, result)["outcome"], "half_won")

    def test_second_half_result_and_quarter_lines_use_remaining_goals(self):
        result = {
            **_result(3, 2),
            "half_home_goals": 1,
            "half_away_goals": 1,
        }
        legs = (
            ({"market": "SH_1X2", "selection_code": "home", "odds": 2.0}, "won"),
            ({"market": "SH_AH", "selection_code": "home", "line": -0.75, "odds": 2.0}, "half_won"),
            ({"market": "SH_OU", "selection_code": "over", "line": 2.75, "odds": 2.0}, "half_won"),
        )
        for leg, expected in legs:
            with self.subTest(market=leg["market"]):
                self.assertEqual(evaluate_leg(leg, result)["outcome"], expected)

    def test_second_half_exact_markets_use_only_second_half_goals(self):
        result = {
            **_result(4, 3),
            "half_home_goals": 2,
            "half_away_goals": 1,
        }
        winning_legs = (
            {"market": "SH_TEAM_GOALS", "selection_code": "home_plus", "line": 2, "odds": 2.0},
            {"market": "SH_TEAM_GOALS", "selection_code": "away_plus", "line": 2, "odds": 2.0},
            {"market": "SH_TOTAL_GOALS", "selection_code": "three_plus", "line": 3, "odds": 2.0},
            {"market": "SH_SCORE", "selection_code": "exact", "line": "2-2", "odds": 5.0},
        )
        self.assertTrue(all(evaluate_leg(leg, result)["outcome"] == "won" for leg in winning_legs))

    def test_team_goal_quarter_totals_use_selected_team_score(self):
        result = _result(2, 1)
        home_over = {"market": "TEAM_OU", "selection_code": "home_over", "line": 1.75, "odds": 2.0}
        away_under = {"market": "TEAM_OU", "selection_code": "away_under", "line": 1.25, "odds": 2.0}
        self.assertEqual(evaluate_leg(home_over, result)["outcome"], "half_won")
        self.assertEqual(evaluate_leg(away_under, result)["outcome"], "half_won")

    def test_backend_reprices_new_market_selections(self):
        match = {
            "fixture_date": "2030-01-02", "competition_id": 1,
            "kickoff_minutes": 900, "competition_name": "测试联赛",
            "home": {"id": 10, "name": "主队", "profile": {}},
            "away": {"id": 20, "name": "客队", "profile": {}},
            "xg": {"home": 1.8, "away": 0.9},
            "casino_1x2": {"home": 1.5, "draw": 4.0, "away": 6.0},
        }
        self.assertTrue(ensure_match_market_catalog(match))
        output = {"matches": [match], "managed_teams": [{"id": 10}]}
        clock = {"date": "2030-01-01", "minutes": 600}
        base = {
            "fixture_date": "2030-01-02", "competition_id": 1,
            "home_id": 10, "away_id": 20, "pricing_mode": "casino",
        }
        second = LocalOddsState._validated_leg(output, {
            **base, "market": "SH_TEAM_GOALS", "selection_code": "away_plus", "line": 2,
        }, clock)
        team_total = LocalOddsState._validated_leg(output, {
            **base, "market": "TEAM_OU", "selection_code": "home_over",
            "line": match["team_total_options"]["home"][0]["line"],
        }, clock)
        exact_six = LocalOddsState._validated_leg(output, {
            **base, "market": "TOTAL_GOALS", "selection_code": "exact", "line": 6,
        }, clock)
        seven_plus = LocalOddsState._validated_leg(output, {
            **base, "market": "TOTAL_GOALS", "selection_code": "seven_plus", "line": 7,
        }, clock)
        with self.assertRaisesRegex(ValueError, "总进球数选项无效"):
            LocalOddsState._validated_leg(output, {
                **base, "market": "TOTAL_GOALS", "selection_code": "six_plus", "line": 6,
            }, clock)
        self.assertGreaterEqual(second["odds"], 1.01)
        self.assertGreaterEqual(team_total["odds"], 1.01)
        self.assertEqual(exact_six["selection"], "总进球 6")
        self.assertEqual(seven_plus["selection"], "总进球 7+")
        self.assertEqual(second["integrity"]["integrity_pattern"], "managed_market_monitor")
        self.assertFalse(second["integrity"]["monitoring_suspicious"])
        self.assertFalse(team_total["integrity"]["monitoring_suspicious"])
        self.assertTrue(exact_six["integrity"]["monitoring_suspicious"])
        self.assertTrue(seven_plus["integrity"]["monitoring_suspicious"])

    def test_equivalent_no_goal_markets_use_one_price(self):
        match = {
            "fixture_date": "2030-01-02", "competition_id": 1,
            "kickoff_minutes": 900, "competition_name": "Test League",
            "home": {"id": 10, "name": "Home", "profile": {}},
            "away": {"id": 20, "name": "Away", "profile": {}},
            "xg": {"home": 1.8, "away": 0.9},
            "casino_1x2": {"home": 1.5, "draw": 4.0, "away": 6.0},
        }
        self.assertTrue(ensure_match_market_catalog(match))
        output = {"matches": [match], "managed_teams": [{"id": 10}]}
        clock = {"date": "2030-01-01", "minutes": 600}
        base = {
            "fixture_date": "2030-01-02", "competition_id": 1,
            "home_id": 10, "away_id": 20, "pricing_mode": "casino",
        }

        def price(market: str, line, code: str = "exact") -> float:
            return LocalOddsState._validated_leg(output, {
                **base, "market": market, "selection_code": code, "line": line,
            }, clock)["odds"]

        full_no_goal = price("FIRST_SCORE", None, "none")
        self.assertEqual(match["first_score"]["none"], full_no_goal)
        self.assertEqual(price("TOTAL_GOALS", 0), full_no_goal)
        self.assertEqual(price("SCORE", "0-0"), full_no_goal)
        self.assertEqual(price("HT_TOTAL_GOALS", 0), price("HT_SCORE", "0-0"))
        self.assertEqual(price("SH_TOTAL_GOALS", 0), price("SH_SCORE", "0-0"))
        home_minus_half = next(
            item for item in match["handicap_options"]
            if item["home_line"] == -0.5
        )
        self.assertEqual(home_minus_half["home_odds"], match["casino_1x2"]["home"])

    def test_half_results_apply_to_single_parlay_and_system_returns(self):
        half_win_leg = _leg(-0.75)
        half_win = evaluate_leg(half_win_leg, _result(2, 1))
        winning_leg = {"market": "1X2", "selection_code": "home", "odds": 2.0}
        full_win = evaluate_leg(winning_leg, _result(2, 1))

        status, payout, multiplier, _ = _record_settlement_amount(
            {"type": "single", "stake": 100.0, **half_win_leg}, [half_win],
        )
        self.assertEqual((status, payout, multiplier), ("won", 150.0, 1.5))

        status, payout, multiplier, _ = _record_settlement_amount(
            {"type": "parlay", "stake": 100.0, "legs": [half_win_leg, winning_leg]},
            [half_win, full_win],
        )
        self.assertEqual((status, payout, multiplier), ("won", 300.0, 3.0))

        losing_leg = {"market": "1X2", "selection_code": "away", "odds": 3.0}
        full_loss = evaluate_leg(losing_leg, _result(2, 1))
        record = {
            "type": "system_parlay", "stake": 20.0, "unit_stake": 10.0,
            "pass_code": "2X1", "combination_count": 2,
            "legs": [half_win_leg, losing_leg, winning_leg],
            "groups": [
                {"selections": [half_win_leg, losing_leg]},
                {"selections": [winning_leg]},
            ],
        }
        status, payout, multiplier, summary = _record_settlement_amount(
            record, [half_win, full_loss, full_win],
        )
        self.assertEqual((status, payout, multiplier), ("won", 30.0, 3.0))
        self.assertEqual(summary["winning_combinations"], 1)


if __name__ == "__main__":
    unittest.main()
