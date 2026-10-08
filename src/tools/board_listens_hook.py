from __future__ import annotations

import ctypes
import struct
import threading
import time
from ctypes import wintypes
from typing import Any

from fm_collector.win32 import (
    MEM_IMAGE,
    PAGE_EXECUTE_READ,
    PAGE_EXECUTE_READWRITE,
    PAGE_EXECUTE_WRITECOPY,
    find_module,
    iter_readable_regions,
    kernel32,
    open_process,
    read_process_memory,
    write_process_memory,
)
from tools.game_layout import FM24_EXE_SHA256, FM26_EXE_SHA256
from tools.initial_data_audit import select_process_layout
from tools.redbull_hook import MEM_COMMIT_RESERVE, MEM_RELEASE, _Code, _matches, _rel32


# FM26 Super Nuclear Ver0.46. The three hooks operate on game_plugin.dll.
FM26_GENERAL_PATTERN = (
    0xB2, 0x0F, 0xE8, None, None, None, None, 0x48, 0x85, 0xC0, 0x0F, 0x84,
)
FM26_CLUB_PATTERN = (
    0x45, None, None, 0xE8, None, None, None, None,
    0x4C, 0x8B, None, None, None, 0x00, 0x00, 0x48, 0x85, 0xC0, 0x4C, 0x8B,
)
FM26_VISION_PATTERN = (
    0x0F, 0xBF, None, 0x62, 0x0F, 0xB6, None, 0x6B, 0x01, None,
    0x48, 0x89, None, None, None, 0x00, 0x00, 0x78,
)
FM26_GENERAL_HOOK_OFFSET = 0x02
FM26_CLUB_HOOK_OFFSET = 0x08
FM26_VISION_PATCH_OFFSET = 0x11
FM26_GENERAL_ORIGINAL = b"\xE8\xB8\x86\xF1\x00"
FM26_CLUB_ORIGINAL = b"\x4C\x8B\x8D\x90\x00\x00\x00"
FM26_VISION_ORIGINAL = b"\x78\x7C"
FM26_VERIFIED_HOOK_RVAS = (0x1313443, 0x1321BDA, 0x1AB1C56)
FM26_VERIFIED_ORIGINALS = (
    FM26_GENERAL_ORIGINAL,
    FM26_CLUB_ORIGINAL,
    FM26_VISION_ORIGINAL,
)

# FM2024 Ver1.2 by fm-san. These patterns were extracted from the unpacked
# Trainer Lua bytecode. The FM24 code compares a state byte and forces the
# comparison result; it does not use the FM26 return-object convention.
FM24_GENERAL_PATTERN = (
    0x41, 0x80, 0xBF, 0x83, 0x00, 0x00, 0x00, 0x01,
    None, 0x89, None, None, None, 0x00, 0x00,
)
FM24_CLUB_ONE_PATTERN = (
    0x0F, 0x82, None, None, None, None,
    0x41, 0x80, 0xBF, None, None, 0x00, 0x00, 0x01, 0x0F, 0x85,
)
FM24_CLUB_TWO_PATTERN = (
    0x41, 0x80, 0xBF, None, None, 0x00, 0x00, 0x00,
    0x4C, 0x89, 0xF9,
)
FM24_VISION_PATTERN = (
    0x0F, 0xBF, None, 0x62, 0x0F, 0xB6, None, 0x6B, 0x01,
)
FM24_GENERAL_HOOK_OFFSET = 0
FM24_CLUB_ONE_HOOK_OFFSET = 6
FM24_CLUB_TWO_HOOK_OFFSET = 0
FM24_VISION_PATCH_OFFSET = 0x0A
FM24_VISION_PATCH_SIZE = 2
FM24_VISION_PATCH = b"\x90\x90"
FM24_VERIFIED_ORIGINALS = (
    b"\x41\x80\xBF\x83\x00\x00\x00\x01",
    b"\x41\x80\xBF\x83\x00\x00\x00\x01",
    b"\x41\x80\xBF\x8A\x00\x00\x00\x00",
    b"\x78\x73",
)
FM24_VERIFIED_HOOK_RVAS = (0x339CF47, 0x33A6F30, 0x33A87B9, 0x2BB637E)

