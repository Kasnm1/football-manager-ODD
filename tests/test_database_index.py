from __future__ import annotations

from datetime import date
from types import SimpleNamespace
import struct
import threading
from contextlib import nullcontext
from unittest.mock import patch

import pytest

from tools.database_index import (
    TABLE_COUNT_LIMITS, DatabaseIndex, PersonSearchRow, _compact_search_text,
    database_index_error_for_reader, database_index_for_reader,
    resolve_team_club,
)
from tools import world_clubs
from tools.game_layout import (
    FM24_240_LAYOUT, FM24_241_LAYOUT, FM24_EPIC_LAYOUT, FM24_LAYOUT,
    FM24_XGP_LAYOUT, FM26_LAYOUT, FM26_XGP_TEMPLATE,
)


class MemoryReader:
    def __init__(self):
        self.module_base = 0x100000
        self.module = SimpleNamespace(
            base_address=self.module_base, size=0x200000, path="",
        )
        self.process = SimpleNamespace(pid=26)
        pattern = (0x48, 0x8B, 0x05, None, None, None, None)
        manager_pattern = (0x48, 0x8B, 0x0D, None, None, None, None)
        self.layout = SimpleNamespace(
            key="fm26",
            database_root_pattern=pattern,
            database_root_probe_rva=0x100,
            database_root_rel32_offset=3,
            database_root_instruction_size=7,
            human_manager_root_pattern=manager_pattern,
            human_manager_root_probe_rva=0x200,
            human_manager_root_rel32_offset=3,
            human_manager_root_instruction_size=7,
            club_vtable_rva=0x1000,
            competition_vtable_rva=0x1100,
            nation_vtable_rva=0x1200,
            team_vtable_rva=0x1300,
            national_team_vtable_rva=0x1400,
            human_manager_vtable_rvas=(0x1500,),
            human_manager_vtable_on_person=False,
            manager_person_offset=0x80,
            actual_player_vtable_rvas=(0x1700,),
            player_and_non_player_vtable_rvas=(0x1800,),
            player_person_offset=0x30,
            player_and_non_player_person_offset=0x40,
            player_ca_offset=0x264,
            player_pa_offset=0x266,
            player_ca_bytes=2,
            player_world_reputation_offset=0x262,
            person_nationality_offset=0x68,
            person_full_name_offset=0x40,
            person_first_name_offset=0x48,
            person_last_name_offset=0x50,
            person_common_name_offset=0x58,
        )
        self.memory = bytearray(0x200000)
        self._write_pattern(0x100, pattern, self.module_base + 0x500)
        self._write_pattern(0x200, manager_pattern, self.module_base + 0x600)
        table_specs = {
            "club": (0x10, 0x1000, 1001),
            "competition": (0x18, 0x1100, 2001),
            "nation": (0x28, 0x1200, 3001),
            "person": (0x68, 0x1600, 4001),
            "team": (0x98, 0x1300, 5001),
        }
        self.objects = {}
        for index, (name, (slot, vtable_rva, uid)) in enumerate(table_specs.items()):
            table_object = self.module_base + 0x10000 + index * 0x100
            vector = self.module_base + 0x11000 + index * 0x100
            pointer_array = self.module_base + 0x12000 + index * 0x100
            object_address = self.module_base + 0x14000 + index * 0x100
            self._qword(0x500 + slot, table_object)
            self._qword(table_object - self.module_base + 0x80, vector)
            self._qword(vector - self.module_base, pointer_array)
            self._qword(vector - self.module_base + 8, pointer_array + 8)
            self._qword(vector - self.module_base + 16, pointer_array + 8)
            self._qword(pointer_array - self.module_base, object_address)
            self._qword(object_address - self.module_base, self.module_base + vtable_rva)
            self._dword(object_address - self.module_base + 0x0C, uid)
            self.objects[name] = (pointer_array, object_address, uid)
        person = self.objects["person"][1]
        player = person - 0x30
        self._qword(player - self.module_base, self.module_base + 0x1700)
        struct.pack_into(
            "<I", self.memory, player - self.module_base + 0x234, 12_500_000,
        )
        struct.pack_into(
            "<HH", self.memory, player - self.module_base + 0x264, 125, 160,
        )
        struct.pack_into(
            "<H", self.memory, player - self.module_base + 0x262, 7600,
        )
        self._qword(
            person - self.module_base + 0x68, self.objects["nation"][1],
        )
        first_entry = self.module_base + 0x1A000
        last_entry = self.module_base + 0x1A010
        common_entry = self.module_base + 0x1A020
        first_string = self.module_base + 0x1B000
        last_string = self.module_base + 0x1B100
        common_string = self.module_base + 0x1B200
        full_string = self.module_base + 0x1B300
        self._qword(person - self.module_base + 0x48, first_entry)
        self._qword(person - self.module_base + 0x50, last_entry)
        self._qword(person - self.module_base + 0x58, common_entry)
        self._qword(person - self.module_base + 0x40, full_string)
        self._qword(first_entry - self.module_base, first_string)
        self._qword(last_entry - self.module_base, last_string)
        self._qword(common_entry - self.module_base, common_string)
        self._string(first_string, "Walter")
        self._string(last_string, "Benitez")
        self._string(common_string, "沃尔特·贝尼特斯")
        self._string(full_string, "沃尔特·丹尼尔·贝尼特斯")
        owner = self.module_base + 0x18000
        manager_array = self.module_base + 0x18100
        manager = self.module_base + 0x18200
        self._qword(0x600, owner)
        self._qword(owner - self.module_base + 0x18, manager_array)
        self._qword(owner - self.module_base + 0x20, manager_array + 8)
        self._qword(manager_array - self.module_base, manager)
        self._qword(manager - self.module_base, self.module_base + 0x1500)
        self._dword(manager - self.module_base + 0x80 + 0x0C, 77)
        self.manager = manager

    def _write_pattern(self, rva, pattern, target):
        raw = bytes(value or 0 for value in pattern)
        self.memory[rva:rva + len(raw)] = raw
        displacement = target - (self.module_base + rva + 7)
        struct.pack_into("<i", self.memory, rva + 3, displacement)

    def _qword(self, offset, value):
        struct.pack_into("<Q", self.memory, offset, value)

    def _dword(self, offset, value):
        struct.pack_into("<I", self.memory, offset, value)

    def _string(self, address, value):
        raw = value.encode("utf-8")
        offset = int(address) - self.module_base
        struct.pack_into("<I", self.memory, offset, len(raw))
        self.memory[offset + 4:offset + 4 + len(raw)] = raw

    def bytes(self, address, size):
        offset = int(address) - self.module_base
        if offset < 0 or offset + int(size) > len(self.memory):
            return None
        return bytes(self.memory[offset:offset + int(size)])

    def ptr(self, address):
        raw = self.bytes(address, 8)
        return struct.unpack("<Q", raw)[0] if raw else None

    def u32(self, address):
        raw = self.bytes(address, 4)
        return struct.unpack("<I", raw)[0] if raw else None

    def fm_string_at(self, address):
        pointer = self.ptr(address)
        if not pointer:
            return None
        length = self.u32(pointer)
        raw = self.bytes(pointer + 4, int(length or 0)) if length else None
        return raw.decode("utf-8") if raw else None

    def fm_nested_string_at(self, address):
        entry = self.ptr(address)
        return self.fm_string_at(entry) if entry else None

    def _fixed_size_snapshots(self, addresses, size):
        return {
            int(address): raw for address in addresses
            if (raw := self.bytes(int(address), int(size))) is not None
        }


