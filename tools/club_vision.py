from __future__ import annotations

import struct
from contextlib import nullcontext
from typing import Any

from fm_collector.win32 import open_process, write_process_memory
from tools.database_index import (
    DatabaseIndex,
    database_index_for_reader,
    resolve_team_club,
)
from tools.initial_data_audit import (
    ENTITY_UID, NATION_NAMES, Reader, select_process_layout,
)
from tools.game_layout import FM24_EPIC_EXE_SHA256


CLUB_VISION_TYPE_NAMES: dict[int, str] = {
    1: "赢得赛事",
    2: "获得赛事资格",
    3: "升级到指定赛事",
    4: "成为其他球队中的佼佼者",
    5: "争夺赛事冠军",
    6: "进入附加赛",
    7: "取得联赛上半区排名",
    8: "成为稳定参赛球队",
    9: "挑战赛事目标",
    10: "进入赛事后期阶段",
    11: "至少进入淘汰赛",
    12: "进入赛事正赛",
    13: "赢得赛事",
    14: "挑战赛事目标",
    15: "留在该项赛事",
    16: "直接升级到指定赛事",
    33: "在该项赛事保持竞争力",
    37: "避免在赛事中垫底",
    38: "取得赛事前指定名次",
    40: "实现财政自给自足",
    41: "修复俱乐部财政状况",
    42: "提高商业收入",
    43: "签下年轻球员并培养获利",
    44: "遵守工资预算",
    45: "签下球员并出售获利",
    46: "使用原始转会预算",
    60: "成为本国最具声望的球队",
    61: "成为本洲最具声望的球队",
    62: "成为世界最具声望的球队",
    63: "提升俱乐部声望",
    64: "持续提升俱乐部声望",
    80: "在转会市场实现收支平衡",
    81: "为超过指定年龄的球员最多提供一年合同",
    82: "为超过指定年龄的球员最多提供两年合同",
    83: "为超过指定年龄的球员最多提供三年合同",
    84: "为一线队球员提供至少指定年限的合同",
    85: "先出售球员再引援",
    86: "只签下巴斯克球员",
    87: "只签下非洲球员",
    88: "不签下非本国球员",
    89: "不签下非本国门将",
    91: "只签下会说威尔士语的球员",
    92: "不得签下球员",
    100: "签下指定国籍球员",
    101: "签下效力于指定国家的球员",
    102: "从国内低级别联赛引援",
    103: "签下高声望球员",
    104: "为一线队签下低于指定年龄的球员",
    105: "为未来签下低于指定年龄的球员",
    106: "从国内竞争对手引援",
    107: "不签下超过指定年龄的球员",
    120: "使用俱乐部青训体系培养球员",
    121: "建立国内最佳青训体系",
    122: "建立本级别最佳青训体系",
    123: "建立世界最佳青训体系",
    124: "为母队培养球员",
    125: "给予一线队球员出场时间",
    150: "踢攻势足球",
    151: "踢防守稳固的足球",
    152: "踢控球足球",
    153: "踢直接足球",
    154: "充分利用定位球",
    155: "踢赏心悦目的足球",
    156: "踢防守反击足球",
    157: "踢高节奏压迫足球",
    170: "建造新球场",
    171: "扩建球场",
    172: "购买当前球场",
    184: "联赛排名高于指定俱乐部",
}

COMPETITION_TYPES = frozenset({1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 33})
CUP_COMPETITION_TYPES = frozenset({1, 2, 5, 10, 11, 12, 13, 14})
AGE_TYPES = frozenset({81, 82, 83, 104, 105, 107})
MINIMUM_YEAR_TYPES = frozenset({84})
VALUE_TYPES = AGE_TYPES | MINIMUM_YEAR_TYPES
SIMPLE_TYPES = frozenset({
    40, 41, 42, 43, 44, 45, 46, 62, 63, 64, 80, 85, 86, 87, 88, 89,
    91, 92, 102, 103, 106, 120, 125, 150, 151, 152, 153, 154, 155, 156,
    157, 170, 171, 172,
})

# These types have no external reference field but their semantics are still
# hard-coded to a specific language/region by FM, so they are not generic
# creation templates for an arbitrary acquired club.
CREATABLE_SIMPLE_TYPES = SIMPLE_TYPES.difference({86, 88, 91})
CREATABLE_VALUE_TYPES = VALUE_TYPES

BOARD_SOURCE_TYPE = 1
NATION_REFERENCE_TYPE = 9
NATIONALITY_REFERENCE_TYPE = 10
NATIONALITY_TARGET_TYPE = 100
BASED_IN_NATION_TARGET_TYPE = 101
NATIONALITY_REFERENCE_TARGET_TYPE = 88
COUNTRY_REFERENCE_TARGET_TYPES = frozenset({
    NATIONALITY_REFERENCE_TARGET_TYPE, NATIONALITY_TARGET_TYPE,
})
COUNTRY_REFERENCE_TYPES = {
    NATIONALITY_REFERENCE_TARGET_TYPE: NATIONALITY_REFERENCE_TYPE,
    NATIONALITY_TARGET_TYPE: NATION_REFERENCE_TYPE,
}


def _custom_nation_target_layout_supported(layout: Any) -> bool:
    distribution = str(getattr(layout, "distribution", "")).lower()
    if str(getattr(layout, "key", "")) != "fm24":
        return False
    if distribution == "steam":
        return True
    return bool(
        distribution == "epic"
        and str(getattr(layout, "executable_sha256", "") or "").upper()
        == FM24_EPIC_EXE_SHA256
    )


COUNTRY_DISPLAY_REFERENCE_TYPES = {
    NATIONALITY_REFERENCE_TARGET_TYPE: frozenset({NATIONALITY_REFERENCE_TYPE}),
    NATIONALITY_TARGET_TYPE: frozenset({
        NATION_REFERENCE_TYPE, NATIONALITY_REFERENCE_TYPE,
    }),
    BASED_IN_NATION_TARGET_TYPE: frozenset({
        NATION_REFERENCE_TYPE, NATIONALITY_REFERENCE_TYPE,
    }),
}
BOARD_IMPORTANCE_OPTIONS = (
    (2, "锦上添花"),
    (6, "有点在意"),
    (8, "十分渴望"),
    (10, "不容有失"),
)
BOARD_IMPORTANCE_VALUES = frozenset(value for value, _label in BOARD_IMPORTANCE_OPTIONS)
# These recognized types are not public board objectives.  63/64 are
# supporter-facing reputation goals; 30/184/185 are separately excluded by
# research evidence.  Types absent from CLUB_VISION_TYPE_NAMES are also hidden
# by public_club_vision instead of exposing an unexplained numeric fallback.
HIDDEN_TARGET_TYPES = frozenset({30, 63, 64, 184, 185})
NATION_DISPLAY_NAMES = {
    "中华人民共和国": "中国",
}


