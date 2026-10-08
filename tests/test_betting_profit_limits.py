from __future__ import annotations

import unittest
from unittest.mock import patch

from fm_odds_web import assert_bet_profit_limit, maximum_stake_for_profit
from tools.betting_account import reserve_bets


def single_bet(
    bet_id: str, stake: float, odds: float, *, status: str = "pending",
    fixture_date: str = "2030-01-02", home_id: int = 10, away_id: int = 20,
) -> dict[str, object]:
    return {
        "bet_id": bet_id,
        "type": "single",
        "status": status,
        "fixture_date": fixture_date,
        "competition_id": 100,
        "home_id": home_id,
        "away_id": away_id,
        "stake": stake,
        "odds": odds,
    }


class BettingProfitLimitTests(unittest.TestCase):
    def wallet(self) -> dict[str, object]:
        return {
            "balance": 10_000.0,
            "credit_effective_bet_count": 0,
            "transactions": [],
        }

    def test_repeated_pending_bets_cannot_exceed_match_limit(self) -> None:
        existing = [single_bet("existing", 60, 2.0)]
        incoming = [single_bet("incoming", 50, 2.0)]
        wallet = self.wallet()

        with (
            patch("tools.betting_account.load_wallet", return_value=wallet),
            patch("tools.betting_account.load_bets", return_value=existing),
            patch("tools.betting_account._commit_account") as commit,
        ):
            with self.assertRaisesRegex(ValueError, "累计可赢额不能超过"):
                reserve_bets(incoming, 50, single_profit_limit=100)

        self.assertEqual(wallet["balance"], 10_000.0)
        commit.assert_not_called()

    def test_multiple_new_bets_for_same_match_are_aggregated(self) -> None:
        incoming = [
            single_bet("first", 60, 2.0),
            single_bet("second", 50, 2.0),
        ]

        with (
            patch("tools.betting_account.load_wallet", return_value=self.wallet()),
            patch("tools.betting_account.load_bets", return_value=[]),
            patch("tools.betting_account._commit_account") as commit,
        ):
            with self.assertRaisesRegex(ValueError, "累计可赢额不能超过"):
                reserve_bets(incoming, 110, single_profit_limit=100)

        commit.assert_not_called()

    def test_settled_and_other_match_bets_do_not_consume_limit(self) -> None:
        existing = [
            single_bet("settled", 100, 2.0, status="won"),
            single_bet("other", 100, 2.0, home_id=30, away_id=40),
        ]
        incoming = [single_bet("incoming", 100, 2.0)]
        wallet = self.wallet()

        with (
            patch("tools.betting_account.load_wallet", return_value=wallet),
            patch("tools.betting_account.load_bets", return_value=existing),
            patch("tools.betting_account._commit_account") as commit,
        ):
            result = reserve_bets(incoming, 100, single_profit_limit=100)

        self.assertEqual(result["balance"], 9_900.0)
        commit.assert_called_once()

    def test_retried_submission_returns_existing_bets_without_charging_again(self) -> None:
        existing = [
            {**single_bet("existing", 100, 2.0), "client_submission_id": "request-1"},
        ]
        incoming = [
            {**single_bet("replacement", 100, 2.0), "client_submission_id": "request-1"},
        ]
        wallet = self.wallet()

        with (
            patch("tools.betting_account.load_wallet", return_value=wallet),
            patch("tools.betting_account.load_bets", return_value=existing),
            patch("tools.betting_account._commit_account") as commit,
        ):
            result = reserve_bets(incoming, 100, single_profit_limit=100)

        self.assertTrue(result["already_reserved"])
        self.assertEqual(result["reserved_bets"], existing)
        self.assertEqual(result["balance"], 10_000.0)
        commit.assert_not_called()

    def test_large_maximum_stake_stays_below_profit_limit(self) -> None:
        maximum_profit = 4_452_476_087.17
        stake = maximum_stake_for_profit(1.30, maximum_profit)

        assert_bet_profit_limit("single", stake, 1.30, maximum_profit)
        with self.assertRaisesRegex(ValueError, "最大可赢额"):
            assert_bet_profit_limit("single", stake + 0.01, 1.30, maximum_profit)


if __name__ == "__main__":
    unittest.main()
