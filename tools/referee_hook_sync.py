from __future__ import annotations

import ctypes
import struct
import threading
import time
from ctypes import wintypes
from typing import Any

from fm_collector.win32 import (
    PAGE_EXECUTE_READWRITE, find_module, kernel32, open_process,
    read_process_memory, write_process_memory,
)
from tools.initial_data_audit import (
    GAME_PLUGIN, select_process_layout,
)
from tools.redbull_hook import MEM_COMMIT_RESERVE, MEM_RELEASE, _Code, _matches, _rel32


REFEREE_PATTERN = (
    0x44, 0x88, 0x8E, 0x62, 0x02, 0x00, 0x00,
    0x44, 0x88, 0x9E, 0x60, 0x02, 0x00, 0x00,
    0x44, 0x88, 0x96, 0x61, 0x02, 0x00, 0x00,
)
FM24_REFEREE_PATTERN = (
    0x0F, 0x2E, None, 0x0F, 0x83, None, None, 0x00, 0x00,
    0x48, 0x8B, None, None, None, 0x00, 0x00,
    0x48, 0x85, 0xFF, 0x0F, 0x84,
)
FM24_REFEREE_AOB_RVA = 0x1A0AC338

REFEREE_HOOK_REVISION = 8
REFEREE_ORIGINAL_SIZE = 21
REFEREE_PATCH_TAIL = b"\x90" * (REFEREE_ORIGINAL_SIZE - 5) + b"\x44\x88\xAE\x56\x02\x00\x00"
HANDLER_OFFSET = 0x600
CALL_STUB_OFFSET = 0x700
TABLE_OFFSET = 0x800
VEH_RESULT_OFFSET = 0xF0
WAIT_OBJECT_0 = 0
INFINITE_TIMEOUT_MS = 5000


kernel32.CreateRemoteThread.argtypes = [
    wintypes.HANDLE, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p,
    ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
]
kernel32.CreateRemoteThread.restype = wintypes.HANDLE
kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
kernel32.WaitForSingleObject.restype = wintypes.DWORD
kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
kernel32.GetModuleHandleW.restype = ctypes.c_void_p
kernel32.GetProcAddress.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
kernel32.GetProcAddress.restype = ctypes.c_void_p


def _proc_address(name: bytes) -> int:
    module = kernel32.GetModuleHandleW("kernel32.dll")
    address = kernel32.GetProcAddress(module, name)
    if not address:
        raise RuntimeError(f"无法解析 Windows 函数 {name.decode('ascii')}")
    return int(address)


def _build_handler(base: int, protected_start: int, protected_end: int, exit_address: int, table: int) -> bytes:
    code = _Code(base)
    code.emit(b"\x48\x8B\x01")                                      # exception record
    code.emit(b"\x81\x38\x05\x00\x00\xC0")                  # access violation
    code.jump32(b"\x0F\x85", "search")
    code.emit(b"\x48\x8B\x50\x10")                              # exception address
    code.emit(b"\x48\xB8" + struct.pack("<Q", protected_start))
    code.emit(b"\x48\x39\xC2")
    code.jump32(b"\x0F\x82", "search")
    code.emit(b"\x48\xB8" + struct.pack("<Q", protected_end))
    code.emit(b"\x48\x39\xC2")
    code.jump32(b"\x0F\x83", "search")
    code.emit(b"\x48\xB8" + struct.pack("<Q", table + 0x38))
    code.emit(b"\xF0\x48\xFF\x00")                              # handled exception count
    code.emit(b"\x48\x8B\x41\x08")                              # context record
    code.emit(b"\x48\xBA" + struct.pack("<Q", exit_address))
    code.emit(b"\x48\x89\x90\xF8\x00\x00\x00")          # CONTEXT.Rip
    code.emit(b"\xB8\xFF\xFF\xFF\xFF\xC3")                  # continue execution
    code.label("search")
    code.emit(b"\x31\xC0\xC3")                                  # continue search
    return code.finish()


