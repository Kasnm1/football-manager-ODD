from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass
from datetime import date, timedelta
import struct
import time
from typing import Any, Callable

from fm_collector.win32 import (
    find_module, kernel32, open_process, read_process_memory, write_process_memory,
)
from tools.club_reader import (
    PERSON_CONTRACT, PERSON_CONTRACT_COLLECTION, _address, _name,
    _resolve_team_address, _scan_head_coaches, _scan_staff, _staff_scan_entries,
    _validated_player_person, is_club_controller_job_type,
)
from tools.database_index import database_index_for_reader
from tools.initial_data_audit import (
    ENTITY_UID, MANAGEABLE_CLUB_TEAM_TYPES, Reader, TEAM_CLUB, TEAM_MANAGER,
    TEAM_ROSTER_BEGIN, TEAM_ROSTER_END, decode_date, select_process_layout,
)
from tools.game_layout import FM24_EPIC_EXE_SHA256, FM24_EXE_SHA256, FM26_EXE_SHA256
from tools.future_transfers import has_confirmed_future_transfer


TH32CS_SNAPTHREAD = 0x00000004
THREAD_SUSPEND_RESUME = 0x0002
THREAD_GET_CONTEXT = 0x0008
THREAD_SET_CONTEXT = 0x0010
THREAD_QUERY_INFORMATION = 0x0040
CONTEXT_AMD64 = 0x00100000
CONTEXT_CONTROL = CONTEXT_AMD64 | 0x00000001
CONTEXT_INTEGER = CONTEXT_AMD64 | 0x00000002
CONTEXT_CAPTURE_FLAGS = CONTEXT_CONTROL | CONTEXT_INTEGER
THREAD_QUERY_SET_WIN32_START_ADDRESS = 9
MEM_COMMIT_RESERVE = 0x3000
MEM_RELEASE = 0x8000
PAGE_EXECUTE_READWRITE = 0x40
PAGE_READWRITE = 0x04
REMOTE_BLOCK_SIZE = 0x30000
REMOTE_CONTEXT_OFFSET = 0x2000
REMOTE_RESULT_OFFSET = 0x3000
REMOTE_STACK_TOP_OFFSET = 0x2F000
PLAYER_CURRENT_TEAM = 0x130
PLAYER_REGISTERED_TEAM = 0x138
TEAM_ROSTER_CAPACITY = 0x48
CONTRACT_SIZE = 0xC8
FM26_LOAN_CONTRACT_SIZE = 0xF8
FM26_PREVIOUS_CLUB = 0x108
FM26_JOINED_CLUB_DATE = 0x11C
FM26_CONTRACT_STARTED = 0x44
FM26_CONTRACT_EXPIRY = 0x48
FM26_CONTRACT_SIGNED = 0x4C
FM26_TRANSFER_STATUS = 0x57
CONTRACT_OWNER_INTERFACE_OFFSET = 0x28
FM26_CONTRACT_OWNER_INTERFACE_RVAS = {
    # db::ACTUAL_PLAYER PERSON subobject vtable -> contract-owner interface.
    0x4509D68: (0x4509D18, 0x4509804),
    # db::ACTUAL_PLAYER_AND_NON_PLAYER PERSON subobject and interface.
    0x4785848: (0x47857F8, 0x47852DC),
}
TERMINATE_CONTRACT_THUNK_PREFIX = bytes.fromhex("e96b847e07")
FM26_LOAN_CONSTRUCTOR_THUNK_PREFIX = bytes.fromhex("e9ebe68c10")
FM24_PERSON_CONTRACT = 0xC8
FM24_PERSON_CONTRACT_COLLECTION = 0xD0
FM24_JOINED_CLUB_DATE = 0x14C
FM24_CONTRACT_SIZE = 0xB8
FM24_LOAN_CONTRACT_POOL_RVA = 0x642D438
FM24_LOAN_CONTRACT_SIZE = 0xE8
FM24_OTHER_CONTRACTS_SIZE = 0x20
FM24_CLUB_TRANSFER_VTABLE_SLOT = 0x1E8
FM24_TRANSFER_EXE_SHA256 = FM24_EXE_SHA256
FM24_TRANSFER_EXE_SHA256S = frozenset({
    FM24_EXE_SHA256, FM24_EPIC_EXE_SHA256,
})
FM26_STAFF_RELEASE_EXE_SHA256 = "3653C97F9CCEC2BE28EDC4FAAE67304B5B6C26733F2F07DEA3E7C591D3B9FF73"
FM24_TRANSACTION_ALLOCATOR_PREFIX = bytes.fromhex("e9abaa6612")
FM24_TRANSACTION_CONSTRUCTOR_PREFIX = bytes.fromhex(
    "55564883ec28488d6c242048c74500feffffff48"
)
FM24_TRANSACTION_INITIALIZER_PREFIX = bytes.fromhex(
    "56574883ec384889ce488b7c2470488b4c24780f"
)
FM24_TRANSACTION_PREPARE_PREFIX = bytes.fromhex(
    "5657534883ec2089d74889ce0fb6056d769f0389c180e1fe"
)
FM24_TRANSACTION_COMMIT_PREFIX = bytes.fromhex(
    "4157415641554154565755534881ecf80000004989cd488b"
)
FM24_TRANSACTION_TRANSITION_PREFIX = bytes.fromhex(
    "41574156415541545657534883ec304489c74889ce0fbe41"
)
FM24_TRANSACTION_OUTER_SUBMIT_PREFIX = bytes.fromhex(
    "5541574156415541545657534881ec68020000488dac2480"
)
FM24_TRANSACTION_ENTRY_PREFIX = bytes.fromhex(
    "4157415641554154565755534881ec3802000048"
)
FM24_SOURCE_CLEANUP_PREFIX = bytes.fromhex("5541574156415541545657534881eca8")
FM24_TRANSFER_PREFIX = bytes.fromhex("4157415641554154565755534883ec78")
FM24_LOAN_TRANSFER_PREFIX = bytes.fromhex("4157415641554154565755534883ec78")
FM24_CONTRACT_FACTORY_PREFIX = bytes.fromhex("55564883ec48488d6c244048c74500fe")
FM24_LOAN_CONSTRUCTOR_PREFIX = bytes.fromhex("55564883ec28488d6c242048c74500fe")


class PlayerMovementOperationError(RuntimeError):
    """A language-independent classified player-movement failure."""

    def __init__(
        self, message: str, *, code: str, phase: str,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.error_code = str(code)
        self.error_phase = str(phase)
        self.retryable = bool(retryable)


class PlayerMovementContextChangedError(PlayerMovementOperationError):
    """A pre-write player-movement failure that may be fixed by rebinding IDs."""

    def __init__(
        self, message: str, *, retryable: bool = True,
        error_code: str = "stale_context", error_phase: str = "resolve_context",
    ) -> None:
        super().__init__(
            message, code=error_code, phase=error_phase, retryable=retryable,
        )


class PlayerMovementCapabilityError(PlayerMovementOperationError):
    """The selected build does not expose the requested movement capability."""

    def __init__(self, message: str, *, code: str = "unsupported_build") -> None:
        super().__init__(message, code=code, phase="capability")


@dataclass(frozen=True)
class PlayerMovementContext:
    """Live, UID-validated objects used by one movement transaction."""

    source_team: int
    target_team: int
    source_club: int
    target_club: int
    player: int
    person: int
    source_rows: tuple[dict[str, Any], ...]
    target_rows: tuple[dict[str, Any], ...]


def describe_player_movement_error(
    error: Exception, *, mode: str = "",
) -> dict[str, Any]:
    """Return a portable classification without exposing session addresses."""
    message = str(error).strip() or type(error).__name__
    code = str(getattr(error, "error_code", "") or "")
    phase = str(getattr(error, "error_phase", "") or "")
    retryable = bool(getattr(error, "retryable", False))

    if code:
        phase = phase or "preflight"
    elif isinstance(error, OSError):
        code, phase, retryable = "native_access", "native_access", True
    elif isinstance(error, (ValueError, KeyError, TypeError)):
        code, phase = "invalid_request", "request"
    else:
        code, phase = "transaction_failed", "preflight"

    return {
        "error": message,
        "error_code": code,
        "error_phase": phase,
        "retryable": bool(retryable),
        "operation": str(mode or "move"),
        "error_type": type(error).__name__,
    }


def _fm26_player_move_entry_failures(
    reader: Any, *, mode: str, module_base: int,
    offsets: dict[str, int], expected_club_vtable: int,
    person: int, primary_contract: int,
) -> tuple[str, ...]:
    """Validate only the native entries used by the requested transaction."""
    pool_key = (
        "player_move_loan_contract_pool_rva"
        if mode == "loan" else "player_move_contract_pool_rva"
    )
    expected_pool_size = FM26_LOAN_CONTRACT_SIZE if mode == "loan" else CONTRACT_SIZE
    pool = module_base + offsets[pool_key]
    factory_global = module_base + offsets["player_move_contract_factory_global_rva"]
    factory_target = module_base + offsets["player_move_contract_factory_target_rva"]
    main_vtable = module_base + offsets["player_move_main_contract_vtable_rva"]
    club_method = module_base + offsets["player_move_club_method_rva"]
    factory_object = int(reader.ptr(factory_global) or 0)
    factory_vtable = int(reader.ptr(factory_object) or 0) if factory_object else 0
    contract_owner_interface = person + CONTRACT_OWNER_INTERFACE_OFFSET
    failures: list[str] = []
    if reader.u32(pool + 0x38) != expected_pool_size:
        failures.append("合同池条目大小")
    if reader.ptr(factory_vtable + 0x18) != factory_target:
        failures.append("合同工厂入口")
    if reader.ptr(expected_club_vtable + 0x1C0) != club_method:
        failures.append("俱乐部事务入口")
    if reader.ptr(primary_contract) != main_vtable:
        failures.append("主合同类型")
    person_vtable = int(reader.ptr(person) or 0)
    person_vtable_rva = person_vtable - module_base if person_vtable else 0
    expected_interface_rvas = FM26_CONTRACT_OWNER_INTERFACE_RVAS.get(
        person_vtable_rva,
    )
    actual_interface = tuple(
        int(reader.ptr(contract_owner_interface + index * 8) or 0)
        for index in range(2)
    )
    if expected_interface_rvas is None:
        failures.append("人物接口类型")
    elif actual_interface != tuple(
        module_base + rva for rva in expected_interface_rvas
    ):
        failures.append("合同所有者接口")
    if mode == "loan":
        prepare_loan = module_base + offsets["player_move_prepare_loan_rva"]
        if reader.ptr(int(reader.ptr(person) or 0) + 0x320) != prepare_loan:
            failures.append("租借准备入口")
        loan_constructor = (
            module_base + offsets["player_move_loan_contract_constructor_rva"]
        )
        if (
            reader.bytes(
                loan_constructor, len(FM26_LOAN_CONSTRUCTOR_THUNK_PREFIX),
            ) != FM26_LOAN_CONSTRUCTOR_THUNK_PREFIX
        ):
            failures.append("租借合同构造器")
    elif mode == "transfer":
        terminate_contract = module_base + offsets["player_move_terminate_contract_rva"]
        if (
            reader.bytes(terminate_contract, len(TERMINATE_CONTRACT_THUNK_PREFIX))
            != TERMINATE_CONTRACT_THUNK_PREFIX
        ):
            failures.append("终止合同入口")
    else:
        failures.append("事务模式")
    return tuple(failures)
FM24_LOAN_VTABLE_LEA_OFFSET = 0x7A
FM24_TRANSACTION_SIZE = 0xD0
# A zero fee is interpreted as a normal zero-value offer by FM24's high-level
# transaction path. The final native comparison uses a nonzero recorded fee.
FM24_TRANSFER_FEE = 125_000

# FM26 permanent transfers use the exact-build FMRTE-style transaction below.
# FM24 uses separate full-contract and loan-contract pool transactions.
FM24_TRANSFER_RUNTIME_ENABLED = True
FM24_LOAN_RUNTIME_ENABLED = True
FM26_TRANSFER_RUNTIME_ENABLED = True
FM26_LOAN_RUNTIME_ENABLED = False


def player_movement_capabilities(layout: Any) -> dict[str, Any]:
    """Describe transfer and loan support independently for one exact layout."""
    game_key = str(getattr(layout, "key", "") or "")
    identity = {
        "game_key": game_key,
        "distribution": str(getattr(layout, "distribution", "") or ""),
        "game_version": str(getattr(layout, "game_version", "") or ""),
        "executable_sha256": str(getattr(layout, "executable_sha256", "") or ""),
    }

    def checked(
        enabled: bool, implementation: str, validator: Any,
        disabled_reason: str,
    ) -> dict[str, Any]:
        if not enabled:
            return {
                "enabled": False, "implementation": implementation,
                "reason": disabled_reason, "verified_identity": False,
            }
        try:
            validator(layout)
        except (AttributeError, RuntimeError, TypeError, ValueError) as error:
            return {
                "enabled": False, "implementation": implementation,
                "reason": str(error), "verified_identity": False,
            }
        return {
            "enabled": True, "implementation": implementation,
            "reason": None, "verified_identity": True,
        }

    if game_key == "fm24":
        transfer = checked(
            FM24_TRANSFER_RUNTIME_ENABLED, "fm24_main_contract_pool",
            _required_fm24_layout, "FM24 兼容转会桥梁已停用",
        )
        loan = checked(
            FM24_LOAN_RUNTIME_ENABLED, "fm24_loan_contract_pool",
            _required_fm24_layout, "FM24 租借尚未开放",
        )
    elif game_key == "fm26":
        transfer = checked(
            FM26_TRANSFER_RUNTIME_ENABLED, "fm26_main_contract_pool",
            _required_fm26_fmrte_layout,
            "FM26 永久转会已暂时关闭：合同池兼容事务在打开日程表时已两次触发游戏严重错误；"
            "需完成完整的报价、租借、训练位置、俱乐部登记和容器更新验证后才能重新开放",
        )

        def validate_fm26_loan(candidate: Any) -> dict[str, int]:
            if (
                str(getattr(candidate, "distribution", "")) != "steam"
                or str(getattr(candidate, "game_version", "")) != "26.3.2"
                or str(getattr(candidate, "executable_sha256", "") or "").upper()
                != str(FM26_EXE_SHA256).upper()
            ):
                raise RuntimeError("FM26 实验租借仅允许精确匹配 Steam 26.3.2")
            return _required_layout(candidate)

        loan = checked(
            FM26_LOAN_RUNTIME_ENABLED, "fm26_native_loan_constructor",
            validate_fm26_loan,
            "FM26 租借已暂时关闭：新构造器路径实机仍会导致游戏闪退",
        )
    else:
        reason = "球员转会与租借仅支持已识别的 FM24 与 FM26 版本"
        transfer = {
            "enabled": False, "implementation": "unavailable",
            "reason": reason, "verified_identity": False,
        }
        loan = dict(transfer)
    return {**identity, "transfer": transfer, "loan": loan}


class THREADENTRY32(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ThreadID", wintypes.DWORD),
        ("th32OwnerProcessID", wintypes.DWORD),
        ("tpBasePri", wintypes.LONG),
        ("tpDeltaPri", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
    ]


class CONTEXT64(ctypes.Structure):
    _fields_ = [
        ("P1Home", ctypes.c_uint64), ("P2Home", ctypes.c_uint64),
        ("P3Home", ctypes.c_uint64), ("P4Home", ctypes.c_uint64),
        ("P5Home", ctypes.c_uint64), ("P6Home", ctypes.c_uint64),
        ("ContextFlags", wintypes.DWORD), ("MxCsr", wintypes.DWORD),
        ("SegCs", wintypes.WORD), ("SegDs", wintypes.WORD),
        ("SegEs", wintypes.WORD), ("SegFs", wintypes.WORD),
        ("SegGs", wintypes.WORD), ("SegSs", wintypes.WORD),
        ("EFlags", wintypes.DWORD),
        ("Dr0", ctypes.c_uint64), ("Dr1", ctypes.c_uint64),
        ("Dr2", ctypes.c_uint64), ("Dr3", ctypes.c_uint64),
        ("Dr6", ctypes.c_uint64), ("Dr7", ctypes.c_uint64),
        ("Rax", ctypes.c_uint64), ("Rcx", ctypes.c_uint64),
        ("Rdx", ctypes.c_uint64), ("Rbx", ctypes.c_uint64),
        ("Rsp", ctypes.c_uint64), ("Rbp", ctypes.c_uint64),
        ("Rsi", ctypes.c_uint64), ("Rdi", ctypes.c_uint64),
        ("R8", ctypes.c_uint64), ("R9", ctypes.c_uint64),
        ("R10", ctypes.c_uint64), ("R11", ctypes.c_uint64),
        ("R12", ctypes.c_uint64), ("R13", ctypes.c_uint64),
        ("R14", ctypes.c_uint64), ("R15", ctypes.c_uint64),
        ("Rip", ctypes.c_uint64),
        ("_rest", ctypes.c_ubyte * (0x4D0 - 0x100)),
    ]


@dataclass(frozen=True)
class _ThreadRow:
    thread_id: int
    start_address: int
    created: int
    user_time: int
    stack_base: int


class _Code:
    def __init__(self) -> None:
        self.raw = bytearray()
        self.labels: dict[str, int] = {}
        self.jumps: list[tuple[int, str]] = []

    def emit(self, raw: bytes) -> None:
        self.raw.extend(raw)

    def label(self, name: str) -> None:
        self.labels[name] = len(self.raw)

    def jz(self, label: str) -> None:
        self.emit(b"\x0f\x84\0\0\0\0")
        self.jumps.append((len(self.raw) - 4, label))

    def jnz(self, label: str) -> None:
        self.emit(b"\x0f\x85\0\0\0\0")
        self.jumps.append((len(self.raw) - 4, label))

    def finish(self) -> bytes:
        for offset, label in self.jumps:
            target = self.labels[label]
            struct.pack_into("<i", self.raw, offset, target - (offset + 4))
        return bytes(self.raw)


def _configure_thread_api() -> None:
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Thread32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(THREADENTRY32)]
    kernel32.Thread32First.restype = wintypes.BOOL
    kernel32.Thread32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(THREADENTRY32)]
    kernel32.Thread32Next.restype = wintypes.BOOL
    kernel32.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenThread.restype = wintypes.HANDLE
    kernel32.SuspendThread.argtypes = [wintypes.HANDLE]
    kernel32.SuspendThread.restype = wintypes.DWORD
    kernel32.ResumeThread.argtypes = [wintypes.HANDLE]
    kernel32.ResumeThread.restype = wintypes.DWORD
    kernel32.GetThreadContext.argtypes = [wintypes.HANDLE, ctypes.POINTER(CONTEXT64)]
    kernel32.GetThreadContext.restype = wintypes.BOOL
    kernel32.SetThreadContext.argtypes = [wintypes.HANDLE, ctypes.POINTER(CONTEXT64)]
    kernel32.SetThreadContext.restype = wintypes.BOOL
    kernel32.GetThreadTimes.argtypes = [
        wintypes.HANDLE, ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
    ]
    kernel32.GetThreadTimes.restype = wintypes.BOOL
    kernel32.VirtualAllocEx.argtypes = [
        wintypes.HANDLE, ctypes.c_void_p, ctypes.c_size_t,
        wintypes.DWORD, wintypes.DWORD,
    ]
    kernel32.VirtualAllocEx.restype = ctypes.c_void_p
    kernel32.VirtualFreeEx.argtypes = [
        wintypes.HANDLE, ctypes.c_void_p, ctypes.c_size_t, wintypes.DWORD,
    ]
    kernel32.VirtualFreeEx.restype = wintypes.BOOL


def _thread_ids(pid: int) -> list[int]:
    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0)
    if snapshot == wintypes.HANDLE(-1).value:
        raise OSError(ctypes.get_last_error(), "CreateToolhelp32Snapshot failed")
    rows: list[int] = []
    try:
        entry = THREADENTRY32()
        entry.dwSize = ctypes.sizeof(entry)
        ok = kernel32.Thread32First(snapshot, ctypes.byref(entry))
        while ok:
            if int(entry.th32OwnerProcessID) == int(pid):
                rows.append(int(entry.th32ThreadID))
            ok = kernel32.Thread32Next(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    return rows


def _open_thread(thread_id: int) -> int:
    return int(kernel32.OpenThread(
        THREAD_SUSPEND_RESUME | THREAD_GET_CONTEXT | THREAD_SET_CONTEXT
        | THREAD_QUERY_INFORMATION,
        False, int(thread_id),
    ) or 0)


def _filetime(value: wintypes.FILETIME) -> int:
    return (int(value.dwHighDateTime) << 32) | int(value.dwLowDateTime)


def _get_context(handle: int) -> CONTEXT64:
    context = CONTEXT64()
    context.ContextFlags = CONTEXT_CAPTURE_FLAGS
    if not kernel32.GetThreadContext(handle, ctypes.byref(context)):
        raise OSError(ctypes.get_last_error(), "GetThreadContext failed")
    return context


def _thread_rows(process: Any, ucrt_base: int, ucrt_size: int) -> list[_ThreadRow]:
    ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
    query = ntdll.NtQueryInformationThread
    query.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
        ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong),
    ]
    query.restype = ctypes.c_long
    rows: list[_ThreadRow] = []
    for thread_id in _thread_ids(process.pid):
        handle = _open_thread(thread_id)
        if not handle:
            continue
        try:
            start = ctypes.c_void_p()
            returned = ctypes.c_ulong()
            if query(
                handle, THREAD_QUERY_SET_WIN32_START_ADDRESS,
                ctypes.byref(start), ctypes.sizeof(start), ctypes.byref(returned),
            ) != 0:
                continue
            start_address = int(start.value or 0)
            if not ucrt_base <= start_address < ucrt_base + ucrt_size:
                continue
            created, exited, kernel_time, user_time = (wintypes.FILETIME() for _ in range(4))
            if not kernel32.GetThreadTimes(
                handle, ctypes.byref(created), ctypes.byref(exited),
                ctypes.byref(kernel_time), ctypes.byref(user_time),
            ):
                continue
            suspended = kernel32.SuspendThread(handle) != 0xFFFFFFFF
            if not suspended:
                continue
            try:
                context = _get_context(handle)
            finally:
                kernel32.ResumeThread(handle)
            rows.append(_ThreadRow(
                thread_id=thread_id,
                start_address=start_address,
                created=_filetime(created),
                user_time=_filetime(user_time),
                stack_base=int(context.Rsp),
            ))
        finally:
            kernel32.CloseHandle(handle)
    return rows


def _logic_thread(process: Any) -> tuple[int, CONTEXT64]:
    ucrt = find_module(process, "ucrtbase.dll")
    ntdll = find_module(process, "ntdll.dll")
    if not ucrt or not ntdll:
        raise RuntimeError("无法定位 FM 游戏线程运行库")
    grouped: dict[int, list[_ThreadRow]] = {}
    for row in _thread_rows(process, ucrt.base_address, ucrt.size):
        grouped.setdefault(row.start_address, []).append(row)
    candidates: list[_ThreadRow] = []
    for rows in grouped.values():
        if len(rows) < 8:
            continue
        earliest = min(rows, key=lambda row: (row.created, row.stack_base))
        busiest = max(rows, key=lambda row: row.user_time)
        if earliest.thread_id == busiest.thread_id and busiest.user_time >= 10_000_000:
            candidates.append(busiest)
    if not candidates:
        raise RuntimeError("FM 游戏逻辑线程当前无法安全定位，请回到普通游戏界面后重试")
    selected = max(candidates, key=lambda row: row.user_time)
    first: CONTEXT64 | None = None
    for attempt in range(2):
        handle = _open_thread(selected.thread_id)
        if not handle:
            raise RuntimeError("FM 游戏逻辑线程已经变化，请重试")
        suspended = kernel32.SuspendThread(handle) != 0xFFFFFFFF
        try:
            if not suspended:
                raise RuntimeError("FM 游戏逻辑线程当前无法暂停，请重试")
            current = _get_context(handle)
        finally:
            if suspended:
                kernel32.ResumeThread(handle)
            kernel32.CloseHandle(handle)
        if not ntdll.base_address <= int(current.Rip) < ntdll.base_address + ntdll.size:
            raise RuntimeError("FM 正在处理游戏事务，请稍后重试")
        if first is not None and (
            int(first.Rip) != int(current.Rip) or int(first.Rsp) != int(current.Rsp)
        ):
            raise RuntimeError("FM 游戏逻辑线程尚未空闲，请稍后重试")
        first = current
        if attempt == 0:
            time.sleep(0.015)
    return selected.thread_id, first


def _encode_date(value: date) -> int:
    return (value.year << 16) | value.timetuple().tm_yday


def _add_year(value: date) -> date:
    return _add_years(value, 1)


def _add_years(value: date, years: int) -> date:
    try:
        return value.replace(year=value.year + int(years))
    except ValueError:
        return value.replace(year=value.year + int(years), day=28)


def _transfer_listing_status(raw: int, listed: bool) -> int:
    value = int(raw) & 0xFF
    return (value | 0x01) & ~0x10 if listed else value & ~0x01


def _mov_rax(value: int) -> bytes:
    return b"\x48\xb8" + struct.pack("<Q", int(value))


def _mov_rcx(value: int) -> bytes:
    return b"\x48\xb9" + struct.pack("<Q", int(value))


def _mov_rdx(value: int) -> bytes:
    return b"\x48\xba" + struct.pack("<Q", int(value))


def _mov_r8(value: int) -> bytes:
    return b"\x49\xb8" + struct.pack("<Q", int(value))


def _mov_r9(value: int) -> bytes:
    return b"\x49\xb9" + struct.pack("<Q", int(value))


def _store_rdi_qword(offset: int, value: int) -> bytes:
    return _mov_rax(value) + b"\x48\x89\x87" + struct.pack("<i", int(offset))


def _store_rdi_dword(offset: int, value: int) -> bytes:
    return b"\xc7\x87" + struct.pack("<iI", int(offset), int(value) & 0xFFFFFFFF)


