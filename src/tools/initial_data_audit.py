from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import struct
import sys
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from functools import lru_cache
from itertools import combinations, product
from pathlib import Path
from typing import Any, Callable, Iterable

from tools.app_paths import ASSET_ROOT
from tools.game_layout import (
    FM24_XGP_LAYOUT, FM26_LAYOUT, FM26_XGP_TEMPLATE, GameLayout,
    is_fm24_xgp_module, is_fm26_xgp_module,
    layout_for_executable,
    resolve_fm24_xgp_layout, resolve_fm26_steam_layout,
    resolve_fm26_xgp_layout,
)
from tools.refresh_memory_core import (
    complete_fixture_pool_slab_addresses,
    read_fixture_pool_addresses,
    scan_fixture_addresses,
    scan_fixture_and_result_addresses,
    scan_result_addresses,
)

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fm_collector.win32 import (
    MEM_PRIVATE,
    ProcessHandle,
    find_module,
    find_processes,
    iter_readable_regions,
    open_process,
    read_process_memory,
)


SUPPORTED_EXE_SHA256 = "3653C97F9CCEC2BE28EDC4FAAE67304B5B6C26733F2F07DEA3E7C591D3B9FF73"
GAME_PLUGIN = "game_plugin.dll"
FIXTURE_VTABLE_RVA = 0x449EF98
FIXTURE_RESULT_VTABLE_RVA = 0x4332188
# Persistent season fixture records survive after the short-lived result object is cleared.
SEASON_RESULT_VTABLE_RVA = 0x4330778
TEAM_VTABLE_RVA = 0x44C13A8
NATIONAL_TEAM_VTABLE_RVA = 0x4592528
NATION_VTABLE_RVA = 0x44CDD68
CLUB_VTABLE_RVA = 0x44BA518
COMP_VTABLE_RVA = 0x44BFE68
GAME_DATE_RVA = 0x4DF3C18
_PROCESS_SELECTION_LOCK = threading.RLock()
_PREFERRED_LAYOUT_KEY: str | None = None
_PREFERRED_PROCESS_PID: int | None = None
_PROCESS_LAYOUT_CACHE_SECONDS = 2.0
_PROCESS_LAYOUT_CACHE: tuple[
    float, str | None, int | None, tuple[int, str, GameLayout]
] | None = None

FIXTURE_SIZE = 0x58
TEAM_CLUB = 0x30

# Verified on FM24 Steam 24.4.2 against the native Team Type field.  Code 22
# is an evaluation container rather than a normal manageable squad.
CLUB_TEAM_TYPE_NAMES = {
    0: "一线队", 1: "预备队", 2: "A队", 3: "B队", 9: "U23",
    10: "U21", 11: "U19", 12: "U18", 13: "C队", 14: "业余队",
    15: "II队", 16: "二队", 17: "三队", 18: "U20",
    22: "青年评估队", 30: "荷兰预备队",
}
MANAGEABLE_CLUB_TEAM_TYPES = frozenset(CLUB_TEAM_TYPE_NAMES) - {22}
TEAM_ROSTER_BEGIN = 0x38
TEAM_ROSTER_END = 0x40
TEAM_COMP = 0x50
TEAM_STADIUM = 0x78
TEAM_MANAGER = 0x80
ENTITY_UID = 0x0C
CLUB_NAME_FULL = 0xC0
CLUB_NAME_SHORT = 0xC8
CLUB_CINO = 0xB0
CLUB_CINT = 0x100
CLUB_PREF_FORMATION = 0x74
CLUB_MORALE = 0x119
COMP_NAME_FULL = 0x40
COMP_NAME_SHORT = 0x48
STADIUM_NAME = 0x40

ACTUAL_PLAYER_VTABLE_RVA = 0x4509828
ACTUAL_PLAYER_AND_NON_PLAYER_VTABLE_RVA = 0x4785308
PLAYER_PERSON = 0x288
PLAYER_AND_NON_PLAYER_PERSON = 0x380
PLAYER_MATCH_ID = PLAYER_PERSON + 0x08
PLAYER_AND_NON_PLAYER_MATCH_ID = PLAYER_AND_NON_PLAYER_PERSON + 0x08
PLAYER_POSITIONS = 0x150
PLAYER_SHARPNESS = 0x258
PLAYER_FATIGUE = 0x25A
PLAYER_FITNESS = 0x25C
PLAYER_CA = 0x264
PLAYER_MORALE = 0x26C
PLAYER_INJURY_LIST = 0xF8
PERSON_FIRST_NAME = 0x50
PERSON_LAST_NAME = 0x58
PERSON_COMMON_NAME = 0x60
PERSON_FULL_NAME = 0x40

POSITION_NAMES = (
    "GK", "SW", "DL", "DC", "DR", "DM", "ML", "MC", "MR",
    "AML", "AMC", "AMR", "ST", "WBL", "WBR",
)

NATION_MAPPING_PATH = (
    ASSET_ROOT
    / "assets" / "nation_mappings" / "nation_mappings_zh.json"
)


def load_nation_names() -> dict[int, str]:
    try:
        payload = json.loads(NATION_MAPPING_PATH.read_text(encoding="utf-8-sig"))
        names = {
            int(identifier): details.get("name") or details.get("code") or str(identifier)
            for identifier, details in payload.get("nations", {}).items()
        }
        # The bundled Chinese mapping mistranslates the first two FM nation IDs
        # as continents; its English counterpart correctly identifies them.
        names.update({5: "阿尔及利亚", 6: "安哥拉", 380: "库拉索"})
        return names
    except (OSError, ValueError, TypeError):
        return {}


NATION_NAMES = load_nation_names()


def load_nation_codes() -> dict[int, str]:
    """Load FM's three-letter nation codes from the bundled English mapping."""
    try:
        path = NATION_MAPPING_PATH.with_name("nation_mappings_en.json")
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        return {
            int(identifier): str(details.get("code") or "").upper()
            for identifier, details in payload.get("nations", {}).items()
            if details.get("code")
        }
    except (OSError, ValueError, TypeError):
        return {}


NATION_CODES = load_nation_codes()


@dataclass(frozen=True)
class Fixture:
    address: int
    home_team: int
    away_team: int
    result_or_state: int
    competition_season: int
    stadium: int
    match_date: date
    kickoff_minutes: int | None
    round_or_slot: int
    raw_tail: str
    stage_index: int | None = None
    group_index: int | None = None


