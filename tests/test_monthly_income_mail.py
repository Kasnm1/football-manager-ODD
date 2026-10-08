from __future__ import annotations

import threading
from unittest.mock import patch

import fm_odds_web


def test_salary_check_without_payment_does_not_touch_mailbox():
    with patch("fm_odds_web.load_mail") as load_mail:
        assert fm_odds_web.archive_salary_payment_mail([]) == []
    load_mail.assert_not_called()


def test_salary_mail_is_created_once_for_each_actual_payment():
    records = []
    payments = [
        {"id": "salary-1", "game_date": "2028-02-06", "net_amount": 420.0},
        {"id": "salary-2", "game_date": "2028-02-13", "net_amount": 420.0},
    ]
    with (
        patch("fm_odds_web.load_mail", side_effect=lambda: records),
        patch("fm_odds_web.save_mail"),
        patch("fm_odds_web.configured_money", side_effect=lambda value: f"£{value:,.2f}"),
    ):
        first = fm_odds_web.archive_salary_payment_mail(payments)
        second = fm_odds_web.archive_salary_payment_mail(payments)

    assert len(first) == 2
    assert second == []
    assert [mail["type"] for mail in first] == ["manager_salary", "manager_salary"]
    assert [mail["game_date"] for mail in first] == ["2028-02-06", "2028-02-13"]
    assert [mail["title"] for mail in first] == ["工资已到账", "工资已到账"]
    assert all(mail["template_version"] == 1 for mail in first)
    assert all(mail["title_key"] == "mail.type.manager_salary.title" for mail in first)
    assert first[0]["template_params"] == {
        "date": "2028-02-06", "amount": 420.0,
    }


def test_dividend_mail_is_created_once_for_each_club_payment():
    records = []
    payment = {
        "id": "dividend-1",
        "team_id": 42,
        "team_name": "Test Club",
        "month": "2028-01",
        "paid_on": "2028-02-01",
        "rate": 0.05,
        "amount": 100_000.0,
    }
    with (
        patch("fm_odds_web.load_mail", side_effect=lambda: records),
        patch("fm_odds_web.save_mail"),
        patch("fm_odds_web.configured_money", side_effect=lambda value: f"£{value:,.2f}"),
    ):
        first = fm_odds_web.archive_club_dividend_mail([payment])
        second = fm_odds_web.archive_club_dividend_mail([payment])

    assert len(first) == 1
    assert second == []
    assert first[0]["type"] == "club_dividend"
    assert first[0]["game_date"] == "2028-02-01"
    assert first[0]["amount"] == 100_000.0
    assert first[0]["team_name"] == "Test Club"
    assert first[0]["message_key"] == "mail.type.club_dividend.message"
    assert first[0]["template_params"] == {
        "team": "Test Club", "month": "2028-01", "rate": 5.0,
        "date": "2028-02-01", "amount": 100_000.0,
    }
    assert first[0]["message"] == (
        "2028-01 月度利润的 5% 分红 £100,000.00 已于 2028-02-01 存入银行。"
    )


def test_monthly_dividend_trigger_runs_once_for_current_account_month():
    state = fm_odds_web.LocalOddsState.__new__(fm_odds_web.LocalOddsState)
    state.lock = threading.RLock()
    state.output = {"save_instance_id": "save-1", "account_scope_id": "account-1"}
    state._has_verified_save = lambda: True
    state._data_scope_id = lambda _output: "account-1"
    state.owned_world_club_metrics = lambda: {"dividends": {"paid_count": 1}}

    with (
        patch("fm_odds_web.set_active_save_id") as bind_scope,
        patch("fm_odds_web.load_economy", return_value={
            "club_dividend_last_settlement_month": "2028-01",
        }),
    ):
        result = state._process_club_dividends_for_date("2028-02-17")

    bind_scope.assert_called_once_with("account-1")
    assert result["processed"] is True
    assert result["dividends"]["paid_count"] == 1


def test_monthly_dividend_trigger_skips_month_already_settled():
    state = fm_odds_web.LocalOddsState.__new__(fm_odds_web.LocalOddsState)
    state.lock = threading.RLock()
    state.output = {"save_instance_id": "save-1", "account_scope_id": "account-1"}
    state._has_verified_save = lambda: True
    state._data_scope_id = lambda _output: "account-1"
    with (
        patch("fm_odds_web.set_active_save_id"),
        patch("fm_odds_web.load_economy", return_value={
            "club_dividend_last_settlement_month": "2028-02",
        }),
        patch.object(state, "owned_world_club_metrics") as metrics,
    ):
        result = state._process_club_dividends_for_date("2028-02-17")

    metrics.assert_not_called()
    assert result == {"processed": True, "already_settled": True}