def _store_absolute_qword(address: int, value: int) -> bytes:
    return _mov_rax(value) + b"\x48\xa3" + struct.pack("<Q", int(address))


def _load_absolute_rax(address: int) -> bytes:
    return b"\x48\xa1" + struct.pack("<Q", int(address))


def _contract_template(main_vtable: int) -> bytes:
    raw = bytearray(CONTRACT_SIZE)
    struct.pack_into("<Q", raw, 0x00, main_vtable)
    struct.pack_into("<Q", raw, 0x3C, 0xFFFFFFFFFFFFFFFF)
    struct.pack_into("<I", raw, 0x44, 0x076C0001)
    struct.pack_into("<I", raw, 0x48, 0x076C0001)
    struct.pack_into("<I", raw, 0x4C, 0x076C0001)
    struct.pack_into("<Q", raw, 0x50, 0x076C0001)
    struct.pack_into("<B", raw, 0x5A, 0x64)
    struct.pack_into("<H", raw, 0x5C, 0xFF14)
    struct.pack_into("<Q", raw, 0xB4, 0x076C0001)
    struct.pack_into("<H", raw, 0xBC, 0x076C)
    struct.pack_into("<H", raw, 0xC3, 0xFF01)
    return bytes(raw)


def _fm24_native_stack_rsp(original_rsp: int) -> int:
    # Keep the transaction on the FM logic thread's real stack. The stub
    # subtracts 0x68, so an 8-mod-16 entry RSP preserves Win64 call alignment.
    return (int(original_rsp) & ~0xF) - 8


def _build_fm24_transfer_stub(
    *, allocator: int, constructor: int, initializer: int,
    transaction_prepare: int, transaction_commit: int,
    transaction_transition: int, transaction_outer_submit: int,
    transaction_vtable: int,
    transaction_context: int, person: int, source_team: int,
    target_team: int, transfer_fee: int, wrapper_address: int,
    result_address: int,
) -> bytes:
    code = _Code()
    transaction_slot = result_address + 8
    stage_slot = result_address + 16
    code.emit(b"\x48\x83\xec\x68")
    for offset in range(0x20, 0x60, 8):
        code.emit(_mov_rax(0) + b"\x48\x89\x44\x24" + bytes([offset]))

    # Build the same 0xD0 reference-counted transaction used by the native
    # editor. The high-level commit function owns cleanup and contract creation.
    code.emit(_store_absolute_qword(stage_slot, 1))
    code.emit(_mov_rcx(FM24_TRANSACTION_SIZE) + _mov_rax(allocator) + b"\xff\xd0")
    code.emit(b"\x48\x85\xc0")
    code.jz("failed_allocation")
    code.emit(b"\x48\x89\xc7")
    code.emit(_mov_rax(transaction_slot) + b"\x48\x89\x38")
    code.emit(_store_absolute_qword(stage_slot, 2))
    code.emit(b"\x48\x89\xf9" + _mov_rax(constructor) + b"\xff\xd0")
    code.emit(b"\x48\x39\xf8")
    code.jnz("failed_validation")
    code.emit(_store_absolute_qword(stage_slot, 3))
    code.emit(b"\xf0\xff\x47\x08")

    # Dedicated initializer arguments: source Team, optional date pointer and
    # commit flag occupy caller stack slots 5-7.
    code.emit(_mov_rax(source_team) + b"\x48\x89\x44\x24\x20")
    code.emit(_mov_rax(0) + b"\x48\x89\x44\x24\x28")
    code.emit(_mov_rax(1) + b"\x48\x89\x44\x24\x30")
    code.emit(
        b"\x48\x89\xf9" + _mov_rdx(person) + _mov_r8(target_team)
        + _mov_r9(transfer_fee) + _mov_rax(initializer) + b"\xff\xd0"
    )
    code.emit(_store_absolute_qword(stage_slot, 4))

    # The editor form normalizes constructor padding and optional transaction
    # fields before submission. Zero is a captured valid value for the optional
    # registration fields; leaving allocator bytes untouched made the high-level
    # commit consume stale values and crash FM24.
    code.emit(_store_rdi_dword(0x0C, 1))
    code.emit(_store_rdi_dword(0x7C, 0))
    code.emit(_store_rdi_qword(0x80, 0x1000))
    code.emit(_store_rdi_dword(0x8C, 0x00000100))

    # Refuse to enter the native transaction unless construction produced the
    # exact captured vtable and core fields.
    for offset, value in (
        (0x00, transaction_vtable),
        (0x30, person),
        (0x38, target_team),
        (0x40, target_team),
        (0x48, source_team),
    ):
        code.emit(_mov_rax(value) + b"\x48\x39\x87" + struct.pack("<i", offset))
        code.jnz("failed_validation")
    code.emit(
        b"\x81\xbf" + struct.pack("<iI", 0x50, int(transfer_fee) & 0xFFFFFFFF)
    )
    code.jnz("failed_validation")
    code.emit(b"\x83\x7f\x08\x01")
    code.jnz("failed_validation")

    # Native editor construction calls the auxiliary preparation method with
    # a zero flag, then lets the high-level transaction builder assign the
    # status, sequence and date fields before it enters the Club commit path.
    code.emit(b"\x48\x89\xf9" + _mov_rdx(0) + _mov_rax(transaction_prepare) + b"\xff\xd0")
    code.emit(b"\x84\xc0")
    code.jz("failed_validation")
    code.emit(b"\x48\x83\xbf" + struct.pack("<i", 0xA8) + b"\x00")
    code.jz("failed_validation")

    code.emit(_mov_rax(wrapper_address) + b"\x48\x89\x38")
    code.emit(b"\xf0\xff\x47\x08")
    for offset in (0x20, 0x28, 0x30):
        code.emit(_mov_rax(0) + b"\x48\x89\x44\x24" + bytes([offset]))
    code.emit(_store_absolute_qword(stage_slot, 5))
    code.emit(
        _mov_rcx(transaction_context) + _mov_rdx(wrapper_address)
        + _mov_r8(1) + _mov_r9(0) + _mov_rax(transaction_commit) + b"\xff\xd0"
    )
    code.emit(b"\x83\xf8\xff")
    code.jz("failed_after_commit")
    code.emit(_store_absolute_qword(stage_slot, 6))

    # The native editor performs a second submission phase after the high-level
    # offer transaction returns. Match its reference ownership and state change
    # before dispatching the outer immediate-commit entry.
    code.emit(b"\xf0\xff\x4f\x08")
    code.emit(
        b"\x48\x89\xf9" + _mov_rdx(5) + _mov_r8(4)
        + _mov_rax(transaction_transition) + b"\xff\xd0"
    )
    code.emit(_store_absolute_qword(stage_slot, 7))
    code.emit(b"\xf0\xff\x47\x08")
    code.emit(
        _mov_rcx(transaction_context) + _mov_rdx(wrapper_address)
        + _mov_r8(0) + _mov_rax(transaction_outer_submit) + b"\xff\xd0"
    )
    code.emit(b"\xf0\xff\x4f\x08\xf0\xff\x4f\x08")
    code.emit(b"\x83\x7f\x08\x01")
    code.jnz("failed_after_commit")
    code.emit(_store_absolute_qword(stage_slot, 8))
    code.emit(_store_absolute_qword(result_address, 1))
    code.emit(b"\xe9\0\0\0\0")
    code.jumps.append((len(code.raw) - 4, "restore"))
    code.label("failed_validation")
    code.emit(_store_absolute_qword(result_address, 3))
    code.emit(b"\xe9\0\0\0\0")
    code.jumps.append((len(code.raw) - 4, "restore"))
    code.label("failed_after_commit")
    code.emit(_store_absolute_qword(result_address, 3))
    code.jumps.append((len(code.raw) - 4, "restore"))
    code.label("failed_allocation")
    code.emit(_store_absolute_qword(result_address, 2))
    code.label("restore")
    # Park after publishing the result. The controller restores the original
    # thread CONTEXT with SetThreadContext; FM24 crashed reproducibly when this
    # long transaction tried to tail-jump through RtlRestoreContext itself.
    code.emit(b"\xeb\xfe")
    return code.finish()


def _build_fm24_direct_transfer_stub(
    *, contract_factory: int, transfer_main: int, main_vtable: int,
    target_club: int, person: int, source_team: int, target_team: int,
    game_date_address: int, contract_scalars: bytes,
    start_code: int, expiry_code: int, result_address: int,
    context_address: int, rtl_restore: int,
) -> bytes:
    if len(contract_scalars) != 0x40:
        raise ValueError("FM24 main-contract scalar template must be 0x40 bytes")

    code = _Code()
    contract_address_slot = result_address + 8
    call_result_slot = result_address + 16
    code.emit(b"\x48\x83\xec\x68")

    # Factory type 0 constructs the native FULL_CONTRACT object and its owned
    # containers. Only copy the old contract's scalar block; copying pointers
    # beyond +0x58 would alias bonuses and clauses owned by the old contract.
    code.emit(_mov_rcx(0) + _mov_rax(contract_factory) + b"\xff\xd0")
    code.emit(b"\x48\x85\xc0")
    code.jz("failed_without_contract")
    code.emit(b"\x48\x89\xc7")
    code.emit(b"\x48\xa3" + struct.pack("<Q", contract_address_slot))
    code.emit(_mov_rax(main_vtable) + b"\x48\x39\x07")
    code.jnz("failed_contract_validation")
    code.emit(_store_rdi_qword(0x08, person))
    code.emit(_store_rdi_qword(0x10, target_team))
    for offset in range(0x18, 0x58, 8):
        value = struct.unpack_from("<Q", contract_scalars, offset - 0x18)[0]
        code.emit(_store_rdi_qword(offset, value))
    code.emit(_store_rdi_dword(0x3C, start_code))
    code.emit(_store_rdi_dword(0x40, expiry_code))

    # Captured permanent-transfer call to Team vtable +0x1E8:
    #   target Club, Person, true, target Team,
    #   mode byte 0, null, target Team, game-date address,
    #   prepared main contract, source Team, null, true.
    # The native capture looked pointer-like because the caller only overwrote
    # the low byte of an existing stack qword. The callee reads that low byte
    # directly; passing an actual scratch pointer selects the wrong mode.
    frame = bytearray(0x60)
    struct.pack_into("<Q", frame, 0x30, target_team)
    struct.pack_into("<Q", frame, 0x38, game_date_address)
    struct.pack_into("<Q", frame, 0x48, source_team)
    frame[0x58] = 1
    for offset in range(0x20, 0x60, 8):
        if offset == 0x40:
            code.emit(b"\x48\x89\x7c\x24\x40")
        else:
            value = struct.unpack_from("<Q", frame, offset)[0]
            code.emit(_mov_rax(value) + b"\x48\x89\x44\x24" + bytes([offset]))
    code.emit(
        _mov_rcx(target_club) + _mov_rdx(person) + _mov_r8(1)
        + _mov_r9(target_team) + _mov_rax(transfer_main) + b"\xff\xd0"
    )
    code.emit(b"\x84\xc0")
    code.jz("failed_after_call")
    code.emit(_store_absolute_qword(call_result_slot, 1))
    code.emit(_store_absolute_qword(result_address, 1))
    code.emit(b"\xe9\0\0\0\0")
    code.jumps.append((len(code.raw) - 4, "restore"))

    code.label("failed_contract_validation")
    code.emit(_store_absolute_qword(result_address, 2))
    code.emit(b"\xe9\0\0\0\0")
    code.jumps.append((len(code.raw) - 4, "restore"))
    code.label("failed_after_call")
    code.emit(_store_absolute_qword(result_address, 3))
    code.emit(b"\xe9\0\0\0\0")
    code.jumps.append((len(code.raw) - 4, "restore"))
    code.label("failed_without_contract")
    code.emit(_store_absolute_qword(result_address, 2))
    code.label("restore")
    code.emit(
        b"\x48\x83\xc4\x68" + _mov_rcx(context_address)
        + b"\x31\xd2" + _mov_rax(rtl_restore) + b"\xff\xe0"
    )
    return code.finish()


def _build_fm24_loan_stub(
    *, contract_factory: int, transfer_main: int,
    target_club: int, person: int, target_team: int,
    start_code: int, expiry_code: int, result_address: int,
    context_address: int, rtl_restore: int,
) -> bytes:
    code = _Code()
    contract_address_slot = result_address + 8
    call_result_slot = result_address + 16
    code.emit(b"\x48\x83\xec\x68")

    # FM24's native factory type 1 allocates and constructs LOAN_CONTRACT.
    code.emit(_mov_rcx(1) + _mov_rax(contract_factory) + b"\xff\xd0")
    code.emit(b"\x48\x85\xc0")
    code.jz("failed_without_contract")
    code.emit(b"\x48\x89\xc7")
    code.emit(b"\x48\xa3" + struct.pack("<Q", contract_address_slot))
    code.emit(_store_rdi_qword(0x10, target_team))
    code.emit(_store_rdi_dword(0x3C, start_code))
    code.emit(_store_rdi_dword(0x40, expiry_code))
    code.emit(_store_rdi_dword(0x44, start_code))
    code.emit(_store_rdi_dword(0x48, 0x076C0001))
    code.emit(_store_rdi_dword(0x4C, 0x00030003))
    code.emit(_store_rdi_dword(0x50, 0x48640000))
    code.emit(_store_rdi_dword(0x54, 0x0000FF14))

    # Captured FM24 loan call: target Team is argument 7 and the prepared
    # contract is argument 9. The remaining optional relationship arguments
    # are null for a direct editor loan.
    frame = bytearray(0x60)
    struct.pack_into("<Q", frame, 0x30, target_team)
    for offset in range(0x20, 0x60, 8):
        if offset == 0x40:
            code.emit(b"\x48\x89\x7c\x24\x40")
        else:
            value = struct.unpack_from("<Q", frame, offset)[0]
            code.emit(_mov_rax(value) + b"\x48\x89\x44\x24" + bytes([offset]))
    code.emit(
        _mov_rcx(target_club) + _mov_rdx(person) + _mov_r8(1)
        + _mov_r9(target_team) + _mov_rax(transfer_main) + b"\xff\xd0"
    )
    code.emit(b"\x84\xc0")
    code.jz("failed_after_call")
    code.emit(_store_absolute_qword(call_result_slot, 1))
    code.emit(_store_absolute_qword(result_address, 1))
    code.emit(b"\xe9\0\0\0\0")
    code.jumps.append((len(code.raw) - 4, "restore"))

    # A false result can follow partial game-side work. Keep the native object
    # alive and require the caller to inspect the save instead of freeing it.
    code.label("failed_after_call")
    code.emit(_store_absolute_qword(result_address, 3))
    code.emit(b"\xe9\0\0\0\0")
    code.jumps.append((len(code.raw) - 4, "restore"))
    code.label("failed_without_contract")
    code.emit(_store_absolute_qword(result_address, 2))
    code.label("restore")
    code.emit(
        b"\x48\x83\xc4\x68" + _mov_rcx(context_address)
        + b"\x31\xd2" + _mov_rax(rtl_restore) + b"\xff\xe0"
    )
    return code.finish()


def _build_stub(
    *, mode: str, pool: int, allocate: int, release: int,
    factory_object: int, factory_target: int, main_vtable: int,
    loan_vtable: int, loan_constructor: int,
    terminate_contract: int, prepare_loan: int,
    club_method: int,
    source_club: int, target_club: int, contract_owner_interface: int,
    person: int, player: int, source_team: int, target_team: int,
    source_roster_slot: int, source_roster_end: int,
    game_date_address: int, start_code: int, expiry_code: int,
    source_wage: int, result_address: int, context_address: int,
    rtl_restore: int,
) -> bytes:
    code = _Code()
    contract_address_slot = result_address + 8
    call_result_slot = result_address + 16
    expected_vtable = loan_vtable if mode == "loan" else main_vtable
    factory_mode = 1 if mode == "loan" else 0x40

    template = _contract_template(expected_vtable) if mode == "transfer" else None
    contract_size = CONTRACT_SIZE if mode == "transfer" else FM26_LOAN_CONTRACT_SIZE

    code.emit(b"\x48\x83\xec\x68")
    code.emit(
        _mov_rcx(pool) + b"\xba" + struct.pack("<I", contract_size)
        + _mov_rax(allocate) + b"\xff\xd0"
    )
    code.emit(b"\x48\x85\xc0")
    code.jz("failed_without_contract")
    code.emit(b"\x48\x89\xc7")
    code.emit(b"\x48\xa3" + struct.pack("<Q", contract_address_slot))
    if mode == "loan":
        code.emit(b"\x48\x89\xf9" + _mov_rax(loan_constructor) + b"\xff\xd0")
        code.emit(_store_rdi_dword(0x44, start_code))
    else:
        assert template is not None
        for offset in range(0, contract_size, 8):
            code.emit(_store_rdi_qword(
                offset, struct.unpack_from("<Q", template, offset)[0],
            ))

    # The factory uses the same stack contract observed in both editor paths.
    frame = bytearray(0x60)
    struct.pack_into("<I", frame, 0x20, factory_mode)
    struct.pack_into("<I", frame, 0x38, 0xFFFFFFFF)
    frame[0x50] = 1
    for offset in range(0x20, 0x60, 8):
        value = struct.unpack_from("<Q", frame, offset)[0]
        code.emit(_mov_rax(value) + b"\x48\x89\x44\x24" + bytes([offset]))
    code.emit(
        _mov_rcx(factory_object) + b"\x48\x89\xfa" + _mov_r8(contract_owner_interface)
        + _mov_r9(target_club) + _mov_rax(factory_target) + b"\xff\xd0"
    )
    code.emit(_mov_rax(expected_vtable) + b"\x48\x39\x07")
    code.jz("factory_vtable_ok")
    code.emit(b"\x48\x8b\x07\x48\xa3" + struct.pack("<Q", call_result_slot))
    code.emit(b"\xe9\0\0\0\0")
    code.jumps.append((len(code.raw) - 4, "failed_factory_vtable"))
    code.label("factory_vtable_ok")
    code.emit(_mov_rax(person) + b"\x48\x39\x47\x08")
    code.jz("factory_person_ok")
    code.emit(b"\xe9\0\0\0\0")
    code.jumps.append((len(code.raw) - 4, "failed_factory_person"))
    code.label("factory_person_ok")
    code.emit(_mov_rax(target_team) + b"\x48\x39\x47\x10")
    code.jz("factory_team_ok")
    code.emit(b"\xe9\0\0\0\0")
    code.jumps.append((len(code.raw) - 4, "failed_factory_team"))
    code.label("factory_team_ok")
    if mode == "transfer":
        code.emit(_store_rdi_dword(0x20, source_wage))
        code.emit(_store_rdi_dword(0x44, start_code))
        code.emit(_store_rdi_dword(0x48, expiry_code))
        # The editor terminates the old primary contract before asking the
        # destination club to adopt the newly created contract.
        code.emit(
            _mov_rcx(person) + _mov_rdx(1) + _mov_r8(game_date_address)
            + _mov_r9(source_club) + _mov_rax(terminate_contract) + b"\xff\xd0"
        )
    else:
        code.emit(
            _mov_rcx(person) + _mov_rdx(source_team) + _mov_r8(0xFF)
            + _mov_rax(prepare_loan) + b"\xff\xd0"
        )

    move_frame = bytearray(0x60)
    struct.pack_into("<Q", move_frame, 0x30, target_team)
    struct.pack_into("<Q", move_frame, 0x38, game_date_address if mode == "transfer" else 0)
    struct.pack_into("<Q", move_frame, 0x48, source_team if mode == "transfer" else 0)
    move_frame[0x58] = 1
    for offset in range(0x20, 0x60, 8):
        if offset == 0x40:
            code.emit(b"\x48\x89\x7c\x24\x40")
        else:
            value = struct.unpack_from("<Q", move_frame, offset)[0]
            code.emit(_mov_rax(value) + b"\x48\x89\x44\x24" + bytes([offset]))
    code.emit(
        _mov_rcx(target_club) + _mov_rdx(person) + b"\x41\xb8\x01\x00\x00\x00"
        + _mov_r9(target_team) + _mov_rax(club_method) + b"\xff\xd0"
    )
    code.emit(b"\x84\xc0")
    code.jz("failed_after_commit" if mode == "transfer" else "failed_loan_club")
    if mode == "transfer":
        # The captured Club method adds the destination roster entry but does
        # not remove the source entry on its own. Only compact the original
        # vector when both its end and the unique player slot are unchanged.
        code.emit(_load_absolute_rax(source_team + TEAM_ROSTER_END))
        code.emit(_mov_rcx(source_roster_end) + b"\x48\x39\xc8")
        code.jz("source_roster_end_ok")
        code.emit(b"\xe9\0\0\0\0")
        code.jumps.append((len(code.raw) - 4, "failed_roster_cleanup"))
        code.label("source_roster_end_ok")
        code.emit(_load_absolute_rax(source_roster_slot))
        code.emit(_mov_rcx(player) + b"\x48\x39\xc8")
        code.jz("source_roster_slot_ok")
        code.emit(b"\xe9\0\0\0\0")
        code.jumps.append((len(code.raw) - 4, "failed_roster_cleanup"))
        code.label("source_roster_slot_ok")
        for slot in range(source_roster_slot, source_roster_end - 8, 8):
            code.emit(_load_absolute_rax(slot + 8))
            code.emit(b"\x48\xa3" + struct.pack("<Q", slot))
        code.emit(_store_absolute_qword(source_roster_end - 8, 0))
        code.emit(_store_absolute_qword(
            source_team + TEAM_ROSTER_END, source_roster_end - 8,
        ))
    code.emit(_store_absolute_qword(call_result_slot, 1))
    code.emit(_store_absolute_qword(result_address, 1))
    code.emit(b"\xe9\0\0\0\0")
    code.jumps.append((len(code.raw) - 4, "restore"))

    code.label("failed_with_contract")
    code.emit(_mov_rcx(pool) + b"\x48\x89\xfa" + _mov_rax(release) + b"\xff\xd0")
    code.emit(b"\xe9\0\0\0\0")
    code.jumps.append((len(code.raw) - 4, "failed_without_contract"))
    for label, status in (
        ("failed_factory_vtable", 7),
        ("failed_factory_person", 8),
        ("failed_factory_team", 9),
    ):
        code.label(label)
        code.emit(_mov_rcx(pool) + b"\x48\x89\xfa" + _mov_rax(release) + b"\xff\xd0")
        code.emit(_store_absolute_qword(result_address, status))
        code.emit(b"\xe9\0\0\0\0")
        code.jumps.append((len(code.raw) - 4, "restore"))
    code.label("failed_after_commit")
    code.emit(_store_absolute_qword(result_address, 3))
    code.emit(b"\xe9\0\0\0\0")
    code.jumps.append((len(code.raw) - 4, "restore"))
    if mode == "transfer":
        code.label("failed_roster_cleanup")
        code.emit(_store_absolute_qword(result_address, 4))
        code.emit(b"\xe9\0\0\0\0")
        code.jumps.append((len(code.raw) - 4, "restore"))
    else:
        code.label("failed_loan_club")
        code.emit(_mov_rcx(pool) + b"\x48\x89\xfa" + _mov_rax(release) + b"\xff\xd0")
        code.emit(_store_absolute_qword(result_address, 6))
        code.emit(b"\xe9\0\0\0\0")
        code.jumps.append((len(code.raw) - 4, "restore"))
    code.label("failed_without_contract")
    code.emit(_store_absolute_qword(result_address, 2))
    code.label("restore")
    code.emit(
        b"\x48\x83\xc4\x68" + _mov_rcx(context_address)
        + b"\x31\xd2" + _mov_rax(rtl_restore) + b"\xff\xe0"
    )
    return code.finish()


def _required_layout(layout: Any) -> dict[str, int]:
    names = (
        "player_move_contract_pool_rva", "player_move_contract_allocate_rva",
        "player_move_loan_contract_pool_rva",
        "player_move_loan_contract_constructor_rva",
        "player_move_contract_release_rva", "player_move_contract_factory_global_rva",
        "player_move_contract_factory_target_rva",
        "player_move_main_contract_vtable_rva", "player_move_terminate_contract_rva",
        "player_move_prepare_loan_rva",
        "player_move_club_method_rva",
        "loan_contract_vtable_rva", "game_date_rva",
    )
    values = {name: getattr(layout, name, None) for name in names}
    if (
        layout.key != "fm26" or layout.distribution != "steam"
        or str(getattr(layout, "game_version", "")) != "26.3.2"
        or any(value is None for value in values.values())
    ):
        raise RuntimeError("球员转会与租借目前仅支持已验证的 FM26 Steam 26.3.2")
    return {name: int(value) for name, value in values.items()}


def _required_fm26_fmrte_layout(layout: Any) -> dict[str, int]:
    """Return only the fields used by the external-editor-style transaction."""
    names = (
        "player_move_contract_pool_rva",
        "player_move_main_contract_vtable_rva",
        "game_date_rva",
    )
    values = {name: getattr(layout, name, None) for name in names}
    if (
        layout.key != "fm26" or layout.distribution != "steam"
        or str(getattr(layout, "game_version", "")) != "26.3.2"
        or str(getattr(layout, "executable_sha256", "")).upper()
        != FM26_STAFF_RELEASE_EXE_SHA256
        or any(value is None for value in values.values())
    ):
        raise RuntimeError(
            "球员转会目前仅支持已验证的 FM26 Steam 26.3.2"
        )
    return {name: int(value) for name, value in values.items()}


