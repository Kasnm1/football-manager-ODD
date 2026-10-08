from __future__ import annotations

import unittest
from unittest.mock import patch

from tools.betting_account import reserve_bets


class BettingAccountMessageTests(unittest.TestCase):
    def test_reserve_bets_rejects_zero_stake_in_chinese(self):
        with (
            patch("tools.betting_account.load_wallet", return_value={"balance": 100.0}),
            patch("tools.betting_account.load_bets", return_value=[]),
        ):
            with self.assertRaisesRegex(ValueError, "下注金额必须大于 0"):
                reserve_bets([], 0)

    def test_reserve_bets_rejects_insufficient_balance_in_chinese(self):
        with (
            patch("tools.betting_account.load_wallet", return_value={"balance": 100.0}),
            patch("tools.betting_account.load_bets", return_value=[]),
        ):
            with self.assertRaisesRegex(ValueError, "可用余额不足"):
                reserve_bets([], 101)


if __name__ == "__main__":
    unittest.main()
