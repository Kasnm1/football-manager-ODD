from __future__ import annotations

import unittest
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fm_odds_web import LocalOddsState


ROOT = Path(__file__).resolve().parents[1]


class HomeDesktopTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
        cls.script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
        cls.css = (ROOT / "web" / "app.css").read_text(encoding="utf-8")

    def test_home_is_the_default_page_and_exposes_connection_action(self) -> None:
        self.assertIn('id="page-home"', self.html)
        self.assertIn('id="connect-save-button"', self.html)
        self.assertIn('showPage("home");', self.script)
        self.assertIn('page: "home"', self.script)

    def test_home_render_signature_skips_unchanged_dom_but_keeps_progress_live(self) -> None:
        signature = self.script.split("function homeRenderStateSignature(view)", 1)[1].split(
            "function renderHome()", 1,
        )[0]
        render = self.script.split("function renderHome()", 1)[1].split(
            "async function connectCurrentSave()", 1,
        )[0]

        self.assertIn("app.state?.data_scope_id", signature)
        self.assertIn("app.state?.data_version", signature)
        self.assertIn("view.connection.status", signature)
        self.assertIn("view.title, view.detail, view.oddsBusy, view.refreshControlBusy, view.clubBusy", signature)
        self.assertIn("Number(total.played || 0)", signature)
        self.assertIn("Number(total.win_rate || 0)", signature)
        self.assertIn("app.homeManagerRecordLoadFailed", signature)
        self.assertIn("view.data.save_name", signature)
        self.assertLess(
            render.index("syncHomeConnectionProgress(connecting);"),
            render.index("if (app.homeRenderSignature === signature) return;"),
        )
        self.assertIn("app.homeRenderSignature = signature;", render)
        self.assertGreaterEqual(self.script.count("app.homeRenderSignature = null;"), 4)

    def test_home_connection_accessibility_contract(self) -> None:
        render = self.script.split("function renderHome()", 1)[1].split(
            "async function connectCurrentSave()", 1,
        )[0]
        progress = self.script.split("function updateHomeConnectionProgress()", 1)[1].split(
            "function homeViewModel()", 1,
        )[0]

        self.assertIn('id="page-home" aria-labelledby="home-page-title" aria-busy="false"', self.html)
        self.assertIn('id="home-manager-record" aria-live="polite" aria-busy="false"', self.html)
        self.assertIn('class="home-connection-state" role="status" aria-live="polite"', self.html)
        self.assertIn('id="home-connection-dot" aria-hidden="true"', self.html)
        self.assertIn('id="connect-save-button" type="button" aria-controls="home-connect-progress" aria-busy="false"', self.html)
        self.assertIn('id="home-connect-progress" role="progressbar"', self.html)
        self.assertIn('aria-valuemin="0" aria-valuemax="100" aria-valuenow="0"', self.html)
        self.assertIn('progress?.setAttribute("aria-valuenow"', progress)
        self.assertIn('progress?.setAttribute("aria-valuetext"', progress)
        self.assertIn('root.setAttribute("aria-busy"', render)
        self.assertIn('recordPanel.setAttribute("aria-busy"', render)
        self.assertIn('connectButton.setAttribute("aria-busy"', render)
        self.assertIn("#connect-save-button:focus-visible", self.css)
        self.assertIn('@media (prefers-reduced-motion: reduce)', self.css)
        self.assertIn('.home-connect-track i { transition:none; }', self.css)

    def test_home_shows_current_player_manager_record_after_connection(self) -> None:
        loader = self.script.split(
            "async function ensureHomeManagerRecordLoaded()", 1,
        )[1].split("function renderCurrentPage", 1)[0]
        self.assertIn('id="home-manager-record"', self.html)
        self.assertIn("执教本队以来", self.html)
        self.assertIn("function homeManagerRecord()", self.script)
        self.assertIn("ensureHomeManagerRecordLoaded();", self.script)
        self.assertIn("recordPanel.hidden = !connected", self.script)
        self.assertIn("正在整理执教战绩", self.script)
        self.assertIn("loadClub({silent:true})", loader)
        self.assertNotIn('request("/api/club/refresh"', loader)
        self.assertNotIn("!app.state?.club_status?.ready", self.script)
        self.assertIn("recordTotal.win_rate", self.script)
        self.assertIn(".home-manager-record[hidden] { display:none; }", self.css)
        self.assertIn("font-size:13px", self.css)

    def test_top_save_label_uses_player_and_all_managed_teams(self) -> None:
        label = self.script.split("function topSaveLabel(data)", 1)[1].split(
            "function isSaveConnected()", 1,
        )[0]
        self.assertIn("const managerName = selectedManager?.name || data.manager?.name", label)
        self.assertIn('const teamNames = [...new Set(', label)
        self.assertIn('const teamName = teamNames.join("＋")', label)
        self.assertIn("`${managerName}-${teamName}`", label)
        self.assertNotIn('data.game_layout === "fm24"', label)

    def test_manager_switcher_distinguishes_club_national_and_unemployed(self) -> None:
        label = self.script.split("function managerOptionLabel(manager)", 1)[1].split(
            "function renderManagerSwitcher()", 1,
        )[0]

        self.assertIn('"俱乐部＋国家队"', label)
        self.assertIn('"国家队"', label)
        self.assertIn('"无业"', label)

    def test_multi_manager_prompt_is_populated_before_connection_completes(self) -> None:
        render = self.script.split("function render()", 1)[1].split(
            "async function maybeShowIntegrityNotice", 1,
        )[0]
        shell = self.script.split("function renderShellControls", 1)[1].split(
            "function render()", 1,
        )[0]

        self.assertIn("renderShellControls(data, connected, walletReady);", render)
        self.assertIn("renderManagerSwitcher();", shell)
        self.assertNotIn("if (connected) renderManagerSwitcher();", shell)
        self.assertLess(
            render.index("renderShellControls(data, connected, walletReady);"),
            render.index("maybeShowManagerPrompt();"),
        )

    def test_initial_manager_selection_reports_connection_progress(self) -> None:
        switcher = self.script.split("async function switchManager", 1)[1].split(
            "function renderTopbarRefreshStatus", 1,
        )[0]

        self.assertIn("const response = await request", switcher)
        self.assertIn("if (response.started) beginRefreshStatusPolling", switcher)
        self.assertIn("await loadState({fresh:true});", switcher)

    def test_usage_notice_opens_immediately_after_home_initialization(self) -> None:
        startup = self.script.rsplit("ensureMoneyCurrencyControl();", 1)[1]
        self.assertLess(
            startup.index('showPage("home");'),
            startup.index("showInitialUsageNotice();"),
        )
        notice = self.script.split("function showInitialUsageNotice()", 1)[1].split(
            "function ", 1,
        )[0]
        self.assertIn('dialog.showModal();', notice)
        self.assertNotIn("isSaveConnected()", notice)
        self.assertNotIn('page !== "home" && isSaveConnected()', self.script)

    def test_apps_require_a_live_save_connection(self) -> None:
        self.assertIn('function isSaveConnected()', self.script)
        self.assertIn('function isWalletReady()', self.script)
        self.assertIn('wallet_ready', self.script)
        self.assertIn('page !== "home" && !isSaveConnected()', self.script)
        self.assertIn('classList.toggle("home-view", page === "home")', self.script)

    def test_history_button_is_global(self) -> None:
        self.assertIn('historyButton.classList.remove("page-reserved-hidden")', self.script)
        self.assertIn('historyButton.tabIndex = 0;', self.script)

    def test_connect_button_resumes_an_existing_refresh(self) -> None:
        connect = self.script.split("async function connectCurrentSave()", 1)[1].split(
            "function renderRefreshControls()", 1,
        )[0]
        self.assertIn('message:"正在继续读取当前存档"', connect)
        self.assertIn('beginPolling(ACTIVE_REFRESH_POLL_MS, "status")', connect)
        self.assertNotIn('toast("当前正在读取数据，请稍候")', connect)

    def test_connection_stays_pending_when_startup_escalates_to_full_scan(self) -> None:
        state = LocalOddsState.__new__(LocalOddsState)
        state.lock = threading.RLock()
        state.connection_requested = True
        state.refreshing = True
        state.output = {}
        state.cache_verified = False
        state.refresh_reason = "partial_schedule_full"
        state.connection_scope_id = None
        state.wallet_ready = False
        state._refresh_state = MagicMock(return_value={"refreshing": True})

        status = state.refresh_status()

        self.assertTrue(status["connection_pending"])
        self.assertIn("if (status.connection_pending)", self.script)
        self.assertIn('status:"connecting"', self.script)
        self.assertIn("Boolean(app.state?.connection_pending)", self.script)

    def test_automatic_schedule_refresh_cannot_mask_initial_connection_failure(self) -> None:
        state = LocalOddsState.__new__(LocalOddsState)
        state.lock = threading.RLock()
        state.refreshing = False
        state.reconciling = False
        state.output = {}
        state.cache_verified = False

        started = state.refresh_async("schedule_change", full=False)

        self.assertFalse(started)
        self.assertFalse(state.refreshing)

    def test_clock_disconnect_requires_repeated_failures(self) -> None:
        self.assertIn("clockFailureCount: 0", self.script)
        self.assertIn("app.clockFailureCount += 1;", self.script)
        self.assertIn("app.clockFailureCount === 3", self.script)
        self.assertNotIn('message:"与游戏的连接已断开"', self.script)

    def test_backend_clock_gaps_do_not_disable_live_settlement(self) -> None:
        state = LocalOddsState.__new__(LocalOddsState)
        state.lock = threading.RLock()
        state.connection_requested = True
        state.cache_verified = True
        state.output = {"save_instance_id": "career-test"}
        state.refreshing = False
        state.reconciling = False
        state.connection_liveness_failures = 0
        state.connection_live_confirmed = False
        state.last_connection_clock = None
        state.connection_unavailable_since = None
        clock = {"date": "2026-07-23", "minutes": 720}

        current, alive = state._note_connection_liveness(clock, True)
        self.assertTrue(alive)
        self.assertEqual(current, clock)
        current, alive = state._note_connection_liveness(clock, False)
        self.assertTrue(alive)
        self.assertEqual(current, clock)
        self.assertEqual(state.connection_liveness_failures, 0)
        self.assertTrue(state.connection_requested)
        self.assertTrue(state.cache_verified)
        for _attempt in range(3):
            current, alive = state._note_connection_liveness(None, True)
            self.assertTrue(alive)
            self.assertEqual(current, clock)
            self.assertEqual(state.connection_liveness_failures, 0)
            self.assertTrue(state.connection_requested)
            self.assertTrue(state.cache_verified)

        with patch("fm_odds_web.monotonic", side_effect=[100.0, 105.0, 110.0, 131.0]):
            for _attempt in range(3):
                current, alive = state._note_connection_liveness(None, False)
                self.assertTrue(alive)
                self.assertEqual(current, clock)
            current, alive = state._note_connection_liveness(None, False)
        self.assertFalse(alive)
        self.assertIsNone(current)
        self.assertTrue(state.connection_requested)
        self.assertTrue(state.cache_verified)
        self.assertTrue(state.connection_suspended)
        self.assertEqual(state.output["save_instance_id"], "career-test")
        self.assertIn("已保留存档与玩家信息", state.status)
        self.assertFalse(state._betting_ready())

    def test_connected_process_survives_transient_layout_classification_failure(self) -> None:
        state = LocalOddsState.__new__(LocalOddsState)
        state.lock = threading.RLock()
        state.connection_process_pid = 23712
        state.connection_game_key = "fm24"

        with patch("fm_odds_web.fm_process_is_running", return_value=True) as running:
            available = state._connected_process_available({"available": []})

        self.assertTrue(available)
        running.assert_called_once_with(23712)

    def test_other_fm_process_does_not_keep_a_closed_connection_alive(self) -> None:
        state = LocalOddsState.__new__(LocalOddsState)
        state.lock = threading.RLock()
        state.connection_process_pid = 23712
        state.connection_game_key = "fm24"

        with patch("fm_odds_web.fm_process_is_running", return_value=False):
            available = state._connected_process_available({
                "available": [{"pid": 27704, "key": "fm26"}],
            })

        self.assertFalse(available)

    def test_other_fm_process_does_not_keep_closed_fm26_connection_alive(self) -> None:
        state = LocalOddsState.__new__(LocalOddsState)
        state.lock = threading.RLock()
        state.connection_process_pid = 39680
        state.connection_game_key = "fm26"

        with patch("fm_odds_web.fm_process_is_running", return_value=False) as running:
            available = state._connected_process_available({
                "available": [{"pid": 27704, "key": "fm24"}],
            })

        self.assertFalse(available)
        running.assert_called_once_with(39680)

    def test_refresh_keeps_verified_connection_when_live_probes_are_busy(self) -> None:
        state = LocalOddsState.__new__(LocalOddsState)
        state.lock = threading.RLock()
        state.connection_requested = True
        state.cache_verified = True
        state.output = {"save_instance_id": "career-test"}
        state.refreshing = True
        state.reconciling = False
        state.connection_liveness_failures = 2
        state.connection_live_confirmed = True
        state.last_connection_clock = {"date": "2026-07-23", "minutes": 720}
        state.connection_unavailable_since = 100.0

        current, alive = state._note_connection_liveness(None, False)

        self.assertTrue(alive)
        self.assertEqual(current, state.last_connection_clock)
        self.assertEqual(state.connection_liveness_failures, 2)
        self.assertTrue(state.connection_requested)
        self.assertTrue(state.cache_verified)

    def test_busy_clock_probe_does_not_count_as_process_loss(self) -> None:
        state = LocalOddsState.__new__(LocalOddsState)
        state.lock = threading.RLock()
        state.connection_requested = True
        state.cache_verified = True
        state.output = {"save_instance_id": "career-test"}
        state.refreshing = False
        state.reconciling = False
        state.connection_liveness_failures = 2
        state.connection_live_confirmed = True
        state.last_connection_clock = {"date": "2026-07-23", "minutes": 720}
        state.connection_unavailable_since = 100.0

        current, alive = state._note_connection_liveness(
            None, False, probe_busy=True,
        )

        self.assertTrue(alive)
        self.assertEqual(current, state.last_connection_clock)
        self.assertEqual(state.connection_liveness_failures, 2)
        self.assertEqual(state.connection_unavailable_since, 100.0)

    def test_world_scan_does_not_count_as_process_loss(self) -> None:
        state = LocalOddsState.__new__(LocalOddsState)
        state.lock = threading.RLock()
        state.connection_requested = True
        state.cache_verified = True
        state.output = {"save_instance_id": "career-test"}
        state.refreshing = False
        state.reconciling = False
        state.world_club_scanning = True
        state.connection_liveness_failures = 2
        state.connection_live_confirmed = True
        state.last_connection_clock = {"date": "2026-07-23", "minutes": 720}
        state.connection_unavailable_since = 100.0

        current, alive = state._note_connection_liveness(None, False)

        self.assertTrue(alive)
        self.assertEqual(current, state.last_connection_clock)
        self.assertEqual(state.connection_liveness_failures, 2)
        self.assertEqual(state.connection_unavailable_since, 100.0)

    def test_home_hides_global_refresh_progress(self) -> None:
        refresh = self.script.split("function renderTopbarRefreshStatus()", 1)[1].split(
            "function showPage", 1,
        )[0]
        self.assertIn('app.page === "home"', refresh)
        self.assertIn('label.textContent = "";', refresh)

    def test_application_sidebar_navigation_scrolls_independently(self) -> None:
        self.assertRegex(
            self.css,
            r"\.main-sidebar nav\s*\{[^}]*grid-row:2;[^}]*overflow-y:auto;",
        )

    def test_expanded_sidebar_brand_shares_header_with_toggle(self) -> None:
        self.assertRegex(
            self.css,
            r"\.main-sidebar\s*\{[^}]*grid-template-columns:minmax\(0,1fr\) 42px;",
        )
        self.assertIn(".sidebar-toggle { grid-column:2; grid-row:1;", self.css)
        self.assertIn(".sidebar-brand > span:first-child { grid-row:1/-1; color:#fff; font-size:27px;", self.css)
        self.assertIn(".sidebar-brand .game-version-switch [data-game-build] { display:none; }", self.css)

    def test_topbar_game_versions_have_distinct_rows(self) -> None:
        self.assertIn(
            ".brand .game-version-switch > [data-game-version-button] { height:44px; display:grid; grid-template-rows:24px 13px; align-content:center; gap:4px;",
            self.css,
        )
        self.assertIn(
            ".brand .game-version-switch [data-game-build] { position:static;",
            self.css,
        )

    def test_game_clock_polling_waits_for_wallet_connection(self) -> None:
        clock = self.script.split("async function loadGameClock()", 1)[1].split(
            "async function pollRefreshStatus()", 1,
        )[0]
        self.assertIn("if (!isWalletReady()) return;", clock)

    def test_connect_rejects_when_game_is_not_running(self) -> None:
        state = SimpleNamespace(startup_async=MagicMock())
        with patch("fm_odds_web.process_selection_status", return_value={"available": []}):
            with self.assertRaisesRegex(ValueError, "请先启动游戏"):
                LocalOddsState.connect_current_save(state)
        state.startup_async.assert_not_called()

    def test_connect_defers_save_reads_to_original_startup_flow(self) -> None:
        state = SimpleNamespace(
            startup_async=MagicMock(return_value=True),
            lock=threading.RLock(),
            wallet_ready=False,
            connection_scope_id=None,
        )
        with (
            patch("fm_odds_web.process_selection_status", return_value={
                "available": [{"pid": 27704, "key": "fm26"}],
                "selected": "fm26", "selected_pid": 27704,
            }),
            patch("fm_odds_web.select_game_process") as select_target,
            patch("fm_odds_web.read_game_clock") as read_clock,
            patch("fm_odds_web.read_save_identity") as read_identity,
            patch("fm_odds_web.public_economy") as economy,
        ):
            result = LocalOddsState.connect_current_save(state)

        self.assertTrue(result["started"])
        self.assertFalse(result["wallet_ready"])
        self.assertTrue(state.connection_requested)
        self.assertEqual(state.connection_process_pid, 27704)
        self.assertEqual(state.connection_game_key, "fm26")
        self.assertEqual(state.last_process_selection_status, {
            "available": [{"pid": 27704, "key": "fm26"}],
            "selected": "fm26", "selected_pid": 27704,
        })
        select_target.assert_called_once_with("fm26", 27704)
        read_clock.assert_not_called()
        read_identity.assert_not_called()
        economy.assert_not_called()

    def test_connect_binds_ready_wallet_without_building_economy_snapshot(self) -> None:
        state = SimpleNamespace(
            startup_async=MagicMock(return_value=False),
            lock=threading.RLock(),
            wallet_ready=True,
            connection_scope_id="account-test",
        )
        with patch("fm_odds_web.process_selection_status", return_value={
            "available": [{"pid": 27704, "key": "fm26"}],
            "selected": "fm26", "selected_pid": 27704,
        }), patch("fm_odds_web.select_game_process"), patch(
            "fm_odds_web.set_active_save_id",
        ) as activate, patch(
            "fm_odds_web.public_economy",
        ) as public:
            result = LocalOddsState.connect_current_save(state)

        activate.assert_called_once_with("account-test")
        public.assert_not_called()
        self.assertEqual(result["data_scope_id"], "account-test")
        self.assertNotIn("balance", result)
        self.assertNotIn("economy", result)

    def test_wallet_can_bind_before_the_full_snapshot_is_verified(self) -> None:
        state = SimpleNamespace(
            _has_verified_save=lambda: False,
            output={},
            connection_scope_id="account-test",
            wallet_ready=True,
        )
        with patch("fm_odds_web.set_active_save_id") as activate:
            scope_id = LocalOddsState._bind_wallet_scope(state)
        self.assertEqual(scope_id, "account-test")
        activate.assert_called_once_with("account-test")

    def test_deferred_save_identity_blocks_account_bindings(self) -> None:
        state = SimpleNamespace(
            output={"save_identity_discovery_deferred": True},
        )

        with self.assertRaisesRegex(ValueError, "正在确认存档身份"):
            LocalOddsState._bind_current_save(state)
        with self.assertRaisesRegex(ValueError, "正在确认存档身份"):
            LocalOddsState._bind_wallet_scope(state)

    def test_deferred_save_identity_pauses_betting_until_snapshot_replaces_it(self) -> None:
        state = SimpleNamespace(
            _has_verified_save=lambda: True,
            output={"save_identity_discovery_deferred": True},
            save_change_pending=False,
            connection_suspended=False,
        )

        self.assertFalse(LocalOddsState._betting_ready(state))

        state.output = {"save_instance_id": "career-test"}
        self.assertTrue(LocalOddsState._betting_ready(state))

    def test_wallet_operation_keeps_the_scope_displayed_by_the_client(self) -> None:
        state = SimpleNamespace(
            _has_verified_save=lambda: True,
            _data_scope_id=lambda _output: "account-current",
            output={},
            connection_scope_id="account-early",
            wallet_ready=True,
        )
        with patch("fm_odds_web.set_active_save_id") as activate:
            scope_id = LocalOddsState._bind_wallet_scope(state, "account-current")
        self.assertEqual(scope_id, "account-current")
        activate.assert_called_once_with("account-current")

    def test_wallet_operation_rejects_a_stale_client_scope(self) -> None:
        state = SimpleNamespace(
            _has_verified_save=lambda: True,
            _data_scope_id=lambda _output: "account-current",
            output={},
            connection_scope_id="account-early",
            wallet_ready=True,
        )
        with self.assertRaisesRegex(ValueError, "账户已变化"):
            LocalOddsState._bind_wallet_scope(state, "account-stale")

    def test_connect_starts_cache_validating_startup_flow(self) -> None:
        state = SimpleNamespace(
            startup_async=MagicMock(return_value=True),
            lock=threading.RLock(),
            wallet_ready=False,
            connection_scope_id=None,
        )
        versions = {
            "available": [{"pid": 27704, "key": "fm26"}],
            "selected": "fm26", "selected_pid": 27704,
        }
        with (
            patch("fm_odds_web.process_selection_status", return_value=versions),
            patch("fm_odds_web.select_game_process"),
        ):
            result = LocalOddsState.connect_current_save(state)
        self.assertTrue(result["started"])
        self.assertFalse(result["wallet_ready"])
        self.assertNotIn("balance", result)
        self.assertNotIn("economy", result)
        state.startup_async.assert_called_once_with()

    def test_connect_forwards_verified_selection_and_keeps_it_private(self) -> None:
        layout = SimpleNamespace(key="fm26")
        selected = (27704, "steam/fm.exe", layout)
        state = SimpleNamespace(
            startup_async=MagicMock(return_value=True),
            lock=threading.RLock(), wallet_ready=False,
            connection_scope_id=None,
        )
        versions = {
            "available": [{"pid": 27704, "key": "fm26"}],
            "selected": "fm26", "selected_pid": 27704,
            "_selected_process": selected,
        }
        with (
            patch("fm_odds_web.process_selection_status", return_value=versions),
            patch("fm_odds_web.select_game_process", return_value=False) as pin,
        ):
            result = LocalOddsState.connect_current_save(state)

        pin.assert_called_once_with("fm26", 27704, selected=selected)
        startup_kwargs = state.startup_async.call_args.kwargs
        self.assertIs(startup_kwargs["selected_process"], selected)
        self.assertFalse(startup_kwargs["connection_trace"]["selection_changed"])
        self.assertNotIn("_selected_process", result["game_versions"])
        self.assertNotIn("_selected_process", state.last_process_selection_status)

    def test_uncached_startup_publishes_connection_before_full_odds_read(self) -> None:
        context = {
            "save_instance_id": "career-test",
            "game_layout": "fm26",
            "game_version": "26.3.2",
            "game_date": "2026-07-23",
            "game_time": "12:00",
            "manager": {"id": 77, "name": "Manager"},
            "manager_options": [{"id": 77, "name": "Manager"}],
            "selected_manager_id": 77,
            "managed_team": {"id": 42, "name": "Club", "team_type": "club"},
            "managed_teams": [{"id": 42, "name": "Club", "team_type": "club"}],
            "connection_performance": {
                "total_ms": 12.5, "manager_count": 1,
                "session_generation": 3,
            },
        }
        selected = (27704, "steam/fm.exe", SimpleNamespace(key="fm26"))
        state = SimpleNamespace(
            lock=threading.RLock(),
            memory_lock=threading.Lock(),
            output={},
            cache_verified=False,
            _has_verified_save=lambda: False,
            _assign_account_scope=lambda output, _previous: "account-test",
            _apply_unemployed_confirmation_state=lambda _output, _previous=None: None,
            refresh_mode="startup",
            refresh_reason="startup",
            refresh_cancel_event=None,
            status="",
            error=None,
            connection_save_id=None,
            connection_scope_id=None,
            wallet_ready=False,
            connection_live_confirmed=False,
            connection_suspended=False,
            connection_liveness_failures=0,
            connection_unavailable_since=None,
            last_connection_clock=None,
            pending_save_identity=None,
            save_change_pending=False,
            data_version=0,
            club_profile=None,
            club_profiles={},
            club_context=None,
            club_contexts={},
            club_last_updated=None,
            _refresh_worker=MagicMock(),
            _warm_world_player_index_after_connect=MagicMock(),
        )
        cancel_event = threading.Event()
        state.refresh_cancel_event = cancel_event
        with (
            patch("fm_odds_web.read_connection_context", return_value=context) as read_context,
            patch("fm_odds_web.set_active_save_id"),
            patch("fm_odds_web.remember_active_save_id"),
        ):
            LocalOddsState._startup_worker(
                state, cancel_event, selected,
                {
                    "process_discovery_ms": 4.0,
                    "selection_pin_ms": 0.1,
                    "selection_changed": False,
                },
            )

        read_context.assert_called_once_with(
            preferred_save_id=None,
            preferred_manager_id=None,
            known_manager_sessions=[],
            selected_process=selected,
        )
        self.assertTrue(state.cache_verified)
        self.assertEqual(state.output["manager"]["id"], 77)
        self.assertEqual(state.connection_scope_id, "account-test")
        self.assertEqual(state.last_connection_clock["date"], "2026-07-23")
        self.assertEqual(state.output["connection_performance"]["context_read_ms"], 12.5)
        self.assertEqual(state.output["connection_performance"]["session_generation"], 3)
        self.assertFalse(state.output["connection_performance"]["selection_changed"])
        state._refresh_worker.assert_called_once_with(
            "startup_odds", "full", False, cancel_event,
        )
        state._warm_world_player_index_after_connect.assert_called_once_with(selected)

    def test_fm24_cold_start_queues_one_full_odds_rebuild_after_club_read(self) -> None:
        context = {
            "save_instance_id": "career-fm24",
            "game_layout": "fm24",
            "game_version": "24.4.2",
            "game_date": "2025-02-20",
            "game_time": "07:00",
            "manager": {"id": 77, "name": "Manager"},
            "manager_options": [{"id": 77, "name": "Manager"}],
            "selected_manager_id": 77,
            "managed_team": {"id": 42, "name": "Club", "team_type": "club"},
            "managed_teams": [{"id": 42, "name": "Club", "team_type": "club"}],
            "connection_performance": {"total_ms": 12.5},
        }
        cancel_event = threading.Event()
        state = SimpleNamespace(
            lock=threading.RLock(), memory_lock=threading.Lock(), output={},
            cache_verified=False, _has_verified_save=lambda: False,
            _assign_account_scope=lambda output, _previous: "account-fm24",
            _apply_unemployed_confirmation_state=lambda _output, _previous=None: None,
            refresh_mode="startup", refresh_reason="startup",
            refresh_cancel_event=cancel_event, refresh_cancel_on_clock_change=False,
            refreshing=True, status="", error=None,
            connection_save_id=None, connection_scope_id=None, wallet_ready=False,
            connection_live_confirmed=False, connection_suspended=False,
            connection_liveness_failures=0, connection_unavailable_since=None,
            last_connection_clock=None, pending_save_identity=None,
            save_change_pending=False, data_version=0,
            club_profile=None, club_profiles={}, club_context=None,
            club_contexts={}, club_last_updated=None, club_error=None,
            deferred_memory_job_after_club=None,
            _refresh_worker=MagicMock(), refresh_club_async=MagicMock(return_value=True),
            refresh_async=MagicMock(return_value=True),
            _warm_world_player_index_after_connect=MagicMock(),
        )
        with (
            patch("fm_odds_web.read_connection_context", return_value=context),
            patch("fm_odds_web.set_active_save_id"),
            patch("fm_odds_web.remember_active_save_id"),
            patch("fm_odds_web.startup_cache_validation", return_value=(False, "missing_snapshot")),
            patch("fm_odds_web.startup_odds_refresh_mode", return_value="full"),
        ):
            LocalOddsState._startup_worker(state, cancel_event)

        self.assertFalse(state.refreshing)
        self.assertEqual(state.status, "已连接 FM24，正在优先读取俱乐部资料")
        self.assertEqual(state.deferred_memory_job_after_club, {
            "kind": "refresh", "reason": "startup_odds", "full": True,
            "cancel_on_clock_change": False,
        })
        state.refresh_club_async.assert_called_once_with()
        state.refresh_async.assert_not_called()
        state._refresh_worker.assert_not_called()

    def test_world_player_index_warmup_runs_outside_connection_worker(self) -> None:
        started = threading.Event()
        release = threading.Event()
        selected = (27704, "steam/fm.exe", SimpleNamespace(key="fm26"))

        def warm(_selected):
            started.set()
            release.wait(2)

        state = SimpleNamespace()
        with patch("fm_odds_web.warm_database_index", side_effect=warm) as trigger:
            LocalOddsState._warm_world_player_index_after_connect(state, selected)
            self.assertTrue(started.wait(1))
            trigger.assert_called_once_with(selected)
        release.set()

    def test_connection_preflight_failure_keeps_verified_player_context(self) -> None:
        previous = {
            "save_instance_id": "career-test",
            "manager": {"id": 77, "name": "Manager"},
            "managed_team": {"id": 42, "name": "Club", "team_type": "club"},
            "managed_teams": [{"id": 42, "name": "Club", "team_type": "club"}],
        }
        cancel_event = threading.Event()
        state = SimpleNamespace(
            lock=threading.RLock(), memory_lock=threading.Lock(),
            output=previous, cache_verified=True,
            _has_verified_save=lambda: True,
            refreshing=True, refresh_mode="startup", refresh_reason="startup",
            refresh_cancel_event=cancel_event, status="", error=None,
            _refresh_worker=MagicMock(),
        )
        with patch(
            "fm_odds_web.read_connection_context",
            side_effect=RuntimeError("temporary read failure"),
        ):
            LocalOddsState._startup_worker(state, cancel_event)

        self.assertIs(state.output, previous)
        self.assertTrue(state.cache_verified)
        self.assertFalse(state.refreshing)
        self.assertIn("已保留上次玩家信息", state.status)
        state._refresh_worker.assert_not_called()

    def test_game_version_switch_waits_for_manual_connection(self) -> None:
        source = __import__("inspect").getsource(LocalOddsState.switch_game_version)
        self.assertNotIn("refresh_async", source)
        self.assertIn('"refresh_started": False', source)


if __name__ == "__main__":
    unittest.main()