def _required_fm24_legacy_layout(layout: Any, mode: str = "transfer") -> dict[str, int]:
    common = (
        "player_move_main_contract_vtable_rva",
        "player_move_fm24_source_cleanup_rva",
        "player_move_fm24_transfer_rva",
        "player_move_fm24_contract_factory_rva",
        "player_move_fm24_loan_constructor_rva",
        "loan_contract_vtable_rva",
        "game_date_rva",
    )
    if mode == "transfer":
        specific = (
            "player_move_fm24_transaction_allocator_rva",
            "player_move_fm24_transaction_constructor_rva",
            "player_move_fm24_transaction_initializer_rva",
            "player_move_fm24_transaction_prepare_rva",
            "player_move_fm24_transaction_commit_rva",
            "player_move_fm24_transaction_transition_rva",
            "player_move_fm24_transaction_outer_submit_rva",
            "player_move_fm24_transaction_entry_rva",
            "player_move_fm24_transaction_vtable_rva",
            "player_move_fm24_context_root_rva",
            "player_move_fm24_context_vtable_rva",
        )
    elif mode == "loan":
        specific = ()
    else:
        raise ValueError("FM24 player movement mode is invalid")
    names = common + specific
    values = {name: getattr(layout, name, None) for name in names}
    if (
        layout.key != "fm24" or layout.distribution != "steam"
        or str(getattr(layout, "executable_sha256", "")).upper()
        != FM24_TRANSFER_EXE_SHA256
        or any(value is None for value in values.values())
    ):
        raise RuntimeError("原生高层球员事务仅支持已验证的 FM24 Steam 24.4.2")
    return {name: int(value) for name, value in values.items()}


def _required_fm24_layout(layout: Any) -> dict[str, int]:
    names = (
        "player_move_contract_pool_rva",
        "player_move_main_contract_vtable_rva",
        "game_date_rva",
    )
    values = {name: getattr(layout, name, None) for name in names}
    if (
        layout.key != "fm24" or layout.distribution not in {"steam", "epic"}
        or str(getattr(layout, "executable_sha256", "")).upper()
        not in FM24_TRANSFER_EXE_SHA256S
        or any(value is None for value in values.values())
    ):
        raise RuntimeError("球员转会与租借目前仅支持已识别的 FM24 Steam/Epic 24.4.2")
    return {name: int(value) for name, value in values.items()}


def _required_staff_release_layout(layout: Any) -> dict[str, int]:
    if layout.key == "fm24":
        # Staff dismissal is a pure main-contract pool transaction.  It does
        # not use the FM24 game-date global required by the player-move path.
        names = (
            "player_move_contract_pool_rva",
            "player_move_main_contract_vtable_rva",
        )
        values = {name: getattr(layout, name, None) for name in names}
        if (
            layout.distribution not in {"steam", "epic"}
            or any(value is None for value in values.values())
        ):
            raise RuntimeError(
                "FM24 人员事务仅支持已识别且具备合同池布局的 Steam/Epic 版本"
            )
        return {
            **{name: int(value) for name, value in values.items()},
            "person_contract_offset": FM24_PERSON_CONTRACT,
            "person_contract_collection_offset": FM24_PERSON_CONTRACT_COLLECTION,
            "contract_size": FM24_CONTRACT_SIZE,
        }
    names = (
        "player_move_contract_pool_rva",
        "player_move_main_contract_vtable_rva",
    )
    values = {name: getattr(layout, name, None) for name in names}
    if (
        layout.key != "fm26" or layout.distribution != "steam"
        or str(getattr(layout, "game_version", "")) != "26.3.2"
        or str(getattr(layout, "executable_sha256", "")).upper()
        != FM26_STAFF_RELEASE_EXE_SHA256
        or any(value is None for value in values.values())
    ):
        raise RuntimeError(
            "职员解雇目前仅支持已验证的 FM24 Steam 24.4.2 与 FM26 Steam 26.3.2"
        )
    return {
        **{name: int(value) for name, value in values.items()},
        "person_contract_offset": PERSON_CONTRACT,
        "person_contract_collection_offset": PERSON_CONTRACT_COLLECTION,
        "contract_size": CONTRACT_SIZE,
    }


def _validated_staff_main_contract(
    reader: Reader, module: Any, layout: Any, *,
    person: int, staff_id: int, team: int, person_contract_offset: int,
    listed_contract: int | None = None, expected_job_type: int | None = None,
    label: str = "职员",
) -> int:
    failures: list[str] = []
    expected_person_vtable = (
        module.base_address + int(layout.staff_person_vtable_rva)
        if layout.staff_person_vtable_rva is not None else 0
    )
    main_contract_vtable = (
        module.base_address + int(layout.player_move_main_contract_vtable_rva)
        if layout.player_move_main_contract_vtable_rva is not None else 0
    )
    if not person:
        failures.append("人物地址为空")
        contract = 0
    else:
        if not expected_person_vtable or reader.ptr(person) != expected_person_vtable:
            failures.append("人物 vtable 不匹配")
        if int(reader.u32(person + ENTITY_UID) or 0) != int(staff_id):
            failures.append("UID 不匹配")
        contract = int(reader.ptr(person + int(person_contract_offset)) or 0)
    if not contract:
        failures.append("主合同指针为空")
    else:
        if listed_contract is not None and contract != int(listed_contract):
            failures.append("主合同指针与职员列表不一致")
        if not main_contract_vtable or reader.ptr(contract) != main_contract_vtable:
            failures.append("主合同 vtable 不匹配")
        if reader.ptr(contract + 0x08) != person:
            failures.append("主合同 Person 反向引用不匹配")
        if reader.ptr(contract + 0x10) != team:
            failures.append("主合同 Team 引用不匹配")
        if expected_job_type is not None:
            job_offset = layout.staff_job_type_offset
            if (
                job_offset is None
                or reader.u8(contract + int(job_offset)) != int(expected_job_type)
            ):
                failures.append("合同职务编号不匹配")
    if failures:
        error = ValueError(f"{label}合同状态已变化，请刷新后重试")
        error.validation_failures = tuple(failures)  # type: ignore[attr-defined]
        raise error
    return contract


def _move_fm24_owned_club_player_legacy(
    *, pid: int, layout: Any,
    source_team_id: int, source_team_address: Any,
    target_team_id: int, target_team_address: Any,
    player_id: int, mode: str,
) -> dict[str, Any]:
    offsets = _required_fm24_legacy_layout(layout, mode)
    with open_process(pid, write_memory=True) as process:
        module = layout.module(process)
        if not module:
            raise RuntimeError(f"{layout.module_name} 尚未加载")
        reader = _writable_player_movement_reader(process, module.base_address, layout)
        source_team = _resolve_team_address(reader, int(source_team_id), source_team_address)
        target_team = _resolve_team_address(reader, int(target_team_id), target_team_address)
        source_club = int(reader.ptr(source_team + TEAM_CLUB) or 0)
        target_club = int(reader.ptr(target_team + TEAM_CLUB) or 0)
        expected_club_vtable = module.base_address + int(layout.club_vtable_rva)
        if (
            not source_club or not target_club
            or reader.ptr(source_club) != expected_club_vtable
            or reader.ptr(target_club) != expected_club_vtable
            or int(reader.u32(source_club + ENTITY_UID) or 0) != int(source_team_id)
            or int(reader.u32(target_club + ENTITY_UID) or 0) != int(target_team_id)
        ):
            raise PlayerMovementContextChangedError("来源或目标俱乐部原生对象校验失败")

        source_roster = {
            int(row.get("id") or 0): _address(row.get("address"))
            for row in reader.roster(source_team)
        }
        target_roster_ids = {
            int(row.get("id") or 0) for row in reader.roster(target_team)
        }
        player = int(source_roster.get(int(player_id)) or 0)
        if not player:
            raise ValueError("该球员已经不在来源俱乐部一线队")
        if int(player_id) in target_roster_ids:
            raise ValueError("该球员已经在目标俱乐部一线队")
        person = _validated_player_person(reader, player, int(player_id))
        primary_contract = int(reader.ptr(person + FM24_PERSON_CONTRACT) or 0)
        other_contracts = int(reader.ptr(person + FM24_PERSON_CONTRACT_COLLECTION) or 0)
        main_vtable = module.base_address + offsets["player_move_main_contract_vtable_rva"]
        if (
            not primary_contract or other_contracts
            or reader.ptr(primary_contract) != main_vtable
            or reader.ptr(primary_contract + 0x08) != person
            or not _team_belongs_to_club(
                reader, int(reader.ptr(primary_contract + 0x10) or 0), source_club,
            )
        ):
            raise ValueError("球员当前主合同或附加合同已经变化，请刷新后重试")

        source_cleanup = module.base_address + offsets["player_move_fm24_source_cleanup_rva"]
        transfer_main = module.base_address + offsets["player_move_fm24_transfer_rva"]
        contract_factory = (
            module.base_address + offsets["player_move_fm24_contract_factory_rva"]
        )
        loan_constructor = (
            module.base_address + offsets["player_move_fm24_loan_constructor_rva"]
        )
        loan_vtable = module.base_address + offsets["loan_contract_vtable_rva"]
        loan_vtable_lea = reader.bytes(loan_constructor + FM24_LOAN_VTABLE_LEA_OFFSET, 7)
        loan_vtable_target = 0
        if len(loan_vtable_lea) == 7 and loan_vtable_lea[:3] == b"\x48\x8d\x05":
            loan_vtable_target = (
                loan_constructor + FM24_LOAN_VTABLE_LEA_OFFSET + 7
                + struct.unpack_from("<i", loan_vtable_lea, 3)[0]
            )
        if (
            reader.bytes(source_cleanup, len(FM24_SOURCE_CLEANUP_PREFIX))
            != FM24_SOURCE_CLEANUP_PREFIX
            or reader.bytes(transfer_main, len(FM24_TRANSFER_PREFIX))
            != FM24_TRANSFER_PREFIX
            or reader.ptr(expected_club_vtable + FM24_CLUB_TRANSFER_VTABLE_SLOT)
            != transfer_main
            or reader.bytes(contract_factory, len(FM24_CONTRACT_FACTORY_PREFIX))
            != FM24_CONTRACT_FACTORY_PREFIX
            or reader.bytes(loan_constructor, len(FM24_LOAN_CONSTRUCTOR_PREFIX))
            != FM24_LOAN_CONSTRUCTOR_PREFIX
            or loan_vtable_target != loan_vtable
        ):
            raise RuntimeError("FM24 原生球员事务入口校验失败，当前游戏构建可能已经变化")

        game_date_address = module.base_address + offsets["game_date_rva"]
        if not decode_date(int(reader.u32(game_date_address) or 0)):
            raise RuntimeError("无法读取当前游戏日期")
        ntdll_remote = find_module(process, "ntdll.dll")
        ntdll_local = ctypes.WinDLL("ntdll", use_last_error=True)
        rtl_local = int(ctypes.cast(ntdll_local.RtlRestoreContext, ctypes.c_void_p).value or 0)
        rtl_restore = ntdll_remote.base_address + rtl_local - int(ntdll_local._handle)
        game_date_code = int(reader.u32(game_date_address) or 0)
        current_date = decode_date(game_date_code)
        if not current_date:
            raise RuntimeError("无法读取当前游戏日期")
        if mode == "loan":
            loan_expiry = current_date + timedelta(days=365)
            expiry_code = (
                _encode_date(loan_expiry) | (game_date_code & 0xFE00)
            )
            placeholder = _build_fm24_loan_stub(
                contract_factory=contract_factory, transfer_main=transfer_main,
                target_club=target_club, person=person, target_team=target_team,
                start_code=game_date_code, expiry_code=expiry_code,
                result_address=0, context_address=0, rtl_restore=rtl_restore,
            )
        else:
            transfer_expiry = current_date
            for _ in range(3):
                transfer_expiry = _add_year(transfer_expiry)
            expiry_code = _encode_date(transfer_expiry) | (game_date_code & 0xFE00)
            contract_scalars = reader.bytes(primary_contract + 0x18, 0x40)
            if len(contract_scalars) != 0x40:
                raise RuntimeError("FM24 source main-contract scalars are unreadable")
            placeholder = _build_fm24_direct_transfer_stub(
                contract_factory=contract_factory, transfer_main=transfer_main,
                main_vtable=main_vtable, target_club=target_club,
                person=person, source_team=source_team, target_team=target_team,
                game_date_address=game_date_address,
                contract_scalars=contract_scalars,
                start_code=game_date_code, expiry_code=expiry_code,
                result_address=0, context_address=0, rtl_restore=rtl_restore,
            )
        thread_id, idle_context = _logic_thread(process)
        cave = int(kernel32.VirtualAllocEx(
            process.handle, None, REMOTE_BLOCK_SIZE,
            MEM_COMMIT_RESERVE, PAGE_EXECUTE_READWRITE,
        ) or 0)
        if not cave:
            raise RuntimeError("无法建立 FM24 原生球员转会调用区")
        result_address = cave + REMOTE_RESULT_OFFSET
        context_address = cave + REMOTE_CONTEXT_OFFSET
        wrapper_address = result_address + 0x40
        if mode == "loan":
            code = _build_fm24_loan_stub(
                contract_factory=contract_factory, transfer_main=transfer_main,
                target_club=target_club, person=person, target_team=target_team,
                start_code=game_date_code, expiry_code=expiry_code,
                result_address=result_address, context_address=context_address,
                rtl_restore=rtl_restore,
            )
        else:
            code = _build_fm24_direct_transfer_stub(
                contract_factory=contract_factory, transfer_main=transfer_main,
                main_vtable=main_vtable, target_club=target_club,
                person=person, source_team=source_team, target_team=target_team,
                game_date_address=game_date_address,
                contract_scalars=contract_scalars,
                start_code=game_date_code, expiry_code=expiry_code,
                result_address=result_address, context_address=context_address,
                rtl_restore=rtl_restore,
            )
        if len(code) != len(placeholder):
            kernel32.VirtualFreeEx(process.handle, ctypes.c_void_p(cave), 0, MEM_RELEASE)
            raise RuntimeError("FM24 原生球员转会代码生成失败")

        completed = False
        handle = 0
        try:
            write_process_memory(process, cave, code)
            write_process_memory(process, result_address, b"\0" * 0x48)
            kernel32.FlushInstructionCache(process.handle, ctypes.c_void_p(cave), len(code))
            handle = _open_thread(thread_id)
            if not handle or kernel32.SuspendThread(handle) == 0xFFFFFFFF:
                raise RuntimeError(f"{game_label} 游戏逻辑线程当前无法暂停，请重试")
            suspended = True
            try:
                original = _get_context(handle)
                if int(original.Rip) != int(idle_context.Rip) or int(original.Rsp) != int(idle_context.Rsp):
                    raise RuntimeError("FM24 游戏逻辑线程刚刚恢复工作，请稍后重试")
                write_process_memory(process, context_address, bytes(original))
                hijacked = CONTEXT64.from_buffer_copy(bytes(original))
                hijacked.Rip = cave
                hijacked.Rsp = _fm24_native_stack_rsp(int(original.Rsp))
                if not kernel32.SetThreadContext(handle, ctypes.byref(hijacked)):
                    raise OSError(ctypes.get_last_error(), "SetThreadContext failed")
            finally:
                if suspended:
                    kernel32.ResumeThread(handle)
            deadline = time.monotonic() + 10.0
            status = middle_value = stage = 0
            while time.monotonic() < deadline:
                raw = read_process_memory(process, result_address, 24) or b""
                if len(raw) == 24:
                    status, middle_value, stage = struct.unpack("<QQQ", raw)
                    if status in {1, 2, 3}:
                        completed = True
                        break
                time.sleep(0.01)
            if not completed:
                stage_text = f"，最后阶段 {stage}" if mode == "transfer" else ""
                raise RuntimeError(
                    f"FM24 原生球员转会执行超时{stage_text}；请不要立即重复操作"
                )
            if status == 2:
                raise RuntimeError("FM24 无法创建原生租借合同")
            if status == 3:
                raise RuntimeError(
                    "FM24 目标俱乐部拒绝了球员事务，且游戏可能已经部分处理；"
                    "请立即检查游戏状态且不要保存"
                )
            expected_stage = 1
            if status != 1 or stage != expected_stage:
                raise RuntimeError("FM24 拒绝了本次球员转会事务")
            time.sleep(0.25 if mode == "transfer" else 0.05)
        finally:
            if handle:
                kernel32.CloseHandle(handle)
            if completed:
                kernel32.VirtualFreeEx(process.handle, ctypes.c_void_p(cave), 0, MEM_RELEASE)

        _invalidate_reader_after_write(reader)
        source_after = {int(row.get("id") or 0) for row in reader.roster(source_team)}
        target_after = {int(row.get("id") or 0) for row in reader.roster(target_team)}
        if mode == "loan":
            contract = middle_value
            collection = int(reader.ptr(person + FM24_PERSON_CONTRACT_COLLECTION) or 0)
            collection_contract = int(reader.ptr(collection) or 0) if collection else 0
            valid = (
                int(player_id) in source_after and int(player_id) in target_after
                and reader.ptr(player + PLAYER_CURRENT_TEAM) == target_team
                and reader.ptr(person + FM24_PERSON_CONTRACT) == primary_contract
                and _team_belongs_to_club(
                    reader, int(reader.ptr(primary_contract + 0x10) or 0), source_club,
                )
                and collection_contract == int(contract) and contract
                and not reader.ptr(collection + 0x08)
                and not reader.ptr(collection + 0x10)
                and reader.ptr(contract) == loan_vtable
                and reader.ptr(contract + 0x08) == person
                and reader.ptr(contract + 0x10) == target_team
                and int(reader.u32(contract + 0x3C) or 0) == game_date_code
                and int(reader.u32(contract + 0x40) or 0) == expiry_code
            )
            if not valid:
                raise RuntimeError(
                    "FM24 已执行租借，但球员合同或一线队名单回读不一致；"
                    "请先检查游戏状态且不要保存"
                )
            return {
                "player_id": int(player_id), "mode": "loan",
                "source_team_id": int(source_team_id),
                "target_team_id": int(target_team_id),
                "contract_address": hex(int(contract)),
                "thread_id": int(thread_id), "verified": True,
                "loan_start_date": current_date.isoformat(),
                "loan_expiry_date": loan_expiry.isoformat(),
                "native_transfer_fee_recorded": False,
            }

        constructed_contract = int(middle_value or 0)
        new_contract = int(reader.ptr(person + FM24_PERSON_CONTRACT) or 0)
        valid = (
            int(player_id) not in source_after and int(player_id) in target_after
            and reader.ptr(player + PLAYER_CURRENT_TEAM) == target_team
            and new_contract and new_contract == constructed_contract
            and new_contract != primary_contract
            and reader.ptr(new_contract) == main_vtable
            and reader.ptr(new_contract + 0x08) == person
            and reader.ptr(new_contract + 0x10) == target_team
            and not reader.ptr(person + FM24_PERSON_CONTRACT_COLLECTION)
            and int(reader.u32(new_contract + 0x3C) or 0) == game_date_code
            and int(reader.u32(new_contract + 0x40) or 0) == expiry_code
        )
        if not valid:
            raise RuntimeError(
                "FM24 转会事务已返回，但球员合同或一线队名单未完整更新；"
                "请先检查游戏状态且不要保存"
            )
        return {
            "player_id": int(player_id), "mode": "transfer",
            "source_team_id": int(source_team_id),
            "target_team_id": int(target_team_id),
            "contract_address": hex(new_contract),
            "thread_id": int(thread_id), "verified": True,
            "native_transfer_fee_recorded": False,
        }


def _roster_vector(reader: Reader, team: int) -> tuple[int, int, int, list[int]]:
    begin = int(reader.ptr(team + TEAM_ROSTER_BEGIN) or 0)
    end = int(reader.ptr(team + TEAM_ROSTER_END) or 0)
    capacity = int(reader.ptr(team + TEAM_ROSTER_CAPACITY) or 0)
    if (
        not begin or not (begin <= end <= capacity)
        or (end - begin) % 8 or (capacity - begin) % 8
        or end - begin > 200 * 8 or capacity - begin > 256 * 8
    ):
        raise RuntimeError("俱乐部一线队名单向量校验失败")
    raw = reader.bytes(begin, end - begin)
    if len(raw) != end - begin:
        raise RuntimeError("俱乐部一线队名单无法完整读取")
    pointers = list(struct.unpack(f"<{len(raw) // 8}Q", raw)) if raw else []
    if any(not pointer for pointer in pointers):
        raise RuntimeError("俱乐部一线队名单包含空球员地址")
    return begin, end, capacity, pointers


def _remove_unique_pointer(pointers: list[int], player: int) -> list[int]:
    if pointers.count(int(player)) != 1:
        raise RuntimeError("来源俱乐部名单中的球员地址不是唯一值")
    return [pointer for pointer in pointers if pointer != int(player)]


def _pointer_matches(reader: Reader, address: int, expected: int) -> bool:
    """Compare a native pointer while preserving Reader's null-as-None API."""
    return int(reader.ptr(address) or 0) == int(expected)


def _invalidate_reader_after_write(reader: Any) -> None:
    """Force post-transaction checks to read current process memory."""
    invalidate = getattr(reader, "invalidate_prefetch", None)
    if callable(invalidate):
        invalidate()
        return
    page_cache = getattr(reader, "_page_cache", None)
    if isinstance(page_cache, dict):
        page_cache.clear()


def _writable_player_movement_reader(
    process: Any, module_base: int, layout: Any,
) -> Reader:
    """Create a live reader for a transaction that mutates the same process.

    Roster expansion prefetches whole memory pages. Keeping those pages while
    this module writes contracts and roster vectors makes final verification
    machine/timing dependent: a cached pre-write page can make a successful
    transaction look unchanged. Writable club-reader paths already disable
    this cache; player movement follows the same rule.
    """
    reader = Reader(process, module_base, layout)
    reader._page_cache = None  # type: ignore[assignment]
    return reader


def _resolve_owned_source_team(
    reader: Reader, module: Any, layout: Any, *,
    source_team_id: int, source_team_address: Any,
    source_squad_team_id: int = 0, source_squad_team_address: Any = 0,
) -> tuple[int, int]:
    """Resolve the owned Club through its first team and its selected roster."""
    club_team = _resolve_team_address(
        reader, int(source_team_id), source_team_address,
    )
    source_club = int(reader.ptr(club_team + TEAM_CLUB) or 0)
    expected_club_vtable = int(module.base_address) + int(layout.club_vtable_rva)
    if (
        not source_club
        or reader.ptr(source_club) != expected_club_vtable
        or int(reader.u32(source_club + ENTITY_UID) or 0) != int(source_team_id)
    ):
        raise PlayerMovementContextChangedError("来源俱乐部原生对象校验失败")
    squad_team = club_team
    if int(source_squad_team_id or 0):
        squad_team = _resolve_team_address(
            reader, int(source_squad_team_id), source_squad_team_address,
        )
    if squad_team != club_team:
        type_offset = getattr(layout, "team_type_offset", None)
        raw_squad_type = (
            reader.u8(squad_team + int(type_offset))
            if type_offset is not None else None
        )
        squad_type = int(raw_squad_type) if raw_squad_type is not None else -1
        if (
            reader.ptr(squad_team + TEAM_CLUB) != source_club
            or squad_type not in MANAGEABLE_CLUB_TEAM_TYPES
        ):
            raise PlayerMovementContextChangedError(
                "所选阵容已不属于来源俱乐部，或该阵容类型尚未验证"
            )
    return squad_team, source_club


def _indexed_player_target(
    reader: Reader, player_id: int,
) -> tuple[int, int] | None:
    """Return a session-indexed Player/Person candidate for one UID.

    The database index is an address accelerator only. Callers must still
    validate the object and its live team/roster/contract state before any
    write. Failure to obtain the index is intentionally silent so older or
    transient sessions can use the roster fallback below.
    """
    directory = database_index_for_reader(reader)
    if directory is None:
        return None
    try:
        target = directory.player_target_for_uid(int(player_id))
    except (OSError, RuntimeError, TypeError, ValueError):
        return None
    if not target or len(target) < 2:
        return None
    person, player = int(target[0] or 0), int(target[1] or 0)
    return (player, person) if player and person else None


def _resolve_player_movement_context(
    reader: Reader, module: Any, layout: Any, *,
    source_team_id: int, source_team_address: Any,
    source_squad_team_id: int = 0, source_squad_team_address: Any = 0,
    target_team_id: int, target_team_address: Any,
    player_id: int,
) -> PlayerMovementContext:
    """Resolve every mutable object from stable UIDs in one live reader."""
    source_team, source_club = _resolve_owned_source_team(
        reader, module, layout,
        source_team_id=source_team_id,
        source_team_address=source_team_address,
        source_squad_team_id=source_squad_team_id,
        source_squad_team_address=source_squad_team_address,
    )
    target_team = _resolve_team_address(
        reader, int(target_team_id), target_team_address,
    )
    target_club = int(reader.ptr(target_team + TEAM_CLUB) or 0)
    expected_club_vtable = int(module.base_address) + int(layout.club_vtable_rva)
    if (
        not source_club or not target_club
        or reader.ptr(source_club) != expected_club_vtable
        or reader.ptr(target_club) != expected_club_vtable
        or int(reader.u32(source_club + ENTITY_UID) or 0) != int(source_team_id)
        or int(reader.u32(target_club + ENTITY_UID) or 0) != int(target_team_id)
    ):
        raise PlayerMovementContextChangedError(
            "来源或目标俱乐部原生对象校验失败"
        )


    source_rows = tuple(reader.roster(source_team))
    target_rows = tuple(reader.roster(target_team))
    source_players = {
        int(row.get("id") or 0): _address(row.get("address"))
        for row in source_rows
    }
    indexed_target = _indexed_player_target(reader, int(player_id))
    roster_player = int(source_players.get(int(player_id)) or 0)
    # Prefer the live roster pointer when both sources disagree. The global
    # index repairs a missing/stale roster address, never bypasses membership.
    indexed_player = int(indexed_target[0] if indexed_target else 0)
    indexed_in_roster = indexed_player and any(
        _address(row.get("address")) == indexed_player for row in source_rows
    )
    player = roster_player or (indexed_player if indexed_in_roster else 0)
    if not player:
        raise PlayerMovementContextChangedError(
            "该球员已经不在来源俱乐部一线队"
        )
    if any(int(row.get("id") or 0) == int(player_id) for row in target_rows):
        raise ValueError("该球员已经在目标俱乐部一线队")
    try:
        person = _validated_player_person(reader, player, int(player_id))
    except ValueError as error:
        if roster_player and player != roster_player:
            person = _validated_player_person(reader, roster_player, int(player_id))
            player = roster_player
        else:
            raise PlayerMovementContextChangedError(str(error)) from error
    if indexed_target and player == indexed_target[0] and person != indexed_target[1]:
        raise PlayerMovementContextChangedError(
            "全球球员索引中的 Person 对象已经变化",
        )
    return PlayerMovementContext(
        source_team=source_team,
        target_team=target_team,
        source_club=source_club,
        target_club=target_club,
        player=player,
        person=person,
        source_rows=source_rows,
        target_rows=target_rows,
    )


