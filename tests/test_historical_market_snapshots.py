from __future__ import annotations

import unittest
from unittest.mock import patch

from fm_odds_web import attach_historical_market_snapshots


def _match() -> dict:
    return {
        "fixture_date": "2030-01-02",
        "kickoff_minutes": 900,
        "kickoff_time": "15:00",
        "competition_id": 1,
        "competition_name": "测试联赛",
        "competition_kind": "league",
        "home": {
            "id": 10,
            "name": "主队",
            "profile": {"candidate_20_ca": 140.0, "private_strength": 99},
        },
        "away": {
            "id": 20,
            "name": "客队",
            "profile": {"candidate_20_ca": 130.0, "private_strength": 88},
        },
        "xg": {"home": 1.8, "away": 1.1},
        "fair_1x2": {"home": 1.9, "draw": 3.5, "away": 4.2},
        "casino_1x2": {"home": 1.78, "draw": 3.25, "away": 3.9},
        "early_casino_1x2": {"home": 1.81, "draw": 3.3, "away": 4.0},
        "market_phase": "live",
        "market_catalog_state": "core",
    }


def _result() -> dict:
    return {
        "date": "2030-01-02",
        "competition_id": 1,
        "home": {"id": 10, "name": "主队"},
        "away": {"id": 20, "name": "客队"},
        "home_goals": 2,
        "away_goals": 1,
    }


def _result_on(day: str, home_id: int, away_id: int) -> dict:
    result = _result()
    result["date"] = day
    result["home"] = {"id": home_id, "name": f"主队{home_id}"}
    result["away"] = {"id": away_id, "name": f"客队{away_id}"}
    return result


def _forecast() -> dict:
    return {
        "kickoff_minutes": 900,
        "kickoff_time": "15:00",
        "xg": {"home": 1.8, "away": 1.1},
        "fair_1x2": {"home": 1.9, "draw": 3.5, "away": 4.2},
        "casino_1x2": {"home": 1.78, "draw": 3.25, "away": 3.9},
        "market_catalog_state": "core",
    }


class HistoricalMarketSnapshotTests(unittest.TestCase):
    def test_completed_result_keeps_published_early_odds_and_full_market(self):
        output = {"matches": [], "season_results": [_result()]}
        attach_historical_market_snapshots(output, {"matches": [_match()]})

        snapshot = output["season_results"][0]["odds_snapshot"]
        self.assertEqual(
            snapshot["casino_1x2"],
            {"home": 1.81, "draw": 3.3, "away": 4.0},
        )
        self.assertEqual(snapshot["market_phase"], "early")
        self.assertEqual(snapshot["market_catalog_state"], "full")
        self.assertIn("half_time", snapshot)
        self.assertEqual(snapshot["home"]["profile"], {"candidate_20_ca": 140.0})
        self.assertEqual(snapshot["away"]["profile"], {"candidate_20_ca": 130.0})

    def test_existing_snapshot_survives_later_refreshes(self):
        first = {"matches": [], "season_results": [_result()]}
        attach_historical_market_snapshots(first, {"matches": [_match()]})
        previous = {"matches": [], "season_results": first["season_results"]}

        later = {"matches": [], "season_results": [_result()]}
        attach_historical_market_snapshots(later, previous)

        self.assertEqual(
            later["season_results"][0]["odds_snapshot"]["casino_1x2"],
            {"home": 1.81, "draw": 3.3, "away": 4.0},
        )

    def test_existing_snapshot_can_be_enriched_by_native_result_time(self):
        previous_result = _result()
        previous_result["odds_snapshot"] = {
            **_match(),
            "kickoff_minutes": None,
            "kickoff_time": None,
        }
        previous = {"matches": [], "season_results": [previous_result]}
        current_result = {
            **_result(),
            "kickoff_minutes": 1215,
            "kickoff_time": "20:15",
        }
        current = {"matches": [], "season_results": [current_result]}

        attach_historical_market_snapshots(current, previous)

        snapshot = current_result["odds_snapshot"]
        self.assertEqual(snapshot["kickoff_minutes"], 1215)
        self.assertEqual(snapshot["kickoff_time"], "20:15")

    def test_archived_forecast_backfills_result_missing_previous_match(self):
        result = _result()
        output = {"matches": [], "season_results": [result]}
        archived = {
            "2030-01-02:1:10:20": {
                "xg": {"home": 1.8, "away": 1.1},
                "fair_1x2": {"home": 1.9, "draw": 3.5, "away": 4.2},
                "casino_1x2": {"home": 1.78, "draw": 3.25, "away": 3.9},
                "market_catalog_state": "core",
            },
        }

        attach_historical_market_snapshots(output, None, archived)

        snapshot = result["odds_snapshot"]
        self.assertEqual(
            snapshot["casino_1x2"],
            {"home": 1.78, "draw": 3.25, "away": 3.9},
        )
        self.assertEqual(snapshot["market_catalog_state"], "full")
        self.assertEqual(snapshot["market_phase"], "early")

    def test_native_result_time_backfills_archived_forecast_without_time(self):
        result = {
            **_result(),
            "kickoff_minutes": 1215,
            "kickoff_time": "20:15",
        }
        output = {"matches": [], "season_results": [result]}
        archived = {
            "2030-01-02:1:10:20": {
                "xg": {"home": 1.8, "away": 1.1},
                "fair_1x2": {"home": 1.9, "draw": 3.5, "away": 4.2},
                "casino_1x2": {"home": 1.78, "draw": 3.25, "away": 3.9},
                "market_catalog_state": "core",
            },
        }

        attach_historical_market_snapshots(output, None, archived)

        snapshot = result["odds_snapshot"]
        self.assertEqual(snapshot["kickoff_minutes"], 1215)
        self.assertEqual(snapshot["kickoff_time"], "20:15")

    def test_archive_fallback_restores_latest_seven_prior_market_dates(self):
        results = [
            _result_on(f"2030-01-{day:02d}", day * 10, day * 10 + 1)
            for day in range(1, 10)
        ]
        output = {
            "game_date": "2030-01-10",
            "matches": [],
            "season_results": results,
        }
        archived = {
            f"2030-01-{day:02d}:1:{day * 10}:{day * 10 + 1}": _forecast()
            for day in range(1, 9)
        }

        with patch(
            "fm_odds_web.load_season_forecasts", return_value=archived,
        ) as loader:
            attach_historical_market_snapshots(output, None)

        loader.assert_called_once_with(output, {
            f"2030-01-{day:02d}:1:{day * 10}:{day * 10 + 1}"
            for day in range(1, 10)
        })
        self.assertNotIn("odds_snapshot", results[8])
        for result in results[1:8]:
            self.assertEqual(
                result["odds_snapshot"]["casino_1x2"],
                {"home": 1.78, "draw": 3.25, "away": 3.9},
            )
            self.assertEqual(result["odds_snapshot"]["kickoff_minutes"], 900)
            self.assertEqual(result["odds_snapshot"]["kickoff_time"], "15:00")
        self.assertNotIn("odds_snapshot", results[0])


if __name__ == "__main__":
    unittest.main()
