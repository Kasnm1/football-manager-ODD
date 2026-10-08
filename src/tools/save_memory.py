from __future__ import annotations

import json
import mmap
import struct
import time
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from threading import RLock
from typing import Any, Iterator

from fm_collector.win32 import MEM_PRIVATE, ModuleInfo, iter_readable_regions
from tools.app_paths import DATA_ROOT
from tools.initial_data_audit import Reader


SAVEGAME_ROOT_RVAS = (0x04E35F60, 0x04E44950)
SAVEGAME_ID_OFFSET = 0xB8
SESSION_NONCE_OFFSET = 0xBC
SAVE_NAME_OWNERS = (
    (".?AVSAVE_SYSTEM@@", 0x20),
    (".?AVGAME_GAME_DATABASE_RECORD@@", 0x08),
)
FM24_SAVE_NAME_OWNERS = (
    (".?AVGAME_SAVE_PROVIDER_LOCAL@sidistribution@@", 0xB0),
)
FM24_LOCAL_SAVE_PATH_OFFSET = 0xB0
FM24_LOCAL_SAVE_LABEL_OFFSET = 0x1F0
KNOWN_SAVE_NAME_VTABLE_RVAS = {
    ".?AVSAVE_SYSTEM@@": (0x44A0FC8, 0x44A0FD8),
    ".?AVGAME_GAME_DATABASE_RECORD@@": (0x4485968, 0x4485AB8),
    ".?AVGAME_SAVE_PROVIDER_LOCAL@sidistribution@@": (0x57FCB28,),
}
SCAN_BLOCK_BYTES = 8 * 1024 * 1024
MAX_PRIVATE_REGION_BYTES = 512 * 1024 * 1024
SAVE_NAME_ADDRESS_CACHE_PATH = DATA_ROOT / "runtime" / "save-name-addresses.v1.json"
_VTABLE_CACHE: dict[tuple[str, int, str], list[int]] = {}
_SAVE_NAME_ADDRESS_CACHE_LOCK = RLock()


@dataclass(frozen=True)
class SaveNameCandidate:
    value: str
    owner_rtti: str
    object_address: int
    vtable_address: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "value": self.value,
            "owner_rtti": self.owner_rtti,
            "object_address": hex(self.object_address),
            "vtable_address": hex(self.vtable_address),
        }


_SAVE_NAME_CACHE: dict[tuple[int, int, int], SaveNameCandidate] = {}


def _persistent_cache_key(
    reader: Reader, module: ModuleInfo, nonce: int,
) -> str:
    return ":".join((
        str(reader.layout.key), str(reader.process.pid), hex(reader.module_base),
        str(int(module.size)), str(int(nonce)),
    ))


def _load_persistent_candidates() -> dict[str, Any]:
    try:
        payload = json.loads(SAVE_NAME_ADDRESS_CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict) or int(payload.get("version") or 0) != 1:
        return {}
    records = payload.get("records")
    return records if isinstance(records, dict) else {}


def _write_persistent_candidate(
    reader: Reader, module: ModuleInfo, nonce: int, candidate: SaveNameCandidate,
) -> None:
    key = _persistent_cache_key(reader, module, nonce)
    with _SAVE_NAME_ADDRESS_CACHE_LOCK:
        records = _load_persistent_candidates()
        records[key] = {
            "layout_key": str(reader.layout.key),
            "pid": int(reader.process.pid),
            "module_base": hex(reader.module_base),
            "module_path": str(module.path),
            "module_size": int(module.size),
            "session_nonce": int(nonce),
            "owner_rtti": candidate.owner_rtti,
            "object_address": hex(candidate.object_address),
            "vtable_address": hex(candidate.vtable_address),
            "updated_at": int(time.time()),
        }
        newest = sorted(
            (
                (record_key, record)
                for record_key, record in records.items()
                if isinstance(record, dict)
            ),
            key=lambda item: int(item[1].get("updated_at") or 0),
            reverse=True,
        )[:32]
        try:
            SAVE_NAME_ADDRESS_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            temporary = SAVE_NAME_ADDRESS_CACHE_PATH.with_suffix(".tmp")
            temporary.write_text(
                json.dumps({"version": 1, "records": dict(newest)}, ensure_ascii=True),
                encoding="utf-8",
            )
            temporary.replace(SAVE_NAME_ADDRESS_CACHE_PATH)
        except OSError:
            # The address cache is only an optimization; live discovery remains authoritative.
            return


