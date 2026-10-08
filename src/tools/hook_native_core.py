from __future__ import annotations

import ctypes
import os
import struct
import sys
from pathlib import Path
from typing import Sequence

from tools.native_library_loader import frozen_runtime, load_native_library


ABI_VERSION = 1
WRITE_RESULT_MESSAGES = {
    1: "invalid native Hook write arguments",
    2: "native Hook pre-write read failed",
    3: "native Hook expected bytes mismatch",
    4: "native Hook process-memory write failed",
    5: "native Hook verification failed and the original bytes were restored",
    6: "native Hook verification and rollback both failed",
}


def _candidate_paths() -> list[Path]:
    candidates: list[Path] = []
    frozen_root = getattr(sys, "_MEIPASS", None)
    if frozen_runtime():
        return [
            Path(frozen_root) / "tools" / "fmodd_hook_core.dll",
        ]
    configured = os.environ.get("FMODD_CPP_HOOK_DLL")
    if configured:
        candidates.append(Path(configured))
    root = Path(__file__).resolve().parents[1]
    candidates.append(root / "build" / "cpp_native" / "fmodd_hook_core.dll")
    return candidates


def _load_library() -> tuple[ctypes.CDLL, Path]:
    try:
        return load_native_library(
            _candidate_paths(), packaged_name="tools/fmodd_hook_core.dll",
            abi_symbol="fmodd_hook_abi_version", abi_version=ABI_VERSION,
            label="FMODD C++ Hook core",
        )
    except RuntimeError as error:
        if str(error) == "FMODD C++ Hook core is missing":
            raise RuntimeError(
                "FMODD C++ Hook core is missing; run: "
                "python scripts\\build_cpp_hook_core.py"
            ) from error
        raise


_LIBRARY, _LOADED_PATH = _load_library()
_LIBRARY.fmodd_hook_verified_write.argtypes = [
    ctypes.c_size_t, ctypes.c_size_t,
    ctypes.POINTER(ctypes.c_ubyte), ctypes.POINTER(ctypes.c_ubyte), ctypes.c_size_t,
]
_LIBRARY.fmodd_hook_verified_write.restype = ctypes.c_int32
_LIBRARY.fmodd_hook_rel32.argtypes = [
    ctypes.c_size_t, ctypes.c_size_t, ctypes.POINTER(ctypes.c_int32),
]
_LIBRARY.fmodd_hook_rel32.restype = ctypes.c_int32
_LIBRARY.fmodd_hook_pattern_matches.argtypes = [
    ctypes.POINTER(ctypes.c_ubyte), ctypes.c_size_t,
    ctypes.POINTER(ctypes.c_ubyte), ctypes.POINTER(ctypes.c_ubyte), ctypes.c_size_t,
    ctypes.POINTER(ctypes.c_size_t), ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t),
]
_LIBRARY.fmodd_hook_pattern_matches.restype = ctypes.c_int32


def verified_hook_write(
    process_handle: int, address: int, expected: bytes, replacement: bytes,
) -> None:
    expected = bytes(expected)
    replacement = bytes(replacement)
    if not expected or len(expected) != len(replacement):
        raise ValueError("expected and replacement must be non-empty and equal in length")
    expected_buffer = (ctypes.c_ubyte * len(expected)).from_buffer_copy(expected)
    replacement_buffer = (ctypes.c_ubyte * len(replacement)).from_buffer_copy(replacement)
    code = int(_LIBRARY.fmodd_hook_verified_write(
        int(process_handle), int(address), expected_buffer, replacement_buffer, len(expected),
    ))
    if code:
        raise RuntimeError(WRITE_RESULT_MESSAGES.get(code, f"unknown native Hook write result {code}"))


def rel32(instruction_end: int, target: int) -> bytes:
    output = ctypes.c_int32()
    code = int(_LIBRARY.fmodd_hook_rel32(
        int(instruction_end), int(target), ctypes.byref(output),
    ))
    if code == 2:
        raise OverflowError("relative Hook target exceeds rel32 range")
    if code:
        raise ValueError("invalid relative Hook arguments")
    return struct.pack("<i", output.value)


def pattern_matches(data: bytes, pattern: Sequence[int | None]) -> list[int]:
    data = bytes(data)
    if not data or not pattern:
        return []
    raw_pattern = bytes(0 if value is None else int(value) for value in pattern)
    raw_mask = bytes(0 if value is None else 1 for value in pattern)
    data_buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    pattern_buffer = (ctypes.c_ubyte * len(raw_pattern)).from_buffer_copy(raw_pattern)
    mask_buffer = (ctypes.c_ubyte * len(raw_mask)).from_buffer_copy(raw_mask)
    capacity = min(256, max(1, len(data) - len(pattern) + 1))
    output = (ctypes.c_size_t * capacity)()
    total = ctypes.c_size_t()
    code = int(_LIBRARY.fmodd_hook_pattern_matches(
        data_buffer, len(data), pattern_buffer, mask_buffer, len(pattern),
        output, capacity, ctypes.byref(total),
    ))
    if code == 2:
        capacity = int(total.value)
        output = (ctypes.c_size_t * capacity)()
        code = int(_LIBRARY.fmodd_hook_pattern_matches(
            data_buffer, len(data), pattern_buffer, mask_buffer, len(pattern),
            output, capacity, ctypes.byref(total),
        ))
    if code:
        raise RuntimeError(f"native Hook pattern scan failed (code {code})")
    return [int(output[index]) for index in range(total.value)]
