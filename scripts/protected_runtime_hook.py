"""Fail closed before the protected desktop entry imports application modules."""

from __future__ import annotations

import ctypes
import sys
from pathlib import Path


def _abort(errors: list[str]) -> None:
    details = "\n".join(errors)
    ctypes.windll.user32.MessageBoxW(
        None,
        "FMODD protected files failed integrity verification.\n\n" + details,
        "FMODD",
        0x10,
    )
    raise SystemExit(1)


try:
    from tools._release_integrity import verify_packaged_files
except ImportError:
    _abort(["tools/_release_integrity is missing from the protected runtime"])

asset_root = getattr(sys, "_MEIPASS", None)
if not asset_root:
    _abort(["protected runtime extraction root is unavailable"])

integrity_errors = list(verify_packaged_files(str(Path(asset_root))))
if integrity_errors:
    _abort(integrity_errors)

sys._fmodd_release_integrity_verified = True
