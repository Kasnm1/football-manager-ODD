from __future__ import annotations

import struct
import unittest
from datetime import date
from unittest.mock import patch

from tools.club_economy import _new_state, process_medical_treatments
from tools.club_reader import _sync_medical_unavailability_records


def _encoded_date(value: date) -> int:
    return (value.year << 16) | value.timetuple().tm_yday


class _UnavailabilityReader:
    def __init__(self) -> None:
        self.container = 0x1000
        self.begin = 0x2000
        self.records = (0x3000, 0x4000, 0x5000, 0x6000)
        self.pointers = {
            self.container + 0x18: self.begin,
            self.container + 0x20: self.begin + len(self.records) * 8,
            **{
                self.begin + index * 8: record
                for index, record in enumerate(self.records)
            },
        }
        self.scopes = {
            self.records[0] + 0x05: 29,
            self.records[1] + 0x05: 15,
            self.records[2] + 0x05: 29,
            self.records[3] + 0x05: 29,
        }
        self.dates = {
            record + 0x10: _encoded_date(date(2028, 1, day))
            for record, day in zip(self.records, (1, 1, 1, 2), strict=True)
        }
        self.days = {
            self.records[0] + 0x16: 30,
            self.records[1] + 0x16: 30,
            self.records[2] + 0x16: 0xFFFF,
            self.records[3] + 0x16: 30,
        }

    def ptr(self, address: int) -> int | None:
        return self.pointers.get(address)

    def u8(self, address: int) -> int | None:
        return self.scopes.get(address)

    def u16(self, address: int) -> int | None:
        return self.days.get(address)

    def u32(self, address: int) -> int | None:
        return self.dates.get(address)

    def bytes(self, address: int, size: int) -> bytes | None:
        if size == 2 and address in self.days:
            return struct.pack("<H", self.days[address])
        return None


class MedicalUnavailabilitySyncTests(unittest.TestCase):
    def test_sync_only_updates_matching_finite_medical_record(self) -> None:
        reader = _UnavailabilityReader()

        def write(_process: object, address: int, raw: bytes) -> None:
            reader.days[address] = struct.unpack("<H", raw)[0]

        with patch("tools.club_reader.write_process_memory", side_effect=write):
            result = _sync_medical_unavailability_records(
                object(), reader, reader.container,
                {"2028-01-01": date(2028, 1, 10)},
            )

        self.assertEqual(result[0]["after_days"], 9)
        self.assertEqual(reader.days[reader.records[0] + 0x16], 9)
        self.assertEqual(reader.days[reader.records[1] + 0x16], 30)
        self.assertEqual(reader.days[reader.records[2] + 0x16], 0xFFFF)
        self.assertEqual(reader.days[reader.records[3] + 0x16], 30)

    def test_completion_releases_active_legacy_medical_records_only(self) -> None:
        reader = _UnavailabilityReader()

        def write(_process: object, address: int, raw: bytes) -> None:
            reader.days[address] = struct.unpack("<H", raw)[0]

        with patch("tools.club_reader.write_process_memory", side_effect=write):
            _sync_medical_unavailability_records(
                object(), reader, reader.container, {},
                default_return=date(2028, 1, 10),
            )

        self.assertEqual(reader.days[reader.records[0] + 0x16], 9)
        self.assertEqual(reader.days[reader.records[1] + 0x16], 30)
        self.assertEqual(reader.days[reader.records[2] + 0x16], 0xFFFF)
        self.assertEqual(reader.days[reader.records[3] + 0x16], 8)

    def test_completion_callback_runs_before_treatment_is_removed(self) -> None:
        state = _new_state()
        treatment = {
            "player_id": 7,
            "player_name": "Test Player",
            "mode": "conservative",
            "factor": 1.5,
            "injury_start_dates": ["2028-01-01"],
        }
        state["medical_treatments"]["7"] = treatment
        observed: list[bool] = []

        def complete(_treatment: dict[str, object]) -> None:
            observed.append("7" in state["medical_treatments"])

        with patch("tools.club_economy.load_economy", return_value=state), patch(
            "tools.club_economy.save_economy"
        ) as save, patch(
            "tools.club_economy.load_wallet", return_value={"balance": 0.0},
        ):
            result = process_medical_treatments(
                "2028-01-10", set(), lambda _state: None, complete,
            )

        self.assertEqual(observed, [True])
        self.assertNotIn("7", state["medical_treatments"])
        self.assertEqual(result["completed"], [7])
        save.assert_called_once_with(state)

    def test_unresolved_player_waits_instead_of_being_marked_recovered(self) -> None:
        state = _new_state()
        treatment = {
            "player_id": 7,
            "player_name": "National Player",
            "mode": "conservative",
            "factor": 1.5,
            "player_address": "0x7000",
            "last_charged_date": "2028-01-01",
        }
        state["medical_treatments"]["7"] = treatment
        completed: list[int] = []

        with patch("tools.club_economy.load_economy", return_value=state), patch(
            "tools.club_economy.save_economy"
        ) as save:
            result = process_medical_treatments(
                "2028-01-10", set(), lambda _state: None,
                lambda row: completed.append(int(row["player_id"])),
                resolved_player_ids=set(),
            )

        self.assertIn("7", state["medical_treatments"])
        self.assertEqual(completed, [])
        self.assertEqual(result["pending"], [7])
        self.assertEqual(result["completed"], [])
        save.assert_not_called()


if __name__ == "__main__":
    unittest.main()
