from __future__ import annotations

import threading
from contextlib import nullcontext
from types import SimpleNamespace

import fm_odds_web
import pytest


def _state() -> fm_odds_web.LocalOddsState:
    state = fm_odds_web.LocalOddsState.__new__(fm_odds_web.LocalOddsState)
    state.lock = threading.RLock()
    state.memory_lock = threading.RLock()
    state.last_operation_performance = {}
    state.output = {"data_scope_id": "scope-1"}
    state._bind_current_save = lambda: "scope-1"
    state._managed_club_team = lambda: {
        "id": 42, "name": "测试俱乐部", "address": "0x1000",
    }
    state._economy_patch_response = lambda response, **_kwargs: response
    return state


def test_transfer_budget_uses_wallet_when_bank_is_short(monkeypatch):
    state = _state()
    operation = SimpleNamespace(reader=object(), writable=True)
    writes = []
    monkeypatch.setattr(
        fm_odds_web, "borrow_game_operation",
        lambda **_kwargs: nullcontext(operation),
    )
    monkeypatch.setattr(
        fm_odds_web, "read_game_clock_from_reader",
        lambda _reader: {"date": "2026-08-24", "minutes": 600, "time": "10:00"},
    )
    monkeypatch.setattr(
        fm_odds_web, "read_transfer_budget",
        lambda *_args, **_kwargs: {"amount": 500_000},
    )
    monkeypatch.setattr(
        fm_odds_web, "write_transfer_budget",
        lambda _address, amount, **kwargs: (
            writes.append((amount, kwargs)) or {"amount": amount}
        ),
    )
    monkeypatch.setattr(
        fm_odds_web, "public_economy",
        lambda: {"bank_balance": 50.0, "casino_balance": 100.0},
    )
    payments = []
    monkeypatch.setattr(
        fm_odds_web, "charge_combined_funds",
        lambda amount, kind, **details: (
            payments.append((amount, kind, details))
            or {"bank": 50.0, "wallet": 50.0, "total": amount},
        ),
    )
    monkeypatch.setattr(
        fm_odds_web, "adjust_bank_balance",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("银行不足时不应走纯银行扣款")
        ),
    )

    result = state.transfer_club_budget({"direction": "in", "amount": 100})

    assert writes[0][0] == 500_100
    assert payments[0][0:2] == (100, "bank_to_transfer_budget")
    assert result["transfer_budget"]["amount"] == 500_100


def test_transfer_budget_retries_exact_object_lookup_with_a_fresh_operation(monkeypatch):
    state = _state()
    operations = [
        SimpleNamespace(reader=SimpleNamespace(name="first"), writable=True),
        SimpleNamespace(reader=SimpleNamespace(name="second"), writable=True),
    ]
    borrowed = []

    def borrow(**_kwargs):
        operation = operations[len(borrowed)]
        borrowed.append(operation)
        return nullcontext(operation)

    monkeypatch.setattr(fm_odds_web, "borrow_game_operation", borrow)
    monkeypatch.setattr(
        fm_odds_web, "read_game_clock_from_reader",
        lambda _reader: {"date": "2026-08-30", "minutes": 600, "time": "10:00"},
    )
    reads = []

    def read_budget(_address, _team_id, **kwargs):
        reads.append(kwargs)
        if len(reads) == 1:
            raise fm_odds_web.TransferBudgetLocationError("临时对象定位失败")
        return {"amount": 500_000}

    monkeypatch.setattr(fm_odds_web, "read_transfer_budget", read_budget)
    writes = []
    monkeypatch.setattr(
        fm_odds_web, "write_transfer_budget",
        lambda _address, amount, **kwargs: (
            writes.append((amount, kwargs)) or {"amount": amount}
        ),
    )
    monkeypatch.setattr(
        fm_odds_web, "public_economy", lambda: {"bank_balance": 1_000.0},
    )
    charges = []
    monkeypatch.setattr(
        fm_odds_web, "adjust_bank_balance",
        lambda amount, kind, **details: (
            charges.append((amount, kind, details)) or {"bank_balance": 900.0}
        ),
    )

    result = state.transfer_club_budget({"direction": "in", "amount": 100})

    assert len(borrowed) == 2
    assert [row["allow_scan"] for row in reads] == [False, False]
    assert writes[0][0] == 500_100
    assert writes[0][1]["operation"] is borrowed[1]
    assert charges[0][0:2] == (-100, "bank_to_transfer_budget")
    assert result["transfer_budget"]["amount"] == 500_100


def test_transfer_budget_persistent_location_failure_never_charges_account(monkeypatch):
    state = _state()
    borrow_count = 0

    def borrow(**_kwargs):
        nonlocal borrow_count
        borrow_count += 1
        return nullcontext(SimpleNamespace(reader=object(), writable=True))

    monkeypatch.setattr(fm_odds_web, "borrow_game_operation", borrow)
    monkeypatch.setattr(
        fm_odds_web, "read_game_clock_from_reader", lambda _reader: {},
    )
    monkeypatch.setattr(
        fm_odds_web, "read_transfer_budget",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            fm_odds_web.TransferBudgetLocationError("无法取得财务对象")
        ),
    )
    monkeypatch.setattr(
        fm_odds_web, "write_transfer_budget",
        lambda *_args, **_kwargs: pytest.fail("定位失败时不应写入 FM 内存"),
    )
    monkeypatch.setattr(
        fm_odds_web, "adjust_bank_balance",
        lambda *_args, **_kwargs: pytest.fail("定位失败时不应扣款"),
    )
    monkeypatch.setattr(
        fm_odds_web, "charge_combined_funds",
        lambda *_args, **_kwargs: pytest.fail("定位失败时不应扣钱包"),
    )

    with pytest.raises(
        fm_odds_web.TransferBudgetLocationError,
        match="无法取得财务对象",
    ):
        state.transfer_club_budget({"direction": "in", "amount": 100})

    assert borrow_count == 2


