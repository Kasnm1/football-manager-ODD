from __future__ import annotations

import struct
from types import SimpleNamespace

import pytest

from tools import transfer_budget


class ExactBudgetReader:
    def __init__(
        self, *, team=0x1000, club=0x2000, finance=0x3000,
        team_id=1190, club_finance_offset=0x1A0, budget_offset=0x24,
    ):
        self.module_base = 0x10000000
        self.team = team
        self.club = club
        self.finance = finance
        self.team_id = team_id
        self.club_finance_offset = club_finance_offset
        self.budget_offset = budget_offset
        self.amount = 25_000_000
        self.reverse_reference = club

    def ptr(self, address):
        return {
            self.team: self.module_base + 0x1110,
            self.team + transfer_budget.TEAM_CLUB: self.club,
            self.club: self.module_base + 0x2220,
            self.club + self.club_finance_offset: self.finance,
            self.finance + transfer_budget.FINANCE_CLUB_REFERENCE:
                self.reverse_reference,
        }.get(address, 0)

    def u32(self, address):
        return self.team_id if address == self.team + transfer_budget.ENTITY_UID else 0

    def bytes(self, address, size):
        if address == self.finance + self.budget_offset and size == 4:
            return struct.pack("<I", self.amount)
        return None


def exact_operation(reader):
    return SimpleNamespace(
        reader=reader, process=object(), pid=42, writable=True,
        layout=SimpleNamespace(
            team_vtable_rva=0x1110,
            club_vtable_rva=0x2220,
            club_finance_offset=reader.club_finance_offset,
            finance_remaining_transfer_budget_offset=reader.budget_offset,
        ),
    )


def test_force_scan_retries_a_cached_locator_failure(monkeypatch):
    cache_key = (42, 0x5000)
    transfer_budget._RECORD_CACHE.pop(cache_key, None)
    transfer_budget._SESSION_SCAN_FAILURES[cache_key] = "cached failure"
    scans = []
    monkeypatch.setattr(
        transfer_budget, "_scan_regions",
        lambda _process: (scans.append(True) or ([], set())),
    )

    try:
        with pytest.raises(RuntimeError, match="无法定位当前俱乐部的转会预算"):
            transfer_budget._signature_address(
                object(), object(), cache_key[0], cache_key[1], force_scan=True,
            )
        assert scans == [True]
    finally:
        transfer_budget._SESSION_SCAN_FAILURES.pop(cache_key, None)


def test_write_uses_layout_object_chain_and_never_scans(monkeypatch):
    reader = ExactBudgetReader()
    operation = exact_operation(reader)
    writes = []
    monkeypatch.setattr(transfer_budget, "database_index_for_reader", lambda _reader: None)
    monkeypatch.setattr(
        transfer_budget, "_signature_address",
        lambda *_args, **_kwargs: pytest.fail("写入路径不得扫描金额候选"),
    )

    def write(_process, address, data):
        writes.append((address, data))
        reader.amount = struct.unpack("<I", data)[0]

    monkeypatch.setattr(transfer_budget, "write_process_memory", write)

    result = transfer_budget.write_transfer_budget(
        reader.team, 26_000_000, team_id=reader.team_id,
        expected=25_000_000, operation=operation,
    )

    assert writes == [(
        reader.finance + reader.budget_offset,
        struct.pack("<I", 26_000_000),
    )]
    assert result["previous_amount"] == 25_000_000
    assert result["amount"] == 26_000_000


def test_exact_read_relocates_stale_team_address_through_object_directory(monkeypatch):
    reader = ExactBudgetReader(team=0x5000)
    operation = exact_operation(reader)

    class Directory:
        def addresses_for_uid(self, name, uid):
            assert (name, uid) == ("team", reader.team_id)
            return (reader.team,)

    monkeypatch.setattr(
        transfer_budget, "database_index_for_reader", lambda _reader: Directory(),
    )
    monkeypatch.setattr(
        transfer_budget, "_signature_address",
        lambda *_args, **_kwargs: pytest.fail("对象目录命中后不得扫描"),
    )

    result = transfer_budget.read_transfer_budget(
        0xDEAD, reader.team_id, allow_scan=False, operation=operation,
    )

    assert result["amount"] == 25_000_000
    assert result["address"] == hex(reader.finance + reader.budget_offset)


