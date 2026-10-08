from __future__ import annotations

import ctypes
import mmap
import struct
import threading
import time
from ctypes import wintypes
from pathlib import Path
from typing import Any

from fm_collector.win32 import (
    MEM_IMAGE,
    PAGE_EXECUTE_READ,
    PAGE_EXECUTE_READWRITE,
    PAGE_EXECUTE_WRITECOPY,
    iter_readable_regions,
    kernel32,
    open_process,
    read_process_memory,
    write_process_memory,
)
from tools.initial_data_audit import (
    Reader,
    select_process_layout,
)
from tools.database_index import resolve_team_club
from tools.game_session import borrow_game_reader
from tools.hook_native_core import pattern_matches as _native_pattern_matches
from tools.redbull_hook import MEM_COMMIT_RESERVE, MEM_RELEASE, _Code, _rel32


MAX_PLANS = 32
SON_CA_MIN, SON_CA_MAX = 50, 150
SON_PA_MIN, SON_PA_MAX = 80, 200
ALLOCATION_SIZE = 0x2000
GATE_CODE_OFFSET = 0x000
PA_CODE_OFFSET = 0x200
STATE_CODE_OFFSET = 0x400
SON_CODE_OFFSET = 0xC00
RUNTIME_OFFSET = 0x600
TABLE_OFFSET = 0x800
TABLE_HEADER_SIZE = 8
ENTRY_SIZE = 24
MARKER_OFFSET = 0x7E0
MARKER = b"FMODDYG3"
METADATA_OFFSET = 0x700
METADATA_MAGIC = b"FMODDYG4"
METADATA_ENTRY_SIZE = 32
HOOK_NAME_IDS = {
    "quality_gate": 1,
    "quality_capture": 2,
    "quality_pa": 3,
    "quality_state": 4,
    "son": 5,
}
HOOK_ID_NAMES = {value: key for key, value in HOOK_NAME_IDS.items()}
EXECUTE_MASK = PAGE_EXECUTE_READ | PAGE_EXECUTE_READWRITE | PAGE_EXECUTE_WRITECOPY

GATE_ORIGINAL = bytes.fromhex("48 89 D7 49 89 CD")
GATE_SIGNATURE = bytes.fromhex(
    "48 89 D7 49 89 CD 4C 8B 62 10 4D 85 E4 0F 84 2B 01 00 00"
)
PA_ORIGINAL = bytes.fromhex("66 45 89 10 66 44 89 0A")
PA_SIGNATURE = PA_ORIGINAL + b"\xC3"
STATE_ORIGINAL = bytes.fromhex("41 0F B6 46 33")
STATE_SIGNATURE = bytes.fromhex("41 0F B6 46 33 24 FE 31 FF 3C 08")
FM24_SON_ORIGINAL = bytes.fromhex("48 89 85 D0 00 00 00")
FM24_SON_SIGNATURE = bytes.fromhex(
    "E8 97 0A C0 FE 48 89 85 D0 00 00 00 48 8B 47 18 48 3B 47 20"
)
FM26_CAPTURE_ORIGINAL = bytes.fromhex("48 83 C4 28 5B")
FM26_CAPTURE_SIGNATURE = bytes.fromhex("F3 0F 2C C0 66 89 06 48 83 C4")
FM26_PA_ORIGINAL = bytes.fromhex("66 41 89 08 66 89 02")
FM26_PA_SIGNATURE = FM26_PA_ORIGINAL
FM26_SON_ORIGINAL = bytes.fromhex("48 89 85 38 01 00 00")
FM26_SON_SIGNATURE = bytes.fromhex(
    "E8 40 38 1B FE 48 89 85 38 01 00 00 49 8B 44 24 18"
)

SON_TARGET_OFFSET = RUNTIME_OFFSET + 0x40
SON_TEAM_ID_OFFSET = RUNTIME_OFFSET + 0x48
SON_TOKEN_OFFSET = RUNTIME_OFFSET + 0x4C
SON_REMAINING_OFFSET = RUNTIME_OFFSET + 0x50
SON_APPLIED_OFFSET = RUNTIME_OFFSET + 0x58
APPLIED_OFFSET = RUNTIME_OFFSET + 0x10
QUALITY_CAPTURE_HITS_OFFSET = RUNTIME_OFFSET + 0x20
QUALITY_PA_HITS_OFFSET = RUNTIME_OFFSET + 0x28
QUALITY_MATCH_HITS_OFFSET = RUNTIME_OFFSET + 0x30
LAST_CAPTURED_CLUB_OFFSET = RUNTIME_OFFSET + 0x38
SON_HITS_OFFSET = RUNTIME_OFFSET + 0x60
SON_MATCH_HITS_OFFSET = RUNTIME_OFFSET + 0x68
LAST_SON_CLUB_OFFSET = RUNTIME_OFFSET + 0x70
LAST_PA_CLUB_OFFSET = RUNTIME_OFFSET + 0x78
SON_PENDING_OFFSET = RUNTIME_OFFSET + 0x80
DIAGNOSTIC_RUNTIME_SIZE = 0x88

FM24_ADAPTIVE_PATTERNS: dict[str, tuple[tuple[int | None, ...], int, int, int]] = {
    "quality_gate": ((
        None, 0x89, None, None, 0x89, None, 0x4C, 0x8B, None, 0x10,
        0x4D, 0x85, None, 0x0F, 0x84, None, None, 0x00, 0x00,
    ), 0, len(GATE_ORIGINAL), GATE_CODE_OFFSET),
    "quality_pa": (tuple(PA_SIGNATURE), 0, len(PA_ORIGINAL), PA_CODE_OFFSET),
    "quality_state": (tuple(STATE_SIGNATURE[:7]), 0, len(STATE_ORIGINAL), STATE_CODE_OFFSET),
    "son": ((
        0xE8, None, None, None, None, 0x48, 0x89, 0x85, None, None,
        0x00, 0x00, 0x48, 0x8B, 0x47, 0x18, 0x48, 0x3B, 0x47, 0x20,
    ), 5, len(FM24_SON_ORIGINAL), SON_CODE_OFFSET),
}


def _file_match_count(path: str, signature: bytes) -> int:
    count = 0
    with Path(path).open("rb") as handle, mmap.mmap(
        handle.fileno(), 0, access=mmap.ACCESS_READ,
    ) as image:
        position = 0
        while True:
            position = image.find(signature, position)
            if position < 0:
                return count
            count += 1
            if count > 1:
                return count
            position += 1


def _pattern_matches(data: bytes, pattern: tuple[int | None, ...]) -> list[int]:
    if not pattern or all(value is None for value in pattern):
        return []
    return _native_pattern_matches(data, pattern)


def _scan_module_pattern(
    process: Any, module: Any, pattern: tuple[int | None, ...],
) -> list[int]:
    return _scan_module_patterns(process, module, {"pattern": pattern})["pattern"]


def _scan_module_patterns(
    process: Any, module: Any,
    patterns: dict[str, tuple[int | None, ...]],
) -> dict[str, list[int]]:
    matches: dict[str, list[int]] = {name: [] for name in patterns}
    module_end = int(module.base_address) + int(module.size)
    for region in iter_readable_regions(process):
        if (
            region.type != MEM_IMAGE
            or not region.protect & EXECUTE_MASK
            or region.base_address < module.base_address
            or region.base_address >= module_end
        ):
            continue
        size = min(region.size, module_end - region.base_address)
        data = read_process_memory(process, region.base_address, size)
        if not data:
            continue
        for name, pattern in patterns.items():
            matches[name].extend(
                region.base_address + offset - module.base_address
                for offset in _pattern_matches(data, pattern)
            )
    return matches


