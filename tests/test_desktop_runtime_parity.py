from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from fmodd_desktop import (
    _protected_release_runtime,
    acquire_single_instance,
    release_single_instance,
    start_local_runtime as desktop_start_local_runtime,
    verify_release_integrity,
)
from fm_odds_web import start_local_runtime


ROOT = Path(__file__).resolve().parents[1]


class DesktopRuntimeParityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = (ROOT / "fmodd_desktop.py").read_text(encoding="utf-8")
        cls.web_source = (ROOT / "fm_odds_web.py").read_text(encoding="utf-8")

    def test_web_and_desktop_use_one_shared_runtime_entry(self) -> None:
        self.assertIn("from fm_odds_web import Handler, LocalOddsState, start_local_runtime", self.source)
        self.assertIn("start_local_runtime(state)", self.source)
        self.assertIn("start_local_runtime(state)", self.web_source)
        self.assertIs(desktop_start_local_runtime, start_local_runtime)

    def test_formal_desktop_waits_for_manual_save_connection(self) -> None:
        self.assertNotIn("state.startup_async()", self.web_source)
        self.assertNotIn('state.refresh_async("startup", full=True)', self.source)

    def test_formal_desktop_can_open_and_remain_without_fm(self) -> None:
        self.assertNotIn("select_process()", self.source)
        self.assertNotIn("host.terminate()\n                break", self.source)

    def test_formal_desktop_rejects_a_second_instance(self) -> None:
        with patch("fmodd_desktop._kernel32.CreateMutexW", return_value=123), patch(
            "fmodd_desktop.ctypes.get_last_error", return_value=183,
        ), patch("fmodd_desktop._kernel32.CloseHandle") as close_handle:
            self.assertIsNone(acquire_single_instance())

        close_handle.assert_called_once_with(123)

    def test_formal_desktop_retains_and_releases_first_instance_mutex(self) -> None:
        with patch("fmodd_desktop._kernel32.CreateMutexW", return_value=456), patch(
            "fmodd_desktop.ctypes.get_last_error", return_value=0,
        ), patch("fmodd_desktop._kernel32.CloseHandle") as close_handle:
            handle = acquire_single_instance()
            self.assertEqual(handle, 456)
            close_handle.assert_not_called()
            release_single_instance(handle)

        close_handle.assert_called_once_with(456)

    def test_shared_runtime_starts_idle_monitors_without_connecting(self) -> None:
        state = MagicMock()
        started_targets = []

        class ThreadStub:
            def __init__(self, *, target, daemon):
                self.target = target
                self.daemon = daemon

            def start(self):
                started_targets.append(self.target)

        with patch("fm_odds_web.threading.Thread", ThreadStub):
            start_local_runtime(state)

        self.assertEqual(started_targets, [
            state.watch_game_date,
            state.watch_match_intelligence,
            state.watch_live_result_capture,
            state.watch_live_settlement,
        ])
        state.startup_async.assert_not_called()
        self.assertEqual(state.status, "等待连接 Football Manager 存档")

    def test_shared_runtime_does_not_probe_fm_before_manual_connection(self) -> None:
        state = MagicMock()

        class ThreadStub:
            def __init__(self, *, target, daemon):
                self.target = target

            def start(self):
                return None

        with patch("fm_odds_web.threading.Thread", ThreadStub), patch("fm_odds_web.select_process") as select:
            start_local_runtime(state)

        select.assert_not_called()
        state.startup_async.assert_not_called()
        self.assertEqual(state.status, "等待连接 Football Manager 存档")


    def test_unprotected_release_does_not_require_integrity_extension(self) -> None:
        with patch("fmodd_desktop.FROZEN", True), patch.dict(
            sys.modules, {"tools._release_integrity": None},
        ):
            self.assertEqual(verify_release_integrity(), [])

    def test_protected_release_fails_closed_when_integrity_extension_is_missing(self) -> None:
        with patch("fmodd_desktop.FROZEN", True), patch(
            "fmodd_desktop._protected_release_runtime", return_value=True,
        ), patch.dict(sys.modules, {"tools._release_integrity": None}):
            errors = verify_release_integrity()

        self.assertEqual(
            errors,
            ["tools/_release_integrity is missing from the protected runtime"],
        )

    def test_protected_release_is_detected_from_compiled_module_sentinel(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tools_root = root / "tools"
            tools_root.mkdir()
            (tools_root / "game_layout.cp313-win_amd64.pyd").write_bytes(b"native")

            with patch("fmodd_desktop.ASSET_ROOT", root):
                self.assertTrue(_protected_release_runtime())

    def test_pre_entry_integrity_verification_is_not_repeated(self) -> None:
        with patch("fmodd_desktop.FROZEN", True), patch.object(
            sys, "_fmodd_release_integrity_verified", True, create=True,
        ), patch.dict(sys.modules, {"tools._release_integrity": None}):
            self.assertEqual(verify_release_integrity(), [])

    def test_formal_desktop_closes_attribute_growth_hook(self) -> None:
        self.assertIn("state.attribute_growth_hook,", self.source)

    def test_native_startup_overlay_covers_webview_initialization(self) -> None:
        host_source = (ROOT / "desktop" / "WebViewHost.cs").read_text(encoding="utf-8")
        self.assertIn('title.Text = UserText("startup.initialising");', host_source)
        self.assertIn("browser.Visible = false;", host_source)
        self.assertIn("startupOverlay.Visible = false;", host_source)
        self.assertIn("browser.CoreWebView2.Navigate(address);", host_source)
        self.assertNotIn("NavigateToString", host_source)

    def test_native_host_opens_only_approved_external_domains(self) -> None:
        host_source = (ROOT / "desktop" / "WebViewHost.cs").read_text(encoding="utf-8")
        handler = host_source.split("NewWindowRequested +=", 1)[1].split(
            "NavigationStarting +=", 1
        )[0]
        self.assertIn("Uri.TryCreate(args.Uri, UriKind.Absolute, out externalTarget)", handler)
        self.assertIn("externalTarget.Scheme == Uri.UriSchemeHttps", handler)
        self.assertIn('externalTarget.Host, "fmodd.com"', handler)
        self.assertIn('externalTarget.Host, "ko-fi.com"', handler)
        self.assertIn("Process.Start(args.Uri)", handler)
        self.assertNotIn("space.bilibili.com", handler)

    def test_native_window_restores_visible_previous_placement(self) -> None:
        host_source = (ROOT / "desktop" / "WebViewHost.cs").read_text(encoding="utf-8")
        self.assertIn('"FMODD", "window-state.txt"', host_source)
        self.assertIn("LoadWindowPlacement();", host_source)
        self.assertIn("FormClosing += OnFormClosing;", host_source)
        self.assertIn("foreach (Screen screen in Screen.AllScreens)", host_source)
        self.assertIn("Rectangle.Intersect(savedBounds, screen.WorkingArea)", host_source)
        self.assertIn("WindowState = FormWindowState.Maximized;", host_source)
        self.assertIn("File.Replace(temporaryPath, statePath, null, true);", host_source)


if __name__ == "__main__":
    unittest.main()