def _reassignment_roster_vector(
    reader: Reader, team: int,
) -> tuple[int, int, int, list[int]]:
    """Read a squad roster while allowing a genuinely empty target vector."""
    begin = int(reader.ptr(team + TEAM_ROSTER_BEGIN) or 0)
    end = int(reader.ptr(team + TEAM_ROSTER_END) or 0)
    capacity = int(reader.ptr(team + TEAM_ROSTER_CAPACITY) or 0)
    if not begin and not end and not capacity:
        return 0, 0, 0, []
    return _roster_vector(reader, team)


def reassign_owned_club_player_squad(
    *, club_team_id: int, club_team_address: Any,
    source_squad_team_id: int, source_squad_team_address: Any,
    target_squad_team_id: int, target_squad_team_address: Any,
    player_id: int,
) -> dict[str, Any]:
    """Promote or demote a player without changing contract ownership."""
    if int(club_team_id) <= 0 or int(player_id) <= 0:
        raise ValueError("俱乐部或球员 ID 无效")
    if int(source_squad_team_id) <= 0 or int(target_squad_team_id) <= 0:
        raise ValueError("来源阵容或目标阵容无效")
    if int(source_squad_team_id) == int(target_squad_team_id):
        raise ValueError("来源阵容与目标阵容不能相同")

    _configure_thread_api()
    pid, _path, layout = select_process_layout()
    if str(getattr(layout, "key", "")) not in {"fm24", "fm26"}:
        raise PlayerMovementCapabilityError("阵容调整仅支持已识别的 FM24 与 FM26")
    type_offset = getattr(layout, "team_type_offset", None)
    if type_offset is None:
        raise PlayerMovementCapabilityError(
            "当前游戏构建尚未定位可校验的青年队 Team Type"
        )

    game_label = "FM24" if layout.key == "fm24" else "FM26"
    person_contract_offset = (
        FM24_PERSON_CONTRACT if layout.key == "fm24" else PERSON_CONTRACT
    )
    person_contract_collection_offset = (
        FM24_PERSON_CONTRACT_COLLECTION
        if layout.key == "fm24" else PERSON_CONTRACT_COLLECTION
    )

    with open_process(pid, write_memory=True) as process:
        module = layout.module(process)
        if not module:
            raise RuntimeError(f"{layout.module_name} 尚未加载")
        reader = _writable_player_movement_reader(
            process, module.base_address, layout,
        )
        source_team, club = _resolve_owned_source_team(
            reader, module, layout,
            source_team_id=int(club_team_id),
            source_team_address=club_team_address,
            source_squad_team_id=int(source_squad_team_id),
            source_squad_team_address=source_squad_team_address,
        )
        target_team = _resolve_team_address(
            reader, int(target_squad_team_id), target_squad_team_address,
        )
        source_type_raw = reader.u8(source_team + int(type_offset))
        target_type_raw = reader.u8(target_team + int(type_offset))
        source_type = int(source_type_raw) if source_type_raw is not None else -1
        target_type = int(target_type_raw) if target_type_raw is not None else -1
        if (
            reader.ptr(target_team + TEAM_CLUB) != club
            or source_type not in MANAGEABLE_CLUB_TEAM_TYPES
            or target_type not in MANAGEABLE_CLUB_TEAM_TYPES
        ):
            raise PlayerMovementContextChangedError(
                "来源阵容或目标阵容已不属于该俱乐部"
            )
        if (source_type == 0) == (target_type == 0):
            raise ValueError("阵容调整只允许一线队与非一线队之间上调或下调")

        source_rows = tuple(reader.roster(source_team))
        target_rows = tuple(reader.roster(target_team))
        source_players = {
            int(row.get("id") or 0): _address(row.get("address"))
            for row in source_rows
        }
        player = int(source_players.get(int(player_id)) or 0)
        if not player:
            raise PlayerMovementContextChangedError(
                "球员已不在所选来源阵容，请刷新后重试"
            )
        if any(int(row.get("id") or 0) == int(player_id) for row in target_rows):
            raise PlayerMovementContextChangedError("球员已经位于目标阵容")
        try:
            person = _validated_player_person(reader, player, int(player_id))
        except ValueError as error:
            raise PlayerMovementContextChangedError(str(error)) from error

        primary_contract = int(
            reader.ptr(person + person_contract_offset) or 0
        )
        other_contracts = int(
            reader.ptr(person + person_contract_collection_offset) or 0
        )
        contract_team = (
            int(reader.ptr(primary_contract + 0x10) or 0)
            if primary_contract else 0
        )
        registered_team = int(
            reader.ptr(player + PLAYER_REGISTERED_TEAM) or 0
        )
        if (
            not primary_contract
            or other_contracts
            or reader.ptr(primary_contract + 0x08) != person
            or not _team_belongs_to_club(reader, contract_team, club)
            or reader.ptr(player + PLAYER_CURRENT_TEAM) != source_team
            or (
                registered_team
                and not _team_belongs_to_club(reader, registered_team, club)
            )
        ):
            raise PlayerMovementContextChangedError(
                "球员当前阵容、注册队、主合同或租借状态已发生变化"
            )

        source_begin, source_end, source_capacity, source_pointers = (
            _roster_vector(reader, source_team)
        )
        target_begin, target_end, target_capacity, target_pointers = (
            _reassignment_roster_vector(reader, target_team)
        )
        source_after = _remove_unique_pointer(source_pointers, player)
        if player in target_pointers:
            raise PlayerMovementContextChangedError("目标阵容已包含该球员")
        target_after = [*target_pointers, player]

        snapshots: list[tuple[int, bytes]] = []
        seen: set[tuple[int, int]] = set()

        def snapshot(address: int, size: int) -> None:
            if size <= 0:
                return
            key = (int(address), int(size))
            raw = reader.bytes(address, size)
            if not raw or len(raw) != size:
                raise RuntimeError(
                    f"{game_label} 阵容调整快照读取失败：{hex(address)}"
                )
            if key not in seen:
                snapshots.append((address, raw))
                seen.add(key)

        def checked_write(address: int, raw: bytes) -> None:
            write_process_memory(process, address, raw)
            if read_process_memory(process, address, len(raw)) != raw:
                raise RuntimeError(
                    f"{game_label} 阵容调整写后回读失败：{hex(address)}"
                )

        snapshot(source_team + TEAM_ROSTER_BEGIN, 24)
        snapshot(target_team + TEAM_ROSTER_BEGIN, 24)
        snapshot(source_begin, source_capacity - source_begin)
        if target_begin:
            snapshot(target_begin, target_capacity - target_begin)
        snapshot(player + PLAYER_CURRENT_TEAM, 16)
        snapshot(person + person_contract_offset, 8)
        snapshot(person + person_contract_collection_offset, 8)

        thread_id, idle_context = _logic_thread(process)
        handle = _open_thread(thread_id)
        allocated_target_storage = 0
        suspended = False
        committed = False
        try:
            if not handle or kernel32.SuspendThread(handle) == 0xFFFFFFFF:
                raise RuntimeError(
                    f"{game_label} 游戏逻辑线程当前无法暂停，请稍后重试"
                )
            suspended = True
            current_context = _get_context(handle)
            if (
                int(current_context.Rip) != int(idle_context.Rip)
                or int(current_context.Rsp) != int(idle_context.Rsp)
            ):
                raise RuntimeError(
                    f"{game_label} 游戏逻辑线程刚刚恢复工作，请稍后重试"
                )
            for address, raw in snapshots:
                if read_process_memory(process, address, len(raw)) != raw:
                    raise PlayerMovementContextChangedError(
                        "球员或阵容数据在提交前已发生变化"
                    )
            if (
                reader.ptr(source_team + TEAM_CLUB) != club
                or reader.ptr(target_team + TEAM_CLUB) != club
                or reader.u8(source_team + int(type_offset)) != source_type
                or reader.u8(target_team + int(type_offset)) != target_type
            ):
                raise PlayerMovementContextChangedError(
                    "来源阵容或目标阵容对象在提交前已发生变化"
                )

            target_size = len(target_after) * 8
            if target_begin and target_end + 8 <= target_capacity:
                target_storage = target_begin
                target_storage_capacity = target_capacity
            else:
                allocated_target_storage = int(kernel32.VirtualAllocEx(
                    process.handle, None, max(0x1000, target_size),
                    MEM_COMMIT_RESERVE, PAGE_READWRITE,
                ) or 0)
                if not allocated_target_storage:
                    raise RuntimeError(
                        f"{game_label} 目标阵容名单扩展失败"
                    )
                target_storage = allocated_target_storage
                target_storage_capacity = target_storage + target_size

            target_raw = struct.pack(
                f"<{len(target_after)}Q", *target_after,
            )
            checked_write(target_storage, target_raw)
            checked_write(
                target_team + TEAM_ROSTER_BEGIN,
                struct.pack(
                    "<QQQ", target_storage, target_storage + target_size,
                    target_storage_capacity,
                ),
            )
            source_raw = (
                struct.pack(f"<{len(source_after)}Q", *source_after)
                if source_after else b""
            )
            checked_write(source_begin, source_raw + b"\0" * 8)
            checked_write(
                source_team + TEAM_ROSTER_END,
                struct.pack("<Q", source_end - 8),
            )
            checked_write(
                player + PLAYER_CURRENT_TEAM, struct.pack("<Q", target_team),
            )

            _invalidate_reader_after_write(reader)
            _, _, _, source_verified = _reassignment_roster_vector(
                reader, source_team,
            )
            _, _, _, target_verified = _reassignment_roster_vector(
                reader, target_team,
            )
            valid = (
                player not in source_verified
                and target_verified.count(player) == 1
                and reader.ptr(player + PLAYER_CURRENT_TEAM) == target_team
                and int(reader.ptr(player + PLAYER_REGISTERED_TEAM) or 0)
                == registered_team
                and reader.ptr(person + person_contract_offset)
                == primary_contract
                and not reader.ptr(person + person_contract_collection_offset)
                and reader.ptr(primary_contract + 0x10) == contract_team
                and reader.ptr(source_team + TEAM_CLUB) == club
                and reader.ptr(target_team + TEAM_CLUB) == club
                and reader.u8(source_team + int(type_offset)) == source_type
                and reader.u8(target_team + int(type_offset)) == target_type
            )
            if not valid:
                raise RuntimeError(
                    f"{game_label} 阵容调整写后结构校验失败"
                )
            committed = True
        except Exception:
            rollback_failed = False
            for address, raw in reversed(snapshots):
                try:
                    write_process_memory(process, address, raw)
                except Exception:
                    rollback_failed = True
            _invalidate_reader_after_write(reader)
            for address, raw in snapshots:
                if read_process_memory(process, address, len(raw)) != raw:
                    rollback_failed = True
            if allocated_target_storage and not kernel32.VirtualFreeEx(
                process.handle, ctypes.c_void_p(allocated_target_storage),
                0, MEM_RELEASE,
            ):
                rollback_failed = True
            if rollback_failed:
                raise RuntimeError(
                    f"{game_label} 阵容调整失败且回滚未完整，请不要保存当前存档"
                )
            raise
        finally:
            if suspended:
                kernel32.ResumeThread(handle)
            if handle:
                kernel32.CloseHandle(handle)

        return {
            "player_id": int(player_id),
            "mode": "squad_reassignment",
            "club_team_id": int(club_team_id),
            "source_squad_team_id": int(source_squad_team_id),
            "target_squad_team_id": int(target_squad_team_id),
            "source_squad_type": source_type,
            "target_squad_type": target_type,
            "registered_team_preserved": True,
            "contract_team_preserved": True,
            "thread_id": int(thread_id),
            "verified": bool(committed),
        }


def _fm24_loan_contract_data(
    template: bytes, *, loan_vtable: int, person: int, target_team: int,
    start_code: int, expiry_code: int,
) -> bytes:
    if len(template) != FM24_LOAN_CONTRACT_SIZE:
        raise ValueError("FM24 loan contract template size is invalid")
    data = bytearray(template)
    struct.pack_into("<Q", data, 0x00, int(loan_vtable))
    struct.pack_into("<Q", data, 0x08, int(person))
    struct.pack_into("<Q", data, 0x10, int(target_team))
    struct.pack_into("<I", data, 0x18, 0)
    struct.pack_into("<I", data, 0x3C, int(start_code))
    struct.pack_into("<I", data, 0x40, int(expiry_code))
    struct.pack_into("<I", data, 0x44, int(start_code))
    return bytes(data)


def _fm24_other_contracts_data(loan_contract: int) -> bytes:
    data = bytearray(FM24_OTHER_CONTRACTS_SIZE)
    struct.pack_into("<Q", data, 0x00, int(loan_contract))
    return bytes(data)


def _move_fm24_owned_club_player_loan(
    *, pid: int, layout: Any,
    source_team_id: int, source_team_address: Any,
    source_squad_team_id: int = 0, source_squad_team_address: Any = 0,
    target_team_id: int, target_team_address: Any,
    player_id: int,
) -> dict[str, Any]:
    offsets = _required_fm24_layout(layout)
    with open_process(pid, write_memory=True) as process:
        module = layout.module(process)
        if not module:
            raise RuntimeError(f"{layout.module_name} 尚未加载")
        reader = _writable_player_movement_reader(process, module.base_address, layout)
        context = _resolve_player_movement_context(
            reader, module, layout,
            source_team_id=source_team_id,
            source_team_address=source_team_address,
            source_squad_team_id=source_squad_team_id,
            source_squad_team_address=source_squad_team_address,
            target_team_id=target_team_id,
            target_team_address=target_team_address,
            player_id=player_id,
        )
        source_team, target_team = context.source_team, context.target_team
        source_rows, target_rows = context.source_rows, context.target_rows
        player, person = context.player, context.person
        source_club = context.source_club
        primary_contract = int(reader.ptr(person + FM24_PERSON_CONTRACT) or 0)
        collection = int(reader.ptr(person + FM24_PERSON_CONTRACT_COLLECTION) or 0)
        registered_team = int(reader.ptr(player + PLAYER_REGISTERED_TEAM) or 0)
        contract_team = int(reader.ptr(primary_contract + 0x10) or 0) if primary_contract else 0
        main_vtable = module.base_address + offsets["player_move_main_contract_vtable_rva"]
        loan_vtable = module.base_address + int(layout.loan_contract_vtable_rva)
        if (
            not primary_contract or collection
            or reader.ptr(primary_contract) != main_vtable
            or reader.ptr(primary_contract + 0x08) != person
            or not _team_belongs_to_club(reader, contract_team, source_club)
            or reader.ptr(player + PLAYER_CURRENT_TEAM) != source_team
            or not _valid_registered_club_team(reader, registered_team)
        ):
            raise PlayerMovementContextChangedError(
                "FM24 球员当前球队、注册球队、主合同或租借状态已经变化"
            )
        if has_confirmed_future_transfer(reader, person):
            raise ValueError(
                "该球员存在已确认的未来转会或租借，请先在未来转会中叫停后再移动"
            )

        template_data = None
        template_contract = 0
        for row in target_rows:
            candidate = _address(row.get("address"))
            candidate_id = int(row.get("id") or 0)
            if not candidate or not candidate_id:
                continue
            try:
                candidate_person = _validated_player_person(reader, candidate, candidate_id)
            except Exception:
                continue
            other = int(reader.ptr(candidate_person + FM24_PERSON_CONTRACT_COLLECTION) or 0)
            candidate_loan = int(reader.ptr(other) or 0) if other else 0
            if (
                candidate_loan
                and reader.ptr(candidate_loan) == loan_vtable
                and reader.ptr(candidate_loan + 0x08) == candidate_person
                and reader.ptr(candidate_loan + 0x10) == target_team
            ):
                raw = reader.bytes(candidate_loan, FM24_LOAN_CONTRACT_SIZE)
                if len(raw) == FM24_LOAN_CONTRACT_SIZE:
                    template_contract, template_data = candidate_loan, raw
                    break
        if not template_data:
            raise RuntimeError("FM24 目标俱乐部没有可验证的原生租借合同模板")

        source_begin, source_end, source_capacity, source_pointers = _roster_vector(
            reader, source_team,
        )
        target_begin, target_end, target_capacity, target_pointers = _roster_vector(
            reader, target_team,
        )
        if source_pointers.count(player) != 1 or player in target_pointers:
            raise RuntimeError("FM24 来源或目标名单在租借准备期间发生变化")
        target_after_pointers = [*target_pointers, player]

        loan_pool = module.base_address + FM24_LOAN_CONTRACT_POOL_RVA
        pool_head = int(reader.ptr(loan_pool + 0x28) or 0)
        pool_available = int(reader.u32(loan_pool + 0x30) or 0)
        pool_in_use = int(reader.u32(loan_pool + 0x34) or 0)
        pool_item_size = int(reader.u32(loan_pool + 0x38) or 0)
        if (
            not pool_head or pool_available <= 0
            or pool_item_size != FM24_LOAN_CONTRACT_SIZE
            or pool_head == template_contract
        ):
            raise RuntimeError("FM24 租借合同池当前不能安全分配合同")
        next_free = int(reader.ptr(pool_head) or 0)
        free_slot = reader.bytes(pool_head, FM24_LOAN_CONTRACT_SIZE)
        if (
            len(free_slot) != FM24_LOAN_CONTRACT_SIZE
            or (pool_available > 1 and not next_free)
        ):
            raise RuntimeError("FM24 租借合同池空闲链校验失败")

        game_date_code = int(
            reader.u32(module.base_address + offsets["game_date_rva"]) or 0
        )
        current_date = decode_date(game_date_code)
        if not current_date:
            raise RuntimeError("FM24 当前游戏日期不可用")
        expiry_date = current_date + timedelta(days=365)
        expiry_code = _encode_date(expiry_date) | (game_date_code & 0xFE00)
        new_contract = _fm24_loan_contract_data(
            template_data, loan_vtable=loan_vtable, person=person,
            target_team=target_team, start_code=game_date_code,
            expiry_code=expiry_code,
        )
        original_primary_expiry = int(reader.u32(primary_contract + 0x40) or 0)
        decoded_primary_expiry = decode_date(original_primary_expiry)
        updated_primary_expiry = (
            expiry_code
            if not decoded_primary_expiry or decoded_primary_expiry < expiry_date
            else original_primary_expiry
        )

        snapshots: list[tuple[int, bytes]] = []
        seen: set[tuple[int, int]] = set()

        def snapshot(address: int, size: int) -> bytes:
            key = (int(address), int(size))
            raw = reader.bytes(address, size)
            if not raw or len(raw) != size:
                raise RuntimeError(f"FM24 租借快照读取失败：{hex(address)}")
            if key not in seen:
                snapshots.append((address, raw))
                seen.add(key)
            return raw

        def checked_write(address: int, raw: bytes) -> None:
            write_process_memory(process, address, raw)
            if read_process_memory(process, address, len(raw)) != raw:
                raise RuntimeError(f"FM24 租借写后回读失败：{hex(address)}")

        snapshot(loan_pool + 0x28, 0x18)
        snapshot(pool_head, FM24_LOAN_CONTRACT_SIZE)
        snapshot(source_team + TEAM_ROSTER_BEGIN, 24)
        snapshot(target_team + TEAM_ROSTER_BEGIN, 24)
        snapshot(source_begin, source_capacity - source_begin)
        snapshot(target_begin, target_capacity - target_begin)
        snapshot(person + FM24_PERSON_CONTRACT_COLLECTION, 8)
        snapshot(player + PLAYER_CURRENT_TEAM, 16)
        snapshot(primary_contract + 0x40, 4)

        thread_id, idle_context = _logic_thread(process)
        handle = _open_thread(thread_id)
        allocated_target_storage = 0
        other_contracts = 0
        suspended = False
        committed = False
        try:
            if not handle or kernel32.SuspendThread(handle) == 0xFFFFFFFF:
                raise RuntimeError("FM24 游戏逻辑线程当前无法暂停，请稍后重试")
            suspended = True
            current_context = _get_context(handle)
            if (
                int(current_context.Rip) != int(idle_context.Rip)
                or int(current_context.Rsp) != int(idle_context.Rsp)
            ):
                raise RuntimeError("FM24 游戏逻辑线程在租借提交前恢复工作，请稍后重试")
            for address, raw in snapshots:
                if read_process_memory(process, address, len(raw)) != raw:
                    raise RuntimeError("FM24 球员、合同池或俱乐部名单在租借提交前发生变化")

            target_size = len(target_after_pointers) * 8
            if target_end + 8 <= target_capacity:
                target_storage = target_begin
                target_storage_capacity = target_capacity
            else:
                allocated_target_storage = int(kernel32.VirtualAllocEx(
                    process.handle, None, max(0x1000, target_size),
                    MEM_COMMIT_RESERVE, PAGE_READWRITE,
                ) or 0)
                if not allocated_target_storage:
                    raise RuntimeError("FM24 目标俱乐部一线队名单扩展失败")
                target_storage = allocated_target_storage
                target_storage_capacity = target_storage + target_size

            other_contracts = int(kernel32.VirtualAllocEx(
                process.handle, None, FM24_OTHER_CONTRACTS_SIZE,
                MEM_COMMIT_RESERVE, PAGE_READWRITE,
            ) or 0)
            if not other_contracts:
                raise RuntimeError("FM24 附加合同容器分配失败")

            checked_write(loan_pool + 0x28, struct.pack("<Q", next_free))
            checked_write(loan_pool + 0x30, struct.pack("<I", pool_available - 1))
            checked_write(loan_pool + 0x34, struct.pack("<I", pool_in_use + 1))
            checked_write(pool_head, new_contract)
            checked_write(
                other_contracts, _fm24_other_contracts_data(pool_head),
            )

            target_raw = struct.pack(
                f"<{len(target_after_pointers)}Q", *target_after_pointers,
            )
            checked_write(target_storage, target_raw)
            checked_write(
                target_team + TEAM_ROSTER_BEGIN,
                struct.pack(
                    "<QQQ", target_storage, target_storage + target_size,
                    target_storage_capacity,
                ),
            )
            checked_write(
                person + FM24_PERSON_CONTRACT_COLLECTION,
                struct.pack("<Q", other_contracts),
            )
            checked_write(player + PLAYER_CURRENT_TEAM, struct.pack("<Q", target_team))
            checked_write(
                player + PLAYER_REGISTERED_TEAM,
                struct.pack("<Q", contract_team),
            )
            if updated_primary_expiry != original_primary_expiry:
                checked_write(
                    primary_contract + 0x40,
                    struct.pack("<I", updated_primary_expiry),
                )

            _invalidate_reader_after_write(reader)
            _, _, _, source_verified = _roster_vector(reader, source_team)
            _, _, _, target_verified = _roster_vector(reader, target_team)
            valid = (
                source_verified.count(player) == 1
                and target_verified.count(player) == 1
                and reader.ptr(player + PLAYER_CURRENT_TEAM) == target_team
                and int(reader.ptr(player + PLAYER_REGISTERED_TEAM) or 0)
                == contract_team
                and reader.ptr(person + FM24_PERSON_CONTRACT) == primary_contract
                and reader.ptr(primary_contract + 0x10) == contract_team
                and reader.ptr(person + FM24_PERSON_CONTRACT_COLLECTION)
                == other_contracts
                and reader.ptr(other_contracts) == pool_head
                and not reader.ptr(other_contracts + 0x08)
                and not reader.ptr(other_contracts + 0x10)
                and reader.ptr(pool_head) == loan_vtable
                and reader.ptr(pool_head + 0x08) == person
                and reader.ptr(pool_head + 0x10) == target_team
                and int(reader.u32(pool_head + 0x3C) or 0) == game_date_code
                and int(reader.u32(pool_head + 0x40) or 0) == expiry_code
                and reader.ptr(loan_pool + 0x28) == next_free
                and int(reader.u32(loan_pool + 0x30) or 0) == pool_available - 1
                and int(reader.u32(loan_pool + 0x34) or 0) == pool_in_use + 1
            )
            if not valid:
                raise RuntimeError("FM24 租借写后结构校验失败")
            committed = True
        except Exception:
            rollback_failed = False
            for address, raw in reversed(snapshots):
                try:
                    write_process_memory(process, address, raw)
                except Exception:
                    rollback_failed = True
            _invalidate_reader_after_write(reader)
            for address, raw in snapshots:
                if read_process_memory(process, address, len(raw)) != raw:
                    rollback_failed = True
            for allocated in (other_contracts, allocated_target_storage):
                if allocated and not kernel32.VirtualFreeEx(
                    process.handle, ctypes.c_void_p(allocated), 0, MEM_RELEASE,
                ):
                    rollback_failed = True
            if rollback_failed:
                raise RuntimeError(
                    "FM24 租借失败且回滚不完整，请不要保存当前存档"
                )
            raise
        finally:
            if suspended:
                kernel32.ResumeThread(handle)
            if handle:
                kernel32.CloseHandle(handle)

        return {
            "player_id": int(player_id), "mode": "loan",
            "source_team_id": int(source_team_id),
            "target_team_id": int(target_team_id),
            "registered_team_before": hex(registered_team) if registered_team else None,
            "registered_team_reset_to_contract_team": True,
            "contract_address": hex(pool_head),
            "other_contracts_address": hex(other_contracts),
            "template_contract_address": hex(template_contract),
            "thread_id": int(thread_id), "verified": bool(committed),
            "compatibility_mode": "fm24_verified_loan_pool",
            "warning": "FM24 租借已完成，请核对球员页面并刷新后再保存存档。",
        }


