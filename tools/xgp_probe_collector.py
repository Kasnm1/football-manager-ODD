from __future__ import annotations

import ctypes
import hashlib
import os
import platform
import struct
import sys
import tempfile
import threading
import traceback
from ctypes import wintypes
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import messagebox


APP_TITLE = "FMODD XGP 采集器"
PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_VM_READ = 0x0010
TH32CS_SNAPPROCESS = 0x00000002
LIST_MODULES_ALL = 0x03
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
PAGE_SIZE = 0x1000
IMAGE_SCN_MEM_EXECUTE = 0x20000000
IMAGE_SCN_MEM_WRITE = 0x80000000


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


class MODULEINFO(ctypes.Structure):
    _fields_ = [
        ("lpBaseOfDll", ctypes.c_void_p),
        ("SizeOfImage", wintypes.DWORD),
        ("EntryPoint", ctypes.c_void_p),
    ]


kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
psapi = ctypes.WinDLL("psapi", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)

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
kernel32.ReadProcessMemory.argtypes = [
    wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t,
    ctypes.POINTER(ctypes.c_size_t),
]
kernel32.ReadProcessMemory.restype = wintypes.BOOL
kernel32.QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD),
]
kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
psapi.EnumProcessModulesEx.argtypes = [
    wintypes.HANDLE, ctypes.POINTER(ctypes.c_void_p), wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD), wintypes.DWORD,
]
psapi.EnumProcessModulesEx.restype = wintypes.BOOL
psapi.GetModuleBaseNameW.argtypes = [
    wintypes.HANDLE, ctypes.c_void_p, wintypes.LPWSTR, wintypes.DWORD,
]
psapi.GetModuleBaseNameW.restype = wintypes.DWORD
psapi.GetModuleFileNameExW.argtypes = [
    wintypes.HANDLE, ctypes.c_void_p, wintypes.LPWSTR, wintypes.DWORD,
]
psapi.GetModuleFileNameExW.restype = wintypes.DWORD
psapi.GetModuleInformation.argtypes = [
    wintypes.HANDLE, ctypes.c_void_p, ctypes.POINTER(MODULEINFO), wintypes.DWORD,
]
psapi.GetModuleInformation.restype = wintypes.BOOL


KNOWN_RVAS = {
    "fixture_vtable": 0x449EF98,
    "fixture_result_vtable": 0x4332188,
    "season_result_vtable": 0x4330778,
    "team_vtable": 0x44C13A8,
    "national_team_vtable": 0x4592528,
    "nation_vtable": 0x44CDD68,
    "club_vtable": 0x44BA518,
    "competition_vtable": 0x44BFE68,
    "human_manager_vtable": 0x44A51CC,
    "actual_player_vtable": 0x4509828,
    "player_non_player_vtable": 0x4785308,
    "staff_person_vtable": 0x4565018,
    "game_date": 0x4DF3C18,
    "save_root_1": 0x4E35F60,
    "save_root_2": 0x4E44950,
    "match_engine_phase": 0x4E46428,
    "match_engine_mode": 0x4E47B0C,
}


PATTERNS: dict[str, tuple[int | None, ...]] = {
    "red_bull": (0x44, 0x0F, 0xBE, 0xB4, 0x0B, None, None, 0x00, 0x00, 0xE8),
    "ca_growth": (0x41, 0x0F, 0xB7, None, 0x64, 0x02, 0x00, 0x00, 0x66, 0x01, 0xF8),
    "referee": (
        0x44, 0x88, 0x8E, 0x62, 0x02, 0x00, 0x00,
        0x44, 0x88, 0x9E, 0x60, 0x02, 0x00, 0x00,
        0x44, 0x88, 0x96, 0x61, 0x02, 0x00, 0x00,
    ),
    "no_retirement": (0x0F, 0xB7, 0x41, 0x14, 0x3D, 0x80, 0x00, 0x00, 0x00, 0x74),
    "club_work_permit": (0x48, 0x83, 0xEC, None, 0x31, 0xC0, 0x48, 0x39, 0xCA),
    "world_work_permit": (
        0x48, 0x39, 0xCA, 0x0F, 0x84, None, None, None, None, 0x48, 0x89, 0xD6,
    ),
    "green_card_a": (
        None, 0x89, 0xC1, 0x41, 0xFF, None, None, 0x02, 0x00, 0x00,
        0x90, 0x48, 0x83, 0xC4,
    ),
    "green_card_b": (
        None, 0xC0, None, 0x02, None, 0x80, None, 0x01, 0xEB, 0x02,
        None, None, None, None, 0x48, 0x81, 0xC4,
    ),
}


