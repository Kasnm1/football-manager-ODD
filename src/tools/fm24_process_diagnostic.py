from __future__ import annotations

import ctypes
import base64
import hashlib
import json
import mmap
import os
import platform
import struct
import threading
import tkinter as tk
import zlib
from ctypes import wintypes
from dataclasses import fields as dataclass_fields
from datetime import datetime
from functools import lru_cache
from pathlib import Path, PureWindowsPath
from tkinter.scrolledtext import ScrolledText


APP_TITLE = "FMODD Diagnostic Tool"
DETECTING_MESSAGE = "Detecting... This may take up to 2 minutes."
COMPLETION_MESSAGE = "Detection complete. Please send the report to the developer."
KNOWN_BUILDS = {
    "E1059EEE82FA7832188831521A3FA633EC3260DD98D03DA48B16661147E3AB48": "FM2024 Steam 24.4.2+2081827",
    "1473E5C3A778CB255A30C7A43D8346E27B031B47C42E93533097E049C110D58E": "FM2024 Steam 24.4.0.0（Beta 适配）",
    "ED5D0F9C3D06194FA5D19E5A2C14EF8F9C6AB8A9FF7E77220594E8A600A75F5F": "FM2024 Steam 24.4.1.0（Beta 适配）",
    "33E8A2E48C4986F2A15E11C4C93ACA1199029D66B19BD765403EB22DEA265378": "FM2024 Epic",
    "3653C97F9CCEC2BE28EDC4FAAE67304B5B6C26733F2F07DEA3E7C591D3B9FF73": "FM2026 Steam",
}
KNOWN_MEMORY_BUILDS = {
    (0x67FF9A0A, 0x1CD9F000): "FM2024 XGP（Beta 适配）",
    (0x6A229DA9, 0x1EDF4000): "FM2026 XGP 0.9.9957.0（Beta 适配）",
}

TH32CS_SNAPPROCESS = 0x00000002
PROCESS_VM_READ = 0x0010
PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
version = ctypes.WinDLL("version", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * 260),
    ]


class VS_FIXEDFILEINFO(ctypes.Structure):
    _fields_ = [
        ("dwSignature", wintypes.DWORD),
        ("dwStrucVersion", wintypes.DWORD),
        ("dwFileVersionMS", wintypes.DWORD),
        ("dwFileVersionLS", wintypes.DWORD),
        ("dwProductVersionMS", wintypes.DWORD),
        ("dwProductVersionLS", wintypes.DWORD),
        ("dwFileFlagsMask", wintypes.DWORD),
        ("dwFileFlags", wintypes.DWORD),
        ("dwFileOS", wintypes.DWORD),
        ("dwFileType", wintypes.DWORD),
        ("dwFileSubtype", wintypes.DWORD),
        ("dwFileDateMS", wintypes.DWORD),
        ("dwFileDateLS", wintypes.DWORD),
    ]


kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
kernel32.Process32FirstW.restype = wintypes.BOOL
kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
kernel32.Process32NextW.restype = wintypes.BOOL
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL
kernel32.QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD),
]
kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
version.GetFileVersionInfoSizeW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
version.GetFileVersionInfoSizeW.restype = wintypes.DWORD
version.GetFileVersionInfoW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID]
version.GetFileVersionInfoW.restype = wintypes.BOOL
version.VerQueryValueW.argtypes = [wintypes.LPCVOID, wintypes.LPCWSTR, ctypes.POINTER(wintypes.LPVOID), ctypes.POINTER(wintypes.UINT)]
version.VerQueryValueW.restype = wintypes.BOOL


def windows_error(code: int) -> str:
    try:
        return ctypes.FormatError(code).strip() or f"Windows 错误 {code}"
    except Exception:
        return f"Windows 错误 {code}"


def fm_processes() -> list[tuple[int, int]]:
    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snapshot == INVALID_HANDLE_VALUE:
        code = ctypes.get_last_error()
        raise OSError(code, windows_error(code))
    found: list[tuple[int, int]] = []
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            if entry.szExeFile.casefold() == "fm.exe":
                found.append((int(entry.th32ProcessID), int(entry.cntThreads)))
            ok = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    return found


def open_process(pid: int, access: int) -> tuple[int | None, str | None]:
    ctypes.set_last_error(0)
    handle = kernel32.OpenProcess(access, False, pid)
    if handle:
        return int(handle), None
    code = ctypes.get_last_error()
    return None, f"{code}（{windows_error(code)}）"


def process_path(pid: int) -> tuple[str | None, str | None]:
    access = PROCESS_QUERY_INFORMATION | PROCESS_QUERY_LIMITED_INFORMATION
    handle, error = open_process(pid, access)
    if not handle:
        return None, error
    try:
        size = wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        ctypes.set_last_error(0)
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            code = ctypes.get_last_error()
            return None, f"{code}（{windows_error(code)}）"
        return buffer.value, None
    finally:
        kernel32.CloseHandle(handle)


def can_read_memory(pid: int) -> tuple[bool, str | None]:
    access = PROCESS_QUERY_INFORMATION | PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_VM_READ
    handle, error = open_process(pid, access)
    if not handle:
        return False, error
    kernel32.CloseHandle(handle)
    return True, None


def process_module_pe_identity(pid: int, module_name: str = "fm.exe") -> tuple[int, int] | None:
    try:
        from fm_collector.win32 import (
            find_module, open_process as open_remote_process, read_process_memory,
        )

        with open_remote_process(pid) as process:
            module = find_module(process, module_name)
            if module is None:
                return None
            header = read_process_memory(process, module.base_address, 0x1000)
        if not header or header[:2] != b"MZ":
            return None
        pe_offset = struct.unpack_from("<I", header, 0x3C)[0]
        if header[pe_offset:pe_offset + 4] != b"PE\0\0":
            return None
        return (
            struct.unpack_from("<I", header, pe_offset + 8)[0],
            struct.unpack_from("<I", header, pe_offset + 24 + 56)[0],
        )
    except (OSError, struct.error):
        return None


def file_version(path: str) -> str:
    ignored = wintypes.DWORD()
    size = version.GetFileVersionInfoSizeW(path, ctypes.byref(ignored))
    if not size:
        return "无法读取"
    buffer = ctypes.create_string_buffer(size)
    if not version.GetFileVersionInfoW(path, 0, size, buffer):
        return "无法读取"
    value = wintypes.LPVOID()
    length = wintypes.UINT()
    if not version.VerQueryValueW(buffer, "\\", ctypes.byref(value), ctypes.byref(length)):
        return "无法读取"
    info = ctypes.cast(value, ctypes.POINTER(VS_FIXEDFILEINFO)).contents
    return ".".join(str(part) for part in (
        info.dwFileVersionMS >> 16,
        info.dwFileVersionMS & 0xFFFF,
        info.dwFileVersionLS >> 16,
        info.dwFileVersionLS & 0xFFFF,
    ))


@lru_cache(maxsize=8)
def _sha256_file(path: str, size: int, modified_ns: int) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def sha256(path: str) -> str:
    resolved = Path(path).resolve()
    details = resolved.stat()
    return _sha256_file(str(resolved), details.st_size, details.st_mtime_ns)


def distribution_hint(path: str) -> str:
    normalized = path.casefold().replace("/", "\\")
    if "\\steamapps\\" in normalized:
        return "Steam 目录"
    if "\\epic games\\" in normalized:
        return "Epic 目录"
    if "\\xboxgames\\" in normalized or "\\windowsapps\\" in normalized:
        return "Xbox / Microsoft Store 目录"
    if normalized.endswith("\\content\\fm.exe"):
        return "Xbox / Microsoft Store Content 目录"
    return "未知目录（可能是离线版、自定义安装或其他平台）"


def pe_details(path: str) -> dict[str, str]:
    try:
        with open(path, "rb") as source:
            header = source.read(0x1000)
        if len(header) < 0x40 or header[:2] != b"MZ":
            raise ValueError("不是有效的 PE 文件")
        pe_offset = struct.unpack_from("<I", header, 0x3C)[0]
        if pe_offset + 0x80 > len(header):
            with open(path, "rb") as source:
                source.seek(pe_offset)
                pe = source.read(0x100)
            base = 0
        else:
            pe = header
            base = pe_offset
        if pe[base:base + 4] != b"PE\0\0":
            raise ValueError("PE 签名无效")
        machine, _sections, timestamp = struct.unpack_from("<HHI", pe, base + 4)
        optional = base + 24
        image_size = struct.unpack_from("<I", pe, optional + 56)[0]
        return {
            "machine": f"0x{machine:04X}",
            "timestamp": f"0x{timestamp:08X}（{datetime.fromtimestamp(timestamp).isoformat(sep=' ', timespec='seconds')}）",
            "image_size": f"0x{image_size:X}（{image_size} 字节）",
        }
    except (OSError, ValueError, struct.error) as error:
        return {"machine": "无法读取", "timestamp": f"无法读取（{error}）", "image_size": "无法读取"}


FM24_RTTI_NAMES = {
    "赛程对象": ".?AVFIXTURE@sicomps@@",
    "赛果对象": ".?AVFIXTURE_RESULT@sicomps@@",
    "持久赛果对象": ".?AVBASIC_SCORELINE@SCORELINE@db@@",
    "球队": ".?AVTEAM@db@@",
    "国家队": ".?AVNATIONAL_TEAM@db@@",
    "国家容器": ".?AVNATIONAL_TEAM_CONTAINER@db@@",
    "俱乐部": ".?AVCLUB@db@@",
    "赛事": ".?AVCOMP@db@@",
    "球员": ".?AVACTUAL_PLAYER@db@@",
    "球员/职员": ".?AVACTUAL_PLAYER_AND_NON_PLAYER@db@@",
    "职员": ".?AVACTUAL_NON_PLAYER@db@@",
    "人类经理": ".?AVHUMAN_NON_PLAYER@db@@",
    "比赛会话管理器": ".?AVMATCH_SESSION_MANAGER@fmmatchviewer@@",
}

FM24_SAVE_PROVIDER_RTTI = ".?AVGAME_SAVE_PROVIDER_LOCAL@sidistribution@@"
FM24_MANAGER_PERSON_OFFSET = 0x450
FM24_ENTITY_UID_OFFSET = 0x0C
FM24_MANAGER_CONTRACT_OFFSET = 0xC8
FM24_CONTRACT_PERSON_OFFSET = 0x08
FM24_CONTRACT_TEAM_OFFSET = 0x10
FM24_TEAM_MANAGER_OFFSET = 0x80
FM24_LOCAL_SAVE_PATH_OFFSET = 0xB0
FM24_LOCAL_SAVE_LABEL_OFFSET = 0x1F0
FM24_SCAN_BLOCK_BYTES = 8 * 1024 * 1024
FM24_SAVE_PROVIDER_REGION_LIMIT = 8 * 1024 * 1024
FM24_MANAGER_REGION_LIMIT = 64 * 1024 * 1024
FM24_DEEP_DIAGNOSTIC_REGION_LIMIT = 512 * 1024 * 1024

