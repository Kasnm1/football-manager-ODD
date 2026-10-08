"""Session-local indexes for Football Manager's native database tables.

The database root is resolved from the generation-specific tdg6661 AOB and is
validated against live table/vector structure.  Stable UIDs are indexed while
native addresses remain session hints: every business reader still validates
the target object's vtable, UID and ownership before use.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import date, timedelta
from hashlib import blake2b
import mmap
from pathlib import Path
import re
import struct
import threading
import unicodedata
from time import monotonic
from typing import Any

from fm_collector.win32 import MEM_IMAGE, iter_readable_regions, read_process_memory


DATABASE_TABLE_SLOTS = {
    "club": 0x10,
    "competition": 0x18,
    "nation": 0x28,
    "language": 0x50,
    "national_team_container": 0x60,
    "person": 0x68,
    "region": 0x78,
    "stadium": 0x80,
    "team": 0x98,
}
TABLE_COUNT_LIMITS = {
    "club": 100_000,
    "competition": 100_000,
    "nation": 10_000,
    "language": 1_000,
    "national_team_container": 10_000,
    # Large databases can exceed one million native Person rows once staff
    # and non-player records are included. Keep the pointer table bounded to
    # 16 MiB while accepting the verified 1,034,663-row FM26 sample.
    "person": 2_000_000,
    "region": 100_000,
    "stadium": 100_000,
    "team": 250_000,
}
TABLE_RECHECK_SECONDS = 2.0
PERSON_SEARCH_QUERY_CACHE_LIMIT = 16
PERSON_SEARCH_QUERY_CACHE_MAX_MATCHES = 25_000
ENTITY_UID_OFFSET = 0x0C
TEAM_CLUB_OFFSET = 0x30
_COMPACT_SEARCH_PATTERN = re.compile(r"[\W_]+", re.UNICODE)
_CONTROL_CHARACTER_PATTERN = re.compile(r"[\x00-\x1f]")


def _matches(raw: bytes | mmap.mmap, offset: int, pattern: tuple[int | None, ...]) -> bool:
    if offset < 0 or offset + len(pattern) > len(raw):
        return False
    return all(value is None or raw[offset + index] == value for index, value in enumerate(pattern))


def _longest_anchor(pattern: tuple[int | None, ...]) -> tuple[int, bytes]:
    best_start = current_start = 0
    best = bytearray()
    current = bytearray()
    for index, value in enumerate((*pattern, None)):
        if value is not None:
            if not current:
                current_start = index
            current.append(value)
            continue
        if len(current) > len(best):
            best_start, best = current_start, bytearray(current)
        current.clear()
    return best_start, bytes(best)


def _pattern_offsets(raw: bytes | mmap.mmap, pattern: tuple[int | None, ...]) -> list[int]:
    anchor_start, anchor = _longest_anchor(pattern)
    if not anchor:
        return []
    offsets: list[int] = []
    cursor = 0
    while True:
        position = raw.find(anchor, cursor)
        if position < 0:
            break
        candidate = position - anchor_start
        if _matches(raw, candidate, pattern):
            offsets.append(candidate)
        cursor = position + 1
    return offsets


def _pe_sections(raw: bytes | mmap.mmap) -> list[tuple[int, int, int, int]]:
    if len(raw) < 0x100 or raw[:2] != b"MZ":
        return []
    pe_offset = struct.unpack_from("<I", raw, 0x3C)[0]
    if pe_offset + 24 > len(raw) or raw[pe_offset:pe_offset + 4] != b"PE\0\0":
        return []
    section_count = struct.unpack_from("<H", raw, pe_offset + 6)[0]
    optional_size = struct.unpack_from("<H", raw, pe_offset + 20)[0]
    table = pe_offset + 24 + optional_size
    sections = []
    for index in range(section_count):
        offset = table + index * 40
        if offset + 40 > len(raw):
            return []
        virtual_size, virtual_address, raw_size, raw_address = struct.unpack_from(
            "<IIII", raw, offset + 8,
        )
        sections.append((raw_address, raw_size, virtual_address, virtual_size))
    return sections


def _file_pattern_rvas(path: str, pattern: tuple[int | None, ...]) -> list[int]:
    target = Path(str(path or ""))
    if not target.is_file():
        return []
    try:
        with target.open("rb") as stream, mmap.mmap(
            stream.fileno(), 0, access=mmap.ACCESS_READ,
        ) as image:
            sections = _pe_sections(image)
            rvas = []
            for file_offset in _pattern_offsets(image, pattern):
                for raw_address, raw_size, virtual_address, _virtual_size in sections:
                    if raw_address <= file_offset < raw_address + raw_size:
                        rvas.append(virtual_address + file_offset - raw_address)
                        break
            return sorted(set(rvas))
    except (OSError, ValueError):
        return []


def _memory_pattern_rvas(reader: Any, pattern: tuple[int | None, ...]) -> list[int]:
    module_start = int(reader.module_base)
    module_end = module_start + int(reader.module.size)
    overlap = max(0, len(pattern) - 1)
    hits: set[int] = set()
    for region in iter_readable_regions(reader.process):
        if (
            region.type != MEM_IMAGE
            or region.base_address < module_start
            or region.base_address >= module_end
        ):
            continue
        region_size = min(int(region.size), module_end - int(region.base_address))
        carry = b""
        for relative in range(0, region_size, 8 * 1024 * 1024):
            length = min(8 * 1024 * 1024, region_size - relative)
            block = read_process_memory(reader.process, region.base_address + relative, length)
            if not block:
                carry = b""
                continue
            scan = carry + block
            scan_base = int(region.base_address) + relative - len(carry)
            for offset in _pattern_offsets(scan, pattern):
                hits.add(scan_base + offset - module_start)
            carry = scan[-overlap:] if overlap else b""
    return sorted(hits)


def _resolve_relative_global(
    reader: Any, pattern: tuple[int | None, ...], displacement_offset: int | None,
    instruction_size: int | None, preferred_rva: int | None = None,
) -> int:
    if not pattern or displacement_offset is None or instruction_size is None:
        return 0
    rvas = [int(preferred_rva)] if preferred_rva is not None else []
    rvas.extend(_file_pattern_rvas(str(reader.module.path or ""), pattern) if not rvas else [])
    verified = []
    for rva in rvas:
        raw = reader.bytes(reader.module_base + rva, len(pattern))
        if raw and _matches(raw, 0, pattern):
            verified.append(int(rva))
    if not verified:
        verified = _memory_pattern_rvas(reader, pattern)
    verified = sorted(set(verified))
    if len(verified) != 1:
        return 0
    hit = int(reader.module_base) + verified[0]
    raw = reader.bytes(hit, max(len(pattern), int(displacement_offset) + 4))
    if not raw or not _matches(raw, 0, pattern):
        return 0
    displacement = struct.unpack_from("<i", raw, int(displacement_offset))[0]
    target = hit + int(instruction_size) + displacement
    module_end = int(reader.module_base) + int(reader.module.size)
    return target if int(reader.module_base) <= target < module_end else 0


def _decode_person_birth_date(layout: Any, raw: bytes | None) -> date | None:
    if not raw or len(raw) != 4:
        return None
    if bool(getattr(layout, "person_date_of_birth_day_year", False)):
        day_of_year, year = struct.unpack("<HH", raw)
    else:
        encoded = struct.unpack("<I", raw)[0]
        year, day_of_year = encoded >> 16, encoded & 0x1FF
    if not 1800 <= int(year) <= 2200 or not 1 <= int(day_of_year) <= 366:
        return None
    try:
        value = date(int(year), 1, 1) + timedelta(days=int(day_of_year) - 1)
    except ValueError:
        return None
    return value if value.year == int(year) else None


def _age_on(birth: date | None, current: date | None) -> int | None:
    if birth is None or current is None:
        return None
    return current.year - birth.year - (
        (current.month, current.day) < (birth.month, birth.day)
    )


def _validate_player_filter_ranges(
    min_age: int, max_age: int, min_ca: int, max_ca: int,
    min_pa: int, max_pa: int,
) -> None:
    for label, minimum, maximum, ceiling in (
        ("年龄", min_age, max_age, 100),
        ("CA", min_ca, max_ca, 200),
        ("PA", min_pa, max_pa, 200),
    ):
        minimum, maximum = int(minimum or 0), int(maximum or 0)
        if minimum < 0 or maximum < 0 or minimum > ceiling or maximum > ceiling:
            raise ValueError(f"{label}筛选范围无效")
        if minimum and maximum and minimum > maximum:
            raise ValueError(f"{label}最低值不能高于最高值")


@dataclass
class NativeTableIndex:
    name: str
    table_object: int
    vector: int
    begin: int
    end: int
    capacity: int
    fingerprint: str
    addresses: tuple[int, ...]
    checked_at: float = field(default_factory=monotonic)
    uid_addresses: dict[int, tuple[int, ...]] | None = None
    vtable_addresses: dict[int, tuple[int, ...]] | None = None


@dataclass(frozen=True)
class PersonSearchRow:
    uid: int
    address: int
    player_address: int
    object_type: str
    display_name: str
    first_name: str
    last_name: str
    common_name: str
    full_name: str
    searchable_names: str
    compact_searchable_names: str
    normalized_display_name: str = ""
    ca: int | None = None
    pa: int | None = None
    asking_price: int | None = None
    female: bool | None = None
    nationality_id: int | None = None
    world_reputation: int | None = None
    date_of_birth: date | None = None

    def public(self, *, game_date: date | None = None) -> dict[str, Any]:
        result = {
            "id": self.uid,
            "name": self.display_name,
            "first_name": self.first_name or None,
            "last_name": self.last_name or None,
            "common_name": self.common_name or None,
            "full_name": self.full_name or None,
            "address": hex(self.player_address),
            "person_address": hex(self.address),
            "object_type": self.object_type,
            "ca": self.ca,
            "pa": self.pa,
            "asking_price": self.asking_price,
            "nationality_id": self.nationality_id,
            "world_reputation": self.world_reputation,
        }
        if self.date_of_birth is not None:
            result["date_of_birth"] = self.date_of_birth.isoformat()
            if game_date is not None:
                result["age"] = _age_on(self.date_of_birth, game_date)
        return result


@dataclass(frozen=True)
class ResolvedTeamClub:
    """A validated Team -> Club pair for one live reader session.

    Addresses are deliberately not persisted.  The optional database index is
    the preferred relocation source, while the caller's address remains a
    compatibility fallback for readers that predate the session index.
    """

    team_address: int
    club_address: int
    team_id: int


def _normalized_search_text(value: Any) -> str:
    return " ".join(
        unicodedata.normalize("NFKC", str(value or "")).casefold().split()
    )


def _compact_search_text(value: Any) -> str:
    return _COMPACT_SEARCH_PATTERN.sub("", _normalized_search_text(value))


def _contains_cjk(value: str) -> bool:
    return any("\u3400" <= char <= "\u9fff" for char in value)


class DatabaseIndex:
    """FMRTE-style object directory owned by one live FM process session."""

    def __init__(self, reader: Any) -> None:
        self.reader = reader
        self.layout = reader.layout
        self._lock = threading.RLock()
        self.root_slot = _resolve_relative_global(
            reader,
            tuple(getattr(self.layout, "database_root_pattern", ()) or ()),
            getattr(self.layout, "database_root_rel32_offset", None),
            getattr(self.layout, "database_root_instruction_size", None),
            getattr(self.layout, "database_root_probe_rva", None),
        )
        if not self.root_slot:
            raise RuntimeError("native database root AOB is unavailable or non-unique")
        self.human_manager_slot = _resolve_relative_global(
            reader,
            tuple(getattr(self.layout, "human_manager_root_pattern", ()) or ()),
            getattr(self.layout, "human_manager_root_rel32_offset", None),
            getattr(self.layout, "human_manager_root_instruction_size", None),
            getattr(self.layout, "human_manager_root_probe_rva", None),
        )
        self._tables: dict[str, NativeTableIndex] = {}
        self._build_locks = {
            name: threading.Lock() for name in DATABASE_TABLE_SLOTS
        }
        self._human_manager_checked_at = 0.0
        self._human_manager_fingerprint = ""
        self._human_managers: dict[int, tuple[int, ...]] = {}
        self._person_search_build_lock = threading.Lock()
        self._person_search_rows: tuple[PersonSearchRow, ...] | None = None
        self._person_search_by_uid: dict[int, PersonSearchRow] | None = None
        self._person_search_token: tuple[int, int, str] | None = None
        self._person_search_view_lock = threading.RLock()
        self._person_search_views: dict[
            tuple[int, str, str], tuple[PersonSearchRow, ...]
        ] = {}
        self._person_search_query_views: OrderedDict[
            tuple[Any, ...], tuple[PersonSearchRow, ...]
        ] = OrderedDict()
        self._person_search_build_duration_ms = 0
        self._person_search_building = False
        self._player_uid_build_lock = threading.Lock()
        self._player_targets_by_uid: dict[
            int, tuple[int, int, str]
        ] | None = None
        self._player_target_token: tuple[int, int, str] | None = None
        self._public_catalog_lock = threading.RLock()
        self._public_language_catalog_token: tuple[int, int, str] | None = None
        self._public_language_catalog_rows: tuple[dict[str, Any], ...] = ()
        # Tables are validated independently on first use. A corrupt or
        # transiently unavailable table must not block unrelated directories.

    def _table_header(self, name: str) -> tuple[int, int, int, int, int]:
        slot = DATABASE_TABLE_SLOTS[name]
        table_object = int(self.reader.ptr(self.root_slot + slot) or 0)
        vector = int(self.reader.ptr(table_object + 0x80) or 0) if table_object else 0
        raw = self.reader.bytes(vector, 0x18) if vector else None
        if not raw or len(raw) != 0x18:
            raise RuntimeError(f"native {name} table header is unreadable")
        begin, end, capacity = struct.unpack("<QQQ", raw)
        maximum = int(TABLE_COUNT_LIMITS[name])
        if (
            not begin or end < begin or capacity < end
            or (end - begin) % 8 or (capacity - begin) % 8
            or (end - begin) // 8 > maximum
        ):
            raise RuntimeError(f"native {name} table header is invalid")
        return table_object, vector, begin, end, capacity

    def _refresh_table(self, name: str, *, force: bool = False) -> NativeTableIndex:
        with self._lock:
            current = self._tables.get(name)
            now = monotonic()
            if current and not force and now - current.checked_at < TABLE_RECHECK_SECONDS:
                return current
            table_object, vector, begin, end, capacity = self._table_header(name)
            raw = self.reader.bytes(begin, end - begin) if end > begin else b""
            if raw is None or len(raw) != end - begin:
                raise RuntimeError(f"native {name} pointer table is unreadable")
            if self._table_header(name) != (table_object, vector, begin, end, capacity):
                raise RuntimeError(f"native {name} table changed during snapshot")
            fingerprint = blake2b(raw, digest_size=16).hexdigest()
            if (
                current
                and current.table_object == table_object
                and current.vector == vector
                and current.begin == begin
                and current.end == end
                and current.capacity == capacity
                and current.fingerprint == fingerprint
            ):
                current.checked_at = now
                return current
            addresses = tuple(
                address for (address,) in struct.iter_unpack("<Q", raw)
                if address and address % 8 == 0
            )
            updated = NativeTableIndex(
                name=name, table_object=table_object, vector=vector,
                begin=begin, end=end, capacity=capacity,
                fingerprint=fingerprint, addresses=addresses, checked_at=now,
            )
            self._tables[name] = updated
            return updated

    def _expected_vtables(self, name: str) -> set[int] | None:
        mapping = {
            "club": (self.layout.club_vtable_rva,),
            "competition": (self.layout.competition_vtable_rva,),
            "nation": (self.layout.nation_vtable_rva,),
            "team": (self.layout.team_vtable_rva, self.layout.national_team_vtable_rva),
        }
        values = mapping.get(name)
        return {
            int(self.reader.module_base) + int(value)
            for value in values or () if value is not None
        } or None

    def _build_objects(self, table: NativeTableIndex) -> NativeTableIndex:
        if table.uid_addresses is not None and table.vtable_addresses is not None:
            return table
        # Snapshotting a large Team or Person pool can take seconds.  Serialize
        # only builds of the same table; keeping the directory-wide lock free
        # lets manager discovery and already-built tables remain responsive.
        with self._build_locks[table.name]:
            if table.uid_addresses is not None and table.vtable_addresses is not None:
                return table
            snapshots = self.reader._fixed_size_snapshots(table.addresses, 0x10)
            expected = self._expected_vtables(table.name)
            uid_rows: dict[int, list[int]] = {}
            vtable_rows: dict[int, list[int]] = {}
            module_start = int(self.reader.module_base)
            module_end = module_start + int(self.reader.module.size)
            for address in table.addresses:
                raw = snapshots.get(address)
                if not raw or len(raw) < 0x10:
                    continue
                vtable = struct.unpack_from("<Q", raw, 0)[0]
                uid = struct.unpack_from("<I", raw, 0x0C)[0]
                if not uid or not module_start <= vtable < module_end:
                    continue
                if expected is not None and vtable not in expected:
                    continue
                uid_rows.setdefault(int(uid), []).append(int(address))
                vtable_rows.setdefault(int(vtable - module_start), []).append(int(address))
            with self._lock:
                table.uid_addresses = {
                    uid: tuple(addresses) for uid, addresses in uid_rows.items()
                }
                table.vtable_addresses = {
                    rva: tuple(addresses) for rva, addresses in vtable_rows.items()
                }
            return table

    def addresses(self, name: str, *, force: bool = False) -> tuple[int, ...]:
        return self._refresh_table(name, force=force).addresses

    def table_token(self, name: str) -> tuple[int, int, str]:
        table = self._refresh_table(name)
        return table.table_object, table.vector, table.fingerprint

    def table_ready(self, name: str) -> bool:
        """Return whether the current table already has its object index."""
        with self._lock:
            table = self._tables.get(name)
            return table is not None and table.vtable_addresses is not None

    def addresses_for_uid(self, name: str, uid: int) -> tuple[int, ...]:
        table = self._build_objects(self._refresh_table(name))
        return tuple((table.uid_addresses or {}).get(int(uid), ()))

    def addresses_for_vtable(self, name: str, rva: int) -> tuple[int, ...]:
        table = self._build_objects(self._refresh_table(name))
        return tuple((table.vtable_addresses or {}).get(int(rva), ()))

    def _player_person_groups(
        self, table: NativeTableIndex,
    ) -> tuple[tuple[str, int, tuple[int, ...]], ...]:
        """Classify Person vtable groups from their verified outer player object."""
        layout = self.layout
        player_offset = getattr(layout, "player_person_offset", None)
        hybrid_offset = getattr(layout, "player_and_non_player_person_offset", None)
        if player_offset is None or hybrid_offset is None:
            raise RuntimeError("player/person object offsets are unavailable")
        candidates = (
            (
                "actual_player", int(player_offset),
                {
                    int(self.reader.module_base) + int(rva)
                    for rva in getattr(layout, "actual_player_vtable_rvas", ())
                },
            ),
            (
                "actual_player_and_non_player",
                int(hybrid_offset),
                {
                    int(self.reader.module_base) + int(rva)
                    for rva in getattr(layout, "player_and_non_player_vtable_rvas", ())
                },
            ),
        )
        sample_addresses: set[int] = set()
        group_samples: dict[int, tuple[int, ...]] = {}
        for rva, addresses in (table.vtable_addresses or {}).items():
            if not addresses:
                continue
            sample_count = min(6, len(addresses))
            indexes = {
                min(
                    len(addresses) - 1,
                    round(index * (len(addresses) - 1) / max(1, sample_count - 1)),
                )
                for index in range(sample_count)
            }
            samples = tuple(addresses[index] for index in sorted(indexes))
            group_samples[int(rva)] = samples
            for person in samples:
                for _kind, offset, expected in candidates:
                    if expected and person > offset:
                        sample_addresses.add(person - offset)
        snapshots = self.reader._fixed_size_snapshots(sample_addresses, 8)
        groups: list[tuple[str, int, tuple[int, ...]]] = []
        for rva, addresses in (table.vtable_addresses or {}).items():
            matched: list[tuple[str, int]] = []
            for kind, offset, expected in candidates:
                if not expected:
                    continue
                verdicts = []
                for person in group_samples.get(int(rva), ()):
                    raw = snapshots.get(person - offset)
                    vtable = (
                        struct.unpack_from("<Q", raw, 0)[0]
                        if raw and len(raw) >= 8 else 0
                    )
                    verdicts.append(vtable in expected)
                if verdicts and all(verdicts):
                    matched.append((kind, offset))
            # A shared or inconsistent vtable group is not safe to expose.
            if len(matched) == 1:
                kind, offset = matched[0]
                groups.append((kind, offset, tuple(addresses)))
        return tuple(groups)

    def _build_player_target_index(
        self,
    ) -> dict[int, tuple[int, int, str]]:
        """Index verified player targets without constructing search text."""
        with self._player_uid_build_lock:
            table = self._build_objects(self._refresh_table("person"))
            token = (table.table_object, table.vector, table.fingerprint)
            with self._lock:
                if (
                    self._player_targets_by_uid is not None
                    and self._player_target_token == token
                ):
                    return self._player_targets_by_uid
            uid_by_person: dict[int, int] = {}
            for uid, addresses in (table.uid_addresses or {}).items():
                for person in addresses:
                    uid_by_person.setdefault(int(person), int(uid))
            targets: dict[int, tuple[int, int, str]] = {}
            for kind, offset, addresses in self._player_person_groups(table):
                for person in addresses:
                    person = int(person)
                    uid = int(uid_by_person.get(person) or 0)
                    if uid > 0 and person > int(offset):
                        targets.setdefault(
                            uid, (person, person - int(offset), str(kind)),
                        )
            latest = self._refresh_table("person", force=True)
            latest_token = (
                latest.table_object, latest.vector, latest.fingerprint,
            )
            if latest_token != token:
                raise RuntimeError(
                    "native person table changed while building player UID index"
                )
            with self._lock:
                self._player_targets_by_uid = targets
                self._player_target_token = token
            return targets

    def person_uids_snapshot(self) -> tuple[int, ...]:
        """Read a complete existing-person UID baseline without player filtering.

        Reuse the registered Person table underlying world-player search. For
        an exclusion baseline, staff and other valid Person types are harmless
        supersets; dropping an unreadable object or unclassified player is not.
        Never publish or cache a partial snapshot as complete.
        """
        invalidate = getattr(self.reader, "invalidate_prefetch", None)
        if callable(invalidate):
            invalidate()
        table = self._refresh_table("person", force=True)
        token = (table.table_object, table.vector, table.fingerprint)
        raw = self.reader.bytes(table.begin, table.end - table.begin)
        if raw is None or blake2b(raw, digest_size=16).hexdigest() != table.fingerprint:
            raise RuntimeError("native person table changed before UID baseline")
        pointers = tuple(address for (address,) in struct.iter_unpack("<Q", raw) if address)
        if any(address % 8 for address in pointers):
            raise RuntimeError("native person UID baseline contains an invalid pointer")
        headers = self.reader._fixed_size_snapshots(pointers, 0x10)
        module_start = int(self.reader.module_base)
        module_end = module_start + int(self.reader.module.size)
        uid_addresses: dict[int, int] = {}
        for person in pointers:
            header = headers.get(person)
            if not header or len(header) != 0x10:
                raise RuntimeError("native person UID baseline contains an unreadable object")
            vtable, _padding, uid = struct.unpack("<QII", header)
            if not uid or not module_start <= vtable < module_end:
                raise RuntimeError("native person UID baseline contains an invalid identity")
            previous = uid_addresses.setdefault(uid, person)
            if previous != person:
                raise RuntimeError("native person UID baseline contains ambiguous identities")
        if callable(invalidate):
            invalidate()
        latest = self._refresh_table("person", force=True)
        if (latest.table_object, latest.vector, latest.fingerprint) != token:
            raise RuntimeError("native person table changed during UID baseline")
        return tuple(sorted(uid_addresses))

    def player_uids(self) -> tuple[int, ...]:
        """Return all verified player Person UIDs without building name data."""
        return tuple(sorted(self._build_player_target_index()))

    def player_targets(self) -> dict[int, tuple[int, int, str]]:
        """Return verified UID -> (Person, player, object type) targets."""
        return dict(self._build_player_target_index())

    def player_target_for_uid(self, uid: int) -> tuple[int, int, str] | None:
        """Return one verified (Person, Player, object type) target by UID.

        This is the address-only counterpart to ``player_for_uid``.  Movement
        and other write paths still perform their own live object, ownership,
        and expected-value checks; this helper only avoids copying the entire
        world-player mapping or reading display names for one lookup.
        """
        target = int(uid)
        if target <= 0:
            return None
        return self._build_player_target_index().get(target)

    def _single_player_row(
        self, uid: int, target: tuple[int, int, str],
    ) -> dict[str, Any] | None:
        """Read one player's live names for an exact-UID lookup."""
        person, player, kind = target
        layout = self.layout
        full = str(
            self.reader.fm_string_at(
                int(person) + int(layout.person_full_name_offset)
            ) or ""
        )
        first = str(
            self.reader.fm_nested_string_at(
                int(person) + int(layout.person_first_name_offset)
            ) or ""
        )
        last = str(
            self.reader.fm_nested_string_at(
                int(person) + int(layout.person_last_name_offset)
            ) or ""
        )
        common = str(
            self.reader.fm_nested_string_at(
                int(person) + int(layout.person_common_name_offset)
            ) or ""
        )
        joined = " ".join(part for part in (first, last) if part)
        localized = ""
        if str(getattr(layout, "key", "")).startswith("fm24"):
            localized = next(
                (
                    value for value in (full, common, joined)
                    if _contains_cjk(value)
                ),
                "",
            )
        display = localized or common or joined or full
        if not display:
            return None
        return {
            "id": int(uid),
            "name": display,
            "first_name": first or None,
            "last_name": last or None,
            "common_name": common or None,
            "full_name": full or None,
            "address": hex(int(player)),
            "person_address": hex(int(person)),
            "object_type": str(kind),
            "ca": None,
            "pa": None,
            "asking_price": None,
            "nationality_id": None,
            "world_reputation": None,
        }

    def public_language_catalog(self) -> list[dict[str, Any]]:
        """Return a fingerprint-bound public language projection for this session."""
        table = self._refresh_table("language")
        token = (table.table_object, table.vector, table.fingerprint)
        with self._public_catalog_lock:
            if (
                self._public_language_catalog_token == token
                and self._public_language_catalog_rows
            ):
                return [dict(row) for row in self._public_language_catalog_rows]

        from tools.player_languages import (
            _valid_language_object, public_language_catalog,
        )

        native_rows = []
        for address in table.addresses:
            row = _valid_language_object(self.reader, int(address))
            if row:
                native_rows.append({
                    "id": int(row["id"]), "name": str(row["name"]),
                })
        published = tuple(
            dict(row) for row in public_language_catalog(native_rows)
        )
        if not published:
            raise RuntimeError("当前游戏版本的原生语言目录校验失败")
        latest = self._refresh_table("language", force=True)
        latest_token = (
            latest.table_object, latest.vector, latest.fingerprint,
        )
        if latest_token != token:
            raise RuntimeError("native language table changed while building catalog")
        with self._public_catalog_lock:
            self._public_language_catalog_token = token
            self._public_language_catalog_rows = published
        return [dict(row) for row in published]

    def _read_fm_strings(self, pointers: set[int]) -> dict[int, str]:
        snapshots = self.reader._fixed_size_snapshots(pointers, 260)
        values: dict[int, str] = {}
        for pointer, raw in snapshots.items():
            if not raw or len(raw) < 4:
                continue
            length = struct.unpack_from("<I", raw, 0)[0]
            if not 0 < length <= 256 or len(raw) < 4 + length:
                continue
            try:
                value = raw[4:4 + length].decode("utf-8")
            except UnicodeDecodeError:
                continue
            if value and _CONTROL_CHARACTER_PATTERN.search(value) is None:
                values[int(pointer)] = value.strip()
        return values

    def _build_person_search_index(
        self, cancel_event: threading.Event | None = None, *, force: bool = False,
    ) -> tuple[PersonSearchRow, ...]:
        with self._person_search_build_lock:
            table = self._build_objects(self._refresh_table("person", force=force))
            token = (table.table_object, table.vector, table.fingerprint)
            if (
                not force and self._person_search_rows is not None
                and self._person_search_token == token
            ):
                return self._person_search_rows
            self._person_search_building = True
            started_at = monotonic()
            try:
                groups = self._player_person_groups(table)
                classified = [
                    (person, person - offset, kind)
                    for kind, offset, addresses in groups
                    for person in addresses if person > offset
                ]
                if cancel_event is not None and cancel_event.is_set():
                    return self._person_search_rows or ()
                layout = self.layout
                name_offsets = {
                    "full_name": int(layout.person_full_name_offset),
                    "first_name": int(layout.person_first_name_offset),
                    "last_name": int(layout.person_last_name_offset),
                    "common_name": int(layout.person_common_name_offset),
                }
                header_size = max(0x10, *(offset + 8 for offset in name_offsets.values()))
                flags_offset = getattr(layout, "person_flags_offset", None)
                flags_size = int(getattr(layout, "person_flags_bytes", 8) or 8)
                if flags_offset is not None:
                    header_size = max(header_size, int(flags_offset) + flags_size)
                nationality_offset = getattr(layout, "person_nationality_offset", None)
                if nationality_offset is not None:
                    header_size = max(header_size, int(nationality_offset) + 8)
                birth_date_offset = getattr(layout, "person_date_of_birth_offset", None)
                if birth_date_offset is not None:
                    header_size = max(header_size, int(birth_date_offset) + 4)
                ca_offset = int(layout.player_ca_offset)
                ca_size = int(layout.player_ca_bytes)
                pa_offset = int(layout.player_pa_offset)
                asking_price_offset = (
                    0x1D0 if str(getattr(layout, "key", "")).startswith("fm24")
                    else 0x234
                )
                world_reputation_offset = getattr(
                    layout, "player_world_reputation_offset", None,
                )
                summary_offsets = [ca_offset, pa_offset, asking_price_offset]
                if world_reputation_offset is not None:
                    summary_offsets.append(int(world_reputation_offset))
                summary_start = min(summary_offsets)
                summary_size = max(
                    ca_offset + ca_size, pa_offset + 2, asking_price_offset + 4,
                    *(
                        [int(world_reputation_offset) + 2]
                        if world_reputation_offset is not None else []
                    ),
                ) - summary_start
                player_person_offsets = tuple(
                    int(offset) for offset in (
                        getattr(layout, "player_person_offset", None),
                        getattr(layout, "player_and_non_player_person_offset", None),
                    ) if offset is not None
                )
                player_snapshot_size = max(
                    summary_start + summary_size,
                    max(player_person_offsets, default=0) + header_size,
                )
                player_snapshots = self.reader._fixed_size_snapshots(
                    (player for _person, player, _kind in classified),
                    player_snapshot_size,
                )
                headers: dict[int, bytes] = {}
                summary_snapshots: dict[int, bytes] = {}
                missing_persons: list[int] = []
                missing_summaries: list[int] = []
                for person, player, _kind in classified:
                    raw = player_snapshots.get(player)
                    person_offset = int(person) - int(player)
                    if raw and len(raw) >= person_offset + header_size:
                        headers[int(person)] = raw[person_offset:person_offset + header_size]
                    else:
                        missing_persons.append(int(person))
                    if raw and len(raw) >= summary_start + summary_size:
                        summary_snapshots[int(player) + summary_start] = raw[
                            summary_start:summary_start + summary_size
                        ]
                    else:
                        missing_summaries.append(int(player) + summary_start)
                if missing_persons:
                    headers.update(self.reader._fixed_size_snapshots(
                        missing_persons, header_size,
                    ))
                if missing_summaries:
                    summary_snapshots.update(self.reader._fixed_size_snapshots(
                        missing_summaries, summary_size,
                    ))
                name_links: dict[int, dict[str, int]] = {}
                nested_entries: set[int] = set()
                for person, _player, _kind in classified:
                    raw = headers.get(person)
                    if not raw or len(raw) < header_size:
                        continue
                    links = {
                        field: struct.unpack_from("<Q", raw, offset)[0]
                        for field, offset in name_offsets.items()
                    }
                    name_links[person] = links
                    nested_entries.update(
                        links[field] for field in ("first_name", "last_name", "common_name")
                        if links[field]
                    )
                entry_snapshots = self.reader._fixed_size_snapshots(nested_entries, 8)
                nationality_pointers = {
                    struct.unpack_from("<Q", raw, int(nationality_offset))[0]
                    for raw in headers.values()
                    if nationality_offset is not None
                    and len(raw) >= int(nationality_offset) + 8
                    and struct.unpack_from("<Q", raw, int(nationality_offset))[0]
                }
                nationality_snapshots = self.reader._fixed_size_snapshots(
                    nationality_pointers, 0x10,
                )
                string_pointers: set[int] = {
                    links["full_name"] for links in name_links.values()
                    if links["full_name"]
                }
                for links in name_links.values():
                    for field in ("first_name", "last_name", "common_name"):
                        raw = entry_snapshots.get(links[field])
                        links[field] = (
                            struct.unpack_from("<Q", raw, 0)[0]
                            if raw and len(raw) >= 8 else 0
                        )
                        if links[field]:
                            string_pointers.add(links[field])
                strings = self._read_fm_strings(string_pointers)
                if cancel_event is not None and cancel_event.is_set():
                    return self._person_search_rows or ()
                rows: list[PersonSearchRow] = []
                search_projections: dict[str, tuple[str, str]] = {}

                def search_projection(value: str) -> tuple[str, str]:
                    cached = search_projections.get(value)
                    if cached is not None:
                        return cached
                    normalized_value = _normalized_search_text(value)
                    projected = (
                        normalized_value,
                        _COMPACT_SEARCH_PATTERN.sub("", normalized_value),
                    )
                    search_projections[value] = projected
                    return projected

                for person, player, kind in classified:
                    raw = headers.get(person)
                    links = name_links.get(person)
                    if not raw or not links:
                        continue
                    uid = struct.unpack_from("<I", raw, 0x0C)[0]
                    if not uid:
                        continue
                    first = strings.get(links["first_name"], "")
                    last = strings.get(links["last_name"], "")
                    common = strings.get(links["common_name"], "")
                    full = strings.get(links["full_name"], "")
                    joined = " ".join(part for part in (first, last) if part)
                    localized = ""
                    if str(getattr(layout, "key", "")).startswith("fm24"):
                        localized = next(
                            (value for value in (full, common, joined) if _contains_cjk(value)),
                            "",
                        )
                    display = localized or common or joined or full
                    if not display:
                        continue
                    variants = tuple(dict.fromkeys(
                        value for value in (display, common, joined, first, last, full)
                        if value
                    ))
                    projections = tuple(
                        search_projection(value) for value in variants
                    )
                    uid_text = str(uid)
                    searchable = " ".join(
                        (*(
                            normalized for normalized, _compact in projections
                            if normalized
                        ), uid_text)
                    )
                    compact = " ".join(
                        (*(
                            compacted for _normalized, compacted in projections
                            if compacted
                        ), uid_text)
                    )
                    normalized_display = search_projection(display)[0]
                    summary_raw = summary_snapshots.get(player + summary_start)
                    ca = pa = asking_price = world_reputation = None
                    if summary_raw and len(summary_raw) >= summary_size:
                        ca_format = "<B" if ca_size == 1 else "<H"
                        ca_value = struct.unpack_from(
                            ca_format, summary_raw, ca_offset - summary_start,
                        )[0]
                        pa_value = struct.unpack_from(
                            "<H", summary_raw, pa_offset - summary_start,
                        )[0]
                        asking_price = int(struct.unpack_from(
                            "<I", summary_raw, asking_price_offset - summary_start,
                        )[0])
                        if asking_price in {300_000_000, 0xFFFFFFFF}:
                            asking_price = 0
                        ca = int(ca_value) if 1 <= int(ca_value) <= 200 else None
                        pa = int(pa_value) if 1 <= int(pa_value) <= 200 else None
                        if world_reputation_offset is not None:
                            reputation_value = struct.unpack_from(
                                "<H", summary_raw,
                                int(world_reputation_offset) - summary_start,
                            )[0]
                            world_reputation = (
                                int(reputation_value)
                                if 0 <= int(reputation_value) <= 10_000 else None
                            )
                    female = None
                    if flags_offset is not None:
                        flags_raw = raw[int(flags_offset):int(flags_offset) + flags_size]
                        female = bool(
                            len(flags_raw) >= 8
                            and int.from_bytes(flags_raw[:8], "little") & 0x1000
                        )
                    nationality_id = None
                    if nationality_offset is not None:
                        nation_pointer = struct.unpack_from(
                            "<Q", raw, int(nationality_offset),
                        )[0]
                        nation_raw = nationality_snapshots.get(int(nation_pointer))
                        if nation_raw and len(nation_raw) >= 0x10:
                            nation_uid = struct.unpack_from("<I", nation_raw, 0x0C)[0]
                            nationality_id = int(nation_uid) if nation_uid else None
                    date_of_birth = (
                        _decode_person_birth_date(
                            layout,
                            raw[int(birth_date_offset):int(birth_date_offset) + 4],
                        )
                        if birth_date_offset is not None else None
                    )
                    rows.append(PersonSearchRow(
                        uid=int(uid), address=int(person), player_address=int(player),
                        object_type=kind, display_name=display,
                        first_name=first, last_name=last, common_name=common,
                        full_name=full, searchable_names=searchable,
                        compact_searchable_names=compact,
                        normalized_display_name=normalized_display,
                        ca=ca, pa=pa, asking_price=asking_price, female=female,
                        nationality_id=nationality_id,
                        world_reputation=world_reputation,
                        date_of_birth=date_of_birth,
                    ))
                latest = self._refresh_table("person", force=True)
                latest_token = (latest.table_object, latest.vector, latest.fingerprint)
                if latest_token != token:
                    raise RuntimeError("native person table changed while building search index")
                rows.sort(key=lambda row: (
                    row.normalized_display_name or _normalized_search_text(row.display_name),
                    row.uid,
                ))
                published = tuple(rows)
                with self._lock:
                    self._person_search_rows = published
                    by_uid: dict[int, PersonSearchRow] = {}
                    for row in published:
                        by_uid.setdefault(int(row.uid), row)
                    self._person_search_by_uid = by_uid
                    self._person_search_token = token
                    with self._person_search_view_lock:
                        self._person_search_views.clear()
                        self._person_search_query_views.clear()
                    self._person_search_build_duration_ms = round(
                        (monotonic() - started_at) * 1000,
                    )
                return published
            finally:
                self._person_search_building = False

    def _sorted_person_search_rows(
        self, rows: tuple[PersonSearchRow, ...], sort_field: str, order: str,
    ) -> tuple[PersonSearchRow, ...]:
        """Reuse one of the six immutable sort views for the live index."""
        key = (id(rows), sort_field, order)
        with self._person_search_view_lock:
            cached = self._person_search_views.get(key)
            if cached is not None:
                return cached
            direction = 1 if order == "asc" else -1

            def numeric_sort_key(row: PersonSearchRow) -> tuple[Any, ...]:
                value = getattr(row, sort_field)
                return (
                    value is None,
                    direction * int(value or 0),
                    row.normalized_display_name or _normalized_search_text(row.display_name),
                    row.uid,
                )

            prepared = tuple(sorted(rows, key=numeric_sort_key))
            self._person_search_views[key] = prepared
            return prepared

    def _filtered_person_search_rows(
        self, rows: tuple[PersonSearchRow, ...], normalized: str, compact: str,
        sort_field: str, order: str, resolved_gender: str,
        nationality_id: int = 0,
        min_age: int = 0, max_age: int = 0,
        min_ca: int = 0, max_ca: int = 0,
        min_pa: int = 0, max_pa: int = 0,
        game_date: date | None = None,
    ) -> tuple[PersonSearchRow, ...]:
        """Cache bounded, immutable query views without retaining broad matches."""
        key = (
            id(rows), normalized, sort_field, order, resolved_gender,
            int(nationality_id or 0), int(min_age or 0), int(max_age or 0),
            int(min_ca or 0), int(max_ca or 0), int(min_pa or 0),
            int(max_pa or 0), game_date.isoformat() if game_date else "",
        )
        with self._person_search_view_lock:
            cached = self._person_search_query_views.get(key)
            if cached is not None:
                self._person_search_query_views.move_to_end(key)
                return cached
        matches = [
            row for row in rows
            if normalized in row.searchable_names
            or (compact and compact in row.compact_searchable_names)
        ]
        if resolved_gender != "all" and getattr(
            self.layout, "person_flags_offset", None,
        ) is not None:
            female = resolved_gender == "women"
            matches = [row for row in matches if bool(row.female) == female]
        if int(nationality_id or 0) > 0:
            matches = [
                row for row in matches
                if int(row.nationality_id or 0) == int(nationality_id)
            ]
        if int(min_age or 0) > 0 or int(max_age or 0) > 0:
            matches = [
                row for row in matches
                if (age := _age_on(row.date_of_birth, game_date)) is not None
                and (int(min_age or 0) <= 0 or age >= int(min_age))
                and (int(max_age or 0) <= 0 or age <= int(max_age))
            ]
        if int(min_ca or 0) > 0 or int(max_ca or 0) > 0:
            matches = [
                row for row in matches
                if row.ca is not None
                and (int(min_ca or 0) <= 0 or int(row.ca) >= int(min_ca))
                and (int(max_ca or 0) <= 0 or int(row.ca) <= int(max_ca))
            ]
        if int(min_pa or 0) > 0 or int(max_pa or 0) > 0:
            matches = [
                row for row in matches
                if row.pa is not None
                and (int(min_pa or 0) <= 0 or int(row.pa) >= int(min_pa))
                and (int(max_pa or 0) <= 0 or int(row.pa) <= int(max_pa))
            ]
        direction = 1 if order == "asc" else -1
        matches.sort(key=lambda row: (
            getattr(row, sort_field) is None,
            direction * int(getattr(row, sort_field) or 0),
            row.normalized_display_name or _normalized_search_text(row.display_name),
            row.uid,
        ))
        prepared = tuple(matches)
        if len(prepared) <= PERSON_SEARCH_QUERY_CACHE_MAX_MATCHES:
            with self._person_search_view_lock:
                self._person_search_query_views[key] = prepared
                self._person_search_query_views.move_to_end(key)
                while len(self._person_search_query_views) > PERSON_SEARCH_QUERY_CACHE_LIMIT:
                    self._person_search_query_views.popitem(last=False)
        return prepared

    def search_players(
        self, query: str = "", *, page: int = 1, page_size: int = 30,
        force_refresh: bool = False, sort_by: str = "pa", sort_order: str = "desc",
        gender: str = "all", nationality_id: int = 0,
        min_age: int = 0, max_age: int = 0,
        min_ca: int = 0, max_ca: int = 0,
        min_pa: int = 0, max_pa: int = 0,
        game_date: date | None = None,
    ) -> dict[str, Any]:
        _validate_player_filter_ranges(
            min_age, max_age, min_ca, max_ca, min_pa, max_pa,
        )
        rows = self._build_person_search_index(force=force_refresh)
        normalized = _normalized_search_text(query)
        compact = _compact_search_text(query)
        sort_field = str(sort_by or "").strip().lower()
        if sort_field not in {"ca", "pa", "asking_price", "world_reputation"}:
            sort_field = "pa"
        order = "asc" if str(sort_order or "").strip().lower() == "asc" else "desc"
        resolved_gender = str(gender or "all").strip().lower()
        if resolved_gender not in {"men", "women", "all"}:
            resolved_gender = "all"
        flags_offset = getattr(self.layout, "person_flags_offset", None)
        range_filters = (
            int(min_age or 0), int(max_age or 0), int(min_ca or 0),
            int(max_ca or 0), int(min_pa or 0), int(max_pa or 0),
        )
        if any(range_filters[:2]) and game_date is None:
            raise RuntimeError("当前版本无法读取游戏日期，暂不能按年龄筛选")
        if normalized or int(nationality_id or 0) > 0 or any(range_filters):
            matches = self._filtered_person_search_rows(
                rows, normalized, compact, sort_field, order, resolved_gender,
                int(nationality_id or 0),
                *range_filters, game_date,
            )
        else:
            matches = self._sorted_person_search_rows(rows, sort_field, order)
        if (
            not normalized and int(nationality_id or 0) <= 0
            and resolved_gender != "all" and flags_offset is not None
        ):
            matches = [
                row for row in matches
                if bool(row.female) == (resolved_gender == "women")
            ]
        bounded_size = max(1, min(50, int(page_size or 30)))
        total = len(matches)
        page_count = max(1, (total + bounded_size - 1) // bounded_size)
        bounded_page = max(1, min(page_count, int(page or 1)))
        start = (bounded_page - 1) * bounded_size
        selected = matches[start:start + bounded_size]
        return {
            "players": [row.public(game_date=game_date) for row in selected],
            "pagination": {
                "page": bounded_page, "page_size": bounded_size,
                "page_count": page_count, "total": total,
                "from": start + 1 if total else 0,
                "to": min(total, start + bounded_size),
            },
            "filters": {
                "search": str(query or ""), "sort_by": sort_field,
                "sort_order": order, "gender": resolved_gender,
                **(
                    {"nationality_id": int(nationality_id)}
                    if int(nationality_id or 0) > 0 else {}
                ),
                **{
                    name: int(value)
                    for name, value in (
                        ("min_age", min_age), ("max_age", max_age),
                        ("min_ca", min_ca), ("max_ca", max_ca),
                        ("min_pa", min_pa), ("max_pa", max_pa),
                    ) if int(value or 0) > 0
                },
            },
            "filter_options": {
                "nationality_ids": sorted({
                    int(row.nationality_id) for row in rows
                    if int(row.nationality_id or 0) > 0
                }),
            },
            "index": {
                "source": "session_person_cache",
                "player_count": len(rows),
                "build_duration_ms": self._person_search_build_duration_ms,
            },
        }

    def search_players_by_uids(
        self, player_uids: Any, *, query: str = "", page: int = 1,
        page_size: int = 30, sort_by: str = "pa", sort_order: str = "desc",
        gender: str = "all", nationality_id: int = 0,
        min_age: int = 0, max_age: int = 0,
        min_ca: int = 0, max_ca: int = 0,
        min_pa: int = 0, max_pa: int = 0,
        game_date: date | None = None,
    ) -> dict[str, Any]:
        """Project a localized-name UID match through the same live index."""
        _validate_player_filter_ranges(
            min_age, max_age, min_ca, max_ca, min_pa, max_pa,
        )
        rows = self._build_person_search_index()
        wanted = {int(uid) for uid in player_uids if int(uid) > 0}
        sort_field = str(sort_by or "").strip().lower()
        if sort_field not in {"ca", "pa", "asking_price", "world_reputation"}:
            sort_field = "pa"
        order = "asc" if str(sort_order or "").strip().lower() == "asc" else "desc"
        resolved_gender = str(gender or "all").strip().lower()
        if resolved_gender not in {"men", "women", "all"}:
            resolved_gender = "all"
        matches = [row for row in rows if int(row.uid) in wanted]
        if resolved_gender != "all" and getattr(
            self.layout, "person_flags_offset", None,
        ) is not None:
            female = resolved_gender == "women"
            matches = [row for row in matches if bool(row.female) == female]
        if int(nationality_id or 0) > 0:
            matches = [
                row for row in matches
                if int(row.nationality_id or 0) == int(nationality_id)
            ]
        if int(min_age or 0) > 0 or int(max_age or 0) > 0:
            if game_date is None:
                raise RuntimeError("当前版本无法读取游戏日期，暂不能按年龄筛选")
            matches = [
                row for row in matches
                if (age := _age_on(row.date_of_birth, game_date)) is not None
                and (int(min_age or 0) <= 0 or age >= int(min_age))
                and (int(max_age or 0) <= 0 or age <= int(max_age))
            ]
        if int(min_ca or 0) > 0 or int(max_ca or 0) > 0:
            matches = [
                row for row in matches
                if row.ca is not None
                and (int(min_ca or 0) <= 0 or int(row.ca) >= int(min_ca))
                and (int(max_ca or 0) <= 0 or int(row.ca) <= int(max_ca))
            ]
        if int(min_pa or 0) > 0 or int(max_pa or 0) > 0:
            matches = [
                row for row in matches
                if row.pa is not None
                and (int(min_pa or 0) <= 0 or int(row.pa) >= int(min_pa))
                and (int(max_pa or 0) <= 0 or int(row.pa) <= int(max_pa))
            ]
        direction = 1 if order == "asc" else -1
        matches.sort(key=lambda row: (
            getattr(row, sort_field) is None,
            direction * int(getattr(row, sort_field) or 0),
            row.normalized_display_name or _normalized_search_text(row.display_name),
            row.uid,
        ))
        bounded_size = max(1, min(50, int(page_size or 30)))
        total = len(matches)
        page_count = max(1, (total + bounded_size - 1) // bounded_size)
        bounded_page = max(1, min(page_count, int(page or 1)))
        start = (bounded_page - 1) * bounded_size
        selected = matches[start:start + bounded_size]
        return {
            "players": [row.public(game_date=game_date) for row in selected],
            "pagination": {
                "page": bounded_page, "page_size": bounded_size,
                "page_count": page_count, "total": total,
                "from": start + 1 if total else 0,
                "to": min(total, start + bounded_size),
            },
            "filters": {
                "search": str(query or ""), "sort_by": sort_field,
                "sort_order": order, "gender": resolved_gender,
                **(
                    {"nationality_id": int(nationality_id)}
                    if int(nationality_id or 0) > 0 else {}
                ),
                **{
                    name: int(value)
                    for name, value in (
                        ("min_age", min_age), ("max_age", max_age),
                        ("min_ca", min_ca), ("max_ca", max_ca),
                        ("min_pa", min_pa), ("max_pa", max_pa),
                    ) if int(value or 0) > 0
                },
            },
            "filter_options": {
                "nationality_ids": sorted({
                    int(row.nationality_id) for row in rows
                    if int(row.nationality_id or 0) > 0
                }),
            },
            "index": {
                "source": "session_person_cache_localized_uid",
                "player_count": len(rows),
                "build_duration_ms": self._person_search_build_duration_ms,
            },
        }

    def player_for_uid(self, uid: int) -> dict[str, Any] | None:
        """Return one current-session player row from the verified Person table."""
        target = int(uid)
        if target <= 0:
            return None
        table = self._refresh_table("person")
        token = (table.table_object, table.vector, table.fingerprint)
        with self._lock:
            row = (
                (self._person_search_by_uid or {}).get(target)
                if self._person_search_token == token else None
            )
        if row is not None:
            return row.public()
        quick_target = self._build_player_target_index().get(target)
        return (
            self._single_player_row(target, quick_target)
            if quick_target is not None else None
        )

    def all_player_rows(self) -> list[dict[str, Any]]:
        """Return the verified session player directory without pagination."""
        return [row.public() for row in self._build_person_search_index()]

    def _refresh_human_managers(self) -> None:
        with self._lock:
            now = monotonic()
            if now - self._human_manager_checked_at < TABLE_RECHECK_SECONDS:
                return
            self._human_manager_checked_at = now
            if not self.human_manager_slot:
                self._human_managers = {}
                return
            owner = int(self.reader.ptr(self.human_manager_slot) or 0)
            begin = int(self.reader.ptr(owner + 0x18) or 0) if owner else 0
            end = int(self.reader.ptr(owner + 0x20) or 0) if owner else 0
            if not begin or end < begin or (end - begin) % 8 or (end - begin) // 8 > 1024:
                self._human_managers = {}
                return
            raw = self.reader.bytes(begin, end - begin) if end > begin else b""
            if raw is None or len(raw) != end - begin:
                self._human_managers = {}
                return
            fingerprint = blake2b(raw, digest_size=16).hexdigest()
            if fingerprint == self._human_manager_fingerprint:
                return
            expected = {
                int(self.reader.module_base) + int(rva)
                for rva in self.layout.human_manager_vtable_rvas
            }
            rows: dict[int, list[int]] = {}
            for (manager,) in struct.iter_unpack("<Q", raw):
                if not manager:
                    continue
                vtable_address = (
                    manager + int(self.layout.manager_person_offset)
                    if self.layout.human_manager_vtable_on_person else manager
                )
                if int(self.reader.ptr(vtable_address) or 0) not in expected:
                    continue
                uid = int(self.reader.u32(
                    manager + int(self.layout.manager_person_offset) + 0x0C,
                ) or 0)
                if uid:
                    rows.setdefault(uid, []).append(int(manager))
            self._human_manager_fingerprint = fingerprint
            self._human_managers = {
                uid: tuple(addresses) for uid, addresses in rows.items()
            }

    def human_manager_addresses(self, uid: int | None = None) -> tuple[int, ...]:
        self._refresh_human_managers()
        with self._lock:
            if uid is not None:
                return tuple(self._human_managers.get(int(uid), ()))
            return tuple(
                address for addresses in self._human_managers.values()
                for address in addresses
            )

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            with self._person_search_view_lock:
                return {
                    "root_slot": hex(self.root_slot),
                    "human_manager_slot": hex(self.human_manager_slot or 0),
                    "tables": {
                        name: {
                            "count": len(table.addresses),
                            "fingerprint": table.fingerprint,
                            "indexed_uids": len(table.uid_addresses or {}),
                        }
                        for name, table in self._tables.items()
                    },
                    "person_search": {
                        "building": self._person_search_building,
                        "player_count": len(self._person_search_rows or ()),
                        "build_duration_ms": self._person_search_build_duration_ms,
                        "sort_view_count": len(self._person_search_views),
                        "query_view_count": len(self._person_search_query_views),
                    },
                }

    def warm(self, cancel_event: threading.Event | None = None) -> None:
        """Populate search indexes in the background without delaying connect."""
        # World-player search is the most latency-sensitive directory consumer.
        # Build its Person projection before the broader Team/Club/Nation warmup
        # so an early page visit does not wait behind unrelated tables.
        if cancel_event is None or not cancel_event.is_set():
            try:
                self._build_person_search_index(cancel_event)
            except (OSError, RuntimeError, TypeError, ValueError):
                pass
        for name in ("team", "club", "nation"):
            if cancel_event is not None and cancel_event.is_set():
                return
            try:
                self._build_objects(self._refresh_table(name))
            except (OSError, RuntimeError, TypeError, ValueError):
                continue


def _native_address(value: Any) -> int:
    """Convert a public hexadecimal address without accepting invalid values."""
    if isinstance(value, int):
        return int(value)
    try:
        return int(str(value or ""), 16)
    except (TypeError, ValueError):
        return 0


def _layout_vtable_address(reader: Any, name: str) -> int:
    """Return a layout-gated vtable address, or zero when unavailable."""
    layout = getattr(reader, "layout", None)
    module_base = int(getattr(reader, "module_base", 0) or 0)
    rva = getattr(layout, f"{name}_vtable_rva", None)
    if module_base <= 0 or rva is None:
        return 0
    try:
        return module_base + int(rva)
    except (TypeError, ValueError):
        return 0


def resolve_team_club(
    reader: Any,
    team_address: Any = 0,
    team_id: int = 0,
    *,
    prefer_index: bool = True,
) -> ResolvedTeamClub:
    """Resolve a live ``Team -> Club`` pair by stable UID.

    The session's native object directory is the preferred relocation source,
    matching FMRTE's object-oriented lookup.  The caller-provided address is
    retained as a compatibility fallback for readers without an index (or
    when the index is temporarily unavailable).  Every candidate is checked
    against the Team UID, the Team -> Club pointer, and any version-gated
    vtables available on the reader; no address escapes this validation.
    """
    requested_id = int(team_id or 0)
    hint = _native_address(team_address)
    if not hint and requested_id <= 0:
        raise RuntimeError("尚未定位当前执教俱乐部")

    candidates: list[int] = []
    if prefer_index and requested_id > 0:
        directory = database_index_for_reader(reader)
        if directory is not None:
            try:
                candidates.extend(
                    int(address)
                    for address in directory.addresses_for_uid(
                        "team", requested_id,
                    )
                    if int(address or 0) > 0
                )
            except (OSError, RuntimeError, TypeError, ValueError):
                # A transient directory failure must not break the old,
                # already-validated address path.
                candidates.clear()
    if hint > 0 and hint not in candidates:
        candidates.append(hint)

    expected_team_vtable = _layout_vtable_address(reader, "team")
    expected_club_vtable = _layout_vtable_address(reader, "club")
    observed_hint_id = 0
    for candidate in candidates:
        try:
            actual_id = int(reader.u32(candidate + ENTITY_UID_OFFSET) or 0)
            if candidate == hint:
                observed_hint_id = actual_id
            if not actual_id or (requested_id > 0 and actual_id != requested_id):
                continue
            if (
                expected_team_vtable
                and int(reader.ptr(candidate) or 0) != expected_team_vtable
            ):
                continue
            club = int(reader.ptr(candidate + TEAM_CLUB_OFFSET) or 0)
            if not club:
                continue
            if expected_club_vtable and int(reader.ptr(club) or 0) != expected_club_vtable:
                continue
            # Reserve/B teams legitimately point at a Club object whose UID
            # differs from the Team UID (for example Ajax II).  The Team UID,
            # Team -> Club pointer and Club vtable establish the object chain;
            # Club UID is intentionally informational here and must not be
            # used as an equality gate.
            return ResolvedTeamClub(
                team_address=int(candidate), club_address=club, team_id=actual_id,
            )
        except (OSError, RuntimeError, TypeError, ValueError):
            continue

    if (
        hint and requested_id > 0 and observed_hint_id
        and observed_hint_id != requested_id
    ):
        raise RuntimeError("当前执教俱乐部已变化，请先刷新")
    if requested_id > 0:
        raise RuntimeError("无法按俱乐部 ID 取得当前俱乐部对象，请刷新后重试")
    raise RuntimeError("尚未定位当前执教俱乐部")


def resolve_native_entity(
    reader: Any,
    table_name: str,
    entity_id: int,
    address: Any = 0,
    *,
    vtable_attribute: str | None = None,
) -> int:
    """Resolve one native database entity by UID, retaining a validated hint.

    This is the read/write boundary used by higher-level features that operate
    on a Nation or another typed database object.  The object directory is
    preferred, but a caller's address remains a compatibility fallback.  No
    address is returned until its UID and (when mapped) vtable match.
    """
    requested_id = int(entity_id or 0)
    if requested_id <= 0:
        raise RuntimeError("对象 ID 无效，请重新读取")
    hint = _native_address(address)
    candidates: list[int] = []
    directory = database_index_for_reader(reader)
    if directory is not None:
        try:
            candidates.extend(
                int(candidate)
                for candidate in directory.addresses_for_uid(table_name, requested_id)
                if int(candidate or 0) > 0
            )
        except (OSError, RuntimeError, TypeError, ValueError):
            candidates.clear()
    if hint and hint not in candidates:
        candidates.append(hint)
    expected_vtable = (
        _layout_vtable_address(reader, vtable_attribute)
        if vtable_attribute else 0
    )
    observed_hint_id = 0
    for candidate in candidates:
        try:
            actual_id = int(reader.u32(candidate + ENTITY_UID_OFFSET) or 0)
            if candidate == hint:
                observed_hint_id = actual_id
            if actual_id != requested_id:
                continue
            observed_vtable = int(reader.ptr(candidate) or 0)
            if expected_vtable and observed_vtable != expected_vtable:
                continue
            return int(candidate)
        except (OSError, RuntimeError, TypeError, ValueError):
            continue
    if hint and observed_hint_id and observed_hint_id != requested_id:
        raise RuntimeError("对象地址已失效，请重新读取")
    raise RuntimeError(f"无法按 ID 定位 {table_name} 对象，请刷新后重试")


def database_index_for_reader(
    reader: Any, *, force_retry: bool = False,
) -> DatabaseIndex | None:
    provider = getattr(reader, "database_index_provider", None)
    if not callable(provider):
        return None
    try:
        return provider(force_retry=True) if force_retry else provider()
    except (OSError, RuntimeError, TypeError, ValueError):
        return None


def database_index_error_for_reader(reader: Any) -> str:
    provider = getattr(reader, "database_index_error_provider", None)
    if not callable(provider):
        return ""
    try:
        return str(provider() or "").strip()
    except (OSError, RuntimeError, TypeError, ValueError):
        return ""


__all__ = [
    "DatabaseIndex", "PersonSearchRow", "ResolvedTeamClub",
    "database_index_error_for_reader", "database_index_for_reader",
    "resolve_native_entity", "resolve_team_club",
]
