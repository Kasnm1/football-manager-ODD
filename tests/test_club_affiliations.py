from __future__ import annotations

import struct
import threading
from types import SimpleNamespace

import pytest

import fm_odds_web
from tools import club_affiliations
from tools.game_layout import FM24_EPIC_LAYOUT, FM24_LAYOUT


class FakeContext:
    def __enter__(self):
        return object()

    def __exit__(self, *_args):
        return None


class FakeLayout:
    module_name = "fm.exe"
    display_name = "test"
    club_affiliations_offset = 0x118
    club_affiliation_record_size = 0x38
    club_vtable_rva = 0x500
    game_date_rva = 0x700

    def module(self, _process):
        return SimpleNamespace(base_address=0x100000)


class FakeReader:
    module_base = 0x100000
    layout = FakeLayout()

    def __init__(self):
        self.teams = {11: 0x200000, 22: 0x210000}
        self.clubs = {11: 0x300000, 22: 0x310000}
        self.memory = {
            self.clubs[11] + 0x118: bytearray(24),
            self.clubs[22] + 0x118: bytearray(24),
        }

    def team(self, address):
        for team_id, team_address in self.teams.items():
            if address == team_address:
                return {"id": team_id, "team_type": "club"}
        return None

    def u32(self, address):
        if address == self.module_base + self.layout.game_date_rva:
            return (2025 << 16) | 295
        for team_id, team_address in self.teams.items():
            if address == team_address + club_affiliations.ENTITY_UID:
                return team_id
        for team_id, club_address in self.clubs.items():
            if address == club_address + club_affiliations.ENTITY_UID:
                return team_id + 1000
        return 0

    def ptr(self, address):
        for team_id, team_address in self.teams.items():
            if address == team_address + 0x30:
                return self.clubs[team_id]
        if address in self.clubs.values():
            return self.module_base + self.layout.club_vtable_rva
        return 0

    def bytes(self, address, size):
        if size == 0:
            return b""
        for base, raw in self.memory.items():
            offset = address - base
            if 0 <= offset and offset + size <= len(raw):
                return bytes(raw[offset:offset + size])
        return None

    def write(self, _process, address, data):
        for base, raw in self.memory.items():
            offset = address - base
            if 0 <= offset and offset + len(data) <= len(raw):
                raw[offset:offset + len(data)] = data
                return
        self.memory[address] = bytearray(data)


def test_supported_relation_types_are_the_requested_five():
    assert club_affiliations.affiliation_type_options() == [
        {"value": 1, "label": "普通关联俱乐部"},
        {"value": 2, "label": "下级俱乐部"},
        {"value": 3, "label": "合作俱乐部"},
        {"value": 16, "label": "关系良好"},
        {"value": 17, "label": "可能的友谊赛"},
    ]
    benefits = club_affiliations.affiliation_benefit_options()
    assert {row["key"] for row in benefits} >= {
        "uses_same_setup", "players_move_freely", "same_board",
        "financial_help", "scouting_knowledge_shared",
        "send_youth_for_experience", "reserve_team_player_relation",
        "send_first_team_to_improve_affiliate",
    }
    conditional_keys = {
        "send_youth_for_experience", "reserve_team_player_relation",
        "send_first_team_to_improve_affiliate",
    }
    assert {
        row["key"]: row["requires"] for row in benefits
        if row["key"] in conditional_keys
    } == {key: 0x80 for key in conditional_keys}


def test_club_target_uses_uid_resolver_result_over_stale_address_hint(monkeypatch):
    reader = FakeReader()
    stale_team = reader.teams[11]
    fresh_team = 0x220000
    fresh_club = 0x320000
    reader.teams[11] = fresh_team
    reader.clubs[11] = fresh_club
    reader.memory[fresh_club + 0x118] = bytearray(24)
    calls = []

    def resolve(_reader, team_address, team_id):
        calls.append((team_address, team_id))
        return SimpleNamespace(
            team_address=fresh_team, club_address=fresh_club, team_id=team_id,
        )

    monkeypatch.setattr(club_affiliations, "resolve_team_club", resolve)

    target = club_affiliations._club_target(reader, hex(stale_team), 11)

    assert calls == [(stale_team, 11)]
    assert target.team_address == fresh_team
    assert target.club_address == fresh_club
    assert target.team_id == 11
    assert target.club_id == 1011


