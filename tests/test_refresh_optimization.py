from __future__ import annotations

import ast
import threading
import textwrap
import unittest
import inspect
import struct
from contextlib import nullcontext
from datetime import date, datetime, timedelta
from pathlib import Path
from time import perf_counter
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fm_odds_web import (
    background_pool_reconcile_requires_heap,
    LIVE_SETTLEMENT_CONFIRMATIONS,
    LIVE_SETTLEMENT_POLL_SECONDS,
    LocalOddsState,
    advance_identity_change_confirmation,
    club_preload_targets,
    disappeared_same_day_result_keys,
    fast_refresh_crossed_save,
    fixture_at_game_clock,
    fm24_spectator_session_from_native_state,
    full_reconcile_due,
    managed_club_profiles_are_current,
    preserve_previous_schedule_after_empty_read,
    read_betting_match_engine_state,
    settlement_keys_safe_for_engine_state,
    should_refresh_club_profiles_after_refresh,
    suspicious_empty_schedule_refresh,
    suspicious_partial_schedule_refresh,
    startup_cache_validation,
    startup_odds_refresh_mode,
    latest_snapshot,
    light_result_refresh_changed,
    manager_team_context_status,
    validate_identity_change_output,
    validate_active_match_bet_times,
    validate_betting_clock_sync,
    validate_bet_times,
    validate_permanent_spectator_locks,
    validate_bet_snapshot,
    validate_spectator_bet_times,
    match_engine_restricted_legs,
)
from tools import club_reader, preview_cup_odds
from tools.app_paths import set_active_save_id
from tools.local_state_services import RefreshCoordinator
from tools.live_market import market_output
from tools.preview_cup_odds import (
    CHAMPIONSHIP_DISCOVERY_DAYS,
    MODEL_VERSION,
    persistent_competition_format_snapshots,
    read_completed_results,
    select_current_competition_format_snapshots,
)


def _snapshot() -> dict:
    return {
        "matches": [{"fixture_date": "2028-06-06"}],
        "save_instance_id": "save-123",
        "game_date": "2028-06-06",
        "fixture_scan_mode": "heap",
        "full_verified_at": "2028-06-06T12:00:00",
        "model_version": MODEL_VERSION,
        "odds_days": 7,
        "odds_scope": "famous",
    }