def test_person_table_accepts_verified_large_native_directory() -> None:
    reader = MemoryReader()
    index = DatabaseIndex(reader)
    table_object = int(reader.ptr(index.root_slot + 0x68) or 0)
    vector = int(reader.ptr(table_object + 0x80) or 0)
    begin = 0x50000000
    entry_count = 1_034_663
    capacity_count = 1_036_163
    reader._qword(vector - reader.module_base, begin)
    reader._qword(vector - reader.module_base + 8, begin + entry_count * 8)
    reader._qword(vector - reader.module_base + 16, begin + capacity_count * 8)

    assert TABLE_COUNT_LIMITS["person"] == 2_000_000
    assert index._table_header("person") == (
        table_object,
        vector,
        begin,
        begin + entry_count * 8,
        begin + capacity_count * 8,
    )


def test_database_index_resolves_uid_and_human_manager_without_heap_scan():
    reader = MemoryReader()
    index = DatabaseIndex(reader)

    _pointer_array, team_address, team_uid = reader.objects["team"]
    assert not index.table_ready("team")
    assert index.addresses_for_uid("team", team_uid) == (team_address,)
    assert index.table_ready("team")
    assert index.human_manager_addresses(77) == (reader.manager,)


def test_database_index_detects_in_place_pointer_table_change():
    reader = MemoryReader()
    index = DatabaseIndex(reader)
    pointer_array, old_address, old_uid = reader.objects["team"]
    new_address = reader.module_base + 0x19000
    new_uid = 5002
    reader._qword(new_address - reader.module_base, reader.module_base + 0x1300)
    reader._dword(new_address - reader.module_base + 0x0C, new_uid)
    reader._qword(pointer_array - reader.module_base, new_address)

    index._refresh_table("team", force=True)

    assert not index.table_ready("team")
    assert index.addresses_for_uid("team", old_uid) == ()
    assert index.addresses_for_uid("team", new_uid) == (new_address,)
    assert old_address != new_address