def test_relation_transaction_creates_updates_and_removes_both_sides(monkeypatch):
    reader = FakeReader()
    allocations = iter((0x400000, 0x410000, 0x420000, 0x430000))
    monkeypatch.setattr(
        club_affiliations, "_open_reader",
        lambda **_kwargs: (1, reader.layout, FakeContext()),
    )
    monkeypatch.setattr(club_affiliations, "Reader", lambda *_args: reader)
    monkeypatch.setattr(club_affiliations, "write_process_memory", reader.write)
    monkeypatch.setattr(
        club_affiliations, "_allocate_native_block",
        lambda _process, _size: (next(allocations), 0x999999),
    )
    monkeypatch.setattr(club_affiliations, "_free_native_block", lambda *_args: None)
    parent = {"id": 11, "name": "Parent", "address": hex(reader.teams[11])}
    feeder = {"id": 22, "name": "Feeder", "address": hex(reader.teams[22])}

    created = club_affiliations.create_club_affiliation(
        parent, feeder, 3,
        start_date="2025-01-01", end_date="2035-01-01",
        updates={
            "annual_payment": 123_456,
            "benefits": 0x80 | 0x00080000,
            "friendly_probability": 50,
            "maximum_players_loaned": 6,
            "parent_wage_percentage": 25,
        },
    )
    record = int(created["record_address"], 0)
    assert created["type_name"] == "合作俱乐部"
    assert created["start_date"] == "2025-01-01"
    assert created["end_date"] == "2035-01-01"
    for team_id in (11, 22):
        vector = club_affiliations._read_vector(
            reader,
            club_affiliations._club_target(reader, reader.teams[team_id], team_id),
        )
        assert vector.pointers == (record,)
    raw = reader.bytes(record, 0x38)
    assert struct.unpack_from("<Q", raw, 0x00)[0] == reader.clubs[11]
    assert struct.unpack_from("<Q", raw, 0x08)[0] == reader.clubs[22]
    assert club_affiliations._date_text(struct.unpack_from("<I", raw, 0x20)[0]) == "2025-01-01"
    assert club_affiliations._date_text(struct.unpack_from("<I", raw, 0x24)[0]) == "2035-01-01"
    assert raw[0x30] == 3
    assert struct.unpack_from("<I", raw, 0x10)[0] == 123_456
    assert struct.unpack_from("<I", raw, 0x1C)[0] == 0x00080080
    assert (raw[0x33], raw[0x34], raw[0x36]) == (50, 6, 25)

    updated = club_affiliations.update_club_affiliation(
        parent, feeder, created["record_address"],
        expected_type=3, affiliation_type=16,
        expected_record=raw.hex(),
        updates={
            "annual_payment": 17_291_049,
            "benefits": 0x02 | 0x80 | 0x4000,
            "start_date": "2025-10-22",
            "end_date": "2030-06-30",
            "friendly_probability": 75,
            "maximum_players_loaned": 8,
            "parent_wage_percentage": 60,
        },
    )
    assert updated["type_name"] == "关系良好"
    updated_raw = reader.bytes(record, 0x38)
    updated_state = club_affiliations._record_state(updated_raw)
    assert updated_state["type"] == 16
    assert updated_state["annual_payment"] == 17_291_049
    assert updated_state["benefits"] == 0x4082
    assert club_affiliations._date_text(updated_state["start_date_raw"]) == "2025-10-22"
    assert club_affiliations._date_text(updated_state["end_date_raw"]) == "2030-06-30"
    assert updated_state["friendly_probability_raw"] == 75
    assert updated_state["maximum_players_loaned_raw"] == 8
    assert updated_state["parent_wage_percentage_raw"] == 60

    deleted = club_affiliations.delete_club_affiliation(
        parent, feeder, created["record_address"], expected_type=16,
    )
    assert deleted["operation"] == "delete"
    for team_id in (11, 22):
        vector = club_affiliations._read_vector(
            reader,
            club_affiliations._club_target(reader, reader.teams[team_id], team_id),
        )
        assert vector.pointers == ()

    reversed_relation = club_affiliations.create_club_affiliation(
        parent, feeder, 17,
        start_date="2025-01-01", end_date="2035-01-01",
        updates={"is_parent_club": False},
    )
    reversed_raw = reader.bytes(int(reversed_relation["record_address"], 0), 0x38)
    assert reversed_relation["parent_team_id"] == 22
    assert reversed_relation["feeder_team_id"] == 11
    assert struct.unpack_from("<Q", reversed_raw, 0x00)[0] == reader.clubs[22]
    assert struct.unpack_from("<Q", reversed_raw, 0x08)[0] == reader.clubs[11]


