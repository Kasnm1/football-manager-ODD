from __future__ import annotations

import ctypes
import struct
import threading
import time
from ctypes import wintypes
from typing import Any

from fm_collector.win32 import (
    PAGE_EXECUTE_READWRITE, find_module, kernel32, open_process, read_process_memory,
    write_process_memory,
)
from tools.initial_data_audit import (
    GAME_PLUGIN, select_process_layout,
)
from tools.hook_native_core import pattern_matches as _native_pattern_matches, rel32 as _native_rel32


MEM_COMMIT_RESERVE = 0x3000
MEM_RELEASE = 0x8000
STATE_OFFSET = 0x500
TABLE_OFFSET = 0x600
GOALKEEPER_TABLE_OFFSET = 0x800
MAX_PLAYERS = 64
HOOK_PATTERN = (0x44, 0x0F, 0xBE, 0xB4, 0x0B, None, None, 0x00, 0x00, 0xE8)
FM24_HOOK_PATTERN = (0x41, 0x0F, 0xBE, 0xBC, 0x0D, None, None, 0x00, 0x00)
FM24_HOOK_RVA = 0x43B3FDF

# Preserve the original product behavior, but apply it to the match player's
# computed attribute instead of rewriting the persistent 54-byte roster block.
GOALKEEPER_LOW_ATTRIBUTE_IDS = tuple(sorted({
    0x15, 0x16, 0x19, 0x1A, 0x1B, 0x1C, 0x1D, 0x1E, 0x1F, 0x20,
    0x21, 0x22, 0x23, 0x24, 0x25, 0x29, 0x2B, 0x2C, 0x31, 0x33,
    0x36, 0x37, 0x39, 0x3A, 0x3C, 0x3D, 0x42, 0x43, 0x44,
}))
GOALKEEPER_HIGH_ATTRIBUTE_IDS = (0x2F,)


def _matches(data: bytes, pattern: tuple[int | None, ...]) -> list[int]:
    return _native_pattern_matches(data, pattern)


def _rel32(source_after_instruction: int, target: int) -> bytes:
    try:
        return _native_rel32(source_after_instruction, target)
    except OverflowError as error:
        raise RuntimeError("比赛属性跳转距离超出范围") from error


class _Code:
    def __init__(self, base: int):
        self.base, self.data, self.labels, self.patches = base, bytearray(), {}, []

    def emit(self, data: bytes) -> None:
        self.data.extend(data)

    def label(self, name: str) -> None:
        self.labels[name] = len(self.data)

    def jump32(self, opcode: bytes, label: str) -> None:
        self.emit(opcode); position = len(self.data); self.emit(b"\0\0\0\0"); self.patches.append((position, label))

    def finish(self) -> bytes:
        for position, label in self.patches:
            target = self.base + self.labels[label]
            self.data[position:position+4] = _rel32(self.base + position + 4, target)
        return bytes(self.data)


def _emit_target_lookup(
    code: _Code, table: int, prefix: str, hit_label: str, miss_label: str,
) -> None:
    code.emit(b"\x48\xB8" + struct.pack("<Q", table))
    code.emit(b"\x8B\x08\x48\x83\xC0\x04\x85\xC9")
    code.jump32(b"\x0F\x84", miss_label)
    code.label(f"{prefix}_loop")
    code.emit(b"\x3B\x10")
    code.jump32(b"\x0F\x84", hit_label)
    code.emit(b"\x48\x83\xC0\x04\xFF\xC9")
    code.jump32(b"\x0F\x85", f"{prefix}_loop")
    code.jump32(b"\xE9", miss_label)


