from __future__ import annotations

import struct
import unittest
from contextlib import ExitStack, nullcontext
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tools import club_reader


class _FakeReader:
    def __init__(self, layout, *, goalkeepers: bool = False):
        self.layout = layout
        self.address = 0x1000
        self.attributes = bytes([20] * 54)
        self.sharpness = 6400
        self.fitness = 7300
        self.goalkeepers = goalkeepers

    def roster(self, _team_address):
        return [{"id": 42, "name": "Player", "address": hex(self.address)}]

    def team(self, _team_address):
        return {"name": "Team"}

    def bytes(self, address, size):
        if address == self.address + self.layout.player_attributes_offset and size == 54:
            return self.attributes
        return None

    def u16(self, address):
        if address == self.address + self.layout.player_sharpness_offset:
            return self.sharpness
        if address == self.address + self.layout.player_fitness_offset:
            return self.fitness
        return None


class MatchItemPlayerStateTests(unittest.TestCase):
    def setUp(self):
        self.layout = SimpleNamespace(
            key="fm26",
            module_name="game_plugin.dll",
            module=lambda _process: SimpleNamespace(base_address=0x50000000),
            player_attributes_offset=0x15F,
            player_sharpness_offset=0x258,
            player_fitness_offset=0x25C,
        )

    def _patch_memory(self, reader):
        reader.process = SimpleNamespace(pid=1)
        module = SimpleNamespace(base_address=0x50000000)
        reader.module = module

        def write(_process, address, data):
            if address == reader.address + self.layout.player_attributes_offset:
                reader.attributes = data
            elif address == reader.address + self.layout.player_sharpness_offset:
                reader.sharpness = struct.unpack("<H", data)[0]
            elif address == reader.address + self.layout.player_fitness_offset:
                reader.fitness = struct.unpack("<H", data)[0]

        return (
            patch.object(
                club_reader, "_writable_game_reader",
                return_value=nullcontext((reader, object(), module)),
            ),
            patch.object(club_reader, "_resolve_team_address", return_value=0x2000),
            patch.object(club_reader, "write_process_memory", side_effect=write),
        )

    def test_team_relocation_uses_native_uid_index_after_cached_address_expires(self):
        reader = Mock()
        reader.team.side_effect = lambda address: (
            {"id": 9} if int(address or 0) == 0x3000 else None
        )
        directory = SimpleNamespace(
            addresses_for_uid=lambda table, uid: (
                (0x3000,) if table == "team" and uid == 9 else ()
            ),
        )

        with (
            patch.object(club_reader, "database_index_for_reader", return_value=directory),
            patch.object(club_reader, "scan_fixture_addresses") as fixture_scan,
        ):
            resolved = club_reader._resolve_team_address(reader, 9, "0x2000")

        self.assertEqual(resolved, 0x3000)
        fixture_scan.assert_not_called()

    def test_team_relocation_falls_back_when_native_index_refresh_fails(self):
        reader = Mock()
        reader.team.side_effect = lambda address: (
            {"id": 9} if int(address or 0) == 0x4000 else None
        )
        directory = Mock()
        directory.addresses_for_uid.side_effect = RuntimeError("table changed")
        fixture = SimpleNamespace(home_team=0x4000, away_team=0x5000)

        with (
            patch.object(club_reader, "database_index_for_reader", return_value=directory),
            patch.object(club_reader, "scan_fixture_addresses", return_value=([0x6000], {})),
            patch.object(club_reader, "parse_fixture", return_value=fixture),
        ):
            resolved = club_reader._resolve_team_address(reader, 9, "0x2000")

        self.assertEqual(resolved, 0x4000)

    def test_goalkeeper_bribe_sets_minimum_sharpness_and_snapshots_original(self):
        reader = _FakeReader(self.layout, goalkeepers=True)
        patches = self._patch_memory(reader)
        with ExitStack() as stack:
            for context in patches:
                stack.enter_context(context)
            stack.enter_context(patch.object(club_reader, "_is_goalkeeper", return_value=True))
            result = club_reader.apply_goalkeeper_bribe_match_state(9, "0x2000")

        self.assertEqual(reader.sharpness, 0)
        self.assertEqual(result["players"][0]["original_sharpness"], 6400)

    def test_goalkeeper_bribe_maintenance_reapplies_overwritten_sharpness(self):
        reader = _FakeReader(self.layout, goalkeepers=True)
        snapshot = {
            "player_id": 42,
            "player_name": "Player",
            "player_address": hex(reader.address),
            "original_sharpness": 6400,
        }
        patches = self._patch_memory(reader)
        with ExitStack() as stack:
            for context in patches:
                stack.enter_context(context)
            stack.enter_context(patch.object(club_reader, "_player_id_at", return_value=42))
            result = club_reader.maintain_goalkeeper_bribe_match_state(
                9, "0x2000", [snapshot],
            )

        self.assertEqual(result["maintained"], [42])
        self.assertEqual(result["repaired"], [42])
        self.assertFalse(result["errors"])
        self.assertEqual(reader.sharpness, 0)
        self.assertEqual(snapshot["original_sharpness"], 6400)
        self.assertEqual(result["players"][0]["original_sharpness"], 6400)

    def test_goalkeeper_bribe_writes_attributes_and_restores_full_original_block(self):
        reader = _FakeReader(self.layout, goalkeepers=True)
        original_attributes = bytes(reader.attributes)
        patches = self._patch_memory(reader)
        with ExitStack() as stack:
            for context in patches:
                stack.enter_context(context)
            stack.enter_context(patch.object(club_reader, "_is_goalkeeper", return_value=True))
            stack.enter_context(patch.object(club_reader, "_player_id_at", return_value=42))

            applied = club_reader.apply_goalkeeper_bribe(9, "0x2000")
            written_attributes = bytes(reader.attributes)
            restored = club_reader.restore_goalkeeper_bribe(
                9, "0x2000", applied["players"],
            )

        expected_attributes = bytearray(original_attributes)
        for attribute_id, raw_value in club_reader.GOALKEEPER_BRIBE_ATTRIBUTE_VALUES.items():
            expected_attributes[attribute_id - 0x0F] = raw_value
        self.assertEqual(written_attributes, bytes(expected_attributes))
        self.assertEqual(
            applied["players"][0]["original_attributes"],
            original_attributes.hex(),
        )
        self.assertEqual(restored["restored"], [42])
        self.assertFalse(restored["remaining"])
        self.assertFalse(restored["errors"])
        self.assertEqual(reader.attributes, original_attributes)

    def test_doping_maximizes_sharpness_and_fitness_and_snapshots_originals(self):
        reader = _FakeReader(self.layout)
        patches = self._patch_memory(reader)
        with ExitStack() as stack:
            for context in patches:
                stack.enter_context(context)
            stack.enter_context(patch.object(club_reader, "_is_goalkeeper", return_value=False))
            result = club_reader.apply_doping_effect(9, "0x2000", [{"player_id": 42}])

        snapshot = result["players"][0]
        self.assertTrue(all(
            reader.attributes[attribute_id - 0x0F] == 90
            for attribute_id in club_reader.DOPING_ATTRIBUTE_IDS
        ))
        self.assertEqual(reader.sharpness, 10000)
        self.assertEqual(reader.fitness, 10000)
        self.assertEqual(snapshot["original_attributes"], bytes([20] * 54).hex())
        self.assertEqual(snapshot["original_sharpness"], 6400)
        self.assertEqual(snapshot["original_fitness"], 7300)

    def test_doping_restore_restores_attributes_sharpness_and_fitness(self):
        reader = _FakeReader(self.layout)
        original_attributes = bytes([20] * 54)
        reader.attributes = bytes([90] * 54)
        reader.sharpness = 10000
        reader.fitness = 10000
        patches = self._patch_memory(reader)
        snapshot = {
            "player_id": 42,
            "player_name": "Player",
            "player_address": hex(reader.address),
            "original_attributes": original_attributes.hex(),
            "original_sharpness": 6400,
            "original_fitness": 7300,
        }
        with ExitStack() as stack:
            for context in patches:
                stack.enter_context(context)
            stack.enter_context(patch.object(club_reader, "_player_id_at", return_value=42))
            result = club_reader.restore_doping_effect(9, "0x2000", [snapshot])

        self.assertEqual(result["restored"], [42])
        self.assertEqual(reader.attributes, original_attributes)
        self.assertEqual(reader.sharpness, 6400)
        self.assertEqual(reader.fitness, 7300)

    def test_doping_maintenance_reapplies_overwritten_values_without_resnapshotting(self):
        reader = _FakeReader(self.layout)
        original_attributes = bytes([20] * 54)
        snapshot = {
            "player_id": 42,
            "player_name": "Player",
            "player_address": hex(reader.address),
            "original_attributes": original_attributes.hex(),
            "original_sharpness": 6400,
            "original_fitness": 7300,
        }
        patches = self._patch_memory(reader)
        with ExitStack() as stack:
            for context in patches:
                stack.enter_context(context)
            stack.enter_context(patch.object(club_reader, "_player_id_at", return_value=42))
            stack.enter_context(patch.object(club_reader, "_is_goalkeeper", return_value=False))
            result = club_reader.maintain_doping_effect(9, "0x2000", [snapshot])

        self.assertEqual(result["maintained"], [42])
        self.assertFalse(result["errors"])
        self.assertTrue(all(
            reader.attributes[attribute_id - 0x0F] >= 90
            for attribute_id in club_reader.DOPING_ATTRIBUTE_IDS
        ))
        self.assertEqual(reader.sharpness, 10000)
        self.assertEqual(reader.fitness, 10000)
        self.assertEqual(snapshot["original_attributes"], original_attributes.hex())
        self.assertEqual(result["players"][0]["original_attributes"], original_attributes.hex())

    def test_doping_maintenance_migrates_hook_only_snapshot_before_writing_attributes(self):
        reader = _FakeReader(self.layout)
        original_attributes = bytes(reader.attributes)
        snapshot = {
            "player_id": 42,
            "player_name": "Player",
            "player_address": hex(reader.address),
            "original_sharpness": 6400,
            "original_fitness": 7300,
        }
        patches = self._patch_memory(reader)
        with ExitStack() as stack:
            for context in patches:
                stack.enter_context(context)
            stack.enter_context(patch.object(club_reader, "_player_id_at", return_value=42))
            stack.enter_context(patch.object(club_reader, "_is_goalkeeper", return_value=False))
            result = club_reader.maintain_doping_effect(9, "0x2000", [snapshot])

        migrated = result["players"][0]
        self.assertEqual(migrated["original_attributes"], original_attributes.hex())
        self.assertTrue(all(
            reader.attributes[attribute_id - 0x0F] >= 90
            for attribute_id in club_reader.DOPING_ATTRIBUTE_IDS
        ))


if __name__ == "__main__":
    unittest.main()
