from __future__ import annotations

import struct
import threading
from dataclasses import dataclass, replace
from typing import Any

from fm_collector.win32 import (
    MEM_PRIVATE,
    iter_readable_regions,
    open_process,
    read_process_memory,
    write_process_memory,
)
from tools.game_layout import (
    FM24_EPIC_EXE_SHA256,
    FM24_EXE_SHA256,
    FM26_EXE_SHA256,
    _pe_sections,
    _rtti_vtables,
)
from tools.initial_data_audit import ENTITY_UID, Reader, decode_date, select_process_layout
from tools.database_index import database_index_for_reader, resolve_team_club


CONFIRMED_STATUS = 19
CANCELLED_STATUS = 3


@dataclass(frozen=True)
class FutureTransferLayout:
    manager_vtable_rva: int
    manager_pointer_rva: int
    full_offer_vtable_rva: int
    loan_offer_vtable_rva: int
    person_offset: int
    buyer_offset: int
    seller_offset: int
    value_offset: int
    transfer_id_offset: int
    status_offset: int
    offers_vector_offset: int = 0x20
    dates_vector_offset: int = 0x50
    date_record_size: int = 0x10

    @property
    def offer_read_size(self) -> int:
        return self.status_offset + 1


@dataclass(frozen=True)
class HistoricalTransferArchiveLayout:
    manager: FutureTransferLayout
    source_name: str
    vector_offset: int = 0x70
    record_size: int = 0x70
    destination_offset: int = 0x00
    source_offset: int = 0x08
    destination_check_offset: int = 0x10
    date_offset: int = 0x28
    person_database_id_offset: int = 0x2C
    fee_offset: int = 0x30
    loan_type_offset: int | None = None
    loan_type_values: tuple[int, ...] = ()


FM24_FUTURE_TRANSFERS = FutureTransferLayout(
    manager_vtable_rva=0x5B08480,
    manager_pointer_rva=0x6374650,
    full_offer_vtable_rva=0x5C85388,
    loan_offer_vtable_rva=0x5A86988,
    person_offset=0x30,
    buyer_offset=0x38,
    seller_offset=0x48,
    value_offset=0x50,
    transfer_id_offset=0x54,
    status_offset=0x7B,
)

FM26_FUTURE_TRANSFERS = FutureTransferLayout(
    manager_vtable_rva=0x4473EE0,
    manager_pointer_rva=0x4E35E20,
    full_offer_vtable_rva=0x470B128,
    loan_offer_vtable_rva=0x45EBC88,
    person_offset=0x38,
    buyer_offset=0x40,
    seller_offset=0x50,
    value_offset=0x58,
    transfer_id_offset=0x5C,
    status_offset=0x8D,
)


FM24_EPIC_FUTURE_TRANSFERS = replace(
    FM24_FUTURE_TRANSFERS,
    manager_pointer_rva=0,
)

FM26_TRANSFER_ARCHIVE = HistoricalTransferArchiveLayout(
    manager=FM26_FUTURE_TRANSFERS,
    source_name="fm26_native_transfer_archive",
)

# FM24 Epic 24.4.2+2081827 uses the same 0x70-byte archive row shape as the
# validated FM26 Steam profile, but remains a separate exact-build contract.
# In the live Epic save, byte +0x63 was 0x0A for the in-game row labelled as a
# loan (Wang Shaolong, 2037-07-07) and differed from the adjacent permanent
# and free-transfer rows.
FM24_EPIC_TRANSFER_ARCHIVE = HistoricalTransferArchiveLayout(
    manager=FM24_EPIC_FUTURE_TRANSFERS,
    source_name="fm24_native_transfer_archive",
    loan_type_offset=0x63,
    loan_type_values=(0x0A,),
)


_MANAGER_CACHE: dict[tuple[Any, ...], int] = {}
_MANAGER_CACHE_LOCK = threading.RLock()