def test_background_table_build_does_not_block_human_manager_lookup():
    reader = MemoryReader()
    index = DatabaseIndex(reader)
    started = threading.Event()
    release = threading.Event()
    original = reader._fixed_size_snapshots

    def snapshots(addresses, size):
        if set(addresses) == set(index.addresses("team")):
            started.set()
            release.wait(2)
        return original(addresses, size)

    reader._fixed_size_snapshots = snapshots
    build = threading.Thread(target=index.warm, daemon=True)
    build.start()
    assert started.wait(1)
    result = []
    lookup = threading.Thread(
        target=lambda: result.extend(index.human_manager_addresses(77)),
        daemon=True,
    )
    try:
        lookup.start()
        lookup.join(0.2)
        assert not lookup.is_alive()
        assert result == [reader.manager]
    finally:
        release.set()
        build.join(2)


def test_database_index_provider_is_optional_and_fail_closed():
    assert database_index_for_reader(SimpleNamespace()) is None
    reader = SimpleNamespace(database_index_provider=lambda: "index")
    assert database_index_for_reader(reader) == "index"
    forced = SimpleNamespace(database_index_provider=lambda **kwargs: kwargs)
    assert database_index_for_reader(forced, force_retry=True) == {
        "force_retry": True,
    }
    failed = SimpleNamespace(database_index_error_provider=lambda: "root failed")
    assert database_index_error_for_reader(failed) == "root failed"


def test_resolve_team_club_accepts_distinct_reserve_club_uid():
    team_address = 0x500000
    club_address = 0x600000
    module_base = 0x100000
    layout = SimpleNamespace(team_vtable_rva=0x1000, club_vtable_rva=0x3000)
    reader = SimpleNamespace(
        module_base=module_base,
        layout=layout,
        u32=lambda address: {
            team_address + 0x0C: 200,
            club_address + 0x0C: 100,
        }.get(address, 0),
        ptr=lambda address: {
            team_address: module_base + layout.team_vtable_rva,
            team_address + 0x30: club_address,
            club_address: module_base + layout.club_vtable_rva,
        }.get(address, 0),
    )

    resolved = resolve_team_club(reader, hex(team_address), 200)

    assert resolved.team_address == team_address
    assert resolved.club_address == club_address
    assert resolved.team_id == 200


def test_compact_search_text_preserves_unicode_alnum_semantics():
    assert _compact_search_text(" A_B-9 / 沃尔特·贝尼特斯 ") == "ab9沃尔特贝尼特斯"


def test_unavailable_club_table_does_not_block_person_search():
    reader = MemoryReader()
    reader._qword(0x500 + 0x10, 0)

    result = DatabaseIndex(reader).search_players("Walter")

    assert result["players"][0]["id"] == 4001


