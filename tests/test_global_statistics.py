from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import tools.global_statistics as statistics


ROOT = Path(__file__).resolve().parents[1]


class GlobalStatisticsTests(unittest.TestCase):
    def setUp(self) -> None:
        statistics._LAST_USAGE_HEARTBEAT = None
        statistics._LAST_USAGE_SCOPE_ID = None

    def tearDown(self) -> None:
        statistics._LAST_USAGE_HEARTBEAT = None
        statistics._LAST_USAGE_SCOPE_ID = None

    def test_visible_heartbeats_count_usage_but_long_gaps_do_not(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "global_statistics.json"
            with (
                patch.object(statistics, "GLOBAL_STATISTICS_PATH", path),
                patch.object(statistics, "monotonic", side_effect=[10.0, 40.0, 100.0]),
                patch.object(statistics, "_now", side_effect=[
                    datetime(2026, 8, 18, 10, 0, 0),
                    datetime(2026, 8, 18, 10, 0, 30),
                    datetime(2026, 8, 19, 10, 0, 0),
                ]),
            ):
                statistics.record_usage_heartbeat(True)
                second = statistics.record_usage_heartbeat(True)
                third = statistics.record_usage_heartbeat(True)
                inactive = statistics.record_usage_heartbeat(False)

        self.assertEqual(second["usage_seconds"], 30)
        self.assertEqual(third["usage_seconds"], 30)
        self.assertEqual(third["active_days"], 2)
        self.assertEqual(inactive["usage_seconds"], 30)
        self.assertIsNone(statistics._LAST_USAGE_HEARTBEAT)

    def test_usage_is_accumulated_for_the_continuously_active_scope(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "global_statistics.json"
            with (
                patch.object(statistics, "GLOBAL_STATISTICS_PATH", path),
                patch.object(statistics, "monotonic", side_effect=[10.0, 40.0, 60.0]),
                patch.object(statistics, "_now", side_effect=[
                    datetime(2026, 8, 18, 10, 0, 0),
                    datetime(2026, 8, 18, 10, 0, 30),
                    datetime(2026, 8, 18, 10, 0, 50),
                ]),
            ):
                statistics.record_usage_heartbeat(True, "save-one")
                statistics.record_usage_heartbeat(True, "save-one")
                statistics.record_usage_heartbeat(True, "save-two")
                first = statistics.usage_statistics("save-one")
                second = statistics.usage_statistics("save-two")

        self.assertEqual(first["usage_seconds"], 30)
        self.assertEqual(first["active_days"], 1)
        self.assertEqual(second["usage_seconds"], 0)
        self.assertEqual(second["active_days"], 1)

    def test_cross_save_projection_orders_settlements_and_ignores_pushes_in_streaks(self) -> None:
        rows = {
            "save-one": [
                {"bet_id":"a", "settled_at":"2026-01-01T10:00:00", "stake_minor":10000, "payout_minor":15000},
                {"bet_id":"c", "settled_at":"2026-01-01T12:00:00", "stake_minor":10000, "payout_minor":10000},
            ],
            "save-two": [
                {"bet_id":"b", "settled_at":"2026-01-01T11:00:00", "stake_minor":10000, "payout_minor":12000},
                {"bet_id":"d", "settled_at":"2026-01-01T13:00:00", "stake_minor":10000, "payout_minor":6000},
            ],
        }
        usage = {"usage_seconds":3600, "active_days":2, "first_used_at":"", "last_used_at":""}
        with (
            patch.object(statistics, "saved_account_scopes", return_value=[
                {"scope_id":"save-one"}, {"scope_id":"save-two"},
            ]),
            patch.object(statistics, "_scope_analytics", side_effect=lambda scope: rows[scope]),
            patch.object(statistics, "usage_statistics", return_value=usage),
        ):
            result = statistics.global_statistics()

        self.assertEqual(result["save_count"], 2)
        self.assertEqual(result["betting_save_count"], 2)
        self.assertEqual(result["settled_bets"], 4)
        self.assertEqual(result["net_profit"], 30.0)
        self.assertEqual(result["roi"], 7.5)
        self.assertEqual(result["maximum_profit"], 50.0)
        self.assertEqual(result["maximum_loss"], -40.0)
        self.assertEqual(result["longest_win_streak"], 2)
        self.assertEqual(result["longest_loss_streak"], 1)
        self.assertEqual(result["current_streak"], -1)
        self.assertEqual(result["pushes"], 1)

    def test_current_save_projection_reads_only_the_requested_scope(self) -> None:
        usage = {"usage_seconds":120, "active_days":1, "first_used_at":"", "last_used_at":""}
        with (
            patch.object(statistics, "_scope_analytics", return_value=[
                {"bet_id":"a", "settled_at":"2026-01-01T10:00:00", "stake_minor":10000, "payout_minor":12500},
            ]) as analytics,
            patch.object(statistics, "usage_statistics", return_value=usage) as usage_reader,
        ):
            result = statistics.save_statistics("save-one")

        analytics.assert_called_once_with("save-one")
        usage_reader.assert_called_once_with("save-one")
        self.assertEqual(result["scope"], "current")
        self.assertTrue(result["available"])
        self.assertEqual(result["save_count"], 1)
        self.assertEqual(result["settled_bets"], 1)
        self.assertEqual(result["net_profit"], 25.0)

    def test_home_is_the_only_visible_entry_and_only_exposes_usage_time(self) -> None:
        html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
        script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
        home = html.split('id="page-home"', 1)[1].split('id="page-betting"', 1)[0]

        self.assertEqual(html.count('id="home-global-career"'), 1)
        self.assertIn('id="home-global-usage"', home)
        self.assertIn("累计使用时间", home)
        self.assertNotIn("最长连红", home)
        self.assertNotIn("最大单笔赢额", home)
        self.assertIn('request("/api/global-statistics")', script)
        self.assertIn('request("/api/global-statistics?scope=current")', script)
        self.assertIn('request("/api/global-statistics/heartbeat"', script)
        self.assertIn('data-global-career-scope="global"', html)
        self.assertIn('data-global-career-scope="current"', html)
        self.assertNotIn("统计覆盖本机已登记的全部存档", script)

    def test_global_statistics_always_use_extended_compact_money_units(self) -> None:
        script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
        units = script.split("const MONEY_ABBREVIATION_UNITS", 1)[1].split(
            "const MONEY_ABBREVIATION_MULTIPLIERS", 1
        )[0]
        formatter = script.split("const formatGlobalStatisticMoney", 1)[1].split(
            "const formatMoney", 1
        )[0]

        for suffix in ('"K"', '"M"', '"B"', '"T"', '"P"', '"E"', '"Z"', '"Y"', '"R"', '"Q"'):
            self.assertIn(suffix, units)
        self.assertIn("moneyAbbreviationUnit(absolute)", formatter)
        self.assertNotIn("app.compactMoney", formatter)
        self.assertIn("formatGlobalStatisticMoney(Number(value || 0))", script)

    def test_global_statistics_routes_are_registered(self) -> None:
        import fm_odds_web

        self.assertIsNotNone(fm_odds_web.API_ROUTES.resolve("GET", "/api/global-statistics"))
        self.assertIsNotNone(fm_odds_web.API_ROUTES.resolve("POST", "/api/global-statistics/heartbeat"))


if __name__ == "__main__":
    unittest.main()
