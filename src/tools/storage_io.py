from __future__ import annotations

import ctypes
import hashlib
import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any


_LOCKS_GUARD = threading.Lock()
_LOCKS: dict[str, "StorageRLock"] = {}


def _lock_identity(path: Path) -> str:
    resolved = os.path.normcase(str(Path(path).resolve(strict=False)))
    return hashlib.sha256(resolved.encode("utf-8")).hexdigest()


class StorageRLock:
    """Re-entrant process and Windows-session lock for one storage target."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._thread_lock = threading.RLock()
        self._local = threading.local()

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        acquired = self._thread_lock.acquire(blocking, timeout)
        if not acquired:
            return False
        handle = 0
        try:
            if os.name == "nt":
                kernel32 = ctypes.windll.kernel32
                kernel32.CreateMutexW.argtypes = [
                    ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p,
                ]
                kernel32.CreateMutexW.restype = ctypes.c_void_p
                kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
                kernel32.WaitForSingleObject.restype = ctypes.c_uint32
                handle = int(kernel32.CreateMutexW(
                    None, False, f"Local\\FMODD-Storage-{_lock_identity(self.path)}",
                ) or 0)
                if not handle:
                    raise OSError("无法创建 FMODD 存储互斥锁")
                wait_ms = 0xFFFFFFFF if blocking and timeout < 0 else max(
                    0, int(timeout * 1000) if timeout >= 0 else 0,
                )
                result = int(kernel32.WaitForSingleObject(handle, wait_ms))
                if result not in {0x00000000, 0x00000080}:
                    kernel32.CloseHandle(handle)
                    handle = 0
                    if result == 0x00000102:
                        self._thread_lock.release()
                        return False
                    raise OSError(f"无法取得 FMODD 存储互斥锁：{result}")
            handles = getattr(self._local, "handles", None)
            if handles is None:
                handles = []
                self._local.handles = handles
            handles.append(handle)
            return True
        except Exception:
            self._thread_lock.release()
            raise

    def release(self) -> None:
        handles = getattr(self._local, "handles", None)
        if not handles:
            raise RuntimeError("FMODD 存储锁未持有")
        handle = int(handles.pop() or 0)
        try:
            if handle and os.name == "nt":
                kernel32 = ctypes.windll.kernel32
                kernel32.ReleaseMutex.argtypes = [ctypes.c_void_p]
                kernel32.ReleaseMutex.restype = ctypes.c_bool
                kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
                kernel32.CloseHandle.restype = ctypes.c_bool
                kernel32.ReleaseMutex(handle)
                kernel32.CloseHandle(handle)
        finally:
            self._thread_lock.release()

    def __enter__(self) -> "StorageRLock":
        self.acquire()
        return self

    def __exit__(self, _type: Any, _value: Any, _traceback: Any) -> None:
        self.release()


def storage_lock(path: Path) -> StorageRLock:
    identity = _lock_identity(Path(path))
    with _LOCKS_GUARD:
        lock = _LOCKS.get(identity)
        if lock is None:
            lock = StorageRLock(Path(path))
            _LOCKS[identity] = lock
        return lock


def atomic_write_bytes(path: Path, payload: bytes) -> None:
    """Durably write bytes through a unique temporary file and atomic replace."""
    path = Path(path)
    with storage_lock(path):
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(
            f".{path.name}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex}.tmp"
        )
        try:
            with temporary.open("wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            for attempt in range(20):
                try:
                    os.replace(temporary, path)
                    break
                except PermissionError:
                    if attempt == 19:
                        raise
                    time.sleep(0.05 * (attempt + 1))
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def atomic_write_text(path: Path, payload: str) -> None:
    atomic_write_bytes(Path(path), str(payload).encode("utf-8"))


def atomic_write_json(
    path: Path, payload: Any, *, indent: int | None = None,
) -> None:
    atomic_write_text(
        Path(path),
        json.dumps(
            payload, ensure_ascii=False, indent=indent,
            separators=None if indent is not None else (",", ":"),
        ),
    )


__all__ = [
    "StorageRLock", "atomic_write_bytes", "atomic_write_json",
    "atomic_write_text", "storage_lock",
]
