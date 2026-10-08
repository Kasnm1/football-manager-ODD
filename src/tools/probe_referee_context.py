from __future__ import annotations

import ctypes
import json
import struct
import time
from ctypes import wintypes
from datetime import datetime
from pathlib import Path

from fm_collector.win32 import (
    PAGE_EXECUTE_READWRITE, find_module, kernel32, open_process,
    read_process_memory, write_process_memory,
)
from tools.initial_data_audit import GAME_PLUGIN, select_process
from tools.redbull_hook import MEM_COMMIT_RESERVE, MEM_RELEASE, _matches, _rel32
from tools.referee_hook import REFEREE_PATTERN


def allocate_near(process, hook: int) -> int:
    origin = hook & ~0xFFFF
    for distance in range(0x10000, 0x70000000, 0x10000):
        for hint in (origin + distance, max(0x10000, origin - distance)):
            value = int(kernel32.VirtualAllocEx(
                process.handle, ctypes.c_void_p(hint), 0x1000,
                MEM_COMMIT_RESERVE, PAGE_EXECUTE_READWRITE,
            ) or 0)
            if value and abs(value - hook) < 0x7FFFFFFF:
                return value
            if value:
                kernel32.VirtualFreeEx(process.handle, ctypes.c_void_p(value), 0, MEM_RELEASE)
    raise RuntimeError("无法在判罚代码附近分配探针内存")


def write_code(process, address: int, data: bytes) -> None:
    old = wintypes.DWORD(0)
    if not kernel32.VirtualProtectEx(
        process.handle, ctypes.c_void_p(address), len(data),
        PAGE_EXECUTE_READWRITE, ctypes.byref(old),
    ):
        raise RuntimeError("无法修改探针入口保护")
    try:
        write_process_memory(process, address, data)
        kernel32.FlushInstructionCache(process.handle, ctypes.c_void_p(address), len(data))
    finally:
        restored = wintypes.DWORD(0)
        kernel32.VirtualProtectEx(
            process.handle, ctypes.c_void_p(address), len(data), old.value, ctypes.byref(restored)
        )


def main(seconds: float = 30.0) -> int:
    pid, _ = select_process()
    hook = cave = 0
    original = patch = b""
    samples: dict[int, dict[str, object]] = {}
    with open_process(pid, write_memory=True) as process:
        module = find_module(process, GAME_PLUGIN)
        if not module:
            raise RuntimeError("game_plugin.dll 尚未加载")
        image = read_process_memory(process, module.base_address, module.size) or b""
        hits = _matches(image, REFEREE_PATTERN)
        if len(hits) != 1:
            raise RuntimeError(f"判罚探针特征码命中 {len(hits)} 处")
        hook = module.base_address + hits[0] + 13
        original = image[hits[0] + 13:hits[0] + 20]
        cave = allocate_near(process, hook)
        table = cave + 0x300
        # Execute the original movzx, then record only register values into our
        # own allocation. No participant pointer is dereferenced in game code.
        code = bytearray(original)
        code += b"\x9C\x52"                                      # pushfq; push rdx
        code += b"\x48\xBA" + struct.pack("<Q", table)           # mov rdx,table
        code += b"\x48\xFF\x02"                                 # inc qword [rdx]
        code += b"\x48\x89\x5A\x08"                            # [table+08]=rbx
        code += b"\x48\x89\x42\x10"                            # [table+10]=rax
        code += b"\x48\x89\x4A\x18"                            # [table+18]=rcx
        code += b"\x5A\x9D\xE9"                                # pop rdx; popfq; jmp return
        code += _rel32(cave + len(code) + 4, hook + 7)
        if len(code) >= 0x300:
            raise RuntimeError("探针代码区溢出")
        write_process_memory(process, cave, bytes(code))
        write_process_memory(process, table, b"\0" * 0x40)
        patch = b"\xE9" + _rel32(hook + 5, cave) + b"\x90\x90"
        write_code(process, hook, patch)
        started = time.monotonic(); last_count = -1
        try:
            while time.monotonic() - started < seconds:
                row = read_process_memory(process, table, 0x20)
                if row and len(row) == 0x20:
                    count, rbx, rax, rcx = struct.unpack("<QQQQ", row)
                    if count != last_count and rbx > 0xFFFFF:
                        block = read_process_memory(process, rbx + 0x280, 0x220)
                        samples[rbx] = {
                            "count": count, "rbx": hex(rbx), "rax": hex(rax), "rcx": hex(rcx),
                            "participant_288": hex(struct.unpack_from("<Q", block, 8)[0]) if block and len(block) >= 16 else None,
                            "participant_290": hex(struct.unpack_from("<Q", block, 16)[0]) if block and len(block) >= 24 else None,
                            "block_280_49f": block.hex() if block else None,
                        }
                        last_count = count
                time.sleep(0.02)
        finally:
            if read_process_memory(process, hook, 7) == patch:
                write_code(process, hook, original)
            time.sleep(0.1)
            kernel32.VirtualFreeEx(process.handle, ctypes.c_void_p(cave), 0, MEM_RELEASE)
    output = Path("data/research/club_structures") / f"referee_context_{datetime.now():%Y%m%d_%H%M%S}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps({"pid": pid, "samples": list(samples.values())}, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(output), "samples": len(samples)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
