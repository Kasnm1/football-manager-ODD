from __future__ import annotations

import struct
import unittest
import json
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import patch

from tools.fm24_process_diagnostic import (
    _adaptation_probe_kind,
    _runtime_layout_for_process,
    fmodd_service_probe,
    fm26_runtime_save_probe,
)
from tools.game_layout import FM26_XGP_IMAGE_SIZE, FM26_XGP_PE_TIMESTAMP


class _ProcessContext:
    def __init__(self, process: object) -> None:
        self.process = process

    def __enter__(self) -> object:
        return self.process

    def __exit__(self, *_args: object) -> None:
        return None


class _HttpResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self.stream = BytesIO(json.dumps(payload).encode("utf-8"))

    def __enter__(self) -> "_HttpResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return self.stream.read()


class FM26DiagnosticSaveProbeTests(unittest.TestCase):
    def test_fm26_plugin_wins_over_generic_xgp_content_fallback(self) -> None:
        process_path = "E:/XboxGames/Football Manager 26/Content/fm.exe"

        self.assertEqual(
            _adaptation_probe_kind(
                process_path,
                "无法读取",
                "FM2026 XGP 0.9.9957.0（Beta 适配）",
                SimpleNamespace(),
            ),
            "fm26",
        )
        self.assertEqual(
            _adaptation_probe_kind(process_path, "无法读取", None, None),
            "fm24_memory",
        )

    def test_xgp_layout_uses_plugin_identity_instead_of_launcher_identity(self) -> None:
        process = object()
        module = SimpleNamespace(
            base_address=0x20000000,
            size=FM26_XGP_IMAGE_SIZE,
            path="E:/XboxGames/Football Manager 26/Content/game_plugin.dll",
        )
        resolved = object()

        with (
            patch(
                "fm_collector.win32.open_process",
                return_value=_ProcessContext(process),
            ),
            patch("fm_collector.win32.find_module", return_value=module),
            patch(
                "tools.game_layout.resolve_fm26_xgp_layout",
                return_value=resolved,
            ) as resolve,
        ):
            layout = _runtime_layout_for_process(
                7,
                "E:/XboxGames/Football Manager 26/Content/fm.exe",
                None,
                (0x68DEEFA9, 0xA9000),
                (FM26_XGP_PE_TIMESTAMP, FM26_XGP_IMAGE_SIZE),
            )

        self.assertIs(layout, resolved)
        resolve.assert_called_once_with(
            7,
            "E:/XboxGames/Football Manager 26/Content/fm.exe",
            module.base_address,
            module.size,
            module.path,
        )

    def test_reports_fmodd_refresh_error_from_local_service(self) -> None:
        payload = {
            "refreshing": False,
            "reconciling": False,
            "refresh_mode": None,
            "refresh_reason": None,
            "has_output": False,
            "cache_verified": False,
            "status": "连接存档失败",
            "error": "赛程池读取失败",
            "error_stage": "generate_odds",
            "error_traceback": "Traceback\nIndexError: list index out of range",
            "operation_error": "FM24 logic thread could not be suspended",
            "operation_error_type": "RuntimeError",
            "operation_error_stage": "player_loan",
            "operation_error_traceback": "Traceback\nRuntimeError: loan failed",
        }
        with patch("urllib.request.urlopen", return_value=_HttpResponse(payload)):
            rows = fmodd_service_probe()

        report = "\n".join(rows)
        self.assertIn("状态：读取失败", report)
        self.assertIn("赛程池读取失败", report)
        self.assertIn("失败阶段：generate_odds", report)
        self.assertIn("IndexError: list index out of range", report)
        self.assertIn("FM24 logic thread could not be suspended", report)
        self.assertIn("最近操作异常类型：RuntimeError", report)
        self.assertIn("最近操作失败阶段：player_loan", report)
        self.assertIn("RuntimeError: loan failed", report)
        self.assertNotIn("连接存档失败", report)
        self.assertNotIn("save_instance_id", report)

    def test_reports_ready_without_exposing_identity_values(self) -> None:
        process = object()
        module = SimpleNamespace(base_address=0x10000000)
        root = 0x20000000

        def read(_process: object, address: int, size: int) -> bytes | None:
            if address == module.base_address + 0x1234 and size == 8:
                return struct.pack("<Q", root)
            if address == root + 0xB8 and size == 8:
                return struct.pack("<II", 7818016, 9839325)
            return None

        with (
            patch("fm_collector.win32.open_process", return_value=_ProcessContext(process)),
            patch("fm_collector.win32.find_module", return_value=module),
            patch("fm_collector.win32.read_process_memory", side_effect=read),
        ):
            result = fm26_runtime_save_probe(7, [0x1234])

        self.assertIn("已就绪", result)
        self.assertIn("身份值已隐藏", result)
        self.assertNotIn("7818016", result)
        self.assertNotIn("9839325", result)

    def test_distinguishes_loaded_object_from_active_save(self) -> None:
        process = object()
        module = SimpleNamespace(base_address=0x10000000)
        root = 0x20000000

        def read(_process: object, address: int, size: int) -> bytes | None:
            if address == module.base_address + 0x1234 and size == 8:
                return struct.pack("<Q", root)
            if address == root + 0xB8 and size == 8:
                return bytes(8)
            return None

        with (
            patch("fm_collector.win32.open_process", return_value=_ProcessContext(process)),
            patch("fm_collector.win32.find_module", return_value=module),
            patch("fm_collector.win32.read_process_memory", side_effect=read),
        ):
            result = fm26_runtime_save_probe(7, [0x1234])

        self.assertIn("对象可读但身份字段未就绪", result)


if __name__ == "__main__":
    unittest.main()
