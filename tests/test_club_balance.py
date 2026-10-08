from __future__ import annotations

import struct
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from tools import club_balance


class FakeReader:
    def __init__(
        self,
        balance: int = 28_881_600,
        *,
        club_finance_offset: int = club_balance.CLUB_CURRENT_FINANCE,
        finance_balance_offset: int = club_balance.CLUB_BALANCE,
    ):
        self.team = 0x100000
        self.club = 0x200000
        self.finance = 0x300000
        self.balance = balance
        self.club_finance_offset = club_finance_offset
        self.finance_balance_offset = finance_balance_offset

    def u32(self, address: int):
        return 1190 if address == self.team + club_balance.ENTITY_UID else 0

    def ptr(self, address: int):
        return {
            self.team + 0x30: self.club,
            self.club + self.club_finance_offset: self.finance,
            self.finance + club_balance.FINANCE_CLUB_REFERENCE: self.club,
        }.get(address, 0)

    def bytes(self, address: int, size: int):
        if address == self.finance + self.finance_balance_offset and size == 4:
            return struct.pack("<i", self.balance)
        return None


class ClubBalanceTests(unittest.TestCase):
    def test_locates_balance_through_verified_finance_record(self):
        reader = FakeReader(288_818_880)

        address, amount = club_balance._locate(reader, reader.team, 1190)

        self.assertEqual(address, reader.finance + 0x14)
        self.assertEqual(amount, 288_818_880)

    def test_locates_balance_from_native_index_when_hint_is_stale(self):
        reader = FakeReader(288_818_880)

        class Directory:
            @staticmethod
            def addresses_for_uid(name, uid):
                self.assertEqual((name, uid), ("team", 1190))
                return (reader.team,)

        with patch(
            "tools.database_index.database_index_for_reader",
            return_value=Directory(),
        ):
            address, amount = club_balance._locate(reader, 0xDEAD, 1190)

        self.assertEqual(address, reader.finance + 0x14)
        self.assertEqual(amount, 288_818_880)

    def test_rejects_finance_record_for_another_club(self):
        reader = FakeReader()
        original_ptr = reader.ptr
        reader.ptr = lambda address: 0xDEADBEEF if address == reader.finance + 0x08 else original_ptr(address)

        with self.assertRaisesRegex(RuntimeError, "无法定位当前俱乐部的财务记录"):
            club_balance._locate(reader, reader.team, 1190)

    def test_write_checks_expected_value_and_reads_back(self):
        reader = FakeReader()
        layout = SimpleNamespace(module=lambda _process: SimpleNamespace(base_address=0x500000))
        operation = SimpleNamespace(
            reader=reader, process=object(), layout=layout, writable=True,
        )

        def write(_process, address, data, **kwargs):
            self.assertEqual(address, reader.finance + 0x14)
            self.assertTrue(kwargs["compatibility_fallback"])
            reader.balance = struct.unpack("<i", data)[0]

        with (
            patch.object(club_balance, "write_process_memory", side_effect=write),
        ):
            result = club_balance.write_club_balance(
                reader.team, 30_000_000, team_id=1190, expected=28_881_600,
                operation=operation,
            )

        self.assertEqual(result["previous_amount"], 28_881_600)
        self.assertEqual(result["amount"], 30_000_000)

    def test_write_uses_version_layout_finance_offsets(self):
        reader = FakeReader(
            club_finance_offset=0x1A0,
            finance_balance_offset=0x24,
        )
        layout = SimpleNamespace(
            module=lambda _process: SimpleNamespace(base_address=0x500000),
            club_finance_offset=0x1A0,
            finance_balance_offset=0x24,
        )
        operation = SimpleNamespace(
            reader=reader, process=object(), layout=layout, writable=True,
        )

        def write(_process, address, data, **kwargs):
            self.assertEqual(address, reader.finance + 0x24)
            self.assertTrue(kwargs["compatibility_fallback"])
            reader.balance = struct.unpack("<i", data)[0]

        with (
            patch.object(club_balance, "write_process_memory", side_effect=write),
        ):
            result = club_balance.write_club_balance(
                reader.team, 30_000_000, team_id=1190, expected=28_881_600,
                operation=operation,
            )

        self.assertEqual(result["amount"], 30_000_000)

    def test_write_allows_negative_balance_to_be_increased_while_still_negative(self):
        reader = FakeReader(-30_000_000)
        layout = SimpleNamespace(module=lambda _process: SimpleNamespace(base_address=0x500000))
        operation = SimpleNamespace(
            reader=reader, process=object(), layout=layout, writable=True,
        )

        def write(_process, address, data, **kwargs):
            self.assertEqual(address, reader.finance + 0x14)
            self.assertTrue(kwargs["compatibility_fallback"])
            reader.balance = struct.unpack("<i", data)[0]

        with (
            patch.object(club_balance, "write_process_memory", side_effect=write),
        ):
            result = club_balance.write_club_balance(
                reader.team, -20_000_000, team_id=1190, expected=-30_000_000,
                operation=operation,
            )

        self.assertEqual(result["previous_amount"], -30_000_000)
        self.assertEqual(result["amount"], -20_000_000)

    def test_write_retries_once_when_game_keeps_the_expected_old_balance(self):
        reader = FakeReader()
        layout = SimpleNamespace(module=lambda _process: SimpleNamespace(base_address=0x500000))
        operation = SimpleNamespace(
            reader=reader, process=object(), layout=layout, writable=True,
        )
        writes = []

        def write(_process, address, data, **kwargs):
            self.assertTrue(kwargs["compatibility_fallback"])
            writes.append((address, data))

        with (
            patch.object(club_balance, "write_process_memory", side_effect=write),
        ):
            with self.assertRaisesRegex(RuntimeError, "写入后校验失败，已恢复原值"):
                club_balance.write_club_balance(
                    reader.team, 30_000_000, team_id=1190, expected=28_881_600,
                    operation=operation,
                )

        self.assertEqual(len(writes), 2)


if __name__ == "__main__":
    unittest.main()