def test_fm26_native_creation_uses_global_registry_and_native_transaction(monkeypatch):
    reader = FakeReader()
    monkeypatch.setattr(reader.layout, "club_affiliation_create_rva", 0x1234, raising=False)
    monkeypatch.setattr(
        reader.layout, "club_affiliation_manager_rva", 0x400000, raising=False,
    )
    monkeypatch.setattr(
        club_affiliations, "_open_reader",
        lambda **_kwargs: (1, reader.layout, FakeContext()),
    )
    monkeypatch.setattr(club_affiliations, "Reader", lambda *_args: reader)
    monkeypatch.setattr(
        club_affiliations, "_allocate_native_block",
        lambda *_args: pytest.fail("FM26 native creation must not use UCRT malloc"),
    )
    record = 0x400000
    registry_reads = iter((
        (0x500000, club_affiliations._Vector(0x600000, 0, 0, 0, (), b"")),
        (0x500000, club_affiliations._Vector(
            0x600000, 0x700000, 0x700008, 0x700008,
            (record,), struct.pack("<Q", record),
        )),
    ))
    monkeypatch.setattr(
        club_affiliations, "_native_affiliation_registry",
        lambda _reader, *_args: next(registry_reads),
    )

    def native_create(
        _process, _reader, manager, raw, parent_id, feeder_id, _create_address,
    ):
        assert manager == 0x500000
        assert (parent_id, feeder_id) == (1011, 1022)
        reader.memory[record] = bytearray(raw)
        for index, team_id in enumerate((11, 22)):
            array = 0x710000 + index * 0x100
            reader.memory[array] = bytearray(struct.pack("<Q", record))
            reader.memory[reader.clubs[team_id] + 0x118] = bytearray(
                struct.pack("<QQQ", array, array + 8, array + 8)
            )
        return record

    monkeypatch.setattr(
        club_affiliations, "_native_create_affiliation", native_create,
    )
    created = club_affiliations.create_club_affiliation(
        {"id": 11, "address": hex(reader.teams[11])},
        {"id": 22, "address": hex(reader.teams[22])},
        3, start_date="2025-01-01", end_date="2035-01-01",
        updates={"annual_payment": 123, "benefits": 0x80},
    )
    assert created["record_address"] == hex(record)
    assert created["parent_team_id"] == 11
    assert created["feeder_team_id"] == 22


def test_fm26_native_creation_frame_matches_captured_abi():
    raw = bytearray(club_affiliations.AFFILIATION_RECORD_SIZE)
    struct.pack_into("<q", raw, 0x10, 123_456)
    struct.pack_into("<I", raw, 0x1C, 0x00080080)
    struct.pack_into("<I", raw, 0x20, 0x07E90001)
    struct.pack_into("<I", raw, 0x24, 0x07F30001)
    raw[0x30] = 3
    raw[0x33], raw[0x34], raw[0x36] = 50, 6, 25

    frame = club_affiliations._native_creation_frame(
        bytes(raw), 0x11110000, 0x22220000,
    )

    assert struct.unpack_from("<Q", frame, 0x20)[0] == 0x00080080
    assert struct.unpack_from("<Q", frame, 0x28)[0] == 0x11110000
    assert struct.unpack_from("<Q", frame, 0x30)[0] == 0x22220000
    assert struct.unpack_from("<Q", frame, 0x38)[0] == 0
    assert struct.unpack_from("<Q", frame, 0x40)[0] == 0
    assert struct.unpack_from("<Q", frame, 0x48)[0] == 123_456
    assert (frame[0x50], frame[0x58], frame[0x60], frame[0x68]) == (50, 0xFF, 6, 25)


