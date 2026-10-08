from __future__ import annotations

import json
import struct
from datetime import datetime
from pathlib import Path
from typing import Any

from fm_collector.win32 import find_module, open_process
from tools.app_paths import SAVE_REGISTRY_PATH, active_save_id
from tools.club_reader import _contract, _find_human_manager, _scan_staff
from tools.initial_data_audit import (
    ACTUAL_PLAYER_AND_NON_PLAYER_VTABLE_RVA, ACTUAL_PLAYER_VTABLE_RVA, ENTITY_UID,
    GAME_PLUGIN, PLAYER_AND_NON_PLAYER_PERSON, PLAYER_PERSON, Reader, select_process,
)
from tools.preview_cup_odds import MANAGER_PERSON


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "data" / "research" / "club_structures"


def _pointer_vectors(reader: Reader, owner: int, roster_count: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for offset in range(0, 0x800, 8):
        begin, end = reader.ptr(owner + offset), reader.ptr(owner + offset + 8)
        if not begin or not end or end <= begin or end - begin > 0x10000:
            continue
        distance = end - begin
        possibilities = []
        for stride in (1, 2, 4, 8, 12, 16, 24, 32, 40, 48, 56, 64):
            if distance % stride == 0:
                count = distance // stride
                if 1 <= count <= 200:
                    possibilities.append({"stride": stride, "count": count, "matches_roster": count == roster_count})
        if not possibilities:
            continue
        sample = reader.bytes(begin, min(distance, 256)) or b""
        rows.append({
            "offset": hex(offset), "begin": hex(begin), "end": hex(end), "distance": distance,
            "possibilities": possibilities, "sample_hex": sample.hex(" "),
        })
    return rows


def _player_bytes(reader: Reader, address: int, name: str, identifier: int) -> dict[str, Any]:
    vtable = reader.ptr(address)
    person_offset = PLAYER_AND_NON_PLAYER_PERSON if vtable == reader.module_base + ACTUAL_PLAYER_AND_NON_PLAYER_VTABLE_RVA else PLAYER_PERSON
    return {
        "id": identifier, "name": name, "address": hex(address), "person_offset": hex(person_offset),
        "object_0_800": (reader.bytes(address, 0x800) or b"").hex(" "),
        "person_0_200": (reader.bytes(address + person_offset, 0x200) or b"").hex(" "),
    }


def main() -> int:
    save_id = str(active_save_id() or "")
    try:
        aliases = json.loads(SAVE_REGISTRY_PATH.read_text(encoding="utf-8")).get("manager_aliases", {})
    except (OSError, json.JSONDecodeError):
        aliases = {}
    manager_keys = [key for key, value in aliases.items() if not save_id or str(value) == save_id]
    if not manager_keys:
        manager_keys = list(aliases)
    if not manager_keys or not all(str(key).startswith("human-") for key in manager_keys):
        raise RuntimeError("存档注册表中没有唯一的人类经理 ID")
    pid, _path = select_process()
    with open_process(pid) as process:
        module = find_module(process, GAME_PLUGIN)
        if not module:
            raise RuntimeError("game_plugin.dll 尚未加载")
        reader = Reader(process, module.base_address)
        manager = 0
        manager_id = 0
        for manager_key in manager_keys:
            try:
                candidate_id = int(str(manager_key).removeprefix("human-"))
                manager = _find_human_manager(reader, candidate_id)
                manager_id = candidate_id
                save_id = str(aliases.get(manager_key) or save_id or manager_key)
                break
            except RuntimeError:
                continue
        if not manager:
            raise RuntimeError("无法定位注册表中的任何人类经理对象")
        manager_snapshot = {"id": manager_id, "save_id": save_id}
        team_address, contract = _contract(reader, manager + MANAGER_PERSON)
        if not team_address:
            raise RuntimeError("当前经理没有俱乐部合同")
        roster = reader.roster(team_address)
        club_address = reader.ptr(team_address + 0x30) or 0
        player_rows = []
        for row in roster:
            address = int(row["address"], 16)
            player_rows.append(_player_bytes(reader, address, str(row["name"]), int(row["id"])))
        staff = _scan_staff(reader, team_address, manager_id)
        payload = {
            "captured_at": datetime.now().isoformat(timespec="seconds"), "pid": pid,
            "manager": manager_snapshot, "team_address": hex(team_address), "club_address": hex(club_address),
            "contract": contract, "roster_count": len(roster), "staff_count": len(staff),
            "team_vectors": _pointer_vectors(reader, team_address, len(roster)),
            "club_vectors": _pointer_vectors(reader, club_address, len(roster)) if club_address else [],
            "players": player_rows, "staff": staff,
        }
    OUTPUT.mkdir(parents=True, exist_ok=True)
    path = OUTPUT / f"club_structures_{datetime.now():%Y%m%d_%H%M%S}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(path)
    print(json.dumps({
        "roster_count": payload["roster_count"], "staff_count": payload["staff_count"],
        "team_vectors": len(payload["team_vectors"]), "club_vectors": len(payload["club_vectors"]),
        "team_roster_matches": [row["offset"] for row in payload["team_vectors"] if any(item["matches_roster"] for item in row["possibilities"])],
        "club_roster_matches": [row["offset"] for row in payload["club_vectors"] if any(item["matches_roster"] for item in row["possibilities"])],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
