from __future__ import annotations

import struct
from dataclasses import dataclass
from datetime import date
from typing import Any

from fm_collector.win32 import (
    MEM_IMAGE, PAGE_EXECUTE_READ, PAGE_EXECUTE_READWRITE,
    PAGE_EXECUTE_WRITECOPY, iter_readable_regions, open_process,
    read_process_memory, write_process_memory,
)
from tools.initial_data_audit import (
    ENTITY_UID, Reader, decode_date, select_process_layout,
)
from tools.database_index import resolve_team_club


AFFILIATION_TYPE_NAMES: dict[int, str] = {
    1: "普通关联俱乐部",
    2: "下级俱乐部",
    3: "合作俱乐部",
    16: "关系良好",
    17: "可能的友谊赛",
}
AFFILIATION_TYPES = frozenset(AFFILIATION_TYPE_NAMES)
AFFILIATIONS_OFFSET = 0x118
# tdg6661's FM24 24.3.0 and FM26 26.3.0 tables both allocate exactly
# 0x38 bytes for one native Club Affiliation record.  The last known field is
# the parent-club wage percentage byte at +0x36.
AFFILIATION_RECORD_SIZE = 0x38
MAX_AFFILIATIONS = 256
MAX_GLOBAL_AFFILIATIONS = 100_000
NATIVE_CREATE_PREFIX = bytes.fromhex(
    "4157415641554154565755534883ec48"
)
EXECUTE_MASK = PAGE_EXECUTE_READ | PAGE_EXECUTE_READWRITE | PAGE_EXECUTE_WRITECOPY

AFFILIATION_BENEFITS: tuple[tuple[int, str, str], ...] = (
    (0x00000001, "uses_same_setup", "使用同一设定"),
    (0x00000002, "players_move_freely", "球员自由转会"),
    (0x00000004, "players_go_on_loan", "球员租借"),
    (0x00000008, "first_option_to_buy", "优先购买权"),
    (0x00000010, "play_friendlies", "进行友谊赛"),
    (0x00000020, "youth_players_training", "青年球员训练"),
    (0x00000040, "permanent_deal", "长期合约"),
    (0x00000080, "same_board", "相同的董事会"),
    (0x00000200, "financial_help", "财政上的帮助"),
    (0x00000400, "help_with_facilities", "协助改善设施"),
    (0x00000800, "renewable", "可续约关系"),
    (0x00002000, "marketing_relations", "商业关系"),
    (0x00004000, "scouting_knowledge_shared", "球探情报共享"),
    (0x00008000, "cannot_play_same_division", "不能参加同一级别联赛"),
    (0x00010000, "uses_parent_facilities", "使用所有母俱乐部设施"),
    (0x00020000, "loan_players_from_subteam", "从下级球队租借球员"),
    (0x00040000, "uses_subteam_training", "使用备用训练设施"),
    (0x00080000, "send_youth_for_experience", "送年轻球员去增长比赛经验"),
    (0x00100000, "reserve_team_player_relation", "用于表示预备队球员的关联"),
    (0x00200000, "send_first_team_to_improve_affiliate", "送一线队球员去帮助附属俱乐部升级"),
    (0x00800000, "uses_same_kit", "使用相同球衣"),
)
AFFILIATION_BENEFIT_MASK = sum(row[0] for row in AFFILIATION_BENEFITS)
AFFILIATION_BENEFIT_DEPENDENCIES = {
    0x00080000: 0x00000080,
    0x00100000: 0x00000080,
    0x00200000: 0x00000080,
}


def _address(value: Any) -> int:
    if isinstance(value, int):
        return value
    try:
        return int(str(value or "0"), 0)
    except (TypeError, ValueError):
        return 0


@dataclass(frozen=True)
class _ClubTarget:
    team_id: int
    team_address: int
    club_address: int
    club_id: int
    vector_root: int


@dataclass(frozen=True)
class _Vector:
    root: int
    begin: int
    end: int
    capacity: int
    pointers: tuple[int, ...]
    raw: bytes

    @property
    def header(self) -> bytes:
        return struct.pack("<QQQ", self.begin, self.end, self.capacity)


@dataclass
class _AppendRollback:
    vector: _Vector
    original_tail: bytes | None
    allocation: int = 0
    free_address: int = 0


def _layout_values(reader: Reader) -> tuple[int, int]:
    offset = reader.layout.club_affiliations_offset
    size = reader.layout.club_affiliation_record_size
    if offset is None or size is None:
        raise RuntimeError("当前游戏版本尚未定位俱乐部关系布局")
    if int(offset) != AFFILIATIONS_OFFSET or int(size) != AFFILIATION_RECORD_SIZE:
        raise RuntimeError("当前游戏版本的俱乐部关系布局与已验证结构不一致")
    return int(offset), int(size)


def _club_target(reader: Reader, team_address: Any, team_id: Any) -> _ClubTarget:
    identifier = int(team_id or 0)
    address = _address(team_address)
    if identifier <= 0:
        raise ValueError("俱乐部关系目标无效，请刷新后重试")
    resolved_team_id = identifier
    # Prefer the session's stable Team UID directory (the same object-oriented
    # lookup used by FMRTE), while keeping the caller address as a validated
    # compatibility hint for readers without a usable directory.
    try:
        resolved = resolve_team_club(reader, address, identifier)
        address = int(resolved.team_address)
        club = int(resolved.club_address)
        resolved_team_id = int(resolved.team_id)
    except (OSError, RuntimeError, TypeError, ValueError) as resolver_error:
        # Keep the legacy Reader path for older adapters that expose a
        # validated Team/Club wrapper but no native UID directory.  The hint
        # is still checked for type, Team UID, Club pointer and Club vtable.
        if not address:
            raise resolver_error
        team = reader.team(address)
        if (
            not team
            or team.get("team_type") != "club"
            or int(team.get("id") or 0) != identifier
            or int(reader.u32(address + ENTITY_UID) or 0) != identifier
        ):
            raise RuntimeError("目标俱乐部已经变化，请刷新后重试") from resolver_error
        club = int(reader.ptr(address + 0x30) or 0)
        vtable = int(reader.ptr(club) or 0) if club else 0
        expected_rva = int(getattr(reader.layout, "club_vtable_rva", 0) or 0)
        if not club or not vtable or (
            expected_rva and vtable != reader.module_base + expected_rva
        ):
            raise RuntimeError("目标俱乐部对象校验失败") from resolver_error
    offset, _size = _layout_values(reader)
    club_id = int(reader.u32(club + ENTITY_UID) or 0)
    if club_id <= 0:
        raise RuntimeError("目标俱乐部原生 UID 校验失败")
    return _ClubTarget(resolved_team_id, address, club, club_id, club + offset)


