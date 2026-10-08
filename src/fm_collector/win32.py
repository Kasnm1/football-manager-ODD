from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass


kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
psapi = ctypes.WinDLL("psapi", use_last_error=True)

INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

PROCESS_VM_READ = 0x0010
PROCESS_VM_WRITE = 0x0020
PROCESS_VM_OPERATION = 0x0008
PROCESS_CREATE_THREAD = 0x0002
PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

TH32CS_SNAPPROCESS = 0x00000002

MEM_COMMIT = 0x1000
MEM_IMAGE = 0x1000000
MEM_MAPPED = 0x40000
MEM_PRIVATE = 0x20000

PAGE_NOACCESS = 0x01
PAGE_READONLY = 0x02
PAGE_READWRITE = 0x04
PAGE_WRITECOPY = 0x08
PAGE_EXECUTE_READ = 0x20
PAGE_EXECUTE_READWRITE = 0x40
PAGE_EXECUTE_WRITECOPY = 0x80
PAGE_GUARD = 0x100

READABLE_PROTECTIONS = (
    PAGE_READONLY
    | PAGE_READWRITE
    | PAGE_WRITECOPY
    | PAGE_EXECUTE_READ
    | PAGE_EXECUTE_READWRITE
    | PAGE_EXECUTE_WRITECOPY
)

WRITABLE_PROTECTIONS = (
    PAGE_READWRITE
    | PAGE_WRITECOPY
    | PAGE_EXECUTE_READWRITE
    | PAGE_EXECUTE_WRITECOPY
)


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


if ctypes.sizeof(ctypes.c_void_p) == 8:

    class MEMORY_BASIC_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BaseAddress", ctypes.c_void_p),
            ("AllocationBase", ctypes.c_void_p),
            ("AllocationProtect", wintypes.DWORD),
            ("__alignment1", wintypes.DWORD),
            ("RegionSize", ctypes.c_size_t),
            ("State", wintypes.DWORD),
            ("Protect", wintypes.DWORD),
            ("Type", wintypes.DWORD),
            ("__alignment2", wintypes.DWORD),
        ]

else:

    class MEMORY_BASIC_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BaseAddress", ctypes.c_void_p),
            ("AllocationBase", ctypes.c_void_p),
            ("AllocationProtect", wintypes.DWORD),
            ("RegionSize", ctypes.c_size_t),
            ("State", wintypes.DWORD),
            ("Protect", wintypes.DWORD),
            ("Type", wintypes.DWORD),
        ]


class MODULEINFO(ctypes.Structure):
    _fields_ = [
        ("lpBaseOfDll", ctypes.c_void_p),
        ("SizeOfImage", wintypes.DWORD),
        ("EntryPoint", ctypes.c_void_p),
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
    wintypes.HANDLE,
    wintypes.DWORD,
    wintypes.LPWSTR,
    ctypes.POINTER(wintypes.DWORD),
]
kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL

kernel32.VirtualQueryEx.argtypes = [
    wintypes.HANDLE,
    ctypes.c_void_p,
    ctypes.POINTER(MEMORY_BASIC_INFORMATION),
    ctypes.c_size_t,
]
kernel32.VirtualQueryEx.restype = ctypes.c_size_t

kernel32.ReadProcessMemory.argtypes = [
    wintypes.HANDLE,
    ctypes.c_void_p,
    ctypes.c_void_p,
    ctypes.c_size_t,
    ctypes.POINTER(ctypes.c_size_t),
]
kernel32.ReadProcessMemory.restype = wintypes.BOOL
kernel32.WriteProcessMemory.argtypes = [
    wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t),
]
kernel32.WriteProcessMemory.restype = wintypes.BOOL
kernel32.VirtualAllocEx.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_size_t, wintypes.DWORD, wintypes.DWORD]
kernel32.VirtualAllocEx.restype = ctypes.c_void_p
kernel32.VirtualFreeEx.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_size_t, wintypes.DWORD]
kernel32.VirtualFreeEx.restype = wintypes.BOOL
kernel32.VirtualProtectEx.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_size_t, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
kernel32.VirtualProtectEx.restype = wintypes.BOOL
kernel32.FlushInstructionCache.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_size_t]
kernel32.FlushInstructionCache.restype = wintypes.BOOL

psapi.EnumProcessModulesEx.argtypes = [
    wintypes.HANDLE,
    ctypes.POINTER(ctypes.c_void_p),
    wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD),
    wintypes.DWORD,
]
psapi.EnumProcessModulesEx.restype = wintypes.BOOL

psapi.GetModuleBaseNameW.argtypes = [
    wintypes.HANDLE,
    ctypes.c_void_p,
    wintypes.LPWSTR,
    wintypes.DWORD,
]
psapi.GetModuleBaseNameW.restype = wintypes.DWORD