def test_first_connected_month_processes_dividends_without_portfolio_refresh():
    state = fm_odds_web.LocalOddsState.__new__(fm_odds_web.LocalOddsState)
    state.lock = threading.RLock()
    state.output = {"save_instance_id": "save-1"}
    state._has_verified_save = lambda: True
    state._data_scope_id = lambda _output: "account-1"
    state.data_version = 7
    state.monthly_income_last_processed_month = None
    state.monthly_income_last_processed_key = None
    state.monthly_income_last_attempted_date = None
    with patch.object(
        state,
        "_process_club_dividends_for_date",
        return_value={"processed": True, "paid": 1},
    ) as process:
        result = state._process_monthly_club_dividends_if_due("2028-02-17")

    process.assert_called_once_with("2028-02-17")
    assert result == {"processed": True, "paid": 1}
    assert state.monthly_income_last_processed_month == "2028-02"
    assert state.monthly_income_last_processed_key == ("account-1", "2028-02")
    assert state.monthly_income_last_attempted_date == "2028-02-17"
    assert state.data_version == 8


def test_observed_month_transition_still_processes_dividends():
    state = fm_odds_web.LocalOddsState.__new__(fm_odds_web.LocalOddsState)
    state.lock = threading.RLock()
    state.output = {"save_instance_id": "save-1"}
    state._has_verified_save = lambda: True
    state._data_scope_id = lambda _output: "account-1"
    state.data_version = 2
    state.monthly_income_last_processed_month = "2028-01"
    state.monthly_income_last_processed_key = ("account-1", "2028-01")
    state.monthly_income_last_attempted_date = "2028-01-31"
    with patch.object(
        state,
        "_process_club_dividends_for_date",
        return_value={"processed": True, "paid": 1},
    ) as process:
        result = state._process_monthly_club_dividends_if_due("2028-02-01")

    process.assert_called_once_with("2028-02-01")
    assert result == {"processed": True, "paid": 1}
    assert state.monthly_income_last_processed_month == "2028-02"
    assert state.monthly_income_last_processed_key == ("account-1", "2028-02")
    assert state.data_version == 3


def test_monthly_dividend_failure_retries_on_the_same_game_date():
    state = fm_odds_web.LocalOddsState.__new__(fm_odds_web.LocalOddsState)
    state.lock = threading.RLock()
    state.output = {"save_instance_id": "save-1"}
    state._has_verified_save = lambda: True
    state._data_scope_id = lambda _output: "account-1"
    state.data_version = 4
    state.monthly_income_last_processed_month = None
    state.monthly_income_last_processed_key = None
    state.monthly_income_last_attempted_date = None
    with patch.object(
        state,
        "_process_club_dividends_for_date",
        side_effect=[RuntimeError("temporary read failure"), {"processed": True, "paid": 1}],
    ) as process:
        try:
            state._process_monthly_club_dividends_if_due("2028-02-17")
        except RuntimeError:
            pass
        else:
            raise AssertionError("the first temporary failure must be observable")
        result = state._process_monthly_club_dividends_if_due("2028-02-17")

    assert process.call_count == 2
    assert result == {"processed": True, "paid": 1}
    assert state.monthly_income_last_attempted_date == "2028-02-17"
    assert state.data_version == 5


def test_monthly_dividend_guard_is_scoped_to_the_current_account():
    state = fm_odds_web.LocalOddsState.__new__(fm_odds_web.LocalOddsState)
    state.lock = threading.RLock()
    state.output = {"save_instance_id": "save-2"}
    state._has_verified_save = lambda: True
    active_scope = {"value": "account-1"}
    state._data_scope_id = lambda _output: active_scope["value"]
    state.data_version = 10
    state.monthly_income_last_processed_month = "2028-02"
    state.monthly_income_last_processed_key = ("account-1", "2028-02")
    state.monthly_income_last_attempted_date = "2028-02-17"
    active_scope["value"] = "account-2"
    with patch.object(
        state,
        "_process_club_dividends_for_date",
        return_value={"processed": True, "paid": 1},
    ) as process:
        result = state._process_monthly_club_dividends_if_due("2028-02-17")

    process.assert_called_once_with("2028-02-17")
    assert result == {"processed": True, "paid": 1}
    assert state.monthly_income_last_processed_key == ("account-2", "2028-02")
    assert state.data_version == 11
