from __future__ import annotations

import argparse
import json
import struct
import time

from fm_collector.win32 import find_module, open_process, read_process_memory
from tools.initial_data_audit import GAME_PLUGIN
from tools.redbull_hook import _matches
from tools.referee_live_learner import PATCHED_REFEREE_PATTERN


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only live referee event monitor")
    parser.add_argument("--pid", type=int, required=True)
    args = parser.parse_args()

    with open_process(args.pid) as process:
        module = find_module(process, GAME_PLUGIN)
        if not module:
            raise RuntimeError("game_plugin.dll is not loaded")
        image = read_process_memory(process, module.base_address, module.size) or b""
        hits = _matches(image, PATCHED_REFEREE_PATTERN)
        if len(hits) != 1:
            raise RuntimeError(f"patched referee hook hits: {len(hits)}")
        hook = module.base_address + hits[0] + 13
        patch = read_process_memory(process, hook, 7)
        if not patch or patch[0] != 0xE9:
            raise RuntimeError("referee hook patch is not installed")
        cave = hook + 5 + struct.unpack_from("<i", patch, 1)[0]
        table = cave + 0x800
        last_candidates = -1

        while read_process_memory(process, hook, 7) == patch:
            row = read_process_memory(process, table, 0x98)
            if not row or len(row) < 0x98:
                time.sleep(0.002)
                continue
            sequence, last_rbx, p288, p290, managed, red = struct.unpack_from("<QQQQQQ", row)
            candidates, skipped = struct.unpack_from("<QQ", row, 0x80)
            if candidates != last_candidates:
                last_candidates = candidates
                adjudication = struct.unpack_from("<II", row, 0x90)
                record = {
                    "captured_at": time.time(),
                    "sequence": sequence,
                    "candidate_count": candidates,
                    "skipped_no_foul": skipped,
                    "managed_actions": managed,
                    "red_actions": red,
                    "last_rbx": hex(last_rbx),
                    "p288": hex(p288),
                    "p290": hex(p290),
                    "adjudication": list(adjudication),
                }
                if last_rbx >= 0x100000:
                    block = read_process_memory(process, last_rbx, 0x580)
                    if block and len(block) >= 0x57C:
                        record["rbx_578"] = block[0x578]
                        record["rbx_57b"] = block[0x57B]
                print(json.dumps(record, ensure_ascii=False), flush=True)
            time.sleep(0.002)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
