from __future__ import annotations

import ctypes
import struct
import threading
import time
from ctypes import wintypes
from typing import Any

from fm_collector.win32 import (
    MEM_IMAGE, PAGE_EXECUTE_READ, PAGE_EXECUTE_READWRITE,
    PAGE_EXECUTE_WRITECOPY, find_module, iter_readable_regions, kernel32,
    open_process, read_process_memory, write_process_memory,
)
from tools.initial_data_audit import select_process_layout
from tools.redbull_hook import MEM_COMMIT_RESERVE, MEM_RELEASE, _Code, _rel32


DEPARTURE_PATTERN = (
    0x8B, None, 0x2C, 0x8B, None, 0x30, 0x85, 0xC0, 0x79, None,
    0x89, 0xCA, None, None, 0x00, 0x00, 0x08, 0x00,
)
FM24_DEPARTURE_PATTERN = (
    0x8B, 0x87, None, 0x00, 0x00, 0x00,
    0x8B, None, None, 0x00, 0x00, 0x00, 0x85, 0xC0, 0x79,
)
FM24_SALARY_PATTERN = (
    0x4C, 0x8D, 0x8D, None, None, None, 0x00,
    0x44, 0x89, None, 0x4D, 0x89, None, 0xE8,
)
SALARY_CALL_PATTERN = (
    0x45, 0x31, 0xC0, 0xE8, None, None, None, None,
    0x89, 0xC6, 0x41, 0x89, 0x86, None, None, 0x00, 0x00,
)
PROMISE_SIGNATURE = bytes.fromhex("44 0F B7 89 F8 01 00 00")
EXECUTE_MASK = PAGE_EXECUTE_READ | PAGE_EXECUTE_READWRITE | PAGE_EXECUTE_WRITECOPY
INTERVIEW_PATTERN = (
    0xE8, None, None, None, None, 0x8B, None, 0x10, 0x0F, 0x57,
    0xC0, 0xF3, 0x0F, 0x2A, 0xC0, 0xF3, 0x0F, 0x59, 0x05,
)
FM24_INTERVIEW_SCORE_PATTERN = (
    0x81, 0xFB, 0xC1, 0xD4, 0x01, 0x00, 0x7C,
)
FM24_INTERVIEW_MULTIPLIER_PATTERN = (
    0x48, 0x8D, 0x05, None, None, None, None,
    0xF2, 0x0F, 0x59, 0x04, None, 0xF2, 0x0F, 0x2C, 0xC0,
)


def _matches(image: bytes, pattern: tuple[int | None, ...]) -> list[int]:
    fixed_runs: list[tuple[int, bytes]] = []
    start = -1
    run = bytearray()
    for index, value in enumerate((*pattern, None)):
        if value is not None:
            if start < 0:
                start = index
            run.append(value)
        elif run:
            fixed_runs.append((start, bytes(run)))
            start, run = -1, bytearray()
    anchor_offset, anchor = max(fixed_runs, key=lambda item: len(item[1]))
    hits: list[int] = []
    cursor = 0
    while True:
        found = image.find(anchor, cursor)
        if found < 0:
            return hits
        candidate = found - anchor_offset
        if candidate >= 0 and candidate + len(pattern) <= len(image) and all(
            expected is None or image[candidate + index] == expected
            for index, expected in enumerate(pattern)
        ):
            hits.append(candidate)
        cursor = found + 1


def _departure_code(
    cave: int, original: bytes, return_address: int,
    manager_address: int, team_address: int,
) -> bytes:
    code = _Code(cave)
    code.emit(original)
    code.emit(b"\x9C\x52\x41\x50")  # pushfq; push rdx; push r8
    code.emit(b"\x48\xBA" + struct.pack("<Q", manager_address))
    code.emit(b"\x4D\xB8" + struct.pack("<Q", team_address))
    code.emit(b"\x4C\x39\x82\x10\x02\x00\x00")
    code.jump32(b"\x0F\x85", "exit")
    code.emit(b"\xB8\xFF\xFF\xFF\x00")
    code.label("exit")
    code.emit(b"\x41\x58\x5A\x9D\xE9")
    code.emit(_rel32(cave + len(code.data) + 4, return_address))
    return code.finish()


