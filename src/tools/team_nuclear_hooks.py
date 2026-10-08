from __future__ import annotations

import ctypes
import struct
import threading
import time
from ctypes import wintypes
from typing import Any

from fm_collector.win32 import PAGE_EXECUTE_READWRITE, find_module, kernel32, open_process, read_process_memory, write_process_memory
from tools.game_layout import FM24_240_EXE_SHA256, FM24_EXE_SHA256
from tools.initial_data_audit import GAME_PLUGIN, select_process_layout
from tools.redbull_hook import MEM_COMMIT_RESERVE, MEM_RELEASE, _Code, _matches, _rel32


NO_RETIREMENT_PATTERN = (0x0F, 0xB7, 0x41, 0x14, 0x3D, 0x80, 0x00, 0x00, 0x00, 0x74)
FM24_NO_RETIREMENT_RVA = 0x2EF09D3
FM24_NO_RETIREMENT_SIGNATURE = bytes.fromhex(
    "0F B7 49 14 B0 01 81 F9 80 00 00 00 74 02"
)
CLUB_WORK_PERMIT_PATTERN = (0x48, 0x83, 0xEC, None, 0x31, 0xC0, 0x48, 0x39, 0xCA)


def _no_retirement_code(cave: int, original: bytes, return_address: int, team_address: int) -> bytes:
    code = _Code(cave)
    code.emit(b"\x9C\x50\x41\x50")
    code.emit(b"\x48\x81\xFA\xFF\xFF\x0F\x00")
    code.jump32(b"\x0F\x86", "exit")
    code.emit(b"\x66\x81\x7A\x34\x00\x7F")
    code.jump32(b"\x0F\x86", "exit")
    code.emit(b"\x66\x81\x7A\x34\xFF\x7F")
    code.jump32(b"\x0F\x87", "exit")
    code.emit(b"\x4C\x8B\x82\xA8\x00\x00\x00\x4D\x85\xC0")
    code.jump32(b"\x0F\x84", "exit")
    code.emit(b"\x4D\x8B\x40\x10\x48\xB8" + struct.pack("<Q", team_address) + b"\x49\x39\xC0")
    code.jump32(b"\x0F\x85", "exit")
    code.emit(b"\x66\xC7\x41\x14\x80\x00")
    code.label("exit")
    code.emit(b"\x41\x58\x58\x9D")
    code.emit(original)
    code.emit(b"\xE9" + _rel32(cave + len(code.data) + 5, return_address))
    return code.finish()


def _fm24_no_retirement_code(
    cave: int, original: bytes, return_address: int,
    team_address: int, club_address: int,
) -> bytes:
    code = _Code(cave)
    code.emit(b"\x9C\x50\x41\x50")
    code.emit(b"\x4D\x89\xE0")                     # mov r8,r12 (person)
    code.emit(b"\x48\xB8\xFF\xFF\xFF\xFF\xFF\x7F\x00\x00")
    code.emit(b"\x49\x81\xF8\xFF\xFF\x0F\x00")
    code.jump32(b"\x0F\x86", "exit")
    code.emit(b"\x49\x39\xC0")
    code.jump32(b"\x0F\x87", "exit")
    code.emit(b"\x4D\x8B\x80\xC8\x00\x00\x00") # contract = person+0xc8
    code.emit(b"\x49\x81\xF8\xFF\xFF\x0F\x00")
    code.jump32(b"\x0F\x86", "exit")
    code.emit(b"\x49\x39\xC0")
    code.jump32(b"\x0F\x87", "exit")
    code.emit(b"\x4D\x8B\x40\x10")                 # team = contract+0x10
    code.emit(b"\x49\x81\xF8\xFF\xFF\x0F\x00")
    code.jump32(b"\x0F\x86", "exit")
    code.emit(b"\x49\x39\xC0")
    code.jump32(b"\x0F\x87", "exit")
    code.emit(b"\x48\xB8" + struct.pack("<Q", team_address))
    code.emit(b"\x49\x39\xC0")
    code.jump32(b"\x0F\x84", "apply")
    if club_address:
        code.emit(b"\x4D\x8B\x40\x30")             # parent club = team+0x30
        code.emit(b"\x48\xB8" + struct.pack("<Q", club_address))
        code.emit(b"\x49\x39\xC0")
        code.jump32(b"\x0F\x85", "exit")
    else:
        code.jump32(b"\xE9", "exit")
    code.label("apply")
    code.emit(b"\x66\xC7\x41\x14\x80\x00")
    code.label("exit")
    code.emit(b"\x41\x58\x58\x9D")
    code.emit(original)
    code.emit(b"\xE9" + _rel32(cave + len(code.data) + 5, return_address))
    return code.finish()


