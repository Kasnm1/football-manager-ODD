from __future__ import annotations

import struct
import threading
from typing import Any

from fm_collector.win32 import MEM_PRIVATE, iter_readable_regions, write_process_memory
from tools.database_index import database_index_for_reader
from tools.game_session import GameOperationSession, borrow_game_operation
from tools.initial_data_audit import (
    ENTITY_UID,
    Reader,
    TEAM_CLUB,
)


# Club -> current finance record. The record keeps its club reference at +0x08.
CLUB_CURRENT_FINANCE = 0x150
FINANCE_CLUB_REFERENCE = 0x08
MAX_TRANSFER_BUDGET = 2_000_000_000
TRANSFER_BUDGET_REGION_SIZE = 0x1B1000
TRANSFER_BUDGET_REGION_MIN = 0x40000
TRANSFER_BUDGET_REGION_MAX = 8 * 1024 * 1024
TRANSFER_BUDGET_SCAN_LIMIT = 256 * 1024 * 1024
CLUB_REFERENCE_TO_TRANSFER_BUDGET = 0x7C4
TRANSFER_BUDGET_SIGNATURE = bytes.fromhex(
    "3c7e2b00190000006c9201000000000000000000e0c81000084c0100"
    "ffffffff55010000975700000000000001000000"
)
_LOCATOR_LOCK = threading.RLock()
_RECORD_CACHE: dict[tuple[int, int], int] = {}
_SESSION_SCAN_FAILURES: dict[tuple[int, int], str] = {}


class TransferBudgetLocationError(RuntimeError):
    """The exact Team -> Club -> Finances object chain could not be proven."""


def clear_locator_failures(club_address: int = 0) -> None:
    """Forget read-scan failures after a fresh validated club profile refresh."""
    target = int(club_address or 0)
    with _LOCATOR_LOCK:
        if target <= 0:
            _SESSION_SCAN_FAILURES.clear()
            return
        for cache_key in list(_SESSION_SCAN_FAILURES):
            if cache_key[1] == target:
                _SESSION_SCAN_FAILURES.pop(cache_key, None)


def _address(value: Any) -> int:
    if isinstance(value, int):
        return value
    try:
        return int(str(value), 16)
    except (TypeError, ValueError):
        return 0


def _budget_value(reader: Reader, address: int) -> int | None:
    raw = reader.bytes(address, 4) if address else None
    if not raw or len(raw) != 4:
        return None
    value = int(struct.unpack("<I", raw)[0])
    return value if 0 <= value <= MAX_TRANSFER_BUDGET else None


def _validated_candidate(reader: Reader, address: int, club_address: int) -> int:
    if (
        address > CLUB_REFERENCE_TO_TRANSFER_BUDGET
        and int(reader.ptr(address - CLUB_REFERENCE_TO_TRANSFER_BUDGET) or 0) == club_address
        and _budget_value(reader, address) is not None
    ):
        return address
    return 0


def _validated_club_candidates(
    reader: Reader, team_address: int, team_id: int, layout: Any,
) -> list[tuple[int, int]]:
    """Resolve live Team/Club objects by UID, preferring the native object directory."""
    addresses: list[int] = []
    if team_id:
        directory = database_index_for_reader(reader)
        if directory is not None:
            try:
                addresses.extend(
                    int(address)
                    for address in directory.addresses_for_uid("team", int(team_id))
                    if int(address or 0) > 0
                )
            except (OSError, RuntimeError, TypeError, ValueError):
                addresses.clear()
    if team_address and int(team_address) not in addresses:
        addresses.append(int(team_address))
    elif team_address in addresses:
        addresses.remove(int(team_address))
        addresses.insert(0, int(team_address))

    team_vtable_rva = getattr(layout, "team_vtable_rva", None)
    club_vtable_rva = getattr(layout, "club_vtable_rva", None)
    module_base = int(getattr(reader, "module_base", 0) or 0)
    if team_vtable_rva is None or club_vtable_rva is None or module_base <= 0:
        raise TransferBudgetLocationError("当前游戏版本尚未映射俱乐部对象")

    candidates: list[tuple[int, int]] = []
    seen: set[int] = set()
    for candidate in addresses:
        if candidate in seen:
            continue
        seen.add(candidate)
        if int(reader.ptr(candidate) or 0) != module_base + int(team_vtable_rva):
            continue
        actual_team_id = int(reader.u32(candidate + ENTITY_UID) or 0)
        if team_id and actual_team_id != int(team_id):
            continue
        club_address = int(reader.ptr(candidate + TEAM_CLUB) or 0)
        if (
            not club_address
            or int(reader.ptr(club_address) or 0)
            != module_base + int(club_vtable_rva)
        ):
            continue
        candidates.append((candidate, club_address))
    if not candidates:
        raise TransferBudgetLocationError("无法按俱乐部 ID 取得当前俱乐部对象，请刷新后重试")
    return candidates


