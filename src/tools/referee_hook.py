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
from tools.initial_data_audit import GAME_PLUGIN, select_process_layout
from tools.redbull_hook import MEM_COMMIT_RESERVE, MEM_RELEASE, _Code, _matches, _rel32


# The hook point is the seven-byte `movzx ecx,byte ptr [rax+142FA]`
# recovered from the trainer's referee-manipulation implementation.
REFEREE_PATTERN = (
    0x0F, 0x83, None, None, None, None,
    0x48, 0x8B, 0x86, None, None, 0x00, 0x00,
    0x0F, 0xB6, 0x88,
)

REFEREE_TABLE_OFFSET = 0x800
REFEREE_MAX_PARTICIPANTS = 8
REFEREE_HOOK_REVISION = 5


def _referee_code(
    cave: int, original: bytes, return_address: int, level: int,
) -> bytes:
    code = _Code(cave)
    table = cave + REFEREE_TABLE_OFFSET
    code.emit(original)
    # Only read the two participant pointers directly owned by the live RBX
    # object.  The old implementation followed participant+0xA8 inside the
    # injected code; unlike Cheat Engine's {$try}, raw machine code cannot
    # recover when that transient object disappears and could crash FM.
    # A Python learner resolves the pointers outside the game thread and puts
    # confirmed managed-team participant pointers in our own allocation.
    code.emit(b"\x41\x50\x41\x51\x9C")                         # push r8; push r9; pushfq
    code.emit(b"\x48\x81\xFB\xFF\xFF\x0F\x00")             # reject invalid RBX before field reads
    code.jump32(b"\x0F\x86", "exit")
    code.emit(b"\x49\xB9" + struct.pack("<Q", table))          # mov r9,table
    code.emit(b"\x4C\x8B\x83\x88\x02\x00\x00")             # mov r8,[rbx+288]
    code.emit(b"\x4D\x89\x41\x10")                             # last participant 288
    code.emit(b"\x49\x81\xF8\xFF\xFF\x0F\x00")             # zero/small values must not match empty slots
    code.jump32(b"\x0F\x86", "exit")
    code.emit(b"\x4C\x8B\x83\x90\x02\x00\x00")             # mov r8,[rbx+290]
    code.emit(b"\x4D\x89\x41\x18")                             # last participant 290
    code.emit(b"\x49\x81\xF8\xFF\xFF\x0F\x00")
    code.jump32(b"\x0F\x86", "exit")
    code.emit(b"\x4D\x3B\x41\x10")                             # both sides must be distinct objects
    code.jump32(b"\x0F\x84", "exit")
    code.emit(b"\x49\x89\x59\x08\x49\xFF\x01")             # last rbx; sequence++
    code.emit(b"\x45\x31\xC0")                                 # record incoming adjudication bytes
    code.emit(b"\x44\x8A\x84\x24\xD8\x00\x00\x00")
    code.emit(b"\x45\x89\x81\x90\x00\x00\x00")
    code.emit(b"\x44\x8A\x84\x24\xE0\x00\x00\x00")
    code.emit(b"\x45\x89\x81\x94\x00\x00\x00")
    if level == 0:                                                 # diagnostic: learn only
        code.jump32(b"\xE9", "exit")
    code.emit(b"\x4C\x8B\x83\x88\x02\x00\x00")             # participant 288 again
    for index in range(REFEREE_MAX_PARTICIPANTS):
        code.emit(b"\x4D\x3B\x41" + bytes((0x40 + index * 8,))) # cmp r8,[r9+slot]
        code.jump32(b"\x0F\x84", "managed_ignore")
    code.jump32(b"\xE9", "check_opponent" if level == 2 else "exit")
    code.label("managed_ignore")
    code.emit(b"\x80\xBC\x24\xD8\x00\x00\x00\x04")         # adjudication bytes must be in 0..4
    code.jump32(b"\x0F\x87", "exit")
    code.emit(b"\x80\xBC\x24\xE0\x00\x00\x00\x04")
    code.jump32(b"\x0F\x87", "exit")
    code.emit(b"\x49\x3B\x59\x38")                             # never rewrite the same managed event twice
    code.jump32(b"\x0F\x84", "exit")
    code.emit(b"\x49\x89\x59\x38")
    code.emit(b"\x49\xFF\x41\x20")                             # managed actions counter
    code.emit(b"\xC6\x84\x24\xD8\x00\x00\x00\x00")         # managed team: completely ignored
    code.emit(b"\xC6\x84\x24\xE0\x00\x00\x00\x00")
    code.emit(b"\xB9\x0A\x00\x00\x00")                         # trainer's successful-ignore result
    code.jump32(b"\xE9", "exit")
    if level == 2:
        code.label("check_opponent")
        code.emit(b"\x4C\x8B\x83\x90\x02\x00\x00")             # participant 290
        for index in range(REFEREE_MAX_PARTICIPANTS):
            code.emit(b"\x4D\x3B\x41" + bytes((0x40 + index * 8,)))
            code.jump32(b"\x0F\x84", "opponent_red")
        code.jump32(b"\xE9", "exit")
        code.label("opponent_red")
        code.emit(b"\x49\xFF\x81\x80\x00\x00\x00")       # opponent candidates counter
        code.emit(b"\x80\xBB\x78\x05\x00\x00\x14")             # preserve trainer's two exclusion guards
        code.jump32(b"\x0F\x84", "exit")
        code.emit(b"\x80\xBB\x7B\x05\x00\x00\x14")
        code.jump32(b"\x0F\x84", "exit")
        code.emit(b"\x80\xBC\x24\xD8\x00\x00\x00\x03")     # never rewrite an already-red/invalid event
        code.jump32(b"\x0F\x87", "exit")
        code.emit(b"\x80\xBC\x24\xE0\x00\x00\x00\x03")
        code.jump32(b"\x0F\x87", "exit")
        # Team participants are learned outside the game thread. Never modify
        # half of an event while that mapping is still changing: the learner
        # arms this branch only after the participant table has been stable.
        code.emit(b"\x41\x80\xB9\xA0\x00\x00\x00\x01")
        code.jump32(b"\x0F\x85", "skip_unarmed")
        code.emit(b"\x49\x3B\x59\x30")                         # never rewrite the same opponent event twice
        code.jump32(b"\x0F\x84", "exit")
        code.emit(b"\x49\x89\x59\x30")
        code.emit(b"\x49\xFF\x41\x28")                         # opponent red actions counter
        code.emit(b"\x31\xC9")                                     # xor ecx,ecx
        code.emit(b"\x88\x88" + original[3:7])                     # clear original adjudication byte
        code.emit(b"\xC6\x84\x24\xD8\x00\x00\x00\x04")
        code.emit(b"\xC6\x84\x24\xE0\x00\x00\x00\x04")       # direct red-card level
        code.jump32(b"\xE9", "exit")
        code.label("skip_unarmed")
        code.emit(b"\x49\xFF\x81\x88\x00\x00\x00")       # candidates deferred while mapping settles
    code.label("exit")
    code.emit(b"\x9D\x41\x59\x41\x58\xE9")                   # popfq; pop r9; pop r8; jmp
    code.emit(_rel32(cave + len(code.data) + 4, return_address))
    result = code.finish()
    if len(result) >= REFEREE_TABLE_OFFSET:
        raise RuntimeError("黑哨代码区溢出")
    return result