def test_fm24_steam_uses_verified_native_affiliation_registry():
    assert FM24_LAYOUT.club_affiliation_create_rva == 0x23E63A0
    assert FM24_LAYOUT.club_affiliation_manager_rva == 0x642ECD0
    assert FM24_LAYOUT.club_affiliation_global_vector_offset == 0x680


def test_fm24_epic_resolves_native_entry_and_manager_from_patterns(monkeypatch):
    reader = FakeReader()
    reader.layout = FM24_EPIC_LAYOUT
    create = reader.module_base + 0x234560
    manager = reader.module_base + 0x654320
    call_hits = (reader.module_base + 0x1000, reader.module_base + 0x2000)

    monkeypatch.setattr(
        club_affiliations, "_scan_executable_patterns",
        lambda _reader, _patterns: {
            "create": [create], "manager": list(call_hits),
        },
    )
    for hit in call_hits:
        raw = bytearray(
            0 if value is None else value
            for value in FM24_EPIC_LAYOUT.club_affiliation_manager_call_pattern
        )
        struct.pack_into(
            "<i", raw, FM24_EPIC_LAYOUT.club_affiliation_manager_rel32_offset,
            manager - (hit + FM24_EPIC_LAYOUT.club_affiliation_manager_instruction_size),
        )
        struct.pack_into(
            "<i", raw, FM24_EPIC_LAYOUT.club_affiliation_create_call_rel32_offset,
            create - (hit + FM24_EPIC_LAYOUT.club_affiliation_create_call_instruction_size),
        )
        reader.memory[hit] = raw

    assert club_affiliations._resolved_native_affiliation_addresses(reader) == (
        manager, create,
    )


def test_fm24_epic_rejects_ambiguous_native_call_chain(monkeypatch):
    reader = FakeReader()
    reader.layout = FM24_EPIC_LAYOUT
    create = reader.module_base + 0x234560
    call_hit = reader.module_base + 0x1000
    monkeypatch.setattr(
        club_affiliations, "_scan_executable_patterns",
        lambda _reader, _patterns: {
            "create": [create], "manager": [call_hit],
        },
    )
    raw = bytearray(
        0 if value is None else value
        for value in FM24_EPIC_LAYOUT.club_affiliation_manager_call_pattern
    )
    struct.pack_into(
        "<i", raw, FM24_EPIC_LAYOUT.club_affiliation_manager_rel32_offset,
        0x1000,
    )
    struct.pack_into(
        "<i", raw, FM24_EPIC_LAYOUT.club_affiliation_create_call_rel32_offset,
        0x2000,
    )
    reader.memory[call_hit] = raw

    with pytest.raises(RuntimeError, match="调用链解析不唯一"):
        club_affiliations._resolved_native_affiliation_addresses(reader)


def test_relation_detail_update_preserves_unknown_benefit_bits():
    raw = bytearray(club_affiliations.AFFILIATION_RECORD_SIZE)
    struct.pack_into("<Q", raw, 0x00, 0x1000)
    struct.pack_into("<Q", raw, 0x08, 0x2000)
    struct.pack_into("<I", raw, 0x1C, 0x40000000 | 0x02)
    raw[0x30] = 1
    raw[0x33] = raw[0x34] = raw[0x36] = 0xFF
    state = club_affiliations._record_state(bytes(raw))

    updated = club_affiliations._updated_record(bytes(raw), state, {
        "type": 1, "benefits": 0x80,
    })

    assert struct.unpack_from("<I", updated, 0x1C)[0] == 0x40000080
    payload = club_affiliations._record_payload(updated, club_affiliations._record_state(updated))
    assert payload["unknown_benefits"] == 0x40000000
    assert payload["friendly_probability"] is None