def _build_hook(
    cave: int, original: bytes, return_address: int, level: int, club_address: int = 0,
) -> tuple[bytes, int, int, int]:
    code = _Code(cave)
    table = cave + TABLE_OFFSET
    code.emit(original)
    code.emit(b"\x41\x50\x41\x51\x9C")                       # preserve r8/r9/flags
    code.emit(b"\x49\xB9" + struct.pack("<Q", table))
    code.emit(b"\x49\x89\x59\x08\x49\xFF\x01")           # last context and sequence
    code.emit(b"\x45\x31\xC0")
    code.emit(b"\x44\x8A\x86\x60\x02\x00\x00")
    code.emit(b"\x45\x89\x41\x40")                             # committed primary value
    code.emit(b"\x44\x8A\x86\x61\x02\x00\x00")
    code.emit(b"\x45\x89\x41\x44")                             # committed secondary value
    code.emit(b"\x48\x81\xFB\xFF\xFF\x0F\x00")
    code.jump32(b"\x0F\x86", "exit")
    code.label("protected_start")

    code.emit(b"\x4C\x8B\x83\x88\x02\x00\x00")           # participant 288
    code.emit(b"\x4D\x89\x41\x10")
    code.emit(b"\x49\x81\xF8\xFF\xFF\x0F\x00")
    code.jump32(b"\x0F\x86", "exit")
    code.emit(b"\x4D\x8B\x80\xA8\x00\x00\x00")           # participant -> team
    code.emit(b"\x4D\x3B\x81\x80\x00\x00\x00")
    code.jump32(b"\x0F\x84", "managed")
    if club_address:
        code.emit(b"\x4D\x85\xC0")
        code.jump32(b"\x0F\x84", "check_opponent")
        code.emit(b"\x4D\x8B\x40\x30")                         # team -> club
        code.emit(b"\x4D\x3B\x81\x88\x00\x00\x00")
        code.jump32(b"\x0F\x84", "managed")

    code.label("check_opponent")
    if level == 1:
        code.jump32(b"\xE9", "exit")
    else:
        code.emit(b"\x4C\x8B\x83\x90\x02\x00\x00")       # participant 290
        code.emit(b"\x4D\x89\x41\x18")
        code.emit(b"\x49\x81\xF8\xFF\xFF\x0F\x00")
        code.jump32(b"\x0F\x86", "exit")
        code.emit(b"\x4D\x8B\x80\xA8\x00\x00\x00")
        code.emit(b"\x4D\x3B\x81\x80\x00\x00\x00")
        code.jump32(b"\x0F\x84", "opponent")
        if club_address:
            code.emit(b"\x4D\x85\xC0")
            code.jump32(b"\x0F\x84", "exit")
            code.emit(b"\x4D\x8B\x40\x30")
            code.emit(b"\x4D\x3B\x81\x88\x00\x00\x00")
            code.jump32(b"\x0F\x84", "opponent")
        code.jump32(b"\xE9", "exit")

    code.label("managed")
    code.emit(b"\x49\xFF\x41\x20")
    code.emit(b"\xC6\x86\x60\x02\x00\x00\x00")
    code.emit(b"\xC6\x86\x61\x02\x00\x00\x00")
    code.jump32(b"\xE9", "exit")

    if level in {2, 3}:
        code.label("opponent")
        code.emit(b"\x49\xFF\x41\x30")                           # opponent candidates
        code.emit(b"\x80\xBB\x78\x05\x00\x00\x14")
        code.jump32(b"\x0F\x84", "exit")
        code.emit(b"\x80\xBB\x7B\x05\x00\x00\x14")
        code.jump32(b"\x0F\x84", "exit")
        code.emit(b"\x49\xFF\x41\x28")                           # opponent sanction writes
        if level == 3:
            code.emit(b"\xC6\x86\x60\x02\x00\x00\x04")
            code.emit(b"\xC6\x86\x61\x02\x00\x00\x04")
        else:
            # Compare in place. The previous MOV AL form corrupted the low
            # byte of RAX, which is live across this hook and could crash FM
            # after returning to the match engine.
            code.emit(b"\x80\xBE\x60\x02\x00\x00\x02")         # yellow or worse -> red
            code.jump32(b"\x0F\x83", "opponent_escalate_red")
            code.emit(b"\xC6\x86\x60\x02\x00\x00\x02")         # no card -> yellow
            code.emit(b"\xC6\x86\x61\x02\x00\x00\x02")
            code.jump32(b"\xE9", "opponent_escalate_done")
            code.label("opponent_escalate_red")
            code.emit(b"\xC6\x86\x60\x02\x00\x00\x04")         # yellow -> red
            code.emit(b"\xC6\x86\x61\x02\x00\x00\x04")
            code.label("opponent_escalate_done")

    code.label("protected_end")
    code.label("exit")
    code.emit(b"\x9D\x41\x59\x41\x58\xE9")
    code.emit(_rel32(cave + len(code.data) + 4, return_address))
    result = code.finish()
    if len(result) >= HANDLER_OFFSET:
        raise RuntimeError("黑哨同步代码区溢出")
    return (
        result,
        cave + code.labels["protected_start"],
        cave + code.labels["protected_end"],
        cave + code.labels["exit"],
    )


