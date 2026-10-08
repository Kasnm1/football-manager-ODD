from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class MoneyAbbreviationsFrontendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")

    def test_compact_money_supports_every_unit_from_k_through_q(self):
        units = self.script.split("const MONEY_ABBREVIATION_UNITS", 1)[1].split(
            "const MONEY_ABBREVIATION_MULTIPLIERS", 1
        )[0]
        for exponent, suffix in (
            (30, "Q"), (27, "R"), (24, "Y"), (21, "Z"), (18, "E"),
            (15, "P"), (12, "T"), (9, "B"), (6, "M"), (3, "K"),
        ):
            self.assertIn(f'[1e{exponent}, "{suffix}"]', units)
        self.assertNotIn('[1e9, "G"]', units)
        for legacy_suffix in ('"Qa"', '"Qi"', '"Sx"', '"Sp"', '"Oc"'):
            self.assertNotIn(legacy_suffix, units)

    def test_compact_money_units_keep_one_decimal_place(self):
        self.assertIn(
            'compact.toLocaleString(activeUiLocale(), {minimumFractionDigits:1, maximumFractionDigits:1})',
            self.script,
        )
        self.assertIn('const compact = (number, suffix) => `${number.toFixed(1)}${suffix}`;', self.script)

    def test_betting_limit_input_accepts_every_supported_suffix(self):
        self.assertIn("([KMBTPEZYRQ]?)", self.script)
        self.assertIn("MONEY_ABBREVIATION_MULTIPLIERS[match[2]]", self.script)
        self.assertIn("if (unit) return compact(unit[0], unit[1]);", self.script)
        self.assertIn('`${(amount / unit).toFixed(1)}${suffix}`', self.script)

    def test_settings_describe_all_supported_units(self):
        settings = open(ROOT / "web" / "i18n.settings.js", encoding="utf-8").read()
        self.assertIn('"settings.currency.compact_note"', settings)
        self.assertIn(
            "Large amounts use K, M, B, T, P, E, Z, Y, R and Q with one decimal place.",
            settings,
        )

    def test_bank_account_split_uses_compact_money_setting(self):
        bank = self.script.split('function renderBank() {', 1)[1].split(
            '\n}\n\nfunction renderInventory', 1,
        )[0]
        self.assertIn('formatPounds(funding.bank)', bank)
        self.assertIn('formatPounds(funding.wallet)', bank)
        self.assertNotIn('formatFullMoney(funding.bank)', bank)
        self.assertNotIn('formatFullMoney(funding.wallet)', bank)


if __name__ == "__main__":
    unittest.main()
