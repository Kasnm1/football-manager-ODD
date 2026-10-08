"""Relationship-record parsing, pair/bidirectional reads and transactional writes.

The FM relationship vector is a per-Person container reached through
``layout.person_relationships_offset``.  Its header is a 24-byte ``<QQQ``
triplet (begin/end/capacity); each entry is a 16-byte record:

  [0x00:8] Target ptr (person/nation/club)
  [0x08:2] Reason (uint16 LE) — relationship category
  [0x0A]   ObjectType  (1=Club, 3=Person, 8=Nation)
  [0x0B]   RelationType (1=LikesPerson, others=Dislikes)
  [0x0C]   Level (0-100)
  [0x0D]   Permanence (79=Permanent)
  [0x0E]   padding
  [0x0F]   Termination marker (0xFF)

This module is the 2.3.3-owned port of the FModd 2.1.2 relationship
ecosystem core.  It deliberately reuses the 2.1.2 parsing/decision rules
(``relation_type`` decides the sign, not ``level``; existing records keep
their reason on update) while adapting every entry point to the 2.3.3
resident ``Reader`` model: ``borrow_game_reader()`` sessions, per-session
``session_generation`` cache keys and the current ``GameLayout`` offsets.
Nothing here selects a process or opens a handle itself; callers pass a
Reader (read) or use ``write_bidirectional_intimacy`` / the writable
helpers which reuse ``club_reader._writable_game_reader``.
"""

from __future__ import annotations

import struct
import threading
from copy import deepcopy
from typing import Any

from fm_collector.win32 import write_process_memory
from tools.club_reader import (
    COACHING_LICENSE_NAMES, ENTITY_UID, POSITION_NAMES, STAFF_JOB_TYPES,
    _address, _contract, _name, _remote_free_block, _remote_malloc_block,
    _writable_game_reader,
)

# ── Relationship vector bounds (2.3.3-verified) ──────────────────────────
# The 2.1.2 read path capped at 16*512 records; 2.3.3's own write path
# (adjust_manager_intimacy_points / _person_relation_container) validates
# against 16*1024.  The max record count is decided by the target version's
# implementation, so reads follow 16*1024 as well.
REL_VECTOR_MAX = 16 * 1024


# ── Per-session LRU caches ──────────────────────────────────────────────
# Keys are scoped by session_generation so a save reload, reconnect or FM
# restart invalidates every stale address immediately.
_REL_BLOCK_CACHE: dict[tuple[Any, ...], dict[str, Any]] = {}
_REL_BLOCK_CACHE_MAX = 512
_PUBLIC_PROFILE_CACHE: dict[tuple[int, int], dict[str, Any] | None] = {}
_PUBLIC_PROFILE_CACHE_MAX = 1024
_CACHE_LOCK = threading.RLock()


def _reader_cache_key(reader: Any) -> int:
    """Per-session identifier for cache keys.

    Uses the resident session generation when present (2.3.3 model), falling
    back to the process handle identity so request-local Readers created
    outside a GameSession still isolate per process.
    """
    generation = int(getattr(reader, "session_generation", 0) or 0)
    if generation:
        return generation
    return id(reader.process)


def invalidate_relationship_caches() -> None:
    """Drop all relationship/profile caches (process switch, save switch)."""
    with _CACHE_LOCK:
        _REL_BLOCK_CACHE.clear()
        _PUBLIC_PROFILE_CACHE.clear()


def _trim_public_profile_cache() -> None:
    with _CACHE_LOCK:
        if len(_PUBLIC_PROFILE_CACHE) >= _PUBLIC_PROFILE_CACHE_MAX:
            _PUBLIC_PROFILE_CACHE.pop(next(iter(_PUBLIC_PROFILE_CACHE)))


# ── Read helpers ─────────────────────────────────────────────────────────

