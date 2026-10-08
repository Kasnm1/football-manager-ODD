from __future__ import annotations

import struct
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from tools import club_reader


class FakeBlackRoomReader:
    def __init__(self, layout):
        self.layout = layout
        self.module_base = 0x50000000
        self.player = 0x1000000
        self.team = 0x2000000
        self.container = 0x3000000
        self.injury_pointer = self.player + layout.player_injury_list_offset
        self.memory: dict[int, bytearray] = {
            self.injury_pointer: bytearray(struct.pack("<Q", self.container)),
            self.container: bytearray(0x30),
        }

    def roster(self, _team):
        return [{"id": 42, "address": hex(self.player)}]

    def bytes(self, address, size):
        raw = self.memory.get(address)
        return bytes(raw[:size]) if raw is not None and len(raw) >= size else None

    def ptr(self, address):
        raw = self.bytes(address, 8)
        return struct.unpack("<Q", raw)[0] if raw else 0

    def fm_string_at(self, address):
        return "脚趾骨折" if address == 0x6000000 + 0x18 else None

    def _availability_details(self, container):
        begin, end, _capacity = struct.unpack("<QQQ", self.bytes(container, 24))
        if end - begin != 8:
            return None
        record = self.ptr(begin)
        raw = self.bytes(record, 0x50)
        if not raw:
            return None
        low, high = struct.unpack_from("<HH", raw, 0x28)
        return {
            "injury_count": 1,
            "injuries": [{
                "type": "脚趾骨折",
                "duration_days_low_raw": low,
                "duration_days_high_raw": high,
            }],
        }


def black_room_runtime(monkeypatch):
    layout = SimpleNamespace(
        display_name="FM26 Steam",
        module_name="game_plugin.dll",
        player_injury_list_offset=0xF8,
        module=lambda _process: SimpleNamespace(base_address=0x50000000),
    )
    reader = FakeBlackRoomReader(layout)
    process = SimpleNamespace(pid=7)
    allocations = iter(((0x4000000, 0x9000000), (0x5000000, 0x9000000)))
    freed = []
    template = bytearray(0x50)
    struct.pack_into("<Q", template, 0x00, 0x7000000)
    struct.pack_into("<Q", template, 0x08, 0x6000000)
    struct.pack_into("<HH", template, 0x28, 10, 20)

    def write_memory(_process, address, data):
        existing = reader.memory.get(address, bytearray())
        size = max(len(existing), len(data))
        merged = bytearray(size)
        merged[:len(existing)] = existing
        merged[:len(data)] = data
        reader.memory[address] = merged

    monkeypatch.setattr(club_reader, "select_process_layout", lambda: (7, "fm.exe", layout))
    monkeypatch.setattr(club_reader, "open_process", lambda *_args, **_kwargs: nullcontext(process))
    monkeypatch.setattr(club_reader, "Reader", lambda *_args: reader)
    monkeypatch.setattr(club_reader, "_resolve_team_address", lambda *_args: reader.team)
    monkeypatch.setattr(club_reader, "_validated_player_person", lambda *_args: 0x1100)
    monkeypatch.setattr(
        club_reader, "_native_injury_templates",
        lambda *_args, **_kwargs: [{
            "name": "脚趾骨折", "duration_days_low": 10,
            "duration_days_high": 20, "record": bytes(template),
        }],
    )
    monkeypatch.setattr(club_reader, "_remote_malloc_block", lambda *_args: next(allocations))
    monkeypatch.setattr(
        club_reader, "_remote_free_block",
        lambda _process, _free, address: freed.append(address),
    )
    monkeypatch.setattr(club_reader, "write_process_memory", write_memory)
    return reader, freed


def test_template_selection_filters_duration_and_prioritizes_fractures():
    selected = club_reader._select_random_world_injury_template([
        {"name": "流感", "duration_days_low": 3, "duration_days_high": 5},
        {"name": "腿筋拉伤", "duration_days_low": 10, "duration_days_high": 18},
        {"name": "脚趾骨折", "duration_days_low": 14, "duration_days_high": 28},
        {"name": "十字韧带损伤", "duration_days_low": 120, "duration_days_high": 180},
    ], rng=SimpleNamespace(choice=lambda rows: rows[0]))

    assert selected["name"] == "脚趾骨折"


def test_template_selection_rejects_world_without_eligible_injury():
    with pytest.raises(RuntimeError, match="7-30 天"):
        club_reader._select_random_world_injury_template([
            {"name": "流感", "duration_days_low": 3, "duration_days_high": 5},
            {"name": "重伤", "duration_days_low": 40, "duration_days_high": 60},
        ])


def test_black_room_clones_world_injury_and_rebinds_target(monkeypatch):
    reader, freed = black_room_runtime(monkeypatch)

    result = club_reader.create_random_world_injury(
        42, hex(reader.player), "2030-01-10",
        team_id=9, team_address=hex(reader.team),
    )

    begin, end, capacity = struct.unpack("<QQQ", reader.bytes(reader.container, 24))
    record = reader.ptr(begin)
    record_raw = reader.bytes(record, 0x50)
    assert (end, capacity) == (begin + 8, begin + 8)
    assert struct.unpack_from("<Q", record_raw, 0x00)[0] == 0
    assert struct.unpack_from("<Q", record_raw, 0x18)[0] == reader.team
    assert struct.unpack_from("<HH", record_raw, 0x28) == (10, 20)
    assert result["injury"] == "脚趾骨折"
    assert result["cause"] == "unknown"
    assert result["fracture_preferred"] is True
    assert freed == []


def test_black_room_restores_container_and_frees_allocations_on_failure(monkeypatch):
    reader, freed = black_room_runtime(monkeypatch)
    original = reader.bytes(reader.container, 0x30)
    monkeypatch.setattr(reader, "_availability_details", lambda _container: None)

    with pytest.raises(RuntimeError, match="写入校验失败"):
        club_reader.create_random_world_injury(
            42, hex(reader.player), "2030-01-10",
            team_id=9, team_address=hex(reader.team),
        )

    assert reader.bytes(reader.container, 0x30) == original
    assert freed == [0x5000000, 0x4000000]
