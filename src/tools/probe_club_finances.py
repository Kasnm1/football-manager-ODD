from __future__ import annotations

import json
import struct
from datetime import datetime
from pathlib import Path

from fm_collector.win32 import MEM_PRIVATE, find_module, iter_readable_regions, open_process
from tools.initial_data_audit import CLUB_VTABLE_RVA, ENTITY_UID, GAME_PLUGIN, TEAM_VTABLE_RVA, Reader, select_process


TARGETS = {
    23292170: {"name": "上海海港", "balance": 14_000_000, "transfer": 105_000, "wage": 275_000},
    414: {"name": "上海申花", "balance": 13_750_000, "transfer": 0, "wage": 195_000},
    2000328113: {"name": "俱乐部 2000328113", "balance": 10_000_000, "transfer": 0, "wage": 160_000},
}
OUTPUT = Path(__file__).resolve().parents[1] / "data" / "research" / "club_finances"


def _find_teams(reader: Reader) -> dict[int, int]:
    needle = struct.pack("<Q", reader.module_base + TEAM_VTABLE_RVA)
    wanted = set(TARGETS)
    found: dict[int, int] = {}
    for region in iter_readable_regions(reader.process):
        if region.type != MEM_PRIVATE or region.size > 512 * 1024 * 1024:
            continue
        offset, carry = 0, b""
        while offset < region.size:
            length = min(8 * 1024 * 1024, region.size - offset)
            block = reader.bytes(region.base_address + offset, length)
            if block:
                data = carry + block
                base = region.base_address + offset - len(carry)
                cursor = 0
                while True:
                    cursor = data.find(needle, cursor)
                    if cursor < 0:
                        break
                    address = base + cursor
                    uid = reader.u32(address + ENTITY_UID)
                    club = reader.ptr(address + 0x30)
                    if uid in wanted and club and reader.ptr(club) == reader.module_base + CLUB_VTABLE_RVA:
                        found[int(uid)] = address
                    cursor += 8
                carry = data[-7:]
            offset += length
            if len(found) == len(wanted):
                return found
    return found


def _encodings(value: int) -> dict[str, bytes]:
    variants = {
        "u32_pounds": ("<I", value),
        "u64_pounds": ("<Q", value),
        "f32_pounds": ("<f", float(value)),
        "f64_pounds": ("<d", float(value)),
        "u64_pence": ("<Q", value * 100),
        "f32_thousands": ("<f", value / 1000),
        "f64_thousands": ("<d", value / 1000),
    }
    if value * 100 <= 0xFFFFFFFF:
        variants["u32_pence"] = ("<I", value * 100)
    return {name: struct.pack(fmt, number) for name, (fmt, number) in variants.items()}


def _hits(data: bytes, base: int, expected: dict[str, int]) -> list[dict[str, object]]:
    rows = []
    for field, value in expected.items():
        if not isinstance(value, (int, float)):
            continue
        # Zero is far too common to be useful without a paired field nearby.
        if not value:
            continue
        for encoding, needle in _encodings(int(value)).items():
            cursor = 0
            while True:
                cursor = data.find(needle, cursor)
                if cursor < 0:
                    break
                rows.append({"field": field, "value": value, "encoding": encoding, "address": hex(base + cursor), "offset": hex(cursor)})
                cursor += 1
    return rows


def _readable_owner(regions, pointer: int) -> bool:
    return any(region.base_address <= pointer < region.base_address + region.size for region in regions)


def _inspect_graph(reader: Reader, roots: list[tuple[str, int]], expected: dict[str, int]) -> list[dict[str, object]]:
    regions = [region for region in iter_readable_regions(reader.process) if region.type == MEM_PRIVATE]
    queue = [(label, address, 0) for label, address in roots if address]
    visited: set[int] = set()
    reports: list[dict[str, object]] = []
    while queue:
        label, address, depth = queue.pop(0)
        if address in visited or len(visited) >= 2500:
            continue
        visited.add(address)
        data = reader.bytes(address, 0x2000)
        if not data:
            continue
        hits = _hits(data, address, expected)
        if hits:
            reports.append({"owner": label, "owner_address": hex(address), "depth": depth, "hits": hits})
        if depth >= 2:
            continue
        for offset in range(0, min(len(data), 0x1000) - 7, 8):
            pointer = struct.unpack_from("<Q", data, offset)[0]
            if pointer >= 0x10000 and pointer not in visited and _readable_owner(regions, pointer):
                queue.append((f"{label}->+0x{offset:X}", pointer, depth + 1))
    return reports


def _global_integer_hits(reader: Reader) -> dict[str, list[str]]:
    needles = {
        f"{team_id}:transfer_wage_pair": struct.pack("<II", int(values["transfer"]), int(values["wage"]))
        for team_id, values in TARGETS.items()
    }
    found: dict[str, list[str]] = {key: [] for key in needles}
    for region in iter_readable_regions(reader.process):
        if region.type != MEM_PRIVATE or region.size > 512 * 1024 * 1024:
            continue
        offset, carry = 0, b""
        while offset < region.size:
            length = min(8 * 1024 * 1024, region.size - offset)
            block = reader.bytes(region.base_address + offset, length)
            if block:
                data = carry + block
                base = region.base_address + offset - len(carry)
                for key, needle in needles.items():
                    cursor = 0
                    alignment = 4
                    while len(found[key]) < 500:
                        cursor = data.find(needle, cursor)
                        if cursor < 0:
                            break
                        address = base + cursor
                        if address % alignment == 0:
                            found[key].append(hex(address))
                        cursor += 1
                carry = data[-7:]
            offset += length
    return {key: addresses for key, addresses in found.items() if addresses}


def _nearby_amounts(reader: Reader, hits: dict[str, list[str]]) -> list[dict[str, object]]:
    expected = {"balance": 14_000_000, "transfer": 105_000, "wage": 275_000}
    reports = []
    for addresses in hits.values():
        for address_text in addresses:
            address = int(address_text, 16)
            base = max(0x10000, address - 0x2000)
            data = reader.bytes(base, 0x4000) or b""
            rows = _hits(data, base, expected)
            reports.append({"anchor": address_text, "nearby": rows})
    return reports


def main() -> int:
    pid, _path = select_process()
    with open_process(pid) as process:
        module = find_module(process, GAME_PLUGIN)
        if not module:
            raise RuntimeError("game_plugin.dll 尚未加载")
        reader = Reader(process, module.base_address)
        teams = _find_teams(reader)
        integer_hits = _global_integer_hits(reader)
        payload = {
            "captured_at": datetime.now().isoformat(timespec="seconds"), "pid": pid,
            "global_integer_hits": integer_hits,
            "nearby_amounts": _nearby_amounts(reader, integer_hits), "clubs": {},
        }
        for team_id, expected in TARGETS.items():
            team = teams.get(team_id, 0)
            club = reader.ptr(team + 0x30) if team else 0
            payload["clubs"][str(team_id)] = {
                "expected": expected,
                "team_address": hex(team) if team else None,
                "club_address": hex(club) if club else None,
                "reports": [],
            }
    OUTPUT.mkdir(parents=True, exist_ok=True)
    path = OUTPUT / f"club_finances_{datetime.now():%Y%m%d_%H%M%S}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(path)
    print("global integer hits", {key: len(value) for key, value in payload["global_integer_hits"].items()})
    for team_id, row in payload["clubs"].items():
        print(team_id, row["team_address"], row["club_address"], "reports", len(row["reports"]))
        for report in row["reports"][:20]:
            print(" ", report["owner"], report["hits"][:8])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