def future_transfer_layout_for(game_layout: Any) -> FutureTransferLayout:
    identity = str(getattr(game_layout, "executable_sha256", "") or "").upper()
    distribution = str(getattr(game_layout, "distribution", "") or "").lower()
    if str(getattr(game_layout, "key", "")) == "fm26" and distribution == "xgp":
        vtables = (
            getattr(game_layout, "future_transfer_manager_vtable_rva", None),
            getattr(game_layout, "future_transfer_full_offer_vtable_rva", None),
            getattr(game_layout, "future_transfer_loan_offer_vtable_rva", None),
        )
        if any(value is None for value in vtables):
            raise RuntimeError("FM26 XGP 未来转会 RTTI 布局不完整")
        return replace(
            FM26_FUTURE_TRANSFERS,
            manager_vtable_rva=int(vtables[0]),
            full_offer_vtable_rva=int(vtables[1]),
            loan_offer_vtable_rva=int(vtables[2]),
            manager_pointer_rva=0,
        )
    if str(getattr(game_layout, "key", "")) == "fm24" and distribution == "xgp":
        vtables = (
            getattr(game_layout, "future_transfer_manager_vtable_rva", None),
            getattr(game_layout, "future_transfer_full_offer_vtable_rva", None),
            getattr(game_layout, "future_transfer_loan_offer_vtable_rva", None),
        )
        if any(value is None for value in vtables):
            raise RuntimeError("FM24 XGP 未来转会需要运行时 RTTI 布局")
        return replace(
            FM24_FUTURE_TRANSFERS,
            manager_vtable_rva=int(vtables[0]),
            full_offer_vtable_rva=int(vtables[1]),
            loan_offer_vtable_rva=int(vtables[2]),
            manager_pointer_rva=0,
        )
    if (
        str(getattr(game_layout, "key", "")) == "fm26"
        and distribution == "steam"
        and identity == FM26_EXE_SHA256
    ):
        return FM26_FUTURE_TRANSFERS
    if (
        str(getattr(game_layout, "key", "")) == "fm24"
        and distribution in {"steam", "epic"}
        and identity in {FM24_EXE_SHA256, FM24_EPIC_EXE_SHA256}
    ):
        return FM24_EPIC_FUTURE_TRANSFERS if distribution == "epic" else FM24_FUTURE_TRANSFERS
    raise RuntimeError("当前 Football Manager 版本尚未验证未来转会布局")


def resolve_future_transfer_layout(reader: Reader) -> FutureTransferLayout:
    """Resolve transfer object RTTI for protected layouts on first use."""
    try:
        return future_transfer_layout_for(reader.layout)
    except RuntimeError as error:
        if str(getattr(reader.layout, "key", "")) != "fm24":
            raise
        module = reader.layout.module(reader.process)
        if not module:
            raise RuntimeError(f"{reader.layout.module_name} 尚未加载") from error
        image = read_process_memory(reader.process, module.base_address, module.size)
        if not image:
            raise RuntimeError("无法读取 FM24 XGP 模块以解析未来转会 RTTI") from error
        sections = _pe_sections(image)
        names = {
            "manager": ".?AVTRANSFER_MANAGER@@",
            "full": ".?AVFULL_TRANSFER_OFFER@@",
            "loan": ".?AVLOAN_OFFER@@",
        }
        resolved = {
            key: _rtti_vtables(image, sections, name)
            for key, name in names.items()
        }
        if any(len(values) != 1 for values in resolved.values()):
            raise RuntimeError(
                "FM24 XGP 未来转会 RTTI 不完整或存在歧义"
            ) from error
        layout = replace(
            reader.layout,
            future_transfer_manager_vtable_rva=resolved["manager"][0],
            future_transfer_full_offer_vtable_rva=resolved["full"][0],
            future_transfer_loan_offer_vtable_rva=resolved["loan"][0],
        )
        return replace(
            FM24_FUTURE_TRANSFERS,
            manager_vtable_rva=int(layout.future_transfer_manager_vtable_rva),
            full_offer_vtable_rva=int(layout.future_transfer_full_offer_vtable_rva),
            loan_offer_vtable_rva=int(layout.future_transfer_loan_offer_vtable_rva),
            manager_pointer_rva=0,
        )


def historical_transfer_archive_layout_for(
    game_layout: Any,
) -> HistoricalTransferArchiveLayout:
    identity = str(getattr(game_layout, "executable_sha256", "") or "").upper()
    distribution = str(getattr(game_layout, "distribution", "") or "").lower()
    key = str(getattr(game_layout, "key", ""))
    if key == "fm26" and distribution == "steam" and identity == FM26_EXE_SHA256:
        return FM26_TRANSFER_ARCHIVE
    if key == "fm24" and distribution == "epic" and identity == FM24_EPIC_EXE_SHA256:
        return FM24_EPIC_TRANSFER_ARCHIVE
    raise RuntimeError("当前 Football Manager 版本尚未验证历史转会归档布局")


