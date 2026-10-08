from __future__ import annotations

import struct
from calendar import monthrange
from datetime import date
from typing import Any

from fm_collector.win32 import MEM_PRIVATE, iter_readable_regions, open_process, read_process_memory, write_process_memory
from tools.initial_data_audit import ENTITY_UID, Reader, decode_date, select_process_layout


FM24_RETIREMENT_OWNER_RVA = 0x6378C78
FM26_RETIREMENT_OWNER_RVA = 0x4E3ACE0
FM24_VECTOR_PAIRS = ((0x18, 0x20), (0x30, 0x38), (0x60, 0x68))
FM26_VECTOR_OFFSET = 0x90
FM24_PERSON_FLAGS_OFFSET = 0x18
RETIREMENT_DATE_OFFSET = 0x0C
CHECK_DATE_OFFSET = 0x10
STATE_OFFSET = 0x14
RETIREMENT_PERSON_OFFSET = 0x04
RETIREMENT_ACTION_MONTHS = {
    "six_months": 6,
    "two_years": 24,
    "ten_years": 120,
    "twenty_years": 240,
}


def _player_person(reader: Reader, player: int) -> int:
    vtable = reader.ptr(player)
    rva = vtable - reader.module_base if vtable else 0
    if rva in reader.layout.actual_player_vtable_rvas:
        return player + reader.layout.player_person_offset
    if rva in reader.layout.player_and_non_player_vtable_rvas:
        return player + reader.layout.player_and_non_player_person_offset
    raise RuntimeError("球员对象类型校验失败")


def _valid_vector(begin: int, end: int, capacity: int, stride: int) -> bool:
    return bool(
        begin and begin <= end <= capacity
        and (end - begin) % stride == 0
        and (end - begin) // stride <= 250_000
    )


def _record_index(
    reader: Reader, keys: set[int] | None = None, *, allow_fallback: bool = True,
) -> dict[int, list[int]]:
    result: dict[int, list[int]] = {}
    owner = None
    authoritative_index = False
    if reader.layout.key == "fm26" and reader.layout.distribution == "steam":
        owner = reader.ptr(reader.module_base + FM26_RETIREMENT_OWNER_RVA)
    elif reader.layout.key == "fm24" and reader.layout.distribution == "steam":
        owner = reader.ptr(reader.module_base + FM24_RETIREMENT_OWNER_RVA)
    if reader.layout.key == "fm26" and owner:
        begin = reader.ptr(owner + FM26_VECTOR_OFFSET)
        end = reader.ptr(owner + FM26_VECTOR_OFFSET + 8)
        capacity = reader.ptr(owner + FM26_VECTOR_OFFSET + 16)
        if not _valid_vector(int(begin or 0), int(end or 0), int(capacity or 0), 16):
            owner = None
        else:
            authoritative_index = True
            raw = reader.bytes(int(begin), int(end) - int(begin)) or b""
            for offset in range(0, len(raw), 16):
                key, record = struct.unpack_from("<IxxxxQ", raw, offset)
                if key and record:
                    result.setdefault(int(key), []).append(int(record))
    elif reader.layout.key == "fm24" and owner:
        seen: set[int] = set()
        for begin_offset, end_offset in FM24_VECTOR_PAIRS:
            begin = reader.ptr(owner + begin_offset)
            end = reader.ptr(owner + end_offset)
            if not _valid_vector(int(begin or 0), int(end or 0), int(end or 0), 8):
                continue
            authoritative_index = True
            raw = reader.bytes(int(begin), int(end) - int(begin)) or b""
            for offset in range(0, len(raw), 8):
                record = struct.unpack_from("<Q", raw, offset)[0]
                if not record or record in seen:
                    continue
                seen.add(record)
                key = reader.u32(record)
                if key:
                    result.setdefault(int(key), []).append(int(record))

    wanted = {int(key) for key in (keys or ()) if int(key) > 0}
    if (
        authoritative_index
        or not allow_fallback
        or not wanted
        or wanted.issubset(result.keys())
    ):
        return result

    # Protected distributions do not expose the Steam global manager RVA.
    # Locate only records for the requested player keys in readable private
    # memory, then apply the same date/mirror validation as the normal chain.
    missing = wanted - result.keys()
    if not missing:
        return result
    needles = {struct.pack("<I", key): key for key in missing}
    for region in iter_readable_regions(reader.process):
        if region.type != MEM_PRIVATE:
            continue
        carry = b""
        chunk_size = 8 * 1024 * 1024
        for offset in range(0, int(region.size), chunk_size):
            data = read_process_memory(
                reader.process, int(region.base_address) + offset,
                min(chunk_size, int(region.size) - offset),
            )
            if not data:
                carry = b""
                continue
            scan = carry + data
            base = int(region.base_address) + offset - len(carry)
            found: set[int] = set()
            for needle, key in needles.items():
                cursor = 0
                while True:
                    cursor = scan.find(needle, cursor)
                    if cursor < 0:
                        break
                    record = base + cursor
                    if record % 8 == 0 and _detail(reader, [record]):
                        result.setdefault(key, []).append(record)
                        found.add(key)
                    cursor += 1
            if found:
                needles = {
                    needle: key for needle, key in needles.items()
                    if key not in found
                }
                if not needles:
                    return result
            carry = scan[-3:]
    return result


