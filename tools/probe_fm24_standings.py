from __future__ import annotations

import argparse
import json
import re
import struct
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fm_collector.win32 import MEM_PRIVATE, iter_readable_regions, open_process
from tools.game_layout import FM24_LAYOUT
from tools.initial_data_audit import ENTITY_UID, Reader, select_process_layout


BLOCK_BYTES = 8 * 1024 * 1024
J1_RANKED_TEAM_IDS = (
    1190, 107280, 1198, 1184, 1185, 1194, 1186, 106844, 107285,
    1188, 1189, 1195, 107309, 1196, 107301, 107313, 788904, 788854,
)
J1_STATS = {
    "played": (28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28),
    "wins": (24, 18, 15, 14, 13, 11, 11, 11, 12, 10, 10, 9, 9, 7, 6, 5, 3, 3),
    "draws": (3, 4, 8, 4, 6, 9, 8, 8, 5, 7, 6, 7, 6, 10, 7, 7, 11, 6),
    "losses": (1, 6, 5, 10, 9, 8, 9, 9, 11, 11, 12, 12, 13, 11, 15, 16, 14, 19),
    "goals_for": (105, 59, 58, 51, 46, 43, 30, 48, 34, 45, 56, 40, 36, 37, 31, 33, 29, 29),
    "goals_against": (31, 36, 37, 39, 46, 45, 27, 42, 37, 55, 58, 47, 46, 43, 49, 63, 42, 67),
    "goal_difference": (74, 23, 21, 12, 0, -2, 3, 6, -3, -10, -2, -7, -10, -6, -18, -30, -13, -38),
    "points": (75, 58, 53, 46, 45, 42, 41, 41, 41, 37, 36, 34, 33, 31, 25, 22, 20, 15),
}


def find_team(reader: Reader, team_id: int) -> tuple[int, Any] | tuple[None, None]:
    needle = struct.pack("<I", team_id)
    expected_vtable = reader.module_base + reader.layout.team_vtable_rva
    for region in iter_readable_regions(reader.process):
        if region.type != MEM_PRIVATE:
            continue
        carry = b""
        for offset in range(0, region.size, BLOCK_BYTES):
            raw = reader.bytes(
                region.base_address + offset,
                min(BLOCK_BYTES, region.size - offset),
            )
            if not raw:
                carry = b""
                continue
            data = carry + raw
            origin = region.base_address + offset - len(carry)
            start = 0
            while True:
                found = data.find(needle, start)
                if found < 0:
                    break
                uid_address = origin + found
                address = uid_address - ENTITY_UID
                if reader.ptr(address) == expected_vtable:
                    team = reader.team(address)
                    if team and int(team["id"]) == team_id:
                        return address, region
                start = found + 1
            carry = data[-3:]
    return None, None


def team_slab(reader: Reader, region: Any) -> list[dict[str, Any]]:
    needle = struct.pack(
        "<Q", reader.module_base + reader.layout.team_vtable_rva,
    )
    rows = []
    carry = b""
    for offset in range(0, region.size, BLOCK_BYTES):
        raw = reader.bytes(
            region.base_address + offset,
            min(BLOCK_BYTES, region.size - offset),
        )
        if not raw:
            carry = b""
            continue
        data = carry + raw
        origin = region.base_address + offset - len(carry)
        start = 0
        while True:
            found = data.find(needle, start)
            if found < 0:
                break
            address = origin + found
            team = reader.team(address)
            if team:
                competition = team.get("competition") or {}
                rows.append({
                    "id": int(team["id"]),
                    "name": team.get("short_name") or team.get("name"),
                    "address": hex(address),
                    "competition_id": competition.get("id"),
                    "competition_name": competition.get("short_name") or competition.get("name"),
                })
            start = found + 8
        carry = data[-7:]
    return rows


