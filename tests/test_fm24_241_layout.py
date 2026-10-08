from __future__ import annotations

import unittest

from tools.fm24_process_diagnostic import KNOWN_BUILDS
from tools.game_layout import (
    FM24_240_LAYOUT,
    FM24_241_EXE_SHA256,
    FM24_241_LAYOUT,
    FM24_EPIC_LAYOUT,
    LAYOUT_BY_HASH,
    FM24_LAYOUT,
    FM24_XGP_LAYOUT,
)


class FM24241LayoutTests(unittest.TestCase):
    def test_exact_hash_selects_the_241_layout(self):
        self.assertIs(LAYOUT_BY_HASH[FM24_241_EXE_SHA256], FM24_241_LAYOUT)
        self.assertIn("24.4.1", KNOWN_BUILDS[FM24_241_EXE_SHA256])

    def test_probe_addresses_are_recorded_exactly(self):
        layout = FM24_241_LAYOUT
        self.assertEqual(layout.fixture_vtable_rva, 0x5A25F58)
        self.assertEqual(layout.fixture_result_vtable_rva, 0x5608078)
        self.assertEqual(layout.team_vtable_rva, 0x5A78848)
        self.assertEqual(layout.national_team_vtable_rva, 0x5A6CAB8)
        self.assertEqual(layout.nation_vtable_rva, 0x5A6CD98)
        self.assertEqual(layout.club_vtable_rva, 0x5A51F38)
        self.assertEqual(layout.competition_vtable_rva, 0x5A54A38)
        self.assertEqual(layout.actual_player_vtable_rvas, (0x5A4E958,))
        self.assertEqual(layout.player_and_non_player_vtable_rvas, (0x5C7E798,))
        self.assertEqual(layout.staff_person_vtable_rva, 0x5A4C358)
        self.assertEqual(layout.human_manager_vtable_rvas, (0x5A6A388,))
        self.assertEqual(layout.match_session_vtable_rva, 0x5836A90)
        self.assertEqual(layout.game_match_session_vtable_rva, 0x5604F08)
        self.assertEqual(layout.game_date_rva, 0x631D5BC)
        self.assertEqual(layout.redbull_hook_rva, 0x43B3FDF)
        self.assertEqual(layout.referee_hook_rva, 0x19DA4E2A)

    def test_player_staff_person_subobject_uses_verified_fm24_offset(self):
        for layout in (
            FM24_LAYOUT, FM24_240_LAYOUT, FM24_241_LAYOUT,
            FM24_EPIC_LAYOUT, FM24_XGP_LAYOUT,
        ):
            self.assertEqual(layout.player_and_non_player_person_offset, 0x368)

    def test_staff_abilities_use_the_verified_fm24_complete_object(self):
        for layout in (
            FM24_LAYOUT, FM24_240_LAYOUT, FM24_241_LAYOUT,
            FM24_EPIC_LAYOUT, FM24_XGP_LAYOUT,
        ):
            self.assertTrue(layout.staff_abilities_verified)
            self.assertEqual(layout.staff_ability_complete_object_offset, 0xF0)

    def test_game_match_session_viewer_identity_fails_closed_by_build(self):
        for layout in (FM24_LAYOUT, FM24_EPIC_LAYOUT, FM24_241_LAYOUT):
            self.assertEqual(layout.game_match_session_vtable_rva, 0x5604F08)
        self.assertIsNone(FM24_240_LAYOUT.game_match_session_vtable_rva)
        self.assertIsNone(FM24_XGP_LAYOUT.game_match_session_vtable_rva)


if __name__ == "__main__":
    unittest.main()