def windows_error(prefix: str) -> OSError:
    code = ctypes.get_last_error()
    return OSError(code, f"{prefix}，Windows 错误 {code}")


def is_admin() -> bool:
    try:
        return bool(shell32.IsUserAnAdmin())
    except OSError:
        return False


def desktop_directory() -> Path:
    buffer = ctypes.create_unicode_buffer(32768)
    if shell32.SHGetFolderPathW(None, 0x10, None, 0, buffer) == 0 and buffer.value:
        return Path(buffer.value)
    return Path.home() / "Desktop"


def find_fm_pids() -> list[int]:
    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snapshot == INVALID_HANDLE_VALUE:
        raise windows_error("无法枚举进程")
    pids: list[int] = []
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            if entry.szExeFile.casefold() == "fm.exe":
                pids.append(int(entry.th32ProcessID))
            ok = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    return pids


def open_read_process(pid: int) -> int:
    access = PROCESS_QUERY_INFORMATION | PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_VM_READ
    handle = kernel32.OpenProcess(access, False, pid)
    if not handle:
        raise windows_error(f"无法以只读方式打开 fm.exe（PID {pid}）")
    return int(handle)


def process_path(handle: int) -> str:
    size = wintypes.DWORD(32768)
    buffer = ctypes.create_unicode_buffer(size.value)
    if not kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
        return "<unavailable>"
    return buffer.value


