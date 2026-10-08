from __future__ import annotations

import argparse
import json
import struct
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fm_collector.win32 import find_module, find_processes, open_process
from tools.initial_data_audit import (
    GAME_PLUGIN,
    Reader,
    decode_date,
    parse_completed_result,
    scan_result_addresses,
)


XG_SHOT_DATA_VTABLE_RVA = 0x45FA0D8


def is_pointer(value: int) -> bool:
    return 0x10000 <= value <= 0x7FFFFFFFFFFF


def vector_at(reader: Reader, address: int) -> dict | None:
    raw = reader.bytes(address, 24)
    if not raw or len(raw) != 24:
        return None
    begin, end, capacity = struct.unpack("<QQQ", raw)
    if not (is_pointer(begin) and begin <= end <= capacity):
        return None
    size = end - begin
    if size > 0x100000 or capacity - begin > 0x200000:
        return None
    return {"field": hex(address), "begin": begin, "end": end, "size": size}


def find_sequences(data: bytes, pairs: list[tuple[int, ...]]) -> list[dict]:
    hits = []
    for values in pairs:
        for fmt, label in (("<" + "H" * len(values), "u16"), ("<" + "I" * len(values), "u32")):
            needle = struct.pack(fmt, *values)
            start = 0
            while True:
                offset = data.find(needle, start)
                if offset < 0:
                    break
                hits.append({"encoding": label, "values": values, "offset": offset})
                start = offset + 1
    return hits


def scan_object_vtables(reader: Reader, vtable: int) -> list[int]:
    from fm_collector.win32 import MEM_PRIVATE, iter_readable_regions

    needle = struct.pack("<Q", vtable)
    found = []
    for region in iter_readable_regions(reader.process):
        if region.type != MEM_PRIVATE or region.size > 128 * 1024 * 1024:
            continue
        data = reader.bytes(region.base_address, region.size)
        if not data:
            continue
        offset = 0
        while True:
            offset = data.find(needle, offset)
            if offset < 0:
                break
            found.append(region.base_address + offset)
            offset += 1
    return found


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--home", type=int, required=True)
    parser.add_argument("--away", type=int, required=True)
    parser.add_argument("--date", required=True)
    parser.add_argument("--known", action="append", default=[])
    args = parser.parse_args()

    processes = find_processes("fm.exe")
    if not processes:
        raise SystemExit("fm.exe not found")
    pid = processes[0].pid
    process = open_process(pid)
    try:
        module = find_module(process, GAME_PLUGIN)
        if not module:
            raise SystemExit("game_plugin.dll not found")
        reader = Reader(process, module.base_address)
        target = None
        for address in scan_result_addresses(reader)[0]:
            parsed = parse_completed_result(reader, address)
            if not parsed:
                continue
            if (
                parsed["date"] == args.date
                and parsed["home_team"]["id"] == args.home
                and parsed["away_team"]["id"] == args.away
            ):
                target = (address, parsed)
                break
        if not target:
            raise SystemExit("target result not found")

        result_address, parsed = target
        root = reader.ptr(result_address + 0x78)
        sequences = [tuple(int(item) for item in value.split(",")) for value in args.known]
        vectors = []
        if root:
            for offset in range(0, 0x1000, 8):
                vector = vector_at(reader, root + offset)
                if not vector or vector["size"] == 0:
                    continue
                data = reader.bytes(vector["begin"], vector["size"])
                if not data or len(data) != vector["size"]:
                    continue
                hits = find_sequences(data, sequences)
                vectors.append({
                    **{key: hex(value) if key in {"begin", "end"} else value for key, value in vector.items()},
                    "root_offset": hex(offset),
                    "head": data[:64].hex(" "),
                    "known_hits": hits,
                })

        xg_objects = []
        for address in scan_object_vtables(reader, module.base_address + XG_SHOT_DATA_VTABLE_RVA):
            raw = reader.bytes(address, 0x100)
            if not raw:
                continue
            pointers = []
            for offset in range(8, len(raw) - 7, 8):
                value = struct.unpack_from("<Q", raw, offset)[0]
                if value in {result_address, root}:
                    pointers.append({"offset": hex(offset), "target": hex(value)})
            floats = []
            for offset in range(8, len(raw) - 3, 4):
                value = struct.unpack_from("<f", raw, offset)[0]
                if 0.001 <= value <= 1.0:
                    floats.append({"offset": hex(offset), "value": round(value, 6)})
            xg_objects.append({
                "address": hex(address),
                "direct_match_pointers": pointers,
                "candidate_floats": floats[:20],
                "raw": raw.hex(" "),
            })

        output = {
            "pid": pid,
            "module_base": hex(module.base_address),
            "result": parsed,
            "result_address": hex(result_address),
            "detail_root": hex(root) if root else None,
            "vectors": vectors,
            "xg_shot_data": xg_objects,
        }
        print(json.dumps(output, ensure_ascii=False, indent=2))
    finally:
        process.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