def _emit_goalkeeper_override(code: _Code, low_write: bytes, high_write: bytes) -> None:
    # RCX was saved at [rsp+8] after pushfq/rax/rcx/rdx. Recover its original
    # low byte, which both verified Trainer generations use as the attribute ID.
    code.emit(b"\x48\x8B\x44\x24\x08")
    for attribute_id in GOALKEEPER_LOW_ATTRIBUTE_IDS:
        code.emit(b"\x3C" + bytes([attribute_id]))
        code.jump32(b"\x0F\x84", "goalkeeper_low")
    for attribute_id in GOALKEEPER_HIGH_ATTRIBUTE_IDS:
        code.emit(b"\x3C" + bytes([attribute_id]))
        code.jump32(b"\x0F\x84", "goalkeeper_high")
    code.jump32(b"\xE9", "exit")
    code.label("goalkeeper_low")
    code.emit(low_write)
    _emit_goalkeeper_hit_record(code)
    code.jump32(b"\xE9", "exit")
    code.label("goalkeeper_high")
    code.emit(high_write)
    _emit_goalkeeper_hit_record(code)
    code.jump32(b"\xE9", "exit")


def _emit_goalkeeper_hit_record(code: _Code) -> None:
    state = code.base + STATE_OFFSET
    code.emit(b"\x48\xB8" + struct.pack("<Q", state))
    code.emit(b"\xF0\x48\xFF\x40\x08")                # total goalkeeper overrides
    code.emit(b"\x89\x50\x10")                          # last person UID
    code.emit(b"\x48\x8B\x4C\x24\x08\x89\x48\x14") # last attribute ID


def _build_code(cave: int, original: bytes, return_address: int) -> bytes:
    code = _Code(cave)
    code.emit(original)
    code.emit(b"\x9C\x50\x51\x52")                      # pushfq; push rax; push rcx; push rdx
    code.emit(b"\x48\xB8" + struct.pack("<Q", cave + STATE_OFFSET))
    code.emit(b"\xF0\x48\xFF\x00")                    # lock inc qword ptr [rax]
    code.emit(b"\x48\x8B\x43\x28\x48\x85\xC0")      # mov rax,[rbx+28]; test rax,rax
    code.jump32(b"\x0F\x84", "exit")
    code.emit(b"\x48\x3D\xFF\xFF\x0F\x00")
    code.jump32(b"\x0F\x86", "exit")
    code.emit(b"\x48\xB9\xFF\xFF\xFF\xFF\xFF\x7F\x00\x00")
    code.emit(b"\x48\x39\xC8")
    code.jump32(b"\x0F\x87", "exit")
    code.emit(b"\x66\x81\x78\x34\x00\x7F")
    code.jump32(b"\x0F\x82", "exit")
    code.emit(b"\x66\x81\x78\x34\xFF\x7F")
    code.jump32(b"\x0F\x87", "exit")
    code.emit(b"\x8B\x50\x0C")                          # person UID
    _emit_target_lookup(
        code, cave + TABLE_OFFSET, "stamina", "stamina_apply", "goalkeeper_lookup",
    )
    code.label("stamina_apply")
    code.emit(b"\xC7\x83\x3C\x1C\x00\x00\x00\x00\x00\x00")
    code.emit(b"\xC7\x83\x40\x1C\x00\x00\x40\x42\x0F\x00")
    code.emit(b"\x66\xC7\x83\x8A\x0C\x00\x00\x10\x27")
    code.emit(b"\x66\xC7\x83\x7A\x0C\x00\x00\x10\x27")
    code.emit(b"\x66\xC7\x83\x84\x0C\x00\x00\x0C\xFE")
    code.label("goalkeeper_lookup")
    _emit_target_lookup(
        code, cave + GOALKEEPER_TABLE_OFFSET, "goalkeeper",
        "goalkeeper_apply", "exit",
    )
    code.label("goalkeeper_apply")
    _emit_goalkeeper_override(
        code,
        b"\x41\xBE\x05\x00\x00\x00",                 # mov r14d,5
        b"\x41\xBE\x64\x00\x00\x00",                 # mov r14d,100
    )
    code.label("exit")
    code.emit(b"\x48\xB8" + struct.pack("<Q", cave + STATE_OFFSET))
    code.emit(b"\xF0\x48\xFF\x08")                    # lock dec qword ptr [rax]
    code.emit(b"\x5A\x59\x58\x9D\xE9")
    code.emit(_rel32(cave + len(code.data) + 4, return_address))
    result = code.finish()
    if len(result) >= TABLE_OFFSET:
        raise RuntimeError("比赛属性代码区布局异常")
    return result


