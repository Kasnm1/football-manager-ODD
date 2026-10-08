from __future__ import annotations

import ctypes
import struct
import threading
import time
from ctypes import wintypes
from typing import Any

from fm_collector.win32 import (
    PAGE_EXECUTE_READWRITE, kernel32, open_process, read_process_memory,
    write_process_memory,
)
from tools.initial_data_audit import select_process_layout
from tools.redbull_hook import MEM_COMMIT_RESERVE, MEM_RELEASE, _Code, _matches, _rel32


MULTIPLIER = 3
MAX_FOCUSES = 2048
TABLE_OFFSET = 0x200
TABLE_ENTRY_SIZE = 0x10
CODE_MARKER = b"FMODDAG1"
CODE_PREFIX = b"\xEB" + bytes([len(CODE_MARKER)]) + CODE_MARKER

FM24_HOOK_RVA = 0x29046D4
FM24_SIGNATURE = bytes.fromhex(
    "48 8B 4D F0 48 8B 41 18 48 8B 78 08 48 8B 00 48 29 C7 "
    "0F 84 8F 04 00 00 41 00 DC 48 D1 FF 41 80 FE FE 44 89 65 F8"
)
FM24_ORIGINAL_SIZE = 8
FM24_PATTERN = tuple(FM24_SIGNATURE)

FM26_HOOK_RVA = 0x31749BA
FM26_SIGNATURE = bytes.fromhex(
    "0F 57 C0 0F 2E F0 0F 86 84 01 00 00 F3 0F 10 05 CE B5 1B 01"
)
FM26_ORIGINAL_SIZE = 6
FM26_PATTERN = (
    0x0F, 0x57, 0xC0, 0x0F, 0x2E, 0xF0, 0x0F, 0x86,
    None, None, None, None, 0xF3, 0x0F, 0x10, 0x05,
    None, None, None, None,
)


def _normalized_focuses(focuses: list[dict[str, Any]]) -> list[tuple[int, int]]:
    rows: dict[int, int] = {}
    for focus in focuses:
        raw_address = focus.get("player_address")
        try:
            address = int(raw_address, 0) if isinstance(raw_address, str) else int(raw_address or 0)
            attribute_id = int(focus.get("attribute_id", -1))
        except (TypeError, ValueError):
            continue
        if address > 0xFFFFF and 0 <= attribute_id < 0x45:
            rows[address] = attribute_id
    if len(rows) > MAX_FOCUSES:
        raise ValueError(f"专项训练同时使用人数不能超过{MAX_FOCUSES}")
    return sorted(rows.items())


def _emit_triple_positive_delta(
    code: _Code, load: bytes, store: bytes, label_prefix: str,
) -> None:
    code.emit(load)                              # movsx ecx, byte ptr [delta]
    code.emit(b"\x85\xC9")                    # test ecx,ecx
    code.jump32(b"\x0F\x8E", f"{label_prefix}_done")
    code.emit(b"\x8D\x0C\x49")               # lea ecx,[rcx+rcx*2]
    code.emit(b"\x81\xF9\x7F\x00\x00\x00")
    code.jump32(b"\x0F\x8E", f"{label_prefix}_store")
    code.emit(b"\xB9\x7F\x00\x00\x00")
    code.label(f"{label_prefix}_store")
    code.emit(store)
    code.label(f"{label_prefix}_done")