def _read_vector(reader: Reader, target: _ClubTarget) -> _Vector:
    header = reader.bytes(target.vector_root, 24)
    if not header or len(header) != 24:
        raise RuntimeError("俱乐部关系列表读取失败")
    begin, end, capacity = struct.unpack("<QQQ", header)
    length = end - begin
    if (
        end < begin
        or capacity < end
        or length % 8
        or length > MAX_AFFILIATIONS * 8
        or (not begin and (end or capacity))
    ):
        raise RuntimeError("俱乐部关系列表结构校验失败")
    raw = reader.bytes(begin, length) if length else b""
    if raw is None or len(raw) != length:
        raise RuntimeError("俱乐部关系列表读取失败")
    pointers = tuple(
        int(struct.unpack_from("<Q", raw, index)[0])
        for index in range(0, length, 8)
    )
    if any(not pointer for pointer in pointers) or len(set(pointers)) != len(pointers):
        raise RuntimeError("俱乐部关系列表包含无效或重复记录")
    return _Vector(target.vector_root, begin, end, capacity, pointers, raw)


def _read_pointer_vector(
    reader: Reader, root: int, *, maximum: int,
) -> _Vector:
    header = reader.bytes(root, 24)
    if not header or len(header) != 24:
        raise RuntimeError("俱乐部关系全局登记表读取失败")
    begin, end, capacity = struct.unpack("<QQQ", header)
    length = end - begin
    if (
        end < begin
        or capacity < end
        or length % 8
        or length > int(maximum) * 8
        or (not begin and (end or capacity))
    ):
        raise RuntimeError("俱乐部关系全局登记表结构校验失败")
    raw = reader.bytes(begin, length) if length else b""
    if raw is None or len(raw) != length:
        raise RuntimeError("俱乐部关系全局登记表读取失败")
    pointers = tuple(
        int(struct.unpack_from("<Q", raw, index)[0])
        for index in range(0, length, 8)
    )
    if any(not pointer for pointer in pointers) or len(set(pointers)) != len(pointers):
        raise RuntimeError("俱乐部关系全局登记表包含无效或重复记录")
    return _Vector(root, begin, end, capacity, pointers, raw)


def _pattern_matches(data: bytes, pattern: tuple[int | None, ...]) -> list[int]:
    if not pattern or len(data) < len(pattern):
        return []
    anchor = next(
        (index for index, value in enumerate(pattern) if value is not None), None,
    )
    if anchor is None:
        return []
    needle = bytes([int(pattern[anchor])])
    matches: list[int] = []
    start = 0
    while True:
        position = data.find(needle, start)
        if position < 0:
            return matches
        candidate = position - anchor
        if candidate >= 0 and candidate + len(pattern) <= len(data) and all(
            expected is None or data[candidate + index] == expected
            for index, expected in enumerate(pattern)
        ):
            matches.append(candidate)
        start = position + 1


def _scan_executable_patterns(
    reader: Reader, patterns: dict[str, tuple[int | None, ...]],
) -> dict[str, list[int]]:
    module = reader.layout.module(reader.process)
    if not module:
        return {name: [] for name in patterns}
    hits: dict[str, list[int]] = {name: [] for name in patterns}
    module_end = int(module.base_address) + int(module.size)
    for region in iter_readable_regions(reader.process):
        if (
            region.type != MEM_IMAGE
            or not region.protect & EXECUTE_MASK
            or region.base_address < module.base_address
            or region.base_address >= module_end
        ):
            continue
        size = min(int(region.size), module_end - int(region.base_address))
        data = read_process_memory(reader.process, region.base_address, size) or b""
        for name, pattern in patterns.items():
            for offset in _pattern_matches(data, pattern):
                hits[name].append(int(region.base_address) + offset)
    return hits


def _scan_executable_pattern(
    reader: Reader, pattern: tuple[int | None, ...],
) -> list[int]:
    return _scan_executable_patterns(reader, {"pattern": pattern})["pattern"]


