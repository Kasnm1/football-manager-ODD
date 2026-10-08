from __future__ import annotations

import struct
import sys
import re
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fm_collector.win32 import MEM_PRIVATE, find_module, iter_readable_regions, open_process
from tools.initial_data_audit import GAME_PLUGIN, Reader, select_process


EXPECTED = {0x2B: 7, 0x2D: 14, 0x32: 7, 0x33: 3, 0x34: 18, 0x3B: 20}


def main() -> None:
    pid, _ = select_process()
    with open_process(pid) as process:
        module = find_module(process, GAME_PLUGIN)
        if not module:
            raise RuntimeError("game_plugin.dll not loaded")
        reader = Reader(process, module.base_address)
        matches: list[int] = []
        pattern = re.compile(
            b"[\x1f-\x23].[\x42-\x46].{4}[\x1f-\x23][\x0b-\x0f][\x56-\x5a].{6}[\x60-\x64]",
            re.DOTALL,
        )
        for region in iter_readable_regions(process):
            if region.type != MEM_PRIVATE or region.size > 512 * 1024 * 1024:
                continue
            offset = 0
            while offset < region.size:
                length = min(8 * 1024 * 1024, region.size - offset)
                data = reader.bytes(region.base_address + offset, length)
                if data and len(data) > 0x44:
                    for match in pattern.finditer(data):
                        matches.append(region.base_address + offset + match.start() - 0x2B)
                        if len(matches) >= 100:
                            break
                if len(matches) >= 100:
                    break
                offset += length
            if len(matches) >= 100:
                break
        print("direct_matches", len(matches), [hex(value) for value in matches[:100]])
        for address in matches[:100]:
            block = reader.bytes(address + 0x10, 0x44) or b""
            print(hex(address), list(block[0x1B:0x34]))


if __name__ == "__main__":
    main()
