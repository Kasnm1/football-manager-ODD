from __future__ import annotations

from functools import lru_cache
from pathlib import Path
import re
import struct
from typing import Any

from fm_collector.win32 import open_process, write_process_memory
from tools.domain_errors import ValidationError
from tools.initial_data_audit import ENTITY_UID, Reader, select_process_layout


FM26_PREFERRED_MOVES_OFFSET = 0x348
FM24_PREFERRED_MOVES_OFFSET = 0x368
PREFERRED_MOVE_COUNT = 64
FM26_PREFERRED_MOVE_CONFLICT_TABLE_RVA = 0x4597A80
FM26_PREFERRED_MOVE_CONFLICT_RULE_COUNT = 41
_DISPLAY_SUFFIXES = ("[%male#1-hidden]", "[%person#1-hidden]")


class PreferredMoveStateError(ValidationError):
    """A stable, locale-independent preferred-move state conflict."""

    def __init__(self, operation: str) -> None:
        normalized = str(operation or "")
        if normalized == "learn":
            code = "preferred_move_already_known"
            message = "球员已经拥有该个人习惯"
            message_key = "training.preferred_move.already_known"
        elif normalized == "unlearn":
            code = "preferred_move_not_known"
            message = "球员没有该个人习惯"
            message_key = "training.preferred_move.not_known"
        else:
            raise ValueError("个人习惯训练操作无效")
        super().__init__(
            message,
            code=code,
            phase="validate_state",
            details={"operation": normalized, "already_satisfied": True},
            message_key=message_key,
        )
        # Player-effect diagnostics use these compatibility attributes.
        self.error_code = code
        self.error_phase = "validate_state"
        self.already_satisfied = True
FM24_VERIFIED_PREFERRED_MOVES = {
    0: "沿左路带球突进",
    1: "沿右路带球突进",
    2: "从中路带球突进",
    3: "插入对方禁区",
    4: "移动到空当",
    5: "有机会就前插",
    6: "选择简单传球",
    7: "经常尝试传身后球",
    8: "喜欢远射",
    9: "大力射门",
    10: "追求射门角度",
    11: "尝试弧线球",
    12: "喜欢过掉门将",
    13: "喜欢反越位",
    14: "用外脚背",
    15: "贴身防守",
    16: "激怒对手",
    17: "与裁判争论",
    18: "背身拿球",
    19: "回撤拿球",
    20: "撞墙式配合",
    21: "喜欢过顶球吊射",
    22: "控制节奏",
    23: "尝试倒钩球",
    24: "乐意把球传给位置更好的队友，而不是选择射门",
    25: "不喜欢传身后球",
    26: "停球观察",
    27: "趟球变向加速过人",
    28: "在盘带前先将球停至右脚",
    29: "在盘带前先将球停至左脚",
    30: "长时间控球",
    31: "后排插上进攻",
    32: "利用脚下技术将球带出危险区",
    33: "很少前插",
    34: "避免使用弱势脚",
    35: "尝试花哨动作",
    36: "主罚远距离任意球",
    37: "倒地铲球",
    38: "不喜欢倒地铲球",
    39: "喜欢从双侧内切",
    40: "拉边",
    41: "鼓动观众情绪",
    42: "第一时间射门",
    43: "长距离传球",
    44: "喜欢接脚下球",
    45: "罚任意球时选择大力射门",
    46: "喜欢连续过多人",
    47: "转移球到另一侧",
    48: "会在巅峰期退役",
    49: "尽可能长时间踢球",
    50: "大力平直界外球",
    51: "经常带球",
    52: "很少带球",
    53: "尝试发展弱势脚",
    54: "站桩式中锋",
    55: "用大力手抛球发动反击",
    56: "不喜欢尝试远射",
    57: "喜欢从左路内切",
    58: "喜欢从右路内切",
    59: "早传中",
    60: "把球带出防守区域",
    63: "门将用脚出球",
}
FM24_VERIFIED_PREFERRED_MOVE_CONFLICTS = (
    (1 << 13, 1 << 54),
    (1 << 13, 1 << 18),
    (1 << 19, 1 << 13),
    (1 << 24, 1 << 42),
    (1 << 27, 1 << 52),
)


def _normalized(value: str) -> str:
    return value.replace("\u200b", "").strip()


def _record(raw: bytes, start: int) -> tuple[str, int] | None:
    if start < 0 or start + 5 > len(raw) or raw[start] != 1:
        return None
    size = struct.unpack_from("<I", raw, start + 1)[0]
    end = start + 5 + size
    if size > 4096 or end > len(raw):
        return None
    try:
        value = raw[start + 5:end].decode("utf-8")
    except UnicodeDecodeError:
        return None
    for suffix in _DISPLAY_SUFFIXES:
        value = value.removesuffix(suffix)
    return _normalized(value), end