def _read_persistent_candidate(
    reader: Reader, module: ModuleInfo, nonce: int,
) -> SaveNameCandidate | None:
    with _SAVE_NAME_ADDRESS_CACHE_LOCK:
        record = _load_persistent_candidates().get(
            _persistent_cache_key(reader, module, nonce),
        )
    if not isinstance(record, dict):
        return None
    try:
        if (
            str(record.get("layout_key") or "") != str(reader.layout.key)
            or int(record.get("pid") or 0) != int(reader.process.pid)
            or int(str(record.get("module_base") or "0"), 16) != int(reader.module_base)
            or int(record.get("module_size") or 0) != int(module.size)
            or str(record.get("module_path") or "").casefold() != str(module.path).casefold()
            or int(record.get("session_nonce") or 0) != int(nonce)
        ):
            return None
        return SaveNameCandidate(
            "",
            str(record["owner_rtti"]),
            int(str(record["object_address"]), 16),
            int(str(record["vtable_address"]), 16),
        )
    except (KeyError, TypeError, ValueError):
        return None


def read_savegame_identity(reader: Reader) -> dict[str, Any] | None:
    """Read FM's stable career ID and same-session nonce directly."""
    layout = reader.layout
    absolute_address = getattr(layout, "savegame_id_absolute_address", None)
    if absolute_address is not None:
        savegame_id = reader.u32(absolute_address)
        if savegame_id and savegame_id <= 0x7FFFFFFF:
            return {
                "savegame_id": str(savegame_id),
                "root_address": hex(absolute_address),
                "source": "fm24_savegame_id_absolute",
                "requires_legacy_validation": bool(
                    layout.savegame_id_requires_legacy_validation
                ),
            }
        return None
    if layout.key == "fm24":
        return None
    for rva in layout.savegame_root_rvas or SAVEGAME_ROOT_RVAS:
        root = reader.ptr(reader.module_base + rva)
        if not root:
            continue
        savegame_id = reader.u32(root + SAVEGAME_ID_OFFSET)
        session_nonce = reader.u32(root + SESSION_NONCE_OFFSET)
        if savegame_id and session_nonce:
            return {
                "savegame_id": str(savegame_id),
                "session_nonce": int(session_nonce),
                "root_address": hex(root),
                "source_rva": hex(rva),
                "source": "fm_savegame_root",
            }
    return None


def _pe_sections(raw: mmap.mmap) -> list[tuple[int, int, int, int]]:
    """Return (RVA, virtual size, file offset, raw size) without copying the DLL."""
    pe_offset = struct.unpack_from("<I", raw, 0x3C)[0]
    if raw[pe_offset:pe_offset + 4] != b"PE\0\0":
        raise ValueError("invalid PE image")
    section_count = struct.unpack_from("<H", raw, pe_offset + 6)[0]
    optional_size = struct.unpack_from("<H", raw, pe_offset + 20)[0]
    table = pe_offset + 24 + optional_size
    sections = []
    for index in range(section_count):
        header = table + index * 40
        virtual_size, virtual_rva, raw_size, raw_offset = struct.unpack_from("<IIII", raw, header + 8)
        sections.append((virtual_rva, virtual_size, raw_offset, raw_size))
    return sections


def _pe_image_base(raw: mmap.mmap) -> int:
    pe_offset = struct.unpack_from("<I", raw, 0x3C)[0]
    optional_header = pe_offset + 24
    magic = struct.unpack_from("<H", raw, optional_header)[0]
    if magic != 0x20B:
        raise ValueError("expected a 64-bit PE image")
    return struct.unpack_from("<Q", raw, optional_header + 24)[0]