def _fm24_departure_code(
    cave: int, original: bytes, return_address: int,
    team_address: int, club_address: int,
) -> bytes:
    code = _Code(cave)
    code.emit(original)
    code.emit(b"\x9C\x52\x41\x50")  # pushfq; push rdx; push r8
    code.emit(b"\x49\xB8" + struct.pack("<Q", club_address))
    code.emit(b"\x4D\x39\xC1")  # cmp r9, r8
    code.jump32(b"\x0F\x84", "valid")
    code.emit(b"\x48\x81\xFF\xFF\xFF\x0F\x00")  # cmp rdi, 0xfffff
    code.jump32(b"\x0F\x86", "exit")
    code.emit(b"\x48\x8B\x97\xA0\x00\x00\x00")  # mov rdx, [rdi+0xa0]
    for offset in (0x08, 0xC8, 0x10):
        code.emit(b"\x48\x81\xFA\xFF\xFF\x0F\x00")  # cmp rdx, 0xfffff
        code.jump32(b"\x0F\x86", "exit")
        if offset <= 0x7F:
            code.emit(b"\x48\x8B\x52" + bytes((offset,)))
        else:
            code.emit(b"\x48\x8B\x92" + struct.pack("<I", offset))
    code.emit(b"\x49\xB8" + struct.pack("<Q", team_address))
    code.emit(b"\x4C\x39\xC2")  # cmp rdx, r8
    code.jump32(b"\x0F\x84", "valid")
    code.emit(b"\x48\x81\xFA\xFF\xFF\x0F\x00")
    code.jump32(b"\x0F\x86", "exit")
    code.emit(b"\x48\x8B\x52\x30")  # mov rdx, [rdx+0x30]
    code.emit(b"\x49\xB8" + struct.pack("<Q", club_address))
    code.emit(b"\x4C\x39\xC2")  # cmp rdx, r8
    code.jump32(b"\x0F\x85", "exit")
    code.label("valid")
    code.emit(b"\xB8\xFF\xFF\xFF\x7F")
    code.label("exit")
    code.emit(b"\x41\x58\x5A\x9D\xE9")
    code.emit(_rel32(cave + len(code.data) + 4, return_address))
    return code.finish()


def _fm24_salary_code(
    cave: int, original: bytes, return_address: int, manager_address: int,
) -> bytes:
    code = _Code(cave)
    code.emit(original)
    code.emit(b"\x9C\x52\x41\x50")  # pushfq; push rdx; push r8
    code.emit(b"\x48\x3D\xFF\xFF\x0F\x00")  # cmp rax, 0xfffff
    code.jump32(b"\x0F\x86", "exit")
    code.emit(b"\x48\x8B\x50\x08")  # mov rdx, [rax+8]
    code.emit(b"\x49\xB8" + struct.pack("<Q", manager_address))
    code.emit(b"\x4C\x39\xC2")  # cmp rdx, r8
    code.jump32(b"\x0F\x85", "exit")
    code.emit(b"\xB9\xFF\xFF\xFF\x00")
    code.label("exit")
    code.emit(b"\x41\x58\x5A\x9D\xE9")
    code.emit(_rel32(cave + len(code.data) + 4, return_address))
    return code.finish()


def _salary_budget_code(
    cave: int, original: bytes, return_address: int, manager_address: int,
) -> bytes:
    code = _Code(cave)
    code.emit(b"\x9C\x41\x50")
    code.emit(b"\x49\x81\xFE\xFF\xFF\x0F\x00")
    code.jump32(b"\x0F\x86", "original")
    code.emit(b"\x4D\x8B\x46\x08")
    code.emit(b"\x48\xB8" + struct.pack("<Q", manager_address))
    code.emit(b"\x49\x39\xC0")
    code.jump32(b"\x0F\x85", "original")
    code.emit(b"\xB8\xFF\xFF\xFF\x00\x41\x58\x9D\xC3")
    code.label("original")
    code.emit(b"\x41\x58\x9D")
    code.emit(original)
    code.emit(b"\xE9" + _rel32(cave + len(code.data) + 5, return_address))
    return code.finish()


def _salary_promise_code(cave: int, original: bytes, return_address: int) -> bytes:
    code = _Code(cave)
    code.emit(b"\x66\xC7\x81\xF8\x01\x00\x00\x14\x00")
    code.emit(b"\xC7\x81\xE0\x01\x00\x00\x7F\x96\x98\x00")
    code.emit(original)
    code.emit(b"\xE9" + _rel32(cave + len(code.data) + 5, return_address))
    return code.finish()