def _previous_record_start(raw: bytes, current: int) -> int | None:
    for candidate in range(current - 1, max(-1, current - 4096), -1):
        parsed = _record(raw, candidate)
        if parsed and parsed[1] == current:
            return candidate
    return None


def _optional_zero_width_pattern(value: str) -> bytes:
    separator = rb"(?:\xe2\x80\x8b)?"
    return separator.join(re.escape(char.encode("utf-8")) for char in value)


@lru_cache(maxsize=4)
def read_fm26_preferred_move_catalog(languages_fmf: str | Path) -> tuple[str, ...]:
    """Read FM26's current Simplified Chinese PPM display list.

    FM's upper preferred-move enum contains gaps/reordered values, so callers
    must not treat every display-list index as a bit index without a verified
    enum mapping.
    """
    raw = Path(languages_fmf).read_bytes()
    anchor_pattern = _optional_zero_width_pattern("插入对方禁区")
    for match in re.finditer(anchor_pattern, raw):
        start = match.start() - 5
        anchor = _record(raw, start)
        if not anchor or anchor[0] != "插入对方禁区":
            continue
        for _ in range(3):
            previous = _previous_record_start(raw, start)
            if previous is None:
                break
            start = previous
        else:
            labels = []
            cursor = start
            for _index in range(PREFERRED_MOVE_COUNT):
                parsed = _record(raw, cursor)
                if not parsed:
                    break
                label, cursor = parsed
                labels.append(label)
            if (
                len(labels) == PREFERRED_MOVE_COUNT
                and labels[0] == "沿左路带球突进"
                and labels[3] == "插入对方禁区"
                and labels[5] == "有机会就前插"
                and labels[8] == "远射"
                and labels[20] == "撞墙式配合"
                and labels[39] == "喜欢从双侧内切"
                and labels[63] == "仅左脚"
            ):
                return tuple(labels)
    raise RuntimeError("无法从当前FM26语言包解析个人习惯中文表")


def preferred_moves_from_mask(mask: int, catalog: tuple[str, ...]) -> list[dict[str, int | str]]:
    """Decode only the currently verified direct-index portion of the enum."""
    return [
        {"bit": bit, "name": catalog[bit]}
        for bit in range(min(48, len(catalog)))
        if int(mask) & (1 << bit)
    ]


def read_fm26_preferred_move_conflict_rules(
    reader: Any, module_base: int,
) -> tuple[tuple[int, int], ...]:
    size = FM26_PREFERRED_MOVE_CONFLICT_RULE_COUNT * 16
    raw = reader.bytes(module_base + FM26_PREFERRED_MOVE_CONFLICT_TABLE_RVA, size)
    if not raw or len(raw) != size:
        raise RuntimeError("无法读取FM26个人习惯冲突表")
    values = struct.unpack(f"<{FM26_PREFERRED_MOVE_CONFLICT_RULE_COUNT * 2}Q", raw)
    rules = tuple(zip(values[0::2], values[1::2]))
    if any(not left or not right for left, right in rules):
        raise RuntimeError("FM26个人习惯冲突表校验失败")
    return rules


def preferred_move_conflicts(
    mask: int, rules: tuple[tuple[int, int], ...],
) -> list[dict[str, int]]:
    value = int(mask) & ((1 << PREFERRED_MOVE_COUNT) - 1)
    return [
        {"left": left, "right": right}
        for left, right in rules
        if value & left == left and value & right == right
    ]


def validate_preferred_move_addition(
    current_mask: int, bit: int, rules: tuple[tuple[int, int], ...],
) -> int:
    if not 0 <= int(bit) < PREFERRED_MOVE_COUNT:
        raise ValueError("个人习惯编号超出范围")
    candidate = int(current_mask) | (1 << int(bit))
    if preferred_move_conflicts(candidate, rules):
        raise ValueError("所选个人习惯与球员现有习惯冲突")
    return candidate


def preferred_move_catalog_for_current_game() -> list[dict[str, int | str]]:
    _pid, path, layout = select_process_layout()
    if layout.key == "fm24":
        return [
            {"bit": bit, "name": name}
            for bit, name in FM24_VERIFIED_PREFERRED_MOVES.items()
        ]
    if layout.key != "fm26":
        return []
    return [
        {"bit": bit, "name": name}
        for bit, name in FM24_VERIFIED_PREFERRED_MOVES.items()
    ]