def _pointer_chain_address(
    reader: Reader, team_address: int, team_id: int, layout: Any,
) -> tuple[int, int, int]:
    """Follow the FMRTE-style Team -> Club -> Finances object path."""
    club_finance_offset = getattr(layout, "club_finance_offset", None)
    budget_offset = getattr(layout, "finance_remaining_transfer_budget_offset", None)
    if club_finance_offset is None or budget_offset is None:
        raise TransferBudgetLocationError("当前游戏版本尚未映射俱乐部转会预算")
    if int(club_finance_offset) < 0 or int(budget_offset) < 0:
        raise TransferBudgetLocationError("当前游戏版本尚未映射俱乐部转会预算")

    hits: dict[int, tuple[int, int]] = {}
    club_candidates = _validated_club_candidates(
        reader, team_address, team_id, layout,
    )
    for _resolved_team, club_address in club_candidates:
        finance = int(reader.ptr(club_address + int(club_finance_offset)) or 0)
        if (
            not finance
            or int(reader.ptr(finance + FINANCE_CLUB_REFERENCE) or 0)
            != club_address
        ):
            continue
        address = finance + int(budget_offset)
        value = _budget_value(reader, address)
        if value is not None:
            hits[address] = (value, club_address)
    if len(hits) == 1:
        address, (value, club_address) = next(iter(hits.items()))
        return address, value, club_address
    if len(hits) > 1:
        raise TransferBudgetLocationError("找到多个当前俱乐部财务对象，已拒绝写入")
    raise TransferBudgetLocationError("无法从当前俱乐部对象取得财务记录，请刷新后重试")


def _scan_regions(process: Any) -> tuple[list[Any], set[int]]:
    private = [
        region for region in iter_readable_regions(process)
        if region.type == MEM_PRIVATE
        and TRANSFER_BUDGET_REGION_MIN <= region.size <= TRANSFER_BUDGET_REGION_MAX
    ]
    private.sort(key=lambda region: (
        0 if region.size == TRANSFER_BUDGET_REGION_SIZE else 1,
        abs(region.size - TRANSFER_BUDGET_REGION_SIZE),
        region.base_address,
    ))
    selected = []
    preferred_bases: set[int] = set()
    scanned_bytes = 0
    for region in private:
        if region.size == TRANSFER_BUDGET_REGION_SIZE:
            preferred_bases.add(region.base_address)
        if scanned_bytes + region.size > TRANSFER_BUDGET_SCAN_LIMIT and selected:
            continue
        selected.append(region)
        scanned_bytes += region.size
    return selected, preferred_bases


def _signature_address(
    process: Any, reader: Reader, pid: int, club_address: int, *, force_scan: bool = False,
) -> int:
    with _LOCATOR_LOCK:
        cache_key = (pid, club_address)
        cached = int(_RECORD_CACHE.get(cache_key) or 0)
        if cached and _validated_candidate(reader, cached, club_address):
            return cached

        if force_scan:
            _SESSION_SCAN_FAILURES.pop(cache_key, None)
        if failure := _SESSION_SCAN_FAILURES.get(cache_key):
            raise RuntimeError(failure)

        regions, preferred_bases = _scan_regions(process)
        club_needle = struct.pack("<Q", club_address)
        club_hits: set[int] = set()
        preferred_club_hits: set[int] = set()
        legacy_signature_hits: set[int] = set()
        signature_hits: set[int] = set()
        legacy_first_hit = 0
        for region in regions:
            data = reader.bytes(region.base_address, region.size)
            if not data:
                continue
            cursor = 0
            while True:
                cursor = data.find(club_needle, cursor)
                if cursor < 0:
                    break
                reference = region.base_address + cursor
                candidate = reference + CLUB_REFERENCE_TO_TRANSFER_BUDGET
                if reference % 8 == 0 and _budget_value(reader, candidate) is not None:
                    club_hits.add(candidate)
                    if region.base_address in preferred_bases:
                        preferred_club_hits.add(candidate)
                cursor += 1
            cursor = 0
            while True:
                cursor = data.find(TRANSFER_BUDGET_SIGNATURE, cursor)
                if cursor < 0:
                    break
                candidate = region.base_address + cursor - 8
                if _budget_value(reader, candidate) is not None:
                    signature_hits.add(candidate)
                    if region.base_address in preferred_bases:
                        legacy_signature_hits.add(candidate)
                cursor += 1
            if region.base_address in preferred_bases and not legacy_first_hit:
                region_hits = {
                    candidate for candidate in signature_hits
                    if region.base_address <= candidate < region.base_address + region.size
                }
                if len(region_hits) == 1:
                    # Preserve the original locator's working path: the first
                    # legacy-sized region with one valid signature wins.
                    legacy_first_hit = next(iter(region_hits))

        selected = 0
        if len(preferred_club_hits) == 1:
            # Preserve the original working locator: the legacy-sized budget
            # region is more specific than generic club references elsewhere.
            selected = next(iter(preferred_club_hits))
        elif len(preferred_club_hits) > 1:
            tied = preferred_club_hits & signature_hits
            if len(tied) == 1:
                selected = next(iter(tied))
        elif len(club_hits) == 1:
            selected = next(iter(club_hits))
        elif len(club_hits) > 1:
            tied = club_hits & signature_hits
            if len(tied) == 1:
                selected = next(iter(tied))
        if not selected and legacy_first_hit:
            selected = legacy_first_hit
        elif not selected and len(legacy_signature_hits) == 1:
            # Compatibility with the old exact-region locator. This fallback is
            # intentionally not applied to arbitrary adaptive regions.
            selected = next(iter(legacy_signature_hits))
        if selected:
            _RECORD_CACHE[cache_key] = selected
            return selected

        reason = (
            "找到多个疑似转会预算字段，无法安全确认当前俱乐部"
            if len(club_hits) > 1
            else "无法定位当前俱乐部的转会预算（已尝试指针链和自适应内存区）"
        )
        _SESSION_SCAN_FAILURES[cache_key] = reason
        raise RuntimeError(reason)