def read_person_relationships(
    reader: Any, person: int, manager_person: int = 0, *,
    scope_id: str = "", data_version: int = 0,
) -> dict[str, Any]:
    """Read one person's full relationship vector once.

    Returns manager intimacy plus the complete signed relation maps and raw
    per-target detail (reason/object_type/relation_type/permanence).  The
    ENTIRE container is traversed without team filtering; records whose target
    is only a UID are preserved at the data layer — the frontend decides
    visibility.  The signed score is derived from ``relation_type`` (1 = liked,
    otherwise disliked), never from ``level``.
    """
    result: dict[str, Any] = {
        "manager_intimacy": 0, "person_relations": {},
        "person_relations_detail": {},
        "records": [], "records_by_target": {},
        "manager_relation_reason": None, "manager_relation_permanence": None,
        "manager_relation_object_type": None, "manager_relation_type": None,
    }
    cache_key = (
        str(scope_id), int(data_version),
        _reader_cache_key(reader), int(person), int(manager_person or 0),
    )
    with _CACHE_LOCK:
        cached = _REL_BLOCK_CACHE.get(cache_key)
        if cached is not None:
            return deepcopy(cached)
    offset = reader.layout.person_relationships_offset
    if offset is None:
        return result
    relationships = reader.ptr(person + offset)
    if not relationships:
        return result
    begin = reader.ptr(relationships)
    end = reader.ptr(relationships + 8)
    capacity = reader.ptr(relationships + 16)
    if (
        not begin or not end or not capacity
        or begin >= end or end > capacity
        or (end - begin) % 16 or (end - begin) > REL_VECTOR_MAX
    ):
        return result
    byte_length = end - begin
    raw = reader.bytes(begin, byte_length)
    if not raw or len(raw) != byte_length:
        return result
    for record_offset in range(0, byte_length, 16):
        target_id = struct.unpack_from("<Q", raw, record_offset)[0]
        if not target_id:
            continue
        score = raw[record_offset + 12]
        if score > 100:
            continue
        reason = struct.unpack_from("<H", raw, record_offset + 8)[0]
        object_type = raw[record_offset + 0x0A]
        relation_type = raw[record_offset + 0x0B]
        permanence = raw[record_offset + 0x0D]
        signed_score = int(score) if relation_type == 1 else -int(score)
        target_key = hex(target_id)
        record = {
            "target_key": target_key,
            "target_address": int(target_id),
            "reason": int(reason),
            "object_type": int(object_type),
            "relation_type": int(relation_type),
            "level": int(score),
            "signed_score": signed_score,
            "permanence": int(permanence),
            "record_address": int(begin + record_offset),
        }
        result["records"].append(record)
        result["records_by_target"][target_key] = record
        result["person_relations"][target_key] = signed_score
        result["person_relations_detail"][target_key] = {
            "score": signed_score,
            "level": int(score),
            "reason": int(reason),
            "object_type": int(object_type),
            "relation_type": int(relation_type),
            "permanence": int(permanence),
        }
        if manager_person and target_id == manager_person and 0 <= score <= 100:
            result["manager_intimacy"] = int(score)
            result["manager_relation_reason"] = int(reason)
            result["manager_relation_permanence"] = int(permanence)
            result["manager_relation_object_type"] = int(object_type)
            result["manager_relation_type"] = int(relation_type)
    with _CACHE_LOCK:
        if len(_REL_BLOCK_CACHE) >= _REL_BLOCK_CACHE_MAX:
            _REL_BLOCK_CACHE.pop(next(iter(_REL_BLOCK_CACHE)))
        _REL_BLOCK_CACHE[cache_key] = deepcopy(result)
    return result


def read_person_relations(reader: Any, person: int) -> dict[str, int]:
    """Return ``{target_key: signed_score}`` for a person (read-only shortcut)."""
    block = read_person_relationships(reader, int(person), 0)
    return dict(block.get("person_relations") or {})


def _read_person_pair_detail(
    reader: Any, person_from: int, person_to: int,
) -> dict[str, int] | None:
    """Exact single-direction lookup of person_from → person_to."""
    offset = reader.layout.person_relationships_offset
    if offset is None:
        return None
    relationships = reader.ptr(person_from + offset)
    if not relationships:
        return None
    begin = reader.ptr(relationships)
    end = reader.ptr(relationships + 8)
    capacity = reader.ptr(relationships + 16)
    if (
        not begin or not end or not capacity
        or begin >= end or end > capacity
        or (end - begin) % 16 or (end - begin) > REL_VECTOR_MAX
    ):
        return None
    byte_length = end - begin
    raw = reader.bytes(begin, byte_length)
    if raw and len(raw) == byte_length:
        for record_offset in range(0, byte_length, 16):
            if struct.unpack_from("<Q", raw, record_offset)[0] != person_to:
                continue
            score = raw[record_offset + 12]
            if score > 100:
                continue
            relation_type = raw[record_offset + 0x0B]
            reason = struct.unpack_from("<H", raw, record_offset + 8)[0]
            permanence = raw[record_offset + 0x0D]
            signed_score = int(score) if relation_type == 1 else -int(score)
            return {
                "signed_score": signed_score, "score": signed_score,
                "level": int(score), "reason": int(reason),
                "relation_type": int(relation_type),
                "permanence": int(permanence),
                "permanent": int(permanence) == 79,
            }
    return None


