from __future__ import annotations

import struct
import threading
import unittest
from contextlib import nullcontext
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import fm_odds_web
from tools.initial_data_audit import ENTITY_UID, decode_date
from tools.retirement import (
    CHECK_DATE_OFFSET,
    RETIREMENT_PERSON_OFFSET,
    FM26_RETIREMENT_OWNER_RVA,
    RETIREMENT_DATE_OFFSET,
    STATE_OFFSET,
    attach_retirement_details,
    restore_retirement_plan,
    update_retirement_plan,
    _one_year_before,
    _record_index,
    _replace_date,
    _retirement_target_date,
)


class RetirementDateTests(unittest.TestCase):
    def test_real_date_replacement_preserves_non_date_flags(self) -> None:
        original = 0x07ED1B55
        replaced = struct.unpack("<I", _replace_date(original, date(2029, 12, 15)))[0]
        self.assertEqual(decode_date(replaced), date(2029, 12, 15))
        self.assertEqual(replaced & 0xFE00, original & 0xFE00)

    def test_check_date_is_one_calendar_year_earlier(self) -> None:
        self.assertEqual(_one_year_before(date(2029, 12, 15)), date(2028, 12, 15))
        self.assertEqual(_one_year_before(date(2028, 2, 29)), date(2027, 2, 28))

    def test_fixed_retirement_options_use_calendar_intervals(self) -> None:
        self.assertEqual(
            _retirement_target_date(date(2027, 8, 31), "six_months"),
            date(2028, 2, 29),
        )
        self.assertEqual(
            _retirement_target_date(date(2028, 2, 29), "two_years"),
            date(2030, 2, 28),
        )
        self.assertEqual(
            _retirement_target_date(date(2027, 8, 31), "ten_years"),
            date(2037, 8, 31),
        )
        self.assertEqual(
            _retirement_target_date(date(2027, 8, 31), "twenty_years"),
            date(2047, 8, 31),
        )

    def test_six_month_option_reactivates_cancelled_fm26_record(self) -> None:
        module_base = 0x50000000
        person = 0x2000
        record = 0x3000
        internal_key = 77
        layout = SimpleNamespace(
            key="fm26",
            module_name="game_plugin.dll",
            game_date_rva=0x100,
            module=lambda _process: SimpleNamespace(base_address=module_base),
        )

        def encoded(value: date) -> bytes:
            return struct.pack("<I", (value.year << 16) | value.timetuple().tm_yday)

        memory = {
            record + RETIREMENT_DATE_OFFSET: encoded(date(2030, 6, 30)),
            record + CHECK_DATE_OFFSET: encoded(date(2029, 6, 30)),
            record + STATE_OFFSET: struct.pack("<H", 0x80),
        }

        class FakeReader:
            def __init__(self, *_args):
                self.layout = layout
                self.module_base = module_base

            def u32(self, address):
                if address == person + ENTITY_UID:
                    return 123
                if address == person + 8 or address == record:
                    return internal_key
                if address == module_base + layout.game_date_rva:
                    return (2028 << 16) | date(2028, 8, 31).timetuple().tm_yday
                raw = memory.get(address)
                return struct.unpack("<I", raw)[0] if raw and len(raw) >= 4 else None

            def u16(self, address):
                raw = memory.get(address)
                return struct.unpack("<H", raw)[0] if raw and len(raw) >= 2 else None

            def bytes(self, address, size):
                raw = memory.get(address)
                return raw[:size] if raw and len(raw) >= size else None

        def write(_process, address, raw):
            memory[address] = bytes(raw)

        with (
            patch("tools.retirement.select_process_layout", return_value=(1, "fm.exe", layout)),
            patch("tools.retirement.open_process", return_value=nullcontext(object())),
            patch("tools.retirement.Reader", FakeReader),
            patch("tools.retirement._player_person", return_value=person),
            patch("tools.retirement._record_index", return_value={internal_key: [record]}),
            patch("tools.retirement.write_process_memory", side_effect=write),
        ):
            result = update_retirement_plan(123, "0x1000", action="six_months")

        self.assertEqual(result["retirement_date"], "2029-02-28")
        self.assertEqual(result["check_date"], "2029-06-30")
        self.assertFalse(result["cancelled"])
        self.assertEqual(memory[record + STATE_OFFSET], b"\x00\x00")

    def test_cancel_clears_duplicate_native_records_for_same_player(self) -> None:
        module_base = 0x50000000
        person = 0x2000
        records = [0x3000, 0x4000]
        internal_key = 77
        layout = SimpleNamespace(
            key="fm24", module_name="fm.exe", game_date_rva=0x100,
            module=lambda _process: SimpleNamespace(base_address=module_base),
        )

        def encoded(value: date) -> bytes:
            return struct.pack("<I", (value.year << 16) | value.timetuple().tm_yday)

        memory = {
            record + RETIREMENT_PERSON_OFFSET: struct.pack("<I", 150)
            for record in records
        }
        memory.update({
            record + RETIREMENT_DATE_OFFSET: encoded(date(2030, 6, 30))
            for record in records
        })
        memory[person + 0x18] = struct.pack("<I", 0x03)
        memory.update({
            record + CHECK_DATE_OFFSET: encoded(date(2029, 6, 30))
            for record in records
        })
        memory.update({record + STATE_OFFSET: b"\x00\x00" for record in records})

        class FakeReader:
            def __init__(self, *_args):
                self.layout = layout
                self.module_base = module_base

            def u32(self, address):
                if address == person + ENTITY_UID:
                    return 123
                if address == person + 8 or address in records:
                    return internal_key
                if address == module_base + layout.game_date_rva:
                    return (2028 << 16) | date(2028, 8, 31).timetuple().tm_yday
                raw = memory.get(address)
                return struct.unpack("<I", raw)[0] if raw and len(raw) >= 4 else None

            def u16(self, address):
                raw = memory.get(address)
                return struct.unpack("<H", raw)[0] if raw and len(raw) >= 2 else None

            def bytes(self, address, size):
                raw = memory.get(address)
                return raw[:size] if raw and len(raw) >= size else None

        def write(_process, address, raw):
            memory[address] = bytes(raw)

        with (
            patch("tools.retirement.select_process_layout", return_value=(1, "fm.exe", layout)),
            patch("tools.retirement.open_process", return_value=nullcontext(object())),
            patch("tools.retirement.Reader", FakeReader),
            patch("tools.retirement._player_person", return_value=person),
            patch("tools.retirement._record_index", return_value={internal_key: records}),
            patch("tools.retirement.write_process_memory", side_effect=write),
        ):
            result = update_retirement_plan(123, "0x1000", action="cancel")

        self.assertTrue(result["cancelled"])
        self.assertTrue(all(memory[record + STATE_OFFSET] == b"\x80\x00" for record in records))
        self.assertTrue(all(memory[record + RETIREMENT_PERSON_OFFSET] == b"\x00\x00\x00\x00" for record in records))
        self.assertTrue(all(memory[record + RETIREMENT_DATE_OFFSET] == b"\x00\x00\x00\x00" for record in records))
        self.assertTrue(all(memory[record + CHECK_DATE_OFFSET] == b"\x00\x00\x00\x00" for record in records))
        self.assertEqual(memory[person + 0x18], struct.pack("<I", 0x02))

    def test_cancel_snapshot_restores_only_when_cancelled_values_are_unchanged(self) -> None:
        module_base = 0x50000000
        person = 0x2000
        layout = SimpleNamespace(
            key="fm26", module_name="game_plugin.dll",
            module=lambda _process: SimpleNamespace(base_address=module_base),
        )
        first_address = 0x3010
        second_address = 0x3014
        snapshot = [
            (first_address, b"old1", b"\x00\x00\x00\x00"),
            (second_address, b"\x00\x00", b"\x80\x00"),
        ]
        memory = {
            first_address: b"\x00\x00\x00\x00",
            second_address: b"\x80\x00",
        }

        class FakeReader:
            def __init__(self, *_args):
                self.layout = layout

            def u32(self, address):
                return 123 if address == person + ENTITY_UID else None

            def bytes(self, address, size):
                raw = memory.get(address)
                return raw[:size] if raw and len(raw) >= size else None

        def write(_process, address, raw):
            memory[address] = bytes(raw)

        with (
            patch("tools.retirement.select_process_layout", return_value=(1, "fm.exe", layout)),
            patch("tools.retirement.open_process", return_value=nullcontext(object())),
            patch("tools.retirement.Reader", FakeReader),
            patch("tools.retirement._player_person", return_value=person),
            patch("tools.retirement.write_process_memory", side_effect=write),
        ):
            restore_retirement_plan(123, "0x1000", snapshot)
            self.assertEqual(memory[first_address], b"old1")
            self.assertEqual(memory[second_address], b"\x00\x00")

            memory[first_address] = b"busy"
            with self.assertRaisesRegex(RuntimeError, "已被其他操作改变"):
                restore_retirement_plan(123, "0x1000", snapshot)

    def test_optional_cancel_is_a_noop_when_player_has_no_retirement_record(self) -> None:
        module_base = 0x50000000
        person = 0x2000
        internal_key = 77
        layout = SimpleNamespace(
            key="fm26", module_name="game_plugin.dll", game_date_rva=0x100,
            module=lambda _process: SimpleNamespace(base_address=module_base),
        )

        class FakeReader:
            def __init__(self, *_args):
                self.layout = layout
                self.module_base = module_base

            def u32(self, address):
                if address == person + ENTITY_UID:
                    return 123
                if address == person + 8:
                    return internal_key
                if address == module_base + layout.game_date_rva:
                    return (2028 << 16) | date(2028, 8, 31).timetuple().tm_yday
                return None

            @staticmethod
            def bytes(_address, _size):
                return None

        with (
            patch("tools.retirement.select_process_layout", return_value=(1, "fm.exe", layout)),
            patch("tools.retirement.open_process", return_value=nullcontext(object())),
            patch("tools.retirement.Reader", FakeReader),
            patch("tools.retirement._player_person", return_value=person),
            patch("tools.retirement._record_index", return_value={}),
            patch("tools.retirement.write_process_memory") as write,
        ):
            result = update_retirement_plan(
                123, "0x1000", action="cancel",
                capture_rollback=True, missing_ok=True,
            )

        self.assertEqual(result["has_plan"], False)
        self.assertEqual(result["_rollback"], [])
        write.assert_not_called()

    def test_verified_steam_index_does_not_heap_scan_for_players_without_plans(self) -> None:
        module_base = 0x100000
        owner = 0x200000
        vector = 0x300000
        pointers = {
            module_base + FM26_RETIREMENT_OWNER_RVA: owner,
            owner + 0x90: vector,
            owner + 0x98: vector,
            owner + 0xA0: vector,
        }
        reader = SimpleNamespace(
            layout=SimpleNamespace(key="fm26", distribution="steam"),
            module_base=module_base,
            process=object(),
            ptr=lambda address: pointers.get(address),
            bytes=lambda _address, _size: b"",
        )

        with patch(
            "tools.retirement.iter_readable_regions",
            side_effect=AssertionError("authoritative Steam index must not heap scan"),
        ):
            self.assertEqual(_record_index(reader, {12345}), {})

    def test_club_refresh_never_uses_retirement_heap_fallback(self) -> None:
        reader = MagicMock()
        reader.u32.side_effect = lambda address: {
            0x1008: 101,
            0x100C: 1,
            0x2008: 202,
            0x200C: 2,
        }.get(address)
        players = [
            {"id": 1, "address": "0x1000", "age": 31},
            {"id": 2, "address": "0x2000", "age": 29},
        ]

        with (
            patch("tools.retirement._player_person", side_effect=lambda _reader, address: address),
            patch("tools.retirement._record_index", return_value={}) as record_index,
        ):
            attach_retirement_details(reader, players)

        record_index.assert_called_once_with(reader, {101, 202}, allow_fallback=False)
        self.assertEqual(players[0]["retirement"], {"has_plan": False})
        self.assertEqual(players[1]["retirement"], {"has_plan": False})


