from __future__ import annotations

import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import native_library_loader


class NativeLibraryLoaderTests(unittest.TestCase):
    def test_frozen_integrity_failure_happens_before_dll_load(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "native.dll"
            path.write_bytes(b"not-a-library")
            verifier = types.ModuleType("tools._release_integrity")
            verifier.verify_native_library = lambda *_args: ["native modified"]
            with patch.object(sys, "frozen", True, create=True), patch.object(
                sys, "_MEIPASS", temporary, create=True,
            ), patch.dict(sys.modules, {"tools._release_integrity": verifier}), patch(
                "tools.native_library_loader.ctypes.CDLL",
            ) as load_library:
                with self.assertRaisesRegex(RuntimeError, "native modified"):
                    native_library_loader.load_native_library(
                        [path], packaged_name="tools/native.dll",
                        abi_symbol="abi", abi_version=1, label="test native",
                    )
                load_library.assert_not_called()

    def test_frozen_integrity_module_is_required(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "native.dll"
            path.write_bytes(b"not-a-library")
            with patch.object(sys, "frozen", True, create=True), patch.object(
                sys, "_MEIPASS", temporary, create=True,
            ), patch.dict(sys.modules, {"tools._release_integrity": None}):
                with self.assertRaisesRegex(RuntimeError, "integrity verifier is missing"):
                    native_library_loader.load_native_library(
                        [path], packaged_name="tools/native.dll",
                        abi_symbol="abi", abi_version=1, label="test native",
                    )


if __name__ == "__main__":
    unittest.main()
