from __future__ import annotations

import json
import struct
from datetime import datetime
from pathlib import Path

from fm_collector.win32 import MEM_PRIVATE, find_module, iter_readable_regions, open_process
from tools.club_reader import _contract, _find_human_manager
from tools.initial_data_audit import GAME_PLUGIN, Reader, select_process
from tools.preview_cup_odds import MANAGER_PERSON


ROOT = Path(__file__).resolve().parents[1]


def _manager_id() -> int:
    state = json.loads((ROOT / "data" / "save_registry.json").read_text(encoding="utf-8"))
    for key in state.get("manager_aliases", {}):
        if key.startswith("human-"):
            return int(key.removeprefix("human-"))
    raise RuntimeError("没有经理 ID")


def main() -> int:
    pid, _ = select_process()
    with open_process(pid) as process:
        module = find_module(process, GAME_PLUGIN)
        if not module:
            raise RuntimeError("game_plugin.dll 尚未加载")
        reader = Reader(process, module.base_address)
        manager = _find_human_manager(reader, _manager_id())
        team, _ = _contract(reader, manager + MANAGER_PERSON)
        roster = reader.roster(team or 0)[:8]
        needles = {int(row["address"], 16): row for row in roster}
        packed = {struct.pack("<Q", address): address for address in needles}
        hits = {str(row["id"]): [] for row in roster}
        all_regions = [region for region in iter_readable_regions(process) if region.type == MEM_PRIVATE]
        player_regions = {
            region.base_address for region in all_regions
            if any(region.base_address <= address < region.base_address + region.size for address in needles)
        }
        # FM's persistent database objects live in repeated ~16 MB slabs. Scanning
        # those plus the current player pool avoids rereading the entire 5 GB heap.
        regions = [
            region for region in all_regions
            if region.base_address in player_regions or region.size == 0xFCF000
        ]
        for region in regions:
            offset, carry = 0, b""
            while offset < region.size:
                size = min(8 * 1024 * 1024, region.size - offset)
                block = reader.bytes(region.base_address + offset, size)
                if not block:
                    carry = b""; offset += size; continue
                data = carry + block; base = region.base_address + offset - len(carry)
                for needle, player_address in packed.items():
                    cursor = 0
                    while len(hits[str(needles[player_address]["id"])]) < 300:
                        position = data.find(needle, cursor)
                        if position < 0:
                            break
                        address = base + position
                        context_address = max(region.base_address, address - 48)
                        context = reader.bytes(context_address, 104) or b""
                        hits[str(needles[player_address]["id"])].append({
                            "reference": hex(address), "region": hex(region.base_address),
                            "region_size": region.size, "context_address": hex(context_address),
                            "pointer_offset": address - context_address, "context_hex": context.hex(" "),
                        })
                        cursor = position + 1
                carry = data[-7:]
                offset += size
        payload = {
            "captured_at": datetime.now().isoformat(timespec="seconds"), "team": hex(team or 0),
            "players": [{"id": row["id"], "name": row["name"], "address": row["address"], "references": hits[str(row["id"])]} for row in roster],
        }
    output = ROOT / "data" / "research" / "club_structures" / f"roster_references_{datetime.now():%Y%m%d_%H%M%S}.json"
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(output)
    print(json.dumps({row["name"]: len(row["references"]) for row in payload["players"]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
