from __future__ import annotations

from datetime import date
import struct
from types import SimpleNamespace
import unittest

from tools.player_details_fm26 import (
    _loan_details,
    read_fm26_asking_price,
    read_fm26_current_season_stats,
    read_fm26_career_stats,
    read_fm26_international_stats,
    read_fm26_extended_player,
)


def fm_date(year: int, day: int) -> int:
    return (year << 16) | day


class FakeReader:
    def __init__(self) -> None:
        self.module_base = 0x100000
        self.memory: dict[int, bytes] = {}
        self.strings: dict[int, str] = {}
        self.nested_strings: dict[int, str] = {}
        self.byte_calls: list[tuple[int, int]] = []

    def put(self, address: int, value: bytes) -> None:
        self.memory[address] = value

    def _read(self, address: int, size: int) -> bytes | None:
        for base, raw in self.memory.items():
            offset = address - base
            if 0 <= offset and offset + size <= len(raw):
                return raw[offset:offset + size]
        return None

    def bytes(self, address: int, size: int) -> bytes | None:
        self.byte_calls.append((address, size))
        return self._read(address, size)

    def ptr(self, address: int) -> int:
        raw = self._read(address, 8)
        return struct.unpack("<Q", raw)[0] if raw else 0

    def u32(self, address: int) -> int | None:
        raw = self._read(address, 4)
        return struct.unpack("<I", raw)[0] if raw else None

    def u8(self, address: int) -> int | None:
        raw = self._read(address, 1)
        return raw[0] if raw else None

    def fm_string_at(self, address: int) -> str | None:
        return self.strings.get(address)

    def fm_nested_string_at(self, address: int) -> str | None:
        return self.nested_strings.get(address)

    def team(self, address: int) -> dict[str, object] | None:
        return {"id": 1190, "name": "柏太阳神"} if address == 0x4000 else None