def test_person_search_builds_once_and_matches_native_name_variants():
    reader = MemoryReader()
    index = DatabaseIndex(reader)
    original = reader._fixed_size_snapshots
    snapshot_calls = []

    def snapshots(addresses, size):
        addresses = tuple(addresses)
        snapshot_calls.append((addresses, size))
        return original(addresses, size)

    reader._fixed_size_snapshots = snapshots
    chinese = index.search_players("沃尔特", page=1, page_size=10)
    first_build_calls = len(snapshot_calls)
    english = index.search_players("Walter", page=1, page_size=10)

    assert chinese["index"]["source"] == "session_person_cache"
    assert chinese["index"]["player_count"] == 1
    assert chinese["players"][0]["id"] == 4001
    assert chinese["players"][0]["name"] == "沃尔特·贝尼特斯"
    assert english["players"][0]["name"] == "沃尔特·贝尼特斯"
    assert len(snapshot_calls) == first_build_calls
    person = reader.objects["person"][1]
    player = person - reader.layout.player_person_offset
    person_header_size = max(
        int(reader.layout.person_full_name_offset) + 8,
        int(reader.layout.person_first_name_offset) + 8,
        int(reader.layout.person_last_name_offset) + 8,
        int(reader.layout.person_common_name_offset) + 8,
        int(reader.layout.person_nationality_offset) + 8,
    )
    assert any(
        player in addresses
        and size >= int(reader.layout.player_person_offset) + person_header_size
        for addresses, size in snapshot_calls
    )


def test_player_uids_uses_verified_player_groups_without_name_index():
    index = DatabaseIndex(MemoryReader())

    assert index.player_uids() == (4001,)
    assert index._person_search_rows is None


def test_exact_player_uid_lookup_does_not_build_full_search_index():
    index = DatabaseIndex(MemoryReader())
    index._build_person_search_index = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("exact UID lookup must not build the name search index")
    )

    row = index.player_for_uid(4001)

    assert row == {
        "id": 4001,
        "name": "沃尔特·贝尼特斯",
        "first_name": "Walter",
        "last_name": "Benitez",
        "common_name": "沃尔特·贝尼特斯",
        "full_name": "沃尔特·丹尼尔·贝尼特斯",
        "address": hex(index.reader.objects["person"][1] - 0x30),
        "person_address": hex(index.reader.objects["person"][1]),
        "object_type": "actual_player",
        "ca": None,
        "pa": None,
        "asking_price": None,
        "nationality_id": None,
        "world_reputation": None,
    }
    assert index._person_search_rows is None


def test_player_target_uid_lookup_returns_addresses_without_name_reads():
    index = DatabaseIndex(MemoryReader())
    index._build_person_search_index = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("address-only lookup must not build the name search index")
    )

    person = index.reader.objects["person"][1]
    assert index.player_target_for_uid(4001) == (person, person - 0x30, "actual_player")
    assert index._person_search_rows is None


def test_public_language_catalog_is_session_cached_and_returns_copies():
    index = DatabaseIndex(MemoryReader())
    table = SimpleNamespace(
        table_object=0x10, vector=0x20, fingerprint="language-v1",
        addresses=(0x3000, 0x4000),
    )
    index._refresh_table = lambda name, force=False: table

    with patch(
        "tools.player_languages._valid_language_object",
        side_effect=(
            {"id": 1, "name": "English"},
            {"id": 2, "name": "Mandarin"},
        ),
    ) as validate:
        first = index.public_language_catalog()
        first[0]["name"] = "mutated"
        second = index.public_language_catalog()

    assert validate.call_count == 2
    assert [row["id"] for row in second] == [1, 2]
    assert second[0]["name"] != "mutated"


def test_person_search_paginates_and_matches_uid():
    result = DatabaseIndex(MemoryReader()).search_players(
        "4001", page=99, page_size=500,
    )

    assert result["pagination"] == {
        "page": 1, "page_size": 50, "page_count": 1,
        "total": 1, "from": 1, "to": 1,
    }
    assert result["players"][0]["object_type"] == "actual_player"
    assert result["players"][0]["nationality_id"] == 3001
    assert result["players"][0]["world_reputation"] == 7600