def read_relationship_pair(
    reader: Any, person_a: int, person_b: int,
) -> dict[str, Any]:
    """Read both directions between two persons.

    Returns ``{a_to_b, b_to_a}``; either side may be ``None`` when no record
    exists.  Pages must never average or take ``max(a_to_b, b_to_a)`` — the
    two directions can disagree in reason and sign by design.
    """
    return {
        "a_to_b": _read_person_pair_detail(reader, int(person_a), int(person_b)),
        "b_to_a": _read_person_pair_detail(reader, int(person_b), int(person_a)),
    }


def read_manager_relations_map(
    reader: Any, manager_person: int,
) -> dict[int, dict[str, Any]]:
    """Read the manager's own relationship container indexed by target person.

    FM keeps staff→manager relationship records sparse (verified on FM24: a
    club's staff containers contain no record pointing at the human manager),
    while the manager's own container holds the meaningful manager→staff
    records.  Staff intimacy therefore reads through the manager's container.
    Every valid non-zero target is preserved; the frontend decides visibility.
    """
    result: dict[int, dict[str, Any]] = {}
    offset = reader.layout.person_relationships_offset
    if offset is None:
        return result
    relationships = reader.ptr(manager_person + offset)
    if not relationships:
        return result
    begin = reader.ptr(relationships)
    end = reader.ptr(relationships + 8)
    capacity = reader.ptr(relationships + 16)
    if (
        not begin or not end or not capacity
        or begin >= end or end > capacity
        or (end - begin) % 16 or (end - begin) > REL_VECTOR_MAX
    ):
        return result
    byte_length = end - begin
    raw = reader.bytes(begin, byte_length)
    if not raw or len(raw) != byte_length:
        return result
    for record_offset in range(0, byte_length, 16):
        target_id = struct.unpack_from("<Q", raw, record_offset)[0]
        if not target_id:
            continue
        score = raw[record_offset + 12]
        if score > 100:
            continue
        reason = struct.unpack_from("<H", raw, record_offset + 8)[0]
        object_type = raw[record_offset + 0x0A]
        relation_type = raw[record_offset + 0x0B]
        permanence = raw[record_offset + 0x0D]
        result[target_id] = {
            "level": int(score),
            "signed_score": int(score) if relation_type == 1 else -int(score),
            "reason": int(reason),
            "object_type": int(object_type),
            "relation_type": int(relation_type),
            "permanence": int(permanence),
            "permanent": int(permanence) == 79,
        }
    return result


