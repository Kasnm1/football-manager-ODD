from __future__ import annotations

import argparse
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fm_collector.win32 import MEM_PRIVATE, find_module, iter_readable_regions, open_process
from tools.initial_data_audit import GAME_PLUGIN, Reader, select_process
from tools.save_memory import find_vtables_for_rtti


PERSON_BASE = 0x288
ENTITY_UID = 0x0C
STAFF_POINTER = 0x280
KNOWN_OFFSETS = {
    "门将训练": 0x2B,
    "判断球员能力": 0x2C,
    "判断球员潜力": 0x2D,
    "进攻训练": 0x32,
    "防守训练": 0x33,
    "体能训练": 0x34,
    "适应性": 0x3B,
}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("player_id", type=int)
    parser.add_argument("--person-address", type=lambda value: int(value, 0))
    args = parser.parse_args()
    pid, _path = select_process()
    needle = struct.pack("<I", args.player_id)
    with open_process(pid) as process:
        module = find_module(process, GAME_PLUGIN)
        if not module:
            raise RuntimeError("game_plugin.dll not loaded")
        reader = Reader(process, module.base_address)
        vtable_rows = []
        for rtti_name in (
            ".?AVACTUAL_NON_PLAYER@db@@",
            ".?AVACTUAL_PLAYER_AND_NON_PLAYER@db@@",
            ".?AVEXTERN_ACTUAL_NON_PLAYER@db@@",
        ):
            for value in find_vtables_for_rtti(reader, module, rtti_name):
                vtable_rows.append((rtti_name, value, struct.pack("<Q", value)))
        print("candidate_vtables", [(name, hex(value)) for name, value, _ in vtable_rows])
        for region in iter_readable_regions(process):
            if region.type != MEM_PRIVATE or region.size > 512 * 1024 * 1024:
                continue
            block = reader.bytes(region.base_address, region.size)
            if not block:
                continue
            for rtti_name, vtable, vtable_needle in vtable_rows:
                position = 0
                while True:
                    position = block.find(vtable_needle, position)
                    if position < 0:
                        break
                    actual = region.base_address + position
                    person_offsets = (0x288, 0x380)
                    for person_offset in person_offsets:
                        person = actual + person_offset
                        if reader.u32(person + ENTITY_UID) != args.player_id:
                            continue
                        staff = reader.ptr(actual + STAFF_POINTER)
                        print("actual_target", {
                            "rtti": rtti_name, "person_offset": hex(person_offset),
                            "actual": hex(actual), "person": hex(person),
                            "staff": hex(staff or 0),
                            "contract": hex(reader.ptr(actual + 0x330) or 0),
                            "job_preferences": {
                                "manager": reader.u8(actual + 0x358),
                                "assistant_manager": reader.u8(actual + 0x359),
                                "coach": reader.u8(actual + 0x35A),
                                "physio": reader.u8(actual + 0x35B),
                                "scout": reader.u8(actual + 0x35C),
                                "goalkeeping_coach": reader.u8(actual + 0x35D),
                                "fitness_coach": reader.u8(actual + 0x35E),
                                "set_piece_coach": reader.u8(actual + 0x35F),
                            },
                        })
                        if staff:
                            for attrs_base in (0, 0x10):
                                raw = {name: reader.u8(staff + attrs_base + offset) for name, offset in KNOWN_OFFSETS.items()}
                                print("staff_values", hex(attrs_base), raw)
                    position += 8
        hits = []
        for region in iter_readable_regions(process):
            if region.type != MEM_PRIVATE or region.size > 512 * 1024 * 1024:
                continue
            offset = 0
            carry = b""
            while offset < region.size:
                length = min(4 * 1024 * 1024, region.size - offset)
                block = reader.bytes(region.base_address + offset, length)
                if not block:
                    carry = b""
                    offset += length
                    continue
                data = carry + block
                base = region.base_address + offset - len(carry)
                position = 0
                while True:
                    position = data.find(needle, position)
                    if position < 0:
                        break
                    hits.append(base + position)
                    position += 1
                carry = data[-3:]
                offset += length
        if args.person_address:
            hits = [args.person_address + ENTITY_UID]
        print(f"uid_hits={len(hits)}")
        expected = {
            "门将训练": 7,
            "判断球员潜力": 14,
            "进攻训练": 7,
            "防守训练": 3,
            "体能训练": 18,
            "适应性": 20,
        }
        for hit in hits:
            for person_offset in range(0x100, 0x501, 8):
                outer = hit - ENTITY_UID - person_offset
                outer_vtable = reader.ptr(outer)
                if not outer_vtable or not (
                    module.base_address <= outer_vtable < module.base_address + module.size
                ):
                    continue
                outer_staff = reader.ptr(outer + STAFF_POINTER)
                if not outer_staff:
                    continue
                for outer_attrs_base in (0, 0x10):
                    outer_raw = {
                        name: reader.u8(outer_staff + outer_attrs_base + offset)
                        for name, offset in KNOWN_OFFSETS.items()
                    }
                    if any(value is None or value > 100 for value in outer_raw.values()):
                        continue
                    variants = (
                        ("direct", outer_raw),
                        ("scaled", {
                            name: max(1, min(20, (int(value) + 4) // 5))
                            for name, value in outer_raw.items()
                        }),
                    )
                    for conversion_name, outer_display in variants:
                        outer_score = sum(
                            outer_display.get(name) == value
                            for name, value in expected.items()
                        )
                        if outer_score >= 4:
                            print("offset_search", {
                                "score": outer_score, "conversion": conversion_name,
                                "person_offset": hex(person_offset),
                                "attrs_base": hex(outer_attrs_base),
                                "actual": hex(outer), "person": hex(hit - ENTITY_UID),
                                "vtable": hex(outer_vtable), "staff": hex(outer_staff),
                                "raw": outer_raw, "display": outer_display,
                            })
        for hit in hits:
            actual = hit - PERSON_BASE - ENTITY_UID
            staff = reader.ptr(actual + STAFF_POINTER)
            if not staff:
                continue
            for attrs_base in (0, 0x10):
                raw = {
                    name: reader.u8(staff + attrs_base + offset)
                    for name, offset in KNOWN_OFFSETS.items()
                }
                if any(value is None or value > 100 for value in raw.values()):
                    continue
                display = {
                    name: max(1, min(20, (int(value) + 4) // 5))
                    for name, value in raw.items()
                }
                score = sum(display.get(name) == value for name, value in expected.items())
                if score >= 3 or args.person_address:
                    print({
                        "score": score,
                        "attrs_base": hex(attrs_base),
                        "uid_address": hex(hit),
                        "actual_non_player": hex(actual),
                        "vtable": hex(reader.ptr(actual) or 0),
                        "staff": hex(staff),
                        "raw": raw,
                        "fm_display": display,
                    })


if __name__ == "__main__":
    main()