def _cache_key(reader: Reader, save_identity: str) -> tuple[Any, ...]:
    layout = reader.layout
    return (
        int(reader.process.pid),
        int(reader.module_base),
        str(layout.key),
        str(layout.distribution),
        str(layout.executable_sha256),
        str(getattr(layout, "game_version", "") or ""),
        str(save_identity or ""),
    )


def _vector_header(
    reader: Reader, address: int, *, maximum_count: int,
) -> tuple[int, int, int, int] | None:
    raw = reader.bytes(address, 24)
    if not raw or len(raw) != 24:
        return None
    begin, end, capacity = struct.unpack("<QQQ", raw)
    if not begin or end < begin or capacity < end:
        return None
    if any(value % 8 for value in (begin, end, capacity)):
        return None
    if (end - begin) % 8 or (capacity - begin) % 8:
        return None
    count = (end - begin) // 8
    capacity_count = (capacity - begin) // 8
    if count > maximum_count or capacity_count > maximum_count:
        return None
    return begin, end, capacity, count


def _vector_pointers(
    reader: Reader, address: int, *, maximum_count: int,
) -> tuple[list[int], tuple[int, int, int, int]]:
    header = _vector_header(reader, address, maximum_count=maximum_count)
    if header is None:
        raise RuntimeError("未来转会容器已经失效")
    begin, end, _capacity, count = header
    if not count:
        return [], header
    raw = reader.bytes(begin, end - begin)
    if not raw or len(raw) != end - begin:
        raise RuntimeError("无法读取未来转会容器")
    return [value for value in struct.unpack(f"<{count}Q", raw) if value], header


def _manager_candidate_score(
    reader: Reader, address: int, layout: FutureTransferLayout,
) -> int | None:
    if reader.ptr(address) != reader.module_base + layout.manager_vtable_rva:
        return None
    offers = _vector_header(
        reader, address + layout.offers_vector_offset, maximum_count=500_000,
    )
    dates = _vector_header(
        reader, address + layout.dates_vector_offset, maximum_count=100_000,
    )
    if offers is None or dates is None:
        return None
    offer_count = offers[3]
    date_count = dates[3]
    if not offer_count and not date_count:
        return 1

    score = min(offer_count, 20_000) + date_count * 20
    allowed_vtables = {
        reader.module_base + layout.full_offer_vtable_rva,
        reader.module_base + layout.loan_offer_vtable_rva,
    }
    if offer_count:
        sample_count = min(16, offer_count)
        raw = reader.bytes(offers[0], sample_count * 8)
        if not raw or len(raw) != sample_count * 8:
            return None
        pointers = struct.unpack(f"<{sample_count}Q", raw)
        valid = sum(1 for pointer in pointers if reader.ptr(pointer) in allowed_vtables)
        if not valid:
            return None
        score += valid * 10_000
    if date_count:
        sample_count = min(16, date_count)
        raw = reader.bytes(dates[0], sample_count * 8)
        if not raw or len(raw) != sample_count * 8:
            return None
        valid_dates = 0
        for pointer in struct.unpack(f"<{sample_count}Q", raw):
            record = reader.bytes(pointer, layout.date_record_size)
            if not record or len(record) != layout.date_record_size:
                continue
            person, date_code, transfer_id = struct.unpack("<QII", record)
            if person and decode_date(date_code):
                valid_dates += 1
        if not valid_dates:
            return None
        score += valid_dates * 20_000
    return score


def _discover_transfer_manager(
    reader: Reader, layout: FutureTransferLayout,
) -> int:
    needle = struct.pack("<Q", reader.module_base + layout.manager_vtable_rva)
    candidates: list[tuple[int, int]] = []
    for region in iter_readable_regions(reader.process):
        if region.type != MEM_PRIVATE:
            continue
        carry = b""
        for offset in range(0, region.size, 8 * 1024 * 1024):
            size = min(8 * 1024 * 1024, region.size - offset)
            raw = read_process_memory(reader.process, region.base_address + offset, size)
            if not raw:
                carry = b""
                continue
            scan = carry + raw
            scan_base = region.base_address + offset - len(carry)
            start = scan.find(needle)
            while start >= 0:
                address = scan_base + start
                if address % 8 == 0:
                    score = _manager_candidate_score(reader, address, layout)
                    if score is not None:
                        candidates.append((score, address))
                start = scan.find(needle, start + 1)
            carry = scan[-7:]
    if not candidates:
        raise RuntimeError("无法定位当前存档的未来转会管理器")
    candidates.sort(reverse=True)
    return candidates[0][1]


