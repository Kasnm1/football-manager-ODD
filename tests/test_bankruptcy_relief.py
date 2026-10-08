from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from fm_odds_web import LocalOddsState
from tools.betting_account import grant_bankruptcy_relief
from tools.club_economy import _collect_combined_funds


class BankruptcyReliefTests(unittest.TestCase):
    def test_explicit_combined_payment_can_opt_into_bank_overdraft(self):
        economy = {"general_balance": 100.0, "transactions": []}
        with patch("tools.club_economy.available_balance", return_value=50.0):
            payment = _collect_combined_funds(
                economy, 300.0, "explicit_overdraft_payment",
                allow_bank_overdraft=True,
            )

        self.assertEqual(
            {key: payment[key] for key in ("bank", "wallet", "total")},
            {"bank": 250.0, "wallet": 50.0, "total": 300.0},
        )
        self.assertTrue(payment["transaction_id"])
        self.assertEqual(economy["general_balance"], -150.0)
        self.assertEqual(economy["transactions"][-1]["bank_overdraft"], 150.0)

    def test_public_club_economy_keeps_derived_fields_after_balance_reload(self):
        state = LocalOddsState.__new__(LocalOddsState)
        state.club_contexts = {
            679: {"contract": {"active": True, "gross_weekly_display": 1000.0}},
        }
        state.refreshing = False
        state.refresh_mode = None
        state._transfer_budget_status = Mock(
            return_value={"available": True, "amount": 25_000_000},
        )
        state._club_balance_status = Mock(
            return_value={"available": True, "amount": 82_000_000},
        )

        with (
            patch("fm_odds_web.public_economy", return_value={"bank_balance": 5000.0}),
            patch(
                "fm_odds_web.salary_status",
                return_value={"net_weekly": 600.0, "next_payday": "2028-01-02"},
            ),
            patch("fm_odds_web.credit_effective_bet_count", return_value=4),
            patch("fm_odds_web.credit_status", return_value={"available_credit": 1200.0}),
        ):
            economy = state._public_club_economy("2027-12-27")

        self.assertEqual(economy["bank_balance"], 5000.0)
        self.assertEqual(economy["salary"]["net_weekly"], 600.0)
        self.assertEqual(economy["transfer_budget"]["amount"], 25_000_000)
        self.assertEqual(economy["club_balance"]["amount"], 82_000_000)

    def test_public_club_economy_does_not_read_fm_while_memory_is_busy(self):
        state = LocalOddsState.__new__(LocalOddsState)
        state.club_contexts = {}
        state.refreshing = True
        state.refresh_mode = "fast"
        state._transfer_budget_status = Mock()
        state._club_balance_status = Mock()

        with (
            patch("fm_odds_web.public_economy", return_value={"bank_balance": 5000.0}),
            patch("fm_odds_web.salary_status", return_value={}),
        ):
            economy = state._public_club_economy(
                "2027-12-27", read_memory=False,
            )

        self.assertFalse(economy["transfer_budget"]["available"])
        self.assertFalse(economy["club_balance"]["available"])
        state._transfer_budget_status.assert_not_called()
        state._club_balance_status.assert_not_called()

    def test_grants_5000_when_bank_wallet_and_pending_bets_are_empty(self):
        wallet = {"balance": 0.0, "transactions": []}
        with (
            patch("tools.betting_account.load_wallet", return_value=wallet),
            patch("tools.betting_account.load_bets", return_value=[]),
            patch("tools.betting_account.save_wallet") as save_wallet,
        ):
            result = grant_bankruptcy_relief(0)

        self.assertTrue(result["granted"])
        self.assertEqual(result["amount"], 5000.0)
        self.assertEqual(wallet["balance"], 5000.0)
        self.assertEqual(wallet["transactions"][-1]["type"], "bankruptcy_relief")
        save_wallet.assert_called_once_with(wallet)

    def test_does_not_grant_while_a_real_bet_is_pending(self):
        wallet = {"balance": 0.0, "transactions": []}
        with (
            patch("tools.betting_account.load_wallet", return_value=wallet),
            patch(
                "tools.betting_account.load_bets",
                return_value=[{"status": "pending", "stake": 100.0}],
            ),
            patch("tools.betting_account.save_wallet") as save_wallet,
        ):
            result = grant_bankruptcy_relief(0)

        self.assertFalse(result["granted"])
        self.assertEqual(wallet["balance"], 0.0)
        save_wallet.assert_not_called()

    def test_grants_when_both_balances_are_below_one(self):
        wallet = {"balance": 0.37, "transactions": []}
        with (
            patch("tools.betting_account.load_wallet", return_value=wallet),
            patch("tools.betting_account.load_bets", return_value=[]),
            patch("tools.betting_account.save_wallet") as save_wallet,
        ):
            result = grant_bankruptcy_relief(0.82)

        self.assertTrue(result["granted"])
        self.assertEqual(result["amount"], 5000.0)
        self.assertEqual(result["balance"], 5000.37)
        self.assertEqual(wallet["balance"], 5000.37)
        self.assertEqual(wallet["transactions"][-1]["balance_after"], 5000.37)
        save_wallet.assert_called_once_with(wallet)

    def test_negative_bank_balance_remains_eligible_for_relief(self):
        wallet = {"balance": 0.0, "transactions": []}
        with (
            patch("tools.betting_account.load_wallet", return_value=wallet),
            patch("tools.betting_account.load_bets", return_value=[]),
            patch("tools.betting_account.save_wallet"),
        ):
            result = grant_bankruptcy_relief(-350.0)
        self.assertTrue(result["granted"])
        self.assertEqual(result["bank_balance"], -350.0)
        self.assertEqual(wallet["balance"], 5000.0)

    def test_does_not_grant_when_either_balance_reaches_one(self):
        for bank_balance, wallet_balance in ((1.0, 0.0), (0.0, 1.0)):
            with self.subTest(bank=bank_balance, wallet=wallet_balance):
                wallet = {"balance": wallet_balance, "transactions": []}
                with (
                    patch("tools.betting_account.load_wallet", return_value=wallet),
                    patch("tools.betting_account.load_bets", return_value=[]),
                    patch("tools.betting_account.save_wallet") as save_wallet,
                ):
                    result = grant_bankruptcy_relief(bank_balance)

                self.assertFalse(result["granted"])
                save_wallet.assert_not_called()


if __name__ == "__main__":
    unittest.main()
