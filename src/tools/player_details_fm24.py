from __future__ import annotations

from datetime import date
import struct
from typing import Any

from tools.initial_data_audit import NATION_CODES, NATION_NAMES, decode_date
from tools.preferred_moves import FM24_VERIFIED_PREFERRED_MOVES


BONUS_AND_CLAUSE_NAMES = {
    0: "最低解约金",
    1: "降级解约条款",
    2: "未升级解约条款",
    3: "年度涨薪",
    4: "升级涨薪",
    5: "降级减薪",
    6: "非球员职务邀请解约条款",
    7: "可随时解约条款",
    8: "二次转会分成",
    9: "二次转会利润分成",
    10: "赛季进球里程碑奖金",
    11: "末季联赛出场自动续约一年",
    12: "最高工资匹配条款",
    13: "达到联赛出场次数后涨薪",
    14: "升入顶级联赛涨薪",
    15: "从顶级联赛降级减薪",
    16: "国外俱乐部最低解约金",
    17: "更高级别联赛俱乐部最低解约金",
    18: "国内俱乐部最低解约金",
    19: "达到国家队出场次数后涨薪",
    20: "赞助商承担工资比例",
    21: "门票收入分成",
    22: "俱乐部可选择续约",
    23: "接受管理职位时放弃俱乐部补偿",
    24: "管理职位所需俱乐部补偿比例",
    25: "末季升级后联赛出场自动续约一年",
    26: "末季保级后联赛出场自动续约一年",
    27: "欧冠俱乐部最低解约金",
    28: "MLS保障合同",
    29: "升级后自动续约",
    30: "受伤解约条款",
    31: "洲际赛事俱乐部最低解约金",
    32: "出场费",
    33: "进球奖金",
    34: "零封奖金",
    35: "联赛年度最佳阵容奖金",
    36: "联赛最佳射手奖金",
    37: "国家队出场奖金",
    38: "替补未登场奖金",
    39: "助攻奖金",
    40: "赛季助攻里程碑奖金",
    41: "赛季进球和助攻里程碑奖金",
    54: "合同到期后离队",
    55: "已生效的降级解约条款",
    56: "已生效的未升级解约条款",
    57: "委员会设定最低解约金",
    60: "原俱乐部承担工资比例",
    63: "合同到期时考虑选择",
}

PLAYING_TIME_NAMES = {
    0: "未设定", 1: "明星球员", 2: "重要球员", 3: "常规首发",
    4: "轮换球员", 5: "关键替补", 6: "轮换球员", 7: "边缘球员",
    8: "边缘球员", 9: "紧急替补", 10: "突破型新秀", 11: "未来新星",
    12: "希望之星", 13: "年轻球员", 14: "B队常规球员", 15: "主力门将",
    16: "杯赛门将", 17: "国内杯赛门将", 18: "洲际杯赛门将",
    19: "杯赛门将", 20: "替补球员", 21: "紧急替补", 22: "不在计划内",
}

TRANSFER_STATUS_FLAGS = {
    1: "挂牌出售", 2: "可外租", 4: "未挂牌", 8: "主动要求转会",
    16: "非卖品", 64: "不接受外租",
}

REGISTRATION_NAMES = {
    0: "欧足联赛事注册", 2: "解放者杯注册", 4: "美国职业大联盟注册",
    8: "国内赛事注册", 11: "世界杯注册", 16: "旧版世俱杯注册",
    19: "训练营注册", 23: "中北美冠军联赛注册", 27: "世俱杯注册",
}

PERSON_RELATION_REASON_NAMES = {
    1: "父母",
    3: "子女",
}
PERSON_RELATION_TYPE_NAMES = {
    1: "喜欢的人",
}

NATIONALITY_INFO_NAMES = {
    80: "有资格代表该国", 81: "已代表该国出场", 82: "已入籍但仍按外援处理",
    83: "已选择代表该国", 84: "亲属出生于该国", 85: "出生于该国",
    86: "无资格代表该国", 87: "通过亲属取得国籍",
    88: "已入籍但尚无代表资格", 89: "已入籍并选择代表该国",
    90: "通过亲属取得国籍但尚无代表资格",
}

OFFER_STATUS_NAMES = {0: "未知", 2: "已接受"}


def _signed_byte(value: int | None) -> int | None:
    if value is None:
        return None
    return value - 256 if value > 127 else value


def _clean_name(value: str | None) -> str | None:
    cleaned = str(value or "").replace("\u200b", "").strip()
    return cleaned or None