def _resolved_native_affiliation_addresses(reader: Reader) -> tuple[int, int]:
    manager_rva = getattr(reader.layout, "club_affiliation_manager_rva", None)
    create_rva = getattr(reader.layout, "club_affiliation_create_rva", None)
    if manager_rva is not None and create_rva is not None:
        return (
            reader.module_base + int(manager_rva),
            reader.module_base + int(create_rva),
        )

    create_pattern = tuple(getattr(
        reader.layout, "club_affiliation_create_pattern", (),
    ) or ())
    manager_pattern = tuple(getattr(
        reader.layout, "club_affiliation_manager_call_pattern", (),
    ) or ())
    offsets = (
        getattr(reader.layout, "club_affiliation_manager_rel32_offset", None),
        getattr(reader.layout, "club_affiliation_manager_instruction_size", None),
        getattr(reader.layout, "club_affiliation_create_call_rel32_offset", None),
        getattr(reader.layout, "club_affiliation_create_call_instruction_size", None),
    )
    if not create_pattern or not manager_pattern or any(
        value is None for value in offsets
    ):
        raise RuntimeError("当前游戏版本尚未定位俱乐部关系原生持久化事务")

    scanned = _scan_executable_patterns(reader, {
        "create": create_pattern, "manager": manager_pattern,
    })
    create_hits = scanned["create"]
    call_hits = scanned["manager"]
    if len(create_hits) != 1 or not call_hits:
        raise RuntimeError("俱乐部关系原生事务特征码缺失或不唯一")
    manager_rel, manager_end, call_rel, call_end = map(int, offsets)
    pairs: set[tuple[int, int]] = set()
    for hit in call_hits:
        raw = reader.bytes(hit, len(manager_pattern)) or b""
        if len(raw) != len(manager_pattern):
            continue
        manager = hit + manager_end + struct.unpack_from("<i", raw, manager_rel)[0]
        called = hit + call_end + struct.unpack_from("<i", raw, call_rel)[0]
        pairs.add((manager, called))
    managers = {manager for manager, called in pairs if called == create_hits[0]}
    if len(managers) != 1 or any(called != create_hits[0] for _, called in pairs):
        raise RuntimeError("俱乐部关系原生事务调用链解析不唯一")
    return next(iter(managers)), create_hits[0]


def _has_native_affiliation_creation(layout: Any) -> bool:
    return bool(
        getattr(layout, "club_affiliation_create_rva", None) is not None
        or getattr(layout, "club_affiliation_create_pattern", ())
    )


def _native_affiliation_registry(
    reader: Reader, addresses: tuple[int, int] | None = None,
) -> tuple[int, _Vector]:
    vector_offset = getattr(
        reader.layout, "club_affiliation_global_vector_offset", None,
    )
    if vector_offset is None:
        raise RuntimeError("当前游戏版本尚未定位俱乐部关系原生持久化事务")
    manager, create = addresses or _resolved_native_affiliation_addresses(reader)
    if reader.bytes(create, len(NATIVE_CREATE_PREFIX)) != NATIVE_CREATE_PREFIX:
        raise RuntimeError("俱乐部关系原生事务入口校验失败，当前游戏构建可能已经变化")
    if not reader.ptr(manager + 0x10):
        raise RuntimeError("俱乐部关系原生管理器校验失败")
    vector_root = int(reader.ptr(manager + int(vector_offset)) or 0)
    if not vector_root:
        raise RuntimeError("俱乐部关系全局登记表尚未初始化")
    return manager, _read_pointer_vector(
        reader, vector_root, maximum=MAX_GLOBAL_AFFILIATIONS,
    )


def _build_native_create_stub(
    *, function: int, manager: int, parent_id: int, feeder_id: int,
    record: bytes, start_address: int, end_address: int,
    result_address: int, context_address: int, rtl_restore: int,
) -> bytes:
    from tools.player_movement import (
        _Code, _mov_rax, _mov_rcx, _mov_rdx, _mov_r8, _mov_r9,
        _store_absolute_qword,
    )

    state = _record_state(record)
    frame = _native_creation_frame(record, start_address, end_address)

    code = _Code()
    code.emit(b"\x48\x83\xec\x78")
    for offset in range(0x20, 0x70, 8):
        value = struct.unpack_from("<Q", frame, offset)[0]
        code.emit(_mov_rax(value) + b"\x48\x89\x44\x24" + bytes([offset]))
    code.emit(
        _mov_rcx(manager) + _mov_rdx(parent_id) + _mov_r8(feeder_id)
        + _mov_r9(state["type"]) + _mov_rax(function) + b"\xff\xd0"
    )
    code.emit(b"\x48\xa3" + struct.pack("<Q", result_address + 8))
    code.emit(b"\x48\x85\xc0")
    code.jz("failed")
    code.emit(_store_absolute_qword(result_address, 1))
    code.emit(b"\xe9\0\0\0\0")
    code.jumps.append((len(code.raw) - 4, "restore"))
    code.label("failed")
    code.emit(_store_absolute_qword(result_address, 2))
    code.label("restore")
    code.emit(
        b"\x48\x83\xc4\x78" + _mov_rcx(context_address)
        + b"\x31\xd2" + _mov_rax(rtl_restore) + b"\xff\xe0"
    )
    return code.finish()


def _native_creation_frame(
    record: bytes, start_address: int, end_address: int,
) -> bytes:
    state = _record_state(record)
    frame = bytearray(0x70)
    struct.pack_into("<Q", frame, 0x20, state["benefits"])
    struct.pack_into("<Q", frame, 0x28, start_address)
    struct.pack_into("<Q", frame, 0x30, end_address)
    struct.pack_into("<Q", frame, 0x48, state["annual_payment"])
    frame[0x50] = state["friendly_probability_raw"]
    frame[0x58] = 0xFF
    frame[0x60] = state["maximum_players_loaned_raw"]
    frame[0x68] = state["parent_wage_percentage_raw"]
    return bytes(frame)


