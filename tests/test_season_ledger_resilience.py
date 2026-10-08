from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.season_ledger import archive_season_output, load_season_forecasts


class SeasonLedgerResilienceTests(unittest.TestCase):
    def test_core_market_is_archived_without_extended_catalog(self) -> None:
        output = {
            "season_start": "2025-07-01",
            "season_end": "2026-06-30",
            "generated_at": "2026-05-11T09:00:00",
            "game_date": "2026-05-11",
            "model_version": "test-model",
            "matches": [{
                "fixture_date": "2026-05-12",
                "kickoff_minutes": 720,
                "competition_id": 1,
                "competition_name": "Test League",
                "home": {"id": 10, "name": "Home", "profile": {}},
                "away": {"id": 20, "name": "Away", "profile": {}},
                "xg": {"home": 1.2, "away": 0.8},
                "fair_1x2": {"home": 2.0, "draw": 3.0, "away": 4.0},
                "market_catalog_state": "core",
            }],
            "season_results": [],
        }
        with tempfile.TemporaryDirectory() as directory:
            with patch("tools.season_ledger.cache_data_root", return_value=Path(directory)):
                path = archive_season_output(output)
            payload = json.loads(path.read_text(encoding="utf-8"))
        record = payload["fixtures"]["2026-05-12:1:10:20"]
        forecast = record["latest_forecast"]
        self.assertEqual(forecast["market_catalog_state"], "core")
        self.assertEqual(forecast["xg"], {"home": 1.2, "away": 0.8})
        self.assertEqual(forecast["kickoff_minutes"], 720)
        self.assertEqual(forecast["kickoff_time"], "12:00")
        self.assertNotIn("asian_handicap", forecast)

    def test_published_forecast_can_be_reloaded_for_a_completed_fixture(self) -> None:
        output = {
            "account_scope_id": "account-test",
            "season_start": "2025-07-01",
            "season_end": "2026-06-30",
            "generated_at": "2025-10-18T09:00:00",
            "game_date": "2025-10-18",
            "model_version": "test-model",
            "matches": [{
                "fixture_date": "2025-10-18",
                "kickoff_minutes": 1215,
                "kickoff_time": "20:15",
                "competition_id": 1,
                "competition_name": "Test League",
                "home": {"id": 10, "name": "Home", "profile": {}},
                "away": {"id": 20, "name": "Away", "profile": {}},
                "xg": {"home": 1.7, "away": 0.9},
                "fair_1x2": {"home": 1.8, "draw": 3.4, "away": 4.8},
                "casino_1x2": {"home": 1.7, "draw": 3.2, "away": 4.5},
                "market_catalog_state": "core",
            }],
            "season_results": [],
        }
        fixture_key = "2025-10-18:1:10:20"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("tools.season_ledger.cache_data_root", return_value=root):
                archive_season_output(output)
                forecasts = load_season_forecasts(output, {fixture_key})

        self.assertEqual(set(forecasts), {fixture_key})
        self.assertEqual(
            forecasts[fixture_key]["casino_1x2"],
            {"home": 1.7, "draw": 3.2, "away": 4.5},
        )
        self.assertEqual(forecasts[fixture_key]["kickoff_minutes"], 1215)
        self.assertEqual(forecasts[fixture_key]["kickoff_time"], "20:15")

    def test_loader_recovers_kickoff_metadata_from_record_level(self) -> None:
        output = {
            "account_scope_id": "account-test",
            "season_start": "2025-07-01",
            "season_end": "2026-06-30",
        }
        fixture_key = "2025-10-18:1:10:20"
        payload = {
            "season_start": output["season_start"],
            "season_end": output["season_end"],
            "fixtures": {
                fixture_key: {
                    "kickoff_minutes": 900,
                    "kickoff_time": "15:00",
                    "latest_forecast": {
                        "xg": {"home": 1.7, "away": 0.9},
                        "fair_1x2": {"home": 1.8, "draw": 3.4, "away": 4.8},
                    },
                },
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "model" / "seasons" / "2025-2026" / "forecast_ledger.json"
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps(payload), encoding="utf-8")
            with patch("tools.season_ledger.cache_data_root", return_value=root):
                forecasts = load_season_forecasts(output, {fixture_key})

        self.assertEqual(forecasts[fixture_key]["kickoff_minutes"], 900)
        self.assertEqual(forecasts[fixture_key]["kickoff_time"], "15:00")


if __name__ == "__main__":
    unittest.main()