def _vector(
    reader: Any, address: int, *, width: int, limit: int,
    header: bytes | None = None,
) -> list[bytes]:
    header = header if header is not None else reader.bytes(address, 24)
    if not header or len(header) != 24:
        return []
    begin, end, capacity = struct.unpack("<QQQ", header)
    if not begin and not end and not capacity:
        return []
    if (
        not begin or end < begin or capacity < end or (end - begin) % width
        or (end - begin) // width > limit
    ):
        return []
    raw = reader.bytes(begin, end - begin)
    if raw is None or len(raw) != end - begin:
        return []
    return [raw[offset:offset + width] for offset in range(0, len(raw), width)]


def _date(reader: Any, address: int) -> date | None:
    return decode_date(int(reader.u32(address) or 0))


def _money(value: int | None) -> int | None:
    return int(value) if value is not None and 0 <= int(value) <= 10_000_000_000 else None


def read_fm24_asking_price(reader: Any, player: int) -> int | None:
    value = reader.u32(player + 0x1D0)
    return 0 if value in {300_000_000, 0xFFFFFFFF} else _money(value)


def read_fm24_current_season_stats(
    reader: Any, player: int, *, expected_uid: int | None = None,
) -> dict[str, Any] | None:
    """Read FM24's verified current-season aggregate from db::PLAYER."""
    layout = getattr(reader, "layout", None)
    root_offset = getattr(layout, "player_season_stats_root_offset", None)
    slot_offset = getattr(layout, "player_season_stats_total_slot_offset", None)
    block_size = getattr(layout, "player_season_stats_block_size", None)
    if root_offset is None or slot_offset is None or block_size != 0x78:
        return None
    module_base = int(getattr(reader, "module_base", 0) or 0)
    vtable = int(reader.ptr(player) or 0)
    rva = vtable - module_base if vtable and module_base else 0
    if rva in tuple(getattr(layout, "actual_player_vtable_rvas", ()) or ()):
        person_offset = int(layout.player_person_offset)
    elif rva in tuple(getattr(layout, "player_and_non_player_vtable_rvas", ()) or ()):
        person_offset = int(layout.player_and_non_player_person_offset)
    else:
        return None
    uid = int(reader.u32(player + person_offset + 0x0C) or 0)
    if uid <= 0 or (expected_uid is not None and uid != int(expected_uid)):
        return None
    root = int(reader.ptr(player + int(root_offset)) or 0)
    slot_address = int(reader.ptr(root + int(slot_offset)) or 0) if root else 0
    slot = reader.bytes(slot_address, 0x20) if slot_address else None
    if not slot or len(slot) != 0x20:
        return None
    detail_address = struct.unpack_from("<Q", slot, 0)[0]
    detail = reader.bytes(detail_address, int(block_size)) if detail_address else None
    if not detail or len(detail) != int(block_size):
        return None
    rating_sum_x10, minutes = struct.unpack_from("<HH", slot, 0x08)
    starts, subs = slot[0x0C], slot[0x0D]
    appearances, goals = slot[0x0E], slot[0x0F]
    assists, goals_conceded = slot[0x10], slot[0x11]
    penalties_taken, penalties_scored = slot[0x12], slot[0x13]
    player_of_match, red_cards = slot[0x14], slot[0x15]
    yellow_cards, clean_sheets = slot[0x16], slot[0x17]
    appearances_total = starts + subs
    if appearances > appearances_total or appearances_total > 300:
        return None
    if not appearances and rating_sum_x10:
        return None
    average_rating = (
        rating_sum_x10 / 10.0 / appearances
        if appearances else None
    )
    if average_rating is not None and not 0.0 <= average_rating <= 10.0:
        return None

    def u16(offset: int) -> int:
        return int(struct.unpack_from("<H", detail, offset)[0])

    return {
        "source": "fm24_native_current_season",
        "scope": "current_season_total", "player_uid": uid,
        "starts": starts, "sub_appearances": subs,
        "appearances": appearances_total, "rated_appearances": appearances,
        "minutes": minutes, "rating_sum_x10": rating_sum_x10,
        "average_rating": average_rating, "goals": goals, "assists": assists,
        "goals_conceded": goals_conceded, "clean_sheets": clean_sheets,
        "penalties_taken": penalties_taken, "penalties_scored": penalties_scored,
        "player_of_match": player_of_match, "yellow_cards": yellow_cards,
        "red_cards": red_cards,
        "team_goals": u16(0x00), "team_goals_conceded": u16(0x02),
        "games_won": u16(0x04), "games_lost": u16(0x06),
        "passes_attempted": u16(0x08), "passes_completed": u16(0x0A),
        "pass_completion_pct": round(u16(0x0A) * 100.0 / u16(0x08), 2) if u16(0x08) else None,
        "tackles_attempted": u16(0x0C), "tackles_completed": u16(0x0E),
        "headers_attempted": u16(0x10), "headers_won": u16(0x12),
        "dribbles": u16(0x14), "fouls_made": u16(0x16), "fouls_against": u16(0x18),
        "shots": u16(0x1A), "shots_on_target": u16(0x1C),
        "shot_accuracy_pct": round(u16(0x1C) * 100.0 / u16(0x1A), 2) if u16(0x1A) else None,
        "mistakes_leading_to_goals": u16(0x28),
        "minutes_since_last_goal": u16(0x2A), "minutes_since_last_conceded": u16(0x2C),
        "distance_x10": u16(0x2E), "offsides": u16(0x30),
        "open_play_crosses_completed": u16(0x34), "key_passes": u16(0x36),
        "key_tackles": u16(0x38), "key_headers": u16(0x3A),
        "interceptions": u16(0x3C), "clear_cut_chances_created": u16(0x3E),
        "blocks": u16(0x40), "hat_tricks": u16(0x42),
        "possession_lost": u16(0x44), "possession_won": u16(0x46),
        "xg": u16(0x48) / 100.0, "xa": u16(0x4A) / 100.0,
        "clearances": u16(0x4C), "free_kick_shots": u16(0x50),
        "goals_from_outside_box": u16(0x52),
        "open_play_crosses_attempted": u16(0x5A),
        "shots_from_outside_box": u16(0x5C),
        "expected_goals_prevented": u16(0x5E) / 100.0,
        "sprints": u16(0x60), "progressive_passes": u16(0x64),
        "pressures_attempted": u16(0x66), "pressures_completed": u16(0x68),
        "open_play_key_passes": u16(0x6A), "npxg": u16(0x6C) / 100.0,
        "xg_overperformance": struct.unpack_from("<h", detail, 0x6E)[0] / 100.0,
        "crosses_attempted": u16(0x70), "crosses_completed": u16(0x72),
        "shots_blocked": u16(0x74), "headers_lost": u16(0x76),
    }