def test_transfer_budget_location_failure_returns_last_confirmed_amount(monkeypatch):
    state = _state()
    state.last_club_funds_status = {
        "transfer_budget": {
            "available": True,
            "amount": 500_000,
            "team_id": 42,
            "team_name": "测试俱乐部",
        },
    }

    monkeypatch.setattr(
        fm_odds_web, "borrow_game_operation",
        lambda **_kwargs: nullcontext(SimpleNamespace(reader=object(), writable=True)),
    )
    monkeypatch.setattr(
        fm_odds_web, "read_game_clock_from_reader", lambda _reader: {},
    )
    monkeypatch.setattr(
        fm_odds_web, "read_transfer_budget",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            fm_odds_web.TransferBudgetLocationError("无法取得财务对象")
        ),
    )
    monkeypatch.setattr(
        fm_odds_web, "public_economy",
        lambda: {"bank_balance": 1_000, "casino_balance": 2_000},
    )
    monkeypatch.setattr(
        fm_odds_web, "write_transfer_budget",
        lambda *_args, **_kwargs: pytest.fail("定位失败时不应写入 FM 内存"),
    )
    monkeypatch.setattr(
        fm_odds_web, "adjust_bank_balance",
        lambda *_args, **_kwargs: pytest.fail("定位失败时不应扣款"),
    )

    result = state.transfer_club_budget({"direction": "in", "amount": 100})

    assert result["operation_applied"] is False
    assert result["transfer_budget"]["amount"] == 500_000
    assert result["transfer_budget"]["read_only"] is True
    assert "未修改" in result["operation_status"]


def test_transfer_budget_write_location_failure_returns_last_confirmed_amount(monkeypatch):
    state = _state()
    state.last_club_funds_status = {
        "transfer_budget": {
            "available": True,
            "amount": 500_000,
            "team_id": 42,
            "team_name": "测试俱乐部",
        },
    }
    operations = [
        SimpleNamespace(reader=SimpleNamespace(name="first"), writable=True),
        SimpleNamespace(reader=SimpleNamespace(name="second"), writable=True),
    ]
    borrowed = []

    def borrow(**_kwargs):
        operation = operations[len(borrowed)]
        borrowed.append(operation)
        return nullcontext(operation)

    monkeypatch.setattr(fm_odds_web, "borrow_game_operation", borrow)
    monkeypatch.setattr(
        fm_odds_web, "read_game_clock_from_reader", lambda _reader: {},
    )
    reads = []

    def read_budget(_address, _team_id, **kwargs):
        reads.append(kwargs)
        return {"amount": 500_000}

    monkeypatch.setattr(fm_odds_web, "read_transfer_budget", read_budget)
    writes = []

    def write_budget(_address, amount, **kwargs):
        writes.append((amount, kwargs))
        raise fm_odds_web.TransferBudgetLocationError("写入阶段对象定位失败")

    monkeypatch.setattr(fm_odds_web, "write_transfer_budget", write_budget)
    monkeypatch.setattr(
        fm_odds_web, "public_economy",
        lambda: {"bank_balance": 1_000, "casino_balance": 2_000},
    )
    monkeypatch.setattr(
        fm_odds_web, "adjust_bank_balance",
        lambda *_args, **_kwargs: pytest.fail("写入定位失败时不应扣款"),
    )
    monkeypatch.setattr(
        fm_odds_web, "charge_combined_funds",
        lambda *_args, **_kwargs: pytest.fail("写入定位失败时不应扣钱包"),
    )

    result = state.transfer_club_budget({"direction": "in", "amount": 100})

    assert len(borrowed) == 2
    assert len(reads) == 2
    assert len(writes) == 2
    assert [row[1]["operation"] for row in writes] == operations
    assert result["operation_applied"] is False
    assert result["transfer_budget"]["amount"] == 500_000
    assert result["transfer_budget"]["read_only"] is True
    assert "未修改" in result["operation_status"]


def test_wallet_top_up_rejects_when_combined_funds_are_insufficient(monkeypatch):
    state = _state()
    operation = SimpleNamespace(reader=object(), writable=True)
    writes = []
    monkeypatch.setattr(
        fm_odds_web, "borrow_game_operation",
        lambda **_kwargs: nullcontext(operation),
    )
    monkeypatch.setattr(
        fm_odds_web, "read_game_clock_from_reader",
        lambda _reader: {"date": "2026-08-24", "minutes": 600, "time": "10:00"},
    )
    monkeypatch.setattr(
        fm_odds_web, "read_transfer_budget",
        lambda *_args, **_kwargs: {"amount": 500_000},
    )
    monkeypatch.setattr(
        fm_odds_web, "write_transfer_budget",
        lambda _address, amount, **kwargs: (
            writes.append((amount, kwargs)) or {"amount": amount}
        ),
    )
    monkeypatch.setattr(
        fm_odds_web, "public_economy",
        lambda: {"bank_balance": 50.0, "casino_balance": 25.0},
    )
    monkeypatch.setattr(
        fm_odds_web, "charge_combined_funds",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ValueError("银行与钱包余额合计不足")
        ),
    )

    try:
        state.transfer_club_budget({"direction": "in", "amount": 100})
    except ValueError as error:
        assert str(error) == "银行与钱包余额合计不足"
    else:
        raise AssertionError("余额合计不足时应拒绝转入")

    assert [row[0] for row in writes] == [500_100, 500_000]