def locate_transfer_manager(
    reader: Reader, save_identity: str = "", native_layout: FutureTransferLayout | None = None,
) -> tuple[int, bool]:
    layout = native_layout or resolve_future_transfer_layout(reader)
    key = _cache_key(reader, save_identity)
    with _MANAGER_CACHE_LOCK:
        cached = _MANAGER_CACHE.get(key)
    if cached and _manager_candidate_score(reader, cached, layout) is not None:
        return cached, True

    direct = (
        reader.ptr(reader.module_base + layout.manager_pointer_rva)
        if layout.manager_pointer_rva else None
    )
    manager = (
        int(direct)
        if direct and _manager_candidate_score(reader, int(direct), layout) is not None
        else _discover_transfer_manager(reader, layout)
    )
    with _MANAGER_CACHE_LOCK:
        for old_key in list(_MANAGER_CACHE):
            if old_key[:2] == key[:2] and old_key != key:
                _MANAGER_CACHE.pop(old_key, None)
        _MANAGER_CACHE[key] = manager
    return manager, False


def _person_identity(reader: Reader, person: int) -> dict[str, Any]:
    uid = int(reader.u32(person + ENTITY_UID) or 0)
    layout = reader.layout
    common = reader.fm_nested_string_at(person + layout.person_common_name_offset)
    first = reader.fm_nested_string_at(person + layout.person_first_name_offset)
    last = reader.fm_nested_string_at(person + layout.person_last_name_offset)
    full = reader.fm_string_at(person + layout.person_full_name_offset)
    if str(layout.key) == "fm24":
        first = reader.fm_nested_string_at(person + 0x58) or first
        last = reader.fm_nested_string_at(person + 0x60) or last
        common = reader.fm_nested_string_at(person + 0x68) or common
        full = reader.fm_string_at(person + 0x48) or full
    joined = " ".join(part for part in (first, last) if part)
    localized = next(
        (
            value for value in (full, common, joined)
            if value and any("\u3400" <= char <= "\u9fff" for char in value)
        ),
        None,
    )
    return {"id": uid or None, "name": localized or common or joined or full or "球员未读取"}


def _archive_player_identities(
    reader: Reader, database_ids: set[int],
) -> dict[int, dict[str, Any]]:
    """Resolve archive database IDs through verified live Player/Person pairs."""
    wanted = {int(value) for value in database_ids if int(value) > 0}
    if not wanted:
        return {}
    directory = database_index_for_reader(reader, force_retry=True)
    if directory is None:
        raise RuntimeError("当前版本尚未建立可用的 Person 数据库目录")
    targets = directory.player_targets()
    person_addresses = [int(target[0]) for target in targets.values()]
    snapshots = reader._fixed_size_snapshots(person_addresses, 0x10)
    resolved: dict[int, dict[str, Any]] = {}
    ambiguous: set[int] = set()
    for uid, target in targets.items():
        person = int(target[0])
        raw = snapshots.get(person)
        if not raw or len(raw) < 0x10:
            continue
        database_id = int(struct.unpack_from("<I", raw, 0x08)[0])
        stored_uid = int(struct.unpack_from("<I", raw, ENTITY_UID)[0])
        if database_id not in wanted or stored_uid != int(uid):
            continue
        identity = _person_identity(reader, person)
        if int(identity.get("id") or 0) != int(uid):
            continue
        if database_id in resolved:
            ambiguous.add(database_id)
            continue
        resolved[database_id] = identity
    for database_id in ambiguous:
        resolved.pop(database_id, None)
    return resolved