def read_fm24_career_stats(reader: Any, player: int) -> dict[str, Any] | None:
    """Read the four career counters verified in the FM24 native block."""
    offset = getattr(getattr(reader, "layout", None), "player_career_stats_offset", None)
    address = int(reader.ptr(player + int(offset or 0)) or 0) if offset is not None else 0
    raw = reader.bytes(address, 120) if address else None
    if not raw or len(raw) != 120:
        return None
    values = struct.unpack_from("<60H", raw)
    return {
        "source": "fm24_native_career_stats",
        "all_time_league_goals": int(values[3]),
        "all_time_goals": int(values[4]),
        "all_time_appearances": int(values[5]),
        "all_time_league_appearances": int(values[7]),
    }


def read_fm24_international_stats(reader: Any, player: int) -> dict[str, Any] | None:
    """Read the FM24 native international counters when the profile exposes them.

    The 24.4.2 research profile exposes these as four bytes in the player
    entity (the companion Person offsets are pointers used by the upstream
    tool).  Keep the offsets build-gated in ``GameLayout`` and fail closed if
    any counter cannot be read.
    """
    layout = getattr(reader, "layout", None)
    offsets = (
        getattr(layout, "player_international_appearances_offset", None),
        getattr(layout, "player_international_goals_offset", None),
        getattr(layout, "player_u21_international_appearances_offset", None),
        getattr(layout, "player_u21_international_goals_offset", None),
    )
    if any(offset is None for offset in offsets):
        return None
    width = int(getattr(layout, "player_international_counter_width", 1) or 1)
    u21_width = int(getattr(layout, "player_u21_international_counter_width", 1) or 1)

    def counter(offset: int | None, size: int) -> int | None:
        if offset is None or int(offset) < 0 or size not in (1, 2, 4):
            return None
        raw = reader.bytes(int(player) + int(offset), size)
        if raw is None or len(raw) != size:
            return None
        value = int.from_bytes(raw, "little", signed=False)
        return value if value <= 1000 else None

    values = (
        counter(offsets[0], width), counter(offsets[1], width),
        counter(offsets[2], u21_width), counter(offsets[3], u21_width),
    )
    if any(value is None for value in values):
        return None
    return {
        "source": "fm24_native_international_counters",
        "international_appearances": int(values[0]),
        "international_goals": int(values[1]),
        "u21_international_appearances": int(values[2]),
        "u21_international_goals": int(values[3]),
    }