def _address(value: Any) -> int:
    if isinstance(value, int):
        return value
    try:
        return int(str(value or "0"), 0)
    except (TypeError, ValueError):
        return 0


def _type_options(row: dict[str, Any]) -> list[dict[str, Any]]:
    culture_type = int(row.get("type") or 0)
    reference_type = int(row.get("reference_type") if row.get("reference_type") is not None else -1)
    value_raw = int(row.get("value_raw") if row.get("value_raw") is not None else -1)
    if reference_type == 25 and culture_type in COMPETITION_TYPES:
        reference_name = str(row.get("reference_name") or "").casefold()
        allowed = (
            CUP_COMPETITION_TYPES
            if any(marker in reference_name for marker in ("杯", "cup"))
            else COMPETITION_TYPES
        )
    elif reference_type == -1 and culture_type in AGE_TYPES:
        allowed = AGE_TYPES
    elif reference_type == -1 and culture_type in MINIMUM_YEAR_TYPES:
        allowed = MINIMUM_YEAR_TYPES
    elif reference_type == -1 and value_raw == -1 and culture_type in SIMPLE_TYPES:
        allowed = SIMPLE_TYPES
    else:
        allowed = frozenset({culture_type}) if culture_type else frozenset()
    return [
        {"value": value, "label": CLUB_VISION_TYPE_NAMES.get(value, f"目标 {value}")}
        for value in sorted(allowed)
    ]


def _public_importance_options() -> list[dict[str, Any]]:
    return [
        {"value": value, "label": label}
        for value, label in BOARD_IMPORTANCE_OPTIONS
    ]


def _competition_target_label(label: str, competition_name: str) -> str:
    for token in ("指定赛事", "该项赛事", "赛事", "联赛"):
        if token in label:
            return label.replace(token, competition_name)
    return f"{label}（{competition_name}）"


def _country_target_label(culture_type: int, country_name: str) -> str:
    if culture_type == NATIONALITY_REFERENCE_TARGET_TYPE:
        return f"不签下非{country_name}国籍的球员"
    if culture_type == NATIONALITY_TARGET_TYPE:
        return f"签下{country_name}籍球员"
    if culture_type == BASED_IN_NATION_TARGET_TYPE:
        return f"签下效力于{country_name}的球员"
    return CLUB_VISION_TYPE_NAMES.get(culture_type, f"目标 {culture_type}")


def _target_category(culture_type: int) -> str:
    if culture_type <= 38:
        return "赛事"
    if culture_type in {40, 41, 42, 43, 44, 45, 46, 80}:
        return "财政经营"
    if 60 <= culture_type <= 64:
        return "俱乐部声望"
    if 81 <= culture_type <= 107:
        return "合同与引援"
    if 120 <= culture_type <= 125:
        return "青训培养"
    if 150 <= culture_type <= 157:
        return "比赛风格"
    if 170 <= culture_type <= 172:
        return "球场建设"
    return "其他"


def _target_parameter(culture_type: int) -> str | None:
    if culture_type in COUNTRY_REFERENCE_TARGET_TYPES:
        return "nation"
    if culture_type in AGE_TYPES:
        return "age"
    if culture_type in MINIMUM_YEAR_TYPES:
        return "years"
    return None


