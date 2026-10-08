from __future__ import annotations

import unittest
from datetime import date
from unittest.mock import patch

from tools import club_economy
from tools.club_economy import (
    _advance_salary_state, _new_state, next_salary_payday,
)
from tools.money import read_minor, to_minor, write_minor


class SalaryPaymentPrecisionTests(unittest.TestCase):
    def test_salary_defaults_to_weekly_and_next_payday_is_sunday(self):
        state = _new_state()

        self.assertEqual(state["salary_schedule"], "weekly")
        self.assertEqual(next_salary_payday("2028-02-01", "weekly"), "2028-02-06")
        self.assertEqual(next_salary_payday("2028-02-06", "weekly"), "2028-02-13")

    def test_salary_status_uses_thirty_percent_tax_rate(self):
        with patch.object(club_economy, "load_economy", return_value=_new_state()):
            status = club_economy.salary_status("2028-02-01", gross_weekly=100)

        self.assertEqual(status["tax_rate"], 0.30)
        self.assertEqual(status["net_weekly"], 70.0)
        self.assertEqual(status["net_monthly"], 303.33)

    def test_full_week_pays_exact_integer_net_salary(self):
        state = _new_state()
        state["salary_accrual_through"] = "2027-12-26"
        state["salary_checked_through"] = "2027-12-26"

        paydays, total = _advance_salary_state(
            state, date(2028, 1, 2), gross_weekly=100, schedule="weekly",
        )

        self.assertEqual(paydays, [date(2028, 1, 2)])
        self.assertEqual(total, 70.0)
        self.assertEqual(state["general_balance"], 70.0)
        self.assertEqual(state["salary_payments"][-1]["net_amount"], 70.0)
        self.assertEqual(state["salary_payments"][-1]["tax_rate"], 0.30)

    def test_date_jump_pays_each_crossed_sunday_separately(self):
        state = _new_state()
        state["salary_accrual_through"] = "2027-12-26"
        state["salary_checked_through"] = "2027-12-26"

        paydays, total = _advance_salary_state(
            state, date(2028, 1, 16), gross_weekly=100, schedule="weekly",
        )

        self.assertEqual(paydays, [
            date(2028, 1, 2), date(2028, 1, 9), date(2028, 1, 16),
        ])
        self.assertEqual(total, 210.0)
        self.assertEqual(
            [payment["game_date"] for payment in state["salary_payments"]],
            ["2028-01-02", "2028-01-09", "2028-01-16"],
        )

    def test_midweek_salary_change_keeps_fractional_daily_accrual(self):
        state = _new_state()
        state["salary_accrual_through"] = "2027-12-26"
        state["salary_checked_through"] = "2027-12-26"

        _advance_salary_state(
            state, date(2027, 12, 29), gross_weekly=100, schedule="weekly",
        )
        _paydays, total = _advance_salary_state(
            state, date(2028, 1, 2), gross_weekly=200, schedule="weekly",
        )

        self.assertEqual(total, 110.0)
        self.assertEqual(state["general_balance"], 110.0)

    def test_salary_atomically_adds_to_latest_persisted_bank_balance(self):
        stale = _new_state()
        stale["wallet_mode"] = "bank_v2"
        stale["salary_accrual_through"] = "2027-12-26"
        stale["salary_checked_through"] = "2027-12-26"
        latest = {**stale}
        write_minor(latest, "general_balance", to_minor(9_000_000_000_000))

        def update(_key, _default, mutator, **_kwargs):
            return mutator(latest)

        with (
            patch.object(club_economy, "load_economy", return_value=stale),
            patch.object(club_economy, "update_document", side_effect=update) as atomic_update,
            patch.object(club_economy, "salary_status", return_value={}),
            patch.object(club_economy, "save_economy") as stale_save,
        ):
            result = club_economy.process_salary_payments(
                "2028-01-02", gross_weekly=3_000,
            )

        self.assertEqual(result["amount"], 2_100.0)
        self.assertEqual(
            read_minor(latest, "general_balance"),
            to_minor(9_000_000_002_100),
        )
        atomic_update.assert_called_once()
        stale_save.assert_not_called()


if __name__ == "__main__":
    unittest.main()