def _contract_details(
    reader: Any, contract: int, person: int, game_date: date | None,
) -> dict[str, Any] | None:
    if not contract:
        return None
    contract_raw = reader.bytes(contract, 0xC0)
    if not contract_raw or len(contract_raw) != 0xC0:
        return None
    if struct.unpack_from("<Q", contract_raw, 0x08)[0] != person:
        return None
    team_address = int(struct.unpack_from("<Q", contract_raw, 0x10)[0] or 0)
    if not team_address:
        return None
    started = decode_date(struct.unpack_from("<I", contract_raw, 0x3C)[0])
    expiry = decode_date(struct.unpack_from("<I", contract_raw, 0x40)[0])
    signed = decode_date(struct.unpack_from("<I", contract_raw, 0x44)[0])
    raw_transfer_status = int(contract_raw[0x4F])
    bonuses = []
    for bonus_index, bonus_raw in enumerate(_vector(
        reader, contract + 0x60, width=8, limit=64,
        header=contract_raw[0x60:0x78],
    )):
        amount, number, clause_type = struct.unpack("<ihh", bonus_raw)
        if clause_type not in BONUS_AND_CLAUSE_NAMES or amount < 0:
            continue
        bonuses.append({
            "index": bonus_index,
            "type": clause_type,
            "name": BONUS_AND_CLAUSE_NAMES[clause_type],
            "amount": amount,
            "number": None if number < 0 else number,
            "is_clause": clause_type < 32 or clause_type >= 54,
        })
    team = reader.team(team_address) or {}
    joined = decode_date(int(reader.u32(person + 0x14C) or 0))
    playing_time = int(contract_raw[0x4C])
    squad_number = _signed_byte(contract_raw[0x55])
    return {
        "address": hex(contract),
        "team_id": int(team.get("id") or reader.u32(team_address + 0x0C) or 0),
        "team_name": team.get("short_name") or team.get("name"),
        "team_address": hex(team_address),
        "contract_type": int(contract_raw[0xB3]),
        "joined_club_date": joined.isoformat() if joined else None,
        "start_date": started.isoformat() if started else None,
        "expiry_date": expiry.isoformat() if expiry else None,
        "signed_date": signed.isoformat() if signed else None,
        "wage_per_week": _money(struct.unpack_from("<I", contract_raw, 0x18)[0]),
        "signing_fee": None,
        "signing_fee_note": "已生效合同不保留独立签字费字段",
        "loyalty_bonus": _money(struct.unpack_from("<I", contract_raw, 0x30)[0]),
        "agreed_playing_time": playing_time,
        "agreed_playing_time_name": PLAYING_TIME_NAMES.get(playing_time, f"未知（{playing_time}）"),
        "squad_number": squad_number if squad_number is not None and 0 <= squad_number <= 99 else None,
        "transfer_status_raw": raw_transfer_status,
        "transfer_status": [
            label for flag, label in TRANSFER_STATUS_FLAGS.items()
            if raw_transfer_status & flag
        ],
        "active": bool(not expiry or not game_date or game_date <= expiry),
        "bonuses_and_clauses": bonuses,
        "appearance_fee": next((row["amount"] for row in bonuses if row["type"] == 32), None),
        "goal_bonus": next((row["amount"] for row in bonuses if row["type"] == 33), None),
        "release_clauses": [row for row in bonuses if row["type"] in {0, 1, 2, 6, 7, 16, 17, 18, 27, 30, 31, 54, 55, 56, 57}],
        "extension_clauses": [row for row in bonuses if row["type"] in {11, 22, 25, 26, 29}],
        "relegation_clauses": [row for row in bonuses if row["type"] in {1, 5, 15, 55}],
    }


def _person_names(reader: Any, person: int) -> dict[str, str | None]:
    def pointed(offset: int) -> str | None:
        pointer = int(reader.ptr(person + offset) or 0)
        return _clean_name(reader.fm_string_at(pointer)) if pointer else None

    return {
        "full_name": _clean_name(reader.fm_string_at(person + 0x48)),
        "first_name": pointed(0x58),
        "last_name": pointed(0x60),
        "common_name": pointed(0x68),
    }


