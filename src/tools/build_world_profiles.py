from __future__ import annotations

import argparse
import hashlib
import json
import struct
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fm_collector.win32 import MEM_PRIVATE, find_module, iter_readable_regions, open_process, read_process_memory
from tools.initial_data_audit import (
    ACTUAL_PLAYER_AND_NON_PLAYER_VTABLE_RVA,
    ACTUAL_PLAYER_VTABLE_RVA,
    CLUB_NAME_SHORT,
    CLUB_VTABLE_RVA,
    ENTITY_UID,
    GAME_DATE_RVA,
    GAME_PLUGIN,
    PLAYER_AND_NON_PLAYER_PERSON,
    PLAYER_CA,
    PLAYER_FITNESS,
    PLAYER_FATIGUE,
    PLAYER_MORALE,
    PLAYER_PERSON,
    PLAYER_SHARPNESS,
    PERSON_COMMON_NAME,
    PERSON_FIRST_NAME,
    PERSON_FULL_NAME,
    PERSON_LAST_NAME,
    Reader,
    decode_date,
    select_process,
    sha256,
    SUPPORTED_EXE_SHA256,
)


ROOT = Path(__file__).resolve().parents[1]
WORLD_ROOT = ROOT / "data" / "world"
PLAYER_ROOT = WORLD_ROOT / "characters" / "players"
STAFF_ROOT = WORLD_ROOT / "characters" / "staff"

PERSON_CONTRACT = 0xA8
PERSON_PREVIOUS_CLUB = 0x108
PERSON_JOINED_CURRENT_CLUB = 0x11C
PLAYER_CURRENT_TEAM = 0x130
MANAGER_PERSON = 0x450

POSITION_LABELS = {
    "GK": "门将", "SW": "清道夫", "DL": "左后卫", "DC": "中后卫", "DR": "右后卫",
    "DM": "后腰", "ML": "左中场", "MC": "中场", "MR": "右中场", "AML": "左边锋",
    "AMC": "前腰", "AMR": "右边锋", "ST": "前锋", "WBL": "左翼卫", "WBR": "右翼卫",
}

ARCHETYPES = (
    ("沉静观察者", "更习惯先摸清更衣室里的节奏，再把意见留在关键时刻说出来。"),
    ("训练场推动者", "把训练当作建立位置感和信任的方式，对细节有近乎固执的耐心。"),
    ("竞争型合作者", "不避讳位置竞争，但更看重竞争之后团队仍然能正常运转。"),
    ("更衣室连接者", "擅长在语言、年龄和经历不同的队友之间搭一座小桥。"),
    ("复盘者", "赛后会反复回想几个决定比赛走向的瞬间，习惯把情绪收进分析里。"),
    ("城市探索者", "将新的联赛和新的城市视作人生章节，愿意从陌生环境里寻找秩序。"),
    ("老派职业者", "相信准时、稳定与完成职责本身就能建立信誉。"),
    ("冒险的执行者", "愿意承担高风险选择，但会把自己的判断建立在充分准备之上。"),
)


def _safe_filename(value: str) -> str:
    return "".join(char if char.isalnum() or char in "-_" else "_" for char in value)


def _name(reader: Reader, person: int) -> str | None:
    common = reader.fm_nested_string_at(person + PERSON_COMMON_NAME)
    first = reader.fm_nested_string_at(person + PERSON_FIRST_NAME)
    last = reader.fm_nested_string_at(person + PERSON_LAST_NAME)
    full = reader.fm_string_at(person + PERSON_FULL_NAME)
    return common or " ".join(part for part in (first, last) if part) or full


def _club_name(reader: Reader, address: int | None) -> str | None:
    if not address or reader.ptr(address) != reader.module_base + CLUB_VTABLE_RVA:
        return None
    return reader.fm_string_at(address + CLUB_NAME_SHORT)


def _age(birth: date | None, game_date: date) -> int | None:
    if not birth:
        return None
    return game_date.year - birth.year - ((game_date.month, game_date.day) < (birth.month, birth.day))


