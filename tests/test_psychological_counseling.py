from __future__ import annotations

import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import patch

from tools.club_economy import (
    perform_psychological_counseling,
    psychological_counseling_outcome,
)
from tools.club_reader import PERSON_CONTRACT, _fm24_unhappiness_records, _person_has_unhappiness


class _Fm24UnhappinessReader:
    def __init__(self) -> None:
        self.layout = SimpleNamespace(key="fm24")
        self.pointers = {
            0x1000 + 0xC8: 0x2000,
            0x2000 + 0x08: 0x1000,
            0x2000 + 0x10: 0x3000,
            0x2000 + 0x28: 0x4000,
            0x4000 + 0x08: 0x5000,
        }
        self.values = {
            0x4000: 1,
            0x5000: (2023 << 16) | 295,
            0x5000 + 0x04: (1900 << 16) | 1,
        }

    def ptr(self, address: int) -> int:
        return self.pointers.get(address, 0)

    def u32(self, address: int) -> int | None:
        return self.values.get(address)


class PsychologicalCounselingTests(unittest.TestCase):
    def test_fm24_unhappiness_reads_active_contract_record(self) -> None:
        _contract, records = _fm24_unhappiness_records(
            _Fm24UnhappinessReader(), 0x1000, date(2023, 10, 23),
        )
        self.assertEqual(len(records), 1)
        self.assertTrue(records[0]["active"])
        self.assertEqual(records[0]["init_date"], date(2023, 10, 22))

    def test_roster_unhappiness_status_reuses_validated_person_contract(self) -> None:
        reader = _Fm24UnhappinessReader()
        self.assertTrue(
            _person_has_unhappiness(reader, 0x1000, date(2023, 10, 23))
        )

        fm26 = _Fm24UnhappinessReader()
        fm26.layout = SimpleNamespace(key="fm26")
        fm26.pointers = {
            0x1000 + PERSON_CONTRACT: 0x2000,
            0x2000 + 0x08: 0x1000,
            0x2000 + 0x10: 0x3000,
            0x2000 + 0x30: 0x4000,
        }
        self.assertTrue(_person_has_unhappiness(fm26, 0x1000, None))

    def test_probability_boundaries(self) -> None:
        self.assertEqual(psychological_counseling_outcome(0.0), "relieved")
        self.assertEqual(psychological_counseling_outcome(0.399999), "relieved")
        self.assertEqual(psychological_counseling_outcome(0.4), "opened_up")
        self.assertEqual(psychological_counseling_outcome(0.65), "no_effect")
        self.assertEqual(psychological_counseling_outcome(0.95), "rupture")
        self.assertEqual(psychological_counseling_outcome(0.999999), "rupture")

    def test_opened_up_preserves_fractional_intimacy(self) -> None:
        state = {
            "owned_activity_centres": ["talk_room"],
            "owned_activity_facilities": [],
            "activity_floor_cooldowns": {},
            "psychological_counseling": {},
            "activity_progress": {},
            "activity_history": [],
        }
        with patch("tools.club_economy.load_economy", return_value=state), patch(
            "tools.club_economy.save_economy"
        ):
            result = perform_psychological_counseling(
                1, 2, "球员", "2028-01-01", 20,
                lambda outcome, points: {
                    "before": 20, "after": 20, "applied": 0,
                    "effect": {"field": outcome, "effects": []},
                },
                roll=0.5,
            )
        self.assertEqual(result["outcome"], "opened_up")
        self.assertEqual(result["fractional_remainder"], 0.5)
        self.assertEqual(state["activity_progress"]["1:2"], 0.5)

    def test_paid_unlock_forces_relief_outcome(self) -> None:
        state = {
            "owned_activity_centres": ["talk_room"],
            "owned_activity_facilities": [],
            "activity_floor_cooldowns": {},
            "psychological_counseling": {},
            "activity_progress": {},
            "activity_history": [],
        }
        applied = []
        with patch("tools.club_economy.load_economy", return_value=state), patch(
            "tools.club_economy.save_economy"
        ):
            result = perform_psychological_counseling(
                1, 2, "球员", "2028-01-01", 20,
                lambda outcome, points: applied.append((outcome, points)) or {
                    "before": 20, "after": 21, "applied": points,
                    "effect": {"field": outcome, "effects": [
                        {"field": "unhappiness", "after": False},
                    ]},
                },
                roll=0.99, forced_outcome="unlocked",
            )
        self.assertEqual(result["outcome"], "unlocked")
        self.assertEqual(applied, [("unlocked", 0)])
        self.assertFalse(result["effect"]["effects"][0]["after"])

    def test_rupture_uses_shared_two_day_floor_cooldown(self) -> None:
        state = {
            "owned_activity_centres": ["talk_room"],
            "owned_activity_facilities": [],
            "activity_floor_cooldowns": {},
            "psychological_counseling": {},
            "activity_progress": {},
            "activity_history": [],
        }
        with patch("tools.club_economy.load_economy", return_value=state), patch(
            "tools.club_economy.save_economy"
        ):
            result = perform_psychological_counseling(
                1, 2, "球员", "2028-01-01", 20,
                lambda outcome, points: {
                    "before": 20, "after": 19, "applied": points,
                    "effect": {"field": outcome, "effects": []},
                },
                roll=0.99,
            )
            self.assertEqual(result["cooldown_until"], "2028-01-03")
            with self.assertRaisesRegex(ValueError, "2F活动冷却至 2028-01-03"):
                perform_psychological_counseling(
                    1, 2, "球员", "2028-01-02", 19,
                    lambda _outcome, _points: {}, roll=0.0,
                )


if __name__ == "__main__":
    unittest.main()
