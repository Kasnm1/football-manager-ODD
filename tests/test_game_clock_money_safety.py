from __future__ import annotations

import unittest
import json
import tempfile
from datetime import date
from pathlib import Path
from unittest.mock import patch

from tools import club_economy, preview_cup_odds
from tools.betting_account import load_wallet


class GameClockMoneySafetyTests(unittest.TestCase):
    def test_clock_reader_rejects_the_1900_sentinel_date(self) -> None:
        with (
            patch.object(preview_cup_odds, "read_layout_game_date_code", return_value=0),
            patch.object(preview_cup_odds, "decode_date", return_value=date(1900, 1, 1)),
        ):
            with self.assertRaisesRegex(RuntimeError, "game date unavailable"):
                preview_cup_odds.read_game_clock_from_reader(object())

    def test_1900_sentinel_cannot_trigger_a_funds_clawback(self) -> None:
        with patch.object(
            club_economy, "load_economy",
            side_effect=AssertionError("invalid clock must not load account funds"),
        ):
            withdrawals = club_economy.rewound_transfer_budget_withdrawals(
                5103750, ("2026-07-03", 900), ("1900-01-01", 0),
            )

        self.assertEqual(withdrawals, [])

    def test_real_backward_clock_still_finds_crossed_withdrawals(self) -> None:
        withdrawal = {
            "id": "withdrawal-1",
            "team_id": 5103750,
            "game_date": "2026-07-03",
            "game_minutes": 900,
        }
        with patch.object(
            club_economy, "load_economy",
            return_value={"transfer_budget_withdrawals": [withdrawal]},
        ):
            withdrawals = club_economy.rewound_transfer_budget_withdrawals(
                5103750, ("2026-07-03", 901), ("2026-07-03", 899),
            )

        self.assertEqual(withdrawals, [withdrawal])

    def test_invalid_clock_clawback_is_refunded_once(self) -> None:
        rollback_id = "rollback-1900"
        withdrawal_id = "withdrawal-1"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            economy_path = root / "economy" / "club_economy.json"
            wallet_path = root / "bets" / "fm26_wallet.json"
            economy_path.parent.mkdir(parents=True)
            wallet_path.parent.mkdir(parents=True)
            economy = {
                **club_economy._new_state(),
                "wallet_mode": "bank_v2",
                "transfer_budget_withdrawals": [{
                    "id": withdrawal_id,
                    "team_id": 5103750,
                    "amount": 250.0,
                    "game_date": "2026-07-03",
                    "game_minutes": 900,
                    "clawed_back_at": "2026-07-26T05:17:56",
                    "clawed_back_amount": 250.0,
                }],
                "transfer_budget_rollbacks": [{
                    "id": rollback_id,
                    "withdrawal_ids": [withdrawal_id],
                    "amount": 250.0,
                    "bank_deducted": 50.0,
                    "wallet_deducted": 200.0,
                    "unrecovered": 0.0,
                    "previous_game_date": "2026-07-03",
                    "previous_game_minutes": 900,
                    "current_game_date": "1900-01-01",
                    "current_game_minutes": 0,
                }],
            }
            wallet = {
                "schema_version": 1,
                "balance": 0.0,
                "credit_effective_bet_count": 0,
                "transactions": [],
            }
            economy_path.write_text(json.dumps(economy), encoding="utf-8")
            wallet_path.write_text(json.dumps(wallet), encoding="utf-8")

            with (
                patch("tools.club_economy.save_data_root", return_value=root),
                patch("tools.betting_account.save_data_root", return_value=root),
            ):
                first = club_economy.load_economy()
                second = club_economy.load_economy()
                wallet_after = load_wallet()

        self.assertEqual(first["general_balance"], 50.0)
        self.assertEqual(second["general_balance"], 50.0)
        self.assertEqual(wallet_after["balance"], 200.0)
        self.assertEqual(
            sum(
                row.get("type") == "invalid_clock_rollback_refund"
                for row in wallet_after["transactions"]
            ),
            1,
        )
        withdrawal = second["transfer_budget_withdrawals"][0]
        self.assertNotIn("clawed_back_at", withdrawal)


if __name__ == "__main__":
    unittest.main()