def retirement_record_index(reader: Reader) -> dict[int, list[int]]:
    """Return the authoritative session retirement records keyed by native person key.

    This intentionally does not use the bounded private-memory fallback: the
    activity centre's world view must only expose records proven by FM's native
    retirement manager, rather than arbitrary byte-pattern matches.
    """
    return _record_index(reader, None, allow_fallback=False)


def _date_at(reader: Reader, record: int, offset: int) -> date | None:
    return decode_date(int(reader.u32(record + offset) or 0))


def _detail(reader: Reader, records: list[int]) -> dict[str, Any] | None:
    for record in records:
        retirement_date = _date_at(reader, record, RETIREMENT_DATE_OFFSET)
        check_date = _date_at(reader, record, CHECK_DATE_OFFSET)
        state = reader.u16(record + STATE_OFFSET)
        if not retirement_date or not check_date or state is None:
            continue
        return {
            "has_plan": True,
            "retirement_date": retirement_date.isoformat(),
            "check_date": check_date.isoformat(),
            "cancelled": int(state) == 0x80,
            "state": int(state),
            "_record": int(record),
        }
    return None


def attach_retirement_details(reader: Reader, players: list[dict[str, Any]]) -> None:
    keys: set[int] = set()
    for player in players:
        try:
            address = int(str(player.get("address") or "0"), 16)
            person = _player_person(reader, address)
            keys.add(int(reader.u32(person + 8) or 0))
        except (TypeError, ValueError, RuntimeError):
            continue
    # A club refresh must remain bounded. Protected builds can still use the
    # targeted fallback when the user submits a retirement-plan action.
    index = _record_index(reader, keys, allow_fallback=False)
    for player in players:
        try:
            address = int(str(player.get("address") or "0"), 16)
            person = _player_person(reader, address)
            if int(reader.u32(person + ENTITY_UID) or 0) != int(player.get("id") or 0):
                player["retirement"] = None
                continue
            internal_key = int(reader.u32(person + 8) or 0)
            detail = _detail(reader, index.get(internal_key, []))
            player["retirement"] = (
                {key: value for key, value in detail.items() if not key.startswith("_")}
                if detail else {"has_plan": False}
            )
        except (TypeError, ValueError, RuntimeError):
            player["retirement"] = None


def _replace_date(raw_value: int, target: date) -> bytes:
    flags = raw_value & 0x0000FE00
    return struct.pack("<I", (target.year << 16) | flags | target.timetuple().tm_yday)


def _one_year_before(target: date) -> date:
    try:
        return target.replace(year=target.year - 1)
    except ValueError:
        return target.replace(year=target.year - 1, day=28)


def _add_calendar_months(value: date, months: int) -> date:
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    return date(year, month, min(value.day, monthrange(year, month)[1]))


def _retirement_target_date(current: date, action: str) -> date:
    months = RETIREMENT_ACTION_MONTHS.get(action)
    if months is None:
        raise ValueError("未知的退役活动操作")
    return _add_calendar_months(current, months)


