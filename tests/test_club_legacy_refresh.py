from __future__ import annotations

import threading
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import Mock, patch

import fm_odds_web as web
from tools.local_state_services import ClubLegacyRefreshCoordinator


class ClubLegacyRefreshTests(unittest.TestCase):
    def state(self):
        state = web.LocalOddsState.__new__(web.LocalOddsState)
        state.lock = threading.RLock()
        state.memory_lock = threading.Lock()
        state.output = {
            "save_instance_id": "career-a", "account_scope_id": "account-a",
            "account_scope_manager_id": 7, "selected_manager_id": 7,
            "manager": {"id": 7}, "game_layout": "fm26", "game_date": "2030-09-01",
            "managed_team": {"id": 10, "team_type": "club"},
            "managed_teams": [{"id": 10, "team_type": "club"}],
        }
        state.club_profiles = {}
        state.club_profile = None
        state.club_loading_profiles = {}
        state.club_refreshing = True
        state.club_refresh_generation = 1
        state.club_error = None
        state.club_legacy_error = None
        state.refreshing = state.reconciling = state.world_club_scanning = False
        state.deferred_memory_job_after_club = {
            "kind": "refresh", "reason": "schedule_change", "full": False,
        }
        state._club_memory_read_lock = lambda: state.memory_lock
        state._cache_club_funds_from_profiles = Mock()
        state._apply_default_medical_treatments = Mock(return_value={})
        state._process_due_medical_treatments = Mock()
        state._process_language_learning = Mock()
        state._refresh_world_watch_players = Mock(return_value=[])
        state.refresh_async = Mock(return_value=True)
        return state

    def profile(self):
        return {"team": {"id": 10, "team_type": "club"}, "players": [{"id": 42}]}

    def isolated_worker(self, stack, profile):
        # No real accounts, file writes, FM reads or medical effects in these tests.
        for name, value in (
            ("read_club_profile", profile),
            ("read_game_clock", {"date": "2030-09-01"}),
            ("welfare_status", {}), ("apply_player_aliases", None),
            ("set_active_save_id", None),
        ):
            stack.enter_context(patch.object(web, name, return_value=value))

    def test_team_ready_and_deferred_odds_resume_before_slow_archive(self):
        state = self.state()
        entered, release = threading.Event(), threading.Event()

        def archive(*args, **kwargs):
            entered.set()
            self.assertTrue(release.wait(3), "test did not release archive")

        with ExitStack() as stack:
            self.isolated_worker(stack, self.profile())
            stack.enter_context(patch.object(web, "sync_club_profile", side_effect=archive))
            worker = threading.Thread(target=state._club_refresh_worker)
            worker.start()
            try:
                self.assertTrue(entered.wait(3))
                self.assertFalse(state.club_refreshing)
                self.assertEqual(state.club_profile["players"][0]["id"], 42)
                self.assertFalse(state.memory_lock.locked())
                state.refresh_async.assert_called_once()
                state._process_language_learning.assert_called_once()
                self.assertTrue(state._club_legacy_refresh().snapshot("account-a")["refreshing"])
                # Archive input is detached from the now publicly mutable profile.
                state.club_profile["players"][0]["id"] = 99
                self.assertEqual(state._club_legacy_refresh().active["profiles"][10]["players"][0]["id"], 42)
            finally:
                release.set()
                worker.join(3)
            self.assertFalse(worker.is_alive())
        self.assertEqual(state._club_legacy_refresh().snapshot("account-a")["revision"], 1)

    def test_archive_failure_is_independent_of_successful_team_read(self):
        state = self.state()
        with ExitStack() as stack:
            self.isolated_worker(stack, self.profile())
            stack.enter_context(patch.object(web, "sync_club_profile", side_effect=RuntimeError("archive failed")))
            state._club_refresh_worker()
        self.assertFalse(state.club_refreshing)
        self.assertIsNone(state.club_error)
        self.assertEqual(state.club_progress, 100)
        self.assertEqual(state.club_legacy_error, "archive failed")
        self.assertFalse(state._club_legacy_refresh().busy())

    def test_scope_change_before_publication_discards_old_team_and_archive(self):
        state = self.state()

        def change_scope(*args, **kwargs):
            state.output = {**state.output, "save_instance_id": "career-b"}

        state._process_language_learning.side_effect = change_scope
        with ExitStack() as stack:
            self.isolated_worker(stack, self.profile())
            archive = stack.enter_context(patch.object(web, "sync_club_profile"))
            state._club_refresh_worker()
        self.assertFalse(state.club_refreshing)
        self.assertEqual(state.club_profiles, {})
        archive.assert_not_called()

    def test_queue_coalesces_latest_and_old_completion_cannot_publish_error(self):
        host = SimpleNamespace(lock=threading.RLock(), key=1, club_legacy_error=None)
        host._club_legacy_context_key = lambda: host.key
        service = ClubLegacyRefreshCoordinator(host)
        called = []

        def sync(job, current):
            called.append(job["key"])
            if job["key"] == 1:
                host.key = 2
                service.enqueue({"key": 2, "scope_id": "a"})
                host.key = 3
                service.enqueue({"key": 3, "scope_id": "b"})
                service.drain()  # Cannot start a second consumer.
                self.assertFalse(current())
                raise RuntimeError("obsolete failure")
            return []

        host._sync_club_legacy_job = sync
        service.enqueue({"key": 1, "scope_id": "a"})
        service.drain()
        self.assertEqual(called, [1, 3])
        self.assertEqual(service.snapshot("b")["revision"], 1)
        self.assertIsNone(host.club_legacy_error)
        self.assertFalse(service.busy())

    def test_world_watch_discards_read_after_context_changes_and_releases_lock(self):
        state = self.state()
        key = state._club_legacy_context_key()
        is_current = lambda: key == state._club_legacy_context_key()

        def read(uid):
            self.assertTrue(state.memory_lock.locked())
            state.club_refresh_generation += 1
            return {"id": uid}

        with patch.object(web, "load_public_club_legacy", return_value={
            "players": [{"id": 42, "favorite": True}, {"id": 43, "favorite": True}],
        }), patch.object(web, "read_world_player_profile", side_effect=read) as reader, patch.object(
            web, "sync_world_watch_player",
        ) as sync:
            errors = web.LocalOddsState._refresh_world_watch_players(
                state, "career-a", "account-a", "2030-09-01", "fm26", is_current=is_current,
            )
        self.assertEqual(errors, [])
        reader.assert_called_once_with(42)
        sync.assert_not_called()
        self.assertFalse(state.memory_lock.locked())

    def test_snapshot_is_scope_bound_and_job_rebinds_its_account(self):
        state = self.state()
        service = state._club_legacy_refresh()
        job = {
            "key": state._club_legacy_context_key(), "scope_id": "account-a",
            "career_id": "career-a", "game_layout": "fm26", "game_date": "2030-09-01",
            "season_start": "", "season_end": "", "profiles": {10: self.profile()},
        }
        service.enqueue(job)
        self.assertFalse(service.snapshot("account-b")["refreshing"])
        with patch.object(web, "set_active_save_id") as bind, patch.object(web, "sync_club_profile"):
            service.drain()
        bind.assert_called_once_with("account-a")

    def test_obsolete_partial_failure_never_replaces_current_profiles(self):
        state = self.state()

        def read(*args, **kwargs):
            kwargs["partial_callback"](self.profile())
            state.output = {**state.output, "save_instance_id": "career-b"}
            raise RuntimeError("obsolete read")

        with patch.object(web, "read_club_profile", side_effect=read), patch.object(
            web, "apply_player_aliases",
        ), patch.object(web, "set_active_save_id"):
            state._club_refresh_worker()
        self.assertEqual(state.club_profiles, {})
        self.assertEqual(state.club_loading_profiles, {})
        self.assertIsNone(state.club_error)
        self.assertFalse(state.club_refreshing)

    def test_legacy_get_reads_archives_without_syncing_profiles(self):
        state = self.state()
        state.club_profiles = {10: self.profile()}
        with patch.object(web, "load_public_club_legacy_index", return_value={"clubs": []}), patch.object(
            web, "load_public_club_legacy", return_value={"players": []},
        ), patch.object(web, "sync_club_profile") as sync:
            self.assertEqual(state.public_club_legacy(0)["current_team_id"], 10)
            self.assertEqual(state.public_club_legacy(10)["players"], [])
        sync.assert_not_called()

    def test_cleanup_and_account_changes_still_reject_active_archive(self):
        state = self.state()
        state.club_refreshing = False
        state._has_verified_save = Mock(side_effect=AssertionError("guard bypassed"))
        state._bind_request_scope = Mock(side_effect=AssertionError("guard bypassed"))
        state._club_legacy_refresh().enqueue({"key": state._club_legacy_context_key(), "scope_id": "account-a"})
        for operation in (
            lambda: state.delete_current_save("DELETE_CURRENT_SAVE_FILE"),
            lambda: state.delete_other_saves("DELETE_OTHER_SAVE_FILES"),
            lambda: state.clear_cache("CLEAR_REBUILDABLE_CACHE"),
            lambda: state.switch_game_version({"key": "fm24"}),
            lambda: state.merge_same_manager_accounts({"confirm": "merge_same_manager_accounts"}),
        ):
            with self.subTest(operation=operation), self.assertRaisesRegex(ValueError, "当前正在"):
                operation()

    def test_reconnect_invalidates_old_archive_before_new_identity_is_published(self):
        state = self.state()
        state._has_verified_save = Mock(return_value=True)
        old_job = {"key": state._club_legacy_context_key(), "scope_id": "account-a"}
        service = state._club_legacy_refresh()
        service.enqueue(old_job)
        with patch.object(web.threading, "Thread"):
            self.assertTrue(state.startup_async())
        self.assertFalse(service.current(old_job))
        self.assertEqual(state.output["save_instance_id"], "career-a")


if __name__ == "__main__":
    unittest.main()