def _build_fm26_code(
    cave: int, original: bytes, return_address: int, focuses: list[tuple[int, int]],
) -> bytes:
    code = _Code(cave)
    table = cave + TABLE_OFFSET
    code.emit(CODE_PREFIX)
    code.emit(b"\x9C\x50\x51\x52\x57")       # flags,rax,rcx,rdx,rdi
    code.emit(b"\x48\xB8" + struct.pack("<Q", table))
    code.emit(b"\x8B\x10\x48\x83\xC0\x08\x85\xD2")
    code.jump32(b"\x0F\x84", "exit")
    code.label("focus_loop")
    code.emit(b"\x4C\x39\x28")                # cmp [rax],r13
    code.jump32(b"\x0F\x84", "apply")
    code.emit(b"\x48\x83\xC0" + bytes([TABLE_ENTRY_SIZE]) + b"\xFF\xCA")
    code.jump32(b"\x0F\x85", "focus_loop")
    code.jump32(b"\xE9", "exit")
    code.label("apply")
    code.emit(b"\x0F\xB6\x78\x08")            # movzx edi,byte [rax+8]
    _emit_triple_positive_delta(
        code,
        b"\x0F\xBE\x8C\x3D\x70\x03\x00\x00",
        b"\x88\x8C\x3D\x70\x03\x00\x00",
        "delta",
    )
    code.label("exit")
    code.emit(b"\x5F\x5A\x59\x58\x9D")       # rdi,rdx,rcx,rax,flags
    code.emit(original)                          # xorps xmm0,xmm0; ucomiss xmm6,xmm0
    code.emit(b"\xE9")
    code.emit(_rel32(cave + len(code.data) + 4, return_address))
    result = code.finish()
    if len(result) >= TABLE_OFFSET:
        raise RuntimeError("FM26 专项成长代码区布局异常")
    table_data = struct.pack("<II", len(focuses), 0) + b"".join(
        struct.pack("<QB7x", player_address, attribute_id)
        for player_address, attribute_id in focuses
    )
    return result + b"\0" * (TABLE_OFFSET - len(result)) + table_data


def _build_fm24_code(
    cave: int, original: bytes, return_address: int, focuses: list[tuple[int, int]],
) -> bytes:
    code = _Code(cave)
    table = cave + TABLE_OFFSET
    code.emit(CODE_PREFIX)
    code.emit(original)                          # recover vector holder in rax
    code.emit(b"\x9C\x50\x51\x52\x57\x41\x50\x41\x51")
    code.emit(b"\x49\x89\xC1")                # r9 = vector holder
    code.emit(b"\x48\xB8" + struct.pack("<Q", table))
    code.emit(b"\x8B\x10\x48\x83\xC0\x08\x85\xD2")
    code.jump32(b"\x0F\x84", "exit")
    code.label("focus_loop")
    code.emit(b"\x4C\x39\x28")                # cmp [rax],r13
    code.jump32(b"\x0F\x84", "focus_found")
    code.emit(b"\x48\x83\xC0" + bytes([TABLE_ENTRY_SIZE]) + b"\xFF\xCA")
    code.jump32(b"\x0F\x85", "focus_loop")
    code.jump32(b"\xE9", "exit")
    code.label("focus_found")
    code.emit(b"\x0F\xB6\x78\x08")            # movzx edi,byte [rax+8]
    code.emit(b"\x49\x8B\x11")                # mov rdx,[r9] (begin)
    code.emit(b"\x4D\x8B\x41\x08")            # mov r8,[r9+8] (end)
    code.emit(b"\x49\x39\xD0")                # cmp r8,rdx
    code.jump32(b"\x0F\x82", "exit")
    code.emit(b"\x4C\x89\xC1\x48\x29\xD1")
    code.emit(b"\x48\x81\xF9\x8A\x00\x00\x00")
    code.jump32(b"\x0F\x87", "exit")
    code.emit(b"\xF6\xC1\x01")                # pair list has an even byte count
    code.jump32(b"\x0F\x85", "exit")
    code.label("delta_loop")
    code.emit(b"\x49\x39\xD0")
    code.jump32(b"\x0F\x84", "exit")
    code.emit(b"\x40\x38\x3A")                # cmp byte [rdx],dil
    code.jump32(b"\x0F\x85", "advance")
    _emit_triple_positive_delta(
        code, b"\x0F\xBE\x4A\x01", b"\x88\x4A\x01", "delta",
    )
    code.label("advance")
    code.emit(b"\x48\x83\xC2\x02")
    code.jump32(b"\xE9", "delta_loop")
    code.label("exit")
    code.emit(b"\x41\x59\x41\x58\x5F\x5A\x59\x58\x9D\xE9")
    code.emit(_rel32(cave + len(code.data) + 4, return_address))
    result = code.finish()
    if len(result) >= TABLE_OFFSET:
        raise RuntimeError("FM24 专项成长代码区布局异常")
    table_data = struct.pack("<II", len(focuses), 0) + b"".join(
        struct.pack("<QB7x", player_address, attribute_id)
        for player_address, attribute_id in focuses
    )
    return result + b"\0" * (TABLE_OFFSET - len(result)) + table_data