CLUB_CAVE_OFFSET = 0x100
EXECUTE_MASK = PAGE_EXECUTE_READ | PAGE_EXECUTE_READWRITE | PAGE_EXECUTE_WRITECOPY


def _scan_module_patterns(
    process: Any, module: Any, patterns: dict[str, tuple[int | None, ...]],
) -> dict[str, list[int]]:
    try:
        image = read_process_memory(process, module.base_address, module.size)
    except OSError:
        image = b""
    if image and len(image) == int(module.size):
        return {name: _matches(image, pattern) for name, pattern in patterns.items()}

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
        size = min(int(region.size), module_end - int(region.base_address))
        data = read_process_memory(process, region.base_address, size)
        if not data:
            continue
        for name, pattern in patterns.items():
            matches[name].extend(
                int(region.base_address) + offset - int(module.base_address)
                for offset in _matches(data, pattern)
            )
    return matches


def _recover_exact_fm24_stale_patches(
    process: Any, module: Any, write_code: Any,
) -> None:
    if not module:
        return
    cave_targets: list[int] = []
    for rva, original in zip(FM24_VERIFIED_HOOK_RVAS, FM24_VERIFIED_ORIGINALS):
        address = int(module.base_address) + rva
        size = len(original)
        current = read_process_memory(process, address, size)
        if size == 2:
            if current == FM24_VISION_PATCH:
                write_code(process, address, original)
            continue
        expected_jump = current[:1] == b"\xE9" and (
            current[5:] == b"\x90" * (size - 5)
        )
        if not expected_jump:
            continue
        cave_targets.append(address + 5 + struct.unpack_from("<i", current, 1)[0])
        write_code(process, address, original)

    if len(cave_targets) == 3:
        cave = cave_targets[0]
        if cave_targets == [cave, cave + 0x100, cave + 0x200]:
            kernel32.VirtualFreeEx(
                process.handle, ctypes.c_void_p(cave), 0, MEM_RELEASE,
            )


def _recover_exact_fm26_stale_patches(
    process: Any, module: Any, write_code: Any,
) -> None:
    """Restore an orphaned FMODD hook from the verified FM26 Steam build."""
    addresses = [
        int(module.base_address) + rva for rva in FM26_VERIFIED_HOOK_RVAS
    ]
    current = [
        read_process_memory(process, address, len(original))
        for address, original in zip(addresses, FM26_VERIFIED_ORIGINALS)
    ]
    if current == list(FM26_VERIFIED_ORIGINALS):
        return
    if not (
        len(current[0]) == 5
        and current[0][:1] == b"\xE9"
        and len(current[1]) == 7
        and current[1][:1] == b"\xE9"
        and current[1][5:] == b"\x90\x90"
        and current[2] == FM24_VISION_PATCH
    ):
        return
    general_cave = addresses[0] + 5 + struct.unpack_from("<i", current[0], 1)[0]
    club_cave = addresses[1] + 5 + struct.unpack_from("<i", current[1], 1)[0]
    if club_cave != general_cave + CLUB_CAVE_OFFSET:
        return

    restored: list[tuple[int, bytes]] = []
    try:
        for address, original, patch in zip(
            addresses, FM26_VERIFIED_ORIGINALS, current,
        ):
            write_code(process, address, original)
            restored.append((address, patch))
            if read_process_memory(process, address, len(original)) != original:
                raise RuntimeError("FM26 stale board hook recovery verification failed")
    except Exception:
        for address, patch in reversed(restored):
            try:
                write_code(process, address, patch)
            except Exception:
                pass
        raise
    time.sleep(0.1)
    kernel32.VirtualFreeEx(
        process.handle, ctypes.c_void_p(general_cave), 0, MEM_RELEASE,
    )


def _build_fm26_general_code(
    cave: int, original_call: bytes, hook: int, return_address: int,
) -> bytes:
    if len(original_call) != 5 or original_call[0] != 0xE8:
        raise RuntimeError("FM26 board general CALL is invalid")
    call_target = hook + 5 + struct.unpack_from("<i", original_call, 1)[0]
    code = _Code(cave)
    code.emit(b"\xE8" + _rel32(cave + 5, call_target))
    code.emit(b"\x48\x85\xC0")
    code.jump32(b"\x0F\x84", "exit")
    code.emit(b"\xC6\x80\x83\x00\x00\x00\x01")
    code.label("exit")
    code.emit(b"\xE9" + _rel32(cave + len(code.data) + 5, return_address))
    return code.finish()