def read_person_public_profile(reader: Any, person: int) -> dict[str, Any] | None:
    """Read a public profile for any person address (relation-network externals).

    Returns kind ("player" | "staff" | "person"), CA/PA, primary positions or a
    Chinese job title, and the current club name.  Free agents / unemployed
    staff report club=None so the UI can label them 自由身.  Each branch is
    independently fault-tolerant: a partial read drops only that segment and
    the name is always a safe fallback.
    """
    cache_key = (_reader_cache_key(reader), int(person))
    with _CACHE_LOCK:
        cached = _PUBLIC_PROFILE_CACHE.get(cache_key)
        if cached is not None:
            return cached if cached else None
    layout = reader.layout
    name = _name(reader, int(person))
    if not name:
        with _CACHE_LOCK:
            _PUBLIC_PROFILE_CACHE[cache_key] = None
        _trim_public_profile_cache()
        return None
    profile: dict[str, Any] = {
        "name": name,
        "uid": reader.u32(int(person) + ENTITY_UID),
        "kind": "person", "ca": None, "pa": None,
        "positions": [], "role": None, "club": None,
    }
    module_base = reader.module_base

    def team_status_of(team_address: Any) -> dict[str, Any]:
        if not team_address:
            return {}
        try:
            team = reader.team(int(team_address))
        except Exception:
            return {}
        if not team:
            return {}
        return {
            "club": team.get("short_name") or team.get("name") or None,
            "team_name": team.get("short_name") or team.get("name") or None,
            "team_id": team.get("id"),
            "team_type": team.get("team_type") or "club",
        }

    # ── 1. 球员：person 内嵌于 player 记录，反查记录地址并验证 vtable ──
    try:
        player_record = 0
        player_candidates = (
            (int(getattr(layout, "player_person_offset", 0) or 0),
             tuple(getattr(layout, "actual_player_vtable_rvas", ()) or ())),
            (int(getattr(layout, "player_and_non_player_person_offset", 0) or 0),
             tuple(getattr(layout, "player_and_non_player_vtable_rvas", ()) or ())),
        )
        for person_offset, rvas in player_candidates:
            if not person_offset or not rvas:
                continue
            candidate = int(person) - person_offset
            if candidate <= 0:
                continue
            vtable = reader.ptr(candidate)
            if vtable and (int(vtable) - module_base) in rvas:
                player_record = candidate
                break
    except Exception:
        player_record = 0
    if player_record:
        profile["kind"] = "player"
        try:
            ca = (
                reader.u8(player_record + layout.player_ca_offset)
                if getattr(layout, "player_ca_bytes", 2) == 1
                else reader.u16(player_record + layout.player_ca_offset)
            )
            pa = reader.u16(player_record + layout.player_pa_offset)
            profile["ca"] = int(ca) if ca is not None and 1 <= int(ca) <= 200 else None
            profile["pa"] = int(pa) if pa is not None and 1 <= int(pa) <= 200 else None
        except Exception:
            pass
        try:
            positions_raw = reader.bytes(
                player_record + layout.player_positions_offset, len(POSITION_NAMES),
            ) or b""
            rated = [
                (pos, int(val))
                for pos, val in zip(POSITION_NAMES, positions_raw)
                if int(val) > 1
            ]
            rated.sort(key=lambda item: -item[1])
            profile["positions"] = [pos for pos, _ in rated[:2]]
        except Exception:
            pass
        try:
            team_address, _contract_row = _contract(reader, int(person))
            profile.update(team_status_of(team_address))
        except Exception:
            pass
        profile["role"] = (
            "球员 · " + "/".join(profile["positions"]) if profile["positions"] else "球员"
        )
        with _CACHE_LOCK:
            _PUBLIC_PROFILE_CACHE[cache_key] = profile
        _trim_public_profile_cache()
        return profile

    # ── 2. 职员：person 自身 vtable 匹配 staff_person_vtable_rva ──
    try:
        staff_vtable_rva = getattr(layout, "staff_person_vtable_rva", None)
        is_staff = (
            staff_vtable_rva is not None
            and reader.ptr(int(person)) == module_base + int(staff_vtable_rva)
        )
    except Exception:
        is_staff = False
    if is_staff:
        profile["kind"] = "staff"
        try:
            complete_object = int(person) - int(layout.staff_complete_object_offset)
            ca = (
                reader.u16(complete_object + int(layout.staff_ca_offset))
                if layout.staff_ca_offset is not None else None
            )
            pa = (
                reader.u16(complete_object + int(layout.staff_pa_offset))
                if layout.staff_pa_offset is not None else None
            )
            profile["ca"] = int(ca) if ca is not None and 1 <= int(ca) <= 200 else None
            profile["pa"] = int(pa) if pa is not None and 1 <= int(pa) <= 200 else None
        except Exception:
            pass
        try:
            if layout.staff_coaching_license_offset is not None:
                license_code = reader.u8(int(person) + int(layout.staff_coaching_license_offset))
                if license_code in COACHING_LICENSE_NAMES and int(license_code) != 0:
                    profile["license"] = COACHING_LICENSE_NAMES[int(license_code)]
        except Exception:
            pass
        try:
            team_address, contract_row = _contract(reader, int(person))
            job_type = None
            if contract_row and layout.staff_job_type_offset is not None:
                job_type = reader.u8(
                    int(str(contract_row["address"]), 16) + int(layout.staff_job_type_offset),
                )
            profile["role"] = (
                STAFF_JOB_TYPES.get(int(job_type)) if job_type is not None else None
            ) or "职员"
            if job_type is not None:
                profile["job_type"] = int(job_type)
                profile["job_type_name"] = profile["role"]
            profile.update(team_status_of(team_address))
        except Exception:
            profile["role"] = profile["role"] or "职员"
        with _CACHE_LOCK:
            _PUBLIC_PROFILE_CACHE[cache_key] = profile
        _trim_public_profile_cache()
        return profile

    # ── 3. 其他人员（名宿/经纪人等）：尝试合同定位在职俱乐部 ──
    try:
        team_address, _contract_row = _contract(reader, int(person))
        profile.update(team_status_of(team_address))
    except Exception:
        pass
    with _CACHE_LOCK:
        _PUBLIC_PROFILE_CACHE[cache_key] = profile
    _trim_public_profile_cache()
    return profile


