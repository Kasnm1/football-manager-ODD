from __future__ import annotations

import struct
import unittest
from types import SimpleNamespace

from tools.player_details_fm24 import (
    _loan_details,
    read_fm24_asking_price, read_fm24_current_season_stats,
    read_fm24_career_stats, read_fm24_extended_player,
    read_fm24_international_stats,
)


def fm_date(year: int, day: int) -> int:
    return (year << 16) | day


class FakeReader:
    def __init__(self) -> None:
        self.memory: dict[int, bytes] = {}
        self.strings: dict[int, str] = {}
        self.teams: dict[int, dict] = {}
        self.byte_calls: list[tuple[int, int]] = []

    def put(self, address: int, raw: bytes) -> None:
        self.memory[address] = raw

    def bytes(self, address: int, size: int):
        self.byte_calls.append((address, size))
        for base, raw in self.memory.items():
            if base <= address and address + size <= base + len(raw):
                start = address - base
                return raw[start:start + size]
        return None

    def ptr(self, address: int):
        raw = self.bytes(address, 8)
        return struct.unpack("<Q", raw)[0] if raw else None

    def u32(self, address: int):
        raw = self.bytes(address, 4)
        return struct.unpack("<I", raw)[0] if raw else None

    def u8(self, address: int):
        raw = self.bytes(address, 1)
        return raw[0] if raw else None

    def fm_string_at(self, address: int):
        return self.strings.get(address)

    def team(self, address: int):
        return self.teams.get(address)