def _date_text(value: date | None) -> str | None:
    return value.isoformat() if value else None


def _player_snapshot(reader: Reader, address: int, game_date: date) -> dict[str, Any] | None:
    vtable = reader.ptr(address)
    person_offset = (
        PLAYER_AND_NON_PLAYER_PERSON
        if vtable == reader.module_base + ACTUAL_PLAYER_AND_NON_PLAYER_VTABLE_RVA
        else PLAYER_PERSON
    )
    if vtable not in {
        reader.module_base + ACTUAL_PLAYER_VTABLE_RVA,
        reader.module_base + ACTUAL_PLAYER_AND_NON_PLAYER_VTABLE_RVA,
    }:
        return None
    person = address + person_offset
    player_id = reader.u32(person + ENTITY_UID)
    name = _name(reader, person)
    if not player_id or not name:
        return None
    current_team_address = reader.ptr(address + PLAYER_CURRENT_TEAM)
    current_team = reader.team(current_team_address) if current_team_address else None
    previous_club_address = reader.ptr(person + PERSON_PREVIOUS_CLUB)
    previous_club = _club_name(reader, previous_club_address)
    joined = decode_date(reader.u32(person + PERSON_JOINED_CURRENT_CLUB) or 0)
    birth = decode_date(reader.u32(person + 0x88) or 0)
    ca = reader.u16(address + PLAYER_CA)
    positions_raw = reader.bytes(address + 0x150, 15) or b""
    position_names = (
        "GK", "SW", "DL", "DC", "DR", "DM", "ML", "MC", "MR",
        "AML", "AMC", "AMR", "ST", "WBL", "WBR",
    )
    positions = [position for position, value in zip(position_names, positions_raw) if value > 1]
    return {
        "id": player_id,
        "name": name,
        "address": hex(address),
        "object_type": "actual_player_and_non_player" if person_offset == PLAYER_AND_NON_PLAYER_PERSON else "actual_player",
        "current_team": (current_team.get("short_name") or current_team.get("name")) if current_team else None,
        "current_team_id": current_team.get("id") if current_team else None,
        "previous_club": previous_club,
        "previous_club_id": reader.u32((previous_club_address or 0) + ENTITY_UID) if previous_club_address else None,
        "joined_current_team": _date_text(joined),
        "date_of_birth": _date_text(birth),
        "age": _age(birth, game_date),
        "ca": ca,
        "positions": positions,
        "fitness_percent": round((reader.u16(address + PLAYER_FITNESS) or 0) / 100, 2),
        "sharpness_percent": round((reader.u16(address + PLAYER_SHARPNESS) or 0) / 100, 2),
        "fatigue_raw": reader.u16(address + PLAYER_FATIGUE),
        "morale_raw": reader.u8(address + PLAYER_MORALE),
    }


def _find_team(reader: Reader, team_id: int) -> int:
    needle = struct.pack("<Q", reader.module_base + 0x44C13A8)
    for region in iter_readable_regions(reader.process):
        if region.type != MEM_PRIVATE or region.size > 256 * 1024 * 1024:
            continue
        data = read_process_memory(reader.process, region.base_address, region.size)
        if not data:
            continue
        offset = 0
        while True:
            found = data.find(needle, offset)
            if found < 0:
                break
            address = region.base_address + found
            if reader.u32(address + ENTITY_UID) == team_id and reader.team(address):
                return address
            offset = found + 8
    raise RuntimeError(f"team {team_id} was not found")


def _scan_recent_departures(reader: Reader, club_address: int, game_date: date) -> list[dict[str, Any]]:
    """Find players whose latest move was away from the managed club."""
    specs = (
        reader.module_base + ACTUAL_PLAYER_VTABLE_RVA,
        reader.module_base + ACTUAL_PLAYER_AND_NON_PLAYER_VTABLE_RVA,
    )
    seen: set[int] = set()
    departures: list[dict[str, Any]] = []
    for region in iter_readable_regions(reader.process):
        if region.type != MEM_PRIVATE or region.size > 256 * 1024 * 1024:
            continue
        data = read_process_memory(reader.process, region.base_address, region.size)
        if not data:
            continue
        for vtable in specs:
            needle = struct.pack("<Q", vtable)
            offset = 0
            while True:
                found = data.find(needle, offset)
                if found < 0:
                    break
                address = region.base_address + found
                offset = found + 8
                if address in seen:
                    continue
                seen.add(address)
                snapshot = _player_snapshot(reader, address, game_date)
                if snapshot and snapshot["previous_club_id"] == reader.u32(club_address + ENTITY_UID):
                    departures.append(snapshot)
    return departures