def _languages(reader: Any, person: int) -> list[dict[str, Any]]:
    rows = []
    offset = getattr(getattr(reader, "layout", None), "person_languages_offset", 0x120)
    if offset is None:
        return rows
    for raw_pointer in _vector(reader, person + int(offset), width=8, limit=64):
        record = struct.unpack("<Q", raw_pointer)[0]
        language = int(reader.ptr(record) or 0) if record else 0
        value = reader.u8(record + 0x08) if record else None
        name = _clean_name(reader.fm_string_at(language + 0x18)) if language else None
        if name and value is not None and 0 <= value <= 10:
            row = {"name": name, "proficiency": int(value), "maximum": 10}
            language_id = int(reader.u32(language + 0x0C) or 0)
            if language_id > 0:
                row["id"] = language_id
            rows.append(row)
    return rows


def _relations(reader: Any, person: int, team_address: int) -> dict[str, Any]:
    info = int(reader.ptr(person + 0x80) or 0)
    rows = _vector(reader, info, width=16, limit=1024) if info else []
    other_nationalities = []
    registrations = []
    person_relations = []
    for raw in rows:
        target = struct.unpack_from("<Q", raw)[0]
        reason = struct.unpack_from("<H", raw, 8)[0]
        object_type = raw[10]
        relation_type = raw[11]
        if target and object_type == 3 and relation_type == 1:
            target_id = int(reader.u32(target + 0x0C) or 0)
            if target_id > 0:
                target_name = _clean_name(reader.fm_string_at(target + 0x48))
                person_relations.append({
                    "target_id": target_id,
                    "target_name": target_name or str(target_id),
                    "reason": int(reason),
                    "reason_name": PERSON_RELATION_REASON_NAMES.get(int(reason), f"未知原因 ({int(reason)})"),
                    "relation_type": int(relation_type),
                    "relation_type_name": PERSON_RELATION_TYPE_NAMES.get(int(relation_type), f"未知关系 ({int(relation_type)})"),
                    "level": int(raw[12]),
                    "permanent": int(raw[13]) == 79,
                })
        if target and object_type == 8 and relation_type == 9:
            nation_id = int(reader.u32(target + 0x0C) or 0)
            info_value = int(raw[13])
            other_nationalities.append({
                "id": nation_id,
                "name": NATION_NAMES.get(nation_id, f"国家 {nation_id}"),
                "code": NATION_CODES.get(nation_id),
                "eligibility": info_value,
                "eligibility_name": NATIONALITY_INFO_NAMES.get(info_value, f"未知（{info_value}）"),
            })
        if target == int(team_address) and object_type == 4 and relation_type == 25:
            registration = int(raw[12])
            registrations.append({
                "type": registration,
                "name": REGISTRATION_NAMES.get(registration, f"未知注册（{registration}）"),
            })
    return {
        "other_nationalities": other_nationalities,
        "second_nationality": other_nationalities[0] if other_nationalities else None,
        "registrations": registrations,
        "person_relations": person_relations,
    }


def _transfer_offer_ids(reader: Any, person: int) -> list[int]:
    rows = _vector(reader, person + 0x98, width=4, limit=128)
    return [struct.unpack("<I", row)[0] for row in rows]


def _loan_details(reader: Any, person: int, team_address: int) -> dict[str, Any] | None:
    expected_rva = getattr(getattr(reader, "layout", None), "loan_contract_vtable_rva", None)
    other_contracts = int(reader.ptr(person + 0xD0) or 0)
    loan_contract = int(reader.ptr(other_contracts) or 0) if other_contracts else 0
    if (
        expected_rva is None or not loan_contract
        or reader.ptr(loan_contract) != reader.module_base + int(expected_rva)
        or reader.ptr(loan_contract + 0x08) != person
    ):
        return None
    loaned_to = int(reader.ptr(loan_contract + 0x10) or 0)
    if not loaned_to:
        return None
    team = reader.team(loaned_to) or {}
    loan_raw = reader.bytes(loan_contract, 0xC0)
    loan_number = None
    if loan_raw and len(loan_raw) == 0xC0:
        candidate = _signed_byte(loan_raw[0x55])
        if candidate is not None and 0 <= candidate <= 99:
            loan_number = candidate
    return {
        "contract_address": hex(loan_contract),
        "team_id": int(team.get("id") or reader.u32(loaned_to + 0x0C) or 0),
        "team_name": team.get("short_name") or team.get("name"),
        "team_address": hex(loaned_to),
        "is_loaned_out": loaned_to != int(team_address),
        "squad_number": loan_number,
    }


