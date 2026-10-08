from __future__ import annotations

import struct
from typing import Any

from fm_collector.win32 import write_process_memory
from tools.database_index import resolve_team_club
from tools.game_session import GameOperationSession, borrow_game_operation
from tools.initial_data_audit import ENTITY_UID, Reader


CLUB_CURRENT_FINANCE = 0x150
FINANCE_CLUB_REFERENCE = 0x08
CLUB_BALANCE = 0x14
MAX_CLUB_BALANCE = 2_000_000_000


def _address(value: Any) -> int:
    if isinstance(value, int):
        return value
    try:
        return int(str(value), 16)
    except (TypeError, ValueError):
        return 0


def _locate(
    reader: Reader,
    team_address: int,
    team_id: int = 0,
    *,
    club_finance_offset: int = CLUB_CURRENT_FINANCE,
    finance_balance_offset: int = CLUB_BALANCE,
) -> tuple[int, int]:
    if int(club_finance_offset) < 0 or int(finance_balance_offset) < 0:
        raise RuntimeError("当前游戏版本尚未映射俱乐部财务字段")
    resolved = resolve_team_club(reader, team_address, int(team_id or 0))
    club_address = int(resolved.club_address)
    finance = int(reader.ptr(club_address + int(club_finance_offset)) or 0)
    if not finance or int(reader.ptr(finance + FINANCE_CLUB_REFERENCE) or 0) != club_address:
        raise RuntimeError("无法定位当前俱乐部的财务记录")
    address = finance + int(finance_balance_offset)
    raw = reader.bytes(address, 4)
    if not raw or len(raw) != 4:
        raise RuntimeError("俱乐部结余字段读取失败")
    value = int(struct.unpack("<i", raw)[0])
    if not -MAX_CLUB_BALANCE <= value <= MAX_CLUB_BALANCE:
        raise RuntimeError("俱乐部结余字段校验失败")
    return address, value


def read_club_balance(
    team_address: Any, team_id: int = 0, *,
    operation: GameOperationSession | None = None,
) -> dict[str, Any]:
    if operation is None:
        with borrow_game_operation() as current:
            return read_club_balance(
                team_address, team_id, operation=current,
            )
    reader = operation.reader
    layout = operation.layout
    address, value = _locate(
        reader, _address(team_address), int(team_id or 0),
        club_finance_offset=getattr(layout, "club_finance_offset", CLUB_CURRENT_FINANCE),
        finance_balance_offset=getattr(layout, "finance_balance_offset", CLUB_BALANCE),
    )
    return {
        "available": True, "amount": value, "address": hex(address),
        "team_id": int(team_id or 0),
    }


def write_club_balance(
    team_address: Any, amount: int, *, team_id: int = 0,
    expected: int | None = None, operation: GameOperationSession | None = None,
) -> dict[str, Any]:
    amount = int(amount)
    if not -MAX_CLUB_BALANCE <= amount <= MAX_CLUB_BALANCE:
        raise ValueError("俱乐部结余超出允许范围")
    if operation is None:
        with borrow_game_operation(write_memory=True) as current_operation:
            return write_club_balance(
                team_address, amount, team_id=team_id, expected=expected,
                operation=current_operation,
            )
    if not operation.writable:
        raise RuntimeError("club balance write requires a writable operation session")
    reader = operation.reader
    process = operation.process
    layout = operation.layout
    address, current = _locate(
        reader, _address(team_address), int(team_id or 0),
        club_finance_offset=getattr(layout, "club_finance_offset", CLUB_CURRENT_FINANCE),
        finance_balance_offset=getattr(layout, "finance_balance_offset", CLUB_BALANCE),
    )
    if expected is not None and current != int(expected):
        raise RuntimeError("俱乐部结余刚刚发生变化，请重试")
    written_value = current
    for _attempt in range(2):
        write_process_memory(
            process, address, struct.pack("<i", amount),
            compatibility_fallback=True,
        )
        written = reader.bytes(address, 4)
        written_value = (
            int(struct.unpack("<i", written)[0])
            if written and len(written) == 4 else None
        )
        if written_value == amount or written_value != current:
            break
    if written_value != amount:
        if written_value != current:
            write_process_memory(
                process, address, struct.pack("<i", current),
                compatibility_fallback=True,
            )
        restored = reader.bytes(address, 4)
        restored_value = (
            int(struct.unpack("<i", restored)[0])
            if restored and len(restored) == 4 else None
        )
        if restored_value != current:
            raise RuntimeError("俱乐部结余写入后校验失败，且原值恢复失败")
        raise RuntimeError("俱乐部结余写入后校验失败，已恢复原值")
    return {
        "available": True, "amount": amount, "previous_amount": current,
        "address": hex(address), "team_id": int(team_id or 0),
    }