# ── Write helpers ───────────────────────────────────────────────────────

def _person_relationship_record(
    to_person_ptr: int, reason: int, object_type: int,
    relation_type: int, level: int, permanence: int,
) -> bytes:
    return (
        struct.pack("<Q", to_person_ptr)
        + struct.pack("<H", int(reason))
        + bytes([int(object_type), int(relation_type), int(level), int(permanence), 0, 0xFF])
    )


def _write_person_relationship(
    reader: Any, process: Any, from_person: int, to_person_ptr: int,
    default_reason: int = 5, object_type: int = 3,
    default_relation_type: int = 1, permanence: int = 0, points: int = 1,
) -> dict[str, Any]:
    """Generic single-direction relationship write.

    Core rules (ported from 2.1.2, §1.2.4):
    - existing record → keep its reason/object_type/relation_type/permanence,
      only adjust level;
    - no record → create with the caller-supplied defaults;
    - ``points < 0`` never creates a new record.

    Returns an undo snapshot so an outer transaction (e.g. the other half of a
    bidirectional write) can restore this direction exactly.
    """
    points = int(points)
    if not points:
        raise ValueError("亲密度调整值无效")
    layout = reader.layout
    offset = layout.person_relationships_offset
    if offset is None:
        raise RuntimeError("当前游戏版本不支持人际关系读写")
    relationships = reader.ptr(from_person + offset)
    if not relationships:
        raise RuntimeError("无法定位关系记录容器")
    header = reader.bytes(relationships, 24)
    if not header or len(header) != 24:
        raise RuntimeError("无法读取关系记录")
    begin, end, capacity = struct.unpack("<QQQ", header)
    if (
        not begin or begin > end or end > capacity or (end - begin) % 16
        or (end - begin) > REL_VECTOR_MAX
    ):
        raise RuntimeError("关系记录结构无效")
    byte_length = end - begin
    raw = reader.bytes(begin, byte_length) if byte_length else b""
    if raw is None or len(raw) != byte_length:
        raise RuntimeError("无法完整读取关系记录")
    entry_offset = -1
    for record_offset in range(0, byte_length, 16):
        if struct.unpack_from("<Q", raw, record_offset)[0] == to_person_ptr:
            entry_offset = record_offset
            break
    entry = begin + entry_offset if entry_offset >= 0 else 0
    undo: dict[str, Any] = {
        "container": relationships, "header_before": header,
        "header_after": header, "slot_address": 0,
        "slot_bytes_before": b"", "slot_bytes_after": b"",
        "allocation": 0, "free_address": 0,
    }
    if entry:
        slot_bytes = reader.bytes(entry, 16)
        if not slot_bytes or len(slot_bytes) != 16:
            raise RuntimeError("无法读取关系记录条目")
        before = int(slot_bytes[12])
        after = max(0, min(100, before + points))
        if after == before:
            reason = struct.unpack("<H", slot_bytes[8:10])[0]
            return {
                "before": before, "after": after, "applied": 0,
                "reason": int(reason), "new_record": False, "undo": undo,
            }
        try:
            write_process_memory(process, entry + 12, bytes([after]))
            if reader.u8(entry + 12) != after:
                write_process_memory(process, entry + 12, bytes([before]))
                raise RuntimeError("亲密度写入校验失败")
        except Exception:
            write_process_memory(process, entry + 12, bytes([before]))
            raise
        # 保持原 reason/object_type/relation_type/permanence 不变
        existing_reason = struct.unpack("<H", slot_bytes[8:10])[0]
        slot_bytes_after = slot_bytes[:12] + bytes([after]) + slot_bytes[13:]
        undo.update({
            "slot_address": entry,
            "slot_bytes_before": slot_bytes,
            "slot_bytes_after": slot_bytes_after,
        })
        return {
            "before": before, "after": after, "applied": after - before,
            "reason": int(existing_reason), "new_record": False, "undo": undo,
        }

    if points < 0:
        return {
            "before": 0, "after": 0, "applied": 0,
            "reason": int(default_reason), "new_record": False, "undo": undo,
        }
    after = min(100, points)
    relation_raw = _person_relationship_record(
        to_person_ptr, default_reason, object_type,
        default_relation_type, after, permanence,
    )
    try:
        if capacity - end >= 16:
            slot = end
            write_process_memory(process, slot, relation_raw)
            write_process_memory(process, relationships + 8, struct.pack("<Q", end + 16))
            if reader.ptr(slot) != to_person_ptr or reader.u8(slot + 12) != after:
                write_process_memory(process, relationships, header)
                raise RuntimeError("新建亲密度关系记录校验失败")
            undo.update({
                "header_after": struct.pack("<QQQ", begin, end + 16, capacity),
                "slot_address": slot, "slot_bytes_before": b"\x00" * 16,
                "slot_bytes_after": relation_raw,
            })
        else:
            existing = reader.bytes(begin, end - begin) or b""
            allocated, free_address = _remote_malloc_block(process, len(existing) + 16)
            undo.update({"allocation": allocated, "free_address": free_address})
            try:
                write_process_memory(process, allocated, existing + relation_raw)
                header_after = struct.pack(
                    "<QQQ", allocated, allocated + len(existing) + 16,
                    allocated + len(existing) + 16,
                )
                write_process_memory(process, relationships, header_after)
                if reader.ptr(allocated + len(existing)) != to_person_ptr:
                    write_process_memory(process, relationships, header)
                    raise RuntimeError("新建亲密度关系记录校验失败")
                undo.update({
                    "header_after": header_after,
                    "slot_address": allocated + len(existing),
                    "slot_bytes_before": b"\x00" * 16,
                    "slot_bytes_after": relation_raw,
                })
            except Exception:
                _remote_free_block(process, free_address, allocated)
                raise
    except Exception:
        # 还原 header（tail 追加场景）
        write_process_memory(process, relationships, header)
        raise
    return {
        "before": 0, "after": after, "applied": after,
        "reason": int(default_reason), "new_record": True, "undo": undo,
    }