def test_relation_detail_can_reverse_main_club_direction():
    raw = bytearray(club_affiliations.AFFILIATION_RECORD_SIZE)
    struct.pack_into("<Q", raw, 0x00, 0x1000)
    struct.pack_into("<Q", raw, 0x08, 0x2000)
    raw[0x30] = 1
    raw[0x33] = raw[0x34] = raw[0x36] = 0xFF
    state = club_affiliations._record_state(bytes(raw))

    updated = club_affiliations._updated_record(bytes(raw), state, {
        "type": 1, "is_parent_club": False,
    })

    assert struct.unpack_from("<Q", updated, 0x00)[0] == 0x2000
    assert struct.unpack_from("<Q", updated, 0x08)[0] == 0x1000


def test_relation_detail_rejects_invalid_ranges_and_reversed_dates():
    raw = bytearray(club_affiliations.AFFILIATION_RECORD_SIZE)
    struct.pack_into("<Q", raw, 0x00, 0x1000)
    struct.pack_into("<Q", raw, 0x08, 0x2000)
    raw[0x30] = 1
    raw[0x33] = raw[0x34] = raw[0x36] = 0xFF
    state = club_affiliations._record_state(bytes(raw))

    with pytest.raises(ValueError, match="友谊赛概率"):
        club_affiliations._updated_record(bytes(raw), state, {
            "type": 1, "friendly_probability": 101,
        })
    with pytest.raises(ValueError, match="最多租借人数"):
        club_affiliations._updated_record(bytes(raw), state, {
            "type": 1, "maximum_players_loaned": 0,
        })
    with pytest.raises(ValueError, match="结束日期不能早于开始日期"):
        club_affiliations._updated_record(bytes(raw), state, {
            "type": 1, "start_date": "2030-01-01", "end_date": "2029-12-31",
        })
    for dependent in (0x00080000, 0x00100000, 0x00200000):
        with pytest.raises(ValueError, match="相同的董事会"):
            club_affiliations._updated_record(bytes(raw), state, {
                "type": 1, "benefits": dependent,
            })
    with pytest.raises(ValueError, match="支付工资比例"):
        club_affiliations._updated_record(bytes(raw), state, {
            "type": 1, "parent_wage_percentage": 50,
        })
    linked = club_affiliations._updated_record(bytes(raw), state, {
        "type": 1, "benefits": 0x00380000 | 0x80,
        "parent_wage_percentage": 50,
    })
    assert struct.unpack_from("<I", linked, 0x1C)[0] == 0x00380080
    assert linked[0x36] == 50


def _relation_state(monkeypatch):
    state = fm_odds_web.LocalOddsState.__new__(fm_odds_web.LocalOddsState)
    state.lock = threading.RLock()
    state.memory_lock = threading.RLock()
    state.output = {"save_instance_id": "save-1"}
    state._bind_current_save = lambda: "scope-1"
    owned = [
        {"id": 11, "name": "Parent"},
        {"id": 22, "name": "Feeder"},
    ]
    native = {
        11: {"id": 11, "name": "Parent", "address": "0x200000"},
        22: {"id": 22, "name": "Feeder", "address": "0x210000"},
    }
    state._owned_world_club_target = lambda team_id: (
        "scope-1", state.output, native[team_id],
    )
    state._owned_world_club_relation_targets = lambda: list(native.values())
    monkeypatch.setattr(fm_odds_web, "account_acquired_clubs", lambda *_args: owned)
    return state


