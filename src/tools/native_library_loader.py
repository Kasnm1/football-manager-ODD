from __future__ import annotations

import ctypes
import os
import sys
from ctypes import wintypes
from pathlib import Path
from typing import Iterable


GENERIC_READ = 0x80000000
FILE_SHARE_READ = 0x00000001
OPEN_EXISTING = 3
FILE_ATTRIBUTE_NORMAL = 0x00000080
LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR = 0x00000100
LOAD_LIBRARY_SEARCH_SYSTEM32 = 0x00000800
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.CreateFileW.argtypes = [
    wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
    wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
]
_kernel32.CreateFileW.restype = wintypes.HANDLE
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
_kernel32.CloseHandle.restype = wintypes.BOOL
_kernel32.GetModuleFileNameW.argtypes = [wintypes.HMODULE, wintypes.LPWSTR, wintypes.DWORD]
_kernel32.GetModuleFileNameW.restype = wintypes.DWORD

# These handles intentionally remain open for the lifetime of the process.
# FILE_SHARE_READ allows hashing and dependency loading but denies replacement,
# deletion, and write access while native code is active.
_LOCKED_FILE_HANDLES: list[int] = []


def frozen_runtime() -> bool:
    return bool(getattr(sys, "frozen", False) and getattr(sys, "_MEIPASS", None))


def _canonical(path: Path) -> str:
    return os.path.normcase(str(path.resolve(strict=True)))


def _lock_for_process_lifetime(path: Path) -> int:
    handle = _kernel32.CreateFileW(
        str(path), GENERIC_READ, FILE_SHARE_READ, None,
        OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, None,
    )
    value = int(handle or 0)
    if not value or value == INVALID_HANDLE_VALUE:
        raise ctypes.WinError(ctypes.get_last_error())
    return value


def _loaded_module_path(library: ctypes.CDLL) -> str:
    buffer = ctypes.create_unicode_buffer(32768)
    length = int(_kernel32.GetModuleFileNameW(
        wintypes.HMODULE(library._handle), buffer, len(buffer),
    ))
    if not length or length >= len(buffer):
        raise ctypes.WinError(ctypes.get_last_error())
    return os.path.normcase(str(Path(buffer.value).resolve(strict=True)))


def _verify_frozen_library(path: Path, packaged_name: str) -> None:
    if not frozen_runtime():
        return
    try:
        from tools._release_integrity import verify_native_library
    except ImportError as error:
        raise RuntimeError("FMODD native integrity verifier is missing") from error
    errors = list(verify_native_library(str(path), packaged_name))
    if errors:
        raise RuntimeError("; ".join(errors))


def load_native_library(
    candidates: Iterable[Path], *, packaged_name: str,
    abi_symbol: str, abi_version: int, label: str,
) -> tuple[ctypes.CDLL, Path]:
    for candidate in candidates:
        try:
            path = candidate.resolve(strict=True)
        except OSError:
            continue
        if not path.is_file():
            continue
        lock_handle = _lock_for_process_lifetime(path)
        try:
            _verify_frozen_library(path, packaged_name)
            library = ctypes.CDLL(
                str(path),
                winmode=(
                    LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR
                    | LOAD_LIBRARY_SEARCH_SYSTEM32
                ),
            )
            if _loaded_module_path(library) != _canonical(path):
                raise RuntimeError(f"{label} loaded from an unexpected path")
            abi = getattr(library, abi_symbol)
            abi.argtypes = []
            abi.restype = ctypes.c_uint32
            actual = int(abi())
            if actual != int(abi_version):
                raise RuntimeError(
                    f"{label} ABI mismatch: expected {abi_version}, got {actual}"
                )
        except Exception:
            _kernel32.CloseHandle(wintypes.HANDLE(lock_handle))
            raise
        _LOCKED_FILE_HANDLES.append(lock_handle)
        return library, path
    raise RuntimeError(f"{label} is missing")