def reference_contexts(
    reader: Reader, team_address: int, expected: list[int], limit: int = 128,
) -> list[dict[str, Any]]:
    needle = struct.pack("<Q", team_address)
    hits = []
    for region in iter_readable_regions(reader.process):
        if region.type != MEM_PRIVATE:
            continue
        carry = b""
        for offset in range(0, region.size, BLOCK_BYTES):
            raw = reader.bytes(
                region.base_address + offset,
                min(BLOCK_BYTES, region.size - offset),
            )
            if not raw:
                carry = b""
                continue
            data = carry + raw
            origin = region.base_address + offset - len(carry)
            start = 0
            while True:
                found = data.find(needle, start)
                if found < 0:
                    break
                address = origin + found
                context_start = max(region.base_address, address - 0x100)
                context = reader.bytes(context_start, 0x208) or b""
                offsets: dict[str, list[str]] = {}
                for value in expected:
                    found_offsets = set()
                    for label, packed in (
                        ("u8", struct.pack("<B", value & 0xFF)),
                        ("i16", struct.pack("<h", value)),
                        ("i32", struct.pack("<i", value)),
                    ):
                        position = 0
                        while True:
                            position = context.find(packed, position)
                            if position < 0:
                                break
                            found_offsets.add(f"{label}:{position - (address - context_start):+#x}")
                            position += 1
                    offsets[str(value)] = sorted(found_offsets)
                score = sum(bool(values) for values in offsets.values())
                if score >= 4:
                    hits.append({
                        "reference_address": hex(address),
                        "region": hex(region.base_address),
                        "region_size": region.size,
                        "score": score,
                        "value_offsets": offsets,
                        "context_address": hex(context_start),
                        "context_hex": context.hex(),
                    })
                    if len(hits) >= limit:
                        return sorted(hits, key=lambda item: item["score"], reverse=True)
                start = found + 1
            carry = data[-7:]
    return sorted(hits, key=lambda item: item["score"], reverse=True)


def ranked_table_candidates(
    reader: Reader, ranked_team_addresses: list[int], limit: int = 32,
) -> list[dict[str, Any]]:
    first = struct.pack("<Q", ranked_team_addresses[0])
    candidates = []
    for region in iter_readable_regions(reader.process):
        if region.type != MEM_PRIVATE:
            continue
        carry = b""
        for offset in range(0, region.size, BLOCK_BYTES):
            raw = reader.bytes(
                region.base_address + offset,
                min(BLOCK_BYTES, region.size - offset),
            )
            if not raw:
                carry = b""
                continue
            data = carry + raw
            origin = region.base_address + offset - len(carry)
            start = 0
            while True:
                found = data.find(first, start)
                if found < 0:
                    break
                first_address = origin + found
                for stride in range(8, 0x401, 8):
                    matched = 1
                    for index, team_address in enumerate(ranked_team_addresses[1:], 1):
                        if reader.ptr(first_address + index * stride) != team_address:
                            break
                        matched += 1
                    if matched >= 3:
                        row_size = min(max(stride, 0x40), 0x400)
                        row = reader.bytes(first_address, row_size) or b""
                        candidates.append({
                            "first_team_pointer": hex(first_address),
                            "region": hex(region.base_address),
                            "region_size": region.size,
                            "stride": stride,
                            "matched_rows": matched,
                            "first_row_hex": row.hex(),
                        })
                        if matched == len(ranked_team_addresses) or len(candidates) >= limit:
                            return sorted(
                                candidates,
                                key=lambda item: item["matched_rows"], reverse=True,
                            )
                start = found + 1
            carry = data[-7:]
    return sorted(candidates, key=lambda item: item["matched_rows"], reverse=True)