def test_person_search_normalizes_unset_asking_price_sentinels():
    for sentinel in (300_000_000, 0xFFFFFFFF):
        reader = MemoryReader()
        person = reader.objects["person"][1]
        player = person - reader.layout.player_person_offset
        struct.pack_into(
            "<I", reader.memory, player - reader.module_base + 0x234, sentinel,
        )

        result = DatabaseIndex(reader).search_players("4001")

        assert result["players"][0]["asking_price"] == 0


def test_person_search_filters_fm26_female_flag_before_pagination():
    reader = MemoryReader()
    reader.layout.person_flags_offset = 0x18
    reader.layout.person_flags_bytes = 8
    person = reader.objects["person"][1]
    struct.pack_into("<Q", reader.memory, person - reader.module_base + 0x18, 0x1000)
    index = DatabaseIndex(reader)

    women = index.search_players(gender="women")
    men = index.search_players(gender="men")
    all_players = index.search_players(gender="all")

    assert women["pagination"]["total"] == 1
    assert women["players"][0]["id"] == 4001
    assert men["pagination"]["total"] == 0
    assert all_players["pagination"]["total"] == 1


def test_person_search_defaults_to_pa_desc_and_sorts_ca_or_asking_price():
    index = DatabaseIndex(MemoryReader())

    def row(uid, name, ca, pa, asking_price):
        return PersonSearchRow(
            uid=uid, address=0x2000 + uid, player_address=0x1000 + uid,
            object_type="actual_player", display_name=name,
            first_name=name, last_name="", common_name="", full_name=name,
            searchable_names=name.casefold(), compact_searchable_names=name.casefold(),
            ca=ca, pa=pa, asking_price=asking_price,
        )

    rows = (
        row(1, "Alpha", 120, 190, 50_000_000),
        row(2, "Beta", 180, 195, 10_000_000),
        row(3, "Gamma", None, None, None),
    )
    index._build_person_search_index = lambda **_kwargs: rows

    default_desc = index.search_players(page_size=3)
    ca_desc = index.search_players(sort_by="ca", sort_order="desc", page_size=2)
    pa_asc = index.search_players(sort_by="pa", sort_order="asc", page_size=3)
    price_desc = index.search_players(
        sort_by="asking_price", sort_order="desc", page_size=3,
    )

    assert [item["id"] for item in default_desc["players"]] == [2, 1, 3]
    assert default_desc["filters"] == {
        "search": "", "sort_by": "pa", "sort_order": "desc", "gender": "all",
    }
    assert [item["id"] for item in ca_desc["players"]] == [2, 1]
    assert ca_desc["pagination"]["total"] == 3
    assert ca_desc["filters"] == {
        "search": "", "sort_by": "ca", "sort_order": "desc", "gender": "all",
    }
    assert [item["id"] for item in pa_asc["players"]] == [1, 2, 3]
    assert [item["id"] for item in price_desc["players"]] == [1, 2, 3]


def test_person_search_filters_primary_nationality_and_sorts_world_reputation():
    index = DatabaseIndex(MemoryReader())

    def row(uid, name, nation_id, reputation):
        return PersonSearchRow(
            uid=uid, address=0x2000 + uid, player_address=0x1000 + uid,
            object_type="actual_player", display_name=name,
            first_name=name, last_name="", common_name="", full_name=name,
            searchable_names=name.casefold(), compact_searchable_names=name.casefold(),
            ca=120, pa=160, asking_price=10_000_000,
            nationality_id=nation_id, world_reputation=reputation,
        )

    rows = (
        row(1, "Alpha", 765, 7000),
        row(2, "Beta", 765, 9000),
        row(3, "Gamma", 769, 9500),
    )
    index._build_person_search_index = lambda **_kwargs: rows

    result = index.search_players(
        nationality_id=765, sort_by="world_reputation", page_size=30,
    )

    assert [item["id"] for item in result["players"]] == [2, 1]
    assert result["pagination"]["total"] == 2
    assert result["filters"]["nationality_id"] == 765
    assert result["filters"]["sort_by"] == "world_reputation"


