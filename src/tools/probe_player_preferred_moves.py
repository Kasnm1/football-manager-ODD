from __future__ import annotations

import argparse
import struct
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fm_collector.win32 import MEM_PRIVATE, iter_readable_regions, open_process
from tools.initial_data_audit import ENTITY_UID, Reader, select_process_layout
from tools.save_memory import find_vtables_for_rtti
from tools.preferred_moves import preferred_moves_from_mask, read_fm26_preferred_move_catalog


MAX_REGION_SIZE = 512 * 1024 * 1024
CHUNK_SIZE = 8 * 1024 * 1024


@dataclass(frozen=True)
class VectorCandidate:
    owner: str
    offset: int
    begin: int
    end: int
    capacity: int
    raw: bytes

    def values(self, width: int) -> tuple[int, ...]:
        if not self.raw or len(self.raw) % width:
            return ()
        code = {1: "B", 2: "H", 4: "I", 8: "Q"}[width]
        return struct.unpack(f"<{len(self.raw) // width}{code}", self.raw)


def _scan_player_objects(reader: Reader, player_ids: set[int]) -> dict[int, int]:
    needles = [
        (
            struct.pack("<Q", reader.module_base + rva),
            reader.layout.player_person_offset,
        )
        for rva in reader.layout.actual_player_vtable_rvas
    ]
    needles.extend(
        (
            struct.pack("<Q", reader.module_base + rva),
            reader.layout.player_and_non_player_person_offset,
        )
        for rva in reader.layout.player_and_non_player_vtable_rvas
    )
    found: dict[int, int] = {}
    for region in iter_readable_regions(reader.process):
        if region.type != MEM_PRIVATE or region.size > MAX_REGION_SIZE:
            continue
        offset = 0
        carry = b""
        while offset < region.size:
            length = min(CHUNK_SIZE, region.size - offset)
            block = reader.bytes(region.base_address + offset, length)
            if not block:
                carry = b""
                offset += length
                continue
            data = carry + block
            base = region.base_address + offset - len(carry)
            for needle, person_offset in needles:
                position = 0
                while True:
                    position = data.find(needle, position)
                    if position < 0:
                        break
                    player = base + position
                    player_id = reader.u32(player + person_offset + ENTITY_UID)
                    if player_id in player_ids:
                        found[int(player_id)] = player
                    position += 8
            if found.keys() >= player_ids:
                return found
            carry = data[-7:]
            offset += length
    return found


def _vector_candidates(
    reader: Reader, owner: str, address: int, size: int,
) -> list[VectorCandidate]:
    block = reader.bytes(address, size) or b""
    candidates = []
    for offset in range(0, max(0, len(block) - 23), 8):
        begin, end, capacity = struct.unpack_from("<QQQ", block, offset)
        if not begin or end < begin or capacity < end:
            continue
        used = end - begin
        allocated = capacity - begin
        if used > 4096 or allocated > 16384:
            continue
        raw = reader.bytes(begin, used) if used else b""
        if raw is None or len(raw) != used:
            continue
        candidates.append(VectorCandidate(owner, offset, begin, end, capacity, raw))
    return candidates


def _pair_candidates(
    reader: Reader, owner: str, address: int, size: int,
) -> list[VectorCandidate]:
    block = reader.bytes(address, size) or b""
    candidates = []
    for offset in range(0, max(0, len(block) - 15), 8):
        begin, end = struct.unpack_from("<QQ", block, offset)
        if not begin or end <= begin:
            continue
        used = end - begin
        if used > 4096:
            continue
        raw = reader.bytes(begin, used)
        if raw is None or len(raw) != used:
            continue
        candidates.append(VectorCandidate(owner, offset, begin, end, end, raw))
    return candidates