def _emit_active_counter(code: _Code, address: int, increment: bool) -> None:
    code.emit(b"\x48\xB8" + struct.pack("<Q", address))
    code.emit(b"\xF0\x48\xFF" + (b"\x00" if increment else b"\x08"))


def _build_gate_code(
    cave: int, original: bytes, return_address: int,
) -> bytes:
    runtime = cave - GATE_CODE_OFFSET + RUNTIME_OFFSET
    table = cave - GATE_CODE_OFFSET + TABLE_OFFSET
    selected = runtime + 8
    code = _Code(cave)
    code.emit(original)
    code.emit(b"\x9C\x50\x51\x53")  # flags, rax, rcx, rbx
    _emit_active_counter(code, runtime, True)
    code.emit(b"\x48\xB8" + struct.pack("<Q", selected))
    code.emit(b"\x48\xC7\x00\x00\x00\x00\x00")
    code.emit(b"\x48\x8B\x42\x10")  # generated object club
    code.emit(b"\x48\x3D\x00\x00\x01\x00")
    code.jump32(b"\x0F\x86", "exit")
    code.emit(b"\x48\xBB" + struct.pack("<Q", table))
    code.emit(b"\x8B\x0B\x48\x83\xC3" + bytes([TABLE_HEADER_SIZE]))
    code.emit(b"\x85\xC9")
    code.jump32(b"\x0F\x84", "exit")
    code.label("loop")
    code.emit(b"\x48\x3B\x03")
    code.jump32(b"\x0F\x85", "next")
    code.emit(b"\x83\x7B\x0C\x00")
    code.jump32(b"\x0F\x8E", "exit")
    code.emit(b"\x48\xB8" + struct.pack("<Q", selected) + b"\x48\x89\x18")
    code.emit(b"\xC6\x42\x33\x01")
    code.jump32(b"\xE9", "exit")
    code.label("next")
    code.emit(b"\x48\x83\xC3" + bytes([ENTRY_SIZE]) + b"\xFF\xC9")
    code.jump32(b"\x0F\x85", "loop")
    code.label("exit")
    _emit_active_counter(code, runtime, False)
    code.emit(b"\x5B\x59\x58\x9D\xE9")
    code.emit(_rel32(cave + len(code.data) + 4, return_address))
    return code.finish()


def _build_pa_code(cave: int, original: bytes, return_address: int) -> bytes:
    allocation = cave - PA_CODE_OFFSET
    runtime = allocation + RUNTIME_OFFSET
    selected = runtime + 8
    applied = runtime + 16
    code = _Code(cave)
    code.emit(b"\x9C\x50\x51\x53")  # flags, rax, rcx, rbx
    _emit_active_counter(code, runtime, True)
    code.emit(b"\x48\xB8" + struct.pack("<Q", selected) + b"\x48\x8B\x18")
    code.emit(b"\x48\x85\xDB")
    code.jump32(b"\x0F\x84", "exit")
    code.emit(b"\x48\xB8" + struct.pack("<Q", allocation + SON_PENDING_OFFSET))
    code.emit(b"\x83\x38\x00")
    code.jump32(b"\x0F\x84", "quality")
    code.emit(b"\x48\xB8" + struct.pack("<Q", allocation + SON_TARGET_OFFSET))
    code.emit(b"\x48\x8B\x08\x48\x39\x0B")
    code.jump32(b"\x0F\x85", "quality")
    code.emit(b"\x66\x44\x8B\x48\x16")
    code.emit(b"\x66\x44\x8B\x50\x16")
    code.emit(b"\x48\xB8" + struct.pack("<Q", allocation + SON_PENDING_OFFSET))
    code.emit(b"\xC7\x00\x00\x00\x00\x00")
    code.jump32(b"\xE9", "clear")
    code.label("quality")
    code.emit(b"\x83\x7B\x0C\x00")
    code.jump32(b"\x0F\x8E", "clear")
    code.emit(b"\x66\x44\x8B\x4B\x08")
    code.emit(b"\x66\x44\x8B\x53\x0A")
    code.emit(b"\xFF\x4B\x0C")
    # FM24 keeps the club-gated, paid-count PA path.  Final UID reconciliation
    # remains authoritative and fills any slots consumed by temporary callbacks.
    _emit_active_counter(code, applied, True)
    code.label("clear")
    code.emit(b"\x48\xB8" + struct.pack("<Q", selected))
    code.emit(b"\x48\xC7\x00\x00\x00\x00\x00")
    code.label("exit")
    _emit_active_counter(code, runtime, False)
    code.emit(b"\x5B\x59\x58\x9D")
    code.emit(original)
    code.emit(b"\xE9" + _rel32(cave + len(code.data) + 5, return_address))
    return code.finish()


def _build_state_code(cave: int, original: bytes, return_address: int) -> bytes:
    allocation = cave - STATE_CODE_OFFSET
    runtime = allocation + RUNTIME_OFFSET
    table = allocation + TABLE_OFFSET
    son_target = allocation + SON_TARGET_OFFSET
    code = _Code(cave)
    code.emit(b"\x9C\x50\x51\x53")
    _emit_active_counter(code, runtime, True)
    code.emit(b"\x41\x80\x7E\x33\x09")
    code.jump32(b"\x0F\x85", "original")
    code.emit(b"\x49\x8B\x46\x10")  # generated object club
    code.emit(b"\x48\x85\xC0")
    code.jump32(b"\x0F\x84", "original")
    code.emit(b"\x48\xBB" + struct.pack("<Q", son_target))
    code.emit(b"\x48\x3B\x03")
    code.jump32(b"\x0F\x84", "original")
    code.emit(b"\x48\xBB" + struct.pack("<Q", table))
    code.emit(b"\x8B\x0B\x48\x83\xC3" + bytes([TABLE_HEADER_SIZE]))
    code.emit(b"\x85\xC9")
    code.jump32(b"\x0F\x84", "original")
    code.label("loop")
    code.emit(b"\x48\x3B\x03")
    code.jump32(b"\x0F\x84", "normalize")
    code.emit(b"\x48\x83\xC3" + bytes([ENTRY_SIZE]) + b"\xFF\xC9")
    code.jump32(b"\x0F\x85", "loop")
    code.jump32(b"\xE9", "original")
    code.label("normalize")
    code.emit(b"\x41\xC6\x46\x33\x01")
    code.label("original")
    _emit_active_counter(code, runtime, False)
    code.emit(b"\x5B\x59\x58\x9D")
    code.emit(original + b"\x24\xFE")
    code.emit(b"\xE9" + _rel32(cave + len(code.data) + 5, return_address))
    return code.finish()


def _build_fm26_capture_code(
    cave: int, original: bytes, return_address: int,
) -> bytes:
    allocation = cave - GATE_CODE_OFFSET
    runtime = allocation + RUNTIME_OFFSET
    selected = runtime + 8
    code = _Code(cave)
    code.emit(original)
    code.emit(b"\x9C\x50")
    _emit_active_counter(code, runtime, True)
    _emit_active_counter(code, allocation + QUALITY_CAPTURE_HITS_OFFSET, True)
    code.emit(
        b"\x48\xB8" + struct.pack("<Q", allocation + LAST_CAPTURED_CLUB_OFFSET)
    )
    code.emit(b"\x4C\x89\x30")
    code.emit(b"\x48\xB8" + struct.pack("<Q", selected))
    code.emit(b"\x4C\x89\x30")  # captured youth club from r14
    _emit_active_counter(code, runtime, False)
    code.emit(b"\x58\x9D\xE9")
    code.emit(_rel32(cave + len(code.data) + 4, return_address))
    return code.finish()


