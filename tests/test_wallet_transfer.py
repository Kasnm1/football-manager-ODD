from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import betting_account, club_economy
from tools.money import to_minor, write_minor


class WalletTransferTests(unittest.TestCase):
    def test_zero_wallet_can_be_recharged_from_a_large_bank_balance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            economy_path = root / "economy" / "club_economy.json"
            wallet_path = root / "bets" / "fm26_wallet.json"
            economy_path.parent.mkdir(parents=True)
            wallet_path.parent.mkdir(parents=True)
            economy = club_economy._new_state()
            write_minor(economy, "general_balance", to_minor(718_694_370.66))
            economy["wallet_mode"] = "bank_v2"
            economy_path.write_text(json.dumps(economy), encoding="utf-8")
            wallet_path.write_text(json.dumps({
                "schema_version": 1,
                "balance": 0.0,
                "credit_effective_bet_count": 0,
                "transactions": [],
            }), encoding="utf-8")

            with (
                patch("tools.club_economy.save_data_root", return_value=root),
                patch("tools.betting_account.save_data_root", return_value=root),
            ):
                result = club_economy.transfer_wallet("recharge", 100_000_000)
                self.assertTrue((root / ".account.fmodd").is_file())
                self.assertFalse((root / "bets" / ".account.fmodd").exists())

        self.assertEqual(result["bank_balance"], 618_694_370.66)
        self.assertEqual(result["casino_balance"], 100_000_000.0)
        self.assertEqual(result["total_balance"], 718_694_370.66)
        self.assertEqual(result["transactions"][0]["amount"], -100_000_000.0)

    def test_failed_cross_document_commit_changes_neither_balance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (
                patch("tools.club_economy.save_data_root", return_value=root),
                patch("tools.betting_account.save_data_root", return_value=root),
            ):
                economy = club_economy.load_economy()
                write_minor(economy, "general_balance", to_minor(500.0))
                club_economy.save_economy(economy)
                wallet_before = betting_account.available_balance()

                with patch(
                    "tools.betting_account.save_documents",
                    side_effect=OSError("simulated write failure"),
                ):
                    with self.assertRaisesRegex(OSError, "simulated write failure"):
                        club_economy.transfer_wallet("recharge", 200.0)

                self.assertEqual(club_economy.public_economy()["bank_balance"], 500.0)
                self.assertEqual(betting_account.available_balance(), wallet_before)

    def test_non_finite_money_is_rejected(self) -> None:
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "有限数值"):
                    club_economy._money(value)
                with self.assertRaisesRegex(ValueError, "有限数值"):
                    betting_account._round_money(value)

    def test_free_purchase_flag_cannot_bypass_account_setting(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (
                patch("tools.club_economy.save_data_root", return_value=root),
                patch("tools.betting_account.save_data_root", return_value=root),
            ):
                wallet = betting_account.load_wallet()
                write_minor(wallet, "balance", 0)
                betting_account.save_wallet(wallet)

                with self.assertRaisesRegex(ValueError, "余额合计不足"):
                    club_economy.buy_item("red_bull", free=True)

                economy = club_economy.load_economy()
                economy["free_services"] = True
                club_economy.save_economy(economy)
                result = club_economy.buy_item("red_bull", free=False)

                self.assertEqual(result["payment"]["total"], 0.0)
                self.assertEqual(len(result["inventory"]), 1)

    def test_combined_payment_refund_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (
                patch("tools.club_economy.save_data_root", return_value=root),
                patch("tools.betting_account.save_data_root", return_value=root),
            ):
                economy = club_economy.load_economy()
                write_minor(economy, "general_balance", to_minor(100.0))
                club_economy.save_economy(economy)
                wallet = betting_account.load_wallet()
                write_minor(wallet, "balance", to_minor(50.0))
                betting_account.save_wallet(wallet)

                payment = club_economy.charge_combined_funds(
                    120.0, "test_purchase",
                )
                first = club_economy.refund_combined_funds(
                    payment, "test_purchase_rollback",
                )
                second = club_economy.refund_combined_funds(
                    payment, "test_purchase_rollback",
                )
                result = club_economy.public_economy()

                self.assertFalse(first["already_refunded"])
                self.assertTrue(second["already_refunded"])
                self.assertEqual(result["bank_balance"], 100.0)
                self.assertEqual(result["casino_balance"], 50.0)
                self.assertEqual(sum(
                    row.get("refund_of") == payment["transaction_id"]
                    for row in result["transactions"]
                ), 1)

    def test_temporary_bank_cheat_adds_one_hundred_trillion_and_records_transaction(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (
                patch("tools.club_economy.save_data_root", return_value=root),
                patch("tools.betting_account.save_data_root", return_value=root),
            ):
                result = club_economy.adjust_bank_balance(
                    100_000_000_000_000, "temporary_bank_cheat",
                    source="bank_cheat_button",
                )

        self.assertEqual(result["bank_balance"], 100_000_000_000_000.0)
        self.assertEqual(result["transactions"][0]["type"], "temporary_bank_cheat")
        self.assertEqual(result["transactions"][0]["source"], "bank_cheat_button")

    def test_bank_adjustment_does_not_reload_after_commit(self) -> None:
        economy = {
            **club_economy._new_state(),
            "wallet_mode": "bank_v2",
        }
        with (
            patch("tools.club_economy.load_economy", return_value=economy),
            patch("tools.club_economy.available_balance", return_value=25.0),
            patch("tools.club_economy.save_economy") as save,
            patch(
                "tools.club_economy.public_economy",
                side_effect=RuntimeError("must not reload"),
            ) as reload_public,
        ):
            result = club_economy.adjust_bank_balance(75.0, "test_credit")

        self.assertEqual(result["bank_balance"], 75.0)
        self.assertEqual(result["total_balance"], 100.0)
        save.assert_called_once_with(economy)
        reload_public.assert_not_called()

    def test_bank_page_shows_hundred_trillion_button_only_in_development(self) -> None:
        root = Path(__file__).parents[1]
        script = (root / "web" / "app.js").read_text(encoding="utf-8")
        backend = (root / "fm_odds_web.py").read_text(encoding="utf-8")

        self.assertIn('endsWith("-dev")', script)
        self.assertIn("data-bank-cheat-hundred-trillion", script)
        self.assertIn('request("/api/bank/cheat-hundred-trillion"', script)
        self.assertIn('toast(uiText("bank.cheat_balance"))', script)
        self.assertLess(
            script.index("${developmentBankAction}"),
            script.index('data-open-wallet-transfer>${uiText("bank.transfer")}'),
        )
        self.assertIn(
            '100_000_000_000_000, "temporary_bank_cheat"', backend,
        )
        import fm_odds_web
        for frozen in (False, True):
            with patch.object(fm_odds_web, "FROZEN", frozen):
                route = fm_odds_web.build_api_routes().resolve(
                    "POST", "/api/bank/cheat-hundred-trillion",
                )
            self.assertEqual(route is not None, not frozen)

    def test_bank_overview_uses_text_labels_and_folds_older_dividends(self) -> None:
        root = Path(__file__).parents[1]
        script = (root / "web" / "app.js").read_text(encoding="utf-8")
        styles = (root / "web" / "app.css").read_text(encoding="utf-8")
        bank = script.split("function renderBank()", 1)[1].split(
            "function openMoneyDialog", 1,
        )[0]

        self.assertNotIn('class="bank-row-icon', bank)
        self.assertNotIn('data-lucide="landmark"', bank)
        self.assertNotIn('data-lucide="building-2"', bank)
        self.assertNotIn('data-lucide="wallet-cards"', bank)
        self.assertIn("latestDividendPayments", bank)
        self.assertIn('class="bank-dividend-archive"', bank)
        self.assertIn('uiText("bank.dividend.earlier"', bank)
        self.assertIn("#page-bank [data-refresh-club-info] svg{width:12px", styles)

    def test_bank_statement_uses_server_filters_pagination_and_stale_request_guard(self) -> None:
        root = Path(__file__).parents[1]
        script = (root / "web" / "app.js").read_text(encoding="utf-8")
        markup = (root / "web" / "index.html").read_text(encoding="utf-8")
        statement = script.split("function renderBankStatementDialog()", 1)[1].split(
            "function renderBank()", 1,
        )[0]

        self.assertIn("/api/bank/statement?", statement)
        self.assertIn("new URLSearchParams", statement)
        self.assertIn('page_size:"25"', statement)
        self.assertIn('month:"overview"', statement)
        self.assertIn('data-ledger-month="overview"', statement)
        self.assertIn('data-ledger-month="${escapeHtml(row.value)}"', statement)
        self.assertIn('class="bank-statement-workspace"', statement)
        self.assertIn('class="bank-statement-month-grid"', statement)
        self.assertIn('uiText("bank.month_change")', statement)
        self.assertIn('data-ledger-filter="account"', statement)
        self.assertIn('data-ledger-filter="category"', statement)
        self.assertIn('data-ledger-filter="direction"', statement)
        self.assertIn("localizedItemName({sku:entry.item_sku, name:entry.description})", statement)
        self.assertIn('localizedLedgerValue("ledger.transaction", entry.type', statement)
        self.assertIn('localizedLedgerValue("ledger.category", entry.category', statement)
        self.assertIn('localizedLedgerValue("ledger.account", entry.account', statement)
        self.assertIn('"ledger.account")', statement)
        self.assertIn('"ledger.category")', statement)
        self.assertIn("++app.bankStatementRequestKey", statement)
        self.assertIn("requestKey !== app.bankStatementRequestKey", statement)
        self.assertIn('aria-live="polite"', markup)

    def test_money_keypads_share_kmb_zero_shortcuts(self) -> None:
        root = Path(__file__).parents[1]
        script = (root / "web" / "app.js").read_text(encoding="utf-8")

        self.assertIn(
            'MONEY_ZERO_SHORTCUTS = Object.freeze({K:"000", M:"000000", B:"000000000"})',
            script,
        )
        self.assertIn(
            '"maximum","K","M","B","÷2"]',
            script,
        )
        self.assertIn(
            'const moneyKeys = ["1","2","3","⌫","4","5","6","clear","7","8","9",".","00","0","×2","maximum","K","M","B"]',
            script,
        )
        self.assertIn('key === "clear" ? "keypad.clear"', script)
        self.assertIn('key === "maximum" ? "keypad.maximum"', script)
        self.assertEqual(script.count("value += expandMoneyKey(key)"), 2)


if __name__ == "__main__":
    unittest.main()