FM24_GAME_DATE_PATTERN = (
    0x44, 0x8B, 0x1D, None, None, None, None,
    0x48, 0xFF, None, 0x31, None, 0xEB,
)
FM24_COMPAT_FINGERPRINT_A = (
    0x41, 0x0F, 0xBE, 0xBC, 0x0D, None, None, 0x00, 0x00,
)
FM24_COMPAT_FINGERPRINT_B = (
    0x0F, 0x2E, None, 0x0F, 0x83, None, None, 0x00, 0x00,
    0x48, 0x8B, None, None, None, 0x00, 0x00,
    0x48, 0x85, 0xFF, 0x0F, 0x84,
)
FM26_GAME_DATE_PATTERN = (
    0x0F, 0xB7, 0x03, 0x25, 0xFF, 0x01, 0x00, 0x00,
    0x66, 0x83, 0xF8, 0x01, 0x75, 0x08, 0x8B, 0x05,
    None, None, None, None, 0x89, 0x03,
)
FM26_COMPAT_FINGERPRINT_A = (
    0x44, 0x0F, 0xBE, 0xB4, 0x0B, None, None, 0x00, 0x00, 0xE8,
)
FM26_COMPAT_FINGERPRINT_B = (
    0x44, 0x88, 0x8E, 0x62, 0x02, 0x00, 0x00,
    0x44, 0x88, 0x9E, 0x60, 0x02, 0x00, 0x00,
    0x44, 0x88, 0x96, 0x61, 0x02, 0x00, 0x00,
)
FM26_SAVE_ROOT_PATTERN = (
    0x48, 0x8B, 0x35, None, None, None, None, 0x48, 0x85, 0xF6,
    0x74, None, 0x48, 0x8D, 0x05, None, None, None, None,
    0x48, 0x89, 0x86, 0x00, 0x02, 0x00, 0x00,
)
# The default Club Culture constructor has the same stable instruction shape
# in the verified FM24 and FM26 builds; relocations and call targets are masked.
CLUB_CULTURE_CONSTRUCTOR_PATTERN = (
    0x55, 0x56, 0x57, 0x48, 0x83, 0xEC, 0x40, 0x48,
    0x8D, 0x6C, 0x24, 0x40, 0x48, 0xC7, 0x45, 0xF8,
    0xFE, 0xFF, 0xFF, 0xFF, 0x48, 0x89, 0xCE, 0x48,
    0x8D, 0x05, None, None, None, None, 0x48, 0x89,
    0x01, 0xE8, None, None, None, None, 0x48, 0x89, 0xC1,
    0x48, 0x83, 0xC1, 0x08, 0x48, 0xC7, 0x45, 0xF0,
    0x00, 0x00, 0x00, 0x00,
)
# FM24 additionally needs the native initialization entry after construction.
FM24_CLUB_CULTURE_INITIALIZE_PATTERN = (
    0x41, 0x57, 0x41, 0x56, 0x41, 0x55, 0x41, 0x54,
    0x56, 0x57, 0x55, 0x53, 0x48, 0x83, 0xEC, 0x28,
    0x48, 0x89, 0xD7, 0x49, 0x89, 0xCF, 0x0F, 0xB6,
    0x49, 0x2C, 0xE8, None, None, None, None, 0x84,
    0xC0, 0x74, None, 0x41, 0xC6, 0x47, 0x3C, 0x00,
    0xE9, None, None, None, None, 0x41, 0xF6, 0x47,
    0x3D, 0x04, 0x0F, 0x85, None, None, None, None,
)
FM26_SAVEGAME_ID_OFFSET = 0xB8
FM26_SESSION_NONCE_OFFSET = 0xBC

CODE_REPORT_SCHEMA = "ODD-C1"
CODE_REPORT_BEGIN = f"-----BEGIN {CODE_REPORT_SCHEMA}-----"
CODE_REPORT_END = f"-----END {CODE_REPORT_SCHEMA}-----"

# These fields cover the read-only roots and object identities used most often
# while establishing an FMODD session.  Keep this list deliberately narrower
# than every GameLayout RVA: a readable address is diagnostic evidence, not a
# capability gate or proof that a write/Hook is safe.
CORE_RUNTIME_RVA_FIELDS = (
    "game_date_rva",
    "database_root_probe_rva",
    "human_manager_root_probe_rva",
    "savegame_root_rvas",
    "fixture_pool_rva",
    "match_session_pointer_rva",
    "play_fixture_manager_pointer_rva",
    "fixture_vtable_rva",
    "fixture_result_vtable_rva",
    "season_result_vtable_rva",
    "team_vtable_rva",
    "national_team_vtable_rva",
    "competition_vtable_rva",
    "actual_player_vtable_rvas",
    "human_manager_vtable_rvas",
    "staff_person_vtable_rva",
)
CLUB_RUNTIME_RVA_FIELDS = (
    "club_vtable_rva",
    "club_culture_vtable_rva",
    "club_culture_constructor_rva",
    "club_culture_initialize_rva",
    "club_affiliation_manager_rva",
    "club_affiliation_create_rva",
    "stadium_vtable_rva",
    "player_move_club_method_rva",
    "club_policy_departure_pattern_rva",
    "club_policy_salary_pattern_rva",
    "club_policy_salary_promise_pattern_rva",
)
CRITICAL_RUNTIME_RVA_FIELDS = CORE_RUNTIME_RVA_FIELDS + CLUB_RUNTIME_RVA_FIELDS
CRITICAL_POINTER_RVA_FIELDS = {
    "savegame_root_rvas",
    "match_session_pointer_rva",
    "play_fixture_manager_pointer_rva",
}


def _layout_field_code(name: str) -> str:
    """Return a stable opaque label for one GameLayout field."""
    return f"F{zlib.crc32(name.encode('utf-8')) & 0xFFFFFFFF:08X}"