def _validated_preferred_move_address(
    reader: Reader, player_address: int, player_id: int,
) -> tuple[int, int]:
    layout = reader.layout
    vtable = reader.ptr(player_address)
    candidates = [
        (layout.actual_player_vtable_rvas, layout.player_person_offset),
        (layout.player_and_non_player_vtable_rvas, layout.player_and_non_player_person_offset),
    ]
    for rvas, person_offset in candidates:
        if vtable not in {reader.module_base + rva for rva in rvas}:
            continue
        if reader.u32(player_address + person_offset + ENTITY_UID) != int(player_id):
            raise ValueError("球员ID回读校验失败")
        if rvas != layout.actual_player_vtable_rvas:
            raise ValueError("该球员对象类型尚未验证个人习惯字段")
        offset = FM24_PREFERRED_MOVES_OFFSET if layout.key == "fm24" else FM26_PREFERRED_MOVES_OFFSET
        return player_address + offset, offset
    raise ValueError("球员对象类型校验失败")


def read_player_preferred_moves(player_id: int, player_address: Any) -> dict[str, Any]:
    pid, _path, layout = select_process_layout()
    address = int(str(player_address), 0) if isinstance(player_address, str) else int(player_address)
    with open_process(pid) as process:
        module = layout.module(process)
        if not module:
            raise RuntimeError(f"{layout.module_name} 尚未加载")
        reader = Reader(process, module.base_address, layout)
        mask_address, offset = _validated_preferred_move_address(reader, address, player_id)
        raw = reader.bytes(mask_address, 8)
        if not raw or len(raw) != 8:
            raise ValueError("无法读取球员个人习惯")
        mask = struct.unpack("<Q", raw)[0]
        catalog = preferred_move_catalog_for_current_game()
        names = {int(row["bit"]): str(row["name"]) for row in catalog}
        return {
            "player_id": int(player_id), "offset": offset, "mask": mask,
            "moves": [
                {"bit": bit, "name": names.get(bit, f"未验证习惯 {bit}")}
                for bit in range(PREFERRED_MOVE_COUNT) if mask & (1 << bit)
            ],
        }


def update_player_preferred_move(
    player_id: int, player_address: Any, bit: int, operation: str,
) -> dict[str, Any]:
    bit = int(bit)
    if operation not in {"learn", "unlearn"}:
        raise ValueError("个人习惯训练操作无效")
    pid, _path, layout = select_process_layout()
    supported = {int(row["bit"]): str(row["name"]) for row in preferred_move_catalog_for_current_game()}
    if bit not in supported:
        raise ValueError("该个人习惯编号尚未在当前版本验证")
    address = int(str(player_address), 0) if isinstance(player_address, str) else int(player_address)
    with open_process(pid, write_memory=True) as process:
        module = layout.module(process)
        if not module:
            raise RuntimeError(f"{layout.module_name} 尚未加载")
        reader = Reader(process, module.base_address, layout)
        mask_address, offset = _validated_preferred_move_address(reader, address, player_id)
        original_raw = reader.bytes(mask_address, 8)
        if not original_raw or len(original_raw) != 8:
            raise ValueError("无法读取球员个人习惯")
        original = struct.unpack("<Q", original_raw)[0]
        flag = 1 << bit
        if operation == "learn" and original & flag:
            raise PreferredMoveStateError("learn")
        if operation == "unlearn" and not original & flag:
            raise PreferredMoveStateError("unlearn")
        if operation == "learn":
            rules = (
                read_fm26_preferred_move_conflict_rules(reader, module.base_address)
                if layout.key == "fm26"
                else FM24_VERIFIED_PREFERRED_MOVE_CONFLICTS
            )
            updated = validate_preferred_move_addition(original, bit, rules)
        else:
            updated = original & ~flag
        updated_raw = struct.pack("<Q", updated)
        try:
            write_process_memory(process, mask_address, updated_raw)
            if reader.bytes(mask_address, 8) != updated_raw:
                raise RuntimeError("写入后的个人习惯校验失败")
        except Exception:
            try:
                write_process_memory(process, mask_address, original_raw)
            except Exception:
                pass
            raise
        return {
            "player_id": int(player_id), "bit": bit, "name": supported[bit],
            "operation": operation, "offset": offset, "mask": updated,
            "_original_mask": original,
        }


def restore_player_preferred_moves(
    player_id: int, player_address: Any, original_mask: int,
) -> None:
    pid, _path, layout = select_process_layout()
    address = int(str(player_address), 0) if isinstance(player_address, str) else int(player_address)
    raw = struct.pack("<Q", int(original_mask))
    with open_process(pid, write_memory=True) as process:
        module = layout.module(process)
        if not module:
            raise RuntimeError(f"{layout.module_name} 尚未加载")
        reader = Reader(process, module.base_address, layout)
        mask_address, _offset = _validated_preferred_move_address(reader, address, player_id)
        write_process_memory(process, mask_address, raw)
        if reader.bytes(mask_address, 8) != raw:
            raise RuntimeError("原个人习惯恢复校验失败")