def _roles(positions: list[str]) -> str:
    if not positions:
        return "位置待确认"
    return "、".join(POSITION_LABELS.get(position, position) for position in positions)


def _ability_band(ca: int | None) -> str:
    if not ca:
        return "能力资料待确认"
    if ca >= 155:
        return "球队的高端核心"
    if ca >= 140:
        return "可以决定比赛局部走势的一线主力"
    if ca >= 125:
        return "可靠的一线队竞争者"
    if ca >= 110:
        return "仍在建立稳定位置的发展型成员"
    return "需要通过比赛与训练争取位置的成员"


def _archetype(player_id: int) -> tuple[str, str]:
    digest = hashlib.sha256(str(player_id).encode("ascii")).digest()
    return ARCHETYPES[digest[0] % len(ARCHETYPES)]


def _narrative(player: dict[str, Any], relationship: str, club_name: str) -> dict[str, Any]:
    title, description = _archetype(int(player["id"]))
    move_date = player.get("joined_current_team")
    source_club = player.get("previous_club")
    if relationship == "incoming":
        move_hook = (
            f"从{source_club or '未知前东家'}来到{club_name}的日期是{move_date or '待确认'}。"
            "这段转变可以成为他在新城市、新语言或新战术体系中寻找归属感的长期线索。"
        )
    else:
        destination = player.get("current_team") or "暂未绑定俱乐部"
        move_hook = (
            f"离开{club_name}后，他在{move_date or '待确认'}与{destination}建立了新的关系。"
            "旧队友、未竟目标和两地球迷的记忆都可以成为后续故事的回声。"
        )
    condition = []
    if (player.get("fitness_percent") or 0) < 85:
        condition.append("身体状态并不在理想区间，故事中可表现为训练节奏或恢复安排带来的压力。")
    if (player.get("sharpness_percent") or 0) < 60:
        condition.append("比赛锐度偏低，适合安排一次重新赢得信任的支线。")
    if (player.get("morale_raw") or 0) >= 16:
        condition.append("当前士气较高，容易成为更衣室情绪的正向支点。")
    if not condition:
        condition.append("当前状态没有明显警报，更适合把矛盾放在竞争、归属或个人目标上。")
    return {
        "archetype": title,
        "description": description,
        "move_hook": move_hook,
        "condition_hook": condition[0],
        "ability_hook": f"以 {player.get('ca') or '?'} CA 和 {_roles(player.get('positions') or [])} 的身份，他是{_ability_band(player.get('ca'))}。",
    }


def _front_matter(payload: dict[str, Any]) -> str:
    return "---\n" + json.dumps(payload, ensure_ascii=False, indent=2) + "\n---"


