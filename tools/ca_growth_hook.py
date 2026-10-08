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
from tools.redbull_hook import (
    MEM_COMMIT_RESERVE, MEM_RELEASE, _Code, _matches, _rel32,
)


HOOK_ORIGINAL = bytes.fromhex("41 0F B7 85 64 02 00 00")
FM24_ORIGINAL = bytes.fromhex("41 0F B7 85 00 02 00 00")
CODE_MARKER = b"FMODDCG3"
CODE_PREFIX = b"\xEB" + bytes([len(CODE_MARKER)]) + CODE_MARKER
LEGACY_CODE_PREFIX = b"\xEB\x08FMODDCG2"
MULTIPLIER = 2
MAX_PLAUSIBLE_CA_DELTA = 200
MAX_PLAYER_ARRAY_INDEX = 0x500
SUPPORTED_MULTIPLIERS = (1.5, 2, 3)
TELEMETRY_OFFSET = 0x800
TELEMETRY_STRUCT = struct.Struct("<QQQQhHBB2xQhhHBB")
TELEMETRY_STAGES = {
    0: "entered",
    1: "player_pointer_plausible",
    2: "player_uid_plausible",
    3: "indexed_array_present",
    4: "index_in_range",
    5: "team_slot_present",
    6: "team_resolved",
    7: "national_team_resolved",
    8: "club_team_matched",
    9: "national_team_matched",
}
FM26_SIGNATURE = bytes.fromhex("41 0F B7 85 64 02 00 00 66 01 F8")
FM24_SIGNATURE = bytes.fromhex("41 0F B7 85 00 02 00 00 66 44 01 F0")