def _json_safe_layout_value(value: object) -> object:
    if isinstance(value, tuple):
        return [_json_safe_layout_value(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return repr(value)


def layout_code_manifest(layout: object) -> dict[str, object]:
    """Serialize every current GameLayout field behind stable field codes."""
    codebook: dict[str, str] = {}
    values: dict[str, object] = {}
    for field in dataclass_fields(layout):
        code = _layout_field_code(field.name)
        if code in codebook and codebook[code] != field.name:
            raise RuntimeError(f"layout code collision: {code}")
        codebook[code] = field.name
        values[code] = _json_safe_layout_value(getattr(layout, field.name))
    return {
        "layout": str(getattr(layout, "display_name", "") or ""),
        "generation": str(getattr(layout, "key", "") or ""),
        "distribution": str(getattr(layout, "distribution", "") or ""),
        "codebook": codebook,
        "values": values,
    }


def _configured_rvas(layout: object, field_name: str) -> list[int]:
    value = getattr(layout, field_name, None)
    if value is None:
        return []
    if isinstance(value, (tuple, list)):
        return [int(item) for item in value if item is not None]
    return [int(value)]


def runtime_rva_probe_manifest(pid: int, layout: object) -> dict[str, object]:
    """Probe critical configured RVAs without retaining process addresses."""
    from fm_collector.win32 import (
        find_module, open_process as open_remote_process, read_process_memory,
    )

    module_name = str(getattr(layout, "module_name", "") or "")
    with open_remote_process(pid) as process:
        module = find_module(process, module_name)
        if module is None:
            return {"module": _layout_field_code("module_name"), "state": "missing"}

        module_base = int(module.base_address)
        module_size = int(module.size)
        rows: list[dict[str, object]] = []
        for field_name in CRITICAL_RUNTIME_RVA_FIELDS:
            values = _configured_rvas(layout, field_name)
            samples: list[dict[str, object]] = []
            if not values:
                samples.append({"state": "unset"})
            for rva in values:
                sample: dict[str, object] = {"rva": rva}
                if rva <= 0 or rva >= module_size:
                    sample["state"] = "outside_module"
                    samples.append(sample)
                    continue
                read_size = 8 if field_name in CRITICAL_POINTER_RVA_FIELDS else (
                    4 if field_name == "game_date_rva" else 1
                )
                try:
                    raw = read_process_memory(process, module_base + rva, read_size)
                except OSError:
                    raw = None
                if raw is None or len(raw) != read_size:
                    sample["state"] = "unreadable"
                    samples.append(sample)
                    continue
                sample["state"] = "readable"
                if field_name == "game_date_rva":
                    sample["value_state"] = (
                        "nonzero" if struct.unpack("<I", raw)[0] else "zero"
                    )
                elif field_name in CRITICAL_POINTER_RVA_FIELDS:
                    pointer = struct.unpack("<Q", raw)[0]
                    if not pointer:
                        sample["target_state"] = "null"
                    else:
                        try:
                            target = read_process_memory(process, pointer, 1)
                        except OSError:
                            target = None
                        sample["target_state"] = (
                            "readable" if target is not None and len(target) == 1
                            else "unreadable"
                        )
                samples.append(sample)
            rows.append({
                "field": _layout_field_code(field_name),
                "group": "club" if field_name in CLUB_RUNTIME_RVA_FIELDS else "core",
                "samples": samples,
            })
    return {
        "module": _layout_field_code("module_name"),
        "module_size": module_size,
        "state": "probed",
        "fields": rows,
    }


def summarize_person_table_header(
    raw: bytes | None, *, maximum: int = 1_000_000,
) -> dict[str, object]:
    """Classify a native Person vector header without retaining addresses."""
    limit = max(0, int(maximum))
    if raw is None or len(raw) != 0x18:
        return {
            "state": "unreadable",
            "entry_limit": limit,
            "failure_codes": ["header_unreadable"],
        }

    begin, end, capacity = struct.unpack("<QQQ", raw)
    end_after_begin = end >= begin
    capacity_after_end = capacity >= end
    size_aligned = (end - begin) % 8 == 0
    capacity_aligned = (capacity - begin) % 8 == 0
    raw_entry_count = (end - begin) // 8
    entry_count = raw_entry_count if end_after_begin and size_aligned else None
    capacity_count = (
        (capacity - begin) // 8
        if capacity >= begin and capacity_aligned else None
    )
    checks = {
        "begin_nonzero": begin != 0,
        "end_after_begin": end_after_begin,
        "capacity_after_end": capacity_after_end,
        "size_aligned_8": size_aligned,
        "capacity_aligned_8": capacity_aligned,
        "entry_count_within_limit": raw_entry_count <= limit,
    }
    failure_codes = [name for name, passed in checks.items() if not passed]
    result: dict[str, object] = {
        "state": "valid" if not failure_codes else "invalid",
        "entry_limit": limit,
        "checks": checks,
        "failure_codes": failure_codes,
    }
    if entry_count is not None:
        result["entry_count"] = entry_count
    if capacity_count is not None:
        result["capacity_count"] = capacity_count
    return result


def runtime_person_table_probe(pid: int, layout: object) -> dict[str, object]:
    """Reproduce the production Person-table header checks without row reads."""
    from fm_collector.win32 import open_process as open_remote_process
    from tools.database_index import (
        DATABASE_TABLE_SLOTS, TABLE_COUNT_LIMITS, _resolve_relative_global,
    )
    from tools.initial_data_audit import Reader

    result: dict[str, object] = {
        "probe": "DB-P1",
        "mode": "read_only_header_only",
        "table": "person",
    }
    try:
        with open_remote_process(pid) as process:
            module = layout.module(process)
            if module is None:
                return {
                    **result, "state": "unavailable", "stage": "module",
                    "failure_codes": ["module_missing"],
                }
            reader = Reader(process, int(module.base_address), layout)
            try:
                reader.module = module
            except AttributeError:
                pass
            root_slot = _resolve_relative_global(
                reader,
                tuple(getattr(layout, "database_root_pattern", ()) or ()),
                getattr(layout, "database_root_rel32_offset", None),
                getattr(layout, "database_root_instruction_size", None),
                getattr(layout, "database_root_probe_rva", None),
            )
            if not root_slot:
                return {
                    **result, "state": "unavailable", "stage": "database_root",
                    "failure_codes": ["root_unresolved_or_non_unique"],
                }

            pointer_raw = reader.bytes(
                int(root_slot) + int(DATABASE_TABLE_SLOTS["person"]), 8,
            )
            if pointer_raw is None or len(pointer_raw) != 8:
                return {
                    **result, "state": "unreadable", "stage": "table_pointer",
                    "root_resolved": True,
                    "failure_codes": ["table_pointer_unreadable"],
                }
            table_object = struct.unpack("<Q", pointer_raw)[0]
            if not table_object:
                return {
                    **result, "state": "unreadable", "stage": "table_pointer",
                    "root_resolved": True,
                    "failure_codes": ["table_pointer_null"],
                }

            vector_raw = reader.bytes(int(table_object) + 0x80, 8)
            if vector_raw is None or len(vector_raw) != 8:
                return {
                    **result, "state": "unreadable", "stage": "vector_pointer",
                    "root_resolved": True, "table_pointer_nonzero": True,
                    "failure_codes": ["vector_pointer_unreadable"],
                }
            vector = struct.unpack("<Q", vector_raw)[0]
            if not vector:
                return {
                    **result, "state": "unreadable", "stage": "vector_pointer",
                    "root_resolved": True, "table_pointer_nonzero": True,
                    "failure_codes": ["vector_pointer_null"],
                }

            samples = [reader.bytes(int(vector), 0x18) for _index in range(2)]
            summaries = [
                summarize_person_table_header(
                    sample, maximum=int(TABLE_COUNT_LIMITS["person"]),
                )
                for sample in samples
            ]
            stable = samples[0] == samples[1]
            if not stable:
                return {
                    **result, "state": "changing", "stage": "header",
                    "root_resolved": True, "table_pointer_nonzero": True,
                    "vector_pointer_nonzero": True, "header_stable": False,
                    "observations": summaries,
                    "failure_codes": ["header_changed_between_reads"],
                }
            return {
                **result, **summaries[-1], "stage": "header",
                "root_resolved": True, "table_pointer_nonzero": True,
                "vector_pointer_nonzero": True, "header_stable": True,
            }
    except Exception as error:
        return {
            **result, "state": "error", "stage": "probe",
            "error_type": type(error).__name__,
            "failure_codes": ["probe_exception"],
        }


def render_person_table_probe(probe: dict[str, object]) -> str:
    """Render the address-free DB-P1 result in the readable support report."""
    parts = [
        f"状态={probe.get('state') or 'unknown'}",
        f"阶段={probe.get('stage') or 'unknown'}",
    ]
    if "header_stable" in probe:
        parts.append(f"表头稳定={'是' if probe.get('header_stable') else '否'}")
    if "entry_count" in probe:
        parts.append(f"人物指针数={int(probe['entry_count'])}")
    if "capacity_count" in probe:
        parts.append(f"容量槽数={int(probe['capacity_count'])}")
    failures = [str(value) for value in probe.get("failure_codes", [])]
    if failures:
        parts.append("失败条件=" + ",".join(failures))
    if probe.get("error_type"):
        parts.append(f"异常类型={probe['error_type']}")
    return "  检测项 DB-P1：" + "；".join(parts)


def _runtime_layout_for_process(
    pid: int, process_path_value: str | None, executable_sha256: str | None,
    memory_identity: tuple[int, int] | None,
    plugin_memory_identity: tuple[int, int] | None = None,
) -> object | None:
    """Resolve the exact recognized layout, including dynamic XGP identities."""
    from fm_collector.win32 import find_module, open_process as open_remote_process
    from tools.game_layout import (
        FM24_XGP_IMAGE_SIZE, FM24_XGP_PE_TIMESTAMP,
        FM26_XGP_IMAGE_SIZE, FM26_XGP_PE_TIMESTAMP,
        LAYOUT_BY_HASH, resolve_fm24_xgp_layout, resolve_fm26_xgp_layout,
    )

    layout = LAYOUT_BY_HASH.get(executable_sha256 or "")
    if layout is not None:
        return layout
    if memory_identity == (FM24_XGP_PE_TIMESTAMP, FM24_XGP_IMAGE_SIZE):
        with open_remote_process(pid) as process:
            module = find_module(process, "fm.exe")
            if module is None:
                return None
            return resolve_fm24_xgp_layout(
                pid, int(module.base_address), int(module.size),
            )
    if (
        plugin_memory_identity == (FM26_XGP_PE_TIMESTAMP, FM26_XGP_IMAGE_SIZE)
        or memory_identity == (FM26_XGP_PE_TIMESTAMP, FM26_XGP_IMAGE_SIZE)
    ):
        with open_remote_process(pid) as process:
            module = find_module(process, "game_plugin.dll")
            if module is None or not process_path_value:
                return None
            return resolve_fm26_xgp_layout(
                pid, str(process_path_value), int(module.base_address), int(module.size),
                str(module.path),
            )
    return None


def collect_layout_code_manifests() -> list[dict[str, object]]:
    """Collect configured capability maps without exposing paths or process IDs."""
    manifests: list[dict[str, object]] = []
    for ordinal, (pid, _thread_count) in enumerate(fm_processes(), 1):
        path, _error = process_path(pid)
        executable_sha256 = None
        if path:
            try:
                executable_sha256 = sha256(path)
            except OSError:
                executable_sha256 = None
        memory_identity = process_module_pe_identity(pid)
        plugin_memory_identity = process_module_pe_identity(pid, "game_plugin.dll")
        layout_error = None
        try:
            layout = _runtime_layout_for_process(
                pid, path, executable_sha256, memory_identity,
                plugin_memory_identity,
            )
        except Exception as error:
            layout = None
            layout_error = type(error).__name__
        row: dict[str, object] = {"process": ordinal, "recognized": layout is not None}
        if path:
            row["executable_sha256"] = executable_sha256
        if memory_identity:
            row["memory_pe"] = {
                "timestamp": memory_identity[0], "image_size": memory_identity[1],
            }
        if plugin_memory_identity:
            row["plugin_memory_pe"] = {
                "timestamp": plugin_memory_identity[0],
                "image_size": plugin_memory_identity[1],
            }
        if layout is not None:
            row["manifest"] = layout_code_manifest(layout)
            try:
                row["address_probes"] = runtime_rva_probe_manifest(pid, layout)
            except Exception as error:
                row["address_probe_error"] = type(error).__name__
            row["person_table_probe"] = runtime_person_table_probe(pid, layout)
        if layout_error:
            row["layout_error"] = layout_error
        manifests.append(row)
    return manifests


def build_code_report(
    readable_report: str, *, manifests: list[dict[str, object]] | None = None,
) -> str:
    """Build a reversible, copy-safe support bundle with no readable raw labels."""
    payload = {
        "schema": CODE_REPORT_SCHEMA,
        "report": str(readable_report),
        "layouts": collect_layout_code_manifests() if manifests is None else manifests,
    }
    raw = json.dumps(
        payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True,
    ).encode("utf-8")
    encoded = base64.b85encode(zlib.compress(raw, level=9)).decode("ascii")
    wrapped = "\n".join(encoded[index:index + 96] for index in range(0, len(encoded), 96))
    return f"{CODE_REPORT_BEGIN}\n{wrapped}\n{CODE_REPORT_END}"


def decode_code_report(bundle: str) -> dict[str, object]:
    """Decode an ODD-C1 report received from a tester."""
    text = str(bundle).strip()
    if not text.startswith(CODE_REPORT_BEGIN) or not text.endswith(CODE_REPORT_END):
        raise ValueError("not an ODD-C1 code report")
    body = text[len(CODE_REPORT_BEGIN):-len(CODE_REPORT_END)]
    encoded = "".join(body.split())
    try:
        payload = json.loads(zlib.decompress(base64.b85decode(encoded)).decode("utf-8"))
    except (ValueError, TypeError, zlib.error, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("ODD-C1 code report is damaged") from error
    if payload.get("schema") != CODE_REPORT_SCHEMA:
        raise ValueError("unsupported code report schema")
    return payload


def _memory_pattern_occurrences(image: bytes, pattern: tuple[int | None, ...]) -> list[int]:
    fixed = [(index, value) for index, value in enumerate(pattern) if value is not None]
    if not fixed:
        return []
    anchor_index, anchor_value = fixed[0]
    needle = bytes((int(anchor_value),))
    rows: list[int] = []
    cursor = 0
    while True:
        position = image.find(needle, cursor)
        if position < 0:
            return rows
        start = position - anchor_index
        if 0 <= start <= len(image) - len(pattern) and all(
            expected is None or image[start + index] == expected
            for index, expected in enumerate(pattern)
        ):
            rows.append(start)
        cursor = position + 1


def _memory_occurrences(image: bytes, needle: bytes):
    cursor = 0
    while True:
        cursor = image.find(needle, cursor)
        if cursor < 0:
            return
        yield cursor
        cursor += 1


def _memory_rtti_vtables(image: bytes, module_base: int, name: str) -> list[int]:
    vtables: set[int] = set()
    for name_rva in _memory_occurrences(image, name.encode("ascii") + b"\0"):
        type_rva = name_rva - 16
        if type_rva < 0:
            continue
        for reference_rva in _memory_occurrences(image, struct.pack("<I", type_rva)):
            locator_rva = reference_rva - 12
            if locator_rva < 0 or locator_rva + 24 > len(image):
                continue
            signature, _offset, _cd, found_type, _class, self_rva = struct.unpack_from(
                "<IIIIII", image, locator_rva,
            )
            if signature != 1 or found_type != type_rva or self_rva != locator_rva:
                continue
            pointer = struct.pack("<Q", module_base + locator_rva)
            for pointer_rva in _memory_occurrences(image, pointer):
                vtables.add(pointer_rva + 8)
    return sorted(vtables)


def _runtime_exact(read, process: object, address: int, size: int) -> bytes | None:
    data = read(process, int(address), int(size))
    return data if data and len(data) == size else None


def _runtime_ptr(read, process: object, address: int) -> int:
    raw = _runtime_exact(read, process, address, 8)
    return int(struct.unpack("<Q", raw)[0]) if raw else 0


def _runtime_u32(read, process: object, address: int) -> int:
    raw = _runtime_exact(read, process, address, 4)
    return int(struct.unpack("<I", raw)[0]) if raw else 0


def _valid_runtime_text(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = value.strip()
    if not 1 <= len(cleaned) <= 160 or any(ord(char) < 32 for char in cleaned):
        return None
    return cleaned


def _runtime_fm_string(read, process: object, address: int) -> str | None:
    pointer = _runtime_ptr(read, process, address)
    length = _runtime_u32(read, process, pointer) if pointer else 0
    if not 0 < length <= 256:
        return None
    raw = _runtime_exact(read, process, pointer + 4, length)
    if not raw:
        return None
    try:
        return _valid_runtime_text(raw.decode("utf-8"))
    except UnicodeDecodeError:
        return None


def _runtime_std_string(read, process: object, address: int) -> str | None:
    raw = _runtime_exact(read, process, address, 32)
    if not raw:
        return None
    length = int(struct.unpack_from("<Q", raw, 16)[0])
    capacity = int(struct.unpack_from("<Q", raw, 24)[0])
    if not 0 < length <= 160 or capacity < length:
        return None
    payload = (
        raw[:length]
        if capacity <= 15
        else _runtime_exact(read, process, struct.unpack_from("<Q", raw, 0)[0], length)
    )
    if not payload:
        return None
    try:
        return _valid_runtime_text(payload.decode("utf-8"))
    except UnicodeDecodeError:
        return None


def _runtime_text_field(read, process: object, address: int) -> str | None:
    direct = _runtime_fm_string(read, process, address)
    if direct:
        return direct
    nested = _runtime_ptr(read, process, address)
    if nested:
        value = _runtime_fm_string(read, process, nested)
        if value:
            return value
    return _runtime_std_string(read, process, address)


def _valid_local_save_pair(path_value: str | None, label_value: str | None) -> bool:
    if not path_value or not label_value:
        return False
    filename = PureWindowsPath(path_value.replace("/", "\\")).name
    if not filename.casefold().endswith(".fm"):
        return False
    stem = filename[:-3].strip()
    return bool(stem and stem.casefold() == label_value.strip().casefold())


def _iter_runtime_vtable_instances(
    process: object, regions: list[object], read, vtables: list[int],
    *, max_region_size: int, min_region_size: int = 0, limit: int = 128,
):
    needles = [(int(vtable), struct.pack("<Q", int(vtable))) for vtable in vtables]
    if not needles:
        return
    yielded: set[int] = set()
    for region in sorted(regions, key=lambda item: int(item.base_address), reverse=True):
        if not int(min_region_size) < int(region.size) <= int(max_region_size):
            continue
        carry = b""
        for offset in range(0, int(region.size), FM24_SCAN_BLOCK_BYTES):
            size = min(FM24_SCAN_BLOCK_BYTES, int(region.size) - offset)
            raw = read(process, int(region.base_address) + offset, size)
            if not raw:
                carry = b""
                continue
            window = carry + raw
            origin = int(region.base_address) + offset - len(carry)
            for vtable, needle in needles:
                for position in _memory_occurrences(window, needle):
                    address = origin + position
                    if address in yielded:
                        continue
                    yielded.add(address)
                    yield address, vtable
                    if len(yielded) >= limit:
                        return
            carry = window[-7:]


def _team_type_sample_summary(
    samples: list[tuple[int, int, int]],
) -> dict[str, object]:
    """Summarize candidate Team+0x28 values without retaining UIDs or pointers."""
    value_counts: dict[int, int] = {}
    club_types: dict[int, set[int]] = {}
    for _uid, club, team_type in samples:
        value_counts[team_type] = value_counts.get(team_type, 0) + 1
        club_types.setdefault(club, set()).add(team_type)
    linked_groups = sum(
        1 for values in club_types.values()
        if 0 in values and any(value in {10, 11, 12, 13} for value in values)
    )
    return {
        "objects": len(samples),
        "values": dict(sorted(value_counts.items())),
        "clubs": len(club_types),
        "linked_groups": linked_groups,
    }


def _player_performance_sample_summary(
    matches: list[dict[str, object]],
) -> dict[str, object]:
    """Summarize match-player evidence without retaining identities or fixtures."""
    score_goals = 0
    event_matches = 0
    event_goals = 0
    scorer_links = 0
    assist_events = 0
    assist_links = 0
    ratings: list[float] = []
    for match in matches:
        score_goals += max(0, int(match.get("home_goals") or 0))
        score_goals += max(0, int(match.get("away_goals") or 0))
        events = match.get("goal_events")
        if isinstance(events, list):
            event_matches += 1
            event_goals += len(events)
            for event in events:
                if not isinstance(event, dict):
                    continue
                scorer = event.get("scorer")
                assist = event.get("assist")
                if isinstance(scorer, dict) and scorer.get("name"):
                    scorer_links += 1
                if assist is not None:
                    assist_events += 1
                    if isinstance(assist, dict) and assist.get("name"):
                        assist_links += 1
        raw_ratings = match.get("player_ratings")
        if isinstance(raw_ratings, list):
            for value in raw_ratings:
                try:
                    rating = float(value)
                except (TypeError, ValueError):
                    continue
                if 0.0 <= rating <= 10.0:
                    ratings.append(rating)
    return {
        "matches": len(matches),
        "score_goals": score_goals,
        "event_matches": event_matches,
        "event_goals": event_goals,
        "scorer_links": scorer_links,
        "assist_events": assist_events,
        "assist_links": assist_links,
        "ratings": len(ratings),
        "rating_min": round(min(ratings), 2) if ratings else None,
        "rating_max": round(max(ratings), 2) if ratings else None,
    }


def _club_read_sample_summary(
    samples: list[dict[str, object]],
) -> dict[str, object]:
    """Reduce native club-profile samples to anonymous coverage counters."""
    summary: dict[str, object] = {
        "clubs": len(samples),
        "year_founded": 0,
        "attendance": 0,
        "supporters": 0,
        "ownership": 0,
        "culture": 0,
        "culture_records": 0,
        "facilities": 0,
        "facility_values": 0,
        "facility_min": None,
        "facility_max": None,
        "stadiums": 0,
        "stadium_capacity": 0,
        "stadium_pitch": 0,
        "training_grounds": 0,
        "finances": 0,
        "finance_core": 0,
        "income_statements": 0,
        "monthly_summaries": 0,
        "debt_lists": 0,
        "debt_records": 0,
        "sponsor_lists": 0,
        "sponsor_records": 0,
        "roster_teams": 0,
        "players": 0,
        "player_ca": 0,
        "player_pa": 0,
        "player_positions": 0,
        "player_fitness": 0,
        "player_sharpness": 0,
        "player_morale": 0,
    }
    facility_levels: list[int] = []
    facility_levels_by_field: dict[str, list[int]] = {
        key: [] for key in ("training", "youth", "junior_coaching", "youth_recruitment")
    }
    for sample in samples:
        information = sample.get("club_information")
        if not isinstance(information, dict):
            information = {}
        if information.get("year_founded") is not None:
            summary["year_founded"] = int(summary["year_founded"]) + 1
        attendance = information.get("attendance")
        if isinstance(attendance, dict) and all(
            attendance.get(key) is not None for key in ("average", "minimum", "maximum")
        ):
            summary["attendance"] = int(summary["attendance"]) + 1
        supporters = information.get("supporters")
        if isinstance(supporters, dict) and all(
            supporters.get(key) is not None
            for key in (
                "season_ticket_holders", "social_media_followers", "profile", "distribution",
            )
        ):
            summary["supporters"] = int(summary["supporters"]) + 1
        if isinstance(information.get("ownership"), dict):
            summary["ownership"] = int(summary["ownership"]) + 1
        culture = information.get("culture")
        if isinstance(culture, list):
            summary["culture"] = int(summary["culture"]) + 1
            summary["culture_records"] = int(summary["culture_records"]) + len(culture)

        facilities = information.get("facilities")
        if isinstance(facilities, dict):
            values = [facilities.get(key) for key in facility_levels_by_field]
            valid = [int(value) for value in values if isinstance(value, int) and 1 <= value <= 20]
            for key, value in zip(facility_levels_by_field, values, strict=True):
                if isinstance(value, int) and 1 <= value <= 20:
                    facility_levels_by_field[key].append(int(value))
            facility_levels.extend(valid)
            summary["facility_values"] = int(summary["facility_values"]) + len(valid)
            if len(valid) == 4:
                summary["facilities"] = int(summary["facilities"]) + 1

        stadium = information.get("stadium")
        if isinstance(stadium, dict):
            summary["stadiums"] = int(summary["stadiums"]) + 1
            if all(
                stadium.get(key) is not None
                for key in (
                    "capacity", "seating_capacity", "used_capacity", "expansion_capacity",
                )
            ):
                summary["stadium_capacity"] = int(summary["stadium_capacity"]) + 1
            if stadium.get("pitch_condition") is not None and stadium.get("pitch_type") is not None:
                summary["stadium_pitch"] = int(summary["stadium_pitch"]) + 1
        if isinstance(information.get("training_ground"), dict):
            summary["training_grounds"] = int(summary["training_grounds"]) + 1

        finances = information.get("finances")
        if isinstance(finances, dict):
            summary["finances"] = int(summary["finances"]) + 1
            if all(
                finances.get(key) is not None
                for key in ("balance", "remaining_transfer_budget")
            ):
                summary["finance_core"] = int(summary["finance_core"]) + 1
            if isinstance(finances.get("income_statement"), dict):
                summary["income_statements"] = int(summary["income_statements"]) + 1
            monthly = finances.get("monthly_summary")
            if isinstance(monthly, list):
                summary["monthly_summaries"] = int(summary["monthly_summaries"]) + 1
            debts = finances.get("debts")
            if isinstance(debts, list):
                summary["debt_lists"] = int(summary["debt_lists"]) + 1
                summary["debt_records"] = int(summary["debt_records"]) + len(debts)
            sponsors = finances.get("sponsors")
            if isinstance(sponsors, list):
                summary["sponsor_lists"] = int(summary["sponsor_lists"]) + 1
                summary["sponsor_records"] = int(summary["sponsor_records"]) + len(sponsors)

        roster = sample.get("roster")
        if not isinstance(roster, list):
            continue
        if roster:
            summary["roster_teams"] = int(summary["roster_teams"]) + 1
        summary["players"] = int(summary["players"]) + len(roster)
        for player in roster:
            if not isinstance(player, dict):
                continue
            if player.get("ca") is not None:
                summary["player_ca"] = int(summary["player_ca"]) + 1
            if player.get("pa") is not None:
                summary["player_pa"] = int(summary["player_pa"]) + 1
            if player.get("positions"):
                summary["player_positions"] = int(summary["player_positions"]) + 1
            if player.get("fitness_raw") is not None:
                summary["player_fitness"] = int(summary["player_fitness"]) + 1
            if player.get("sharpness_raw") is not None:
                summary["player_sharpness"] = int(summary["player_sharpness"]) + 1
            if player.get("morale_raw") is not None:
                summary["player_morale"] = int(summary["player_morale"]) + 1
    if facility_levels:
        summary["facility_min"] = min(facility_levels)
        summary["facility_max"] = max(facility_levels)
    for key, values in facility_levels_by_field.items():
        summary[f"facility_{key}"] = len(values)
        summary[f"facility_{key}_min"] = min(values) if values else None
        summary[f"facility_{key}_max"] = max(values) if values else None
    return summary


def _staff_structure_sample_summary(
    samples: list[dict[str, bool]],
) -> dict[str, int]:
    """Count validated staff/contract stages without retaining object identity."""
    keys = ("uid", "contract", "backref", "team", "job", "wage", "dates")
    return {
        "objects": len(samples),
        **{key: sum(1 for sample in samples if sample.get(key)) for key in keys},
    }


def _club_read_probe_rows(summary: dict[str, object]) -> list[str]:
    """Format stable codeword rows for the native club-profile reader."""
    minimum = summary["facility_min"] if summary["facility_min"] is not None else "-"
    maximum = summary["facility_max"] if summary["facility_max"] is not None else "-"

    def facility_range(key: str) -> str:
        low = summary[f"facility_{key}_min"]
        high = summary[f"facility_{key}_max"]
        return (
            f"{summary[f'facility_{key}']}:{low if low is not None else '-'}-"
            f"{high if high is not None else '-'}"
        )

    return [
        (
            f"  检测项 D-C1：C{summary['clubs']} Y{summary['year_founded']} "
            f"A{summary['attendance']} U{summary['supporters']} "
            f"O{summary['ownership']} K{summary['culture']} R{summary['culture_records']}"
        ),
        (
            f"  检测项 F-C1：C{summary['clubs']} V{summary['facilities']} "
            f"Q{summary['facility_values']} N{minimum} X{maximum} "
            f"T{facility_range('training')} Y{facility_range('youth')} "
            f"J{facility_range('junior_coaching')} "
            f"R{facility_range('youth_recruitment')}"
        ),
        (
            f"  检测项 F-C2：S{summary['stadiums']} C{summary['stadium_capacity']} "
            f"P{summary['stadium_pitch']} G{summary['training_grounds']}"
        ),
        (
            f"  检测项 E-C1：F{summary['finances']} C{summary['finance_core']} "
            f"I{summary['income_statements']} M{summary['monthly_summaries']} "
            f"D{summary['debt_lists']}:{summary['debt_records']} "
            f"S{summary['sponsor_lists']}:{summary['sponsor_records']}"
        ),
        (
            f"  检测项 R-C1：T{summary['roster_teams']} P{summary['players']} "
            f"C{summary['player_ca']} A{summary['player_pa']} "
            f"O{summary['player_positions']} F{summary['player_fitness']} "
            f"H{summary['player_sharpness']} M{summary['player_morale']}"
        ),
    ]


def runtime_native_club_probe(pid: int, layout: object) -> list[str]:
    """Exercise bounded official club/roster reads and retain only aggregates."""
    families = ("D-C1", "F-C1", "F-C2", "E-C1", "R-C1")
    try:
        from fm_collector.win32 import (
            MEM_PRIVATE, iter_readable_regions, open_process as open_remote_process,
            read_process_memory,
        )
        from tools.club_reader import _club_information
        from tools.initial_data_audit import Reader

        with open_remote_process(pid) as process:
            module = layout.module(process)
            team_vtable_rva = int(getattr(layout, "team_vtable_rva", 0) or 0)
            if module is None or not team_vtable_rva:
                return [f"  检测项 {family}：M0" for family in families]
            reader = Reader(process, int(module.base_address), layout)
            regions = [
                region for region in iter_readable_regions(process)
                if int(region.type) == int(MEM_PRIVATE)
            ]
            samples: list[dict[str, object]] = []
            seen_clubs: set[int] = set()
            for team_address, expected_vtable in _iter_runtime_vtable_instances(
                process, regions, read_process_memory,
                [int(module.base_address) + team_vtable_rva],
                max_region_size=FM24_DEEP_DIAGNOSTIC_REGION_LIMIT, limit=4096,
            ):
                if reader.ptr(team_address) != expected_vtable:
                    continue
                team = reader.team(team_address)
                if not team or team.get("team_type") != "club":
                    continue
                club_address = int(reader.ptr(team_address + 0x30) or 0)
                if not club_address or club_address in seen_clubs:
                    continue
                information = _club_information(reader, team_address)
                if not information:
                    continue
                seen_clubs.add(club_address)
                samples.append({
                    "club_information": information,
                    "roster": reader.roster(team_address),
                })
                if len(samples) >= 16:
                    break
        return _club_read_probe_rows(_club_read_sample_summary(samples))
    except Exception as error:
        return [f"  检测项 {family}：E{type(error).__name__}" for family in families]


def runtime_staff_structure_probe(pid: int, layout: object) -> str:
    """Sample the official staff primary-contract chain without names or IDs."""
    try:
        from fm_collector.win32 import open_process as open_remote_process
        from tools.club_reader import _staff_scan_entries
        from tools.initial_data_audit import Reader

        staff_vtable_rva = getattr(layout, "staff_person_vtable_rva", None)
        if staff_vtable_rva is None:
            return "  检测项 S-C1：U0"
        with open_remote_process(pid) as process:
            module = layout.module(process)
            if module is None:
                return "  检测项 S-C1：M0"
            reader = Reader(process, int(module.base_address), layout)
            samples: list[dict[str, bool]] = []
            for person, contract in _staff_scan_entries(reader, 0):
                uid_valid = int(reader.u32(person + 0x0C) or 0) > 0
                contract = int(contract or 0)
                contract_valid = contract > 0
                backref_valid = bool(contract and reader.ptr(contract + 0x08) == person)
                team_address = int(reader.ptr(contract + 0x10) or 0) if backref_valid else 0
                team_valid = bool(team_address and reader.team(team_address))
                job_offset = getattr(layout, "staff_job_type_offset", None)
                wage_offset = getattr(layout, "staff_wage_offset", None)
                start_offset = getattr(layout, "staff_contract_start_offset", None)
                expiry_offset = getattr(layout, "staff_contract_expiry_offset", None)
                job = reader.u8(contract + int(job_offset)) if contract and job_offset is not None else None
                wage = reader.u32(contract + int(wage_offset)) if contract and wage_offset is not None else None
                start = reader.u32(contract + int(start_offset)) if contract and start_offset is not None else None
                expiry = reader.u32(contract + int(expiry_offset)) if contract and expiry_offset is not None else None
                samples.append({
                    "uid": uid_valid,
                    "contract": contract_valid,
                    "backref": backref_valid,
                    "team": team_valid,
                    "job": job is not None,
                    "wage": wage is not None and 0 <= int(wage) <= 2_000_000_000,
                    "dates": start is not None and expiry is not None,
                })
                if len(samples) >= 512:
                    break
        summary = _staff_structure_sample_summary(samples)
        return (
            f"  检测项 S-C1：O{summary['objects']} U{summary['uid']} "
            f"C{summary['contract']} B{summary['backref']} T{summary['team']} "
            f"J{summary['job']} W{summary['wage']} D{summary['dates']}"
        )
    except Exception as error:
        return f"  检测项 S-C1：E{type(error).__name__}"


def runtime_player_performance_probe(pid: int, layout: object) -> list[str]:
    """Collect bounded, read-only goal/rating coverage from validated results."""
    try:
        from fm_collector.win32 import open_process as open_remote_process
        from tools.initial_data_audit import (
            Reader, deduplicate_results, parse_completed_result,
            parse_goal_events, scan_result_addresses,
        )

        with open_remote_process(pid) as process:
            module = layout.module(process)
            if module is None:
                return ["  检测项 P-C1：M0", "  检测项 P-C2：M0"]
            reader = Reader(process, module.base_address, layout)
            addresses, _bytes_scanned = scan_result_addresses(reader)
            completed = deduplicate_results([
                item for address in addresses
                if (item := parse_completed_result(reader, address))
            ])
            completed.sort(key=lambda item: str(item.get("date") or ""), reverse=True)
            samples: list[dict[str, object]] = []
            for item in completed[:64]:
                sample: dict[str, object] = {
                    "home_goals": int(item.get("home_goals") or 0),
                    "away_goals": int(item.get("away_goals") or 0),
                }
                result_address = int(str(item.get("result_address") or "0"), 16)
                sample["goal_events"] = (
                    parse_goal_events(reader, result_address) if result_address else None
                )
                # The result reader deliberately has no inferred rating fallback.
                # A verified match-player rating reader can populate this key later.
                sample["player_ratings"] = item.get("player_ratings")
                samples.append(sample)
        summary = _player_performance_sample_summary(samples)
        return [
            (
                f"  检测项 P-C1：M{summary['matches']} S{summary['score_goals']} "
                f"E{summary['event_matches']} G{summary['event_goals']} "
                f"L{summary['scorer_links']} A{summary['assist_events']} "
                f"Q{summary['assist_links']}"
            ),
            (
                f"  检测项 P-C2：R{summary['ratings']} "
                f"N{summary['rating_min'] if summary['rating_min'] is not None else '-'} "
                f"X{summary['rating_max'] if summary['rating_max'] is not None else '-'}"
            ),
        ]
    except Exception as error:
        return [
            f"  检测项 P-C1：E{type(error).__name__}",
            f"  检测项 P-C2：E{type(error).__name__}",
        ]


def runtime_team_type_probe(
    pid: int, module_name: str, rtti_vtables: dict[str, list[int]],
) -> str:
    """Sample the candidate Team Type byte through validated Team/Club objects."""
    try:
        from fm_collector.win32 import (
            find_module, iter_readable_regions, open_process as open_remote_process,
            read_process_memory,
        )

        with open_remote_process(pid) as process:
            module = find_module(process, module_name)
            if module is None:
                return "  检测项 T-C1：M0"
            regions = list(iter_readable_regions(process))
            team_vtables = [
                module.base_address + int(rva)
                for rva in rtti_vtables.get(FM24_RTTI_NAMES["球队"], [])
            ]
            club_vtables = {
                module.base_address + int(rva)
                for rva in rtti_vtables.get(FM24_RTTI_NAMES["俱乐部"], [])
            }
            samples: list[tuple[int, int, int]] = []
            for address, expected_vtable in _iter_runtime_vtable_instances(
                process, regions, read_process_memory, team_vtables,
                max_region_size=FM24_DEEP_DIAGNOSTIC_REGION_LIMIT, limit=4096,
            ):
                if _runtime_ptr(read_process_memory, process, address) != expected_vtable:
                    continue
                uid = _runtime_u32(read_process_memory, process, address + 0x0C)
                club = _runtime_ptr(read_process_memory, process, address + 0x30)
                club_vtable = _runtime_ptr(read_process_memory, process, club) if club else 0
                raw_type = _runtime_exact(read_process_memory, process, address + 0x28, 1)
                if not uid or not club or club_vtable not in club_vtables or not raw_type:
                    continue
                samples.append((uid, club, raw_type[0]))
        summary = _team_type_sample_summary(samples)
        values = ",".join(
            f"{value}:{count}" for value, count in summary["values"].items()
        ) or "-"
        return (
            f"  检测项 T-C1：O{summary['objects']} G{summary['clubs']} "
            f"L{summary['linked_groups']} V{values}"
        )
    except Exception as error:
        return f"  检测项 T-C1：E{type(error).__name__}"


def _fm24_runtime_connection_summary(
    process: object, module_base: int, regions: list[object], read,
    rtti_vtables: dict[str, list[int]],
) -> dict[str, int]:
    provider_vtables = [
        module_base + int(rva)
        for rva in rtti_vtables.get(FM24_SAVE_PROVIDER_RTTI, [])
    ]
    manager_rtti_vtables = sorted(
        int(rva) for rva in rtti_vtables.get(FM24_RTTI_NAMES["人类经理"], [])
    )
    # The decorated HUMAN_NON_PLAYER type exposes several subobject vtables.
    # FM24's authoritative person vtable is the final/highest RVA, matching
    # GameLayout.human_manager_vtable_rvas rather than every RTTI hit.
    manager_vtables = (
        [module_base + manager_rtti_vtables[-1]] if manager_rtti_vtables else []
    )
    team_vtables = {
        module_base + int(rva)
        for name in (FM24_RTTI_NAMES["球队"], FM24_RTTI_NAMES["国家队"])
        for rva in rtti_vtables.get(name, [])
    }

    provider_objects = 0
    valid_local_providers = 0
    deep_provider_objects = 0
    deep_valid_local_providers = 0

    def inspect_providers(minimum: int, maximum: int, *, deep: bool) -> None:
        nonlocal provider_objects, valid_local_providers
        nonlocal deep_provider_objects, deep_valid_local_providers
        for address, expected_vtable in _iter_runtime_vtable_instances(
            process, regions, read, provider_vtables,
            min_region_size=minimum, max_region_size=maximum,
        ):
            if _runtime_ptr(read, process, address) != expected_vtable:
                continue
            provider_objects += 1
            if deep:
                deep_provider_objects += 1
            path_value = _runtime_text_field(
                read, process, address + FM24_LOCAL_SAVE_PATH_OFFSET,
            )
            label_value = _runtime_text_field(
                read, process, address + FM24_LOCAL_SAVE_LABEL_OFFSET,
            )
            if _valid_local_save_pair(path_value, label_value):
                valid_local_providers += 1
                if deep:
                    deep_valid_local_providers += 1

    inspect_providers(0, FM24_SAVE_PROVIDER_REGION_LIMIT, deep=False)
    provider_deep_scan = int(not valid_local_providers and bool(provider_vtables))
    if provider_deep_scan:
        inspect_providers(
            FM24_SAVE_PROVIDER_REGION_LIMIT,
            FM24_DEEP_DIAGNOSTIC_REGION_LIMIT,
            deep=True,
        )

    manager_candidates = 0
    deep_manager_candidates = 0
    manager_uids: set[int] = set()
    contract_uids: set[int] = set()
    contract_backref_uids: set[int] = set()
    team_pointer_uids: set[int] = set()
    team_type_uids: set[int] = set()
    team_uid_uids: set[int] = set()
    linked_manager_uids: set[int] = set()
    deep_linked_manager_uids: set[int] = set()

    def inspect_managers(minimum: int, maximum: int, *, deep: bool) -> None:
        nonlocal manager_candidates, deep_manager_candidates
        for person, expected_vtable in _iter_runtime_vtable_instances(
            process, regions, read, manager_vtables,
            min_region_size=minimum, max_region_size=maximum,
        ):
            if _runtime_ptr(read, process, person) != expected_vtable:
                continue
            manager_candidates += 1
            if deep:
                deep_manager_candidates += 1
            manager = person - FM24_MANAGER_PERSON_OFFSET
            manager_uid = _runtime_u32(
                read, process, person + FM24_ENTITY_UID_OFFSET,
            )
            if manager <= 0 or manager_uid <= 0:
                continue
            manager_uids.add(manager_uid)
            contract = _runtime_ptr(
                read, process, person + FM24_MANAGER_CONTRACT_OFFSET,
            )
            if not contract:
                continue
            contract_uids.add(manager_uid)
            if _runtime_ptr(
                read, process, contract + FM24_CONTRACT_PERSON_OFFSET,
            ) != person:
                continue
            contract_backref_uids.add(manager_uid)
            team = _runtime_ptr(
                read, process, contract + FM24_CONTRACT_TEAM_OFFSET,
            )
            if not team:
                continue
            team_pointer_uids.add(manager_uid)
            if _runtime_ptr(read, process, team) not in team_vtables:
                continue
            team_type_uids.add(manager_uid)
            if _runtime_u32(
                read, process, team + FM24_ENTITY_UID_OFFSET,
            ) <= 0:
                continue
            team_uid_uids.add(manager_uid)
            if _runtime_ptr(
                read, process, team + FM24_TEAM_MANAGER_OFFSET,
            ) != manager:
                continue
            linked_manager_uids.add(manager_uid)
            if deep:
                deep_linked_manager_uids.add(manager_uid)

    inspect_managers(0, FM24_MANAGER_REGION_LIMIT, deep=False)
    manager_deep_scan = int(not linked_manager_uids and bool(manager_vtables))
    if manager_deep_scan:
        inspect_managers(
            FM24_MANAGER_REGION_LIMIT,
            FM24_DEEP_DIAGNOSTIC_REGION_LIMIT,
            deep=True,
        )

    return {
        "provider_type_count": len(provider_vtables),
        "provider_object_count": provider_objects,
        "valid_local_provider_count": valid_local_providers,
        "provider_deep_scan": provider_deep_scan,
        "deep_provider_object_count": deep_provider_objects,
        "deep_valid_local_provider_count": deep_valid_local_providers,
        "manager_type_count": len(manager_vtables),
        "manager_rtti_variant_count": len(manager_rtti_vtables),
        "manager_candidate_count": manager_candidates,
        "manager_object_count": len(manager_uids),
        "manager_contract_count": len(contract_uids),
        "manager_contract_backref_count": len(contract_backref_uids),
        "manager_team_pointer_count": len(team_pointer_uids),
        "manager_team_type_count": len(team_type_uids),
        "manager_team_uid_count": len(team_uid_uids),
        "linked_manager_count": len(linked_manager_uids),
        "manager_deep_scan": manager_deep_scan,
        "deep_manager_candidate_count": deep_manager_candidates,
        "deep_linked_manager_count": len(deep_linked_manager_uids),
    }


def _fm24_runtime_connection_rows(summary: dict[str, int]) -> list[str]:
    local_ready = summary["valid_local_provider_count"] > 0
    manager_ready = summary["linked_manager_count"] > 0
    rows = [
        "FM2024 当前存档连接检查（只读；不输出存档名、路径、经理/球队名称或 ID）：",
        "  本地存档类型：" + (
            "已定位" if summary["provider_type_count"] else "未定位"
        ),
        "  本地存档对象：" + (
            f"已确认（{summary['valid_local_provider_count']} 个有效对象）"
            if local_ready else
            f"发现 {summary['provider_object_count']} 个候选，但路径/名称校验未通过"
            if summary["provider_object_count"] else "未找到"
        ),
        "  人类经理对象：" + (
            f"已确认（{summary['manager_object_count']} 个）"
            if summary["manager_object_count"] else "未确认"
        ),
        "  主经理校验链："
        f"候选 {summary.get('manager_candidate_count', summary['manager_object_count'])}"
        f" / UID {summary['manager_object_count']}"
        f" / 合同 {summary.get('manager_contract_count', 0)}"
        f" / 合同回指 {summary.get('manager_contract_backref_count', 0)}"
        f" / 球队指针 {summary.get('manager_team_pointer_count', 0)}"
        f" / 球队类型 {summary.get('manager_team_type_count', 0)}"
        f" / 球队 UID {summary.get('manager_team_uid_count', 0)}"
        f" / 球队回指 {summary['linked_manager_count']}",
        "  经理—球队关系：" + (
            f"已确认（{summary['linked_manager_count']} 个经理关系闭环）"
            if manager_ready else "未确认"
        ),
    ]
    if summary.get("provider_deep_scan") or summary.get("manager_deep_scan"):
        rows.append(
            "  深层区域补充："
            f"存档候选 {summary.get('deep_provider_object_count', 0)}"
            f" / 有效 {summary.get('deep_valid_local_provider_count', 0)}；"
            f"主经理候选 {summary.get('deep_manager_candidate_count', 0)}"
            f" / 关系闭环 {summary.get('deep_linked_manager_count', 0)}"
        )
    if local_ready and manager_ready:
        conclusion = "本地存档身份与经理会话证据均可读取"
    elif local_ready:
        conclusion = "本地存档身份可读取，但经理会话证据缺失"
    elif manager_ready:
        conclusion = "未读取到本地存档对象，但可建立当前连接的经理会话作用域"
    else:
        conclusion = "两类连接证据均缺失，FMODD 会拒绝首次连接"
    rows.append(f"  连接判定：{conclusion}。")
    return rows


def fm24_runtime_connection_probe(
    pid: int, process_path: str | None = None,
    rtti_vtables: dict[str, list[int]] | None = None,
) -> list[str]:
    """Inspect only the FM24 evidence required by FMODD's connection handshake."""
    try:
        from fm_collector.win32 import (
            MEM_PRIVATE, find_module, iter_readable_regions,
            open_process as open_remote_process, read_process_memory,
        )

        required_names = (
            FM24_SAVE_PROVIDER_RTTI,
            FM24_RTTI_NAMES["人类经理"],
            FM24_RTTI_NAMES["球队"],
            FM24_RTTI_NAMES["国家队"],
        )
        if rtti_vtables is None:
            if not process_path:
                raise RuntimeError("缺少 fm.exe 路径")
            from tools.game_layout import _pe_sections, _rtti_vtables

            with open(process_path, "rb") as source, mmap.mmap(
                source.fileno(), 0, access=mmap.ACCESS_READ,
            ) as raw:
                sections = _pe_sections(raw)
                rtti_vtables = {
                    name: list(_rtti_vtables(raw, sections, name))
                    for name in required_names
                }

        with open_remote_process(pid) as process:
            module = find_module(process, "fm.exe")
            if module is None:
                raise RuntimeError("进程中未找到 fm.exe 主模块")
            regions = [
                region for region in iter_readable_regions(process)
                if int(region.type) == int(MEM_PRIVATE)
            ]
            summary = _fm24_runtime_connection_summary(
                process, int(module.base_address), regions,
                read_process_memory, rtti_vtables,
            )

        return _fm24_runtime_connection_rows(summary)
    except Exception as error:
        return [f"FM2024 当前存档连接检查：执行失败（{type(error).__name__}: {error}）"]


def fm24_memory_adaptation_probe(pid: int) -> list[str]:
    """Probe an ACL-protected FM24 image, then validate connection evidence."""
    try:
        from fm_collector.win32 import (
            find_module, iter_readable_regions, open_process as open_remote_process,
            read_process_memory,
        )

        with open_remote_process(pid) as process:
            module = find_module(process, "fm.exe")
            if module is None:
                raise RuntimeError("进程中未找到 fm.exe 主模块")
            header = read_process_memory(process, module.base_address, 0x1000)
            if not header or header[:2] != b"MZ":
                raise RuntimeError("无法从进程读取 PE 头")
            pe_offset = struct.unpack_from("<I", header, 0x3C)[0]
            if header[pe_offset:pe_offset + 4] != b"PE\0\0":
                raise RuntimeError("进程 PE 签名无效")
            machine, _count, timestamp = struct.unpack_from("<HHI", header, pe_offset + 4)
            image_size = struct.unpack_from("<I", header, pe_offset + 24 + 56)[0]
            snapshot_size = min(int(module.size), int(image_size))
            image = bytearray(snapshot_size)
            readable_bytes = 0
            module_end = module.base_address + snapshot_size
            for region in iter_readable_regions(process):
                start = max(module.base_address, region.base_address)
                end = min(module_end, region.base_address + region.size)
                if start >= end:
                    continue
                for address in range(start, end, 4 * 1024 * 1024):
                    size = min(4 * 1024 * 1024, end - address)
                    data = read_process_memory(process, address, size)
                    if not data:
                        continue
                    offset = address - module.base_address
                    image[offset:offset + len(data)] = data
                    readable_bytes += len(data)

        raw = bytes(image)
        rows = [
            "FM2024 XGP 版本结构检查（先读取 fm.exe 映像，再只读验证连接条件）：",
            f"  模块基址：0x{module.base_address:X}",
            f"  PE 机器类型：0x{machine:04X}",
            f"  PE 时间戳：0x{timestamp:08X}（{datetime.fromtimestamp(timestamp).isoformat(sep=' ', timespec='seconds')}）",
            f"  PE 映像大小：0x{image_size:X}（{image_size} 字节）",
            f"  可读映像：{readable_bytes}/{snapshot_size} 字节",
        ]
        resolved = 0
        rtti_vtables: dict[str, list[int]] = {}
        for label, decorated_name in FM24_RTTI_NAMES.items():
            hits = _memory_rtti_vtables(raw, module.base_address, decorated_name)
            rtti_vtables[decorated_name] = list(hits)
            if hits:
                resolved += 1
            rows.append(f"  RTTI {label}：" + (
                ", ".join(f"0x{rva:X}" for rva in hits) if hits else "未找到"
            ))
        date_hits = _memory_pattern_occurrences(raw, FM24_GAME_DATE_PATTERN)
        date_rows = []
        for hit in date_hits:
            target = hit + 7 + struct.unpack_from("<i", raw, hit + 3)[0]
            date_rows.append(f"代码 0x{hit:X} -> 数据 0x{target:X}")
        rows.append("  当前日期签名：" + (", ".join(date_rows) if date_rows else "未找到"))
        for code, pattern in (
            ("H24-A", FM24_COMPAT_FINGERPRINT_A),
            ("H24-B", FM24_COMPAT_FINGERPRINT_B),
            ("C-C1", CLUB_CULTURE_CONSTRUCTOR_PATTERN),
            ("C-C2", FM24_CLUB_CULTURE_INITIALIZE_PATTERN),
        ):
            hits = _memory_pattern_occurrences(raw, pattern)
            rows.append(f"  兼容性指纹 {code}：" + (
                ", ".join(f"0x{rva:X}" for rva in hits) if hits else "未找到"
            ))
        rtti_vtables[FM24_SAVE_PROVIDER_RTTI] = _memory_rtti_vtables(
            raw, module.base_address, FM24_SAVE_PROVIDER_RTTI,
        )
        rows.append(f"  RTTI 解析汇总：{resolved}/{len(FM24_RTTI_NAMES)} 类已定位")
        rows.extend(fm24_runtime_connection_probe(pid, rtti_vtables=rtti_vtables))
        rows.append(runtime_team_type_probe(pid, "fm.exe", rtti_vtables))
        return rows
    except Exception as error:
        return [f"FM2024 XGP 内存适配探针：执行失败（{type(error).__name__}: {error}）"]


def fm24_adaptation_probe(path: str, pid: int | None = None) -> list[str]:
    """Resolve FM24 structure RVAs and optionally validate live connection evidence."""
    try:
        from tools.game_layout import (
            _pattern_occurrences, _pe_sections, _read_rva, _rtti_vtables,
        )

        with open(path, "rb") as source, mmap.mmap(source.fileno(), 0, access=mmap.ACCESS_READ) as raw:
            sections = _pe_sections(raw)
            rows = ["FM2024 版本结构检查（分析 fm.exe；不输出存档或进程内文本）："]
            resolved = 0
            rtti_vtables: dict[str, list[int]] = {}
            for label, decorated_name in FM24_RTTI_NAMES.items():
                hits = _rtti_vtables(raw, sections, decorated_name)
                rtti_vtables[decorated_name] = list(hits)
                if hits:
                    resolved += 1
                    value = ", ".join(f"0x{rva:X}" for rva in hits)
                else:
                    value = "未找到"
                rows.append(f"  RTTI {label}：{value}")

            date_hits = _pattern_occurrences(raw, sections, FM24_GAME_DATE_PATTERN)
            date_targets: list[int] = []
            for hit in date_hits:
                instruction = _read_rva(raw, sections, hit, 7)
                if instruction and len(instruction) == 7:
                    date_targets.append(hit + 7 + struct.unpack_from("<i", instruction, 3)[0])
            rows.append(
                "  当前日期签名：" + (
                    ", ".join(f"代码 0x{hit:X} -> 数据 0x{target:X}" for hit, target in zip(date_hits, date_targets))
                    if date_hits and len(date_hits) == len(date_targets) else f"匹配 {len(date_hits)} 处（目标解析不完整）"
                )
            )

            for code, pattern in (
                ("H24-A", FM24_COMPAT_FINGERPRINT_A),
                ("H24-B", FM24_COMPAT_FINGERPRINT_B),
                ("C-C1", CLUB_CULTURE_CONSTRUCTOR_PATTERN),
                ("C-C2", FM24_CLUB_CULTURE_INITIALIZE_PATTERN),
            ):
                hits = _pattern_occurrences(raw, sections, pattern)
                rows.append(f"  兼容性指纹 {code}：" + (
                    ", ".join(f"0x{rva:X}" for rva in hits) if hits else "未找到"
                ))
            rtti_vtables[FM24_SAVE_PROVIDER_RTTI] = list(
                _rtti_vtables(raw, sections, FM24_SAVE_PROVIDER_RTTI)
            )
            rows.append(f"  RTTI 解析汇总：{resolved}/{len(FM24_RTTI_NAMES)} 类已定位")
            if pid is not None:
                rows.extend(fm24_runtime_connection_probe(
                    pid, process_path=path, rtti_vtables=rtti_vtables,
                ))
                rows.append(runtime_team_type_probe(pid, "fm.exe", rtti_vtables))
            return rows
    except Exception as error:
        return [f"适配探针：执行失败（{type(error).__name__}: {error}）"]


def _fm26_plugin_path(process_path: str) -> Path | None:
    install_root = Path(process_path).parent
    candidates = (
        install_root / "fm_Data" / "Plugins" / "x86_64" / "game_plugin.dll",
        install_root / "Content" / "fm_Data" / "Plugins" / "x86_64" / "game_plugin.dll",
        install_root / "game_plugin.dll",
        install_root / "Content" / "game_plugin.dll",
    )
    return next((candidate for candidate in candidates if candidate.is_file()), None)


def _adaptation_probe_kind(
    process_path: str, detected_version: str, known_build: str | None,
    fm26_plugin_path: Path | None,
) -> str | None:
    """Select the generation probe before the generic XGP Content fallback."""
    if known_build == "FM2026 Steam" or fm26_plugin_path is not None:
        return "fm26"
    if detected_version.startswith("24."):
        return "fm24_file"
    normalized = process_path.casefold().replace("/", "\\")
    if normalized.endswith("\\content\\fm.exe"):
        return "fm24_memory"
    return None


def fm26_runtime_save_probe(pid: int, save_root_rvas: list[int]) -> str:
    """Report save-root readiness without exposing save IDs or text."""
    try:
        from fm_collector.win32 import (
            find_module, open_process as open_remote_process, read_process_memory,
        )

        with open_remote_process(pid) as process:
            module = find_module(process, "game_plugin.dll")
            if module is None:
                return "  运行时存档根：game_plugin.dll 尚未加载"
            readable_roots = 0
            active_roots = 0
            for rva in save_root_rvas:
                pointer_raw = read_process_memory(process, module.base_address + rva, 8)
                if not pointer_raw or len(pointer_raw) != 8:
                    continue
                root = struct.unpack("<Q", pointer_raw)[0]
                if not root:
                    continue
                readable_roots += 1
                identity_raw = read_process_memory(
                    process,
                    root + FM26_SAVEGAME_ID_OFFSET,
                    FM26_SESSION_NONCE_OFFSET - FM26_SAVEGAME_ID_OFFSET + 4,
                )
                if not identity_raw or len(identity_raw) != 8:
                    continue
                savegame_id, session_nonce = struct.unpack("<II", identity_raw)
                if savegame_id and session_nonce:
                    active_roots += 1
        if active_roots:
            return (
                f"  运行时存档根：已就绪（{active_roots}/{len(save_root_rvas)} 个候选有效；"
                "身份值已隐藏）"
            )
        if readable_roots:
            return (
                f"  运行时存档根：对象可读但身份字段未就绪（{readable_roots}/"
                f"{len(save_root_rvas)} 个候选有对象）"
            )
        return f"  运行时存档根：未就绪（0/{len(save_root_rvas)} 个候选有对象）"
    except Exception as error:
        return f"  运行时存档根：检查失败（{type(error).__name__}: {error}）"


def fm26_adaptation_probe(process_path: str, pid: int | None = None) -> list[str]:
    """Resolve FM26 RVAs and inspect only non-identifying save readiness."""
    plugin_path = _fm26_plugin_path(process_path)
    if plugin_path is None:
        return ["FM2026 适配探针：未找到 game_plugin.dll（请返回安装目录结构信息）"]
    try:
        from tools.game_layout import (
            _pattern_occurrences, _pe_sections, _read_rva, _rtti_vtables,
        )

        plugin_pe = pe_details(str(plugin_path))
        rows = [
            "FM2026 适配探针（分析 game_plugin.dll 并只读检查存档根状态；"
            "不采集存档 ID、名称或进程内文本）：",
            f"  核心模块：{plugin_path}",
            f"  核心模块大小：{plugin_path.stat().st_size} 字节",
            f"  核心模块 SHA256：{sha256(str(plugin_path))}",
            f"  核心模块 PE 时间戳：{plugin_pe['timestamp']}",
            f"  核心模块 PE 映像大小：{plugin_pe['image_size']}",
        ]
        with plugin_path.open("rb") as source, mmap.mmap(source.fileno(), 0, access=mmap.ACCESS_READ) as raw:
            sections = _pe_sections(raw)
            resolved = 0
            rtti_vtables: dict[str, list[int]] = {}
            # FM26 does not expose the FM24 match-session class in this module.
            names = tuple(FM24_RTTI_NAMES.items())[:-1]
            for label, decorated_name in names:
                hits = _rtti_vtables(raw, sections, decorated_name)
                rtti_vtables[decorated_name] = list(hits)
                if hits:
                    resolved += 1
                    value = ", ".join(f"0x{rva:X}" for rva in hits)
                else:
                    value = "未找到"
                rows.append(f"  RTTI {label}：{value}")

            date_hits = _pattern_occurrences(raw, sections, FM26_GAME_DATE_PATTERN)
            date_rows: list[str] = []
            for hit in date_hits:
                instruction_rva = hit + 14
                instruction = _read_rva(raw, sections, instruction_rva, 6)
                if instruction and len(instruction) == 6:
                    target = instruction_rva + 6 + struct.unpack_from("<i", instruction, 2)[0]
                    date_rows.append(f"代码 0x{hit:X} -> 数据 0x{target:X}")
            rows.append("  当前日期签名：" + (", ".join(date_rows) if date_rows else f"匹配 {len(date_hits)} 处（目标解析不完整）"))

            save_hits = _pattern_occurrences(raw, sections, FM26_SAVE_ROOT_PATTERN)
            save_root_rvas: list[int] = []
            for hit in save_hits:
                instruction = _read_rva(raw, sections, hit, 7)
                if instruction and len(instruction) == 7:
                    save_root_rvas.append(
                        hit + 7 + struct.unpack_from("<i", instruction, 3)[0]
                    )
            rows.append(
                "  存档根签名：" + (
                    ", ".join(f"0x{rva:X}" for rva in save_root_rvas)
                    if save_root_rvas else f"匹配 {len(save_hits)} 处（目标解析不完整）"
                )
            )
            if pid is not None and save_root_rvas:
                rows.append(fm26_runtime_save_probe(pid, save_root_rvas))

            for code, pattern in (
                ("H26-A", FM26_COMPAT_FINGERPRINT_A),
                ("H26-B", FM26_COMPAT_FINGERPRINT_B),
                ("C-C1", CLUB_CULTURE_CONSTRUCTOR_PATTERN),
            ):
                hits = _pattern_occurrences(raw, sections, pattern)
                rows.append(f"  兼容性指纹 {code}：" + (
                    ", ".join(f"0x{rva:X}" for rva in hits) if hits else "未找到"
                ))
            rows.append(f"  RTTI 解析汇总：{resolved}/{len(names)} 类已定位")
            if pid is not None:
                rows.append(runtime_team_type_probe(
                    pid, "game_plugin.dll", rtti_vtables,
                ))
        return rows
    except Exception as error:
        return [f"FM2026 适配探针：执行失败（{type(error).__name__}: {error}）"]


def yes_no(value: bool) -> str:
    return "是" if value else "否"


def fmodd_service_probe() -> list[str]:
    """Read FMODD's local refresh status without collecting save data."""
    from urllib.error import URLError
    from urllib.request import urlopen

    for port, label in ((7856, "正式桌面服务"), (7857, "开发服务")):
        url = f"http://127.0.0.1:{port}/api/refresh/status"
        try:
            with urlopen(url, timeout=2) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (OSError, URLError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        error_value = payload.get("error")
        error = str(error_value or "无")[:2000]
        error_stage = str(payload.get("error_stage") or "无")[:200]
        error_traceback = str(payload.get("error_traceback") or "").strip()[-6000:]
        operation_error = str(payload.get("operation_error") or "无")[:2000]
        operation_error_type = str(payload.get("operation_error_type") or "无")[:200]
        operation_error_stage = str(payload.get("operation_error_stage") or "无")[:200]
        operation_traceback = str(
            payload.get("operation_error_traceback") or ""
        ).strip()[-6000:]
        status = (
            "读取失败" if error_value else
            "正在刷新" if payload.get("refreshing") else
            "正在后台核对" if payload.get("reconciling") else
            "空闲"
        )
        return [
            f"FMODD 本地状态（{label}，端口 {port}；不采集存档内容）：",
            f"  正在刷新：{yes_no(bool(payload.get('refreshing')))}",
            f"  后台核对：{yes_no(bool(payload.get('reconciling')))}",
            f"  刷新模式：{payload.get('refresh_mode') or '无'}",
            f"  刷新原因：{payload.get('refresh_reason') or '无'}",
            f"  已有输出：{yes_no(bool(payload.get('has_output')))}",
            f"  缓存已验证：{yes_no(bool(payload.get('cache_verified')))}",
            f"  状态：{status}",
            f"  错误：{error}",
            f"  失败阶段：{error_stage}",
            *(
                ["  异常栈（不含存档内容）：", *[f"    {line}" for line in error_traceback.splitlines()]]
                if error_traceback else []
            ),
            f"  最近操作错误：{operation_error}",
            f"  最近操作异常类型：{operation_error_type}",
            f"  最近操作失败阶段：{operation_error_stage}",
            *(
                [
                    "  最近操作异常栈（不含存档内容）：",
                    *[f"    {line}" for line in operation_traceback.splitlines()],
                ]
                if operation_traceback else []
            ),
        ]
    return ["FMODD 本地状态：未检测到 7856/7857 服务（请在问题出现时保持 FMODD 打开）"]


def generate_report() -> str:
    lines = [
        APP_TITLE,
        "=" * 64,
        f"Windows：{platform.platform()}",
        f"检测工具管理员权限：{yes_no(bool(shell32.IsUserAnAdmin()))}",
        "用途：检查 FM2024 / FM2026 的启动、读取权限与当前存档连接条件",
        "",
    ]
    processes = fm_processes()
    if not processes:
        lines.extend([
            "检测结果：未发现进程名为 fm.exe 的进程。",
            "建议：先启动 FM2024 或 FM2026 并进入存档，再点击“重新检测”。",
        ])
        return "\n".join(lines)

    lines.append(f"发现 fm.exe：{len(processes)} 个")
    supported = False
    readable_count = 0
    fm24_probe_count = 0
    fm26_probe_count = 0
    for index, (pid, thread_count) in enumerate(processes, 1):
        lines.extend(["", f"[{index}] PID：{pid}", f"线程数：{thread_count}"])
        path, path_error = process_path(pid)
        if not path:
            lines.extend([
                "进程路径：无法读取",
                f"路径读取错误：{path_error}",
                "内存读取权限：未继续检测",
                "判定：FMODD 无法识别该进程；可能是进程正在退出、无效残留或权限等级不同。",
            ])
            continue

        readable, read_error = can_read_memory(pid)
        if readable:
            readable_count += 1
        lines.append(f"进程路径：{path}")
        lines.append(f"安装来源推测：{distribution_hint(path)}")
        detected_version = file_version(path)
        lines.append(f"文件版本：{detected_version}")
        try:
            stat = Path(path).stat()
            lines.append(f"文件大小：{stat.st_size} 字节")
            lines.append(f"修改时间：{datetime.fromtimestamp(stat.st_mtime).isoformat(sep=' ', timespec='seconds')}")
        except OSError as error:
            lines.append(f"文件信息：读取失败（{error}）")
        pe = pe_details(path)
        lines.append(f"PE 机器类型：{pe['machine']}")
        lines.append(f"PE 时间戳：{pe['timestamp']}")
        lines.append(f"PE 映像大小：{pe['image_size']}")
        lines.append(f"内存读取权限：{'正常' if readable else '失败：' + str(read_error)}")
        memory_identity = process_module_pe_identity(pid) if readable else None
        plugin_memory_identity = (
            process_module_pe_identity(pid, "game_plugin.dll") if readable else None
        )
        memory_known_build = (
            KNOWN_MEMORY_BUILDS.get(plugin_memory_identity)
            if plugin_memory_identity else None
        ) or (KNOWN_MEMORY_BUILDS.get(memory_identity) if memory_identity else None)
        if memory_identity:
            lines.append(
                f"内存 PE 身份：时间戳 0x{memory_identity[0]:08X}，映像大小 0x{memory_identity[1]:X}"
            )
        if plugin_memory_identity:
            lines.append(
                "核心模块内存 PE 身份："
                f"时间戳 0x{plugin_memory_identity[0]:08X}，"
                f"映像大小 0x{plugin_memory_identity[1]:X}"
            )
        try:
            actual_hash = sha256(path)
            known_build = KNOWN_BUILDS.get(actual_hash) or memory_known_build
            lines.append(f"实际 SHA256：{actual_hash}")
            lines.append(f"已知版本识别：{known_build or '未收录（仍可将本报告返回给开发者）'}")
        except OSError as error:
            actual_hash = ""
            known_build = memory_known_build
            lines.append(f"实际 SHA256：读取失败（{error}）")
            lines.append(f"已知版本识别：{known_build or '无法判断'}")

        if known_build and readable:
            supported = True
            lines.append("判定：这是已收录版本，且当前进程读取权限正常。")
        elif not known_build and readable:
            lines.append("判定：进程读取正常，但属于尚未收录或经过修改的版本。")
            lines.append("说明：离线版、不同更新版本和修改版出现此结果属于正常现象。")
        else:
            lines.append("判定：已发现游戏，但当前没有读取游戏进程的权限。")
            lines.append("建议：让 FM2024 与 FMODD 使用相同权限等级启动。")

        fm26_plugin_path = _fm26_plugin_path(path) if readable else None
        probe_kind = _adaptation_probe_kind(
            path, detected_version, known_build, fm26_plugin_path,
        ) if readable else None
        if probe_kind == "fm26":
            lines.extend(fm26_adaptation_probe(path, pid))
            fm26_probe_count += 1
        elif probe_kind == "fm24_file":
            lines.extend(fm24_adaptation_probe(path, pid))
            fm24_probe_count += 1
        elif probe_kind == "fm24_memory":
            lines.extend(fm24_memory_adaptation_probe(pid))
            fm24_probe_count += 1

        if readable:
            try:
                runtime_layout = _runtime_layout_for_process(
                    pid, path, actual_hash or None, memory_identity,
                    plugin_memory_identity,
                )
                if runtime_layout is not None:
                    lines.append(render_person_table_probe(
                        runtime_person_table_probe(pid, runtime_layout),
                    ))
                    lines.extend(runtime_player_performance_probe(pid, runtime_layout))
                    lines.extend(runtime_native_club_probe(pid, runtime_layout))
                    lines.append(runtime_staff_structure_probe(pid, runtime_layout))
            except Exception as error:
                lines.extend([
                    f"  检测项 P-C1：E{type(error).__name__}",
                    f"  检测项 P-C2：E{type(error).__name__}",
                    f"  检测项 D-C1：E{type(error).__name__}",
                    f"  检测项 F-C1：E{type(error).__name__}",
                    f"  检测项 F-C2：E{type(error).__name__}",
                    f"  检测项 E-C1：E{type(error).__name__}",
                    f"  检测项 R-C1：E{type(error).__name__}",
                    f"  检测项 S-C1：E{type(error).__name__}",
                ])

    lines.extend(["", *fmodd_service_probe(), "", "=" * 64])
    lines.append(f"最终结果：检测到 {len(processes)} 个 fm.exe，其中 {readable_count} 个可读取；已知可用版本：{yes_no(supported)}。")
    if fm24_probe_count or fm26_probe_count:
        lines.append(f"运行检查：FM2024 已执行 {fm24_probe_count} 个；FM2026 已执行 {fm26_probe_count} 个。")
        lines.append("请点击“复制检测报告”，将复制内容原样发送给开发者。")
    else:
        lines.append("运行检查：未执行（没有可读取且能定位核心模块的 FM2024 / FM2026 进程）。")
        lines.append("请确认游戏仍在运行并进入存档，然后点击“重新检测”。")
    return "\n".join(lines)


class DiagnosticWindow(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.code_report = ""
        self.title(APP_TITLE)
        self.geometry("820x620")
        self.minsize(700, 480)
        self.configure(bg="#f2f5f4")

        header = tk.Frame(self, bg="#123d32", padx=18, pady=14)
        header.pack(fill="x")
        tk.Label(header, text=APP_TITLE, bg="#123d32", fg="white", font=("Microsoft YaHei UI", 15, "bold")).pack(anchor="w")

        body = tk.Frame(self, bg="#f2f5f4", padx=14, pady=14)
        body.pack(fill="both", expand=True)

        actions = tk.Frame(body, bg="#f2f5f4", pady=10)
        actions.pack(side="bottom", fill="x")
        self.refresh_button = tk.Button(actions, text="Run Again", width=14, command=self.refresh)
        self.refresh_button.pack(side="left")
        self.copy_button = tk.Button(
            actions, text="Copy Report", width=14, command=self.copy_report,
            state="disabled",
        )
        self.copy_button.pack(side="left", padx=8)
        tk.Button(actions, text="Close", width=10, command=self.destroy).pack(side="right")

        self.output = ScrolledText(body, wrap="word", font=("Microsoft YaHei UI", 12), padx=10, pady=10)
        self.output.pack(side="top", fill="both", expand=True)
        self.output.configure(state="disabled")
        self.after(100, self.refresh)

    def set_report(self, report: str, code_report: str | None = None) -> None:
        if code_report is not None:
            self.code_report = code_report
        self.output.configure(state="normal")
        self.output.delete("1.0", "end")
        self.output.insert("1.0", report)
        self.output.configure(state="disabled")

    def refresh(self) -> None:
        self.code_report = ""
        self.refresh_button.configure(state="disabled", text="Detecting...")
        self.copy_button.configure(state="disabled", text="Copy Report")
        self.set_report(DETECTING_MESSAGE)

        def worker() -> None:
            try:
                report = generate_report()
            except Exception as error:
                report = f"检测工具运行失败：{type(error).__name__}: {error}"
            try:
                code_report = build_code_report(report)
            except Exception as error:
                code_report = build_code_report(
                    f"code-report-error:{type(error).__name__}:{error}", manifests=[],
                )
            # Keep the reversible trusted-developer bundle off screen while
            # retaining it for the explicit Copy Report action.
            def finish() -> None:
                self.set_report(COMPLETION_MESSAGE, code_report)
                self.refresh_button.configure(state="normal", text="Run Again")
                self.copy_button.configure(state="normal", text="Copy Report")

            self.after(0, finish)

        threading.Thread(target=worker, daemon=True).start()

    def copy_report(self) -> None:
        report = self.code_report or build_code_report(
            self.output.get("1.0", "end-1c"), manifests=[],
        )
        self.clipboard_clear()
        self.clipboard_append(report)
        self.update_idletasks()
        self.copy_button.configure(text="Copied")
        self.after(1500, lambda: self.copy_button.configure(text="Copy Report"))


def main() -> int:
    if os.name != "nt":
        raise SystemExit("该检测工具仅支持 Windows。")
    DiagnosticWindow().mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