def update_retirement_plan(
    player_id: int, player_address: Any, *, action: str,
    capture_rollback: bool = False,
    missing_ok: bool = False,
) -> dict[str, Any]:
    pid, _path, layout = select_process_layout()
    if layout.key not in {"fm24", "fm26"}:
        raise RuntimeError("当前FM版本尚未支持退役活动")
    with open_process(pid, write_memory=True) as process:
        module = layout.module(process)
        if not module:
            raise RuntimeError(f"{layout.module_name} 尚未加载")
        reader = Reader(process, module.base_address, layout)
        address = int(str(player_address), 16) if isinstance(player_address, str) else int(player_address)
        person = _player_person(reader, address)
        if int(reader.u32(person + ENTITY_UID) or 0) != int(player_id):
            raise RuntimeError("球员身份已变化，请刷新我的俱乐部后重试")
        internal_key = int(reader.u32(person + 8) or 0)
        records = list(dict.fromkeys(
            _record_index(reader, {internal_key}).get(internal_key, [])
        ))
        detail = _detail(reader, records)
        record: int | None = None
        if detail:
            record = int(detail["_record"])
        else:
            candidates = [
                int(record) for record in records
                if int(reader.u32(int(record)) or 0) == internal_key
            ]
            if not candidates:
                if action != "cancel" or not missing_ok:
                    raise RuntimeError("该球员当前没有可激活的原生退役记录")
            elif len(candidates) != 1:
                raise RuntimeError("该球员的原生退役记录不唯一，已拒绝修改")
            else:
                record = candidates[0]

        rollback_snapshot: list[tuple[int, bytes, bytes]] = []
        if action == "cancel":
            current = decode_date(int(reader.u32(module.base_address + int(layout.game_date_rva or 0)) or 0))
            if not current:
                raise RuntimeError("无法读取当前游戏日期")
            # A malformed/duplicated index can expose more than one native
            # record for the same person.  Cancelling only the first record
            # leaves the game free to consume another active record.
            cancel_records = list(dict.fromkeys(
                int(candidate) for candidate in records
                if int(reader.u32(int(candidate)) or 0) == internal_key
            ))
            if record is not None and record not in cancel_records:
                cancel_records.insert(0, record)
            originals: dict[tuple[int, int], bytes] = {}
            replacements: dict[tuple[int, int], bytes] = {}
            try:
                if reader.layout.key == "fm24":
                    flags_raw = reader.bytes(person + FM24_PERSON_FLAGS_OFFSET, 4)
                    if not flags_raw or len(flags_raw) != 4:
                        raise RuntimeError("无法读取球员退役状态")
                    originals[(person, FM24_PERSON_FLAGS_OFFSET)] = flags_raw
                    replacements[(person, FM24_PERSON_FLAGS_OFFSET)] = struct.pack(
                        "<I", struct.unpack("<I", flags_raw)[0] & ~0x01,
                    )
                for candidate in cancel_records:
                    for offset, size in (
                        (RETIREMENT_PERSON_OFFSET, 4),
                        (RETIREMENT_DATE_OFFSET, 4),
                        (CHECK_DATE_OFFSET, 4),
                        (STATE_OFFSET, 2),
                    ):
                        original = reader.bytes(candidate + offset, size)
                        if not original or len(original) != size:
                            raise RuntimeError("无法读取退役记录")
                        originals[(candidate, offset)] = original
                    replacements[(candidate, RETIREMENT_PERSON_OFFSET)] = b"\x00" * 4
                    replacements[(candidate, RETIREMENT_DATE_OFFSET)] = b"\x00" * 4
                    replacements[(candidate, CHECK_DATE_OFFSET)] = b"\x00" * 4
                    replacements[(candidate, STATE_OFFSET)] = struct.pack("<H", 0x80)
                for (candidate, offset), replacement in replacements.items():
                    write_process_memory(process, candidate + offset, replacement)
                if any(
                    reader.bytes(candidate + offset, len(replacement)) != replacement
                    for (candidate, offset), replacement in replacements.items()
                ):
                    raise RuntimeError("取消退役计划回读校验失败")
                rollback_snapshot = [
                    (candidate + offset, originals[(candidate, offset)], replacement)
                    for (candidate, offset), replacement in replacements.items()
                ]
            except Exception:
                for (candidate, offset), original in originals.items():
                    write_process_memory(process, candidate + offset, original)
                raise
        elif action in RETIREMENT_ACTION_MONTHS:
            if record is None:
                raise RuntimeError("该球员当前没有可激活的原生退役记录")
            current = decode_date(int(reader.u32(module.base_address + int(layout.game_date_rva or 0)) or 0))
            if not current:
                raise RuntimeError("无法读取当前游戏日期")
            target = _retirement_target_date(current, action)
            write_offsets = [RETIREMENT_DATE_OFFSET, STATE_OFFSET]
            originals: dict[int, bytes] = {}
            replacements: dict[int, bytes] = {}
            date_raw = reader.bytes(record + RETIREMENT_DATE_OFFSET, 4)
            if not date_raw or len(date_raw) != 4:
                raise RuntimeError("退役日期记录读取不完整")
            originals[RETIREMENT_DATE_OFFSET] = date_raw
            replacements[RETIREMENT_DATE_OFFSET] = _replace_date(
                struct.unpack("<I", date_raw)[0], target
            )
            state_raw = reader.bytes(record + STATE_OFFSET, 2)
            if not state_raw or len(state_raw) != 2:
                raise RuntimeError("无法读取退役状态")
            originals[STATE_OFFSET] = state_raw
            replacements[STATE_OFFSET] = struct.pack(
                "<H", struct.unpack("<H", state_raw)[0] & ~0x80
            )
            try:
                for offset in write_offsets:
                    write_process_memory(process, record + offset, replacements[offset])
                if any(
                    reader.bytes(record + offset, len(replacements[offset]))
                    != replacements[offset]
                    for offset in write_offsets
                ):
                    raise RuntimeError("退役计划写入回读校验失败")
            except Exception:
                for offset, raw in originals.items():
                    write_process_memory(process, record + offset, raw)
                raise
        else:
            raise ValueError("未知的退役活动操作")

        updated = _detail(reader, [record] if record is not None else [])
        if not updated:
            if action == "cancel":
                result = {
                    "has_plan": False,
                    "cancelled": bool(rollback_snapshot),
                    "state": 0x80 if cancel_records else None,
                }
                if capture_rollback:
                    result["_rollback"] = rollback_snapshot
                return result
            raise RuntimeError("无法回读修改后的退役计划")
        result = {key: value for key, value in updated.items() if not key.startswith("_")}
        if capture_rollback and action == "cancel":
            result["_rollback"] = rollback_snapshot
        return result


