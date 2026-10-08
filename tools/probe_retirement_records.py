from __future__ import annotations

import argparse
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fm_collector.win32 import (
    MEM_PRIVATE, iter_readable_regions, open_process, read_process_memory,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="只读查找FM退役状态表候选记录")
    parser.add_argument("pid", type=int)
    parser.add_argument("player_ids", nargs="+", type=int)
    args = parser.parse_args()
    needles = {uid: struct.pack("<I", uid) for uid in set(args.player_ids)}
    candidates: dict[int, list[dict[str, object]]] = {uid: [] for uid in needles}
    with open_process(args.pid) as process:
        for region in iter_readable_regions(process):
            if region.type != MEM_PRIVATE or region.size > 512 * 1024 * 1024:
                continue
            carry = b""
            for offset in range(0, region.size, 8 * 1024 * 1024):
                block = read_process_memory(
                    process, region.base_address + offset,
                    min(8 * 1024 * 1024, region.size - offset),
                ) or b""
                data = carry + block
                base = region.base_address + offset - len(carry)
                for uid, needle in needles.items():
                    position = 0
                    while True:
                        position = data.find(needle, position)
                        if position < 0:
                            break
                        entry_address = base + position
                        entry = read_process_memory(process, entry_address, 16) or b""
                        if len(entry) == 16:
                            record_address = struct.unpack_from("<Q", entry, 8)[0]
                            record = (
                                read_process_memory(process, record_address, 0x20)
                                if 0x100000 < record_address < 0x800000000000 else None
                            )
                            if record and len(record) == 0x20:
                                day, year, state = struct.unpack_from("<HHH", record, 0x10)
                                if 1800 <= year <= 2300 and day & 0x1FF <= 366:
                                    neighbors = read_process_memory(
                                        process, entry_address - 16, 48,
                                    ) or b""
                                    candidates[uid].append({
                                        "entry": hex(entry_address),
                                        "record": hex(record_address),
                                        "day": day & 0x1FF,
                                        "year": year,
                                        "state": hex(state),
                                        "neighbors": neighbors.hex(),
                                    })
                        position += 1
    for uid in args.player_ids:
        print(uid, candidates.get(uid, []))


if __name__ == "__main__":
    main()
