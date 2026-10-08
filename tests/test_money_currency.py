from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import app_settings


class MoneyCurrencyTests(unittest.TestCase):
    def test_local_purchase_prices_use_currency_face_value_scale(self):
        base_price = 20_000
        expected_display = {
            "GBP": 20_000,
            "USD": 20_000,
            "EUR": 20_000,
            "CNY": 200_000,
            "RUB": 2_000_000,
            "JPY": 4_000_000,
            "KRW": 40_000_000,
            "CAD": 20_000,
            "AUD": 20_000,
            "CHF": 20_000,
        }
        for currency, expected in expected_display.items():
            with self.subTest(currency=currency):
                settings = app_settings._currency_payload(currency)
                internal = app_settings.local_purchase_price(base_price, settings)
                self.assertAlmostEqual(
                    internal * settings["money_rate"], expected, delta=0.05,
                )

    def test_currency_default_betting_limits(self):
        expected = {
            "GBP": (20_000_000, 100_000_000),
            "USD": (20_000_000, 100_000_000),
            "EUR": (20_000_000, 100_000_000),
            "CNY": (200_000_000, 1_000_000_000),
            "RUB": (2_000_000_000, 10_000_000_000),
            "JPY": (4_000_000_000, 20_000_000_000),
            "KRW": (40_000_000_000, 200_000_000_000),
            "CAD": (20_000_000, 100_000_000),
            "AUD": (20_000_000, 100_000_000),
            "CHF": (20_000_000, 100_000_000),
        }
        for currency, (single_display, parlay_display) in expected.items():
            with self.subTest(currency=currency):
                settings = app_settings._currency_payload(currency)
                self.assertAlmostEqual(
                    settings["default_betting_limit_single"] * settings["money_rate"],
                    single_display,
                )
                self.assertAlmostEqual(
                    settings["default_betting_limit_parlay"] * settings["money_rate"],
                    parlay_display,
                )

    def test_money_currency_is_persisted(self):
        with tempfile.TemporaryDirectory() as directory:
            settings_path = Path(directory) / "settings.json"
            with patch.object(app_settings, "SETTINGS_PATH", settings_path):
                saved = app_settings.set_money_currency("CNY")
                loaded = app_settings.load_settings()

        self.assertEqual(saved["money_currency"], "CNY")
        self.assertAlmostEqual(saved["money_rate"], 7_000_000_000 / 721_142_843)
        self.assertEqual(loaded["money_currency"], "CNY")
        self.assertAlmostEqual(loaded["money_rate"], 7_000_000_000 / 721_142_843)

    def test_usd_rate_uses_integer_fm_sample(self):
        settings = app_settings._currency_payload("USD")

        self.assertAlmostEqual(settings["money_rate"], 420_000_000 / 310_921_197)

    def test_zero_decimal_currencies_preserve_internal_minor_units(self):
        for currency in ("JPY", "KRW"):
            with self.subTest(currency=currency):
                settings = app_settings._currency_payload(currency)
                self.assertEqual(settings["money_decimal_digits"], 0)
                self.assertGreater(settings["money_rate"], 1)

        self.assertEqual(app_settings._currency_payload("RUB")["money_decimal_digits"], 2)

    def test_native_currency_symbols_do_not_need_disambiguation_prefixes(self):
        self.assertEqual(app_settings._currency_payload("JPY")["money_symbol"], "¥")
        self.assertEqual(app_settings._currency_payload("CAD")["money_symbol"], "$")
        self.assertEqual(app_settings._currency_payload("AUD")["money_symbol"], "$")

    def test_extended_currency_choices_are_persisted(self):
        with tempfile.TemporaryDirectory() as directory:
            settings_path = Path(directory) / "settings.json"
            with patch.object(app_settings, "SETTINGS_PATH", settings_path):
                for currency in ("RUB", "JPY", "KRW", "CAD", "AUD", "CHF"):
                    with self.subTest(currency=currency):
                        app_settings.set_money_currency(currency)
                        self.assertEqual(app_settings.load_settings()["money_currency"], currency)

    def test_frontend_exposes_extended_choices_and_currency_precision(self):
        source = (Path(__file__).resolve().parents[1] / "web" / "app.js").read_text(
            encoding="utf-8",
        )
        self.assertIn(
            '["gbp", "usd", "eur", "cny", "rub", "jpy", "krw", "cad", "aud", "chf"]',
            source,
        )
        self.assertIn("const moneyDecimalDigits", source)
        self.assertIn("minimumFractionDigits:digits, maximumFractionDigits:digits", source)


if __name__ == "__main__":
    unittest.main()