def _locate(
    process: Any, reader: Reader, pid: int, team_address: int, team_id: int = 0,
    *, layout: Any, force_scan: bool = False, allow_scan: bool = True,
) -> tuple[int, int]:
    if not team_address:
        raise TransferBudgetLocationError("尚未定位当前执教俱乐部")
    try:
        address, value, _club_address = _pointer_chain_address(
            reader, team_address, team_id, layout,
        )
        return address, value
    except TransferBudgetLocationError as exact_error:
        if not allow_scan:
            raise
        club_candidates = _validated_club_candidates(
            reader, team_address, team_id, layout,
        )
        club_addresses = {club for _team, club in club_candidates}
        if len(club_addresses) != 1:
            raise exact_error
        club_address = next(iter(club_addresses))
        address = _signature_address(
            process, reader, pid, club_address, force_scan=force_scan,
        )
        value = _budget_value(reader, address)
        if value is None:
            raise RuntimeError("转会预算字段校验失败")
        return address, value


def read_transfer_budget(
    team_address: Any, team_id: int = 0, *, force_scan: bool = False,
    allow_scan: bool = True,
    operation: GameOperationSession | None = None,
) -> dict[str, Any]:
    if operation is None:
        with borrow_game_operation() as current:
            return read_transfer_budget(
                team_address, team_id, force_scan=force_scan,
                allow_scan=allow_scan,
                operation=current,
            )
    address, value = _locate(
        operation.process, operation.reader, operation.pid,
        _address(team_address), int(team_id or 0), layout=operation.layout,
        force_scan=force_scan, allow_scan=allow_scan,
    )
    return {
        "available": True, "amount": value, "address": hex(address),
        "team_id": int(team_id or 0),
    }


def write_transfer_budget(
    team_address: Any,
    amount: int,
    *,
    team_id: int = 0,
    expected: int | None = None,
    operation: GameOperationSession | None = None,
) -> dict[str, Any]:
    amount = int(amount)
    if not 0 <= amount <= MAX_TRANSFER_BUDGET:
        raise ValueError("转会预算超出允许范围")
    if operation is None:
        with borrow_game_operation(write_memory=True) as current_operation:
            return write_transfer_budget(
                team_address, amount, team_id=team_id, expected=expected,
                operation=current_operation,
            )
    if not operation.writable:
        raise RuntimeError("transfer budget write requires a writable operation session")
    process = operation.process
    reader = operation.reader
    address, current = _locate(
        process, reader, operation.pid,
        _address(team_address), int(team_id or 0),
        layout=operation.layout, allow_scan=False,
    )
    if expected is not None and current != int(expected):
        raise RuntimeError("转会预算刚刚发生变化，请重试")
    written = current
    for _attempt in range(2):
        write_process_memory(process, address, struct.pack("<I", amount))
        written = _budget_value(reader, address)
        if written == amount or written != current:
            break
    if written != amount:
        if written != current:
            write_process_memory(process, address, struct.pack("<I", current))
        restored = _budget_value(reader, address)
        if restored != current:
            raise RuntimeError("转会预算写入后校验失败，且原值恢复失败")
        raise RuntimeError("转会预算写入后校验失败，已恢复原值")
    return {
        "available": True,
        "amount": amount,
        "previous_amount": current,
        "address": hex(address),
        "team_id": int(team_id or 0),
    }
