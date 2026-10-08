from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from tools.club_economy import collect_match_integrity_penalty
from tools.match_integrity import (
    _event_from_leg,
    _merge_attempt_contributions,
    _penalty_body,
    _refresh_attempt_exemptions,
    _repair_penalty_metadata,
    _reviewable_events,
    _season_offense_number,
    _sequence_pair,
    _sync_blocks,
    _sync_event_fixture_shares,
    enforce_fourth_penalties,
    integrity_snapshot,
    process_settled_bets,
)


class MatchIntegrityPatternTests(unittest.TestCase):
    def setUp(self):
        self.output = {
            "managed_teams": [{"id": 10, "name": "玩家队", "address": "0x10"}],
        }
        self.match = {
            "fixture_date": "2030-01-02",
            "competition_id": 1,
            "home": {"id": 10, "name": "玩家队", "profile": {}, "address": "0x10"},
            "away": {"id": 20, "name": "对手队", "profile": {}},
        }

    def snapshot(self, market, code, line=None, odds=5.0):
        return integrity_snapshot(
            self.output, self.match, market, code, odds,
            line=line, opponent_win_odds=9.0,
            selection_label=f"{market}:{code}:{line}",
        )

    def test_only_control_markets_and_opponent_overs_are_monitored(self):
        monitored = (
            ("SCORE", "exact", "5-0"),
            ("HT_SCORE", "exact", "2-1"),
            ("SH_SCORE", "exact", "2-1"),
            ("TOTAL_GOALS", "exact", 4),
            ("TOTAL_GOALS", "seven_plus", 7),
            ("HT_TOTAL_GOALS", "exact", 2),
            ("SH_TOTAL_GOALS", "exact", 2),
            ("TEAM_GOALS", "home", 2),
            ("TEAM_GOALS", "away", 3),
            ("TEAM_GOALS", "away_plus", 5),
            ("HT_TEAM_GOALS", "away", 1),
            ("SH_TEAM_GOALS", "home", 1),
            ("HTFT", "away_home", None),
            ("HTFT", "home_away", None),
            ("WINNING_MARGIN", "away_3_plus", None),
            ("CLEAN_SHEET", "away_yes", None),
            ("WIN_TO_NIL", "away", None),
            ("ADVANCE_METHOD", "20_penalties", None),
            ("TEAM_OU", "away_over", 2.5),
        )
        for market, code, line in monitored:
            with self.subTest(market=market, code=code, line=line):
                self.assertTrue(self.snapshot(market, code, line)["monitoring_suspicious"])

    def test_non_control_and_coarse_goal_markets_are_not_monitored(self):
        ignored = (
            ("BTTS", "yes", None),
            ("BTTS", "no", None),
            ("GOAL_PARITY", "odd", None),
            ("HT_1X2", "away", None),
            ("SH_1X2", "away", None),
            ("TOTAL_GOALS", "six_plus", 6),
            ("HT_TOTAL_GOALS", "three_plus", 3),
            ("SH_TOTAL_GOALS", "two_plus", 2),
            ("TEAM_GOALS", "home_plus", 5),
            ("TEAM_GOALS", "away_plus", 4),
            ("HT_TEAM_GOALS", "away_plus", 2),
            ("SH_TEAM_GOALS", "away_plus", 2),
            ("TEAM_OU", "away_under", 2.5),
            ("TEAM_OU", "home_over", 2.5),
        )
        for market, code, line in ignored:
            with self.subTest(market=market, code=code, line=line):
                self.assertFalse(self.snapshot(market, code, line)["monitoring_suspicious"])

    def test_whitelists_and_btts_are_exempt(self):
        cases = (
            ("BTTS", "yes", None),
            ("BTTS", "no", None),
            ("GOAL_PARITY", "even", None),
            ("1X2", "home", None),
            ("HT_1X2", "home", None),
            ("SH_1X2", "home", None),
            ("HTFT", "home_home", None),
            ("HTFT", "draw_home", None),
            ("WIN_TO_NIL", "home", None),
            ("WINNING_MARGIN", "home_1", None),
            ("WINNING_MARGIN", "home_2", None),
            ("WINNING_MARGIN", "home_3_plus", None),
            ("ADVANCE_METHOD", "10_penalties", None),
            ("TOTAL_GOALS", "exact", 1),
            ("TOTAL_GOALS", "exact", 2),
            ("TOTAL_GOALS", "exact", 3),
            ("SCORE", "exact", "1-0"),
            ("SCORE", "exact", "2-0"),
            ("SCORE", "exact", "3-0"),
            ("SCORE", "exact", "2-1"),
            ("SCORE", "exact", "1-1"),
            ("SCORE", "exact", "3-1"),
            ("SCORE", "exact", "4-0"),
            ("SCORE", "exact", "4-1"),
        )
        for market, code, line in cases:
            with self.subTest(market=market, code=code, line=line):
                evidence = self.snapshot(market, code, line)
                self.assertTrue(evidence["monitoring_exempt"])
                self.assertFalse(evidence["monitoring_suspicious"])

    def test_score_whitelist_uses_managed_team_perspective(self):
        away_output = {"managed_teams": [{"id": 20, "name": "玩家队"}]}
        safe = integrity_snapshot(
            away_output, self.match, "SCORE", "exact", 5.0, line="1-2",
        )
        unsafe = integrity_snapshot(
            away_output, self.match, "SCORE", "exact", 5.0, line="2-1",
        )
        self.assertTrue(safe["monitoring_exempt"])
        self.assertFalse(unsafe["monitoring_exempt"])

    def test_managed_winning_margin_exemption_uses_team_perspective(self):
        home_safe = self.snapshot("WINNING_MARGIN", "home_2")
        home_unsafe = self.snapshot("WINNING_MARGIN", "away_2")
        away_output = {"managed_teams": [{"id": 20, "name": "玩家队"}]}
        away_safe = integrity_snapshot(
            away_output, self.match, "WINNING_MARGIN", "away_3_plus", 5.0,
            opponent_win_odds=9.0,
        )
        away_unsafe = integrity_snapshot(
            away_output, self.match, "WINNING_MARGIN", "home_3_plus", 5.0,
            opponent_win_odds=9.0,
        )

        for evidence in (home_safe, away_safe):
            self.assertEqual(
                evidence["monitoring_exemption"], "managed_team_winning_margin",
            )
            self.assertFalse(evidence["monitoring_suspicious"])
            self.assertIsNone(evidence["direct_trigger_kind"])
        for evidence in (home_unsafe, away_unsafe):
            self.assertTrue(evidence["monitoring_suspicious"])
            self.assertEqual(evidence["direct_trigger_kind"], "opponent_winning_margin")

    @staticmethod
    def contribution(
        identifier, fixture, group, code, line, stake, outcome,
        *, suspicious=True, exempt=False, odds=5.0, game_date="2030-01-01",
        competition_kind="league",
    ):
        return {
            "id": identifier,
            "bet_id": identifier,
            "fixture_key": fixture,
            "group": group,
            "selection_key": json.dumps([code, line], separators=(",", ":")),
            "selection_label": f"{group}:{code}:{line}",
            "exempt": exempt,
            "exemption": "safe" if exempt else None,
            "suspicious": suspicious,
            "direct_candidate": False,
            "outcome": outcome,
            "stake": stake,
            "ticket_stake": stake,
            "odds": odds,
            "profit": 8_000_000.0 if outcome == "won" else 0.0,
            "game_date": game_date,
            "fixture_date": game_date,
            "competition_id": 1,
            "competition_name": "测试赛事",
            "competition_kind": competition_kind,
            "home_id": 10,
            "away_id": 20,
            "home": "玩家队",
            "away": "对手队",
            "home_goals": 5 if outcome == "won" else 4,
            "away_goals": 0,
            "half_score": "2-0",
            "managed_team_id": 10,
            "managed_team_name": "玩家队",
            "managed_team_address": "0x10",
            "managed_side": "home",
            "opponent_team_id": 20,
            "opponent_team_name": "对手队",
            "manager_name": "玩家姓名",
            "weekly_salary": 1000,
        }

    def test_same_market_hedge_requires_second_option_below_half(self):
        balanced = {"attempts": []}
        _merge_attempt_contributions(balanced, [
            self.contribution("b1", "f1", "SCORE", "exact", "5-0", 10_000_000, "won"),
            self.contribution("b2", "f1", "SCORE", "exact", "4-0", 5_000_000, "lost"),
        ])
        self.assertEqual(balanced["attempts"][0]["status"], "ignored")
        self.assertEqual(balanced["attempts"][0]["groups"][0]["status"], "ignored_multi_choice")

        dominant = {"attempts": []}
        _merge_attempt_contributions(dominant, [
            self.contribution("b1", "f1", "SCORE", "exact", "5-0", 10_000_000, "won"),
            self.contribution("b2", "f1", "SCORE", "exact", "4-0", 4_999_999, "lost"),
        ])
        self.assertEqual(dominant["attempts"][0]["status"], "ignored")
        self.assertEqual(
            dominant["attempts"][0]["groups"][0]["status"], "ignored_match_share",
        )

    def test_fixture_requires_ten_million_and_at_least_75_percent(self):
        below_amount = {"attempts": []}
        _merge_attempt_contributions(below_amount, [
            self.contribution("b1", "f1", "SCORE", "exact", "5-0", 9_999_999, "won"),
        ])
        self.assertFalse(below_amount["attempts"][0]["fixture_anomaly_amount_qualified"])

        exact_amount = {"attempts": []}
        _merge_attempt_contributions(exact_amount, [
            self.contribution("b1", "f1", "SCORE", "exact", "5-0", 10_000_000, "won"),
        ])
        self.assertEqual(exact_amount["attempts"][0]["status"], "hit")

        exact_share = {"attempts": []}
        _merge_attempt_contributions(exact_share, [
            self.contribution("b1", "f1", "SCORE", "exact", "5-0", 12_000_000, "won"),
            self.contribution(
                "b2", "f1", "BTTS", "yes", None, 4_000_000, "won",
                suspicious=False, exempt=True,
            ),
        ])
        attempt = exact_share["attempts"][0]
        self.assertEqual(attempt["anomalous_stake_share"], 0.75)
        self.assertTrue(attempt["fixture_anomaly_share_qualified"])

    def test_abnormal_categories_combine_and_one_hit_is_not_reset_by_a_miss(self):
        payload = {"attempts": []}
        _merge_attempt_contributions(payload, [
            self.contribution("b1", "f1", "SCORE", "exact", "5-0", 6_000_000, "won"),
            self.contribution("b2", "f1", "HTFT", "home_away", None, 5_000_000, "lost"),
            self.contribution(
                "b3", "f1", "BTTS", "yes", None, 1_000_000, "won",
                suspicious=False, exempt=True,
            ),
        ])
        attempt = payload["attempts"][0]
        self.assertEqual(attempt["anomalous_fixture_stake"], 11_000_000)
        self.assertGreater(attempt["anomalous_stake_share"], 0.75)
        self.assertEqual(attempt["status"], "hit")
        self.assertEqual(
            {row["group"]: row["status"] for row in attempt["groups"]},
            {"BTTS": "ignored_whitelist", "HTFT": "miss", "SCORE": "hit"},
        )

    def test_persisted_contributions_are_reclassified_with_current_rules(self):
        payload = {"attempts": []}
        row = self.contribution(
            "b1", "f1", "BTTS", "yes", None, 10_000_000, "won",
            suspicious=True,
        )
        _merge_attempt_contributions(payload, [row])
        self.assertEqual(payload["attempts"][0]["status"], "hit")
        self.assertTrue(_refresh_attempt_exemptions(payload))
        self.assertEqual(payload["attempts"][0]["status"], "ignored")
        self.assertEqual(row["exemption"], "both_teams_to_score")

    def test_persisted_managed_winning_margin_is_reclassified_as_exempt(self):
        payload = {"attempts": []}
        row = self.contribution(
            "b1", "f1", "WINNING_MARGIN", "home_2", None,
            10_000_000, "won", suspicious=True,
        )
        row["managed_side"] = "home"
        row["managed_team_id"] = 10
        _merge_attempt_contributions(payload, [row])
        self.assertEqual(payload["attempts"][0]["status"], "hit")

        self.assertTrue(_refresh_attempt_exemptions(payload))
        self.assertEqual(payload["attempts"][0]["status"], "ignored")
        self.assertEqual(row["exemption"], "managed_team_winning_margin")
        self.assertFalse(row["suspicious"])

    @staticmethod
    def attempt(fixture, game_date, status, *, season="2029-07-01", qualified=True):
        return {
            "fixture_key": fixture,
            "game_date": game_date,
            "status": status,
            "fixture_anomaly_qualified": qualified,
            "season_key": season,
        }

    def test_latest_four_qualifying_attempts_require_three_hits(self):
        payload = {"attempts": [
            self.attempt("a", "2030-01-01", "hit"),
            self.attempt("b", "2030-01-02", "miss"),
            self.attempt("c", "2030-01-03", "hit"),
            self.attempt("d", "2030-01-04", "hit"),
        ]}
        sequence = _sequence_pair(payload, "2030-01-05", "2029-07-01")
        self.assertEqual(
            [entry["attempt"]["fixture_key"] for entry in sequence["entries"]],
            ["a", "c", "d"],
        )
        self.assertEqual([row["fixture_key"] for row in sequence["attempts"]], ["a", "b", "c", "d"])

        payload["attempts"].append(self.attempt("e", "2030-01-05", "miss"))
        self.assertIsNone(_sequence_pair(payload, "2030-01-06", "2029-07-01"))

    def test_three_hits_in_three_attempts_qualify_and_ignored_fixtures_do_not_count(self):
        payload = {"attempts": [
            self.attempt("a", "2030-01-01", "hit"),
            self.attempt("ignored", "2030-01-02", "ignored", qualified=False),
            self.attempt("c", "2030-01-03", "hit"),
            self.attempt("d", "2030-01-04", "hit"),
        ]}
        sequence = _sequence_pair(payload, "2030-01-05", "2029-07-01")
        self.assertEqual(len(sequence["attempts"]), 3)
        self.assertEqual(len(sequence["entries"]), 3)

    def make_record(
        self, bet_id, fixture_date, *, outcome="won", market="SCORE",
        code="exact", line="5-0", odds=5.0, opponent_win_odds=9.0,
        competition_kind="league",
    ):
        match = {**self.match, "fixture_date": fixture_date}
        evidence = integrity_snapshot(
            self.output, match, market, code, odds,
            line=line, opponent_win_odds=opponent_win_odds,
            selection_label=f"{market} {code} {line}",
        )
        score = "5-0" if outcome == "won" else "4-0"
        return {
            "bet_id": bet_id,
            "status": outcome,
            "stake": 10_000_000,
            "payout": 18_000_000 if outcome == "won" else 0,
            "integrity_manager_name": "玩家姓名",
            "integrity_weekly_salary": 1000,
            "legs": [{
                "integrity": evidence,
                "fixture_date": fixture_date,
                "competition_id": 1,
                "competition_name": "测试赛事",
                "competition_kind": competition_kind,
                "home_id": 10,
                "away_id": 20,
                "home": "玩家队",
                "away": "对手队",
                "market": market,
                "selection_code": code,
                "selection": f"{market} {code} {line}",
                "line": line,
                "odds": odds,
            }],
            "settlement": [{
                "outcome": outcome,
                "score": score,
                "half_score": "2-0",
                "result_date": fixture_date,
            }],
        }

    @staticmethod
    def financial_result(confiscation, fine, *, maximum_total=None):
        actual_confiscation = min(confiscation, maximum_total) if maximum_total is not None else confiscation
        actual_fine = (
            min(fine, max(0, maximum_total - actual_confiscation))
            if maximum_total is not None else fine
        )
        return {
            "confiscation_requested": confiscation,
            "confiscated": actual_confiscation,
            "standard_fine": fine,
            "actual_fine": actual_fine,
            "bank_used": actual_confiscation + actual_fine,
            "bank_overdraft": 0.0,
            "wallet_used": 0.0,
            "total_deducted": actual_confiscation + actual_fine,
        }

    def run_process(self, records, payload, game_date="2030-01-05"):
        output = {
            "game_date": game_date,
            "season_start": "2029-07-01",
            "season_end": "2030-06-30",
            "matches": [],
            "managed_teams": self.output["managed_teams"],
        }
        with patch("tools.match_integrity.integrity_bet_snapshot", return_value=records), patch(
            "tools.match_integrity.mark_integrity_bets_assessed"
        ), patch("tools.match_integrity._load_state", return_value=payload), patch(
            "tools.match_integrity._save_state"
        ), patch(
            "tools.match_integrity.collect_match_integrity_penalty",
            side_effect=self.financial_result,
        ) as collect:
            penalty = process_settled_bets(
                [record["bet_id"] for record in records], output, "玩家姓名", 1000,
            )
        return penalty, collect

    def test_three_of_four_triggers_next_day_and_consumes_full_window(self):
        records = [
            self.make_record("b1", "2030-01-01"),
            self.make_record("b2", "2030-01-02", outcome="lost"),
            self.make_record("b3", "2030-01-03"),
            self.make_record("b4", "2030-01-04"),
        ]
        payload = {"schema_version": 3, "events": [], "attempts": [], "penalties": []}
        penalty, collect = self.run_process(records, payload)
        self.assertIsNotNone(penalty)
        self.assertEqual(len(penalty["events"]), 3)
        self.assertFalse(penalty["direct_trigger"])
        self.assertEqual(penalty["season_offense_number"], 1)
        self.assertEqual(penalty["profit_confiscation_rate"], 0.5)
        self.assertEqual(penalty["stake_confiscation_rate"], 0.0)
        self.assertEqual(penalty["blocked_match_count"], 1)
        self.assertNotIn("fourth_effective", penalty)
        self.assertIn("未来1场比赛盘口", penalty["body"])
        self.assertNotIn("四、", penalty["body"])
        self.assertEqual(penalty["total_deducted"], 12_000_000.0)
        self.assertEqual(penalty["actual_fine"], 0.0)
        collect.assert_called_once_with(
            12_000_000.0, 0.0, maximum_total=24_000_000.0,
        )
        self.assertTrue(all(row.get("cycle_penalty_id") == penalty["id"] for row in payload["attempts"]))
        self.assertIsNone(_sequence_pair(payload, "2030-01-06", "2029-07-01"))

    def test_second_offense_uses_repeat_financial_and_sporting_penalties(self):
        records = [
            self.make_record("n1", "2030-02-01"),
            self.make_record("n2", "2030-02-02"),
            self.make_record("n3", "2030-02-03"),
        ]
        payload = {
            "schema_version": 3,
            "events": [],
            "attempts": [],
            "penalties": [{"id": "old", "game_date": "2030-01-05", "season_key": "2029-07-01"}],
        }
        penalty, collect = self.run_process(records, payload, "2030-02-04")
        self.assertEqual(penalty["season_offense_number"], 2)
        self.assertEqual(penalty["profit_confiscation_rate"], 0.5)
        self.assertEqual(penalty["stake_confiscation_rate"], 0.0)
        self.assertEqual(penalty["blocked_match_count"], 2)
        self.assertTrue(penalty["fourth_effective"])
        self.assertEqual(penalty["reputation_delta"], -100)
        self.assertIn("联赛积分3分", penalty["body"])
        self.assertIn("声望100点", penalty["body"])
        self.assertEqual(penalty["total_deducted"], 12_000_000.0)
        self.assertEqual(penalty["actual_fine"], 0.0)
        collect.assert_called_once_with(
            12_000_000.0, 0.0, maximum_total=24_000_000.0,
        )

    def test_new_season_resets_offense_number(self):
        payload = {
            "penalties": [{
                "id": "old", "game_date": "2030-05-01", "season_key": "2029-07-01",
            }],
        }
        output = {"season_start": "2030-07-01", "season_end": "2031-06-30"}
        self.assertEqual(_season_offense_number(payload, output, "2030-07-01"), 1)

    def test_separate_penalties_reserve_distinct_future_fixtures(self):
        matches = []
        for day in (10, 11, 12):
            matches.append({
                "fixture_date": f"2030-02-{day}",
                "competition_id": 1,
                "home": {"id": 10, "name": "玩家队"},
                "away": {"id": 20 + day, "name": f"对手{day}"},
            })
        payload = {"penalties": [{
            "id": "first",
            "managed_team_ids": [10],
            "blocked_match_count": 1,
            "blocked_fixtures": [],
            "events": [],
        }, {
            "id": "second",
            "managed_team_ids": [10],
            "blocked_match_count": 2,
            "blocked_fixtures": [],
            "events": [],
        }]}
        self.assertTrue(_sync_blocks(payload, {"matches": matches}))
        keys = [
            row["fixture_key"]
            for penalty in payload["penalties"]
            for row in penalty["blocked_fixtures"]
        ]
        self.assertEqual(len(keys), 3)
        self.assertEqual(len(set(keys)), 3)

    def test_direct_punishment_requires_opponent_win_odds_above_eight(self):
        for odds, expected in ((8.0, False), (8.01, True)):
            with self.subTest(odds=odds):
                record = self.make_record(
                    f"direct-{odds}", "2030-01-01",
                    market="1X2", code="away", line=None, odds=odds,
                    opponent_win_odds=odds,
                )
                payload = {"schema_version": 3, "events": [], "attempts": [], "penalties": []}
                penalty, _collect = self.run_process([record], payload, "2030-01-02")
                self.assertEqual(penalty is not None, expected)
                if penalty:
                    self.assertTrue(penalty["direct_trigger"])

    def test_expanded_direct_markets_are_classified_and_pure_draws_are_allowed(self):
        cases = (
            ("1X2", "away", None, 8.01),
            ("1X2", "draw", None, 8.01),
            ("HT_1X2", "away", None, 5.0),
            ("SH_1X2", "away", None, 5.0),
            ("HTFT", "away_home", None, 5.0),
            ("HTFT", "home_draw", None, 5.0),
            ("TEAM_GOALS", "away_plus", 5, 5.0),
            ("CLEAN_SHEET", "away_yes", None, 5.0),
            ("WIN_TO_NIL", "away", None, 5.0),
            ("AH", "away", 0.25, 8.01),
            ("HT_AH", "away", 0.25, 8.01),
            ("SH_AH", "away", 0.25, 8.01),
            ("WINNING_MARGIN", "away_1", None, 5.0),
            ("WINNING_MARGIN", "away_2", None, 5.0),
            ("WINNING_MARGIN", "away_3_plus", None, 5.0),
            ("WINNING_MARGIN", "draw", None, 5.0),
        )
        for index, (market, code, line, selected_odds) in enumerate(cases):
            with self.subTest(market=market, code=code):
                record = self.make_record(
                    f"direct-case-{index}", "2030-03-01", market=market,
                    code=code, line=line, odds=selected_odds,
                    opponent_win_odds=9.0,
                )
                event = _event_from_leg(record, record["legs"][0], record["settlement"][0])
                self.assertIsNotNone(event)
                self.assertTrue(event["direct_trigger_kind"])

    def test_direct_exclusions_and_handicap_boundary(self):
        cases = (
            ("DOUBLE_CHANCE", "draw_away", None, 20.0, False),
            ("SCORE", "exact", "1-1", 20.0, False),
            ("HTFT", "home_home", None, 20.0, False),
            ("AH", "away", 0.25, 8.0, False),
        )
        for index, (market, code, line, odds, expected) in enumerate(cases):
            with self.subTest(market=market, code=code):
                record = self.make_record(
                    f"direct-excluded-{index}", "2030-03-02", market=market,
                    code=code, line=line, odds=odds, opponent_win_odds=9.0,
                )
                event = _event_from_leg(record, record["legs"][0], record["settlement"][0])
                self.assertEqual(event is not None, expected)

    def test_exempt_half_full_draw_can_trigger_direct_penalty(self):
        record = self.make_record(
            "draw-home", "2030-03-03", market="HTFT", code="draw_home",
            line=None, odds=5.0, opponent_win_odds=9.0,
        )
        self.assertTrue(record["legs"][0]["integrity"]["monitoring_exempt"])
        payload = {"schema_version": 3, "events": [], "attempts": [], "penalties": []}
        penalty, _collect = self.run_process([record], payload, "2030-03-04")
        self.assertIsNotNone(penalty)
        self.assertTrue(penalty["direct_trigger"])
        self.assertIn("HTFT draw_home", penalty["body"])
        self.assertNotIn("对手队全场获胜", penalty["body"])

    def test_handicap_half_win_counts_in_fixture_total_but_not_as_direct_hit(self):
        record = self.make_record(
            "half-won", "2030-03-03", outcome="half_won", market="AH",
            code="away", line=0.25, odds=9.0, opponent_win_odds=9.0,
        )
        payload = {"schema_version": 3, "events": [], "attempts": [], "penalties": []}
        penalty, _collect = self.run_process([record], payload, "2030-03-04")
        self.assertIsNone(penalty)
        self.assertEqual(payload["attempts"][0]["total_fixture_stake"], 10_000_000)
        self.assertEqual(payload["attempts"][0]["anomalous_fixture_stake"], 0)

    def test_direct_punishment_consumes_prior_attempt_window(self):
        payload = {"schema_version": 3, "events": [], "attempts": [], "penalties": []}
        _merge_attempt_contributions(payload, [
            self.contribution(
                "old-1", "old-fixture-1", "SCORE", "exact", "5-0",
                10_000_000, "won", game_date="2030-01-01",
            ),
            self.contribution(
                "old-2", "old-fixture-2", "SCORE", "exact", "5-0",
                10_000_000, "won", game_date="2030-01-02",
            ),
        ])
        direct = self.make_record(
            "direct", "2030-01-03", market="1X2", code="away", line=None, odds=8.01,
        )
        penalty, _collect = self.run_process([direct], payload, "2030-01-04")
        self.assertTrue(penalty["direct_trigger"])
        self.assertEqual(len(payload["attempts"]), 3)
        self.assertTrue(all(row.get("cycle_penalty_id") == penalty["id"] for row in payload["attempts"]))

    def test_direct_event_still_requires_fixture_amount_and_share(self):
        evidence = self.snapshot("1X2", "away", None, odds=10.01)
        record = self.make_record(
            "direct", "2030-01-01", market="1X2", code="away", line=None, odds=8.01,
        )
        event = _event_from_leg(record, record["legs"][0], record["settlement"][0])
        self.assertIsNotNone(event)
        event["stake_by_bet"] = {"direct": 10_000_000}
        payload = {
            "attempts": [],
            "events": [event],
        }
        direct = self.contribution(
            "direct", event["fixture_key"], "1X2", "away", None,
            10_000_000, "won", suspicious=False, odds=8.01,
        )
        direct["direct_candidate"] = True
        safe = self.contribution(
            "safe", event["fixture_key"], "BTTS", "yes", None,
            4_000_000, "won", suspicious=False, exempt=True,
        )
        _merge_attempt_contributions(payload, [direct, safe])
        _sync_event_fixture_shares(payload)
        self.assertFalse(event["direct_trigger"])
        self.assertFalse(payload["attempts"][0]["fixture_anomaly_share_qualified"])

    def test_review_delay_is_one_game_day(self):
        payload = {"events": [
            {"fixture_key": "old", "game_date": "2030-01-01", "direct_trigger": True},
            {"fixture_key": "today", "game_date": "2030-01-02", "direct_trigger": True},
        ]}
        self.assertEqual(
            [row["fixture_key"] for row in _reviewable_events(payload, "2030-01-02")],
            ["old"],
        )

    def test_first_and_repeat_mail_text_match_actual_effects(self):
        event = {
            "game_date": "2030-01-02",
            "home": "玩家队",
            "away": "对手队",
            "home_goals": 0,
            "away_goals": 2,
            "competition_name": "测试联赛",
            "competition_kind": "league",
            "opponent_team_name": "对手队",
            "stake": 10_000_000,
            "net_profit": 8_000_000,
            "integrity_pattern": "opponent_full_time_win",
        }
        first = {
            "confiscation_requested": 4_000_000,
            "confiscated": 4_000_000,
            "confiscated_profit": 4_000_000,
            "confiscated_stake": 0,
            "standard_fine": 0,
            "actual_fine": 0,
            "season_offense_number": 1,
            "profit_confiscation_rate": 0.5,
            "stake_confiscation_rate": 0,
            "salary_weeks": 0,
            "blocked_match_count": 1,
        }
        repeat = {
            **first,
            "season_offense_number": 2,
            "blocked_match_count": 2,
        }
        first_body = _penalty_body("玩家姓名", [event], first, "2030-01-03")
        repeat_body = _penalty_body("玩家姓名", [event], repeat, "2030-01-03")
        self.assertIn("净盈利的50%", first_body)
        self.assertNotIn("投注本金", first_body)
        self.assertNotIn("工资", first_body)
        self.assertNotIn("四、", first_body)
        self.assertNotIn("投注本金", repeat_body)
        self.assertNotIn("工资", repeat_body)
        self.assertIn("未来2场比赛盘口", repeat_body)
        self.assertIn("联赛积分3分；降低您执教俱乐部声望100点", repeat_body)

    def test_old_penalty_repairs_zero_stake_and_manager_name(self):
        event = {
            "fixture_key": "f1",
            "game_date": "2030-01-02",
            "home": "玩家队",
            "away": "对手队",
            "home_goals": 0,
            "away_goals": 2,
            "competition_name": "测试",
            "competition_kind": "league",
            "opponent_team_name": "对手队",
            "stake": 0,
            "net_profit": 0,
            "integrity_pattern": "opponent_full_time_win",
            "manager_name": "经理",
            "bet_ids": ["old-bet"],
        }
        penalty = {
            "id": "penalty-1",
            "game_date": "2030-01-03",
            "events": [event],
            "confiscated": 200,
            "confiscated_stake": 50,
            "confiscated_profit": 100,
            "standard_fine": 400,
            "actual_fine": 400,
        }
        payload = {"attempts": [], "events": [], "penalties": [penalty]}
        changed = _repair_penalty_metadata(payload, {
            "selected_manager_id": 7,
            "manager_options": [{"id": 7, "name": "玩家姓名"}],
        }, "")
        self.assertTrue(changed)
        self.assertEqual(event["stake"], 100)
        self.assertEqual(event["net_profit"], 100)
        self.assertEqual(event["manager_name"], "玩家姓名")
        self.assertIn("尊敬的玩家姓名：", penalty["body"])

    def test_penalty_stops_when_available_balances_are_exhausted(self):
        economy = {"general_balance": 100.0, "transactions": []}
        with patch("tools.club_economy.load_economy", return_value=economy), patch(
            "tools.club_economy.available_balance", return_value=50.0
        ), patch(
            "tools.club_economy._save_economy_with_wallet_adjustments"
        ) as save:
            result = collect_match_integrity_penalty(200.0, 300.0)
        self.assertEqual(result["total_deducted"], 150.0)
        self.assertEqual(result["confiscated"], 150.0)
        self.assertEqual(result["actual_fine"], 0.0)
        self.assertEqual(result["wallet_used"], 50.0)
        self.assertEqual(result["bank_overdraft"], 0.0)
        self.assertEqual(economy["general_balance"], 0.0)
        save.assert_called_once()
        self.assertEqual(save.call_args.args[0], economy)
        self.assertEqual(save.call_args.args[1][0]["amount"], -50.0)
        self.assertEqual(save.call_args.args[1][0]["type"], "match_integrity_penalty")

    def test_penalty_total_can_be_capped_at_related_net_profit(self):
        economy = {"general_balance": 1000.0, "transactions": []}
        with patch("tools.club_economy.load_economy", return_value=economy), patch(
            "tools.club_economy.available_balance", return_value=0.0
        ), patch("tools.club_economy._save_economy_with_wallet_adjustments"):
            result = collect_match_integrity_penalty(
                380.0, 200.0, maximum_total=100.0,
            )
        self.assertEqual(result["confiscation_requested"], 380.0)
        self.assertEqual(result["confiscated"], 100.0)
        self.assertEqual(result["actual_fine"], 0.0)
        self.assertEqual(result["total_deducted"], 100.0)
        self.assertEqual(economy["general_balance"], 900.0)

    def test_repeat_mixed_competition_penalty_applies_points_and_reputation_once(self):
        payload = {"penalties": [{
            "id": "p1",
            "created_at": "2030-01-01T09:00:00",
            "game_date": "2030-01-01",
            "season_offense_number": 2,
            "fourth_effective": True,
            "reputation_delta": -100,
            "events": [{
                "managed_team_id": 10,
                "competition_id": 20,
                "competition_kind": "league",
                "managed_team_address": "0x10",
            }, {
                "managed_team_id": 10,
                "competition_id": 30,
                "competition_kind": "cup",
                "managed_team_address": "0x10",
            }],
        }, {
            "id": "first-malformed",
            "season_offense_number": 1,
            "fourth_effective": True,
            "events": [{
                "managed_team_id": 10,
                "competition_id": 40,
                "competition_kind": "league",
            }],
        }]}
        points = []
        reputations = []
        with patch("tools.match_integrity._load_state", return_value=payload), patch(
            "tools.match_integrity._save_state"
        ):
            first = enforce_fourth_penalties(
                {"managed_teams": []},
                apply_points=lambda output, item: points.append(item),
                apply_reputation=lambda team_id, address, delta: reputations.append(
                    (team_id, address, delta)
                ),
            )
            second = enforce_fourth_penalties(
                {"managed_teams": []},
                apply_points=lambda output, item: points.append(item),
                apply_reputation=lambda team_id, address, delta: reputations.append(
                    (team_id, address, delta)
                ),
            )
        self.assertEqual(len([row for row in first if row.get("applied")]), 2)
        self.assertEqual(second, [])
        self.assertEqual(len(points), 1)
        self.assertEqual(points[0]["delta"], -3)
        self.assertEqual(
            points[0]["source_id"],
            "match_integrity:p1:10:20:league_points",
        )
        self.assertEqual(points[0]["legacy_source_game_date"], "2030-01-01")
        self.assertEqual(reputations, [(10, "0x10", -100)])


if __name__ == "__main__":
    unittest.main()