def _native_create_affiliation(
    process: Any, reader: Reader, manager: int, record: bytes,
    parent_id: int, feeder_id: int, create_address: int | None = None,
) -> int:
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
    function = int(
        create_address or _resolved_native_affiliation_addresses(reader)[1]
    )
    ntdll_remote = find_module(process, "ntdll.dll")
    ntdll_local = ctypes.WinDLL("ntdll", use_last_error=True)
    if not ntdll_remote:
        raise RuntimeError("无法定位 FM 原生事务恢复入口")
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
        raise RuntimeError("无法建立俱乐部关系原生事务调用区")
    result_address = cave + REMOTE_RESULT_OFFSET
    context_address = cave + REMOTE_CONTEXT_OFFSET
    start_address = result_address + 0x20
    end_address = result_address + 0x24
    code = _build_native_create_stub(
        function=function, manager=manager,
        parent_id=parent_id, feeder_id=feeder_id, record=record,
        start_address=start_address, end_address=end_address,
        result_address=result_address, context_address=context_address,
        rtl_restore=rtl_restore,
    )
    completed = False
    thread = 0
    try:
        write_process_memory(process, cave, code)
        write_process_memory(process, result_address, b"\0" * 0x40)
        write_process_memory(
            process, start_address,
            struct.pack("<II", _record_state(record)["start_date_raw"],
                        _record_state(record)["end_date_raw"]),
        )
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
            hijacked.Rsp = ((cave + REMOTE_STACK_TOP_OFFSET) & ~0xF) - 8
            write_process_memory(process, int(hijacked.Rsp), b"\0" * 8)
            if not kernel32.SetThreadContext(thread, ctypes.byref(hijacked)):
                raise OSError(ctypes.get_last_error(), "SetThreadContext failed")
        finally:
            if suspended:
                kernel32.ResumeThread(thread)
        deadline = time.monotonic() + 10.0
        status = created = 0
        while time.monotonic() < deadline:
            result = read_process_memory(process, result_address, 16) or b""
            if len(result) == 16:
                status, created = struct.unpack("<QQ", result)
                if status in {1, 2}:
                    completed = True
                    break
            time.sleep(0.01)
        if not completed:
            raise RuntimeError(
                "俱乐部关系原生事务执行超时，请不要立即重复操作"
            )
        if status != 1 or not created:
            raise RuntimeError("FM 拒绝了本次俱乐部关系创建事务")
        time.sleep(0.05)
        return int(created)
    finally:
        if thread:
            kernel32.CloseHandle(thread)
        if completed:
            kernel32.VirtualFreeEx(
                process.handle, ctypes.c_void_p(cave), 0, MEM_RELEASE,
            )


def _record_state(raw: bytes) -> dict[str, int]:
    if len(raw) != AFFILIATION_RECORD_SIZE:
        raise RuntimeError("俱乐部关系记录长度无效")
    start_raw = int(struct.unpack_from("<I", raw, 0x20)[0])
    end_raw = int(struct.unpack_from("<I", raw, 0x24)[0])
    return {
        "parent_club": int(struct.unpack_from("<Q", raw, 0x00)[0]),
        "feeder_club": int(struct.unpack_from("<Q", raw, 0x08)[0]),
        "annual_payment": int(struct.unpack_from("<q", raw, 0x10)[0]),
        "benefits": int(struct.unpack_from("<I", raw, 0x1C)[0]),
        "start_date_raw": start_raw,
        "end_date_raw": end_raw,
        "type": int(raw[0x30]),
        "friendly_probability_raw": int(raw[0x33]),
        "maximum_players_loaned_raw": int(raw[0x34]),
        "parent_wage_percentage_raw": int(raw[0x36]),
    }


def _optional_byte(value: int) -> int | None:
    return None if value == 0xFF else value


def _date_text(raw: int) -> str | None:
    parsed = decode_date(raw)
    return parsed.isoformat() if parsed else None


def _record_payload(raw: bytes, state: dict[str, int]) -> dict[str, Any]:
    benefits = int(state["benefits"])
    return {
        "record_token": raw.hex(),
        "annual_payment": int(state["annual_payment"]),
        "benefits": benefits,
        "unknown_benefits": benefits & ~AFFILIATION_BENEFIT_MASK,
        "enabled_benefits": [
            key for bit, key, _label in AFFILIATION_BENEFITS if benefits & bit
        ],
        "start_date": _date_text(state["start_date_raw"]),
        "end_date": _date_text(state["end_date_raw"]),
        "friendly_probability": _optional_byte(state["friendly_probability_raw"]),
        "maximum_players_loaned": _optional_byte(state["maximum_players_loaned_raw"]),
        "parent_wage_percentage": _optional_byte(state["parent_wage_percentage_raw"]),
    }


def _record_identity(state: dict[str, int]) -> dict[str, int]:
    return {
        "parent_club": state["parent_club"],
        "feeder_club": state["feeder_club"],
        "type": state["type"],
    }


def affiliation_benefit_options() -> list[dict[str, Any]]:
    return [
        {
            "value": bit,
            "key": key,
            "label": label,
            "requires": AFFILIATION_BENEFIT_DEPENDENCIES.get(bit),
        }
        for bit, key, label in AFFILIATION_BENEFITS
    ]


def _read_record(reader: Reader, address: int) -> tuple[bytes, dict[str, int]]:
    _offset, size = _layout_values(reader)
    raw = reader.bytes(address, size)
    if not raw or len(raw) != size:
        raise RuntimeError("俱乐部关系记录读取失败")
    return raw, _record_state(raw)


def _valid_record_identity(state: dict[str, int]) -> bool:
    return bool(state["parent_club"] and state["feeder_club"] and state["type"])


def _open_reader(*, write: bool, create_thread: bool = False):
    pid, _path, layout = select_process_layout()
    return pid, layout, open_process(pid, write_memory=write, create_thread=create_thread)


def _targets_and_vectors(
    reader: Reader, clubs: list[dict[str, Any]],
) -> tuple[dict[int, _ClubTarget], dict[int, _Vector]]:
    targets: dict[int, _ClubTarget] = {}
    vectors: dict[int, _Vector] = {}
    for club in clubs:
        target = _club_target(reader, club.get("address"), club.get("id"))
        if target.team_id in targets:
            raise ValueError("俱乐部关系目标重复")
        targets[target.team_id] = target
        vectors[target.team_id] = _read_vector(reader, target)
    return targets, vectors


