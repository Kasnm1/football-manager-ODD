from __future__ import annotations

import threading
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fm_odds_web import (
    LIVE_RESULT_CAPTURE_LEAD_MINUTES,
    LIVE_RESULT_CAPTURE_TAIL_MINUTES,
    LocalOddsState,
)
from tools import preview_cup_odds


class StopLoop(BaseException):
    pass


class BackgroundMemoryPollingTests(unittest.TestCase):
    def test_cached_persistent_score_is_accepted_without_archive_consensus(self) -> None:
        key = ("2028-06-06", 100, 10, 20)
        base = {
            "date": key[0], "competition": {"id": key[1]},
            "home_team": {"id": key[2]}, "away_team": {"id": key[3]},
            "home_goals": 2, "away_goals": 1,
        }
        persistent = {
            **base, "result_source": "persistent_season_record",
            "result_address": "0x301000",
        }
        reader = MagicMock()
        context = MagicMock()
        context.__enter__.return_value = (
            "fm.exe", SimpleNamespace(key="fm26"), SimpleNamespace(), reader,
        )
        context.__exit__.return_value = False

        with (
            patch("tools.preview_cup_odds.open_supported_reader", return_value=context),
            patch("tools.preview_cup_odds.decode_date", return_value=SimpleNamespace(
                isoformat=lambda: "2028-06-07",
            )),
            patch("tools.preview_cup_odds.read_layout_game_date_code", return_value=1),
            patch("tools.preview_cup_odds.discover_manager_session", return_value=None),
            patch("tools.preview_cup_odds.read_savegame_identity", return_value={}),
            patch("tools.preview_cup_odds.stable_save_key", return_value="save"),
            patch("tools.preview_cup_odds.resolve_save_identity", return_value="save"),
            patch("tools.preview_cup_odds.layout_save_identity", return_value="save"),
            patch(
                "tools.preview_cup_odds.cached_completed_results",
                return_value=([persistent], True),
            ) as cached,
            patch("tools.preview_cup_odds.recover_fixture_hint_results") as recover_hint,
            patch("tools.preview_cup_odds.recover_persistent_results") as recover_persistent,
            patch("tools.preview_cup_odds.enrich_halftime_results", side_effect=lambda _r, rows, _k: rows),
            patch("tools.preview_cup_odds.remember_completed_results"),
            patch("tools.preview_cup_odds.save_result_history"),
        ):
            observed = preview_cup_odds.read_completed_results({key})

        self.assertEqual(len(observed), 1)
        self.assertEqual(observed[0]["home_goals"], 2)
        self.assertEqual(observed[0]["away_goals"], 1)
        self.assertEqual(observed[0]["result_source"], "persistent_season_record")
        cached.assert_called_once()
        recover_hint.assert_called_once_with(reader, {})
        recover_persistent.assert_not_called()

    def test_result_dedup_prefers_archive_evidence_over_cached_scoreline(self) -> None:
        base = {
            "date": "2028-06-06",
            "competition": {"id": 100},
            "home_team": {"id": 10},
            "away_team": {"id": 20},
            "home_goals": 2,
            "away_goals": 1,
        }
        cached_scoreline = {
            **base,
            "result_source": "basic_scoreline",
        }
        archive_result = {
            **base,
            "result_source": "fixture_result_archive",
            "result_address": "0x201000",
        }

        observed = preview_cup_odds.deduplicate_results([
            cached_scoreline, archive_result,
        ])

        self.assertEqual(observed, [archive_result])

    def test_runtime_sample_reuses_one_validated_reader(self) -> None:
        layout = SimpleNamespace(key="fm26")
        module = SimpleNamespace(base_address=0x100000)
        reader = MagicMock()
        context = MagicMock()
        context.__enter__.return_value = ("fm.exe", layout, module, reader)
        context.__exit__.return_value = False
        clock = {"date": "2028-06-06", "minutes": 900}
        engine = {"known": True, "active": True}

        with (
            patch("tools.preview_cup_odds.open_supported_reader", return_value=context) as opened,
            patch("tools.preview_cup_odds.read_game_clock_from_reader", return_value=clock),
            patch("tools.preview_cup_odds.read_match_engine_state_from_reader", return_value=engine),
        ):
            result = preview_cup_odds.read_game_runtime_state()

        self.assertEqual(result, (clock, engine))
        opened.assert_called_once_with()

    def test_fast_capture_probe_never_falls_back_to_heap_scan(self) -> None:
        key = ("2028-06-06", 100, 10, 20)
        state = {
            "result_region_spans": [],
            "result_addresses": [],
            "result_fingerprints": {},
            "generic_results": [],
            "result_pool_rescanned_at": -100.0,
        }
        reader = MagicMock()

        with (
            patch("tools.preview_cup_odds._runtime_state", return_value=state),
            patch("tools.preview_cup_odds.read_fixture_pool_addresses", return_value=None) as pool,
            patch("tools.preview_cup_odds.scan_result_addresses") as heap_scan,
        ):
            result = preview_cup_odds.probe_live_completed_results_with_reader(
                reader, {key}, allow_expensive_rescan=False,
            )

        self.assertEqual(result["results"], [])
        pool.assert_called_once_with(reader)
        heap_scan.assert_not_called()
        self.assertGreater(state["result_pool_rescanned_at"], 0)

    def test_fast_capture_refreshes_known_addresses_without_scanning_slab(self) -> None:
        key = ("2028-06-06", 100, 10, 20)
        state = {
            "result_region_spans": [(0x200000, 0x4000000)],
            "result_addresses": [0x201000],
            "result_fingerprints": {},
            "generic_results": [],
            "result_pool_rescanned_at": -100.0,
        }
        reader = MagicMock()

        with (
            patch("tools.preview_cup_odds._runtime_state", return_value=state),
            patch(
                "tools.preview_cup_odds.read_fixture_pool_addresses",
                return_value=([], [0x201000], 256),
            ),
            patch(
                "tools.preview_cup_odds.read_result_fingerprints_at_addresses",
                return_value=({}, 0),
            ) as direct_read,
            patch("tools.preview_cup_odds.scan_result_fingerprints_in_spans") as slab_scan,
            patch("tools.preview_cup_odds.result_region_spans", return_value=[]),
        ):
            preview_cup_odds.probe_live_completed_results_with_reader(
                reader, {key}, allow_expensive_rescan=False,
            )

        direct_read.assert_called_once_with(reader, [0x201000])
        slab_scan.assert_not_called()

    def test_cached_result_is_returned_without_native_revalidation(self) -> None:
        key = ("2028-06-06", 100, 10, 20)
        cached = {
            "date": key[0],
            "competition": {"id": key[1]},
            "home_team": {"id": key[2]},
            "away_team": {"id": key[3]},
            "home_goals": 2,
            "away_goals": 1,
            "result_source": "fixture_result_archive",
            "result_address": "0x201000",
        }
        state = {
            "result_region_spans": [],
            "result_addresses": [0x201000],
            "result_fingerprints": {},
            "generic_results": [cached],
            "result_pool_rescanned_at": 100.0,
        }

        with (
            patch("tools.preview_cup_odds._runtime_state", return_value=state),
            patch("tools.preview_cup_odds.monotonic", return_value=101.0),
            patch("tools.preview_cup_odds.enrich_halftime_results", side_effect=lambda _r, rows, _k: rows),
            patch("tools.preview_cup_odds.parse_completed_result") as parser,
        ):
            observed = preview_cup_odds.probe_live_completed_results_with_reader(
                MagicMock(), {key},
            )

        self.assertEqual(observed["results"], [cached])
        parser.assert_not_called()

    def test_changed_live_result_invalidates_same_day_model_cache(self) -> None:
        key = ("2028-06-06", 100, 10, 20)
        result = {
            "date": key[0], "competition": {"id": key[1]},
            "home_team": {"id": key[2]}, "away_team": {"id": key[3]},
            "home_goals": 2, "away_goals": 1,
        }
        state = {
            "result_region_spans": [(0x200000, 0x10000)],
            "result_addresses": [0x201000],
            "result_fingerprints": {0x201000: b"old"},
            "generic_results": [],
            "merged_result_game_date": key[0],
            "result_pool_rescanned_at": 100.0,
        }
        reader = MagicMock()

        with (
            patch("tools.preview_cup_odds._runtime_state", return_value=state),
            patch("tools.preview_cup_odds.monotonic", return_value=101.0),
            patch(
                "tools.preview_cup_odds.read_result_fingerprints_at_addresses",
                return_value=({0x201000: b"new"}, 3),
            ),
            patch(
                "tools.preview_cup_odds.parse_completed_result_from_raw",
                return_value=result,
            ),
            patch(
                "tools.preview_cup_odds.enrich_halftime_results",
                side_effect=lambda _reader, rows, _keys: rows,
            ),
        ):
            observed = preview_cup_odds.probe_live_completed_results_with_reader(
                reader, {key},
            )

        self.assertEqual(observed["results"], [result])
        self.assertIsNone(state["merged_result_game_date"])

    def test_fixture_hint_is_enriched_before_it_overrides_pool_result(self) -> None:
        key = ("2028-06-06", 100, 10, 20)
        hinted = {
            "date": key[0], "competition": {"id": key[1]},
            "home_team": {"id": key[2]}, "away_team": {"id": key[3]},
            "home_goals": 2, "away_goals": 1,
            "result_source": "fixture_result_pointer",
            "result_address": "0x201000",
            "fixture_address": "0x1234",
            "settlement_verified": True,
            "settlement_evidence": "bet_fixture_result_pointer",
        }
        state = {
            "result_region_spans": [],
            "result_addresses": [],
            "result_fingerprints": {},
            "generic_results": [],
            "result_pool_rescanned_at": 100.0,
        }

        def enrich(_reader, rows, _keys):
            return [
                {
                    **item,
                    "half_home_goals": 1,
                    "half_away_goals": 0,
                    "first_scoring_team": "home",
                }
                for item in rows
            ]

        with (
            patch("tools.preview_cup_odds._runtime_state", return_value=state),
            patch("tools.preview_cup_odds.monotonic", return_value=101.0),
            patch(
                "tools.preview_cup_odds.recover_fixture_hint_results",
                return_value=[hinted],
            ),
            patch(
                "tools.preview_cup_odds.enrich_halftime_results",
                side_effect=enrich,
            ),
        ):
            observed = preview_cup_odds.probe_live_completed_results_with_reader(
                MagicMock(), {key}, allow_expensive_rescan=False,
                fixture_hints={key: 0x1234},
            )

        self.assertEqual(observed["results"][0]["half_home_goals"], 1)
        self.assertEqual(observed["results"][0]["half_away_goals"], 0)

    def test_active_match_captures_final_score_without_waiting_for_details(self) -> None:
        state = LocalOddsState.__new__(LocalOddsState)
        state.lock = threading.RLock()
        state.memory_lock = threading.Lock()
        state.connection_requested = True
        state.output = {
            "save_instance_id": "save-123",
            "game_version": "Football Manager 26",
        }
        state.last_connection_clock = {"date": "2028-06-06", "minutes": 900}
        state.match_engine_runtime_state = {"known": True, "active": True}
        state.live_settlement_status = {}
        state.live_captured_result_details = set()
        state.live_captured_result_keys = set()
        key = ("2028-06-06", 100, 10, 20)
        result = {
            "date": key[0], "competition": {"id": key[1]},
            "home_team": {"id": key[2]}, "away_team": {"id": key[3]},
            "home_goals": 2, "away_goals": 1,
        }
        layout = SimpleNamespace(display_name="Football Manager 26")
        reader = MagicMock()
        context = MagicMock()
        context.__enter__.return_value = ("fm.exe", layout, MagicMock(), reader)
        context.__exit__.return_value = False

        with (
            patch("fm_odds_web.sleep", side_effect=[None, StopLoop()]),
            patch("fm_odds_web.set_active_save_id") as set_scope,
            patch("fm_odds_web.pending_detail_capture_keys", return_value={key}) as pending,
            patch(
                "fm_odds_web.pending_result_fixture_hints",
                return_value={key: 0x1234},
            ) as fixture_hints,
            patch("fm_odds_web.open_supported_reader", return_value=context) as open_reader,
            patch(
                "fm_odds_web.probe_live_completed_results_with_reader",
                return_value={"results": [result]},
            ) as probe,
            patch("fm_odds_web.merge_result_history", return_value=[result]),
            patch("fm_odds_web.save_result_history") as save_history,
        ):
            with self.assertRaises(StopLoop):
                state.watch_live_result_capture()

        set_scope.assert_called_once_with("save-123")
        pending.assert_called_once_with(
            "2028-06-06", 900,
            lead_minutes=LIVE_RESULT_CAPTURE_LEAD_MINUTES,
            tail_minutes=LIVE_RESULT_CAPTURE_TAIL_MINUTES,
        )
        open_reader.assert_called_once_with()
        fixture_hints.assert_called_once_with({key})
        probe.assert_called_once_with(
            reader, {key}, allow_expensive_rescan=False,
            fixture_hints={key: 0x1234},
        )
        save_history.assert_called_once()
        self.assertIn(("save-123", *key), state.live_captured_result_keys)
        self.assertFalse(state.live_settlement_status["capture_paused_for_match"])
        self.assertTrue(state.live_settlement_status["capture_match_active"])
        self.assertEqual(state.live_settlement_status["captured_results"], 1)
        self.assertEqual(state.live_settlement_status["captured_details"], 0)

    def test_active_match_pauses_all_background_settlement_reads(self) -> None:
        state = LocalOddsState.__new__(LocalOddsState)
        state.lock = threading.RLock()
        state.connection_requested = True
        state.refreshing = False
        state.reconciling = False
        state.save_change_pending = False
        state.output = {"save_instance_id": "save-123"}
        state.live_match_seen_active = False
        state.live_settlement_status = {}

        due = {("2028-06-05", 100, 10, 20)}
        with (
            patch("fm_odds_web.sleep", side_effect=[None, StopLoop()]),
            patch("fm_odds_web.set_active_save_id"),
            patch("fm_odds_web.pending_result_keys", return_value=due),
            patch("fm_odds_web.pending_due_result_keys", return_value=due),
            patch(
                "fm_odds_web.read_game_clock",
                return_value={"date": "2028-06-06", "minutes": 900},
            ),
            patch(
                "fm_odds_web.read_match_engine_state",
                return_value={"known": True, "active": True},
            ),
            patch("fm_odds_web.probe_live_completed_results") as probe,
        ):
            with self.assertRaises(StopLoop):
                state.watch_live_settlement()

        probe.assert_not_called()
        self.assertTrue(state.live_settlement_status["settlement_paused_for_match"])

    def test_captured_result_cannot_bypass_scheduled_finish_time(self) -> None:
        state = LocalOddsState.__new__(LocalOddsState)
        state.lock = threading.RLock()
        state.connection_requested = True
        state.refreshing = False
        state.reconciling = False
        state.save_change_pending = False
        state.output = {"save_instance_id": "save-123"}
        state.live_match_seen_active = True
        state.live_captured_result_keys = set()
        state.live_settlement_status = {"missing_results": 7}
        state.live_settlement_recovery_attempts = {
            ("2028-06-05", 99, 1, 2): 3,
        }
        key = ("2028-06-06", 100, 10, 20)
        state.live_captured_result_keys.add(("save-123", *key))

        with (
            patch("fm_odds_web.sleep", side_effect=[None, StopLoop()]),
            patch.object(state, "_data_scope_id", return_value="account-1"),
            patch("fm_odds_web.set_active_save_id"),
            patch("fm_odds_web.pending_result_keys", return_value={key}),
            patch("fm_odds_web.pending_due_result_keys", return_value=set()),
            patch(
                "fm_odds_web.read_game_clock",
                return_value={"date": key[0], "minutes": 1200},
            ),
            patch(
                "fm_odds_web.read_match_engine_state",
                return_value={"known": True, "active": False},
            ),
            patch("fm_odds_web.probe_live_completed_results") as probe,
        ):
            with self.assertRaises(StopLoop):
                state.watch_live_settlement()

        probe.assert_not_called()
        self.assertEqual(state.live_settlement_status["due_matches"], 0)
        self.assertEqual(state.live_settlement_status["missing_results"], 0)
        self.assertEqual(state.live_settlement_recovery_attempts, {})

    def test_active_match_pauses_identity_and_schedule_maintenance(self) -> None:
        state = LocalOddsState.__new__(LocalOddsState)
        state.lock = threading.RLock()
        state.memory_lock = threading.Lock()
        state.connection_requested = True
        state.cache_verified = True
        state.output = {"save_instance_id": "save-123"}
        state.live_match_seen_active = False
        state.match_engine_runtime_state = {"known": False, "active": False}
        state._note_connection_liveness = MagicMock()
        state._note_betting_match_engine_state = MagicMock()
        state._persist_youth_generation_progress = MagicMock()
        state._read_save_identity_when_idle = MagicMock()
        state._probe_schedule_change = MagicMock()
        state._sync_fixture_items_for_clock = MagicMock()

        runtime = (
            {"date": "2028-06-06", "minutes": 900},
            {"known": True, "active": True},
        )
        with (
            patch("fm_odds_web.read_game_runtime_state", return_value=runtime),
            patch("fm_odds_web.sleep", side_effect=[None, StopLoop()]),
        ):
            with self.assertRaises(StopLoop):
                state.watch_game_date()

        state._persist_youth_generation_progress.assert_not_called()
        state._read_save_identity_when_idle.assert_not_called()
        state._probe_schedule_change.assert_not_called()
        state._sync_fixture_items_for_clock.assert_any_call(
            {"date": "2028-06-06", "minutes": 900},
        )
        self.assertTrue(state.live_match_seen_active)

    def test_unchanged_game_clock_does_not_start_wall_clock_full_reconcile(self) -> None:
        state = LocalOddsState.__new__(LocalOddsState)
        state.lock = threading.RLock()
        state.connection_requested = True
        state.cache_verified = True
        state.output = {
            "save_instance_id": "save-123",
            "full_verified_at": "2020-01-01T00:00:00",
            "matches": [],
        }
        state.last_connection_clock = {
            "date": "2028-06-06", "time": "15:00", "minutes": 900,
        }
        state.last_auto_refresh_clock = ("2028-06-06", 900)
        state.auto_reconcile_last_attempt_at = 0.0
        state.odds_auto_refresh_seconds = 8
        state.refreshing = False
        state.reconciling = False
        state.club_refreshing = False
        state.save_change_pending = False
        state.refresh_cancel_on_clock_change = False
        state.refresh_cancel_event = None
        state.match_engine_runtime_state = {"known": True, "active": False}
        state.live_match_seen_active = False
        state._has_verified_save = MagicMock(return_value=True)
        state._run_memory_operation_if_idle = MagicMock(return_value=(
            {"date": "2028-06-06", "time": "15:00", "minutes": 900},
            {"known": True, "active": False},
        ))
        state._record_background_probe_metric = MagicMock()
        state._note_connection_liveness = MagicMock()
        state._sync_fixture_items_for_clock = MagicMock()
        state._note_betting_match_engine_state = MagicMock()
        state._process_monthly_club_dividends_if_due = MagicMock()
        state._reconcile_youth_generation_if_due = MagicMock()
        state._start_reconcile = MagicMock()

        with (
            patch("fm_odds_web.monotonic", return_value=2000.0),
            patch("fm_odds_web.sleep", side_effect=StopLoop()),
        ):
            with self.assertRaises(StopLoop):
                state.watch_game_date()

        state._start_reconcile.assert_not_called()

    def test_clock_monitor_prioritizes_clock_read_while_full_scan_owns_memory(self) -> None:
        state = LocalOddsState.__new__(LocalOddsState)
        state.lock = threading.RLock()
        state.memory_lock = threading.Lock()
        state.memory_lock.acquire()
        state.connection_requested = True
        state.cache_verified = True
        state.output = {"save_instance_id": "save-123"}
        state.last_connection_clock = None
        state.last_auto_refresh_clock = None
        state.refresh_cancel_on_clock_change = False
        state.refresh_cancel_event = None
        state._note_connection_liveness = MagicMock()

        try:
            with (
                patch("fm_odds_web.read_game_runtime_state") as read_runtime,
                patch(
                    "fm_odds_web.read_game_clock",
                    return_value={
                        "date": "2028-06-06", "time": "15:00", "minutes": 900,
                    },
                ) as read_clock,
                patch("fm_odds_web.sleep", side_effect=StopLoop()),
            ):
                with self.assertRaises(StopLoop):
                    state.watch_game_date()
        finally:
            state.memory_lock.release()

        read_runtime.assert_not_called()
        read_clock.assert_called_once_with()
        state._note_connection_liveness.assert_called_once_with(
            {"date": "2028-06-06", "time": "15:00", "minutes": 900}, True,
        )

    def test_priority_clock_read_cancels_refresh_when_clock_changes(self) -> None:
        state = LocalOddsState.__new__(LocalOddsState)
        state.lock = threading.RLock()
        state.memory_lock = threading.Lock()
        state.memory_lock.acquire()
        state.connection_requested = True
        state.cache_verified = True
        state.output = {"save_instance_id": "save-123"}
        state.last_connection_clock = None
        state.last_auto_refresh_clock = None
        state.refresh_cancel_on_clock_change = True
        state.refresh_cancel_event = threading.Event()
        state.status = "正在刷新盘口..."
        state._note_connection_liveness = MagicMock()

        clocks = [
            {"date": "2028-06-06", "time": "15:00", "minutes": 900},
            {"date": "2028-06-06", "time": "15:01", "minutes": 901},
        ]
        try:
            with (
                patch("fm_odds_web.read_game_runtime_state") as read_runtime,
                patch("fm_odds_web.read_game_clock", side_effect=clocks),
                patch("fm_odds_web.sleep", side_effect=[None, StopLoop()]),
            ):
                with self.assertRaises(StopLoop):
                    state.watch_game_date()
        finally:
            state.memory_lock.release()

        read_runtime.assert_not_called()
        self.assertTrue(state.refresh_cancel_event.is_set())
        self.assertEqual(state.status, "游戏时间变化，正在停止本次刷新...")


if __name__ == "__main__":
    unittest.main()
