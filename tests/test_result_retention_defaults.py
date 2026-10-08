import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import (
    app_paths, app_settings, preview_cup_odds, storage_management,
    storage_retention,
)


def _result(
    date_text: str = "2028-06-01", *, home_goals: int = 1,
    competition_id: int = 300,
) -> dict:
    return {
        "date": date_text,
        "competition": {"id": competition_id},
        "home_team": {"id": 10},
        "away_team": {"id": 20},
        "home_goals": home_goals,
        "away_goals": 0,
    }


def _write_history(path: Path, results: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"results": results}), encoding="utf-8")


def _read_history(path: Path) -> dict:
    return storage_retention.decode_result_history_bytes(path.read_bytes())


class ResultRetentionDefaultsTests(unittest.TestCase):
    def test_missing_settings_default_to_two_seasons(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings_path = Path(directory) / "settings.json"
            with patch.object(app_settings, "SETTINGS_PATH", settings_path):
                settings = app_settings.load_settings()
        self.assertEqual(settings["result_retention_seasons"], 2)

    def test_legacy_retention_setting_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings_path = Path(directory) / "settings.json"
            settings_path.write_text('{"result_retention_seasons": "never"}', encoding="utf-8")
            with patch.object(app_settings, "SETTINGS_PATH", settings_path):
                settings = app_settings.load_settings()
        self.assertEqual(settings["result_retention_seasons"], 2)

    def test_result_history_uses_durable_career_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(app_paths, "DATA_ROOT", root):
                path = preview_cup_odds.result_history_path("career-one")

        self.assertEqual(
            path, root / "careers" / "career-one" / "result_history.json",
        )
        self.assertNotIn("cache", path.parts)

    def test_legacy_only_result_history_remains_readable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            durable = root / "careers" / "career-one" / "result_history.json"
            legacy = root / "cache" / "old-scope" / "results" / "fm26_result_history.json"
            expected = _result()
            _write_history(legacy, [expected])
            with (
                patch.object(preview_cup_odds, "result_history_path", return_value=durable),
                patch.object(preview_cup_odds, "legacy_result_history_path", return_value=legacy),
            ):
                results = preview_cup_odds.read_result_history("career-one")

        self.assertEqual(results, [expected])

    def test_durable_history_overrides_duplicate_legacy_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            durable = root / "careers" / "career-one" / "result_history.json"
            legacy = root / "cache" / "old-scope" / "results" / "fm26_result_history.json"
            _write_history(legacy, [_result(home_goals=1), _result("2028-05-31", competition_id=301)])
            _write_history(durable, [_result(home_goals=2)])
            with (
                patch.object(preview_cup_odds, "result_history_path", return_value=durable),
                patch.object(preview_cup_odds, "legacy_result_history_path", return_value=legacy),
            ):
                results = preview_cup_odds.read_result_history("career-one")

        self.assertEqual(len(results), 2)
        current = next(item for item in results if item["date"] == "2028-06-01")
        self.assertEqual(current["home_goals"], 2)

    def test_saving_result_history_writes_only_durable_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            durable = root / "careers" / "career-one" / "result_history.json"
            legacy = root / "cache" / "old-scope" / "results" / "fm26_result_history.json"
            legacy_result = _result("2028-05-31", competition_id=301)
            current_result = _result()
            _write_history(legacy, [legacy_result])
            with (
                patch.object(preview_cup_odds, "result_history_path", return_value=durable),
                patch.object(preview_cup_odds, "legacy_result_history_path", return_value=legacy),
            ):
                preview_cup_odds.save_result_history([current_result], "career-one")
                _write_history(legacy, [legacy_result, _result("2028-05-30", competition_id=302)])
                reloaded = preview_cup_odds.read_result_history("career-one")

            payload = _read_history(durable)
            legacy_exists = legacy.exists()

        self.assertTrue(payload["legacy_history_imported"])
        expected = [
            {**legacy_result, "save_instance_id": "career-one"},
            {**current_result, "save_instance_id": "career-one"},
        ]
        self.assertEqual(payload["results"], expected)
        self.assertEqual(reloaded, expected)
        self.assertTrue(legacy_exists)

    def test_cache_clear_cannot_remove_durable_result_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            durable = root / "careers" / "career-one" / "result_history.json"
            disposable = root / "cache" / "scope-one" / "model" / "forecast.json"
            _write_history(durable, [_result()])
            disposable.parent.mkdir(parents=True, exist_ok=True)
            disposable.write_text("cache", encoding="utf-8")
            with (
                patch.object(storage_management, "_validated_data_root", return_value=root),
                patch.object(storage_management, "ensure_data_directories"),
            ):
                storage_management.clear_cache_files()

            self.assertTrue(durable.is_file())
            self.assertFalse(disposable.exists())

    def test_concurrent_result_saves_do_not_overwrite_each_other(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            durable = root / "careers" / "career-one" / "result_history.json"
            legacy = root / "cache" / "old-scope" / "results" / "fm26_result_history.json"
            original_merge = preview_cup_odds.merge_result_history

            def delayed_merge(results: list[dict], save_id: str | None) -> list[dict]:
                merged = original_merge(results, save_id)
                time.sleep(0.05)
                return merged

            start = threading.Barrier(3)
            errors: list[Exception] = []

            def save_one(result: dict) -> None:
                try:
                    start.wait()
                    preview_cup_odds.save_result_history([result], "career-one")
                except Exception as error:  # pragma: no cover - asserted below
                    errors.append(error)

            with (
                patch.object(preview_cup_odds, "result_history_path", return_value=durable),
                patch.object(preview_cup_odds, "legacy_result_history_path", return_value=legacy),
                patch.object(preview_cup_odds, "merge_result_history", side_effect=delayed_merge),
            ):
                threads = [
                    threading.Thread(target=save_one, args=(_result(competition_id=competition_id),))
                    for competition_id in (300, 301)
                ]
                for thread in threads:
                    thread.start()
                start.wait()
                for thread in threads:
                    thread.join()

            payload = _read_history(durable)

        self.assertEqual(errors, [])
        self.assertEqual(
            [item["competition"]["id"] for item in payload["results"]],
            [300, 301],
        )

    def test_retention_prunes_the_durable_result_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            durable_root = Path(directory) / "careers" / "career-one"
            ledger = durable_root / "result_history.json"
            _write_history(ledger, [_result("2026-01-01"), _result("2028-06-01")])
            with patch.object(
                storage_retention, "career_data_root", return_value=durable_root,
            ):
                removed, updated = storage_retention.prune_result_history(
                    "2028-06-30", "career-one",
                )
            payload = _read_history(ledger)
            compressed = ledger.read_bytes().startswith(b"\x1f\x8b")

        self.assertTrue(compressed)
        self.assertEqual((removed, updated), (1, 1))
        self.assertEqual([item["date"] for item in payload["results"]], ["2028-06-01"])

    def test_storage_budget_compresses_ledgers_and_removes_disposable_cache(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger = root / "careers" / "career-one" / "result_history.json"
            backup = ledger.with_suffix(ledger.suffix + ".backup")
            results = [
                _result(f"2028-06-{day:02d}", competition_id=300 + day)
                for day in range(1, 21)
            ]
            _write_history(ledger, results)
            _write_history(backup, results)
            cache = root / "cache" / "scope" / "odds" / "live"
            cache.mkdir(parents=True)
            protected = cache / "current.json"
            disposable = cache / "old.json"
            protected.write_bytes(b"p" * 1024)
            disposable.write_bytes(b"d" * 4096)

            report = storage_retention.enforce_storage_budget(
                root=root, protected_paths={protected},
                budget_bytes=4096, target_bytes=3072,
            )

            self.assertTrue(ledger.read_bytes().startswith(b"\x1f\x8b"))
            self.assertEqual(_read_history(ledger)["results"], results)
            self.assertTrue(protected.is_file())
            self.assertFalse(disposable.exists())
            self.assertFalse(report["over_budget"])


if __name__ == "__main__":
    unittest.main()