class CAGrowthHookError(RuntimeError):
    def __init__(
        self, message: str, *, code: str, retryable: bool = False,
        detail: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.detail = detail


def _normalized_multiplier(value: float | int) -> float | int:
    try:
        numeric = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError("食堂成长倍率无效") from error
    for supported in SUPPORTED_MULTIPLIERS:
        if abs(numeric - float(supported)) < 0.0001:
            return supported
    raise ValueError("食堂成长倍率仅支持 1.5x、2x 或 3x")


def _zero_increment(multiplier: float | int) -> int:
    return 1 if multiplier == 1.5 else int(multiplier) - 1


def _expected_applied_delta(
    raw_delta: int, multiplier: float | int,
) -> int:
    multiplier = _normalized_multiplier(multiplier)
    if not -MAX_PLAUSIBLE_CA_DELTA <= raw_delta <= MAX_PLAUSIBLE_CA_DELTA:
        return raw_delta
    if raw_delta == 0:
        return _zero_increment(multiplier)
    magnitude = abs(raw_delta)
    if multiplier == 1.5:
        return (magnitude * 3 + 1) // 2
    return magnitude * int(multiplier)


def _decode_telemetry(
    data: bytes | None, multiplier: float | int,
) -> dict[str, Any]:
    empty = {
        "available": True,
        "observed": False,
        "hit_count": 0,
        "instruction_hit_count": 0,
        "last_filter_player_address": None,
        "last_filter_raw_delta": None,
        "last_filter_ca_before": None,
        "player_address": None,
        "resolved_team_address": None,
        "filter_stage": None,
        "filter_stage_name": None,
        "target_team_matched": False,
        "matched_scope": None,
        "matched_team_address": None,
        "raw_delta": None,
        "applied_delta": None,
        "ca_before": None,
        "predicted_ca_after": None,
        "within_growth_range": None,
        "transformation_verified": None,
    }
    if not data or len(data) != TELEMETRY_STRUCT.size:
        return {**empty, "available": False}
    (
        instruction_hit_count, hit_count, player_address,
        resolved_team_address, last_raw_delta, last_ca_before,
        filter_stage, _reserved, matched_player_address,
        raw_delta, applied_delta, ca_before, matched_scope_code, transformed,
    ) = TELEMETRY_STRUCT.unpack(data)
    if not instruction_hit_count:
        return empty
    matched = bool(hit_count)
    if not matched:
        return {
            **empty,
            "instruction_hit_count": int(instruction_hit_count),
            "last_filter_player_address": (
                hex(player_address) if player_address else None
            ),
            "last_filter_raw_delta": int(last_raw_delta),
            "last_filter_ca_before": int(last_ca_before),
            "resolved_team_address": (
                hex(resolved_team_address) if resolved_team_address else None
            ),
            "filter_stage": int(filter_stage),
            "filter_stage_name": TELEMETRY_STAGES.get(
                int(filter_stage), "unknown",
            ),
        }
    expected = _expected_applied_delta(raw_delta, multiplier)
    matched_scope = (
        "club" if int(matched_scope_code) == 1
        else "national" if int(matched_scope_code) == 2
        else None
    )
    return {
        **empty,
        "observed": True,
        "hit_count": int(hit_count),
        "instruction_hit_count": int(instruction_hit_count),
        "last_filter_player_address": (
            hex(player_address) if player_address else None
        ),
        "last_filter_raw_delta": int(last_raw_delta),
        "last_filter_ca_before": int(last_ca_before),
        "player_address": (
            hex(matched_player_address) if matched_player_address else None
        ),
        "resolved_team_address": (
            hex(resolved_team_address) if resolved_team_address else None
        ),
        "filter_stage": int(filter_stage),
        "filter_stage_name": TELEMETRY_STAGES.get(int(filter_stage), "unknown"),
        "target_team_matched": True,
        "matched_scope": matched_scope,
        "raw_delta": int(raw_delta),
        "applied_delta": int(applied_delta),
        "ca_before": int(ca_before),
        "predicted_ca_after": int((ca_before + applied_delta) & 0xFFFF),
        "within_growth_range": bool(transformed),
        "transformation_verified": int(applied_delta) == expected,
    }


def _friendly_error(error: Exception) -> CAGrowthHookError:
    if isinstance(error, CAGrowthHookError):
        return error
    message = str(error).strip()
    lowered = message.casefold()
    if isinstance(error, OSError) or any(
        token in lowered for token in ("access", "permission", "拒绝访问")
    ):
        return CAGrowthHookError(
            "无法连接游戏的成长功能，请确认 FMODD 与游戏权限一致后重试",
            code="native_access", retryable=True, detail=message,
        )
    return CAGrowthHookError(
        "食堂成长加速启动失败，请关闭其他修改工具并刷新后重试",
        code="hook_start_failed", retryable=True, detail=message,
    )


def _build_code(
    cave: int, original: bytes, return_address: int, team_address: int,
    *, multiplier: float | int = MULTIPLIER, marker: bytes = CODE_PREFIX,
    zero_increment: int | None = None,
    bounded_delta: bool = True,
    telemetry: bool = True,
    national_team_address: int = 0,
    dual_scope: bool = True,
    strict_pointer_checks: bool = True,
) -> bytes:
    multiplier = _normalized_multiplier(multiplier)
    zero_increment = _zero_increment(multiplier) if zero_increment is None else zero_increment
    code = _Code(cave)
    code.emit(marker)
    code.emit(original)
    if telemetry:
        telemetry_address = cave + TELEMETRY_OFFSET
        code.emit(b"\x9C\x50\x41\x50\x41\x51")  # flags, rax, r8, r9
        code.emit(b"\x49\xB9" + struct.pack("<Q", telemetry_address))
        code.emit(b"\x4D\x89\x69\x10")  # last player address
        code.emit(b"\x66\x41\x89\x79\x20")  # latest raw signed delta
        code.emit(b"\x48\x8B\x44\x24\x10")  # original EAX / CA before
        code.emit(b"\x66\x41\x89\x41\x22")
        code.emit(b"\x41\xC6\x41\x24\x00")  # entered
        code.emit(b"\x49\xC7\x41\x18\x00\x00\x00\x00")
    else:
        code.emit(b"\x9C\x50\x41\x50")  # pushfq; push rax; push r8
    code.emit(b"\x49\x81\xFD\xFF\xFF\x0F\x00")
    code.jump32(b"\x0F\x86", "exit")
    if telemetry:
        code.emit(b"\x41\xC6\x41\x24\x01")
    code.emit(b"\x66\x41\x81\x7D\x0C\x00\x7F")
    code.jump32(b"\x0F\x86", "exit")
    code.emit(b"\x66\x41\x81\x7D\x0C\xFF\x7F")
    code.jump32(b"\x0F\x87", "exit")
    if telemetry:
        code.emit(b"\x41\xC6\x41\x24\x02")
    code.emit(b"\x4D\x8B\x45\x08\x4D\x85\xC0")
    code.jump32(b"\x0F\x84", "exit")
    if telemetry:
        code.emit(b"\x41\xC6\x41\x24\x03")
    code.emit(
        b"\x45\x8B\x40\x04\x41\x81\xF8"
        + struct.pack("<I", MAX_PLAYER_ARRAY_INDEX)
    )
    code.jump32(b"\x0F\x87", "exit")
    if telemetry:
        code.emit(b"\x41\xC6\x41\x24\x04")
    code.emit(b"\x4D\x63\xC0\x4F\x8B\x84\x05\xB0\x00\x00\x00")
    if strict_pointer_checks:
        code.emit(b"\x49\x81\xF8\xFF\xFF\x0F\x00")
        code.jump32(b"\x0F\x86", "exit")
    else:
        code.emit(b"\x4D\x85\xC0")
        code.jump32(b"\x0F\x84", "exit")
    if telemetry:
        code.emit(b"\x41\xC6\x41\x24\x05")
    code.emit(b"\x4D\x8B\x40\x10")
    if strict_pointer_checks:
        code.emit(b"\x49\x81\xF8\xFF\xFF\x0F\x00")
        code.jump32(b"\x0F\x86", "exit")
    if telemetry:
        code.emit(b"\x4D\x89\x41\x18")  # resolved team address
        code.emit(b"\x41\xC6\x41\x24\x06")
    code.emit(b"\x48\xB8" + struct.pack("<Q", team_address) + b"\x49\x39\xC0")
    if dual_scope:
        code.jump32(b"\x0F\x84", "club_match")
        code.emit(b"\x4D\x8B\x85\x28\x01\x00\x00")
        if telemetry:
            code.emit(b"\x4D\x89\x41\x18")
            code.emit(b"\x41\xC6\x41\x24\x07")
        code.emit(b"\x48\xB8" + struct.pack("<Q", national_team_address))
        code.emit(b"\x48\x85\xC0")
        code.jump32(b"\x0F\x84", "exit")
        code.emit(b"\x49\x39\xC0")
        code.jump32(b"\x0F\x85", "exit")
        if telemetry:
            code.emit(b"\x41\xC6\x41\x24\x09")
            code.emit(b"\x41\xC6\x41\x36\x02")
        code.jump32(b"\xE9", "apply")
        code.label("club_match")
        if telemetry:
            code.emit(b"\x41\xC6\x41\x24\x08")
            code.emit(b"\x41\xC6\x41\x36\x01")
        code.label("apply")
    else:
        code.jump32(b"\x0F\x85", "exit")
        if telemetry:
            code.emit(b"\x41\xC6\x41\x24\x08")
            code.emit(b"\x41\xC6\x41\x36\x01")
    if telemetry:
        code.emit(b"\x4D\x89\x69\x28")  # last matching player
        code.emit(b"\x66\x41\x89\x79\x30")  # matching raw delta
        code.emit(b"\x66\x41\x89\x79\x32")  # defaults to unchanged
        code.emit(b"\x48\x8B\x44\x24\x10")
        code.emit(b"\x66\x41\x89\x41\x34")  # matching CA before
        code.emit(b"\x41\xC6\x41\x37\x00")  # not transformed
    if bounded_delta:
        code.emit(b"\x66\x81\xFF" + struct.pack("<H", MAX_PLAUSIBLE_CA_DELTA))
        code.jump32(b"\x0F\x8F", "commit" if telemetry else "exit")
        code.emit(b"\x66\x81\xFF" + struct.pack("<h", -MAX_PLAUSIBLE_CA_DELTA))
        code.jump32(b"\x0F\x8C", "commit" if telemetry else "exit")
    code.emit(b"\x66\x85\xFF")
    code.jump32(b"\x0F\x84", "zero")
    code.jump32(b"\x0F\x8F", "positive")
    code.emit(b"\x66\xF7\xDF")
    code.label("positive")
    if multiplier == 1.5:
        code.emit(b"\x66\x6B\xFF\x03\x66\xFF\xC7\x66\xD1\xEF")
    else:
        code.emit(b"\x66\x6B\xFF" + bytes([int(multiplier)]))
    code.jump32(b"\xE9", "record" if telemetry else "exit")
    code.label("zero")
    code.emit(b"\x66\xBF" + struct.pack("<H", zero_increment))
    if telemetry:
        code.label("record")
        code.emit(b"\x66\x41\x89\x79\x32")  # applied signed delta
        code.emit(b"\x41\xC6\x41\x37\x01")  # multiplier branch applied
        code.label("commit")
        code.emit(b"\xF0\x49\xFF\x41\x08")  # scoped match count
    code.label("exit")
    code.emit(b"\xF0\x49\xFF\x01" if telemetry else b"")  # publish instruction hit
    code.emit(b"\x41\x59" if telemetry else b"")
    code.emit(b"\x41\x58\x58\x9D\xE9")
    code.emit(_rel32(cave + len(code.data) + 4, return_address))
    return code.finish()


def _build_fm24_code(
    cave: int, original: bytes, return_address: int, team_address: int,
    club_address: int, *, multiplier: float | int = MULTIPLIER,
    marker: bytes = CODE_PREFIX, zero_increment: int | None = None,
    bounded_delta: bool = True,
    telemetry: bool = True,
) -> bytes:
    multiplier = _normalized_multiplier(multiplier)
    zero_increment = _zero_increment(multiplier) if zero_increment is None else zero_increment
    code = _Code(cave)
    code.emit(marker)
    code.emit(original)
    if telemetry:
        telemetry_address = cave + TELEMETRY_OFFSET
        code.emit(b"\x9C\x50\x41\x50\x41\x51")  # flags, rax, r8, r9
        code.emit(b"\x49\xB9" + struct.pack("<Q", telemetry_address))
        code.emit(b"\x4D\x89\x69\x10")  # last player address
        code.emit(b"\x66\x45\x89\x71\x20")  # latest raw signed delta
        code.emit(b"\x48\x8B\x44\x24\x10")  # original EAX / CA before
        code.emit(b"\x66\x41\x89\x41\x22")
        code.emit(b"\x41\xC6\x41\x24\x00")  # entered
        code.emit(b"\x49\xC7\x41\x18\x00\x00\x00\x00")
    else:
        code.emit(b"\x9C\x50\x41\x50")  # pushfq; push rax; push r8
    code.emit(b"\x49\x81\xFD\xFF\xFF\x0F\x00")
    code.jump32(b"\x0F\x86", "exit")
    if telemetry:
        code.emit(b"\x41\xC6\x41\x24\x01")
    code.emit(b"\x4D\x8B\x45\x08\x4D\x85\xC0")
    code.jump32(b"\x0F\x84", "exit")
    if telemetry:
        code.emit(b"\x41\xC6\x41\x24\x03")
    code.emit(
        b"\x45\x8B\x40\x04\x41\x81\xF8"
        + struct.pack("<I", MAX_PLAYER_ARRAY_INDEX)
    )
    code.jump32(b"\x0F\x87", "exit")
    if telemetry:
        code.emit(b"\x41\xC6\x41\x24\x04")
    code.emit(b"\x4D\x63\xC0\x4F\x8B\x84\x05\xD0\x00\x00\x00\x4D\x85\xC0")
    code.jump32(b"\x0F\x84", "exit")
    if telemetry:
        code.emit(b"\x41\xC6\x41\x24\x05")
    code.emit(b"\x4D\x8B\x40\x10\x4D\x85\xC0")
    code.jump32(b"\x0F\x84", "exit")
    if telemetry:
        code.emit(b"\x4D\x89\x41\x18")
        code.emit(b"\x41\xC6\x41\x24\x06")
    code.emit(b"\x48\xB8" + struct.pack("<Q", team_address) + b"\x49\x39\xC0")
    code.jump32(b"\x0F\x84", "apply")
    code.emit(b"\x4D\x8B\x40\x30\x48\xB8" + struct.pack("<Q", club_address) + b"\x49\x39\xC0")
    code.jump32(b"\x0F\x85", "exit")
    code.label("apply")
    if telemetry:
        code.emit(b"\x41\xC6\x41\x24\x08")
        code.emit(b"\x41\xC6\x41\x36\x01")
        code.emit(b"\x4D\x89\x69\x28")
        code.emit(b"\x66\x45\x89\x71\x30")
        code.emit(b"\x66\x45\x89\x71\x32")
        code.emit(b"\x48\x8B\x44\x24\x10")
        code.emit(b"\x66\x41\x89\x41\x34")
        code.emit(b"\x41\xC6\x41\x37\x00")
    if bounded_delta:
        code.emit(b"\x66\x41\x81\xFE" + struct.pack("<H", MAX_PLAUSIBLE_CA_DELTA))
        code.jump32(b"\x0F\x8F", "commit" if telemetry else "exit")
        code.emit(b"\x66\x41\x81\xFE" + struct.pack("<h", -MAX_PLAUSIBLE_CA_DELTA))
        code.jump32(b"\x0F\x8C", "commit" if telemetry else "exit")
    code.emit(b"\x66\x45\x85\xF6")
    code.jump32(b"\x0F\x84", "zero")
    code.jump32(b"\x0F\x8F", "positive")
    code.emit(b"\x66\x41\xF7\xDE")
    code.label("positive")
    if multiplier == 1.5:
        code.emit(b"\x66\x45\x6B\xF6\x03\x66\x41\xFF\xC6\x66\x41\xD1\xEE")
    else:
        code.emit(b"\x66\x45\x6B\xF6" + bytes([int(multiplier)]))
    code.jump32(b"\xE9", "record" if telemetry else "exit")
    code.label("zero")
    code.emit(b"\x66\x41\xBE" + struct.pack("<H", zero_increment))
    if telemetry:
        code.label("record")
        code.emit(b"\x66\x45\x89\x71\x32")
        code.emit(b"\x41\xC6\x41\x37\x01")
        code.label("commit")
        code.emit(b"\xF0\x49\xFF\x41\x08")
    code.label("exit")
    code.emit(b"\xF0\x49\xFF\x01" if telemetry else b"")
    code.emit(b"\x41\x59" if telemetry else b"")
    code.emit(b"\x41\x58\x58\x9D\xE9")
    code.emit(_rel32(cave + len(code.data) + 4, return_address))
    return code.finish()


def _managed_code_addresses(
    actual: bytes | None, expected: bytes,
) -> tuple[bool, tuple[int, ...]]:
    if actual is None or len(actual) != len(expected):
        return False, ()
    offsets: list[int] = []
    cursor = 0
    while True:
        operand = expected.find(b"\x48\xB8", cursor)
        if operand < 0:
            break
        offsets.append(operand + 2)
        cursor = operand + 10
    if not offsets:
        return False, ()
    cursor = 0
    addresses: list[int] = []
    for offset in offsets:
        if actual[cursor:offset] != expected[cursor:offset]:
            return False, ()
        addresses.append(struct.unpack_from("<Q", actual, offset)[0])
        cursor = offset + 8
    if actual[cursor:] != expected[cursor:]:
        return False, ()
    return True, tuple(addresses)


def _managed_code_match(actual: bytes | None, expected: bytes) -> tuple[bool, int]:
    matches, addresses = _managed_code_addresses(actual, expected)
    return matches, addresses[0] if matches else 0


def _original_for_layout(layout_key: str) -> bytes:
    return FM24_ORIGINAL if layout_key == "fm24" else HOOK_ORIGINAL


def _build_layout_code(
    layout_key: str, cave: int, hook: int, team_address: int,
    club_address: int, *, multiplier: float | int = MULTIPLIER,
    national_team_address: int = 0,
    marker: bytes = CODE_PREFIX,
    zero_increment: int | None = None,
    original: bytes | None = None,
    bounded_delta: bool = True,
    telemetry: bool | None = None,
    dual_scope: bool = True,
    strict_pointer_checks: bool = True,
) -> bytes:
    original = original or _original_for_layout(layout_key)
    if layout_key == "fm24":
        return _build_fm24_code(
            cave, original, hook + len(original), team_address, club_address,
            multiplier=multiplier, marker=marker,
            zero_increment=zero_increment,
            bounded_delta=bounded_delta,
            telemetry=True if telemetry is None else telemetry,
        )
    return _build_code(
        cave, original, hook + len(original), team_address,
        multiplier=multiplier, marker=marker,
        zero_increment=zero_increment,
        bounded_delta=bounded_delta,
        telemetry=True if telemetry is None else telemetry,
        national_team_address=national_team_address,
        dual_scope=dual_scope,
        strict_pointer_checks=strict_pointer_checks,
    )


class CAGrowthHookController:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.pid = self.hook = self.cave = self.team_address = self.club_address = 0
        self.national_team_address = 0
        self.layout_key = ""
        self.multiplier: float | int = MULTIPLIER
        self.original = self.patch = b""
        self.error: str | None = None
        self.error_code: str | None = None
        self.retryable = False

    def status(self) -> dict[str, Any]:
        with self.lock:
            status = {
                "installed": bool(self.hook),
                "team_address": hex(self.team_address) if self.team_address else None,
                "club_address": hex(self.club_address) if self.club_address else None,
                "national_team_address": (
                    hex(self.national_team_address)
                    if self.national_team_address else None
                ),
                "layout": self.layout_key or None,
                "multiplier": self.multiplier,
                "error": self.error,
                "error_code": self.error_code,
                "retryable": self.retryable,
            }
            status["telemetry"] = self._telemetry_status()
            return status

    def _telemetry_status(self) -> dict[str, Any]:
        if not self.hook or not self.cave or self.layout_key not in {"fm24", "fm26"}:
            return {
                "available": False,
                "observed": False,
                "hit_count": 0,
            }
        try:
            previous = None
            with open_process(self.pid) as process:
                for _attempt in range(3):
                    current = read_process_memory(
                        process, self.cave + TELEMETRY_OFFSET,
                        TELEMETRY_STRUCT.size,
                    )
                    if current == previous:
                        result = _decode_telemetry(current, self.multiplier)
                        return self._with_matched_team_address(result)
                    previous = current
            result = _decode_telemetry(previous, self.multiplier)
            return self._with_matched_team_address(result)
        except Exception:
            return {
                "available": False,
                "observed": False,
                "hit_count": 0,
            }

    def _with_matched_team_address(self, telemetry: dict[str, Any]) -> dict[str, Any]:
        scope = telemetry.get("matched_scope")
        address = (
            self.team_address if scope == "club"
            else self.national_team_address if scope == "national"
            else 0
        )
        if address:
            telemetry["matched_team_address"] = hex(address)
        return telemetry

    def sync(
        self, enabled: bool, team_address: int, club_address: int = 0,
        multiplier: float | int = MULTIPLIER,
        national_team_address: int = 0,
    ) -> dict[str, Any]:
        with self.lock:
            try:
                multiplier = _normalized_multiplier(multiplier)
                if not enabled:
                    self._uninstall()
                    self._remove_orphaned_hook()
                    self.error = None
                    self.error_code = None
                    self.retryable = False
                    return self.status()
                if not team_address and not national_team_address:
                    self._uninstall()
                    raise CAGrowthHookError(
                        "请先刷新并读取当前执教俱乐部，再选择食堂方案",
                        code="team_context_missing", retryable=True,
                    )
                pid, path, layout = select_process_layout()
                supported = layout.key in {"fm24", "fm26"}
                if self.pid and (
                    self.pid != pid
                    or self.layout_key != layout.key
                    or self.team_address != team_address
                    or self.club_address != club_address
                    or self.national_team_address != national_team_address
                    or self.multiplier != multiplier
                    or not supported
                ):
                    self._uninstall()
                if not supported:
                    raise CAGrowthHookError(
                        "当前游戏版本暂时无法启用食堂成长加速",
                        code="unsupported_game", retryable=False,
                    )
                if not self.hook:
                    self._install(
                        pid, path, layout, team_address, club_address,
                        multiplier,
                        national_team_address,
                    )
                self.error = None
                self.error_code = None
                self.retryable = False
            except Exception as error:
                friendly = _friendly_error(error)
                self.error = str(friendly)
                self.error_code = friendly.code
                self.retryable = friendly.retryable
                if not self.hook:
                    self._clear()
            return self.status()

    def close(self) -> None:
        with self.lock:
            self._uninstall()

    @staticmethod
    def _allocate_near(process, hook: int, size: int = 0x1000) -> int:
        granularity = 0x10000; origin = hook & ~(granularity - 1)
        for distance in range(granularity, 0x70000000, granularity):
            for hint in (origin + distance, max(granularity, origin - distance)):
                address = kernel32.VirtualAllocEx(process.handle, ctypes.c_void_p(hint), size, MEM_COMMIT_RESERVE, PAGE_EXECUTE_READWRITE)
                value = int(address or 0)
                if value and abs(value - hook) < 0x7FFFFFFF:
                    return value
                if value:
                    kernel32.VirtualFreeEx(process.handle, ctypes.c_void_p(value), 0, MEM_RELEASE)
        raise RuntimeError("无法在 CA 代码附近分配内存")

    @staticmethod
    def _scan_module_pattern(
        process: Any, module: Any, pattern: tuple[int | None, ...],
    ) -> list[int]:
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
        return sorted(set(hits))

    @classmethod
    def _resolve_pattern_hook(
        cls, process: Any, module: Any, signature: bytes,
    ) -> int:
        hits = cls._scan_module_pattern(process, module, tuple(signature))
        if len(hits) != 1:
            raise CAGrowthHookError(
                "当前游戏版本暂时无法启用食堂成长加速，请更新 FMODD 后重试",
                code="growth_signature_unavailable", retryable=False,
                detail=f"growth signature hits={len(hits)}",
            )
        return hits[0]

    @classmethod
    def _resolve_patched_hook(
        cls, process: Any, module: Any, signature: bytes,
    ) -> int | None:
        pattern = (
            0xE9, None, None, None, None, 0x90, 0x90, 0x90,
            *tuple(signature[8:]),
        )
        hits = cls._scan_module_pattern(process, module, pattern)
        if not hits:
            return None
        managed: list[int] = []
        for hook in hits:
            entry = read_process_memory(process, hook, 8)
            if not entry or len(entry) != 8:
                continue
            cave = hook + 5 + struct.unpack_from("<i", entry, 1)[0]
            prefix = read_process_memory(process, cave, len(CODE_PREFIX))
            if prefix in {CODE_PREFIX, LEGACY_CODE_PREFIX}:
                managed.append(hook)
        managed = sorted(set(managed))
        if len(managed) == 1:
            return managed[0]
        if len(hits) == 1:
            # A unique foreign JMP at the complete growth signature location is
            # safe to classify as a conflict; installation still refuses it.
            return hits[0]
        return None

    @classmethod
    def _resolve_hook_entry(
        cls, process: Any, module: Any, layout: Any,
    ) -> tuple[int, bytes]:
        original = _original_for_layout(layout.key)
        fixed_rva = getattr(layout, "ca_growth_hook_rva", None)
        if fixed_rva is not None:
            hook = int(module.base_address) + int(fixed_rva)
            entry = read_process_memory(process, hook, len(original))
            if entry == original or (entry and entry[0] == 0xE9):
                return hook, original
        signature = FM24_SIGNATURE if layout.key == "fm24" else FM26_SIGNATURE
        try:
            hook = cls._resolve_pattern_hook(process, module, signature)
        except CAGrowthHookError as error:
            hook = cls._resolve_patched_hook(process, module, signature)
            if hook is None:
                raise error
        return hook, signature[:len(original)]

    def _install(
        self, pid: int, _path: str, layout: Any, team_address: int,
        club_address: int, multiplier: float | int,
        national_team_address: int = 0,
    ) -> None:
        with open_process(pid, write_memory=True) as process:
            module = layout.module(process)
            if not module:
                raise RuntimeError(f"{layout.module_name} 尚未加载")
            hook, original = self._resolve_hook_entry(process, module, layout)
            entry = read_process_memory(process, hook, len(original))
            if entry != original:
                adopted = self._adopt_or_remove_existing(
                    process, pid, layout.key, hook, entry, original,
                    team_address, club_address, multiplier,
                    national_team_address,
                )
                if adopted:
                    return
                entry = read_process_memory(process, hook, len(original))
            if entry != original:
                raise CAGrowthHookError(
                    "检测到其他修改工具正在占用球员成长功能，请关闭后刷新再试",
                    code="growth_hook_conflict", retryable=True,
                )
            cave = self._allocate_near(process, hook)
            code = _build_layout_code(
                layout.key, cave, hook, team_address, club_address,
                multiplier=multiplier, original=original,
                national_team_address=national_team_address,
            )
            write_process_memory(process, cave, code)
            patch = b"\xE9" + _rel32(hook + 5, cave) + b"\x90" * 3
            self._write_code(process, hook, patch)
            self.pid, self.hook, self.cave = pid, hook, cave
            self.team_address, self.club_address = team_address, club_address
            self.national_team_address = national_team_address
            self.layout_key = layout.key
            self.multiplier = multiplier
            self.original, self.patch = original, patch

    def _remove_orphaned_hook(self) -> None:
        """Remove a recognized FMODD cave left by an older service process."""
        try:
            pid, _path, layout = select_process_layout()
            if layout.key not in {"fm24", "fm26"} or layout.ca_growth_hook_rva is None:
                return
            with open_process(pid, write_memory=True) as process:
                module = layout.module(process)
                if not module:
                    return
                original = _original_for_layout(layout.key)
                hook = module.base_address + int(layout.ca_growth_hook_rva)
                entry = read_process_memory(process, hook, len(original))
                if not entry or entry == original or entry[0] != 0xE9:
                    return
                self._adopt_or_remove_existing(
                    process, pid, layout.key, hook, entry, original,
                    0, 0, MULTIPLIER,
                    0,
                )
                if read_process_memory(process, hook, len(original)) != original:
                    return
        except Exception:
            return

    def _adopt_or_remove_existing(
        self, process: Any, pid: int, layout_key: str, hook: int,
        entry: bytes | None, original: bytes, team_address: int,
        club_address: int, multiplier: float | int,
        national_team_address: int = 0,
    ) -> bool:
        if not entry or len(entry) != len(original) or entry[0] != 0xE9:
            return False
        cave = hook + 5 + struct.unpack_from("<i", entry, 1)[0]
        if cave < 0x100000 or abs(cave - hook) >= 0x7FFFFFFF:
            return False
        telemetry_variants = (True, False)
        fm26_boolean_variants = (True, False)
        scope_variants = fm26_boolean_variants if layout_key == "fm26" else (False,)
        pointer_variants = fm26_boolean_variants if layout_key == "fm26" else (False,)
        variants = tuple(
            (candidate_multiplier, zero_increment, bounded_delta, telemetry, dual_scope, strict_pointer_checks, _build_layout_code(
                layout_key, cave, hook, 0, 0,
                multiplier=candidate_multiplier, marker=marker,
                national_team_address=0,
                zero_increment=zero_increment,
                original=original,
                bounded_delta=bounded_delta,
                telemetry=telemetry,
                dual_scope=dual_scope,
                strict_pointer_checks=strict_pointer_checks,
            ))
            for candidate_multiplier in (1.5, 2, 3)
            for zero_increment in (
                (_zero_increment(candidate_multiplier),)
                if candidate_multiplier != 3 else (_zero_increment(candidate_multiplier), 1)
            )
            for bounded_delta, marker in (
                (True, CODE_PREFIX),
                (True, b""),
                (False, LEGACY_CODE_PREFIX),
                (False, b""),
            )
            for telemetry in telemetry_variants
            for dual_scope in scope_variants
            for strict_pointer_checks in pointer_variants
        )
        installed_addresses: tuple[int, ...] = ()
        installed_multiplier = MULTIPLIER
        installed_current = False
        for candidate_multiplier, zero_increment, bounded_delta, telemetry, dual_scope, strict_pointer_checks, expected in variants:
            actual = read_process_memory(process, cave, len(expected))
            matches, installed_addresses = _managed_code_addresses(actual, expected)
            if matches:
                installed_multiplier = candidate_multiplier
                installed_current = (
                    bounded_delta
                    and zero_increment == _zero_increment(candidate_multiplier)
                    and telemetry
                    and (layout_key != "fm26" or dual_scope)
                    and (layout_key != "fm26" or strict_pointer_checks)
                )
                break
        else:
            return False
        patch = bytes(entry)
        desired_addresses = (
            (team_address, club_address) if layout_key == "fm24"
            else (team_address, national_team_address)
        )
        if (
            installed_addresses == desired_addresses
            and installed_multiplier == multiplier
            and installed_current
        ):
            self.pid, self.hook, self.cave = pid, hook, cave
            self.team_address, self.club_address = team_address, club_address
            self.national_team_address = national_team_address
            self.layout_key = layout_key
            self.multiplier = multiplier
            self.original, self.patch = original, patch
            return True
        self._write_code(process, hook, original)
        time.sleep(0.1)
        kernel32.VirtualFreeEx(
            process.handle, ctypes.c_void_p(cave), 0, MEM_RELEASE,
        )
        return False

    def _uninstall(self) -> None:
        if not self.hook:
            self._clear(); return
        try:
            with open_process(self.pid, write_memory=True) as process:
                if read_process_memory(process, self.hook, 8) == self.patch:
                    self._write_code(process, self.hook, self.original)
                if self.cave:
                    time.sleep(0.1)
                    kernel32.VirtualFreeEx(process.handle, ctypes.c_void_p(self.cave), 0, MEM_RELEASE)
        except Exception:
            pass
        self._clear()

    def _clear(self) -> None:
        self.pid = self.hook = self.cave = self.team_address = self.club_address = 0
        self.national_team_address = 0
        self.layout_key = ""
        self.multiplier = MULTIPLIER
        self.original = self.patch = b""

    @staticmethod
    def _write_code(process, address: int, data: bytes) -> None:
        old = wintypes.DWORD(0)
        if not kernel32.VirtualProtectEx(process.handle, ctypes.c_void_p(address), len(data), PAGE_EXECUTE_READWRITE, ctypes.byref(old)):
            raise RuntimeError("无法修改 CA 代码页保护")
        try:
            write_process_memory(process, address, data)
            kernel32.FlushInstructionCache(process.handle, ctypes.c_void_p(address), len(data))
        finally:
            restored = wintypes.DWORD(0)
            kernel32.VirtualProtectEx(process.handle, ctypes.c_void_p(address), len(data), old.value, ctypes.byref(restored))