class Reader:
    _PAGE_SIZE = 0x1000
    _PAGE_MASK = _PAGE_SIZE - 1
    _MAX_CACHED_READ = 64 * 1024
    _MAX_PREFETCH_PAGES = 128

    def __init__(
        self, process: ProcessHandle, module_base: int,
        layout: GameLayout = FM26_LAYOUT,
    ):
        self.process = process
        self.module_base = module_base
        self.layout = layout
        self.team_cache: dict[int, dict[str, Any] | None] = {}
        self.comp_cache: dict[int, dict[str, Any] | None] = {}
        self.string_cache: dict[int, str | None] = {}
        self._page_cache: dict[int, bytes] = {}
        self.read_calls = 0
        self.requested_bytes = 0
        self.returned_bytes = 0

    def prefetch(self, address: int, size: int) -> None:
        """Cache only explicitly requested readable pages for this Reader."""
        page_cache = getattr(self, "_page_cache", None)
        if not isinstance(page_cache, dict):
            return
        address = int(address)
        size = int(size)
        if address <= 0 or size <= 0:
            return
        first_page = address & ~self._PAGE_MASK
        last_page = (address + size - 1) & ~self._PAGE_MASK
        page_count = (last_page - first_page) // self._PAGE_SIZE + 1
        if page_count > self._MAX_PREFETCH_PAGES:
            return
        for page in range(first_page, last_page + self._PAGE_SIZE, self._PAGE_SIZE):
            if page in page_cache:
                continue
            self.read_calls += 1
            data = read_process_memory(self.process, page, self._PAGE_SIZE)
            self.returned_bytes += len(data) if data else 0
            if data and len(data) == self._PAGE_SIZE:
                page_cache[page] = data

    def invalidate_prefetch(self) -> None:
        """Discard process-memory pages after an in-process write transaction."""
        page_cache = getattr(self, "_page_cache", None)
        if isinstance(page_cache, dict):
            page_cache.clear()

    def _cached_bytes(self, address: int, size: int) -> bytes | None:
        if size <= 0 or size > self._MAX_CACHED_READ:
            return None
        page_cache = getattr(self, "_page_cache", None)
        if not isinstance(page_cache, dict):
            return None
        first_page = address & ~self._PAGE_MASK
        last_page = (address + size - 1) & ~self._PAGE_MASK
        if first_page == last_page:
            cached = page_cache.get(first_page)
            if cached is None:
                return None
            offset = address - first_page
            return cached[offset:offset + size]
        pages = []
        for page in range(first_page, last_page + self._PAGE_SIZE, self._PAGE_SIZE):
            cached = page_cache.get(page)
            if cached is None:
                return None
            pages.append(cached)
        combined = b"".join(pages)
        offset = address - first_page
        return combined[offset:offset + size]

    def bytes(self, address: int, size: int) -> bytes | None:
        size = max(0, int(size))
        if not size:
            return b""
        self.requested_bytes += size
        cached = self._cached_bytes(int(address), size)
        if cached is not None and len(cached) == size:
            return cached
        self.read_calls += 1
        data = read_process_memory(self.process, address, size)
        self.returned_bytes += len(data) if data else 0
        return data

    def read_metrics(self) -> dict[str, int]:
        return {
            "memory_read_calls": self.read_calls,
            "memory_requested_bytes": self.requested_bytes,
            "memory_returned_bytes": self.returned_bytes,
        }

    def u8(self, address: int) -> int | None:
        data = self.bytes(address, 1)
        return data[0] if data and len(data) == 1 else None

    def u16(self, address: int) -> int | None:
        data = self.bytes(address, 2)
        return struct.unpack("<H", data)[0] if data and len(data) == 2 else None

    def u32(self, address: int) -> int | None:
        data = self.bytes(address, 4)
        return struct.unpack("<I", data)[0] if data and len(data) == 4 else None

    def ptr(self, address: int) -> int | None:
        data = self.bytes(address, 8)
        if not data or len(data) != 8:
            return None
        value = struct.unpack("<Q", data)[0]
        return value or None

    def ptr_array(self, address: int, count: int) -> list[int | None]:
        count = max(0, int(count))
        if not count:
            return []
        data = self.bytes(address, count * 8)
        if data and len(data) == count * 8:
            return [value or None for value in struct.unpack(f"<{count}Q", data)]
        return [self.ptr(address + index * 8) for index in range(count)]

    def fm_string_at(self, address: int) -> str | None:
        pointer = self.ptr(address)
        if not pointer:
            return None
        if pointer in self.string_cache:
            return self.string_cache[pointer]
        header = self.bytes(pointer, 4)
        if not header or len(header) != 4:
            self.string_cache[pointer] = None
            return None
        length = struct.unpack("<I", header)[0]
        if not 0 < length <= 256:
            self.string_cache[pointer] = None
            return None
        raw = self.bytes(pointer + 4, length)
        try:
            value = raw.decode("utf-8") if raw and len(raw) == length else None
        except UnicodeDecodeError:
            value = None
        if value and any(ord(char) < 32 for char in value):
            value = None
        self.string_cache[pointer] = value
        return value

    def fm_nested_string_at(self, address: int) -> str | None:
        entry = self.ptr(address)
        return self.fm_string_at(entry) if entry else None

    def national_team_name(self, nation: int, fallback_uid: int) -> str:
        name = NATION_NAMES.get(fallback_uid)
        nation_offset = getattr(self.layout, "national_team_nation_offset", None)
        nationality_vtable_rva = getattr(self.layout, "nationality_vtable_rva", None)
        if nation_offset is not None and nationality_vtable_rva is not None:
            canonical_nation = self.ptr(nation + int(nation_offset))
            if (
                canonical_nation
                and self.ptr(canonical_nation) == self.module_base + int(nationality_vtable_rva)
            ):
                canonical_uid = self.u32(canonical_nation + ENTITY_UID)
                if canonical_uid and canonical_uid in NATION_NAMES:
                    name = NATION_NAMES[canonical_uid]
        return name or f"国家队 {fallback_uid}"

    def team(self, address: int) -> dict[str, Any] | None:
        if address in self.team_cache:
            return self.team_cache[address]
        vtable = self.ptr(address)
        if vtable == self.module_base + self.layout.national_team_vtable_rva:
            uid = self.u32(address + ENTITY_UID)
            nation = self.ptr(address + TEAM_CLUB)
            nation_uid = self.u32(nation + ENTITY_UID) if nation else None
            if not uid or uid != nation_uid or self.ptr(nation or 0) != self.module_base + self.layout.nation_vtable_rva:
                self.team_cache[address] = None
                return None
            competition = self.ptr(address + TEAM_COMP)
            stadium = self.ptr(address + TEAM_STADIUM)
            name = self.national_team_name(nation, uid)
            reputation = (
                self.u16(address + self.layout.team_reputation_offset)
                if self.layout.team_reputation_offset is not None else None
            )
            value = {
                "id": uid,
                "name": name,
                "short_name": name,
                "address": hex(address),
                "club_address": None,
                "competition": self.competition(competition) if competition else None,
                "stadium": self.fm_string_at(stadium + STADIUM_NAME) if stadium else None,
                "manager_address": hex(self.ptr(address + TEAM_MANAGER) or 0),
                "preferred_formation_code": None,
                "morale": None,
                "team_type": "national",
                "squad_type_code": None,
                "squad_type_name": None,
                "reputation": reputation if reputation is not None and 1 <= reputation <= 10000 else None,
            }
            self.team_cache[address] = value
            return value
        if vtable != self.module_base + self.layout.team_vtable_rva:
            self.team_cache[address] = None
            return None
        uid = self.u32(address + ENTITY_UID)
        club = self.ptr(address + TEAM_CLUB)
        if not uid or not club or self.ptr(club) != self.module_base + self.layout.club_vtable_rva:
            self.team_cache[address] = None
            return None
        # Reserve and B-team objects can have their own team UID while sharing
        # a separately identified club object.
        cino = self.ptr(club + CLUB_CINO)
        cint = self.ptr(club + CLUB_CINT)
        competition = self.ptr(address + TEAM_COMP)
        stadium = self.ptr(address + TEAM_STADIUM)
        reputation = (
            self.u16(address + self.layout.team_reputation_offset)
            if self.layout.team_reputation_offset is not None else None
        )
        team_type_offset = getattr(self.layout, "team_type_offset", None)
        squad_type_code = (
            self.u8(address + int(team_type_offset))
            if team_type_offset is not None else None
        )
        value = {
            "id": uid,
            "name": self.fm_string_at(club + CLUB_NAME_FULL),
            "short_name": self.fm_string_at(club + CLUB_NAME_SHORT),
            "address": hex(address),
            "club_address": hex(club),
            "competition": self.competition(competition) if competition else None,
            "stadium": self.fm_string_at(stadium + STADIUM_NAME) if stadium else None,
            "manager_address": hex(self.ptr(address + TEAM_MANAGER) or 0),
            "preferred_formation_code": self.u8(cino + CLUB_PREF_FORMATION) if cino else None,
            "morale": self.u8(cint + CLUB_MORALE) if cint else None,
            "team_type": "club",
            "squad_type_code": squad_type_code,
            "squad_type_name": CLUB_TEAM_TYPE_NAMES.get(squad_type_code),
            "reputation": reputation if reputation is not None and 1 <= reputation <= 10000 else None,
        }
        self.team_cache[address] = value
        return value

    def competition(self, season_or_comp: int) -> dict[str, Any] | None:
        if season_or_comp in self.comp_cache:
            return self.comp_cache[season_or_comp]
        actual = season_or_comp
        if self.ptr(actual) != self.module_base + self.layout.competition_vtable_rva:
            nested = self.ptr(season_or_comp + 0x18)
            if not nested or self.ptr(nested) != self.module_base + self.layout.competition_vtable_rva:
                self.comp_cache[season_or_comp] = None
                return None
            actual = nested
        uid = self.u32(actual + ENTITY_UID)
        if not uid:
            self.comp_cache[season_or_comp] = None
            return None
        value = {
            "id": uid,
            "name": self.fm_string_at(actual + COMP_NAME_FULL),
            "short_name": self.fm_string_at(actual + COMP_NAME_SHORT),
            "address": hex(actual),
        }
        self.comp_cache[season_or_comp] = value
        return value

    def roster(self, team_address: int) -> list[dict[str, Any]]:
        begin = self.ptr(team_address + TEAM_ROSTER_BEGIN)
        end = self.ptr(team_address + TEAM_ROSTER_END)
        if not begin or not end or end <= begin or (end - begin) % 8:
            return []
        count = (end - begin) // 8
        if not 1 <= count <= 200:
            return []
        players = []
        player_addresses = self.ptr_array(begin, count)
        # Roster expansion reads many fields from the same embedded player/person
        # object. Explicit page warming keeps this optimization local to roster
        # reads instead of changing every Reader user into a large auto-prefetcher.
        for player in player_addresses:
            if player:
                self.prefetch(int(player), 0x400)
        for player in player_addresses:
            if not player:
                continue
            player_vtable = self.ptr(player)
            player_rva = player_vtable - self.module_base if player_vtable else 0
            if player_rva in self.layout.player_and_non_player_vtable_rvas:
                person_offset = self.layout.player_and_non_player_person_offset
            elif player_rva in self.layout.actual_player_vtable_rvas:
                person_offset = self.layout.player_person_offset
            else:
                continue
            person = player + person_offset
            uid = self.u32(person + ENTITY_UID)
            if not uid:
                continue
            common = self.fm_nested_string_at(person + PERSON_COMMON_NAME)
            first = self.fm_nested_string_at(person + PERSON_FIRST_NAME)
            last = self.fm_nested_string_at(person + PERSON_LAST_NAME)
            full = self.fm_string_at(person + PERSON_FULL_NAME)
            if self.layout.key == "fm24":
                # FM24's name links precede the now-validated person layout by
                # eight bytes compared with the provisional mapping.
                first = self.fm_nested_string_at(person + 0x58) or first
                last = self.fm_nested_string_at(person + 0x60) or last
                common = self.fm_nested_string_at(person + 0x68) or common
                full = self.fm_string_at(person + 0x48) or full
                # Both ACTUAL_PLAYER variants expose the stable FM UID at
                # person+0x0C.  Older players are often represented by the
                # player/non-player hybrid object; using its session address
                # as the ID also caused the later profile pass to skip age and
                # nationality for exactly those players.
            joined_name = " ".join(part for part in (first, last) if part)
            if self.layout.key == "fm24":
                # FM24 can keep the database given/family names in English
                # while the direct full-name field contains the localized
                # Chinese display name. Prefer a confirmed CJK candidate, then
                # retain the existing common/given-family fallback order.
                localized_name = next(
                    (
                        value for value in (full, common, joined_name)
                        if value and any("\u3400" <= char <= "\u9fff" for char in value)
                    ),
                    None,
                )
                name = localized_name or common or joined_name or full
            else:
                name = common or joined_name or full
            ca = (
                self.u8(player + self.layout.player_ca_offset)
                if self.layout.player_ca_bytes == 1
                else self.u16(player + self.layout.player_ca_offset)
            )
            pa = self.u16(player + self.layout.player_pa_offset)
            fitness = (
                self.u16(player + self.layout.player_fitness_offset)
                if self.layout.player_fitness_offset is not None else None
            )
            # Some lower-league roster vectors retain deleted placeholder objects.
            # They have a player vtable but no readable person name and invalid CA.
            if not name or ca is None or not 1 <= ca <= 200:
                continue
            positions_raw = self.bytes(
                player + self.layout.player_positions_offset, len(POSITION_NAMES)
            ) or b""
            positions = {
                position: rating
                for position, rating in zip(POSITION_NAMES, positions_raw)
                # FM stores 1 as the default unavailable baseline.
                if rating > 1
            }
            # FM24 24.4.2 uses the same player+0xF8 availability container as
            # FM26. Live injured/healthy samples confirm the injury vector and
            # ban counter share the existing decoded layout.
            injury_offset = self.layout.player_injury_list_offset
            injury_ptr = self.ptr(player + injury_offset) if injury_offset is not None else None
            availability = self._availability_details(injury_ptr) if injury_ptr else None
            sharpness = (
                self.u16(player + self.layout.player_sharpness_offset)
                if self.layout.player_sharpness_offset is not None else None
            )
            fatigue = (
                self.u16(player + self.layout.player_fatigue_offset)
                if self.layout.player_fatigue_offset is not None else None
            )
            morale = (
                self.u8(player + self.layout.player_morale_offset)
                if self.layout.player_morale_offset is not None else None
            )
            players.append(
                {
                    "id": uid,
                    # Before the FM24 person mapping was completed, the UI used
                    # this session-local object address as the player ID.  Keep
                    # it briefly as a compatibility lookup key for aliases
                    # saved during the same running FM session.
                    "legacy_id": player if self.layout.key == "fm24" else None,
                    "name": name,
                    "ca": ca,
                    "pa": pa if pa is not None and 1 <= pa <= 200 else None,
                    "positions": positions,
                    "fitness_percent": round(fitness / 100, 2) if fitness is not None else None,
                    "fitness_raw": fitness,
                    "fatigue_raw": fatigue,
                    "sharpness_percent": round(sharpness / 100, 2) if sharpness is not None else None,
                    "sharpness_raw": sharpness,
                    "morale_raw": morale,
                    "availability": availability,
                    "address": hex(player),
                    "object_type": (
                        "actual_player_and_non_player"
                        if player_rva in self.layout.player_and_non_player_vtable_rvas
                        else "actual_player"
                    ),
                }
            )
        return players

    def roster_valuation(self, team_address: int) -> list[dict[str, int]]:
        """Read only the stable roster fields used by club valuation cards."""
        begin = self.ptr(team_address + TEAM_ROSTER_BEGIN)
        end = self.ptr(team_address + TEAM_ROSTER_END)
        if not begin or not end or end <= begin or (end - begin) % 8:
            return []
        count = (end - begin) // 8
        if not 1 <= count <= 200:
            return []
        players = []
        for player in self.ptr_array(begin, count):
            if not player:
                continue
            player_vtable = self.ptr(player)
            player_rva = player_vtable - self.module_base if player_vtable else 0
            if player_rva in self.layout.player_and_non_player_vtable_rvas:
                person_offset = self.layout.player_and_non_player_person_offset
            elif player_rva in self.layout.actual_player_vtable_rvas:
                person_offset = self.layout.player_person_offset
            else:
                continue
            uid = self.u32(player + person_offset + ENTITY_UID)
            ca = (
                self.u8(player + self.layout.player_ca_offset)
                if self.layout.player_ca_bytes == 1
                else self.u16(player + self.layout.player_ca_offset)
            )
            pa = self.u16(player + self.layout.player_pa_offset)
            if not uid or ca is None or not 1 <= int(ca) <= 200:
                continue
            players.append({
                "id": int(uid),
                "ca": int(ca),
                "pa": int(pa) if pa is not None and 1 <= int(pa) <= 200 else int(ca),
            })
        return players

    def _fixed_size_snapshots(
        self, addresses: Iterable[int], size: int, *, maximum_span: int = 8 * 1024 * 1024,
    ) -> dict[int, bytes]:
        """Read nearby fixed-size objects in a few contiguous process calls."""
        ordered = sorted({int(address) for address in addresses if int(address) > 0})
        if not ordered or size <= 0:
            return {}
        spans: list[tuple[int, int, list[int]]] = []
        for address in ordered:
            end = address + size
            if (
                spans
                and address - spans[-1][1] <= 0x1000
                and end - spans[-1][0] <= int(maximum_span)
            ):
                start, previous_end, members = spans[-1]
                members.append(address)
                spans[-1] = (start, max(previous_end, end), members)
            else:
                spans.append((address, end, [address]))
        snapshots: dict[int, bytes] = {}
        for start, end, members in spans:
            raw = self.bytes(start, end - start)
            if raw and len(raw) == end - start:
                for address in members:
                    offset = address - start
                    snapshots[address] = raw[offset:offset + size]
                continue
            for address in members:
                raw = self.bytes(address, size)
                if raw and len(raw) == size:
                    snapshots[address] = raw
        return snapshots

    def roster_model_snapshot(
        self, team_address: int,
    ) -> tuple[str, list[dict[str, Any]]]:
        """Read the live odds fields once and return both signature and squad."""
        begin = self.ptr(team_address + TEAM_ROSTER_BEGIN)
        end = self.ptr(team_address + TEAM_ROSTER_END)
        if not begin or not end or end <= begin or (end - begin) % 8:
            return "empty", []
        count = (end - begin) // 8
        if not 1 <= count <= 200:
            return "invalid", []
        player_addresses = [
            int(player) for player in self.ptr_array(begin, count) if player
        ]
        optional_offsets = [
            offset for offset in (
                self.layout.player_fitness_offset,
                self.layout.player_sharpness_offset,
                self.layout.player_morale_offset,
                self.layout.player_injury_list_offset,
            )
            if offset is not None
        ]
        header_size = max(
            8,
            int(self.layout.player_ca_offset) + int(self.layout.player_ca_bytes),
            int(self.layout.player_person_offset) + ENTITY_UID + 4,
            int(self.layout.player_and_non_player_person_offset) + ENTITY_UID + 4,
            *(int(offset) + 8 for offset in optional_offsets),
        )
        player_snapshots = self._fixed_size_snapshots(
            player_addresses, header_size,
        )

        def unpack(raw: bytes, offset: int | None, kind: str) -> int | None:
            if offset is None:
                return None
            width = struct.calcsize(kind)
            if int(offset) < 0 or int(offset) + width > len(raw):
                return None
            return int(struct.unpack_from(kind, raw, int(offset))[0])

        pending: list[dict[str, Any]] = []
        injury_addresses: list[int] = []
        for player in player_addresses:
            raw = player_snapshots.get(player)
            player_vtable = unpack(raw, 0, "<Q") if raw else self.ptr(player)
            player_rva = player_vtable - self.module_base if player_vtable else 0
            if player_rva in self.layout.player_and_non_player_vtable_rvas:
                person_offset = self.layout.player_and_non_player_person_offset
            elif player_rva in self.layout.actual_player_vtable_rvas:
                person_offset = self.layout.player_person_offset
            else:
                continue
            uid = (
                unpack(raw, int(person_offset) + ENTITY_UID, "<I")
                if raw else self.u32(player + int(person_offset) + ENTITY_UID)
            )
            ca_kind = "<B" if self.layout.player_ca_bytes == 1 else "<H"
            ca = (
                unpack(raw, self.layout.player_ca_offset, ca_kind)
                if raw else (
                    self.u8(player + int(self.layout.player_ca_offset))
                    if self.layout.player_ca_bytes == 1
                    else self.u16(player + int(self.layout.player_ca_offset))
                )
            )
            if not uid or ca is None or not 1 <= ca <= 200:
                continue
            fitness = (
                unpack(raw, self.layout.player_fitness_offset, "<H")
                if raw else (
                    self.u16(player + int(self.layout.player_fitness_offset))
                    if self.layout.player_fitness_offset is not None else None
                )
            )
            sharpness = (
                unpack(raw, self.layout.player_sharpness_offset, "<H")
                if raw else (
                    self.u16(player + int(self.layout.player_sharpness_offset))
                    if self.layout.player_sharpness_offset is not None else None
                )
            )
            morale = (
                unpack(raw, self.layout.player_morale_offset, "<B")
                if raw else (
                    self.u8(player + int(self.layout.player_morale_offset))
                    if self.layout.player_morale_offset is not None else None
                )
            )
            injury_offset = self.layout.player_injury_list_offset
            injury_ptr = (
                unpack(raw, injury_offset, "<Q")
                if raw else (
                    self.ptr(player + int(injury_offset))
                    if injury_offset is not None else None
                )
            )
            if injury_ptr:
                injury_addresses.append(int(injury_ptr))
            pending.append({
                "address": int(player), "uid": int(uid), "ca": int(ca),
                "fitness": fitness, "sharpness": sharpness, "morale": morale,
                "injury_ptr": int(injury_ptr or 0),
            })
        injury_snapshots = self._fixed_size_snapshots(injury_addresses, 0x28)
        rows = []
        squad: list[dict[str, Any]] = []
        for item in pending:
            injury_count = 0
            ban_count = 0
            availability = None
            injury_ptr = int(item["injury_ptr"])
            if injury_ptr:
                vectors = injury_snapshots.get(injury_ptr)
                if vectors and len(vectors) == 0x28:
                    injury_begin, injury_end = struct.unpack_from("<QQ", vectors, 0)
                    ban_begin, ban_end = struct.unpack_from("<QQ", vectors, 0x18)
                    if injury_begin <= injury_end and (injury_end - injury_begin) % 8 == 0:
                        injury_count = min((injury_end - injury_begin) // 8, 33)
                    if ban_begin <= ban_end and (ban_end - ban_begin) % 8 == 0:
                        ban_count = min((ban_end - ban_begin) // 8, 33)
                    availability = {
                        "injury_count": int(injury_count),
                        "ban_count": int(ban_count),
                    }
            rows.append((
                int(item["address"]), int(item["uid"]), int(item["ca"]),
                item["fitness"], item["sharpness"], item["morale"],
                int(injury_count), int(ban_count),
            ))
            squad.append({
                "id": int(item["uid"]),
                "address": hex(int(item["address"])),
                "ca": int(item["ca"]),
                "fitness_percent": (
                    round(int(item["fitness"]) / 100, 2)
                    if item["fitness"] is not None else None
                ),
                "sharpness_percent": (
                    round(int(item["sharpness"]) / 100, 2)
                    if item["sharpness"] is not None else None
                ),
                "morale_raw": item["morale"],
                "availability": availability,
            })
        payload = json.dumps(rows, ensure_ascii=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("ascii")).hexdigest(), squad

    def roster_model_signature(self, team_address: int) -> str:
        """Hash only the live roster fields that influence the odds team profile."""
        signature, _squad = self.roster_model_snapshot(team_address)
        return signature

    def _availability_details(self, container: int) -> dict[str, Any] | None:
        injury_begin = self.ptr(container)
        injury_end = self.ptr(container + 0x08)
        ban_begin = self.ptr(container + 0x18)
        ban_end = self.ptr(container + 0x20)

        def count(begin: int | None, end: int | None) -> int | None:
            if begin is None and end is None:
                return 0
            if not begin or not end or end < begin or (end - begin) % 8:
                return None
            value = (end - begin) // 8
            return value if value <= 32 else None

        injury_count = count(injury_begin, injury_end)
        bans = count(ban_begin, ban_end)
        if injury_count is None and bans is None:
            return None

        injuries = []
        if injury_count and injury_begin:
            for index in range(injury_count):
                record = self.ptr(injury_begin + index * 8)
                if not record:
                    continue
                injury_type = self.ptr(record + 0x08)
                start_date = decode_date(self.u32(record + 0x20) or 0)
                duration_high = self.u16(record + 0x28)
                duration_low = self.u16(record + 0x2A)
                lower = min(duration_low, duration_high) if None not in (duration_low, duration_high) else None
                upper = max(duration_low, duration_high) if None not in (duration_low, duration_high) else None
                injuries.append({
                    "type": self.fm_string_at(injury_type + 0x18) if injury_type else None,
                    "start_date": start_date.isoformat() if start_date else None,
                    "duration_days_low_raw": lower,
                    "duration_days_high_raw": upper,
                    "estimated_return_from": (
                        (start_date + timedelta(days=lower)).isoformat()
                        if start_date and lower is not None else None
                    ),
                    "estimated_return_to": (
                        (start_date + timedelta(days=upper)).isoformat()
                        if start_date and upper is not None else None
                    ),
                })
        unavailability = []
        if bans and ban_begin:
            for index in range(bans):
                record = self.ptr(ban_begin + index * 8)
                if not record:
                    continue
                scope = self.u8(record + 0x05)
                start_date = decode_date(self.u32(record + 0x10) or 0)
                number_of_days = self.u16(record + 0x16)
                reason = self.u8(record + 0x1A)
                unavailability.append({
                    "scope": scope,
                    "start_date": start_date.isoformat() if start_date else None,
                    "number_of_days": number_of_days,
                    "reason": reason,
                    "medical": scope == 29,
                })
        return {
            "injury_count": injury_count,
            "ban_count": bans,
            "unavailability_count": len(unavailability),
            "medical_unavailability_count": sum(
                1 for item in unavailability if item["medical"]
            ),
            "injuries": injuries,
            "unavailability": unavailability,
        }


def decode_date(code: int) -> date | None:
    year = code >> 16
    day_of_year = code & 0x1FF
    if not 1900 <= year <= 2200 or not 1 <= day_of_year <= 366:
        return None
    try:
        return date(year, 1, 1) + timedelta(days=day_of_year - 1)
    except ValueError:
        return None


def decode_kickoff_minutes(code: int) -> int | None:
    """Decode FM's quarter-hour fixture slot into minutes after midnight."""
    slot = (code >> 9) & 0x7F
    if slot == 0:
        return None
    return ((slot + 23) * 15) % (24 * 60)


def executable_file_version(path: str) -> str | None:
    try:
        stat = Path(path).stat()
    except OSError:
        return None
    return _cached_executable_file_version(path, stat.st_size, stat.st_mtime_ns)


@lru_cache(maxsize=16)
def _cached_executable_file_version(
    path: str, _size: int, _modified_ns: int,
) -> str | None:
    """Read the fixed Windows file version without starting another process."""
    if not path:
        return None
    try:
        from ctypes import wintypes

        api = ctypes.WinDLL("version", use_last_error=True)
        api.GetFileVersionInfoSizeW.argtypes = [
            wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD),
        ]
        api.GetFileVersionInfoSizeW.restype = wintypes.DWORD
        api.GetFileVersionInfoW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
        ]
        api.GetFileVersionInfoW.restype = wintypes.BOOL
        api.VerQueryValueW.argtypes = [
            wintypes.LPCVOID, wintypes.LPCWSTR,
            ctypes.POINTER(wintypes.LPVOID), ctypes.POINTER(wintypes.UINT),
        ]
        api.VerQueryValueW.restype = wintypes.BOOL

        ignored = wintypes.DWORD()
        size = api.GetFileVersionInfoSizeW(path, ctypes.byref(ignored))
        if not size:
            return None
        buffer = ctypes.create_string_buffer(size)
        if not api.GetFileVersionInfoW(path, 0, size, buffer):
            return None
        value = wintypes.LPVOID()
        length = wintypes.UINT()
        if api.VerQueryValueW(
            buffer, "\\VarFileInfo\\Translation",
            ctypes.byref(value), ctypes.byref(length),
        ) and length.value >= 4:
            translations = ctypes.cast(
                value, ctypes.POINTER(ctypes.c_uint16 * (length.value // 2)),
            ).contents
            for index in range(0, len(translations) - 1, 2):
                key = f"{translations[index]:04x}{translations[index + 1]:04x}"
                string_value = wintypes.LPVOID()
                string_length = wintypes.UINT()
                if api.VerQueryValueW(
                    buffer, f"\\StringFileInfo\\{key}\\FileVersion",
                    ctypes.byref(string_value), ctypes.byref(string_length),
                ) and string_length.value:
                    detected = ctypes.wstring_at(
                        string_value, string_length.value,
                    ).rstrip("\0").strip()
                    if detected:
                        return detected
        if not api.VerQueryValueW(
            buffer, "\\", ctypes.byref(value), ctypes.byref(length),
        ):
            return None
        words = ctypes.cast(
            value, ctypes.POINTER(ctypes.c_uint32 * 13),
        ).contents
        if words[0] != 0xFEEF04BD:
            return None
        return ".".join(str(part) for part in (
            words[2] >> 16, words[2] & 0xFFFF,
            words[3] >> 16, words[3] & 0xFFFF,
        ))
    except (AttributeError, OSError, TypeError, ValueError):
        return None


def compact_build_version(version: str | None) -> str:
    parts = [part for part in str(version or "").split(".") if part]
    while len(parts) > 3 and parts[-1] == "0":
        parts.pop()
    if len(".".join(parts)) > 12 and len(parts) > 3:
        parts = parts[:3]
    return ".".join(parts)


def is_unity_engine_version(version: str | None) -> bool:
    parts = str(version or "").split(".")
    return bool(parts and parts[0].isdigit() and int(parts[0]) >= 6000)


def running_supported_processes() -> list[dict[str, Any]]:
    matches = [
        item for item in find_processes("fm")
        if item.name.casefold() == "fm.exe" and item.thread_count > 0
    ]
    rows: list[dict[str, Any]] = []
    for item in matches:
        path = item.path or ""
        layout = layout_for_executable(path)
        if not layout:
            try:
                with open_process(int(item.pid)) as process:
                    module = find_module(process, GAME_PLUGIN)
                    if module and is_fm26_xgp_module(process, module):
                        layout = FM26_XGP_TEMPLATE
                    if not layout:
                        module = find_module(process, "fm.exe")
                        if module and is_fm24_xgp_module(process, module):
                            layout = FM24_XGP_LAYOUT
            except OSError:
                layout = None
        if not layout:
            continue
        file_version = executable_file_version(path)
        unity_version = (
            file_version
            if layout.key == "fm26" and is_unity_engine_version(file_version)
            else None
        )
        build_version = getattr(layout, "game_version", None) or (
            None if unity_version else file_version
        )
        row = {
            "pid": int(item.pid), "path": path, "thread_count": int(item.thread_count),
            "layout": layout, "key": layout.key, "display_name": layout.display_name,
            "version": "24" if layout.key == "fm24" else "26" if layout.key == "fm26" else layout.key,
            "distribution": layout.distribution,
            "file_version": file_version or "",
            "build_version": build_version or "",
            "build_label": compact_build_version(build_version),
            "unity_version": unity_version or "",
            "unity_label": compact_build_version(unity_version),
        }
        rows.append(row)
    return sorted(
        rows,
        key=lambda row: (
            str(row["version"]), -int(row["thread_count"]), int(row["pid"]),
        ),
    )


def _representative_processes(
    rows: list[dict[str, Any]], preferred_pid: int | None = None,
) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = str(row["key"])
        previous = grouped.get(key)
        if previous is None or (
            int(row["pid"]) == int(preferred_pid or 0)
            and int(previous["pid"]) != int(preferred_pid or 0)
        ):
            grouped[key] = row
    return sorted(grouped.values(), key=lambda row: str(row["version"]))


def fm_process_is_running(pid: int) -> bool:
    """Return whether the exact FM process behind a verified connection still exists."""
    target = int(pid or 0)
    if target <= 0:
        return False
    return any(
        int(item.pid) == target
        and item.name.casefold() == "fm.exe"
        and int(item.thread_count) > 0
        for item in find_processes("fm")
    )


def select_game_layout(key: str) -> dict[str, Any]:
    available = _representative_processes(running_supported_processes())
    selected = next((row for row in available if row["key"] == str(key)), None)
    if not selected:
        raise ValueError("所选 Football Manager 版本当前没有运行")
    select_game_process(
        str(key), int(selected["pid"]),
        selected=(int(selected["pid"]), str(selected["path"]), selected["layout"]),
    )
    return process_selection_status()


def select_game_process(
    key: str, pid: int, *, selected: tuple[int, str, GameLayout] | None = None,
) -> bool:
    """Pin reads to one process and preserve an unchanged live read session.

    ``selected`` is the already verified result from process discovery. Keeping
    it avoids a second process scan/layout resolution in the asynchronous
    connection worker. Callers without that result retain lazy resolution.
    """
    global _PREFERRED_LAYOUT_KEY, _PREFERRED_PROCESS_PID, _PROCESS_LAYOUT_CACHE
    target_key = str(key)
    target_pid = int(pid)
    resolved = selected
    if resolved is not None:
        resolved = resolve_selected_process_layout(resolved)
        resolved_pid, _resolved_path, resolved_layout = resolved
        if (
            int(resolved_pid) != target_pid
            or str(getattr(resolved_layout, "key", "")) != target_key
        ):
            raise ValueError(
                "selected Football Manager process does not match the requested target"
            )
    with _PROCESS_SELECTION_LOCK:
        cached = _PROCESS_LAYOUT_CACHE
        cached_layout = (
            cached[3][2]
            if (
                cached
                and cached[1] == target_key
                and int(cached[2] or 0) == target_pid
            ) else None
        )
        changed = (
            _PREFERRED_LAYOUT_KEY != target_key
            or int(_PREFERRED_PROCESS_PID or 0) != target_pid
            or (
                resolved is not None
                and cached_layout is not None
                and cached_layout != resolved_layout
            )
        )
        _PREFERRED_LAYOUT_KEY = target_key
        _PREFERRED_PROCESS_PID = target_pid
        if resolved is not None:
            _PROCESS_LAYOUT_CACHE = (
                time.monotonic(), target_key, target_pid, resolved,
            )
        elif changed:
            _PROCESS_LAYOUT_CACHE = None
    if not changed:
        return False
    from tools.game_session import invalidate_game_read_sessions
    invalidate_game_read_sessions()
    return True


def resolve_selected_process_layout(
    selected: tuple[int, str, GameLayout],
) -> tuple[int, str, GameLayout]:
    """Resolve a dynamic XGP template before pinning it to a read session."""
    pid, process_path, layout = selected
    if not (
        str(getattr(layout, "key", "")) == "fm26"
        and str(getattr(layout, "distribution", "")) == "xgp"
        and getattr(layout, "game_date_rva", None) is None
    ):
        return selected
    with open_process(int(pid)) as process:
        module = find_module(process, GAME_PLUGIN)
        if module is None:
            raise RuntimeError("FM26 XGP game_plugin.dll is not loaded")
        resolved = resolve_fm26_xgp_layout(
            int(pid), str(process_path), int(module.base_address),
            int(module.size), str(module.path),
        )
    return int(pid), str(process_path), resolved


def process_selection_status(*, include_internal: bool = False) -> dict[str, Any]:
    running = running_supported_processes()
    with _PROCESS_SELECTION_LOCK:
        preferred = _PREFERRED_LAYOUT_KEY
        preferred_pid = _PREFERRED_PROCESS_PID
    available = _representative_processes(running, preferred_pid)
    selected = next((row for row in available if row["key"] == preferred), None)
    if selected is None and available:
        selected = next((row for row in available if row["key"] == "fm26"), available[0])
    result = {
        "selected": selected["key"] if selected else None,
        "selected_pid": int(selected["pid"]) if selected else None,
        "available": [
            {key: row[key] for key in (
                "key", "version", "display_name", "pid", "distribution",
                "file_version", "build_version", "build_label",
                "unity_version", "unity_label",
            )}
            for row in available
        ],
    }
    if include_internal and selected is not None:
        result["_selected_process"] = (
            int(selected["pid"]), str(selected["path"]), selected["layout"],
        )
    return result


def select_process() -> tuple[int, str]:
    available = running_supported_processes()
    if not available:
        raise RuntimeError("supported fm.exe is not running")
    with _PROCESS_SELECTION_LOCK:
        selected_key = _PREFERRED_LAYOUT_KEY
        selected_pid = _PREFERRED_PROCESS_PID
    selected = next((
        row for row in available
        if int(row["pid"]) == int(selected_pid or 0)
        and (not selected_key or row["key"] == selected_key)
    ), None)
    if selected is None and selected_pid is None:
        representatives = _representative_processes(available)
        selected = next(
            (row for row in representatives if row["key"] == selected_key),
            None,
        )
        if selected is None and representatives:
            selected = next(
                (row for row in representatives if row["key"] == "fm26"),
                representatives[0],
            )
    if not selected:
        raise RuntimeError("selected fm.exe is not running")
    return int(selected["pid"]), str(selected["path"])


def select_process_layout() -> tuple[int, str, GameLayout]:
    global _PROCESS_LAYOUT_CACHE
    with _PROCESS_SELECTION_LOCK:
        cached = _PROCESS_LAYOUT_CACHE
        preferred = _PREFERRED_LAYOUT_KEY
        preferred_pid = _PREFERRED_PROCESS_PID
        if (
            cached
            and cached[1] == preferred
            and cached[2] == preferred_pid
            and time.monotonic() - cached[0] < _PROCESS_LAYOUT_CACHE_SECONDS
        ):
            return cached[3]
    # A connected GameReadSession already owns a verified handle, module and
    # exact layout identity.  Prefer its cheap module-liveness check after the
    # short discovery cache expires; legacy callers then avoid repeatedly
    # enumerating processes and resolving the executable while keeping the
    # original full discovery path when the session is absent or stale.
    from tools.game_session import active_game_read_selection

    active = active_game_read_selection()
    if active is not None:
        active_pid, _active_path, active_layout = active
        if (
            (not preferred or str(getattr(active_layout, "key", "")) == preferred)
            and (preferred_pid is None or int(active_pid) == int(preferred_pid))
        ):
            with _PROCESS_SELECTION_LOCK:
                if (
                    _PREFERRED_LAYOUT_KEY == preferred
                    and _PREFERRED_PROCESS_PID == preferred_pid
                ):
                    _PROCESS_LAYOUT_CACHE = (
                        time.monotonic(), preferred, preferred_pid, active,
                    )
                    return active
    pid, path = select_process()
    layout = layout_for_executable(path)
    if not layout:
        with open_process(pid) as process:
            module = find_module(process, GAME_PLUGIN)
            if module and is_fm26_xgp_module(process, module):
                layout = resolve_fm26_xgp_layout(
                    pid, path, module.base_address, module.size, module.path,
                )
            if not layout:
                module = find_module(process, "fm.exe")
                if module and is_fm24_xgp_module(process, module):
                    layout = resolve_fm24_xgp_layout(
                        pid, module.base_address, module.size,
                    )
    if layout and str(getattr(layout, "key", "")) == "fm26" and str(
        getattr(layout, "distribution", "")
    ) == "steam" and str(getattr(layout, "module_name", "")) == GAME_PLUGIN:
        # The EXE identity is shared by minor Steam updates. Resolve the date
        # from the actually loaded plugin, then require the rest of that
        # plugin's profile to be the fully verified Steam layout.
        with open_process(pid) as process:
            module = find_module(process, GAME_PLUGIN)
        if not module:
            raise RuntimeError("FM26 Steam game_plugin.dll is not loaded")
        layout = resolve_fm26_steam_layout(
            pid, int(module.base_address), int(module.size), str(module.path),
        )
    if not layout:
        try:
            detail = sha256(path)
        except OSError:
            detail = path or "unreadable executable"
        raise RuntimeError("unsupported fm.exe build; refusing to use offsets: " + detail)
    selected = (pid, path, layout)
    with _PROCESS_SELECTION_LOCK:
        if (
            _PREFERRED_LAYOUT_KEY == preferred
            and _PREFERRED_PROCESS_PID == preferred_pid
        ):
            _PROCESS_LAYOUT_CACHE = (
                time.monotonic(), preferred, preferred_pid, selected,
            )
    return selected


def invalidate_process_layout_cache() -> None:
    global _PROCESS_LAYOUT_CACHE
    with _PROCESS_SELECTION_LOCK:
        _PROCESS_LAYOUT_CACHE = None


def sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()














def scan_result_fingerprints_in_spans(
    reader: Reader,
    spans: list[tuple[int, int]],
) -> tuple[dict[int, bytes], int]:
    """Scan previously identified result-pool spans without probing the whole heap."""
    result_rva = reader.layout.fixture_result_vtable_rva
    if result_rva is None:
        return {}, 0
    needle = struct.pack("<Q", reader.module_base + result_rva)
    fingerprints: dict[int, bytes] = {}
    bytes_scanned = 0
    object_size = 0x80
    overlap = object_size - 1
    for base_address, region_size in spans:
        carry = b""
        for offset in range(0, region_size, 8 * 1024 * 1024):
            size = min(8 * 1024 * 1024, region_size - offset)
            block = reader.bytes(base_address + offset, size)
            if not block:
                carry = b""
                continue
            bytes_scanned += len(block)
            data = carry + block
            origin = base_address + offset - len(carry)
            start = 0
            while True:
                found = data.find(needle, start)
                if found < 0:
                    break
                if found + object_size <= len(data):
                    fingerprints[origin + found] = data[found:found + object_size]
                start = found + 1
            carry = data[-overlap:] if len(data) >= overlap else data
    return fingerprints, bytes_scanned


def read_result_fingerprints_at_addresses(
    reader: Reader,
    addresses: list[int],
    progress: Callable[[int, int], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> tuple[dict[int, bytes], int]:
    """Read known FIXTURE_RESULT slots without rescanning their heap slabs."""
    result_rva = reader.layout.fixture_result_vtable_rva
    if result_rva is None:
        return {}, 0
    expected_vtable = struct.pack("<Q", reader.module_base + result_rva)
    fingerprints: dict[int, bytes] = {}
    bytes_read = 0
    ordered = sorted(set(addresses))
    start = 0
    while start < len(ordered):
        if cancel_check and cancel_check():
            raise RuntimeError("refresh cancelled")
        stop = start + 1
        span_start = ordered[start]
        while (
            stop < len(ordered)
            and ordered[stop] - ordered[stop - 1] <= 0x1000
            and ordered[stop] + 0x80 - span_start <= 8 * 1024 * 1024
        ):
            stop += 1
        span_end = ordered[stop - 1] + 0x80
        block = reader.bytes(span_start, span_end - span_start)
        if block and len(block) == span_end - span_start:
            bytes_read += len(block)
            for address in ordered[start:stop]:
                offset = address - span_start
                raw = block[offset:offset + 0x80]
                if raw[:8] == expected_vtable:
                    fingerprints[address] = raw
        else:
            for address in ordered[start:stop]:
                raw = reader.bytes(address, 0x80)
                if not raw or len(raw) != 0x80:
                    continue
                bytes_read += len(raw)
                if raw[:8] == expected_vtable:
                    fingerprints[address] = raw
        start = stop
        if progress:
            progress(start, len(ordered))
    return fingerprints, bytes_read


def read_fixture_snapshots_at_addresses(
    reader: Reader, addresses: list[int],
    progress: Callable[[int, int], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> tuple[dict[int, bytes], int]:
    """Bulk-read validated fixture headers from their already discovered slabs."""
    expected_vtable = struct.pack(
        "<Q", reader.module_base + reader.layout.fixture_vtable_rva,
    )
    snapshots: dict[int, bytes] = {}
    bytes_read = 0
    ordered = sorted(set(addresses))
    start = 0
    while start < len(ordered):
        if cancel_check and cancel_check():
            raise RuntimeError("refresh cancelled")
        stop = start + 1
        span_start = ordered[start]
        while (
            stop < len(ordered)
            and ordered[stop] - ordered[stop - 1] <= 0x1000
            and ordered[stop] + FIXTURE_SIZE - span_start <= 8 * 1024 * 1024
        ):
            stop += 1
        span_end = ordered[stop - 1] + FIXTURE_SIZE
        block = reader.bytes(span_start, span_end - span_start)
        if block and len(block) == span_end - span_start:
            bytes_read += len(block)
            for address in ordered[start:stop]:
                offset = address - span_start
                raw = block[offset:offset + FIXTURE_SIZE]
                if raw[:8] == expected_vtable:
                    snapshots[address] = raw
        else:
            for address in ordered[start:stop]:
                raw = reader.bytes(address, FIXTURE_SIZE)
                if not raw or len(raw) != FIXTURE_SIZE:
                    continue
                bytes_read += len(raw)
                if raw[:8] == expected_vtable:
                    snapshots[address] = raw
        start = stop
        if progress:
            progress(start, len(ordered))
    return snapshots, bytes_read




def scan_season_result_addresses(reader: Reader) -> tuple[list[int], int]:
    result_rva = reader.layout.season_result_vtable_rva
    if result_rva is None:
        return [], 0
    needle = struct.pack("<Q", reader.module_base + result_rva)
    addresses = []
    bytes_scanned = 0
    overlap = len(needle) - 1
    for region in iter_readable_regions(reader.process):
        if region.type != MEM_PRIVATE or not 0x10000 <= region.size <= 128 * 1024 * 1024:
            continue
        carry = b""
        for offset in range(0, region.size, 8 * 1024 * 1024):
            block = reader.bytes(
                region.base_address + offset,
                min(8 * 1024 * 1024, region.size - offset),
            )
            if not block:
                carry = b""
                continue
            bytes_scanned += len(block)
            data = carry + block
            origin = region.base_address + offset - len(carry)
            start = 0
            while True:
                found = data.find(needle, start)
                if found < 0:
                    break
                addresses.append(origin + found)
                start = found + 1
            carry = data[-overlap:] if len(data) >= overlap else data
    return sorted(set(addresses)), bytes_scanned


def valid_completed_result_outcome(
    home_goals: int,
    away_goals: int,
    home_outcome: int,
    away_outcome: int,
) -> bool:
    """Validate FM's completion code while keeping the regular-time score.

    For knockout matches that are level after 90 minutes, FM stores the
    advancement/penalty outcome instead of the ordinary ``9, 9`` draw code.
    The secondary code is build- and competition-dependent, so only its
    completion shape is constrained here: one side advances (10), the other
    has a non-zero elimination code.  Score extraction remains strictly from
    the regular-time fields above.
    """
    if not (1 <= home_outcome <= 10 and 1 <= away_outcome <= 10):
        return False
    if home_goals > away_goals:
        # FM normally stores (1, 10), but a knockout advancement code can
        # replace the winning side's 1 with another non-zero code.
        return away_outcome == 10 and home_outcome != 10
    if home_goals < away_goals:
        return home_outcome == 10 and away_outcome != 10
    if (home_outcome, away_outcome) == (9, 9):
        return True
    return (home_outcome == 10) != (away_outcome == 10)


def fm26_result_decision(
    home_goals: int, away_goals: int,
    extra_home: int, extra_away: int,
    penalty_home: int, penalty_away: int,
    home_outcome: int, away_outcome: int,
) -> dict[str, Any]:
    """Decode FM26's native knockout result fields without changing 90-minute scores."""
    details: dict[str, Any] = {
        "home_outcome_code": int(home_outcome),
        "away_outcome_code": int(away_outcome),
    }
    winner = (
        "home" if away_outcome == 10 and home_outcome != 10
        else "away" if home_outcome == 10 and away_outcome != 10
        else None
    )
    if winner:
        details["winner_side"] = winner
    if extra_home != 0xFF and extra_away != 0xFF:
        details["after_extra_time_home_goals"] = int(extra_home)
        details["after_extra_time_away_goals"] = int(extra_away)
    if penalty_home != 0xFF and penalty_away != 0xFF:
        details["penalty_shootout_home_goals"] = int(penalty_home)
        details["penalty_shootout_away_goals"] = int(penalty_away)
    if winner and penalty_home != 0xFF and penalty_away != 0xFF:
        details["decided_by"] = "penalties"
    elif (
        winner and home_goals == away_goals
        and extra_home != 0xFF and extra_away != 0xFF
        and extra_home != extra_away
    ):
        details["decided_by"] = "extra_time"
    elif winner and home_goals != away_goals:
        details["decided_by"] = "regular"
    elif winner:
        details["decided_by"] = "other_knockout_rule"
    return details


def parse_season_completed_result(
    reader: Reader,
    address: int,
    competition_override: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Parse FM's persistent season record, using the regular-time score only."""
    raw = reader.bytes(address, 0xC0)
    if not raw or len(raw) != 0xC0:
        return None
    match_date_code = struct.unpack_from("<I", raw, 0x10)[0]
    match_date = decode_date(match_date_code)
    kickoff_minutes = decode_kickoff_minutes(match_date_code)
    home_pointer, away_pointer = struct.unpack_from("<QQ", raw, 0x18)
    # FM26 stores the competition-season pointer at +0x38 in the current
    # season-record layout. Keep +0xB8 as a compatibility fallback for older
    # captures, where that field was observed in a different build.
    competition_pointers = (
        struct.unpack_from("<Q", raw, 0x38)[0],
        struct.unpack_from("<Q", raw, 0xB8)[0],
    )
    home = reader.team(home_pointer)
    away = reader.team(away_pointer)
    competition = next(
        (reader.competition(pointer) for pointer in competition_pointers if pointer),
        None,
    ) or competition_override
    if not match_date or not home or not away or not competition:
        return None

    home_goals, away_goals = raw[0x28], raw[0x29]
    home_outcome, away_outcome = raw[0x33], raw[0x34]
    decision = {}
    if reader.layout.result_event_record_format == "fm26":
        # Persistent FM26 scorelines retain the 90-minute, after-extra-time,
        # and shootout totals even after the short-lived result object expires.
        home_goals, away_goals = raw[0x2A], raw[0x2B]
        decision = fm26_result_decision(
            home_goals, away_goals, raw[0x2C], raw[0x2D],
            raw[0x2E], raw[0x2F], home_outcome, away_outcome,
        )
    if home_goals > 30 or away_goals > 30 or not valid_completed_result_outcome(
        home_goals, away_goals, home_outcome, away_outcome
    ):
        return None
    return {
        "date": match_date.isoformat(),
        "kickoff_minutes": kickoff_minutes,
        "kickoff_time": (
            f"{kickoff_minutes // 60:02d}:{kickoff_minutes % 60:02d}"
            if kickoff_minutes is not None else None
        ),
        "status": "completed",
        "competition": competition,
        "home_team": {"id": home["id"], "name": home["short_name"] or home["name"]},
        "away_team": {"id": away["id"], "name": away["short_name"] or away["name"]},
        "home_goals": home_goals,
        "away_goals": away_goals,
        **decision,
        "result_address": hex(address),
        "result_source": "persistent_season_record",
    }


def parse_completed_result_from_raw(
    reader: Reader, address: int, raw: bytes,
) -> dict[str, Any] | None:
    if not raw or len(raw) != 0x80:
        return None
    home_pointer, away_pointer, official_pointer, comp_pointer, stadium_pointer = struct.unpack_from(
        "<QQQQQ", raw, 0x08
    )
    home = reader.team(home_pointer)
    away = reader.team(away_pointer)
    competition = reader.competition(comp_pointer)
    match_date_code = struct.unpack_from("<I", raw, 0x4C)[0]
    match_date = decode_date(match_date_code)
    kickoff_minutes = decode_kickoff_minutes(match_date_code)
    if not home or not away or not competition or not match_date:
        return None

    home_goals = raw[reader.layout.result_home_goals_offset]
    away_goals = raw[reader.layout.result_away_goals_offset]
    home_outcome = raw[reader.layout.result_home_outcome_offset]
    away_outcome = raw[reader.layout.result_away_outcome_offset]
    if home_goals > 30 or away_goals > 30 or not valid_completed_result_outcome(
        home_goals, away_goals, home_outcome, away_outcome
    ):
        return None

    decision = {}
    if reader.layout.result_event_record_format == "fm26":
        decision = fm26_result_decision(
            home_goals, away_goals,
            raw[reader.layout.result_home_goals_offset + 1],
            raw[reader.layout.result_away_goals_offset + 1],
            raw[reader.layout.result_home_goals_offset + 2],
            raw[reader.layout.result_away_goals_offset + 2],
            home_outcome, away_outcome,
        )

    return {
        "date": match_date.isoformat(),
        "kickoff_minutes": kickoff_minutes,
        "kickoff_time": (
            f"{kickoff_minutes // 60:02d}:{kickoff_minutes % 60:02d}"
            if kickoff_minutes is not None else None
        ),
        "status": "completed",
        "competition": competition,
        "home_team": {"id": home["id"], "name": home["short_name"] or home["name"]},
        "away_team": {"id": away["id"], "name": away["short_name"] or away["name"]},
        "home_goals": home_goals,
        "away_goals": away_goals,
        **decision,
        "stadium": reader.fm_string_at(stadium_pointer + STADIUM_NAME) if stadium_pointer else None,
        "result_address": hex(address),
        "result_source": "fixture_result_archive",
        "official_address": hex(official_pointer) if official_pointer else None,
    }


def parse_completed_result(reader: Reader, address: int) -> dict[str, Any] | None:
    raw = reader.bytes(address, 0x80)
    return parse_completed_result_from_raw(reader, address, raw) if raw else None


def decode_match_minute(code: int) -> dict[str, Any]:
    minute = code & 0xFF
    stoppage = code >> 8
    display = f"{minute}+{stoppage}" if stoppage else str(minute)
    return {"minute": minute, "stoppage": stoppage, "display": display, "raw": code}


def _read_result_event_records(
    reader: Reader, result_address: int, expected_goals: int,
) -> list[dict[str, int]] | None:
    """Normalize the version-specific compact goal vector."""
    detail_root = reader.ptr(result_address + reader.layout.result_event_root_offset)
    if not detail_root:
        return None
    vector = reader.bytes(detail_root, 24)
    if not vector or len(vector) != 24:
        return None
    begin, end, capacity = struct.unpack("<QQQ", vector)
    record_size = reader.layout.result_event_record_size
    record_count = (end - begin) // record_size if record_size else -1
    count_is_valid = (
        expected_goals <= record_count <= max(expected_goals + 32, 32)
        if reader.layout.result_event_record_format == "fm24"
        else expected_goals <= record_count <= max(expected_goals + 16, 16)
    )
    if not (
        0x10000 <= begin <= end <= capacity
        and record_size > 0
        and (end - begin) % record_size == 0
        and count_is_valid
    ):
        return None
    event_data = reader.bytes(begin, end - begin)
    if event_data is None or len(event_data) != end - begin:
        return None

    records = []
    for offset in range(0, len(event_data), record_size):
        if reader.layout.result_event_record_format == "fm24":
            scorer, flags, minute_code = struct.unpack_from("<IHH", event_data, offset)
            records.append({
                "scorer": scorer,
                "assist": 0xFFFFFFFF,
                "clock": minute_code,
                "flags": flags,
                "minute_code": minute_code,
            })
        else:
            scorer, assist, clock, flags = struct.unpack_from("<IIII", event_data, offset)
            records.append({
                "scorer": scorer,
                "assist": assist,
                "clock": clock,
                "flags": flags,
                "minute_code": (flags >> 16) & 0xFFFF,
            })
    return records


def _event_summary(records: list[dict[str, int]]) -> dict[str, Any]:
    half_home = half_away = 0
    first_event: tuple[int, int, str] | None = None
    for event_order, event in enumerate(records):
        away_side = bool(event["flags"] & 1)
        side = "away" if away_side else "home"
        candidate = (event["clock"], event_order, side)
        if first_event is None or candidate[:2] < first_event[:2]:
            first_event = candidate
        if event["minute_code"] & 0xFF <= 45:
            if away_side:
                half_away += 1
            else:
                half_home += 1
    return {
        "half_home_goals": half_home,
        "half_away_goals": half_away,
        "first_scoring_team": first_event[2] if first_event else "none",
    }


def reconcile_result_event_summary(
    records: list[dict[str, int]], home_goals: int, away_goals: int,
) -> dict[str, Any] | None:
    """Reconcile FM26 goal events against the immutable 90-minute score.

    Knockout results retain extra-time goals in the event vector while the
    scoreline fields used by normal betting markets remain the score after 90
    minutes.  Treat post-90 events as removable only when the vector contains
    more goals than that scoreline; an ordinary stoppage-time goal is retained
    when the counts already agree.
    """
    observed = (
        sum(not bool(item["flags"] & 1) for item in records),
        sum(bool(item["flags"] & 1) for item in records),
    )
    expected = (int(home_goals), int(away_goals))
    if observed == expected:
        return _event_summary(records)
    excess = (observed[0] - expected[0], observed[1] - expected[1])
    if excess[0] < 0 or excess[1] < 0:
        return None
    candidates = (
        [
            index for index, item in enumerate(records)
            if not item["flags"] & 1
            and (item["flags"] & 0x300 or item["minute_code"] & 0xFF > 90)
        ],
        [
            index for index, item in enumerate(records)
            if item["flags"] & 1
            and (item["flags"] & 0x300 or item["minute_code"] & 0xFF > 90)
        ],
    )
    if any(len(candidates[side]) < excess[side] for side in (0, 1)):
        return None
    choices = [
        list(combinations(candidates[side], excess[side]))
        for side in (0, 1)
    ]
    if len(choices[0]) * len(choices[1]) > 256:
        return None
    summaries: dict[tuple[int, int, str], dict[str, Any]] = {}
    for home_removed, away_removed in product(*choices):
        removed = set(home_removed) | set(away_removed)
        summary = _event_summary([
            item for index, item in enumerate(records) if index not in removed
        ])
        signature = (
            int(summary["half_home_goals"]), int(summary["half_away_goals"]),
            str(summary["first_scoring_team"]),
        )
        summaries[signature] = summary
        if len(summaries) > 1:
            return None
    return next(iter(summaries.values()), None)


def reconcile_fm24_result_event_summary(
    records: list[dict[str, int]], home_goals: int, away_goals: int,
) -> dict[str, Any] | None:
    """Recover FM24's 90-minute goal summary from its mixed event vector."""
    regular_records = [
        item for item in records
        if not item["flags"] & 0x400
        and item["minute_code"] & 0xFF <= 90
    ]
    observed = (
        sum(not bool(item["flags"] & 1) for item in regular_records),
        sum(bool(item["flags"] & 1) for item in regular_records),
    )
    expected = (int(home_goals), int(away_goals))
    if observed == expected:
        return _event_summary(regular_records)
    excess = (observed[0] - expected[0], observed[1] - expected[1])
    if excess[0] < 0 or excess[1] < 0:
        return None
    candidates = (
        [
            index for index, item in enumerate(regular_records)
            if not item["flags"] & 1 and (item["flags"] & 0x300) == 0x300
        ],
        [
            index for index, item in enumerate(regular_records)
            if item["flags"] & 1 and (item["flags"] & 0x300) == 0x300
        ],
    )
    if any(len(candidates[side]) < excess[side] for side in (0, 1)):
        return None
    choices = [
        list(combinations(candidates[side], excess[side]))
        for side in (0, 1)
    ]
    if len(choices[0]) * len(choices[1]) > 256:
        return None
    summaries: dict[tuple[int, int, str], dict[str, Any]] = {}
    for home_removed, away_removed in product(*choices):
        removed = set(home_removed) | set(away_removed)
        summary = _event_summary([
            item for index, item in enumerate(regular_records) if index not in removed
        ])
        signature = (
            int(summary["half_home_goals"]), int(summary["half_away_goals"]),
            str(summary["first_scoring_team"]),
        )
        summaries[signature] = summary
        if len(summaries) > 1:
            return None
    return next(iter(summaries.values()), None)


def parse_result_event_summary(
    reader: Reader,
    result_address: int,
    *,
    expected_home_goals: int | None = None,
    expected_away_goals: int | None = None,
) -> dict[str, Any] | None:
    """Read the compact goal-event details needed by result settlement."""
    raw = reader.bytes(result_address, 0x80)
    if not raw or len(raw) != 0x80:
        return None
    raw_home_goals = raw[reader.layout.result_home_goals_offset]
    raw_away_goals = raw[reader.layout.result_away_goals_offset]
    override_supplied = (
        expected_home_goals is not None and expected_away_goals is not None
    )
    home_goals = (
        int(expected_home_goals) if override_supplied else raw_home_goals
    )
    away_goals = (
        int(expected_away_goals) if override_supplied else raw_away_goals
    )
    if not (0 <= home_goals <= 30 and 0 <= away_goals <= 30):
        return None
    expected_goals = home_goals + away_goals
    if expected_goals == 0:
        return {"half_home_goals": 0, "half_away_goals": 0, "first_scoring_team": "none"}
    records = _read_result_event_records(reader, result_address, expected_goals)
    if records is None:
        return None
    observed = (
        sum(not bool(item["flags"] & 1) for item in records),
        sum(bool(item["flags"] & 1) for item in records),
    )
    if reader.layout.result_event_record_format == "fm24":
        summary = reconcile_fm24_result_event_summary(records, home_goals, away_goals)
    else:
        summary = reconcile_result_event_summary(records, home_goals, away_goals)
    if summary is None:
        return None
    # FM updates and reuses result objects while background simulations finish.
    # Reject a mixed snapshot when the fixture identity, final score or event
    # vector changed while the event records were being read.
    current = reader.bytes(result_address, 0x80)
    detail_offset = reader.layout.result_event_root_offset
    stable_ranges = (
        (0x08, 0x30),
        (0x4C, 0x50),
        (reader.layout.result_home_goals_offset, reader.layout.result_away_outcome_offset + 1),
        (detail_offset, detail_offset + 8),
    )
    if not current or len(current) != 0x80 or any(
        raw[start:end] != current[start:end] for start, end in stable_ranges
    ):
        return None
    if summary["half_home_goals"] > home_goals or summary["half_away_goals"] > away_goals:
        return None
    return summary


def parse_halftime_score(reader: Reader, result_address: int) -> tuple[int, int] | None:
    summary = parse_result_event_summary(reader, result_address)
    if not summary:
        return None
    return int(summary["half_home_goals"]), int(summary["half_away_goals"])


def parse_goal_events(reader: Reader, result_address: int) -> list[dict[str, Any]] | None:
    raw = reader.bytes(result_address, 0x80)
    if not raw or len(raw) != 0x80:
        return None
    home_pointer, away_pointer = struct.unpack_from("<QQ", raw, 0x08)
    expected_goals = (
        raw[reader.layout.result_home_goals_offset]
        + raw[reader.layout.result_away_goals_offset]
    )
    records = _read_result_event_records(reader, result_address, expected_goals)
    if records is None:
        return None
    observed = (
        sum(not bool(item["flags"] & 1) for item in records),
        sum(bool(item["flags"] & 1) for item in records),
    )
    if observed != (
        raw[reader.layout.result_home_goals_offset],
        raw[reader.layout.result_away_goals_offset],
    ):
        return None

    players: dict[int, dict[str, Any]] = {}
    for side, team_pointer in enumerate((home_pointer, away_pointer)):
        for player in reader.roster(team_pointer):
            player_address = int(player["address"], 16)
            match_id_offset = (
                reader.layout.player_and_non_player_person_offset + 0x08
                if player["object_type"] == "actual_player_and_non_player"
                else reader.layout.player_person_offset + 0x08
            )
            match_id = reader.u32(player_address + match_id_offset)
            if match_id:
                players[match_id] = {
                    "id": player["id"],
                    "name": player["name"],
                    "team_side": "home" if side == 0 else "away",
                }

    events = []
    for event in records:
        scorer_match_id = event["scorer"]
        assist_match_id = event["assist"]
        clock_ticks = event["clock"]
        flags = event["flags"]
        awarded_side = "away" if flags & 1 else "home"
        scorer = players.get(scorer_match_id)
        assist = None if assist_match_id == 0xFFFFFFFF else players.get(assist_match_id)
        events.append({
            "team_side": awarded_side,
            "scorer": scorer or {"match_player_id": scorer_match_id, "name": None},
            "assist": (
                None
                if assist_match_id == 0xFFFFFFFF
                else assist or {"match_player_id": assist_match_id, "name": None}
            ),
            "own_goal": bool(scorer and scorer["team_side"] != awarded_side),
            "penalty": bool(
                reader.layout.result_event_record_format == "fm26"
                and flags & 0x200
            ),
            "time": decode_match_minute(event["minute_code"]),
            "clock_ticks_raw": clock_ticks,
            "flags_raw": hex(flags),
        })
    return events


def deduplicate_results(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    unique = {}

    def evidence_priority(item: dict[str, Any]) -> tuple[int, int, int, int]:
        """Prefer settlement-capable archive evidence over a cached scoreline."""
        source = str(item.get("source") or item.get("result_source") or "").lower()
        detail_count = int(
            item.get("half_home_goals") is not None
            and item.get("half_away_goals") is not None
        ) + int(item.get("first_scoring_team") in {"home", "away", "none"})
        archive = int(source in {
            "fixture_result_archive", "fixture_result_pointer",
        })
        verified = int("verified" in source or "club schedule" in source)
        return detail_count, archive, verified, int(bool(item.get("result_address")))

    for item in results:
        key = (
            item["date"],
            item["competition"]["id"],
            item["home_team"]["id"],
            item["away_team"]["id"],
            item["home_goals"],
            item["away_goals"],
        )
        previous = unique.get(key)
        if previous is None or evidence_priority(item) > evidence_priority(previous):
            unique[key] = item
    return list(unique.values())


def rolling_competition_baseline(
    results: list[dict[str, Any]], competition_id: int, game_date: date, limit: int = 100
) -> dict[str, Any] | None:
    eligible = [
        item for item in results
        if item["competition"]["id"] == competition_id
        and date.fromisoformat(item["date"]) <= game_date
    ]
    eligible.sort(key=lambda item: item["date"], reverse=True)
    sample = eligible[:limit]
    if not sample:
        return None
    home_wins = sum(item["home_goals"] > item["away_goals"] for item in sample)
    draws = sum(item["home_goals"] == item["away_goals"] for item in sample)
    away_wins = len(sample) - home_wins - draws
    home_goals = sum(item["home_goals"] for item in sample)
    away_goals = sum(item["away_goals"] for item in sample)
    return {
        "type": "rolling_completed_matches",
        "sample_size": len(sample),
        "from_date": min(item["date"] for item in sample),
        "to_date": max(item["date"] for item in sample),
        "average_home_goals": round(home_goals / len(sample), 4),
        "average_away_goals": round(away_goals / len(sample), 4),
        "average_total_goals": round((home_goals + away_goals) / len(sample), 4),
        "home_win_rate": round(home_wins / len(sample), 4),
        "draw_rate": round(draws / len(sample), 4),
        "away_win_rate": round(away_wins / len(sample), 4),
        "note": "Rolling baseline; current-season boundary and official standings are not mapped yet.",
    }


def parse_fixture_from_raw(
    reader: Reader, address: int, raw: bytes | None, *,
    validate_references: bool = True,
) -> Fixture | None:
    """Parse one already-read fixture header.

    Callers that only need the native date/pointers may defer relationship
    expansion until after applying their date window.  Final consumers still
    validate teams and competition before publishing a fixture.
    """
    if not raw or len(raw) != FIXTURE_SIZE:
        return None
    home, away, result_or_state, comp, stadium = struct.unpack_from("<QQQQQ", raw, 0x08)
    match_date_code = struct.unpack_from("<I", raw, 0x4C)[0]
    match_date = decode_date(match_date_code)
    if not match_date or not all((home, away, comp)):
        return None
    if validate_references and (
        not reader.team(home)
        or not reader.team(away)
        or not reader.competition(comp)
    ):
        return None
    stage_offset = reader.layout.fixture_stage_index_offset
    group_offset = reader.layout.fixture_group_index_offset
    round_offset = reader.layout.fixture_round_number_offset
    return Fixture(
        address=address,
        home_team=home,
        away_team=away,
        result_or_state=result_or_state,
        competition_season=comp,
        stadium=stadium,
        match_date=match_date,
        kickoff_minutes=decode_kickoff_minutes(match_date_code),
        round_or_slot=raw[round_offset] if round_offset is not None else raw[0x3B],
        raw_tail=raw[0x30:0x58].hex(" "),
        stage_index=raw[stage_offset] if stage_offset is not None else None,
        group_index=raw[group_offset] if group_offset is not None else None,
    )


def parse_fixture(reader: Reader, address: int) -> Fixture | None:
    return parse_fixture_from_raw(
        reader, address, reader.bytes(address, FIXTURE_SIZE),
    )


def fixture_json(reader: Reader, item: Fixture) -> dict[str, Any]:
    home = reader.team(item.home_team)
    away = reader.team(item.away_team)
    competition = reader.competition(item.competition_season)
    return {
        "match_id": None,
        "date": item.match_date.isoformat(),
        "kickoff_time": (
            f"{item.kickoff_minutes // 60:02d}:{item.kickoff_minutes % 60:02d}"
            if item.kickoff_minutes is not None else None
        ),
        "status": None,
        "competition": competition,
        "home_team": home,
        "away_team": away,
        "stadium": reader.fm_string_at(item.stadium + STADIUM_NAME) if item.stadium else None,
        "neutral_venue": None,
        "postponed_or_rescheduled": None,
        "round_or_slot_raw": item.round_or_slot,
        "stage_index": item.stage_index,
        "group_index": item.group_index,
        "fixture_address": hex(item.address),
        "result_or_state_address": hex(item.result_or_state) if item.result_or_state else None,
        "raw_tail": item.raw_tail,
    }


def coverage(snapshot: dict[str, Any]) -> dict[str, list[str]]:
    return {
        "validated_in_this_snapshot": [
            "FM process and game_plugin module discovery",
            "game date",
            "fixture date",
            "competition ID and name",
            "home/away team ID and name",
            "stadium name when present",
            "team roster",
            "player ID, CA, positions, fitness/fatigue/sharpness/morale raw values",
            "completed match date, teams and final score",
            "completed match scorers, assists and goal minutes",
            "rolling competition scoring and result baseline",
        ],
        "partially_acquired": [
            "injury type, start date and estimated duration (duration semantics need another example)",
            "ban count (competition-specific suspension details are not decoded)",
            "team morale and preferred formation code",
            "round/stage raw value",
            "match status (scheduled and completed are distinguished; cancelled/abandoned are not)",
        ],
        "not_yet_acquired": [
            "stable match ID",
            "kickoff time",
            "neutral venue flag",
            "postponed/rescheduled flag",
            "competition-specific registration eligibility",
            "recent match lineups, formations and substitutions",
            "match-level xG, shots and shots on target",
            "team cohesion and tactical familiarity",
            "official current-season standings and season boundary",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only Football Manager initial data audit")
    parser.add_argument("--team-id", type=int, default=1190)
    parser.add_argument("--competition-id", type=int, default=102428)
    parser.add_argument("--hours", type=int, default=72)
    parser.add_argument(
        "--game-date", type=date.fromisoformat,
        help="explicit YYYY-MM-DD date for a supported build whose date address is not mapped",
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    started = time.perf_counter()
    pid, process_path, layout = select_process_layout()
    executable_hash = sha256(process_path)

    with open_process(pid) as process:
        module = layout.module(process)
        if not module:
            raise RuntimeError(f"{layout.module_name} is not loaded")
        reader = Reader(process, module.base_address, layout)
        game_date_code = (
            reader.u32(module.base_address + layout.game_date_rva)
            if layout.game_date_rva is not None else None
        )
        game_date = args.game_date or decode_date(game_date_code or 0)
        if not game_date:
            raise RuntimeError(
                f"{layout.display_name} current game date is unavailable; use --game-date YYYY-MM-DD"
            )

        addresses, bytes_scanned = scan_fixture_addresses(reader)
        parsed = [item for address in addresses if (item := parse_fixture(reader, address))]

        deduplicated: dict[tuple[Any, ...], Fixture] = {}
        for item in parsed:
            home = reader.team(item.home_team)
            away = reader.team(item.away_team)
            comp = reader.competition(item.competition_season)
            key = (
                item.match_date,
                comp["id"] if comp else None,
                home["id"] if home else None,
                away["id"] if away else None,
            )
            deduplicated.setdefault(key, item)

        end_date = game_date + timedelta(days=(args.hours + 23) // 24)
        future = [
            item for item in deduplicated.values()
            if game_date <= item.match_date <= end_date
        ]
        future.sort(key=lambda item: (item.match_date, item.address))
        selected = []
        selected_team_addresses: set[int] = set()
        for item in future:
            home = reader.team(item.home_team)
            away = reader.team(item.away_team)
            comp = reader.competition(item.competition_season)
            if not home or not away or not comp:
                continue
            if comp["id"] == args.competition_id or args.team_id in (home["id"], away["id"]):
                selected.append(fixture_json(reader, item))
                selected_team_addresses.update((item.home_team, item.away_team))

        teams = []
        for address in sorted(selected_team_addresses):
            team = dict(reader.team(address) or {})
            team["roster"] = reader.roster(address)
            teams.append(team)

        result_addresses, result_bytes_scanned = scan_result_addresses(reader)
        completed_results = deduplicate_results([
            item for address in result_addresses
            if (item := parse_completed_result(reader, address))
        ])
        recent_team_results = [
            item for item in completed_results
            if args.team_id in (item["home_team"]["id"], item["away_team"]["id"])
            and date.fromisoformat(item["date"]) <= game_date
        ]
        recent_team_results.sort(key=lambda item: item["date"], reverse=True)
        for item in recent_team_results[:20]:
            item["goal_events"] = parse_goal_events(reader, int(item["result_address"], 16))

        snapshot = {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "source": "read-only process memory",
            "process": {
                "pid": pid,
                "path": process_path,
                "executable_sha256": executable_hash,
                "module_name": module.name,
                "module_base": hex(module.base_address),
                "module_size": module.size,
                "layout": layout.key,
            },
            "game_date": game_date.isoformat(),
            "requested_window_hours": args.hours,
            "date_filter_note": "kickoff time is not mapped, so the initial filter includes whole calendar days",
            "scan": {
                "private_bytes_scanned": bytes_scanned,
                "fixture_vtable_hits": len(addresses),
                "validated_fixture_records": len(parsed),
                "deduplicated_fixture_records": len(deduplicated),
                "selected_fixtures": len(selected),
                "fixture_result_vtable_hits": len(result_addresses),
                "validated_completed_results": len(completed_results),
                "result_private_bytes_scanned": result_bytes_scanned,
            },
            "fixtures": selected,
            "recent_completed_team_results": recent_team_results[:20],
            "competition_baseline": rolling_competition_baseline(
                completed_results, args.competition_id, game_date
            ),
            "teams": teams,
        }
        snapshot["coverage"] = coverage(snapshot)
        snapshot["duration_seconds"] = round(time.perf_counter() - started, 3)

    output = args.output or Path("data") / "audits" / (
        f"{layout.key}_initial_audit_" + datetime.now().strftime("%Y%m%d_%H%M%S") + ".json"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "output": str(output.resolve()),
        "game_date": snapshot["game_date"],
        "fixtures": len(snapshot["fixtures"]),
        "teams": len(snapshot["teams"]),
        "players": sum(len(team["roster"]) for team in snapshot["teams"]),
        "scan": snapshot["scan"],
        "duration_seconds": snapshot["duration_seconds"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