def _matching_fields(blocks: list[bytes]) -> dict[str, list[dict[str, Any]]]:
    matches: dict[str, list[dict[str, Any]]] = {name: [] for name in J1_STATS}
    if not blocks or any(not block for block in blocks):
        return matches
    minimum = min(len(block) for block in blocks)
    formats = (("u8", "<B", 1), ("i8", "<b", 1), ("u16", "<H", 2),
               ("i16", "<h", 2), ("u32", "<I", 4), ("i32", "<i", 4))
    for label, fmt, width in formats:
        for offset in range(0, minimum - width + 1):
            values = tuple(struct.unpack_from(fmt, block, offset)[0] for block in blocks)
            for name, expected in J1_STATS.items():
                if values == expected:
                    matches[name].append({"encoding": label, "offset": hex(offset)})
    return matches


def analyze_ranked_rows(
    reader: Reader, first_team_pointer: int, stride: int,
) -> dict[str, Any]:
    rows = [reader.bytes(first_team_pointer + index * stride, stride) or b""
            for index in range(len(J1_RANKED_TEAM_IDS))]
    result: dict[str, Any] = {"inline": _matching_fields(rows), "pointers": []}
    for pointer_offset in range(0, stride - 7, 8):
        pointers = [
            struct.unpack_from("<Q", row, pointer_offset)[0] if len(row) >= pointer_offset + 8 else 0
            for row in rows
        ]
        if any(pointer < 0x10000 for pointer in pointers):
            continue
        blocks = [reader.bytes(pointer, 0x400) or b"" for pointer in pointers]
        matches = _matching_fields(blocks)
        if any(matches.values()):
            result["pointers"].append({
                "row_pointer_offset": hex(pointer_offset),
                "targets": [hex(pointer) for pointer in pointers],
                "matches": matches,
            })
    return result


def standings_root_candidates(
    reader: Reader, first_row: int, row_count: int, stride: int,
) -> list[dict[str, Any]]:
    needle = struct.pack("<Q", first_row)
    expected_end = first_row + row_count * stride
    candidates = []
    for region in iter_readable_regions(reader.process):
        if region.type != MEM_PRIVATE:
            continue
        carry = b""
        for offset in range(0, region.size, BLOCK_BYTES):
            raw = reader.bytes(
                region.base_address + offset,
                min(BLOCK_BYTES, region.size - offset),
            )
            if not raw:
                carry = b""
                continue
            data = carry + raw
            origin = region.base_address + offset - len(carry)
            start = 0
            while True:
                found = data.find(needle, start)
                if found < 0:
                    break
                address = origin + found
                context_start = max(region.base_address, address - 0x80)
                context = reader.bytes(context_start, 0x118) or b""
                following_end = reader.ptr(address + 8) or 0
                following_capacity = reader.ptr(address + 16) or 0
                candidates.append({
                    "reference_address": hex(address),
                    "relative_offset": hex(address - region.base_address),
                    "region": hex(region.base_address),
                    "region_size": region.size,
                    "looks_like_vector": (
                        following_end == expected_end
                        and following_capacity >= following_end
                        and (following_capacity - first_row) % stride == 0
                    ),
                    "vector_end": hex(following_end),
                    "vector_capacity": hex(following_capacity),
                    "context_address": hex(context_start),
                    "context_hex": context.hex(),
                })
                start = found + 1
            carry = data[-7:]
    return sorted(candidates, key=lambda item: not item["looks_like_vector"])


def parse_standings_bytes(
    row: bytes, address: int, teams_by_address: dict[int, dict[str, Any]],
) -> dict[str, Any] | None:
    if not row or len(row) != 0xE8:
        return None
    team_pointer = struct.unpack_from("<Q", row, 0x78)[0]
    team = teams_by_address.get(team_pointer)
    if not team:
        return None
    goals_for = struct.unpack_from("<H", row, 0x08)[0]
    goals_against = struct.unpack_from("<H", row, 0x0A)[0]
    points = struct.unpack_from("<H", row, 0x0C)[0]
    played, played_copy = row[0x0E], row[0x0F]
    wins, draws, losses = row[0x10], row[0x11], row[0x12]
    if (
        played != played_copy
        or played != wins + draws + losses
        or points > 3 * wins + draws
        or goals_for > 300 or goals_against > 300
    ):
        return None
    return {
        "team_id": int(team["id"]),
        "team_name": team.get("name"),
        "row_address": hex(address),
        "played": played,
        "wins": wins,
        "draws": draws,
        "losses": losses,
        "goals_for": goals_for,
        "goals_against": goals_against,
        "goal_difference": goals_for - goals_against,
        "points": points,
    }