def _build_fm24_hook(
    cave: int, original: bytes, return_address: int, level: int,
    team_address: int, club_address: int = 0,
) -> tuple[bytes, int, int, int]:
    code = _Code(cave)
    displacement = struct.unpack_from("<i", original, 3)[0]
    code.emit(original)
    code.emit(b"\x50\x53\x9C")  # preserve rax, rbx and incoming flags
    code.label("protected_start")

    def load_participant(extra: int) -> None:
        code.emit(b"\x48\x8B\x9F" + struct.pack("<i", displacement + extra))
        code.emit(b"\x48\x81\xFB\xFF\xFF\x0F\x00")
        code.jump32(b"\x0F\x86", "exit")
        code.emit(b"\x48\x8B\x5B\x48")
        code.emit(b"\x48\x81\xFB\xFF\xFF\x0F\x00")
        code.jump32(b"\x0F\x86", "exit")

    def compare_managed(destination: str, fallback: str) -> None:
        code.emit(b"\x48\xB8" + struct.pack("<Q", team_address))
        code.emit(b"\x48\x39\xC3")
        code.jump32(b"\x0F\x84", destination)
        if club_address:
            code.emit(b"\x48\x8B\x5B\x30")
            code.emit(b"\x48\xB8" + struct.pack("<Q", club_address))
            code.emit(b"\x48\x39\xC3")
            code.jump32(b"\x0F\x84", destination)
        code.jump32(b"\xE9", fallback)

    # The trainer stores the two incident participants at displacement+8/+10.
    load_participant(0x08)
    compare_managed("managed", "check_opponent")
    code.label("check_opponent")
    if level == 1:
        code.jump32(b"\xE9", "exit")
    else:
        load_participant(0x10)
        compare_managed("opponent", "exit")

    code.label("managed")
    code.emit(b"\x48\x31\xFF")
    code.emit(b"\xC6\x84\x24\xE0\x00\x00\x00\x00")
    code.emit(b"\xC6\x84\x24\xE8\x00\x00\x00\x00")
    code.jump32(b"\xE9", "exit")

    if level in {2, 3}:
        code.label("opponent")
        code.emit(b"\x80\xBF" + struct.pack("<i", displacement + 0x1C0) + b"\x14")
        code.jump32(b"\x0F\x84", "exit")
        code.emit(b"\x80\xBF" + struct.pack("<i", displacement + 0x1C3) + b"\x14")
        code.jump32(b"\x0F\x84", "exit")
        if level == 2:
            code.emit(b"\x80\xBC\x24\xE0\x00\x00\x00\x03")
            code.jump32(b"\x0F\x87", "exit")
            code.emit(b"\xFE\x84\x24\xE0\x00\x00\x00")
            code.emit(b"\xFE\x84\x24\xE8\x00\x00\x00")
        else:
            code.emit(b"\xC6\x84\x24\xE0\x00\x00\x00\x04")
            code.emit(b"\xC6\x84\x24\xE8\x00\x00\x00\x04")

    code.label("protected_end")
    code.label("exit")
    code.emit(b"\x9D\x5B\x58\xE9")
    code.emit(_rel32(cave + len(code.data) + 4, return_address))
    result = code.finish()
    if len(result) >= HANDLER_OFFSET:
        raise RuntimeError("FM2024 黑哨代码区溢出")
    return (
        result,
        cave + code.labels["protected_start"],
        cave + code.labels["protected_end"],
        cave + code.labels["exit"],
    )