def _sale_credit(reader: Reader, source_club: int, amount: int) -> tuple[int, bytes, bytes, int] | None:
    """Credit the seller's remaining transfer budget in the native transfer transaction."""
    if not amount:
        return None
    if isinstance(amount, bool) or not isinstance(amount, int) or not 0 < amount <= 2_000_000_000:
        raise ValueError("球员出售金额无效")
    layout = reader.layout
    club_offset = getattr(layout, "club_finance_offset", None)
    budget_offset = getattr(layout, "finance_remaining_transfer_budget_offset", None)
    if club_offset is None or budget_offset is None:
        raise RuntimeError("当前版本不支持球员出售转会预算入账")
    finance = int(reader.ptr(source_club + int(club_offset)) or 0)
    if not finance or int(reader.ptr(finance + 8) or 0) != source_club:
        raise RuntimeError("卖方俱乐部财政反向引用校验失败")
    address = finance + int(budget_offset)
    before = reader.bytes(address, 4)
    if not before or len(before) != 4:
        raise RuntimeError("卖方俱乐部转会预算读取失败")
    balance = struct.unpack("<i", before)[0] + amount
    if not -2_147_483_648 <= balance <= 2_147_483_647:
        raise ValueError("出售后俱乐部转会预算超出支持范围")
    return address, before, struct.pack("<i", balance), balance