def _contract_offers(
    reader: Any, person: int, game_date: date | None,
) -> list[dict[str, Any]]:
    rows = []
    for raw_pointer in _vector(reader, person + 0xB0, width=8, limit=64):
        offer = struct.unpack("<Q", raw_pointer)[0]
        if not offer:
            continue
        contract = int(reader.ptr(offer + 0xB0) or 0)
        details = _contract_details(reader, contract, person, game_date)
        if not details:
            continue
        status = int(reader.u8(offer + 0xC6) or 0)
        rows.append({
            "transfer_id": int(reader.u32(offer + 0xA0) or 0),
            "status": status,
            "status_name": OFFER_STATUS_NAMES.get(status, f"未知（{status}）"),
            "contract": details,
        })
    return rows


def read_fm24_extended_player(
    reader: Any, player: int, person: int, team_address: int,
    game_date: date | None,
) -> dict[str, Any]:
    contract_address = int(reader.ptr(person + 0xC8) or 0)
    contract = _contract_details(reader, contract_address, person, game_date)
    asking_price = read_fm24_asking_price(reader, player)
    city = int(reader.ptr(person + 0x88) or 0)
    nationality_info = int(reader.u8(player + 0x1A7) or 0)
    relation_details = _relations(reader, person, team_address)
    offers = _contract_offers(reader, person, game_date)
    season_stats = read_fm24_current_season_stats(
        reader, player, expected_uid=int(reader.u32(person + 0x0C) or 0),
    )
    career_stats = read_fm24_career_stats(reader, player)
    international_stats = read_fm24_international_stats(reader, player)
    transfer_status = int((contract or {}).get("transfer_status_raw") or 0)
    future_offer = next((row for row in offers if row["status"] == 2), None)
    loan = _loan_details(reader, person, team_address)
    preferred_moves_mask = struct.unpack(
        "<Q", reader.bytes(person + 0xF0, 8) or b"\0" * 8,
    )[0]
    return {
        **_person_names(reader, person),
        "city_of_birth": _clean_name(reader.fm_string_at(city + 0x18)) if city else None,
        "languages": _languages(reader, person),
        **relation_details,
        "nationality_eligibility": nationality_info,
        "nationality_eligibility_name": NATIONALITY_INFO_NAMES.get(nationality_info),
        "declared_for_nation": nationality_info == 83,
        "preferred_moves_mask": preferred_moves_mask,
        "preferred_moves": [
            {
                "bit": bit,
                "name": FM24_VERIFIED_PREFERRED_MOVES.get(bit, f"未命名习惯 {bit + 1}"),
            }
            for bit in range(64) if preferred_moves_mask & (1 << bit)
        ],
        "loan": loan,
        "is_loaned_out": bool(loan and loan["is_loaned_out"]),
        "shirt_number": (
            (contract or {}).get("squad_number")
            if (contract or {}).get("squad_number") is not None
            else (loan or {}).get("squad_number")
        ),
        "player_contract": contract,
        "asking_price": asking_price,
        "market_value": None,
        "market_value_note": "当前版本未提供可验证的独立当前身价字段",
        **({"season_stats": season_stats} if season_stats is not None else {}),
        **({"career_stats": career_stats} if career_stats is not None else {}),
        **({"international_stats": international_stats} if international_stats is not None else {}),
        **({
            key: value for key, value in international_stats.items()
            if key != "source"
        } if international_stats is not None else {}),
        "transfer": {
            "asking_price": asking_price,
            "transfer_status_raw": transfer_status,
            "transfer_status": (contract or {}).get("transfer_status", []),
            "transfer_listed": bool(transfer_status & 1),
            "loan_listed": bool(transfer_status & 2),
            "transfer_requested": bool(transfer_status & 8),
            "not_for_sale": bool(transfer_status & 16),
            "not_for_loan": bool(transfer_status & 64),
            "available_on_free": bool(not contract or not contract.get("active")),
            "transfer_offer_ids": _transfer_offer_ids(reader, person),
            "contract_offers": offers,
            "future_transfer": future_offer,
            "has_future_agreement": future_offer is not None,
        },
    }