def test_relation_targets_batch_address_recovery_to_two_resolver_calls(monkeypatch):
    state = object.__new__(fm_odds_web.LocalOddsState)
    state.lock = threading.RLock()
    state.output = {"save_instance_id": "save-1"}
    state._bind_current_save = lambda: "scope-1"
    state._world_club_native_cache = lambda _save_id: {"clubs": []}
    state.owned_world_club_addresses = {
        team_id: hex(0x200000 + team_id * 0x1000)
        for team_id in range(1, 11)
    }
    state.owned_world_club_address_save_id = "save-1"
    owned = [
        {"id": team_id, "name": f"Club {team_id}"}
        for team_id in range(1, 11)
    ]
    monkeypatch.setattr(
        fm_odds_web, "account_acquired_clubs", lambda *_args, **_kwargs: owned,
    )
    monkeypatch.setattr(
        fm_odds_web, "world_club_team_address_hints", lambda _output: set(),
    )
    calls = []

    def resolve(team_ids, _hints, *, scan_all_if_unresolved, known_addresses):
        requested = set(team_ids)
        calls.append((requested, scan_all_if_unresolved, dict(known_addresses)))
        if not scan_all_if_unresolved:
            return {
                team_id: state.owned_world_club_addresses[team_id]
                for team_id in requested if team_id != 10
            }
        return {10: state.owned_world_club_addresses[10]}

    monkeypatch.setattr(fm_odds_web, "resolve_native_team_addresses", resolve)

    targets = state._owned_world_club_relation_targets()

    assert len(targets) == 10
    assert calls[0][0] == set(range(1, 11))
    assert calls[0][1] is False
    assert calls[1][0] == {10}
    assert calls[1][1] is True
    assert len(calls) == 2
    assert state.owned_world_club_address_save_id == "save-1"
    assert state.owned_world_club_addresses[10] == hex(0x200000 + 10 * 0x1000)


def test_relation_state_limits_writes_to_two_active_owned_clubs(monkeypatch):
    state = _relation_state(monkeypatch)
    calls = []
    monkeypatch.setattr(
        fm_odds_web, "create_club_affiliation",
        lambda parent, feeder, relation_type, **kwargs: calls.append(
            (parent["id"], feeder["id"], relation_type, kwargs),
        ) or {"operation": "create"},
    )
    monkeypatch.setattr(
        fm_odds_web, "read_club_affiliations",
        lambda clubs: {"types": [], "relations": [], "seen": [row["id"] for row in clubs]},
    )

    result = state.update_owned_world_club_relation({
        "operation": "create", "parent_team_id": 11,
        "feeder_team_id": 22, "type": 17,
        "start_date": "2025-01-01", "end_date": "2035-01-01",
    })

    assert calls == [(
        11, 22, 17,
        {
            "start_date": "2025-01-01",
            "end_date": "2035-01-01",
            "updates": {
                "operation": "create", "parent_team_id": 11,
                "feeder_team_id": 22, "type": 17,
                "start_date": "2025-01-01", "end_date": "2035-01-01",
            },
        },
    )]
    assert result["seen"] == [11, 22]
    with pytest.raises(ValueError, match="当前已生效的收购俱乐部"):
        state.update_owned_world_club_relation({
            "operation": "create", "parent_team_id": 11,
            "feeder_team_id": 33, "type": 17,
        })


def test_relation_state_forwards_full_detail_update(monkeypatch):
    state = _relation_state(monkeypatch)
    captured = {}

    def update(parent, feeder, address, **kwargs):
        captured.update({
            "parent": parent["id"], "feeder": feeder["id"],
            "address": address, **kwargs,
        })
        return {"operation": "update"}

    monkeypatch.setattr(fm_odds_web, "update_club_affiliation", update)
    monkeypatch.setattr(
        fm_odds_web, "read_club_affiliations",
        lambda clubs: {"types": [], "benefit_options": [], "relations": []},
    )
    payload = {
        "operation": "update", "parent_team_id": 11, "feeder_team_id": 22,
        "record_address": "0x400000", "expected_record": "00" * 0x38,
        "expected_type": 1, "type": 16, "annual_payment": 123,
        "benefits": 0x80, "friendly_probability": 75,
    }

    state.update_owned_world_club_relation(payload)

    assert captured["parent"] == 11
    assert captured["feeder"] == 22
    assert captured["expected_record"] == "00" * 0x38
    assert captured["updates"] is payload
    assert captured["updates"]["benefits"] == 0x80