def _validate_person_relationship_rollback(reader: Any, undo: dict[str, Any]) -> bool:
    """Validate that a relationship undo token still owns its written bytes."""
    container = int(undo.get("container") or 0)
    header_before = undo.get("header_before") or b""
    if not container or not header_before:
        return False
    if reader.bytes(container, 24) != undo.get("header_after"):
        raise RuntimeError("关系记录已被其他操作改变，拒绝覆盖回滚")
    slot_address = int(undo.get("slot_address") or 0)
    slot_bytes_before = undo.get("slot_bytes_before") or b""
    slot_bytes_after = undo.get("slot_bytes_after") or b""
    if (
        slot_address and slot_bytes_after
        and reader.bytes(slot_address, 16) != slot_bytes_after
    ):
        raise RuntimeError("关系记录已被其他操作改变，拒绝覆盖回滚")
    return True


def _rollback_person_relationship(
    reader: Any, process: Any, undo: dict[str, Any], *, validated: bool = False,
) -> None:
    """Restore a previous single-direction write (header + slot + allocation)."""
    if not validated and not _validate_person_relationship_rollback(reader, undo):
        return
    container = int(undo.get("container") or 0)
    header_before = undo.get("header_before") or b""
    slot_address = int(undo.get("slot_address") or 0)
    slot_bytes_before = undo.get("slot_bytes_before") or b""
    write_process_memory(process, container, header_before)
    if slot_address and slot_bytes_before and len(slot_bytes_before) == 16:
        write_process_memory(process, slot_address, slot_bytes_before)
    allocation = int(undo.get("allocation") or 0)
    if allocation:
        _remote_free_block(process, int(undo.get("free_address") or 0), allocation)
    if reader.bytes(container, 24) != header_before:
        raise RuntimeError("关系记录回滚回读校验失败")