def _build_fm24_code(cave: int, original: bytes, return_address: int) -> bytes:
    code = _Code(cave)
    code.emit(original)
    code.emit(b"\x9C\x50\x51\x52")
    code.emit(b"\x48\xB8" + struct.pack("<Q", cave + STATE_OFFSET))
    code.emit(b"\xF0\x48\xFF\x00")
    code.emit(b"\x49\x8B\x45\x28\x48\x85\xC0")
    code.jump32(b"\x0F\x84", "exit")
    code.emit(b"\x48\x3D\xFF\xFF\x0F\x00")
    code.jump32(b"\x0F\x86", "exit")
    code.emit(b"\x48\xB9\xFF\xFF\xFF\xFF\xFF\x7F\x00\x00")
    code.emit(b"\x48\x39\xC8")
    code.jump32(b"\x0F\x87", "exit")
    code.emit(b"\x8B\x50\x0C")
    _emit_target_lookup(
        code, cave + TABLE_OFFSET, "stamina", "stamina_apply", "goalkeeper_lookup",
    )
    code.label("stamina_apply")
    # FM2024 Ver1.2 Trainer live match-player fields.
    code.emit(b"\x41\xC7\x85\x40\x02\x00\x00\x40\x42\x0F\x00")
    for offset, value in (
        (0x2CA, 10000), (0x2CC, 10000), (0x2CE, 10000), (0x2D0, -500),
    ):
        code.emit(b"\x66\x41\xC7\x85" + struct.pack("<Ih", offset, value))
    code.emit(b"\x41\xC6\x85\xBA\x06\x00\x00\x14")
    code.label("goalkeeper_lookup")
    _emit_target_lookup(
        code, cave + GOALKEEPER_TABLE_OFFSET, "goalkeeper",
        "goalkeeper_apply", "exit",
    )
    code.label("goalkeeper_apply")
    _emit_goalkeeper_override(
        code,
        b"\xBF\x05\x00\x00\x00",                       # mov edi,5
        b"\xBF\x64\x00\x00\x00",                       # mov edi,100
    )
    code.label("exit")
    code.emit(b"\x48\xB8" + struct.pack("<Q", cave + STATE_OFFSET))
    code.emit(b"\xF0\x48\xFF\x08")
    code.emit(b"\x5A\x59\x58\x9D\xE9")
    code.emit(_rel32(cave + len(code.data) + 4, return_address))
    result = code.finish()
    if len(result) >= TABLE_OFFSET:
        raise RuntimeError("FM2024 比赛属性代码区布局异常")
    return result