def _section_occurrences(
    raw: mmap.mmap, sections: list[tuple[int, int, int, int]], needle: bytes,
) -> Iterator[int]:
    """Yield RVAs for a byte sequence found in section-backed file data."""
    for virtual_rva, _virtual_size, raw_offset, raw_size in sections:
        position = raw_offset
        end = min(len(raw), raw_offset + raw_size)
        while True:
            position = raw.find(needle, position, end)
            if position < 0:
                break
            yield virtual_rva + position - raw_offset
            position += 1


def _read_rva(
    raw: mmap.mmap, sections: list[tuple[int, int, int, int]], rva: int, size: int,
) -> bytes | None:
    for virtual_rva, _virtual_size, raw_offset, raw_size in sections:
        relative = rva - virtual_rva
        if 0 <= relative and relative + size <= raw_size:
            return raw[raw_offset + relative:raw_offset + relative + size]
    return None


def _find_all(raw: bytes, needle: bytes) -> Iterator[int]:
    position = 0
    while True:
        position = raw.find(needle, position)
        if position < 0:
            return
        yield position
        position += 1


def find_vtables_for_rtti(reader: Reader, module: ModuleInfo, rtti_name: str) -> list[int]:
    """Resolve MSVC x64 vtables from a decorated RTTI type name."""
    cache_key = (module.path, reader.module_base, rtti_name)
    if cache_key in _VTABLE_CACHE:
        return list(_VTABLE_CACHE[cache_key])
    known = [reader.module_base + rva for rva in KNOWN_SAVE_NAME_VTABLE_RVAS.get(rtti_name, ())]
    known = [
        vtable for vtable in known
        if (method := reader.ptr(vtable))
        and reader.module_base <= method < reader.module_base + module.size
    ]
    if known:
        _VTABLE_CACHE[cache_key] = known
        return list(known)
    vtables: set[int] = set()
    with Path(module.path).open("rb") as stream:
        with mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as image:
            sections = _pe_sections(image)
            preferred_base = _pe_image_base(image)
            for name_rva in _section_occurrences(
                image, sections, rtti_name.encode("ascii") + b"\0"
            ):
                if name_rva < 16:
                    continue
                type_descriptor_rva = name_rva - 16
                type_reference = struct.pack("<I", type_descriptor_rva)
                for reference_rva in _section_occurrences(image, sections, type_reference):
                    complete_locator_rva = reference_rva - 12
                    locator = _read_rva(image, sections, complete_locator_rva, 24)
                    if not locator:
                        continue
                    signature, _offset, _cd_offset, type_rva, _class_rva, self_rva = struct.unpack(
                        "<IIIIII", locator
                    )
                    if signature != 1 or type_rva != type_descriptor_rva or self_rva != complete_locator_rva:
                        continue
                    # The file contains the preferred image-base pointer; the
                    # loader relocates it to the process module base at runtime.
                    locator_pointer = struct.pack("<Q", preferred_base + complete_locator_rva)
                    for locator_ref_rva in _section_occurrences(image, sections, locator_pointer):
                        vtable = reader.module_base + locator_ref_rva + 8
                        first_method = reader.ptr(vtable)
                        if first_method and reader.module_base <= first_method < reader.module_base + module.size:
                            vtables.add(vtable)
    resolved = sorted(vtables)
    _VTABLE_CACHE[cache_key] = resolved
    return list(resolved)


