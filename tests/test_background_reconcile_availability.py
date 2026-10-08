from __future__ import annotations

import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

from fm_odds_web import (
    LocalOddsState,
    _post_settings_championship,
    _post_settings_odds,
)
from tools.local_state_services import RefreshCoordinator


ROOT = (Path(__file__).resolve().parents[1] / "src")


class BackgroundReconcileAvailabilityTests(unittest.TestCase):
    def test_verified_background_reconcile_marks_published_state_ready(self):
        state = LocalOddsState.__new__(LocalOddsState)
        state.reconciling = True
        state.refreshing = False
        state.cache_verified = True
        state.save_change_pending = False
        state.output = {"save_instance_id": "career-a"}
        state._has_verified_save = lambda: True
        state._club_legacy_refresh = lambda: SimpleNamespace(
            snapshot=lambda _scope: {},
        )
        state._data_scope_id = lambda _output: "account-a"
        state.refresh_mode = "reconcile"
        state.data_version = 7
        state._betting_ready = lambda: True
        state.pending_manager_club_change = None
        state.manager_context_absence_pending = False
        state.status = "ready"
        state.error = None
        state.save_change_pending = False

        payload = LocalOddsState._refresh_state(state)

        self.assertTrue(payload["background_reconcile_ready"])
        self.assertTrue(payload["betting_ready"])

    def test_unverified_or_save_change_reconcile_remains_blocking(self):
        for verified, pending in ((False, False), (True, True)):
            host = SimpleNamespace(
                lock=threading.RLock(), output={"save_instance_id": "career-a"},
                refreshing=False, reconciling=True, cache_verified=verified,
                save_change_pending=pending, data_version=1, status="working",
                error=None, refresh_reason="background_reconcile",
                refresh_stage="generate_odds", refresh_progress={},
                _refresh_progress_updated_at=0.0, _refresh_stage_started_at=0.0,
                connection_requested=False,
            )
            payload = RefreshCoordinator(host)._contended_status(1.0)
            with self.subTest(verified=verified, pending=pending):
                self.assertFalse(payload["background_reconcile_ready"])
                self.assertTrue(payload["status_snapshot_delayed"])

    def test_frontend_separates_page_availability_from_refresh_controls(self):
        source = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
        helper = source.split("function refreshBlocksPublishedState", 1)[1].split(
            "function liveRefreshProgressText", 1,
        )[0]
        self.assertIn("state?.reconciling && !state?.background_reconcile_ready", helper)
        home = source.split("function homeViewModel()", 1)[1].split(
            "function homeRenderStateSignature", 1,
        )[0]
        self.assertIn("const oddsBusy = refreshBlocksPublishedState();", home)
        self.assertIn("refreshControlBusy", home)
        controls = source.split("function renderRefreshControls()", 1)[1].split(
            "function syncBettingPageStatus", 1,
        )[0]
        self.assertIn("app.state.refreshing || app.state.reconciling", controls)
        betting = source.split("function syncBettingPageStatus()", 1)[1].split(
            "function homeManagerRecord", 1,
        )[0]
        self.assertIn("const oddsBusy = refreshBlocksPublishedState();", betting)
        for marker in (
            "function renderLeague()", "function renderChampionship()",
            "function syncClubPageStatus(status, club)",
        ):
            block = source.split(marker, 1)[1].split("\n}\n", 1)[0]
            self.assertIn("refreshBlocksPublishedState()", block)

        settings = source.split("function syncSettingsDialogStatus()", 1)[1].split(
            "function setSettingsMutationBusy", 1,
        )[0]
        self.assertIn(
            "app.state?.refreshing || app.state?.reconciling || app.state?.connection_pending",
            settings,
        )
        self.assertNotIn("refreshBlocksPublishedState()", settings)

    def test_scope_and_manager_mutations_remain_blocked_during_reconcile(self):
        state = LocalOddsState.__new__(LocalOddsState)
        state.lock = threading.RLock()
        state.refreshing = False
        state.reconciling = True

        with self.assertRaisesRegex(ValueError, "切换账户"):
            state.select_saved_account({"scope_id": "account-b"})
        with self.assertRaisesRegex(ValueError, "确认经理状态"):
            state.confirm_unemployed_manager({"confirmed": True})

    def test_odds_settings_remain_blocked_during_reconcile(self):
        state = SimpleNamespace(
            lock=threading.RLock(), refreshing=False, reconciling=True,
        )
        handler = SimpleNamespace(state=state)

        with self.assertRaisesRegex(ValueError, "冠军盘设置"):
            _post_settings_championship(handler, {"enabled": True}, {})
        with self.assertRaisesRegex(ValueError, "盘口读取设置"):
            _post_settings_odds(handler, {"scope": "all", "days": 7}, {})


if __name__ == "__main__":
    unittest.main()
