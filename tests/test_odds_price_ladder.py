from __future__ import annotations

import unittest

from tools.preview_cup_odds import (
    bookmaker_weighted_odds,
    decimal_odds,
    quote_decimal_odds,
)


class OddsPriceLadderTests(unittest.TestCase):
    def test_standard_prices_use_realistic_range_increments(self):
        cases = {
            1.873: 1.87,
            2.013: 2.01,
            3.123: 3.12,
            4.073: 4.07,
            7.87: 8.0,
            18.73: 19.0,
            27.6: 28.0,
            43.1: 43.0,
            50.0: 50.0,
            52.4: 50.0,
            52.5: 55.0,
            88.1: 90.0,
            100.0: 100.0,
            104.9: 100.0,
            105.0: 110.0,
            347.0: 350.0,
            22329.2: 22330.0,
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(quote_decimal_odds(raw), expected)

    def test_standard_prices_have_a_minimum_without_clipping_longshots(self):
        self.assertEqual(quote_decimal_odds(1.0), 1.01)
        self.assertEqual(quote_decimal_odds(5000.0), 5000.0)

    def test_asian_prices_use_the_same_global_ladder(self):
        self.assertEqual(quote_decimal_odds(7.873, asian=True), 8.0)
        quoted = bookmaker_weighted_odds((0.1234, 0.0, 0.8766))
        self.assertEqual(quoted, round(quoted, 2))

    def test_probability_conversion_returns_executable_price(self):
        self.assertEqual(decimal_odds(1 / 18.73), 19.0)


if __name__ == "__main__":
    unittest.main()