psapi.GetModuleFileNameExW.argtypes = [
    wintypes.HANDLE,
    ctypes.c_void_p,
    wintypes.LPWSTR,
    wintypes.DWORD,
]
psapi.GetModuleFileNameExW.restype = wintypes.DWORD

psapi.GetModuleInformation.argtypes = [
    wintypes.HANDLE,
    ctypes.c_void_p,
    ctypes.POINTER(MODULEINFO),
    wintypes.DWORD,
]
psapi.GetModuleInformation.restype = wintypes.BOOL


@dataclass(frozen=True)
class ProcessInfo:
    pid: int
    name: str
    path: str | None = None
    thread_count: int = 0

    def to_dict(self) -> dict[str, object]:
        return {"pid": self.pid, "name": self.name, "path": self.path, "thread_count": self.thread_count}


@dataclass(frozen=True)
class MemoryRegion:
    base_address: int
    size: int
    state: int
    protect: int
    type: int


@dataclass(frozen=True)
class ModuleInfo:
    name: str
    path: str
    base_address: int
    size: int


class ProcessHandle:
    def __init__(self, pid: int, handle: int):
        self.pid = pid
        self.handle = handle

    def __enter__(self) -> "ProcessHandle":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def close(self) -> None:
        if self.handle:
            kernel32.CloseHandle(self.handle)
            self.handle = 0


def _last_error_message(prefix: str) -> OSError:
    code = ctypes.get_last_error()
    return OSError(code, f"{prefix}: Windows error {code}")


def _query_process_path(handle: int) -> str | None:
    size = wintypes.DWORD(32768)
    buf = ctypes.create_unicode_buffer(size.value)
    ok = kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size))
    if not ok:
        return None
    return buf.value


def find_processes(fragment: str) -> list[ProcessInfo]:
    fragment_lower = fragment.lower()
    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == INVALID_HANDLE_VALUE:
        raise _last_error_message("CreateToolhelp32Snapshot failed")

    results: list[ProcessInfo] = []
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = kernel32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            name = entry.szExeFile
            normalized = name.lower()
            bare = normalized.removesuffix(".exe")
            if fragment_lower in normalized or fragment_lower in bare:
                path = None
                try:
                    with open_process(int(entry.th32ProcessID), read_memory=False) as proc:
                        path = _query_process_path(proc.handle)
                except OSError:
                    path = None
                results.append(ProcessInfo(
                    pid=int(entry.th32ProcessID), name=name, path=path,
                    thread_count=int(entry.cntThreads),
                ))
            ok = kernel32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snap)
    return results


def open_process(
    pid: int, read_memory: bool = True, write_memory: bool = False,
    create_thread: bool = False,
) -> ProcessHandle:
    access = PROCESS_QUERY_INFORMATION | PROCESS_QUERY_LIMITED_INFORMATION
    if read_memory:
        access |= PROCESS_VM_READ
    if write_memory:
        access |= PROCESS_VM_WRITE | PROCESS_VM_OPERATION
    if create_thread:
        access |= PROCESS_CREATE_THREAD
    handle = kernel32.OpenProcess(access, False, pid)
    if not handle:
        raise _last_error_message(f"OpenProcess({pid}) failed")
    return ProcessHandle(pid, handle)