def _build_fm26_pa_code(
    cave: int, original: bytes, return_address: int,
) -> bytes:
    allocation = cave - PA_CODE_OFFSET
    runtime = allocation + RUNTIME_OFFSET
    selected = runtime + 8
    applied = runtime + 16
    table = allocation + TABLE_OFFSET
    code = _Code(cave)
    code.emit(original)
    code.emit(b"\x9C\x50\x51\x53\x41\x51\x41\x52")
    _emit_active_counter(code, runtime, True)
    _emit_active_counter(code, allocation + QUALITY_PA_HITS_OFFSET, True)
    code.emit(b"\x48\xB8" + struct.pack("<Q", selected) + b"\x48\x8B\x00")
    code.emit(b"\x48\x85\xC0")
    code.jump32(b"\x0F\x84", "clear")
    code.emit(b"\x48\xBB" + struct.pack("<Q", allocation + LAST_PA_CLUB_OFFSET))
    code.emit(b"\x48\x89\x03")
    code.emit(b"\x48\xBB" + struct.pack("<Q", allocation + SON_PENDING_OFFSET))
    code.emit(b"\x83\x3B\x00")
    code.jump32(b"\x0F\x84", "quality")
    code.emit(b"\x48\xBB" + struct.pack("<Q", allocation + SON_TARGET_OFFSET))
    code.emit(b"\x48\x3B\x03")
    code.jump32(b"\x0F\x85", "quality")
    code.emit(b"\x0F\xB7\x4B\x16")
    code.emit(b"\x66\x89\x0A\x66\x41\x89\x08")
    code.emit(b"\x66\xC7\x06\xF6\xFF")
    code.emit(b"\x48\xBB" + struct.pack("<Q", allocation + SON_PENDING_OFFSET))
    code.emit(b"\xC7\x03\x00\x00\x00\x00")
    code.jump32(b"\xE9", "clear")
    code.label("quality")
    code.emit(b"\x48\xBB" + struct.pack("<Q", table))
    code.emit(b"\x8B\x0B\x48\x83\xC3" + bytes([TABLE_HEADER_SIZE]))
    code.emit(b"\x85\xC9")
    code.jump32(b"\x0F\x84", "clear")
    code.label("loop")
    code.emit(b"\x48\x3B\x03")
    code.jump32(b"\x0F\x85", "next")
    code.emit(b"\x83\x7B\x0C\x00")
    code.jump32(b"\x0F\x8E", "clear")
    _emit_active_counter(code, allocation + QUALITY_MATCH_HITS_OFFSET, True)
    # Keep the original PA writes emitted at entry.  Quality callbacks observe
    # the matching club but never change PA or generation state; exact-count
    # changes are made only after stable player UIDs can be reconciled.
    _emit_active_counter(code, applied, True)
    code.jump32(b"\xE9", "clear")
    code.label("next")
    code.emit(b"\x48\x83\xC3" + bytes([ENTRY_SIZE]) + b"\xFF\xC9")
    code.jump32(b"\x0F\x85", "loop")
    code.label("clear")
    code.emit(b"\x48\xB8" + struct.pack("<Q", selected))
    code.emit(b"\x48\xC7\x00\x00\x00\x00\x00")
    _emit_active_counter(code, runtime, False)
    code.emit(b"\x41\x5A\x41\x59\x5B\x59\x58\x9D\xE9")
    code.emit(_rel32(cave + len(code.data) + 4, return_address))
    return code.finish()


def _build_son_code(
    cave: int, original: bytes, return_address: int, *, game_key: str,
) -> bytes:
    allocation = cave - SON_CODE_OFFSET
    runtime = allocation + RUNTIME_OFFSET
    target = allocation + SON_TARGET_OFFSET
    selected_slot = allocation + SON_REMAINING_OFFSET
    applied = allocation + SON_APPLIED_OFFSET
    code = _Code(cave)
    code.emit(original)
    code.emit(b"\x9C\x50")
    _emit_active_counter(code, runtime, True)
    _emit_active_counter(code, allocation + SON_HITS_OFFSET, True)
    code.emit(b"\x48\xB8" + struct.pack("<Q", allocation + LAST_SON_CLUB_OFFSET))
    if game_key == "fm24":
        code.emit(b"\x48\x89\x38")
    elif game_key == "fm26":
        code.emit(b"\x4C\x89\x20")
    code.emit(b"\x48\xB8" + struct.pack("<Q", target) + b"\x48\x8B\x00")
    code.emit(b"\x48\x85\xC0")
    code.jump32(b"\x0F\x84", "exit")
    if game_key == "fm24":
        code.emit(b"\x48\x39\xC7")  # rdi is the generated club
    elif game_key == "fm26":
        code.emit(b"\x49\x39\xC4")  # r12 is the generated club
    else:
        raise ValueError("unsupported youth son hook generation")
    code.jump32(b"\x0F\x85", "exit")
    _emit_active_counter(code, allocation + SON_MATCH_HITS_OFFSET, True)
    code.emit(b"\x48\xB8" + struct.pack("<Q", applied) + b"\x48\x83\x38\x00")
    code.jump32(b"\x0F\x85", "exit")
    code.emit(b"\x40\xB6\x02")
    code.emit(b"\x48\xB8" + struct.pack("<Q", selected_slot))
    code.emit(b"\x66\x44\x39\x28")
    code.jump32(b"\x0F\x85", "exit")
    code.emit(b"\x40\xB6\x09")
    code.emit(b"\x48\xB8" + struct.pack("<Q", allocation + SON_PENDING_OFFSET))
    code.emit(b"\xC7\x00\x01\x00\x00\x00")
    _emit_active_counter(code, applied, True)
    code.label("exit")
    _emit_active_counter(code, runtime, False)
    code.emit(b"\x58\x9D\xE9")
    code.emit(_rel32(cave + len(code.data) + 4, return_address))
    return code.finish()