def _public_target_catalog(public_nations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    creatable = CREATABLE_SIMPLE_TYPES | CREATABLE_VALUE_TYPES | (
        COUNTRY_REFERENCE_TARGET_TYPES if public_nations else set()
    )
    return [
        {
            "value": value,
            "label": label,
            "category": _target_category(value),
            "available": value in creatable,
            "parameter": _target_parameter(value),
        }
        for value, label in sorted(CLUB_VISION_TYPE_NAMES.items())
        if value not in HIDDEN_TARGET_TYPES
    ]


def _nation_reference_rows(reader: Reader) -> list[dict[str, Any]]:
    module = getattr(reader, "module", None) or reader.layout.module(reader.process)
    if not module:
        raise RuntimeError("当前游戏模块尚未加载")
    if getattr(reader, "module", None) is None:
        reader.module = module
    directory = database_index_for_reader(reader)
    if directory is None:
        directory = DatabaseIndex(reader)
    token_before = directory.table_token("nation")
    addresses = directory.addresses("nation")
    snapshots = reader._fixed_size_snapshots(addresses, 0x10)
    module_start = int(reader.module_base)
    module_end = module_start + int(module.size)
    rows: list[dict[str, Any]] = []
    seen_uids: set[int] = set()
    seen_item_ids: set[int] = set()
    for address in addresses:
        raw = snapshots.get(int(address))
        if not raw or len(raw) < 0x10:
            continue
        vtable = int(struct.unpack_from("<Q", raw, 0)[0])
        item_id = int(struct.unpack_from("<H", raw, 0x08)[0])
        nation_uid = int(struct.unpack_from("<I", raw, ENTITY_UID)[0])
        raw_name = NATION_NAMES.get(nation_uid)
        name = NATION_DISPLAY_NAMES.get(str(raw_name or ""), raw_name)
        if (
            not module_start <= vtable < module_end
            or not 0 < item_id <= 0x7FFF
            or nation_uid <= 0
            or not name
        ):
            continue
        if nation_uid in seen_uids or item_id in seen_item_ids:
            raise RuntimeError("国家数据库中的愿景引用 ID 不唯一")
        seen_uids.add(nation_uid)
        seen_item_ids.add(item_id)
        rows.append({
            "uid": nation_uid,
            "item_id": item_id,
            "name": str(name),
            "address": int(address),
        })
    if directory.table_token("nation") != token_before:
        raise RuntimeError("国家数据库在读取过程中发生变化，请重试")
    if not rows:
        raise RuntimeError("没有读取到可用于董事会目标的国家")
    return sorted(rows, key=lambda row: (str(row["name"]), int(row["uid"])))


def club_vision_nation_options() -> list[dict[str, Any]]:
    try:
        from tools.game_session import borrow_game_reader
        with borrow_game_reader() as reader:
            return [
                {key: row[key] for key in ("uid", "item_id", "name")}
                for row in _nation_reference_rows(reader)
            ]
    except (OSError, RuntimeError, TypeError, ValueError):
        return []


def club_vision_reference_names(culture: Any) -> dict[str, str]:
    requested: dict[int, set[int]] = {}
    for row in culture if isinstance(culture, list) else []:
        if not isinstance(row, dict) or row.get("reference_id") is None:
            continue
        reference_type = int(row.get("reference_type") or -1)
        reference_id = int(row.get("reference_id") or -1)
        if reference_type in {3, NATION_REFERENCE_TYPE, NATIONALITY_REFERENCE_TYPE, 25} and reference_id >= 0:
            requested.setdefault(reference_type, set()).add(reference_id)
    if not requested:
        return {}
    try:
        from tools.game_session import borrow_game_reader
        with borrow_game_reader() as reader:
            module = getattr(reader, "module", None) or reader.layout.module(reader.process)
            directory = database_index_for_reader(reader) or DatabaseIndex(reader)
            module_start = int(reader.module_base)
            module_end = module_start + int(module.size)
            resolved: dict[str, str] = {}
            for row in _nation_reference_rows(reader):
                item_id = int(row["item_id"])
                for reference_type in (NATION_REFERENCE_TYPE, NATIONALITY_REFERENCE_TYPE):
                    if item_id in requested.get(reference_type, set()):
                        resolved[f"{reference_type}:{item_id}"] = str(row["name"])
            for table_name, reference_type in (("competition", 25), ("club", 3)):
                wanted = requested.get(reference_type, set())
                if not wanted:
                    continue
                token_before = directory.table_token(table_name)
                addresses = directory.addresses(table_name)
                snapshots = reader._fixed_size_snapshots(addresses, 0x10)
                matches: dict[int, tuple[int, int]] = {}
                for address in addresses:
                    raw = snapshots.get(int(address))
                    if not raw or len(raw) < 0x10:
                        continue
                    vtable = int(struct.unpack_from("<Q", raw, 0)[0])
                    item_id = int(struct.unpack_from("<H", raw, 0x08)[0])
                    uid = int(struct.unpack_from("<I", raw, ENTITY_UID)[0])
                    if item_id in wanted and uid > 0 and module_start <= vtable < module_end:
                        matches[item_id] = (int(address), uid)
                if directory.table_token(table_name) != token_before:
                    raise RuntimeError(f"{table_name} 数据库在读取过程中发生变化")
                for item_id, (address, uid) in matches.items():
                    if reference_type == 25:
                        name = str((reader.competition(address) or {}).get("name") or "")
                    else:
                        name = ""
                        for team_address in directory.addresses_for_uid("team", uid):
                            team = reader.team(team_address)
                            if (
                                team and int(reader.ptr(team_address + 0x30) or 0) == address
                                and int(team.get("id") or 0) == uid
                            ):
                                name = str(team.get("name") or team.get("short_name") or "")
                                break
                    if name:
                        resolved[f"{reference_type}:{item_id}"] = name
            return resolved
    except (OSError, RuntimeError, TypeError, ValueError):
        return {}


def _resolve_nation_reference(reader: Reader, nation_uid: Any) -> dict[str, Any]:
    target_uid = int(nation_uid or 0)
    if target_uid <= 0:
        raise ValueError("请选择目标球员国籍")
    matches = [
        row for row in _nation_reference_rows(reader)
        if int(row["uid"]) == target_uid
    ]
    if len(matches) != 1:
        raise ValueError("所选国家已经失效，请刷新后重试")
    return matches[0]


def _resolve_club_nation_reference(
    reader: Reader, club_address: int,
) -> dict[str, Any]:
    offset = getattr(reader.layout, "club_nation_offset", None)
    if offset is None:
        raise RuntimeError("当前游戏版本尚未定位俱乐部所属国家")
    nation_address = int(reader.ptr(int(club_address) + int(offset)) or 0)
    nation_uid = int(reader.u32(nation_address + ENTITY_UID) or 0) if nation_address else 0
    matches = [
        row for row in _nation_reference_rows(reader)
        if int(row["uid"]) == nation_uid
        and int(row["address"]) == nation_address
    ]
    if len(matches) != 1:
        raise RuntimeError("无法验证俱乐部所属国家，不能新增本国球员目标")
    return matches[0]


def public_club_vision(
    culture: Any, nation_options: list[dict[str, Any]] | None = None,
    reference_names: dict[str, str] | None = None,
) -> dict[str, Any]:
    public_nations = [
        {key: row[key] for key in ("uid", "item_id", "name") if key in row}
        for row in (nation_options or []) if isinstance(row, dict)
    ]
    if culture is None:
        return {
            "available": False,
            "editable": False,
            "mode": "manage_board_targets",
            "create_options": [],
            "catalog_options": _public_target_catalog(public_nations),
            "importance_options": _public_importance_options(),
            "nation_options": public_nations,
            "items": [],
            "error": "当前俱乐部没有可读取的愿景记录，或当前版本尚未定位愿景布局",
        }
    rows = []
    ignored_items = 0
    for index, value in enumerate(culture if isinstance(culture, list) else []):
        if not isinstance(value, dict):
            continue
        row = dict(value)
        source_type = int(row.get("source_type") or 0)
        culture_type = int(row.get("type") or 0)
        if (
            source_type == 2
            or culture_type in HIDDEN_TARGET_TYPES
            or culture_type not in CLUB_VISION_TYPE_NAMES
        ):
            ignored_items += 1
            continue
        row["index"] = index
        row["type_name"] = CLUB_VISION_TYPE_NAMES.get(culture_type, row.get("type_name"))
        reference_key = f"{int(row.get('reference_type') or -1)}:{int(row.get('reference_id') if row.get('reference_id') is not None else -1)}"
        reference_name = str((reference_names or {}).get(reference_key) or "")
        if reference_name:
            row["reference_name"] = reference_name
            if culture_type == 184:
                row["type_name"] = f"联赛排名高于{reference_name}"
            elif int(row.get("reference_type") or -1) == 25 and row.get("type_name"):
                row["type_name"] = _competition_target_label(
                    str(row["type_name"]), reference_name,
                )
        display_reference_types = COUNTRY_DISPLAY_REFERENCE_TYPES.get(culture_type)
        if display_reference_types and (
            int(row.get("reference_type") or -1) in display_reference_types
        ):
            reference = next((
                nation for nation in public_nations
                if int(nation.get("item_id") or -1) == int(row.get("reference_id") or -1)
            ), None)
            country_name = str((reference or {}).get("name") or reference_name)
            if country_name:
                row["reference_name"] = country_name
                row["type_name"] = _country_target_label(
                    culture_type, country_name,
                )
        if culture_type == 38 and int(row.get("value_raw") or -1) >= 0:
            row["type_name"] = f"取得赛事前 {int(row['value_raw']) + 1} 名"
        row["type_options"] = _type_options(row)
        row["editable"] = bool(
            row["type_options"] and _address(row.get("address"))
            and int(row.get("source_type") or 0) == BOARD_SOURCE_TYPE
        )
        row["deletable"] = bool(
            _address(row.get("address"))
            and source_type != 2
        )
        row["value_editable"] = bool(
            row["editable"] and culture_type in VALUE_TYPES
            and int(row.get("reference_type") or -1) == -1
        )
        rows.append(row)
    return {
        "available": True,
        "editable": any(row["editable"] for row in rows),
        "mode": "manage_board_targets",
        "create_options": [
            {
                "value": value,
                "label": CLUB_VISION_TYPE_NAMES.get(value, f"目标 {value}"),
                "parameter": _target_parameter(value),
            }
            for value in sorted(
                CREATABLE_SIMPLE_TYPES | CREATABLE_VALUE_TYPES
                | (COUNTRY_REFERENCE_TARGET_TYPES if public_nations else set())
            )
            if value in CLUB_VISION_TYPE_NAMES and value not in HIDDEN_TARGET_TYPES
        ],
        "catalog_options": _public_target_catalog(public_nations),
        "importance_options": _public_importance_options(),
        "nation_options": public_nations,
        "ignored_non_board_items": ignored_items,
        "items": rows,
        "error": None,
    }


def _locate_records(
    reader: Reader, team_address: int, team_id: int,
) -> tuple[int, dict[int, bytes]]:
    club, _root, _begin, _end, _capacity, _pointers, records = (
        _locate_vector(reader, team_address, team_id)
    )
    return club, records


def _prepare_team_club_resolver(reader: Reader) -> None:
    """Make the stable-UID directory available to a one-off reader.

    Most current readers come from ``GameReadSession`` and already expose the
    session-owned database-index provider.  The vision write path historically
    opened its own short-lived ``Reader`` directly, though, so keep that path
    compatible while still giving it the same FMRTE-style UID lookup.  Building
    the directory is lazy at the table level and is retained on this reader for
    the remainder of the operation; if the module or AOB is unavailable the
    resolver below still validates the caller's address as its legacy fallback.
    """
    if callable(getattr(reader, "database_index_provider", None)):
        return
    module = getattr(reader, "module", None)
    if module is None:
        try:
            module = reader.layout.module(reader.process)
        except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
            module = None
        if module is not None:
            reader.module = module
    if module is None:
        return
    try:
        directory = DatabaseIndex(reader)
    except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
        return
    reader.database_index_provider = lambda **_kwargs: directory


def _read_vector_header(
    reader: Reader, header_address: int,
) -> tuple[int, int, int]:
    header = reader.bytes(header_address, 24)
    if not header or len(header) != 24:
        raise RuntimeError("俱乐部愿景列表读取失败")
    begin, end, capacity = struct.unpack("<QQQ", header)
    if end < begin or capacity < end or (end - begin) % 8 or end - begin > 8 * 256:
        raise RuntimeError("俱乐部愿景列表结构校验失败")
    return begin, end, capacity


def _locate_vector(
    reader: Reader, team_address: int, team_id: int,
) -> tuple[int, int, int, int, int, list[int], dict[int, bytes]]:
    layout = reader.layout
    required = (
        layout.team_details_offset,
        layout.team_details_club_vision_offset,
        layout.club_vision_culture_offset,
        layout.club_culture_record_size,
        layout.club_vtable_rva,
    )
    if any(value is None for value in required):
        raise RuntimeError("当前游戏版本尚未定位俱乐部愿景布局")
    _prepare_team_club_resolver(reader)
    try:
        resolved = resolve_team_club(reader, team_address, team_id)
        resolved_team_address = int(resolved.team_address)
        resolved_club_address = int(resolved.club_address)
    except (OSError, RuntimeError, TypeError, ValueError) as resolver_error:
        # Preserve the legacy short-lived Reader path when it has a valid
        # Team wrapper but no readable UID directory.  The old checks remain
        # strict; this is only an address-source fallback.
        try:
            legacy_address = int(str(team_address or "0"), 0)
        except (TypeError, ValueError):
            legacy_address = 0
        team = reader.team(legacy_address) if legacy_address else None
        if (
            not team or team.get("team_type") != "club"
            or int(team.get("id") or 0) != int(team_id)
            or int(reader.u32(legacy_address + ENTITY_UID) or 0) != int(team_id)
        ):
            raise RuntimeError("目标俱乐部已变化，请刷新后重试") from resolver_error
        resolved_team_address = legacy_address
        resolved_club_address = int(reader.ptr(legacy_address + 0x30) or 0)
        expected_club_vtable = reader.module_base + int(layout.club_vtable_rva)
        if (
            not resolved_club_address
            or int(reader.ptr(resolved_club_address) or 0) != expected_club_vtable
        ):
            raise RuntimeError("目标俱乐部对象校验失败") from resolver_error
    team = reader.team(resolved_team_address) if resolved_team_address else None
    if (
        not team or team.get("team_type") != "club"
        or int(team.get("id") or 0) != int(team_id)
        or int(reader.u32(resolved_team_address + ENTITY_UID) or 0) != int(team_id)
    ):
        raise RuntimeError("目标俱乐部已变化，请刷新后重试")
    club = resolved_club_address
    expected_club_vtable = reader.module_base + int(layout.club_vtable_rva)
    if not club or int(reader.ptr(club) or 0) != expected_club_vtable:
        raise RuntimeError("目标俱乐部对象校验失败")
    details = int(reader.ptr(resolved_team_address + int(layout.team_details_offset)) or 0)
    vision = int(reader.ptr(details + int(layout.team_details_club_vision_offset)) or 0)
    if not vision:
        raise RuntimeError("俱乐部愿景所属对象校验失败")

    # Populated FM24 clubs normally point to an external vector header at
    # ClubVision+0x78.  Acquired clubs with no native objectives can expose
    # an inline ClubVision node instead: its vtable is at +0x00, the empty
    # vector header starts at +0x08, and the owning Team is at +0x40.
    root = int(reader.ptr(vision + int(layout.club_vision_culture_offset)) or 0)
    header_address = 0
    if root and int(reader.ptr(root + 0x30) or 0) == club:
        header_address = root
    elif int(reader.ptr(vision + 0x40) or 0) == resolved_team_address:
        header_address = vision + 0x08
    else:
        raise RuntimeError("俱乐部愿景所属对象校验失败")
    begin, end, capacity = _read_vector_header(reader, header_address)
    pointer_data = reader.bytes(begin, end - begin) if end > begin else b""
    if pointer_data is None:
        raise RuntimeError("俱乐部愿景列表读取失败")
    pointers = [
        int(struct.unpack_from("<Q", pointer_data, offset)[0])
        for offset in range(0, len(pointer_data), 8)
    ]
    size = int(layout.club_culture_record_size)
    records: dict[int, bytes] = {}
    for offset in range(0, len(pointer_data), 8):
        address = int(struct.unpack_from("<Q", pointer_data, offset)[0])
        raw = reader.bytes(address, size) if address else None
        if not raw or len(raw) != size:
            raise RuntimeError("俱乐部愿景记录读取失败")
        importance = int(raw[0x2D])
        if not int(raw[0x2C]):
            raise RuntimeError("俱乐部愿景记录结构校验失败")
        # FM24 retains inactive competition objectives with importance 0.
        # Preserve their vector entries, but never expose them as writable
        # records. Other out-of-range values remain a hard failure.
        if importance == 0:
            continue
        if not 1 <= importance <= 10:
            raise RuntimeError("俱乐部愿景记录结构校验失败")
        records[address] = raw
    return club, header_address, begin, end, capacity, pointers, records


def _allocate_native_block(process: Any, size: int) -> tuple[int, int]:
    # Imported lazily because club_reader also consumes the public vision names.
    from tools.club_reader import _remote_malloc_block
    return _remote_malloc_block(process, size)


def _free_native_block(process: Any, free_address: int, address: int) -> None:
    from tools.club_reader import _remote_free_block
    _remote_free_block(process, free_address, address)


def _build_native_unary_stub(
    function: int, argument: int, result_address: int,
    context_address: int, rtl_restore: int,
) -> bytes:
    from tools.player_movement import _mov_rax, _mov_rcx, _store_absolute_qword
    code = bytearray(b"\x48\x83\xec\x28")
    code += _mov_rcx(argument) + _mov_rax(function) + b"\xff\xd0"
    code += _store_absolute_qword(result_address, 1)
    code += b"\x48\x83\xc4\x28" + _mov_rcx(context_address)
    code += b"\x31\xd2" + _mov_rax(rtl_restore) + b"\xff\xe0"
    return bytes(code)


def _native_unary_call(process: Any, function: int, argument: int) -> None:
    """Run one verified Club Culture constructor/initializer on FM's logic thread."""
    import ctypes
    import time

    from fm_collector.win32 import find_module, kernel32, read_process_memory
    from tools.player_movement import (
        CONTEXT64, MEM_COMMIT_RESERVE, MEM_RELEASE, PAGE_EXECUTE_READWRITE,
        REMOTE_BLOCK_SIZE, REMOTE_CONTEXT_OFFSET, REMOTE_RESULT_OFFSET,
        REMOTE_STACK_TOP_OFFSET, _configure_thread_api, _get_context,
        _logic_thread, _open_thread,
    )

    _configure_thread_api()
    ntdll_remote = find_module(process, "ntdll.dll")
    ntdll_local = ctypes.WinDLL("ntdll", use_last_error=True)
    if not ntdll_remote:
        raise RuntimeError("无法定位 FM 原生调用恢复入口")
    rtl_local = int(
        ctypes.cast(ntdll_local.RtlRestoreContext, ctypes.c_void_p).value or 0
    )
    rtl_restore = ntdll_remote.base_address + rtl_local - int(ntdll_local._handle)
    thread_id, idle_context = _logic_thread(process)
    cave = int(kernel32.VirtualAllocEx(
        process.handle, None, REMOTE_BLOCK_SIZE,
        MEM_COMMIT_RESERVE, PAGE_EXECUTE_READWRITE,
    ) or 0)
    if not cave:
        raise RuntimeError("无法建立 FM 原生调用区")
    result_address = cave + REMOTE_RESULT_OFFSET
    context_address = cave + REMOTE_CONTEXT_OFFSET
    stack = ((cave + REMOTE_STACK_TOP_OFFSET) & ~0xF) - 8
    code = _build_native_unary_stub(
        function, argument, result_address, context_address, rtl_restore,
    )
    thread = 0
    completed = False
    try:
        write_process_memory(process, cave, code)
        write_process_memory(process, result_address, b"\0" * 8)
        kernel32.FlushInstructionCache(
            process.handle, ctypes.c_void_p(cave), len(code),
        )
        thread = _open_thread(thread_id)
        if not thread or kernel32.SuspendThread(thread) == 0xFFFFFFFF:
            raise RuntimeError("FM 游戏逻辑线程当前无法暂停，请稍后重试")
        suspended = True
        try:
            original = _get_context(thread)
            if (
                int(original.Rip) != int(idle_context.Rip)
                or int(original.Rsp) != int(idle_context.Rsp)
            ):
                raise RuntimeError("FM 游戏逻辑线程刚刚恢复工作，请稍后重试")
            write_process_memory(process, context_address, bytes(original))
            hijacked = CONTEXT64.from_buffer_copy(bytes(original))
            hijacked.Rip = cave
            hijacked.Rsp = stack
            write_process_memory(process, stack, b"\0" * 8)
            if not kernel32.SetThreadContext(thread, ctypes.byref(hijacked)):
                raise OSError(ctypes.get_last_error(), "SetThreadContext failed")
        finally:
            if suspended:
                kernel32.ResumeThread(thread)
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            raw = read_process_memory(process, result_address, 8) or b""
            if len(raw) == 8 and struct.unpack("<Q", raw)[0] == 1:
                completed = True
                break
            time.sleep(0.01)
        if not completed:
            raise RuntimeError("FM 原生调用超时，请暂时不要重复操作")
    finally:
        if thread:
            kernel32.CloseHandle(thread)
        if completed:
            kernel32.VirtualFreeEx(
                process.handle, ctypes.c_void_p(cave), 0, MEM_RELEASE,
            )


def _native_culture_layout_ready(reader: Reader) -> tuple[int, int, int | None]:
    layout = reader.layout
    values = (
        getattr(layout, "club_culture_vtable_rva", None),
        getattr(layout, "club_culture_constructor_rva", None),
        getattr(layout, "club_culture_initialize_rva", None),
    )
    if values[0] is None or values[1] is None:
        raise RuntimeError("当前游戏版本尚未定位董事会目标原生构造器")
    return (
        reader.module_base + int(values[0]),
        reader.module_base + int(values[1]),
        reader.module_base + int(values[2]) if values[2] is not None else None,
    )


def _assert_expected_record(current: dict[str, int], expected: dict[str, Any]) -> None:
    for key in (
        "type", "importance", "source_type", "value_raw",
        "reference_id", "reference_type",
    ):
        if key not in expected or int(expected[key]) != current[key]:
            raise RuntimeError("愿景记录刚刚发生变化，请刷新后重试")


def _record_state(raw: bytes) -> dict[str, int]:
    return {
        "type": int(raw[0x2C]),
        "importance": int(raw[0x2D]),
        "source_type": int(raw[0x30]),
        "value_raw": int(struct.unpack_from("<i", raw, 0x28)[0]),
        "reference_id": int(struct.unpack_from("<h", raw, 0x08)[0]),
        "reference_type": int(struct.unpack_from("<b", raw, 0x0B)[0]),
    }


def _checked_value(culture_type: int, value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise ValueError("请填写目标年龄或合同年限") from None
    if culture_type == 84:
        if not 1 <= parsed <= 10:
            raise ValueError("最短合同年限必须为 1 至 10 年")
    elif not 15 <= parsed <= 99:
        raise ValueError("目标年龄必须为 15 至 99 岁")
    return parsed


def _creation_template_address(
    pointers: list[int], states: dict[int, dict[str, int]],
    target_type: int, target_reference_type: int,
) -> int:
    def first_matching(predicate: Any) -> int:
        return next((
            address for address in pointers
            if states[address]["source_type"] == BOARD_SOURCE_TYPE
            and predicate(states[address])
        ), 0)

    if target_type in COUNTRY_REFERENCE_TARGET_TYPES:
        exact = first_matching(
            lambda state: state["reference_type"] == target_reference_type,
        )
        if exact:
            return exact
        country_reference = first_matching(
            lambda state: state["reference_type"] in {
                NATION_REFERENCE_TYPE, NATIONALITY_REFERENCE_TYPE,
            },
        )
        if country_reference:
            return country_reference
    if target_type in CREATABLE_VALUE_TYPES:
        value_template = first_matching(
            lambda state: state["reference_type"] == -1
            and state["type"] in VALUE_TYPES,
        )
        if value_template:
            return value_template
    simple_template = first_matching(
        lambda state: state["type"] in SIMPLE_TYPES
        and state["reference_type"] == -1
        and state["value_raw"] == -1,
    )
    if simple_template:
        return simple_template
    # Some clubs legitimately expose only competition- or nation-referenced
    # board targets.  Those records still carry the same native Club Culture
    # object layout; create_club_vision_record overwrites the verified
    # business fields and clears the reference/value slots before insertion.
    # Limit this fallback to reference kinds that the reader resolves and
    # validates elsewhere; unknown native shapes remain fail-closed.
    return first_matching(
        lambda state: state["reference_type"] in {
            3, NATION_REFERENCE_TYPE, NATIONALITY_REFERENCE_TYPE, 25,
        },
    )


def _find_external_creation_template(
    reader: Reader, target_team_id: int,
) -> tuple[int, bytes] | None:
    """Borrow one verified board record when an acquired club has no goals."""
    try:
        directory = database_index_for_reader(reader) or DatabaseIndex(reader)
        if not callable(getattr(reader, "database_index_provider", None)):
            reader.database_index_provider = lambda **_kwargs: directory
        team_addresses = directory.addresses("team")[:256]
    except (OSError, RuntimeError, TypeError, ValueError):
        return None
    for candidate_team in team_addresses:
        try:
            candidate = reader.team(int(candidate_team))
            candidate_id = int((candidate or {}).get("id") or 0)
            if candidate_id <= 0 or candidate_id == int(target_team_id):
                continue
            _club, _root, _begin, _end, _capacity, _pointers, records = (
                _locate_vector(reader, int(candidate_team), candidate_id)
            )
        except (OSError, RuntimeError, TypeError, ValueError):
            continue
        states = {address: _record_state(raw) for address, raw in records.items()}
        address = _creation_template_address(list(records), states, 157, -1)
        if address and address in records:
            return address, records[address]
    return None


def update_club_vision_record(
    team_address: Any, team_id: int, record_address: Any, *,
    expected: dict[str, Any], updates: dict[str, Any], operation: Any = None,
) -> dict[str, Any]:
    team_id = int(team_id)
    team_address_int = _address(team_address)
    record_address_int = _address(record_address)
    if team_id <= 0 or not team_address_int or not record_address_int:
        raise ValueError("俱乐部愿景目标无效，请刷新后重试")
    if operation is None:
        pid, _path, layout = select_process_layout()
        process_context = open_process(pid, write_memory=True)
    else:
        layout = operation.reader.layout
        process_context = nullcontext(operation.process)
    with process_context as process:
        module = layout.module(process) if operation is None else operation.module
        if not module:
            raise RuntimeError(f"{layout.module_name} 尚未加载")
        reader = Reader(process, module.base_address, layout) if operation is None else operation.reader
        _club, records = _locate_records(reader, team_address_int, team_id)
        original = records.get(record_address_int)
        if original is None:
            raise RuntimeError("愿景记录已变化，请刷新后重试")
        current = _record_state(original)
        _assert_expected_record(current, expected)
        if current["source_type"] != BOARD_SOURCE_TYPE:
            raise ValueError("只能修改董事会目标")
        target_type = int(updates.get("type", current["type"]))
        target_importance = int(updates.get("importance", current["importance"]))
        target_source = int(updates.get("source_type", current["source_type"]))
        if target_importance not in BOARD_IMPORTANCE_VALUES:
            raise ValueError("目标重要性只能选择锦上添花、有点在意、十分渴望或不容有失")
        if target_source != BOARD_SOURCE_TYPE:
            raise ValueError("目标来源固定为董事会")

        if current["reference_type"] == 25 and current["type"] in COMPETITION_TYPES:
            allowed_types = COMPETITION_TYPES
        elif current["reference_type"] == -1 and current["type"] in AGE_TYPES:
            allowed_types = AGE_TYPES
        elif current["reference_type"] == -1 and current["type"] in MINIMUM_YEAR_TYPES:
            allowed_types = MINIMUM_YEAR_TYPES
        elif (
            current["reference_type"] == -1 and current["value_raw"] == -1
            and current["type"] in SIMPLE_TYPES
        ):
            allowed_types = SIMPLE_TYPES
        else:
            allowed_types = frozenset({current["type"]})
        if target_type not in allowed_types:
            raise ValueError("所选目标与当前记录结构不兼容")

        target_value = current["value_raw"]
        if target_type in VALUE_TYPES and current["reference_type"] == -1:
            target_value = _checked_value(target_type, updates.get("value", target_value))

        updated = bytearray(original)
        updated[0x2C] = target_type
        updated[0x2D] = target_importance
        updated[0x30] = target_source
        if target_value != current["value_raw"]:
            struct.pack_into("<i", updated, 0x28, target_value)
        encoded = bytes(updated)
        if encoded == original:
            return {
                "changed": False,
                "team_id": team_id,
                "record_address": hex(record_address_int),
                **current,
                "type_name": CLUB_VISION_TYPE_NAMES.get(current["type"]),
            }

        field_address = record_address_int + 0x28
        original_fields = original[0x28:0x31]
        encoded_fields = encoded[0x28:0x31]
        wrote = False
        try:
            write_process_memory(process, field_address, encoded_fields)
            wrote = True
            written = reader.bytes(record_address_int, len(original))
            if written != encoded:
                raise RuntimeError("俱乐部愿景写入后校验失败")
        except Exception:
            if wrote:
                write_process_memory(process, field_address, original_fields)
                if reader.bytes(record_address_int, len(original)) != original:
                    raise RuntimeError("俱乐部愿景写入失败，且原记录恢复失败")
            raise
        state = _record_state(encoded)
        return {
            "changed": True,
            "team_id": team_id,
            "record_address": hex(record_address_int),
            **state,
            "type_name": CLUB_VISION_TYPE_NAMES.get(state["type"]),
        }


def create_club_vision_record(
    team_address: Any, team_id: int, *, culture_type: Any, importance: Any,
    nation_id: Any = None, value: Any = None, operation: Any = None,
) -> dict[str, Any]:
    team_id = int(team_id)
    team_address_int = _address(team_address)
    target_type = int(culture_type)
    target_importance = int(importance)
    if team_id <= 0 or not team_address_int:
        raise ValueError("俱乐部愿景目标无效，请刷新后重试")
    if (
        target_type not in (
            CREATABLE_SIMPLE_TYPES | CREATABLE_VALUE_TYPES
            | COUNTRY_REFERENCE_TARGET_TYPES
        )
        or target_type not in CLUB_VISION_TYPE_NAMES
    ):
        raise ValueError("新增目标类型尚未通过结构验证")
    if target_importance not in BOARD_IMPORTANCE_VALUES:
        raise ValueError("目标重要性只能选择锦上添花、有点在意、十分渴望或不容有失")

    if operation is None:
        pid, _path, layout = select_process_layout()
        process_context = open_process(pid, write_memory=True, create_thread=True)
    else:
        layout = operation.reader.layout
        process_context = nullcontext(operation.process)
    with process_context as process:
        module = layout.module(process) if operation is None else operation.module
        if not module:
            raise RuntimeError(f"{layout.module_name} 尚未加载")
        # Keep the request-local fresh Reader and session-owned UID directory.
        # Rebuilding a standalone directory here repeats the AOB/table scans
        # that the current connected session has already performed.
        reader = Reader(process, module.base_address, layout) if operation is None else operation.reader
        reader.module = module
        (
            club, root, begin, end, capacity, pointers, records,
        ) = _locate_vector(reader, team_address_int, team_id)
        states = {address: _record_state(raw) for address, raw in records.items()}
        nation_reference = None
        if target_type == NATIONALITY_REFERENCE_TARGET_TYPE:
            if nation_id is None or int(nation_id or 0) <= 0:
                # Preserve the legacy API behavior for callers that do not
                # provide a country, while the UI now always sends one.
                nation_reference = _resolve_club_nation_reference(reader, club)
            else:
                if not _custom_nation_target_layout_supported(layout):
                    raise RuntimeError(
                        "当前版本尚未验证类型88的自定义国家目标"
                    )
                nation_reference = _resolve_nation_reference(reader, nation_id)
        elif target_type == NATIONALITY_TARGET_TYPE:
            nation_reference = _resolve_nation_reference(reader, nation_id)
        target_reference_type = COUNTRY_REFERENCE_TYPES.get(target_type, -1)
        target_value = (
            _checked_value(target_type, value)
            if target_type in CREATABLE_VALUE_TYPES else -1
        )
        if any(
            state["type"] == target_type
            and state["source_type"] == BOARD_SOURCE_TYPE
            and (
                target_type not in COUNTRY_REFERENCE_TARGET_TYPES
                or (
                    state["reference_type"] == target_reference_type
                    and state["reference_id"] == int(nation_reference["item_id"])
                )
            )
            for state in states.values()
        ):
            raise ValueError("该俱乐部已经有相同的董事会目标")
        template_address = _creation_template_address(
            pointers, states, target_type, target_reference_type,
        )
        template_raw = records.get(template_address) if template_address else None
        if not template_raw and not records:
            external_template = _find_external_creation_template(reader, team_id)
            if external_template:
                template_address, template_raw = external_template
        if not template_address:
            raise RuntimeError("该俱乐部没有可验证的董事会目标模板，无法安全新增")
        culture_vtable, culture_constructor, culture_initialize = (
            _native_culture_layout_ready(reader)
        )

        original_header = struct.pack("<QQQ", begin, end, capacity)
        record = record_free = vector = vector_free = 0
        active_vector = begin
        appended_in_place = capacity - end >= 8
        original_tail = reader.bytes(end, 8) if appended_in_place else None
        swapped = False
        try:
            record, record_free = _allocate_native_block(
                process, int(layout.club_culture_record_size),
            )
            encoded = bytearray(template_raw or records[template_address])
            reference_id = -1
            reference_type = -1
            if nation_reference:
                reference_id = int(nation_reference["item_id"])
                reference_type = target_reference_type
            struct.pack_into("<h", encoded, 0x08, reference_id)
            encoded[0x0A] = 0
            struct.pack_into("<b", encoded, 0x0B, reference_type)
            struct.pack_into("<i", encoded, 0x28, target_value)
            encoded[0x2C] = target_type
            encoded[0x2D] = target_importance
            encoded[0x30] = BOARD_SOURCE_TYPE
            # A copied 0x40-byte object bypasses the native construction and
            # reference-lifecycle work associated with its +0x10 state.  Build
            # a real object first, then copy only verified business fields.
            _native_unary_call(process, culture_constructor, record)
            if culture_vtable and int(reader.ptr(record) or 0) != culture_vtable:
                raise RuntimeError("董事会目标原生构造器返回了错误的对象类型")
            write_process_memory(process, record + 0x08, bytes(encoded[0x08:0x0C]))
            business = bytearray(encoded[0x20:0x40])
            if culture_initialize is not None:
                # FM24's deserializer performs this normalization and marks
                # the object as a live Club Culture before vector insertion.
                business[0x3D - 0x20] |= 0x04
            write_process_memory(process, record + 0x20, bytes(business))
            if culture_initialize is not None:
                _native_unary_call(process, culture_initialize, record)

            if appended_in_place:
                write_process_memory(process, end, struct.pack("<Q", record))
                new_end = end + 8
            else:
                vector, vector_free = _allocate_native_block(
                    process, (len(pointers) + 1) * 8,
                )
                active_vector = vector
                pointer_data = struct.pack(
                    "<" + "Q" * (len(pointers) + 1), *(pointers + [record]),
                )
                write_process_memory(process, vector, pointer_data)
                new_end = vector + len(pointer_data)
                capacity = new_end
            write_process_memory(
                process, root,
                struct.pack("<QQQ", active_vector, new_end, capacity),
            )
            swapped = True
            _owner, _root, _begin, _end, _capacity, _pointers, after = (
                _locate_vector(reader, team_address_int, team_id)
            )
            written = after.get(record)
            if (
                len(after) != len(records) + 1
                or not written
                or (culture_vtable and int(reader.ptr(record) or 0) != culture_vtable)
            ):
                raise RuntimeError("新增董事会目标写入后校验失败")
            state = _record_state(written)
            expected_state = {
                "type": target_type,
                "importance": target_importance,
                "source_type": BOARD_SOURCE_TYPE,
                "value_raw": target_value,
                "reference_id": reference_id,
                "reference_type": reference_type,
            }
            if state != expected_state:
                raise RuntimeError("新增董事会目标写入后校验失败")
            created_record_address = record
            # The active allocations now belong to the live FM object graph.
            record = vector = 0
            result = {
                "changed": True, "operation": "create", "team_id": team_id,
                "record_address": hex(created_record_address),
                **state,
                "type_name": CLUB_VISION_TYPE_NAMES.get(target_type),
            }
            if nation_reference:
                result["reference_uid"] = int(nation_reference["uid"])
                result["reference_name"] = str(nation_reference["name"])
                result["type_name"] = (
                    f"不签下非{nation_reference['name']}国籍的球员"
                    if target_type == NATIONALITY_REFERENCE_TARGET_TYPE
                    else f"签下{nation_reference['name']}籍球员"
                )
            return result
        except Exception:
            write_process_memory(process, root, original_header)
            if appended_in_place and original_tail is not None:
                write_process_memory(process, end, original_tail)
            restored = reader.bytes(root, 24) == original_header
            if restored:
                if vector:
                    _free_native_block(process, vector_free, vector)
                if record:
                    _free_native_block(process, record_free, record)
            else:
                record = vector = 0
                raise RuntimeError("新增目标失败，且原愿景列表恢复失败")
            raise


def delete_club_vision_record(
    team_address: Any, team_id: int, record_address: Any, *,
    expected: dict[str, Any],
) -> dict[str, Any]:
    team_id = int(team_id)
    team_address_int = _address(team_address)
    record_address_int = _address(record_address)
    if team_id <= 0 or not team_address_int or not record_address_int:
        raise ValueError("俱乐部愿景目标无效，请刷新后重试")
    if not isinstance(expected, dict):
        raise ValueError("愿景删除参数不完整")
    pid, _path, layout = select_process_layout()
    with open_process(pid, write_memory=True) as process:
        module = layout.module(process)
        if not module:
            raise RuntimeError(f"{layout.module_name} 尚未加载")
        reader = Reader(process, module.base_address, layout)
        (
            _club, root, begin, end, capacity, pointers, records,
        ) = _locate_vector(reader, team_address_int, team_id)
        original = records.get(record_address_int)
        if original is None or record_address_int not in pointers:
            raise RuntimeError("愿景记录已变化，请刷新后重试")
        current = _record_state(original)
        _assert_expected_record(current, expected)
        if current["source_type"] == 2:
            raise ValueError("只能删除董事会目标")
        original_header = struct.pack("<QQQ", begin, end, capacity)
        original_vector = reader.bytes(begin, end - begin) if end > begin else b""
        if original_vector is None:
            raise RuntimeError("俱乐部愿景列表读取失败")
        remaining = [value for value in pointers if value != record_address_int]
        if len(remaining) != len(pointers) - 1:
            raise RuntimeError("愿景记录指针不唯一，拒绝删除")
        compact = (
            struct.pack("<" + "Q" * len(remaining), *remaining)
            if remaining else b""
        )
        wrote_vector = False
        try:
            if compact:
                write_process_memory(process, begin, compact)
                wrote_vector = True
            write_process_memory(
                process, root,
                struct.pack("<QQQ", begin, begin + len(compact), capacity),
            )
            _owner, _root, _begin, _end, _capacity, _pointers, after = (
                _locate_vector(reader, team_address_int, team_id)
            )
            if len(after) != len(records) - 1 or record_address_int in after:
                raise RuntimeError("删除董事会目标写入后校验失败")
        except Exception:
            if original_vector:
                write_process_memory(process, begin, original_vector)
            write_process_memory(process, root, original_header)
            if (
                reader.bytes(root, 24) != original_header
                or (original_vector and reader.bytes(begin, len(original_vector)) != original_vector)
            ):
                raise RuntimeError("删除目标失败，且原愿景列表恢复失败")
            raise
        return {
            "changed": True, "operation": "delete", "team_id": team_id,
            "record_address": hex(record_address_int), **current,
            "type_name": CLUB_VISION_TYPE_NAMES.get(current["type"]),
        }


__all__ = [
    "BOARD_IMPORTANCE_OPTIONS", "CLUB_VISION_TYPE_NAMES",
    "club_vision_nation_options", "club_vision_reference_names",
    "create_club_vision_record",
    "delete_club_vision_record", "public_club_vision",
    "update_club_vision_record",
]