def read_club_affiliations(clubs: list[dict[str, Any]]) -> dict[str, Any]:
    if not clubs:
        return {
            "types": affiliation_type_options(),
            "benefit_options": affiliation_benefit_options(),
            "relations": [],
        }
    pid, layout, handle = _open_reader(write=False)
    with handle as process:
        module = layout.module(process)
        if not module:
            raise RuntimeError(f"{layout.module_name} 尚未加载")
        reader = Reader(process, module.base_address, layout)
        default_start, default_end = _default_affiliation_dates(reader)
        targets, vectors = _targets_and_vectors(reader, clubs)
        club_by_address = {target.club_address: target for target in targets.values()}
        memberships: dict[int, set[int]] = {}
        for team_id, vector in vectors.items():
            for pointer in vector.pointers:
                memberships.setdefault(pointer, set()).add(team_id)
        relations: list[dict[str, Any]] = []
        for pointer in sorted(memberships):
            raw, state = _read_record(reader, pointer)
            if not _valid_record_identity(state):
                continue
            parent = club_by_address.get(state["parent_club"])
            feeder = club_by_address.get(state["feeder_club"])
            if not parent or not feeder or state["type"] not in AFFILIATION_TYPES:
                continue
            expected_members = {parent.team_id, feeder.team_id}
            actual_members = memberships[pointer]
            relations.append({
                "record_address": hex(pointer),
                "parent_team_id": parent.team_id,
                "feeder_team_id": feeder.team_id,
                "type": state["type"],
                "type_name": AFFILIATION_TYPE_NAMES[state["type"]],
                "consistent": actual_members == expected_members,
                **_record_payload(raw, state),
            })
        return {
            "types": affiliation_type_options(),
            "benefit_options": affiliation_benefit_options(),
            "relations": relations,
            "game": layout.display_name,
            "default_start_date": default_start.isoformat(),
            "default_end_date": default_end.isoformat(),
        }


def affiliation_type_options() -> list[dict[str, Any]]:
    return [
        {"value": value, "label": label}
        for value, label in AFFILIATION_TYPE_NAMES.items()
    ]


def _allocate_native_block(process: Any, size: int) -> tuple[int, int]:
    from tools.club_reader import _remote_malloc_block
    return _remote_malloc_block(process, size)


def _free_native_block(process: Any, free_address: int, address: int) -> None:
    from tools.club_reader import _remote_free_block
    _remote_free_block(process, free_address, address)


def _append_pointer(
    process: Any, reader: Reader, vector: _Vector, pointer: int,
) -> _AppendRollback:
    if pointer in vector.pointers:
        raise RuntimeError("俱乐部关系列表已经包含该记录")
    rollback = _AppendRollback(vector=vector, original_tail=None)
    try:
        if vector.capacity - vector.end >= 8:
            rollback.original_tail = reader.bytes(vector.end, 8)
            if rollback.original_tail is None or len(rollback.original_tail) != 8:
                raise RuntimeError("俱乐部关系列表尾部读取失败")
            write_process_memory(process, vector.end, struct.pack("<Q", pointer))
            new_header = struct.pack(
                "<QQQ", vector.begin, vector.end + 8, vector.capacity,
            )
        else:
            new_raw = vector.raw + struct.pack("<Q", pointer)
            rollback.allocation, rollback.free_address = _allocate_native_block(
                process, len(new_raw),
            )
            write_process_memory(process, rollback.allocation, new_raw)
            new_header = struct.pack(
                "<QQQ", rollback.allocation,
                rollback.allocation + len(new_raw),
                rollback.allocation + len(new_raw),
            )
        write_process_memory(process, vector.root, new_header)
        return rollback
    except Exception:
        _restore_append(process, reader, rollback)
        raise


def _restore_append(process: Any, reader: Reader, rollback: _AppendRollback) -> None:
    vector = rollback.vector
    write_process_memory(process, vector.root, vector.header)
    if rollback.original_tail is not None:
        write_process_memory(process, vector.end, rollback.original_tail)
    if reader.bytes(vector.root, 24) != vector.header:
        raise RuntimeError("俱乐部关系写入失败，且原列表恢复失败")
    if rollback.allocation:
        _free_native_block(process, rollback.free_address, rollback.allocation)


def _validated_pair(
    reader: Reader, parent: dict[str, Any], feeder: dict[str, Any],
) -> tuple[_ClubTarget, _ClubTarget, _Vector, _Vector]:
    parent_target = _club_target(reader, parent.get("address"), parent.get("id"))
    feeder_target = _club_target(reader, feeder.get("address"), feeder.get("id"))
    if parent_target.team_id == feeder_target.team_id:
        raise ValueError("上级俱乐部和关联俱乐部不能相同")
    return (
        parent_target, feeder_target,
        _read_vector(reader, parent_target), _read_vector(reader, feeder_target),
    )


def _default_affiliation_dates(reader: Reader) -> tuple[date, date]:
    rva = reader.layout.game_date_rva
    if rva is None:
        raise RuntimeError("当前游戏版本尚未定位游戏日期，不能建立俱乐部关系")
    game_date = decode_date(int(reader.u32(reader.module_base + int(rva)) or 0))
    if not game_date or game_date.year > 9989:
        raise RuntimeError("当前游戏日期读取失败，不能建立俱乐部关系")
    return date(game_date.year, 1, 1), date(game_date.year + 10, 1, 1)


def _plain_date_code(value: date) -> int:
    return (value.year << 16) | value.timetuple().tm_yday


def _creation_date(value: Any, label: str) -> date:
    try:
        return date.fromisoformat(str(value or "").strip())
    except ValueError:
        raise ValueError(f"{label}格式无效") from None


def _boolean_field(value: Any, label: str) -> bool:
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{label}必须为布尔值")


