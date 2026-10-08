from __future__ import annotations

import argparse
import json
import struct
from datetime import datetime
from pathlib import Path

from fm_collector.win32 import MEM_PRIVATE, iter_readable_regions, open_process, read_process_memory
from tools.initial_data_audit import select_process


OUTPUT = Path(__file__).resolve().parents[1] / "data" / "research" / "club_finances"


def scan_u32(value: int) -> list[int]:
    needle = struct.pack("<I", value)
    pid, _path = select_process()
    addresses: list[int] = []
    with open_process(pid) as process:
        for region in iter_readable_regions(process):
            if region.type != MEM_PRIVATE or region.size > 512 * 1024 * 1024:
                continue
            offset, carry = 0, b""
            while offset < region.size:
                length = min(8 * 1024 * 1024, region.size - offset)
                block = read_process_memory(process, region.base_address + offset, length)
                if block:
                    data = carry + block
                    base = region.base_address + offset - len(carry)
                    cursor = 0
                    while True:
                        cursor = data.find(needle, cursor)
                        if cursor < 0:
                            break
                        address = base + cursor
                        if address % 4 == 0:
                            addresses.append(address)
                        cursor += 1
                    carry = data[-3:]
                offset += length
    return addresses


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("value", type=int)
    args = parser.parse_args()
    addresses = scan_u32(args.value)
    OUTPUT.mkdir(parents=True, exist_ok=True)
    path = OUTPUT / f"money_u32_{args.value}_{datetime.now():%Y%m%d_%H%M%S}.json"
    path.write_text(json.dumps({
        "captured_at": datetime.now().isoformat(timespec="seconds"),
        "value": args.value,
        "addresses": [hex(address) for address in addresses],
    }, indent=2), encoding="utf-8")
    print(path)
    print("candidates", len(addresses))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
