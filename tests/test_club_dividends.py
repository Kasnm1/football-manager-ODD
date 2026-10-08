import copy

import tools.club_economy as club_economy
from tools.money import to_minor, write_minor


def dividend_state(monkeypatch):
    state = club_economy._new_state()
    write_minor(state, "general_balance", to_minor(1_000.0))
    saved = []
    monkeypatch.setattr(club_economy, "load_economy", lambda: state)
    monkeypatch.setattr(
        club_economy, "save_economy",
        lambda payload: saved.append(copy.deepcopy(payload)),
    )
    return state, saved


def test_dividend_pays_five_percent_once_for_closed_full_ownership_month(monkeypatch):
    state, saved = dividend_state(monkeypatch)
    clubs = [{
        "id": 42, "name": "测试俱乐部", "acquired_game_date": "2026-06-15",
    }]
    metrics = {42: {"monthly_profits": [
        {"month": "2026-06", "profit": 9_000_000},
        {"month": "2026-07", "profit": 2_000_000},
        {"month": "2026-08", "profit": 3_000_000},
    ]}}

    first = club_economy.settle_club_dividends("2026-08-12", clubs, metrics)
    second = club_economy.settle_club_dividends("2026-08-12", clubs, metrics)

    assert first["paid_count"] == 1
    assert first["paid_amount"] == 100_000.0
    assert first["forecasts"] == [{
        "team_id": 42, "team_name": "测试俱乐部", "month": "2026-08",
        "profit_month": "2026-07", "profit": 2_000_000,
        "profit_source": "monthly_summary", "rate": 0.05,
        "estimated_amount": 100_000.0, "eligible": True,
    }]
    assert first["last_paid_month"] == "2026-07"
    assert first["last_paid_total"] == 100_000.0
    assert first["new_payments"][0]["paid_on"] == "2026-08-01"
    assert second["paid_count"] == 0
    assert state["general_balance"] == 101_000.0
    assert len(state["club_dividend_payments"]) == 1
    assert state["transactions"][-1]["type"] == "club_dividend"
    assert state["transactions"][-1]["dividend_month"] == "2026-07"
    assert saved


def test_legacy_acquisition_starts_from_current_month_without_backpay(monkeypatch):
    state, _saved = dividend_state(monkeypatch)
    clubs = [{"id": 42, "name": "旧存档俱乐部"}]
    metrics = {42: {"monthly_profits": [
        {"month": "2026-07", "profit": 5_000_000},
        {"month": "2026-08", "profit": 1_000_000},
    ]}}

    result = club_economy.settle_club_dividends("2026-08-12", clubs, metrics)

    assert result["paid_count"] == 0
    assert result["forecasts"][0]["estimated_amount"] == 250_000.0
    assert result["forecasts"][0]["profit_month"] == "2026-07"
    assert result["forecasts"][0]["eligible"] is False
    assert state["club_dividend_baselines"] == {"42": "2026-08"}
    assert state["general_balance"] == 1_000.0


def test_loss_month_is_closed_without_creating_a_dividend(monkeypatch):
    state, _saved = dividend_state(monkeypatch)
    clubs = [{
        "id": 42, "name": "测试俱乐部", "acquired_game_date": "2026-05-01",
    }]
    metrics = {42: {"monthly_profits": [
        {"month": "2026-06", "profit": -500_000},
        {"month": "2026-07", "profit": 0},
    ]}}

    result = club_economy.settle_club_dividends("2026-07-10", clubs, metrics)

    assert result["paid_count"] == 0
    assert state["club_dividend_checked_through"] == {"42": "2026-06"}
    assert state["club_dividend_payments"] == []
    assert state["general_balance"] == 1_000.0


def test_income_statement_last_month_fills_missing_monthly_summary(monkeypatch):
    state, _saved = dividend_state(monkeypatch)
    clubs = [{
        "id": 42, "name": "测试俱乐部", "acquired_game_date": "2026-05-01",
    }]
    metrics = {42: {"monthly_profits": [], "last_month_profit": 3_000_000}}

    result = club_economy.settle_club_dividends("2026-07-10", clubs, metrics)

    assert result["paid_count"] == 1
    assert result["paid_amount"] == 150_000.0
    assert result["forecasts"][0]["profit_month"] == "2026-06"
    assert result["forecasts"][0]["profit_source"] == "income_statement"
    assert result["last_paid_month"] == "2026-06"
    assert result["last_paid_total"] == 150_000.0


def test_sold_club_stops_current_and_future_dividend_tracking(monkeypatch):
    state, saved = dividend_state(monkeypatch)
    state["club_dividend_baselines"] = {"42": "2026-05"}
    state["club_dividend_checked_through"] = {"42": "2026-06"}
    state["club_dividend_forecasts"] = {"42": {"team_id": 42, "month": "2026-07"}}
    state["club_dividend_payments"] = [{"team_id": 42, "month": "2026-06"}]

    club_economy.forget_club_dividend_tracking(42)

    assert state["club_dividend_baselines"] == {}
    assert state["club_dividend_checked_through"] == {}
    assert state["club_dividend_forecasts"] == {}
    assert state["club_dividend_payments"] == [{"team_id": 42, "month": "2026-06"}]
    assert saved