def create_club_affiliation(
    parent: dict[str, Any], feeder: dict[str, Any], affiliation_type: Any, *,
    start_date: Any, end_date: Any, updates: dict[str, Any] | None = None,
) -> dict[str, Any]:
    relation_type = int(affiliation_type or 0)
    if relation_type not in AFFILIATION_TYPES:
        raise ValueError("不支持的俱乐部关系类型")
    pid, layout, handle = _open_reader(write=True, create_thread=True)
    with handle as process:
        module = layout.module(process)
        if not module:
            raise RuntimeError(f"{layout.module_name} 尚未加载")
        reader = Reader(process, module.base_address, layout)
        parent_target, feeder_target, parent_vector, feeder_vector = _validated_pair(
            reader, parent, feeder,
        )
        left_is_parent = _boolean_field(
            (updates or {}).get("is_parent_club", True), "是主俱乐部",
        )
        record_parent = parent_target if left_is_parent else feeder_target
        record_feeder = feeder_target if left_is_parent else parent_target
        relation_start = _creation_date(start_date, "开始日期")
        relation_end = _creation_date(end_date, "结束日期")
        if relation_end < relation_start:
            raise ValueError("结束日期不能早于开始日期")
        for pointer in set(parent_vector.pointers) | set(feeder_vector.pointers):
            _raw, state = _read_record(reader, pointer)
            if not _valid_record_identity(state):
                continue
            if (
                state["parent_club"] == record_parent.club_address
                and state["feeder_club"] == record_feeder.club_address
                and state["type"] == relation_type
            ):
                raise ValueError("两家俱乐部已经存在相同类型的关系")

        raw = bytearray(AFFILIATION_RECORD_SIZE)
        struct.pack_into("<Q", raw, 0x00, record_parent.club_address)
        struct.pack_into("<Q", raw, 0x08, record_feeder.club_address)
        struct.pack_into("<I", raw, 0x20, _plain_date_code(relation_start))
        struct.pack_into("<I", raw, 0x24, _plain_date_code(relation_end))
        raw[0x30] = relation_type
        raw[0x33] = 0xFF
        raw[0x34] = 0xFF
        raw[0x36] = 0xFF
        changes = dict(updates or {})
        changes.pop("is_parent_club", None)
        changes.update({
            "type": relation_type,
            "start_date": relation_start.isoformat(),
            "end_date": relation_end.isoformat(),
        })
        raw = bytearray(_updated_record(
            bytes(raw), _record_state(bytes(raw)), changes,
        ))

        if _has_native_affiliation_creation(layout):
            addresses = _resolved_native_affiliation_addresses(reader)
            manager, global_before = _native_affiliation_registry(reader, addresses)
            created = _native_create_affiliation(
                process, reader, manager, bytes(raw),
                record_parent.club_id, record_feeder.club_id, addresses[1],
            )
            parent_after = _read_vector(reader, parent_target)
            feeder_after = _read_vector(reader, feeder_target)
            _manager_after, global_after = _native_affiliation_registry(
                reader, addresses,
            )
            written, state = _read_record(reader, created)
            if (
                created in global_before.pointers
                or len(global_after.pointers) != len(global_before.pointers) + 1
                or created not in global_after.pointers
                or created not in parent_after.pointers
                or created not in feeder_after.pointers
                or _record_identity(state) != {
                    "parent_club": record_parent.club_address,
                    "feeder_club": record_feeder.club_address,
                    "type": relation_type,
                }
                or state["annual_payment"] != _record_state(bytes(raw))["annual_payment"]
                or state["benefits"] != _record_state(bytes(raw))["benefits"]
                or state["start_date_raw"] != _record_state(bytes(raw))["start_date_raw"]
                or state["end_date_raw"] != _record_state(bytes(raw))["end_date_raw"]
                or state["friendly_probability_raw"]
                != _record_state(bytes(raw))["friendly_probability_raw"]
                or state["maximum_players_loaned_raw"]
                != _record_state(bytes(raw))["maximum_players_loaned_raw"]
                or state["parent_wage_percentage_raw"]
                != _record_state(bytes(raw))["parent_wage_percentage_raw"]
            ):
                raise RuntimeError(
                    "FM 已执行俱乐部关系原生事务，但登记回读不一致；"
                    "请立即检查游戏状态且暂时不要保存"
                )
            return {
                "changed": True,
                "operation": "create",
                "record_address": hex(created),
                "parent_team_id": record_parent.team_id,
                "feeder_team_id": record_feeder.team_id,
                "type": relation_type,
                "type_name": AFFILIATION_TYPE_NAMES[relation_type],
                "start_date": relation_start.isoformat(),
                "end_date": relation_end.isoformat(),
            }

        record = record_free = 0
        rollbacks: list[_AppendRollback] = []
        try:
            record, record_free = _allocate_native_block(
                process, AFFILIATION_RECORD_SIZE,
            )
            write_process_memory(process, record, bytes(raw))
            rollbacks.append(_append_pointer(process, reader, parent_vector, record))
            rollbacks.append(_append_pointer(process, reader, feeder_vector, record))

            parent_after = _read_vector(reader, parent_target)
            feeder_after = _read_vector(reader, feeder_target)
            written, state = _read_record(reader, record)
            if (
                record not in parent_after.pointers
                or record not in feeder_after.pointers
                or _record_identity(state) != {
                    "parent_club": record_parent.club_address,
                    "feeder_club": record_feeder.club_address,
                    "type": relation_type,
                }
                or written != bytes(raw)
            ):
                raise RuntimeError("俱乐部关系写入后校验失败")
            # Live FM now owns the record and any replacement pointer arrays.
            created = record
            record = 0
            for rollback in rollbacks:
                rollback.allocation = 0
            return {
                "changed": True,
                "operation": "create",
                "record_address": hex(created),
                "parent_team_id": record_parent.team_id,
                "feeder_team_id": record_feeder.team_id,
                "type": relation_type,
                "type_name": AFFILIATION_TYPE_NAMES[relation_type],
                "start_date": relation_start.isoformat(),
                "end_date": relation_end.isoformat(),
            }
        except Exception:
            restore_error: Exception | None = None
            for rollback in reversed(rollbacks):
                try:
                    _restore_append(process, reader, rollback)
                except Exception as error:
                    restore_error = error
            if record and restore_error is None:
                _free_native_block(process, record_free, record)
            if restore_error is not None:
                raise RuntimeError("俱乐部关系写入失败，且双边列表恢复失败") from restore_error
            raise