def find_module(handle: int, wanted: str) -> tuple[int, int, str] | None:
    modules = (ctypes.c_void_p * 4096)()
    needed = wintypes.DWORD()
    if not psapi.EnumProcessModulesEx(
        handle, modules, ctypes.sizeof(modules), ctypes.byref(needed), LIST_MODULES_ALL,
    ):
        raise windows_error("无法枚举游戏模块")
    count = min(needed.value // ctypes.sizeof(ctypes.c_void_p), len(modules))
    for index in range(count):
        module = modules[index]
        name = ctypes.create_unicode_buffer(1024)
        if not psapi.GetModuleBaseNameW(handle, module, name, len(name)):
            continue
        if name.value.casefold() != wanted.casefold():
            continue
        info = MODULEINFO()
        if not psapi.GetModuleInformation(handle, module, ctypes.byref(info), ctypes.sizeof(info)):
            raise windows_error("无法读取 game_plugin.dll 模块信息")
        path = ctypes.create_unicode_buffer(32768)
        psapi.GetModuleFileNameExW(handle, module, path, len(path))
        return int(info.lpBaseOfDll or 0), int(info.SizeOfImage), path.value
    return None


def read_memory(handle: int, address: int, size: int) -> bytes | None:
    if size <= 0:
        return b""
    buffer = ctypes.create_string_buffer(size)
    read = ctypes.c_size_t()
    if not kernel32.ReadProcessMemory(
        handle, ctypes.c_void_p(address), buffer, size, ctypes.byref(read),
    ):
        return None
    return buffer.raw[:read.value] if read.value else None


def parse_pe(handle: int, base: int) -> tuple[dict[str, int], list[dict[str, int | str]]]:
    header = read_memory(handle, base, 0x2000)
    if not header or len(header) < 0x200 or header[:2] != b"MZ":
        raise RuntimeError("无法从进程内存读取 game_plugin.dll PE 头")
    pe_offset = struct.unpack_from("<I", header, 0x3C)[0]
    if header[pe_offset:pe_offset + 4] != b"PE\0\0":
        raise RuntimeError("game_plugin.dll PE 签名无效")
    coff = pe_offset + 4
    machine, section_count, timestamp = struct.unpack_from("<HHI", header, coff)
    optional_size = struct.unpack_from("<H", header, coff + 16)[0]
    optional = coff + 20
    image_size = struct.unpack_from("<I", header, optional + 56)[0]
    entrypoint = struct.unpack_from("<I", header, optional + 16)[0]
    sections: list[dict[str, int | str]] = []
    cursor = optional + optional_size
    for _ in range(section_count):
        if cursor + 40 > len(header):
            extra = read_memory(handle, base, cursor + 40)
            if not extra or cursor + 40 > len(extra):
                break
            header = extra
        name = header[cursor:cursor + 8].split(b"\0", 1)[0].decode("ascii", "replace")
        virtual_size, rva, raw_size = struct.unpack_from("<III", header, cursor + 8)
        characteristics = struct.unpack_from("<I", header, cursor + 36)[0]
        sections.append({
            "name": name, "rva": rva, "virtual_size": virtual_size,
            "raw_size": raw_size, "characteristics": characteristics,
        })
        cursor += 40
    return {
        "machine": machine, "section_count": section_count, "timestamp": timestamp,
        "image_size": image_size, "entrypoint": entrypoint,
    }, sections


def hex_bytes(data: bytes | None) -> str:
    return data.hex(" ").upper() if data else "<READ FAILED>"


def pointer_description(value: int, base: int, size: int) -> str:
    if value == 0:
        return "null"
    if base <= value < base + size:
        return f"module+rva 0x{value - base:X}"
    if value >= 0x10000:
        return "external/private candidate"
    return "small/invalid"


def pattern_matches(data: bytes, pattern: tuple[int | None, ...]) -> list[int]:
    anchor = next(index for index, value in enumerate(pattern) if value is not None)
    needle = bytes((pattern[anchor],))
    hits: list[int] = []
    cursor = 0
    while True:
        found = data.find(needle, cursor)
        if found < 0:
            return hits
        start = found - anchor
        if 0 <= start <= len(data) - len(pattern) and all(
            expected is None or data[start + index] == expected
            for index, expected in enumerate(pattern)
        ):
            hits.append(start)
        cursor = found + 1


def scan_executable_sections(
    handle: int, base: int, sections: list[dict[str, int | str]],
    baseline_file=None, baseline_segments: list[tuple[int, int, int]] | None = None,
    progress=None,
) -> tuple[dict[str, str], dict[str, list[int]], list[str]]:
    section_hashes: dict[str, str] = {}
    all_hits = {name: [] for name in PATTERNS}
    failures: list[str] = []
    overlap = max(len(pattern) for pattern in PATTERNS.values()) - 1
    for section in sections:
        if not int(section["characteristics"]) & IMAGE_SCN_MEM_EXECUTE:
            continue
        name = str(section["name"])
        rva = int(section["rva"])
        size = int(section["virtual_size"])
        digest = hashlib.sha256()
        carry = b""
        offset = 0
        read_total = 0
        if progress:
            progress(f"正在扫描 {name}（0/{size // (1024 * 1024)} MB）...")
        while offset < size:
            block_size = min(4 * 1024 * 1024, size - offset)
            block = read_memory(handle, base + rva + offset, block_size)
            if not block:
                failures.append(f"{name}+0x{offset:X}: read failed")
                offset += block_size
                carry = b""
                continue
            digest.update(block)
            read_total += len(block)
            if (
                baseline_file is not None
                and baseline_segments is not None
                and int(section["characteristics"]) & IMAGE_SCN_MEM_WRITE
            ):
                file_offset = baseline_file.tell()
                baseline_file.write(block)
                baseline_segments.append((rva + offset, len(block), file_offset))
            scan_data = carry + block
            scan_rva = rva + offset - len(carry)
            for pattern_name, pattern in PATTERNS.items():
                for hit in pattern_matches(scan_data, pattern):
                    absolute_rva = scan_rva + hit
                    if absolute_rva not in all_hits[pattern_name]:
                        all_hits[pattern_name].append(absolute_rva)
            carry = scan_data[-overlap:] if len(scan_data) >= overlap else scan_data
            offset += len(block)
            if progress:
                progress(
                    f"正在扫描 {name}（{offset // (1024 * 1024)}/"
                    f"{max(1, size // (1024 * 1024))} MB）..."
                )
        section_hashes[name] = f"{digest.hexdigest().upper()} ({read_total}/{size} bytes)"
    return section_hashes, all_hits, failures


def append_non_executable_writable_snapshot(
    handle: int, base: int, sections: list[dict[str, int | str]], baseline_file,
    baseline_segments: list[tuple[int, int, int]],
) -> list[str]:
    failures: list[str] = []
    for section in sections:
        characteristics = int(section["characteristics"])
        if not characteristics & IMAGE_SCN_MEM_WRITE or characteristics & IMAGE_SCN_MEM_EXECUTE:
            continue
        name = str(section["name"])
        rva = int(section["rva"])
        size = int(section["virtual_size"])
        offset = 0
        while offset < size:
            block_size = min(4 * 1024 * 1024, size - offset)
            block = read_memory(handle, base + rva + offset, block_size)
            if not block:
                failures.append(f"{name}+0x{offset:X}: read failed")
                offset += block_size
                continue
            file_offset = baseline_file.tell()
            baseline_file.write(block)
            baseline_segments.append((rva + offset, len(block), file_offset))
            offset += len(block)
    return failures


def compare_snapshot_file(
    handle: int, base: int, baseline_path: Path,
    segments: list[tuple[int, int, int]], limit: int = 20000,
) -> tuple[list[str], int, int, int]:
    candidates: list[str] = []
    changed_pages: set[int] = set()
    total_changes = 0
    compared_bytes = 0
    with baseline_path.open("rb") as baseline:
        for rva, size, file_offset in segments:
            baseline.seek(file_offset)
            before = baseline.read(size)
            after = read_memory(handle, base + rva, size)
            if not after:
                continue
            compared_bytes += min(len(before), len(after))
            comparable = min(len(before), len(after))
            if before[:comparable] == after[:comparable]:
                continue
            for page_offset in range(0, comparable, PAGE_SIZE):
                page_end = min(page_offset + PAGE_SIZE, comparable)
                before_page = before[page_offset:page_end]
                after_page = after[page_offset:page_end]
                if before_page == after_page:
                    continue
                changed_pages.add((rva + page_offset) & ~(PAGE_SIZE - 1))
                aligned = min(len(before_page), len(after_page)) & ~3
                for offset in range(0, aligned, 4):
                    old = struct.unpack_from("<I", before_page, offset)[0]
                    new = struct.unpack_from("<I", after_page, offset)[0]
                    if old == new:
                        continue
                    total_changes += 1
                    if len(candidates) >= limit:
                        continue
                    if (old <= 0x10000 and new <= 0x10000) or (
                        old == 0 and new >= 0x10000
                    ) or (new == 0 and old >= 0x10000):
                        candidates.append(
                            f"RVA 0x{rva + page_offset + offset:X}: "
                            f"0x{old:X} -> 0x{new:X}"
                        )
    return candidates, len(changed_pages), total_changes, compared_bytes


def capture_writable_pages(
    handle: int, base: int, sections: list[dict[str, int | str]],
) -> tuple[dict[int, bytes], list[str]]:
    pages: dict[int, bytes] = {}
    failures: list[str] = []
    for section in sections:
        characteristics = int(section["characteristics"])
        if not characteristics & IMAGE_SCN_MEM_WRITE or characteristics & IMAGE_SCN_MEM_EXECUTE:
            continue
        rva = int(section["rva"])
        size = int(section["virtual_size"])
        for offset in range(0, size, PAGE_SIZE):
            page_size = min(PAGE_SIZE, size - offset)
            data = read_memory(handle, base + rva + offset, page_size)
            if data:
                pages[rva + offset] = data
            else:
                failures.append(f"{section['name']}+0x{offset:X}: read failed")
    return pages, failures


def compare_writable_pages(
    baseline: dict[int, bytes], current: dict[int, bytes], limit: int = 20000,
) -> tuple[list[str], int, int]:
    candidates: list[str] = []
    changed_pages = 0
    total_changes = 0
    for page_rva in sorted(set(baseline) & set(current)):
        before = baseline[page_rva]
        after = current[page_rva]
        if before == after:
            continue
        changed_pages += 1
        aligned = min(len(before), len(after)) & ~3
        for offset in range(0, aligned, 4):
            old = struct.unpack_from("<I", before, offset)[0]
            new = struct.unpack_from("<I", after, offset)[0]
            if old == new:
                continue
            total_changes += 1
            if len(candidates) >= limit:
                continue
            if (old <= 0x10000 and new <= 0x10000) or (
                old == 0 and new >= 0x10000
            ) or (new == 0 and old >= 0x10000):
                candidates.append(f"RVA 0x{page_rva + offset:X}: 0x{old:X} -> 0x{new:X}")
    return candidates, changed_pages, total_changes


class CollectorApp:
    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title(APP_TITLE)
        self.root.geometry("560x330")
        self.root.resizable(False, False)
        self.root.configure(bg="#16191d")
        self.report_path: Path | None = None
        self.baseline_path: Path | None = None
        self.baseline_segments: list[tuple[int, int, int]] = []
        self.baseline_identity: tuple[int, int, int, int] | None = None
        self.busy = False
        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self.close)

    def _build_ui(self) -> None:
        tk.Label(
            self.root, text=APP_TITLE, bg="#16191d", fg="#f4f5f6",
            font=("Microsoft YaHei UI", 18, "bold"),
        ).pack(pady=(28, 8))
        tk.Label(
            self.root,
            text="保持本窗口打开，先在消息页面采集，再进入一场比赛采集。\n程序仅以只读方式访问 fm.exe。",
            bg="#16191d", fg="#aeb5bd", font=("Microsoft YaHei UI", 10),
            justify="center",
        ).pack(pady=(0, 22))
        buttons = tk.Frame(self.root, bg="#16191d")
        buttons.pack()
        self.inbox_button = tk.Button(
            buttons, text="消息页面采集", command=lambda: self.start_capture("inbox"),
            width=18, height=3, bg="#d6b35a", fg="#111315",
            activebackground="#e2c577", relief="flat",
            font=("Microsoft YaHei UI", 11, "bold"), cursor="hand2",
        )
        self.inbox_button.grid(row=0, column=0, padx=10)
        self.match_button = tk.Button(
            buttons, text="进入比赛后采集", command=lambda: self.start_capture("match"),
            width=18, height=3, bg="#353b42", fg="#f4f5f6",
            activebackground="#454d56", relief="flat",
            font=("Microsoft YaHei UI", 11, "bold"), cursor="hand2",
        )
        self.match_button.grid(row=0, column=1, padx=10)
        self.status = tk.StringVar(value="等待第一次采集")
        tk.Label(
            self.root, textvariable=self.status, bg="#16191d", fg="#d7dbe0",
            font=("Microsoft YaHei UI", 10), wraplength=500, justify="center",
        ).pack(pady=(24, 4))
        self.path_text = tk.StringVar(value="报告将保存到桌面")
        tk.Label(
            self.root, textvariable=self.path_text, bg="#16191d", fg="#7f8994",
            font=("Microsoft YaHei UI", 9), wraplength=520, justify="center",
        ).pack()

    def set_busy(self, value: bool) -> None:
        self.busy = value
        state = tk.DISABLED if value else tk.NORMAL
        self.inbox_button.configure(state=state)
        self.match_button.configure(state=state)

    def start_capture(self, stage: str) -> None:
        if self.busy:
            return
        if stage == "match" and not self.baseline_path:
            messagebox.showwarning(APP_TITLE, "请先在消息页面点击第一次采集，并保持程序打开。")
            return
        self.set_busy(True)
        self.status.set("正在读取游戏进程，请勿关闭 FM 或采集器...")
        threading.Thread(target=self._capture_worker, args=(stage,), daemon=True).start()

    def _capture_worker(self, stage: str) -> None:
        try:
            result = self.collect(stage)
        except Exception as error:
            detail = traceback.format_exc()
            self.root.after(
                0,
                lambda stage=stage, error=error, detail=detail:
                    self.capture_failed(stage, error, detail),
            )
            return
        self.root.after(0, lambda: self.capture_finished(stage, result))

    def _find_target(self) -> tuple[int, int, int, str, str]:
        candidates: list[tuple[int, int, int, str, str]] = []
        errors: list[str] = []
        for pid in find_fm_pids():
            handle = 0
            try:
                handle = open_read_process(pid)
                module = find_module(handle, "game_plugin.dll")
                if module:
                    base, size, module_path = module
                    candidates.append((pid, handle, base, process_path(handle), module_path))
                    handle = 0
            except OSError as error:
                errors.append(str(error))
            finally:
                if handle:
                    kernel32.CloseHandle(handle)
        if not candidates:
            suffix = f"\n{errors[0]}" if errors else ""
            raise RuntimeError("未找到已加载 game_plugin.dll 的 fm.exe。请启动游戏并进入存档。" + suffix)
        if len(candidates) > 1:
            for _pid, handle, _base, _process_path, _module_path in candidates:
                kernel32.CloseHandle(handle)
            raise RuntimeError("检测到多个 FM26 游戏进程，请只保留一个游戏实例。")
        return candidates[0]

    def collect(self, stage: str) -> dict[str, object]:
        pid, handle, base, exe_path, module_path = self._find_target()
        try:
            module = find_module(handle, "game_plugin.dll")
            if not module:
                raise RuntimeError("game_plugin.dll 尚未加载")
            current_base, module_size, _ = module
            pe, sections = parse_pe(handle, current_base)
            identity = (pid, current_base, int(pe["timestamp"]), int(pe["image_size"]))
            if stage == "match" and identity != self.baseline_identity:
                raise RuntimeError("游戏进程或 game_plugin.dll 已变化，请重新进行消息页面采集。")

            lines = self.common_report_lines(
                stage, pid, current_base, module_size, exe_path, module_path, pe, sections,
            )
            if stage == "inbox":
                self.cleanup_baseline()
                baseline = tempfile.NamedTemporaryFile(
                    prefix="FMODD-XGP-baseline-", suffix=".bin", delete=False,
                )
                self.baseline_path = Path(baseline.name)
                self.baseline_segments = []
                self._thread_status("正在扫描代码特征并建立基线，通常需要几分钟...")
                hashes, hits, failures = scan_executable_sections(
                    handle, current_base, sections, baseline, self.baseline_segments,
                    self._thread_status,
                )
                page_failures = append_non_executable_writable_snapshot(
                    handle, current_base, sections, baseline, self.baseline_segments,
                )
                baseline.flush()
                baseline.close()
                lines.extend(["", "[Executable section hashes]"])
                lines.extend(f"{name}: {value}" for name, value in hashes.items())
                lines.extend(["", "[Known code pattern candidates]"])
                for name, positions in hits.items():
                    rendered = ", ".join(f"0x{value:X}" for value in positions[:100]) or "<none>"
                    lines.append(f"{name}: count={len(positions)}; rvas={rendered}")
                if failures:
                    lines.extend(["", "[Executable read failures]", *failures[:200]])
                self.baseline_identity = identity
                lines.extend(["", "[Writable baseline]"])
                baseline_size = self.baseline_path.stat().st_size
                lines.append(
                    f"segments={len(self.baseline_segments)}; bytes={baseline_size}; "
                    f"temporary_file=true"
                )
                if page_failures:
                    lines.extend(["Writable read failures:", *page_failures[:200]])
                self.report_path = desktop_directory() / (
                    f"FMODD-XGP-Probe-{datetime.now():%Y%m%d-%H%M%S}.txt"
                )
                self.write_new_report(lines)
            else:
                self._thread_status("正在比较比赛状态与消息页面基线...")
                if not self.baseline_path or not self.baseline_path.exists():
                    raise RuntimeError("临时基线已经丢失，请重新进行消息页面采集。")
                candidates, changed_pages, total_changes, compared_bytes = compare_snapshot_file(
                    handle, current_base, self.baseline_path, self.baseline_segments,
                )
                lines.extend(["", "[Inbox -> match writable-section diff]"])
                lines.append(f"compared_bytes={compared_bytes}")
                lines.append(f"changed_pages={changed_pages}")
                lines.append(f"changed_u32_values={total_changes}")
                lines.append(f"reported_candidates={len(candidates)}")
                lines.extend(candidates or ["<no candidate changes>"])
                if len(candidates) >= 20000:
                    lines.append("<candidate list truncated at 20000 rows>")
                self.append_report(lines)
                self.cleanup_baseline()
            return {"path": self.report_path, "pid": pid}
        finally:
            kernel32.CloseHandle(handle)

    def common_report_lines(
        self, stage: str, pid: int, base: int, module_size: int, exe_path: str,
        module_path: str, pe: dict[str, int], sections: list[dict[str, int | str]],
    ) -> list[str]:
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        stage_name = "消息页面" if stage == "inbox" else "比赛中"
        lines = [
            "=" * 78,
            f"FMODD XGP PROBE - {stage_name}",
            f"captured_at={now}",
            f"collector_version=2",
            f"administrator={is_admin()}",
            f"windows={platform.platform()}",
            f"python_runtime={platform.python_version()}",
            f"pid={pid}",
            f"exe_path={exe_path}",
            f"module_path={module_path}",
            f"module_base=0x{base:X}",
            f"module_size={module_size} (0x{module_size:X})",
            f"pe_machine=0x{pe['machine']:X}",
            f"pe_timestamp=0x{pe['timestamp']:X}",
            f"pe_size_of_image=0x{pe['image_size']:X}",
            f"pe_entrypoint_rva=0x{pe['entrypoint']:X}",
            "",
            "[PE sections]",
        ]
        for section in sections:
            lines.append(
                f"{section['name']}: rva=0x{int(section['rva']):X}; "
                f"virtual_size=0x{int(section['virtual_size']):X}; "
                f"raw_size=0x{int(section['raw_size']):X}; "
                f"characteristics=0x{int(section['characteristics']):X}"
            )
        lines.extend(["", "[Steam V1.6.2 known-RVA probes]"])
        handle = open_read_process(pid)
        try:
            for name, rva in KNOWN_RVAS.items():
                data = read_memory(handle, base + rva, 64)
                qword = struct.unpack_from("<Q", data, 0)[0] if data and len(data) >= 8 else 0
                dword = struct.unpack_from("<I", data, 0)[0] if data and len(data) >= 4 else 0
                lines.append(
                    f"{name}: rva=0x{rva:X}; u32=0x{dword:X}; u64=0x{qword:X}; "
                    f"pointer={pointer_description(qword, base, module_size)}"
                )
                lines.append(f"  bytes[64]={hex_bytes(data)}")
        finally:
            kernel32.CloseHandle(handle)
        return lines

    @staticmethod
    def pages_hash(pages: dict[int, bytes]) -> str:
        digest = hashlib.sha256()
        for rva, data in sorted(pages.items()):
            digest.update(struct.pack("<Q", rva))
            digest.update(data)
        return digest.hexdigest().upper()

    def cleanup_baseline(self) -> None:
        path = self.baseline_path
        self.baseline_path = None
        self.baseline_segments = []
        if path:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass

    def close(self) -> None:
        self.cleanup_baseline()
        self.root.destroy()

    def write_new_report(self, lines: list[str]) -> None:
        if not self.report_path:
            raise RuntimeError("报告路径尚未建立")
        self.report_path.write_text("\ufeff" + "\n".join(lines) + "\n", encoding="utf-8")

    def append_report(self, lines: list[str]) -> None:
        if not self.report_path:
            raise RuntimeError("第一次采集报告不存在")
        with self.report_path.open("a", encoding="utf-8") as report:
            report.write("\n" + "\n".join(lines) + "\n")

    def _thread_status(self, value: str) -> None:
        self.root.after(0, lambda: self.status.set(value))

    def capture_finished(self, stage: str, result: dict[str, object]) -> None:
        self.set_busy(False)
        path = Path(result["path"])
        self.path_text.set(str(path))
        if stage == "inbox":
            self.inbox_button.configure(text="消息页面已采集", bg="#4f8a67", fg="#ffffff")
            self.status.set("第一次采集完成。保持本窗口打开，进入一场比赛后点击右侧按钮。")
        else:
            self.match_button.configure(text="比赛状态已采集", bg="#4f8a67", fg="#ffffff")
            self.status.set("两阶段采集完成，TXT 报告已保存到桌面。")
            messagebox.showinfo(APP_TITLE, f"采集完成：\n{path}")

    def capture_failed(self, stage: str, error: Exception, detail: str) -> None:
        self.set_busy(False)
        if stage == "inbox":
            self.cleanup_baseline()
        self.status.set(f"采集失败：{error}")
        if self.report_path:
            try:
                with self.report_path.open("a", encoding="utf-8") as report:
                    report.write("\n[COLLECTOR ERROR]\n" + detail + "\n")
            except OSError:
                pass
        messagebox.showerror(APP_TITLE, str(error))

    def run(self) -> None:
        if not is_admin():
            self.status.set("当前没有管理员权限；若无法读取 XGP 进程，请以管理员身份重新运行。")
        self.root.mainloop()


def main() -> int:
    if os.name != "nt":
        print("This collector only supports Windows.", file=sys.stderr)
        return 1
    CollectorApp().run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