def read_historical_transfer_archive(
    reader: Reader, team_ids: Any, *, save_identity: str = "",
) -> dict[str, Any]:
    """Read an exact-build native transfer archive for selected Team UIDs."""
    layout = reader.layout
    archive_layout = historical_transfer_archive_layout_for(layout)

    requested_ids = sorted({int(value) for value in team_ids if int(value) > 0})
    if not requested_ids:
        return {
            "transfers": [], "clubs": [], "native_history_complete": False,
            "unresolved_count": 0,
        }

    selected_by_address: dict[int, dict[str, Any]] = {}
    unresolved_teams = 0
    for team_id in requested_ids:
        try:
            pair = resolve_team_club(reader, team_id=team_id)
            team = reader.team(int(pair.team_address))
        except (OSError, RuntimeError, TypeError, ValueError):
            team = None
            pair = None
        if not team or pair is None or int(team.get("id") or 0) != team_id:
            unresolved_teams += 1
            continue
        selected_by_address[int(pair.team_address)] = {
            "id": team_id,
            "name": str(team.get("name") or team.get("short_name") or team_id),
        }
    if not selected_by_address:
        raise RuntimeError("无法按俱乐部 UID 定位历史转会目标")

    manager, cache_hit = locate_transfer_manager(
        reader, save_identity, archive_layout.manager,
    )
    pointers, header = _vector_pointers(
        reader, manager + archive_layout.vector_offset,
        maximum_count=500_000,
    )
    snapshots = reader._fixed_size_snapshots(
        pointers, archive_layout.record_size,
    )
    candidates: list[dict[str, Any]] = []
    database_ids: set[int] = set()
    unresolved_records = 0
    for address in pointers:
        raw = snapshots.get(int(address))
        if not raw or len(raw) != archive_layout.record_size:
            unresolved_records += 1
            continue
        destination = int(struct.unpack_from(
            "<Q", raw, archive_layout.destination_offset,
        )[0])
        source = int(struct.unpack_from(
            "<Q", raw, archive_layout.source_offset,
        )[0])
        destination_check = int(struct.unpack_from(
            "<Q", raw, archive_layout.destination_check_offset,
        )[0])
        selected_directions = []
        if destination in selected_by_address:
            selected_directions.append(("in", destination, source))
        if source in selected_by_address:
            selected_directions.append(("out", source, destination))
        if not selected_directions:
            continue
        date_code = int(struct.unpack_from(
            "<I", raw, archive_layout.date_offset,
        )[0])
        transfer_date = decode_date(date_code)
        database_id = int(struct.unpack_from(
            "<I", raw, archive_layout.person_database_id_offset,
        )[0])
        fee = int(struct.unpack_from(
            "<I", raw, archive_layout.fee_offset,
        )[0])
        is_loan = bool(
            archive_layout.loan_type_offset is not None
            and raw[archive_layout.loan_type_offset] in archive_layout.loan_type_values
        )
        if destination <= 0 or destination_check != destination or not transfer_date or database_id <= 0:
            unresolved_records += len(selected_directions)
            continue
        database_ids.add(database_id)
        for direction, selected_address, counterparty_address in selected_directions:
            candidates.append({
                "record_address": int(address),
                "direction": direction,
                "team_address": selected_address,
                "counterparty_address": counterparty_address,
                "date": transfer_date.isoformat(),
                "database_id": database_id,
                "fee": fee,
                "is_loan": is_loan,
                "source_team_address": source,
                "destination_team_address": destination,
            })

    latest_pointers, latest_header = _vector_pointers(
        reader, manager + archive_layout.vector_offset,
        maximum_count=500_000,
    )
    if latest_header != header or latest_pointers != pointers:
        raise RuntimeError("历史转会归档在读取期间发生变化，请重试")

    players = _archive_player_identities(reader, database_ids)
    team_cache: dict[int, dict[str, Any] | None] = {}
    rows: list[dict[str, Any]] = []
    for candidate in candidates:
        player = players.get(int(candidate["database_id"]))
        if not player:
            unresolved_records += 1
            continue
        counterparty_address = int(candidate["counterparty_address"])
        if counterparty_address:
            if counterparty_address not in team_cache:
                team_cache[counterparty_address] = reader.team(counterparty_address)
            counterparty = team_cache[counterparty_address]
            if not counterparty:
                unresolved_records += 1
        else:
            counterparty = None
        selected = selected_by_address[int(candidate["team_address"])]
        source_address = int(candidate["source_team_address"])
        destination_address = int(candidate["destination_team_address"])
        source_team = (
            selected_by_address.get(source_address)
            or team_cache.get(source_address)
            or (reader.team(source_address) if source_address else None)
        )
        destination_team = (
            selected_by_address.get(destination_address)
            or team_cache.get(destination_address)
            or (reader.team(destination_address) if destination_address else None)
        )
        source_id = int((source_team or {}).get("id") or 0)
        destination_id = int((destination_team or {}).get("id") or 0)
        direction = str(candidate["direction"])
        rows.append({
            "id": (
                f'native:{candidate["date"]}:{candidate["database_id"]}:'
                f'{source_id}:{destination_id}:{direction}:{selected["id"]}'
            ),
            "direction": direction,
            "player_id": int(player["id"]),
            "player_name": str(player["name"]),
            "team_id": int(selected["id"]),
            "team_name": str(selected["name"]),
            "counterparty_id": int((counterparty or {}).get("id") or 0) or None,
            "counterparty_kind": "free_agent" if not counterparty_address else "club",
            "counterparty_name": (
                str((counterparty or {}).get("name") or (counterparty or {}).get("short_name") or "").strip()
                or None
            ),
            "date": str(candidate["date"]),
            "year": str(candidate["date"])[:4],
            "date_source": archive_layout.source_name,
            "fee": int(candidate["fee"]),
            "native_fee": int(candidate["fee"]),
            "fee_status": (
                "loan" if bool(candidate["is_loan"])
                else "free" if int(candidate["fee"]) == 0 else "known"
            ),
            "transfer_type": "loan" if bool(candidate["is_loan"]) else "permanent",
            "fee_source": archive_layout.source_name,
            "source": archive_layout.source_name,
        })
    rows.sort(key=lambda row: (
        str(row.get("date") or ""), str(row.get("player_name") or "").casefold(),
        str(row.get("id") or ""),
    ), reverse=True)
    return {
        "transfers": rows,
        "clubs": sorted(selected_by_address.values(), key=lambda row: str(row["name"]).casefold()),
        "native_history_complete": unresolved_teams == 0 and unresolved_records == 0,
        "unresolved_count": int(unresolved_teams + unresolved_records),
        "manager_cache_hit": bool(cache_hit),
        "game_version": str(layout.game_version or layout.display_name),
    }