class YouthGenerationHookController:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.pid = self.cave = 0
        self.hooks: dict[str, int] = {}
        self.originals: dict[str, bytes] = {}
        self.patches: dict[str, bytes] = {}
        self.targets: list[dict[str, Any]] = []
        self.son_target: dict[str, Any] | None = None
        self._adaptive_cache: dict[
            tuple[int, int, int], dict[str, tuple[int, bytes, bytes, int]]
        ] = {}
        self.game_key = ""
        self.error: str | None = None

    def status(self) -> dict[str, Any]:
        with self.lock:
            targets = [dict(row) for row in self.targets]
            son_target = dict(self.son_target) if self.son_target else None
            applied = 0
            son_applied = 0
            son_pending = 0
            diagnostics = {
                "quality_capture_hits": 0,
                "quality_pa_hits": 0,
                "quality_match_hits": 0,
                "son_hits": 0,
                "son_match_hits": 0,
                "last_captured_club": None,
                "last_pa_club": None,
                "last_son_club": None,
                "active_calls": 0,
                "selected_club": None,
            }
            hook_integrity: dict[str, bool] = {}
            marker_integrity = False
            diagnostic_error = None
            if self.pid and self.cave:
                try:
                    with open_process(self.pid) as process:
                        runtime_raw = read_process_memory(
                            process, self.cave + RUNTIME_OFFSET,
                            DIAGNOSTIC_RUNTIME_SIZE,
                        ) or b""
                        if len(runtime_raw) >= DIAGNOSTIC_RUNTIME_SIZE:
                            def runtime_qword(offset: int) -> int:
                                return struct.unpack_from(
                                    "<Q", runtime_raw, offset - RUNTIME_OFFSET,
                                )[0]

                            applied = runtime_qword(APPLIED_OFFSET)
                            diagnostics.update({
                                "active_calls": runtime_qword(RUNTIME_OFFSET),
                                "selected_club": hex(runtime_qword(RUNTIME_OFFSET + 8))
                                if runtime_qword(RUNTIME_OFFSET + 8) else None,
                                "quality_capture_hits": runtime_qword(QUALITY_CAPTURE_HITS_OFFSET),
                                "quality_pa_hits": runtime_qword(QUALITY_PA_HITS_OFFSET),
                                "quality_match_hits": runtime_qword(QUALITY_MATCH_HITS_OFFSET),
                                "son_hits": runtime_qword(SON_HITS_OFFSET),
                                "son_match_hits": runtime_qword(SON_MATCH_HITS_OFFSET),
                                "last_captured_club": hex(runtime_qword(LAST_CAPTURED_CLUB_OFFSET))
                                if runtime_qword(LAST_CAPTURED_CLUB_OFFSET) else None,
                                "last_pa_club": hex(runtime_qword(LAST_PA_CLUB_OFFSET))
                                if runtime_qword(LAST_PA_CLUB_OFFSET) else None,
                                "last_son_club": hex(runtime_qword(LAST_SON_CLUB_OFFSET))
                                if runtime_qword(LAST_SON_CLUB_OFFSET) else None,
                            })
                            son_applied = runtime_qword(SON_APPLIED_OFFSET)
                            son_pending = runtime_qword(SON_PENDING_OFFSET)
                        table = read_process_memory(
                            process, self.cave + TABLE_OFFSET,
                            TABLE_HEADER_SIZE + len(targets) * ENTRY_SIZE,
                        )
                        if table and len(table) >= TABLE_HEADER_SIZE:
                            for index, row in enumerate(targets):
                                offset = TABLE_HEADER_SIZE + index * ENTRY_SIZE
                                if offset + ENTRY_SIZE <= len(table):
                                    row["remaining"] = struct.unpack_from(
                                        "<I", table, offset + 12,
                                    )[0]
                        son_raw = runtime_raw[
                            SON_REMAINING_OFFSET - RUNTIME_OFFSET:
                            SON_REMAINING_OFFSET - RUNTIME_OFFSET + 16
                        ]
                        if len(son_raw) == 16:
                            if son_target:
                                son_target["slot"] = struct.unpack_from(
                                    "<I", son_raw,
                                )[0]
                                son_target["remaining"] = 0 if son_applied else 1
                        for name, address in self.hooks.items():
                            patch = self.patches.get(name, b"")
                            hook_integrity[name] = bool(
                                patch
                                and read_process_memory(process, address, len(patch)) == patch
                            )
                        marker_integrity = bool(
                            self.cave
                            and read_process_memory(
                                process, self.cave + MARKER_OFFSET, len(MARKER),
                            ) == MARKER
                        )
                except Exception as error:
                    diagnostic_error = str(error)
            installed = bool(self.hooks) and marker_integrity and all(
                hook_integrity.get(name) is True for name in self.hooks
            )
            return {
                "supported": True,
                "priority": "native_first",
                "completion_authority": "final_roster_reconcile",
                "installed": installed,
                "tracked": bool(self.hooks),
                "game_key": self.game_key or None,
                "verification": (
                    "runtime_verified"
                    if applied > 0 or son_applied > 0
                    else "experimental_unverified"
                    if self.game_key == "fm26"
                    else "supported"
                ),
                "process_id": self.pid or None,
                "cave_address": hex(self.cave) if self.cave else None,
                "hooks": {
                    name: {
                        "address": hex(address),
                        "patched": hook_integrity.get(name),
                    }
                    for name, address in self.hooks.items()
                },
                "marker_valid": marker_integrity,
                "targets": targets,
                "applied_count": applied,
                "son_target": son_target,
                "son_applied_count": son_applied,
                "son_pending": bool(son_pending),
                "diagnostics": diagnostics,
                "diagnostic_error": diagnostic_error,
                "error": self.error,
            }

    def reconcile_son_quality(self) -> dict[str, Any]:
        with self.lock:
            if not self.pid or not self.cave or not self.son_target:
                return {"reconciled": False, "pending": False, "compensated": False}
            with open_process(self.pid, write_memory=True) as process:
                runtime = read_process_memory(
                    process, self.cave + RUNTIME_OFFSET, DIAGNOSTIC_RUNTIME_SIZE,
                ) or b""
                if len(runtime) < DIAGNOSTIC_RUNTIME_SIZE:
                    raise RuntimeError("无法读取儿子质量隔离状态")
                active_calls = struct.unpack_from("<Q", runtime, 0)[0]
                pending = struct.unpack_from(
                    "<Q", runtime, SON_PENDING_OFFSET - RUNTIME_OFFSET,
                )[0]
                applied = struct.unpack_from(
                    "<Q", runtime, SON_APPLIED_OFFSET - RUNTIME_OFFSET,
                )[0]
                if not pending or not applied:
                    return {
                        "reconciled": False, "pending": bool(pending),
                        "compensated": False,
                    }
                if active_calls:
                    return {
                        "reconciled": False, "pending": True,
                        "compensated": False, "reason": "hook_active",
                    }
                cleared = struct.pack("<Q", 0)
                write_process_memory(
                    process, self.cave + SON_PENDING_OFFSET, cleared,
                )
                if read_process_memory(
                    process, self.cave + SON_PENDING_OFFSET, len(cleared),
                ) != cleared:
                    raise RuntimeError("儿子质量隔离标记清理失败")
                return {
                    "reconciled": True, "pending": False,
                    "compensated": False,
                }

    def sync(self, plans: list[dict[str, Any]]) -> dict[str, Any]:
        with self.lock:
            try:
                plans = [
                    plan for plan in plans
                    if not plan.get("roster_finalize_only")
                ]
                if not plans:
                    if not self.hooks:
                        try:
                            pid, _path, layout = select_process_layout()
                            self._adopt_existing(pid, layout)
                        except Exception:
                            pass
                    self._uninstall()
                    self.error = None
                    return self.status()
                pid, process_path, layout = select_process_layout()
                quality_requested = any(
                    str(plan.get("kind") or "") == "golden_generation"
                    for plan in plans
                )
                son_requested = any(
                    str(plan.get("kind") or "") == "academy_son"
                    for plan in plans
                )
                if layout.key not in {"fm24", "fm26"}:
                    raise RuntimeError("当前 Football Manager 版本尚未适配青训生成")
                if layout.key == "fm26" and quality_requested and (
                    layout.youth_generation_gate_hook_rva is None
                    or layout.youth_generation_pa_hook_rva is None
                ):
                    raise RuntimeError("当前 Football Manager 版本尚未适配青训质量")
                if (
                    layout.key == "fm26" and son_requested
                    and layout.youth_son_hook_rva is None
                ):
                    raise RuntimeError("当前 Football Manager 版本尚未适配青训儿子")
                if self.pid and self.pid != pid:
                    self._uninstall()
                targets, son_target = self._resolve_targets(
                    pid, layout, plans, process_path,
                )
                if self.hooks and not self._hooks_intact(pid):
                    self._uninstall()
                if not self.hooks and not self._adopt_existing(pid, layout):
                    self._install(
                        pid, layout, quality=quality_requested, son=son_requested,
                    )
                desired = self._desired_names(
                    layout, quality=quality_requested, son=son_requested,
                )
                if set(self.hooks) != desired:
                    self._uninstall()
                    self._install(
                        pid, layout, quality=quality_requested, son=son_requested,
                    )
                self._write_targets(targets, son_target)
                self.error = None
            except Exception as error:
                message = str(error)
                self.error = (
                    f"{type(error).__name__}: {message[:240]}..."
                    if len(message) > 240 else message
                )
                if self.hooks:
                    self._uninstall()
                else:
                    self._clear()
            return self.status()

    def _hooks_intact(self, pid: int) -> bool:
        if not self.hooks or self.pid != pid or not self.cave:
            return False
        try:
            with open_process(pid) as process:
                if read_process_memory(
                    process, self.cave + MARKER_OFFSET, len(MARKER),
                ) != MARKER:
                    return False
                return all(
                    read_process_memory(
                        process, address, len(self.patches.get(name, b"")),
                    ) == self.patches.get(name, b"")
                    for name, address in self.hooks.items()
                    if self.patches.get(name)
                ) and len(self.patches) == len(self.hooks)
        except Exception:
            return False

    def close(self) -> None:
        with self.lock:
            self._uninstall()

    def suspend(self, error: str) -> dict[str, Any]:
        with self.lock:
            self._uninstall()
            self.error = str(error or "") or None
            return self.status()

    def _resolve_targets(
        self, pid: int, layout: Any, plans: list[dict[str, Any]],
        process_path: str | None = None,
    ) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
        normalized: list[dict[str, Any]] = []
        son_target: dict[str, Any] | None = None
        # A hook plan can outlive a game reload.  Use the verified read-session
        # Reader when the caller has the selected process path so
        # ``resolve_team_club`` can consult the session-owned UID directory.
        # Tests and older callers may omit the path; retain the direct Reader
        # fallback for those short-lived adapters.
        from contextlib import ExitStack

        with ExitStack() as stack:
            reader = None
            module = None
            if process_path:
                try:
                    reader = stack.enter_context(
                        borrow_game_reader((int(pid), str(process_path), layout))
                    )
                    module = getattr(reader, "module", None)
                except (OSError, RuntimeError, TypeError, ValueError):
                    reader = None
                    module = None
            if reader is None:
                process = stack.enter_context(open_process(pid))
                module = layout.module(process)
                if module is None:
                    raise RuntimeError("fm.exe 尚未加载")
                reader = Reader(process, module.base_address, layout)
            if module is None:
                module = layout.module(reader.process)
            if module is None:
                raise RuntimeError("fm.exe 尚未加载")
            for plan in plans[:MAX_PLANS]:
                team_id = int(plan.get("team_id") or 0)
                team_hint = int(str(plan.get("team_address") or "0"), 0)
                kind = str(plan.get("kind") or "")
                min_pa = int(plan.get("min_pa") or 0)
                max_pa = int(plan.get("max_pa") or 0)
                count = int(plan.get("count") or 0)
                token = int(plan.get("token") or 0) & 0xFFFFFFFF
                if kind == "golden_generation" and (
                    not 1 <= min_pa < max_pa <= 200 or not 1 <= count <= 10
                ):
                    raise ValueError("青训黄金一代参数无效")
                if kind == "academy_son" and count != 1:
                    raise ValueError("儿子历练计划固定为 1 人")
                son_ca = int(plan.get("ca") or 0)
                son_pa = int(plan.get("pa") or 0)
                son_slot = int(plan.get("slot", -1))
                if kind == "academy_son" and not (
                    SON_CA_MIN <= son_ca <= SON_CA_MAX
                    and SON_PA_MIN <= son_pa <= SON_PA_MAX
                    and son_pa > son_ca
                    and 0 <= son_slot < 10
                ):
                    raise ValueError("儿子 CA/PA 或随机候选位置无效")
                if kind not in {"golden_generation", "academy_son"}:
                    raise ValueError("未知的青训计划")
                if team_id <= 0:
                    raise RuntimeError(f"俱乐部 {team_id} 的球队 ID 无效")
                try:
                    # Plans may outlive a game reload.  Resolve by stable Team
                    # UID through the current native directory first, retaining
                    # the persisted address only as a validated session hint.
                    resolved = resolve_team_club(reader, team_hint, team_id)
                except (OSError, RuntimeError, TypeError, ValueError) as error:
                    raise RuntimeError(
                        f"俱乐部 {team_id} 的球队/俱乐部对象已失效，请重新读取"
                    ) from error
                team = int(resolved.team_address)
                club = int(resolved.club_address)
                target = {
                    "team_id": team_id,
                    "club_address": club,
                    "count": count,
                    "token": token,
                    "kind": kind,
                }
                if kind == "golden_generation":
                    target.update({"min_pa": min_pa, "max_pa": max_pa})
                    normalized.append(target)
                elif son_target is None:
                    target.update({
                        "ca": son_ca, "pa": son_pa, "slot": son_slot,
                    })
                    son_target = target
                else:
                    raise ValueError("儿子历练计划同时只能指定一个俱乐部")
        return normalized, son_target

    @staticmethod
    def _allocate_near(process: Any, hook: int) -> int:
        granularity = 0x10000
        origin = hook & ~(granularity - 1)
        for distance in range(granularity, 0x70000000, granularity):
            for hint in (origin + distance, max(granularity, origin - distance)):
                address = kernel32.VirtualAllocEx(
                    process.handle, ctypes.c_void_p(hint), ALLOCATION_SIZE,
                    MEM_COMMIT_RESERVE, PAGE_EXECUTE_READWRITE,
                )
                value = int(address or 0)
                if value and abs(value - hook) < 0x7FFFFFFF:
                    return value
                if value:
                    kernel32.VirtualFreeEx(
                        process.handle, ctypes.c_void_p(value), 0, MEM_RELEASE,
                    )
        raise RuntimeError("无法在青训生成入口附近分配内存")

    @staticmethod
    def _desired_names(
        layout: Any, *, quality: bool, son: bool,
    ) -> set[str]:
        names: set[str] = set()
        if quality:
            names.update(
                {"quality_gate", "quality_pa"}
                if layout.key == "fm24" else {"quality_capture", "quality_pa"}
            )
        if son:
            names.add("son")
        return names

    @classmethod
    def _resolve_definitions(
        cls, process: Any, module: Any, layout: Any, *, quality: bool, son: bool,
    ) -> dict[str, tuple[int, bytes, bytes, int]]:
        if layout.key != "fm24":
            return cls._definitions(layout, quality=quality, son=son)
        wanted = cls._desired_names(layout, quality=quality, son=son)
        definitions: dict[str, tuple[int, bytes, bytes, int]] = {}
        resolved = _scan_module_patterns(
            process, module, {
                name: FM24_ADAPTIVE_PATTERNS[name][0]
                for name in wanted
            },
        )
        for name in ("quality_gate", "quality_pa", "quality_state", "son"):
            if name not in wanted:
                continue
            pattern, patch_offset, original_size, code_offset = FM24_ADAPTIVE_PATTERNS[name]
            matches = resolved[name]
            if len(matches) != 1:
                raise RuntimeError(
                    f"FM24 青训 {name} 动态特征码命中 {len(matches)} 处"
                )
            rva = int(matches[0]) + patch_offset
            original = read_process_memory(
                process, module.base_address + rva, original_size,
            )
            if not original or len(original) != original_size:
                raise RuntimeError(f"FM24 青训 {name} 原始指令读取失败")
            definitions[name] = (rva, original, b"", code_offset)
        return definitions

    @staticmethod
    def _definitions(
        layout: Any, *, quality: bool, son: bool,
    ) -> dict[str, tuple[int, bytes, bytes, int]]:
        definitions: dict[str, tuple[int, bytes, bytes, int]] = {}
        if quality and layout.key == "fm24":
            definitions.update({
                "quality_gate": (
                    int(layout.youth_generation_gate_hook_rva), GATE_ORIGINAL,
                    GATE_SIGNATURE, GATE_CODE_OFFSET,
                ),
                "quality_pa": (
                    int(layout.youth_generation_pa_hook_rva), PA_ORIGINAL,
                    PA_SIGNATURE, PA_CODE_OFFSET,
                ),
                "quality_state": (
                    int(layout.youth_generation_state_hook_rva), STATE_ORIGINAL,
                    STATE_SIGNATURE, STATE_CODE_OFFSET,
                ),
            })
        elif quality and layout.key == "fm26":
            definitions.update({
                "quality_capture": (
                    int(layout.youth_generation_gate_hook_rva), FM26_CAPTURE_ORIGINAL,
                    FM26_CAPTURE_SIGNATURE, GATE_CODE_OFFSET,
                ),
                "quality_pa": (
                    int(layout.youth_generation_pa_hook_rva), FM26_PA_ORIGINAL,
                    FM26_PA_SIGNATURE, PA_CODE_OFFSET,
                ),
            })
        if son:
            if layout.key == "fm24":
                definitions["son"] = (
                    int(layout.youth_son_hook_rva), FM24_SON_ORIGINAL,
                    FM24_SON_SIGNATURE, SON_CODE_OFFSET,
                )
            elif layout.key == "fm26":
                definitions["son"] = (
                    int(layout.youth_son_hook_rva), FM26_SON_ORIGINAL,
                    FM26_SON_SIGNATURE, SON_CODE_OFFSET,
                )
        return definitions

    @staticmethod
    def _build_code(
        layout: Any, name: str, cave: int, hook: int, original: bytes,
    ) -> bytes:
        return_address = hook + len(original)
        if name == "quality_gate":
            return _build_gate_code(cave, original, return_address)
        if name == "quality_capture":
            return _build_fm26_capture_code(cave, original, return_address)
        if name == "quality_pa" and layout.key == "fm24":
            return _build_pa_code(cave, original, return_address)
        if name == "quality_pa" and layout.key == "fm26":
            return _build_fm26_pa_code(cave, original, return_address)
        if name == "quality_state":
            return _build_state_code(cave, original, return_address)
        if name == "son":
            return _build_son_code(
                cave, original, return_address, game_key=layout.key,
            )
        raise ValueError(f"未知的青训：{name}")

    def _install(
        self, pid: int, layout: Any, *, quality: bool, son: bool,
    ) -> None:
        with open_process(pid, write_memory=True) as process:
            module = layout.module(process)
            if module is None:
                raise RuntimeError(f"{layout.module_name} 尚未加载")
            if layout.key == "fm24":
                cache_key = (pid, int(module.base_address), int(module.size))
                cached = self._adaptive_cache.setdefault(cache_key, {})
                wanted = self._desired_names(layout, quality=quality, son=son)
                missing = wanted - set(cached)
                if missing:
                    cached.update(self._resolve_definitions(
                        process, module, layout,
                        quality=any(name.startswith("quality_") for name in missing),
                        son="son" in missing,
                    ))
                definitions = {
                    name: cached[name]
                    for name in ("quality_gate", "quality_pa", "quality_state", "son")
                    if name in wanted
                }
                self._adaptive_cache = {cache_key: cached}
            else:
                definitions = self._resolve_definitions(
                    process, module, layout, quality=quality, son=son,
                )
            if not definitions:
                raise RuntimeError("没有需要安装的青训生成")
            for name, (rva, original, signature, _offset) in definitions.items():
                if signature and _file_match_count(module.path, signature) != 1:
                    raise RuntimeError(f"{layout.key.upper()} 青训 {name} 特征码不是唯一命中")
                current = read_process_memory(
                    process, module.base_address + rva, len(original),
                )
                if current != original:
                    raise RuntimeError(f"{layout.key.upper()} 青训 {name} 入口已被修改")

            first_rva = next(iter(definitions.values()))[0]
            cave = self._allocate_near(process, module.base_address + first_rva)
            hooks = {
                name: module.base_address + definition[0]
                for name, definition in definitions.items()
            }
            codes = {
                name: self._build_code(
                    layout, name, cave + definition[3], hooks[name], definition[1],
                )
                for name, definition in definitions.items()
            }
            limits = {
                GATE_CODE_OFFSET: PA_CODE_OFFSET,
                PA_CODE_OFFSET: STATE_CODE_OFFSET,
                STATE_CODE_OFFSET: RUNTIME_OFFSET,
                SON_CODE_OFFSET: ALLOCATION_SIZE,
            }
            if any(
                len(codes[name]) >= limits[definition[3]] - definition[3]
                for name, definition in definitions.items()
            ):
                kernel32.VirtualFreeEx(
                    process.handle, ctypes.c_void_p(cave), 0, MEM_RELEASE,
                )
                raise RuntimeError("青训生成代码布局超出预留空间")
            patched: list[str] = []
            try:
                for name, code in codes.items():
                    write_process_memory(
                        process, cave + definitions[name][3], code,
                    )
                write_process_memory(
                    process, cave + RUNTIME_OFFSET,
                    b"\0" * (TABLE_OFFSET - RUNTIME_OFFSET),
                )
                write_process_memory(process, cave + MARKER_OFFSET, MARKER)
                self._write_metadata(process, cave, hooks, definitions)
                write_process_memory(
                    process, cave + TABLE_OFFSET,
                    b"\0" * (TABLE_HEADER_SIZE + MAX_PLANS * ENTRY_SIZE),
                )
                kernel32.FlushInstructionCache(
                    process.handle, ctypes.c_void_p(cave), ALLOCATION_SIZE,
                )
                patches = {
                    name: b"\xE9" + _rel32(
                        hooks[name] + 5, cave + definitions[name][3],
                    ) + b"\x90" * (len(definitions[name][1]) - 5)
                    for name in definitions
                }
                for name in definitions:
                    self._write_code(process, hooks[name], patches[name])
                    patched.append(name)
                self.pid = pid
                self.cave = cave
                self.game_key = str(layout.key)
                self.hooks = hooks
                self.originals = {
                    name: definition[1] for name, definition in definitions.items()
                }
                self.patches = patches
            except Exception:
                for name in reversed(patched):
                    try:
                        self._write_code(process, hooks[name], definitions[name][1])
                    except Exception:
                        pass
                kernel32.VirtualFreeEx(
                    process.handle, ctypes.c_void_p(cave), 0, MEM_RELEASE,
                )
                raise

    @staticmethod
    def _write_metadata(
        process: Any, cave: int, hooks: dict[str, int],
        definitions: dict[str, tuple[int, bytes, bytes, int]],
    ) -> None:
        payload = bytearray(16 + len(definitions) * METADATA_ENTRY_SIZE)
        struct.pack_into("<8sII", payload, 0, METADATA_MAGIC, len(definitions), 0)
        for index, (name, definition) in enumerate(definitions.items()):
            original = bytes(definition[1])
            if not 5 <= len(original) <= 8:
                raise RuntimeError(f"青训 {name} 原始指令长度无法持久化")
            struct.pack_into(
                "<IIQ8sII", payload, 16 + index * METADATA_ENTRY_SIZE,
                HOOK_NAME_IDS[name], len(original), int(hooks[name]),
                original.ljust(8, b"\0"), int(definition[3]), 0,
            )
        write_process_memory(process, cave + METADATA_OFFSET, bytes(payload))
        if read_process_memory(
            process, cave + METADATA_OFFSET, len(payload),
        ) != bytes(payload):
            raise RuntimeError("青训 Hook 接管元数据写入校验失败")

    def _adopt_metadata_hook(
        self, pid: int, process: Any, module: Any, layout: Any,
    ) -> bool:
        module_base = int(module.base_address)
        lower = max(0, module_base - 0x7FFFFFFF)
        upper = module_base + int(module.size) + 0x7FFFFFFF
        caves: list[int] = []
        for region in iter_readable_regions(process):
            if (
                region.type == MEM_IMAGE
                or not region.protect & EXECUTE_MASK
                or region.base_address < lower
                or region.base_address >= upper
                or region.size < ALLOCATION_SIZE
                or region.size > 0x100000
            ):
                continue
            raw = read_process_memory(process, region.base_address, region.size) or b""
            position = raw.find(METADATA_MAGIC)
            while position >= 0:
                cave = region.base_address + position - METADATA_OFFSET
                if cave >= region.base_address:
                    caves.append(cave)
                position = raw.find(METADATA_MAGIC, position + 1)
        for cave in caves:
            header = read_process_memory(process, cave + METADATA_OFFSET, 16) or b""
            if len(header) != 16:
                continue
            magic, count, _reserved = struct.unpack("<8sII", header)
            if magic != METADATA_MAGIC or not 1 <= count <= len(HOOK_NAME_IDS):
                continue
            raw = read_process_memory(
                process, cave + METADATA_OFFSET + 16,
                count * METADATA_ENTRY_SIZE,
            ) or b""
            if len(raw) != count * METADATA_ENTRY_SIZE:
                continue
            hooks: dict[str, int] = {}
            originals: dict[str, bytes] = {}
            patches: dict[str, bytes] = {}
            definitions: dict[str, tuple[int, bytes, bytes, int]] = {}
            valid = True
            for index in range(count):
                name_id, original_len, hook, original_raw, code_offset, _ = (
                    struct.unpack_from("<IIQ8sII", raw, index * METADATA_ENTRY_SIZE)
                )
                name = HOOK_ID_NAMES.get(name_id)
                if (
                    not name or name in hooks or not 5 <= original_len <= 8
                    or not module_base <= hook < module_base + int(module.size)
                    or code_offset not in {
                        GATE_CODE_OFFSET, PA_CODE_OFFSET, STATE_CODE_OFFSET,
                        SON_CODE_OFFSET,
                    }
                ):
                    valid = False
                    break
                original = original_raw[:original_len]
                patch = b"\xE9" + _rel32(hook + 5, cave + code_offset)
                patch += b"\x90" * (original_len - 5)
                if read_process_memory(process, hook, original_len) != patch:
                    valid = False
                    break
                hooks[name] = hook
                originals[name] = original
                patches[name] = patch
                definitions[name] = (
                    hook - module_base, original, b"", code_offset,
                )
            if not valid or read_process_memory(
                process, cave + MARKER_OFFSET, len(MARKER),
            ) != MARKER:
                continue
            if any(
                read_process_memory(
                    process, cave + definition[3],
                    len(self._build_code(
                        layout, name, cave + definition[3], hooks[name], definition[1],
                    )),
                )
                != self._build_code(
                    layout, name, cave + definition[3], hooks[name], definition[1],
                )
                for name, definition in definitions.items()
            ):
                continue
            self.pid = pid
            self.cave = cave
            self.game_key = str(layout.key)
            self.hooks = hooks
            self.originals = originals
            self.patches = patches
            return True
        return False

    def _adopt_existing(self, pid: int, layout: Any) -> bool:
        if layout.key not in {"fm24", "fm26"}:
            return False
        with open_process(pid) as process:
            module = layout.module(process)
            if module is None:
                return False
            if self._adopt_metadata_hook(pid, process, module, layout):
                return True
        if (
            layout.youth_generation_gate_hook_rva is None
            or layout.youth_generation_pa_hook_rva is None
            or (layout.key == "fm24" and layout.youth_generation_state_hook_rva is None)
            or layout.youth_son_hook_rva is None
        ):
            return False
        possible = self._definitions(
            layout,
            quality=(
                layout.youth_generation_gate_hook_rva is not None
                and layout.youth_generation_pa_hook_rva is not None
                and (layout.key != "fm24" or layout.youth_generation_state_hook_rva is not None)
            ),
            son=layout.youth_son_hook_rva is not None,
        )
        with open_process(pid) as process:
            module = layout.module(process)
            if module is None:
                return False
            hooks: dict[str, int] = {}
            patches: dict[str, bytes] = {}
            targets: dict[str, int] = {}
            active_definitions: dict[str, tuple[int, bytes, bytes, int]] = {}
            for name, definition in possible.items():
                rva, original, _signature, offset = definition
                hook = module.base_address + rva
                patch = read_process_memory(process, hook, len(original))
                if patch == original:
                    continue
                if (
                    not patch or len(patch) != len(original)
                    or patch[0] != 0xE9
                    or patch[5:] != b"\x90" * (len(original) - 5)
                ):
                    return False
                hooks[name] = hook
                patches[name] = patch
                targets[name] = hook + 5 + struct.unpack_from("<i", patch, 1)[0]
                active_definitions[name] = definition
            if not active_definitions:
                return False
            first_name = next(iter(active_definitions))
            cave = targets[first_name] - active_definitions[first_name][3]
            if any(
                targets[name] != cave + active_definitions[name][3]
                for name in active_definitions
            ):
                return False
            if read_process_memory(
                process, cave + MARKER_OFFSET, len(MARKER),
            ) != MARKER:
                return False
            for name, definition in active_definitions.items():
                expected = self._build_code(
                    layout, name, cave + definition[3], hooks[name], definition[1],
                )
                if read_process_memory(
                    process, cave + definition[3], len(expected),
                ) != expected:
                    return False
        self.pid = pid
        self.cave = cave
        self.game_key = str(layout.key)
        self.hooks = hooks
        self.originals = {
            name: definition[1] for name, definition in active_definitions.items()
        }
        self.patches = patches
        return True

    def _write_targets(
        self, targets: list[dict[str, Any]], son_target: dict[str, Any] | None,
    ) -> None:
        with open_process(self.pid, write_memory=True) as process:
            old_remaining: dict[tuple[int, int, int, int], int] = {}
            raw = read_process_memory(
                process, self.cave + TABLE_OFFSET,
                TABLE_HEADER_SIZE + MAX_PLANS * ENTRY_SIZE,
            ) or b""
            if len(raw) >= TABLE_HEADER_SIZE:
                old_count = min(struct.unpack_from("<I", raw)[0], MAX_PLANS)
                for index in range(old_count):
                    offset = TABLE_HEADER_SIZE + index * ENTRY_SIZE
                    if offset + ENTRY_SIZE > len(raw):
                        break
                    _club, min_pa, max_pa, remaining, team_id, token = struct.unpack_from(
                        "<QHHIII", raw, offset,
                    )
                    old_remaining[(team_id, token, min_pa, max_pa)] = remaining
            payload = bytearray(TABLE_HEADER_SIZE + MAX_PLANS * ENTRY_SIZE)
            for index, target in enumerate(targets):
                key = (
                    int(target["team_id"]), int(target["token"]),
                    int(target["min_pa"]), int(target["max_pa"]),
                )
                remaining = min(
                    int(target["count"]),
                    old_remaining.get(key, int(target["count"])),
                )
                struct.pack_into(
                    "<QHHIII", payload,
                    TABLE_HEADER_SIZE + index * ENTRY_SIZE,
                    int(target["club_address"]), int(target["min_pa"]),
                    int(target["max_pa"]), remaining,
                    int(target["team_id"]), int(target["token"]),
                )
                target["remaining"] = remaining
            write_process_memory(
                process, self.cave + TABLE_OFFSET, struct.pack("<I", 0),
            )
            write_process_memory(process, self.cave + TABLE_OFFSET, bytes(payload))
            write_process_memory(
                process, self.cave + TABLE_OFFSET, struct.pack("<I", len(targets)),
            )
            struct.pack_into("<I", payload, 0, len(targets))
            check = read_process_memory(
                process, self.cave + TABLE_OFFSET,
                TABLE_HEADER_SIZE + len(targets) * ENTRY_SIZE,
            )
            if check != bytes(payload[:len(check or b"")]):
                raise RuntimeError("青训生成目标表写入校验失败")
            old_son = read_process_memory(
                process, self.cave + SON_TARGET_OFFSET, 32,
            ) or b""
            old_team_id = old_token = 0
            old_applied = 0
            old_pending = 0
            if len(old_son) == 32:
                _club, old_team_id, old_token, _old_slot, _ability, old_applied = (
                    struct.unpack("<QIIIIQ", old_son)
                )
                old_pending_raw = read_process_memory(
                    process, self.cave + SON_PENDING_OFFSET, 8,
                ) or b""
                if len(old_pending_raw) == 8:
                    old_pending = struct.unpack("<Q", old_pending_raw)[0]
            write_process_memory(
                process, self.cave + SON_TARGET_OFFSET, b"\0" * 8,
            )
            son_payload = bytearray(32)
            if son_target:
                same_plan = (
                    old_team_id == int(son_target["team_id"])
                    and old_token == int(son_target["token"])
                )
                applied = (
                    min(1, old_applied) if same_plan
                    else min(1, int(son_target.get("applied") or 0))
                )
                pending = min(1, old_pending) if same_plan else 0
                struct.pack_into(
                    "<QIIIIQ", son_payload, 0,
                    int(son_target["club_address"]), int(son_target["team_id"]),
                    int(son_target["token"]), int(son_target["slot"]),
                    (int(son_target["pa"]) << 16) | int(son_target["ca"]), applied,
                )
                son_target["remaining"] = 0 if applied else 1
                son_target["applied"] = applied
            write_process_memory(
                process, self.cave + SON_TARGET_OFFSET, bytes(son_payload),
            )
            if read_process_memory(
                process, self.cave + SON_TARGET_OFFSET, len(son_payload),
            ) != bytes(son_payload):
                raise RuntimeError("青训儿子目标写入校验失败")
            pending_payload = struct.pack("<Q", pending if son_target else 0)
            write_process_memory(
                process, self.cave + SON_PENDING_OFFSET, pending_payload,
            )
            if read_process_memory(
                process, self.cave + SON_PENDING_OFFSET, len(pending_payload),
            ) != pending_payload:
                raise RuntimeError("青训儿子一次性状态写入校验失败")
        self.targets = [dict(row) for row in targets]
        self.son_target = dict(son_target) if son_target else None

    def _uninstall(self) -> None:
        if not self.hooks:
            self._clear()
            return
        can_free = False
        try:
            with open_process(self.pid, write_memory=True) as process:
                restored = True
                for name in self.hooks:
                    current = read_process_memory(
                        process, self.hooks[name], len(self.patches[name]),
                    )
                    if current == self.patches[name]:
                        self._write_code(
                            process, self.hooks[name], self.originals[name],
                        )
                    elif current != self.originals[name]:
                        restored = False
                if restored:
                    deadline = time.monotonic() + 1.0
                    while time.monotonic() < deadline:
                        active = read_process_memory(
                            process, self.cave + RUNTIME_OFFSET, 8,
                        )
                        if active == b"\0" * 8:
                            can_free = True
                            break
                        time.sleep(0.01)
                if can_free and self.cave:
                    kernel32.VirtualFreeEx(
                        process.handle, ctypes.c_void_p(self.cave), 0, MEM_RELEASE,
                    )
        except Exception:
            pass
        self._clear()

    def _clear(self) -> None:
        self.pid = self.cave = 0
        self.game_key = ""
        self.hooks = {}
        self.originals = {}
        self.patches = {}
        self.targets = []
        self.son_target = None

    @staticmethod
    def _write_code(process: Any, address: int, data: bytes) -> None:
        old = wintypes.DWORD(0)
        if not kernel32.VirtualProtectEx(
            process.handle, ctypes.c_void_p(address), len(data),
            PAGE_EXECUTE_READWRITE, ctypes.byref(old),
        ):
            raise RuntimeError("无法修改青训生成代码页保护")
        try:
            write_process_memory(process, address, data)
            kernel32.FlushInstructionCache(
                process.handle, ctypes.c_void_p(address), len(data),
            )
            if read_process_memory(process, address, len(data)) != data:
                raise RuntimeError("青训生成代码写入校验失败")
        finally:
            restored = wintypes.DWORD(0)
            kernel32.VirtualProtectEx(
                process.handle, ctypes.c_void_p(address), len(data), old.value,
                ctypes.byref(restored),
            )


__all__ = [
    "ALLOCATION_SIZE", "ENTRY_SIZE", "FM24_SON_ORIGINAL",
    "FM24_SON_SIGNATURE", "FM26_CAPTURE_ORIGINAL", "FM26_CAPTURE_SIGNATURE",
    "FM26_PA_ORIGINAL", "FM26_PA_SIGNATURE", "FM26_SON_ORIGINAL",
    "FM26_SON_SIGNATURE", "GATE_ORIGINAL", "GATE_SIGNATURE", "MAX_PLANS",
    "PA_ORIGINAL", "PA_SIGNATURE", "SON_APPLIED_OFFSET", "SON_CODE_OFFSET",
    "SON_REMAINING_OFFSET", "SON_TARGET_OFFSET", "STATE_ORIGINAL",
    "STATE_SIGNATURE", "TABLE_OFFSET",
    "YouthGenerationHookController", "_build_gate_code", "_build_pa_code",
    "_build_fm26_capture_code", "_build_fm26_pa_code", "_build_son_code",
    "_build_state_code",
]