def test_exact_read_falls_back_to_valid_hint_when_directory_snapshot_is_stale(monkeypatch):
    reader = ExactBudgetReader(team=0x5000)
    operation = exact_operation(reader)

    class Directory:
        @staticmethod
        def addresses_for_uid(_name, _uid):
            return (0xDEAD,)  # stale table entry

    monkeypatch.setattr(
        transfer_budget, "database_index_for_reader", lambda _reader: Directory(),
    )
    monkeypatch.setattr(
        transfer_budget, "_signature_address",
        lambda *_args, **_kwargs: pytest.fail("有效 hint 不应触发堆扫描"),
    )

    result = transfer_budget.read_transfer_budget(
        reader.team, reader.team_id, allow_scan=False, operation=operation,
    )

    assert result["amount"] == 25_000_000


def test_write_rejects_invalid_finance_back_reference_without_scanning(monkeypatch):
    reader = ExactBudgetReader()
    reader.reverse_reference = 0x9999
    operation = exact_operation(reader)
    scans = []
    writes = []
    monkeypatch.setattr(transfer_budget, "database_index_for_reader", lambda _reader: None)
    monkeypatch.setattr(
        transfer_budget, "_signature_address",
        lambda *_args, **_kwargs: scans.append(True),
    )
    monkeypatch.setattr(
        transfer_budget, "write_process_memory",
        lambda *_args, **_kwargs: writes.append(True),
    )

    with pytest.raises(
        transfer_budget.TransferBudgetLocationError,
        match="无法从当前俱乐部对象取得财务记录",
    ):
        transfer_budget.write_transfer_budget(
            reader.team, 26_000_000, team_id=reader.team_id,
            expected=25_000_000, operation=operation,
        )

    assert scans == []
    assert writes == []


def test_write_rejects_team_uid_mismatch_before_memory_write(monkeypatch):
    reader = ExactBudgetReader(team_id=7)
    operation = exact_operation(reader)
    writes = []
    monkeypatch.setattr(transfer_budget, "database_index_for_reader", lambda _reader: None)
    monkeypatch.setattr(
        transfer_budget, "write_process_memory",
        lambda *_args, **_kwargs: writes.append(True),
    )

    with pytest.raises(
        transfer_budget.TransferBudgetLocationError,
        match="无法按俱乐部 ID 取得当前俱乐部对象",
    ):
        transfer_budget.write_transfer_budget(
            reader.team, 26_000_000, team_id=1190,
            expected=25_000_000, operation=operation,
        )

    assert writes == []


def test_write_transfer_budget_rejects_an_ignored_memory_write(monkeypatch):
    address = 0x7000
    current = 25_000_000

    class FakeReader:
        def bytes(self, target, size):
            assert (target, size) == (address, 4)
            return struct.pack("<I", current)

    layout = SimpleNamespace(
        module=lambda _process: SimpleNamespace(base_address=0x100000),
    )
    operation = SimpleNamespace(
        reader=FakeReader(), process=object(), pid=42,
        layout=layout, writable=True,
    )
    writes = []
    monkeypatch.setattr(
        transfer_budget, "_locate",
        lambda *_args, **_kwargs: (address, current),
    )
    monkeypatch.setattr(
        transfer_budget, "write_process_memory",
        lambda _process, target, data: writes.append((target, data)),
    )

    with pytest.raises(RuntimeError, match="写入后校验失败，已恢复原值"):
        transfer_budget.write_transfer_budget(
            0x1000, current + 1_000_000, team_id=1190, expected=current,
            operation=operation,
        )

    assert writes == [
        (address, struct.pack("<I", current + 1_000_000)),
        (address, struct.pack("<I", current + 1_000_000)),
    ]