def find_module(process: ProcessHandle, wanted_name: str) -> ModuleInfo | None:
    modules = (ctypes.c_void_p * 2048)()
    needed = wintypes.DWORD(0)
    list_modules_all = 0x03
    ok = psapi.EnumProcessModulesEx(
        process.handle,
        modules,
        ctypes.sizeof(modules),
        ctypes.byref(needed),
        list_modules_all,
    )
    if not ok:
        raise _last_error_message("EnumProcessModulesEx failed")

    count = min(int(needed.value) // ctypes.sizeof(ctypes.c_void_p), len(modules))
    for index in range(count):
        module = modules[index]
        name_buf = ctypes.create_unicode_buffer(1024)
        if not psapi.GetModuleBaseNameW(process.handle, module, name_buf, len(name_buf)):
            continue
        if name_buf.value.casefold() != wanted_name.casefold():
            continue

        path_buf = ctypes.create_unicode_buffer(32768)
        psapi.GetModuleFileNameExW(process.handle, module, path_buf, len(path_buf))
        info = MODULEINFO()
        if not psapi.GetModuleInformation(
            process.handle, module, ctypes.byref(info), ctypes.sizeof(info)
        ):
            raise _last_error_message("GetModuleInformation failed")
        return ModuleInfo(
            name=name_buf.value,
            path=path_buf.value,
            base_address=int(info.lpBaseOfDll or 0),
            size=int(info.SizeOfImage),
        )
    return None


def _is_readable(protect: int) -> bool:
    if protect & PAGE_GUARD:
        return False
    if protect & PAGE_NOACCESS:
        return False
    return bool(protect & READABLE_PROTECTIONS)


def iter_readable_regions(process: ProcessHandle):
    address = 0
    mbi = MEMORY_BASIC_INFORMATION()
    mbi_size = ctypes.sizeof(MEMORY_BASIC_INFORMATION)
    max_address = (1 << (ctypes.sizeof(ctypes.c_void_p) * 8)) - 1

    while address < max_address:
        result = kernel32.VirtualQueryEx(process.handle, ctypes.c_void_p(address), ctypes.byref(mbi), mbi_size)
        if not result:
            break

        base = int(mbi.BaseAddress or 0)
        size = int(mbi.RegionSize)
        if size <= 0:
            break

        if int(mbi.State) == MEM_COMMIT and _is_readable(int(mbi.Protect)):
            yield MemoryRegion(
                base_address=base,
                size=size,
                state=int(mbi.State),
                protect=int(mbi.Protect),
                type=int(mbi.Type),
            )

        next_address = base + size
        if next_address <= address:
            break
        address = next_address


def read_process_memory(process: ProcessHandle, address: int, size: int) -> bytes | None:
    if size <= 0:
        return b""
    buf = ctypes.create_string_buffer(size)
    bytes_read = ctypes.c_size_t(0)
    ok = kernel32.ReadProcessMemory(
        process.handle,
        ctypes.c_void_p(address),
        buf,
        size,
        ctypes.byref(bytes_read),
    )
    if not ok or bytes_read.value == 0:
        return None
    return buf.raw[: bytes_read.value]


def _direct_verified_write(
    process: ProcessHandle, address: int, expected: bytes, replacement: bytes,
) -> None:
    buffer = ctypes.create_string_buffer(replacement)
    written = ctypes.c_size_t(0)
    ok = kernel32.WriteProcessMemory(
        process.handle, ctypes.c_void_p(address), buffer, len(replacement),
        ctypes.byref(written),
    )
    if not ok or written.value != len(replacement):
        raise _last_error_message(f"WriteProcessMemory(0x{address:X}) failed")
    actual = read_process_memory(process, address, len(replacement))
    if actual == replacement:
        return
    if actual != expected:
        rollback = ctypes.create_string_buffer(expected)
        restored = ctypes.c_size_t(0)
        rollback_ok = kernel32.WriteProcessMemory(
            process.handle, ctypes.c_void_p(address), rollback, len(expected),
            ctypes.byref(restored),
        )
        if (
            not rollback_ok
            or restored.value != len(expected)
            or read_process_memory(process, address, len(expected)) != expected
        ):
            raise RuntimeError("direct write verification and rollback both failed")
    raise RuntimeError("direct write verification failed and the original bytes were restored")


def write_process_memory(
    process: ProcessHandle,
    address: int,
    data: bytes,
    *,
    compatibility_fallback: bool = False,
) -> None:
    if not data:
        return
    expected = read_process_memory(process, address, len(data))
    if expected is None or len(expected) != len(data):
        raise OSError(f"Pre-write read at 0x{address:X} failed")
    mbi = MEMORY_BASIC_INFORMATION()
    queried = kernel32.VirtualQueryEx(
        process.handle, ctypes.c_void_p(address), ctypes.byref(mbi), ctypes.sizeof(mbi),
    )
    executable = bool(
        queried and int(mbi.Protect) & (
            PAGE_EXECUTE_READ | PAGE_EXECUTE_READWRITE | PAGE_EXECUTE_WRITECOPY
        )
    )
    if executable:
        from tools.hook_native_core import verified_hook_write

        verified_hook_write(process.handle, address, expected, data)
    else:
        from tools.rust_native_core import NativeWriteError, verified_write

        def write_data() -> None:
            try:
                verified_write(process.handle, address, expected, data)
            except NativeWriteError as error:
                if not compatibility_fallback or error.code != 4:
                    raise
                _direct_verified_write(process, address, expected, data)

        protection = int(mbi.Protect) if queried else 0
        if not compatibility_fallback or protection & WRITABLE_PROTECTIONS:
            write_data()
            return

        old_protection = wintypes.DWORD(0)
        if not queried or not kernel32.VirtualProtectEx(
            process.handle, ctypes.c_void_p(address), len(data), PAGE_READWRITE,
            ctypes.byref(old_protection),
        ):
            raise _last_error_message(
                f"VirtualProtectEx(0x{address:X}, {len(data)}) failed"
            )
        try:
            write_data()
        finally:
            restored = wintypes.DWORD(0)
            if not kernel32.VirtualProtectEx(
                process.handle, ctypes.c_void_p(address), len(data),
                old_protection.value, ctypes.byref(restored),
            ):
                raise _last_error_message(
                    f"VirtualProtectEx restore at 0x{address:X} failed"
                )
