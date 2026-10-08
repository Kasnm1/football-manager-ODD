from __future__ import annotations

import struct
import unittest
from contextlib import nullcontext
from unittest.mock import patch

from tools import goalkeeper_bribe
from tools.redbull_hook import (
    FM24_HOOK_PATTERN,
    GOALKEEPER_HIGH_ATTRIBUTE_IDS,
    GOALKEEPER_LOW_ATTRIBUTE_IDS,
    GOALKEEPER_TABLE_OFFSET,
    HOOK_PATTERN,
    MAX_PLAYERS,
    STATE_OFFSET,
    TABLE_OFFSET,
    RedBullHookController,
    _build_code,
    _build_fm24_code,
)


class GoalkeeperMatchHookTests(unittest.TestCase):
    def test_fm26_code_uses_ce_attribute_output_register(self):
        original = bytes(value or 0 for value in HOOK_PATTERN[:9])
        code = _build_code(0x10000000, original, 0x10002000)

        self.assertTrue(code.startswith(original))
        self.assertLess(len(code), STATE_OFFSET)
        self.assertIn(b"\x41\xBE\x05\x00\x00\x00", code)
        self.assertIn(b"\x41\xBE\x64\x00\x00\x00", code)
        self.assertIn(b"\xF0\x48\xFF\x00", code)
        self.assertIn(b"\xF0\x48\xFF\x08", code)
        for attribute_id in (*GOALKEEPER_LOW_ATTRIBUTE_IDS, *GOALKEEPER_HIGH_ATTRIBUTE_IDS):
            self.assertIn(b"\x3C" + bytes([attribute_id]) + b"\x0F\x84", code)

    def test_fm24_code_uses_trainer_edi_output_register(self):
        original = bytes(value or 0 for value in FM24_HOOK_PATTERN)
        code = _build_fm24_code(0x10000000, original, 0x10002000)

        self.assertTrue(code.startswith(original))
        self.assertLess(len(code), STATE_OFFSET)
        self.assertIn(b"\xBF\x05\x00\x00\x00", code)
        self.assertIn(b"\xBF\x64\x00\x00\x00", code)

    def test_tables_fit_in_single_code_page(self):
        table_size = 4 + MAX_PLAYERS * 4
        self.assertLess(STATE_OFFSET + 8, TABLE_OFFSET)
        self.assertLess(TABLE_OFFSET + table_size, GOALKEEPER_TABLE_OFFSET)
        self.assertLessEqual(GOALKEEPER_TABLE_OFFSET + table_size, 0x1000)

    def test_target_update_disarms_counts_before_writing_ids(self):
        controller = RedBullHookController()
        controller.pid = 123
        controller.cave = 0x10000000
        writes: list[tuple[int, bytes]] = []

        with (
            patch("tools.redbull_hook.open_process", return_value=nullcontext(object())),
            patch(
                "tools.redbull_hook.write_process_memory",
                side_effect=lambda _process, address, data: writes.append((address, data)),
            ),
        ):
            controller._write_targets([11, 22], [33, 44])

        stamina_table = controller.cave + TABLE_OFFSET
        goalkeeper_table = controller.cave + GOALKEEPER_TABLE_OFFSET
        self.assertEqual(writes[0], (stamina_table, struct.pack("<I", 0)))
        self.assertEqual(writes[1], (goalkeeper_table, struct.pack("<I", 0)))
        self.assertIn((stamina_table + 4, struct.pack("<II", 11, 22)), writes)
        self.assertIn((goalkeeper_table + 4, struct.pack("<II", 33, 44)), writes)
        self.assertEqual(controller.targets, [11, 22])
        self.assertEqual(controller.goalkeeper_targets, [33, 44])

    def test_sync_applies_direct_attribute_block_without_hook_targets(self):
        item = {
            "id": "item-1",
            "fixture_key": "fixture-1",
            "fixture_name": "Home v Away",
            "opponent_team_id": 99,
            "opponent_team_address": "0x1234",
        }
        resolved = {
            "team_id": 99,
            "team_name": "Away",
            "team_address": "0x5678",
            "players": [
                {"player_id": 20, "player_name": "GK Two", "original_attributes": "00" * 54},
                {"player_id": 10, "player_name": "GK One", "original_attributes": "00" * 54},
            ],
        }
        with (
            patch.object(goalkeeper_bribe, "_load", return_value={"schema_version": 2, "effects": {}}),
            patch.object(goalkeeper_bribe, "_save") as save,
            patch.object(
                goalkeeper_bribe, "apply_goalkeeper_bribe", return_value=resolved,
            ) as apply_state,
            patch.object(goalkeeper_bribe, "restore_goalkeeper_bribe") as restore,
        ):
            result = goalkeeper_bribe.sync_goalkeeper_bribes([item])

        self.assertEqual(result["mode"], "direct_attribute_block")
        self.assertNotIn("player_ids", result)
        apply_state.assert_called_once_with(99, "0x1234")
        restore.assert_not_called()
        save.assert_called_once()

    def test_sync_restores_inactive_direct_snapshot(self):
        state = {
            "schema_version": 1,
            "effects": {
                "legacy": {
                    "opponent_team_id": 99,
                    "opponent_team_address": "0x1234",
                    "players": [{"player_id": 10, "original_attributes": "00" * 54}],
                },
            },
        }
        with (
            patch.object(goalkeeper_bribe, "_load", return_value=state),
            patch.object(goalkeeper_bribe, "_save") as save,
            patch.object(
                goalkeeper_bribe,
                "restore_goalkeeper_bribe",
                return_value={
                    "restored": [10], "remaining": [], "errors": [],
                    "team_address": "0x1234",
                },
            ) as restore,
        ):
            result = goalkeeper_bribe.sync_goalkeeper_bribes([])

        self.assertEqual(result["tracked"], 0)
        self.assertEqual(state["schema_version"], 4)
        self.assertEqual(state["effects"], {})
        restore.assert_called_once()
        save.assert_called_once_with(state)

    def test_active_hook_snapshot_is_restored_then_reapplied_directly(self):
        hook_player = {
            "player_id": 10,
            "player_name": "GK One",
            "player_address": "0x1000",
            "original_sharpness": 6400,
        }
        direct_player = {
            "player_id": 10,
            "player_name": "GK One",
            "player_address": "0x1000",
            "original_attributes": "00" * 54,
        }
        state = {
            "schema_version": 3,
            "effects": {
                "item-1": {
                    "item_id": "item-1",
                    "opponent_team_id": 99,
                    "opponent_team_address": "0x1234",
                    "players": [hook_player],
                },
            },
        }
        item = {
            "id": "item-1",
            "fixture_key": "fixture-1",
            "opponent_team_id": 99,
            "opponent_team_address": "0x1234",
        }
        applied = {
            "team_id": 99,
            "team_name": "Away",
            "team_address": "0x5678",
            "players": [direct_player],
            "process_session": "1:fm26:50000000",
        }
        with (
            patch.object(goalkeeper_bribe, "_load", return_value=state),
            patch.object(goalkeeper_bribe, "_save") as save,
            patch.object(
                goalkeeper_bribe, "restore_goalkeeper_bribe",
                return_value={
                    "restored": [10], "remaining": [], "errors": [],
                    "team_address": "0x1234",
                },
            ) as restore,
            patch.object(
                goalkeeper_bribe, "apply_goalkeeper_bribe", return_value=applied,
            ) as apply,
        ):
            result = goalkeeper_bribe.sync_goalkeeper_bribes([item])

        restore.assert_called_once_with(99, "0x1234", [hook_player])
        apply.assert_called_once_with(99, "0x1234")
        save.assert_called_once_with(state)
        self.assertEqual(state["schema_version"], 4)
        self.assertEqual(state["effects"]["item-1"]["players"], [direct_player])
        self.assertEqual(result["mode"], "direct_attribute_block")
        self.assertNotIn("player_ids", result)

    def test_existing_effect_is_verified_and_repaired(self):
        player = {
            "player_id": 10,
            "player_name": "GK One",
            "player_address": "0x1000",
            "original_attributes": "00" * 54,
        }
        state = {
            "schema_version": 3,
            "effects": {
                "item-1": {
                    "item_id": "item-1",
                    "fixture_key": "fixture-1",
                    "opponent_team_id": 99,
                    "opponent_team_address": "0x1234",
                    "players": [player],
                },
            },
        }
        item = {"id": "item-1", "fixture_key": "fixture-1"}
        observation = {
            "maintained": [10],
            "repaired": [10],
            "players": [player],
            "errors": [],
            "team_address": "0x1234",
            "process_session": "1:fm26:50000000",
        }
        with (
            patch.object(goalkeeper_bribe, "_load", return_value=state),
            patch.object(goalkeeper_bribe, "_save") as save,
            patch.object(
                goalkeeper_bribe, "maintain_goalkeeper_bribe",
                return_value=observation,
            ) as maintain,
            patch.object(goalkeeper_bribe, "apply_goalkeeper_bribe") as apply,
        ):
            result = goalkeeper_bribe.sync_goalkeeper_bribes([item])

        maintain.assert_called_once_with(99, "0x1234", [player])
        apply.assert_not_called()
        save.assert_called_once_with(state)
        self.assertEqual(result["maintained"], 1)
        self.assertNotIn("player_ids", result)
        self.assertEqual(result["effects"][0]["state"], "verified")
        self.assertIsNotNone(result["effects"][0]["last_drift_at"])

    def test_runtime_failure_state_caps_without_repeated_persistence(self):
        effect = {}
        self.assertTrue(goalkeeper_bribe._mark_failed(effect, "read failed"))
        self.assertTrue(goalkeeper_bribe._mark_failed(effect, "read failed"))
        self.assertTrue(goalkeeper_bribe._mark_failed(effect, "read failed"))
        self.assertFalse(goalkeeper_bribe._mark_failed(effect, "read failed"))
        self.assertEqual(effect["runtime_state"], "error")
        self.assertEqual(effect["consecutive_failures"], 3)

    def test_pending_restore_remains_visible_in_runtime_status(self):
        player = {"player_id": 10, "original_sharpness": 6400}
        state = {
            "schema_version": 3,
            "effects": {
                "item-1": {
                    "item_id": "item-1",
                    "opponent_team_id": 99,
                    "opponent_team_address": "0x1234",
                    "players": [player],
                },
            },
        }
        with (
            patch.object(goalkeeper_bribe, "_load", return_value=state),
            patch.object(goalkeeper_bribe, "_save"),
            patch.object(
                goalkeeper_bribe, "restore_goalkeeper_bribe",
                return_value={
                    "restored": [], "remaining": [player],
                    "errors": ["busy"], "team_address": "0x1234",
                },
            ),
        ):
            result = goalkeeper_bribe.sync_goalkeeper_bribes([])

        self.assertEqual(result["tracked"], 1)
        self.assertEqual(result["effects"][0]["state"], "restore_pending")
        self.assertEqual(result["effects"][0]["last_error"], "busy")


if __name__ == "__main__":
    unittest.main()
