from __future__ import annotations

import json
import unittest
import threading
import struct
from collections import Counter
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from types import SimpleNamespace

from conftest import legacy_web_source

from fm_odds_web import (
    LocalOddsState,
    publish_championship_markets,
)
from tools.betting_account import settle_championship_bets
from tools.championship_odds import (
    CHAMPIONSHIP_MARGIN,
    CHAMPIONSHIP_HIGH_ODDS_LADDER,
    CHAMPIONSHIP_MAX_ODDS,
    CHAMPIONSHIP_MODEL_VERSION,
    CHAMPIONSHIP_PRICE_CACHE_LIMIT,
    FAMOUS_LEAGUES,
    TEAM_REPUTATION_LOG_COEFFICIENT,
    _current_knockout_contenders,
    _confirmed_league_table,
    _discovered_cup_configs,
    _discovered_league_configs,
    _final_result_winner_id,
    _league_phase_contenders,
    _live_league_ratings,
    _league_ratings,
    _native_season_calendar,
    _result_rows,
    _ratings,
    _display_championship_odds,
    _simulate_cup_prices,
    _simulate_prices,
    _spread_cup_longshots,
    _spread_league_longshots,
    _weighted_tail_tier_counts,
    build_championship_markets,
    championship_season_window,
    pending_cup_final_result_keys,
)
from tools.preview_cup_odds import (
    STAGE_TYPE_MARKERS,
    competition_metadata,
    is_national_qualification_competition,
    _native_stage_teams,
    native_competition_format_snapshots,
    native_retained_league_format_snapshots,
    native_result_competition_format_snapshots,
)


def _profile(ca: float) -> dict:
    return {
        "candidate_players": 20,
        "candidate_13_ca": ca + 2,
        "candidate_20_ca": ca,
        "weighted_raw_ca": ca,
    }


def _output(team_count: int = 20) -> dict:
    teams = [
        {"id": index, "name": f"球队{index}", "reputation": 5000 + index * 100, "profile": _profile(100 + index)}
        for index in range(1, team_count + 1)
    ]
    matches = []
    for index in range(0, len(teams), 2):
        if index + 1 >= len(teams):
            break
        matches.append({
            "competition_id": 11,
            "fixture_date": "2026-08-08",
            "home": teams[index],
            "away": teams[index + 1],
        })
    return {"game_date": "2026-08-01", "matches": matches}


def _standings(team_count: int = 20, played: int = 0) -> dict:
    return {"competitions": [{
        "competition_id": 11,
        "competition_name": "英超联赛",
        "teams": [
            {
                "team_id": index,
                "team_name": f"球队{index}",
                "played": played,
                "points": (team_count - index) * 3 if played else 0,
                "goal_difference": team_count - index,
                "elo": 1500 + index,
                "position": index,
            }
            for index in range(1, team_count + 1)
        ],
    }]}


def _cup_output(game_date: str = "2026-09-01", team_count: int = 36) -> dict:
    teams = [
        {"id": index, "name": f"欧冠球队{index}", "reputation": 6000 + index * 80, "profile": _profile(105 + index)}
        for index in range(1, team_count + 1)
    ]
    matches = []
    for index in range(0, len(teams), 2):
        if index + 1 >= len(teams):
            break
        matches.append({
            "competition_id": 1301394, "competition_name": "欧冠联赛",
            "fixture_date": "2026-09-10", "home": teams[index], "away": teams[index + 1],
        })
    return {
        "game_date": game_date, "save_instance_id": "save-1",
        "matches": matches, "season_results": [],
        "competitions": [{"id": 1301394, "name": "欧冠联赛"}],
        "competition_formats": [{
            "competition_id": 1301394,
            "competition_name": "欧冠联赛",
            "competition_kind": "cup",
            "opening_slot_count": 36,
            "opening_field_complete": team_count == 36,
            "opening_teams": teams,
            "opening_stage_type": "league",
            "first_fixture_date": "2026-09-10",
            "knockout_slot_count": 24,
            "knockout_round_tie_counts": [8, 8, 4, 2, 1],
            "has_terminal_cup_stage": True,
        }],
    }