def _call_stub(base: int, function: int, arg1: int, arg2: int, result_address: int) -> bytes:
    code = bytearray()
    code += b"\x48\xB9" + struct.pack("<Q", arg1)
    code += b"\x48\xBA" + struct.pack("<Q", arg2)
    code += b"\x48\xB8" + struct.pack("<Q", function)
    code += b"\x48\x83\xEC\x28\xFF\xD0"
    code += b"\x48\xA3" + struct.pack("<Q", result_address)
    code += b"\x48\x83\xC4\x28\xC3"
    return bytes(code)


def _remote_call(process, cave: int, function: int, arg1: int, arg2: int = 0) -> int:
    result_address = cave + TABLE_OFFSET + VEH_RESULT_OFFSET
    write_process_memory(process, result_address, b"\0" * 8)
    stub_address = cave + CALL_STUB_OFFSET
    stub = _call_stub(stub_address, function, arg1, arg2, result_address)
    write_process_memory(process, stub_address, stub)
    kernel32.FlushInstructionCache(process.handle, ctypes.c_void_p(stub_address), len(stub))
    thread_id = wintypes.DWORD(0)
    thread = kernel32.CreateRemoteThread(
        process.handle, None, 0, ctypes.c_void_p(stub_address), None, 0, ctypes.byref(thread_id),
    )
    if not thread:
        raise RuntimeError("无法创建黑哨异常保护初始化线程")
    try:
        wait = kernel32.WaitForSingleObject(thread, INFINITE_TIMEOUT_MS)
        if wait != WAIT_OBJECT_0:
            raise RuntimeError(f"黑哨异常保护初始化等待失败：{wait}")
    finally:
        kernel32.CloseHandle(thread)
    result = read_process_memory(process, result_address, 8)
    return struct.unpack("<Q", result)[0] if result and len(result) == 8 else 0


