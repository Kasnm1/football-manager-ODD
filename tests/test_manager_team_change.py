from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fm_odds_web import (
    managed_team_rows, matching_manager_team_profile,
    manager_assignment_absence_requires_confirmation,
    manager_assignment_context_changed, manager_context_confirmation_key,
    manager_club_change_confirmation_key,
    manager_club_change_requires_confirmation,
    manager_options_changed, manager_team_context_changed,
    same_account_manager_club_changed, should_retain_cached_manager_context,
)
from tools.preview_cup_odds import (
    _cached_direct_human_managers,
    _human_manager_session_from_address,
    _merge_human_manager_sessions,
    _person_contract_candidates,
    _revalidate_known_human_manager_session,
    discover_human_managers,
)


def _context(team_ids: list[int], manager_id: int = 123) -> dict:
    teams = [
        {"id": team_id, "team_type": "club", "name": f"Team {team_id}"}
        for team_id in team_ids
    ]
    return {
        "selected_manager_id": manager_id,
        "manager": {"id": manager_id},
        "managed_team": teams[0] if teams else None,
        "managed_teams": teams,
    }


class ManagerTeamChangeTests(unittest.TestCase):
    @staticmethod
    def _fixture_manager_reader(manager_id: int = 10) -> Mock:
        reader = Mock()
        reader.layout.manager_person_offset = 0
        reader.ptr.return_value = 0x3000
        reader.team.return_value = {
            "id": 100,
            "team_type": "club",
            "address": "0x2000",
            "manager_address": "0x3000",
        }
        reader.u32.return_value = manager_id
        return reader

    def test_single_fixture_manager_still_scans_for_second_manager(self):
        reader = self._fixture_manager_reader()
        fixture = SimpleNamespace(home_team=0x2000, away_team=0x2008)
        direct_session = {
            "manager_id": 20,
            "manager_name": "Two",
            "managed_team_refs": [{"team_id": 200, "team_type": "club"}],
        }

        with (
            patch("tools.preview_cup_odds.parse_fixture", return_value=fixture),
            patch("tools.preview_cup_odds._human_manager_matches", return_value=True),
            patch("tools.preview_cup_odds._manager_name", return_value="One"),
            patch(
                "tools.preview_cup_odds._cached_direct_human_managers",
                return_value=[direct_session],
            ) as direct_scan,
        ):
            sessions = discover_human_managers(reader, [0x1000])

        direct_scan.assert_called_once_with(reader)
        self.assertEqual([item["manager_id"] for item in sessions], [10, 20])

    def test_missing_fixture_manager_uses_direct_scan(self):
        direct_session = {"manager_id": 20, "managed_team_refs": []}

        with patch(
            "tools.preview_cup_odds._cached_direct_human_managers",
            return_value=[direct_session],
        ) as direct_scan:
            sessions = discover_human_managers(Mock(), [])

        direct_scan.assert_called_once()
        self.assertEqual([item["manager_id"] for item in sessions], [20])

    def test_connection_manager_discovery_skips_heap_fallback(self):
        reader = Mock()
        reader.layout.key = "fm24"
        reader.layout.human_manager_vtable_rvas = (0x1234,)
        directory = Mock()
        directory.human_manager_addresses.return_value = ()

        with (
            patch(
                "tools.preview_cup_odds.database_index_for_reader",
                return_value=directory,
            ),
            patch(
                "tools.preview_cup_odds._runtime_state",
                return_value={
                    "human_manager_sessions": [],
                    "human_manager_scanned_at": 0.0,
                },
            ),
            patch("tools.preview_cup_odds.iter_readable_regions") as heap_scan,
        ):
            sessions = discover_human_managers(
                reader, [], allow_expensive_direct_scan=False,
            )

        self.assertEqual(sessions, [])
        heap_scan.assert_not_called()

    def test_missing_preferred_manager_uses_direct_scan(self):
        reader = self._fixture_manager_reader()
        fixture = SimpleNamespace(home_team=0x2000, away_team=0x2008)
        direct_session = {"manager_id": 20, "managed_team_refs": []}

        with (
            patch("tools.preview_cup_odds.parse_fixture", return_value=fixture),
            patch("tools.preview_cup_odds._human_manager_matches", return_value=True),
            patch("tools.preview_cup_odds._manager_name", return_value="One"),
            patch(
                "tools.preview_cup_odds._cached_direct_human_managers",
                return_value=[direct_session],
            ) as direct_scan,
        ):
            sessions = discover_human_managers(
                reader, [0x1000], preferred_manager_id=20,
            )

        direct_scan.assert_called_once()
        self.assertEqual([item["manager_id"] for item in sessions], [10, 20])

    def test_multi_manager_discovery_does_not_stop_after_two_fixture_managers(self):
        reader = self._fixture_manager_reader()
        fixtures = [
            SimpleNamespace(home_team=0x2000, away_team=0x2008),
            SimpleNamespace(home_team=0x2010, away_team=0x2018),
        ]
        manager_by_team = {
            0x2000: 0x3000, 0x2008: 0x3000,
            0x2010: 0x4000, 0x2018: 0x4000,
        }
        reader.ptr.side_effect = lambda address: manager_by_team.get(address - 0x80, 0)
        reader.team.side_effect = lambda address: {
            "id": address,
            "team_type": "club",
            "address": hex(address),
            "manager_address": hex(manager_by_team[address]),
        }
        reader.u32.side_effect = lambda address: 10 if address == 0x300C else 20
        direct_session = {
            "manager_id": 30,
            "manager_name": "Three",
            "managed_team_refs": [{"team_id": 300, "team_type": "club"}],
        }

        with (
            patch("tools.preview_cup_odds.parse_fixture", side_effect=fixtures),
            patch("tools.preview_cup_odds._human_manager_matches", return_value=True),
            patch("tools.preview_cup_odds._manager_name", return_value="Manager"),
            patch(
                "tools.preview_cup_odds._cached_direct_human_managers",
                return_value=[direct_session],
            ) as direct_scan,
        ):
            sessions = discover_human_managers(reader, [0x1000, 0x1008])

        direct_scan.assert_called_once_with(reader)
        self.assertEqual([item["manager_id"] for item in sessions], [10, 20, 30])

    def test_manager_switch_hint_is_revalidated_before_team_reuse(self):
        reader = Mock()
        known = {
            "manager_id": 20,
            "managed_team_refs": [{"team_id": 100, "address": "0x1000"}],
        }
        live = {
            "manager_id": 20,
            "managed_team_refs": [{"team_id": 200, "address": "0x2000"}],
        }

        with patch(
            "tools.preview_cup_odds.discover_human_managers",
            return_value=[live],
        ) as discover:
            selected, sessions = _revalidate_known_human_manager_session(
                reader, 20, known,
            )

        discover.assert_called_once_with(
            reader, [], preferred_manager_id=20,
            known_manager_sessions=[known],
        )
        self.assertIs(selected, live)
        self.assertEqual(sessions, [live])

    def test_partial_cached_manager_validation_does_not_hide_other_managers(self):
        reader = Mock()
        selected = {
            "manager_id": 10,
            "manager_address": "0x1000",
            "managed_team_refs": [{"team_id": 100, "address": "0x2000"}],
        }
        other = {
            "manager_id": 20,
            "manager_address": "0x3000",
            "managed_team_refs": [{"team_id": 200, "address": "0x4000"}],
        }

        with (
            patch(
                "tools.preview_cup_odds._validated_known_human_manager_sessions",
                return_value=[selected],
            ),
            patch(
                "tools.preview_cup_odds._cached_direct_human_managers",
                return_value=[other],
            ) as direct_scan,
        ):
            sessions = discover_human_managers(
                reader, [], preferred_manager_id=10,
                known_manager_sessions=[selected, other],
            )

        direct_scan.assert_called_once_with(reader)
        self.assertEqual([item["manager_id"] for item in sessions], [10, 20])

    def test_fm24_human_manager_uses_validated_alternate_contract_slot(self):
        manager = 0x1000
        person = manager + 0x450
        contract = 0x3000
        team = 0x4000

        def pointer(address: int) -> int | None:
            return {
                person + 0xC8: None,
                person + 0xA8: contract,
                person + 0xB0: None,
                contract + 0x08: person,
                contract + 0x10: team,
                team + 0x80: manager,
            }.get(address)

        reader = SimpleNamespace(
            layout=SimpleNamespace(key="fm24", manager_person_offset=0x450),
            ptr=Mock(side_effect=pointer),
            u32=Mock(return_value=77),
            team=Mock(return_value={
                "id": 42,
                "team_type": "club",
                "address": hex(team),
                "club_address": "0x5000",
            }),
        )
        with (
            patch("tools.preview_cup_odds._human_manager_matches", return_value=True),
            patch("tools.preview_cup_odds._manager_name", return_value="Manager"),
        ):
            session = _human_manager_session_from_address(reader, manager)

        self.assertIsNotNone(session)
        self.assertEqual(session["manager_id"], 77)
        self.assertEqual(session["managed_team_refs"][0]["team_id"], 42)
        reader.ptr.assert_any_call(person + 0xA8)

    def test_fm24_national_team_contract_is_discovered_from_other_contracts(self):
        manager = 0x1000
        person = manager + 0x450
        other_contracts = 0x2000
        national_contract = 0x3000
        national_team = 0x4000

        def pointer(address: int) -> int | None:
            return {
                person + 0xC8: None,
                person + 0xA8: None,
                person + 0xB0: None,
                person + 0xD0: other_contracts,
                other_contracts + 0x10: national_contract,
                national_contract + 0x08: person,
                national_contract + 0x10: national_team,
                national_team + 0x80: manager,
            }.get(address)

        reader = SimpleNamespace(
            layout=SimpleNamespace(key="fm24", manager_person_offset=0x450),
            ptr=Mock(side_effect=pointer),
            u32=Mock(return_value=77),
            team=Mock(return_value={
                "id": 765,
                "team_type": "national",
                "address": hex(national_team),
                "club_address": None,
            }),
        )
        with (
            patch("tools.preview_cup_odds._human_manager_matches", return_value=True),
            patch("tools.preview_cup_odds._manager_name", return_value="Manager"),
        ):
            session = _human_manager_session_from_address(reader, manager)

        self.assertIsNotNone(session)
        self.assertEqual(session["managed_team_refs"], [{
            "game_instance_id": "human-77",
            "team_id": 765,
            "team_type": "national",
            "manager_id": 77,
            "address": hex(national_team),
            "club_address": None,
            "manager_address": hex(manager),
            "source": "native_database_index",
        }])

    def test_human_manager_without_team_is_retained_for_unemployed_confirmation(self):
        manager = 0x1000
        reader = SimpleNamespace(
            layout=SimpleNamespace(key="fm26", manager_person_offset=0x18),
            u32=Mock(return_value=77),
        )
        with (
            patch("tools.preview_cup_odds._human_manager_matches", return_value=True),
            patch("tools.preview_cup_odds._person_contract_candidates", return_value=[]),
            patch("tools.preview_cup_odds._manager_name", return_value="Manager"),
        ):
            session = _human_manager_session_from_address(reader, manager)

        self.assertEqual(session["manager_id"], 77)
        self.assertEqual(session["manager_address"], hex(manager))
        self.assertEqual(session["manager_name"], "Manager")
        self.assertEqual(session["managed_team_refs"], [])

    def test_fm24_alternate_contract_requires_person_and_team_links(self):
        person = 0x2000
        contract = 0x3000

        def pointer(address: int) -> int | None:
            if address == person + 0xA8:
                return contract
            if address == contract + 0x08:
                return 0xDEADBEEF
            if address == contract + 0x10:
                return 0x4000
            return None

        reader = SimpleNamespace(
            layout=SimpleNamespace(key="fm24"),
            ptr=Mock(side_effect=pointer),
        )

        self.assertEqual(_person_contract_candidates(reader, person), [])

    def test_verified_cached_manager_skips_fixture_and_direct_scans(self):
        reader = self._fixture_manager_reader()
        known_session = {
            "id": 10,
            "name": "One",
            "manager_address": "0x3000",
            "managed_team_refs": [{
                "id": 100,
                "team_type": "club",
                "address": "0x2000",
                "manager_address": "0x3000",
            }],
        }

        with (
            patch("tools.preview_cup_odds._human_manager_matches", return_value=True),
            patch("tools.preview_cup_odds.parse_fixture") as parse_fixture,
            patch("tools.preview_cup_odds._cached_direct_human_managers") as direct_scan,
        ):
            sessions = discover_human_managers(
                reader, [0x1000], preferred_manager_id=10,
                known_manager_sessions=[known_session],
            )

        parse_fixture.assert_not_called()
        direct_scan.assert_not_called()
        self.assertEqual([item["manager_id"] for item in sessions], [10])
        self.assertEqual(sessions[0]["source"], "verified_snapshot_manager")

    def test_fixture_snapshots_deduplicate_team_manager_validation(self):
        reader = self._fixture_manager_reader()
        fixtures = [
            SimpleNamespace(home_team=0x2000, away_team=0x2008),
            SimpleNamespace(home_team=0x2008, away_team=0x2000),
        ]
        reader.team.side_effect = lambda address: {
            "id": 100 if address == 0x2000 else 200,
            "team_type": "club",
            "address": hex(address),
            "manager_address": "0x3000",
        }
        progress = []

        with (
            patch(
                "tools.preview_cup_odds.parse_fixture_from_raw",
                side_effect=fixtures,
            ) as parse_raw,
            patch("tools.preview_cup_odds.parse_fixture") as parse_fixture,
            patch("tools.preview_cup_odds._human_manager_matches", return_value=True),
            patch("tools.preview_cup_odds._manager_name", return_value="One"),
            patch("tools.preview_cup_odds._cached_direct_human_managers", return_value=[]),
        ):
            sessions = discover_human_managers(
                reader,
                [0x1000, 0x1008],
                fixture_snapshots={0x1000: b"first", 0x1008: b"second"},
                progress_callback=lambda phase, completed, total: progress.append(
                    (phase, completed, total)
                ),
            )

        self.assertEqual(parse_raw.call_count, 2)
        parse_fixture.assert_not_called()
        self.assertEqual(reader.ptr.call_count, 2)
        self.assertEqual(reader.team.call_count, 2)
        self.assertEqual(sessions[0]["managed_team_refs"][0]["team_id"], 100)
        self.assertEqual(len(sessions[0]["managed_team_refs"]), 2)
        self.assertIn(("fixtures", 2, 2), progress)
        self.assertIn(("teams", 2, 2), progress)

    def test_stale_cached_manager_falls_back_to_live_discovery(self):
        reader = self._fixture_manager_reader()
        reader.ptr.return_value = 0
        known_session = {
            "id": 10,
            "manager_address": "0x3000",
            "managed_team_refs": [{"id": 100, "address": "0x2000"}],
        }
        direct_session = {"manager_id": 20, "managed_team_refs": []}

        with (
            patch("tools.preview_cup_odds._human_manager_matches", return_value=True),
            patch(
                "tools.preview_cup_odds._cached_direct_human_managers",
                return_value=[direct_session],
            ) as direct_scan,
        ):
            sessions = discover_human_managers(
                reader, [], preferred_manager_id=10,
                known_manager_sessions=[known_session],
            )

        direct_scan.assert_called_once_with(reader)
        self.assertEqual([item["manager_id"] for item in sessions], [20])

    def test_new_human_manager_option_is_detected_without_team_change(self):
        current = {"manager_options": [{"id": 10}]}
        refreshed = {"manager_options": [{"id": 10}, {"id": 20}]}

        self.assertTrue(manager_options_changed(current, refreshed))

    def test_direct_manager_discovery_adds_manager_missing_from_fixture_pool(self):
        fixture_sessions = [{
            "manager_id": 10,
            "manager_name": "One",
            "managed_team_refs": [{"team_id": 100, "team_type": "club"}],
        }]
        direct_sessions = [
            {
                "manager_id": 10,
                "managed_team_refs": [{"team_id": 100, "team_type": "club"}],
            },
            {
                "manager_id": 20,
                "manager_name": "Two",
                "managed_team_refs": [{"team_id": 200, "team_type": "club"}],
            },
        ]

        merged = _merge_human_manager_sessions(fixture_sessions, direct_sessions)

        self.assertEqual([row["manager_id"] for row in merged], [10, 20])
        self.assertEqual(merged[1]["managed_team_refs"][0]["team_id"], 200)

    def test_empty_direct_manager_cache_is_retried_before_expiry(self):
        state = {
            "human_manager_sessions": [],
            "human_manager_scanned_at": 95.0,
        }
        recovered = [{"manager_id": 20, "managed_team_refs": [{"team_id": 200}]}]

        with (
            patch("tools.preview_cup_odds._runtime_state", return_value=state),
            patch("tools.preview_cup_odds.monotonic", return_value=100.0),
            patch(
                "tools.preview_cup_odds._discover_human_managers_direct",
                return_value=recovered,
            ) as discover,
        ):
            result = _cached_direct_human_managers(Mock())

        discover.assert_called_once()
        self.assertEqual(result, recovered)
        self.assertEqual(state["human_manager_sessions"], recovered)

    def test_manager_only_direct_cache_is_retried_before_expiry(self):
        partial = [{"manager_id": 20, "managed_team_refs": []}]
        recovered = [{"manager_id": 20, "managed_team_refs": [{"team_id": 200}]}]
        state = {
            "human_manager_sessions": partial,
            "human_manager_scanned_at": 95.0,
        }

        with (
            patch("tools.preview_cup_odds._runtime_state", return_value=state),
            patch("tools.preview_cup_odds.monotonic", return_value=100.0),
            patch(
                "tools.preview_cup_odds._discover_human_managers_direct",
                return_value=recovered,
            ) as discover,
        ):
            result = _cached_direct_human_managers(Mock())

        discover.assert_called_once()
        self.assertEqual(result, recovered)
        self.assertEqual(state["human_manager_sessions"], recovered)

    def test_complete_direct_manager_cache_is_reused_before_expiry(self):
        complete = [{"manager_id": 20, "managed_team_refs": [{"team_id": 200}]}]
        state = {
            "human_manager_sessions": complete,
            "human_manager_scanned_at": 95.0,
        }

        with (
            patch("tools.preview_cup_odds._runtime_state", return_value=state),
            patch("tools.preview_cup_odds.monotonic", return_value=100.0),
            patch(
                "tools.preview_cup_odds._discover_human_managers_direct",
            ) as discover,
        ):
            result = _cached_direct_human_managers(Mock())

        discover.assert_not_called()
        self.assertEqual(result, complete)

    def test_legacy_single_managed_team_is_normalized(self):
        team = {"id": 10, "team_type": "club"}

        self.assertEqual(managed_team_rows({"managed_team": team}), [team])

    def test_managed_team_collection_takes_precedence(self):
        teams = [{"id": 10}, {"id": 20}]

        self.assertEqual(
            managed_team_rows({"managed_team": {"id": 30}, "managed_teams": teams}),
            teams,
        )

    def test_same_manager_new_club_is_detected(self):
        self.assertTrue(manager_team_context_changed(_context([10]), _context([20])))

    def test_club_to_club_change_requires_user_confirmation(self):
        current = {**_context([10]), "save_instance_id": "save-a"}
        refreshed = {**_context([20]), "save_instance_id": "save-a"}

        self.assertTrue(
            manager_club_change_requires_confirmation(current, refreshed)
        )
        self.assertEqual(
            manager_club_change_confirmation_key(current, refreshed),
            ("save-a", 123, (10,), (20,)),
        )

    def test_first_club_after_unemployment_does_not_use_change_prompt(self):
        current = {**_context([]), "save_instance_id": "save-a"}
        refreshed = {**_context([20]), "save_instance_id": "save-a"}

        self.assertFalse(
            manager_club_change_requires_confirmation(current, refreshed)
        )

    def test_national_assignment_change_does_not_use_club_change_prompt(self):
        current = {**_context([10]), "save_instance_id": "save-a"}
        refreshed = {**_context([10]), "save_instance_id": "save-a"}
        refreshed["managed_teams"].append({"id": 86, "team_type": "national"})

        self.assertFalse(
            manager_club_change_requires_confirmation(current, refreshed)
        )

    def test_confirmed_unemployed_manager_joining_club_is_detected(self):
        current = _context([])
        current["unemployed_confirmed"] = True

        self.assertTrue(manager_team_context_changed(current, _context([20])))

    def test_unconfirmed_unemployed_manager_joining_club_is_positive_evidence(self):
        self.assertTrue(manager_team_context_changed(_context([]), _context([20])))

    def test_confirmed_unemployed_manager_joining_national_team_is_detected(self):
        current = _context([])
        current.update({
            "unemployed_manager_detected": True,
            "unemployed_confirmed": True,
        })
        national = {"id": 765, "team_type": "national", "name": "England"}
        refreshed = {
            "selected_manager_id": 123,
            "manager": {"id": 123},
            "managed_team": national,
            "managed_teams": [national],
        }

        self.assertTrue(manager_assignment_context_changed(current, refreshed))
        self.assertFalse(
            manager_assignment_absence_requires_confirmation(current, refreshed)
        )

    def test_club_resignation_is_detected_from_authoritative_empty_context(self):
        refreshed = _context([])
        refreshed["unemployed_manager_detected"] = True

        self.assertTrue(manager_team_context_changed(_context([10]), refreshed))
        self.assertTrue(
            manager_assignment_absence_requires_confirmation(_context([10]), refreshed)
        )

    def test_unverified_empty_context_is_not_treated_as_resignation(self):
        self.assertFalse(manager_team_context_changed(_context([10]), _context([])))

    def test_club_resignation_with_national_job_is_a_club_change(self):
        current = _context([10])
        refreshed = _context([])
        refreshed["managed_team"] = {"id": 86, "team_type": "national", "name": "National"}
        refreshed["managed_teams"] = [refreshed["managed_team"]]

        self.assertTrue(manager_team_context_changed(current, refreshed))
        self.assertTrue(manager_assignment_context_changed(current, refreshed))
        self.assertTrue(
            manager_assignment_absence_requires_confirmation(current, refreshed)
        )

    def test_team_order_does_not_trigger_change(self):
        self.assertFalse(manager_team_context_changed(_context([10, 20]), _context([20, 10])))

    def test_national_team_change_does_not_release_club_training_users(self):
        current = _context([10])
        current["managed_teams"].append({"id": 86, "team_type": "national"})
        refreshed = _context([10])
        refreshed["managed_teams"].append({"id": 99, "team_type": "national"})

        self.assertFalse(manager_team_context_changed(current, refreshed))
        self.assertTrue(manager_assignment_context_changed(current, refreshed))

    def test_manager_option_assignment_change_refreshes_cached_manager_list(self):
        current = {"manager_options": [{
            "id": 10,
            "managed_team_refs": [{"team_id": 100, "team_type": "club"}],
        }]}
        refreshed = {"manager_options": [{"id": 10, "managed_team_refs": []}]}

        self.assertTrue(manager_options_changed(current, refreshed))

    def test_manager_context_confirmation_key_ignores_team_order(self):
        current = _context([10, 20])
        refreshed = _context([20, 10])

        self.assertEqual(
            manager_context_confirmation_key(current),
            manager_context_confirmation_key(refreshed),
        )

    def test_different_manager_is_not_treated_as_team_change(self):
        self.assertFalse(manager_team_context_changed(_context([10], 123), _context([20], 456)))

    def test_same_manager_in_different_save_does_not_release_training_users(self):
        current = {**_context([10]), "save_instance_id": "save-a"}
        refreshed = {**_context([20]), "save_instance_id": "save-b"}

        self.assertFalse(same_account_manager_club_changed(current, refreshed))

    def test_same_account_resignation_releases_club_training_users(self):
        current = {**_context([10]), "save_instance_id": "save-a"}
        refreshed = {
            **_context([]),
            "save_instance_id": "save-a",
            "unemployed_manager_detected": True,
        }

        self.assertTrue(same_account_manager_club_changed(current, refreshed))

    def test_authoritative_unemployment_does_not_restore_stale_cached_club(self):
        cached = _context([10])
        refreshed = {
            **_context([]),
            "unemployed_manager_detected": True,
        }

        self.assertFalse(
            should_retain_cached_manager_context(cached, refreshed, same_save=True)
        )

    def test_unverified_missing_assignment_temporarily_retains_cached_club(self):
        self.assertTrue(
            should_retain_cached_manager_context(
                _context([10]), _context([]), same_save=True,
            )
        )

    def test_old_club_profile_is_not_reused_after_resignation_or_transfer(self):
        profile = {"manager": {"id": 123}, "team": {"id": 10}}

        self.assertIsNone(matching_manager_team_profile(profile, 123, 0))
        self.assertIsNone(matching_manager_team_profile(profile, 123, 20))
        self.assertIs(profile, matching_manager_team_profile(profile, 123, 10))


if __name__ == "__main__":
    unittest.main()