class RefreshOptimizationTests(unittest.TestCase):

    def test_background_page_reads_use_nonforeground_memory_priority(self):
        state = LocalOddsState.__new__(LocalOddsState)
        state.lock = threading.RLock()
        state.output = {
            "save_instance_id": "save-123",
            "data_scope_id": "account-42",
            "game_date": "2028-06-06",
        }
        state.memory_lock = MagicMock()
        state.memory_lock.__enter__.return_value = None
        state._has_verified_save = MagicMock(return_value=True)
        state._data_scope_id = MagicMock(return_value="account-42")
        timed_foreground = MagicMock(return_value=nullcontext())
        state._timed_user_memory_operation = timed_foreground

        with (
            patch("fm_odds_web.search_world_players", return_value={
                "players": [], "pagination": {}, "index": {},
            }),
            patch("fm_odds_web.set_active_save_id"),
            patch("fm_odds_web.apply_player_aliases"),
        ):
            result = state.public_world_players(
                live_summary=False, background=True,
            )

        self.assertTrue(result["ready"])
        state.memory_lock.__enter__.assert_called_once()
        timed_foreground.assert_not_called()

    def test_background_relationship_prewarm_uses_shared_memory_gate(self):
        state = LocalOddsState.__new__(LocalOddsState)
        state.lock = threading.RLock()
        state.data_version = 7
        state.memory_lock = MagicMock()
        state.memory_lock.__enter__.return_value = None
        state.relations_people = MagicMock(return_value={"people": []})
        operation = MagicMock()
        context = MagicMock()
        context.__enter__.return_value = operation
        context.__exit__.return_value = False

        with (
            patch("fm_odds_web.borrow_game_operation", return_value=context),
            patch("fm_odds_web.enrich_relationship_page", return_value={"people": []}),
        ):
            result = state.relations_network({"background": True})

        self.assertEqual(result["data_version"], 7)
        state.memory_lock.__enter__.assert_called_once()

    def test_background_relationship_entity_resolution_uses_shared_memory_gate(self):
        state = LocalOddsState.__new__(LocalOddsState)
        state.memory_lock = MagicMock()
        state.memory_lock.__enter__.return_value = None
        operation = MagicMock()
        context = MagicMock()
        context.__enter__.return_value = operation
        context.__exit__.return_value = False

        with (
            patch("fm_odds_web.borrow_game_operation", return_value=context),
            patch("fm_odds_web.resolve_relationship_entities", return_value={
                "entities": {},
            }),
        ):
            result = state.relations_entities({
                "person_keys": ["person:1"], "background": True,
            })

        self.assertEqual(result, {"entities": {}})
        state.memory_lock.__enter__.assert_called_once()

    def test_refresh_status_does_not_wait_for_public_state_lock(self):
        host = SimpleNamespace(
            lock=threading.RLock(),
            refreshing=True,
            reconciling=False,
            refresh_mode="fast",
            cache_verified=False,
            data_version=7,
            status="正在读取存档",
            error=None,
            last_refresh_error_stage=None,
            refresh_reason="connect_save",
            refresh_stage="result_fixture_links",
            refresh_progress={"phase": "result_fixture_links", "current": 3},
            _refresh_progress_updated_at=perf_counter(),
            _refresh_stage_started_at=perf_counter(),
            output={},
            connection_requested=True,
        )
        locked = threading.Event()
        release = threading.Event()

        def hold_state_lock():
            with host.lock:
                locked.set()
                release.wait(1.0)

        worker = threading.Thread(target=hold_state_lock)
        worker.start()
        self.assertTrue(locked.wait(0.5))
        started_at = perf_counter()
        try:
            status = RefreshCoordinator(host).status()
        finally:
            release.set()
            worker.join(1.0)

        self.assertLess(perf_counter() - started_at, 0.25)
        self.assertTrue(status["refreshing"])
        self.assertTrue(status["connection_pending"])
        self.assertTrue(status["status_snapshot_delayed"])
        self.assertEqual(status["refresh_stage"], "result_fixture_links")
        self.assertEqual(status["refresh_progress"]["current"], 3)

    def test_date_change_salary_settlement_binds_account_without_odds_refresh(self):
        state = LocalOddsState.__new__(LocalOddsState)
        state.lock = threading.RLock()
        state.cache_verified = True
        state.output = {
            "save_instance_id": "save-123",
            "account_scope_id": "account-42",
        }
        state.club_contexts = {
            679: {"contract": {"gross_weekly_display": 7_000.0}},
        }
        state.salary_last_processed_date = "2028-06-05"
        state.status = "ready"

        with (
            patch("fm_odds_web.set_active_save_id") as bind_scope,
            patch("fm_odds_web.process_salary_payments", return_value={
                "paid": 1, "amount": 4_200.0,
            }) as process_salary,
        ):
            result = state._process_manager_salary_for_date("2028-06-06")

        bind_scope.assert_called_once_with("account-42")
        process_salary.assert_called_once_with("2028-06-06", 7_000.0)
        self.assertEqual(result, {"paid": 1, "amount": 4_200.0})
        self.assertEqual(state.salary_last_processed_date, "2028-06-06")
        self.assertIn("工资存入银行", state.status)

    def test_salary_date_hook_is_independent_of_auto_refresh_setting(self):
        source = inspect.getsource(LocalOddsState.watch_game_date)

        salary_hook = source.index("self._process_manager_salary_for_date")
        auto_refresh_gate = source.index("auto_refresh_seconds > 0")
        self.assertLess(salary_hook, auto_refresh_gate)

    def test_club_profiles_are_refreshed_only_when_context_is_stale(self):
        source = inspect.getsource(LocalOddsState._refresh_worker)

        self.assertIn(
            "should_refresh_club_profiles_after_refresh(",
            source,
        )
        self.assertLess(
            source.index('self._set_refresh_stage("publish_snapshot")'),
            source.index("self.refresh_club_async()"),
        )
        club_effects = source.split(
            'with self._timed_memory_lock("club_effects"):', 1,
        )[1].split('self._set_refresh_stage("persist_snapshot")', 1)[0]
        self.assertNotIn("_process_due_medical_treatments", club_effects)
        self.assertNotIn("treatment_due or language_due", club_effects)

        self.assertTrue(should_refresh_club_profiles_after_refresh(
            {"id": 10}, [{"id": 100, "team_type": "club"}],
        ))
        self.assertFalse(should_refresh_club_profiles_after_refresh(
            {}, [{"id": 100, "team_type": "club"}],
        ))
        self.assertFalse(should_refresh_club_profiles_after_refresh(
            {"id": 10}, [{"id": 0, "team_type": "club"}],
        ))
        current_profile = {
            "game_date": "2028-06-06",
            "profile_stage": "complete",
            "manager": {"id": 10},
            "team": {"id": 100, "team_type": "club"},
            "players": [{"id": 1}],
            "staff": [],
        }
        self.assertTrue(managed_club_profiles_are_current(
            {"id": 10}, [{"id": 100, "team_type": "club"}],
            {100: current_profile}, "2028-06-06",
        ))
        self.assertFalse(managed_club_profiles_are_current(
            {"id": 10}, [{"id": 100, "team_type": "club"}],
            {100: current_profile}, "2028-06-07",
        ))
        self.assertFalse(managed_club_profiles_are_current(
            {"id": 11}, [{"id": 100, "team_type": "club"}],
            {100: current_profile}, "2028-06-06",
        ))

    def test_startup_does_not_route_fm24_into_a_competing_club_priority_read(self):
        source = inspect.getsource(LocalOddsState._startup_worker)

        # The experimental FM24 cold-start branch marked odds refresh as idle
        # while a full roster scan still held memory_lock.  That made facility
        # actions wait and did not actually make the roster available sooner.
        self.assertNotIn("fm24_cold_start", source)
        self.assertIn('self._refresh_worker("startup_odds", mode, False, cancel_event)', source)

    def test_club_profile_publishes_players_before_staff_scan(self):
        source = inspect.getsource(club_reader.read_club_profile)

        self.assertIn("partial_callback", source)
        self.assertLess(source.index("partial_callback({"), source.index("staff = _scan_staff("))

    def test_due_treatment_waits_for_valid_manager_team_context(self):
        self.assertEqual(club_preload_targets({}, [], True), [])
        self.assertEqual(
            club_preload_targets({"id": 10}, [{"id": 0}], True),
            [],
        )

    def test_due_treatment_preloads_only_valid_managed_teams(self):
        valid = {"id": 100, "team_type": "club"}

        self.assertEqual(
            club_preload_targets(
                {"id": 10}, [valid, {"id": 0}, {}], True,
            ),
            [valid],
        )
        self.assertEqual(
            club_preload_targets({"id": 10}, [valid], False),
            [],
        )

    @staticmethod
    def _fm26_match_state_reader(
        states: list[int | None],
        *,
        phase: int = 0,
        mode: int = 4,
        queue_counts: tuple[int, int] = (0, 0),
    ):
        layout = SimpleNamespace(
            key="fm26", distribution="steam", display_name="Football Manager 26",
            match_engine_phase_rva=0x100,
            match_engine_mode_rva=0x200,
            match_engine_active_state_rva=0x300,
            play_fixture_manager_pointer_rva=0x400,
            play_fixture_manager_vtable_rva=0x500,
        )
        module = SimpleNamespace(base_address=0x100000)
        reader = MagicMock()
        reader.u32.side_effect = [phase, mode]
        reader.u8.side_effect = states
        manager = 0x200000
        reader.ptr.side_effect = [manager, module.base_address + 0x500]
        headers = []
        for index, count in enumerate(queue_counts):
            begin = 0x300000 + index * 0x10000
            end = begin + count * 8
            headers.append(struct.pack("<QQQ", begin, end, end))
        reader.bytes.side_effect = headers
        context = MagicMock()
        context.__enter__.return_value = ("fm.exe", layout, module, reader)
        context.__exit__.return_value = False
        return context, reader

    @staticmethod
    def _fm24_match_state_reader(viewer_count: int):
        layout = SimpleNamespace(
            key="fm24", display_name="Football Manager 2024",
            match_session_pointer_rva=0x100,
            match_session_vtable_rva=0x200,
            game_match_session_vtable_rva=0x300,
        )
        module = SimpleNamespace(base_address=0x100000)
        manager = 0x200000
        slots = 0x300000
        session = 0x400000
        viewer_begin = 0x500000
        reader = MagicMock()

        def read_ptr(address):
            return {
                module.base_address + 0x100: manager,
                manager: module.base_address + 0x200,
                session: module.base_address + 0x300,
            }.get(address)

        def read_bytes(address, size):
            if address == manager + 0x08 and size == 0x18:
                return struct.pack("<QQQ", slots, slots + 8, slots + 8)
            if address == slots and size == 8:
                return struct.pack("<Q", session)
            if address == session + 0x28 and size == 0x18:
                end = viewer_begin + viewer_count * 8
                return struct.pack("<QQQ", viewer_begin, end, end)
            return None

        reader.ptr.side_effect = read_ptr
        reader.bytes.side_effect = read_bytes
        context = MagicMock()
        context.__enter__.return_value = ("fm.exe", layout, module, reader)
        context.__exit__.return_value = False
        return context

    def test_fm24_live_game_session_viewer_marks_spectator(self):
        context = self._fm24_match_state_reader(1)
        with patch("tools.preview_cup_odds.open_supported_reader", return_value=context):
            state = preview_cup_odds.read_match_engine_state()

        self.assertTrue(state["known"])
        self.assertTrue(state["active"])
        self.assertTrue(state["spectator"])
        self.assertEqual(state["session_count"], 1)
        self.assertEqual(state["spectator_session_count"], 1)

    def test_fm24_retained_game_session_after_home_return_is_not_spectator(self):
        context = self._fm24_match_state_reader(0)
        with patch("tools.preview_cup_odds.open_supported_reader", return_value=context):
            state = preview_cup_odds.read_match_engine_state()

        self.assertTrue(state["active"])
        self.assertFalse(state["spectator"])
        self.assertEqual(state["spectator_session_count"], 0)

    def test_fm26_player_prematch_empty_processing_queues_are_inactive(self):
        context, reader = self._fm26_match_state_reader([0, 0])

        with patch("tools.preview_cup_odds.open_supported_reader", return_value=context):
            state = preview_cup_odds.read_match_engine_state()

        self.assertEqual(state["state"], 0)
        self.assertTrue(state["known"])
        self.assertFalse(state["active"])
        self.assertFalse(state["spectator"])
        self.assertEqual(
            state["detector"], "native_match_runtime_and_processing_queues",
        )
        self.assertEqual(state["processing_queue_counts"], [0, 0])
        self.assertEqual(reader.u8.call_count, 2)

    def test_fm26_player_live_and_replay_runtime_states_are_active(self):
        for runtime_state in (4, 6):
            with self.subTest(runtime_state=runtime_state):
                context, _reader = self._fm26_match_state_reader(
                    [runtime_state, runtime_state],
                )
                with patch(
                    "tools.preview_cup_odds.open_supported_reader", return_value=context,
                ):
                    state = preview_cup_odds.read_match_engine_state()

                self.assertEqual(state["state"], runtime_state)
                self.assertTrue(state["known"])
                self.assertTrue(state["active"])
                self.assertFalse(state["spectator"])

    def test_fm26_v200_processing_queues_mark_active_spectator(self):
        context, _reader = self._fm26_match_state_reader(
            [0, 0], queue_counts=(24, 74),
        )

        with patch("tools.preview_cup_odds.open_supported_reader", return_value=context):
            state = preview_cup_odds.read_match_engine_state()

        self.assertTrue(state["known"])
        self.assertTrue(state["active"])
        self.assertTrue(state["spectator"])
        self.assertEqual(state["processing_fixture_count"], 98)
        self.assertEqual(
            state["detector"], "native_match_runtime_and_processing_queues",
        )

    def test_fm26_v200_processing_queues_do_not_use_playback_discovery(self):
        context, reader = self._fm26_match_state_reader(
            [0, 0], queue_counts=(0, 110),
        )
        _path, layout, module, _reader = context.__enter__.return_value
        playback_state = {
            "active": True,
            "spectator": False,
            "candidate_count": 1,
            "valid_candidate_count": 1,
            "viewable_match_type": 0,
            "match_kicked_off": True,
        }

        with patch(
            "tools.preview_cup_odds._read_fm26_match_playback_state",
            return_value=playback_state,
        ) as read_playback:
            state = preview_cup_odds.read_match_engine_state_from_reader(
                reader,
                layout=layout,
                module_base=module.base_address,
                allow_spectator_discovery=True,
                allow_large_match_playback_discovery=True,
            )

        self.assertTrue(state["active"])
        self.assertTrue(state["spectator"])
        self.assertEqual(state["processing_queue_counts"], [0, 110])
        self.assertEqual(
            state["detector"], "native_match_runtime_and_processing_queues",
        )
        read_playback.assert_not_called()

    def test_player_playback_large_scan_trigger_requires_exact_fixture_clock(self):
        output = {"matches": [{
            "fixture_date": "2028-06-06", "kickoff_minutes": 900,
        }]}

        self.assertTrue(fixture_at_game_clock(
            output, {"date": "2028-06-06", "minutes": 900},
        ))
        self.assertFalse(fixture_at_game_clock(
            output, {"date": "2028-06-06", "minutes": 899},
        ))
        self.assertFalse(fixture_at_game_clock(
            output, {"date": "2028-06-07", "minutes": 900},
        ))

    def test_fm26_active_spectator_requires_live_controller(self):
        setup = 0x1000
        controller = 0x2000
        setup_class = 0x3000
        controller_class = 0x4000
        name_address = 0x5000
        namespace_address = 0x6000
        reader = MagicMock()
        pointers = {
            setup: setup_class,
            setup + 0x550: controller,
            controller + 0x108: setup,
            controller: controller_class,
            controller_class + 0x10: name_address,
            controller_class + 0x18: namespace_address,
            controller + 0x10: 0x7000,
        }
        reader.ptr.side_effect = lambda address: pointers.get(address)
        reader.bytes.side_effect = lambda address, _size: {
            name_address: b"MatchPlaybackController\0",
            namespace_address: b"FM.Match\0",
        }.get(address)
        for kicked_off in (0, 1):
            with self.subTest(kicked_off=kicked_off):
                reader.u8.side_effect = lambda address: {
                    setup + 0x518: 1,
                    controller + 0x11B: kicked_off,
                }.get(address)

                state = preview_cup_odds._validate_fm26_match_setup(
                    reader, setup, setup_class,
                )

                self.assertTrue(state["active"])
                self.assertTrue(state["spectator"])

    def test_fm26_closed_controller_is_not_active_spectator(self):
        with patch(
            "tools.preview_cup_odds._il2cpp_class_identity",
            return_value=("MatchPlaybackController", "FM.Match"),
        ):
            for kicked_off in (0, 1):
                with self.subTest(kicked_off=kicked_off):
                    reader = MagicMock()
                    reader.ptr.side_effect = [0x3000, 0x2000, 0x1000, 0x4000, None]
                    reader.u8.side_effect = [1, kicked_off]

                    state = preview_cup_odds._validate_fm26_match_setup(
                        reader, 0x1000, 0x3000,
                    )

                    self.assertFalse(state["active"])
                    self.assertFalse(state["spectator"])

    def test_fm26_betting_read_never_discovers_or_scans_match_setups(self):
        reader = MagicMock()
        reader.process.pid = 99
        layout = SimpleNamespace()
        preview_cup_odds._FM26_MATCH_PLAYBACK_CACHE.pop(99, None)

        with (
            patch("tools.preview_cup_odds.find_module") as find_module,
            patch("tools.preview_cup_odds._scan_fm26_match_setups") as scan,
        ):
            state = preview_cup_odds._read_fm26_match_playback_state(
                reader, layout, allow_discovery=False,
            )

        self.assertFalse(state["active"])
        self.assertFalse(state["spectator"])
        find_module.assert_not_called()
        scan.assert_not_called()

    def test_fm26_spectator_prematch_and_transition_state_are_inactive(self):
        for runtime_state in (0, 24):
            with self.subTest(runtime_state=runtime_state):
                context, _reader = self._fm26_match_state_reader(
                    [runtime_state, runtime_state], phase=1, mode=0,
                )
                with patch(
                    "tools.preview_cup_odds.open_supported_reader", return_value=context,
                ):
                    state = preview_cup_odds.read_match_engine_state()

                self.assertTrue(state["known"])
                self.assertFalse(state["active"])

    def test_fm26_runtime_state_transition_is_unknown(self):
        context, _reader = self._fm26_match_state_reader([0, 4])

        with patch("tools.preview_cup_odds.open_supported_reader", return_value=context):
            state = preview_cup_odds.read_match_engine_state()

        self.assertFalse(state["known"])
        self.assertFalse(state["active"])
        self.assertIn("changed during read", state["reason"])

    def test_settlement_recovery_refreshes_stale_same_day_result_cache(self):
        key = ("2028-06-06", 300, 10, 20)
        result = {
            "date": key[0],
            "competition": {"id": key[1]},
            "home_team": {"id": key[2]},
            "away_team": {"id": key[3]},
            "home_goals": 2,
            "away_goals": 1,
        }
        layout = SimpleNamespace(key="fm26")
        reader = object()
        context = MagicMock()
        context.__enter__.return_value = ("fm.exe", layout, object(), reader)
        context.__exit__.return_value = False

        with (
            patch("tools.preview_cup_odds.open_supported_reader", return_value=context),
            patch("tools.preview_cup_odds.read_layout_game_date_code", return_value=1),
            patch("tools.preview_cup_odds.decode_date", return_value=date(2028, 6, 6)),
            patch("tools.preview_cup_odds.discover_manager_session", return_value=None),
            patch("tools.preview_cup_odds.read_savegame_identity", return_value={"savegame_id": "save-1"}),
            patch("tools.preview_cup_odds.resolve_save_identity", return_value="career-1"),
            patch(
                "tools.preview_cup_odds.cached_completed_results",
                side_effect=[([], True), ([result], False)],
            ) as cached,
            patch("tools.preview_cup_odds.enrich_halftime_results", return_value=[result]),
            patch("tools.preview_cup_odds.remember_completed_results"),
            patch("tools.preview_cup_odds.save_result_history"),
        ):
            recovered = read_completed_results({key})

        self.assertEqual(recovered, [result])
        self.assertEqual(cached.call_count, 2)
        self.assertTrue(cached.call_args_list[1].kwargs["force_scan"])

    def test_fixture_hint_hit_avoids_forced_global_result_scan(self):
        key = ("2028-06-06", 300, 10, 20)
        result = {
            "date": key[0],
            "competition": {"id": key[1]},
            "home_team": {"id": key[2]},
            "away_team": {"id": key[3]},
            "home_goals": 2,
            "away_goals": 1,
        }
        layout = SimpleNamespace(key="fm26")
        reader = object()
        context = MagicMock()
        context.__enter__.return_value = ("fm.exe", layout, object(), reader)
        context.__exit__.return_value = False

        with (
            patch("tools.preview_cup_odds.open_supported_reader", return_value=context),
            patch("tools.preview_cup_odds.read_layout_game_date_code", return_value=1),
            patch("tools.preview_cup_odds.decode_date", return_value=date(2028, 6, 7)),
            patch("tools.preview_cup_odds.discover_manager_session", return_value=None),
            patch("tools.preview_cup_odds.read_savegame_identity", return_value={"savegame_id": "save-1"}),
            patch("tools.preview_cup_odds.resolve_save_identity", return_value="career-1"),
            patch(
                "tools.preview_cup_odds.cached_completed_results",
                return_value=([], True),
            ) as cached,
            patch(
                "tools.preview_cup_odds.recover_fixture_hint_results",
                return_value=[result],
            ),
            patch("tools.preview_cup_odds.enrich_halftime_results", return_value=[result]),
            patch("tools.preview_cup_odds.remember_completed_results"),
            patch("tools.preview_cup_odds.save_result_history"),
        ):
            recovered = read_completed_results({key}, fixture_hints={key: 0x1234})

        self.assertEqual(recovered, [result])
        cached.assert_called_once()

    def test_manual_deep_recovery_rebuilds_fixture_index_and_bypasses_fallback_throttle(self):
        key = ("2028-06-06", 300, 10, 20)
        result = {
            "date": key[0],
            "competition": {"id": key[1]},
            "home_team": {"id": key[2]},
            "away_team": {"id": key[3]},
            "home_goals": 2,
            "away_goals": 1,
        }
        layout = SimpleNamespace(key="fm26")
        reader = object()
        context = MagicMock()
        context.__enter__.return_value = ("fm.exe", layout, object(), reader)
        context.__exit__.return_value = False

        with (
            patch("tools.preview_cup_odds.open_supported_reader", return_value=context),
            patch("tools.preview_cup_odds.read_layout_game_date_code", return_value=1),
            patch("tools.preview_cup_odds.decode_date", return_value=date(2028, 6, 7)),
            patch("tools.preview_cup_odds.discover_manager_session", return_value=None),
            patch("tools.preview_cup_odds.read_savegame_identity", return_value={"savegame_id": "save-1"}),
            patch("tools.preview_cup_odds.resolve_save_identity", return_value="career-1"),
            patch(
                "tools.preview_cup_odds.cached_completed_results",
                return_value=([], False),
            ) as cached,
            patch("tools.preview_cup_odds.recover_fixture_hint_results", return_value=[]),
            patch(
                "tools.preview_cup_odds.recover_persistent_results",
                return_value=[result],
            ) as persistent,
            patch("tools.preview_cup_odds.merge_verified_results", side_effect=lambda rows: rows),
            patch("tools.preview_cup_odds.merge_result_history", side_effect=lambda rows, _save: rows),
            patch("tools.preview_cup_odds.resolve_daily_team_conflicts", side_effect=lambda rows: rows),
            patch("tools.preview_cup_odds.enrich_halftime_results", return_value=[result]),
            patch("tools.preview_cup_odds.remember_completed_results"),
            patch("tools.preview_cup_odds.save_result_history"),
        ):
            recovered = read_completed_results({key}, deep_recovery=True)

        self.assertEqual(recovered, [result])
        self.assertTrue(cached.call_args.kwargs["force_fixture_scan"])
        persistent.assert_called_once_with(
            reader, "2028-06-07", {key}, force=True,
        )

    def test_manual_result_button_requests_deep_recovery(self):
        source = Path(inspect.getsourcefile(LocalOddsState)).read_text(encoding="utf-8")
        source = source.split("def search_pending_bet_result", 1)[1].split(
            "def ", 1,
        )[0]

        self.assertIn("deep_recovery=True", source)

    def test_full_refresh_does_not_eagerly_build_detailed_markets(self):
        source = inspect.getsource(LocalOddsState._refresh_worker)
        self.assertNotIn("ensure_match_market_catalog", source)
        self.assertIn(
            "force_fixture_heap_scan=False", source,
        )

    def test_snapshot_persistence_does_not_hold_game_memory_lock(self):
        tree = ast.parse(textwrap.dedent(inspect.getsource(LocalOddsState._refresh_worker)))
        memory_blocks = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.With)
            and any(
                (
                    isinstance(item.context_expr, ast.Attribute)
                    and item.context_expr.attr == "memory_lock"
                ) or (
                    isinstance(item.context_expr, ast.Call)
                    and isinstance(item.context_expr.func, ast.Attribute)
                    and item.context_expr.func.attr == "_timed_memory_lock"
                )
                for item in node.items
            )
        ]
        locked_calls = {
            call.func.id if isinstance(call.func, ast.Name) else call.func.attr
            for block in memory_blocks
            for call in ast.walk(block)
            if isinstance(call, ast.Call)
            and isinstance(call.func, (ast.Name, ast.Attribute))
        }

        self.assertNotIn("save_odds", locked_calls)
        self.assertNotIn("archive_season_output", locked_calls)
        self.assertNotIn("apply_storage_retention", locked_calls)
        self.assertNotIn("settle_pending_bets", locked_calls)
        self.assertNotIn("publish_championship_markets", locked_calls)
        self.assertNotIn("settle_championship_bets", locked_calls)
        self.assertNotIn("archive_winning_mail", locked_calls)
        self.assertNotIn("_sync_youth_generation_plans", locked_calls)
        self.assertNotIn("_reconcile_owned_club_customizations", locked_calls)

        timed_phases = {
            item.context_expr.args[0].value
            for node in ast.walk(tree)
            if isinstance(node, ast.With)
            for item in node.items
            if isinstance(item.context_expr, ast.Call)
            and isinstance(item.context_expr.func, ast.Attribute)
            and item.context_expr.func.attr == "_timed_memory_lock"
            and item.context_expr.args
            and isinstance(item.context_expr.args[0], ast.Constant)
        }
        self.assertEqual(timed_phases, {"odds_read", "club_effects"})

    def test_fast_refresh_archives_latest_forecast_after_snapshot_persist(self):
        source = inspect.getsource(LocalOddsState._refresh_worker)
        persist_block = source.split(
            'self._set_refresh_stage("persist_snapshot")', 1,
        )[1].split("retention =", 1)[0]

        self.assertIn("save_odds(output, path)", persist_block)
        self.assertIn("archive_season_output(output)", persist_block)
        self.assertNotIn('if mode == "full":', persist_block)

        maintenance = inspect.getsource(
            LocalOddsState._post_publish_maintenance_worker,
        )
        self.assertIn('self._timed_memory_lock("youth_generation_sync")', maintenance)
        self.assertIn("self._sync_owned_club_customizations(", maintenance)
        owned_sync = inspect.getsource(
            LocalOddsState._sync_owned_club_customizations,
        )
        self.assertIn(
            'self._timed_memory_lock("owned_customization_sync")', owned_sync,
        )

    def test_club_refresh_reconciles_owned_club_customizations(self):
        lines = inspect.getsource(LocalOddsState._club_refresh_worker).splitlines()

        call_index = next(
            index for index, line in enumerate(lines)
            if "self._sync_owned_club_customizations(" in line
        )
        lock_index = next(
            index for index, line in enumerate(lines)
            if "with self._club_memory_read_lock():" in line
        )
        self.assertLess(lock_index, call_index)
        self.assertEqual(
            len(lines[call_index]) - len(lines[call_index].lstrip()),
            len(lines[lock_index]) - len(lines[lock_index].lstrip()),
        )

    def test_post_publish_maintenance_starts_after_refresh_flag_is_cleared(self):
        source = inspect.getsource(LocalOddsState._refresh_worker)

        publish = source.index('self._set_refresh_stage("publish_snapshot")')
        clear_refresh = source.index("self.refreshing = False")
        maintenance = source.index("self._start_post_publish_maintenance(")
        self.assertLess(publish, clear_refresh)
        self.assertLess(clear_refresh, maintenance)

    def test_post_publish_maintenance_skips_a_replaced_snapshot(self):
        state = self._state(verified=True)
        stale = state.output
        state.output = {"save_instance_id": "save-456"}
        state.memory_lock = threading.RLock()
        state._sync_youth_generation_plans = MagicMock()
        state._reconcile_owned_club_customizations = MagicMock()

        state._post_publish_maintenance_worker(
            stale, reconcile_owned_customizations=True,
        )

        state._sync_youth_generation_plans.assert_not_called()
        state._reconcile_owned_club_customizations.assert_not_called()

    def test_post_publish_maintenance_runs_for_the_current_snapshot(self):
        state = self._state(verified=True)
        current = state.output
        state.memory_lock = threading.RLock()
        state.youth_generation_hook = MagicMock()
        state._sync_youth_generation_plans = MagicMock()
        state._reconcile_owned_club_customizations = MagicMock()

        state._post_publish_maintenance_worker(
            current, reconcile_owned_customizations=True,
        )

        state._sync_youth_generation_plans.assert_called_once_with(
            "save-123", "save-123",
        )
        state._reconcile_owned_club_customizations.assert_called_once_with(current)

    def test_light_result_business_work_does_not_hold_game_memory_lock(self):
        tree = ast.parse(textwrap.dedent(
            inspect.getsource(LocalOddsState._result_refresh_worker)
        ))
        memory_blocks = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.With)
            and any(
                isinstance(item.context_expr, ast.Attribute)
                and item.context_expr.attr == "memory_lock"
                for item in node.items
            )
        ]
        locked_calls = {
            call.func.id if isinstance(call.func, ast.Name) else call.func.attr
            for block in memory_blocks
            for call in ast.walk(block)
            if isinstance(call, ast.Call)
            and isinstance(call.func, (ast.Name, ast.Attribute))
        }

        self.assertNotIn("settle_pending_bets", locked_calls)
        self.assertNotIn("publish_championship_markets", locked_calls)
        self.assertNotIn("settle_championship_bets", locked_calls)
        self.assertNotIn("process_salary_payments", locked_calls)
        self.assertNotIn("save_odds", locked_calls)

    def test_priority_settlement_commits_after_releasing_game_memory_lock(self):
        state = self._state(verified=True)
        state.memory_lock = threading.Lock()
        state.refresh_trace = {}
        state.output = {
            "save_instance_id": "save-123",
            "game_date": "2028-06-06",
        }
        key = ("2028-06-05", 100, 10, 20)
        result = {
            "date": key[0],
            "competition": {"id": key[1]},
            "home_team": {"id": key[2]},
            "away_team": {"id": key[3]},
            "home_goals": 2,
            "away_goals": 1,
        }
        settlement = {
            "settled": 1, "settled_records": [{"bet_id": "bet-1"}],
            "returned": 250.0,
        }

        def settle(*_args, **_kwargs):
            self.assertFalse(state.memory_lock.locked())
            return settlement

        with (
            patch("fm_odds_web.read_game_clock", return_value={
                "date": "2028-06-06", "minutes": 1000,
            }),
            patch("fm_odds_web.pending_due_result_keys", return_value={key}),
            patch("fm_odds_web.read_match_engine_state", return_value={
                "known": True, "active": False,
            }),
            patch("fm_odds_web.probe_live_completed_results", return_value={
                "results": [result],
            }),
            patch("fm_odds_web.settle_pending_bets", side_effect=settle),
            patch.object(state, "_finalize_settlement") as finalize,
            patch("fm_odds_web.sleep"),
        ):
            resolved = state._settle_results_before_refresh()

        self.assertIs(resolved, settlement)
        finalize.assert_called_once_with(settlement, state.output)
        self.assertIn(
            "pre_refresh_result_read",
            state.refresh_trace["memory_lock_held_ms"],
        )

    def test_priority_settlement_skips_transient_invalid_clock(self):
        state = self._state(verified=True)
        state.memory_lock = threading.Lock()
        state.refresh_trace = {}

        with (
            patch("fm_odds_web.read_game_clock", return_value=None),
            patch("fm_odds_web.pending_due_result_keys") as pending,
            patch("fm_odds_web.settle_pending_bets") as settle,
        ):
            result = state._settle_results_before_refresh()

        self.assertEqual(result, {
            "settled": 0, "settled_records": [], "returned": 0.0,
        })
        self.assertFalse(state.memory_lock.locked())
        pending.assert_not_called()
        settle.assert_not_called()

    def test_priority_settlement_skips_transient_clock_read_error(self):
        state = self._state(verified=True)
        state.memory_lock = threading.Lock()
        state.refresh_trace = {}

        with (
            patch(
                "fm_odds_web.read_game_clock",
                side_effect=RuntimeError("temporary clock failure"),
            ),
            patch("fm_odds_web.pending_due_result_keys") as pending,
        ):
            result = state._settle_results_before_refresh()

        self.assertEqual(result["settled"], 0)
        self.assertFalse(state.memory_lock.locked())
        pending.assert_not_called()

    def test_priority_settlement_skips_transient_result_probe_error(self):
        state = self._state(verified=True)
        state.memory_lock = threading.Lock()
        state.refresh_trace = {}
        key = ("2028-06-05", 100, 10, 20)

        with (
            patch("fm_odds_web.read_game_clock", return_value={
                "date": "2028-06-06", "minutes": 1000,
            }),
            patch("fm_odds_web.pending_due_result_keys", return_value={key}),
            patch("fm_odds_web.read_match_engine_state", return_value={
                "known": True, "active": False,
            }),
            patch(
                "fm_odds_web.probe_live_completed_results",
                side_effect=RuntimeError("temporary read failure"),
            ),
            patch("fm_odds_web.settle_pending_bets") as settle,
        ):
            result = state._settle_results_before_refresh()

        self.assertEqual(result["settled"], 0)
        self.assertFalse(state.memory_lock.locked())
        settle.assert_not_called()

    def test_timed_memory_lock_releases_when_diagnostics_fail(self):
        state = self._state(verified=True)
        state.memory_lock = threading.Lock()

        with patch.object(
            state, "_record_refresh_metric",
            side_effect=RuntimeError("trace failure"),
        ):
            with state._timed_memory_lock("odds_read"):
                pass

        self.assertFalse(state.memory_lock.locked())

    def test_auto_refresh_is_deferred_while_club_read_is_active(self):
        state = self._state(verified=True)
        state.club_refreshing = True
        state.deferred_memory_job_after_club = None

        with patch("fm_odds_web.threading.Thread") as worker:
            started = state.refresh_async(
                "game_clock_stable", full=False,
                cancel_on_clock_change=True,
            )

        self.assertFalse(started)
        self.assertFalse(state.refreshing)
        self.assertEqual(state.deferred_memory_job_after_club, {
            "kind": "refresh", "reason": "game_clock_stable",
            "full": False, "cancel_on_clock_change": True,
        })
        worker.assert_not_called()

    def test_manual_refresh_does_not_queue_behind_club_read(self):
        state = self._state(verified=True)
        state.club_refreshing = True
        state.deferred_memory_job_after_club = None

        started = state.refresh_async("manual_fast", full=False)

        self.assertFalse(started)
        self.assertIsNone(state.deferred_memory_job_after_club)
        self.assertIn("俱乐部资料正在读取", state.status)

    def test_background_reconcile_is_deferred_while_club_read_is_active(self):
        state = self._state(verified=True)
        state.club_refreshing = True
        state.deferred_memory_job_after_club = None

        with patch("fm_odds_web.threading.Thread") as worker:
            state._start_reconcile(cancel_on_clock_change=True)

        self.assertFalse(state.reconciling)
        self.assertEqual(state.deferred_memory_job_after_club, {
            "kind": "reconcile", "cancel_on_clock_change": True,
        })
        worker.assert_not_called()

    def test_club_read_cooperatively_cancels_auto_refresh_and_queues_one_retry(self):
        state = self._state(verified=True)
        state.output.update({
            "manager": {"id": 77},
            "managed_teams": [{"id": 42, "name": "Club"}],
        })
        state.club_refreshing = False
        state.club_refresh_pending = True
        state.club_loading_profiles = {}
        state.club_progress = 0
        state.club_progress_text = None
        state.club_error = None
        state.refreshing = True
        state.refresh_mode = "fast"
        state.refresh_reason = "game_clock_stable"
        state.refresh_cancel_event = threading.Event()
        state.refresh_cancel_on_clock_change = True
        state.refresh_cancel_reason = None
        state.deferred_memory_job_after_club = None

        with patch("fm_odds_web.threading.Thread") as worker:
            started = state.refresh_club_async()

        self.assertTrue(started)
        self.assertTrue(state.club_refreshing)
        self.assertFalse(state.club_refresh_pending)
        self.assertTrue(state.refresh_cancel_event.is_set())
        self.assertEqual(state.refresh_cancel_reason, "club_priority")
        self.assertEqual(state.deferred_memory_job_after_club["reason"], "game_clock_stable")
        worker.assert_called_once()

    def test_club_read_prioritizes_over_startup_odds_when_no_cancel_token_exists(self):
        state = self._state(verified=True)
        state.output.update({
            "manager": {"id": 77},
            "managed_teams": [{"id": 42, "name": "Club"}],
        })
        state.refreshing = True
        state.refresh_reason = "startup_odds"
        state.club_refreshing = False
        state.club_refresh_pending = False

        with patch("fm_odds_web.threading.Thread") as worker:
            started = state.refresh_club_async()

        self.assertFalse(started)
        self.assertTrue(state.club_refresh_pending)
        self.assertEqual(state.club_progress_text, "正在优先读取当前经理和执教队伍")
        worker.assert_not_called()

    def test_club_read_cancels_long_startup_odds_and_resumes_it_afterwards(self):
        state = self._state(verified=True)
        state.output.update({
            "manager": {"id": 77},
            "managed_teams": [{"id": 42, "name": "Club"}],
        })
        state.refreshing = True
        state.refresh_mode = "full"
        state.refresh_reason = "startup_odds"
        state.refresh_cancel_event = threading.Event()
        state.club_refreshing = False
        state.club_refresh_pending = False

        with patch("fm_odds_web.threading.Thread") as worker:
            started = state.refresh_club_async()

        self.assertFalse(started)
        self.assertTrue(state.club_refresh_pending)
        self.assertTrue(state.refresh_cancel_event.is_set())
        self.assertEqual(state.refresh_cancel_reason, "club_priority")
        self.assertEqual(state.deferred_memory_job_after_club, {
            "kind": "refresh", "reason": "startup_odds", "full": True,
            "cancel_on_clock_change": False,
        })
        worker.assert_not_called()

    def test_club_worker_starts_deferred_refresh_once_after_finishing(self):
        state = self._state(verified=True)
        state.club_refreshing = True
        state.club_loading_profiles = {}
        state.club_profiles = {}
        state.club_error = None
        state.club_progress_text = None
        state.deferred_memory_job_after_club = {
            "kind": "refresh", "reason": "schedule_change",
            "full": False, "cancel_on_clock_change": True,
        }
        state._club_memory_read_lock = MagicMock(
            side_effect=RuntimeError("stop before memory read"),
        )
        state.refresh_async = MagicMock(return_value=True)

        state._club_refresh_worker()

        self.assertFalse(state.club_refreshing)
        self.assertIsNone(state.deferred_memory_job_after_club)
        state.refresh_async.assert_called_once_with(
            "schedule_change", full=False, cancel_on_clock_change=True,
        )

    def test_optional_polling_memory_work_runs_only_while_idle(self):
        state = self._state(verified=True)
        state.memory_lock = threading.Lock()
        operation = MagicMock(side_effect=lambda: state.memory_lock.locked())

        self.assertTrue(state._run_memory_operation_if_idle(operation, False))
        operation.assert_called_once_with()
        self.assertFalse(state.memory_lock.locked())

        state.memory_lock.acquire()
        try:
            operation.reset_mock()
            self.assertEqual(
                state._run_memory_operation_if_idle(operation, "busy"),
                "busy",
            )
            operation.assert_not_called()
        finally:
            state.memory_lock.release()

    def test_busy_club_economy_poll_reuses_last_memory_snapshot(self):
        state = self._state(verified=True)
        state.club_contexts = {}
        state.refresh_mode = None
        state.last_club_funds_status = {}
        transfer_budget = {
            "available": True, "amount": 25_000_000, "team_id": 679,
        }
        club_balance = {
            "available": True, "amount": 80_000_000, "team_id": 679,
        }

        with (
            patch("fm_odds_web.public_economy", return_value={}),
            patch.object(
                state, "_transfer_budget_status", return_value=transfer_budget,
            ),
            patch.object(
                state, "_club_balance_status", return_value=club_balance,
            ),
        ):
            live = state._public_club_economy("2028-06-06")
            busy = state._public_club_economy(
                "2028-06-06", read_memory=False,
            )

        self.assertEqual(live["transfer_budget"], transfer_budget)
        self.assertEqual(live["club_balance"], club_balance)
        self.assertEqual(busy["transfer_budget"], transfer_budget)
        self.assertEqual(busy["club_balance"], club_balance)

    def test_failed_club_economy_refresh_keeps_same_team_confirmed_budget(self):
        state = self._state(verified=True)
        state.club_contexts = {}
        state.refresh_mode = None
        state.last_club_funds_status = {
            "transfer_budget": {
                "available": True, "amount": 25_000_000, "team_id": 679,
                "team_name": "Manchester City",
            },
            "club_balance": {
                "available": True, "amount": 80_000_000, "team_id": 679,
            },
        }
        unavailable_budget = {
            "available": False, "amount": 0, "team_id": 679,
            "error": "无法定位转会预算",
        }
        unavailable_balance = {
            "available": False, "amount": 0, "team_id": 679,
            "error": "无法定位俱乐部结余",
        }

        with (
            patch("fm_odds_web.public_economy", return_value={}),
            patch.object(
                state, "_transfer_budget_status", return_value=unavailable_budget,
            ),
            patch.object(
                state, "_club_balance_status", return_value=unavailable_balance,
            ),
        ):
            economy = state._public_club_economy("2028-06-06")

        self.assertFalse(economy["transfer_budget"]["available"])
        self.assertEqual(
            state.last_club_funds_status["transfer_budget"]["amount"],
            25_000_000,
        )
        self.assertTrue(state.last_club_funds_status["transfer_budget"]["read_only"])
        self.assertEqual(
            state.last_club_funds_status["club_balance"]["amount"], 80_000_000,
        )

    def test_failed_club_economy_refresh_does_not_leak_previous_team_budget(self):
        state = self._state(verified=True)
        state.club_contexts = {}
        state.refresh_mode = None
        state.last_club_funds_status = {
            "transfer_budget": {
                "available": True, "amount": 25_000_000, "team_id": 679,
            },
        }
        unavailable = {
            "available": False, "amount": 0, "team_id": 680,
            "error": "无法定位转会预算",
        }

        with (
            patch("fm_odds_web.public_economy", return_value={}),
            patch.object(state, "_transfer_budget_status", return_value=unavailable),
            patch.object(
                state, "_club_balance_status",
                return_value={"available": False, "amount": 0, "team_id": 680},
            ),
        ):
            state._public_club_economy("2028-06-06")

        self.assertEqual(state.last_club_funds_status["transfer_budget"], unavailable)

    def test_foreground_memory_operation_records_wait_and_hold_time(self):
        state = SimpleNamespace(
            memory_lock=threading.Lock(), lock=threading.RLock(),
            last_operation_performance={},
        )

        with LocalOddsState._timed_user_memory_operation(state, "test_write"):
            pass

        trace = state.last_operation_performance
        self.assertEqual(trace["operation"], "test_write")
        self.assertTrue(trace["succeeded"])
        self.assertGreaterEqual(trace["memory_lock_wait_ms"], 0.0)
        self.assertGreaterEqual(trace["memory_lock_held_ms"], 0.0)
        self.assertGreaterEqual(trace["total_ms"], 0.0)

    def test_successful_budget_transfer_updates_busy_poll_snapshot(self):
        state = self._state(verified=True)
        state.memory_lock = threading.Lock()
        state.lock = threading.RLock()
        state.last_club_funds_status = {
            "transfer_budget": {"available": True, "amount": 25_000_000},
        }
        state._bind_current_save = MagicMock(return_value="account-1")
        state._managed_club_team = MagicMock(return_value={
            "id": 679, "name": "Manchester City", "address": "0x1234",
        })
        state._economy_patch_response = MagicMock(side_effect=lambda value: value)
        operation = SimpleNamespace(reader=object(), writable=True)

        with (
            patch(
                "fm_odds_web.borrow_game_operation",
                return_value=nullcontext(operation),
            ),
            patch("fm_odds_web.read_game_clock_from_reader", return_value={}),
            patch("fm_odds_web.read_transfer_budget", return_value={
                "amount": 25_000_000,
            }),
            patch("fm_odds_web.write_transfer_budget", return_value={
                "amount": 30_000_000,
            }),
            patch("fm_odds_web.public_economy", return_value={
                "bank_balance": 10_000_000,
            }),
            patch("fm_odds_web.adjust_bank_balance", return_value={}),
        ):
            state.transfer_club_budget({"direction": "in", "amount": 5_000_000})

        self.assertEqual(state.last_club_funds_status["transfer_budget"], {
            "available": True, "amount": 30_000_000,
            "team_id": 679, "team_name": "Manchester City",
        })

    def test_successful_club_balance_transfer_updates_busy_poll_snapshot(self):
        state = self._state(verified=True)
        state.memory_lock = threading.Lock()
        state.lock = threading.RLock()
        state.last_club_funds_status = {
            "club_balance": {"available": True, "amount": 80_000_000},
        }
        state._bind_current_save = MagicMock(return_value="account-1")
        state._managed_club_team = MagicMock(return_value={
            "id": 679, "name": "Manchester City", "address": "0x1234",
        })
        state._is_acquired_club = MagicMock(return_value=True)
        state._economy_patch_response = MagicMock(side_effect=lambda value: value)
        operation = SimpleNamespace(reader=object(), writable=True)

        with (
            patch(
                "fm_odds_web.borrow_game_operation",
                return_value=nullcontext(operation),
            ),
            patch("fm_odds_web.read_game_clock_from_reader", return_value={}),
            patch("fm_odds_web.read_club_balance", return_value={
                "amount": 80_000_000,
            }),
            patch("fm_odds_web.write_club_balance", return_value={
                "amount": 75_000_000,
            }),
            patch("fm_odds_web.public_economy", return_value={
                "bank_balance": 10_000_000,
            }),
            patch("fm_odds_web.adjust_bank_balance", return_value={}),
        ):
            state.transfer_club_balance({"direction": "out", "amount": 5_000_000})

        self.assertEqual(state.last_club_funds_status["club_balance"], {
            "available": True, "amount": 75_000_000,
            "team_id": 679, "team_name": "Manchester City",
            "owned_by_player": True,
        })

    def test_daily_fixture_effect_sync_skips_while_memory_is_busy(self):
        state = self._state(verified=True)
        state.output = {
            "save_instance_id": "save-123", "account_scope_id": "account-1",
            "game_date": "2028-06-06", "matches": [],
        }
        state.memory_lock = threading.Lock()
        state.fixture_item_sync_key = None
        state.fixture_item_sync_retry_key = None
        state.fixture_item_sync_retry_at = 0.0
        state.memory_lock.acquire()
        try:
            with (
                patch("fm_odds_web.monotonic", return_value=100.0),
                patch("fm_odds_web.reconcile_inventory") as reconcile,
                patch("fm_odds_web.sync_doping_effects") as doping,
                patch("fm_odds_web.sync_goalkeeper_bribes") as goalkeeper,
            ):
                state._sync_fixture_items_for_clock({
                    "date": "2028-06-06", "minutes": 0,
                })
        finally:
            state.memory_lock.release()

        reconcile.assert_not_called()
        doping.assert_not_called()
        goalkeeper.assert_not_called()

    def test_refresh_cleanup_survives_trace_finalization_failure(self):
        state = self._state(verified=True)
        state.memory_lock = threading.Lock()
        state.refreshing = True
        state.refresh_mode = "fast"
        state.refresh_reason = "manual_fast"
        state.refresh_cancel_on_clock_change = True
        state.club_refreshing = False
        cancel_event = threading.Event()
        cancel_event.set()
        state.refresh_cancel_event = cancel_event

        with (
            patch("fm_odds_web.set_active_save_id"),
            patch.object(state, "_settle_results_before_refresh", return_value={
                "settled": 0, "settled_records": [], "returned": 0.0,
            }),
            patch.object(
                state, "_finish_refresh_trace",
                side_effect=RuntimeError("trace failure"),
            ),
        ):
            state._refresh_worker("manual_fast", "fast", False, cancel_event)

        self.assertFalse(state.refreshing)
        self.assertIsNone(state.refresh_mode)
        self.assertIsNone(state.refresh_reason)
        self.assertIsNone(state.refresh_cancel_event)
        self.assertFalse(state.refresh_cancel_on_clock_change)
        self.assertIsNone(state.refresh_stage)

    def test_cancel_after_championship_does_not_read_club_or_persist(self):
        state = self._state(verified=True, data_version=7)
        state.memory_lock = threading.Lock()
        state.refreshing = True
        state.refresh_mode = "fast"
        state.refresh_reason = "manual_fast"
        state.refresh_cancel_on_clock_change = True
        state.club_refreshing = False
        cancel_event = threading.Event()
        state.refresh_cancel_event = cancel_event
        previous = {
            "save_instance_id": "save-123", "game_date": "2028-06-06",
            "odds_scope": "all", "odds_days": 14, "matches": [],
        }
        output = dict(previous)
        state.output = previous

        def cancel_after_championship(_markets):
            cancel_event.set()
            return {"settled": 1, "settled_records": [], "returned": 200.0}

        with (
            patch("fm_odds_web.set_active_save_id"),
            patch("fm_odds_web.remember_active_save_id"),
            patch.object(state, "_data_scope_id", return_value="account-1"),
            patch.object(state, "_assign_account_scope"),
            patch.object(state, "_settle_results_before_refresh", return_value={
                "settled": 0, "settled_records": [], "returned": 0.0,
            }),
            patch.object(state, "_finalize_settlement"),
            patch("fm_odds_web.load_settings", return_value={
                "odds_days": 14, "odds_scope": "all",
            }),
            patch("fm_odds_web.generate_all_odds", return_value=output),
            patch("fm_odds_web.prepare_early_market_prices"),
            patch("fm_odds_web.result_payload", return_value=[]),
            patch("fm_odds_web.settle_pending_bets", return_value={
                "settled": 0, "settled_records": [], "returned": 0.0,
            }),
            patch(
                "fm_odds_web.publish_championship_markets",
                return_value=({}, False),
            ),
            patch(
                "fm_odds_web.settle_championship_bets",
                side_effect=cancel_after_championship,
            ),
            patch("fm_odds_web.read_club_context") as read_club,
            patch("fm_odds_web.save_odds") as save_odds_mock,
        ):
            state._refresh_worker("manual_fast", "fast", False, cancel_event)

        self.assertIs(state.output, previous)
        self.assertEqual(state.data_version, 7)
        self.assertFalse(state.refreshing)
        self.assertEqual(
            state.status,
            "游戏时间变化，已停止刷新；刷新失败前已完成结算 1 笔",
        )
        self.assertEqual(state.last_refresh_partial_result, {
            "settled": 1,
            "returned": 200.0,
            "schedule_refunded": 0,
            "failed_stage": "championship_markets",
            "snapshot_published": False,
        })
        read_club.assert_not_called()
        save_odds_mock.assert_not_called()

    def test_refresh_trace_records_stages_memory_lock_and_completion(self):
        state = self._state(verified=True)
        state.memory_lock = threading.Lock()

        state._start_refresh_trace("manual_fast", "fast", False)
        state._set_refresh_stage("odds_read")
        with state._timed_memory_lock("odds_read"):
            pass
        state._finish_refresh_trace(True)

        trace = state.last_refresh_trace
        self.assertEqual(trace["reason"], "manual_fast")
        self.assertEqual(trace["mode"], "fast")
        self.assertFalse(trace["background"])
        self.assertTrue(trace["succeeded"])
        self.assertEqual(trace["error"], "")
        self.assertIn("odds_read", trace["stages_ms"])
        self.assertIn("odds_read", trace["memory_lock_wait_ms"])
        self.assertIn("odds_read", trace["memory_lock_held_ms"])
        self.assertGreaterEqual(trace["total_ms"], 0.0)

        snapshot = state._refresh_trace_snapshot(trace)
        self.assertEqual(snapshot, trace)
        self.assertIsNot(snapshot["stages_ms"], trace["stages_ms"])
        self.assertIsNot(
            snapshot["memory_lock_held_ms"], trace["memory_lock_held_ms"],
        )

    def test_refresh_status_exposes_live_backend_progress_and_elapsed_time(self):
        state = self._state(verified=False)
        state.refreshing = True
        state._start_refresh_trace("manual_fast", "fast", False)
        state._set_refresh_stage("generate_odds")
        state._publish_refresh_progress({
            "phase": "fixture_filter",
            "text": "正在筛选盘口日期窗口",
            "completed": 25,
            "total": 100,
        })

        status = state.refresh_status()

        self.assertEqual(status["refresh_stage"], "generate_odds")
        self.assertEqual(status["refresh_progress"]["completed"], 25)
        self.assertEqual(status["refresh_progress"]["total"], 100)
        self.assertIn("heartbeat_age_ms", status["refresh_progress"])
        self.assertIn("stage_elapsed_ms", status["refresh_progress"])

    def test_public_market_output_is_built_after_releasing_state_lock(self):
        class TrackingLock:
            def __init__(self):
                self.depth = 0

            def __enter__(self):
                self.depth += 1
                return self

            def __exit__(self, *_args):
                self.depth -= 1

        state = self._state(verified=False, data_version=0)
        state.lock = TrackingLock()
        state.output = {"matches": [{"fixture_date": "2028-06-06"}]}
        state.last_connection_clock = None
        state.last_process_selection_status = None
        state.connection_live_confirmed = False
        state.connection_scope_id = None
        state.wallet_ready = False
        state.connection_requested = False
        state.club_profiles = {}
        state.club_profile = None
        state.club_refreshing = False
        state.club_last_updated = None
        state.club_error = None
        state.club_progress = 0
        state.club_progress_text = ""
        state.redbull_hook = MagicMock(status=MagicMock(return_value={}))
        state.fixture_item_effect_status = {}
        state.referee_hook = MagicMock(status=MagicMock(return_value={}))
        state.live_settlement_status = {}
        state.last_background_settled = 0
        state.last_result_refresh_date = None
        state._note_connection_liveness = MagicMock(return_value=(None, False))

        def build_market(output, clock):
            self.assertEqual(state.lock.depth, 0)
            self.assertEqual(output, state.output)
            self.assertIsNot(output, state.output)
            self.assertIsNot(output["matches"][0], state.output["matches"][0])
            self.assertIsNone(clock)
            return {"matches": []}

        with (
            patch("fm_odds_web.process_selection_status", return_value={
                "available": [], "selected": None,
            }),
            patch("fm_odds_web.managed_team_rows", return_value=[]),
            patch("fm_odds_web.load_settings", return_value={}),
            patch("fm_odds_web.saved_account_scopes", return_value=[]),
            patch("fm_odds_web.market_output", side_effect=build_market),
            patch.object(state, "_nuclear_status", return_value={}),
        ):
            payload = state.public_state()

        self.assertEqual(payload["output"], {
            "matches": [], "season_result_count": 0,
        })
        self.assertEqual(state.lock.depth, 0)
        self.assertIn("locked_ms", state.last_public_state_metrics)
        self.assertIn("market_output_ms", state.last_public_state_metrics)
        self.assertIn("total_ms", state.last_public_state_metrics)

    def test_public_state_reuses_short_lived_snapshot(self):
        from tools.state_publication import PublicationCandidate

        state = LocalOddsState.__new__(LocalOddsState)
        state.data_version = 8
        state._capture_public_state = MagicMock(return_value=PublicationCandidate(
            state._public_state_context_key(), {"data_version": 8},
        ))

        first = state.public_state()
        second = state.public_state()

        self.assertIs(first, second)
        state._capture_public_state.assert_called_once_with()

    def test_public_state_snapshot_does_not_run_live_memory_maintenance(self):
        source = inspect.getsource(LocalOddsState._capture_public_state)

        self.assertNotIn("self._sync_fixture_items_for_clock", source)
        self.assertNotIn("self._sync_training_focuses", source)
        self.assertNotIn("self._sync_club_policies", source)
        self.assertNotIn("self._enforce_referee_integrity_notices", source)
        self.assertIn("read_memory=False", source)
        self.assertNotIn("self._request_public_state_maintenance", source)

    def test_historical_results_are_deferred_from_public_state(self):
        state = self._state(verified=False, data_version=7)
        state.output = {
            "season_results": [{
                "date": "2028-06-06",
                "home": {"id": 1, "name": "Home"},
                "away": {"id": 2, "name": "Away"},
            }],
        }
        state._has_verified_save = MagicMock(return_value=True)
        state._data_scope_id = MagicMock(return_value="save-1")

        results = state.public_results()

        self.assertEqual(results["data_version"], 7)
        self.assertEqual(results["data_scope_id"], "save-1")
        self.assertEqual(results["season_results"], state.output["season_results"])
        self.assertIsNot(results["season_results"], state.output["season_results"])
        self.assertIsNot(
            results["season_results"][0]["home"],
            state.output["season_results"][0]["home"],
        )

    def test_manual_fast_refresh_rescans_fixture_pool_without_reconcile(self):
        source = inspect.getsource(LocalOddsState._refresh_worker)
        force_scan_block = source.split("force_fixture_scan=reason in", 1)[1].split(
            "force_fixture_heap_scan", 1,
        )[0]
        reconcile_block = source.split("immediate_reconcile_reasons =", 1)[1].split(
            "should_reconcile =", 1,
        )[0]

        self.assertIn('"manual_fast"', force_scan_block)
        self.assertIn('"manual_standings"', force_scan_block)
        self.assertIn('"schedule_rollover"', force_scan_block)
        self.assertIn('"travel_complete"', force_scan_block)
        self.assertIn('"startup_odds"', force_scan_block)
        self.assertNotIn('"manual_fast"', reconcile_block)
        self.assertNotIn('"startup_odds"', reconcile_block)
        self.assertIn(
            'reason not in {"manager_change_schedule", "manual_fast"}',
            source,
        )
        self.assertIn(
            'reason not in {"manager_change_schedule", "manual_fast"}', source,
        )
        self.assertIn(
            "快速刷新结果与当前盘口不一致，已保留原盘口；请使用全量刷新",
            source,
        )

    def test_manual_standings_refresh_requests_wide_season_discovery(self):
        source = inspect.getsource(LocalOddsState._refresh_worker)

        self.assertIn(
            'discover_future_league_seasons = reason == "manual_standings"',
            source,
        )
        self.assertEqual(source.count("discover_future_league_seasons=("), 2)

    def test_league_format_refresh_is_independent_of_outward_markets(self):
        source = inspect.getsource(preview_cup_odds.generate_all_odds)
        retained = source.split("retained_league_formats =", 1)[1].split(
            "competition_formats.extend(retained_league_formats)", 1,
        )[0]
        result_formats = source.split("result_competition_formats =", 1)[1].split(
            "competition_formats.extend(result_competition_formats)", 1,
        )[0]

        self.assertNotIn("championship_enabled", retained)
        self.assertNotIn("championship_enabled", result_formats)
        self.assertIn(
            "fixture_discovery_days = max(days, CHAMPIONSHIP_DISCOVERY_DAYS)",
            source,
        )
        self.assertIn("or discover_future_league_seasons", source)

    def test_background_reconcile_does_not_force_heap_fixture_scan(self):
        source = inspect.getsource(LocalOddsState._refresh_worker)
        heap_scan_expression = source.split(
            "force_fixture_heap_scan=", 1,
        )[1].split(",\n", 1)[0]

        self.assertEqual(heap_scan_expression, "False")

    def test_background_pool_reconcile_escalates_unsafe_snapshots(self):
        previous = {
            **_snapshot(),
            "full_verified_at": "2028-06-06T12:00:00",
            "matches": [
                {"fixture_date": "2028-06-06", "id": index}
                for index in range(100)
            ],
        }
        complete = {**previous, "fixture_scan_mode": "pool"}
        partial = {**complete, "matches": complete["matches"][:60]}
        empty = {**complete, "matches": []}
        crossed = {**complete, "save_instance_id": "save-other"}

        self.assertFalse(
            background_pool_reconcile_requires_heap(previous, complete)
        )
        self.assertTrue(
            background_pool_reconcile_requires_heap(previous, partial)
        )
        self.assertTrue(
            background_pool_reconcile_requires_heap(previous, empty)
        )
        self.assertTrue(
            background_pool_reconcile_requires_heap(previous, crossed)
        )

    def test_save_change_full_refresh_uses_validated_pool_before_heap_fallback(self):
        source = inspect.getsource(LocalOddsState._refresh_worker)
        heap_scan_expression = source.split(
            "force_fixture_heap_scan=", 1,
        )[1].split(",\n", 1)[0]

        self.assertEqual(heap_scan_expression, "False")

    def test_save_change_refresh_invalidates_runtime_and_previous_market_caches(self):
        source = inspect.getsource(LocalOddsState._refresh_worker)
        runtime_reset = source.split("deferred_club_refresh = False", 1)[1].split(
            "self._start_refresh_trace", 1,
        )[0]
        cross_save_reset = source.split(
            "needs_cross_save_full = fast_refresh_crossed_save", 1,
        )[1].split("needs_partial_recovery", 1)[0]

        self.assertIn('"identity_change_full"', runtime_reset)
        self.assertIn('"save_change_full"', runtime_reset)
        self.assertIn("self._invalidate_rebuildable_runtime_caches()", runtime_reset)
        self.assertIn("previous_market_output = {}", cross_save_reset)

    def test_runtime_cache_reset_clears_all_owned_club_hints(self):
        state = LocalOddsState.__new__(LocalOddsState)
        state.lock = threading.RLock()
        state.club_manager_record_cache_key = ("scope-a", "2026-08-08")
        state.club_manager_record_cache = {1: {"matches": 1}}
        state.world_club_cache = {"clubs": [{"id": 1}]}
        state.world_club_cache_save_id = "career-a"
        state.owned_world_club_addresses = {1: "0x1234"}
        state.owned_world_club_address_save_id = "career-a"
        state.acquired_status_enforced = {("scope-a", 1, "0x1234")}

        with (
            patch("fm_odds_web.invalidate_runtime_cache") as odds,
            patch("fm_odds_web.invalidate_club_profile_cache") as clubs,
            patch("fm_odds_web.invalidate_head_coach_search_cache") as coaches,
            patch("fm_odds_web.invalidate_world_club_runtime_cache") as world,
        ):
            state._invalidate_rebuildable_runtime_caches()

        odds.assert_called_once_with()
        clubs.assert_called_once_with()
        coaches.assert_called_once_with()
        world.assert_called_once_with()
        self.assertIsNone(state.world_club_cache)
        self.assertEqual(state.owned_world_club_addresses, {})
        self.assertEqual(state.acquired_status_enforced, set())

    def test_clear_cache_protects_pending_snapshots_before_rebuild(self):
        state = LocalOddsState.__new__(LocalOddsState)
        state.lock = threading.RLock()
        state.memory_lock = threading.RLock()
        state.output = {
            "save_instance_id": "career-a",
            "account_scope_id": "scope-a",
        }
        state.connection_scope_id = "scope-a"
        state.refreshing = False
        state.reconciling = False
        state.club_refreshing = False
        state.world_club_scanning = False
        state.refresh_mode = None
        state.data_version = 1
        state._has_verified_save = lambda: True
        state._invalidate_rebuildable_runtime_caches = MagicMock()
        state.refresh_async = MagicMock(return_value=True)
        protected = {Path("U:/cache/pending.json")}

        with (
            patch("fm_odds_web.set_active_save_id") as bind,
            patch("fm_odds_web.pending_snapshot_paths", return_value=protected) as pending,
            patch("fm_odds_web.clear_cache_files", return_value={
                "cleared_bytes": 10, "cleared_files": 1,
            }) as clear,
        ):
            result = state.clear_cache("CLEAR_REBUILDABLE_CACHE")

        bind.assert_called_once_with("scope-a")
        pending.assert_called_once_with()
        clear.assert_called_once_with(protected_paths=protected)
        state._invalidate_rebuildable_runtime_caches.assert_called_once_with()
        state.refresh_async.assert_called_once_with("cache_cleared", full=True)
        self.assertTrue(result["refresh_started"])
        self.assertEqual(state.output["save_instance_id"], "career-a")
        self.assertEqual(state.output["account_scope_id"], "scope-a")

    def test_manual_refresh_without_save_identity_does_not_start_worker(self):
        state = LocalOddsState.__new__(LocalOddsState)
        state.lock = threading.RLock()
        state.output = {}
        state.refreshing = False
        state.reconciling = False
        state.club_refreshing = False

        with patch("fm_odds_web.threading.Thread") as worker:
            started = state.refresh_async("manual_fast", full=False)

        self.assertFalse(started)
        self.assertFalse(state.refreshing)
        self.assertIn("请先连接", state.status)
        worker.assert_not_called()

    def test_cache_clear_refresh_discards_market_snapshot_but_keeps_identity(self):
        source = inspect.getsource(LocalOddsState._refresh_worker)
        market_scope = source.split("previous_market_output = (", 1)[1].split(
            "if previous_output.get", 1,
        )[0]

        self.assertIn('reason not in {"cache_cleared", "storage_cleared"}', market_scope)
        self.assertIn("previous_output", market_scope)

        bind_position = source.index(
            "set_active_save_id(self._data_scope_id(previous_output))",
        )
        preferences_position = source.index("load_hidden_competition_ids()")
        self.assertLess(bind_position, preferences_position)

    def test_old_model_snapshot_keeps_manager_identity_hints(self):
        source = inspect.getsource(LocalOddsState._refresh_worker)
        first_refresh_call = source.split("output = generate_all_odds(", 1)[1].split(
            "needs_cross_save_full =", 1,
        )[0]

        self.assertIn("previous_snapshot=previous_market_output or None", first_refresh_call)
        self.assertIn(
            'previous_output.get("manager_options")',
            first_refresh_call,
        )
        self.assertNotIn(
            'previous_market_output.get("manager_options")',
            first_refresh_call,
        )
        self.assertIn("connection_context_verified=bool(", first_refresh_call)

    def test_verified_connection_context_skips_duplicate_manager_session_scan(self):
        source = inspect.getsource(preview_cup_odds.generate_all_odds)
        reuse_block = source.split("reuse_confirmed_manager_context = bool(", 1)[1].split(
            "phase_started = perf_counter()", 1,
        )[0]
        discovery_block = source.split("telemetry_session = None", 1)[1].split(
            "timings: dict[str, float]", 1,
        )[0]

        self.assertIn("connection_context_verified", reuse_block)
        self.assertIn("if not reuse_confirmed_manager_context", discovery_block)
        self.assertIn("discover_manager_session(", discovery_block)

    def test_verified_fm24_connection_skips_duplicate_save_provider_scan(self):
        source = inspect.getsource(preview_cup_odds.generate_all_odds)
        identity_block = source.split('"正在确认当前存档身份"', 1)[1].split(
            "save_context_key =", 1,
        )[0]

        self.assertIn("connection_context_verified and preferred_save_id", identity_block)
        self.assertIn("confirmed_save_name(preferred_save_id)", identity_block)
        self.assertIn("else:", identity_block)
        self.assertIn("fm24_save_evidence(", identity_block)

    def test_refresh_progress_leaves_completed_manager_step_before_result_work(self):
        source = inspect.getsource(preview_cup_odds.generate_all_odds)

        manager_done = source.index('"当前经理和执教队伍校验完成"')
        save_identity = source.index('"正在确认当前存档身份"')
        managed_teams = source.index('"正在读取当前执教队伍资料"')
        result_archive = source.index('"正在读取历史赛果"')

        self.assertLess(manager_done, save_identity)
        self.assertLess(save_identity, managed_teams)
        self.assertLess(managed_teams, result_archive)
        self.assertIn("fixture_snapshots_override=fixture_snapshots", source)

    def test_odds_model_preloads_unique_teams_and_caches_competition_reputation(self):
        source = inspect.getsource(preview_cup_odds.generate_all_odds)
        precompute = source.split("fixture_team_inputs:", 1)[1].split(
            "model_total =", 1,
        )[0]
        model_loop = source.split("model_total =", 1)[1].split(
            "tie_leg_xg:", 1,
        )[0]

        self.assertIn("fixture_team_inputs.setdefault", precompute)
        self.assertIn("pending_team_inputs", precompute)
        self.assertIn("cached_team_profile(", precompute)
        self.assertIn("competition_reputations", model_loop)
        self.assertIn("if season_address not in competition_reputations", model_loop)
        self.assertEqual(model_loop.count("native_competition_reputation("), 1)

    def test_disabled_championship_skips_extra_team_profiles_and_market_work(self):
        generator_source = inspect.getsource(preview_cup_odds.generate_all_odds)
        refresh_source = inspect.getsource(LocalOddsState._refresh_worker)
        result_source = inspect.getsource(LocalOddsState._result_refresh_worker)

        profile_block = generator_source.rsplit(
            "if championship_enabled:", 1,
        )[1].split("fixture_team_inputs:", 1)[0]
        self.assertIn("championship_profiles", profile_block)
        self.assertIn("enrich_competition_format_team_profiles", profile_block)
        self.assertEqual(
            refresh_source.count("championship_enabled=championship_enabled"), 2,
        )
        self.assertIn("if championship_enabled:", refresh_source)
        self.assertIn("if not championship_enabled:", result_source)

    def test_disabled_championship_rejects_bets_before_payload_validation(self):
        state = LocalOddsState.__new__(LocalOddsState)

        with patch(
            "fm_odds_web.load_settings",
            return_value={"championship_enabled": False},
        ):
            with self.assertRaisesRegex(ValueError, "冠军盘已在设置中关闭"):
                state.place_championship_bet({})

    def test_validated_pool_snapshot_can_connect_before_background_reconcile(self):
        snapshot = {**_snapshot(), "fixture_scan_mode": "pool"}

        valid, reason = startup_cache_validation(
            snapshot, "save-123", {"date": "2028-06-06"},
            {"odds_days": 7, "odds_scope": "famous"},
        )

        self.assertTrue(valid)
        self.assertEqual(reason, "valid")

    def test_referee_debt_handler_has_no_unreachable_legacy_penalty(self):
        source = inspect.getsource(LocalOddsState._enforce_referee_debts)
        self.assertNotIn("apply_team_reputation_delta", source)
        self.assertNotIn("add_points_adjustment", source)

    def test_light_result_refresh_publishes_and_settles_championships(self):
        state = LocalOddsState.__new__(LocalOddsState)
        state.lock = threading.RLock()
        state.memory_lock = threading.Lock()
        state.output = {
            "save_instance_id": "save-123",
            "account_scope_id": "account-1",
            "account_scope_manager_id": 1,
            "selected_manager_id": 1,
            "game_date": "2028-06-05",
            "matches": [],
        }
        state.output_path = None
        state.club_contexts = {}
        state.cache_verified = True
        state.data_version = 4
        state.last_game_date = "2028-06-05"
        state.last_result_refresh_date = "2028-06-05"
        state.salary_last_processed_date = "2028-06-05"
        state.last_background_settled = 0
        state.status = "ready"
        state.error = None
        state.refreshing = True
        state.refresh_mode = "results"
        state.refresh_reason = "test"
        cancel_event = threading.Event()
        state.refresh_cancel_event = cancel_event
        state.refresh_cancel_on_clock_change = False
        previous_output = state.output
        updated = {**state.output}
        markets = {"competitions": [{"status": "complete"}]}
        final_key = ("2028-06-05", 1301385, 10, 20)
        final_result = {
            "date": final_key[0],
            "competition": {"id": final_key[1], "name": "World Cup"},
            "home_team": {"id": final_key[2], "name": "Home"},
            "away_team": {"id": final_key[3], "name": "Away"},
            "home_goals": 1, "away_goals": 2,
        }
        championship_record = {
            "bet_id": "champ-1", "type": "championship", "status": "won",
        }

        def publish(current, previous):
            self.assertEqual(previous, state.output)
            self.assertIsNot(previous, state.output)
            current["championship_markets"] = markets
            return markets, False

        with (
            patch.object(state, "_due_existing_result_keys", return_value=set()),
            patch.object(state, "_merge_light_results", return_value=(updated, 1)),
            patch.object(state, "_finalize_settlement"),
            patch("fm_odds_web.read_game_clock", return_value={"date": "2028-06-06", "minutes": 0}),
            patch("fm_odds_web.pending_due_result_keys", return_value=set()),
            patch("fm_odds_web.settled_missing_half_score_keys", return_value=set()),
            patch(
                "fm_odds_web.pending_cup_final_result_keys",
                return_value={final_key},
            ),
            patch("fm_odds_web.pending_result_fixture_hints", return_value={}),
            patch("fm_odds_web.probe_live_completed_results", return_value={"results": []}),
            patch(
                "fm_odds_web.read_completed_results", return_value=[final_result],
            ) as recover_mock,
            patch("fm_odds_web.settle_pending_bets", return_value={
                "settled": 0, "returned": 0.0, "settled_records": [],
            }),
            patch("fm_odds_web.load_settings", return_value={"championship_enabled": True}),
            patch("fm_odds_web.publish_championship_markets", side_effect=publish) as publish_mock,
            patch("fm_odds_web.settle_championship_bets", return_value={
                "settled": 1, "returned": 400.0,
                "settled_records": [championship_record],
            }) as settle_mock,
            patch("fm_odds_web.archive_winning_mail") as archive_mock,
            patch("fm_odds_web.attach_historical_market_snapshots") as attach_snapshots,
            patch("fm_odds_web.save_odds"),
            patch("fm_odds_web.cache_data_root", return_value=Path("cache")),
            patch("fm_odds_web.set_active_save_id"),
            patch("fm_odds_web.sleep"),
        ):
            state._result_refresh_worker(cancel_event, "test")

        publish_mock.assert_called_once()
        recover_mock.assert_called_once_with({final_key}, fixture_hints={})
        settle_mock.assert_called_once_with(markets)
        archive_mock.assert_called_once_with([championship_record])
        self.assertIs(state.output, updated, state.error)
        self.assertEqual(state.output["championship_markets"], markets)
        self.assertEqual(state.last_background_settled, 1)
        self.assertEqual(state.data_version, 5)
        attach_snapshots.assert_called_once_with(updated, previous_output)

    def test_light_result_refresh_reads_recent_settled_halftime_backfill_keys(self):
        state = LocalOddsState.__new__(LocalOddsState)
        state.lock = threading.RLock()
        state.memory_lock = threading.Lock()
        state.output = {
            "save_instance_id": "save-123",
            "account_scope_id": "account-1",
            "game_date": "2028-06-07",
            "matches": [],
        }
        state.output_path = None
        state.club_contexts = {}
        state.cache_verified = True
        state.data_version = 4
        state.last_background_settled = 0
        state.refreshing = True
        state.refresh_mode = "results"
        state.refresh_reason = "test"
        state.refresh_cancel_event = threading.Event()
        state.refresh_cancel_on_clock_change = False
        state.refresh_cancel_reason = None
        state.status = "ready"
        state.error = None
        key = ("2028-06-06", 100, 10, 20)
        result = {
            "date": key[0],
            "competition": {"id": key[1], "name": "Competition"},
            "home_team": {"id": key[2], "name": "Home"},
            "away_team": {"id": key[3], "name": "Away"},
            "home_goals": 5,
            "away_goals": 1,
            "half_home_goals": 1,
            "half_away_goals": 1,
            "first_scoring_team": "away",
        }
        settlement = {
            "settled": 0, "details_updated": 1,
            "settled_records": [], "returned": 0.0,
        }
        cancel_event = state.refresh_cancel_event

        with (
            patch.object(state, "_due_existing_result_keys", return_value=set()),
            patch.object(state, "_merge_light_results", return_value=(state.output, 0)),
            patch.object(state, "_finalize_settlement"),
            patch("fm_odds_web.read_game_clock", return_value={
                "date": "2028-06-07", "minutes": 900,
            }),
            patch("fm_odds_web.pending_due_result_keys", return_value=set()),
            patch("fm_odds_web.settled_missing_half_score_keys", return_value={key}),
            patch(
                "fm_odds_web.pending_result_fixture_hints",
                return_value={key: 0x1234},
            ),
            patch("fm_odds_web.read_match_engine_state", return_value={
                "known": True, "active": False,
            }),
            patch("fm_odds_web.probe_live_completed_results", return_value={
                "results": [result],
            }) as probe,
            patch("fm_odds_web.settle_pending_bets", return_value=settlement) as settle,
            patch("fm_odds_web.load_settings", return_value={"championship_enabled": False}),
            patch("fm_odds_web.process_salary_payments", return_value={
                "paid": 0, "amount": 0.0,
            }),
            patch("fm_odds_web.set_active_save_id"),
            patch("fm_odds_web.sleep"),
        ):
            state._result_refresh_worker(cancel_event, "test")

        self.assertEqual(probe.call_count, 2)
        probe.assert_any_call({key}, fixture_hints={key: 0x1234})
        settle.assert_called_once_with(
            [result], "2028-06-07", game_minutes=900,
            trusted_same_day_keys={key},
            void_missing_after_timeout=False,
        )

    def test_public_match_output_does_not_duplicate_championship_snapshot(self):
        output = {
            "game_date": "2028-06-06",
            "matches": [],
            "championship_markets": {"competitions": [{"competition_id": 77}]},
            "competition_formats": [{"competition_id": 77}],
            "model_cache": {"profiles": [{"team_id": 10}]},
            "performance": {"total_ms": 100.0},
            "settlement_results": [{"competition_id": 77}],
            "season_results": [{"competition_id": 77}],
        }

        public = market_output(output, {"date": "2028-06-06", "minutes": 0})

        self.assertNotIn("championship_markets", public)
        for field in (
            "competition_formats", "model_cache", "performance",
            "settlement_results",
        ):
            self.assertNotIn(field, public)
        self.assertIn("season_results", public)
        self.assertIn("championship_markets", output)

    def test_championship_discovery_uses_two_week_lookahead(self):
        self.assertEqual(CHAMPIONSHIP_DISCOVERY_DAYS, 14)

    def test_discovered_competition_format_persists_in_same_save(self):
        previous_format = {
            "competition_id": 77,
            "competition_name": "Stored Cup",
            "competition_season_address": "0x1000",
            "actual_competition_address": "0x2000",
            "opening_teams": [{"id": 1}, {"id": 2}],
            "format_discovered_at": "2028-06-01",
            "format_last_observed_at": "2028-06-01",
        }
        previous = {
            "save_instance_id": "save-123",
            "competition_formats": [previous_format],
        }

        retained = persistent_competition_format_snapshots(
            [], previous, "save-123", "2028-07-01",
        )

        self.assertEqual(retained, [previous_format])

    def test_new_competition_season_replaces_stored_season(self):
        previous = {
            "save_instance_id": "save-123",
            "competition_formats": [{
                "competition_id": 77,
                "competition_season_address": "0x1000",
                "actual_competition_address": "0x2000",
                "opening_teams": [{"id": 1}, {"id": 2}],
                "format_discovered_at": "2028-06-01",
            }],
        }
        current = [{
            "competition_id": 77,
            "competition_season_address": "0x3000",
            "actual_competition_address": "0x4000",
            "opening_teams": [{"id": 3}, {"id": 4}],
        }]

        merged = persistent_competition_format_snapshots(
            current, previous, "save-123", "2029-06-01",
        )

        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["competition_season_address"], "0x3000")
        self.assertEqual(merged[0]["format_discovered_at"], "2029-06-01")

    def test_current_format_wins_over_far_future_same_competition(self):
        current = {
            "competition_id": 77,
            "competition_season_address": "0x1000",
            "first_fixture_date": "2027-08-01",
            "last_fixture_date": "2028-05-20",
            "opening_field_complete": True,
        }
        future = {
            "competition_id": 77,
            "competition_season_address": "0x2000",
            "first_fixture_date": "2028-08-01",
            "last_fixture_date": "2029-05-20",
            "opening_field_complete": True,
        }

        selected = select_current_competition_format_snapshots(
            [future, current], "2028-01-15",
        )

        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]["competition_season_address"], "0x1000")

    def test_next_format_takes_over_after_current_season_ends(self):
        selected = select_current_competition_format_snapshots([
            {
                "competition_id": 77,
                "competition_season_address": "0x1000",
                "first_fixture_date": "2027-08-01",
                "last_fixture_date": "2028-05-20",
            },
            {
                "competition_id": 77,
                "competition_season_address": "0x2000",
                "first_fixture_date": "2028-08-01",
                "last_fixture_date": "2029-05-20",
            },
        ], "2028-06-01")

        self.assertEqual(selected[0]["competition_season_address"], "0x2000")

    def test_competition_formats_do_not_cross_save_boundaries(self):
        previous = {
            "save_instance_id": "save-123",
            "competition_formats": [{"competition_id": 77}],
        }

        merged = persistent_competition_format_snapshots(
            [], previous, "save-456", "2028-07-01",
        )

        self.assertEqual(merged, [])

    def test_economy_patch_keeps_response_fields_and_adds_scope_guard(self):
        state = self._state(verified=True, data_version=7)
        state.output.update({
            "selected_manager_id": 42,
            "account_scope_manager_id": 42,
            "account_scope_id": "account-42",
        })

        response = state._economy_patch_response({
            "casino_balance": 125.0,
            "bank_balance": 500.0,
            "inventory": [],
            "payment": {"total": 10.0},
        }, transient=("payment",))

        self.assertEqual(response["payment"], {"total": 10.0})
        self.assertEqual(response["state_patch"]["balance"], 125.0)
        self.assertEqual(response["state_patch"]["economy"], {
            "casino_balance": 125.0,
            "bank_balance": 500.0,
            "inventory": [],
        })
        self.assertEqual(response["state_sync"], {
            "data_version": 7,
            "data_scope_id": "account-42",
        })

    def test_mutation_patch_reuses_returned_state_without_a_full_state_build(self):
        state = self._state(verified=True, data_version=7)
        state.output.update({
            "selected_manager_id": 42,
            "account_scope_manager_id": 42,
            "account_scope_id": "account-42",
        })
        economy = {
            "casino_balance": 125.0,
            "bank_balance": 500.0,
            "inventory": [],
        }
        standings = {"competitions": []}

        response = state._mutation_patch_response({
            "economy": economy,
            "league_standings": standings,
            "message": "done",
        })

        self.assertEqual(response["state_patch"], {
            "economy": economy,
            "balance": 125.0,
            "league_standings": standings,
        })
        self.assertEqual(response["state_sync"], {
            "data_version": 7,
            "data_scope_id": "account-42",
        })
        self.assertEqual(response["message"], "done")

    def test_mutation_patch_recognizes_direct_public_economy_payload(self):
        state = self._state(verified=True, data_version=7)
        state.output.update({
            "selected_manager_id": 42,
            "account_scope_manager_id": 42,
            "account_scope_id": "account-42",
        })
        economy = {
            "casino_balance": 125.0,
            "bank_balance": 500.0,
            "inventory": [],
        }

        response = state._mutation_patch_response(economy)

        self.assertEqual(response["state_patch"]["economy"], economy)
        self.assertEqual(response["state_patch"]["balance"], 125.0)

    def test_latest_snapshot_restores_last_active_scope_before_lookup(self):
        with (
            patch("fm_odds_web.active_save_id", side_effect=[None, "scope-1"]),
            patch("fm_odds_web.restore_active_save_id", return_value="scope-1") as restore,
            patch("fm_odds_web.cache_data_root") as root,
        ):
            root.return_value.joinpath.return_value.glob.return_value = []
            self.assertIsNone(latest_snapshot())

        restore.assert_called_once_with()
        root.assert_called_once_with()
    @staticmethod
    def _state(*, verified: bool, data_version: int = 3) -> LocalOddsState:
        set_active_save_id("test-refresh-optimization")
        state = LocalOddsState.__new__(LocalOddsState)
        state.lock = threading.RLock()
        state.cache_verified = verified
        state.save_change_pending = False
        state.refreshing = False
        state.reconciling = False
        state.data_version = data_version
        state.output = {"save_instance_id": "save-123"}
        state.output_path = None
        state.refresh_mode = None
        state.refresh_reason = None
        state.status = "ready"
        state.error = None
        return state

    def test_matching_snapshot_is_safe_to_unlock(self):
        valid, reason = startup_cache_validation(
            _snapshot(), "save-123", {"date": "2028-06-06"},
            {"odds_days": 7, "odds_scope": "famous"},
        )

        self.assertTrue(valid)
        self.assertEqual(reason, "valid")

    def test_previous_model_snapshot_is_never_accepted_after_cache_fix(self):
        snapshot = {**_snapshot(), "model_version": "legacy-runtime-cache"}

        valid, reason = startup_cache_validation(
            snapshot, "save-123", {"date": "2028-06-06"},
            {"odds_days": 7, "odds_scope": "famous"},
        )

        self.assertFalse(valid)
        self.assertEqual(reason, "model_changed")

    def test_old_model_schedule_is_not_carried_into_new_snapshot(self):
        previous = {
            **_snapshot(),
            "model_version": "legacy-runtime-cache",
            "matches": [{"fixture_date": "2028-06-07"}],
        }
        current = {
            **_snapshot(),
            "model_version": MODEL_VERSION,
            "matches": [],
        }

        retained = preserve_previous_schedule_after_empty_read(previous, current)

        self.assertIs(retained, current)
        self.assertEqual(retained["matches"], [])

    def test_runtime_cache_resets_when_reader_session_generation_changes(self):
        preview_cup_odds.invalidate_runtime_cache()
        reader_one = SimpleNamespace(
            process=SimpleNamespace(pid=321),
            module_base=0x140000000,
            session_generation=10,
        )
        reader_two = SimpleNamespace(
            process=SimpleNamespace(pid=321),
            module_base=0x140000000,
            session_generation=11,
        )
        try:
            first = preview_cup_odds._runtime_state(reader_one)
            first["fixture_addresses"] = [0x5000]
            first_scope = first["scope"]
            second = preview_cup_odds._runtime_state(reader_two)

            self.assertEqual(first_scope, (321, 0x140000000, 10, None))
            self.assertEqual(second["scope"], (321, 0x140000000, 11, None))
            self.assertEqual(second["fixture_addresses"], [])
        finally:
            preview_cup_odds.invalidate_runtime_cache()

    def test_direct_manager_cache_is_not_reused_across_reader_generations(self):
        preview_cup_odds.invalidate_runtime_cache()
        reader_one = SimpleNamespace(
            process=SimpleNamespace(pid=654),
            module_base=0x140000000,
            session_generation=20,
        )
        reader_two = SimpleNamespace(
            process=SimpleNamespace(pid=654),
            module_base=0x140000000,
            session_generation=21,
        )
        old_session = {"manager_id": 1}
        new_session = {"manager_id": 2}
        try:
            with patch.object(
                preview_cup_odds, "_discover_human_managers_direct",
                side_effect=[[old_session], [new_session]],
            ) as discover:
                self.assertEqual(
                    preview_cup_odds._cached_direct_human_managers(reader_one),
                    [old_session],
                )
                self.assertEqual(
                    preview_cup_odds._cached_direct_human_managers(reader_two),
                    [new_session],
                )

            self.assertEqual(discover.call_count, 2)
        finally:
            preview_cup_odds.invalidate_runtime_cache()

    def test_runtime_cache_resets_when_confirmed_save_scope_changes(self):
        preview_cup_odds.invalidate_runtime_cache()
        reader = SimpleNamespace(
            process=SimpleNamespace(pid=777),
            module_base=0x140000000,
            session_generation=30,
        )
        try:
            with patch(
                "tools.preview_cup_odds.iter_readable_regions", return_value=[],
            ):
                preview_cup_odds.discover_manager_session(
                    reader, date(2028, 6, 6), stable_scope="save-a",
                )
                first = preview_cup_odds._runtime_state(reader)
                first["fixture_addresses"] = [0x7000]
                preview_cup_odds.discover_manager_session(
                    reader, date(2028, 6, 6), stable_scope="save-b",
                )
                second = preview_cup_odds._runtime_state(reader)

            self.assertEqual(second["scope"], (777, 0x140000000, 30, "save-b"))
            self.assertEqual(second["fixture_addresses"], [])
        finally:
            preview_cup_odds.invalidate_runtime_cache()

    def test_fm26_numeric_manager_without_team_is_not_a_valid_connection(self):
        snapshot = {
            **_snapshot(),
            "game_layout": "fm26",
            "selected_manager_id": 2995152,
            "manager": {"id": 2995152},
            "manager_options": [{"id": 2995152, "name": "经理 2995152"}],
            "managed_teams": [],
        }

        valid, reason = manager_team_context_status(snapshot)
        cache_valid, cache_reason = startup_cache_validation(
            snapshot, "save-123", {"date": "2028-06-06"},
            {"odds_days": 7, "odds_scope": "famous"},
        )

        self.assertFalse(valid)
        self.assertEqual(reason, "managed_team_missing")
        self.assertFalse(cache_valid)
        self.assertEqual(cache_reason, "managed_team_missing")

    def test_confirmed_unemployed_fm26_manager_is_a_valid_connection(self):
        snapshot = {
            **_snapshot(),
            "game_layout": "fm26",
            "selected_manager_id": 2995152,
            "manager": {"id": 2995152, "name": "Test Manager"},
            "manager_options": [{"id": 2995152, "name": "Test Manager"}],
            "managed_teams": [],
            "unemployed_manager_detected": True,
            "unemployed_confirmed": True,
        }

        self.assertEqual(
            manager_team_context_status(snapshot),
            (True, "unemployed_confirmed"),
        )
        self.assertEqual(
            startup_cache_validation(
                snapshot, "save-123", {"date": "2028-06-06"},
                {"odds_days": 7, "odds_scope": "famous"},
            ),
            (True, "valid"),
        )

    def test_confirmed_unemployed_fm24_manager_is_a_valid_connection(self):
        snapshot = {
            **_snapshot(),
            "game_layout": "fm24",
            "selected_manager_id": 2002097907,
            "manager": {"id": 2002097907, "name": "Test Manager"},
            "manager_options": [{"id": 2002097907, "name": "Test Manager"}],
            "managed_teams": [],
            "unemployed_manager_detected": True,
            "unemployed_confirmed": True,
        }

        self.assertEqual(
            manager_team_context_status(snapshot),
            (True, "unemployed_confirmed"),
        )
        self.assertEqual(
            startup_cache_validation(
                snapshot, "save-123", {"date": "2028-06-06"},
                {"odds_days": 7, "odds_scope": "famous"},
            ),
            (True, "valid"),
        )

    def test_fm24_unemployed_manager_requires_explicit_confirmation(self):
        state = self._state(verified=True)
        output = {
            "game_layout": "fm24",
            "save_instance_id": "save-123",
            "selected_manager_id": 2002097907,
            "manager": {"id": 2002097907, "name": "Test Manager"},
            "managed_teams": [],
            "unemployed_manager_detected": True,
        }

        state._apply_unemployed_confirmation_state(output)

        self.assertTrue(output["unemployed_confirmation_required"])
        self.assertFalse(output["unemployed_confirmed"])

    def test_unemployed_confirmation_without_authoritative_detection_is_rejected(self):
        snapshot = {
            **_snapshot(),
            "game_layout": "fm26",
            "selected_manager_id": 2995152,
            "manager": {"id": 2995152},
            "managed_teams": [],
            "unemployed_confirmed": True,
        }

        self.assertEqual(
            manager_team_context_status(snapshot),
            (False, "managed_team_missing"),
        )

    def test_confirm_unemployed_manager_starts_full_refresh(self):
        state = self._state(verified=True)
        state.output = {
            "game_layout": "fm26",
            "save_instance_id": "save-123",
            "selected_manager_id": 77,
            "manager": {"id": 77, "name": "Test Manager"},
            "managed_teams": [],
            "unemployed_manager_detected": True,
            "unemployed_confirmation_required": True,
        }
        state.refreshing = False
        state.reconciling = False

        with patch.object(state, "refresh_async", return_value=True) as refresh:
            result = state.confirm_unemployed_manager({"confirmed": True})

        refresh.assert_called_once_with("unemployed_manager_confirmed", full=True)
        self.assertTrue(result["started"])
        self.assertTrue(state.output["unemployed_confirmed"])
        self.assertFalse(state.output["unemployed_confirmation_required"])

    def test_confirm_unemployed_manager_during_refresh_does_not_start_another(self):
        state = self._state(verified=True)
        state.output = {
            "game_layout": "fm26",
            "save_instance_id": "save-123",
            "selected_manager_id": 77,
            "manager": {"id": 77, "name": "Test Manager"},
            "managed_teams": [],
            "unemployed_manager_detected": True,
            "unemployed_confirmation_required": True,
        }
        state.refreshing = True

        with patch.object(state, "refresh_async") as refresh:
            result = state.confirm_unemployed_manager({"confirmed": True})

        refresh.assert_not_called()
        self.assertFalse(result["started"])
        self.assertTrue(state.output["unemployed_confirmed"])
        self.assertFalse(state.output["unemployed_confirmation_required"])

    def test_betting_is_blocked_while_manager_resignation_is_pending(self):
        state = self._state(verified=True)
        state.output = {
            "game_layout": "fm24",
            "save_instance_id": "save-123",
            "selected_manager_id": 77,
            "manager": {"id": 77, "name": "Test Manager"},
            "managed_teams": [{"id": 10, "name": "Test Club", "team_type": "club"}],
        }
        state.manager_context_absence_pending = True

        self.assertFalse(state._betting_ready())
        with self.assertRaisesRegex(ValueError, "俱乐部任职变化"):
            state._assert_betting_ready()

    def test_initial_manager_selection_resumes_startup_connection(self):
        state = self._state(verified=True)
        state.connection_requested = True
        state.output = {
            "game_layout": "fm24",
            "save_instance_id": "save-123",
            "manager_selection_required": True,
            "manager_options": [{
                "id": 20, "name": "Manager Two",
                "managed_team_refs": [{"team_id": 200, "team_type": "club"}],
            }],
        }
        state._assign_account_scope = MagicMock(return_value="account-20")

        with (
            patch.object(state, "startup_async", return_value=True) as startup,
            patch.object(state, "connect_current_save") as reconnect,
            patch("fm_odds_web.remember_active_save_id"),
            patch("fm_odds_web.remember_selected_manager"),
        ):
            result = state.switch_manager({"manager_id": 20})

        startup.assert_called_once_with()
        reconnect.assert_not_called()
        self.assertTrue(result["started"])
        self.assertEqual(result["mode"], "startup_connection")
        self.assertFalse(state.output["manager_selection_required"])

    def test_initial_manager_selection_rebinds_after_service_restart(self):
        state = self._state(verified=True)
        state.connection_requested = False
        state.output = {
            "game_layout": "fm24",
            "save_instance_id": "save-123",
            "manager_selection_required": True,
            "manager_options": [{
                "id": 20, "name": "Manager Two",
                "managed_team_refs": [{"team_id": 200, "team_type": "club"}],
            }],
        }
        state._assign_account_scope = MagicMock(return_value="account-20")

        with (
            patch.object(
                state, "connect_current_save", return_value={"started": True},
            ) as reconnect,
            patch.object(state, "startup_async") as startup,
            patch("fm_odds_web.remember_active_save_id"),
            patch("fm_odds_web.remember_selected_manager"),
        ):
            result = state.switch_manager({"manager_id": 20})

        reconnect.assert_called_once_with()
        startup.assert_not_called()
        self.assertTrue(result["started"])
        self.assertEqual(result["mode"], "startup_connection")

    def test_unemployed_manager_can_be_selected_in_multi_manager_save(self):
        state = self._state(verified=True)
        state.output = {
            "game_layout": "fm26",
            "save_instance_id": "save-123",
            "selected_manager_id": 10,
            "manager": {"id": 10, "name": "Employed"},
            "managed_team": {"id": 100, "name": "Club", "team_type": "club"},
            "managed_teams": [{"id": 100, "name": "Club", "team_type": "club"}],
            "manager_options": [
                {"id": 10, "name": "Employed", "managed_team_refs": [{"team_id": 100}]},
                {"id": 20, "name": "Unemployed", "managed_team_refs": []},
            ],
        }
        state._assign_account_scope = MagicMock(return_value="account-20")
        state.club_context = None
        state.club_contexts = {}
        state.club_profile = None
        state.club_profiles = {}
        state.club_last_updated = None
        state.club_error = None

        with (
            patch("fm_odds_web.remember_active_save_id"),
            patch("fm_odds_web.remember_selected_manager"),
        ):
            result = state.switch_manager({"manager_id": 20})

        self.assertTrue(result["completed"])
        self.assertEqual(state.output["selected_manager_id"], 20)
        self.assertEqual(state.output["managed_teams"], [])
        self.assertTrue(state.output["unemployed_manager_detected"])
        self.assertTrue(state.output["unemployed_confirmation_required"])

    def test_explicit_empty_ca_hook_target_does_not_reuse_old_club(self):
        state = LocalOddsState.__new__(LocalOddsState)
        state.club_context = {
            "team": {"address": "0x1000"},
            "club": {"address": "0x2000"},
        }
        state.canteen_plan = "nutrition"
        state.ca_growth_hook = MagicMock()
        state.ca_growth_hook.sync.return_value = {"active": False}

        state._sync_ca_growth_hook(set(), team_address=None, club_address=None)

        state.ca_growth_hook.sync.assert_called_once_with(
            True, 0, 0, 1.5, national_team_address=0,
        )

    def test_explicit_empty_referee_context_does_not_reuse_old_club(self):
        state = LocalOddsState.__new__(LocalOddsState)
        state.club_context = {"team": {"id": 10, "address": "0x1000"}}
        state.club_contexts = {10: state.club_context}

        with patch("fm_odds_web.public_economy", return_value={
            "inventory": [{
                "sku": "referee",
                "status": "active",
                "managed_team_id": 10,
            }],
        }):
            context = state._active_referee_context({}, {"managed_teams": []})

        self.assertEqual(context, {})

    def test_fm26_numeric_manager_name_does_not_block_a_valid_team_context(self):
        snapshot = {
            **_snapshot(),
            "game_layout": "fm26",
            "selected_manager_id": 2995152,
            "manager": {"id": 2995152},
            "manager_options": [{"id": 2995152, "name": "经理 2995152"}],
            "managed_teams": [{"id": 10, "name": "Test Club"}],
        }

        self.assertEqual(
            manager_team_context_status(snapshot),
            (True, "valid"),
        )

    def test_fm26_named_manager_and_team_are_a_valid_connection(self):
        snapshot = {
            **_snapshot(),
            "game_layout": "fm26",
            "selected_manager_id": 77,
            "manager": {"id": 77},
            "manager_options": [{"id": 77, "name": "Test Manager"}],
            "managed_teams": [{"id": 10, "name": "Test Club"}],
        }

        self.assertEqual(manager_team_context_status(snapshot), (True, "valid"))

    def test_live_settlement_uses_fast_double_confirmation(self):
        self.assertEqual(LIVE_SETTLEMENT_POLL_SECONDS, 5)
        self.assertEqual(LIVE_SETTLEMENT_CONFIRMATIONS, 2)

    def test_missing_live_result_uses_persistent_fallback_after_retries(self):
        state = self._state(verified=True)
        state.memory_lock = threading.Lock()
        state.live_settlement_recovery_attempts = {}
        state.live_settlement_recovery_last_at = 0.0
        state.live_settlement_status = {
            "fallback_attempts": 0, "fallback_recovered": 0,
        }
        key = ("2028-06-06", 300, 10, 20)
        result = {
            "date": key[0],
            "competition": {"id": key[1]},
            "home_team": {"id": key[2]},
            "away_team": {"id": key[3]},
            "home_goals": 1,
            "away_goals": 0,
        }

        with (
            patch("fm_odds_web.monotonic", side_effect=[100.0, 105.0, 110.0]),
            patch("fm_odds_web.read_completed_results", return_value=[result]) as read,
        ):
            self.assertEqual(state._recover_missing_live_results({key}, set()), [])
            self.assertEqual(state._recover_missing_live_results({key}, set()), [])
            self.assertEqual(
                state._recover_missing_live_results({key}, set()), [result],
            )

        read.assert_called_once_with({key}, fixture_hints={})
        self.assertEqual(state.live_settlement_status["fallback_attempts"], 1)
        self.assertEqual(state.live_settlement_status["fallback_recovered"], 1)

    def test_persistent_fallback_accepts_unique_adjacent_result_date(self):
        state = self._state(verified=True)
        state.memory_lock = threading.Lock()
        state.live_settlement_recovery_attempts = {}
        state.live_settlement_recovery_last_at = 0.0
        state.live_settlement_status = {
            "fallback_attempts": 0, "fallback_recovered": 0,
        }
        key = ("2028-06-06", 300, 10, 20)
        result = {
            "date": "2028-06-07",
            "competition": {"id": key[1]},
            "home_team": {"id": key[2]},
            "away_team": {"id": key[3]},
            "home_goals": 1,
            "away_goals": 0,
        }

        with (
            patch("fm_odds_web.monotonic", side_effect=[100.0, 105.0, 110.0]),
            patch("fm_odds_web.read_completed_results", return_value=[result]),
        ):
            self.assertEqual(state._recover_missing_live_results({key}, set()), [])
            self.assertEqual(state._recover_missing_live_results({key}, set()), [])
            recovered = state._recover_missing_live_results({key}, set())

        self.assertEqual(recovered, [result])
        self.assertNotIn(key, state.live_settlement_recovery_attempts)
        self.assertEqual(state.live_settlement_status["fallback_recovered"], 1)

    def test_managed_team_fixture_survives_missing_competition_scope_cache(self):
        fixture = SimpleNamespace(match_date=datetime(2028, 6, 9).date())
        league_one = {"id": 300, "name": "英格兰甲级联赛", "kind": "league"}
        managed_match = (fixture, league_one, {"id": 10}, {"id": 20})
        unrelated_match = (fixture, league_one, {"id": 30}, {"id": 40})

        visible = preview_cup_odds.filter_fixture_rows_for_odds(
            [managed_match, unrelated_match], "famous", fixture.match_date,
            managed_competition_ids=set(), managed_team_ids={10},
        )

        self.assertEqual(visible, [managed_match])

    def test_famous_scope_keeps_all_matches_from_player_competition(self):
        fixture = SimpleNamespace(match_date=date(2028, 6, 6))
        domestic_cup = {"id": 300, "name": "本国杯赛", "kind": "cup"}
        managed_match = (fixture, domestic_cup, {"id": 10}, {"id": 20})
        other_match = (fixture, domestic_cup, {"id": 30}, {"id": 40})

        visible = preview_cup_odds.filter_fixture_rows_for_odds(
            [managed_match, other_match], "famous", fixture.match_date,
            managed_competition_ids={300}, managed_team_ids={10},
        )

        self.assertEqual(visible, [managed_match, other_match])

    def test_favorite_scope_keeps_all_matches_from_favorite_team_competitions(self):
        fixture = SimpleNamespace(match_date=date(2028, 6, 6))
        domestic_cup = {"id": 300, "name": "本国杯赛", "kind": "cup"}
        favorite_match = (fixture, domestic_cup, {"id": 10}, {"id": 20})
        other_match = (fixture, domestic_cup, {"id": 30}, {"id": 40})
        unrelated_match = (
            fixture, {"id": 400, "name": "其他联赛", "kind": "league"},
            {"id": 50}, {"id": 60},
        )

        visible = preview_cup_odds.filter_fixture_rows_for_odds(
            [favorite_match, other_match, unrelated_match],
            "favorite_schedule", fixture.match_date,
            managed_competition_ids={300}, managed_team_ids={10},
        )

        self.assertEqual(visible, [favorite_match, other_match])

    def test_favorite_scope_header_index_expands_only_valid_favorite_seasons(self):
        fixtures = [
            SimpleNamespace(
                match_date=date(2028, 8, 1), home_team=0x1000,
                away_team=0x2000, competition_season=0x3000,
            ),
            SimpleNamespace(
                match_date=date(2028, 8, 2), home_team=0x3000,
                away_team=0x4000, competition_season=0x3000,
            ),
            SimpleNamespace(
                match_date=date(2028, 8, 3), home_team=0x5000,
                away_team=0x6000, competition_season=0x7000,
            ),
            SimpleNamespace(
                match_date=date(2030, 8, 1), home_team=0x1000,
                away_team=0x8000, competition_season=0x9000,
            ),
        ]

        def uid_snapshot(uid: int) -> bytes:
            raw = bytearray(preview_cup_odds.ENTITY_UID + 4)
            struct.pack_into("<I", raw, preview_cup_odds.ENTITY_UID, uid)
            return bytes(raw)

        snapshots = {
            0x1000: uid_snapshot(10),
            0x2000: uid_snapshot(20),
            0x3000: uid_snapshot(30),
            0x4000: uid_snapshot(40),
            # Same UID in stale memory must not become trusted until reader.team
            # confirms that this is still the favorite club object.
            0x5000: uid_snapshot(10),
            0x6000: uid_snapshot(60),
        }
        reader = SimpleNamespace(
            _fixed_size_snapshots=MagicMock(return_value=snapshots),
            team=MagicMock(side_effect=lambda address: (
                {"id": 10, "team_type": "club"}
                if address == 0x1000 else {"id": 999, "team_type": "club"}
            )),
            competition=MagicMock(side_effect=lambda address: {
                0x3000: {"id": 300},
                0x7000: {"id": 700},
            }.get(address)),
        )
        progress = []

        seasons, competitions = (
            preview_cup_odds.favorite_schedule_scope_from_fixture_headers(
                reader, fixtures, {10}, "2028-07-01", "2029-06-30",
                progress_callback=lambda completed, total: progress.append(
                    (completed, total)
                ),
            )
        )

        self.assertEqual(seasons, {0x3000})
        self.assertEqual(competitions, {300})
        self.assertEqual(reader.team.call_count, 2)
        reader.competition.assert_called_once_with(0x3000)
        self.assertEqual(progress[-1], (3, 3))

    def test_favorite_scope_filters_headers_before_team_and_competition_expansion(self):
        source = inspect.getsource(preview_cup_odds.generate_all_odds)
        header_pass = source.split("parsed_fixtures = []", 1)[1].split(
            "favorite_scope_season_addresses", 1,
        )[0]
        scoped_pass = source.split("for fixture in parsed_fixtures:", 1)[1].split(
            "discovered_representative_seasons", 1,
        )[0]

        self.assertNotIn("reader.team(", header_pass)
        self.assertNotIn("reader.competition(", header_pass)
        self.assertLess(
            scoped_pass.index("season_address not in favorite_scope_season_addresses"),
            scoped_pass.index("append_fixture_row(fixture)"),
        )

    def test_hidden_competitions_resolve_to_seasons_before_fixture_expansion(self):
        fixtures = [
            SimpleNamespace(competition_season=0x3000),
            SimpleNamespace(competition_season=0x3000),
            SimpleNamespace(competition_season=0x4000),
        ]
        expected_vtable = 0x140000000 + 0x1234

        def competition_snapshot(competition_id: int) -> bytes:
            raw = bytearray(0x20)
            struct.pack_into("<Q", raw, 0, expected_vtable)
            struct.pack_into(
                "<I", raw, preview_cup_odds.ENTITY_UID, competition_id,
            )
            return bytes(raw)

        nested_season = bytearray(0x20)
        struct.pack_into("<Q", nested_season, 0x18, 0x5000)

        def snapshots(addresses, _size):
            payloads = {
                0x3000: competition_snapshot(300),
                0x4000: bytes(nested_season),
                0x5000: competition_snapshot(400),
            }
            return {address: payloads[address] for address in addresses}

        reader = SimpleNamespace(
            module_base=0x140000000,
            layout=SimpleNamespace(competition_vtable_rva=0x1234),
            _fixed_size_snapshots=MagicMock(side_effect=snapshots),
        )

        hidden = preview_cup_odds.hidden_competition_seasons_from_fixture_headers(
            reader, fixtures, {400},
        )

        self.assertEqual(hidden, {0x4000})
        self.assertEqual(reader._fixed_size_snapshots.call_count, 2)

    def test_hidden_season_filter_runs_before_deep_fixture_expansion(self):
        source = inspect.getsource(preview_cup_odds.generate_all_odds)
        scoped_pass = source.split("for fixture in parsed_fixtures:", 1)[1].split(
            "discovered_representative_seasons", 1,
        )[0]

        self.assertLess(
            scoped_pass.index("season_address in hidden_scope_season_addresses"),
            scoped_pass.index("append_fixture_row(fixture)"),
        )

    def test_favorite_team_full_season_fixture_discovers_its_cup(self):
        raw = bytearray(0x58)
        struct.pack_into(
            "<QQQQQ", raw, 0x08,
            0x1000, 0x2000, 0x4000, 0x3000, 0x6000,
        )
        target_date = date(2028, 9, 12)
        day_of_year = (target_date - date(2028, 1, 1)).days + 1
        struct.pack_into("<I", raw, 0x4C, (2028 << 16) | day_of_year)
        reader = SimpleNamespace(
            process=SimpleNamespace(pid=987654),
            module_base=0x140000000,
            layout=SimpleNamespace(
                fixture_stage_index_offset=None,
                fixture_group_index_offset=None,
                fixture_round_number_offset=None,
            ),
            u32=lambda address: {
                0x1000 + preview_cup_odds.ENTITY_UID: 10,
                0x2000 + preview_cup_odds.ENTITY_UID: 20,
            }.get(address, 0),
            team=lambda address: {
                "id": 10 if address == 0x1000 else 20,
            },
            competition=lambda _address: {"id": 300},
        )

        competition_ids, cache_hit = (
            preview_cup_odds.cached_managed_schedule_competitions(
                reader, [0x5000], [], [],
                [{"id": 10, "team_type": "club"}],
                "save-favorites", "2028-07-01", "2029-06-30",
                fixture_snapshots={0x5000: bytes(raw)},
            )
        )

        self.assertEqual(competition_ids, {300})
        self.assertFalse(cache_hit)

    def test_famous_scope_includes_national_continental_and_major_cups(self):
        self.assertTrue(preview_cup_odds.is_famous_competition({
            "id": 900, "name": "非洲杯", "kind": "national",
        }))
        self.assertTrue(preview_cup_odds.is_famous_competition({
            "id": 901, "name": "南美解放者杯", "kind": "cup",
        }))
        self.assertTrue(preview_cup_odds.is_famous_competition({
            "id": 1301426, "name": "英格兰足总杯", "kind": "cup",
        }))

    def test_fourteen_day_window_includes_same_weekday_two_weeks_later(self):
        output = {
            "odds_days": 14,
            "matches": [{
                "fixture_date": "2028-06-20", "kickoff_minutes": 1185,
                "competition_id": 300,
                "home": {"id": 10}, "away": {"id": 20},
            }],
        }

        snapshot = LocalOddsState._output_schedule_snapshot(output, "2028-06-06")

        self.assertEqual(snapshot["fixtures"], [
            ("2028-06-20", 300, 10, 20, 1185),
        ])

    def test_schedule_snapshot_keeps_fixture_at_exact_kickoff(self):
        output = {
            "odds_days": 7,
            "matches": [{
                "fixture_date": "2028-06-06", "kickoff_minutes": 900,
                "competition_id": 300,
                "home": {"id": 10}, "away": {"id": 20},
            }],
        }

        snapshot = LocalOddsState._output_schedule_snapshot(
            output, "2028-06-06", game_minutes=900,
        )

        self.assertEqual(snapshot["fixtures"], [
            ("2028-06-06", 300, 10, 20, 900),
        ])

    def test_changed_save_keeps_snapshot_read_only(self):
        valid, reason = startup_cache_validation(
            _snapshot(), "save-456", {"date": "2028-06-06"},
            {"odds_days": 7, "odds_scope": "famous"},
        )

        self.assertFalse(valid)
        self.assertEqual(reason, "save_changed")

    def test_fast_refresh_crossing_save_requires_full_read(self):
        previous = {"save_instance_id": "save-123"}
        current = {"save_instance_id": "save-456"}

        self.assertTrue(fast_refresh_crossed_save(previous, current))
        self.assertFalse(fast_refresh_crossed_save(previous, previous))

    def test_fast_refresh_rejects_large_drop_from_full_verified_schedule(self):
        previous = {
            "save_instance_id": "save-123",
            "game_date": "2028-06-06",
            "odds_scope": "all",
            "odds_days": 14,
            "full_verified_at": "2028-06-06T12:00:00",
            "matches": [
                {"fixture_date": "2028-06-07"} for _index in range(1100)
            ],
        }
        partial = {
            **previous,
            "full_verified_at": None,
            "matches": [
                {"fixture_date": "2028-06-07"} for _index in range(440)
            ],
        }

        self.assertTrue(
            suspicious_partial_schedule_refresh(previous, partial),
        )

    def test_refresh_rejects_one_missing_known_future_fixture(self):
        previous = {
            "save_instance_id": "save-123", "game_date": "2028-06-06",
            "game_time": "12:00", "odds_scope": "all", "odds_days": 14,
            "full_verified_at": "2028-06-06T12:00:00",
            "matches": [
                {
                    "fixture_date": "2028-06-07", "kickoff_minutes": 900,
                    "competition_id": 100,
                    "home": {"id": 10}, "away": {"id": 20},
                },
                {
                    "fixture_date": "2028-06-08", "kickoff_minutes": 900,
                    "competition_id": 100,
                    "home": {"id": 30}, "away": {"id": 40},
                },
            ],
        }
        current = {**previous, "matches": previous["matches"][:1]}

        self.assertTrue(
            suspicious_partial_schedule_refresh(previous, current)
        )

    def test_refresh_allows_missing_already_started_same_day_fixture(self):
        previous = {
            "save_instance_id": "save-123", "game_date": "2028-06-06",
            "game_time": "12:00", "odds_scope": "all", "odds_days": 14,
            "full_verified_at": "2028-06-06T12:00:00",
            "matches": [{
                "fixture_date": "2028-06-06", "kickoff_minutes": 600,
                "competition_id": 100,
                "home": {"id": 10}, "away": {"id": 20},
            }],
        }
        current = {**previous, "matches": []}

        self.assertFalse(
            suspicious_partial_schedule_refresh(previous, current)
        )

    def test_changed_date_uses_stale_cache_path(self):
        valid, reason = startup_cache_validation(
            _snapshot(), "save-123", {"date": "2028-06-07"},
            {"odds_days": 7, "odds_scope": "famous"},
        )

        self.assertFalse(valid)
        self.assertEqual(reason, "date_changed")

    def test_transient_empty_fixture_read_preserves_verified_schedule(self):
        previous = {
            "save_instance_id": "save-123", "game_date": "2028-06-06",
            "odds_scope": "all", "odds_days": 14,
            "competitions": [{"id": 100}],
            "matches": [{
                "fixture_date": "2028-06-07", "competition_id": 100,
                "home": {"id": 10}, "away": {"id": 20},
            }],
        }
        current = {
            "save_instance_id": "save-123", "game_date": "2028-06-06",
            "odds_scope": "all", "odds_days": 14,
            "competitions": [], "matches": [],
        }

        recovered = preserve_previous_schedule_after_empty_read(previous, current)

        self.assertEqual(recovered["matches"], previous["matches"])
        self.assertTrue(recovered["schedule_read_fallback"])

    def test_empty_read_after_date_advance_keeps_only_future_cached_fixtures(self):
        previous = {
            "save_instance_id": "save-123", "game_date": "2028-06-06",
            "odds_scope": "all", "odds_days": 14,
            "matches": [
                {"fixture_date": "2028-06-06", "competition_id": 100},
                {"fixture_date": "2028-06-09", "competition_id": 100},
            ],
        }
        current = {
            "save_instance_id": "save-123", "game_date": "2028-06-08",
            "odds_scope": "all", "odds_days": 14, "matches": [],
        }

        recovered = preserve_previous_schedule_after_empty_read(previous, current)

        self.assertEqual(
            [item["fixture_date"] for item in recovered["matches"]],
            ["2028-06-09"],
        )

    def test_long_running_fast_refresh_rejects_same_context_empty_snapshot(self):
        previous = {
            "save_instance_id": "save-123", "game_date": "2028-06-06",
            "odds_scope": "all", "odds_days": 14,
            "model_version": MODEL_VERSION,
            "matches": [{"fixture_date": "2028-06-07"}],
        }
        current = {
            "save_instance_id": "save-123", "game_date": "2028-07-01",
            "odds_scope": "all", "odds_days": 14, "matches": [],
        }

        recovered = preserve_previous_schedule_after_empty_read(previous, current)

        self.assertEqual(recovered["matches"], [])
        self.assertTrue(suspicious_empty_schedule_refresh(previous, recovered))

    def test_empty_snapshot_after_scope_change_is_not_rejected(self):
        previous = {
            "save_instance_id": "save-123", "game_date": "2028-06-06",
            "odds_scope": "all", "odds_days": 14,
            "model_version": MODEL_VERSION,
            "matches": [{"fixture_date": "2028-06-07"}],
        }
        current = {
            "save_instance_id": "save-123", "game_date": "2028-06-06",
            "odds_scope": "managed_schedule", "odds_days": 14, "matches": [],
        }

        self.assertFalse(suspicious_empty_schedule_refresh(previous, current))

    def test_blacklist_change_allows_expected_empty_or_partial_schedule(self):
        previous = {
            "save_instance_id": "save-123", "game_date": "2028-06-06",
            "odds_scope": "all", "odds_days": 14,
            "model_version": MODEL_VERSION,
            "full_verified_at": "2028-06-06T12:00:00",
            "hidden_competition_ids": [],
            "matches": [{
                "fixture_date": "2028-06-07", "competition_id": 300,
                "home": {"id": 10}, "away": {"id": 20},
            }],
        }
        current = {
            **previous,
            "hidden_competition_ids": [300],
            "matches": [],
        }

        self.assertIs(
            preserve_previous_schedule_after_empty_read(previous, current),
            current,
        )
        self.assertFalse(suspicious_empty_schedule_refresh(previous, current))
        self.assertFalse(suspicious_partial_schedule_refresh(
            previous,
            {**current, "matches": [{
                "fixture_date": "2028-06-08", "competition_id": 400,
                "home": {"id": 30}, "away": {"id": 40},
            }]},
        ))

    def test_fast_refresh_does_not_publish_suspicious_empty_snapshot(self):
        state = self._state(verified=True, data_version=7)
        state.memory_lock = threading.Lock()
        state.refreshing = True
        state.refresh_mode = "fast"
        state.refresh_reason = "manual_fast"
        state.refresh_cancel_on_clock_change = False
        cancel_event = threading.Event()
        state.refresh_cancel_event = cancel_event
        previous = {
            "save_instance_id": "save-123", "game_date": "2028-06-06",
            "odds_scope": "all", "odds_days": 14,
            "model_version": MODEL_VERSION,
            "matches": [{"fixture_date": "2028-06-07"}],
        }
        state.output = previous
        empty = {
            "save_instance_id": "save-123", "game_date": "2028-06-06",
            "odds_scope": "all", "odds_days": 14,
            "model_version": MODEL_VERSION, "matches": [],
        }

        with (
            patch("fm_odds_web.set_active_save_id"),
            patch.object(state, "_settle_results_before_refresh", return_value={"settled": 0}),
            patch("fm_odds_web.load_settings", return_value={"odds_days": 14, "odds_scope": "all"}),
            patch("fm_odds_web.generate_all_odds", return_value=empty),
        ):
            state._refresh_worker("manual_fast", "fast", False, cancel_event)

        self.assertIs(state.output, previous)
        self.assertEqual(state.data_version, 7)
        self.assertTrue(state.cache_verified)
        self.assertFalse(state.refreshing)
        self.assertIn("盘口读取结果异常为空", state.error)
        self.assertEqual(state.last_refresh_error_stage, "validate_snapshot")
        self.assertIn("_refresh_worker", state.last_refresh_traceback)
        self.assertIn("盘口读取结果异常为空", state.last_refresh_traceback)

    def test_completed_same_day_result_requires_fixture_to_disappear(self):
        fixture = {
            "fixture_date": "2028-06-06", "competition_id": 100,
            "home": {"id": 10}, "away": {"id": 20},
        }
        previous = {"matches": [fixture]}
        current = {"game_date": "2028-06-06", "matches": []}
        result = {
            "date": "2028-06-06", "competition": {"id": 100},
            "home_team": {"id": 10}, "away_team": {"id": 20},
            "home_goals": 2, "away_goals": 1,
        }

        self.assertEqual(
            disappeared_same_day_result_keys(previous, current, [result]),
            {("2028-06-06", 100, 10, 20)},
        )
        self.assertEqual(
            disappeared_same_day_result_keys(previous, {**current, "matches": [fixture]}, [result]),
            set(),
        )

    def test_light_result_refresh_publishes_new_standings_data(self):
        self.assertTrue(light_result_refresh_changed(False, 1, 20, 20))
        self.assertFalse(light_result_refresh_changed(False, 0, 20, 20))

    def test_recent_full_refresh_is_throttled(self):
        now = datetime(2028, 6, 6, 12, 0, 0)
        output = {"full_verified_at": (now - timedelta(minutes=5)).isoformat()}

        self.assertFalse(full_reconcile_due(output, now))

    def test_startup_uses_one_fast_pass_when_recent_full_snapshot_is_reusable(self):
        now = datetime(2028, 6, 6, 12, 0, 0)
        output = {"full_verified_at": (now - timedelta(minutes=5)).isoformat()}

        self.assertEqual(startup_odds_refresh_mode(True, "valid", output, now), "fast")
        self.assertEqual(
            startup_odds_refresh_mode(False, "date_changed", output, now),
            "fast",
        )

    def test_startup_runs_directly_as_full_when_reconciliation_is_due(self):
        now = datetime(2028, 6, 6, 12, 0, 0)
        old = {"full_verified_at": (now - timedelta(hours=7)).isoformat()}
        recent = {"full_verified_at": (now - timedelta(minutes=5)).isoformat()}

        self.assertEqual(startup_odds_refresh_mode(True, "valid", old, now), "full")
        self.assertEqual(
            startup_odds_refresh_mode(False, "settings_changed", recent, now),
            "full",
        )

    def test_old_or_missing_full_refresh_is_due(self):
        now = datetime(2028, 6, 6, 12, 0, 0)
        old = {"full_verified_at": (now - timedelta(hours=7)).isoformat()}

        self.assertTrue(full_reconcile_due(old, now))
        self.assertTrue(full_reconcile_due({}, now))

    def test_save_identity_change_requires_two_matching_observations(self):
        candidate, confirmations, changed = advance_identity_change_confirmation(
            "save-a", "save-b", None, 0,
        )
        self.assertEqual((candidate, confirmations, changed), ("save-b", 1, False))

        candidate, confirmations, changed = advance_identity_change_confirmation(
            "save-a", "save-b", candidate, confirmations,
        )
        self.assertEqual((candidate, confirmations, changed), ("save-b", 2, False))

        candidate, confirmations, changed = advance_identity_change_confirmation(
            "save-a", "save-b", candidate, confirmations,
        )
        self.assertEqual((candidate, confirmations, changed), ("save-b", 3, True))

        self.assertEqual(
            advance_identity_change_confirmation(
                "save-a", "save-a", candidate, confirmations,
            ),
            (None, 0, False),
        )

    def test_save_identity_read_failure_keeps_pending_confirmation(self):
        self.assertEqual(
            advance_identity_change_confirmation(
                "save-a", None, "save-b", 1,
            ),
            ("save-b", 1, False),
        )

    def test_equivalent_save_identity_does_not_trigger_change(self):
        with patch(
            "fm_odds_web.save_identity_matches",
            side_effect=lambda left, right: {left, right} == {"career-a", "legacy-a"},
        ):
            self.assertEqual(
                advance_identity_change_confirmation(
                    "career-a", "legacy-a", "career-b", 1,
                ),
                (None, 0, False),
            )

    def test_unconfirmed_save_identity_change_does_not_block_account(self):
        state = self._state(verified=True)

        started = state._mark_save_identity_candidate("save-b", False)

        self.assertFalse(started)
        self.assertTrue(state.cache_verified)
        self.assertFalse(state.save_change_pending)
        self.assertIsNone(getattr(state, "pending_save_identity", None))
        self.assertTrue(state._has_verified_save())
        self.assertTrue(state._betting_ready())

    def test_confirmed_save_identity_change_keeps_old_account_until_publish(self):
        state = self._state(verified=True)

        with patch.object(state, "refresh_async", return_value=True) as refresh:
            started = state._mark_save_identity_candidate("save-b", True)

        self.assertTrue(started)
        self.assertTrue(state.cache_verified)
        self.assertTrue(state.save_change_pending)
        self.assertTrue(state._has_verified_save())
        self.assertEqual(state.pending_save_identity, "save-b")
        self.assertFalse(state._betting_ready())
        refresh.assert_called_once_with("identity_change_full", full=True)

    def test_confirmed_identity_waits_without_blocking_while_refresh_is_busy(self):
        state = self._state(verified=True)
        state.refreshing = True

        with patch.object(state, "refresh_async") as refresh:
            started = state._mark_save_identity_candidate("save-b", True)

        self.assertFalse(started)
        self.assertFalse(state.save_change_pending)
        self.assertIsNone(getattr(state, "pending_save_identity", None))
        refresh.assert_not_called()

    def test_item_memory_transaction_blocks_identity_probe(self):
        state = self._state(verified=True)
        state.memory_lock = threading.Lock()
        state.memory_lock.acquire()

        with patch("fm_odds_web.read_save_identity") as read_identity:
            attempted, identity = state._read_save_identity_when_idle()

        state.memory_lock.release()
        self.assertFalse(attempted)
        self.assertIsNone(identity)
        read_identity.assert_not_called()

    def test_identity_probe_reuses_verified_save_id(self):
        state = self._state(verified=True)
        state.memory_lock = threading.Lock()
        state.output["selected_manager_id"] = 20
        state.output["manager_options"] = [{"id": 20, "manager_id": 20}]

        with patch(
            "fm_odds_web.read_save_identity", return_value="save-123",
        ) as read_identity:
            attempted, identity = state._read_save_identity_when_idle()

        self.assertTrue(attempted)
        self.assertEqual(identity, "save-123")
        read_identity.assert_called_once_with(
            preferred_save_id="save-123",
            preferred_manager_id=20,
            known_manager_sessions=[{"id": 20, "manager_id": 20}],
        )

    def test_identity_change_refresh_preserves_old_snapshot_until_publish(self):
        state = self._state(verified=True)
        state.pending_save_identity = "save-b"
        state.save_change_pending = True
        state.club_profile = {"team": {"id": 10}}
        state.club_context = {"team": {"id": 10}}
        state.club_profiles = {10: state.club_profile}
        state.club_contexts = {10: state.club_context}
        state.club_last_updated = "2028-06-06"
        state.club_error = None

        with patch("fm_odds_web.threading.Thread") as create_thread:
            started = state.refresh_async("identity_change_full", full=True)

        self.assertTrue(started)
        self.assertTrue(state.cache_verified)
        self.assertEqual(state.output["save_instance_id"], "save-123")
        self.assertEqual(state.club_profile, {"team": {"id": 10}})
        self.assertEqual(
            create_thread.call_args.kwargs["args"][0], "identity_change_full",
        )

    def test_identity_change_full_read_must_confirm_new_candidate(self):
        validate_identity_change_output("save-a", "save-b", "save-b")

        with self.assertRaisesRegex(RuntimeError, "候选存档身份不一致"):
            validate_identity_change_output("save-a", "save-b", "save-c")
        with self.assertRaisesRegex(RuntimeError, "仍属于当前存档"):
            validate_identity_change_output("save-a", "save-b", "save-a")

    def test_stable_clock_schedule_is_rechecked_after_interval(self):
        state = self._state(verified=True)
        state.memory_lock = threading.Lock()
        state.output = {
            "save_instance_id": "save-123",
            "odds_days": 7,
            "odds_scope": "all",
            "managed_teams": [],
            "matches": [{
                "fixture_date": "2028-06-06",
                "competition_id": 100,
                "home": {"id": 10},
                "away": {"id": 20},
                "kickoff_minutes": 900,
            }],
        }
        clock = ("2028-06-06", 600)
        baseline = state._output_schedule_snapshot(state.output, *clock)
        state.schedule_probe_clock = clock
        state.schedule_probe_completed_clock = clock
        state.schedule_probe_candidate = None
        state.schedule_probe_confirmations = 0
        state.schedule_probe_last_at = 100.0

        with (
            patch("fm_odds_web.monotonic", return_value=131.0),
            patch(
                "fm_odds_web.read_upcoming_fixture_snapshot",
                return_value={"game_date": clock[0], **baseline},
            ) as read,
        ):
            result = state._probe_schedule_change(clock)

        self.assertEqual(result, "stable")
        read.assert_called_once()

    def test_changed_schedule_clears_stable_clock_marker(self):
        state = self._state(verified=True)
        state.memory_lock = threading.Lock()
        state.output = {
            "save_instance_id": "save-123",
            "odds_days": 7,
            "odds_scope": "all",
            "managed_teams": [],
            "matches": [{
                "fixture_date": "2028-06-06",
                "competition_id": 100,
                "home": {"id": 10},
                "away": {"id": 20},
                "kickoff_minutes": 900,
            }],
        }
        clock = ("2028-06-06", 600)
        state.schedule_probe_clock = clock
        state.schedule_probe_completed_clock = clock
        state.schedule_probe_candidate = None
        state.schedule_probe_confirmations = 0
        state.schedule_probe_last_at = 100.0
        live = {
            "game_date": clock[0],
            "fingerprint": "changed",
            "fixtures": [[clock[0], 100, 10, 30, 900]],
        }

        with (
            patch("fm_odds_web.monotonic", return_value=131.0),
            patch("fm_odds_web.read_upcoming_fixture_snapshot", return_value=live),
        ):
            result = state._probe_schedule_change(clock)

        self.assertEqual(result, "pending")
        self.assertIsNone(state.schedule_probe_completed_clock)
        self.assertEqual(state.schedule_probe_confirmations, 1)

    def test_fixture_disappearance_is_not_recorded_as_cancellation(self):
        state = self._state(verified=True)
        state.memory_lock = threading.Lock()
        state.output = {
            "save_instance_id": "save-123",
            "odds_days": 7,
            "odds_scope": "all",
            "managed_teams": [],
            "matches": [{
                "fixture_date": "2028-06-07",
                "competition_id": 100,
                "home": {"id": 10},
                "away": {"id": 20},
                "kickoff_minutes": 900,
            }],
        }
        clock = ("2028-06-06", 600)
        state.schedule_probe_clock = clock
        state.schedule_probe_completed_clock = None
        state.schedule_probe_candidate = None
        state.schedule_probe_confirmations = 1
        state.schedule_probe_last_at = 100.0
        baseline = state._output_schedule_snapshot(state.output, *clock)
        missing_signature = __import__("hashlib").sha256(__import__("json").dumps(
            {"missing": baseline["fixtures"], "added": []},
            ensure_ascii=True, separators=(",", ":"),
        ).encode("ascii")).hexdigest()
        state.schedule_probe_candidate = missing_signature
        live = {"game_date": clock[0], "fingerprint": "changed", "fixtures": []}

        with (
            patch("fm_odds_web.monotonic", return_value=111.0),
            patch("fm_odds_web.read_upcoming_fixture_snapshot", return_value=live),
            patch("fm_odds_web.settle_pending_bets") as settle,
            patch.object(state, "refresh_async", return_value=False),
        ):
            state._probe_schedule_change(clock)

        settle.assert_not_called()

    def test_fixture_address_cache_expires_after_two_minutes(self):
        state = {"fixture_addresses": [0x1000], "fixture_scanned_at": 100.0}
        reader = object()
        with (
            patch("tools.preview_cup_odds._runtime_state", return_value=state),
            patch("tools.preview_cup_odds.monotonic", return_value=219.9),
            patch("tools.preview_cup_odds.scan_fixture_addresses") as scan,
        ):
            addresses, cache_hit = preview_cup_odds.cached_fixture_addresses(reader)

        self.assertEqual(addresses, [0x1000])
        self.assertTrue(cache_hit)
        scan.assert_not_called()

        with (
            patch("tools.preview_cup_odds._runtime_state", return_value=state),
            patch("tools.preview_cup_odds.monotonic", return_value=220.0),
            patch(
                "tools.preview_cup_odds.scan_fixture_addresses",
                return_value=([0x1000, 0x2000], 4096),
            ) as scan,
        ):
            addresses, cache_hit = preview_cup_odds.cached_fixture_addresses(reader)

        self.assertEqual(addresses, [0x1000, 0x2000])
        self.assertFalse(cache_hit)
        scan.assert_called_once()

    def test_forced_fixture_address_refresh_bypasses_fresh_cache(self):
        state = {"fixture_addresses": [0x1000], "fixture_scanned_at": 100.0}
        reader = object()
        with (
            patch("tools.preview_cup_odds._runtime_state", return_value=state),
            patch("tools.preview_cup_odds.monotonic", return_value=101.0),
            patch(
                "tools.preview_cup_odds.scan_fixture_addresses",
                return_value=([0x1000, 0x2000], 4096),
            ) as scan,
        ):
            addresses, cache_hit = preview_cup_odds.cached_fixture_addresses(
                reader, force_scan=True,
            )

        self.assertEqual(addresses, [0x1000, 0x2000])
        self.assertFalse(cache_hit)
        scan.assert_called_once_with(reader, cancel_check=None)

    def test_fixture_address_rescan_keeps_still_valid_cached_matches(self):
        state = {
            "scope": (123, 0x400000, "save-1"),
            "fixture_addresses": [0x1000, 0x2000, 0x3000],
            "fixture_scanned_at": 100.0,
        }
        reader = object()
        with (
            patch("tools.preview_cup_odds._runtime_state", return_value=state),
            patch("tools.preview_cup_odds.monotonic", return_value=220.0),
            patch(
                "tools.preview_cup_odds.scan_fixture_addresses",
                return_value=([0x1000, 0x4000], 4096),
            ),
            patch(
                "tools.preview_cup_odds.read_fixture_snapshots_at_addresses",
                return_value=({0x2000: b"fixture"}, 0x100),
            ) as validate,
        ):
            addresses, cache_hit = preview_cup_odds.cached_fixture_addresses(reader)

        self.assertEqual(addresses, [0x1000, 0x2000, 0x4000])
        self.assertEqual(state["fixture_addresses"], addresses)
        self.assertFalse(cache_hit)
        validate.assert_called_once_with(reader, [0x2000, 0x3000])

    def test_fixture_address_rescan_drops_invalid_cached_matches(self):
        state = {
            "scope": (123, 0x400000, "save-1"),
            "fixture_addresses": [0x1000, 0x2000],
            "fixture_scanned_at": 100.0,
        }
        reader = object()
        with (
            patch("tools.preview_cup_odds._runtime_state", return_value=state),
            patch("tools.preview_cup_odds.monotonic", return_value=220.0),
            patch(
                "tools.preview_cup_odds.scan_fixture_addresses",
                return_value=([0x3000], 4096),
            ),
            patch(
                "tools.preview_cup_odds.read_fixture_snapshots_at_addresses",
                return_value=({}, 0x100),
            ),
        ):
            addresses, cache_hit = preview_cup_odds.cached_fixture_addresses(reader)

        self.assertEqual(addresses, [0x3000])
        self.assertEqual(state["fixture_addresses"], addresses)
        self.assertFalse(cache_hit)

    def test_unverified_cache_is_rejected_before_memory_reads(self):
        state = self._state(verified=False)

        with self.assertRaisesRegex(ValueError, "缓存尚未验证"):
            state.place_bet({
                "mode": "single", "stake": 10, "data_version": 3,
                "selections": [{"fixture_date": "2028-06-06"}],
            })

    def test_parlay_fixture_dates_are_validated_before_memory_reads(self):
        state = self._state(verified=False)
        selections = [
            {
                "fixture_date": "2028-06-06", "competition_id": 100,
                "home_id": 10, "away_id": 20,
            },
            {
                "fixture_date": "2028-06-07", "competition_id": 100,
                "home_id": 30, "away_id": 40,
            },
        ]

        with self.assertRaisesRegex(ValueError, "缓存尚未验证"):
            state.place_bet({
                "mode": "parlay", "stake": 10, "data_version": 3,
                "selections": selections,
            })

    def test_parlay_match_limit_accepts_ten_and_rejects_eleven(self):
        state = self._state(verified=False)

        def selections(count: int) -> list[dict[str, object]]:
            return [
                {
                    "fixture_date": "2028-06-06",
                    "competition_id": 100,
                    "home_id": index * 2 + 1,
                    "away_id": index * 2 + 2,
                }
                for index in range(count)
            ]

        with self.assertRaisesRegex(ValueError, "缓存尚未验证"):
            state.place_bet({
                "mode": "parlay", "stake": 10, "data_version": 3,
                "selections": selections(10),
            })

        with self.assertRaisesRegex(ValueError, "最多选择 10 场比赛"):
            state.place_bet({
                "mode": "parlay", "stake": 10, "data_version": 3,
                "selections": selections(11),
            })

    def test_betting_cutoff_uses_clock_and_closes_after_kickoff(self):
        state = self._state(verified=True)
        state.refreshing = True
        state.refresh_mode = "results"

        self.assertTrue(state.refresh_status()["betting_ready"])
        clock = {"date": "2028-06-06", "minutes": 900}
        future_leg = {
            "fixture_date": "2028-06-06", "kickoff_minutes": 930,
            "home": "Future Home", "away": "Future Away",
        }
        current_leg = {
            "fixture_date": "2028-06-06", "kickoff_minutes": 900,
            "home": "Current Home", "away": "Current Away",
        }
        started_leg = {
            "fixture_date": "2028-06-06", "kickoff_minutes": 899,
            "home": "Started Home", "away": "Started Away",
        }

        validate_bet_times(clock, [future_leg])
        validate_bet_times(clock, [current_leg])
        with self.assertRaisesRegex(ValueError, "已超过下注时间"):
            validate_bet_times(clock, [started_leg])

    def test_betting_clock_requires_background_and_live_clock_to_match(self):
        clock = {"date": "2028-06-06", "minutes": 900}

        validate_betting_clock_sync(clock, dict(clock), block_on_mismatch=True)
        validate_betting_clock_sync(
            clock, {"date": "2028-06-06", "minutes": 901},
            block_on_mismatch=False,
        )
        with self.assertRaisesRegex(ValueError, "当前时钟尚未同步"):
            validate_betting_clock_sync(None, clock, block_on_mismatch=True)
        with self.assertRaisesRegex(ValueError, "游戏时间已经变化"):
            validate_betting_clock_sync(
                clock, {"date": "2028-06-06", "minutes": 901}, block_on_mismatch=True,
            )
        with self.assertRaisesRegex(ValueError, "游戏时间已经变化"):
            validate_betting_clock_sync(
                clock, {"date": "2028-06-07", "minutes": 900}, block_on_mismatch=True,
            )

    def test_place_bet_rejects_unsynchronized_clock_before_engine_read(self):
        state = self._state(verified=True)
        state.last_connection_clock = {
            "date": "2028-06-06", "time": "15:00", "minutes": 900,
        }
        state.output = {"save_instance_id": "save-123", "matches": []}

        with (
            patch("fm_odds_web.set_active_save_id"),
            patch(
                "fm_odds_web.read_game_clock",
                return_value={
                    "date": "2028-06-06", "time": "15:01", "minutes": 901,
                },
            ),
            patch(
                "fm_odds_web.read_betting_match_engine_state",
                return_value={"known": True, "active": True},
            ) as read_engine,
            patch("fm_odds_web.reserve_bets") as reserve,
        ):
            with self.assertRaisesRegex(ValueError, "游戏时间已经变化"):
                state.place_bet({
                    "mode": "single",
                    "stake": 10,
                    "data_version": 3,
                    "save_instance_id": "save-123",
                    "selections": [{
                        "fixture_date": "2028-06-06",
                        "competition_id": 100,
                        "home_id": 10,
                        "away_id": 20,
                    }],
                })

        read_engine.assert_called_once_with()
        reserve.assert_not_called()

    def test_god_mode_skips_engine_and_clock_sync_guards(self):
        state = self._state(verified=True)
        state.last_connection_clock = {
            "date": "2028-06-06", "time": "15:00", "minutes": 900,
        }
        state.output = {"save_instance_id": "save-123", "matches": []}
        state.club_contexts = {}
        leg = {
            "fixture_date": "2028-06-06",
            "kickoff_minutes": 930,
            "home": "Future Home",
            "away": "Future Away",
            "odds": 2.0,
        }
        settings = {
            "god_mode": True,
            "disable_fa_penalties": True,
            "betting_limits_enabled": False,
            "default_betting_limit_single": 20_000_000,
            "default_betting_limit_parlay": 100_000_000,
        }

        with (
            patch("fm_odds_web.set_active_save_id"),
            patch("fm_odds_web.load_settings", return_value=settings),
            patch(
                "fm_odds_web.read_game_clock",
                return_value={
                    "date": "2028-06-06", "time": "15:01", "minutes": 901,
                },
            ),
            patch("fm_odds_web.read_betting_match_engine_state") as read_engine,
            patch.object(state, "_validated_leg", return_value=leg),
            patch(
                "fm_odds_web.reserve_bets",
                return_value={"balance": 990.0, "reserved_bets": []},
            ) as reserve,
        ):
            result = state.place_bet({
                "mode": "single",
                "stake": 10,
                "data_version": 3,
                "save_instance_id": "save-123",
                "selections": [{
                    "fixture_date": "2028-06-06",
                    "competition_id": 100,
                    "home_id": 10,
                    "away_id": 20,
                }],
            })

        read_engine.assert_not_called()
        reserve.assert_called_once()
        self.assertEqual(result["balance"], 990.0)

    def test_acquired_intelligence_skips_spectator_guards_in_place_bet(self):
        state = self._state(verified=True)
        state.last_connection_clock = {
            "date": "2028-06-06", "time": "21:59", "minutes": 1319,
        }
        state.output = {"save_instance_id": "save-123", "matches": []}
        state.club_contexts = {}
        leg = {
            "fixture_date": "2028-06-06", "kickoff_minutes": 1320,
            "competition_id": 100, "home_id": 10, "away_id": 20,
            "home": "Intel Home", "away": "Intel Away", "odds": 2.0,
        }
        settings = {
            "god_mode": False,
            "disable_fa_penalties": True,
            "betting_limits_enabled": False,
            "default_betting_limit_single": 20_000_000,
            "default_betting_limit_parlay": 100_000_000,
        }

        with (
            patch("fm_odds_web.set_active_save_id"),
            patch("fm_odds_web.load_settings", return_value=settings),
            patch(
                "fm_odds_web.match_intelligence_betting_unlocked_fixture_keys",
                return_value={("2028-06-06", 100, 10, 20)},
            ),
            patch(
                "fm_odds_web.read_game_clock",
                return_value={
                    "date": "2028-06-06", "time": "22:00", "minutes": 1320,
                },
            ),
            patch(
                "fm_odds_web.read_betting_match_engine_state",
                return_value={"known": True, "active": True, "spectator": True},
            ),
            patch.object(state, "_validated_leg", return_value=leg),
            patch(
                "fm_odds_web.reserve_bets",
                return_value={"balance": 990.0, "reserved_bets": []},
            ) as reserve,
        ):
            result = state.place_bet({
                "mode": "single", "stake": 10, "data_version": 3,
                "save_instance_id": "save-123", "selections": [{
                    "fixture_date": "2028-06-06", "competition_id": 100,
                    "home_id": 10, "away_id": 20,
                }],
            })

        reserve.assert_called_once()
        self.assertEqual(result["balance"], 990.0)

    def test_betting_does_not_depend_on_native_match_engine_state(self):
        source = inspect.getsource(LocalOddsState.place_bet)

        self.assertNotIn("read_match_engine_state", source)

    def test_confirmed_active_match_blocks_fixture_at_kickoff(self):
        clock = {"date": "2028-06-06", "minutes": 900}
        leg = {
            "fixture_date": "2028-06-06", "kickoff_minutes": 900,
            "home": "Current Home", "away": "Current Away",
        }

        with self.assertRaisesRegex(ValueError, "已经进入比赛引擎"):
            validate_active_match_bet_times(clock, [leg])

    def test_match_engine_guard_only_applies_to_exact_current_kickoff(self):
        clock = {"date": "2028-06-06", "minutes": 900}
        earlier_leg = {
            "fixture_date": "2028-06-06", "kickoff_minutes": 899,
            "home": "Earlier Home", "away": "Earlier Away",
        }
        future_leg = {
            "fixture_date": "2028-06-06", "kickoff_minutes": 901,
            "home": "Future Home", "away": "Future Away",
        }

        validate_active_match_bet_times(clock, [earlier_leg, future_leg])
        with self.assertRaisesRegex(ValueError, "已超过下注时间"):
            validate_bet_times(clock, [earlier_leg])

    def test_unknown_engine_state_does_not_add_a_betting_lock(self):
        state = self._state(verified=True)
        clock = {"date": "2028-06-06", "minutes": 900}
        started = {"known": True, "active": True, "state": 4}
        unknown = {"known": False, "active": False}

        self.assertTrue(
            state._note_betting_match_engine_state(clock, started)["current_time"]
        )
        restrictions = state._note_betting_match_engine_state(clock, unknown)

        self.assertFalse(restrictions["current_time"])
        self.assertFalse(restrictions["spectator_day"])
        self.assertIsNone(state.match_engine_betting_lock)

    def test_betting_engine_state_retries_transient_unknown_read(self):
        with patch(
            "fm_odds_web.read_match_engine_state",
            side_effect=[
                {"known": False, "active": False},
                {"known": True, "active": False},
            ],
        ) as read_engine:
            engine_state = read_betting_match_engine_state()

        self.assertTrue(engine_state["known"])
        self.assertFalse(engine_state["active"])
        self.assertEqual(read_engine.call_count, 2)

    def test_formal_match_state_locks_only_current_game_clock(self):
        state = self._state(verified=True)
        clock = {"date": "2028-06-06", "minutes": 900}
        started = {"known": True, "active": True, "state": 4}
        postmatch = {"known": True, "active": False, "state": 0}
        current_leg = {
            "fixture_date": "2028-06-06", "kickoff_minutes": 900,
            "home": "Current Home", "away": "Current Away",
        }
        future_leg = {
            "fixture_date": "2028-06-06", "kickoff_minutes": 930,
            "home": "Future Home", "away": "Future Away",
        }

        started_restrictions = state._note_betting_match_engine_state(clock, started)
        postmatch_restrictions = state._note_betting_match_engine_state(clock, postmatch)
        self.assertTrue(started_restrictions["current_time"])
        self.assertFalse(started_restrictions["spectator_day"])
        self.assertTrue(postmatch_restrictions["current_time"])
        self.assertFalse(postmatch_restrictions["spectator_day"])
        with self.assertRaisesRegex(ValueError, "已经进入比赛引擎"):
            validate_active_match_bet_times(clock, [current_leg])
        validate_active_match_bet_times(clock, [future_leg])

        advanced_clock = {"date": "2028-06-06", "minutes": 901}
        advanced = state._note_betting_match_engine_state(advanced_clock, postmatch)
        self.assertFalse(advanced["current_time"])
        self.assertFalse(advanced["spectator_day"])
        self.assertIsNone(state.match_engine_betting_lock)

    def test_match_engine_betting_lock_does_not_cross_saves(self):
        state = self._state(verified=True)
        clock = {"date": "2028-06-06", "minutes": 900}
        started = {"known": True, "active": True, "state": 4}
        inactive = {"known": True, "active": False, "state": 0}

        self.assertTrue(
            state._note_betting_match_engine_state(clock, started)["current_time"]
        )
        state.output = {"save_instance_id": "save-456"}

        restrictions = state._note_betting_match_engine_state(clock, inactive)
        self.assertFalse(restrictions["current_time"])
        self.assertFalse(restrictions["spectator_day"])
        self.assertIsNone(state.match_engine_betting_lock)

    def test_spectator_lock_only_blocks_fixtures_within_three_hours(self):
        state = self._state(verified=True)
        clock = {"date": "2028-06-06", "minutes": 900}
        spectating = {
            "known": True, "active": True, "spectator": True, "state": 0,
        }
        within_window = {
            "fixture_date": "2028-06-06", "kickoff_minutes": 1080,
            "home": "Near Home", "away": "Near Away",
        }
        outside_window = {
            "fixture_date": "2028-06-06", "kickoff_minutes": 1081,
            "home": "Later Home", "away": "Later Away",
        }

        restrictions = state._note_betting_match_engine_state(clock, spectating)
        self.assertTrue(restrictions["current_time"])
        self.assertTrue(restrictions["spectator_day"])
        with self.assertRaises(ValueError) as raised:
            validate_spectator_bet_times(clock, [within_window])
        self.assertEqual(
            str(raised.exception),
            "观赛期间不能投注的比赛：Near Home VS Near Away",
        )
        validate_spectator_bet_times(clock, [outside_window])

    def test_fm24_native_viewer_state_applies_spectator_lock(self):
        clock = {"date": "2023-08-21", "minutes": 19 * 60}
        output = {
            "save_instance_id": "save-123",
            "game_layout": "fm24",
            "game_date": "2023-08-21",
            "odds_scope": "all",
            "fixture_scan_mode": "pool",
            "managed_teams": [{"id": 679, "name": "Manchester City"}],
            "matches": [
                {
                    "fixture_date": "2023-08-21", "kickoff_minutes": 19 * 60,
                    "home": {"id": 2389}, "away": {"id": 729500},
                },
                {
                    "fixture_date": "2023-08-21", "kickoff_minutes": 19 * 60,
                    "home": {"id": 2000340743}, "away": {"id": 2000340733},
                },
            ],
        }
        engine_state = {
            "known": True, "active": True, "session_count": 1,
            "spectator": True, "spectator_session_count": 1,
            "detector": "native_match_session_vector",
        }

        self.assertTrue(
            fm24_spectator_session_from_native_state(output, engine_state)
        )
        state = self._state(verified=True)
        state.output = output
        restrictions = state._note_betting_match_engine_state(
            clock, engine_state,
        )

        self.assertTrue(restrictions["spectator_day"])
        self.assertEqual(
            engine_state["spectator_detector"],
            "fm24_game_match_session_viewer_vector",
        )

    def test_fm24_retained_session_without_live_viewer_is_not_spectator(self):
        output = {
            "game_layout": "fm24",
        }
        engine_state = {
            "known": True, "active": True, "session_count": 1,
            "spectator": False, "spectator_session_count": 0,
            "detector": "native_match_session_vector",
        }

        self.assertFalse(
            fm24_spectator_session_from_native_state(output, engine_state)
        )

    def test_fm24_native_viewer_state_does_not_require_schedule_scope(self):
        engine_state = {
            "known": True, "active": True, "spectator": True,
            "detector": "native_match_session_vector",
        }
        self.assertTrue(fm24_spectator_session_from_native_state(
            {"game_layout": "fm24", "odds_scope": "famous"}, engine_state,
        ))
        self.assertFalse(fm24_spectator_session_from_native_state(
            {"game_layout": "fm26"}, engine_state,
        ))

    def test_spectator_lock_three_hour_window_crosses_midnight(self):
        clock = {"date": "2028-06-06", "minutes": 23 * 60 + 30}
        within_window = {
            "fixture_date": "2028-06-07", "kickoff_minutes": 150,
            "home": "Near Home", "away": "Near Away",
        }
        outside_window = {
            "fixture_date": "2028-06-07", "kickoff_minutes": 151,
            "home": "Later Home", "away": "Later Away",
        }

        with self.assertRaisesRegex(ValueError, "观赛期间不能投注的比赛"):
            validate_spectator_bet_times(clock, [within_window])
        validate_spectator_bet_times(clock, [outside_window])

    def test_spectator_lock_remains_after_spectator_session_ends(self):
        state = self._state(verified=True)
        state.output = {
            "save_instance_id": "save-123",
            "matches": [{
                "fixture_date": "2028-06-06", "kickoff_minutes": 1320,
                "competition_id": 100,
                "home": {"id": 10}, "away": {"id": 20},
            }],
        }
        state.connection_save_id = "save-123"
        state.match_engine_betting_lock = None
        state.spectator_betting_locks = set()
        state._note_betting_match_engine_state(
            {"date": "2028-06-06", "minutes": 1140},
            {"known": True, "active": True, "spectator": True},
        )
        locked_leg = {
            "fixture_date": "2028-06-06", "kickoff_minutes": 1320,
            "competition_id": 100, "home_id": 10, "away_id": 20,
            "home": "Locked Home", "away": "Locked Away",
        }
        with self.assertRaises(ValueError) as raised:
            validate_permanent_spectator_locks(
                [locked_leg], state.spectator_betting_locks,
            )
        self.assertEqual(
            str(raised.exception),
            "观战之后不允许下注：Locked Home VS Locked Away",
        )

    def test_acquired_intelligence_bypasses_engine_guards_but_not_betting_cutoff(self):
        clock = {"date": "2028-06-06", "minutes": 1320}
        unlocked_leg = {
            "fixture_date": "2028-06-06", "kickoff_minutes": 1320,
            "competition_id": 100, "home_id": 10, "away_id": 20,
            "home": "Intel Home", "away": "Intel Away",
        }
        regular_leg = {
            "fixture_date": "2028-06-06", "kickoff_minutes": 1320,
            "competition_id": 100, "home_id": 30, "away_id": 40,
            "home": "Regular Home", "away": "Regular Away",
        }
        unlocked_key = {("2028-06-06", 100, 10, 20)}

        restricted = match_engine_restricted_legs(
            [unlocked_leg, regular_leg], unlocked_key,
        )
        self.assertEqual(restricted, [regular_leg])
        validate_permanent_spectator_locks(
            match_engine_restricted_legs([unlocked_leg], unlocked_key),
            unlocked_key,
        )
        validate_spectator_bet_times(
            clock, match_engine_restricted_legs([unlocked_leg], unlocked_key),
        )
        validate_active_match_bet_times(
            clock, match_engine_restricted_legs([unlocked_leg], unlocked_key),
        )

        started_leg = {**unlocked_leg, "kickoff_minutes": 1319}
        with self.assertRaisesRegex(ValueError, "已超过下注时间"):
            validate_bet_times(clock, [started_leg])

    def test_verified_flag_without_save_identity_is_not_bettable(self):
        state = self._state(verified=True)
        state.output = {"matches": []}

        self.assertFalse(state.refresh_status()["betting_ready"])

    def test_refresh_status_never_blocks_on_account_wallet_storage(self):
        state = self._state(verified=True)
        state.output = {
            "save_instance_id": "save-123",
            "selected_manager_id": 42,
            "account_scope_id": "account-current",
            "account_scope_manager_id": 42,
        }
        state.connection_scope_id = "account-initial"
        state.wallet_ready = True

        with (
            patch("fm_odds_web.set_active_save_id") as bind_scope,
            patch("fm_odds_web.available_balance") as read_balance,
        ):
            status = state.refresh_status()

        bind_scope.assert_not_called()
        read_balance.assert_not_called()
        self.assertEqual(status["wallet_scope_id"], "account-current")
        self.assertNotIn("wallet_balance", status)

    def test_refresh_status_reuses_wallet_snapshot_while_refresh_is_busy(self):
        from tools.refresh_status import RefreshStatusSnapshot

        state = self._state(verified=True)
        state.output = {
            "save_instance_id": "save-123",
            "selected_manager_id": 42,
            "account_scope_id": "account-current",
            "account_scope_manager_id": 42,
        }
        state.connection_scope_id = "account-current"
        state.wallet_ready = True
        state.refreshing = True
        coordinator = state._refresh_coordinator()
        coordinator._last_status_snapshot = RefreshStatusSnapshot(
            coordinator._status_context_key(),
            {"wallet_scope_id": "account-current", "wallet_balance": 875.0},
        )

        with (
            patch("fm_odds_web.set_active_save_id") as bind_scope,
            patch("fm_odds_web.available_balance") as read_balance,
        ):
            status = state.refresh_status()

        bind_scope.assert_not_called()
        read_balance.assert_not_called()
        self.assertEqual(status["wallet_scope_id"], "account-current")
        self.assertEqual(status["wallet_balance"], 875.0)

    def test_verified_snapshot_remains_bettable_during_odds_refresh(self):
        state = self._state(verified=True)

        for refresh_mode in ("fast", "full", "results"):
            with self.subTest(refresh_mode=refresh_mode):
                state.refreshing = True
                state.refresh_mode = refresh_mode
                self.assertTrue(state._betting_ready())

        source = inspect.getsource(LocalOddsState.place_bet)
        self.assertNotIn("当前盘口正在刷新", source)

    def test_same_save_stale_version_is_repriced(self):
        self.assertTrue(validate_bet_snapshot(
            {"data_version": 2, "save_instance_id": "save-123"},
            "save-123", 3,
        ))

    def test_stale_request_from_another_save_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "存档已变化"):
            validate_bet_snapshot(
                {"data_version": 2, "save_instance_id": "save-old"},
                "save-current", 3,
            )

    def test_manual_manager_switch_reuses_current_schedule(self):
        state = self._state(verified=True)
        state.memory_lock = threading.Lock()
        state.output = {
            "save_instance_id": "save-123",
            "selected_manager_id": 10,
            "manager_options": [
                {"id": 10, "teams": [{"id": 100, "team_type": "club"}]},
                {"id": 20, "name": "Manager Two", "teams": [{"id": 200, "team_type": "club"}]},
            ],
            "managed_team": {"id": 100, "name": "Team One"},
            "managed_teams": [{"id": 100, "name": "Team One"}],
            "matches": [{"fixture_date": "2028-06-06"}],
        }
        state.club_refreshing = False
        for name in (
            "redbull_hook", "referee_hook", "ca_growth_hook",
            "attribute_growth_hook", "team_nuclear_hooks", "simple_effects",
        ):
            setattr(state, name, SimpleNamespace(close=MagicMock()))

        with (
            patch("fm_odds_web.remember_selected_manager") as remember,
            patch.object(state, "_assign_account_scope", return_value="save-123.manager-20"),
            patch("fm_odds_web.remember_active_save_id") as activate,
            patch("fm_odds_web.threading.Thread") as create_thread,
        ):
            result = state.switch_manager({"manager_id": 20})

        self.assertEqual(result["mode"], "cached_account_context")
        self.assertTrue(result["completed"])
        self.assertFalse(state.refreshing)
        self.assertEqual(state.output["matches"], [{"fixture_date": "2028-06-06"}])
        self.assertEqual(state.output["selected_manager_id"], 20)
        self.assertEqual(state.output["managed_team"]["id"], 200)
        remember.assert_called_once_with("save-123", 20)
        activate.assert_called_once_with("save-123.manager-20")
        create_thread.assert_not_called()
        for name in (
            "redbull_hook", "referee_hook", "ca_growth_hook",
            "attribute_growth_hook", "team_nuclear_hooks", "simple_effects",
        ):
            getattr(state, name).close.assert_not_called()

    def test_confirmed_team_change_starts_worker_with_training_reset(self):
        state = self._state(verified=True)
        state.output.update({
            "selected_manager_id": 10,
            "manager": {"id": 10},
            "managed_team": {"id": 100},
            "managed_teams": [{"id": 100}],
        })
        state.club_refreshing = False
        state.club_profile = {"players": []}
        state.club_profiles = {100: state.club_profile}
        state.club_context = {"team": {"id": 100}}
        state.club_contexts = {100: state.club_context}
        state.club_last_updated = "2028-06-06"
        state.club_error = None
        refreshed = {
            "selected_manager_id": 10,
            "manager": {"id": 10},
            "managed_team": {"id": 200},
            "managed_teams": [{"id": 200}],
        }

        with (
            patch("fm_odds_web.load_settings", return_value={"odds_scope": "all"}),
            patch("fm_odds_web.threading.Thread") as create_thread,
        ):
            started = state._start_manager_team_context_refresh(refreshed)

        self.assertTrue(started)
        worker_args = create_thread.call_args.kwargs["args"]
        self.assertFalse(worker_args[5])
        self.assertTrue(worker_args[6])

    def test_same_manager_team_change_keeps_owned_club_account_scope(self):
        state = self._state(verified=True)
        state.memory_lock = threading.Lock()
        state.output = {
            "save_instance_id": "career-a",
            "game_date": "2028-06-06",
            "selected_manager_id": 10,
            "account_scope_id": "account-owned-clubs",
            "account_scope_manager_id": 10,
            "manager": {"id": 10},
            "manager_options": [{"id": 10}],
            "managed_team": {"id": 100},
            "managed_teams": [{"id": 100}],
        }
        refreshed = {
            "game_date": "2028-06-06",
            "selected_manager_id": 10,
            "manager": {"id": 10},
            "manager_options": [{"id": 10}],
            "manager_selection_required": False,
            "managed_team": {"id": 200, "name": "New Club"},
            "managed_teams": [{"id": 200, "name": "New Club"}],
        }
        state._data_scope_id = MagicMock(return_value="account-owned-clubs")
        state._assign_account_scope = MagicMock(return_value="wrong-new-account")
        state.attribute_growth_hook = SimpleNamespace(sync=MagicMock())
        state._sync_youth_generation_plans = MagicMock()
        state._sync_owned_club_board_hook = MagicMock()
        state._fixture_index = MagicMock(return_value={})
        state.redbull_hook = SimpleNamespace(sync=MagicMock(return_value={}))
        state.referee_hook = SimpleNamespace(sync=MagicMock())
        state.simple_effects = SimpleNamespace(sync=MagicMock())
        state.team_nuclear_hooks = SimpleNamespace(sync=MagicMock())
        state._sync_ca_growth_hook = MagicMock()
        state._remember_fixture_effect_status = MagicMock()
        state._active_referee_context = MagicMock(return_value=None)
        state._club_team_address = MagicMock(return_value=None)
        state._club_address = MagicMock(return_value=None)
        state.roster_effect_errors = []

        with (
            patch("fm_odds_web.remember_active_save_id") as remember,
            patch("fm_odds_web.release_training_users") as release_training,
            patch("fm_odds_web.reconcile_inventory"),
            patch(
                "fm_odds_web.read_club_context",
                return_value={"team": {}},
            ),
            patch("fm_odds_web.sync_doping_effects", return_value={}),
            patch("fm_odds_web.sync_goalkeeper_bribes", return_value={}),
            patch("fm_odds_web.active_player_ids", return_value=[]),
            patch("fm_odds_web.active_effect_skus", return_value=[]),
            patch("fm_odds_web.active_doping_items", return_value=[]),
            patch("fm_odds_web.active_goalkeeper_bribes", return_value=[]),
            patch("fm_odds_web.active_referee_level", return_value=0),
            patch("fm_odds_web.canteen_plan", return_value=None),
            patch(
                "fm_odds_web.read_match_engine_state",
                return_value={"known": True, "active": False},
            ),
        ):
            state._manager_context_worker(
                10, 10, "career-a", refreshed_context=refreshed,
                release_training_users_on_club_change=True,
            )

        state._assign_account_scope.assert_not_called()
        remember.assert_called_once_with("account-owned-clubs")
        self.assertEqual(state.output["account_scope_id"], "account-owned-clubs")
        self.assertEqual(state.output["managed_team"]["id"], 200)
        release_training.assert_called_once_with(
            "2028-06-06", reason="manager_club_change",
        )
        state._sync_owned_club_board_hook.assert_called_once()

    def test_manager_club_change_waits_for_confirmation_before_refresh(self):
        state = self._state(verified=True)
        state.manager_context_absence_pending = False
        state.pending_manager_club_change = None
        state.declined_manager_club_change_key = None
        state.output = {
            "save_instance_id": "career-a",
            "selected_manager_id": 10,
            "manager": {"id": 10, "name": "Manager"},
            "managed_team": {"id": 100, "name": "Old Club"},
            "managed_teams": [{"id": 100, "name": "Old Club"}],
        }
        candidate = {
            "save_instance_id": "career-a",
            "selected_manager_id": 10,
            "manager": {"id": 10, "name": "Manager"},
            "managed_team": {"id": 200, "name": "New Club"},
            "managed_teams": [{"id": 200, "name": "New Club"}],
        }

        self.assertTrue(
            state._queue_manager_club_change_confirmation(state.output, candidate)
        )
        confirmation = state._public_manager_club_change_confirmation()
        self.assertEqual(confirmation["previous_clubs"], [{"id": 100, "name": "Old Club"}])
        self.assertEqual(confirmation["new_clubs"], [{"id": 200, "name": "New Club"}])

        with patch.object(
            state, "_start_manager_team_context_refresh", return_value=True,
        ) as start:
            result = state.confirm_manager_club_change({
                "request_id": confirmation["request_id"], "confirmed": True,
            })

        self.assertEqual(result, {"confirmed": True, "started": True})
        start.assert_called_once_with(candidate)
        self.assertIsNone(state.pending_manager_club_change)

    def test_declined_manager_club_change_is_not_queued_again_until_context_changes(self):
        state = self._state(verified=True)
        state.manager_context_absence_pending = False
        state.pending_manager_club_change = None
        state.declined_manager_club_change_key = None
        current = {
            "save_instance_id": "career-a",
            "selected_manager_id": 10,
            "manager": {"id": 10},
            "managed_team": {"id": 100, "name": "Old Club"},
            "managed_teams": [{"id": 100, "name": "Old Club"}],
        }
        candidate = {
            "save_instance_id": "career-a",
            "selected_manager_id": 10,
            "manager": {"id": 10},
            "managed_team": {"id": 200, "name": "New Club"},
            "managed_teams": [{"id": 200, "name": "New Club"}],
        }
        state.output = current
        state._queue_manager_club_change_confirmation(current, candidate)
        request_id = state._public_manager_club_change_confirmation()["request_id"]

        result = state.confirm_manager_club_change({
            "request_id": request_id, "confirmed": False,
        })

        self.assertEqual(result, {"confirmed": False, "started": False})
        self.assertIsNone(state.pending_manager_club_change)
        self.assertTrue(state._queue_manager_club_change_confirmation(current, candidate))
        self.assertIsNone(state.pending_manager_club_change)

    def test_habit_training_writes_preferred_move_only_when_week_is_due(self):
        state = self._state(verified=True)
        state.club_profile = None
        state.club_profiles = {
            100: {"players": [{"id": 10, "name": "A", "address": "0x1234"}]},
        }
        state.attribute_growth_hook = SimpleNamespace(
            sync=MagicMock(return_value={"installed": False, "error": None}),
        )
        focus = {
            "id": "habit-1", "status": "active", "focus_type": "preferred_move",
            "player_id": 10, "player_name": "A", "preferred_move_bit": 13,
            "operation": "learn", "expires_on": "2026-07-26",
        }

        with (
            patch("fm_odds_web.active_training_focuses", return_value=[focus]),
            patch("fm_odds_web.due_coaching_license_focuses", return_value=[]),
            patch("fm_odds_web.update_player_preferred_move") as update_move,
        ):
            state._sync_training_focuses("2026-07-25")
        update_move.assert_not_called()

        with (
            patch("fm_odds_web.active_training_focuses", side_effect=[[focus], []]),
            patch("fm_odds_web.due_coaching_license_focuses", return_value=[]),
            patch(
                "fm_odds_web.update_player_preferred_move",
                return_value={"bit": 13, "operation": "learn", "_original_mask": 0},
            ) as update_move,
            patch("fm_odds_web.complete_habit_training_focus") as complete_focus,
        ):
            status = state._sync_training_focuses("2026-07-26")

        update_move.assert_called_once_with(10, "0x1234", 13, "learn")
        complete_focus.assert_called_once()
        self.assertEqual(status["completed_habit_focus_ids"], ["habit-1"])

    def test_habit_training_uses_structured_already_satisfied_state(self):
        from tools.preferred_moves import PreferredMoveStateError

        state = self._state(verified=True)
        state.club_profile = None
        state.club_profiles = {
            100: {"players": [{"id": 10, "name": "A", "address": "0x1234"}]},
        }
        state.attribute_growth_hook = SimpleNamespace(
            sync=MagicMock(return_value={"installed": False, "error": None}),
        )
        focus = {
            "id": "habit-1", "status": "active", "focus_type": "preferred_move",
            "player_id": 10, "player_name": "A", "preferred_move_bit": 13,
            "operation": "learn", "expires_on": "2026-07-26",
        }
        with (
            patch("fm_odds_web.active_training_focuses", side_effect=[[focus], []]),
            patch("fm_odds_web.due_coaching_license_focuses", return_value=[]),
            patch(
                "fm_odds_web.update_player_preferred_move",
                side_effect=PreferredMoveStateError("learn"),
            ),
            patch("fm_odds_web.complete_habit_training_focus") as complete_focus,
        ):
            status = state._sync_training_focuses("2026-07-26")

        complete_focus.assert_called_once_with(
            "habit-1", "2026-07-26", {"already_satisfied": True},
        )
        self.assertEqual(status["completed_habit_focus_ids"], ["habit-1"])

    @staticmethod
    def _attribute_training_state() -> LocalOddsState:
        state = RefreshOptimizationTests._state(verified=True)
        state.club_profile = None
        state.club_profiles = {
            100: {
                "players": [{
                    "id": 10, "name": "A", "address": "0x1234",
                    "attributes": {"身体": {"速度": 17}}, "ca": 150, "pa": 180,
                }],
            },
        }
        state._live_player_profile = lambda player_id, _team_id=0: (
            state.club_profiles[100],
            next(
                (
                    player for player in state.club_profiles[100]["players"]
                    if int(player.get("id") or 0) == int(player_id)
                ),
                None,
            ),
        )
        state.attribute_growth_hook = SimpleNamespace(
            sync=MagicMock(return_value={"installed": False, "error": None}),
        )
        return state

    @staticmethod
    def _attribute_training_focus() -> dict:
        return {
            "id": "attribute-1", "status": "active", "focus_type": "attribute",
            "player_id": 10, "player_name": "A", "attribute_key": "身体:速度",
            "attribute_id": 0x35, "initial_attribute": 17, "target_attribute": 18,
        }

    def test_attribute_training_does_not_write_before_points_are_due(self):
        state = self._attribute_training_state()
        focus = self._attribute_training_focus()
        with (
            patch("fm_odds_web.active_training_focuses", return_value=[focus]),
            patch("fm_odds_web.due_coaching_license_focuses", return_value=[]),
            patch("fm_odds_web.advance_training_focuses", return_value=[]),
            patch("fm_odds_web.read_player_training_attributes", return_value={"attribute-1": 17}),
            patch("fm_odds_web.develop_player") as develop,
        ):
            status = state._sync_training_focuses("2026-07-26")
        develop.assert_not_called()
        self.assertEqual(status["completed_attribute_focus_ids"], [])

    def test_attribute_training_reuses_same_day_sync_result(self):
        state = self._attribute_training_state()
        focus = self._attribute_training_focus()
        with (
            patch("fm_odds_web.active_training_focuses", return_value=[focus]),
            patch("fm_odds_web.due_coaching_license_focuses", return_value=[]),
            patch("fm_odds_web.advance_training_focuses", return_value=[]),
            patch(
                "fm_odds_web.read_player_training_attributes",
                return_value={"attribute-1": 17},
            ) as read_attributes,
        ):
            first = state._sync_training_focuses("2026-07-26")
            second = state._sync_training_focuses("2026-07-26")

        self.assertEqual(read_attributes.call_count, 1)
        self.assertEqual(first["pending_player_ids"], second["pending_player_ids"])

    def test_due_attribute_training_writes_once_and_updates_cached_player(self):
        state = self._attribute_training_state()
        focus = self._attribute_training_focus()
        result = {
            "attributes": {"身体": {"速度": 18}}, "ca": 151, "pa": 180,
            "attribute_after": 18, "_original_raw": b"raw",
            "_original_ca": 150, "_original_pa": 180,
        }
        with (
            patch("fm_odds_web.active_training_focuses", side_effect=[[focus], [focus]]),
            patch("fm_odds_web.due_coaching_license_focuses", return_value=[]),
            patch("fm_odds_web.advance_training_focuses", return_value=[focus]),
            patch("fm_odds_web.read_player_training_attributes", return_value={"attribute-1": 17}),
            patch("fm_odds_web.develop_player", return_value=result) as develop,
            patch(
                "fm_odds_web.complete_attribute_training_focus",
                return_value={"status": "completed"},
            ) as complete,
        ):
            status = state._sync_training_focuses("2026-07-26")
        develop.assert_called_once_with(
            10, "0x1234", "身体:速度", 17, "training", 1,
            allow_ca_over_pa=False,
        )
        complete.assert_called_once()
        player = state.club_profiles[100]["players"][0]
        self.assertEqual((player["attributes"]["身体"]["速度"], player["ca"]), (18, 151))
        self.assertEqual(status["completed_attribute_focus_ids"], ["attribute-1"])

    def test_training_run_requires_explicit_ca_over_pa_confirmation(self):
        state = self._attribute_training_state()
        state.memory_lock = threading.RLock()
        state.output["game_date"] = "2026-07-26"
        player = state.club_profiles[100]["players"][0]
        player["attributes"] = {"身体": {"速度": 10}}
        with (
            patch.object(state, "_bind_current_save"),
            patch(
                "fm_odds_web.assert_training_available",
                return_value=(
                    {"id": "facility-1", "sku": "treadmill"},
                    {"attributes": ["身体:速度"], "session_fee": 0},
                ),
            ),
            patch(
                "fm_odds_web.estimate_player_training_ca",
                return_value={
                    "attribute_before": 10, "ca_cost": 3,
                    "ca_before": 148, "pa": 150,
                    "model": "fm24-verified", "verified": True,
                },
            ),
            patch("fm_odds_web.record_training_focus") as record,
        ):
            with self.assertRaisesRegex(ValueError, "可能触发属性重新分配"):
                state.training_run({
                    "facility_id": "facility-1", "player_id": 10,
                    "operation": "attribute", "attribute_key": "身体:速度",
                    "duration_mode": "until_increase", "free_purchase": True,
                })
        record.assert_not_called()

    def test_training_run_persists_confirmed_ca_over_pa_risk(self):
        state = self._attribute_training_state()
        state.memory_lock = threading.RLock()
        state.output["game_date"] = "2026-07-26"
        state.attribute_growth_hook.status = MagicMock(return_value={"installed": False})
        player = state.club_profiles[100]["players"][0]
        player["attributes"] = {"身体": {"速度": 10}}
        focus = {"id": "focus-1", "multiplier": 3, "expires_on": None,
                 "duration_mode": "until_increase", "duration_days": None,
                 "required_points": 14, "progress_points": 0}
        with (
            patch.object(state, "_bind_current_save"),
            patch(
                "fm_odds_web.assert_training_available",
                return_value=(
                    {"id": "facility-1", "sku": "treadmill"},
                    {"attributes": ["身体:速度"], "session_fee": 0},
                ),
            ),
            patch(
                "fm_odds_web.estimate_player_training_ca",
                return_value={
                    "attribute_before": 10, "ca_cost": 3,
                    "ca_before": 148, "pa": 150,
                    "model": "fm24-verified", "verified": True,
                },
            ),
            patch(
                "fm_odds_web.record_training_focus",
                return_value=(focus, {"id": "session-1"}),
            ) as record,
            patch("fm_odds_web.public_training_ground", return_value={}),
            patch("fm_odds_web.public_economy", return_value={}),
        ):
            response = state.training_run({
                "facility_id": "facility-1", "player_id": 10,
                "operation": "attribute", "attribute_key": "身体:速度",
                "duration_mode": "until_increase", "free_purchase": True,
                "confirm_ca_over_pa": True,
            })

        record.assert_called_once_with(
            "facility-1", 10, "A", "2026-07-26", "身体:速度", 0x35,
            "until_increase", 10, allow_ca_over_pa=True,
            attribute_kind="visible", direction=1,
            player_address="0x1234", player_team_id=None,
            player_team_name=None, player_team_type="club",
        )
        self.assertEqual(response["training_ground"]["growth_hook"], {"installed": False})

    def test_training_run_retry_returns_existing_submission_without_charge(self):
        state = self._attribute_training_state()
        state.memory_lock = threading.RLock()
        state.output["game_date"] = "2026-07-26"
        state.attribute_growth_hook.status = MagicMock(return_value={"installed": False})
        focus = {
            "id": "focus-1", "focus_type": "attribute", "status": "active",
            "facility_id": "facility-1", "player_id": 10,
            "attribute_key": "身体:速度", "duration_mode": "until_increase",
        }
        session = {"id": "session-1", "focus_id": "focus-1"}
        with (
            patch.object(state, "_bind_current_save"),
            patch(
                "fm_odds_web.find_training_submission",
                return_value=(focus, session),
            ),
            patch("fm_odds_web.assert_training_available") as available,
            patch("fm_odds_web.charge_combined_funds") as charge,
            patch("fm_odds_web.public_training_ground", return_value={}),
            patch("fm_odds_web.public_economy", return_value={}),
        ):
            response = state.training_run({
                "facility_id": "facility-1", "player_id": 10,
                "operation": "attribute", "attribute_key": "身体:速度",
                "duration_mode": "until_increase",
                "client_submission_id": "training-request-1",
            })

        self.assertTrue(response["duplicate"])
        self.assertEqual(response["focus"]["id"], "focus-1")
        available.assert_not_called()
        charge.assert_not_called()

    def test_until_20_attribute_training_advances_without_releasing_focus(self):
        state = self._attribute_training_state()
        focus = {**self._attribute_training_focus(), "duration_mode": "until_20", "target_attribute": 20}
        result = {
            "attributes": {"身体": {"速度": 18}}, "ca": 151, "pa": 180,
            "attribute_after": 18, "_original_raw": b"raw",
            "_original_ca": 150, "_original_pa": 180,
        }
        with (
            patch("fm_odds_web.active_training_focuses", side_effect=[[focus], [focus]]),
            patch("fm_odds_web.due_coaching_license_focuses", return_value=[]),
            patch("fm_odds_web.advance_training_focuses", return_value=[focus]),
            patch("fm_odds_web.read_player_training_attributes", return_value={"attribute-1": 17}),
            patch("fm_odds_web.develop_player", return_value=result),
            patch(
                "fm_odds_web.complete_attribute_training_focus",
                return_value={"status": "active", "initial_attribute": 18},
            ),
        ):
            status = state._sync_training_focuses("2026-07-26")
        self.assertEqual(status["completed_attribute_focus_ids"], [])
        self.assertEqual(status["advanced_attribute_focus_ids"], ["attribute-1"])

    def test_attribute_training_restores_memory_when_completion_record_fails(self):
        state = self._attribute_training_state()
        focus = self._attribute_training_focus()
        result = {
            "attributes": {"身体": {"速度": 18}}, "ca": 151, "pa": 180,
            "attribute_after": 18, "_original_raw": b"raw",
            "_original_ca": 150, "_original_pa": 180,
        }
        with (
            patch("fm_odds_web.active_training_focuses", side_effect=[[focus], [focus]]),
            patch("fm_odds_web.due_coaching_license_focuses", return_value=[]),
            patch("fm_odds_web.advance_training_focuses", return_value=[focus]),
            patch("fm_odds_web.read_player_training_attributes", return_value={"attribute-1": 17}),
            patch("fm_odds_web.develop_player", return_value=result),
            patch("fm_odds_web.complete_attribute_training_focus", side_effect=OSError("disk full")),
            patch("fm_odds_web.restore_player_development") as restore,
        ):
            status = state._sync_training_focuses("2026-07-26")
        restore.assert_called_once_with(10, "0x1234", b"raw", 150, 180)
        self.assertIn("disk full", status["progress_error"])
        self.assertEqual(status["completed_attribute_focus_ids"], [])

    def test_live_match_skips_refresh_priority_settlement(self):
        state = self._state(verified=True)
        state.output = {"save_instance_id": "save-123"}

        with (
            patch(
                "fm_odds_web.read_match_engine_state",
                return_value={"known": True, "active": True},
            ),
            patch(
                "fm_odds_web.read_game_clock",
                return_value={"date": "2028-06-06", "minutes": 900},
            ) as read_clock,
            patch(
                "fm_odds_web.pending_due_result_keys",
                return_value={("2028-06-06", 100, 10, 20)},
            ),
            patch("fm_odds_web.probe_live_completed_results") as probe,
        ):
            result = state._settle_results_before_refresh()

        self.assertEqual(result["settled"], 0)
        read_clock.assert_called_once()
        probe.assert_not_called()

    def test_first_connection_skips_priority_settlement_without_snapshot(self):
        state = self._state(verified=False)
        state.output = {}

        with patch("fm_odds_web.read_game_clock") as read_clock:
            result = state._settle_results_before_refresh()

        self.assertEqual(result, {
            "settled": 0, "settled_records": [], "returned": 0.0,
        })
        read_clock.assert_not_called()

    def test_unknown_match_session_does_not_probe_same_day_result(self):
        state = self._state(verified=True)
        state.output = {"save_instance_id": "save-123"}
        key = ("2028-06-06", 100, 10, 20)

        with (
            patch(
                "fm_odds_web.read_game_clock",
                return_value={"date": "2028-06-06", "minutes": 1000},
            ),
            patch(
                "fm_odds_web.pending_due_result_keys",
                return_value={key},
            ),
            patch("fm_odds_web.read_match_engine_state", side_effect=RuntimeError("busy")),
            patch("fm_odds_web.probe_live_completed_results") as probe,
            patch("fm_odds_web.settle_pending_bets") as settle,
        ):
            resolved = state._settle_results_before_refresh()

        self.assertEqual(resolved["settled"], 0)
        probe.assert_not_called()
        settle.assert_not_called()

    def test_active_match_session_does_not_block_previous_day_settlement(self):
        keys = {
            ("2028-06-05", 100, 10, 20),
            ("2028-06-06", 200, 30, 40),
        }

        safe = settlement_keys_safe_for_engine_state(
            keys, "2028-06-06", {"known": True, "active": True},
        )

        self.assertEqual(safe, {("2028-06-05", 100, 10, 20)})

    def test_unknown_match_session_only_blocks_same_day_settlement(self):
        keys = {
            ("2028-06-05", 100, 10, 20),
            ("2028-06-06", 200, 30, 40),
        }

        safe = settlement_keys_safe_for_engine_state(keys, "2028-06-06", None)

        self.assertEqual(safe, {("2028-06-05", 100, 10, 20)})

    def test_unknown_match_session_never_trusts_same_day_result(self):
        historical = ("2028-06-05", 100, 10, 20)
        overdue = ("2028-06-06", 200, 30, 40)

        safe = settlement_keys_safe_for_engine_state(
            {historical, overdue}, "2028-06-06", None,
        )

        self.assertEqual(safe, {historical})

    def test_active_match_session_still_blocks_long_overdue_same_day_settlement(self):
        historical = ("2028-06-05", 100, 10, 20)
        overdue = ("2028-06-06", 200, 30, 40)

        safe = settlement_keys_safe_for_engine_state(
            {historical, overdue}, "2028-06-06",
            {"known": True, "active": True},
        )

        self.assertEqual(safe, {historical})

    def test_inactive_known_match_session_is_not_same_day_completion_evidence(self):
        keys = {("2028-06-06", 200, 30, 40)}

        safe = settlement_keys_safe_for_engine_state(
            keys, "2028-06-06", {"known": True, "active": False},
        )

        self.assertEqual(safe, set())

    def test_manager_account_switch_uses_cached_team_identity_without_settlement(self):
        state = self._state(verified=True)
        state.memory_lock = threading.Lock()
        state.output = {
            "save_instance_id": "save-123",
            "game_date": "2028-06-06",
            "selected_manager_id": 10,
            "manager_options": [
                {"id": 10, "name": "One", "teams": [{"id": 100, "team_type": "club"}]},
                {"id": 20, "name": "Two", "teams": [{"id": 200, "team_type": "club"}]},
            ],
            "manager": {"id": 10},
            "managed_team": {"id": 100},
            "managed_teams": [{"id": 100}],
            "matches": [{"fixture_date": "2028-06-07"}],
        }
        refreshed = {
            "game_date": "2028-06-06",
            "manager": {"id": 20},
            "manager_options": [{"id": 20}],
            "selected_manager_id": 20,
            "manager_selection_required": False,
            "managed_team": {"id": 200, "name": "Team Two"},
            "managed_teams": [{"id": 200, "name": "Team Two"}],
        }

        with (
            patch("fm_odds_web.read_manager_context", return_value=refreshed) as read_context,
            patch.object(state, "_assign_account_scope", return_value="save-123.manager-20"),
            patch("fm_odds_web.remember_active_save_id"),
            patch("fm_odds_web.settle_pending_bets") as settle,
        ):
            state._manager_context_worker(20, 10, "save-123", account_only=True)

        known_session = read_context.call_args.kwargs["known_session"]
        self.assertEqual(known_session["managed_team_refs"][0]["team_id"], 200)
        self.assertEqual(state.output["matches"], [{"fixture_date": "2028-06-07"}])
        self.assertEqual(len(state.output["manager_options"]), 2)
        self.assertEqual(state.output["selected_manager_id"], 20)
        settle.assert_not_called()

    def test_legacy_stale_request_without_save_id_is_repriced(self):
        self.assertTrue(validate_bet_snapshot(
            {"data_version": 2}, "save-current", 3,
        ))


if __name__ == "__main__":
    unittest.main()
