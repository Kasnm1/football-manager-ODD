from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from tools.preview_cup_odds import (
    anchored_save_context_evidence, fm24_save_evidence, read_manager_context,
    read_save_identity,
)
from tools.game_layout import (
    FM24_240_LAYOUT, FM24_241_LAYOUT, FM24_EPIC_LAYOUT, FM24_LAYOUT,
    FM24_XGP_LAYOUT,
)
from tools.save_memory import (
    SAVE_NAME_OWNERS,
    SaveNameCandidate,
    _SAVE_NAME_CACHE,
    _read_fm24_local_save_name,
    _write_persistent_candidate,
    read_cached_save_name,
    read_savegame_identity,
    read_save_name,
)


class FM24SaveIdentityTests(unittest.TestCase):
    def test_manager_revalidation_skips_identity_manager_discovery(self):
        import inspect

        source = inspect.getsource(read_manager_context)
        self.assertIn(
            "stable_save_name, _candidates = read_cached_save_name", source,
        )
        self.assertIn("if preferred_save_id:", source)
        self.assertIn('known_session.get("id")', source)

    def test_cached_save_name_probe_never_starts_heap_discovery(self):
        reader = SimpleNamespace(
            layout=SimpleNamespace(key="fm24"),
            process=SimpleNamespace(pid=24),
            module_base=0x100000,
        )
        module = SimpleNamespace(path="fm.exe", size=0x1000)
        with (
            patch("tools.save_memory._read_persistent_candidate", return_value=None),
            patch(
                "tools.save_memory.find_save_name_candidates",
                side_effect=AssertionError("cache-only probe must not scan heaps"),
            ),
        ):
            self.assertEqual(
                read_cached_save_name(reader, module, None),
                (None, []),
            )

    def test_background_identity_probe_reuses_confirmed_network_context(self):
        layout = SimpleNamespace(key="fm24")
        module = SimpleNamespace()
        reader = SimpleNamespace()
        context = MagicMock()
        context.__enter__.return_value = ("fm.exe", layout, module, reader)
        context.__exit__.return_value = False

        with (
            patch("tools.preview_cup_odds.open_supported_reader", return_value=context),
            patch("tools.preview_cup_odds.read_savegame_identity", return_value=None),
            patch(
                "tools.preview_cup_odds.read_cached_save_name",
                return_value=(None, []),
            ) as cached_name,
            patch("tools.preview_cup_odds.fm24_save_evidence") as deep_evidence,
            patch("tools.preview_cup_odds.discover_human_managers") as discover,
            patch("tools.preview_cup_odds.fm24_network_save_key") as fallback,
            patch("tools.preview_cup_odds.resolve_save_identity") as resolve,
            patch("tools.preview_cup_odds.remember_save_name"),
        ):
            result = read_save_identity(
                preferred_save_id="fm24-network-session-confirmed",
            )

        self.assertEqual(result, "fm24-network-session-confirmed")
        discover.assert_not_called()
        fallback.assert_not_called()
        cached_name.assert_called_once_with(reader, module, None)
        deep_evidence.assert_not_called()
        resolve.assert_not_called()

    def test_background_identity_probe_keeps_same_network_career_after_team_change(self):
        layout = SimpleNamespace(key="fm24")
        module = SimpleNamespace()
        reader = SimpleNamespace()
        context = MagicMock()
        context.__enter__.return_value = ("fm.exe", layout, module, reader)
        context.__exit__.return_value = False
        old_session = {"manager_id": 77, "managed_team_refs": [{"team_id": 1}]}
        changed_session = {"manager_id": 77, "managed_team_refs": [{"team_id": 2}]}

        with (
            patch("tools.preview_cup_odds.open_supported_reader", return_value=context),
            patch("tools.preview_cup_odds.read_savegame_identity", return_value=None),
            patch("tools.preview_cup_odds.read_cached_save_name", return_value=(None, [])),
            patch("tools.preview_cup_odds._validated_known_human_manager_sessions", return_value=[]),
            patch("tools.preview_cup_odds.read_save_name", return_value=(None, [])),
            patch("tools.preview_cup_odds.discover_human_managers", return_value=[changed_session]),
            patch("tools.preview_cup_odds.resolve_save_identity") as resolve,
        ):
            result = read_save_identity(
                preferred_save_id="career-current",
                preferred_manager_id=77,
                known_manager_sessions=[old_session],
            )

        self.assertEqual(result, "career-current")
        resolve.assert_not_called()

    def test_background_identity_probe_detects_changed_local_save_name(self):
        layout = SimpleNamespace(key="fm24")
        module = SimpleNamespace()
        reader = SimpleNamespace()
        context = MagicMock()
        context.__enter__.return_value = ("fm.exe", layout, module, reader)
        context.__exit__.return_value = False
        old_session = {"manager_id": 77}

        with (
            patch("tools.preview_cup_odds.open_supported_reader", return_value=context),
            patch("tools.preview_cup_odds.read_savegame_identity", return_value=None),
            patch("tools.preview_cup_odds.read_cached_save_name", return_value=(None, [])),
            patch("tools.preview_cup_odds._validated_known_human_manager_sessions", return_value=[]),
            patch("tools.preview_cup_odds.read_save_name", return_value=("New Save", [object()])),
            patch("tools.preview_cup_odds.discover_human_managers") as discover,
            patch("tools.preview_cup_odds.resolve_save_identity", return_value="career-new") as resolve,
            patch("tools.preview_cup_odds.remember_save_name"),
        ):
            result = read_save_identity(
                preferred_save_id="career-old",
                preferred_manager_id=77,
                known_manager_sessions=[old_session],
            )

        self.assertEqual(result, "career-new")
        self.assertTrue(resolve.call_args.kwargs["stable_id"].startswith("save-name-"))
        discover.assert_not_called()

    def test_background_identity_probe_keeps_account_when_fm24_save_is_renamed_after_club_change(self):
        layout = SimpleNamespace(key="fm24")
        module = SimpleNamespace()
        reader = SimpleNamespace()
        context = MagicMock()
        context.__enter__.return_value = ("fm.exe", layout, module, reader)
        context.__exit__.return_value = False
        old_session = {
            "manager_id": 77,
            "managed_team_refs": [{"team_id": 1, "team_type": "club"}],
        }
        with (
            patch("tools.preview_cup_odds.open_supported_reader", return_value=context),
            patch("tools.preview_cup_odds.read_savegame_identity", return_value=None),
            patch(
                "tools.preview_cup_odds.read_cached_save_name",
                return_value=("New Club Save", [object()]),
            ),
            patch("tools.preview_cup_odds.confirmed_save_name", return_value="Old Club Save"),
            patch(
                "tools.preview_cup_odds._fm24_manager_survives_save_name_change",
                return_value=True,
            ) as survives,
            patch("tools.preview_cup_odds.resolve_save_identity") as resolve,
        ):
            result = read_save_identity(
                preferred_save_id="career-current",
                preferred_manager_id=77,
                known_manager_sessions=[old_session],
            )

        self.assertEqual(result, "career-current")
        survives.assert_called_once_with(reader, 77, [old_session])
        resolve.assert_not_called()

    def test_background_identity_probe_detects_new_network_career(self):
        layout = SimpleNamespace(key="fm24")
        module = SimpleNamespace()
        reader = SimpleNamespace()
        context = MagicMock()
        context.__enter__.return_value = ("fm.exe", layout, module, reader)
        context.__exit__.return_value = False
        old_session = {"manager_id": 77}
        new_session = {
            "manager_id": 88,
            "managed_team_refs": [{"team_id": 2, "team_type": "club"}],
        }

        with (
            patch("tools.preview_cup_odds.open_supported_reader", return_value=context),
            patch("tools.preview_cup_odds.read_savegame_identity", return_value=None),
            patch("tools.preview_cup_odds.read_cached_save_name", return_value=(None, [])),
            patch("tools.preview_cup_odds._validated_known_human_manager_sessions", return_value=[]),
            patch("tools.preview_cup_odds.read_save_name", return_value=(None, [])),
            patch("tools.preview_cup_odds.discover_human_managers", return_value=[new_session]),
            patch("tools.preview_cup_odds.resolve_save_identity", return_value="career-new") as resolve,
            patch("tools.preview_cup_odds.remember_save_name"),
        ):
            result = read_save_identity(
                preferred_save_id="career-old",
                preferred_manager_id=77,
                known_manager_sessions=[old_session],
            )

        self.assertEqual(result, "career-new")
        self.assertTrue(resolve.call_args.kwargs["stable_id"].startswith("network-session-"))

    def test_background_identity_probe_without_confirmed_context_stays_empty(self):
        layout = SimpleNamespace(key="fm24")
        module = SimpleNamespace()
        reader = SimpleNamespace()
        context = MagicMock()
        context.__enter__.return_value = ("fm.exe", layout, module, reader)
        context.__exit__.return_value = False

        with (
            patch("tools.preview_cup_odds.open_supported_reader", return_value=context),
            patch("tools.preview_cup_odds.read_savegame_identity", return_value=None),
            patch(
                "tools.preview_cup_odds.fm24_save_evidence",
                return_value=(None, None, None),
            ) as evidence,
            patch("tools.preview_cup_odds.discover_human_managers") as discover,
            patch("tools.preview_cup_odds.resolve_save_identity") as resolve,
        ):
            result = read_save_identity()

        self.assertIsNone(result)
        evidence.assert_called_once_with(
            layout, reader, module, None, discover_managers=False,
        )
        discover.assert_not_called()
        resolve.assert_not_called()

    def test_weak_network_context_is_anchored_to_confirmed_connection(self):
        layout = SimpleNamespace(key="fm24")

        self.assertEqual(
            anchored_save_context_evidence(
                layout, "network-session-a", None, "career-current",
            ),
            (None, None),
        )
        self.assertEqual(
            anchored_save_context_evidence(
                layout, "network-session-a", None, None,
            ),
            ("network-session-a", None),
        )

    def test_local_name_context_is_never_anchored(self):
        self.assertEqual(
            anchored_save_context_evidence(
                SimpleNamespace(key="fm24"),
                "save-name-a", None, "career-current",
            ),
            ("save-name-a", None),
        )

    def test_all_fm24_layouts_reject_the_volatile_numeric_identity(self):
        for layout in (
            FM24_LAYOUT, FM24_EPIC_LAYOUT, FM24_240_LAYOUT,
            FM24_241_LAYOUT, FM24_XGP_LAYOUT,
        ):
            self.assertIsNone(layout.savegame_id_absolute_address)
            reader = SimpleNamespace(layout=layout, u32=MagicMock())
            self.assertIsNone(read_savegame_identity(reader))
            reader.u32.assert_not_called()

    def test_fm24_numeric_identity_is_ignored_without_local_or_network_evidence(self):
        layout = SimpleNamespace(key="fm24")
        reader = object()
        with (
            patch("tools.preview_cup_odds.read_save_name", return_value=(None, [])),
            patch("tools.preview_cup_odds.discover_human_managers", return_value=[]),
        ):
            stable, equivalent, save_name = fm24_save_evidence(
                layout, reader, object(), {"savegame_id": "4088782"},
                discover_managers=True,
            )

        self.assertIsNone(stable)
        self.assertIsNone(equivalent)
        self.assertIsNone(save_name)

    def test_verified_local_name_replaces_numeric_identity(self):
        layout = SimpleNamespace(key="fm24")
        with (
            patch(
                "tools.preview_cup_odds.read_save_name",
                return_value=("Verified Save", [object()]),
            ),
        ):
            stable, equivalent, save_name = fm24_save_evidence(
                layout, object(), object(), {"savegame_id": "4088782"},
            )

        self.assertTrue(str(stable).startswith("save-name-"))
        self.assertIsNone(equivalent)
        self.assertEqual(save_name, "Verified Save")

    def test_identity_resolution_uses_local_slot_instead_of_numeric_value(self):
        layout = SimpleNamespace(key="fm24")
        reader = SimpleNamespace()
        context = MagicMock()
        context.__enter__.return_value = ("fm.exe", layout, object(), reader)
        context.__exit__.return_value = False
        with (
            patch("tools.preview_cup_odds.open_supported_reader", return_value=context),
            patch(
                "tools.preview_cup_odds.read_savegame_identity",
                return_value={"savegame_id": "4088782"},
            ),
            patch(
                "tools.preview_cup_odds.read_save_name",
                return_value=("Verified Save", [object()]),
            ),
            patch(
                "tools.preview_cup_odds.resolve_save_identity",
                return_value="career-local",
            ) as resolve,
            patch("tools.preview_cup_odds.remember_save_name"),
        ):
            result = read_save_identity()

        self.assertEqual(result, "career-local")
        self.assertTrue(resolve.call_args.kwargs["stable_id"].startswith("save-name-"))
        self.assertIsNone(resolve.call_args.kwargs["equivalent_stable_id"])

    def test_verified_path_and_label_supply_local_save_name(self):
        base = 0x1000
        values = {
            base + 0xB0: "U:/FM2024/games/Miku 刘-曼城454.fm",
            base + 0x1F0: "Miku 刘-曼城454",
        }
        with patch(
            "tools.save_memory._read_save_name_field",
            side_effect=lambda _reader, address: values.get(address),
        ):
            self.assertEqual(
                _read_fm24_local_save_name(object(), base),
                "Miku 刘-曼城454",
            )

    def test_provider_ui_text_is_not_accepted_as_save_name(self):
        base = 0x2000
        values = {
            base + 0xD0: "移动到我的国家/地区队",
            base + 0x1F0: "staf",
        }
        with patch(
            "tools.save_memory._read_save_name_field",
            side_effect=lambda _reader, address: values.get(address),
        ):
            self.assertIsNone(_read_fm24_local_save_name(object(), base))

    def test_mismatched_path_and_label_fail_closed(self):
        base = 0x3000
        values = {
            base + 0xB0: "C:/games/real-save.fm",
            base + 0x1F0: "none",
        }
        with patch(
            "tools.save_memory._read_save_name_field",
            side_effect=lambda _reader, address: values.get(address),
        ):
            self.assertIsNone(_read_fm24_local_save_name(object(), base))

    def test_persistent_address_cache_is_revalidated_before_skipping_scan(self):
        reader = SimpleNamespace(
            layout=SimpleNamespace(key="fm26"),
            process=SimpleNamespace(pid=123),
            module_base=0x100000,
            ptr=MagicMock(return_value=0x200000),
        )
        module = SimpleNamespace(path="U:/FM/game_plugin.dll", size=4096)
        candidate = SaveNameCandidate(
            "Cached Save", SAVE_NAME_OWNERS[0][0], 0x300000, 0x200000,
        )
        identity = {"session_nonce": 77, "root_address": "0x400000"}
        with tempfile.TemporaryDirectory() as directory, patch(
            "tools.save_memory.SAVE_NAME_ADDRESS_CACHE_PATH",
            Path(directory) / "save-name-addresses.v1.json",
        ):
            _write_persistent_candidate(reader, module, 77, candidate)
            _SAVE_NAME_CACHE.clear()
            with (
                patch("tools.save_memory.find_vtables_for_rtti", return_value=[0x200000]),
                patch("tools.save_memory._read_owner_save_name", return_value="Live Save"),
                patch("tools.save_memory.find_save_name_candidates") as scan,
            ):
                value, candidates = read_save_name(reader, module, identity)

        self.assertEqual(value, "Live Save")
        self.assertEqual(candidates[0].object_address, 0x300000)
        scan.assert_not_called()


if __name__ == "__main__":
    unittest.main()