class RefereeHookController:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.pid = self.hook = self.cave = self.team_address = self.club_address = self.level = 0
        self.veh_handle = 0
        self.original = self.patch = b""
        self.error: str | None = None
        self.layout_key = ""
        self.observed_events = self.managed_actions = self.red_actions = 0
        self.opponent_candidates = self.exception_count = 0
        self.last_primary_adjudication = self.last_secondary_adjudication = 0

    def _refresh_counters(self) -> None:
        if not self.pid or not self.cave:
            return
        try:
            with open_process(self.pid) as process:
                row = read_process_memory(process, self.cave + TABLE_OFFSET, 0x48)
            if not row or len(row) < 0x48:
                return
            self.observed_events = struct.unpack_from("<Q", row, 0x00)[0]
            self.managed_actions = struct.unpack_from("<Q", row, 0x20)[0]
            self.red_actions = struct.unpack_from("<Q", row, 0x28)[0]
            self.opponent_candidates = struct.unpack_from("<Q", row, 0x30)[0]
            self.exception_count = struct.unpack_from("<Q", row, 0x38)[0]
            self.last_primary_adjudication = struct.unpack_from("<I", row, 0x40)[0]
            self.last_secondary_adjudication = struct.unpack_from("<I", row, 0x44)[0]
        except Exception:
            pass

    def status(self) -> dict[str, Any]:
        with self.lock:
            self._refresh_counters()
            return {
                "installed": bool(self.hook),
                "layout": self.layout_key or None,
                "team_address": hex(self.team_address) if self.team_address else None,
                "level": self.level,
                "mode": {1: "managed_ignore_opponent_normal", 2: "managed_ignore_opponent_escalated", 3: "managed_ignore_opponent_red"}.get(self.level, "inactive"),
                "revision": REFEREE_HOOK_REVISION,
                "recognition": "post_commit_protected",
                "armed": bool(self.hook and self.veh_handle),
                "observed_events": self.observed_events,
                "managed_actions": self.managed_actions,
                "red_actions": self.red_actions,
                "opponent_candidates": self.opponent_candidates,
                "protected_exceptions": self.exception_count,
                "last_primary_adjudication": self.last_primary_adjudication,
                "last_secondary_adjudication": self.last_secondary_adjudication,
                "error": self.error,
            }

    def sync(self, level: int, team_address: int, club_address: int = 0) -> dict[str, Any]:
        with self.lock:
            try:
                level = int(level or 0)
                if level not in {1, 2, 3}:
                    self._uninstall(); self.error = None
                    return self.status()
                if not team_address:
                    self._uninstall()
                    self.error = "尚未读取到黑哨目标球队地址"
                    return self.status()
                pid, _path, layout = select_process_layout()
                if layout.key not in {"fm24", "fm26"}:
                    raise RuntimeError("当前 FM 版本不支持黑哨同步")
                if self.hook and (
                    self.pid != pid or self.team_address != team_address
                    or self.club_address != club_address or self.level != level
                    or self.layout_key != layout.key
                ):
                    self._uninstall()
                if not self.hook:
                    if layout.key == "fm24":
                        self._install_fm24(pid, team_address, club_address, level, layout)
                    else:
                        self._install(pid, team_address, club_address, level)
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
    def _allocate_near(process, hook: int) -> int:
        origin = hook & ~0xFFFF
        for distance in range(0x10000, 0x70000000, 0x10000):
            for hint in (origin + distance, max(0x10000, origin - distance)):
                address = int(kernel32.VirtualAllocEx(
                    process.handle, ctypes.c_void_p(hint), 0x1000,
                    MEM_COMMIT_RESERVE, PAGE_EXECUTE_READWRITE,
                ) or 0)
                if address and abs(address - hook) < 0x7FFFFFFF:
                    return address
                if address:
                    kernel32.VirtualFreeEx(process.handle, ctypes.c_void_p(address), 0, MEM_RELEASE)
        raise RuntimeError("无法在比赛判罚代码附近分配同步内存")

    def _install(self, pid: int, team_address: int, club_address: int, level: int) -> None:
        with open_process(pid, write_memory=True, create_thread=True) as process:
            module = find_module(process, GAME_PLUGIN)
            if not module:
                raise RuntimeError("game_plugin.dll 尚未加载")
            image = read_process_memory(process, module.base_address, module.size)
            offsets = _matches(image or b"", REFEREE_PATTERN)
            if not offsets and self._adopt_existing(
                process, module.base_address, image or b"", pid,
                team_address, club_address, level,
            ):
                return
            if not offsets and self._remove_stale_existing(
                process, module.base_address, image or b"",
            ):
                image = read_process_memory(process, module.base_address, module.size)
                offsets = _matches(image or b"", REFEREE_PATTERN)
            if len(offsets) != 1:
                raise RuntimeError(f"黑哨同步特征码命中 {len(offsets)} 处，已拒绝安装")
            hook = module.base_address + offsets[0]
            original = image[offsets[0]:offsets[0] + REFEREE_ORIGINAL_SIZE]
            if len(original) != REFEREE_ORIGINAL_SIZE or tuple(original) != REFEREE_PATTERN:
                raise RuntimeError("黑哨同步入口指令不符合预期")
            cave = self._allocate_near(process, hook)
            veh_handle = 0
            try:
                code, protected_start, protected_end, exit_address = _build_hook(
                    cave, original, hook + REFEREE_ORIGINAL_SIZE, level, club_address,
                )
                handler_address = cave + HANDLER_OFFSET
                handler = _build_handler(
                    handler_address, protected_start, protected_end, exit_address, cave + TABLE_OFFSET,
                )
                write_process_memory(process, cave, b"\0" * 0x1000)
                write_process_memory(process, cave, code)
                write_process_memory(process, handler_address, handler)
                table = bytearray(0x100)
                struct.pack_into("<Q", table, 0x80, team_address)
                struct.pack_into("<Q", table, 0x88, club_address)
                write_process_memory(process, cave + TABLE_OFFSET, bytes(table))
                kernel32.FlushInstructionCache(process.handle, ctypes.c_void_p(cave), 0x800)
                veh_handle = _remote_call(
                    process, cave, _proc_address(b"AddVectoredExceptionHandler"), 1, handler_address,
                )
                if not veh_handle:
                    raise RuntimeError("FM 未接受黑哨异常保护处理器")
                patch = b"\xE9" + _rel32(hook + 5, cave) + b"\x90" * (REFEREE_ORIGINAL_SIZE - 5)
                self._write_code(process, hook, patch)
            except Exception:
                safe_to_free = not veh_handle
                if veh_handle:
                    try:
                        safe_to_free = bool(_remote_call(
                            process, cave, _proc_address(b"RemoveVectoredExceptionHandler"), veh_handle,
                        ))
                    except Exception:
                        safe_to_free = False
                # A registered handler must never point into freed memory. If
                # Windows refuses removal, leak this one page until FM exits.
                if safe_to_free:
                    kernel32.VirtualFreeEx(process.handle, ctypes.c_void_p(cave), 0, MEM_RELEASE)
                raise
            self.pid, self.hook, self.cave = pid, hook, cave
            self.team_address, self.club_address, self.level = team_address, club_address, level
            self.veh_handle = veh_handle
            self.original, self.patch = original, patch
            self.layout_key = "fm26"
            self.observed_events = self.managed_actions = self.red_actions = 0
            self.opponent_candidates = self.exception_count = 0

    def _install_fm24(
        self, pid: int, team_address: int, club_address: int, level: int, layout: Any,
    ) -> None:
        with open_process(pid, write_memory=True, create_thread=True) as process:
            module = find_module(process, "fm.exe")
            if not module:
                raise RuntimeError("fm.exe 尚未加载")
            aob_rva = layout.referee_hook_rva
            if aob_rva is None:
                raise RuntimeError("当前 FM2024 分发版本未映射黑哨")
            aob_address = module.base_address + int(aob_rva)
            signature = read_process_memory(process, aob_address, len(FM24_REFEREE_PATTERN)) or b""
            if len(signature) != len(FM24_REFEREE_PATTERN) or any(
                expected is not None and signature[index] != expected
                for index, expected in enumerate(FM24_REFEREE_PATTERN)
            ):
                raise RuntimeError("FM2024 黑哨入口已被修改")
            hook = aob_address + 9
            original = signature[9:16]
            cave = self._allocate_near(process, hook)
            veh_handle = 0
            try:
                code, protected_start, protected_end, exit_address = _build_fm24_hook(
                    cave, original, hook + len(original), level, team_address, club_address,
                )
                handler_address = cave + HANDLER_OFFSET
                handler = _build_handler(
                    handler_address, protected_start, protected_end,
                    exit_address, cave + TABLE_OFFSET,
                )
                write_process_memory(process, cave, b"\0" * 0x1000)
                write_process_memory(process, cave, code)
                write_process_memory(process, handler_address, handler)
                write_process_memory(process, cave + TABLE_OFFSET, b"\0" * 0x100)
                kernel32.FlushInstructionCache(process.handle, ctypes.c_void_p(cave), 0x800)
                veh_handle = _remote_call(
                    process, cave, _proc_address(b"AddVectoredExceptionHandler"), 1, handler_address,
                )
                if not veh_handle:
                    raise RuntimeError("FM2024 未接受黑哨异常保护处理器")
                patch = b"\xE9" + _rel32(hook + 5, cave) + b"\x90" * 2
                self._write_code(process, hook, patch)
            except Exception:
                safe_to_free = not veh_handle
                if veh_handle:
                    try:
                        safe_to_free = bool(_remote_call(
                            process, cave,
                            _proc_address(b"RemoveVectoredExceptionHandler"), veh_handle,
                        ))
                    except Exception:
                        safe_to_free = False
                if safe_to_free:
                    kernel32.VirtualFreeEx(process.handle, ctypes.c_void_p(cave), 0, MEM_RELEASE)
                raise
            self.pid, self.hook, self.cave = pid, hook, cave
            self.team_address, self.club_address, self.level = team_address, club_address, level
            self.veh_handle = veh_handle
            self.original, self.patch = original, patch
            self.layout_key = "fm24"
            self.observed_events = self.managed_actions = self.red_actions = 0
            self.opponent_candidates = self.exception_count = 0

    def _remove_stale_existing(self, process, module_base: int, image: bytes) -> bool:
        """Safely detach an orphaned FMODD referee hook before replacement."""
        positions: list[int] = []
        start = 0
        while True:
            position = image.find(REFEREE_PATCH_TAIL, start)
            if position < 0:
                break
            positions.append(position)
            start = position + 1
        if len(positions) != 1 or positions[0] < 5:
            return False

        hook = module_base + positions[0] - 5
        patch = read_process_memory(process, hook, REFEREE_ORIGINAL_SIZE)
        if (
            not patch or len(patch) != REFEREE_ORIGINAL_SIZE
            or patch[0] != 0xE9
            or patch[5:] != b"\x90" * (REFEREE_ORIGINAL_SIZE - 5)
        ):
            return False
        cave = hook + 5 + struct.unpack_from("<i", patch, 1)[0]
        if cave < 0x100000 or abs(cave - hook) >= 0x7FFFFFFF:
            return False

        # Verify the cave is one of our synchronized referee hooks before
        # restoring code or calling a handle found in its table.
        prefix = read_process_memory(process, cave, REFEREE_ORIGINAL_SIZE + 17)
        if (
            not prefix or len(prefix) != REFEREE_ORIGINAL_SIZE + 17
            or prefix[:REFEREE_ORIGINAL_SIZE] != bytes(REFEREE_PATTERN)
            or prefix[REFEREE_ORIGINAL_SIZE:REFEREE_ORIGINAL_SIZE + 5]
            != b"\x41\x50\x41\x51\x9C"
            or prefix[REFEREE_ORIGINAL_SIZE + 5:REFEREE_ORIGINAL_SIZE + 7]
            != b"\x49\xB9"
            or struct.unpack_from("<Q", prefix, REFEREE_ORIGINAL_SIZE + 7)[0]
            != cave + TABLE_OFFSET
        ):
            return False
        table = read_process_memory(process, cave + TABLE_OFFSET, 0x100)
        if not table or len(table) != 0x100:
            return False
        veh_handle = struct.unpack_from("<Q", table, VEH_RESULT_OFFSET)[0]

        self._write_code(process, hook, bytes(REFEREE_PATTERN))
        time.sleep(0.1)
        removed = not veh_handle
        if veh_handle:
            try:
                removed = bool(_remote_call(
                    process, cave,
                    _proc_address(b"RemoveVectoredExceptionHandler"), veh_handle,
                ))
            except Exception:
                removed = False
        # Never free a page while Windows may still dispatch its handler.
        # A leaked page is harmless until FM exits; a dangling VEH is not.
        if removed:
            kernel32.VirtualFreeEx(
                process.handle, ctypes.c_void_p(cave), 0, MEM_RELEASE,
            )
        return True

    def _adopt_existing(
        self, process, module_base: int, image: bytes, pid: int,
        team_address: int, club_address: int, level: int,
    ) -> bool:
        positions: list[int] = []
        start = 0
        while True:
            position = image.find(REFEREE_PATCH_TAIL, start)
            if position < 0:
                break
            positions.append(position)
            start = position + 1
        if len(positions) != 1 or positions[0] < 5:
            return False

        hook = module_base + positions[0] - 5
        patch = read_process_memory(process, hook, REFEREE_ORIGINAL_SIZE)
        if not patch or len(patch) != REFEREE_ORIGINAL_SIZE or patch[0] != 0xE9:
            return False
        cave = hook + 5 + struct.unpack_from("<i", patch, 1)[0]
        if cave < 0x100000 or abs(cave - hook) >= 0x7FFFFFFF:
            return False

        original = bytes(REFEREE_PATTERN)
        expected_code, protected_start, protected_end, exit_address = _build_hook(
            cave, original, hook + REFEREE_ORIGINAL_SIZE, level, club_address,
        )
        live_code = read_process_memory(process, cave, len(expected_code))
        if live_code != expected_code:
            return False
        expected_handler = _build_handler(
            cave + HANDLER_OFFSET, protected_start, protected_end,
            exit_address, cave + TABLE_OFFSET,
        )
        live_handler = read_process_memory(process, cave + HANDLER_OFFSET, len(expected_handler))
        if live_handler != expected_handler:
            return False

        table = read_process_memory(process, cave + TABLE_OFFSET, 0x100)
        if not table or len(table) != 0x100:
            return False
        live_team = struct.unpack_from("<Q", table, 0x80)[0]
        live_club = struct.unpack_from("<Q", table, 0x88)[0]
        veh_handle = struct.unpack_from("<Q", table, VEH_RESULT_OFFSET)[0]
        if live_team != team_address or live_club != club_address or not veh_handle:
            return False

        self.pid, self.hook, self.cave = pid, hook, cave
        self.team_address, self.club_address, self.level = team_address, club_address, level
        self.veh_handle = veh_handle
        self.original, self.patch = original, patch
        self._refresh_counters()
        return True

    def _uninstall(self) -> None:
        if not self.hook:
            self._clear(); return
        cave_can_be_freed = False
        try:
            with open_process(self.pid, write_memory=True, create_thread=True) as process:
                if read_process_memory(process, self.hook, len(self.patch)) == self.patch:
                    self._write_code(process, self.hook, self.original)
                time.sleep(0.1)
                if self.veh_handle:
                    removed = _remote_call(
                        process, self.cave, _proc_address(b"RemoveVectoredExceptionHandler"), self.veh_handle,
                    )
                    cave_can_be_freed = bool(removed)
                else:
                    cave_can_be_freed = True
                if cave_can_be_freed and self.cave:
                    kernel32.VirtualFreeEx(process.handle, ctypes.c_void_p(self.cave), 0, MEM_RELEASE)
        except Exception:
            pass
        self._clear()

    @staticmethod
    def _write_code(process, address: int, data: bytes) -> None:
        old = wintypes.DWORD(0)
        if not kernel32.VirtualProtectEx(
            process.handle, ctypes.c_void_p(address), len(data),
            PAGE_EXECUTE_READWRITE, ctypes.byref(old),
        ):
            raise RuntimeError("无法修改比赛判罚代码页保护")
        try:
            write_process_memory(process, address, data)
            kernel32.FlushInstructionCache(process.handle, ctypes.c_void_p(address), len(data))
        finally:
            restored = wintypes.DWORD(0)
            kernel32.VirtualProtectEx(
                process.handle, ctypes.c_void_p(address), len(data), old.value, ctypes.byref(restored),
            )

    def _clear(self) -> None:
        self.pid = self.hook = self.cave = self.team_address = self.club_address = self.level = 0
        self.veh_handle = 0
        self.original = self.patch = b""
        self.layout_key = ""
        self.observed_events = self.managed_actions = self.red_actions = 0
        self.opponent_candidates = self.exception_count = 0
        self.last_primary_adjudication = self.last_secondary_adjudication = 0