def _expected_relation(
    reader: Reader, parent_target: _ClubTarget, feeder_target: _ClubTarget,
    parent_vector: _Vector, feeder_vector: _Vector, record_address: Any,
    expected_type: Any, expected_record: Any = None,
) -> tuple[int, bytes, dict[str, int]]:
    pointer = _address(record_address)
    relation_type = int(expected_type or 0)
    if not pointer or relation_type not in AFFILIATION_TYPES:
        raise ValueError("俱乐部关系参数无效，请刷新后重试")
    if (
        parent_vector.pointers.count(pointer) != 1
        or feeder_vector.pointers.count(pointer) != 1
    ):
        raise RuntimeError("俱乐部关系双边列表不一致，请刷新后重试")
    raw, state = _read_record(reader, pointer)
    if not _valid_record_identity(state):
        raise RuntimeError("俱乐部关系记录结构校验失败，请刷新后重试")
    if _record_identity(state) != {
        "parent_club": parent_target.club_address,
        "feeder_club": feeder_target.club_address,
        "type": relation_type,
    }:
        raise RuntimeError("俱乐部关系刚刚发生变化，请刷新后重试")
    token = str(expected_record or "").strip().lower()
    if token and token != raw.hex():
        raise RuntimeError("俱乐部关系详情刚刚发生变化，请刷新后重试")
    return pointer, raw, state