def _confirmed_transfer_rows(
    reader: Reader, manager: int, layout: FutureTransferLayout,
    *, team_id: int, team_address: int,
) -> list[dict[str, Any]]:
    target = reader.team(team_address)
    if not target or int(target.get("id") or 0) != int(team_id):
        raise RuntimeError("俱乐部地址已失效，请重新扫描俱乐部")

    date_pointers, _ = _vector_pointers(
        reader, manager + layout.dates_vector_offset, maximum_count=100_000,
    )
    dates: dict[tuple[int, int], str] = {}
    for pointer in date_pointers:
        record = reader.bytes(pointer, layout.date_record_size)
        if not record or len(record) != layout.date_record_size:
            continue
        person, date_code, transfer_id = struct.unpack("<QII", record)
        transfer_date = decode_date(date_code)
        if person and transfer_date:
            dates[(int(transfer_id), int(person))] = transfer_date.isoformat()

    offer_pointers, _ = _vector_pointers(
        reader, manager + layout.offers_vector_offset, maximum_count=500_000,
    )
    full_vtable = reader.module_base + layout.full_offer_vtable_rva
    loan_vtable = reader.module_base + layout.loan_offer_vtable_rva
    rows: list[dict[str, Any]] = []
    for address in offer_pointers:
        raw = reader.bytes(address, layout.offer_read_size)
        if not raw or len(raw) != layout.offer_read_size:
            continue
        vtable = struct.unpack_from("<Q", raw, 0)[0]
        if vtable not in {full_vtable, loan_vtable}:
            continue
        if raw[layout.status_offset] != CONFIRMED_STATUS:
            continue
        person = struct.unpack_from("<Q", raw, layout.person_offset)[0]
        buyer = struct.unpack_from("<Q", raw, layout.buyer_offset)[0]
        seller = struct.unpack_from("<Q", raw, layout.seller_offset)[0]
        value = struct.unpack_from("<I", raw, layout.value_offset)[0]
        transfer_id = struct.unpack_from("<I", raw, layout.transfer_id_offset)[0]
        transfer_date = dates.get((int(transfer_id), int(person)))
        if not transfer_date:
            continue
        if buyer == team_address:
            direction = "incoming"
        elif seller == team_address:
            direction = "outgoing"
        else:
            continue
        buyer_team = reader.team(buyer) if buyer else None
        seller_team = reader.team(seller) if seller else None
        player = _person_identity(reader, person)
        rows.append({
            "offer_address": hex(address),
            "transfer_id": int(transfer_id),
            "player_id": player["id"],
            "player_name": player["name"],
            "direction": direction,
            "buyer_id": int(buyer_team.get("id") or 0) if buyer_team else None,
            "buyer_name": str(buyer_team.get("name") or "买方未读取") if buyer_team else "买方未读取",
            "seller_id": int(seller_team.get("id") or 0) if seller_team else None,
            "seller_name": str(seller_team.get("name") or "自由球员") if seller_team else "自由球员",
            "fee": int(value),
            "transfer_type": "loan" if vtable == loan_vtable else "full",
            "transfer_type_name": "租借" if vtable == loan_vtable else "正式转会",
            "transfer_date": transfer_date,
            "status": CONFIRMED_STATUS,
        })
    rows.sort(key=lambda row: (str(row["transfer_date"]), str(row["player_name"]), int(row["transfer_id"])))
    return rows