def _club_work_permit_code(cave: int, original: bytes, return_address: int, team_address: int, club_address: int) -> bytes:
    code = _Code(cave)
    code.emit(original)
    # The original prologue leaves RAX cleared. Preserve both scratch registers
    # used by the scope check so the hooked function observes the same state.
    code.emit(b"\x9C\x50\x41\x50")
    code.emit(b"\x49\x81\xFF\xFF\xFF\x0F\x00")
    code.jump32(b"\x0F\x86", "exit")
    code.emit(b"\x49\xB8" + struct.pack("<Q", team_address) + b"\x4D\x39\xC7")
    code.jump32(b"\x0F\x84", "valid")
    code.emit(b"\x4D\x8B\x47\x30\x48\xB8" + struct.pack("<Q", club_address) + b"\x49\x39\xC0")
    code.jump32(b"\x0F\x85", "exit")
    code.label("valid")
    code.emit(b"\x48\x89\xCA")
    code.label("exit")
    code.emit(b"\x41\x58\x58\x9D\xE9")
    code.emit(_rel32(cave + len(code.data) + 4, return_address))
    return code.finish()


class TeamNuclearHookController:
    def __init__(self) -> None:
        self.lock = threading.RLock(); self.pid = self.team_address = self.club_address = 0
        self.installed: dict[str, dict[str, Any]] = {}; self.errors: dict[str, str] = {}

    @property
    def supported(self) -> set[str]:
        return {"no_retirement", "club_work_permit"}

    def status(self) -> dict[str, Any]:
        with self.lock:
            return {"supported": sorted(self.supported), "installed": sorted(self.installed), "errors": dict(self.errors)}

    def sync(self, desired: list[str], team_address: int, club_address: int = 0) -> dict[str, Any]:
        wanted = set(desired) & self.supported
        with self.lock:
            if self.team_address and (self.team_address != team_address or self.club_address != club_address):
                self.close()
            self.team_address, self.club_address = team_address, club_address
            for key in list(self.installed):
                if key not in wanted or not team_address:
                    self._disable(key)
            for key in sorted(wanted):
                if key not in self.installed and team_address:
                    try:
                        if key == "no_retirement":
                            self._enable_no_retirement(team_address, club_address)
                        else:
                            self._enable_club_work_permit(team_address, club_address)
                        self.errors.pop(key, None)
                    except Exception as error:
                        self.errors[key] = str(error)
            return self.status()

    def close(self) -> None:
        with self.lock:
            for key in list(self.installed):
                self._disable(key)

    @staticmethod
    def _allocate_near(process, hook: int) -> int:
        origin = hook & ~0xFFFF
        for distance in range(0x10000, 0x70000000, 0x10000):
            for hint in (origin + distance, max(0x10000, origin - distance)):
                address = int(kernel32.VirtualAllocEx(process.handle, ctypes.c_void_p(hint), 0x1000, MEM_COMMIT_RESERVE, PAGE_EXECUTE_READWRITE) or 0)
                if address and abs(address - hook) < 0x7FFFFFFF:
                    return address
                if address:
                    kernel32.VirtualFreeEx(process.handle, ctypes.c_void_p(address), 0, MEM_RELEASE)
        raise RuntimeError("无法在人员退役代码附近分配内存")

    def _enable_no_retirement(self, team_address: int, club_address: int) -> None:
        pid, _path, layout = select_process_layout()
        if layout.key not in {"fm24", "fm26"}:
            raise RuntimeError("当前 FM 版本不支持人员不退役")
        with open_process(pid, write_memory=True) as process:
            module_name = "fm.exe" if layout.key == "fm24" else GAME_PLUGIN
            module = find_module(process, module_name)
            if not module:
                raise RuntimeError(f"{module_name} 尚未加载")
            if layout.key == "fm24":
                image = read_process_memory(process, module.base_address, module.size)
                if not image:
                    raise RuntimeError("无法读取 fm.exe")
                hits = _matches(image, tuple(FM24_NO_RETIREMENT_SIGNATURE))
                exact = (
                    layout.executable_sha256.upper()
                    in {FM24_EXE_SHA256, FM24_240_EXE_SHA256}
                )
                if exact:
                    hook = module.base_address + FM24_NO_RETIREMENT_RVA
                    actual = read_process_memory(
                        process, hook, len(FM24_NO_RETIREMENT_SIGNATURE),
                    )
                    if actual != FM24_NO_RETIREMENT_SIGNATURE:
                        raise RuntimeError("FM2024 人员不退役完整特征码不匹配")
                elif len(hits) == 1:
                    hook = module.base_address + hits[0]
                    actual = FM24_NO_RETIREMENT_SIGNATURE
                else:
                    raise RuntimeError(
                        f"FM2024 人员不退役特征码命中 {len(hits)} 处，已拒绝安装"
                    )
                original = actual[:6]
            else:
                image = read_process_memory(process, module.base_address, module.size)
                offsets = _matches(image or b"", NO_RETIREMENT_PATTERN)
                if len(offsets) != 1:
                    raise RuntimeError(f"人员不退役特征码命中 {len(offsets)} 处，已拒绝安装")
                hook = module.base_address + offsets[0]
                original = image[offsets[0]:offsets[0] + 9]
            cave = self._allocate_near(process, hook)
            code = (
                _fm24_no_retirement_code(
                    cave, original, hook + len(original), team_address, club_address,
                )
                if layout.key == "fm24"
                else _no_retirement_code(cave, original, hook + 9, team_address)
            )
            write_process_memory(process, cave, code)
            patch = b"\xE9" + _rel32(hook + 5, cave) + b"\x90" * (len(original) - 5)
            self._write_code(process, hook, patch)
            self.pid = pid
            self.installed["no_retirement"] = {
                "hook": hook, "cave": cave, "original": original,
                "patch": patch, "layout": layout.key,
            }

    def _enable_club_work_permit(self, team_address: int, club_address: int) -> None:
        if not club_address:
            raise RuntimeError("无法确认当前俱乐部指针")
        pid, _path, layout = select_process_layout()
        if layout.key != "fm26":
            raise RuntimeError("当前 FM 版本不支持执教球队劳工证")
        with open_process(pid, write_memory=True) as process:
            module = find_module(process, GAME_PLUGIN)
            if not module:
                raise RuntimeError("game_plugin.dll 尚未加载")
            image = read_process_memory(process, module.base_address, module.size)
            offsets = _matches(image or b"", CLUB_WORK_PERMIT_PATTERN)
            if len(offsets) != 1:
                raise RuntimeError(f"执教球队劳工证特征码命中 {len(offsets)} 处，已拒绝安装")
            hook = module.base_address + offsets[0]; original = image[offsets[0]:offsets[0] + 6]
            cave = self._allocate_near(process, hook)
            code = _club_work_permit_code(cave, original, hook + 6, team_address, club_address)
            write_process_memory(process, cave, code)
            patch = b"\xE9" + _rel32(hook + 5, cave) + b"\x90"
            self._write_code(process, hook, patch)
            self.pid = pid
            self.installed["club_work_permit"] = {"hook": hook, "cave": cave, "original": original, "patch": patch}

    def _disable(self, key: str) -> None:
        record = self.installed.pop(key, None)
        if not record:
            return
        try:
            with open_process(self.pid, write_memory=True) as process:
                if read_process_memory(process, record["hook"], len(record["patch"])) == record["patch"]:
                    self._write_code(process, record["hook"], record["original"])
                time.sleep(0.1)
                kernel32.VirtualFreeEx(process.handle, ctypes.c_void_p(record["cave"]), 0, MEM_RELEASE)
        except Exception:
            pass

    @staticmethod
    def _write_code(process, address: int, data: bytes) -> None:
        old = wintypes.DWORD(0)
        if not kernel32.VirtualProtectEx(process.handle, ctypes.c_void_p(address), len(data), PAGE_EXECUTE_READWRITE, ctypes.byref(old)):
            raise RuntimeError("无法修改人员退役代码页保护")
        try:
            write_process_memory(process, address, data)
            kernel32.FlushInstructionCache(process.handle, ctypes.c_void_p(address), len(data))
        finally:
            restored = wintypes.DWORD(0)
            kernel32.VirtualProtectEx(process.handle, ctypes.c_void_p(address), len(data), old.value, ctypes.byref(restored))
