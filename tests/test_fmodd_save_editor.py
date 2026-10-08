from __future__ import annotations

import gzip
import json
from pathlib import Path

import pytest

from tools.fmodd_save_editor import (
    FmoddEditorError,
    load_fmodd,
    read_balance,
    save_fmodd,
    set_balance,
)


def _write_container(path: Path) -> None:
    payload = {
        "schema_version": 1,
        "scope_id": path.stem,
        "updated_at": "2026-01-01T00:00:00",
        "documents": {
            "wallet": {
                "balance": 10.0,
                "balance_minor": 1000,
                "transactions": [],
            },
            "economy": {
                "general_balance": 20.0,
                "general_balance_minor": 2000,
                "transactions": [],
            },
        },
    }
    path.write_bytes(gzip.compress(json.dumps(payload).encode("utf-8")))


def test_editor_round_trip_updates_balances_and_keeps_backup(tmp_path: Path) -> None:
    path = tmp_path / "account-test.fmodd"
    _write_container(path)
    original = path.read_bytes()
    payload = load_fmodd(path)

    set_balance(payload, "wallet", "1234.56")
    set_balance(payload, "bank", "789.01")
    backup = save_fmodd(path, payload)

    assert backup.read_bytes() == original
    reopened = load_fmodd(path)
    assert str(read_balance(reopened, "wallet")) == "1234.56"
    assert str(read_balance(reopened, "bank")) == "789.01"
    wallet_transaction = reopened["documents"]["wallet"]["transactions"][-1]
    assert wallet_transaction["amount_minor"] == 122456
    assert wallet_transaction["balance_after_minor"] == 123456
    bank_transaction = reopened["documents"]["economy"]["transactions"][-1]
    assert bank_transaction["amount_minor"] == 76901
    assert bank_transaction["general_balance_after_minor"] == 78901


def test_editor_rejects_scope_mismatch(tmp_path: Path) -> None:
    path = tmp_path / "expected.fmodd"
    _write_container(path)
    payload = json.loads(gzip.decompress(path.read_bytes()).decode("utf-8"))
    payload["scope_id"] = "different"
    path.write_bytes(gzip.compress(json.dumps(payload).encode("utf-8")))

    with pytest.raises(FmoddEditorError, match="作用域"):
        load_fmodd(path)


def test_editor_rejects_negative_quick_balance(tmp_path: Path) -> None:
    path = tmp_path / "account-test.fmodd"
    _write_container(path)
    payload = load_fmodd(path)

    with pytest.raises(FmoddEditorError, match="大于或等于 0"):
        set_balance(payload, "wallet", "-1")
