from __future__ import annotations

import threading
import unittest
from contextlib import nullcontext
from unittest.mock import patch

from conftest import legacy_web_source

from fm_odds_web import LocalOddsState
from tools.betting_account import (
    RESULT_SEARCH_WAIT_DAYS,
    pending_bet_result_search,
    pending_detail_capture_keys,
)


def _single(bet_id: str = "bet-1", fixture_date: str = "2028-06-01") -> dict:
    return {
        "bet_id": bet_id,
        "type": "single",
        "status": "pending",
        "fixture_date": fixture_date,
        "competition_id": 300,
        "home_id": 10,
        "away_id": 20,
    }


class PendingResultSearchTests(unittest.TestCase):
    def test_live_detail_capture_is_limited_to_detail_markets_and_match_window(self) -> None:
        detail = {
            **_single(fixture_date="2028-06-04"),
            "market": "HT_AH", "kickoff_minutes": 900,
        }
        full_time = {
            **_single("full-time", fixture_date="2028-06-04"),
            "home_id": 30, "away_id": 40,
            "market": "1X2", "kickoff_minutes": 900,
        }
        with patch("tools.betting_account.load_bets", return_value=[detail, full_time]):
            self.assertEqual(
                pending_detail_capture_keys("2028-06-04", 870),
                {("2028-06-04", 300, 10, 20)},
            )
            self.assertEqual(
                pending_detail_capture_keys("2028-06-04", 1080),
                {("2028-06-04", 300, 10, 20)},
            )
            self.assertEqual(pending_detail_capture_keys("2028-06-04", 1081), set())
            self.assertEqual(pending_detail_capture_keys("2028-06-05", 0), set())

    def test_search_unlocks_after_three_game_days(self) -> None:
        with patch("tools.betting_account.load_bets", return_value=[_single()]):
            with self.assertRaisesRegex(ValueError, "未满3天"):
                pending_bet_result_search("bet-1", "2028-06-03")
            request = pending_bet_result_search("bet-1", "2028-06-04")

        self.assertEqual(RESULT_SEARCH_WAIT_DAYS, 3)
        self.assertEqual(request["overdue_days"], 3)
        self.assertEqual(request["keys"], {("2028-06-01", 300, 10, 20)})

    def test_parlay_waits_three_days_after_its_last_fixture(self) -> None:
        record = {
            "bet_id": "parlay-1",
            "type": "parlay",
            "status": "pending",
            "legs": [
                {**_single(fixture_date="2028-06-01"), "bet_id": None},
                {
                    **_single(fixture_date="2028-06-04"),
                    "bet_id": None,
                    "home_id": 30,
                    "away_id": 40,
                },
            ],
        }
        with patch("tools.betting_account.load_bets", return_value=[record]):
            with self.assertRaisesRegex(ValueError, "未满3天"):
                pending_bet_result_search("parlay-1", "2028-06-06")
            request = pending_bet_result_search("parlay-1", "2028-06-07")

        self.assertEqual(request["latest_fixture_date"], "2028-06-04")
        self.assertEqual(len(request["keys"]), 2)

    def test_championship_bet_uses_championship_settlement_instead(self) -> None:
        record = {
            "bet_id": "champ-1", "type": "championship", "status": "pending",
        }
        with patch("tools.betting_account.load_bets", return_value=[record]):
            with self.assertRaisesRegex(ValueError, "冠军盘"):
                pending_bet_result_search("champ-1", "2028-06-09")

    def test_state_searches_only_requested_keys_and_settles_found_result(self) -> None:
        state = LocalOddsState.__new__(LocalOddsState)
        state.lock = threading.RLock()
        state.output = {"game_date": "2028-06-10", "season_results": []}
        state.data_version = 7
        state.refreshing = False
        state.reconciling = False
        state.save_change_pending = False
        key = ("2028-06-01", 300, 10, 20)
        result = {
            "date": key[0],
            "competition": {"id": key[1]},
            "home_team": {"id": key[2]},
            "away_team": {"id": key[3]},
            "home_goals": 2,
            "away_goals": 1,
        }
        settlement = {
            "settled": 1,
            "returned": 200.0,
            "settled_records": [{"bet_id": "bet-1"}],
        }

        with (
            patch.object(state, "_bind_current_save"),
            patch.object(state, "_timed_memory_lock", return_value=nullcontext()),
            patch.object(state, "_finalize_settlement") as finalize,
            patch.object(state, "_merge_light_results", return_value=({**state.output}, 1)),
            patch("fm_odds_web.read_game_clock", return_value={"date": "2028-06-10"}),
            patch("fm_odds_web.pending_bet_result_search", return_value={"keys": {key}}),
            patch("fm_odds_web.pending_result_fixture_hints", return_value={key: 0x1234}),
            patch("fm_odds_web.read_completed_results", return_value=[result]) as read,
            patch("fm_odds_web.settle_pending_bets", return_value=settlement) as settle,
        ):
            response = state.search_pending_bet_result({"bet_id": "bet-1"})

        read.assert_called_once_with(
            {key}, fixture_hints={key: 0x1234}, deep_recovery=True,
        )
        settle.assert_called_once_with(
            [result], "2028-06-10", game_minutes=0,
            trusted_same_day_keys={key},
            void_missing_after_timeout=True,
        )
        finalize.assert_called_once_with(settlement, {"game_date": "2028-06-10", "season_results": []})
        self.assertTrue(response["target_settled"])
        self.assertEqual(response["found"], 1)
        self.assertEqual(state.data_version, 8)

    def test_state_uses_exact_cached_result_when_memory_no_longer_retains_it(self) -> None:
        state = LocalOddsState.__new__(LocalOddsState)
        state.lock = threading.RLock()
        state.output = {
            "game_date": "2028-06-10",
            "season_results": [{
                "date": "2028-06-01",
                "competition_id": 300,
                "home": {"id": 10, "name": "Home"},
                "away": {"id": 20, "name": "Away"},
                "home_goals": 2,
                "away_goals": 1,
            }],
            "settlement_results": [],
        }
        state.data_version = 7
        state.refreshing = False
        state.reconciling = False
        state.save_change_pending = False
        key = ("2028-06-01", 300, 10, 20)
        settlement = {
            "settled": 1,
            "returned": 100.0,
            "settled_records": [{"bet_id": "bet-1"}],
        }

        with (
            patch.object(state, "_bind_current_save"),
            patch.object(state, "_timed_memory_lock", return_value=nullcontext()),
            patch.object(state, "_finalize_settlement") as finalize,
            patch.object(state, "_merge_light_results", return_value=({**state.output}, 0)),
            patch("fm_odds_web.read_game_clock", return_value={"date": "2028-06-10"}),
            patch("fm_odds_web.pending_bet_result_search", return_value={"keys": {key}}),
            patch("fm_odds_web.pending_result_fixture_hints", return_value={key: 0x1234}),
            patch("fm_odds_web.read_completed_results", return_value=[]) as read,
            patch("fm_odds_web.settle_pending_bets", return_value=settlement) as settle,
        ):
            response = state.search_pending_bet_result({"bet_id": "bet-1"})

        cached_result = {
            "date": key[0],
            "competition": {"id": key[1]},
            "home_team": {"id": key[2], "name": "Home"},
            "away_team": {"id": key[3], "name": "Away"},
            "home_goals": 2,
            "away_goals": 1,
        }
        read.assert_called_once_with(
            {key}, fixture_hints={key: 0x1234}, deep_recovery=True,
        )
        settle.assert_called_once_with(
            [cached_result], "2028-06-10", game_minutes=0,
            trusted_same_day_keys={key},
            void_missing_after_timeout=True,
        )
        finalize.assert_called_once_with(settlement, state.output)
        self.assertTrue(response["target_settled"])
        self.assertEqual(response["found"], 1)

    def test_state_voids_after_deep_search_finds_no_matching_result(self) -> None:
        state = LocalOddsState.__new__(LocalOddsState)
        state.lock = threading.RLock()
        state.output = {
            "game_date": "2028-06-10",
            "season_results": [{
                "date": "2028-06-01",
                "competition_id": 301,
                "home": {"id": 10},
                "away": {"id": 20},
                "home_goals": 2,
                "away_goals": 1,
            }],
        }
        state.refreshing = False
        state.reconciling = False
        state.save_change_pending = False
        state.data_version = 7
        key = ("2028-06-01", 300, 10, 20)
        settlement = {
            "settled": 1,
            "returned": 100.0,
            "settled_records": [{"bet_id": "bet-1"}],
        }

        with (
            patch.object(state, "_bind_current_save"),
            patch.object(state, "_timed_memory_lock", return_value=nullcontext()),
            patch.object(state, "_finalize_settlement") as finalize,
            patch.object(state, "_merge_light_results", return_value=({**state.output}, 0)),
            patch("fm_odds_web.read_game_clock", return_value={"date": "2028-06-10"}),
            patch("fm_odds_web.pending_bet_result_search", return_value={"keys": {key}}),
            patch("fm_odds_web.pending_result_fixture_hints", return_value={}),
            patch("fm_odds_web.read_completed_results", return_value=[]),
            patch("fm_odds_web.settle_pending_bets", return_value=settlement) as settle,
        ):
            response = state.search_pending_bet_result({"bet_id": "bet-1"})

        settle.assert_called_once_with(
            [], "2028-06-10", game_minutes=0,
            trusted_same_day_keys=set(),
            void_missing_after_timeout=True,
        )
        finalize.assert_called_once_with(settlement, state.output)
        self.assertEqual(response["found"], 0)
        self.assertEqual(response["remaining"], 0)
        self.assertTrue(response["target_settled"])

    def test_search_explains_when_fm_only_provides_full_time_score(self) -> None:
        state = LocalOddsState.__new__(LocalOddsState)
        state.lock = threading.RLock()
        state.output = {"game_date": "2028-06-10", "season_results": []}
        state.data_version = 1
        state.refreshing = False
        state.reconciling = False
        state.save_change_pending = False
        key = ("2028-06-01", 300, 10, 20)
        leg = {
            "fixture_date": key[0], "competition_id": key[1],
            "home_id": key[2], "away_id": key[3],
            "home": "圭亚那", "away": "圣皮埃尔和密克隆",
            "market": "HT_AH", "selection": "半场 圭亚那 -2",
        }
        result = {
            "date": key[0], "competition": {"id": key[1]},
            "home_team": {"id": key[2], "name": leg["home"]},
            "away_team": {"id": key[3], "name": leg["away"]},
            "home_goals": 4, "away_goals": 0,
            "result_source": "persistent_fixture_pointer",
        }
        settlement = {"settled": 0, "returned": 0.0, "settled_records": []}

        with (
            patch.object(state, "_bind_current_save"),
            patch.object(state, "_timed_memory_lock", return_value=nullcontext()),
            patch.object(state, "_finalize_settlement"),
            patch.object(state, "_merge_light_results", return_value=({**state.output}, 0)),
            patch("fm_odds_web.read_game_clock", return_value={"date": "2028-06-10"}),
            patch("fm_odds_web.pending_bet_result_search", return_value={
                "keys": {key}, "legs": [{"key": key, "leg": leg}],
            }),
            patch("fm_odds_web.pending_result_fixture_hints", return_value={key: 0x1234}),
            patch("fm_odds_web.read_completed_results", return_value=[result]),
            patch("fm_odds_web.settle_pending_bets", return_value=settlement),
        ):
            response = state.search_pending_bet_result({"bet_id": "bet-1"})

        self.assertEqual(response["missing_details"][0]["missing_fields"], ["半场比分"])
        self.assertEqual(response["missing_details"][0]["score"], "4-0")
        self.assertIn("比赛只有全场比分，没有半场比分", response["message"])
        self.assertIn("可选择手动退款", response["message"])

    def test_frontend_replaces_overdue_pending_status_with_search_button(self) -> None:
        script = legacy_web_source("web/app.js")
        stylesheet = open("web/app.css", encoding="utf-8").read()
        server = open("fm_odds_web.py", encoding="utf-8").read()

        self.assertIn("elapsedDays >= waitDays;", script)
        self.assertIn('"result_search_wait_days": RESULT_SEARCH_WAIT_DAYS', server)
        self.assertIn('data-search-bet-result=', script)
        self.assertIn('request("/api/bets/search-result"', script)
        self.assertIn("app.resultSearchRefundIds.add(betId)", script)
        self.assertIn("app.resultSearchIssues.set(betId, result.missing_details || [])", script)
        self.assertIn('"history.issue_full_only"', script)
        self.assertIn('data-manual-refund-bet=', script)
        self.assertIn("openManualRefundDialog(refundButton.dataset.manualRefundBet)", script)
        self.assertIn(
            "if (target && target.available !== false) "
            "app.manualRefundSelection.add(String(target.bet_id));",
            script,
        )
        self.assertNotIn("if (target?.available !== false)", script)
        self.assertIn(".pending-result-search", stylesheet)
        self.assertIn(".pending-result-search.manual-refund", stylesheet)
        self.assertIn("font-size:13px", stylesheet)


if __name__ == "__main__":
    unittest.main()