def _write_player_document(player: dict[str, Any], relationship: str, club_name: str) -> Path:
    narrative = _narrative(player, relationship, club_name)
    if relationship == "incoming":
        if player.get("current_team") == club_name:
            relationship_label = "现役一线队名单成员"
        else:
            relationship_label = "柏太阳神名单关联球员；当前球队记录与名单不一致，可能涉及租借或其他状态，待后续验证"
    else:
        relationship_label = "最近离队球员"
    document = "\n\n".join((
        _front_matter({
            "schema": "fm-world-character/v1",
            "id": player["id"],
            "kind": "player",
            "relationship": relationship,
            "facts_source": "read-only FM26 process memory",
            "narrative_source": "generated fictional setting",
        }),
        f"# {player['name']}",
        "## 游戏事实\n"
        f"- 与{club_name}的关系：{relationship_label}\n"
        f"- 当前球队：{player.get('current_team') or '无有效俱乐部记录'}\n"
        f"- 上一家俱乐部：{player.get('previous_club') or '无有效记录'}\n"
        f"- 最近加盟日期：{player.get('joined_current_team') or '无有效记录'}\n"
        f"- 年龄：{player.get('age') if player.get('age') is not None else '待确认'}\n"
        f"- 位置：{_roles(player.get('positions') or [])}\n"
        f"- 当前能力 CA：{player.get('ca') or '待确认'}\n"
        f"- 体能 / 锐度 / 士气：{player.get('fitness_percent')} / {player.get('sharpness_percent')} / {player.get('morale_raw')}",
        "## 叙事设定（非游戏数据）\n"
        f"- 性格底色：{narrative['archetype']}。{narrative['description']}\n"
        f"- 能力视角：{narrative['ability_hook']}\n"
        f"- 转会线索：{narrative['move_hook']}\n"
        f"- 当前状态线索：{narrative['condition_hook']}",
        "## 调用摘要\n"
        f"{player['name']}是一名{_roles(player.get('positions') or [])}，当前故事基调为“{narrative['archetype']}”。"
        "需要推进剧情时，优先围绕位置竞争、转会归属和当前身体状态展开。",
    )) + "\n"
    destination = PLAYER_ROOT / f"{player['id']}_{_safe_filename(player['name'])}.md"
    destination.write_text(document, encoding="utf-8")
    return destination


def _manager_snapshot(reader: Reader, manager_address: int) -> dict[str, Any]:
    person = manager_address + MANAGER_PERSON
    return {
        "id": reader.u32(person + ENTITY_UID),
        "name": _name(reader, person) or "主教练",
        "address": hex(manager_address),
    }


def _write_manager_document(manager: dict[str, Any], club_name: str, game_date: date) -> Path:
    document = "\n\n".join((
        _front_matter({
            "schema": "fm-world-character/v1",
            "id": manager.get("id"),
            "kind": "manager",
            "facts_source": "read-only FM26 process memory",
            "narrative_source": "generated fictional setting",
        }),
        f"# {manager.get('name') or '玩家经理'}",
        f"## 游戏事实\n- 当前角色：{club_name}主教练\n- 游戏日期：{game_date.isoformat()}",
        "## 叙事设定（非游戏数据）\n"
        "- 他把球队视作一项长期作品：成绩重要，但更在意球员是否愿意把彼此的成功当作自己的成功。\n"
        "- 面对转会市场时，他偏向把新援视作需要被安放的人，而不是单纯补强位置的数字。",
        "## 调用摘要\n"
        "适合作为球队里连接战术决定、更衣室情绪和俱乐部长线目标的叙事视角。",
    )) + "\n"
    destination = STAFF_ROOT / f"{manager.get('id') or 'manager'}_{_safe_filename(manager.get('name') or '玩家经理')}.md"
    destination.write_text(document, encoding="utf-8")
    return destination


def _cached_departures(reader: Reader, club_address: int, game_date: date) -> list[dict[str, Any]]:
    index_path = WORLD_ROOT / "world_index.json"
    if not index_path.exists():
        return []
    try:
        cached = json.loads(index_path.read_text(encoding="utf-8")).get("recent_departures", [])
    except (OSError, json.JSONDecodeError):
        return []
    rows = []
    for item in cached:
        try:
            address = int(str(item["address"]), 16)
        except (KeyError, TypeError, ValueError):
            continue
        snapshot = _player_snapshot(reader, address, game_date)
        if snapshot and snapshot["previous_club_id"] == reader.u32(club_address + ENTITY_UID):
            rows.append(snapshot)
    return rows