class Fm24ExtendedPlayerTests(unittest.TestCase):
    def test_reads_loan_contract_squad_number(self) -> None:
        reader = FakeReader()
        reader.module_base = 0x100000
        reader.layout = SimpleNamespace(loan_contract_vtable_rva=0x777)
        person, current_team, loan_team, other, loan = 0x2000, 0x3000, 0x4000, 0x5000, 0x6000
        reader.put(person + 0xD0, struct.pack("<Q", other))
        reader.put(other, struct.pack("<Q", loan))
        raw = bytearray(0xC0)
        struct.pack_into("<Q", raw, 0x00, reader.module_base + 0x777)
        struct.pack_into("<Q", raw, 0x08, person)
        struct.pack_into("<Q", raw, 0x10, loan_team)
        raw[0x55] = 27
        reader.put(loan, bytes(raw))
        reader.teams[loan_team] = {"id": 10, "name": "租借俱乐部"}
        self.assertEqual(_loan_details(reader, person, current_team)["squad_number"], 27)

    def test_reads_profiled_international_counters(self) -> None:
        reader = FakeReader()
        reader.layout = SimpleNamespace(
            player_international_appearances_offset=0x3DC,
            player_international_goals_offset=0x3DE,
            player_u21_international_appearances_offset=0x3DD,
            player_u21_international_goals_offset=0x3DF,
            player_international_counter_width=1,
            player_u21_international_counter_width=1,
        )
        player = 0x1000
        raw = bytearray(0x400)
        raw[0x3DC:0x3E0] = bytes((42, 18, 7, 3))
        reader.put(player, bytes(raw))
        self.assertEqual(read_fm24_international_stats(reader, player), {
            "source": "fm24_native_international_counters",
            "international_appearances": 42,
            "international_goals": 7,
            "u21_international_appearances": 18,
            "u21_international_goals": 3,
        })

    def test_reads_verified_current_season_and_career_stats(self) -> None:
        reader = FakeReader()
        reader.module_base = 0x100000
        reader.layout = SimpleNamespace(
            player_season_stats_root_offset=0x108,
            player_season_stats_total_slot_offset=0x48,
            player_season_stats_block_size=0x78,
            player_career_stats_offset=0x110,
            actual_player_vtable_rvas=(0x500,),
            player_and_non_player_vtable_rvas=(0x600,),
            player_person_offset=0x278,
            player_and_non_player_person_offset=0x368,
        )
        player, person, root, slot, detail, career = (0x1000, 0x1278, 0x3000, 0x4000, 0x5000, 0x6000)
        player_raw = bytearray(0x200)
        struct.pack_into("<Q", player_raw, 0x00, reader.module_base + 0x500)
        struct.pack_into("<Q", player_raw, 0x108, root)
        struct.pack_into("<Q", player_raw, 0x110, career)
        reader.put(player, bytes(player_raw))
        person_raw = bytearray(0x100)
        struct.pack_into("<I", person_raw, 0x0C, 12345)
        reader.put(person, bytes(person_raw))
        root_raw = bytearray(0x60)
        struct.pack_into("<Q", root_raw, 0x48, slot)
        reader.put(root, bytes(root_raw))
        slot_raw = bytearray(0x20)
        struct.pack_into("<Q", slot_raw, 0x00, detail)
        struct.pack_into("<H", slot_raw, 0x08, 700)
        struct.pack_into("<H", slot_raw, 0x0A, 900)
        slot_raw[0x0C:0x18] = bytes((8, 2, 7, 3, 4, 1, 2, 0, 1, 0, 5, 0))
        reader.put(slot, bytes(slot_raw))
        detail_raw = bytearray(0x78)
        for offset, value in ((0x08, 100), (0x0A, 90), (0x36, 12), (0x48, 250)):
            struct.pack_into("<H", detail_raw, offset, value)
        reader.put(detail, bytes(detail_raw))
        career_raw = bytearray(120)
        struct.pack_into("<H", career_raw, 3 * 2, 34)
        struct.pack_into("<H", career_raw, 4 * 2, 37)
        struct.pack_into("<H", career_raw, 5 * 2, 207)
        struct.pack_into("<H", career_raw, 7 * 2, 198)
        reader.put(career, bytes(career_raw))

        season = read_fm24_current_season_stats(reader, player, expected_uid=12345)
        self.assertEqual(season["appearances"], 10)
        self.assertEqual(season["goals"], 3)
        self.assertEqual(season["assists"], 4)
        self.assertEqual(season["key_passes"], 12)
        self.assertEqual(season["xg"], 2.5)
        self.assertEqual(read_fm24_career_stats(reader, player), {
            "source": "fm24_native_career_stats",
            "all_time_league_goals": 34,
            "all_time_goals": 37,
            "all_time_appearances": 207,
            "all_time_league_appearances": 198,
        })

    def test_unset_asking_price_sentinels_are_zero(self) -> None:
        for sentinel in (300_000_000, 0xFFFFFFFF):
            with self.subTest(sentinel=sentinel):
                reader = FakeReader()
                player = 0x1000
                reader.put(player + 0x1D0, struct.pack("<I", sentinel))
                self.assertEqual(read_fm24_asking_price(reader, player), 0)

    def test_reads_contract_person_and_team_status(self) -> None:
        reader = FakeReader()
        player, person, team, contract = 0x1000, 0x2000, 0x3000, 0x4000
        bonus_data, language_ptrs, language_record = 0x5000, 0x5100, 0x5200
        language, info, relation_data, city, nation = 0x5300, 0x5400, 0x5500, 0x5600, 0x5700

        player_raw = bytearray(0x400)
        struct.pack_into("<I", player_raw, 0x1D0, 228_484_832)
        player_raw[0x1A7] = 83
        reader.put(player, bytes(player_raw))

        person_raw = bytearray(0x160)
        struct.pack_into("<Q", person_raw, 0x80, info)
        struct.pack_into("<Q", person_raw, 0x88, city)
        struct.pack_into("<Q", person_raw, 0xC8, contract)
        struct.pack_into("<Q", person_raw, 0xF0, (1 << 13) | (1 << 41))
        struct.pack_into("<I", person_raw, 0x14C, fm_date(2023, 214))
        struct.pack_into("<QQQ", person_raw, 0x120, language_ptrs, language_ptrs + 8, language_ptrs + 8)
        reader.put(person, bytes(person_raw))
        reader.strings[person + 0x48] = "Erling Braut Haaland"
        reader.strings[city + 0x18] = "利兹"

        contract_raw = bytearray(0xC0)
        struct.pack_into("<Q", contract_raw, 0x08, person)
        struct.pack_into("<Q", contract_raw, 0x10, team)
        struct.pack_into("<I", contract_raw, 0x18, 340_000)
        struct.pack_into("<I", contract_raw, 0x30, 37_130)
        struct.pack_into("<I", contract_raw, 0x3C, fm_date(2022, 182))
        struct.pack_into("<I", contract_raw, 0x40, fm_date(2027, 181))
        contract_raw[0x4C] = 1
        contract_raw[0x4F] = 64
        contract_raw[0x55] = 9
        struct.pack_into("<QQQ", contract_raw, 0x60, bonus_data, bonus_data + 24, bonus_data + 24)
        contract_raw[0xB3] = 1
        reader.put(contract, bytes(contract_raw))
        reader.put(bonus_data, b"".join((
            struct.pack("<ihh", 40_000, -1, 32),
            struct.pack("<ihh", 50_000, -1, 33),
            struct.pack("<ihh", 17_000, -1, 38),
        )))
        reader.teams[team] = {"id": 679, "name": "曼城"}

        reader.put(language_ptrs, struct.pack("<Q", language_record))
        reader.put(language_record, struct.pack("<QB7x", language, 10))
        reader.strings[language + 0x18] = "挪\u200b威语"

        reader.put(info, struct.pack("<QQQ", relation_data, relation_data + 32, relation_data + 32))
        reader.put(relation_data, b"".join((
            struct.pack("<Q", nation) + bytes((0, 0, 8, 9, 100, 80, 0, 255)),
            struct.pack("<Q", team) + bytes((0, 0, 4, 25, 8, 0, 0, 255)),
        )))
        reader.put(nation, b"\0" * 12 + struct.pack("<I", 765))

        result = read_fm24_extended_player(reader, player, person, team, None)
        self.assertEqual(result["city_of_birth"], "利兹")
        self.assertEqual(result["languages"], [{"name": "挪威语", "proficiency": 10, "maximum": 10}])
        self.assertEqual(result["second_nationality"]["name"], "英格兰")
        self.assertEqual(result["registrations"][0]["type"], 8)
        self.assertTrue(result["declared_for_nation"])
        self.assertEqual(result["player_contract"]["wage_per_week"], 340_000)
        self.assertEqual(result["player_contract"]["contract_type"], 1)
        self.assertEqual(result["player_contract"]["appearance_fee"], 40_000)
        self.assertEqual(result["player_contract"]["goal_bonus"], 50_000)
        self.assertEqual(result["player_contract"]["squad_number"], 9)
        self.assertEqual(result["player_contract"]["joined_club_date"], "2023-08-02")
        self.assertIn((contract, 0xC0), reader.byte_calls)
        self.assertNotIn((contract + 0x08, 8), reader.byte_calls)
        self.assertNotIn((contract + 0x60, 24), reader.byte_calls)
        self.assertTrue(result["transfer"]["not_for_loan"])
        self.assertEqual([row["bit"] for row in result["preferred_moves"]], [13, 41])

    def test_rejects_contract_owned_by_another_person(self) -> None:
        reader = FakeReader()
        player, person, contract = 0x1000, 0x2000, 0x4000
        reader.put(player, b"\0" * 0x400)
        person_raw = bytearray(0x160)
        struct.pack_into("<Q", person_raw, 0xC8, contract)
        reader.put(person, bytes(person_raw))
        contract_raw = bytearray(0xC0)
        struct.pack_into("<Q", contract_raw, 0x08, 0x9999)
        reader.put(contract, bytes(contract_raw))

        result = read_fm24_extended_player(reader, player, person, 0x3000, None)
        self.assertIsNone(result["player_contract"])
        self.assertTrue(result["transfer"]["available_on_free"])


if __name__ == "__main__":
    unittest.main()
