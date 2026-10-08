from __future__ import annotations

import inspect
import struct
import unittest
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

from tools import club_reader
from tools.initial_data_audit import Reader


class FM24ReaderOptimizationTests(unittest.TestCase):
    def setUp(self) -> None:
        club_reader._HUMAN_MANAGER_CACHE.clear()
        club_reader._STAFF_SCAN_CACHE.clear()

    def tearDown(self) -> None:
        club_reader._HUMAN_MANAGER_CACHE.clear()
        club_reader._STAFF_SCAN_CACHE.clear()

    def test_roster_bulk_reads_fm24_pointer_vector(self):
        reader = Reader.__new__(Reader)
        reader.layout = SimpleNamespace(key="fm24")
        reader.ptr = Mock(side_effect=[0x8000, 0x8018])
        reader.ptr_array = Mock(return_value=[])

        self.assertEqual(reader.roster(0x1000), [])

        reader.ptr_array.assert_called_once_with(0x8000, 3)

    def test_writable_reader_disables_prefetch_cache(self):
        reader = SimpleNamespace(
            _page_cache={0x1000: b"stale"},
            process=SimpleNamespace(pid=123),
            module=SimpleNamespace(base_address=0x100000),
            layout=SimpleNamespace(module_name="fm.exe"),
        )
        with (
            patch("tools.club_reader.borrow_game_reader", return_value=nullcontext(reader)),
            patch("tools.club_reader.open_process", return_value=nullcontext(object())),
        ):
            with club_reader._writable_game_reader() as (resolved, _process, _module):
                self.assertIs(resolved, reader)
                self.assertIsNone(reader._page_cache)

    def test_valid_cached_fm24_manager_skips_memory_scan(self):
        reader = SimpleNamespace(
            process=SimpleNamespace(pid=123),
            module_base=0x100000,
            layout=SimpleNamespace(key="fm24"),
        )
        key = (123, 0x100000, 77)
        club_reader._HUMAN_MANAGER_CACHE[key] = 0xABC000

        with (
            patch("tools.club_reader._human_manager_matches", return_value=True),
            patch(
                "tools.club_reader._find_human_manager_by_id",
                side_effect=AssertionError("scan should not run"),
            ),
        ):
            address = club_reader._find_human_manager(reader, 77)

        self.assertEqual(address, 0xABC000)

    def test_scanned_fm24_manager_is_cached(self):
        reader = SimpleNamespace(
            process=SimpleNamespace(pid=123),
            module_base=0x100000,
            layout=SimpleNamespace(key="fm24"),
        )
        key = (123, 0x100000, 77)

        with patch(
            "tools.club_reader._find_human_manager_by_id", return_value=0xABC000,
        ) as scan:
            first = club_reader._find_human_manager(reader, 77)
        with patch("tools.club_reader._human_manager_matches", return_value=True):
            second = club_reader._find_human_manager(reader, 77)

        self.assertEqual((first, second), (0xABC000, 0xABC000))
        self.assertEqual(club_reader._HUMAN_MANAGER_CACHE[key], 0xABC000)
        scan.assert_called_once_with(reader, 77)

    def test_database_manager_directory_skips_fm24_heap_scan(self):
        directory = SimpleNamespace(
            human_manager_addresses=lambda manager_id: (0xABC000,),
        )
        reader = SimpleNamespace(
            process=SimpleNamespace(pid=123), module_base=0x100000,
            layout=SimpleNamespace(key="fm24"),
            database_index_provider=lambda: directory,
        )
        with (
            patch("tools.club_reader._human_manager_matches", return_value=True),
            patch(
                "tools.club_reader._find_human_manager_by_id",
                side_effect=AssertionError("heap scan should not run"),
            ),
        ):
            address = club_reader._find_human_manager(reader, 77)

        self.assertEqual(address, 0xABC000)

    def test_staff_directory_reads_person_table_without_heap_scan(self):
        person = 0x5000
        contract = 0x7000
        raw = bytearray(0xD0)
        struct.pack_into("<Q", raw, 0xC8, contract)
        directory = SimpleNamespace(
            table_token=lambda name: (1, 2, "person-token"),
            table_ready=lambda name: True,
            addresses_for_vtable=lambda name, rva: (person,),
        )
        reader = SimpleNamespace(
            process=SimpleNamespace(pid=123), module_base=0x100000,
            layout=SimpleNamespace(
                key="fm24", staff_person_vtable_rva=0x200,
            ),
            database_index_provider=lambda: directory,
            _fixed_size_snapshots=Mock(return_value={person: bytes(raw)}),
        )
        unrelated_key = (123, 0x100000, 0x200, 88, (1, 2, "person-token"))
        club_reader._STAFF_SCAN_CACHE[unrelated_key] = (10**12, [(0x9000, 0xA000)])
        with patch(
            "tools.club_reader.iter_readable_regions",
            side_effect=AssertionError("heap scan should not run"),
        ):
            entries = club_reader._staff_scan_entries(reader, 77)

        self.assertEqual(entries, [(person, contract)])
        reader._fixed_size_snapshots.assert_called_once_with((person,), 0xD0)
        self.assertEqual(
            club_reader._STAFF_SCAN_CACHE[unrelated_key][1], [(0x9000, 0xA000)],
        )

    def test_timed_cache_pruning_removes_expired_and_oldest_entries(self):
        cache = {
            (123, index): (float(index), {"index": index})
            for index in range(15)
        }
        cache[(999, 99)] = (14.5, {"index": 99})

        with patch("tools.club_reader.monotonic", return_value=20.0):
            club_reader._prune_timed_cache_locked(
                cache, ttl=100, maximum=12, current_pid=123,
            )

        self.assertEqual(len(cache), 12)
        self.assertNotIn((999, 99), cache)
        self.assertNotIn((123, 0), cache)
        self.assertNotIn((123, 1), cache)
        self.assertNotIn((123, 2), cache)
        self.assertIn((123, 14), cache)

    def test_club_information_runs_after_the_early_player_callback(self):
        source = inspect.getsource(club_reader.read_club_profile)

        self.assertLess(
            source.index("partial_callback({"),
            source.index("club_information = (", source.index("partial_callback({")),
        )

    def test_managed_club_squads_reuse_first_team_and_read_youth_teams(self):
        first_team_address = 0x1000
        first_team_players = [{"id": 1, "name": "First"}]
        reader = SimpleNamespace(
            layout=SimpleNamespace(key="fm24"),
            roster=Mock(side_effect=lambda address: {
                0x2000: [{"id": 2}],
                0x3000: [{"id": 3}],
            }[address]),
        )
        related = [
            (0x1000, {"id": 10, "team_type": "club", "squad_type_code": 0, "squad_type_name": "一线队"}),
            (0x2000, {"id": 20, "team_type": "club", "squad_type_code": 10, "squad_type_name": "21岁以下青年队"}),
            (0x3000, {"id": 30, "team_type": "club", "squad_type_code": 12, "squad_type_name": "18岁以下青年队"}),
        ]

        with (
            patch("tools.world_clubs._related_club_squads", return_value=related),
            patch("tools.club_reader._prefetch_roster_dependencies"),
            patch(
                "tools.club_reader.read_roster_player_profile",
                side_effect=lambda _reader, row, *_args: {
                    "id": row["id"], "name": f"Youth {row['id']}",
                },
            ),
            patch("tools.club_reader.attach_retirement_details"),
        ):
            squads = club_reader._read_managed_club_squads(
                reader,
                first_team_address=first_team_address,
                first_team=related[0][1],
                first_team_players=first_team_players,
                game_date=None,
                manager_person=0x9000,
            )

        self.assertEqual([row["team_id"] for row in squads], [10, 20, 30])
        self.assertIs(squads[0]["players"], first_team_players)
        self.assertEqual([row["player_count"] for row in squads], [1, 1, 1])
        self.assertEqual(squads[1]["players"][0]["squad_team_id"], 20)
        self.assertEqual(squads[2]["players"][0]["squad_type_code"], 12)

    def test_vector_prefetch_enforces_the_declared_record_limit(self):
        reader = SimpleNamespace(
            bytes=Mock(return_value=struct.pack("<QQQ", 0x4000, 0x4400, 0x4400)),
            prefetch=Mock(),
        )

        club_reader._prefetch_vector_data(reader, 0x3000, width=8, limit=64)

        reader.prefetch.assert_not_called()

    def test_fm26_dependency_prefetch_uses_generation_specific_offsets(self):
        player = 0x5000
        person = player + 0x380
        relationships = 0x7000
        contract = 0x9000
        language_pointers = 0xB000

        def pointer(address):
            return {
                person + 0x78: relationships,
                person + club_reader.PERSON_CONTRACT: contract,
            }.get(address)

        def read_bytes(address, _size):
            return {
                relationships: struct.pack("<QQQ", 0x8000, 0x8020, 0x8020),
                contract + 0x68: struct.pack("<QQQ", 0xA000, 0xA010, 0xA010),
                person + 0xF0: struct.pack(
                    "<QQQ", language_pointers, language_pointers + 8,
                    language_pointers + 8,
                ),
            }.get(address)

        reader = SimpleNamespace(
            layout=SimpleNamespace(
                key="fm26", player_person_offset=0x288,
                player_and_non_player_person_offset=0x380,
                person_relationships_offset=0x78,
            ),
            ptr=Mock(side_effect=pointer),
            bytes=Mock(side_effect=read_bytes),
            prefetch=Mock(),
        )

        club_reader._prefetch_roster_dependencies(reader, [{
            "address": hex(player), "object_type": "actual_player_and_non_player",
        }])

        self.assertEqual(reader.ptr.call_args_list, [
            call(person + 0x78),
            call(person + club_reader.PERSON_CONTRACT),
        ])
        calls = [call.args for call in reader.prefetch.call_args_list]
        self.assertIn((relationships, 24), calls)
        self.assertIn((0x8000, 0x20), calls)
        self.assertIn((contract, 0xC0), calls)
        self.assertIn((0xA000, 0x10), calls)
        self.assertIn((language_pointers, 8), calls)

    def test_staff_directory_not_ready_joins_person_index_build(self):
        person = 0x5000
        contract = 0x7000
        raw = bytearray(0xD0)
        struct.pack_into("<Q", raw, 0xC8, contract)
        directory = SimpleNamespace(
            table_token=lambda name: (1, 2, "person-token"),
            table_ready=lambda name: False,
            addresses_for_vtable=Mock(return_value=(person,)),
        )
        reader = SimpleNamespace(
            process=SimpleNamespace(pid=123), module_base=0x100000,
            layout=SimpleNamespace(
                key="fm24", staff_person_vtable_rva=0x200,
            ),
            database_index_provider=lambda: directory,
            _fixed_size_snapshots=Mock(return_value={person: bytes(raw)}),
        )
        with patch(
            "tools.club_reader.iter_readable_regions",
            side_effect=AssertionError("heap scan should not run"),
        ):
            entries = club_reader._staff_scan_entries(reader, 77)

        self.assertEqual(entries, [(person, contract)])
        directory.addresses_for_vtable.assert_called_once_with("person", 0x200)
        reader._fixed_size_snapshots.assert_called_once_with((person,), 0xD0)

    def test_empty_ready_staff_index_still_falls_back_to_heap_scan(self):
        directory = SimpleNamespace(
            table_token=lambda name: (1, 2, "person-token"),
            table_ready=lambda name: True,
            addresses_for_vtable=lambda name, rva: (),
        )
        reader = SimpleNamespace(
            process=SimpleNamespace(pid=123), module_base=0x100000,
            layout=SimpleNamespace(
                key="fm24", staff_person_vtable_rva=0x200,
            ),
            database_index_provider=lambda: directory,
            _fixed_size_snapshots=Mock(return_value={}),
        )
        with patch(
            "tools.club_reader.iter_readable_regions", return_value=[],
        ) as scan:
            entries = club_reader._staff_scan_entries(reader, 77)

        self.assertEqual(entries, [])
        scan.assert_called_once_with(reader.process)

    def test_failed_ready_staff_index_still_falls_back_to_heap_scan(self):
        directory = SimpleNamespace(
            table_token=lambda name: (1, 2, "person-token"),
            table_ready=lambda name: True,
            addresses_for_vtable=Mock(
                side_effect=RuntimeError("person table changed"),
            ),
        )
        reader = SimpleNamespace(
            process=SimpleNamespace(pid=123), module_base=0x100000,
            layout=SimpleNamespace(
                key="fm24", staff_person_vtable_rva=0x200,
            ),
            database_index_provider=lambda: directory,
        )
        with patch(
            "tools.club_reader.iter_readable_regions", return_value=[],
        ) as scan:
            entries = club_reader._staff_scan_entries(reader, 77)

        self.assertEqual(entries, [])
        scan.assert_called_once_with(reader.process)

    def test_club_profile_cache_misses_when_team_table_token_changes(self):
        old_token = (1, 2, "old")
        new_token = (1, 2, "new")
        key = (123, 77, 42, old_token)
        club_reader._CACHE[key] = (10**12, {"stale": True})
        directory = SimpleNamespace(table_token=lambda name: new_token)
        reader = SimpleNamespace(
            process=SimpleNamespace(pid=123), layout=SimpleNamespace(key="fm24"),
            database_index_provider=lambda: directory,
        )
        try:
            with (
                patch(
                    "tools.club_reader.borrow_game_reader",
                    return_value=nullcontext(reader),
                ),
                patch(
                    "tools.club_reader._context_addresses",
                    side_effect=RuntimeError("cache miss confirmed"),
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "cache miss confirmed"):
                    club_reader.read_club_profile(77, 42)
        finally:
            with club_reader._CACHE_LOCK:
                club_reader._CACHE.pop(key, None)

    def test_club_profile_cache_misses_when_read_session_generation_changes(self):
        token = (1, 2, "same")
        key = (123, 77, 42, token, 1)
        club_reader._CACHE[key] = (10**12, {"stale": True})
        directory = SimpleNamespace(table_token=lambda name: token)
        reader = SimpleNamespace(
            process=SimpleNamespace(pid=123), layout=SimpleNamespace(key="fm24"),
            session_generation=2,
            database_index_provider=lambda: directory,
        )
        try:
            with (
                patch(
                    "tools.club_reader.borrow_game_reader",
                    return_value=nullcontext(reader),
                ),
                patch(
                    "tools.club_reader._context_addresses",
                    side_effect=RuntimeError("new session cache miss"),
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "new session cache miss"):
                    club_reader.read_club_profile(77, 42)
        finally:
            with club_reader._CACHE_LOCK:
                club_reader._CACHE.pop(key, None)

    def test_club_profile_cache_returns_copy_instead_of_shared_payload(self):
        token = (1, 2, "same")
        key = (123, 77, 42, token, 1)
        cached = {"team": {"name": "Original"}}
        club_reader._CACHE[key] = (10**12, cached)
        directory = SimpleNamespace(table_token=lambda name: token)
        reader = SimpleNamespace(
            process=SimpleNamespace(pid=123), layout=SimpleNamespace(key="fm24"),
            session_generation=1,
            database_index_provider=lambda: directory,
        )
        try:
            with patch(
                "tools.club_reader.borrow_game_reader",
                return_value=nullcontext(reader),
            ):
                returned = club_reader.read_club_profile(77, 42)
            returned["team"]["name"] = "Changed"
            self.assertEqual(cached["team"]["name"], "Original")
        finally:
            with club_reader._CACHE_LOCK:
                club_reader._CACHE.pop(key, None)

    def test_manager_relationship_vector_is_parsed_from_one_read(self):
        manager_person = 0x9000
        payload = b"".join((
            struct.pack("<Q4xB3x", 0x8000, 25),
            struct.pack("<Q4xB3x", manager_person, 73),
            struct.pack("<Q4xB3x", manager_person, 81),
        ))

        class FakeReader:
            layout = SimpleNamespace(person_relationships_offset=0x80)

            def __init__(self):
                self.ptr_calls = []
                self.byte_calls = []

            def ptr(self, address):
                self.ptr_calls.append(address)
                return {
                    0x1080: 0x2000,
                    0x2000: 0x3000,
                    0x2008: 0x3030,
                    0x2010: 0x3040,
                }.get(address)

            def bytes(self, address, size):
                self.byte_calls.append((address, size))
                return payload

            def u8(self, _address):
                raise AssertionError("per-entry reads should not run")

        reader = FakeReader()

        self.assertEqual(
            club_reader._manager_intimacy(reader, 0x1000, manager_person), 81,
        )
        self.assertEqual(reader.byte_calls, [(0x3000, 0x30)])

    def test_light_player_target_resolution_does_not_expand_roster_profiles(self):
        module_base = 0x100000
        team = 0x2000
        roster = 0x3000
        player = 0x4000
        layout = SimpleNamespace(
            team_vtable_rva=0x100,
            national_team_vtable_rva=0x200,
            actual_player_vtable_rvas=(0x300,),
            player_and_non_player_vtable_rvas=(),
            player_person_offset=0x278,
            player_and_non_player_person_offset=0x368,
        )

        class FakeReader:
            def __init__(self):
                self.module_base = module_base
                self.layout = layout
                self.ptr_array_calls = []
                self.byte_calls = []

            def bytes(self, address, size):
                self.byte_calls.append((address, size))
                raw = bytearray(size)
                if address == team:
                    struct.pack_into("<Q", raw, 0, module_base + layout.team_vtable_rva)
                    struct.pack_into("<I", raw, 0x0C, 9)
                    struct.pack_into("<Q", raw, 0x38, roster)
                    struct.pack_into("<Q", raw, 0x40, roster + 16)
                    return bytes(raw)
                if address == player:
                    struct.pack_into(
                        "<Q", raw, 0,
                        module_base + layout.actual_player_vtable_rvas[0],
                    )
                    struct.pack_into(
                        "<I", raw, layout.player_person_offset + 0x0C, 42,
                    )
                    return bytes(raw)
                return None

            def ptr_array(self, address, count):
                self.ptr_array_calls.append((address, count))
                return [0x5000, player]

            def roster(self, _team):
                raise AssertionError("full roster expansion must not run")

        reader = FakeReader()
        with patch(
            "tools.club_reader.borrow_game_reader", return_value=nullcontext(reader),
        ):
            resolved = club_reader.resolve_team_player_address(
                9, hex(team), 42, hex(player),
            )

        self.assertEqual(resolved, hex(player))
        self.assertEqual(reader.ptr_array_calls, [(roster, 2)])
        self.assertEqual(reader.byte_calls, [(team, 0x48), (player, 0x378)])

    def test_intimacy_write_finds_manager_in_one_relationship_block_read(self):
        manager_person = 0x9000
        relationships = 0x2000
        begin = 0x3000
        payload = b"".join((
            struct.pack("<Q4xB3x", 0x8000, 25),
            struct.pack("<Q4xB3x", manager_person, 73),
        ))
        score = {begin + 16 + 12: 73}

        class FakeReader:
            def __init__(self):
                self.byte_calls = []
                self.ptr_calls = []

            def ptr(self, address):
                self.ptr_calls.append(address)
                return relationships if address == 0x1080 else None

            def bytes(self, address, size):
                self.byte_calls.append((address, size))
                if address == relationships and size == 24:
                    return struct.pack("<QQQ", begin, begin + len(payload), begin + len(payload))
                if address == begin and size == len(payload):
                    return payload
                return None

            def u8(self, address):
                return score.get(address)

        reader = FakeReader()
        layout = SimpleNamespace(
            module_name="fm.exe", person_relationships_offset=0x80,
            module=lambda _process: SimpleNamespace(base_address=0x100000),
        )

        def write(_process, address, data):
            score[address] = data[0]

        with (
            patch("tools.club_reader.select_process_layout", return_value=(1, "fm.exe", layout)),
            patch("tools.club_reader.open_process", return_value=nullcontext(object())),
            patch("tools.club_reader.Reader", return_value=reader),
            patch(
                "tools.club_reader._context_addresses",
                return_value=(0x7000, manager_person, 0x6000),
            ),
            patch("tools.club_reader._validated_player_person", return_value=0x1000),
            patch("tools.club_reader.write_process_memory", side_effect=write),
        ):
            result = club_reader.adjust_manager_intimacy_points(
                42, "0x4000", 7, 9, points=1,
            )

        self.assertEqual(result, {"before": 73, "after": 74, "applied": 1})
        self.assertEqual(reader.byte_calls, [
            (relationships, 24), (begin, len(payload)),
        ])
        self.assertEqual(reader.ptr_calls, [0x1080])

    def test_shared_attribute_block_avoids_duplicate_reads(self):
        raw = bytes([50] * 54)
        reader = SimpleNamespace(
            layout=SimpleNamespace(
                player_attributes_offset=0x200,
                attribute_display_bias=2,
            ),
            bytes=Mock(side_effect=AssertionError("attribute block was reread")),
        )

        with patch("tools.club_reader._person_hidden_attributes", return_value={}):
            visible = club_reader._visible_player_attributes(
                reader, 0x1000, raw, goalkeeper=False,
            )
            hidden = club_reader._player_hidden_attributes(
                reader, 0x1000, 0x1200, raw,
            )

        self.assertTrue(visible)
        self.assertTrue(hidden)
        reader.bytes.assert_not_called()


if __name__ == "__main__":
    unittest.main()