def test_person_search_index_reads_birth_date_for_age_filtering():
    reader = MemoryReader()
    reader.layout.person_date_of_birth_offset = 0x70
    reader.layout.person_date_of_birth_day_year = True
    person = reader.objects["person"][1]
    struct.pack_into(
        "<HH", reader.memory, person - reader.module_base + 0x70, 251, 2005,
    )

    result = DatabaseIndex(reader).search_players(
        min_age=20, max_age=20, game_date=date(2026, 9, 7),
    )

    assert result["pagination"]["total"] == 1
    assert result["players"][0]["date_of_birth"] == "2005-09-08"
    assert result["players"][0]["age"] == 20


def test_person_search_filters_age_ca_and_pa_ranges_before_pagination():
    index = DatabaseIndex(MemoryReader())

    def row(uid, name, birth, ca, pa, nation_id):
        return PersonSearchRow(
            uid=uid, address=0x2000 + uid, player_address=0x1000 + uid,
            object_type="actual_player", display_name=name,
            first_name=name, last_name="", common_name="", full_name=name,
            searchable_names=name.casefold(), compact_searchable_names=name.casefold(),
            ca=ca, pa=pa, nationality_id=nation_id, date_of_birth=birth,
        )

    rows = (
        row(1, "Alpha", date(2005, 9, 8), 150, 180, 765),
        row(2, "Beta", date(2001, 1, 1), 160, 185, 765),
        row(3, "Gamma", date(2006, 12, 1), 145, 170, 769),
    )
    index._build_person_search_index = lambda **_kwargs: rows

    result = index.search_players(
        page_size=1, nationality_id=765,
        min_age=20, max_age=24, min_ca=145, max_ca=155,
        min_pa=175, max_pa=185, game_date=date(2026, 9, 7),
    )

    assert result["pagination"]["total"] == 1
    assert result["players"][0]["id"] == 1
    assert result["players"][0]["age"] == 20
    assert result["filters"] == {
        "search": "", "sort_by": "pa", "sort_order": "desc", "gender": "all",
        "nationality_id": 765, "min_age": 20, "max_age": 24,
        "min_ca": 145, "max_ca": 155, "min_pa": 175, "max_pa": 185,
    }
    assert result["filter_options"]["nationality_ids"] == [765, 769]


def test_person_search_reuses_bounded_sort_views_and_uid_lookup():
    index = DatabaseIndex(MemoryReader())

    first = index.search_players(sort_by="pa", sort_order="desc")
    rows = index._person_search_rows
    key = (id(rows), "pa", "desc")
    prepared = index._person_search_views[key]
    second = index.search_players(page=2, sort_by="pa", sort_order="desc")

    assert first["pagination"]["total"] == second["pagination"]["total"] == 1
    assert index._person_search_views[key] is prepared
    assert len(index._person_search_views) == 1
    assert index.player_for_uid(4001)["id"] == 4001
    assert index._person_search_by_uid[4001] is rows[0]


def test_person_search_query_views_are_reused_and_lru_bounded():
    index = DatabaseIndex(MemoryReader())
    first = index.search_players("Walter")
    key = (
        id(index._person_search_rows), "walter", "pa", "desc", "all",
        0, 0, 0, 0, 0, 0, 0, "",
    )
    prepared = index._person_search_query_views[key]

    second = index.search_players("Walter", page=2)
    assert first["pagination"]["total"] == second["pagination"]["total"] == 1
    assert index._person_search_query_views[key] is prepared

    for query in (f"missing-{number}" for number in range(20)):
        index.search_players(query)
    assert len(index._person_search_query_views) == 16
    assert key not in index._person_search_query_views


def test_person_search_does_not_cache_queries_above_match_limit():
    index = DatabaseIndex(MemoryReader())

    with patch(
        "tools.database_index.PERSON_SEARCH_QUERY_CACHE_MAX_MATCHES", 0,
    ):
        result = index.search_players("Walter")

    assert result["pagination"]["total"] == 1
    assert not index._person_search_query_views


