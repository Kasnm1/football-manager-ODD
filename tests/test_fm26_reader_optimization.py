from __future__ import annotations

import struct
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tools import club_reader
from tools.club_reader import _player
from tools.initial_data_audit import ENTITY_UID, TEAM_CLUB, Reader
from tools import initial_data_audit
from tools.game_layout import FM24_LAYOUT, FM26_LAYOUT, FM26_XGP_TEMPLATE


class FM26ReaderOptimizationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.preferred_layout_key = initial_data_audit._PREFERRED_LAYOUT_KEY
        self.preferred_process_pid = initial_data_audit._PREFERRED_PROCESS_PID
        club_reader._HUMAN_MANAGER_CACHE.clear()
        club_reader._STAFF_SCAN_CACHE.clear()
        initial_data_audit.invalidate_process_layout_cache()

    def tearDown(self) -> None:
        initial_data_audit._PREFERRED_LAYOUT_KEY = self.preferred_layout_key
        initial_data_audit._PREFERRED_PROCESS_PID = self.preferred_process_pid
        club_reader._HUMAN_MANAGER_CACHE.clear()
        club_reader._STAFF_SCAN_CACHE.clear()
        initial_data_audit.invalidate_process_layout_cache()

    def test_fm26_team_type_uses_live_verified_native_byte(self):
        self.assertEqual(FM26_LAYOUT.team_type_offset, 0x28)

    def test_national_team_canonical_nation_layout_is_steam_only(self):
        self.assertEqual(FM26_LAYOUT.national_team_nation_offset, 0x170)
        self.assertEqual(FM26_LAYOUT.nationality_vtable_rva, 0x44BF9B8)
        self.assertIsNone(FM26_XGP_TEMPLATE.national_team_nation_offset)
        self.assertIsNone(FM26_XGP_TEMPLATE.nationality_vtable_rva)
        self.assertIsNone(FM24_LAYOUT.national_team_nation_offset)
        self.assertIsNone(FM24_LAYOUT.nationality_vtable_rva)

    def test_national_team_uses_verified_canonical_nation_name(self):
        team_address = 0x500000
        nation_container = 0x600000
        canonical_nation = 0x700000
        module_base = 0x100000
        layout = SimpleNamespace(
            national_team_vtable_rva=0x1000,
            nation_vtable_rva=0x2000,
            national_team_nation_offset=0x170,
            nationality_vtable_rva=0x3000,
            team_reputation_offset=None,
        )
        reader = Reader.__new__(Reader)
        reader.module_base = module_base
        reader.layout = layout
        reader.team_cache = {}
        reader.ptr = Mock(side_effect=lambda address: {
            team_address: module_base + layout.national_team_vtable_rva,
            team_address + TEAM_CLUB: nation_container,
            nation_container: module_base + layout.nation_vtable_rva,
            nation_container + layout.national_team_nation_offset: canonical_nation,
            canonical_nation: module_base + layout.nationality_vtable_rva,
        }.get(address))
        reader.u32 = Mock(side_effect=lambda address: {
            team_address + ENTITY_UID: 2000169662,
            nation_container + ENTITY_UID: 2000169662,
            canonical_nation + ENTITY_UID: 1438,
        }.get(address))
        reader.competition = Mock(return_value=None)
        reader.fm_string_at = Mock(return_value=None)

        team = reader.team(team_address)

        self.assertIsNotNone(team)
        self.assertEqual(team["name"], initial_data_audit.NATION_NAMES[1438])
        self.assertEqual(team["short_name"], initial_data_audit.NATION_NAMES[1438])
        self.assertEqual(team["team_type"], "national")

    def test_national_team_invalid_canonical_relation_keeps_uid_fallback(self):
        reader = Reader.__new__(Reader)
        reader.module_base = 0x100000
        reader.layout = SimpleNamespace(
            national_team_nation_offset=0x170,
            nationality_vtable_rva=0x3000,
        )
        nation_container = 0x600000
        canonical_nation = 0x700000
        reader.ptr = Mock(side_effect=lambda address: {
            nation_container + 0x170: canonical_nation,
            canonical_nation: reader.module_base + 0x9999,
        }.get(address))
        reader.u32 = Mock(side_effect=AssertionError("invalid object UID must not be trusted"))

        self.assertEqual(
            reader.national_team_name(nation_container, 2000169662),
            "国家队 2000169662",
        )

    def test_pointer_array_uses_one_bulk_read(self):
        reader = Reader.__new__(Reader)
        reader.read_calls = 0
        reader.requested_bytes = 0
        reader.returned_bytes = 0
        reader.process = object()
        payload = struct.pack("<4Q", 0x1000, 0, 0x3000, 0x4000)

        with patch("tools.initial_data_audit.read_process_memory", return_value=payload) as read:
            pointers = reader.ptr_array(0x8000, 4)

        self.assertEqual(pointers, [0x1000, None, 0x3000, 0x4000])
        read.assert_called_once_with(reader.process, 0x8000, 32)
        self.assertEqual(
            reader.read_metrics(),
            {
                "memory_read_calls": 1,
                "memory_requested_bytes": 32,
                "memory_returned_bytes": 32,
            },
        )

    def test_pointer_array_falls_back_when_bulk_read_fails(self):
        reader = Reader.__new__(Reader)
        reader.read_calls = 0
        reader.requested_bytes = 0
        reader.returned_bytes = 0
        reader.process = object()
        responses = [None, struct.pack("<Q", 0x1000), struct.pack("<Q", 0x2000)]

        with patch("tools.initial_data_audit.read_process_memory", side_effect=responses) as read:
            pointers = reader.ptr_array(0x8000, 2)

        self.assertEqual(pointers, [0x1000, 0x2000])
        self.assertEqual(read.call_count, 3)

    def test_explicit_page_prefetch_serves_cross_page_reads_from_cache(self):
        reader = Reader(object(), 0, SimpleNamespace())
        first_page = bytes([0xAA]) * 0x1000
        second_page = bytes([0xBB]) * 0x1000

        def read_page(_process, address, size):
            self.assertEqual(size, 0x1000)
            return {0x1000: first_page, 0x2000: second_page}.get(address)

        with patch(
            "tools.initial_data_audit.read_process_memory", side_effect=read_page,
        ) as read:
            reader.prefetch(0x1FFE, 4)
            payload = reader.bytes(0x1FFE, 4)

        self.assertEqual(payload, b"\xAA\xAA\xBB\xBB")
        self.assertEqual(read.call_count, 2)
        self.assertEqual(reader.read_metrics(), {
            "memory_read_calls": 2,
            "memory_requested_bytes": 4,
            "memory_returned_bytes": 0x2000,
        })

    def test_roster_valuation_skips_names_positions_and_availability(self):
        reader = Reader.__new__(Reader)
        reader.module_base = 0x100000
        reader.layout = SimpleNamespace(
            actual_player_vtable_rvas=(0x200,),
            player_and_non_player_vtable_rvas=(0x300,),
            player_person_offset=0x80,
            player_and_non_player_person_offset=0xA0,
            player_ca_offset=0x20,
            player_ca_bytes=2,
            player_pa_offset=0x22,
        )
        team_address = 0x500000
        player_address = 0x600000
        reader.ptr = Mock(side_effect=lambda address: {
            team_address + initial_data_audit.TEAM_ROSTER_BEGIN: 0x700000,
            team_address + initial_data_audit.TEAM_ROSTER_END: 0x700008,
            player_address: reader.module_base + 0x200,
        }.get(address))
        reader.ptr_array = Mock(return_value=[player_address])
        reader.u32 = Mock(return_value=42)
        reader.u8 = Mock(side_effect=AssertionError("one-byte CA should not be used"))
        reader.u16 = Mock(side_effect=lambda address: {
            player_address + 0x20: 150,
            player_address + 0x22: 180,
        }.get(address))
        reader.fm_string_at = Mock(side_effect=AssertionError("names should not be read"))

        roster = reader.roster_valuation(team_address)

        self.assertEqual(roster, [{"id": 42, "ca": 150, "pa": 180}])
        reader.fm_string_at.assert_not_called()

    def test_roster_model_snapshot_bulk_reads_live_odds_fields_once(self):
        team_address = 0x4000
        vector_address = 0x4500
        player_address = 0x5000
        injury_address = 0x9000
        module_base = 0x100000
        layout = SimpleNamespace(
            actual_player_vtable_rvas=(0x100,),
            player_and_non_player_vtable_rvas=(0x200,),
            player_person_offset=0x80,
            player_and_non_player_person_offset=0xA0,
            player_ca_offset=0x20,
            player_ca_bytes=2,
            player_fitness_offset=0x22,
            player_sharpness_offset=0x24,
            player_morale_offset=0x26,
            player_injury_list_offset=0x30,
        )
        header = bytearray(0xB0)
        struct.pack_into("<Q", header, 0, module_base + 0x100)
        struct.pack_into("<H", header, 0x20, 150)
        struct.pack_into("<H", header, 0x22, 9850)
        struct.pack_into("<H", header, 0x24, 9100)
        struct.pack_into("<B", header, 0x26, 18)
        struct.pack_into("<Q", header, 0x30, injury_address)
        struct.pack_into("<I", header, 0x80 + ENTITY_UID, 42)
        vectors = bytearray(0x28)
        struct.pack_into("<QQ", vectors, 0, 0xA000, 0xA008)
        struct.pack_into("<QQ", vectors, 0x18, 0xB000, 0xB000)

        reader = Reader.__new__(Reader)
        reader.module_base = module_base
        reader.layout = layout
        reader.ptr = Mock(side_effect=lambda address: {
            team_address + initial_data_audit.TEAM_ROSTER_BEGIN: vector_address,
            team_address + initial_data_audit.TEAM_ROSTER_END: vector_address + 8,
        }.get(address))
        reader.ptr_array = Mock(return_value=[player_address])
        reader.bytes = Mock(side_effect=lambda address, _size: {
            player_address: bytes(header),
            injury_address: bytes(vectors),
        }.get(address))

        signature, squad = reader.roster_model_snapshot(team_address)

        self.assertEqual(len(signature), 64)
        self.assertEqual(squad, [{
            "id": 42, "address": hex(player_address), "ca": 150,
            "fitness_percent": 98.5, "sharpness_percent": 91.0,
            "morale_raw": 18,
            "availability": {"injury_count": 1, "ban_count": 0},
        }])
        self.assertEqual(reader.bytes.call_count, 2)

    def test_team_accepts_reserve_team_with_distinct_club_uid(self):
        team_address = 0x500000
        club_address = 0x600000
        module_base = 0x100000
        layout = SimpleNamespace(
            team_vtable_rva=0x1000,
            national_team_vtable_rva=0x2000,
            club_vtable_rva=0x3000,
            team_reputation_offset=None,
        )
        reader = Reader.__new__(Reader)
        reader.module_base = module_base
        reader.layout = layout
        reader.team_cache = {}
        reader.comp_cache = {}
        reader.ptr = Mock(side_effect=lambda address: {
            team_address: module_base + layout.team_vtable_rva,
            team_address + TEAM_CLUB: club_address,
            club_address: module_base + layout.club_vtable_rva,
        }.get(address))
        reader.u32 = Mock(side_effect=lambda address: {
            team_address + ENTITY_UID: 200,
            club_address + ENTITY_UID: 100,
        }.get(address))
        reader.u8 = Mock(return_value=None)
        reader.fm_string_at = Mock(side_effect=["Ajax II", "Ajax II"])
        reader.competition = Mock(return_value=None)

        team = reader.team(team_address)

        self.assertIsNotNone(team)
        self.assertEqual(team["id"], 200)
        self.assertEqual(team["name"], "Ajax II")
        self.assertEqual(team["club_address"], hex(club_address))

    def test_valid_cached_manager_skips_full_memory_scan(self):
        reader = SimpleNamespace(
            process=SimpleNamespace(pid=123),
            module_base=0x100000,
            layout=SimpleNamespace(key="fm26"),
        )
        key = (123, 0x100000, 77)
        club_reader._HUMAN_MANAGER_CACHE[key] = 0xABC000

        with (
            patch("tools.club_reader._human_manager_matches", return_value=True),
            patch("tools.club_reader.iter_readable_regions", side_effect=AssertionError("scan should not run")),
        ):
            address = club_reader._find_human_manager(reader, 77)

        self.assertEqual(address, 0xABC000)

    def test_cold_person_index_reads_fm26_staff_contract_without_heap_scan(self):
        person = 0x5000
        contract = 0x7000
        raw = bytearray(club_reader.PERSON_CONTRACT + 8)
        struct.pack_into("<Q", raw, club_reader.PERSON_CONTRACT, contract)
        directory = SimpleNamespace(
            table_token=lambda name: (1, 2, "fm26-person-token"),
            table_ready=lambda name: False,
            addresses_for_vtable=lambda name, rva: (person,),
        )
        reader = SimpleNamespace(
            process=SimpleNamespace(pid=126), module_base=0x100000,
            layout=SimpleNamespace(
                key="fm26", staff_person_vtable_rva=0x200,
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
        reader._fixed_size_snapshots.assert_called_once_with(
            (person,), club_reader.PERSON_CONTRACT + 8,
        )

    def test_invalid_person_table_header_falls_back_to_bounded_staff_scan(self):
        directory = SimpleNamespace(
            table_token=Mock(
                side_effect=RuntimeError("native person table header is invalid"),
            ),
            table_ready=Mock(
                side_effect=AssertionError("invalid person table must not be reused"),
            ),
        )
        reader = SimpleNamespace(
            process=SimpleNamespace(pid=127), module_base=0x100000,
            layout=SimpleNamespace(
                key="fm26", staff_person_vtable_rva=0x200,
            ),
            database_index_provider=lambda: directory,
        )
        with patch(
            "tools.club_reader.iter_readable_regions", return_value=[],
        ) as scan:
            entries = club_reader._staff_scan_entries(reader, 77)

        self.assertEqual(entries, [])
        directory.table_ready.assert_not_called()
        scan.assert_called_once_with(reader.process)

    def test_invalid_cached_manager_is_evicted(self):
        reader = SimpleNamespace(
            process=SimpleNamespace(pid=123),
            module_base=0x100000,
            layout=SimpleNamespace(key="fm26"),
        )
        key = (123, 0x100000, 77)
        club_reader._HUMAN_MANAGER_CACHE[key] = 0xABC000

        with (
            patch("tools.club_reader._human_manager_matches", return_value=False),
            patch("tools.club_reader.iter_readable_regions", return_value=[]),
        ):
            with self.assertRaisesRegex(RuntimeError, "无法定位当前人类经理对象"):
                club_reader._find_human_manager(reader, 77)

        self.assertNotIn(key, club_reader._HUMAN_MANAGER_CACHE)

    def test_player_details_reuse_roster_summary(self):
        player_address = 0x500000
        person_address = player_address + 0x100
        layout = SimpleNamespace(
            player_person_offset=0x100,
            player_and_non_player_person_offset=0x200,
            actual_player_vtable_rvas=(0x20,),
            player_and_non_player_vtable_rvas=(0x30,),
            player_positions_offset=0x10,
            player_attributes_offset=0x40,
            person_nationality_offset=0x70,
            player_height_offset=0x80,
            player_world_reputation_offset=0x82,
            player_pa_offset=0x84,
            player_ca_offset=0x86,
            player_fitness_offset=0x88,
            player_sharpness_offset=0x8A,
            player_fatigue_offset=0x8C,
            player_morale_offset=0x8E,
            person_first_name_offset=0x50,
            person_last_name_offset=0x58,
            person_common_name_offset=0x60,
        )

        class FakeReader:
            module_base = 0x100000

            def __init__(self):
                self.layout = layout
                self.ptr_calls = []
                self.u32_calls = []
                self.bytes_calls = []

            def ptr(self, address):
                self.ptr_calls.append(address)
                if address == person_address + layout.person_nationality_offset:
                    return 0x900000
                return 0xA00000 + address

            def u32(self, address):
                self.u32_calls.append(address)
                return 1

            def u16(self, _address):
                return 100

            def u8(self, _address):
                return 180

            def bytes(self, address, size):
                self.bytes_calls.append((address, size))
                if address == player_address + layout.player_attributes_offset + 0x18:
                    return bytes((80, 90))
                return bytes(size)

        reader = FakeReader()
        summary = {
            "id": 42,
            "name": "Cached Player",
            "address": hex(player_address),
            "object_type": "actual_player",
            "positions": {"ST": 20},
            "ca": 150,
            "pa": 180,
            "fitness_percent": 98.5,
            "sharpness_percent": 91.0,
            "fatigue_raw": 123,
            "morale_raw": 88,
        }

        with (
            patch("tools.club_reader._name", side_effect=AssertionError("name was reread")),
            patch("tools.club_reader._visible_player_attributes", return_value={}),
            patch("tools.club_reader._person_birth_date", return_value=None),
            patch(
                "tools.club_reader._player_hidden_attributes",
                return_value={"大赛发挥": 11},
            ) as hidden_attributes,
            patch("tools.club_reader._manager_intimacy", return_value=0),
        ):
            player = _player(
                reader, player_address, roster_summary=summary,
                raw_snapshot={"positions": bytes(15), "attributes": bytes(54)},
            )

        self.assertIsNotNone(player)
        self.assertEqual((player["id"], player["name"], player["ca"], player["pa"]), (42, "Cached Player", 150, 180))
        self.assertEqual((player["fitness"], player["sharpness"], player["fatigue"], player["morale"]), (98.5, 91.0, 123, 88))
        self.assertEqual(player["hidden_attributes"]["大赛发挥"], 11)
        hidden_attributes.assert_called_once_with(
            reader, player_address, person_address, bytes(54),
        )
        self.assertNotIn(player_address, reader.ptr_calls)
        self.assertNotIn(person_address + 0x0C, reader.u32_calls)
        self.assertNotIn(
            (player_address + layout.player_positions_offset, 15), reader.bytes_calls,
        )
        self.assertNotIn(
            (player_address + layout.player_attributes_offset, 54), reader.bytes_calls,
        )

    def test_process_layout_is_reused_within_short_session_window(self):
        layout = SimpleNamespace(key="fm26")
        with (
            patch(
                "tools.game_session.active_game_read_selection",
                return_value=None,
            ),
            patch("tools.initial_data_audit.select_process", return_value=(123, "fm.exe")) as select,
            patch("tools.initial_data_audit.layout_for_executable", return_value=layout) as resolve,
        ):
            first = initial_data_audit.select_process_layout()
            second = initial_data_audit.select_process_layout()

        self.assertEqual(first, (123, "fm.exe", layout))
        self.assertIs(first, second)
        select.assert_called_once_with()
        resolve.assert_called_once_with("fm.exe")

    def test_expired_layout_cache_reuses_live_verified_session(self):
        layout = SimpleNamespace(key="fm26")
        selected = (321, "steam/fm.exe", layout)
        initial_data_audit._PREFERRED_LAYOUT_KEY = "fm26"
        initial_data_audit._PREFERRED_PROCESS_PID = 321
        initial_data_audit._PROCESS_LAYOUT_CACHE = (
            0.0, "fm26", 321, selected,
        )

        with (
            patch("tools.initial_data_audit.time.monotonic", return_value=10.0),
            patch(
                "tools.game_session.active_game_read_selection",
                return_value=selected,
            ) as active,
            patch("tools.initial_data_audit.select_process") as scan,
            patch("tools.initial_data_audit.layout_for_executable") as resolve,
        ):
            self.assertIs(initial_data_audit.select_process_layout(), selected)

        active.assert_called_once_with()
        scan.assert_not_called()
        resolve.assert_not_called()

    def test_live_session_mismatch_falls_back_to_selected_process(self):
        active_layout = SimpleNamespace(key="fm26")
        selected_layout = SimpleNamespace(key="fm24")
        initial_data_audit._PREFERRED_LAYOUT_KEY = "fm24"
        initial_data_audit._PREFERRED_PROCESS_PID = 24

        with (
            patch(
                "tools.game_session.active_game_read_selection",
                return_value=(26, "fm26.exe", active_layout),
            ),
            patch(
                "tools.initial_data_audit.select_process",
                return_value=(24, "fm24.exe"),
            ) as scan,
            patch(
                "tools.initial_data_audit.layout_for_executable",
                return_value=selected_layout,
            ) as resolve,
        ):
            selected = initial_data_audit.select_process_layout()

        self.assertEqual(selected, (24, "fm24.exe", selected_layout))
        scan.assert_called_once_with()
        resolve.assert_called_once_with("fm24.exe")

    def test_process_layout_cache_respects_selected_version(self):
        first_layout = SimpleNamespace(key="fm26")
        second_layout = SimpleNamespace(key="fm24")
        with (
            patch(
                "tools.game_session.active_game_read_selection",
                return_value=None,
            ),
            patch("tools.initial_data_audit.select_process", side_effect=[(123, "fm26.exe"), (456, "fm24.exe")]),
            patch("tools.initial_data_audit.layout_for_executable", side_effect=[first_layout, second_layout]),
        ):
            initial_data_audit._PREFERRED_LAYOUT_KEY = "fm26"
            first = initial_data_audit.select_process_layout()
            initial_data_audit._PREFERRED_LAYOUT_KEY = "fm24"
            second = initial_data_audit.select_process_layout()

        self.assertEqual(first, (123, "fm26.exe", first_layout))
        self.assertEqual(second, (456, "fm24.exe", second_layout))

    def test_process_layout_cache_respects_selected_pid(self):
        layout = SimpleNamespace(key="fm24")
        with (
            patch(
                "tools.initial_data_audit.select_process",
                side_effect=[(123, "steam/fm.exe"), (456, "epic/fm.exe")],
            ),
            patch("tools.initial_data_audit.layout_for_executable", return_value=layout),
        ):
            initial_data_audit._PREFERRED_LAYOUT_KEY = "fm24"
            initial_data_audit._PREFERRED_PROCESS_PID = 123
            first = initial_data_audit.select_process_layout()
            initial_data_audit._PREFERRED_PROCESS_PID = 456
            second = initial_data_audit.select_process_layout()

        self.assertEqual(first[0], 123)
        self.assertEqual(second[0], 456)

    def test_same_generation_processes_keep_distinct_pids(self):
        steam = SimpleNamespace(pid=123, path="steam/fm.exe", thread_count=8, name="fm.exe")
        epic = SimpleNamespace(pid=456, path="epic/fm.exe", thread_count=12, name="fm.exe")
        layouts = {
            steam.path: SimpleNamespace(
                key="fm24", display_name="Football Manager 2024",
                distribution="steam", game_version="24.4.2",
            ),
            epic.path: SimpleNamespace(
                key="fm24", display_name="Football Manager 2024",
                distribution="epic", game_version="24.4.2",
            ),
        }
        with (
            patch("tools.initial_data_audit.find_processes", return_value=[steam, epic]),
            patch(
                "tools.initial_data_audit.layout_for_executable",
                side_effect=lambda path: layouts[path],
            ),
            patch("tools.initial_data_audit.executable_file_version", return_value="24.4.2.0"),
        ):
            rows = initial_data_audit.running_supported_processes()

        self.assertEqual({row["pid"] for row in rows}, {123, 456})
        self.assertEqual({row["distribution"] for row in rows}, {"steam", "epic"})

    def test_status_keeps_pinned_same_generation_process(self):
        rows = [
            {"pid": 456, "path": "epic/fm.exe", "thread_count": 20,
             "layout": object(), "key": "fm24", "version": "24",
             "display_name": "FM24", "distribution": "epic", "file_version": "",
             "build_version": "", "build_label": "", "unity_version": "",
             "unity_label": ""},
            {"pid": 123, "path": "steam/fm.exe", "thread_count": 8,
             "layout": object(), "key": "fm24", "version": "24",
             "display_name": "FM24", "distribution": "steam", "file_version": "",
             "build_version": "", "build_label": "", "unity_version": "",
             "unity_label": ""},
        ]
        initial_data_audit._PREFERRED_LAYOUT_KEY = "fm24"
        initial_data_audit._PREFERRED_PROCESS_PID = 123
        with patch("tools.initial_data_audit.running_supported_processes", return_value=rows):
            status = initial_data_audit.process_selection_status()

        self.assertEqual(len(status["available"]), 1)
        self.assertEqual(status["selected_pid"], 123)
        self.assertEqual(status["available"][0]["distribution"], "steam")

    def test_status_can_return_verified_internal_selection_without_exposing_it(self):
        layout = SimpleNamespace(key="fm26")
        rows = [{
            "pid": 321, "path": "steam/fm.exe", "thread_count": 20,
            "layout": layout, "key": "fm26", "version": "26",
            "display_name": "FM26", "distribution": "steam", "file_version": "",
            "build_version": "", "build_label": "", "unity_version": "",
            "unity_label": "",
        }]
        with patch(
            "tools.initial_data_audit.running_supported_processes", return_value=rows,
        ):
            public = initial_data_audit.process_selection_status()
            internal = initial_data_audit.process_selection_status(include_internal=True)

        self.assertNotIn("_selected_process", public)
        self.assertEqual(
            internal["_selected_process"], (321, "steam/fm.exe", layout),
        )
        self.assertNotIn("path", internal["available"][0])
        self.assertNotIn("layout", internal["available"][0])

    def test_reselecting_same_process_preserves_session_and_refreshes_layout_cache(self):
        layout = SimpleNamespace(key="fm26")
        selected = (321, "steam/fm.exe", layout)
        initial_data_audit._PREFERRED_LAYOUT_KEY = "fm26"
        initial_data_audit._PREFERRED_PROCESS_PID = 321

        with patch(
            "tools.game_session.invalidate_game_read_sessions",
        ) as invalidate:
            changed = initial_data_audit.select_game_process(
                "fm26", 321, selected=selected,
            )

        self.assertFalse(changed)
        invalidate.assert_not_called()
        self.assertEqual(initial_data_audit._PROCESS_LAYOUT_CACHE[3], selected)

    def test_selecting_different_process_retires_sessions_once(self):
        layout = SimpleNamespace(key="fm24")
        selected = (456, "epic/fm.exe", layout)
        initial_data_audit._PREFERRED_LAYOUT_KEY = "fm26"
        initial_data_audit._PREFERRED_PROCESS_PID = 321

        with patch(
            "tools.game_session.invalidate_game_read_sessions",
        ) as invalidate:
            changed = initial_data_audit.select_game_process(
                "fm24", 456, selected=selected,
            )

        self.assertTrue(changed)
        invalidate.assert_called_once_with()
        self.assertEqual(initial_data_audit._PROCESS_LAYOUT_CACHE[3], selected)

    def test_same_pid_with_changed_layout_identity_retires_sessions(self):
        old_layout = SimpleNamespace(key="fm26", executable_sha256="old")
        new_layout = SimpleNamespace(key="fm26", executable_sha256="new")
        initial_data_audit._PREFERRED_LAYOUT_KEY = "fm26"
        initial_data_audit._PREFERRED_PROCESS_PID = 321
        initial_data_audit._PROCESS_LAYOUT_CACHE = (
            1.0, "fm26", 321, (321, "steam/fm.exe", old_layout),
        )

        with patch(
            "tools.game_session.invalidate_game_read_sessions",
        ) as invalidate:
            changed = initial_data_audit.select_game_process(
                "fm26", 321,
                selected=(321, "steam/fm.exe", new_layout),
            )

        self.assertTrue(changed)
        invalidate.assert_called_once_with()

    def test_select_process_uses_one_scan_and_preserves_pinned_pid(self):
        rows = [
            {"pid": 456, "path": "epic/fm.exe", "thread_count": 20, "key": "fm24", "version": "24"},
            {"pid": 123, "path": "steam/fm.exe", "thread_count": 8, "key": "fm24", "version": "24"},
        ]
        initial_data_audit._PREFERRED_LAYOUT_KEY = "fm24"
        initial_data_audit._PREFERRED_PROCESS_PID = 123
        with patch(
            "tools.initial_data_audit.running_supported_processes", return_value=rows,
        ) as scan:
            selected = initial_data_audit.select_process()

        self.assertEqual(selected, (123, "steam/fm.exe"))
        scan.assert_called_once_with()

    def test_missing_pinned_process_does_not_switch_to_peer(self):
        rows = [
            {"pid": 456, "path": "epic/fm.exe", "thread_count": 20, "key": "fm24", "version": "24"},
        ]
        initial_data_audit._PREFERRED_LAYOUT_KEY = "fm24"
        initial_data_audit._PREFERRED_PROCESS_PID = 123
        with patch("tools.initial_data_audit.running_supported_processes", return_value=rows):
            with self.assertRaisesRegex(RuntimeError, "selected fm.exe is not running"):
                initial_data_audit.select_process()

    def test_supported_process_classifies_unity_file_version(self):
        process = SimpleNamespace(pid=123, path="fm.exe", thread_count=8, name="fm.exe")
        layout = SimpleNamespace(
            key="fm26", display_name="Football Manager 26", distribution="steam",
            game_version="26.3.2",
        )
        with (
            patch("tools.initial_data_audit.find_processes", return_value=[process]),
            patch("tools.initial_data_audit.layout_for_executable", return_value=layout),
            patch(
                "tools.initial_data_audit.executable_file_version",
                return_value="6000.0.52.8888375",
            ),
        ):
            rows = initial_data_audit.running_supported_processes()

        self.assertEqual(rows[0]["file_version"], "6000.0.52.8888375")
        self.assertEqual(rows[0]["unity_version"], "6000.0.52.8888375")
        self.assertEqual(rows[0]["unity_label"], "6000.0.52")
        self.assertEqual(rows[0]["build_version"], "26.3.2")
        self.assertEqual(rows[0]["build_label"], "26.3.2")
        self.assertEqual(rows[0]["distribution"], "steam")

    def test_exact_fm_process_liveness_does_not_require_layout_classification(self):
        processes = [
            SimpleNamespace(pid=23712, name="fm.exe", thread_count=182, path=""),
            SimpleNamespace(pid=27704, name="fm.exe", thread_count=219, path=""),
        ]
        with patch("tools.initial_data_audit.find_processes", return_value=processes):
            self.assertTrue(initial_data_audit.fm_process_is_running(23712))
            self.assertFalse(initial_data_audit.fm_process_is_running(99999))

    def test_non_unity_file_version_remains_a_game_build(self):
        self.assertFalse(initial_data_audit.is_unity_engine_version("24.4.2.0"))
        self.assertTrue(initial_data_audit.is_unity_engine_version("6000.0.52.8888375"))

    def test_compact_build_version_removes_redundant_zero(self):
        self.assertEqual(initial_data_audit.compact_build_version("24.4.2.0"), "24.4.2")


if __name__ == "__main__":
    unittest.main()