def build_profiles(team_id: int, reuse_departures: bool = False) -> dict[str, Any]:
    pid, process_path = select_process()
    if sha256(process_path) != SUPPORTED_EXE_SHA256:
        raise RuntimeError("unsupported fm.exe build")
    with open_process(pid) as process:
        module = find_module(process, GAME_PLUGIN)
        if not module:
            raise RuntimeError("game_plugin.dll not loaded")
        reader = Reader(process, module.base_address)
        game_date = decode_date(reader.u32(module.base_address + GAME_DATE_RVA) or 0)
        if not game_date:
            raise RuntimeError("game date unavailable")
        team_address = _find_team(reader, team_id)
        team = reader.team(team_address)
        if not team:
            raise RuntimeError("team details unavailable")
        club_address = reader.ptr(team_address + 0x30)
        club_name = team["short_name"] or team["name"] or str(team_id)
        current = []
        for item in reader.roster(team_address):
            snapshot = _player_snapshot(reader, int(item["address"], 16), game_date)
            if snapshot:
                current.append(snapshot)
        departures = _cached_departures(reader, club_address or 0, game_date) if reuse_departures else []
        if not departures:
            departures = _scan_recent_departures(reader, club_address or 0, game_date)
        current_ids = {item["id"] for item in current}
        departures = [item for item in departures if item["id"] not in current_ids]
        manager_address = reader.ptr(team_address + 0x80)
        manager = _manager_snapshot(reader, manager_address) if manager_address else None

    PLAYER_ROOT.mkdir(parents=True, exist_ok=True)
    STAFF_ROOT.mkdir(parents=True, exist_ok=True)
    paths = []
    for player in [*current, *departures]:
        relationship = "incoming" if player["id"] in current_ids else "outgoing"
        paths.append(_write_player_document(player, relationship, club_name))
    manager_path = _write_manager_document(manager, club_name, game_date) if manager else None
    index = {
        "schema": "fm-world-index/v1",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "game_date": game_date.isoformat(),
        "club": {
            "id": team["id"],
            "name": club_name,
            "competition": (team.get("competition") or {}).get("short_name"),
            "stadium": team.get("stadium"),
            "morale": team.get("morale"),
            "manager": manager,
        },
        "current_players": current,
        "recent_departures": departures,
        "documents": [str(path.relative_to(ROOT)).replace("\\", "/") for path in paths],
        "manager_document": str(manager_path.relative_to(ROOT)).replace("\\", "/") if manager_path else None,
        "limitations": [
            "Transfer facts are the latest move retained on each player object.",
            "A player who moved again after leaving this club may no longer be discoverable as a departure.",
            "Narrative sections are fictional settings, not Football Manager source data.",
        ],
    }
    (WORLD_ROOT / "world_index.json").write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
    club_document = "\n\n".join((
        _front_matter({"schema": "fm-world-club/v1", "id": team["id"], "facts_source": "read-only FM26 process memory"}),
        f"# {club_name}",
        "## 游戏事实\n"
        f"- 联赛：{(team.get('competition') or {}).get('short_name') or '待确认'}\n"
        f"- 主场：{team.get('stadium') or '待确认'}\n"
        f"- 更衣室士气：{team.get('morale') if team.get('morale') is not None else '待确认'}\n"
        f"- 现役人物档案：{len(current)}\n"
        f"- 最近离队人物档案：{len(departures)}",
        "## 叙事设定（非游戏数据）\n"
        "- 这是一支把本土根基与国际化引援同时放在桌面上的球队。每一笔转会都不只是阵容变化，也会改变更衣室的语言、记忆与野心。\n"
        "- 俱乐部档案是人物关系的锚点：新援带来新的坐标，离队者留下旧目标和未说完的话。",
    )) + "\n"
    (WORLD_ROOT / "club.md").write_text(club_document, encoding="utf-8")
    return {"club": club_name, "game_date": game_date.isoformat(), "current": len(current), "departures": len(departures), "documents": len(paths)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build local narrative profiles from read-only FM26 memory")
    parser.add_argument("--team-id", type=int, default=1190)
    parser.add_argument("--reuse-departures", action="store_true", help="reuse departure addresses from the current world index")
    arguments = parser.parse_args()
    print(json.dumps(build_profiles(arguments.team_id, arguments.reuse_departures), ensure_ascii=False))
