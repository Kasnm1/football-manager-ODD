from __future__ import annotations

import argparse
import struct
import time

from fm_collector.win32 import find_module, open_process, read_process_memory, write_process_memory
from tools.initial_data_audit import GAME_PLUGIN
from tools.redbull_hook import _matches


PATCHED_REFEREE_PATTERN = (
    0x0F, 0x83, None, None, None, None,
    0x48, 0x8B, 0x86, None, None, 0x00, 0x00,
    0xE9, None, None, None, None, 0x90, 0x90,
)
TABLE_OFFSET = 0x800
MAX_PARTICIPANTS = 8


def main() -> int:
    parser = argparse.ArgumentParser(description="Repair the live referee participant learner")
    parser.add_argument("--pid", type=int, required=True)
    parser.add_argument("--team", type=lambda value: int(value, 0), required=True)
    args = parser.parse_args()

    learned: list[int] = []
    last_sequence = -1
    with open_process(args.pid, write_memory=True) as process:
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
            raise RuntimeError("referee hook patch is no longer installed")
        cave = hook + 5 + struct.unpack_from("<i", patch, 1)[0]
        table = cave + TABLE_OFFSET

        while True:
            current_patch = read_process_memory(process, hook, 7)
            if current_patch != patch:
                break
            row = read_process_memory(process, table, 0x98)
            if not row or len(row) < 0x98:
                time.sleep(0.02)
                continue
            sequence, _, participant_288, participant_290 = struct.unpack_from("<QQQQ", row)
            if sequence != last_sequence:
                last_sequence = sequence
                for participant in (participant_288, participant_290):
                    if participant < 0x100000 or participant in learned:
                        continue
                    block = read_process_memory(process, participant, 0xB0)
                    if not block or len(block) < 0xB0:
                        continue
                    if struct.unpack_from("<Q", block, 0xA8)[0] != args.team:
                        continue
                    learned.append(participant)
                    learned = learned[-MAX_PARTICIPANTS:]
                    slots = b"".join(struct.pack("<Q", value) for value in learned)
                    slots += b"\0" * (MAX_PARTICIPANTS * 8 - len(slots))
                    write_process_memory(process, table + 0x40, slots)
            time.sleep(0.02)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