def _integer_field(value: Any, label: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{label}必须是 {minimum} 到 {maximum} 之间的整数")
    try:
        resolved = int(str(value).strip())
    except (TypeError, ValueError):
        raise ValueError(f"{label}必须是 {minimum} 到 {maximum} 之间的整数") from None
    if resolved < minimum or resolved > maximum:
        raise ValueError(f"{label}必须是 {minimum} 到 {maximum} 之间的整数")
    return resolved


def _nullable_byte(value: Any, label: str, minimum: int, maximum: int) -> int:
    if value is None or str(value).strip() == "":
        return 0xFF
    return _integer_field(value, label, minimum, maximum)


def _encoded_date(value: Any, old_raw: int, label: str) -> int:
    if value is None or str(value).strip() == "":
        return 0
    try:
        parsed = date.fromisoformat(str(value).strip())
    except ValueError:
        raise ValueError(f"{label}格式无效") from None
    return (parsed.year << 16) | parsed.timetuple().tm_yday | (old_raw & 0xFE00)


def _updated_record(raw: bytes, state: dict[str, int], updates: dict[str, Any]) -> bytes:
    output = bytearray(raw)
    target_type = _integer_field(updates.get("type"), "关系类型", 1, 255)
    if target_type not in AFFILIATION_TYPES:
        raise ValueError("不支持的俱乐部关系类型")
    annual_payment = _integer_field(
        updates.get("annual_payment", state["annual_payment"]),
        "年费", 0, 1_000_000_000,
    )
    start_raw = _encoded_date(
        updates.get("start_date", _date_text(state["start_date_raw"])),
        state["start_date_raw"], "开始日期",
    )
    end_raw = _encoded_date(
        updates.get("end_date", _date_text(state["end_date_raw"])),
        state["end_date_raw"], "结束日期",
    )
    start_date = decode_date(start_raw)
    end_date = decode_date(end_raw)
    if start_date and end_date and end_date < start_date:
        raise ValueError("结束日期不能早于开始日期")
    requested_benefits = _integer_field(
        updates.get("benefits", state["benefits"]),
        "关系权益", 0, 0xFFFFFFFF,
    )
    benefits = (
        (state["benefits"] & ~AFFILIATION_BENEFIT_MASK)
        | (requested_benefits & AFFILIATION_BENEFIT_MASK)
    )
    for dependent, required in AFFILIATION_BENEFIT_DEPENDENCIES.items():
        if benefits & dependent and not benefits & required:
            raise ValueError("董事会共享从属权益需要先启用相同的董事会")
    parent_wage_percentage = _nullable_byte(
        updates.get(
            "parent_wage_percentage",
            _optional_byte(state["parent_wage_percentage_raw"]),
        ), "主俱乐部工资承担比例", 0, 100,
    )
    if parent_wage_percentage != 0xFF and not benefits & 0x00000080:
        raise ValueError("母俱乐部支付工资比例需要先启用相同的董事会")
    struct.pack_into("<q", output, 0x10, annual_payment)
    struct.pack_into("<I", output, 0x1C, benefits)
    struct.pack_into("<I", output, 0x20, start_raw)
    struct.pack_into("<I", output, 0x24, end_raw)
    output[0x30] = target_type
    output[0x33] = _nullable_byte(
        updates.get(
            "friendly_probability",
            _optional_byte(state["friendly_probability_raw"]),
        ), "友谊赛概率", 0, 100,
    )
    output[0x34] = _nullable_byte(
        updates.get(
            "maximum_players_loaned",
            _optional_byte(state["maximum_players_loaned_raw"]),
        ), "最多租借人数", 1, 30,
    )
    output[0x36] = parent_wage_percentage
    if "is_parent_club" in updates and not _boolean_field(
        updates["is_parent_club"], "是主俱乐部",
    ):
        struct.pack_into("<Q", output, 0x00, state["feeder_club"])
        struct.pack_into("<Q", output, 0x08, state["parent_club"])
    return bytes(output)


def update_club_affiliation(
    parent: dict[str, Any], feeder: dict[str, Any], record_address: Any, *,
    expected_type: Any, affiliation_type: Any, expected_record: Any = None,
    updates: dict[str, Any] | None = None,
) -> dict[str, Any]:
    target_type = int(affiliation_type or 0)
    if target_type not in AFFILIATION_TYPES:
        raise ValueError("不支持的俱乐部关系类型")
    pid, layout, handle = _open_reader(write=True)
    with handle as process:
        module = layout.module(process)
        if not module:
            raise RuntimeError(f"{layout.module_name} 尚未加载")
        reader = Reader(process, module.base_address, layout)
        parent_target, feeder_target, parent_vector, feeder_vector = _validated_pair(
            reader, parent, feeder,
        )
        pointer, raw, state = _expected_relation(
            reader, parent_target, feeder_target, parent_vector, feeder_vector,
            record_address, expected_type, expected_record,
        )
        changes = dict(updates or {})
        changes["type"] = target_type
        written_record = _updated_record(raw, state, changes)
        written_state = _record_state(written_record)
        written_parent = (
            feeder_target
            if written_state["parent_club"] == feeder_target.club_address
            else parent_target
        )
        written_feeder = feeder_target if written_parent is parent_target else parent_target
        if written_record == raw:
            return {
                "changed": False, "operation": "update",
                "record_address": hex(pointer),
                "parent_team_id": parent_target.team_id,
                "feeder_team_id": feeder_target.team_id,
                "type": target_type,
                "type_name": AFFILIATION_TYPE_NAMES[target_type],
            }
        for other_pointer in set(parent_vector.pointers) | set(feeder_vector.pointers):
            if other_pointer == pointer:
                continue
            _other_raw, other = _read_record(reader, other_pointer)
            if not _valid_record_identity(other):
                continue
            if _record_identity(other) == {
                "parent_club": written_parent.club_address,
                "feeder_club": written_feeder.club_address,
                "type": target_type,
            }:
                raise ValueError("这两家俱乐部已经存在所选类型的关系")
        try:
            write_process_memory(process, pointer, written_record)
            verified, after = _read_record(reader, pointer)
            if verified != written_record or _record_identity(after) != {
                "parent_club": written_parent.club_address,
                "feeder_club": written_feeder.club_address,
                "type": target_type,
            }:
                raise RuntimeError("俱乐部关系详情写入后校验失败")
        except Exception:
            write_process_memory(process, pointer, raw)
            if reader.bytes(pointer, len(raw)) != raw:
                raise RuntimeError("俱乐部关系详情写入失败，且原值恢复失败")
            raise
        return {
            "changed": True, "operation": "update",
            "record_address": hex(pointer),
            "parent_team_id": written_parent.team_id,
            "feeder_team_id": written_feeder.team_id,
            "type": target_type,
            "type_name": AFFILIATION_TYPE_NAMES[target_type],
        }


def _remove_pointer(
    process: Any, vector: _Vector, pointer: int,
) -> None:
    remaining = tuple(value for value in vector.pointers if value != pointer)
    if len(remaining) != len(vector.pointers) - 1:
        raise RuntimeError("俱乐部关系记录指针不唯一")
    compact = (
        struct.pack("<" + "Q" * len(remaining), *remaining)
        if remaining else b""
    )
    if compact:
        write_process_memory(process, vector.begin, compact)
    write_process_memory(
        process, vector.root,
        struct.pack("<QQQ", vector.begin, vector.begin + len(compact), vector.capacity),
    )


def _restore_vector(process: Any, reader: Reader, vector: _Vector) -> None:
    if vector.raw:
        write_process_memory(process, vector.begin, vector.raw)
    write_process_memory(process, vector.root, vector.header)
    if (
        reader.bytes(vector.root, 24) != vector.header
        or (vector.raw and reader.bytes(vector.begin, len(vector.raw)) != vector.raw)
    ):
        raise RuntimeError("俱乐部关系删除失败，且原列表恢复失败")


def delete_club_affiliation(
    parent: dict[str, Any], feeder: dict[str, Any], record_address: Any, *,
    expected_type: Any,
) -> dict[str, Any]:
    pid, layout, handle = _open_reader(write=True)
    with handle as process:
        module = layout.module(process)
        if not module:
            raise RuntimeError(f"{layout.module_name} 尚未加载")
        reader = Reader(process, module.base_address, layout)
        parent_target, feeder_target, parent_vector, feeder_vector = _validated_pair(
            reader, parent, feeder,
        )
        pointer, _raw, state = _expected_relation(
            reader, parent_target, feeder_target, parent_vector, feeder_vector,
            record_address, expected_type,
        )
        try:
            _remove_pointer(process, parent_vector, pointer)
            _remove_pointer(process, feeder_vector, pointer)
            if (
                pointer in _read_vector(reader, parent_target).pointers
                or pointer in _read_vector(reader, feeder_target).pointers
            ):
                raise RuntimeError("俱乐部关系删除后校验失败")
        except Exception:
            restore_error: Exception | None = None
            for vector in (parent_vector, feeder_vector):
                try:
                    _restore_vector(process, reader, vector)
                except Exception as error:
                    restore_error = error
            if restore_error is not None:
                raise RuntimeError("俱乐部关系删除失败，且双边列表恢复失败") from restore_error
            raise
        return {
            "changed": True, "operation": "delete",
            "record_address": hex(pointer),
            "parent_team_id": parent_target.team_id,
            "feeder_team_id": feeder_target.team_id,
            "type": state["type"],
            "type_name": AFFILIATION_TYPE_NAMES[state["type"]],
        }


__all__ = [
    "AFFILIATION_BENEFITS", "AFFILIATION_BENEFIT_DEPENDENCIES",
    "AFFILIATION_TYPE_NAMES",
    "affiliation_benefit_options", "affiliation_type_options",
    "create_club_affiliation", "delete_club_affiliation",
    "read_club_affiliations", "update_club_affiliation",
]