def _scan_vtable_instances(
    reader: Reader, vtables: list[int], limit: int = 64, *, anchor: int | None = None,
    max_region_bytes: int = MAX_PRIVATE_REGION_BYTES,
) -> Iterator[tuple[int, int]]:
    needles = [(vtable, struct.pack("<Q", vtable)) for vtable in vtables]
    yielded = 0
    regions = [
        region for region in iter_readable_regions(reader.process)
        if region.type == MEM_PRIVATE and 8 <= region.size <= max_region_bytes
    ]
    if anchor:
        # The active save root and SAVE_SYSTEM allocation live in the same FM
        # heap neighbourhood. Search upward from that root before unrelated
        # heaps and DLL-private allocations.
        regions.sort(key=lambda region: (
            0 if region.base_address + region.size >= anchor else 1,
            max(0, region.base_address - anchor)
            if region.base_address + region.size >= anchor
            else anchor - (region.base_address + region.size),
        ))
    else:
        regions.reverse()
    for region in regions:
        carry = b""
        for offset in range(0, region.size, SCAN_BLOCK_BYTES):
            size = min(SCAN_BLOCK_BYTES, region.size - offset)
            raw = reader.bytes(region.base_address + offset, size)
            if not raw:
                carry = b""
                continue
            window = carry + raw
            origin = region.base_address + offset - len(carry)
            for vtable, needle in needles:
                for position in _find_all(window, needle):
                    address = origin + position
                    if reader.ptr(address) != vtable:
                        continue
                    yield address, vtable
                    yielded += 1
                    if yielded >= limit:
                        return
            carry = window[-7:]


