from __future__ import annotations

import ctypes
import os
import sys
from pathlib import Path
from typing import Sequence

from tools.native_library_loader import frozen_runtime, load_native_library


ABI_VERSION = 1
WRITE_RESULT_MESSAGES = {
    1: "invalid native write arguments",
    2: "native pre-write read failed",
    3: "native expected bytes mismatch",
    4: "native process-memory write failed",
    5: "native write verification failed and the original bytes were restored",
    6: "native write verification and rollback both failed",
}


class NativeWriteError(RuntimeError):
    def __init__(self, code: int):
        self.code = int(code)
        super().__init__(
            WRITE_RESULT_MESSAGES.get(self.code, f"unknown native write result {self.code}")
        )


def _candidate_paths() -> list[Path]:
    candidates: list[Path] = []
    frozen_root = getattr(sys, "_MEIPASS", None)
    if frozen_runtime():
        return [
            Path(frozen_root) / "tools" / "fmodd_native_core.dll",
        ]
    configured = os.environ.get("FMODD_RUST_NATIVE_DLL")
    if configured:
        candidates.append(Path(configured))
    root = Path(__file__).resolve().parents[1]
    candidates.extend([
        root / "fmodd_native_core.dll",
        root / "build" / "rust_native" / "fmodd_native_core.dll",
        root / "native" / "fmodd_native_core" / "target" / "release" / "fmodd_native_core.dll",
    ])
    return candidates


def _load_library() -> tuple[ctypes.CDLL, Path]:
    try:
        return load_native_library(
            _candidate_paths(), packaged_name="tools/fmodd_native_core.dll",
            abi_symbol="fmodd_native_abi_version", abi_version=ABI_VERSION,
            label="FMODD Rust native core",
        )
    except RuntimeError as error:
        if str(error) == "FMODD Rust native core is missing":
            raise RuntimeError(
                "FMODD Rust native core is missing; run: "
                "python scripts\\build_rust_native.py"
            ) from error
        raise


_LIBRARY, _LOADED_PATH = _load_library()
_LIBRARY.fmodd_poisson.argtypes = [ctypes.c_int32, ctypes.c_double, ctypes.POINTER(ctypes.c_double)]
_LIBRARY.fmodd_poisson.restype = ctypes.c_int32
_LIBRARY.fmodd_decode_vector_header.argtypes = [
    ctypes.POINTER(ctypes.c_ubyte), ctypes.c_size_t,
    ctypes.c_uint64, ctypes.c_uint64, ctypes.c_uint64,
    ctypes.c_uint32, ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint64),
]
_LIBRARY.fmodd_decode_vector_header.restype = ctypes.c_int32
_LIBRARY.fmodd_score_matrix.argtypes = [
    ctypes.c_double, ctypes.c_double, ctypes.c_size_t, ctypes.c_double,
    ctypes.POINTER(ctypes.c_double), ctypes.c_size_t,
]
_LIBRARY.fmodd_score_matrix.restype = ctypes.c_int32
_LIBRARY.fmodd_market_weights.argtypes = [
    ctypes.POINTER(ctypes.c_double), ctypes.c_size_t, ctypes.c_size_t,
    ctypes.c_double, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32,
    ctypes.POINTER(ctypes.c_double),
]
_LIBRARY.fmodd_market_weights.restype = ctypes.c_int32
_LIBRARY.fmodd_verified_write.argtypes = [
    ctypes.c_size_t, ctypes.c_size_t,
    ctypes.POINTER(ctypes.c_ubyte), ctypes.POINTER(ctypes.c_ubyte), ctypes.c_size_t,
    ctypes.c_uint32,
]
_LIBRARY.fmodd_verified_write.restype = ctypes.c_int32


def poisson(k: int, mean: float) -> float:
    output = ctypes.c_double()
    code = _LIBRARY.fmodd_poisson(int(k), float(mean), ctypes.byref(output))
    if code:
        raise ValueError("invalid Poisson inputs")
    return float(output.value)


def decode_vector_header(
    raw: bytes,
    item_size: int,
    max_count: int,
    max_capacity_count: int = 0,
    allow_null_empty: bool = False,
    require_alignment: bool = True,
) -> tuple[int, int, int, int, int] | None:
    if not isinstance(raw, bytes) or len(raw) != 0x18 or int(item_size) <= 0:
        return None
    source = (ctypes.c_ubyte * len(raw)).from_buffer_copy(raw)
    output = (ctypes.c_uint64 * 5)()
    code = int(_LIBRARY.fmodd_decode_vector_header(
        source, len(raw), int(item_size), int(max_count), int(max_capacity_count),
        int(allow_null_empty), int(require_alignment), output,
    ))
    if code:
        return None
    return tuple(int(value) for value in output)


def score_matrix(home_xg: float, away_xg: float, maximum: int, rho: float) -> list[list[float]]:
    maximum = int(maximum)
    if maximum < 1 or maximum > 64:
        raise ValueError("maximum must be between 1 and 64")
    side = maximum + 1
    flat = (ctypes.c_double * (side * side))()
    code = _LIBRARY.fmodd_score_matrix(
        float(home_xg), float(away_xg), maximum, float(rho), flat, len(flat),
    )
    if code:
        raise ValueError(f"invalid score-matrix inputs (native code {code})")
    return [list(flat[offset:offset + side]) for offset in range(0, len(flat), side)]


def market_weights(
    matrix: Sequence[Sequence[float]], line: float, market: int, side: int, over: int,
) -> tuple[float, float, float]:
    rows = len(matrix)
    columns = len(matrix[0]) if rows else 0
    if not rows or not columns or any(len(row) != columns for row in matrix):
        raise ValueError("matrix must be non-empty and rectangular")
    values = [float(value) for row in matrix for value in row]
    flat = (ctypes.c_double * len(values))(*values)
    output = (ctypes.c_double * 3)()
    code = _LIBRARY.fmodd_market_weights(
        flat, rows, columns, float(line), int(market), int(side), int(over), output,
    )
    if code:
        raise ValueError(f"invalid market-weight inputs (native code {code})")
    return float(output[0]), float(output[1]), float(output[2])


def verified_write(
    process_handle: int,
    address: int,
    expected: bytes,
    replacement: bytes,
    *,
    flush_instruction_cache: bool = False,
) -> None:
    expected = bytes(expected)
    replacement = bytes(replacement)
    if not expected or len(expected) != len(replacement):
        raise ValueError("expected and replacement must be non-empty and equal in length")
    expected_buffer = (ctypes.c_ubyte * len(expected)).from_buffer_copy(expected)
    replacement_buffer = (ctypes.c_ubyte * len(replacement)).from_buffer_copy(replacement)
    code = int(_LIBRARY.fmodd_verified_write(
        int(process_handle), int(address), expected_buffer, replacement_buffer,
        len(expected), int(flush_instruction_cache),
    ))
    if code:
        raise NativeWriteError(code)