def _build_fm26_club_code(cave: int, original: bytes, return_address: int) -> bytes:
    if len(original) != 7:
        raise RuntimeError("FM26 board club instruction is invalid")
    code = _Code(cave)
    code.emit(original)
    code.emit(b"\x80\xB8\x83\x00\x00\x00\x03")
    code.jump32(b"\x0F\x85", "exit")
    code.emit(b"\xC6\x80\x83\x00\x00\x00\x01")
    code.emit(b"\xC6\x80\x8A\x00\x00\x00\x01")
    code.label("exit")
    code.emit(b"\xE9" + _rel32(cave + len(code.data) + 5, return_address))
    return code.finish()


def _build_fm24_force_state_code(
    cave: int, original: bytes, return_address: int,
) -> bytes:
    if len(original) != 8 or original[:3] != b"\x41\x80\xBF":
        raise RuntimeError("FM24 board state comparison is invalid")
    code = _Code(cave)
    # cmp byte ptr [r15+disp32], imm8 -> mov byte ptr [r15+disp32], 1
    code.emit(b"\x41\xC6\x87" + original[3:7] + b"\x01")
    code.emit(b"\x39\xC0")  # cmp eax,eax: force the following condition true
    code.emit(b"\xE9" + _rel32(cave + len(code.data) + 5, return_address))
    return code.finish()


def _build_fm24_force_club_two_code(
    cave: int, original: bytes, return_address: int,
) -> bytes:
    # The Trainer writes 1, then executes the displaced instruction sequence.
    # Rebuild this as one cave so the original post-comparison instructions
    # keep their original flags and register effects.
    code_builder = _Code(cave)
    code_builder.emit(b"\x41\xC6\x87" + original[3:7] + b"\x01")
    code_builder.emit(original)
    code_builder.emit(b"\xE9" + _rel32(cave + len(code_builder.data) + 5, return_address))
    return code_builder.finish()


# Compatibility aliases for the first FM26 implementation and its tests.
GENERAL_PATTERN = FM26_GENERAL_PATTERN
CLUB_PATTERN = FM26_CLUB_PATTERN
VISION_PATTERN = FM26_VISION_PATTERN
GENERAL_HOOK_OFFSET = FM26_GENERAL_HOOK_OFFSET
CLUB_HOOK_OFFSET = FM26_CLUB_HOOK_OFFSET
VISION_PATCH_OFFSET = FM26_VISION_PATCH_OFFSET
GENERAL_ORIGINAL_SIZE = len(FM26_GENERAL_ORIGINAL)
CLUB_ORIGINAL_SIZE = len(FM26_CLUB_ORIGINAL)
GENERAL_ORIGINAL = FM26_GENERAL_ORIGINAL
CLUB_ORIGINAL = FM26_CLUB_ORIGINAL
VISION_ORIGINAL = FM26_VISION_ORIGINAL
VISION_PATCH = FM24_VISION_PATCH
_build_general_code = _build_fm26_general_code
_build_club_code = _build_fm26_club_code