def _interview_code(cave: int, original: bytes, return_address: int, manager_address: int) -> bytes:
    code = _Code(cave)
    code.emit(original)
    code.emit(b"\x49\xBF" + struct.pack("<Q", manager_address))
    code.emit(b"\x4D\x39\xFE")
    code.jump32(b"\x0F\x85", "exit")
    code.emit(b"\xB8\x40\x42\x0F\x00")
    code.label("exit")
    code.emit(b"\xE9" + _rel32(cave + len(code.data) + 5, return_address))
    return code.finish()


def _fm24_interview_score_code(
    cave: int, original: bytes, return_address: int, manager_address: int,
) -> bytes:
    code = _Code(cave)
    code.emit(b"\x9C\x50")  # pushfq; push rax
    code.emit(b"\x48\xB8" + struct.pack("<Q", manager_address))
    code.emit(b"\x48\x39\xC7")  # cmp rdi, rax
    code.jump32(b"\x0F\x85", "original")
    code.emit(b"\xBB\x40\x42\x0F\x00")  # mov ebx, 1,000,000
    code.label("original")
    code.emit(b"\x58\x9D")
    code.emit(original)
    code.emit(b"\xE9" + _rel32(cave + len(code.data) + 5, return_address))
    return code.finish()


def _fm24_interview_multiplier_code(
    cave: int, original: bytes, return_address: int,
) -> bytes:
    code = _Code(cave)
    code.emit(original)
    code.emit(b"\x50")
    code.emit(b"\x48\xB8" + struct.pack("<d", 1_000_000.0))
    code.emit(b"\x66\x48\x0F\x6E\xC0")  # movq xmm0, rax
    code.emit(b"\x58")
    code.emit(b"\xE9" + _rel32(cave + len(code.data) + 5, return_address))
    return code.finish()