def _resolve_target_team_address(
    reader: Reader, team_id: int, team_address: Any,
) -> int:
    """Resolve a current Team address by UID, retaining legacy Reader fallback."""
    try:
        hint = int(str(team_address or "0"), 0)
    except (TypeError, ValueError):
        hint = 0
    if int(team_id or 0) <= 0:
        raise ValueError("俱乐部标识无效")
    try:
        resolved = resolve_team_club(reader, hint, int(team_id))
        return int(resolved.team_address)
    except (OSError, RuntimeError, TypeError, ValueError) as resolver_error:
        # Older/fake readers may not expose the database index or the layout
        # vtables yet.  Preserve their previously validated address path, but
        # never accept it without the existing Team UID check.
        if hint:
            target = reader.team(hint)
            if target and int(target.get("id") or 0) == int(team_id):
                return hint
        raise RuntimeError("俱乐部地址已失效，请重新扫描俱乐部") from resolver_error


def read_future_transfers(
    team_id: int, team_address: Any, *, save_identity: str = "",
) -> dict[str, Any]:
    if int(team_id or 0) <= 0:
        raise ValueError("俱乐部标识无效")
    pid, _path, game_layout = select_process_layout()
    with open_process(pid) as process:
        module = game_layout.module(process)
        if not module:
            raise RuntimeError(f"{game_layout.module_name} not loaded")
        reader = Reader(process, module.base_address, game_layout)
        address = _resolve_target_team_address(reader, int(team_id), team_address)
        native_layout = resolve_future_transfer_layout(reader)
        manager, cache_hit = locate_transfer_manager(reader, save_identity)
        rows = _confirmed_transfer_rows(
            reader, manager, native_layout, team_id=int(team_id), team_address=address,
        )
        return {
            "transfers": rows,
            "count": len(rows),
            "manager_cache_hit": cache_hit,
            "game_version": str(game_layout.game_version or game_layout.display_name),
        }


def has_confirmed_future_transfer(
    reader: Reader, person: int, *, save_identity: str = "",
) -> bool:
    """Return whether a person is owned by a still-confirmed native offer."""
    if not int(person):
        return False
    native_layout = resolve_future_transfer_layout(reader)
    manager, _cache_hit = locate_transfer_manager(reader, save_identity)
    offer_pointers, _ = _vector_pointers(
        reader,
        manager + native_layout.offers_vector_offset,
        maximum_count=500_000,
    )
    full_vtable = reader.module_base + native_layout.full_offer_vtable_rva
    loan_vtable = reader.module_base + native_layout.loan_offer_vtable_rva
    for address in offer_pointers:
        raw = reader.bytes(address, native_layout.offer_read_size)
        if not raw or len(raw) != native_layout.offer_read_size:
            continue
        if struct.unpack_from("<Q", raw, 0)[0] not in {full_vtable, loan_vtable}:
            continue
        if raw[native_layout.status_offset] != CONFIRMED_STATUS:
            continue
        if struct.unpack_from("<Q", raw, native_layout.person_offset)[0] == int(person):
            return True
    return False


def _find_matching_date(
    reader: Reader, manager: int, layout: FutureTransferLayout,
    transfer_id: int, person: int,
) -> bool:
    pointers, _ = _vector_pointers(
        reader, manager + layout.dates_vector_offset, maximum_count=100_000,
    )
    for pointer in pointers:
        record = reader.bytes(pointer, layout.date_record_size)
        if not record or len(record) != layout.date_record_size:
            continue
        record_person, date_code, record_id = struct.unpack("<QII", record)
        if record_id == transfer_id and record_person == person and decode_date(date_code):
            return True
    return False


