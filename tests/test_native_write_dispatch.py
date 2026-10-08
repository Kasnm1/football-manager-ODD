from __future__ import annotations

import ctypes
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fm_collector.win32 import (
    MEMORY_BASIC_INFORMATION, PAGE_EXECUTE_READWRITE, PAGE_READONLY,
    PAGE_READWRITE, ProcessHandle, kernel32,
    write_process_memory,
)
from tools import rust_native_core
from tools import hook_native_core
from tools.hook_native_core import ABI_VERSION as HOOK_ABI_VERSION
from tools.hook_native_core import _LIBRARY as HOOK_LIBRARY
from tools.hook_native_core import pattern_matches, rel32, verified_hook_write


MEM_COMMIT_RESERVE = 0x3000
MEM_RELEASE = 0x8000


class NativeWriteDispatchTests(unittest.TestCase):
    @staticmethod
    def current_process() -> ProcessHandle:
        kernel32.GetCurrentProcess.argtypes = []
        kernel32.GetCurrentProcess.restype = ctypes.c_void_p
        return ProcessHandle(0, int(kernel32.GetCurrentProcess() or 0))

    def allocate(self, protect: int) -> int:
        address = int(kernel32.VirtualAllocEx(
            self.current_process().handle, None, 0x1000, MEM_COMMIT_RESERVE, protect,
        ) or 0)
        self.assertTrue(address)
        self.addCleanup(
            kernel32.VirtualFreeEx, self.current_process().handle,
            ctypes.c_void_p(address), 0, MEM_RELEASE,
        )
        return address

    def protect(self, address: int, protect: int) -> int:
        previous = ctypes.c_ulong(0)
        self.assertTrue(kernel32.VirtualProtectEx(
            self.current_process().handle, ctypes.c_void_p(address), 0x1000,
            protect, ctypes.byref(previous),
        ))
        return int(previous.value)

    def page_protection(self, address: int) -> int:
        info = MEMORY_BASIC_INFORMATION()
        self.assertTrue(kernel32.VirtualQueryEx(
            self.current_process().handle, ctypes.c_void_p(address),
            ctypes.byref(info), ctypes.sizeof(info),
        ))
        return int(info.Protect)

    def test_cpp_hook_abi_and_primitives(self) -> None:
        self.assertEqual(HOOK_LIBRARY.fmodd_hook_abi_version(), HOOK_ABI_VERSION)
        self.assertEqual(rel32(0x1005, 0x2000), b"\xfb\x0f\x00\x00")
        self.assertEqual(pattern_matches(b"ABAC", (0x41, None)), [0, 2])

    def test_loaded_native_dlls_are_locked_against_write_or_replacement(self) -> None:
        create_file = ctypes.WinDLL("kernel32", use_last_error=True).CreateFileW
        create_file.argtypes = [
            ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
            ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
        ]
        create_file.restype = ctypes.c_void_p
        invalid = ctypes.c_void_p(-1).value
        for path in (rust_native_core._LOADED_PATH, hook_native_core._LOADED_PATH):
            with self.subTest(path=str(path)):
                handle = create_file(
                    str(path), 0x40000000, 0x1 | 0x2 | 0x4,
                    None, 3, 0x80, None,
                )
                if handle and int(handle) != invalid:
                    ctypes.windll.kernel32.CloseHandle(handle)
                self.assertEqual(int(handle or 0), invalid)
                self.assertEqual(ctypes.get_last_error(), 32)

    def test_frozen_runtime_ignores_environment_dll_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch.object(
            sys, "frozen", True, create=True,
        ), patch.object(
            sys, "_MEIPASS", temporary, create=True,
        ), patch.dict(os.environ, {
            "FMODD_RUST_NATIVE_DLL": r"C:\\untrusted\\rust.dll",
            "FMODD_CPP_HOOK_DLL": r"C:\\untrusted\\hook.dll",
        }):
            root = Path(temporary)
            self.assertEqual(
                rust_native_core._candidate_paths(),
                [root / "tools" / "fmodd_native_core.dll"],
            )
            self.assertEqual(
                hook_native_core._candidate_paths(),
                [root / "tools" / "fmodd_hook_core.dll"],
            )

    def test_readwrite_page_dispatches_to_rust(self) -> None:
        address = self.allocate(PAGE_READWRITE)
        ctypes.memmove(address, b"ABCD", 4)
        with patch("tools.rust_native_core.verified_write", wraps=rust_native_core.verified_write) as rust_write, patch(
            "tools.hook_native_core.verified_hook_write",
        ) as hook_write:
            write_process_memory(self.current_process(), address, b"WXYZ")
        rust_write.assert_called_once()
        hook_write.assert_not_called()
        self.assertEqual(ctypes.string_at(address, 4), b"WXYZ")

    def test_executable_page_dispatches_to_cpp(self) -> None:
        address = self.allocate(PAGE_EXECUTE_READWRITE)
        ctypes.memmove(address, b"ABCD", 4)
        with patch("tools.hook_native_core.verified_hook_write", wraps=verified_hook_write) as hook_write, patch(
            "tools.rust_native_core.verified_write",
        ) as rust_write:
            write_process_memory(self.current_process(), address, b"WXYZ")
        hook_write.assert_called_once()
        rust_write.assert_not_called()
        self.assertEqual(ctypes.string_at(address, 4), b"WXYZ")

    def test_readonly_data_page_is_temporarily_made_writable_and_restored(self) -> None:
        address = self.allocate(PAGE_READWRITE)
        ctypes.memmove(address, b"ABCD", 4)
        self.protect(address, PAGE_READONLY)

        with patch(
            "tools.rust_native_core.verified_write",
            wraps=rust_native_core.verified_write,
        ) as rust_write:
            write_process_memory(
                self.current_process(), address, b"WXYZ",
                compatibility_fallback=True,
            )

        rust_write.assert_called_once()
        self.assertEqual(ctypes.string_at(address, 4), b"WXYZ")
        self.assertEqual(self.page_protection(address), PAGE_READONLY)

    def test_readonly_data_page_protection_is_restored_when_write_fails(self) -> None:
        address = self.allocate(PAGE_READWRITE)
        ctypes.memmove(address, b"ABCD", 4)
        self.protect(address, PAGE_READONLY)

        with patch(
            "tools.rust_native_core.verified_write",
            side_effect=RuntimeError("synthetic write failure"),
        ):
            with self.assertRaisesRegex(RuntimeError, "synthetic write failure"):
                write_process_memory(
                    self.current_process(), address, b"WXYZ",
                    compatibility_fallback=True,
                )

        self.assertEqual(ctypes.string_at(address, 4), b"ABCD")
        self.assertEqual(self.page_protection(address), PAGE_READONLY)

    def test_club_balance_compatibility_falls_back_after_native_write_code_four(self) -> None:
        address = self.allocate(PAGE_READWRITE)
        ctypes.memmove(address, b"ABCD", 4)

        with patch(
            "tools.rust_native_core.verified_write",
            side_effect=rust_native_core.NativeWriteError(4),
        ) as rust_write:
            write_process_memory(
                self.current_process(), address, b"WXYZ",
                compatibility_fallback=True,
            )

        rust_write.assert_called_once()
        self.assertEqual(ctypes.string_at(address, 4), b"WXYZ")

    def test_native_write_code_four_does_not_fall_back_without_opt_in(self) -> None:
        address = self.allocate(PAGE_READWRITE)
        ctypes.memmove(address, b"ABCD", 4)

        with patch(
            "tools.rust_native_core.verified_write",
            side_effect=rust_native_core.NativeWriteError(4),
        ):
            with self.assertRaisesRegex(RuntimeError, "native process-memory write failed"):
                write_process_memory(self.current_process(), address, b"WXYZ")

        self.assertEqual(ctypes.string_at(address, 4), b"ABCD")


if __name__ == "__main__":
    unittest.main()
