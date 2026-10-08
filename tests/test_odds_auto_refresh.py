from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import app_settings


ROOT = Path(__file__).resolve().parents[1]


class OddsAutoRefreshTests(unittest.TestCase):
    def test_default_interval_is_eight_seconds(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            with patch.object(app_settings, "SETTINGS_PATH", path):
                self.assertEqual(app_settings.load_settings()["odds_auto_refresh_seconds"], 8)

    def test_default_odds_window_is_seven_days(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            with patch.object(app_settings, "SETTINGS_PATH", path):
                self.assertEqual(app_settings.load_settings()["odds_days"], 7)

    def test_championship_is_enabled_by_default_and_can_be_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            with patch.object(app_settings, "SETTINGS_PATH", path):
                self.assertTrue(app_settings.load_settings()["championship_enabled"])

                saved = app_settings.set_championship_enabled(False)

                self.assertFalse(saved["championship_enabled"])
                self.assertFalse(app_settings.load_settings()["championship_enabled"])

    def test_god_mode_is_disabled_by_default_and_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            with patch.object(app_settings, "SETTINGS_PATH", path):
                self.assertFalse(app_settings.load_settings()["god_mode"])
                saved = app_settings.set_god_mode(True)
                self.assertTrue(saved["god_mode"])
                self.assertTrue(app_settings.load_settings()["god_mode"])

    def test_supported_intervals_and_manual_mode_are_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            with patch.object(app_settings, "SETTINGS_PATH", path):
                for value in (8, 30, 60, "manual"):
                    settings = app_settings.set_odds_reading("all", 14, value)
                    expected = 0 if value == "manual" else value
                    self.assertEqual(settings["odds_auto_refresh_seconds"], expected)
                    self.assertEqual(
                        app_settings.load_settings()["odds_auto_refresh_seconds"], expected,
                    )

    def test_invalid_interval_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            with patch.object(app_settings, "SETTINGS_PATH", path):
                with self.assertRaisesRegex(ValueError, "仅支持8秒、30秒、1分钟"):
                    app_settings.set_odds_reading("all", 14, 10)

    def test_favorite_schedule_scope_is_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            with patch.object(app_settings, "SETTINGS_PATH", path):
                settings = app_settings.set_odds_reading("favorite_schedule", 14, 8)

                self.assertEqual(settings["odds_scope"], "favorite_schedule")
                self.assertEqual(app_settings.load_settings()["odds_scope"], "favorite_schedule")

    def test_legacy_managed_schedule_scope_migrates_to_favorite_schedule(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            path.write_text('{"odds_scope":"managed_schedule"}', encoding="utf-8")
            with patch.object(app_settings, "SETTINGS_PATH", path):
                self.assertEqual(
                    app_settings.load_settings()["odds_scope"], "favorite_schedule",
                )

    def test_ui_locale_defaults_to_english_and_is_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            with patch.object(app_settings, "SETTINGS_PATH", path):
                self.assertEqual(app_settings.load_settings()["ui_locale"], "en-GB")
                for locale in ("zh-TW", "ko-KR", "de-DE", "es-ES", "fr-FR", "ru-RU", "ja-JP", "pt-BR", "pt-PT"):
                    saved = app_settings.set_ui_locale(locale)
                    self.assertEqual(saved["ui_locale"], locale)
                    self.assertEqual(app_settings.load_settings()["ui_locale"], locale)

    def test_ui_locale_preserves_chinese_for_legacy_settings_without_locale(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            path.write_text('{"odds_days":7}', encoding="utf-8")
            with patch.object(app_settings, "SETTINGS_PATH", path):
                self.assertEqual(app_settings.load_settings()["ui_locale"], "zh-CN")

    def test_ui_locale_rejects_unsupported_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            with patch.object(app_settings, "SETTINGS_PATH", path):
                with self.assertRaisesRegex(ValueError, "en-GB, zh-CN, zh-TW, ko-KR, de-DE, es-ES, fr-FR, ru-RU, ja-JP, pt-BR or pt-PT"):
                    app_settings.set_ui_locale("it-IT")

    def test_frontend_exposes_all_interval_options(self) -> None:
        source = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
        for value in ("8", "30", "60", "0"):
            self.assertIn(f'<option value="{value}"', source)
        self.assertIn("auto_refresh_seconds:autoRefreshSeconds", source)
        self.assertIn("/api/cheat/god-mode", source)
        settings_i18n = (ROOT / "web" / "i18n.settings.js").read_text(encoding="utf-8")
        self.assertIn('"settings.cheats.god_mode.note"', settings_i18n)
        self.assertNotIn("关闭观赛和比赛检测，但这会大幅下降游戏性，谨慎选择。", source)


if __name__ == "__main__":
    unittest.main()