class AttributeGrowthHookController:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.pid = self.hook = self.cave = 0
        self.original = self.patch = b""
        self.layout_key = ""
        self.focuses: list[tuple[int, int]] = []
        self.error: str | None = None

    def status(self) -> dict[str, Any]:
        with self.lock:
            return {
                "installed": bool(self.hook), "focus_count": len(self.focuses),
                "multiplier": MULTIPLIER, "layout": self.layout_key or None,
                "error": self.error,
            }

    def sync(self, focuses: list[dict[str, Any]]) -> dict[str, Any]:
        with self.lock:
            try:
                targets = _normalized_focuses(focuses)
                if not targets:
                    self._uninstall()
                    self.error = None
                    return self.status()
                pid, _path, layout = select_process_layout()
                if layout.key not in {"fm24", "fm26"}:
                    raise RuntimeError("当前 FM 版本尚未验证专项属性成长")
                if (
                    self.hook and (self.pid != pid or self.layout_key != layout.key
                                   or self.focuses != targets)
                ):
                    self._uninstall()
                if not self.hook:
                    self._install(pid, layout, targets)
                self.focuses = targets
                self.error = None
            except Exception as error:
                self.error = str(error)
                if not self.hook:
                    self._clear()
            return self.status()

    def close(self) -> None:
        with self.lock:
            self._uninstall()

    @staticmethod
    def _allocate_near(process: Any, hook: int, size: int = 0x1000) -> int:
        granularity = 0x10000
        origin = hook & ~(granularity - 1)
        for distance in range(granularity, 0x70000000, granularity):
            for hint in (origin + distance, max(granularity, origin - distance)):
                address = kernel32.VirtualAllocEx(
                    process.handle, ctypes.c_void_p(hint), size,
                    MEM_COMMIT_RESERVE, PAGE_EXECUTE_READWRITE,
                )
                value = int(address or 0)
                if value and abs(value - hook) < 0x7FFFFFFF:
                    return value
                if value:
                    kernel32.VirtualFreeEx(
                        process.handle, ctypes.c_void_p(value), 0, MEM_RELEASE,
                    )
        raise RuntimeError("无法在专项成长代码附近分配内存")

    def _install(self, pid: int, layout: Any, focuses: list[tuple[int, int]]) -> None:
        hook_rva, signature, original_size = (
            (FM24_HOOK_RVA, FM24_SIGNATURE, FM24_ORIGINAL_SIZE)
            if layout.key == "fm24"
            else (FM26_HOOK_RVA, FM26_SIGNATURE, FM26_ORIGINAL_SIZE)
        )
        pattern = FM24_PATTERN if layout.key == "fm24" else FM26_PATTERN
        with open_process(pid, write_memory=True) as process:
            module = layout.module(process)
            if not module:
                raise RuntimeError(f"{layout.module_name} 尚未加载")
            hook = module.base_address + hook_rva
            actual = read_process_memory(process, hook, len(signature))
            if actual != signature and self._recover_stale_hook(
                process, hook, actual, signature[:original_size],
            ):
                actual = read_process_memory(process, hook, len(signature))
            if actual != signature:
                hook = self._resolve_pattern_hook(process, module, pattern)
                actual = read_process_memory(process, hook, len(pattern))
            if (
                not actual or len(actual) < original_size
                or any(
                    expected is not None and actual[index] != expected
                    for index, expected in enumerate(pattern)
                )
            ):
                raise RuntimeError("专项属性成长完整特征码不匹配，已拒绝安装")
            original = actual[:original_size]
            required_size = TABLE_OFFSET + 8 + len(focuses) * TABLE_ENTRY_SIZE
            allocation_size = max(0x1000, (required_size + 0xFFF) & ~0xFFF)
            cave = self._allocate_near(process, hook, allocation_size)
            code = (
                _build_fm24_code(cave, original, hook + original_size, focuses)
                if layout.key == "fm24"
                else _build_fm26_code(cave, original, hook + original_size, focuses)
            )
            if len(code) > allocation_size:
                raise RuntimeError("专项属性成长代码超过安全容量")
            write_process_memory(process, cave, code)
            patch = b"\xE9" + _rel32(hook + 5, cave) + b"\x90" * (original_size - 5)
            self._write_code(process, hook, patch)
            self.pid, self.hook, self.cave = pid, hook, cave
            self.original, self.patch = original, patch
            self.layout_key, self.focuses = layout.key, list(focuses)

    @staticmethod
    def _resolve_pattern_hook(process: Any, module: Any, pattern: tuple[int | None, ...]) -> int:
        hits: list[int] = []
        carry = b""
        chunk_size = 8 * 1024 * 1024
        for offset in range(0, int(module.size), chunk_size):
            data = read_process_memory(
                process, int(module.base_address) + offset,
                min(chunk_size, int(module.size) - offset),
            )
            if not data:
                carry = b""
                continue
            scan = carry + data
            base = int(module.base_address) + offset - len(carry)
            for match in _matches(scan, pattern):
                address = base + match
                if int(module.base_address) <= address < int(module.base_address) + int(module.size):
                    hits.append(address)
            carry = scan[-(len(pattern) - 1):]
        hits = sorted(set(hits))
        if len(hits) != 1:
            raise RuntimeError(f"专项属性成长特征码命中 {len(hits)} 处，已拒绝安装")
        return hits[0]

    def _recover_stale_hook(
        self, process: Any, hook: int, actual: bytes | None, original: bytes,
    ) -> bool:
        if not actual or len(actual) < 5 or actual[0] != 0xE9:
            return False
        cave = hook + 5 + struct.unpack_from("<i", actual, 1)[0]
        if read_process_memory(process, cave, len(CODE_PREFIX)) != CODE_PREFIX:
            return False
        self._write_code(process, hook, original)
        time.sleep(0.1)
        kernel32.VirtualFreeEx(
            process.handle, ctypes.c_void_p(cave), 0, MEM_RELEASE,
        )
        return True

    def _uninstall(self) -> None:
        if not self.hook:
            self._clear()
            return
        try:
            with open_process(self.pid, write_memory=True) as process:
                if read_process_memory(process, self.hook, len(self.patch)) == self.patch:
                    self._write_code(process, self.hook, self.original)
                if self.cave:
                    time.sleep(0.1)
                    kernel32.VirtualFreeEx(
                        process.handle, ctypes.c_void_p(self.cave), 0, MEM_RELEASE,
                    )
        except Exception:
            pass
        self._clear()

    def _clear(self) -> None:
        self.pid = self.hook = self.cave = 0
        self.original = self.patch = b""
        self.layout_key = ""
        self.focuses = []

    @staticmethod
    def _write_code(process: Any, address: int, data: bytes) -> None:
        old = wintypes.DWORD(0)
        if not kernel32.VirtualProtectEx(
            process.handle, ctypes.c_void_p(address), len(data),
            PAGE_EXECUTE_READWRITE, ctypes.byref(old),
        ):
            raise RuntimeError("无法修改专项属性成长代码页保护")
        try:
            write_process_memory(process, address, data)
            kernel32.FlushInstructionCache(
                process.handle, ctypes.c_void_p(address), len(data),
            )
        finally:
            restored = wintypes.DWORD(0)
            kernel32.VirtualProtectEx(
                process.handle, ctypes.c_void_p(address), len(data), old.value,
                ctypes.byref(restored),
            )