def cancel_future_transfer(
    team_id: int, team_address: Any, transfer_id: int, offer_address: Any,
    *, save_identity: str = "",
) -> dict[str, Any]:
    try:
        expected_offer = int(str(offer_address or "0"), 0)
    except (TypeError, ValueError) as error:
        raise ValueError("未来转会地址无效") from error
    if int(team_id) <= 0 or int(transfer_id) < 0 or not expected_offer:
        raise ValueError("未来转会标识无效")

    pid, _path, game_layout = select_process_layout()
    with open_process(pid, write_memory=True) as process:
        module = game_layout.module(process)
        if not module:
            raise RuntimeError(f"{game_layout.module_name} not loaded")
        reader = Reader(process, module.base_address, game_layout)
        target_address = _resolve_target_team_address(reader, int(team_id), team_address)
        native_layout = resolve_future_transfer_layout(reader)
        target = reader.team(target_address)
        if not target or int(target.get("id") or 0) != int(team_id):
            raise RuntimeError("俱乐部地址已失效，请重新扫描俱乐部")
        manager, _cache_hit = locate_transfer_manager(reader, save_identity)
        offer_pointers, _ = _vector_pointers(
            reader, manager + native_layout.offers_vector_offset, maximum_count=500_000,
        )
        if expected_offer not in set(offer_pointers):
            raise RuntimeError("未来转会对象已经失效，请重新读取")
        raw = reader.bytes(expected_offer, native_layout.offer_read_size)
        if not raw or len(raw) != native_layout.offer_read_size:
            raise RuntimeError("无法读取未来转会对象")
        allowed_vtables = {
            reader.module_base + native_layout.full_offer_vtable_rva,
            reader.module_base + native_layout.loan_offer_vtable_rva,
        }
        if struct.unpack_from("<Q", raw, 0)[0] not in allowed_vtables:
            raise RuntimeError("未来转会对象类型已经变化")
        current_id = struct.unpack_from("<I", raw, native_layout.transfer_id_offset)[0]
        status = raw[native_layout.status_offset]
        person = struct.unpack_from("<Q", raw, native_layout.person_offset)[0]
        buyer = struct.unpack_from("<Q", raw, native_layout.buyer_offset)[0]
        seller = struct.unpack_from("<Q", raw, native_layout.seller_offset)[0]
        if current_id != int(transfer_id):
            raise RuntimeError("未来转会 ID 已变化，请重新读取")
        if status != CONFIRMED_STATUS:
            raise ValueError("该未来转会已经不是已确认状态")
        if target_address not in {buyer, seller}:
            raise RuntimeError("该未来转会不属于当前俱乐部")
        if not _find_matching_date(
            reader, manager, native_layout, int(transfer_id), int(person),
        ):
            raise RuntimeError("未来转会日期或球员身份已经变化，请重新读取")

        status_address = expected_offer + native_layout.status_offset
        try:
            write_process_memory(process, status_address, bytes([CANCELLED_STATUS]))
        except Exception as write_error:
            current = read_process_memory(process, status_address, 1)
            if current != bytes([CONFIRMED_STATUS]):
                try:
                    write_process_memory(process, status_address, bytes([CONFIRMED_STATUS]))
                    if read_process_memory(process, status_address, 1) != bytes([CONFIRMED_STATUS]):
                        raise RuntimeError("恢复后的状态回读不一致")
                except Exception as rollback_error:
                    raise RuntimeError(
                        f"叫停写入异常且状态恢复失败：{write_error}；{rollback_error}"
                    ) from write_error
            raise RuntimeError(f"叫停写入失败，原状态已确认：{write_error}") from write_error
        readback = read_process_memory(process, status_address, 1)
        if readback != bytes([CANCELLED_STATUS]):
            rollback_error: Exception | None = None
            try:
                write_process_memory(process, status_address, bytes([CONFIRMED_STATUS]))
                if read_process_memory(process, status_address, 1) != bytes([CONFIRMED_STATUS]):
                    raise RuntimeError("恢复后的状态回读不一致")
            except Exception as error:
                rollback_error = error
            if rollback_error:
                raise RuntimeError(f"叫停写入失败且状态恢复异常：{rollback_error}")
            raise RuntimeError("叫停写入回读失败，已恢复原状态")
        return {
            "cancelled": True,
            "team_id": int(team_id),
            "transfer_id": int(transfer_id),
            "offer_address": hex(expected_offer),
            "status_before": CONFIRMED_STATUS,
            "status_after": CANCELLED_STATUS,
        }


__all__ = [
    "CANCELLED_STATUS",
    "CONFIRMED_STATUS",
    "FM24_FUTURE_TRANSFERS",
    "FM24_EPIC_FUTURE_TRANSFERS",
    "FM26_FUTURE_TRANSFERS",
    "FutureTransferLayout",
    "cancel_future_transfer",
    "future_transfer_layout_for",
    "has_confirmed_future_transfer",
    "locate_transfer_manager",
    "read_historical_transfer_archive",
    "read_future_transfers",
]