def restore_retirement_plan(
    player_id: int, player_address: Any,
    rollback_snapshot: list[tuple[int, bytes, bytes]],
) -> None:
    """Restore a cancellation snapshot without overwriting intervening writes."""
    if not rollback_snapshot:
        raise ValueError("原退役状态快照无效")
    changes: list[tuple[int, bytes, bytes]] = []
    for entry in rollback_snapshot:
        if not isinstance(entry, (tuple, list)) or len(entry) != 3:
            raise ValueError("原退役状态快照无效")
        address, original, cancelled = entry
        original = bytes(original)
        cancelled = bytes(cancelled)
        if int(address) <= 0 or len(original) not in {2, 4} or len(original) != len(cancelled):
            raise ValueError("原退役状态快照无效")
        changes.append((int(address), original, cancelled))

    pid, _path, layout = select_process_layout()
    if layout.key not in {"fm24", "fm26"}:
        raise RuntimeError("当前FM版本尚未支持退役活动")
    with open_process(pid, write_memory=True) as process:
        module = layout.module(process)
        if not module:
            raise RuntimeError(f"{layout.module_name} 尚未加载")
        reader = Reader(process, module.base_address, layout)
        address = int(str(player_address), 16) if isinstance(player_address, str) else int(player_address)
        person = _player_person(reader, address)
        if int(reader.u32(person + ENTITY_UID) or 0) != int(player_id):
            raise RuntimeError("球员身份已变化，无法恢复原退役状态")
        if any(
            reader.bytes(change_address, len(cancelled)) != cancelled
            for change_address, _original, cancelled in changes
        ):
            raise RuntimeError("退役状态已被其他操作改变，无法安全恢复")
        try:
            for change_address, original, _cancelled in changes:
                write_process_memory(process, change_address, original)
            if any(
                reader.bytes(change_address, len(original)) != original
                for change_address, original, _cancelled in changes
            ):
                raise RuntimeError("原退役状态恢复回读校验失败")
        except Exception as error:
            for change_address, _original, cancelled in changes:
                write_process_memory(process, change_address, cancelled)
            if any(
                reader.bytes(change_address, len(cancelled)) != cancelled
                for change_address, _original, cancelled in changes
            ):
                raise RuntimeError("原退役状态恢复失败且回退校验异常") from error
            raise
