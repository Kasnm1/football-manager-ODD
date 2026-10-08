from __future__ import annotations

import threading
import unittest
from unittest.mock import Mock, patch

import fm_odds_web
from tools import doping_effect


class DopingEffectSyncTests(unittest.TestCase):
    def test_existing_overlapping_snapshot_is_repaired_from_first_record(self):
        first_original = {
            "player_id": 42, "player_name": "Player",
            "original_attributes": (bytes([10] * 54)).hex(),
            "original_sharpness": 7300, "original_fitness": 8100,
        }
        polluted_original = {
            **first_original,
            "original_attributes": (bytes([18] * 54)).hex(),
            "original_sharpness": 10000, "original_fitness": 10000,
        }
        state = {
            "schema_version": 1,
            "effects": {
                "item-1": {
                    "managed_team_id": 9, "managed_team_address": "0x2000",
                    "players": [first_original],
                },
                "item-2": {
                    "managed_team_id": 9, "managed_team_address": "0x2000",
                    "players": [polluted_original],
                },
            },
        }
        active = [
            {"id": "item-1", "sku": "doping"},
            {"id": "item-2", "sku": "doping"},
        ]

        def maintained(_team_id, _team_address, players):
            return {
                "maintained": [42], "repaired": [], "players": players,
                "errors": [], "team_address": "0x2000",
                "process_session": "1:fm26:50000000",
            }

        with (
            patch.object(doping_effect, "_load", return_value=state),
            patch.object(doping_effect, "_save") as save,
            patch.object(
                doping_effect, "maintain_doping_effect", side_effect=maintained,
            ),
        ):
            doping_effect.sync_doping_effects(active)

        repaired = state["effects"]["item-2"]["players"][0]
        self.assertEqual(repaired["original_attributes"], first_original["original_attributes"])
        self.assertEqual(repaired["original_sharpness"], 7300)
        self.assertEqual(repaired["original_fitness"], 8100)
        save.assert_called_once_with(state)

    def test_overlapping_effect_inherits_first_original_snapshot(self):
        first_original = {
            "player_id": 42,
            "player_name": "Player",
            "player_address": "0x1000",
            "original_attributes": (bytes([10] * 54)).hex(),
            "original_sharpness": 7300,
            "original_fitness": 8100,
        }
        already_doped = {
            **first_original,
            "original_attributes": (bytes([18] * 54)).hex(),
            "original_sharpness": 10000,
            "original_fitness": 10000,
        }
        state = {
            "schema_version": 1,
            "effects": {
                "item-1": {
                    "managed_team_id": 9,
                    "managed_team_address": "0x2000",
                    "players": [first_original],
                },
            },
        }
        active = [
            {"id": "item-1", "sku": "doping", "fixture_name": "Match 1"},
            {
                "id": "item-2", "sku": "doping", "fixture_name": "Match 2",
                "managed_team_id": 9, "managed_team_address": "0x2000",
                "player_id": 42, "player_name": "Player",
            },
        ]
        maintained = {
            "maintained": [42], "repaired": [], "players": [first_original],
            "errors": [], "team_address": "0x2000",
            "process_session": "1:fm26:50000000",
        }
        applied = {
            "team_id": 9, "team_name": "Team", "team_address": "0x2000",
            "players": [already_doped], "process_session": "1:fm26:50000000",
        }
        with (
            patch.object(doping_effect, "_load", return_value=state),
            patch.object(doping_effect, "_save") as save,
            patch.object(
                doping_effect, "maintain_doping_effect", return_value=maintained,
            ),
            patch.object(doping_effect, "apply_doping_effect", return_value=applied),
        ):
            doping_effect.sync_doping_effects(active)

        saved_player = state["effects"]["item-2"]["players"][0]
        self.assertEqual(saved_player["original_attributes"], first_original["original_attributes"])
        self.assertEqual(saved_player["original_sharpness"], 7300)
        self.assertEqual(saved_player["original_fitness"], 8100)
        save.assert_called_once_with(state)

    def test_save_failure_rolls_back_only_the_latest_application(self):
        first_original = {
            "player_id": 42, "player_name": "Player",
            "original_attributes": (bytes([10] * 54)).hex(),
            "original_sharpness": 7300, "original_fitness": 8100,
        }
        immediate_before_apply = {
            **first_original,
            "original_attributes": (bytes([18] * 54)).hex(),
            "original_sharpness": 10000, "original_fitness": 10000,
        }
        state = {
            "schema_version": 1,
            "effects": {
                "item-1": {
                    "managed_team_id": 9, "managed_team_address": "0x2000",
                    "players": [first_original],
                },
            },
        }
        maintained = {
            "maintained": [42], "repaired": [], "players": [first_original],
            "errors": [], "team_address": "0x2000",
            "process_session": "1:fm26:50000000",
        }
        applied = {
            "team_id": 9, "team_name": "Team", "team_address": "0x2000",
            "players": [immediate_before_apply],
            "process_session": "1:fm26:50000000",
        }
        active = [
            {"id": "item-1", "sku": "doping"},
            {"id": "item-2", "sku": "doping", "managed_team_id": 9, "player_id": 42},
        ]
        with (
            patch.object(doping_effect, "_load", return_value=state),
            patch.object(doping_effect, "_save", side_effect=OSError("disk full")),
            patch.object(
                doping_effect, "maintain_doping_effect", return_value=maintained,
            ),
            patch.object(doping_effect, "apply_doping_effect", return_value=applied),
            patch.object(doping_effect, "restore_doping_effect") as restore,
        ):
            with self.assertRaisesRegex(OSError, "disk full"):
                doping_effect.sync_doping_effects(active)

        restore.assert_called_once_with(9, "0x2000", [immediate_before_apply])

    def test_existing_effect_is_maintained_instead_of_skipped(self):
        player = {
            "player_id": 42,
            "player_name": "Player",
            "player_address": "0x1000",
            "original_attributes": (bytes([20] * 54)).hex(),
        }
        state = {
            "schema_version": 1,
            "effects": {
                "item-1": {
                    "managed_team_id": 9,
                    "managed_team_address": "0x2000",
                    "players": [player],
                },
            },
        }
        active = [{"id": "item-1", "sku": "doping", "fixture_name": "Match"}]
        maintained = {
            "maintained": [42],
            "repaired": [],
            "players": [player],
            "errors": [],
            "team_address": "0x2000",
            "process_session": "1:fm26:50000000",
        }
        with (
            patch.object(doping_effect, "_load", return_value=state),
            patch.object(doping_effect, "_save") as save,
            patch.object(doping_effect, "maintain_doping_effect", return_value=maintained) as maintain,
            patch.object(doping_effect, "apply_doping_effect") as apply,
        ):
            result = doping_effect.sync_doping_effects(active)

        maintain.assert_called_once_with(9, "0x2000", [player])
        apply.assert_not_called()
        save.assert_called_once_with(state)
        self.assertEqual(result["maintained"], 1)
        self.assertFalse(result["errors"])
        self.assertEqual(result["effects"][0]["state"], "verified")
        self.assertEqual(
            result["effects"][0]["process_session"], "1:fm26:50000000",
        )

    def test_pending_restore_remains_visible_in_runtime_status(self):
        player = {"player_id": 42, "original_attributes": "00" * 54}
        state = {
            "schema_version": 1,
            "effects": {
                "item-1": {
                    "item_id": "item-1",
                    "managed_team_id": 9,
                    "managed_team_address": "0x2000",
                    "players": [player],
                },
            },
        }
        with (
            patch.object(doping_effect, "_load", return_value=state),
            patch.object(doping_effect, "_save"),
            patch.object(
                doping_effect, "restore_doping_effect",
                return_value={
                    "restored": [], "remaining": [player],
                    "errors": ["busy"], "team_address": "0x2000",
                },
            ),
        ):
            result = doping_effect.sync_doping_effects([])

        self.assertEqual(result["tracked"], 1)
        self.assertEqual(result["effects"][0]["state"], "restore_pending")
        self.assertEqual(result["effects"][0]["last_error"], "busy")


