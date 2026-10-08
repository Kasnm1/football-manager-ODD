from __future__ import annotations

import ctypes
import re
import random
import struct
import threading
from collections import OrderedDict
from copy import deepcopy
from contextlib import contextmanager, nullcontext
from ctypes import wintypes
from datetime import date, timedelta
from time import monotonic, perf_counter, sleep
from typing import Any, Callable

from fm_collector.win32 import (
    MEM_PRIVATE, find_module, iter_readable_regions, kernel32, open_process,
    read_process_memory, write_process_memory,
)
from tools.initial_data_audit import (
    CLUB_NAME_FULL, CLUB_NAME_SHORT, ENTITY_UID, GAME_DATE_RVA, GAME_PLUGIN, PLAYER_CA, PLAYER_FATIGUE,
    PLAYER_FITNESS, PLAYER_MORALE, PLAYER_SHARPNESS,
    NATION_CODES, NATION_NAMES, Reader, TEAM_CLUB, TEAM_MANAGER,
    TEAM_ROSTER_BEGIN, TEAM_ROSTER_END, TEAM_STADIUM,
    decode_date, parse_fixture, scan_fixture_addresses, select_process, select_process_layout,
)
from tools.game_session import borrow_game_reader
from tools.game_layout import FM24_EPIC_EXE_SHA256
from tools.database_index import (
    database_index_error_for_reader, database_index_for_reader,
    resolve_team_club,
)
from tools.player_details_fm24 import (
    BONUS_AND_CLAUSE_NAMES, NATIONALITY_INFO_NAMES,
    read_fm24_asking_price, read_fm24_extended_player,
)
from tools.player_details_fm26 import read_fm26_asking_price, read_fm26_extended_player
from tools.player_rca import (
    FM24_RCA_MODEL, FM26_RCA_MODEL,
    fm24_attribute_ca_milestones, fm24_ca_budget_attribute_change,
    fm24_recommended_ca, fm24_training_ca_change,
    fm26_attribute_ca_milestones, fm26_ca_budget_attribute_change,
    fm26_recommended_ca, fm26_training_ca_change,
)
from tools.retirement import (
    attach_retirement_details, retirement_record_index, update_retirement_plan,
)
from tools.club_vision import CLUB_VISION_TYPE_NAMES


# Keep the club-only read path independent from the odds native core.  The
# diagnostic executable imports this module only for bounded club/staff reads;
# importing preview_cup_odds here would otherwise load the Rust pricing DLL at
# module import time even though none of those manager helpers are used.
MANAGER_PERSON = 0x450
HUMAN_MANAGER_VTABLE_RVA = 0x44A51CC
SALARY_TAX_RATE = 0.30
SALARY_NET_RATE = 0.70
REMOTE_THREAD_WAIT_MS = 10_000
REMOTE_FREE_THREAD_WAIT_MS = 1_000
WAIT_OBJECT_0 = 0


def _human_manager_matches(reader: Reader, address: int, manager_id: int) -> bool:
    from tools.preview_cup_odds import _human_manager_matches as implementation

    return implementation(reader, address, manager_id)


def _find_human_manager_by_id(reader: Reader, manager_id: int) -> int:
    from tools.preview_cup_odds import _find_human_manager_by_id as implementation

    return implementation(reader, manager_id)


def _person_contract_candidates(reader: Reader, person: int) -> list[int]:
    from tools.preview_cup_odds import _person_contract_candidates as implementation

    return implementation(reader, person)


PERSON_CONTRACT = 0xA8
FM24_PERSON_CONTRACT = 0xC8
FM24_UNHAPPINESS_CONTAINER = 0x28
FM24_UNHAPPINESS_RECORD_SIZE = 0x1C
FM24_UNHAPPINESS_LIMIT = 10
PERSON_CONTRACT_COLLECTION = 0xB0
FM24_PERSON_JOINED_CLUB = 0x14C
FM26_PERSON_JOINED_CLUB = 0x11C
PLAYER_HEIGHT = 0x22E
PLAYER_ATTRIBUTES = 0x15F
PLAYER_INTERNATIONAL_REPUTATION = 0x262
PLAYER_PA = 0x266
PERSON_HIDDEN_ATTRIBUTES = 0x70
PERSON_RELATIONSHIPS = 0x78
PERSON_NATIONALITY = 0x68
PERSON_DATE_OF_BIRTH = 0x88
PERSON_HIDDEN_ATTRIBUTE_NAMES = (
    "适应性", "雄心", "忠诚", "抗压能力",
    "职业素养", "体育精神", "情绪控制", "争论",
)
PLAYER_HIDDEN_ATTRIBUTE_IDS = {
    "左脚": 0x27, "右脚": 0x28, "肮脏动作": 0x38,
    "稳定性": 0x3B, "大赛发挥": 0x3E,
    "受伤倾向": 0x3F, "多面性": 0x40,
}
HIDDEN_ATTRIBUTE_ALIASES = {"争议性": "争论"}
_CACHE_LOCK = threading.RLock()
_CACHE: dict[tuple[Any, ...], tuple[float, dict[str, Any]]] = {}
_LOCALIZED_NAME_CACHE: dict[tuple[int, tuple[int, int, int]], str | None] = {}
_STAFF_SCAN_CACHE: dict[tuple[Any, ...], tuple[float, list[tuple[int, int]]]] = {}
_FREE_STAFF_SCAN_CACHE: dict[
    tuple[int, int, int, int], tuple[float, list[dict[str, Any]]]
] = {}
_HUMAN_MANAGER_CACHE: dict[tuple[int, int, int], int] = {}
_NATIVE_INJURY_TEMPLATE_CACHE: dict[
    tuple[int, int, int, tuple[str, ...], int, int], list[dict[str, Any]]
] = {}
_NATIVE_INJURY_TYPE_CACHE: dict[tuple[int, str], int] = {}
_PLAYER_TRAINING_CA_CACHE_LIMIT = 64
_PLAYER_TRAINING_CA_CACHE_LOCK = threading.RLock()
_PLAYER_TRAINING_CA_CACHE: OrderedDict[
    tuple[Any, ...], dict[str, Any]
] = OrderedDict()

SECOND_NATIONALITY_RECORD_SIZE = 16
SECOND_NATIONALITY_OBJECT_TYPE = 8
SECOND_NATIONALITY_RELATION_TYPE = 9
SECOND_NATIONALITY_RELATION_TYPES = {9, 0x46}
SECOND_NATIONALITY_LEVEL = 100
SECOND_NATIONALITY_ELIGIBLE = 80

PERSON_RELATION_OBJECT_TYPE = 3
PERSON_RELATION_TYPE = 1
PERSON_RELATION_LEVEL = 100
PERSON_RELATION_PERMANENT = 79
PERSON_RELATION_REASONS = {"parent": 1, "child": 3}


def _prune_timed_cache_locked(
    cache: dict[Any, tuple[float, Any]], *, ttl: float, maximum: int,
    current_pid: int | None = None,
) -> None:
    """Prune one timestamped cache while its owning lock is held."""
    now = monotonic()
    stale = [
        key for key, (cached_at, _value) in cache.items()
        if now - cached_at > float(ttl)
        or (
            current_pid is not None
            and (not isinstance(key, tuple) or not key or int(key[0]) != int(current_pid))
        )
    ]
    for key in stale:
        cache.pop(key, None)
    overflow = len(cache) - max(1, int(maximum))
    if overflow > 0:
        oldest = sorted(cache.items(), key=lambda item: item[1][0])[:overflow]
        for key, _value in oldest:
            cache.pop(key, None)


def patch_club_profile_cache(
    team_id: int, *,
    name: str | None = None,
    short_name: str | None = None,
    stadium: dict[str, Any] | None = None,
    facilities: dict[str, Any] | None = None,
    debts_repaid: bool = False,
) -> int:
    """Patch only the stable cached club profile affected by a native write."""
    patched = 0
    with _CACHE_LOCK:
        for cache_key, (_cached_at, profile) in _CACHE.items():
            if int(cache_key[2]) != int(team_id):
                continue
            team = profile.get("team") or {}
            if short_name is not None or name is not None:
                team["name"] = short_name or name
            information = profile.get("club_information") or {}
            if stadium is not None:
                cached_stadium = information.get("stadium")
                if isinstance(cached_stadium, dict):
                    cached_stadium.update(stadium)
                else:
                    information["stadium"] = dict(stadium)
            if facilities is not None:
                cached_facilities = information.get("facilities")
                if isinstance(cached_facilities, dict):
                    cached_facilities.update(facilities)
                else:
                    information["facilities"] = dict(facilities)
            if debts_repaid:
                finances = information.get("finances")
                if isinstance(finances, dict):
                    finances["debts"] = []
            patched += 1
    return patched


def invalidate_club_profile_cache(
    *, pid: int | None = None, team_id: int | None = None,
) -> int:
    """Invalidate a precise profile scope instead of clearing every club."""
    with _CACHE_LOCK:
        stale = [
            key for key in _CACHE
            if (pid is None or int(key[0]) == int(pid))
            and (team_id is None or int(key[2]) == int(team_id))
        ]
        for key in stale:
            _CACHE.pop(key, None)
        return len(stale)


def _injury_list_offset(layout: Any) -> int:
    offset = layout.player_injury_list_offset
    if offset is None:
        raise RuntimeError(f"{layout.display_name} 尚未适配球员伤病结构")
    return int(offset)


def _world_player_asking_price_offset(layout: Any) -> int:
    """Return the verified asking-price offset for the active FM generation."""
    return 0x1D0 if str(layout.key).startswith("fm24") else 0x234


def _world_player_nationality_eligibility_offset(layout: Any) -> int:
    """Return the verified national-team eligibility byte offset."""
    return 0x1A7 if str(layout.key).startswith("fm24") else 0x26E


def _world_player_contract_size(layout: Any) -> int:
    """Return the minimum primary-contract bytes needed by the active generation."""
    return 0xC8 if str(layout.key).startswith("fm26") else 0xC0


def _world_player_name_storage(
    reader: Reader, person: int, offset: int, nested: bool,
) -> tuple[int, str, bytes]:
    field = int(person) + int(offset)
    entry = int(reader.ptr(field) or 0)
    pointer = int(reader.ptr(entry) or 0) if nested and entry else entry
    if pointer <= 0x1000:
        raise RuntimeError("球员姓名字符串指针无效")
    header = reader.bytes(pointer - 12, 16)
    if not header or len(header) != 16:
        raise RuntimeError("球员姓名字符串头读取失败")
    capacity_word, refcount, length = struct.unpack("<QII", header)
    capacity = int(capacity_word & 0xFFFFFFFF)
    if not 0 < length <= 256 or not 0 < refcount < 1_000_000 or capacity < length + 9:
        raise RuntimeError("球员姓名字符串头校验失败")
    raw = reader.bytes(pointer + 4, int(length))
    if not raw or len(raw) != int(length):
        raise RuntimeError("球员姓名字符串内容读取失败")
    try:
        value = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise RuntimeError("球员姓名不是有效 UTF-8") from error
    return pointer, value, bytes(raw)

# Confirmed from live FM24 24.4.2 injury records.  Keep only stable metadata
# here: all process addresses and record pointers change between sessions.
# Kept as stable reference metadata; live creation uses genuine process-local
# records so pointers are never copied across sessions or game versions.
FM24_INJURY_CATALOG = (
    {"name": "腹股沟拉伤", "duration_days_low": 12, "duration_days_high": 21},
    {"name": "腘绳肌撕裂", "duration_days_low": 30, "duration_days_high": 51},
)

# A small number of real players have no full/common-name localization link
# in the person object. These ID-scoped fallbacks are used only after the
# regular localized-name record lookup fails.
LOCALIZED_PLAYER_NAME_FALLBACKS = {
    67191240: "白昇浩",
    91193050: "延斯·卡斯特罗普",
}

STAFF_ABILITY_NAMES = (
    "进攻训练", "防守训练", "体能训练", "门将训练", "心理训练", "战术训练", "技术训练",
    "青训培养", "适应能力", "意志力", "判断球员能力", "判断球员潜力", "纪律管理",
)

# ACTUAL_NON_PLAYER exposes its PERSON subobject through the vtable scanned
# above. The complete object starts 0x100 bytes before that subobject. FM's
# coaching values are stored as 0-100 ratings and displayed on a 1-20 scale;
# role preferences and personality values are already stored as 1-20 bytes.
STAFF_COACHING_FIELDS = {
    "门将训练": 0x2B,
    "判断球员能力": 0x2C,
    "判断球员潜力": 0x2D,
    "人员管理": 0x2E,
    "激励": 0x2F,
    "理疗": 0x30,
    "战术知识": 0x31,
    "进攻训练": 0x32,
    "防守训练": 0x33,
    "体能训练": 0x34,
    "控球训练": 0x35,
    "技术训练": 0x36,
    "战术训练": 0x37,
    "数据分析": 0x3C,
    "运动科学": 0x3F,
    "谈判": 0x41,
    "判断职员能力": 0x42,
    "定位球训练": 0x43,
}

STAFF_JOB_TYPES = {
    2: "教练",
    4: "主席",
    6: "总监",
    8: "常务总监",
    10: "足球总监",
    12: "理疗师",
    14: "球探",
    16: "主教练",
    20: "助理教练",
    22: "定位球教练",
    26: "体能教练",
    34: "门将教练",
    38: "首席医生",
    40: "运动科学团队主管",
    44: "首席球探",
    46: "队医",
    48: "运动科学顾问",
    50: "首席理疗师",
    58: "招募分析师",
    60: "表现分析师",
    62: "首席表现分析师",
    64: "青训主管",
    66: "所有者",
    70: "主席",
    86: "外租总监",
    88: "技术总监",
}

COACHING_LICENSE_NAMES = {
    0: "无证书",
    7: "国家 C 级",
    6: "国家 B 级",
    5: "国家 A 级",
    4: "洲际 C 级",
    3: "洲际 B 级",
    2: "洲际 A 级",
    1: "洲际职业级",
}
COACHING_LICENSE_NEXT = {
    0: 7, 7: 6, 6: 5, 5: 4, 4: 3, 3: 2, 2: 1,
}
COACHING_STAFF_JOB_TYPES = {2, 20, 22, 26, 34, 64}
CLUB_CONTROLLER_JOB_TYPES = frozenset({4, 66, 70})


def is_club_controller_job_type(value: Any) -> bool:
    try:
        return int(value) in CLUB_CONTROLLER_JOB_TYPES
    except (TypeError, ValueError):
        return False


def _club_controller_staff(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Return FM's chairman/owner/president record using a stable priority."""
    priority = {4: 0, 66: 1, 70: 2}
    candidates = [
        row for row in rows
        if is_club_controller_job_type(row.get("job_type"))
    ]
    return min(
        candidates,
        key=lambda row: (
            priority.get(int(row.get("job_type") or 0), len(priority)),
            str(row.get("name") or "").casefold(),
            int(row.get("id") or 0),
        ),
        default=None,
    )


def _scan_club_controllers(
    reader: Reader, team_address: int, manager_id: int = 0,
) -> list[dict[str, Any]]:
    """Find every chairman/owner/president linked to a club.

    Some saves place club controllers in a sparse staff-object slab which the
    normal staff sampler intentionally skips. Controller contracts themselves
    share validated vtables with controllers found in the dense slabs, so use
    those contracts as process-local seeds and scan only their containing
    regions for the requested team pointer.
    """
    regular = _scan_staff(reader, team_address, manager_id)
    rows = {
        int(row.get("id") or 0): row
        for row in regular
        if is_club_controller_job_type(row.get("job_type"))
        and int(row.get("id") or 0) > 0
    }
    try:
        seeds: list[tuple[int, int]] = []
        contract_vtables: set[int] = set()
        job_offset = reader.layout.staff_job_type_offset
        if job_offset is None:
            return list(rows.values())
        for _person, contract in _staff_scan_entries(reader, manager_id):
            if (
                not contract
                or not is_club_controller_job_type(
                    reader.u8(contract + int(job_offset))
                )
            ):
                continue
            vtable = int(reader.ptr(contract) or 0)
            if vtable:
                seeds.append((int(contract), vtable))
                contract_vtables.add(vtable)
        if not seeds or not contract_vtables:
            return list(rows.values())

        regions = []
        for region in iter_readable_regions(reader.process):
            if (
                region.type != MEM_PRIVATE
                or region.size <= 0
                or region.size > 512 * 1024 * 1024
            ):
                continue
            end = region.base_address + region.size
            if any(region.base_address <= contract < end for contract, _ in seeds):
                regions.append(region)

        expected_person_vtable = (
            reader.module_base + int(reader.layout.staff_person_vtable_rva)
            if reader.layout.staff_person_vtable_rva is not None else 0
        )
        for region in regions:
            data = reader.bytes(region.base_address, region.size)
            if not data:
                continue
            for contract_vtable in contract_vtables:
                needle = struct.pack("<Q", contract_vtable)
                position = 0
                while True:
                    position = data.find(needle, position)
                    if position < 0:
                        break
                    contract = region.base_address + position
                    position += 8
                    if position - 8 + max(0x18, int(job_offset) + 1) > len(data):
                        continue
                    if struct.unpack_from("<Q", data, position - 8 + 0x10)[0] != int(team_address):
                        continue
                    job_type = data[position - 8 + int(job_offset)]
                    if not is_club_controller_job_type(job_type):
                        continue
                    person = struct.unpack_from("<Q", data, position - 8 + 0x08)[0]
                    if (
                        not person
                        or (
                            expected_person_vtable
                            and reader.ptr(person) != expected_person_vtable
                        )
                    ):
                        continue
                    row = _staff_candidate(
                        reader, int(person), int(team_address), int(manager_id),
                    )
                    identifier = int((row or {}).get("id") or 0)
                    if (
                        row
                        and identifier > 0
                        and is_club_controller_job_type(row.get("job_type"))
                    ):
                        rows[identifier] = row
    except Exception:
        # The regular staff result remains safe and useful if the optional
        # sparse-contract discovery is unavailable on an unverified layout.
        pass
    priority = {4: 0, 66: 1, 70: 2}
    return sorted(
        rows.values(),
        key=lambda row: (
            priority.get(int(row.get("job_type") or 0), len(priority)),
            int(row.get("id") or 0),
        ),
    )


POSITION_NAMES = ("GK", "SW", "DL", "DC", "DR", "DM", "ML", "MC", "MR", "AML", "AMC", "AMR", "ST", "WBL", "WBR")

PITCH_TYPE_NAMES = {
    1: "天然草皮",
    2: "人造草皮（软）",
    3: "人造草皮（硬）",
    4: "砾石、黏土与沙地混合",
    5: "砾石",
    6: "黏土",
    7: "沙地",
    8: "天然与人造混合草皮",
}

CLUB_STATUS_NAMES = {1: "职业", 2: "半职业", 3: "业余"}
SUGAR_DADDY_NAMES = {
    0: "无",
    1: "疯狂烧钱型",
    2: "背景",
    3: "填补亏空型",
    4: "填补亏空型（期待回报）",
}
STADIUM_STATE_NAMES = {1: "非常好", 2: "良好", 6: "一般", 11: "较差", 16: "非常差"}
PITCH_DETERIORATION_NAMES = {1: "慢", 2: "中", 3: "快"}
STADIUM_MATCH_NAMES = {
    0: "不承办比赛",
    1: "全部比赛",
    2: "重大比赛",
    3: "中等比赛",
    4: "小型比赛",
    5: "重大及部分中等比赛",
    6: "小型及部分中等比赛",
}
MONEY_LOANED_FROM_NAMES = {
    1: "银行", 2: "主席", 3: "公司自愿安排", 4: "球员工会",
    5: "政府贷款", 6: "球迷信托贷款", 7: "赠与贷款",
    8: "主席离任时偿还的赠与贷款", 9: "无需偿还的赠与贷款",
    10: "升入顶级联赛时偿还", 11: "立足顶级联赛后偿还",
    12: "俱乐部董事贷款", 13: "到期偿还总额的 PIK 贷款",
    14: "升级时一次性偿还", 15: "其他债务", 16: "未知来源",
}
OTHER_INCOME_TYPE_NAMES = {
    1: "球衣赞助", 2: "市政补助", 3: "球场冠名", 4: "一般赞助",
    5: "独立电视转播协议", 6: "其他收入", 7: "会员收入",
    8: "客场球衣赞助", 9: "附加球衣赞助", 10: "降级补助",
    11: "球衣背部赞助", 12: "短裤赞助", 13: "训练服赞助",
    14: "青年队赞助", 15: "训练基地赞助", 16: "洲际赛事收入",
    17: "股权注资", 18: "商业总收入", 19: "球衣袖口赞助",
    22: "球衣制造商", 23: "球场收入", 24: "未知收入",
}

INCOME_STATEMENT_FIELDS = (
    ("gate_receipts", "门票收入", 0x00),
    ("other", "其他收入", 0x04),
    ("season_tickets", "季票收入", 0x08),
    ("interest", "利息收入", 0x0C),
    ("investments", "投资收入", 0x10),
    ("merchandising", "商品销售", 0x14),
    ("players_sold", "出售球员", 0x18),
    ("sponsorship", "赞助收入", 0x1C),
    ("match_day_income", "比赛日收入", 0x20),
    ("fund_raising", "筹款收入", 0x24),
    ("grants", "补助金", 0x2C),
    ("b_club_income", "B 队收入", 0x30),
    ("prize_money", "赛事奖金", 0x34),
    ("tv_revenue", "电视转播收入", 0x38),
    ("corporate_facilities_income", "商务设施收入", 0x3C),
    ("solidarity_payments", "团结机制补偿", 0x40),
)

EXPENDITURE_STATEMENT_FIELDS = (
    ("other", "其他支出", 0x00),
    ("loan_repayments_and_interest", "贷款还款及利息", 0x04),
    ("league_fines", "联赛罚款", 0x08),
    ("ground_maintenance", "场地维护", 0x0C),
    ("player_wages", "球员工资", 0x10),
    ("staff_wages", "职员工资", 0x14),
    ("bonuses", "奖金", 0x1C),
    ("loyalty_bonuses", "忠诚奖金", 0x20),
    ("transfer_expenditure", "转会支出", 0x24),
    ("vat_tax", "增值税", 0x38),
    ("ni_employer_tax", "雇主国民保险", 0x40),
    ("pension_employee_tax", "雇员养老金税", 0x44),
    ("pension_employer_tax", "雇主养老金税", 0x48),
    ("transfer_tax", "转会税", 0x4C),
    ("internal_transfer_tax", "国内转会税", 0x50),
    ("external_transfer_tax", "国际转会税", 0x54),
    ("other_tax_1", "其他税费一", 0x58),
    ("other_tax_2", "其他税费二", 0x5C),
    ("ticket_entertainment_tax", "门票娱乐税", 0x60),
    ("other_tax", "其他税费", 0x64),
    ("brazilian_ticket_tax", "巴西门票税", 0x68),
    ("foreign_player_transfer_tax", "外籍球员转会税", 0x6C),
    ("domestic_player_transfer_tax", "本国球员转会税", 0x70),
    ("match_day_expenses", "比赛日支出", 0x74),
    ("director_emoluments", "董事报酬", 0x78),
    ("non_football_costs", "非足球成本", 0x7C),
    ("agent_fees", "经纪人费用", 0x80),
    ("youth_setup", "青训体系", 0x84),
    ("scouting_costs", "球探费用", 0x88),
    ("travel_costs", "差旅费", 0x8C),
    ("b_club_expenditure", "B 队支出", 0x90),
)

FINANCE_STATEMENT_PERIODS = (
    "this_month", "last_month", "this_season", "last_season",
)
FINANCE_STATEMENT_MAX_AMOUNT = 2_000_000_000


def validated_finance_income_statement(
    statement: Any,
) -> dict[str, list[dict[str, Any]]]:
    """Keep independently valid native income/expenditure statement groups."""
    if not isinstance(statement, dict):
        return {}
    validated: dict[str, list[dict[str, Any]]] = {}
    for group in ("income", "expenditure"):
        rows = statement.get(group)
        if not isinstance(rows, list) or not rows:
            continue
        valid = True
        for row in rows:
            if not isinstance(row, dict):
                valid = False
                break
            for period in FINANCE_STATEMENT_PERIODS:
                if period not in row:
                    continue
                value = row.get(period)
                if (
                    isinstance(value, bool)
                    or not isinstance(value, int)
                    or value < 0
                    or value > FINANCE_STATEMENT_MAX_AMOUNT
                ):
                    valid = False
                    break
            if not valid:
                break
        if valid:
            validated[group] = rows
    return validated


def valid_finance_income_statement(statement: Any) -> bool:
    """Return whether both native money statement groups are populated and valid."""
    return len(validated_finance_income_statement(statement)) == 2

CLUB_CULTURE_TYPE_NAMES = CLUB_VISION_TYPE_NAMES

CLUB_CULTURE_SOURCE_NAMES = {1: "董事会", 2: "球迷"}
CLUB_CULTURE_REFERENCE_NAMES = {
    3: "俱乐部", 5: "大洲", 9: "国家", 10: "国籍", 25: "赛事",
}
OWNERSHIP_PROMISES = {
    "new_manager": 0x02,
    "new_players": 0x04,
    "more_money": 0x08,
    "promotion": 0x10,
    "new_stadium": 0x20,
}

# FM26 stores these as 54 consecutive 0-100 values keyed by the internal
# attribute IDs 0x0F..0x44. The seven foot/hidden fields are deliberately not
# published as visible attributes.
OUTFIELD_ATTRIBUTE_GROUPS = {
    "技术": {
        0x16: "传球", 0x0F: "传中", 0x14: "盯人", 0x26: "技术", 0x10: "盘带",
        0x18: "抢断", 0x11: "射门", 0x25: "停球", 0x12: "头球", 0x13: "远射",
    },
    "定位球": {
        0x17: "罚点球", 0x2A: "角球", 0x2D: "界外球", 0x32: "任意球",
    },
    "精神": {
        0x29: "才华", 0x23: "防守站位", 0x2C: "工作投入", 0x44: "集中",
        0x21: "决断", 0x37: "领导力", 0x3C: "侵略性", 0x19: "视野",
        0x2B: "团队合作", 0x15: "无球跑动", 0x42: "意志力", 0x3A: "勇敢",
        0x20: "预判", 0x43: "镇定",
    },
    "身体": {
        0x31: "爆发力", 0x36: "弹跳", 0x3D: "灵活", 0x34: "耐力",
        0x39: "平衡", 0x33: "强壮", 0x35: "速度", 0x41: "体质",
    },
}

GOALKEEPER_ATTRIBUTE_GROUPS = {
    "门将": {
        0x2F: "出击(倾向)", 0x16: "传球", 0x1E: "大脚开球", 0x24: "反应",
        0x1C: "拦截传中", 0x30: "拳击球(倾向)", 0x1A: "手控球", 0x1F: "手抛球",
        0x25: "停球", 0x22: "一对一", 0x2E: "意外性", 0x1D: "指挥防守",
        0x1B: "制空范围",
    },
    "精神": OUTFIELD_ATTRIBUTE_GROUPS["精神"],
    "身体": OUTFIELD_ATTRIBUTE_GROUPS["身体"],
    "技术训练": {0x17: "罚点球", 0x26: "技术", 0x32: "任意球"},
}

VISIBLE_ATTRIBUTE_IDS = {
    f"{group}:{name}": attribute_id
    for groups in (OUTFIELD_ATTRIBUTE_GROUPS, GOALKEEPER_ATTRIBUTE_GROUPS)
    for group, fields in groups.items()
    for attribute_id, name in fields.items()
}

REALLOCATION_ATTRIBUTE_LIMITS = {
    "martial_manual_fragment": 12,
    "martial_manual_mid": 15,
    "martial_manual_high": 18,
    "martial_manual_immortal": 20,
}

BREAKTHROUGH_PA_LIMITS = {
    "marrow_cleansing_basic": 130,
    "marrow_cleansing_mid": 145,
    "marrow_cleansing_high": 160,
    "marrow_cleansing_immortal": 180,
    "marrow_cleansing_divine": 200,
}

LATENT_DRAGON_PA_LIMITS = {
    "latent_dragon_basic": 130,
    "latent_dragon_mid": 145,
    "latent_dragon_high": 160,
    "latent_dragon_immortal": 180,
    "latent_dragon_divine": 200,
}
def _is_goalkeeper(reader: Reader, address: int) -> bool:
    positions = reader.bytes(address + reader.layout.player_positions_offset, 15) or b""
    highest = max(positions, default=0)
    return bool(highest and len(positions) > 0 and positions[0] == highest)


GOALKEEPER_BRIBE_ATTRIBUTE_VALUES = {
    0x24: 5,   # 反应 1
    0x22: 5,   # 一对一 1
    0x1D: 5,   # 指挥防守 1
    0x1B: 5,   # 制空范围 1
    0x1C: 5,   # 拦截传中 1
    0x1A: 5,   # 手控球 1
    0x1F: 5,   # 手抛球 1
    0x25: 5,   # 停球 1
    0x36: 5,   # 弹跳 1
    0x23: 5,   # 防守站位 1
    0x16: 5,   # 传球 1
    0x1E: 5,   # 大脚开球 1
    0x31: 5,   # 爆发力 1
    0x3D: 5,   # 灵活 1
    0x39: 5,   # 平衡 1
    0x33: 5,   # 强壮 1
    **{attribute_id: 5 for attribute_id in OUTFIELD_ATTRIBUTE_GROUPS["精神"]},
    0x2F: 100, # 出击 20
}

DOPING_ATTRIBUTE_IDS = tuple(dict.fromkeys(
    attribute_id
    for group in ("技术", "精神", "身体")
    for attribute_id in OUTFIELD_ATTRIBUTE_GROUPS[group]
))
DOPING_GOALKEEPER_ATTRIBUTE_IDS = tuple(dict.fromkeys((
    *(
        attribute_id for attribute_id in GOALKEEPER_ATTRIBUTE_GROUPS["门将"]
        if attribute_id != 0x2E  # 意外性保持不变
    ),
    *OUTFIELD_ATTRIBUTE_GROUPS["精神"],
    *OUTFIELD_ATTRIBUTE_GROUPS["身体"],
)))


def _doped_attribute_block(original: bytes, goalkeeper: bool) -> bytes:
    updated = bytearray(original)
    attribute_ids = DOPING_GOALKEEPER_ATTRIBUTE_IDS if goalkeeper else DOPING_ATTRIBUTE_IDS
    for attribute_id in attribute_ids:
        index = attribute_id - 0x0F
        if updated[index] < 90:
            updated[index] = 90
    return bytes(updated)


def _player_id_at(reader: Reader, address: int) -> int:
    vtable = reader.ptr(address)
    player_rva = vtable - reader.module_base if vtable else 0
    if player_rva in reader.layout.actual_player_vtable_rvas:
        person = address + reader.layout.player_person_offset
    elif player_rva in reader.layout.player_and_non_player_vtable_rvas:
        person = address + reader.layout.player_and_non_player_person_offset
    else:
        return 0
    return int(reader.u32(person + ENTITY_UID) or 0)


def _validated_player_person(reader: Reader, address: int, player_id: int) -> int:
    vtable = reader.ptr(address)
    player_rva = vtable - reader.module_base if vtable else 0
    if player_rva in reader.layout.actual_player_vtable_rvas:
        person = address + reader.layout.player_person_offset
    elif player_rva in reader.layout.player_and_non_player_vtable_rvas:
        person = address + reader.layout.player_and_non_player_person_offset
    else:
        raise ValueError("所选球员对象已经失效")
    if _player_id_at(reader, address) != int(player_id):
        raise ValueError("球员 ID 与当前内存对象不一致")
    return person


def resolve_public_person_address(
    reader: Reader, person: dict[str, Any], person_kind: str,
) -> int:
    """Resolve a public player/staff row to a validated Person address."""
    address = _address(person.get("address"))
    identifier = int(person.get("id") or 0)
    if not address or identifier <= 0:
        raise ValueError("人物地址或 ID 无效，请刷新执教球队")
    if str(person_kind) == "player":
        return _validated_player_person(reader, address, identifier)
    if str(person_kind) == "staff":
        if int(reader.u32(address + ENTITY_UID) or 0) != identifier:
            raise ValueError("职员 ID 与当前内存对象不一致")
        expected_rva = getattr(reader.layout, "staff_person_vtable_rva", None)
        vtable = reader.ptr(address)
        if expected_rva is not None and vtable != reader.module_base + int(expected_rva):
            raise ValueError("所选职员对象已经失效")
        return address
    raise ValueError("不支持的人物类型")


def _resolve_team_address(reader: Reader, team_id: int, team_address: Any = 0) -> int:
    address = _address(team_address)
    team = reader.team(address) if address else None
    if team and int(team.get("id") or 0) == int(team_id):
        return address
    # The native Team table survives fixture turnover and is the authoritative
    # relocation path for a stable FM UID. Match effects previously fell
    # straight through to the fixture pool, which can no longer contain the
    # completed fixture when its pre-match values need restoring.
    directory = database_index_for_reader(reader)
    if directory is not None:
        try:
            candidates = directory.addresses_for_uid("team", int(team_id))
        except (OSError, RuntimeError, TypeError, ValueError):
            candidates = ()
        for candidate in candidates:
            team = reader.team(int(candidate))
            if team and int(team.get("id") or 0) == int(team_id):
                return int(candidate)
    # Team objects are normally stable. If the cached pointer has gone stale,
    # use the already-known fixture pool to relocate the team by its FM UID.
    for fixture_address in scan_fixture_addresses(reader)[0]:
        fixture = parse_fixture(reader, fixture_address)
        if not fixture:
            continue
        for candidate in (int(fixture.home_team), int(fixture.away_team)):
            team = reader.team(candidate)
            if team and int(team.get("id") or 0) == int(team_id):
                return candidate
    raise RuntimeError(f"无法重新定位对手球队（ID {team_id}）")


@contextmanager
def _writable_game_reader():
    """Pair the indexed read session with a short-lived writable handle."""
    with borrow_game_reader() as reader:
        # Page prefetching is valid for read-only scans, but a write followed by
        # a verification read must observe the process immediately. Keep the
        # writable session uncached so stale prefetched pages cannot mask writes.
        if hasattr(reader, "_page_cache"):
            reader._page_cache = None
        pid = int(reader.process.pid)
        module = getattr(reader, "module", None)
        if module is None:
            module = reader.layout.module(reader.process)
        if not module:
            raise RuntimeError(f"{reader.layout.module_name} 尚未加载")
        with open_process(pid, write_memory=True) as writable_process:
            yield reader, writable_process, module


def resolve_goalkeeper_bribe_targets(team_id: int, team_address: Any = 0) -> dict[str, Any]:
    """Resolve opponent goalkeeper UIDs without changing persistent attributes."""
    with borrow_game_reader() as reader:
        resolved_team = _resolve_team_address(reader, int(team_id), team_address)
        goalkeepers = []
        for player in reader.roster(resolved_team):
            address = _address(player.get("address"))
            player_id = int(player.get("id") or 0)
            if address and player_id > 0 and _is_goalkeeper(reader, address):
                goalkeepers.append({
                    "player_id": player_id,
                    "player_name": str(player.get("name") or player_id),
                    "player_address": hex(address),
                })
        if not goalkeepers:
            raise RuntimeError("对手一线队没有可识别的门将")
        team = reader.team(resolved_team) or {}
        return {
            "team_id": int(team_id),
            "team_name": str(team.get("short_name") or team.get("name") or team_id),
            "team_address": hex(resolved_team),
            "players": goalkeepers,
        }


def apply_goalkeeper_bribe_match_state(
    team_id: int, team_address: Any = 0,
) -> dict[str, Any]:
    """Set opponent first-team goalkeepers to minimum match sharpness."""
    with _writable_game_reader() as (reader, process, module):
        pid = int(reader.process.pid)
        layout = reader.layout
        if layout.player_sharpness_offset is None:
            raise RuntimeError("当前游戏版本的比赛状态字段尚未映射完整")
        sharpness_offset = int(layout.player_sharpness_offset)
        resolved_team = _resolve_team_address(reader, int(team_id), team_address)
        goalkeepers = []
        for player in reader.roster(resolved_team):
            address = _address(player.get("address"))
            if address and _is_goalkeeper(reader, address):
                goalkeepers.append((player, address))
        if not goalkeepers:
            raise RuntimeError("对手一线队没有可识别的门将")
        snapshots: list[dict[str, Any]] = []
        try:
            for player, address in goalkeepers:
                original_sharpness = reader.u16(address + sharpness_offset)
                if original_sharpness is None:
                    raise RuntimeError(
                        f"无法读取门将 {player.get('name') or player.get('id')} 的比赛状态"
                    )
                snapshot = {
                    "player_id": int(player["id"]),
                    "player_name": str(player.get("name") or player["id"]),
                    "player_address": hex(address),
                    "original_sharpness": int(original_sharpness),
                }
                snapshots.append(snapshot)
                write_process_memory(process, address + sharpness_offset, struct.pack("<H", 0))
                if reader.u16(address + sharpness_offset) != 0:
                    raise RuntimeError(
                        f"门将 {player.get('name') or player.get('id')} 的比赛状态写入校验失败"
                    )
        except Exception:
            for snapshot in snapshots:
                try:
                    write_process_memory(
                        process,
                        _address(snapshot["player_address"]) + sharpness_offset,
                        struct.pack("<H", int(snapshot["original_sharpness"])),
                    )
                except Exception:
                    pass
            raise
        team = reader.team(resolved_team) or {}
        return {
            "team_id": int(team_id),
            "team_name": str(team.get("short_name") or team.get("name") or team_id),
            "team_address": hex(resolved_team),
            "players": snapshots,
            "process_session": f"{pid}:{layout.key}:{module.base_address:x}",
        }


def maintain_goalkeeper_bribe_match_state(
    team_id: int, team_address: Any, snapshots: list[dict[str, Any]],
) -> dict[str, Any]:
    """Reassert goalkeeper match sharpness without replacing its snapshot."""
    with _writable_game_reader() as (reader, process, module):
        pid = int(reader.process.pid)
        layout = reader.layout
        if layout.player_sharpness_offset is None:
            raise RuntimeError("当前游戏版本的比赛状态字段尚未映射")
        sharpness_offset = int(layout.player_sharpness_offset)
        resolved_team = _resolve_team_address(reader, int(team_id), team_address)
        roster_rows = reader.roster(resolved_team)
        roster = {int(row["id"]): _address(row.get("address")) for row in roster_rows}
        roster_by_name = {
            str(row.get("name")): _address(row.get("address"))
            for row in roster_rows if row.get("name")
        }
        maintained: list[int] = []
        repaired: list[int] = []
        players: list[dict[str, Any]] = []
        errors: list[str] = []
        for stored_snapshot in snapshots:
            snapshot = dict(stored_snapshot)
            player_id = int(snapshot.get("player_id") or 0)
            address = _address(snapshot.get("player_address"))
            if _player_id_at(reader, address) != player_id:
                address = int(roster.get(player_id) or 0)
            if not address and layout.key == "fm24":
                address = int(roster_by_name.get(str(snapshot.get("player_name") or "")) or 0)
                player_id = _player_id_at(reader, address) if address else player_id
            try:
                if not address or _player_id_at(reader, address) != player_id:
                    raise RuntimeError("门将地址或比赛状态快照无效")
                if "original_sharpness" not in snapshot:
                    raise RuntimeError("门将比赛状态快照无效")
                current = reader.u16(address + sharpness_offset)
                if current is None:
                    raise RuntimeError("无法读取门将比赛状态")
                if current != 0:
                    write_process_memory(
                        process, address + sharpness_offset, struct.pack("<H", 0),
                    )
                    repaired.append(player_id)
                if reader.u16(address + sharpness_offset) != 0:
                    raise RuntimeError("门将比赛状态补写校验失败")
                snapshot["player_id"] = player_id
                snapshot["player_address"] = hex(address)
                maintained.append(player_id)
            except Exception as error:
                errors.append(f"{snapshot.get('player_name') or player_id}: {error}")
            players.append(snapshot)
        return {
            "maintained": maintained,
            "repaired": repaired,
            "players": players,
            "errors": errors,
            "team_address": hex(resolved_team),
            "process_session": f"{pid}:{layout.key}:{module.base_address:x}",
        }


def apply_goalkeeper_bribe(team_id: int, team_address: Any = 0) -> dict[str, Any]:
    """Apply the match-day goalkeeper values and return restorable snapshots."""
    with _writable_game_reader() as (reader, process, module):
        pid = int(reader.process.pid)
        layout = reader.layout
        resolved_team = _resolve_team_address(reader, int(team_id), team_address)
        goalkeepers = []
        for player in reader.roster(resolved_team):
            address = _address(player.get("address"))
            if address and _is_goalkeeper(reader, address):
                goalkeepers.append((player, address))
        if not goalkeepers:
            raise RuntimeError("对手一线队没有可识别的门将")
        snapshots: list[dict[str, Any]] = []
        try:
            for player, address in goalkeepers:
                original = reader.bytes(address + layout.player_attributes_offset, 54)
                if not original or len(original) != 54:
                    raise RuntimeError(f"无法读取门将 {player.get('name') or player.get('id')} 的属性")
                updated = bytearray(original)
                for attribute_id, raw_value in GOALKEEPER_BRIBE_ATTRIBUTE_VALUES.items():
                    updated[attribute_id - 0x0F] = raw_value
                write_process_memory(process, address + layout.player_attributes_offset, bytes(updated))
                if reader.bytes(address + layout.player_attributes_offset, 54) != bytes(updated):
                    raise RuntimeError(f"门将 {player.get('name') or player.get('id')} 的属性写入校验失败")
                snapshots.append({
                    "player_id": int(player["id"]),
                    "player_name": str(player.get("name") or player["id"]),
                    "player_address": hex(address),
                    "original_attributes": original.hex(),
                })
        except Exception:
            for snapshot in snapshots:
                try:
                    write_process_memory(
                        process,
                        _address(snapshot["player_address"]) + layout.player_attributes_offset,
                        bytes.fromhex(snapshot["original_attributes"]),
                    )
                except Exception:
                    pass
            raise
        team = reader.team(resolved_team) or {}
        return {
            "team_id": int(team_id),
            "team_name": str(team.get("short_name") or team.get("name") or team_id),
            "team_address": hex(resolved_team),
            "players": snapshots,
            "process_session": f"{pid}:{layout.key}:{module.base_address:x}",
        }


def maintain_goalkeeper_bribe(
    team_id: int, team_address: Any, snapshots: list[dict[str, Any]],
) -> dict[str, Any]:
    """Reassert direct goalkeeper attribute values without replacing snapshots."""
    with _writable_game_reader() as (reader, process, module):
        pid = int(reader.process.pid)
        layout = reader.layout
        resolved_team = _resolve_team_address(reader, int(team_id), team_address)
        roster_rows = reader.roster(resolved_team)
        roster = {int(row["id"]): _address(row.get("address")) for row in roster_rows}
        roster_by_name = {
            str(row.get("name")): _address(row.get("address"))
            for row in roster_rows if row.get("name")
        }
        maintained: list[int] = []
        repaired: list[int] = []
        players: list[dict[str, Any]] = []
        errors: list[str] = []
        for stored_snapshot in snapshots:
            snapshot = dict(stored_snapshot)
            player_id = int(snapshot.get("player_id") or 0)
            address = _address(snapshot.get("player_address"))
            if _player_id_at(reader, address) != player_id:
                address = int(roster.get(player_id) or 0)
            if not address and layout.key == "fm24":
                address = int(roster_by_name.get(str(snapshot.get("player_name") or "")) or 0)
                player_id = _player_id_at(reader, address) if address else player_id
            try:
                original = bytes.fromhex(str(snapshot.get("original_attributes") or ""))
                if (
                    not address
                    or _player_id_at(reader, address) != player_id
                    or len(original) != 54
                ):
                    raise RuntimeError("门将地址或属性快照无效")
                current = reader.bytes(address + layout.player_attributes_offset, 54)
                if not current or len(current) != 54:
                    raise RuntimeError("无法读取门将属性")
                expected = bytearray(current)
                for attribute_id, raw_value in GOALKEEPER_BRIBE_ATTRIBUTE_VALUES.items():
                    expected[attribute_id - 0x0F] = raw_value
                if current != bytes(expected):
                    write_process_memory(
                        process, address + layout.player_attributes_offset, bytes(expected),
                    )
                    repaired.append(player_id)
                if reader.bytes(address + layout.player_attributes_offset, 54) != bytes(expected):
                    raise RuntimeError("门将属性补写校验失败")
                snapshot["player_id"] = player_id
                snapshot["player_address"] = hex(address)
                maintained.append(player_id)
            except Exception as error:
                errors.append(f"{snapshot.get('player_name') or player_id}: {error}")
            players.append(snapshot)
        return {
            "maintained": maintained,
            "repaired": repaired,
            "players": players,
            "errors": errors,
            "team_address": hex(resolved_team),
            "process_session": f"{pid}:{layout.key}:{module.base_address:x}",
        }


def restore_goalkeeper_bribe(
    team_id: int, team_address: Any, snapshots: list[dict[str, Any]],
) -> dict[str, Any]:
    """Restore as many saved goalkeeper blocks as possible, retaining failures for retry."""
    with _writable_game_reader() as (reader, process, module):
        layout = reader.layout
        resolved_team = _resolve_team_address(reader, int(team_id), team_address)
        roster_rows = reader.roster(resolved_team)
        roster = {int(row["id"]): _address(row.get("address")) for row in roster_rows}
        roster_by_name = {
            str(row.get("name")): _address(row.get("address"))
            for row in roster_rows if row.get("name")
        }
        restored: list[int] = []
        remaining: list[dict[str, Any]] = []
        errors: list[str] = []
        for snapshot in snapshots:
            player_id = int(snapshot.get("player_id") or 0)
            address = _address(snapshot.get("player_address"))
            if _player_id_at(reader, address) != player_id:
                address = int(roster.get(player_id) or 0)
            if not address and layout.key == "fm24":
                address = int(roster_by_name.get(str(snapshot.get("player_name") or "")) or 0)
                player_id = _player_id_at(reader, address) if address else player_id
            try:
                if not address or _player_id_at(reader, address) != player_id:
                    raise RuntimeError("球员地址或属性快照无效")
                restored_any = False
                original_hex = str(snapshot.get("original_attributes") or "")
                if original_hex:
                    original = bytes.fromhex(original_hex)
                    if len(original) != 54:
                        raise RuntimeError("球员属性快照无效")
                    write_process_memory(process, address + layout.player_attributes_offset, original)
                    if reader.bytes(address + layout.player_attributes_offset, 54) != original:
                        raise RuntimeError("属性恢复校验失败")
                    restored_any = True
                for key, offset in (
                    ("original_sharpness", layout.player_sharpness_offset),
                    ("original_fitness", layout.player_fitness_offset),
                ):
                    if key not in snapshot:
                        continue
                    if offset is None:
                        raise RuntimeError("当前游戏版本的球员状态字段尚未映射完整")
                    original_value = int(snapshot[key])
                    write_process_memory(
                        process, address + int(offset), struct.pack("<H", original_value),
                    )
                    if reader.u16(address + int(offset)) != original_value:
                        raise RuntimeError("球员状态恢复校验失败")
                    restored_any = True
                if not restored_any:
                    raise RuntimeError("球员快照无效")
                restored.append(player_id)
            except Exception as error:
                remaining.append(snapshot)
                errors.append(f"{snapshot.get('player_name') or player_id}: {error}")
        return {"restored": restored, "remaining": remaining, "errors": errors, "team_address": hex(resolved_team)}


def apply_doping_effect(
    team_id: int, team_address: Any, targets: list[dict[str, Any]], *, all_team: bool = False,
) -> dict[str, Any]:
    """Raise visible attributes to at least 18 and snapshot every changed field."""
    target_ids = {int(row.get("player_id") or row.get("id") or 0) for row in targets}
    target_ids.discard(0)
    if not target_ids and not all_team:
        raise RuntimeError("没有可应用兴奋剂的球员")
    with _writable_game_reader() as (reader, process, module):
        pid = int(reader.process.pid)
        layout = reader.layout
        if layout.player_sharpness_offset is None or layout.player_fitness_offset is None:
            raise RuntimeError("当前游戏版本的比赛状态或体能字段尚未映射完整")
        sharpness_offset = int(layout.player_sharpness_offset)
        fitness_offset = int(layout.player_fitness_offset)
        resolved_team = _resolve_team_address(reader, int(team_id), team_address)
        roster = {int(row["id"]): row for row in reader.roster(resolved_team)}
        if all_team:
            target_ids = set(roster)
        elif layout.key == "fm24":
            # Current FM24 targets use stable person UIDs. Keep the full-name
            # relocation path for assignments saved before that mapping was
            # available, when the session-local object address was the ID.
            by_name: dict[str, list[int]] = {}
            for current_id, row in roster.items():
                by_name.setdefault(str(row.get("name") or ""), []).append(current_id)
            relocated: set[int] = set()
            for target in targets:
                requested_id = int(target.get("player_id") or target.get("id") or 0)
                if requested_id in roster:
                    relocated.add(requested_id)
                    continue
                matches = by_name.get(str(target.get("player_name") or ""), [])
                if len(matches) == 1:
                    relocated.add(matches[0])
            target_ids = relocated
        if not target_ids:
            raise RuntimeError("当前一线队没有可应用兴奋剂的球员")
        missing = target_ids - set(roster)
        if missing:
            raise RuntimeError("无法在当前一线队中定位部分目标球员")
        snapshots: list[dict[str, Any]] = []
        try:
            for player_id in sorted(target_ids):
                player = roster[player_id]
                address = _address(player.get("address"))
                original = reader.bytes(address + layout.player_attributes_offset, 54)
                if not original or len(original) != 54:
                    raise RuntimeError(f"无法读取球员 {player.get('name') or player_id} 的属性")
                original_sharpness = reader.u16(address + sharpness_offset)
                original_fitness = reader.u16(address + fitness_offset)
                if original_sharpness is None or original_fitness is None:
                    raise RuntimeError(
                        f"无法读取球员 {player.get('name') or player_id} 的比赛状态或体能"
                    )
                snapshot = {
                    "player_id": player_id,
                    "player_name": str(player.get("name") or player_id),
                    "player_address": hex(address),
                    "original_attributes": original.hex(),
                    "original_sharpness": int(original_sharpness),
                    "original_fitness": int(original_fitness),
                }
                snapshots.append(snapshot)
                updated = _doped_attribute_block(original, _is_goalkeeper(reader, address))
                write_process_memory(process, address + layout.player_attributes_offset, updated)
                write_process_memory(process, address + sharpness_offset, struct.pack("<H", 10000))
                write_process_memory(process, address + fitness_offset, struct.pack("<H", 10000))
                if reader.bytes(address + layout.player_attributes_offset, 54) != updated:
                    raise RuntimeError(f"球员 {player.get('name') or player_id} 的属性写入校验失败")
                if (
                    reader.u16(address + sharpness_offset) != 10000
                    or reader.u16(address + fitness_offset) != 10000
                ):
                    raise RuntimeError(
                        f"球员 {player.get('name') or player_id} 的比赛状态或体能写入校验失败"
                    )
        except Exception:
            for snapshot in snapshots:
                try:
                    original_hex = str(snapshot.get("original_attributes") or "")
                    if original_hex:
                        write_process_memory(
                            process,
                            _address(snapshot["player_address"]) + layout.player_attributes_offset,
                            bytes.fromhex(original_hex),
                        )
                    write_process_memory(
                        process,
                        _address(snapshot["player_address"]) + sharpness_offset,
                        struct.pack("<H", int(snapshot["original_sharpness"])),
                    )
                    write_process_memory(
                        process,
                        _address(snapshot["player_address"]) + fitness_offset,
                        struct.pack("<H", int(snapshot["original_fitness"])),
                    )
                except Exception:
                    pass
            raise
        team = reader.team(resolved_team) or {}
        return {
            "team_id": int(team_id),
            "team_name": str(team.get("short_name") or team.get("name") or team_id),
            "team_address": hex(resolved_team),
            "players": snapshots,
            "process_session": f"{pid}:{layout.key}:{module.base_address:x}",
        }


def maintain_doping_effect(
    team_id: int, team_address: Any, snapshots: list[dict[str, Any]],
) -> dict[str, Any]:
    """Reassert an active effect without replacing its pre-match snapshots."""
    with _writable_game_reader() as (reader, process, module):
        pid = int(reader.process.pid)
        layout = reader.layout
        if layout.player_sharpness_offset is None or layout.player_fitness_offset is None:
            raise RuntimeError("当前游戏版本的比赛状态或体能字段尚未映射完整")
        sharpness_offset = int(layout.player_sharpness_offset)
        fitness_offset = int(layout.player_fitness_offset)
        resolved_team = _resolve_team_address(reader, int(team_id), team_address)
        roster_rows = reader.roster(resolved_team)
        roster = {int(row["id"]): _address(row.get("address")) for row in roster_rows}
        roster_by_name = {
            str(row.get("name")): _address(row.get("address"))
            for row in roster_rows if row.get("name")
        }
        maintained: list[int] = []
        repaired: list[int] = []
        players: list[dict[str, Any]] = []
        errors: list[str] = []
        for stored_snapshot in snapshots:
            snapshot = dict(stored_snapshot)
            player_id = int(snapshot.get("player_id") or 0)
            address = _address(snapshot.get("player_address"))
            if _player_id_at(reader, address) != player_id:
                address = int(roster.get(player_id) or 0)
            if not address and layout.key == "fm24":
                address = int(roster_by_name.get(str(snapshot.get("player_name") or "")) or 0)
                player_id = _player_id_at(reader, address) if address else player_id
            try:
                if not address or _player_id_at(reader, address) != player_id:
                    raise RuntimeError("球员地址或属性快照无效")
                current = reader.bytes(address + layout.player_attributes_offset, 54)
                if not current or len(current) != 54:
                    raise RuntimeError("无法读取球员属性")
                if not snapshot.get("original_attributes"):
                    # Migrate effects created by the Hook-only build before
                    # overwriting the block for the first time.
                    snapshot["original_attributes"] = current.hex()
                updated = _doped_attribute_block(current, _is_goalkeeper(reader, address))
                if current != updated:
                    write_process_memory(
                        process, address + layout.player_attributes_offset, updated,
                    )
                    repaired.append(player_id)
                sharpness = reader.u16(address + sharpness_offset)
                if sharpness != 10000:
                    write_process_memory(
                        process, address + sharpness_offset, struct.pack("<H", 10000),
                    )
                    if player_id not in repaired:
                        repaired.append(player_id)
                fitness = reader.u16(address + fitness_offset)
                if fitness != 10000:
                    write_process_memory(
                        process, address + fitness_offset, struct.pack("<H", 10000),
                    )
                    if player_id not in repaired:
                        repaired.append(player_id)
                if (
                    reader.bytes(address + layout.player_attributes_offset, 54) != updated
                    or
                    reader.u16(address + sharpness_offset) != 10000
                    or reader.u16(address + fitness_offset) != 10000
                ):
                    raise RuntimeError("比赛状态或体能补写校验失败")
                snapshot["player_id"] = player_id
                snapshot["player_address"] = hex(address)
                maintained.append(player_id)
            except Exception as error:
                errors.append(f"{snapshot.get('player_name') or player_id}: {error}")
            players.append(snapshot)
        return {
            "maintained": maintained,
            "repaired": repaired,
            "players": players,
            "errors": errors,
            "team_address": hex(resolved_team),
            "process_session": f"{pid}:{layout.key}:{module.base_address:x}",
        }


def restore_doping_effect(
    team_id: int, team_address: Any, snapshots: list[dict[str, Any]],
) -> dict[str, Any]:
    # Restore match state and any legacy attribute snapshots, relocating by UID.
    return restore_goalkeeper_bribe(team_id, team_address, snapshots)


def _visible_player_attributes(
    reader: Reader, address: int, raw_attributes: bytes | None = None,
    goalkeeper: bool | None = None,
) -> dict[str, dict[str, int]]:
    raw = (
        raw_attributes
        if raw_attributes is not None
        else reader.bytes(address + reader.layout.player_attributes_offset, 54)
    )
    if not raw or len(raw) != 54:
        return {}
    is_goalkeeper = _is_goalkeeper(reader, address) if goalkeeper is None else goalkeeper
    groups = GOALKEEPER_ATTRIBUTE_GROUPS if is_goalkeeper else OUTFIELD_ATTRIBUTE_GROUPS
    return {
        group: {
            name: max(1, min(20, (
                int(raw[attribute_id - 0x0F]) + reader.layout.attribute_display_bias
            ) // 5))
            for attribute_id, name in fields.items()
            if int(raw[attribute_id - 0x0F]) <= 100
        }
        for group, fields in groups.items()
        if any(int(raw[attribute_id - 0x0F]) <= 100 for attribute_id in fields)
    }


def _display_attribute(raw: int, bias: int = 0) -> int:
    return max(1, min(20, (int(raw) + int(bias)) // 5))


def _raw_attribute_target(current_raw: int, target: int, bias: int = 0) -> int:
    current = _display_attribute(current_raw, bias)
    preserved = int(current_raw) + (int(target) - current) * 5
    if 0 <= preserved <= 100 and _display_attribute(preserved, bias) == int(target):
        return preserved
    raw = int(target) * 5 - int(bias)
    if not 1 <= int(target) <= 20 or not 0 <= raw <= 100:
        raise ValueError("属性目标无法安全换算")
    if _display_attribute(raw, bias) != int(target):
        raise ValueError("属性目标无法安全换算")
    return raw


def _hidden_attribute_name(attribute_key: str) -> str:
    name = str(attribute_key or "").partition(":")[2] or str(attribute_key or "")
    return HIDDEN_ATTRIBUTE_ALIASES.get(name, name)


def _hidden_attribute_location(
    reader: Reader, address: int, person: int, attribute_key: str,
) -> tuple[str, int]:
    name = _hidden_attribute_name(attribute_key)
    if str(attribute_key) == "physical:height":
        offset = reader.layout.player_height_offset
        if offset is None:
            raise RuntimeError("当前游戏版本尚未映射球员身高")
        return "身高", address + int(offset)
    if name in PERSON_HIDDEN_ATTRIBUTE_NAMES:
        offset = reader.layout.person_hidden_attributes_offset
        if offset is None:
            raise RuntimeError("当前游戏版本尚未映射人物隐藏属性")
        return name, person + int(offset) + PERSON_HIDDEN_ATTRIBUTE_NAMES.index(name)
    attribute_id = PLAYER_HIDDEN_ATTRIBUTE_IDS.get(name)
    if attribute_id is None:
        raise ValueError("所选隐藏属性尚未验证内存字段")
    return name, address + reader.layout.player_attributes_offset + attribute_id - 0x0F


def _read_hidden_training_attribute(
    reader: Reader, address: int, person: int, attribute_key: str,
) -> int | None:
    name, value_address = _hidden_attribute_location(
        reader, address, person, attribute_key,
    )
    raw = reader.u8(value_address)
    if name == "身高":
        return int(raw) if 100 <= int(raw) <= 250 else None
    if name in PERSON_HIDDEN_ATTRIBUTE_NAMES:
        return int(raw) if 0 <= int(raw) <= 20 else None
    return (
        _display_attribute(raw, reader.layout.attribute_display_bias)
        if 0 <= int(raw) <= 100 else None
    )


def read_player_training_attributes(targets: list[dict[str, Any]]) -> dict[str, int]:
    if not targets:
        return {}
    values: dict[str, int] = {}
    with borrow_game_reader() as reader:
        layout = reader.layout
        for target in targets:
            focus_id = str(target.get("id") or "")
            player_id = int(target.get("player_id") or 0)
            address = _address(target.get("player_address"))
            attribute_kind = str(target.get("attribute_kind") or "visible")
            attribute_id = int(target.get("attribute_id") or -1)
            if not focus_id or not player_id or not address:
                continue
            try:
                person = _validated_player_person(reader, address, player_id)
                if attribute_kind == "hidden":
                    value = _read_hidden_training_attribute(
                        reader, address, person,
                        str(target.get("attribute_key") or ""),
                    )
                    if value is not None:
                        values[focus_id] = value
                    continue
                if not 0x0F <= attribute_id <= 0x44:
                    continue
                raw = reader.u8(address + layout.player_attributes_offset + attribute_id - 0x0F)
            except (OSError, RuntimeError, TypeError, ValueError):
                continue
            if 0 <= int(raw) <= 100:
                values[focus_id] = _display_attribute(raw, layout.attribute_display_bias)
    return values


def read_player_training_positions(targets: list[dict[str, Any]]) -> dict[str, int]:
    if not targets:
        return {}
    values: dict[str, int] = {}
    with borrow_game_reader() as reader:
        layout = reader.layout
        for target in targets:
            focus_id = str(target.get("id") or "")
            player_id = int(target.get("player_id") or 0)
            address = _address(target.get("player_address"))
            position_key = str(target.get("position_key") or "").upper()
            if (
                not focus_id or not player_id or not address
                or position_key not in POSITION_NAMES
            ):
                continue
            try:
                _validated_player_person(reader, address, player_id)
                raw = reader.bytes(
                    address + layout.player_positions_offset,
                    len(POSITION_NAMES),
                )
            except (OSError, RuntimeError, TypeError, ValueError):
                continue
            if (
                not raw or len(raw) != len(POSITION_NAMES)
                or any(int(value) > 20 for value in raw)
            ):
                continue
            values[focus_id] = int(raw[POSITION_NAMES.index(position_key)])
    return values


def _training_ca_change(
    layout: Any, raw: bytes, positions: bytes, attribute_id: int, amount: int,
) -> dict[str, Any]:
    def formula_unavailable(error: ValueError) -> bool:
        message = str(error)
        return any(text in message for text in (
            "训练属性无法安全读取", "球员公式属性无法安全读取",
            "球员位置数据无法安全读取", "球员目标属性无法安全读取",
        ))
    try:
        if str(getattr(layout, "key", "")).startswith("fm24"):
            return fm24_training_ca_change(
                raw, positions, attribute_id, amount, layout.attribute_display_bias,
            )
        if str(getattr(layout, "key", "")).startswith("fm26"):
            return fm26_training_ca_change(
                raw, positions, attribute_id, amount, layout.attribute_display_bias,
            )
    except ValueError as error:
        if not formula_unavailable(error):
            raise
        # RCA is an optional enhancement.  Keep the original linear CA rule
        # when the live formula inputs are unavailable or invalid.
        return {
            "model": "unverified-linear-fallback",
            "verified": False,
            "position_key": None,
            "recommended_ca_before": None,
            "recommended_ca_after": None,
            "ca_cost": int(amount),
        }
    return {
        "model": "unverified-linear-fallback",
        "verified": False,
        "position_key": None,
        "recommended_ca_before": None,
        "recommended_ca_after": None,
        "ca_cost": int(amount),
    }


def _rca_functions(layout: Any) -> tuple[Any, Any, Any]:
    layout_key = str(getattr(layout, "key", ""))
    if layout_key.startswith("fm24"):
        return (
            fm24_recommended_ca,
            fm24_attribute_ca_milestones,
            fm24_ca_budget_attribute_change,
        )
    if layout_key.startswith("fm26"):
        return (
            fm26_recommended_ca,
            fm26_attribute_ca_milestones,
            fm26_ca_budget_attribute_change,
        )
    raise ValueError("当前游戏版本没有已验证的 CA 属性公式")


def _formula_ca_delta(
    layout: Any, before: bytes, after: bytes, positions: bytes,
) -> dict[str, int | str]:
    recommended_ca, _milestones, _budget = _rca_functions(layout)
    try:
        before_result = recommended_ca(before, positions, layout.attribute_display_bias)
        after_result = recommended_ca(after, positions, layout.attribute_display_bias)
    except ValueError as error:
        if "公式" not in str(error) and "属性无法安全读取" not in str(error):
            raise
        return {
            "model": "unverified-no-ca-formula",
            "position_key": None,
            "recommended_ca_before": None,
            "recommended_ca_after": None,
            "ca_change": 0,
        }
    return {
        "model": str(before_result["model"]),
        "position_key": int(before_result["position_key"]),
        "recommended_ca_before": int(before_result["recommended_ca"]),
        "recommended_ca_after": int(after_result["recommended_ca"]),
        "ca_change": (
            int(after_result["recommended_ca"])
            - int(before_result["recommended_ca"])
        ),
    }


def _formula_development_plan(
    layout: Any, raw: bytes, positions: bytes, visible_keys: set[str],
    effect: str, ca_units: int, attribute_key: str | None,
    current_ca: int, current_pa: int,
) -> dict[str, Any]:
    _recommended, _milestones, budget_change = _rca_functions(layout)
    if current_ca >= current_pa:
        raise ValueError("仅可对 CA 低于 PA 的球员使用")
    randomized = effect in {"random_enlightenment", "extremely_unstable_enlightenment"}
    if not randomized:
        if attribute_key not in visible_keys or attribute_key not in VISIBLE_ATTRIBUTE_IDS:
            raise ValueError("所选属性不属于该球员可修改的属性范围")
        keys = [str(attribute_key)]
        directions = (1,)
    else:
        keys = sorted(key for key in visible_keys if key in VISIBLE_ATTRIBUTE_IDS)
        directions = (-1, 1) if effect == "extremely_unstable_enlightenment" else (1,)
    candidates: list[dict[str, Any]] = []
    first_error: ValueError | None = None
    for key in keys:
        attribute_id = VISIBLE_ATTRIBUTE_IDS[key]
        for direction in directions:
            try:
                plan = dict(budget_change(
                    raw, positions, attribute_id, ca_units, direction,
                    layout.attribute_display_bias,
                ))
            except ValueError as error:
                first_error = first_error or error
                continue
            ca_change = int(plan["ca_change"])
            target_ca = int(current_ca) + ca_change
            if not 1 <= target_ca <= 200 or target_ca > int(current_pa):
                continue
            plan.update({"attribute_key": key, "ca_units": int(ca_units)})
            candidates.append(plan)
    if not candidates:
        if first_error is not None and (
            "公式" in str(first_error) or "属性无法安全读取" in str(first_error)
        ):
            # Compatibility path used before the verified RCA formulas: one
            # CA unit corresponds to one attribute point, bounded by PA.
            fallback_keys = [str(attribute_key)] if not randomized else [
                key for key in keys if key in VISIBLE_ATTRIBUTE_IDS
            ]
            if fallback_keys:
                key = random.choice(fallback_keys) if randomized else fallback_keys[0]
                direction = (
                    random.choice((-1, 1))
                    if effect == "extremely_unstable_enlightenment" else 1
                )
                return {
                    "model": "unverified-linear-fallback",
                    "verified": False,
                    "attribute_key": key,
                    "ca_units": int(ca_units),
                    "attribute_points": int(ca_units) * direction,
                    "ca_change": int(ca_units) * direction,
                }
        if first_error is not None and not randomized:
            raise first_error
        raise ValueError("没有属性能够按所选丹药数量产生合法的 CA 变化")
    return dict(random.choice(candidates) if randomized else candidates[0])


def estimate_player_training_ca(
    player_id: int, player_address: Any, attribute_key: str, target_attribute: int,
) -> dict[str, Any]:
    attribute_id = VISIBLE_ATTRIBUTE_IDS.get(str(attribute_key))
    if attribute_id is None:
        raise ValueError("所选属性尚未验证成长编号")
    pid, _path, layout = select_process_layout()
    address = _address(player_address)
    with open_process(pid) as process:
        module = layout.module(process)
        if not module:
            raise RuntimeError(f"{layout.module_name} 尚未加载")
        reader = Reader(process, module.base_address, layout)
        _validated_player_person(reader, address, int(player_id))
        raw = reader.bytes(address + layout.player_attributes_offset, 54)
        positions = reader.bytes(address + layout.player_positions_offset, 15)
        if not raw or not positions:
            raise ValueError("无法读取球员属性或位置数据")
        current = _display_attribute(
            raw[attribute_id - 0x0F], layout.attribute_display_bias,
        )
        target = int(target_attribute)
        if not current < target <= 20:
            raise ValueError("训练目标必须高于当前属性且不超过20")
        result = _training_ca_change(
            layout, raw, positions, attribute_id, target - current,
        )
        current_ca = reader.u16(address + layout.player_ca_offset)
        current_pa = reader.u16(address + layout.player_pa_offset)
        return {
            **result,
            "attribute_before": current,
            "attribute_after": target,
            "ca_before": current_ca,
            "ca_after": current_ca + int(result["ca_cost"]),
            "pa": current_pa,
        }


def _player_training_ca_snapshot(
    layout: Any, raw: bytes, positions: bytes,
    attributes: dict[str, dict[str, int]],
) -> dict[str, Any]:
    layout_key = str(getattr(layout, "key", ""))
    if layout_key.startswith("fm24"):
        model = FM24_RCA_MODEL
        recommended_ca = fm24_recommended_ca
        training_ca_change = fm24_training_ca_change
    elif layout_key.startswith("fm26"):
        model = FM26_RCA_MODEL
        recommended_ca = fm26_recommended_ca
        training_ca_change = fm26_training_ca_change
    else:
        return {"model": "unverified-linear-fallback", "verified": False, "costs": {}}
    attribute_signature = tuple(
        (
            str(group),
            tuple(sorted(
                (str(name), int(value)) for name, value in values.items()
            )),
        )
        for group, values in sorted(attributes.items())
    )
    cache_key = (
        layout_key, int(getattr(layout, "attribute_display_bias", 0) or 0),
        bytes(raw), bytes(positions), attribute_signature,
    )
    with _PLAYER_TRAINING_CA_CACHE_LOCK:
        cached = _PLAYER_TRAINING_CA_CACHE.get(cache_key)
        if cached is not None:
            _PLAYER_TRAINING_CA_CACHE.move_to_end(cache_key)
            return deepcopy(cached)
    try:
        recommended = recommended_ca(raw, positions, layout.attribute_display_bias)
    except ValueError as error:
        if "公式" not in str(error) and "属性无法安全读取" not in str(error):
            raise
        return {
            "model": "unverified-linear-fallback", "verified": False, "costs": {},
        }
    _recommended, milestones, _budget = _rca_functions(layout)
    costs: dict[str, dict[str, Any]] = {}

    def compact(rows: list[dict[str, int]]) -> list[dict[str, int]]:
        seen: set[int] = set()
        result = []
        for row in rows:
            ca_change = int(row["ca_change"])
            if ca_change == 0 or ca_change in seen:
                continue
            seen.add(ca_change)
            result.append(row)
        return result

    for group, values in attributes.items():
        for name, current in values.items():
            attribute_key = f"{group}:{name}"
            attribute_id = VISIBLE_ATTRIBUTE_IDS.get(attribute_key)
            if attribute_id is None or not 1 <= int(current) < 20:
                continue
            next_change = training_ca_change(
                raw, positions, attribute_id, 1, layout.attribute_display_bias,
            )
            attribute_costs = {
                "ca_affected": bool(next_change.get("ca_affected", True)),
                "next": int(next_change["ca_cost"]),
                "milestones": compact(milestones(
                    raw, positions, attribute_id, 1,
                    layout.attribute_display_bias,
                )),
                "downgrade_milestones": compact(milestones(
                    raw, positions, attribute_id, -1,
                    layout.attribute_display_bias,
                )),
            }
            for target in (14, 16, 18, 20):
                if int(current) >= target:
                    continue
                target_change = training_ca_change(
                    raw, positions, attribute_id, target - int(current),
                    layout.attribute_display_bias,
                )
                attribute_costs[f"to_{target}"] = int(target_change["ca_cost"])
            costs[attribute_key] = attribute_costs
    result = {
        "model": model,
        "verified": True,
        "position_key": int(recommended["position_key"]),
        "recommended_ca": int(recommended["recommended_ca"]),
        "costs": costs,
    }
    with _PLAYER_TRAINING_CA_CACHE_LOCK:
        _PLAYER_TRAINING_CA_CACHE[cache_key] = deepcopy(result)
        _PLAYER_TRAINING_CA_CACHE.move_to_end(cache_key)
        while len(_PLAYER_TRAINING_CA_CACHE) > _PLAYER_TRAINING_CA_CACHE_LIMIT:
            _PLAYER_TRAINING_CA_CACHE.popitem(last=False)
    return result


def plan_attribute_reallocation(
    raw: bytes, changes: dict[str, int], expected: dict[str, int], attribute_limit: int = 12,
    max_points: int = 3, display_bias: int = 0,
) -> tuple[bytes, dict[str, dict[str, int]]]:
    if len(raw) != 54:
        raise ValueError("球员属性块无法安全读取")
    if not isinstance(changes, dict) or not changes:
        raise ValueError("尚未重新分配属性点")
    if set(changes) - set(VISIBLE_ATTRIBUTE_IDS):
        raise ValueError("包含不可修改的属性")
    normalized: dict[str, int] = {}
    removed = 0
    added = 0
    for key, value in changes.items():
        try:
            delta = int(value)
        except (TypeError, ValueError):
            raise ValueError("属性点数无效")
        if not delta or abs(delta) > max_points:
            raise ValueError("单项属性变化无效")
        attribute_id = VISIBLE_ATTRIBUTE_IDS[key]
        raw_index = attribute_id - 0x0F
        if int(raw[raw_index]) > 100:
            raise ValueError(f"{key.split(':', 1)[1]} 无法安全读取")
        current = _display_attribute(raw[raw_index], display_bias)
        if int(expected.get(key, -1)) != current:
            raise ValueError(f"{key.split(':', 1)[1]} 已发生变化，请重新打开面板")
        if current >= attribute_limit:
            raise ValueError(f"{key.split(':', 1)[1]} 原值不低于{attribute_limit}，不能参与分配")
        target = current + delta
        if not 1 <= target <= attribute_limit:
            raise ValueError(f"{key.split(':', 1)[1]} 调整后必须处于1至{attribute_limit}")
        normalized[key] = delta
        removed += max(-delta, 0)
        added += max(delta, 0)
    if removed != added or not 1 <= removed <= max_points:
        raise ValueError(f"扣除与增加点数必须相等，且最多重新分配{max_points}点")
    updated = bytearray(raw)
    before: dict[str, int] = {}
    after: dict[str, int] = {}
    for key, delta in normalized.items():
        raw_index = VISIBLE_ATTRIBUTE_IDS[key] - 0x0F
        before[key] = _display_attribute(raw[raw_index], display_bias)
        updated[raw_index] = int(raw[raw_index]) + delta * 5
        after[key] = _display_attribute(updated[raw_index], display_bias)
        if after[key] != before[key] + delta:
            raise ValueError("属性换算校验失败")
    return bytes(updated), {"before": before, "after": after}


def plan_player_development(
    raw: bytes, current_ca: int, current_pa: int, attribute_key: str | None,
    expected_attribute: int, effect: str, amount: int = 1, display_bias: int = 0,
    training_ca_cost: int | None = None,
    allow_ca_over_pa: bool = False,
    visible_attribute_keys: set[str] | None = None,
    formula_plan: dict[str, Any] | None = None,
) -> tuple[bytes, int, int, dict[str, Any]]:
    if not 1 <= int(current_ca) <= 200 or not 1 <= int(current_pa) <= 200:
        raise ValueError("球员当前 CA/PA 数据不适用")
    if int(current_ca) > int(current_pa) and not (
        effect == "training" and allow_ca_over_pa
    ):
        raise ValueError("球员当前 CA 已超过 PA")
    if not 1 <= int(amount) <= 99:
        raise ValueError("批量使用数量无效")
    if effect in LATENT_DRAGON_PA_LIMITS:
        pa_limit = LATENT_DRAGON_PA_LIMITS[effect]
        if current_pa + amount > pa_limit:
            raise ValueError(f"该品级潜龙丹批量使用后 PA 不能超过{pa_limit}")
        return raw, current_ca, current_pa + amount, {
            "attribute_before": None, "attribute_after": None,
            "ca_before": current_ca, "ca_after": current_ca,
            "pa_before": current_pa, "pa_after": current_pa + amount,
        }
    if effect in BREAKTHROUGH_PA_LIMITS:
        pa_limit = BREAKTHROUGH_PA_LIMITS[effect]
        if current_pa + amount > pa_limit:
            raise ValueError(f"该品级破境丹批量使用后 PA 不能超过{pa_limit}")
        return raw, current_ca + amount, current_pa + amount, {
            "attribute_before": None, "attribute_after": None,
            "ca_before": current_ca, "ca_after": current_ca + amount,
            "pa_before": current_pa, "pa_after": current_pa + amount,
        }
    if len(raw) != 54:
        raise ValueError("球员属性块无法安全读取")
    enlightenment_effects = {
        "enlightenment", "random_enlightenment", "extremely_unstable_enlightenment",
    }
    if effect in enlightenment_effects:
        if int(current_ca) >= int(current_pa):
            raise ValueError("仅可对 CA 低于 PA 的球员使用")
        if not formula_plan:
            raise ValueError("培元丹缺少已验证的 CA 属性公式结算结果")
        selected_key = str(formula_plan.get("attribute_key") or "")
        if selected_key not in VISIBLE_ATTRIBUTE_IDS:
            raise ValueError("培元丹公式结算属性无效")
        if effect == "enlightenment" and selected_key != attribute_key:
            raise ValueError("培元丹公式结算属性与选择不一致")
        raw_index = VISIBLE_ATTRIBUTE_IDS[selected_key] - 0x0F
        before = _display_attribute(raw[raw_index], display_bias)
        if effect == "enlightenment" and int(expected_attribute) != before:
            raise ValueError(f"{selected_key.split(':', 1)[1]} 已发生变化，请重新打开面板")
        attribute_points = int(formula_plan.get("attribute_points") or 0)
        ca_change = int(formula_plan.get("ca_change") or 0)
        if not attribute_points or abs(ca_change) != int(amount):
            raise ValueError("培元丹公式结算结果与使用数量不一致")
        if effect != "extremely_unstable_enlightenment" and ca_change <= 0:
            raise ValueError("培元丹必须产生正向 CA 变化")
        target_ca = int(current_ca) + ca_change
        if not 1 <= target_ca <= int(current_pa):
            raise ValueError("培元丹使用后的 CA 必须处于1至PA")
        updated = bytearray(raw)
        updated[raw_index] = _raw_attribute_target(
            updated[raw_index], before + attribute_points, display_bias,
        )
        after = _display_attribute(updated[raw_index], display_bias)
        if after != before + attribute_points or not 1 <= after <= 20:
            raise ValueError("属性换算校验失败")
        changes = {selected_key: {"before": before, "after": after}}
        return bytes(updated), target_ca, current_pa, {
            "attribute_key": selected_key,
            "attribute_keys": [selected_key],
            "attribute_changes": changes,
            "random_steps": ([{
                "attribute_key": selected_key,
                "ca_direction": 1 if ca_change > 0 else -1,
                "attribute_direction": 1 if attribute_points > 0 else -1,
                "attribute_before": before, "attribute_after": after,
                "ca_after": target_ca,
            }] if effect != "enlightenment" else []),
            "attribute_before": before, "attribute_after": after,
            "attribute_points": attribute_points,
            "ca_before": current_ca, "ca_after": target_ca,
            "ca_cost": ca_change,
            "ca_model": formula_plan.get("model"),
            "ca_model_verified": bool(formula_plan.get("verified")),
            "recommended_ca_before": formula_plan.get("recommended_ca_before"),
            "recommended_ca_after": formula_plan.get("recommended_ca_after"),
            "ca_exceeds_pa": False, "ca_over_pa_confirmed": False,
            "pa_before": current_pa, "pa_after": current_pa,
        }
    if attribute_key not in VISIBLE_ATTRIBUTE_IDS:
        raise ValueError("所选属性不能修改")
    raw_index = VISIBLE_ATTRIBUTE_IDS[attribute_key] - 0x0F
    if effect == "training":
        if any(value > 100 for value in raw):
            raise ValueError("球员属性块无法安全读取")
    elif int(raw[raw_index]) > 100:
        raise ValueError(f"{attribute_key.split(':', 1)[1]} 无法安全读取")
    current_attribute = _display_attribute(raw[raw_index], display_bias)
    if int(expected_attribute) != current_attribute:
        raise ValueError(f"{attribute_key.split(':', 1)[1]} 已发生变化，请重新打开面板")
    target_attribute = current_attribute + amount
    try:
        target_raw_attribute = _raw_attribute_target(
            raw[raw_index], target_attribute, display_bias,
        )
    except ValueError:
        raise ValueError("使用后所选属性将超出1至20")
    if effect == "training":
        ca_cost = int(amount if training_ca_cost is None else training_ca_cost)
        if ca_cost < 0:
            raise ValueError("训练 CA 成本无效")
        if current_ca + ca_cost > 200:
            raise ValueError("训练增加属性后 CA 将超过200")
        if current_ca + ca_cost > current_pa and not allow_ca_over_pa:
            raise ValueError("训练增加属性后 CA 将超过 PA")
        target_ca, target_pa = current_ca + ca_cost, current_pa
    else:
        raise ValueError("未知的球员成长道具")
    updated = bytearray(raw)
    updated[raw_index] = target_raw_attribute
    if _display_attribute(updated[raw_index], display_bias) != target_attribute:
        raise ValueError("属性换算校验失败")
    return bytes(updated), target_ca, target_pa, {
        "attribute_before": current_attribute,
        "attribute_after": target_attribute,
        "ca_before": current_ca,
        "ca_after": target_ca,
        "ca_cost": target_ca - current_ca,
        "ca_exceeds_pa": target_ca > target_pa,
        "ca_over_pa_confirmed": bool(allow_ca_over_pa),
        "pa_before": current_pa,
        "pa_after": target_pa,
    }


def _memory_value_matches(reader: Reader, address: int, expected: bytes) -> bool:
    if len(expected) == 1:
        read_u8 = getattr(reader, "u8", None)
        if callable(read_u8):
            return read_u8(int(address)) == expected[0]
    elif len(expected) == 2:
        read_u16 = getattr(reader, "u16", None)
        if callable(read_u16):
            return read_u16(int(address)) == struct.unpack("<H", expected)[0]
    read_bytes = getattr(reader, "bytes", None)
    return bool(
        callable(read_bytes)
        and read_bytes(int(address), len(expected)) == expected
    )


def _read_relative_memory_fields(
    reader: Reader, base: int, fields: dict[str, tuple[int, int]],
    *, max_span: int = 0x400,
) -> dict[str, bytes | None]:
    """Read nearby fields in one request, with an exact-read fallback.

    Player and Person editor fields live inside small native objects. One
    bounded snapshot avoids a kernel transition per scalar while the fallback
    preserves the former behavior if a wider region is not fully readable.
    """
    normalized = {
        str(name): (int(offset), int(width))
        for name, (offset, width) in fields.items()
        if int(offset) >= 0 and int(width) > 0
    }
    if not normalized:
        return {}
    first = min(offset for offset, _width in normalized.values())
    last = max(offset + width for offset, width in normalized.values())
    span = last - first
    if 0 < span <= int(max_span):
        block = reader.bytes(int(base) + first, span)
        if block is not None and len(block) == span:
            return {
                name: bytes(block[offset - first:offset - first + width])
                for name, (offset, width) in normalized.items()
            }
    return {
        name: reader.bytes(int(base) + offset, width)
        for name, (offset, width) in normalized.items()
    }


def _restore_memory_changes(
    reader: Reader, process: Any, changes: list[dict[str, Any]], error_message: str,
) -> None:
    failed = False
    for change in reversed(changes):
        try:
            write_process_memory(
                process, int(change["address"]), bytes(change["original"]),
            )
            _invalidate_effect_reader(reader)
            if not _memory_value_matches(
                reader, int(change["address"]), bytes(change["original"]),
            ):
                failed = True
        except Exception:
            failed = True
    if failed:
        raise RuntimeError(error_message)


def _apply_verified_memory_changes(
    reader: Reader, process: Any, changes: list[dict[str, Any]],
    *, write_error: str, rollback_error: str,
) -> None:
    applied: list[dict[str, Any]] = []
    try:
        for change in changes:
            applied.append(change)
            write_process_memory(
                process, int(change["address"]), bytes(change["updated"]),
            )
            _invalidate_effect_reader(reader)
            if not _memory_value_matches(
                reader, int(change["address"]), bytes(change["updated"]),
            ):
                raise RuntimeError(write_error)
    except Exception as error:
        try:
            _restore_memory_changes(reader, process, applied, rollback_error)
        except Exception as rollback_failure:
            raise RuntimeError(rollback_error) from error
        raise


def _write_verified_memory_values(
    reader: Reader, process: Any, values: list[dict[str, Any]], error_message: str,
) -> None:
    for value in values:
        raw = bytes(value["value"])
        write_process_memory(process, int(value["address"]), raw)
        _invalidate_effect_reader(reader)
        if not _memory_value_matches(reader, int(value["address"]), raw):
            raise RuntimeError(error_message)


def develop_player(
    player_id: int, player_address: Any, attribute_key: str | None,
    expected_attribute: int, effect: str, amount: int = 1,
    allow_ca_over_pa: bool = False,
) -> dict[str, Any]:
    pid, path, layout = select_process_layout()
    address = _address(player_address)
    with open_process(pid, write_memory=True) as process:
        module = layout.module(process)
        if not module:
            raise RuntimeError(f"{layout.module_name} 尚未加载")
        reader = Reader(process, module.base_address, layout)
        _validated_player_person(reader, address, player_id)
        original_ca = reader.u16(address + layout.player_ca_offset)
        original_pa = reader.u16(address + layout.player_pa_offset)
        ability_only = (
            effect in LATENT_DRAGON_PA_LIMITS
            or effect in BREAKTHROUGH_PA_LIMITS
        )
        original_raw = b""
        if not ability_only:
            original_raw = reader.bytes(address + layout.player_attributes_offset, 54)
            if not original_raw or len(original_raw) != 54:
                raise ValueError("无法读取球员属性")
        visible_keys = {
            f"{group}:{name}"
            for group, values in _visible_player_attributes(
                reader, address, original_raw,
            ).items()
            for name in values
        } if not ability_only else set()
        randomized = effect in {"random_enlightenment", "extremely_unstable_enlightenment"}
        if not ability_only and not randomized and attribute_key not in visible_keys:
            raise ValueError("所选属性不属于该球员可修改的属性范围")
        enlightenment_effects = {
            "enlightenment", "random_enlightenment", "extremely_unstable_enlightenment",
        }
        positions = b""
        if effect == "training" or effect in enlightenment_effects:
            positions = reader.bytes(address + layout.player_positions_offset, 15)
            if not positions or len(positions) < 15:
                raise ValueError("无法读取球员位置数据")
        training_plan = None
        formula_plan = None
        if effect == "training":
            attribute_id = VISIBLE_ATTRIBUTE_IDS.get(str(attribute_key))
            if attribute_id is None:
                raise ValueError("无法读取训练所需的属性或位置数据")
            training_plan = _training_ca_change(
                layout, original_raw, positions, attribute_id, amount,
            )
        elif effect in enlightenment_effects:
            formula_plan = _formula_development_plan(
                layout, original_raw, positions, visible_keys, effect, amount,
                attribute_key, original_ca, original_pa,
            )
        updated_raw, target_ca, target_pa, audit = plan_player_development(
            original_raw, original_ca, original_pa, attribute_key, expected_attribute, effect, amount,
            layout.attribute_display_bias,
            int(training_plan["ca_cost"]) if training_plan is not None else None,
            allow_ca_over_pa,
            visible_keys,
            formula_plan,
        )
        if training_plan is not None:
            audit.update({
                "ca_model": training_plan["model"],
                "ca_model_verified": bool(training_plan["verified"]),
                "recommended_ca_before": training_plan["recommended_ca_before"],
                "recommended_ca_after": training_plan["recommended_ca_after"],
            })
        restore_attribute_keys: tuple[str, ...] | None
        changes: list[dict[str, Any]] = []
        if effect == "training":
            restore_attribute_keys = None
            changes.append({
                "address": address + layout.player_attributes_offset,
                "original": original_raw,
                "updated": updated_raw,
            })
        elif ability_only:
            restore_attribute_keys = ()
        else:
            restore_attribute_keys = tuple(
                str(key) for key in audit.get("attribute_keys") or (attribute_key,)
            )
            for changed_key in restore_attribute_keys:
                raw_index = VISIBLE_ATTRIBUTE_IDS[changed_key] - 0x0F
                changes.append({
                    "address": address + layout.player_attributes_offset + raw_index,
                    "original": original_raw[raw_index:raw_index + 1],
                    "updated": updated_raw[raw_index:raw_index + 1],
                })
        restore_ca = effect == "training" or target_ca != original_ca
        restore_pa = effect == "training" or target_pa != original_pa
        if restore_ca:
            changes.append({
                "address": address + layout.player_ca_offset,
                "original": struct.pack("<H", int(original_ca)),
                "updated": struct.pack("<H", int(target_ca)),
            })
        if restore_pa:
            changes.append({
                "address": address + layout.player_pa_offset,
                "original": struct.pack("<H", int(original_pa)),
                "updated": struct.pack("<H", int(target_pa)),
            })
        _apply_verified_memory_changes(
            reader, process, changes,
            write_error="写入后的球员成长数据校验失败",
            rollback_error="球员成长数据写入失败且回滚校验异常",
        )
        updated_attributes = (
            None if ability_only
            else _visible_player_attributes(reader, address, updated_raw)
        )
        training_ca = None
        if effect == "training" or effect in enlightenment_effects:
            training_ca = _player_training_ca_snapshot(
                layout, updated_raw, positions, updated_attributes,
            )
        return {
            "player_id": int(player_id),
            "attribute_key": audit.get("attribute_key", attribute_key),
            "attributes": updated_attributes,
            "training_ca": training_ca,
            "ca": target_ca,
            "pa": target_pa,
            "_original_raw": original_raw,
            "_original_ca": original_ca,
            "_original_pa": original_pa,
            "_restore_attribute_keys": restore_attribute_keys,
            "_restore_ca": restore_ca,
            "_restore_pa": restore_pa,
            **audit,
        }


def develop_player_position(
    player_id: int, player_address: Any, position_key: str,
    expected_position: int, amount: int = 1,
) -> dict[str, Any]:
    position = str(position_key or "").upper()
    if position not in POSITION_NAMES:
        raise ValueError("所选训练位置无效")
    delta = int(amount)
    if delta != 1:
        raise ValueError("位置训练每次只能提升1点")
    expected = max(1, int(expected_position))
    target = expected + delta
    if not 1 <= expected < target <= 20:
        raise ValueError("位置熟练度训练目标必须处于1至20")
    pid, _path, layout = select_process_layout()
    address = _address(player_address)
    with open_process(pid, write_memory=True) as process:
        module = layout.module(process)
        if not module:
            raise RuntimeError(f"{layout.module_name} 尚未加载")
        reader = Reader(process, module.base_address, layout)
        _validated_player_person(reader, address, int(player_id))
        positions_address = address + layout.player_positions_offset
        original_positions = reader.bytes(positions_address, len(POSITION_NAMES))
        if (
            not original_positions
            or len(original_positions) != len(POSITION_NAMES)
            or any(int(value) > 20 for value in original_positions)
        ):
            raise ValueError("无法安全读取球员位置数据")
        position_index = POSITION_NAMES.index(position)
        live_value = int(original_positions[position_index])
        live_normalized = max(1, live_value)
        if live_normalized != expected:
            raise ValueError(f"{position} 位置熟练度已发生变化，请重新结算训练")
        original = bytes([live_value])
        updated = bytes([target])
        value_address = positions_address + position_index
        _apply_verified_memory_changes(
            reader, process, [{
                "address": value_address,
                "original": original,
                "updated": updated,
            }],
            write_error="位置熟练度训练写后回读失败",
            rollback_error="位置熟练度训练失败且原值回滚校验异常",
        )
        updated_positions = bytearray(original_positions)
        updated_positions[position_index] = target
        ratings = {
            label: int(value)
            for label, value in zip(POSITION_NAMES, updated_positions)
            if int(value) > 1
        }
        highest = max(ratings.values(), default=0)
        return {
            "player_id": int(player_id), "position_key": position,
            "position_before": live_value, "position_after": target,
            "attribute_before": live_value, "attribute_after": target,
            "position_ratings": ratings, "positions": list(ratings),
            "primary_positions": [
                label for label, value in ratings.items() if value == highest
            ],
            "_original_raw": original, "_updated_raw": updated,
        }


def restore_player_position(
    player_id: int, player_address: Any, position_key: str,
    original_raw: bytes, expected_current_raw: bytes,
) -> None:
    position = str(position_key or "").upper()
    if position not in POSITION_NAMES:
        raise ValueError("所选训练位置无效")
    original = bytes(original_raw)
    expected_current = bytes(expected_current_raw)
    if len(original) != 1 or len(expected_current) != 1:
        raise ValueError("位置熟练度回滚数据无效")
    pid, _path, layout = select_process_layout()
    address = _address(player_address)
    with open_process(pid, write_memory=True) as process:
        module = layout.module(process)
        if not module:
            raise RuntimeError(f"{layout.module_name} 尚未加载")
        reader = Reader(process, module.base_address, layout)
        _validated_player_person(reader, address, int(player_id))
        value_address = (
            address + layout.player_positions_offset
            + POSITION_NAMES.index(position)
        )
        current = reader.bytes(value_address, 1)
        if current != expected_current:
            raise RuntimeError("位置熟练度回滚前校验失败")
        _apply_verified_memory_changes(
            reader, process, [{
                "address": value_address,
                "original": expected_current,
                "updated": original,
            }],
            write_error="位置熟练度回滚写后回读失败",
            rollback_error="位置熟练度回滚失败且恢复当前值异常",
        )


def develop_player_hidden_attribute(
    player_id: int, player_address: Any, attribute_key: str,
    expected_attribute: int, amount: int = 1,
) -> dict[str, Any]:
    delta = int(amount)
    if delta not in {-1, 1}:
        raise ValueError("隐藏属性训练每次只能变化1点")
    expected = int(expected_attribute)
    target = expected + delta
    is_height = str(attribute_key) == "physical:height"
    if is_height and (
        not 100 <= expected <= 250 or not 100 <= target <= 250
    ):
        raise ValueError("球员身高训练目标必须处于100至250厘米")
    if not is_height and (not 1 <= expected <= 20 or not 1 <= target <= 20):
        raise ValueError("隐藏属性训练目标必须处于1至20")
    pid, path, layout = select_process_layout()
    address = _address(player_address)
    with open_process(pid, write_memory=True) as process:
        module = layout.module(process)
        if not module:
            raise RuntimeError(f"{layout.module_name} 尚未加载")
        reader = Reader(process, module.base_address, layout)
        person = _validated_player_person(reader, address, int(player_id))
        name, value_address = _hidden_attribute_location(
            reader, address, person, attribute_key,
        )
        original = reader.bytes(value_address, 1)
        if not original or len(original) != 1:
            raise ValueError("无法读取球员隐藏属性")
        current = (
            int(original[0]) if name in PERSON_HIDDEN_ATTRIBUTE_NAMES or is_height
            else _display_attribute(original[0], layout.attribute_display_bias)
        )
        if current != expected:
            raise ValueError(f"{name} 已发生变化，请重新结算训练")
        raw_target = (
            target if name in PERSON_HIDDEN_ATTRIBUTE_NAMES or is_height
            else target * 5 - int(layout.attribute_display_bias)
        )
        if is_height:
            raw_target_valid = 100 <= raw_target <= 250
        else:
            raw_target_valid = 0 <= raw_target <= 100 and (
                name in PERSON_HIDDEN_ATTRIBUTE_NAMES
                or _display_attribute(raw_target, layout.attribute_display_bias) == target
            )
        if not raw_target_valid:
            raise ValueError(f"{name} 无法安全换算为游戏属性")
        updated = bytes([raw_target])
        _apply_verified_memory_changes(
            reader, process, [{
                "address": value_address,
                "original": original,
                "updated": updated,
            }],
            write_error="隐藏属性训练写后回读失败",
            rollback_error="隐藏属性训练失败且原值回滚校验异常",
        )
        raw_attributes = reader.bytes(
            address + layout.player_attributes_offset, 54,
        ) or b""
        return {
            "player_id": int(player_id),
            "attribute_key": str(attribute_key),
            "attribute_name": name,
            "attribute_before": current,
            "attribute_after": target,
            "hidden_attributes": _player_hidden_attributes(
                reader, address, person, raw_attributes,
            ),
            "ca_affected": False,
            "_original_raw": original,
            "_updated_raw": updated,
        }


def adjust_player_training_condition(
    player_id: int, player_address: Any, sports_multiplier: float,
) -> dict[str, Any]:
    """Apply one verified sports-science fatigue/sharpness/fitness transaction."""
    multiplier = max(0.0, float(sports_multiplier))
    pid, _path, layout = select_process_layout()
    address = _address(player_address)
    offsets = {
        "fatigue": layout.player_fatigue_offset,
        "sharpness": layout.player_sharpness_offset,
        "fitness": layout.player_fitness_offset,
    }
    if any(value is None for value in offsets.values()):
        raise RuntimeError("当前游戏版本尚未完整映射球员身体状态")
    with open_process(pid, write_memory=True) as process:
        module = layout.module(process)
        if not module:
            raise RuntimeError(f"{layout.module_name} 尚未加载")
        reader = Reader(process, module.base_address, layout)
        _validated_player_person(reader, address, int(player_id))
        before = {
            key: int(reader.u16(address + int(offset)))
            for key, offset in offsets.items()
        }
        def clamp_condition(value: int) -> int:
            return max(0, min(10000, int(value)))
        after = {
            "fatigue": clamp_condition(
                before["fatigue"] - round(500 * multiplier)
            ),
            "sharpness": clamp_condition(
                before["sharpness"] + round(500 * multiplier)
            ),
            "fitness": clamp_condition(
                before["fitness"] + round(300 * multiplier)
            ),
        }
        changes = []
        for key, offset in offsets.items():
            original = struct.pack("<H", before[key])
            updated = struct.pack("<H", after[key])
            if original != updated:
                changes.append({
                    "address": address + int(offset),
                    "original": original, "updated": updated,
                })
        _apply_verified_memory_changes(
            reader, process, changes,
            write_error="运动科学身体状态写后回读失败",
            rollback_error="运动科学身体状态写入失败且回滚校验异常",
        )
        return {
            "player_id": int(player_id), "before": before, "after": after,
            "_original": {key: struct.pack("<H", value) for key, value in before.items()},
            "_updated": {key: struct.pack("<H", value) for key, value in after.items()},
        }


def restore_player_training_condition(
    player_id: int, player_address: Any,
    original: dict[str, bytes], expected_current: dict[str, bytes],
) -> None:
    pid, _path, layout = select_process_layout()
    address = _address(player_address)
    offsets = {
        "fatigue": layout.player_fatigue_offset,
        "sharpness": layout.player_sharpness_offset,
        "fitness": layout.player_fitness_offset,
    }
    if any(value is None for value in offsets.values()):
        raise RuntimeError("当前游戏版本尚未完整映射球员身体状态")
    with open_process(pid, write_memory=True) as process:
        module = layout.module(process)
        if not module:
            raise RuntimeError(f"{layout.module_name} 尚未加载")
        reader = Reader(process, module.base_address, layout)
        _validated_player_person(reader, address, int(player_id))
        restores = []
        for key, offset in offsets.items():
            before = bytes(original.get(key) or b"")
            expected = bytes(expected_current.get(key) or b"")
            field_address = address + int(offset)
            if len(before) != 2 or len(expected) != 2:
                raise ValueError("运动科学身体状态回滚令牌无效")
            if not _memory_value_matches(reader, field_address, expected):
                raise RuntimeError("球员身体状态已再次变化，拒绝覆盖恢复")
            restores.append({"address": field_address, "value": before})
        _write_verified_memory_values(
            reader, process, restores, "运动科学身体状态恢复校验失败",
        )


def restore_player_hidden_attribute(
    player_id: int, player_address: Any, attribute_key: str,
    original: bytes, expected_current: bytes | None = None,
) -> None:
    if len(original) != 1 or expected_current is not None and len(expected_current) != 1:
        raise ValueError("原隐藏属性值无效")
    pid, path, layout = select_process_layout()
    address = _address(player_address)
    with open_process(pid, write_memory=True) as process:
        module = layout.module(process)
        if not module:
            raise RuntimeError(f"{layout.module_name} 尚未加载")
        reader = Reader(process, module.base_address, layout)
        person = _validated_player_person(reader, address, int(player_id))
        _name, value_address = _hidden_attribute_location(
            reader, address, person, attribute_key,
        )
        if expected_current is not None and not _memory_value_matches(
            reader, value_address, expected_current,
        ):
            raise RuntimeError("隐藏属性已再次变化，拒绝覆盖恢复")
        _write_verified_memory_values(
            reader, process,
            [{"address": value_address, "value": original}],
            "原隐藏属性恢复校验失败",
        )


def _plan_live_player_attribute_reallocation(
    reader: Reader, layout: Any, address: int, player_id: int,
    changes: dict[str, int], expected: dict[str, int],
    attribute_limit: int, max_points: int,
) -> dict[str, Any]:
    _validated_player_person(reader, address, player_id)
    original = reader.bytes(address + layout.player_attributes_offset, 54)
    if not original:
        raise ValueError("无法读取球员属性")
    visible_keys = {
        f"{group}:{name}"
        for group, values in _visible_player_attributes(
            reader, address, original,
        ).items()
        for name in values
    }
    if set(changes) - visible_keys:
        raise ValueError("包含不属于该球员类型的属性")
    positions = reader.bytes(address + layout.player_positions_offset, 15)
    if not positions or len(positions) < 15:
        raise ValueError("无法读取球员位置数据")
    updated, audit = plan_attribute_reallocation(
        original, changes, expected, attribute_limit, max_points,
        layout.attribute_display_bias,
    )
    formula = _formula_ca_delta(layout, original, updated, positions)
    return {
        "original": original,
        "updated": updated,
        "positions": positions,
        "formula": formula,
        **audit,
    }


def preview_player_attribute_reallocation(
    player_id: int, player_address: Any, changes: dict[str, int], expected: dict[str, int],
    attribute_limit: int = 12, max_points: int = 3,
) -> dict[str, Any]:
    pid, path, layout = select_process_layout()
    address = _address(player_address)
    with open_process(pid) as process:
        module = layout.module(process)
        if not module:
            raise RuntimeError(f"{layout.module_name} 尚未加载")
        reader = Reader(process, module.base_address, layout)
        plan = _plan_live_player_attribute_reallocation(
            reader, layout, address, player_id, changes, expected,
            attribute_limit, max_points,
        )
        formula = plan["formula"]
        change = int(formula["ca_change"])
        return {
            "valid": change == 0,
            "ca_model": formula["model"],
            "recommended_ca_before": formula["recommended_ca_before"],
            "recommended_ca_after": formula["recommended_ca_after"],
            "formula_ca_change": change,
            "before": plan["before"],
            "after": plan["after"],
        }


def reallocate_player_attributes(
    player_id: int, player_address: Any, changes: dict[str, int], expected: dict[str, int],
    attribute_limit: int = 12, max_points: int = 3,
) -> dict[str, Any]:
    pid, path, layout = select_process_layout()
    address = _address(player_address)
    with open_process(pid, write_memory=True) as process:
        module = layout.module(process)
        if not module:
            raise RuntimeError(f"{layout.module_name} 尚未加载")
        reader = Reader(process, module.base_address, layout)
        plan = _plan_live_player_attribute_reallocation(
            reader, layout, address, player_id, changes, expected,
            attribute_limit, max_points,
        )
        original = plan["original"]
        updated = plan["updated"]
        positions = plan["positions"]
        formula = plan["formula"]
        original_ca = reader.u16(address + layout.player_ca_offset)
        original_pa = reader.u16(address + layout.player_pa_offset)
        changed_keys = tuple(str(key) for key in changes)
        memory_changes = []
        for key in changed_keys:
            raw_index = VISIBLE_ATTRIBUTE_IDS[key] - 0x0F
            memory_changes.append({
                "address": address + layout.player_attributes_offset + raw_index,
                "original": original[raw_index:raw_index + 1],
                "updated": updated[raw_index:raw_index + 1],
            })
        _apply_verified_memory_changes(
            reader, process, memory_changes,
            write_error="写入后的属性校验失败",
            rollback_error="属性写入失败且回滚校验异常",
        )
        attributes = _visible_player_attributes(reader, address, updated)
        current_ca = reader.u16(address + layout.player_ca_offset)
        current_pa = reader.u16(address + layout.player_pa_offset)
        if current_ca != original_ca or current_pa != original_pa:
            _restore_memory_changes(
                reader, process, memory_changes,
                "CA/PA 发生变化且原属性回滚校验异常",
            )
            raise RuntimeError("CA/PA 校验发生变化，属性已恢复")
        return {
            "player_id": int(player_id),
            "attributes": attributes,
            "training_ca": _player_training_ca_snapshot(
                layout, updated, positions, attributes,
            ),
            "ca": current_ca,
            "pa": current_pa,
            "_original_raw": original,
            "_original_attribute_keys": changed_keys,
            "ca_model": formula["model"],
            "recommended_ca_before": formula["recommended_ca_before"],
            "recommended_ca_after": formula["recommended_ca_after"],
            "formula_ca_change": formula["ca_change"],
            "before": plan["before"],
            "after": plan["after"],
        }


def plan_fake_marrow_ca(current_ca: int) -> int:
    current_ca = int(current_ca)
    if not 2 <= current_ca <= 200:
        raise ValueError("球员当前 CA 不足2，无法安全减半")
    return current_ca // 2


def apply_fake_marrow_pill(
    player_id: int, player_address: Any,
) -> dict[str, Any]:
    pid, _path, layout = select_process_layout()
    address = _address(player_address)
    with open_process(pid, write_memory=True) as process:
        module = layout.module(process)
        if not module:
            raise RuntimeError(f"{layout.module_name} 尚未加载")
        reader = Reader(process, module.base_address, layout)
        _validated_player_person(reader, address, int(player_id))
        original = reader.bytes(address + layout.player_attributes_offset, 54)
        if not original or len(original) != 54:
            raise ValueError("无法读取球员属性")
        original_ca = reader.u16(address + layout.player_ca_offset)
        original_pa = reader.u16(address + layout.player_pa_offset)
        if not 1 <= int(original_ca or 0) <= 200 or not 1 <= int(original_pa or 0) <= 200:
            raise RuntimeError("无法安全读取球员当前 CA/PA")
        target_ca = plan_fake_marrow_ca(int(original_ca))
        memory_changes = [{
            "address": address + layout.player_ca_offset,
            "original": struct.pack("<H", int(original_ca)),
            "updated": struct.pack("<H", target_ca),
        }]
        _apply_verified_memory_changes(
            reader, process, memory_changes,
            write_error="洗髓丹（赝品）效果写入校验失败",
            rollback_error="洗髓丹（赝品）效果写入失败且原CA回滚校验异常",
        )
        _invalidate_effect_reader(reader)
        if reader.u16(address + layout.player_pa_offset) != original_pa:
            try:
                _restore_memory_changes(
                    reader, process, memory_changes,
                    "PA 发生变化且原CA回滚校验异常",
                )
            except Exception as rollback_failure:
                raise RuntimeError("PA 发生变化且原CA回滚校验异常") from rollback_failure
            raise RuntimeError("PA 校验发生变化，原CA已恢复")
        attributes = _visible_player_attributes(reader, address, original)
        return {
            "player_id": int(player_id),
            "attributes": attributes,
            "ca": target_ca, "pa": int(original_pa),
            "ca_before": int(original_ca), "ca_after": target_ca,
            "_original_raw": b"",
            "_original_ca": int(original_ca),
            "_original_pa": int(original_pa),
        }


def restore_player_attribute_block(
    player_id: int, player_address: Any, original: bytes,
    attribute_keys: tuple[str, ...] | None = None,
) -> None:
    if len(original) != 54:
        raise ValueError("原属性块无效")
    pid, path, layout = select_process_layout()
    address = _address(player_address)
    with open_process(pid, write_memory=True) as process:
        module = layout.module(process)
        if not module:
            raise RuntimeError(f"{layout.module_name} 尚未加载")
        reader = Reader(process, module.base_address, layout)
        _validated_player_person(reader, address, player_id)
        if attribute_keys is None:
            values = [{
                "address": address + layout.player_attributes_offset,
                "value": original,
            }]
        else:
            values = []
            for key in attribute_keys:
                if key not in VISIBLE_ATTRIBUTE_IDS:
                    raise ValueError("原属性字段无效")
                raw_index = VISIBLE_ATTRIBUTE_IDS[key] - 0x0F
                values.append({
                    "address": address + layout.player_attributes_offset + raw_index,
                    "value": original[raw_index:raw_index + 1],
                })
        _write_verified_memory_values(
            reader, process, values, "原属性恢复校验失败",
        )


def restore_player_development(
    player_id: int, player_address: Any, original_raw: bytes, original_ca: int, original_pa: int,
    *, attribute_keys: tuple[str, ...] | None = None,
    restore_ca: bool = True, restore_pa: bool = True,
) -> None:
    if len(original_raw) not in {0, 54} or attribute_keys is None and not original_raw:
        raise ValueError("原球员成长数据无效")
    pid, path, layout = select_process_layout()
    address = _address(player_address)
    with open_process(pid, write_memory=True) as process:
        module = layout.module(process)
        if not module:
            raise RuntimeError(f"{layout.module_name} 尚未加载")
        reader = Reader(process, module.base_address, layout)
        _validated_player_person(reader, address, player_id)
        values = []
        if attribute_keys is None:
            values.append({
                "address": address + layout.player_attributes_offset,
                "value": original_raw,
            })
        else:
            for key in attribute_keys:
                if key not in VISIBLE_ATTRIBUTE_IDS:
                    raise ValueError("原球员成长属性字段无效")
                raw_index = VISIBLE_ATTRIBUTE_IDS[key] - 0x0F
                values.append({
                    "address": address + layout.player_attributes_offset + raw_index,
                    "value": original_raw[raw_index:raw_index + 1],
                })
        if restore_ca:
            values.append({
                "address": address + layout.player_ca_offset,
                "value": struct.pack("<H", int(original_ca)),
            })
        if restore_pa:
            values.append({
                "address": address + layout.player_pa_offset,
                "value": struct.pack("<H", int(original_pa)),
            })
        _write_verified_memory_values(
            reader, process, values, "原球员成长数据恢复校验失败",
        )


def remove_player_injury(player_id: int, player_address: Any) -> dict[str, Any]:
    pid, _path, layout = select_process_layout()
    address = _address(player_address)
    with open_process(pid, write_memory=True) as process:
        module = layout.module(process)
        if not module:
            raise RuntimeError(f"{layout.module_name} 尚未加载")
        reader = Reader(process, module.base_address, layout)
        _validated_player_person(reader, address, player_id)
        injury_container = reader.ptr(address + _injury_list_offset(layout))
        if not injury_container or injury_container <= 0xFFFFF:
            raise ValueError("该球员当前没有伤病")
        original = reader.bytes(injury_container, 24)
        if not original or len(original) != 24:
            raise ValueError("无法安全读取该球员的伤病数据")
        injury_begin, injury_end = struct.unpack_from("<QQ", original)
        if not injury_begin or not injury_end or injury_end <= injury_begin:
            raise ValueError("该球员当前没有伤病")
        write_process_memory(process, injury_container, b"\0" * 24)
        if reader.bytes(injury_container, 24) != b"\0" * 24:
            write_process_memory(process, injury_container, original)
            raise RuntimeError("清除伤病后的数据校验失败")
        return {
            "player_id": int(player_id), "injury_container": hex(injury_container),
            "_original_injury": original,
        }


def restore_player_injury(player_address: Any, injury_container: Any, original: bytes) -> None:
    pid, _path, layout = select_process_layout()
    container = _address(injury_container)
    if not container or len(original) != 24:
        return
    with open_process(pid, write_memory=True) as process:
        module = layout.module(process)
        if not module:
            raise RuntimeError(f"{layout.module_name} 尚未加载")
        write_process_memory(process, container, original)
        reader = Reader(process, module.base_address, layout)
        if reader.bytes(container, len(original)) != original:
            raise RuntimeError("伤病数据恢复校验失败")


def _person_hidden_attributes(
    reader: Reader, person: int, raw_attributes: bytes | None = None,
) -> dict[str, int]:
    offset = reader.layout.person_hidden_attributes_offset
    if offset is None:
        return {}
    raw = (
        raw_attributes
        if raw_attributes is not None
        else reader.bytes(person + offset, 8)
    )
    if not raw or len(raw) != 8 or any(value > 20 for value in raw):
        return {}
    return {
        name: int(value)
        for name, value in zip(PERSON_HIDDEN_ATTRIBUTE_NAMES, raw)
    }


def _player_hidden_attributes(
    reader: Reader, address: int, person: int,
    raw_attributes: bytes | None = None,
    raw_person_attributes: bytes | None = None,
) -> dict[str, int]:
    values = _person_hidden_attributes(
        reader, person, raw_attributes=raw_person_attributes,
    )
    raw = (
        raw_attributes
        if raw_attributes is not None
        else reader.bytes(address + reader.layout.player_attributes_offset, 54)
    ) or b""
    if len(raw) != 54:
        return values
    for name, attribute_id in PLAYER_HIDDEN_ATTRIBUTE_IDS.items():
        raw_value = int(raw[attribute_id - 0x0F])
        if raw_value <= 100:
            values[name] = _display_attribute(
                raw_value, reader.layout.attribute_display_bias,
            )
    return values


def _decode_person_birth_date_raw(layout: Any, raw: bytes | None) -> date | None:
    if not raw or len(raw) != 4:
        return None
    if layout.person_date_of_birth_day_year:
        day_of_year, year = struct.unpack("<HH", raw)
        if not day_of_year or not year or not 1 <= day_of_year <= 366 or not 1800 <= year <= 2200:
            return None
        try:
            value = date(year, 1, 1) + timedelta(days=day_of_year - 1)
        except ValueError:
            return None
        return value if value.year == year else None
    return decode_date(struct.unpack("<I", raw)[0])


def _person_birth_date(reader: Reader, person: int) -> date | None:
    offset = reader.layout.person_date_of_birth_offset
    if offset is None:
        return None
    raw = reader.bytes(person + int(offset), 4)
    decoded = _decode_person_birth_date_raw(reader.layout, raw)
    if raw is not None and len(raw) == 4:
        return decoded
    # Preserve compatibility with narrow Reader adapters that expose only the
    # typed accessors. The real Reader takes the single 4-byte path above.
    if reader.layout.person_date_of_birth_day_year:
        day_of_year = reader.u16(person + int(offset))
        year = reader.u16(person + int(offset) + 2)
        if not day_of_year or not year:
            return None
        return _decode_person_birth_date_raw(
            reader.layout, struct.pack("<HH", day_of_year, year),
        )
    value = reader.u32(person + int(offset))
    return _decode_person_birth_date_raw(
        reader.layout, struct.pack("<I", int(value or 0)),
    )


def _person_birth_date_snapshot(
    reader: Reader, person: int,
) -> tuple[bytes, date]:
    offset = getattr(reader.layout, "person_date_of_birth_offset", None)
    raw = reader.bytes(person + int(offset), 4) if offset is not None else None
    value = _decode_person_birth_date_raw(reader.layout, raw)
    if not raw or len(raw) != 4 or value is None:
        raise RuntimeError("人物出生日期对象校验失败")
    return bytes(raw), value


def plan_age_reversal_ca(current_ca: int) -> int:
    current_ca = int(current_ca)
    if not 16 <= current_ca <= 200:
        raise ValueError("球员当前 CA 不足16，无法安全降低15点")
    return current_ca - 15


def set_player_age(
    player_id: int, player_address: Any, game_date: str, target_age: int,
) -> dict[str, Any]:
    target_age = int(target_age)
    if not 16 <= target_age <= 24:
        raise ValueError("目标年龄仅支持16至24岁")
    current = date.fromisoformat(str(game_date))
    pid, _path, layout = select_process_layout()
    with open_process(pid, write_memory=True) as process:
        module = layout.module(process)
        if not module:
            raise RuntimeError(f"{layout.module_name} 尚未加载")
        reader = Reader(process, module.base_address, layout)
        player = _address(player_address)
        person = _validated_player_person(reader, player, int(player_id))
        offset = layout.person_date_of_birth_offset
        if offset is None:
            raise RuntimeError("当前游戏版本尚未映射球员出生日期")
        before = _person_birth_date(reader, person)
        if not before:
            raise RuntimeError("无法读取球员当前出生日期")
        birthday_passed = (current.month, current.day) >= (before.month, before.day)
        birth_year = current.year - target_age if birthday_passed else current.year - target_age - 1
        try:
            updated = date(birth_year, before.month, before.day)
        except ValueError as error:
            raise ValueError("目标年龄对应年份无法保留该球员的2月29日生日") from error
        day_of_year = (updated - date(updated.year, 1, 1)).days + 1
        encoded = (
            struct.pack("<HH", day_of_year, updated.year)
            if layout.person_date_of_birth_day_year
            else struct.pack("<I", (updated.year << 16) | day_of_year)
        )
        address = person + offset
        original = reader.bytes(address, 4)
        if not original or len(original) != 4:
            raise RuntimeError("无法读取球员出生日期原始数据")
        original_ca = reader.u16(player + layout.player_ca_offset)
        original_pa = reader.u16(player + layout.player_pa_offset)
        if not 1 <= original_ca <= original_pa <= 200:
            raise RuntimeError("无法安全读取球员当前 CA/PA")
        target_ca = plan_age_reversal_ca(original_ca)
        memory_changes = [
            {"address": address, "original": original, "updated": encoded},
            {
                "address": player + layout.player_ca_offset,
                "original": struct.pack("<H", original_ca),
                "updated": struct.pack("<H", target_ca),
            },
        ]
        _apply_verified_memory_changes(
            reader, process, memory_changes,
            write_error="球员返老还童数据写入校验失败",
            rollback_error="球员返老还童数据写入失败且回滚校验异常",
        )
        _invalidate_effect_reader(reader)
        if reader.u16(player + layout.player_pa_offset) != original_pa:
            try:
                _restore_memory_changes(
                    reader, process, memory_changes,
                    "PA 发生变化且返老还童数据回滚校验异常",
                )
            except Exception as rollback_failure:
                raise RuntimeError("PA 发生变化且返老还童数据回滚校验异常") from rollback_failure
            raise RuntimeError("PA 校验发生变化，返老还童数据已恢复")
        return {
            "player_id": int(player_id), "before": before.isoformat(),
            "after": updated.isoformat(), "age": target_age,
            "ca_before": original_ca, "ca_after": target_ca, "pa": original_pa,
            "attribute_changes": {},
            "_original_birth_date": original, "_original_ca": original_ca,
            "_original_pa": original_pa,
        }


def restore_player_birth_date(
    player_id: int, player_address: Any, original: bytes, original_ca: int | None = None,
    original_attributes: bytes | None = None, original_pa: int | None = None,
    attribute_indexes: tuple[int, ...] | None = None,
) -> None:
    if not original or len(original) != 4:
        raise ValueError("原出生日期数据无效")
    if original_attributes is not None and len(original_attributes) != 54:
        raise ValueError("原返老还童属性数据无效")
    pid, _path, layout = select_process_layout()
    with open_process(pid, write_memory=True) as process:
        module = layout.module(process)
        if not module or layout.person_date_of_birth_offset is None:
            raise RuntimeError("当前游戏版本尚未映射球员出生日期")
        reader = Reader(process, module.base_address, layout)
        player = _address(player_address)
        person = _validated_player_person(reader, player, int(player_id))
        values = [{
            "address": person + layout.person_date_of_birth_offset,
            "value": original,
        }]
        if original_ca is not None:
            values.append({
                "address": player + layout.player_ca_offset,
                "value": struct.pack("<H", int(original_ca)),
            })
        if original_attributes and len(original_attributes) == 54:
            if attribute_indexes is None:
                values.append({
                    "address": player + layout.player_attributes_offset,
                    "value": original_attributes,
                })
            else:
                for index in attribute_indexes:
                    if not 0 <= int(index) < 54:
                        raise ValueError("原返老还童属性字段无效")
                    values.append({
                        "address": player + layout.player_attributes_offset + int(index),
                        "value": original_attributes[int(index):int(index) + 1],
                    })
        _write_verified_memory_values(
            reader, process, values, "返老还童原数据恢复校验失败",
        )
        if original_pa is not None:
            _invalidate_effect_reader(reader)
            if reader.u16(player + layout.player_pa_offset) != int(original_pa):
                raise RuntimeError("返老还童回滚时 PA 校验发生变化")


def _invalidate_effect_reader(reader: Reader) -> None:
    invalidate = getattr(reader, "invalidate_prefetch", None)
    if callable(invalidate):
        invalidate()


def _effect_reader_matches(reader: Reader, address: int, expected: bytes) -> bool:
    read_bytes = getattr(reader, "bytes", None)
    if callable(read_bytes):
        return read_bytes(int(address), len(expected)) == expected
    if len(expected) == 1:
        return reader.u8(int(address)) == expected[0]
    if len(expected) == 2:
        return reader.u16(int(address)) == struct.unpack("<H", expected)[0]
    return False


def apply_player_activity_effect(
    player_id: int, player_address: Any, activity: str, *,
    _reader: Reader | None = None, _process: Any = None,
) -> dict[str, Any]:
    activity = str(activity)
    if activity not in {"drinks", "hot_spring", "football_game", "massage", "media_interview"}:
        raise ValueError("活动类型无效")
    with _shared_writable_reader(_reader, _process) as (reader, process):
        layout = reader.layout
        player = _address(player_address)
        _validated_player_person(reader, player, int(player_id))

        changes: list[dict[str, Any]] = []
        if activity in {"drinks", "massage", "media_interview"}:
            offset = layout.player_morale_offset
            if offset is None:
                raise RuntimeError("当前游戏版本尚未映射球员士气")
            address = player + offset
            before = reader.u8(address)
            if before is None or not 0 <= before <= 20:
                raise RuntimeError("球员士气数据无效")
            after = min(20, before + (2 if activity == "media_interview" else 1))
            original = bytes([before])
            encoded = bytes([after])
            field = "morale"
            changes.append({
                "field": field, "address": address, "original": original, "encoded": encoded,
                "before_raw": int(before), "after_raw": int(after),
                "before": int(before), "after": int(after),
            })
        if activity == "hot_spring":
            offset = layout.player_fitness_offset
            if offset is None:
                raise RuntimeError("当前游戏版本尚未映射球员体能")
            address = player + offset
            before = reader.u16(address)
            if before is None or not 0 <= before <= 10000:
                raise RuntimeError("球员体能数据无效")
            after = min(10000, before + 500)
            original = struct.pack("<H", before)
            encoded = struct.pack("<H", after)
            field = "fitness"
            changes.append({
                "field": field, "address": address, "original": original, "encoded": encoded,
                "before_raw": int(before), "after_raw": int(after),
                "before": round(int(before) / 100, 2), "after": round(int(after) / 100, 2),
            })
        elif activity in {"football_game", "massage"}:
            offset = layout.player_sharpness_offset
            if offset is None:
                raise RuntimeError("当前游戏版本尚未映射球员比赛状态")
            address = player + offset
            before = reader.u16(address)
            if before is None or not 0 <= before <= 10000:
                raise RuntimeError("球员比赛状态数据无效")
            after = max(0, before - 500) if activity == "massage" else min(10000, before + 300)
            original = struct.pack("<H", before)
            encoded = struct.pack("<H", after)
            field = "sharpness"
            changes.append({
                "field": field, "address": address, "original": original, "encoded": encoded,
                "before_raw": int(before), "after_raw": int(after),
                "before": round(int(before) / 100, 2), "after": round(int(after) / 100, 2),
            })

        written: list[dict[str, Any]] = []
        try:
            for change in changes:
                write_process_memory(process, int(change["address"]), change["encoded"])
                written.append(change)
                _invalidate_effect_reader(reader)
                if not _effect_reader_matches(
                    reader, int(change["address"]), change["encoded"],
                ):
                    raise RuntimeError("球员活动效果写入校验失败")
        except Exception as error:
            rollback_failed = False
            for change in reversed(written):
                try:
                    write_process_memory(
                        process, int(change["address"]), change["original"],
                    )
                    _invalidate_effect_reader(reader)
                    rollback_failed = rollback_failed or (
                        not _effect_reader_matches(
                            reader, int(change["address"]), change["original"],
                        )
                    )
                except Exception:
                    rollback_failed = True
            if rollback_failed:
                raise RuntimeError("球员活动效果写入失败且回滚校验异常") from error
            raise

        public_changes = [
            {key: value for key, value in change.items() if key not in {"address", "original", "encoded"}}
            for change in changes
        ]
        restores = [(int(change["address"]), change["original"]) for change in changes]
        if len(public_changes) == 1:
            return {**public_changes[0], "_restores": restores}
        return {"field": activity, "effects": public_changes, "_restores": restores}


@contextmanager
def _shared_writable_reader(
    reader: Reader | None = None, process: Any = None,
):
    """Reuse one validated writable process handle for a bounded batch."""
    if reader is not None and process is not None:
        yield reader, process
        return
    pid, _path, layout = select_process_layout()
    with open_process(pid, write_memory=True) as opened_process:
        module = layout.module(opened_process)
        if not module:
            raise RuntimeError(f"{layout.module_name} 尚未加载")
        opened_reader = Reader(opened_process, module.base_address, layout)
        if not hasattr(opened_reader, "layout"):
            opened_reader.layout = layout
        yield opened_reader, opened_process


def apply_canteen_sea_cucumber_effect(
    player_id: int, player_address: Any, *,
    _reader: Reader | None = None, _process: Any = None,
) -> dict[str, Any]:
    with _shared_writable_reader(_reader, _process) as (reader, process):
        layout = reader.layout
        if layout.player_fitness_offset is None or layout.player_sharpness_offset is None:
            raise RuntimeError("当前游戏版本尚未映射球员体能或比赛状态")
        player = _address(player_address)
        _validated_player_person(reader, player, int(player_id))
        changes = []
        for field, label, offset in (
            ("fitness", "体能", layout.player_fitness_offset),
            ("sharpness", "比赛状态", layout.player_sharpness_offset),
        ):
            address = player + int(offset)
            before = reader.u16(address)
            if before is None or not 0 <= int(before) <= 10000:
                raise RuntimeError(f"球员{label}数据无效")
            changes.append({
                "field": field,
                "label": label,
                "address": address,
                "before_raw": int(before),
                "original": struct.pack("<H", int(before)),
            })

        encoded = struct.pack("<H", 10000)
        written = []
        try:
            for change in changes:
                write_process_memory(process, int(change["address"]), encoded)
                written.append(change)
                _invalidate_effect_reader(reader)
                if reader.u16(int(change["address"])) != 10000:
                    raise RuntimeError(f"球员{change['label']}写入校验失败")
        except Exception as error:
            rollback_failed = False
            for change in reversed(written):
                try:
                    write_process_memory(
                        process, int(change["address"]), change["original"],
                    )
                    _invalidate_effect_reader(reader)
                    rollback_failed = rollback_failed or (
                        not _effect_reader_matches(
                            reader, int(change["address"]), change["original"],
                        )
                    )
                except Exception:
                    rollback_failed = True
            if rollback_failed:
                raise RuntimeError("海参效果写入失败且回滚校验异常") from error
            raise

        public_effects = {
            str(change["field"]): {
                "field": str(change["field"]),
                "before_raw": int(change["before_raw"]),
                "after_raw": 10000,
                "before": round(int(change["before_raw"]) / 100, 2),
                "after": 100.0,
            }
            for change in changes
        }
        return {
            "field": "canteen_sea_cucumber",
            **public_effects,
            "_restores": [
                (int(change["address"]), change["original"])
                for change in changes
            ],
        }


def restore_player_activity_effect(
    result: dict[str, Any], *,
    _reader: Reader | None = None, _process: Any = None,
) -> None:
    restores = list(result.get("_restores") or [])
    if not restores:
        address = int(result.get("_address") or 0)
        original = result.get("_original")
        restores = [(address, original)] if address and isinstance(original, bytes) else []
    if not restores:
        return
    with _shared_writable_reader(_reader, _process) as (reader, process):
        rollback_failed = False
        for address, original in restores:
            if not int(address) or not isinstance(original, bytes):
                continue
            try:
                write_process_memory(process, int(address), original)
                _invalidate_effect_reader(reader)
                rollback_failed = rollback_failed or (
                    not _effect_reader_matches(reader, int(address), original)
                )
            except Exception:
                rollback_failed = True
        if rollback_failed:
            raise RuntimeError("球员效果恢复写后回读不一致")


def _fm24_unhappiness_records(
    reader: Reader, person: int, current: date | None,
) -> tuple[int, list[dict[str, Any]]]:
    contract = reader.ptr(person + FM24_PERSON_CONTRACT)
    if not contract or reader.ptr(contract + 0x08) != person or not reader.ptr(contract + 0x10):
        raise RuntimeError("球员主合同结构无效")
    container = reader.ptr(contract + FM24_UNHAPPINESS_CONTAINER)
    if not container:
        return contract, []
    count = reader.u32(container)
    records = reader.ptr(container + 0x08)
    if count is None or not 0 <= int(count) <= FM24_UNHAPPINESS_LIMIT:
        raise RuntimeError("FM24 球员不满记录数量无效")
    if not count:
        return contract, []
    if not records:
        raise RuntimeError("FM24 球员不满记录地址无效")
    result = []
    for index in range(int(count)):
        address = records + index * FM24_UNHAPPINESS_RECORD_SIZE
        init_raw = reader.u32(address)
        end_raw = reader.u32(address + 0x04)
        init_date = decode_date(init_raw or 0)
        end_date = decode_date(end_raw or 0)
        if not init_date or not end_date:
            raise RuntimeError("FM24 球员不满日期结构无效")
        active = end_date == date(1900, 1, 1) or bool(current and end_date > current)
        result.append({
            "address": address, "init_date": init_date, "end_date": end_date,
            "end_raw": int(end_raw), "active": active,
        })
    return contract, result


def _person_has_unhappiness(
    reader: Reader, person: int, current: date | None,
) -> bool:
    if reader.layout.key == "fm24":
        _contract, records = _fm24_unhappiness_records(reader, person, current)
        return any(record["active"] for record in records)
    if reader.layout.key != "fm26":
        raise RuntimeError("当前游戏版本尚未验证球员不满结构")
    contract = reader.ptr(person + PERSON_CONTRACT)
    if not contract or reader.ptr(contract + 0x08) != person or not reader.ptr(contract + 0x10):
        raise RuntimeError("球员主合同结构无效")
    return bool(reader.ptr(contract + 0x30))


def _safe_person_has_unhappiness(
    reader: Reader, person: int, current: date | None,
) -> bool | None:
    try:
        return _person_has_unhappiness(reader, person, current)
    except (TypeError, ValueError, RuntimeError):
        return None


def player_has_unhappiness(player_id: int, player_address: Any) -> bool:
    pid, _path, layout = select_process_layout()
    if layout.key not in {"fm24", "fm26"}:
        raise RuntimeError("当前游戏版本尚未验证球员不满结构")
    with open_process(pid) as process:
        module = layout.module(process)
        if not module:
            raise RuntimeError(f"{layout.module_name} 尚未加载")
        reader = Reader(process, module.base_address, layout)
        player = _address(player_address)
        person = _validated_player_person(reader, player, int(player_id))
        current = decode_date(
            reader.u32(module.base_address + layout.game_date_rva) or 0
        ) if layout.game_date_rva is not None else None
        return _person_has_unhappiness(reader, person, current)


def apply_psychological_counseling_effect(
    player_id: int, player_address: Any, outcome: str,
) -> dict[str, Any]:
    outcome = str(outcome)
    if outcome not in {"unlocked", "relieved", "opened_up", "no_effect", "rupture"}:
        raise ValueError("心理辅导结果无效")
    pid, _path, layout = select_process_layout()
    if layout.key not in {"fm24", "fm26"}:
        raise RuntimeError("当前游戏版本尚未验证球员不满结构")
    with open_process(pid, write_memory=True) as process:
        module = layout.module(process)
        if not module:
            raise RuntimeError(f"{layout.module_name} 尚未加载")
        reader = Reader(process, module.base_address, layout)
        player = _address(player_address)
        person = _validated_player_person(reader, player, int(player_id))

        changes: list[dict[str, Any]] = []
        if outcome in {"unlocked", "relieved", "opened_up"}:
            if layout.key == "fm24":
                if layout.game_date_rva is None:
                    raise RuntimeError("FM24 当前游戏日期地址无效")
                current_raw = reader.u32(module.base_address + layout.game_date_rva)
                current = decode_date(current_raw or 0)
                if not current:
                    raise RuntimeError("无法读取 FM24 当前游戏日期")
                _contract, records = _fm24_unhappiness_records(reader, person, current)
                active_records = [record for record in records if record["active"]]
                if not active_records:
                    raise ValueError("该球员当前没有可清除的不满")
                encoded_end = struct.pack(
                    "<I", (current.year << 16) | current.timetuple().tm_yday,
                )
                for record in active_records:
                    end_address = int(record["address"]) + 0x04
                    original = reader.bytes(end_address, 4)
                    if not original or len(original) != 4:
                        raise RuntimeError("无法读取 FM24 球员不满结束日期")
                    changes.append({
                        "field": "unhappiness", "address": end_address,
                        "original": original, "encoded": encoded_end,
                        "before": True, "after": False,
                    })
            else:
                contract = reader.ptr(person + PERSON_CONTRACT)
                if not contract or reader.ptr(contract + 0x08) != person or not reader.ptr(contract + 0x10):
                    raise RuntimeError("球员主合同结构无效")
                unhappiness_address = contract + 0x30
                original = reader.bytes(unhappiness_address, 8)
                if not original or len(original) != 8 or struct.unpack("<Q", original)[0] == 0:
                    raise ValueError("该球员当前没有可清除的不满")
                changes.append({
                    "field": "unhappiness", "address": unhappiness_address,
                    "original": original, "encoded": b"\x00" * 8,
                    "before": True, "after": False,
                })
        morale_delta = 2 if outcome == "relieved" else -1 if outcome == "rupture" else 0
        if morale_delta:
            offset = layout.player_morale_offset
            if offset is None:
                raise RuntimeError("当前游戏版本尚未映射球员士气")
            address = player + offset
            before = reader.u8(address)
            if before is None or not 0 <= before <= 20:
                raise RuntimeError("球员士气数据无效")
            after = max(0, min(20, before + morale_delta))
            changes.append({
                "field": "morale", "address": address,
                "original": bytes([before]), "encoded": bytes([after]),
                "before_raw": int(before), "after_raw": int(after),
                "before": int(before), "after": int(after),
            })

        written: list[dict[str, Any]] = []
        try:
            for change in changes:
                write_process_memory(process, int(change["address"]), change["encoded"])
                written.append(change)
                if reader.bytes(int(change["address"]), len(change["encoded"])) != change["encoded"]:
                    raise RuntimeError("心理辅导效果写入校验失败")
        except Exception:
            for change in reversed(written):
                write_process_memory(process, int(change["address"]), change["original"])
            raise

        public_changes = [
            {key: value for key, value in change.items() if key not in {"address", "original", "encoded"}}
            for change in changes
        ]
        return {
            "field": "psychological_counseling", "effects": public_changes,
            "_restores": [(int(change["address"]), change["original"]) for change in changes],
        }


def read_team_roster(team_address: Any) -> list[dict[str, Any]]:
    with borrow_game_reader() as reader:
        address = _address(team_address)
        if not address or not reader.team(address):
            raise ValueError("无法定位对手球队")
        return [
            {
                "id": int(row["id"]), "name": str(row.get("name") or row["id"]),
                "address": str(row.get("address") or ""),
                "positions": list((row.get("positions") or {}).keys()),
                "availability": dict(row.get("availability") or {}),
            }
            for row in reader.roster(address)
        ]


def resolve_team_player_address(
    team_id: int, team_address: Any, player_id: int, player_address: Any = 0,
    *, _reader: Reader | None = None, _roster_cache: dict | None = None,
) -> str | None:
    """Resolve one roster member without expanding every player's profile."""
    with (nullcontext(_reader) if _reader is not None else borrow_game_reader()) as reader:
        team = _address(team_address)
        if not team:
            raise ValueError("无法定位当前执教球队")
        team_header_size = TEAM_ROSTER_END + 8
        team_header = reader.bytes(team, team_header_size)
        if not team_header or len(team_header) != team_header_size:
            raise ValueError("执教球队对象已经失效")
        team_vtable = struct.unpack_from("<Q", team_header, 0)[0]
        valid_team_vtables = {
            reader.module_base + int(reader.layout.team_vtable_rva),
            reader.module_base + int(reader.layout.national_team_vtable_rva),
        }
        if (
            team_vtable not in valid_team_vtables
            or struct.unpack_from("<I", team_header, ENTITY_UID)[0] != int(team_id)
        ):
            raise ValueError("执教球队对象已经失效")
        begin = struct.unpack_from("<Q", team_header, TEAM_ROSTER_BEGIN)[0]
        end = struct.unpack_from("<Q", team_header, TEAM_ROSTER_END)[0]
        if not begin or not end or end <= begin or (end - begin) % 8:
            return None
        count = (end - begin) // 8
        if not 1 <= count <= 200:
            return None
        cache_key = (int(team_id), team)
        signature = (team_vtable, begin, end)
        cached_roster = (_roster_cache or {}).get(cache_key)
        if cached_roster and cached_roster[0] == signature:
            roster_addresses = cached_roster[1]
        else:
            roster_addresses = [
                int(address or 0) for address in reader.ptr_array(begin, count)
            ]
            if _roster_cache is not None:
                _roster_cache[cache_key] = (signature, roster_addresses)
        cached_address = _address(player_address)
        candidates = (
            [cached_address] + [address for address in roster_addresses if address and address != cached_address]
            if cached_address and cached_address in roster_addresses else [address for address in roster_addresses if address]
        )
        player_header_size = max(
            8,
            int(reader.layout.player_person_offset) + ENTITY_UID + 4,
            int(reader.layout.player_and_non_player_person_offset) + ENTITY_UID + 4,
        )
        for candidate in candidates:
            raw = reader.bytes(candidate, player_header_size)
            if not raw or len(raw) != player_header_size:
                continue
            player_vtable = struct.unpack_from("<Q", raw, 0)[0]
            player_rva = player_vtable - reader.module_base
            if player_rva in reader.layout.actual_player_vtable_rvas:
                person_offset = int(reader.layout.player_person_offset)
            elif player_rva in reader.layout.player_and_non_player_vtable_rvas:
                person_offset = int(reader.layout.player_and_non_player_person_offset)
            else:
                continue
            candidate_id = struct.unpack_from(
                "<I", raw, person_offset + ENTITY_UID,
            )[0]
            if candidate_id == int(player_id):
                if _roster_cache is not None:
                    # A roster can replace/reorder pointers without resizing.
                    # Verify just this member's slot; reread the vector only
                    # when it changed, retaining the uncached fallback.
                    slot = begin + roster_addresses.index(candidate) * 8
                    if reader.ptr(slot) != candidate:
                        _roster_cache.pop(cache_key, None)
                        return resolve_team_player_address(
                            team_id, team_address, player_id, player_address,
                            _reader=reader,
                        )
                return hex(candidate)
        return None


def _prefetch_vector_data(
    reader: Reader, header_address: int, *, width: int, limit: int,
) -> None:
    prefetch = getattr(reader, "prefetch", None)
    if not callable(prefetch):
        return
    header = reader.bytes(int(header_address), 24)
    if not header or len(header) != 24:
        return
    begin, end, capacity = struct.unpack("<QQQ", header)
    byte_length = end - begin
    if (
        not begin or end < begin or capacity < end
        or byte_length % int(width) or byte_length // int(width) > int(limit)
    ):
        return
    if byte_length:
        prefetch(int(begin), int(byte_length))


def _prefetch_roster_dependencies(reader: Reader, roster: list[dict[str, Any]]) -> None:
    """Warm bounded, layout-verified child objects used by profile expansion."""
    prefetch = getattr(reader, "prefetch", None)
    if not callable(prefetch):
        return
    layout = reader.layout
    for row in roster:
        try:
            player = int(str(row.get("address") or "0"), 16)
        except (TypeError, ValueError):
            continue
        person = player + (
            int(layout.player_and_non_player_person_offset)
            if row.get("object_type") == "actual_player_and_non_player"
            else int(layout.player_person_offset)
        )
        relationships_offset = layout.person_relationships_offset
        relationships = (
            int(reader.ptr(person + int(relationships_offset)) or 0)
            if relationships_offset is not None else 0
        )
        if relationships:
            prefetch(relationships, 24)
            _prefetch_vector_data(
                reader, relationships, width=16,
                limit=1024 if layout.key == "fm24" else 512,
            )
        if layout.key == "fm24":
            contract_offset = FM24_PERSON_CONTRACT
            bonus_vector_offset = 0x60
            bonus_limit = 64
        elif layout.key == "fm26":
            contract_offset = PERSON_CONTRACT
            bonus_vector_offset = 0x68
            bonus_limit = 1000
        else:
            continue
        contract = int(reader.ptr(person + contract_offset) or 0)
        if contract:
            prefetch(contract, 0xC0)
            _prefetch_vector_data(
                reader, contract + bonus_vector_offset,
                width=8, limit=bonus_limit,
            )
        if layout.key != "fm24":
            _prefetch_vector_data(reader, person + 0xF0, width=8, limit=64)
            continue
        _prefetch_vector_data(reader, person + 0xB0, width=8, limit=64)
        _prefetch_vector_data(reader, person + 0x120, width=8, limit=64)
        _prefetch_vector_data(reader, person + 0x98, width=4, limit=128)


def _person_nationality(reader: Reader, person: int) -> tuple[int | None, str | None, str | None]:
    offset = reader.layout.person_nationality_offset
    nation = reader.ptr(person + offset) if offset is not None else None
    nation_id = reader.u32(nation + ENTITY_UID) if nation else None
    if nation_id not in NATION_NAMES:
        return None, None, None
    return int(nation_id), NATION_NAMES.get(nation_id), NATION_CODES.get(nation_id)


def _game_wage(raw: int | None) -> int:
    """Match FM's compact weekly-wage display in pounds."""
    value = max(0, int(raw or 0))
    if value < 100:
        step = 5
    elif value < 1000:
        step = 25
    elif value < 10000:
        step = 250
    elif value < 100000:
        step = 1000
    else:
        step = 5000
    return int((value + step / 2) // step * step)


def _name(reader: Reader, person: int) -> str | None:
    layout = reader.layout
    common = reader.fm_nested_string_at(person + layout.person_common_name_offset)
    first = reader.fm_nested_string_at(person + layout.person_first_name_offset)
    last = reader.fm_nested_string_at(person + layout.person_last_name_offset)
    full = reader.fm_string_at(person + layout.person_full_name_offset)
    return common or " ".join(part for part in (first, last) if part) or full


def _player_name_signature(reader: Reader, person: int) -> tuple[int, int, int]:
    values = []
    for offset in (
        reader.layout.person_first_name_offset,
        reader.layout.person_last_name_offset,
        reader.layout.person_common_name_offset,
    ):
        entry = reader.ptr(person + offset)
        values.append(int(reader.ptr(entry) or 0) if entry else 0)
    return tuple(values)


def _clean_localized_player_name(value: str | None) -> str | None:
    if not value or "\u200b" in value:
        return None
    cleaned = value.strip()
    for marker in ("〈", "<", "\r", "\n"):
        cleaned = cleaned.split(marker, 1)[0].strip()
    if not cleaned or len(cleaned) > 40:
        return None
    return cleaned


def _apply_localized_player_names(reader: Reader, players: list[dict[str, Any]]) -> None:
    """Resolve FM's localized full-name records in one bounded batch scan."""
    pid = int(reader.process.pid)
    signatures = {
        tuple(int(value) for value in player.get("_name_signature", (0, 0, 0)))
        for player in players
    }
    signatures.discard((0, 0, 0))
    with _CACHE_LOCK:
        unresolved = {
            signature for signature in signatures
            if (pid, signature) not in _LOCALIZED_NAME_CACHE
        }
    if unresolved:
        patterns = {struct.pack("<QQQ", *signature): signature for signature in unresolved}
        pattern_matcher = re.compile(b"|".join(re.escape(pattern) for pattern in patterns))
        resolved: dict[tuple[int, int, int], str] = {}
        # Localized person-name pools are dense 8–16 MiB allocations. Sample
        # each candidate for the repeated record marker first, then read only
        # the actual pools. This also finds names stored far away from their
        # common-name strings (for example Hwang Hee-Chan / 黄喜灿).
        record_marker = b"\xff\xff\xff\xff\x50\x00\x00\x00"
        candidate_regions = []
        for region in iter_readable_regions(reader.process):
            if (
                region.type != MEM_PRIVATE
                or not 8 * 1024 * 1024 <= region.size <= 16 * 1024 * 1024
            ):
                continue
            marker_count = 0
            for offset in range(0, region.size, 1024 * 1024):
                sample = reader.bytes(
                    region.base_address + offset,
                    min(64 * 1024, region.size - offset),
                )
                if sample:
                    marker_count += sample.count(record_marker)
            if marker_count >= 20:
                candidate_regions.append(region)
        for region in candidate_regions:
            block = reader.bytes(region.base_address, region.size)
            if not block:
                continue
            for match in pattern_matcher.finditer(block):
                signature = patterns[match.group(0)]
                if signature in resolved:
                    continue
                localized = _clean_localized_player_name(
                    reader.fm_string_at(region.base_address + match.start() + 0x20)
                )
                if localized:
                    resolved[signature] = localized
        # Some records remain in a smaller pool close to the common-name
        # string instead of the dense global pools. Search those bounded
        # neighborhoods only for players not resolved above.
        nearby_unresolved = unresolved - set(resolved)
        if nearby_unresolved:
            nearby_patterns = {
                struct.pack("<QQQ", *signature): signature
                for signature in nearby_unresolved
            }
            nearby_matcher = re.compile(
                b"|".join(re.escape(pattern) for pattern in nearby_patterns)
            )
            ranges = sorted(
                (
                    signature[2] - 32 * 1024 * 1024,
                    signature[2] + 32 * 1024 * 1024,
                )
                for signature in nearby_unresolved if signature[2]
            )
            merged: list[tuple[int, int]] = []
            for lower, upper in ranges:
                if merged and lower <= merged[-1][1]:
                    merged[-1] = (merged[-1][0], max(merged[-1][1], upper))
                else:
                    merged.append((lower, upper))
            for region in iter_readable_regions(reader.process):
                if region.type != MEM_PRIVATE or not any(
                    region.base_address < upper
                    and region.base_address + region.size > lower
                    for lower, upper in merged
                ):
                    continue
                region_end = region.base_address + region.size
                intersections = [
                    (max(region.base_address, lower), min(region_end, upper))
                    for lower, upper in merged
                    if region.base_address < upper and region_end > lower
                ]
                for lower, upper in intersections:
                    # Read only the part inside the bounded name neighbourhood.
                    # A single intersecting 256 MiB heap used to be read whole.
                    offset = lower
                    carry = b""
                    while offset < upper:
                        length = min(8 * 1024 * 1024, upper - offset)
                        chunk = reader.bytes(offset, length)
                        if not chunk:
                            carry = b""
                            offset += length
                            continue
                        block = carry + chunk
                        block_base = offset - len(carry)
                        for match in nearby_matcher.finditer(block):
                            signature = nearby_patterns[match.group(0)]
                            if signature in resolved:
                                continue
                            localized = _clean_localized_player_name(
                                reader.fm_string_at(block_base + match.start() + 0x20)
                            )
                            if localized:
                                resolved[signature] = localized
                        carry = block[-23:] if len(block) >= 23 else block
                        offset += length
        with _CACHE_LOCK:
            for signature in unresolved:
                _LOCALIZED_NAME_CACHE[(pid, signature)] = resolved.get(signature)
    with _CACHE_LOCK:
        for player in players:
            signature = tuple(int(value) for value in player.pop("_name_signature", (0, 0, 0)))
            localized = _LOCALIZED_NAME_CACHE.get((pid, signature))
            if localized:
                player.setdefault("game_name", str(player.get("name") or ""))
                player["name"] = localized
            elif int(player.get("id") or 0) in LOCALIZED_PLAYER_NAME_FALLBACKS:
                player.setdefault("game_name", str(player.get("name") or ""))
                player["name"] = LOCALIZED_PLAYER_NAME_FALLBACKS[int(player["id"])]


def _apply_ui_localized_staff_names(reader: Reader, staff: list[dict[str, Any]]) -> None:
    """Use FM's live staff-report payload as a localized-name fallback.

    Staff without a common-name record are absent from the localized person-name
    pools used by players. When FM has rendered their report, its transient UI
    payload stores the localized name near the stable staff UID. Read that
    association only; never translate or infer a name ourselves.
    """
    unresolved = {
        int(row.get("id") or 0): row
        for row in staff
        if int(row.get("id") or 0) > 0
    }
    if not unresolved:
        return
    uid_patterns = {
        struct.pack("<I", identifier): identifier
        for identifier in unresolved
    }
    # Serialized report fragment: 0x02 + localized name + 0x01 + "出​生".
    name_pattern = re.compile(
        rb"\x02([^\x00\x01\x02]{3,120})\x01\xe5\x87\xba(?:\xe2\x80\x8b)?\xe7\x94\x9f"
    )
    for region in iter_readable_regions(reader.process):
        if (
            region.type != MEM_PRIVATE
            or not 0x1000 <= region.size <= 8 * 1024 * 1024
        ):
            continue
        block = reader.bytes(region.base_address, region.size)
        if not block:
            continue
        for uid_pattern, identifier in list(uid_patterns.items()):
            cursor = 0
            while True:
                uid_position = block.find(uid_pattern, cursor)
                if uid_position < 0:
                    break
                cursor = uid_position + 1
                window = block[max(0, uid_position - 4096):uid_position]
                matches = list(name_pattern.finditer(window))
                if not matches:
                    continue
                try:
                    localized = matches[-1].group(1).decode("utf-8")
                except UnicodeDecodeError:
                    continue
                localized = _clean_localized_player_name(localized)
                if not localized or not any("\u3400" <= char <= "\u9fff" for char in localized):
                    continue
                unresolved[identifier]["name"] = localized
                uid_patterns.pop(uid_pattern, None)
                break
        if not uid_patterns:
            break


def _find_human_manager(reader: Reader, manager_id: int) -> int:
    cache_key = (int(reader.process.pid), int(reader.module_base), int(manager_id))
    with _CACHE_LOCK:
        cached = int(_HUMAN_MANAGER_CACHE.get(cache_key) or 0)
    if cached and _human_manager_matches(reader, cached, int(manager_id)):
        return cached
    if cached:
        with _CACHE_LOCK:
            _HUMAN_MANAGER_CACHE.pop(cache_key, None)
    directory = database_index_for_reader(reader)
    if directory is not None:
        for address in directory.human_manager_addresses(int(manager_id)):
            if _human_manager_matches(reader, int(address), int(manager_id)):
                with _CACHE_LOCK:
                    _HUMAN_MANAGER_CACHE[cache_key] = int(address)
                return int(address)
    if reader.layout.key != "fm26":
        address = _find_human_manager_by_id(reader, manager_id)
        if address:
            with _CACHE_LOCK:
                _HUMAN_MANAGER_CACHE[cache_key] = address
                stale = [
                    key for key in _HUMAN_MANAGER_CACHE
                    if key[0] != cache_key[0]
                ]
                for key in stale:
                    _HUMAN_MANAGER_CACHE.pop(key, None)
            return address
        raise RuntimeError("无法定位当前人类经理对象")
    needle = struct.pack("<Q", reader.module_base + HUMAN_MANAGER_VTABLE_RVA)
    regions = [region for region in iter_readable_regions(reader.process) if region.type == MEM_PRIVATE and region.size <= 512 * 1024 * 1024]
    regions.sort(key=lambda region: (0 if 0x10000 <= region.size <= 64 * 1024 * 1024 else 1, region.size))
    for region in regions:
        offset, carry = 0, b""
        while offset < region.size:
            length = min(8 * 1024 * 1024, region.size - offset)
            block = reader.bytes(region.base_address + offset, length)
            if block:
                data = carry + block
                base = region.base_address + offset - len(carry)
                position = 0
                while True:
                    position = data.find(needle, position)
                    if position < 0:
                        break
                    address = base + position
                    if reader.u32(address + MANAGER_PERSON + ENTITY_UID) == manager_id:
                        with _CACHE_LOCK:
                            _HUMAN_MANAGER_CACHE[cache_key] = address
                            stale = [key for key in _HUMAN_MANAGER_CACHE if key[0] != cache_key[0]]
                            for key in stale:
                                _HUMAN_MANAGER_CACHE.pop(key, None)
                        return address
                    position += 8
                carry = data[-7:]
            else:
                carry = b""
            offset += length
    raise RuntimeError("无法定位当前人类经理对象")


def _address(value: Any) -> int:
    try:
        return int(str(value), 16) if isinstance(value, str) else int(value or 0)
    except (TypeError, ValueError):
        return 0


def _person_joined_club_offset(reader: Reader) -> int:
    """Return the verified JoinedClubDate offset for the active generation."""
    return (
        FM24_PERSON_JOINED_CLUB
        if str(reader.layout.key).startswith("fm24")
        else FM26_PERSON_JOINED_CLUB
    )


def _context_addresses(
    reader: Reader, manager_id: int, team_id: int,
    team_address: Any = 0, manager_address: Any = 0,
) -> tuple[int, int, int]:
    team_id = int(team_id or 0)
    team = _address(team_address)
    manager = _address(manager_address)
    if not team or team_id <= 0 or reader.u32(team + ENTITY_UID) != team_id:
        team = 0
    if manager and not _human_manager_matches(reader, manager, int(manager_id)):
        manager = 0
    elif manager:
        with _CACHE_LOCK:
            _HUMAN_MANAGER_CACHE[
                (int(reader.process.pid), int(reader.module_base), int(manager_id))
            ] = manager
    if not manager and team:
        candidate = reader.ptr(team + TEAM_MANAGER)
        if (
            candidate and _human_manager_matches(reader, candidate, int(manager_id))
        ):
            manager = candidate
    if not manager:
        manager = _find_human_manager(reader, int(manager_id))
    person = manager + reader.layout.manager_person_offset
    if team_id <= 0:
        return manager, person, 0
    if not team:
        for contract in _person_contract_candidates(reader, person):
            candidate_team = reader.ptr(contract + 0x10)
            if candidate_team and reader.u32(candidate_team + ENTITY_UID) == team_id:
                team = candidate_team
                break
    contract_team, _contract_row = _contract(reader, person)
    if not team:
        team = int(contract_team or 0)
    if not team or reader.u32(team + ENTITY_UID) != team_id:
        raise RuntimeError("经理合同与当前执教球队不一致")
    return manager, person, team


def resolve_manager_person_address(
    reader: Reader, manager_id: int, team_id: int, *,
    team_address: Any = 0, manager_address: Any = 0,
) -> int:
    """Resolve and validate the active human manager's Person address."""
    _manager, person, _team = _context_addresses(
        reader, int(manager_id), int(team_id), team_address, manager_address,
    )
    return int(person)


def _contract(
    reader: Reader, person: int, team_address: int | None = None,
) -> tuple[int | None, dict[str, Any] | None]:
    valid = _person_contract_candidates(reader, person)
    if team_address:
        address = next((value for value in valid if reader.ptr(value + 0x10) == int(team_address)), 0)
    else:
        address = valid[0] if valid else 0
    if not address:
        return None, None
    team = reader.ptr(address + 0x10)
    fm24 = reader.layout.key == "fm24"
    raw = int(reader.u32(address + (0x18 if fm24 else 0x20)) or 0)
    gross_gbp = _game_wage(raw)
    start = decode_date(reader.u32(address + (0x3C if fm24 else 0x44)) or 0)
    expiry = decode_date(reader.u32(address + (0x40 if fm24 else 0x48)) or 0)
    start = start if start and start.year > 1900 else None
    expiry = expiry if expiry and expiry.year > 1900 else None
    joined = decode_date(
        reader.u32(person + _person_joined_club_offset(reader)) or 0,
    )
    return team, {
        "address": hex(address), "gross_weekly_raw": raw, "gross_weekly_display": gross_gbp,
        "currency": "GBP", "currency_symbol": "£", "tax_rate": SALARY_TAX_RATE,
        "net_weekly_display": round(gross_gbp * SALARY_NET_RATE, 2),
        "start_date": start.isoformat() if start else None,
        "expiry_date": expiry.isoformat() if expiry else None,
        "joined_club_date": joined.isoformat() if joined else None,
        "contract_type": "全职合同",
    }


def _age_on(birth: date | None, current: date | None) -> int | None:
    if not birth or not current:
        return None
    return current.year - birth.year - ((current.month, current.day) < (birth.month, birth.day))


def _manager_intimacy(reader: Reader, person: int, manager_person: int) -> int:
    """Read the player's confirmed relationship score for the selected manager."""
    offset = reader.layout.person_relationships_offset
    if offset is None:
        return 0
    relationships = reader.ptr(person + offset)
    if not relationships:
        return 0
    begin = reader.ptr(relationships)
    end = reader.ptr(relationships + 8)
    capacity = reader.ptr(relationships + 16)
    if not begin or not end or not capacity:
        return 0
    byte_length = end - begin
    if begin >= end or end > capacity or byte_length % 16 or byte_length > 16 * 512:
        return 0
    scores = []
    raw = reader.bytes(begin, byte_length)
    if raw and len(raw) == byte_length:
        for record_offset in range(0, byte_length, 16):
            if struct.unpack_from("<Q", raw, record_offset)[0] != manager_person:
                continue
            score = raw[record_offset + 12]
            if score <= 100:
                scores.append(score)
    else:
        for entry in range(begin, end, 16):
            if reader.ptr(entry) != manager_person:
                continue
            score = reader.u8(entry + 12)
            if score is not None and 0 <= score <= 100:
                scores.append(score)
    return max(scores, default=0)


def _player(
    reader: Reader, address: int, game_date: date | None = None,
    manager_person: int = 0, roster_summary: dict[str, Any] | None = None,
    raw_snapshot: dict[str, bytes] | None = None,
) -> dict[str, Any] | None:
    raw_snapshot = raw_snapshot if raw_snapshot is not None else {}
    summary = roster_summary if _address((roster_summary or {}).get("address")) == address else None
    object_type = str((summary or {}).get("object_type") or "")
    if object_type == "actual_player":
        person = address + reader.layout.player_person_offset
    elif object_type == "actual_player_and_non_player":
        person = address + reader.layout.player_and_non_player_person_offset
    else:
        vtable = reader.ptr(address)
        player_rva = vtable - reader.module_base if vtable else 0
        if player_rva in reader.layout.actual_player_vtable_rvas:
            person = address + reader.layout.player_person_offset
        elif player_rva in reader.layout.player_and_non_player_vtable_rvas:
            person = address + reader.layout.player_and_non_player_person_offset
        else:
            return None
    identifier = int((summary or {}).get("id") or 0) or reader.u32(person + ENTITY_UID)
    name = str((summary or {}).get("name") or "") or _name(reader, person)
    if not identifier or not name:
        return None
    positions = raw_snapshot.get("positions")
    if positions is None:
        positions = reader.bytes(address + reader.layout.player_positions_offset, 15) or b""
    else:
        positions = bytes(positions)
    raw_snapshot["positions"] = positions
    if summary is not None:
        position_ratings = {
            str(label): int(value)
            for label, value in dict(summary.get("positions") or {}).items()
            if int(value) > 1
        }
    else:
        position_ratings = {
            label: int(value)
            for label, value in zip(POSITION_NAMES, positions)
            if value > 1
        }
    highest = max(position_ratings.values(), default=0)
    raw_attributes = raw_snapshot.get("attributes")
    if raw_attributes is None:
        raw_attributes = reader.bytes(address + reader.layout.player_attributes_offset, 54) or b""
    else:
        raw_attributes = bytes(raw_attributes)
    raw_snapshot["attributes"] = raw_attributes
    attributes = _visible_player_attributes(reader, address, raw_attributes)
    try:
        training_ca = _player_training_ca_snapshot(
            reader.layout, raw_attributes, positions, attributes,
        )
    except (TypeError, ValueError):
        training_ca = {"model": None, "verified": False, "costs": {}}
    foot_raw = raw_attributes[0x27 - 0x0F:0x29 - 0x0F]
    preferred_foot = None
    if len(foot_raw) == 2:
        left, right = foot_raw
        preferred_foot = "双足" if min(left, right) >= 75 else "左脚" if left > right else "右脚"
    nation = reader.ptr(person + reader.layout.person_nationality_offset)
    nation_id = reader.u32(nation + ENTITY_UID) if nation else None
    if nation_id not in NATION_NAMES:
        nation_id = None
    birth = _person_birth_date(reader, person)
    relationship = None
    if manager_person > 0:
        from tools.person_relationships import read_person_relationships

        relationship = read_person_relationships(reader, person, int(manager_person))
    return {
        "id": int(identifier), "name": name, "address": hex(address),
        "_name_signature": _player_name_signature(reader, person),
        "shirt_number": None,
        "height_cm": reader.u8(address + reader.layout.player_height_offset),
        "nationality_id": int(nation_id) if nation_id is not None else None,
        "nationality": NATION_NAMES.get(nation_id) if nation_id is not None else None,
        "nationality_code": NATION_CODES.get(nation_id) if nation_id is not None else None,
        "date_of_birth": birth.isoformat() if birth else None,
        "age": _age_on(birth, game_date),
        "positions": list(position_ratings), "position_ratings": position_ratings,
        "primary_positions": [label for label, value in position_ratings.items() if value == highest],
        "attributes": attributes, "preferred_foot": preferred_foot,
        "training_ca": training_ca,
        "international_reputation": reader.u16(address + reader.layout.player_world_reputation_offset),
        "pa": (
            summary.get("pa")
            if summary is not None else reader.u16(address + reader.layout.player_pa_offset)
        ),
        "hidden_attributes": _player_hidden_attributes(
            reader, address, person, raw_attributes,
        ),
        "manager_intimacy": int((relationship or {}).get("manager_intimacy") or 0),
        "manager_relation_reason": (relationship or {}).get("manager_relation_reason"),
        "manager_relation_permanence": (relationship or {}).get("manager_relation_permanence"),
        "manager_relation_object_type": (relationship or {}).get("manager_relation_object_type"),
        "manager_relation_type": (relationship or {}).get("manager_relation_type"),
        "person_relations": (relationship or {}).get("person_relations", {}),
        "person_relations_detail": (relationship or {}).get("person_relations_detail", {}),
        "ca": (
            int(summary.get("ca") or 0)
            if summary is not None else reader.u16(address + reader.layout.player_ca_offset)
        ),
        "fitness": (
            summary.get("fitness_percent")
            if summary is not None
            else round((reader.u16(address + int(reader.layout.player_fitness_offset)) or 0) / 100, 2)
        ),
        "sharpness": (
            summary.get("sharpness_percent")
            if summary is not None
            else round((reader.u16(address + int(reader.layout.player_sharpness_offset)) or 0) / 100, 2)
        ),
        "fatigue": (
            summary.get("fatigue_raw")
            if summary is not None else reader.u16(address + int(reader.layout.player_fatigue_offset))
        ),
        "morale": (
            summary.get("morale_raw")
            if summary is not None else reader.u8(address + int(reader.layout.player_morale_offset))
        ),
    }


def read_roster_player_profile(
    reader: Reader, roster_summary: dict[str, Any], team_address: int,
    game_date: date | None = None, manager_person: int = 0,
    *, raw_snapshot: dict[str, bytes] | None = None,
) -> dict[str, Any] | None:
    """Expand one verified roster row into the shared player-card payload."""
    address = _address(roster_summary.get("address"))
    raw_snapshot = raw_snapshot if raw_snapshot is not None else {}
    player = _player(
        reader, address, game_date,
        manager_person=manager_person, roster_summary=roster_summary,
        raw_snapshot=raw_snapshot,
    )
    if not player:
        return None

    player_id = int(player["id"])
    person = _validated_player_person(reader, address, player_id)
    raw_attributes = raw_snapshot.get("attributes")
    if raw_attributes is None:
        raw_attributes = reader.bytes(
            address + reader.layout.player_attributes_offset, 54,
        ) or b""
    nation_id, nationality, nationality_code = _person_nationality(reader, person)
    birth = _person_birth_date(reader, person)

    def optional_u8(offset: int | None) -> int | None:
        return reader.u8(address + int(offset)) if offset is not None else None

    def optional_u16(offset: int | None) -> int | None:
        return reader.u16(address + int(offset)) if offset is not None else None

    player.update({
        "object_type": roster_summary.get("object_type"),
        "weight_kg": optional_u8(reader.layout.player_weight_offset),
        "nationality_id": nation_id,
        "nationality": nationality,
        "nationality_code": nationality_code,
        "date_of_birth": birth.isoformat() if birth else None,
        "age": _age_on(birth, game_date),
        "home_reputation": optional_u16(reader.layout.player_home_reputation_offset),
        "current_reputation": optional_u16(reader.layout.player_current_reputation_offset),
        "world_reputation": optional_u16(reader.layout.player_world_reputation_offset),
        "hidden_attributes": _player_hidden_attributes(
            reader, address, person, raw_attributes,
        ),
        "availability": roster_summary.get("availability"),
        "season_rating": roster_summary.get("season_rating"),
    })
    player["international_reputation"] = player.get("world_reputation")
    if reader.layout.key == "fm24":
        player.update(read_fm24_extended_player(
            reader, address, person, int(team_address), game_date,
        ))
    else:
        player.update(read_fm26_extended_player(
            reader, address, person, int(team_address), game_date,
            expected_uid=int(player.get("id") or 0),
        ))
    player["career_status"] = {
        "is_player_staff": (
            roster_summary.get("object_type") == "actual_player_and_non_player"
        ),
    }
    return player


CREDIT_GOALKEEPER_ATTRIBUTE_IDS = tuple(dict.fromkeys((
    *GOALKEEPER_ATTRIBUTE_GROUPS["门将"],
    *OUTFIELD_ATTRIBUTE_GROUPS["精神"],
    *OUTFIELD_ATTRIBUTE_GROUPS["身体"],
)))
CREDIT_BODY_ATTRIBUTE_IDS = frozenset(OUTFIELD_ATTRIBUTE_GROUPS["身体"])


def credit_default_penalty_counts(unpaid: float, loan_amount: float) -> tuple[int, int, int, float]:
    ratio = max(0.0, float(unpaid)) / max(float(loan_amount), 0.01)
    if ratio >= 0.80:
        return 10, 10, 0, ratio
    if ratio >= 0.60:
        return 8, 8, 0, ratio
    if ratio >= 0.40:
        return 4, 4, 0, ratio
    if ratio >= 0.20:
        return 2, 0, 2, ratio
    return 1, 0, 1, ratio


def _plan_credit_attribute_penalty(
    original: bytes, goalkeeper: bool, rng: Any = random,
) -> tuple[bytes, dict[str, int], dict[str, int]]:
    attribute_ids = CREDIT_GOALKEEPER_ATTRIBUTE_IDS if goalkeeper else DOPING_ATTRIBUTE_IDS
    capacities = {
        attribute_id: min(2, max(0, _display_attribute(original[attribute_id - 0x0F]) - 1))
        for attribute_id in attribute_ids
    }
    deductions: dict[int, int] = {}
    for _ in range(10):
        candidates = [
            attribute_id for attribute_id in attribute_ids
            if deductions.get(attribute_id, 0) < capacities[attribute_id]
        ]
        if not candidates:
            raise ValueError("球员可扣除属性不足10点")
        weights = [1.5 if value in CREDIT_BODY_ATTRIBUTE_IDS else 1.0 for value in candidates]
        selected = rng.choices(candidates, weights=weights, k=1)[0]
        deductions[selected] = deductions.get(selected, 0) + 1
    updated = bytearray(original)
    before, after = {}, {}
    for attribute_id, points in deductions.items():
        index = attribute_id - 0x0F
        before[str(attribute_id)] = _display_attribute(updated[index])
        updated[index] = max(1, int(updated[index]) - 5 * points)
        after[str(attribute_id)] = _display_attribute(updated[index])
    return bytes(updated), before, after


def _native_injury_templates(
    process: Any, reader: Reader, minimum_days: int = 30,
    maximum_days: int = 180,
    required_name_tokens: tuple[str, ...] = (),
    minimum_templates: int = 192, minimum_names: int = 32,
) -> list[dict[str, Any]]:
    """Collect a bounded pool of genuine records for the active FM layout."""
    required_tokens = tuple(str(value).casefold() for value in required_name_tokens if value)
    minimum_templates = max(1, int(minimum_templates))
    minimum_names = max(1, int(minimum_names))
    cache_key = (
        process.pid, int(minimum_days), int(maximum_days), required_tokens,
        minimum_templates, minimum_names,
    )
    cached = _NATIVE_INJURY_TEMPLATE_CACHE.get(cache_key)
    if cached:
        return cached
    templates: list[dict[str, Any]] = []
    names: set[str] = set()
    required_found = not required_tokens
    seen: set[int] = set()
    needles = [
        struct.pack("<Q", reader.module_base + rva)
        for rva in reader.layout.actual_player_vtable_rvas
    ]
    done = False
    chunk_size = 8 * 1024 * 1024
    overlap_size = max((len(needle) for needle in needles), default=1) - 1
    for region in iter_readable_regions(process):
        if region.type != MEM_PRIVATE:
            continue
        carry = b""
        for offset in range(0, region.size, chunk_size):
            raw = read_process_memory(
                process, region.base_address + offset,
                min(chunk_size, region.size - offset),
            )
            if not raw:
                carry = b""
                continue
            block = carry + raw
            block_base = region.base_address + offset - len(carry)
            for needle in needles:
                position = 0
                while True:
                    index = block.find(needle, position)
                    if index < 0:
                        break
                    player = block_base + index
                    position = index + len(needle)
                    if player in seen:
                        continue
                    seen.add(player)
                    container = reader.ptr(player + _injury_list_offset(reader.layout))
                    if not container:
                        continue
                    begin = reader.ptr(container)
                    end = reader.ptr(container + 0x08)
                    if not begin or not end or end <= begin or (end - begin) % 8:
                        continue
                    for slot in range(begin, min(end, begin + 32), 8):
                        record = reader.ptr(slot)
                        record_raw = reader.bytes(record, 0x50) if record else None
                        if not record_raw or len(record_raw) != 0x50:
                            continue
                        injury_type = reader.ptr(record + 0x08)
                        name = reader.fm_string_at(injury_type + 0x18) if injury_type else None
                        high_raw = reader.u16(record + 0x28)
                        low_raw = reader.u16(record + 0x2A)
                        if not name or high_raw is None or low_raw is None:
                            continue
                        low = min(int(low_raw), int(high_raw))
                        high = max(int(low_raw), int(high_raw))
                        # Loan-default injuries should be meaningful without creating
                        # multi-season outliers from historical database records.
                        if not int(minimum_days) <= low <= high <= int(maximum_days):
                            continue
                        folded_name = name.casefold()
                        is_required = any(token in folded_name for token in required_tokens)
                        row = {
                            "name": name,
                            "duration_days_low": low,
                            "duration_days_high": high,
                            "record": bytes(record_raw),
                        }
                        if len(templates) < 512 or is_required:
                            templates.append(row)
                        names.add(name)
                        required_found = required_found or is_required
                        if (
                            len(templates) >= minimum_templates
                            and len(names) >= minimum_names
                            and required_found
                        ):
                            done = True
                            break
                    if done:
                        break
                if done:
                    break
            if done:
                break
            carry = block[-overlap_size:] if overlap_size else b""
        if done:
            break
    if not templates:
        raise RuntimeError("当前 FM 内存中没有可用的真实伤病模板")
    _NATIVE_INJURY_TEMPLATE_CACHE[cache_key] = templates
    return templates


def _configure_native_remote_calls() -> None:
    kernel32.LoadLibraryW.argtypes = [wintypes.LPCWSTR]
    kernel32.LoadLibraryW.restype = wintypes.HMODULE
    kernel32.GetProcAddress.argtypes = [wintypes.HMODULE, wintypes.LPCSTR]
    kernel32.GetProcAddress.restype = ctypes.c_void_p
    kernel32.VirtualAllocEx.argtypes = [
        wintypes.HANDLE, ctypes.c_void_p, ctypes.c_size_t,
        wintypes.DWORD, wintypes.DWORD,
    ]
    kernel32.VirtualAllocEx.restype = ctypes.c_void_p
    kernel32.CreateRemoteThread.argtypes = [
        wintypes.HANDLE, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p,
        ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.CreateRemoteThread.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.VirtualFreeEx.argtypes = [
        wintypes.HANDLE, ctypes.c_void_p, ctypes.c_size_t, wintypes.DWORD,
    ]
    kernel32.VirtualFreeEx.restype = wintypes.BOOL


def _native_injury_type(
    process: Any, reader: Reader, templates: list[dict[str, Any]], injury_name: str,
) -> int:
    """Locate a named FM injury definition without requiring a live instance."""
    cache_key = (process.pid, str(injury_name))
    cached = _NATIVE_INJURY_TYPE_CACHE.get(cache_key)
    if cached and reader.fm_string_at(cached + 0x18) == injury_name:
        return cached
    vtables: set[int] = set()
    known_types: set[int] = set()
    for row in templates:
        raw = row.get("record") or b""
        injury_type = struct.unpack_from("<Q", raw, 0x08)[0] if len(raw) >= 0x10 else 0
        vtable = reader.ptr(injury_type) if injury_type else 0
        if vtable:
            known_types.add(int(injury_type))
            vtables.add(int(vtable))
    if not vtables:
        raise RuntimeError("无法确定 FM 伤病类型结构")
    for injury_type in known_types:
        if reader.fm_string_at(injury_type + 0x18) == injury_name:
            _NATIVE_INJURY_TYPE_CACHE[cache_key] = injury_type
            return injury_type
    # FM24 and FM26 use different definition strides (observed 0x60 and 0x78).
    # Scan the readable allocation containing a known definition in bulk rather
    # than guessing a stride or issuing thousands of cross-process reads.
    definition_regions = []
    for region in iter_readable_regions(process):
        if any(
            region.base_address <= injury_type < region.base_address + region.size
            for injury_type in known_types
        ):
            definition_regions.append(region)
    needles = [struct.pack("<Q", value) for value in vtables]
    for region in definition_regions:
        carry = b""
        chunk_size = 8 * 1024 * 1024
        for offset in range(0, region.size, chunk_size):
            raw = read_process_memory(
                process, region.base_address + offset,
                min(chunk_size, region.size - offset),
            )
            if not raw:
                carry = b""
                continue
            block = carry + raw
            block_base = region.base_address + offset - len(carry)
            for needle in needles:
                position = 0
                while True:
                    position = block.find(needle, position)
                    if position < 0:
                        break
                    candidate = block_base + position
                    position += 8
                    if reader.fm_string_at(candidate + 0x18) == injury_name:
                        _NATIVE_INJURY_TYPE_CACHE[cache_key] = candidate
                        return candidate
            carry = block[-7:]
    needles = [struct.pack("<Q", value) for value in vtables]
    chunk_size = 8 * 1024 * 1024
    overlap_size = 7
    seen: set[int] = set()
    for region in iter_readable_regions(process):
        carry = b""
        for offset in range(0, region.size, chunk_size):
            raw = read_process_memory(
                process, region.base_address + offset,
                min(chunk_size, region.size - offset),
            )
            if not raw:
                carry = b""
                continue
            block = carry + raw
            block_base = region.base_address + offset - len(carry)
            for needle in needles:
                position = 0
                while True:
                    index = block.find(needle, position)
                    if index < 0:
                        break
                    position = index + len(needle)
                    candidate = block_base + index
                    if candidate in seen:
                        continue
                    seen.add(candidate)
                    if reader.fm_string_at(candidate + 0x18) == injury_name:
                        _NATIVE_INJURY_TYPE_CACHE[cache_key] = candidate
                        return candidate
            carry = block[-overlap_size:]
    raise RuntimeError(f"当前 FM 内存中未找到可验证的{injury_name}类型")


def _remote_malloc_block(process: Any, size: int) -> tuple[int, int]:
    """Allocate process-owned memory through the target's verified UCRT."""
    _configure_native_remote_calls()
    remote_ucrt = find_module(process, "ucrtbase.dll")
    local_ucrt = kernel32.LoadLibraryW("ucrtbase.dll")
    if not remote_ucrt or not local_ucrt:
        raise RuntimeError("无法定位 FM C 运行库")
    local_malloc = int(kernel32.GetProcAddress(local_ucrt, b"malloc") or 0)
    local_free = int(kernel32.GetProcAddress(local_ucrt, b"free") or 0)
    malloc_address = remote_ucrt.base_address + local_malloc - int(local_ucrt)
    free_address = remote_ucrt.base_address + local_free - int(local_ucrt)
    if (
        not local_malloc or not local_free
        or read_process_memory(process, malloc_address, 16) != ctypes.string_at(local_malloc, 16)
    ):
        raise RuntimeError("FM 内存分配器校验失败")
    cave = int(kernel32.VirtualAllocEx(process.handle, None, 0x1000, 0x3000, 0x40) or 0)
    if not cave:
        raise RuntimeError("无法建立 FM 安全分配调用区")
    cave_releasable = True
    try:
        result_address = cave + 0x200
        code = (
            b"\x48\x83\xEC\x28"
            + b"\x48\xB9" + struct.pack("<Q", int(size))
            + b"\x48\xB8" + struct.pack("<Q", malloc_address)
            + b"\xFF\xD0"
            + b"\x48\xA3" + struct.pack("<Q", result_address)
            + b"\x48\x83\xC4\x28\x33\xC0\xC3"
        )
        write_process_memory(process, cave, code)
        thread = kernel32.CreateRemoteThread(
            process.handle, None, 0, ctypes.c_void_p(cave), None, 0, None,
        )
        if not thread:
            raise RuntimeError("FM 内存分配线程创建失败")
        try:
            if kernel32.WaitForSingleObject(thread, REMOTE_THREAD_WAIT_MS) != WAIT_OBJECT_0:
                # The remote thread may still be executing this code.  Leak the
                # tiny call cave instead of freeing executable memory under it.
                cave_releasable = False
                raise RuntimeError("FM 内存分配等待失败")
        finally:
            kernel32.CloseHandle(thread)
        raw_pointer = read_process_memory(process, result_address, 8)
        value = struct.unpack("<Q", raw_pointer)[0] if raw_pointer else 0
        if not value:
            raise RuntimeError("FM 内存分配失败")
        return value, free_address
    finally:
        if cave_releasable:
            kernel32.VirtualFreeEx(process.handle, ctypes.c_void_p(cave), 0, 0x8000)


def _remote_free_block(process: Any, free_address: int, address: int) -> None:
    if not free_address or not address:
        return
    thread = kernel32.CreateRemoteThread(
        process.handle, None, 0, ctypes.c_void_p(free_address),
        ctypes.c_void_p(address), 0, None,
    )
    if thread:
        try:
            kernel32.WaitForSingleObject(thread, REMOTE_FREE_THREAD_WAIT_MS)
        finally:
            kernel32.CloseHandle(thread)


def _fm_utf8_string_block(encoded: bytes) -> bytes:
    """Build the reference-counted UTF-8 block used by FMRTE/FM strings."""
    encoded = bytes(encoded)
    if not encoded or len(encoded) > 256:
        raise ValueError("FM 字符串长度无效")
    return (
        struct.pack("<QII", len(encoded) + 9, 1, len(encoded))
        + encoded + b"\0"
    )


def _allocate_fm_utf8_string(process: Any, encoded: bytes) -> tuple[int, int]:
    """Allocate a complete FM/FMRTE-compatible string through VirtualAllocEx."""
    _configure_native_remote_calls()
    block = _fm_utf8_string_block(encoded)
    allocation = int(kernel32.VirtualAllocEx(
        process.handle, None, len(block), 0x1000, 0x40,
    ) or 0)
    if not allocation:
        raise RuntimeError("FM 字符串内存分配失败")
    try:
        write_process_memory(process, allocation, block)
        if read_process_memory(process, allocation, len(block)) != block:
            raise RuntimeError("FM 字符串内存初始化校验失败")
        return allocation + 12, allocation
    except Exception:
        kernel32.VirtualFreeEx(
            process.handle, ctypes.c_void_p(allocation), 0, 0x8000,
        )
        raise


def _free_fm_utf8_string(process: Any, allocation: int) -> None:
    if allocation:
        _configure_native_remote_calls()
        kernel32.VirtualFreeEx(
            process.handle, ctypes.c_void_p(int(allocation)), 0, 0x8000,
        )


def create_player_injury(
    player_id: int, player_address: Any, game_date: str,
    *, team_id: int, team_address: Any = 0,
    injury_name: str = "病毒感染", duration_days: int = 3,
) -> dict[str, Any]:
    current = date.fromisoformat(str(game_date))
    duration_days = int(duration_days)
    if duration_days <= 0 or duration_days > 180:
        raise ValueError("伤病持续时间无效")
    pid, _path, layout = select_process_layout()
    allocated: list[tuple[int, int]] = []
    created_container = 0
    injury_pointer = 0
    original_container_pointer = b""
    with open_process(pid, write_memory=True) as process:
        module = layout.module(process)
        if not module:
            raise RuntimeError(f"{layout.module_name} 尚未加载")
        reader = Reader(process, module.base_address, layout)
        resolved_team = _resolve_team_address(reader, int(team_id), team_address)
        roster = {int(row.get("id") or 0): _address(row.get("address")) for row in reader.roster(resolved_team)}
        player = _address(player_address)
        if int(roster.get(int(player_id)) or 0) != player:
            player = int(roster.get(int(player_id)) or 0)
        if not player:
            raise ValueError("目标球员已不在所选对手的一线队")
        _validated_player_person(reader, player, int(player_id))
        injury_pointer = player + _injury_list_offset(layout)
        original_container_pointer = reader.bytes(injury_pointer, 8) or b""
        if len(original_container_pointer) != 8:
            raise RuntimeError("无法读取球员伤病容器指针")
        container = reader.ptr(injury_pointer)
        if container and container <= 0xFFFFF:
            raise RuntimeError("目标球员伤病容器指针无效")
        original = reader.bytes(container, 0x30) if container else b"\0" * 0x30
        if container and (not original or len(original) != 0x30):
            raise RuntimeError("无法读取球员伤病槽")
        begin, end, capacity = struct.unpack_from("<QQQ", original)
        if begin or end or capacity:
            if not begin or not end or not capacity or begin > end or end > capacity or (end - begin) % 8:
                raise RuntimeError("球员伤病槽结构无效")
            if end > begin:
                raise ValueError("该球员已有伤病，不能使用病毒包裹")
        templates = _native_injury_templates(
            process, reader, minimum_days=1,
            minimum_templates=1, minimum_names=1,
        )
        candidates = [
            row for row in templates
            if injury_name.casefold() in str(row.get("name") or "").casefold()
        ]
        template = candidates[0] if candidates else templates[0]
        injury_type = _native_injury_type(process, reader, templates, injury_name)
        record = vector = 0
        try:
            if not container:
                container, container_free = _remote_malloc_block(process, 0x30)
                allocated.append((container_free, container))
                created_container = container
                write_process_memory(process, container, b"\0" * 0x30)
                write_process_memory(process, injury_pointer, struct.pack("<Q", container))
                if (
                    reader.ptr(injury_pointer) != container
                    or reader.bytes(container, 0x30) != b"\0" * 0x30
                ):
                    raise RuntimeError("球员伤病容器创建校验失败")
            record, record_free = _remote_malloc_block(process, 0x50)
            allocated.append((record_free, record))
            vector, vector_free = _remote_malloc_block(process, 8)
            allocated.append((vector_free, vector))
            raw = bytearray(template["record"])
            struct.pack_into("<Q", raw, 0x08, injury_type)
            struct.pack_into("<Q", raw, 0x18, resolved_team)
            date_code = (current.year << 16) | current.timetuple().tm_yday
            struct.pack_into("<I", raw, 0x20, date_code)
            struct.pack_into("<HH", raw, 0x28, duration_days, duration_days)
            write_process_memory(process, record, bytes(raw))
            write_process_memory(process, vector, struct.pack("<Q", record))
            write_process_memory(process, container, struct.pack("<QQQ", vector, vector + 8, vector + 8))
            details = reader._availability_details(container)
            if not details or details.get("injury_count") != 1:
                raise RuntimeError("病毒感染写入校验失败")
            injury = (details.get("injuries") or [{}])[0]
            if str(injury.get("type") or "") != injury_name:
                raise RuntimeError("病毒感染类型回读校验失败")
            if int(injury.get("duration_days_low_raw") or 0) != duration_days:
                raise RuntimeError("病毒感染持续时间回读校验失败")
            written = reader.bytes(record, 0x20)
            if not written or struct.unpack_from("<Q", written, 0x18)[0] != resolved_team:
                raise RuntimeError("病毒感染球队指针回读校验失败")
            allocated.clear()
            return {
                "player_id": int(player_id), "injury": injury_name,
                "duration_days": duration_days, "start_date": current.isoformat(),
                "team_id": int(team_id),
            }
        except Exception as error:
            try:
                if created_container:
                    write_process_memory(process, injury_pointer, original_container_pointer)
                    restored = reader.bytes(injury_pointer, 8) == original_container_pointer
                else:
                    write_process_memory(process, container, original)
                    restored = reader.bytes(container, len(original)) == original
                if not restored:
                    raise RuntimeError("伤病容器恢复回读不一致")
            except Exception as rollback_error:
                # Keep allocations alive if the game may still reference them.
                # A small leak is safer than freeing memory behind a live pointer.
                allocated.clear()
                raise RuntimeError(
                    f"病毒感染写入失败且伤病容器恢复异常：{rollback_error}"
                ) from error
            for free_address, address in reversed(allocated):
                _remote_free_block(process, free_address, address)
            raise


FRACTURE_INJURY_TOKENS = ("骨折", "骨裂", "fractur", "broken")


def _select_random_world_injury_template(
    templates: list[dict[str, Any]], *, minimum_days: int = 7,
    maximum_days: int = 30, rng: Any = random,
) -> dict[str, Any]:
    candidates = [
        row for row in templates
        if minimum_days <= int(row.get("duration_days_low") or 0)
        <= int(row.get("duration_days_high") or 0) <= maximum_days
    ]
    if not candidates:
        raise RuntimeError(f"当前世界没有持续 {minimum_days}-{maximum_days} 天的可用伤病模板")
    fracture_templates = [
        row for row in candidates
        if any(
            token in str(row.get("name") or "").casefold()
            for token in FRACTURE_INJURY_TOKENS
        )
    ]
    return dict(rng.choice(fracture_templates or candidates))


def create_random_world_injury(
    player_id: int, player_address: Any, game_date: str,
    *, team_id: int, team_address: Any = 0, rng: Any = random,
) -> dict[str, Any]:
    current = date.fromisoformat(str(game_date))
    pid, _path, layout = select_process_layout()
    allocated: list[tuple[int, int]] = []
    created_container = 0
    injury_pointer = 0
    original_container_pointer = b""
    with open_process(pid, write_memory=True) as process:
        module = layout.module(process)
        if not module:
            raise RuntimeError(f"{layout.module_name} 尚未加载")
        reader = Reader(process, module.base_address, layout)
        resolved_team = _resolve_team_address(reader, int(team_id), team_address)
        roster = {
            int(row.get("id") or 0): _address(row.get("address"))
            for row in reader.roster(resolved_team)
        }
        player = _address(player_address)
        if int(roster.get(int(player_id)) or 0) != player:
            player = int(roster.get(int(player_id)) or 0)
        if not player:
            raise ValueError("目标球员已不在所选球队的一线队")
        _validated_player_person(reader, player, int(player_id))

        injury_pointer = player + _injury_list_offset(layout)
        original_container_pointer = reader.bytes(injury_pointer, 8) or b""
        if len(original_container_pointer) != 8:
            raise RuntimeError("无法读取球员伤病容器指针")
        container = reader.ptr(injury_pointer)
        if container and container <= 0xFFFFF:
            raise RuntimeError("目标球员伤病容器指针无效")
        original = reader.bytes(container, 0x30) if container else b"\0" * 0x30
        if container and (not original or len(original) != 0x30):
            raise RuntimeError("无法读取球员伤病槽")
        begin, end, capacity = struct.unpack_from("<QQQ", original)
        if begin or end or capacity:
            if not begin or not end or not capacity or begin > end or end > capacity or (end - begin) % 8:
                raise RuntimeError("球员伤病槽结构无效")
            existing_pointers = reader.bytes(begin, end - begin) or b""
            if len(existing_pointers) != end - begin:
                raise RuntimeError("无法读取球员已有伤病记录")
            existing_count = (end - begin) // 8
        else:
            existing_pointers = b""
            existing_count = 0

        templates = _native_injury_templates(
            process, reader, minimum_days=7, maximum_days=30,
            required_name_tokens=FRACTURE_INJURY_TOKENS,
            minimum_templates=192, minimum_names=32,
        )
        template = _select_random_world_injury_template(templates, rng=rng)
        injury_name = str(template.get("name") or "伤病")
        duration_low = int(template.get("duration_days_low") or 0)
        duration_high = int(template.get("duration_days_high") or 0)
        raw_template = bytes(template.get("record") or b"")
        if len(raw_template) != 0x50:
            raise RuntimeError("当前世界伤病模板结构无效")
        injury_type = struct.unpack_from("<Q", raw_template, 0x08)[0]
        if not injury_type or reader.fm_string_at(injury_type + 0x18) != injury_name:
            raise RuntimeError("当前世界伤病模板类型校验失败")

        try:
            if not container:
                container, container_free = _remote_malloc_block(process, 0x30)
                allocated.append((container_free, container))
                created_container = container
                write_process_memory(process, container, b"\0" * 0x30)
                write_process_memory(process, injury_pointer, struct.pack("<Q", container))
                if (
                    reader.ptr(injury_pointer) != container
                    or reader.bytes(container, 0x30) != b"\0" * 0x30
                ):
                    raise RuntimeError("球员伤病容器创建校验失败")

            record, record_free = _remote_malloc_block(process, 0x50)
            allocated.append((record_free, record))
            vector_size = (existing_count + 1) * 8
            vector, vector_free = _remote_malloc_block(process, vector_size)
            allocated.append((vector_free, vector))
            record_raw = bytearray(raw_template)
            # The cloned template may retain a match-source pointer here. A null
            # source makes FM render the injury cause as its localized unknown reason.
            struct.pack_into("<Q", record_raw, 0x00, 0)
            struct.pack_into("<Q", record_raw, 0x18, resolved_team)
            date_code = (current.year << 16) | current.timetuple().tm_yday
            struct.pack_into("<I", record_raw, 0x20, date_code)
            write_process_memory(process, record, bytes(record_raw))
            write_process_memory(
                process, vector,
                existing_pointers + struct.pack("<Q", record),
            )
            write_process_memory(
                process, container,
                struct.pack("<QQQ", vector, vector + vector_size, vector + vector_size),
            )

            details = reader._availability_details(container)
            injuries = list((details or {}).get("injuries") or [])
            if not details or details.get("injury_count") != existing_count + 1 or len(injuries) != existing_count + 1:
                raise RuntimeError("小黑屋伤病写入校验失败")
            injury = injuries[-1]
            if str(injury.get("type") or "") != injury_name:
                raise RuntimeError("小黑屋伤病类型回读校验失败")
            if (
                int(injury.get("duration_days_low_raw") or 0) != duration_low
                or int(injury.get("duration_days_high_raw") or 0) != duration_high
            ):
                raise RuntimeError("小黑屋伤病持续时间回读校验失败")
            written = reader.bytes(record, 0x20)
            if not written or len(written) != 0x20:
                raise RuntimeError("小黑屋伤病上下文回读失败")
            if struct.unpack_from("<Q", written, 0x00)[0] != 0:
                raise RuntimeError("小黑屋伤病原因清除回读校验失败")
            if struct.unpack_from("<Q", written, 0x18)[0] != resolved_team:
                raise RuntimeError("小黑屋伤病球队指针回读校验失败")

            allocated.clear()
            return {
                "player_id": int(player_id), "injury": injury_name,
                "minimum_days": duration_low, "maximum_days": duration_high,
                "start_date": current.isoformat(), "team_id": int(team_id),
                "cause": "unknown",
                "fracture_preferred": any(
                    token in injury_name.casefold() for token in FRACTURE_INJURY_TOKENS
                ),
                "injuries": injuries,
            }
        except Exception as error:
            try:
                if created_container:
                    write_process_memory(process, injury_pointer, original_container_pointer)
                    restored = reader.bytes(injury_pointer, 8) == original_container_pointer
                else:
                    write_process_memory(process, container, original)
                    restored = reader.bytes(container, len(original)) == original
                if not restored:
                    raise RuntimeError("伤病容器恢复回读不一致")
            except Exception as rollback_error:
                allocated.clear()
                raise RuntimeError(
                    f"小黑屋伤病写入失败且伤病容器恢复异常：{rollback_error}"
                ) from error
            for free_address, address in reversed(allocated):
                _remote_free_block(process, free_address, address)
            raise


def adjust_manager_intimacy_points(
    player_id: int, player_address: Any, manager_id: int, team_id: int,
    *, team_address: Any = 0, manager_address: Any = 0, points: int = 1,
    _reader: Reader | None = None, _process: Any = None,
) -> dict[str, Any]:
    points = int(points)
    if not points:
        raise ValueError("亲密度调整值无效")
    with _shared_writable_reader(_reader, _process) as (reader, process):
        layout = reader.layout
        _manager, manager_person, _team = _context_addresses(
            reader, int(manager_id), int(team_id), team_address, manager_address,
        )
        player = _address(player_address)
        person = _validated_player_person(reader, player, int(player_id))
        offset = layout.person_relationships_offset
        relationships = reader.ptr(person + offset) if offset is not None else 0
        header = reader.bytes(relationships, 24) if relationships else None
        if not relationships or not header or len(header) != 24:
            raise RuntimeError("无法读取球员关系记录")
        begin, end, capacity = struct.unpack("<QQQ", header)
        byte_length = end - begin
        if (
            not begin or begin > end or end > capacity
            or byte_length % 16 or byte_length > 16 * 1024
        ):
            raise RuntimeError("球员关系记录结构无效")
        relationship_raw = reader.bytes(begin, byte_length) if byte_length else b""
        if relationship_raw is None or len(relationship_raw) != byte_length:
            raise RuntimeError("无法完整读取球员关系记录")
        entry_offset = next((
            offset for offset in range(0, byte_length, 16)
            if struct.unpack_from("<Q", relationship_raw, offset)[0] == manager_person
        ), -1)
        entry = begin + entry_offset if entry_offset >= 0 else 0
        if entry:
            before = int(relationship_raw[entry_offset + 12])
            after = max(0, min(100, before + points))
            write_process_memory(process, entry + 12, bytes([after]))
            if reader.u8(entry + 12) != after:
                write_process_memory(process, entry + 12, bytes([before]))
                raise RuntimeError("亲密度写入校验失败")
            return {"before": before, "after": after, "applied": after - before}

        if points < 0:
            return {"before": 0, "after": 0, "applied": 0}
        after = min(100, points)
        relation_raw = struct.pack("<Q", manager_person) + b"\x06\x00\x03\x01" + bytes([after, 0, 0, 0xFF])
        if capacity - end >= 16:
            write_process_memory(process, end, relation_raw)
            write_process_memory(process, relationships + 8, struct.pack("<Q", end + 16))
            if reader.ptr(end) != manager_person or reader.u8(end + 12) != after:
                write_process_memory(process, relationships, header)
                raise RuntimeError("新建亲密度关系记录校验失败")
        else:
            existing = reader.bytes(begin, end - begin) or b""
            allocated, free_address = _remote_malloc_block(process, len(existing) + 16)
            try:
                write_process_memory(process, allocated, existing + relation_raw)
                write_process_memory(
                    process, relationships,
                    struct.pack("<QQQ", allocated, allocated + len(existing) + 16, allocated + len(existing) + 16),
                )
                if reader.ptr(allocated + len(existing)) != manager_person:
                    write_process_memory(process, relationships, header)
                    raise RuntimeError("新建亲密度关系记录校验失败")
            except Exception:
                _remote_free_block(process, free_address, allocated)
                raise
        return {"before": 0, "after": after, "applied": after}


class _BatchRosterSession:
    """Roster pointers belong only to this request and reader."""

    def __init__(self, reader: Reader, process: Any) -> None:
        self.reader = reader
        self.process = process
        self._roster_cache: dict = {}

    def resolve_roster_player(
        self, team_id: int, team_address: Any, player_id: int, player_address: Any = 0,
    ) -> str | None:
        return resolve_team_player_address(
            team_id, team_address, player_id, player_address,
            _reader=self.reader, _roster_cache=self._roster_cache,
        )


class PlayerActivityMemorySession(_BatchRosterSession):
    """Reuse one validated writable reader for a player's batch activities."""

    def apply_effect(self, player_id: int, player_address: Any, activity: str) -> dict[str, Any]:
        return apply_player_activity_effect(
            player_id, player_address, activity,
            _reader=self.reader, _process=self.process,
        )

    def apply_intimacy(
        self, player_id: int, player_address: Any, manager_id: int, team_id: int,
        *, team_address: Any = 0, manager_address: Any = 0, points: int = 1,
    ) -> dict[str, Any]:
        return adjust_manager_intimacy_points(
            player_id, player_address, manager_id, team_id,
            team_address=team_address, manager_address=manager_address, points=points,
            _reader=self.reader, _process=self.process,
        )

    def restore(self, result: dict[str, Any]) -> None:
        restore_player_activity_effect(
            result, _reader=self.reader, _process=self.process,
        )


@contextmanager
def player_activity_memory_session():
    with _shared_writable_reader() as (reader, process):
        yield PlayerActivityMemorySession(reader, process)


class CanteenSeaCucumberMemorySession(_BatchRosterSession):
    """A short-lived writer reused by one bulk canteen request."""

    def apply_effect(self, player_id: int, player_address: Any) -> dict[str, Any]:
        return apply_canteen_sea_cucumber_effect(
            player_id, player_address,
            _reader=self.reader, _process=self.process,
        )

    def apply_intimacy(
        self, player_id: int, player_address: Any,
        manager_id: int, team_id: int, *,
        team_address: Any = 0, manager_address: Any = 0,
    ) -> dict[str, Any]:
        return adjust_manager_intimacy_points(
            player_id, player_address, manager_id, team_id,
            team_address=team_address, manager_address=manager_address,
            points=1, _reader=self.reader, _process=self.process,
        )

    def restore(self, result: dict[str, Any]) -> None:
        restore_player_activity_effect(
            result, _reader=self.reader, _process=self.process,
        )


@contextmanager
def canteen_sea_cucumber_memory_session():
    """Open one writable session for a sequential, independently safe batch."""
    with _shared_writable_reader() as (reader, process):
        yield CanteenSeaCucumberMemorySession(reader, process)


def _nationality_layout_supported(layout: Any) -> bool:
    distribution = str(getattr(layout, "distribution", "")).lower()
    game_key = str(getattr(layout, "key", ""))
    identity = str(getattr(layout, "executable_sha256", "") or "").upper()
    return bool(
        (
            distribution == "steam" and game_key in {"fm24", "fm26"}
            or game_key == "fm24" and distribution == "epic"
            and identity == FM24_EPIC_EXE_SHA256
        )
        and getattr(layout, "person_nationality_offset", None) is not None
        and getattr(layout, "person_relationships_offset", None) is not None
        and getattr(layout, "nation_men_container_offset", None) is not None
        and int(getattr(layout, "nation_vtable_rva", 0) or 0) > 0
    )


def nationality_write_capability() -> dict[str, Any]:
    """Report support for exact layouts with validated nationality structures."""
    try:
        _pid, _path, layout = select_process_layout()
    except Exception as error:
        return {"available": False, "reason": str(error) or "尚未连接支持的 Football Manager"}
    available = _nationality_layout_supported(layout)
    return {
        "available": available,
        "game_key": str(getattr(layout, "key", "")),
        "reason": "" if available else "当前游戏版本尚未验证第二国籍写入",
    }


def _validated_nationality_target(
    reader: Reader, module_base: int, nation_id: int, nation_address: Any,
) -> int:
    nation = _address(nation_address)
    if not nation or int(reader.u32(nation + ENTITY_UID) or 0) != int(nation_id):
        raise ValueError("目标国家地址与国家 ID 不一致，请重新扫描世界数据")
    men_offset = reader.layout.nation_men_container_offset
    expected_rva = int(getattr(reader.layout, "nation_vtable_rva", 0) or 0)
    men_container = reader.ptr(nation + int(men_offset)) if men_offset is not None else 0
    if not men_container or reader.ptr(men_container) != int(module_base) + expected_rva:
        raise ValueError("目标国家对象类型校验失败，请重新扫描世界数据")
    return nation


def _manager_primary_nation(
    reader: Reader, module_base: int, manager_id: int, manager_address: Any = 0,
) -> tuple[int, int]:
    """Resolve the selected human manager's validated primary Nation object."""
    person = _validated_manager_person(reader, manager_id, manager_address)
    nationality_offset = reader.layout.person_nationality_offset
    nation = (
        int(reader.ptr(person + int(nationality_offset)) or 0)
        if nationality_offset is not None else 0
    )
    nation_id = int(reader.u32(nation + ENTITY_UID) or 0) if nation else 0
    _validated_nationality_target(reader, module_base, nation_id, nation)
    return nation, nation_id


def _validated_manager_person(
    reader: Reader, manager_id: int, manager_address: Any = 0,
) -> int:
    manager = _address(manager_address)
    if not manager:
        manager = _find_human_manager(reader, int(manager_id))
    if (
        int(manager_id) <= 0
        or not manager
        or not _human_manager_matches(reader, manager, int(manager_id))
    ):
        raise RuntimeError("玩家主教练对象校验失败")
    person = manager + int(reader.layout.manager_person_offset)
    if int(reader.u32(person + ENTITY_UID) or 0) != int(manager_id):
        raise RuntimeError("玩家主教练人物对象校验失败")
    return person


def _nation_address_for_uid(
    reader: Reader, module_base: int, nation_id: int,
    nation_address: Any = 0,
) -> int:
    """Re-resolve a persisted Nation UID after a save reload or process change.

    The world-club scan already validates and publishes a Nation address.  Use
    that address as a first-class hint when available, then fall back to the
    database index for callers that do not have a world scan row.
    """
    hint = _address(nation_address)
    if hint:
        try:
            return _validated_nationality_target(
                reader, module_base, int(nation_id), hint,
            )
        except (RuntimeError, ValueError):
            pass
    directory = database_index_for_reader(reader)
    if directory is not None:
        for address in directory.addresses_for_uid("nation", int(nation_id)):
            try:
                return _validated_nationality_target(
                    reader, module_base, int(nation_id), int(address),
                )
            except (RuntimeError, ValueError):
                continue
    raise RuntimeError(f"无法重新定位国家对象（国家 ID {int(nation_id)}）")


def update_world_player_primary_nationality(
    player_id: int, nation_id: int, expected_nation_id: int,
    player_address: Any = 0, nation_address: Any = 0,
) -> dict[str, Any]:
    """Replace a world player's validated primary Nation pointer."""
    player_id = int(player_id)
    nation_id = int(nation_id)
    expected_nation_id = int(expected_nation_id)
    if player_id <= 0 or nation_id <= 0 or expected_nation_id <= 0:
        raise ValueError("球员或国家 ID 无效")
    with _writable_game_reader() as (reader, process, module):
        if not _nationality_layout_supported(reader.layout):
            raise RuntimeError("当前游戏版本尚未验证主要国籍写入")
        if player_address:
            _player = _address(player_address)
            person = _validated_player_person(reader, _player, player_id)
        else:
            _indexed, _player, person = _world_player_index_target(reader, player_id)
        nationality_slot = person + int(reader.layout.person_nationality_offset)
        original = reader.bytes(nationality_slot, 8)
        if not original or len(original) != 8:
            raise RuntimeError("无法读取球员主要国籍")
        current_nation = int.from_bytes(original, "little")
        current_nation_id = int(reader.u32(current_nation + ENTITY_UID) or 0)
        if current_nation_id != expected_nation_id:
            raise ValueError("球员主要国籍已发生变化，请重新打开详情")
        _validated_nationality_target(
            reader, int(module.base_address), current_nation_id, current_nation,
        )
        target_nation = _nation_address_for_uid(
            reader, int(module.base_address), nation_id, nation_address,
        )
        if target_nation == current_nation:
            raise ValueError("没有需要保存的主要国籍修改")

        relationships = int(
            reader.ptr(person + int(reader.layout.person_relationships_offset)) or 0
        )
        header = reader.bytes(relationships, 24) if relationships else None
        if relationships and header and len(header) == 24:
            begin, end, capacity = struct.unpack("<QQQ", header)
            if (
                not begin or begin > end or end > capacity
                or (end - begin) % SECOND_NATIONALITY_RECORD_SIZE
                or capacity - begin > 16 * 1024 * 1024
            ):
                raise RuntimeError("球员关系记录结构无效")
            for entry in range(begin, end, SECOND_NATIONALITY_RECORD_SIZE):
                if (
                    reader.ptr(entry) == target_nation
                    and reader.u8(entry + 10) == SECOND_NATIONALITY_OBJECT_TYPE
                    and reader.u8(entry + 11) in {
                        SECOND_NATIONALITY_RELATION_TYPE, 0x46,
                    }
                ):
                    raise ValueError("目标国家当前是第二国籍，请先移除该第二国籍")

        updated = int(target_nation).to_bytes(8, "little")
        _apply_verified_memory_changes(
            reader, process,
            [{"address": nationality_slot, "original": original, "updated": updated}],
            write_error="主要国籍写入后回读失败",
            rollback_error="主要国籍修改失败且原值回滚校验异常",
        )
        return {
            "player_id": player_id,
            "before_nation_id": current_nation_id,
            "nation_id": nation_id,
            "changed": True,
        }


def clear_world_player_unhappiness(player_id: int) -> dict[str, Any]:
    """Resolve a world player and clear its native active unhappiness records."""
    with borrow_game_reader() as reader:
        _indexed, address, _person = _world_player_index_target(reader, int(player_id))
    return apply_psychological_counseling_effect(
        int(player_id), hex(address), "opened_up",
    )


def apply_player_second_nationality(
    player_id: int, player_address: Any, nation_id: int, nation_address: Any,
    *, replace_existing: bool = False,
) -> dict[str, Any]:
    """Set an eligible other-nationality relation with read-back and rollback data."""
    pid, _path, layout = select_process_layout()
    if not _nationality_layout_supported(layout):
        raise RuntimeError("当前游戏版本尚未验证第二国籍写入")
    with open_process(pid, write_memory=True) as process:
        module = layout.module(process)
        if not module:
            raise RuntimeError(f"{layout.module_name} 尚未加载")
        reader = Reader(process, module.base_address, layout)
        player = _address(player_address)
        person = _validated_player_person(reader, player, int(player_id))
        nation = _validated_nationality_target(
            reader, module.base_address, int(nation_id), nation_address,
        )
        primary = reader.ptr(person + int(layout.person_nationality_offset))
        if primary == nation:
            raise ValueError("所选国家已经是该球员的主要国籍")

        container = reader.ptr(person + int(layout.person_relationships_offset))
        header = reader.bytes(container, 24) if container else None
        if not container or not header or len(header) != 24:
            raise RuntimeError("无法读取球员关系记录")
        begin, end, capacity = struct.unpack("<QQQ", header)
        if (
            not begin or begin > end or end > capacity
            or (end - begin) % SECOND_NATIONALITY_RECORD_SIZE
            or capacity - begin > 16 * 1024 * 1024
        ):
            raise RuntimeError("球员关系记录结构无效")
        existing_entry = 0
        for entry in range(begin, end, SECOND_NATIONALITY_RECORD_SIZE):
            if (
                reader.ptr(entry) == nation
                and reader.u8(entry + 10) == SECOND_NATIONALITY_OBJECT_TYPE
                and reader.u8(entry + 11) in SECOND_NATIONALITY_RELATION_TYPES
            ):
                raise ValueError("该球员已经拥有所选第二国籍")
            if (
                not existing_entry
                and reader.u8(entry + 10) == SECOND_NATIONALITY_OBJECT_TYPE
                and reader.u8(entry + 11) in SECOND_NATIONALITY_RELATION_TYPES
            ):
                existing_entry = entry

        record = (
            struct.pack("<QH", nation, 0)
            + bytes([
                SECOND_NATIONALITY_OBJECT_TYPE,
                SECOND_NATIONALITY_RELATION_TYPE,
                SECOND_NATIONALITY_LEVEL,
                SECOND_NATIONALITY_ELIGIBLE,
                0,
                0xFF,
            ])
        )
        if replace_existing and existing_entry:
            original = reader.bytes(existing_entry, SECOND_NATIONALITY_RECORD_SIZE)
            if original is None or len(original) != SECOND_NATIONALITY_RECORD_SIZE:
                raise RuntimeError("无法读取待覆盖的第二国籍关系")
            try:
                write_process_memory(process, existing_entry, record)
                if reader.bytes(existing_entry, SECOND_NATIONALITY_RECORD_SIZE) != record:
                    raise RuntimeError("第二国籍覆盖回读校验失败")
            except Exception as error:
                try:
                    write_process_memory(process, existing_entry, original)
                except Exception as rollback_error:
                    raise RuntimeError("第二国籍覆盖失败且关系记录回滚异常") from rollback_error
                raise error
            return {
                "player_id": int(player_id), "nation_id": int(nation_id),
                "eligible_for_nation": True,
                "replaced": True,
                "_rollback": {
                    "mode": "replace", "pid": int(pid), "layout_key": str(layout.key),
                    "module_base": int(module.base_address), "entry": int(existing_entry),
                    "record_before": original, "record_after": record,
                },
            }
        allocation = free_address = 0
        slot_before = b""
        header_after = b""
        try:
            if capacity - end >= SECOND_NATIONALITY_RECORD_SIZE:
                slot_before = reader.bytes(end, SECOND_NATIONALITY_RECORD_SIZE)
                if slot_before is None or len(slot_before) != SECOND_NATIONALITY_RECORD_SIZE:
                    raise RuntimeError("无法读取球员关系记录备用空间")
                write_process_memory(process, end, record)
                header_after = struct.pack("<QQQ", begin, end + 16, capacity)
                write_process_memory(process, container, header_after)
                entry = end
            else:
                existing = reader.bytes(begin, end - begin) or b""
                if len(existing) != end - begin:
                    raise RuntimeError("无法完整读取现有球员关系记录")
                allocation, free_address = _remote_malloc_block(process, len(existing) + 16)
                write_process_memory(process, allocation, existing + record)
                header_after = struct.pack(
                    "<QQQ", allocation, allocation + len(existing) + 16,
                    allocation + len(existing) + 16,
                )
                write_process_memory(process, container, header_after)
                entry = allocation + len(existing)
            if reader.bytes(container, 24) != header_after or reader.bytes(entry, 16) != record:
                raise RuntimeError("第二国籍写入回读校验失败")
        except Exception as error:
            restored = False
            try:
                write_process_memory(process, container, header)
                if slot_before:
                    write_process_memory(process, end, slot_before)
                restored = reader.bytes(container, 24) == header
            finally:
                if restored and allocation:
                    _remote_free_block(process, free_address, allocation)
            if not restored:
                raise RuntimeError("第二国籍写入失败且关系容器回滚异常") from error
            raise
        return {
            "player_id": int(player_id), "nation_id": int(nation_id),
            "eligible_for_nation": True,
            "_rollback": {
                "pid": int(pid), "layout_key": str(layout.key),
                "module_base": int(module.base_address), "container": int(container),
                "header_before": header, "header_after": header_after,
                "slot": int(end) if slot_before else 0, "slot_before": slot_before,
                "allocation": int(allocation), "free_address": int(free_address),
            },
        }


def restore_player_second_nationality(result: dict[str, Any]) -> None:
    rollback = dict(result.get("_rollback") or {})
    if not rollback:
        return
    pid, _path, layout = select_process_layout()
    if int(pid) != int(rollback.get("pid") or 0) or str(layout.key) != rollback.get("layout_key"):
        raise RuntimeError("游戏进程已变化，无法安全回滚第二国籍")
    with open_process(pid, write_memory=True) as process:
        module = layout.module(process)
        if not module or int(module.base_address) != int(rollback.get("module_base") or 0):
            raise RuntimeError("游戏模块已变化，无法安全回滚第二国籍")
        reader = Reader(process, module.base_address, layout)
        if rollback.get("mode") == "replace":
            entry = int(rollback["entry"])
            before = bytes(rollback["record_before"])
            after = bytes(rollback["record_after"])
            if reader.bytes(entry, len(after)) != after:
                raise RuntimeError("第二国籍关系已变化，拒绝覆盖后续修改")
            write_process_memory(process, entry, before)
            if reader.bytes(entry, len(before)) != before:
                raise RuntimeError("第二国籍覆盖回滚回读校验失败")
            return
        container = int(rollback["container"])
        if reader.bytes(container, 24) != rollback["header_after"]:
            raise RuntimeError("球员关系记录已变化，拒绝覆盖后续修改")
        write_process_memory(process, container, rollback["header_before"])
        slot = int(rollback.get("slot") or 0)
        if slot:
            write_process_memory(process, slot, rollback["slot_before"])
        if reader.bytes(container, 24) != rollback["header_before"]:
            raise RuntimeError("第二国籍回滚回读校验失败")
        allocation = int(rollback.get("allocation") or 0)
        if allocation:
            _remote_free_block(process, int(rollback.get("free_address") or 0), allocation)


def remove_player_second_nationality(
    player_id: int, player_address: Any, nation_id: int, nation_address: Any,
) -> dict[str, Any]:
    """Remove one verified other-nationality relation from a player vector."""
    pid, _path, layout = select_process_layout()
    if not _nationality_layout_supported(layout):
        raise RuntimeError("当前游戏版本尚未验证第二国籍写入")
    with open_process(pid, write_memory=True) as process:
        module = layout.module(process)
        if not module:
            raise RuntimeError(f"{layout.module_name} 尚未加载")
        reader = Reader(process, module.base_address, layout)
        player = _address(player_address)
        person = _validated_player_person(reader, player, int(player_id))
        nation = _validated_nationality_target(
            reader, module.base_address, int(nation_id), nation_address,
        )
        primary = reader.ptr(person + int(layout.person_nationality_offset))
        if primary == nation:
            raise ValueError("不能移除球员的主要国籍")
        container = reader.ptr(person + int(layout.person_relationships_offset))
        header = reader.bytes(container, 24) if container else None
        if not container or not header or len(header) != 24:
            raise RuntimeError("无法读取球员关系记录")
        begin, end, capacity = struct.unpack("<QQQ", header)
        if (
            not begin or begin > end or end > capacity
            or (end - begin) % SECOND_NATIONALITY_RECORD_SIZE
            or capacity - begin > 16 * 1024 * 1024
        ):
            raise RuntimeError("球员关系记录结构无效")
        entry = 0
        for candidate in range(begin, end, SECOND_NATIONALITY_RECORD_SIZE):
            if (
                reader.ptr(candidate) == nation
                and reader.u8(candidate + 10) == SECOND_NATIONALITY_OBJECT_TYPE
                and reader.u8(candidate + 11) in SECOND_NATIONALITY_RELATION_TYPES
            ):
                entry = candidate
                break
        if not entry:
            raise ValueError("该球员没有指定的第二国籍")
        original = reader.bytes(entry, end - entry)
        if not original or len(original) != end - entry:
            raise RuntimeError("无法读取待移除的国籍关系")
        tail = original[SECOND_NATIONALITY_RECORD_SIZE:]
        updated = tail + (b"\0" * SECOND_NATIONALITY_RECORD_SIZE)
        header_after = struct.pack("<QQQ", begin, end - SECOND_NATIONALITY_RECORD_SIZE, capacity)
        try:
            write_process_memory(process, entry, updated)
            write_process_memory(process, container, header_after)
            if (
                reader.bytes(container, 24) != header_after
                or reader.bytes(entry, len(updated)) != updated
            ):
                raise RuntimeError("第二国籍移除回读校验失败")
        except Exception as error:
            restored = False
            try:
                write_process_memory(process, entry, original)
                write_process_memory(process, container, header)
                restored = (
                    reader.bytes(container, 24) == header
                    and reader.bytes(entry, len(original)) == original
                )
            except Exception:
                restored = False
            if not restored:
                raise RuntimeError("第二国籍移除失败且关系容器回滚异常") from error
            raise
        return {
            "player_id": int(player_id),
            "nation_id": int(nation_id),
            "removed": True,
        }


def _person_relation_layout_supported(layout: Any) -> bool:
    distribution = str(getattr(layout, "distribution", "")).lower()
    game_key = str(getattr(layout, "key", ""))
    identity = str(getattr(layout, "executable_sha256", "") or "").upper()
    return bool(
        (
            distribution == "steam" and game_key in {"fm24", "fm26"}
            or game_key == "fm24" and distribution == "epic"
            and identity == FM24_EPIC_EXE_SHA256
        )
        and getattr(layout, "person_relationships_offset", None) is not None
    )


def _person_relation_record(target_person: int, reason: int) -> bytes:
    return struct.pack("<QH", int(target_person), int(reason)) + bytes([
        PERSON_RELATION_OBJECT_TYPE,
        PERSON_RELATION_TYPE,
        PERSON_RELATION_LEVEL,
        PERSON_RELATION_PERMANENT,
        0,
        0xFF,
    ])


def _person_relation_container(reader: Reader, person: int) -> tuple[int, bytes, int, int, int]:
    offset = getattr(reader.layout, "person_relationships_offset", None)
    container = int(reader.ptr(int(person) + int(offset)) or 0) if offset is not None else 0
    header = reader.bytes(container, 24) if container else None
    if not container or not header or len(header) != 24:
        raise RuntimeError("无法读取人物关系容器")
    begin, end, capacity = struct.unpack("<QQQ", header)
    if begin == end == capacity == 0:
        return container, header, begin, end, capacity
    if (
        not begin or begin > end or end > capacity
        or (end - begin) % 16 or end - begin > 16 * 1024
        or capacity - end > 16 * 1024
    ):
        raise RuntimeError("人物关系容器结构无效")
    return container, header, begin, end, capacity


def _append_person_relation(
    reader: Reader, process: Any, source_person: int, target_person: int, reason: int,
) -> dict[str, Any]:
    container, header, begin, end, capacity = _person_relation_container(reader, source_person)
    record = _person_relation_record(target_person, reason)
    existing = reader.bytes(begin, end - begin) if end > begin else b""
    if existing is None or len(existing) != end - begin:
        raise RuntimeError("无法完整读取人物关系记录")
    for position in range(0, len(existing), 16):
        row = existing[position:position + 16]
        if (
            struct.unpack_from("<Q", row)[0] == int(target_person)
            and struct.unpack_from("<H", row, 8)[0] == int(reason)
            and row[10] == PERSON_RELATION_OBJECT_TYPE
            and row[11] == PERSON_RELATION_TYPE
        ):
            raise ValueError("该人物关系已经存在")

    allocation = free_address = 0
    slot = 0
    header_after: bytes
    try:
        if begin == end == capacity == 0:
            allocation, free_address = _remote_malloc_block(process, 16)
            write_process_memory(process, allocation, record)
            header_after = struct.pack("<QQQ", allocation, allocation + 16, allocation + 16)
            write_process_memory(process, container, header_after)
            slot = allocation
        elif capacity - end >= 16:
            slot = end
            write_process_memory(process, slot, record)
            header_after = struct.pack("<QQQ", begin, end + 16, capacity)
            write_process_memory(process, container, header_after)
        else:
            allocation, free_address = _remote_malloc_block(process, len(existing) + 16)
            write_process_memory(process, allocation, existing + record)
            header_after = struct.pack(
                "<QQQ", allocation, allocation + len(existing) + 16,
                allocation + len(existing) + 16,
            )
            write_process_memory(process, container, header_after)
            slot = allocation + len(existing)
        if reader.bytes(container, 24) != header_after or reader.bytes(slot, 16) != record:
            raise RuntimeError("人物关系写入回读校验失败")
    except Exception as error:
        try:
            write_process_memory(process, container, header)
            restored = reader.bytes(container, 24) == header
        except Exception:
            restored = False
        if allocation:
            _remote_free_block(process, free_address, allocation)
        if not restored:
            raise RuntimeError("人物关系写入失败且容器回滚异常") from error
        raise
    return {
        "container": container, "header_before": header, "header_after": header_after,
        "slot": slot, "allocation": allocation, "free_address": free_address,
    }


def _rollback_person_relation(reader: Reader, process: Any, undo: dict[str, Any]) -> None:
    container = int(undo.get("container") or 0)
    header_before = undo.get("header_before") or b""
    if not container or not header_before or reader.bytes(container, 24) != undo.get("header_after"):
        raise RuntimeError("人物关系已被其他操作改变，拒绝覆盖回滚")
    write_process_memory(process, container, header_before)
    if reader.bytes(container, 24) != header_before:
        raise RuntimeError("人物关系回滚回读校验失败")
    allocation = int(undo.get("allocation") or 0)
    if allocation:
        _remote_free_block(process, int(undo.get("free_address") or 0), allocation)


def _remove_person_relation(
    reader: Reader, process: Any, source_person: int, target_person: int, reason: int,
) -> dict[str, Any]:
    container, header, begin, end, capacity = _person_relation_container(reader, source_person)
    if begin == end == capacity == 0:
        raise ValueError("该人物没有指定关系")
    raw = reader.bytes(begin, end - begin) or b""
    entry_offset = -1
    for position in range(0, len(raw), 16):
        row = raw[position:position + 16]
        if (
            struct.unpack_from("<Q", row)[0] == int(target_person)
            and struct.unpack_from("<H", row, 8)[0] == int(reason)
            and row[10] == PERSON_RELATION_OBJECT_TYPE
            and row[11] == PERSON_RELATION_TYPE
        ):
            entry_offset = position
            break
    if entry_offset < 0:
        raise ValueError("该人物没有指定关系")
    updated = raw[entry_offset + 16:] + (b"\0" * 16)
    header_after = struct.pack("<QQQ", begin, end - 16, capacity)
    try:
        write_process_memory(process, begin + entry_offset, updated)
        write_process_memory(process, container, header_after)
        if reader.bytes(container, 24) != header_after or reader.bytes(begin + entry_offset, len(updated)) != updated:
            raise RuntimeError("人物关系移除回读校验失败")
    except Exception as error:
        try:
            write_process_memory(process, begin + entry_offset, raw[entry_offset:])
            write_process_memory(process, container, header)
            restored = (
                reader.bytes(container, 24) == header
                and reader.bytes(begin + entry_offset, len(raw) - entry_offset) == raw[entry_offset:]
            )
        except Exception:
            restored = False
        if not restored:
            raise RuntimeError("人物关系移除失败且回滚异常") from error
        raise
    return {"player_id": 0, "target_id": int(target_person), "removed": True}


def update_world_player_person_relation(
    player_id: int, target_player_id: int, relation: str, *, remove: bool = False,
) -> dict[str, Any]:
    """Add or remove a validated parent/child relation on the live Person vector."""
    relation = str(relation or "").strip().lower()
    reason = PERSON_RELATION_REASONS.get(relation)
    if reason is None:
        raise ValueError("只支持父母或子女关系")
    if int(player_id) == int(target_player_id):
        raise ValueError("人物关系不能指向球员本人")
    with _writable_game_reader() as (reader, process, _module):
        if not _person_relation_layout_supported(reader.layout):
            raise RuntimeError("当前版本尚未验证人物关系写入")
        _source_index, _source_player, source_person = _world_player_index_target(reader, int(player_id))
        _target_index, _target_player, target_person = _world_player_index_target(reader, int(target_player_id))
        if remove:
            result = _remove_person_relation(reader, process, source_person, target_person, reason)
        else:
            undo = _append_person_relation(reader, process, source_person, target_person, reason)
            result = {"player_id": int(player_id), "target_id": int(target_player_id), "relation": relation, "changed": True, "_undo": undo}
        result.update({"player_id": int(player_id), "target_id": int(target_player_id), "relation": relation, "removed": bool(remove), "changed": True})
        return result


def apply_manager_intimacy_points(
    player_id: int, player_address: Any, manager_id: int, team_id: int,
    *, team_address: Any = 0, manager_address: Any = 0, points: int = 1,
) -> dict[str, Any]:
    if int(points) <= 0:
        raise ValueError("亲密度增加值无效")
    return adjust_manager_intimacy_points(
        player_id, player_address, manager_id, team_id,
        team_address=team_address, manager_address=manager_address, points=points,
    )


def update_player_injury_treatment(
    player_id: int, player_address: Any, game_date: str,
    old_factor: float, new_factor: float,
) -> dict[str, Any]:
    if old_factor not in {1.0, 1.5, 3.0} or new_factor not in {1.0, 1.5, 3.0}:
        raise ValueError("治疗倍率无效")
    current = date.fromisoformat(str(game_date))
    pid, _path, layout = select_process_layout()
    originals: list[tuple[int, bytes]] = []
    with open_process(pid, write_memory=True) as process:
        module = layout.module(process)
        if not module:
            raise RuntimeError(f"{layout.module_name} 尚未加载")
        reader = Reader(process, module.base_address, layout)
        player = _address(player_address)
        _validated_player_person(reader, player, int(player_id))
        container = reader.ptr(player + _injury_list_offset(layout))
        begin = reader.ptr(container) if container else 0
        end = reader.ptr(container + 8) if container else 0
        if not begin or not end or end <= begin or (end - begin) % 8:
            raise ValueError("该球员当前没有伤病")
        try:
            for slot in range(begin, end, 8):
                record = reader.ptr(slot)
                start = decode_date(reader.u32(record + 0x20) or 0) if record else None
                raw = reader.bytes(record + 0x28, 4) if record else None
                if not record or not start or not raw or len(raw) != 4:
                    raise RuntimeError("无法读取伤病持续时间")
                high_raw, low_raw = struct.unpack("<HH", raw)
                elapsed = max(0, (current - start).days)

                def adjusted(value: int) -> int:
                    remaining = max(1, int(value) - elapsed)
                    next_remaining = max(1, int(remaining * old_factor / new_factor + 0.5))
                    return min(0xFFFF, elapsed + next_remaining)

                low, high = sorted((adjusted(low_raw), adjusted(high_raw)))
                updated = struct.pack("<HH", high, low)
                originals.append((record + 0x28, raw))
                write_process_memory(process, record + 0x28, updated)
                if reader.bytes(record + 0x28, 4) != updated:
                    raise RuntimeError("治疗天数写入校验失败")
            details = reader._availability_details(container) or {}
            return_by_start: dict[str, date] = {}
            for injury in details.get("injuries") or []:
                start_text = str(injury.get("start_date") or "")
                return_text = str(injury.get("estimated_return_to") or "")
                if not start_text or not return_text:
                    continue
                target = date.fromisoformat(return_text)
                if target > return_by_start.get(start_text, date.min):
                    return_by_start[start_text] = target
            _sync_medical_unavailability_records(
                process, reader, container, return_by_start,
            )
        except Exception as error:
            rollback_failed = False
            for address, raw in reversed(originals):
                try:
                    write_process_memory(process, address, raw)
                    rollback_failed = rollback_failed or reader.bytes(address, len(raw)) != raw
                except Exception:
                    rollback_failed = True
            if rollback_failed:
                raise RuntimeError("治疗天数写入失败且回滚校验异常") from error
            raise
        details = reader._availability_details(container)
        return {
            "player_id": int(player_id), "factor": float(new_factor),
            "injuries": list((details or {}).get("injuries") or []),
            "unavailability": list((details or {}).get("unavailability") or []),
        }


def _sync_medical_unavailability_records(
    process: Any, reader: Reader, container: int,
    return_by_start: dict[str, date],
    default_return: date | None = None,
) -> list[dict[str, Any]]:
    """Synchronize only finite, matching medical unavailability records."""
    begin = reader.ptr(container + 0x18)
    end = reader.ptr(container + 0x20)
    if begin is None and end is None:
        return []
    if not begin or not end or end < begin or (end - begin) % 8:
        raise RuntimeError("球员不可用记录结构无效")
    count = (end - begin) // 8
    if count > 32:
        raise RuntimeError("球员不可用记录数量异常")
    originals: list[tuple[int, bytes]] = []
    updated_records: list[dict[str, Any]] = []
    try:
        for index in range(count):
            record = reader.ptr(begin + index * 8)
            if not record or reader.u8(record + 0x05) != 29:
                continue
            start = decode_date(reader.u32(record + 0x10) or 0)
            days = reader.u16(record + 0x16)
            if not start or days is None or days >= 0x7FFF:
                continue
            if default_return is not None and (
                start > default_return
                or start + timedelta(days=int(days)) <= default_return
            ):
                continue
            target = return_by_start.get(start.isoformat(), default_return)
            if target is None:
                continue
            next_days = max(0, min(0x7FFE, (target - start).days))
            if next_days == days:
                continue
            address = record + 0x16
            original = struct.pack("<H", days)
            encoded = struct.pack("<H", next_days)
            originals.append((address, original))
            write_process_memory(process, address, encoded)
            if reader.bytes(address, 2) != encoded:
                raise RuntimeError("医疗不可上场期限写入校验失败")
            updated_records.append({
                "start_date": start.isoformat(),
                "before_days": int(days), "after_days": next_days,
                "estimated_return_to": target.isoformat(),
            })
    except Exception as error:
        rollback_failed = False
        for address, original in reversed(originals):
            try:
                write_process_memory(process, address, original)
                rollback_failed = rollback_failed or reader.bytes(address, 2) != original
            except Exception:
                rollback_failed = True
        if rollback_failed:
            raise RuntimeError("医疗不可上场期限写入失败且回滚校验异常") from error
        raise
    return updated_records


def complete_player_medical_treatment(
    player_id: int, player_address: Any, game_date: str,
    injury_start_dates: list[str],
) -> dict[str, Any]:
    current = date.fromisoformat(str(game_date))
    starts = {
        str(value) for value in injury_start_dates
        if str(value or "").strip()
    }
    pid, _path, layout = select_process_layout()
    with open_process(pid, write_memory=True) as process:
        module = layout.module(process)
        if not module:
            raise RuntimeError(f"{layout.module_name} 尚未加载")
        reader = Reader(process, module.base_address, layout)
        player = _address(player_address)
        _validated_player_person(reader, player, int(player_id))
        container = reader.ptr(player + _injury_list_offset(layout))
        if not container:
            return {"player_id": int(player_id), "updated": []}
        updated = _sync_medical_unavailability_records(
            process, reader, container, {start: current for start in starts},
            default_return=current,
        )
        return {"player_id": int(player_id), "updated": updated}


def _apply_native_credit_default_penalty(
    ranked: list[dict[str, Any]], ca_players: list[dict[str, Any]],
    injury_players: list[dict[str, Any]], unpaid: float, loan_amount: float,
    unpaid_ratio: float, pid: int, layout: Any, injury_minimum_days: int = 30,
) -> dict[str, Any]:
    changed: list[dict[str, Any]] = []
    injury_changed: list[dict[str, Any]] = []
    attribute_originals: list[tuple[int, bytes, int]] = []
    injury_originals: list[tuple[int, bytes]] = []
    allocations: list[int] = []
    cave = 0
    cave_releasable = True
    free_address = 0
    with open_process(pid, write_memory=True) as process:
        module = layout.module(process)
        if not module:
            raise RuntimeError("fm.exe 尚未加载")
        reader = Reader(process, module.base_address, layout)

        def remote_free(address: int) -> None:
            if not address or not free_address:
                return
            thread = kernel32.CreateRemoteThread(
                process.handle, None, 0, ctypes.c_void_p(free_address),
                ctypes.c_void_p(address), 0, None,
            )
            if thread:
                try:
                    kernel32.WaitForSingleObject(thread, REMOTE_FREE_THREAD_WAIT_MS)
                finally:
                    kernel32.CloseHandle(thread)

        try:
            for player in ca_players:
                address = _address(player["address"])
                _validated_player_person(reader, address, int(player.get("id") or 0))
                attribute_offset = int(layout.player_attributes_offset)
                original = reader.bytes(address + attribute_offset, 54)
                if not original or len(original) != 54:
                    raise ValueError(f"无法读取球员 {player.get('name') or player.get('id')} 的属性")
                positions = set(player.get("primary_positions") or player.get("positions") or [])
                goalkeeper = "GK" in positions
                position_group = "守门员" if goalkeeper else "外场"
                updated, before, after = _plan_credit_attribute_penalty(original, goalkeeper)
                original_ca = reader.u16(address + int(layout.player_ca_offset))
                if original_ca is None:
                    raise ValueError(f"无法读取球员 {player.get('name') or player.get('id')} 的CA")
                attribute_originals.append((address, original, int(original_ca)))
                write_process_memory(process, address + attribute_offset, updated)
                target_ca = max(1, int(original_ca) - 10)
                write_process_memory(
                    process, address + int(layout.player_ca_offset), struct.pack("<H", target_ca),
                )
                if (
                    reader.bytes(address + attribute_offset, 54) != updated
                    or reader.u16(address + int(layout.player_ca_offset)) != target_ca
                ):
                    raise ValueError(f"球员 {player.get('name') or player.get('id')} 属性写入校验失败")
                changed.append({
                    "id": player.get("id"), "name": player.get("name"),
                    "position_group": position_group, "ca_before": original_ca,
                    "ca_after": target_ca, "before": before, "after": after,
                })

            if injury_players:
                templates = _native_injury_templates(
                    process, reader, minimum_days=injury_minimum_days,
                )
                fracture_templates = [
                    row for row in templates
                    if "骨折" in str(row["name"]) or "骨裂" in str(row["name"])
                ]
                pool = fracture_templates if unpaid < 10000 and fracture_templates else templates
                game_date = decode_date(
                    reader.u32(module.base_address + int(layout.game_date_rva)) or 0
                )
                if not game_date:
                    raise RuntimeError("无法读取 FM 当前游戏日期")
                date_code = (game_date.year << 16) | game_date.timetuple().tm_yday

                _configure_native_remote_calls()
                remote_ucrt = find_module(process, "ucrtbase.dll")
                local_ucrt = kernel32.LoadLibraryW("ucrtbase.dll")
                if not remote_ucrt or not local_ucrt:
                    raise RuntimeError("无法定位 FM C 运行库")
                local_malloc = int(kernel32.GetProcAddress(local_ucrt, b"malloc") or 0)
                local_free = int(kernel32.GetProcAddress(local_ucrt, b"free") or 0)
                malloc_address = remote_ucrt.base_address + local_malloc - int(local_ucrt)
                free_address = remote_ucrt.base_address + local_free - int(local_ucrt)
                if (
                    not local_malloc or not local_free
                    or read_process_memory(process, malloc_address, 16) != ctypes.string_at(local_malloc, 16)
                ):
                    raise RuntimeError("FM 内存分配器校验失败")
                cave = int(kernel32.VirtualAllocEx(
                    process.handle, None, 0x1000, 0x3000, 0x40,
                ) or 0)
                if not cave:
                    raise RuntimeError("无法建立 FM 安全分配调用区")

                def remote_malloc(size: int, result_offset: int) -> int:
                    nonlocal cave_releasable
                    result_address = cave + result_offset
                    code = (
                        b"\x48\x83\xEC\x28"
                        + b"\x48\xB9" + struct.pack("<Q", size)
                        + b"\x48\xB8" + struct.pack("<Q", malloc_address)
                        + b"\xFF\xD0"
                        + b"\x48\xA3" + struct.pack("<Q", result_address)
                        + b"\x48\x83\xC4\x28\x33\xC0\xC3"
                    )
                    write_process_memory(process, cave, code)
                    thread = kernel32.CreateRemoteThread(
                        process.handle, None, 0, ctypes.c_void_p(cave), None, 0, None,
                    )
                    if not thread:
                        raise RuntimeError("FM 内存分配线程创建失败")
                    try:
                        if kernel32.WaitForSingleObject(
                            thread, REMOTE_THREAD_WAIT_MS,
                        ) != WAIT_OBJECT_0:
                            cave_releasable = False
                            raise RuntimeError("FM 内存分配等待失败")
                    finally:
                        kernel32.CloseHandle(thread)
                    raw_pointer = read_process_memory(process, result_address, 8)
                    value = struct.unpack("<Q", raw_pointer)[0] if raw_pointer else 0
                    if not value:
                        raise RuntimeError("FM 内存分配失败")
                    allocations.append(value)
                    return value

                for index, player in enumerate(injury_players):
                    address = _address(player["address"])
                    _validated_player_person(reader, address, int(player.get("id") or 0))
                    container = reader.ptr(address + _injury_list_offset(layout))
                    original = reader.bytes(container, 24) if container else None
                    if not container or not original or len(original) != 24:
                        raise ValueError(f"无法读取球员 {player.get('name') or player.get('id')} 的伤病槽")
                    begin, end, _capacity = struct.unpack("<QQQ", original)
                    if begin or end:
                        if not begin or not end or end < begin or (end - begin) % 8:
                            raise ValueError(f"球员 {player.get('name') or player.get('id')} 的伤病槽无效")
                        existing_pointers = reader.bytes(begin, end - begin) or b""
                        existing_count = (end - begin) // 8
                    else:
                        existing_pointers = b""
                        existing_count = 0
                    template = random.choice(pool)
                    record = remote_malloc(0x50, 0x200 + index * 16)
                    vector_size = (existing_count + 1) * 8
                    vector = remote_malloc(vector_size, 0x208 + index * 16)
                    record_raw = bytearray(template["record"])
                    struct.pack_into("<I", record_raw, 0x20, date_code)
                    write_process_memory(process, record, bytes(record_raw))
                    write_process_memory(
                        process, vector,
                        existing_pointers + struct.pack("<Q", record),
                    )
                    injury_originals.append((container, original))
                    write_process_memory(
                        process, container,
                        struct.pack("<QQQ", vector, vector + vector_size, vector + vector_size),
                    )
                    details = reader._availability_details(container)
                    if not details or details.get("injury_count") != existing_count + 1:
                        raise RuntimeError(f"球员 {player.get('name') or player.get('id')} 伤病写入校验失败")
                    injury_changed.append({
                        "id": player.get("id"), "name": player.get("name"),
                        "injury": template["name"],
                        "minimum_days": template["duration_days_low"],
                        "maximum_days": template["duration_days_high"],
                        "fracture_preferred": unpaid < 10000,
                    })
            allocations.clear()
        except Exception:
            for container, original in reversed(injury_originals):
                try:
                    write_process_memory(process, container, original)
                except Exception:
                    pass
            for address, original, original_ca in reversed(attribute_originals):
                try:
                    write_process_memory(
                        process, address + int(layout.player_attributes_offset), original,
                    )
                    write_process_memory(
                        process, address + int(layout.player_ca_offset), struct.pack("<H", original_ca),
                    )
                except Exception:
                    pass
            for allocated in reversed(allocations):
                remote_free(allocated)
            raise
        finally:
            if cave and cave_releasable:
                kernel32.VirtualFreeEx(process.handle, ctypes.c_void_p(cave), 0, 0x8000)

    return {
        "affected_players": [{"id": row.get("id"), "name": row.get("name")} for row in ranked],
        "attribute_players": changed,
        "injury_players": injury_changed,
        "count": len(ranked), "unpaid": round(float(unpaid), 2),
        "loan_amount": round(float(loan_amount), 2), "unpaid_ratio": round(unpaid_ratio, 4),
    }


def apply_credit_default_penalty(
    players: list[dict[str, Any]], unpaid: float, loan_amount: float,
) -> dict[str, Any]:
    """Apply a ratio-based default penalty with weighted random attribute losses."""
    affected_count, ca_count, injury_count, unpaid_ratio = credit_default_penalty_counts(unpaid, loan_amount)
    ranked = sorted(
        (row for row in players if row.get("address")),
        key=lambda row: (int(row.get("manager_intimacy") or 0) > 0,
                         int(row.get("manager_intimacy") or 0), int(row.get("ca") or 0)),
        reverse=True,
    )[:affected_count]
    if not ranked:
        raise ValueError("没有可执行违约处理的一线球员")
    injury_players = random.sample(ranked, min(injury_count, len(ranked)))
    injury_ids = {int(row.get("id") or 0) for row in injury_players}
    ca_players = [row for row in ranked if int(row.get("id") or 0) not in injury_ids][:ca_count]

    layout_pid, _layout_path, layout = select_process_layout()
    return _apply_native_credit_default_penalty(
        ranked, ca_players, injury_players, unpaid, loan_amount,
        unpaid_ratio, layout_pid, layout,
    )


def _staff_candidate(
    reader: Reader, person: int, team_address: int, manager_id: int, *,
    manager_person: int = 0,
) -> dict[str, Any] | None:
    identifier = reader.u32(person + ENTITY_UID)
    if not identifier or int(identifier) == manager_id:
        return None
    _contract_team, contract_row = _contract(reader, person, team_address)
    if not contract_row:
        return None
    name = _name(reader, person)
    if not name:
        return None
    layout = reader.layout
    contract = int(str(contract_row["address"]), 16)
    complete_object = person - layout.staff_complete_object_offset
    ability_complete_offset = getattr(
        layout, "staff_ability_complete_object_offset", None,
    )
    ability_base = (
        person - int(ability_complete_offset)
        if ability_complete_offset is not None
        else complete_object
    )
    abilities: dict[str, int] = {}
    if layout.staff_abilities_verified:
        raw_abilities = {
            label: reader.u8(ability_base + offset)
            for label, offset in STAFF_COACHING_FIELDS.items()
        }
        invalid_count = sum(
            1 for value in raw_abilities.values()
            if value is None or value > 100
        )
        if invalid_count > max(1, len(STAFF_COACHING_FIELDS) // 3):
            return None
        abilities = {
            label: max(1, min(20, (int(value) + 2) // 5))
            for label, value in raw_abilities.items()
            if value is not None and 0 <= value <= 100
        }
        working_with_youngsters = reader.u8(ability_base + 0x1C)
        determination_raw = reader.u8(ability_base + 0x1D)
        hidden_offset = layout.person_hidden_attributes_offset
        adaptability = (
            reader.u8(person + int(hidden_offset))
            if hidden_offset is not None else None
        )
        if working_with_youngsters is not None and 1 <= working_with_youngsters <= 20:
            abilities["青训培养"] = int(working_with_youngsters)
        if determination_raw is not None and determination_raw <= 100:
            abilities["意志力"] = max(1, min(20, (int(determination_raw) + 2) // 5))
        if adaptability is not None and 1 <= adaptability <= 20:
            abilities["适应能力"] = int(adaptability)
    job_type = (
        reader.u8(contract + layout.staff_job_type_offset)
        if layout.staff_job_type_offset is not None else None
    )
    role = STAFF_JOB_TYPES.get(int(job_type)) if job_type is not None else None
    if not role:
        role = f"职务编号 {job_type}" if job_type is not None else "职务未知"
    raw_wage = (
        reader.u32(contract + layout.staff_wage_offset)
        if layout.staff_wage_offset is not None else None
    )
    start = (
        decode_date(reader.u32(contract + layout.staff_contract_start_offset) or 0)
        if layout.staff_contract_start_offset is not None else None
    )
    expiry = (
        decode_date(reader.u32(contract + layout.staff_contract_expiry_offset) or 0)
        if layout.staff_contract_expiry_offset is not None else None
    )
    read_complete_u16 = lambda offset: (
        reader.u16(complete_object + offset) if offset is not None else None
    )
    coaching_license_code = (
        reader.u8(person + int(layout.staff_coaching_license_offset))
        if layout.staff_coaching_license_offset is not None else None
    )
    coaching_license = None
    if coaching_license_code in COACHING_LICENSE_NAMES:
        next_code = COACHING_LICENSE_NEXT.get(int(coaching_license_code))
        coaching_license = {
            "code": int(coaching_license_code),
            "name": COACHING_LICENSE_NAMES[int(coaching_license_code)],
            "next_code": next_code,
            "next_name": COACHING_LICENSE_NAMES.get(next_code),
            "maximum": next_code is None,
        }
    chairman_attributes = None
    if is_club_controller_job_type(job_type):
        chairman_base = complete_object + int(layout.chairman_base_adjustment)
        fields = {
            "business": layout.chairman_business_offset,
            "interference": layout.chairman_interference_offset,
            "patience": layout.chairman_patience_offset,
            "resources": layout.chairman_resources_offset,
        }
        values = {
            key: _validated_number(
                reader.u8(chairman_base + int(offset)) if offset is not None else None,
                0, 20,
            )
            for key, offset in fields.items()
        }
        if all(value is not None for value in values.values()):
            chairman_attributes = values
    relationship = None
    if manager_person > 0:
        # Match 2.1.2: this value is the staff member's own relationship
        # towards the human manager, not the reverse record.
        from tools.person_relationships import read_person_relationships

        relationship = read_person_relationships(
            reader, person, int(manager_person),
        )
    return {
        "id": int(identifier), "name": name, "role": role,
        "manager_intimacy": int((relationship or {}).get("manager_intimacy") or 0),
        "manager_relation_reason": (relationship or {}).get("manager_relation_reason"),
        "manager_relation_permanence": (relationship or {}).get("manager_relation_permanence"),
        "manager_relation_object_type": (relationship or {}).get("manager_relation_object_type"),
        "manager_relation_type": (relationship or {}).get("manager_relation_type"),
        "person_relations": (relationship or {}).get("person_relations", {}),
        "person_relations_detail": (relationship or {}).get("person_relations_detail", {}),
        "_name_signature": _player_name_signature(reader, person),
        "address": hex(person), "contract_address": hex(contract),
        "wage_raw": raw_wage, "wage_display": _game_wage(raw_wage),
        "contract_start_date": start.isoformat() if start else None,
        "contract_expiry_date": expiry.isoformat() if expiry else None,
        "abilities": abilities,
        "hidden_attributes": _person_hidden_attributes(reader, person),
        "domestic_reputation": read_complete_u16(layout.staff_domestic_reputation_offset),
        "current_reputation": read_complete_u16(layout.staff_current_reputation_offset),
        "world_reputation": read_complete_u16(layout.staff_world_reputation_offset),
        "ca": read_complete_u16(layout.staff_ca_offset),
        "pa": read_complete_u16(layout.staff_pa_offset),
        "coaching_license": coaching_license,
        "job_type": int(job_type) if job_type is not None else None,
        "chairman_attributes": chairman_attributes,
        "ability_mapping": "actual_non_player_v2" if abilities else None,
    }


def _staff_scan_entries(reader: Reader, manager_id: int) -> list[tuple[int, int]]:
    staff_vtable_rva = reader.layout.staff_person_vtable_rva
    if staff_vtable_rva is None:
        return []
    needle = struct.pack("<Q", reader.module_base + staff_vtable_rva)
    # The FM process survives a save switch. Include the human manager UID so
    # staff addresses from the previous save are never reused in the new one.
    directory = database_index_for_reader(reader)
    directory_token = None
    if directory is not None:
        try:
            directory_token = directory.table_token("person")
        except (OSError, RuntimeError, TypeError, ValueError):
            # Some saves expose the independently resolved manager/team
            # context before their native Person table has a valid vector
            # header. The staff reader already has a bounded slab fallback;
            # keep the failed table out of this scan so that fallback can run.
            directory = None
    cache_key = (
        reader.process.pid, reader.module_base, staff_vtable_rva,
        int(manager_id), directory_token,
    )
    with _CACHE_LOCK:
        cached = _STAFF_SCAN_CACHE.get(cache_key)
        staff_entries = list(cached[1]) if cached and monotonic() - cached[0] < 300 else []
    if not staff_entries:
        primary_contract_offset = 0xC8 if reader.layout.key == "fm24" else PERSON_CONTRACT
        if directory is not None:
            try:
                # Join the session-owned Person index build even when its
                # background warm-up is still in progress.  Falling back just
                # because the index was not ready started a second, broad heap
                # scan beside that warm-up and made the club page stall at the
                # staff phase.  ``addresses_for_vtable`` serializes the shared
                # build and returns as soon as the object index is available;
                # the slower name/search projection may continue separately.
                persons = directory.addresses_for_vtable(
                    "person", int(staff_vtable_rva),
                )
                snapshots = reader._fixed_size_snapshots(
                    persons, int(primary_contract_offset) + 8,
                )
                for person in persons:
                    raw = snapshots.get(int(person))
                    contract = (
                        struct.unpack_from("<Q", raw, int(primary_contract_offset))[0]
                        if raw and len(raw) >= int(primary_contract_offset) + 8 else 0
                    )
                    staff_entries.append((int(person), int(contract)))
            except (OSError, RuntimeError, TypeError, ValueError):
                staff_entries = []
            if staff_entries:
                with _CACHE_LOCK:
                    _STAFF_SCAN_CACHE[cache_key] = (monotonic(), list(staff_entries))
                    _prune_timed_cache_locked(
                        _STAFF_SCAN_CACHE, ttl=600, maximum=8,
                        current_pid=int(reader.process.pid),
                    )
                return staff_entries
        regions = [region for region in iter_readable_regions(reader.process) if region.type == MEM_PRIVATE and region.size <= 512 * 1024 * 1024]
        selected = []
        for region in regions:
            # The previous three 1 MiB probes read roughly 15 GiB on a large
            # save before the actual staff scan began.  Staff objects live in a
            # dense slab, so small probes across the whole allocation identify
            # it reliably while keeping startup bounded.
            sample_size = min(64 * 1024, region.size)
            sample_offsets = {
                0,
                max(0, region.size // 4 - sample_size // 2),
                max(0, region.size // 2 - sample_size // 2),
                max(0, region.size * 3 // 4 - sample_size // 2),
                max(0, region.size - sample_size),
            }
            hit_count = 0
            for offset in sample_offsets:
                sample = reader.bytes(
                    region.base_address + offset,
                    min(sample_size, region.size - offset),
                )
                if sample:
                    hit_count += sample.count(needle)
            if hit_count >= 4:
                selected.append(region)
        for region in selected:
            offset, carry = 0, b""
            while offset < region.size:
                length = min(8 * 1024 * 1024, region.size - offset)
                block = reader.bytes(region.base_address + offset, length)
                if block:
                    data = carry + block; base = region.base_address + offset - len(carry); position = 0
                    while True:
                        position = data.find(needle, position)
                        if position < 0:
                            break
                        person = base + position
                        contract_position = position + primary_contract_offset
                        contract = (
                            struct.unpack_from("<Q", data, contract_position)[0]
                            if contract_position + 8 <= len(data)
                            else int(reader.ptr(person + primary_contract_offset) or 0)
                        )
                        staff_entries.append((person, int(contract)))
                        position += 8
                    carry = data[-7:]
                else:
                    carry = b""
                offset += length
        with _CACHE_LOCK:
            _STAFF_SCAN_CACHE[cache_key] = (monotonic(), list(staff_entries))
            _prune_timed_cache_locked(
                _STAFF_SCAN_CACHE, ttl=600, maximum=8,
                current_pid=int(reader.process.pid),
            )
    return staff_entries


def _scan_head_coaches(reader: Reader, manager_id: int = 0) -> list[dict[str, Any]]:
    layout = reader.layout
    if layout.staff_job_type_offset is None:
        return []
    rows: dict[int, dict[str, Any]] = {}
    for person, primary_contract in _staff_scan_entries(reader, manager_id):
        if (
            not primary_contract
            or reader.ptr(primary_contract + 0x08) != person
        ):
            continue
        team_address = int(reader.ptr(primary_contract + 0x10) or 0)
        manager_object = person - int(
            getattr(layout, "team_manager_person_offset", layout.staff_complete_object_offset)
        )
        if (
            not team_address
            or int(reader.ptr(team_address + TEAM_MANAGER) or 0) != manager_object
        ):
            continue
        # The verified TEAM_MANAGER back-reference is authoritative. Some
        # employed head coaches use a contract job code other than 16, so the
        # code must not hide a person who currently occupies the manager slot.
        team = reader.team(team_address)
        identifier = int(reader.u32(person + ENTITY_UID) or 0)
        if (
            not identifier or identifier == int(manager_id)
            or not team or team.get("team_type") != "club"
        ):
            continue
        name = _name(reader, person)
        if not name:
            continue
        expiry = (
            decode_date(reader.u32(primary_contract + layout.staff_contract_expiry_offset) or 0)
            if layout.staff_contract_expiry_offset is not None else None
        )
        rows[identifier] = {
            "id": identifier,
            "name": name,
            "team_id": int(team.get("id") or 0),
            "team_name": str(team.get("name") or team.get("short_name") or team.get("id") or ""),
            "contract_expiry_date": expiry.isoformat() if expiry else None,
            "address": hex(person),
            "contract_address": hex(primary_contract),
            "team_address": hex(team_address),
        }
    return sorted(rows.values(), key=lambda row: (row["name"].casefold(), row["id"]))


def _scan_free_manager_candidates(
    reader: Reader, manager_id: int = 0,
) -> list[dict[str, Any]]:
    layout = reader.layout
    staff_vtable_rva = layout.staff_person_vtable_rva
    if staff_vtable_rva is None:
        return []
    cache_key = (
        int(reader.process.pid), int(reader.module_base),
        int(staff_vtable_rva), int(manager_id),
    )
    with _CACHE_LOCK:
        cached = _FREE_STAFF_SCAN_CACHE.get(cache_key)
        if cached and monotonic() - cached[0] < 300:
            return [dict(row) for row in cached[1]]

    manager_offset = int(getattr(
        layout, "team_manager_person_offset", layout.staff_complete_object_offset,
    ))
    manager_vtables = {
        int(reader.ptr(_address(row.get("address")) - manager_offset) or 0)
        for row in _scan_head_coaches(reader, manager_id)
        if _address(row.get("address")) > manager_offset
    }
    manager_vtables.discard(0)
    if not manager_vtables:
        return []
    primary_contract_offset = 0xC8 if layout.key == "fm24" else PERSON_CONTRACT
    rows: dict[int, dict[str, Any]] = {}
    for person, _captured_contract in _staff_scan_entries(reader, manager_id):
        identifier = int(reader.u32(person + ENTITY_UID) or 0)
        if (
            not identifier or identifier == int(manager_id)
            or reader.ptr(person + primary_contract_offset)
            or int(reader.ptr(person - manager_offset) or 0) not in manager_vtables
        ):
            continue
        name = _name(reader, person)
        if not name:
            continue
        rows[identifier] = {
            "id": identifier,
            "name": name,
            "team_id": 0,
            "team_name": "自由职员",
            "contract_expiry_date": None,
            "free_agent": True,
        }
    result = sorted(rows.values(), key=lambda row: (row["name"].casefold(), row["id"]))
    with _CACHE_LOCK:
        _FREE_STAFF_SCAN_CACHE.clear()
        _FREE_STAFF_SCAN_CACHE[cache_key] = (monotonic(), [dict(row) for row in result])
    return result


def invalidate_head_coach_search_cache() -> None:
    with _CACHE_LOCK:
        _STAFF_SCAN_CACHE.clear()
        _FREE_STAFF_SCAN_CACHE.clear()


def search_head_coaches(
    query: str = "", *, exclude_team_id: int = 0, manager_id: int = 0,
    limit: int = 50,
) -> dict[str, Any]:
    normalized = str(query or "").strip().casefold()
    bounded_limit = max(1, min(100, int(limit or 50)))
    pid, _path, layout = select_process_layout()
    with open_process(pid) as process:
        module = layout.module(process)
        if not module:
            raise RuntimeError(f"{layout.module_name} 尚未加载")
        reader = Reader(process, module.base_address, layout)
        active_rows = _scan_head_coaches(reader, manager_id)
        free_rows = _scan_free_manager_candidates(reader, manager_id) if normalized else []
        combined = {int(row["id"]): row for row in free_rows}
        combined.update({int(row["id"]): row for row in active_rows})
        matches = [
            row for row in combined.values()
            if int(row.get("team_id") or 0) != int(exclude_team_id)
            and (not normalized or normalized in str(row.get("name") or "").casefold())
        ]
        matches.sort(key=lambda row: (str(row.get("name") or "").casefold(), int(row["id"])))
    public_rows = [
        {
            "id": int(row["id"]),
            "name": str(row["name"]),
            "team_id": int(row["team_id"]),
            "team_name": str(row["team_name"]),
            "contract_expiry_date": row.get("contract_expiry_date"),
            "free_agent": bool(row.get("free_agent")),
        }
        for row in matches[:bounded_limit]
    ]
    return {
        "head_coaches": public_rows,
        "count": len(public_rows),
        "total_matches": len(matches),
        "truncated": len(matches) > bounded_limit,
    }


def search_world_players(
    query: str = "", *, page: int = 1, page_size: int = 30,
    force_refresh: bool = False, sort_by: str = "pa", sort_order: str = "desc",
    gender: str = "all", nationality_id: int = 0,
    min_age: int = 0, max_age: int = 0,
    min_ca: int = 0, max_ca: int = 0,
    min_pa: int = 0, max_pa: int = 0,
    candidate_uids: tuple[int, ...] = (),
    live_summary: bool = True,
) -> dict[str, Any]:
    """Search cached rows and optionally hydrate the visible page from FM."""
    with borrow_game_reader() as reader:
        directory = database_index_for_reader(reader, force_retry=force_refresh)
        if directory is None:
            detail = database_index_error_for_reader(reader)
            if detail:
                raise RuntimeError(f"世界球员数据库目录初始化失败：{detail}")
            raise RuntimeError("当前版本尚未建立可搜索的 Person 数据库目录")
        layout = reader.layout
        game_date = (
            decode_date(
                reader.u32(reader.module_base + int(layout.game_date_rva)) or 0,
            )
            if getattr(layout, "game_date_rva", None) is not None else None
        )
        if candidate_uids:
            result = directory.search_players_by_uids(
                candidate_uids, query=query, page=page, page_size=page_size,
                sort_by=sort_by, sort_order=sort_order, gender=gender,
                nationality_id=nationality_id,
                min_age=min_age, max_age=max_age,
                min_ca=min_ca, max_ca=max_ca,
                min_pa=min_pa, max_pa=max_pa,
                game_date=game_date,
            )
        else:
            result = directory.search_players(
                query, page=page, page_size=page_size,
                force_refresh=force_refresh, sort_by=sort_by, sort_order=sort_order,
                gender=gender, nationality_id=nationality_id,
                min_age=min_age, max_age=max_age,
                min_ca=min_ca, max_ca=max_ca,
                min_pa=min_pa, max_pa=max_pa,
                game_date=game_date,
            )
        option_payload = result.setdefault("filter_options", {})
        nationality_ids = option_payload.pop("nationality_ids", [])
        option_payload["nationalities"] = sorted(
            (
                {"id": int(nation_id), "name": str(NATION_NAMES[int(nation_id)])}
                for nation_id in nationality_ids if int(nation_id) in NATION_NAMES
            ),
            key=lambda row: row["name"],
        )
        for row in result.get("players") or []:
            nation_id = int(row.get("nationality_id") or 0)
            if nation_id in NATION_NAMES:
                row.setdefault("nationality", str(NATION_NAMES[nation_id]))
        if live_summary:
            result["players"] = _world_player_table_rows(
                reader, list(result.get("players") or []),
            )
        result.setdefault("index", {})["live_summary"] = bool(live_summary)
        result["index"]["session_generation"] = int(
            getattr(reader, "session_generation", 0) or 0
        )
        result["gender_supported"] = bool(
            str(getattr(reader.layout, "key", "")).startswith("fm26")
            and getattr(reader.layout, "person_flags_offset", None) is not None
        )
        return result


def _world_player_index_target(
    reader: Reader, player_id: int,
) -> tuple[dict[str, Any], int, int]:
    """Resolve one indexed player and revalidate its outer and Person objects."""
    player_id = int(player_id)
    if player_id <= 0:
        raise ValueError("球员 ID 无效")
    directory = database_index_for_reader(reader)
    if directory is None:
        raise RuntimeError("当前版本尚未建立可搜索的 Person 数据库目录")
    indexed = directory.player_for_uid(player_id)
    if not indexed:
        raise ValueError("当前存档中找不到该球员")
    address = _address(indexed.get("address"))
    person = _validated_player_person(reader, address, player_id)
    if int(person) != _address(indexed.get("person_address")):
        raise ValueError("球员 Person 对象与当前数据库目录不一致")
    return indexed, address, person


def _read_world_player_previous_club(
    reader: Reader, person: int,
) -> dict[str, Any]:
    """Read a build-gated Person.PreviousClub pointer without guessing."""
    result = {
        "previous_club_state": "unavailable",
        "previous_club_id": None,
        "previous_club_name": None,
    }
    offset = getattr(reader.layout, "person_previous_club_offset", None)
    club_vtable_rva = getattr(reader.layout, "club_vtable_rva", None)
    if offset is None or club_vtable_rva is None:
        return result
    try:
        raw = reader.bytes(int(person) + int(offset), 8)
        if raw is None or len(raw) != 8:
            return result
        club = int(struct.unpack("<Q", raw)[0])
        if not club:
            result["previous_club_state"] = "none"
            return result
        if int(reader.ptr(club) or 0) != reader.module_base + int(club_vtable_rva):
            return result
        club_id = int(reader.u32(club + ENTITY_UID) or 0)
        club_name = str(
            reader.fm_string_at(club + CLUB_NAME_SHORT)
            or reader.fm_string_at(club + CLUB_NAME_FULL)
            or ""
        ).strip()
        if club_id <= 0 or not club_name:
            return result
    except (OSError, RuntimeError, TypeError, ValueError, struct.error):
        return result
    return {
        "previous_club_state": "available",
        "previous_club_id": club_id,
        "previous_club_name": club_name,
    }


def read_world_player_profile(player_id: int) -> dict[str, Any]:
    """Resolve a world player by UID and return the shared live detail payload."""
    player_id = int(player_id)
    with borrow_game_reader() as reader:
        indexed, address, person = _world_player_index_target(reader, player_id)
        directory = database_index_for_reader(reader)
        if directory is None:
            raise RuntimeError("当前版本尚未建立可搜索的数据库目录")

        layout = reader.layout
        prefetch = getattr(reader, "prefetch", None)
        if callable(prefetch):
            try:
                prefetch(address, 0x500)
                _prefetch_roster_dependencies(reader, [indexed])
            except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
                # Prefetch is an optional latency optimization.  Every factual
                # read below retains the original narrow-read fallback.
                pass
        position_raw = reader.bytes(
            address + layout.player_positions_offset, len(POSITION_NAMES),
        ) or b""
        if len(position_raw) != len(POSITION_NAMES):
            raise RuntimeError("无法读取球员位置数据")
        position_ratings = {
            label: int(value)
            for label, value in zip(POSITION_NAMES, position_raw)
            if int(value) > 1
        }
        ca = (
            reader.u8(address + layout.player_ca_offset)
            if layout.player_ca_bytes == 1
            else reader.u16(address + layout.player_ca_offset)
        )
        pa = reader.u16(address + layout.player_pa_offset)
        if not 1 <= int(ca or 0) <= 200 or not 1 <= int(pa or 0) <= 200:
            raise RuntimeError("无法安全读取球员当前 CA/PA")
        game_date = (
            decode_date(reader.u32(reader.module_base + int(layout.game_date_rva)) or 0)
            if layout.game_date_rva is not None else None
        )
        contract_offset = FM24_PERSON_CONTRACT if layout.key.startswith("fm24") else PERSON_CONTRACT
        contract = int(reader.ptr(person + contract_offset) or 0)
        team_address = 0
        if contract and int(reader.ptr(contract + 0x08) or 0) == person:
            candidate_team = int(reader.ptr(contract + 0x10) or 0)
            if candidate_team and reader.team(candidate_team):
                team_address = candidate_team
        summary = {
            "id": player_id,
            "name": str(indexed.get("name") or player_id),
            "address": hex(address),
            "object_type": indexed.get("object_type"),
            "positions": position_ratings,
            "ca": int(ca),
            "pa": int(pa),
            "fitness_percent": (
                round((reader.u16(address + int(layout.player_fitness_offset)) or 0) / 100, 2)
                if layout.player_fitness_offset is not None else None
            ),
            "sharpness_percent": (
                round((reader.u16(address + int(layout.player_sharpness_offset)) or 0) / 100, 2)
                if layout.player_sharpness_offset is not None else None
            ),
            "fatigue_raw": (
                reader.u16(address + int(layout.player_fatigue_offset))
                if layout.player_fatigue_offset is not None else None
            ),
            "morale_raw": (
                reader.u8(address + int(layout.player_morale_offset))
                if layout.player_morale_offset is not None else None
            ),
        }
        raw_snapshot = {"positions": bytes(position_raw)}
        player = read_roster_player_profile(
            reader, summary, team_address, game_date=game_date,
            raw_snapshot=raw_snapshot,
        )
        if not player or int(player.get("id") or 0) != player_id:
            raise RuntimeError("球员详情读取失败")
        attach_retirement_details(reader, [player])
        editor_options: dict[str, Any] = {
            "languages": [], "preferred_moves": [],
            "nations": [
                {"id": int(nation_id), "name": str(name)}
                for nation_id, name in sorted(NATION_NAMES.items(), key=lambda item: str(item[1]))
            ],
        }
        try:
            editor_options["languages"] = directory.public_language_catalog()
        except (OSError, RuntimeError, TypeError, ValueError):
            editor_options["languages"] = []
        try:
            from tools.preferred_moves import preferred_move_catalog_for_current_game

            editor_options["preferred_moves"] = preferred_move_catalog_for_current_game()
        except (OSError, RuntimeError, TypeError, ValueError):
            editor_options["preferred_moves"] = []
        primary_contract = contract
        contract_size = _world_player_contract_size(layout)
        contract_raw = reader.bytes(primary_contract, contract_size) if primary_contract else b""
        contract_raw = contract_raw or b""
        if (
            len(contract_raw) != contract_size
            or struct.unpack_from("<Q", contract_raw, 0x08)[0] != int(person)
        ):
            contract_raw = b""
        contract_bonus_raw = b""
        if len(contract_raw) == contract_size:
            bonus_vector_offset = 0x60 if layout.key.startswith("fm24") else 0x68
            bonus_limit = 64 if layout.key.startswith("fm24") else 1000
            begin, end, capacity = struct.unpack_from(
                "<QQQ", contract_raw, bonus_vector_offset,
            )
            if not begin and not end and not capacity:
                contract_bonus_raw = b""
            elif (
                begin and begin <= end <= capacity
                and (end - begin) % 8 == 0
                and (end - begin) // 8 <= bonus_limit
            ):
                candidate = reader.bytes(begin, end - begin)
                if candidate is not None and len(candidate) == end - begin:
                    contract_bonus_raw = candidate
        joined_offset = _person_joined_club_offset(reader)
        joined_raw = reader.bytes(person + joined_offset, 4) or b""
        team = reader.team(team_address) if team_address else None
        previous_club = _read_world_player_previous_club(reader, person)
        name_values = {
            "full_name": reader.fm_string_at(
                person + int(layout.person_full_name_offset)
            ),
            "first_name": reader.fm_nested_string_at(
                person + int(layout.person_first_name_offset)
            ),
            "last_name": reader.fm_nested_string_at(
                person + int(layout.person_last_name_offset)
            ),
            "common_name": reader.fm_nested_string_at(
                person + int(layout.person_common_name_offset)
            ),
        }
        player.update({
            "team_id": int((team or {}).get("id") or 0),
            "team_name": str(
                (team or {}).get("short_name") or (team or {}).get("name") or "自由球员"
            ),
            "team_address": hex(team_address) if team_address else None,
            **previous_club,
            "has_unhappiness": _safe_person_has_unhappiness(reader, person, game_date),
            "editor": {
                "positions": {
                    label: int(value)
                    for label, value in zip(POSITION_NAMES, position_raw)
                },
                "capabilities": {
                    "date_of_birth": layout.person_date_of_birth_offset is not None,
                    "height_cm": layout.player_height_offset is not None,
                    "weight_kg": layout.player_weight_offset is not None,
                    "names": any(name_values.values()),
                    "primary_nationality": _nationality_layout_supported(layout),
                    "asking_price": True,
                    "nationality_eligibility": int(
                        player.get("nationality_eligibility") or -1
                    ) in NATIONALITY_INFO_NAMES,
                    "fitness": layout.player_fitness_offset is not None,
                    "sharpness": layout.player_sharpness_offset is not None,
                    "fatigue": layout.player_fatigue_offset is not None,
                    "morale": layout.player_morale_offset is not None,
                    "home_reputation": layout.player_home_reputation_offset is not None,
                    "current_reputation": layout.player_current_reputation_offset is not None,
                    "world_reputation": layout.player_world_reputation_offset is not None,
                    "person_hidden_attributes": layout.person_hidden_attributes_offset is not None,
                    "contract": len(contract_raw) == contract_size,
                    "contract_happiness": (
                        layout.key.startswith("fm26") and len(contract_raw) == contract_size
                    ),
                    "joined_club_date": len(joined_raw) == 4,
                },
                "expected_raw": {
                    "names": name_values,
                    "positions": position_raw.hex(),
                    "attributes": bytes(
                        raw_snapshot.get("attributes") or b""
                    ).hex(),
                    "person_hidden_attributes": (
                        reader.bytes(person + int(layout.person_hidden_attributes_offset), 8) or b""
                    ).hex() if layout.person_hidden_attributes_offset is not None else "",
                    "contract": contract_raw.hex(),
                    "contract_bonuses": contract_bonus_raw.hex(),
                    "person_joined_club_date": joined_raw.hex(),
                },
                "options": editor_options,
            },
        })
        player.pop("_name_signature", None)
        return player


def update_world_player_fields(
    player_id: int, changes: dict[str, Any], expected: dict[str, Any],
) -> dict[str, Any]:
    """Apply direct player-editor scalar changes as one verified transaction."""
    if not isinstance(changes, dict) or not isinstance(expected, dict):
        raise ValueError("球员编辑参数不完整")
    allowed = {
        "ca", "pa", "date_of_birth", "height_cm", "weight_kg",
        "asking_price", "nationality_eligibility",
        "fitness", "sharpness", "fatigue", "morale",
        "home_reputation", "current_reputation", "world_reputation",
        "positions", "attributes", "hidden_attributes", "joined_club_date",
    }
    unknown = set(changes) - allowed
    if unknown:
        raise ValueError(f"包含尚未支持的球员字段：{', '.join(sorted(unknown))}")

    def integer(value: Any, label: str, minimum: int, maximum: int) -> int:
        if isinstance(value, bool):
            raise ValueError(f"{label}必须是整数")
        try:
            parsed = int(value)
        except (TypeError, ValueError) as error:
            raise ValueError(f"{label}必须是整数") from error
        if str(value).strip() not in {str(parsed), f"{parsed}.0"} and not isinstance(value, int):
            raise ValueError(f"{label}必须是整数")
        if not minimum <= parsed <= maximum:
            raise ValueError(f"{label}必须在 {minimum} 至 {maximum} 之间")
        return parsed

    expected_labels = {
        "ca": "CA", "pa": "PA", "date_of_birth": "出生日期",
        "height_cm": "身高", "weight_kg": "体重", "fitness": "体能",
        "sharpness": "比赛状态", "fatigue": "疲劳值", "morale": "士气",
        "home_reputation": "本国声望", "current_reputation": "当前声望",
        "world_reputation": "世界声望", "joined_club_date": "加盟当前球队日期",
    }
    expected_labels.update({
        "asking_price": "挂牌价",
        "nationality_eligibility": "国家队资格",
    })

    def expected_value(name: str, current: Any) -> None:
        if name not in expected or expected.get(name) != current:
            raise ValueError(
                f"{expected_labels.get(name, name)} 已发生变化，请重新打开球员详情"
            )

    memory_changes: dict[int, dict[str, Any]] = {}

    def stage(address: int, original: bytes, updated: bytes) -> None:
        if original == updated:
            return
        prior = memory_changes.get(int(address))
        if prior and bytes(prior["updated"]) != bytes(updated):
            raise RuntimeError("球员编辑字段发生地址冲突")
        memory_changes[int(address)] = {
            "address": int(address), "original": bytes(original), "updated": bytes(updated),
        }

    with _writable_game_reader() as (reader, process, _module):
        _indexed, player, person = _world_player_index_target(reader, int(player_id))
        layout = reader.layout
        scalar_specs = {
            "ca": (layout.player_ca_offset, layout.player_ca_bytes, 1, 200),
            "pa": (layout.player_pa_offset, 2, 1, 200),
            "height_cm": (layout.player_height_offset, 1, 100, 250),
            "weight_kg": (layout.player_weight_offset, 1, 30, 200),
            "asking_price": (_world_player_asking_price_offset(layout), 4, 0, 0xFFFFFFFF),
            "nationality_eligibility": (
                _world_player_nationality_eligibility_offset(layout), 1, 0, 255,
            ),
            "morale": (layout.player_morale_offset, 1, 0, 20),
            "fatigue": (layout.player_fatigue_offset, 2, 0, 65535),
            "home_reputation": (layout.player_home_reputation_offset, 2, 0, 10000),
            "current_reputation": (layout.player_current_reputation_offset, 2, 0, 10000),
            "world_reputation": (layout.player_world_reputation_offset, 2, 0, 10000),
        }
        player_fields: dict[str, tuple[int, int]] = {}
        if "positions" in changes:
            player_fields["positions"] = (
                int(layout.player_positions_offset), len(POSITION_NAMES),
            )
        if {"attributes", "hidden_attributes"} & set(changes):
            player_fields["attributes"] = (
                int(layout.player_attributes_offset), 54,
            )
        for name, (offset, width, _minimum, _maximum) in scalar_specs.items():
            if name in changes and offset is not None:
                player_fields[name] = (int(offset), int(width))
        for name, offset in (
            ("fitness", layout.player_fitness_offset),
            ("sharpness", layout.player_sharpness_offset),
        ):
            if name in changes and offset is not None:
                player_fields[name] = (int(offset), 2)

        hidden_offset = layout.person_hidden_attributes_offset
        person_fields: dict[str, tuple[int, int]] = {}
        if "hidden_attributes" in changes and hidden_offset is not None:
            person_fields["person_hidden_attributes"] = (int(hidden_offset), 8)
        if (
            "date_of_birth" in changes
            and layout.person_date_of_birth_offset is not None
        ):
            person_fields["date_of_birth"] = (
                int(layout.person_date_of_birth_offset), 4,
            )
        if "joined_club_date" in changes:
            person_fields["joined_club_date"] = (
                _person_joined_club_offset(reader), 4,
            )

        player_raw = _read_relative_memory_fields(reader, player, player_fields)
        person_raw = _read_relative_memory_fields(reader, person, person_fields)
        raw_positions = player_raw.get("positions") or b""
        raw_attributes = player_raw.get("attributes") or b""
        raw_person_hidden = person_raw.get("person_hidden_attributes") or b""
        updated_attribute_block = bytearray(raw_attributes)
        if "positions" in changes and len(raw_positions) != len(POSITION_NAMES):
            raise RuntimeError("无法读取球员位置数据")
        if ({"attributes", "hidden_attributes"} & set(changes)) and len(raw_attributes) != 54:
            raise RuntimeError("无法读取球员属性数据")
        raw_expected = expected.get("expected_raw")
        if not isinstance(raw_expected, dict):
            raise ValueError("球员旧值签名缺失，请重新打开球员详情")
        if "positions" in changes and raw_expected.get("positions") != raw_positions.hex():
            raise ValueError("球员位置已发生变化，请重新打开球员详情")
        if (
            ({"attributes", "hidden_attributes"} & set(changes))
            and raw_expected.get("attributes") != raw_attributes.hex()
        ):
            raise ValueError("球员属性已发生变化，请重新打开球员详情")
        if (
            "hidden_attributes" in changes and hidden_offset is not None
            and raw_expected.get("person_hidden_attributes") != raw_person_hidden.hex()
        ):
            raise ValueError("球员隐藏属性已发生变化，请重新打开球员详情")

        labels = {
            "ca": "CA", "pa": "PA", "height_cm": "身高", "weight_kg": "体重",
            "morale": "士气", "fatigue": "疲劳值", "home_reputation": "本国声望",
            "current_reputation": "当前声望", "world_reputation": "世界声望",
        }
        labels.update({
            "asking_price": "挂牌价",
            "nationality_eligibility": "国家队资格",
        })
        for name, (offset, width, minimum, maximum) in scalar_specs.items():
            if name not in changes:
                continue
            if offset is None:
                raise RuntimeError(f"当前游戏版本尚未映射{labels[name]}")
            address = player + int(offset)
            original = player_raw.get(name)
            if not original or len(original) != int(width):
                raise RuntimeError(f"无法读取球员{labels[name]}")
            current = int.from_bytes(original, "little", signed=False)
            expected_value(name, current)
            target = integer(changes[name], labels[name], minimum, maximum)
            if name == "nationality_eligibility" and target not in NATIONALITY_INFO_NAMES:
                raise ValueError("国家队资格值无效")
            stage(address, original, target.to_bytes(int(width), "little"))

        for name, offset in (
            ("fitness", layout.player_fitness_offset),
            ("sharpness", layout.player_sharpness_offset),
        ):
            if name not in changes:
                continue
            if offset is None:
                raise RuntimeError(f"当前游戏版本尚未映射{name}")
            address = player + int(offset)
            original = player_raw.get(name)
            if not original or len(original) != 2:
                raise RuntimeError(f"无法读取球员{name}")
            current_raw = int.from_bytes(original, "little")
            try:
                expected_raw_value = round(float(expected.get(name)) * 100)
                target_raw = round(float(changes[name]) * 100)
            except (TypeError, ValueError) as error:
                raise ValueError(f"{name} 必须是数字") from error
            if current_raw != expected_raw_value:
                raise ValueError(f"{name} 已发生变化，请重新打开球员详情")
            if not 0 <= target_raw <= 10000:
                raise ValueError(f"{name} 必须在 0 至 100 之间")
            stage(address, original, struct.pack("<H", target_raw))

        if "date_of_birth" in changes:
            offset = layout.person_date_of_birth_offset
            if offset is None:
                raise RuntimeError("当前游戏版本尚未映射球员出生日期")
            address = person + int(offset)
            original = person_raw.get("date_of_birth")
            if not original or len(original) != 4:
                raise RuntimeError("无法读取球员出生日期")
            before = _decode_person_birth_date_raw(layout, original)
            expected_value("date_of_birth", before.isoformat() if before else None)
            try:
                target_date = date.fromisoformat(str(changes["date_of_birth"]))
            except ValueError as error:
                raise ValueError("出生日期格式无效") from error
            if not 1900 <= target_date.year <= 2200:
                raise ValueError("出生年份必须在 1900 至 2200 之间")
            day_of_year = (target_date - date(target_date.year, 1, 1)).days + 1
            updated = (
                struct.pack("<HH", day_of_year, target_date.year)
                if layout.person_date_of_birth_day_year
                else struct.pack("<I", (target_date.year << 16) | day_of_year)
            )
            stage(address, original, updated)

        if "joined_club_date" in changes:
            address = person + _person_joined_club_offset(reader)
            original = person_raw.get("joined_club_date")
            if not original or len(original) != 4:
                raise RuntimeError("无法读取球员加盟当前球队日期")
            expected_raw_joined = raw_expected.get("person_joined_club_date")
            if expected_raw_joined and expected_raw_joined != original.hex():
                raise ValueError("球员加盟当前球队日期已发生变化，请重新打开球员详情")
            current_date = decode_date(int.from_bytes(original, "little"))
            expected_value(
                "joined_club_date",
                current_date.isoformat() if current_date else None,
            )
            target_value = changes.get("joined_club_date")
            if target_value in (None, ""):
                target_raw = 0
            else:
                try:
                    target_date = date.fromisoformat(str(target_value))
                except (TypeError, ValueError) as error:
                    raise ValueError("加盟当前球队日期格式必须为 YYYY-MM-DD") from error
                if not 1900 <= target_date.year <= 2200:
                    raise ValueError("加盟当前球队日期年份超出安全范围")
                target_raw = (
                    (target_date.year << 16) | target_date.timetuple().tm_yday
                ) | (int.from_bytes(original, "little") & 0xFE00)
            stage(address, original, int(target_raw).to_bytes(4, "little"))

        if "positions" in changes:
            requested = changes.get("positions")
            expected_positions = expected.get("positions")
            if not isinstance(requested, dict) or not isinstance(expected_positions, dict):
                raise ValueError("球员位置编辑参数不完整")
            updated = bytearray(raw_positions)
            for label, raw_value in requested.items():
                if label not in POSITION_NAMES:
                    raise ValueError(f"未知球员位置：{label}")
                index = POSITION_NAMES.index(label)
                if expected_positions.get(label) != int(raw_positions[index]):
                    raise ValueError(f"{label} 位置熟练度已发生变化，请重新打开详情")
                updated[index] = integer(raw_value, f"{label} 熟练度", 0, 20)
            stage(
                player + layout.player_positions_offset,
                raw_positions, bytes(updated),
            )

        if "attributes" in changes:
            requested = changes.get("attributes")
            expected_attributes = expected.get("attributes")
            if not isinstance(requested, dict) or not isinstance(expected_attributes, dict):
                raise ValueError("球员属性编辑参数不完整")
            visible = {
                f"{group}:{name}": value
                for group, values in _visible_player_attributes(
                    reader, player, raw_attributes,
                ).items()
                for name, value in values.items()
            }
            for key, raw_value in requested.items():
                attribute_id = VISIBLE_ATTRIBUTE_IDS.get(str(key))
                if attribute_id is None or key not in visible:
                    raise ValueError(f"该球员不支持属性：{key}")
                if expected_attributes.get(key) != visible[key]:
                    raise ValueError(f"{key} 已发生变化，请重新打开详情")
                target = integer(raw_value, str(key), 1, 20)
                raw_target = target * 5 - int(layout.attribute_display_bias)
                if not 0 <= raw_target <= 100 or _display_attribute(
                    raw_target, layout.attribute_display_bias,
                ) != target:
                    raise ValueError(f"{key} 无法安全换算为游戏属性")
                updated_attribute_block[int(attribute_id) - 0x0F] = raw_target

        if "hidden_attributes" in changes:
            requested = changes.get("hidden_attributes")
            expected_hidden = expected.get("hidden_attributes")
            if not isinstance(requested, dict) or not isinstance(expected_hidden, dict):
                raise ValueError("隐藏属性编辑参数不完整")
            current_hidden = _player_hidden_attributes(
                reader, player, person, raw_attributes, raw_person_hidden,
            )
            person_names = PERSON_HIDDEN_ATTRIBUTE_NAMES
            block_fields = PLAYER_HIDDEN_ATTRIBUTE_IDS
            updated_person = bytearray(raw_person_hidden)
            for name, raw_value in requested.items():
                if name not in current_hidden or expected_hidden.get(name) != current_hidden[name]:
                    raise ValueError(f"{name} 已发生变化，请重新打开详情")
                target = integer(raw_value, name, 0 if name in person_names else 1, 20)
                if name in person_names:
                    if hidden_offset is None or len(updated_person) != len(person_names):
                        raise RuntimeError("当前游戏版本尚未映射人物隐藏属性")
                    updated_person[person_names.index(name)] = target
                elif name in block_fields:
                    raw_target = target * 5 - int(layout.attribute_display_bias)
                    if not 0 <= raw_target <= 100:
                        raise ValueError(f"{name} 无法安全换算为游戏属性")
                    updated_attribute_block[block_fields[name] - 0x0F] = raw_target
                else:
                    raise ValueError(f"未知隐藏属性：{name}")
            if hidden_offset is not None and updated_person != raw_person_hidden:
                stage(
                    person + int(hidden_offset), raw_person_hidden, bytes(updated_person),
                )

        if updated_attribute_block != raw_attributes:
            stage(
                player + layout.player_attributes_offset,
                raw_attributes, bytes(updated_attribute_block),
            )

        staged = list(memory_changes.values())
        if not staged:
            raise ValueError("没有需要保存的球员修改")
        _apply_verified_memory_changes(
            reader, process, staged,
            write_error="球员编辑写后回读失败",
            rollback_error="球员编辑失败且原值回滚校验异常",
        )
        return {
            "player_id": int(player_id),
            "changed_fields": sorted(changes),
            "write_count": len(staged),
        }


def update_world_player_names(
    player_id: int, changes: dict[str, Any], expected: dict[str, Any],
) -> dict[str, Any]:
    """Edit existing FM name payloads without replacing their owning pointers."""
    if not isinstance(changes, dict) or not isinstance(expected, dict):
        raise ValueError("球员姓名编辑参数不完整")
    descriptors = {
        "full_name": ("person_full_name_offset", False),
        "first_name": ("person_first_name_offset", True),
        "last_name": ("person_last_name_offset", True),
        "common_name": ("person_common_name_offset", True),
    }
    unknown = set(changes) - set(descriptors)
    if unknown:
        raise ValueError(f"包含尚未支持的姓名字段：{', '.join(sorted(unknown))}")
    expected_names = expected.get("names")
    if not isinstance(expected_names, dict):
        raise ValueError("球员姓名旧值签名缺失，请重新打开球员详情")

    def target_text(value: Any) -> str:
        if not isinstance(value, str) or not value or len(value) > 256:
            raise ValueError("球员姓名长度无效")
        if any(ord(char) < 32 for char in value):
            raise ValueError("球员姓名包含控制字符")
        return value

    with _writable_game_reader() as (reader, process, _module):
        _indexed, _player, person = _world_player_index_target(reader, int(player_id))
        live: dict[str, tuple[int, str, bytes]] = {}
        for name, (offset_name, nested) in descriptors.items():
            try:
                live[name] = _world_player_name_storage(
                    reader, person, int(getattr(reader.layout, offset_name)), nested,
                )
            except (RuntimeError, TypeError, ValueError):
                continue
        if not live:
            raise RuntimeError("当前游戏版本没有可验证的球员姓名字符串")
        pointers = {}
        for name, (pointer, _current, _raw) in live.items():
            pointers.setdefault(pointer, []).append(name)
        staged: list[dict[str, Any]] = []
        for name, value in changes.items():
            if name not in live:
                raise RuntimeError(f"当前游戏版本没有可验证的{name}姓名字段")
            pointer, current, original = live[name]
            if expected_names.get(name) != current:
                raise ValueError(f"{name} 已发生变化，请重新打开球员详情")
            if len(pointers.get(pointer, [])) > 1:
                raise ValueError("多个姓名字段共享同一原生字符串，拒绝覆盖")
            encoded = target_text(value).encode("utf-8")
            if len(encoded) != len(original):
                raise ValueError("姓名编辑目前要求保持原生 UTF-8 存储长度不变")
            if encoded != original:
                staged.append({
                    "address": int(pointer + 4),
                    "original": bytes(original),
                    "updated": bytes(encoded),
                })
        if not staged:
            raise ValueError("没有需要保存的姓名修改")
        _apply_verified_memory_changes(
            reader, process, staged,
            write_error="球员姓名写入后回读失败",
            rollback_error="球员姓名编辑失败且原值回滚校验异常",
        )
        reader.string_cache.clear()
        return {
            "player_id": int(player_id),
            "changed_fields": sorted(changes),
            "write_count": len(staged),
        }


def update_world_player_contract(
    player_id: int, changes: dict[str, Any], expected: dict[str, Any],
) -> dict[str, Any]:
    """Edit verified scalar fields on the existing primary player contract."""
    if not isinstance(changes, dict) or not isinstance(expected, dict):
        raise ValueError("合同编辑参数不完整")
    allowed = {
        "wage_per_week", "loyalty_bonus", "start_date", "expiry_date",
        "signed_date", "agreed_playing_time", "squad_number",
        "happiness", "playing_time_happiness",
        "transfer_status_raw", "contract_type", "bonuses_and_clauses",
    }
    unknown = set(changes) - allowed
    if unknown:
        raise ValueError(f"包含尚未支持的合同字段：{', '.join(sorted(unknown))}")
    expected_contract = expected.get("contract")
    if not isinstance(expected_contract, dict):
        raise ValueError("合同旧值签名缺失，请重新打开球员详情")

    def integer(value: Any, label: str, minimum: int, maximum: int) -> int:
        if isinstance(value, bool):
            raise ValueError(f"{label}必须是整数")
        try:
            parsed = int(value)
        except (TypeError, ValueError) as error:
            raise ValueError(f"{label}必须是整数") from error
        if str(value).strip() not in {str(parsed), f"{parsed}.0"} and not isinstance(value, int):
            raise ValueError(f"{label}必须是整数")
        if not minimum <= parsed <= maximum:
            raise ValueError(f"{label}必须在 {minimum} 至 {maximum} 之间")
        return parsed

    def encode_date(value: Any, original_raw: int, label: str) -> int:
        if value in (None, ""):
            return 0
        try:
            parsed = date.fromisoformat(str(value))
        except (TypeError, ValueError) as error:
            raise ValueError(f"{label}格式必须为 YYYY-MM-DD") from error
        if not 1900 <= parsed.year <= 2200:
            raise ValueError(f"{label}年份超出安全范围")
        # FM date words keep calendar flags in the high byte; preserve them.
        return ((parsed.year << 16) | parsed.timetuple().tm_yday) | (int(original_raw) & 0xFE00)

    with _writable_game_reader() as (reader, process, _module):
        _indexed, _player, person = _world_player_index_target(reader, int(player_id))
        contract_offset = (
            FM24_PERSON_CONTRACT if reader.layout.key.startswith("fm24") else PERSON_CONTRACT
        )
        contract = int(reader.ptr(person + contract_offset) or 0)
        contract_size = _world_player_contract_size(reader.layout)
        raw = reader.bytes(contract, contract_size) if contract else None
        if not contract or not raw or len(raw) != contract_size:
            raise ValueError("当前球员没有可编辑的主合同")
        if int(reader.ptr(contract + 0x08) or 0) != int(person):
            raise ValueError("主合同 Person 回指校验失败")
        expected_raw = expected.get("expected_raw")
        expected_contract_raw = (
            expected_raw.get("contract")
            if isinstance(expected_raw, dict) else expected_raw
        )
        if str(expected_contract_raw or "") and expected_contract_raw != raw.hex():
            raise ValueError("球员合同已发生变化，请重新打开球员详情")

        if reader.layout.key.startswith("fm24"):
            bonus_vector_offset = 0x60
            bonus_limit = 64
            specs = {
                "wage_per_week": (0x18, 4, "u32"),
                "loyalty_bonus": (0x30, 4, "u32"),
                "start_date": (0x3C, 4, "date"),
                "expiry_date": (0x40, 4, "date"),
                "signed_date": (0x44, 4, "date"),
                "agreed_playing_time": (0x4C, 1, "u8"),
                "transfer_status_raw": (0x4F, 1, "u8"),
                "squad_number": (0x55, 1, "i8"),
                "contract_type": (0xB3, 1, "u8"),
            }
        else:
            bonus_vector_offset = 0x68
            bonus_limit = 1000
            specs = {
                "wage_per_week": (0x20, 4, "u32"),
                "loyalty_bonus": (0x38, 4, "u32"),
                "start_date": (0x44, 4, "date"),
                "expiry_date": (0x48, 4, "date"),
                "signed_date": (0x4C, 4, "date"),
                "agreed_playing_time": (0x54, 1, "u8"),
                "transfer_status_raw": (0x57, 1, "u8"),
                "squad_number": (0x5D, 1, "i8"),
                "contract_type": (0xC3, 1, "u8"),
                # FMRTE 26 exposes these two signed contract state bytes.
                "happiness": (0x5A, 1, "i8"),
                "playing_time_happiness": (0x5C, 1, "i8"),
            }

        def current_value(name: str, offset: int, width: int, kind: str) -> Any:
            raw_value = bytes(raw[offset:offset + width])
            if kind == "date":
                decoded = decode_date(struct.unpack("<I", raw_value)[0])
                return decoded.isoformat() if decoded else None
            if kind == "i8":
                value = int(raw_value[0])
                return value - 256 if value > 127 else value
            if kind == "u8":
                return int(raw_value[0])
            return int.from_bytes(raw_value, "little")

        staged: list[dict[str, Any]] = []
        updated = bytearray(raw)
        for name, value in changes.items():
            if name == "bonuses_and_clauses":
                continue
            if name not in specs:
                raise ValueError(f"当前版本不支持合同字段：{name}")
            offset, width, kind = specs[name]
            current = current_value(name, offset, width, kind)
            if expected_contract.get(name) != current:
                raise ValueError(f"合同字段 {name} 已发生变化，请重新打开球员详情")
            if kind == "date":
                target_raw = encode_date(value, int.from_bytes(raw[offset:offset + width], "little"), name)
                target = target_raw.to_bytes(width, "little")
            elif kind == "i8":
                minimum, maximum = (-128, 127)
                if name == "squad_number":
                    minimum, maximum = -1, 99
                target_value = integer(value, name, minimum, maximum)
                target = bytes((target_value & 0xFF,))
            elif kind == "u8":
                target_value = integer(value, name, 0, 255)
                if name == "contract_type" and target_value != 1:
                    raise ValueError("目前只支持将合同类型切换为全职合同")
                target = bytes((target_value,))
            else:
                target = integer(value, name, 0, 0xFFFFFFFF).to_bytes(width, "little")
            original = bytes(raw[offset:offset + width])
            if original == target:
                continue
            updated[offset:offset + width] = target
            staged.append({
                "address": int(contract + offset),
                "original": original,
                "updated": target,
            })

        if "bonuses_and_clauses" in changes:
            requested_rows = changes.get("bonuses_and_clauses")
            expected_rows = expected_contract.get("bonuses_and_clauses")
            if not isinstance(requested_rows, list) or not isinstance(expected_rows, list):
                raise ValueError("合同奖金/条款编辑参数不完整")
            begin, end, capacity = struct.unpack_from("<QQQ", raw, bonus_vector_offset)
            if not begin and not end and not capacity:
                raise ValueError("当前合同没有可编辑的奖金或条款")
            if (
                not begin or begin > end or end > capacity or (end - begin) % 8
                or (end - begin) // 8 > bonus_limit
            ):
                raise RuntimeError("合同奖金/条款向量结构无效")
            if end == begin:
                raise ValueError("当前合同没有可编辑的奖金或条款")
            bonus_raw = reader.bytes(begin, end - begin)
            if bonus_raw is None or len(bonus_raw) != end - begin:
                raise RuntimeError("无法读取合同奖金/条款记录")
            expected_bonus_raw = (
                expected_raw.get("contract_bonuses")
                if isinstance(expected_raw, dict) else expected.get("expected_bonuses")
            )
            if expected_bonus_raw != bonus_raw.hex():
                raise ValueError("合同奖金/条款已发生变化，请重新打开球员详情")
            expected_by_index = {
                int(row.get("index")): row
                for row in expected_rows
                if isinstance(row, dict) and row.get("index") is not None
            }
            seen_indexes: set[int] = set()
            for requested in requested_rows:
                if not isinstance(requested, dict) or requested.get("index") is None:
                    raise ValueError("合同奖金/条款记录索引无效")
                index = integer(requested.get("index"), "合同奖金/条款索引", 0, (end - begin) // 8 - 1)
                if index in seen_indexes:
                    raise ValueError("合同奖金/条款记录重复")
                seen_indexes.add(index)
                expected_row = expected_by_index.get(index)
                if not expected_row:
                    raise ValueError("合同奖金/条款旧值缺失，请重新打开详情")
                offset = index * 8
                original = bytes(bonus_raw[offset:offset + 8])
                current_amount, current_number, current_type = struct.unpack("<ihh", original)
                expected_number = expected_row.get("number")
                normalized_number = None if current_number < 0 else current_number
                if (
                    int(
                        expected_row.get("type")
                        if expected_row.get("type") is not None else -1
                    ) != current_type
                    or expected_row.get("amount") != current_amount
                    or expected_number != normalized_number
                ):
                    raise ValueError("合同奖金/条款记录已发生变化，请重新打开详情")
                if current_type not in BONUS_AND_CLAUSE_NAMES or current_amount < 0:
                    raise ValueError("该合同奖金/条款记录不支持编辑")
                target_amount = integer(
                    requested.get("amount", current_amount), "合同奖金/条款金额", 0, 0x7FFFFFFF,
                )
                target_number = current_number
                if "number" in requested:
                    if current_number < 0:
                        raise ValueError("该合同条款没有可编辑的次数字段")
                    target_number = integer(
                        requested.get("number"), "合同奖金/条款次数", 0, 0x7FFF,
                    )
                target = struct.pack("<ihh", target_amount, target_number, current_type)
                if target == original:
                    continue
                staged.append({
                    "address": int(begin + offset),
                    "original": original,
                    "updated": target,
                })

        if not staged:
            raise ValueError("没有需要保存的合同修改")
        _apply_verified_memory_changes(
            reader, process, staged,
            write_error="合同写入后回读失败",
            rollback_error="合同编辑失败且原值回滚校验异常",
        )
        return {
            "player_id": int(player_id),
            "changed_fields": [f"contract.{name}" for name in changes],
            "write_count": len(staged),
        }


def apply_world_player_language_level(
    player_id: int, language_id: int, target_level: int,
) -> dict[str, Any]:
    """Re-resolve a world player before using the verified language-vector writer."""
    with borrow_game_reader() as reader:
        _indexed, address, _person = _world_player_index_target(reader, int(player_id))
    from tools.player_languages import apply_player_language_level

    return apply_player_language_level(
        int(player_id), hex(address), int(language_id), int(target_level),
    )


def update_world_player_preferred_move(
    player_id: int, bit: int, operation: str,
) -> dict[str, Any]:
    """Re-resolve a world player before using the verified habit-mask writer."""
    with borrow_game_reader() as reader:
        _indexed, address, _person = _world_player_index_target(reader, int(player_id))
    from tools.preferred_moves import update_player_preferred_move

    return update_player_preferred_move(
        int(player_id), hex(address), int(bit), str(operation),
    )


def apply_world_player_second_nationality(
    player_id: int, nation_id: int, nation_address: Any = 0,
    *, replace_existing: bool = False,
) -> dict[str, Any]:
    """Resolve both Person and Nation from the current indexed session."""
    with borrow_game_reader() as reader:
        _indexed, address, _person = _world_player_index_target(reader, int(player_id))
        nation_address = _nation_address_for_uid(
            reader, int(reader.module_base), int(nation_id), nation_address,
        )
    return apply_player_second_nationality(
        int(player_id), hex(address), int(nation_id), hex(nation_address),
        replace_existing=replace_existing,
    )


def remove_world_player_second_nationality(
    player_id: int, nation_id: int, nation_address: Any = 0,
) -> dict[str, Any]:
    """Resolve a world player and nation UID before removing the relation."""
    with borrow_game_reader() as reader:
        _indexed, address, _person = _world_player_index_target(reader, int(player_id))
        nation_address = _nation_address_for_uid(
            reader, int(reader.module_base), int(nation_id), nation_address,
        )
    return remove_player_second_nationality(
        int(player_id), hex(address), int(nation_id), hex(nation_address),
    )


def remove_world_player_injury(player_id: int) -> dict[str, Any]:
    """Re-resolve a world player before clearing its current injury container."""
    with borrow_game_reader() as reader:
        _indexed, address, _person = _world_player_index_target(reader, int(player_id))
    return remove_player_injury(int(player_id), hex(address))


def add_world_player_injury(player_id: int) -> dict[str, Any]:
    """Add one genuine native injury record to a UID-resolved world player."""
    with borrow_game_reader() as reader:
        _indexed, address, person = _world_player_index_target(reader, int(player_id))
        layout = reader.layout
        if layout.game_date_rva is None:
            raise RuntimeError("当前游戏版本没有可验证的游戏日期")
        game_date = decode_date(
            reader.u32(reader.module_base + int(layout.game_date_rva)) or 0,
        )
        if not game_date:
            raise RuntimeError("当前存档没有可验证的游戏日期")
        contract_offset = (
            FM24_PERSON_CONTRACT if str(layout.key).startswith("fm24")
            else PERSON_CONTRACT
        )
        contract = int(reader.ptr(person + contract_offset) or 0)
        team_address = int(reader.ptr(contract + 0x10) or 0) if contract else 0
        team = reader.team(team_address) if team_address else None
        team_id = int((team or {}).get("id") or 0)
        if not team_id or not team_address:
            raise ValueError("该球员当前没有可验证的合同球队，无法添加原生伤病")
    return create_random_world_injury(
        int(player_id), hex(address), game_date.isoformat(),
        team_id=team_id, team_address=hex(team_address),
    )


def update_world_player_retirement(
    player_id: int, action: str,
) -> dict[str, Any]:
    """Re-resolve a world player before updating its native retirement record."""
    with borrow_game_reader() as reader:
        _indexed, address, _person = _world_player_index_target(reader, int(player_id))
    return update_retirement_plan(int(player_id), hex(address), action=str(action))


def _world_player_table_rows(
    reader: Reader, rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Expand cached search hits with the factual fields shown in result tables."""
    layout = reader.layout
    game_date = (
        decode_date(reader.u32(reader.module_base + int(layout.game_date_rva)) or 0)
        if layout.game_date_rva is not None else None
    )
    for row in rows:
        address = _address(row.get("address"))
        if address:
            reader.prefetch(address, 0x500)

    expanded: list[dict[str, Any]] = []
    for source in rows:
        row = dict(source)
        address = _address(row.get("address"))
        try:
            person = _validated_player_person(reader, address, int(row.get("id") or 0))
        except (TypeError, ValueError):
            continue
        ca = (
            reader.u8(address + layout.player_ca_offset)
            if layout.player_ca_bytes == 1
            else reader.u16(address + layout.player_ca_offset)
        )
        pa = reader.u16(address + layout.player_pa_offset)
        ca = int(ca) if ca is not None and 1 <= int(ca) <= 200 else None
        pa = int(pa) if pa is not None and 1 <= int(pa) <= 200 else None
        positions_raw = reader.bytes(
            address + layout.player_positions_offset, len(POSITION_NAMES),
        ) or b""
        position_ratings = {
            label: int(value)
            for label, value in zip(POSITION_NAMES, positions_raw)
            if int(value) > 1
        }
        positions = [
            label for label, value in position_ratings.items() if value >= 15
        ]
        if not positions and position_ratings:
            highest = max(position_ratings.values())
            positions = [
                label for label, value in position_ratings.items() if value == highest
            ]
        nation_id, nationality, nationality_code = _person_nationality(reader, person)
        birth = _person_birth_date(reader, person)

        contract_offset = FM24_PERSON_CONTRACT if layout.key.startswith("fm24") else PERSON_CONTRACT
        contract = int(reader.ptr(person + contract_offset) or 0)
        team_address = 0
        if contract and int(reader.ptr(contract + 0x08) or 0) == person:
            team_address = int(reader.ptr(contract + 0x10) or 0)
        team = reader.team(team_address) if team_address else None
        if layout.key.startswith("fm24"):
            asking_price = read_fm24_asking_price(reader, address)
        elif layout.key.startswith("fm26"):
            asking_price = read_fm26_asking_price(reader, address)
        else:
            asking_price = None
        row.update({
            "age": _age_on(birth, game_date),
            "ca": ca,
            "pa": pa,
            "nationality_id": nation_id,
            "nationality": nationality,
            "nationality_code": nationality_code,
            "team_id": int(team.get("id") or 0) if team else None,
            "team_name": (
                str(team.get("short_name") or team.get("name") or "") or None
                if team else None
            ),
            "asking_price": asking_price,
            "positions": positions,
            "position_ratings": position_ratings,
            "summary_live": True,
        })
        expanded.append(row)
    return expanded



def _scan_staff_for_teams(
    reader: Reader, team_addresses: set[int], manager_id: int = 0,
) -> dict[int, list[dict[str, Any]]]:
    """Walk the staff pool once; expand profiles only for owned teams."""
    rows: dict[int, dict[int, dict[str, Any]]] = {
        address: {} for address in team_addresses
    }
    for person, primary_contract in _staff_scan_entries(reader, manager_id):
        contracts = (
            [primary_contract] if primary_contract else []
        ) if reader.layout.key == "fm24" else _person_contract_candidates(reader, person)
        matching_teams = {
            int(reader.ptr(contract + 0x10) or 0) for contract in contracts
        }.intersection(team_addresses)
        for team_address in matching_teams:
            row = _staff_candidate(reader, person, team_address, manager_id)
            if row:
                rows[team_address][row["id"]] = row
    return {
        address: sorted(people.values(), key=lambda row: row["name"].casefold())
        for address, people in rows.items()
    }


def _scan_staff(
    reader: Reader, team_address: int, manager_id: int, *,
    manager_person: int = 0,
) -> list[dict[str, Any]]:
    rows: dict[int, dict[str, Any]] = {}
    staff_entries = _staff_scan_entries(reader, manager_id)
    for person, primary_contract in staff_entries:
        # FM24 keeps the primary contract pointer inside the same dense staff
        # slab. Capture it while that slab is already in memory, then perform
        # only one lightweight team-pointer check for the global staff pool.
        # Full parsing is limited to the handful of staff at the selected club.
        if reader.layout.key == "fm24" and (
            not primary_contract
            or reader.ptr(primary_contract + 0x10) != int(team_address)
        ):
            continue
        row = _staff_candidate(
            reader, person, team_address, manager_id,
            manager_person=manager_person,
        )
        if row:
            rows[row["id"]] = row
    return sorted(rows.values(), key=lambda row: row["name"].casefold())


def _discover_squad_numbers(reader: Reader, players: list[dict[str, Any]]) -> dict[int, int]:
    """Discover FM's transient 16-byte squad-number rows without guessing a player offset."""
    known = {int(str(player["address"]), 16): int(player["id"]) for player in players}
    anchors = list(known)[:8]
    if not anchors:
        return {}
    candidates: dict[tuple[tuple[int, int], ...], dict[int, int]] = {}
    for region in iter_readable_regions(reader.process):
        # FM currently keeps squad-number rows in both database slabs and a
        # smaller registration slab. Validate the row structure below; the
        # sizes only limit an otherwise multi-gigabyte heap scan.
        if region.type != MEM_PRIVATE or region.size not in {0xFCF000, 0x451000}:
            continue
        data = reader.bytes(region.base_address, region.size)
        if not data:
            continue
        for anchor in anchors:
            needle = struct.pack("<Q", anchor); cursor = 0
            while True:
                hit = data.find(needle, cursor)
                if hit < 0:
                    break
                cursor = hit + 1
                rows: dict[int, int] = {}
                for delta in range(-80, 81):
                    offset = hit + delta * 16
                    if offset < 0 or offset + 16 > len(data):
                        continue
                    pointer, index, packed_number = struct.unpack_from("<QII", data, offset)
                    number = packed_number & 0xFF
                    if pointer in known and index < 256 and 1 <= number <= 99:
                        rows[known[pointer]] = number
                if len(rows) >= min(4, len(players)):
                    candidates[tuple(sorted(rows.items()))] = rows
    if not candidates:
        return {}
    votes: dict[int, dict[int, int]] = {}
    for rows in candidates.values():
        for identifier, number in rows.items():
            choices = votes.setdefault(identifier, {})
            choices[number] = choices.get(number, 0) + 1
    resolved = {}
    for identifier, choices in votes.items():
        ranked = sorted(choices.items(), key=lambda item: (-item[1], item[0]))
        if len(ranked) == 1 or ranked[0][1] > ranked[1][1]:
            resolved[identifier] = ranked[0][0]
    return resolved


def read_club_context(
    manager_id: int, team_id: int, *, team_address: Any = 0, manager_address: Any = 0,
) -> dict[str, Any]:
    with borrow_game_reader() as reader:
        layout = reader.layout
        manager, person, team = _context_addresses(
            reader, int(manager_id), int(team_id), team_address, manager_address,
        )
        _contract_team, contract = _contract(reader, person, team)
        team_row = reader.team(team) or {}
        if contract and team_row.get("team_type") == "national":
            contract["contract_type"] = "国家队合同"
        game_date = decode_date(
            reader.u32(reader.module_base + layout.game_date_rva) or 0
        ) if layout.game_date_rva is not None else None
        if contract:
            expiry = date.fromisoformat(contract["expiry_date"]) if contract.get("expiry_date") else None
            contract["active"] = bool(game_date and (not expiry or game_date <= expiry))
        return {
            "game_date": game_date.isoformat() if game_date else None,
            "team": {
                "id": int(team_id),
                "name": team_row.get("short_name") or team_row.get("name") or str(team_id),
                "reputation": team_row.get("reputation"),
                "address": hex(team),
                "club_address": hex(reader.ptr(team + 0x30) or 0),
            },
            "manager": {"id": int(manager_id), "address": hex(manager)},
            "contract": contract,
        }


def _validated_number(value: int | None, minimum: int, maximum: int) -> int | None:
    if value is None or not minimum <= int(value) <= maximum:
        return None
    return int(value)


def _stadium_name(reader: Reader, stadium_address: int) -> str | None:
    """Read stadium names that may carry FM26's trailing localization marker."""
    field = int(stadium_address) + int(reader.layout.stadium_name_offset)
    value = reader.fm_string_at(field)
    if value:
        return value
    pointer = int(reader.ptr(field) or 0)
    length = reader.u32(pointer) if pointer else None
    if length is None or not 0 < int(length) <= 256:
        return None
    raw = reader.bytes(pointer + 4, int(length))
    if not raw or len(raw) != int(length):
        return None
    raw = raw.rstrip(bytes(range(32)))
    try:
        value = raw.decode("utf-8") if raw else None
    except UnicodeDecodeError:
        return None
    return value if value and not any(ord(char) < 32 for char in value) else None


def _club_information(reader: Reader, team_address: int) -> dict[str, Any] | None:
    """Read the FMRTE-mapped club detail and stadium fields without writes."""
    layout = reader.layout
    required = (
        layout.club_detail_offset,
        layout.club_detail2_offset,
        layout.club_training_ground_offset,
    )
    if any(offset is None for offset in required):
        return None
    original_team_address = int(team_address or 0)
    team_id = int(reader.u32(original_team_address + ENTITY_UID) or 0)
    if team_id <= 0:
        return None
    try:
        resolved = resolve_team_club(reader, original_team_address, team_id)
        team_address = int(resolved.team_address)
        club = int(resolved.club_address)
    except (OSError, RuntimeError, TypeError, ValueError):
        # Keep the legacy address path for lightweight/older Readers that do
        # not expose a database index.  The existing Club vtable check remains
        # mandatory; Club UID is not compared to Team UID because reserve/B
        # teams legitimately use a distinct Club UID.
        team_address = original_team_address
        club = int(reader.ptr(team_address + 0x30) or 0)
        if not club:
            return None
        expected_vtable = reader.module_base + int(layout.club_vtable_rva)
        if int(reader.ptr(club) or 0) != expected_vtable:
            return None
    if not club:
        return None
    detail = reader.ptr(club + int(layout.club_detail_offset))
    detail2 = reader.ptr(club + int(layout.club_detail2_offset))

    def optional_u8(base: int, offset: int | None) -> int | None:
        return reader.u8(base + int(offset)) if base and offset is not None else None

    def optional_u16(base: int, offset: int | None) -> int | None:
        return reader.u16(base + int(offset)) if base and offset is not None else None

    def optional_u32(base: int, offset: int | None) -> int | None:
        return reader.u32(base + int(offset)) if base and offset is not None else None

    def optional_ptr(base: int, offset: int | None) -> int:
        return int(reader.ptr(base + int(offset)) or 0) if base and offset is not None else 0

    def optional_date(base: int, offset: int | None) -> str | None:
        value = optional_u32(base, offset)
        parsed = decode_date(value or 0)
        return parsed.isoformat() if parsed and parsed.year > 1900 else None

    def stadium_state(raw: int | None) -> tuple[int | None, str | None]:
        if raw is None:
            return None, None
        # FMRTE maps FM's zero-based storage bands to its public enum values.
        code = 1 if raw < 5 else 2 if raw < 11 else 6 if raw < 15 else 11 if raw < 20 else 16
        return code, STADIUM_STATE_NAMES.get(code)

    def optional_float(base: int, offset: int | None) -> float | None:
        raw = reader.bytes(base + int(offset), 4) if base and offset is not None else None
        if not raw or len(raw) != 4:
            return None
        value = float(struct.unpack("<f", raw)[0])
        return round(value, 6) if -180 <= value <= 180 else None

    def detail_u32(offset: int | None) -> int | None:
        return reader.u32(detail + int(offset)) if detail and offset is not None else None

    stadium = reader.ptr(team_address + TEAM_STADIUM)
    training_ground = reader.ptr(club + int(layout.club_training_ground_offset))
    team_row = reader.team(team_address) or {}

    def stadium_row(address: int, *, training: bool = False) -> dict[str, Any] | None:
        if not address:
            return None
        capacity = _validated_number(
            reader.u32(address + int(layout.stadium_capacity_offset))
            if layout.stadium_capacity_offset is not None else None, 0, 500_000,
        )
        seating = _validated_number(
            reader.u32(address + int(layout.stadium_seating_capacity_offset))
            if layout.stadium_seating_capacity_offset is not None else None, 0, 500_000,
        )
        used = _validated_number(
            reader.u32(address + int(layout.stadium_used_capacity_offset))
            if layout.stadium_used_capacity_offset is not None else None, 0, 500_000,
        )
        expansion = _validated_number(
            reader.u32(address + int(layout.stadium_expansion_capacity_offset))
            if layout.stadium_expansion_capacity_offset is not None else None, 0, 500_000,
        )
        capacities_valid = bool(
            capacity is not None and seating is not None and used is not None
            and expansion is not None and seating <= max(capacity, 1)
            and used <= max(capacity, 1) and expansion >= capacity
        )
        pitch_type = _validated_number(
            reader.u8(address + int(layout.stadium_pitch_type_offset))
            if layout.stadium_pitch_type_offset is not None else None, 0, 32,
        )
        location = optional_ptr(address, layout.stadium_location_offset)
        nearby = optional_ptr(address, layout.stadium_nearby_offset)
        owner = optional_ptr(address, layout.stadium_owner_offset)
        extras = optional_u8(address, layout.stadium_extras_offset)
        state_raw = optional_u8(address, layout.stadium_state_offset)
        state, state_name = stadium_state(state_raw)
        deterioration = optional_u8(address, layout.stadium_pitch_deterioration_offset)
        national_team_use = optional_u8(address, layout.stadium_national_team_use_offset)
        national_u21_use = optional_u8(address, layout.stadium_national_u21_use_offset)
        national_u19_use = optional_u8(address, layout.stadium_national_u19_use_offset)
        seat_color = (
            reader.bytes(address + int(layout.stadium_seat_color_offset), 4)
            if layout.stadium_seat_color_offset is not None else None
        )
        return {
            "name": _stadium_name(reader, address),
            "address": hex(address),
            "id": optional_u32(address, layout.stadium_id_offset),
            "database_id": optional_u32(address, layout.stadium_item_id_offset),
            "secondary_id": optional_u32(address, layout.stadium_id2_offset),
            "is_training_ground": training,
            "capacity": capacity if capacities_valid else None,
            "seating_capacity": seating if capacities_valid else None,
            "used_capacity": used if capacities_valid else None,
            "expansion_capacity": expansion if capacities_valid else None,
            "pitch_condition": _validated_number(
                reader.u8(address + int(layout.stadium_pitch_condition_offset))
                if layout.stadium_pitch_condition_offset is not None else None, 0, 200,
            ),
            "pitch_type": pitch_type,
            "pitch_type_name": PITCH_TYPE_NAMES.get(pitch_type),
            "all_seater_capacity": _validated_number(
                optional_u32(address, layout.stadium_all_seater_capacity_offset), 0, 500_000,
            ),
            "build_date": optional_date(address, layout.stadium_build_date_offset),
            "rebuild_date": optional_date(address, layout.stadium_rebuild_date_offset),
            "pitch_last_relaid": optional_date(address, layout.stadium_pitch_relaid_date_offset),
            "pitch_relay_required": optional_date(
                address, layout.stadium_pitch_relay_required_date_offset,
            ),
            "latitude": optional_float(address, layout.stadium_latitude_offset),
            "longitude": optional_float(address, layout.stadium_longitude_offset),
            "location": {
                "id": reader.u32(location + ENTITY_UID) if location else None,
                "name": reader.fm_string_at(location + 0x18) if location else None,
                "address": hex(location) if location else None,
            } if location else None,
            "nearby_stadium": {
                "name": _stadium_name(reader, nearby),
                "address": hex(nearby),
            } if nearby else None,
            "owner": {
                "name": team_row.get("name") if owner == club else None,
                "address": hex(owner),
                "is_current_club": owner == club,
            } if owner else None,
            "covered": bool(extras & 0x01) if extras is not None else None,
            "undersoil_heating": bool(extras & 0x02) if extras is not None else None,
            "retractable_roof": bool(extras & 0x40) if extras is not None else None,
            "extras_raw": extras,
            "state": state,
            "state_name": state_name,
            "state_raw": state_raw,
            "extinct": (
                value == 1
                if (value := optional_u8(address, layout.stadium_extinct_offset)) is not None
                else None
            ),
            "pitch_deterioration": deterioration,
            "pitch_deterioration_name": PITCH_DETERIORATION_NAMES.get(deterioration),
            "pitch_recovery": _validated_number(
                optional_u8(address, layout.stadium_pitch_recovery_offset), 0, 200,
            ),
            "national_team_use": national_team_use,
            "national_u21_use": national_u21_use,
            "national_u19_use": national_u19_use,
            "national_team_use_name": STADIUM_MATCH_NAMES.get(national_team_use),
            "national_u21_use_name": STADIUM_MATCH_NAMES.get(national_u21_use),
            "national_u19_use_name": STADIUM_MATCH_NAMES.get(national_u19_use),
            "seat_color_raw": seat_color.hex().upper() if seat_color and len(seat_color) == 4 else None,
        }

    attendance = {
        "average": _validated_number(
            detail_u32(layout.club_average_attendance_offset), 100, 500_000,
        ),
        "minimum": _validated_number(
            detail_u32(layout.club_minimum_attendance_offset), 0, 500_000,
        ),
        "maximum": _validated_number(
            detail_u32(layout.club_maximum_attendance_offset), 100, 500_000,
        ),
    }
    if not (
        attendance["average"] is not None
        and attendance["minimum"] is not None
        and attendance["maximum"] is not None
        and attendance["minimum"] <= attendance["average"] <= attendance["maximum"]
    ):
        attendance = {"average": None, "minimum": None, "maximum": None}
    facilities = {
        "training": _validated_number(
            reader.u8(detail2 + int(layout.club_training_facilities_offset))
            if detail2 and layout.club_training_facilities_offset is not None else None, 1, 20,
        ),
        "youth": _validated_number(
            reader.u8(detail2 + int(layout.club_youth_facilities_offset))
            if detail2 and layout.club_youth_facilities_offset is not None else None, 1, 20,
        ),
        "junior_coaching": _validated_number(
            reader.u8(detail2 + int(layout.club_junior_coaching_offset))
            if detail2 and layout.club_junior_coaching_offset is not None else None, 1, 20,
        ),
        "youth_recruitment": _validated_number(
            reader.u8(detail2 + int(layout.club_youth_recruitment_offset))
            if detail2 and layout.club_youth_recruitment_offset is not None else None, 1, 20,
        ),
    }
    if any(value is None for value in facilities.values()):
        facilities = {key: None for key in facilities}
    profile_raw = (
        reader.bytes(detail2 + int(layout.club_supporters_profile_offset), 6)
        if detail2 and layout.club_supporters_profile_offset is not None else None
    )
    distribution_raw = (
        reader.bytes(detail2 + int(layout.club_supporters_distribution_offset), 6)
        if detail2 and layout.club_supporters_distribution_offset is not None else None
    )
    profile_names = ("loyalty", "passion", "patience", "affluence", "temperament", "expectations")
    distribution_names = ("hardcore", "core", "family", "fair_weather", "corporate", "casual")
    supporters_profile = (
        dict(zip(profile_names, profile_raw, strict=True))
        if profile_raw and len(profile_raw) == 6 and all(value <= 20 for value in profile_raw)
        else None
    )
    supporters_distribution = (
        dict(zip(distribution_names, distribution_raw, strict=True))
        if distribution_raw and len(distribution_raw) == 6
        and all(value <= 100 for value in distribution_raw)
        and sum(distribution_raw) == 100
        else None
    )

    def read_club_culture() -> list[dict[str, Any]] | None:
        required_offsets = (
            layout.team_details_offset,
            layout.team_details_club_vision_offset,
            layout.club_vision_culture_offset,
            layout.club_culture_record_size,
        )
        if any(offset is None for offset in required_offsets):
            return None
        team_details = optional_ptr(team_address, layout.team_details_offset)
        club_vision = optional_ptr(team_details, layout.team_details_club_vision_offset)
        if not club_vision:
            return None
        root = optional_ptr(club_vision, layout.club_vision_culture_offset)
        if not root or reader.ptr(root + 0x30) != club:
            # FM24 acquired clubs with no native objectives keep an inline
            # Club Vision node: the vector header begins at +0x08 and the
            # owning Team is validated at +0x40.
            if reader.ptr(club_vision + 0x40) != team_address:
                return None
            root = club_vision + 0x08
        header = reader.bytes(root, 24)
        if not header or len(header) != 24:
            return None
        begin, end, capacity = struct.unpack("<QQQ", header)
        if end < begin or capacity < end or (end - begin) % 8 or end - begin > 8 * 256:
            return None
        pointers = reader.bytes(begin, end - begin) if end > begin else b""
        if pointers is None:
            return None
        rows: list[dict[str, Any]] = []
        for offset in range(0, len(pointers), 8):
            address = struct.unpack_from("<Q", pointers, offset)[0]
            raw = reader.bytes(address, int(layout.club_culture_record_size)) if address else None
            if not raw or len(raw) != int(layout.club_culture_record_size):
                return None
            item_id = struct.unpack_from("<h", raw, 0x08)[0]
            reference_type = struct.unpack_from("<b", raw, 0x0B)[0]
            value_raw = struct.unpack_from("<i", raw, 0x28)[0]
            culture_type = raw[0x2C]
            importance = raw[0x2D]
            source_type = raw[0x30]
            if not culture_type:
                return None
            # FM24 keeps inactive competition objectives in the same vector
            # with importance 0. They are valid native records, but are not
            # current club-culture targets and must not poison the complete
            # readable list. Other out-of-range values still fail closed.
            if importance == 0:
                continue
            if not 1 <= importance <= 10:
                return None
            rows.append({
                "address": hex(address),
                "type": culture_type,
                "type_name": CLUB_CULTURE_TYPE_NAMES.get(culture_type),
                "source_type": source_type,
                "source_name": CLUB_CULTURE_SOURCE_NAMES.get(source_type),
                "importance": importance,
                "value": None if value_raw == -1 else value_raw,
                "value_raw": value_raw,
                "start_date": optional_date(address, 0x20),
                "end_date": optional_date(address, 0x24),
                "reference_id": None if item_id == -1 else item_id,
                "reference_type": reference_type,
                "reference_type_name": CLUB_CULTURE_REFERENCE_NAMES.get(reference_type),
                # Culture references use the editor database-index namespace,
                # not FMODD's stable entity UID namespace.
                "reference_name": None,
                "reference_unknown_byte": raw[0x0A],
                "for_competition": reference_type == 25,
                "flags_raw": struct.unpack_from("<I", raw, 0x3C)[0],
            })
        return rows

    club_culture = read_club_culture()

    def read_debts() -> list[dict[str, Any]] | None:
        if not detail2 or layout.club_loans_offset is None or layout.club_debt_record_size is None:
            return None
        header = reader.bytes(detail2 + int(layout.club_loans_offset), 24)
        if not header or len(header) != 24:
            return None
        begin, end, capacity = struct.unpack("<QQQ", header)
        if end < begin or capacity < end or (end - begin) % 8 or end - begin > 8 * 128:
            return None
        pointers = reader.bytes(begin, end - begin) if end > begin else b""
        if pointers is None:
            return None
        rows: list[dict[str, Any]] = []
        for offset in range(0, len(pointers), 8):
            address = struct.unpack_from("<Q", pointers, offset)[0]
            raw = reader.bytes(address, int(layout.club_debt_record_size)) if address else None
            if not raw or len(raw) != int(layout.club_debt_record_size):
                return None
            source = raw[0x18]
            rows.append({
                "address": hex(address),
                "original_debt": struct.unpack_from("<I", raw, 0x00)[0],
                "monthly_repayment": struct.unpack_from("<I", raw, 0x04)[0],
                "monthly_interest_repayment": struct.unpack_from("<I", raw, 0x08)[0],
                "conditional_repayment": struct.unpack_from("<I", raw, 0x0C)[0],
                "end_date": optional_date(address, 0x10),
                "start_date": optional_date(address, 0x14),
                "source": source,
                "source_name": MONEY_LOANED_FROM_NAMES.get(source),
                "include_in_fpp": raw[0x19] == 1,
                "interest_only": raw[0x1A] == 1,
            })
        return rows

    debts = read_debts()

    def read_sponsors() -> list[dict[str, Any]] | None:
        if layout.club_sponsors_root_offset is None or layout.club_sponsor_record_size is None:
            return None
        root = optional_ptr(club, layout.club_sponsors_root_offset)
        header = reader.bytes(root, 24) if root else None
        if not header or len(header) != 24:
            return None
        begin, end, capacity = struct.unpack("<QQQ", header)
        if end < begin or capacity < end or (end - begin) % 8 or end - begin > 8 * 256:
            return None
        pointers = reader.bytes(begin, end - begin) if end > begin else b""
        if pointers is None:
            return None
        rows: list[dict[str, Any]] = []
        for offset in range(0, len(pointers), 8):
            address = struct.unpack_from("<Q", pointers, offset)[0]
            raw = reader.bytes(address, int(layout.club_sponsor_record_size)) if address else None
            if not raw or len(raw) != int(layout.club_sponsor_record_size):
                return None
            income_type = raw[0x12]
            # FM24 stores money as an 8-byte protected value. The displayed
            # amount is the low 32-bit word; the high word is validation data.
            total_value = struct.unpack_from("<I", raw, 0x08)[0]
            if not 0 <= total_value <= 100_000_000_000:
                return None
            rows.append({
                "address": hex(address),
                "type": income_type,
                "type_name": OTHER_INCOME_TYPE_NAMES.get(income_type),
                "total_value": total_value,
                "start_date": optional_date(address, 0x00),
                "end_date": optional_date(address, 0x04),
                "renew_income": raw[0x13] == 1,
                "fixed_value": raw[0x14] == 1,
            })
        return rows

    sponsors = read_sponsors()
    ownership_address = (
        detail2 + int(layout.club_ownership_offset)
        if detail2 and layout.club_ownership_offset is not None else 0
    )
    ownership = None
    if ownership_address:
        promises = reader.u8(ownership_address + 0x10)
        ownership = {
            "address": hex(ownership_address),
            "election_date": (
                parsed.isoformat()
                if (parsed := decode_date(reader.u32(ownership_address + 0x0C) or 0)) else None
            ),
            "type": reader.u8(ownership_address + 0x11),
            "max_term_years": reader.u8(ownership_address + 0x12),
            "max_terms": reader.u8(ownership_address + 0x13),
            "current_term": reader.u8(ownership_address + 0x14),
            "minimum_turnout": reader.u8(ownership_address + 0x15),
            "maximum_turnout": reader.u8(ownership_address + 0x16),
            "will_remain_fan_owned": reader.u8(ownership_address + 0x17) == 1,
            "promises_raw": promises,
            "promises": {
                name: bool(promises & mask) if promises is not None else None
                for name, mask in OWNERSHIP_PROMISES.items()
            },
        }
    finance_address = optional_ptr(club, layout.club_finance_offset)
    finances = None
    if finance_address and reader.ptr(finance_address + 0x08) == club:
        def finance_i32(offset: int | None) -> int | None:
            raw = reader.bytes(finance_address + int(offset), 4) if offset is not None else None
            return struct.unpack("<i", raw)[0] if raw and len(raw) == 4 else None

        def finance_float(offset: int | None) -> float | None:
            raw = reader.bytes(finance_address + int(offset), 4) if offset is not None else None
            return round(float(struct.unpack("<f", raw)[0]), 2) if raw and len(raw) == 4 else None

        balance = finance_i32(layout.finance_balance_offset)
        remaining_budget = finance_i32(layout.finance_remaining_transfer_budget_offset)

        def read_income_statement() -> dict[str, list[dict[str, Any]]] | None:
            offsets = layout.finance_income_statement_period_offsets
            if offsets is None or len(offsets) != 4:
                return None
            snapshots: dict[str, bytes] = {}
            for period_name, period_offset in zip(
                FINANCE_STATEMENT_PERIODS, offsets, strict=True,
            ):
                raw = reader.bytes(finance_address + int(period_offset), 0xE0)
                if not raw or len(raw) != 0xE0:
                    return None
                snapshots[period_name] = raw

            def rows(fields: tuple[tuple[str, str, int], ...], base_offset: int) -> list[dict[str, Any]]:
                return [
                    {
                        "key": key,
                        "name": name,
                        **{
                            period_name: struct.unpack_from(
                                "<I", snapshots[period_name], base_offset + field_offset,
                            )[0]
                            for period_name in FINANCE_STATEMENT_PERIODS
                        },
                    }
                    for key, name, field_offset in fields
                ]

            statement = {
                "income": rows(INCOME_STATEMENT_FIELDS, 0x9C),
                "expenditure": rows(EXPENDITURE_STATEMENT_FIELDS, 0x00),
            }
            validated = validated_finance_income_statement(statement)
            return validated or None

        def read_monthly_summary() -> list[dict[str, Any]] | None:
            offset = layout.finance_monthly_summary_offset
            if offset is None:
                return None
            header = reader.bytes(finance_address + int(offset), 8)
            if not header or len(header) != 8:
                return None
            count, reserved = struct.unpack("<II", header)
            if count > 679 or reserved != 0:
                return None
            pointers = reader.bytes(finance_address + int(offset) + 8, count * 8) if count else b""
            if pointers is None:
                return None
            game_date = (
                decode_date(reader.u32(reader.module_base + int(layout.game_date_rva)) or 0)
                if layout.game_date_rva is not None else None
            )
            rows: list[dict[str, Any]] = []
            for index in range(count):
                address = struct.unpack_from("<Q", pointers, index * 8)[0]
                raw = reader.bytes(address, 0x24) if address else None
                if not raw or len(raw) != 0x24:
                    return None
                month = None
                if game_date:
                    absolute_month = (
                        game_date.year * 12 + game_date.month - 1 - (count - index - 1)
                    )
                    year, month_index = divmod(absolute_month, 12)
                    month = f"{year:04d}-{month_index + 1:02d}"
                rows.append({
                    "month": month,
                    "balance": struct.unpack_from("<i", raw, 0x00)[0],
                    "profit": struct.unpack_from("<i", raw, 0x20)[0],
                })
            return rows

        sugar_daddy = _validated_number(
            optional_u8(
                finance_address,
                getattr(layout, "finance_sugar_daddy_offset", None),
            ),
            0,
            4,
        )
        finances = {
            "address": hex(finance_address),
            "balance": _validated_number(balance, -2_000_000_000, 2_000_000_000),
            "sugar_daddy": sugar_daddy,
            "sugar_daddy_name": SUGAR_DADDY_NAMES.get(sugar_daddy),
            "transfer_budget": _validated_number(remaining_budget, 0, 2_000_000_000),
            "remaining_transfer_budget": _validated_number(
                remaining_budget, 0, 2_000_000_000,
            ),
            "season_transfer_budget": _validated_number(
                finance_i32(layout.finance_season_transfer_budget_offset), 0, 2_000_000_000,
            ),
            "wage_budget": _validated_number(
                finance_i32(layout.finance_wage_budget_offset), 0, 2_000_000_000,
            ),
            "wage_used": _validated_number(
                finance_i32(layout.finance_wage_used_offset), 0, 2_000_000_000,
            ),
            "max_wage": _validated_number(
                finance_i32(layout.finance_max_wage_offset), 0, 2_000_000_000,
            ),
            "average_match_ticket_price": finance_float(
                layout.finance_average_ticket_price_offset
            ),
            "transfer_revenue_percentage": _validated_number(
                optional_u8(finance_address, layout.finance_transfer_revenue_percentage_offset),
                0, 100,
            ),
            "income_statement": read_income_statement(),
            "monthly_summary": read_monthly_summary(),
            "debts": debts,
            "sponsors": sponsors,
        }
    nation_address = optional_ptr(club, layout.club_nation_offset)
    foreground = (
        reader.bytes(detail + int(layout.club_foreground_color_offset), 4)
        if detail and layout.club_foreground_color_offset is not None else None
    )
    background = (
        reader.bytes(detail + int(layout.club_background_color_offset), 4)
        if detail and layout.club_background_color_offset is not None else None
    )
    club_status = optional_u8(club, layout.club_status_offset)
    return {
        "address": hex(club),
        "year_founded": _validated_number(
            reader.u16(detail2 + int(layout.club_year_founded_offset))
            if detail2 and layout.club_year_founded_offset is not None else None, 1800, 2200,
        ),
        "attendance": attendance,
        "supporters": {
            "season_ticket_holders": _validated_number(
                reader.u32(detail2 + int(layout.club_season_ticket_holders_offset))
                if detail2 and layout.club_season_ticket_holders_offset is not None else None,
                0, 2_000_000,
            ),
            "social_media_followers": _validated_number(
                reader.u32(detail2 + int(layout.club_social_media_followers_offset))
                if detail2 and layout.club_social_media_followers_offset is not None else None,
                0, 2_000_000_000,
            ),
            "profile": supporters_profile,
            "distribution": supporters_distribution,
        },
        "status": {
            "code": club_status,
            "name": CLUB_STATUS_NAMES.get(club_status),
            "morale": _validated_number(optional_u8(detail2, layout.club_morale_offset), 0, 200),
            "chairman_status": optional_u16(detail2, layout.club_chairman_status_offset),
            "allow_custom_logo": (
                value == 1
                if (value := optional_u8(club, layout.club_allow_custom_logo_offset)) is not None
                else None
            ),
        },
        "nation": {
            "id": reader.u32(nation_address + ENTITY_UID) if nation_address else None,
            "name": NATION_NAMES.get(reader.u32(nation_address + ENTITY_UID) or 0)
            if nation_address else None,
            "address": hex(nation_address) if nation_address else None,
        } if nation_address else None,
        "colors": {
            "foreground_raw": foreground.hex().upper()
            if foreground and len(foreground) == 4 else None,
            "background_raw": background.hex().upper()
            if background and len(background) == 4 else None,
        },
        "ownership": ownership,
        "culture": club_culture,
        "finances": finances,
        "facilities": facilities,
        "stadium": stadium_row(stadium),
        "training_ground": stadium_row(training_ground, training=True),
    }


def search_retirement_players(
    query: str = "", *, scope: str = "club", club_team_id: int = 0,
) -> dict[str, Any]:
    """Return players with a native retirement plan for the activity centre.

    The native retirement manager is the candidate source; only that bounded
    set is expanded into full player rows.  This avoids probing every person in
    the world database and also catches planned retirees outside the managed
    club roster.
    """
    resolved_scope = "world" if str(scope or "").strip().lower() == "world" else "club"
    normalized = str(query or "").strip().casefold()
    with borrow_game_reader() as reader:
        records = retirement_record_index(reader)
        if not records:
            return {"players": [], "scope": resolved_scope, "search": str(query or ""), "total": 0}
        directory = database_index_for_reader(reader)
        if directory is None:
            raise RuntimeError("当前版本尚未建立可搜索的 Person 数据库目录")
        targets = directory.player_targets()
        if not targets:
            return {"players": [], "scope": resolved_scope, "search": str(query or ""), "total": 0}
        # Person + 8 is FM's native key used by the retirement manager.  Read
        # these headers in one snapshot instead of issuing one process read per
        # world player.
        person_addresses = [int(target[0]) for target in targets.values()]
        snapshots = reader._fixed_size_snapshots(
            [address for address in person_addresses if address], 0x10,
        )
        wanted_keys = {int(key) for key in records if int(key) > 0}
        candidates = []
        for uid, target in targets.items():
            person_address = int(target[0])
            raw = snapshots.get(person_address)
            if not raw or len(raw) < 0x0C:
                continue
            internal_key = int.from_bytes(raw[8:12], "little")
            if internal_key not in wanted_keys:
                continue
            row = directory.player_for_uid(int(uid))
            if row:
                candidates.append(dict(row))
        expanded = _world_player_table_rows(reader, candidates)
        attach_retirement_details(reader, expanded)
        output_rows = []
        for player in expanded:
            retirement = player.get("retirement")
            if not isinstance(retirement, dict) or not retirement.get("has_plan"):
                continue
            if resolved_scope == "club" and int(player.get("team_id") or 0) != int(club_team_id or 0):
                continue
            if normalized:
                haystack = " ".join(
                    str(player.get(key) or "")
                    for key in ("name", "team_name", "id")
                ).casefold()
                if normalized not in haystack:
                    continue
            output_rows.append(player)
        output_rows.sort(
            key=lambda row: (-int(row.get("age") or 0), str(row.get("name") or "").casefold(), int(row.get("id") or 0)),
        )
        return {
            "players": output_rows,
            "scope": resolved_scope,
            "search": str(query or ""),
            "total": len(output_rows),
        }


def _limited_liability_ownership_block(raw: bytes) -> bytes:
    if len(raw) != 12:
        raise ValueError("俱乐部所有权字段长度无效")
    updated = bytearray(raw)
    updated[0:4] = b"\0\0\0\0"
    updated[5] = 1
    updated[6:12] = b"\0" * 6
    return bytes(updated)


def _write_person_display_name(
    process: Any, name_fields: tuple[int, int, int, int], encoded_name: bytes,
) -> tuple[int, int]:
    """Write a direct full name plus the nested common-name reference FM uses."""
    payload = struct.pack("<I", len(encoded_name)) + encoded_name + b"\0"
    common_entry_offset = (len(payload) + 7) & ~7
    allocated, free_address = _remote_malloc_block(process, common_entry_offset + 8)
    try:
        write_process_memory(
            process, allocated,
            payload + b"\0" * (common_entry_offset - len(payload))
            + struct.pack("<Q", allocated),
        )
        for address in name_fields[:2]:
            write_process_memory(process, address, struct.pack("<Q", 0))
        write_process_memory(
            process, name_fields[2], struct.pack("<Q", allocated + common_entry_offset),
        )
        write_process_memory(process, name_fields[3], struct.pack("<Q", allocated))
    except Exception:
        _remote_free_block(process, free_address, allocated)
        raise
    return allocated, free_address


def _validated_person_name_references(
    reader: Reader, person: int,
) -> tuple[tuple[int, int, int], tuple[int, int, int]]:
    """Return validated FirstName/LastName/CommonName reference fields and values."""
    fields = (
        person + int(reader.layout.person_first_name_offset),
        person + int(reader.layout.person_last_name_offset),
        person + int(reader.layout.person_common_name_offset),
    )
    references = tuple(int(reader.ptr(field) or 0) for field in fields)
    readable = []
    for field, reference in zip(fields, references, strict=True):
        value = reader.fm_nested_string_at(field) if reference else ""
        if reference and not value:
            raise RuntimeError("玩家主教练姓名引用校验失败")
        readable.append(str(value or "").strip())
    if not any(readable):
        raise RuntimeError("玩家主教练姓名为空，无法复制姓名引用")
    return fields, references


def _write_person_name_references(
    process: Any, name_fields: tuple[int, int, int],
    references: tuple[int, int, int],
) -> None:
    """Copy FMRTE-style component references without replacing FullName."""
    for field, reference in zip(name_fields, references, strict=True):
        write_process_memory(process, field, struct.pack("<Q", reference))


def _acquire_fm26_controller_attributes(
    pid: int, layout: Any, team_id: int, team_address: Any, manager_name: str,
) -> dict[str, Any]:
    """Keep only the proven scalar controller effects for FM26 acquisitions."""
    controller_states: list[dict[str, Any]] = []
    modified_fields: set[str] = set()
    with open_process(pid, write_memory=True) as process:
        module = layout.module(process)
        if not module:
            raise RuntimeError(f"{layout.module_name} 尚未加载")
        reader = Reader(process, module.base_address, layout)
        team = _resolve_team_address(reader, int(team_id), team_address)
        club = int(reader.ptr(team + TEAM_CLUB) or 0)
        if (
            not club
            or reader.ptr(club) != module.base_address + int(layout.club_vtable_rva)
            or int(reader.u32(club + ENTITY_UID) or 0) != int(team_id)
        ):
            raise RuntimeError("俱乐部对象校验失败")
        controllers = _scan_club_controllers(reader, team, int(manager_id))
        for controller in controllers:
            person = _address(controller.get("address"))
            identifier = int(controller.get("id") or 0)
            if (
                not person or identifier <= 0
                or int(reader.u32(person + ENTITY_UID) or 0) != identifier
            ):
                raise RuntimeError("俱乐部控制人人物对象校验失败")
            complete = person - int(layout.staff_complete_object_offset)
            controller_base = complete + int(layout.chairman_base_adjustment)
            patience_address = controller_base + int(layout.chairman_patience_offset)
            interference_address = controller_base + int(layout.chairman_interference_offset)
            patience_before = reader.u8(patience_address)
            interference_before = reader.u8(interference_address)
            if (
                patience_before is None or not 0 <= int(patience_before) <= 20
                or interference_before is None
                or not 0 <= int(interference_before) <= 20
            ):
                raise RuntimeError("俱乐部控制人属性原值校验失败")
            state_fields = sorted({
                *({"patience"} if int(patience_before) != 20 else set()),
                *({"interference"} if int(interference_before) != 0 else set()),
            })
            controller_states.append({
                "id": identifier,
                "job_type": int(controller.get("job_type") or 0),
                "role": str(controller.get("role") or "主席"),
                "name_before": _name(reader, person),
                "patience_address": patience_address,
                "patience_before": int(patience_before),
                "interference_address": interference_address,
                "interference_before": int(interference_before),
                "modified_fields": state_fields,
            })
        if any("patience" in state["modified_fields"] for state in controller_states):
            modified_fields.add("chairman_patience")
        if any("interference" in state["modified_fields"] for state in controller_states):
            modified_fields.add("chairman_interference")
        try:
            for state in controller_states:
                if "patience" in state["modified_fields"]:
                    write_process_memory(
                        process, int(state["patience_address"]), bytes([20]),
                    )
                if "interference" in state["modified_fields"]:
                    write_process_memory(
                        process, int(state["interference_address"]), bytes([0]),
                    )
            if any(
                (
                    "patience" in state["modified_fields"]
                    and reader.u8(int(state["patience_address"])) != 20
                )
                or (
                    "interference" in state["modified_fields"]
                    and reader.u8(int(state["interference_address"])) != 0
                )
                for state in controller_states
            ):
                raise RuntimeError("俱乐部控制人属性写入后校验失败")
        except Exception:
            for state in reversed(controller_states):
                if "patience" in state["modified_fields"]:
                    write_process_memory(
                        process, int(state["patience_address"]),
                        bytes([int(state["patience_before"])]),
                    )
                if "interference" in state["modified_fields"]:
                    write_process_memory(
                        process, int(state["interference_address"]),
                        bytes([int(state["interference_before"])]),
                    )
            raise
    chairman = _club_controller_staff(controllers)
    snapshot = {
        "pid": int(pid), "team_id": int(team_id),
        "modified_fields": sorted(modified_fields),
        "controllers": controller_states,
    }
    return {
        "team_id": int(team_id),
        "chairman_id": int(chairman.get("id") or 0) if chairman else 0,
        "chairman_available": bool(chairman),
        "chairman_name": None,
        "controller_job_type": int(chairman.get("job_type") or 0) if chairman else None,
        "controller_role": str(chairman.get("role") or "主席") if chairman else None,
        "controller_count": len(controller_states),
        "controllers": [
            {
                "id": int(state["id"]),
                "job_type": int(state["job_type"]),
                "role": str(state["role"]),
                "name": state["name_before"],
                "patience": 20,
                "interference": 0,
            }
            for state in controller_states
        ],
        "requested_owner_name": manager_name,
        "native_effects_verified": bool(controller_states),
        "native_effects_applied": bool(modified_fields),
        "applied_fields": sorted(modified_fields),
        "acquisition_mode": "controller_attributes_only",
        "sale_restore": {
            "schema_version": 1,
            "game_key": "fm26",
            "modified_fields": sorted(modified_fields),
            "chairman_id": int(chairman.get("id") or 0) if chairman else 0,
            "controllers": [
                {
                    "id": int(state["id"]),
                    "job_type": int(state["job_type"]),
                    "role": str(state["role"]),
                    "modified_fields": list(state["modified_fields"]),
                    **(
                        {"patience": int(state["patience_before"])}
                        if "patience" in state["modified_fields"] else {}
                    ),
                    **(
                        {"interference": int(state["interference_before"])}
                        if "interference" in state["modified_fields"] else {}
                    ),
                }
                for state in controller_states if state["modified_fields"]
            ],
        },
        "warning": (
            "FM26 收购仅保留控制人耐心 20、干涉度 0；不会改写姓名、"
            "主席状态、俱乐部财务或所有制内存"
            if controller_states else
            "FM26 收购未找到可安全校验的主席、所有者或总裁对象，未写入游戏内存"
        ),
        "_rollback": snapshot if modified_fields else {},
    }


def acquire_native_club(
    team_id: int, team_address: Any, manager_name: str, *,
    manager_id: int = 0, manager_address: Any = 0,
) -> dict[str, Any]:
    """Apply the verified acquisition fields as one rollback-capable transaction."""
    manager_name = str(manager_name or "").strip()
    encoded_name = manager_name.encode("utf-8")
    if not manager_name or len(encoded_name) > 256 or any(ord(char) < 32 for char in manager_name):
        raise ValueError("玩家经理名称无效")
    pid, _path, layout = select_process_layout()
    # Changing the inline ownership/election block can crash both FM24 and
    # FM26 even after an immediate readback succeeds. Keep it out of every
    # generation's write set while retaining the independently validated
    # controller, chairman-status and finance effects below.
    ownership_write_enabled = False
    required = (
        layout.club_detail2_offset, layout.club_chairman_status_offset,
        layout.chairman_patience_offset, layout.chairman_interference_offset,
        layout.club_finance_offset, layout.finance_balance_offset,
        layout.person_nationality_offset,
    ) + (
        (layout.club_ownership_offset,) if ownership_write_enabled else ()
    )
    if any(offset is None for offset in required):
        raise RuntimeError("当前游戏版本尚未完整映射俱乐部收购字段")
    allocated = 0
    free_address = 0
    snapshot: dict[str, Any] = {}
    with open_process(pid, write_memory=True) as process:
        module = layout.module(process)
        if not module:
            raise RuntimeError(f"{layout.module_name} 尚未加载")
        reader = Reader(process, module.base_address, layout)
        team = _resolve_team_address(reader, int(team_id), team_address)
        club = int(reader.ptr(team + TEAM_CLUB) or 0)
        detail2 = int(reader.ptr(club + int(layout.club_detail2_offset)) or 0)
        finance = int(reader.ptr(club + int(layout.club_finance_offset)) or 0)
        if not club or not detail2 or not finance or reader.ptr(finance + 0x08) != club:
            raise RuntimeError("俱乐部详情或财务对象校验失败")
        controllers = _scan_club_controllers(reader, team, 0)
        chairman = _club_controller_staff(controllers)
        manager_name_references: tuple[int, int, int] = ()
        if controllers:
            manager_person = _validated_manager_person(
                reader, int(manager_id), manager_address,
            )
            _manager_name_fields, manager_name_references = (
                _validated_person_name_references(reader, manager_person)
            )
        controller_states: list[dict[str, Any]] = []
        for controller in controllers:
            person = _address(controller.get("address"))
            identifier = int(controller.get("id") or 0)
            if (
                not person
                or identifier <= 0
                or int(reader.u32(person + ENTITY_UID) or 0) != identifier
            ):
                raise RuntimeError("俱乐部控制人人物对象校验失败")
            complete = person - int(layout.staff_complete_object_offset)
            chairman_base = complete + int(layout.chairman_base_adjustment)
            name_fields = (
                person + int(layout.person_first_name_offset),
                person + int(layout.person_last_name_offset),
                person + int(layout.person_common_name_offset),
                person + int(layout.person_full_name_offset),
            )
            state = {
                "id": identifier,
                "job_type": int(controller.get("job_type") or 0),
                "role": str(controller.get("role") or "主席"),
                "person": person,
                "name_before": _name(reader, person),
                "name_fields": name_fields,
                "name_pointers": [
                    int(reader.ptr(address) or 0) for address in name_fields
                ],
                "patience_address": (
                    chairman_base + int(layout.chairman_patience_offset)
                ),
                "interference_address": (
                    chairman_base + int(layout.chairman_interference_offset)
                ),
                "allocated": 0,
                "free_address": 0,
            }
            state["patience_before"] = reader.u8(state["patience_address"])
            state["interference_before"] = reader.u8(
                state["interference_address"]
            )
            if (
                state["patience_before"] is None
                or not 0 <= int(state["patience_before"]) <= 20
                or state["interference_before"] is None
                or not 0 <= int(state["interference_before"]) <= 20
            ):
                raise RuntimeError("俱乐部控制人属性原值校验失败")
            state["modified_fields"] = sorted({
                *(
                    {"name"}
                    if tuple(state["name_pointers"][:3])
                    != manager_name_references else set()
                ),
                *(
                    {"patience"}
                    if state["patience_before"] != 20 else set()
                ),
                *(
                    {"interference"}
                    if state["interference_before"] != 0 else set()
                ),
            })
            controller_states.append(state)
        chairman_person = _address(chairman.get("address")) if chairman else 0
        chairman_nationality_address = (
            chairman_person + int(layout.person_nationality_offset)
            if chairman_person else 0
        )
        chairman_nationality_before = (
            int(reader.ptr(chairman_nationality_address) or 0)
            if chairman_nationality_address else 0
        )
        chairman_nationality_before_id = (
            int(reader.u32(chairman_nationality_before + ENTITY_UID) or 0)
            if chairman_nationality_before else 0
        )
        manager_nation = manager_nation_id = 0
        chairman_birth_address = 0
        chairman_birth_before = manager_birth_raw = b""
        chairman_birth_before_value = manager_birth_value = None
        if chairman:
            _validated_nationality_target(
                reader, module.base_address,
                chairman_nationality_before_id, chairman_nationality_before,
            )
            manager_nation, manager_nation_id = _manager_primary_nation(
                reader, module.base_address, int(manager_id), manager_address,
            )
            birth_offset = getattr(layout, "person_date_of_birth_offset", None)
            if birth_offset is not None:
                manager_person = _validated_manager_person(
                    reader, int(manager_id), manager_address,
                )
                manager_birth_raw, manager_birth_value = (
                    _person_birth_date_snapshot(reader, manager_person)
                )
                chairman_birth_before, chairman_birth_before_value = (
                    _person_birth_date_snapshot(reader, chairman_person)
                )
                chairman_birth_address = chairman_person + int(birth_offset)
        status_address = detail2 + int(layout.club_chairman_status_offset)
        balance_address = finance + int(layout.finance_balance_offset)
        sugar_daddy_address = (
            finance + int(layout.finance_sugar_daddy_offset)
            if layout.finance_sugar_daddy_offset is not None else 0
        )
        ownership_address = (
            detail2 + int(layout.club_ownership_offset)
            if ownership_write_enabled and detail2
            and layout.club_ownership_offset is not None else 0
        )
        ownership_before = (
            reader.bytes(ownership_address + 0x0C, 12)
            if ownership_address else None
        )
        if ownership_write_enabled and (
            not ownership_before or len(ownership_before) != 12
        ):
            raise RuntimeError("俱乐部所有权字段读取失败")
        ownership_after = (
            _limited_liability_ownership_block(ownership_before)
            if ownership_before is not None else None
        )
        status_before = reader.u16(status_address)
        balance_raw = reader.bytes(balance_address, 4)
        balance_before = struct.unpack("<i", balance_raw)[0] if balance_raw and len(balance_raw) == 4 else None
        sugar_daddy_before = reader.u8(sugar_daddy_address) if sugar_daddy_address else None
        if (
            status_before is None or balance_before is None
            or (sugar_daddy_address and sugar_daddy_before not in range(5))
        ):
            raise RuntimeError("主席、俱乐部财务或资助者原值校验失败")
        controller_modified = {
            field
            for state in controller_states
            for field in state["modified_fields"]
        }
        modified_fields = {
            *({"chairman_name"} if "name" in controller_modified else set()),
            *({"chairman_status"} if status_before != 10000 else set()),
            *({"chairman_patience"} if "patience" in controller_modified else set()),
            *({"chairman_interference"} if "interference" in controller_modified else set()),
            *({"balance"} if balance_before < 0 else set()),
            *({"sugar_daddy"} if sugar_daddy_address and sugar_daddy_before != 3 else set()),
            *(
                {"chairman_nationality"}
                if chairman and chairman_nationality_before != manager_nation else set()
            ),
            *(
                {"chairman_birth_date"}
                if chairman_birth_address
                and chairman_birth_before != manager_birth_raw else set()
            ),
            *({"ownership"} if ownership_write_enabled and ownership_before != ownership_after else set()),
        }
        snapshot = {
            "pid": pid, "team_id": int(team_id),
            "modified_fields": sorted(modified_fields),
            "controllers": controller_states,
            "status_address": status_address, "status_before": status_before,
            "balance_address": balance_address, "balance_before": balance_before,
            "sugar_daddy_address": sugar_daddy_address,
            "sugar_daddy_before": sugar_daddy_before,
            "chairman_nationality_address": chairman_nationality_address,
            "chairman_nationality_before": chairman_nationality_before,
            "chairman_birth_date_address": chairman_birth_address,
            "chairman_birth_date_before": chairman_birth_before,
            "ownership_address": ownership_address,
            "ownership_before": ownership_before,
        }
        try:
            for state in controller_states:
                state_fields = set(state["modified_fields"])
                if "name" in state_fields:
                    _write_person_name_references(
                        process, state["name_fields"][:3],
                        manager_name_references,
                    )
                if "patience" in state_fields:
                    write_process_memory(
                        process, state["patience_address"], bytes([20]),
                    )
                if "interference" in state_fields:
                    write_process_memory(
                        process, state["interference_address"], bytes([0]),
                    )
            if "chairman_status" in modified_fields:
                write_process_memory(process, status_address, struct.pack("<H", 10000))
            if "balance" in modified_fields:
                write_process_memory(process, balance_address, struct.pack("<i", 0))
            if "sugar_daddy" in modified_fields:
                write_process_memory(process, sugar_daddy_address, bytes([3]))
            if "chairman_nationality" in modified_fields:
                write_process_memory(
                    process, chairman_nationality_address,
                    struct.pack("<Q", manager_nation),
                )
            if "chairman_birth_date" in modified_fields:
                write_process_memory(
                    process, chairman_birth_address, manager_birth_raw,
                )
            if "ownership" in modified_fields:
                write_process_memory(process, ownership_address + 0x0C, ownership_after)
            reader.string_cache.clear()
            if (
                any(
                    (
                        "name" in set(state["modified_fields"])
                        and tuple(
                            int(reader.ptr(field) or 0)
                            for field in state["name_fields"][:3]
                        ) != manager_name_references
                    )
                    or (
                        "patience" in set(state["modified_fields"])
                        and reader.u8(state["patience_address"]) != 20
                    )
                    or (
                        "interference" in set(state["modified_fields"])
                        and reader.u8(state["interference_address"]) != 0
                    )
                    for state in controller_states
                )
                or ("chairman_status" in modified_fields and reader.u16(status_address) != 10000)
                or ("balance" in modified_fields and reader.bytes(balance_address, 4) != struct.pack("<i", 0))
                or ("sugar_daddy" in modified_fields and reader.u8(sugar_daddy_address) != 3)
                or (
                    "chairman_nationality" in modified_fields
                    and reader.ptr(chairman_nationality_address) != manager_nation
                )
                or (
                    "chairman_birth_date" in modified_fields
                    and reader.bytes(chairman_birth_address, 4) != manager_birth_raw
                )
                or ("ownership" in modified_fields and reader.bytes(ownership_address + 0x0C, 12) != ownership_after)
            ):
                raise RuntimeError("俱乐部收购写入后校验失败")
        except Exception:
            for state in reversed(controller_states):
                state_fields = set(state["modified_fields"])
                if "name" in state_fields:
                    for address, pointer in zip(
                        state["name_fields"],
                        state["name_pointers"],
                        strict=True,
                    ):
                        write_process_memory(
                            process, address, struct.pack("<Q", pointer),
                        )
                if (
                    "patience" in state_fields
                    and state["patience_before"] is not None
                ):
                    write_process_memory(
                        process, state["patience_address"],
                        bytes([int(state["patience_before"])]),
                    )
                if (
                    "interference" in state_fields
                    and state["interference_before"] is not None
                ):
                    write_process_memory(
                        process, state["interference_address"],
                        bytes([int(state["interference_before"])]),
                    )
                if state.get("allocated"):
                    _remote_free_block(
                        process,
                        int(state.get("free_address") or 0),
                        int(state["allocated"]),
                    )
            if "chairman_status" in modified_fields:
                write_process_memory(process, status_address, struct.pack("<H", status_before))
            if "balance" in modified_fields:
                write_process_memory(process, balance_address, struct.pack("<i", balance_before))
            if "sugar_daddy" in modified_fields and sugar_daddy_before is not None:
                write_process_memory(process, sugar_daddy_address, bytes([sugar_daddy_before]))
            if "chairman_nationality" in modified_fields:
                write_process_memory(
                    process, chairman_nationality_address,
                    struct.pack("<Q", chairman_nationality_before),
                )
            if "chairman_birth_date" in modified_fields:
                write_process_memory(
                    process, chairman_birth_address, chairman_birth_before,
                )
            if "ownership" in modified_fields:
                write_process_memory(process, ownership_address + 0x0C, ownership_before)
            raise
    representative_state = next(
        (
            state for state in controller_states
            if chairman and state["id"] == int(chairman.get("id") or 0)
        ),
        None,
    )
    return {
        "team_id": int(team_id), "chairman_id": int(chairman.get("id") or 0) if chairman else 0,
        "chairman_available": bool(chairman),
        "chairman_name": manager_name if chairman else None,
        "controller_job_type": int(chairman.get("job_type") or 0) if chairman else None,
        "controller_role": str(chairman.get("role") or "主席") if chairman else None,
        "controller_count": len(controller_states),
        "controllers": [
            {
                "id": int(state["id"]),
                "job_type": int(state["job_type"]),
                "role": str(state["role"]),
                "name": manager_name,
                "patience": 20,
                "interference": 0,
            }
            for state in controller_states
        ],
        "requested_owner_name": manager_name, "chairman_status": 10000,
        "chairman_patience": 20 if chairman else None, "balance_before": balance_before,
        "chairman_interference_before": (
            representative_state.get("interference_before")
            if representative_state else None
        ),
        "chairman_interference_after": 0 if chairman else None,
        "balance_after": 0 if balance_before < 0 else balance_before,
        "sugar_daddy_before": sugar_daddy_before,
        "sugar_daddy_after": 3 if sugar_daddy_address else None,
        "sugar_daddy_updated": "sugar_daddy" in modified_fields,
        "chairman_nationality_before": (
            chairman_nationality_before_id if chairman else None
        ),
        "chairman_nationality_after": manager_nation_id if chairman else None,
        "chairman_nationality_updated": "chairman_nationality" in modified_fields,
        "chairman_birth_date_before": (
            chairman_birth_before_value.isoformat()
            if chairman_birth_before_value else None
        ),
        "chairman_birth_date_after": (
            manager_birth_value.isoformat() if manager_birth_value else None
        ),
        "chairman_birth_date_updated": "chairman_birth_date" in modified_fields,
        "ownership_type": 1 if ownership_write_enabled else None,
        "ownership_updated": "ownership" in modified_fields,
        "native_effects_verified": True,
        "native_effects_applied": bool(modified_fields),
        "applied_fields": sorted(modified_fields),
        "sale_restore": {
            "schema_version": 1,
            "game_key": str(getattr(layout, "key", "")),
            "modified_fields": sorted(modified_fields - {"balance"}),
            "chairman_id": (
                int(chairman.get("id") or 0)
                if chairman and modified_fields & {
                    "chairman_name", "chairman_patience", "chairman_interference",
                    "chairman_nationality", "chairman_birth_date",
                }
                else 0
            ),
            **({
                "controllers": [
                    {
                        "id": int(state["id"]),
                        "job_type": int(state["job_type"]),
                        "role": str(state["role"]),
                        "modified_fields": list(state["modified_fields"]),
                        **(
                            {"name": state["name_before"]}
                            if "name" in state["modified_fields"] else {}
                        ),
                        **(
                            {"patience": state["patience_before"]}
                            if "patience" in state["modified_fields"] else {}
                        ),
                        **(
                            {"interference": state["interference_before"]}
                            if "interference" in state["modified_fields"] else {}
                        ),
                    }
                    for state in controller_states
                    if state["modified_fields"]
                ],
            } if controller_modified else {}),
            **({
                "chairman_name": representative_state["name_before"],
            } if "chairman_name" in modified_fields and representative_state else {}),
            **({"chairman_status": int(status_before)} if "chairman_status" in modified_fields else {}),
            **({
                "chairman_patience": representative_state["patience_before"],
            } if "chairman_patience" in modified_fields and representative_state else {}),
            **({
                "chairman_interference": representative_state["interference_before"],
            } if "chairman_interference" in modified_fields and representative_state else {}),
            **({"sugar_daddy": sugar_daddy_before} if "sugar_daddy" in modified_fields else {}),
            **(
                {"chairman_nationality_id": chairman_nationality_before_id}
                if "chairman_nationality" in modified_fields else {}
            ),
            **(
                {
                    "chairman_birth_date_raw": chairman_birth_before.hex(),
                    "chairman_birth_date": chairman_birth_before_value.isoformat(),
                }
                if "chairman_birth_date" in modified_fields
                and chairman_birth_before_value else {}
            ),
            **({"ownership": ownership_before.hex()} if "ownership" in modified_fields else {}),
        },
        "warning": "；".join([
            *(
                [] if chairman else [
                    "该俱乐部当前没有可写入的主席、所有者或总裁对象，"
                    "姓名、国籍与耐心未修改"
                ]
            ),
        ]) or None,
        "_rollback": snapshot,
    }


def rollback_native_club_acquisition(snapshot: dict[str, Any]) -> None:
    if not snapshot:
        return
    pid, _path, layout = select_process_layout()
    if int(snapshot.get("pid") or 0) != pid:
        raise RuntimeError("FM 进程已变化，无法回滚俱乐部收购")
    modified_fields = (
        set(snapshot.get("modified_fields") or [])
        if "modified_fields" in snapshot
        else {
            "chairman_name", "chairman_status", "chairman_patience",
            "chairman_interference", "chairman_nationality", "balance",
            "chairman_birth_date", "sugar_daddy", "ownership",
        }
    )
    modified_fields.discard("ownership")
    if not modified_fields:
        return
    with open_process(pid, write_memory=True) as process:
        controller_snapshots = list(snapshot.get("controllers") or [])
        if controller_snapshots:
            for state in reversed(controller_snapshots):
                state_fields = set(state.get("modified_fields") or [])
                if "name" in state_fields:
                    for address, pointer in zip(
                        state.get("name_fields") or (),
                        state.get("name_pointers") or (),
                        strict=True,
                    ):
                        write_process_memory(
                            process, int(address),
                            struct.pack("<Q", int(pointer)),
                        )
                if (
                    "patience" in state_fields
                    and state.get("patience_address")
                    and state.get("patience_before") is not None
                ):
                    write_process_memory(
                        process, int(state["patience_address"]),
                        bytes([int(state["patience_before"])]),
                    )
                if (
                    "interference" in state_fields
                    and state.get("interference_address")
                    and state.get("interference_before") is not None
                ):
                    write_process_memory(
                        process, int(state["interference_address"]),
                        bytes([int(state["interference_before"])]),
                    )
                if "name" in state_fields:
                    _remote_free_block(
                        process,
                        int(state.get("free_address") or 0),
                        int(state.get("allocated") or 0),
                    )
        elif "chairman_name" in modified_fields:
            for address, pointer in zip(snapshot.get("name_fields") or (), snapshot.get("name_pointers") or (), strict=True):
                write_process_memory(process, int(address), struct.pack("<Q", int(pointer)))
        if "chairman_status" in modified_fields:
            write_process_memory(process, int(snapshot["status_address"]), struct.pack("<H", int(snapshot["status_before"])))
        if not controller_snapshots and "chairman_patience" in modified_fields and snapshot.get("patience_address") and snapshot.get("patience_before") is not None:
            write_process_memory(process, int(snapshot["patience_address"]), bytes([int(snapshot["patience_before"])]))
        if not controller_snapshots and "chairman_interference" in modified_fields and snapshot.get("interference_address") and snapshot.get("interference_before") is not None:
            write_process_memory(
                process, int(snapshot["interference_address"]),
                bytes([int(snapshot["interference_before"])]),
            )
        if "balance" in modified_fields:
            write_process_memory(process, int(snapshot["balance_address"]), struct.pack("<i", int(snapshot["balance_before"])))
        if "sugar_daddy" in modified_fields and snapshot.get("sugar_daddy_address") and snapshot.get("sugar_daddy_before") is not None:
            write_process_memory(
                process, int(snapshot["sugar_daddy_address"]),
                bytes([int(snapshot["sugar_daddy_before"])]),
            )
        if (
            "chairman_nationality" in modified_fields
            and snapshot.get("chairman_nationality_address")
            and snapshot.get("chairman_nationality_before")
        ):
            write_process_memory(
                process, int(snapshot["chairman_nationality_address"]),
                struct.pack("<Q", int(snapshot["chairman_nationality_before"])),
            )
        if (
            "chairman_birth_date" in modified_fields
            and snapshot.get("chairman_birth_date_address")
            and snapshot.get("chairman_birth_date_before")
        ):
            write_process_memory(
                process, int(snapshot["chairman_birth_date_address"]),
                bytes(snapshot["chairman_birth_date_before"]),
            )
        if "ownership" in modified_fields and snapshot.get("ownership_address") and snapshot.get("ownership_before"):
            write_process_memory(
                process, int(snapshot["ownership_address"]) + 0x0C,
                bytes(snapshot["ownership_before"]),
            )
        if not controller_snapshots and "chairman_name" in modified_fields:
            _remote_free_block(process, int(snapshot.get("free_address") or 0), int(snapshot.get("allocated") or 0))


def restore_native_club_after_sale(
    team_id: int, team_address: Any, sale_restore: dict[str, Any],
) -> dict[str, Any]:
    """Restore the acquisition-only club and chairman fields by current identity."""
    if not isinstance(sale_restore, dict) or int(sale_restore.get("schema_version") or 0) != 1:
        return {"restored": False, "legacy": True, "_rollback": {}}
    if "modified_fields" in sale_restore:
        modified_fields = {
            str(field) for field in sale_restore.get("modified_fields") or []
        }
    else:
        modified_fields = {
            field for field, key in (
                ("chairman_name", "chairman_name"),
                ("chairman_status", "chairman_status"),
                ("chairman_patience", "chairman_patience"),
                ("chairman_interference", "chairman_interference"),
                ("chairman_nationality", "chairman_nationality_id"),
                ("chairman_birth_date", "chairman_birth_date_raw"),
                ("sugar_daddy", "sugar_daddy"),
                ("ownership", "ownership"),
            )
            if key in sale_restore
        }
    if not modified_fields:
        return {
            "restored": False, "legacy": False, "skipped": True,
            "modified_fields": [], "_rollback": {},
        }
    pid, _path, layout = select_process_layout()
    modified_fields.discard("ownership")
    if not modified_fields:
        return {
            "restored": False, "legacy": False, "skipped": True,
            "modified_fields": [], "_rollback": {},
        }
    original_game_key = str(sale_restore.get("game_key") or "")
    if original_game_key and original_game_key != str(getattr(layout, "key", "")):
        raise RuntimeError("俱乐部收购版本与当前游戏版本不一致")
    required = (
        layout.club_detail2_offset, layout.club_chairman_status_offset,
        layout.chairman_patience_offset, layout.chairman_interference_offset,
        layout.club_finance_offset, layout.person_nationality_offset,
    )
    if any(offset is None for offset in required):
        raise RuntimeError("当前游戏版本尚未完整映射俱乐部出售恢复字段")

    allocated = 0
    free_address = 0
    rollback: dict[str, Any] = {}
    with open_process(pid, write_memory=True) as process:
        module = layout.module(process)
        if not module:
            raise RuntimeError(f"{layout.module_name} 尚未加载")
        reader = Reader(process, module.base_address, layout)
        team = _resolve_team_address(reader, int(team_id), team_address)
        club = int(reader.ptr(team + TEAM_CLUB) or 0)
        detail2 = int(reader.ptr(club + int(layout.club_detail2_offset)) or 0)
        finance = int(reader.ptr(club + int(layout.club_finance_offset)) or 0)
        if not club or not detail2 or not finance or reader.ptr(finance + 0x08) != club:
            raise RuntimeError("俱乐部详情或财务对象校验失败")

        controllers = _scan_club_controllers(reader, team, 0)
        chairman = _club_controller_staff(controllers)
        person = _address(chairman.get("address")) if chairman else 0
        expected_chairman_id = int(sale_restore.get("chairman_id") or 0)
        current_chairman_id = int(chairman.get("id") or 0) if chairman else 0
        if expected_chairman_id and current_chairman_id != expected_chairman_id:
            raise RuntimeError("俱乐部主席已变化，不能恢复原主席资料")
        if person and int(reader.u32(person + ENTITY_UID) or 0) != current_chairman_id:
            raise RuntimeError("主席人物对象校验失败")

        complete = person - int(layout.staff_complete_object_offset) if person else 0
        chairman_base = complete + int(layout.chairman_base_adjustment) if complete else 0
        patience_address = chairman_base + int(layout.chairman_patience_offset) if chairman_base else 0
        interference_address = chairman_base + int(layout.chairman_interference_offset) if chairman_base else 0
        status_address = detail2 + int(layout.club_chairman_status_offset)
        sugar_daddy_address = (
            finance + int(layout.finance_sugar_daddy_offset)
            if layout.finance_sugar_daddy_offset is not None else 0
        )
        ownership_address = (
            detail2 + int(layout.club_ownership_offset)
            if detail2 and layout.club_ownership_offset is not None else 0
        )

        name_fields = (
            (
                person + int(layout.person_first_name_offset),
                person + int(layout.person_last_name_offset),
                person + int(layout.person_common_name_offset),
                person + int(layout.person_full_name_offset),
            )
            if person else ()
        )
        target_name = (
            str(sale_restore.get("chairman_name") or "").strip()
            if "chairman_name" in modified_fields else ""
        )
        target_status = (
            int(sale_restore["chairman_status"])
            if "chairman_status" in modified_fields else None
        )
        target_patience = sale_restore.get("chairman_patience") if "chairman_patience" in modified_fields else None
        target_interference = sale_restore.get("chairman_interference") if "chairman_interference" in modified_fields else None
        target_nationality_id = (
            int(sale_restore.get("chairman_nationality_id") or 0)
            if "chairman_nationality" in modified_fields else 0
        )
        if "chairman_nationality" in modified_fields and target_nationality_id <= 0:
            raise RuntimeError("原主席国籍快照无效")
        target_nationality = (
            _nation_address_for_uid(
                reader, module.base_address, target_nationality_id,
            )
            if target_nationality_id else 0
        )
        nationality_address = (
            person + int(layout.person_nationality_offset) if person else 0
        )
        if "chairman_nationality" in modified_fields and not nationality_address:
            raise RuntimeError("无法定位主席国籍字段")
        if "chairman_nationality" in modified_fields:
            current_nationality = int(reader.ptr(nationality_address) or 0)
            current_nationality_id = (
                int(reader.u32(current_nationality + ENTITY_UID) or 0)
                if current_nationality else 0
            )
            _validated_nationality_target(
                reader, module.base_address,
                current_nationality_id, current_nationality,
            )
        birth_offset = getattr(layout, "person_date_of_birth_offset", None)
        birth_address = (
            person + int(birth_offset)
            if person and birth_offset is not None else 0
        )
        target_birth_raw = b""
        if "chairman_birth_date" in modified_fields:
            birth_hex = str(sale_restore.get("chairman_birth_date_raw") or "")
            try:
                target_birth_raw = bytes.fromhex(birth_hex)
            except ValueError as error:
                raise RuntimeError("原主席出生日期快照无效") from error
            if not birth_address or len(target_birth_raw) != 4:
                raise RuntimeError("原主席出生日期快照无效")
        target_sugar_daddy = sale_restore.get("sugar_daddy") if "sugar_daddy" in modified_fields else None
        ownership_hex = str(sale_restore.get("ownership") or "")
        target_ownership = (
            bytes.fromhex(ownership_hex)
            if "ownership" in modified_fields and ownership_hex else None
        )
        if target_ownership is not None and len(target_ownership) != 12:
            raise RuntimeError("原俱乐部所有权快照无效")
        controller_restore_rows = [
            dict(row) for row in sale_restore.get("controllers") or []
            if isinstance(row, dict) and int(row.get("id") or 0) > 0
        ]
        controller_restore_states: list[dict[str, Any]] = []
        if controller_restore_rows:
            current_by_id = {
                int(row.get("id") or 0): row for row in controllers
            }
            for saved in controller_restore_rows:
                identifier = int(saved.get("id") or 0)
                current = current_by_id.get(identifier)
                if not current:
                    raise RuntimeError(
                        f"俱乐部控制人（ID {identifier}）已变化，不能恢复原资料"
                    )
                current_person = _address(current.get("address"))
                if (
                    not current_person
                    or int(reader.u32(current_person + ENTITY_UID) or 0)
                    != identifier
                ):
                    raise RuntimeError("俱乐部控制人人物对象校验失败")
                current_complete = (
                    current_person - int(layout.staff_complete_object_offset)
                )
                current_base = (
                    current_complete + int(layout.chairman_base_adjustment)
                )
                current_name_fields = (
                    current_person + int(layout.person_first_name_offset),
                    current_person + int(layout.person_last_name_offset),
                    current_person + int(layout.person_common_name_offset),
                    current_person + int(layout.person_full_name_offset),
                )
                saved_fields = {
                    str(field) for field in saved.get("modified_fields") or []
                }
                controller_restore_states.append({
                    "id": identifier,
                    "person": current_person,
                    "modified_fields": sorted(saved_fields),
                    "target_name": str(saved.get("name") or "").strip(),
                    "target_patience": saved.get("patience"),
                    "target_interference": saved.get("interference"),
                    "name_fields": current_name_fields,
                    "name_pointers": [
                        int(reader.ptr(address) or 0)
                        for address in current_name_fields
                    ],
                    "patience_address": (
                        current_base + int(layout.chairman_patience_offset)
                    ),
                    "patience": reader.u8(
                        current_base + int(layout.chairman_patience_offset)
                    ),
                    "interference_address": (
                        current_base + int(layout.chairman_interference_offset)
                    ),
                    "interference": reader.u8(
                        current_base + int(layout.chairman_interference_offset)
                    ),
                    "allocated": 0,
                    "free_address": 0,
                })

        rollback = {
            "pid": pid,
            "modified_fields": sorted(modified_fields),
            "name_fields": name_fields,
            "name_pointers": [int(reader.ptr(address) or 0) for address in name_fields],
            "status_address": status_address,
            "status": reader.u16(status_address),
            "patience_address": patience_address,
            "patience": reader.u8(patience_address) if patience_address else None,
            "interference_address": interference_address,
            "interference": reader.u8(interference_address) if interference_address else None,
            "sugar_daddy_address": sugar_daddy_address,
            "sugar_daddy": reader.u8(sugar_daddy_address) if sugar_daddy_address else None,
            "chairman_nationality_address": nationality_address,
            "chairman_nationality": (
                int(reader.ptr(nationality_address) or 0)
                if nationality_address else 0
            ),
            "chairman_birth_date_address": birth_address,
            "chairman_birth_date": (
                reader.bytes(birth_address, 4) if birth_address else None
            ),
            "ownership_address": ownership_address,
            "ownership": reader.bytes(ownership_address + 0x0C, 12) if ownership_address else None,
            "controllers": controller_restore_states,
        }
        try:
            for state in controller_restore_states:
                state_fields = set(state["modified_fields"])
                if (
                    "name" in state_fields
                    and state["target_name"]
                    and _name(reader, state["person"]) != state["target_name"]
                ):
                    state["allocated"], state["free_address"] = (
                        _write_person_display_name(
                            process,
                            state["name_fields"],
                            state["target_name"].encode("utf-8"),
                        )
                    )
                if (
                    "patience" in state_fields
                    and state["target_patience"] is not None
                ):
                    write_process_memory(
                        process, state["patience_address"],
                        bytes([int(state["target_patience"])]),
                    )
                if (
                    "interference" in state_fields
                    and state["target_interference"] is not None
                ):
                    write_process_memory(
                        process, state["interference_address"],
                        bytes([int(state["target_interference"])]),
                    )
            if not controller_restore_states and "chairman_name" in modified_fields and person and target_name and _name(reader, person) != target_name:
                allocated, free_address = _write_person_display_name(
                    process, name_fields, target_name.encode("utf-8"),
                )
            if "chairman_status" in modified_fields and target_status is not None:
                write_process_memory(process, status_address, struct.pack("<H", target_status))
            if not controller_restore_states and "chairman_patience" in modified_fields and patience_address and target_patience is not None:
                write_process_memory(process, patience_address, bytes([int(target_patience)]))
            if not controller_restore_states and "chairman_interference" in modified_fields and interference_address and target_interference is not None:
                write_process_memory(process, interference_address, bytes([int(target_interference)]))
            if "sugar_daddy" in modified_fields and sugar_daddy_address and target_sugar_daddy is not None:
                write_process_memory(process, sugar_daddy_address, bytes([int(target_sugar_daddy)]))
            if "chairman_nationality" in modified_fields:
                write_process_memory(
                    process, nationality_address,
                    struct.pack("<Q", target_nationality),
                )
            if "chairman_birth_date" in modified_fields:
                write_process_memory(
                    process, birth_address, target_birth_raw,
                )
            if "ownership" in modified_fields and ownership_address and target_ownership is not None:
                write_process_memory(process, ownership_address + 0x0C, target_ownership)
            reader.string_cache.clear()
            if (
                any(
                    (
                        "name" in set(state["modified_fields"])
                        and state["target_name"]
                        and _name(reader, state["person"]) != state["target_name"]
                    )
                    or (
                        "patience" in set(state["modified_fields"])
                        and state["target_patience"] is not None
                        and reader.u8(state["patience_address"])
                        != int(state["target_patience"])
                    )
                    or (
                        "interference" in set(state["modified_fields"])
                        and state["target_interference"] is not None
                        and reader.u8(state["interference_address"])
                        != int(state["target_interference"])
                    )
                    for state in controller_restore_states
                )
                or (not controller_restore_states and "chairman_name" in modified_fields and person and target_name and _name(reader, person) != target_name)
                or ("chairman_status" in modified_fields and reader.u16(status_address) != target_status)
                or (not controller_restore_states and "chairman_patience" in modified_fields and patience_address and target_patience is not None and reader.u8(patience_address) != int(target_patience))
                or (not controller_restore_states and "chairman_interference" in modified_fields and interference_address and target_interference is not None and reader.u8(interference_address) != int(target_interference))
                or ("sugar_daddy" in modified_fields and sugar_daddy_address and target_sugar_daddy is not None and reader.u8(sugar_daddy_address) != int(target_sugar_daddy))
                or (
                    "chairman_nationality" in modified_fields
                    and reader.ptr(nationality_address) != target_nationality
                )
                or (
                    "chairman_birth_date" in modified_fields
                    and reader.bytes(birth_address, 4) != target_birth_raw
                )
                or ("ownership" in modified_fields and ownership_address and target_ownership is not None and reader.bytes(ownership_address + 0x0C, 12) != target_ownership)
            ):
                raise RuntimeError("俱乐部出售恢复后校验失败")
        except Exception:
            for state in reversed(controller_restore_states):
                state_fields = set(state["modified_fields"])
                if "name" in state_fields:
                    for address, pointer in zip(
                        state["name_fields"],
                        state["name_pointers"],
                        strict=True,
                    ):
                        write_process_memory(
                            process, address, struct.pack("<Q", pointer),
                        )
                if (
                    "patience" in state_fields
                    and state["patience"] is not None
                ):
                    write_process_memory(
                        process, state["patience_address"],
                        bytes([int(state["patience"])]),
                    )
                if (
                    "interference" in state_fields
                    and state["interference"] is not None
                ):
                    write_process_memory(
                        process, state["interference_address"],
                        bytes([int(state["interference"])]),
                    )
                if state.get("allocated"):
                    _remote_free_block(
                        process,
                        int(state.get("free_address") or 0),
                        int(state["allocated"]),
                    )
            if not controller_restore_states and "chairman_name" in modified_fields:
                for address, pointer in zip(name_fields, rollback["name_pointers"], strict=True):
                    write_process_memory(process, address, struct.pack("<Q", pointer))
            if "chairman_status" in modified_fields and rollback["status"] is not None:
                write_process_memory(process, status_address, struct.pack("<H", int(rollback["status"])))
            if not controller_restore_states and "chairman_patience" in modified_fields and patience_address and rollback["patience"] is not None:
                write_process_memory(process, patience_address, bytes([int(rollback["patience"])]))
            if not controller_restore_states and "chairman_interference" in modified_fields and interference_address and rollback["interference"] is not None:
                write_process_memory(process, interference_address, bytes([int(rollback["interference"])]))
            if "sugar_daddy" in modified_fields and sugar_daddy_address and rollback["sugar_daddy"] is not None:
                write_process_memory(process, sugar_daddy_address, bytes([int(rollback["sugar_daddy"])]))
            if (
                "chairman_nationality" in modified_fields
                and rollback["chairman_nationality"]
            ):
                write_process_memory(
                    process, nationality_address,
                    struct.pack("<Q", int(rollback["chairman_nationality"])),
                )
            if (
                "chairman_birth_date" in modified_fields
                and rollback.get("chairman_birth_date")
            ):
                write_process_memory(
                    process, birth_address,
                    bytes(rollback["chairman_birth_date"]),
                )
            if "ownership" in modified_fields and ownership_address and rollback["ownership"]:
                write_process_memory(process, ownership_address + 0x0C, bytes(rollback["ownership"]))
            if not controller_restore_states:
                _remote_free_block(process, free_address, allocated)
            raise
    rollback.update({"allocated": allocated, "free_address": free_address})
    return {
        "restored": True,
        "legacy": False,
        "chairman_name": target_name or None,
        "modified_fields": sorted(modified_fields),
        "_rollback": rollback,
    }


def rollback_native_club_sale(snapshot: dict[str, Any]) -> None:
    if not snapshot:
        return
    pid, _path, _layout = select_process_layout()
    if int(snapshot.get("pid") or 0) != pid:
        raise RuntimeError("FM 进程已变化，无法回滚俱乐部出售")
    modified_fields = (
        set(snapshot.get("modified_fields") or [])
        if "modified_fields" in snapshot
        else {
            "chairman_name", "chairman_status", "chairman_patience",
            "chairman_interference", "chairman_nationality", "sugar_daddy",
            "chairman_birth_date", "ownership",
        }
    )
    modified_fields.discard("ownership")
    if not modified_fields:
        return
    with open_process(pid, write_memory=True) as process:
        controller_snapshots = list(snapshot.get("controllers") or [])
        if controller_snapshots:
            for state in reversed(controller_snapshots):
                state_fields = set(state.get("modified_fields") or [])
                if "name" in state_fields:
                    for address, pointer in zip(
                        state.get("name_fields") or (),
                        state.get("name_pointers") or (),
                        strict=True,
                    ):
                        write_process_memory(
                            process, int(address),
                            struct.pack("<Q", int(pointer)),
                        )
                if (
                    "patience" in state_fields
                    and state.get("patience_address")
                    and state.get("patience") is not None
                ):
                    write_process_memory(
                        process, int(state["patience_address"]),
                        bytes([int(state["patience"])]),
                    )
                if (
                    "interference" in state_fields
                    and state.get("interference_address")
                    and state.get("interference") is not None
                ):
                    write_process_memory(
                        process, int(state["interference_address"]),
                        bytes([int(state["interference"])]),
                    )
                if "name" in state_fields:
                    _remote_free_block(
                        process,
                        int(state.get("free_address") or 0),
                        int(state.get("allocated") or 0),
                    )
        elif "chairman_name" in modified_fields:
            for address, pointer in zip(
                snapshot.get("name_fields") or (), snapshot.get("name_pointers") or (), strict=True,
            ):
                write_process_memory(process, int(address), struct.pack("<Q", int(pointer)))
        if "chairman_status" in modified_fields and snapshot.get("status_address") and snapshot.get("status") is not None:
            write_process_memory(
                process, int(snapshot["status_address"]), struct.pack("<H", int(snapshot["status"])),
            )
        for field, address_key, value_key in (
            ("chairman_patience", "patience_address", "patience"),
            ("chairman_interference", "interference_address", "interference"),
            ("sugar_daddy", "sugar_daddy_address", "sugar_daddy"),
        ):
            if (
                (not controller_snapshots or field == "sugar_daddy")
                and field in modified_fields
                and snapshot.get(address_key)
                and snapshot.get(value_key) is not None
            ):
                write_process_memory(
                    process, int(snapshot[address_key]), bytes([int(snapshot[value_key])]),
                )
        if (
            "chairman_nationality" in modified_fields
            and snapshot.get("chairman_nationality_address")
            and snapshot.get("chairman_nationality")
        ):
            write_process_memory(
                process, int(snapshot["chairman_nationality_address"]),
                struct.pack("<Q", int(snapshot["chairman_nationality"])),
            )
        if (
            "chairman_birth_date" in modified_fields
            and snapshot.get("chairman_birth_date_address")
            and snapshot.get("chairman_birth_date")
        ):
            write_process_memory(
                process, int(snapshot["chairman_birth_date_address"]),
                bytes(snapshot["chairman_birth_date"]),
            )
        if "ownership" in modified_fields and snapshot.get("ownership_address") and snapshot.get("ownership"):
            write_process_memory(
                process, int(snapshot["ownership_address"]) + 0x0C,
                bytes(snapshot["ownership"]),
            )
        if not controller_snapshots and "chairman_name" in modified_fields:
            _remote_free_block(
                process, int(snapshot.get("free_address") or 0), int(snapshot.get("allocated") or 0),
            )


def enforce_acquired_club_chairman_status(
    team_id: int, team_address: Any, owner_name: str | None = None, *,
    manager_id: int = 0, manager_address: Any = 0,
) -> bool:
    """Re-apply verified controller fields to the club's current controllers.

    A takeover can replace controller objects with new UIDs, which silently
    drops the player name, patience and interference written at acquisition.
    Every controller slot is re-resolved against its live UID and only the
    fields whose current value differs are rewritten, so the check stays
    idempotent and never touches ownership.
    """
    pid, _path, layout = select_process_layout()
    if (
        layout.club_detail2_offset is None
        or layout.club_chairman_status_offset is None
    ):
        return False
    patience_offset = getattr(layout, "chairman_patience_offset", None)
    interference_offset = getattr(layout, "chairman_interference_offset", None)
    complete_offset = getattr(layout, "staff_complete_object_offset", None)
    base_adjustment = getattr(layout, "chairman_base_adjustment", None)
    controller_base_ready = (
        complete_offset is not None and base_adjustment is not None
    )
    with open_process(pid, write_memory=True, create_thread=True) as process:
        module = layout.module(process)
        if not module:
            return False
        reader = Reader(process, module.base_address, layout)
        team = _resolve_team_address(reader, int(team_id), team_address)
        club = int(reader.ptr(team + TEAM_CLUB) or 0)
        detail2 = int(reader.ptr(club + int(layout.club_detail2_offset)) or 0)
        if not detail2:
            return False
        address = detail2 + int(layout.club_chairman_status_offset)
        status_before = reader.u16(address)
        controller_states: list[dict[str, Any]] = []
        nationality_field = nationality_before = target_nation = 0
        try:
            expected_name = str(owner_name or "").strip()
            controllers = (
                _scan_club_controllers(reader, team, int(manager_id))
                if expected_name or manager_id else []
            )
            target_name_references: tuple[int, int, int] = ()
            if expected_name and manager_id:
                manager_person = _validated_manager_person(
                    reader, int(manager_id), manager_address,
                )
                _manager_name_fields, target_name_references = (
                    _validated_person_name_references(reader, manager_person)
                )
            for controller in controllers:
                person = _address(controller.get("address"))
                identifier = int(controller.get("id") or 0)
                if (
                    not person or identifier <= 0
                    or int(reader.u32(person + ENTITY_UID) or 0) != identifier
                ):
                    continue
                state: dict[str, Any] = {
                    "name_fields": (),
                    "name_pointers": (),
                    "patience_address": 0,
                    "patience_before": None,
                    "interference_address": 0,
                    "interference_before": None,
                }
                if target_name_references:
                    name_fields = (
                        person + int(layout.person_first_name_offset),
                        person + int(layout.person_last_name_offset),
                        person + int(layout.person_common_name_offset),
                    )
                    state["name_fields"] = name_fields
                    state["name_pointers"] = tuple(
                        int(reader.ptr(field) or 0) for field in name_fields
                    )
                if controller_base_ready:
                    controller_base = (
                        person - int(complete_offset) + int(base_adjustment)
                    )
                    if patience_offset is not None:
                        patience_address = controller_base + int(patience_offset)
                        patience_before = reader.u8(patience_address)
                        if (
                            patience_before is None
                            or not 0 <= int(patience_before) <= 20
                        ):
                            raise RuntimeError("俱乐部控制人耐心原值校验失败")
                        state["patience_address"] = patience_address
                        state["patience_before"] = int(patience_before)
                    if interference_offset is not None:
                        interference_address = (
                            controller_base + int(interference_offset)
                        )
                        interference_before = reader.u8(interference_address)
                        if (
                            interference_before is None
                            or not 0 <= int(interference_before) <= 20
                        ):
                            raise RuntimeError("俱乐部控制人干预程度原值校验失败")
                        state["interference_address"] = interference_address
                        state["interference_before"] = int(interference_before)
                controller_states.append(state)
            chairman = _club_controller_staff(controllers)
            chairman_person = _address(chairman.get("address")) if chairman else 0
            if manager_id and chairman_person:
                target_nation, _target_nation_id = _manager_primary_nation(
                    reader, module.base_address, int(manager_id), manager_address,
                )
                nationality_field = (
                    chairman_person + int(layout.person_nationality_offset)
                    if layout.person_nationality_offset is not None else 0
                )
                nationality_before = (
                    int(reader.ptr(nationality_field) or 0)
                    if nationality_field else 0
                )
                current_nation_id = (
                    int(reader.u32(nationality_before + ENTITY_UID) or 0)
                    if nationality_before else 0
                )
                _validated_nationality_target(
                    reader, module.base_address,
                    current_nation_id, nationality_before,
                )
            for state in controller_states:
                if state["name_pointers"]:
                    if tuple(state["name_pointers"]) != target_name_references:
                        _write_person_name_references(
                            process, state["name_fields"], target_name_references,
                        )
                        if tuple(
                            int(reader.ptr(field) or 0)
                            for field in state["name_fields"]
                        ) != target_name_references:
                            raise RuntimeError("主席姓名引用写回后校验失败")
                if state["patience_address"] and state["patience_before"] != 20:
                    write_process_memory(
                        process, int(state["patience_address"]), bytes([20]),
                    )
                if (
                    state["interference_address"]
                    and state["interference_before"] != 0
                ):
                    write_process_memory(
                        process, int(state["interference_address"]), bytes([0]),
                    )
            if nationality_field and nationality_before != target_nation:
                write_process_memory(
                    process, nationality_field, struct.pack("<Q", target_nation),
                )
            if status_before != 10000:
                write_process_memory(process, address, struct.pack("<H", 10000))
            if reader.u16(address) != 10000:
                raise RuntimeError("主席状态写入后校验失败")
            if (
                nationality_field and target_nation
                and reader.ptr(nationality_field) != target_nation
            ):
                raise RuntimeError("主席国籍写入后校验失败")
            for state in controller_states:
                if (
                    state["patience_address"] and state["patience_before"] != 20
                    and reader.u8(int(state["patience_address"])) != 20
                ):
                    raise RuntimeError("俱乐部控制人耐心写入后校验失败")
                if (
                    state["interference_address"]
                    and state["interference_before"] != 0
                    and reader.u8(int(state["interference_address"])) != 0
                ):
                    raise RuntimeError("俱乐部控制人干预程度写入后校验失败")
            return True
        except Exception:
            if status_before is not None:
                write_process_memory(process, address, struct.pack("<H", status_before))
            for state in controller_states:
                if state["name_fields"] and state["name_pointers"]:
                    for field, pointer in zip(
                        state["name_fields"], state["name_pointers"], strict=True,
                    ):
                        write_process_memory(process, field, struct.pack("<Q", pointer))
                if (
                    state["patience_address"]
                    and state["patience_before"] is not None
                ):
                    write_process_memory(
                        process, int(state["patience_address"]),
                        bytes([int(state["patience_before"])]),
                    )
                if (
                    state["interference_address"]
                    and state["interference_before"] is not None
                ):
                    write_process_memory(
                        process, int(state["interference_address"]),
                        bytes([int(state["interference_before"])]),
                    )
            if nationality_field and nationality_before:
                write_process_memory(
                    process, nationality_field,
                    struct.pack("<Q", nationality_before),
                )
            raise


def rename_native_club(
    team_id: int, team_address: Any, new_name: str | None = None, *,
    new_short_name: str | None = None, is_managed_club: bool = False,
) -> dict[str, Any]:
    """Update the full and short club names through their separate FMRTE paths."""
    requested_name = None if new_name is None else str(new_name).strip()
    requested_short_name = (
        None if new_short_name is None else str(new_short_name).strip()
    )
    if requested_name is None and requested_short_name is None:
        raise ValueError("请至少提供一个俱乐部名称字段")
    for label, value in (
        ("俱乐部完整名称", requested_name),
        ("俱乐部简称", requested_short_name),
    ):
        if value is None:
            continue
        encoded_value = value.encode("utf-8")
        if (
            not value or len(value) > 60 or len(encoded_value) > 180
            or any(ord(char) < 32 for char in value)
        ):
            raise ValueError(f"{label}需为 1 至 60 个有效字符")
    pid, _path, layout = select_process_layout()
    allocation = new_short_pointer = 0
    with open_process(pid, write_memory=True, create_thread=True) as process:
        module = layout.module(process)
        if not module:
            raise RuntimeError(f"{layout.module_name} 尚未加载")
        reader = Reader(process, module.base_address, layout)
        team = _resolve_team_address(reader, int(team_id), team_address)
        club = int(reader.ptr(team + TEAM_CLUB) or 0)
        if (
            not club
            or reader.ptr(club) != module.base_address + layout.club_vtable_rva
            or int(reader.u32(club + ENTITY_UID) or 0) != int(team_id)
        ):
            raise RuntimeError("俱乐部对象校验失败")
        fields = (club + CLUB_NAME_FULL, club + CLUB_NAME_SHORT)
        before_pointers = tuple(int(reader.ptr(address) or 0) for address in fields)
        before_names = tuple(reader.fm_string_at(address) for address in fields)
        if not all(before_pointers) or not all(before_names):
            raise RuntimeError("俱乐部原名称读取失败")
        before_name = str(before_names[0] or "")
        before_short_name = str(before_names[1] or "")
        target_name = requested_name if requested_name is not None else before_name
        target_short_name = (
            requested_short_name
            if requested_short_name is not None else before_short_name
        )
        full_changed = target_name != before_name
        short_changed = target_short_name != before_short_name
        if not full_changed and not short_changed:
            raise ValueError("新的俱乐部完整名称和简称均与当前值相同")
        full_pointer, short_pointer = before_pointers
        full_header = reader.bytes(full_pointer - 12, 16)
        short_header = reader.bytes(short_pointer - 12, 16)
        if (
            not full_header or len(full_header) != 16
            or not short_header or len(short_header) != 16
        ):
            raise RuntimeError("俱乐部名称字符串头读取失败")
        full_capacity_word, full_refcount, full_length = struct.unpack(
            "<QII", full_header,
        )
        short_capacity_word, short_refcount, short_length = struct.unpack(
            "<QII", short_header,
        )
        if (
            not 0 < full_length <= 256
            or not 0 < short_length <= 256
            or not 0 < full_refcount < 1_000_000
            or not 0 < short_refcount < 1_000_000
            or (full_capacity_word & 0xFFFFFFFF) < full_length + 9
            or (short_capacity_word & 0xFFFFFFFF) < short_length + 9
        ):
            raise RuntimeError("俱乐部名称字符串头校验失败")
        full_before_raw = reader.bytes(full_pointer + 4, full_length)
        if not full_before_raw or len(full_before_raw) != full_length:
            raise RuntimeError("俱乐部完整名称原始内容读取失败")
        full_after_raw = target_name.encode("utf-8")
        if full_changed and (
            len(target_name) != len(before_name) or len(full_after_raw) != full_length
        ):
            raise ValueError(
                "俱乐部完整名称仅支持与原名称字符数和存储长度都一致的新名称；"
                "简称可单独使用任意有效长度。"
            )
        short_after_raw = target_short_name.encode("utf-8")
        replace_short_pointer = short_changed or (
            full_changed and full_pointer == short_pointer
        )
        try:
            if replace_short_pointer:
                new_short_pointer, allocation = _allocate_fm_utf8_string(
                    process, short_after_raw,
                )
                # FMRTE's ShortName setter replaces the field pointer.  Do this
                # before mutating a possibly shared full-name payload so the two
                # logical fields never observe the same changed buffer.
                write_process_memory(
                    process, fields[1], struct.pack("<Q", new_short_pointer),
                )
            if full_changed:
                write_process_memory(process, full_pointer + 4, full_after_raw)
            reader.string_cache.clear()
            if (
                reader.ptr(fields[0]) != full_pointer
                or reader.ptr(fields[1]) != (
                    new_short_pointer if replace_short_pointer else short_pointer
                )
                or reader.fm_string_at(fields[0]) != target_name
                or reader.fm_string_at(fields[1]) != target_short_name
            ):
                raise RuntimeError("俱乐部名称写入后校验失败")
            sleep(0.15)
            reader.string_cache.clear()
            if (
                reader.ptr(fields[0]) != full_pointer
                or reader.ptr(fields[1]) != (
                    new_short_pointer if replace_short_pointer else short_pointer
                )
                or (
                    full_changed
                    and reader.bytes(full_pointer + 4, full_length) != full_after_raw
                )
                or reader.fm_string_at(fields[0]) != target_name
                or reader.fm_string_at(fields[1]) != target_short_name
            ):
                raise RuntimeError("俱乐部名称未能保持稳定")
        except Exception as write_error:
            if full_changed:
                write_process_memory(process, full_pointer + 4, full_before_raw)
            for field, pointer in zip(fields, before_pointers, strict=True):
                write_process_memory(process, field, struct.pack("<Q", pointer))
            reader.string_cache.clear()
            restored = (
                reader.ptr(fields[0]) == full_pointer
                and reader.ptr(fields[1]) == short_pointer
                and reader.bytes(full_pointer + 4, full_length) == full_before_raw
            )
            _free_fm_utf8_string(process, allocation)
            if not restored:
                raise RuntimeError("俱乐部改名失败且原名称恢复异常") from write_error
            raise
    patch_club_profile_cache(
        int(team_id), name=target_name, short_name=target_short_name,
    )
    return {
        "team_id": int(team_id),
        "before": {"name": before_name, "short_name": before_short_name},
        "after": {"name": target_name, "short_name": target_short_name},
    }


def update_native_club_sugar_daddy(
    team_id: int, team_address: Any, target: int, *,
    expected: int | None = None,
    operation: Any | None = None,
) -> dict[str, Any]:
    """Change the verified BasicFinances.SugarDaddy byte for one club.

    The field is only enabled by layouts with an independently verified
    offset.  The club and finance object must point back to the requested
    team/club before the byte is changed, and a failed read-back restores the
    previous value.
    """
    try:
        target_value = int(target)
    except (TypeError, ValueError) as error:
        raise ValueError("俱乐部资助类型无效") from error
    if target_value not in range(5):
        raise ValueError("俱乐部资助类型无效")
    if operation is None:
        with borrow_game_operation(write_memory=True) as current:
            return update_native_club_sugar_daddy(
                team_id, team_address, target_value, expected=expected,
                operation=current,
            )
    if not operation.writable:
        raise RuntimeError("club sugar daddy write requires a writable operation session")
    reader = operation.reader
    process = operation.process
    layout = operation.layout
    sugar_offset = getattr(layout, "finance_sugar_daddy_offset", None)
    club_finance_offset = getattr(layout, "club_finance_offset", None)
    if sugar_offset is None or club_finance_offset is None:
        raise RuntimeError("当前游戏版本尚未映射俱乐部资助类型")
    team = _resolve_team_address(reader, int(team_id), team_address)
    club = int(reader.ptr(team + TEAM_CLUB) or 0)
    if (
        not club
        or reader.ptr(club) != reader.module_base + int(layout.club_vtable_rva)
        or int(reader.u32(club + ENTITY_UID) or 0) != int(team_id)
    ):
        raise RuntimeError("俱乐部对象校验失败")
    finance = int(reader.ptr(club + int(club_finance_offset)) or 0)
    if not finance or int(reader.ptr(finance + 0x08) or 0) != club:
        raise RuntimeError("俱乐部财务对象校验失败")
    address = finance + int(sugar_offset)
    current = reader.u8(address)
    if current not in range(5):
        raise RuntimeError("俱乐部资助类型原值校验失败")
    current = int(current)
    if expected is not None and current != int(expected):
        raise RuntimeError("俱乐部资助类型刚刚发生变化，请刷新后重试")
    if current == target_value:
        return {
            "team_id": int(team_id), "before": current, "after": current,
            "before_name": SUGAR_DADDY_NAMES[current],
            "after_name": SUGAR_DADDY_NAMES[current],
        }
    write_process_memory(
        process, address, bytes([target_value]), compatibility_fallback=True,
    )
    after = reader.u8(address)
    if after != target_value:
        try:
            write_process_memory(
                process, address, bytes([current]), compatibility_fallback=True,
            )
            restored = reader.u8(address)
        except Exception as error:
            raise RuntimeError("俱乐部资助类型写入失败，且原值恢复失败") from error
        if restored != current:
            raise RuntimeError("俱乐部资助类型写入失败，且原值恢复失败")
        raise RuntimeError("俱乐部资助类型写入后校验失败，已恢复原值")
    return {
        "team_id": int(team_id), "before": current, "after": int(after),
        "before_name": SUGAR_DADDY_NAMES[current],
        "after_name": SUGAR_DADDY_NAMES[int(after)],
    }


def update_native_stadium(
    team_id: int, team_address: Any, *, name: str | None = None,
    current_capacity: int | None = None, expansion_capacity: int | None = None,
    state_raw: int | None = None, pitch_condition: int | None = None,
    pitch_type: int | None = None,
    expected_name: str | None = None, expected_capacity: int | None = None,
    expected_expansion_capacity: int | None = None,
    expected_state_raw: int | None = None,
    expected_pitch_condition: int | None = None,
    expected_pitch_type: int | None = None,
) -> dict[str, Any]:
    """Update selected stadium fields in one readback-verified transaction."""
    name_allocation = new_name_pointer = 0
    requested_name = str(name or "").strip() if name is not None else None
    encoded_name = requested_name.encode("utf-8") if requested_name is not None else b""
    if requested_name is not None and (
        not requested_name or len(requested_name) > 60 or len(encoded_name) > 180
        or any(ord(char) < 32 for char in requested_name)
    ):
        raise ValueError("球场名称需为 1 至 60 个有效字符")
    requested_capacity = int(current_capacity) if current_capacity is not None else None
    requested_expansion = int(expansion_capacity) if expansion_capacity is not None else None
    requested_state = int(state_raw) if state_raw is not None else None
    requested_pitch_condition = (
        int(pitch_condition) if pitch_condition is not None else None
    )
    requested_pitch_type = int(pitch_type) if pitch_type is not None else None
    if requested_capacity is not None and not 1 <= requested_capacity <= 500_000:
        raise ValueError("球场当前容量需为 1 至 500000")
    if requested_expansion is not None and not 1 <= requested_expansion <= 500_000:
        raise ValueError("球场扩建上限需为 1 至 500000")
    if requested_state is not None and not 0 <= requested_state <= 255:
        raise ValueError("球场状态值无效")
    if requested_pitch_condition is not None and not 0 <= requested_pitch_condition <= 200:
        raise ValueError("草皮状况需为 0 至 200")
    if requested_pitch_type is not None and requested_pitch_type not in PITCH_TYPE_NAMES:
        raise ValueError("草皮类型无效")

    pid, _path, layout = select_process_layout()
    offsets = (
        layout.stadium_capacity_offset,
        layout.stadium_seating_capacity_offset,
        layout.stadium_used_capacity_offset,
        layout.stadium_all_seater_capacity_offset,
        layout.stadium_expansion_capacity_offset,
    )
    if any(value is None for value in offsets):
        raise RuntimeError("当前游戏布局缺少球场容量字段")
    numeric_offsets = tuple(int(value) for value in offsets)
    if numeric_offsets != tuple(range(numeric_offsets[0], numeric_offsets[0] + 20, 4)):
        raise RuntimeError("当前游戏版本的球场容量布局不连续，已拒绝写入")

    with open_process(pid, write_memory=True, create_thread=True) as process:
        module = layout.module(process)
        if not module:
            raise RuntimeError(f"{layout.module_name} 尚未加载")
        reader = Reader(process, module.base_address, layout)
        team = _resolve_team_address(reader, int(team_id), team_address)
        club = int(reader.ptr(team + TEAM_CLUB) or 0)
        stadium = int(reader.ptr(team + TEAM_STADIUM) or 0)
        stadium_vtable = int(reader.ptr(stadium) or 0) if stadium else 0
        if layout.stadium_vtable_rva is not None:
            stadium_vtable_valid = (
                stadium_vtable == module.base_address + int(layout.stadium_vtable_rva)
            )
        else:
            module_size = int(getattr(module, "size", 0) or 0)
            peers = []
            if club and layout.club_training_ground_offset is not None:
                peers.append(reader.ptr(club + int(layout.club_training_ground_offset)))
            if stadium and layout.stadium_nearby_offset is not None:
                peers.append(reader.ptr(stadium + int(layout.stadium_nearby_offset)))
            stadium_vtable_valid = bool(
                module_size > 0
                and module.base_address <= stadium_vtable < module.base_address + module_size
                and any(
                    peer and reader.ptr(int(peer)) == stadium_vtable
                    for peer in peers
                )
            )
        stadium_uid = (
            reader.u32(stadium + int(layout.stadium_id_offset))
            if stadium and layout.stadium_id_offset is not None else None
        )
        stadium_uid2 = (
            reader.u32(stadium + int(layout.stadium_id2_offset))
            if stadium and layout.stadium_id2_offset is not None else None
        )
        if (
            not club or not stadium
            or reader.ptr(club) != module.base_address + layout.club_vtable_rva
            or int(reader.u32(club + ENTITY_UID) or 0) != int(team_id)
            or not stadium_vtable_valid
            or not stadium_uid or stadium_uid != stadium_uid2
        ):
            raise RuntimeError("俱乐部或球场对象校验失败")

        capacity_address = stadium + numeric_offsets[0]
        original_capacity_raw = reader.bytes(capacity_address, 20)
        if not original_capacity_raw or len(original_capacity_raw) != 20:
            raise RuntimeError("球场原容量读取失败")
        before_values = tuple(int(value) for value in struct.unpack("<5I", original_capacity_raw))
        before_capacity, before_seating, before_used, before_all_seater, before_expansion = before_values
        if (
            before_capacity > 500_000 or before_seating > before_capacity
            or before_used > before_capacity or before_all_seater > before_capacity
            or before_expansion < before_capacity or before_expansion > 500_000
        ):
            raise RuntimeError("球场原容量关系无效")
        before_name = _stadium_name(reader, stadium)
        name_field = stadium + int(layout.stadium_name_offset)
        before_name_pointer = int(reader.ptr(name_field) or 0)
        if not before_name or not before_name_pointer:
            raise RuntimeError("球场原名称读取失败")
        name_original_raw = name_target_raw = name_write_raw = None
        replace_name_pointer = False
        if requested_name is not None and requested_name != before_name:
            name_header = reader.bytes(before_name_pointer - 12, 16)
            if not name_header or len(name_header) != 16:
                raise RuntimeError("球场名称字符串头读取失败，请重启 FM 并重新载入存档")
            capacity_word, name_refcount, name_length = struct.unpack(
                "<QII", name_header,
            )
            if (
                not 0 < name_length <= 256
                or name_refcount != 1
                or (capacity_word & 0xFFFFFFFF) < name_length + 9
            ):
                raise RuntimeError(
                    "当前球场名称的存储状态不适合安全修改；请重启 FM、重新载入存档后再试"
                )
            name_original_raw = reader.bytes(before_name_pointer + 4, name_length)
            if not name_original_raw or len(name_original_raw) != name_length:
                raise RuntimeError("球场原名称内容读取失败")
            visible_raw = name_original_raw.rstrip(bytes(range(32)))
            trailing_marker = name_original_raw[len(visible_raw):]
            try:
                visible_name = visible_raw.decode("utf-8")
            except UnicodeDecodeError as error:
                raise RuntimeError("球场原名称不是有效 UTF-8") from error
            if visible_name != before_name or trailing_marker not in {b"", b"\x09"}:
                raise RuntimeError("球场名称本地化标记或可见文本校验失败")
            replace_name_pointer = (
                len(requested_name) != len(before_name)
                or len(encoded_name) != len(visible_raw)
            )
            # The verified FMRTE path writes the existing visible payload.
            # Different lengths use an experimental managed-block replacement
            # so the old allocation is never overwritten past its boundary.
            name_write_raw = None if replace_name_pointer else encoded_name
            name_target_raw = encoded_name + trailing_marker
        if expected_name is not None and str(expected_name).strip() != before_name:
            raise ValueError("球场名称已经变化，请刷新后重试")
        if expected_capacity is not None and int(expected_capacity) != before_capacity:
            raise ValueError("球场当前容量已经变化，请刷新后重试")
        if (
            expected_expansion_capacity is not None
            and int(expected_expansion_capacity) != before_expansion
        ):
            raise ValueError("球场扩建上限已经变化，请刷新后重试")
        state_address = (
            stadium + int(layout.stadium_state_offset)
            if layout.stadium_state_offset is not None else 0
        )
        before_state = reader.u8(state_address) if state_address else None
        if requested_state is not None and before_state is None:
            raise RuntimeError("当前游戏布局缺少球场状态字段")
        if expected_state_raw is not None and int(expected_state_raw) != int(before_state or 0):
            raise ValueError("球场状态已经变化，请刷新后重试")
        pitch_condition_address = (
            stadium + int(getattr(layout, "stadium_pitch_condition_offset", None))
            if getattr(layout, "stadium_pitch_condition_offset", None) is not None else 0
        )
        pitch_type_address = (
            stadium + int(getattr(layout, "stadium_pitch_type_offset", None))
            if getattr(layout, "stadium_pitch_type_offset", None) is not None else 0
        )
        before_pitch_condition = (
            reader.u8(pitch_condition_address) if pitch_condition_address else None
        )
        before_pitch_type = reader.u8(pitch_type_address) if pitch_type_address else None
        if requested_pitch_condition is not None and before_pitch_condition is None:
            raise RuntimeError("当前游戏布局缺少草皮状况字段")
        if requested_pitch_type is not None and before_pitch_type is None:
            raise RuntimeError("当前游戏布局缺少草皮类型字段")
        if (
            (requested_pitch_condition is not None or expected_pitch_condition is not None)
            and before_pitch_condition is not None
            and not 0 <= int(before_pitch_condition) <= 200
        ):
            raise RuntimeError("球场原草皮状况无效")
        if (
            (requested_pitch_type is not None or expected_pitch_type is not None)
            and before_pitch_type is not None
            and int(before_pitch_type) not in PITCH_TYPE_NAMES
        ):
            raise RuntimeError("球场原草皮类型无效")
        if (
            expected_pitch_condition is not None
            and int(expected_pitch_condition) != int(before_pitch_condition or 0)
        ):
            raise ValueError("草皮状况已经变化，请刷新后重试")
        if (
            expected_pitch_type is not None
            and int(expected_pitch_type) != int(before_pitch_type or 0)
        ):
            raise ValueError("草皮类型已经变化，请刷新后重试")

        target_name = requested_name if requested_name is not None else before_name
        target_capacity = requested_capacity if requested_capacity is not None else before_capacity
        target_expansion = requested_expansion if requested_expansion is not None else before_expansion
        if not target_capacity <= target_expansion <= 500_000:
            raise ValueError("球场扩建上限不得低于当前容量，且不得超过 500000")
        if requested_capacity is None:
            target_capacity_values = (
                before_capacity, before_seating, before_used, before_all_seater,
            )
        else:
            target_capacity_values = (target_capacity,) * 4

        target_capacity_raw = struct.pack(
            "<5I", *target_capacity_values, target_expansion,
        )
        target_state = requested_state if requested_state is not None else before_state
        target_pitch_condition = (
            requested_pitch_condition
            if requested_pitch_condition is not None else before_pitch_condition
        )
        target_pitch_type = (
            requested_pitch_type if requested_pitch_type is not None else before_pitch_type
        )
        name_changed = target_name != before_name
        capacity_changed = target_capacity_raw != original_capacity_raw
        state_changed = requested_state is not None and target_state != before_state
        pitch_condition_changed = (
            requested_pitch_condition is not None
            and target_pitch_condition != before_pitch_condition
        )
        pitch_type_changed = (
            requested_pitch_type is not None and target_pitch_type != before_pitch_type
        )
        if not any((name_changed, capacity_changed, state_changed,
                    pitch_condition_changed, pitch_type_changed)):
            raise ValueError("球场数据均未变化")
        try:
            if name_changed and replace_name_pointer:
                new_name_pointer, name_allocation = _allocate_fm_utf8_string(
                    process, bytes(name_target_raw),
                )
            if capacity_changed:
                write_process_memory(process, capacity_address, target_capacity_raw)
            if name_changed:
                if replace_name_pointer:
                    write_process_memory(
                        process, name_field, struct.pack("<Q", new_name_pointer),
                    )
                else:
                    write_process_memory(
                        process, before_name_pointer + 4, bytes(name_write_raw),
                    )
            if state_changed:
                write_process_memory(process, state_address, bytes([int(target_state)]))
            if pitch_condition_changed:
                write_process_memory(
                    process, pitch_condition_address, bytes([int(target_pitch_condition)]),
                )
            if pitch_type_changed:
                write_process_memory(
                    process, pitch_type_address, bytes([int(target_pitch_type)]),
                )
            reader.string_cache.clear()
            if reader.bytes(capacity_address, 20) != target_capacity_raw:
                raise RuntimeError("球场容量写入后校验失败")
            if str(_stadium_name(reader, stadium) or "").rstrip() != target_name:
                raise RuntimeError("球场名称写入后校验失败")
            if state_changed and reader.u8(state_address) != target_state:
                raise RuntimeError("球场状态写入后校验失败")
            if (
                pitch_condition_changed
                and reader.u8(pitch_condition_address) != target_pitch_condition
            ):
                raise RuntimeError("草皮状况写入后校验失败")
            if pitch_type_changed and reader.u8(pitch_type_address) != target_pitch_type:
                raise RuntimeError("草皮类型写入后校验失败")
            if name_changed:
                sleep(0.15)
                reader.string_cache.clear()
                if (
                    reader.ptr(name_field) != (
                        new_name_pointer if replace_name_pointer else before_name_pointer
                    )
                    or reader.bytes(
                        (new_name_pointer if replace_name_pointer else before_name_pointer) + 4,
                        len(name_target_raw),
                    )
                    != name_target_raw
                    or str(_stadium_name(reader, stadium) or "").rstrip() != target_name
                ):
                    raise RuntimeError("球场名称未能保持稳定")
        except Exception as write_error:
            write_process_memory(process, capacity_address, original_capacity_raw)
            if name_changed:
                if replace_name_pointer:
                    write_process_memory(
                        process, name_field, struct.pack("<Q", before_name_pointer),
                    )
                else:
                    write_process_memory(
                        process, before_name_pointer + 4, bytes(name_original_raw),
                    )
            if state_changed and before_state is not None:
                write_process_memory(process, state_address, bytes([int(before_state)]))
            if pitch_condition_changed and before_pitch_condition is not None:
                write_process_memory(
                    process, pitch_condition_address, bytes([int(before_pitch_condition)]),
                )
            if pitch_type_changed and before_pitch_type is not None:
                write_process_memory(
                    process, pitch_type_address, bytes([int(before_pitch_type)]),
                )
            reader.string_cache.clear()
            capacity_restored = reader.bytes(capacity_address, 20) == original_capacity_raw
            name_restored = (
                not name_changed
                or (
                    reader.ptr(name_field) == before_name_pointer
                    and reader.bytes(before_name_pointer + 4, len(name_original_raw))
                    == name_original_raw
                    and _stadium_name(reader, stadium) == before_name
                )
            )
            _free_fm_utf8_string(process, name_allocation)
            state_restored = not state_changed or reader.u8(state_address) == before_state
            pitch_condition_restored = (
                not pitch_condition_changed
                or reader.u8(pitch_condition_address) == before_pitch_condition
            )
            pitch_type_restored = (
                not pitch_type_changed
                or reader.u8(pitch_type_address) == before_pitch_type
            )
            if not all((capacity_restored, name_restored, state_restored,
                        pitch_condition_restored, pitch_type_restored)):
                raise RuntimeError("球场写入失败且原值恢复异常") from write_error
            raise RuntimeError(f"球场写入失败，原值已恢复：{write_error}") from write_error

    patch_club_profile_cache(
        int(team_id), stadium={
            "name": target_name,
            "capacity": target_capacity_values[0],
            "seating_capacity": target_capacity_values[1],
            "used_capacity": target_capacity_values[2],
            "all_seater_capacity": target_capacity_values[3],
            "expansion_capacity": target_expansion,
            "state_raw": target_state,
            "pitch_condition": target_pitch_condition,
            "pitch_type": target_pitch_type,
        },
    )
    return {
        "team_id": int(team_id), "stadium_address": hex(stadium),
        "before": {
            "name": before_name, "capacity": before_capacity,
            "seating_capacity": before_seating, "used_capacity": before_used,
            "all_seater_capacity": before_all_seater,
            "expansion_capacity": before_expansion,
            "state_raw": before_state, "pitch_condition": before_pitch_condition,
            "pitch_type": before_pitch_type,
        },
        "after": {
            "name": target_name, "capacity": target_capacity_values[0],
            "seating_capacity": target_capacity_values[1],
            "used_capacity": target_capacity_values[2],
            "all_seater_capacity": target_capacity_values[3],
            "expansion_capacity": target_expansion,
            "state_raw": target_state, "pitch_condition": target_pitch_condition,
            "pitch_type": target_pitch_type,
        },
    }


def _write_native_club_facility_level(
    reader: Reader, process: Any, layout: Any,
    team_id: int, team_address: Any, facility: str, *,
    expected_level: int | None = None, target_level: int | None = None,
) -> dict[str, Any]:
    field_names = {
        "training": "club_training_facilities_offset",
        "youth": "club_youth_facilities_offset",
        "junior": "club_junior_coaching_offset",
        "recruitment": "club_youth_recruitment_offset",
    }
    facility = str(facility or "").strip().lower()
    if facility not in field_names:
        raise ValueError("不支持提高该项俱乐部设施")
    detail_offset = layout.club_detail2_offset
    field_offset = getattr(layout, field_names[facility], None)
    if detail_offset is None or field_offset is None:
        raise RuntimeError("当前游戏版本尚未映射该俱乐部设施字段")
    team = _resolve_team_address(reader, int(team_id), team_address)
    club = int(reader.ptr(team + TEAM_CLUB) or 0)
    detail = int(reader.ptr(club + int(detail_offset)) or 0) if club else 0
    if (
        not club or not detail
        or reader.ptr(club) != reader.module_base + layout.club_vtable_rva
        or int(reader.u32(club + ENTITY_UID) or 0) != int(team_id)
    ):
        raise RuntimeError("俱乐部详情对象校验失败")
    address = detail + int(field_offset)
    before = reader.u8(address)
    if before is None or not 1 <= int(before) <= 20:
        raise RuntimeError("俱乐部设施原值无效")
    before = int(before)
    if expected_level is not None and before != int(expected_level):
        raise ValueError("俱乐部设施等级已经变化，请刷新后重试")
    if target_level is None:
        if before >= 20:
            raise ValueError("该项俱乐部设施已经达到 20 级")
        after = before + 1
    else:
        after = int(target_level)
        if not 1 <= after <= 20:
            raise ValueError("俱乐部设施目标等级必须处于 1 至 20")
    if after == before:
        return {
            "team_id": int(team_id), "facility": facility,
            "before": before, "after": after,
        }
    try:
        write_process_memory(process, address, bytes([after]))
    except Exception as write_error:
        current = read_process_memory(process, address, 1)
        if current != bytes([before]):
            write_process_memory(process, address, bytes([before]))
        raise RuntimeError(f"俱乐部设施写入失败，原值已恢复：{write_error}") from write_error
    if reader.u8(address) != after:
        write_process_memory(process, address, bytes([before]))
        if reader.u8(address) != before:
            raise RuntimeError("俱乐部设施写入失败且原值恢复异常")
        raise RuntimeError("俱乐部设施写入校验失败，已恢复原值")
    return {
        "team_id": int(team_id), "facility": facility,
        "before": before, "after": after,
    }


def read_native_club_reputation(
    team_id: int, team_address: Any, *, reader: Reader | None = None,
) -> int:
    """Read only the live Team reputation used by brand operations."""
    def read(active_reader: Reader) -> int:
        offset = active_reader.layout.team_reputation_offset
        if offset is None:
            raise RuntimeError("当前游戏版本尚未映射俱乐部声望")
        team = _resolve_team_address(active_reader, int(team_id), team_address)
        value = active_reader.u16(team + int(offset))
        if value is None or not 0 <= int(value) <= 10_000:
            raise RuntimeError("俱乐部声望数据无效")
        return int(value)

    if reader is not None:
        return read(reader)
    with borrow_game_reader() as borrowed:
        return read(borrowed)


def read_native_club_debt_total(
    team_id: int, team_address: Any, *, reader: Reader | None = None,
) -> int:
    """Read only validated live debt records needed for repayment quoting."""
    def read(active_reader: Reader) -> int:
        layout = active_reader.layout
        if (
            layout.club_detail2_offset is None
            or layout.club_loans_offset is None
            or layout.club_debt_record_size != 0x1C
        ):
            raise RuntimeError("当前游戏版本尚未映射俱乐部债务记录")
        try:
            club = int(resolve_team_club(
                active_reader, team_address, int(team_id),
            ).club_address)
        except (OSError, RuntimeError, TypeError, ValueError):
            team = _resolve_team_address(active_reader, int(team_id), team_address)
            club = int(active_reader.ptr(team + TEAM_CLUB) or 0)
        detail = int(active_reader.ptr(
            club + int(layout.club_detail2_offset),
        ) or 0) if club else 0
        if (
            not club or not detail
            or active_reader.ptr(club)
            != active_reader.module_base + int(layout.club_vtable_rva)
        ):
            raise RuntimeError("俱乐部债务对象校验失败")
        header = active_reader.bytes(detail + int(layout.club_loans_offset), 24)
        if not header or len(header) != 24:
            raise RuntimeError("俱乐部债务容器读取失败")
        begin, end, capacity = struct.unpack("<QQQ", header)
        if end < begin or capacity < end or (end - begin) % 8 or end - begin > 8 * 128:
            raise RuntimeError("俱乐部债务容器结构无效")
        pointers = active_reader.bytes(begin, end - begin) if end > begin else b""
        if pointers is None:
            raise RuntimeError("俱乐部债务列表读取失败")
        total = 0
        for offset in range(0, len(pointers), 8):
            address = struct.unpack_from("<Q", pointers, offset)[0]
            raw = active_reader.bytes(address, 0x1C) if address else None
            if not raw or len(raw) != 0x1C:
                raise RuntimeError("俱乐部债务记录校验失败")
            total += max(0, int(struct.unpack_from("<I", raw, 0)[0]))
        return total

    if reader is not None:
        return read(reader)
    with borrow_game_reader() as borrowed:
        return read(borrowed)


def read_native_stadium_action_state(
    team_id: int, team_address: Any, *, reader: Reader | None = None,
) -> dict[str, Any]:
    """Read only stadium and facility fields required by paid club actions."""
    def read(active_reader: Reader) -> dict[str, Any]:
        layout = active_reader.layout
        capacity_offsets = (
            layout.stadium_capacity_offset,
            layout.stadium_seating_capacity_offset,
            layout.stadium_used_capacity_offset,
            layout.stadium_all_seater_capacity_offset,
            layout.stadium_expansion_capacity_offset,
        )
        if any(value is None for value in capacity_offsets):
            raise RuntimeError("当前游戏布局缺少球场容量字段")
        numeric_offsets = tuple(int(value) for value in capacity_offsets)
        if numeric_offsets != tuple(range(numeric_offsets[0], numeric_offsets[0] + 20, 4)):
            raise RuntimeError("当前游戏版本的球场容量布局不连续，已拒绝写入")
        team = _resolve_team_address(active_reader, int(team_id), team_address)
        club = int(active_reader.ptr(team + TEAM_CLUB) or 0)
        stadium = int(active_reader.ptr(team + TEAM_STADIUM) or 0)
        stadium_vtable = int(active_reader.ptr(stadium) or 0) if stadium else 0
        if layout.stadium_vtable_rva is not None:
            stadium_vtable_valid = (
                stadium_vtable
                == active_reader.module_base + int(layout.stadium_vtable_rva)
            )
        else:
            module = getattr(active_reader, "module", None)
            module_size = int(getattr(module, "size", 0) or 0)
            peers = []
            if club and layout.club_training_ground_offset is not None:
                peers.append(active_reader.ptr(
                    club + int(layout.club_training_ground_offset),
                ))
            if stadium and layout.stadium_nearby_offset is not None:
                peers.append(active_reader.ptr(
                    stadium + int(layout.stadium_nearby_offset),
                ))
            stadium_vtable_valid = bool(
                module_size > 0
                and active_reader.module_base <= stadium_vtable
                < active_reader.module_base + module_size
                and any(peer and active_reader.ptr(int(peer)) == stadium_vtable for peer in peers)
            )
        stadium_uid = (
            active_reader.u32(stadium + int(layout.stadium_id_offset))
            if stadium and layout.stadium_id_offset is not None else None
        )
        stadium_uid2 = (
            active_reader.u32(stadium + int(layout.stadium_id2_offset))
            if stadium and layout.stadium_id2_offset is not None else None
        )
        if (
            not club or not stadium
            or active_reader.ptr(club)
            != active_reader.module_base + int(layout.club_vtable_rva)
            or not stadium_vtable_valid
            or not stadium_uid or stadium_uid != stadium_uid2
        ):
            raise RuntimeError("俱乐部或球场对象校验失败")
        capacity_raw = active_reader.bytes(stadium + numeric_offsets[0], 20)
        if not capacity_raw or len(capacity_raw) != 20:
            raise RuntimeError("球场原容量读取失败")
        capacity, seating, used, all_seater, expansion = struct.unpack("<5I", capacity_raw)
        if (
            capacity > 500_000 or seating > capacity or used > capacity
            or all_seater > capacity or expansion < capacity or expansion > 500_000
        ):
            raise RuntimeError("球场原容量关系无效")
        state = {
            "name": _stadium_name(active_reader, stadium),
            "capacity": int(capacity), "seating_capacity": int(seating),
            "used_capacity": int(used), "all_seater_capacity": int(all_seater),
            "expansion_capacity": int(expansion),
            "state_raw": active_reader.u8(stadium + int(layout.stadium_state_offset))
            if layout.stadium_state_offset is not None else None,
            "pitch_condition": active_reader.u8(
                stadium + int(layout.stadium_pitch_condition_offset),
            ) if layout.stadium_pitch_condition_offset is not None else None,
            "pitch_type": active_reader.u8(stadium + int(layout.stadium_pitch_type_offset))
            if layout.stadium_pitch_type_offset is not None else None,
        }
        if not state["name"]:
            raise RuntimeError("球场原名称读取失败")
        return {
            "stadium": state,
            "facilities": read_native_club_facility_levels(
                team_id, team_address, reader=active_reader,
            ),
        }

    if reader is not None:
        return read(reader)
    with borrow_game_reader() as borrowed:
        return read(borrowed)


def read_native_club_facility_levels(
    team_id: int, team_address: Any, *, reader: Reader | None = None,
) -> dict[str, int]:
    """Read only the four facility scalars needed by upgrade planning."""
    def read(active_reader: Reader) -> dict[str, int]:
        layout = active_reader.layout
        detail_offset = layout.club_detail2_offset
        field_names = {
            "training": "club_training_facilities_offset",
            "youth": "club_youth_facilities_offset",
            "junior": "club_junior_coaching_offset",
            "recruitment": "club_youth_recruitment_offset",
        }
        if detail_offset is None or any(
            getattr(layout, field_name, None) is None
            for field_name in field_names.values()
        ):
            raise RuntimeError("当前游戏版本尚未映射俱乐部设施字段")
        team = _resolve_team_address(active_reader, int(team_id), team_address)
        club = int(active_reader.ptr(team + TEAM_CLUB) or 0)
        detail = int(active_reader.ptr(club + int(detail_offset)) or 0) if club else 0
        if (
            not club or not detail
            or active_reader.ptr(club) != active_reader.module_base + layout.club_vtable_rva
            or int(active_reader.u32(club + ENTITY_UID) or 0) != int(team_id)
        ):
            raise RuntimeError("俱乐部详情对象校验失败")
        levels = {
            facility: active_reader.u8(detail + int(getattr(layout, field_name)))
            for facility, field_name in field_names.items()
        }
        if any(value is None or not 1 <= int(value) <= 20 for value in levels.values()):
            raise RuntimeError("俱乐部设施原值无效")
        return {facility: int(value) for facility, value in levels.items()}

    if reader is not None:
        return read(reader)
    with borrow_game_reader() as borrowed:
        return read(borrowed)


def set_native_club_facility_level(
    team_id: int, team_address: Any, facility: str, target_level: int, *,
    expected_level: int | None = None,
) -> dict[str, Any]:
    """Restore one account-scoped facility floor through verified scalar write."""
    pid, _path, layout = select_process_layout()
    with open_process(pid, write_memory=True) as process:
        module = layout.module(process)
        if not module:
            raise RuntimeError(f"{layout.module_name} 尚未加载")
        reader = Reader(process, module.base_address, layout)
        result = _write_native_club_facility_level(
            reader, process, layout, team_id, team_address, facility,
            expected_level=expected_level, target_level=target_level,
        )
    facility_cache_fields = {
        "training": "training", "youth": "youth",
        "junior": "junior_coaching", "recruitment": "youth_recruitment",
    }
    patch_club_profile_cache(
        int(team_id), facilities={facility_cache_fields[str(facility).strip().lower()]: result["after"]},
    )
    return result


def upgrade_native_club_facility(
    team_id: int, team_address: Any, facility: str, *, expected_level: int | None = None,
) -> dict[str, Any]:
    """Raise one verified club facility scalar by one level, up to 20."""
    pid, _path, layout = select_process_layout()
    with open_process(pid, write_memory=True) as process:
        module = layout.module(process)
        if not module:
            raise RuntimeError(f"{layout.module_name} 尚未加载")
        reader = Reader(process, module.base_address, layout)
        result = _write_native_club_facility_level(
            reader, process, layout, team_id, team_address, facility,
            expected_level=expected_level,
        )
    facility_cache_fields = {
        "training": "training", "youth": "youth",
        "junior": "junior_coaching", "recruitment": "youth_recruitment",
    }
    patch_club_profile_cache(
        int(team_id), facilities={
            facility_cache_fields[str(facility).strip().lower()]: result["after"],
        },
    )
    return result


def repay_native_club_debts(
    team_id: int, team_address: Any, *, expected_total: int | None = None,
) -> dict[str, Any]:
    """Zero verified debt amounts after an external full repayment."""
    pid, _path, layout = select_process_layout()
    if (
        layout.club_detail2_offset is None
        or layout.club_loans_offset is None
        or layout.club_debt_record_size != 0x1C
    ):
        raise RuntimeError("当前游戏版本尚未映射俱乐部债务记录")
    with open_process(pid, write_memory=True) as process:
        module = layout.module(process)
        if not module:
            raise RuntimeError(f"{layout.module_name} 尚未加载")
        reader = Reader(process, module.base_address, layout)
        # Resolve the live Team -> Club pair by stable Team UID.  The object
        # directory is needed after save reloads, when the cached address can
        # point at a different object; Club UID is intentionally not required
        # to equal Team UID because reserve/B teams can share a Club object.
        try:
            resolved = resolve_team_club(reader, team_address, int(team_id))
            team = int(resolved.team_address)
            club = int(resolved.club_address)
        except (OSError, RuntimeError, TypeError, ValueError):
            # Keep compatibility with lightweight/older Readers that do not
            # expose the native object directory.  The fallback still checks
            # the Team UID and Club vtable immediately below.
            team = _resolve_team_address(reader, int(team_id), team_address)
            club = int(reader.ptr(team + TEAM_CLUB) or 0)
        detail2 = (
            int(reader.ptr(club + int(layout.club_detail2_offset)) or 0)
            if club else 0
        )
        if (
            not club or not detail2
            or reader.ptr(club) != module.base_address + layout.club_vtable_rva
        ):
            raise RuntimeError("俱乐部债务对象校验失败")
        header = reader.bytes(detail2 + int(layout.club_loans_offset), 24)
        if not header or len(header) != 24:
            raise RuntimeError("俱乐部债务容器读取失败")
        begin, end, capacity = struct.unpack("<QQQ", header)
        if end < begin or capacity < end or (end - begin) % 8 or end - begin > 8 * 128:
            raise RuntimeError("俱乐部债务容器结构无效")
        pointers = reader.bytes(begin, end - begin) if end > begin else b""
        if pointers is None:
            raise RuntimeError("俱乐部债务列表读取失败")
        records: list[tuple[int, bytes]] = []
        total = 0
        for offset in range(0, len(pointers), 8):
            address = struct.unpack_from("<Q", pointers, offset)[0]
            raw = reader.bytes(address, 0x1C) if address else None
            # The source field is an opaque native enum.  Known values are
            # mapped for display, but newer/less common debt types must not be
            # rejected solely because they are not in our name table.
            if not raw or len(raw) != 0x1C:
                raise RuntimeError("俱乐部债务记录校验失败")
            amount = struct.unpack_from("<I", raw, 0x00)[0]
            if amount <= 0:
                continue
            records.append((address, raw))
            total += amount
        if total <= 0:
            raise ValueError("该俱乐部当前没有需要偿还的债务")
        if expected_total is not None and total != int(expected_total):
            raise ValueError("俱乐部负债已经变化，请刷新后重试")
        try:
            for address, raw in records:
                updated = bytearray(raw)
                updated[0x00:0x10] = b"\0" * 0x10
                write_process_memory(process, address, bytes(updated))
            if any(reader.bytes(address, 0x10) != b"\0" * 0x10 for address, _ in records):
                raise RuntimeError("俱乐部债务写后回读不一致")
        except Exception as error:
            for address, raw in records:
                write_process_memory(process, address, raw)
            if any(reader.bytes(address, 0x1C) != raw for address, raw in records):
                raise RuntimeError("俱乐部债务偿还失败且原值恢复异常") from error
            raise RuntimeError(f"俱乐部债务偿还失败，原值已恢复：{error}") from error
    patch_club_profile_cache(int(team_id), debts_repaid=True)
    return {
        "team_id": int(team_id), "before": total, "after": 0,
        "repaid_records": len(records),
    }


def apply_team_reputation_delta(team_id: int, team_address: Any, delta: int) -> dict[str, Any]:
    """Apply a signed club/team reputation change on the verified FM24/FM26 field."""
    delta = int(delta)
    if not delta:
        raise ValueError("俱乐部声望调整值无效")
    pid, _path, layout = select_process_layout()
    if layout.team_reputation_offset is None:
        raise RuntimeError("当前游戏版本尚未映射俱乐部声望")
    with open_process(pid, write_memory=True) as process:
        module = layout.module(process)
        if not module:
            raise RuntimeError(f"{layout.module_name} 尚未加载")
        reader = Reader(process, module.base_address, layout)
        resolved = resolve_team_club(reader, _address(team_address), int(team_id))
        team = int(resolved.team_address)
        if not reader.team(team) or int(reader.u32(team + ENTITY_UID) or 0) != int(team_id):
            raise ValueError("俱乐部对象与球队 ID 不一致")
        address = team + int(layout.team_reputation_offset)
        before = reader.u16(address)
        if before is None or not 0 <= int(before) <= 10000:
            raise RuntimeError("俱乐部声望数据无效")
        after = max(0, min(10000, int(before) + delta))
        original = struct.pack("<H", int(before))
        encoded = struct.pack("<H", after)
        write_process_memory(process, address, encoded)
        if reader.u16(address) != after:
            write_process_memory(process, address, original)
            raise RuntimeError("俱乐部声望写入校验失败")
        return {"team_id": int(team_id), "before": int(before), "after": after, "applied": after - int(before)}


def apply_club_publicity_effect(
    team_id: int, team_address: Any, *, follower_delta: int = 10_000,
    supporter_delta: int = 1,
) -> dict[str, Any]:
    """Increase verified supporter engagement fields with read-back rollback."""
    pid, _path, layout = select_process_layout()
    required = (
        layout.club_detail2_offset,
        layout.club_social_media_followers_offset,
        layout.club_supporters_profile_offset,
    )
    if any(offset is None for offset in required):
        raise RuntimeError("当前游戏版本尚未映射俱乐部球迷关系")
    with open_process(pid, write_memory=True) as process:
        module = layout.module(process)
        if not module:
            raise RuntimeError(f"{layout.module_name} 尚未加载")
        reader = Reader(process, module.base_address, layout)
        resolved = resolve_team_club(reader, _address(team_address), int(team_id))
        team = int(resolved.team_address)
        if not reader.team(team) or int(reader.u32(team + ENTITY_UID) or 0) != int(team_id):
            raise ValueError("俱乐部对象与球队 ID 不一致")
        club = int(resolved.club_address)
        if (
            not club
            or reader.ptr(club) != module.base_address + int(layout.club_vtable_rva)
        ):
            raise ValueError("俱乐部对象校验失败")
        detail2 = int(reader.ptr(club + int(layout.club_detail2_offset)) or 0)
        if not detail2:
            raise RuntimeError("无法读取俱乐部详细资料")
        followers_address = detail2 + int(layout.club_social_media_followers_offset)
        profile_address = detail2 + int(layout.club_supporters_profile_offset)
        followers_before = reader.u32(followers_address)
        profile_before = reader.bytes(profile_address, 6)
        if followers_before is None or not 0 <= int(followers_before) <= 2_000_000_000:
            raise RuntimeError("俱乐部社交媒体关注数无效")
        if not profile_before or len(profile_before) != 6 or any(value > 20 for value in profile_before):
            raise RuntimeError("俱乐部支持者画像无效")
        followers_after = max(0, min(2_000_000_000, int(followers_before) + int(follower_delta)))
        profile_after = bytearray(profile_before)
        profile_after[0] = max(0, min(20, int(profile_after[0]) + int(supporter_delta)))
        profile_after[1] = max(0, min(20, int(profile_after[1]) + int(supporter_delta)))
        try:
            write_process_memory(process, followers_address, struct.pack("<I", followers_after))
            write_process_memory(process, profile_address, bytes(profile_after))
            if reader.u32(followers_address) != followers_after or reader.bytes(profile_address, 6) != bytes(profile_after):
                raise RuntimeError("俱乐部球迷关系写入校验失败")
        except Exception:
            write_process_memory(process, followers_address, struct.pack("<I", int(followers_before)))
            write_process_memory(process, profile_address, profile_before)
            raise
        return {
            "team_id": int(team_id),
            "social_media_followers": {
                "before": int(followers_before), "after": followers_after,
                "applied": followers_after - int(followers_before),
            },
            "supporters": {
                "loyalty_before": int(profile_before[0]), "loyalty_after": int(profile_after[0]),
                "passion_before": int(profile_before[1]), "passion_after": int(profile_after[1]),
            },
            "_restores": (
                (followers_address, struct.pack("<I", int(followers_before))),
                (profile_address, profile_before),
            ),
        }


def restore_club_publicity_effect(result: dict[str, Any]) -> None:
    restores = tuple(result.get("_restores") or ())
    if not restores:
        return
    pid, _path, layout = select_process_layout()
    with open_process(pid, write_memory=True) as process:
        module = layout.module(process)
        if not module:
            raise RuntimeError(f"{layout.module_name} 尚未加载")
        reader = Reader(process, module.base_address, layout)
        for address, raw in restores:
            write_process_memory(process, int(address), bytes(raw))
            if reader.bytes(int(address), len(raw)) != bytes(raw):
                raise RuntimeError("俱乐部球迷关系恢复校验失败")


def apply_player_world_reputation_delta(
    player_id: int, player_address: Any, delta: int,
) -> dict[str, Any]:
    """Apply a signed world-reputation change to a validated player object."""
    delta = int(delta)
    if not delta:
        raise ValueError("球员世界声望调整值无效")
    pid, _path, layout = select_process_layout()
    if layout.player_world_reputation_offset is None:
        raise RuntimeError("当前游戏版本尚未映射球员世界声望")
    with open_process(pid, write_memory=True) as process:
        module = layout.module(process)
        if not module:
            raise RuntimeError(f"{layout.module_name} 尚未加载")
        reader = Reader(process, module.base_address, layout)
        player = _address(player_address)
        player_vtable = reader.ptr(player) if player else 0
        player_rva = player_vtable - module.base_address if player_vtable else 0
        if player_rva in layout.player_and_non_player_vtable_rvas:
            person_offset = layout.player_and_non_player_person_offset
        elif player_rva in layout.actual_player_vtable_rvas:
            person_offset = layout.player_person_offset
        else:
            raise ValueError("球员对象类型无效，请刷新执教球队")
        actual_player_id = reader.u32(player + int(person_offset) + ENTITY_UID)
        if int(actual_player_id or 0) != int(player_id):
            raise ValueError("球员对象与球员 ID 不一致，请刷新执教球队")
        address = player + int(layout.player_world_reputation_offset)
        before = reader.u16(address)
        if before is None or not 0 <= int(before) <= 10000:
            raise RuntimeError("球员世界声望数据无效")
        after = max(0, min(10000, int(before) + delta))
        original = struct.pack("<H", int(before))
        write_process_memory(process, address, struct.pack("<H", after))
        if reader.u16(address) != after:
            write_process_memory(process, address, original)
            raise RuntimeError("球员世界声望写入校验失败")
        return {
            "player_id": int(player_id), "before": int(before),
            "after": after, "applied": after - int(before),
        }


def upgrade_staff_coaching_license(
    staff_id: int, staff_address: Any, team_id: int, team_address: Any,
    expected_before: int, target_code: int,
) -> dict[str, Any]:
    """Promote one validated club coach by exactly one licence level."""
    staff_id = int(staff_id)
    team_id = int(team_id)
    expected_before = int(expected_before)
    target_code = int(target_code)
    if COACHING_LICENSE_NEXT.get(expected_before) != target_code:
        raise ValueError("教练证书只能按顺序提升一级")
    pid, _path, layout = select_process_layout()
    offset = layout.staff_coaching_license_offset
    if offset is None:
        raise RuntimeError("当前游戏版本尚未映射教练证书")
    with open_process(pid, write_memory=True) as process:
        module = layout.module(process)
        if not module:
            raise RuntimeError(f"{layout.module_name} 尚未加载")
        reader = Reader(process, module.base_address, layout)
        person = _address(staff_address)
        team = _address(team_address)
        if (
            not person or not team
            or reader.ptr(person) != module.base_address + int(layout.staff_person_vtable_rva or 0)
            or int(reader.u32(person + ENTITY_UID) or 0) != staff_id
            or int(reader.u32(team + ENTITY_UID) or 0) != team_id
        ):
            raise ValueError("教练或俱乐部对象身份已变化，请刷新执教球队")
        primary_contract_offset = 0xC8 if layout.key == "fm24" else PERSON_CONTRACT
        contract = int(reader.ptr(person + primary_contract_offset) or 0)
        if (
            not contract
            or int(reader.ptr(contract + 0x08) or 0) != person
            or int(reader.ptr(contract + 0x10) or 0) != team
        ):
            raise ValueError("教练合同或所属俱乐部已变化，请刷新执教球队")
        job_type = (
            int(reader.u8(contract + int(layout.staff_job_type_offset)) or 0)
            if layout.staff_job_type_offset is not None else 0
        )
        if job_type not in COACHING_STAFF_JOB_TYPES:
            raise ValueError("所选职员当前不是可参加考证的教练")
        address = person + int(offset)
        before = reader.u8(address)
        if before == target_code:
            return {
                "staff_id": staff_id, "before": expected_before,
                "after": target_code, "already_satisfied": True,
                "license": COACHING_LICENSE_NAMES[target_code],
            }
        if before != expected_before:
            raise ValueError("教练证书已发生变化，请取消课程后重新安排")
        write_process_memory(process, address, bytes([target_code]))
        after = reader.u8(address)
        if after != target_code:
            write_process_memory(process, address, bytes([expected_before]))
            raise RuntimeError("教练证书写后回读失败")
        return {
            "staff_id": staff_id, "before": expected_before,
            "after": target_code, "already_satisfied": False,
            "license": COACHING_LICENSE_NAMES[target_code],
        }


def upgrade_human_manager_coaching_license(
    manager_id: int, manager_address: Any, team_id: int, team_address: Any,
    expected_before: int, target_code: int,
) -> dict[str, Any]:
    """Promote the validated player-manager by exactly one licence level."""
    manager_id = int(manager_id)
    team_id = int(team_id)
    expected_before = int(expected_before)
    target_code = int(target_code)
    if COACHING_LICENSE_NEXT.get(expected_before) != target_code:
        raise ValueError("教练证书只能按顺序提升一级")
    pid, _path, layout = select_process_layout()
    offset = layout.staff_coaching_license_offset
    if offset is None:
        raise RuntimeError("当前游戏版本尚未映射教练证书")
    with open_process(pid, write_memory=True) as process:
        module = layout.module(process)
        if not module:
            raise RuntimeError(f"{layout.module_name} 尚未加载")
        reader = Reader(process, module.base_address, layout)
        manager, person, team = _context_addresses(
            reader, manager_id, team_id, team_address, manager_address,
        )
        team_row = reader.team(team)
        if (
            not team_row
            or str(team_row.get("team_type") or "club") != "club"
            or int(team_row.get("id") or 0) != team_id
            or int(reader.ptr(team + TEAM_MANAGER) or 0) != manager
        ):
            raise ValueError("玩家经理与当前执教俱乐部关系已变化，请重新连接存档")
        address = person + int(offset)
        before = reader.u8(address)
        if before == target_code:
            return {
                "manager_id": manager_id, "before": expected_before,
                "after": target_code, "already_satisfied": True,
                "license": COACHING_LICENSE_NAMES[target_code],
            }
        if before != expected_before:
            raise ValueError("玩家经理证书已发生变化，请取消课程后重新安排")
        write_process_memory(process, address, bytes([target_code]))
        after = reader.u8(address)
        if after != target_code:
            write_process_memory(process, address, bytes([expected_before]))
            raise RuntimeError("玩家经理证书写后回读失败")
        return {
            "manager_id": manager_id, "before": expected_before,
            "after": target_code, "already_satisfied": False,
            "license": COACHING_LICENSE_NAMES[target_code],
        }


def _read_managed_club_squads(
    reader: Reader, *, first_team_address: int, first_team: dict[str, Any],
    first_team_players: list[dict[str, Any]], game_date: date | None,
    manager_person: int,
) -> list[dict[str, Any]]:
    """Expand the verified first team and its sibling youth/reserve Teams."""
    if str(first_team.get("team_type") or "club") != "club":
        return []
    try:
        from tools.world_clubs import _related_club_squads

        related = _related_club_squads(reader, int(first_team_address))
    except (OSError, RuntimeError, TypeError, ValueError):
        related = [(int(first_team_address), first_team)]

    squads: list[dict[str, Any]] = []
    for squad_address, squad_team in related:
        squad_address = int(squad_address)
        if squad_address == int(first_team_address):
            squad_players = first_team_players
        else:
            squad_roster = reader.roster(squad_address)
            _prefetch_roster_dependencies(reader, squad_roster)
            squad_players = []
            for roster_summary in squad_roster:
                try:
                    player = read_roster_player_profile(
                        reader, roster_summary, squad_address,
                        game_date, manager_person,
                    )
                except (OSError, RuntimeError, TypeError, ValueError):
                    player = None
                if player:
                    squad_players.append(player)
            attach_retirement_details(reader, squad_players)
            if reader.layout.key != "fm24":
                _apply_localized_player_names(reader, squad_players)
            else:
                for player in squad_players:
                    player.pop("_name_signature", None)

        type_code = squad_team.get("squad_type_code")
        label = (
            squad_team.get("squad_type_name")
            or ("一线队" if squad_address == int(first_team_address) else f"球队 {squad_team.get('id')}")
        )
        for player in squad_players:
            player.update({
                "squad_team_id": int(squad_team.get("id") or 0),
                "squad_team_address": hex(squad_address),
                "squad_type_code": type_code,
                "squad_type": label,
                "squad_label": label,
            })
        squads.append({
            "team_id": int(squad_team.get("id") or 0),
            "team_address": hex(squad_address),
            "type_code": type_code,
            "type": label,
            "label": label,
            "player_count": len(squad_players),
            "players": squad_players,
        })
    return squads


def read_club_profile(
    manager_id: int, team_id: int, *, force: bool = False,
    team_address: Any = 0, manager_address: Any = 0,
    partial_callback: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    started = perf_counter()
    timings: dict[str, float] = {}
    with borrow_game_reader() as reader:
        pid = int(reader.process.pid)
        layout = reader.layout
        directory = database_index_for_reader(reader)
        directory_token = (
            directory.table_token("team") if directory is not None else None
        )
        key = (
            pid, int(manager_id), int(team_id), directory_token,
            int(getattr(reader, "session_generation", 0) or 0),
        )
        with _CACHE_LOCK:
            cached = _CACHE.get(key)
            if not force and cached and monotonic() - cached[0] < 60:
                return deepcopy(cached[1])
        manager, person, resolved_team_address = _context_addresses(
            reader, int(manager_id), int(team_id), team_address, manager_address,
        )
        timings["context_ms"] = round((perf_counter() - started) * 1000, 1)
        _contract_team, contract = _contract(reader, person, resolved_team_address)
        team = reader.team(resolved_team_address) or {}
        if contract and team.get("team_type") == "national":
            contract["contract_type"] = "国家队合同"
        game_date = decode_date(
            reader.u32(reader.module_base + layout.game_date_rva) or 0
        ) if layout.game_date_rva is not None else None
        if contract:
            expiry = date.fromisoformat(contract["expiry_date"]) if contract.get("expiry_date") else None
            contract["active"] = bool(game_date and (not expiry or game_date <= expiry))
        club_information = None
        roster = reader.roster(resolved_team_address)
        _prefetch_roster_dependencies(reader, roster)
        if layout.key == "fm24":
            from tools.person_relationships import read_person_relationships

            players = []
            for row in roster:
                address = int(str(row["address"]), 16)
                player_person = address + (
                    layout.player_and_non_player_person_offset
                    if row.get("object_type") == "actual_player_and_non_player"
                    else layout.player_person_offset
                )
                position_ratings = {
                    str(name): int(value)
                    for name, value in dict(row.get("positions") or {}).items()
                }
                position_values = bytes(
                    max(0, min(255, int(position_ratings.get(name, 0))))
                    for name in POSITION_NAMES
                )
                highest = max(position_ratings.values(), default=0)
                raw_attributes = reader.bytes(
                    address + layout.player_attributes_offset, 54,
                ) or b""
                visible_attributes = _visible_player_attributes(
                    reader, address, raw_attributes,
                    goalkeeper=bool(
                        highest
                        and position_ratings.get(POSITION_NAMES[0]) == highest
                    ),
                )
                try:
                    training_ca = _player_training_ca_snapshot(
                        layout, raw_attributes, position_values, visible_attributes,
                    )
                except (TypeError, ValueError):
                    training_ca = {"model": None, "verified": False, "costs": {}}
                foot_offset = 0x27 - 0x0F
                foot_raw = raw_attributes[foot_offset:foot_offset + 2]
                preferred_foot = None
                if len(foot_raw) == 2:
                    left, right = foot_raw
                    preferred_foot = (
                        "双足" if min(left, right) >= 75
                        else "左脚" if left > right else "右脚"
                    )
                nation_id, nationality, nationality_code = (
                    _person_nationality(reader, player_person)
                    if player_person else (None, None, None)
                )
                birth = _person_birth_date(reader, player_person) if player_person else None
                relationship = (
                    read_person_relationships(reader, player_person, person)
                    if player_person else {}
                )
                fitness_raw = row.get("fitness_raw")
                sharpness_raw = row.get("sharpness_raw")
                fatigue_raw = row.get("fatigue_raw")
                morale_raw = row.get("morale_raw")
                home_reputation = (
                    reader.u16(address + layout.player_home_reputation_offset)
                    if layout.player_home_reputation_offset is not None else None
                )
                current_reputation = (
                    reader.u16(address + layout.player_current_reputation_offset)
                    if layout.player_current_reputation_offset is not None else None
                )
                world_reputation = (
                    reader.u16(address + layout.player_world_reputation_offset)
                    if layout.player_world_reputation_offset is not None else None
                )
                players.append({
                    "id": int(row["id"]), "legacy_id": row.get("legacy_id"),
                    "name": row["name"], "address": row["address"],
                    "object_type": row.get("object_type"),
                    "shirt_number": None,
                    "height_cm": (
                        reader.u8(address + layout.player_height_offset)
                        if layout.player_height_offset is not None else None
                    ),
                    "weight_kg": (
                        reader.u8(address + layout.player_weight_offset)
                        if layout.player_weight_offset is not None else None
                    ),
                    "nationality_id": nation_id, "nationality": nationality,
                    "nationality_code": nationality_code,
                    "date_of_birth": birth.isoformat() if birth else None,
                    "age": _age_on(birth, game_date),
                    "positions": list(position_ratings), "position_ratings": position_ratings,
                    "primary_positions": [
                        name for name, value in position_ratings.items() if value == highest
                    ],
                    "attributes": visible_attributes,
                    "training_ca": training_ca,
                    "preferred_foot": preferred_foot,
                    "home_reputation": home_reputation,
                    "current_reputation": current_reputation,
                    "world_reputation": world_reputation,
                    "international_reputation": world_reputation, "pa": row.get("pa"),
                    "hidden_attributes": (
                        _player_hidden_attributes(
                            reader, address, player_person, raw_attributes,
                        )
                        if player_person else {}
                    ),
                    "manager_intimacy": int(
                        relationship.get("manager_intimacy")
                        or (_manager_intimacy(reader, player_person, person) if player_person else 0)
                    ),
                    "manager_relation_reason": relationship.get("manager_relation_reason"),
                    "manager_relation_permanence": relationship.get("manager_relation_permanence"),
                    "manager_relation_object_type": relationship.get("manager_relation_object_type"),
                    "manager_relation_type": relationship.get("manager_relation_type"),
                    "has_unhappiness": (
                        _safe_person_has_unhappiness(reader, player_person, game_date)
                        if player_person else None
                    ),
                    "ca": int(row["ca"]),
                    "fitness": round(fitness_raw / 100, 2) if fitness_raw is not None else None,
                    "sharpness": round(sharpness_raw / 100, 2) if sharpness_raw is not None else None,
                    "fatigue": fatigue_raw, "morale": morale_raw,
                    "availability": row.get("availability"),
                })
                players[-1].update(read_fm24_extended_player(
                    reader, address, player_person, resolved_team_address, game_date,
                ))
                players[-1]["career_status"] = {
                    "is_player_staff": row.get("object_type") == "actual_player_and_non_player",
                }
        else:
            players = []
            for roster_player in roster:
                player = _player(
                    reader, int(roster_player["address"], 16), game_date,
                    manager_person=person, roster_summary=roster_player,
                )
                if not player:
                    continue
                player["availability"] = roster_player.get("availability")
                player_address = int(str(player["address"]), 16)
                player_person = player_address + (
                    layout.player_and_non_player_person_offset
                    if roster_player.get("object_type") == "actual_player_and_non_player"
                    else layout.player_person_offset
                )
                player.update(read_fm26_extended_player(
                    reader, player_address, player_person, resolved_team_address,
                    game_date, expected_uid=int(player.get("id") or 0),
                ))
                player["has_unhappiness"] = _safe_person_has_unhappiness(
                    reader, player_person, game_date,
                )
                players.append(player)
        attach_retirement_details(reader, players)
        phase_started = perf_counter()
        if layout.key != "fm24":
            _apply_localized_player_names(reader, players)
        timings["player_names_ms"] = round((perf_counter() - phase_started) * 1000, 1)
        squads = (
            [{
                "team_id": int(team_id),
                "team_address": hex(resolved_team_address),
                "type_code": team.get("squad_type_code"),
                "type": team.get("squad_type_name") or "一线队",
                "label": team.get("squad_type_name") or "一线队",
                "player_count": len(players),
                "players": players,
            }]
            if team.get("team_type") == "club" else []
        )
        if partial_callback is not None:
            partial_callback({
                "game_date": game_date.isoformat() if game_date else None,
                "team": {
                    "id": int(team_id),
                    "name": team.get("short_name") or team.get("name") or str(team_id),
                    "team_type": team.get("team_type") or "club",
                    "reputation": team.get("reputation"),
                    "address": hex(resolved_team_address),
                    "club_address": hex(reader.ptr(resolved_team_address + 0x30) or 0),
                },
                "manager": {"id": int(manager_id)},
                "contract": contract,
                "players": players,
                "squads": squads,
                "staff": [],
                "club_information": club_information,
                "performance": dict(timings),
                "profile_stage": "players",
            })
        phase_started = perf_counter()
        if team.get("team_type") == "club":
            squads = _read_managed_club_squads(
                reader,
                first_team_address=resolved_team_address,
                first_team=team,
                first_team_players=players,
                game_date=game_date,
                manager_person=person,
            )
        timings["squad_players_ms"] = round(
            (perf_counter() - phase_started) * 1000, 1,
        )
        phase_started = perf_counter()
        club_information = (
            _club_information(reader, resolved_team_address)
            if team.get("team_type") != "national" else None
        )
        timings["club_info_ms"] = round((perf_counter() - phase_started) * 1000, 1)
        phase_started = perf_counter()
        staff = _scan_staff(
            reader, resolved_team_address, int(manager_id),
            manager_person=person,
        )
        timings["staff_scan_ms"] = round((perf_counter() - phase_started) * 1000, 1)
        phase_started = perf_counter()
        if layout.key != "fm24":
            _apply_localized_player_names(reader, staff)
        else:
            for row in staff:
                row.pop("_name_signature", None)
        if club_information is not None:
            chairman = _club_controller_staff(staff)
            club_information["chairman"] = (
                {
                    "id": int(chairman.get("id") or 0),
                    "name": chairman.get("name"),
                    "role": chairman.get("role") or "主席",
                    "attributes": chairman.get("chairman_attributes"),
                }
                if chairman else None
            )
            director = next(
                (
                    row for row in staff
                    if row.get("role") == "足球总监" or int(row.get("job_type") or 0) == 10
                ),
                None,
            )
            club_information["director_of_football"] = (
                {
                    "id": int(director.get("id") or 0),
                    "name": director.get("name"),
                    "role": director.get("role") or "足球总监",
                }
                if director else None
            )
        timings["staff_names_ms"] = round((perf_counter() - phase_started) * 1000, 1)
        # Do not run the transient staff-report UI sweep on later refreshes.
        # Names that cannot be obtained from FM's stable person-name pools stay
        # in their existing English form; this keeps every refresh predictable.
        profile = {
            "game_date": game_date.isoformat() if game_date else None,
            "team": {
                "id": int(team_id), "name": team.get("short_name") or team.get("name") or str(team_id),
                "team_type": team.get("team_type") or "club",
                "reputation": team.get("reputation"),
                "address": hex(resolved_team_address), "club_address": hex(reader.ptr(resolved_team_address + 0x30) or 0),
            },
            "manager": {"id": int(manager_id)},
            "contract": contract, "players": players, "staff": staff,
            "squads": squads,
            "club_information": club_information,
            "performance": timings,
            "profile_stage": "complete",
        }
        timings.update(reader.read_metrics())
        timings["total_ms"] = round((perf_counter() - started) * 1000, 1)
    with _CACHE_LOCK:
        # A manager may control a club and a national team at the same time.
        # Retain both profiles instead of evicting the first one when the second
        # team is read during startup.
        _CACHE[key] = (monotonic(), deepcopy(profile))
        _prune_timed_cache_locked(
            _CACHE, ttl=120, maximum=12, current_pid=pid,
        )
    return profile