class RefereeHookController:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.pid = self.hook = self.cave = self.team_address = self.club_address = self.level = 0
        self.original = self.patch = b""
        self.error: str | None = None
        self.learned_participants: list[int] = []
        self.observed_events = self.managed_actions = self.red_actions = 0
        self.opponent_candidates = self.skipped_unarmed = 0
        self.last_primary_adjudication = self.last_secondary_adjudication = 0
        self.armed = False
        self._learner_stop = threading.Event()
        self._learner_thread: threading.Thread | None = None

    def status(self) -> dict[str, Any]:
        with self.lock:
            return {
                "installed": bool(self.hook),
                "team_address": hex(self.team_address) if self.team_address else None,
                "level": self.level,
                "mode": "managed_ignore_opponent_red" if self.level == 2 else "managed_ignore_opponent_normal",
                "revision": REFEREE_HOOK_REVISION,
                "learned_participants": len(self.learned_participants),
                "observed_events": self.observed_events,
                "managed_actions": self.managed_actions,
                "red_actions": self.red_actions,
                "opponent_candidates": self.opponent_candidates,
                "skipped_unarmed": self.skipped_unarmed,
                "last_primary_adjudication": self.last_primary_adjudication,
                "last_secondary_adjudication": self.last_secondary_adjudication,
                "armed": self.armed,
                "error": self.error,
            }

    def sync(self, level: int, team_address: int, club_address: int = 0) -> dict[str, Any]:
        with self.lock:
            try:
                level = int(level or 0)
                if level not in {1, 2} or not team_address:
                    self._uninstall(); self.error = None
                    return self.status()
                pid, _path, layout = select_process_layout()
                if layout.key != "fm26":
                    raise RuntimeError("当前 FM 版本不支持黑哨")
                if self.hook and (self.pid != pid or self.team_address != team_address or self.club_address != club_address or self.level != level):
                    self._uninstall()
                if not self.hook:
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
        raise RuntimeError("无法在比赛判罚代码附近分配内存")

    def _install(self, pid: int, team_address: int, club_address: int, level: int) -> None:
        with open_process(pid, write_memory=True) as process:
            module = find_module(process, GAME_PLUGIN)
            if not module:
                raise RuntimeError("game_plugin.dll 尚未加载")
            image = read_process_memory(process, module.base_address, module.size)
            offsets = _matches(image or b"", REFEREE_PATTERN)
            if len(offsets) != 1:
                raise RuntimeError(f"黑哨特征码命中 {len(offsets)} 处，已拒绝安装")
            hook = module.base_address + offsets[0] + 13
            original = image[offsets[0] + 13:offsets[0] + 20]
            if len(original) != 7 or original[:3] != b"\x0F\xB6\x88":
                raise RuntimeError("黑哨入口指令不符合预期")
            cave = self._allocate_near(process, hook)
            try:
                code = _referee_code(cave, original, hook + 7, level)
                write_process_memory(process, cave, code)
                write_process_memory(process, cave + REFEREE_TABLE_OFFSET, b"\0" * 0x100)
                patch = b"\xE9" + _rel32(hook + 5, cave) + b"\x90\x90"
                self._write_code(process, hook, patch)
            except Exception:
                kernel32.VirtualFreeEx(process.handle, ctypes.c_void_p(cave), 0, MEM_RELEASE)
                raise
            self.pid, self.hook, self.cave = pid, hook, cave
            self.team_address, self.club_address = team_address, club_address
            self.level = level
            self.original, self.patch = original, patch
            self.learned_participants = []
            self.observed_events = self.managed_actions = self.red_actions = 0
            self.opponent_candidates = self.skipped_unarmed = 0
            self.last_primary_adjudication = self.last_secondary_adjudication = 0
            self.armed = False
            self._start_learner()

    def _start_learner(self) -> None:
        self._learner_stop.clear()
        self._learner_thread = threading.Thread(
            target=self._learn_participants,
            args=(self.pid, self.cave + REFEREE_TABLE_OFFSET, self.team_address, self.club_address),
            name="FMODD-referee-learner", daemon=True,
        )
        self._learner_thread.start()

    def _stop_learner(self) -> None:
        self._learner_stop.set()
        thread, self._learner_thread = self._learner_thread, None
        if thread and thread is not threading.current_thread():
            thread.join(timeout=0.5)

    def _learn_participants(self, pid: int, table: int, team_address: int, club_address: int) -> None:
        """Resolve transient participant objects outside FM's execution thread.

        ReadProcessMemory failure is harmless here; no invalid pointer is ever
        dereferenced by injected machine code beyond RBX's own participant fields.
        """
        last_sequence = -1
        learned: list[int] = []
        last_learned_at = 0.0
        armed = False
        try:
            with open_process(pid, write_memory=True) as process:
                while not self._learner_stop.wait(0.02):
                    row = read_process_memory(process, table, 0xA1)
                    if not row or len(row) < 0xA1:
                        continue
                    sequence, _, participant_288, participant_290, managed_actions, red_actions = struct.unpack_from("<QQQQQQ", row)
                    self.observed_events = sequence
                    self.managed_actions = managed_actions
                    self.red_actions = red_actions
                    self.opponent_candidates, self.skipped_unarmed = struct.unpack_from("<QQ", row, 0x80)
                    self.last_primary_adjudication = struct.unpack_from("<I", row, 0x90)[0]
                    self.last_secondary_adjudication = struct.unpack_from("<I", row, 0x94)[0]
                    self.armed = bool(row[0xA0])
                    if learned and not armed and time.monotonic() - last_learned_at >= 0.5:
                        write_process_memory(process, table + 0xA0, b"\x01")
                        armed = True
                        self.armed = True
                    if sequence == last_sequence:
                        continue
                    last_sequence = sequence
                    for participant in (participant_288, participant_290):
                        if participant < 0x100000 or participant in learned:
                            continue
                        block = read_process_memory(process, participant, 0xB0)
                        if not block or len(block) < 0xB0:
                            continue
                        candidate_team = struct.unpack_from("<Q", block, 0xA8)[0]
                        is_managed = candidate_team == team_address
                        if not is_managed and club_address and candidate_team >= 0x100000:
                            team_block = read_process_memory(process, candidate_team, 0x38)
                            if team_block and len(team_block) >= 0x38:
                                is_managed = struct.unpack_from("<Q", team_block, 0x30)[0] == club_address
                        if is_managed:
                            write_process_memory(process, table + 0xA0, b"\0")
                            armed = False
                            learned.append(participant)
                            learned = learned[-REFEREE_MAX_PARTICIPANTS:]
                            slots = b"".join(struct.pack("<Q", value) for value in learned)
                            slots += b"\0" * (REFEREE_MAX_PARTICIPANTS * 8 - len(slots))
                            write_process_memory(process, table + 0x40, slots)
                            self.learned_participants = list(learned)
                            last_learned_at = time.monotonic()
        except Exception as error:
            if not self._learner_stop.is_set():
                self.error = f"黑哨参与方识别失败：{error}"

    def _uninstall(self) -> None:
        if not self.hook:
            self._clear(); return
        self._stop_learner()
        try:
            with open_process(self.pid, write_memory=True) as process:
                if read_process_memory(process, self.hook, len(self.patch)) == self.patch:
                    self._write_code(process, self.hook, self.original)
                time.sleep(0.1)
                if self.cave:
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
                process.handle, ctypes.c_void_p(address), len(data), old.value, ctypes.byref(restored)
            )

    def _clear(self) -> None:
        self.pid = self.hook = self.cave = self.team_address = self.club_address = self.level = 0
        self.original = self.patch = b""
        self.learned_participants = []
        self.observed_events = self.managed_actions = self.red_actions = 0
        self.opponent_candidates = self.skipped_unarmed = 0
        self.last_primary_adjudication = self.last_secondary_adjudication = 0
        self.armed = False