class PlayerDetailsFm26Tests(unittest.TestCase):
    def test_reads_loan_contract_squad_number(self):
        reader = FakeReader()
        reader.layout = SimpleNamespace(loan_contract_vtable_rva=0x777)
        person, loan_team, other, loan = 0x2000, 0x4000, 0x5000, 0x6000
        reader.put(person + 0xB0, struct.pack("<Q", other))
        reader.put(other, struct.pack("<Q", loan))
        raw = bytearray(0xC8)
        struct.pack_into("<Q", raw, 0x00, reader.module_base + 0x777)
        struct.pack_into("<Q", raw, 0x08, person)
        struct.pack_into("<Q", raw, 0x10, loan_team)
        raw[0x5D] = 46
        reader.put(loan, bytes(raw))
        self.assertEqual(_loan_details(reader, person, 0x3000)["squad_number"], 46)

    def test_reads_profiled_international_counters(self):
        reader = FakeReader()
        reader.layout = SimpleNamespace(
            player_international_appearances_offset=0x3BC,
            player_international_goals_offset=0x3BE,
            player_u21_international_appearances_offset=0x3C0,
            player_u21_international_goals_offset=0x3C1,
            player_international_counter_width=2,
            player_u21_international_counter_width=1,
        )
        raw = bytearray(0x3C2)
        struct.pack_into("<HH", raw, 0x3BC, 88, 16)
        raw[0x3C0:0x3C2] = bytes((21, 5))
        reader.put(0x1000, bytes(raw))
        self.assertEqual(read_fm26_international_stats(reader, 0x1000), {
            "source": "fm26_native_international_counters",
            "international_appearances": 88,
            "international_goals": 16,
            "u21_international_appearances": 21,
            "u21_international_goals": 5,
        })

    def setUp(self) -> None:
        self.reader = FakeReader()
        self.player = 0x1000
        self.person = 0x2000
        self.contract = 0x3000
        self.team = 0x4000
        self.primary_nation = 0x6000
        self.second_nation = 0x7000

        self.reader.put(self.person + 0xA8, struct.pack("<Q", self.contract))
        self.reader.put(self.person + 0x68, struct.pack("<Q", self.primary_nation))
        self.reader.put(self.person + 0x78, struct.pack("<Q", 0x5000))
        self.reader.put(self.person + 0x80, struct.pack("<Q", 0x8000))
        self.reader.put(self.person + 0xC0, struct.pack("<Q", (1 << 13) | (1 << 51)))
        self.reader.put(self.person + 0x11C, struct.pack("<I", fm_date(2026, 42)))
        self.reader.put(self.person + 0xF0, struct.pack("<QQQ", 0x9000, 0x9008, 0x9008))
        self.reader.put(0x9000, struct.pack("<Q", 0x9100))
        self.reader.put(0x9100, struct.pack("<QB7x", 0x9200, 10))
        self.reader.strings[0x9200 + 0x18] = "英\u200b语"

        relations = (
            struct.pack("<Q", self.second_nation) + b"\0\0\x08\x46\x01\0\0\xff"
            + struct.pack("<Q", self.team) + b"\0\0\x04\x19\0\0\0\xff"
        )
        self.reader.put(0x5000, struct.pack("<QQQ", 0x5100, 0x5120, 0x5120))
        self.reader.put(0x5100, relations)
        self.reader.put(self.second_nation + 0x0C, struct.pack("<I", 771))

        contract_raw = bytearray(0xC8)
        struct.pack_into("<Q", contract_raw, 0x08, self.person)
        struct.pack_into("<Q", contract_raw, 0x10, self.team)
        struct.pack_into("<I", contract_raw, 0x20, 47_800)
        struct.pack_into("<I", contract_raw, 0x38, 29_061)
        struct.pack_into("<I", contract_raw, 0x44, fm_date(2028, 225))
        struct.pack_into("<I", contract_raw, 0x48, fm_date(2029, 181))
        struct.pack_into("<I", contract_raw, 0x4C, fm_date(2028, 225))
        contract_raw[0x54] = 1
        contract_raw[0x5A] = 98
        contract_raw[0x5C] = 60
        contract_raw[0x5D] = 1
        contract_raw[0xC3] = 1
        struct.pack_into("<QQQ", contract_raw, 0x68, 0xA000, 0xA010, 0xA010)
        self.reader.put(self.contract, bytes(contract_raw))
        self.reader.put(0xA000, struct.pack("<ihhihh", 8_897, -1, 32, 17_794, -1, 34))
        self.reader.put(self.player + 0x234, struct.pack("<I", 29_073_664))
        self.reader.put(self.player + 0x26E, bytes([83]))

        self.reader.strings[self.person + 0x40] = "乔丹·李·皮克福德"
        self.reader.nested_strings[self.person + 0x50] = "乔丹"
        self.reader.nested_strings[self.person + 0x58] = "皮克福德"
        self.reader.nested_strings[self.person + 0x60] = "乔丹·皮克福德"
        self.reader.strings[0x8000 + 0x18] = "桑德兰"

    def test_verified_pickford_contract_and_person_fields(self):
        catalog = tuple(f"习惯 {index}" for index in range(64))
        result = read_fm26_extended_player(
            self.reader, self.player, self.person, self.team,
            date(2028, 8, 20), catalog,
        )

        self.assertEqual(result["shirt_number"], 1)
        self.assertEqual(result["asking_price"], 29_073_664)
        self.assertEqual(result["full_name"], "乔丹·李·皮克福德")
        self.assertEqual(result["city_of_birth"], "桑德兰")
        self.assertEqual(result["second_nationality"]["name"], "德国")
        self.assertEqual(result["languages"], [{"name": "英语", "proficiency": 10, "maximum": 10}])
        self.assertEqual(result["registrations"][0]["type"], 0)
        self.assertEqual(result["preferred_moves"][0]["name"], "喜欢反越位")
        self.assertEqual(result["preferred_moves"][1]["name"], "经常带球")

        contract = result["player_contract"]
        self.assertEqual(contract["wage_per_week"], 47_800)
        self.assertEqual(contract["expiry_date"], "2029-06-30")
        self.assertEqual(contract["appearance_fee"], 8_897)
        self.assertEqual(contract["clean_sheet_bonus"], 17_794)
        self.assertEqual(contract["squad_number"], 1)
        self.assertEqual(contract["happiness"], 98)
        self.assertEqual(contract["playing_time_happiness"], 60)
        self.assertEqual(contract["contract_type"], 1)
        self.assertEqual(contract["joined_club_date"], "2026-02-11")
        self.assertIn((self.contract, 0xC8), self.reader.byte_calls)
        self.assertNotIn((self.contract + 0x08, 8), self.reader.byte_calls)
        self.assertNotIn((self.contract + 0x68, 24), self.reader.byte_calls)
        self.assertNotIn((self.contract + 0x20, 4), self.reader.byte_calls)
        self.assertNotIn((self.contract + 0x54, 1), self.reader.byte_calls)

    def test_unset_asking_price_sentinels_are_zero(self):
        for sentinel in (300_000_000, 0xFFFFFFFF):
            with self.subTest(sentinel=sentinel):
                self.reader.put(self.player + 0x234, struct.pack("<I", sentinel))
                self.assertEqual(read_fm26_asking_price(self.reader, self.player), 0)

    def test_reads_verified_current_season_total(self):
        self.reader.layout = SimpleNamespace(
            key="fm26",
            actual_player_vtable_rvas=(0x1234,),
            player_and_non_player_vtable_rvas=(0x5678,),
            player_person_offset=0x288,
            player_and_non_player_person_offset=0x380,
            player_season_stats_root_offset=0x108,
            player_season_stats_total_slot_offset=0x48,
            player_season_stats_block_size=0x78,
        )
        self.reader.put(self.player, struct.pack("<Q", self.reader.module_base + 0x1234))
        self.reader.put(self.player + 0x288 + 0x0C, struct.pack("<I", 777))
        self.reader.put(self.player + 0x108, struct.pack("<Q", 0xB000))
        self.reader.put(0xB048, struct.pack("<Q", 0xB100))
        slot = bytearray(0x20)
        struct.pack_into("<QHH", slot, 0, 0xB200, 2972, 3600)
        slot[0x0C:0x18] = bytes((39, 2, 40, 33, 6, 0, 0, 0, 8, 0, 4, 0))
        self.reader.put(0xB100, bytes(slot))
        detail = bytearray(0x78)
        struct.pack_into("<HH", detail, 0x08, 1165, 1019)
        struct.pack_into("<HH", detail, 0x1A, 121, 68)
        struct.pack_into("<HH", detail, 0x48, 2739, 633)
        struct.pack_into("<h", detail, 0x6E, 561)
        self.reader.put(0xB200, bytes(detail))

        result = read_fm26_current_season_stats(
            self.reader, self.player, expected_uid=777,
        )

        self.assertEqual(result["appearances"], 41)
        self.assertEqual(result["rated_appearances"], 40)
        self.assertEqual(result["goals"], 33)
        self.assertEqual(result["assists"], 6)
        self.assertAlmostEqual(result["average_rating"], 7.43)
        self.assertEqual(result["xg"], 27.39)
        self.assertEqual(result["xa"], 6.33)
        self.assertEqual(result["pass_completion_pct"], 87.47)
        self.assertEqual(result["xg_overperformance"], 5.61)

    def test_reads_verified_career_stats(self):
        self.reader.layout = SimpleNamespace(player_career_stats_offset=0x110)
        career = bytearray(120)
        for index, value in ((3, 34), (4, 37), (5, 207), (7, 198)):
            struct.pack_into("<H", career, index * 2, value)
        self.reader.put(self.player + 0x110, struct.pack("<Q", 0xC000))
        self.reader.put(0xC000, bytes(career))
        self.assertEqual(read_fm26_career_stats(self.reader, self.player), {
            "source": "fm26_native_career_stats",
            "all_time_league_goals": 34,
            "all_time_goals": 37,
            "all_time_appearances": 207,
            "all_time_league_appearances": 198,
        })

    def test_rejects_season_stats_when_uid_or_total_slot_mismatches(self):
        self.reader.layout = SimpleNamespace(
            key="fm26",
            actual_player_vtable_rvas=(0x1234,),
            player_and_non_player_vtable_rvas=(0x5678,),
            player_person_offset=0x288,
            player_and_non_player_person_offset=0x380,
            player_season_stats_root_offset=0x108,
            player_season_stats_total_slot_offset=0x48,
            player_season_stats_block_size=0x78,
        )
        self.reader.put(self.player, struct.pack("<Q", self.reader.module_base + 0x1234))
        self.reader.put(self.player + 0x288 + 0x0C, struct.pack("<I", 777))
        self.reader.put(self.player + 0x108, struct.pack("<Q", 0xB000))
        self.reader.put(0xB048, struct.pack("<Q", 0))

        self.assertIsNone(read_fm26_current_season_stats(
            self.reader, self.player, expected_uid=778,
        ))
        self.assertIsNone(read_fm26_current_season_stats(
            self.reader, self.player, expected_uid=777,
        ))

    def test_reads_player_and_non_player_embedded_person_uid(self):
        self.reader.layout = SimpleNamespace(
            key="fm26",
            actual_player_vtable_rvas=(0x1234,),
            player_and_non_player_vtable_rvas=(0x5678,),
            player_person_offset=0x288,
            player_and_non_player_person_offset=0x380,
            player_season_stats_root_offset=0x108,
            player_season_stats_total_slot_offset=0x48,
            player_season_stats_block_size=0x78,
        )
        self.reader.put(self.player, struct.pack("<Q", self.reader.module_base + 0x5678))
        self.reader.put(self.player + 0x380 + 0x0C, struct.pack("<I", 888))
        self.reader.put(self.player + 0x108, struct.pack("<Q", 0xB000))
        self.reader.put(0xB048, struct.pack("<Q", 0xB100))
        slot = bytearray(0x20)
        struct.pack_into("<QHH", slot, 0, 0xB200, 66, 36)
        slot[0x0C:0x18] = bytes((0, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0))
        self.reader.put(0xB100, bytes(slot))
        self.reader.put(0xB200, bytes(0x78))

        result = read_fm26_current_season_stats(
            self.reader, self.player, expected_uid=888,
        )

        self.assertEqual(result["appearances"], 1)
        self.assertAlmostEqual(result["average_rating"], 6.6)


if __name__ == "__main__":
    unittest.main()