def _valid_save_name(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = value.strip()
    if not 1 <= len(cleaned) <= 160:
        return None
    if any(ord(char) < 32 for char in cleaned):
        return None
    return cleaned


def _decode_std_string(reader: Reader, address: int) -> str | None:
    raw = reader.bytes(address, 32)
    if not raw or len(raw) != 32:
        return None
    length = struct.unpack_from("<Q", raw, 16)[0]
    capacity = struct.unpack_from("<Q", raw, 24)[0]
    if not 0 < length <= 160 or capacity < length:
        return None
    payload = raw[:length] if capacity <= 15 else reader.bytes(struct.unpack_from("<Q", raw, 0)[0], length)
    if not payload or len(payload) != length:
        return None
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _read_save_name_field(reader: Reader, address: int) -> str | None:
    for value in (
        reader.fm_string_at(address),
        reader.fm_nested_string_at(address),
        _decode_std_string(reader, address),
    ):
        valid = _valid_save_name(value)
        if valid:
            return valid
    return None


def _read_fm24_local_save_name(reader: Reader, address: int) -> str | None:
    """Return a verified local-save name, never an adjacent provider/UI string."""
    path_value = _read_save_name_field(reader, address + FM24_LOCAL_SAVE_PATH_OFFSET)
    label_value = _read_save_name_field(reader, address + FM24_LOCAL_SAVE_LABEL_OFFSET)
    if not path_value or not label_value:
        return None
    filename = PureWindowsPath(path_value.replace("/", "\\")).name
    if not filename.casefold().endswith(".fm"):
        return None
    stem = filename[:-3].strip()
    if not stem or stem.casefold() != label_value.strip().casefold():
        return None
    return label_value.strip()


def _read_owner_save_name(
    reader: Reader, address: int, owner_rtti: str, name_offset: int,
) -> str | None:
    if owner_rtti == ".?AVGAME_SAVE_PROVIDER_LOCAL@sidistribution@@":
        return _read_fm24_local_save_name(reader, address)
    return _read_save_name_field(reader, address + name_offset)


def find_save_name_candidates(
    reader: Reader, module: ModuleInfo, *, anchor: int | None = None,
) -> list[SaveNameCandidate]:
    candidates: list[SaveNameCandidate] = []
    seen: set[tuple[str, int]] = set()
    owners = FM24_SAVE_NAME_OWNERS if reader.layout.key == "fm24" else SAVE_NAME_OWNERS
    max_region_bytes = 8 * 1024 * 1024 if reader.layout.key == "fm24" else MAX_PRIVATE_REGION_BYTES
    for rtti_name, name_offset in owners:
        vtables = find_vtables_for_rtti(reader, module, rtti_name)
        for address, vtable in _scan_vtable_instances(
            reader, vtables, anchor=anchor, max_region_bytes=max_region_bytes,
        ):
            value = _read_owner_save_name(reader, address, rtti_name, name_offset)
            if not value or (value, address) in seen:
                continue
            seen.add((value, address))
            candidates.append(SaveNameCandidate(value, rtti_name, address, vtable))
            # Descending allocation order makes this the newest valid live
            # instance.  One candidate is sufficient for the preferred owner.
            return candidates
    return candidates


def select_save_name(
    candidates: list[SaveNameCandidate], owners: tuple[tuple[str, int], ...] = SAVE_NAME_OWNERS,
) -> str | None:
    """Prefer the live SAVE_SYSTEM owner, then the database record fallback."""
    for owner, _offset in owners:
        owner_candidates = [item for item in candidates if item.owner_rtti == owner]
        if len(owner_candidates) == 1:
            return owner_candidates[0].value
        if owner_candidates:
            # Live allocations tend to be newer and are normally at the highest
            # address.  Keep candidates visible to diagnostics if this fallback
            # ever needs tightening for a later FM build.
            return max(owner_candidates, key=lambda item: item.object_address).value
    return None


def _validate_cached_candidate(
    reader: Reader, module: ModuleInfo, candidate: SaveNameCandidate,
    owners: tuple[tuple[str, int], ...],
) -> SaveNameCandidate | None:
    owner_offset = dict(owners).get(candidate.owner_rtti)
    if owner_offset is None:
        return None
    if candidate.vtable_address not in find_vtables_for_rtti(
        reader, module, candidate.owner_rtti,
    ):
        return None
    if reader.ptr(candidate.object_address) != candidate.vtable_address:
        return None
    current = _read_owner_save_name(
        reader, candidate.object_address, candidate.owner_rtti, int(owner_offset),
    )
    if not current:
        return None
    return SaveNameCandidate(
        current, candidate.owner_rtti,
        candidate.object_address, candidate.vtable_address,
    )


def read_save_name(
    reader: Reader, module: ModuleInfo, identity: dict[str, Any] | None = None,
) -> tuple[str | None, list[SaveNameCandidate]]:
    cached_name, cached_candidates = read_cached_save_name(
        reader, module, identity,
    )
    if cached_name:
        return cached_name, cached_candidates
    identity = identity or (
        read_savegame_identity(reader) if reader.layout.key == "fm26" else None
    )
    owners = FM24_SAVE_NAME_OWNERS if reader.layout.key == "fm24" else SAVE_NAME_OWNERS
    nonce = int((identity or {}).get("session_nonce") or 0)
    cache_key = (reader.process.pid, reader.module_base, nonce)
    anchor_text = (identity or {}).get("root_address")
    anchor = int(anchor_text, 16) if isinstance(anchor_text, str) else None
    candidates = find_save_name_candidates(reader, module, anchor=anchor)
    selected = select_save_name(candidates, owners)
    if selected and candidates:
        _SAVE_NAME_CACHE[cache_key] = candidates[0]
        _write_persistent_candidate(reader, module, nonce, candidates[0])
    return selected, candidates


def read_cached_save_name(
    reader: Reader, module: ModuleInfo, identity: dict[str, Any] | None = None,
) -> tuple[str | None, list[SaveNameCandidate]]:
    """Validate an existing save-name address without scanning process heaps."""
    identity = identity or (
        read_savegame_identity(reader) if reader.layout.key == "fm26" else None
    )
    owners = FM24_SAVE_NAME_OWNERS if reader.layout.key == "fm24" else SAVE_NAME_OWNERS
    nonce = int((identity or {}).get("session_nonce") or 0)
    cache_key = (reader.process.pid, reader.module_base, nonce)
    cached = _SAVE_NAME_CACHE.get(cache_key)
    refreshed = (
        _validate_cached_candidate(reader, module, cached, owners)
        if cached else None
    )
    if not refreshed:
        persistent = _read_persistent_candidate(reader, module, nonce)
        refreshed = (
            _validate_cached_candidate(reader, module, persistent, owners)
            if persistent else None
        )
    if refreshed:
        _SAVE_NAME_CACHE[cache_key] = refreshed
        return refreshed.value, [refreshed]
    return None, []
