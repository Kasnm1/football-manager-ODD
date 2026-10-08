from __future__ import annotations

import unittest
from datetime import date

from unittest.mock import patch

from tools import league_standings
from tools.league_standings import (
    _apply_same_league_playoff_positions, _fm26_table_results,
    add_points_adjustment, build_standings,
    public_league_standings,
)
from tools.preview_cup_odds import competition_season_starts
from tools.storage_retention import result_retention_start


class LeagueStandingsTests(unittest.TestCase):
    def tearDown(self) -> None:
        league_standings._PUBLIC_STANDINGS_CACHE.clear()

    def test_uses_native_start_dates_for_calendar_and_cross_year_leagues(self) -> None:
        starts = competition_season_starts(date(2026, 10, 1), [
            {"competition_id": 1, "first_fixture_date": "2026-03-15", "season_fixture_bounds_verified": True},
            {"competition_id": 2, "first_fixture_date": "2026-08-09", "season_fixture_bounds_verified": True},
        ])

        self.assertEqual(starts, {1: "2026-03-15", 2: "2026-08-09"})

    def test_future_calendar_season_keeps_current_calendar_year_results(self) -> None:
        starts = competition_season_starts(date(2026, 12, 1), [
            {"competition_id": 1, "first_fixture_date": "2027-03-15", "season_fixture_bounds_verified": True},
            {"competition_id": 2, "first_fixture_date": "2027-08-09", "season_fixture_bounds_verified": True},
        ])

        self.assertEqual(starts[1], "2026-01-01")
        self.assertNotIn(2, starts)

    def test_incomplete_fixture_bounds_do_not_truncate_serie_a(self) -> None:
        starts = competition_season_starts(date(2026, 5, 21), [{
            "competition_id": 32,
            "first_fixture_date": "2026-05-24",
            "last_fixture_date": "2026-05-24",
            "season_fixture_bounds_verified": False,
        }])

        self.assertNotIn(32, starts)

    def test_configured_cross_year_leagues_ignore_misleading_native_bounds(self) -> None:
        starts = competition_season_starts(date(2026, 5, 21), [
            {
                "competition_id": competition_id,
                "first_fixture_date": "2026-01-03",
                "last_fixture_date": "2026-05-24",
                "season_fixture_bounds_verified": True,
            }
            for competition_id in (10, 11, 16, 22, 32, 67)
        ])

        self.assertEqual(starts, {})

    def test_loaded_leagues_infer_calendar_from_results_and_current_teams(self) -> None:
        def result(day: str, home: int, away: int, competition_id: int) -> dict:
            return {
                "date": day, "competition": {"id": competition_id},
                "home_team": {"id": home}, "away_team": {"id": away},
            }

        formats = [
            {"competition_id": 9001, "opening_teams": [{"id": 1}, {"id": 2}]},
            {"competition_id": 9002, "opening_teams": [{"id": 3}, {"id": 4}]},
        ]
        starts = competition_season_starts(date(2026, 10, 1), formats, [
            result("2026-05-20", 1, 9, 9001),
            result("2026-08-10", 1, 2, 9001),
            result("2026-03-10", 3, 4, 9002),
            result("2026-09-20", 3, 4, 9002),
        ])

        self.assertEqual(starts[9001], "2026-08-10")
        self.assertEqual(starts[9002], "2026-03-10")

    def test_patched_season_address_excludes_same_team_results_from_old_calendar(self) -> None:
        def result(day: str, season_address: int) -> dict:
            return {
                "date": day,
                "competition": {"id": 130931, "address": hex(season_address)},
                "home_team": {"id": 1},
                "away_team": {"id": 2},
            }

        format_row = {
            "competition_id": 130931,
            "competition_season_address": "0x2000",
            "opening_teams": [{"id": 1}, {"id": 2}],
            "season_fixture_bounds_verified": False,
        }
        results = [
            result("2023-04-15", 0x1000),
            result("2023-05-10", 0x1000),
            result("2023-06-07", 0x1000),
            result("2023-06-28", 0x1000),
            result("2023-07-01", 0x2000),
        ]

        starts = competition_season_starts(
            date(2023, 7, 3), [format_row], results,
        )

        self.assertEqual(starts[130931], "2023-07-01")

        for row in results:
            row["competition"]["address"] = "0x2000"
        original_calendar = competition_season_starts(
            date(2023, 7, 3), [format_row], results,
        )
        self.assertEqual(original_calendar[130931], "2023-04-15")

    def test_new_cross_year_season_rejects_previous_season_inferred_start(self) -> None:
        formats = [{
            "competition_id": 9001,
            "opening_teams": [{"id": 1}, {"id": 2}],
            "first_fixture_date": "2025-08-10",
            "season_fixture_bounds_verified": True,
        }]
        results = [{
            "date": day,
            "competition": {"id": 9001},
            "home_team": {"id": 1},
            "away_team": {"id": 2},
        } for day in ("2025-08-10", "2026-05-20")]

        starts = competition_season_starts(
            date(2026, 8, 7), formats, results,
        )

        self.assertNotIn(9001, starts)

    def test_public_table_does_not_infer_season_rollover_from_match_count(self) -> None:
        teams = [{
            "id": team_id,
            "name": f"Team {team_id}",
            "played": 33,
            "won": 20,
            "drawn": 7,
            "lost": 6,
            "goals_for": 60,
            "goals_against": 30,
            "goal_difference": 30,
            "points": 67,
            "position": team_id,
            "native_standing_verified": True,
            "native_standing_address": hex(0x3000 + team_id * 0x100),
        } for team_id in range(1, 19)]
        output = {
            "save_instance_id": "save-1",
            "game_layout": "fm26",
            "game_date": "2026-08-07",
            "season_start": "2026-07-01",
            "season_end": "2027-06-30",
            "competition_season_starts": {},
            "season_results": [],
            "competitions": [{"id": 9001, "reputation": 100}],
            "competition_formats": [{
                "competition_id": 9001,
                "competition_name": "World Development League",
                "competition_kind": "league",
                "opening_stage_type": "league",
                "opening_slot_count": 18,
                "opening_field_complete": True,
                "opening_teams": teams,
            }],
        }

        with patch.object(
            league_standings, "_load_state",
            return_value={"schema_version": 1, "records": []},
        ):
            standings = public_league_standings(output)

        competition = standings["competitions"][0]
        self.assertEqual(competition["source"], "native_league_stage")
        self.assertEqual({row["played"] for row in competition["teams"]}, {33})
        self.assertEqual({row["points"] for row in competition["teams"]}, {67})
        self.assertTrue(all(
            row.get("native_standing_address")
            for row in competition["teams"]
        ))

    def test_current_native_season_table_publishes_its_own_zero_state(self) -> None:
        teams = [{
            "id": team_id,
            "name": f"Team {team_id}",
            "played": 0,
            "won": 0,
            "drawn": 0,
            "lost": 0,
            "goals_for": 0,
            "goals_against": 0,
            "goal_difference": 0,
            "points": 0,
            "position": team_id,
            "native_standing_verified": True,
            "native_standing_address": hex(0x5000 + team_id * 0x100),
        } for team_id in range(1, 11)]
        output = {
            "save_instance_id": "save-1",
            "game_layout": "fm26",
            "game_date": "2026-08-07",
            "season_results": [],
            "competitions": [{"id": 9001, "reputation": 100}],
            "competition_formats": [{
                "competition_id": 9001,
                "competition_name": "Single Round League",
                "competition_kind": "league",
                "competition_season_address": "0x4000",
                "actual_competition_address": "0x4100",
                "opening_stage_type": "league",
                "opening_slot_count": 10,
                "opening_field_complete": True,
                "opening_teams": teams,
            }],
        }

        with patch.object(
            league_standings, "_load_state",
            return_value={"schema_version": 1, "records": []},
        ):
            standings = public_league_standings(output)

        competition = standings["competitions"][0]
        self.assertEqual(competition["source"], "native_league_stage")
        self.assertEqual({row["played"] for row in competition["teams"]}, {0})
        self.assertEqual({row["points"] for row in competition["teams"]}, {0})

    def test_native_league_stage_replaces_incomplete_reconstructed_table(self) -> None:
        output = {
            "game_layout": "fm24",
            "season_results": [{
                "date": "2023-07-01",
                "competition_id": 130931,
                "competition_name": "中超联赛",
                "competition_kind": "league",
                "home": {"id": 1, "name": "甲队"},
                "away": {"id": 2, "name": "乙队"},
                "home_goals": 1,
                "away_goals": 0,
            }],
            "competition_formats": [{
                "competition_id": 130931,
                "competition_name": "中超联赛",
                "competition_kind": "league",
                "opening_stage_type": "league",
                "opening_slot_count": 2,
                "opening_field_complete": True,
                "opening_teams": [{
                    "id": 1, "name": "甲队", "address": "0x1000",
                    "played": 16, "won": 11, "drawn": 4, "lost": 1,
                    "goals_for": 30, "goals_against": 10,
                    "goal_difference": 20, "points": 37, "position": 1,
                    "native_standing_verified": True,
                    "native_standing_address": "0x3000",
                }, {
                    "id": 2, "name": "乙队", "address": "0x2000",
                    "played": 16, "won": 9, "drawn": 4, "lost": 3,
                    "goals_for": 22, "goals_against": 16,
                    "goal_difference": 6, "points": 31, "position": 2,
                    "native_standing_verified": True,
                    "native_standing_address": "0x3100",
                }],
            }],
        }

        with patch("tools.league_standings._load_state", return_value={"records": []}):
            standings = public_league_standings(output)

        self.assertEqual(
            standings["source"],
            "native_league_stage_with_reconstructed_fallback",
        )
        rows = standings["competitions"][0]["teams"]
        self.assertEqual(
            [(row["team_id"], row["played"], row["points"]) for row in rows],
            [(1, 16, 37), (2, 16, 31)],
        )
        self.assertEqual(rows[0]["native_standing_address"], "0x3000")

    def test_complete_reconstructed_table_rejects_older_native_snapshot(self) -> None:
        def result(day: str, home: int, away: int, home_goals: int) -> dict:
            return {
                "date": day,
                "competition_id": 7,
                "competition_name": "Test League",
                "competition_kind": "league",
                "home": {"id": home, "name": f"Team {home}"},
                "away": {"id": away, "name": f"Team {away}"},
                "home_goals": home_goals,
                "away_goals": 0,
            }

        output = {
            "game_layout": "fm26",
            "season_results": [
                result("2028-08-01", 1, 2, 1),
                result("2028-08-08", 2, 1, 0),
            ],
            "competition_formats": [{
                "competition_id": 7,
                "competition_name": "Test League",
                "competition_kind": "league",
                "opening_stage_type": "league",
                "opening_slot_count": 2,
                "opening_field_complete": True,
                "opening_teams": [{
                    "id": 1, "name": "Team 1", "played": 1,
                    "won": 1, "drawn": 0, "lost": 0,
                    "goals_for": 1, "goals_against": 0,
                    "goal_difference": 1, "points": 3, "position": 1,
                    "native_standing_verified": True,
                    "native_standing_address": "0x3000",
                }, {
                    "id": 2, "name": "Team 2", "played": 1,
                    "won": 0, "drawn": 0, "lost": 1,
                    "goals_for": 0, "goals_against": 1,
                    "goal_difference": -1, "points": 0, "position": 2,
                    "native_standing_verified": True,
                    "native_standing_address": "0x3100",
                }],
            }],
        }

        with patch("tools.league_standings._load_state", return_value={"records": []}):
            standings = public_league_standings(output)

        self.assertEqual(standings["source"], "reconstructed_results")
        rows = standings["competitions"][0]["teams"]
        self.assertEqual({row["played"] for row in rows}, {2})
        self.assertTrue(all("native_standing_address" not in row for row in rows))

    def test_playoff_stage_is_excluded_from_reconstructed_league_table(self) -> None:
        def result(day: str, stage: int, home: int, away: int) -> dict:
            return {
                "date": day, "competition_id": 7,
                "competition_name": "Test League", "competition_kind": "league",
                "competition_stage_index": stage,
                "home": {"id": home, "name": f"Team {home}"},
                "away": {"id": away, "name": f"Team {away}"},
                "home_goals": 1, "away_goals": 0,
            }

        output = {
            "game_layout": "fm26",
            "season_results": [
                result("2028-04-01", 0, 1, 2),
                result("2028-05-01", 1, 1, 2),
            ],
            "competition_formats": [{
                "competition_id": 7, "competition_kind": "league",
                "opening_stage_type": "league", "opening_stage_index": 0,
            }],
        }

        with patch("tools.league_standings._load_state", return_value={"records": []}):
            standings = public_league_standings(output)

        rows = standings["competitions"][0]["teams"]
        self.assertEqual({row["played"] for row in rows}, {1})

    def test_same_league_two_team_playoff_only_swaps_final_positions(self) -> None:
        rows = [{
            "team_id": team_id, "team_name": f"Team {team_id}",
            "position": position, "played": 34, "won": 10 + team_id,
            "drawn": 5, "lost": 19 - team_id, "goals_for": 40 + team_id,
            "goals_against": 35, "goal_difference": 5 + team_id,
            "points": 50 + team_id, "elo": 1500 + team_id,
            "elo_matches": 34,
        } for position, team_id in enumerate((1, 2, 3, 4), 1)]
        before = {row["team_id"]: dict(row) for row in rows}
        output = {
            "competition_formats": [{
                "competition_id": 7, "competition_kind": "league",
                "opening_stage_index": 0,
                "stages": [
                    {"stage_index": 0, "stage_type": "league", "team_ids": [1, 2, 3, 4]},
                    {"stage_index": 1, "stage_type": "cup", "team_ids": [3, 4]},
                ],
                "knockout_active_team_ids": [4],
                "knockout_eliminated_team_ids": [3],
            }],
            "season_results": [],
        }

        result = _apply_same_league_playoff_positions(
            output, [{"competition_id": 7, "teams": rows}],
        )[0]
        after = {row["team_id"]: row for row in result["teams"]}

        self.assertEqual((after[4]["position"], after[3]["position"]), (3, 4))
        self.assertTrue(result["playoff_position_applied"])
        for team_id in (3, 4):
            for field in (
                "played", "won", "drawn", "lost", "goals_for",
                "goals_against", "goal_difference", "points", "elo",
                "elo_matches",
            ):
                self.assertEqual(after[team_id][field], before[team_id][field])

    def test_cross_league_promotion_playoff_does_not_change_positions(self) -> None:
        rows = [
            {"team_id": team_id, "position": position, "played": 34, "points": 50}
            for position, team_id in enumerate((1, 2, 3, 4), 1)
        ]
        output = {
            "competition_formats": [{
                "competition_id": 7, "competition_kind": "league",
                "opening_stage_index": 0,
                "stages": [
                    {"stage_index": 0, "stage_type": "league", "team_ids": [1, 2, 3, 4]},
                    {"stage_index": 1, "stage_type": "cup", "team_ids": [4, 99]},
                ],
                "knockout_active_team_ids": [99],
                "knockout_eliminated_team_ids": [4],
            }],
            "season_results": [],
        }

        result = _apply_same_league_playoff_positions(
            output, [{"competition_id": 7, "teams": rows}],
        )[0]

        self.assertEqual(
            [(row["team_id"], row["position"]) for row in result["teams"]],
            [(1, 1), (2, 2), (3, 3), (4, 4)],
        )
        self.assertNotIn("playoff_position_applied", result)

    def test_same_league_two_leg_playoff_uses_aggregate_result_fallback(self) -> None:
        rows = [
            {"team_id": team_id, "position": position, "played": 34, "points": 50}
            for position, team_id in enumerate((1, 2, 3, 4), 1)
        ]
        output = {
            "competition_formats": [{
                "competition_id": 7, "competition_kind": "league",
                "opening_stage_index": 0,
                "stages": [
                    {"stage_index": 0, "stage_type": "league", "team_ids": [1, 2, 3, 4]},
                    {"stage_index": 1, "stage_type": "cup", "team_ids": [3, 4]},
                ],
            }],
            "season_results": [{
                "date": "2028-05-20", "competition_id": 7,
                "competition_stage_index": 1,
                "home": {"id": 4}, "away": {"id": 3},
                "home_goals": 3, "away_goals": 0,
            }, {
                "date": "2028-05-27", "competition_id": 7,
                "competition_stage_index": 1,
                "home": {"id": 3}, "away": {"id": 4},
                "home_goals": 1, "away_goals": 0,
            }],
        }

        result = _apply_same_league_playoff_positions(
            output, [{"competition_id": 7, "teams": rows}],
        )[0]

        self.assertEqual(
            [(row["team_id"], row["position"]) for row in result["teams"]],
            [(1, 1), (2, 2), (4, 3), (3, 4)],
        )
        self.assertTrue(result["playoff_position_applied"])

    def test_legacy_results_without_stage_identity_remain_compatible(self) -> None:
        output = {
            "season_results": [{
                "date": "2028-05-01", "competition_id": 7,
                "competition_name": "Test League", "competition_kind": "league",
                "home": {"id": 1, "name": "Team 1"},
                "away": {"id": 2, "name": "Team 2"},
                "home_goals": 1, "away_goals": 0,
            }],
            "competition_formats": [{
                "competition_id": 7, "competition_kind": "league",
                "opening_stage_type": "league", "opening_stage_index": 0,
            }],
        }

        with patch("tools.league_standings._load_state", return_value={"records": []}):
            standings = public_league_standings(output)

        self.assertEqual({row["played"] for row in standings["competitions"][0]["teams"]}, {1})

    def test_public_projection_cache_returns_isolated_copies(self) -> None:
        output = {
            "save_instance_id": "save-1", "game_layout": "fm24",
            "game_date": "2028-05-01", "competition_formats": [],
            "competitions": [{"id": 7, "reputation": 100}],
            "season_results": [{
                "date": "2028-05-01", "competition_id": 7,
                "competition_name": "Test League", "competition_kind": "league",
                "home": {"id": 1, "name": "Team 1"},
                "away": {"id": 2, "name": "Team 2"},
                "home_goals": 1, "away_goals": 0,
            }],
        }
        league_standings._PUBLIC_STANDINGS_CACHE.clear()
        with (
            patch("tools.league_standings._load_state", return_value={"records": []}),
            patch(
                "tools.league_standings.build_standings",
                wraps=build_standings,
            ) as build,
        ):
            first = public_league_standings(output)
            first["competitions"][0]["teams"][0]["team_name"] = "changed"
            second = public_league_standings(output)

        self.assertEqual(build.call_count, 1)
        self.assertNotEqual(second["competitions"][0]["teams"][0]["team_name"], "changed")

    def test_public_projection_cache_invalidates_when_adjustments_change(self) -> None:
        output = {
            "save_instance_id": "save-1", "game_layout": "fm24",
            "game_date": "2028-05-01", "competition_formats": [],
            "competitions": [{"id": 7, "reputation": 100}],
            "season_results": [{
                "date": "2028-05-01", "competition_id": 7,
                "competition_name": "Test League", "competition_kind": "league",
                "home": {"id": 1, "name": "Team 1"},
                "away": {"id": 2, "name": "Team 2"},
                "home_goals": 1, "away_goals": 0,
            }],
        }
        states = [
            {"records": []},
            {"records": [{
                "id": "adjustment-1", "competition_id": 7, "team_id": 2,
                "delta": 5, "reason": "test", "native_applied": True,
            }]},
        ]
        league_standings._PUBLIC_STANDINGS_CACHE.clear()
        with patch("tools.league_standings._load_state", side_effect=states):
            first = public_league_standings(output)
            second = public_league_standings(output)

        first_points = {row["team_id"]: row["points"] for row in first["competitions"][0]["teams"]}
        second_points = {row["team_id"]: row["points"] for row in second["competitions"][0]["teams"]}
        self.assertEqual(second_points[2], first_points[2] + 5)

    def test_points_adjustment_source_id_is_applied_only_once(self) -> None:
        state = {"schema_version": 1, "records": []}
        standings = {
            "native_write_available": True,
            "competitions": [{
                "competition_id": 7, "competition_name": "测试联赛",
                "teams": [{
                    "team_id": 1, "team_name": "甲队", "team_address": "0x1000",
                    "native_standing_address": "0x3000", "points": 16,
                }],
            }],
            "history": [],
        }
        output = {
            "game_layout": "fm24", "game_date": "2030-01-01",
            "competition_formats": [{
                "competition_id": 7,
                "opening_teams": [{"id": 1, "points": 16}],
            }],
        }
        payload = {
            "competition_id": 7, "team_id": 1, "delta": -3,
            "reason": "足协收买裁判处罚", "source_id": "mail-1:points",
        }
        with patch(
            "tools.league_standings._load_state", return_value=state,
        ), patch(
            "tools.league_standings._save_state",
        ) as save, patch(
            "tools.league_standings.public_league_standings", return_value=standings,
        ), patch(
            "tools.league_table_memory.update_league_points",
            return_value={"before": 16, "after": 13},
        ) as native:
            add_points_adjustment(output, payload)
            add_points_adjustment(output, payload)

        native.assert_called_once()
        save.assert_called_once()
        self.assertEqual(len(state["records"]), 1)
        self.assertEqual(state["records"][0]["source_id"], "mail-1:points")
        self.assertEqual(output["competition_formats"][0]["opening_teams"][0]["points"], 13)

    def test_points_adjustment_adopts_matching_legacy_native_record(self) -> None:
        state = {
            "schema_version": 1,
            "records": [{
                "id": "old-1", "created_at": "2030-01-13T09:00:00",
                "game_date": "2030-01-13", "competition_id": 7,
                "team_id": 1, "delta": -3, "reason": "足协收买裁判处罚",
                "reversal_of": None, "native_applied": True,
            }, {
                "id": "old-2", "created_at": "2030-01-13T09:01:00",
                "game_date": "2030-01-13", "competition_id": 7,
                "team_id": 1, "delta": -3, "reason": "足协收买裁判处罚",
                "reversal_of": None, "native_applied": True,
            }],
        }
        standings = {
            "native_write_available": True, "competitions": [], "history": [],
        }
        payload = {
            "competition_id": 7, "team_id": 1, "delta": -3,
            "reason": "足协收买裁判处罚", "source_id": "mail-1:points",
            "legacy_source_created_after": "2030-01-12T10:00:00",
            "legacy_source_game_date": "2030-01-13",
        }
        with patch(
            "tools.league_standings._load_state", return_value=state,
        ), patch(
            "tools.league_standings._save_state",
        ) as save, patch(
            "tools.league_standings.public_league_standings", return_value=standings,
        ), patch(
            "tools.league_table_memory.update_league_points",
        ) as native:
            result = add_points_adjustment({}, payload)

        self.assertIs(result, standings)
        native.assert_not_called()
        save.assert_called_once_with(state)
        self.assertEqual(state["records"][0]["source_id"], "mail-1:points")
        self.assertEqual(state["records"][1]["suspected_duplicate_of"], "old-1")

    def test_result_retention_covers_both_season_calendars(self) -> None:
        self.assertEqual(result_retention_start(date(2026, 5, 21)), date(2024, 7, 1))
        self.assertEqual(result_retention_start(date(2026, 10, 1)), date(2025, 1, 1))

    def test_builds_table_with_elo_and_adjustments(self) -> None:
        results = [
            {"date": "2026-08-01", "competition_id": 7, "competition_name": "测试联赛", "competition_kind": "league", "home": {"id": 1, "name": "甲队"}, "away": {"id": 2, "name": "乙队"}, "home_goals": 2, "away_goals": 0},
            {"date": "2026-08-08", "competition_id": 7, "competition_name": "测试联赛", "competition_kind": "league", "home": {"id": 2, "name": "乙队"}, "away": {"id": 1, "name": "甲队"}, "home_goals": 1, "away_goals": 1},
        ]
        tables = build_standings(results, [{"competition_id": 7, "team_id": 1, "delta": -6}])
        self.assertEqual(len(tables), 1)
        rows = {row["team_id"]: row for row in tables[0]["teams"]}
        self.assertEqual(rows[1]["base_points"], 4)
        self.assertEqual(rows[1]["points"], -2)
        self.assertEqual(rows[1]["points_adjustment"], -6)
        self.assertGreater(rows[1]["elo"], rows[2]["elo"])
        self.assertEqual(tables[0]["teams"][0]["team_id"], 2)

    def test_ignores_non_league_results(self) -> None:
        tables = build_standings([{
            "date": "2026-08-01", "competition_id": 8, "competition_name": "杯赛",
            "competition_kind": "cup", "home": {"id": 1, "name": "甲队"},
            "away": {"id": 2, "name": "乙队"}, "home_goals": 1, "away_goals": 0,
        }])
        self.assertEqual(tables, [])

    def test_native_layout_ignores_legacy_virtual_adjustments(self) -> None:
        output = {
            "game_layout": "fm26",
            "season_results": [{
                "date": "2026-08-01", "competition_id": 7,
                "competition_name": "测试联赛", "competition_kind": "league",
                "home": {"id": 1, "name": "甲队"},
                "away": {"id": 2, "name": "乙队"},
                "home_goals": 1, "away_goals": 0,
            }],
        }
        records = [
            {"competition_id": 7, "team_id": 1, "delta": 50, "native_applied": False},
            {"competition_id": 7, "team_id": 1, "delta": -6, "native_applied": True},
        ]
        with patch("tools.league_standings._load_state", return_value={"records": records}):
            result = public_league_standings(output)
        self.assertTrue(result["native_write_available"])
        team = next(row for row in result["competitions"][0]["teams"] if row["team_id"] == 1)
        self.assertEqual(team["points_adjustment"], -6)
        self.assertEqual(team["points"], -3)

    def test_fm26_champions_league_keeps_only_league_phase_window(self) -> None:
        def result(date: str, home: int, away: int) -> dict:
            return {
                "date": date, "competition_id": 1301394,
                "competition_name": "欧冠联赛", "competition_kind": "cup",
                "home": {"id": home, "name": f"球队{home}"},
                "away": {"id": away, "name": f"球队{away}"},
                "home_goals": 1, "away_goals": 0,
            }
        qualification = result("2026-08-20", 1, 2)
        league_phase = result("2026-09-20", 3, 4)
        knockout = result("2027-02-20", 3, 4)
        rows = _fm26_table_results({
            "game_layout": "fm26",
            "season_results": [qualification, league_phase, knockout],
        })
        tables = build_standings(rows)
        self.assertEqual(len(tables), 1)
        self.assertEqual({row["team_id"] for row in tables[0]["teams"]}, {3, 4})


if __name__ == "__main__":
    unittest.main()
