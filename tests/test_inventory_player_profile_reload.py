from __future__ import annotations

import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from fm_odds_web import LocalOddsState


ROOT = Path(__file__).resolve().parents[1]


class InventoryPlayerProfileReloadTests(unittest.TestCase):
    def test_non_native_player_alias_does_not_trigger_live_memory_resolution(self):
        source = (ROOT / "fm_odds_web.py").read_text(encoding="utf-8")

        alias_handler = source.split(
            "def update_player_name", 1,
        )[1].split("def inventory_cancel", 1)[0]
        self.assertNotIn("self._live_player_profile(", alias_handler)
        self.assertNotIn("self._live_player_effect_target(", alias_handler)
        self.assertIn("for profile in profiles", alias_handler)

    def test_player_alias_can_target_managed_youth_squad(self):
        state = LocalOddsState.__new__(LocalOddsState)
        youth = {"id": 42, "name": "游戏原名", "game_name": "游戏原名"}
        state.memory_lock = threading.RLock()
        state.lock = threading.RLock()
        state.club_profile = None
        state.club_profiles = {
            10: {
                "team": {"id": 10, "team_type": "club"},
                "players": [],
                "squads": [{"team_id": 20, "players": [youth]}],
            },
        }
        state._bind_current_save = lambda: "save-1"
        state._player_profile = lambda _player_id: (None, None)

        with patch("fm_odds_web.set_player_alias", return_value="青年新名"):
            result = state.update_player_name({"player_id": 42, "name": "青年新名"})

        self.assertEqual(result["name"], "青年新名")
        self.assertEqual(youth["name"], "青年新名")
        self.assertTrue(youth["custom_name"])

    def test_frontend_sends_team_id_with_every_player_item_request(self):
        source = (ROOT / "web" / "app.js").read_text(encoding="utf-8")

        self.assertGreaterEqual(
            source.count("team_id:managedPlayerTeamId(player.id)"), 2,
        )
        self.assertIn(
            "team_id:Number(selectedTarget?.dataset.teamId || "
            "managedPlayerTeamId(targetId,true))",
            source,
        )
        self.assertIn("player_id:playerId,team_id:teamId", source)
        self.assertIn("team_id:managedPlayerTeamId(playerId),age", source)

    def test_cached_player_resolution_prefers_club_but_honors_explicit_team(self):
        state = LocalOddsState.__new__(LocalOddsState)
        national_player = {"id": 42, "name": "Player", "address": "0x4201"}
        club_player = {"id": 42, "name": "Player", "address": "0x4202"}
        national = {
            "team": {"id": 86, "team_type": "national"},
            "players": [national_player],
        }
        club = {
            "team": {"id": 9, "team_type": "club"},
            "players": [club_player],
        }
        state.club_profiles = {86: national, 9: club}
        state.club_profile = club

        default_profile, default_player = state._cached_player_profile(42)
        national_profile, selected_national_player = state._cached_player_profile(42, 86)

        self.assertIs(default_profile, club)
        self.assertIs(default_player, club_player)
        self.assertIs(national_profile, national)
        self.assertIs(selected_national_player, national_player)

    def test_missing_lazy_profile_is_reloaded_from_managed_team(self):
        state = LocalOddsState.__new__(LocalOddsState)
        state.lock = threading.RLock()
        state.output = {
            "manager": {"id": 7, "manager_address": "0x7000"},
            "managed_team": {
                "id": 9,
                "name": "Managed Club",
                "address": "0x9000",
                "team_type": "club",
            },
        }
        state.club_profile = None
        state.club_profiles = {}
        state.club_context = None
        state.club_contexts = {}
        state.club_last_updated = None
        profile = {
            "manager": {"id": 7},
            "team": {"id": 9, "name": "Managed Club"},
            "players": [{"id": 42, "name": "Player", "address": "0x4200"}],
            "staff": [],
        }

        with (
            patch.object(state, "_data_scope_id", return_value="save.manager-7"),
            patch("fm_odds_web.read_club_profile", return_value=profile) as read_profile,
            patch("fm_odds_web.apply_player_aliases") as aliases,
        ):
            resolved_profile, player = state._reload_player_profile(42)

        self.assertIs(resolved_profile, profile)
        self.assertEqual(player["id"], 42)
        self.assertIs(state.club_profiles[9], profile)
        self.assertIs(state.club_profile, profile)
        read_profile.assert_called_once_with(
            7, 9, force=True,
            team_address="0x9000",
            manager_address="0x7000",
        )
        aliases.assert_called_once_with(profile, "save.manager-7")

    def test_live_player_resolution_validates_cached_player_with_light_target_read(self):
        state = LocalOddsState.__new__(LocalOddsState)
        stale_player = {"id": 42, "name": "Player", "address": "0xOLD"}
        stale_profile = {
            "team": {"id": 9, "team_type": "club"},
            "players": [stale_player],
        }
        state.lock = threading.RLock()
        state.output = {
            "manager": {"id": 7, "manager_address": "0x7000"},
            "managed_teams": [{
                "id": 9, "name": "Managed Club", "address": "0x9000",
                "team_type": "club",
            }],
        }
        state.club_profile = stale_profile
        state.club_profiles = {9: stale_profile}
        state.club_context = None
        state.club_contexts = {}

        with (
            patch(
                "fm_odds_web.resolve_team_player_address", return_value="0xNEW",
            ) as resolve_player,
            patch("fm_odds_web.read_club_profile") as read_profile,
        ):
            resolved_profile, player = state._live_player_profile(42, 9)

        self.assertIs(resolved_profile, stale_profile)
        self.assertIs(player, stale_player)
        self.assertEqual(player["address"], "0xNEW")
        resolve_player.assert_called_once_with(9, "0x9000", 42, "0xOLD")
        read_profile.assert_not_called()

    def test_live_player_resolution_falls_back_when_light_roster_check_misses(self):
        state = LocalOddsState.__new__(LocalOddsState)
        stale_player = {"id": 42, "name": "Player", "address": "0xOLD"}
        stale_profile = {
            "team": {"id": 9, "team_type": "club"},
            "players": [stale_player],
        }
        fresh_player = {"id": 42, "name": "Player", "address": "0xNEW"}
        fresh_profile = {
            "team": {"id": 9, "team_type": "club"},
            "players": [fresh_player],
        }
        state.lock = threading.RLock()
        state.output = {
            "manager": {"id": 7, "manager_address": "0x7000"},
            "managed_teams": [{
                "id": 9, "name": "Managed Club", "address": "0x9000",
                "team_type": "club",
            }],
        }
        state.club_profile = stale_profile
        state.club_profiles = {9: stale_profile}
        state.club_context = None
        state.club_contexts = {}

        with (
            patch("fm_odds_web.resolve_team_player_address", return_value=None),
            patch.object(state, "_data_scope_id", return_value="save.manager-7"),
            patch("fm_odds_web.read_club_profile", return_value=fresh_profile) as read_profile,
            patch("fm_odds_web.apply_player_aliases"),
        ):
            resolved_profile, player = state._live_player_profile(42, 9)

        self.assertIs(resolved_profile, fresh_profile)
        self.assertIs(player, fresh_player)
        read_profile.assert_called_once_with(
            7, 9, force=True,
            team_address="0x9000",
            manager_address="0x7000",
        )

    def test_live_player_resolution_rejects_mismatched_team_identity(self):
        state = LocalOddsState.__new__(LocalOddsState)
        state.lock = threading.RLock()
        state.output = {
            "manager": {"id": 7},
            "managed_teams": [{"id": 9, "team_type": "club"}],
        }
        state.club_profile = None
        state.club_profiles = {}
        state.club_context = None
        state.club_contexts = {}
        wrong_team_profile = {
            "team": {"id": 10},
            "players": [{"id": 42, "address": "0x4200"}],
        }

        with (
            patch.object(state, "_data_scope_id", return_value="save.manager-7"),
            patch("fm_odds_web.read_club_profile", return_value=wrong_team_profile),
        ):
            profile, player = state._live_player_profile(42, 9)

        self.assertIsNone(profile)
        self.assertIsNone(player)
        self.assertNotIn(9, state.club_profiles)

    def test_single_player_match_item_resolves_player_in_fixture_team(self):
        state = LocalOddsState.__new__(LocalOddsState)
        state.lock = threading.RLock()
        state.memory_lock = threading.RLock()
        state._bind_current_save = MagicMock()
        state.club_profile = None
        state.club_profiles = {}
        state.output = {
            "managed_teams": [
                {"id": 9, "name": "Club", "team_type": "club"},
                {"id": 86, "name": "Nation", "team_type": "national"},
            ],
            "matches": [{
                "fixture_date": "2028-01-10", "competition_id": 5,
                "kickoff_minutes": 900,
                "home": {"id": 86, "name": "Nation"},
                "away": {"id": 87, "name": "Opponent"},
            }],
        }
        state._live_player_profile = MagicMock(side_effect=RuntimeError("stop after lookup"))
        fixture_key = "2028-01-10|5|86|87"

        with (
            patch("fm_odds_web.public_economy", return_value={
                "inventory": [{"id": "drink-1", "sku": "red_bull"}],
            }),
            patch("fm_odds_web.select_process_layout"),
            patch("fm_odds_web.read_game_clock", return_value={
                "date": "2028-01-09", "minutes": 0,
            }),
        ):
            with self.assertRaisesRegex(RuntimeError, "stop after lookup"):
                state.inventory_use({
                    "item_id": "drink-1", "team_id": 86,
                    "player_id": 42, "fixture_key": fixture_key,
                })

        state._live_player_profile.assert_called_once_with(42, 86)

    def test_reallocation_uses_live_reload_after_cache_miss(self):
        state = LocalOddsState.__new__(LocalOddsState)
        state.lock = threading.RLock()
        state.memory_lock = threading.RLock()
        item = {"id": "pill-1", "sku": "martial_manual_fragment", "status": "available"}
        player = {"id": 42, "name": "Player", "address": "0x4200"}
        result = {
            "_original_raw": bytes(54),
            "before": {"pace": 10},
            "after": {"pace": 11},
            "attributes": {},
            "ca": 100,
            "pa": 120,
        }

        with (
            patch.object(state, "_bind_current_save"),
            patch.object(state, "_live_player_profile", return_value=({"players": [player]}, player)) as live_profile,
            patch("fm_odds_web.public_economy", return_value={"inventory": [item]}),
            patch("fm_odds_web.reallocate_player_attributes", return_value=result),
            patch("fm_odds_web.consume_instant_items", return_value={"inventory": []}),
        ):
            response = state.inventory_reallocate_attributes({
                "item_id": "pill-1",
                "player_id": 42,
                "team_id": 9,
                "changes": {"pace": 1},
                "expected": {"pace": 10},
            })

        live_profile.assert_called_once_with(42, 9)
        self.assertEqual(response["player"]["ca"], 100)

    def test_other_player_items_share_the_same_live_reload(self):
        state = LocalOddsState.__new__(LocalOddsState)
        state.lock = threading.RLock()
        state.memory_lock = threading.RLock()
        player = {"id": 42, "name": "Player", "address": "0x4200"}

        with (
            patch.object(state, "_bind_current_save"),
            patch.object(state, "_live_player_profile", return_value=({"players": [player]}, player)) as live_profile,
            patch("fm_odds_web.public_economy", return_value={
                "inventory": [{"id": "heal-1", "sku": "rejuvenation_pill", "status": "available"}],
            }),
            patch("fm_odds_web.remove_player_injury", return_value={
                "_original_injury": b"original",
                "injury_container": "0x5000",
            }),
            patch("fm_odds_web.consume_instant_item", return_value={"inventory": []}),
        ):
            response = state.inventory_heal_player({
                "item_id": "heal-1", "player_id": 42, "team_id": 9,
            })

        live_profile.assert_called_once_with(42, 9)
        self.assertEqual(response["player"]["id"], 42)


if __name__ == "__main__":
    unittest.main()