class RedBullHookController:
    def __init__(self) -> None:
        self.lock = threading.RLock(); self.pid = 0; self.hook = 0; self.cave = 0
        self.original = b""; self.patch = b""; self.targets: list[int] = []
        self.goalkeeper_targets: list[int] = []
        self.error: str | None = None
        self.layout_key = ""
        self.goalkeeper_override_count = 0
        self.last_goalkeeper_uid = self.last_goalkeeper_attribute = 0

    def _refresh_counters(self) -> None:
        if not self.pid or not self.cave:
            return
        try:
            with open_process(self.pid) as process:
                state = read_process_memory(process, self.cave + STATE_OFFSET + 8, 16)
            if not state or len(state) != 16:
                return
            self.goalkeeper_override_count = struct.unpack_from("<Q", state, 0)[0]
            self.last_goalkeeper_uid = struct.unpack_from("<I", state, 8)[0]
            self.last_goalkeeper_attribute = struct.unpack_from("<I", state, 12)[0] & 0xFF
        except Exception:
            pass

    def status(self) -> dict[str, Any]:
        with self.lock:
            self._refresh_counters()
            return {
                "installed": bool(self.hook),
                "targets": list(self.targets),
                "goalkeeper_targets": list(self.goalkeeper_targets),
                "goalkeeper_override_count": self.goalkeeper_override_count,
                "last_goalkeeper_uid": self.last_goalkeeper_uid or None,
                "last_goalkeeper_attribute": (
                    hex(self.last_goalkeeper_attribute)
                    if self.last_goalkeeper_attribute else None
                ),
                "error": self.error,
            }

    def sync(
        self, player_ids: list[int], goalkeeper_player_ids: list[int] | None = None,
    ) -> dict[str, Any]:
        targets = sorted({int(value) for value in player_ids if int(value) > 0})[:MAX_PLAYERS]
        goalkeeper_targets = sorted({
            int(value) for value in (goalkeeper_player_ids or []) if int(value) > 0
        })[:MAX_PLAYERS]
        with self.lock:
            try:
                if not targets and not goalkeeper_targets:
                    self._uninstall(); self.error = None; return self.status()
                pid, _path, layout = select_process_layout()
                if layout.key not in {"fm24", "fm26"}:
                    raise RuntimeError("当前 FM 版本不支持比赛属性")
                if self.pid and (self.pid != pid or self.layout_key != layout.key):
                    self._clear()
                if not self.hook:
                    self._install(pid, layout)
                self._write_targets(targets, goalkeeper_targets); self.error = None
            except Exception as error:
                self.error = str(error)
                if not self.hook:
                    self._clear()
            return self.status()

    def close(self) -> None:
        with self.lock:
            self._uninstall()

    def _allocate_near(self, process, hook: int, size: int = 0x1000) -> int:
        granularity = 0x10000; origin = hook & ~(granularity - 1)
        for distance in range(granularity, 0x70000000, granularity):
            for hint in (origin + distance, max(granularity, origin - distance)):
                address = kernel32.VirtualAllocEx(process.handle, ctypes.c_void_p(hint), size, MEM_COMMIT_RESERVE, PAGE_EXECUTE_READWRITE)
                value = int(address or 0)
                if value and abs(value - hook) < 0x7FFFFFFF:
                    return value
                if value:
                    kernel32.VirtualFreeEx(process.handle, ctypes.c_void_p(value), 0, MEM_RELEASE)
        raise RuntimeError("无法在比赛代码附近分配比赛属性内存")

    def _install(self, pid: int, layout: Any) -> None:
        layout_key = str(layout.key)
        with open_process(pid, write_memory=True) as process:
            module_name = "fm.exe" if layout_key == "fm24" else GAME_PLUGIN
            module = find_module(process, module_name)
            if not module:
                raise RuntimeError(f"{module_name} 尚未加载")
            if layout_key == "fm24":
                hook_rva = layout.redbull_hook_rva
                if hook_rva is None:
                    raise RuntimeError("当前 FM2024 分发版本未映射比赛属性")
                hook = module.base_address + int(hook_rva)
                original = read_process_memory(process, hook, len(FM24_HOOK_PATTERN)) or b""
                if len(original) != len(FM24_HOOK_PATTERN) or any(
                    expected is not None and original[index] != expected
                    for index, expected in enumerate(FM24_HOOK_PATTERN)
                ):
                    raise RuntimeError("FM2024 比赛属性入口已被修改")
            else:
                image = read_process_memory(process, module.base_address, module.size)
                if not image:
                    raise RuntimeError("无法读取 game_plugin.dll")
                offsets = _matches(image, HOOK_PATTERN)
                if len(offsets) != 1:
                    raise RuntimeError(f"比赛属性特征码命中 {len(offsets)} 处，已拒绝安装")
                hook = module.base_address + offsets[0]
                original = image[offsets[0]:offsets[0]+9]
            cave = self._allocate_near(process, hook)
            code = (
                _build_fm24_code(cave, original, hook + len(original))
                if layout_key == "fm24" else _build_code(cave, original, hook + 9)
            )
            write_process_memory(process, cave, code)
            write_process_memory(process, cave + STATE_OFFSET, b"\0" * 8)
            write_process_memory(process, cave + TABLE_OFFSET, b"\0" * (4 + MAX_PLAYERS * 4))
            write_process_memory(
                process, cave + GOALKEEPER_TABLE_OFFSET, b"\0" * (4 + MAX_PLAYERS * 4),
            )
            kernel32.FlushInstructionCache(process.handle, ctypes.c_void_p(cave), len(code))
            patch = b"\xE9" + _rel32(hook + 5, cave) + b"\x90" * 4
            old = wintypes.DWORD(0)
            if not kernel32.VirtualProtectEx(process.handle, ctypes.c_void_p(hook), 9, PAGE_EXECUTE_READWRITE, ctypes.byref(old)):
                kernel32.VirtualFreeEx(process.handle, ctypes.c_void_p(cave), 0, MEM_RELEASE)
                raise RuntimeError("无法修改比赛属性入口保护")
            try:
                write_process_memory(process, hook, patch)
                kernel32.FlushInstructionCache(process.handle, ctypes.c_void_p(hook), 9)
            finally:
                restored = wintypes.DWORD(0)
                kernel32.VirtualProtectEx(process.handle, ctypes.c_void_p(hook), 9, old.value, ctypes.byref(restored))
            self.pid, self.hook, self.cave, self.original, self.patch = pid, hook, cave, original, patch
            self.layout_key = layout_key

    def _write_targets(
        self, targets: list[int], goalkeeper_targets: list[int],
    ) -> None:
        with open_process(self.pid, write_memory=True) as process:
            table = self.cave + TABLE_OFFSET
            goalkeeper_table = self.cave + GOALKEEPER_TABLE_OFFSET
            write_process_memory(process, table, struct.pack("<I", 0))
            write_process_memory(process, goalkeeper_table, struct.pack("<I", 0))
            write_process_memory(process, table + 4, b"".join(struct.pack("<I", value) for value in targets))
            write_process_memory(
                process, goalkeeper_table + 4,
                b"".join(struct.pack("<I", value) for value in goalkeeper_targets),
            )
            write_process_memory(process, table, struct.pack("<I", len(targets)))
            write_process_memory(
                process, goalkeeper_table, struct.pack("<I", len(goalkeeper_targets)),
            )
        self.targets = targets
        self.goalkeeper_targets = goalkeeper_targets

    def _uninstall(self) -> None:
        if not self.hook:
            self._clear(); return
        cave_can_be_freed = False
        try:
            with open_process(self.pid, write_memory=True) as process:
                current = read_process_memory(process, self.hook, 9)
                entry_restored = current == self.original
                if current == self.patch:
                    old = wintypes.DWORD(0)
                    if kernel32.VirtualProtectEx(process.handle, ctypes.c_void_p(self.hook), 9, PAGE_EXECUTE_READWRITE, ctypes.byref(old)):
                        write_process_memory(process, self.hook, self.original)
                        kernel32.FlushInstructionCache(process.handle, ctypes.c_void_p(self.hook), 9)
                        restored = wintypes.DWORD(0)
                        kernel32.VirtualProtectEx(process.handle, ctypes.c_void_p(self.hook), 9, old.value, ctypes.byref(restored))
                        entry_restored = read_process_memory(process, self.hook, 9) == self.original
                if entry_restored:
                    deadline = time.monotonic() + 1.0
                    time.sleep(0.05)
                    consecutive_zero = 0
                    while time.monotonic() < deadline:
                        active = read_process_memory(process, self.cave + STATE_OFFSET, 8)
                        if active == b"\0" * 8:
                            consecutive_zero += 1
                            if consecutive_zero >= 3:
                                cave_can_be_freed = True
                                break
                        else:
                            consecutive_zero = 0
                        if not active or len(active) != 8:
                            break
                        time.sleep(0.01)
                if cave_can_be_freed and self.cave:
                    kernel32.VirtualFreeEx(process.handle, ctypes.c_void_p(self.cave), 0, MEM_RELEASE)
        except Exception:
            pass
        self._clear()

    def _clear(self) -> None:
        self.pid = self.hook = self.cave = 0; self.original = self.patch = b""; self.targets = []
        self.goalkeeper_targets = []
        self.layout_key = ""
        self.goalkeeper_override_count = 0
        self.last_goalkeeper_uid = self.last_goalkeeper_attribute = 0
