from __future__ import annotations

import argparse
import bisect
import struct
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fm_collector.win32 import open_process, read_process_memory, write_process_memory
from tools.game_layout import FM26_LAYOUT
from tools.initial_data_audit import ENTITY_UID


RETIREMENT_OWNER_RVA = 0x4E3ACE0
RETIREMENT_VECTOR_OFFSET = 0x90
RETIREMENT_DATE_OFFSETS = (0x0C, 0x24)
CHECK_DATE_OFFSETS = (0x10, 0x28)


def _unpack_date(value: int) -> date:
    year = value >> 16
    day = value & 0x1FF
    return date.fromordinal(date(year, 1, 1).toordinal() + day - 1)


def _replace_date(value: int, target: date) -> int:
    flags = value & 0x0000FE00
    day = target.timetuple().tm_yday
    return (target.year << 16) | flags | day


def _person_address(process, module_base: int, player: int) -> int:
    vtable_raw = read_process_memory(process, player, 8)
    if not vtable_raw or len(vtable_raw) != 8:
        raise RuntimeError("无法读取球员对象")
    vtable_rva = struct.unpack("<Q", vtable_raw)[0] - module_base
    if vtable_rva in FM26_LAYOUT.actual_player_vtable_rvas:
        return player + FM26_LAYOUT.player_person_offset
    if vtable_rva in FM26_LAYOUT.player_and_non_player_vtable_rvas:
        return player + FM26_LAYOUT.player_and_non_player_person_offset
    raise RuntimeError("球员对象类型校验失败")


def main() -> None:
    parser = argparse.ArgumentParser(description="安全修改FM26球员真实退役日期及检查日期")
    parser.add_argument("pid", type=int)
    parser.add_argument("player_id", type=int)
    parser.add_argument("player_address", type=lambda value: int(value, 0))
    parser.add_argument("retirement_date", type=date.fromisoformat)
    parser.add_argument("check_date", type=date.fromisoformat)
    args = parser.parse_args()

    with open_process(args.pid, write_memory=True) as process:
        module = FM26_LAYOUT.module(process)
        if not module:
            raise RuntimeError("game_plugin.dll 尚未加载")
        person = _person_address(process, module.base_address, args.player_address)
        uid_raw = read_process_memory(process, person + ENTITY_UID, 4)
        key_raw = read_process_memory(process, person + 8, 4)
        if not uid_raw or not key_raw:
            raise RuntimeError("无法读取球员身份")
        uid = struct.unpack("<I", uid_raw)[0]
        internal_key = struct.unpack("<I", key_raw)[0]
        if uid != args.player_id:
            raise RuntimeError(f"球员UID不匹配：读取到 {uid}")

        owner_raw = read_process_memory(
            process, module.base_address + RETIREMENT_OWNER_RVA, 8,
        )
        if not owner_raw:
            raise RuntimeError("无法读取退役记录管理器")
        owner = struct.unpack("<Q", owner_raw)[0]
        vector_raw = read_process_memory(
            process, owner + RETIREMENT_VECTOR_OFFSET, 24,
        )
        if not vector_raw:
            raise RuntimeError("无法读取退役记录索引")
        begin, end, capacity = struct.unpack("<QQQ", vector_raw)
        if not begin <= end <= capacity or (end - begin) % 16:
            raise RuntimeError("退役记录索引结构校验失败")
        rows_raw = read_process_memory(process, begin, end - begin) or b""
        rows = [
            struct.unpack_from("<IxxxxQ", rows_raw, offset)
            for offset in range(0, len(rows_raw), 16)
        ]
        keys = [row[0] for row in rows]
        index = bisect.bisect_left(keys, internal_key)
        if index >= len(rows) or rows[index][0] != internal_key or not rows[index][1]:
            raise RuntimeError("没有找到该球员的退役记录")
        record = rows[index][1]

        offsets = (*RETIREMENT_DATE_OFFSETS, *CHECK_DATE_OFFSETS)
        originals: dict[int, bytes] = {}
        for offset in offsets:
            raw = read_process_memory(process, record + offset, 4)
            if not raw or len(raw) != 4:
                raise RuntimeError("退役日期记录读取不完整")
            originals[offset] = raw
        retirement_dates = {
            _unpack_date(struct.unpack("<I", originals[offset])[0])
            for offset in RETIREMENT_DATE_OFFSETS
        }
        if len(retirement_dates) != 1:
            raise RuntimeError("两份真实退役日期镜像不一致，已拒绝写入")

        replacements = {
            offset: struct.pack(
                "<I", _replace_date(struct.unpack("<I", originals[offset])[0], target),
            )
            for target, target_offsets in (
                (args.retirement_date, RETIREMENT_DATE_OFFSETS),
                (args.check_date, CHECK_DATE_OFFSETS),
            )
            for offset in target_offsets
        }
        try:
            for offset in offsets:
                write_process_memory(process, record + offset, replacements[offset])
            for offset in offsets:
                if read_process_memory(process, record + offset, 4) != replacements[offset]:
                    raise RuntimeError("日期写入回读校验失败")
        except Exception:
            for offset, raw in originals.items():
                write_process_memory(process, record + offset, raw)
            raise

        print({
            "player_id": uid,
            "internal_key": internal_key,
            "record": hex(record),
            "old_retirement_date": next(iter(retirement_dates)).isoformat(),
            "retirement_date": args.retirement_date.isoformat(),
            "check_date": args.check_date.isoformat(),
        })


if __name__ == "__main__":
    main()