def test_background_warm_prioritizes_world_player_search_before_other_tables():
    index = DatabaseIndex(MemoryReader())
    events = []
    index._build_person_search_index = lambda _cancel=None: events.append("person_search")
    index._refresh_table = lambda name: events.append(f"refresh:{name}") or name
    index._build_objects = lambda table: events.append(f"objects:{table}") or table

    index.warm()

    assert events == [
        "person_search",
        "refresh:team", "objects:team",
        "refresh:club", "objects:club",
        "refresh:nation", "objects:nation",
    ]


def test_person_gender_filter_reuses_flags_captured_by_search_index():
    reader = MemoryReader()
    reader.layout.person_flags_offset = 0x18
    reader.layout.person_flags_bytes = 8
    person = reader.objects["person"][1]
    struct.pack_into("<Q", reader.memory, person - reader.module_base + 0x18, 0x1000)
    index = DatabaseIndex(reader)
    index.search_players(gender="all")

    reader._fixed_size_snapshots = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("gender filter must not reread Person flags")
    )
    result = index.search_players(gender="women")

    assert result["players"][0]["id"] == 4001


def test_forced_person_index_refresh_invalidates_old_sort_views():
    index = DatabaseIndex(MemoryReader())
    index.search_players(sort_by="ca", sort_order="asc")
    previous_rows = index._person_search_rows

    index.search_players(force_refresh=True, sort_by="ca", sort_order="asc")

    assert index._person_search_rows is not previous_rows
    assert set(index._person_search_views) == {
        (id(index._person_search_rows), "ca", "asc"),
    }


def test_world_club_resolution_uses_database_index_without_scan_hints():
    directory = SimpleNamespace(addresses_for_uid=lambda name, uid: (0x5000,))
    reader = SimpleNamespace(
        database_index_provider=lambda: directory,
        layout=SimpleNamespace(),
        module=SimpleNamespace(),
        process=SimpleNamespace(),
        team=lambda address: {
            "id": 42, "team_type": "club", "club_address": "0x6000",
        } if address == 0x5000 else None,
    )
    with patch.object(
        world_clubs, "borrow_game_reader", return_value=nullcontext(reader),
    ):
        resolved = world_clubs.resolve_native_team_addresses({42}, set())

    assert resolved == {42: "0x5000"}


def test_exact_steam_layouts_use_fixed_database_probes_and_variants_scan_aob():
    assert FM24_LAYOUT.database_root_probe_rva == 0x3BDC363
    assert FM24_LAYOUT.human_manager_root_probe_rva == 0x3A278B1
    assert FM24_EPIC_LAYOUT.database_root_probe_rva == 0x3BDC363
    assert FM24_EPIC_LAYOUT.human_manager_root_probe_rva == 0x3A278B1
    assert FM26_LAYOUT.database_root_probe_rva == 0x2C033AB
    assert FM26_LAYOUT.human_manager_root_probe_rva == 0xD0BFE3
    for layout in (
        FM24_240_LAYOUT, FM24_241_LAYOUT, FM24_XGP_LAYOUT,
        FM26_XGP_TEMPLATE,
    ):
        assert layout.database_root_probe_rva is None
        assert layout.human_manager_root_probe_rva is None
        assert layout.database_root_pattern
        assert layout.human_manager_root_pattern