def write_person_intimacy(
    person_from: int, person_to: int, *, default_reason: int,
    points: int = 1, object_type: int = 3, permanence: int = 0,
) -> dict[str, Any]:
    """Write one relationship direction and retain an exact rollback token."""
    with _writable_game_reader() as (reader, process, _module):
        result = _write_person_relationship(
            reader, process, int(person_from), int(person_to),
            default_reason=int(default_reason), object_type=int(object_type),
            permanence=int(permanence), points=int(points),
        )
    invalidate_relationship_caches()
    return result


def rollback_person_intimacy(result: dict[str, Any]) -> None:
    undo = dict(result.get("undo") or {})
    if not undo:
        return
    with _writable_game_reader() as (reader, process, _module):
        _rollback_person_relationship(reader, process, undo)
    invalidate_relationship_caches()


def write_bidirectional_intimacy(
    person_a: int, person_b: int,
    reason_a2b: int = 5, reason_b2a: int = 5,
    points: int = 1, points_a2b: int | None = None, points_b2a: int | None = None,
    object_type: int = 3, permanence: int = 0,
    *, operation: Any | None = None,
) -> dict[str, Any]:
    """Bidirectional intimacy write — A→B then B→A in one writable session.

    Both directions may carry different default reasons (e.g. coach→player=7,
    player→coach=8).  If the second direction fails, the first direction is
    rolled back exactly.
    """
    def apply(reader: Any, process: Any) -> dict[str, Any]:
        result_a2b = _write_person_relationship(
            reader, process, int(person_a), int(person_b),
            default_reason=int(reason_a2b), object_type=int(object_type),
            permanence=int(permanence), points=int(points if points_a2b is None else points_a2b),
        )
        try:
            result_b2a = _write_person_relationship(
                reader, process, int(person_b), int(person_a),
                default_reason=int(reason_b2a), object_type=int(object_type),
                permanence=int(permanence), points=int(points if points_b2a is None else points_b2a),
            )
        except Exception:
            _rollback_person_relationship(reader, process, result_a2b.get("undo") or {})
            raise
        return {"a_to_b": result_a2b, "b_to_a": result_b2a}

    if operation is not None:
        if not getattr(operation, "writable", False):
            raise RuntimeError("双向关系写入需要可写操作会话")
        result = apply(operation.reader, operation.process)
    else:
        with _writable_game_reader() as (reader, process, _module):
            result = apply(reader, process)
    invalidate_relationship_caches()
    return result


def rollback_bidirectional_intimacy(
    result: dict[str, Any], *, operation: Any | None = None,
) -> None:
    """Restore both directions in reverse order using their exact undo tokens."""
    def apply(reader: Any, process: Any) -> None:
        undos: list[dict[str, Any]] = []
        for key in ("b_to_a", "a_to_b"):
            undo = dict((result.get(key) or {}).get("undo") or {})
            if undo:
                undos.append(undo)
        for undo in undos:
            _validate_person_relationship_rollback(reader, undo)
        for undo in undos:
            _rollback_person_relationship(reader, process, undo, validated=True)

    if operation is not None:
        if not getattr(operation, "writable", False):
            raise RuntimeError("双向关系回滚需要可写操作会话")
        apply(operation.reader, operation.process)
    else:
        with _writable_game_reader() as (reader, process, _module):
            apply(reader, process)
    invalidate_relationship_caches()


def persons_relation_negative(reader: Any, person_a: int, person_b: int) -> bool:
    """§3.1 pairing precondition: either direction ``relation_type != 1`` (dislike)."""
    for person_from, person_to in (
        (int(person_a), int(person_b)), (int(person_b), int(person_a)),
    ):
        detail = _read_person_pair_detail(reader, person_from, person_to)
        if detail and int(detail.get("relation_type", 1)) != 1:
            return True
    return False