class RetirementActivityTests(unittest.TestCase):
    def test_live_profile_resolves_related_squad_retirement_player(self) -> None:
        state = fm_odds_web.LocalOddsState.__new__(fm_odds_web.LocalOddsState)
        state.lock = threading.RLock()
        youth_player = {
            "id": 456,
            "name": "青年队退役球员",
            "address": "0x3000",
            "squad_team_id": 2200,
            "squad_team_address": "0x2000",
            "retirement": {"has_plan": True, "cancelled": False},
        }
        profile = {
            "team": {
                "id": 1190, "name": "测试俱乐部", "team_type": "club",
                "address": "0x1000",
            },
            "players": [],
            "squads": [{
                "team_id": 2200,
                "team_address": "0x2000",
                "players": [youth_player],
            }],
        }
        state.club_profiles = {1190: profile}
        state.club_profile = profile
        state.output = {
            "managed_teams": [{
                "id": 1190, "team_type": "club", "address": "0x1000",
            }],
        }

        with patch.object(
            fm_odds_web, "resolve_team_player_address", return_value="0x3333",
        ) as resolve:
            found_profile, found_player = state._live_player_profile(456, 1190)

        self.assertIs(found_profile, profile)
        self.assertIs(found_player, youth_player)
        self.assertEqual(found_player["address"], "0x3333")
        resolve.assert_called_once_with(2200, "0x2000", 456, "0x3000")

    def test_schedule_allows_under_30_player_with_active_plan(self) -> None:
        state = fm_odds_web.LocalOddsState.__new__(fm_odds_web.LocalOddsState)
        state.memory_lock = threading.RLock()
        state.lock = threading.RLock()
        state.output = {
            "manager": {"id": 9},
            "selected_manager_id": 9,
            "game_date": "2028-08-31",
        }
        state._bind_current_save = lambda: None
        player = {
            "id": 123,
            "name": "测试球员",
            "age": 27,
            "address": "0x1000",
            "retirement": {
                "has_plan": True,
                "retirement_date": "2030-06-30",
                "cancelled": False,
            },
        }
        state._live_player_profile = lambda _player_id, _team_id=0: ({}, player)
        updated = {
            "has_plan": True,
            "retirement_date": "2029-02-28",
            "check_date": "2028-02-28",
            "cancelled": False,
            "state": 0,
        }

        with (
            patch.object(fm_odds_web, "assert_activity_facility_unlocked"),
            patch.object(fm_odds_web, "assert_activity_floor_available"),
            patch.object(fm_odds_web, "read_game_clock", return_value={"date": "2028-08-31"}),
            patch.object(fm_odds_web, "record_activity_floor_cooldown"),
            patch.object(fm_odds_web, "public_economy", return_value={}),
            patch.object(fm_odds_web, "welfare_status", return_value={}),
            patch.object(fm_odds_web, "update_retirement_plan", return_value=updated) as update,
        ):
            result = state.retirement_activity({"player_id": 123, "action": "six_months"})

        update.assert_called_once_with(123, "0x1000", action="six_months")
        self.assertTrue(result["modified"])
        self.assertEqual(result["retirement"], updated)

    def test_schedule_rejects_player_without_active_retirement_plan(self) -> None:
        state = fm_odds_web.LocalOddsState.__new__(fm_odds_web.LocalOddsState)
        state.memory_lock = threading.RLock()
        state.lock = threading.RLock()
        state.output = {"manager": {"id": 9}, "selected_manager_id": 9}
        state._bind_current_save = lambda: None
        player = {
            "id": 123,
            "name": "测试球员",
            "age": 34,
            "address": "0x1000",
            "retirement": {"has_plan": False},
        }
        state._live_player_profile = lambda _player_id, _team_id=0: ({}, player)

        with (
            patch.object(fm_odds_web, "assert_activity_facility_unlocked"),
            patch.object(fm_odds_web, "update_retirement_plan") as update,
        ):
            with self.assertRaisesRegex(ValueError, "显示退役或已有退役计划"):
                state.retirement_activity({"player_id": 123, "action": "six_months"})

        update.assert_not_called()

    def test_cancel_allows_legacy_cancelled_record_with_date(self) -> None:
        state = fm_odds_web.LocalOddsState.__new__(fm_odds_web.LocalOddsState)
        state.memory_lock = threading.RLock()
        state.lock = threading.RLock()
        state.output = {"manager": {"id": 9}, "selected_manager_id": 9, "game_date": "2028-08-31"}
        state._bind_current_save = lambda: None
        player = {
            "id": 123, "name": "测试球员", "address": "0x1000",
            "retirement": {"has_plan": True, "retirement_date": "2026-07-01", "cancelled": True},
        }
        state._live_player_profile = lambda _player_id, _team_id=0: ({}, player)
        updated = {"has_plan": False, "cancelled": True, "state": 0x80}
        with (
            patch.object(fm_odds_web, "assert_activity_facility_unlocked"),
            patch.object(fm_odds_web, "assert_activity_floor_available"),
            patch.object(fm_odds_web, "read_game_clock", return_value={"date": "2028-08-31"}),
            patch.object(fm_odds_web, "record_activity_floor_cooldown"),
            patch.object(fm_odds_web, "public_economy", return_value={}),
            patch.object(fm_odds_web, "welfare_status", return_value={}),
            patch.object(fm_odds_web, "update_retirement_plan", return_value=updated) as update,
        ):
            result = state.retirement_activity({"player_id": 123, "action": "cancel"})
        update.assert_called_once_with(123, "0x1000", action="cancel")
        self.assertEqual(result["retirement"], updated)

    def test_frontend_offers_only_three_fixed_retirement_actions(self) -> None:
        script = ((Path(__file__).resolve().parents[1] / "src") / "web" / "app.js").read_text(
            encoding="utf-8"
        )

        self.assertIn('<option value="cancel">取消退役计划</option>', script)
        self.assertIn('<option value="six_months">半年后退役</option>', script)
        self.assertIn('<option value="two_years">两年后退役</option>', script)
        self.assertIn('<option value="ten_years">十年后退役</option>', script)
        self.assertIn('<option value="twenty_years">二十年后退役</option>', script)
        self.assertNotIn('id="activity-retirement-date"', script)
        self.assertNotIn('id="retirement-date"', script)

    def test_frontend_labels_detected_retirement_plan_states(self) -> None:
        script = ((Path(__file__).resolve().parents[1] / "src") / "web" / "app.js").read_text(
            encoding="utf-8"
        )

        self.assertIn('计划退役：${retirementDate}', script)
        self.assertIn('退役计划已取消', script)
        self.assertIn('暂无退役计划', script)
        self.assertIn('退役计划未识别', script)
        self.assertIn("player.retirement.has_plan", script)
        self.assertIn("!player.retirement.cancelled || player.retirement.retirement_date", script)
        self.assertNotIn('player.age != null && Number(player.age) >= 30', script)


if __name__ == "__main__":
    unittest.main()
