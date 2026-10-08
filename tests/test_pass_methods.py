from __future__ import annotations

import unittest
from pathlib import Path

from tools.pass_methods import MAX_PARLAY_COMBINATIONS, pass_leg_sizes, pass_subsets


class PassMethodTests(unittest.TestCase):
    def test_parlay_combination_limit_is_five_thousand_in_backend_and_frontend(self):
        self.assertEqual(MAX_PARLAY_COMBINATIONS, 5000)

        script = ((Path(__file__).parents[1] / "src") / "web" / "app.js").read_text(encoding="utf-8")
        self.assertIn("const MAX_PARLAY_COMBINATIONS = 5000;", script)
        self.assertIn("systemSummary.combinations > MAX_PARLAY_COMBINATIONS", script)

    def test_nine_and_ten_match_parlays_only_offer_straight_accumulators(self):
        self.assertEqual(pass_leg_sizes(9, "9X1"), ("9X1", (9,)))
        self.assertEqual(pass_leg_sizes(10, "10X1"), ("10X1", (10,)))

        for match_count, pass_code in ((9, "9X2"), (10, "10X2")):
            with self.subTest(match_count=match_count, pass_code=pass_code):
                with self.assertRaises(ValueError):
                    pass_leg_sizes(match_count, pass_code)

    def test_ten_match_straight_accumulator_has_one_match_subset(self):
        self.assertEqual(pass_subsets(10, (10,)), [tuple(range(10))])


if __name__ == "__main__":
    unittest.main()