class DopingEffectClockSyncTests(unittest.TestCase):
    def test_same_day_poll_maintains_active_effect_with_throttle(self):
        state = object.__new__(fm_odds_web.LocalOddsState)
        state.output = {"account_scope_id": "scope-1"}
        state.fixture_item_sync_key = ("scope-1", "2026-07-27")
        state.fixture_item_sync_error = None
        state.fixture_effect_maintenance_at = 0.0
        state.memory_lock = threading.RLock()
        active = [{"id": "item-1", "sku": "doping"}]
        with (
            patch.object(fm_odds_web, "monotonic", side_effect=[100.0, 101.0]),
            patch.object(fm_odds_web, "active_doping_items", return_value=active) as items,
            patch.object(fm_odds_web, "active_goalkeeper_bribes", return_value=[]),
            patch.object(fm_odds_web, "referee_hook_candidate", return_value=None),
            patch.object(
                fm_odds_web.LocalOddsState, "_sync_referee_fixture_hook",
                return_value={"state": "inactive", "hook": {}},
            ),
            patch.object(
                fm_odds_web, "sync_doping_effects",
                return_value={
                    "active": 1, "tracked": 1, "maintained": 1,
                    "effects": [{"item_id": "item-1", "state": "verified"}],
                    "errors": [],
                },
            ) as sync,
        ):
            state._sync_fixture_items_for_clock({"date": "2026-07-27"})
            state._sync_fixture_items_for_clock({"date": "2026-07-27"})

        items.assert_called_once_with()
        sync.assert_called_once_with(active)
        self.assertIsNone(state.fixture_item_sync_error)
        self.assertEqual(
            state.fixture_item_effect_status["doping"]["effects"][0]["state"],
            "verified",
        )

    def test_same_day_poll_maintains_direct_goalkeeper_state_without_hook_targets(self):
        state = object.__new__(fm_odds_web.LocalOddsState)
        state.output = {"account_scope_id": "scope-1"}
        state.fixture_item_sync_key = ("scope-1", "2026-07-27")
        state.fixture_item_sync_error = None
        state.fixture_effect_maintenance_at = 0.0
        state.memory_lock = threading.RLock()
        state.redbull_hook = Mock()
        state.redbull_hook.sync.return_value = {"error": None}
        active = [{"id": "item-2", "sku": "bribed_goalkeeper"}]
        goalkeeper_status = {
            "active": 1, "tracked": 1, "maintained": 1,
            "effects": [{"item_id": "item-2", "state": "verified"}],
            "errors": [],
        }
        with (
            patch.object(fm_odds_web, "monotonic", return_value=100.0),
            patch.object(fm_odds_web, "active_doping_items", return_value=[]),
            patch.object(fm_odds_web, "active_goalkeeper_bribes", return_value=active),
            patch.object(fm_odds_web, "active_player_ids", return_value=[20]),
            patch.object(fm_odds_web, "referee_hook_candidate", return_value=None),
            patch.object(
                fm_odds_web.LocalOddsState, "_sync_referee_fixture_hook",
                return_value={"state": "inactive", "hook": {}},
            ),
            patch.object(
                fm_odds_web, "sync_goalkeeper_bribes",
                return_value=goalkeeper_status,
            ) as sync,
        ):
            state._sync_fixture_items_for_clock({"date": "2026-07-27"})

        sync.assert_called_once_with(active)
        state.redbull_hook.sync.assert_called_once_with([20], [])
        self.assertEqual(
            state.fixture_item_effect_status["goalkeeper_bribe"]["effects"][0]["state"],
            "verified",
        )


if __name__ == "__main__":
    unittest.main()