def test_world_club_scan_uses_targeted_database_snapshots_without_heap_scan():
    module_base = 0x100000
    team_address = 0x5000
    club_address = 0x6000
    name_address = 0x7000
    reputation_offset = 0x86
    layout = SimpleNamespace(
        key="fm26", game_version="26.3.2", executable_sha256="hash",
        display_name="FM26", team_vtable_rva=0x1000,
        club_vtable_rva=0x2000, club_nation_offset=0xD0,
        team_reputation_offset=reputation_offset,
    )
    team_raw = bytearray(0xB0)
    struct.pack_into("<Q", team_raw, 0, module_base + layout.team_vtable_rva)
    struct.pack_into("<I", team_raw, 0x0C, 42)
    struct.pack_into("<Q", team_raw, 0x30, club_address)
    struct.pack_into("<H", team_raw, reputation_offset, 9000)
    club_raw = bytearray(0xE0)
    struct.pack_into("<Q", club_raw, 0, module_base + layout.club_vtable_rva)
    struct.pack_into("<I", club_raw, 0x0C, 42)
    struct.pack_into("<Q", club_raw, 0xC0, name_address)
    name_raw = bytearray(260)
    encoded = b"Manchester United"
    struct.pack_into("<I", name_raw, 0, len(encoded))
    name_raw[4:4 + len(encoded)] = encoded
    directory = SimpleNamespace(addresses=lambda name: (team_address,))

    class FakeReader:
        @staticmethod
        def _fixed_size_snapshots(addresses, size):
            values = set(addresses)
            if size == 0xB0 and values == {team_address}:
                return {team_address: bytes(team_raw)}
            if size == 0xE0 and values == {club_address}:
                return {club_address: bytes(club_raw)}
            if size == 260 and values == {name_address}:
                return {name_address: bytes(name_raw)}
            raise AssertionError((values, size))

        @staticmethod
        def competition(_address):
            return None

    reader = FakeReader()
    reader.process = SimpleNamespace(pid=26)
    reader.module = SimpleNamespace(base_address=module_base)
    reader.module_base = module_base
    reader.returned_bytes = 0
    reader.database_index_provider = lambda: directory
    reader.layout = layout
    with (
        patch.object(world_clubs, "select_process_layout", return_value=(26, "fm.exe", layout)),
        patch.object(world_clubs, "borrow_game_reader", return_value=nullcontext(reader)),
        patch.object(
            world_clubs, "iter_readable_regions",
            side_effect=AssertionError("database index must not enumerate the heap"),
        ),
    ):
        result = world_clubs.scan_native_world_clubs("save-a")

    assert result["scan_mode"] == "database_index"
    assert result["candidate_addresses"] == 1
    assert [(row["id"], row["name"]) for row in result["clubs"]] == [
        (42, "Manchester United"),
    ]


def test_person_uid_baseline_includes_unclassified_existing_players(monkeypatch):
    reader = MemoryReader()
    index = DatabaseIndex(reader)
    monkeypatch.setattr(index, "_player_person_groups", lambda _t: ())
    assert index.player_uids() == ()
    assert index.person_uids_snapshot() == (4001,)


@pytest.mark.parametrize("failure", ["unreadable", "identity", "unaligned"])
def test_person_uid_baseline_rejects_partial_or_invalid_objects(monkeypatch, failure):
    reader = MemoryReader()
    index = DatabaseIndex(reader)
    pointer_array, person, _uid = reader.objects["person"]
    if failure == "unreadable":
        monkeypatch.setattr(reader, "_fixed_size_snapshots", lambda *_a: {})
    elif failure == "identity":
        reader._dword(person - reader.module_base + 0x0C, 0)
    else:
        reader._qword(pointer_array - reader.module_base, person + 1)
    with pytest.raises(RuntimeError, match="baseline"):
        index.person_uids_snapshot()


def test_person_uid_baseline_rejects_table_mutation_during_scan(monkeypatch):
    reader = MemoryReader()
    index = DatabaseIndex(reader)
    original = reader._fixed_size_snapshots
    pointer_array, person, _uid = reader.objects["person"]
    def mutate(addresses, size):
        result = original(addresses, size)
        reader._qword(pointer_array - reader.module_base, person + 0x100)
        return result
    monkeypatch.setattr(reader, "_fixed_size_snapshots", mutate)
    with pytest.raises(RuntimeError, match="changed during"):
        index.person_uids_snapshot()


def test_person_uid_baseline_includes_nonplayer_and_ignores_vacant_slots():
    reader = MemoryReader()
    index = DatabaseIndex(reader)
    pointer_array, person, _uid = reader.objects["person"]
    other = person + 0x800
    reader._qword(other - reader.module_base, reader.module_base + 0x1900)
    reader._dword(other - reader.module_base + 0x0C, 4002)
    reader._qword(pointer_array - reader.module_base + 8, 0)
    reader._qword(pointer_array - reader.module_base + 16, other)
    vector = reader.ptr(reader.ptr(index.root_slot + 0x68) + 0x80)
    reader._qword(vector - reader.module_base + 8, pointer_array + 24)
    reader._qword(vector - reader.module_base + 16, pointer_array + 24)
    assert index.person_uids_snapshot() == (4001, 4002)
