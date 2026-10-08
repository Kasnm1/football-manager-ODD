from __future__ import annotations

import struct
import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

from tools.league_table_memory import _valid_row, update_league_points


class LeagueTableMemoryTests(unittest.TestCase):
    def test_validates_verified_fm24_statistics_shape(self) -> None:
        row = {
            "goals_for": 13, "goals_against": 5, "points": 16,
            "played": 8, "won": 4, "drawn": 4, "lost": 0,
        }
        data = struct.pack("<HHhBBBBBB", 13, 5, 16, 8, 8, 4, 4, 0, 0)
        self.assertTrue(_valid_row(data, row))

    def test_rejects_statistics_from_another_team(self) -> None:
        row = {
            "goals_for": 13, "goals_against": 5, "points": 16,
            "played": 8, "won": 4, "drawn": 4, "lost": 0,
        }
        data = struct.pack("<HHhBBBBBB", 14, 5, 16, 8, 8, 4, 4, 0, 0)
        self.assertFalse(_valid_row(data, row))

    def test_updates_only_current_verified_standing_points(self) -> None:
        standing = 0x3000
        team_address = 0x4000
        current = struct.pack("<HHh6B", 13, 5, 16, 8, 8, 4, 4, 0, 0)
        previous = struct.pack("<HHh6B", 12, 5, 13, 7, 7, 3, 4, 0, 0)
        memory = {
            standing + 0x08: current,
            standing + 0x50: previous,
            standing + 0x0C: struct.pack("<h", 16),
            standing + 0x54: struct.pack("<h", 13),
        }

        class Reader:
            layout = SimpleNamespace(key="fm24")

            @staticmethod
            def ptr(address):
                return team_address if address == standing + 0x78 else None

            @staticmethod
            def team(address):
                return {"id": 10} if address == team_address else None

        @contextmanager
        def opened(write=False):
            self.assertTrue(write)
            yield SimpleNamespace(), Reader()

        def read(_process, address, size):
            value = memory.get(address)
            return value[:size] if value else None

        def write(_process, address, value):
            memory[address] = value

        row = {
            "team_id": 10, "native_standing_address": hex(standing),
            "goals_for": 13, "goals_against": 5, "points": 16,
            "played": 8, "won": 4, "drawn": 4, "lost": 0,
        }
        with patch("tools.league_table_memory._open_supported_reader", opened), patch(
            "tools.league_table_memory.read_process_memory", side_effect=read,
        ), patch(
            "tools.league_table_memory.write_process_memory", side_effect=write,
        ) as write_mock:
            result = update_league_points(hex(team_address), row, -3)

        self.assertEqual(result["before"], 16)
        self.assertEqual(result["after"], 13)
        self.assertEqual(memory[standing + 0x0C], struct.pack("<h", 13))
        self.assertEqual(memory[standing + 0x54], struct.pack("<h", 13))
        self.assertEqual(
            [call.args[1] for call in write_mock.call_args_list],
            [standing + 0x0C],
        )

    def test_rejects_write_without_verified_standing_address(self) -> None:
        @contextmanager
        def opened(write=False):
            yield SimpleNamespace(), SimpleNamespace(layout=SimpleNamespace(key="fm26"))

        with patch("tools.league_table_memory._open_supported_reader", opened):
            with self.assertRaisesRegex(RuntimeError, "唯一 Standing 地址"):
                update_league_points("0x4000", {"team_id": 10}, -3)


if __name__ == "__main__":
    unittest.main()