def _move_fm24_owned_club_player(
    *, pid: int, layout: Any,
    source_team_id: int, source_team_address: Any,
    source_squad_team_id: int = 0, source_squad_team_address: Any = 0,
    target_team_id: int, target_team_address: Any,
    player_id: int, mode: str, transfer_fee: int = 0,
    commit_transfer: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    offsets = _required_fm24_layout(layout)
    with open_process(pid, write_memory=True) as process:
        module = layout.module(process)
        if not module:
            raise RuntimeError(f"{layout.module_name} 尚未加载")
        reader = _writable_player_movement_reader(process, module.base_address, layout)
        context = _resolve_player_movement_context(
            reader, module, layout,
            source_team_id=source_team_id,
            source_team_address=source_team_address,
            source_squad_team_id=source_squad_team_id,
            source_squad_team_address=source_squad_team_address,
            target_team_id=target_team_id,
            target_team_address=target_team_address,
            player_id=player_id,
        )
        source_team, target_team = context.source_team, context.target_team
        source_rows, target_rows = context.source_rows, context.target_rows
        player, person = context.player, context.person
        source_club = context.source_club
        primary_contract = int(reader.ptr(person + FM24_PERSON_CONTRACT) or 0)
        main_vtable = module.base_address + offsets["player_move_main_contract_vtable_rva"]
        if (
            not primary_contract
            or reader.ptr(person + FM24_PERSON_CONTRACT_COLLECTION)
            or reader.ptr(primary_contract) != main_vtable
            or reader.ptr(primary_contract + 0x08) != person
            or not _team_belongs_to_club(
                reader, int(reader.ptr(primary_contract + 0x10) or 0), source_club,
            )
            or reader.ptr(player + PLAYER_CURRENT_TEAM) != source_team
            or (
                int(reader.ptr(player + PLAYER_REGISTERED_TEAM) or 0)
                and not _team_belongs_to_club(
                    reader,
                    int(reader.ptr(player + PLAYER_REGISTERED_TEAM) or 0),
                    source_club,
                )
            )
        ):
            raise ValueError("球员当前球队、合同或租借状态已经变化，请刷新后重试")

        template_contract = template_data = None
        for row in target_rows:
            candidate = _address(row.get("address"))
            candidate_id = int(row.get("id") or 0)
            if not candidate or not candidate_id:
                continue
            try:
                candidate_person = _validated_player_person(reader, candidate, candidate_id)
            except Exception:
                continue
            contract = int(reader.ptr(candidate_person + FM24_PERSON_CONTRACT) or 0)
            if (
                contract and reader.ptr(contract) == main_vtable
                and reader.ptr(contract + 0x08) == candidate_person
                and reader.ptr(contract + 0x10) == target_team
            ):
                raw = reader.bytes(contract, FM24_CONTRACT_SIZE)
                if len(raw) == FM24_CONTRACT_SIZE:
                    template_contract, template_data = contract, raw
                    break
        if not template_contract or not template_data:
            raise RuntimeError("目标俱乐部没有可用的主合同模板")

        source_begin, source_end, source_capacity, source_pointers = _roster_vector(
            reader, source_team,
        )
        target_begin, target_end, target_capacity, target_pointers = _roster_vector(
            reader, target_team,
        )
        source_after_pointers = _remove_unique_pointer(source_pointers, player)
        if player in target_pointers:
            raise RuntimeError("目标俱乐部名单已经包含该球员地址")
        target_after_pointers = [*target_pointers, player]

        pool = module.base_address + offsets["player_move_contract_pool_rva"]
        pool_head = int(reader.ptr(pool + 0x28) or 0)
        pool_available = int(reader.u32(pool + 0x30) or 0)
        pool_in_use = int(reader.u32(pool + 0x34) or 0)
        if (
            not pool_head or pool_available <= 0
            or int(reader.u32(pool + 0x38) or 0) != FM24_CONTRACT_SIZE
        ):
            raise RuntimeError("FM24 主合同池当前不可用")
        next_free = int(reader.ptr(pool_head) or 0)
        free_slot = reader.bytes(pool_head, FM24_CONTRACT_SIZE)
        old_contract = reader.bytes(primary_contract, FM24_CONTRACT_SIZE)
        if (
            (pool_available > 1 and not next_free)
            or len(free_slot) != FM24_CONTRACT_SIZE
            or len(old_contract) != FM24_CONTRACT_SIZE
            or pool_head in {primary_contract, template_contract}
        ):
            raise RuntimeError("FM24 主合同池空闲链校验失败")

        game_date_code = int(reader.u32(module.base_address + offsets["game_date_rva"]) or 0)
        current_date = decode_date(game_date_code)
        if not current_date:
            raise RuntimeError("无法读取当前游戏日期")
        template_expiry = decode_date(struct.unpack_from("<I", template_data, 0x40)[0])
        expiry = (
            template_expiry
            if template_expiry and template_expiry > current_date + timedelta(days=30)
            else current_date.replace(year=current_date.year + 3)
        )
        new_contract = bytearray(template_data)
        struct.pack_into("<Q", new_contract, 0x08, person)
        struct.pack_into("<Q", new_contract, 0x10, target_team)
        struct.pack_into("<I", new_contract, 0x3C, game_date_code)
        struct.pack_into(
            "<I", new_contract, 0x40,
            _encode_date(expiry) | (game_date_code & 0xFE00),
        )
        struct.pack_into("<I", new_contract, 0x44, game_date_code)

        snapshots: list[tuple[int, bytes]] = []
        seen: set[tuple[int, int]] = set()

        def snapshot(address: int, size: int) -> bytes:
            key = (int(address), int(size))
            raw = reader.bytes(address, size)
            if not raw or len(raw) != size:
                raise RuntimeError(f"FM24 事务快照读取失败：{hex(address)}")
            if key not in seen:
                snapshots.append((address, raw))
                seen.add(key)
            return raw

        def checked_write(address: int, raw: bytes) -> None:
            write_process_memory(process, address, raw)
            if read_process_memory(process, address, len(raw)) != raw:
                raise RuntimeError(f"FM24 事务写后回读失败：{hex(address)}")

        snapshot(pool + 0x28, 0x18)
        snapshot(pool_head, FM24_CONTRACT_SIZE)
        snapshot(primary_contract, FM24_CONTRACT_SIZE)
        snapshot(source_team + TEAM_ROSTER_BEGIN, 24)
        snapshot(target_team + TEAM_ROSTER_BEGIN, 24)
        snapshot(source_begin, source_capacity - source_begin)
        snapshot(target_begin, target_capacity - target_begin)
        snapshot(person + FM24_PERSON_CONTRACT, 8)
        snapshot(person + FM24_JOINED_CLUB_DATE, 4)
        snapshot(player + PLAYER_CURRENT_TEAM, 16)

        sale_credit = _sale_credit(reader, source_club, transfer_fee)
        if sale_credit:
            finance = int(reader.ptr(source_club + int(layout.club_finance_offset)) or 0)
            snapshot(source_club + int(layout.club_finance_offset), 8)
            snapshot(finance + 8, 8)
            snapshot(sale_credit[0], 4)

        thread_id, idle_context = _logic_thread(process)
        handle = _open_thread(thread_id)
        allocated_target_storage = 0
        suspended = False
        committed = False
        try:
            if not handle or kernel32.SuspendThread(handle) == 0xFFFFFFFF:
                raise RuntimeError(f"{game_label} 游戏逻辑线程当前无法暂停，请重试")
            suspended = True
            current_context = _get_context(handle)
            if (
                int(current_context.Rip) != int(idle_context.Rip)
                or int(current_context.Rsp) != int(idle_context.Rsp)
            ):
                raise RuntimeError(f"{game_label} 游戏逻辑线程刚刚恢复工作，请稍后重试")
            for address, raw in snapshots:
                if read_process_memory(process, address, len(raw)) != raw:
                    raise RuntimeError("FM24 球员或俱乐部数据在执行前已经变化，请刷新后重试")

            target_size = len(target_after_pointers) * 8
            if target_end + 8 <= target_capacity:
                target_storage = target_begin
                target_storage_capacity = target_capacity
            else:
                allocated_target_storage = int(kernel32.VirtualAllocEx(
                    process.handle, None, max(0x1000, target_size),
                    MEM_COMMIT_RESERVE, PAGE_READWRITE,
                ) or 0)
                if not allocated_target_storage:
                    raise RuntimeError("无法扩展目标俱乐部一线队名单")
                target_storage = allocated_target_storage
                target_storage_capacity = target_storage + target_size

            checked_write(pool + 0x28, struct.pack("<Q", next_free))
            checked_write(pool + 0x30, struct.pack("<I", pool_available - 1))
            checked_write(pool + 0x34, struct.pack("<I", pool_in_use + 1))
            checked_write(pool_head, bytes(new_contract))

            target_raw = struct.pack(f"<{len(target_after_pointers)}Q", *target_after_pointers)
            checked_write(target_storage, target_raw)
            checked_write(
                target_team + TEAM_ROSTER_BEGIN,
                struct.pack(
                    "<QQQ", target_storage, target_storage + target_size,
                    target_storage_capacity,
                ),
            )
            source_raw = struct.pack(
                f"<{len(source_after_pointers)}Q", *source_after_pointers,
            ) if source_after_pointers else b""
            checked_write(source_begin, source_raw + b"\0" * 8)
            checked_write(source_team + TEAM_ROSTER_END, struct.pack("<Q", source_end - 8))

            checked_write(person + FM24_PERSON_CONTRACT, struct.pack("<Q", pool_head))
            checked_write(person + FM24_JOINED_CLUB_DATE, struct.pack("<I", game_date_code))
            checked_write(
                player + PLAYER_CURRENT_TEAM, struct.pack("<QQ", target_team, target_team),
            )

            checked_write(primary_contract, struct.pack("<Q", next_free))
            checked_write(pool + 0x28, struct.pack("<Q", primary_contract))
            checked_write(pool + 0x30, struct.pack("<I", pool_available))
            checked_write(pool + 0x34, struct.pack("<I", pool_in_use))

            _invalidate_reader_after_write(reader)
            _, _, _, source_verified = _roster_vector(reader, source_team)
            _, _, _, target_verified = _roster_vector(reader, target_team)
            valid = (
                player not in source_verified and target_verified.count(player) == 1
                and reader.ptr(player + PLAYER_CURRENT_TEAM) == target_team
                and reader.ptr(player + PLAYER_REGISTERED_TEAM) == target_team
                and reader.ptr(person + FM24_PERSON_CONTRACT) == pool_head
                and reader.ptr(pool_head) == main_vtable
                and reader.ptr(pool_head + 0x08) == person
                and reader.ptr(pool_head + 0x10) == target_team
                and reader.ptr(pool + 0x28) == primary_contract
                and int(reader.u32(pool + 0x30) or 0) == pool_available
                and int(reader.u32(pool + 0x34) or 0) == pool_in_use
            )
            if not valid:
                raise RuntimeError("FM24 兼容转移写后校验失败")
            if sale_credit:
                checked_write(sale_credit[0], sale_credit[2])
            if commit_transfer is not None:
                commit_transfer({
                    "player_id": int(player_id), "source_team_id": int(source_team_id),
                    "target_team_id": int(target_team_id), "transfer_fee": transfer_fee,
                    "seller_transfer_budget_after": sale_credit[3] if sale_credit else None,
                })
            committed = True
        except Exception:
            rollback_failed = False
            for address, raw in reversed(snapshots):
                try:
                    write_process_memory(process, address, raw)
                except Exception:
                    rollback_failed = True
            _invalidate_reader_after_write(reader)
            for address, raw in snapshots:
                if read_process_memory(process, address, len(raw)) != raw:
                    rollback_failed = True
            if allocated_target_storage:
                if not kernel32.VirtualFreeEx(
                    process.handle, ctypes.c_void_p(allocated_target_storage), 0, MEM_RELEASE,
                ):
                    rollback_failed = True
            if rollback_failed:
                error = RuntimeError("FM24 球员转移失败且回滚未完整，请不要保存当前存档")
                error.rollback_incomplete = True
                raise error
            raise
        finally:
            if suspended:
                kernel32.ResumeThread(handle)
            if handle:
                kernel32.CloseHandle(handle)

        warning = (
            "FM24 当前租借按兼容转移执行：球员会离开来源名单并改签目标俱乐部；"
            "母队合同与租借记录将在后续完善。"
            if mode == "loan" else
            "FM24 转会已完成；当前赛季旧场次可能暂时显示在新俱乐部名下。"
        )
        return {
            "player_id": int(player_id), "mode": mode,
            "source_team_id": int(source_team_id),
            "target_team_id": int(target_team_id),
            "contract_address": hex(pool_head),
            "template_contract_address": hex(int(template_contract)),
            "thread_id": int(thread_id), "verified": bool(committed),
            "compatibility_mode": "fm24_permanent_move",
            "warning": warning,
        }


def _release_owned_club_player_from_pool(
    *, pid: int, layout: Any,
    source_team_id: int, source_team_address: Any,
    source_squad_team_id: int = 0, source_squad_team_address: Any = 0,
    player_id: int,
) -> dict[str, Any]:
    offsets = _required_staff_release_layout(layout)
    person_contract_offset = offsets["person_contract_offset"]
    collection_offset = offsets["person_contract_collection_offset"]
    contract_size = offsets["contract_size"]
    game_label = "FM24" if layout.key == "fm24" else "FM26"
    with open_process(pid, write_memory=True) as process:
        module = layout.module(process)
        if not module:
            raise RuntimeError(f"{layout.module_name} 尚未加载")
        reader = _writable_player_movement_reader(process, module.base_address, layout)
        source_team, source_club = _resolve_owned_source_team(
            reader, module, layout,
            source_team_id=source_team_id,
            source_team_address=source_team_address,
            source_squad_team_id=source_squad_team_id,
            source_squad_team_address=source_squad_team_address,
        )

        source_players = {
            int(row.get("id") or 0): _address(row.get("address"))
            for row in reader.roster(source_team)
        }
        player = int(source_players.get(int(player_id)) or 0)
        if not player:
            raise ValueError("该球员已经不在来源俱乐部一线队")
        person = _validated_player_person(reader, player, int(player_id))
        primary_contract = int(reader.ptr(person + person_contract_offset) or 0)
        main_vtable = module.base_address + offsets["player_move_main_contract_vtable_rva"]
        if (
            not primary_contract
            or reader.ptr(person + collection_offset)
            or reader.ptr(primary_contract) != main_vtable
            or reader.ptr(primary_contract + 0x08) != person
            or not _team_belongs_to_club(
                reader, int(reader.ptr(primary_contract + 0x10) or 0), source_club,
            )
            or reader.ptr(player + PLAYER_CURRENT_TEAM) != source_team
            or (
                int(reader.ptr(player + PLAYER_REGISTERED_TEAM) or 0)
                and not _team_belongs_to_club(
                    reader,
                    int(reader.ptr(player + PLAYER_REGISTERED_TEAM) or 0),
                    source_club,
                )
            )
        ):
            raise ValueError(
                "球员当前球队、主合同或租借状态已经变化，请刷新后重试"
            )

        source_begin, source_end, source_capacity, source_pointers = _roster_vector(
            reader, source_team,
        )
        source_after_pointers = _remove_unique_pointer(source_pointers, player)
        pool = module.base_address + offsets["player_move_contract_pool_rva"]
        pool_head = int(reader.ptr(pool + 0x28) or 0)
        pool_available = int(reader.u32(pool + 0x30) or 0)
        pool_in_use = int(reader.u32(pool + 0x34) or 0)
        pool_item_size = int(reader.u32(pool + 0x38) or 0)
        if (
            pool_item_size != contract_size
            or pool_in_use <= 0
            or pool_available >= 0x7FFFFFFF
            or (pool_available > 0 and not pool_head)
            or primary_contract == pool_head
        ):
            raise RuntimeError(f"{game_label} 主合同池当前不可安全回收合同")

        snapshots: list[tuple[int, bytes]] = []
        seen: set[tuple[int, int]] = set()

        def snapshot(address: int, size: int) -> bytes:
            key = (int(address), int(size))
            raw = reader.bytes(address, size)
            if not raw or len(raw) != size:
                raise RuntimeError(f"{game_label} 解约事务快照读取失败：{hex(address)}")
            if key not in seen:
                snapshots.append((address, raw))
                seen.add(key)
            return raw

        def checked_write(address: int, raw: bytes) -> None:
            write_process_memory(process, address, raw)
            if read_process_memory(process, address, len(raw)) != raw:
                raise RuntimeError(f"{game_label} 解约事务写后回读失败：{hex(address)}")

        snapshot(pool + 0x28, 0x18)
        snapshot(primary_contract, contract_size)
        snapshot(source_team + TEAM_ROSTER_BEGIN, 24)
        snapshot(source_begin, source_capacity - source_begin)
        snapshot(person + person_contract_offset, 8)
        snapshot(player + PLAYER_CURRENT_TEAM, 16)

        thread_id, idle_context = _logic_thread(process)
        handle = _open_thread(thread_id)
        suspended = False
        committed = False
        try:
            if not handle or kernel32.SuspendThread(handle) == 0xFFFFFFFF:
                raise RuntimeError(f"{game_label} 游戏逻辑线程当前无法暂停，请重试")
            suspended = True
            current_context = _get_context(handle)
            if (
                int(current_context.Rip) != int(idle_context.Rip)
                or int(current_context.Rsp) != int(idle_context.Rsp)
            ):
                raise RuntimeError(f"{game_label} 游戏逻辑线程刚刚恢复工作，请稍后重试")
            for address, raw in snapshots:
                if read_process_memory(process, address, len(raw)) != raw:
                    raise RuntimeError(
                        f"{game_label} 球员或俱乐部数据在执行前已经变化，请刷新后重试"
                    )

            source_raw = (
                struct.pack(f"<{len(source_after_pointers)}Q", *source_after_pointers)
                if source_after_pointers else b""
            )
            checked_write(source_begin, source_raw + b"\0" * 8)
            checked_write(
                source_team + TEAM_ROSTER_END, struct.pack("<Q", source_end - 8),
            )
            checked_write(person + person_contract_offset, b"\0" * 8)
            checked_write(player + PLAYER_CURRENT_TEAM, b"\0" * 16)

            checked_write(primary_contract, struct.pack("<Q", pool_head))
            checked_write(pool + 0x28, struct.pack("<Q", primary_contract))
            checked_write(pool + 0x30, struct.pack("<I", pool_available + 1))
            checked_write(pool + 0x34, struct.pack("<I", pool_in_use - 1))

            _, _, _, source_verified = _roster_vector(reader, source_team)
            valid = (
                player not in source_verified
                and not reader.ptr(player + PLAYER_CURRENT_TEAM)
                and not reader.ptr(player + PLAYER_REGISTERED_TEAM)
                and not reader.ptr(person + person_contract_offset)
                and not reader.ptr(person + collection_offset)
                and _pointer_matches(reader, primary_contract, pool_head)
                and reader.ptr(pool + 0x28) == primary_contract
                and int(reader.u32(pool + 0x30) or 0) == pool_available + 1
                and int(reader.u32(pool + 0x34) or 0) == pool_in_use - 1
            )
            if not valid:
                raise RuntimeError(f"{game_label} 球员解约写后校验失败")
            committed = True
        except Exception:
            rollback_failed = False
            for address, raw in reversed(snapshots):
                try:
                    write_process_memory(process, address, raw)
                except Exception:
                    rollback_failed = True
            _invalidate_reader_after_write(reader)
            for address, raw in snapshots:
                if read_process_memory(process, address, len(raw)) != raw:
                    rollback_failed = True
            if rollback_failed:
                raise RuntimeError(
                    f"{game_label} 球员解约失败且回滚未完整，请不要保存当前存档"
                )
            raise
        finally:
            if suspended:
                kernel32.ResumeThread(handle)
            if handle:
                kernel32.CloseHandle(handle)

        return {
            "player_id": int(player_id),
            "mode": "release",
            "source_team_id": int(source_team_id),
            "released_contract_address": hex(primary_contract),
            "thread_id": int(thread_id),
            "verified": bool(committed),
            "compatibility_mode": f"{layout.key}_free_agent",
            "warning": "球员已与当前俱乐部解约并恢复自由身。",
        }


def _release_fm24_owned_club_player(**kwargs: Any) -> dict[str, Any]:
    return _release_owned_club_player_from_pool(**kwargs)


def _release_fm26_owned_club_player(**kwargs: Any) -> dict[str, Any]:
    return _release_owned_club_player_from_pool(**kwargs)


def release_owned_club_player(
    *, source_team_id: int, source_team_address: Any,
    source_squad_team_id: int = 0, source_squad_team_address: Any = 0,
    player_id: int,
) -> dict[str, Any]:
    if int(source_team_id) <= 0 or int(player_id) <= 0:
        raise ValueError("来源俱乐部或球员 ID 无效")
    _configure_thread_api()
    pid, _path, layout = select_process_layout()
    release = {
        "fm24": _release_fm24_owned_club_player,
        "fm26": _release_fm26_owned_club_player,
    }.get(layout.key)
    if release is None:
        raise RuntimeError(
            "球员解约目前仅支持已验证的 FM24 Steam 24.4.2 与 FM26 Steam 26.3.2"
        )
    squad_kwargs: dict[str, Any] = {}
    if int(source_squad_team_id or 0):
        squad_kwargs = {
            "source_squad_team_id": int(source_squad_team_id),
            "source_squad_team_address": source_squad_team_address,
        }
    return release(
        pid=pid, layout=layout,
        source_team_id=source_team_id,
        source_team_address=source_team_address,
        **squad_kwargs,
        player_id=player_id,
    )


def _staff_release_candidate(
    reader: Reader, team: int, staff_id: int, *,
    person_contract_offset: int,
) -> dict[str, Any] | None:
    """Resolve a staff member using only identity and contract fields.

    The normal staff scan also parses optional display fields (abilities,
    localized name, reputation). Those fields may be incomplete for some
    native Person objects, but a failed display parse must not make a valid
    head coach impossible to dismiss.
    """
    candidates: dict[int, tuple[int, int]] = {}

    # The manager slot is authoritative for the head coach and avoids relying
    # on the optional staff-display parser altogether.
    manager_base = int(reader.ptr(int(team) + TEAM_MANAGER) or 0)
    manager_offset = _team_manager_person_offset(reader.layout)
    if manager_base:
        manager_person = manager_base + manager_offset
        if int(reader.u32(manager_person + ENTITY_UID) or 0) == int(staff_id):
            candidates[manager_person] = (manager_person, 0)

    # Fall back to the bounded/global Person index for every other staff role,
    # and for builds where the manager slot is temporarily unavailable.
    for person, captured_contract in _staff_scan_entries(reader, 0):
        person = int(person)
        if int(reader.u32(person + ENTITY_UID) or 0) != int(staff_id):
            continue
        candidates.setdefault(person, (person, int(captured_contract or 0)))

    matches: list[dict[str, Any]] = []
    for person, captured_contract in candidates.values():
        contract = int(reader.ptr(person + int(person_contract_offset)) or 0)
        if not contract:
            contract = int(captured_contract or 0)
        if (
            not contract
            or reader.ptr(contract + 0x08) != person
            or reader.ptr(contract + 0x10) != int(team)
        ):
            continue
        job_type = (
            reader.u8(contract + int(reader.layout.staff_job_type_offset))
            if reader.layout.staff_job_type_offset is not None else None
        )
        matches.append({
            "id": int(staff_id),
            "name": _name(reader, person) or str(staff_id),
            "role": "主教练" if int(job_type or 0) == 16 else "职员",
            "job_type": int(job_type) if job_type is not None else None,
            "address": hex(person),
            "contract_address": hex(contract),
        })
    return matches[0] if len(matches) == 1 else None


def _release_owned_club_staff_from_pool(
    *, pid: int, layout: Any,
    source_team_id: int, source_team_address: Any,
    staff_id: int,
) -> dict[str, Any]:
    offsets = _required_staff_release_layout(layout)
    person_contract_offset = offsets["person_contract_offset"]
    contract_size = offsets["contract_size"]
    game_label = "FM24" if layout.key == "fm24" else "FM26"
    with open_process(pid, write_memory=True) as process:
        module = layout.module(process)
        if not module:
            raise RuntimeError(f"{layout.module_name} 尚未加载")
        reader = _writable_player_movement_reader(process, module.base_address, layout)
        source_team = _resolve_team_address(
            reader, int(source_team_id), source_team_address,
        )
        source_club = int(reader.ptr(source_team + TEAM_CLUB) or 0)
        expected_club_vtable = module.base_address + int(layout.club_vtable_rva)
        if (
            not source_club
            or reader.ptr(source_club) != expected_club_vtable
            or int(reader.u32(source_club + ENTITY_UID) or 0) != int(source_team_id)
        ):
            raise RuntimeError("来源俱乐部原生对象校验失败")

        matches = [
            row for row in _scan_staff(reader, source_team, 0)
            if int(row.get("id") or 0) == int(staff_id)
        ]
        # A display scan may omit a valid Person when optional staff fields are
        # unreadable. Resolve the write target from stable identity and the
        # verified main-contract Team edge before declaring it absent.
        if len(matches) != 1:
            fallback = _staff_release_candidate(
                reader, source_team, int(staff_id),
                person_contract_offset=person_contract_offset,
            )
            matches = [fallback] if fallback else []
        if len(matches) != 1:
            raise ValueError("该职员已不在来源俱乐部，或职员对象无法唯一定位")
        staff = matches[0]
        job_type = int(staff.get("job_type") or 0)
        if is_club_controller_job_type(job_type):
            raise ValueError("俱乐部控制人不能通过职员解雇功能移除")
        person = _address(staff.get("address"))
        listed_contract = _address(staff.get("contract_address"))
        primary_contract = _validated_staff_main_contract(
            reader, module, layout,
            person=person, staff_id=int(staff_id), team=source_team,
            person_contract_offset=person_contract_offset,
            listed_contract=listed_contract, expected_job_type=job_type,
        )

        # The manager slot is authoritative.  Some saves use a non-standard
        # contract job code for an employed head coach, so checking only
        # ``job_type == 16`` would leave TEAM_MANAGER pointing at a dismissed
        # Person (or reject the coach during the display scan).
        manager_reference = source_team + TEAM_MANAGER
        manager_base = person - _team_manager_person_offset(layout)
        manager_slot = int(reader.ptr(manager_reference) or 0)
        if manager_slot == manager_base:
            pass
        elif job_type == 16:
            # A conventional head-coach contract that no longer owns the
            # slot indicates a concurrent game-state change.
            raise RuntimeError("球队主教练槽位已经变化，请刷新俱乐部资料后重试")
        else:
            manager_reference = 0
            manager_base = 0

        pool = module.base_address + offsets["player_move_contract_pool_rva"]
        pool_head = int(reader.ptr(pool + 0x28) or 0)
        pool_available = int(reader.u32(pool + 0x30) or 0)
        pool_in_use = int(reader.u32(pool + 0x34) or 0)
        pool_item_size = int(reader.u32(pool + 0x38) or 0)
        if (
            pool_item_size != contract_size
            or pool_in_use <= 0
            or pool_available >= 0x7FFFFFFF
            or (pool_available > 0 and not pool_head)
            or primary_contract == pool_head
        ):
            raise RuntimeError(f"{game_label} 主合同池当前不可安全回收职员合同")

        snapshots: list[tuple[int, bytes]] = []
        seen: set[tuple[int, int]] = set()

        def snapshot(address: int, size: int) -> bytes:
            key = (int(address), int(size))
            raw = reader.bytes(address, size)
            if len(raw) != size:
                raise RuntimeError(
                    f"{game_label} 职员解雇事务快照读取失败：{hex(address)}"
                )
            if key not in seen:
                snapshots.append((address, raw))
                seen.add(key)
            return raw

        def checked_write(address: int, raw: bytes) -> None:
            write_process_memory(process, address, raw)
            if read_process_memory(process, address, len(raw)) != raw:
                raise RuntimeError(
                    f"{game_label} 职员解雇事务写后回读失败：{hex(address)}"
                )

        snapshot(pool + 0x28, 0x18)
        snapshot(primary_contract, contract_size)
        snapshot(person + person_contract_offset, 8)
        if manager_reference:
            snapshot(manager_reference, 8)

        thread_id, idle_context = _logic_thread(process)
        handle = _open_thread(thread_id)
        suspended = False
        committed = False
        try:
            if not handle or kernel32.SuspendThread(handle) == 0xFFFFFFFF:
                raise RuntimeError("FM24 游戏逻辑线程当前无法暂停，请重试")
            suspended = True
            current_context = _get_context(handle)
            if (
                int(current_context.Rip) != int(idle_context.Rip)
                or int(current_context.Rsp) != int(idle_context.Rsp)
            ):
                raise RuntimeError(f"{game_label} 游戏逻辑线程刚刚恢复工作，请稍后重试")
            for address, raw in snapshots:
                if read_process_memory(process, address, len(raw)) != raw:
                    raise RuntimeError(
                        f"{game_label} 职员或合同池数据在执行前已经变化，请刷新后重试"
                    )

            checked_write(person + person_contract_offset, b"\0" * 8)
            if manager_reference:
                checked_write(manager_reference, b"\0" * 8)
            checked_write(primary_contract, struct.pack("<Q", pool_head))
            checked_write(pool + 0x28, struct.pack("<Q", primary_contract))
            checked_write(pool + 0x30, struct.pack("<I", pool_available + 1))
            checked_write(pool + 0x34, struct.pack("<I", pool_in_use - 1))

            valid = (
                not reader.ptr(person + person_contract_offset)
                and _pointer_matches(reader, primary_contract, pool_head)
                and reader.ptr(pool + 0x28) == primary_contract
                and int(reader.u32(pool + 0x30) or 0) == pool_available + 1
                and int(reader.u32(pool + 0x34) or 0) == pool_in_use - 1
                and (not manager_reference or not reader.ptr(manager_reference))
            )
            if not valid:
                raise RuntimeError(f"{game_label} 职员解雇写后校验失败")
            committed = True
        except Exception:
            rollback_failed = False
            for address, raw in reversed(snapshots):
                try:
                    write_process_memory(process, address, raw)
                except Exception:
                    rollback_failed = True
            _invalidate_reader_after_write(reader)
            for address, raw in snapshots:
                if read_process_memory(process, address, len(raw)) != raw:
                    rollback_failed = True
            if rollback_failed:
                raise RuntimeError(
                    f"{game_label} 职员解雇失败且回滚未完整，请不要保存当前存档"
                )
            raise
        finally:
            if suspended:
                kernel32.ResumeThread(handle)
            if handle:
                kernel32.CloseHandle(handle)

        return {
            "staff_id": int(staff_id),
            "staff_name": str(staff.get("name") or staff_id),
            "role": str(staff.get("role") or "职员"),
            "mode": "staff_release",
            "source_team_id": int(source_team_id),
            "released_contract_address": hex(primary_contract),
            "thread_id": int(thread_id),
            "verified": bool(committed),
            "compatibility_mode": f"{layout.key}_staff_free_agent",
            "warning": f"{staff.get('name') or '所选职员'} 已与俱乐部解除合同",
        }


def _release_fm24_owned_club_staff(**kwargs: Any) -> dict[str, Any]:
    return _release_owned_club_staff_from_pool(**kwargs)


def _release_fm26_owned_club_staff(**kwargs: Any) -> dict[str, Any]:
    return _release_owned_club_staff_from_pool(**kwargs)


def release_owned_club_staff(
    *, source_team_id: int, source_team_address: Any, staff_id: int,
) -> dict[str, Any]:
    if int(source_team_id) <= 0 or int(staff_id) <= 0:
        raise ValueError("来源俱乐部或职员 ID 无效")
    _configure_thread_api()
    pid, _path, layout = select_process_layout()
    release = {
        "fm24": _release_fm24_owned_club_staff,
        "fm26": _release_fm26_owned_club_staff,
    }.get(layout.key)
    if release is None:
        raise RuntimeError(
            "职员解雇目前仅支持已验证的 FM24 Steam 24.4.2 与 FM26 Steam 26.3.2"
        )
    return release(
        pid=pid, layout=layout,
        source_team_id=int(source_team_id),
        source_team_address=source_team_address,
        staff_id=int(staff_id),
    )


def set_owned_club_player_transfer_listed(
    *, source_team_id: int, source_team_address: Any,
    source_squad_team_id: int = 0, source_squad_team_address: Any = 0,
    player_id: int, listed: bool,
) -> dict[str, Any]:
    if int(source_team_id) <= 0 or int(player_id) <= 0:
        raise ValueError("来源俱乐部或球员 ID 无效")
    pid, _path, layout = select_process_layout()
    offsets = _required_staff_release_layout(layout)
    person_contract_offset = offsets["person_contract_offset"]
    collection_offset = offsets["person_contract_collection_offset"]
    transfer_status_offset = 0x4F if layout.key == "fm24" else 0x57
    with open_process(pid, write_memory=True) as process:
        module = layout.module(process)
        if not module:
            raise RuntimeError(f"{layout.module_name} 尚未加载")
        reader = _writable_player_movement_reader(process, module.base_address, layout)
        source_team, source_club = _resolve_owned_source_team(
            reader, module, layout,
            source_team_id=source_team_id,
            source_team_address=source_team_address,
            source_squad_team_id=source_squad_team_id,
            source_squad_team_address=source_squad_team_address,
        )
        source_players = {
            int(row.get("id") or 0): _address(row.get("address"))
            for row in reader.roster(source_team)
        }
        player = int(source_players.get(int(player_id)) or 0)
        if not player:
            raise ValueError("该球员已经不在来源俱乐部一线队")
        person = _validated_player_person(reader, player, int(player_id))
        primary_contract = int(reader.ptr(person + person_contract_offset) or 0)
        main_vtable = (
            module.base_address + offsets["player_move_main_contract_vtable_rva"]
        )
        if (
            not primary_contract
            or reader.ptr(person + collection_offset)
            or reader.ptr(primary_contract) != main_vtable
            or reader.ptr(primary_contract + 0x08) != person
            or not _team_belongs_to_club(
                reader, int(reader.ptr(primary_contract + 0x10) or 0), source_club,
            )
            or reader.ptr(player + PLAYER_CURRENT_TEAM) != source_team
        ):
            raise ValueError(
                "球员身份、主合同或所属俱乐部已经变化，请刷新球员资料后重试"
            )
        address = primary_contract + transfer_status_offset
        before = reader.u8(address)
        if before is None:
            raise RuntimeError("无法读取球员转会状态")
        after = _transfer_listing_status(int(before), bool(listed))
        write_process_memory(process, address, bytes((after,)))
        if reader.u8(address) != after:
            write_process_memory(process, address, bytes((int(before),)))
            if reader.u8(address) != int(before):
                raise RuntimeError("球员挂牌失败且原状态未能恢复，请不要保存当前存档")
            raise RuntimeError("球员挂牌写后回读失败")
        return {
            "player_id": int(player_id),
            "source_team_id": int(source_team_id),
            "listed": bool(listed),
            "transfer_status_before": int(before),
            "transfer_status_after": int(after),
            "verified": True,
            "compatibility_mode": f"{layout.key}_transfer_listed",
        }


def move_owned_club_staff(
    *, source_team_id: int, source_team_address: Any,
    target_team_id: int, target_team_address: Any,
    staff_id: int,
) -> dict[str, Any]:
    if int(source_team_id) <= 0 or int(target_team_id) <= 0 or int(staff_id) <= 0:
        raise ValueError("来源俱乐部、目标俱乐部或职员 ID 无效")
    if int(source_team_id) == int(target_team_id):
        raise ValueError("请选择另一家已收购俱乐部")
    _configure_thread_api()
    pid, _path, layout = select_process_layout()
    offsets = _required_staff_release_layout(layout)
    person_contract_offset = offsets["person_contract_offset"]
    game_label = "FM24" if layout.key == "fm24" else "FM26"
    with open_process(pid, write_memory=True) as process:
        module = layout.module(process)
        if not module:
            raise RuntimeError(f"{layout.module_name} 尚未加载")
        reader = _writable_player_movement_reader(process, module.base_address, layout)
        source_team = _resolve_team_address(
            reader, int(source_team_id), source_team_address,
        )
        target_team = _resolve_team_address(
            reader, int(target_team_id), target_team_address,
        )
        expected_club_vtable = module.base_address + int(layout.club_vtable_rva)
        for team, team_id in (
            (source_team, int(source_team_id)),
            (target_team, int(target_team_id)),
        ):
            club = int(reader.ptr(team + TEAM_CLUB) or 0)
            if (
                not club or reader.ptr(club) != expected_club_vtable
                or int(reader.u32(club + ENTITY_UID) or 0) != team_id
            ):
                raise RuntimeError("来源或目标俱乐部原生对象校验失败")

        matches = [
            row for row in _scan_staff(reader, source_team, 0)
            if int(row.get("id") or 0) == int(staff_id)
        ]
        if len(matches) != 1:
            raise ValueError("该职员已不在来源俱乐部，或职员对象无法唯一定位")
        staff = matches[0]
        job_type = int(staff.get("job_type") or 0)
        if is_club_controller_job_type(job_type):
            raise ValueError("俱乐部控制人不能通过职员转会功能移动")
        person = _address(staff.get("address"))
        contract = _validated_staff_main_contract(
            reader, module, layout,
            person=person, staff_id=int(staff_id), team=source_team,
            person_contract_offset=person_contract_offset,
            listed_contract=_address(staff.get("contract_address")),
            expected_job_type=job_type,
        )

        team_reference = contract + 0x10
        manager_base = 0
        source_manager_reference = 0
        target_manager_reference = 0
        if job_type == 16:
            manager_base = person - _team_manager_person_offset(layout)
            source_manager_reference = source_team + TEAM_MANAGER
            target_manager_reference = target_team + TEAM_MANAGER
            if int(reader.ptr(source_manager_reference) or 0) != manager_base:
                raise RuntimeError("来源球队主教练槽位已经变化，请刷新后重试")
            target_manager = int(reader.ptr(target_manager_reference) or 0)
            if target_manager:
                target_manager_person = (
                    target_manager + _team_manager_person_offset(layout)
                )
                target_contract = int(
                    reader.ptr(target_manager_person + person_contract_offset) or 0
                )
                if (
                    target_contract
                    and reader.ptr(target_contract + 0x10) == target_team
                ):
                    raise ValueError("目标俱乐部已有在职主教练，请先解雇或转移该主教练")

        references = [team_reference]
        if source_manager_reference:
            references.extend((source_manager_reference, target_manager_reference))
        snapshots = {
            address: read_process_memory(process, address, 8)
            for address in references
        }
        if snapshots[team_reference] != struct.pack("<Q", source_team):
            raise RuntimeError("职员合同球队引用快照校验失败")
        if any(len(raw) != 8 for raw in snapshots.values()):
            raise RuntimeError("职员转会事务快照读取失败")
        thread_id, idle_context = _logic_thread(process)
        handle = _open_thread(thread_id)
        suspended = False
        try:
            if not handle or kernel32.SuspendThread(handle) == 0xFFFFFFFF:
                raise RuntimeError(f"{game_label} 游戏逻辑线程当前无法暂停，请重试")
            suspended = True
            current_context = _get_context(handle)
            if (
                int(current_context.Rip) != int(idle_context.Rip)
                or int(current_context.Rsp) != int(idle_context.Rsp)
            ):
                raise RuntimeError(f"{game_label} 游戏逻辑线程刚刚恢复工作，请稍后重试")
            for address, raw in snapshots.items():
                if read_process_memory(process, address, 8) != raw:
                    raise RuntimeError("职员或主教练槽位在执行前已经变化，请刷新后重试")
            target_raw = struct.pack("<Q", target_team)
            write_process_memory(process, team_reference, target_raw)
            if source_manager_reference:
                write_process_memory(process, source_manager_reference, b"\0" * 8)
                write_process_memory(
                    process, target_manager_reference, struct.pack("<Q", manager_base),
                )
            valid = (
                read_process_memory(process, team_reference, 8) == target_raw
                and (
                    not source_manager_reference
                    or (
                        not reader.ptr(source_manager_reference)
                        and reader.ptr(target_manager_reference) == manager_base
                    )
                )
            )
            if not valid:
                raise RuntimeError("职员转会写后回读失败")
        except Exception:
            rollback_failed = False
            for address, raw in reversed(list(snapshots.items())):
                try:
                    write_process_memory(process, address, raw)
                except Exception:
                    rollback_failed = True
            for address, raw in snapshots.items():
                if read_process_memory(process, address, 8) != raw:
                    rollback_failed = True
            if rollback_failed:
                raise RuntimeError("职员转会失败且回滚未完整，请不要保存当前存档")
            raise
        finally:
            if suspended:
                kernel32.ResumeThread(handle)
            if handle:
                kernel32.CloseHandle(handle)
        return {
            "staff_id": int(staff_id),
            "staff_name": str(staff.get("name") or staff_id),
            "role": str(staff.get("role") or "职员"),
            "source_team_id": int(source_team_id),
            "target_team_id": int(target_team_id),
            "thread_id": int(thread_id),
            "verified": True,
            "compatibility_mode": f"{layout.key}_staff_move",
        }


def _staff_person_contract_offset(layout: Any) -> int:
    if layout.key == "fm24":
        return FM24_PERSON_CONTRACT
    if layout.key == "fm26":
        return PERSON_CONTRACT
    raise RuntimeError("主教练合同操作仅支持已识别的 FM24 与 FM26 版本")


FULL_TIME_STAFF_CONTRACT_TYPE = 1


def _set_full_time_staff_contract_type(contract_data: bytearray, layout: Any) -> int:
    """Force a newly assigned staff contract to FM's full-time type."""
    offset = getattr(layout, "staff_contract_type_offset", None)
    if offset is None or int(offset) < 0 or int(offset) >= len(contract_data):
        raise RuntimeError("当前游戏布局尚未定位职员合同类型字段")
    contract_data[int(offset)] = FULL_TIME_STAFF_CONTRACT_TYPE
    return int(offset)


def _manager_appointment_contract_data(
    template: bytes, *, layout: Any, candidate_person: int, target_team: int,
    game_date_code: int,
) -> tuple[bytes, int, date, date]:
    """Build the contract written by the release-then-appoint workflow."""
    current_date = decode_date(int(game_date_code))
    if not current_date:
        raise RuntimeError("无法读取当前游戏日期")
    expiry = _add_years(current_date, 3)
    contract_data = bytearray(template)
    struct.pack_into("<Q", contract_data, 0x08, int(candidate_person))
    struct.pack_into("<Q", contract_data, 0x10, int(target_team))
    contract_type_offset = _set_full_time_staff_contract_type(contract_data, layout)
    if layout.staff_job_type_offset is None:
        raise RuntimeError("当前游戏布局尚未定位主教练职务字段")
    contract_data[int(layout.staff_job_type_offset)] = 16
    if (
        layout.staff_contract_start_offset is None
        or layout.staff_contract_expiry_offset is None
    ):
        raise RuntimeError("当前游戏布局尚未定位主教练合同日期")
    date_flags = int(game_date_code) & 0xFE00
    struct.pack_into(
        "<I", contract_data, int(layout.staff_contract_start_offset),
        _encode_date(current_date) | date_flags,
    )
    struct.pack_into(
        "<I", contract_data, int(layout.staff_contract_expiry_offset),
        _encode_date(expiry) | date_flags,
    )
    return bytes(contract_data), contract_type_offset, current_date, expiry


def _team_manager_person_offset(layout: Any) -> int:
    return int(
        getattr(layout, "team_manager_person_offset", layout.staff_complete_object_offset)
    )


def _validated_club_team(reader: Reader, module: Any, team_id: int, address: Any) -> int:
    team = _resolve_team_address(reader, int(team_id), address)
    club = int(reader.ptr(team + TEAM_CLUB) or 0)
    if (
        not club
        or reader.ptr(club) != module.base_address + int(reader.layout.club_vtable_rva)
        or int(reader.u32(club + ENTITY_UID) or 0) != int(team_id)
    ):
        raise RuntimeError("俱乐部原生对象校验失败，请刷新俱乐部资料后重试")
    return team


def extend_owned_club_manager_contract(
    *, team_id: int, team_address: Any, manager_id: int, years: int = 2,
) -> dict[str, Any]:
    if int(team_id) <= 0 or int(manager_id) <= 0:
        raise ValueError("俱乐部或主教练 ID 无效")
    if int(years) != 2:
        raise ValueError("当前仅支持将主教练合同延长两年")
    _configure_thread_api()
    pid, _path, layout = select_process_layout()
    person_contract_offset = _staff_person_contract_offset(layout)
    expiry_offset = layout.staff_contract_expiry_offset
    job_type_offset = layout.staff_job_type_offset
    if expiry_offset is None or job_type_offset is None:
        raise RuntimeError("当前游戏布局尚未定位主教练合同日期")
    with open_process(pid, write_memory=True) as process:
        module = layout.module(process)
        if not module:
            raise RuntimeError(f"{layout.module_name} 尚未加载")
        reader = _writable_player_movement_reader(process, module.base_address, layout)
        team = _validated_club_team(reader, module, int(team_id), team_address)
        manager = int(reader.ptr(team + TEAM_MANAGER) or 0)
        person = manager + _team_manager_person_offset(layout) if manager else 0
        if not manager:
            raise ValueError("当前没有可续约的主教练，请刷新后重试")
        contract = _validated_staff_main_contract(
            reader, module, layout,
            person=person, staff_id=int(manager_id), team=team,
            person_contract_offset=person_contract_offset,
            expected_job_type=16, label="主教练",
        )
        expiry_address = contract + int(expiry_offset)
        before_raw = int(reader.u32(expiry_address) or 0)
        before = decode_date(before_raw)
        if not before:
            raise RuntimeError("主教练合同到期日不是可安全修改的有效日期")
        after = _add_years(before, 2)
        after_raw = _encode_date(after) | (before_raw & 0xFE00)
        before_bytes = struct.pack("<I", before_raw)
        after_bytes = struct.pack("<I", after_raw)
        thread_id, idle_context = _logic_thread(process)
        handle = _open_thread(thread_id)
        suspended = False
        try:
            if not handle or kernel32.SuspendThread(handle) == 0xFFFFFFFF:
                raise RuntimeError("FM 游戏逻辑线程当前无法暂停，请重试")
            suspended = True
            current_context = _get_context(handle)
            if (
                int(current_context.Rip) != int(idle_context.Rip)
                or int(current_context.Rsp) != int(idle_context.Rsp)
            ):
                raise RuntimeError("FM 游戏逻辑线程刚刚恢复工作，请稍后重试")
            if read_process_memory(process, expiry_address, 4) != before_bytes:
                raise RuntimeError("主教练合同日期在执行前已经变化，请刷新后重试")
            write_process_memory(process, expiry_address, after_bytes)
            if read_process_memory(process, expiry_address, 4) != after_bytes:
                write_process_memory(process, expiry_address, before_bytes)
                if read_process_memory(process, expiry_address, 4) != before_bytes:
                    raise RuntimeError("主教练续约失败且原日期未能恢复，请不要保存当前存档")
                raise RuntimeError("主教练合同日期写后回读失败")
        finally:
            if suspended:
                kernel32.ResumeThread(handle)
            if handle:
                kernel32.CloseHandle(handle)
        return {
            "team_id": int(team_id),
            "manager_id": int(manager_id),
            "contract_address": hex(contract),
            "before_expiry_date": before.isoformat(),
            "contract_expiry_date": after.isoformat(),
            "years_extended": 2,
            "thread_id": int(thread_id),
            "verified": True,
            "compatibility_mode": f"{layout.key}_manager_contract_extension",
        }


def replace_owned_club_manager(
    *, target_team_id: int, target_team_address: Any,
    current_manager_id: int, candidate_manager_id: int,
    candidate_team_id: int, scan_manager_id: int = 0,
) -> dict[str, Any]:
    identifiers = (
        int(target_team_id), int(current_manager_id),
        int(candidate_manager_id), int(candidate_team_id),
    )
    if any(value <= 0 for value in identifiers):
        raise ValueError("目标俱乐部、现任主教练或候选主教练 ID 无效")
    if int(target_team_id) == int(candidate_team_id):
        raise ValueError("候选主教练已经在目标俱乐部任职")
    if int(current_manager_id) == int(candidate_manager_id):
        raise ValueError("请选择另一名主教练")
    _configure_thread_api()
    pid, _path, layout = select_process_layout()
    person_contract_offset = _staff_person_contract_offset(layout)
    if layout.staff_job_type_offset is None:
        raise RuntimeError("当前游戏布局尚未定位主教练职务字段")
    with open_process(pid, write_memory=True) as process:
        module = layout.module(process)
        if not module:
            raise RuntimeError(f"{layout.module_name} 尚未加载")
        reader = _writable_player_movement_reader(process, module.base_address, layout)
        target_team = _validated_club_team(
            reader, module, int(target_team_id), target_team_address,
        )
        candidates = [
            row for row in _scan_head_coaches(reader, int(scan_manager_id))
            if int(row.get("id") or 0) == int(candidate_manager_id)
            and int(row.get("team_id") or 0) == int(candidate_team_id)
        ]
        if len(candidates) != 1:
            raise ValueError("候选主教练已不在原俱乐部任职，请重新搜索后再试")
        candidate = candidates[0]
        source_team = _validated_club_team(
            reader, module, int(candidate_team_id), candidate.get("team_address"),
        )
        candidate_person = _address(candidate.get("address"))
        candidate_contract = _address(candidate.get("contract_address"))
        candidate_manager = candidate_person - _team_manager_person_offset(layout)

        current_manager = int(reader.ptr(target_team + TEAM_MANAGER) or 0)
        current_person = (
            current_manager + _team_manager_person_offset(layout)
            if current_manager else 0
        )
        current_contract = (
            int(reader.ptr(current_person + person_contract_offset) or 0)
            if current_person else 0
        )
        candidate_contract = _validated_staff_main_contract(
            reader, module, layout,
            person=candidate_person, staff_id=int(candidate_manager_id),
            team=source_team, person_contract_offset=person_contract_offset,
            listed_contract=candidate_contract, expected_job_type=16,
            label="候选主教练",
        )
        if reader.ptr(source_team + TEAM_MANAGER) != candidate_manager:
            raise ValueError("候选主教练的任职状态已变化，请重新搜索后重试")
        current_contract = _validated_staff_main_contract(
            reader, module, layout,
            person=current_person, staff_id=int(current_manager_id),
            team=target_team, person_contract_offset=person_contract_offset,
            listed_contract=current_contract or None, expected_job_type=16,
            label="现任主教练",
        )

        expected_before = {
            candidate_contract + 0x10: struct.pack("<Q", source_team),
            current_contract + 0x10: struct.pack("<Q", target_team),
            source_team + TEAM_MANAGER: struct.pack("<Q", candidate_manager),
            target_team + TEAM_MANAGER: struct.pack("<Q", current_manager),
        }
        references = {
            candidate_contract + 0x10: struct.pack("<Q", target_team),
            current_contract + 0x10: struct.pack("<Q", source_team),
            source_team + TEAM_MANAGER: struct.pack("<Q", current_manager),
            target_team + TEAM_MANAGER: struct.pack("<Q", candidate_manager),
        }
        snapshots = {
            address: read_process_memory(process, address, 8)
            for address in references
        }
        if snapshots != expected_before:
            raise RuntimeError("主教练任职状态已变化，请刷新后重试")
        thread_id, idle_context = _logic_thread(process)
        handle = _open_thread(thread_id)
        suspended = False
        try:
            if not handle or kernel32.SuspendThread(handle) == 0xFFFFFFFF:
                raise RuntimeError("FM 游戏逻辑线程当前无法暂停，请重试")
            suspended = True
            current_context = _get_context(handle)
            if (
                int(current_context.Rip) != int(idle_context.Rip)
                or int(current_context.Rsp) != int(idle_context.Rsp)
            ):
                raise RuntimeError("FM 游戏逻辑线程刚刚恢复工作，请稍后重试")
            for address, raw in snapshots.items():
                if read_process_memory(process, address, 8) != raw:
                    raise RuntimeError("主教练合同或球队岗位在执行前已经变化，请刷新后重试")
            for address, raw in references.items():
                write_process_memory(process, address, raw)
            if any(
                read_process_memory(process, address, 8) != raw
                for address, raw in references.items()
            ):
                raise RuntimeError("主教练更换写后回读失败")
        except Exception:
            rollback_failed = False
            for address, raw in reversed(list(snapshots.items())):
                try:
                    write_process_memory(process, address, raw)
                except Exception:
                    rollback_failed = True
            if any(
                read_process_memory(process, address, 8) != raw
                for address, raw in snapshots.items()
            ):
                rollback_failed = True
            if rollback_failed:
                raise RuntimeError("主教练更换失败且回滚未完整，请不要保存当前存档")
            raise
        finally:
            if suspended:
                kernel32.ResumeThread(handle)
            if handle:
                kernel32.CloseHandle(handle)
        return {
            "target_team_id": int(target_team_id),
            "source_team_id": int(candidate_team_id),
            "previous_manager_id": int(current_manager_id),
            "manager_id": int(candidate_manager_id),
            "manager_name": str(candidate.get("name") or candidate_manager_id),
            "contract_expiry_date": candidate.get("contract_expiry_date"),
            "previous_manager_destination_team_id": int(candidate_team_id),
            "thread_id": int(thread_id),
            "verified": True,
            "compatibility_mode": f"{layout.key}_manager_swap",
        }


def appoint_free_agent_manager(
    *, target_team_id: int, target_team_address: Any,
    candidate_staff_id: int, candidate_name: str = "",
) -> dict[str, Any]:
    if int(target_team_id) <= 0 or int(candidate_staff_id) <= 0:
        raise ValueError("目标俱乐部或候选主教练 ID 无效")
    _configure_thread_api()
    pid, _path, layout = select_process_layout()
    person_contract_offset = _staff_person_contract_offset(layout)
    with open_process(pid, write_memory=True) as process:
        module = layout.module(process)
        if not module:
            raise RuntimeError(f"{layout.module_name} 尚未加载")
        reader = _writable_player_movement_reader(process, module.base_address, layout)
        target_team = _validated_club_team(
            reader, module, int(target_team_id), target_team_address,
        )
        current_manager = int(reader.ptr(target_team + TEAM_MANAGER) or 0)
        if not current_manager:
            return _appoint_free_agent_manager_to_vacancy(
                process=process, module=module, reader=reader, layout=layout,
                target_team=target_team, target_team_id=int(target_team_id),
                candidate_staff_id=int(candidate_staff_id),
                candidate_name=str(candidate_name or ""),
            )
        manager_offset = _team_manager_person_offset(layout)
        current_person = current_manager + manager_offset
        current_uid = int(reader.u32(current_person + ENTITY_UID) or 0)
        current_contract = _validated_staff_main_contract(
            reader, module, layout,
            person=current_person, staff_id=current_uid, team=target_team,
            person_contract_offset=person_contract_offset,
            expected_job_type=16, label="现任主教练",
        )

        normalized_name = str(candidate_name or "").strip().casefold()
        matches: list[tuple[int, str]] = []
        for person, _captured_contract in _staff_scan_entries(reader, 0):
            if int(reader.u32(person + ENTITY_UID) or 0) != int(candidate_staff_id):
                continue
            name = str(_name(reader, person) or "")
            if normalized_name and name.casefold() != normalized_name:
                continue
            matches.append((person, name))
        if len(matches) != 1:
            raise ValueError("候选主教练已变化，请重新搜索后重试")
        candidate_person, resolved_name = matches[0]
        expected_person_vtable = module.base_address + int(layout.staff_person_vtable_rva or 0)
        candidate_contract = int(
            reader.ptr(candidate_person + person_contract_offset) or 0
        )
        candidate_manager = candidate_person - manager_offset
        if (
            not expected_person_vtable
            or reader.ptr(candidate_person) != expected_person_vtable
            or candidate_contract
            or reader.ptr(candidate_manager) != reader.ptr(current_manager)
        ):
            raise ValueError("候选主教练当前不是可任命的自由职员")

        offsets = _required_staff_release_layout(layout)
        contract_size = int(offsets["contract_size"])
        current_contract_data = reader.bytes(current_contract, contract_size)
        if len(current_contract_data) != contract_size:
            raise RuntimeError("主教练合同事务快照读取失败")
        game_date_rva = getattr(layout, "game_date_rva", None)
        game_date_code = int(
            reader.u32(module.base_address + int(game_date_rva)) or 0
        ) if game_date_rva is not None else 0
        (
            replacement_contract, contract_type_offset,
            contract_start, contract_expiry,
        ) = _manager_appointment_contract_data(
            current_contract_data,
            layout=layout,
            candidate_person=candidate_person,
            target_team=target_team,
            game_date_code=game_date_code,
        )
        expected_before = {
            current_person + person_contract_offset: struct.pack("<Q", current_contract),
            candidate_person + person_contract_offset: b"\0" * 8,
            current_contract: current_contract_data,
            target_team + TEAM_MANAGER: struct.pack("<Q", current_manager),
        }
        replacements = {
            current_person + person_contract_offset: b"\0" * 8,
            current_contract: replacement_contract,
            candidate_person + person_contract_offset: struct.pack("<Q", current_contract),
            target_team + TEAM_MANAGER: struct.pack("<Q", candidate_manager),
        }
        snapshots = {
            address: read_process_memory(process, address, len(raw))
            for address, raw in expected_before.items()
        }
        if snapshots != expected_before:
            raise RuntimeError("主教练任职状态已变化，请刷新后重试")

        thread_id, idle_context = _logic_thread(process)
        handle = _open_thread(thread_id)
        suspended = False
        try:
            if not handle or kernel32.SuspendThread(handle) == 0xFFFFFFFF:
                raise RuntimeError("FM 游戏逻辑线程当前无法暂停，请重试")
            suspended = True
            current_context = _get_context(handle)
            if (
                int(current_context.Rip) != int(idle_context.Rip)
                or int(current_context.Rsp) != int(idle_context.Rsp)
            ):
                raise RuntimeError("FM 游戏逻辑线程刚刚恢复工作，请稍后重试")
            for address, raw in snapshots.items():
                if read_process_memory(process, address, len(raw)) != raw:
                    raise RuntimeError("主教练任职状态已变化，请刷新后重试")
            for address, raw in replacements.items():
                write_process_memory(process, address, raw)
            if any(
                read_process_memory(process, address, len(raw)) != raw
                for address, raw in replacements.items()
            ):
                raise RuntimeError("主教练任命写后回读失败")
        except Exception:
            rollback_failed = False
            for address, raw in reversed(list(snapshots.items())):
                try:
                    write_process_memory(process, address, raw)
                except Exception:
                    rollback_failed = True
            if any(
                read_process_memory(process, address, len(raw)) != raw
                for address, raw in snapshots.items()
            ):
                rollback_failed = True
            if rollback_failed:
                raise RuntimeError("主教练任命失败且回滚未完整，请不要保存当前存档")
            raise
        finally:
            if suspended:
                kernel32.ResumeThread(handle)
            if handle:
                kernel32.CloseHandle(handle)

        time.sleep(0.25)
        if (
            reader.ptr(current_person + person_contract_offset)
            or reader.ptr(candidate_person + person_contract_offset) != current_contract
            or reader.ptr(current_contract + 0x08) != candidate_person
            or reader.ptr(current_contract + 0x10) != target_team
            or reader.ptr(target_team + TEAM_MANAGER) != candidate_manager
            or reader.u8(current_contract + contract_type_offset)
            != FULL_TIME_STAFF_CONTRACT_TYPE
            or reader.u8(current_contract + int(layout.staff_job_type_offset)) != 16
        ):
            raise RuntimeError("游戏未保留本次主教练任命，请不要保存当前存档")
        return {
            "target_team_id": int(target_team_id),
            "previous_manager_id": current_uid,
            "manager_id": int(candidate_staff_id),
            "manager_name": resolved_name or str(candidate_staff_id),
            "contract_address": hex(current_contract),
            "contract_start_date": contract_start.isoformat(),
            "contract_expiry_date": contract_expiry.isoformat(),
            "thread_id": int(thread_id),
            "verified": True,
            "compatibility_mode": (
                f"{layout.key}_existing_manager_release_then_appointment"
            ),
        }


def _appoint_free_agent_manager_to_vacancy(
    *, process: Any, module: Any, reader: Reader, layout: Any,
    target_team: int, target_team_id: int,
    candidate_staff_id: int, candidate_name: str,
) -> dict[str, Any]:
    offsets = _required_staff_release_layout(layout)
    person_contract_offset = int(offsets["person_contract_offset"])
    contract_size = int(offsets["contract_size"])
    manager_offset = _team_manager_person_offset(layout)
    normalized_name = str(candidate_name or "").strip().casefold()
    candidates: list[tuple[int, str]] = []
    for person, _captured_contract in _staff_scan_entries(reader, 0):
        if int(reader.u32(person + ENTITY_UID) or 0) != int(candidate_staff_id):
            continue
        name = str(_name(reader, person) or "")
        if normalized_name and name.casefold() != normalized_name:
            continue
        candidates.append((person, name))
    if len(candidates) != 1:
        raise ValueError("候选主教练已变化，请重新搜索后重试")
    candidate_person, resolved_name = candidates[0]
    candidate_manager = candidate_person - manager_offset
    expected_person_vtable = module.base_address + int(layout.staff_person_vtable_rva or 0)
    if (
        not expected_person_vtable
        or reader.ptr(candidate_person) != expected_person_vtable
        or reader.ptr(candidate_person + person_contract_offset)
    ):
        raise ValueError("候选主教练当前不是可任命的自由职员")

    template_data = None
    template_manager = 0
    for row in _scan_head_coaches(reader, 0):
        template_person = _address(row.get("address"))
        template_contract = _address(row.get("contract_address"))
        if not template_person or not template_contract:
            continue
        raw = reader.bytes(template_contract, contract_size)
        if (
            raw and len(raw) == contract_size
            and reader.ptr(template_contract) == module.base_address + int(
                offsets["player_move_main_contract_vtable_rva"]
            )
        ):
            template_data = raw
            template_manager = template_person - manager_offset
            break
    if not template_data or not template_manager:
        raise RuntimeError("当前没有可用的主教练合同模板")
    if reader.ptr(candidate_manager) != reader.ptr(template_manager):
        raise ValueError("候选主教练当前不是可任命的自由职员")

    pool = module.base_address + int(offsets["player_move_contract_pool_rva"])
    pool_head = int(reader.ptr(pool + 0x28) or 0)
    pool_available = int(reader.u32(pool + 0x30) or 0)
    pool_in_use = int(reader.u32(pool + 0x34) or 0)
    pool_item_size = int(reader.u32(pool + 0x38) or 0)
    if (
        not pool_head or pool_available <= 0 or pool_item_size != contract_size
    ):
        raise RuntimeError("主合同池当前没有可用合同")
    next_free = int(reader.ptr(pool_head) or 0)
    if pool_available > 1 and not next_free:
        raise RuntimeError("主合同池空闲链状态异常")

    game_date_rva = getattr(layout, "game_date_rva", None)
    game_date_code = int(
        reader.u32(module.base_address + int(game_date_rva)) or 0
    ) if game_date_rva is not None else 0
    new_contract, contract_type_offset, current_date, expiry = (
        _manager_appointment_contract_data(
            template_data,
            layout=layout,
            candidate_person=candidate_person,
            target_team=target_team,
            game_date_code=game_date_code,
        )
    )

    snapshot_addresses = {
        pool + 0x28: 0x18,
        pool_head: contract_size,
        candidate_person + person_contract_offset: 8,
        target_team + TEAM_MANAGER: 8,
    }
    snapshots = {
        address: read_process_memory(process, address, size)
        for address, size in snapshot_addresses.items()
    }
    if any(len(raw) != snapshot_addresses[address] for address, raw in snapshots.items()):
        raise RuntimeError("主教练任命快照读取失败")
    expected_pool = struct.pack(
        "<QIIII", pool_head, pool_available, pool_in_use, pool_item_size, 0,
    )
    pool_snapshot = snapshots[pool + 0x28]
    if (
        pool_snapshot[:20] != expected_pool[:20]
        or snapshots[candidate_person + person_contract_offset] != b"\0" * 8
        or snapshots[target_team + TEAM_MANAGER] != b"\0" * 8
    ):
        raise RuntimeError("主教练任职状态已变化，请刷新后重试")

    thread_id, idle_context = _logic_thread(process)
    handle = _open_thread(thread_id)
    suspended = False
    committed = False
    try:
        if not handle or kernel32.SuspendThread(handle) == 0xFFFFFFFF:
            raise RuntimeError("FM 游戏逻辑线程当前无法暂停，请重试")
        suspended = True
        current_context = _get_context(handle)
        if (
            int(current_context.Rip) != int(idle_context.Rip)
            or int(current_context.Rsp) != int(idle_context.Rsp)
        ):
            raise RuntimeError("FM 游戏逻辑线程刚刚恢复工作，请稍后重试")
        for address, raw in snapshots.items():
            if read_process_memory(process, address, len(raw)) != raw:
                raise RuntimeError("主教练任职状态已变化，请刷新后重试")
        writes = {
            pool + 0x28: struct.pack("<Q", next_free),
            pool + 0x30: struct.pack("<I", pool_available - 1),
            pool + 0x34: struct.pack("<I", pool_in_use + 1),
            pool_head: bytes(new_contract),
            candidate_person + person_contract_offset: struct.pack("<Q", pool_head),
            target_team + TEAM_MANAGER: struct.pack("<Q", candidate_manager),
        }
        for address, raw in writes.items():
            write_process_memory(process, address, raw)
            if read_process_memory(process, address, len(raw)) != raw:
                raise RuntimeError("主教练任命写后回读失败")
        valid = (
            reader.ptr(candidate_person + person_contract_offset) == pool_head
            and reader.ptr(pool_head) == module.base_address + int(
                offsets["player_move_main_contract_vtable_rva"]
            )
            and reader.ptr(pool_head + 0x08) == candidate_person
            and reader.ptr(pool_head + 0x10) == target_team
            and reader.u8(pool_head + int(layout.staff_job_type_offset)) == 16
            and reader.u8(pool_head + contract_type_offset) == FULL_TIME_STAFF_CONTRACT_TYPE
            and reader.ptr(target_team + TEAM_MANAGER) == candidate_manager
            and int(reader.u32(pool + 0x30) or 0) == pool_available - 1
            and int(reader.u32(pool + 0x34) or 0) == pool_in_use + 1
        )
        if not valid:
            raise RuntimeError("主教练任命写后校验失败")
        committed = True
    except Exception:
        rollback_failed = False
        for address, raw in reversed(list(snapshots.items())):
            try:
                write_process_memory(process, address, raw)
            except Exception:
                rollback_failed = True
        if any(
            read_process_memory(process, address, len(raw)) != raw
            for address, raw in snapshots.items()
        ):
            rollback_failed = True
        if rollback_failed:
            raise RuntimeError("主教练任命失败且回滚未完整，请不要保存当前存档")
        raise
    finally:
        if suspended:
            kernel32.ResumeThread(handle)
        if handle:
            kernel32.CloseHandle(handle)

    time.sleep(0.25)
    if (
        reader.ptr(candidate_person + person_contract_offset) != pool_head
        or reader.ptr(pool_head + 0x08) != candidate_person
        or reader.ptr(pool_head + 0x10) != target_team
        or reader.ptr(target_team + TEAM_MANAGER) != candidate_manager
        or reader.u8(pool_head + contract_type_offset) != FULL_TIME_STAFF_CONTRACT_TYPE
    ):
        raise RuntimeError("游戏未保留本次主教练任命，请不要保存当前存档")
    return {
        "target_team_id": int(target_team_id),
        "previous_manager_id": None,
        "manager_id": int(candidate_staff_id),
        "manager_name": resolved_name or str(candidate_staff_id),
        "contract_address": hex(pool_head),
        "thread_id": int(thread_id),
        "verified": bool(committed),
        "compatibility_mode": f"{layout.key}_vacant_manager_appointment",
    }


def _fm26_fmrte_contract_data(
    template: bytes, *, main_vtable: int, person: int, target_team: int,
    start_code: int, expiry_code: int,
) -> bytes:
    """Clone the same full-contract record copied by FMRTE's pool manager."""
    if len(template) != CONTRACT_SIZE:
        raise ValueError("FM26 主合同模板大小无效")
    data = bytearray(template)
    struct.pack_into("<Q", data, 0x00, int(main_vtable))
    struct.pack_into("<Q", data, 0x08, int(person))
    struct.pack_into("<Q", data, 0x10, int(target_team))
    struct.pack_into("<I", data, FM26_CONTRACT_STARTED, int(start_code))
    struct.pack_into("<I", data, FM26_CONTRACT_EXPIRY, int(expiry_code))
    struct.pack_into("<I", data, FM26_CONTRACT_SIGNED, int(start_code))
    # FMRTE clears transfer/loan listing state after a permanent move.  This
    # byte is independently used by FMODD's transfer-list operation.
    data[FM26_TRANSFER_STATUS] = 0
    return bytes(data)


def _fm26_contract_slot_plan(
    *, pool_head: int, pool_available: int, pool_item_size: int,
    next_free: int, primary_contract: int, template_contract: int,
) -> tuple[int, bool]:
    """Choose a free contract slot or reuse the player's slot when exhausted."""
    if pool_item_size != CONTRACT_SIZE or not primary_contract:
        raise RuntimeError("FM26 主合同池空闲链校验失败")
    if pool_available == 0:
        if pool_head:
            raise RuntimeError("FM26 主合同池空闲链校验失败")
        return primary_contract, True
    if (
        not pool_head
        or (pool_available > 1 and not next_free)
        or pool_head in {primary_contract, template_contract}
    ):
        raise RuntimeError("FM26 主合同池空闲链校验失败")
    return pool_head, False


def _team_belongs_to_club(reader: Reader, team: int, club: int) -> bool:
    return bool(team and club and int(reader.ptr(team + TEAM_CLUB) or 0) == int(club))


def _valid_registered_club_team(reader: Reader, team: int) -> bool:
    """Accept a live club Team even when its registration club is stale.

    FM can retain a previous registration Team after the player has returned to
    the club that owns the primary contract. Movement transactions replace this
    field as part of their verified commit, so requiring the old value to belong
    to the source club rejects an otherwise coherent source object.  A non-null
    value must still resolve to a structurally valid club Team.
    """
    if not int(team):
        return True
    row = reader.team(int(team))
    return bool(
        row
        and row.get("team_type") == "club"
        and int(row.get("id") or 0) > 0
        and int(reader.ptr(int(team) + TEAM_CLUB) or 0) > 0
    )


def _move_fm26_owned_club_player_fmrte(
    *, pid: int, layout: Any,
    source_team_id: int, source_team_address: Any,
    source_squad_team_id: int = 0, source_squad_team_address: Any = 0,
    target_team_id: int, target_team_address: Any,
    player_id: int, mode: str, transfer_fee: int = 0,
    commit_transfer: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    if mode != "transfer":
        raise RuntimeError(
            "FM26 租借已暂时关闭：旧路径与导致闪退的永久转会共用不完整的 Club 原生调用"
        )
    offsets = _required_fm26_fmrte_layout(layout)
    with open_process(pid, write_memory=True) as process:
        module = layout.module(process)
        if not module:
            raise RuntimeError(f"{layout.module_name} 尚未加载")
        reader = _writable_player_movement_reader(process, module.base_address, layout)
        context = _resolve_player_movement_context(
            reader, module, layout,
            source_team_id=source_team_id,
            source_team_address=source_team_address,
            source_squad_team_id=source_squad_team_id,
            source_squad_team_address=source_squad_team_address,
            target_team_id=target_team_id,
            target_team_address=target_team_address,
            player_id=player_id,
        )
        source_team, target_team = context.source_team, context.target_team
        source_club, target_club = context.source_club, context.target_club
        source_rows, target_rows = context.source_rows, context.target_rows
        player, person = context.player, context.person
        primary_contract = int(reader.ptr(person + PERSON_CONTRACT) or 0)
        other_contracts = int(reader.ptr(person + PERSON_CONTRACT_COLLECTION) or 0)
        current_team = int(reader.ptr(player + PLAYER_CURRENT_TEAM) or 0)
        registered_team = int(reader.ptr(player + PLAYER_REGISTERED_TEAM) or 0)
        contract_team = (
            int(reader.ptr(primary_contract + 0x10) or 0)
            if primary_contract else 0
        )
        main_vtable = (
            module.base_address + offsets["player_move_main_contract_vtable_rva"]
        )
        if other_contracts:
            raise ValueError("该球员正在外租")
        state_failures: list[str] = []
        if not primary_contract:
            state_failures.append("主合同为空")
        elif reader.ptr(primary_contract) != main_vtable:
            state_failures.append("主合同类型")
        elif reader.ptr(primary_contract + 0x08) != person:
            state_failures.append("主合同人物回指")
        if primary_contract and not _team_belongs_to_club(
            reader, contract_team, source_club,
        ):
            state_failures.append("主合同所属球队")
        if current_team != source_team:
            state_failures.append("当前球队")
        if not _valid_registered_club_team(reader, registered_team):
            state_failures.append("注册球队")
        if state_failures:
            raise PlayerMovementContextChangedError(
                "球员状态校验失败：" + "、".join(state_failures),
                retryable=False, error_code="player_state_changed",
                error_phase="preflight",
            )
        if has_confirmed_future_transfer(reader, person):
            raise ValueError(
                "该球员存在已确认的未来转会或租借，请先在未来转会中叫停后再移动"
            )

        template_contract = 0
        template_data: bytes | None = None
        for row in target_rows:
            candidate = _address(row.get("address"))
            candidate_id = int(row.get("id") or 0)
            if not candidate or not candidate_id:
                continue
            try:
                candidate_person = _validated_player_person(
                    reader, candidate, candidate_id,
                )
            except Exception:
                continue
            candidate_contract = int(
                reader.ptr(candidate_person + PERSON_CONTRACT) or 0
            )
            if (
                candidate_contract
                and reader.ptr(candidate_contract) == main_vtable
                and reader.ptr(candidate_contract + 0x08) == candidate_person
                and reader.ptr(candidate_contract + 0x10) == target_team
            ):
                raw = reader.bytes(candidate_contract, CONTRACT_SIZE)
                if raw and len(raw) == CONTRACT_SIZE:
                    template_contract, template_data = candidate_contract, raw
                    break
        if not template_contract or template_data is None:
            raise RuntimeError("目标俱乐部没有可验证的主合同模板")

        source_begin, source_end, source_capacity, source_pointers = _roster_vector(
            reader, source_team,
        )
        target_begin, target_end, target_capacity, target_pointers = _roster_vector(
            reader, target_team,
        )
        if source_pointers.count(player) != 1 or player in target_pointers:
            raise PlayerMovementContextChangedError(
                "来源或目标名单在事务准备期间发生变化"
            )
        source_after_pointers = _remove_unique_pointer(source_pointers, player)
        target_after_pointers = [*target_pointers, player]

        pool = module.base_address + offsets["player_move_contract_pool_rva"]
        pool_head = int(reader.ptr(pool + 0x28) or 0)
        pool_available = int(reader.u32(pool + 0x30) or 0)
        pool_in_use = int(reader.u32(pool + 0x34) or 0)
        pool_item_size = int(reader.u32(pool + 0x38) or 0)
        next_free = int(reader.ptr(pool_head) or 0) if pool_head else 0
        contract_slot, reuse_primary_contract = _fm26_contract_slot_plan(
            pool_head=pool_head,
            pool_available=pool_available,
            pool_item_size=pool_item_size,
            next_free=next_free,
            primary_contract=primary_contract,
            template_contract=template_contract,
        )
        free_slot = reader.bytes(contract_slot, CONTRACT_SIZE)
        old_contract = reader.bytes(primary_contract, CONTRACT_SIZE)
        if (
            not free_slot or len(free_slot) != CONTRACT_SIZE
            or not old_contract or len(old_contract) != CONTRACT_SIZE
        ):
            raise RuntimeError("FM26 主合同池空闲链校验失败")

        game_date_code = int(
            reader.u32(module.base_address + offsets["game_date_rva"]) or 0
        )
        current_date = decode_date(game_date_code)
        if not current_date:
            raise RuntimeError("无法读取当前游戏日期")
        template_expiry = decode_date(
            struct.unpack_from("<I", template_data, FM26_CONTRACT_EXPIRY)[0]
        )
        expiry = (
            template_expiry
            if template_expiry and template_expiry > current_date + timedelta(days=30)
            else _add_years(current_date, 3)
        )
        expiry_code = _encode_date(expiry)
        new_contract = _fm26_fmrte_contract_data(
            template_data, main_vtable=main_vtable, person=person,
            target_team=target_team, start_code=game_date_code,
            expiry_code=expiry_code,
        )

        snapshots: list[tuple[int, bytes]] = []
        seen: set[tuple[int, int]] = set()

        def snapshot(address: int, size: int) -> bytes:
            key = (int(address), int(size))
            raw = reader.bytes(address, size)
            if not raw or len(raw) != size:
                raise RuntimeError(f"FM26 转会快照读取失败：{hex(address)}")
            if key not in seen:
                snapshots.append((address, raw))
                seen.add(key)
            return raw

        def checked_write(address: int, raw: bytes) -> None:
            write_process_memory(process, address, raw)
            if read_process_memory(process, address, len(raw)) != raw:
                raise RuntimeError(f"FM26 转会写后回读失败：{hex(address)}")

        snapshot(pool + 0x28, 0x18)
        snapshot(contract_slot, CONTRACT_SIZE)
        snapshot(primary_contract, CONTRACT_SIZE)
        snapshot(source_team + TEAM_ROSTER_BEGIN, 24)
        snapshot(target_team + TEAM_ROSTER_BEGIN, 24)
        snapshot(source_begin, source_capacity - source_begin)
        snapshot(target_begin, target_capacity - target_begin)
        snapshot(person + FM26_PREVIOUS_CLUB, 8)
        snapshot(person + PERSON_CONTRACT, 8)
        snapshot(person + FM26_JOINED_CLUB_DATE, 4)
        snapshot(player + PLAYER_CURRENT_TEAM, 16)

        sale_credit = _sale_credit(reader, source_club, transfer_fee)
        if sale_credit:
            finance = int(reader.ptr(source_club + int(layout.club_finance_offset)) or 0)
            snapshot(source_club + int(layout.club_finance_offset), 8)
            snapshot(finance + 8, 8)
            snapshot(sale_credit[0], 4)

        thread_id, idle_context = _logic_thread(process)
        handle = _open_thread(thread_id)
        allocated_target_storage = 0
        suspended = False
        committed = False
        try:
            if not handle or kernel32.SuspendThread(handle) == 0xFFFFFFFF:
                raise RuntimeError("FM26 游戏逻辑线程当前无法暂停，请稍后重试")
            suspended = True
            current_context = _get_context(handle)
            if (
                int(current_context.Rip) != int(idle_context.Rip)
                or int(current_context.Rsp) != int(idle_context.Rsp)
            ):
                raise RuntimeError("FM26 游戏逻辑线程已恢复工作，请稍后重试")
            for address, raw in snapshots:
                if read_process_memory(process, address, len(raw)) != raw:
                    raise PlayerMovementContextChangedError(
                        "球员、合同池或俱乐部名单在提交前发生变化"
                    )

            target_size = len(target_after_pointers) * 8
            if target_end + 8 <= target_capacity:
                target_storage = target_begin
                target_storage_capacity = target_capacity
            else:
                allocated_target_storage = int(kernel32.VirtualAllocEx(
                    process.handle, None, max(0x1000, target_size),
                    MEM_COMMIT_RESERVE, PAGE_READWRITE,
                ) or 0)
                if not allocated_target_storage:
                    raise RuntimeError("无法扩展目标俱乐部一线队名单")
                target_storage = allocated_target_storage
                target_storage_capacity = target_storage + target_size

            if reuse_primary_contract:
                # The empty pool cannot allocate before releasing the player's
                # old contract. Reusing that slot keeps the pool state stable.
                checked_write(contract_slot, new_contract)
            else:
                # ContractsMemoryManager<T>.AllocateNewContract copies the
                # template into the free-list head, then advances the pool.
                checked_write(pool + 0x28, struct.pack("<Q", next_free))
                checked_write(pool + 0x30, struct.pack("<I", pool_available - 1))
                checked_write(pool + 0x34, struct.pack("<I", pool_in_use + 1))
                checked_write(contract_slot, new_contract)

            target_raw = struct.pack(
                f"<{len(target_after_pointers)}Q", *target_after_pointers,
            )
            checked_write(target_storage, target_raw)
            checked_write(
                target_team + TEAM_ROSTER_BEGIN,
                struct.pack(
                    "<QQQ", target_storage, target_storage + target_size,
                    target_storage_capacity,
                ),
            )
            source_raw = (
                struct.pack(
                    f"<{len(source_after_pointers)}Q", *source_after_pointers,
                )
                if source_after_pointers else b""
            )
            checked_write(source_begin, source_raw + b"\0" * 8)
            checked_write(
                source_team + TEAM_ROSTER_END,
                struct.pack("<Q", source_end - 8),
            )

            checked_write(person + FM26_PREVIOUS_CLUB, struct.pack("<Q", source_club))
            checked_write(person + PERSON_CONTRACT, struct.pack("<Q", contract_slot))
            checked_write(
                person + FM26_JOINED_CLUB_DATE,
                struct.pack("<I", game_date_code),
            )
            checked_write(
                player + PLAYER_CURRENT_TEAM,
                struct.pack("<QQ", target_team, target_team),
            )

            if not reuse_primary_contract:
                # Release the old primary contract back to the same pool only
                # after every forward reference points to the new record.
                checked_write(primary_contract, struct.pack("<Q", next_free))
                checked_write(pool + 0x28, struct.pack("<Q", primary_contract))
                checked_write(pool + 0x30, struct.pack("<I", pool_available))
                checked_write(pool + 0x34, struct.pack("<I", pool_in_use))

            _invalidate_reader_after_write(reader)
            _, _, _, source_verified = _roster_vector(reader, source_team)
            _, _, _, target_verified = _roster_vector(reader, target_team)
            valid = (
                player not in source_verified
                and target_verified.count(player) == 1
                and reader.ptr(player + PLAYER_CURRENT_TEAM) == target_team
                and reader.ptr(player + PLAYER_REGISTERED_TEAM) == target_team
                and reader.ptr(person + FM26_PREVIOUS_CLUB) == source_club
                and reader.ptr(person + PERSON_CONTRACT) == contract_slot
                and reader.u32(person + FM26_JOINED_CLUB_DATE) == game_date_code
                and reader.ptr(contract_slot) == main_vtable
                and reader.ptr(contract_slot + 0x08) == person
                and reader.ptr(contract_slot + 0x10) == target_team
                and reader.u32(contract_slot + FM26_CONTRACT_STARTED) == game_date_code
                and reader.u32(contract_slot + FM26_CONTRACT_EXPIRY) == expiry_code
                and reader.u32(contract_slot + FM26_CONTRACT_SIGNED) == game_date_code
                and reader.u8(contract_slot + FM26_TRANSFER_STATUS) == 0
                and _pointer_matches(
                    reader, pool + 0x28,
                    0 if reuse_primary_contract else primary_contract,
                )
                and reader.u32(pool + 0x30) == pool_available
                and reader.u32(pool + 0x34) == pool_in_use
            )
            if not valid:
                raise RuntimeError("FM26 球员转会写后结构校验失败")
            if sale_credit:
                checked_write(sale_credit[0], sale_credit[2])
            if commit_transfer is not None:
                commit_transfer({
                    "player_id": int(player_id), "source_team_id": int(source_team_id),
                    "target_team_id": int(target_team_id), "transfer_fee": transfer_fee,
                    "seller_transfer_budget_after": sale_credit[3] if sale_credit else None,
                })
            committed = True
        except Exception:
            rollback_failed = False
            for address, raw in reversed(snapshots):
                try:
                    write_process_memory(process, address, raw)
                except Exception:
                    rollback_failed = True
            for address, raw in snapshots:
                if read_process_memory(process, address, len(raw)) != raw:
                    rollback_failed = True
            if allocated_target_storage and not kernel32.VirtualFreeEx(
                process.handle, ctypes.c_void_p(allocated_target_storage),
                0, MEM_RELEASE,
            ):
                rollback_failed = True
            if rollback_failed:
                error = RuntimeError("FM26 转会失败且回滚不完整，请不要保存当前存档")
                error.rollback_incomplete = True
                raise error
            raise
        finally:
            if suspended:
                kernel32.ResumeThread(handle)
            if handle:
                kernel32.CloseHandle(handle)

        return {
            "player_id": int(player_id), "mode": "transfer",
            "source_team_id": int(source_team_id),
            "target_team_id": int(target_team_id),
            "contract_address": hex(contract_slot),
            "template_contract_address": hex(template_contract),
            "thread_id": int(thread_id), "verified": bool(committed),
            "compatibility_mode": "fm26_verified_contract_pool",
        }


def move_owned_club_player(
    *, source_team_id: int, source_team_address: Any,
    source_squad_team_id: int = 0, source_squad_team_address: Any = 0,
    target_team_id: int, target_team_address: Any,
    player_id: int, mode: str, transfer_fee: int = 0,
    commit_transfer: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    mode = str(mode or "").strip().lower()
    if mode not in {"transfer", "loan"}:
        raise ValueError("球员移动方式无效")
    if int(source_team_id) == int(target_team_id):
        raise ValueError("来源俱乐部与目标俱乐部不能相同")
    if int(player_id) <= 0:
        raise ValueError("球员 ID 无效")
    if (transfer_fee or commit_transfer is not None) and mode != "transfer":
        raise ValueError("出售入账仅允许永久转会")
    sale_kwargs = {}
    if transfer_fee or commit_transfer is not None:
        sale_kwargs = {"transfer_fee": transfer_fee, "commit_transfer": commit_transfer}
    _configure_thread_api()
    pid, _path, layout = select_process_layout()
    capabilities = player_movement_capabilities(layout)
    capability = dict(capabilities.get(mode) or {})
    if not capability.get("enabled"):
        reason = str(capability.get("reason") or "当前游戏版本不支持该球员操作")
        disabled_by_setting = (
            (layout.key == "fm24" and mode == "transfer" and not FM24_TRANSFER_RUNTIME_ENABLED)
            or (layout.key == "fm24" and mode == "loan" and not FM24_LOAN_RUNTIME_ENABLED)
            or (layout.key == "fm26" and mode == "transfer" and not FM26_TRANSFER_RUNTIME_ENABLED)
            or (layout.key == "fm26" and mode == "loan" and not FM26_LOAN_RUNTIME_ENABLED)
        )
        raise PlayerMovementCapabilityError(
            reason,
            code="capability_disabled" if disabled_by_setting else "unsupported_build",
        )

    def attach_capability(result: dict[str, Any]) -> dict[str, Any]:
        result["capability"] = {"mode": mode, **capability}
        result["movement_capabilities"] = capabilities
        return result

    squad_kwargs: dict[str, Any] = {}
    if int(source_squad_team_id or 0):
        squad_kwargs = {
            "source_squad_team_id": int(source_squad_team_id),
            "source_squad_team_address": source_squad_team_address,
        }

    if layout.key == "fm24":
        if mode == "loan":
            return attach_capability(_move_fm24_owned_club_player_loan(
                pid=pid, layout=layout,
                source_team_id=source_team_id,
                source_team_address=source_team_address,
                **squad_kwargs,
                target_team_id=target_team_id,
                target_team_address=target_team_address,
                player_id=player_id,
            ))
        return attach_capability(_move_fm24_owned_club_player(
            pid=pid, layout=layout,
            source_team_id=source_team_id,
            source_team_address=source_team_address,
            **squad_kwargs,
            target_team_id=target_team_id,
            target_team_address=target_team_address,
            player_id=player_id, mode=mode, **sale_kwargs,
        ))
    if mode != "loan":
        # FM26 deliberately bypasses the former native Club call below.  That
        # path returned before FMRTE's date, reverse-reference and cleanup steps
        # had completed and reproduced a game crash.  Keep it unreachable for
        # normal permanent transfers while the pool transaction is validated.
        return attach_capability(_move_fm26_owned_club_player_fmrte(
            pid=pid, layout=layout,
            source_team_id=source_team_id,
            source_team_address=source_team_address,
            **squad_kwargs,
            target_team_id=target_team_id,
            target_team_address=target_team_address,
            player_id=player_id, mode=mode, **sale_kwargs,
        ))
    # The exact FM26 Steam 26.3.2 loan continues into the native Club bridge.
    offsets = _required_layout(layout)
    with open_process(pid, write_memory=True) as process:
        module = layout.module(process)
        if not module:
            raise RuntimeError(f"{layout.module_name} 尚未加载")
        reader = _writable_player_movement_reader(process, module.base_address, layout)
        context = _resolve_player_movement_context(
            reader, module, layout,
            source_team_id=source_team_id,
            source_team_address=source_team_address,
            source_squad_team_id=source_squad_team_id,
            source_squad_team_address=source_squad_team_address,
            target_team_id=target_team_id,
            target_team_address=target_team_address,
            player_id=player_id,
        )
        source_team, target_team = context.source_team, context.target_team
        source_club, target_club = context.source_club, context.target_club
        player, person = context.player, context.person
        expected_club_vtable = (
            int(module.base_address) + int(layout.club_vtable_rva)
        )
        source_roster_begin = int(reader.ptr(source_team + TEAM_ROSTER_BEGIN) or 0)
        source_roster_end = int(reader.ptr(source_team + TEAM_ROSTER_END) or 0)
        source_roster_size = source_roster_end - source_roster_begin
        if (
            not source_roster_begin or source_roster_size <= 0
            or source_roster_size % 8 or source_roster_size > 200 * 8
        ):
            raise PlayerMovementContextChangedError("来源俱乐部一线队名单向量校验失败")
        source_player_slots = [
            slot for slot in range(source_roster_begin, source_roster_end, 8)
            if int(reader.ptr(slot) or 0) == player
        ]
        if len(source_player_slots) != 1:
            raise PlayerMovementContextChangedError("来源俱乐部名单中的球员槽位不是唯一值")
        source_roster_slot = source_player_slots[0]
        contract_owner_interface = person + CONTRACT_OWNER_INTERFACE_OFFSET
        current_team = int(reader.ptr(player + PLAYER_CURRENT_TEAM) or 0)
        registered_team = int(reader.ptr(player + PLAYER_REGISTERED_TEAM) or 0)
        primary_contract = int(reader.ptr(person + PERSON_CONTRACT) or 0)
        contract_team = (
            int(reader.ptr(primary_contract + 0x10) or 0)
            if primary_contract else 0
        )
        if (
            not primary_contract or reader.ptr(primary_contract + 8) != person
            or not _team_belongs_to_club(reader, contract_team, source_club)
            or current_team != source_team
            or not _valid_registered_club_team(reader, registered_team)
        ):
            raise PlayerMovementContextChangedError(
                "球员当前球队、注册球队或主合同已经变化"
            )
        if mode == "loan":
            collection = int(
                reader.ptr(person + PERSON_CONTRACT_COLLECTION) or 0
            )
            existing_loan = int(reader.ptr(collection) or 0) if collection else 0
            if existing_loan:
                raise ValueError("该球员已有其他合同或租借关系，当前不能再次外租")
        pool_rva = (
            offsets["player_move_loan_contract_pool_rva"]
            if mode == "loan" else offsets["player_move_contract_pool_rva"]
        )
        pool = module.base_address + pool_rva
        allocate = module.base_address + offsets["player_move_contract_allocate_rva"]
        release = module.base_address + offsets["player_move_contract_release_rva"]
        factory_global = module.base_address + offsets["player_move_contract_factory_global_rva"]
        factory_target = module.base_address + offsets["player_move_contract_factory_target_rva"]
        main_vtable = module.base_address + offsets["player_move_main_contract_vtable_rva"]
        loan_vtable = module.base_address + offsets["loan_contract_vtable_rva"]
        loan_constructor = (
            module.base_address
            + offsets["player_move_loan_contract_constructor_rva"]
        )
        club_method = module.base_address + offsets["player_move_club_method_rva"]
        prepare_loan = module.base_address + offsets["player_move_prepare_loan_rva"]
        terminate_contract = (
            module.base_address + offsets["player_move_terminate_contract_rva"]
        )
        factory_object = int(reader.ptr(factory_global) or 0)
        entry_failures = _fm26_player_move_entry_failures(
            reader, mode=mode, module_base=module.base_address,
            offsets=offsets, expected_club_vtable=expected_club_vtable,
            person=person, primary_contract=primary_contract,
        )
        if entry_failures:
            error = RuntimeError(
                "FM26 原生球员事务入口校验失败：" + "、".join(entry_failures)
            )
            error.validation_failures = entry_failures  # type: ignore[attr-defined]
            raise error

        game_date_address = module.base_address + offsets["game_date_rva"]
        game_date_code = int(reader.u32(game_date_address) or 0)
        current_date = decode_date(game_date_code)
        if not current_date:
            raise RuntimeError("无法读取当前游戏日期")
        expiry_code = 0
        if mode != "loan":
            existing_expiry = decode_date(int(reader.u32(primary_contract + 0x48) or 0))
            expiry = (
                existing_expiry
                if existing_expiry and existing_expiry > current_date + timedelta(days=30)
                else current_date.replace(year=current_date.year + 3)
            )
            expiry_code = _encode_date(expiry)
        source_wage = int(reader.u32(primary_contract + 0x20) or 0)

        ntdll_remote = find_module(process, "ntdll.dll")
        ntdll_local = ctypes.WinDLL("ntdll", use_last_error=True)
        rtl_local = int(ctypes.cast(ntdll_local.RtlRestoreContext, ctypes.c_void_p).value or 0)
        rtl_restore = ntdll_remote.base_address + rtl_local - int(ntdll_local._handle)
        # Addresses are patched for the remote block after allocation; the first
        # pass only determines the final code size.
        placeholder = _build_stub(
            mode=mode, pool=pool, allocate=allocate, release=release,
            factory_object=factory_object, factory_target=factory_target,
            main_vtable=main_vtable, loan_vtable=loan_vtable,
            loan_constructor=loan_constructor,
            terminate_contract=terminate_contract, prepare_loan=prepare_loan,
            club_method=club_method,
            source_club=source_club, target_club=target_club,
            contract_owner_interface=contract_owner_interface,
            person=person, player=player, source_team=source_team,
            target_team=target_team,
            game_date_address=game_date_address,
            source_roster_slot=source_roster_slot,
            source_roster_end=source_roster_end,
            start_code=game_date_code, expiry_code=expiry_code,
            source_wage=source_wage, result_address=0,
            context_address=0, rtl_restore=rtl_restore,
        )

        thread_id, idle_context = _logic_thread(process)
        cave = int(kernel32.VirtualAllocEx(
            process.handle, None, REMOTE_BLOCK_SIZE,
            MEM_COMMIT_RESERVE, PAGE_EXECUTE_READWRITE,
        ) or 0)
        if not cave:
            raise RuntimeError("无法建立 FM 原生球员事务调用区")
        result_address = cave + REMOTE_RESULT_OFFSET
        context_address = cave + REMOTE_CONTEXT_OFFSET
        code = _build_stub(
            mode=mode, pool=pool, allocate=allocate, release=release,
            factory_object=factory_object, factory_target=factory_target,
            main_vtable=main_vtable, loan_vtable=loan_vtable,
            loan_constructor=loan_constructor,
            terminate_contract=terminate_contract, prepare_loan=prepare_loan,
            club_method=club_method,
            source_club=source_club, target_club=target_club,
            contract_owner_interface=contract_owner_interface,
            person=person, player=player, source_team=source_team,
            target_team=target_team,
            game_date_address=game_date_address,
            source_roster_slot=source_roster_slot,
            source_roster_end=source_roster_end,
            start_code=game_date_code, expiry_code=expiry_code,
            source_wage=source_wage, result_address=result_address,
            context_address=context_address, rtl_restore=rtl_restore,
        )
        if len(code) != len(placeholder):
            kernel32.VirtualFreeEx(process.handle, ctypes.c_void_p(cave), 0, MEM_RELEASE)
            raise RuntimeError("FM 原生球员事务代码生成失败")

        completed = False
        handle = 0
        contract = call_result = 0
        try:
            write_process_memory(process, cave, code)
            write_process_memory(process, result_address, b"\0" * 24)
            kernel32.FlushInstructionCache(process.handle, ctypes.c_void_p(cave), len(code))
            handle = _open_thread(thread_id)
            if not handle or kernel32.SuspendThread(handle) == 0xFFFFFFFF:
                raise RuntimeError("FM 游戏逻辑线程当前无法暂停，请重试")
            suspended = True
            try:
                original = _get_context(handle)
                if int(original.Rip) != int(idle_context.Rip) or int(original.Rsp) != int(idle_context.Rsp):
                    raise RuntimeError("FM 游戏逻辑线程刚刚恢复工作，请稍后重试")
                write_process_memory(process, context_address, bytes(original))
                hijacked = CONTEXT64.from_buffer_copy(bytes(original))
                hijacked.Rip = cave
                hijacked.Rsp = ((cave + REMOTE_STACK_TOP_OFFSET) & ~0xF) - 8
                write_process_memory(process, int(hijacked.Rsp), b"\0" * 8)
                if not kernel32.SetThreadContext(handle, ctypes.byref(hijacked)):
                    raise OSError(ctypes.get_last_error(), "SetThreadContext failed")
            finally:
                if suspended:
                    kernel32.ResumeThread(handle)
            deadline = time.monotonic() + 10.0
            status = 0
            while time.monotonic() < deadline:
                raw = read_process_memory(process, result_address, 24) or b""
                if len(raw) == 24:
                    status, contract, call_result = struct.unpack("<QQQ", raw)
                    if status in {1, 2, 3, 4, 5, 6, 7, 8, 9}:
                        completed = True
                        break
                time.sleep(0.01)
            if not completed:
                raise RuntimeError("FM 原生球员事务执行超时；请不要立即重复操作")
            if status == 3:
                raise RuntimeError(
                    "FM 已终止旧合同，但目标俱乐部拒绝了球员事务；"
                    "请立即检查游戏状态且不要保存"
                )
            if status == 4:
                raise RuntimeError(
                    "FM 已完成目标俱乐部事务，但来源俱乐部名单在执行期间发生变化；"
                    "为避免错误写入已停止清理，请不要保存"
                )
            if status == 6:
                raise RuntimeError(
                    "FM26 租借合同已通过日期校验，但目标 Club 拒绝了事务；"
                    "临时合同已释放"
                )
            if status == 7:
                actual_rva = int(call_result) - int(module.base_address) if call_result else 0
                raise RuntimeError(
                    "FM26 租借合同工厂返回了非预期合同类型 "
                    f"(vtable RVA {hex(actual_rva)})，临时合同已释放"
                )
            if status == 8:
                raise RuntimeError("FM26 租借合同工厂返回了非预期 Person 所有者，临时合同已释放")
            if status == 9:
                raise RuntimeError("FM26 租借合同工厂返回了非预期目标 Team，临时合同已释放")
            if status != 1 or call_result != 1 or not contract:
                raise RuntimeError("FM 拒绝了本次球员转会或租借事务")
            time.sleep(0.05)
        finally:
            if handle:
                kernel32.CloseHandle(handle)
            if completed:
                kernel32.VirtualFreeEx(process.handle, ctypes.c_void_p(cave), 0, MEM_RELEASE)

        _invalidate_reader_after_write(reader)
        source_after = {
            int(row.get("id") or 0) for row in reader.roster(source_team)
        }
        target_after = {
            int(row.get("id") or 0) for row in reader.roster(target_team)
        }
        if mode == "transfer":
            valid = (
                int(player_id) not in source_after and int(player_id) in target_after
                and reader.ptr(player + PLAYER_CURRENT_TEAM) == target_team
                and reader.ptr(person + PERSON_CONTRACT) == int(contract)
                and reader.ptr(int(contract) + 0x08) == person
                and reader.ptr(int(contract) + 0x10) == target_team
            )
        else:
            other_contracts = int(reader.ptr(person + PERSON_CONTRACT_COLLECTION) or 0)
            loan_expiry = decode_date(int(reader.u32(int(contract) + 0x48) or 0))
            valid = (
                int(player_id) in source_after and int(player_id) in target_after
                and other_contracts and reader.ptr(other_contracts) == int(contract)
                and reader.ptr(player + PLAYER_CURRENT_TEAM) == target_team
                and reader.ptr(player + PLAYER_REGISTERED_TEAM) == registered_team
                and reader.ptr(person + PERSON_CONTRACT) == primary_contract
                and _team_belongs_to_club(
                    reader, int(reader.ptr(primary_contract + 0x10) or 0), source_club,
                )
                and reader.ptr(int(contract)) == loan_vtable
                and reader.ptr(int(contract) + 0x08) == person
                and reader.ptr(int(contract) + 0x10) == target_team
                and reader.ptr(int(contract) + 0x18) == target_team
                and reader.u32(int(contract) + 0x44) == game_date_code
                and reader.u32(int(contract) + 0x4C) == game_date_code
                and loan_expiry is not None
                and loan_expiry > current_date
            )
        if not valid:
            raise RuntimeError("FM 已执行事务，但球员合同或一线队名单回读不一致；请先检查游戏状态")
        return attach_capability({
            "player_id": int(player_id), "mode": mode,
            "source_team_id": int(source_team_id),
            "target_team_id": int(target_team_id),
            "contract_address": hex(int(contract)),
            "thread_id": int(thread_id),
            "verified": True,
        })


__all__ = [
    "appoint_free_agent_manager", "extend_owned_club_manager_contract",
    "replace_owned_club_manager",
    "describe_player_movement_error", "player_movement_capabilities",
    "PlayerMovementOperationError", "PlayerMovementCapabilityError",
    "PlayerMovementContextChangedError",
    "move_owned_club_player", "release_owned_club_player",
    "release_owned_club_staff", "set_owned_club_player_transfer_listed",
    "move_owned_club_staff",
]