def _format_candidate(candidate: VectorCandidate) -> dict[str, object]:
    views = {}
    for width in (1, 2, 4, 8):
        values = candidate.values(width)
        if values and len(values) <= 32:
            views[f"u{width * 8}"] = values
    return {
        "owner": candidate.owner,
        "offset": hex(candidate.offset),
        "begin": hex(candidate.begin),
        "used_bytes": len(candidate.raw),
        "capacity_bytes": candidate.capacity - candidate.begin,
        "hex": candidate.raw[:128].hex(" "),
        "views": views,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Read-only FM26 preferred-move container probe",
    )
    parser.add_argument("player_ids", nargs="+", type=int)
    args = parser.parse_args()
    requested = set(args.player_ids)
    pid, _path, layout = select_process_layout()
    if layout.key != "fm26":
        raise RuntimeError("请选择正在运行的 FM26 进程")
    with open_process(pid) as process:
        module = layout.module(process)
        if not module:
            raise RuntimeError(f"{layout.module_name} 尚未加载")
        reader = Reader(process, module.base_address, layout)
        for rtti_name in (
            ".?AVPLAYER_PREFERRED_MOVE_FILTER_RULE@@",
            ".?AVOWN_PLAYER_LEARN_PPM@@",
            ".?AVOWN_PLAYER_UNLEARN_PPM@@",
            ".?AVIS_PLAYER_DOING_PPM_TRAINING@@",
        ):
            for vtable in find_vtables_for_rtti(reader, module, rtti_name):
                methods = []
                for index in range(32):
                    method = reader.ptr(vtable + index * 8)
                    if not method or not module.base_address <= method < module.base_address + module.size:
                        break
                    methods.append(hex(method - module.base_address))
                print("rtti", rtti_name, hex(vtable - module.base_address), methods)
        players = _scan_player_objects(reader, requested)
        print("players", {key: hex(value) for key, value in players.items()})
        masks = {
            player_id: struct.unpack("<Q", reader.bytes(player + 0x348, 8) or b"\0" * 8)[0]
            for player_id, player in players.items()
        }
        catalog_path = Path(_path).parent / "data" / "game_simulation" / "languages.fmf"
        catalog = read_fm26_preferred_move_catalog(catalog_path)
        print("preferred_move_masks", {
            player_id: {
                "value": hex(mask),
                "bits": [bit for bit in range(64) if mask & (1 << bit)],
                "moves": preferred_moves_from_mask(mask, catalog),
            }
            for player_id, mask in masks.items()
        })
        missing = requested - players.keys()
        if missing:
            print("missing", sorted(missing))

        all_candidates: dict[int, dict[tuple[str, int], VectorCandidate]] = {}
        for player_id, player in players.items():
            rows = [
                *_vector_candidates(reader, "player", player, 0x1000),
                *_pair_candidates(reader, "player_pair", player, 0x1000),
            ]
            all_candidates[player_id] = {(row.owner, row.offset): row for row in rows}

        ordered_players = [players[value] for value in args.player_ids if value in players]
        if len(ordered_players) >= 2:
            fixed_blocks = [reader.bytes(player, 0x600) or b"" for player in ordered_players]
            for offset in range(0, min(map(len, fixed_blocks)) - 7):
                values = [struct.unpack_from("<Q", block, offset)[0] for block in fixed_blocks]
                if (
                    all(value.bit_count() == 2 for value in values)
                    and values[0] != values[1]
                    and (values[0] & values[1]).bit_count() == 1
                ):
                    print("bitmask_candidate", {
                        "offset": hex(offset),
                        "values": [hex(value) for value in values],
                        "bits": [
                            [bit for bit in range(64) if value & (1 << bit)]
                            for value in values
                        ],
                    })

        common_keys = set.intersection(
            *(set(rows) for rows in all_candidates.values())
        ) if all_candidates else set()
        for key in sorted(common_keys):
            rows = [all_candidates[player_id][key] for player_id in args.player_ids if player_id in all_candidates]
            if len(rows) < 2 or all(row.raw == rows[0].raw for row in rows[1:]):
                continue
            print("candidate", key)
            for player_id, row in zip((value for value in args.player_ids if value in all_candidates), rows):
                print(player_id, _format_candidate(row))


if __name__ == "__main__":
    main()