def parse_standings_row(
    reader: Reader, address: int, teams_by_address: dict[int, dict[str, Any]],
) -> dict[str, Any] | None:
    return parse_standings_bytes(
        reader.bytes(address, 0xE8) or b"", address, teams_by_address,
    )


def find_competition_standings(
    reader: Reader, teams: list[dict[str, Any]], competition_id: int,
) -> list[dict[str, Any]]:
    competition_teams = {
        int(item["address"], 16): item
        for item in teams if int(item.get("competition_id") or 0) == competition_id
    }
    if not competition_teams:
        return []
    pattern = re.compile(b"|".join(
        re.escape(struct.pack("<Q", address))
        for address in competition_teams
    ))
    row_addresses: set[int] = set()
    for region in iter_readable_regions(reader.process):
        if (
            region.type != MEM_PRIVATE
            or not 0x10000 <= region.size <= 64 * 1024 * 1024
        ):
            continue
        carry = b""
        for offset in range(0, region.size, BLOCK_BYTES):
            raw = reader.bytes(
                region.base_address + offset,
                min(BLOCK_BYTES, region.size - offset),
            )
            if not raw:
                carry = b""
                continue
            data = carry + raw
            origin = region.base_address + offset - len(carry)
            for match in pattern.finditer(data):
                local_start = match.start() - 0x78
                local_end = local_start + 0xE8
                if local_start < 0 or local_end > len(data):
                    continue
                candidate = origin + local_start
                if parse_standings_bytes(
                    data[local_start:local_end], candidate, competition_teams,
                ):
                    row_addresses.add(candidate)
            carry = data[-0x160:]
    blocks = []
    remaining = set(row_addresses)
    while remaining:
        address = min(remaining)
        start = address
        while start - 0xE8 in row_addresses:
            start -= 0xE8
        rows = []
        cursor = start
        seen_teams = set()
        while cursor in row_addresses:
            row = parse_standings_row(reader, cursor, competition_teams)
            if not row or row["team_id"] in seen_teams:
                break
            row["rank"] = len(rows) + 1
            rows.append(row)
            seen_teams.add(row["team_id"])
            remaining.discard(cursor)
            cursor += 0xE8
        if len(rows) >= 4:
            blocks.append({
                "start_address": hex(start),
                "row_count": len(rows),
                "rows": rows,
            })
        else:
            remaining.discard(address)
    return sorted(blocks, key=lambda item: item["row_count"], reverse=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--team-id", type=int, default=1190)
    parser.add_argument(
        "--stats", default="28,24,3,1,105,31,74,75",
        help="comma-separated played,wins,draws,losses,gf,ga,gd,points",
    )
    parser.add_argument("--ranking-only", action="store_true")
    parser.add_argument("--competition-id", type=int, default=102428)
    parser.add_argument("--generic-only", action="store_true")
    parser.add_argument("--ranked-team-ids")
    args = parser.parse_args()
    expected = [int(value) for value in args.stats.split(",")]
    pid, _path, layout = select_process_layout()
    if layout.key != "fm24":
        raise RuntimeError("FM24 is not running")
    with open_process(pid) as process:
        module = layout.module(process)
        if not module:
            raise RuntimeError("fm.exe module is unavailable")
        reader = Reader(process, module.base_address, FM24_LAYOUT)
        team_address, region = find_team(reader, args.team_id)
        if not team_address or region is None:
            raise RuntimeError(f"team {args.team_id} was not found")
        teams = team_slab(reader, region)
        requested_ranking = (
            [int(value) for value in args.ranked_team_ids.split(",")]
            if args.ranked_team_ids else []
        )
        competition_standings = (
            [] if requested_ranking
            else find_competition_standings(reader, teams, args.competition_id)
        )
        requested_ranking_candidates = []
        if requested_ranking:
            team_addresses_by_id = {
                int(item["id"]): int(item["address"], 16) for item in teams
            }
            requested_ranking_candidates = ranked_table_candidates(
                reader,
                [team_addresses_by_id[team_id] for team_id in requested_ranking],
            )
        if args.generic_only:
            print(json.dumps({
                "pid": pid,
                "competition_id": args.competition_id,
                "competition_teams": [
                    item for item in teams
                    if int(item.get("competition_id") or 0) == args.competition_id
                ],
                "competition_standings": competition_standings,
                "requested_ranking_candidates": requested_ranking_candidates,
            }, ensure_ascii=False, indent=2))
            return
        team_addresses_by_id = {
            int(item["id"]): int(item["address"], 16) for item in teams
        }
        ranked_addresses = [
            team_addresses_by_id[team_id] for team_id in J1_RANKED_TEAM_IDS
        ]
        ranking_candidates = ranked_table_candidates(reader, ranked_addresses)
        ranking_analysis = (
            analyze_ranked_rows(
                reader,
                int(ranking_candidates[0]["first_team_pointer"], 16),
                int(ranking_candidates[0]["stride"]),
            )
            if ranking_candidates and ranking_candidates[0]["matched_rows"] == len(J1_RANKED_TEAM_IDS)
            else None
        )
        first_row = (
            reader.ptr(int(ranking_candidates[0]["first_team_pointer"], 16) + 0x70)
            if ranking_candidates else 0
        ) or 0
        parsed_rows = []
        if first_row and ranking_candidates:
            stride = int(ranking_candidates[0]["stride"])
            for rank, team_id in enumerate(J1_RANKED_TEAM_IDS, 1):
                row = reader.bytes(first_row + (rank - 1) * stride, stride) or b""
                if len(row) != stride:
                    continue
                goals_for = struct.unpack_from("<H", row, 0x08)[0]
                goals_against = struct.unpack_from("<H", row, 0x0A)[0]
                parsed_rows.append({
                    "rank": rank,
                    "team_id": team_id,
                    "row_address": hex(first_row + (rank - 1) * stride),
                    "team_pointer": hex(struct.unpack_from("<Q", row, 0x78)[0]),
                    "played": row[0x0E],
                    "wins": row[0x10],
                    "draws": row[0x11],
                    "losses": row[0x12],
                    "goals_for": goals_for,
                    "goals_against": goals_against,
                    "goal_difference": goals_for - goals_against,
                    "points": struct.unpack_from("<H", row, 0x0C)[0],
                })
        root_candidates = (
            standings_root_candidates(
                reader, first_row, len(J1_RANKED_TEAM_IDS),
                int(ranking_candidates[0]["stride"]),
            )
            if first_row and ranking_candidates else []
        )
        references = (
            [] if args.ranking_only
            else reference_contexts(reader, team_address, expected)
        )
        print(json.dumps({
            "pid": pid,
            "team_id": args.team_id,
            "team_address": hex(team_address),
            "team_region": {"base": hex(region.base_address), "size": region.size},
            "same_competition_teams": [
                item for item in teams
                if int(item.get("competition_id") or 0) == args.competition_id
            ],
            "competition_id": args.competition_id,
            "competition_standings": competition_standings,
            "ranking_candidates": ranking_candidates,
            "ranking_analysis": ranking_analysis,
            "parsed_rows": parsed_rows,
            "standings_root_candidates": root_candidates,
            "reference_candidates": references,
        }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