class BoardListensHookController:
    """Install the matching FM24 or FM26 Trainer-style board hooks."""

    FAILURE_RETRY_SECONDS = 300.0

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.pid = 0
        self.cave = 0
        self.patches: list[tuple[int, bytes, bytes]] = []
        self.error: str | None = None
        self.game_key: str | None = None
        self.distribution: str | None = None
        self._failed_identity: tuple[int, str, str, str, str] | None = None
        self._failed_at = 0.0

    @staticmethod
    def _layout_identity(pid: int, layout: Any) -> tuple[int, str, str, str, str]:
        return (
            int(pid),
            str(getattr(layout, "key", "")),
            str(getattr(layout, "distribution", "")),
            str(getattr(layout, "module_name", "")),
            str(getattr(layout, "executable_sha256", "")).upper(),
        )

    def _clear_failed_identity(self) -> None:
        self._failed_identity = None
        self._failed_at = 0.0

    def _retry_after_seconds(self) -> float:
        if self._failed_identity is None:
            return 0.0
        return max(
            0.0,
            self.FAILURE_RETRY_SECONDS - (time.monotonic() - self._failed_at),
        )

    def status(self) -> dict[str, Any]:
        with self.lock:
            retry_after = self._retry_after_seconds()
            return {
                "supported": True,
                "active": bool(self.patches),
                "pid": self.pid or None,
                "game_key": self.game_key,
                "distribution": self.distribution,
                "error": self.error,
                "retry_suppressed": retry_after > 0.0,
                "retry_after_seconds": round(retry_after, 1),
            }

    def sync(self, enabled: bool) -> dict[str, Any]:
        with self.lock:
            if not enabled:
                self._disable()
                self._clear_failed_identity()
                self.error = None
                return self.status()
            identity: tuple[int, str, str, str, str] | None = None
            try:
                pid, _path, layout = select_process_layout()
                identity = self._layout_identity(pid, layout)
                if self.patches and (
                    pid != self.pid
                    or str(getattr(layout, "key", "")) != self.game_key
                ):
                    self._disable()
                if not self.patches:
                    if self._failed_identity != identity:
                        self._clear_failed_identity()
                    elif self._retry_after_seconds() > 0.0:
                        return self.status()
                    self._enable(pid, layout)
                    self._clear_failed_identity()
                self.error = None
            except Exception as error:
                self.error = str(error)
                if identity is not None:
                    self._failed_identity = identity
                    self._failed_at = time.monotonic()
            return self.status()

    def close(self) -> None:
        with self.lock:
            self._disable()

    @staticmethod
    def _allocate_near(process: Any, hook: int) -> int:
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
                    kernel32.VirtualFreeEx(
                        process.handle, ctypes.c_void_p(address), 0, MEM_RELEASE,
                    )
        raise RuntimeError("cannot allocate code cave near board hook")

    @staticmethod
    def _vision_original(data: bytes) -> bool:
        # Both Trainer generations patch a short conditional jump. Requiring
        # a Jcc opcode prevents a coincidental data match from being patched.
        return len(data) == 2 and 0x70 <= data[0] <= 0x7F

    def _enable(self, pid: int, layout: Any) -> None:
        if str(getattr(layout, "key", "")) not in {"fm24", "fm26"}:
            raise RuntimeError("current game generation is not supported")
        with open_process(pid, write_memory=True) as process:
            module_name = str(getattr(
                layout, "module_name",
                "game_plugin.dll" if layout.key == "fm26" else "fm.exe",
            ))
            module = find_module(process, module_name)
            if not module:
                raise RuntimeError(f"{module_name} is not loaded")
            if (
                layout.key == "fm24"
                and str(getattr(layout, "executable_sha256", "")).upper() == FM24_EXE_SHA256
            ):
                _recover_exact_fm24_stale_patches(process, module, self._write_code)
            if (
                layout.key == "fm26"
                and str(getattr(layout, "executable_sha256", "")).upper() == FM26_EXE_SHA256
            ):
                _recover_exact_fm26_stale_patches(process, module, self._write_code)
            if layout.key == "fm24":
                resolved = self._resolve_fm24(process, module, layout)
            else:
                resolved = self._resolve_fm26(process, module, layout)

            first_hook = resolved[0][0]
            cave = self._allocate_near(process, first_hook)
            club_cave = cave + CLUB_CAVE_OFFSET
            if layout.key == "fm24":
                general_code = _build_fm24_force_state_code(
                    cave, resolved[0][1], resolved[0][0] + 8,
                )
                club_one_code = _build_fm24_force_state_code(
                    club_cave, resolved[1][1], resolved[1][0] + 8,
                )
                club_two_code = _build_fm24_force_club_two_code(
                    cave + 0x200, resolved[2][1], resolved[2][0] + 8,
                )
                if max(len(general_code), len(club_one_code), len(club_two_code)) >= 0x100:
                    kernel32.VirtualFreeEx(process.handle, ctypes.c_void_p(cave), 0, MEM_RELEASE)
                    raise RuntimeError("FM24 board code cave layout overflow")
                write_process_memory(process, cave, general_code)
                write_process_memory(process, club_cave, club_one_code)
                write_process_memory(process, cave + 0x200, club_two_code)
                resolved = [
                    *resolved[:3],
                    (resolved[3][0], resolved[3][1], FM24_VISION_PATCH),
                ]
                patches = [
                    (resolved[0][0], resolved[0][1], b"\xE9" + _rel32(resolved[0][0] + 5, cave) + b"\x90" * 3),
                    (resolved[1][0], resolved[1][1], b"\xE9" + _rel32(resolved[1][0] + 5, club_cave) + b"\x90" * 3),
                    (resolved[2][0], resolved[2][1], b"\xE9" + _rel32(resolved[2][0] + 5, cave + 0x200) + b"\x90" * 3),
                    resolved[3],
                ]
            else:
                general_code = _build_fm26_general_code(
                    cave, resolved[0][1], resolved[0][0], resolved[0][0] + 5,
                )
                club_code = _build_fm26_club_code(
                    club_cave, resolved[1][1], resolved[1][0] + 7,
                )
                if len(general_code) >= CLUB_CAVE_OFFSET or len(club_code) >= 0x100:
                    kernel32.VirtualFreeEx(process.handle, ctypes.c_void_p(cave), 0, MEM_RELEASE)
                    raise RuntimeError("FM26 board code cave layout overflow")
                write_process_memory(process, cave, general_code)
                write_process_memory(process, club_cave, club_code)
                patches = [
                    (resolved[0][0], resolved[0][1], b"\xE9" + _rel32(resolved[0][0] + 5, cave)),
                    (resolved[1][0], resolved[1][1], b"\xE9" + _rel32(resolved[1][0] + 5, club_cave) + b"\x90" * 2),
                    (resolved[2][0], resolved[2][1], FM24_VISION_PATCH),
                ]

            applied: list[tuple[int, bytes, bytes]] = []
            try:
                for address, original, patch in patches:
                    self._write_code(process, address, patch)
                    applied.append((address, original, patch))
                    if read_process_memory(process, address, len(patch)) != patch:
                        raise RuntimeError("board hook write-back verification failed")
            except Exception:
                for address, original, patch in reversed(applied):
                    if read_process_memory(process, address, len(patch)) == patch:
                        self._write_code(process, address, original)
                kernel32.VirtualFreeEx(process.handle, ctypes.c_void_p(cave), 0, MEM_RELEASE)
                raise
            self.pid = pid
            self.cave = cave
            self.game_key = str(layout.key)
            self.distribution = str(getattr(layout, "distribution", ""))
            self.patches = patches

    @staticmethod
    def _resolve_fm26(
        process: Any, module: Any, layout: Any,
    ) -> list[tuple[int, bytes, bytes]]:
        exact_layout = (
            str(getattr(layout, "executable_sha256", "")).upper()
            == FM26_EXE_SHA256
        )
        if exact_layout:
            resolved = []
            for rva, expected in zip(
                FM26_VERIFIED_HOOK_RVAS, FM26_VERIFIED_ORIGINALS,
            ):
                address = int(module.base_address) + rva
                original = read_process_memory(process, address, len(expected))
                if original != expected:
                    raise RuntimeError("FM26 board fixed hook bytes do not match")
                resolved.append((address, original, b""))
            return resolved

        patterns = (
            (FM26_GENERAL_PATTERN, FM26_GENERAL_HOOK_OFFSET, 5, FM26_GENERAL_ORIGINAL),
            (FM26_CLUB_PATTERN, FM26_CLUB_HOOK_OFFSET, 7, FM26_CLUB_ORIGINAL),
            (FM26_VISION_PATTERN, FM26_VISION_PATCH_OFFSET, 2, FM26_VISION_ORIGINAL),
        )
        scanned = _scan_module_patterns(
            process, module, {str(index): item[0] for index, item in enumerate(patterns)},
        )
        hits = [scanned[str(index)] for index in range(len(patterns))]
        if any(len(rows) != 1 for rows in hits):
            raise RuntimeError("FM26 board signatures are not unique")
        resolved: list[tuple[int, bytes, bytes]] = []
        for index, (rows, (_pattern, offset, size, expected)) in enumerate(zip(hits, patterns)):
            address = module.base_address + rows[0] + offset
            original = read_process_memory(process, address, size)
            structural_match = (
                (index == 0 and len(original) == 5 and original[0] == 0xE8)
                or (index == 1 and len(original) == 7 and original[:2] == b"\x4C\x8B")
                or (index == 2 and BoardListensHookController._vision_original(original))
            )
            if not structural_match:
                raise RuntimeError("FM26 board original bytes do not match")
            resolved.append((address, original, b""))
        return resolved

    @staticmethod
    def _resolve_fm24(
        process: Any, module: Any, layout: Any,
    ) -> list[tuple[int, bytes, bytes]]:
        exact_layout = (
            str(getattr(layout, "executable_sha256", "")).upper()
            == FM24_EXE_SHA256
        )
        if exact_layout:
            resolved = []
            for rva, expected in zip(
                FM24_VERIFIED_HOOK_RVAS, FM24_VERIFIED_ORIGINALS,
            ):
                address = module.base_address + rva
                original = read_process_memory(process, address, len(expected))
                if original != expected:
                    raise RuntimeError(
                        "FM24 board fixed hook bytes do not match"
                    )
                resolved.append((address, original, b""))
            return resolved

        patterns = (
            (FM24_GENERAL_PATTERN, FM24_GENERAL_HOOK_OFFSET, 8),
            (FM24_CLUB_ONE_PATTERN, FM24_CLUB_ONE_HOOK_OFFSET, 8),
            (FM24_CLUB_TWO_PATTERN, FM24_CLUB_TWO_HOOK_OFFSET, 8),
            (FM24_VISION_PATTERN, FM24_VISION_PATCH_OFFSET, FM24_VISION_PATCH_SIZE),
        )
        scanned = _scan_module_patterns(
            process, module, {str(index): item[0] for index, item in enumerate(patterns)},
        )
        hits = [scanned[str(index)] for index in range(len(patterns))]
        if any(len(rows) != 1 for rows in hits):
            raise RuntimeError("FM24 board signatures are not unique")
        resolved: list[tuple[int, bytes, bytes]] = []
        for index, (rows, (_pattern, offset, size)) in enumerate(zip(hits, patterns)):
            address = module.base_address + rows[0] + offset
            original = read_process_memory(process, address, size)
            if index < 3 and (
                len(original) != 8 or original[:3] != b"\x41\x80\xBF"
            ):
                raise RuntimeError("FM24 board comparison bytes do not match")
            if index == 3 and not BoardListensHookController._vision_original(original):
                raise RuntimeError("FM24 board vision branch is not a short Jcc")
            resolved.append((address, original, b""))
        return resolved

    def _disable(self) -> None:
        if not self.patches:
            return
        patches, pid, cave = self.patches, self.pid, self.cave
        self.patches = []
        self.pid = 0
        self.cave = 0
        self.game_key = None
        self.distribution = None
        try:
            with open_process(pid, write_memory=True) as process:
                cave_is_referenced = False
                for index, (address, original, patch) in enumerate(reversed(patches)):
                    current = read_process_memory(process, address, len(patch))
                    if current == patch:
                        self._write_code(process, address, original)
                    elif index > 0 and current != original:
                        cave_is_referenced = True
                if not cave_is_referenced:
                    time.sleep(0.1)
                    kernel32.VirtualFreeEx(process.handle, ctypes.c_void_p(cave), 0, MEM_RELEASE)
        except Exception:
            pass

    @staticmethod
    def _write_code(process: Any, address: int, data: bytes) -> None:
        old = wintypes.DWORD(0)
        if not kernel32.VirtualProtectEx(
            process.handle, ctypes.c_void_p(address), len(data),
            PAGE_EXECUTE_READWRITE, ctypes.byref(old),
        ):
            raise RuntimeError("cannot change board code page protection")
        try:
            write_process_memory(process, address, data)
            kernel32.FlushInstructionCache(process.handle, ctypes.c_void_p(address), len(data))
        finally:
            restored = wintypes.DWORD(0)
            kernel32.VirtualProtectEx(
                process.handle, ctypes.c_void_p(address), len(data), old.value,
                ctypes.byref(restored),
            )
