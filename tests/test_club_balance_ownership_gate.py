from __future__ import annotations

import threading
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

import fm_odds_web


ROOT = (Path(__file__).resolve().parents[1] / "src")


def managed_club_state() -> fm_odds_web.LocalOddsState:
    state = fm_odds_web.LocalOddsState.__new__(fm_odds_web.LocalOddsState)
    state.lock = threading.RLock()
    state.memory_lock = threading.RLock()
    state.output = {"data_scope_id": "scope-1"}
    state._bind_current_save = lambda: "scope-1"
    state._managed_club_team = lambda: {
        "id": 42, "name": "当前执教俱乐部", "address": "0x1000",
    }
    return state


def test_club_balance_status_marks_only_current_acquired_club(monkeypatch):
    state = managed_club_state()
    monkeypatch.setattr(
        fm_odds_web, "read_club_balance", lambda *_args: {"amount": 500_000},
    )
    monkeypatch.setattr(
        fm_odds_web, "load_acquired_clubs",
        lambda _scope: {"clubs": [{"id": 84}]},
    )

    assert state._club_balance_status()["owned_by_player"] is False

    monkeypatch.setattr(
        fm_odds_web, "load_acquired_clubs",
        lambda _scope: {"clubs": [{"id": 42}, {"id": 84}]},
    )
    assert state._club_balance_status()["owned_by_player"] is True


def test_transfer_budget_status_retries_an_ambiguous_locator_once(monkeypatch):
    state = managed_club_state()
    calls = []

    def read_budget(_address, _team_id, *, force_scan=False):
        calls.append(force_scan)
        if not force_scan:
            raise RuntimeError("找到多个疑似转会预算字段，无法安全确认当前俱乐部")
        return {"amount": 77_400_000}

    monkeypatch.setattr(fm_odds_web, "read_transfer_budget", read_budget)

    result = state._transfer_budget_status()

    assert calls == [False, True]
    assert result["available"] is True
    assert result["amount"] == 77_400_000


def test_transfer_budget_status_uses_last_confirmed_amount_read_only(monkeypatch):
    state = managed_club_state()
    state.last_club_funds_status = {
        "transfer_budget": {
            "available": True, "amount": 77_400_000, "team_id": 42,
            "team_name": "当前执教俱乐部",
        },
    }
    monkeypatch.setattr(
        fm_odds_web, "read_transfer_budget",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("找到多个疑似转会预算字段，无法安全确认当前俱乐部")
        ),
    )

    result = state._transfer_budget_status()

    assert result["available"] is True
    assert result["amount"] == 77_400_000
    assert result["stale"] is True
    assert result["read_only"] is True
    assert result["status"] == "读取暂时不稳定，显示上次已确认金额"
    assert state._transfer_budget_status()["amount"] == 77_400_000


def test_fresh_club_profile_seeds_non_blocking_fund_status(monkeypatch):
    state = managed_club_state()
    state.last_club_funds_status = {}
    profiles = {
        42: {
            "team": {
                "id": 42, "name": "当前执教俱乐部", "team_type": "club",
            },
            "club_information": {"finances": {
                "balance": 134_800_000,
                "remaining_transfer_budget": 77_400_000,
            }},
        },
    }
    monkeypatch.setattr(
        fm_odds_web, "load_acquired_clubs", lambda _scope: {"clubs": [{"id": 42}]},
    )

    result = state._cache_club_funds_from_profiles(
        profiles, 42, scope_id="scope-1",
    )

    assert result["club_balance"]["amount"] == 134_800_000
    assert result["club_balance"]["owned_by_player"] is True
    assert result["transfer_budget"]["amount"] == 77_400_000
    assert state.last_club_funds_status == result
    assert {
        row["source"] for row in result.values()
    } == {"club_profile_refresh"}


def test_unacquired_managed_club_balance_cannot_be_withdrawn(monkeypatch):
    state = managed_club_state()
    monkeypatch.setattr(
        fm_odds_web, "load_acquired_clubs", lambda _scope: {"clubs": []},
    )
    monkeypatch.setattr(
        fm_odds_web, "read_club_balance",
        lambda *_args: pytest.fail("未收购时不应读取结余并开始转账"),
    )

    with pytest.raises(ValueError, match="收购当前执教俱乐部之后才能挪用"):
        state.transfer_club_balance({"direction": "out", "amount": 100_000})


def test_acquired_managed_club_balance_can_be_withdrawn(monkeypatch):
    state = managed_club_state()
    state._economy_patch_response = lambda economy: economy
    operation = SimpleNamespace(reader=object(), writable=True)
    monkeypatch.setattr(
        fm_odds_web, "load_acquired_clubs",
        lambda _scope: {"clubs": [{"id": 42}]},
    )
    monkeypatch.setattr(
        fm_odds_web, "borrow_game_operation",
        lambda **_kwargs: nullcontext(operation),
    )
    monkeypatch.setattr(
        fm_odds_web, "read_game_clock_from_reader",
        lambda _reader: {"date": "2026-07-29", "minutes": 600, "time": "10:00"},
    )
    monkeypatch.setattr(
        fm_odds_web, "read_club_balance",
        lambda *_args, **_kwargs: {"amount": 500_000},
    )
    writes = []
    monkeypatch.setattr(
        fm_odds_web, "write_club_balance",
        lambda _address, amount, **kwargs: (
            writes.append((amount, kwargs)) or {"amount": amount}
        ),
    )
    monkeypatch.setattr(
        fm_odds_web, "public_economy", lambda: {"bank_balance": 1_000_000},
    )
    monkeypatch.setattr(
        fm_odds_web, "adjust_bank_balance",
        lambda *_args, **_kwargs: {"bank_balance": 1_100_000},
    )

    result = state.transfer_club_balance({"direction": "out", "amount": 100_000})

    assert writes == [(400_000, {
        "team_id": 42, "expected": 500_000, "operation": operation,
    })]
    assert result["club_balance"]["owned_by_player"] is True


def test_money_dialog_disables_withdrawal_until_current_club_is_acquired():
    script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")

    assert 'club_balance_out:["挪用俱乐部结余", "收购俱乐部之后开启挪用结余功能"]' in script
    assert 'id="money-input-confirm"' in html
    assert 'direction === "club_balance_out" && !clubBalanceWithdrawalEnabled' in script