class ChampionshipOddsTests(unittest.TestCase):
    def test_national_qualification_keeps_match_market_but_not_championship(self) -> None:
        self.assertTrue(is_national_qualification_competition("世界杯亚洲区预选赛"))
        self.assertTrue(is_national_qualification_competition("World Cup qualifying"))
        self.assertFalse(is_national_qualification_competition("FIFA World Cup"))
        self.assertFalse(is_national_qualification_competition("欧冠附加赛"))
        self.assertEqual(
            competition_metadata(
                100102, "世界杯亚洲区预选赛", national_teams=True,
            ),
            {
                "id": 100102,
                "name": "世界杯亚洲区预选赛",
                "kind": "national",
                "reputation": 96,
            },
        )
        self.assertEqual(
            competition_metadata(100103, "世界杯南美区预选赛")["kind"],
            "national",
        )

    def test_national_qualification_group_stage_does_not_open_championship_market(self) -> None:
        output = _cup_output(team_count=48)
        format_row = output["competition_formats"][0]
        format_row.update({
            "competition_id": 100102,
            "competition_name": "世界杯亚洲区预选赛",
            "competition_kind": "national",
            "opening_stage_type": "group",
            "opening_slot_count": 48,
            "opening_field_complete": True,
            "has_terminal_cup_stage": False,
        })
        output["competitions"] = [{"id": 100102, "name": "世界杯亚洲区预选赛"}]
        self.assertNotIn(100102, _discovered_cup_configs(output))
        diagnostics = build_championship_markets(
            output, {"competitions": []},
        )["discovery_diagnostics"]
        row = next(item for item in diagnostics if item["competition_id"] == 100102)
        self.assertEqual(row["reason"], "qualification_competition_excluded")

    def test_national_qualification_cannot_fall_back_to_league_market(self) -> None:
        output = {
            "game_date": "2026-08-01",
            "competitions": [{
                "id": 100102, "name": "世界杯亚洲区预选赛", "kind": "league",
            }],
            "competition_formats": [{
                "competition_id": 100102,
                "competition_name": "世界杯亚洲区预选赛",
                "competition_kind": "league",
                "opening_stage_type": "league",
            }],
        }
        standings = {"competitions": [{
            "competition_id": 100102,
            "competition_name": "世界杯亚洲区预选赛",
            "teams": [
                {"team_id": index, "team_name": f"国家队{index}", "played": 1}
                for index in range(1, 7)
            ],
        }]}
        self.assertNotIn(100102, _discovered_league_configs(output, standings))

    def test_price_cache_covers_more_than_one_large_competition_catalog(self) -> None:
        self.assertGreaterEqual(CHAMPIONSHIP_PRICE_CACHE_LIMIT, 256)

    def test_empty_or_invalid_cup_bracket_does_not_crash_refresh(self) -> None:
        self.assertEqual(_simulate_cup_prices([], 0, 0, "empty"), [])
        teams = [
            {
                "team_id": index, "team_name": f"Team {index}",
                "reputation": 5000, "profile": _profile(120),
            }
            for index in range(1, 3)
        ]
        self.assertEqual(_simulate_cup_prices(teams, 2, 1, "odd-bracket"), [])

    def test_large_cup_tail_tiers_accept_every_remainder(self) -> None:
        for tail_size in range(10, 40):
            counts = _weighted_tail_tier_counts(tail_size)
            self.assertEqual(len(counts), 4)
            self.assertEqual(sum(counts), tail_size)
            self.assertTrue(all(count >= 0 for count in counts))
        self.assertEqual(_weighted_tail_tier_counts(10), [4, 3, 2, 1])
        self.assertEqual(_weighted_tail_tier_counts(14), [5, 4, 3, 2])
        self.assertEqual(_weighted_tail_tier_counts(15), [6, 4, 3, 2])

        teams = [
            {
                "team_id": index, "team_name": f"Large cup team {index}",
                "reputation": 5000 + index, "profile": _profile(115 + index / 10),
            }
            for index in range(1, 53)
        ]
        with patch("tools.championship_odds.CHAMPIONSHIP_SIMULATIONS", 20):
            prices = _simulate_cup_prices(
                teams, qualifiers=32, byes=0, seed="large-cup-tail",
            )
        self.assertEqual(len(prices), 52)

    def test_stale_persisted_cup_contenders_are_rebuilt_without_blocking(self) -> None:
        output = _cup_output()
        with patch("tools.preview_cup_odds.read_result_history", return_value=[]):
            opened = build_championship_markets(output, {"competitions": []})
            self.assertTrue(opened["competitions"])

            state_path = Path(self._state_directory.name) / "championship_cups.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            record = next(iter(state["seasons"].values()))
            record["contender_team_ids"] = [999_999_991]
            record["knockout_started"] = True
            state_path.write_text(json.dumps(state), encoding="utf-8")

            repaired = build_championship_markets(output, {"competitions": []})
            self.assertFalse(any(
                item.get("competition_id") == 1301394
                for item in repaired["competitions"]
            ))
            persisted = json.loads(state_path.read_text(encoding="utf-8"))
            repaired_record = next(iter(persisted["seasons"].values()))
            self.assertNotIn("contender_team_ids", repaired_record)
            self.assertNotIn("knockout_started", repaired_record)

            rebuilt = build_championship_markets(output, {"competitions": []})
            self.assertTrue(any(
                item.get("competition_id") == 1301394
                for item in rebuilt["competitions"]
            ))

    def test_league_longshots_use_sparse_strength_ordered_tail(self) -> None:
        names = [
            "Napoli", "Inter", "Milan", "Juventus", "Atalanta", "Fiorentina",
            "Cremonese", "Cesena", "Bologna", "Parma", "Lazio", "Pisa",
            "Cagliari", "Sassuolo", "Como", "Roma",
        ]
        points = [24, 22, 21, 18, 8, 6, 3, 4, 14, 12, 12, 10, 3, 16, 5, 10]
        played = [9, 8, 9, 8, 9, 8, 8, 8, 8, 8, 9, 8, 8, 8, 8, 8]
        cas = [160, 165, 160, 158, 150, 148, 130, 132, 148, 138, 150, 133, 135, 142, 140, 155]
        reputations = [8500, 9200, 8800, 9000, 7800, 7500, 5000, 4800, 7200, 5800, 8000, 5200, 5400, 6500, 6200, 8400]
        elos = [1650, 1720, 1680, 1660, 1560, 1530, 1400, 1380, 1580, 1450, 1600, 1420, 1430, 1500, 1480, 1620]
        teams = [
            {
                "team_id": index + 1,
                "team_name": name,
                "played": played[index],
                "points": points[index],
                "elo": elos[index],
                "reputation": reputations[index],
                "profile": {"candidate_20_ca": cas[index]},
            }
            for index, name in enumerate(names)
        ]

        prices = _simulate_prices(
            teams, expected_matches=30, seed="serie-a-tail", simulations=1500,
        )
        odds = {str(team["team_name"]): float(team["odds"]) for team in prices}
        counts = Counter(float(team["odds"]) for team in prices)

        self.assertTrue(all(float(team["champion_probability"]) > 0 for team in prices))
        self.assertGreaterEqual(odds["Inter"], 2.0)
        self.assertLess(odds["Atalanta"], 501.0)
        self.assertLess(odds["Bologna"], 101.0)
        self.assertLessEqual(odds["Sassuolo"], 151.0)
        self.assertLessEqual(counts[1001.0], 1)
        self.assertEqual(counts[1501.0], 0)
        self.assertEqual(counts[2001.0], 0)
        self.assertEqual(CHAMPIONSHIP_MODEL_VERSION, "championship-v25")

    def test_realistic_league_field_keeps_a_sparse_high_odds_tail(self) -> None:
        cas = [166, 164, 160, 156, 153, 150, 148, 146, 144, 142,
               140, 138, 136, 134, 132, 130, 128, 126, 123, 120]
        reputations = [9400, 9200, 8900, 8500, 8200, 7900, 7600, 7300,
                       7000, 6800, 6600, 6400, 6200, 6000, 5800, 5600,
                       5400, 5200, 5000, 4700]
        teams = [
            {
                "team_id": index + 1,
                "team_name": f"League team {index + 1}",
                "played": 0,
                "points": 0,
                "elo": 1660 - index * 15,
                "reputation": reputations[index],
                "profile": {"candidate_20_ca": cas[index]},
            }
            for index in range(20)
        ]

        prices = _simulate_prices(
            teams, expected_matches=38, seed="realistic-league-tail",
            simulations=6_000,
        )
        odds = sorted(float(team["odds"]) for team in prices)

        self.assertGreaterEqual(odds[-1], 501.0)
        self.assertLessEqual(sum(value >= 501.0 for value in odds), 3)
        self.assertGreaterEqual(sum(101.0 <= value <= 451.0 for value in odds), 5)
        self.assertLess(odds[len(odds) // 2], 501.0)

    def test_league_tail_has_no_1501_or_2001_and_at_most_one_1001(self) -> None:
        prices = [
            {"team_id": 1, "odds": 501.0, "champion_probability": 0.0010, "strength_rating": 1.0, "points": 5},
            {"team_id": 2, "odds": 1001.0, "champion_probability": 0.0008, "strength_rating": 0.5, "points": 4},
            {"team_id": 3, "odds": 1501.0, "champion_probability": 0.0005, "strength_rating": 0.0, "points": 3},
            {"team_id": 4, "odds": 2001.0, "champion_probability": 0.0002, "strength_rating": -1.0, "points": 2},
        ]

        _spread_league_longshots(prices)

        self.assertEqual(
            [float(team["odds"]) for team in prices],
            [401.0, 451.0, 501.0, 1001.0],
        )

    def test_32_team_cup_tail_limits_each_higher_tier(self) -> None:
        prices = [
            {
                "team_id": team_id,
                "odds": 2001.0 if team_id <= 5 else 1501.0,
                "champion_probability": team_id / 1_000_000,
                "strength_rating": float(team_id),
            }
            for team_id in range(1, 13)
        ] + [
            {
                "team_id": team_id,
                "odds": 101.0,
                "champion_probability": 0.01,
                "strength_rating": float(team_id),
            }
            for team_id in range(13, 33)
        ]

        _spread_cup_longshots(prices)
        counts = Counter(float(team["odds"]) for team in prices)

        self.assertEqual(
            [counts[tier] for tier in (501.0, 1001.0, 1501.0, 2001.0)],
            [2, 2, 2, 1],
        )
        self.assertEqual(
            {team["team_id"] for team in prices if team["odds"] == 2001.0},
            {1},
        )

    def test_league_reputation_floor_repairs_implausibly_low_squad_ca(self) -> None:
        teams = [
            {
                "team_id": 1, "team_name": "High reputation",
                "reputation": 8500, "elo": 1520,
                "profile": {"candidate_20_ca": 101},
            },
            {
                "team_id": 2, "team_name": "Low reputation",
                "reputation": 7000, "elo": 1500,
                "profile": {"candidate_20_ca": 125},
            },
        ]

        cup_ratings = _ratings(teams)
        league_ratings = _league_ratings(teams)

        self.assertLess(cup_ratings[1], cup_ratings[2])
        self.assertGreater(league_ratings[1], league_ratings[2])

    def test_championship_ratings_prefer_tiered_raw_ca(self) -> None:
        teams = [
            {
                "team_id": 1, "reputation": 7000, "elo": 1500,
                "profile": {"candidate_20_ca": 100, "weighted_raw_ca": 140},
            },
            {
                "team_id": 2, "reputation": 7000, "elo": 1500,
                "profile": {"candidate_20_ca": 120, "weighted_raw_ca": 110},
            },
        ]

        self.assertGreater(_ratings(teams)[1], _ratings(teams)[2])
        self.assertGreater(_league_ratings(teams)[1], _league_ratings(teams)[2])

    def test_live_league_points_narrow_preseason_strength_gap(self) -> None:
        teams = [
            {
                "team_id": 1, "team_name": "Preseason favorite",
                "played": 8, "points": 18, "goal_difference": 12,
                "reputation": 9000, "elo": 1547,
                "profile": {"candidate_20_ca": 165},
            },
            {
                "team_id": 2, "team_name": "Current leader",
                "played": 9, "points": 19, "goal_difference": 6,
                "reputation": 7600, "elo": 1532,
                "profile": {"candidate_20_ca": 148},
            },
            {
                "team_id": 3, "team_name": "Average team",
                "played": 8, "points": 10, "goal_difference": -1,
                "reputation": 6500, "elo": 1490,
                "profile": {"candidate_20_ca": 138},
            },
            {
                "team_id": 4, "team_name": "Bottom team",
                "played": 9, "points": 4, "goal_difference": -12,
                "reputation": 5500, "elo": 1440,
                "profile": {"candidate_20_ca": 125},
            },
        ]

        preseason = _league_ratings(teams)
        live = _live_league_ratings(teams, preseason)

        self.assertLess(
            live[1] - live[2],
            preseason[1] - preseason[2],
        )
        self.assertGreater(live[2], preseason[2])
        self.assertLess(live[4], preseason[4])

    def setUp(self) -> None:
        self._state_directory = TemporaryDirectory()
        self.addCleanup(self._state_directory.cleanup)
        state_patch = patch(
            "tools.championship_odds.cup_state_path",
            return_value=Path(self._state_directory.name) / "championship_cups.json",
        )
        state_patch.start()
        self.addCleanup(state_patch.stop)
        league_state_patch = patch(
            "tools.championship_odds.league_state_path",
            return_value=Path(self._state_directory.name) / "championship_leagues.json",
        )
        league_state_patch.start()
        self.addCleanup(league_state_patch.stop)

    def test_cross_year_and_calendar_year_windows(self) -> None:
        self.assertEqual(CHAMPIONSHIP_MARGIN, 0.205)
        self.assertEqual(
            _native_season_calendar(
                "2027-08-31", "2027-03-01", "league", True,
            ),
            "calendar_year",
        )
        self.assertEqual(
            _native_season_calendar(
                "2027-08-31", "2027-08-10", "league", True,
            ),
            "cross_year",
        )

    def test_unconfirmed_result_is_invisible_until_added_to_snapshot(self) -> None:
        leaked = {
            "date": "2026-08-01", "competition_id": 11,
            "home": {"id": 1}, "away": {"id": 2},
            "home_goals": 2, "away_goals": 0, "winner_side": "home",
        }
        output = {
            "game_date": "2026-08-01", "save_instance_id": "save-1",
            "season_results": [],
        }
        with patch("tools.preview_cup_odds.read_result_history", return_value=[leaked]):
            self.assertEqual(_result_rows(output), [])
            output["season_results"] = [leaked]
            self.assertEqual(_result_rows(output), [leaked])

    def test_due_same_day_result_history_is_visible_after_scheduled_end(self) -> None:
        result = {
            "date": "2026-08-01", "competition_id": 11,
            "home": {"id": 1}, "away": {"id": 2},
            "home_goals": 2, "away_goals": 0, "winner_side": "home",
        }
        output = {
            "game_date": "2026-08-01", "game_time": "16:44",
            "save_instance_id": "save-1", "season_results": [],
            "matches": [{
                "fixture_date": "2026-08-01", "kickoff_minutes": 900,
                "competition_id": 11,
                "home": {"id": 1}, "away": {"id": 2},
            }],
        }
        with patch("tools.preview_cup_odds.read_result_history", return_value=[result]):
            self.assertEqual(_result_rows(output), [])
            output["game_time"] = "16:45"
            self.assertEqual(_result_rows(output), [result])

    def test_trusted_settlement_result_is_included_without_history(self) -> None:
        result = {
            "date": "2026-08-01", "competition_id": 11,
            "home": {"id": 1}, "away": {"id": 2},
            "home_goals": 2, "away_goals": 0, "winner_side": "home",
        }
        output = {
            "game_date": "2026-08-01", "save_instance_id": "save-1",
            "season_results": [], "settlement_results": [result],
        }
        with patch("tools.preview_cup_odds.read_result_history", return_value=[]):
            self.assertEqual(_result_rows(output), [result])

    def test_league_table_ignores_one_unconfirmed_preloaded_match(self) -> None:
        table = {
            1: {"team_id": 1, "played": 2, "points": 6, "goal_difference": 3},
            2: {"team_id": 2, "played": 2, "points": 0, "goal_difference": -3},
        }
        confirmed = [{
            "date": "2026-07-31", "competition_id": 11,
            "home": {"id": 1}, "away": {"id": 2},
            "home_goals": 1, "away_goals": 0, "winner_side": "home",
        }]
        effective = _confirmed_league_table(
            table, confirmed, 11, "2026-07-01", "2027-06-30",
        )
        self.assertEqual(effective[1]["played"], 1)
        self.assertEqual(effective[1]["points"], 3)
        self.assertEqual(effective[2]["played"], 1)
        self.assertEqual(effective[2]["points"], 0)

    def test_league_table_accepts_newer_confirmed_result_history(self) -> None:
        table = {
            1: {"team_id": 1, "played": 0, "points": 0, "goal_difference": 0},
            2: {"team_id": 2, "played": 0, "points": 0, "goal_difference": 0},
        }
        confirmed = [{
            "date": "2026-07-31", "competition_id": 11,
            "home": {"id": 1}, "away": {"id": 2},
            "home_goals": 1, "away_goals": 0, "winner_side": "home",
        }]

        effective = _confirmed_league_table(
            table, confirmed, 11, "2026-07-01", "2027-06-30",
        )

        self.assertEqual(effective[1]["played"], 1)
        self.assertEqual(effective[1]["points"], 3)
        self.assertEqual(effective[2]["played"], 1)

    def test_scheduled_knockout_teams_override_preloaded_native_elimination(self) -> None:
        participants = {1: {"team_id": 1}, 2: {"team_id": 2}}
        output = {
            "game_date": "2026-08-01",
            "competition_formats": [{
                "competition_id": 99,
                "knockout_active_team_ids": [1],
                "knockout_eliminated_team_ids": [2],
            }],
            "matches": [{
                "fixture_date": "2026-08-02", "competition_id": 99,
                "home": {"id": 1}, "away": {"id": 2},
                "competition_round": {
                    "stage_index": 1, "round_index": 1,
                    "round_tie_count": 1, "tie_address": 100,
                },
            }],
        }
        contenders = _current_knockout_contenders(
            output, 99, participants, {}, {"byes": 0},
        )
        self.assertEqual(contenders, {1, 2})
        self.assertEqual(CHAMPIONSHIP_MAX_ODDS, 2001.0)
        self.assertEqual(TEAM_REPUTATION_LOG_COEFFICIENT, 5.0)
        self.assertEqual(
            championship_season_window("2026-08-01", "cross_year"),
            ("2026/27", "2026-07-01", "2027-06-30"),
        )

    def test_fm24_league_stage_reads_standings_team_vector(self) -> None:
        stage = 0x1000
        standings = [0x3000, 0x3100, 0x3200]
        team_addresses = [0x4000, 0x4100, 0x4200]
        native_rows = [
            struct.pack("<HHh6B", 30, 10, 37, 16, 16, 11, 4, 1, 0),
            struct.pack("<HHh6B", 22, 16, 31, 16, 16, 9, 4, 3, 0),
            struct.pack("<HHh6B", 25, 21, 28, 16, 16, 9, 1, 6, 0),
        ]
        previous_rows = [
            struct.pack("<HHh6B", 29, 10, 34, 15, 15, 10, 4, 1, 0),
            struct.pack("<HHh6B", 21, 15, 30, 15, 15, 9, 3, 3, 0),
            struct.pack("<HHh6B", 22, 19, 25, 15, 15, 8, 1, 6, 0),
        ]

        class FakeReader:
            layout = SimpleNamespace(key="fm24")

            def bytes(self, address, size):
                if address == stage + 0x98 and size == 0x18:
                    return struct.pack("<QQQ", 0x2000, 0x2018, 0x2018)
                for standing, native_row, previous_row in zip(
                    standings, native_rows, previous_rows,
                ):
                    if address == standing + 0x08 and size == 12:
                        return native_row
                    if address == standing + 0x50 and size == 12:
                        return previous_row
                return None

            def ptr_array(self, address, count):
                return standings if address == 0x2000 and count == 3 else []

            def ptr(self, address):
                mapping = {
                    standing + 0x78: team_address
                    for standing, team_address in zip(standings, team_addresses)
                }
                return mapping.get(address)

            def team(self, address):
                if address not in team_addresses:
                    return None
                index = team_addresses.index(address) + 1
                return {
                    "id": index, "name": f"球队{index}", "short_name": f"球队{index}",
                    "reputation": 5000 + index, "address": hex(address),
                }

        teams, slots = _native_stage_teams(FakeReader(), stage, "league")
        self.assertEqual(slots, 3)
        self.assertEqual([team["id"] for team in teams], [1, 2, 3])
        self.assertEqual(
            (teams[0]["played"], teams[0]["points"], teams[0]["position"]),
            (16, 37, 1),
        )
        self.assertTrue(teams[0]["native_standing_verified"])
        self.assertEqual(teams[0]["native_standing_address"], hex(standings[0]))
        self.assertEqual(
            championship_season_window("2026-08-01", "calendar_year"),
            ("2026", "2026-01-01", "2026-12-31"),
        )

    def test_completed_result_recovers_native_league_format_outside_odds_window(self) -> None:
        season_address = 0x178ADA8D0

        class FakeReader:
            def competition(self, address):
                if address != season_address:
                    return None
                return {
                    "id": 130931,
                    "name": "中国足球超级联赛",
                    "short_name": "中超联赛",
                    "address": hex(address),
                }

        recovered = [{"competition_id": 130931, "opening_stage_type": "league"}]
        with patch(
            "tools.preview_cup_odds.native_competition_format_snapshots",
            return_value=recovered,
        ) as native_snapshots:
            result = native_result_competition_format_snapshots(FakeReader(), [{
                "date": "2023-07-01",
                "competition": {
                    "id": 130931,
                    "name": "中国足球超级联赛",
                    "address": hex(season_address),
                },
            }], {
                season_address: (date(2023, 4, 15), date(2023, 11, 4)),
            })

        self.assertEqual(result, recovered)
        _reader, representatives, bounds = native_snapshots.call_args.args
        self.assertEqual(len(representatives), 1)
        self.assertEqual(
            representatives[0][0].competition_season, season_address,
        )
        self.assertEqual(
            bounds[season_address], (date(2023, 4, 15), date(2023, 11, 4)),
        )

    def test_retained_league_season_refreshes_frozen_final_table(self) -> None:
        season_address = 0x178ADA8D0
        actual_address = 0x178B00000
        retained = [{
            "competition_id": 130931,
            "competition_name": "中国足球超级联赛",
            "competition_kind": "league",
            "competition_season_address": hex(season_address),
            "actual_competition_address": hex(actual_address),
            "first_fixture_date": "2028-03-01",
            "last_fixture_date": "2028-11-30",
            "opening_teams": [{"id": 1, "played": 33}],
        }]

        class FakeReader:
            def competition(self, address):
                if address != season_address:
                    return None
                return {
                    "id": 130931,
                    "name": "中国足球超级联赛",
                    "address": hex(address),
                }

        refreshed = [{
            **retained[0],
            "opening_teams": [{"id": 1, "played": 34}],
        }]
        with patch(
            "tools.preview_cup_odds.native_competition_format_snapshots",
            return_value=refreshed,
        ) as native_snapshots:
            result = native_retained_league_format_snapshots(
                FakeReader(), retained, "2029-01-31",
            )

        self.assertEqual(result[0]["opening_teams"][0]["played"], 34)
        _reader, representatives, bounds = native_snapshots.call_args.args
        self.assertEqual(
            representatives[0][0].competition_season, season_address,
        )
        self.assertEqual(
            bounds[season_address], (date(2028, 3, 1), date(2028, 11, 30)),
        )

    def test_retained_league_season_rejects_reused_actual_object(self) -> None:
        season_address = 0x178ADA8D0

        class FakeReader:
            def competition(self, address):
                return {
                    "id": 130931,
                    "name": "中超联赛",
                    "address": hex(address),
                }

        retained = [{
            "competition_id": 130931,
            "competition_name": "中超联赛",
            "competition_kind": "league",
            "competition_season_address": hex(season_address),
            "actual_competition_address": "0x2000",
        }]
        with patch(
            "tools.preview_cup_odds.native_competition_format_snapshots",
            return_value=[{
                **retained[0],
                "actual_competition_address": "0x3000",
            }],
        ):
            result = native_retained_league_format_snapshots(
                FakeReader(), retained, "2029-01-31",
            )

        self.assertEqual(result, [])

    def test_fm26_league_stage_uses_generation_specific_vector_and_team_offset(self) -> None:
        stage = 0x5000
        standing = 0x6000
        team_address = 0x7000
        native_row = struct.pack(
            "<HHh6B", 28, 14, 40, 18, 18, 12, 4, 2, 0,
        )

        class FakeReader:
            layout = SimpleNamespace(key="fm26")

            def bytes(self, address, size):
                if address == stage + 0xC0 and size == 0x18:
                    return struct.pack("<QQQ", 0x8000, 0x8008, 0x8008)
                if address == standing + 0x08 and size == 12:
                    return native_row
                return None

            def ptr_array(self, address, count):
                return [standing] if address == 0x8000 and count == 1 else []

            def ptr(self, address):
                return team_address if address == standing + 0x88 else None

            def team(self, address):
                if address != team_address:
                    return None
                return {
                    "id": 1190,
                    "name": "柏太阳神",
                    "short_name": "柏太阳神",
                    "reputation": 5000,
                    "address": hex(address),
                }

        teams, slots = _native_stage_teams(FakeReader(), stage, "league")

        self.assertEqual(slots, 1)
        self.assertEqual(
            (teams[0]["id"], teams[0]["played"], teams[0]["points"]),
            (1190, 18, 40),
        )

    def test_csl_calendar_follows_native_season_start_instead_of_competition_id(self) -> None:
        self.assertNotIn(130931, FAMOUS_LEAGUES)
        output = {
            "game_date": "2023-07-03",
            "game_layout": "fm24",
            "matches": [],
            "season_results": [],
            "competitions": [{
                "id": 130931,
                "name": "中超联赛",
                "kind": "league",
                "reputation": 72,
            }],
            "competition_formats": [{
                "competition_id": 130931,
                "competition_kind": "league",
                "opening_stage_type": "league",
                "opening_field_complete": True,
                "opening_slot_count": 16,
                "opening_teams": [
                    {"id": team_id} for team_id in range(1, 17)
                ],
                "first_fixture_date": "2023-04-15",
                "season_fixture_bounds_verified": True,
                "has_terminal_cup_stage": False,
            }],
            "competition_season_starts": {130931: "2023-04-15"},
        }
        standings = {"competitions": [{
            "competition_id": 130931,
            "competition_name": "中超联赛",
            "teams": [
                {"team_id": team_id, "played": 16, "points": 30}
                for team_id in range(1, 17)
            ],
        }]}

        original = _discovered_league_configs(output, standings)[130931]
        output["competition_season_starts"] = {130931: "2023-07-01"}
        patched = _discovered_league_configs(output, standings)[130931]

        self.assertEqual(original["calendar"], "calendar_year")
        self.assertEqual(patched["calendar"], "cross_year")

    def test_native_format_uses_full_season_fixture_bounds(self) -> None:
        fixture = SimpleNamespace(
            competition_season=0x1000,
            stage_index=0,
            match_date=date(2027, 8, 31),
        )

        class FakeReader:
            layout = SimpleNamespace(
                competition_actual_offset=0x10,
                actual_competition_stages_offset=0x20,
                competition_stage_type_offset=0x30,
            )

            def competition(self, address):
                return {"id": 987655, "address": "0x2000"}

            def ptr(self, address):
                return 0x3000 if address == 0x2010 else None

            def bytes(self, address, size):
                return b"LEAG" if address == 0x4030 and size == 4 else None

        teams = [{"id": 1, "name": "Team 1"}, {"id": 2, "name": "Team 2"}]
        rows = [(fixture, {"name": "Calendar League", "kind": "league"}, {}, {})]
        with (
            patch.dict(STAGE_TYPE_MARKERS, {b"LEAG": "league"}, clear=True),
            patch("tools.preview_cup_odds._native_pointer_vector", return_value=[0x4000]),
            patch("tools.preview_cup_odds._native_stage_teams", return_value=(teams, 2)),
        ):
            snapshot = native_competition_format_snapshots(
                FakeReader(), rows,
                {0x1000: (date(2027, 3, 1), date(2027, 11, 30))},
            )[0]

        self.assertEqual(snapshot["first_fixture_date"], "2027-03-01")
        self.assertEqual(snapshot["last_fixture_date"], "2027-11-30")
        self.assertTrue(snapshot["season_fixture_bounds_verified"])

    def test_extreme_underdog_odds_are_capped_at_2001(self) -> None:
        teams = [
            {
                "team_id": 1, "team_name": "强队", "reputation": 10_000,
                "profile": _profile(200), "played": 0, "points": 0, "elo": 1800,
            },
            {
                "team_id": 2, "team_name": "冷门", "reputation": 1,
                "profile": _profile(20), "played": 0, "points": 0, "elo": 1200,
            },
        ]
        prices = _simulate_cup_prices(teams, qualifiers=2, byes=0, seed="odds-cap")
        underdog = next(team for team in prices if team["team_id"] == 2)
        self.assertEqual(underdog["odds"], 2001.0)

    def test_large_cup_tail_has_fewer_teams_at_each_higher_tier(self) -> None:
        teams = [
            {
                "team_id": team_id, "team_name": f"球队{team_id}",
                "reputation": 3000 + team_id * 100,
                "profile": _profile(80 + team_id * 1.5),
                "played": 0, "points": 0, "elo": 1400 + team_id * 4,
            }
            for team_id in range(1, 49)
        ]
        prices = _simulate_cup_prices(
            teams, qualifiers=32, byes=0, seed="large-cup-tail",
        )
        counts = Counter(float(team["odds"]) for team in prices)
        self.assertEqual(
            [counts[tier] for tier in (501.0, 1001.0, 1501.0, 2001.0)],
            [5, 3, 2, 1],
        )
        highest = [team for team in prices if team["odds"] == 2001.0]
        self.assertEqual({team["team_id"] for team in highest}, {1})

    def test_high_championship_odds_use_bookmaker_ladder(self) -> None:
        self.assertEqual(_display_championship_odds(6.74), 6.5)
        self.assertEqual(_display_championship_odds(6.76), 7.0)
        self.assertEqual(_display_championship_odds(23.39), 23.0)
        self.assertEqual(_display_championship_odds(40.40), 40.0)
        self.assertEqual(_display_championship_odds(40.60), 41.0)
        self.assertEqual(_display_championship_odds(43.87), 46.0)
        self.assertEqual(_display_championship_odds(100.01), 101.0)
        self.assertEqual(_display_championship_odds(149.15), 151.0)
        self.assertEqual(_display_championship_odds(993.14), 1001.0)
        self.assertEqual(_display_championship_odds(749.0), 501.0)
        self.assertEqual(_display_championship_odds(880.0), 501.0)
        self.assertEqual(_display_championship_odds(881.0), 1001.0)
        self.assertEqual(_display_championship_odds(1249.0), 1001.0)
        self.assertEqual(_display_championship_odds(1252.0), 1501.0)
        self.assertEqual(_display_championship_odds(1751.0), 2001.0)
        self.assertEqual(_display_championship_odds(9999.0), 2001.0)

    def test_opens_complete_famous_league_field_with_deterministic_prices(self) -> None:
        first = build_championship_markets(_output(), _standings())
        second = build_championship_markets(_output(), _standings())
        self.assertEqual(first, second)
        self.assertEqual(len(first["competitions"]), 1)
        market = first["competitions"][0]
        self.assertEqual(market["competition_id"], 11)
        self.assertEqual(market["status"], "open")
        self.assertEqual(market["season_start"], "2026-08-08")
        self.assertEqual(len(market["teams"]), 20)
        probability = sum(float(team["champion_probability"]) for team in market["teams"])
        self.assertAlmostEqual(probability, 1.0, places=4)
        implied = sum(1.0 / float(team["odds"]) for team in market["teams"])
        self.assertGreater(implied, 1.0)
        self.assertLess(implied, 1.0 + CHAMPIONSHIP_MARGIN + 0.04)

    def test_new_league_field_filters_stale_previous_season_table_teams(self) -> None:
        output = _output()
        current_teams = [
            {**team, "profile": dict(team.get("profile") or {})}
            for match in output["matches"] for team in (match["home"], match["away"])
        ]
        output["competition_formats"] = [{
            "competition_id": 11,
            "competition_kind": "league",
            "opening_stage_type": "league",
            "opening_slot_count": 20,
            "opening_field_complete": True,
            "opening_teams": current_teams,
        }]
        standings = _standings(22)

        market = build_championship_markets(output, standings)["competitions"][0]

        self.assertEqual(market["status"], "open")
        self.assertEqual(
            {int(team["team_id"]) for team in market["teams"]},
            set(range(1, 21)),
        )

    def test_current_season_evidence_repairs_one_wrong_native_league_team(self) -> None:
        competition_id = 987670
        correct_ids = set(range(1, 25))
        wrong_team_id = 999
        opening_teams = [
            {
                "id": team_id, "name": f"Native team {team_id}",
                "reputation": 5000 + team_id,
                "profile": _profile(110 + team_id / 10),
            }
            for team_id in sorted((correct_ids - {24}) | {wrong_team_id})
        ]
        output = {
            "game_date": "2028-10-01",
            "competitions": [{
                "id": competition_id, "name": "Corrected League",
                "kind": "league", "reputation": 45,
            }],
            "competition_formats": [{
                "competition_id": competition_id,
                "competition_name": "Corrected League",
                "competition_kind": "league",
                "opening_stage_type": "league",
                "opening_slot_count": 24,
                "opening_field_complete": True,
                "opening_teams": opening_teams,
                "has_terminal_cup_stage": False,
            }],
            "matches": [],
            "season_results": [{
                "date": "2028-09-30", "competition_id": competition_id,
                "home": {"id": 24, "name": "Missing team"},
                "away": {"id": 1, "name": "Native team 1"},
                "home_goals": 1, "away_goals": 1,
            }],
        }
        standings = {"competitions": [{
            "competition_id": competition_id,
            "competition_name": "Corrected League",
            "teams": [{
                "team_id": team_id,
                "team_name": "Missing team" if team_id == 24 else f"Native team {team_id}",
                "played": 5, "points": 8, "goal_difference": 0,
                "position": team_id,
            } for team_id in sorted(correct_ids)],
        }]}

        market = build_championship_markets(output, standings)["competitions"][0]
        market_ids = {int(team["team_id"]) for team in market["teams"]}

        self.assertEqual(market_ids, correct_ids)
        self.assertNotIn(wrong_team_id, market_ids)
        self.assertEqual(market["participant_source"], "current_season_table_repair")

        partial_standings = json.loads(json.dumps(standings))
        partial_standings["competitions"][0]["teams"] = [
            team for team in partial_standings["competitions"][0]["teams"]
            if int(team["team_id"]) != 24
        ]
        partial_market = build_championship_markets(
            output, partial_standings,
        )["competitions"][0]
        partial_teams = {
            int(team["team_id"]): str(team["team_name"])
            for team in partial_market["teams"]
        }

        self.assertEqual(set(partial_teams), correct_ids)
        self.assertEqual(partial_teams[24], "Missing team")

    def test_unverified_equal_size_table_conflict_keeps_native_league_field(self) -> None:
        output = _output()
        native_teams = [
            {
                "id": team_id, "name": f"Native {team_id}",
                "reputation": 5000 + team_id,
                "profile": _profile(110 + team_id),
            }
            for team_id in list(range(1, 20)) + [999]
        ]
        output["matches"] = []
        output["competition_formats"] = [{
            "competition_id": 11,
            "competition_kind": "league",
            "opening_stage_type": "league",
            "opening_slot_count": 20,
            "opening_field_complete": True,
            "opening_teams": native_teams,
            "has_terminal_cup_stage": False,
        }]

        market = build_championship_markets(output, _standings())["competitions"][0]
        market_ids = {int(team["team_id"]) for team in market["teams"]}

        self.assertIn(999, market_ids)
        self.assertNotIn(20, market_ids)
        self.assertEqual(market["participant_source"], "native_conflict_unverified")

    def test_new_league_field_prices_teams_without_near_term_fixture_profiles(self) -> None:
        output = _output()
        current_teams = [
            {
                "id": index,
                "name": f"Team {index}",
                "reputation": 6000 + index * 50,
            }
            for index in range(1, 21)
        ]
        output["matches"] = output["matches"][:7]
        output["competition_formats"] = [{
            "competition_id": 11,
            "competition_kind": "league",
            "opening_stage_type": "league",
            "opening_slot_count": 20,
            "opening_field_complete": True,
            "opening_teams": current_teams,
        }]

        market = build_championship_markets(
            output, _standings(),
        )["competitions"][0]

        self.assertEqual(len(market["teams"]), 20)
        self.assertTrue(all(float(team["odds"]) > 1 for team in market["teams"]))

    def test_stale_persisted_cup_format_cannot_seed_new_season(self) -> None:
        output = _cup_output(game_date="2027-08-31")
        format_row = output["competition_formats"][0]
        format_row.update({
            "competition_season_address": "0x1000",
            "first_fixture_date": "2027-05-13",
            "format_last_observed_at": "2027-05-13",
        })

        configs = _discovered_cup_configs(output)

        self.assertNotIn(1301394, configs)

    def test_new_native_cup_season_replaces_same_year_stale_record(self) -> None:
        output = _cup_output(game_date="2026-09-01")
        output["competition_formats"][0].update({
            "competition_season_address": "0x2000",
            "format_last_observed_at": "2026-09-01",
        })
        state_path = Path(self._state_directory.name) / "championship_cups.json"
        state_path.write_text(json.dumps({
            "schema_version": 1,
            "seasons": {
                "1301394:2026/27": {
                    "competition_id": 1301394,
                    "competition_name": "Old Cup Snapshot",
                    "season_key": "1301394:2026/27",
                    "season_label": "2026/27",
                    "season_start": "2026-07-01",
                    "season_end": "2027-06-30",
                    "participants": [],
                    "format_config": {
                        "name": "Old Cup Snapshot",
                        "calendar": "cross_year",
                        "participants": 36,
                        "qualifiers": 24,
                        "byes": 8,
                        "competition_season_address": "0x1000",
                    },
                },
            },
        }), encoding="utf-8")

        markets = build_championship_markets(
            output, {"competitions": []},
        )["competitions"]
        market = next(
            item for item in markets if int(item["competition_id"]) == 1301394
        )
        saved = json.loads(state_path.read_text(encoding="utf-8"))

        self.assertEqual(market["status"], "open")
        self.assertEqual(len(market["teams"]), 36)
        self.assertEqual(
            saved["seasons"]["1301394:2026/27"]["format_config"][
                "competition_season_address"
            ],
            "0x2000",
        )

    def test_does_not_open_incomplete_field_or_famous_cup(self) -> None:
        incomplete = build_championship_markets(_output(18), _standings(18))
        self.assertEqual(incomplete["competitions"], [])
        cup_output = {
            "game_date": "2026-08-01",
            "matches": [{
                "competition_id": 1301394,
                "home": {"id": 1, "name": "甲", "reputation": 9000, "profile": _profile(160)},
                "away": {"id": 2, "name": "乙", "reputation": 9000, "profile": _profile(160)},
            }],
        }
        cup_standings = {"competitions": [{
            "competition_id": 1301394, "competition_name": "欧冠联赛", "teams": [],
        }]}
        self.assertEqual(
            build_championship_markets(cup_output, cup_standings)["competitions"],
            [],
        )

    def test_complete_league_uses_unique_native_first_place_for_tiebreak(self) -> None:
        complete_output = _output()
        complete_output["season_results"] = [{
            "date": "2026-07-31", "competition_id": 11,
            "home": {"id": 1}, "away": {"id": 2},
            "home_goals": 2, "away_goals": 0, "winner_side": "home",
        }]
        complete = build_championship_markets(complete_output, _standings(played=38))
        market = complete["competitions"][0]
        self.assertEqual(market["status"], "complete")
        self.assertEqual(market["winner_team_id"], 1)
        tied = _standings(played=38)
        tied["competitions"][0]["teams"][1]["points"] = tied["competitions"][0]["teams"][0]["points"]
        tied_market = build_championship_markets(complete_output, tied)["competitions"][0]
        self.assertEqual(tied_market["status"], "complete")
        self.assertEqual(tied_market["winner_team_id"], 1)

        tied["competitions"][0]["teams"][0]["position"] = 0
        tied["competitions"][0]["teams"][1]["position"] = 0
        unresolved = build_championship_markets(complete_output, tied)["competitions"][0]
        self.assertEqual(unresolved["status"], "awaiting_tiebreak")
        self.assertIsNone(unresolved["winner_team_id"])

    def test_league_removes_mathematically_eliminated_teams(self) -> None:
        market = build_championship_markets(
            _output(), _standings(played=37),
        )["competitions"][0]
        offered_ids = {int(team["team_id"]) for team in market["teams"]}
        self.assertEqual(offered_ids, {1, 2})
        self.assertAlmostEqual(
            sum(float(team["champion_probability"]) for team in market["teams"]),
            1.0,
            places=4,
        )

    def test_league_locks_betting_when_only_one_team_can_still_win(self) -> None:
        standings = _standings(played=37)
        standings["competitions"][0]["teams"][1]["points"] = 53
        market = build_championship_markets(
            _output(), standings,
        )["competitions"][0]
        self.assertEqual(market["status"], "locked")
        self.assertEqual(market["winner_team_id"], 1)
        self.assertEqual(len(market["teams"]), 1)
        self.assertIsNone(market["teams"][0]["odds"])

        confirmed_output = _output()
        confirmed_output["season_results"] = [{
            "date": confirmed_output["game_date"], "competition_id": 11,
            "home": {"id": 1}, "away": {"id": 2},
            "home_goals": 1, "away_goals": 0, "winner_side": "home",
        }]
        completed = build_championship_markets(
            confirmed_output, standings,
        )["competitions"][0]
        self.assertEqual(completed["status"], "complete")
        self.assertEqual(completed["winner_team_id"], 1)
        self.assertEqual(completed["settlement_date"], confirmed_output["game_date"])

    def test_market_model_is_shared_with_fm24_outputs(self) -> None:
        output = _output()
        output["game_layout"] = "fm24"
        market = build_championship_markets(
            output, _standings(),
        )["competitions"][0]
        self.assertEqual(market["status"], "open")
        self.assertEqual(len(market["teams"]), 20)

    def test_single_round_league_settles_after_native_terminal_date(self) -> None:
        teams = [
            {
                "id": team_id, "name": f"Single round {team_id}",
                "reputation": 5000 + team_id * 100,
                "profile": _profile(110 + team_id),
            }
            for team_id in range(1, 11)
        ]
        output = {
            "game_date": "2028-12-01", "game_layout": "fm24",
            "matches": [], "season_results": [],
            "competitions": [{
                "id": 987660, "name": "Single Round League",
                "kind": "league", "reputation": 45,
            }],
            "competition_formats": [{
                "competition_id": 987660,
                "competition_name": "Single Round League",
                "competition_kind": "league",
                "opening_stage_type": "league",
                "opening_slot_count": 10,
                "opening_field_complete": True,
                "opening_teams": teams,
                "has_terminal_cup_stage": False,
                "first_fixture_date": "2028-03-01",
                "season_fixture_bounds_verified": True,
                "last_fixture_date": "2028-11-30",
                "format_last_observed_at": "2028-12-01",
            }],
        }
        standings = {"competitions": [{
            "competition_id": 987660,
            "competition_name": "Single Round League",
            "teams": [{
                "team_id": team_id,
                "team_name": f"Single round {team_id}",
                "played": 9,
                "points": 30 - team_id,
                "goal_difference": 11 - team_id,
                "position": team_id,
            } for team_id in range(1, 11)],
        }]}

        market = build_championship_markets(
            output, standings,
        )["competitions"][0]

        self.assertEqual(market["expected_matches"], 9)
        self.assertEqual(market["status"], "complete")
        self.assertEqual(market["winner_team_id"], 1)
        self.assertEqual(market["settlement_date"], "2028-11-30")

    def test_native_terminal_waits_until_last_round_has_ended(self) -> None:
        teams = [
            {
                "id": team_id, "name": f"Early table {team_id}",
                "reputation": 5000 + team_id * 100,
                "profile": _profile(110 + team_id),
            }
            for team_id in range(1, 11)
        ]
        output = {
            "game_date": "2028-11-30", "game_time": "21:30",
            "matches": [{
                "competition_id": 987661,
                "fixture_date": "2028-11-30",
                "kickoff_minutes": 1200,
                "home": teams[index], "away": teams[index + 1],
            } for index in range(0, 10, 2)],
            "season_results": [],
            "competitions": [{
                "id": 987661, "name": "Early Table League",
                "kind": "league", "reputation": 45,
            }],
            "competition_formats": [{
                "competition_id": 987661,
                "competition_name": "Early Table League",
                "competition_kind": "league",
                "opening_stage_type": "league",
                "opening_slot_count": 10,
                "opening_field_complete": True,
                "opening_teams": teams,
                "has_terminal_cup_stage": False,
                "first_fixture_date": "2028-03-01",
                "season_fixture_bounds_verified": True,
                "last_fixture_date": "2028-11-30",
                "format_last_observed_at": "2028-11-30",
            }],
        }
        standings = {"competitions": [{
            "competition_id": 987661,
            "competition_name": "Early Table League",
            "teams": [{
                "team_id": team_id,
                "team_name": f"Early table {team_id}",
                "played": 9,
                "points": 30 - team_id,
                "goal_difference": 11 - team_id,
            } for team_id in range(1, 11)],
        }]}

        before = build_championship_markets(output, standings)["competitions"][0]
        output["game_time"] = "21:45"
        after = build_championship_markets(output, standings)["competitions"][0]

        self.assertEqual(before["status"], "open")
        self.assertEqual(after["status"], "complete")
        self.assertEqual(after["winner_team_id"], 1)

    def test_nonstandard_multi_round_league_stays_open_past_double_round_count(self) -> None:
        teams = [
            {
                "id": team_id, "name": f"Triple round {team_id}",
                "reputation": 5000 + team_id * 100,
                "profile": _profile(110 + team_id),
            }
            for team_id in range(1, 11)
        ]
        output = {
            "game_date": "2028-11-01", "matches": [], "season_results": [],
            "competitions": [{
                "id": 987662, "name": "Triple Round League",
                "kind": "league", "reputation": 45,
            }],
            "competition_formats": [{
                "competition_id": 987662,
                "competition_name": "Triple Round League",
                "competition_kind": "league",
                "opening_stage_type": "league",
                "opening_slot_count": 10,
                "opening_field_complete": True,
                "opening_teams": teams,
                "has_terminal_cup_stage": False,
                "last_fixture_date": "2028-12-30",
                "format_last_observed_at": "2028-11-01",
                "season_fixture_bounds_verified": True,
            }],
        }
        standings = {"competitions": [{
            "competition_id": 987662,
            "competition_name": "Triple Round League",
            "teams": [{
                "team_id": team_id,
                "team_name": f"Triple round {team_id}",
                "played": 27,
                "points": 50 - team_id,
                "goal_difference": 11 - team_id,
            } for team_id in range(1, 11)],
        }]}

        market = build_championship_markets(
            output, standings,
        )["competitions"][0]

        self.assertEqual(market["status"], "open")
        self.assertEqual(market["expected_matches"], 28)
        self.assertGreaterEqual(len(market["teams"]), 2)

    def test_native_single_table_league_opens_without_name_whitelist(self) -> None:
        output = {
            "game_date": "2026-08-01", "game_layout": "fm24",
            "matches": [], "season_results": [],
            "competitions": [{"id": 987654, "name": "Regional League", "kind": "league", "reputation": 55}],
        }
        standings = {"competitions": [{
            "competition_id": 987654, "competition_name": "Regional League",
            "teams": [{
                "team_id": team_id, "team_name": f"Team {team_id}",
                "played": 2, "points": 6 if team_id == 1 else 3,
                "goal_difference": 3 if team_id == 1 else 0,
                "elo": 1550 - team_id,
                "position": team_id,
            } for team_id in range(1, 11)],
        }]}
        configs = _discovered_league_configs(output, standings)
        self.assertEqual(configs[987654]["teams"], 10)
        self.assertEqual(configs[987654]["matches"], 18)
        market = build_championship_markets(output, standings)["competitions"][0]
        self.assertEqual(market["competition_id"], 987654)
        self.assertEqual(market["market_kind"], "league")
        self.assertEqual(market["status"], "open")
        self.assertEqual(len(market["teams"]), 10)

    def test_native_league_field_opens_before_standings_exist(self) -> None:
        opening_teams = [
            {
                "id": team_id, "name": f"Team {team_id}",
                "reputation": 5000 + team_id * 100,
                "profile": _profile(100 + team_id),
            }
            for team_id in range(1, 11)
        ]
        output = {
            "game_date": "2028-02-01", "game_layout": "fm26",
            "matches": [], "season_results": [],
            "competitions": [{
                "id": 987656, "name": "Native League",
                "kind": "league", "reputation": 55,
            }],
            "competition_formats": [{
                "competition_id": 987656,
                "competition_name": "Native League",
                "competition_kind": "league",
                "opening_stage_type": "league",
                "opening_slot_count": 10,
                "opening_field_complete": True,
                "opening_teams": opening_teams,
                "has_terminal_cup_stage": False,
            }],
        }

        market = build_championship_markets(
            output, {"competitions": []},
        )["competitions"][0]

        self.assertEqual(market["status"], "open")
        self.assertEqual(market["expected_teams"], 10)
        self.assertEqual(len(market["teams"]), 10)

    def test_native_league_field_completes_partial_standings(self) -> None:
        output = {
            "game_date": "2028-08-10", "game_layout": "fm24",
            "matches": [], "season_results": [],
            "competitions": [{
                "id": 987657, "name": "Partial League",
                "kind": "league", "reputation": 55,
            }],
            "competition_formats": [{
                "competition_id": 987657,
                "competition_name": "Partial League",
                "competition_kind": "league",
                "opening_stage_type": "league",
                "opening_slot_count": 10,
                "opening_field_complete": True,
                "opening_teams": [{
                    "id": team_id, "name": f"Team {team_id}",
                    "reputation": 5000 + team_id * 100,
                    "profile": _profile(100 + team_id),
                } for team_id in range(1, 11)],
                "has_terminal_cup_stage": False,
            }],
        }
        standings = {"competitions": [{
            "competition_id": 987657,
            "competition_name": "Partial League",
            "teams": [{
                "team_id": team_id, "team_name": f"Team {team_id}",
                "played": 1, "points": 3 if team_id <= 2 else 0,
            } for team_id in range(1, 5)],
        }]}

        config = _discovered_league_configs(output, standings)[987657]
        market = build_championship_markets(output, standings)["competitions"][0]

        self.assertEqual(config["teams"], 10)
        self.assertEqual(config["source"], "native_league_format")
        self.assertEqual(len(market["teams"]), 10)

    def test_hybrid_league_is_not_priced_as_single_table(self) -> None:
        output = {
            "game_date": "2028-08-10",
            "competitions": [{
                "id": 987658, "name": "Hybrid League",
                "kind": "league", "reputation": 55,
            }],
            "competition_formats": [{
                "competition_id": 987658,
                "competition_kind": "league",
                "opening_stage_type": "league",
                "opening_slot_count": 10,
                "opening_field_complete": True,
                "opening_teams": [{"id": team_id} for team_id in range(1, 11)],
                "has_terminal_cup_stage": True,
            }],
        }
        standings = {"competitions": [{
            "competition_id": 987658,
            "competition_name": "Hybrid League",
            "teams": [{"team_id": team_id} for team_id in range(1, 11)],
        }]}

        self.assertNotIn(987658, _discovered_league_configs(output, standings))
        diagnostics = build_championship_markets(
            output, standings,
        )["discovery_diagnostics"]
        self.assertEqual(diagnostics[0]["reason"], "hybrid_league_unsupported")

    def test_calendar_year_league_uses_shared_inferred_season_start(self) -> None:
        output = {
            "game_date": "2027-08-31", "game_layout": "fm24",
            "matches": [], "season_results": [],
            "competition_season_starts": {987655: "2027-03-01"},
            "competitions": [{
                "id": 987655, "name": "Calendar League",
                "kind": "league", "reputation": 55,
            }],
            "competition_formats": [{
                "competition_id": 987655,
                "competition_kind": "league",
                "opening_stage_type": "league",
                "opening_field_complete": True,
                "first_fixture_date": "2027-08-31",
                "season_fixture_bounds_verified": False,
            }],
        }
        standings = {"competitions": [{
            "competition_id": 987655,
            "competition_name": "Calendar League",
            "teams": [{
                "team_id": team_id, "team_name": f"Team {team_id}",
                "played": 20, "points": 40 - team_id,
            } for team_id in range(1, 17)],
        }]}

        config = _discovered_league_configs(output, standings)[987655]

        self.assertEqual(config["calendar"], "calendar_year")

    def test_native_calendar_year_cup_uses_native_first_fixture(self) -> None:
        output = _cup_output(game_date="2027-08-31")
        output["competition_formats"][0].update({
            "competition_kind": "cup",
            "first_fixture_date": "2027-04-15",
            "format_last_observed_at": "2027-08-31",
            "season_fixture_bounds_verified": True,
        })

        config = _discovered_cup_configs(output)[1301394]

        self.assertEqual(config["calendar"], "calendar_year")

    def test_league_with_playoffs_is_not_misclassified_as_cup(self) -> None:
        output = _cup_output()
        output["competition_formats"][0]["competition_kind"] = "league"
        self.assertNotIn(1301394, _discovered_cup_configs(output))

    def test_league_playoff_schedule_does_not_settle_from_regular_season_leader(self) -> None:
        competition_id = 987700
        output = {
            "game_date": "2027-05-01",
            "competitions": [{
                "id": competition_id, "name": "带附加赛联赛",
                "kind": "league", "reputation": 70,
            }],
            "competition_formats": [{
                "competition_id": competition_id,
                "competition_name": "带附加赛联赛",
                "competition_kind": "league",
                "opening_stage_type": "league",
                "opening_slot_count": 6,
                "opening_field_complete": True,
                "opening_teams": [{"id": index} for index in range(1, 7)],
                "has_terminal_cup_stage": False,
            }],
            "matches": [], "season_results": [],
        }
        standings = {"competitions": [{
            "competition_id": competition_id,
            "competition_name": "带附加赛联赛",
            "teams": [
                {
                    "team_id": index,
                    "team_name": f"球队{index}",
                    "played": 11,
                    "points": 40 if index == 1 else 0,
                    "goal_difference": 20 if index == 1 else 0,
                    "goals_for": 30 if index == 1 else 0,
                    "position": index,
                }
                for index in range(1, 7)
            ],
        }]}
        market = next(
            item for item in build_championship_markets(output, standings)["competitions"]
            if int(item["competition_id"]) == competition_id
        )
        self.assertNotEqual(market["status"], "complete")

    def test_native_format_opens_an_unlisted_fm24_cup_without_name_matching(self) -> None:
        output = _cup_output()
        output["game_layout"] = "fm24"
        output["competitions"] = [{"id": 987654, "name": "任意赛事"}]
        for match in output["matches"]:
            match["competition_id"] = 987654
            match["competition_name"] = "任意赛事"
        format_row = output["competition_formats"][0]
        format_row["competition_id"] = 987654
        format_row["competition_name"] = "任意赛事"
        with TemporaryDirectory() as directory, patch(
            "tools.championship_odds.cup_state_path",
            return_value=Path(directory) / "championship_cups.json",
        ), patch("tools.preview_cup_odds.read_result_history", return_value=[]):
            market = build_championship_markets(
                output, {"competitions": []},
            )["competitions"][0]
        self.assertEqual(market["competition_id"], 987654)
        self.assertEqual(market["status"], "open")
        self.assertEqual(len(market["teams"]), 36)

    def test_complete_native_field_opens_when_only_part_of_fixture_window_is_visible(self) -> None:
        output = _cup_output(team_count=48)
        output["competition_formats"][0].update({
            "competition_id": 1301385,
            "competition_name": "世界杯",
            "opening_slot_count": 48,
            "opening_field_complete": True,
            "knockout_slot_count": 32,
            "knockout_round_tie_counts": [16, 8, 4, 2, 1, 1],
        })
        output["competitions"] = [{"id": 1301385, "name": "世界杯"}]
        output["matches"] = output["matches"][:8]
        output["season_results"] = [{
            "date": "2026-08-20",
            "competition_id": 1301385,
            "home": {"id": 1, "name": "世界杯球队1"},
            "away": {"id": 2, "name": "世界杯球队2"},
            "home_goals": 1,
            "away_goals": 0,
        }]
        for match in output["matches"]:
            match["competition_id"] = 1301385
            match["competition_name"] = "世界杯"
        with TemporaryDirectory() as directory, patch(
            "tools.championship_odds.cup_state_path",
            return_value=Path(directory) / "championship_cups.json",
        ), patch("tools.preview_cup_odds.read_result_history", return_value=[]):
            market = build_championship_markets(
                output, {"competitions": []},
            )["competitions"][0]
        self.assertEqual(market["competition_id"], 1301385)
        self.assertEqual(market["status"], "open")
        self.assertEqual(len(market["teams"]), 48)

    def test_uefa_league_stage_hints_do_not_override_native_team_counts(self) -> None:
        for competition_id, league_matches in (
            (1301394, 8), (1301396, 8), (31051584, 6),
        ):
            output = _cup_output()
            output["competition_formats"][0]["competition_id"] = competition_id
            config = _discovered_cup_configs(output)[competition_id]
            self.assertEqual(config["participants"], 36)
            self.assertEqual(config["qualifiers"], 24)
            self.assertEqual(config["league_matches"], league_matches)

    def test_complete_group_stage_opens_before_knockout_stage_is_instantiated(self) -> None:
        output = _cup_output(team_count=48)
        format_row = output["competition_formats"][0]
        format_row.update({
            "competition_id": 1301385,
            "competition_name": "FIFA World Cup",
            "competition_kind": "national",
            "opening_slot_count": 48,
            "opening_field_complete": True,
            "opening_stage_type": "group",
            "knockout_slot_count": 0,
            "knockout_round_tie_counts": [],
            "has_terminal_cup_stage": False,
        })
        config = _discovered_cup_configs(output)[1301385]
        self.assertTrue(config["provisional_group_stage"])
        self.assertEqual(config["participants"], 48)
        self.assertEqual(config["qualifiers"], 32)

    def test_final_score_identifies_winner_when_fm24_has_no_outcome_code(self) -> None:
        result = {
            "date": "2026-05-30", "competition_id": 1301394,
            "home": {"id": 868}, "away": {"id": 680},
            "home_goals": 1, "away_goals": 0,
        }
        self.assertEqual(_final_result_winner_id(result), 868)
        result["away_goals"] = 1
        self.assertIsNone(_final_result_winner_id(result))

    def test_final_result_identifies_extra_time_penalty_and_outcome_winners(self) -> None:
        base = {
            "date": "2026-05-30", "competition_id": 1301394,
            "home": {"id": 868}, "away": {"id": 680},
            "home_goals": 1, "away_goals": 1,
        }
        self.assertEqual(_final_result_winner_id({
            **base,
            "penalty_shootout_home_goals": 5,
            "penalty_shootout_away_goals": 4,
        }), 868)
        self.assertEqual(_final_result_winner_id({
            **base,
            "after_extra_time_home_goals": 1,
            "after_extra_time_away_goals": 2,
        }), 680)
        self.assertEqual(_final_result_winner_id({
            **base, "home_outcome_code": 10, "away_outcome_code": 3,
        }), 680)

    def test_cup_waits_for_full_field_and_settles_from_registered_final(self) -> None:
        with TemporaryDirectory() as directory, patch(
            "tools.championship_odds.cup_state_path",
            return_value=Path(directory) / "championship_cups.json",
        ), patch("tools.preview_cup_odds.read_result_history", return_value=[]):
            incomplete = build_championship_markets(
                _cup_output(team_count=34), {"competitions": []},
            )
            self.assertFalse(any(
                item.get("competition_id") == 1301394
                for item in incomplete["competitions"]
            ))

            opened = build_championship_markets(
                _cup_output(), {"competitions": []},
            )
            cup = next(item for item in opened["competitions"] if item["competition_id"] == 1301394)
            self.assertEqual(cup["status"], "open")
            self.assertEqual(cup["market_kind"], "cup")
            self.assertEqual(len(cup["teams"]), 36)
            self.assertLessEqual(
                max(float(team["odds"]) for team in cup["teams"]),
                CHAMPIONSHIP_MAX_ODDS,
            )
            self.assertTrue(all(
                float(team["odds"]) <= 100.0
                or float(team["odds"]) in CHAMPIONSHIP_HIGH_ODDS_LADDER
                for team in cup["teams"]
            ))
            opening_version = cup["market_version"]

            progressed_output = _cup_output(game_date="2026-09-11")
            progressed_output["season_results"] = [{
                "date": "2026-09-10", "competition_id": 1301394,
                "home": {"id": 1, "name": "欧冠球队1"},
                "away": {"id": 2, "name": "欧冠球队2"},
                "home_goals": 2, "away_goals": 0, "winner_side": "home",
            }]
            progressed = build_championship_markets(
                progressed_output, {"competitions": []},
            )
            cup = next(item for item in progressed["competitions"] if item["competition_id"] == 1301394)
            self.assertEqual(cup["status"], "open")
            self.assertEqual(len(cup["teams"]), 36)
            self.assertNotEqual(cup["market_version"], opening_version)

            final_output = {
                "game_date": "2027-05-30", "save_instance_id": "save-1",
                "season_results": [], "competitions": [{"id": 1301394, "name": "欧冠联赛"}],
                "matches": [{
                    "competition_id": 1301394, "competition_name": "欧冠联赛",
                    "fixture_date": "2027-06-01",
                    "home": _cup_output()["matches"][0]["home"],
                    "away": _cup_output()["matches"][0]["away"],
                    "knockout": {
                        "advance": {"1": 1.42, "2": 2.67},
                        "fair_advance": {"1": 1.36, "2": 2.53},
                    },
                    "competition_final": {
                        "date": "2027-06-01", "home_id": 1, "away_id": 2,
                        "kickoff_minutes": 1200,
                        "stage_index": 2, "round_index": 4, "tie_address": 0xABC,
                    },
                }],
            }
            final_markets = build_championship_markets(final_output, {"competitions": []})
            final_market = next(
                item for item in final_markets["competitions"]
                if item["competition_id"] == 1301394
            )
            self.assertEqual(final_market["status"], "open")
            self.assertEqual(final_market["settlement_date"], "2027-06-01")
            self.assertEqual(
                {team["team_id"] for team in final_market["teams"]}, {1, 2},
            )
            self.assertEqual(final_market["pricing_source"], "final_winner_market")
            self.assertEqual(
                {team["team_id"]: team["odds"] for team in final_market["teams"]},
                {1: 1.42, 2: 2.67},
            )
            self.assertTrue(all(
                team["pricing_source"] == "final_winner_market"
                for team in final_market["teams"]
            ))

            early_result_output = {
                **final_output,
                "game_date": "2027-06-01",
                "game_time": "19:59",
                "season_results": [{
                    "date": "2027-06-01", "competition_id": 1301394,
                    "home": {"id": 1, "name": "home"},
                    "away": {"id": 2, "name": "away"},
                    "home_goals": 1, "away_goals": 1,
                    "winner_side": "away", "decided_by": "penalties",
                }],
            }
            early = build_championship_markets(early_result_output, {"competitions": []})
            early_market = next(
                item for item in early["competitions"]
                if item["competition_id"] == 1301394
            )
            self.assertEqual(early_market["status"], "open")
            self.assertIsNone(early_market.get("winner_team_id"))

            started_result_output = {**early_result_output, "game_time": "20:00"}
            started = build_championship_markets(
                started_result_output, {"competitions": []},
            )
            started_market = next(
                item for item in started["competitions"]
                if item["competition_id"] == 1301394
            )
            self.assertEqual(started_market["status"], "awaiting_result")
            self.assertEqual(started_market["teams"], [])

            due_result_output = {**early_result_output, "game_time": "22:30"}
            due = build_championship_markets(due_result_output, {"competitions": []})
            due_market = next(
                item for item in due["competitions"]
                if item["competition_id"] == 1301394
            )
            self.assertEqual(due_market["status"], "complete")
            self.assertEqual(due_market["winner_team_id"], 2)

            completed_output = {
                "game_date": "2027-06-02", "save_instance_id": "save-1",
                "matches": [], "competitions": [{"id": 1301394, "name": "欧冠联赛"}],
                "season_results": [{
                    "date": "2027-06-01", "competition_id": 1301394,
                    "home": {"id": 1, "name": "欧冠球队1"},
                    "away": {"id": 2, "name": "欧冠球队2"},
                    "home_goals": 1, "away_goals": 1,
                    "winner_side": "away", "decided_by": "penalties",
                }],
            }
            completed = build_championship_markets(completed_output, {"competitions": []})
            cup = next(item for item in completed["competitions"] if item["competition_id"] == 1301394)
            self.assertEqual(cup["status"], "complete")
            self.assertEqual(cup["winner_team_id"], 2)

            next_season = build_championship_markets({
                "game_date": "2027-07-02", "save_instance_id": "save-1",
                "matches": [], "season_results": [], "competitions": [],
            }, {"competitions": []})
            previous = next(
                item for item in next_season["settlement_competitions"]
                if item["season_key"] == "1301394:2026/27"
            )
            self.assertEqual(previous["winner_team_id"], 2)

    def test_finished_final_without_result_closes_and_alt_layer_result_settles(self) -> None:
        with TemporaryDirectory() as directory, patch(
            "tools.championship_odds.cup_state_path",
            return_value=Path(directory) / "championship_cups.json",
        ), patch("tools.preview_cup_odds.read_result_history", return_value=[]):
            build_championship_markets(_cup_output(), {"competitions": []})
            opening = _cup_output()["competition_formats"][0]["opening_teams"]
            final_output = {
                "game_date": "2027-05-30", "save_instance_id": "save-1",
                "season_results": [],
                "competitions": [{"id": 1301394, "name": "Cup"}],
                "matches": [{
                    "competition_id": 1301394,
                    "competition_name": "Cup",
                    "fixture_date": "2027-06-01",
                    "home": opening[0],
                    "away": opening[1],
                    "competition_final": {
                        "date": "2027-06-01",
                        "home_id": 1,
                        "away_id": 2,
                        "kickoff_minutes": 1200,
                    },
                }],
            }
            build_championship_markets(final_output, {"competitions": []})

            overdue = build_championship_markets({
                "game_date": "2027-06-02", "save_instance_id": "save-1",
                "matches": [], "season_results": [],
                "competitions": [{"id": 1301394, "name": "Cup"}],
            }, {"competitions": []})
            waiting = next(
                item for item in overdue["competitions"]
                if item["competition_id"] == 1301394
            )
            self.assertEqual(waiting["status"], "awaiting_result")
            self.assertEqual(waiting["teams"], [])

            settled = build_championship_markets({
                "game_date": "2027-06-02", "save_instance_id": "save-1",
                "matches": [],
                "competitions": [{"id": 1301394, "name": "Cup"}],
                "season_results": [{
                    "date": "2027-06-01",
                    "competition_id": 999999,
                    "home": {"id": 1, "name": "Home"},
                    "away": {"id": 2, "name": "Away"},
                    "home_goals": 0,
                    "away_goals": 1,
                }],
            }, {"competitions": []})
            completed = next(
                item for item in settled["competitions"]
                if item["competition_id"] == 1301394
            )
            self.assertEqual(completed["status"], "complete")
            self.assertEqual(completed["winner_team_id"], 2)

    def test_legacy_two_finalists_use_native_last_date_without_final_fixture(self) -> None:
        with TemporaryDirectory() as directory, patch(
            "tools.championship_odds.cup_state_path",
            return_value=Path(directory) / "championship_cups.json",
        ), patch("tools.preview_cup_odds.read_result_history", return_value=[]):
            build_championship_markets(_cup_output(), {"competitions": []})
            state_path = Path(directory) / "championship_cups.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            record = state["seasons"]["1301394:2026/27"]
            record["contender_team_ids"] = [1, 2]
            record["knockout_started"] = True
            record.pop("final_fixture", None)
            state_path.write_text(
                json.dumps(state, ensure_ascii=False), encoding="utf-8",
            )

            overdue = _cup_output(game_date="2027-06-02")
            overdue["matches"] = []
            overdue["competition_formats"][0].update({
                "last_fixture_date": "2027-06-01",
                "format_last_observed_at": "2027-06-02",
            })
            waiting = build_championship_markets(
                overdue, {"competitions": []},
            )["competitions"][0]

            self.assertEqual(waiting["status"], "awaiting_result")
            self.assertEqual(waiting["settlement_date"], "2027-06-01")
            self.assertEqual(waiting["teams"], [])

            overdue["season_results"] = [{
                "date": "2027-06-01", "competition_id": 1301394,
                "home": {"id": 1, "name": "Home"},
                "away": {"id": 2, "name": "Away"},
                "home_goals": 0, "away_goals": 1,
            }]
            completed = build_championship_markets(
                overdue, {"competitions": []},
            )["competitions"][0]

            self.assertEqual(completed["status"], "complete")
            self.assertEqual(completed["winner_team_id"], 2)

    def test_mid_tournament_opening_requires_profiles_only_for_contenders(self) -> None:
        output = _cup_output()
        output["competition_formats"][0]["opening_teams"][2]["profile"] = {}
        output["matches"] = [{
            "competition_id": 1301394, "competition_name": "欧冠联赛",
            "fixture_date": "2027-06-01",
            "home": output["competition_formats"][0]["opening_teams"][0],
            "away": output["competition_formats"][0]["opening_teams"][1],
            "competition_final": {
                "date": "2027-06-01", "home_id": 1, "away_id": 2,
                "stage_index": 1, "round_index": 4, "tie_address": 0xABC,
            },
        }]
        market = build_championship_markets(
            output, {"competitions": []},
        )["competitions"][0]
        self.assertEqual(
            {team["team_id"] for team in market["teams"]}, {1, 2},
        )

    def test_complete_world_cup_knockout_round_removes_absent_teams(self) -> None:
        participant_map = {
            team_id: {"team_id": team_id}
            for team_id in range(1, 49)
        }
        matches = []
        for tie_index in range(4):
            matches.append({
                "competition_id": 1301385,
                "home": {"id": tie_index * 2 + 1},
                "away": {"id": tie_index * 2 + 2},
                "competition_round": {
                    "stage_index": 1, "round_index": 2,
                    "round_tie_count": 4, "tie_address": 100 + tie_index,
                },
            })
        contenders = _current_knockout_contenders(
            {"matches": matches}, 1301385, participant_map, {},
            {"byes": 0},
        )
        self.assertEqual(contenders, set(range(1, 9)))

    def test_native_round_tie_outcomes_override_incomplete_fixture_rounds(self) -> None:
        participant_map = {
            team_id: {"team_id": team_id}
            for team_id in range(1, 49)
        }
        output = {
            "competition_formats": [{
                "competition_id": 1301385,
                "knockout_active_team_ids": list(range(1, 24)),
                "knockout_eliminated_team_ids": list(range(24, 33)),
            }],
            "matches": [{
                "competition_id": 1301385,
                "home": {"id": 1}, "away": {"id": 2},
                "competition_round": {
                    "stage_index": 1, "round_index": 1,
                    "round_tie_count": 8, "tie_address": 100,
                },
            }],
        }
        contenders = _current_knockout_contenders(
            output, 1301385, participant_map, {}, {"byes": 0},
        )
        self.assertEqual(contenders, set(range(1, 24)))

    def test_champions_league_phase_removes_teams_below_safe_cutoff(self) -> None:
        participant_map = {
            team_id: {"team_id": team_id}
            for team_id in range(1, 37)
        }
        table = {
            team_id: {
                "team_id": team_id, "played": 7,
                "points": 10 if team_id <= 24 else 0,
                "goal_difference": 25 - team_id, "position": team_id,
            }
            for team_id in participant_map
        }
        contenders = _league_phase_contenders(
            participant_map, table,
            {"league_matches": 8, "qualifiers": 24},
        )
        self.assertEqual(contenders, set(range(1, 25)))

    def test_settlement_uses_season_key_and_unique_winner(self) -> None:
        records = [
            {"bet_id": "won", "type": "championship", "status": "pending", "season_key": "11:2026/27", "team_id": 1, "stake": 100, "odds": 4.0},
            {"bet_id": "lost", "type": "championship", "status": "pending", "season_key": "11:2026/27", "team_id": 2, "stake": 50, "odds": 6.0},
        ]
        wallet = {"balance": 0.0, "transactions": []}
        markets = {"competitions": [{
            "status": "complete", "season_key": "11:2026/27",
            "winner_team_id": 1, "winner_team_name": "球队1",
            "season_end": "2027-06-30", "settlement_date": "2027-05-29",
        }]}
        with (
            patch("tools.betting_account.load_bets", return_value=records),
            patch("tools.betting_account.load_wallet", return_value=wallet),
            patch("tools.betting_account._commit_account") as commit,
        ):
            result = settle_championship_bets(markets)
        self.assertEqual(result["settled"], 2)
        self.assertEqual(result["returned"], 400.0)
        self.assertEqual(records[0]["status"], "won")
        self.assertEqual(records[1]["status"], "lost")
        self.assertEqual(records[0]["settlement"][0]["result_date"], "2027-05-29")
        self.assertEqual(wallet["balance"], 400.0)
        self.assertEqual(result["bet_ids"], ["won", "lost"])
        self.assertEqual(len(result["settled_records"]), 2)
        self.assertEqual(result["settled_records"][0]["type"], "championship")
        self.assertEqual(result["settled_records"][0]["status"], "won")
        self.assertEqual(result["settled_records"][0]["profit"], 300.0)
        commit.assert_called_once()

    def test_new_season_does_not_inherit_completed_prior_season_table(self) -> None:
        output = _output()
        output.update({"game_date": "2027-07-01", "game_time": "12:00"})
        standings = _standings(20, played=38)
        market_data = build_championship_markets(output, standings)
        self.assertEqual(market_data["competitions"], [])
        self.assertFalse(market_data["settlement_competitions"])
        self.assertTrue(all(team["played"] == 38 for team in standings["competitions"][0]["teams"]))

    def test_rollover_exposes_resolved_prior_league_for_championship_settlement(self) -> None:
        previous = _output()
        previous.update({"game_date": "2027-06-30", "game_time": "12:00"})
        previous["competition_formats"] = [{
            "competition_id": 11, "competition_kind": "league",
            "opening_stage_type": "league", "opening_slot_count": 20,
            "opening_field_complete": True,
            "opening_teams": [{"id": index} for index in range(1, 21)],
            "first_fixture_date": "2026-08-08", "last_fixture_date": "2027-05-30",
            "season_fixture_bounds_verified": True,
        }]
        build_championship_markets(previous, _standings(20, played=38))

        current = _output()
        current.update({"game_date": "2027-07-01", "game_time": "12:00"})
        markets = build_championship_markets(
            current, _standings(20, played=38),
        )
        historical = {
            str(item.get("season_key")): item
            for item in markets.get("settlement_competitions") or []
        }
        self.assertEqual(historical["11:2026/27"]["winner_team_id"], 1)

        records = [{
            "bet_id": "rollover", "type": "championship", "status": "pending",
            "season_key": "11:2026/27", "team_id": 1, "stake": 100, "odds": 4.0,
        }]
        wallet = {"balance": 0.0, "transactions": []}
        with (
            patch("tools.betting_account.load_bets", return_value=records),
            patch("tools.betting_account.load_wallet", return_value=wallet),
            patch("tools.betting_account._commit_account") as commit,
        ):
            result = settle_championship_bets(markets)

        self.assertEqual(result["settled"], 1)
        self.assertEqual(records[0]["status"], "won")
        self.assertEqual(wallet["balance"], 400.0)
        commit.assert_called_once()

    def test_league_terminal_uses_trusted_settlement_result_on_same_day(self) -> None:
        competition_id = 987663
        teams = [
            {
                "id": team_id, "name": f"Terminal {team_id}",
                "reputation": 5000 + team_id,
                "profile": _profile(110 + team_id),
            }
            for team_id in range(1, 11)
        ]
        output = {
            "game_date": "2028-11-30", "game_time": "12:00",
            "matches": [], "season_results": [], "settlement_results": [{
                "date": "2028-11-30", "competition_id": competition_id,
                "home": {"id": 1, "name": "Terminal 1"},
                "away": {"id": 2, "name": "Terminal 2"},
                "home_goals": 2, "away_goals": 0,
            }],
            "competitions": [{
                "id": competition_id, "name": "Terminal League",
                "kind": "league", "reputation": 45,
            }],
            "competition_formats": [{
                "competition_id": competition_id,
                "competition_name": "Terminal League",
                "competition_kind": "league", "opening_stage_type": "league",
                "opening_slot_count": 10, "opening_field_complete": True,
                "opening_teams": teams, "has_terminal_cup_stage": False,
                "last_fixture_date": "2028-11-30",
                "format_last_observed_at": "2028-11-30",
                "season_fixture_bounds_verified": True,
            }],
        }
        standings = {"competitions": [{
            "competition_id": competition_id,
            "competition_name": "Terminal League",
            "teams": [{
                "team_id": team_id, "team_name": f"Terminal {team_id}",
                "played": 9, "points": 30 - team_id,
                "goal_difference": 11 - team_id, "position": team_id,
            } for team_id in range(1, 11)],
        }]}

        market = build_championship_markets(output, standings)["competitions"][0]

        self.assertEqual(market["status"], "complete")
        self.assertEqual(market["winner_team_id"], 1)

    def test_settlement_migrates_wrong_calendar_key_only_with_unique_date_match(self) -> None:
        records = [{
            "bet_id": "migrated",
            "type": "championship",
            "status": "pending",
            "competition_id": 130931,
            "season_key": "130931:2023/24",
            "placed_at": "2023-07-03",
            "team_id": 23292170,
            "stake": 100,
            "odds": 4.0,
        }, {
            "bet_id": "outside-season",
            "type": "championship",
            "status": "pending",
            "competition_id": 130931,
            "season_key": "130931:wrong",
            "placed_at": "2022-07-03",
            "team_id": 23292170,
            "stake": 100,
            "odds": 4.0,
        }]
        wallet = {"balance": 0.0, "transactions": []}
        markets = {"competitions": [{
            "status": "complete",
            "competition_id": 130931,
            "season_key": "130931:2023",
            "season_start": "2023-04-15",
            "season_end": "2023-12-31",
            "settlement_date": "2023-11-04",
            "winner_team_id": 23292170,
            "winner_team_name": "上海海港",
        }]}

        with (
            patch("tools.betting_account.load_bets", return_value=records),
            patch("tools.betting_account.load_wallet", return_value=wallet),
            patch("tools.betting_account._commit_account") as commit,
        ):
            result = settle_championship_bets(markets)

        self.assertEqual(result["settled"], 1)
        self.assertEqual(records[0]["status"], "won")
        self.assertEqual(records[0]["settlement_season_key"], "130931:2023")
        self.assertEqual(
            records[0]["settlement_season_key_source"],
            "competition_id+placed_at_window",
        )
        self.assertEqual(records[1]["status"], "pending")
        commit.assert_called_once()

    def test_frontend_exposes_championship_betting_entry_and_endpoint(self) -> None:
        index = legacy_web_source("web/index.html")
        script = legacy_web_source("web/app.js")
        stylesheet = open("web/app.css", encoding="utf-8").read()
        self.assertNotIn('id="page-championship"', index)
        self.assertNotIn('data-page="championship"', index)
        self.assertIn('id="championship-content"', index)
        self.assertIn('.betting-championship-content { flex:1; min-height:0; overflow:auto; background:var(--surface); }', stylesheet)
        self.assertIn('data-competition="championship"', script)
        self.assertIn('app.selectedCompetition === "championship"', script)
        self.assertIn('["open", "locked"].includes(item.status)', script)
        self.assertIn('locked ? "已夺冠"', script)
        self.assertGreater(
            script.index('data-competition="championship"'),
            script.index("competitionHtml += groups.map"),
        )
        self.assertNotIn('id="championship-bet-dialog"', index)
        self.assertIn('/api/championship-bets', script)
        self.assertIn('type:"championship", market:"CHAMPION"', script)
        self.assertIn('app.selections.push(selection)', script)
        self.assertIn('championships.map((selection)', script)
        self.assertIn('expandedChampionshipIds: new Set()', script)
        self.assertIn('data-championship-toggle="${competitionId}"', script)
        self.assertIn('aria-expanded="${expanded}"', script)
        self.assertIn('class="fixture-row championship-fixture-row"', script)
        self.assertIn('inline-market championship-inline-market', script)
        self.assertIn('competitionReputations.get(Number(right.competition_id))', script)
        self.assertIn('.championship-fixture-row:hover,.championship-fixture-row[aria-expanded="true"] { background:#f7faf8; box-shadow:none; }', stylesheet)
        self.assertIn('.championship-fixture-row:focus-visible {', stylesheet)
        self.assertIn('outline:3px solid #2d8062', stylesheet)
        self.assertIn('.championship-inline-market { margin-top:0; border-top-width:1px; border-top-color:#e4eae7; background:#fff; box-shadow:none; }', stylesheet)
        self.assertIn('.championship-team.chosen b { background:#176548; color:#fff; }', stylesheet)
        self.assertIn('const teams = expanded ?', script)
        self.assertIn('${expanded ? `<section class="inline-market championship-inline-market" id="${panelId}"><div class="championship-meta">', script)
        self.assertIn('/^\\d{4}$/.test(seasonLabel)', script)
        self.assertIn('`${seasonLabel}年`', script)
        self.assertIn('`${seasonLabel}赛季`', script)
        self.assertIn('<span class="championship-competition-title"><h2>', script)
        self.assertIn('冠军盘仅支持单关，不能加入串关或复式', script)
        self.assertIn('championshipSettlementLabel', script)
        self.assertNotIn('赛季结算日', script)
        self.assertNotIn('决赛胜者结算', script)
        self.assertIn('cup ? "冠军"', script)
        self.assertIn('"CHAMPION":"market.champion"', script)

    def test_state_revalidates_quote_before_reserving_bet(self) -> None:
        state = LocalOddsState.__new__(LocalOddsState)
        state.lock = threading.RLock()
        state.cache_verified = True
        state.save_change_pending = False
        state.refreshing = False
        state.refresh_mode = None
        state.output = {"save_instance_id": "save-1", "game_date": "2026-08-01"}
        state.output_path = None
        state.data_version = 7
        state.club_contexts = {}
        market = {
            "model_version": "championship-v2",
            "published_save_id": "save-1",
            "published_game_date": "2026-08-01",
            "competitions": [{
                "competition_id": 11, "competition_name": "英超联赛",
                "season_key": "11:2026/27", "season_label": "2026/27",
                "season_start": "2026-07-01", "season_end": "2027-06-30",
                "market_version": "quote-1", "status": "open",
                "teams": [
                    {"team_id": 1, "team_name": "球队1", "odds": 4.0},
                    {"team_id": 2, "team_name": "球队2", "odds": 6.0},
                ],
            }],
        }
        state.output["championship_markets"] = market
        with (
            patch(
                "fm_odds_web.public_league_standings",
                side_effect=AssertionError("bet validation must use the published snapshot"),
            ) as standings,
            patch(
                "fm_odds_web.build_championship_markets",
                side_effect=AssertionError("bet validation must not rebuild markets"),
            ) as build,
            patch("fm_odds_web.load_settings", return_value={"default_betting_limit_single": 20_000_000}),
            patch("fm_odds_web.read_game_clock", return_value={"date": "2026-08-01", "time": "12:00"}),
            patch("fm_odds_web.manager_display_name", return_value="经理"),
            patch("fm_odds_web.set_active_save_id") as set_scope,
            patch("fm_odds_web.reserve_bets", return_value={"balance": 9600.0}) as reserve,
        ):
            result = state.place_championship_bet({
                "stake": 100,
                "selections": [
                    {"competition_id": 11, "team_id": 1, "market_version": "stale-quote", "odds": 5.0},
                    {"competition_id": 11, "team_id": 2, "market_version": "quote-1", "odds": 6.0},
                ],
            })
            market["competitions"][0]["status"] = "locked"
            with self.assertRaisesRegex(ValueError, "当前未开放"):
                state.place_championship_bet({
                    "stake": 100, "competition_id": 11, "team_id": 1,
                    "market_version": "quote-1", "odds": 4.0,
                })
        self.assertEqual(result["balance"], 9600.0)
        self.assertTrue(result["repriced"])
        self.assertEqual(result["records"], 2)
        self.assertEqual(set_scope.call_count, 2)
        set_scope.assert_called_with("save-1")
        self.assertEqual(reserve.call_count, 1)
        standings.assert_not_called()
        build.assert_not_called()
        self.assertEqual(reserve.call_args.args[1], 200.0)
        self.assertEqual(len(reserve.call_args.args[0]), 2)
        record = reserve.call_args.args[0][0]
        self.assertEqual(record["type"], "championship")
        self.assertEqual(record["season_key"], "11:2026/27")
        self.assertEqual(record["potential_return"], 400.0)

    def test_unchanged_refresh_reuses_published_championship_snapshot(self) -> None:
        standings = {"competitions": [{"competition_id": 11, "table": []}]}
        output = {
            "save_instance_id": "save-1",
            "game_date": "2026-08-01",
            "season_start": "2026-07-01",
            "season_end": "2027-06-30",
            "competitions": [],
            "competition_formats": [],
            "matches": [],
            "season_results": [],
            "settlement_results": [],
            "performance": {"total_ms": 10.0},
        }
        generated = {"model_version": "championship-v9", "competitions": []}
        with (
            patch("fm_odds_web.public_league_standings", return_value=standings),
            patch(
                "fm_odds_web.build_championship_markets", return_value=generated,
            ) as build,
        ):
            first, first_hit = publish_championship_markets(output)
            refreshed = {
                key: value for key, value in output.items()
                if key != "championship_markets"
            }
            second, second_hit = publish_championship_markets(refreshed, output)

        self.assertFalse(first_hit)
        self.assertTrue(second_hit)
        self.assertEqual(first["dependency_signature"], second["dependency_signature"])
        self.assertEqual(build.call_count, 1)
        self.assertEqual(refreshed["performance"]["championship_market_cache_hit"], 1)

    def test_awaiting_result_snapshot_is_rebuilt_on_unchanged_refresh(self) -> None:
        standings = {"competitions": []}
        output = {
            "save_instance_id": "save-1",
            "game_date": "2026-08-01",
            "competitions": [], "competition_formats": [], "matches": [],
            "season_results": [], "settlement_results": [],
            "performance": {},
        }
        awaiting = {
            "model_version": CHAMPIONSHIP_MODEL_VERSION,
            "competitions": [{"status": "awaiting_result"}],
        }
        completed = {
            "model_version": CHAMPIONSHIP_MODEL_VERSION,
            "competitions": [{"status": "complete", "winner_team_id": 1}],
        }
        with (
            patch("fm_odds_web.public_league_standings", return_value=standings),
            patch(
                "fm_odds_web.build_championship_markets",
                side_effect=[awaiting, completed],
            ) as build,
        ):
            first, first_hit = publish_championship_markets(output)
            refreshed = {
                key: value for key, value in output.items()
                if key != "championship_markets"
            }
            second, second_hit = publish_championship_markets(refreshed, output)

        self.assertFalse(first_hit)
        self.assertFalse(second_hit)
        self.assertEqual(build.call_count, 2)
        self.assertEqual(second["competitions"][0]["status"], "complete")

    def test_pending_cup_final_result_keys_reads_overdue_unsettled_finals(self) -> None:
        with TemporaryDirectory() as directory, patch(
            "tools.championship_odds.cup_state_path",
            return_value=Path(directory) / "championship_cups.json",
        ):
            state_path = Path(directory) / "championship_cups.json"
            state_path.write_text(json.dumps({
                "schema_version": 1,
                "seasons": {
                    "1301385:2026": {
                        "competition_id": 1301385,
                        "final_fixture": {
                            "date": "2026-06-24", "home_id": 10, "away_id": 20,
                        },
                    },
                    "1301394:2026/27": {
                        "competition_id": 1301394,
                        "winner_team_id": 30,
                        "final_fixture": {
                            "date": "2027-06-01", "home_id": 30, "away_id": 40,
                        },
                    },
                    "future:2028": {
                        "competition_id": 99,
                        "final_fixture": {
                            "date": "2028-06-01", "home_id": 50, "away_id": 60,
                        },
                    },
                },
            }), encoding="utf-8")

            keys = pending_cup_final_result_keys("2026-08-01")

        self.assertEqual(keys, {("2026-06-24", 1301385, 10, 20)})


if __name__ == "__main__":
    unittest.main()