class ClubPolicyHookController:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.pid = self.manager_address = self.team_address = self.club_address = 0
        self.layout_key = ""
        self.installed: dict[str, dict[str, Any]] = {}
        self.errors: dict[str, str] = {}

    def _interview_components(self) -> set[str]:
        if self.layout_key == "fm24":
            return {"interview_perfume", "interview_perfume_multiplier"}
        return {"interview_perfume"}

    def status(self) -> dict[str, Any]:
        with self.lock:
            salary_components = (
                {"salary_budget"}
                if self.layout_key == "fm24"
                else {"salary_budget", "salary_promises"}
            )
            return {
                "installed": sorted(self.installed),
                "errors": dict(self.errors),
                "layout_key": self.layout_key,
                "departure_active": "departure_mediation" in self.installed,
                "interview_active": self._interview_components().issubset(self.installed),
                "salary_active": salary_components.issubset(self.installed),
            }

    def sync(
        self, *, departure_active: bool, salary_active: bool, interview_active: bool = False,
        manager_address: int, team_address: int, club_address: int = 0,
    ) -> dict[str, Any]:
        with self.lock:
            identity = (int(manager_address), int(team_address), int(club_address))
            previous = (self.manager_address, self.team_address, self.club_address)
            if any(previous) and previous != identity:
                self.close()
            self.manager_address, self.team_address, self.club_address = identity
            desired = set()
            if departure_active:
                desired.add("departure_mediation")
            if interview_active:
                desired.update(self._interview_components())
            if salary_active:
                desired.add("salary_budget")
                if self.layout_key == "fm26":
                    desired.add("salary_promises")
            if not manager_address or not team_address:
                desired.clear()
            if "departure_mediation" not in desired:
                self.errors.pop("departure_mediation", None)
            if not desired.intersection({"salary_budget", "salary_promises"}):
                self.errors.pop("salary_authorization", None)
            for key in list(self.installed):
                if key not in desired:
                    self._disable(key)
            if "departure_mediation" in desired and "departure_mediation" not in self.installed:
                self._enable_group(
                    "departure_mediation", lambda process, module, image, layout: self._enable_departure(
                        process, module, image, layout, manager_address,
                        team_address, club_address,
                    ),
                )
            interview_components = self._interview_components()
            if interview_active and not interview_components.issubset(self.installed):
                for key in interview_components.intersection(self.installed):
                    self._disable(key)
                self._enable_group(
                    "interview_perfume", lambda process, module, image, layout: self._enable_interview(
                        process, module, image, layout, manager_address,
                    ),
                )
                desired.update(self._interview_components())
            salary_missing = desired.intersection({"salary_budget", "salary_promises"}) - self.installed.keys()
            if salary_missing:
                self._enable_salary(manager_address)
            return self.status()

    def close(self) -> None:
        with self.lock:
            for key in list(self.installed):
                self._disable(key)
            if not self.installed:
                self.pid = 0
                self.layout_key = ""

    def _open_verified_module(self):
        pid, _path, layout = select_process_layout()
        if layout.key not in {"fm24", "fm26"}:
            raise RuntimeError("该俱乐部授权仅支持已识别的 FM24/FM26")
        process = open_process(pid, write_memory=True)
        module = find_module(process, layout.module_name)
        if not module:
            process.close()
            raise RuntimeError(f"{layout.module_name} 尚未加载")
        return pid, process, module, None, layout

    @staticmethod
    def _module_bytes(
        process, module, image: bytes | None, offset: int, size: int, label: str,
    ) -> bytes:
        if image is not None:
            data = image[offset:offset + size]
        else:
            data = read_process_memory(
                process, int(module.base_address) + int(offset), int(size),
            )
        if not data or len(data) != size:
            raise RuntimeError(f"无法读取{label}")
        return data

    @classmethod
    def _pattern_offsets(
        cls, process, module, image: bytes | None,
        patterns: dict[str, tuple[int | None, ...]],
        verified_rvas: dict[str, int | None] | None = None,
    ) -> dict[str, list[int]]:
        verified_rvas = verified_rvas or {}
        results: dict[str, list[int]] = {}
        pending: dict[str, tuple[int | None, ...]] = {}
        for name, pattern in patterns.items():
            verified_rva = verified_rvas.get(name)
            if verified_rva is None:
                pending[name] = pattern
                continue
            sample = cls._module_bytes(
                process, module, image, int(verified_rva), len(pattern),
                f"{module.name} 固定特征码",
            )
            if _matches(sample, pattern) != [0]:
                raise RuntimeError(
                    f"{module.name} 固定特征码与当前已识别版本不一致"
                )
            results[name] = [int(verified_rva)]

        if not pending:
            return results
        if image is not None:
            results.update({
                name: _matches(image, pattern)
                for name, pattern in pending.items()
            })
            return results

        results.update({name: [] for name in pending})
        module_start = int(module.base_address)
        module_end = module_start + int(module.size)
        for region in iter_readable_regions(process):
            if (
                region.type != MEM_IMAGE
                or not region.protect & EXECUTE_MASK
                or region.base_address < module_start
                or region.base_address >= module_end
            ):
                continue
            size = min(int(region.size), module_end - int(region.base_address))
            data = read_process_memory(process, region.base_address, size)
            if not data:
                continue
            base_offset = int(region.base_address) - module_start
            for name, pattern in pending.items():
                results[name].extend(
                    base_offset + offset for offset in _matches(data, pattern)
                )
        return results

    def _enable_group(self, key: str, installer) -> None:
        try:
            pid, process, module, image, layout = self._open_verified_module()
            if self.layout_key and self.layout_key != layout.key:
                raise RuntimeError("俱乐部授权的游戏版本已变化，请重新连接")
            self.pid = pid
            with process:
                installer(process, module, image, layout)
            self.pid = pid
            self.layout_key = layout.key
            self.errors.pop(key, None)
        except Exception as error:
            self.errors[key] = str(error)

    def _enable_departure(
        self, process, module, image: bytes | None, layout,
        manager_address: int, team_address: int, club_address: int,
    ) -> None:
        pattern = FM24_DEPARTURE_PATTERN if layout.key == "fm24" else DEPARTURE_PATTERN
        offsets = self._pattern_offsets(
            process, module, image, {"departure": pattern},
            {"departure": getattr(layout, "club_policy_departure_pattern_rva", None)},
        )["departure"]
        if len(offsets) != 1:
            raise RuntimeError(f"劝离特征码命中 {len(offsets)} 处，已拒绝安装")
        offset = offsets[0]
        if layout.key == "fm24" and not club_address:
            raise RuntimeError("尚未确认当前俱乐部地址")
        if layout.key == "fm24":
            builder = lambda cave, original, returning: _fm24_departure_code(
                cave, original, returning, team_address, club_address,
            )
        else:
            builder = lambda cave, original, returning: _departure_code(
                cave, original, returning, manager_address, team_address,
            )
        self._install(
            process, "departure_mediation", module.base_address + offset,
            self._module_bytes(
                process, module, image, offset, 6, "劝离授权入口",
            ),
            builder,
        )

    def _enable_interview(
        self, process, module, image: bytes | None, layout, manager_address: int,
    ) -> None:
        if layout.key == "fm24":
            offsets = self._pattern_offsets(
                process, module, image,
                {
                    "score": FM24_INTERVIEW_SCORE_PATTERN,
                    "multiplier": FM24_INTERVIEW_MULTIPLIER_PATTERN,
                },
            )
            score_offsets = offsets["score"]
            multiplier_offsets = offsets["multiplier"]
            if len(score_offsets) != 1:
                raise RuntimeError(
                    f"FM24 应聘评分特征码命中 {len(score_offsets)} 处，已拒绝安装"
                )
            if len(multiplier_offsets) != 1:
                raise RuntimeError(
                    f"FM24 应聘倍率特征码命中 {len(multiplier_offsets)} 处，已拒绝安装"
                )
            enabled_now: list[str] = []
            try:
                score_offset = score_offsets[0]
                self._install(
                    process, "interview_perfume",
                    module.base_address + score_offset,
                    self._module_bytes(
                        process, module, image, score_offset, 6, "FM24 应聘评分入口",
                    ),
                    lambda cave, original, returning: _fm24_interview_score_code(
                        cave, original, returning, manager_address,
                    ),
                )
                enabled_now.append("interview_perfume")
                multiplier_offset = multiplier_offsets[0] + 7
                self._install(
                    process, "interview_perfume_multiplier",
                    module.base_address + multiplier_offset,
                    self._module_bytes(
                        process, module, image, multiplier_offset, 5,
                        "FM24 应聘倍乘入口",
                    ),
                    _fm24_interview_multiplier_code,
                )
                enabled_now.append("interview_perfume_multiplier")
            except Exception as install_error:
                for key in reversed(enabled_now):
                    self._disable(key)
                if any(key in self.installed for key in enabled_now):
                    raise RuntimeError(
                        "FM24 应聘香水双 Hook 安装失败且回滚不完整"
                    ) from install_error
                raise
            return
        if layout.key != "fm26":
            raise RuntimeError("应聘香水仅支持已识别的 FM24/FM26")
        offsets = self._pattern_offsets(
            process, module, image, {"interview": INTERVIEW_PATTERN},
        )["interview"]
        if len(offsets) != 1:
            raise RuntimeError(f"应聘面试特征码命中 {len(offsets)} 处，已拒绝安装")
        offset = offsets[0] + 5
        self._install(
            process, "interview_perfume", module.base_address + offset,
            self._module_bytes(
                process, module, image, offset, 6, "FM26 应聘评分入口",
            ),
            lambda cave, original, returning: _interview_code(
                cave, original, returning, manager_address,
            ),
        )

    def _enable_salary(self, manager_address: int) -> None:
        enabled_now: list[str] = []
        try:
            pid, process, module, image, layout = self._open_verified_module()
            if self.layout_key and self.layout_key != layout.key:
                raise RuntimeError("俱乐部授权的游戏版本已变化，请重新连接")
            with process:
                if layout.key == "fm24":
                    offsets = self._pattern_offsets(
                        process, module, image, {"salary": FM24_SALARY_PATTERN},
                        {"salary": getattr(layout, "club_policy_salary_pattern_rva", None)},
                    )["salary"]
                    if len(offsets) != 1:
                        raise RuntimeError(
                            f"薪酬谈判特征码命中 {len(offsets)} 处，已拒绝安装"
                        )
                    offset = offsets[0] + 7
                    self._install(
                        process, "salary_budget", module.base_address + offset,
                        self._module_bytes(
                            process, module, image, offset, 6, "FM24 薪酬谈判入口",
                        ),
                        lambda cave, original, returning: _fm24_salary_code(
                            cave, original, returning, manager_address,
                        ),
                    )
                    enabled_now.append("salary_budget")
                    self.pid = pid
                    self.layout_key = layout.key
                    self.errors.pop("salary_authorization", None)
                    return
                offsets = self._pattern_offsets(
                    process, module, image,
                    {
                        "salary": SALARY_CALL_PATTERN,
                        "promise": tuple(PROMISE_SIGNATURE),
                    },
                    {
                        "salary": getattr(layout, "club_policy_salary_pattern_rva", None),
                        "promise": getattr(
                            layout, "club_policy_salary_promise_pattern_rva", None,
                        ),
                    },
                )
                calls = offsets["salary"]
                promises = offsets["promise"]
                if len(calls) != 1:
                    raise RuntimeError(f"薪酬预算特征码命中 {len(calls)} 处，已拒绝安装")
                if len(promises) != 1:
                    raise RuntimeError(f"续约承诺特征码命中 {len(promises)} 处，已拒绝安装")
                call = module.base_address + calls[0] + 3
                displacement = struct.unpack(
                    "<i", self._module_bytes(
                        process, module, image, calls[0] + 4, 4,
                        "薪酬预算调用位移",
                    ),
                )[0]
                budget_hook = call + 5 + displacement
                budget_offset = budget_hook - module.base_address
                if budget_offset < 0 or budget_offset + 5 > int(module.size):
                    raise RuntimeError("薪酬预算入口不在 game_plugin.dll 内")
                self._install(
                    process, "salary_budget", budget_hook,
                    self._module_bytes(
                        process, module, image, budget_offset, 5,
                        "薪酬预算入口",
                    ),
                    lambda cave, original, returning: _salary_budget_code(
                        cave, original, returning, manager_address,
                    ),
                )
                enabled_now.append("salary_budget")
                promise_offset = promises[0]
                self._install(
                    process, "salary_promises", module.base_address + promise_offset,
                    self._module_bytes(
                        process, module, image, promise_offset,
                        len(PROMISE_SIGNATURE), "续约承诺入口",
                    ),
                    _salary_promise_code,
                )
                enabled_now.append("salary_promises")
            self.pid = pid
            self.layout_key = layout.key
            self.errors.pop("salary_authorization", None)
        except Exception as error:
            for key in reversed(enabled_now):
                self._disable(key)
            self.errors["salary_authorization"] = str(error)

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
                    kernel32.VirtualFreeEx(
                        process.handle, ctypes.c_void_p(address), 0, MEM_RELEASE,
                    )
        raise RuntimeError("无法在俱乐部授权代码附近分配内存")

    def _install(self, process, key: str, hook: int, original: bytes, builder) -> None:
        if len(original) < 5:
            raise RuntimeError("俱乐部授权原始指令长度不足")
        if read_process_memory(process, hook, len(original)) != original:
            raise RuntimeError("俱乐部授权原始指令已变化")
        cave = self._allocate_near(process, hook)
        patch = b""
        patched = False
        try:
            code = builder(cave, original, hook + len(original))
            write_process_memory(process, cave, code)
            patch = b"\xE9" + _rel32(hook + 5, cave) + b"\x90" * (len(original) - 5)
            self._write_code(process, hook, patch)
            patched = True
            if read_process_memory(process, hook, len(patch)) != patch:
                raise RuntimeError("俱乐部授权补丁写后回读失败")
        except Exception as install_error:
            if patched and read_process_memory(process, hook, len(patch)) == patch:
                try:
                    self._write_code(process, hook, original)
                except Exception as rollback_error:
                    self.installed[key] = {
                        "hook": hook, "cave": cave, "original": original,
                        "patch": patch,
                    }
                    raise RuntimeError(
                        f"俱乐部授权安装失败且回滚失败：{rollback_error}"
                    ) from install_error
            kernel32.VirtualFreeEx(process.handle, ctypes.c_void_p(cave), 0, MEM_RELEASE)
            raise
        self.installed[key] = {
            "hook": hook, "cave": cave, "original": original, "patch": patch,
        }

    def _disable(self, key: str) -> None:
        record = self.installed.get(key)
        if not record:
            return
        try:
            with open_process(self.pid, write_memory=True) as process:
                current = read_process_memory(
                    process, record["hook"], len(record["patch"]),
                )
                if current == record["patch"]:
                    self._write_code(process, record["hook"], record["original"])
                    if read_process_memory(
                        process, record["hook"], len(record["original"]),
                    ) != record["original"]:
                        raise RuntimeError("俱乐部授权卸载写后回读失败")
                elif current != record["original"]:
                    raise RuntimeError("俱乐部授权入口已被其他补丁修改，未释放代码洞")
                time.sleep(0.05)
                kernel32.VirtualFreeEx(
                    process.handle, ctypes.c_void_p(record["cave"]), 0, MEM_RELEASE,
                )
            self.installed.pop(key, None)
            self.errors.pop(f"restore:{key}", None)
        except Exception as error:
            self.errors[f"restore:{key}"] = str(error)

    @staticmethod
    def _write_code(process, address: int, data: bytes) -> None:
        old = wintypes.DWORD(0)
        if not kernel32.VirtualProtectEx(
            process.handle, ctypes.c_void_p(address), len(data),
            PAGE_EXECUTE_READWRITE, ctypes.byref(old),
        ):
            raise RuntimeError("无法修改俱乐部授权代码页保护")
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
