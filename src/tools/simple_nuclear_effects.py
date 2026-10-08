from __future__ import annotations

import ctypes
import threading
from ctypes import wintypes
from dataclasses import dataclass
from typing import Any

from fm_collector.win32 import PAGE_EXECUTE_READWRITE, find_module, kernel32, open_process, read_process_memory, write_process_memory
from tools.initial_data_audit import GAME_PLUGIN, select_process_layout


Pattern = tuple[int | None, ...]


def _scan(data: bytes, pattern: Pattern) -> list[int]:
    anchor = next(i for i, value in enumerate(pattern) if value is not None)
    needle = bytes([pattern[anchor]])
    rows, cursor = [], 0
    while True:
        hit = data.find(needle, cursor)
        if hit < 0:
            return rows
        base = hit - anchor
        if 0 <= base <= len(data) - len(pattern) and all(value is None or data[base+i] == value for i, value in enumerate(pattern)):
            rows.append(base)
        cursor = hit + 1


@dataclass(frozen=True)
class PatchSpec:
    pattern: Pattern
    offset: int
    replacement: bytes


EFFECTS: dict[str, tuple[PatchSpec, ...]] = {
    "world_work_permit": (
        PatchSpec((0x48,0x39,0xCA,0x0F,0x84,None,None,None,None,0x48,0x89,0xD6), 3, b"\x90\xE9"),
    ),
    "green_card": (
        PatchSpec((None,0x89,0xC1,0x41,0xFF,None,None,0x02,0x00,0x00,0x90,0x48,0x83,0xC4), 3, b"\xEB\x03"),
        PatchSpec((None,0x89,0xC1,0x41,0xFF,None,None,0x02,0x00,0x00,0x90,0x48,0x83,0xC4), 8, b"\xB0\x01"),
        PatchSpec((None,0xC0,None,0x02,None,0x80,None,0x01,0xEB,0x02,None,None,None,None,0x48,0x81,0xC4), 12, b"\xB0\x01"),
    ),
}


class SimpleNuclearController:
    def __init__(self) -> None:
        self.lock = threading.RLock(); self.pid = 0
        self.installed: dict[str, list[tuple[int, bytes, bytes]]] = {}; self.errors: dict[str, str] = {}

    @property
    def supported(self) -> set[str]:
        return set(EFFECTS)

    def status(self) -> dict[str, Any]:
        with self.lock:
            return {"supported": sorted(self.supported), "installed": sorted(self.installed), "errors": dict(self.errors)}

    def sync(self, desired: list[str]) -> dict[str, Any]:
        desired_set = set(desired) & self.supported
        with self.lock:
            for key in list(self.installed):
                if key not in desired_set:
                    self._disable(key)
            for key in sorted(desired_set):
                if key not in self.installed:
                    try:
                        self._enable(key); self.errors.pop(key, None)
                    except Exception as error:
                        self.errors[key] = str(error)
            return self.status()

    def close(self) -> None:
        with self.lock:
            for key in list(self.installed):
                self._disable(key)

    def _enable(self, key: str) -> None:
        pid, _path, layout = select_process_layout()
        if layout.key != "fm26":
            raise RuntimeError("当前 FM 版本不支持该商品效果")
        with open_process(pid, write_memory=True) as process:
            module = find_module(process, GAME_PLUGIN)
            if not module:
                raise RuntimeError("game_plugin.dll 尚未加载")
            image = read_process_memory(process, module.base_address, module.size)
            if not image:
                raise RuntimeError("无法读取 game_plugin.dll")
            resolved = []
            for spec in EFFECTS[key]:
                matches = _scan(image, spec.pattern)
                if len(matches) != 1:
                    raise RuntimeError(f"特征码命中 {len(matches)} 处")
                address = module.base_address + matches[0] + spec.offset
                original = read_process_memory(process, address, len(spec.replacement))
                if not original:
                    raise RuntimeError("无法保存原始指令")
                resolved.append((address, original, spec.replacement))
            applied = []
            try:
                for address, original, replacement in resolved:
                    self._write_code(process, address, replacement); applied.append((address, original, replacement))
            except Exception:
                for address, original, _replacement in reversed(applied):
                    self._write_code(process, address, original)
                raise
            self.pid = pid; self.installed[key] = resolved

    def _disable(self, key: str) -> None:
        records = self.installed.pop(key, [])
        if not records:
            return
        try:
            with open_process(self.pid, write_memory=True) as process:
                for address, original, replacement in reversed(records):
                    if read_process_memory(process, address, len(replacement)) == replacement:
                        self._write_code(process, address, original)
        except Exception:
            pass

    @staticmethod
    def _write_code(process, address: int, data: bytes) -> None:
        old = wintypes.DWORD(0)
        if not kernel32.VirtualProtectEx(process.handle, ctypes.c_void_p(address), len(data), PAGE_EXECUTE_READWRITE, ctypes.byref(old)):
            raise RuntimeError("无法修改代码页保护")
        try:
            write_process_memory(process, address, data)
            kernel32.FlushInstructionCache(process.handle, ctypes.c_void_p(address), len(data))
        finally:
            restored = wintypes.DWORD(0)
            kernel32.VirtualProtectEx(process.handle, ctypes.c_void_p(address), len(data), old.value, ctypes.byref(restored))
